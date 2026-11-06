# Ollama and llama.cpp adapters

AS-020 defines `OllamaAdapter` and `LlamaCppAdapter` in
[`local_provider_adapters.py`](../local_provider_adapters.py). Both adapters
discover local model records, normalize them into AS-017 descriptors, and
reuse AS-051 negotiation for task/hardware capability checks.

Discovery uses an injected transport and performs no runtime installation,
model loading, or network access by itself. Unavailable endpoints, non-success
responses, and malformed model payloads return an unavailable snapshot with a
safe error. Cancellation propagates to the caller.

Ollama uses `/api/tags`; llama.cpp uses the OpenAI-compatible `/v1/models`
shape. Missing remote digests are labelled `unverified:<model-id>` rather than
being presented as cryptographic verification.

## MoneyPrinterAPI gateway

`MoneyPrinterAPIAdapter` applies the same OpenAI-compatible `/v1/models`
contract to a MoneyPrinterTurbo gateway (`runtime_name="moneyprinter"`), and
`model_discovery.py` merges the declared gateway capability surface
(`material-search`, `script-generation`, `speech-synthesis`,
`subtitle-generation`, `video-generation`) into descriptors discovered from
that runtime. Stock MoneyPrinterTurbo serves a native `/api/v1` REST API, not
`/v1/models`; against a stock install the adapter reports itself unavailable
rather than inventing models. See
[`moneyprinter-turbo-integration.md`](moneyprinter-turbo-integration.md).

All adapters accept an optional `budget=`
(`execution_budget.ExecutionBudget`): each remote discovery request consumes one
tool unit, exhaustion fails closed with `"execution budget exceeded"`, and a
cancelled budget propagates `ExecutionCancelled` instead of degrading into a
generic unavailable snapshot.

## Evidence

`tests/test_local_provider_adapters.py` covers both response shapes, descriptor
normalization, capability negotiation, unavailable runtimes, malformed data,
and cancellation without contacting a live local service.
`tests/test_moneyprinter_api.py` covers the MoneyPrinterAPI adapter, gateway
capability merging, and budget behavior.
