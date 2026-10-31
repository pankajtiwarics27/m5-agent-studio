from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_screenshots.py"
CAPTURE = ROOT / "scripts" / "capture_screenshots.mjs"
MANIFEST = ROOT / "docs" / "media" / "screenshots" / "manifest.json"
INDEX = ROOT / "docs" / "media" / "screenshots" / "index.json"

sys.path.insert(0, str(ROOT / "scripts"))
import validate_screenshots as vs  # noqa: E402


def _png(width: int, height: int) -> bytes:
    """Build a minimal valid PNG of the given size."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    ihdr = struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    data_chunk = b"IDAT" + zlib.compress(raw, 9)
    idat = struct.pack(">I", len(data_chunk) - 4) + data_chunk
    idat += struct.pack(">I", zlib.crc32(data_chunk) & 0xFFFFFFFF)
    end = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + end


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    shutil.copyfile(MANIFEST, manifest_path)
    index_path = tmp_path / "index.json"
    return manifest_path, index_path


def _manifest(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))


def _capture_all(tmp_path: Path, manifest: dict) -> dict:
    """Write PNGs for every planned shot plus a matching index."""
    viewport = manifest["viewport"]
    scale = viewport.get("device_scale_factor", 1)
    entries = []
    for shot in manifest["shots"]:
        data = _png(int(viewport["width"] * scale), int(viewport["height"] * scale))
        (tmp_path / shot["file"]).write_bytes(data)
        import hashlib

        entries.append(
            {
                "id": shot["id"],
                "tab": shot["tab"],
                "file": shot["file"],
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    index = {"schema": 1, "captured_at": "2026-10-29T00:00:00Z", "viewport": viewport, "shots": entries}
    (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return index


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_committed_manifest_is_valid() -> None:
    manifest = vs.load_manifest(MANIFEST)

    assert len(manifest["shots"]) == 6
    assert [shot["tab"] for shot in manifest["shots"]] == list(vs.ui_tabs())


def test_every_ui_tab_has_a_screenshot_entry() -> None:
    assert vs.check_manifest_against_ui(vs.load_manifest(MANIFEST)) == []


def test_manifest_detects_an_unknown_tab(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["shots"][0]["tab"] = "not-a-pane"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    problems = vs.check_manifest_against_ui(vs.load_manifest(manifest_path))

    assert any("not a tab" in problem for problem in problems)
    assert any("has no screenshot entry" in problem for problem in problems)


def test_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["shots"][0]["file"] = "../escape.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(vs.ScreenshotError, match="plain file name"):
        vs.load_manifest(manifest_path)


def test_manifest_rejects_duplicates_and_missing_fields(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["shots"][1]["id"] = manifest["shots"][0]["id"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(vs.ScreenshotError, match="duplicate shot id"):
        vs.load_manifest(manifest_path)

    manifest = _manifest(tmp_path)
    del manifest["shots"][0]["purpose"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(vs.ScreenshotError, match="non-empty purpose"):
        vs.load_manifest(manifest_path)


def test_manifest_rejects_a_bad_viewport(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["viewport"]["width"] = 0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(vs.ScreenshotError, match="viewport.width"):
        vs.load_manifest(manifest_path)


def test_manifest_must_be_json(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(vs.ScreenshotError, match="not valid JSON"):
        vs.load_manifest(manifest_path)


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------


def test_uncaptured_set_is_not_an_error(tmp_path: Path) -> None:
    manifest_path, index_path = _workspace(tmp_path)

    problems, verified, planned = vs.check_capture(index_path)

    assert problems == []
    assert (verified, planned) == (0, 6)
    assert manifest_path.exists()


def test_a_complete_capture_validates(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    _capture_all(tmp_path, manifest)

    problems, verified, planned = vs.check_capture(index_path)

    assert problems == []
    assert (verified, planned) == (6, 6)


def test_a_modified_image_is_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    _capture_all(tmp_path, manifest)
    target = tmp_path / manifest["shots"][2]["file"]
    target.write_bytes(_png(10, 10))

    problems, verified, _ = vs.check_capture(index_path)

    assert any("does not match the sha256" in problem for problem in problems)
    assert verified == 5


def test_a_missing_image_is_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    _capture_all(tmp_path, manifest)
    (tmp_path / manifest["shots"][0]["file"]).unlink()

    problems, _, _ = vs.check_capture(index_path)

    assert any("missing on disk" in problem for problem in problems)


def test_wrong_dimensions_are_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    index = _capture_all(tmp_path, manifest)
    shot = manifest["shots"][1]
    data = _png(100, 100)
    (tmp_path / shot["file"]).write_bytes(data)
    import hashlib

    entry = next(item for item in index["shots"] if item["id"] == shot["id"])
    entry["bytes"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    index_path.write_text(json.dumps(index), encoding="utf-8")

    problems, _, _ = vs.check_capture(index_path)

    assert any("expected 2880x1800" in problem for problem in problems)


def test_an_unplanned_image_is_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    _capture_all(tmp_path, manifest)
    (tmp_path / "leftover.png").write_bytes(_png(2880, 1800))

    problems, _, _ = vs.check_capture(index_path)

    assert any("leftover.png is captured but not listed" in problem for problem in problems)


def test_a_planned_but_uncaptured_shot_is_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    index = _capture_all(tmp_path, manifest)
    dropped = index["shots"].pop()
    (tmp_path / dropped["file"]).unlink()
    index_path.write_text(json.dumps(index), encoding="utf-8")

    problems, _, _ = vs.check_capture(index_path)

    assert any("planned but not present" in problem for problem in problems)


def test_images_without_an_index_are_detected(tmp_path: Path) -> None:
    _, index_path = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    (tmp_path / manifest["shots"][0]["file"]).write_bytes(_png(2880, 1800))

    problems, _, _ = vs.check_capture(index_path)

    assert any("no index.json" in problem for problem in problems)


def test_png_size_rejects_non_png_data() -> None:
    with pytest.raises(vs.ScreenshotError, match="not a PNG"):
        vs.png_size(b"GIF89a not a png at all")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60, cwd=ROOT
    )


def test_cli_reports_the_capture_state_rather_than_assuming_it() -> None:
    """An uncaptured set is valid; a captured one must be valid too.

    The old version of this test pinned "0 of 6" against the committed tree, which
    made a legitimately captured set a test failure. What the CLI owes a reader is
    the count it actually found, so the assertion is that the count matches the
    files on disk.
    """
    result = _cli()

    assert result.returncode == 0, result.stderr
    report = json.loads(_cli("--json").stdout)
    assert f"{report['captured']} of {report['planned']} captured" in result.stdout

    captured_files = [
        entry for entry in json.loads((ROOT / "docs" / "media" / "screenshots" / "manifest.json").read_text(encoding="utf-8"))["shots"]
        if (ROOT / "docs" / "media" / "screenshots" / entry["file"]).is_file()
    ]
    assert report["captured"] == len(captured_files)
    assert report["status"] == ("complete" if captured_files else "not-captured")


def test_cli_json_status_for_the_committed_tree() -> None:
    result = _cli("--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["planned"] == 6
    assert report["problems"] == []
    assert report["status"] in {"not-captured", "complete"}
    assert report["captured"] == (6 if report["status"] == "complete" else 0)


def test_cli_fails_on_an_inconsistent_manifest(tmp_path: Path) -> None:
    manifest_path, _ = _workspace(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["shots"][0]["tab"] = "ghost"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _cli("--manifest", str(manifest_path), "--index", str(tmp_path / "index.json"))

    assert result.returncode == 1
    assert "ghost" in result.stderr


# ---------------------------------------------------------------------------
# Capture script (no browser required for the plan)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_capture_script_plan_matches_the_manifest() -> None:
    result = subprocess.run(
        ["node", str(CAPTURE), "--dry-run"], capture_output=True, text=True, timeout=60, cwd=ROOT
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert [shot["id"] for shot in plan["shots"]] == [shot["id"] for shot in manifest["shots"]]
    assert plan["viewport"] == manifest["viewport"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_capture_script_refuses_to_run_without_a_browser(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            shutil.which("node") or "node",
            str(CAPTURE),
            "--manifest",
            str(MANIFEST),
            "--out",
            str(tmp_path),
            "--base-url",
            "http://127.0.0.1:1",
            "--token",
            "t" * 32,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "STUDIO_SCREENSHOT_CHROME": "", "HOME": str(tmp_path)},
    )

    assert result.returncode == 1
    assert "capture_screenshots:" in result.stderr
    assert list(tmp_path.glob("*.png")) == []
