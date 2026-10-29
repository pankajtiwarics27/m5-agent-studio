#!/usr/bin/env python3
"""Validate the onboarding screenshot manifest and any captured images (AS-045).

    python3 scripts/validate_screenshots.py           # consistency check
    python3 scripts/validate_screenshots.py --json    # machine-readable report

This never fails merely because the screenshots have not been captured yet: the
capture needs a browser and a display, and pretending the media exists would be
worse than saying it does not. It fails when the manifest contradicts itself, when
`index.json` disagrees with the images on disk, when a captured image is missing
from the manifest, or when the manifest drifts from the UI's own tab list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "docs" / "media" / "screenshots" / "manifest.json"
INDEX = PROJECT_ROOT / "docs" / "media" / "screenshots" / "index.json"
UI = PROJECT_ROOT / "static" / "index.html"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ScreenshotError(RuntimeError):
    """The screenshot set is inconsistent."""


def ui_tabs(ui_path: Path = UI) -> tuple[str, ...]:
    """Read the tab ids the UI actually declares."""
    text = ui_path.read_text(encoding="utf-8")
    return tuple(re.findall(r'class="mode-tab[^"]*"\s+(?:aria-current="page"\s+)?data-tab="([a-z]+)"', text))


def png_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) from a PNG's IHDR chunk."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        raise ScreenshotError("file is not a PNG")
    if data[12:16] != b"IHDR":
        raise ScreenshotError("PNG is missing its IHDR chunk")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def load_manifest(path: Path = MANIFEST) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScreenshotError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScreenshotError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ScreenshotError(f"{path} must contain a JSON object.")
    if manifest.get("schema") != 1:
        raise ScreenshotError("manifest.schema must be 1.")
    viewport = manifest.get("viewport")
    if not isinstance(viewport, dict):
        raise ScreenshotError("manifest.viewport must be an object.")
    for field in ("width", "height"):
        value = viewport.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ScreenshotError(f"manifest.viewport.{field} must be a positive integer.")
    scale = viewport.get("device_scale_factor", 1)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise ScreenshotError("manifest.viewport.device_scale_factor must be a positive number.")
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ScreenshotError("manifest.shots must be a non-empty list.")
    seen: set[str] = set()
    for shot in shots:
        if not isinstance(shot, dict):
            raise ScreenshotError("every manifest shot must be an object.")
        for field in ("id", "tab", "title", "file", "purpose"):
            value = shot.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ScreenshotError(f"every shot needs a non-empty {field}.")
        name = shot["file"]
        if not name.endswith(".png"):
            raise ScreenshotError(f"{name} must be a .png file name.")
        if "/" in name or "\\" in name or name.startswith("."):
            raise ScreenshotError(f"{name} must be a plain file name inside the screenshots directory.")
        if shot["id"] in seen:
            raise ScreenshotError(f"duplicate shot id: {shot['id']}")
        seen.add(shot["id"])
    return manifest


def check_manifest_against_ui(manifest: dict) -> list[str]:
    problems: list[str] = []
    tabs = ui_tabs()
    documented = {shot["tab"] for shot in manifest["shots"]}
    for tab in sorted(documented - set(tabs)):
        problems.append(f"manifest shot tab {tab!r} is not a tab in static/index.html")
    for tab in sorted(set(tabs) - documented):
        problems.append(f"UI tab {tab!r} has no screenshot entry in the manifest")
    return problems


def check_capture(index_path: Path = INDEX) -> tuple[list[str], int, int]:
    """Validate captured images against the manifest and index."""
    manifest = load_manifest()
    directory = manifest_path_dir(index_path)
    present = sorted(path.name for path in directory.glob("*.png"))
    if not index_path.is_file():
        if present:
            return (
                [f"{name} is present but there is no index.json describing it" for name in present],
                0,
                len(manifest["shots"]),
            )
        return [], 0, len(manifest["shots"])

    problems: list[str] = []
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScreenshotError(f"{index_path} is not valid JSON: {exc}") from exc
    if not isinstance(index, dict) or index.get("schema") != 1:
        raise ScreenshotError("index.schema must be 1.")
    entries = index.get("shots")
    if not isinstance(entries, list):
        raise ScreenshotError("index.shots must be a list.")

    planned = {shot["id"]: shot for shot in manifest["shots"]}
    recorded: set[str] = set()
    verified = 0
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry:
            problems.append("index entry without an id")
            continue
        shot_id = str(entry["id"])
        if shot_id not in planned:
            problems.append(f"index records {shot_id!r}, which the manifest does not plan")
            continue
        recorded.add(shot_id)
        path = directory / planned[shot_id]["file"]
        if not path.is_file():
            problems.append(f"{path.name} is described by index.json but missing on disk")
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if entry.get("sha256") != digest:
            problems.append(f"{path.name} does not match the sha256 recorded in index.json")
            continue
        if entry.get("bytes") != len(data):
            problems.append(f"{path.name} does not match the size recorded in index.json")
            continue
        try:
            width, height = png_size(data)
        except ScreenshotError as exc:
            problems.append(f"{path.name} is not a readable PNG: {exc}")
            continue
        scale = float(manifest["viewport"].get("device_scale_factor", 1))
        expected = (
            int(manifest["viewport"]["width"] * scale),
            int(manifest["viewport"]["height"] * scale),
        )
        if (width, height) != expected:
            problems.append(f"{path.name} is {width}x{height}, expected {expected[0]}x{expected[1]}")
            continue
        verified += 1

    for shot_id in sorted(set(planned) - recorded):
        problems.append(f"{planned[shot_id]['file']} is planned but not present in index.json")
    known = {shot["file"] for shot in manifest["shots"]}
    for name in present:
        if name not in known:
            problems.append(f"{name} is captured but not listed in the manifest")
    return problems, verified, len(planned)


def manifest_path_dir(index_path: Path) -> Path:
    return index_path.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--index", default=str(INDEX))
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(Path(args.manifest))
        problems = check_manifest_against_ui(manifest)
        capture_problems, verified, planned = check_capture(Path(args.index))
    except ScreenshotError as exc:
        print(f"screenshot validation failed: {exc}", file=sys.stderr)
        return 1
    problems.extend(capture_problems)

    report = {
        "planned": planned,
        "captured": verified,
        "problems": problems,
        "status": "inconsistent" if problems else ("complete" if verified == planned else "not-captured"),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif problems:
        print("Screenshot validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
    elif verified == planned:
        print(f"Screenshots validated: {verified} of {planned} captured and consistent with the manifest.")
    else:
        print(
            f"Screenshot manifest validated: {verified} of {planned} captured. "
            "Capture needs a browser; see docs/demo.md."
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
