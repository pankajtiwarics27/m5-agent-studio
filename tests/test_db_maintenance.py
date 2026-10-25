from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from scripts.db_maintenance import MaintenanceError, backup_database, restore_database


def make_database(path: Path, version: int = 1) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        conn.execute("INSERT INTO runs(title) VALUES ('preserved')")
        conn.execute(f"PRAGMA user_version = {version}")


def test_backup_is_consistent_private_and_non_overwriting(tmp_path: Path) -> None:
    db = tmp_path / "studio.db"
    backup = tmp_path / "backups" / "snapshot.sqlite3"
    make_database(db)
    result = backup_database(db, backup)
    assert result == backup.resolve()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT title FROM runs").fetchone()[0] == "preserved"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(MaintenanceError, match="overwrite"):
        backup_database(db, backup)


def test_restore_requires_explicit_force_and_creates_safety_backup(tmp_path: Path) -> None:
    live = tmp_path / "studio.db"
    snapshot = tmp_path / "snapshot.db"
    make_database(live)
    make_database(snapshot)
    with sqlite3.connect(live) as conn:
        conn.execute("UPDATE runs SET title='current data'")
    with pytest.raises(MaintenanceError, match="--force"):
        restore_database(snapshot, live)
    restored, safety = restore_database(snapshot, live, force=True)
    assert restored == live.resolve()
    assert safety is not None and safety.is_file()
    with sqlite3.connect(live) as conn:
        assert conn.execute("SELECT title FROM runs").fetchone()[0] == "preserved"
    with sqlite3.connect(safety) as conn:
        assert conn.execute("SELECT title FROM runs").fetchone()[0] == "current data"


def test_backup_refuses_plaintext_credentials_and_future_schema(tmp_path: Path) -> None:
    db = tmp_path / "studio.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cloud_providers(alias TEXT, api_key TEXT)")
        conn.execute("INSERT INTO cloud_providers VALUES ('test', 'cleartext-secret')")
    with pytest.raises(MaintenanceError, match="plaintext provider credential"):
        backup_database(db, tmp_path / "backup.db")

    future = tmp_path / "future.db"
    make_database(future, version=999)
    with pytest.raises(MaintenanceError, match="newer than supported"):
        backup_database(future, tmp_path / "future-backup.db")
