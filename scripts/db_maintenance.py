#!/usr/bin/env python3
"""Safe offline SQLite backup/restore helpers for local Studio data.

Backups use SQLite's online backup API (including committed WAL contents). Restore
requires Studio to be stopped. Backups are local user data and are chmod 0600.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
KEYRING_REF_PREFIX = "keyring:v1:"
SENSITIVE_PARTS = ("authorization", "cookie", "token", "secret", "password", "api-key", "apikey", "access-key", "accesskey", "private-key", "privatekey", "credential")
DEFAULT_DB = Path(os.environ.get("STUDIO_DB_PATH", str(Path(__file__).resolve().parents[1] / "studio_history.db"))).expanduser()


class MaintenanceError(RuntimeError):
    pass


def _sensitive_name(name: object) -> bool:
    normalized = str(name or "").lower().replace("_", "-")
    return any(part in normalized for part in SENSITIVE_PARTS)


def _validate_no_plaintext_secrets(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "cloud_providers" in tables:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("cloud_providers")')}
        if "api_key" in cols:
            for (value,) in conn.execute("SELECT api_key FROM cloud_providers WHERE api_key IS NOT NULL AND api_key != ''"):
                if not str(value).startswith(KEYRING_REF_PREFIX):
                    raise MaintenanceError("Database still contains a plaintext provider credential. Start Studio once to migrate secrets before backing up.")
    if "mcp_servers" in tables:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("mcp_servers")')}
        for column in ("auth_token", "headers_json", "env_json"):
            if column not in cols:
                continue
            for (value,) in conn.execute(f'SELECT "{column}" FROM mcp_servers'):
                if column == "auth_token":
                    values = {column: value}
                else:
                    try:
                        decoded = json.loads(value or "{}")
                    except (TypeError, json.JSONDecodeError):
                        raise MaintenanceError(f"Cannot safely inspect mcp_servers.{column}; repair or migrate the record first.")
                    values = decoded if isinstance(decoded, dict) else {}
                for key, secret in values.items():
                    if _sensitive_name(key) and secret and not str(secret).startswith(KEYRING_REF_PREFIX):
                        raise MaintenanceError("Database still contains plaintext MCP credentials. Start Studio once to migrate secrets before backing up.")


def _validate_database(path: Path, *, reject_plaintext_secrets: bool = True) -> int:
    if not path.is_file():
        raise MaintenanceError(f"Database does not exist or is not a regular file: {path}")
    try:
        with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise MaintenanceError(f"SQLite integrity_check failed: {result[0] if result else 'no result'}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise MaintenanceError(f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}.")
            if reject_plaintext_secrets:
                _validate_no_plaintext_secrets(conn)
            return version
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError(f"Not a readable SQLite database: {exc}") from exc


def _secure_mode(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise MaintenanceError(f"Could not set private permissions on {path}: {exc}") from exc


def backup_database(source: Path, destination: Path) -> Path:
    source, destination = source.expanduser().resolve(), destination.expanduser().resolve()
    if source == destination:
        raise MaintenanceError("Backup destination must differ from the source database.")
    _validate_database(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise MaintenanceError(f"Refusing to overwrite an existing backup: {destination}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        _secure_mode(temp)
        with sqlite3.connect(source, timeout=30) as src, sqlite3.connect(temp, timeout=30) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        _validate_database(temp)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        _secure_mode(destination)
        try:
            dir_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return destination
    finally:
        temp.unlink(missing_ok=True)


def restore_database(source: Path, destination: Path, *, force: bool = False) -> tuple[Path, Path | None]:
    """Restore a validated backup; return (restored path, safety-backup path)."""
    source, destination = source.expanduser().resolve(), destination.expanduser().resolve()
    if source == destination:
        raise MaintenanceError("Restore source and destination must differ.")
    _validate_database(source)
    if destination.exists() and not force:
        raise MaintenanceError("Destination already exists; pass --force only after stopping Studio.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise MaintenanceError("SQLite WAL/SHM sidecars exist. Stop Studio cleanly before restoring.")

    safety_backup = None
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_backup = destination.with_name(f"{destination.name}.pre-restore-{stamp}.bak")
        backup_database(destination, safety_backup)

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".restore.tmp", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        _secure_mode(temp)
        with sqlite3.connect(source, timeout=30) as src, sqlite3.connect(temp, timeout=30) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        _validate_database(temp)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, destination)
        _secure_mode(destination)
        return destination, safety_backup
    finally:
        temp.unlink(missing_ok=True)


def _default_backup_path(source: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return source.with_name(f"{source.stem}-{stamp}.backup.sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="Create a private, integrity-checked backup")
    backup.add_argument("--database", type=Path, default=DEFAULT_DB)
    backup.add_argument("--output", type=Path)
    restore = sub.add_parser("restore", help="Restore a backup while Studio is stopped")
    restore.add_argument("--database", type=Path, default=DEFAULT_DB)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--force", action="store_true", help="Replace existing database after creating a safety backup")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            output = backup_database(args.database, args.output or _default_backup_path(args.database))
            print(f"Backup created and validated: {output}")
        else:
            restored, safety = restore_database(args.input, args.database, force=args.force)
            print(f"Database restored and validated: {restored}")
            if safety:
                print(f"Pre-restore safety backup: {safety}")
        return 0
    except (MaintenanceError, OSError, sqlite3.Error) as exc:
        print(f"Database maintenance failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
