"""M5-P1-05 tests: Durable task state machine, safe boundaries, checkpoints, and budgets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import pytest

import execution_budget
import studio
import workflow_checkpoints
import workflow_state


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "m5_p1_05.sqlite3"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir(parents=True, exist_ok=True)
    harness_store = tmp_path / "harness_store"
    harness_store.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws_root)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_store)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "t" * 40)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(ws_root), create_if_missing=True)
    return ws_root


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}


def _create_session(ws_root: Path, *, policy: str = "autonomous", title: str = "P1-05 Task") -> dict[str, Any]:
    return studio.create_harness_session_db(
        title=title,
        workspace_dir=str(ws_root),
        use_worktree=False,
        policy=policy,
        lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
        critic_model="Gemma-4-21B-REAP-4bit",
    )


def test_root_task_and_milestone_states_and_transitions_persist_across_reopen(
    isolated_env: Path,
) -> None:
    """Task 1: Root task & child milestone states, versions, and transitions persist in SQLite."""
    sm = studio.get_task_state_machine()
    coord = studio.get_worker_lease_coordinator(isolated_env)
    sess = _create_session(isolated_env, title="Root + Milestones")
    sid = sess["id"]

    root, budget = sm.ensure_root_task(
        sid,
        session_id=sid,
        title="Root + Milestones",
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    assert root.state == "pending"
    assert budget.spec.max_recoveries == 3

    ms1 = sm.ensure_milestone(sid, "plan", title="Plan Milestone")
    ms2 = sm.ensure_milestone(sid, "implement", title="Implement Milestone")
    assert ms1.parent_task_id == sid
    assert ms2.parent_task_id == sid
    assert [m.state for m in sm.list_milestones(sid)] == ["pending", "pending"]

    # Illegal direct transition to 'completed' from 'pending' is rejected.
    with pytest.raises(studio.WorkflowTransitionRejected):
        sm.complete_task(sid)

    claim = coord.acquire_or_takeover(sid, f"wrk-{studio._PROCESS_BOOT_ID}-m1", lease_ttl_s=60.0)
    ep = claim.epoch_record.epoch

    ms1_run = sm.transition_milestone(
        sid,
        "plan",
        "running",
        epoch=ep,
        expected_state_version=ms1.state_version,
        reason="start_plan",
    )
    # Stale state_version on milestone fails closed without mutating state.
    with pytest.raises(studio.WorkflowTransitionRejected, match="state_version"):
        sm.transition_milestone(
            sid,
            "plan",
            "completed",
            epoch=ep,
            expected_state_version=ms1.state_version,
        )

    plan_ref = studio.EvidenceRef(
        kind="artifact",
        ref_id=".planning/PLAN.md",
        sha256="a" * 64,
        summary="plan spec",
    )
    ms1_done = sm.transition_milestone(
        sid,
        "plan",
        "completed",
        epoch=ep,
        expected_state_version=ms1_run.state_version,
        reason="finish_plan",
        evidence_refs=(plan_ref,),
    )
    assert ms1_done.state == "completed"

    # Root task CANNOT claim completion while milestone 2 ('implement') is still 'pending'!
    with pytest.raises(studio.WorkflowTransitionRejected, match="milestone"):
        sm.complete_task(sid, worker_id=claim.epoch_record.worker_id, epoch=ep, step=1)

    sm.transition_milestone(sid, "implement", "running", epoch=ep, reason="start_impl")
    sm.transition_milestone(sid, "implement", "completed", epoch=ep, reason="finish_impl")

    done_root = sm.complete_task(
        sid,
        worker_id=claim.epoch_record.worker_id,
        epoch=ep,
        step=2,
        checkpoint_payload={"workflow": "unit", "completed_step": 2},
        evidence_refs=(plan_ref,),
        reason="all_milestones_done",
    )
    assert done_root.state == "completed"

    # Re-instantiate fresh state machine against the same SQLite file to verify durability.
    reopened = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    persisted_root = reopened.store.get_task(sid)
    assert persisted_root is not None
    assert persisted_root.state == "completed"
    assert [m.state for m in reopened.list_milestones(sid)] == ["completed", "completed"]
    root_transitions = reopened.store.list_transitions(sid)
    assert [t.to_state for t in root_transitions] == ["running", "completed"]
    ms1_transitions = reopened.store.list_transitions(ms1.task_id)
    assert [t.to_state for t in ms1_transitions] == ["running", "completed"]
    assert json.loads(ms1_transitions[-1].evidence_json)["evidence_refs"][0]["ref_id"] == ".planning/PLAN.md"


def test_safe_boundary_pause_finishes_in_flight_tool_and_never_claims_completion(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2 & AC 2: Mid-tool pause defers to safe boundary, finishes Tool 0, skips Tool 1, and never claims completion."""
    sess = _create_session(isolated_env, title="Safe Boundary Pause")
    sid = sess["id"]

    async def fake_stream(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        # Step 1 asks for TWO sequential write_file tool calls.
        payload = (
            "Executing two edits.\n"
            '<tool_call>{"name": "write_file", "arguments": {"path": "first.txt", "content": "one\\n"}}</tool_call>\n'
            '<tool_call>{"name": "write_file", "arguments": {"path": "second.txt", "content": "two\\n"}}</tool_call>'
        )
        yield {"type": "content", "text": payload}
        yield {"type": "usage", "decode_tps": 50.0, "prefill_tps": 200.0, "ttft": 0.05, "completion_tokens": 30, "cached_tokens": 0}

    monkeypatch.setattr(studio, "stream_any_model", fake_stream)

    orig_inner_tool = studio._execute_harness_tool_inner
    pause_outcome_holder: list[Any] = []

    async def intercepting_inner_tool(client: Any, sess_arg: dict[str, Any], tool_name: str, args_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        # Operator requests pause WHILE Tool Call 0 (first.txt) is genuinely mid-execution inside the admitted boundary!
        if args_dict.get("path") == "first.txt":
            pause_res = studio.get_task_state_machine().request_pause(sid, reason="mid_tool_operator_pause")
            pause_outcome_holder.append(pause_res)
        return await orig_inner_tool(client, sess_arg, tool_name, args_dict, **kwargs)

    monkeypatch.setattr(studio, "_execute_harness_tool_inner", intercepting_inner_tool)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid, "prompt": "Write two files", "policy": "autonomous", "max_steps": 3},
        )
        assert resp.status_code == 200
        body_text = resp.text
        assert "event: paused" in body_text

        status_resp = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers())
        assert status_resp.status_code == 200
        status_json = status_resp.json()

    # 1. Pause request saw active lease and deferred to safe boundary instead of killing Tool 0 mid-flight.
    assert len(pause_outcome_holder) == 1
    assert pause_outcome_holder[0].deferred_to_safe_boundary is True

    # 2. Tool 0 committed cleanly; Tool 1 NEVER ran because safe boundary tripped right after Tool 0!
    assert (isolated_env / "first.txt").read_text(encoding="utf-8") == "one\n"
    assert not (isolated_env / "second.txt").exists()

    # 3. AC 2: Neither durable runtime_tasks nor harness_sessions ever claimed 'completed'!
    assert status_json["task"]["state"] == "paused"
    assert studio.get_harness_session_db(sid)["status"] == "paused"
    assert all(tr["to_state"] != "completed" for tr in status_json["transitions"])

    # 4. Interrupted checkpoint + committed action EvidenceRef were persisted at the safe boundary.
    latest_ckpt = status_json["latest_checkpoint"]
    assert latest_ckpt is not None
    assert latest_ckpt["status"] == "interrupted"
    action_refs = [r for r in latest_ckpt["evidence_refs"] if r["kind"] == "action"]
    assert len(action_refs) == 1


def test_cancel_during_harness_and_gsd_waves_never_claims_completion_and_blocks_resume(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2 & AC 2: Cancel fences task/milestones into 'cancelled', never claims completion, and rejects resume."""
    sess_gsd = _create_session(isolated_env, title="Cancel GSD")
    sid_gsd = sess_gsd["id"]

    call_counter = 0

    async def gsd_stream_with_cancel(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal call_counter
        call_counter += 1
        if call_counter == 1:
            # Wave 1 completes specification.
            yield {"type": "content", "text": "# Plan\n1. Create cancelled_check.txt\n"}
            yield {"type": "usage", "completion_tokens": 12}
        else:
            # During Wave 2 model streaming, operator cancels the task!
            studio.get_task_state_machine().cancel_task(sid_gsd, reason="cancelled_mid_wave_2")
            studio._HARNESS_ABORT_FLAGS.add(sid_gsd)
            yield {"type": "content", "text": "Partial output after cancel"}

    monkeypatch.setattr(studio, "stream_any_model", gsd_stream_with_cancel)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/api/harness/gsd-wave",
            headers=_headers(),
            json={"session_id": sid_gsd, "prompt": "Build widget", "policy": "autonomous"},
        )
        assert resp.status_code == 200
        assert "event: cancelled" in resp.text
        assert "VERIFICATION.md" not in resp.text

        snap = client.get(f"/api/harness/sessions/{sid_gsd}/task", headers=_headers()).json()
        assert snap["task"]["state"] == "cancelled"
        assert studio.get_harness_session_db(sid_gsd)["status"] == "cancelled"
        assert all(tr["to_state"] != "completed" for tr in snap["transitions"])

        # Wave 1 completed before cancel; Wave 2 & Wave 3 transitioned to 'cancelled' (never 'completed').
        ms_by_title = {m["title"]: m["state"] for m in snap["milestones"]}
        assert ms_by_title["Wave 1: Spec & Plan"] == "completed"
        assert ms_by_title["Wave 2: Fresh-Context Execute"] == "cancelled"
        assert ms_by_title["Wave 3: Fresh-Context Verify"] == "cancelled"

        # Cancelled task rejects both /resume and new /run calls with HTTP 409 Conflict!
        res_409 = client.post(f"/api/harness/sessions/{sid_gsd}/resume", headers=_headers())
        assert res_409.status_code == 409
        run_409 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid_gsd, "prompt": "Try running cancelled task"},
        )
        assert run_409.status_code == 409


def test_checkpoint_and_evidence_refs_persist_and_detect_payload_or_evidence_tampering(
    isolated_env: Path,
) -> None:
    """Task 3: Checkpoint + EvidenceRef SHA-256 integrity rejects tampering in either payload or evidence_refs."""
    sm = studio.get_task_state_machine()
    coord = studio.get_worker_lease_coordinator(isolated_env)
    sess = _create_session(isolated_env, title="Checkpoint Integrity")
    sid = sess["id"]

    artifact_file = isolated_env / "spec.md"
    artifact_file.write_text("# Verified Spec\n", encoding="utf-8")
    art_sha = hashlib.sha256(artifact_file.read_bytes()).hexdigest()

    sm.ensure_root_task(sid, session_id=sid, workspace_dir=str(isolated_env), policy="autonomous")
    claim = coord.acquire_or_takeover(sid, f"wrk-{studio._PROCESS_BOOT_ID}-ck1", lease_ttl_s=60.0)
    ep = claim.epoch_record.epoch

    sm.pause_at_safe_boundary(
        sid,
        worker_id=claim.epoch_record.worker_id,
        epoch=ep,
        step=2,
        checkpoint_payload={"workflow": "harness", "completed_step": 2},
        evidence_refs=(
            workflow_checkpoints.EvidenceRef(
                kind="artifact",
                ref_id="spec.md",
                sha256=art_sha,
                summary="spec digest",
            ),
        ),
        reason="paused_with_evidence",
    )

    loaded = sm.checkpoints.load_latest_checkpoint(sid, verify_integrity=True)
    assert loaded is not None
    assert loaded.step == 2
    assert loaded.evidence_refs[0].sha256 == art_sha

    # Tamper with evidence_refs_json directly in SQLite -> resume MUST fail closed!
    with sqlite3.connect(studio.DB_PATH) as conn:
        orig_refs = conn.execute(
            "SELECT evidence_refs_json FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
            (loaded.checkpoint_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE runtime_task_checkpoints SET evidence_refs_json = ? WHERE checkpoint_id = ?",
            ('[{"kind":"artifact","ref_id":"evil.md","sha256":"00","summary":"forged"}]', loaded.checkpoint_id),
        )

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        bad_resume = client.post(f"/api/harness/sessions/{sid}/resume", headers=_headers())
        assert bad_resume.status_code == 409
        assert "integrity" in bad_resume.json()["detail"]

    # Restore evidence_refs_json, then tamper with payload_json -> resume MUST also fail closed!
    with sqlite3.connect(studio.DB_PATH) as conn:
        conn.execute(
            "UPDATE runtime_task_checkpoints SET evidence_refs_json = ?, payload_json = ? WHERE checkpoint_id = ?",
            (orig_refs, '{"completed_step":999,"workflow":"harness"}', loaded.checkpoint_id),
        )
    with pytest.raises(studio.ResumeRejected, match="integrity"):
        sm.prepare_resume(sid)


def test_incomplete_transitions_staged_checkpoints_and_milestone_skew_recover_on_startup(
    isolated_env: Path,
) -> None:
    """Task 4: Startup recovery reconciles staged checkpoints, orphaned running tasks, pending cancels, and milestone skew."""
    sm = studio.get_task_state_machine()
    coord = studio.get_worker_lease_coordinator(isolated_env)

    # Case 1: Orphaned 'running' task + valid 'pending_commit' staged checkpoint + skewed 'running' child milestone.
    s1 = _create_session(isolated_env, title="Crash Mid Checkpoint")
    sid1 = s1["id"]
    sm.ensure_root_task(sid1, session_id=sid1, workspace_dir=str(isolated_env))
    ms1 = sm.ensure_milestone(sid1, "step_1", title="Step 1")
    c1 = coord.acquire_or_takeover(sid1, f"wrk-{studio._PROCESS_BOOT_ID}-c1", lease_ttl_s=300.0)
    sm.transition_milestone(sid1, "step_1", "running", epoch=c1.epoch_record.epoch)
    staged_ok = sm.checkpoints.stage_checkpoint(
        sid1,
        step=3,
        payload={"workflow": "harness", "completed_step": 3},
        evidence_refs=(workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="a.py", sha256="1" * 64),),
        epoch=c1.epoch_record.epoch,
        milestone_id=ms1.task_id,
    )
    studio._update_harness_session_state(sid1, status="running")

    # Case 2: Orphaned 'running' task with a CORRUPT 'pending_commit' row on top of an intact committed checkpoint.
    s2 = _create_session(isolated_env, title="Corrupt Staged Checkpoint")
    sid2 = s2["id"]
    sm.ensure_root_task(sid2, session_id=sid2, workspace_dir=str(isolated_env))
    c2 = coord.acquire_or_takeover(sid2, f"wrk-{studio._PROCESS_BOOT_ID}-c2", lease_ttl_s=300.0)
    good_ck2 = sm.checkpoints.save_checkpoint(
        sid2,
        step=1,
        payload={"workflow": "harness", "completed_step": 1},
        epoch=c2.epoch_record.epoch,
        status="committed",
        require_active_epoch=True,
    )
    bad_staged = sm.checkpoints.stage_checkpoint(
        sid2,
        step=2,
        payload={"workflow": "harness", "completed_step": 2},
        epoch=c2.epoch_record.epoch,
    )
    with sqlite3.connect(studio.DB_PATH) as conn:
        conn.execute(
            "UPDATE runtime_task_checkpoints SET payload_json = '{truncated' WHERE checkpoint_id = ?",
            (bad_staged.checkpoint_id,),
        )

    # Case 3: Orphaned 'running' task where 'cancel_requested' was persisted right before crash.
    s3 = _create_session(isolated_env, title="Interrupted Cancel")
    sid3 = s3["id"]
    sm.ensure_root_task(sid3, session_id=sid3, workspace_dir=str(isolated_env))
    coord.acquire_or_takeover(sid3, f"wrk-{studio._PROCESS_BOOT_ID}-c3", lease_ttl_s=300.0)
    sm.budgets.set_control_flags(sid3, cancel_requested=True)
    studio._update_harness_session_state(sid3, status="running")

    # Inject hard process restart -> fences foreign-boot leases and runs recover_incomplete_transitions()!
    studio.simulate_process_restart()

    sm_after = studio.get_task_state_machine()

    # Verify Case 1: staged checkpoint promoted to 'interrupted' + checkpoint_ref updated + root & milestone 'paused'!
    t1_after = sm_after.store.get_task(sid1)
    assert t1_after is not None
    assert t1_after.state == "paused"
    assert t1_after.checkpoint_ref == staged_ok.checkpoint_id
    ck1_after = sm_after.checkpoints.load_latest_checkpoint(sid1, verify_integrity=True)
    assert ck1_after is not None and ck1_after.status == "interrupted" and ck1_after.step == 3
    assert sm_after.list_milestones(sid1)[0].state == "paused"
    assert studio.get_harness_session_db(sid1)["status"] == "paused"

    # Verify Case 2: corrupt staged row marked 'superseded'; task safely retains verified good_ck2!
    t2_after = sm_after.store.get_task(sid2)
    assert t2_after is not None and t2_after.state == "paused"
    assert t2_after.checkpoint_ref == good_ck2.checkpoint_id
    ck2_after = sm_after.checkpoints.load_latest_checkpoint(sid2, verify_integrity=True)
    assert ck2_after is not None and ck2_after.checkpoint_id == good_ck2.checkpoint_id
    with sqlite3.connect(studio.DB_PATH) as conn:
        sup_status = conn.execute(
            "SELECT status FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
            (bad_staged.checkpoint_id,),
        ).fetchone()[0]
    assert sup_status == "superseded"

    # Verify Case 3: interrupted cancel completed to 'cancelled'!
    t3_after = sm_after.store.get_task(sid3)
    assert t3_after is not None and t3_after.state == "cancelled"
    assert studio.get_harness_session_db(sid3)["status"] == "cancelled"


def test_gsd_3_wave_task_survives_injected_restart_without_repeating_completed_waves_or_effects(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: GSD 3-wave task survives hard restart after Wave 2, skipping Waves 1 & 2 and finishing Wave 3 once."""
    sess = _create_session(isolated_env, title="GSD Restart Survival")
    sid = sess["id"]
    effect_log = isolated_env / "wave2_effect.log"

    run1_calls: list[str] = []

    async def gsd_stream_crash_before_wave3(
        _client: Any,
        _model: str,
        messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        sys_txt = messages[0]["content"]
        if "Wave-1 Spec Architect" in sys_txt:
            run1_calls.append("wave1")
            yield {"type": "content", "text": "# Plan\nAppend once to wave2_effect.log\n"}
            yield {"type": "usage", "completion_tokens": 15}
        elif "Independent Verifier" in sys_txt:
            run1_calls.append("wave3_crash")
            raise RuntimeError("Injected worker crash entering Wave 3")
        else:
            # Wave 2 step 1 runs bash side effect; step 2 finishes Wave 2 cleanly.
            if len([c for c in run1_calls if c.startswith("wave2")]) == 0:
                run1_calls.append("wave2_step1")
                cmd = f"printf 'applied\\n' >> {effect_log.name}"
                tc = json.dumps({"name": "run_bash", "arguments": {"command": cmd}})
                yield {"type": "content", "text": f"Running wave 2 build.\n<tool_call>{tc}</tool_call>"}
                yield {"type": "usage", "completion_tokens": 20}
            else:
                run1_calls.append("wave2_step2")
                yield {"type": "content", "text": "Wave 2 implementation complete."}
                yield {"type": "usage", "completion_tokens": 10}

    monkeypatch.setattr(studio, "stream_any_model", gsd_stream_crash_before_wave3)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        r1 = client.post(
            "/api/harness/gsd-wave",
            headers=_headers(),
            json={"session_id": sid, "prompt": "Implement wave2_effect", "policy": "autonomous", "max_steps": 4},
        )
        assert r1.status_code == 200
        assert "event: error" in r1.text
        assert effect_log.read_text(encoding="utf-8").splitlines() == ["applied"]

    # Simulate hard process restart after Wave 2 committed!
    studio.simulate_process_restart()
    sm = studio.get_task_state_machine()
    milestones_mid = {m.title: m.state for m in sm.list_milestones(sid)}
    assert milestones_mid["Wave 1: Spec & Plan"] == "completed"
    assert milestones_mid["Wave 2: Fresh-Context Execute"] == "completed"
    assert milestones_mid["Wave 3: Fresh-Context Verify"] != "completed"
    assert sm.store.get_task(sid).state in {"failed", "paused"}  # type: ignore[union-attr]

    # Post-restart model stream: ONLY Wave 3 should ever be invoked!
    run2_calls: list[str] = []

    async def gsd_stream_resume_wave3_only(
        _client: Any,
        _model: str,
        messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        sys_txt = messages[0]["content"]
        if "Independent Verifier" not in sys_txt:
            raise AssertionError(f"Completed wave should have been skipped on resume, got: {sys_txt[:80]}")
        run2_calls.append("wave3_verify")
        yield {"type": "content", "text": "## Verification\nStatus: PASS\n"}
        yield {"type": "usage", "completion_tokens": 18}

    monkeypatch.setattr(studio, "stream_any_model", gsd_stream_resume_wave3_only)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        r2 = client.post(
            "/api/harness/gsd-wave",
            headers=_headers(),
            json={
                "session_id": sid,
                "prompt": "Implement wave2_effect",
                "policy": "autonomous",
                "max_steps": 4,
                "resume": True,
            },
        )
        assert r2.status_code == 200
        assert "event: gsd_wave_skipped" in r2.text
        assert "event: gsd_wave_done" in r2.text

        final_snap = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers()).json()

    assert run2_calls == ["wave3_verify"]
    assert effect_log.read_text(encoding="utf-8").splitlines() == ["applied"]
    assert final_snap["task"]["state"] == "completed"
    assert all(m["state"] == "completed" for m in final_snap["milestones"])
    assert final_snap["budget"]["recoveries_used"] == 1
    assert (isolated_env / ".planning" / "VERIFICATION.md").is_file()


def test_multi_step_harness_task_survives_injected_restart_and_resumes_from_checkpoint_step(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: Multi-step /api/harness/run survives injected crash in Step 2 and resumes from Step 2 without double-charging recovery."""
    sess = _create_session(isolated_env, title="Harness Multi-Step Restart")
    sid = sess["id"]
    step1_marker = isolated_env / "step1_once.log"

    attempt1_steps: list[int] = []

    async def stream_crash_on_step2(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        step_num = len(attempt1_steps) + 1
        attempt1_steps.append(step_num)
        if step_num == 1:
            tc = json.dumps({"name": "run_bash", "arguments": {"command": f"printf 'step1\\n' >> {step1_marker.name}"}})
            yield {"type": "content", "text": f"Running step 1.\n<tool_call>{tc}</tool_call>"}
            yield {"type": "usage", "decode_tps": 50.0, "prefill_tps": 150.0, "ttft": 0.05, "completion_tokens": 20, "cached_tokens": 0}
        else:
            raise RuntimeError("Injected worker crash at start of Step 2")

    monkeypatch.setattr(studio, "stream_any_model", stream_crash_on_step2)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        r1 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid, "prompt": "Two-step task", "policy": "autonomous", "max_steps": 4},
        )
        assert r1.status_code == 200
        assert "event: error" in r1.text
        assert step1_marker.read_text(encoding="utf-8").splitlines() == ["step1"]

        studio.simulate_process_restart()

        # Two-step operator resume: /resume prepares next_step=2 and charges 1 recovery unit;
        # subsequent /run with resume=True starts at Step 2 and does NOT charge a 2nd recovery unit!
        prep = client.post(f"/api/harness/sessions/{sid}/resume", headers=_headers())
        assert prep.status_code == 200
        prep_json = prep.json()
        assert prep_json["next_step"] == 2
        assert prep_json["budget"]["recoveries_used"] == 1

        resumed_steps_seen: list[str] = []

        async def stream_finish_step2(
            _client: Any,
            _model: str,
            _messages: list[dict[str, str]],
            **_kwargs: Any,
        ) -> AsyncIterator[dict[str, Any]]:
            resumed_steps_seen.append("step2_finish")
            yield {"type": "content", "text": "Step 2 finished cleanly after restart."}
            yield {"type": "usage", "decode_tps": 50.0, "prefill_tps": 150.0, "ttft": 0.05, "completion_tokens": 14, "cached_tokens": 0}

        monkeypatch.setattr(studio, "stream_any_model", stream_finish_step2)

        r2 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid, "prompt": "Two-step task", "policy": "autonomous", "max_steps": 4, "resume": True},
        )
        assert r2.status_code == 200
        assert '"resumed_from_step": 1' in r2.text
        assert '"step": 2' in r2.text

        snap = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers()).json()
        assert snap["task"]["state"] == "completed"
        assert snap["budget"]["recoveries_used"] == 1
        assert snap["budget"]["steps_used"] == 2
        assert step1_marker.read_text(encoding="utf-8").splitlines() == ["step1"]


def test_task_and_recovery_budgets_persist_across_restarts_and_enforce_caps(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: Task usage counters and recovery budgets persist across restarts and fail closed when exhausted."""
    sess = _create_session(isolated_env, title="Persistent Budgets")
    sid = sess["id"]

    step_mode = {"call_tools": True}

    async def budgeted_stream(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if step_mode["call_tools"]:
            tc = json.dumps({"name": "write_file", "arguments": {"path": "b.txt", "content": "ok\n"}})
            yield {"type": "content", "text": f"Step work.\n<tool_call>{tc}</tool_call>"}
            yield {"type": "usage", "decode_tps": 40.0, "prefill_tps": 100.0, "ttft": 0.05, "completion_tokens": 50, "cached_tokens": 0}
        else:
            yield {"type": "content", "text": "Finished within budget."}
            yield {"type": "usage", "decode_tps": 40.0, "prefill_tps": 100.0, "ttft": 0.05, "completion_tokens": 25, "cached_tokens": 0}

    monkeypatch.setattr(studio, "stream_any_model", budgeted_stream)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        # Configure max_steps=2, max_tool_calls=2, max_recoveries=2; run 3-step loop -> halts after Step 2 on max_steps=2!
        r_run1 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={
                "session_id": sid,
                "prompt": "Run budgeted steps",
                "policy": "autonomous",
                "max_steps": 4,
                "budget": {
                    "max_steps": 2,
                    "max_tool_calls": 5,
                    "max_tokens": 10_000,
                    "max_elapsed_s": 300.0,
                    "max_recoveries": 2,
                },
            },
        )
        assert r_run1.status_code == 200
        # Step 1 (1 step), Step 2 (2 steps), Step 3 exceeds max_steps=2 -> budget_exceeded & paused (NOT completed)!
        assert "event: budget_exceeded" in r_run1.text

        snap1 = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers()).json()
        assert snap1["task"]["state"] == "paused"
        assert snap1["budget"]["steps_used"] == 3
        assert snap1["budget"]["tool_calls_used"] == 3
        assert snap1["budget"]["tokens_used"] == 150
        assert "step budget exceeded" in snap1["budget"]["exhausted_reason"]

        # Restart process -> budget counters MUST survive unchanged in SQLite!
        studio.simulate_process_restart()
        snap_after_restart = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers()).json()
        assert snap_after_restart["budget"]["steps_used"] == 3
        assert snap_after_restart["budget"]["tool_calls_used"] == 3
        assert snap_after_restart["budget"]["tokens_used"] == 150

        # Attempting another run without raising max_steps fails closed with HTTP 409!
        blocked_409 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid, "prompt": "Continue", "resume": True},
        )
        assert blocked_409.status_code == 409
        assert "step budget exceeded" in blocked_409.json()["detail"]

        # Now test durable recovery budget cap (max_recoveries = 2) on a crash-looping session!
        sess_rec = _create_session(isolated_env, title="Recovery Cap")
        sid_rec = sess_rec["id"]
        sm = studio.get_task_state_machine()
        sm.ensure_root_task(
            sid_rec,
            session_id=sid_rec,
            workspace_dir=str(isolated_env),
            budget_spec=execution_budget.TaskBudgetSpec(max_steps=10, max_recoveries=2),
            override_budget_spec=True,
        )

        for expected_rec in (1, 2):
            coord = studio.get_worker_lease_coordinator(isolated_env)
            c = coord.acquire_or_takeover(sid_rec, f"wrk-{studio._PROCESS_BOOT_ID}-r{expected_rec}", lease_ttl_s=300.0)
            sm.checkpoints.save_checkpoint(
                sid_rec,
                step=expected_rec,
                payload={"workflow": "harness", "completed_step": expected_rec},
                epoch=c.epoch_record.epoch,
                status="committed",
                require_active_epoch=True,
            )
            # Hard crash & restart!
            studio.simulate_process_restart()
            res_ok = client.post(f"/api/harness/sessions/{sid_rec}/resume", headers=_headers())
            assert res_ok.status_code == 200
            assert res_ok.json()["budget"]["recoveries_used"] == expected_rec

        # Crash a 3rd time (exceeding max_recoveries=2)!
        coord3 = studio.get_worker_lease_coordinator(isolated_env)
        c3 = coord3.acquire_or_takeover(sid_rec, f"wrk-{studio._PROCESS_BOOT_ID}-r3", lease_ttl_s=300.0)
        sm.checkpoints.save_checkpoint(
            sid_rec,
            step=3,
            payload={"workflow": "harness", "completed_step": 3},
            epoch=c3.epoch_record.epoch,
            status="committed",
            require_active_epoch=True,
        )
        studio.simulate_process_restart()

        res_exhausted = client.post(f"/api/harness/sessions/{sid_rec}/resume", headers=_headers())
        assert res_exhausted.status_code == 409
        assert "recovery budget exceeded" in res_exhausted.json()["detail"]

        rec_snap = client.get(f"/api/harness/sessions/{sid_rec}/task", headers=_headers()).json()
        assert rec_snap["task"]["state"] == "blocked"
        assert rec_snap["budget"]["recoveries_used"] == 2
        assert "recovery budget exceeded" in rec_snap["budget"]["exhausted_reason"]


def test_standalone_studio_bundle_runs_durable_state_machine_in_isolation(tmp_path: Path) -> None:
    """Standalone studio.py runs DurableTaskStateMachine, checkpoints, and budgets with zero sibling modules."""
    isolated_dir = tmp_path / "standalone"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    bundle_src = Path(__file__).resolve().parents[1] / "studio.py"
    static_src = Path(__file__).resolve().parents[1] / "static" / "index.html"
    bundle_dst = isolated_dir / "studio.py"
    (isolated_dir / "static").mkdir(parents=True, exist_ok=True)
    bundle_dst.write_text(bundle_src.read_text(encoding="utf-8"), encoding="utf-8")
    (isolated_dir / "static" / "index.html").write_text(static_src.read_text(encoding="utf-8"), encoding="utf-8")

    mod_name = f"standalone_studio_p105_{int(time.time() * 1000)}"
    old_sys_path = list(sys.path)
    try:
        sys.path = [str(isolated_dir)]
        spec = importlib.util.spec_from_file_location(mod_name, bundle_dst)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

        ws = tmp_path / "standalone_ws"
        ws.mkdir(parents=True, exist_ok=True)
        mod.DB_PATH = tmp_path / "standalone.sqlite3"
        mod.WORKSPACE_ROOT = ws
        mod.HARNESS_STORE_DIR = tmp_path / "standalone_harness"
        mod.HARNESS_STORE_DIR.mkdir(parents=True, exist_ok=True)
        mod.init_db()
        mod._set_default_projects_dir(str(ws), create_if_missing=True)

        sm = mod.get_task_state_machine()
        coord = mod.get_worker_lease_coordinator(ws)
        sm.ensure_root_task(
            "tsk-standalone",
            session_id="tsk-standalone",
            workspace_dir=str(ws),
            budget_spec=mod.execution_budget.TaskBudgetSpec(max_steps=5, max_recoveries=1),
            override_budget_spec=True,
        )
        sm.ensure_milestone("tsk-standalone", "m1", title="Standalone Milestone")
        c = coord.acquire_or_takeover("tsk-standalone", f"wrk-{mod._PROCESS_BOOT_ID}-sa", lease_ttl_s=120.0)
        sm.transition_milestone("tsk-standalone", "m1", "running", epoch=c.epoch_record.epoch)
        sm.checkpoints.stage_checkpoint(
            "tsk-standalone",
            step=1,
            payload={"workflow": "standalone", "completed_step": 1},
            epoch=c.epoch_record.epoch,
        )

        mod.simulate_process_restart()
        sm2 = mod.get_task_state_machine()
        recovered = sm2.store.get_task("tsk-standalone")
        assert recovered is not None and recovered.state == "paused"
        plan = sm2.prepare_resume("tsk-standalone")
        assert plan.next_step == 2
        assert plan.budget.recoveries_used == 1
    finally:
        sys.path = old_sys_path
        sys.modules.pop(mod_name, None)
