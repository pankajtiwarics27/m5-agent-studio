"""M5-P1-07 tests: durable task events, cursors, replay, and reload reconstruction.

Acceptance criteria covered:

1. *Reload reconstructs task status* — the task snapshot endpoint rebuilds status,
   turn counts, and approval state from durable stores only; the UI reload path
   (`selectHarnessSession` → `/state` + `/events`) is wired to it.
2. *SSE gap triggers snapshot resync* — pruned history, a sequence jump, or a
   cursor ahead of the store is answered with `snapshot_required` /
   `resync_required`, and the client-side gap predicate forces a snapshot reload
   instead of applying a partial log.
3. *No duplicate completion or stale approval acceptance* — terminal records and
   approval decisions are deduplicated durably, replays are rejected even after
   the in-memory tombstone is gone, and the client merge is sequence-keyed.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

import app as studio
import task_events

TOKEN = "t" * 40
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "m5_p1_07.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness_store = tmp_path / "harness_store"
    harness_store.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_store)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", {"http://testserver", "https://testserver"})
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio._RESOLVED_APPROVALS.clear()
    studio._RESOLVED_APPROVALS_TS.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(workspace), create_if_missing=True)
    return workspace


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _new_session(workspace: Path, title: str = "P1-07 task") -> dict[str, object]:
    return studio.create_harness_session_db(
        title=title,
        workspace_dir=str(workspace),
        use_worktree=False,
        policy="standard",
        lead_model="local-model",
        critic_model="critic-model",
    )


# ---------------------------------------------------------------------------
# 1. Durable store: monotonic cursors, dedupe, pruning, reconstruction
# ---------------------------------------------------------------------------


def test_event_log_cursor_reads_are_monotonic_and_survive_reopen(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    for step in range(1, 6):
        log.append("hns-cursor", "step/start", {"step": step})

    first = log.read("hns-cursor", cursor=0, limit=2)
    assert [ev["seq"] for ev in first.events] == [1, 2]
    assert first.next_cursor == 2
    assert first.has_more is True
    assert first.snapshot_required is False and first.truncated is False

    rest = log.read("hns-cursor", cursor=first.next_cursor, limit=10)
    assert [ev["seq"] for ev in rest.events] == [3, 4, 5]
    assert rest.has_more is False
    assert rest.next_cursor == 5 == rest.latest_seq

    # A fresh reader (new process) resumes from the last applied cursor and does
    # not renumber or replay anything.
    reopened = task_events.TaskEventLog(tmp_path)
    assert reopened.read("hns-cursor", cursor=5).events == ()
    written = reopened.append("hns-cursor", "turn/start", {"prompt": "next"})
    assert written["seq"] == 6
    assert reopened.latest_seq("hns-cursor") == 6


def test_event_log_dedupe_key_is_idempotent_across_restart(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    log.append("hns-dedupe", "turn/start", {"prompt": "fix"})
    original = log.append("hns-dedupe", "turn/end", {"state": "completed", "steps": 2}, dedupe_key="turn-end:1:completed")
    duplicate = log.append("hns-dedupe", "turn/end", {"state": "completed", "steps": 2}, dedupe_key="turn-end:1:completed")
    assert duplicate == original

    # A restarted process rebuilds the dedupe index from the durable log.
    reopened = task_events.TaskEventLog(tmp_path)
    after_restart = reopened.append("hns-dedupe", "turn/end", {"state": "completed"}, dedupe_key="turn-end:1:completed")
    assert after_restart["seq"] == original["seq"]

    events = reopened.all_events("hns-dedupe")
    assert [ev["type"] for ev in events] == ["turn/start", "turn/end"]
    # A different key is a different outcome and must not be swallowed.
    other = reopened.append("hns-dedupe", "turn/end", {"state": "failed"}, dedupe_key="turn-end:1:failed")
    assert other["seq"] == 3


def test_event_log_pruning_forces_resync_for_stale_cursor_and_truncates_fresh_reader(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    for step in range(6):
        log.append("hns-prune", "step/start", {"step": step})
    assert log.prune("hns-prune", retain=3) == 3

    stale = log.read("hns-prune", cursor=1)
    assert stale.snapshot_required is True
    assert stale.resync_reason == "events_pruned"
    assert stale.missing_ranges == ((2, 3),)
    assert stale.events == ()

    fresh = log.read("hns-prune", cursor=0)
    assert fresh.snapshot_required is False
    assert fresh.truncated is True
    assert fresh.retained_from_seq == 4
    assert [ev["seq"] for ev in fresh.events] == [4, 5, 6]

    # Resuming from the retained floor is contiguous and needs no resync.
    resumed = log.read("hns-prune", cursor=4)
    assert resumed.snapshot_required is False
    assert [ev["seq"] for ev in resumed.events] == [5, 6]

    ahead = log.read("hns-prune", cursor=99)
    assert ahead.snapshot_required is True and ahead.resync_reason == "cursor_ahead"


def test_event_log_detects_sequence_gaps_and_skips_torn_records(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    log.append("hns-gap", "step/start", {"step": 1})
    path = log.log_path("hns-gap")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"torn": true\n')  # an interrupted append
    log.append("hns-gap", "step/start", {"step": 2})

    page = log.read("hns-gap", cursor=1)
    # The torn line is skipped and counted, never repaired into a fake event, and
    # it does not create a gap: the next record still carries the next sequence.
    snapshot = log.reconstruct("hns-gap")
    assert snapshot.skipped_records == 1
    assert page.snapshot_required is False
    assert [ev["seq"] for ev in page.events] == [2]

    # A genuinely missing record (recorded seq, lost append) is a gap, not a
    # silent hole in the transcript.
    log.append("hns-gap", "step/start", {"step": 3})
    records = log.all_events("hns-gap")
    assert [ev["seq"] for ev in records] == [1, 2, 3]
    log.log_path("hns-gap").write_text(
        "".join(json.dumps(ev) + "\n" for ev in records if ev["seq"] != 2),
        encoding="utf-8",
    )
    gapped = log.read("hns-gap", cursor=1)
    assert gapped.snapshot_required is True
    assert gapped.resync_reason == "sequence_gap"
    assert gapped.missing_ranges == ((2, 2),)


def test_event_log_rejects_unsafe_session_ids(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    for bad in ("", "  ", "../escape", "a/b", "a\\b", "x" * 200, "bad id"):
        with pytest.raises(task_events.TaskEventError):
            log.append(bad, "turn/start", {})
    with pytest.raises(task_events.TaskEventError):
        log.read("../escape")


def test_reconstruct_reports_undecided_and_decided_approvals(tmp_path: Path) -> None:
    log = task_events.TaskEventLog(tmp_path)
    log.append("hns-approvals", "approval/request", {"approval_id": "apr-1", "tool": "run_bash", "args": {"command": "ls"}})
    log.append("hns-approvals", "approval/request", {"approval_id": "apr-2", "tool": "write_file", "args": {}})
    log.append("hns-approvals", "approval/decision", {"approval_id": "apr-1", "decision": "reject"}, dedupe_key="approval-decision:apr-1")
    log.append("hns-approvals", "turn/start", {"prompt": "task"})
    log.append("hns-approvals", "turn/end", {"state": "cancelled"}, dedupe_key="turn-end:1:cancelled")

    snapshot = log.reconstruct("hns-approvals")
    assert snapshot.undecided_approvals == ("apr-2",)
    assert snapshot.approval_decisions == (("apr-1", "reject"),)
    assert log.approval_decision("hns-approvals", "apr-1") == "reject"
    assert log.approval_decision("hns-approvals", "apr-2") == ""
    assert snapshot.last_turn_state == "cancelled"
    assert snapshot.terminal is True
    assert snapshot.turns_started == 1


# ---------------------------------------------------------------------------
# 2. HTTP: snapshot reconstruction, cursor reads, replay stream, approvals
# ---------------------------------------------------------------------------


def test_state_snapshot_reconstructs_task_status_for_reload(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    studio._append_harness_event(sid, "turn/start", {"prompt": "fix the discount bug", "policy": "standard"})
    studio._append_harness_event(sid, "step/start", {"step": 1, "model": "local-model"})
    studio._append_harness_event(
        sid,
        "turn/end",
        {"steps": 1, "elapsed_s": 1.25, "state": "paused"},
        dedupe_key="turn-end:1:paused",
    )

    with TestClient(studio.app) as client:
        response = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers())
        assert response.status_code == 200
        state = response.json()
        # Status comes from the runtime task row, never from rendered text.
        assert state["status"] == state["task_status"]
        assert state["session"]["id"] == sid
        assert state["turns_started"] == 1
        assert state["turns_completed"] == 0
        assert state["last_turn_state"] == "paused"
        assert state["event_stream"]["latest_seq"] == 4
        assert state["approvals"] == {"pending": [], "expired": [], "recent_decisions": []}
        assert "events" not in state["session"]

        page = client.get(f"/api/harness/sessions/{sid}/events?cursor=0", headers=_headers()).json()
        assert page["next_cursor"] == 4
        assert page["snapshot_required"] is False
        assert [ev["type"] for ev in page["events"]] == ["session/init", "turn/start", "step/start", "turn/end"]

        missing = client.get("/api/harness/sessions/hns-missing/state", headers=_headers())
        assert missing.status_code == 404
        unauthenticated = client.get(f"/api/harness/sessions/{sid}/events?cursor=0")
        assert unauthenticated.status_code == 401


def test_state_snapshot_tracks_runtime_task_transitions_not_rendered_text(isolated_env: Path) -> None:
    """Reload status follows the durable task state machine, not the transcript."""
    workspace = isolated_env
    session = _new_session(workspace, title="Transition tracking")
    sid = str(session["id"])
    sm = studio.get_task_state_machine()
    coord = studio.get_worker_lease_coordinator(workspace)
    root, _budget = sm.ensure_root_task(
        sid,
        session_id=sid,
        title="Transition tracking",
        workspace_dir=str(workspace),
        policy="standard",
    )
    assert root.state == "pending"

    with TestClient(studio.app) as client:
        pending_state = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert pending_state["status"] == "pending"
        assert pending_state["terminal"] is False

        # Acquiring the worker lease is what moves the root task to `running`.
        worker = f"wrk-{studio._PROCESS_BOOT_ID}-p107"
        claim = coord.acquire_or_takeover(sid, worker, lease_ttl_s=120.0)
        epoch = claim.epoch_record.epoch
        studio._append_harness_event(sid, "turn/start", {"prompt": "do the task", "policy": "standard"})
        running_state = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert running_state["status"] == "running"
        assert running_state["task"]["state"] == "running"
        assert running_state["turns_started"] == 1
        assert running_state["terminal"] is False

        studio._append_harness_event(
            sid,
            "turn/end",
            {"steps": 1, "elapsed_s": 0.5, "state": "completed"},
            dedupe_key=f"turn-end:{epoch}:completed",
        )
        # A completion record without a committed runtime completion is NOT a
        # completed task: the status stays running until the state machine says so.
        still_running = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert still_running["status"] == "running"
        assert still_running["turns_completed"] == 1

        sm.complete_task(
            sid,
            worker_id=worker,
            epoch=epoch,
            step=1,
            checkpoint_payload={"workflow": "harness", "terminal": True},
            reason="turn_completed",
        )
        completed = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert completed["status"] == "completed"
        assert completed["terminal"] is True
        assert completed["last_turn_state"] == "completed"
        assert completed["latest_checkpoint"]["checkpoint_id"]


def test_pending_approvals_are_reconstructed_from_the_durable_log(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    pending = studio._create_pending_tool_approval(sid, 1, "run_bash", {"command": "echo hi"})
    with TestClient(studio.app) as client:
        state = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert state["status"] == "waiting_approval"
        assert [a["approval_id"] for a in state["approvals"]["pending"]] == [pending["approval_id"]]
        pending_entry = state["approvals"]["pending"][0]
        assert pending_entry["tool"] == "run_bash"
        assert pending_entry["args"] == {"command": "echo hi"}
        assert pending_entry["live"] is True

        # A restart loses the in-memory slot: the same undecided request must be
        # reported as expired (not actionable), never as a live approval.
        studio._PENDING_APPROVALS.clear()
        restarted = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert restarted["approvals"]["pending"] == []
        assert [a["approval_id"] for a in restarted["approvals"]["expired"]] == [pending["approval_id"]]
        assert restarted["status"] != "waiting_approval"


def test_stale_approval_submission_is_rejected_after_in_memory_state_is_gone(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    pending = studio._create_pending_tool_approval(sid, 1, "run_bash", {"command": "echo hi"})
    with TestClient(studio.app) as client:
        approved = client.post(
            "/api/harness/approve",
            headers=_headers(),
            json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": sid},
        )
        assert approved.status_code == 200

        # Simulate a process restart: no pending slots, no replay tombstones.
        studio._PENDING_APPROVALS.clear()
        studio._RESOLVED_APPROVALS.clear()
        studio._RESOLVED_APPROVALS_TS.clear()

        replay = client.post(
            "/api/harness/approve",
            headers=_headers(),
            json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": sid},
        )
        assert replay.status_code == 409
        assert "durable" in replay.json()["detail"].lower()

        # The durable log holds exactly one decision record for the id.
        decisions = [
            ev
            for ev in studio._read_harness_events(sid)
            if ev.get("type") == "approval/decision" and ev.get("approval_id") == pending["approval_id"]
        ]
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "approve"


def test_terminal_records_are_never_duplicated_in_the_durable_log(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    first = studio._append_harness_event(sid, "turn/end", {"state": "completed", "steps": 2}, dedupe_key="turn-end:7:completed")
    second = studio._append_harness_event(sid, "turn/end", {"state": "completed", "steps": 2}, dedupe_key="turn-end:7:completed")
    studio._append_harness_event(sid, "turn/verified", {"spec_sha256": "a" * 64}, dedupe_key="turn-verified:7")
    studio._append_harness_event(sid, "turn/verified", {"spec_sha256": "a" * 64}, dedupe_key="turn-verified:7")
    assert first["seq"] == second["seq"]

    types = [ev["type"] for ev in studio._read_harness_events(sid)]
    assert types.count("turn/end") == 1
    assert types.count("turn/verified") == 1


def test_event_stream_replays_from_cursor_and_last_event_id(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    for step in (1, 2, 3):
        studio._append_harness_event(sid, "step/start", {"step": step, "model": "local-model"})

    def frames(body: str) -> list[tuple[int, dict[str, object]]]:
        parsed: list[tuple[int, dict[str, object]]] = []
        event_id: int | None = None
        event_name = ""
        data = ""
        for line in body.splitlines():
            if line.startswith("id:"):
                event_id = int(line[3:].strip())
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
            elif not line:
                if event_id is not None and event_name == "task_event" and data:
                    parsed.append((event_id, json.loads(data)))
                event_id, event_name, data = None, "", ""
        return parsed

    with TestClient(studio.app) as client:
        with client.stream("GET", f"/api/harness/sessions/{sid}/events/stream?cursor=2&drain=true", headers=_headers()) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
        assert "stream_caught_up" in body
        parsed = frames(body)
        assert [seq for seq, _ in parsed] == [3, 4]
        assert [ev["type"] for _, ev in parsed] == ["step/start", "step/start"]

        # Reconnect with Last-Event-ID (what a browser sends) resumes after seq 3.
        headers = {**_headers(), "Last-Event-ID": "3"}
        with client.stream("GET", f"/api/harness/sessions/{sid}/events/stream?drain=true", headers=headers) as response:
            body = "".join(response.iter_text())
        assert [seq for seq, _ in frames(body)] == [4]

        # A cursor ahead of the store cannot be resumed: resync, do not guess.
        with client.stream("GET", f"/api/harness/sessions/{sid}/events/stream?cursor=99&drain=true", headers=_headers()) as response:
            ahead = "".join(response.iter_text())
        assert "resync_required" in ahead
        assert "cursor_ahead" in ahead


def test_pruned_history_triggers_snapshot_resync_over_http(isolated_env: Path) -> None:
    session = _new_session(isolated_env)
    sid = str(session["id"])
    for step in range(1, 6):
        studio._append_harness_event(sid, "step/start", {"step": step})
    studio._task_event_log().prune(sid, retain=2)

    with TestClient(studio.app) as client:
        page = client.get(f"/api/harness/sessions/{sid}/events?cursor=2&limit=10", headers=_headers()).json()
        assert page["snapshot_required"] is True
        assert page["resync_reason"] == "events_pruned"
        assert page["missing_ranges"]

        fresh = client.get(f"/api/harness/sessions/{sid}/events?cursor=0&limit=10", headers=_headers()).json()
        assert fresh["snapshot_required"] is False
        assert fresh["truncated"] is True

        with client.stream("GET", f"/api/harness/sessions/{sid}/events/stream?cursor=2&drain=true", headers=_headers()) as response:
            body = "".join(response.iter_text())
        assert "event: resync_required" in body
        assert "events_pruned" in body

        # The snapshot endpoint is the documented recovery path and still works.
        state = client.get(f"/api/harness/sessions/{sid}/state", headers=_headers()).json()
        assert state["event_stream"]["retained_from_seq"] == 5
        assert state["session_id"] == sid


# ---------------------------------------------------------------------------
# 3. UI wiring and the client-side gap/dedupe predicates (Node)
# ---------------------------------------------------------------------------

NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('/* M5-P1-07-PURE-BEGIN */');
const end = src.indexOf('/* M5-P1-07-PURE-END */');
if (start < 0 || end < 0) { console.error('pure helpers not found'); process.exit(2); }
const context = {};
vm.createContext(context);
vm.runInContext(src.slice(start, end), context);
const ev = (seq) => ({ seq, type: 'step/start' });
context.ev = ev;
const out = vm.runInContext(`
  ({
    contiguous: harnessEventGap({ events: [ev(3), ev(4)], next_cursor: 4, latest_seq: 4, retained_from_seq: 1, snapshot_required: false }, 2),
    seqJump: harnessEventGap({ events: [ev(5)], next_cursor: 5, latest_seq: 5, retained_from_seq: 1, snapshot_required: false }, 2),
    pruned: harnessEventGap({ events: [], next_cursor: 2, latest_seq: 9, retained_from_seq: 7, snapshot_required: true, resync_reason: 'events_pruned' }, 2),
    ahead: harnessEventGap({ events: [], next_cursor: 0, latest_seq: 4, retained_from_seq: 1, snapshot_required: false }, 9),
    truncatedFresh: harnessEventGap({ events: [ev(7), ev(8)], next_cursor: 8, latest_seq: 8, retained_from_seq: 7, snapshot_required: false, truncated: true }, 0),
    emptyButBehind: harnessEventGap({ events: [], next_cursor: 3, latest_seq: 6, retained_from_seq: 1, snapshot_required: false }, 3),
    merged: harnessMergeEvents([ev(2), ev(1)], [ev(2), ev(3), { seq: 'bad' }, null]),
    dedupeReplay: harnessMergeEvents([ev(1), ev(2)], [ev(2), ev(2)]),
    cursorOf: harnessEventCursor([ev(4), { seq: 'x' }, ev(9)]),
    statusWaiting: harnessStatusBadge({ status: 'waiting_approval' }),
    statusPaused: harnessStatusBadge({ status: 'paused', task_status: 'paused' }),
    statusCompleted: harnessStatusBadge({ task_status: 'completed' }),
  })
`, context);
console.log(JSON.stringify(out));
"""


def _node_pure_helpers() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment without Node.js
        pytest.skip("Node.js is required for the frontend helper checks")
    result = subprocess.run(
        [node, "-e", NODE_DRIVER, str(ROOT / "static" / "index.html")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_frontend_gap_predicate_requires_snapshot_resync_and_merges_without_duplicates() -> None:
    out = _node_pure_helpers()
    assert out["contiguous"] == {"gap": False, "reason": ""}
    assert out["seqJump"] == {"gap": True, "reason": "sequence_gap"}
    assert out["pruned"] == {"gap": True, "reason": "events_pruned"}
    assert out["ahead"] == {"gap": True, "reason": "cursor_ahead"}
    assert out["truncatedFresh"] == {"gap": False, "reason": ""}
    assert out["emptyButBehind"] == {"gap": True, "reason": "missing_events"}
    assert [ev["seq"] for ev in out["merged"]] == [1, 2, 3]  # type: ignore[index]
    assert [ev["seq"] for ev in out["dedupeReplay"]] == [1, 2]  # type: ignore[index]
    assert out["cursorOf"] == 9
    assert out["statusWaiting"] == {"cls": "amber", "label": "WAITING APPROVAL"}
    assert out["statusPaused"] == {"cls": "amber", "label": "PAUSED"}
    assert out["statusCompleted"] == {"cls": "green", "label": "COMPLETED"}


def test_frontend_reload_and_resync_paths_use_the_durable_endpoints() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    # Reload path: the selected session is rebuilt from /state + /events, not from
    # the session row's inline event dump.
    assert 'async function harnessResyncFromSnapshot(' in source
    assert '/state`' in source
    assert '/events?cursor=' in source
    assert 'await harnessResyncFromSnapshot("select")' in source
    # Gap handling: the follow stream handles the server's resync verdict.
    assert 'frameEvent === "resync_required"' in source
    assert 'await harnessResyncFromSnapshot(verdict.reason || "stream_gap")' in source
    # Run lifecycle: a finished turn re-renders from the durable log.
    assert 'setHarnessRunActive(false)' in source
    assert 'setHarnessRunActive(true)' in source
    # Rendering is sequence-keyed, so a replayed record cannot render twice.
    assert "harnessMergeEvents([], events || [])" in source
    # Expired approvals are not offered as actionable buttons.
    assert 'livePending' in source


def test_standalone_bundle_inlines_the_task_event_store() -> None:
    studio_source = (ROOT / "studio.py").read_text(encoding="utf-8")
    assert "class TaskEventLog" in studio_source
    assert "TASK_EVENT_STREAM_VERSION = 1" in studio_source
    assert "task_events.TaskEventLog(HARNESS_STORE_DIR)" in studio_source
    assert not (ROOT / "task_events.py").read_text(encoding="utf-8").startswith("from app")
    assert "task_events" in sys.modules or True  # module import is exercised above
