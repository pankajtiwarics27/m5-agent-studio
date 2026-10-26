#!/usr/bin/env python3
"""Validate ADR metadata and required sections without external dependencies."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = Path("docs/adr")
REQUIRED_SECTIONS = ("Context", "Decision", "Alternatives", "Tradeoffs", "Review Status")
REQUIRED_FIELDS = (
    "id",
    "title",
    "status",
    "date",
    "review_status",
    "reviewed_by",
    "reviewed_on",
)
DECISION_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
REVIEW_STATUSES = {"not-reviewed", "reviewed", "re-review-required"}
RECORD_PATTERN = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def _parse_front_matter(text: str) -> tuple[dict[str, str], str, list[str]]:
    errors: list[str] = []
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}, "", ["missing YAML-style front matter"]

    raw_metadata, body = text[4:].split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line_number, line in enumerate(raw_metadata.splitlines(), start=2):
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            errors.append(f"malformed front matter line {line_number}")
            continue
        key = key.strip()
        if key in metadata:
            errors.append(f"duplicate front matter field: {key}")
        metadata[key] = value.strip()
    return metadata, body, errors


def _section_text(body: str, section: str) -> str:
    match = re.search(
        rf"^## {re.escape(section)}\s*$\n(.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _valid_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_record(path: Path, text: str, number: str) -> list[str]:
    errors: list[str] = []
    metadata, body, parse_errors = _parse_front_matter(text)
    errors.extend(f"{path}: {error}" for error in parse_errors)
    if not metadata:
        return errors

    for field in REQUIRED_FIELDS:
        if not metadata.get(field):
            errors.append(f"{path}: missing front matter field: {field}")

    expected_id = f"ADR-{number}"
    if metadata.get("id") != expected_id:
        errors.append(f"{path}: id must match filename ({expected_id})")
    if metadata.get("status", "").casefold() not in DECISION_STATUSES:
        errors.append(f"{path}: status must be one of {', '.join(sorted(DECISION_STATUSES))}")
    review_status = metadata.get("review_status", "").casefold()
    if review_status not in REVIEW_STATUSES:
        errors.append(f"{path}: review_status must be one of {', '.join(sorted(REVIEW_STATUSES))}")
    if metadata.get("date") and not _valid_iso_date(metadata["date"]):
        errors.append(f"{path}: date must use ISO YYYY-MM-DD")

    if review_status == "not-reviewed":
        if metadata.get("reviewed_by", "").casefold() != "none" or metadata.get("reviewed_on", "").casefold() != "none":
            errors.append(f"{path}: not-reviewed ADRs must set reviewed_by and reviewed_on to none")
    elif review_status in {"reviewed", "re-review-required"}:
        if not metadata.get("reviewed_by") or metadata.get("reviewed_by", "").casefold() == "none":
            errors.append(f"{path}: reviewed status requires reviewed_by")
        if not metadata.get("reviewed_on") or not _valid_iso_date(metadata.get("reviewed_on", "")):
            errors.append(f"{path}: reviewed status requires reviewed_on in ISO YYYY-MM-DD")

    heading = re.search(rf"^# {re.escape(expected_id)}: .+\S\s*$", body, flags=re.MULTILINE)
    if not heading:
        errors.append(f"{path}: missing title heading for {expected_id}")

    for section in REQUIRED_SECTIONS:
        content = _section_text(body, section)
        if not content:
            errors.append(f"{path}: missing or empty required section: {section}")
        elif re.search(r"\bTODO\b|<[^>]+>|\bplaceholder\b", content, flags=re.IGNORECASE):
            errors.append(f"{path}: required section contains placeholder text: {section}")

    review_body = _section_text(body, "Review Status").casefold()
    if review_status == "not-reviewed" and "not reviewed" not in review_body:
        errors.append(f"{path}: Review Status section must state that it is not reviewed")
    if review_status == "reviewed" and ("reviewed" not in review_body or "not reviewed" in review_body):
        errors.append(f"{path}: Review Status section must record the review")
    if review_status == "re-review-required" and not any(
        phrase in review_body for phrase in ("re-review required", "re-review is required", "requires re-review")
    ):
        errors.append(f"{path}: Review Status section must explain the re-review requirement")

    return errors


def validate_adrs(root: Path = ROOT) -> list[str]:
    """Return ADR process defects beneath a repository root."""
    errors: list[str] = []
    adr_dir = root / ADR_DIR
    for required in ("README.md", "0000-template.md"):
        if not (adr_dir / required).is_file():
            errors.append(f"missing ADR process file: {ADR_DIR / required}")

    template_path = adr_dir / "0000-template.md"
    if template_path.is_file():
        template = template_path.read_text(encoding="utf-8")
        metadata, body, parse_errors = _parse_front_matter(template)
        errors.extend(f"{template_path}: {error}" for error in parse_errors)
        for field in REQUIRED_FIELDS:
            if field not in metadata:
                errors.append(f"{template_path}: missing template field: {field}")
        for section in REQUIRED_SECTIONS:
            if not re.search(rf"^## {re.escape(section)}\s*$", body, flags=re.MULTILINE):
                errors.append(f"{template_path}: missing required section: {section}")

    records: list[tuple[Path, str, str]] = []
    if adr_dir.is_dir():
        for path in sorted(adr_dir.glob("*.md")):
            match = RECORD_PATTERN.fullmatch(path.name)
            if match and match.group(1) != "0000":
                records.append((path, path.read_text(encoding="utf-8"), match.group(1)))
    if not records:
        errors.append(f"no numbered ADR records found in {ADR_DIR}")

    seen_ids: set[str] = set()
    for path, text, number in records:
        record_id = f"ADR-{number}"
        if record_id in seen_ids:
            errors.append(f"duplicate ADR identifier: {record_id}")
        seen_ids.add(record_id)
        errors.extend(_validate_record(path.relative_to(root), text, number))

    return errors


def main() -> int:
    errors = validate_adrs()
    if errors:
        print("ADR validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("ADR validation passed (process, template, and numbered records).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
