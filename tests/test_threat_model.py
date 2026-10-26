from __future__ import annotations

from pathlib import Path

from scripts.validate_threat_model import THREAT_MODEL, validate_threat_model

ROOT = Path(__file__).resolve().parents[1]


def test_threat_model_has_actors_boundaries_priorities_and_source_evidence() -> None:
    assert validate_threat_model(ROOT) == []


def test_validator_rejects_missing_abuse_case_and_recovers_after_fix(tmp_path: Path) -> None:
    target = tmp_path / THREAT_MODEL
    target.parent.mkdir(parents=True)
    (tmp_path / "app.py").write_text((ROOT / "app.py").read_text(encoding="utf-8"), encoding="utf-8")
    source = (ROOT / THREAT_MODEL).read_text(encoding="utf-8")
    target.write_text(source.replace("| AC-012 |", "| AC-999 |", 1), encoding="utf-8")
    errors = validate_threat_model(tmp_path)
    assert any("missing abuse cases" in error and "AC-012" in error for error in errors)

    target.write_text(source, encoding="utf-8")
    assert validate_threat_model(tmp_path) == []


def test_validator_rejects_missing_trust_boundary_section(tmp_path: Path) -> None:
    target = tmp_path / THREAT_MODEL
    target.parent.mkdir(parents=True)
    (tmp_path / "app.py").write_text((ROOT / "app.py").read_text(encoding="utf-8"), encoding="utf-8")
    source = (ROOT / THREAT_MODEL).read_text(encoding="utf-8")
    target.write_text(source.replace("## Trust boundaries and assets", "## Removed boundary", 1), encoding="utf-8")
    errors = validate_threat_model(tmp_path)
    assert "missing or empty required section: Trust boundaries and assets" in errors
