# AP-026 — researching improvements and evaluating candidates

An "improvement lab" is where a system starts changing itself, so this is the story where the quiet
failures are the expensive ones. Each of the seven refusals below has already shipped somewhere, and
each looks like diligence from the outside.

The module is the record and the gates. Fetching is injected through `Fetcher`, trials through a
`trialler` callable; nothing here opens a socket or starts a thread.

## 1. No watcher without a grant

A background job polling a repository every hour looks like attention. It is an outbound request the
owner never approved, made on a schedule nobody chose.

`WatchSchedule` cannot be constructed without a `WatchGrant`. The grant has a **namespaced human
author**, a purpose, an **allowlisted host set** (a subset of `autonomous_production_research`'s
`PUBLIC_READ_ALLOWLIST`), a request cap and an expiry date. `Watcher` has no `start`, no `daemon` and
no loop: a round happens only when a caller says `tick(now=..., fetcher=...)`.

Three refusals follow from that shape: a location outside the granted hosts (the grant names *where*,
not merely *that*), an expired grant, and a round that would exceed the cap — all raised **before**
the fetcher is called, so an over-budget round costs nothing. `assert_no_implicit_egress()` parses the
module's own source and refuses a socket, a thread, a scheduler or a `while True`; it parses rather
than greps because its own text contains the names it forbids.

## 2. A candidate is a version, not a name

`CandidateCard` requires the exact version or commit (this story's own rule is that the literal
strings `latest`, `main`, `HEAD`, `master`, `newest` and `*` are refused as moving targets), the
access date, the licence, and **non-empty limitations**. A candidate with no stated limits is being
recommended rather than evaluated.

A candidate must also live somewhere already permitted: its `source_url` is validated against the
public read allowlist, so "we found it on a blog" is not a research path this programme has.

## 3. An experiment starts from a problem, not an idea

`Hypothesis` names the failing traces it came from, the metric, the measured baseline, the direction
and **the threshold it could fail**. A threshold that does not beat the baseline in the stated
direction is refused as unfalsifiable: a hypothesis that cannot fail is a description of intent.

## 4. Compatibility and resources precede trials

`CompatibilityCheck` is required by `Experiment` and re-checked by `run_experiment` **before the
trialler is called** — a check that runs after the work is a comment. It compares the declared
download against both a cap and free disk, the memory requirement against what is available, and the
runtime presence and licence compatibility. `fits` is derived, so it cannot be asserted.

## 5. The optimiser cannot edit its own exam

`HeldOutGuard` fingerprints the held-out tasks, the evaluator and the authority controls before the
experiment. Any difference afterwards raises, including a **removal** — a deleted control is not an
unchanged control — and a report whose post-trial guard differs recommends `keep_incumbent` with the
reason stated. An optimisation that improves its score by editing the scorer improved nothing.

## 6. Memory-shaped updates only

`OPTIMISATION_SURFACES` is `prompt`, `skill`, `config`. `weights`, `fine_tune` and `rl_rollout` are
refused **by name**, so the refusal can say what was asked for: this programme has no training path
and no budget authority for one. Every report states `weights_changed: false`.

## 7. Candidate generation, judging and inference stay local

A candidate that names a hosted dependency, or is marked `reference_only`, never reaches the
trialler: `run_experiment` raises `LocalOnlyRefused` first. Reference projects that only work against
a hosted model may be read and recorded — that is what `adaptation="reference_only"` is for — and may
not be trialled. A candidate that merely *looks* hosted but names no dependency is refused for the
opposite reason: whether a trial would leave this machine is not a detail to infer.

## What the report says

`ImprovementReport` carries the hypothesis, the surface, the candidate card, the compatibility check,
both guards, **every** trial (including failures), the trial budget, `runs_evaluated`,
`intervention_minutes`, limitations and `blocking_reasons`. `recommendation()` is derived:
`adopt_candidate` only when nothing blocks, the best observed value meets the threshold and at least
one trial passed.

## What this story does not claim

- **No route or orchestrator consumes `improvement_lab.py` yet.** Contract with two injected seams
  and no surface.
- **No research was actually performed for this story.** No repository, paper or model was fetched,
  and no candidate here was evaluated against a real failure trace — the fixtures exercise the rules.
- **No model was trained, fine-tuned or rolled out**, and no improvement was adopted. Adoption is
  AP-028's decision.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_improvement_lab.py` | 45 passed |
| mypy (97 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
