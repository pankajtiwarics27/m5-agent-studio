# AP-029 — governed recurring missions and external delivery

A schedule is a promise to do something again without being asked; an external action is a promise
that leaves the machine. Both accumulate consequences while nobody is watching, and both fail in the
same direction: by doing something twice.

The module reuses the repository's existing outbound vocabulary rather than inventing one —
`side_effect_ledger` already owns action identity, replay policy and the "uncertain" state that
refuses a blind replay, and this module consumes the same states and verdicts.

## 1. A schedule grants only its purpose

`ScheduleGrant` names one project, one cadence, an expiry and a run cap, and its purposes are a set of
specific ones — `draft`, `publish`, `deploy`. A grant for `draft` says nothing about `publish`, and
`permits()` is the only way to ask. A `RecurringMission` cannot exist without a grant; cannot borrow
another project's grant; and cannot have a purpose its grant does not name.

The expiry must be real (`expires_at=0` authorises nothing) and the run cap must be positive, because
an unbounded schedule is a runaway with a calendar entry.

## 2. Draft creation never implies publication

`DraftArtifact` has no `published` field and no method that sends it — `to_mapping()` states
`published: false` and a test asserts the attribute does not exist. The only path from a draft to an
external effect is `requires_external_action()`, and it demands a **separate** grant that carries the
matching purpose. A draft-only mission cannot call it successfully, whatever it produced, and a
`publish` grant does not authorise a `deploy`.

## 3. An external action binds its terms

`ExternalActionIntent` pins the account, destination, content hash, policy revision and expiry. One
changed byte of content, a re-pointed destination or a newer policy revision makes `is_current()` false,
and `changed_since()` names which term moved so the refusal is actionable rather than mysterious.

The idempotency key is **derived**, not optional: without one, a retry after a timeout is a second
publish rather than the same one. An intent's expiry is also capped by its grant's, so authority that
lapses cannot leave a live intent behind.

## 4. Retries reconcile; they never resend blindly

`decide_retry()` is deliberately ordered:

| state | decision |
| --- | --- |
| `prepared` / `dispatched` / `uncertain` with no probe | **reconcile** |
| any unknown state whose probe says `unresolved` | **stop** |
| probe says `verified_committed` | **stop** (the effect landed) |
| probe says `verified_not_applied` **and** the provider is idempotent | **retry** |
| probe says absent, but no idempotency key | **stop** — a retry could duplicate an effect that landed after the probe |
| `committed` / `reconciled_committed` / `abandoned` | **stop** |

"We will retry and see" is not an available decision.

## 5. Restarts, missed slots and revocation do not duplicate effects

A run's identity is derived from the mission, the purpose and the cadence slot, so a second process
waking on the same slot produces the same `run_id` and `claim_run()` refuses it as a duplicate
delivery rather than recording a second one. A run may be finished exactly once.

A missed slot is recorded as `missed` rather than back-filled: firing three days of a daily mission at
once is a burst, not a catch-up. `runs_used()` does not count a missed slot against the grant, so
missing a run neither consumes authority nor invents work.

The ledger also refuses to re-record an action under a *new* idempotency key — the action's terms
changed after approval, which makes it a different action wearing the same name — and refuses to reopen
a committed action as `prepared`.

## 6. Runaway activity stops early

`Governor` holds a global pause, per-project stops and a notification threshold. It is consulted before
a run starts, and a paused system does not depend on every producer remembering to check. A
notification threshold requires a **named recipient**: a threshold nobody is told about is not a
safeguard, and reaching it blocks the next run until the person is told.

## 7. Suggestions are not results

`Suggestion` refuses causal language — "caused", "drove", "led to", "grew revenue" — unless it names the
experiment that measured it. A metric movement is an observation; a business outcome it did not measure
is a story. A suggestion can only ever become a `DraftArtifact`: `changes_public_output` is a constant
`False`, so analytics advice cannot quietly rewrite public output.

## What this story does not claim

- **No route or orchestrator consumes `governed_missions.py` yet.** Contract with no surface.
- **Nothing was scheduled and nothing was sent.** No mission ran, no provider was contacted, and no
  external action exists.
- **The ledger is in memory.** Durability belongs to the existing `DurableActionLedger` and
  `runtime_persistence`; this module defines the rules a caller must apply to them, not a second store.
- **Reconciliation is simulated**: `Reconciliation` records what a probe *reported*; this module
  decides what that report permits, and cannot verify the probe itself.
- **No causal claim is validated**, only refused when unmeasured: a suggestion naming an experiment id
  is not checked to have been run.

## Coverage of the scheduling decision

`RecurringMission.due()` — the call that decides whether work runs at all — originally
had no test: the suite exercised `slot_for()`, `run_id_for()` and the grant, but never
the combination. It is now covered directly, including the two ways it must say no
(a slot already run, and a slot earlier than the last one, so a backwards clock cannot
back-fill) and the two ways the grant overrides a fresh slot (run cap and expiry).
`Reconciliation` likewise gained coverage for the `unresolved` verdict, which had been
reachable but unexercised. Both are mutation-verified: breaking the slot dedupe and
ignoring the grant each fail the suite.

`due()`'s purpose check is unreachable by construction — the constructor refuses a
mission whose purpose its grant does not name — so the test asserts the construction
refusal rather than a branch that cannot run.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_governed_missions.py` | 77 passed |
| mypy (100 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
