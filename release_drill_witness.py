"""AP-032 drill witness — perform the rollback, migration and restore drills, then measure them.

`release_qualification.DrillRecord` is a claim. This module is what makes the claim true: it runs the
three drills against a throwaway database, measures the outcome, and hands back records whose digests
were computed from the actual bytes.

Three distinctions this module refuses to blur:

1. **Drills run against a scratch database, never the operator's history.** Every path is created
   under a private temporary directory. The module refuses a source database it did not create, so a
   caller cannot accidentally run a "restore" that overwrites real data.

2. **Restore is compared logically, and says so.** A SQLite backup taken with the online backup API
   and restored into a fresh file is *logically* identical but not byte-identical: page layout,
   freelist state and header fields legitimately differ. Measuring this repository's own
   `scripts/db_maintenance.py` round-trip gives equal logical digests and unequal file digests. A
   drill that compared file hashes would report a false failure; one that claimed a byte match would
   overclaim. So the logical digest is used and labelled `logical-content`.

3. **A drill that did not run is not a drill that passed.** Each record is `exercised=True` only after
   the measurement agrees. A mismatch raises rather than returning a soft "probably fine".

The logical digest covers ordered table names, ordered rows from each table (ordered by primary key
where one exists), and `PRAGMA user_version`, so a restore that silently dropped the schema version or
reordered rows is caught.

Stdlib only. No network, no clock, no writes outside the temporary directory it creates.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "DrillWitnessError",
    "SCHEMA_VERSION",
    "logical_digest",
    "file_digest",
    "run_drills",
    "DrillEvidence",
]

#: The schema version the migration drill migrates *to*; read from the module that owns it.
SCHEMA_VERSION = 4

_DRILL_KINDS = ("rollback", "migration", "restore")


class DrillWitnessError(RuntimeError):
    """A drill could not be run, or its measurement disagreed with the claim."""


@dataclass(frozen=True, slots=True)
class DrillEvidence:
    """The measured result of one drill, before it becomes a `DrillRecord`."""

    kind: str
    exercised: bool
    source_digest: str = ""
    restored_digest: str = ""
    comparison: str = ""
    detail: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "exercised": self.exercised,
            "source_digest": self.source_digest,
            "restored_digest": self.restored_digest,
            "comparison": self.comparison,
            "detail": self.detail,
        }


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)


def logical_digest(path: Path) -> str:
    """A digest of what a database *contains*, not how its pages happen to be laid out.

    Ordered by table name and, where a table has an INTEGER PRIMARY KEY, by that key; otherwise by
    rowid. `PRAGMA user_version` is included because a restore that silently downgraded the schema
    would otherwise look identical.
    """
    if not path.is_file():
        raise DrillWitnessError(f"no database at {path}")
    digest = hashlib.sha256()
    try:
        with _connect(path) as conn:
            return _digest_open_database(conn, digest)
    except sqlite3.DatabaseError as exc:
        # A file that is not SQLite must raise this module's error, not leak a sqlite3 type the
        # caller has no reason to know about.
        raise DrillWitnessError(f"not a readable SQLite database: {path.name}: {exc}") from exc


def _digest_open_database(conn: sqlite3.Connection, digest: "hashlib._Hash") -> str:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise DrillWitnessError(f"integrity_check failed: {integrity}")
    digest.update(f"user_version={int(conn.execute('PRAGMA user_version').fetchone()[0])};".encode())
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        digest.update(f"table={table};".encode())
        columns = list(conn.execute(f'PRAGMA table_info("{table}")'))
        pk = [col[1] for col in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
        order = f'ORDER BY {", ".join(f"{c}" for c in pk)}' if pk else "ORDER BY rowid"
        for row in conn.execute(f'SELECT * FROM "{table}" {order}'):
            digest.update(repr(row).encode())
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    """The digest of the file's bytes. Recorded for transparency, never used to prove a restore."""
    if not path.is_file():
        raise DrillWitnessError(f"no file at {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed(path: Path) -> None:
    """A schema-v1 database with rows worth preserving, as an older release would leave one."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=1")
        conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO runs(name) VALUES(?)", [("first",), ("second",), ("third",)])
        conn.executemany("INSERT INTO notes(body) VALUES(?)", [("alpha",), ("beta",)])
        conn.commit()


def _migrate(path: Path, *, schema_version: int) -> int:
    """Apply the repository's own migrations, returning the resulting user_version."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runtime_persistence  # imported here so the module stays cheap to import in tests

    with sqlite3.connect(path) as conn:
        runtime_persistence.apply_schema_migrations(conn, target_version=schema_version)
        conn.commit()
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _check_scratch(path: Path, root: Path) -> Path:
    """Refuse any database this module did not create inside its own scratch directory."""
    resolved = path.resolve()
    if root.resolve() not in resolved.parents:
        raise DrillWitnessError(
            f"{resolved} is outside the drill's scratch directory; these drills never touch a real database"
        )
    return resolved


def run_drills(*, schema_version: int = SCHEMA_VERSION) -> tuple[DrillEvidence, ...]:
    """Perform all three drills in a scratch directory and return measured evidence.

    Raises :class:`DrillWitnessError` if any measurement disagrees with the expected outcome — a
    drill that did not do what it claims is not evidence, and returning a soft record would be the
    exact "command ran, therefore restore works" claim this story refuses.
    """
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise DrillWitnessError("schema_version must be a positive integer")

    evidence: list[DrillEvidence] = []
    with tempfile.TemporaryDirectory(prefix="ap032-drills-") as raw_root:
        root = Path(raw_root).resolve()

        # --- restore ------------------------------------------------------
        # Take a backup, mutate the live file, restore, and compare logical content.
        from importlib import import_module

        sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
        db_maintenance = import_module("db_maintenance")

        live = _check_scratch(root / "live.sqlite3", root)
        _seed(live)
        _migrate(live, schema_version=schema_version)
        source_logical = logical_digest(live)
        source_file = file_digest(live)

        backup = _check_scratch(root / "backup.sqlite3", root)
        db_maintenance.backup_database(live, backup)

        with sqlite3.connect(live) as conn:
            conn.execute("INSERT INTO runs(name) VALUES('written-after-the-backup')")
            conn.commit()
        if logical_digest(live) == source_logical:
            raise DrillWitnessError("the post-backup mutation did not change the database, so restore proves nothing")

        restored, _safety = db_maintenance.restore_database(backup, live, force=True)
        restored_logical = logical_digest(restored)
        restored_file = file_digest(restored)
        if restored_logical != source_logical:
            raise DrillWitnessError(
                "restore did not reproduce the backup's logical content; the drill failed, not the measurement"
            )
        evidence.append(
            DrillEvidence(
                kind="restore",
                exercised=True,
                source_digest=source_logical,
                restored_digest=restored_logical,
                comparison="logical-content",
                detail=(
                    "backup, mutate, restore, re-read: identical logical content. File digests differ "
                    f"({source_file[:12]} vs {restored_file[:12]}), which is expected for SQLite and is why the "
                    "comparison is logical."
                ),
            )
        )

        # --- migration ----------------------------------------------------
        # A schema-v1 database must reach the current version with its rows intact.
        legacy = _check_scratch(root / "legacy.sqlite3", root)
        _seed(legacy)
        before_projection = _columns(legacy)
        before_rows = _row_snapshot(legacy)
        before_version = int(_version(legacy))
        if before_version != 1:
            raise DrillWitnessError(f"the legacy fixture is at version {before_version}, expected 1")
        after_version = _migrate(legacy, schema_version=schema_version)
        after_rows = _row_snapshot(legacy, projection=before_projection)
        if after_version != schema_version:
            raise DrillWitnessError(f"migration reached version {after_version}, expected {schema_version}")
        if after_rows != before_rows:
            raise DrillWitnessError("migration changed the values of pre-existing columns; that is data loss")
        added = {
            table: [col for col in cols if col not in before_projection.get(table, ())]
            for table, cols in _columns(legacy).items()
        }
        added = {table: cols for table, cols in added.items() if cols}
        legacy_additions = added.get("runs", [])
        evidence.append(
            DrillEvidence(
                kind="migration",
                exercised=True,
                detail=(
                    f"schema v{before_version} -> v{after_version}; {len(before_rows)} pre-existing rows kept "
                    f"every original column value; {len(added)} tables gained columns, including "
                    f"runs: {legacy_additions}"
                ),
            )
        )

        # --- rollback -----------------------------------------------------
        # Reverse the migration the way an operator would: restore the pre-migration backup and
        # confirm the database is back to v1 with the original rows, then migrate forward again.
        rollback_backup = _check_scratch(root / "pre-migration.sqlite3", root)
        fresh = _check_scratch(root / "rollback-source.sqlite3", root)
        _seed(fresh)
        db_maintenance.backup_database(fresh, rollback_backup)
        _migrate(fresh, schema_version=schema_version)
        if int(_version(fresh)) != schema_version:
            raise DrillWitnessError("the rollback fixture never reached the migrated version")
        rolled_back, _safety2 = db_maintenance.restore_database(rollback_backup, fresh, force=True)
        rolled_version = int(_version(rolled_back))
        if rolled_version != 1:
            raise DrillWitnessError(f"rollback left the database at v{rolled_version}, expected v1")
        if _row_snapshot(rolled_back) != before_rows:
            raise DrillWitnessError("rollback did not restore the original rows")
        recovered_version = _migrate(rolled_back, schema_version=schema_version)
        if recovered_version != schema_version:
            raise DrillWitnessError("the rolled-back database could not be migrated forward again")
        evidence.append(
            DrillEvidence(
                kind="rollback",
                exercised=True,
                detail=(
                    f"v{schema_version} -> restore pre-migration backup -> v1 with the original rows -> "
                    f"v{recovered_version} again; rollback is reversible, not one-way"
                ),
            )
        )

    by_kind = {item.kind: item for item in evidence}
    missing = [kind for kind in _DRILL_KINDS if kind not in by_kind]
    if missing:
        raise DrillWitnessError(f"drills did not run: {missing}")
    return tuple(by_kind[kind] for kind in _DRILL_KINDS)


def _version(path: Path) -> int:
    with _connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _columns(path: Path) -> dict[str, tuple[str, ...]]:
    """The column names of every user table, so a projection can survive an additive migration."""
    found: dict[str, tuple[str, ...]] = {}
    with _connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            if table == "schema_migrations":
                continue
            found[table] = tuple(col[1] for col in conn.execute(f'PRAGMA table_info("{table}")'))
    return found


def _row_snapshot(
    path: Path, *, projection: dict[str, tuple[str, ...]] | None = None
) -> tuple[tuple[Any, ...], ...]:
    """Rows as (table, *values), optionally only the named columns.

    A projection is what makes an *additive* migration measurable. The repository's real migration
    from v1 to v4 adds columns (``created_at``, ``mode``, ``title``, ``payload_json``) with defaults to
    a legacy ``runs`` table. Comparing whole rows would report that as changed data; comparing the
    columns that existed before asserts the thing that actually matters — the old values survived.
    """
    rows: list[tuple[Any, ...]] = []
    with _connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            if table == "schema_migrations":
                continue
            if projection is not None and table not in projection:
                continue
            columns = projection[table] if projection is not None else _columns(path)[table]
            selected = ", ".join(f'"{col}"' for col in columns)
            for row in conn.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid'):
                rows.append((table,) + tuple(row))
    return tuple(rows)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the drills and print their evidence; exit non-zero if any drill disagrees."""
    try:
        evidence = run_drills()
    except DrillWitnessError as exc:
        print(f"drill witness failed: {exc}", file=sys.stderr)
        return 1
    for item in evidence:
        digest = f" {item.source_digest[:12]}=={item.restored_digest[:12]}" if item.source_digest else ""
        print(f"{item.kind:10} exercised={item.exercised}{digest} {item.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
