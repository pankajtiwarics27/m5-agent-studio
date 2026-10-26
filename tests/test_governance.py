from __future__ import annotations

import shutil
from pathlib import Path

from scripts.validate_governance import REQUIRED_FILES, validate_governance

ROOT = Path(__file__).resolve().parents[1]


def _copy_governance_files(destination: Path) -> None:
    for relative in REQUIRED_FILES:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def test_repository_governance_package_is_complete_and_safe() -> None:
    assert validate_governance(ROOT) == []


def test_validator_reports_missing_governance_files(tmp_path: Path) -> None:
    errors = validate_governance(tmp_path)
    assert any("LICENSE" in error for error in errors)
    assert any("CONTRIBUTING.md" in error for error in errors)
    assert any("CODE_OF_CONDUCT.md" in error for error in errors)


def test_validator_rejects_security_policy_without_private_disclosure(tmp_path: Path) -> None:
    _copy_governance_files(tmp_path)
    security = tmp_path / "SECURITY.md"
    security.write_text(
        security.read_text(encoding="utf-8").replace("privately", "publicly"),
        encoding="utf-8",
    )

    errors = validate_governance(tmp_path)
    assert any("SECURITY.md is missing disclosure guidance: privately" in error for error in errors)


def test_validator_rejects_issue_template_without_security_policy_reference(tmp_path: Path) -> None:
    _copy_governance_files(tmp_path)
    bug_report = tmp_path / ".github/ISSUE_TEMPLATE/bug_report.md"
    text = bug_report.read_text(encoding="utf-8").replace("SECURITY.md", "security guidance")
    bug_report.write_text(text, encoding="utf-8")

    errors = validate_governance(tmp_path)
    assert any("must direct security vulnerabilities to SECURITY.md" in error for error in errors)
