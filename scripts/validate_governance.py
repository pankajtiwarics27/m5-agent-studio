#!/usr/bin/env python3
"""Validate the repository's contributor and governance policy files."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "LICENSE",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/ISSUE_TEMPLATE/config.yml",
)


def validate_governance(root: Path = ROOT) -> list[str]:
    """Return policy-package defects; never access the network or project services."""
    errors: list[str] = []
    documents: dict[str, str] = {}
    for relative in REQUIRED_FILES:
        path = root / relative
        try:
            documents[relative] = path.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"missing or unreadable required file: {relative}")

    license_text = documents.get("LICENSE", "")
    for marker in (
        "SPDX-License-Identifier: MIT",
        "MIT License",
        "Copyright (c) 2026 Pankaj Tiwari",
        "THE SOFTWARE IS PROVIDED \"AS IS\"",
    ):
        if marker not in license_text:
            errors.append(f"LICENSE is missing required MIT notice text: {marker}")

    contributing = documents.get("CONTRIBUTING.md", "")
    for marker in (
        "CODE_OF_CONDUCT.md",
        "Python 3.11",
        "pytest -q",
        "compileall -q app.py workflow_orchestration.py scripts tests",
        "scripts/smoke_local.py",
        "hardware",
        "provider",
        "credentials",
    ):
        if marker.casefold() not in contributing.casefold():
            errors.append(f"CONTRIBUTING.md is missing required guidance: {marker}")

    security = documents.get("SECURITY.md", "").casefold()
    for marker in ("reporting a vulnerability", "privately", "public issue", "maintainer"):
        if marker not in security:
            errors.append(f"SECURITY.md is missing disclosure guidance: {marker}")

    conduct = documents.get("CODE_OF_CONDUCT.md", "").casefold()
    for marker in ("report", "privately", "maintainer", "enforcement"):
        if marker not in conduct:
            errors.append(f"CODE_OF_CONDUCT.md is missing conduct-reporting guidance: {marker}")

    for relative in (".github/ISSUE_TEMPLATE/bug_report.md", ".github/ISSUE_TEMPLATE/feature_request.md"):
        text = documents.get(relative, "")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            errors.append(f"issue template needs YAML front matter: {relative}")
        else:
            front_matter = text.split("\n---\n", 1)[0][4:]
            for field in ("name:", "about:", "title:", "labels:", "assignees:"):
                if not any(line.startswith(field) for line in front_matter.splitlines()):
                    errors.append(f"issue template front matter is missing {field}: {relative}")
        if "do not include" not in text.casefold() or "credential" not in text.casefold():
            errors.append(f"issue template must warn against posting credentials: {relative}")

    bug_template = documents.get(".github/ISSUE_TEMPLATE/bug_report.md", "").casefold()
    if "security.md" not in bug_template or "security vulnerability" not in bug_template:
        errors.append("bug report template must direct security vulnerabilities to SECURITY.md")

    config = documents.get(".github/ISSUE_TEMPLATE/config.yml", "")
    if "blank_issues_enabled: false" not in config:
        errors.append("issue template config must disable unstructured blank issues")

    return errors


def main() -> int:
    errors = validate_governance()
    if errors:
        print("Governance validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Governance validation passed ({len(REQUIRED_FILES)} required files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
