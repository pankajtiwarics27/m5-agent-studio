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

Four changes are on paths the running application already uses. Two of them
are visible to API clients today: two routes refuse six budget keys, and the
security-incident routes return two more keys for a qualification. They are
listed under [Changes on live paths](#changes-on-live-paths).

The design decision is recorded in
[ADR-0041](adr/0041-mission-halt-bounded-stops-and-recovery-reports.md).
[ADR-0042](adr/0042-late-result-fence-limited-to-mission-owned-work.md) limits
the late-result fence to mission-owned work.
[ADR-0043](adr/0043-proven-sandbox-admission-for-mission-owned-consequential-work.md)
makes isolation proof mandatory for consequential actions of mission-owned work
and judges it against a recorded acceptance.
[ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
adds two conditions to that proof, a qualification time that the report stated
itself and the recorded target host, and makes the recovery report state the
refusal. The mission plans this builds on are described in
[Durable mission graphs](durable-mission-graphs.md).

## Owners

No new control plane, table, store file or schema version is added. SQLite stays
at schema v4.

| Capability | Owner | What changed |
|---|---|---|
| Mission stop and recovery report | `workflow_state.DurableTaskStateMachine` | `halt_mission`, `halt_mission_async`, `mission_recovery_report`; a gate in `create_mission_plan`; isolation and deadline checks in `claim_ready_mission_task` and `prepare_mission_task_resume` |
| Bounded limits and observations | `execution_budget` | six optional limit fields, their counters, `record_bounded_observation` |
| Isolation gate | `execution_budget`, held by `DurableTaskBudgetManager` | `IsolationEvidence`, `IsolationAcceptance`, the three activation steps, `isolation_refusal`; the charge asks for proof for consequential actions of mission-owned work |
| What a qualification rests on | `sandbox_boundary.BackendQualificationRecord` | two fields with defaults, `report_time_stated` and `host_fingerprint`; `qualification_host_fingerprint`. The sandbox admission of the application reads neither field |
| Late-result fence | `side_effect_ledger.DurableActionLedger`, `execution_spine.DurableActionBoundary` | `complete` and `record_exception` leave an `uncertain` mission-owned action unchanged; the boundary then reports `late_result_fenced` |
| Process termination | `process_supervision.ProcessSupervisor` | reused as is; one read-only helper, `still_running` |
| Stop records | `runtime_mission_joins` (existing table) | holds `mission_recovery` reports beside the AP-005 join snapshots |
| HTTP budget parsing | `app._parse_task_budget_spec` | refuses the six new keys with 400 |

`execution_spine.MissionAuthorityEnvelope` is unchanged. A halt needs the typed,
server-created envelope; request JSON and model tool arguments are never parsed
into one.

## Changes on live paths

Most of this work is unreachable until a caller is added. Four changes sit on
paths the running application already uses. The second changes what that
application does today, and the fourth adds two keys to JSON it already returns.

1. **Late-result fence, for mission-owned work only.** Every tool call goes
   through `DurableActionBoundary` and the action ledger, so this code is on
   the path of every call. It changes the outcome only for an action that a
   mission owns: its task is a task of a mission plan, or a descendant of one.
   When a result arrives for such an action after the ledger marked it
   `uncertain` (a mission stop, a takeover or recovery got there first), the
   result does not settle the action. The caller receives
   `late_result_fenced`, and the harness pauses as it does for any other
   uncertain result.

   For every other action the late result is recorded and replaces the
   `uncertain` verdict, as it did before AP-007. Nothing in the running
   application creates a mission plan, so the fence does not fire there.
   PR #303 first merged this fence for every task;
   [ADR-0042](adr/0042-late-result-fence-limited-to-mission-owned-work.md)
   records why it was limited.
2. **Budget keys over HTTP.** Two routes refuse six key names that they used to
   ignore. See [HTTP boundary](#http-boundary).
3. **Budget charge.** Every tool-call charge now reads each budget from the task
   up to the root. The application creates root tasks and direct child tasks
   today. For those chains, with none of the new limits declared, the outcome
   is unchanged.

   Since [ADR-0043](adr/0043-proven-sandbox-admission-for-mission-owned-consequential-work.md)
   the charge of a consequential action also looks up whether a task of the
   chain belongs to a mission plan, and the application's mission envelope
   coordinator tells the charge whether the action is consequential. When a
   mission owns the task, the action is refused with `isolation_unavailable`.
   Nothing in the running application creates a mission plan, so this refusal
   does not occur there. See [Isolation](#isolation).
4. **Qualification records.** Since
   [ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
   a qualification that the security store records carries two more fields:
   whether its report stated a generation time of its own, and the fingerprint
   of the machine the report describes. They are written by the three
   security-incident routes that record a qualification, and they appear in the
   JSON those routes and `GET /api/security/incidents` already return for a
   qualification, and in the store file.

   The application's own admission does not read them. It accepts the same
   reports and gives the same answers as before. See
   [What a qualification rests on](#what-a-qualification-rests-on).

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
| Action ledger `complete` or `record_exception` on an `uncertain` action of the plan | row unchanged |
| Action boundary receiving that late result | `ok: false`, `uncertain: true`, `error_code: "late_result_fenced"` |

A fenced action is resolved only by
`DurableActionLedger.resolve_uncertain_action`, with `verified_committed` or
`verified_not_applied`. A late result is not stored on the fenced row.

The last two rows cover the plan's tasks and the tasks they delegated. They do
not depend on what declared the action `uncertain`: a mission stop, an ordinary
task cancel, a takeover and recovery are treated alike. An action that no
mission owns is outside the fence. That includes an action of the mission's
root task and an action with no task. Its late result is recorded, as before
AP-007.

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
  `IsolationUnavailable`;
- a task of a mission plan anywhere in the chain needs isolation proof for a
  consequential action, otherwise `IsolationUnavailable`
  (see [Isolation](#isolation)).

Cancellation is checked first, then isolation, then the limits.

### One task at its limit

When one task reaches its own limit and the root budget is within limits, the
live report stays `in_progress` and lists that stop code. It does not demand a
halt. A `bounded_stop` halt is accepted and records `scope: "task:<key>"`. The
report reads `stopped` without a halt only when the root budget itself requires
the stop.

## Isolation

[ADR-0043](adr/0043-proven-sandbox-admission-for-mission-owned-consequential-work.md)
and
[ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
record this section's rules. They follow the owner's U06 answers 1 to 4, which
are owner input and not a resolution of U06.

**In this build every consequential action of mission-owned work is refused.**
No acceptance is recorded, no production caller presents proof, and nothing
observes the host. The accepting path runs only in tests, with a fictional
acceptance record and fictional evidence, because no qualified runtime exists on
the authoring host.

### When proof is needed

| Work | Action | Proof needed |
|---|---|---|
| Mission-owned | consequential (`managed_edit`, `consequential`, `external`) | yes |
| Mission-owned | read-only | no, unless a budget in the chain declares `isolation_required` |
| Mission-owned | any action of the browser-worker or native-lifecycle step | yes, and it is always refused (see [Steps](#steps)) |
| Any task whose chain declares `isolation_required` | any | yes |
| Work that no mission owns, nothing declared | any | no; this gate does not judge it |

Work is mission-owned when the action's task is a task of a mission plan, or a
descendant of one. This is the rule the mission stop and the late-result fence
use. The task a plan hangs from is not a task of the plan. A store without
mission tables owns no mission work.

The check sits in the budget charge, `charge_hierarchical_tool_call`, which is
the last admission step before durable intent and dispatch. The charge takes
`consequential` and `isolation_step`. Both default to the refusing side: a
caller that does not say what it dispatches is treated as dispatching a
consequential action of the contained-execution step.

- Every dispatch charge that needs proof and does not get it raises
  `IsolationUnavailable`. The app tool boundary maps it to
  `error_code: "isolation_unavailable"`. No tool call is debited and nothing is
  executed.
- `claim_ready_mission_task` and `prepare_mission_task_resume` check the root
  budget's declared `isolation_required`. On refusal they raise
  `MissionIsolationRejected`; no worker lease is created. A claim is not a
  dispatch, so a plan that declares nothing can be claimed without proof.
- There is no downgrade. `host` and `disabled` modes are never isolation.

### What counts as proof

Proof has two parts. Both are server-side values; neither is ever parsed from a
request body or from model output.

**The acceptance record**, `IsolationAcceptance`, says what a recorded owner
decision accepts. `DurableTaskBudgetManager` and `DurableTaskStateMachine` take
it when they are constructed (`isolation_acceptance`). The default is none, and
without one nothing is accepted.

| Field | Meaning |
|---|---|
| `decision_ref` | the recorded decision the acceptance comes from |
| `runtime`, `runtime_version`, `image_digest` | the one container backend that is accepted; the digest must be a full `sha256` digest |
| `qualified_not_before` | the earliest qualification run that is accepted |
| `target_host` | the host the decision accepts, as a host fingerprint (see below) |
| `activated_steps` | the steps the decision covers, as a prefix of the activation order |

**The evidence**, `IsolationEvidence`, is what the server observed for one
dispatch. `IsolationEvidence.from_admission` builds it from the sandbox
admission decision, the active backend's qualification record and the host the
server observed. It is accepted only when all of these hold:

- the acceptance activates the step of the dispatch, and the evidence proves
  that step;
- execution mode is `sandbox`, admission is literally `True`, the backend is
  literally qualified, and a backend id is present;
- runtime, runtime version and image digest equal the accepted ones. The
  comparison is exact; only the runtime name is case-insensitive;
- the evidence names a qualification, the qualification has a usable time for
  its run, and that run is not older than `qualified_not_before`;
- the qualification was run on the accepted target host (`qualified_host`), and
  the dispatch happens on it (`observed_host`).

Each refusal names the first condition that fails, in this order.

`SecurityIncidentStore.check_admission` returns `allowed=True` with no backend
when none exists, so `allowed` alone is never accepted as proof.

A qualification is as old as the run that produced it. The adapter uses the
earlier of the time the qualification record was written and the time its report
was generated. Recording an old report again does not make it fresh.

### What a qualification rests on

[ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
records these rules.

**The time must be the report's own.** When a qualification report states no
generation time, the sandbox layer records the time of ingestion. That time says
nothing about the age of the run. The qualification record now says which case
applies (`report_time_stated`), and the adapter takes a qualification time only
from a record whose report stated one. Evidence without a usable time is refused
with its own reason. A time that is zero, negative, not finite or not a number
is not usable.

**The host must be the recorded one.** A host is named by a fingerprint:
`sha256:` and 64 hex digits, computed by
`sandbox_boundary.qualification_host_fingerprint` from the machine description
that a qualification report carries. Five values count: operating system,
architecture, processor model, processor count and memory size. The
operating-system release and version, the Python version, the git revision and
the memory readings of the same description do not.

| Value | Where it comes from |
|---|---|
| `IsolationAcceptance.target_host` | the recorded decision |
| `IsolationEvidence.qualified_host` | the qualification record, which keeps the fingerprint of its report's machine description (`host_fingerprint`) |
| `IsolationEvidence.observed_host` | server code, for the dispatch. It is never taken from a request or from the record |

All three must be equal. A report produced on another machine fails on
`qualified_host`. A security store copied to another machine fails on
`observed_host`.

What the fingerprint is not:

- It is not a unique hardware identifier. Two machines of the same model and
  memory size share it.
- It is not authenticated. The gate believes what a report says about its
  machine and its time, as the sandbox layer already believes its verdict.

**For the application nothing changes.** The sandbox layer accepts an undated
report, and a report without a machine description, as before. The backend is
qualified for the application either way. A record written before these fields
existed reads as not stated and no host: it stays valid for the application and
is no proof for a mission until its report is ingested again.

### Steps

The activation order is `contained_execution`, `browser_worker`,
`native_lifecycle`. An acceptance can record only a prefix of it.

Contained execution is the only step this build defines evidence for. A sandbox
admission proves contained execution and nothing else. Work of the two later
steps is refused for mission-owned work, whatever its action class and even when
an acceptance names the step. No tool in this build belongs to either later
step.

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
| `blocked` | work is owed, the root budget declares `isolation_required`, and isolation is not proven |
| `in_progress` | work is owed and the plan is not blocked as a whole |
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
`isolation_unavailable`, `consequential_actions_refused`, `uncertain_effects`,
`owned_work_lingering`, `halt_required`, `owned_work_not_terminated`,
`tasks_incomplete`, `missing_required_outputs`, `artifact_incomplete`,
`artifacts_not_verified` and `plan_superseded`.

**Refused consequential actions.** Every task of a plan is mission-owned, so the
charge refuses each of its consequential actions without accepted proof. The
report says so. For a live plan that still owes work and is neither cancelled
nor stopped, it judges the evidence it was given (`isolation`) by the rule the
charge uses. If the evidence is refused, the report carries
`consequential_actions_refused` with the reason. In this build that is every
such report, because no acceptance is recorded.

The limitation does not change the outcome. A plan with unfinished tasks stays
`in_progress`. It is not blocked as a whole: a read-only action is still
charged, and the report cannot know whether the remaining tasks need a
consequential action.

A plan is live while its status is `active`. It owes work until its join
succeeds.

| Plan | What the report says about isolation |
|---|---|
| not live (halted or superseded), or the join succeeded | nothing |
| live, owes work, proof accepted | nothing |
| live, owes work, proof refused, root budget declares `isolation_required` | `isolation_unavailable`, as before |
| live, owes work, proof refused, nothing declared, outcome `cancelled` or `stopped` | nothing; the report already says that the plan runs no further |
| live, owes work, proof refused, nothing declared, any other outcome | `consequential_actions_refused` |

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
- The two fields of a qualification record need no migration. A security store
  written before them reads with the refusing defaults. A binary older than
  them ignores both keys.
- A stored recovery report keeps its hash. A new report of a live unfinished
  plan has another hash than one written before
  `consequential_actions_refused` existed, because the limitation is part of the
  hashed report.

## Known limits

- Nothing calls the mission stop, the report or the observation recorder in
  production. Activation is separate, owner-authorized work and depends on U06.
- The stop terminates processes registered with the supervisor under the
  plan's task ids. A process that was never registered is not found.
- Stopping without a supervisor fences dispatch durably but attests nothing
  about running processes.
- A superseded plan is fenced by the AP-005 path, which does not turn in-flight
  actions `uncertain`. That behaviour is unchanged here.
- The late-result fence covers mission-owned work only. Outside a mission, a
  late result still overwrites an `uncertain` verdict written by a cancel, a
  takeover or crash recovery, as it did before AP-007. Extending the fence to
  all work changes what the running application does and needs an owner
  decision.
- Nothing records an isolation acceptance, builds isolation evidence, observes
  the host or presents proof in production. A mission therefore cannot dispatch
  a consequential action in this build. Whoever adds the observer must read the
  five values the qualification suite records and digest them with
  `qualification_host_fingerprint`; a fingerprint computed another way does not
  match, and the gate refuses.
- The host fingerprint names a kind of machine, not one unit, and a
  qualification report is not authenticated.
- The recovery report does not say that work of the browser-worker and
  native-lifecycle steps is refused. That refusal is reported where it happens,
  at the tool boundary.
- The recovery report reads `isolation_required` from the root budget only. When
  a task budget below the root declares it, the report states the refusal of
  consequential actions and does not say `blocked`.
- The isolation gate does not judge work that no mission owns, whatever step it
  names.
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
| G-1 mission-scoped sandbox gate | PR #303 added an optional mission-level refusal: a budget may declare `isolation_required`, and nothing declares it. ADR-0043 makes proof mandatory for consequential actions of mission-owned work and judges it against a recorded acceptance. ADR-0044 adds a qualification time that the report stated itself and the recorded target host to that proof. Whether that is sufficient as the mission-scoped gate is still the owner's U06 decision. |
| G-6 refusal tests | Implemented offline, together with the behaviour they test. |
| G-2, G-3, G-4, G-5 | Not touched. |

The audit page was not edited by that work. After it landed, the page's G-1 and
G-6 statements were out of date; the audit has since been regenerated at
`972ea74` (see the audit page's revision history), so its U06-decision questions,
gap table and fence statements reflect the landed AP-007-F1 change. The owner
still decides which reading stands.

That revision of the audit describes the isolation gate as opt-in. With
ADR-0043 and ADR-0044 its statements about the gate describe the earlier state
until the page is regenerated, as its maintenance rule asks. This work does not
edit the page.

## Acceptance mapping and offline evidence

| AP-007 acceptance criterion | Evidence |
|---|---|
| Cancellation stops dispatch and terminates owned work with measured cleanup latency | `halt_mission`; `tests/test_durable_task_state_machine.py` cases for cancellation, latency measured from the stop request, delegated descendants, containment retry and containment failure |
| Late outputs are fenced; unresolved external effects remain uncertain and require reconciliation | ledger and boundary fence for mission-owned work; `tests/test_durable_intent_ledger.py` (two cases for mission-owned work, three for the unchanged behaviour outside missions); state-machine cases for late results, the scope of the fence, recovery and the planning gate |
| No-progress, repeated repair, deadline and memory/storage exhaustion stop boundedly | `execution_budget` limits; `tests/test_execution_budget.py`; state-machine cases for declared limits, the deadline, task scope, the measurement gap and stale workers |
| Runtime isolation absence blocks dependent execution rather than switching to host shell | isolation gate; state-machine and budget cases, including the cases for mission-owned work, the recorded acceptance, the steps, the stated qualification time, the target host and the security store's own types; `tests/test_sandbox_boundary.py` and `tests/test_security_incident_containment.py` for the host fingerprint and the unchanged admission of the application; `tests/test_mission_authority_envelope.py` at the real tool boundary, for a declared requirement and for mission-owned work |
| User receives retained partial artifacts and an actionable limitation instead of false success | `mission_recovery_report`; state-machine cases for checked artifacts, an uncarried stop, halted plans and the refusal of consequential actions |

The tests use temporary local SQLite stores and local fixture subprocesses. They
use no network, provider or model.
