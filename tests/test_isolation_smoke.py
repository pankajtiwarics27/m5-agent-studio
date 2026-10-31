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

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "isolation_smoke.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=120, cwd=ROOT, check=False,
    )


def test_smoke_script_reports_observable_checks_and_unqualified_status() -> None:
    result = _run()

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


def test_smoke_script_skips_live_probes_without_opt_in() -> None:
    result = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED: pass --live" in result.stdout
    assert "live-probes-passed" not in result.stdout


def test_smoke_script_never_marks_qualified_without_live_probes() -> None:
    result = _run()

    assert "isolation_status: qualified" not in result.stdout
    assert "released under the observed container boundary" not in result.stdout


def test_smoke_script_handles_hung_container_runtime_info_probe(tmp_path: Path) -> None:
    fake_rt = tmp_path / "hung-docker"
    fake_rt.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_rt.chmod(0o755)
    env = dict(**__import__("os").environ, STUDIO_SANDBOX_RUNTIME=str(fake_rt))
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime_available: no" in result.stdout
    assert "timed out" in result.stdout
    assert "isolation_status: unqualified" in result.stdout

