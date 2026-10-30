"""Tests for M5-P1-03: Durable intent/result ledger integration (`side_effect_ledger` + `runtime_actions`).

Verifies every M5-P1-03 task and acceptance criterion:
1. Intent is durably committed to `runtime_actions` (`status='in_flight'`) BEFORE
   every consequential action executes, and intent-persistence failure blocks execution.
2. Stable `action_id` and monotonic `attempt` (`attempt_id`) identities are preserved
   across retries, while reusing `action_id` with conflicting tool/args fails closed.
3. Explicit separation of `never_started`, `in_flight`, `committed`, and `uncertain`
   (plus `denied` and deterministic `failed`).
4. Missing results (crash/interruption leaving `in_flight`) reconcile to `uncertain`
   and are NEVER treated as `failed`.
5. Action-type replay classification (`idempotent`, `reconcilable`, `non_idempotent`)
   prevents blind retry of non-idempotent or unverified uncertain writes while
   supporting safe state reconciliation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app as studio
import execution_spine as spine
import mcp_client
import runtime_persistence as rp
import side_effect_ledger as sel


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "studio_p103.db"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    store_dir = tmp_path / "harness_store"
    store_dir.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws_root)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "_HARNESS_ABORT_FLAGS", set())
    monkeypatch.setattr(studio, "_MCP_HTTP_SESSIONS", {})
    studio.init_db()
    return ws_root


def _sess(
    ws_root: Path,
    *,
    sid: str = "sess-p103",
    policy: str = "autonomous",
    task_id: str | None = None,
    epoch: int | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "policy": policy,
        "worktree_dir": str(ws_root),
        "status": "running",
        "task_id": task_id,
        "epoch": epoch,
        "worker_id": worker_id,
    }


def _read_action_direct(db_path: Path, action_id: str) -> dict[str, Any] | None:
    """Inspect `runtime_actions` on an independent SQLite connection."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (action_id,)).fetchone()
        return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# AC 1: Intent committed before every consequential action
# ---------------------------------------------------------------------------


def test_intent_committed_on_disk_before_every_consequential_executor_runs(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe `runtime_actions` mid-execution from a separate SQLite connection
    to prove `status='in_flight'` is committed on disk BEFORE side effects occur."""
    store = rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH)
    now = time.time()
    store.create_task("task-intent-1", session_id="sess-intent", state="running", now=now)
    ep = store.claim_epoch("task-intent-1", "w-1", lease_ttl_s=120.0, now=now)
    sess = _sess(
        isolated_env,
        sid="sess-intent",
        policy="autonomous",
        task_id="task-intent-1",
        epoch=ep.epoch,
        worker_id="w-1",
    )

    observed_mid_flight: list[tuple[str, dict[str, Any] | None, bool]] = []
    real_inner = studio._execute_harness_tool_inner

    async def _observing_inner(
        client: Any,
        s: dict[str, Any],
        tool_name: str,
        args: dict[str, Any],
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        scope = spine.current_admission_scope()
        assert scope is not None and scope.action_id
        mid_row = _read_action_direct(studio.DB_PATH, scope.action_id)
        side_effect_exists_before = False
        if tool_name == "write_file":
            side_effect_exists_before = (isolated_env / str(args["path"])).exists()
        observed_mid_flight.append((tool_name, mid_row, side_effect_exists_before))
        return await real_inner(client, s, tool_name, args, approved=approved)

    monkeypatch.setattr(studio, "_execute_harness_tool_inner", _observing_inner)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    # 1. `write_file` (`managed_edit` / `reconcilable`)
    w_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "write_file",
            {"path": "alpha.txt", "content": "v1\n"},
            step_id=1,
            call_index=0,
        )
    )
    assert w_res["ok"] is True
    assert w_res["ledger_status"] == "committed"

    # 2. `edit_file` (`managed_edit` / `non_idempotent`)
    e_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "edit_file",
            {"path": "alpha.txt", "old_text": "v1", "new_text": "v2"},
            step_id=1,
            call_index=1,
        )
    )
    assert e_res["ok"] is True
    assert e_res["ledger_status"] == "committed"

    # 3. `run_bash` (`consequential` / `non_idempotent`)
    b_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "run_bash",
            {"command": "echo ledger-shell-ok"},
            step_id=1,
            call_index=2,
        )
    )
    assert b_res["ok"] is True
    assert b_res["ledger_status"] == "committed"

    # 4. `oc_save_doc` (`managed_edit` / `non_idempotent` without doc_id)
    oc_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "oc_save_doc",
            {"title": "Ledger Note", "content": "Committed after intent", "folder": "decisions"},
            step_id=1,
            call_index=3,
        )
    )
    assert oc_res["ok"] is True
    assert oc_res["ledger_status"] == "committed"

    assert [t for t, _, _ in observed_mid_flight] == ["write_file", "edit_file", "run_bash", "oc_save_doc"]
    for tname, mid_row, existed_before in observed_mid_flight:
        assert mid_row is not None, f"Intent row missing mid-execution for {tname}"
        assert mid_row["status"] == "in_flight"
        assert mid_row["ok"] is None
        assert mid_row["attempt"] == 1
        assert mid_row["task_id"] == "task-intent-1"
        assert mid_row["epoch"] == ep.epoch
        if tname == "write_file":
            assert existed_before is False

    # Every completed action is now finalized as `committed` with output digest.
    for res in (w_res, e_res, b_res, oc_res):
        final_row = _read_action_direct(studio.DB_PATH, res["action_id"])
        assert final_row is not None
        assert final_row["status"] == "committed"
        assert final_row["ok"] == 1
        assert len(final_row["output_sha256"]) == 64


def test_intent_commit_failure_blocks_consequential_side_effects(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If durable intent cannot be persisted to `runtime_actions`, the executor never runs."""
    sess = _sess(isolated_env, sid="sess-fail-closed", policy="elevated")
    executed: list[str] = []

    def _broken_begin(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("simulated runtime_actions disk error")

    async def _spy_executor(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        executed.append("ran")
        return {"ok": True, "output": "should-never-happen", "diff": "", "elapsed_ms": 1}

    monkeypatch.setattr(sel.DurableActionLedger, "begin", _broken_begin)
    monkeypatch.setattr(studio, "_execute_harness_tool_inner", _spy_executor)

    with pytest.raises(sqlite3.OperationalError, match="simulated runtime_actions disk error"):
        asyncio.run(
            studio._execute_harness_tool(
                None,
                sess,
                "write_file",
                {"path": "blocked.txt", "content": "never"},
                step_id=1,
                call_index=0,
            )
        )

    assert executed == []
    assert not (isolated_env / "blocked.txt").exists()


# ---------------------------------------------------------------------------
# Task 2: Stable action and monotonic attempt identities + conflict rejection
# ---------------------------------------------------------------------------


def test_stable_action_id_and_monotonic_attempt_progression(isolated_env: Path) -> None:
    ledger = studio.get_durable_action_ledger(isolated_env)
    args = {"path": "missing_target.txt", "old_text": "a", "new_text": "b"}

    aid_1 = sel.derive_action_id("sess-id-1", "edit_file", args, step_id=2, call_index=1)
    aid_2 = sel.derive_action_id("sess-id-1", "edit_file", args, step_id=2, call_index=1)
    aid_other_slot = sel.derive_action_id("sess-id-1", "edit_file", args, step_id=2, call_index=2)
    assert aid_1 == aid_2
    assert aid_1 != aid_other_slot

    # Stage `never_started` at attempt=1.
    staged = ledger.stage_never_started(
        aid_1,
        tool_name="edit_file",
        args=args,
        policy="standard",
        action_class="managed_edit",
        session_id="sess-id-1",
    )
    assert staged.status == "never_started"
    assert staged.attempt == 1

    # Attempt 2: policy denial (`plan`) records `denied` at attempt=2.
    plan_sess = _sess(isolated_env, sid="sess-id-1", policy="plan")
    denied_res = asyncio.run(
        studio._execute_harness_tool(None, plan_sess, "edit_file", args, action_id=aid_1)
    )
    assert denied_res["ok"] is False
    assert denied_res["action_id"] == aid_1
    assert denied_res["attempt"] == 2
    assert denied_res["attempt_id"] == f"{aid_1}#attempt-2"
    assert denied_res["ledger_status"] == "denied"

    # Attempt 3: deterministic tool failure (`missing_target.txt` does not exist) -> `failed` at attempt=3.
    std_sess = _sess(isolated_env, sid="sess-id-1", policy="standard")
    fail_res = asyncio.run(
        studio._execute_harness_tool(None, std_sess, "edit_file", args, action_id=aid_1)
    )
    assert fail_res["ok"] is False
    assert fail_res["action_id"] == aid_1
    assert fail_res["attempt"] == 3
    assert fail_res["ledger_status"] == "failed"

    # Create file so deterministic retry succeeds -> `committed` at attempt=4!
    (isolated_env / "missing_target.txt").write_text("a\n", encoding="utf-8")
    ok_res = asyncio.run(
        studio._execute_harness_tool(None, std_sess, "edit_file", args, action_id=aid_1)
    )
    assert ok_res["ok"] is True
    assert ok_res["action_id"] == aid_1
    assert ok_res["attempt"] == 4
    assert ok_res["attempt_id"] == f"{aid_1}#attempt-4"
    assert ok_res["ledger_status"] == "committed"

    # Reusing the same `action_id` with mutated arguments fails closed with ActionIdentityConflictError!
    mutated_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            std_sess,
            "edit_file",
            {"path": "missing_target.txt", "old_text": "b", "new_text": "mutated"},
            action_id=aid_1,
        )
    )
    assert mutated_res["ok"] is False
    assert mutated_res["replay_blocked"] is True
    assert "different argument digest" in mutated_res["output"]
    assert (isolated_env / "missing_target.txt").read_text(encoding="utf-8") == "b\n"

    with pytest.raises(sel.ActionIdentityConflictError):
        asyncio.run(
            studio._run_authorized_tool(
                "write_file",
                {"path": "other.txt", "content": "conflict"},
                "standard",
                approved=False,
                session_id="sess-id-1",
                executor=lambda: asyncio.sleep(0, result={"ok": True, "output": "x", "diff": "", "elapsed_ms": 1}),
                denial_as_exception=True,
                action_id=aid_1,
                workspace_root=isolated_env,
            )
        )


# ---------------------------------------------------------------------------
# Task 3: Separate `never_started`, `in_flight`, `committed`, and `uncertain`
# ---------------------------------------------------------------------------


def test_four_states_never_started_in_flight_committed_and_uncertain_are_distinct(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = studio.get_durable_action_ledger(isolated_env)
    sess = _sess(isolated_env, sid="sess-4state", policy="autonomous")

    # 1. `never_started`: pre_execute_guard blocks dispatch before executor enters
    guard_blocked = {"trip": True}

    def _custom_guard() -> None:
        if guard_blocked["trip"]:
            raise RuntimeError("pre-dispatch fence tripped")

    boundary = spine.DurableActionBoundary(
        studio._AppToolPolicy(),
        studio._AppActionLedger(),
        pre_execute_guard=_custom_guard,
        task_context_resolver=studio._AppTaskContextResolver(),
        intent_ledger=studio._AppIntentLedger(workspace_root=isolated_env),
    )
    ran_exec: list[int] = []

    async def _exec_write() -> dict[str, Any]:
        ran_exec.append(1)
        (isolated_env / "four_state.txt").write_text("ok\n", encoding="utf-8")
        return {"ok": True, "output": "wrote", "diff": "", "elapsed_ms": 2}

    with pytest.raises(RuntimeError, match="pre-dispatch fence tripped"):
        asyncio.run(
            boundary.admit_and_execute(
                spine.ActionRequest(
                    tool_name="write_file",
                    args={"path": "four_state.txt", "content": "ok\n"},
                    policy="standard",
                    approved=False,
                    session_id="sess-4state",
                    action_id="act-4state-demo",
                ),
                _exec_write,
            )
        )
    assert ran_exec == []
    rec_ns = ledger.get_action("act-4state-demo")
    assert rec_ns is not None
    assert rec_ns.status == "never_started"
    assert rec_ns.ok is None
    assert rec_ns.attempt == 1

    # 2. Lift guard -> transitions `never_started` -> `in_flight` -> `committed` (attempt=2)
    guard_blocked["trip"] = False
    in_flight_seen: list[str] = []

    async def _exec_write_observing() -> dict[str, Any]:
        live = ledger.get_action("act-4state-demo")
        assert live is not None
        in_flight_seen.append(live.status)
        return await _exec_write()

    res_ok = asyncio.run(
        boundary.admit_and_execute(
            spine.ActionRequest(
                tool_name="write_file",
                args={"path": "four_state.txt", "content": "ok\n"},
                policy="standard",
                approved=False,
                session_id="sess-4state",
                action_id="act-4state-demo",
            ),
            _exec_write_observing,
        )
    )
    assert in_flight_seen == ["in_flight"]
    assert res_ok["ok"] is True
    rec_c = ledger.get_action("act-4state-demo")
    assert rec_c is not None
    assert rec_c.status == "committed"
    assert rec_c.ok is True
    assert rec_c.attempt == 2

    # 3. `uncertain`: post-dispatch shell timeout marks `status='uncertain'` (`ok=None`), NOT `failed`!
    async def _fake_timed_out_exec(*_a: Any, **_kw: Any) -> tuple[int | None, bytes, bool, bool]:
        return (None, b"partial output before timeout", False, True)

    monkeypatch.setattr(studio, "_execute_limited", _fake_timed_out_exec)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    timeout_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "run_bash",
            {"command": "python3 deploy.py", "timeout_sec": 5},
            action_id="act-timeout-bash",
        )
    )
    assert timeout_res["ok"] is False
    assert timeout_res["uncertain"] is True
    assert timeout_res["ledger_status"] == "uncertain"

    rec_u = ledger.get_action("act-timeout-bash")
    assert rec_u is not None
    assert rec_u.status == "uncertain"
    assert rec_u.ok is None
    assert rec_u.error_code == "command_timeout"


# ---------------------------------------------------------------------------
# AC 2: Missing result never treated as failed
# ---------------------------------------------------------------------------


def test_missing_result_reconciles_to_uncertain_never_failed(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = studio.get_durable_action_ledger(isolated_env)
    sess = _sess(isolated_env, sid="sess-crash", policy="elevated")

    # Case A: Simulate process crash / DB drop right when finalizing `complete(...)`
    # so the action executes once and its row remains `in_flight` (missing result).
    orig_complete = sel.DurableActionLedger.complete
    crash_switch = {"drop_complete": True}

    def _flaky_complete(self: sel.DurableActionLedger, action_id: str, result: Any, elapsed_ms: int, **kw: Any) -> Any:
        if crash_switch["drop_complete"]:
            raise sqlite3.OperationalError("simulated power loss before result commit")
        return orig_complete(self, action_id, result, elapsed_ms, **kw)

    monkeypatch.setattr(sel.DurableActionLedger, "complete", _flaky_complete)
    first = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "oc_save_doc",
            {"title": "Crash Doc", "content": "written before crash", "folder": "notes"},
            action_id="act-missing-res-1",
        )
    )
    assert first["ok"] is True
    assert "audit_warning" in first

    orphaned = ledger.get_action("act-missing-res-1")
    assert orphaned is not None
    assert orphaned.status == "in_flight"
    assert orphaned.ok is None

    # Restore normal ledger completion and simulate server restart (`studio.init_db()`).
    crash_switch["drop_complete"] = False
    studio.init_db()

    swept = ledger.get_action("act-missing-res-1")
    assert swept is not None
    assert swept.status == "uncertain"
    assert swept.status != "failed"
    assert swept.ok is None
    assert swept.error_code == "missing_result_on_startup"

    # Case B: Even without a startup sweep, encountering an orphaned `in_flight` row
    # during `begin()` transitions it to `uncertain` (never `failed`) and blocks non-idempotent replay!
    begin_unswept = ledger.begin(
        action_id="act-missing-res-2",
        tool_name="run_bash",
        args={"command": "echo once"},
        policy="elevated",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id="sess-crash",
    )
    assert begin_unswept.proceed is True
    assert begin_unswept.status == "in_flight"

    replay_unswept = ledger.begin(
        action_id="act-missing-res-2",
        tool_name="run_bash",
        args={"command": "echo once"},
        policy="elevated",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id="sess-crash",
    )
    assert replay_unswept.proceed is False
    assert replay_unswept.replay_blocked is True
    assert replay_unswept.status == "uncertain"
    rec2 = ledger.get_action("act-missing-res-2")
    assert rec2 is not None
    assert rec2.status == "uncertain"
    assert rec2.status != "failed"
    assert rec2.ok is None
    assert rec2.error_code == "missing_result"


# ---------------------------------------------------------------------------
# Task 4 & AC 3: Replay classification + non-idempotent uncertain writes never retried blindly
# ---------------------------------------------------------------------------


def test_action_type_replay_classification_matrix() -> None:
    # Idempotent managed reads
    for read_tool in ("read_file", "list_files", "grep_search", "oc_search", "oc_read", "codegraph_impact"):
        assert sel.classify_replay_policy(read_tool, {}) == "idempotent"
    assert sel.classify_replay_policy("ctx_run_block", {"block": "json-peek"}) == "idempotent"
    assert sel.classify_replay_policy("ctx_run_block", {"block": "csv-peek"}) == "idempotent"
    assert sel.classify_replay_policy("ctx_run_block", {"block": "shortcut-debt"}) == "idempotent"

    # Reconcilable deterministic target overwrites
    assert sel.classify_replay_policy("write_file", {"path": "a.py", "content": "x"}) == "reconcilable"
    assert sel.classify_replay_policy("oc_save_doc", {"doc_id": "adr-01", "title": "T", "content": "C"}) == "reconcilable"
    assert sel.classify_replay_policy("mcp_bridge__oc_save_doc", {"doc_id": "adr-01"}) == "reconcilable"

    # Non-idempotent mutations & external side effects
    assert sel.classify_replay_policy("oc_save_doc", {"title": "No ID", "content": "C"}) == "non_idempotent"
    assert sel.classify_replay_policy("edit_file", {"path": "a.py", "old_text": "x", "new_text": "y"}) == "non_idempotent"
    assert sel.classify_replay_policy("run_bash", {"command": "git commit"}) == "non_idempotent"
    assert sel.classify_replay_policy("ctx_execute_python", {"code": "print(1)"}) == "non_idempotent"
    assert sel.classify_replay_policy("ctx_run_block", {"block": "test-summary"}) == "non_idempotent"
    assert sel.classify_replay_policy("skill_execute_script", {"script": "s.py"}) == "non_idempotent"
    assert sel.classify_replay_policy("delegate_subagent", {"task": "audit"}) == "non_idempotent"
    assert sel.classify_replay_policy("mcp__github__create_issue", {"title": "bug"}) == "non_idempotent"
    assert sel.classify_replay_policy("mcp_bridge__start_council", {"request_text": "hi"}) == "non_idempotent"
    assert sel.classify_replay_policy("unregistered_custom_tool", {}) == "non_idempotent"


def test_non_idempotent_uncertain_and_committed_writes_are_never_retried_blindly(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify AC 3 across both shell/file tools and outbound MCP `tools/call`:
    non-idempotent uncertain writes are NEVER retried blindly, whereas idempotent
    reads safely retry."""
    (isolated_env / "readme.txt").write_text("hello durable world\n", encoding="utf-8")
    sess = _sess(isolated_env, sid="sess-replay", policy="elevated")
    ledger = studio.get_durable_action_ledger(isolated_env)

    # 1. Outbound MCP `tools/call` hits HTTP 502 on attempt 1 -> marked `uncertain` (single POST!),
    #    and a second attempt with the same `action_id` is BLOCKED without touching the network!
    studio.upsert_mcp_server(
        {
            "alias": "linear-prod",
            "name": "Linear Prod",
            "transport": "http",
            "url": "https://mcp.linear.example/mcp",
            "enabled": True,
            "approval_policy": "auto",
        }
    )
    rp.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: conn.execute(
            "UPDATE mcp_servers SET tools_json=?, enabled_tools_json=?, updated_at=? WHERE alias='linear-prod'",
            (json.dumps([{"name": "create_issue"}]), json.dumps(["create_issue"]), time.time()),
        ),
    )

    async def _allow_url(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    remote_posts: list[dict[str, Any]] = []

    def _gateway_502_handler(req: httpx.Request) -> httpx.Response:
        remote_posts.append(
            {
                "headers": dict(req.headers),
                "body": json.loads(req.content.decode("utf-8")),
            }
        )
        return httpx.Response(502, text="Bad Gateway after upstream write")

    async def _call_mcp_once() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_gateway_502_handler), follow_redirects=False) as client:
            await studio.execute_mcp_tool_call(
                client,
                "linear-prod",
                "create_issue",
                {"title": "Prod outage"},
                policy="elevated",
                approved=True,
                session_id="sess-replay",
                grant_source="operator_one_call",
                action_id="act-mcp-issue-1",
            )

    with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
        asyncio.run(_call_mcp_once())

    # Remote server was hit ONCE (adaptive_call did NOT fall back and re-POST `tools/call`!),
    # and received `Idempotency-Key: act-mcp-issue-1`.
    assert len(remote_posts) == 1
    assert remote_posts[0]["headers"].get("idempotency-key") == "act-mcp-issue-1"

    mcp_rec = ledger.get_action("act-mcp-issue-1")
    assert mcp_rec is not None
    assert mcp_rec.status == "uncertain"
    assert mcp_rec.ok is None
    assert mcp_rec.attempt == 1

    # Retrying the uncertain non-idempotent MCP write is strictly blocked (`UncertainActionReplayError`)
    # BEFORE any second network request is sent!
    with pytest.raises(sel.UncertainActionReplayError, match="cannot be retried blindly"):
        asyncio.run(_call_mcp_once())
    assert len(remote_posts) == 1
    assert ledger.get_action("act-mcp-issue-1").attempt == 1  # type: ignore[union-attr]

    # 2. Committed consequential write (`edit_file`) is also blocked from duplicate execution.
    (isolated_env / "counter.txt").write_text("count=0\n", encoding="utf-8")
    edit_1 = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "edit_file",
            {"path": "counter.txt", "old_text": "count=0", "new_text": "count=1"},
            action_id="act-edit-once",
        )
    )
    assert edit_1["ok"] is True
    assert edit_1["ledger_status"] == "committed"

    edit_dup = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "edit_file",
            {"path": "counter.txt", "old_text": "count=0", "new_text": "count=1"},
            action_id="act-edit-once",
        )
    )
    assert edit_dup["ok"] is False
    assert edit_dup["replay_blocked"] is True
    assert edit_dup["already_committed"] is True

    # 3. By contrast, an `idempotent` read (`read_file`) in `uncertain` state safely retries!
    ledger.begin(
        action_id="act-read-retry",
        tool_name="read_file",
        args={"path": "readme.txt"},
        policy="standard",
        action_class="read_only",
        allowed=True,
        explicit_approval=False,
        session_id="sess-replay",
    )
    ledger.reconcile_missing_results(session_id="sess-replay")
    assert ledger.get_action("act-read-retry").status == "uncertain"  # type: ignore[union-attr]

    read_retry = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "read_file",
            {"path": "readme.txt"},
            action_id="act-read-retry",
        )
    )
    assert read_retry["ok"] is True
    assert read_retry["attempt"] == 2
    assert read_retry["ledger_status"] == "committed"


def test_reconcilable_uncertain_actions_reconcile_without_blind_overwrite(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify `reconcilable` replay policy for `write_file` and `oc_save_doc(doc_id=...)`."""
    sess = _sess(isolated_env, sid="sess-reconcile", policy="elevated")
    ledger = studio.get_durable_action_ledger(isolated_env)

    # --- Case 1: `write_file` where the file WAS written before crash ---
    target_file = isolated_env / "config.json"
    target_content = '{"version": 2}\n'
    ledger.begin(
        action_id="act-write-reconciled",
        tool_name="write_file",
        args={"path": "config.json", "content": target_content},
        policy="standard",
        action_class="managed_edit",
        allowed=True,
        explicit_approval=False,
        session_id="sess-reconcile",
    )
    target_file.write_text(target_content, encoding="utf-8")
    ledger.reconcile_missing_results(session_id="sess-reconcile")
    assert ledger.get_action("act-write-reconciled").status == "uncertain"  # type: ignore[union-attr]

    inner_calls: list[str] = []
    real_inner = studio._execute_harness_tool_inner

    async def _counting_inner(c: Any, s: dict[str, Any], t: str, a: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        inner_calls.append(t)
        return await real_inner(c, s, t, a, approved=approved)

    monkeypatch.setattr(studio, "_execute_harness_tool_inner", _counting_inner)

    reconciled_write = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "write_file",
            {"path": "config.json", "content": target_content},
            action_id="act-write-reconciled",
        )
    )
    assert reconciled_write["ok"] is True
    assert reconciled_write["reconciled"] is True
    assert reconciled_write["ledger_status"] == "committed"
    assert inner_calls == []  # Reconciled from disk state without re-running executor!

    # --- Case 2: `write_file` where file has DIFFERENT user edits -> refuses blind overwrite! ---
    other_file = isolated_env / "user_edited.txt"
    other_file.write_text("operator manual edit\n", encoding="utf-8")
    ledger.begin(
        action_id="act-write-diverged",
        tool_name="write_file",
        args={"path": "user_edited.txt", "content": "stale agent content\n"},
        policy="standard",
        action_class="managed_edit",
        allowed=True,
        explicit_approval=False,
        session_id="sess-reconcile",
    )
    ledger.reconcile_missing_results(session_id="sess-reconcile")

    blocked_write = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "write_file",
            {"path": "user_edited.txt", "content": "stale agent content\n"},
            action_id="act-write-diverged",
        )
    )
    assert blocked_write["ok"] is False
    assert blocked_write["replay_blocked"] is True
    assert blocked_write["uncertain"] is True
    assert other_file.read_text(encoding="utf-8") == "operator manual edit\n"

    # Explicit operator resolution (`verified_not_applied`) unlocks clean retry at attempt=2.
    ledger.resolve_uncertain_action(
        "act-write-diverged",
        resolution="verified_not_applied",
        reason="operator_approved_retry",
    )
    unlocked_write = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "write_file",
            {"path": "user_edited.txt", "content": "stale agent content\n"},
            action_id="act-write-diverged",
        )
    )
    assert unlocked_write["ok"] is True
    assert unlocked_write["attempt"] == 2
    assert unlocked_write["ledger_status"] == "committed"

    # --- Case 3: `oc_save_doc` with explicit `doc_id` absent -> `verified_not_applied` -> safe retry ---
    doc_args = {
        "doc_id": "adr-p103",
        "title": "Durable Intent Ledger",
        "content": "Intent before effect.",
        "folder": "decisions",
    }
    ledger.begin(
        action_id="act-oc-doc-1",
        tool_name="oc_save_doc",
        args=doc_args,
        policy="standard",
        action_class="managed_edit",
        allowed=True,
        explicit_approval=False,
        session_id="sess-reconcile",
    )
    ledger.reconcile_missing_results(session_id="sess-reconcile")

    oc_applied = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "oc_save_doc",
            doc_args,
            action_id="act-oc-doc-1",
        )
    )
    assert oc_applied["ok"] is True
    assert oc_applied["attempt"] == 2
    assert oc_applied["ledger_status"] == "committed"


def test_mcp_playground_http_route_returns_409_when_uncertain_action_replayed(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio.upsert_mcp_server(
        {
            "alias": "gh-mcp",
            "name": "GitHub MCP",
            "transport": "http",
            "url": "https://mcp.github.example/mcp",
            "enabled": True,
            "approval_policy": "always_ask",
        }
    )
    rp.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: conn.execute(
            "UPDATE mcp_servers SET tools_json=?, enabled_tools_json=?, updated_at=? WHERE alias='gh-mcp'",
            (json.dumps([{"name": "merge_pr"}]), json.dumps(["merge_pr"]), time.time()),
        ),
    )

    calls = {"count": 0}

    async def _ambiguous_rpc(_client: Any, _srv: Any, method: str, params: Any, timeout_sec: float = 45.0) -> dict[str, Any]:
        assert method == "tools/call"
        calls["count"] += 1
        raise mcp_client.McpAmbiguousOutcomeError("HTTP 504 Gateway Timeout during merge_pr")

    test_token = "t" * 40
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", test_token)
    monkeypatch.setattr(studio, "_mcp_http_rpc", _ambiguous_rpc)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        auth_headers = {
            "Authorization": f"Bearer {test_token}",
            "Idempotency-Key": "idem-merge-42",
        }
        # First call fails ambiguously (HTTP 400 with uncertain ledger state recorded).
        r1 = client.post(
            "/api/mcp/servers/gh-mcp/call",
            headers=auth_headers,
            json={"tool": "merge_pr", "arguments": {"pr": 42}},
        )
        assert r1.status_code == 400
        assert calls["count"] == 1
        rec = studio.get_durable_action_ledger(isolated_env).get_action("idem-merge-42")
        assert rec is not None
        assert rec.status == "uncertain"

        # Second call with same Idempotency-Key is rejected with 409 Conflict without calling remote!
        r2 = client.post(
            "/api/mcp/servers/gh-mcp/call",
            headers=auth_headers,
            json={"tool": "merge_pr", "arguments": {"pr": 42}},
        )
        assert r2.status_code == 409
        assert "cannot be retried blindly" in r2.json()["detail"]
        assert calls["count"] == 1


def test_standalone_studio_artifact_inlines_side_effect_ledger_outside_repo(tmp_path: Path) -> None:
    """Load built `studio.py` from an isolated directory outside the repository root
    so sibling `side_effect_ledger.py` cannot be imported from disk, and verify
    the durable intent/result ledger works end-to-end."""
    isolated_dir = tmp_path / "standalone_bundle"
    isolated_dir.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    standalone_path = isolated_dir / "studio_standalone.py"
    shutil.copy2(repo_root / "studio.py", standalone_path)

    spec = importlib.util.spec_from_file_location("studio_standalone_p103", standalone_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved_sys_path = list(sys.path)
    saved_sel = sys.modules.pop("side_effect_ledger", None)
    sys.modules[spec.name] = mod
    try:
        sys.path = [
            p for p in saved_sys_path
            if Path(p or ".").resolve() != repo_root.resolve()
        ]
        sys.path.insert(0, str(isolated_dir))
        spec.loader.exec_module(mod)
    finally:
        sys.path = saved_sys_path
        sys.modules.pop(spec.name, None)
        if saved_sel is not None:
            sys.modules["side_effect_ledger"] = saved_sel

    ws = tmp_path / "standalone_ws"
    ws.mkdir()
    mod.DB_PATH = tmp_path / "standalone.db"
    mod.WORKSPACE_ROOT = ws
    mod.HARNESS_STORE_DIR = tmp_path / "standalone_store"
    mod.HARNESS_STORE_DIR.mkdir()
    mod.init_db()

    res = asyncio.run(
        mod._execute_harness_tool(
            None,
            {"id": "standalone-s1", "policy": "standard", "worktree_dir": str(ws), "status": "running"},
            "write_file",
            {"path": "hello.txt", "content": "standalone ledger ok\n"},
            action_id="act-standalone-1",
        )
    )
    assert res["ok"] is True
    assert res["ledger_status"] == "committed"
    rec = mod.get_durable_action_ledger(ws).get_action("act-standalone-1")
    assert rec is not None
    assert rec.status == "committed"
    assert rec.ok is True
