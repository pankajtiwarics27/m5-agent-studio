# AP-018: early direction samples before expensive production

An autonomous builder that runs to completion before showing anything can spend a large
budget on a direction the owner did not want. The opposite failure is quieter: pausing for
approval before every job turns autonomy back into supervision and teaches the owner to
approve without reading. `direction_checkpoints.py` makes that trade-off explicit.

## The decision

`decide()` returns exactly one of three triggers, in this order:

| trigger | pauses production | when |
| --- | --- | --- |
| `uncertainty_exceeds_coverage` | yes | a material uncertainty is present, benchmarked or not |
| `benchmark_covered` | **no** | an owner-approved reference covers this task kind and nothing is escalated |
| `no_benchmark` | yes | no reference covers the task kind |

Material uncertainty is checked *before* coverage on purpose. A benchmark covers what it
measured; a task that diverges from it — a new format, an unsupported interaction, an
audience or scope change, unresolved rights, an external dependency — is outside the
covered surface. Checking coverage first would let the escalation silently never fire.

An uncertainty string outside `MATERIAL_UNCERTAINTIES` is refused rather than escalated.
Free-form strings would make "did we ask too much or too little" unanswerable afterwards,
because nothing could be counted.

## What a pause is not

`DirectionCheckpoint` carries two flags as constants, not constructor arguments:

```
direction_approval_is_final_acceptance:        False
direction_approval_grants_publish_permission:  False
```

A test constructs the dataclass with `True` and expects `TypeError`. The one mistake that
would matter here is a direction "yes" being read later as "ship it", so the type makes
that unrepresentable rather than merely discouraged.

## Refusals

* A check-in with no sample is refused: asking the owner to judge a direction with
  nothing to look at is a prompt, not a check-in.
* A sample that failed its own technical checks is refused: a direction question about a
  broken sample is a defect report wearing a question's clothes.
* A benchmark must pin the digest of the version it covers, so "benchmarked" cannot drift
  to mean "anything we did once".

## Resume, and the stale answer

`resume()` resolves a pause to `continue` or `redirect`, and refuses when the sample
changed after the question was asked, or when the answered artifact is not the sample the
checkpoint showed. That is the same rule AP-019 applies to scoped feedback, enforced here
at the first point a version can drift.

Feedback preserves because nothing is discarded: the caller keeps the artifact through
the existing checkpoint, and the pause is resumable only when a sample is recorded
(`is_resumable_from_checkpoint`).

## Taking the check-in, and answering it

Two routes do the asking, in `app.py`:

| route | effect |
| --- | --- |
| `POST …/harness/sessions/{id}/direction-checkpoint` | asks `decide()`; on a pause, moves the task to `waiting_approval` |
| `POST …/harness/sessions/{id}/direction-answer` | resolves the pause to `continue` or `redirect` |

The checkpoint is stored in the `evidence_json` of the task's own `running ->
waiting_approval` transition, so it lands in the same transaction as the pause, cannot be
left behind by a resume, and is recovered by scanning that log — by a reader that never saw
the writer. No new table and no new state: `waiting_approval` was already in `TASK_STATES`
and the recovery pane already had a story slot for it.

The refusals live here too. A check-in is only taken on a **running** task. A second pause
**cannot stack** over a live one. An answer about a **different version** than the sample
now shows is refused, and the pause survives so the owner can answer the question they were
actually asked. The stored record is revalidated on read, so a forged flag claiming
acceptance raises instead of becoming permission.

## What the pane says

The direction panel is drawn from `GET …/task`'s `direction_checkpoint`, never from pane
state, so a reconnect shows the same question rather than a reconstruction of it. Every
value is escaped text, and the two sentences that matter are printed beside the buttons:

> Approving a direction is not accepting this artifact, and it does not publish, deploy or
> spend anything. Final acceptance is a separate decision, and so is adopting this work as a
> reference.

`scripts/ui-probes/direction-panel.mjs` fails if either sentence is dropped, and a hostile
sample id is proven to become text rather than markup.

## What this does not do

* No approval is recorded and no benchmark is created. AP-018 decides *whether* to ask and
  carries the answer; it cannot accept an artifact or adopt a reference.
* No model, network, clock or file access in the decision module — asserted by a test that
  greps it.
* No runtime activation, no IsolationAcceptance (still unqualified and blocked), no live
  dispatch.

## Evidence

* `tests/test_direction_checkpoints.py` — 15 tests: each trigger, the ordering refusal, every
  rejection path, the constants, resume with a stale answer, purity.
* `tests/test_direction_checkpoints_routes.py` — 18 tests over the real routes: the pause, the
  covered task left alone, every refusal, a refused answer leaving the pause intact, a forged
  record rejected, and no top-level name shared with `app.py`.
* `scripts/harness_direction_walk.mjs` — 22/22 checks in a real browser against
  `scripts/harness_direction_seed.py`, which stages its states through the real route.

## One trap worth recording

The single-file build inlines this module into `app.py`'s namespace. A module-level
`SCHEMA_VERSION` here replaced the application's (4 → 1), so `init_db` created no runtime
tables and every durable-task test failed in `studio.py` while passing against `app.py`.
Module globals are now `DC_`-prefixed, and `scripts/build_studio.py` refuses any inlined
module whose top-level names already exist in the app.
