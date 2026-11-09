# Bounded audit: legacy unload-other-models acknowledgments

## Scope and coverage accounting

This increment audits `_unload_other_omlx_models`, a legacy best-effort helper
that queries the local model inventory and posts unload requests for other
loaded, nonvirtual models. The helper remains denied in coordinated mode. Tests
use an in-process `httpx.MockTransport`; they make no real provider, network,
runtime, daemon, or hardware request.

The static owner matrix remains **29 owners / 54 sites**. Nineteen owners have
bounded evidence and **10 remain inventory-only**. The added evidence is
strictly about the helper's local HTTP behavior; it does not establish overall
workflow coverage, runtime activation, installed-native behavior, hardware
qualification, release acceptance, test8, or LC09–11 status.

## Behavior checked

- Only loaded, nonvirtual models other than the retained model are requested
  for unload.
- Only a 2xx unload response is included in the helper's returned ID list;
  redirects and non-2xx statuses do not count as acknowledgments.
- A later request failure does not erase earlier acknowledged IDs; existing
  best-effort exception suppression is retained.
- Async cancellation is propagated rather than swallowed.

The return list and its legacy `unloaded_models` consumer field describe
**HTTP acknowledgments only**. A 2xx response does not prove that physical
resources were released, nor does it report an observed post-unload residency
state. The helper's name and existing consumer field are retained for
compatibility; do not treat either as verified runtime state.

## Evidence and limits

[`test_workflow_unload_other_models.py`](../tests/test_workflow_unload_other_models.py)
uses fake responses to exercise success, server error, redirect, partial failure,
and cancellation. The coordinated-mode denial remains covered by
[`test_runtime_dispatch_boundary.py`](../tests/test_runtime_dispatch_boundary.py).
This evidence does not qualify unload completion, resource release, admission
safety, or any runtime or release boundary.
