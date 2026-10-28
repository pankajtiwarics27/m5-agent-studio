# Model quality and compatibility comparison

AS-056 defines `EvalTask`, `EvalConfig`, `score_evaluation()`, and
`compare_evaluations()` in
[`model_comparison.py`](../model_comparison.py). It turns executor-produced
measurements into comparable, reproducible records on top of the AS-017
registry and AS-051 negotiation. Per the roadmap's model benchmarking
safeguards, comparison results hard-filter by compatibility first and rank
by quality second; timing and throughput measurement belong to the
calibration/profile stories, not here.

## Versioned tasks, reproducible configs

An `EvalTask` pins `task_id`, `task_version`, an AS-051 `TaskRequirements`
object, and a weighted `RubricDimension` set (unique names, positive finite
weights, canonicalized by sort order). Its `digest` is a SHA-256 over the
canonical mapping, so "the same task" means the same versioned definition —
weights swapping owners or a version bump changes task identity.

An `EvalConfig` pins the reproducible knobs a rerun must repeat
(`max_output_tokens`, `temperature` in [0, 2], optional `context_length` and
`seed`, `concurrency`); its `digest` covers exactly those fields. The
optional `environment` mapping (machine, OS build, runtime version — free-form
disclosure) is validated, normalized, and reported on comparison but
**excluded** from the digest, so identical configs remain comparable across
machines while every environment divergence is disclosed, never averaged away.

## Scores

`score_evaluation()` accepts raw per-run values for each rubric dimension —
nothing less (missing dimension → rejected; unknown dimension → rejected;
values outside [0, 1] or non-numeric → rejected). `DimensionScore` derives
median and spread from the run values inside the value object; caller-supplied
aggregates are recomputed, so a dishonest median cannot survive
construction. The weighted `overall` is a property derived on access from the
dimension medians and task rubric — it cannot drift from its inputs. The
model's compatibility reasons (plus optional hardware) are recorded on the
score, not raised: an incompatible model may still hold a quality score, and
comparison filters it. Every score exposes a full JSON-safe `to_mapping()`
(task mapping + `task_digest` + config mapping + `config_digest` + model
descriptor mapping + runs) and a `digest` pinning the entire record; an
optional `inputs_digest` pins the exact prompt set.

## Comparison

`compare_evaluations(scores, *, hardware=None)` refuses rather than fudges:
mixed task versions, multiple config digests, duplicate model identities, or
non-`EvalScore` entries all raise. Compatible survivors are hard-filtered
through the AS-051 negotiator (task requirements and, when supplied, the
hardware profile's memory and capability constraints) with their exact
reasons in `excluded`, and the remainder is ranked by overall descending with
`(model_id, digest)` tie-breaking — deterministic and order-independent for
identical inputs, including JSON mappings.

No model execution, provider call, benchmark run, or network access occurs
anywhere in this module; it is a pure validation and accounting layer that
keeps the benchmark stories (AS-059 onward) honest by construction.

## Evidence

`tests/test_model_comparison.py` (15 tests) covers task digest canonicality
(order-normalized, weight-swaps and version bumps change identity), task and
config fail-closed validation, environment disclosure excluded from the
config digest and accepted in both dict/pair shapes, median/spread/weighted
overall arithmetic, derived-aggregate tamper resistance, dimension mismatch
and range rejection, recorded-but-not-raised compatibility, ranking order,
hardware memory filtering, environment-difference reporting (silent when
identical), refusal of incomparable or malformed comparisons, deterministic
tie-breaking, and score-digest sensitivity to model identity and input set.
