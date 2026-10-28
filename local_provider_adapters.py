"""Capability-aware adapters for local Ollama and llama.cpp runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from capability_negotiation import (
    CapabilityNegotiator,
    NegotiationDecision,
    TaskRequirements,
)
from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance


class LocalTransport(Protocol):
    def __call__(self, method: str, path: str, timeout: float) -> Awaitable[tuple[int, Mapping[str, Any]]]: ...


@dataclass(frozen=True, slots=True)
class LocalRuntimeSnapshot:
    runtime: str
    available: bool
    models: tuple[ModelDescriptor, ...] = ()
    error: str | None = None


class LocalAdapterError(RuntimeError):
    """Safe error category for malformed or unavailable local runtimes."""


def _text(value: object, field: str = "value", default: str = "") -> str:
    if not isinstance(value, str):
        return default
    result = value.strip()
    if not result or "\x00" in result:
        return default
    return result


def _positive(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _capabilities(value: object, defaults: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return defaults
    values = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return tuple(sorted(set(values or defaults)))


def _digest(value: object, model_id: str) -> str:
    normalized = _text(value)
    return normalized or f"unverified:{model_id}"


class LocalModelAdapter:
    """Common discovery and negotiation behavior for local runtimes."""

    runtime_name: str
    models_path: str

    def __init__(self, transport: LocalTransport, *, request_timeout: float = 5.0) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive.")
        self._transport = transport
        self._request_timeout = request_timeout

    async def _fetch(self) -> Mapping[str, Any]:
        try:
            status, payload = await self._transport("GET", self.models_path, self._request_timeout)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise LocalAdapterError("local runtime unavailable") from exc
        if status < 200 or status >= 300:
            raise LocalAdapterError(f"local runtime returned HTTP {status}")
        if not isinstance(payload, Mapping):
            raise LocalAdapterError("local runtime returned an invalid response")
        return payload

    async def discover(self) -> LocalRuntimeSnapshot:
        try:
            payload = await self._fetch()
            models = tuple(sorted(self._parse_models(payload), key=lambda item: (item.model_id, item.digest)))
            return LocalRuntimeSnapshot(self.runtime_name, True, models)
        except asyncio.CancelledError:
            raise
        except LocalAdapterError as exc:
            return LocalRuntimeSnapshot(self.runtime_name, False, error=str(exc))

    def negotiate(
        self,
        snapshot: LocalRuntimeSnapshot,
        task: TaskRequirements,
        hardware: HardwareProfile | None = None,
    ) -> tuple[NegotiationDecision, ...]:
        if snapshot.runtime != self.runtime_name:
            raise ValueError("snapshot belongs to a different runtime adapter.")
        if not snapshot.available:
            return ()
        negotiator = CapabilityNegotiator(hardware)
        return tuple(negotiator.negotiate(model, task, snapshot.models) for model in snapshot.models)

    def _parse_models(self, payload: Mapping[str, Any]) -> Sequence[ModelDescriptor]:
        raise NotImplementedError


class OllamaAdapter(LocalModelAdapter):
    runtime_name = "ollama"
    models_path = "/api/tags"

    def _parse_models(self, payload: Mapping[str, Any]) -> Sequence[ModelDescriptor]:
        raw_models = payload.get("models", ())
        if isinstance(raw_models, (str, bytes)) or not isinstance(raw_models, Sequence):
            raise LocalAdapterError("ollama returned invalid model data")
        descriptors: list[ModelDescriptor] = []
        for raw in raw_models:
            if not isinstance(raw, Mapping):
                raise LocalAdapterError("ollama returned an invalid model record")
            model_id = _text(raw.get("name"))
            if not model_id:
                raise LocalAdapterError("ollama returned a model without an id")
            details_raw = raw.get("details")
            details = details_raw if isinstance(details_raw, Mapping) else {}
            capabilities = _capabilities(details.get("families"), ("chat", "streaming"))
            descriptors.append(
                ModelDescriptor(
                    model_id=model_id,
                    digest=_digest(raw.get("digest"), model_id),
                    source="ollama",
                    revision=_text(raw.get("modified_at"), default=_digest(raw.get("digest"), model_id)),
                    quantization=_text(details.get("quantization_level"), default="") or None,
                    runtime=self.runtime_name,
                    capabilities=capabilities,
                    limits=ModelLimits(
                        context_tokens=_positive(details.get("context_length"), 1),
                        memory_bytes=_positive(raw.get("size"), 0) or None,
                    ),
                    supports_tools="tools" in capabilities,
                    supports_schema="json" in capabilities,
                    supports_streaming="streaming" in capabilities,
                    provenance=ModelProvenance("ollama", model_id, "unverified"),
                )
            )
        return descriptors


class LlamaCppAdapter(LocalModelAdapter):
    runtime_name = "llama.cpp"
    models_path = "/v1/models"

    def _parse_models(self, payload: Mapping[str, Any]) -> Sequence[ModelDescriptor]:
        raw_models = payload.get("data", ())
        if isinstance(raw_models, (str, bytes)) or not isinstance(raw_models, Sequence):
            raise LocalAdapterError("llama.cpp returned invalid model data")
        descriptors: list[ModelDescriptor] = []
        for raw in raw_models:
            if not isinstance(raw, Mapping):
                raise LocalAdapterError("llama.cpp returned an invalid model record")
            model_id = _text(raw.get("id"))
            if not model_id:
                raise LocalAdapterError("llama.cpp returned a model without an id")
            capabilities = _capabilities(raw.get("capabilities"), ("chat", "streaming"))
            digest = _digest(raw.get("digest"), model_id)
            descriptors.append(
                ModelDescriptor(
                    model_id=model_id,
                    digest=digest,
                    source="llama.cpp",
                    revision=_text(raw.get("created"), default=digest),
                    quantization=_text(raw.get("quantization"), default="") or None,
                    runtime=self.runtime_name,
                    capabilities=capabilities,
                    limits=ModelLimits(
                        context_tokens=_positive(raw.get("context_length", raw.get("max_model_len")), 1),
                        memory_bytes=_positive(raw.get("memory_bytes"), 0) or None,
                    ),
                    supports_tools="tools" in capabilities,
                    supports_schema="json" in capabilities,
                    supports_streaming="streaming" in capabilities,
                    provenance=ModelProvenance("llama.cpp", model_id, "unverified"),
                )
            )
        return descriptors
