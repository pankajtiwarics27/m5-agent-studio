# LC08 local-stream consumer-close audit — bounded follow-up

## Scope and coverage accounting

This increment covers consumer cancellation of the existing local oMLX-compatible
HTTP stream, specifically closure of its client-owned response body. The test
uses `httpx.MockTransport` and stubs the online check; it does not connect to or
activate an oMLX runtime, daemon, provider, or hardware. The change does not
alter runtime selection, admission policy, request payloads, response semantics,
or release/acceptance status.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Fourteen owners have bounded evidence;
**15 remain inventory-only**. `stream_any_model` remains inventory-only because
this increment checks only its legacy local-stream delegation, not its complete
coordinated, combo, cloud, and local behavior. This is not comprehensive
workflow or permission coverage. No runtime activation, hardware qualification,
release acceptance, test8, or LC09–11 status advances.

## Reproduced failure and bounded behavior

The unchanged-source regression failed: after receiving the first content event,
closing `stream_any_model` returned while the mock HTTP body was still open. The
local branch delegated to `_stream_omlx_completion` with `async for` but did not
explicitly close that iterator when its consumer stopped early.

The local branch now owns the delegated iterator with `contextlib.aclosing`
inside the existing heavy-model-slot context. Consumer cancellation therefore
closes the source generator and its client-owned HTTP response body as the
`stream_any_model` iterator unwinds. The regression makes no assertion about a
real runtime's provider-side work or physical resource release.

## Executable evidence and limits

[`test_workflow_local_stream_close.py`](../tests/test_workflow_local_stream_close.py)
provides a synthetic local SSE frame through `MockTransport`, stubs the online
probe to avoid runtime access, consumes one content event, closes the stream,
and verifies that the body is closed before close returns. No external service,
model, daemon, or runtime is used.

The unchanged-source baseline was **1 failed**. After the bounded fix, the local
closure regression and focused local/provider stream and dispatch regressions
pass. This does not qualify completion/error paths across every local request,
coordinated runtime behavior, physical unload, or other inventory-only owners.
