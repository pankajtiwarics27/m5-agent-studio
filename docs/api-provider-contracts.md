# API and provider contract tests

AS-013 verifies the user-facing streaming boundary with local fakes and
provider-neutral mock transports. The tests do not contact a model, cloud
provider, MCP server, or container runtime.

## Covered contracts

- OpenAI-compatible API streaming preserves content and reasoning SSE deltas,
  completion events, and the `[DONE]` sentinel.
- A provider failure is represented as an SSE error for a streaming API caller;
  provider adapters retain their safe error category and never echo upstream
  secret text.
- A cancelled consumer cancels the in-flight provider stream, and a timeout
  stops a slow stream without requiring a live endpoint.
- Existing provider-wire, local-runtime, redirect, and normalized usage
  contracts remain covered by `tests/test_provider_adapters.py`,
  `tests/test_local_integration_contracts.py`, and
  `tests/test_redirect_safety.py`.

## Evidence

Run the focused contract suite:

```bash
python -m pytest -q \
  tests/test_api_provider_contracts.py \
  tests/test_provider_adapters.py \
  tests/test_local_integration_contracts.py \
  tests/test_redirect_safety.py
```

The fixtures use `httpx.MockTransport`, an injected async stream, and a
temporary local database. They assert both successful output and failure or
cancellation behavior while keeping provider and hardware selection
configurable.
