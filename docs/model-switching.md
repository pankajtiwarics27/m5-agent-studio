# Per-task model switching

AS-052 defines `switch_model()` in
[`model_switching.py`](../model_switching.py). A target model must satisfy the
same explicit task and hardware capability requirements used by AS-051 before
it can replace the current model.

The workflow context is preserved as the newest fitting messages within the
target context limit. Any truncation is reported in `SwitchDecision` with a
count and reason; no silent context loss occurs. The function performs no
provider call, model load, network fallback, or workflow mutation.

## Evidence

`tests/test_model_switching.py` covers compatible switching, context
preservation, auditable truncation, incompatible-target rejection, and invalid
switch inputs.
