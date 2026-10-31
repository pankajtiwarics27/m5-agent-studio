---
id: ADR-0013
title: process-supervision-and-in-flight-containment
status: accepted
date: 2026-10-31
review_status: reviewed
reviewed_by: arena/ff251d72-m5-agent-studio
reviewed_on: 2026-10-31
---

# ADR-0013: Process supervision, detached-tree termination, and durable containment barriers

## Context

`M5-P3-03` requires Studio to contain in-flight execution after operator cancellation,
worker lease ownership loss, mid-flight crash, or a security incident:

1. **Untracked subprocess identities and detached descendants:** Subprocess surfaces
   (`_start_code_process`, `_execute_limited`, `_execute_skill_script_inner`, and
   `_mcp_stdio_rpc`) spawned sessions via `start_new_session=True`, but no registry bound
   running OS `(pid, pgid, uid, start_fingerprint, container_name)` identities to the
   active `(session_id, task_id, epoch, worker_id, action_id)` admission scope. A child
   process calling `os.setsid()` escaped `os.killpg(proc.pid, ...)` completely, and a hard
   crash left orphaned OS processes running across restarts without PID-recycling checks.
2. **Stale and incident-fenced origins:** `WorkerLeaseCoordinator` fenced superseded
   `(task_id, epoch)` rows in SQLite (`ADR-0003`, `M5-P1-04`), and `DurableActionBoundary`
   checked task state (`ADR-0002`), yet taskless calls (`task_id=None`), inbound
   `mcp_bridge__*` tool callbacks carrying a cancelled child environment, foreign-boot
   workers, or security-incident-quarantined sessions needed an immediate origin fence
   across all action classes (`read_only`, `managed_edit`, `consequential`, `external`).
3. **Unresolved side effects across conflicting tasks and restarts:** `DurableActionLedger`
   held a single task (`task_id`) when one of its `runtime_actions` rows became
   `uncertain`, and `UnifiedExecutionController` held in-memory footprint barriers only
   while a Python coroutine stayed inside `admit_and_execute`. Once an interrupted or
   crashed subprocess released its in-memory coroutine frame (or Studio restarted), a
   second task sharing the same workspace could acquire a lease or mutate overlapping
   paths before operator reconciliation, and an unkillable (`D`-state) OS process could
   still be writing into the workspace.

## Decision

Introduce `process_supervision.py` (stdlib-only, inlined into standalone `studio.py`) and
integrate it across `execution_spine.py`, `side_effect_ledger.py`,
`execution_profiles.py`, `workflow_state.py`, and `app.py`:

1. **Subprocess identity and tree discovery (`ProcessProbe`, `SupervisedProcessRecord`):**
   Every subprocess spawned by `_start_code_process`, `_execute_skill_script_inner`, and
   `_mcp_stdio_rpc` receives a unique `M5_SUPERVISION_ID` token (`ContextVar` plus minimal
   spawn environment) and is durably recorded in `ContainmentStore` (`in_flight_processes`)
   with `(pid, pgid, uid, start_fingerprint, container_name, session_id, task_id, epoch,
   worker_id, boot_id, action_id, barriers)` before awaiting I/O. `probe_process` reads
   `/proc/{pid}/stat` on Linux (falling back to `ps` on macOS/POSIX), treats zombie/dead
   states (`Z`, `X`, `x`) and `pid <= 1` as non-live, and binds every signal check to
   `(pid, start_fingerprint)` so recycled PIDs and Studio's own PID/PGID are never
   signaled. `discover_process_tree` walks both matching `pgid` members, recursive `ppid`
   descendants, and `os.setsid()` detached processes carrying `M5_SUPERVISION_ID` under
   the same UID.
2. **Bounded 3-stage containment (`ProcessSupervisor`):**
   Both synchronous (`terminate_record_sync`) and event-loop-friendly
   (`terminate_record_async`) paths execute:
   - Stage 1: cooperative `SIGTERM` to `-pgid` and discovered detached PIDs (plus
     `docker/podman stop -t 1 {container_name}` when containerized) with bounded grace;
   - Stage 2: forced `SIGKILL` to remaining verified `(pid, start_fingerprint)` targets
     and fresh detached descendants (plus `docker/podman rm -f {container_name}`);
   - Stage 3: post-kill verification recording any unkillable `lingering_pids`. Even on
     normal leader exit (`returncode == 0`), `finalize_process_async` sweeps any surviving
     `os.setsid()` background descendants before releasing the workspace barrier.
3. **Durable containment store (`$HARNESS_STORE_DIR/containment/state.json`):**
   `ContainmentStore` persists `schema_version = 1` JSON atomically (`O_NOFOLLOW` symlink
   refusal, `0700` directory, `0600` file, `fsync` file + parent directory) containing
   `in_flight_processes`, `persisted_barriers`, `fenced_origins`, and bounded
   `incident_history`. Corrupted or symlinked state files raise
   `ContainmentIntegrityError` so admission fails closed.
4. **Origin fencing and conflicting resumption barriers:**
   - `UnifiedExecutionCoordinator.admit_profile` checks `ContainmentStore.is_origin_fenced`
     for `(session_id, task_id, epoch, worker_id, supervision_id, current_boot_id)` across
     all action classes (`stale_origin_fenced`), deferring SQLite task-lease TOCTOU checks
     to `DurableActionLedger.begin` when `sql_task_fencing=True`.
   - `PersistedBarrierHold` records conflict via `barriers_conflict` against overlapping
     `workspace:{ws}` / `path:{ws}:{rel}` targets: while `lingering_pids` remain alive in
     the OS, **all** overlapping actions (including `read_only` reads, rollbacks, and hold
     release) are denied (`lingering_process_containment`); once no lingering OS process
     remains alive, `read_only` inspection and acknowledged `rollback_edits` are permitted
     for operator diagnosis while conflicting mutations (`unresolved_containment_barrier`)
     and conflicting task/workspace resumptions (`WorkerLeaseCoordinator.resumption_blocker`
     and `DurableTaskStateMachine.prepare_resume`) stay blocked until explicit
     `resolve_uncertain_action` clears the hold.
   - `POST /api/harness/sessions/{session_id}/contain` provides an explicit operator/runtime
     incident containment endpoint that terminates live session subprocesses, fences active
     epochs, records an auditable `security/containment` incident, and quarantines the
     session when requested.

## Alternatives

1. **Rely solely on `os.killpg(proc.pid, signal.SIGKILL)` in coroutine `finally` blocks:**
   Rejected because `os.setsid()` double-forked children escape the parent's process group,
   hard Studio crashes skip `finally` blocks entirely, and coroutine cancellation releases
   in-memory admission barriers even when side effects are unresolved or `D`-state children
   remain alive.
2. **Store live PID tables inside SQLite `studio_history.db`:**
   Rejected because `runtime_persistence` forbids long-held write transactions and requires
   containment state under `$HARNESS_STORE_DIR` to remain readable/writable independently
   of SQLite lock contention during emergency signal/shutdown containment, matching the
   existing `$HARNESS_STORE_DIR` snapshot, edit-journal, and footprint store split
   (`ADR-0006`, `ADR-0007`, `ADR-0012`).
3. **Quarantine the entire workspace permanently on every single-file `managed_edit`
   crash before effect:**
   Rejected because synchronous in-process `write_file`/`edit_file` operations never spawn
   OS subprocesses and already reconcile deterministically via `DurableActionLedger` and
   `reconcile_startup_in_flight_edits`; scoping persistent `ContainmentStore` barriers to
   subprocess/external surfaces and policy/scope violations avoids false cross-session
   deadlocks while blocking every genuinely ambiguous or contaminated workspace scope.

## Tradeoffs

- User-space supervision on `host` mode relies on PGID, `ppid` ancestry, and the inherited
  `M5_SUPERVISION_ID` environment marker. In `STUDIO_EXECUTION_MODE=host`, a deliberately
  adversarial script that scrubs its own environment and double-forks before tree
  enumeration cannot be kernel-guaranteed without OS cgroups or PID namespaces; complete
  kernel containment continues to require a qualified container runtime (`M5-P3-01`).
- `ContainmentStore` serializes updates through an in-process `RLock` and atomic file
  replacement under `$HARNESS_STORE_DIR/containment/state.json`, matching Studio's
  single-process architecture (`ADR-0003`–`ADR-0012`) rather than multi-node distributed
  consensus.
- Persisting `workspace:{ws}` `exclusive` holds after an interrupted `run_bash` or crashed
  subprocess intentionally blocks **other** sessions targeting the same workspace directory
  until an operator inspects and resolves the uncertain action (`reconcile_task_actions` or
  acknowledged `rollback_edits`), trading convenience on shared directories for strict
  workspace integrity.

## Review Status

Reviewed on 2026-10-31 by `arena/ff251d72-m5-agent-studio` (a session other than the
implementing one), as part of the PR #60 review. The reviewer re-ran the verification
sequence on the PR head (1127 passed, ruff, mypy, docs/ADR/governance validators, a
reproducible bundle rebuild, `smoke_local.py --standalone`) and probed the three
acceptance criteria independently of the shipped tests: an origin fence written by one
`ContainmentStore` is honoured by a fresh instance on the same directory and across a
boot-id change; persisted holds block the owning task and other tasks sharing the
workspace but not other workspaces; and corrupt or symlinked containment state fails
closed. No blocking findings. Two non-blocking documentation corrections were raised
(a stale test count and a misnamed next story in the ledger handoff note) and applied
after merge.
