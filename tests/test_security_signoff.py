from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.security_review_evidence import (
    CheckResult,
    collect_git_state,
    render_report,
    run_checks,
)
from scripts.validate_security_signoff import (
    ROADMAP,
    SIGNOFF,
    parse_record,
    validate_signoff,
)

ROOT = Path(__file__).resolve().parents[1]
PINNED_SHA = "b7d841a2a94842d62662e1c57206a531dd2247f7"

SIGNED_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("| _none recorded_ | _unsigned_ | _unsigned_ | _unsigned_ |", "| none | n/a | n/a | n/a |"),
    ("state: deferred", "state: signed"),
    ('deferral: "Maintainer decision 2026-10-29: independent human review and human verification run once as the final end-to-end pass after all remaining stories are complete; recorded as completed, not verified, until then."', 'deferral: ""'),
    ('reviewer: ""', 'reviewer: "Dana Reviewer (independent)"'),
    ('reviewer_independence: ""', 'reviewer_independence: "No authorship of app.py or the remediation commits; external engagement."'),
    ('commit: ""', f'commit: "{PINNED_SHA}"'),
    ('reviewed_on: ""', 'reviewed_on: "2026-11-02"'),
    ("decision: pending", "decision: accepted"),
    ("| unrated | not-verified |", "| low | verified |"),
    ("| not-examined | not-assessed | not-assessed | no |", "| examined | adequate | adequate | no |"),
    ("_unassigned_", "Dana Reviewer"),
    ("_unsigned_", "Independent of the authors."),
    ("_unset_", "2026-11-02"),
    ("_unset_ |", "2026-11-02 |"),
    ("_to be recorded by reviewer_", "Commands from BASELINE.md plus manual probes."),
    ("**Decision: pending.**", "**Decision: accepted.**"),
    ("| Decision | pending |", "| Decision | accepted |"),
)


def _copy_repository_package(destination: Path) -> None:
    (destination / "docs").mkdir(parents=True, exist_ok=True)
    (destination / "product").mkdir(parents=True, exist_ok=True)
    (destination / SIGNOFF).write_text((ROOT / SIGNOFF).read_text(encoding="utf-8"), encoding="utf-8")
    (destination / ROADMAP).write_text((ROOT / ROADMAP).read_text(encoding="utf-8"), encoding="utf-8")


def _write_signoff(root: Path, text: str) -> None:
    (root / SIGNOFF).write_text(text, encoding="utf-8")


def _signed_document() -> str:
    text = (ROOT / SIGNOFF).read_text(encoding="utf-8")
    for source, replacement in SIGNED_REPLACEMENTS:
        text = text.replace(source, replacement)
    return text


def _set_story_status(root: Path, status: str) -> None:
    document = json.loads((root / ROADMAP).read_text(encoding="utf-8"))
    for story in document["stories"]:
        if story["id"] == "AS-031":
            story["status"] = status
    (root / ROADMAP).write_text(json.dumps(document), encoding="utf-8")


def _rewrite_row(text: str, prefix: str, columns: dict[int, str]) -> str:
    """Rewrite specific cells of the first table row whose first cell starts with `prefix`."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"| {prefix}"):
            continue
        cells = line.split("|")
        for column, value in columns.items():
            cells[column] = f" {value} "
        lines[index] = "|".join(cells)
        return "\n".join(lines)
    raise AssertionError(f"table row starting with {prefix!r} not found")


def test_repository_signoff_record_is_consistent_and_story_never_verified() -> None:
    assert validate_signoff(ROOT) == []
    record, errors = parse_record((ROOT / SIGNOFF).read_text(encoding="utf-8"))
    assert errors == []
    assert record.state == "deferred"
    assert record.decision == "pending"
    assert record.deferral and "end-to-end" in record.deferral
    assert set(record.coverage) == {f"B{number}" for number in range(1, 13)}
    assert {"W-1", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6"} <= set(record.findings)

    roadmap = json.loads((ROOT / ROADMAP).read_text(encoding="utf-8"))
    story = next(story for story in roadmap["stories"] if story["id"] == "AS-031")
    assert story["status"] == "completed"


def test_missing_signoff_record_is_reported(tmp_path: Path) -> None:
    assert any("missing or unreadable sign-off record" in error for error in validate_signoff(tmp_path))


def _unsigned_document(label: str | None = None) -> str:
    """Return the record as it looked before any implementation (state: unsigned)."""
    text = (ROOT / SIGNOFF).read_text(encoding="utf-8")
    text = text.replace("state: deferred", "state: unsigned")
    lines = [
        'deferral: ""' if line.startswith("deferral: ") else line for line in text.splitlines()
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("status", ["completed", "verified"])
def test_unsigned_record_blocks_roadmap_closure(tmp_path: Path, status: str) -> None:
    _copy_repository_package(tmp_path)
    _write_signoff(tmp_path, _unsigned_document())
    _set_story_status(tmp_path, "in_progress")
    assert validate_signoff(tmp_path) == []

    _set_story_status(tmp_path, status)

    errors = validate_signoff(tmp_path)
    assert any(f"status '{status}' is inconsistent with a 'unsigned'" in error for error in errors)


def test_deferred_record_rejects_verified_status(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    assert validate_signoff(tmp_path) == []

    _set_story_status(tmp_path, "verified")

    errors = validate_signoff(tmp_path)
    assert any("status 'verified' is inconsistent with a 'deferred'" in error for error in errors)
    assert any("may only be 'verified' after a signed record" in error for error in errors)


@pytest.mark.parametrize("status", ["planned", "in_progress"])
def test_deferred_record_rejects_unfinished_status(tmp_path: Path, status: str) -> None:
    _copy_repository_package(tmp_path)
    _set_story_status(tmp_path, status)

    errors = validate_signoff(tmp_path)
    assert any(f"status '{status}' is inconsistent with a 'deferred'" in error for error in errors)


@pytest.mark.parametrize(
    "deferral",
    [
        'deferral: ""',
        'deferral: "later"',
        'deferral: "Maintainer decision: human verification happens later after everything."',
        'deferral: "Maintainer decision 2026-10-29: _unset_ end-to-end pass."',
    ],
    ids=["empty", "too-short", "unnamed-pass", "placeholder"],
)
def test_deferred_record_requires_a_complete_deferral_statement(tmp_path: Path, deferral: str) -> None:
    _copy_repository_package(tmp_path)
    text = (ROOT / SIGNOFF).read_text(encoding="utf-8")
    start = text.index("deferral: ")
    end = text.index("\n", start)
    _write_signoff(tmp_path, text[:start] + deferral + text[end:])

    errors = validate_signoff(tmp_path)
    assert any("deferral" in error or "end-to-end" in error for error in errors)


def test_deferred_record_rejects_reviewer_judgement(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _rewrite_row(
        (ROOT / SIGNOFF).read_text(encoding="utf-8"), "F-2 ", {5: "high", 6: "verified"}
    ).replace('reviewer: ""', 'reviewer: "Someone"')
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("deferred sign-off record must leave 'reviewer' empty" in error for error in errors)
    assert any("must not pre-assign reviewer severity: F-2" in error for error in errors)
    assert any("must not pre-assign fix verification: F-2" in error for error in errors)


def test_signed_record_requires_clearing_the_deferral(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _signed_document()
    assert 'deferral: ""' in text
    _write_signoff(tmp_path, text)

    assert validate_signoff(tmp_path) == []


def test_missing_deferral_key_is_reported(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = "\n".join(
        line for line in (ROOT / SIGNOFF).read_text(encoding="utf-8").splitlines() if not line.startswith("deferral: ")
    )
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("missing key: deferral" in error for error in errors)


@pytest.mark.parametrize("status", ["completed", "verified"])
def test_signed_accepted_record_allows_roadmap_closure(tmp_path: Path, status: str) -> None:
    _copy_repository_package(tmp_path)
    _write_signoff(tmp_path, _signed_document())
    _set_story_status(tmp_path, status)

    assert validate_signoff(tmp_path) == []


def test_signed_accepted_record_rejects_unverified_high_finding(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _rewrite_row(_signed_document(), "F-1 ", {5: "high", 6: "not-verified"})
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("inconsistent with unverified critical/high findings: F-1" in error for error in errors)
    assert any("without a recorded accepted residual risk" in error for error in errors)


def test_signed_release_blocked_record_permits_unverified_findings(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _signed_document().replace("decision: accepted", "decision: release-blocked")
    text = _rewrite_row(text, "F-1 ", {5: "critical", 6: "not-verified"})
    _write_signoff(tmp_path, text)

    assert validate_signoff(tmp_path) == []


def test_signed_record_requires_reviewer_commit_and_date(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = (
        _signed_document()
        .replace('reviewer: "Dana Reviewer (independent)"', 'reviewer: ""')
        .replace(f'commit: "{PINNED_SHA}"', 'commit: "b7d841a"')
        .replace('reviewed_on: "2026-11-02"', 'reviewed_on: "2026-13-45"')
    )
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("requires a reviewer identity" in error for error in errors)
    assert any("full 40-character SHA" in error for error in errors)
    assert any("not a real calendar date" in error for error in errors)


def test_signed_record_rejects_leftover_placeholders(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    _write_signoff(tmp_path, _signed_document() + "\n_unset_\n")

    errors = validate_signoff(tmp_path)
    assert any("still contains placeholder text: _unset_" in error for error in errors)


def test_unsigned_record_rejects_prefilled_reviewer_judgement(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _rewrite_row((ROOT / SIGNOFF).read_text(encoding="utf-8"), "F-1 ", {5: "high", 6: "verified"})
    text = _rewrite_row(text, "B1 ", {2: "examined", 3: "adequate", 4: "adequate"})
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("must not pre-assign reviewer severity: F-1" in error for error in errors)
    assert any("must not pre-assign fix verification: F-1" in error for error in errors)
    assert any("must not pre-mark boundaries as examined: B1" in error for error in errors)


def test_unsigned_record_rejects_identity_and_decision_values(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = (
        (ROOT / SIGNOFF)
        .read_text(encoding="utf-8")
        .replace('reviewer: ""', 'reviewer: "Someone"')
        .replace("decision: pending", "decision: accepted")
    )
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("must leave 'reviewer' empty" in error for error in errors)
    assert any("must keep decision 'pending'" in error for error in errors)


def test_missing_boundary_or_finding_rows_are_reported(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = (
        (ROOT / SIGNOFF)
        .read_text(encoding="utf-8")
        .replace("| B9 prompt-injection boundaries", "| B99 prompt-injection boundaries", 1)
        .replace("| F-4 |", "| F-44 |", 1)
    )
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("missing row: B9" in error for error in errors)
    assert any("missing row: F-4" in error for error in errors)
    assert any("unrecognized identifier: F-44" in error for error in errors)


def test_inadequate_control_forces_release_block(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _rewrite_row(_signed_document(), "B1 ", {3: "inadequate"})
    _write_signoff(tmp_path, text)

    errors = validate_signoff(tmp_path)
    assert any("inadequate control at B1" in error for error in errors)


def test_reviewer_found_findings_may_use_r_identifiers(tmp_path: Path) -> None:
    _copy_repository_package(tmp_path)
    text = _signed_document().replace(
        "| F-6 |",
        "| R-1 | Reviewer-discovered issue with a reproduction. | None yet. | n/a | low | verified |\n| F-6 |",
        1,
    )
    _write_signoff(tmp_path, text)

    assert validate_signoff(tmp_path) == []


def test_evidence_bundle_records_commit_environment_and_results() -> None:
    git_state = {
        "commit": PINNED_SHA,
        "commit_subject": "AS-031: subject",
        "commit_author_date": "2026-10-29T00:00:00+00:00",
        "branch": "detached",
        "shallow": "false",
        "working_tree": "clean",
    }
    environment = {"python": "3.11.9", "platform": "Linux-test", "fastapi": "0.162.0"}
    results = [
        CheckResult(name="Test suite", command="-m pytest -q", exit_code=0, summary="438 passed, 1 skipped"),
        CheckResult(name="Ruff lint", command="-m ruff check .", exit_code=1, summary="1 error"),
    ]

    report = render_report(git_state, environment, results, generated_at=datetime(2026, 11, 2, tzinfo=timezone.utc))

    assert PINNED_SHA in report
    assert "| Test suite | 0 | passed |" in report
    assert "| Ruff lint | 1 | FAILED |" in report
    assert "438 passed, 1 skipped" in report
    assert "not an independent security review" in report
    assert "2026-11-02 00:00:00Z" in report


def test_run_checks_uses_injected_runner_and_defaults_to_not_run() -> None:
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(command)
        return 1, "boom\nfailure detail\n"

    results = run_checks((("Sample check", ("scripts/nonexistent.py",)),), runner=fake_runner)

    assert calls and calls[0][-1].endswith("scripts/nonexistent.py")
    assert results[0].status == "FAILED"
    assert results[0].summary == "failure detail"


def test_collect_git_state_survives_missing_git(tmp_path: Path) -> None:
    state = collect_git_state(tmp_path, runner=lambda command, cwd: (128, ""))
    assert "unavailable" in state["commit"]
    assert "unknown" in state["working_tree"]
