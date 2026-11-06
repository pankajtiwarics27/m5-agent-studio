"""Capability-aware adapters for local Ollama, llama.cpp, and MoneyPrinterTurbo runtimes."""

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
from execution_budget import BudgetExceeded, ExecutionBudget
from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance

# Capability surface a MoneyPrinterTurbo deployment acts as a gateway for:
# LLM script/term generation routed through the backend configured in MPT's
# config.toml (commonly an OpenAI-compatible endpoint serving Qwen/DeepSeek-class
# models), TTS/speech synthesis routing, material search, subtitle generation, and
# the final render itself. These are capability tags, not model identifiers.
MONEYPRINTER_GATEWAY_CAPABILITIES: tuple[str, ...] = (
    "material-search",
    "script-generation",
    "speech-synthesis",
    "subtitle-generation",
    "video-generation",
)


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
    """Common discovery and negotiation behavior for local runtimes.

    ``budget`` optionally binds discovery to the Studio's execution budget
    (:class:`execution_budget.ExecutionBudget`). Each remote discovery request
    consumes one tool unit and is refused when the budget is exhausted, so a
    budget-starved runtime fails closed instead of adding concurrent load to the
    host's unified memory. Cancellation propagates to the caller; the adapter
    starts no background work and adds no concurrency of its own.
    """

    runtime_name: str
    models_path: str

    def __init__(
        self,
        transport: LocalTransport,
        *,
        request_timeout: float = 5.0,
        budget: ExecutionBudget | None = None,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive.")
        if budget is not None and not isinstance(budget, ExecutionBudget):
            raise TypeError("budget must be an ExecutionBudget value.")
        self._transport = transport
        self._request_timeout = request_timeout
        self._budget = budget

    async def _fetch(self) -> Mapping[str, Any]:
        if self._budget is not None:
            try:
                # Each remote call consumes one tool unit up front and re-checks
                # elapsed time/tokens/cost/retries. ExecutionCancelled propagates.
                self._budget.consume(tools=1)
            except BudgetExceeded as exc:
                raise LocalAdapterError("execution budget exceeded") from exc
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


class _OpenAIModelsAdapter(LocalModelAdapter):
    """Shared parser for OpenAI-compatible ``/v1/models`` (``data: [...]``) payloads."""

    source_label: str = ""
    error_subject: str = ""
    default_capabilities: tuple[str, ...] = ("chat", "streaming")

    def _parse_models(self, payload: Mapping[str, Any]) -> Sequence[ModelDescriptor]:
        raw_models = payload.get("data", ())
        if isinstance(raw_models, (str, bytes)) or not isinstance(raw_models, Sequence):
            raise LocalAdapterError(f"{self.error_subject} returned invalid model data")
        descriptors: list[ModelDescriptor] = []
        for raw in raw_models:
            if not isinstance(raw, Mapping):
                raise LocalAdapterError(f"{self.error_subject} returned an invalid model record")
            model_id = _text(raw.get("id"))
            if not model_id:
                raise LocalAdapterError(f"{self.error_subject} returned a model without an id")
            capabilities = _capabilities(raw.get("capabilities"), self.default_capabilities)
            digest = _digest(raw.get("digest"), model_id)
            descriptors.append(
                ModelDescriptor(
                    model_id=model_id,
                    digest=digest,
                    source=self.source_label,
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
                    provenance=ModelProvenance(self.source_label, model_id, "unverified"),
                )
            )
        return descriptors


class LlamaCppAdapter(_OpenAIModelsAdapter):
    runtime_name = "llama.cpp"
    models_path = "/v1/models"
    source_label = "llama.cpp"
    error_subject = "llama.cpp"


class MoneyPrinterAPIAdapter(_OpenAIModelsAdapter):
    """Discovery for a MoneyPrinterTurbo gateway's OpenAI-compatible API surface.

    The Studio's ``MoneyPrinterAPI`` provider profile targets the gateway at
    ``http://localhost:8080/v1`` (MoneyPrinterTurbo's default ``listen_port`` is
    8080). Stock MoneyPrinterTurbo serves its native REST API under ``/api/v1``;
    this adapter expects an OpenAI-compatible front (a shim or gateway) on the
    configured base URL and reports itself unavailable — never fabricated models —
    when that surface is absent or misbehaves.
    """

    runtime_name = "moneyprinter"
    models_path = "/v1/models"
    source_label = "moneyprinter"
    error_subject = "moneyprinter"
    default_capabilities = MONEYPRINTER_GATEWAY_CAPABILITIES
