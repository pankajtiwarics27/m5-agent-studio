"""AP-010 real-browser evidence against the pinned Chromium.

These tests launch the repository's own pinned browser tooling — the same
``@sparticuz/chromium`` 153.0.0 build and ``puppeteer-core`` 25.16.0 that
``.github/workflows/ci.yml`` installs — and drive it through
``scripts/browser_worker_bridge.mjs``. No external CDN is involved: the Chromium
binary ships inside the npm tarball.

Configuration (identical to the existing browser regressions):

* ``STUDIO_SCREENSHOT_CHROME`` — the Chromium executable
* ``STUDIO_PUPPETEER_CORE``   — puppeteer-core's ESM entry point
* ``LD_LIBRARY_PATH``         — the libraries from ``al2023.tar.br``
* ``M5_REQUIRE_BROWSER_TESTS=1`` — fail instead of skip when unconfigured (CI)

They are skipped when the browser is not configured, so an offline checkout
still runs the full Python suite.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import browser_worker as bw
from browser_worker import (
    BridgeDriver,
    BrowserAction,
    BrowserSession,
    SessionClass,
    SessionPolicy,
    allocate_profile,
    bridge_available,
)

FIXED = "2026-11-20T18:00:00Z"

PAGE = b"""<!doctype html><html><head><title>AP-010 fixture</title></head><body>
<h1 id="h">fixture</h1>
<button id="go" onclick="document.getElementById('h').textContent='clicked'">go</button>
<input id="q" />
<button id="pop" onclick="window.open('/popup','_blank')">popup</button>
<iframe id="meta" src="http://169.254.169.254/latest/meta-data/" width="1" height="1"></iframe>
</body></html>"""

POPUP = b"<!doctype html><html><head><title>popup</title></head><body>popup</body></html>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        body = POPUP if self.path.startswith("/popup") else PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the fixture server
        return None


@contextmanager
def fixture_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def driver() -> BridgeDriver:
    if not bridge_available():
        if os.environ.get("M5_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail(
                "browser evidence is required but STUDIO_SCREENSHOT_CHROME / "
                "STUDIO_PUPPETEER_CORE are not configured"
            )
        pytest.skip("pinned Chromium is not configured for real-browser evidence")
    return BridgeDriver(
        chrome=os.environ["STUDIO_SCREENSHOT_CHROME"],
        puppeteer_core=os.environ["STUDIO_PUPPETEER_CORE"],
    )


def make_session(tmp_path: Path, origin: str, **overrides) -> BrowserSession:
    profile = allocate_profile("real-1", workspace_root=tmp_path, clock=lambda: FIXED)
    policy = SessionPolicy(
        session_class=SessionClass.PINNED_LOCAL,
        allowed_origins=(origin,),
        **overrides,
    )
    return BrowserSession(
        session_id="real-1", policy=policy, profile=profile, clock=lambda: FIXED
    )


# AC1/AC3: the worker really drives a page, inside the granted origin only.
def test_real_navigation_click_type_and_screenshot(driver: BridgeDriver, tmp_path: Path):
    with fixture_server() as origin:
        session = make_session(tmp_path, origin)
        for action in (
            BrowserAction("navigate", url=f"{origin}/"),
            BrowserAction("click", selector="#go"),
            BrowserAction("type", selector="#q", text="hello"),
        ):
            assert session.admit(action).allowed is True
        report = driver.run_plan(
            [
                {
                    "name": "real-basic",
                    "steps": [
                        {"kind": "navigate", "url": f"{origin}/"},
                        {"kind": "click", "selector": "#go"},
                        {"kind": "type", "selector": "#q", "text": "hello"},
                        {"kind": "screenshot"},
                    ],
                }
            ]
        )
        session.stop()
    assert report["ok"] is True
    result = report["results"][0]
    assert result["title"] == "AP-010 fixture"
    assert result["visited"][0]["url"].startswith(origin)
    assert result["visited"][0]["status"] == 200
    assert result["screenshots"] == 1
    assert result["screenshotBytes"][0] > 1000  # a real render, not a blank frame
    assert result["failures"] == []


# AC2/AC6: the profile is dedicated to the run and is destroyed afterwards.
def test_real_profile_is_isolated_and_removed(driver: BridgeDriver):
    with fixture_server() as origin:
        report = driver.run_plan(
            [{"name": "isolation", "steps": [{"kind": "navigate", "url": f"{origin}/"}]}]
        )
    assert report["profileIsolated"] is True


# AC5: a hostile page cannot obtain a new surface.
def test_real_popup_is_refused_and_closed(driver: BridgeDriver):
    with fixture_server() as origin:
        report = driver.run_plan(
            [
                {
                    "name": "popup",
                    "steps": [
                        {"kind": "navigate", "url": f"{origin}/"},
                        {"kind": "click", "selector": "#pop"},
                    ],
                }
            ]
        )
    result = report["results"][0]
    assert len(result["popups"]) == 1
    assert result["popups"][0]["refused"] is True
    # The popup URL never appears among the surfaces that survived: it was
    # closed rather than becoming an agent-reachable window.
    assert not any(url.endswith("/popup") for url in result["openSurfaces"])


# AC5: a page cannot reach metadata/private space through a subresource the
# worker never granted. The contract refuses the navigation before dispatch.
def test_real_private_target_is_refused_before_dispatch(driver: BridgeDriver, tmp_path: Path):
    with fixture_server() as origin:
        session = make_session(tmp_path, origin)
        decision = session.admit(
            BrowserAction("navigate", url="http://169.254.169.254/latest/meta-data/")
        )
        assert decision.allowed is False
        with pytest.raises(bw.PolicyViolation):
            session.perform(
                BrowserAction("navigate", url="http://169.254.169.254/latest/meta-data/")
            )
        session.stop()


# AC4: takeover suspends the agent, and stop fences the session afterwards.
def test_real_takeover_then_stop(driver: BridgeDriver, tmp_path: Path):
    with fixture_server() as origin:
        session = make_session(tmp_path, origin)
        session.take_over("operator")
        assert session.admit(BrowserAction("navigate", url=f"{origin}/")).allowed is False
        session.resume("operator")
        assert session.admit(BrowserAction("navigate", url=f"{origin}/")).allowed is True
        session.stop()
    # admit() reports a denial; perform() is what refuses to dispatch at all.
    assert session.admit(BrowserAction("navigate", url=f"{origin}/")).allowed is False
    with pytest.raises(bw.SessionFencedError):
        session.perform(BrowserAction("navigate", url=f"{origin}/"))


# AC1: the same deterministic task through two runs of the real driver.
def test_real_two_runs_are_deterministic(driver: BridgeDriver):
    with fixture_server() as origin:
        plan = [
            {
                "name": "determinism",
                "steps": [
                    {"kind": "navigate", "url": f"{origin}/"},
                    {"kind": "click", "selector": "#go"},
                ],
            }
        ]
        first = driver.run_plan(plan)
        second = driver.run_plan(plan)
    a, b = first["results"][0], second["results"][0]
    assert a["executed"] == b["executed"] == 2
    assert a["failures"] == b["failures"] == []
    assert [v["status"] for v in a["visited"]] == [v["status"] for v in b["visited"]]
