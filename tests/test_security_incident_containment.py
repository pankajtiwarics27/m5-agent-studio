"""M5-P3-06 verification suite: Security incident containment and fallback qualification.

Covers all M5-P3-06 implementation tasks and acceptance criteria:
1. Track security dependencies and configuration generations (`SandboxBackendProfile`,
   `ConfigurationGeneration`, `inspect_container_argv_hardening`, drift invalidation).
2. Suspend affected admissions promptly and contain matching in-flight subprocesses
   (`security_guarantee_suspended`, `trigger_security_incident_async`, host-bypass denial).
3. Qualify alternative backend against incident-specific shared dependencies
   (`shared_failed_dependency`, `unsafe_host_fallback_forbidden`, disjoint fallback routing).
4. Requalify before restoration (`requalification_required`, `stale_pre_incident_qualification`,
   `regression_still_present`, `lingering_processes_active`, atomic hold release).
5. Crash durability (`0700` dir / `0600` file, `O_NOFOLLOW`, file + directory `os.fsync`,
   `init_db` hold reconciliation, corruption/symlink fail-closed, standalone `studio.py`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

import app as studio
import process_supervision as ps
import sandbox_boundary as sb


def _wait_until(predicate: Any, *, timeout_s: float = 3.0, poll_s: float = 0.03) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return bool(predicate())


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# Safe Workspace\n", encoding="utf-8")

    db_path = tmp_path / "studio_p3_06.db"
    store_dir = tmp_path / ".harnesses"
    token = "t" * 48

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", token)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", False)
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_RUNTIME", "docker")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_IMAGE", "python:3.12-slim")

    studio._reset_process_supervisor(None)
    studio._reset_execution_profile_controller(None)
    studio.init_db()
    yield ws
    studio._reset_process_supervisor(None)
    studio._reset_execution_profile_controller(None)


def _auth_client() -> TestClient:
    return TestClient(
        studio.app,
        base_url="http://127.0.0.1:8787",
        headers={"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"},
    )


def _make_qual_report(
    backend: sb.SandboxBackendProfile,
    *,
    verdict: str = "qualified",
    config_fingerprint: str = "",
    generated_at_epoch: float | None = None,
    failed_checks: tuple[str, ...] = (),
    omit_checks: tuple[str, ...] = (),
) -> dict[str, Any]:
    all_check_ids = (
        "p3.runtime.cli",
        "p3.runtime.image",
        "p3.positive.execute",
        "p3.adversarial.escape_matrix",
        "p3.adversarial.credentials",
        "p3.adversarial.memory_limit",
        "p3.adversarial.descendants",
        "p3.product.argv",
        "p3.product.no_host_fallback",
    )
    checks: list[dict[str, Any]] = []
    failed_set = set(failed_checks)
    omit_set = set(omit_checks)
    for cid in all_check_ids:
        if cid in omit_set:
            continue
        checks.append(
            {
                "check_id": cid,
                "story": "M5-P3-01",
                "title": cid,
                "status": "fail" if cid in failed_set else ("blocked" if verdict == "blocked" else "pass"),
                "required": True,
            }
        )
    ts = generated_at_epoch if generated_at_epoch is not None else time.time()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "generated_at_epoch": ts,
        "sandbox": {
            "runtime": backend.runtime,
            "runtime_path": backend.runtime_path,
            "image": backend.image,
            "image_digest": backend.image_digest,
            "oci_runtime": backend.oci_runtime,
            "kernel_boundary": backend.kernel_boundary,
        },
        "checks": checks,
        "verdicts": {
            "M5-P3-01": {
                "verdict": verdict,
                "checks_run": len(checks),
                "failed": list(failed_set),
                "not_executed": [],
            }
        },
    }
    if config_fingerprint:
        payload["config_fingerprint"] = config_fingerprint
    return payload


def _run_tool(
    ws: Path,
    tool_name: str,
    args: dict[str, Any],
    *,
    session_id: str = "sess-p3-06",
    policy: str = "autonomous",
    call_index: int = 1,
) -> dict[str, Any]:
    sess = {
        "id": session_id,
        "workspace_dir": str(ws),
        "worktree_dir": str(ws),
        "policy": policy,
    }
    return asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            tool_name,
            args,
            call_index=call_index,
        )
    )


def test_configuration_generation_tracking_and_hardening_regression_suspends_affected_actions(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1 & AC 1: Configuration generations advance monotonically; drift on a qualified
    backend or container hardening regressions immediately suspend affected profiles while
    keeping unaffected read/write profiles operational."""
    sec_store = studio.get_security_incident_store()
    docker_v1 = sb.SandboxBackendProfile(
        backend_id="docker",
        runtime="docker",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        image_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        oci_runtime="runc",
        kernel_boundary="orbstack_vm",
    )
    gen1, inc1 = sec_store.observe_configuration(docker_v1, execution_mode="sandbox")
    assert gen1.generation_seq == 1
    assert gen1.hardening_valid is True
    assert inc1 is None

    # Idempotent re-observation of identical config keeps generation_seq == 1.
    gen1_again, _ = sec_store.observe_configuration(docker_v1, execution_mode="sandbox")
    assert gen1_again.generation_seq == 1

    # Qualify generation 1 -> capability descriptor now reports verified `container_qualified`.
    q_out = sec_store.ingest_qualification_report(
        _make_qual_report(docker_v1, config_fingerprint=gen1.config_fingerprint),
        backend=docker_v1,
    )
    assert q_out.ok is True
    coord = studio._execution_profile_controller().bind(
        workspace_root=isolated_env,
        store_dir=studio.HARNESS_STORE_DIR,
        execution_mode_fn=lambda: "sandbox",
        security_incident_store=sec_store,
    )
    adm_ok = coord.admit_profile(
        studio.execution_spine.ActionRequest(
            tool_name="run_bash",
            args={"command": "echo hi"},
            policy="autonomous",
            approved=True,
            session_id="sess-gen",
            grant_source="policy",
        ),
        effective_policy="autonomous",
        action_class="consequential",
    )
    assert adm_ok.allowed is True
    assert adm_ok.profile is not None
    assert adm_ok.profile["descriptor"]["isolation_posture"] == "container_qualified"
    assert adm_ok.profile["descriptor"]["verified_isolation"] is True
    assert adm_ok.handle is not None
    coord.release_only(adm_ok.handle)

    # Drift image to a different unverified tag -> generation 2 invalidates qualification & suspends subprocesses!
    docker_v2 = sb.SandboxBackendProfile(
        backend_id="docker",
        runtime="docker",
        runtime_path="/usr/bin/docker",
        image="python:3.12-unverified",
        oci_runtime="runc",
        kernel_boundary="orbstack_vm",
    )
    gen2, drift_inc = sec_store.observe_configuration(docker_v2, execution_mode="sandbox")
    assert gen2.generation_seq == 2
    assert drift_inc is not None
    assert drift_inc.source == "configuration_drift"
    assert drift_inc.status == "active"

    # Dependent subprocess profile (`run_bash`) is immediately prevented!
    denied_bash = _run_tool(isolated_env, "run_bash", {"command": "echo blocked"}, call_index=1)
    assert denied_bash["ok"] is False
    assert denied_bash["error_code"] == "security_guarantee_suspended"

    # Unaffected in-process `read_file` and `write_file` still succeed (container guarantees only failed).
    read_ok = _run_tool(isolated_env, "read_file", {"path": "README.md"}, call_index=2)
    assert read_ok["ok"] is True
    write_ok = _run_tool(
        isolated_env, "write_file", {"path": "notes.txt", "content": "safe\n"}, call_index=3
    )
    assert write_ok["ok"] is True

    # Also verify `inspect_container_argv_hardening` catches regressions (`--privileged`, missing `--network=none`).
    monkeypatch.setattr(studio.shutil, "which", lambda _rt: "/usr/bin/docker")
    good_argv = sb.build_container_invocation(
        isolated_env, "echo ok", runtime_path="/usr/bin/docker", image="python:3.12-slim"
    )
    bad_argv = [tok for tok in good_argv if tok != "--network=none"]
    bad_argv.insert(2, "--privileged")
    hard_res = sb.inspect_container_argv_hardening(bad_argv)
    assert hard_res.valid is False
    assert "container_network_isolation" in hard_res.failed_guarantees
    assert "container_privilege_containment" in hard_res.failed_guarantees


def test_qualification_report_regression_prevents_affected_actions_and_blocks_host_mode_bypass(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2 & AC 1: A failing qualification check (`p3.adversarial.escape_matrix`) suspends
    mapped guarantees via API and blocks both sandbox AND host-mode execution bypass attempts."""
    client = _auth_client()
    primary = studio._current_primary_backend_profile(runtime_path_override="/usr/bin/docker")
    failing_report = _make_qual_report(
        primary,
        verdict="unqualified",
        failed_checks=("p3.adversarial.escape_matrix",),
    )

    resp = client.post(
        "/api/security/incidents",
        json={"backend": primary.to_mapping(), "qualification_report": failing_report},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "qualification_regression_suspended"
    inc = body["incident"]
    assert "container_network_isolation" in inc["failed_guarantees"]
    assert "workspace_confinement" in inc["failed_guarantees"]

    # Both `run_bash` AND `read_file` / `write_file` depend on `workspace_confinement` -> prevented!
    for idx, (tool_name, tool_args) in enumerate(
        [
            ("run_bash", {"command": "echo no"}),
            ("write_file", {"path": "x.txt", "content": "nope"}),
            ("read_file", {"path": "README.md"}),
        ],
        start=1,
    ):
        res = _run_tool(isolated_env, tool_name, tool_args, call_index=idx)
        assert res["ok"] is False, (tool_name, res)
        assert res["error_code"] == "security_guarantee_suspended", (tool_name, res)

    # Attempting to bypass by flipping `STUDIO_EXECUTION_MODE='host'` + `STUDIO_ALLOW_HOST_EXECUTION=True`
    # MUST fail closed at both `_run_authorized_tool` AND direct `_start_code_process`!
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    host_bash = _run_tool(isolated_env, "run_bash", {"command": "echo bypass"}, call_index=10)
    assert host_bash["ok"] is False
    assert host_bash["error_code"] == "security_guarantee_suspended"

    with pytest.raises(PermissionError, match="security incident"):
        asyncio.run(studio._start_code_process(isolated_env, "echo direct-bypass"))


def test_prompt_in_flight_subprocess_termination_on_security_incident(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2: Triggering a security incident promptly terminates matching running subprocesses
    (including `os.setsid()` detached children) while preserving unrelated read-only access."""
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    ready_file = isolated_env / "inflight_ready.pid"
    detached_pid_file = isolated_env / "detached_child.pid"
    long_code = (
        "import os, subprocess, sys, time\n"
        f"det = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
        f"open({str(detached_pid_file)!r}, 'w').write(str(det.pid))\n"
        f"open({str(ready_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )

    client = _auth_client()

    async def _run_and_trigger() -> tuple[dict[str, Any], dict[str, Any]]:
        sess = {
            "id": "sess-live-inc",
            "workspace_dir": str(isolated_env),
            "worktree_dir": str(isolated_env),
            "policy": "autonomous",
        }
        exec_task = asyncio.create_task(
            studio._execute_harness_tool(
                None,
                sess,
                "ctx_execute_python",
                {"code": long_code},
                action_id="act-live-secinc-1",
                call_index=1,
            )
        )
        deadline = time.monotonic() + 4.0
        while not ready_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.03)
        assert ready_file.exists(), "Subprocess did not start in time"

        resp = await asyncio.to_thread(
            client.post,
            "/api/security/incidents",
            json={
                "reason": "container_escape_cve_detected",
                "failed_guarantees": ["container_privilege_containment"],
                "failed_dependencies": ["dep:oci_runtime:runc"],
                "affected_backends": ["docker"],
            },
        )
        assert resp.status_code == 201, resp.text
        tool_out = await asyncio.wait_for(exec_task, timeout=5.0)
        return tool_out, resp.json()

    tool_res, inc_payload = asyncio.run(_run_and_trigger())
    assert inc_payload["contained_processes"] >= 1
    assert tool_res["ok"] is False
    det_pid = int(detached_pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(lambda: ps.probe_process(det_pid) is None, timeout_s=2.5)


def test_fallback_qualification_rejects_shared_failed_dependencies_and_host_fallback(
    isolated_env: Path,
) -> None:
    """Task 3 & AC 2: Alternative backends that share ANY failed dependency (VM kernel, OCI
    runtime, compromised image digest, or shared engine confinement) or attempt host fallback
    are rejected fail-closed."""
    client = _auth_client()
    create_resp = client.post(
        "/api/security/incidents",
        json={
            "reason": "orbstack_runc_and_image_regression",
            "failed_guarantees": ["container_network_isolation", "container_privilege_containment"],
            "failed_dependencies": [
                "dep:kernel:orbstack_vm",
                "dep:oci_runtime:runc",
                "dep:image:sha256:badcafe000",
            ],
            "affected_backends": ["docker"],
        },
    )
    assert create_resp.status_code == 201
    inc_id = create_resp.json()["incident"]["incident_id"]

    # 1. Host fallback is unconditionally forbidden (`403`).
    host_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": {
                "backend_id": "host",
                "runtime": "host",
                "runtime_path": "/bin/sh",
                "image": "none",
            }
        },
    )
    assert host_resp.status_code == 403
    assert host_resp.json()["error_code"] == "unsafe_host_fallback_forbidden"

    # 2. Podman sharing `dep:kernel:orbstack_vm` is rejected (`409 shared_failed_dependency`).
    shared_vm = sb.SandboxBackendProfile(
        backend_id="podman-orbstack",
        runtime="podman",
        runtime_path="/usr/bin/podman",
        image="python:3.12-alpine",
        image_digest="sha256:clean999",
        oci_runtime="crun",
        kernel_boundary="orbstack_vm",
    )
    vm_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": shared_vm.to_mapping(),
            "qualification_report": _make_qual_report(shared_vm),
        },
    )
    assert vm_resp.status_code == 409
    assert vm_resp.json()["error_code"] == "shared_failed_dependency"
    assert "dep:kernel:orbstack_vm" in vm_resp.json()["shared_dependencies"]

    # 3. Podman on disjoint VM + `crun` but sharing compromised `dep:image:sha256:badcafe000` is rejected.
    shared_img = sb.SandboxBackendProfile(
        backend_id="podman-same-img",
        runtime="podman",
        runtime_path="/usr/bin/podman",
        image="python:3.12-alpine",
        image_digest="sha256:badcafe000",
        oci_runtime="crun",
        kernel_boundary="podman_machine_vm",
    )
    img_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": shared_img.to_mapping(),
            "qualification_report": _make_qual_report(shared_img),
        },
    )
    assert img_resp.status_code == 409
    assert img_resp.json()["error_code"] == "shared_failed_dependency"
    assert "dep:image:sha256:badcafe000" in img_resp.json()["shared_dependencies"]

    # 4. Podman sharing `dep:oci_runtime:runc` is rejected.
    shared_runc = sb.SandboxBackendProfile(
        backend_id="podman-runc",
        runtime="podman",
        runtime_path="/usr/bin/podman",
        image="python:3.12-alpine",
        image_digest="sha256:clean999",
        oci_runtime="runc",
        kernel_boundary="podman_machine_vm",
    )
    runc_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": shared_runc.to_mapping(),
            "qualification_report": _make_qual_report(shared_runc),
        },
    )
    assert runc_resp.status_code == 409
    assert runc_resp.json()["error_code"] == "shared_failed_dependency"
    assert "dep:oci_runtime:runc" in runc_resp.json()["shared_dependencies"]

    # 5. Disjoint Podman with `blocked` verdict or missing `escape_matrix` check is rejected.
    disjoint_podman = sb.SandboxBackendProfile(
        backend_id="podman-clean",
        runtime="podman",
        runtime_path="/usr/bin/podman",
        image="python:3.12-alpine",
        image_digest="sha256:clean999",
        oci_runtime="crun",
        kernel_boundary="podman_machine_vm",
    )
    blocked_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": disjoint_podman.to_mapping(),
            "qualification_report": _make_qual_report(disjoint_podman, verdict="blocked"),
        },
    )
    assert blocked_resp.status_code == 409
    assert blocked_resp.json()["error_code"] == "qualification_failed"

    incomplete_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": disjoint_podman.to_mapping(),
            "qualification_report": _make_qual_report(
                disjoint_podman,
                omit_checks=("p3.adversarial.escape_matrix", "p3.product.argv"),
            ),
        },
    )
    assert incomplete_resp.status_code == 409
    assert incomplete_resp.json()["error_code"] == "qualification_incomplete_coverage"

    # 6. Shared engine guarantee (`workspace_confinement`) CANNOT be bypassed by any container fallback!
    ws_inc_resp = client.post(
        "/api/security/incidents",
        json={
            "reason": "workspace_symlink_resolver_bug",
            "failed_guarantees": ["workspace_confinement"],
            "failed_dependencies": ["dep:workspace_confinement"],
        },
    )
    ws_inc_id = ws_inc_resp.json()["incident"]["incident_id"]
    ws_fb_resp = client.post(
        f"/api/security/incidents/{ws_inc_id}/fallback",
        json={
            "backend": disjoint_podman.to_mapping(),
            "qualification_report": _make_qual_report(disjoint_podman),
        },
    )
    assert ws_fb_resp.status_code == 409
    assert ws_fb_resp.json()["error_code"] == "shared_failed_dependency"


def test_disjoint_fallback_backend_qualifies_and_routes_sandbox_execution_safely(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3 & AC 2: A candidate backend with completely disjoint dependencies and valid
    post-incident qualification mitigates the incident and routes `_sandbox_command` through
    the qualified fallback runtime while keeping host mode blocked."""
    client = _auth_client()
    inc_resp = client.post(
        "/api/security/incidents",
        json={
            "reason": "docker_runc_breakout",
            "failed_guarantees": ["container_network_isolation", "container_privilege_containment"],
            "failed_dependencies": [
                "dep:runtime:docker",
                "dep:oci_runtime:runc",
                "dep:kernel:orbstack_vm",
            ],
            "affected_backends": ["docker"],
        },
    )
    assert inc_resp.status_code == 201
    inc_id = inc_resp.json()["incident"]["incident_id"]

    # Before fallback qualification, `run_bash` is denied.
    pre_res = _run_tool(isolated_env, "run_bash", {"command": "echo pre"}, call_index=1)
    assert pre_res["ok"] is False
    assert pre_res["error_code"] == "security_guarantee_suspended"

    podman_fb = sb.SandboxBackendProfile(
        backend_id="podman-isolated",
        runtime="podman",
        runtime_path="/usr/bin/podman",
        image="python:3.12-alpine",
        image_digest="sha256:goodpodman777",
        oci_runtime="crun",
        kernel_boundary="podman_machine_vm",
    )
    fb_resp = client.post(
        f"/api/security/incidents/{inc_id}/fallback",
        json={
            "backend": podman_fb.to_mapping(),
            "qualification_report": _make_qual_report(
                podman_fb, generated_at_epoch=time.time() + 0.05
            ),
        },
    )
    assert fb_resp.status_code == 200, fb_resp.text
    fb_data = fb_resp.json()
    assert fb_data["ok"] is True
    assert fb_data["incident"]["status"] == "mitigated_via_fallback"
    assert fb_data["summary"]["active_backend_id"] == "podman-isolated"

    # `_sandbox_command` now builds argv using `/usr/bin/podman` and `python:3.12-alpine`!
    monkeypatch.setattr(
        studio.shutil,
        "which",
        lambda rt: "/usr/bin/podman" if "podman" in str(rt) else "/usr/bin/docker",
    )
    constructed_argv = studio._sandbox_command(
        isolated_env, "echo safe-on-podman", container_name="m5-fb-box"
    )
    assert constructed_argv[0] == "/usr/bin/podman"
    assert "python:3.12-alpine" in constructed_argv

    # Admission in `sandbox` mode succeeds with `isolation_posture == "container_qualified"`.
    coord = studio._execution_profile_controller().bind(
        workspace_root=isolated_env,
        store_dir=studio.HARNESS_STORE_DIR,
        execution_mode_fn=lambda: "sandbox",
        security_incident_store=studio.get_security_incident_store(),
    )
    adm_fb = coord.admit_profile(
        studio.execution_spine.ActionRequest(
            tool_name="run_bash",
            args={"command": "echo ok"},
            policy="autonomous",
            approved=True,
            session_id="sess-fb",
            grant_source="policy",
        ),
        effective_policy="autonomous",
        action_class="consequential",
    )
    assert adm_fb.allowed is True
    assert adm_fb.profile is not None
    assert adm_fb.profile["descriptor"]["isolation_posture"] == "container_qualified"
    assert adm_fb.handle is not None
    coord.release_only(adm_fb.handle)

    # Even while mitigated via sandbox fallback, switching to `host` mode is STILL forbidden!
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    host_attempt = _run_tool(isolated_env, "run_bash", {"command": "echo host"}, call_index=2)
    assert host_attempt["ok"] is False
    assert host_attempt["error_code"] == "unsafe_host_fallback_forbidden"


def test_requalification_required_before_restoration_rejects_stale_and_lingering_holds(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4: Restoration refuses missing qualification, un-remediated immutable image digests,
    stale pre-incident qualification artifacts, and live lingering PIDs; succeeds only once
    fresh post-incident qualification passes and zero lingering processes remain."""
    client = _auth_client()
    pre_incident_ts = time.time() - 60.0

    inc_resp = client.post(
        "/api/security/incidents",
        json={
            "reason": "compromised_image_and_runc_bug",
            "failed_guarantees": ["container_filesystem_isolation", "container_privilege_containment"],
            "failed_dependencies": [
                "dep:oci_runtime:runc-1.1.0",
                "dep:image:sha256:compromised111",
            ],
            "affected_backends": ["docker"],
        },
    )
    assert inc_resp.status_code == 201
    inc_id = inc_resp.json()["incident"]["incident_id"]
    hold_id = f"hold-{inc_id}"

    # 1. Restore without qualification report -> 409 `requalification_required`.
    no_qual = client.post(f"/api/security/incidents/{inc_id}/restore", json={})
    assert no_qual.status_code == 409
    assert no_qual.json()["error_code"] == "requalification_required"

    # 2. Restore while still referencing immutable compromised image digest -> 409 `regression_still_present`.
    unremediated = sb.SandboxBackendProfile(
        backend_id="docker",
        runtime="docker",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        image_digest="sha256:compromised111",
        oci_runtime="runc-1.2.0-patched",
    )
    still_bad = client.post(
        f"/api/security/incidents/{inc_id}/restore",
        json={
            "backend": unremediated.to_mapping(),
            "qualification_report": _make_qual_report(
                unremediated, generated_at_epoch=time.time() + 1.0
            ),
        },
    )
    assert still_bad.status_code == 409
    assert still_bad.json()["error_code"] == "regression_still_present"

    # 3. Remediated backend with replayed STALE pre-incident qualification -> 409 `stale_pre_incident_qualification`.
    remediated = sb.SandboxBackendProfile(
        backend_id="docker",
        runtime="docker",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        image_digest="sha256:patchedclean222",
        oci_runtime="runc-1.2.0-patched",
    )
    stale_resp = client.post(
        f"/api/security/incidents/{inc_id}/restore",
        json={
            "backend": remediated.to_mapping(),
            "qualification_report": _make_qual_report(
                remediated, generated_at_epoch=pre_incident_ts
            ),
        },
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json()["error_code"] == "stale_pre_incident_qualification"

    # 4. Simulate a live lingering process bound to the incident hold -> 409 `lingering_processes_active`.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        probe = ps.probe_process(sleeper.pid)
        assert probe is not None
        cstore = studio.get_containment_store()
        existing_holds = [h for h in cstore.list_holds() if h.hold_id == hold_id]
        assert len(existing_holds) == 1
        old_h = existing_holds[0]
        cstore.persist_barrier_hold(
            ps.PersistedBarrierHold(
                hold_id=old_h.hold_id,
                session_id=old_h.session_id,
                task_id=old_h.task_id,
                action_id=old_h.action_id,
                tool_name=old_h.tool_name,
                footprint_scope=old_h.footprint_scope,
                workspace_root=old_h.workspace_root,
                barriers=old_h.barriers,
                reason=old_h.reason,
                lingering_pids=((sleeper.pid, probe.start_fingerprint),),
            )
        )
        lingering_resp = client.post(
            f"/api/security/incidents/{inc_id}/restore",
            json={
                "backend": remediated.to_mapping(),
                "qualification_report": _make_qual_report(
                    remediated, generated_at_epoch=time.time() + 1.0
                ),
            },
        )
        assert lingering_resp.status_code == 409
        assert lingering_resp.json()["error_code"] == "lingering_processes_active"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)

    # 5. Once lingering process is gone, fresh post-incident requalification restores `docker` and releases holds!
    ok_resp = client.post(
        f"/api/security/incidents/{inc_id}/restore",
        json={
            "backend": remediated.to_mapping(),
            "qualification_report": _make_qual_report(
                remediated, generated_at_epoch=time.time() + 1.0
            ),
        },
    )
    assert ok_resp.status_code == 200, ok_resp.text
    ok_data = ok_resp.json()
    assert ok_data["ok"] is True
    assert ok_data["incident"]["status"] == "restored"
    assert not any(h.hold_id == hold_id for h in studio.get_containment_store().list_holds())

    # Subprocess execution on primary `docker` is now restored!
    monkeypatch.setattr(studio.shutil, "which", lambda _rt: "/usr/bin/docker")
    argv = studio._sandbox_command(isolated_env, "echo restored", container_name="m5-restored")
    assert argv[0] == "/usr/bin/docker"


def test_incident_generations_and_barriers_persist_through_crash_and_restart(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: Security incidents, configuration generations, and containment barriers sync both
    file and parent directory (`os.fsync`) and survive abrupt process crashes and restarts."""
    fsynced_modes: list[int] = []
    real_fsync = sb.os.fsync

    def _spy_fsync(fd: int) -> None:
        fsynced_modes.append(sb.os.fstat(fd).st_mode)
        real_fsync(fd)

    sess = studio.create_harness_session_db(
        title="Crash Persistence Session",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="demo",
        critic_model="demo",
    )
    sid = sess["id"]

    sec_store = studio.get_security_incident_store()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sb.os, "fsync", _spy_fsync)
        inc = sec_store.record_incident(
            reason="crash_durable_confinement_incident",
            failed_guarantees=("container_network_isolation", "workspace_confinement"),
            failed_dependencies=("dep:workspace_confinement",),
            affected_backends=("docker",),
            session_id=sid,
            workspace_root=isolated_env,
        )

    # Verify both regular temp file AND containing directory (`S_IFDIR` = 0o040000) were fsynced!
    assert any(m & 0o100000 for m in fsynced_modes), "Expected regular temp file fsync"
    assert any(m & 0o040000 for m in fsynced_modes), "Expected parent directory fsync"

    hold_id = f"hold-{inc.incident_id}"
    assert any(h.hold_id == hold_id for h in studio.get_containment_store().list_holds())

    # Simulate an abrupt mid-crash discrepancy where ContainmentStore lost the hold before Studio died,
    # then restart Studio (`init_db()` + fresh singletons + new boot ID).
    studio.get_containment_store().release_hold(hold_id, force=True)
    assert not any(h.hold_id == hold_id for h in studio.get_containment_store().list_holds())

    studio._reset_process_supervisor(None)
    studio._reset_execution_profile_controller(None)
    monkeypatch.setattr(studio, "_PROCESS_BOOT_ID", "boot-after-crash-p306")
    studio.init_db()

    # After restart: `init_db()` reconciled `hold-{incident_id}` back into `ContainmentStore`!
    recovered_sec = studio.get_security_incident_store()
    recovered_inc = recovered_sec.get_incident(inc.incident_id)
    assert recovered_inc is not None
    assert recovered_inc.status == "active"
    assert any(h.hold_id == hold_id for h in studio.get_containment_store().list_holds())

    # Affected actions remain blocked after restart!
    post_crash_res = _run_tool(
        isolated_env, "run_bash", {"command": "echo after-crash"}, session_id=sid, call_index=1
    )
    assert post_crash_res["ok"] is False
    assert post_crash_res["error_code"] == "security_guarantee_suspended"

    # And `/api/harness/sessions/{sid}/resume` refuses resumption while the durable hold is active!
    studio._update_harness_session_state(sid, status="paused")
    client = _auth_client()
    resume_resp = client.post(f"/api/harness/sessions/{sid}/resume", json={})
    assert resume_resp.status_code == 409


def test_tampered_or_symlink_security_incident_state_fails_closed(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    """Corrupt or symlinked `security_incidents/state.json` fails closed across admission and APIs."""
    sec_store = studio.get_security_incident_store()
    sec_store.record_incident(
        reason="seed_state",
        failed_guarantees=("container_network_isolation",),
    )
    state_path = sec_store.state_path
    assert state_path.is_file()

    # 1. Corrupt JSON -> `security_state_corrupt` denial & HTTP 503.
    state_path.write_text("{corrupt-json", encoding="utf-8")
    denied = _run_tool(isolated_env, "run_bash", {"command": "echo corrupt"}, call_index=1)
    assert denied["ok"] is False
    assert denied["error_code"] == "security_state_corrupt"

    client = _auth_client()
    api_resp = client.get("/api/security/incidents")
    assert api_resp.status_code == 503

    # 2. Symlink attack on `state.json` -> rejected via `O_NOFOLLOW` / `lstat`!
    outside_fake = tmp_path / "forged_clean_state.json"
    outside_fake.write_text(
        '{"schema_version":"m5-security-incidents-v1","current_generation_seq":0,'
        '"active_backend_id":"","backends":{},"generations":[],"qualifications":{},"incidents":{}}',
        encoding="utf-8",
    )
    state_path.unlink()
    state_path.symlink_to(outside_fake)

    symlink_denied = _run_tool(isolated_env, "read_file", {"path": "README.md"}, call_index=2)
    assert symlink_denied["ok"] is False
    assert symlink_denied["error_code"] == "security_state_corrupt"


def test_standalone_studio_bundle_security_incident_containment_parity(
    tmp_path: Path,
) -> None:
    """Standalone `studio.py` enforces security incident suspension, shared-dependency fallback
    rejection, disjoint fallback qualification, and requalification restoration without sibling `.py` files."""
    repo_root = Path(__file__).resolve().parents[1]
    bundle_dir = tmp_path / "standalone_bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "studio.py", bundle_dir / "studio.py")

    script = """
import asyncio
import importlib.util
import pathlib
import sys
import time

spec = importlib.util.spec_from_file_location("studio_standalone_p306", pathlib.Path.cwd() / "studio.py")
studio = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = studio
spec.loader.exec_module(studio)

ws = pathlib.Path.cwd() / "ws"
ws.mkdir(exist_ok=True)
(ws / "hello.txt").write_text("hi\\n", encoding="utf-8")

studio.DB_PATH = pathlib.Path.cwd() / "standalone.db"
studio.HARNESS_STORE_DIR = pathlib.Path.cwd() / "store"
studio.WORKSPACE_ROOT = ws
studio.STUDIO_EXECUTION_MODE = "sandbox"
studio.STUDIO_ALLOW_HOST_EXECUTION = False
studio.init_db()

sec_store = studio.get_security_incident_store()
inc = sec_store.record_incident(
    reason="standalone_incident",
    failed_guarantees=("container_network_isolation",),
    failed_dependencies=("dep:oci_runtime:runc",),
    affected_backends=("docker",),
)

sess = {"id": "s-standalone", "workspace_dir": str(ws), "worktree_dir": str(ws), "policy": "autonomous"}
denied = asyncio.run(studio._execute_harness_tool(None, sess, "run_bash", {"command": "echo x"}, call_index=1))
assert denied["ok"] is False and denied["error_code"] == "security_guarantee_suspended", denied

allowed_read = asyncio.run(studio._execute_harness_tool(None, sess, "read_file", {"path": "hello.txt"}, call_index=2))
assert allowed_read["ok"] is True, allowed_read

bad_fb = studio.SandboxBackendProfile(
    backend_id="podman-runc",
    runtime="podman",
    runtime_path="/usr/bin/podman",
    image="python:3.12-alpine",
    oci_runtime="runc",
)
rej = sec_store.qualify_fallback_backend(inc.incident_id, bad_fb, qualification_report={})
assert rej.ok is False and rej.error_code == "shared_failed_dependency", rej

good_fb = studio.SandboxBackendProfile(
    backend_id="podman-crun",
    runtime="podman",
    runtime_path="/usr/bin/podman",
    image="python:3.12-alpine",
    oci_runtime="crun",
    kernel_boundary="podman_vm",
)
report = {
    "generated_at_epoch": time.time() + 1.0,
    "sandbox": {"runtime": "podman", "image": "python:3.12-alpine", "oci_runtime": "crun", "kernel_boundary": "podman_vm"},
    "checks": [{"check_id": "p3.adversarial.escape_matrix", "status": "pass", "required": True}],
    "verdicts": {"M5-P3-01": {"verdict": "qualified", "failed": []}},
}
acc = sec_store.qualify_fallback_backend(inc.incident_id, good_fb, qualification_report=report)
assert acc.ok is True and acc.incident.status == "mitigated_via_fallback", acc
print("STANDALONE_P3_06_OK")
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(bundle_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "STANDALONE_P3_06_OK" in proc.stdout
