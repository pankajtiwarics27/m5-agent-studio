# LC08 provider-stream consumer-close audit — bounded follow-up

## Scope and coverage accounting

This audit covers deterministic cleanup when a consumer closes the existing
OpenAI-compatible or Anthropic provider stream after receiving output. It does
not change provider selection, request payloads, error policy, or streaming
semantics. The regression uses only `httpx.MockTransport`; it makes no real
provider/network request and performs no runtime or hardware qualification.

The [static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
contains **29 owners / 54 static sites**. Twelve owners have bounded evidence;
**17 remain inventory-only**. This remains a bounded owner audit, not
comprehensive workflow, provider, or permission coverage. No runtime activation,
hardware qualification, release acceptance, test8, or LC09–11 status advances.

## Reproduced failure and bounded behavior

The new test first failed for both provider wires: closing the public
`stream_any_model` iterator returned while the underlying mocked HTTP body was
still open. The provider stream was nested under several async-generator layers,
none of which explicitly closed its delegated iterator on consumer cancellation.

The provider adapters and dispatcher now close each delegated async iterator
with `contextlib.aclosing`. That cleanup propagates through error normalization
and the provider adapter to the HTTP stream context, so the owned response body
is closed before `stream_any_model.aclose()` returns. The change is deliberately
limited to closure propagation; it does not assert physical provider-side work
cancellation or resource release beyond the client-owned HTTP response body.

## Executable evidence and limits

[`test_workflow_provider_stream_close.py`](../tests/test_workflow_provider_stream_close.py)
uses synthetic OpenAI-compatible and Anthropic SSE frames plus a blocking mock
byte stream. It verifies the first content event, closes the consumer, and
asserts that the mock body has been closed synchronously. No external network,
provider credentials, model, daemon, or hardware is used.

The unchanged-source baseline was **2 failed** (one case per provider wire).
After the bounded fix, the closure regression passes **2 tests** and the focused
provider adapter/API contract tests pass **12 tests, 1 upstream deprecation
warning**. Ruff and diff whitespace checks pass. These checks do not qualify
normal completion behavior across all provider responses, remote cancellation,
provider availability, or unrelated workflow owners.
