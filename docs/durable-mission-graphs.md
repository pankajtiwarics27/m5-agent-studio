# Durable mission graphs and recoverable joins

AP-005 composes immutable project-plan DAGs on the existing durable execution
spine. It adds no public endpoint, scheduler loop, provider call, model execution,
background worker, filesystem grant, or external effect. The implementation is an
offline/local state contract for later authorized consumers.

## Authority and ownership

The existing owners remain unchanged:

- `runtime_tasks`, `runtime_task_epochs`, and `runtime_task_transitions` own task
  lifecycle and monotonic worker fencing;
- `workflow_state.DurableTaskStateMachine` is the only mission transition owner;
- `workflow_checkpoints.DurableTaskCheckpointStore` owns checkpoint integrity and
  the mission resume-binding write boundary;
- `execution_spine.MissionAuthorityEnvelope` supplies server-created session,
  root, leaf, and session/global policy-revision identity;
- `execution_budget.DurableTaskBudgetManager` owns current root/leaf limits,
  counters, cancellation, and verification contracts;
- `runtime_actions` remains the durable source for unresolved consequential
  effects.

A mission binding is coordination evidence, not user or model authority. The
caller must provide the typed server-created AP-004 envelope. Request JSON and
model tool arguments are not parsed into mission bindings.

## Schema v4

SQLite schema v4 adds six normalized tables:

| Table | Purpose |
|---|---|
| `runtime_mission_plans` | Immutable mission/version identity, root task, graph/input digests, policy revisions, budget-contract digest, and lifecycle. |
| `runtime_mission_tasks` | Ordered plan membership that maps task keys onto authoritative `runtime_tasks` rows and declares required outputs. |
| `runtime_mission_dependencies` | Directed prerequisite edges and the prerequisite outputs needed by each edge. |
| `runtime_mission_outputs` | Insert-only output key/reference/SHA-256/evidence identity and producing worker epoch; never artifact bytes. |
| `runtime_mission_checkpoint_bindings` | Plan/input/policy/budget identity plus server-derived task state version and checkpoint/resumed epochs. |
| `runtime_mission_joins` | Hash-addressed snapshots of explicit `pending`, `partial`, `failed`, or `succeeded` parent join reports. AP-007 stores its `mission_recovery` reports in the same table. |

All mission rows use foreign keys into the v3 execution spine. Plan versions are
contiguous and immutable. Creating a successor marks the prior active version
`superseded` and fences its active worker epochs; no prior tasks, checkpoints,
outputs, transitions, or reports are deleted.

## Plan creation and dependency scheduling

`create_mission_plan` validates the complete graph before a write:

1. mission/task/output keys are bounded and normalized;
2. task keys and directed edges are unique;
3. every edge names existing nodes;
4. every edge-required output was declared by its prerequisite; and
5. Kahn traversal visits every node, otherwise the cycle is rejected.

The canonical graph digest covers the mission ID, version, root, deterministic
runtime-task IDs, ordered task/output contracts, and sorted edges. Inputs are
canonicalized and hashed without persisting their potentially sensitive bytes.
The root budget identity hashes immutable limits and the verification contract,
not mutable usage counters.

`list_mission_readiness` reports each node's durable state, blocking and failed
prerequisites, and missing edge outputs. `claim_ready_mission_task` repeats those
checks inside `BEGIN IMMEDIATE`, revalidates plan/input/policy/budget identity,
checks current root cancellation/exhaustion, closes only expired leases, and
atomically transitions the node to `running` with a new monotonic epoch. It never
runs the work itself.

## Checkpoint and restart protocol

A mission checkpoint covers the canonical payload and evidence-reference digest
from the existing checkpoint store. The same transaction adds its mission binding:

- mission ID, plan version, and graph digest;
- canonical input digest;
- AP-004 session and global policy revisions;
- root budget/verification-contract digest;
- current task state version; and
- current positive worker epoch.

The checkpoint store independently verifies that the task belongs to the active
plan, the root budget digest is still current, and the epoch is live before it
advances `runtime_tasks.checkpoint_ref`.

On startup/recovery, an active mission node without a live lease is handled
separately from legacy root-owned milestones. Read-only orphan actions become
failed; consequential in-flight actions become uncertain. A valid bound checkpoint
is marked interrupted and the node pauses. Missing/tampered/mismatched checkpoints
or unresolved consequential effects block the node instead of restarting work.

`prepare_mission_task_resume` accepts only a paused/failed node with the expected
state version and its latest intact interrupted checkpoint. It revalidates all
binding fields, requires the task's epoch to still equal the checkpoint epoch,
rejects an active/superseding worker, and then atomically:

1. creates the sole next worker epoch;
2. records that resumed epoch on the checkpoint binding;
3. marks the checkpoint resumed;
4. transitions the task to running; and
5. returns `checkpoint.step + 1`.

A stale plan, input, session/global policy revision, budget contract, state
version, checkpoint, or worker epoch denies rather than silently refreshing.

## Completion and joins

`complete_mission_task` is the only mission-aware completion path. In one write it
checks the active binding, dependency state/outputs, exact expected state version,
live worker ownership and lease, unresolved effects, and the exact required-output
set. It then inserts output identities, releases the epoch, and records the
completed transition. Existing output rows are never overwritten.

Join evaluation reads durable task state and output rows after restart. Every node
has one explicit outcome:

- `completed` — task completed and all declared required outputs exist;
- `missing_required_outputs` — task state says completed but its contract is not
  fulfilled;
- `failed` or `cancelled` — explicit terminal failure; or
- `incomplete` — pending, running, paused, blocked, or waiting.

The aggregate is `succeeded` only when every node is completed with its required
outputs. Some completed plus any other outcome is `partial`; terminal failure or
missing output with no completed nodes is `failed`; untouched/in-flight work is
`pending`. Root `complete_task` computes the report again inside its final
transaction and cannot succeed on a partial, failed, pending, or missing-output
join.

AP-007 adds a mission-scoped stop, bounded limits and a recovery report on top of
these plans without changing the schema. A stopped plan is `cancelled` or `failed`,
and no new plan is accepted on its root task until the stop is settled. See
[Bounded mission execution](bounded-mission-execution.md).

## Migration, privacy, and rollback

`RUNTIME_SCHEMA_VERSION` and application `SCHEMA_VERSION` are 4. Upgrading a
populated v3 store uses the established owner-only online snapshot, integrity
checks, atomic DDL/version write, rollback, and verified restore path. Tests cover
preservation of user `runs`, runtime tasks, and checkpoints, plus an injected v4
failure that leaves the database at intact v3 with no partial mission tables.
An older target version rejects a v4 store before mutation; there is no reset or
downgrade fallback.

Mission plans, input hashes, output references, checkpoint bindings, and joins are
security/integrity trails and are excluded from `history_privacy.export_history`.
Rollback is a source rollback only while retaining the v4 database for a v4-aware
binary. A pre-v4 binary must not be pointed at the newer store; restore an explicit
pre-upgrade backup only through the documented maintenance procedure.

## Offline evidence

Focused AP-005 cases live in:

- `tests/test_durable_task_state_machine.py` — cycles, dependencies, required
  outputs, partial/failed/successful joins, restart recovery, every resume binding,
  stale worker fencing, plan supersession, and parent false-completion refusal;
- `tests/test_runtime_persistence.py` — populated v3-to-v4 preservation,
  injected-fault rollback, and older-binary future-version refusal;
- existing workflow/checkpoint/storage/privacy/standalone suites — regression and
  generated-artifact coverage.

These tests use temporary local SQLite stores and no network/provider/model. They
do not claim installed-runtime, human, independent-security, screenshot, signing,
or release approval. See [ADR-0039](adr/0039-durable-versioned-mission-graphs.md).
