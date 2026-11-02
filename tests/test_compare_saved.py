"""Compare save/revisit/export contract and opt-in real-browser journey.

The browser test uses scripts/demo_tour.py's disposable store and a synthetic token. The history
bundle is mocked in the browser; no model stream or demo database write is made by the walk.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import selectors
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALK = ROOT / "tests" / "browser" / "compare_saved_walk.mjs"
DEMO = ROOT / "scripts" / "demo_tour.py"


def test_compare_saved_view_uses_only_existing_session_and_history_surfaces() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'const DEBATE_SESSION_KEY = "m5_compare_last_view_v1"' in source
    assert 'sessionStorage.setItem(DEBATE_SESSION_KEY, raw)' in source
    assert 'fetch("/api/history/export", { cache: "no-store" })' in source
    assert 'anchor.download = "studio-history-export.json"' in source
    assert 'id="debateFindSavedBtn"' in source and 'id="debateLoadSavedBtn"' in source
    assert 'id="debateExportHistoryBtn"' in source and 'id="debateForgetSessionBtn"' in source
    assert 'role="status" aria-live="polite"' in source[source.index('id="debateSavedStatus"') - 80 : source.index('id="debateSavedStatus"') + 180]
    assert 'consumeSSE("/api/debate/stream"' in source  # existing run route remains the run route
    assert 'fetch("/api/compare/' not in source


def test_after_screenshots_are_complete_and_hash_checked() -> None:
    root = ROOT / "docs" / "media" / "compare-saved" / "after"
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert "mocked authenticated history export" in index["source"]
    assert {shot["id"] for shot in index["shots"]} == {"session-reload", "history-revisit"}
    for shot in index["shots"]:
        image = (root / shot["file"]).read_bytes()
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(image) == shot["bytes"]
        assert hashlib.sha256(image).hexdigest() == shot["sha256"]
        width, height = struct.unpack(">II", image[16:24])
        assert (width, height) == (index["viewport"]["width"], index["viewport"]["height"])


def _browser_env(tmp_path: Path) -> dict[str, str] | None:
    chrome = os.environ.get("STUDIO_SCREENSHOT_CHROME")
    puppeteer = os.environ.get("STUDIO_PUPPETEER_CORE")
    if not chrome or not puppeteer:
        if os.environ.get("M5_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("browser regressions are required but Chromium or puppeteer-core is not configured")
        return None
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    return env


@contextmanager
def _isolated_demo(env: dict[str, str]):
    process = subprocess.Popen(
        [sys.executable, str(DEMO), "--serve"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    try:
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=60):
            raise AssertionError("isolated demo did not publish its throwaway token in 60 seconds")
        line = process.stdout.readline()
        if not line:
            raise AssertionError(f"isolated demo exited before startup (status {process.poll()})")
        instance = json.loads(line)
        assert instance["url"].startswith("http://127.0.0.1:")
        assert instance["token"]
        yield instance
    finally:
        selector.close()
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()


def test_compare_saved_reload_revisit_export_browser_walk(tmp_path: Path) -> None:
    env = _browser_env(tmp_path)
    if env is None:
        pytest.skip("set STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE for the browser walk")
    with _isolated_demo(env) as instance:
        command = ["node", str(WALK), "--base-url", instance["url"], "--token", instance["token"]]
        screenshot_dir = os.environ.get("M5_COMPARE_SAVED_SCREENSHOTS_DIR", "")
        if screenshot_dir:
            command.extend(["--screenshots-dir", screenshot_dir])
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["console_errors"] == 0
    assert len(report["checks"]) >= 6
