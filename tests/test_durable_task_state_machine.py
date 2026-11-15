"""M5-P1-05 tests: Durable task state machine, safe boundaries, checkpoints, and budgets."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any, AsyncIterator, Iterator

from fastapi.testclient import TestClient
import pytest

import artifact_lineage
import execution_budget
import execution_spine
import process_supervision
import runtime_persistence
import sandbox_boundary
import side_effect_ledger
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

        # AP-005 contracts are inlined too; no sibling source module is available.
        sm2.ensure_root_task("mission-standalone", session_id="mission-standalone")
        mission_authority = mod.MissionAuthorityEnvelope(
            "mission-standalone",
            "mission-standalone",
            "mission-standalone",
            1,
            1,
        )
        sm2.create_mission_plan(
            "mission-standalone",
            "standalone-plan",
            1,
            (mod.MissionTaskSpec("only", required_outputs=("result",)),),
            inputs={"source": "standalone"},
            authority=mission_authority,
        )
        assert sm2.list_mission_readiness("standalone-plan", 1)[0].ready is True

        # AP-007 contracts are inlined too: a supervised stop, its report and a bounded stop.
        halted = sm2.halt_mission(
            "standalone-plan",
            1,
            authority=mission_authority,
            reason="standalone stop",
            supervisor=mod.ProcessSupervisor(mod.ContainmentStore(tmp_path / "standalone_containment")),
        )
        assert (halted.outcome, halted.plan_status, halted.claims_success) == ("cancelled", "cancelled", False)
        assert halted.stop["owned_work"]["cleanly_stopped"] is True
        assert halted.limitation_codes[0] == "mission_cancelled"
        assert sm2.list_mission_readiness("standalone-plan", 1)[0].ready is False
        assert sm2.mission_recovery_report("standalone-plan", 1).report_hash == halted.report_hash
        assert mod.isolation_refusal(None).startswith("runtime isolation is required")
        sm2.ensure_root_task(
            "bounded-standalone",
            session_id="bounded-standalone",
            budget_spec=mod.execution_budget.TaskBudgetSpec(max_no_progress=0),
            override_budget_spec=True,
        )
        with pytest.raises(mod.BoundedStopRequired) as bounded:
            sm2.budgets.record_bounded_observation(
                "bounded-standalone",
                root_task_id="bounded-standalone",
                session_id="bounded-standalone",
                progressed=False,
            )
        assert bounded.value.verdict.code == "no_progress"
    finally:
        sys.path = old_sys_path
        sys.modules.pop(mod_name, None)


# =============================================================================
# AP-005: versioned mission DAGs, fenced workers, resume bindings, and joins
# =============================================================================


def _mission_authority(
    root_task_id: str,
    task_id: str | None = None,
    *,
    session_revision: int = 7,
    global_revision: int = 11,
) -> execution_spine.MissionAuthorityEnvelope:
    return execution_spine.MissionAuthorityEnvelope(
        session_id=root_task_id,
        root_task_id=root_task_id,
        task_id=task_id or root_task_id,
        session_policy_revision=session_revision,
        global_policy_revision=global_revision,
    )


def test_ap005_dependency_dag_rejects_cycles_and_fences_false_completion(
    isolated_env: Path,
) -> None:
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    root = "ap005-dag-root"
    sm.ensure_root_task(
        root,
        session_id=root,
        workspace_dir=str(isolated_env),
        budget_spec=execution_budget.TaskBudgetSpec(max_steps=20),
        override_budget_spec=True,
    )
    inputs = {"request": "build the offline release", "input_version": 3}
    root_authority = _mission_authority(root)

    with pytest.raises(workflow_state.MissionGraphError, match="cycle"):
        sm.create_mission_plan(
            root,
            "ap005-cycle",
            1,
            (
                workflow_state.MissionTaskSpec("a", required_outputs=("a-out",)),
                workflow_state.MissionTaskSpec("b", required_outputs=("b-out",)),
            ),
            (
                workflow_state.MissionDependencySpec("a", "b", ("b-out",)),
                workflow_state.MissionDependencySpec("b", "a", ("a-out",)),
            ),
            inputs=inputs,
            authority=root_authority,
        )

    plan = sm.create_mission_plan(
        root,
        "ap005-build",
        1,
        (
            workflow_state.MissionTaskSpec("spec", "Write spec", ("spec",)),
            workflow_state.MissionTaskSpec("build", "Build package", ("package",)),
        ),
        (workflow_state.MissionDependencySpec("build", "spec", ("spec",)),),
        inputs=inputs,
        authority=root_authority,
    )
    assert plan.status == "active"
    binding = sm.capture_mission_binding("ap005-build", 1)
    spec_task, build_task = sm.list_mission_tasks("ap005-build", 1)
    readiness = sm.list_mission_readiness("ap005-build", 1)
    assert [item.ready for item in readiness] == [True, False]
    assert readiness[1].blocked_by_task_keys == ("spec",)

    with pytest.raises(workflow_state.MissionDependencyRejected, match="not dependency-ready"):
        sm.claim_ready_mission_task(
            "ap005-build",
            1,
            "build",
            binding=binding,
            current_inputs=inputs,
            current_authority=_mission_authority(root, build_task.task_id),
            worker_id="worker-build-too-early",
        )

    spec_lease = sm.claim_ready_mission_task(
        "ap005-build",
        1,
        "spec",
        binding=binding,
        current_inputs=inputs,
        current_authority=_mission_authority(root, spec_task.task_id),
        worker_id="worker-spec",
        now=10.0,
    )
    with pytest.raises(workflow_state.FalseCompletionRejected, match="missing required output"):
        sm.complete_mission_task(
            "ap005-build",
            1,
            "spec",
            binding=binding,
            current_inputs=inputs,
            current_authority=_mission_authority(root, spec_task.task_id),
            worker_id="worker-spec",
            epoch=spec_lease.epoch.epoch,
            expected_state_version=spec_lease.task.state_version,
            outputs=(),
            now=11.0,
        )
    sm.complete_mission_task(
        "ap005-build",
        1,
        "spec",
        binding=binding,
        current_inputs=inputs,
        current_authority=_mission_authority(root, spec_task.task_id),
        worker_id="worker-spec",
        epoch=spec_lease.epoch.epoch,
        expected_state_version=spec_lease.task.state_version,
        outputs=(workflow_state.MissionOutputRef("spec", "artifacts/spec.json", "a" * 64),),
        now=11.0,
    )
    partial = sm.evaluate_mission_join("ap005-build", 1)
    assert partial.status == "partial"
    assert partial.incomplete_task_keys == ("build",)
    assert sm.list_mission_readiness("ap005-build", 1)[1].ready is True
    with pytest.raises(workflow_state.MissionDependencyRejected, match="not dependency-ready"):
        sm.claim_ready_mission_task(
            "ap005-build",
            1,
            "spec",
            binding=binding,
            current_inputs=inputs,
            current_authority=_mission_authority(root, spec_task.task_id),
            worker_id="illegal-second-spec-worker",
        )

    build_lease = sm.claim_ready_mission_task(
        "ap005-build",
        1,
        "build",
        binding=binding,
        current_inputs=inputs,
        current_authority=_mission_authority(root, build_task.task_id),
        worker_id="worker-build-old",
        now=12.0,
    )
    successor, superseded = sm.store.acquire_worker_lease(
        build_task.task_id,
        "worker-build-successor",
        force_takeover=True,
        now=13.0,
    )
    assert successor.epoch == build_lease.epoch.epoch + 1
    assert superseded[0].status == "fenced"
    with pytest.raises(runtime_persistence.StaleEpochError):
        sm.complete_mission_task(
            "ap005-build",
            1,
            "build",
            binding=binding,
            current_inputs=inputs,
            current_authority=_mission_authority(root, build_task.task_id),
            worker_id="worker-build-old",
            epoch=build_lease.epoch.epoch,
            expected_state_version=build_lease.task.state_version,
            outputs=(workflow_state.MissionOutputRef("package", "artifacts/app.tar", "b" * 64),),
            now=14.0,
        )
    sm.complete_mission_task(
        "ap005-build",
        1,
        "build",
        binding=binding,
        current_inputs=inputs,
        current_authority=_mission_authority(root, build_task.task_id),
        worker_id="worker-build-successor",
        epoch=successor.epoch,
        expected_state_version=build_lease.task.state_version,
        outputs=(workflow_state.MissionOutputRef("package", "artifacts/app.tar", "b" * 64),),
        now=14.0,
    )
    assert sm.evaluate_mission_join("ap005-build", 1).status == "succeeded"

    reopened = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    durable = reopened.evaluate_mission_join("ap005-build", 1, persist=False)
    assert durable.status == "succeeded"
    assert [result.present_outputs for result in durable.tasks] == [("spec",), ("package",)]


def test_ap005_restart_resume_revalidates_every_binding_and_fences_old_worker(
    isolated_env: Path,
) -> None:
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    root = "ap005-resume-root"
    original_budget = execution_budget.TaskBudgetSpec(max_steps=17, max_recoveries=2)
    sm.ensure_root_task(
        root,
        session_id=root,
        workspace_dir=str(isolated_env),
        budget_spec=original_budget,
        override_budget_spec=True,
    )
    inputs = {"source": "fixture", "revision": 9}
    sm.create_mission_plan(
        root,
        "ap005-resume",
        1,
        (workflow_state.MissionTaskSpec("render", required_outputs=("rendered",)),),
        inputs=inputs,
        authority=_mission_authority(root),
        now=20.0,
    )
    binding = sm.capture_mission_binding("ap005-resume", 1)
    task = sm.list_mission_tasks("ap005-resume", 1)[0]
    authority = _mission_authority(root, task.task_id)
    lease = sm.claim_ready_mission_task(
        "ap005-resume",
        1,
        "render",
        binding=binding,
        current_inputs=inputs,
        current_authority=authority,
        worker_id="resume-worker-old",
        lease_ttl_s=2.0,
        now=21.0,
    )
    checkpoint = sm.checkpoint_mission_task(
        "ap005-resume",
        1,
        "render",
        binding=binding,
        current_inputs=inputs,
        current_authority=authority,
        worker_id="resume-worker-old",
        epoch=lease.epoch.epoch,
        expected_state_version=lease.task.state_version,
        step=4,
        payload={"completed_chunks": [1, 2, 3]},
        now=22.0,
    )
    report = sm.recover_incomplete_transitions(now=24.0)
    assert task.task_id in report.recovered_task_ids

    restarted = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    paused = restarted.store.get_task(task.task_id)
    assert paused is not None and paused.state == "paused"
    assert restarted.checkpoints.get_checkpoint(checkpoint.checkpoint_id).status == "interrupted"  # type: ignore[union-attr]

    stale_cases = (
        ({"source": "fixture", "revision": 10}, authority, binding, "input digest"),
        (inputs, _mission_authority(root, task.task_id, session_revision=8), binding, "session policy"),
        (inputs, _mission_authority(root, task.task_id, global_revision=12), binding, "global policy"),
        (inputs, authority, replace(binding, plan_digest="f" * 64), "plan/version"),
    )
    for current_inputs, current_authority, current_binding, message in stale_cases:
        with pytest.raises(workflow_state.MissionBindingRejected, match=message):
            restarted.prepare_mission_task_resume(
                "ap005-resume",
                1,
                "render",
                binding=current_binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                worker_id="resume-worker-new",
                expected_state_version=paused.state_version,
                now=25.0,
            )

    restarted.budgets.ensure_budget(
        root,
        spec=execution_budget.TaskBudgetSpec(max_steps=99, max_recoveries=2),
        override_spec=True,
        now=25.0,
    )
    with pytest.raises(workflow_state.MissionBindingRejected, match="budget identity"):
        restarted.prepare_mission_task_resume(
            "ap005-resume",
            1,
            "render",
            binding=binding,
            current_inputs=inputs,
            current_authority=authority,
            worker_id="resume-worker-new",
            expected_state_version=paused.state_version,
            now=25.0,
        )
    restarted.budgets.ensure_budget(root, spec=original_budget, override_spec=True, now=25.0)

    with pytest.raises(workflow_checkpoints.ResumeRejected, match="state_version"):
        restarted.prepare_mission_task_resume(
            "ap005-resume",
            1,
            "render",
            binding=binding,
            current_inputs=inputs,
            current_authority=authority,
            worker_id="resume-worker-new",
            expected_state_version=paused.state_version - 1,
            now=25.0,
        )
    resumed = restarted.prepare_mission_task_resume(
        "ap005-resume",
        1,
        "render",
        binding=binding,
        current_inputs=inputs,
        current_authority=authority,
        worker_id="resume-worker-new",
        expected_state_version=paused.state_version,
        now=25.0,
    )
    assert resumed.next_step == 5
    assert resumed.epoch.epoch == lease.epoch.epoch + 1
    assert restarted.checkpoints.get_mission_binding(checkpoint.checkpoint_id).resumed_epoch == resumed.epoch.epoch  # type: ignore[union-attr]

    with pytest.raises((workflow_state.MissionBindingRejected, runtime_persistence.StaleEpochError)):
        restarted.complete_mission_task(
            "ap005-resume",
            1,
            "render",
            binding=binding,
            current_inputs=inputs,
            current_authority=authority,
            worker_id="resume-worker-old",
            epoch=lease.epoch.epoch,
            expected_state_version=lease.task.state_version,
            outputs=(workflow_state.MissionOutputRef("rendered", "out/render.txt", "c" * 64),),
            now=26.0,
        )
    restarted.complete_mission_task(
        "ap005-resume",
        1,
        "render",
        binding=binding,
        current_inputs=inputs,
        current_authority=authority,
        worker_id="resume-worker-new",
        epoch=resumed.epoch.epoch,
        expected_state_version=resumed.task.state_version,
        outputs=(workflow_state.MissionOutputRef("rendered", "out/render.txt", "c" * 64),),
        now=26.0,
    )
    assert workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH).evaluate_mission_join(
        "ap005-resume", 1, persist=False
    ).status == "succeeded"


def test_ap005_superseded_plan_fences_workers_and_only_successor_is_schedulable(
    isolated_env: Path,
) -> None:
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    root = "ap005-version-root"
    sm.ensure_root_task(root, session_id=root, workspace_dir=str(isolated_env))
    inputs = {"versioned": True}
    sm.create_mission_plan(
        root,
        "ap005-versioned",
        1,
        (workflow_state.MissionTaskSpec("old", required_outputs=("out",)),),
        inputs=inputs,
        authority=_mission_authority(root),
        now=30.0,
    )
    old_binding = sm.capture_mission_binding("ap005-versioned", 1)
    old_task = sm.list_mission_tasks("ap005-versioned", 1)[0]
    old_authority = _mission_authority(root, old_task.task_id)
    old_lease = sm.claim_ready_mission_task(
        "ap005-versioned",
        1,
        "old",
        binding=old_binding,
        current_inputs=inputs,
        current_authority=old_authority,
        worker_id="old-plan-worker",
        now=31.0,
    )
    sm.create_mission_plan(
        root,
        "ap005-versioned",
        2,
        (workflow_state.MissionTaskSpec("new", required_outputs=("out",)),),
        inputs=inputs,
        authority=_mission_authority(root),
        now=32.0,
    )
    assert sm.get_mission_plan("ap005-versioned", 1).status == "superseded"  # type: ignore[union-attr]
    assert sm.store.get_epoch(old_task.task_id, old_lease.epoch.epoch).status == "fenced"  # type: ignore[union-attr]
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        sm.complete_mission_task(
            "ap005-versioned",
            1,
            "old",
            binding=old_binding,
            current_inputs=inputs,
            current_authority=old_authority,
            worker_id="old-plan-worker",
            epoch=old_lease.epoch.epoch,
            expected_state_version=old_lease.task.state_version,
            outputs=(workflow_state.MissionOutputRef("out", "old.out", "d" * 64),),
            now=33.0,
        )
    active_milestones = sm.list_milestones(root)
    assert [task.task_id for task in active_milestones] == [sm.list_mission_tasks("ap005-versioned", 2)[0].task_id]


def test_ap005_join_reports_failed_and_missing_outputs_and_blocks_parent_success(
    isolated_env: Path,
) -> None:
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)

    failed_root = "ap005-failed-root"
    sm.ensure_root_task(failed_root, session_id=failed_root, workspace_dir=str(isolated_env))
    sm.create_mission_plan(
        failed_root,
        "ap005-failed",
        1,
        (workflow_state.MissionTaskSpec("only", required_outputs=("result",)),),
        inputs={},
        authority=_mission_authority(failed_root),
        now=40.0,
    )
    failed_binding = sm.capture_mission_binding("ap005-failed", 1)
    failed_task = sm.list_mission_tasks("ap005-failed", 1)[0]
    failed_authority = _mission_authority(failed_root, failed_task.task_id)
    failed_lease = sm.claim_ready_mission_task(
        "ap005-failed",
        1,
        "only",
        binding=failed_binding,
        current_inputs={},
        current_authority=failed_authority,
        worker_id="failure-worker",
        now=41.0,
    )
    sm.fail_mission_task(
        "ap005-failed",
        1,
        "only",
        binding=failed_binding,
        current_inputs={},
        current_authority=failed_authority,
        worker_id="failure-worker",
        epoch=failed_lease.epoch.epoch,
        expected_state_version=failed_lease.task.state_version,
        reason="fixture_failure",
        now=42.0,
    )
    failed_join = sm.evaluate_mission_join("ap005-failed", 1)
    assert failed_join.status == "failed"
    assert failed_join.failed_task_keys == ("only",)

    missing_root = "ap005-missing-root"
    sm.ensure_root_task(missing_root, session_id=missing_root, workspace_dir=str(isolated_env))
    sm.create_mission_plan(
        missing_root,
        "ap005-missing",
        1,
        (workflow_state.MissionTaskSpec("producer", required_outputs=("artifact",)),),
        inputs={},
        authority=_mission_authority(missing_root),
        now=43.0,
    )
    missing_task = sm.list_mission_tasks("ap005-missing", 1)[0]

    def _simulate_legacy_false_completion(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE runtime_tasks SET state = 'completed', state_version = state_version + 1 WHERE task_id = ?",
            (missing_task.task_id,),
        )

    runtime_persistence.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        _simulate_legacy_false_completion,
    )
    missing_join = sm.evaluate_mission_join("ap005-missing", 1)
    assert missing_join.status == "failed"
    assert missing_join.missing_output_task_keys == ("producer",)
    assert missing_join.tasks[0].missing_outputs == ("artifact",)

    root_epoch, _ = sm.store.acquire_worker_lease(missing_root, "parent-worker", now=44.0)
    sm.transition_task(missing_root, "running", epoch=root_epoch.epoch, now=44.0)
    with pytest.raises(workflow_state.FalseCompletionRejected, match="missing_outputs"):
        sm.complete_task(
            missing_root,
            worker_id="parent-worker",
            epoch=root_epoch.epoch,
            now=45.0,
        )


# =============================================================================
# AP-007: cancellation, bounded stops, isolation refusal and truthful recovery
# =============================================================================

_AP007_INPUTS = {"request": "assemble the offline report", "input_version": 1}
# A harmless idle child.  It stands in for work a mission task owns.
_AP007_IDLE_CHILD = "import time\nwhile True:\n    time.sleep(0.05)\n"
# Clearly fictional evidence for the positive isolation path.  No isolated runtime
# is qualified in this repository; these tests never claim that one exists.
_AP007_FIXTURE_ISOLATION = execution_budget.IsolationEvidence(
    execution_mode="sandbox",
    admitted=True,
    qualified=True,
    backend_id="test-fixture-backend-not-a-real-runtime",
)


def _ap007_machine(
    workspace: Path,
    root: str,
    spec: execution_budget.TaskBudgetSpec | None = None,
) -> workflow_state.DurableTaskStateMachine:
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    sm.ensure_root_task(
        root,
        session_id=root,
        workspace_dir=str(workspace),
        budget_spec=spec or execution_budget.TaskBudgetSpec(max_steps=50),
        override_budget_spec=True,
    )
    return sm


def _ap007_plan(
    sm: workflow_state.DurableTaskStateMachine,
    root: str,
    mission: str,
    *,
    version: int = 1,
    keys: tuple[str, ...] = ("a", "b"),
    deps: tuple[tuple[str, str], ...] = (),
) -> tuple[workflow_state.MissionExecutionBinding, dict[str, runtime_persistence.TaskRecord]]:
    """Create one plan version whose task ``key`` must deliver the output ``<key>-out``."""
    sm.create_mission_plan(
        root,
        mission,
        version,
        tuple(workflow_state.MissionTaskSpec(key, f"Task {key}", (f"{key}-out",)) for key in keys),
        tuple(workflow_state.MissionDependencySpec(task, needs, (f"{needs}-out",)) for task, needs in deps),
        inputs=_AP007_INPUTS,
        authority=_mission_authority(root),
    )
    binding = sm.capture_mission_binding(mission, version)
    tasks = {item.task_key: item.task for item in sm.list_mission_readiness(mission, version)}
    return binding, tasks


def _ap007_claim(
    sm: workflow_state.DurableTaskStateMachine,
    root: str,
    mission: str,
    key: str,
    task: runtime_persistence.TaskRecord,
    binding: workflow_state.MissionExecutionBinding,
    *,
    version: int = 1,
    isolation: execution_budget.IsolationEvidence | None = None,
    now: float | None = None,
) -> workflow_state.MissionTaskLease:
    return sm.claim_ready_mission_task(
        mission,
        version,
        key,
        binding=binding,
        current_inputs=_AP007_INPUTS,
        current_authority=_mission_authority(root, task.task_id),
        worker_id=f"worker-{key}",
        isolation=isolation,
        now=now,
    )


def _ap007_complete(
    sm: workflow_state.DurableTaskStateMachine,
    root: str,
    mission: str,
    key: str,
    lease: workflow_state.MissionTaskLease,
    binding: workflow_state.MissionExecutionBinding,
    sha256: str,
    *,
    version: int = 1,
) -> None:
    sm.complete_mission_task(
        mission,
        version,
        key,
        binding=binding,
        current_inputs=_AP007_INPUTS,
        current_authority=_mission_authority(root, lease.task.task_id),
        worker_id=f"worker-{key}",
        epoch=lease.epoch.epoch,
        expected_state_version=lease.task.state_version,
        outputs=(workflow_state.MissionOutputRef(f"{key}-out", f"artifacts/{key}.json", sha256),),
    )


def _ap007_supervisor(tmp_path: Path) -> process_supervision.ProcessSupervisor:
    return process_supervision.ProcessSupervisor(
        process_supervision.ContainmentStore(tmp_path / "ap007_containment"),
        grace_timeout_s=0.5,
    )


def _ap007_begin_action(
    ledger: side_effect_ledger.DurableActionLedger,
    action_id: str,
    lease: workflow_state.MissionTaskLease,
    *,
    session_id: str,
    read_only: bool = False,
) -> None:
    outcome = ledger.begin(
        action_id=action_id,
        tool_name="read_file" if read_only else "run_bash",
        args={"path": "notes.txt"} if read_only else {"command": "publish the report"},
        policy="autonomous",
        action_class="read_only" if read_only else "consequential",
        allowed=True,
        explicit_approval=False,
        session_id=session_id,
        task_id=lease.task.task_id,
        epoch=lease.epoch.epoch,
        worker_id=lease.epoch.worker_id,
    )
    assert outcome.proceed is True and outcome.status == "in_flight"


@pytest.fixture()
def ap007_children() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Fixture subprocesses a test started; none of them outlives the test."""
    children: list[subprocess.Popen[bytes]] = []
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def _ap007_owned_process(
    supervisor: process_supervision.ProcessSupervisor,
    children: list[subprocess.Popen[bytes]],
    workspace: Path,
    *,
    session_id: str,
    lease: workflow_state.MissionTaskLease,
) -> subprocess.Popen[bytes]:
    """Start the idle child and register it as work owned by the leased task."""
    supervision_id = supervisor.new_supervision_id()
    origin = {
        "session_id": session_id,
        "task_id": lease.task.task_id,
        "epoch": lease.epoch.epoch,
        "worker_id": lease.epoch.worker_id,
    }
    child = subprocess.Popen(
        [sys.executable, "-c", _AP007_IDLE_CHILD],
        start_new_session=True,
        env=supervisor.prepare_spawn_env(os.environ, supervision_id=supervision_id, **origin),
    )
    children.append(child)
    supervisor.register_process(
        child,  # type: ignore[arg-type]  # the supervisor only reads the pid
        supervision_id=supervision_id,
        tool_name="run_bash",
        workspace_root=workspace,
        **origin,
    )
    return child


def test_ap007_cancellation_stops_dispatch_and_terminates_owned_work_with_measured_latency(
    isolated_env: Path,
    tmp_path: Path,
    ap007_children: list[subprocess.Popen[bytes]],
) -> None:
    """AC1: a stop fences every dispatch path, ends owned processes and records how long that took."""
    root, mission = "ap007-cancel-root", "ap007-cancel"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(
        sm, root, mission, keys=("done", "running", "waiting"), deps=(("waiting", "running"),)
    )
    done = _ap007_claim(sm, root, mission, "done", tasks["done"], binding)
    _ap007_complete(sm, root, mission, "done", done, binding, "a" * 64)
    running = _ap007_claim(sm, root, mission, "running", tasks["running"], binding)
    supervisor = _ap007_supervisor(tmp_path)
    child = _ap007_owned_process(supervisor, ap007_children, isolated_env, session_id=root, lease=running)
    assert child.poll() is None
    root_before = sm.store.get_task(root)
    assert root_before is not None

    report = sm.halt_mission(
        mission, 1, authority=_mission_authority(root), reason="operator pressed stop", supervisor=supervisor
    )

    # The owned process is gone, and the stop record carries the measurement.
    assert child.wait(timeout=5) is not None
    assert report.stop is not None
    owned = report.stop["owned_work"]
    assert owned["termination_attempted"] is True
    assert owned["cleanly_stopped"] is True
    assert (owned["processes"], owned["lingering"], owned["errors"]) == (1, 0, 0)
    assert owned["origins_fenced"] == 1
    assert 0 <= owned["dispatch_fenced_ms"] <= owned["cleanup_latency_ms"]
    assert 0 < owned["cleanup_latency_ms"] < 10_000
    assert owned["slowest_process_ms"] <= owned["cleanup_latency_ms"] + 1

    # The report says what happened and what was kept.  It does not claim success.
    assert (report.outcome, report.plan_status, report.claims_success) == ("cancelled", "cancelled", False)
    assert {item.task_key: item.state for item in report.tasks} == {
        "done": "completed",
        "running": "cancelled",
        "waiting": "cancelled",
    }
    assert report.stop["kind"] == "cancelled"
    assert report.stop["reason"] == "operator pressed stop"
    assert report.stop["stopped_task_keys"] == ["running", "waiting"]
    assert report.stop["fenced_epochs"] == 1
    assert [item["artifact_ref"] for item in report.retained_outputs] == ["artifacts/done.json"]
    assert report.limitation_codes[0] == "mission_cancelled"
    assert all(item["next_action"] for item in report.limitations)

    # Finished work is untouched, and so is the root task.
    root_after = sm.store.get_task(root)
    assert root_after is not None
    assert (root_after.state, root_after.state_version) == (root_before.state, root_before.state_version)
    finished = sm.store.get_task(done.task.task_id)
    assert finished is not None and finished.state == "completed"

    # Dispatch is stopped in every layer that could start work.
    running_id = running.task.task_id
    lease = sm.store.get_epoch(running_id, running.epoch.epoch)
    assert lease is not None
    assert (lease.status, lease.fence_reason) == ("fenced", "mission_cancelled")
    last_transition = sm.store.list_transitions(running_id)[-1]
    assert (last_transition.from_state, last_transition.to_state) == ("running", "cancelled")
    assert json.loads(last_transition.evidence_json)["stop_code"] == "mission_cancelled"
    assert [item.ready for item in sm.list_mission_readiness(mission, 1)] == [False, False, False]
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        _ap007_claim(sm, root, mission, "waiting", tasks["waiting"], binding)
    with pytest.raises(execution_budget.ExecutionCancelled):
        sm.budgets.charge_hierarchical_tool_call(
            running_id, root_task_id=root, session_id=root, epoch=running.epoch.epoch
        )
    with pytest.raises(execution_budget.ExecutionCancelled):
        sm.budgets.charge_hierarchical_tool_call(tasks["waiting"].task_id, root_task_id=root, session_id=root)
    with pytest.raises(process_supervision.StaleOriginFencedError):
        supervisor.prepare_spawn_env(
            os.environ,
            supervision_id=supervisor.new_supervision_id(),
            session_id=root,
            task_id=running_id,
            epoch=running.epoch.epoch,
            worker_id=running.epoch.worker_id,
        )
    refused = side_effect_ledger.DurableActionLedger(studio.STORAGE, studio.DB_PATH).begin(
        action_id="ap007-after-stop",
        tool_name="run_bash",
        args={"command": "publish the report"},
        policy="autonomous",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id=root,
        task_id=running_id,
        epoch=running.epoch.epoch,
        worker_id=running.epoch.worker_id,
    )
    assert refused.proceed is False
    assert isinstance(refused.error, runtime_persistence.StaleEpochError)

    # As after a session cancel, the ended process leaves its workspace barrier held
    # until an operator has looked at what the interrupted work left behind.
    holds = supervisor.store.list_barrier_holds(task_id=running_id)
    assert len(holds) == 1
    assert holds[0].lingering_pids == ()
    assert supervisor.store.list_in_flight_processes() == []

    # A repeated stop changes nothing.  The first stop kind stands and nothing is measured again.
    again = sm.halt_mission(
        mission, 1, authority=_mission_authority(root), kind="bounded_stop", supervisor=supervisor
    )
    assert again.report_hash == report.report_hash
    assert again.stop == report.stop


def test_ap007_cleanup_latency_is_measured_from_the_stop_request(isolated_env: Path, tmp_path: Path) -> None:
    """AC1: the recorded durations come from the injected clock, in milliseconds from the request."""
    root, mission = "ap007-clock-root", "ap007-clock"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("only",))
    _ap007_claim(sm, root, mission, "only", tasks["only"], binding)
    ticks = iter([100.0, 100.25, 101.5])  # stop requested, dispatch fenced, containment finished

    report = sm.halt_mission(
        mission,
        1,
        authority=_mission_authority(root),
        supervisor=_ap007_supervisor(tmp_path),
        now=5_000.0,
        clock=lambda: next(ticks),
    )

    assert report.stop is not None
    owned = report.stop["owned_work"]
    assert owned["dispatch_fenced_ms"] == 250.0
    assert owned["cleanup_latency_ms"] == 1500.0
    assert owned["attempted_at"] == 5_000.0
    assert report.stop["requested_at"] == 5_000.0
    assert (owned["processes"], owned["cleanly_stopped"]) == (0, True)
    assert "retry" not in owned


def test_ap007_halt_reaches_delegated_descendants_and_their_processes(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC1: a plan owns the children its tasks delegated to; the async form contains them too."""
    root, mission = "ap007-family-root", "ap007-family"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("lead",))
    lead = _ap007_claim(sm, root, mission, "lead", tasks["lead"], binding)
    helper = sm.spawn_child_subtask(lead.task.task_id, "helper", title="Delegated helper")
    helper_lease, _ = sm.store.acquire_worker_lease(helper.task_id, "worker-helper")
    supervisor = _ap007_supervisor(tmp_path)
    origin = {
        "session_id": root,
        "task_id": helper.task_id,
        "epoch": helper_lease.epoch,
        "worker_id": "worker-helper",
    }

    async def _stop_while_the_helper_runs() -> tuple[workflow_state.MissionRecoveryReport, int | None]:
        supervision_id = supervisor.new_supervision_id()
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _AP007_IDLE_CHILD,
            start_new_session=True,
            env=supervisor.prepare_spawn_env(os.environ, supervision_id=supervision_id, **origin),
        )
        try:
            supervisor.register_process(
                child, supervision_id=supervision_id, tool_name="run_bash", workspace_root=isolated_env, **origin
            )
            stopped = await sm.halt_mission_async(
                mission, 1, authority=_mission_authority(root), supervisor=supervisor
            )
            return stopped, await asyncio.wait_for(child.wait(), timeout=5)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    report, exit_code = asyncio.run(_stop_while_the_helper_runs())

    assert exit_code is not None
    assert report.stop is not None
    assert report.stop["stopped_task_keys"] == ["lead"]
    assert report.stop["stopped_descendant_tasks"] == 1
    assert report.stop["fenced_epochs"] == 2
    owned = report.stop["owned_work"]
    assert (owned["processes"], owned["cleanly_stopped"], owned["origins_fenced"]) == (1, True, 2)
    stopped_helper = sm.store.get_task(helper.task_id)
    assert stopped_helper is not None and stopped_helper.state == "cancelled"
    fenced = sm.store.get_epoch(helper.task_id, helper_lease.epoch)
    assert fenced is not None and (fenced.status, fenced.fence_reason) == ("fenced", "mission_cancelled")
    with pytest.raises(execution_budget.ExecutionCancelled):
        sm.budgets.charge_hierarchical_tool_call(
            helper.task_id, root_task_id=root, session_id=root, epoch=helper_lease.epoch
        )
    with pytest.raises(workflow_state.WorkflowTransitionRejected, match="cancelled parent"):
        sm.spawn_child_subtask(lead.task.task_id, "late-helper")


def test_ap007_late_results_are_fenced_and_unresolved_effects_stay_uncertain(isolated_env: Path) -> None:
    """AC2: nothing a stopped worker reports afterwards is accepted, and an in-flight effect is not guessed."""
    root, mission = "ap007-late-root", "ap007-late"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("publish",))
    lease = _ap007_claim(sm, root, mission, "publish", tasks["publish"], binding)
    ledger = side_effect_ledger.DurableActionLedger(studio.STORAGE, studio.DB_PATH)
    _ap007_begin_action(ledger, "ap007-publish", lease, session_id=root)
    _ap007_begin_action(ledger, "ap007-read", lease, session_id=root, read_only=True)

    report = sm.halt_mission(mission, 1, authority=_mission_authority(root))

    assert report.stop is not None
    assert report.stop["uncertain_action_ids"] == ["ap007-publish"]
    assert report.unresolved_action_ids == ("ap007-publish",)
    assert "uncertain_effects" in report.limitation_codes
    assert report.claims_success is False
    publish = ledger.get_action("ap007-publish")
    read = ledger.get_action("ap007-read")
    assert publish is not None and read is not None
    assert (publish.status, publish.ok, publish.error_code) == ("uncertain", None, "cancelled_in_flight")
    # A read cannot have changed anything, so it is simply failed.
    assert (read.status, read.error_code) == ("failed", "cancelled_in_flight")

    # The worker finishes late.  None of its reports is accepted.
    late = {
        "binding": binding,
        "current_inputs": _AP007_INPUTS,
        "current_authority": _mission_authority(root, lease.task.task_id),
        "worker_id": lease.epoch.worker_id,
        "epoch": lease.epoch.epoch,
        "expected_state_version": lease.task.state_version,
    }
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        sm.complete_mission_task(
            mission,
            1,
            "publish",
            outputs=(workflow_state.MissionOutputRef("publish-out", "artifacts/publish.json", "b" * 64),),
            **late,
        )
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        sm.checkpoint_mission_task(mission, 1, "publish", step=1, payload={"progress": "late"}, **late)
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        sm.fail_mission_task(mission, 1, "publish", reason="late failure", **late)
    with pytest.raises(runtime_persistence.StaleEpochError, match="fenced worker"):
        sm.budgets.record_bounded_observation(
            lease.task.task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch, progressed=True
        )
    # A late "it worked" and a late exception both leave the effect uncertain.
    after_success = ledger.complete("ap007-publish", {"ok": True, "output": "published"}, 12)
    assert (after_success.status, after_success.ok, after_success.error_code) == (
        "uncertain",
        None,
        "cancelled_in_flight",
    )
    after_error = ledger.record_exception("ap007-publish", RuntimeError("connection reset"), 12)
    assert (after_error.status, after_error.ok) == ("uncertain", None)
    assert sm.mission_recovery_report(mission, 1).retained_outputs == ()

    # Only an explicit reconciliation settles the effect.  The mission stays cancelled.
    ledger.resolve_uncertain_action(
        "ap007-publish", resolution="verified_not_applied", reason="the target shows no new report"
    )
    settled = sm.mission_recovery_report(mission, 1)
    assert "uncertain_effects" not in settled.limitation_codes
    assert settled.unresolved_action_ids == ()
    assert (settled.outcome, settled.claims_success) == ("cancelled", False)


def test_ap007_stopped_plan_must_be_settled_before_new_work_is_planned(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC2: an unknown effect or an unverified stop holds every new plan on the same root task."""
    root, mission = "ap007-gate-root", "ap007-gate"
    sm = _ap007_machine(isolated_env, root)
    _ap007_machine(isolated_env, "ap007-gate-other-root")
    binding, tasks = _ap007_plan(sm, root, mission, keys=("publish",))
    lease = _ap007_claim(sm, root, mission, "publish", tasks["publish"], binding)
    ledger = side_effect_ledger.DurableActionLedger(studio.STORAGE, studio.DB_PATH)
    _ap007_begin_action(ledger, "ap007-gate-publish", lease, session_id=root)
    sm.halt_mission(mission, 1, authority=_mission_authority(root))  # no supervisor: nothing is attested

    with pytest.raises(workflow_state.MissionHaltUnsettled, match="ap007-gate-publish"):
        _ap007_plan(sm, root, mission, version=2, keys=("retry",))
    # A new mission name on the same root does not step around the hold.
    with pytest.raises(workflow_state.MissionHaltUnsettled, match="unresolved"):
        _ap007_plan(sm, root, "ap007-gate-renamed", keys=("retry",))
    # Another root task is not affected.
    _ap007_plan(sm, "ap007-gate-other-root", "ap007-gate-elsewhere", keys=("free",))

    ledger.resolve_uncertain_action(
        "ap007-gate-publish", resolution="verified_committed", reason="the report is visible at the target"
    )
    with pytest.raises(workflow_state.MissionHaltUnsettled, match="not attested"):
        _ap007_plan(sm, root, mission, version=2, keys=("retry",))
    unattested = sm.mission_recovery_report(mission, 1)
    assert "owned_work_not_terminated" in unattested.limitation_codes

    attested = sm.halt_mission(
        mission, 1, authority=_mission_authority(root), supervisor=_ap007_supervisor(tmp_path)
    )
    assert attested.stop is not None
    assert attested.stop["owned_work"]["cleanly_stopped"] is True
    assert attested.stop["owned_work"]["retry"] is True
    assert "owned_work_not_terminated" not in attested.limitation_codes

    _, retry_tasks = _ap007_plan(sm, root, mission, version=2, keys=("retry",))
    assert retry_tasks["retry"].state == "pending"
    # The stopped version keeps its own truthful record beside the new one.
    old = sm.mission_recovery_report(mission, 1)
    assert (old.outcome, old.plan_status) == ("cancelled", "cancelled")
    # Asking for the stopped version again returns it unchanged instead of reviving it.
    unchanged = sm.create_mission_plan(
        root,
        mission,
        1,
        (workflow_state.MissionTaskSpec("publish", "Task publish", ("publish-out",)),),
        inputs=_AP007_INPUTS,
        authority=_mission_authority(root),
    )
    assert unchanged.status == "cancelled"


def test_ap007_containment_retry_never_upgrades_a_lingering_stop_without_evidence(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC1/AC5: "no process found" on a retry is not proof that an earlier survivor ended."""
    root, mission = "ap007-linger-root", "ap007-linger"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("stuck",))
    lease = _ap007_claim(sm, root, mission, "stuck", tasks["stuck"], binding)

    # A process that ignores every signal, modelled by a probe double with a fictional pid.
    stuck_pid, stuck_fingerprint = 999_407, "ap007-fixture-start"
    still_alive = {"value": True}

    def _stuck_probe() -> process_supervision.ProcessProbe:
        return process_supervision.ProcessProbe(
            pid=stuck_pid,
            ppid=1,
            pgid=stuck_pid,
            uid=os.getuid(),
            state="D",
            start_fingerprint=stuck_fingerprint,
            alive=True,
        )

    def _probe(pid: int) -> process_supervision.ProcessProbe | None:
        if pid == stuck_pid:
            return _stuck_probe() if still_alive["value"] else None
        return process_supervision.probe_process(pid)

    store = process_supervision.ContainmentStore(tmp_path / "ap007_containment", probe_fn=_probe)
    supervisor = process_supervision.ProcessSupervisor(
        store,
        probe_fn=_probe,
        discover_fn=lambda _record: ([], [_stuck_probe()] if still_alive["value"] else []),
        signal_fn=lambda _target, _signal, _is_group: None,
        grace_timeout_s=0.02,
        kill_timeout_s=0.05,
    )
    store.record_process_started(
        process_supervision.SupervisedProcessRecord(
            supervision_id="sup-a0070000000000a7",
            pid=stuck_pid,
            pgid=stuck_pid,
            uid=os.getuid(),
            start_fingerprint=stuck_fingerprint,
            container_name=None,
            tool_name="run_bash",
            session_id=root,
            task_id=lease.task.task_id,
            epoch=lease.epoch.epoch,
            worker_id=lease.epoch.worker_id,
            boot_id="ap007-fixture-boot",
            action_id="ap007-linger-action",
            workspace_root=str(isolated_env),
            barriers=(),
            started_at=time.time(),
        )
    )

    first = sm.halt_mission(mission, 1, authority=_mission_authority(root), supervisor=supervisor)

    assert first.stop is not None
    owned = first.stop["owned_work"]
    assert (owned["processes"], owned["escalated"], owned["lingering"]) == (1, 1, 1)
    assert owned["lingering_pids"] == [[stuck_pid, stuck_fingerprint]]
    assert owned["cleanly_stopped"] is False
    assert "owned_work_lingering" in first.limitation_codes
    assert first.claims_success is False

    # The supervisor no longer tracks the process, so a retry finds nothing to terminate.
    # The survivor is re-checked by identity and still counts.
    second = sm.halt_mission(mission, 1, authority=_mission_authority(root), supervisor=supervisor)
    assert second.stop is not None
    retried = second.stop["owned_work"]
    assert (retried["processes"], retried["lingering"], retried["retry"]) == (0, 1, True)
    assert retried["lingering_pids"] == [[stuck_pid, stuck_fingerprint]]
    assert retried["cleanly_stopped"] is False
    assert "owned_work_lingering" in second.limitation_codes
    with pytest.raises(workflow_state.MissionHaltUnsettled, match="not verified as stopped"):
        _ap007_plan(sm, root, mission, version=2, keys=("retry",))

    # A stop without a supervisor cannot replace the supervised record either.
    unsupervised = sm.halt_mission(mission, 1, authority=_mission_authority(root))
    assert unsupervised.stop == second.stop

    still_alive["value"] = False
    third = sm.halt_mission(mission, 1, authority=_mission_authority(root), supervisor=supervisor)
    assert third.stop is not None
    verified = third.stop["owned_work"]
    assert (verified["lingering"], verified["lingering_pids"], verified["cleanly_stopped"]) == (0, [], True)
    assert "owned_work_lingering" not in third.limitation_codes
    _ap007_plan(sm, root, mission, version=2, keys=("retry",))
    # Once a clean stop is on record, later calls do not measure again.
    assert sm.halt_mission(
        mission, 1, authority=_mission_authority(root), supervisor=supervisor
    ).report_hash == third.report_hash


def test_ap007_lingering_count_without_identities_is_carried_forward() -> None:
    """A survivor that cannot be re-checked stays counted; it is never assumed gone."""
    listed, unlisted = workflow_state._mission_prior_lingering(
        {"owned_work": {"lingering": 3, "lingering_pids": [[4242, "fp"], ["bad", "fp"], [True, "fp"]]}}
    )
    assert (listed, unlisted) == ([(4242, "fp")], 2)
    assert workflow_state._mission_prior_lingering({"owned_work": {"termination_attempted": False}}) == ([], 0)
    assert workflow_state._mission_prior_lingering({}) == ([], 0)

    summary = workflow_state._mission_owned_work(
        [],
        origins_fenced=0,
        errors=0,
        started=1.0,
        fenced=1.0,
        done=1.0,
        retry=True,
        attempted_at=10.0,
        carried=(),
        carried_unlisted=unlisted,
    )
    assert (summary["processes"], summary["lingering"], summary["cleanly_stopped"]) == (0, 2, False)


def test_ap007_containment_failure_is_reported_and_the_durable_stop_stands(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC1/AC5: when containment itself fails, dispatch stays fenced and the report says so."""
    root, mission = "ap007-error-root", "ap007-error"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("only",))
    _ap007_claim(sm, root, mission, "only", tasks["only"], binding)

    class _FailingSupervisor(process_supervision.ProcessSupervisor):
        def contain_by_scope_sync(self, **_scope: Any) -> list[process_supervision.TerminationOutcome]:
            raise OSError("containment store is unavailable")

    report = sm.halt_mission(
        mission,
        1,
        authority=_mission_authority(root),
        supervisor=_FailingSupervisor(process_supervision.ContainmentStore(tmp_path / "ap007_containment")),
    )

    assert (report.outcome, report.plan_status) == ("cancelled", "cancelled")
    assert report.stop is not None
    owned = report.stop["owned_work"]
    assert (owned["termination_attempted"], owned["errors"], owned["cleanly_stopped"]) == (True, 1, False)
    assert "owned_work_lingering" in report.limitation_codes
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        _ap007_claim(sm, root, mission, "only", tasks["only"], binding)
    # A working supervisor can still settle the stop afterwards.
    retried = sm.halt_mission(
        mission, 1, authority=_mission_authority(root), supervisor=_ap007_supervisor(tmp_path)
    )
    assert retried.stop is not None and retried.stop["owned_work"]["cleanly_stopped"] is True


@pytest.mark.parametrize(
    ("code", "limits", "observations"),
    [
        ("no_progress", {"max_no_progress": 2}, [{"progressed": False}] * 3),
        ("repair_attempts", {"max_repair_attempts": 1}, [{"repair_attempt": True}] * 2),
        ("memory", {"max_memory_bytes": 1_000}, [{"memory_bytes": 1_000}, {"memory_bytes": 1_001}]),
        ("storage", {"max_storage_bytes": 2_000}, [{"storage_bytes": 2_000}, {"storage_bytes": 2_001}]),
        ("measurement_unavailable", {"max_memory_bytes": 1_000}, [{"unmeasured": ("memory",)}]),
        ("measurement_unavailable", {"max_storage_bytes": 2_000}, [{"unmeasured": ("storage",)}]),
    ],
)
def test_ap007_declared_limits_stop_the_mission_boundedly(
    isolated_env: Path,
    code: str,
    limits: dict[str, int],
    observations: list[dict[str, Any]],
) -> None:
    """AC3: each limit stops at its declared bound, durably, with a reason and a next step."""
    root, mission = f"ap007-{code}-root", f"ap007-{code}"
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, **limits))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work", "next"))
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding)
    observe = {"root_task_id": root, "session_id": root, "epoch": lease.epoch.epoch}

    # A caller cannot invent a bounded stop: the budgets have to require it.
    with pytest.raises(workflow_state.WorkflowTransitionRejected, match="No declared limit requires"):
        sm.halt_mission(mission, 1, authority=_mission_authority(root), kind="bounded_stop")

    for within_bound in observations[:-1]:
        assert sm.budgets.record_bounded_observation(lease.task.task_id, **observe, **within_bound).stopped is False
    with pytest.raises(execution_budget.BoundedStopRequired) as stopped:
        sm.budgets.record_bounded_observation(lease.task.task_id, **observe, **observations[-1])
    assert stopped.value.verdict.code == code
    assert stopped.value.verdict.scope == "leaf"

    # The stop is durable before anyone halts the plan: work is refused on its own.
    assert sm.budgets.get_budget(root).bounded_stop() is not None
    with pytest.raises(execution_budget.BudgetExceeded):
        sm.budgets.charge_hierarchical_tool_call(
            lease.task.task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch
        )
    with pytest.raises(workflow_state.MissionBindingRejected):
        _ap007_claim(sm, root, mission, "next", tasks["next"], binding)
    pending = sm.mission_recovery_report(mission, 1)
    assert (pending.outcome, pending.claims_success) == ("stopped", False)
    assert pending.limitation_codes[:2] == (code, "halt_required")

    report = sm.halt_mission(
        mission, 1, authority=_mission_authority(root), kind="bounded_stop", reason="scheduler noticed the stop"
    )

    assert (report.outcome, report.plan_status, report.claims_success) == ("stopped", "failed", False)
    assert report.stop is not None
    assert (report.stop["kind"], report.stop["code"], report.stop["scope"]) == ("bounded_stop", code, "root")
    assert report.stop["note"] == "scheduler noticed the stop"
    assert report.limitation_codes[0] == code
    assert "halt_required" not in report.limitation_codes
    assert all(item["message"] and item["next_action"] for item in report.limitations)
    lease_row = sm.store.get_epoch(lease.task.task_id, lease.epoch.epoch)
    assert lease_row is not None and lease_row.fence_reason == f"mission_bounded_stop:{code}"

    # The same stop is read back by a new process.
    restarted = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    after_restart = restarted.mission_recovery_report(mission, 1)
    assert after_restart.stop == report.stop
    assert (after_restart.outcome, after_restart.limitation_codes) == (report.outcome, report.limitation_codes)


def test_ap007_deadline_stops_claims_dispatch_and_resume_without_any_observation(
    isolated_env: Path,
) -> None:
    """AC3: the deadline is enforced from the clock alone; nobody has to report it."""
    root, mission = "ap007-deadline-root", "ap007-deadline"
    deadline = time.time() + 3_600
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, deadline_at=deadline))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work", "next"))
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding)
    late = deadline + 1

    # Before the deadline everything proceeds.
    sm.budgets.charge_hierarchical_tool_call(
        lease.task.task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch
    )
    assert sm.mission_recovery_report(mission, 1, persist=False).outcome == "in_progress"

    with pytest.raises(execution_budget.BudgetExceeded, match="deadline exceeded"):
        sm.budgets.charge_hierarchical_tool_call(
            lease.task.task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch, now=late
        )
    with pytest.raises(workflow_state.MissionBindingRejected, match="deadline exceeded"):
        _ap007_claim(sm, root, mission, "next", tasks["next"], binding, now=late)
    with pytest.raises(execution_budget.BoundedStopRequired) as stopped:
        sm.budgets.record_bounded_observation(
            lease.task.task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch, progressed=True, now=late
        )
    assert stopped.value.verdict.code == "deadline"

    report = sm.halt_mission(mission, 1, authority=_mission_authority(root), kind="bounded_stop", now=late)

    assert (report.outcome, report.plan_status) == ("stopped", "failed")
    assert report.stop is not None
    assert (report.stop["code"], report.stop["scope"]) == ("deadline", "root")
    # The stored reason names the deadline, not the time of evaluation, so the record is stable.
    assert report.stop["reason"] == f"deadline exceeded (deadline_at={deadline:.2f})"
    assert sm.mission_recovery_report(mission, 1, now=late + 500).report_hash == report.report_hash


def test_ap007_one_task_at_its_limit_stops_the_plan_with_task_scope(isolated_env: Path) -> None:
    """AC3: a task can reach its own bound while the mission as a whole is still progressing."""
    root, mission = "ap007-scope-root", "ap007-scope"
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, max_no_progress=2))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("stalled", "moving"))
    stalled = _ap007_claim(sm, root, mission, "stalled", tasks["stalled"], binding)
    moving = _ap007_claim(sm, root, mission, "moving", tasks["moving"], binding)

    def _observe(lease: workflow_state.MissionTaskLease, progressed: bool) -> execution_budget.BoundedStopVerdict:
        return sm.budgets.record_bounded_observation(
            lease.task.task_id,
            root_task_id=root,
            session_id=root,
            epoch=lease.epoch.epoch,
            progressed=progressed,
            enforce=False,
        )

    assert _observe(stalled, False).stopped is False
    assert _observe(moving, True).stopped is False  # the mission as a whole made progress
    assert _observe(stalled, False).stopped is False
    verdict = _observe(stalled, False)
    assert (verdict.stopped, verdict.code, verdict.scope) == (True, "no_progress", "leaf")
    assert sm.budgets.get_budget(root).bounded_stop() is None

    # A later "progress" report cannot undo the stop of that task.
    assert _observe(stalled, True).stopped is True
    assert sm.budgets.get_budget(stalled.task.task_id).no_progress_count == 3

    live = sm.mission_recovery_report(mission, 1)
    assert "no_progress" in live.limitation_codes and live.claims_success is False
    report = sm.halt_mission(mission, 1, authority=_mission_authority(root), kind="bounded_stop")
    assert report.stop is not None
    assert (report.stop["code"], report.stop["scope"]) == ("no_progress", "task:stalled")
    assert report.stop["stopped_task_keys"] == ["stalled", "moving"]
    assert (report.outcome, report.plan_status) == ("stopped", "failed")


def test_ap007_unmeasurable_ceiling_stops_until_a_real_reading_arrives(isolated_env: Path) -> None:
    """AC3: a ceiling that cannot be measured stops the work; only a reading clears the gap."""
    root = "ap007-gap-root"
    sm = _ap007_machine(
        isolated_env,
        root,
        execution_budget.TaskBudgetSpec(max_steps=50, max_memory_bytes=1_000, max_storage_bytes=2_000),
    )
    observe = {"root_task_id": root, "session_id": root, "enforce": False}

    gap = sm.budgets.record_bounded_observation(root, unmeasured=("memory",), **observe)
    assert (gap.stopped, gap.code, gap.scope) == (True, "measurement_unavailable", "leaf")
    assert sm.budgets.get_budget(root).unmeasured == ("memory",)

    # Progress, or a reading for a different resource, does not close the gap.
    assert sm.budgets.record_bounded_observation(root, progressed=True, **observe).code == "measurement_unavailable"
    assert sm.budgets.record_bounded_observation(root, storage_bytes=10, **observe).code == "measurement_unavailable"
    assert sm.budgets.get_budget(root).storage_bytes_peak == 0  # a stopped budget is not mutated
    with pytest.raises(execution_budget.BudgetExceeded, match="could not be measured"):
        sm.budgets.charge_hierarchical_tool_call(root, root_task_id=root, session_id=root)

    cleared = sm.budgets.record_bounded_observation(root, memory_bytes=400, **observe)
    assert cleared.stopped is False
    state = sm.budgets.get_budget(root)
    assert (state.unmeasured, state.memory_bytes_peak) == ((), 400)
    sm.budgets.charge_hierarchical_tool_call(root, root_task_id=root, session_id=root)

    # A reading that closes the gap is still judged against the ceiling.
    assert sm.budgets.record_bounded_observation(root, unmeasured=("memory",), **observe).stopped is True
    breach = sm.budgets.record_bounded_observation(root, memory_bytes=5_000, **observe)
    assert (breach.stopped, breach.code) == (True, "memory")
    # A peak is a recorded fact: a lower reading afterwards does not clear the breach.
    assert sm.budgets.record_bounded_observation(root, memory_bytes=1, **observe).code == "memory"
    assert sm.budgets.get_budget(root).memory_bytes_peak == 5_000

    # A marker for a ceiling that was never declared is ignored instead of stopping the work.
    plain_root = "ap007-gap-plain-root"
    plain = _ap007_machine(isolated_env, plain_root)
    ignored = plain.budgets.record_bounded_observation(
        plain_root, root_task_id=plain_root, session_id=plain_root, unmeasured=("memory", "storage")
    )
    assert ignored.stopped is False
    assert plain.budgets.get_budget(plain_root).unmeasured == ()
    with pytest.raises(execution_budget.BudgetError, match="both measured and unmeasured"):
        sm.budgets.record_bounded_observation(root, memory_bytes=1, unmeasured=("memory",), **observe)


def test_ap007_stale_or_fenced_workers_cannot_record_observations(isolated_env: Path) -> None:
    """AC2/AC3: a superseded worker can neither reset a counter nor hide a breach."""
    root, mission = "ap007-observer-root", "ap007-observer"
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, max_no_progress=5))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work", "idle"))
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding)
    task_id = lease.task.task_id
    observe = {"root_task_id": root, "session_id": root}

    sm.budgets.record_bounded_observation(task_id, epoch=lease.epoch.epoch, progressed=False, **observe)
    with pytest.raises(runtime_persistence.StaleEpochError, match="Stale or missing epoch"):
        sm.budgets.record_bounded_observation(task_id, progressed=True, **observe)
    with pytest.raises(runtime_persistence.StaleEpochError, match="has no worker epoch"):
        sm.budgets.record_bounded_observation(tasks["idle"].task_id, epoch=1, progressed=True, **observe)
    with pytest.raises(execution_budget.BudgetError, match="mismatched root"):
        sm.budgets.record_bounded_observation(
            task_id, root_task_id=task_id, session_id=root, epoch=lease.epoch.epoch, progressed=True
        )
    with pytest.raises(execution_budget.BudgetError, match="belongs to session"):
        sm.budgets.record_bounded_observation(
            task_id, root_task_id=root, session_id="another-session", epoch=lease.epoch.epoch, progressed=True
        )

    successor, _ = sm.store.acquire_worker_lease(task_id, "worker-successor", force_takeover=True)
    with pytest.raises(runtime_persistence.StaleEpochError):
        sm.budgets.record_bounded_observation(task_id, epoch=lease.epoch.epoch, progressed=True, **observe)
    assert sm.budgets.get_budget(task_id).no_progress_count == 1
    sm.budgets.record_bounded_observation(task_id, epoch=successor.epoch, progressed=True, **observe)
    assert sm.budgets.get_budget(task_id).no_progress_count == 0


def test_ap007_cancellation_and_isolation_bind_the_whole_task_chain(isolated_env: Path) -> None:
    """AC1/AC4: a delegated child cannot run because its own budget happens to be silent."""
    root, mission = "ap007-chain-root", "ap007-chain"
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, isolation_required=True))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("lead",))
    lead = _ap007_claim(sm, root, mission, "lead", tasks["lead"], binding, isolation=_AP007_FIXTURE_ISOLATION)
    helper = sm.spawn_child_subtask(lead.task.task_id, "helper")
    charge = {"root_task_id": root, "session_id": root}
    # The delegated child's own budget declares nothing; the requirement comes from its ancestors.
    assert sm.budgets.get_budget(helper.task_id).spec.isolation_required is False
    assert sm.budgets.get_budget(lead.task.task_id).spec.isolation_required is True

    with pytest.raises(execution_budget.IsolationUnavailable, match="host shell is not used"):
        sm.budgets.charge_hierarchical_tool_call(helper.task_id, **charge)
    assert sm.budgets.get_budget(root).tool_calls_used == 0
    sm.budgets.charge_hierarchical_tool_call(helper.task_id, isolation=_AP007_FIXTURE_ISOLATION, **charge)
    assert sm.budgets.get_budget(root).tool_calls_used == 1

    # Cancelling the task in the middle of the chain stops the child below it.
    sm.budgets.set_control_flags(lead.task.task_id, cancel_requested=True)
    with pytest.raises(execution_budget.ExecutionCancelled):
        sm.budgets.charge_hierarchical_tool_call(helper.task_id, isolation=_AP007_FIXTURE_ISOLATION, **charge)
    with pytest.raises(execution_budget.ExecutionCancelled):
        sm.budgets.record_bounded_observation(helper.task_id, progressed=True, **charge)
    sm.budgets.set_control_flags(lead.task.task_id, cancel_requested=False)

    # One unreadable budget anywhere in the chain refuses the dispatch.
    with sqlite3.connect(studio.DB_PATH) as conn:
        conn.execute("UPDATE runtime_tasks SET budget_json = ? WHERE task_id = ?", ("{not json", lead.task.task_id))
    with pytest.raises(execution_budget.BudgetError, match="malformed"):
        sm.budgets.charge_hierarchical_tool_call(helper.task_id, isolation=_AP007_FIXTURE_ISOLATION, **charge)
    with pytest.raises(execution_budget.BudgetError, match="malformed"):
        sm.budgets.record_bounded_observation(helper.task_id, progressed=True, **charge)
    assert sm.budgets.get_budget(root).tool_calls_used == 1


def test_ap007_missing_isolation_blocks_dependent_execution_without_host_fallback(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC4: with no qualified isolated runtime, nothing is claimed, dispatched or moved to the host."""
    root, mission = "ap007-isolation-root", "ap007-isolation"
    sm = _ap007_machine(isolated_env, root, execution_budget.TaskBudgetSpec(max_steps=50, isolation_required=True))
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work",))
    task_id = tasks["work"].task_id

    # What the real security store says today: no backend is registered or qualified.
    incidents = sandbox_boundary.SecurityIncidentStore(tmp_path / "ap007_security")
    observed: list[execution_budget.IsolationEvidence | None] = [None]
    for mode in ("sandbox", "host", "disabled"):
        decision = incidents.check_admission(
            required_guarantees=("container_boundary",),
            execution_surface="run_bash",
            execution_mode=mode,
            session_id=root,
            workspace_root=isolated_env,
        )
        assert decision.container_qualified is False and decision.active_backend is None
        observed.append(execution_budget.IsolationEvidence.from_admission(decision, execution_mode=mode))

    for evidence in observed:
        assert execution_budget.isolation_refusal(evidence)
        with pytest.raises(workflow_state.MissionIsolationRejected, match="host shell is not used"):
            _ap007_claim(sm, root, mission, "work", tasks["work"], binding, isolation=evidence)
        with pytest.raises(execution_budget.IsolationUnavailable, match="host shell is not used"):
            sm.budgets.charge_hierarchical_tool_call(task_id, root_task_id=root, session_id=root, isolation=evidence)

    # Nothing started: no lease, no state change, no charge.
    untouched = sm.store.get_task(task_id)
    assert untouched is not None
    assert (untouched.state, untouched.current_epoch, untouched.state_version) == ("pending", 0, 0)
    assert sm.store.list_epochs(task_id) == []
    assert sm.budgets.get_budget(root).tool_calls_used == 0
    assert sm.budgets.get_budget(task_id).tool_calls_used == 0

    report = sm.mission_recovery_report(mission, 1, isolation=observed[1])
    assert (report.outcome, report.claims_success) == ("blocked", False)
    blocked = next(item for item in report.limitations if item["code"] == "isolation_unavailable")
    assert "host shell is not used" in blocked["message"]
    assert "not executed on the host" in blocked["next_action"]
    # Without evidence the report reaches the same conclusion.
    assert sm.mission_recovery_report(mission, 1).outcome == "blocked"

    # With proof (a test fixture, not a real runtime) the same task is claimable, and
    # every dispatch has to present the proof again.
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding, isolation=_AP007_FIXTURE_ISOLATION)
    with pytest.raises(execution_budget.IsolationUnavailable):
        sm.budgets.charge_hierarchical_tool_call(
            task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch
        )
    sm.budgets.charge_hierarchical_tool_call(
        task_id, root_task_id=root, session_id=root, epoch=lease.epoch.epoch, isolation=_AP007_FIXTURE_ISOLATION
    )
    assert sm.mission_recovery_report(mission, 1, isolation=_AP007_FIXTURE_ISOLATION).outcome == "in_progress"


def test_ap007_recovery_report_claims_success_only_for_checked_complete_artifacts(
    isolated_env: Path, tmp_path: Path
) -> None:
    """AC5: finished tasks alone are not success; every retained output must be a checked artifact."""
    root, mission = "ap007-report-root", "ap007-report"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("draft", "final"))
    checked, unchecked = "c" * 64, "d" * 64
    registry = artifact_lineage.ArtifactRegistry(tmp_path / "ap007_artifacts")
    for sha in (checked, unchecked):
        registry.record_artifact(
            sha,
            source_kind="pipeline",
            source_ref="ap007-report",
            trust="trusted",
            confidentiality="internal",
            producer="workflow:report-builder",
            producer_version="1.0.0",
            config_sha256="1" * 64,
            inputs={"request.json": "v1"},
            required_checks=("render",),
        )
    registry.record_check(checked, "render", outcome="passed")

    draft = _ap007_claim(sm, root, mission, "draft", tasks["draft"], binding)
    _ap007_complete(sm, root, mission, "draft", draft, binding, checked)
    partial = sm.mission_recovery_report(mission, 1, artifact_status=registry.artifact_status)
    assert (partial.outcome, partial.claims_success) == ("in_progress", False)
    assert partial.limitation_codes == ("tasks_incomplete",)
    assert [item["counts_as_complete"] for item in partial.retained_outputs] == [True]

    final = _ap007_claim(sm, root, mission, "final", tasks["final"], binding)
    _ap007_complete(sm, root, mission, "final", final, binding, unchecked)
    assert sm.evaluate_mission_join(mission, 1).status == "succeeded"

    # Every task finished, but nobody checked the artifacts: that is not success.
    unverified = sm.mission_recovery_report(mission, 1)
    assert (unverified.outcome, unverified.claims_success) == ("unverified", False)
    assert unverified.limitation_codes == ("artifacts_not_verified",)
    # One retained output has not passed its declared check.
    incomplete = sm.mission_recovery_report(mission, 1, artifact_status=registry.artifact_status)
    assert (incomplete.outcome, incomplete.claims_success) == ("partial", False)
    assert incomplete.limitation_codes == ("artifact_incomplete",)
    assert [item["artifact_state"] for item in incomplete.retained_outputs] == ["complete", "partial"]
    assert "final:final-out" in incomplete.limitations[0]["message"]

    # A checker that cannot answer is never read as "complete".
    def _unavailable(_sha256: str) -> Any:
        raise OSError("artifact store is unreadable")

    unknown = sm.mission_recovery_report(mission, 1, artifact_status=_unavailable)
    assert (unknown.outcome, unknown.claims_success) == ("partial", False)
    assert {item["artifact_state"] for item in unknown.retained_outputs} == {"unknown"}

    registry.record_check(unchecked, "render", outcome="passed")
    succeeded = sm.mission_recovery_report(mission, 1, artifact_status=registry.artifact_status)
    assert (succeeded.outcome, succeeded.claims_success, succeeded.limitations) == ("succeeded", True, ())
    assert len(succeeded.retained_outputs) == 2
    # A plan that already succeeded has nothing to halt.
    with pytest.raises(workflow_state.WorkflowTransitionRejected, match="already succeeded"):
        sm.halt_mission(mission, 1, authority=_mission_authority(root))
    # An unresolved effect turns a finished mission back into a partial one.
    ledger = side_effect_ledger.DurableActionLedger(studio.STORAGE, studio.DB_PATH)
    ledger.stage_never_started(
        "ap007-report-effect",
        tool_name="run_bash",
        args={"command": "notify the reviewers"},
        policy="autonomous",
        task_id=final.task.task_id,
        session_id=root,
        epoch=final.epoch.epoch,
    )
    with sqlite3.connect(studio.DB_PATH) as conn:
        conn.execute("UPDATE runtime_actions SET status = 'uncertain' WHERE action_id = 'ap007-report-effect'")
    doubtful = sm.mission_recovery_report(mission, 1, artifact_status=registry.artifact_status)
    assert (doubtful.outcome, doubtful.claims_success) == ("partial", False)
    assert doubtful.limitation_codes == ("uncertain_effects",)


def test_ap007_report_names_a_required_stop_that_nobody_carried_out(isolated_env: Path) -> None:
    """AC5: a cancelled root with a live plan is reported as not yet stopped, never as stopped."""
    root, mission = "ap007-unhalted-root", "ap007-unhalted"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work",))
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding)

    sm.cancel_task(root)

    live = sm.mission_recovery_report(mission, 1)
    assert (live.outcome, live.plan_status, live.claims_success) == ("cancelled", "active", False)
    assert live.limitation_codes[:2] == ("mission_cancelled", "halt_required")
    required = next(item for item in live.limitations if item["code"] == "halt_required")
    assert "not fenced" in required["message"]
    assert live.stop is None

    # Stale policy generations may always stop work; only the identity has to match.
    stale_authority = _mission_authority(root, session_revision=1, global_revision=1)
    report = sm.halt_mission(mission, 1, authority=stale_authority)
    assert "halt_required" not in report.limitation_codes
    assert (report.outcome, report.plan_status) == ("cancelled", "cancelled")
    fenced = sm.store.get_epoch(lease.task.task_id, lease.epoch.epoch)
    assert fenced is not None and fenced.status == "fenced"


def test_ap007_halt_rejects_unknown_plans_wrong_identity_and_finished_versions(isolated_env: Path) -> None:
    root, mission = "ap007-reject-root", "ap007-reject"
    sm = _ap007_machine(isolated_env, root)
    _ap007_machine(isolated_env, "ap007-reject-stranger")
    _, tasks = _ap007_plan(sm, root, mission, keys=("work",))
    authority = _mission_authority(root)

    with pytest.raises(workflow_state.WorkflowStateError, match="Unsupported mission halt kind"):
        sm.halt_mission(mission, 1, authority=authority, kind="paused")
    with pytest.raises(workflow_state.MissionGraphError, match="does not exist"):
        sm.halt_mission("ap007-reject-missing", 1, authority=authority)
    with pytest.raises(workflow_state.MissionBindingRejected, match="envelope is required"):
        sm.halt_mission(mission, 1, authority={"session_id": root})  # type: ignore[arg-type]
    for wrong in (
        _mission_authority("ap007-reject-stranger"),
        _mission_authority(root, tasks["work"].task_id),  # a task-scoped envelope cannot stop the mission
        execution_spine.MissionAuthorityEnvelope("another-session", root, root, 7, 11),
    ):
        with pytest.raises(workflow_state.MissionBindingRejected, match="does not match"):
            sm.halt_mission(mission, 1, authority=wrong)
    plan = sm.get_mission_plan(mission, 1)
    assert plan is not None and plan.status == "active"

    # A superseded version was already fenced by its successor; there is nothing left to halt.
    _ap007_plan(sm, root, mission, version=2, keys=("work",))
    with pytest.raises(workflow_state.WorkflowTransitionRejected, match="'superseded'"):
        sm.halt_mission(mission, 1, authority=authority)
    superseded = sm.mission_recovery_report(mission, 1)
    assert (superseded.outcome, superseded.claims_success) == ("superseded", False)
    assert superseded.limitation_codes[0] == "plan_superseded"


def test_ap007_halted_plan_is_not_revived_by_recovery_or_resume(isolated_env: Path) -> None:
    root, mission = "ap007-revive-root", "ap007-revive"
    sm = _ap007_machine(isolated_env, root)
    binding, tasks = _ap007_plan(sm, root, mission, keys=("work", "next"), deps=(("next", "work"),))
    lease = _ap007_claim(sm, root, mission, "work", tasks["work"], binding)
    sm.halt_mission(mission, 1, authority=_mission_authority(root))

    restarted = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    sweep = restarted.recover_incomplete_transitions()
    assert lease.task.task_id not in sweep.recovered_task_ids
    stopped = restarted.store.get_task(lease.task.task_id)
    assert stopped is not None and stopped.state == "cancelled"
    assert [item.ready for item in restarted.list_mission_readiness(mission, 1)] == [False, False]
    with pytest.raises(workflow_state.MissionBindingRejected, match="not active"):
        restarted.prepare_mission_task_resume(
            mission,
            1,
            "work",
            binding=binding,
            current_inputs=_AP007_INPUTS,
            current_authority=_mission_authority(root, lease.task.task_id),
            worker_id="worker-resume",
            expected_state_version=stopped.state_version,
        )
    plan = restarted.get_mission_plan(mission, 1)
    assert plan is not None and plan.status == "cancelled"


def test_ap007_control_operations_keep_bounded_counters(isolated_env: Path) -> None:
    """Counters that decide a stop survive every other write to the same budget document."""
    root = "ap007-counters-root"
    spec = execution_budget.TaskBudgetSpec(
        max_steps=50, max_no_progress=9, max_repair_attempts=9, max_memory_bytes=10_000, max_storage_bytes=10_000
    )
    sm = _ap007_machine(isolated_env, root, spec)
    sm.budgets.record_bounded_observation(
        root,
        root_task_id=root,
        session_id=root,
        progressed=False,
        repair_attempt=True,
        memory_bytes=512,
        storage_bytes=2_048,
    )
    expected = (1, 1, 512, 2_048)

    def _counters() -> tuple[int, int, int, int]:
        state = sm.budgets.get_budget(root)
        return (
            state.no_progress_count,
            state.repair_attempts_used,
            state.memory_bytes_peak,
            state.storage_bytes_peak,
        )

    assert _counters() == expected
    sm.budgets.set_control_flags(root, pause_requested=True)
    sm.budgets.set_control_flags(root, pause_requested=False)
    assert _counters() == expected
    sm.budgets.record_usage(root, steps_delta=1, tool_calls_delta=1, tokens_delta=10, elapsed_delta_s=0.5)
    assert _counters() == expected
    sm.budgets.charge_hierarchical_tool_call(root, root_task_id=root, session_id=root)
    assert _counters() == expected
    sm.budgets.consume_recovery(root)
    assert _counters() == expected
    # A budget change keeps what was already measured: raising a ceiling does not erase the peak.
    sm.ensure_root_task(
        root,
        session_id=root,
        workspace_dir=str(isolated_env),
        budget_spec=replace(spec, max_memory_bytes=20_000),
        override_budget_spec=True,
    )
    assert _counters() == expected
    restarted = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH)
    assert restarted.budgets.get_budget(root).spec.max_memory_bytes == 20_000
    assert restarted.budgets.get_budget(root).memory_bytes_peak == 512


def test_ap007_task_budget_route_refuses_limits_that_no_route_enforces(isolated_env: Path) -> None:
    """The HTTP boundary does not accept a protection it cannot deliver, and does not ignore it either."""
    session_id = _create_session(isolated_env, title="AP-007 boundary")["id"]
    values: dict[str, Any] = {
        "max_no_progress": 3,
        "max_repair_attempts": 2,
        "deadline_at": time.time() + 600,
        "max_memory_bytes": 1_000_000,
        "max_storage_bytes": 1_000_000,
        "isolation_required": True,
    }
    assert tuple(values) == execution_budget.BOUNDED_LIMIT_FIELDS

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        for route in ("/api/harness/run", "/api/harness/gsd-wave"):
            for key, value in values.items():
                response = client.post(
                    route,
                    headers=_headers(),
                    json={"session_id": session_id, "prompt": "Summarise the notes", "budget": {key: value}},
                )
                assert response.status_code == 400, (route, key)
                detail = response.json()["detail"]
                assert key in detail and "not activated" in detail
        # ``false`` is still a declaration of the key, so it is refused the same way.
        declared_false = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": session_id, "prompt": "Summarise the notes", "budget": {"isolation_required": False}},
        )
        assert declared_false.status_code == 400

    # The refused requests left the task and its budget exactly as they were.
    task = workflow_state.DurableTaskStateMachine(studio.STORAGE, studio.DB_PATH).store.get_task(session_id)
    assert task is not None
    assert (task.state, task.budget_json, task.current_epoch) == ("pending", "{}", 0)
