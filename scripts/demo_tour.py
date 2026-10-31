#!/usr/bin/env python3
"""Run the reproducible Studio demo and maintain its committed transcript (AS-045).

    python3 scripts/demo_tour.py             # print the tour
    python3 scripts/demo_tour.py --write     # regenerate docs/media/demo-transcript.md
    python3 scripts/demo_tour.py --check     # exit 1 if the committed transcript drifted
    python3 scripts/demo_tour.py --json      # machine-readable observations
    python3 scripts/demo_tour.py --serve     # serve a seeded demo instance for screenshots

Every mode runs against a throwaway database with a generated token, no provider,
no key, no model call, and no outbound request. `--serve` prints a JSON line with
the loopback URL and token, then waits until it is terminated, so a screenshot
tool can attach to the seeded state.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour as tour  # noqa: E402


class DemoStepFailure(tour.DemoStepError):
    """A step failed while driving a real loopback server."""


class HttpResponse:
    def __init__(self, status_code: int, payload: bytes) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return json.loads(self._payload.decode("utf-8"))


class HttpClient:
    """Minimal loopback client with the shape the tour expects."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        headers = {"Authorization": f"Bearer {self.token}", "Origin": self.base_url}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())

    def get(self, path: str, **_kwargs) -> HttpResponse:
        return self._request("GET", path)

    def post(self, path: str, json=None, **_kwargs) -> HttpResponse:  # noqa: A002 - test-client shape
        payload = {} if json is None else json
        import json as jsonlib

        return self._request("POST", path, jsonlib.dumps(payload).encode("utf-8"))

    def delete(self, path: str, **_kwargs) -> HttpResponse:
        return self._request("DELETE", path)


def _apply_environment(environment: tour.DemoEnvironment) -> None:
    os.environ.update(environment.environ())
    tour.assert_isolated(os.environ, environment)


def _isolated_app(environment: tour.DemoEnvironment):
    """Configure the modular app before startup without changing production defaults."""
    if "app" in sys.modules:
        raise tour.DemoEnvironmentError("The demo requires a fresh process; app was already imported.")
    _apply_environment(environment)
    import app as studio

    if (
        Path(studio.DB_PATH).resolve() != environment.db_path.resolve()
        or Path(studio.WORKSPACE_ROOT).resolve() != environment.workspace_dir.resolve()
        or Path(studio.STUDIO_API_TOKEN_FILE).resolve() != environment.token_path.resolve()
        or studio.STUDIO_AUTH_DISABLED
        or studio.STUDIO_EXECUTION_MODE != "disabled"
    ):
        raise tour.DemoEnvironmentError("The imported app does not use the isolated demo settings.")
    # The app binds the store at module level; set it before lifespan or any
    # requests run, in this fresh demo process only. Production stays unchanged.
    studio.HARNESS_STORE_DIR = environment.harness_dir
    # App startup seeds cloud providers from these inherited environment keys.
    # Remove known provider and MCP preset credentials before startup as well.
    for preset in (*studio.PROVIDER_PRESETS.values(), *studio.MCP_SERVER_PRESETS.values()):
        name = preset.get("env_key")
        if name:
            os.environ.pop(str(name), None)
    return studio


def run_in_process(environment: tour.DemoEnvironment) -> tuple[tour.StepOutcome, ...]:
    """Drive the real ASGI app in-process; no socket, no port, no subprocess."""
    studio = _isolated_app(environment)
    import warnings

    # The in-process test client warns about its own httpx pin; the tour is not
    # the place to surface that.
    warnings.filterwarnings("ignore", message=".*starlette.testclient.*")
    from fastapi.testclient import TestClient

    with TestClient(studio.app) as client:
        return tour.run_tour(client, environment.token())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, deadline_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise DemoStepFailure(f"The demo server at {base_url} never became healthy.")


def serve(environment: tour.DemoEnvironment, port: int) -> int:
    """Serve a seeded instance on loopback so a screenshot tool can attach."""
    chosen = port or _free_port()
    env = environment.environ()
    env["STUDIO_HOST"] = "127.0.0.1"
    env["STUDIO_PORT"] = str(chosen)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    base_url = f"http://127.0.0.1:{chosen}"
    log_path = environment.root / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "demo_tour.py"), "--_demo-server", "--root", str(environment.root), "--port", str(chosen)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(base_url)
            token = environment.token()
            outcomes = tour.run_tour(HttpClient(base_url, token), token)
            banner = {
                "url": base_url,
                "token": token,  # a throwaway token for a throwaway loopback instance
                "panes": [pane.tab for pane in tour.PANES],
                "steps": [outcome.step_id for outcome in outcomes],
            }
            print(json.dumps(banner), flush=True)
            while process.poll() is None:
                time.sleep(0.5)
            raise DemoStepFailure(
                f"The demo server exited early. Log: {log_path.read_text(errors='replace')[-2000:]}"
            )
        except KeyboardInterrupt:
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _write(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(text, encoding="utf-8")
    if previous == text:
        print(f"Demo transcript already up to date: {path}")
    else:
        print(f"Wrote {path} ({len(text)} bytes)")
    return 0


def _check(path: Path, text: str) -> int:
    if not path.exists():
        print(f"Missing demo transcript: {path}. Run with --write.", file=sys.stderr)
        return 1
    committed = path.read_text(encoding="utf-8")
    if committed == text:
        print(f"Demo transcript is up to date: {path}")
        return 0
    diff = difflib.unified_diff(
        committed.splitlines(True), text.splitlines(True), fromfile=str(path), tofile="regenerated"
    )
    print("Demo transcript has drifted; run `python3 scripts/demo_tour.py --write`:", file=sys.stderr)
    sys.stderr.writelines(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the committed transcript")
    parser.add_argument("--check", action="store_true", help="fail if the committed transcript drifted")
    parser.add_argument("--json", action="store_true", help="print machine-readable observations")
    parser.add_argument("--serve", action="store_true", help="serve a seeded instance for screenshots")
    parser.add_argument("--port", type=int, default=0, help="port for --serve (0 picks a free port)")
    parser.add_argument("--_demo-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--root", default="", help="reuse a specific directory as the throwaway environment"
    )
    args = parser.parse_args(argv)

    try:
        environment = tour.make_environment(Path(args.root).expanduser() if args.root else None)
        if args._demo_server:
            if not args.root or not 1 <= args.port <= 65535:
                raise tour.DemoEnvironmentError("The demo server requires a root and a valid port.")
            studio = _isolated_app(environment)
            import uvicorn

            uvicorn.run(studio.app, host="127.0.0.1", port=args.port, log_level="warning")
            return 0
        if args.serve:
            return serve(environment, args.port)
        outcomes = run_in_process(environment)
        rendered = tour.render_transcript(outcomes)
        if args.json:
            sys.stdout.write(tour.transcript_json(outcomes))
            return 0
        if args.write:
            return _write(PROJECT_ROOT / tour.TRANSCRIPT_PATH, rendered)
        if args.check:
            return _check(PROJECT_ROOT / tour.TRANSCRIPT_PATH, rendered)
        sys.stdout.write(rendered)
        return 0
    except (tour.DemoEnvironmentError, tour.DemoStepError) as exc:
        print(f"demo tour: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
