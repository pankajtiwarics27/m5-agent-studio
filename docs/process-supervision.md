# Process Supervision and In-Flight Containment (`M5-P3-03`, ADR-0013)

`process_supervision.py` (stdlib-only, inlined into standalone `studio.py`) contains
in-flight subprocess trees and enforces durable origin/barrier containment after
cancellation, worker lease ownership loss, mid-flight crash, or a security incident.

## Subprocess Identity & Detached-Tree Discovery

Every subprocess launched across Studio's execution surfaces — `_start_code_process` /
`_execute_limited` (`run_bash`, `ctx_execute_python`, `ctx_run_block`),
`_execute_skill_script_inner` (`skill_execute_script`), and `_mcp_stdio_rpc` (stdio MCP
servers) — is registered in `ProcessSupervisor` and persisted to
`$HARNESS_STORE_DIR/containment/state.json` before awaiting I/O:

- **Identity fields (`SupervisedProcessRecord`):** `supervision_id` (`sup-...`), `pid`,
  `pgid`, `uid`, `start_fingerprint` (OS process start ticks from `/proc/{pid}/stat` field
  22 on Linux, or `ps -o lstart=` on macOS/POSIX), optional `container_name`, and the
  owning `(session_id, task_id, epoch, worker_id, boot_id, action_id, workspace_root,
  barriers)` scope.
- **PID-recycling & system-process safety (`probe_process`, `is_same_live_process`):**
  `probe_process` rejects `pid <= 1` and treats zombie/dead states (`Z`, `X`, `x`) as
  non-live. Signals are sent only when `(pid, start_fingerprint)` matches and neither `pid`
  nor `pgid` equals Studio's own process or process group.
- **Detached child discovery (`discover_process_tree`):** Spawn environments carry
  `M5_SUPERVISION_ID` via `prepare_spawn_env` / `_minimal_execution_env` (host processes
  only; `app._sandbox_command` container flags remain unchanged). Tree discovery collects
  both in-group processes (`pgid == record.pgid` and recursive `ppid` children) and
  `os.setsid()` double-forked detached daemons whose environment still carries the
  matching `M5_SUPERVISION_ID` under the same UID.

## Bounded 3-Stage Termination & Normal-Exit Sweep

`ProcessSupervisor.terminate_record_async` (non-blocking `asyncio.sleep` polling that
never steals `waitpid` return codes from `asyncio`) and `terminate_record_sync` (used on
startup recovery and synchronous lease fencing) execute three bounded stages:

1. **Stage 1 — Cooperative `SIGTERM`:** Sends `SIGTERM` to `-pgid` and any discovered
   detached PIDs (plus `docker/podman stop -t 1 {container_name}` when containerized) and
   waits up to `grace_timeout_s`.
2. **Stage 2 — Escalated `SIGKILL`:** Re-discovers the tree for late-spawned children,
   sends `SIGKILL` to `-pgid` and verified `(pid, start_fingerprint)` survivors (plus
   `docker/podman rm -f {container_name}`), and polls up to `kill_timeout_s`.
3. **Stage 3 — Lingering verification:** Any `(pid, start_fingerprint)` still alive in the
   OS (for example an uninterruptible `D`-state kernel wait) is recorded in
   `PersistedBarrierHold.lingering_pids` so overlapping workspace barriers fail closed
   even for read-only operations until the OS process actually exits.

Even when a leader subprocess exits cleanly (`returncode == 0`), `finalize_process_async`
scans for surviving `os.setsid()` background descendants and terminates them before
releasing the admission barrier.

## Origin Fencing (`AC 1`)

`ContainmentStore.fence_origin` and `is_origin_fenced` block new admissions across all
action classes (`read_only`, `managed_edit`, `consequential`, `external`) inside
`UnifiedExecutionCoordinator.admit_profile` (`error_code = "stale_origin_fenced"`):

- Superseded `(task_id, epoch)` and `worker_id` scopes fenced on lease takeover
  (`WorkerLeaseCoordinator.on_epochs_fenced`);
- Foreign-boot worker IDs (`wrk-{old_boot}-*` != `wrk-{current_boot}-*`);
- Cancelled or timed-out subprocess `M5_SUPERVISION_ID` scopes (including inbound
  `mcp_bridge__*` callbacks originating from a fenced child environment);
- Quarantined sessions from operator/runtime incident containment
  (`POST /api/harness/sessions/{session_id}/contain` with `quarantine_session=true`).

## Persisted Containment Barriers & Resumption Holds (`AC 2` & `AC 3`)

`$HARNESS_STORE_DIR/containment/state.json` (`0700` directory, `0600` file, `O_NOFOLLOW`,
atomic temp-file + `fsync` replacement) persists `in_flight_processes`,
`persisted_barriers`, `fenced_origins`, and `incident_history` across crashes and restarts:

- **Startup sweep (`init_db` -> `recover_on_startup`):** Sweeps any `in_flight_processes`
  left from a crashed Studio boot, terminates live orphan trees whose
  `(pid, start_fingerprint)` still matches, fences prior workers/supervision IDs, and
  persists `PersistedBarrierHold` entries alongside `DurableActionLedger` missing-result
  reconciliation.
- **Conflicting action & resumption blocking:**
  - While `lingering_pids` has any live OS process, **every** overlapping action (including
    `read_file`, `rollback_edits`, and `release_holds_for_action`) is rejected with
    `lingering_process_containment` / `ContainmentHoldActiveError`.
  - Once no live lingering OS process remains, `read_only` inspection and acknowledged
    `rollback_edits(dry_run=False, acknowledge_unresolved=True)` remain available for
    diagnosis and recovery, while conflicting mutations (`unresolved_containment_barrier`)
    and conflicting task/workspace resumptions (`WorkerLeaseCoordinator` and
    `DurableTaskStateMachine.prepare_resume`, even across a **different** session sharing
    the affected workspace) are blocked until `DurableActionLedger.resolve_uncertain_action`
    releases the hold. Non-conflicting workspaces proceed without interference.
- **Incident endpoint & task inspection:**
  - `POST /api/harness/sessions/{session_id}/contain` terminates live subprocesses for the
    session, aborts/fences active epochs, records a `security/containment` event and
    incident entry, and returns the updated containment summary.
  - `GET /api/harness/sessions/{session_id}/task` includes `"containment"` summarizing
    quarantine state, in-flight processes, persisted barriers, fenced origins, and recent
    incidents.

## Known Unverified Caveats

In `STUDIO_EXECUTION_MODE=host`, tree discovery relies on POSIX process groups, `ppid`
ancestry, and inherited `M5_SUPERVISION_ID` environment markers. A deliberately
adversarial host process that scrubs its environment and double-forks before tree
enumeration cannot be kernel-guaranteed without OS cgroups/PID namespaces; strict kernel
process containment requires a qualified container runtime (`M5-P3-01`).
