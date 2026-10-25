#!/usr/bin/env python3
"""Launch a disposable loopback Studio and verify its local auth/security path."""
from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def request(url: str, *, token: str = "", origin: str = "", method: str = "GET", data: bytes | None = None) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    entrypoint = ROOT / ("studio.py" if "--standalone" in sys.argv[1:] else "app.py")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="m5-studio-smoke-") as temp_name:
        temp = Path(temp_name)
        data_dir = temp / "data"
        config_dir = temp / "config"
        data_dir.mkdir(mode=0o700)
        config_dir.mkdir(mode=0o700)
        token_file = config_dir / "api-token"
        db_path = data_dir / "studio.db"
        log_path = temp / "server.log"
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(temp),
            "TMPDIR": str(temp),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "STUDIO_HOST": "127.0.0.1",
            "STUDIO_PORT": str(port),
            "STUDIO_API_TOKEN": "",
            "STUDIO_AUTH_DISABLED": "0",
            "STUDIO_API_TOKEN_FILE": str(token_file),
            "STUDIO_DB_PATH": str(db_path),
            "STUDIO_EXECUTION_MODE": "disabled",
            "STUDIO_ALLOW_HOST_EXECUTION": "0",
        }
        url = f"http://127.0.0.1:{port}"
        with log_path.open("wb") as log:
            proc = subprocess.Popen([sys.executable, str(entrypoint)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                healthy = False
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        status, _ = request(url + "/healthz")
                        if status == 200:
                            healthy = True
                            break
                    except (OSError, TimeoutError):
                        time.sleep(0.2)
                if not healthy:
                    raise RuntimeError(f"Studio did not start. Logs: {log_path.read_text(errors='replace')[-4000:]}")
                if not token_file.is_file():
                    raise RuntimeError("Studio started without creating the configured local API token file.")
                token = token_file.read_text(encoding="utf-8").strip()
                if len(token) < 32:
                    raise RuntimeError("Generated API token is shorter than expected.")
                unauth, _ = request(url + "/api/providers")
                if unauth != 401:
                    raise RuntimeError(f"Protected API returned {unauth} without a token; expected 401.")
                authorized, _ = request(url + "/api/providers", token=token)
                if authorized != 200:
                    raise RuntimeError(f"Protected API returned {authorized} with the generated token; expected 200.")
                html_status, html = request(url + "/")
                if html_status != 200 or b"sessionStorage" not in html or token.encode() in html:
                    raise RuntimeError("UI token helper is missing or the secret was exposed in rendered HTML.")
                node = shutil.which("node")
                ui_result = "UI auth-helper interaction skipped (Node not installed)"
                if node:
                    ui_test = subprocess.run([node, str(ROOT / "scripts" / "test_ui_auth_helper.js")], input=html, capture_output=True, timeout=10)
                    if ui_test.returncode != 0:
                        raise RuntimeError(f"UI auth-helper interaction test failed: {ui_test.stderr.decode(errors='replace')}")
                    ui_result = ui_test.stdout.decode(errors="replace").strip()
                payload = json.dumps({"alias": "smoke-test", "name": "Smoke Test", "targets": ["model"]}).encode()
                cross_origin, _ = request(url + "/api/combos", token=token, origin="https://untrusted.invalid", method="POST", data=payload)
                if cross_origin != 403:
                    raise RuntimeError(f"Cross-origin state-changing request returned {cross_origin}; expected 403.")
                if os.name == "posix":
                    import stat
                    if stat.S_IMODE(token_file.stat().st_mode) != 0o600:
                        raise RuntimeError("Generated API token file permissions are not 0600.")
                if not db_path.is_file():
                    raise RuntimeError("Studio did not create its configured SQLite database.")
                print(f"Local smoke test passed: loopback startup, health, token generation/permissions, protected API, Origin rejection, isolated DB path. {ui_result}")
                return 0
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Local smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
