from __future__ import annotations

import shutil
from pathlib import Path

from scripts.validate_adrs import validate_adrs

ROOT = Path(__file__).resolve().parents[1]
ADR_SOURCE = ROOT / "docs/adr"


def _copy_adr_tree(destination: Path) -> Path:
    target = destination / "docs/adr"
    shutil.copytree(ADR_SOURCE, target)
    return target


def test_repository_adrs_have_required_context_decision_and_review_metadata() -> None:
    assert validate_adrs(ROOT) == []


def test_validator_rejects_adr_missing_alternatives(tmp_path: Path) -> None:
    adr_dir = _copy_adr_tree(tmp_path)
    record = adr_dir / "0001-lightweight-markdown-adr-process.md"
    text = record.read_text(encoding="utf-8").replace("## Alternatives\n\n", "")
    record.write_text(text, encoding="utf-8")

    errors = validate_adrs(tmp_path)
    assert any("missing or empty required section: Alternatives" in error for error in errors)


def test_validator_rejects_unknown_decision_status(tmp_path: Path) -> None:
    adr_dir = _copy_adr_tree(tmp_path)
    record = adr_dir / "0001-lightweight-markdown-adr-process.md"
    text = record.read_text(encoding="utf-8").replace("status: accepted", "status: decided")
    record.write_text(text, encoding="utf-8")

    errors = validate_adrs(tmp_path)
    assert any("status must be one of" in error for error in errors)


def test_validator_accepts_reviewed_record_with_reviewer_and_date(tmp_path: Path) -> None:
    adr_dir = _copy_adr_tree(tmp_path)
    record = adr_dir / "0001-lightweight-markdown-adr-process.md"
    text = record.read_text(encoding="utf-8")
    text = text.replace("review_status: not-reviewed", "review_status: reviewed")
    text = text.replace("reviewed_by: none", "reviewed_by: maintainers")
    text = text.replace("reviewed_on: none", "reviewed_on: 2026-10-26")
    text = text.replace(
        "Not reviewed. AS-005 introduces this process at the user's request; no separate maintainer review of this ADR is claimed. A maintainer may record a later review by updating the front matter and this section together.",
        "Reviewed by maintainers on 2026-10-26. The process and this record were checked.",
    )
    record.write_text(text, encoding="utf-8")

    assert validate_adrs(tmp_path) == []


def test_validator_rejects_reviewed_record_without_review_date(tmp_path: Path) -> None:
    adr_dir = _copy_adr_tree(tmp_path)
    record = adr_dir / "0001-lightweight-markdown-adr-process.md"
    text = record.read_text(encoding="utf-8")
    text = text.replace("review_status: not-reviewed", "review_status: reviewed")
    text = text.replace("reviewed_by: none", "reviewed_by: maintainers")
    text = text.replace("reviewed_on: none", "reviewed_on: invalid-date")
    record.write_text(text, encoding="utf-8")

    errors = validate_adrs(tmp_path)
    assert any("reviewed_on in ISO YYYY-MM-DD" in error for error in errors)


def test_validator_reports_missing_adr_process_files(tmp_path: Path) -> None:
    errors = validate_adrs(tmp_path)
    assert any("missing ADR process file" in error for error in errors)
    assert any("no numbered ADR records" in error for error in errors)
