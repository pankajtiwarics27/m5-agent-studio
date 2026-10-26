# Provider adapter contract

## Scope and dispatch

Cloud streaming is routed through the `ProviderAdapter` protocol and `PROVIDER_ADAPTERS` registry in `app.py`. The built-in `openai` and `anthropic` providers use separate `OpenAICompatibleProviderAdapter` and `AnthropicProviderAdapter` implementations. Local oMLX and combo/failover routing remain separate. An unknown cloud `wire` fails closed rather than silently selecting OpenAI semantics.

The existing `_stream_cloud_openai()` and `_stream_cloud_anthropic()` functions remain the wire-specific implementations and compatibility entry points. Provider presets, model IDs, hardware selection, credential storage, and database schema are unchanged. No provider SDK or runtime dependency is introduced; `studio.py` remains a generated standalone artifact.

## Normalized stream contract

Adapters yield the existing internal event dictionaries:

- `{"type": "content", "text": str}` for user-visible output.
- `{"type": "reasoning", "text": str}` for reasoning deltas.
- A final `{"type": "usage", ...}` event containing `decode_tps`, `prefill_tps`, `ttft`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `finish_reason`, and `elapsed_s`.

Anthropic finish reasons are mapped to the shared OpenAI-style vocabulary (`end_turn`/`stop_sequence` → `stop`, `max_tokens` → `length`, `tool_use` → `tool_calls`). The OpenAI-compatible adapter preserves the provider's corresponding finish reason. The existing reasoning-mode behavior is preserved: Anthropic receives its extended-thinking fields, while the OpenAI-compatible adapter ignores that option.

## Tool contract

Tool schemas continue to be described in the system prompt using Studio's established `<tool_call>…</tool_call>` text protocol. Provider adapters preserve the generated text as content, and the shared `extract_tool_calls()` parser turns it into the existing tool-call structure. This contract is intentionally provider-neutral; it does not enable native OpenAI or Anthropic tool invocation or change downstream execution/approval policy.

## Error and security contract

Provider HTTP-status, provider SSE-error, and transport failures raise `ProviderAdapterError`, a `RuntimeError` subclass with provider alias, category, and (for HTTP errors) status code. Upstream error bodies and messages are not included, so a provider cannot reflect credentials or private prompt text into the Studio error stream. Redirect rejection, outbound URL validation, credential headers, and private-network policy remain in the existing transport boundary.

## Contract tests and evidence

`tests/test_provider_adapters.py` applies the same mocked streaming, tool-markup, HTTP-error, stream-error, and transport-error cases to the actual `openai` and `anthropic` provider aliases. It also verifies request-specific wire encoding, usage/finish normalization, credential placement, and fail-closed behavior for an unknown wire. The tests use `httpx.MockTransport`; they do not call a provider, require a key, or require a model/runtime.

Run the focused suite with:

```bash
python -m pytest -q tests/test_provider_adapters.py tests/test_local_integration_contracts.py tests/test_redirect_safety.py
```
