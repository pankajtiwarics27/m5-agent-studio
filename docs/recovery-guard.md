# Bounded recovery and stagnation detection

Status: implemented in `M5-P2-05` (2026-10-30), decision recorded in
[ADR-0009](adr/0009-durable-recovery-memory-and-stagnation.md). This page
documents observed behaviour of the shipped code: what the runtime remembers about
failed attempts, how it decides that no progress is being made, and where the
guarantees stop.

## Why this exists

The runtime already bounds total work per task (steps, tool calls, tokens, elapsed
time, recovery attempts) and refuses to complete a coding task without
authoritative evidence. Both are necessary and neither is sufficient: a loop can
spend its entire step budget on the *same* failing strategy, or oscillate between
two patch states, and every attempt looks like new work because nothing recorded
what the last attempt observed.

`M5-P2-05` adds the missing memory. Each verification, recovery and rollback
attempt stores what it saw — source revision, transcript hash, failure fingerprint,
failure code and per-file digests — so the next attempt can be compared with it,
and a repeated or revisited state can be refused instead of retried.

## Failure memory

`recovery_guard.py` (standard library only, inlined into `studio.py`) keeps one
document per task at `<harness store>/recovery/<task_id>.json`:

```json
{
  "schema_version": 1,
  "task_id": "hns-1a2b3c4d",
  "budget": {
    "limits": {"max_retries": 3, "max_replans": 3, "max_rollbacks": 3, "max_identical_attempts": 2},
    "retries_used": 1,
    "replans_used": 0,
    "rollbacks_used": 0,
    "pause_requested": false,
    "pause_reason": ""
  },
  "attempts": [
    {
      "sequence": 1,
      "kind": "verification",
      "outcome": "failed",
      "attempted_at": 1793400000.0,
      "evidence": {
        "revision_digest": "…64 hex…",
        "transcript_sha256": "…",
        "failure_fingerprint": "…",
        "failure_code": "verification_test_failed_introduced",
        "failure_ids": ["tests/test_calc.py::test_add"],
        "environment_digest": "…"
      }
    }
  ],
  "updated_at": 1793400000.0
}
```

Two digests are derived from every snapshot:

| Digest | Covers | Used for |
|---|---|---|
| `conditions_digest` | source revision + environment | judging a *recovery* action, which has no transcript yet |
| `evidence_key` | conditions + transcript hash + failure fingerprint + failure code | judging a fresh *verification* attempt |

`failure_fingerprint` is the canonical SHA-256 of the failure code and the sorted
set of failing test ids, so reordered output is the same failure and a different
failing test is not.

## Verdicts

`detect_stagnation` compares a candidate observation with the recorded **failed**
attempts (a verified attempt is progress by definition):

| State | Meaning | Effect |
|---|---|---|
| `first` | no failure has been recorded for this task | allowed |
| `progress` | the observation differs from every recorded failure | allowed |
| `stagnant` | identical to the most recent failure | allowed until `max_identical_attempts` (default 2), then a durable safe pause is requested |
| `oscillating` | the observation equals an *older* abandoned state | escalated immediately as a safe pause |

A verdict that escalates sets `pause_requested` in the recovery document **and**
in the task's durable budget (`runtime_tasks.budget_json`). `pause_requested`
makes the task effectively `paused` at the execution boundary, so the next
consequential tool call is refused until an operator resumes the task — the exact
same mechanism `M5-P2-01` uses for disk-full safe pause.

## Budgets

| Counter | Default | Spent when |
|---|---|---|
| `max_retries` | 3 | a harness turn resumes a task after a failure |
| `max_replans` | 3 | a resumed turn whose latest verdict is `stagnant`/`oscillating` (the strategy has to change, not just be retried) |
| `max_rollbacks` | 3 | `rollback_edits` with `dry_run: false` actually writes |

Consumption happens only after the worker lease is held, so a `409` lease conflict
cannot burn a budget. An exhausted budget is persisted (with
`pause_requested: true`) *before* the refusal is raised, so a crash right after
the refusal cannot lose the exhaustion: the next process still sees the pause.

## Where it is wired

| Path | Behaviour |
|---|---|
| `DurableTaskStateMachine.verify_task_completion` | records every attempt (`verified`/`failed`) with the revision, transcript, failures and environment from the report; attaches the verdict to `TaskVerificationReport.stagnation` |
| `app.py` harness turn, verification failure | an escalated verdict releases the lease into `paused` with reason `stagnation_detected:<state>`, emits `turn/stagnation_paused`, and marks the session paused instead of error |
| `DurableTaskStateMachine.prepare_resume` | raises `WorkflowTransitionRejected` (`resume refused: … Changed evidence or conditions are required …`) when the current conditions digest equals the recorded failure's, records a `recovery_requires_new_evidence` transition, and leaves the task paused |
| `_acquire_turn_plan_and_lease_or_409` | spends `retry` (or `replan` when the latest verdict escalated) once the lease is held; exhaustion is HTTP `409` |
| `rollback_edits` tool | spends `rollback` before writing; exhaustion returns `ok: false` with the reason and writes nothing |
| `GET /api/harness/sessions/{session_id}/task` | returns the read-only `recovery` summary (limits, counters, pause flag, latest verdict, recent attempts) |

## Durability and safety

* One document per task, written with `os.replace` after `fsync` — a reader sees
  either the old or the new document, never a partial one.
* Directory `0700`, file `0600`; a symlinked or non-regular record is refused; a
  record larger than 1 MiB, of another schema version, or belonging to another
  task is refused rather than guessed at.
* Task ids are validated (`[A-Za-z0-9._-]{1,120}`) so a record path cannot escape
  the store.
* History is pruned to the newest 100 attempts; counters are never pruned.
* Per-task writes are serialized by the single-owner worker lease, not by a file
  lock, so two concurrent turns on one task are already impossible.

## Limits

* Memory is keyed by task id. A brand-new session/task starts with no history, even
  in the same workspace.
* `max_identical_attempts` protects against exact repetition. A model that changes
  one irrelevant character in the failure text produces different evidence and is
  only bounded by the retry/replan/recovery budgets.
* The recovery gate is skipped when no conditions can be captured (for example a
  task with no workspace directory): there is nothing to compare, so the recovery
  budget remains the bound.
* An unreadable record is treated as *no history* for admission (the budgets and
  the task budget still bound the loop) but an unwritable record fails a failed
  verification closed with `recovery memory unavailable` and a pause request.
* Pruning eventually forgets very old failures; the counters keep the loop bounded
  after the fingerprints are gone.
