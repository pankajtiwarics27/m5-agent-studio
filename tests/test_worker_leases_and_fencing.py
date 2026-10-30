"""M5-P1-04 verification suite: Worker leases, fencing and ownership recovery.

Covers all M5-P1-04 tasks and acceptance criteria:
- Transactional single-owner worker lease claims and strictly monotonic epochs;
- Epoch, worker ownership, lease-TTL, and unresolved-effect validation at BOTH
  the admission boundary and atomic ``BEGIN IMMEDIATE`` intent commit (eliminating
  TOCTOU windows);
- Pre-takeover reconciliation of orphaned ``in_flight`` effects before replacement
  execution enters ``running``;
- Stale owner consequential admission denial across all surfaces (AC 1);
- Lease expiry never releasing unresolved effects and holding replacement
  execution ``blocked`` until reconciled (AC 2);
- Crash/restart and host sleep/wake recovery never duplicating actions (AC 3);
- Live FastAPI HTTP 409 single-owner & unresolved-effect hold enforcement plus
  standalone ``studio.py`` bundle parity.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

import app as studio
import execution_spine as spine
import runtime_persistence as rp
import side_effect_ledger as sel

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "studio_p104.db"
    store_dir = tmp_path / "harness_store"
    store_dir.mkdir(parents=True, exist_ok=True)
    ws_root = tmp_path / "workspace"
    ws_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws_root)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "x" * 40)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(ws_root), create_if_missing=True)
    return ws_root


def _sess(
    ws_root: Path,
    *,
    sid: str = "sess-p104",
    policy: str = "autonomous",
    task_id: str | None = None,
    epoch: int | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "policy": policy,
        "worktree_dir": str(ws_root),
        "workspace_dir": str(ws_root),
        "status": "running",
        "task_id": task_id,
        "epoch": epoch,
        "worker_id": worker_id,
    }


# ---------------------------------------------------------------------------
# Task 1: Transactional single-owner claim & monotonic epoch
# ---------------------------------------------------------------------------


def test_transactional_single_owner_lease_rejects_concurrent_steal_and_advances_monotonic_epochs(
    isolated_env: Path,
) -> None:
    coord = studio.get_worker_lease_coordinator(isolated_env)
    store = coord.store
    t0 = 10_000.0

    claim1 = coord.acquire_or_takeover(
        "task-mono-1",
        "worker-a",
        session_id="sess-mono",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=30.0,
        now=t0,
    )
    assert claim1.epoch_record.epoch == 1
    assert claim1.epoch_record.status == "active"
    assert claim1.task.state == "running"
    assert claim1.superseded_epochs == ()

    # Another worker CANNOT steal an active unexpired lease.
    with pytest.raises(rp.LeaseOwnershipConflictError, match="actively owned by worker 'worker-a'"):
        coord.acquire_or_takeover(
            "task-mono-1",
            "worker-b",
            lease_ttl_s=30.0,
            now=t0 + 5.0,
        )
    assert store.get_task("task-mono-1").current_epoch == 1  # type: ignore[union-attr]
    assert store.get_epoch("task-mono-1", 1).status == "active"  # type: ignore[union-attr]

    # Clean release -> next acquisition gets epoch 2.
    assert coord.release("task-mono-1", 1, "worker-a", next_state="paused", now=t0 + 10.0) is True
    assert store.get_epoch("task-mono-1", 1).status == "released"  # type: ignore[union-attr]

    claim2 = coord.acquire_or_takeover(
        "task-mono-1",
        "worker-b",
        lease_ttl_s=30.0,
        now=t0 + 11.0,
    )
    assert claim2.epoch_record.epoch == 2

    # Lease expires without release -> next acquisition transitions epoch 2 to `expired` and mints epoch 3.
    claim3 = coord.acquire_or_takeover(
        "task-mono-1",
        "worker-c",
        lease_ttl_s=30.0,
        now=t0 + 50.0,
    )
    assert claim3.epoch_record.epoch == 3
    assert len(claim3.superseded_epochs) == 1
    assert claim3.superseded_epochs[0].epoch == 2
    assert claim3.superseded_epochs[0].status == "expired"

    # Explicit operator `force_takeover=True` fences active epoch 3 and mints epoch 4.
    claim4 = coord.acquire_or_takeover(
        "task-mono-1",
        "worker-d",
        lease_ttl_s=30.0,
        force_takeover=True,
        fence_reason="operator_force_takeover",
        now=t0 + 55.0,
    )
    assert claim4.epoch_record.epoch == 4
    assert claim4.superseded_epochs[0].epoch == 3
    assert claim4.superseded_epochs[0].status == "fenced"
    assert [e.epoch for e in store.list_epochs("task-mono-1")] == [1, 2, 3, 4]


def test_parallel_thread_race_allows_exactly_one_single_owner_claim(
    isolated_env: Path,
) -> None:
    store = rp.RuntimePersistenceStore(studio.STORAGE, studio.DB_PATH)
    store.ensure_task("task-race", session_id="sess-race", workspace_dir=str(isolated_env), now=20_000.0)

    def _race_worker(idx: int) -> str:
        c = studio.get_worker_lease_coordinator(isolated_env)
        try:
            res = c.acquire_or_takeover(
                "task-race",
                f"racer-{idx}",
                lease_ttl_s=60.0,
                now=20_001.0,
            )
            return f"won:{res.epoch_record.worker_id}:ep{res.epoch_record.epoch}"
        except rp.LeaseOwnershipConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_race_worker, range(8)))

    winners = [o for o in outcomes if o.startswith("won:")]
    conflicts = [o for o in outcomes if o == "conflict"]
    assert len(winners) == 1
    assert len(conflicts) == 7
    assert store.get_task("task-race").current_epoch == 1  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# AC 1 + Task 2: Stale owner cannot admit consequential action
# ---------------------------------------------------------------------------


def test_stale_owner_cannot_admit_consequential_actions_or_clobber_replacement_state(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1: Once superseded by a new epoch, a stale worker cannot admit ANY
    consequential action across write_file, edit_file, run_bash, oc_save_doc,
    outbound MCP tools/call, or inbound MCP stdio bridge, nor clobber task state."""
    target_existing = isolated_env / "existing.txt"
    target_existing.write_text("original\n", encoding="utf-8")

    # Configure a mock HTTP MCP server to verify outbound MCP is also fenced.
    rp.run_write_transaction(
        studio.STORAGE,
        studio.DB_PATH,
        lambda conn: conn.execute(
            """
            INSERT INTO mcp_servers
            (alias, name, transport, url, tools_json, enabled, updated_at)
            VALUES (?, ?, 'http', ?, ?, 1, ?)
            """,
            (
                "mcp-fence",
                "FenceMCP",
                "https://mcp.example.com/rpc",
                json.dumps([{"name": "deploy", "description": "deploy"}]),
                time.time(),
            ),
        ),
    )
    outbound_mcp_calls: list[str] = []

    async def _fail_if_called_rpc(*_a: Any, **_kw: Any) -> Any:
        outbound_mcp_calls.append("called")
        return {"content": [{"type": "text", "text": "unexpected"}]}

    monkeypatch.setattr(studio, "_mcp_http_rpc", _fail_if_called_rpc)

    coord = studio.get_worker_lease_coordinator(isolated_env)
    now = time.time()
    ep1_claim = coord.acquire_or_takeover(
        "task-ac1",
        "worker-stale",
        session_id="sess-ac1",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=5.0,
        now=now - 20.0,
    )
    assert ep1_claim.epoch_record.epoch == 1

    # Replacement worker takes over after worker-stale's lease expired -> epoch 2!
    ep2_claim = coord.acquire_or_takeover(
        "task-ac1",
        "worker-fresh",
        session_id="sess-ac1",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=120.0,
        now=now,
    )
    assert ep2_claim.epoch_record.epoch == 2

    stale_sess = _sess(
        isolated_env,
        sid="sess-ac1",
        policy="autonomous",
        task_id="task-ac1",
        epoch=1,
        worker_id="worker-stale",
    )

    for tool_name, tool_args in [
        ("write_file", {"path": "stale_write.txt", "content": "forbidden"}),
        ("edit_file", {"path": "existing.txt", "old_str": "original", "new_str": "hacked"}),
        ("run_bash", {"command": f"touch {isolated_env / 'stale_bash.marker'}"}),
        ("oc_save_doc", {"doc_id": "doc-stale", "title": "Stale", "content": "forbidden"}),
        ("mcp__mcp-fence__deploy", {"env": "prod"}),
    ]:
        res = asyncio.run(
            studio._execute_harness_tool(
                None,
                stale_sess,
                tool_name,
                tool_args,
                approved=True,
                grant_source="operator_approval",
                approval_id="apr-valid-op",
            )
        )
        assert res["ok"] is False, f"{tool_name} should be denied for stale epoch"
        assert "Stale or mismatched task epoch 1" in res["output"]

    assert not (isolated_env / "stale_write.txt").exists()
    assert target_existing.read_text(encoding="utf-8") == "original\n"
    assert not (isolated_env / "stale_bash.marker").exists()
    assert outbound_mcp_calls == []

    # Missing worker_id and forged worker_id on active epoch 2 also fail closed!
    no_wid_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(isolated_env, sid="sess-ac1", task_id="task-ac1", epoch=2, worker_id=None),
            "write_file",
            {"path": "no_wid.txt", "content": "denied"},
            grant_source="policy",
        )
    )
    assert no_wid_res["ok"] is False
    assert "worker_id" in no_wid_res["output"]
    assert not (isolated_env / "no_wid.txt").exists()

    # Stale worker heartbeat, state transition, and late `finally:` release all fail safely!
    with pytest.raises(rp.StaleEpochError):
        coord.heartbeat("task-ac1", 1, "worker-stale", now=now + 1.0)
    with pytest.raises(rp.StaleEpochError):
        coord.store.record_transition("task-ac1", to_state="completed", epoch=1, now=now + 1.0)
    assert coord.release("task-ac1", 1, "worker-stale", next_state="completed", now=now + 1.0) is False
    assert coord.store.get_task("task-ac1").state == "running"  # type: ignore[union-attr]

    # Meanwhile the legitimate current owner (`worker-fresh`, epoch 2) executes cleanly!
    fresh_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(isolated_env, sid="sess-ac1", task_id="task-ac1", epoch=2, worker_id="worker-fresh"),
            "write_file",
            {"path": "fresh_ok.txt", "content": "epoch-2-ok\n"},
            grant_source="policy",
        )
    )
    assert fresh_res["ok"] is True
    assert (isolated_env / "fresh_ok.txt").read_text(encoding="utf-8") == "epoch-2-ok\n"


def test_atomic_begin_immediate_fencing_blocks_toctou_race_after_snapshot_read(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove that even if a stale worker passes the initial `resolve_task_context`
    read and a takeover commits `epoch = 2` in the microsecond BEFORE `begin()`
    enters `BEGIN IMMEDIATE`, `DurableActionLedger.begin` atomically rejects the
    stale worker and prevents tool execution."""
    coord = studio.get_worker_lease_coordinator(isolated_env)
    now = time.time()
    coord.acquire_or_takeover(
        "task-toctou",
        "worker-1",
        session_id="sess-toctou",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=60.0,
        now=now,
    )

    real_resolve = studio._AppTaskContextResolver.resolve_task_context
    interleaved = False

    def _interleaving_resolve(
        self: studio._AppTaskContextResolver, request: spine.ActionRequest
    ) -> spine.TaskAdmissionSnapshot | None:
        nonlocal interleaved
        snap = real_resolve(self, request)
        if not interleaved and request.task_id == "task-toctou" and request.worker_id == "worker-1":
            interleaved = True
            # Simulate operator takeover committing epoch 2 AFTER snapshot read!
            coord.acquire_or_takeover(
                "task-toctou",
                "worker-2",
                session_id="sess-toctou",
                workspace_dir=str(isolated_env),
                policy="autonomous",
                force_takeover=True,
                now=now + 0.1,
            )
        return snap

    monkeypatch.setattr(studio._AppTaskContextResolver, "resolve_task_context", _interleaving_resolve)

    stale_race_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(isolated_env, sid="sess-toctou", task_id="task-toctou", epoch=1, worker_id="worker-1"),
            "write_file",
            {"path": "toctou_blocked.txt", "content": "must-not-exist"},
            grant_source="policy",
            action_id="act-toctou-race",
        )
    )
    assert interleaved is True
    assert stale_race_res["ok"] is False
    assert stale_race_res.get("replay_blocked") is True
    assert "Stale or mismatched task epoch 1" in stale_race_res["output"]
    assert not (isolated_env / "toctou_blocked.txt").exists()

    denied_rec = coord.ledger.get_action("act-toctou-race")
    assert denied_rec is not None
    assert denied_rec.status == "denied"
    assert denied_rec.error_code == "stale_epoch_fenced"


# ---------------------------------------------------------------------------
# AC 2 + Task 3: Lease expiry never releases unresolved effects; pre-takeover reconciliation
# ---------------------------------------------------------------------------


def test_lease_expiry_never_releases_unresolved_effects_and_holds_replacement_execution(
    isolated_env: Path,
) -> None:
    """AC 2: Expiring a worker lease transitions the epoch to `expired` WITHOUT
    clearing/releasing unresolved `in_flight` or `uncertain` effects, and replacement
    execution is held in `blocked` (`UnresolvedEffectsHoldError`) until reconciled."""
    coord = studio.get_worker_lease_coordinator(isolated_env)
    t0 = time.time() - 100.0

    claim1 = coord.acquire_or_takeover(
        "task-ac2",
        "worker-1",
        session_id="sess-ac2",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=10.0,
        now=t0,
    )
    assert claim1.epoch_record.epoch == 1

    # Worker 1 commits durable `in_flight` intent for a non-idempotent bash command, then stalls.
    begin_out = coord.ledger.begin(
        action_id="act-unresolved-bash",
        tool_name="run_bash",
        args={"command": "echo external-mutation"},
        policy="autonomous",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id="sess-ac2",
        task_id="task-ac2",
        epoch=1,
        worker_id="worker-1",
        now=t0 + 1.0,
    )
    assert begin_out.proceed is True
    assert begin_out.status == "in_flight"

    # Lease expires at `t0 + 15.0`: epoch becomes `expired`, but `act-unresolved-bash` is NOT released!
    expired = coord.store.expire_stale_leases(task_id="task-ac2", now=t0 + 15.0)
    assert len(expired) == 1 and expired[0].status == "expired"
    still_in_flight = coord.ledger.get_action("act-unresolved-bash")
    assert still_in_flight is not None
    assert still_in_flight.status == "in_flight"

    # Replacement worker attempts takeover -> sweeps `in_flight -> uncertain`, holds task `blocked`, and raises!
    with pytest.raises(sel.UnresolvedEffectsHoldError) as hold_exc:
        coord.acquire_or_takeover(
            "task-ac2",
            "worker-2",
            session_id="sess-ac2",
            workspace_dir=str(isolated_env),
            policy="autonomous",
            lease_ttl_s=60.0,
            now=t0 + 16.0,
        )
    assert "act-unresolved-bash" in hold_exc.value.unresolved_action_ids
    held_task = coord.store.get_task("task-ac2")
    assert held_task is not None and held_task.state == "blocked"
    uncertain_rec = coord.ledger.get_action("act-unresolved-bash")
    assert uncertain_rec is not None
    assert uncertain_rec.status == "uncertain"
    assert uncertain_rec.error_code == "lease_takeover_in_flight"

    # Defense-in-depth: even if someone forces `state='running'` with an active epoch while
    # `act-unresolved-bash` remains `uncertain`, consequential actions stay blocked at admission!
    ep_forced = coord.store.claim_epoch("task-ac2", "worker-forced", lease_ttl_s=60.0, now=time.time())
    coord.store.record_transition("task-ac2", to_state="running", epoch=ep_forced.epoch, now=time.time())
    blocked_conseq = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(
                isolated_env,
                sid="sess-ac2",
                task_id="task-ac2",
                epoch=ep_forced.epoch,
                worker_id="worker-forced",
            ),
            "write_file",
            {"path": "while_unresolved.txt", "content": "blocked"},
            grant_source="policy",
        )
    )
    assert blocked_conseq["ok"] is False
    assert "unresolved side effects" in blocked_conseq["output"]
    assert not (isolated_env / "while_unresolved.txt").exists()

    # Managed read-only inspection IS still allowed so an operator/verifier can inspect workspace state!
    (isolated_env / "inspect_me.txt").write_text("safe-read\n", encoding="utf-8")
    read_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(
                isolated_env,
                sid="sess-ac2",
                task_id="task-ac2",
                epoch=ep_forced.epoch,
                worker_id="worker-forced",
            ),
            "read_file",
            {"path": "inspect_me.txt"},
            grant_source="policy",
        )
    )
    assert read_res["ok"] is True
    assert "safe-read" in read_res["output"]

    # Release forced epoch, explicitly reconcile `act-unresolved-bash`, and verify clean takeover!
    coord.release("task-ac2", ep_forced.epoch, "worker-forced", next_state="blocked")
    coord.ledger.resolve_uncertain_action(
        "act-unresolved-bash",
        resolution="verified_not_applied",
        reason="operator_confirmed_never_ran",
    )
    clean_claim = coord.acquire_or_takeover(
        "task-ac2",
        "worker-3",
        session_id="sess-ac2",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=60.0,
    )
    assert clean_claim.task.state == "running"
    assert clean_claim.reconciliation.unresolved_action_ids == ()


def test_pre_takeover_reconciles_reconcilable_write_file_without_duplicate_write(
    isolated_env: Path,
) -> None:
    """Task 3: When an orphaned `in_flight` `write_file` already wrote exact target
    bytes before worker lease expiry, pre-takeover reconciliation verifies target
    state outside SQLite write locks, marks it `committed`, and enters `running`
    without duplicating the write."""
    coord = studio.get_worker_lease_coordinator(isolated_env)
    t0 = time.time() - 50.0
    coord.acquire_or_takeover(
        "task-reconcile-pre",
        "worker-1",
        session_id="sess-rec",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=10.0,
        now=t0,
    )
    write_args = {"path": "settings.json", "content": '{"mode": "safe"}\n'}
    b1 = coord.ledger.begin(
        action_id="act-write-pre",
        tool_name="write_file",
        args=write_args,
        policy="autonomous",
        action_class="managed_edit",
        allowed=True,
        explicit_approval=False,
        session_id="sess-rec",
        task_id="task-reconcile-pre",
        epoch=1,
        worker_id="worker-1",
        now=t0 + 1.0,
    )
    assert b1.proceed is True
    # Bytes reached disk before Worker 1 stalled past its 10s lease TTL:
    (isolated_env / "settings.json").write_text(write_args["content"], encoding="utf-8")

    takeover = coord.acquire_or_takeover(
        "task-reconcile-pre",
        "worker-2",
        session_id="sess-rec",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=60.0,
        known_action_args={"act-write-pre": write_args},
        now=time.time(),
    )
    assert takeover.epoch_record.epoch == 2
    assert takeover.task.state == "running"
    assert takeover.reconciliation.swept_in_flight_ids == ("act-write-pre",)
    assert takeover.reconciliation.reconciled_committed_ids == ("act-write-pre",)
    assert takeover.reconciliation.unresolved_action_ids == ()
    assert coord.ledger.get_action("act-write-pre").status == "committed"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Task 4: Sleep / wake clock jump & stale worker wake-up fencing
# ---------------------------------------------------------------------------


def test_sleep_wake_clock_jump_expires_lease_fences_waking_worker_and_recovers(
    isolated_env: Path,
) -> None:
    """Task 4: Simulate host sleep/wake clock jump past `lease_expires_at`."""
    coord = studio.get_worker_lease_coordinator(isolated_env)
    now = time.time()
    # Claim a 20s lease 300s in the past (simulating 5-minute laptop sleep).
    t_before_sleep = now - 300.0
    claim_before_sleep = coord.acquire_or_takeover(
        "task-sleep-wake",
        "worker-sleeping",
        session_id="sess-sleep",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=20.0,
        now=t_before_sleep,
    )
    assert claim_before_sleep.epoch_record.epoch == 1

    # Host wakes at `now`: Worker-sleeping attempts heartbeat -> durably commits `expired` and raises!
    with pytest.raises(rp.LeaseExpiredError, match="expired"):
        coord.heartbeat("task-sleep-wake", 1, "worker-sleeping", now=now)
    ep1_after_wake = coord.store.get_epoch("task-sleep-wake", 1)
    assert ep1_after_wake is not None
    assert ep1_after_wake.status == "expired"

    # Even if waking worker skips heartbeat and attempts direct tool execution -> denied!
    wake_denied = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(
                isolated_env,
                sid="sess-sleep",
                task_id="task-sleep-wake",
                epoch=1,
                worker_id="worker-sleeping",
            ),
            "write_file",
            {"path": "after_sleep.txt", "content": "stale-after-wake"},
            grant_source="policy",
        )
    )
    assert wake_denied["ok"] is False
    assert "lease has expired" in wake_denied["output"]
    assert not (isolated_env / "after_sleep.txt").exists()

    # Replacement worker cleanly recovers in epoch 2 after wake!
    wake_recovery = coord.acquire_or_takeover(
        "task-sleep-wake",
        "worker-awake",
        session_id="sess-sleep",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        lease_ttl_s=60.0,
        now=now + 1.0,
    )
    assert wake_recovery.epoch_record.epoch == 2
    wake_ok = asyncio.run(
        studio._execute_harness_tool(
            None,
            _sess(
                isolated_env,
                sid="sess-sleep",
                task_id="task-sleep-wake",
                epoch=2,
                worker_id="worker-awake",
            ),
            "write_file",
            {"path": "after_sleep.txt", "content": "recovered-epoch-2\n"},
            grant_source="policy",
        )
    )
    assert wake_ok["ok"] is True
    assert (isolated_env / "after_sleep.txt").read_text(encoding="utf-8") == "recovered-epoch-2\n"


# ---------------------------------------------------------------------------
# AC 3 + Task 4: Crash restart cannot duplicate action + Live FastAPI HTTP 409
# ---------------------------------------------------------------------------


def test_crash_restart_cannot_duplicate_committed_or_in_flight_actions(
    isolated_env: Path,
) -> None:
    """AC 3: Crash + process restart fences prior-boot worker leases immediately
    (without waiting for wall-clock TTL), never re-executes committed side effects,
    and holds tasks with interrupted mid-flight actions in `blocked`."""
    sess = studio.create_harness_session_db(
        title="Crash Restart Test",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="m1",
        critic_model="m2",
    )
    sid = sess["id"]
    coord, claim1, wid1 = studio._acquire_turn_lease_or_409(sess, lease_ttl_s=300.0)
    assert claim1.epoch_record.epoch == 1
    sess_e1 = studio._bind_lease_to_session(sess, task_id=sid, epoch=1, worker_id=wid1)

    marker_log = isolated_env / "deploy_once.log"
    bash_args = {"command": f"printf 'deployed\\n' >> {marker_log}"}

    # Step 1 call 0 commits in epoch 1 right before hard process crash (`SIGKILL`).
    r1 = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess_e1,
            "run_bash",
            bash_args,
            grant_source="policy",
            step_id=1,
            call_index=0,
        )
    )
    assert r1["ok"] is True
    assert marker_log.read_text(encoding="utf-8").splitlines() == ["deployed"]

    # Hard process crash & immediate restart (300s lease TTL has NOT elapsed on wall clock!).
    studio.simulate_process_restart()
    fenced_ep1 = coord.store.get_epoch(sid, 1)
    assert fenced_ep1 is not None
    assert fenced_ep1.status == "fenced"
    assert fenced_ep1.fence_reason == "prior_process_restart"

    # Post-restart Worker 2 acquires monotonic epoch 2 immediately and replays Step 1 call 0:
    coord2, claim2, wid2 = studio._acquire_turn_lease_or_409(sess, lease_ttl_s=60.0)
    assert claim2.epoch_record.epoch == 2
    sess_e2 = studio._bind_lease_to_session(sess, task_id=sid, epoch=2, worker_id=wid2)

    replay_res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess_e2,
            "run_bash",
            bash_args,
            grant_source="policy",
            step_id=1,
            call_index=0,
        )
    )
    assert replay_res["ok"] is False
    assert replay_res.get("already_committed") is True
    assert replay_res.get("replay_blocked") is True
    # Side effect ran ONCE across crash + restart!
    assert marker_log.read_text(encoding="utf-8").splitlines() == ["deployed"]

    # Now simulate crash MID-FLIGHT on Step 2 call 0 (`in_flight` on disk when process dies):
    mid_begin = coord2.ledger.begin(
        action_id=None,
        step_id=2,
        call_index=0,
        tool_name="run_bash",
        args={"command": "echo mid-crash"},
        policy="autonomous",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id=sid,
        task_id=sid,
        epoch=2,
        worker_id=wid2,
    )
    assert mid_begin.proceed is True and mid_begin.status == "in_flight"

    studio.simulate_process_restart()
    swept_mid = coord2.ledger.get_action(mid_begin.action_id)
    assert swept_mid is not None and swept_mid.status == "uncertain"
    assert coord2.store.get_task(sid).state == "blocked"  # type: ignore[union-attr]

    # Attempting to start a new turn via HTTP while `swept_mid` is unresolved returns HTTP 409!
    headers = {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}
    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        resp_409 = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Resume blindly"},
        )
        assert resp_409.status_code == 409
        assert mid_begin.action_id in resp_409.json()["detail"]


def test_live_http_harness_routes_enforce_409_single_owner_and_release_cleanly(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify live FastAPI turn endpoints reject concurrent turns with HTTP 409
    while leased, bind authoritative `(task_id, epoch, worker_id)` during tool
    execution, release cleanly on completion, and fence on `/api/harness/abort`."""
    sess = studio.create_harness_session_db(
        title="HTTP Lease Lifecycle",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="m1",
        critic_model="m2",
    )
    sid = sess["id"]

    call_num = 0

    async def _fake_stream_model(*_a: Any, **_kw: Any):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            tc = json.dumps({"name": "write_file", "arguments": {"path": "http_lease.txt", "content": "ok-http\n"}})
            yield {"type": "content", "text": f"Writing file.\n<tool_call>{tc}</tool_call>"}
        else:
            yield {"type": "content", "text": "All done."}

    monkeypatch.setattr(studio, "stream_any_model", _fake_stream_model)
    headers = {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        # Hold an active lease from another worker -> all turn routes return HTTP 409!
        coord = studio.get_worker_lease_coordinator(isolated_env)
        c1 = coord.acquire_or_takeover(sid, "external-worker-1", session_id=sid, lease_ttl_s=60.0)
        assert c1.epoch_record.epoch == 1

        for path, payload in [
            ("/api/harness/run", {"session_id": sid, "prompt": "Concurrent run"}),
            ("/api/harness/gsd-wave", {"session_id": sid, "prompt": "Concurrent gsd"}),
            ("/api/harness/sdlc/run", {"session_id": sid, "prompt": "Concurrent sdlc", "mode": "plan_only"}),
            ("/api/harness/sdlc/feedback", {"session_id": sid, "feedback": "Concurrent fb"}),
        ]:
            conflict = client.post(path, headers=headers, json=payload)
            assert conflict.status_code == 409, f"{path} must reject concurrent lease claim with 409"
            assert "external-worker-1" in conflict.json()["detail"]

        # Abort endpoint durably fences active epoch 1!
        ab = client.post(f"/api/harness/abort/{sid}", headers=headers)
        assert ab.status_code == 200
        assert coord.store.get_epoch(sid, 1).status == "fenced"  # type: ignore[union-attr]

        # Now `POST /api/harness/run` acquires epoch 2, executes `write_file`, and releases cleanly!
        ok_resp = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Write http_lease.txt", "policy": "autonomous", "max_steps": 3},
        )
        assert ok_resp.status_code == 200
        assert "event: turn_done" in ok_resp.text
        assert (isolated_env / "http_lease.txt").read_text(encoding="utf-8") == "ok-http\n"

        ep2 = coord.store.get_epoch(sid, 2)
        assert ep2 is not None and ep2.status == "released"
        task_after = coord.store.get_task(sid)
        assert task_after is not None and task_after.state == "completed"
        actions = coord.ledger.list_task_actions(sid)
        assert len(actions) == 1
        assert actions[0].epoch == 2
        assert actions[0].status == "committed"


# ---------------------------------------------------------------------------
# Standalone built `studio.py` parity check
# ---------------------------------------------------------------------------


def test_standalone_studio_bundle_enforces_worker_leases_and_fencing(tmp_path: Path) -> None:
    bundle_path = REPO_ROOT / "studio.py"
    spec = importlib.util.spec_from_file_location("studio_standalone_p104", bundle_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        mod.DB_PATH = tmp_path / "standalone_p104.db"
        mod.WORKSPACE_ROOT = ws
        mod.HARNESS_STORE_DIR = tmp_path / "harness_store"
        mod.HARNESS_STORE_DIR.mkdir(parents=True, exist_ok=True)
        mod.STUDIO_EXECUTION_MODE = "host"
        mod.STUDIO_ALLOW_HOST_EXECUTION = True
        mod.init_db()
        mod._set_default_projects_dir(str(ws), create_if_missing=True)

        coord = mod.get_worker_lease_coordinator(ws)
        t0 = time.time()
        c1 = coord.acquire_or_takeover(
            "task-sa-1", "sa-w1", session_id="sess-sa", workspace_dir=str(ws), policy="autonomous", lease_ttl_s=30.0, now=t0
        )
        assert c1.epoch_record.epoch == 1

        # Single-owner conflict enforced in standalone bundle:
        with pytest.raises(mod.LeaseOwnershipConflictError):
            coord.acquire_or_takeover("task-sa-1", "sa-w2", lease_ttl_s=30.0, now=t0 + 1.0)

        # Force takeover -> epoch 2 fences epoch 1 -> stale worker denied:
        c2 = coord.acquire_or_takeover(
            "task-sa-1", "sa-w2", session_id="sess-sa", workspace_dir=str(ws), policy="autonomous", force_takeover=True, now=t0 + 2.0
        )
        assert c2.epoch_record.epoch == 2
        stale_res = asyncio.run(
            mod._execute_harness_tool(
                None,
                {"id": "sess-sa", "policy": "autonomous", "worktree_dir": str(ws), "task_id": "task-sa-1", "epoch": 1, "worker_id": "sa-w1"},
                "write_file",
                {"path": "sa_stale.txt", "content": "nope"},
                grant_source="policy",
            )
        )
        assert stale_res["ok"] is False
        assert not (ws / "sa_stale.txt").exists()
    finally:
        sys.modules.pop(spec.name, None)
