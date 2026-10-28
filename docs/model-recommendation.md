# Adaptive model recommendation engine

AS-062 defines [`model_recommendation.py`](../model_recommendation.py), a
pure advisory contract that ranks exact AS-017 model builds for a task. It
composes AS-051 capability negotiation, AS-058 routing policy and consent,
and AS-060 keyed performance profiles. It neither loads nor dispatches a
model, performs fallback, reads the profile vault, nor contacts a provider;
callers pass already-resolved records and remain responsible for execution.

## Feasibility and privacy

A candidate is eligible only when its task capabilities and declared memory
fit the supplied `HardwareProfile`, a matching explicit route context passes
`evaluate_fallback`, and any supplied budget can be evaluated. A missing route
context is fail-closed as a privacy exclusion: the engine does not infer that
a provider is local or safe from a model's name. Network and billable routes
continue to require both operator policy and per-request consent with an
identifier. Measured warm memory is also compared with available memory when
both values are known.

Budget checks require an explicit `RecommendationBudget` wrapping the
existing `execution_budget.BudgetLimits` (in particular its `max_cost`), plus
a currency, estimated request tokens, and a caller-supplied AS-061
`CostObservation` for each candidate. The estimated token count is checked
against `BudgetLimits.max_tokens`, while the cost ceiling comes from
`BudgetLimits.max_cost`. `ExecutionBudget` remains the owner of runtime
enforcement; recommendation only performs an advisory estimate. The engine
calculates
`amount_per_million_tokens * estimated_tokens / 1,000,000`; it does not
invent a price, exchange rate, or token estimate. If the price is absent or
in another currency, the candidate is not presented as budget-feasible.
Without a budget, prices do not affect eligibility.

## Benchmark confidence and comparability

A profile is trusted only if its content agrees with its `ProfileKey`, it
belongs to the candidate's exact model digest, and its hardware digest and OS
match the caller's current hardware snapshot. Profiles compared together also
share a hardware/OS/methodology anchor; a mismatch is reported as
incomparable and confidence is unknown rather than mixing measurements.

Confidence uses the profile's warm `decode_tokens_per_s` and
`peak_memory_bytes` aggregates. The sample count is the smaller metric count;
relative spread is the larger `spread / abs(median)` across those metrics.
Confidence is high only at three or more samples, relative spread at most
0.5, and profile failure rate at most 0.25. Missing aggregates are low
confidence; a missing or untrusted profile has unknown confidence. Eligible
models rank ahead of excluded candidates, then high, low, and unknown
confidence, followed by spread, failure rate, and stable model identity.
These thresholds are deterministic heuristics, not claims of statistical
significance or a guarantee of runtime performance.

## Manual selection

`manual_override(descriptor)` returns the explicit `ModelDescriptor` without
consulting recommendation output, capability feasibility, consent, or budget.
That separation is intentional: the engine is an advisory view, not an
execution gate. Any actual run must still pass through the caller's normal
capability, privacy, consent, and execution controls; override does not grant
permissions or bypass those controls.

Results are deterministic canonical mappings with `sha256:` digests. The
module stores no timestamps, prompts, secrets, or benchmark data; it receives
profile values in memory and writes nothing.

## Evidence

`tests/test_model_recommendation.py` exercises exact confidence arithmetic
and stable ordering, capability and memory refusal, fail-closed privacy,
missing-price and budget boundaries, context mismatch, low/unknown confidence,
manual override independence, and malformed inputs. Tests use synthetic
profiles and prices only; they do not claim live-runtime performance, real
provider pricing, or external review.
