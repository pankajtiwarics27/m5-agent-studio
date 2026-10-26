#!/usr/bin/env python3
"""Validate the source-linked threat model and abuse-case inventory."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THREAT_MODEL = Path("docs/threat-model.md")
REQUIRED_SECTIONS = (
    "Scope and security objectives",
    "Threat actors",
    "Trust boundaries and assets",
    "Prioritized abuse-case inventory",
    "Prioritized mitigation plan",
    "Review status and evidence limits",
)
ACTORS = {f"TA-{number}" for number in range(1, 7)}
ABUSE_CASES = {f"AC-{number:03d}" for number in range(1, 13)}
PRIORITIES = {"P0", "P1", "P2"}
SOURCE_LINK = re.compile(r"app\.py#L(\d+)")


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def validate_threat_model(root: Path = ROOT) -> list[str]:
    """Return defects in the threat model without accessing external services."""
    path = root / THREAT_MODEL
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"missing or unreadable threat model: {THREAT_MODEL}"]

    errors: list[str] = []
    for heading in REQUIRED_SECTIONS:
        body = _section(text, heading)
        if not body:
            errors.append(f"missing or empty required section: {heading}")
        elif re.search(r"\bTODO\b|<[^>]+>|\bplaceholder\b", body, flags=re.IGNORECASE):
            errors.append(f"required section contains placeholder text: {heading}")

    actor_ids = set(re.findall(r"\bTA-\d+\b", _section(text, "Threat actors")))
    missing_actors = sorted(ACTORS - actor_ids)
    if missing_actors:
        errors.append(f"missing threat actors: {', '.join(missing_actors)}")

    abuse_body = _section(text, "Prioritized abuse-case inventory")
    abuse_ids = set(re.findall(r"\bAC-\d{3}\b", abuse_body))
    missing_cases = sorted(ABUSE_CASES - abuse_ids)
    if missing_cases:
        errors.append(f"missing abuse cases: {', '.join(missing_cases)}")
    if not all(priority in abuse_body for priority in PRIORITIES):
        errors.append("abuse-case inventory must include P0, P1, and P2 priorities")
    for case_id in sorted(ABUSE_CASES & abuse_ids):
        row = next((line for line in abuse_body.splitlines() if case_id in line), "")
        if row.count("|") < 5:
            errors.append(f"abuse case {case_id} must include priority, mitigations, and residual risk columns")

    source = (root / "app.py").read_text(encoding="utf-8") if (root / "app.py").is_file() else ""
    source_line_count = len(source.splitlines())
    source_lines = [int(value) for value in SOURCE_LINK.findall(text)]
    if not source_lines:
        errors.append("threat model must include source-linked app.py references")
    elif any(line < 1 or line > source_line_count for line in source_lines):
        errors.append("threat model contains an app.py source line outside the current file")

    for required in (
        "tests/test_security_and_quality.py",
        "tests/test_redirect_safety.py",
        "subsystem-dependency-inventory.md",
    ):
        if required not in text:
            errors.append(f"threat model missing evidence reference: {required}")

    return errors


def main() -> int:
    errors = validate_threat_model()
    if errors:
        print("Threat-model validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Threat-model validation passed (actors, boundaries, abuse cases, and evidence).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
