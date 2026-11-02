#!/usr/bin/env python3
"""Validate the P5-05 before/after screenshot and focused-state evidence.

This story stores desktop, narrow, light/dark, and keyboard-focused captures in
`docs/media/uiux-p5-05`. The repository's shared screenshot validator correctly
uses the desktop onboarding manifest; this story-specific check also verifies
its 390x844 variants and custom interaction-walk indexes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "media" / "uiux-p5-05"
MANIFEST = ROOT / "docs" / "media" / "screenshots" / "manifest.json"
SETS = (
    "before-dark",
    "before-light",
    "before-narrow",
    "after-dark",
    "after-light",
    "after-narrow",
    "before-focus-dark",
    "before-focus-light",
    "before-focus-narrow",
    "after-focus-dark",
    "after-focus-light",
    "after-focus-narrow",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_sets(root: Path = EVIDENCE, manifest_path: Path = MANIFEST) -> tuple[list[str], int]:
    """Return artifact problems and the number of verified PNGs."""
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read screenshot manifest: {exc}"], 0
    expected_tabs = [(shot["id"], shot["tab"]) for shot in manifest.get("shots", [])]
    if not expected_tabs or len(expected_tabs) != 6:
        return ["the shared screenshot manifest must declare six pane shots"], 0

    verified = 0
    for name in SETS:
        directory = root / name
        index_path = directory / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{name}: cannot read index.json: {exc}")
            continue
        if index.get("schema") != 1:
            problems.append(f"{name}: index schema is not 1")
            continue
        viewport = index.get("viewport") or {}
        try:
            width = int(viewport["width"] * viewport["device_scale_factor"])
            height = int(viewport["height"] * viewport["device_scale_factor"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{name}: index has no valid viewport")
            continue
        entries = index.get("shots")
        if not isinstance(entries, list) or len(entries) != len(expected_tabs):
            problems.append(f"{name}: expected six screenshot index entries")
            continue
        actual_tabs = [(entry.get("id"), entry.get("tab")) for entry in entries if isinstance(entry, dict)]
        if actual_tabs != expected_tabs:
            problems.append(f"{name}: pane IDs/order differ from the shared manifest")
        expected_files = {entry.get("file") for entry in entries if isinstance(entry, dict)}
        actual_files = {path.name for path in directory.glob("*.png")}
        if actual_files != expected_files:
            problems.append(f"{name}: PNG set differs from its index (extra={sorted(actual_files-expected_files)}, missing={sorted(expected_files-actual_files)})")

        is_before = name.startswith("before-focus-")
        is_after = name.startswith("after-focus-")
        if is_before or is_after:
            checks = index.get("checks") or {}
            expected_mismatches = 6 if is_before else 0
            if checks.get("panes") != 6 or checks.get("mismatches") != expected_mismatches:
                problems.append(f"{name}: expected six focus-state checks with {expected_mismatches} mismatches")

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("file")
            if not isinstance(filename, str) or Path(filename).name != filename:
                problems.append(f"{name}: unsafe or missing screenshot filename {filename!r}")
                continue
            path = directory / filename
            try:
                data = path.read_bytes()
            except OSError as exc:
                problems.append(f"{name}/{filename}: {exc}")
                continue
            if not data.startswith(PNG_SIGNATURE) or len(data) < 24 or data[12:16] != b"IHDR":
                problems.append(f"{name}/{filename}: invalid PNG")
                continue
            actual_size = struct.unpack(">II", data[16:24])
            if actual_size != (width, height):
                problems.append(f"{name}/{filename}: {actual_size[0]}x{actual_size[1]}, expected {width}x{height}")
                continue
            if entry.get("bytes") != len(data) or entry.get("sha256") != hashlib.sha256(data).hexdigest():
                problems.append(f"{name}/{filename}: size or SHA-256 differs from index.json")
                continue
            verified += 1
    return problems, verified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if any screenshot or index is inconsistent")
    args = parser.parse_args()
    if not args.check:
        parser.error("--check is required")
    problems, verified = validate_sets()
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    print(f"P5-05 screenshots verified: {verified} PNGs in {len(SETS)} before/after sets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
