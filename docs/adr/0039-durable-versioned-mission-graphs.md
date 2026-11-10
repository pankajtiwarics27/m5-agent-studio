---
id: ADR-0039
title: durable-versioned-mission-graphs
status: accepted
date: 2026-11-09
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0039: Durable versioned mission graphs and recoverable output joins

## Context

The existing SQLite execution spine owns durable tasks, state transitions,
worker epochs, actions, and checkpoints. AP-004 also supplies server-created
mission authority envelopes and atomic root/leaf budget admission. It did not,
however, represent one immutable project plan, dependency edges, required output
contracts, or the plan/input/authority/budget identity from which a checkpoint
was produced. A filesystem plan log or a second scheduler would split authority;
reusing child-delegation JSON state as the plan database would also bypass the
transactional task and epoch fences.

AP-005 requires dependency cycle rejection, restart-safe scheduling, stale-worker
and stale-version fencing, explicit partial/failed joins, and a populated-store
migration with no reset fallback. The work remains local and dormant: it must not
activate providers, models, network access, background workers, or later AP
stories.

## Decision

Add application SQLite schema v4 as an additive migration beside the existing
v3 execution spine. Normalize immutable mission plan versions, task membership,
dependency edges, required output references, checkpoint bindings, and append-only
join reports in `runtime_mission_*` tables. Artifact bytes remain in their existing
owners; mission output rows store only a key, reference, digest, evidence summary,
and producing epoch.

Keep `runtime_tasks` and `workflow_state.DurableTaskStateMachine` authoritative
for lifecycle transitions. Plan creation atomically decomposes nodes into existing
runtime-task rows. It validates all references and output contracts and rejects a
cycle before writing. Scheduling is a read/claim boundary: a node can receive a
new monotonic worker epoch only when its prerequisite tasks are complete and all
edge-required outputs are durable. Superseding a plan fences active epochs from
the old version and excludes old nodes from the active parent join without
deleting their history.

Bind mission checkpoints to the immutable plan digest/version, canonical input
digest, AP-004 session/global policy revisions, root budget-contract digest,
server-derived task state version, and worker epoch. Resume revalidates each field,
checkpoint integrity, unresolved effects, current task state/version, and absence
of a successor before atomically minting a new epoch. Usage counters are excluded
from the budget identity so legitimate consumption does not invalidate recovery;
budget limits and the verification contract are included so authority changes do.

Complete a mission node only inside one transaction that verifies the current
plan binding, dependency state, expected state version, live worker/epoch, lease
expiry, unresolved consequential actions, and the exact required-output set.
Output rows are insert-only; stale workers cannot upsert over a successor. Parent
join reports classify every node as completed, incomplete, failed, cancelled, or
missing required outputs, then report `pending`, `partial`, `failed`, or
`succeeded`. Root completion re-evaluates the join in its final write and refuses
anything except `succeeded`.

Apply schema v4 through the existing online snapshot, `BEGIN IMMEDIATE`, integrity
check, rollback, and restore path. Existing user history, sessions, v3 tasks,
actions, transitions, and checkpoints are preserved. Older binaries reject a v4
`user_version`; no code path deletes, recreates, or silently downgrades the store.
Mission tables are security/integrity state and remain excluded from user-history
export.

## Alternatives

- Store a JSON DAG beside the workspace: rejected because filesystem writes would
  not share task epoch, checkpoint, migration, or join transactions.
- Add another scheduler/task database: rejected because it would compete with
  `runtime_tasks`, AP-004 budget authority, and the existing recovery sweep.
- Encode dependencies only in checkpoint payloads: rejected because readiness,
  cycles, output joins, and stale plan versions could not be constrained or
  queried transactionally.
- Reuse child-delegation join state: rejected because bounded subagent delegation
  has a different authority lifecycle and a JSON crash store; mission nodes may
  execute without delegated agents.
- Reset old databases on incompatibility: rejected because it destroys user data
  and audit history and violates the established migration contract.

## Tradeoffs

Schema v4 adds six normalized tables and more validation queries at dispatch,
checkpoint, completion, and join boundaries. This is deliberate fail-closed cost,
not a throughput claim. The graph stores artifact identities but does not add the
selective invalidation, retention, or preview security assigned to AP-006. It also
does not install a scheduler loop or expose a new API; activation and UI remain
later authorized work.

Plan versions are immutable and old rows remain for audit, increasing local
storage until a separately authorized retention policy exists. Budget identity
covers limits and verification contracts but not mutable usage counters; current
budget exhaustion and cancellation are checked independently. The implementation
and offline tests are author-run evidence only and do not constitute independent
security, owner, release, screenshot, or installed-runtime approval.

## Review Status

Not reviewed. Accepted as the implemented AP-005 internal persistence and
state-machine decision, without independent review. Any production activation,
external effect, destructive migration, or change to the authority boundary
requires separate review and authorization.
