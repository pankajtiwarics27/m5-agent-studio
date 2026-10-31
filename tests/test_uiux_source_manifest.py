"""UIUX-P0-01: the pinned source manifest must describe the tree it ships with.

`product/M5_2_0_UIUX_Source_Manifest.json` is the program's answer to a plan
written from web extracts: a hash of every file the UI work may touch, plus the
tab and route tables read out of the markup and the server. A manifest nobody
checks is decoration, so the claims it makes are re-derived here from the files
themselves — the comparison never trusts the manifest's own arithmetic.

The drift test is the important one: a story that edits `static/index.html`
without regenerating the manifest turns CI red, which is what makes the pin a
pin instead of a snapshot someone remembers to refresh.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
FRONTEND = ROOT / "static" / "index.html"
APP = ROOT / "app.py"
SHOT_MANIFEST = ROOT / "docs" / "media" / "screenshots" / "manifest.json"
GENERATOR = ROOT / "scripts" / "uiux_source_manifest.py"

BUTTON_RE = re.compile(r'<button\b[^>]*\bdata-tab="([^"]+)"[^>]*>(.*?)</button>', re.DOTALL)
ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_source_manifest", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_manifest_covers_the_files_the_program_depends_on() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["document_type"] == "m5_uiux_source_manifest"
    assert manifest["pinned_by_story"] == "UIUX-P0-01"
    assert manifest["generator"] == "scripts/uiux_source_manifest.py"

    paths = [entry["path"] for entry in manifest["files"]]
    assert len(paths) == len(set(paths)), "duplicate path in the manifest"
    for required in (
        "static/index.html",
        "app.py",
        "studio.py",
        "scripts/build_studio.py",
        "scripts/capture_screenshots.mjs",
        "docs/media/screenshots/manifest.json",
    ):
        assert required in paths, f"the manifest does not pin {required}"


def test_every_pinned_digest_matches_the_tree() -> None:
    """The pin is only worth something if it still describes disk."""
    for entry in _manifest()["files"]:
        path = ROOT / entry["path"]
        assert path.is_file(), f"{entry['path']} is pinned but missing"
        assert entry["sha256"] == _sha256(path), f"{entry['path']} changed; regenerate the manifest"
        assert entry["bytes"] == path.stat().st_size
        if entry["lines"] is not None:
            assert entry["lines"] == len(path.read_text(encoding="utf-8").splitlines())


def test_the_manifest_and_the_ledger_share_one_pin() -> None:
    pin = _manifest()["pinned_source_commit"]
    ledger_pin = json.loads(LEDGER.read_text(encoding="utf-8"))["source_commit_sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", pin)
    assert pin == ledger_pin, "the manifest and the ledger disagree about the pinned commit"


def test_the_pinned_commit_is_an_ancestor_of_head() -> None:
    """A pin that is not in this clone's history is a fabricated pin."""
    pin = _manifest()["pinned_source_commit"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, local git query only
            ["git", "merge-base", "--is-ancestor", pin, "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - a checkout without git cannot answer
        pytest.skip("git is not available in this environment")
    if result.returncode not in (0, 1):
        pytest.skip(f"git could not answer ({result.returncode}): {result.stderr.strip()[:120]}")
    assert result.returncode == 0, f"pinned commit {pin} is not an ancestor of HEAD"


def test_tab_table_matches_the_markup_and_the_screenshot_manifest() -> None:
    manifest = _manifest()
    frontend = manifest["frontend"]
    html = FRONTEND.read_text(encoding="utf-8")

    parsed = []
    for match in BUTTON_RE.finditer(html):
        dom_id, body = match.group(1), match.group(2)
        label = re.search(r"<span>([^<]+)</span>", body)
        parsed.append({"dom_id": dom_id, "label": label.group(1).strip() if label else ""})

    assert [(tab["dom_id"], tab["label"]) for tab in frontend["tabs"]] == [
        (entry["dom_id"], entry["label"]) for entry in parsed
    ]
    assert frontend["tab_count"] == len(parsed) == 6

    # The label/id split that trips sessions up: these pairs are not interchangeable.
    by_id = {tab["dom_id"]: tab["label"] for tab in frontend["tabs"]}
    assert by_id["hall"] == "Council"
    assert by_id["council"] == "Pipeline"

    shot_tabs = json.loads(SHOT_MANIFEST.read_text(encoding="utf-8"))["shots"]
    assert [shot["tab"] for shot in shot_tabs] == [tab["dom_id"] for tab in frontend["tabs"]]
    assert manifest["screenshot_manifest"]["tab_ids_match_frontend"] is True

    registry = json.loads(LEDGER.read_text(encoding="utf-8"))["tab_registry"]
    assert [entry["dom_id"] for entry in registry] == [tab["dom_id"] for tab in frontend["tabs"]]


def test_route_table_matches_the_server_source() -> None:
    manifest = _manifest()
    source = APP.read_text(encoding="utf-8")
    expected = [{"method": m.group(1).upper(), "path": m.group(2)} for m in ROUTE_RE.finditer(source)]
    assert manifest["api"]["routes"] == expected
    assert manifest["api"]["route_count"] == len(expected)
    counted: dict[str, int] = {}
    for route in expected:
        counted[route["method"]] = counted.get(route["method"], 0) + 1
    assert manifest["api"]["by_method"] == dict(sorted(counted.items()))


def test_screenshot_state_is_reported_not_assumed() -> None:
    """Nothing is captured in CI; the manifest must say so rather than imply media exists."""
    shots = _manifest()["screenshot_manifest"]
    assert shots["total"] == len(shots["shots"]) == 6
    on_disk = [
        entry for entry in shots["shots"] if (ROOT / "docs" / "media" / "screenshots" / entry["file"]).is_file()
    ]
    assert shots["captured"] == len(on_disk)
    if not on_disk:
        assert shots["captured"] == 0


def test_frontend_api_literals_are_services_paths() -> None:
    literals = _manifest()["frontend"]["api_literals"]
    assert literals, "the browser script names no /api/ paths, which cannot be true"
    assert literals == sorted(set(literals)), "literals must be unique and sorted"
    for literal in literals:
        assert literal.startswith("/api/"), f"{literal!r} is not an /api/ path"
        assert "$" not in literal, f"{literal!r} was not normalised"


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_check_mode_fails_when_the_surface_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce drift without touching the committed files."""
    module = _load_generator()
    stale = tmp_path / "manifest.json"
    manifest = module.build_manifest()
    manifest["files"][0]["sha256"] = "0" * 64
    stale.write_text(module.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "MANIFEST_PATH", stale)

    assert module.main(["--check", "--quiet"]) == 1

    fresh = tmp_path / "fresh.json"
    fresh.write_text(module.dumps(module.build_manifest()), encoding="utf-8")
    monkeypatch.setattr(module, "MANIFEST_PATH", fresh)
    assert module.main(["--check", "--quiet"]) == 0


def test_the_generator_refuses_a_ledger_without_a_full_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_generator()
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    ledger["source_commit_sha"] = "main"
    broken = tmp_path / "ledger.json"
    broken.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(module, "LEDGER_PATH", broken)

    with pytest.raises(module.ManifestError):
        module.build_manifest()
