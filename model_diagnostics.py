"""Capability-aware model diagnostics for user-facing selection surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor


@dataclass(frozen=True, slots=True)
class ModelDiagnostic:
    """One model's feasibility result and explainable tradeoffs."""

    model_id: str
    digest: str
    feasible: bool
    reasons: tuple[str, ...]
    tradeoffs: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "digest": self.digest,
            "feasible": self.feasible,
            "reasons": list(self.reasons),
            "tradeoffs": list(self.tradeoffs),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticsReport:
    """Deterministic diagnostics for an ordered set of model candidates."""

    task: TaskRequirements
    models: tuple[ModelDiagnostic, ...]

    @property
    def feasible_models(self) -> tuple[ModelDiagnostic, ...]:
        return tuple(model for model in self.models if model.feasible)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "feasible_models": [model.to_mapping() for model in self.feasible_models],
            "models": [model.to_mapping() for model in self.models],
        }


def _tradeoffs(model: ModelDescriptor) -> tuple[str, ...]:
    limits = [f"context={model.limits.context_tokens} tokens"]
    if model.limits.max_output_tokens is not None:
        limits.append(f"max_output={model.limits.max_output_tokens} tokens")
    if model.limits.memory_bytes is not None:
        limits.append(f"memory={model.limits.memory_bytes} bytes")
    support = ",".join(
        name
        for name, enabled in (
            ("tools", model.supports_tools),
            ("schema", model.supports_schema),
            ("streaming", model.supports_streaming),
        )
        if enabled
    ) or "none"
    capabilities = ",".join(model.capabilities) or "none"
    return (
        f"runtime={model.runtime}",
        f"source={model.source}",
        f"capabilities={capabilities}",
        f"supports={support}",
        *limits,
        f"trust={model.trust}",
    )


def diagnose_models(
    models: Sequence[ModelDescriptor],
    task: TaskRequirements,
    hardware: HardwareProfile | None = None,
) -> DiagnosticsReport:
    """Explain feasibility and tradeoffs without selecting or executing a model."""
    if not isinstance(task, TaskRequirements):
        raise TypeError("task must be a TaskRequirements value.")
    negotiator = CapabilityNegotiator(hardware)
    diagnostics: list[ModelDiagnostic] = []
    descriptors = list(models)
    if not all(isinstance(model, ModelDescriptor) for model in descriptors):
        raise TypeError("models must contain ModelDescriptor values.")
    for model in sorted(descriptors, key=lambda item: (item.model_id, item.digest)):
        reasons = negotiator.evaluate(model, task)
        diagnostics.append(ModelDiagnostic(model.model_id, model.digest, not reasons, reasons, _tradeoffs(model)))
    return DiagnosticsReport(task, tuple(diagnostics))
