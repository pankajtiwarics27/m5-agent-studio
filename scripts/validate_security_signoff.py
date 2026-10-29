#!/usr/bin/env python3
"""Validate the AS-031 independent-review sign-off record and its roadmap gate.

The record (`docs/security-review-signoff.md`) is the only place an independent
reviewer may close AS-031. This validator keeps the record, the roadmap JSON, and
reality aligned in both directions:

* an unsigned record cannot coexist with a `completed`/`verified` AS-031;
* a signed `accepted` record cannot coexist with an `in_progress`/`planned`
  AS-031 left dangling without its decision recorded;
* a signed record cannot leave reviewer fields or severities blank, and cannot
  claim acceptance while a critical/high finding is unverified.

It performs no network access and never mutates the repository.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNOFF = Path("docs/security-review-signoff.md")
ROADMAP = Path("product/Adaptive_Runtime_Product_Roadmap_v2.json")
STORY_ID = "AS-031"

REQUIRED_SECTIONS = (
    "Reviewer identity and independence",
    "Review scope and pinned commit",
    "Boundary review coverage (B1–B12)",
    "Finding register and severity",
    "Residual risks accepted at sign-off",
    "Decision and signature",
    "Completing this record (reviewer instructions)",
)
BOUNDARIES = tuple(f"B{number}" for number in range(1, 13))
FINDINGS = ("W-1", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6")
STATES = {"unsigned", "signed"}
DECISIONS = {"pending", "accepted", "release-blocked"}
SEVERITIES = {"unrated", "none", "low", "medium", "high", "critical"}
BLOCKING_SEVERITIES = {"critical", "high"}
EXAMINED = {"examined", "not-examined"}
ADEQUACY = {"adequate", "inadequate", "not-assessed"}
VERIFICATION = {"verified", "not-verified"}
CLOSED_STATUSES = {"completed", "verified"}
# Residual-risk rows that record "nothing accepted" rather than a real entry.
EMPTY_RESIDUAL = {"_none-recorded_", "none", "n/a", "na", "-", "—"}
RECORD_KEYS = ("state", "reviewer", "reviewer_independence", "commit", "reviewed_on", "decision")
PLACEHOLDERS = (
    "_unset_",
    "_unsigned_",
    "_unassigned_",
    "_to be recorded",
    "_none recorded_",
)
SHA = re.compile(r"^[0-9a-f]{40}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECORD_BLOCK = re.compile(r"<!--\s*as031-signoff\s*\n(.*?)-->", flags=re.DOTALL)


@dataclass
class SignoffRecord:
    """Parsed record: the machine-readable block plus the reviewer tables."""

    state: str = ""
    reviewer: str = ""
    reviewer_independence: str = ""
    commit: str = ""
    reviewed_on: str = ""
    decision: str = ""
    coverage: dict[str, dict[str, str]] = field(default_factory=dict)
    findings: dict[str, dict[str, str]] = field(default_factory=dict)
    residual_risks: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## .*{re.escape(heading)}\s*$",
        text,
        flags=re.MULTILINE,
    )
    if not match:
        return ""
    start = match.end()
    following = re.search(r"^## ", text[start:], flags=re.MULTILINE)
    end = start + following.start() if following else len(text)
    return text[start:end].strip()


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or set(cells[0]) <= {"-", ":", " "} and cells[0]:
            continue
        rows.append(cells)
    return rows


ID_PREFIX = re.compile(r"^((?:B|W|F|R)-?\d+)")


def _identifier(cell: str) -> str | None:
    """Return the leading row identifier (e.g. `B8`, `F-2`) or None for headers."""
    match = ID_PREFIX.match(cell.strip().strip("`*_").strip())
    return match.group(1) if match else None


def _token(value: str) -> str:
    """Normalize a table cell to a lowercase token; keep unknown text visible."""
    cleaned = value.strip().strip("`*_").strip().lower()
    return re.sub(r"\s+", "-", cleaned)


def parse_record(text: str) -> tuple[SignoffRecord, list[str]]:
    """Parse the sign-off document into a record plus structural defects."""
    errors: list[str] = []
    record = SignoffRecord(text=text)

    block = RECORD_BLOCK.search(text)
    if not block:
        errors.append(f"missing machine-readable `as031-signoff` record block in {SIGNOFF}")
        return record, errors

    values: dict[str, str] = {}
    for line in block.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    for key in RECORD_KEYS:
        if key not in values:
            errors.append(f"sign-off record block is missing key: {key}")
    record.state = values.get("state", "").lower()
    record.reviewer = values.get("reviewer", "")
    record.reviewer_independence = values.get("reviewer_independence", "")
    record.commit = values.get("commit", "").lower()
    record.reviewed_on = values.get("reviewed_on", "")
    record.decision = values.get("decision", "").lower()

    for heading in REQUIRED_SECTIONS:
        if not _section(text, heading):
            errors.append(f"missing or empty required sign-off section: {heading}")

    for cells in _table_rows(_section(text, "Boundary review coverage (B1–B12)")):
        identifier = _identifier(cells[0]) if cells else None
        if len(cells) < 5 or not identifier:
            continue
        record.coverage[identifier] = {
            "examined": _token(cells[1]),
            "adequate": _token(cells[2]),
            "tests": _token(cells[3]),
            "residual": _token(cells[4]),
        }

    for cells in _table_rows(_section(text, "Finding register and severity")):
        identifier = _identifier(cells[0]) if cells else None
        if len(cells) < 6 or not identifier:
            continue
        record.findings[identifier] = {
            "severity": _token(cells[4]),
            "verified": _token(cells[5]),
        }

    for cells in _table_rows(_section(text, "Residual risks accepted at sign-off")):
        if len(cells) < 4 or cells[0].lower().startswith("accepted residual risk"):
            continue
        record.residual_risks.append((_token(cells[0]), cells[1]))
    return record, errors


def _accepted_risks(record: SignoffRecord) -> list[tuple[str, str]]:
    """Return residual-risk rows that record an actual accepted risk."""
    return [row for row in record.residual_risks if row[0] not in EMPTY_RESIDUAL]


def _roadmap_status(root: Path) -> tuple[str | None, list[str]]:
    path = root / ROADMAP
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"unreadable roadmap JSON {ROADMAP}: {exc}"]

    stories = document.get("stories")
    if not isinstance(stories, list):
        return None, [f"roadmap JSON has no stories list: {ROADMAP}"]
    for story in stories:
        if isinstance(story, dict) and story.get("id") == STORY_ID:
            return str(story.get("status", "")), []
    return None, [f"roadmap JSON has no {STORY_ID} story"]


def validate_signoff(root: Path = ROOT) -> list[str]:
    """Return sign-off/gate defects for `root`; never touches the network."""
    try:
        text = (root / SIGNOFF).read_text(encoding="utf-8")
    except OSError:
        return [f"missing or unreadable sign-off record: {SIGNOFF}"]

    record, errors = parse_record(text)

    if record.state not in STATES:
        errors.append(f"sign-off record state must be one of {sorted(STATES)}")
    if record.decision not in DECISIONS:
        errors.append(f"sign-off record decision must be one of {sorted(DECISIONS)}")

    for boundary in BOUNDARIES:
        if boundary not in record.coverage:
            errors.append(f"boundary review coverage table is missing row: {boundary}")
    for finding in FINDINGS:
        if finding not in record.findings:
            errors.append(f"finding register is missing row: {finding}")
    for finding in record.findings:
        if finding not in FINDINGS and not finding.startswith("R-"):
            errors.append(f"finding register has unrecognized identifier: {finding}")
    for boundary, cells in record.coverage.items():
        if cells["examined"] not in EXAMINED:
            errors.append(f"{boundary}: reviewer examined value must be one of {sorted(EXAMINED)}")
        for column in ("adequate", "tests"):
            if cells[column] not in ADEQUACY:
                errors.append(f"{boundary}: {column} value must be one of {sorted(ADEQUACY)}")
        if cells["residual"] not in {"yes", "no"}:
            errors.append(f"{boundary}: residual risk column must be yes or no")
    for finding, cells in record.findings.items():
        if cells["severity"] not in SEVERITIES:
            errors.append(f"{finding}: reviewer severity must be one of {sorted(SEVERITIES)}")
        if cells["verified"] not in VERIFICATION:
            errors.append(f"{finding}: fix verification must be one of {sorted(VERIFICATION)}")

    status, roadmap_errors = _roadmap_status(root)
    errors.extend(roadmap_errors)

    if status in CLOSED_STATUSES and not (record.state == "signed" and record.decision == "accepted"):
        errors.append(
            f"{STORY_ID} is '{status}' but the independent sign-off record is "
            f"'{record.state or 'unparseable'}'/'{record.decision or 'unparseable'}'; "
            "a story can only be completed/verified after a signed accepted review"
        )

    if record.state == "unsigned":
        for key in ("reviewer", "reviewer_independence", "commit", "reviewed_on"):
            if getattr(record, key):
                errors.append(f"unsigned sign-off record must leave '{key}' empty")
        if record.decision != "pending":
            errors.append("unsigned sign-off record must keep decision 'pending'")
        unrated = [fid for fid, cells in record.findings.items() if cells["severity"] != "unrated"]
        if unrated:
            errors.append(
                "unsigned sign-off record must not pre-assign reviewer severity: "
                + ", ".join(sorted(unrated))
            )
        verified = [fid for fid, cells in record.findings.items() if cells["verified"] != "not-verified"]
        if verified:
            errors.append(
                "unsigned sign-off record must not pre-assign fix verification: "
                + ", ".join(sorted(verified))
            )
        examined = [bid for bid, cells in record.coverage.items() if cells["examined"] != "not-examined"]
        if examined:
            errors.append(
                "unsigned sign-off record must not pre-mark boundaries as examined: "
                + ", ".join(sorted(examined))
            )
        if status in CLOSED_STATUSES:
            errors.append(f"{STORY_ID} must stay out of {sorted(CLOSED_STATUSES)} while the record is unsigned")
        return errors

    # Signed record: the fields and judgements must now be complete and coherent.
    if not record.reviewer:
        errors.append("signed sign-off record requires a reviewer identity")
    if not record.reviewer_independence:
        errors.append("signed sign-off record requires an independence statement")
    if not SHA.fullmatch(record.commit):
        errors.append("signed sign-off record requires the reviewed commit as a full 40-character SHA")
    if not ISO_DATE.fullmatch(record.reviewed_on):
        errors.append("signed sign-off record requires reviewed_on as YYYY-MM-DD")
    else:
        try:
            if date.fromisoformat(record.reviewed_on) < date(2026, 1, 1):
                errors.append("sign-off review date is implausibly early for this repository")
        except ValueError:
            errors.append("signed sign-off record reviewed_on is not a real calendar date")
    if record.decision == "pending":
        errors.append("signed sign-off record must record a decision of accepted or release-blocked")

    for placeholder in PLACEHOLDERS:
        if record.state == "signed" and placeholder in text:
            errors.append(f"signed sign-off record still contains placeholder text: {placeholder}")

    unrated = [fid for fid, cells in record.findings.items() if cells["severity"] == "unrated"]
    if unrated:
        errors.append("signed sign-off record must rate every finding: " + ", ".join(sorted(unrated)))
    unverified = [
        fid
        for fid, cells in record.findings.items()
        if cells["verified"] != "verified"
    ]
    blocking = [
        fid
        for fid, cells in record.findings.items()
        if cells["severity"] in BLOCKING_SEVERITIES and cells["verified"] != "verified"
    ]
    if record.decision == "accepted" and blocking:
        errors.append(
            "decision 'accepted' is inconsistent with unverified critical/high findings: "
            + ", ".join(sorted(blocking))
        )

    for boundary, cells in record.coverage.items():
        if cells["examined"] != "examined":
            errors.append(f"signed sign-off record must record {boundary} as examined")
        if cells["adequate"] == "inadequate" and record.decision == "accepted":
            errors.append(f"decision 'accepted' is inconsistent with an inadequate control at {boundary}")
        if cells["residual"] == "yes" and not _accepted_risks(record):
            errors.append(f"{boundary} notes residual risk but no accepted residual risk is recorded")

    if unverified and record.decision == "accepted":
        if not _accepted_risks(record):
            errors.append(
                "decision 'accepted' leaves unverified findings without a recorded accepted residual risk"
            )
    return errors


def main() -> int:
    errors = validate_signoff()
    if errors:
        print("Security sign-off validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    record, _ = parse_record((ROOT / SIGNOFF).read_text(encoding="utf-8"))
    print(
        "Security sign-off record is consistent: "
        f"state={record.state}, decision={record.decision}, "
        f"{len(record.coverage)} boundaries, {len(record.findings)} findings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
