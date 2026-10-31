"""End-to-end verification for M5-P4-03: Bounded child delegation and parent joins.

Covers all 4 implementation tasks and all 3 acceptance criteria:
- Task 1: Define child task budget and authority subset
- Task 2: Implement durable join/cancel propagation
- Task 3: Prevent conflicting workspace mutations
- Task 4: Benchmark vs single-agent baseline
- AC 1: Child cannot exceed parent permissions
- AC 2: No orphan consequential actions
- AC 3: Delegation preserves or improves verified outcomes
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import pytest

import app as studio
from capability_negotiation import TaskRequirements
import child_delegation as cd
import execution_budget
from model_comparison import RubricDimension
import workflow_evaluation as we
import workflow_state
import workspace_quarantine

AUTH_TOKEN = "d" * 40


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "allowed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ws / "src" / "forbidden.py").write_text("SECRET_FLAG = 'keep'\n", encoding="utf-8")
    (ws / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")

    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "studio_delegation_test.db"

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    studio._reset_child_delegation_store(None)
    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_mcp_effect_store(None)
    studio._reset_process_supervisor(None)
    studio._reset_heavy_model_admission(
        studio.model_admission.HeavyModelAdmission(
            studio.model_admission.HeavyModelLimits(
                memory_bytes=24 * 1024**3,
                request_bytes=18 * 1024**3,
                slots=1,
            )
        )
    )
    studio.init_db()
    return ws


def _client() -> TestClient:
    return TestClient(
        studio.app,
        base_url="http://127.0.0.1:8787",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )


def _create_parent_session(
    ws: Path,
    *,
    policy: str = "standard",
    budget_spec: execution_budget.TaskBudgetSpec | None = None,
) -> tuple[dict[str, Any], int, str]:
    sess = studio.create_harness_session_db(
        title="Parent Delegation Session",
        workspace_dir=str(ws),
        use_worktree=False,
        policy=policy,
        lead_model="local-lead",
        critic_model="local-critic",
    )
    sid = sess["id"]
    sm = studio.get_task_state_machine()
    sm.ensure_root_task(
        sid,
        session_id=sid,
        title="Parent Delegation Session",
        workspace_dir=str(ws),
        policy=policy,
        budget_spec=budget_spec,
    )
    coord = studio.get_worker_lease_coordinator(ws)
    worker_id = f"wrk-{studio._PROCESS_BOOT_ID}-parent-{sid[:8]}"
    claim = coord.acquire_or_takeover(
        sid,
        worker_id,
        session_id=sid,
        workspace_dir=str(ws),
        policy=policy,
    )
    epoch = claim.epoch_record.epoch
    ctx = {
        **sess,
        "task_id": sid,
        "epoch": epoch,
        "worker_id": worker_id,
    }
    return ctx, epoch, worker_id


def test_authority_subset_refuses_policy_escalation_out_of_scope_tools_and_recursive_spawn(
    isolated_env: Path,
) -> None:
    """Task 1 + AC 1: Child cannot exceed parent policy, tool allowlist, path scope, or depth=1."""
    parent_plan, _, _ = _create_parent_session(isolated_env, policy="plan")
    with _client() as client:
        # 1. `plan` parent cannot spawn `standard` or mutating worker child.
        esc = client.post(
            f"/api/harness/sessions/{parent_plan['id']}/delegations",
            json={
                "objective": "Attempt write escalation from plan parent",
                "role": "worker",
                "policy": "standard",
                "allowed_tools": ["write_file"],
                "writable_paths": ["src/allowed.py"],
            },
        )
        assert esc.status_code == 403
        assert esc.json()["detail"]["code"] == "child_policy_exceeds_parent"

    parent_std, _, _ = _create_parent_session(isolated_env, policy="standard")
    with _client() as client:
        # 2. `standard` parent cannot spawn `autonomous` child.
        esc_auto = client.post(
            f"/api/harness/sessions/{parent_std['id']}/delegations",
            json={
                "objective": "Attempt autonomous escalation from standard parent",
                "role": "worker",
                "policy": "autonomous",
                "writable_paths": ["src/allowed.py"],
            },
        )
        assert esc_auto.status_code == 403
        assert esc_auto.json()["detail"]["code"] == "child_policy_exceeds_parent"

        # 3. `delegate_subagent` is unconditionally forbidden inside child tool subset.
        rec_del = client.post(
            f"/api/harness/sessions/{parent_std['id']}/delegations",
            json={
                "objective": "Attempt nested delegation",
                "role": "worker",
                "allowed_tools": ["delegate_subagent"],
                "writable_paths": ["src/allowed.py"],
            },
        )
        assert rec_del.status_code == 403
        assert rec_del.json()["detail"]["code"] == "child_recursive_delegation_forbidden"

        # 4. Path traversal in `writable_paths` is rejected fail-closed.
        trav = client.post(
            f"/api/harness/sessions/{parent_std['id']}/delegations",
            json={
                "objective": "Attempt path traversal",
                "role": "worker",
                "writable_paths": ["../outside.py"],
            },
        )
        assert trav.status_code == 403
        assert trav.json()["detail"]["code"] == "child_scope_escapes_workspace"

        # 5. Child scoped to `src/allowed.py` is refused writes to `src/forbidden.py` and reads outside `readable_paths`.
        scoped_run = client.post(
            f"/api/harness/sessions/{parent_std['id']}/delegations",
            json={
                "objective": "Attempt out-of-scope file write from child",
                "role": "worker",
                "policy": "standard",
                "allowed_tools": ["read_file", "write_file"],
                "readable_paths": ["src/allowed.py"],
                "writable_paths": ["src/allowed.py"],
                "auto_run": True,
                "auto_join": False,
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "src/forbidden.py", "content": "PWNED = True\n"},
                    }
                ],
            },
        )
        assert scoped_run.status_code == 200
        body = scoped_run.json()
        assert body["delegation"]["status"] == "failed_unjoined"
        assert body["delegation"]["failure_reason"] == "child_write_scope_exceeded"
        assert (isolated_env / "src" / "forbidden.py").read_text(encoding="utf-8") == "SECRET_FLAG = 'keep'\n"

    # 6. Direct recursive `spawn_child_delegation_async` from a `::sub:` task_id is also refused.
    child_caller = {
        **parent_std,
        "task_id": body["delegation"]["child_task_id"],
    }
    with pytest.raises(cd.DelegationAuthorityViolationError) as exc_info:
        asyncio.run(
            studio.spawn_child_delegation_async(
                child_caller,
                objective="Nested grandchild attempt",
                role="critic",
            )
        )
    assert exc_info.value.code == "child_recursive_delegation_forbidden"


def test_live_parent_policy_downgrade_and_grant_revocation_block_already_spawned_child(
    isolated_env: Path,
) -> None:
    """AC 1: Mid-flight parent policy downgrade or capability revocation immediately blocks active child."""
    parent_ctx, _, _ = _create_parent_session(isolated_env, policy="autonomous")
    sid = parent_ctx["id"]

    # Spawn a worker child while parent is `autonomous`.
    spawned = asyncio.run(
        studio.spawn_child_delegation_async(
            parent_ctx,
            objective="Write src/allowed.py unless parent revokes authority",
            role="worker",
            requested_policy="standard",
            requested_tools=["write_file"],
            writable_paths=["src/allowed.py"],
        )
    )

    # Case A: Parent revokes `workspace_write` capability mid-flight before child executes its tool!
    studio.get_scoped_grant_store().revoke_scope(
        session_id=sid,
        capabilities=["workspace_write"],
        reason="operator_revoked_parent_writes",
        workspace_root=isolated_env,
    )
    executed_a, _ = asyncio.run(
        studio.execute_child_delegation_async(
            None,
            spawned.delegation_id,
            planned_tool_calls=[
                {"name": "write_file", "arguments": {"path": "src/allowed.py", "content": "BLOCKED = 1\n"}}
            ],
        )
    )
    assert executed_a.status == "failed_unjoined"
    assert executed_a.failure_reason == "child_capability_revoked_on_parent"
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    asyncio.run(studio.join_child_delegation_async(spawned.delegation_id, disposition="discard"))

    # Case B: Fresh session where parent policy is downgraded to `plan` mid-flight.
    parent_b, _, _ = _create_parent_session(isolated_env, policy="standard")
    spawned_b = asyncio.run(
        studio.spawn_child_delegation_async(
            parent_b,
            objective="Write src/other.py unless parent downgrades policy",
            role="worker",
            requested_policy="standard",
            requested_tools=["write_file"],
            writable_paths=["src/other.py"],
        )
    )
    studio._set_harness_session_policy(parent_b["id"], "plan")
    executed_b, _ = asyncio.run(
        studio.execute_child_delegation_async(
            None,
            spawned_b.delegation_id,
            planned_tool_calls=[
                {"name": "write_file", "arguments": {"path": "src/other.py", "content": "BLOCKED = 2\n"}}
            ],
        )
    )
    assert executed_b.status == "failed_unjoined"
    assert executed_b.failure_reason == "child_parent_policy_downgraded"
    assert (isolated_env / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 2\n"


def test_child_budget_carving_overrun_halt_and_idempotent_parent_debit(
    isolated_env: Path,
) -> None:
    """Task 1: Child budget cannot exceed remaining parent budget, halts on overrun, and debits parent once."""
    tight_budget = execution_budget.TaskBudgetSpec(
        max_steps=6,
        max_tool_calls=6,
        max_tokens=1_000,
        max_elapsed_s=60.0,
    )
    parent_ctx, _, _ = _create_parent_session(
        isolated_env,
        policy="autonomous",
        budget_spec=tight_budget,
    )
    sid = parent_ctx["id"]

    # Requesting more tokens than parent's remaining budget fails closed.
    with pytest.raises(cd.DelegationRejectedError) as exc_over:
        asyncio.run(
            studio.spawn_child_delegation_async(
                parent_ctx,
                objective="Request excessive token budget",
                role="critic",
                requested_budget={"max_tokens": 5_000},
            )
        )
    assert exc_over.value.code == "child_budget_exceeds_parent"

    # Spawn a child capped at max_steps=1, max_tool_calls=1 and give it 2 tool calls -> halts on step 2!
    overrun_out = asyncio.run(
        studio.run_bounded_child_delegation_async(
            None,
            parent_ctx,
            objective="Run two reads under 1-step budget",
            role="critic",
            requested_budget={"max_steps": 1, "max_tool_calls": 1, "max_tokens": 300, "max_elapsed_s": 15.0},
            planned_tool_calls=[
                {"name": "read_file", "arguments": {"path": "src/allowed.py"}, "estimated_tokens": 50},
                {"name": "read_file", "arguments": {"path": "src/other.py"}, "estimated_tokens": 50},
            ],
            auto_join=False,
        )
    )
    del_id = overrun_out["delegation"]["delegation_id"]
    assert overrun_out["delegation"]["status"] == "failed_unjoined"
    assert "child_budget_exceeded" in overrun_out["delegation"]["failure_reason"]

    # Join once and verify parent budget is debited; re-joining is idempotent and does NOT double-debit.
    joined_1 = asyncio.run(studio.join_child_delegation_async(del_id))
    sm = studio.get_task_state_machine()
    bud_after_1 = sm.budgets.get_budget(sid)
    assert joined_1.join_outcome is not None
    assert joined_1.join_outcome.parent_budget_debited is True
    assert bud_after_1.steps_used == 2
    assert bud_after_1.tokens_used == 100

    joined_2 = asyncio.run(studio.join_child_delegation_async(del_id))
    bud_after_2 = sm.budgets.get_budget(sid)
    assert joined_2.delegation_id == del_id
    assert bud_after_2.steps_used == 2
    assert bud_after_2.tokens_used == 100


def test_conflicting_workspace_mutations_blocked_across_siblings_and_parent_and_rolled_back_on_reject(
    isolated_env: Path,
) -> None:
    """Task 3: Sibling & parent overlapping writes fail closed; failed/discarded child edits roll back cleanly."""
    parent_ctx, _, _ = _create_parent_session(isolated_env, policy="autonomous")

    # 1. Spawn Child A holding write reservation on `src/allowed.py`.
    child_a = asyncio.run(
        studio.spawn_child_delegation_async(
            parent_ctx,
            objective="Edit src/allowed.py",
            role="worker",
            requested_policy="standard",
            requested_tools=["read_file", "write_file"],
            writable_paths=["src/allowed.py"],
        )
    )

    # 2. Sibling Child B requesting overlapping `src/allowed.py` OR directory `src/` is rejected (409).
    with pytest.raises(cd.DelegationRejectedError) as sib_conflict:
        asyncio.run(
            studio.spawn_child_delegation_async(
                parent_ctx,
                objective="Conflicting sibling write on src/allowed.py",
                role="worker",
                requested_policy="standard",
                requested_tools=["write_file"],
                writable_paths=["src/allowed.py"],
            )
        )
    assert sib_conflict.value.code == "child_conflicting_write_scope"

    with pytest.raises(cd.DelegationRejectedError):
        asyncio.run(
            studio.spawn_child_delegation_async(
                parent_ctx,
                objective="Conflicting sibling directory write on src/",
                role="worker",
                requested_policy="standard",
                requested_tools=["write_file"],
                writable_paths=["src/"],
            )
        )

    # 3. Disjoint sibling Child C (`src/other.py`) CAN spawn concurrently!
    child_c = asyncio.run(
        studio.spawn_child_delegation_async(
            parent_ctx,
            objective="Disjoint sibling write on src/other.py",
            role="worker",
            requested_policy="standard",
            requested_tools=["write_file"],
            writable_paths=["src/other.py"],
        )
    )
    assert child_c.status == "running"

    # 4. Parent attempting to mutate `src/allowed.py` OR run arbitrary bash while Child A is unjoined fails closed!
    parent_write = asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "write_file",
            {"path": "src/allowed.py", "content": "PARENT_CLOBBER = True\n"},
            step_id=1,
            call_index=1,
        )
    )
    assert parent_write["ok"] is False
    assert parent_write["error_code"] == "child_write_lease_conflict"
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    parent_bash = asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "run_bash",
            {"command": "echo hi"},
            step_id=2,
            call_index=1,
        )
    )
    assert parent_bash["ok"] is False
    assert parent_bash["error_code"] == "child_write_lease_conflict"

    # 5. Child A writes broken code to `src/allowed.py`, then fails verification on join -> auto-rolled back!
    asyncio.run(
        studio.execute_child_delegation_async(
            None,
            child_a.delegation_id,
            planned_tool_calls=[
                {"name": "write_file", "arguments": {"path": "src/allowed.py", "content": "VALUE = 'broken'\n"}}
            ],
        )
    )
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 'broken'\n"

    joined_a = asyncio.run(
        studio.join_child_delegation_async(
            child_a.delegation_id,
            verification={
                "require_managed_edit": True,
                "test_command": [sys.executable, "-c", "import src.allowed as m; assert isinstance(m.VALUE, int)"],
            },
        )
    )
    assert joined_a.status == "joined_discarded"
    assert joined_a.join_outcome is not None
    assert joined_a.join_outcome.verified is False
    assert "src/allowed.py" in joined_a.join_outcome.rolled_back_paths
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    # 6. Child C writes valid code to `src/other.py` and joins accepted; parent can now write freely!
    asyncio.run(
        studio.execute_child_delegation_async(
            None,
            child_c.delegation_id,
            planned_tool_calls=[
                {"name": "write_file", "arguments": {"path": "src/other.py", "content": "OTHER = 42\n"}}
            ],
        )
    )
    joined_c = asyncio.run(studio.join_child_delegation_async(child_c.delegation_id))
    assert joined_c.status == "joined"
    assert (isolated_env / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 42\n"

    asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "read_file",
            {"path": "src/allowed.py"},
            step_id=3,
            call_index=1,
        )
    )
    parent_write_after = asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "write_file",
            {"path": "src/allowed.py", "content": "VALUE = 99\n"},
            step_id=4,
            call_index=1,
        )
    )
    assert parent_write_after["ok"] is True
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 99\n"


def test_durable_join_barrier_orphan_action_fencing_cancel_cascade_and_crash_recovery(
    isolated_env: Path,
) -> None:
    """Task 2 + AC 2: Parent cannot complete with unjoined child or orphan effects; cancel/crash fences children."""
    with _client() as client:
        parent_ctx, parent_epoch, _ = _create_parent_session(isolated_env, policy="autonomous")
        sid = parent_ctx["id"]
        sm = studio.get_task_state_machine()

        # 1. Spawn a child and execute a clean read, leaving it `completed_unjoined`.
        out = asyncio.run(
            studio.run_bounded_child_delegation_async(
                None,
                parent_ctx,
                objective="Audit src/allowed.py before parent completion",
                role="critic",
                planned_tool_calls=[{"name": "read_file", "arguments": {"path": "src/allowed.py"}}],
                auto_join=False,
            )
        )
        del_id = out["delegation"]["delegation_id"]
        child_tid = out["delegation"]["child_task_id"]

        # Parent completion MUST be rejected while child is unjoined!
        ver_spec = workflow_state.TaskVerificationSpec(require_managed_edit=False)
        ver_rep = sm.verify_task_completion(sid, workspace_dir=isolated_env, spec=ver_spec)
        with pytest.raises(workflow_state.WorkflowTransitionRejected, match="unjoined"):
            sm.complete_task(sid, epoch=parent_epoch, step=1, verification_report=ver_rep)

        # 2. Record an unresolved `uncertain` MCP external effect on `child_tid` -> join MUST fail closed!
        mcp_sem = studio.mcp_client.McpToolSemantics(
            alias="github",
            tool_name="create_issue",
            transport="stdio",
            read_only=False,
            destructive=False,
            idempotency_supported=False,
            idempotency_status="unverified",
            idempotency_mechanism="none",
        )
        mcp_rec = studio.get_mcp_effect_store().stage_dispatch(
            action_id="act-mcp-child-1",
            session_id=sid,
            task_id=child_tid,
            epoch=out["delegation"]["child_epoch"],
            semantics=mcp_sem,
            arguments={"title": "test"},
            bound_idempotency_key=None,
        )
        studio.get_mcp_effect_store().record_outcome(mcp_rec.action_id, status="uncertain", error="transport_timeout")

        blocked_join = client.post(f"/api/harness/sessions/{sid}/delegations/{del_id}/join", json={})
        assert blocked_join.status_code == 409
        assert blocked_join.json()["detail"]["code"] == "child_unresolved_consequential_actions"
        assert "act-mcp-child-1" in blocked_join.json()["detail"]["details"]["unresolved_action_ids"]

        # Resolve the uncertain external effect -> join succeeds AND parent can now complete!
        studio.get_mcp_effect_store().resolve_operator(
            mcp_rec.action_id,
            resolution="not_applied",
            reason="Verified issue was not created",
        )
        ok_join = client.post(f"/api/harness/sessions/{sid}/delegations/{del_id}/join", json={})
        assert ok_join.status_code == 200
        assert ok_join.json()["status"] == "joined"

        completed_parent = sm.complete_task(sid, epoch=parent_epoch, step=1, verification_report=ver_rep)
        assert completed_parent.state == "completed"

        # 3. Parent cancel cascade: terminates child subprocess, revokes child grant, fences child epoch, sweeps in-flight!
        parent2, _, _ = _create_parent_session(isolated_env, policy="autonomous")
        sid2 = parent2["id"]
        p_grant = studio.get_scoped_grant_store().issue_grant(
            session_id=sid2,
            task_id=sid2,
            capabilities=["code_execution", "workspace_write"],
            resource_scopes=["path:src/allowed.py"],
            current_policy="autonomous",
            workspace_root=isolated_env,
        )
        spawned2 = asyncio.run(
            studio.spawn_child_delegation_async(
                parent2,
                objective="Long-running child cancelled by parent",
                role="worker",
                requested_policy="autonomous",
                requested_tools=["run_bash", "write_file"],
                writable_paths=["src/allowed.py"],
                parent_grant_ids=[p_grant.grant_id],
            )
        )
        child_grant_id = spawned2.authority.delegated_grant_ids[0]

        # Record an in-flight consequential action on the child + launch a real child subprocess under ProcessSupervisor.
        ledger = studio.get_durable_action_ledger(isolated_env)
        inflight_rec = ledger.begin(
            action_id=None,
            step_id=1,
            call_index=1,
            tool_name="write_file",
            args={"path": "src/allowed.py", "content": "IN_FLIGHT = True\n"},
            policy="autonomous",
            action_class="managed_edit",
            allowed=True,
            explicit_approval=True,
            session_id=sid2,
            task_id=spawned2.child_task_id,
            epoch=spawned2.child_epoch,
            worker_id=spawned2.child_worker_id,
        )
        assert inflight_rec.record is not None

        ready_file = isolated_env / "child_proc.ready"
        ready_tmp = isolated_env / "child_proc.ready.tmp"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    f"from pathlib import Path; import os, time; "
                    f"Path({str(ready_tmp)!r}).write_text(str(os.getpid())); "
                    f"Path({str(ready_tmp)!r}).replace(Path({str(ready_file)!r})); "
                    f"time.sleep(30)"
                ),
            ],
            cwd=str(isolated_env),
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 12.0
            while not ready_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready_file.exists(), "Child test subprocess failed to signal readiness"

            sup = studio.get_process_supervisor()
            sup_id = sup.new_supervision_id()
            sup.register_process(
                proc,  # type: ignore[arg-type]
                supervision_id=sup_id,
                tool_name="run_bash",
                workspace_root=isolated_env,
                session_id=sid2,
                task_id=spawned2.child_task_id,
                epoch=spawned2.child_epoch,
                worker_id=spawned2.child_worker_id,
                boot_id=studio._PROCESS_BOOT_ID,
                action_id=inflight_rec.record.action_id,
            )

            cancel_resp = client.post(
                f"/api/harness/sessions/{sid2}/cancel",
                json={"reason": "operator_abort_parent_and_children"},
            )
            assert cancel_resp.status_code == 200

            proc.wait(timeout=10.0)
            assert proc.returncode is not None
            assert sup.get_contained_outcome(sup_id) is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

    # Verify child grant is revoked, child SQLite row is `cancelled`, child in-flight action swept to `uncertain`
    # (never orphaned!), and `ChildDelegationStore` reloaded from disk shows `cancelled_unjoined`.
    revoked_cg = studio.get_scoped_grant_store().get_grant(child_grant_id)
    assert revoked_cg is not None and revoked_cg.status == "revoked"
    c_row2 = sm.store.get_task(spawned2.child_task_id)
    assert c_row2 is not None and c_row2.state == "cancelled"
    swept_action = sm.store.get_action(inflight_rec.record.action_id)
    assert swept_action is not None and swept_action.status == "uncertain"

    reloaded_store = cd.ChildDelegationStore(studio.HARNESS_STORE_DIR)
    reloaded_del = reloaded_store.get_delegation(spawned2.delegation_id)
    assert reloaded_del is not None and reloaded_del.status == "cancelled_unjoined"

    # 4. Crash recovery (`reconcile_incomplete_tasks`) blocks parent if crashed child left in-flight consequential action.
    ws_crash = isolated_env.parent / "workspace_crash"
    ws_crash.mkdir(parents=True, exist_ok=True)
    (ws_crash / "src").mkdir(parents=True, exist_ok=True)
    (ws_crash / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    parent3, _, _ = _create_parent_session(ws_crash, policy="autonomous")
    sid3 = parent3["id"]
    spawned3 = asyncio.run(
        studio.spawn_child_delegation_async(
            parent3,
            objective="Crash simulation child",
            role="worker",
            requested_policy="standard",
            requested_tools=["write_file"],
            writable_paths=["src/other.py"],
        )
    )
    crash_ledger = studio.get_durable_action_ledger(ws_crash)
    crash_act = crash_ledger.begin(
        action_id=None,
        step_id=1,
        call_index=1,
        tool_name="write_file",
        args={"path": "src/other.py", "content": "CRASH = 1\n"},
        policy="standard",
        action_class="managed_edit",
        allowed=True,
        explicit_approval=True,
        session_id=sid3,
        task_id=spawned3.child_task_id,
        epoch=spawned3.child_epoch,
        worker_id=spawned3.child_worker_id,
    )
    assert crash_act.record is not None

    # Advance past lease expiry (`now + 3600`) to simulate process crash recovery.
    sm.recover_incomplete_transitions(now=time.time() + 3600.0)
    recovered_parent3 = sm.store.get_task(sid3)
    recovered_act3 = sm.store.get_action(crash_act.record.action_id)
    assert recovered_parent3 is not None and recovered_parent3.state == "blocked"
    assert recovered_act3 is not None and recovered_act3.status == "uncertain"


def test_workspace_quarantine_freezes_child_delegation_spawn(isolated_env: Path) -> None:
    """Cross-cutting safety invariant: active workspace quarantine freezes delegation spawn."""
    parent_ctx, _, _ = _create_parent_session(isolated_env, policy="standard")
    workspace_quarantine.freeze_workspace(
        isolated_env,
        store_root=studio.HARNESS_STORE_DIR,
        reason="test_quarantine_freeze",
    )
    with pytest.raises(cd.DelegationRejectedError) as q_err:
        asyncio.run(
            studio.spawn_child_delegation_async(
                parent_ctx,
                objective="Attempt delegation on quarantined workspace",
                role="critic",
            )
        )
    assert q_err.value.code == "child_workspace_quarantined"


def test_delegate_subagent_tool_executes_bounded_fresh_context_and_benchmark_justifies_overhead(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 4 + AC 3: Benchmark single-agent vs bounded delegation, verify parity with workflow_evaluation, and gate overhead."""
    with _client() as client:
        bench_resp = client.post(
            "/api/harness/delegations/benchmark",
            json={"runs_per_strategy": 2, "persist_summaries": True},
        )
        assert bench_resp.status_code == 200
        report = bench_resp.json()
        assert report["all_preserve_or_improve_verified_outcomes"] is True

        scenarios = report["scenarios"]
        # 1. `multi_module_verified_bugfix`: delegation improves verified outcomes (`1.0 > 0.0`).
        bugfix = scenarios["multi_module_verified_bugfix"]
        assert bugfix["single_model"]["success_rate"] == 0.0
        assert bugfix["multi_agent"]["success_rate"] == 1.0
        assert bugfix["delegation_overhead_justified"] is True
        assert bugfix["preserves_or_improves_verified_outcomes"] is True

        # 2. `faulty_subagent_preservation`: rollback on failed child verification preserves 100% verified outcomes!
        faulty = scenarios["faulty_subagent_preservation"]
        assert faulty["single_model"]["success_rate"] == 1.0
        assert faulty["multi_agent"]["success_rate"] == 1.0
        assert faulty["preserves_or_improves_verified_outcomes"] is True

        # 3. `trivial_single_file_edit`: delegation preserves verified outcomes (1.0 == 1.0) while single_model wins on cost!
        trivial = scenarios["trivial_single_file_edit"]
        assert trivial["single_model"]["success_rate"] == 1.0
        assert trivial["multi_agent"]["success_rate"] == 1.0
        assert trivial["delegation_overhead_justified"] is False
        assert trivial["preserves_or_improves_verified_outcomes"] is True

    # Cross-check parity with `workflow_evaluation.compare_workflows` on the same run vectors.
    eval_task = we.EvalTask(
        "trivial_single_file_edit",
        1,
        TaskRequirements(requires_tools=True),
        (RubricDimension("verified_tests", 1.0),),
    )
    eval_cfg = we.EvalConfig(max_output_tokens=2200, temperature=0.0)
    hw_d = "sha256:" + "a" * 64
    we_runs = (
        we.WorkflowRun("r1", "single_model", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 10.0, we.CostMeasurement(90.0, "TOK"), 1024),
        we.WorkflowRun("r2", "multi_agent", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 20.0, we.CostMeasurement(190.0, "TOK"), 1024),
    )
    cd_runs = (
        cd.DelegationEvalRun("r1", "single_model", "trivial_single_file_edit", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 10.0, 90.0, 1, 1024),
        cd.DelegationEvalRun("r2", "multi_agent", "trivial_single_file_edit", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 20.0, 190.0, 2, 1024),
    )
    we_comp = we.compare_workflows(eval_task, eval_cfg, we_runs)
    cd_comp = cd.compare_delegation_runs("trivial_single_file_edit", cd_runs)
    assert we_comp.deltas.success_rate == cd_comp["multi_agent_minus_single_model"]["success_rate"] == 0.0
    assert we_comp.deltas.cost == cd_comp["multi_agent_minus_single_model"]["cost"] == 100.0
    assert cd_comp["delegation_overhead_justified"] is False
    assert cd_comp["single_model"]["cost"]["median"] == we_comp.single_model.cost.median
    assert cd_comp["multi_agent"]["cost"]["median"] == we_comp.multi_agent.cost.median

    # Regression: sub-millisecond wall-clock jitter (lat_delta < 0) must NEVER
    # justify delegation when verified success_rate is tied (sr_delta == 0) and
    # multi_agent spends strictly more tokens (cost_delta > 0).
    jitter_runs = (
        cd.DelegationEvalRun("j1", "single_model", "trivial_single_file_edit", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 12.0, 90.0, 1, 1024),
        cd.DelegationEvalRun("j2", "multi_agent", "trivial_single_file_edit", eval_task.digest, eval_cfg.digest, hw_d, "completed", True, 11.5, 190.0, 2, 1024),
    )
    jitter_comp = cd.compare_delegation_runs("trivial_single_file_edit", jitter_runs)
    assert jitter_comp["multi_agent_minus_single_model"]["latency_ms"] == -0.5
    assert jitter_comp["multi_agent_minus_single_model"]["cost"] == 100.0
    assert jitter_comp["delegation_overhead_justified"] is False

    # 4. Feeding persisted `trivial_single_file_edit` benchmark summary into worker delegation rejects overhead!
    parent_ctx, _, _ = _create_parent_session(isolated_env, policy="autonomous")
    denied_overhead = asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "delegate_subagent",
            {
                "task": "Trivial one-line tweak",
                "role": "worker",
                "policy": "standard",
                "allowed_tools": ["write_file"],
                "writable_paths": ["src/allowed.py"],
                "benchmark_category": "trivial_single_file_edit",
            },
            step_id=1,
            call_index=1,
        )
    )
    assert denied_overhead["ok"] is False
    assert denied_overhead["error_code"] == "delegation_overhead_unjustified"

    # 5. Justified read-only `delegate_subagent` audit executes cleanly and auto-joins into parent!
    async def _fake_stream(
        _http: Any,
        model: str,
        _msgs: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "content", "text": f"Verified {model}: zero edge-case defects in src/allowed.py."}
        yield {"type": "usage", "total_tokens": 42}

    monkeypatch.setattr(studio, "stream_any_model", _fake_stream)
    audit_res = asyncio.run(
        studio._execute_harness_tool(
            None,  # type: ignore[arg-type]
            parent_ctx,
            "delegate_subagent",
            {
                "task": "Audit src/allowed.py for edge cases",
                "context_files": ["src/allowed.py"],
            },
            step_id=2,
            call_index=1,
        )
    )
    assert audit_res["ok"] is True
    assert "zero edge-case defects" in audit_res["output"]

    with _client() as client:
        listed = client.get(f"/api/harness/sessions/{parent_ctx['id']}/delegations")
        assert listed.status_code == 200
        data = listed.json()
        assert data["unjoined_count"] == 0
        assert len(data["delegations"]) == 1
        assert data["delegations"][0]["status"] == "joined"
        assert data["delegations"][0]["join_outcome"]["join_disposition"] == "audit_only"


def test_rollback_refuses_to_clobber_concurrent_user_edit_on_discarded_child_join(
    isolated_env: Path,
) -> None:
    """Task 3: Discarded child rollback refuses to overwrite concurrent user edits (`child_rollback_conflict`)."""
    parent_ctx, _, _ = _create_parent_session(isolated_env, policy="autonomous")
    out = asyncio.run(
        studio.run_bounded_child_delegation_async(
            None,
            parent_ctx,
            objective="Modify src/allowed.py before user edits same file",
            role="worker",
            requested_policy="standard",
            requested_tools=["write_file"],
            writable_paths=["src/allowed.py"],
            planned_tool_calls=[
                {"name": "write_file", "arguments": {"path": "src/allowed.py", "content": "VALUE = 'child_edit'\n"}}
            ],
            auto_join=False,
        )
    )
    del_id = out["delegation"]["delegation_id"]

    # Simulate an out-of-band user edit on the same line after the child finished.
    (isolated_env / "src" / "allowed.py").write_text("VALUE = 'user_manual_fix'\n", encoding="utf-8")

    with pytest.raises(cd.DelegationRejectedError) as conflict_err:
        asyncio.run(studio.join_child_delegation_async(del_id, disposition="discard"))
    assert conflict_err.value.code == "child_rollback_conflict"
    assert (isolated_env / "src" / "allowed.py").read_text(encoding="utf-8") == "VALUE = 'user_manual_fix'\n"


def test_session_delete_purges_delegations_and_corrupt_store_fails_closed_with_503(
    isolated_env: Path,
) -> None:
    """Durable store lifecycle: session deletion purges child records; corrupt JSON fails closed with 503."""
    with _client() as client:
        parent_ctx, _, _ = _create_parent_session(isolated_env, policy="standard")
        sid = parent_ctx["id"]
        created = client.post(
            f"/api/harness/sessions/{sid}/delegations",
            json={
                "objective": "Audit src/allowed.py before session purge",
                "role": "critic",
                "auto_run": True,
                "auto_join": True,
                "tool_calls": [{"name": "read_file", "arguments": {"path": "src/allowed.py"}}],
            },
        )
        assert created.status_code == 200
        assert len(studio.get_child_delegation_store().list_delegations(session_id=sid)) == 1

        deleted = client.delete(f"/api/harness/sessions/{sid}")
        assert deleted.status_code == 200
        assert studio.get_child_delegation_store().list_delegations(session_id=sid) == ()

        # Corrupting delegations/state.json must fail closed with HTTP 503 (`delegation_state_corrupt`).
        parent2, _, _ = _create_parent_session(isolated_env, policy="standard")
        sid2 = parent2["id"]
        state_file = studio.HARNESS_STORE_DIR / "delegations" / "state.json"
        state_file.write_text("{corrupt json", encoding="utf-8")
        studio._reset_child_delegation_store(None)
        corrupt_resp = client.get(f"/api/harness/sessions/{sid2}/delegations")
        assert corrupt_resp.status_code == 503
        assert corrupt_resp.json()["detail"]["code"] == "delegation_state_corrupt"

