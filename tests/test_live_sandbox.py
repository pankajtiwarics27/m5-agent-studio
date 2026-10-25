"""Opt-in integration test for a real pre-pulled Docker/Podman sandbox image.

Run with:
  docker pull python:3.12-slim
  STUDIO_SANDBOX_INTEGRATION=1 STUDIO_EXECUTION_MODE=sandbox pytest -q tests/test_live_sandbox.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import app as studio


@pytest.mark.skipif(os.environ.get("STUDIO_SANDBOX_INTEGRATION") != "1", reason="requires explicit live container-runtime opt-in")
def test_live_sandbox_enforces_mount_root_and_network_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    code = r"""
import os, pathlib, socket, uuid
assert os.getcwd() == "/workspace"
workspace_probe = pathlib.Path("/workspace") / (".studio-sandbox-probe-" + uuid.uuid4().hex)
workspace_probe.write_text("workspace is writable", encoding="utf-8")
workspace_probe.unlink()
root_probe = pathlib.Path("/") / (".studio-sandbox-probe-" + uuid.uuid4().hex)
try:
    root_probe.write_text("must not be writable", encoding="utf-8")
except OSError:
    pass
else:
    root_probe.unlink(missing_ok=True)
    raise AssertionError("container root filesystem is writable")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect(("1.1.1.1", 443))
except OSError:
    pass
else:
    raise AssertionError("container unexpectedly reached the public network")
finally:
    sock.close()
print("sandbox workspace, read-only root, and no-network checks passed")
"""
    code_rc, output, _truncated, timed_out = asyncio.run(
        studio._execute_limited(tmp_path, code, timeout=20, max_output_bytes=8192, python_code=True)
    )
    assert not timed_out, output.decode(errors="replace")
    assert code_rc == 0, output.decode(errors="replace")
    assert b"sandbox workspace, read-only root, and no-network checks passed" in output
