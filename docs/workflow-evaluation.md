# Agent workflow quality evaluation

AS-038 defines [`workflow_evaluation.py`](../workflow_evaluation.py), a
pure aggregation contract for comparing externally measured single-model and
multi-agent runs. Each run is tied to an AS-056 `EvalTask` digest,
`EvalConfig` digest, and hardware digest. `WorkflowRun.from_trace_and_budget()`
can take terminal status from an AS-036 `WorkflowTrace` and elapsed/cost usage
from AS-035 `BudgetUsage`; it copies only the status and opaque trace ID, never
event detail or timestamps. Peak memory and task success remain
caller-measured inputs. The module does not execute workflows or collect
metrics.

## Measurements and comparison

A `WorkflowRun` supplies task success, terminal outcome, latency in
milliseconds, a caller-observed cost amount and currency, peak memory bytes,
and a context-safe run identifier. Both strategies require at least one run;
all task/config/hardware identities, configured environment fields, and cost
currencies must match. Failed or cancelled runs cannot be marked as task
successes. Every metric is required, so absent cost or memory is rejected
rather than represented as zero.

Each strategy reports run count, successful-run count, success rate, and
median, spread, bounds, and count for latency, cost, and peak memory. Deltas
are explicitly `multi_agent - single_model`: a positive success-rate delta
favors multi-agent success, while negative resource deltas mean less latency,
cost, or memory. Medians and spreads summarize the supplied repeated
measurements; they are not predictions or a statistical significance claim.

The engine does not calculate provider prices or exchange rates. Cost values
must come from the caller's measurement/accounting path, and every compared
sample must use the same currency. Hardware, environment, task, and config
mismatches refuse comparison instead of blending incomparable measurements.

## Privacy and determinism

The result is a canonical JSON-compatible mapping with a `sha256:` digest.
It contains task/config/hardware digests, an environment digest, run counts,
aggregate values, and cost currency; it exposes no raw environment values,
prompt text, trace details, or wall-clock timestamps. Run identifiers must be
bounded opaque tokens rather than arbitrary user text. The module performs no
filesystem, model, provider, or network operation.

## Evidence

`tests/test_workflow_evaluation.py` covers exact success-rate/median/spread
arithmetic and strategy deltas, integration with AS-035 budget usage and
AS-036 terminal trace status, order-independent digesting, export isolation,
missing strategy, task/config/hardware/environment/currency incompatibility,
terminal-failure success refusal, and validation of identifiers, units,
measurements, and malformed input. Fixtures are synthetic and do not claim
live workflow performance, actual provider spend, or external review.
