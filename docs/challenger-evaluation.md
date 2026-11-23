# AP-016 — Reproducible challenger and workflow evaluations

**Status:** delivered for the AP-016 scope; **offline and advisory only**. It
**runs no model, workflow, provider or network** and wires no production caller.
It is **not activated** and **not a production guarantee**.

The objective names its own trap: *compare models, tools, prompts and teams
**without self-grading or hidden budget differences**.* Three failures make an
evaluation look rigorous while meaning nothing — a challenger scoring its own
output, a scorer that returns zero when it breaks, and a budget that quietly
favours one arm. Each is structurally impossible here rather than discouraged.

## Every run is pinned and every failure is kept (AC1)

A `RunRecord` cannot be built without exact **task, config, tool, model,
environment and toolchain** digests. A failure is the *same record* with an
outcome and a reason, so it cannot be dropped by filtering on success.

`StrategyRuns` requires `attempted == len(records)`. A caller who ran ten and
kept eight is caught by arithmetic, not by trust:

```
strategy 'specialists' attempted 3 runs but recorded 1; failed runs must be
recorded, not dropped (AC1).
```

## Budgets are disclosed, and unequal ones are a refusal (AC2)

`compare_budgets` compares tokens, cost, latency **and** interventions against a
stated tolerance. Exceeding it on any dimension makes the comparison
non-comparable:

> budgets differ beyond tolerance in max_tokens; a comparison under unequal
> budgets is not a comparison (AC2).

"The multi-agent strategy won" is not a finding when it had ten times the tokens.

## A model score never stands alone (AC3)

`ScoredOutcome` refuses to construct with no evidence at all. Even with evidence,
`is_justified` requires **both** a deterministic check **and** independent
evidence (`factual_verification` or `user_judgement`). A model's own opinion is
not an independent kind.

A check that *fails* disqualifies the score outright — a high score sitting next
to a failing check is the most dangerous shape this record can take.

## The no-op incumbent and held-out tasks always participate (AC4)

`EvaluationDesign` refuses a design with no `held_out` task (a challenger tuned
on its own cases has proved nothing) and refuses one with no `NoOpIncumbent`
(a design without a control cannot separate improvement from activity). And
`build_report` refuses a result set that names the incumbent only in the design
but never in the results.

## Scorer failure, missing evidence and stale hashes fail closed (AC5)

There is **no default score**. `run_scorer` returns either an outcome or a
`ScorerFailure`, never both:

- a scorer that raises → `failed`, with the exception text;
- a score with no justification → `missing_evidence`;
- an out-of-range score → `missing_evidence`.

A failure reports `contributes_a_score: false` and `counted_as_zero: false`,
because turning "we could not measure this" into "0.0" is how the failure the
criterion names actually happens. Any scorer failure makes the whole report
`is_trustworthy: false`, and `require_trustworthy` **raises** rather than warning.

`verify_source_pins` raises if a pinned file hash changed or vanished: a stale
hash silently attached to a report is worse than a refused report.

## Variability, cost, interventions and no promotion on a fake score (AC6)

`Variability` reports **median, spread, min, max** — never a mean alone
(`mean_only: false`). `measure_metrics` carries latency, cost and intervention
variability alongside quality, so a challenger that wins on quality by needing
six human interventions is visible.

`refuse_promotion` raises unless the candidate was independently verified *and*
named who verified it, and — when a report is supplied — unless that report is
itself trustworthy. There is no code path from an unverified score to a
promotion, which is what "never promote on a fabricated score" has to mean.

## What this story does not decide

- **It does not execute anything.** Runs and scores are values the caller
  supplies; the injected scorer is the only callable and it scores a record the
  caller already produced.
- **It does not grade quality itself.** A deterministic check's result is
  recorded, not performed here; `verification_evidence` owns real test evidence
  and `model_comparison` owns rubric scoring.
- **It does not choose a winner.** It reports comparable, variability-aware
  results and refuses to publish the ones whose gates failed.
- **It does not prove general superiority.** `render_report` emits a
  `does_not_prove` block: trustworthy means the gates held, not that the
  challenger is better on tasks nobody ran.

## Reviewability

`render_report` is deterministic sorted-key JSON with a trailing newline and an
`evaluation_rules` block stating every boundary (self-grading, unevidenced
scores, scorer-failure-as-zero, hidden budget differences, omitted failures and
unverified promotion are all `false`). Reports carry stable digests.

## Verification

```sh
python -m pytest -q tests/test_challenger_evaluation.py
```

54 tests, fully offline. Every run and score is synthetic; none is presented as
a measurement of a real model or workflow.
`runtime_activation_authorized` remains `false`; ADR-0040–0045 remain
`not-reviewed`.
