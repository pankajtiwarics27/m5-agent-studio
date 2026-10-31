"""Regression tests for scripts/isolation_smoke.py (M5-P0-04).

The smoke script is the reproducible evidence tool for the isolation story.
These tests pin its always-run behavior: it must execute cleanly without a
container runtime, report every observable check, and — critically — must
report isolation as unqualified with dependent execution blocked whenever the
live container probes have not been observed. It must never advertise
container isolation without live proof.

Run in a subprocess so the in-process ``app`` import state stays untouched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def smoke_source(tmp_path: Path) -> Path:
    """The smoke CLI imports app in a child, so isolate its source-relative store."""
    source = tmp_path / "source"
    source.mkdir()
    for module in ROOT.glob("*.py"):
        shutil.copy2(module, source / module.name)
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "isolation_smoke.py", source / "scripts" / "isolation_smoke.py")
    return source


def _run(source: Path, *args: str, overrides: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    home = source.parent / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update({"HOME": str(home), "STUDIO_DB_PATH": str(home / "studio.db")})
    env.update(overrides or {})
    return subprocess.run(
        [sys.executable, str(source / "scripts" / "isolation_smoke.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=source, env=env, check=False,
    )


def test_smoke_script_reports_observable_checks_and_unqualified_status(smoke_source: Path) -> None:
    result = _run(smoke_source)

    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    # Fingerprints and runtime probe always present.
    assert "Environment fingerprint and runtime availability" in out
    assert "runtime_available:" in out
    # Sandbox invocation invariants are observed from the real builder.
    assert "[PASS] no network" in out
    assert "[PASS] read-only container root" in out
    assert "[PASS] single workspace bind mount" in out
    # Skill-code gates deny by default and never host-fallback.
    assert "[PASS] default-off skill-code denial" in out
    assert "[PASS] sandbox-mode fails closed, no host fallback" in out
    # Confinement denials observed.
    assert "[PASS] traversal denied" in out
    assert "[PASS] symlink escape denied" in out
    # Without observed live probes the honest status is unqualified + blocked.
    assert "isolation_status: unqualified" in out
    assert "dependent_execution: blocked (fail closed)" in out


def test_smoke_script_skips_live_probes_without_opt_in(smoke_source: Path) -> None:
    result = _run(smoke_source)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED: pass --live" in result.stdout
    assert "live-probes-passed" not in result.stdout


def test_smoke_script_never_marks_qualified_without_live_probes(smoke_source: Path) -> None:
    result = _run(smoke_source)

    assert "isolation_status: qualified" not in result.stdout
    assert "released under the observed container boundary" not in result.stdout


def test_smoke_script_handles_hung_container_runtime_info_probe(tmp_path: Path, smoke_source: Path) -> None:
    fake_rt = tmp_path / "hung-docker"
    fake_rt.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_rt.chmod(0o755)
    result = _run(smoke_source, overrides={"STUDIO_SANDBOX_RUNTIME": str(fake_rt)}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime_available: no" in result.stdout
    assert "timed out" in result.stdout
    assert "isolation_status: unqualified" in result.stdout

