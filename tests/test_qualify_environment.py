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
        code = str(argv[-1])
        if "isolation_smoke.py" in joined:
            return _FakeCompleted(behaviour.get("smoke_exit", 0), "isolation_status: qualified\n")
        if "positive-ok" in code:
            if behaviour.get("positive_fails"):
                return _FakeCompleted(1, "")
            target = Path(argv[argv.index("--mount") + 1].split("source=")[1].split(",")[0])
            (target / "qualification-positive.txt").write_text("ok", encoding="utf-8")
            return _FakeCompleted(0, "positive-ok\n")
        if "denials:" in code:
            leaked = behaviour.get("leaked", "none")
            return _FakeCompleted(0 if leaked == "none" else 3, f"denials:{leaked}\n")
        if "bytearray" in code:
            return _FakeCompleted(137, "", "Killed")
        if "spawned" in code:
            return _FakeCompleted(0, "spawned 42\n")
        if "ps" in argv:
            return _FakeCompleted(0, behaviour.get("ps_output", ""))
        if "image" in argv and "inspect" in argv:
            return _FakeCompleted(behaviour.get("image_exit", 0), "")
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
        return qe.container_argv(self.STUDIO_SANDBOX_RUNTIME, self.STUDIO_SANDBOX_IMAGE, root, command)

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


def test_sandbox_suite_fails_when_an_escape_is_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"leaked": "network"})
    assert recorder.verdict(qe.STORY_P3) == "unqualified"
    failed = {result.check_id: result.detail for result in recorder.failures(qe.STORY_P3)}
    assert "p3.adversarial.escape_matrix" in failed
    assert "network" in failed["p3.adversarial.escape_matrix"]


def test_sandbox_suite_fails_when_the_positive_path_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"positive_fails": True})
    assert "p3.positive.execute" in {result.check_id for result in recorder.failures(qe.STORY_P3)}


def test_sandbox_suite_blocks_image_pull_expectations(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _run_sandbox_suite(monkeypatch, behaviour={"image_exit": 1})
    statuses = {result.check_id: result.status for result in recorder.results}
    assert statuses["p3.image.local"] == qe.STATUS_FAIL
    assert statuses["p3.positive.execute"] == qe.STATUS_SKIP


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
        lambda gigabytes, timeout: (True, "measured", {"requested_gib": gigabytes}),
    )
    monkeypatch.setattr(
        qe, "_model_probe",
        lambda command, runs, timeout, out: (True, "1/1 runs exited 0", {"runs": []}),
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
    monkeypatch.setattr(qe, "_pressure_probe", lambda gb, timeout: (True, "measured", {}))
    monkeypatch.setattr(qe, "_model_probe", lambda command, runs, timeout, out: (True, "ok", {}))
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


def test_a_runtime_without_the_image_is_unqualified_never_qualified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host that has Docker but not the image must not produce a pass."""
    monkeypatch.setattr(qe, "run_command", _make_fake_run({"image_exit": 1}))
    monkeypatch.setattr(qe, "_product_sandbox_module", lambda: _FakeStudio())
    monkeypatch.setattr(qe, "detect_runtime", lambda preferred=None: ("/usr/bin/docker", "docker"))
    exit_code = qe.main(["--sandbox", "--quiet", "--json", str(tmp_path / "r.json")])
    assert exit_code == 1
    record = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert record["verdicts"][qe.STORY_P3]["verdict"] == "unqualified"
    assert record["claims"]["container_isolation"] == "unqualified"


def test_asyncio_coroutine_path_is_used_for_product_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the fake studio's async entry point matches what the suite awaits."""
    studio = _FakeStudio()
    with pytest.raises(PermissionError):
        asyncio.run(studio._execute_limited(Path("/tmp"), "print(1)", timeout=1, max_output_bytes=16))
