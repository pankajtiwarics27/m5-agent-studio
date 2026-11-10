# Bounded mission execution, cancellation and truthful recovery

AP-007 lets a mission plan be stopped, keeps late results from settling work that
was stopped, adds limits that end work at a defined point, and reports what was
kept and what to do next.

This is the **offline, fail-closed subset** of AP-007. Owner decision U06
(lifecycle and sandbox qualification and activation) is still pending, so:

- no route, endpoint, UI control, scheduler loop or background worker calls the
  mission stop, the recovery report or the observation recorder, and nothing in
  the running application declares the new limits;
- no provider, model, network destination, browser, account or runtime is
  activated;
- nothing is executed on a qualified isolation backend, and no installed-runtime
  qualification is claimed;
- the only processes terminated by this work are local fixture subprocesses
  started by the tests themselves.

Three changes are on paths the running application already uses. Each one only
adds a refusal. They are listed under
[Changes on live paths](#changes-on-live-paths).

The design decision is recorded in
[ADR-0041](adr/0041-mission-halt-bounded-stops-and-recovery-reports.md). The
mission plans this builds on are described in
[Durable mission graphs](durable-mission-graphs.md).

## Owners

No new control plane, table, store file or schema version is added. SQLite stays
at schema v4.

| Capability | Owner | What changed |
|---|---|---|
| Mission stop and recovery report | `workflow_state.DurableTaskStateMachine` | `halt_mission`, `halt_mission_async`, `mission_recovery_report`; a gate in `create_mission_plan`; isolation and deadline checks in `claim_ready_mission_task` and `prepare_mission_task_resume` |
| Bounded limits and observations | `execution_budget` | six optional limit fields, their counters, `record_bounded_observation`, `IsolationEvidence` |
| Late-result fence | `side_effect_ledger.DurableActionLedger`, `execution_spine.DurableActionBoundary` | `complete` and `record_exception` leave an `uncertain` action unchanged; the boundary reports `late_result_fenced` |
| Process termination | `process_supervision.ProcessSupervisor` | reused as is; one read-only helper, `still_running` |
| Stop records | `runtime_mission_joins` (existing table) | holds `mission_recovery` reports beside the AP-005 join snapshots |
| HTTP budget parsing | `app._parse_task_budget_spec` | refuses the six new keys with 400 |

`execution_spine.MissionAuthorityEnvelope` is unchanged. A halt needs the typed,
server-created envelope; request JSON and model tool arguments are never parsed
into one.

## Changes on live paths

Most of this work is unreachable until a caller is added. These three changes
are reachable today:

1. **Late-result fence.** Every tool call goes through `DurableActionBoundary`
   and the action ledger. When a result arrives for an action that the ledger
   has already marked `uncertain` (a cancel, a takeover or crash recovery got
   there first), the result no longer settles the action. Before this change
   the late result overwrote the `uncertain` verdict. The caller now receives
   `late_result_fenced`, and the harness pauses as it does for any other
   uncertain result. This applies to every task, not only to mission tasks.
2. **Budget keys over HTTP.** Two routes refuse six key names that they used to
   ignore. See [HTTP boundary](#http-boundary).
3. **Budget charge.** Every tool-call charge now reads each budget from the task
   up to the root. The application creates root tasks and direct child tasks
   today. For those chains, with none of the new limits declared, the outcome
   is unchanged.

## Stopping a mission

```python
report = machine.halt_mission(
    mission_id, plan_version,
    authority=envelope,          # AP-004 MissionAuthorityEnvelope
    kind="cancelled",            # or "bounded_stop"
    reason="operator request",
    supervisor=process_supervisor,
)
```

The order is fixed:

1. **One write transaction** marks the plan `cancelled` (`kind="cancelled"`) or
   `failed` (`kind="bounded_stop"`). For every unfinished task the plan owns
   (its nodes and their delegated descendants at any depth) it:
   - fences the live worker lease (`mission_cancelled` or
     `mission_bounded_stop:<code>`);
   - turns in-flight consequential actions `uncertain` with no success value,
     and in-flight read-only actions `failed`;
   - sets `cancel_requested` in the task budget;
   - moves the task to `cancelled` and appends a transition whose evidence names
     the mission, plan version, stop kind, stop code and unresolved action ids.
2. **Process-layer fences.** The leases fenced in step 1 are fenced in the
   containment store, so a late worker cannot spawn a process.
3. **Termination.** With a `supervisor`, every process registered for those tasks
   is terminated through the existing terminate, kill and reap path.
4. **The report** is stored with the measured stop record.

Completed tasks and their outputs are kept. The root task is not cancelled.

### What a halt rejects

| Condition | Raised |
|---|---|
| `kind` is not `cancelled` or `bounded_stop` | `WorkflowStateError` |
| Unknown mission or plan version | `MissionGraphError` |
| Envelope session or root identity does not match the plan | `MissionBindingRejected` |
| Plan is `superseded` or `completed`, or its join already succeeded | `WorkflowTransitionRejected` |
| `kind="bounded_stop"` and no budget requires a stop | `WorkflowTransitionRejected` |

The envelope is checked for identity only. A stale session or global policy
revision is accepted, so an outdated policy generation cannot prevent a stop.

For `bounded_stop`, the stop code and reason come from the durable budget, never
from the caller. `reason` is recorded as a note.

### Repeating a halt

A second call changes nothing durable, and the first stop kind stands. With a
supervisor, termination runs again when no clean stop is on record. Processes an
earlier attempt could not end are checked again by pid and fingerprint. A retry
that finds no registered process does not count as a clean stop while earlier
survivors are unverified.

A pid that was recorded without a fingerprint counts as still running while any
process holds that pid. This over-reports rather than under-reports.

### Cancelling a whole task that has an active mission

`cancel_task` keeps its existing behaviour: it fences the root and its direct
children only, and it never changes a plan's status. To cancel a whole task that
has an active mission, call both, in this order:

1. `cancel_task(root_task_id)`;
2. `halt_mission(mission_id, plan_version, authority=..., kind="cancelled", supervisor=...)`.

Between the two calls the report reads `cancelled` with the limitation
`halt_required`: the plan's leases are not yet fenced and nothing has been
terminated.

### The stop record

The report's `stop` field carries:

| Key | Meaning |
|---|---|
| `kind`, `code`, `reason`, `requested_at` | what stopped the plan and when |
| `scope` | bounded stops only: `root` or `task:<key>` |
| `stopped_task_keys`, `stopped_descendant_tasks` | what was cancelled |
| `fenced_epochs` | number of worker leases fenced |
| `uncertain_action_ids` | consequential actions that were in flight |
| `owned_work` | the measured termination record, below |

`owned_work` after a supervised stop:

| Key | Meaning |
|---|---|
| `termination_attempted` | `true` only when a supervisor ran |
| `cleanly_stopped` | `true` only with no survivor and no containment error |
| `dispatch_fenced_ms` | stop request to process-layer fences in place |
| `cleanup_latency_ms` | stop request to the last verified exit |
| `slowest_process_ms` | longest single process termination |
| `processes`, `escalated` | processes handled, and how many needed a kill |
| `lingering`, `lingering_pids` | survivors, with up to a fixed number listed |
| `origins_fenced`, `errors`, `attempted_at` | fences written, containment errors, wall-clock time |
| `retry` | present when this attempt was a repeat; it measures from its own request |

Without a supervisor, `owned_work` is `{"termination_attempted": false,
"origins_fenced": 0}` and the report carries `owned_work_not_terminated`.

Durations are measured with a monotonic clock, from the moment the halt was
requested.

### What a supervised stop leaves behind

- **Barrier holds.** A supervised stop leaves the barrier hold of each process it
  ended in the containment store, exactly as a session cancel does. The hold
  blocks conflicting work in that workspace until an operator releases it
  (`ContainmentStore.list_barrier_holds`, `release_hold`). The recovery report
  does not list holds.
- **Process-layer fences** are written only for the leases this stop fenced
  (`epoch:<task>:<n>` and `worker:<id>`). No session-wide fence is written, so a
  later plan in the same session can spawn. The containment store keeps the
  newest 500 origin fences.
- **Worker identifiers** are assumed to be unique per claim. A worker identifier
  reused across claims stays blocked after a stop.

## After a stop

| Late operation | Result |
|---|---|
| Mission complete, checkpoint, fail, claim or resume on the stopped plan | `MissionBindingRejected` |
| Child spawn under a stopped task | `WorkflowTransitionRejected` |
| Budget charge for a stopped task | `ExecutionCancelled` |
| Action ledger `begin` with the fenced lease | denied, `StaleEpochError` |
| Process spawn from the fenced lease | `StaleOriginFencedError` |
| Action ledger `complete` or `record_exception` on an `uncertain` action | row unchanged |
| Action boundary receiving that late result | `ok: false`, `uncertain: true`, `error_code: "late_result_fenced"` |

An `uncertain` action is resolved only by
`DurableActionLedger.resolve_uncertain_action`, with `verified_committed` or
`verified_not_applied`. A late result is not stored on the fenced row.

Startup recovery and resume do not revive a halted plan.

## Planning new work after a stop

`create_mission_plan` raises `MissionHaltUnsettled` while any `cancelled` or
`failed` plan of the same root task has:

- an unresolved consequential action; or
- no attested termination (the stop ran without a supervisor); or
- a termination that was not clean (a survivor or a containment error).

The check is scoped to the root task, so a new mission name does not step around
it. A stop that ran without a supervisor therefore blocks new plans on that root
until a supervised stop runs.

## Bounded limits

Six optional fields in `TaskBudgetSpec`, stored in the existing
`runtime_tasks.budget_json` document:

| Field | Stop code | Stops when |
|---|---|---|
| `max_no_progress` | `no_progress` | consecutive no-progress observations `>` limit |
| `max_repair_attempts` | `repair_attempts` | repair attempts `>` limit |
| `deadline_at` | `deadline` | current time `>=` deadline |
| `max_memory_bytes` | `memory` | recorded peak `>` ceiling |
| `max_storage_bytes` | `storage` | recorded peak `>` ceiling |
| either ceiling, unmeasured | `measurement_unavailable` | a declared ceiling is marked unmeasured |
| `isolation_required` | (see [Isolation](#isolation)) | isolation is not proven |

Counters and peaks stop on strictly greater than the limit. The deadline stops
at the deadline itself. It needs no observation: claims, dispatches and resumes
all compare the clock.

### Stored form and identity

An undeclared limit and a zero counter are not written. A budget document that
declares none of these limits is byte-for-byte what it was before, and its
identity digest is unchanged. Declaring a limit changes the budget identity, so
plan bindings captured earlier become stale, as for any other limit change.
Usage (counters, peaks, markers) does not change the identity.

### Recording observations

```python
verdict = machine.budgets.record_bounded_observation(
    task_id, root_task_id=root, session_id=session, epoch=lease_epoch,
    progressed=False, repair_attempt=False,
    memory_bytes=None, storage_bytes=None, unmeasured=("storage",),
)
```

- One transaction updates the task and its root. The root aggregates every task,
  so repeated repair cannot be hidden by spreading it across tasks or plan
  versions.
- Observations are server-observed facts. They are never read from a request
  body or from model tool arguments.
- A stale, fenced or missing lease cannot record anything. A late worker can
  neither reset the no-progress count nor hide a breach.
- Once a stop is required, the document is not changed again. A later
  "progress" report does not undo it.
- The stop reason is committed before `BoundedStopRequired` is raised.
- A task without a worker lease, such as a mission root, is observed with
  `epoch=None`.

**Measurement gap.** Naming a resource in `unmeasured` while its ceiling is
declared stops the work with `measurement_unavailable`. Only an observation that
carries a real reading for every unmeasured resource replaces the marker. That
reading is then judged against the ceiling like any other.

**Peaks are sticky.** A memory or storage peak is the highest reading ever
recorded. Freeing memory or space does not clear a breach; only a higher
declared ceiling does. The root peak is the largest single reading reported by
any task. It is not a sum across tasks that run at the same time; a caller that
needs an aggregate ceiling records an aggregate reading on the root.

### The whole chain is checked

Every dispatch charge walks from the task to the root:

- an unreadable budget anywhere in the chain refuses the dispatch (`BudgetError`);
- `cancel_requested` anywhere raises `ExecutionCancelled`;
- `isolation_required` anywhere needs isolation proof, otherwise
  `IsolationUnavailable`.

### One task at its limit

When one task reaches its own limit and the root budget is within limits, the
live report stays `in_progress` and lists that stop code. It does not demand a
halt. A `bounded_stop` halt is accepted and records `scope: "task:<key>"`. The
report reads `stopped` without a halt only when the root budget itself requires
the stop.

## Isolation

A budget can declare `isolation_required`. Proof is an `IsolationEvidence` value
built by server code from the sandbox admission decision. It counts only when
all of these hold: execution mode is `sandbox`, admission is literally `True`,
the backend is literally qualified, and a backend id is present.

`SecurityIncidentStore.check_admission` returns `allowed=True` with no backend
when none exists, so `allowed` alone is never accepted as proof.

- `claim_ready_mission_task` and `prepare_mission_task_resume` check the root
  budget. On refusal they raise `MissionIsolationRejected`; no worker lease is
  created.
- Every dispatch charge checks the whole chain and raises `IsolationUnavailable`.
  The app tool boundary maps it to `error_code: "isolation_unavailable"`. No tool
  call is debited and nothing is executed.
- There is no downgrade. `host` and `disabled` modes are never isolation.

**No production caller presents isolation proof.** In this build a mission that
requires isolation is always refused. The accepting path is exercised only with
a test fixture, because no qualified runtime exists on the authoring host.

## Recovery report

```python
report = machine.mission_recovery_report(mission_id, plan_version, artifact_status=checker)
```

| Field | Meaning |
|---|---|
| `outcome` | see below |
| `claims_success` | `true` only when `outcome` is `succeeded` and there is no limitation |
| `plan_status`, `join_status` | the plan lifecycle and the AP-005 join result |
| `tasks` | per task: state, outcome, retained outputs, missing outputs, unresolved action ids |
| `stop` | the stop record, when the plan was halted |
| `limitations` | each with `code`, `message` and `next_action` |
| `report_hash` | SHA-256 over the report without its evaluation time |

| Outcome | When |
|---|---|
| `succeeded` | every task complete, nothing unresolved, every retained output checked complete |
| `unverified` | join succeeded, but outputs were not checked or the root budget is unreadable |
| `partial` | join succeeded, but an effect is unresolved or a retained output is not a finished artifact |
| `cancelled` | the plan was cancelled, or the root is cancelled while the plan is still live |
| `stopped` | the plan stopped at a limit, or the root budget requires a stop |
| `blocked` | work is owed and required isolation is not proven |
| `in_progress` | work is owed and nothing blocks it |
| `superseded` | a newer plan version replaced this one |
| `partial` or `failed` (the join status) | work is owed but no task is still in progress: tasks failed, were cancelled or miss outputs |

`artifact_status` is a callable from an artifact SHA-256 to a verdict with
`state` and `complete`. `artifact_lineage.ArtifactRegistry.artifact_status`
fits. A checker that raises, or returns anything other than a literal complete
verdict, is read as not complete. Without a checker, retained outputs are listed
as `unverified` and the report cannot claim success.

Every limitation carries a next action. The codes are `mission_cancelled`,
`no_progress`, `repair_attempts`, `deadline`, `memory`, `storage`,
`measurement_unavailable`, `budget_unreadable`, `stop_record_missing`,
`isolation_unavailable`, `uncertain_effects`, `owned_work_lingering`,
`halt_required`, `owned_work_not_terminated`, `tasks_incomplete`,
`missing_required_outputs`, `artifact_incomplete`, `artifacts_not_verified` and
`plan_superseded`.

Reports are stored as hash-addressed rows in `runtime_mission_joins`, marked
`report_kind: "mission_recovery"`. A report identical to a stored one is not
stored again. Per-task transition evidence records the stop kind and code; the
measured stop record lives in the report row.

## HTTP boundary

`/api/harness/run` and `/api/harness/gsd-wave` parse a budget through
`_parse_task_budget_spec`. A request that names any of the six new fields is
answered with 400:

> Invalid task budget: `<keys>` cannot be set through this route. Bounded-stop
> and isolation limits are not activated.

No route measures progress, memory or storage, so a limit set over HTTP would
either never fire or stop work on a missing measurement.

**This is a visible change for API clients.** Before this change the parser
ignored unknown budget keys, so a request that happened to carry one of these
six names was accepted and the key had no effect. Such a request is now refused.
No route is added or removed, and no caller in this repository sends these keys.
Other unknown keys are still ignored, as before.

## Compatibility and rollback

- Existing budget documents parse unchanged and keep their identity digest.
- No data is deleted. Stops append transitions and report rows.
- **Rollback hazard.** A binary older than this change must not write to a task
  that declares the new limits. The older reader accepts the document and
  ignores the new keys. Its next write to that task drops the declared limits,
  the counters, the unmeasured markers and the isolation requirement. The newer
  binary then finds a different budget identity and refuses the plan's bound
  operations as stale, but the limits themselves are gone and must be declared
  again. This outcome was measured by loading the base commit's
  `execution_budget.py` against a document written by this version.

  No route can declare these limits, so a store written only through the HTTP
  API carries none of them.

## Known limits

- Nothing calls the mission stop, the report or the observation recorder in
  production. Activation is separate, owner-authorized work and depends on U06.
- The stop terminates processes registered with the supervisor under the
  plan's task ids. A process that was never registered is not found.
- Stopping without a supervisor fences dispatch durably but attests nothing
  about running processes.
- A superseded plan is fenced by the AP-005 path, which does not turn in-flight
  actions `uncertain`. That behaviour is unchanged here.
- Results are author-run on a Linux host with no container runtime. They are not
  installed-runtime, owner, independent-security, screenshot, signing or release
  approval.

## Relation to the U06 call-path audit

The [U06 call-path audit](autonomous-production-u06-call-path-audit.md) is
pinned to `bc406fd` and was written without this work. It takes the position
that AP-007 refusal tests should wait until the owner resolves U06 and
authorizes the story (its gap G-6). This work proceeded on the owner
instructions quoted in the AP ledger, which allow a safe fail-closed subset of
a blocked story. The owner decides which reading stands. This document does not
settle it.

| Audit gap | Effect of this work |
|---|---|
| G-1 mission-scoped sandbox gate | Adds an optional mission-level refusal: a budget may declare `isolation_required`. Nothing declares it. Whether missions must require isolation is still a U06 question. |
| G-6 refusal tests | Implemented offline, together with the behaviour they test. |
| G-2, G-3, G-4, G-5 | Not touched. |

The audit page is not edited here. If this work lands, the page's statements
about AP-007 and G-1 are out of date. Its own maintenance rule then asks its
owner to regenerate it or mark it superseded.

## Acceptance mapping and offline evidence

| AP-007 acceptance criterion | Evidence |
|---|---|
| Cancellation stops dispatch and terminates owned work with measured cleanup latency | `halt_mission`; `tests/test_durable_task_state_machine.py` cases for cancellation, latency measured from the stop request, delegated descendants, containment retry and containment failure |
| Late outputs are fenced; unresolved external effects remain uncertain and require reconciliation | ledger and boundary fence; `tests/test_durable_intent_ledger.py` (two cases); state-machine cases for late results and the planning gate |
| No-progress, repeated repair, deadline and memory/storage exhaustion stop boundedly | `execution_budget` limits; `tests/test_execution_budget.py`; state-machine cases for declared limits, the deadline, task scope, the measurement gap and stale workers |
| Runtime isolation absence blocks dependent execution rather than switching to host shell | isolation gate; state-machine and budget cases; `tests/test_mission_authority_envelope.py` at the real tool boundary |
| User receives retained partial artifacts and an actionable limitation instead of false success | `mission_recovery_report`; state-machine cases for checked artifacts, an uncarried stop and halted plans |

The tests use temporary local SQLite stores and local fixture subprocesses. They
use no network, provider or model.
