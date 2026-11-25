# AP-027 — bounded refinement, incumbent preservation and stopping

A refinement loop is the most agreeable feature a system can have and the easiest to make useless.
Ask a reviewer to improve something and it will; ask again and it will again. Nothing in that loop
ever asks whether the output got better, because the question was never given a place to be answered.
Six rules here give it one. The module is a frozen state machine — `RefinementSession.apply()` returns
the next state — with no clock, no model call and no socket.

## 1. The unchanged output is a candidate, always

`RefinementSession.start()` requires an **incumbent**: the output that already exists is the baseline
every round is judged against, and it is the accepted version until something beats it.

Each round's ballot is checked before anything is scored:

- the incumbent appears **exactly once** (`SelectionRefused` otherwise) — a ballot of new candidates
  only can answer "which new one differs?", not "should we change at all?";
- a "revision" whose `content_digest` equals the incumbent's is **refused**, because an unchanged
  output is the incumbent and counting it as a change is how a no-op round reports a win;
- the **currently accepted** version must be on the ballot every round, so a loop cannot quietly stop
  defending what it accepted and drift forward;
- every entry needs a verdict; an unjudged candidate is a preference.

## 2. Independence is recorded, and self-grading is refused

`ReviewerContext` names the model family, the session and the rubric digest behind a verdict — an
opinion with no provenance cannot be checked for independence later.

A verdict from the session that **authored** the variant raises `ComparisonRefused` ("self-grading is
not a review"). A comparison between variants needs verdicts from at least two sessions: one session
judging both sides is one opinion counted twice.

Every round records its `shared_model_bias` state, and there are only two states:
`possible` and `mitigated_by_independent_context`. There is deliberately **no `none`** — a comparison
run and judged on one model family cannot certify itself unbiased, so the most it can say is that
someone else was also in the room.

## 3. Hard requirements are not tradeable against style

`Requirement` is hard **by kind** — `factual`, `functional`, `safety` — rather than by a flag someone
may forget to set. A variant that fails one is ineligible and its style score is never consulted, so
no style score, however close to 1.0, can rescue a failed safety check. A missing result for a hard
requirement is also not a pass: the variant is ineligible with the reason named.

`RequirementResult(passed=True)` without evidence raises `EvidenceRefused`. A failed requirement needs
no evidence — the failure is its own finding — but an unsupported pass is the exact shape a fabricated
compliance report takes once it is written down.

## 4. One budget, counted across every reviewer and every variant

`RefinementLimits` bounds **rounds**, **reviewer calls** and **tokens** for the whole session. Three
reviewers scoring four variants costs twelve calls, and that is the number checked — before the round
is scored, so a refused round records nothing and leaves the session unchanged.

`min_improvement` is a required margin (default 0.02). Without it, a score that moved by 0.001 because
the rubric was re-read becomes "better" and the loop never stops moving. A candidate that clears every
hard requirement but not the margin keeps the incumbent, and the reason says so.

`no_progress_rounds` may not exceed `max_rounds`: a plateau rule that can never trigger is dead code
that reads like a safeguard.

## 5. Stop when evidence does not justify change; preserve the best accepted version

`stop_decision()` returns one of `goal_met`, `no_progress`, `budget_exhausted`, `rounds_exhausted`,
`capability_gap`, `insufficient_evidence`. `change_justified` is true **only** for `goal_met` and only
when the accepted version differs from the incumbent — so a stop of any other kind cannot be recorded
as a justification for an edit, and `StopDecision` raises if a caller tries.

The preserved version is always the **accepted** one, never the most recent candidate: a round that
lost does not replace the version that won.

## 6. Insufficient capability is an honest partial result

`Escalation` requires a concrete missing `capability`, why it was needed, the evidence that shows the
gap, and a **named human** (`owner:`/`user:`/`reviewer:`). An escalation to "the team" is refused as
a note nobody reads, and an escalation without evidence is refused as a complaint.

Once one is recorded, or if the accepted version failed a hard requirement in a later round, the
report's `status` is `partial`, and `claim_success()` raises `FabricatedSuccessRefused`. A partial
result is an acceptable outcome; presenting it as delivered is not.

## What the report says

`RefinementReport.to_mapping()` carries the incumbent, the accepted version, the limits, every round
(with per-variant eligibility, blocking reasons, bias state and spend), total reviewer calls and
tokens, the stop decision, the escalations, the undemonstrated hard requirements,
`hard_requirements_traded_for_style: false` and `weights_changed: false`, plus the measured
`wall_seconds` the caller supplies — this module never reads a clock. `fingerprint()` is stable across
identical sessions.

## What this story does not claim

- **No route or orchestrator consumes `refinement_bounds.py` yet.** Contract with no surface.
- **No model was called.** Verdicts, style scores and token counts are values the caller supplies;
  `tokens_spent` is declared per round, so a caller that under-declares its spend cannot be caught
  here and this is recorded rather than hidden.
- **Bias is recorded, never eliminated.** A `mitigated_by_independent_context` round is a weaker
  claim than it looks: it says a second family was present, not that it was competent.
- **This bounds one session, not a deployment.** The caller's existing `ExecutionBudget` remains the
  outer control plane.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_refinement_bounds.py` | 45 passed |
| mypy (98 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
