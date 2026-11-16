---
id: ADR-0042
title: late-result-fence-limited-to-mission-owned-work
status: accepted
date: 2026-11-16
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0042: Late-result fence limited to mission-owned work

## Context

[ADR-0041](0041-mission-halt-bounded-stops-and-recovery-reports.md) decided,
among other things, that a late result cannot settle an action the ledger has
already declared `uncertain`, and that this "applies to every task, not only to
mission tasks". PR #303 merged with that scope.

The fence sits in `DurableActionLedger.complete` and `record_exception`, which
every tool call uses. With the every-task scope it therefore changed something
the running application already did. In one race (a cancel, a takeover or crash
recovery marks an action `uncertain` while its executor is still running) the
late result used to overwrite the verdict and the session continued. After
PR #303 the session paused for reconciliation instead.

AP-007 is blocked by owner decision U06, and only its offline, fail-closed
subset is in scope. The owner instructed the AP-007 builder session to limit
the fence, so that the action ledger behaves for the running application as it
did before AP-007. The instruction is quoted in the AP ledger
(`stories[AP-007].tracking.post_completion_changes`).

This record changes one decision of ADR-0041. Every other decision in ADR-0041
stands.

## Decision

The late-result fence applies to mission-owned work only.

An action is mission-owned when its task is a task of a mission plan
(`runtime_mission_tasks`, any plan version), or a descendant of one through
`runtime_tasks.parent_task_id`. This is the ownership `halt_mission` uses when
it stops a plan: the plan's tasks and everything they delegated. An action with
no task is never mission-owned. The root task of a mission is not a task of the
plan, so its own actions are not mission-owned either.

- **Mission-owned action.** Unchanged from ADR-0041. `complete` and
  `record_exception` leave an `uncertain` row untouched, the action boundary
  returns `late_result_fenced`, and only `resolve_uncertain_action` resolves
  the action.
- **Any other action.** As before AP-007. The late result is recorded and
  replaces the `uncertain` verdict.

Ownership is decided by who owns the action, not by what declared it
`uncertain`. A mission task's action is fenced whether a mission stop, an
ordinary task cancel, a takeover or recovery made it `uncertain`.

The check is `side_effect_ledger._late_result_is_fenced`. It runs inside the
existing finalize transaction and reads only when the row is already
`uncertain` and has a task. No table, column or schema version is added. A
store that predates mission plans (schema v3) has no mission tables and
therefore no mission-owned work.

## Alternatives

- Keep the fence for every task, as ADR-0041 decided: not chosen for now. It is
  the stricter behaviour, but it changes what the running application does
  before U06 is decided. A later decision can restore it.
- Decide by the error code that declared the action `uncertain`: rejected. A
  mission stop by cancellation writes `cancelled_in_flight`, the same code an
  ordinary task cancel writes, and `reconcile_missing_results` writes whatever
  code its caller passes. The code cannot tell mission work from other work.
- A marker column on `runtime_actions`, set by the mission stop: rejected. It
  needs a schema version for a fact the existing tables already hold, and it
  would miss a mission action that an ordinary cancel or recovery declared
  `uncertain`.
- A configuration switch between the two scopes: rejected. It adds a second
  behaviour to test and a setting with no owner.
- Fence the plan's own tasks but not their delegated descendants: rejected.
  `halt_mission` stops the descendants too, so their late results would settle
  effects of stopped work.

## Tradeoffs

Verified by reading the callers: no route or other production caller creates a
mission plan, so a store written through the application has no mission-owned
work and the fence does not fire there. For that application the action ledger
and the action boundary answer a late result as they did before AP-007.

Tests pin both sides. Session-only actions, plain tasks, plain child tasks, the
mission's root task and a schema v3 store keep the old outcome. A plan task and
a delegated descendant stay fenced after a mission stop, and a plan task stays
fenced after recovery. The cases for work that no mission owns were also run
once against the pre-AP-007 commit and pass there unchanged. That run is a
one-off measurement, not a repository test.

Cost: outside missions the race that ADR-0041 closed is open again. A late
success or failure overwrites an `uncertain` verdict that an ordinary cancel, a
takeover or crash recovery wrote. This is the behaviour the application had
before AP-007, not a new defect, but the every-task fence had removed it.

The check adds, for a late result on an `uncertain` action that has a task, one
catalogue lookup for the mission table and one walk up the task chain. The walk
uses existing primary keys and the existing `runtime_mission_tasks(task_id)`
index.

Ownership follows the task lineage across every plan version and every plan
status. A task of a superseded or finished plan version is still mission-owned.

Two other AP-007 changes remain on paths the running application uses and are
not changed by this record: `/api/harness/run` and `/api/harness/gsd-wave`
refuse the six bounded-stop budget keys with 400, and the budget charge reads
each budget from the task up to the root.

## Review Status

Not reviewed. Accepted on the owner instruction recorded in the AP ledger,
without independent review. U06 remains pending. Widening the fence again, any
activation and any change to the authority boundary require separate review
and owner authorization.
