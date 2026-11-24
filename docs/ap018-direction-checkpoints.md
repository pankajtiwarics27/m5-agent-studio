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

## What this does not do

* No UI. The sample panel and the focused question belong to the integrated surface and
  are not drawn here.
* No rendering to the owner, no approval recorded, no benchmark created. AP-018 decides
  *whether* to ask; it does not ask, and it cannot approve.
* No new persistence, no new control plane: it composes with `workflow_state`'s existing
  `waiting_approval` and the checkpoint record rather than adding either.
* No model, network, clock or file access — asserted by a test that greps the module.

## Evidence

`tests/test_direction_checkpoints.py`, 15 tests, covering each trigger, the ordering
refusal, every rejection path, the constants, resume with a stale answer, and the purity
invariant.
