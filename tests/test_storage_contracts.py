from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app as studio
from scripts.db_maintenance import backup_database, restore_database


def test_storage_connection_seam_is_injectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "studio.db"
    monkeypatch.setattr(studio, "DB_PATH", database)
    studio.init_db()
    opened: list[tuple[Path, float]] = []
    sqlite_storage = studio.SQLiteStorage()

    class RecordingStorage:
        def connect(self, path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
            opened.append((path, timeout))
            return sqlite_storage.connect(path, timeout=timeout)

    monkeypatch.setattr(studio, "STORAGE", RecordingStorage())
    assert studio.list_cloud_providers() == []
    assert opened == [(database, 5.0)]


def test_storage_failure_is_not_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingStorage:
        def connect(self, _path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
            raise sqlite3.OperationalError(f"backend unavailable after {timeout}s")

    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "unavailable.db")
    monkeypatch.setattr(studio, "STORAGE", FailingStorage())
    with pytest.raises(sqlite3.OperationalError, match="backend unavailable"):
        studio.list_cloud_providers()


def test_schema_upgrade_preserves_rows_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "version-one.db"
    monkeypatch.setattr(studio, "DB_PATH", database)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        conn.execute("INSERT INTO runs(id, title) VALUES (7, 'kept')")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        conn.execute("PRAGMA user_version = 1")

    studio.init_db()
    studio.init_db()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT title FROM runs WHERE id = 7").fetchone()[0] == "kept"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == studio.SCHEMA_VERSION == 3
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,)]


def test_schema_failure_rolls_back_partial_ddl(tmp_path: Path) -> None:
    database = tmp_path / "rollback.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            "CREATE TRIGGER force_migration_failure BEFORE INSERT ON schema_migrations "
            "BEGIN SELECT RAISE(ABORT, 'deliberate migration failure'); END"
        )

    with sqlite3.connect(database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="deliberate migration failure"):
            studio._apply_schema_migrations(conn)
        assert {row[1] for row in conn.execute("PRAGMA table_info(runs)")} == {"id"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == []


def test_verified_backup_restores_after_failed_migration(tmp_path: Path) -> None:
    database = tmp_path / "recover.db"
    backup = tmp_path / "pre-migration.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        conn.execute("INSERT INTO runs(id, title) VALUES (4, 'preserved')")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        conn.execute("PRAGMA user_version = 1")
    backup_database(database, backup)

    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TRIGGER force_migration_failure BEFORE INSERT ON schema_migrations "
            "BEGIN SELECT RAISE(ABORT, 'deliberate migration failure'); END"
        )
    with sqlite3.connect(database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="deliberate migration failure"):
            studio._apply_schema_migrations(conn)

    restored, safety = restore_database(backup, database, force=True)
    assert restored == database.resolve()
    assert safety is not None and safety.is_file()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT title FROM runs WHERE id = 4").fetchone()[0] == "preserved"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
