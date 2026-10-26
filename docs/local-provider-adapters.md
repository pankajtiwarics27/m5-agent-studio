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

## Evidence

`tests/test_local_provider_adapters.py` covers both response shapes, descriptor
normalization, capability negotiation, unavailable runtimes, malformed data,
and cancellation without contacting a live local service.
