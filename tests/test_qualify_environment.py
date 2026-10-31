"""Tests for scripts/qualify_environment.py (M5-P3-01 / M5-P4-01 tooling).

The qualification suite exists so that a *human* on a Docker host can produce
isolation and hardware evidence that CI cannot. These tests pin the properties
that make its output trustworthy without a container runtime or 24 GB of RAM:

* a check that cannot run is never reported as a pass;
* the suite's own container flags stay aligned with the product's argv;
* the verdicts (qualified / unqualified / blocked) follow from recorded checks;
* the CLI exits non-zero whenever the evidence is incomplete.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import qualify_environment as qe

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


_FAKE_IMAGE_INSPECT = json.dumps(
    [
        {
            "Id": "sha256:9f1c2b7d5e0a4c3f8b6d1e2a7c0b5f9d4e8a3c6b1f0d7e2a5c8b4f3e0d9a2c7b",
            "RepoTags": ["python:3.12-slim"],
            "RepoDigests": ["python@sha256:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"],
        }
    ]
)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_run(behaviour: dict[str, Any] | None = None) -> Any:
    """A ``run_command`` replacement driven by markers inside the probe code."""
    behaviour = dict(behaviour or {})

    def _run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None, cwd: Path | None = None):
        joined = " ".join(str(part) for part in argv)
        code = str(argv[argv.index("-c") + 1]) if "-c" in argv else str(argv[-1])
        if "isolation_smoke.py" in joined:
            return _FakeCompleted(behaviour.get("smoke_exit", 0), "isolation_status: qualified\n")
        if "positive-ok" in code:
            target = Path(argv[argv.index("--mount") + 1].split("source=")[1].split(",")[0])
            if behaviour.get("mount_missing"):
                return _FakeCompleted(4, "listing:<missing-workspace>\nwrite-error:2:FileNotFoundError\n")
            if behaviour.get("mount_empty"):
                return _FakeCompleted(4, "listing:\nwrite-error:30:OSError\n")
            if behaviour.get("positive_fails"):
                return _FakeCompleted(1, "listing:qualify-link\n")
            (target / "qualification-positive.txt").write_text("ok", encoding="utf-8")
            return _FakeCompleted(0, "listing:qualify-link\ncwd:/workspace\npositive-ok\n")
        if "denials:" in code:
            leaked = behaviour.get("leaked", "none")
            return _FakeCompleted(0 if leaked == "none" else 3, f"reads:outside-direct=ENOENT\ndenials:{leaked}\n")
        if "credentials:" in code:
            leaked = behaviour.get("credentials_visible", "none")
            home = behaviour.get("home", "/tmp")
            return _FakeCompleted(0 if leaked == "none" else 3, f"home:{home}\ncredentials:{leaked}\n")
        if "bytearray" in code:
            exit_code = behaviour.get("memory_exit", 137)
            report = behaviour.get("memory_report") or None
            stdout = f"{json.dumps(report)}\n" if report else ""
            stderr = behaviour.get("memory_stderr", "" if exit_code == 0 else "Killed")
            return _FakeCompleted(exit_code, stdout, stderr)
        if "spawned" in code:
            return _FakeCompleted(0, "spawned 42\n")
        if "ps" in argv:
            return _FakeCompleted(0, behaviour.get("ps_output", ""))
        if "image" in argv and "inspect" in argv:
            return _FakeCompleted(
                behaviour.get("image_exit", 0),
                behaviour.get("image_inspect_stdout", _FAKE_IMAGE_INSPECT),
            )
        if "--version" in argv:
            return _FakeCompleted(0, "Docker version 27.0.0\n")
        if "info" in argv:
            return _FakeCompleted(behaviour.get("info_exit", 0), "")
        return _FakeCompleted(0, "")

    return _run


class _FakeStudio:
    """Stand-in for ``app`` in product-path checks."""

    STUDIO_SANDBOX_RUNTIME = "docker"
    STUDIO_SANDBOX_IMAGE = "python:3.12-slim"

    def __init__(self, *, host_execution: bool = False) -> None:
        self.host_execution = host_execution

    def _sandbox_command(self, root: Path, command: str, *, python_code: bool = False,
                         container_name: str | None = None) -> list[str]:
        if self.STUDIO_SANDBOX_RUNTIME.startswith("m5-nonexistent"):
            raise RuntimeError(f"Sandbox runtime `{self.STUDIO_SANDBOX_RUNTIME}` is not installed.")
        return qe.container_argv(
            self.STUDIO_SANDBOX_RUNTIME, self.STUDIO_SANDBOX_IMAGE, root, command, name=container_name or ""
        )

    async def _execute_limited(self, root: Path, command: str, *, timeout: float,
                               max_output_bytes: int, python_code: bool = False):
        if self.STUDIO_SANDBOX_RUNTIME.startswith("m5-nonexistent"):
            raise RuntimeError(f"Sandbox runtime `{self.STUDIO_SANDBOX_RUNTIME}` is not installed.")
        if self.host_execution:
            (root / "host-write-marker.txt").write_text("host", encoding="utf-8")
            return 0, b"ran-on-host", False, False
        raise PermissionError("sandbox execution is disabled")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_defaults_to_both_suites() -> None:
    args = qe.parse_args([])
    assert qe._selected_suites(args) == [qe.STORY_P3, qe.STORY_P4]


def test_cli_single_suite_selection() -> None:
    assert qe._selected_suites(qe.parse_args(["--sandbox"])) == [qe.STORY_P3]
    assert qe._selected_suites(qe.parse_args(["--hardware"])) == [qe.STORY_P4]
    assert qe._selected_suites(qe.parse_args(["--all"])) == [qe.STORY_P3, qe.STORY_P4]


def test_cli_rejects_conflicting_suites() -> None:
    with pytest.raises(SystemExit):
        qe.parse_args(["--sandbox", "--hardware"])


def test_cli_parses_measurement_options() -> None:
    args = qe.parse_args(
        ["--hardware", "--pressure-gb", "8", "--model-command", "run-me", "--model-runs", "5",
         "--profile-out", "dist/p.jsonl", "--json", "dist/q.json", "--markdown", "dist/q.md"]
    )
    assert args.pressure_gb == 8.0
    assert args.model_command == "run-me"
    assert args.model_runs == 5
    assert args.profile_out == Path("dist/p.jsonl")
    assert args.json == Path("dist/q.json")
    assert args.markdown == Path("dist/q.md")


# ---------------------------------------------------------------------------
# Recorder verdicts
# ---------------------------------------------------------------------------


def test_recorder_verdict_qualified_only_when_every_required_check_ran_and_passed() -> None:
    recorder = qe.Recorder()
    recorder.record("a", qe.STORY_P3, "one", qe.STATUS_PASS)
    recorder.record("b", qe.STORY_P3, "two", qe.STATUS_PASS)
    assert recorder.verdict(qe.STORY_P3) == "qualified"


def test_recorder_verdict_unqualified_on_required_failure() -> None:
    recorder = qe.Recorder()
    recorder.record("a", qe.STORY_P3, "one", qe.STATUS_PASS)
    recorder.record("b", qe.STORY_P3, "two", qe.STATUS_FAIL, detail="network reachable")
    assert recorder.verdict(qe.STORY_P3) == "unqualified"


def test_recorder_verdict_blocked_when_required_check_skipped_or_blocked() -> None:
    recorder = qe.Recorder()
    recorder.record("a", qe.STORY_P3, "one", qe.STATUS_PASS)
    recorder.record("b", qe.STORY_P3, "two", qe.STATUS_SKIP, detail="no runtime")
    assert recorder.verdict(qe.STORY_P3) == "blocked"

    recorder.record("c", qe.STORY_P3, "three", qe.STATUS_BLOCKED, detail="no runtime")
    assert recorder.verdict(qe.STORY_P3) == "blocked"


def test_recorder_optional_skip_does_not_block_verdict() -> None:
    recorder = qe.Recorder()
    recorder.record("a", qe.STORY_P4, "one", qe.STATUS_PASS)
    recorder.record("b", qe.STORY_P4, "informational", qe.STATUS_FAIL, required=False)
    assert recorder.verdict(qe.STORY_P4) == "qualified"


def test_recorder_check_turns_a_raising_probe_into_a_failure() -> None:
    recorder = qe.Recorder()

    def _boom() -> tuple[bool, str]:
        raise RuntimeError("probe exploded")

    result = recorder.check("a", qe.STORY_P3, "title", _boom)
    assert result.status == qe.STATUS_FAIL
    assert "probe exploded" in result.detail


def test_recorder_skip_reason_never_runs_the_probe() -> None:
    recorder = qe.Recorder()
    calls: list[int] = []

    def _probe() -> tuple[bool, str]:
        calls.append(1)
        return True, ""

    result = recorder.check("a", qe.STORY_P3, "title", _probe, skip_reason="no runtime")
    assert result.status == qe.STATUS_SKIP
    assert calls == []


def test_recorder_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        qe.Recorder().record("a", qe.STORY_P3, "title", "passed-ish")


# ---------------------------------------------------------------------------
# Container invocation
# ---------------------------------------------------------------------------


def test_container_argv_carries_every_hardening_flag(tmp_path: Path) -> None:
    argv = qe.container_argv("docker", "python:3.12-slim", tmp_path, "print(1)")
    joined = " ".join(argv)
    for flag in (
        "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit", "--memory", "--cpus", "--tmpfs",
    ):
        assert flag in joined, flag
    assert "--privileged" not in argv
    binds = [part for part in argv if part.startswith("type=bind")]
    assert len(binds) == 1
    assert f"source={tmp_path.resolve()}" in binds[0]
    assert "target=/workspace" in binds[0]


def test_container_argv_matches_the_product_hardening_flags(tmp_path: Path) -> None:
    """The suite must not qualify a sandbox the product does not actually build."""
    import unittest.mock

    studio = pytest.importorskip("app")
    with unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", "/bin/sh"):
        product = studio._sandbox_command(tmp_path, "print(1)", container_name="m5-test")
    product_joined = " ".join(product)
    suite = qe.container_argv("docker", "python:3.12-slim", tmp_path, "print(1)")
    suite_joined = " ".join(suite)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in product_joined, f"product lost {flag}"
        assert flag in suite_joined, f"suite lost {flag}"
    assert product_joined.count("type=bind") == suite_joined.count("type=bind") == 1


def test_looks_denied_recognises_product_refusals() -> None:
    assert qe._looks_denied("Code execution is disabled.")
    assert qe._looks_denied("cannot connect to the docker daemon")
    assert not qe._looks_denied("positive-ok")


def test_detect_runtime_returns_nothing_when_no_cli_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe.shutil, "which", lambda name: None)
    assert qe.detect_runtime("auto") == ("", "")


def test_detect_runtime_prefers_the_requested_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "podman" else None)
    assert qe.detect_runtime("podman") == ("/usr/bin/podman", "podman")


def test_an_explicit_missing_runtime_is_never_replaced_by_another_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--runtime podman` on a Docker-only host must block, not silently use Docker."""
    monkeypatch.setattr(qe.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    assert qe.detect_runtime("podman") == ("", "")
    assert qe.detect_runtime("auto") == ("/usr/bin/docker", "docker")


# ---------------------------------------------------------------------------
# Sandbox suite
# ---------------------------------------------------------------------------


def _run_sandbox_suite(monkeypatch: pytest.MonkeyPatch, *, behaviour: dict[str, Any] | None = None,
                       runtime_available: bool = True, studio: Any | None = None) -> qe.Recorder:
    monkeypatch.setattr(qe, "run_command", _make_fake_run(behaviour))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: studio if studio is not None else _FakeStudio())
    recorder = qe.Recorder()
    qe.sandbox_suite(
        recorder,
        runtime="/usr/bin/docker" if runtime_available else "",
        runtime_name="docker" if runtime_available else "",
        image="python:3.12-slim",
        timeout=5.0,
    )
    return recorder


def test_suite_argv_matches_the_product_argv(tmp_path: Path) -> None:
    """The suite must probe the sandbox the product actually builds."""
    import unittest.mock

    studio = pytest.importorskip("app")
    with unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", "/bin/sh"):
        product = studio._sandbox_command(tmp_path, "print(1)", python_code=True, container_name="m5-test")
    suite = qe.container_argv("docker", studio.STUDIO_SANDBOX_IMAGE, tmp_path, "print(1)", name="m5-test")
    ok, detail = qe.compare_argv(product, suite)
    assert ok, detail
    assert "bind mounts=1" in detail


def test_argv_parity_detects_a_missing_product_flag(tmp_path: Path) -> None:
    suite = qe.container_argv("docker", "python:3.12-slim", tmp_path, "print(1)")
    without_env = [part for part in suite if not part.startswith("--ipc")]
    ok, detail = qe.compare_argv(suite, without_env)
    assert not ok
    assert "--ipc" in detail


def test_sandbox_suite_qualifies_when_every_probe_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch)
    assert recorder.verdict(qe.STORY_P3) == "qualified", [
        (result.check_id, result.status, result.detail) for result in recorder.results
    ]
    check_ids = {result.check_id for result in recorder.results}
    assert {
        "p3.runtime.available",
        "p3.image.local",
        "p3.positive.execute",
        "p3.adversarial.escape_matrix",
        "p3.adversarial.memory_limit",
        "p3.adversarial.descendants",
        "p3.environment.bind",
        "p3.product.argv",
        "p3.product.no_host_fallback",
        "p3.product.isolation_smoke",
    } <= check_ids


def test_sandbox_suite_is_blocked_without_a_container_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, runtime_available=False)
    verdict = recorder.verdict(qe.STORY_P3)
    assert verdict == "blocked"
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.runtime.available"] == qe.STATUS_BLOCKED
    # Nothing that requires a container may be reported as passing.
    for check_id in ("p3.image.local", "p3.positive.execute", "p3.adversarial.escape_matrix"):
        assert statuses[check_id] == qe.STATUS_SKIP


def test_memory_limit_denial_records_the_exit_code_as_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused allocation is a pass, and the record says what was refused."""
    recorder = _run_sandbox_suite(monkeypatch)
    result = next(r for r in recorder.results if r.check_id == "p3.adversarial.memory_limit")
    assert result.status == qe.STATUS_PASS
    assert result.evidence["exit_code"] == 137
    assert result.evidence["requested_bytes"] == qe.MEMORY_PROBE_BYTES
    # The process was killed before it could print; the absence is recorded.
    assert result.evidence["container_report"] is None


def test_memory_limit_failure_names_the_counters_the_container_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3 GiB allocation that survives --memory 512m must say why it is a failure.

    This is the shape the 2026-10-31 Apple M5 run produced: exit 0 with the
    container's own memory.max below the allocation. The record has to carry the
    numbers, otherwise the operator cannot tell a swap escape from an
    unenforced cgroup.
    """
    report = {
        "allocated_bytes": qe.MEMORY_PROBE_BYTES,
        "rss_bytes": qe.MEMORY_PROBE_BYTES,
        "memory_max": "536870912",
        "memory_high": "max",
        "memory_current": str(qe.MEMORY_PROBE_BYTES),
        "memory_peak": str(qe.MEMORY_PROBE_BYTES),
        "memory_swap_max": "max",
        "memory_swap_current": "0",
        "memory_events": "max 0 oom 0 oom_kill 0",
    }
    recorder = _run_sandbox_suite(
        monkeypatch, behaviour={"memory_exit": 0, "memory_report": report}
    )
    result = next(r for r in recorder.results if r.check_id == "p3.adversarial.memory_limit")
    assert result.status == qe.STATUS_FAIL
    assert result.evidence["container_report"] == report
    assert "memory.max=536870912" in result.detail
    assert "swap.max=max" in result.detail
    assert recorder.verdict(qe.STORY_P3) == "unqualified"


def test_memory_probe_code_reports_the_cgroup_counters_it_ran_under() -> None:
    """The probe source is executable and reports its own accounting."""
    import subprocess

    code = qe.memory_probe_code(4 * 1024 * 1024)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["allocated_bytes"] == 4 * 1024 * 1024
    assert report["rss_bytes"] and report["rss_bytes"] >= 4 * 1024 * 1024
    for key in (
        "memory_max",
        "memory_current",
        "memory_peak",
        "memory_swap_max",
        "memory_swap_current",
        "memory_events",
    ):
        assert key in report


def test_sandbox_suite_fails_when_an_escape_is_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"leaked": "network"})
    assert recorder.verdict(qe.STORY_P3) == "unqualified"
    failed = {result.check_id: result.detail for result in recorder.failures(qe.STORY_P3)}
    assert "p3.adversarial.escape_matrix" in failed
    assert "network" in failed["p3.adversarial.escape_matrix"]


def test_sandbox_suite_fails_when_the_positive_path_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"positive_fails": True})
    assert "p3.positive.execute" in {result.check_id for result in recorder.failures(qe.STORY_P3)}


def test_a_missing_image_blocks_the_suite_instead_of_accusing_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing about isolation was observed, so the verdict must not be `unqualified`."""
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"image_exit": 1})
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.image.local"] == qe.STATUS_BLOCKED
    assert statuses["p3.positive.execute"] == qe.STATUS_SKIP
    assert recorder.verdict(qe.STORY_P3) == "blocked"
    detail = next(r.detail for r in recorder.results if r.check_id == "p3.image.local")
    assert "pull" in detail


def test_an_unusable_workspace_mount_is_blocked_not_unqualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime that cannot see the shared workspace is a setup gap, not a leak."""
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"mount_missing": True})
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.positive.execute"] == qe.STATUS_BLOCKED
    assert recorder.verdict(qe.STORY_P3) == "blocked"
    detail = next(r.detail for r in recorder.results if r.check_id == "p3.positive.execute")
    assert "File sharing" in detail
    # Nothing that needs a trustworthy mount may be reported as an isolation failure.
    for check_id in ("p3.adversarial.escape_matrix", "p3.adversarial.memory_limit"):
        assert statuses[check_id] == qe.STATUS_SKIP


def test_an_unwritable_empty_mount_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"mount_empty": True})
    assert recorder.verdict(qe.STORY_P3) == "blocked"
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.positive.execute"] == qe.STATUS_BLOCKED


def test_sandbox_suite_reports_host_execution_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, studio=_FakeStudio(host_execution=True))
    result = next(item for item in recorder.results if item.check_id == "p3.product.no_host_fallback")
    assert result.status == qe.STATUS_FAIL
    assert "real=True" in result.detail


def test_no_host_fallback_passes_when_missing_runtime_denies_and_host_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _run_sandbox_suite(monkeypatch)
    result = next(item for item in recorder.results if item.check_id == "p3.product.no_host_fallback")
    assert result.status == qe.STATUS_PASS
    assert "host_write simulated=False real=False" in result.detail


def test_sandbox_suite_blocks_product_checks_when_app_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "run_command", _make_fake_run({}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: None)
    recorder = qe.Recorder()
    qe.sandbox_suite(recorder, runtime="/usr/bin/docker", runtime_name="docker",
                     image="python:3.12-slim", timeout=5.0)
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.product.argv"] == qe.STATUS_BLOCKED
    assert statuses["p3.product.no_host_fallback"] == qe.STATUS_BLOCKED
    assert recorder.verdict(qe.STORY_P3) == "blocked"


def test_sandbox_suite_fails_when_the_isolation_smoke_test_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"smoke_exit": 1})
    assert "p3.product.isolation_smoke" in {result.check_id for result in recorder.failures(qe.STORY_P3)}


# ---------------------------------------------------------------------------
# Hardware suite
# ---------------------------------------------------------------------------


def _fingerprint(total_memory: int) -> dict[str, Any]:
    return {
        "os": "Darwin",
        "machine": "arm64",
        "cpu_model": "Apple M4 Pro",
        "total_memory_bytes": total_memory,
        "total_memory_gib": round(total_memory / qe.GIB, 2),
        "memory": {"swap_used_bytes": 0},
    }


def test_admission_probe_rejects_a_second_heavy_request() -> None:
    ok, detail = qe._admission_probe(24 * qe.GIB)
    assert ok, detail
    assert "second=refused" in detail


def test_admission_probe_never_admits_beyond_capacity() -> None:
    ok, detail = qe._admission_probe(8 * qe.GIB)
    assert ok, detail


def test_priors_are_not_treated_as_verified() -> None:
    ok, detail = qe._priors_not_verified()
    assert ok
    assert "unknown" in detail


def test_admission_wired_check_observes_the_real_serving_path() -> None:
    """The probe must report the wiring it can actually see, not a source scan."""
    ok, detail = qe._admission_wired()
    assert ok is True, detail
    assert "refused while resident=True" in detail


def test_admission_probe_reports_a_serving_path_that_bypasses_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio = pytest.importorskip("app")

    async def _bypass(*_args: object, **_kwargs: object):
        yield {"type": "content", "text": "probe"}

    monkeypatch.setattr(studio, "stream_any_model", _bypass)
    ok, detail = qe._admission_wired()
    assert ok is False
    assert "does not enforce" in detail


def test_hardware_suite_blocks_off_target_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "_admission_wired", lambda: (True, "stub"))
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder,
        fingerprint=_fingerprint(8 * qe.GIB),
        pressure_gb=0.0,
        model_command="",
        model_runs=1,
        profile_out=None,
        timeout=5.0,
    )
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p4.machine.fingerprint"] == qe.STATUS_BLOCKED
    assert statuses["p4.pressure"] == qe.STATUS_SKIP
    assert statuses["p4.model_load"] == qe.STATUS_SKIP
    assert recorder.verdict(qe.STORY_P4) == "blocked"


def test_hardware_suite_qualifies_on_target_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qe, "_pressure_probe",
        lambda *args, **kwargs: (True, "measured", {"requested_gib": args[0] if args else 0}),
    )
    monkeypatch.setattr(
        qe, "_model_probe",
        lambda command, runs, timeout, out, sampler=None: (True, "1/1 runs exited 0", {"runs": []}),
    )
    monkeypatch.setattr(qe, "_admission_wired", lambda: (True, "confirmed by the stub"))
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder,
        fingerprint=_fingerprint(24 * qe.GIB),
        pressure_gb=12.0,
        model_command="probe",
        model_runs=1,
        profile_out=None,
        timeout=5.0,
    )
    assert recorder.verdict(qe.STORY_P4) == "qualified", [
        (result.check_id, result.status, result.detail) for result in recorder.results
    ]


def test_pressure_probe_reports_swap_evidence() -> None:
    ok, detail, evidence = qe._pressure_probe(0.05, timeout=30.0)
    assert isinstance(ok, bool)
    assert "requested_gib" in evidence
    assert "swap_growth_bytes" in evidence
    assert detail


def test_model_probe_records_each_run(tmp_path: Path) -> None:
    payload = json.dumps(
        {"model_id": "probe", "model_digest": "sha256:abc", "runtime": "stub",
         "context_length": 4096, "first_token_ms": 1, "prompt_tokens": 8, "output_tokens": 4}
    )
    ok, detail, evidence = qe._model_probe(f"echo '{payload}'", 2, 30.0, tmp_path / "profile.jsonl")
    assert ok, detail
    assert evidence["successful_runs"] == 2
    assert Path(evidence["profile_path"]).exists()


def test_model_profile_refuses_to_invent_a_timing() -> None:
    timings = [
        {"run": 1, "exit_code": 0, "wall_s": 0.01,
         "reported": {"model_id": "m", "first_token_ms": 500, "output_tokens": 4}, "stderr_tail": ""}
    ]
    import model_calibration

    path = qe._write_model_profile("probe", timings, Path("/tmp/m5-qualify-inconsistent.jsonl"))
    profile = model_calibration.load_profile(path)
    # A first token after the end of the run is impossible: the sample is recorded
    # as failed instead of contributing a fabricated throughput.
    assert profile.failed_runs == 1
    assert profile.aggregate("decode_tokens_per_s", "warm").median is None


# ---------------------------------------------------------------------------
# Record assembly and end-to-end exit codes
# ---------------------------------------------------------------------------


def test_build_record_and_markdown_never_upgrade_a_blocked_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "run_command", _make_fake_run({}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    recorder = qe.Recorder()
    qe.sandbox_suite(recorder, runtime="", runtime_name="", image="python:3.12-slim", timeout=5.0)
    record = qe.build_record(
        recorder,
        fingerprint=_fingerprint(16 * qe.GIB),
        suites=[qe.STORY_P3],
        runtime_name="",
        runtime_path="",
        image="python:3.12-slim",
        invocation=["qualify_environment.py", "--sandbox"],
    )
    assert record["verdicts"][qe.STORY_P3]["verdict"] == "blocked"
    assert record["claims"]["container_isolation"] == "blocked"
    assert record["fingerprint"]["total_memory_gib"] == 16.0
    markdown = qe.render_markdown(record)
    assert "M5-P3-01" in markdown
    assert "blocked" in markdown
    assert "qualified" not in markdown.split("## How to read this")[0].replace("`qualified`", "")


def test_main_exits_two_when_evidence_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qe, "run_command", _make_fake_run({}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("", ""))
    json_path = tmp_path / "record.json"
    markdown_path = tmp_path / "record.md"
    exit_code = qe.main(
        ["--all", "--json", str(json_path), "--markdown", str(markdown_path), "--quiet"]
    )
    assert exit_code == 2
    record = json.loads(json_path.read_text(encoding="utf-8"))
    assert record["verdicts"][qe.STORY_P3]["verdict"] == "blocked"
    assert record["verdicts"][qe.STORY_P4]["verdict"] == "blocked"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Environment qualification record")


def test_main_exits_zero_only_for_a_fully_qualified_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qe, "run_command", _make_fake_run({}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("/usr/bin/docker", "docker"))
    monkeypatch.setattr(qe, "environment_fingerprint", lambda: _fingerprint(24 * qe.GIB))
    monkeypatch.setattr(qe, "_pressure_probe", lambda *args, **kwargs: (True, "measured", {}))
    monkeypatch.setattr(qe, "_model_probe", lambda command, runs, timeout, out, sampler=None: (True, "ok", {}))
    exit_code = qe.main(
        [
            "--all", "--quiet",
            "--pressure-gb", "1",
            "--model-command", "probe",
            "--json", str(tmp_path / "r.json"),
        ]
    )
    assert exit_code == 0


def test_main_exits_one_when_a_required_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(qe, "run_command", _make_fake_run({"leaked": "network"}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("/usr/bin/docker", "docker"))
    assert qe.main(["--sandbox", "--quiet", "--json", str(tmp_path / "r.json")]) == 1


def test_environment_fingerprint_binds_the_record_to_this_repo() -> None:
    fingerprint = qe.environment_fingerprint()
    assert fingerprint["python"]
    assert "total_memory_bytes" in fingerprint
    assert isinstance(fingerprint["git_revision"], str)
    assert len(fingerprint["git_revision"]) in (0, 40)


def test_script_is_runnable_end_to_end_in_this_environment() -> None:
    """The suite must always produce a record, and never self-qualify.

    ``--runtime`` is pinned at a name that cannot exist so the result does not
    depend on whether the host running this test happens to have Docker (a CI
    runner does, a minimal container does not).
    """
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "qualify_environment.py"), "--all", "--quiet",
         "--runtime", "m5-no-such-container-runtime"],
        capture_output=True, text=True, timeout=600, cwd=ROOT, check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "M5-P3-01: blocked" in result.stdout
    assert "M5-P4-01: blocked" in result.stdout


def test_a_runtime_without_the_image_never_produces_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host with Docker but no image yields `blocked` (exit 2), never `qualified`."""
    monkeypatch.setattr(qe, "run_command", _make_fake_run({"image_exit": 1}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("/usr/bin/docker", "docker"))
    exit_code = qe.main(["--sandbox", "--quiet", "--json", str(tmp_path / "r.json")])
    assert exit_code == 2
    record = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert record["verdicts"][qe.STORY_P3]["verdict"] == "blocked"
    assert record["claims"]["container_isolation"] == "blocked"


def test_dry_run_prints_the_plan_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def _never(argv, **_kwargs):  # noqa: ANN001 - signature mirrors run_command
        calls.append(list(argv))
        raise AssertionError("--dry-run must not execute anything")

    monkeypatch.setattr(qe, "run_command", _never)
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("/usr/bin/docker", "docker"))
    exit_code = qe.main(
        ["--all", "--dry-run", "--pressure-gb", "16", "--model-command", "probe-me",
         "--json", str(tmp_path / "r.json")]
    )
    assert exit_code == 0
    assert calls == []
    assert not (tmp_path / "r.json").exists()
    out = capsys.readouterr().out
    assert "Planned qualification" in out
    assert "--network=none" in out and "--read-only" in out and "--pull=never" in out
    assert "touch 16.0 GiB" in out and "probe-me" in out
    assert "Nothing above has been executed" in out


def test_dry_run_is_explicit_about_skipped_measurements(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("", ""))
    assert qe.main(["--hardware", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "pressure measurement SKIPPED" in out
    assert "model load measurement SKIPPED" in out


def test_asyncio_coroutine_path_is_used_for_product_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the fake studio's async entry point matches what the suite awaits."""
    studio = _FakeStudio()
    with pytest.raises(PermissionError):
        asyncio.run(studio._execute_limited(Path("/tmp"), "print(1)", timeout=1, max_output_bytes=16))


def test_container_argv_preserves_posix_uid_gid_and_the_shell_exec_form(tmp_path: Path) -> None:
    """Parity with app._sandbox_command: drop container root and run via `sh -lc exec`."""
    import os

    argv = qe.container_argv("docker", "python:3.12-slim", tmp_path, "print(1)")
    if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
        idx = argv.index("--user")
        assert argv[idx + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[-2] == "-lc"
    assert argv[-1].startswith("exec python3 -c ")


def test_container_argv_bakes_probe_inputs_into_the_code(tmp_path: Path) -> None:
    """Extra argv would land in the shell, not the interpreter, under `-lc exec`."""
    code = qe._escape_probe_code(
        outside=tmp_path / "canary.txt",
        link="/workspace/qualify-link",
        read_only_targets=("/etc/hostname",),
        host_port=0,
        network_target="127.0.0.1:9",
    )
    assert str(tmp_path / "canary.txt") in code
    assert "/workspace/qualify-link" in code
    assert "127.0.0.1:9" in code
    argv = qe.container_argv("docker", "python:3.12-slim", tmp_path, code)
    assert argv[-2] == "-lc"
    assert argv[-1].startswith("exec python3 -c ")
    assert "qualify-link" in argv[-1]


def test_escape_probe_code_passes_with_isolated_canary_and_readonly_targets(tmp_path: Path) -> None:
    """Execute _ESCAPE_CODE directly: readable container /etc/hostname must not false-fail,
    while dangling symlink + invisible host canary + read-only target pass cleanly."""
    import os
    import socket
    import subprocess

    ws = tmp_path / "workspace"
    ws.mkdir()
    missing_host_canary = tmp_path / "unmounted-host-secret.txt"
    link = ws / "qualify-link"
    link.symlink_to(missing_host_canary)
    ro_dir = tmp_path / "ro_dir"
    ro_dir.mkdir(mode=0o555)
    ro_probe = ro_dir / "cannot-write.txt"

    closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_sock.bind(("127.0.0.1", 0))
    closed_port = int(closed_sock.getsockname()[1])
    closed_sock.close()

    code = qe._escape_probe_code(
        outside=missing_host_canary,
        link=str(link),
        read_only_targets=(str(ro_probe),),
        host_port=0,
        network_target=f"127.0.0.1:{closed_port}",
    )
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    finally:
        ro_dir.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "denials:none" in proc.stdout


def test_escape_probe_code_detects_outside_canary_symlink_writable_root_network_and_env_leaks(
    tmp_path: Path,
) -> None:
    """Execute _ESCAPE_CODE directly against real leaked resources and verify every leak class fails."""
    import os
    import socket
    import subprocess

    ws = tmp_path / "workspace"
    ws.mkdir()
    leaked_canary = tmp_path / "mounted-host-secret.txt"
    leaked_canary.write_text("host-only secret", encoding="utf-8")
    leaked_link = ws / "qualify-link"
    leaked_link.symlink_to(leaked_canary)
    writable_ro_target = tmp_path / "writable-root-probe.txt"

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = int(listener.getsockname()[1])
    try:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "STUDIO_QUALIFICATION_SENTINEL": "leaked-from-host",
        }
        code = qe._escape_probe_code(
            outside=leaked_canary,
            link=str(leaked_link),
            read_only_targets=(str(writable_ro_target),),
            host_port=port,
            network_target=f"127.0.0.1:{port}",
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    finally:
        listener.close()

    assert proc.returncode == 3
    marker = next((ln for ln in proc.stdout.splitlines() if ln.startswith("denials:")), "")
    leaked_items = set(marker.partition(":")[2].split(","))
    assert {"outside-direct", "readonly-root", "symlink", "network", "env"} <= leaked_items



# ---------------------------------------------------------------------------
# Thermal behavior (M5-P4-01 task 3)
# ---------------------------------------------------------------------------


def test_thermal_snapshot_parses_pmset_thermal_pressure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        qe, "_capture",
        lambda argv, timeout=10.0: "Note: No thermal warning level has been recorded\n"
        "CPU_Speed_Limit              = 85\nGPU_Speed_Limit              = 100\n",
    )
    snapshot = qe.thermal_snapshot()
    assert snapshot["available"] is True
    assert snapshot["source"] == "pmset -g therm"
    assert snapshot["readings"]["cpu_speed_limit"] == 85
    assert qe.thermal_stress(snapshot) == 15.0  # 100 - 85: throttling pressure


def test_thermal_snapshot_reports_when_pmset_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(qe, "_capture", lambda argv, timeout=10.0: "")
    snapshot = qe.thermal_snapshot()
    assert snapshot["available"] is False
    assert "pmset" in str(snapshot["source"])


def test_linux_thermal_readings_convert_millidegrees(tmp_path: Path) -> None:
    for index, value in enumerate(("48500", "52000", "garbage")):
        zone = tmp_path / f"thermal_zone{index}"
        zone.mkdir()
        (zone / "temp").write_text(value, encoding="utf-8")
    assert qe.linux_thermal_readings(tmp_path) == [48.5, 52.0]


def test_thermal_snapshot_without_a_readable_source_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe.platform, "system", lambda: "Plan9")
    snapshot = qe.thermal_snapshot()
    assert snapshot["available"] is False
    assert "Plan9" in snapshot["note"]


def test_thermal_sampler_summarises_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter(
        [
            {"available": True, "source": "pmset -g therm", "readings": {"cpu_speed_limit": 100}},
            {"available": True, "source": "pmset -g therm", "readings": {"cpu_speed_limit": 70}},
        ]
    )
    monkeypatch.setattr(qe, "thermal_snapshot", lambda: next(readings))
    sampler = qe.ThermalSampler()
    sampler.sample()
    sampler.sample()
    summary = sampler.summary()
    assert sampler.available is True
    assert summary["throttled"] is True
    assert summary["cpu_speed_limit_min"] == 70
    assert (summary["stress_min"], summary["stress_max"]) == (0.0, 30.0)


def test_thermal_sampler_without_readings_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "thermal_snapshot", lambda: {"available": False, "source": None, "note": "nope"})
    sampler = qe.ThermalSampler()
    sampler.sample()
    summary = sampler.summary()
    assert sampler.available is False
    assert summary["available_samples"] == 0
    assert summary["stress_max"] is None


# ---------------------------------------------------------------------------
# Per-category coding cases (M5-P4-01 tasks 1-2)
# ---------------------------------------------------------------------------


def _cases_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_coding_cases_accepts_the_object_form(tmp_path: Path) -> None:
    cases = qe.load_coding_cases(
        _cases_file(
            tmp_path,
            {
                "categories": ["bugfix", "tests"],
                "cases": [
                    {"id": "b1", "category": "bugfix", "prompt": "fix it"},
                    {"id": "t1", "category": "tests"},
                ],
            },
        )
    )
    assert [case["id"] for case in cases] == ["b1", "t1"]
    assert cases[1]["prompt"] == ""


def test_load_coding_cases_accepts_a_bare_list(tmp_path: Path) -> None:
    cases = qe.load_coding_cases(_cases_file(tmp_path, [{"id": "a", "category": "c"}]))
    assert cases == [{"id": "a", "category": "c", "prompt": ""}]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"cases": []}, "non-empty"),
        ({"cases": [{"category": "c"}]}, "id"),
        ({"cases": [{"id": "a"}]}, "category"),
        ({"cases": [{"id": "a", "category": "c"}, {"id": "a", "category": "c"}]}, "duplicate"),
        ({"cases": ["nope"]}, "object"),
        ({"categories": ["x"], "cases": [{"id": "a", "category": "c"}]}, "not declared"),
    ],
)
def test_load_coding_cases_rejects_malformed_files(tmp_path: Path, payload: object, message: str) -> None:
    with pytest.raises(qe.CodingCaseError) as excinfo:
        qe.load_coding_cases(_cases_file(tmp_path, payload))
    assert message in str(excinfo.value)


def test_load_coding_cases_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(qe.CodingCaseError):
        qe.load_coding_cases(path)


def test_coding_case_probe_passes_case_identity_through_the_environment() -> None:
    sampler = qe.ThermalSampler()
    ok, detail, evidence = qe._coding_case_probe(
        [{"id": "case-7", "category": "refactor", "prompt": "do the thing"}],
        'printf \'{"seen":"%s/%s"}\\n\' "$STUDIO_QUALIFY_CASE_ID" "$STUDIO_QUALIFY_CASE_CATEGORY"',
        1,
        30.0,
        sampler,
    )
    assert ok, detail
    assert evidence["cases"][0]["runs"][0]["reported"]["seen"] == "case-7/refactor"


def test_coding_case_probe_records_failures_and_uncertainty() -> None:
    sampler = qe.ThermalSampler()
    ok, detail, evidence = qe._coding_case_probe(
        [
            {"id": "good", "category": "bugfix", "prompt": ""},
            {"id": "bad", "category": "bugfix", "prompt": ""},
        ],
        'if [ "$STUDIO_QUALIFY_CASE_ID" = bad ]; then exit 3; fi; echo ok',
        2,
        30.0,
        sampler,
    )
    assert ok is False
    assert "incomplete: bad" in detail
    assert evidence["incomplete_cases"] == ["bad"]
    assert evidence["categories"] == {"bugfix": {"cases": 2, "completed": 1}}
    assert evidence["uncertainty"]["cases"] == 2
    # "good" succeeded twice and "bad" never did, so neither is a single-run case.
    assert evidence["uncertainty"]["single_run_cases"] == 0


def test_coding_case_probe_measures_wall_time_spread() -> None:
    sampler = qe.ThermalSampler()
    ok, _detail, evidence = qe._coding_case_probe(
        [{"id": "slow", "category": "c", "prompt": ""}], "sleep 0.05", 2, 30.0, sampler
    )
    assert ok
    case = evidence["cases"][0]
    assert case["wall_s_median"] is not None
    assert case["wall_s_spread_ratio"] is not None


# ---------------------------------------------------------------------------
# Hardware-suite integration for thermal + cases
# ---------------------------------------------------------------------------


def test_thermal_check_is_skipped_without_a_load_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder, fingerprint=_fingerprint(24 * qe.GIB), pressure_gb=0.0, model_command="",
        model_runs=1, profile_out=None, timeout=5.0,
    )
    result = next(item for item in recorder.results if item.check_id == "p4.thermal")
    assert result.status == qe.STATUS_SKIP
    assert result.required is False


def test_thermal_check_blocks_when_the_platform_cannot_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "thermal_snapshot", lambda: {"available": False, "source": "x", "note": "unreadable"})
    monkeypatch.setattr(qe, "_model_probe", lambda command, runs, timeout, out, sampler=None: (True, "ok", {}))
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder, fingerprint=_fingerprint(24 * qe.GIB), pressure_gb=0.0, model_command="probe",
        model_runs=1, profile_out=None, timeout=5.0,
    )
    result = next(item for item in recorder.results if item.check_id == "p4.thermal")
    assert result.status == qe.STATUS_BLOCKED
    assert "powermetrics" in result.detail


def test_thermal_check_passes_with_readings_under_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qe, "thermal_snapshot",
        lambda: {"available": True, "source": "pmset -g therm", "readings": {"cpu_speed_limit": 90}},
    )
    monkeypatch.setattr(qe, "_model_probe", lambda command, runs, timeout, out, sampler=None: (True, "ok", {}))
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder, fingerprint=_fingerprint(24 * qe.GIB), pressure_gb=0.0, model_command="probe",
        model_runs=1, profile_out=None, timeout=5.0,
    )
    result = next(item for item in recorder.results if item.check_id == "p4.thermal")
    assert result.status == qe.STATUS_PASS
    assert "throttling observed" in result.detail


def test_coding_cases_check_skips_without_a_file_or_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "thermal_snapshot", lambda: {"available": True, "source": "s", "readings": {}})
    cases = [{"id": "a", "category": "c", "prompt": ""}]
    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder, fingerprint=_fingerprint(24 * qe.GIB), pressure_gb=0.0, model_command="",
        model_runs=1, profile_out=None, timeout=5.0, coding_cases=cases,
    )
    result = next(item for item in recorder.results if item.check_id == "p4.coding_cases")
    assert result.status == qe.STATUS_SKIP
    assert "--model-command" in result.detail

    recorder = qe.Recorder()
    qe.hardware_suite(
        recorder, fingerprint=_fingerprint(24 * qe.GIB), pressure_gb=0.0, model_command="probe",
        model_runs=1, profile_out=None, timeout=5.0,
    )
    result = next(item for item in recorder.results if item.check_id == "p4.coding_cases")
    assert result.status == qe.STATUS_SKIP
    assert "--coding-cases" in result.detail


def test_malformed_coding_cases_abort_the_run_without_a_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"cases": [{"id": "a", "category": "c"}, {"id": "a", "category": "c"}]}', encoding="utf-8")
    exit_code = qe.main(["--hardware", "--coding-cases", str(bad), "--model-command", "true"])
    assert exit_code == 2
    assert "duplicate case id" in capsys.readouterr().err


def test_dry_run_mentions_coding_cases_and_thermal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"id": "a", "category": "c"}]), encoding="utf-8")
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("", ""))
    assert qe.main(["--hardware", "--dry-run", "--coding-cases", str(cases), "--case-runs", "2"]) == 0
    out = capsys.readouterr().out
    assert "coding cases" in out
    assert "STUDIO_QUALIFY_CASE_ID" in out
    assert "thermal readings" in out


# ---------------------------------------------------------------------------
# Binding the evidence to what was measured (M5-P3-01 task 4)
# ---------------------------------------------------------------------------


def test_runtime_version_reads_the_runtime_version_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qe,
        "run_command",
        lambda argv, **kwargs: _FakeCompleted(0, "\nDocker version 27.3.1, build abcdef0\n"),
    )
    assert qe.runtime_version("/usr/bin/docker") == "Docker version 27.3.1, build abcdef0"


def test_runtime_version_is_empty_rather_than_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    assert qe.runtime_version("") == ""
    monkeypatch.setattr(qe, "run_command", lambda argv, **kwargs: _FakeCompleted(1, "", "boom"))
    assert qe.runtime_version("/usr/bin/docker") == ""


def test_parse_image_identity_extracts_the_content_id_and_digest() -> None:
    identity = qe.parse_image_identity(_FAKE_IMAGE_INSPECT)
    assert identity["image_id"] == "sha256:9f1c2b7d5e0a4c3f8b6d1e2a7c0b5f9d4e8a3c6b1f0d7e2a5c8b4f3e0d9a2c7b"
    assert identity["image_digest"].startswith("python@sha256:")
    assert identity["image_tags"] == ["python:3.12-slim"]


@pytest.mark.parametrize("stdout", ["", "not json", "[]", '"a string"', "{}"])
def test_parse_image_identity_yields_nothing_rather_than_a_guess(stdout: str) -> None:
    """A tag alone is not an identity: absent is reported, never fabricated."""
    identity = qe.parse_image_identity(stdout)
    assert identity.get("image_id", "") == ""


def test_sandbox_config_fingerprint_reads_the_product_argv() -> None:
    config = qe.sandbox_config_fingerprint(_FakeStudio())
    assert config["bind_mounts"] == 1
    assert config["privileged"] is False
    assert "--network" in config["hardening_flags"]
    assert "--read-only" in config["hardening_flags"]
    assert "--privileged" not in config["hardening_flags"]
    # The limits keep their values: --memory=2g and --memory=8g differ.
    assert config["limits"]["--memory"] == "2g"
    assert config["limits"]["--cpus"] == "2"


def test_sandbox_config_fingerprint_is_empty_without_the_product(monkeypatch: pytest.MonkeyPatch) -> None:
    assert qe.sandbox_config_fingerprint(None) == {}
    assert qe.sandbox_config_fingerprint(object()) == {}


def test_sandbox_suite_binds_evidence_to_the_runtime_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = qe.Recorder()
    monkeypatch.setattr(qe, "run_command", _make_fake_run({}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    identity = qe.sandbox_suite(
        recorder, runtime="/usr/bin/docker", runtime_name="docker", image="python:3.12-slim", timeout=5.0
    )
    bind = next(result for result in recorder.results if result.check_id == "p3.environment.bind")
    assert bind.status == qe.STATUS_PASS, bind.detail
    assert identity["runtime_version"] == "Docker version 27.0.0"
    assert identity["image_id"].startswith("sha256:")
    assert identity["config"]["bind_mounts"] == 1
    # The binding is real evidence, not a sentence: the values travel with it.
    assert bind.evidence["image_digest"].startswith("python@sha256:")
    assert bind.evidence["runtime_version"] == "Docker version 27.0.0"


def test_sandbox_suite_blocks_when_the_image_identity_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable identity is a readability gap (blocked), never a failed guarantee."""
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"image_inspect_stdout": ""})
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.image.local"] == qe.STATUS_PASS
    assert statuses["p3.environment.bind"] == qe.STATUS_BLOCKED
    assert recorder.verdict(qe.STORY_P3) == "blocked"
    assert "image id" in next(
        result.detail for result in recorder.results if result.check_id == "p3.environment.bind"
    )


def test_sandbox_suite_bind_is_skipped_without_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, runtime_available=False)
    bind = next(result for result in recorder.results if result.check_id == "p3.environment.bind")
    assert bind.status == qe.STATUS_SKIP
    assert "no reachable container runtime" in bind.detail


def test_build_record_carries_the_sandbox_identity_into_json_and_markdown() -> None:
    recorder = qe.Recorder()
    recorder.record("p3.runtime.available", qe.STORY_P3, "t", qe.STATUS_PASS)
    identity = {
        "runtime": "docker",
        "runtime_path": "/usr/bin/docker",
        "image": "python:3.12-slim",
        "runtime_version": "Docker version 27.3.1, build abcdef0",
        "image_id": "sha256:9f1c",
        "image_digest": "python@sha256:1a2b",
        "config": {"hardening_flags": ["--read-only"], "limits": {"--memory": "2g"},
                   "bind_mounts": 1, "privileged": False},
    }
    record = qe.build_record(
        recorder,
        fingerprint=_fingerprint(16 * qe.GIB),
        suites=[qe.STORY_P3],
        runtime_name="docker",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        invocation=["qualify_environment.py", "--sandbox"],
        sandbox_identity=identity,
    )
    assert record["sandbox"]["runtime_version"] == "Docker version 27.3.1, build abcdef0"
    assert record["sandbox"]["image_digest"] == "python@sha256:1a2b"
    assert record["sandbox"]["runtime"] == "docker"
    markdown = qe.render_markdown(record)
    assert "sha256:9f1c" in markdown
    assert "python@sha256:1a2b" in markdown
    assert "Docker version 27.3.1" in markdown
    assert "1 bind mount(s)" in markdown


def test_build_record_without_an_identity_says_so() -> None:
    recorder = qe.Recorder()
    record = qe.build_record(
        recorder,
        fingerprint=_fingerprint(16 * qe.GIB),
        suites=[qe.STORY_P3],
        runtime_name="",
        runtime_path="",
        image="python:3.12-slim",
        invocation=["qualify_environment.py", "--sandbox"],
        sandbox_identity={},
    )
    # Nothing is invented when the suite never reached a runtime.
    assert "runtime_version" not in record["sandbox"]
    assert "image id: not recorded" in qe.render_markdown(record)


def test_dry_run_announces_the_binding_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("", ""))
    assert qe.main(["--sandbox", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "bind the record to what ran" in out
