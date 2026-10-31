"""M5-P3-03 verification suite: Process supervision and in-flight containment.

Covers all M5-P3-03 implementation tasks and acceptance criteria:
1. Subprocess identity tracking (`pid`, `pgid`, `uid`, `start_fingerprint`,
   `container_name`, `supervision_id`) and PID-recycling safety (`probe_process`,
   `is_same_live_process`, `discover_process_tree`).
2. Cooperative `SIGTERM` -> bounded grace -> `SIGKILL` escalation across both
   process groups and `os.setsid()` double-forked detached descendants, container
   cleanup hooks, normal-exit detached daemon sweeping, stdio MCP containment,
   and lingering unkillable process (`D`-state) fail-closed containment.
3. AC 1: Stale, superseded, foreign-boot, cancelled, and security-incident-fenced
   execution origins cannot admit new actions across any action class.
4. AC 2: Unresolved side effects and persisted containment barriers block
   conflicting actions and conflicting task/workspace resumption while allowing
   non-conflicting workspaces and safe inspection/reconciliation.
5. AC 3: Durable containment state (`<HARNESS_STORE_DIR>/containment/state.json`,
   `0700` dir / `0600` file) survives simulated mid-flight crashes and multiple
   process restarts, failing closed on corruption or symlink tampering.
6. Standalone `studio.py` single-file bundle parity (`python -I` outside checkout)
   and zero top-level AST symbol collisions across `INLINED_MODULES`.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

import app as studio
import execution_profiles as ep
import execution_spine as spine
import mcp_client
import process_supervision as ps
from scripts import build_studio
import side_effect_ledger as sel
import workflow_checkpoints as wc

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "studio_p303.db"
    store_dir = tmp_path / "harness_store"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_SKILL_CODE", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_MCP_STDIO", True)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "p303-test-token-" + ("a" * 32))
    studio._reset_execution_profile_controller(None)
    studio._reset_process_supervisor(None)
    studio._HARNESS_ABORT_FLAGS.clear()
    studio.init_db()
    return ws


def _wait_until(predicate: Any, *, timeout_s: float = 4.0, step_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step_s)
    return bool(predicate())


def _make_session(
    ws: Path,
    *,
    sid: str = "sess-p303",
    policy: str = "autonomous",
    task_id: str | None = None,
    epoch: int | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    s: dict[str, Any] = {
        "id": sid,
        "workspace_dir": str(ws),
        "worktree_dir": str(ws),
        "policy": policy,
        "lead_model": "demo",
        "critic_model": "demo",
    }
    if task_id is not None:
        s["task_id"] = task_id
    if epoch is not None:
        s["epoch"] = epoch
    if worker_id is not None:
        s["worker_id"] = worker_id
    return s


# ---------------------------------------------------------------------------
# Task 1: Subprocess identity tracking, tree discovery & PID-recycling safety
# ---------------------------------------------------------------------------


def test_subprocess_identity_probe_and_pid_recycling_protection(tmp_path: Path) -> None:
    """Task 1: `probe_process` captures real OS `(pid, pgid, uid, start_fingerprint)`
    and `terminate_record_sync` never signals PID <= 1, Studio's own PID/PGID, or a
    recycled PID whose `start_fingerprint` does not match."""
    assert ps.probe_process(0) is None
    assert ps.probe_process(1) is None
    assert ps.probe_process(-5) is None

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        probe = ps.probe_process(proc.pid)
        assert probe is not None
        assert probe.pid == proc.pid
        assert probe.pgid == proc.pid
        assert probe.start_fingerprint != ""
        assert ps.is_same_live_process(proc.pid, probe.start_fingerprint) is True
        assert ps.is_same_live_process(proc.pid, "forged-recycled-start-fingerprint") is False

        # Simulate PID recycling: record holds `proc.pid` with an older start_fingerprint.
        # `terminate_record_sync` MUST NOT kill the innocent live `proc`!
        store = ps.ContainmentStore(tmp_path / "store")
        signaled: list[tuple[int, int, bool]] = []
        sup = ps.ProcessSupervisor(
            store,
            signal_fn=lambda target, sig, is_pg: signaled.append((target, sig, is_pg)),
        )
        stale_rec = ps.SupervisedProcessRecord(
            supervision_id="sup-0011223344556677",
            pid=proc.pid,
            pgid=proc.pid,
            uid=probe.uid,
            start_fingerprint="stale-prior-process-fingerprint",
            container_name=None,
            tool_name="run_bash",
            session_id="sess-recycle",
            task_id="task-recycle",
            epoch=1,
            worker_id="worker-1",
            boot_id="oldboot",
            action_id="act-recycle",
            workspace_root=str(tmp_path),
            barriers=((f"workspace:{tmp_path}", "exclusive"),),
            started_at=time.time() - 50.0,
        )
        outcome = sup.terminate_record_sync(stale_rec, reason="startup_sweep")
        assert outcome.signaled_pids == ()
        assert signaled == []
        assert proc.poll() is None  # Innocent recycled process was NOT touched!
    finally:
        proc.kill()
        proc.wait(timeout=3)


def test_discover_process_tree_finds_group_children_and_setsid_detached_descendants(
    tmp_path: Path,
) -> None:
    """Task 1 + Task 4: `discover_process_tree` discovers both in-group children and
    `os.setsid()` double-forked detached daemons via `M5_SUPERVISION_ID`."""
    store = ps.ContainmentStore(tmp_path / "store")
    sup = ps.ProcessSupervisor(store)
    sup_id = sup.new_supervision_id()
    pids_file = tmp_path / "spawned_pids.json"

    spawner_code = textwrap.dedent(
        f"""
        import json, os, subprocess, sys, time
        group_child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            close_fds=True,
        )
        # Double-fork + setsid detached child whose ppid becomes 1 / subreaper and pgid != parent.pgid!
        detached_code = (
            "import os, subprocess, sys; "
            "os.setsid(); "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
            "print(p.pid)"
        )
        det_out = subprocess.check_output([sys.executable, "-c", detached_code], text=True).strip()
        detached_pid = int(det_out)
        with open({str(pids_file)!r}, "w", encoding="utf-8") as fh:
            json.dump({{"group_child": group_child.pid, "detached_child": detached_pid}}, fh)
        time.sleep(30)
        """
    )

    async def _exercise() -> tuple[int, int, int]:
        env = sup.prepare_spawn_env(os.environ, supervision_id=sup_id, session_id="s-tree")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            spawner_code,
            start_new_session=True,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rec = sup.register_process(
            proc,
            supervision_id=sup_id,
            tool_name="run_bash",
            workspace_root=tmp_path,
            session_id="s-tree",
        )
        assert _wait_until(pids_file.exists, timeout_s=4.0)
        spawned = json.loads(pids_file.read_text(encoding="utf-8"))
        grp_pid = int(spawned["group_child"])
        det_pid = int(spawned["detached_child"])

        in_group, detached = ps.discover_process_tree(rec)
        in_group_pids = {p.pid for p in in_group}
        detached_pids = {p.pid for p in detached}
        assert proc.pid in in_group_pids
        assert grp_pid in in_group_pids
        assert det_pid in (detached_pids | in_group_pids)

        outcome = await sup.terminate_record_async(rec, reason="test_cleanup")
        assert outcome.cleanly_reaped is True
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        return int(proc.pid or 0), grp_pid, det_pid

    leader_pid, grp_pid, det_pid = asyncio.run(_exercise())
    assert _wait_until(
        lambda: all(ps.probe_process(p) is None for p in (leader_pid, grp_pid, det_pid)),
        timeout_s=2.0,
    )


# ---------------------------------------------------------------------------
# Task 2: Cooperative cancel, bounded SIGKILL escalation, detached sweep & D-state
# ---------------------------------------------------------------------------


def test_cooperative_sigterm_exits_cleanly_without_sigkill_escalation(tmp_path: Path) -> None:
    """Task 2: A subprocess honoring `SIGTERM` exits within the cooperative grace window
    without escalating to `SIGKILL`."""
    store = ps.ContainmentStore(tmp_path / "store")
    sup = ps.ProcessSupervisor(store, grace_timeout_s=0.6)
    sup_id = sup.new_supervision_id()
    ready_file = tmp_path / "ready.flag"
    graceful_file = tmp_path / "graceful_exit.flag"

    coop_code = textwrap.dedent(
        f"""
        import pathlib, signal, sys, time
        def _on_term(signum, frame):
            pathlib.Path({str(graceful_file)!r}).write_text("clean-sigterm\\n", encoding="utf-8")
            sys.exit(0)
        signal.signal(signal.SIGTERM, _on_term)
        pathlib.Path({str(ready_file)!r}).write_text("ready\\n", encoding="utf-8")
        while True:
            time.sleep(0.05)
        """
    )

    async def _run() -> ps.TerminationOutcome:
        env = sup.prepare_spawn_env(os.environ, supervision_id=sup_id)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            coop_code,
            start_new_session=True,
            env=env,
        )
        rec = sup.register_process(
            proc,
            supervision_id=sup_id,
            tool_name="ctx_execute_python",
            workspace_root=tmp_path,
            session_id="sess-coop",
        )
        while not ready_file.exists():
            await asyncio.sleep(0.02)
        out = await sup.terminate_record_async(rec, reason="cooperative_cancel")
        await asyncio.wait_for(proc.wait(), timeout=1.0)
        return out

    outcome = asyncio.run(_run())
    assert outcome.cooperative_sent is True
    assert outcome.escalated_kill is False
    assert outcome.cleanly_reaped is True
    assert graceful_file.read_text(encoding="utf-8") == "clean-sigterm\n"


def test_sigterm_ignoring_tree_and_setsid_child_escalate_to_sigkill_and_clean_container(
    tmp_path: Path,
) -> None:
    """Task 2 + Task 4: When both leader and `os.setsid()` detached child ignore `SIGTERM`,
    `ProcessSupervisor` escalates to `SIGKILL` after the bounded grace period and invokes
    container cleanup."""
    cleaned_containers: list[str] = []

    async def _fake_container_cleanup(name: str) -> None:
        cleaned_containers.append(name)

    store = ps.ContainmentStore(tmp_path / "store")
    sup = ps.ProcessSupervisor(
        store,
        grace_timeout_s=0.08,
        kill_timeout_s=0.5,
        async_container_cleanup_fn=_fake_container_cleanup,
    )
    sup_id = sup.new_supervision_id()
    det_pid_file = tmp_path / "stubborn_detached.pid"

    stubborn_code = textwrap.dedent(
        f"""
        import pathlib, signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        det_script = (
            "import os, pathlib, signal, time; "
            "os.setsid(); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(det_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(30)"
        )
        subprocess.Popen(
            [sys.executable, "-c", det_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        while True:
            time.sleep(0.05)
        """
    )

    async def _run() -> tuple[ps.TerminationOutcome, int, int]:
        env = sup.prepare_spawn_env(os.environ, supervision_id=sup_id)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            stubborn_code,
            start_new_session=True,
            env=env,
        )
        rec = sup.register_process(
            proc,
            supervision_id=sup_id,
            tool_name="run_bash",
            workspace_root=tmp_path,
            container_name="m5-studio-testcontainer",
            session_id="sess-stubborn",
        )
        while not det_pid_file.exists():
            await asyncio.sleep(0.02)
        det_pid = int(det_pid_file.read_text(encoding="utf-8").strip())
        out = await sup.terminate_record_async(rec, reason="timeout_escalation")
        await asyncio.wait_for(proc.wait(), timeout=1.0)
        return out, int(proc.pid or 0), det_pid

    outcome, leader_pid, det_pid = asyncio.run(_run())
    assert outcome.cooperative_sent is True
    assert outcome.escalated_kill is True
    assert outcome.cleanly_reaped is True
    assert det_pid in outcome.signaled_pids
    assert cleaned_containers == ["m5-studio-testcontainer"]
    assert ps.probe_process(leader_pid) is None
    assert ps.probe_process(det_pid) is None


def test_normal_leader_exit_sweeps_orphaned_setsid_background_daemon(
    isolated_env: Path,
) -> None:
    """Task 2 + Task 4: Even when `run_bash` leader exits with `returncode == 0`,
    any `os.setsid()` background daemon it spawned is swept before the workspace
    barrier is released so no orphan can mutate files after tool completion."""
    daemon_pid_file = isolated_env / "bg_daemon.pid"
    late_mutation = isolated_env / "late_orphan_write.txt"
    spawner = isolated_env / "spawn_bg.py"
    spawner.write_text(
        textwrap.dedent(
            """
            import os, pathlib, subprocess, sys, time
            code = (
                "import os, pathlib, time; os.setsid(); "
                "pathlib.Path('bg_daemon.pid').write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(2.0); "
                "pathlib.Path('late_orphan_write.txt').write_text('escaped', encoding='utf-8')"
            )
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            deadline = time.time() + 3.0
            while not pathlib.Path("bg_daemon.pid").exists() and time.time() < deadline:
                time.sleep(0.02)
            """
        ),
        encoding="utf-8",
    )
    sess = _make_session(isolated_env, sid="sess-bg-sweep")
    res = asyncio.run(
        studio._execute_harness_tool(
            None,
            sess,
            "run_bash",
            {"command": "python3 spawn_bg.py"},
            grant_source="policy",
            call_index=1,
        )
    )
    assert res["ok"] is True, res
    assert daemon_pid_file.exists()
    bg_pid = int(daemon_pid_file.read_text(encoding="utf-8").strip())
    assert ps.probe_process(bg_pid) is None
    assert not late_mutation.exists()


def test_unkillable_lingering_child_blocks_all_overlapping_barriers_including_reads_until_exit(
    isolated_env: Path,
) -> None:
    """Task 2 + Task 3: If an unkillable (`D`-state) child remains alive after `SIGKILL`,
    `lingering_pids` is persisted and blocks ALL overlapping barriers — including `read_only`
    `read_file`, `rollback_edits(acknowledge_unresolved=True)`, and premature hold release —
    until the OS process actually exits."""
    (isolated_env / "target.txt").write_text("baseline\n", encoding="utf-8")
    fake_d_pid = 999_123
    fake_fp = "d-state-start-999123"
    d_state_alive = True

    real_probe = ps.probe_process

    def _probe_with_d_state(pid: int) -> ps.ProcessProbe | None:
        if pid == fake_d_pid:
            if d_state_alive:
                return ps.ProcessProbe(
                    pid=fake_d_pid,
                    ppid=1,
                    pgid=fake_d_pid,
                    uid=os.getuid() if hasattr(os, "getuid") else 0,
                    state="D",
                    start_fingerprint=fake_fp,
                    alive=True,
                )
            return None
        return real_probe(pid)

    store = ps.ContainmentStore(studio.HARNESS_STORE_DIR, probe_fn=_probe_with_d_state)
    sup = ps.ProcessSupervisor(
        store,
        probe_fn=_probe_with_d_state,
        discover_fn=lambda _rec: (
            [],
            [
                ps.ProcessProbe(
                    pid=fake_d_pid,
                    ppid=1,
                    pgid=fake_d_pid,
                    uid=0,
                    state="D",
                    start_fingerprint=fake_fp,
                    alive=True,
                )
            ]
            if d_state_alive
            else [],
        ),
        signal_fn=lambda _target, _sig, _is_pg: None,
        grace_timeout_s=0.02,
        kill_timeout_s=0.04,
    )
    studio._reset_process_supervisor(sup)

    ws_resolved = str(isolated_env.resolve())
    rec = ps.SupervisedProcessRecord(
        supervision_id="sup-dddddddddddddddd",
        pid=fake_d_pid,
        pgid=fake_d_pid,
        uid=0,
        start_fingerprint=fake_fp,
        container_name=None,
        tool_name="run_bash",
        session_id="sess-dstate",
        task_id="sess-dstate",
        epoch=1,
        worker_id="worker-1",
        boot_id=studio._PROCESS_BOOT_ID,
        action_id="act-dstate-1",
        workspace_root=ws_resolved,
        barriers=((f"workspace:{ws_resolved}", "exclusive"),),
        started_at=time.time(),
    )
    store.record_process_started(rec)
    term_out = sup.terminate_record_sync(rec, reason="cancel_with_d_state")
    assert term_out.cleanly_reaped is False
    assert term_out.lingering_pids == ((fake_d_pid, fake_fp),)

    # Premature hold release MUST raise ContainmentHoldActiveError while lingering PID lives!
    with pytest.raises(ps.ContainmentHoldActiveError):
        store.release_holds_for_action("act-dstate-1")

    # Bind profile coordinator using the D-state-aware ContainmentStore:
    controller = ep.UnifiedExecutionController()
    coord = controller.bind(
        workspace_root=isolated_env,
        store_dir=studio.HARNESS_STORE_DIR,
        containment_store=store,
        execution_mode_fn=lambda: "host",
    )
    # Even `read_only` `read_file` is blocked while a live unkillable process still holds the barrier!
    read_out = coord.admit_profile(
        spine.ActionRequest("read_file", {"path": "target.txt"}, "autonomous", False, "sess-other"),
        effective_policy="autonomous",
        action_class="read_only",
    )
    assert read_out.allowed is False
    assert read_out.error_code == "lingering_process_containment"
    assert str(fake_d_pid) in read_out.reason

    # Once the D-state process finally exits in the OS:
    d_state_alive = False
    # `read_only` inspection is immediately allowed, while mutating `write_file` stays blocked until reconciled!
    read_after_exit = coord.admit_profile(
        spine.ActionRequest("read_file", {"path": "target.txt"}, "autonomous", False, "sess-other"),
        effective_policy="autonomous",
        action_class="read_only",
    )
    assert read_after_exit.allowed is True
    coord.release_only(read_after_exit.handle)

    write_still_blocked = coord.admit_profile(
        spine.ActionRequest(
            "write_file",
            {"path": "target.txt", "content": "overwrite"},
            "autonomous",
            False,
            "sess-other",
        ),
        effective_policy="autonomous",
        action_class="managed_edit",
    )
    assert write_still_blocked.allowed is False
    assert write_still_blocked.error_code == "unresolved_containment_barrier"

    # Now explicit reconciliation succeeds and releases the persisted hold!
    assert store.release_holds_for_action("act-dstate-1") == 1
    write_after_reconcile = coord.admit_profile(
        spine.ActionRequest(
            "write_file",
            {"path": "target.txt", "content": "overwrite"},
            "autonomous",
            False,
            "sess-other",
        ),
        effective_policy="autonomous",
        action_class="managed_edit",
    )
    assert write_after_reconcile.allowed is True
    coord.release_only(write_after_reconcile.handle)


def test_mcp_stdio_post_dispatch_timeout_contains_detached_child_and_persists_barrier(
    isolated_env: Path,
) -> None:
    """Task 2: Stdio MCP server spawning an `os.setsid()` child and hanging during
    `tools/call` is contained completely, leaves zero live child processes, marks the
    action `uncertain`, and persists a workspace containment barrier."""
    mcp_det_pid_file = isolated_env / "mcp_detached.pid"
    mcp_server_script = isolated_env / "hanging_mcp_server.py"
    mcp_server_script.write_text(
        textwrap.dedent(
            f"""
            import json, os, pathlib, subprocess, sys, time
            for raw in sys.stdin:
                msg = json.loads(raw)
                if msg.get("method") == "initialize":
                    resp = {{
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {{
                            "protocolVersion": "2024-11-05",
                            "capabilities": {{"tools": {{}}}},
                            "serverInfo": {{"name": "hang-mcp", "version": "1.0"}},
                        }},
                    }}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
                elif msg.get("method") == "tools/list":
                    resp = {{
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {{
                            "tools": [
                                {{
                                    "name": "mutator",
                                    "description": "Mutates external target",
                                    "inputSchema": {{"type": "object"}},
                                }}
                            ]
                        }},
                    }}
                    sys.stdout.write(json.dumps(resp) + "\\n")
                    sys.stdout.flush()
                elif msg.get("method") == "tools/call":
                    det = (
                        "import os, pathlib, time; os.setsid(); "
                        f"pathlib.Path({str(mcp_det_pid_file)!r}).write_text(str(os.getpid())); "
                        "time.sleep(30)"
                    )
                    subprocess.Popen(
                        [sys.executable, "-c", det],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                    )
                    time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    studio.upsert_mcp_server(
        {
            "alias": "stdiohang",
            "name": "Stdio Hang",
            "transport": "stdio",
            "url": "",
            "command": sys.executable,
            "args": [str(mcp_server_script)],
            "env": {},
            "require_approval": False,
            "enabled": True,
            "tools": [
                {
                    "name": "mutator",
                    "description": "Mutates external target",
                    "inputSchema": {"type": "object"},
                }
            ],
            "enabled_tools": ["mutator"],
        }
    )
    asyncio.run(studio.sync_mcp_server_tools(None, "stdiohang"))  # type: ignore[arg-type]

    coord = studio.get_worker_lease_coordinator(isolated_env)
    claim = coord.acquire_or_takeover(
        "task-mcp-stdio",
        "worker-mcp-1",
        session_id="sess-mcp-stdio",
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )

    srv = studio.get_mcp_server("stdiohang", include_secret=True)

    async def _invoke_with_short_timeout() -> None:
        await studio._run_authorized_tool(
            "mcp__stdiohang__mutator",
            {"x": 1},
            "autonomous",
            approved=True,
            session_id="sess-mcp-stdio",
            executor=lambda: studio._mcp_stdio_rpc(
                srv,
                "tools/call",
                {"name": "mutator", "arguments": {"x": 1}},
                cwd=isolated_env,
                timeout_sec=0.35,
            ),
            denial_as_exception=True,
            grant_source="operator_one_call",
            task_id="task-mcp-stdio",
            epoch=claim.epoch_record.epoch,
            worker_id="worker-mcp-1",
            action_id="act-mcp-stdio-hang",
            workspace_root=isolated_env,
        )

    with pytest.raises(mcp_client.McpAmbiguousOutcomeError):
        asyncio.run(_invoke_with_short_timeout())

    assert mcp_det_pid_file.exists()
    det_pid = int(mcp_det_pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(lambda: ps.probe_process(det_pid) is None, timeout_s=2.0)

    act_rec = coord.ledger.get_action("act-mcp-stdio-hang")
    assert act_rec is not None
    assert act_rec.status == "uncertain"
    holds = studio.get_containment_store().list_holds(session_id="sess-mcp-stdio")
    assert any(h.action_id == "act-mcp-stdio-hang" for h in holds)


# ---------------------------------------------------------------------------
# AC 1: Stale, cancelled, foreign-boot, and incident-fenced origins cannot admit
# ---------------------------------------------------------------------------


def test_ac1_stale_and_cancelled_and_incident_fenced_origins_cannot_admit_new_actions(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: Stale workers (both task-bound and taskless), foreign-boot workers,
    fenced subprocess `M5_SUPERVISION_ID` scopes (including inbound `mcp_bridge__`),
    and security-incident-quarantined sessions are denied across ALL action classes."""
    (isolated_env / "readme.txt").write_text("clean\n", encoding="utf-8")

    # 1. Lease takeover fences `worker-stale-1` in BOTH SQLite and ContainmentStore:
    #    even if the stale worker omits `task_id` (`task_id=None`) or tries `read_only`
    #    `read_file`, admission is denied!
    coord = studio.get_worker_lease_coordinator(isolated_env)
    c1 = coord.acquire_or_takeover(
        "task-ac1-ps",
        "worker-stale-1",
        session_id="sess-ac1-ps",
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    c2 = coord.acquire_or_takeover(
        "task-ac1-ps",
        "worker-fresh-2",
        session_id="sess-ac1-ps",
        workspace_dir=str(isolated_env),
        policy="autonomous",
        force_takeover=True,
    )
    assert c2.epoch_record.epoch == c1.epoch_record.epoch + 1

    for tool_name, tool_args in (
        ("read_file", {"path": "readme.txt"}),
        ("write_file", {"path": "nope.txt", "content": "x"}),
        ("run_bash", {"command": "echo nope"}),
    ):
        # Task-bound stale epoch + stale worker:
        res_bound = asyncio.run(
            studio._execute_harness_tool(
                None,
                _make_session(
                    isolated_env,
                    sid="sess-ac1-ps",
                    task_id="task-ac1-ps",
                    epoch=c1.epoch_record.epoch,
                    worker_id="worker-stale-1",
                ),
                tool_name,
                tool_args,
                grant_source="policy",
            )
        )
        assert res_bound["ok"] is False

        # Taskless call from the fenced stale worker (`task_id=None`):
        res_taskless = asyncio.run(
            studio._execute_harness_tool(
                None,
                _make_session(
                    isolated_env,
                    sid="sess-ac1-ps",
                    task_id=None,
                    worker_id="worker-stale-1",
                ),
                tool_name,
                tool_args,
                grant_source="policy",
            )
        )
        assert res_taskless["ok"] is False
        assert res_taskless.get("error_code") == "stale_origin_fenced"

    # 2. Foreign-boot worker (`wrk-<old_boot>-ghost`) cannot admit any action even without prior DB state:
    foreign_worker = "wrk-deadbeef00-ghost"
    res_foreign = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-foreign", worker_id=foreign_worker),
            "read_file",
            {"path": "readme.txt"},
            grant_source="policy",
        )
    )
    assert res_foreign["ok"] is False
    assert res_foreign.get("error_code") == "stale_origin_fenced"

    # 3. Fenced subprocess supervision scope (`M5_SUPERVISION_ID`) cannot admit inbound
    #    `mcp_bridge__` or harness actions even from a detached child process env!
    fenced_sup = "sup-abcdef0123456789"
    studio.get_containment_store().fence_origin(
        reason="parent_cancelled",
        session_id="sess-child-cb",
        supervision_id=fenced_sup,
    )
    monkeypatch.setenv(ps.SUPERVISION_ENV_VAR, fenced_sup)
    try:
        bridge_res = asyncio.run(
            studio._run_authorized_tool(
                "mcp_bridge__oc_search",
                {"query": "architecture"},
                "autonomous",
                approved=True,
                session_id="sess-child-cb",
                executor=lambda: studio._dispatch_mcp_stdio_tool("oc_search", {"query": "architecture"}, "Agent"),
                grant_source="operator_one_call",
                workspace_root=isolated_env,
            )
        )
        assert bridge_res["ok"] is False
        assert bridge_res.get("error_code") == "stale_origin_fenced"
    finally:
        monkeypatch.delenv(ps.SUPERVISION_ENV_VAR, raising=False)

    # 4. Security-incident containment (`POST /api/harness/sessions/{sid}/contain`)
    #    quarantines the incident session across ALL action classes while unrelated sessions proceed!
    inc_sess = studio.create_harness_session_db(
        title="Incident Session",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="demo",
        critic_model="demo",
    )
    inc_sid = inc_sess["id"]
    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        headers = {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}
        c_resp = client.post(
            f"/api/harness/sessions/{inc_sid}/contain",
            headers=headers,
            json={"reason": "exfiltration_attempt_detected", "quarantine_session": True},
        )
        assert c_resp.status_code == 200
        c_data = c_resp.json()
        assert c_data["status"] == "contained"
        assert c_data["quarantine_session"] is True
        assert c_data["containment"]["quarantined"] is True

    # Layer 1: full spine call on contained session is denied (`aborted` task state).
    quarantined_read = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid=inc_sid),
            "read_file",
            {"path": "readme.txt"},
            grant_source="policy",
        )
    )
    assert quarantined_read["ok"] is False

    # Layer 2: `UnifiedExecutionCoordinator.admit_profile` independently enforces
    # `ContainmentStore` session quarantine (`stale_origin_fenced`) even without task state!
    prof_coord = studio._execution_profile_controller().bind(
        workspace_root=isolated_env,
        containment_store=studio.get_containment_store(),
    )
    prof_out = prof_coord.admit_profile(
        spine.ActionRequest("read_file", {"path": "readme.txt"}, "autonomous", False, inc_sid),
        effective_policy="autonomous",
        action_class="read_only",
    )
    assert prof_out.allowed is False
    assert prof_out.error_code == "stale_origin_fenced"

    # Unrelated clean session on the same workspace can still perform non-conflicting reads!
    clean_read = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-unrelated-clean"),
            "read_file",
            {"path": "readme.txt"},
            grant_source="policy",
        )
    )
    assert clean_read["ok"] is True


# ---------------------------------------------------------------------------
# AC 2: Unresolved side effects block conflicting resumption (non-conflicting ok)
# ---------------------------------------------------------------------------


def test_ac2_in_flight_cancel_contains_subprocess_tree_and_blocks_conflicting_resumption(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    """AC 2: Cancelling/aborting a task mid-subprocess terminates both in-group and
    `os.setsid()` detached children, records `uncertain` + persisted workspace barrier,
    and blocks resumption on BOTH the same task AND any second task sharing that
    workspace, while a task on a disjoint workspace resumes and runs unimpeded."""
    ws_disjoint = tmp_path / "workspace_disjoint"
    ws_disjoint.mkdir(parents=True, exist_ok=True)
    (isolated_env / "app_state.txt").write_text("v1\n", encoding="utf-8")
    (ws_disjoint / "other.txt").write_text("clean\n", encoding="utf-8")

    sess1 = studio.create_harness_session_db(
        title="Cancel Mid Subprocess",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="demo",
        critic_model="demo",
    )
    sid1 = sess1["id"]
    sm = studio.get_task_state_machine()
    sm.ensure_root_task(sid1, session_id=sid1, workspace_dir=str(isolated_env), policy="autonomous")
    coord1 = studio.get_worker_lease_coordinator(isolated_env)
    claim1 = coord1.acquire_or_takeover(
        sid1,
        "worker-live-1",
        session_id=sid1,
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    sm.checkpoints.save_checkpoint(
        sid1,
        step=0,
        epoch=claim1.epoch_record.epoch,
        payload={"state": "running"},
    )

    det_pid_file = isolated_env / "cancel_detached.pid"
    long_worker_py = isolated_env / "long_worker.py"
    long_worker_py.write_text(
        textwrap.dedent(
            """
            import os, pathlib, subprocess, sys, time
            det = (
                "import os, pathlib, time; os.setsid(); "
                "pathlib.Path('cancel_detached.pid').write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            )
            subprocess.Popen(
                [sys.executable, "-c", det],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    long_cmd = "python3 long_worker.py"

    async def _run_and_abort_concurrently() -> dict[str, Any]:
        tool_task = asyncio.create_task(
            studio._execute_harness_tool(
                None,
                _make_session(
                    isolated_env,
                    sid=sid1,
                    task_id=sid1,
                    epoch=claim1.epoch_record.epoch,
                    worker_id="worker-live-1",
                ),
                "run_bash",
                {"command": long_cmd, "timeout_sec": 30},
                grant_source="policy",
                action_id="act-inflight-bash-1",
            )
        )
        while not det_pid_file.exists():
            await asyncio.sleep(0.02)
        # Operator aborts session mid-flight!
        await studio.api_abort_harness(sid1)
        return await tool_task

    tool_res = asyncio.run(_run_and_abort_concurrently())
    det_pid = int(det_pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(lambda: ps.probe_process(det_pid) is None, timeout_s=2.0)

    # Tool result MUST be marked `uncertain` (never falsely `ok` or deterministic `failed`)!
    assert tool_res["ok"] is False
    assert tool_res.get("uncertain") is True
    act_rec = coord1.ledger.get_action("act-inflight-bash-1")
    assert act_rec is not None
    assert act_rec.status == "uncertain"

    # 1. Resuming `sid1` is rejected by BOTH `WorkerLeaseCoordinator` AND `prepare_resume`:
    with pytest.raises(sel.UnresolvedEffectsHoldError):
        coord1.acquire_or_takeover(
            sid1,
            "worker-replacement-2",
            session_id=sid1,
            workspace_dir=str(isolated_env),
            policy="autonomous",
        )
    with pytest.raises(wc.ResumeRejected):
        sm.prepare_resume(sid1, consume_recovery_budget=False)

    # 2. A DIFFERENT task (`task-conflicting-ws`) targeting the SAME workspace `isolated_env`
    #    is ALSO blocked from conflicting resumption and conflicting mutations!
    sm.ensure_root_task(
        "task-conflicting-ws",
        session_id="sess-conflicting-ws",
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    sm.transition_task("task-conflicting-ws", "paused", allow_inactive_epoch=True, reason="staged")
    sm.checkpoints.save_checkpoint(
        "task-conflicting-ws",
        step=0,
        epoch=0,
        payload={"state": "paused"},
    )
    with pytest.raises(sel.UnresolvedEffectsHoldError) as cross_hold_exc:
        coord1.acquire_or_takeover(
            "task-conflicting-ws",
            "worker-cross-1",
            session_id="sess-conflicting-ws",
            workspace_dir=str(isolated_env),
            policy="autonomous",
        )
    assert "act-inflight-bash-1" in cross_hold_exc.value.unresolved_action_ids
    with pytest.raises(wc.ResumeRejected):
        sm.prepare_resume("task-conflicting-ws", consume_recovery_budget=False)

    conflicting_write = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-conflicting-ws"),
            "write_file",
            {"path": "app_state.txt", "content": "corrupt-overwrite\n"},
            grant_source="policy",
        )
    )
    assert conflicting_write["ok"] is False
    assert conflicting_write.get("error_code") == "unresolved_containment_barrier"
    assert (isolated_env / "app_state.txt").read_text(encoding="utf-8") == "v1\n"

    # 3. Meanwhile, a non-conflicting task on `ws_disjoint` acquires its lease, mutates
    #    files, and prepares resume without interference!
    coord_disjoint = studio.get_worker_lease_coordinator(ws_disjoint)
    disjoint_claim = coord_disjoint.acquire_or_takeover(
        "task-disjoint-ws",
        "worker-disjoint-1",
        session_id="sess-disjoint-ws",
        workspace_dir=str(ws_disjoint),
        policy="autonomous",
    )
    assert disjoint_claim.task.state == "running"
    disjoint_write = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(
                ws_disjoint,
                sid="sess-disjoint-ws",
                task_id="task-disjoint-ws",
                epoch=disjoint_claim.epoch_record.epoch,
                worker_id="worker-disjoint-1",
            ),
            "write_file",
            {"path": "other.txt", "content": "disjoint-updated\n"},
            grant_source="policy",
        )
    )
    assert disjoint_write["ok"] is True
    assert (ws_disjoint / "other.txt").read_text(encoding="utf-8") == "disjoint-updated\n"

    # 4. Once operator reconciles `act-inflight-bash-1`, the persisted barrier hold clears
    #    automatically and both `sid1` and `task-conflicting-ws` can resume cleanly!
    coord1.ledger.resolve_uncertain_action(
        "act-inflight-bash-1",
        resolution="verified_not_applied",
        reason="operator_inspected_workspace_v1_intact",
    )
    assert studio.get_containment_store().list_holds(session_id=sid1) == []
    resumed_claim = coord1.acquire_or_takeover(
        sid1,
        "worker-resumed-3",
        session_id=sid1,
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    assert resumed_claim.task.state == "running"


# ---------------------------------------------------------------------------
# AC 3 + Task 4: Crash with live detached child & containment survival across restart
# ---------------------------------------------------------------------------


def test_ac3_mid_flight_crash_with_live_detached_child_survives_multiple_restarts(
    isolated_env: Path,
) -> None:
    """AC 3 + Task 4: Simulate a mid-flight Studio crash leaving a real OS leader +
    `os.setsid()` detached child alive on disk in `containment/state.json` and an
    `in_flight` row in `runtime_actions`. Prove `simulate_process_restart()` kills
    both processes, fences the crashed worker/epoch, persists the containment barrier
    across a SECOND restart, exposes containment via `GET /api/harness/sessions/{sid}/task`,
    and blocks conflicting execution until reconciled."""
    sess = studio.create_harness_session_db(
        title="Crash Recovery Session",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="autonomous",
        lead_model="demo",
        critic_model="demo",
    )
    sid = sess["id"]
    boot_before_crash = studio._PROCESS_BOOT_ID
    crashed_worker = f"wrk-{boot_before_crash}-crashed1"

    coord = studio.get_worker_lease_coordinator(isolated_env)
    claim = coord.acquire_or_takeover(
        sid,
        crashed_worker,
        session_id=sid,
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    begin_out = coord.ledger.begin(
        action_id="act-crash-bash",
        tool_name="run_bash",
        args={"command": "python3 daemon_mutator.py"},
        policy="autonomous",
        action_class="consequential",
        allowed=True,
        explicit_approval=False,
        session_id=sid,
        task_id=sid,
        epoch=claim.epoch_record.epoch,
        worker_id=crashed_worker,
    )
    assert begin_out.proceed is True and begin_out.status == "in_flight"

    # Spawn a real OS leader + `os.setsid()` detached child registered in durable `in_flight_processes`,
    # then drop in-memory references WITHOUT running `finally:` cleanup (hard crash simulation!).
    det_pid_file = isolated_env / "crash_detached.pid"
    crash_script = textwrap.dedent(
        f"""
        import os, pathlib, signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        det_code = (
            "import os, pathlib, signal, time; os.setsid(); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(det_pid_file)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
        subprocess.Popen(
            [sys.executable, "-c", det_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        time.sleep(30)
        """
    )
    sup = studio.get_process_supervisor()
    sup_id = sup.new_supervision_id()
    child_env = sup.prepare_spawn_env(
        os.environ,
        supervision_id=sup_id,
        session_id=sid,
        task_id=sid,
        epoch=claim.epoch_record.epoch,
        worker_id=crashed_worker,
        current_boot_id=boot_before_crash,
    )
    raw_proc = subprocess.Popen(
        [sys.executable, "-c", crash_script],
        cwd=str(isolated_env),
        start_new_session=True,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_until(det_pid_file.exists, timeout_s=4.0)
        det_pid = int(det_pid_file.read_text(encoding="utf-8").strip())
        leader_probe = ps.probe_process(raw_proc.pid)
        assert leader_probe is not None
        assert ps.probe_process(det_pid) is not None

        ws_str = str(isolated_env.resolve())
        sup.store.record_process_started(
            ps.SupervisedProcessRecord(
                supervision_id=sup_id,
                pid=raw_proc.pid,
                pgid=raw_proc.pid,
                uid=leader_probe.uid,
                start_fingerprint=leader_probe.start_fingerprint,
                container_name=None,
                tool_name="run_bash",
                session_id=sid,
                task_id=sid,
                epoch=claim.epoch_record.epoch,
                worker_id=crashed_worker,
                boot_id=boot_before_crash,
                action_id="act-crash-bash",
                workspace_root=ws_str,
                barriers=((f"workspace:{ws_str}", "exclusive"),),
                started_at=time.time(),
            )
        )

        # Verify owner-only filesystem permissions on `<HARNESS_STORE_DIR>/containment/state.json`!
        state_file = studio.HARNESS_STORE_DIR / "containment" / "state.json"
        assert state_file.is_file()
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700

        # Restart #1: sweeps `in_flight_processes`, kills leader + detached child, persists barrier!
        boot_after_r1 = studio.simulate_process_restart()
        assert boot_after_r1 != boot_before_crash
        raw_proc.wait(timeout=2.0)
        assert _wait_until(lambda: ps.probe_process(raw_proc.pid) is None, timeout_s=2.0)
        assert _wait_until(lambda: ps.probe_process(det_pid) is None, timeout_s=2.0)
    finally:
        if raw_proc.poll() is None:
            raw_proc.kill()
            raw_proc.wait(timeout=2)

    # Restart #2: prove containment barriers & fenced origins survive subsequent restarts too!
    boot_after_r2 = studio.simulate_process_restart()
    assert boot_after_r2 != boot_after_r1

    store_after = studio.get_containment_store()
    assert store_after.list_in_flight_processes() == []
    holds_after = store_after.list_holds(session_id=sid)
    assert len(holds_after) >= 1
    assert any(h.action_id == "act-crash-bash" for h in holds_after)

    is_fenced_worker, _ = store_after.is_origin_fenced(
        worker_id=crashed_worker,
        current_boot_id=boot_after_r2,
    )
    is_fenced_sup, _ = store_after.is_origin_fenced(supervision_id=sup_id)
    assert is_fenced_worker is True
    assert is_fenced_sup is True

    # Verify HTTP task view exposes persisted containment state and HTTP run returns 409!
    headers = {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}
    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        task_resp = client.get(f"/api/harness/sessions/{sid}/task", headers=headers)
        assert task_resp.status_code == 200
        task_payload = task_resp.json()
        assert task_payload["task"]["state"] == "blocked"
        containment_view = task_payload["containment"]
        assert containment_view["schema_version"] == ps.PS_CONTAINMENT_SCHEMA_VERSION
        assert len(containment_view["persisted_barriers"]) >= 1
        assert len(containment_view["recent_incidents"]) >= 1

        run_409 = client.post(
            "/api/harness/run",
            headers=headers,
            json={"session_id": sid, "prompt": "Attempt blind resume after crash"},
        )
        assert run_409.status_code == 409

    # Conflicting mutation on `isolated_env` stays blocked after both restarts!
    blocked_mut = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-post-restart"),
            "write_file",
            {"path": "after_crash.txt", "content": "nope"},
            grant_source="policy",
        )
    )
    assert blocked_mut["ok"] is False
    assert blocked_mut.get("error_code") == "unresolved_containment_barrier"


def test_containment_store_corruption_and_symlink_fail_closed(
    isolated_env: Path,
    tmp_path: Path,
) -> None:
    """AC 3 security: Corrupted JSON or symlink replacement of `containment/state.json`
    raises `ContainmentIntegrityError` and causes tool admission to fail closed rather
    than silently clearing persisted holds."""
    store = studio.get_containment_store()
    ws_str = str(isolated_env.resolve())
    store.persist_barrier_hold(
        ps.PersistedBarrierHold(
            hold_id="hold-critical-1",
            session_id="sess-corrupt",
            action_id="act-critical-1",
            tool_name="run_bash",
            workspace_root=ws_str,
            barriers=((f"workspace:{ws_str}", "exclusive"),),
            reason="unresolved_mutation",
        )
    )
    state_path = studio.HARNESS_STORE_DIR / "containment" / "state.json"
    assert state_path.is_file()

    # 1. Truncated/corrupted JSON fails closed!
    state_path.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(ps.ContainmentIntegrityError):
        store.list_holds()

    denied_on_corrupt = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-corrupt"),
            "write_file",
            {"path": "blocked.txt", "content": "must-fail-closed"},
            grant_source="policy",
        )
    )
    assert denied_on_corrupt["ok"] is False
    assert not (isolated_env / "blocked.txt").exists()

    # 2. Symlink replacement fails closed!
    outside_target = tmp_path / "forged_state.json"
    outside_target.write_text(
        json.dumps(
            {
                "schema_version": ps.PS_CONTAINMENT_SCHEMA_VERSION,
                "in_flight_processes": {},
                "persisted_barriers": {},
                "fenced_origins": {},
                "incident_history": [],
            }
        ),
        encoding="utf-8",
    )
    state_path.unlink()
    state_path.symlink_to(outside_target)
    with pytest.raises(ps.ContainmentIntegrityError):
        store.list_holds()

    denied_on_symlink = asyncio.run(
        studio._execute_harness_tool(
            None,
            _make_session(isolated_env, sid="sess-corrupt"),
            "read_file",
            {"path": "blocked.txt"},
            grant_source="policy",
        )
    )
    assert denied_on_symlink["ok"] is False


# ---------------------------------------------------------------------------
# Standalone `studio.py` bundle & INLINED_MODULES AST collision check
# ---------------------------------------------------------------------------


def test_standalone_studio_bundle_inlines_process_supervision_without_collisions(
    tmp_path: Path,
) -> None:
    """Verify `process_supervision` is inlined into standalone `studio.py` with zero
    top-level AST symbol collisions across `INLINED_MODULES`, and works when executed
    in an isolated directory (`python -I`) outside the repository checkout."""
    assert "process_supervision" in build_studio.INLINED_MODULES

    def _top_names(path: Path) -> set[str]:
        found: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(node.name)
            elif isinstance(node, ast.Assign):
                found.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
        found.discard("__all__")
        return found

    mine = _top_names(REPO_ROOT / "process_supervision.py")
    others: dict[str, str] = {}
    for mod_name in build_studio.INLINED_MODULES:
        if mod_name == "process_supervision":
            continue
        for name in _top_names(REPO_ROOT / f"{mod_name}.py"):
            others.setdefault(name, mod_name)
    for name in _top_names(REPO_ROOT / "app.py"):
        others.setdefault(name, "app")
    collisions = {name: others[name] for name in mine if name in others}
    assert collisions == {}, f"process_supervision top-level symbol collisions: {collisions}"

    isolated_dir = tmp_path / "standalone_dir"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "studio.py", isolated_dir / "studio.py")

    smoke_code = textwrap.dedent(
        """
        import asyncio, pathlib, sys
        sys.path.insert(0, str(pathlib.Path.cwd()))
        import studio
        ws = pathlib.Path.cwd() / "ws"
        ws.mkdir(exist_ok=True)
        studio.DB_PATH = pathlib.Path.cwd() / "standalone.db"
        studio.HARNESS_STORE_DIR = pathlib.Path.cwd() / "store"
        studio.WORKSPACE_ROOT = ws
        studio.STUDIO_EXECUTION_MODE = "host"
        studio.STUDIO_ALLOW_HOST_EXECUTION = True
        studio.init_db()

        store = studio.get_containment_store()
        store.fence_origin(reason="standalone_fence", worker_id="worker-standalone-stale")
        res = asyncio.run(
            studio._execute_harness_tool(
                None,
                {
                    "id": "s-standalone",
                    "workspace_dir": str(ws),
                    "worktree_dir": str(ws),
                    "policy": "autonomous",
                    "worker_id": "worker-standalone-stale",
                },
                "write_file",
                {"path": "blocked.txt", "content": "nope"},
                grant_source="policy",
            )
        )
        assert res["ok"] is False and res.get("error_code") == "stale_origin_fenced", res
        print("STANDALONE_SUPERVISION_OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", smoke_code],
        cwd=str(isolated_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "STANDALONE_SUPERVISION_OK" in proc.stdout


def test_cancel_endpoint_and_skill_script_supervision_contain_detached_children(
    isolated_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `api_cancel_harness_session` and `_execute_skill_script_inner`
    both supervise and terminate `os.setsid()` detached child trees."""
    # 1. Skill script timeout kills `os.setsid()` child and marks `uncertain=True`:
    skills_dir = isolated_env / "skills_root"
    skill_dir = skills_dir / "demo-skill" / "scripts"
    skill_dir.mkdir(parents=True, exist_ok=True)
    ((skills_dir / "demo-skill") / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo\n---\n# Demo\n",
        encoding="utf-8",
    )
    skill_det_pid_file = isolated_env / "skill_det.pid"
    script_path = skill_dir / "hang.py"
    script_path.write_text(
        textwrap.dedent(
            f"""
            import os, pathlib, subprocess, sys, time
            det = (
                "import os, pathlib, time; os.setsid(); "
                f"pathlib.Path({str(skill_det_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            )
            subprocess.Popen(
                [sys.executable, "-c", det],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    skill_res = asyncio.run(
        studio._run_authorized_tool(
            "skill_execute_script",
            {"script": str(script_path), "args": [], "cwd": str(isolated_env)},
            "autonomous",
            approved=True,
            session_id="sess-skill-timeout",
            executor=lambda: studio._execute_skill_script_inner(
                script_path,
                [],
                cwd=isolated_env,
                timeout=0.35,
            ),
            grant_source="skill_opt_in",
            workspace_root=isolated_env,
        )
    )
    assert skill_res["ok"] is False
    assert skill_res["uncertain"] is True
    assert skill_det_pid_file.exists()
    skill_det_pid = int(skill_det_pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(lambda: ps.probe_process(skill_det_pid) is None, timeout_s=2.0)

    # 2. `POST /api/harness/sessions/{sid}/cancel` mid-flight contains live subprocess + detached child:
    ws_cancel = tmp_path / "ws_cancel"
    ws_cancel.mkdir(parents=True, exist_ok=True)
    sess = studio.create_harness_session_db(
        title="Cancel Endpoint Session",
        workspace_dir=str(ws_cancel),
        use_worktree=False,
        policy="autonomous",
        lead_model="demo",
        critic_model="demo",
    )
    sid = sess["id"]
    coord = studio.get_worker_lease_coordinator(ws_cancel)
    claim = coord.acquire_or_takeover(
        sid,
        "worker-cancel-ep",
        session_id=sid,
        workspace_dir=str(ws_cancel),
        policy="autonomous",
    )
    cancel_det_pid_file = ws_cancel / "cancel_ep_det.pid"
    (ws_cancel / "hang_cancel.py").write_text(
        textwrap.dedent(
            """
            import os, pathlib, subprocess, sys, time
            det = (
                "import os, pathlib, time; os.setsid(); "
                "pathlib.Path('cancel_ep_det.pid').write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            )
            subprocess.Popen(
                [sys.executable, "-c", det],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    class _FakeCancelReq:
        async def json(self) -> dict[str, Any]:
            return {"reason": "user_clicked_cancel"}

    async def _run_and_cancel() -> dict[str, Any]:
        t = asyncio.create_task(
            studio._execute_harness_tool(
                None,
                _make_session(
                    ws_cancel,
                    sid=sid,
                    task_id=sid,
                    epoch=claim.epoch_record.epoch,
                    worker_id="worker-cancel-ep",
                ),
                "run_bash",
                {"command": "python3 hang_cancel.py", "timeout_sec": 30},
                grant_source="policy",
                action_id="act-cancel-ep-1",
            )
        )
        while not cancel_det_pid_file.exists():
            await asyncio.sleep(0.02)
        await studio.api_cancel_harness_session(sid, _FakeCancelReq())  # type: ignore[arg-type]
        return await t

    cancel_tool_res = asyncio.run(_run_and_cancel())
    assert cancel_tool_res["ok"] is False
    assert cancel_tool_res.get("uncertain") is True
    cancel_det_pid = int(cancel_det_pid_file.read_text(encoding="utf-8").strip())
    assert _wait_until(lambda: ps.probe_process(cancel_det_pid) is None, timeout_s=2.0)
    assert studio.get_task_state_machine().store.get_task(sid).state == "cancelled"  # type: ignore[union-attr]
    assert any(
        h.action_id == "act-cancel-ep-1"
        for h in studio.get_containment_store().list_holds(session_id=sid)
    )

