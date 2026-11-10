---
id: ADR-0041
title: mission-halt-bounded-stops-and-recovery-reports
status: accepted
date: 2026-11-10
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0041: Mission halt, bounded stops and truthful recovery reports

## Context

AP-005 added immutable mission plans on the durable execution spine. The controls
that stop work already existed, but each at a narrower scope:

- `workflow_state.cancel_task` fences the root task and its direct children only,
  and never changes a mission plan's status;
- `process_supervision.ProcessSupervisor` measures terminate, kill and reap per
  supervised process, but nothing ties those processes to a mission plan;
- `execution_budget` limits steps, tool calls, tokens, elapsed time and recoveries,
  but has no no-progress, repair-attempt, deadline, memory or storage limit;
- `sandbox_boundary` refuses unqualified backends, but a mission cannot declare
  that it must not run without isolation;
- an AP-005 join reports `pending`, `partial`, `failed` or `succeeded`, but does
  not say what was retained, which effects are unresolved or what to do next.

AP-007 requires cancellation with measured cleanup latency, fenced late outputs,
bounded stops, refusal when isolation is absent, and a report that never reads
as success after a stop.

AP-007 is blocked by owner decision U06 (lifecycle and sandbox qualification and
activation). Only the offline, fail-closed subset is in scope: nothing may be
activated, and no behaviour may assume how U06 is decided.

## Decision

Extend the existing owners. Add no control plane, table, store file, schema
version, route, endpoint or UI control.

**One mission-scoped stop.** `DurableTaskStateMachine.halt_mission` stops one plan
version. A single write transaction marks the plan `cancelled` (operator stop) or
`failed` (bounded stop). For every unfinished task the plan owns, including
delegated descendants at any depth, it fences the live worker lease, turns
in-flight consequential actions `uncertain`, sets the budget cancellation flag
and moves the task to `cancelled`. After the transaction, the leases it fenced
are also fenced at the process layer. With a `ProcessSupervisor`, the processes
registered for those tasks are terminated and the time from the stop request to
the last verified exit is recorded.

**The stop is not settled until it is verified.** Without a supervisor the stop
record says termination was not attempted. `create_mission_plan` refuses a new
plan on the same root task while any stopped plan of that root has unresolved
consequential actions, no attested termination or no clean stop.

**Late results cannot settle an uncertain action.** `DurableActionLedger.complete`
and `record_exception` leave an `uncertain` row unchanged. The action boundary
returns `late_result_fenced`. Only `resolve_uncertain_action` resolves it.

**Bounded stops live in the existing budget document.** Six optional fields
(`max_no_progress`, `max_repair_attempts`, `deadline_at`, `max_memory_bytes`,
`max_storage_bytes`, `isolation_required`) and their counters are written only
when declared or non-zero. Existing documents and their identity digests are
byte-for-byte unchanged. A ceiling that cannot be measured stops the work
(`measurement_unavailable`) instead of assuming headroom. The stop reason is
committed before the refusal is raised.

**Isolation is a requirement the budget can declare.** When it is declared,
claims, resumes and dispatches are refused unless the caller presents literal
proof of an admitted, qualified sandbox backend. There is no host fallback and
no downgrade. No production caller presents such proof, so in this build a
mission that requires isolation is always refused.

**One truthful report.** `mission_recovery_report` states the outcome, the
retained outputs per task, the unresolved actions and one next action for every
limitation. It claims success only when the join succeeded, nothing is
unresolved, and every retained output was checked as a complete artifact.
Reports are stored as additional hash-addressed rows in the existing
`runtime_mission_joins` table.

**The HTTP boundary refuses the new limits.** `_parse_task_budget_spec` answers
400 for the six new keys, because no route measures or enforces them. The parser
previously ignored unknown keys, so a client that sent one of these names was
accepted before and is refused now.

## Alternatives

- A mission supervisor, scheduler loop or second state store: rejected. It would
  compete with `workflow_state`, `execution_budget` and `process_supervision`.
- A new SQLite column or schema v5 for stop records: rejected. The report rows
  fit the existing joins table, and a schema change would force a migration for
  an offline capability.
- Extending `cancel_task` to cascade through every level: rejected. It would
  change the behaviour of every existing caller. A whole-task cancel with an
  active mission is `cancel_task(root)` followed by `halt_mission`.
- A `stop_code` parameter on `halt_mission`: rejected. A caller could then record
  a bounded stop that no budget requires. The code and reason come from the
  durable budget.
- Storing a late result's digest on a fenced row: rejected. It would read as
  evidence that the effect happened in a known state.
- Fencing every historical epoch of the plan at the process layer: rejected. The
  containment store keeps only the newest 500 origin fences, so this would push
  out fences that are still needed.
- Treating a retry that finds no registered process as a clean stop: rejected. A
  process the first attempt could not end is no longer registered, so "nothing
  found" is not proof that it exited.
- Passing isolation as a boolean: rejected. `allowed` alone is true when no
  backend exists; only an admitted, qualified, named backend in sandbox mode
  counts.

## Tradeoffs

A stop that cannot be verified blocks new plans on that root task until a
supervised stop runs and every uncertain action is reconciled. This is
deliberate: the alternative is planning new work beside processes and effects in
an unknown state.

A supervised stop leaves the barrier hold of each process it ended in the
containment store, as a session cancel already does. The hold blocks conflicting
work in that workspace until an operator releases it. The report does not list
holds.

Process-layer fencing assumes one worker identifier per claim. A worker
identifier that is reused across claims is over-blocked, never under-blocked.

Memory and storage peaks are sticky maxima. Freeing memory or disk space does
not clear a breach; only a higher declared ceiling does. A root peak is the
largest single reading any task reported, not a sum across tasks that run at the
same time.

Rolling back to a binary older than this change is unsafe for any task that
declares the new limits. The older reader accepts the document, ignores the new
keys and drops them on its next write to that task. This was measured against
the base commit's `execution_budget.py`, not assumed.

No route, scheduler or production caller invokes the halt, the report or the
observation recorder. The positive isolation path is exercised only with a test
fixture, because no qualified runtime exists on the authoring host. The
implementation and its offline tests are author-run evidence only and are not
owner, independent-security, screenshot, signing, release or installed-runtime
approval.

## Review Status

Not reviewed. Accepted as the implemented AP-007 offline fail-closed subset,
without independent review. U06 remains pending. Any activation, any execution
on a qualified backend, any installed-runtime qualification and any change to
the authority boundary require separate review and owner authorization.
