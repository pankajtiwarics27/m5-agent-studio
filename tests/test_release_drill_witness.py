"""AP-032 drill witness — the module that turns drill claims into measurements.

These tests run the drills for real against scratch databases. They are deliberately not mocking the
SQLite layer: the whole point of the witness is that the numbers come from actual backup, restore and
migration calls, so a test that stubbed them would prove nothing about the claims it backs.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

import release_drill_witness as rw
import release_qualification as rq


ROOT = Path(__file__).resolve().parents[1]


def test_all_three_drills_run_and_report_exercised() -> None:
    evidence = rw.run_drills()
    assert [item.kind for item in evidence] == ["rollback", "migration", "restore"]
    assert all(item.exercised for item in evidence)


def test_the_restore_drill_compares_logical_content_and_says_so() -> None:
    restore = next(item for item in rw.run_drills() if item.kind == "restore")
    assert restore.comparison == "logical-content"
    assert restore.source_digest == restore.restored_digest
    assert len(restore.source_digest) == 64


def test_the_restore_drill_actually_mutates_and_therefore_proves_something() -> None:
    """A restore over an unchanged file would pass while proving nothing."""
    restore = next(item for item in rw.run_drills() if item.kind == "restore")
    assert "mutate" in restore.detail


def test_sqlite_restore_is_logically_but_not_byte_identical() -> None:
    """The fact that forces `comparison` to exist: measured, not assumed.

    If this ever becomes byte-identical, the repository changed its backup mechanism and the
    logical comparison should be revisited rather than silently kept.
    """
    import tempfile

    sys.path.insert(0, str(ROOT / "scripts"))
    from db_maintenance import backup_database, restore_database

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        live = root / "live.db"
        rw._seed(live)  # noqa: SLF001 - the fixture builder is deliberately shared with the witness
        rw._migrate(live, schema_version=rw.SCHEMA_VERSION)  # noqa: SLF001
        source_file = rw.file_digest(live)
        source_logical = rw.logical_digest(live)
        backup = root / "backup.db"
        backup_database(live, backup)
        restored, _ = restore_database(backup, live, force=True)

        assert rw.logical_digest(restored) == source_logical
        assert rw.file_digest(restored) != source_file, (
            "restored SQLite file is now byte-identical; revisit DrillRecord.comparison"
        )


def test_the_logical_digest_notices_a_schema_version_change() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        db = Path(raw) / "v.db"
        rw._seed(db)  # noqa: SLF001
        before = rw.logical_digest(db)
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        assert rw.logical_digest(db) != before


def test_the_logical_digest_notices_a_changed_value() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        db = Path(raw) / "v.db"
        rw._seed(db)  # noqa: SLF001
        before = rw.logical_digest(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE runs SET name='changed' WHERE id=1")
            conn.commit()
        assert rw.logical_digest(db) != before


def test_a_corrupt_database_has_no_logical_digest(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all")
    with pytest.raises(rw.DrillWitnessError, match="not a readable SQLite database"):
        rw.logical_digest(broken)


def test_a_missing_file_is_refused_rather_than_digested(tmp_path: Path) -> None:
    with pytest.raises(rw.DrillWitnessError, match="no file"):
        rw.file_digest(tmp_path / "absent.db")
    with pytest.raises(rw.DrillWitnessError, match="no database"):
        rw.logical_digest(tmp_path / "absent.db")


def test_the_witness_refuses_a_database_outside_its_scratch_directory(tmp_path: Path) -> None:
    """The drills never touch an operator's real database, and the guard is asserted, not assumed."""
    real = tmp_path / "studio_history.db"
    rw._seed(real)  # noqa: SLF001
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(rw.DrillWitnessError, match="outside the drill's scratch directory"):
        rw._check_scratch(real, scratch)  # noqa: SLF001


def test_a_bad_schema_version_is_refused() -> None:
    for bad in (0, -1, True):
        with pytest.raises(rw.DrillWitnessError, match="positive integer"):
            rw.run_drills(schema_version=bad)


def test_witness_evidence_becomes_a_qualification_drill_record() -> None:
    """The seam: measured evidence must satisfy the record's own refusals, unedited."""
    records = tuple(rq.DrillRecord(**item.to_mapping()) for item in rw.run_drills())
    assert {record.kind for record in records} == set(rq.DRILLS)
    assert all(record.exercised for record in records)
    assert next(record for record in records if record.kind == "restore").proves_restore is True


def test_the_cli_exits_zero_and_names_each_drill(capsys: pytest.CaptureFixture[str]) -> None:
    assert rw.main([]) == 0
    out = capsys.readouterr().out
    for kind in rq.DRILLS:
        assert kind in out
