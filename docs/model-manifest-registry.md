# Model manifest and registry

AS-017 defines the provider-neutral `ModelDescriptor` and `ModelRegistry` in
[`model_registry.py`](../model_registry.py). Descriptors are immutable and
carry the identity and digest needed to distinguish model revisions without
assuming a provider SDK or runtime.

## Descriptor contract

Each descriptor includes:

- model identity, digest, source, revision, quantization, and runtime;
- normalized capability labels;
- context, output, and optional memory limits;
- independent tool, schema, and streaming support flags;
- trust status and provenance origin/locator/verification.

The registry accepts multiple digests for a model identity, rejects conflicting
duplicate registrations, and requires a digest when resolution would otherwise
be ambiguous. It performs no downloads, filesystem access, provider calls, or
model loading.

## Evidence

`tests/test_model_registry.py` covers complete descriptor round trips,
immutable records, duplicate and ambiguous registry handling, and malformed
manifest rejection. Fixtures use generic model/runtime names and no provider
or hardware-specific assumptions.
