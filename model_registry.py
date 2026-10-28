"""Provider-neutral model manifest and registry contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

MODEL_SCHEMA_VERSION = 1
ModelTrust = Literal["trusted", "unverified", "blocked"]
ProvenanceVerification = Literal["verified", "unverified", "rejected"]
_MODEL_TRUST = frozenset({"trusted", "unverified", "blocked"})
_PROVENANCE_VERIFICATION = frozenset({"verified", "unverified", "rejected"})


class ModelManifestError(ValueError):
    """Raised when a model descriptor or registry operation is invalid."""


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise ModelManifestError(f"{field} must not be empty.")
    if "\x00" in result:
        raise ModelManifestError(f"{field} must not contain a NUL character.")
    return result or None


def _positive_int(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be a positive integer.")
    if value <= 0:
        raise ModelManifestError(f"{field} must be a positive integer.")
    return value


def _non_negative_int(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be a non-negative integer.")
    if value < 0:
        raise ModelManifestError(f"{field} must be a non-negative integer.")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence of strings.")
    normalized = []
    for item in value:
        text = _text(item, field)
        if text is not None:
            normalized.append(text)
    return tuple(sorted(set(normalized)))


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object.")
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean.")
    return value


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """Resource and context limits needed for capability negotiation."""

    context_tokens: int
    max_output_tokens: int | None = None
    memory_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_tokens", _positive_int(self.context_tokens, "limits.context_tokens"))
        object.__setattr__(self, "max_output_tokens", _positive_int(self.max_output_tokens, "limits.max_output_tokens", optional=True))
        object.__setattr__(self, "memory_bytes", _non_negative_int(self.memory_bytes, "limits.memory_bytes", optional=True))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ModelLimits:
        return cls(
            context_tokens=cast(int, value.get("context_tokens")),
            max_output_tokens=cast("int | None", value.get("max_output_tokens")),
            memory_bytes=cast("int | None", value.get("memory_bytes")),
        )

    def to_mapping(self) -> dict[str, int | None]:
        return {
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "memory_bytes": self.memory_bytes,
        }


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Origin and verification status for an imported or discovered model."""

    origin: str
    locator: str | None = None
    verification: ProvenanceVerification = "unverified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _text(self.origin, "provenance.origin"))
        object.__setattr__(self, "locator", _text(self.locator, "provenance.locator", optional=True))
        if self.verification not in _PROVENANCE_VERIFICATION:
            accepted = ", ".join(sorted(_PROVENANCE_VERIFICATION))
            raise ModelManifestError(f"provenance.verification must be one of: {accepted}.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ModelProvenance:
        return cls(
            origin=cast(str, value.get("origin")),
            locator=value.get("locator"),
            verification=value.get("verification", "unverified"),
        )

    def to_mapping(self) -> dict[str, str | None]:
        return {"origin": self.origin, "locator": self.locator, "verification": self.verification}


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Immutable identity, capability, limit, and provenance record."""

    model_id: str
    digest: str
    source: str
    revision: str
    runtime: str
    limits: ModelLimits
    provenance: ModelProvenance
    quantization: str | None = None
    capabilities: tuple[str, ...] = ()
    supports_tools: bool = False
    supports_schema: bool = False
    supports_streaming: bool = True
    trust: ModelTrust = "unverified"
    schema_version: int = MODEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in ("model_id", "digest", "source", "revision", "runtime"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(self, "quantization", _text(self.quantization, "quantization", optional=True))
        if not isinstance(self.limits, ModelLimits):
            raise TypeError("limits must be a ModelLimits value.")
        if not isinstance(self.provenance, ModelProvenance):
            raise TypeError("provenance must be a ModelProvenance value.")
        object.__setattr__(self, "capabilities", _strings(self.capabilities, "capabilities"))
        for field in ("supports_tools", "supports_schema", "supports_streaming"):
            object.__setattr__(self, field, _bool(getattr(self, field), field))
        if self.trust not in _MODEL_TRUST:
            accepted = ", ".join(sorted(_MODEL_TRUST))
            raise ModelManifestError(f"trust must be one of: {accepted}.")
        if isinstance(self.schema_version, bool) or self.schema_version != MODEL_SCHEMA_VERSION:
            raise ModelManifestError(f"schema_version must be {MODEL_SCHEMA_VERSION}.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ModelDescriptor:
        payload = _mapping(value, "model_descriptor")
        return cls(
            model_id=cast(str, payload.get("model_id")),
            digest=cast(str, payload.get("digest")),
            source=cast(str, payload.get("source")),
            revision=cast(str, payload.get("revision")),
            runtime=cast(str, payload.get("runtime")),
            quantization=payload.get("quantization"),
            capabilities=payload.get("capabilities", ()),
            limits=ModelLimits.from_mapping(_mapping(payload.get("limits"), "limits")),
            supports_tools=payload.get("supports_tools", False),
            supports_schema=payload.get("supports_schema", False),
            supports_streaming=payload.get("supports_streaming", True),
            trust=payload.get("trust", "unverified"),
            provenance=ModelProvenance.from_mapping(_mapping(payload.get("provenance"), "provenance")),
            schema_version=payload.get("schema_version", MODEL_SCHEMA_VERSION),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "digest": self.digest,
            "source": self.source,
            "revision": self.revision,
            "quantization": self.quantization,
            "runtime": self.runtime,
            "capabilities": list(self.capabilities),
            "limits": self.limits.to_mapping(),
            "supports_tools": self.supports_tools,
            "supports_schema": self.supports_schema,
            "supports_streaming": self.supports_streaming,
            "trust": self.trust,
            "provenance": self.provenance.to_mapping(),
        }


class ModelRegistry:
    """In-memory registry with explicit ambiguity and duplicate handling."""

    def __init__(self, descriptors: Sequence[ModelDescriptor] = ()) -> None:
        self._descriptors: dict[tuple[str, str], ModelDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ModelDescriptor) -> None:
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor value.")
        key = (descriptor.model_id, descriptor.digest)
        existing = self._descriptors.get(key)
        if existing is not None and existing != descriptor:
            raise ModelManifestError(f"model identity is already registered with digest `{descriptor.digest}`.")
        self._descriptors[key] = descriptor

    def resolve(self, model_id: str, digest: str | None = None) -> ModelDescriptor:
        normalized_id = _text(model_id, "model_id")
        matches = [descriptor for (identity, _), descriptor in self._descriptors.items() if identity == normalized_id]
        if digest is not None:
            matches = [descriptor for descriptor in matches if descriptor.digest == digest]
        if not matches:
            raise ModelManifestError("model descriptor was not found.")
        if len(matches) > 1:
            raise ModelManifestError("model identity is ambiguous; specify a digest.")
        return matches[0]

    def descriptors(self) -> tuple[ModelDescriptor, ...]:
        return tuple(sorted(self._descriptors.values(), key=lambda item: (item.model_id, item.digest)))

    @classmethod
    def from_manifest(cls, value: Sequence[Mapping[str, Any]]) -> ModelRegistry:
        return cls(tuple(ModelDescriptor.from_mapping(item) for item in value))

    def to_manifest(self) -> list[dict[str, Any]]:
        return [descriptor.to_mapping() for descriptor in self.descriptors()]
