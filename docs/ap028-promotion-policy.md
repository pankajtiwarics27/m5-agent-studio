# AP-028 — promoting and rolling back improvements by risk and evidence

Everything before this story bounds a *proposal*. This is the story where a proposal becomes
behaviour, which makes it the one place where a wrong answer is not contained. Five rules, each of
which already exists somewhere as a quiet failure.

The module reuses the repository's own promotion vocabulary rather than inventing a second one: the
four classes, their review boundaries and the automatic-promotion flags come from
`product/M5_Autonomous_Production_Operating_Envelope.json` through
`autonomous_production_decisions.load_operating_envelope()`, and requested capabilities come from
`scoped_grants.SG_CAPABILITY_TAXONOMY`.

## 1. The classifier is not the proposer's

`ChangeEffects` has no `risk`, `confidence` or `severity` field. It holds what a caller had to
*observe*: the paths touched, the capabilities requested, the egress hosts, the accounts, the spend,
the sensitive defaults, the executable artifacts, and whether it changes retrieval or prompting. A
proposer that can label its own risk has already been trusted with the answer.

`classify_change()` maps those effects onto the envelope's classes and **only escalates**: authority
controls → `authority_evaluator_or_runtime`; executable behaviour or execution capability →
`executable_skill_or_adapter`; retrieval/prompting → `project_lesson`. An unrecognised capability or
authority-control kind is refused rather than read as harmless, and every escalation reason is part of
the record.

The *ceremony* is sized by the change's strongest effect, not only by its class, using the same
escalate-only helper. A retrieval change that also ships an executable needs `owner_and_security`,
not `owner` — erring toward the stricter boundary is the only direction this code may err in.

## 2. Automatic promotion is off because the owner said so

Owner decision U05 (2026-11-17) declined the conservative `project_lesson` class the proposal offered:
all four classes carry `automatic_promotion=false`, and enabling one needs a new owner decision
recorded *before* the flag flips. `load_policy()` reads those flags instead of hard-coding `False`, so
the owner's decision can change without a code change and no code change can enable a class.

If a class were ever enabled, it would still need **approved thresholds and relevant held-out
results**: `HeldOutSummary` ties the results to the candidate digest and the evaluator digest, requires
a named human to have approved the thresholds, and treats an empty run as supporting no conclusion.
`approval_type="automatic"` is constructible only so the refusal can name it; it approves nothing.

Two-party boundaries need two signatures from two people: an `Approval` refuses a review role recorded
under an owner name (`owner_and_security` needs `security:` or `independent:`), so one person cannot
sign both sides.

## 3. New authority is not a quality improvement

`egress`, `accounts`, `spending` and `sensitive_defaults` are gated effects. A request that introduces
one is refused unless an existing grant names *that* effect — and the refusal says so in the same
sentence the reader needs: an owner's approval of a promotion is not a grant for a new egress host,
account, spend or default. A grant for one effect does not cover another.

## 4. A candidate cannot edit the exam it passes

`AUTHORITY_CONTROL_KINDS` is a closed set (evaluator, approval policy, sandbox policy, audit log,
promotion policy). Touching one escalates to `independent_and_owner` **and** cannot be promoted
straight to a narrow stage: it must pass a canary that measures it against the controls it edits.
AP-027 already refuses an optimiser that edits its held-out set *during* a trial; this refuses the same
edit arriving as the proposal.

## 5. A canary is what makes a success a deployment

`run_stages()` walks `task` → `project` → `canary` → `default` in order and re-checks the request's
refusals at the moment of promotion, because that is the moment that matters. A stage with `tasks == 0`
is refused as unobserved — the most reassuring-looking way to deploy something untested is a canary
with no traffic.

A regression must either name the downstream artifacts it withdraws or state, as a separate claim,
that none existed: silence is indistinguishable from an oversight. It stops the promotion at the
failing stage, records a bounded rollback to the recorded target, and marks the candidate's prior
acceptance superseded. A rollback aimed at a version that is not the one deployed is refused, because
an undo of something that is not running undoes nothing.

## 6. Nothing is overwritten

`PromotionLedger` is append-only and frozen: `append()` returns a new ledger and the old one remains
valid. Every digest that was ever active stays recoverable, re-using a record id is refused, a
replacement promotion must name what it supersedes, and there is no `replace`, `delete`, `update` or
`overwrite` method anywhere in the module (asserted by a test).

`artifact_state()` returns `invalidated` or `unmentioned` — never `valid` — so a caller cannot read
silence as a pass.

`PinRegistry` keeps in-flight tasks on the version they started with: a promotion changes what *new*
work gets, `version_for()` returns the pin if a task has one, and re-pinning a running task is refused
as the mid-flight migration this rule forbids.

## What this story does not claim

- **No route or orchestrator consumes `promotion_policy.py` yet.** Contract with no surface.
- **No promotion was performed.** Every record here is a fixture; the repository's active promotion
  policy (U05, all classes manual) is read but not exercised against a real candidate.
- **The owner-approval ceremony is simulated**, not performed: `Approval` records name
  `owner:`/`security:`/`independent:` strings supplied by the caller. This module enforces *shape*
  — who must sign, for what digest, at what boundary — not the authenticity of the signer.
- **Artifact invalidation is recorded, not propagated**: downstream consumers must read
  `artifact_state()`; nothing here rewrites an existing artifact.
- **Stages are values, not a deployment**: `StageResult.tasks` is declared by the caller, and this
  module cannot tell a canary that ran from one that was described.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_promotion_policy.py` | 65 passed |
| mypy (99 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
