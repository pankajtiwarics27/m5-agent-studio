from __future__ import annotations

import json

import pytest

from model_registry import (
    ModelDescriptor,
    ModelLimits,
    ModelManifestError,
    ModelProvenance,
    ModelRegistry,
)


def _descriptor(digest: str = "sha256:one") -> ModelDescriptor:
    return ModelDescriptor(
        model_id="generic-model",
        digest=digest,
        source="local",
        revision="revision-1",
        quantization="q4",
        runtime="portable-runtime",
        capabilities=("tool-calls", "streaming", "tool-calls"),
        limits=ModelLimits(context_tokens=8192, max_output_tokens=1024, memory_bytes=2 * 1024**3),
        supports_tools=True,
        supports_schema=True,
        supports_streaming=True,
        trust="unverified",
        provenance=ModelProvenance("fixture-manifest", "fixture://generic-model", "verified"),
    )


def test_descriptor_contains_identity_capabilities_limits_and_provenance() -> None:
    descriptor = _descriptor()
    encoded = json.dumps(descriptor.to_mapping(), sort_keys=True)
    restored = ModelDescriptor.from_mapping(json.loads(encoded))

    assert restored == descriptor
    assert restored.digest == "sha256:one"
    assert restored.capabilities == ("streaming", "tool-calls")
    assert restored.limits.context_tokens == 8192
    assert restored.provenance.verification == "verified"


def test_descriptor_is_immutable_and_registry_is_deterministic() -> None:
    first = _descriptor("sha256:two")
    second = _descriptor("sha256:one")
    registry = ModelRegistry([first, second])

    with pytest.raises(AttributeError):
        first.model_id = "changed"  # type: ignore[misc]
    assert [item.digest for item in registry.descriptors()] == ["sha256:one", "sha256:two"]
    assert registry.resolve("generic-model", "sha256:two") == first


def test_registry_rejects_ambiguous_resolution_and_conflicting_duplicate() -> None:
    first = _descriptor("sha256:one")
    registry = ModelRegistry([first])
    registry.register(first)
    with pytest.raises(ModelManifestError, match="already registered"):
        registry.register(
            ModelDescriptor(
                model_id=first.model_id,
                digest=first.digest,
                source="different-source",
                revision=first.revision,
                runtime=first.runtime,
                limits=first.limits,
                provenance=first.provenance,
            )
        )
    registry.register(_descriptor("sha256:two"))
    with pytest.raises(ModelManifestError, match="ambiguous"):
        registry.resolve("generic-model")
    with pytest.raises(ModelManifestError, match="not found"):
        registry.resolve("generic-model", "sha256:missing")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"digest": ""}, "digest"),
        ({"limits": {"context_tokens": 0}}, "context_tokens"),
        ({"trust": "unknown"}, "trust"),
        ({"provenance": {"origin": "fixture", "verification": "unknown"}}, "verification"),
    ],
)
def test_manifest_rejects_missing_or_invalid_contract_values(change: dict[str, object], message: str) -> None:
    payload = _descriptor().to_mapping()
    payload.update(change)
    with pytest.raises((ModelManifestError, TypeError), match=message):
        ModelDescriptor.from_mapping(payload)


def test_registry_manifest_round_trip_preserves_multiple_digests() -> None:
    registry = ModelRegistry([_descriptor("sha256:one"), _descriptor("sha256:two")])
    restored = ModelRegistry.from_manifest(registry.to_manifest())
    assert restored.descriptors() == registry.descriptors()
