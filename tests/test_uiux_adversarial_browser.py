"""Run the adversarial refusal walk against a throwaway app in CI's browser job (UIUX-P6-02).

The walk itself lives in `scripts/adversarial_walk.mjs` so a person can run it against any
Studio. This module is the part CI needs: it starts one isolated demo server, points the walk
at it, and fails the job when a refusal is drawn as a success. `M5_REQUIRE_BROWSER_TESTS=1`
turns a missing browser into a failure instead of a skip, so the job cannot pass by skipping
the thing it exists to run.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALK = ROOT / "scripts" / "adversarial_walk.mjs"
DEMO = ROOT / "scripts" / "demo_tour.py"


@pytest.fixture
def browser_env(tmp_path: Path) -> dict[str, str]:
    if not os.environ.get("STUDIO_SCREENSHOT_CHROME") or not os.environ.get("STUDIO_PUPPETEER_CORE"):
        if os.environ.get("M5_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("the adversarial walk is required but Chromium or puppeteer-core is not configured")
        pytest.skip("set STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE to run the adversarial walk")
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    return env


@contextmanager
def isolated_demo(env: dict[str, str]):
    """Start the repository's generated-token demo and always stop its server child."""
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


def _walk(base_url: str, token: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, local walk only
        ["node", str(WALK), "--base-url", base_url, "--token", token, "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_every_refusal_is_drawn_as_a_refusal(browser_env: dict[str, str]) -> None:
    """One run, every attack: a refusal may be drawn, but never as a success."""
    with isolated_demo(browser_env) as instance:
        result = _walk(instance["url"], instance["token"], browser_env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    report = json.loads(result.stdout[result.stdout.index("{"):])
    assert report["failed"] == 0, [check for check in report["checks"] if not check["passed"]]
    assert report["passed"] >= 15, report
    # The walk must not lose the refusals it was written for.
    titles = " ".join(check["title"] for check in report["checks"])
    for required in (
        "not as \"Applied!\"",
        "cross-origin 403",
        "over the size limit",
        "no longer has says 404",
        "a wrong token is reported as 401",
        "a refused chat stream",
        "Retry-After",
        "none of the attacks raised a page error",
    ):
        assert required in titles, f"the walk no longer checks {required!r}: {titles}"
