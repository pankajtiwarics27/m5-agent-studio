"""Capability negotiation between model descriptors, tasks, and hardware."""

from __future__ import annotations

from dataclasses import dataclass

from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    """Capabilities and limits a task requires before model execution."""

    required_capabilities: tuple[str, ...] = ()
    required_hardware_capabilities: tuple[str, ...] = ()
    minimum_context_tokens: int = 1
    requires_tools: bool = False
    requires_schema: bool = False
    requires_streaming: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.minimum_context_tokens, bool) or self.minimum_context_tokens <= 0:
            raise ValueError("minimum_context_tokens must be positive.")
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(self.required_capabilities))))
        object.__setattr__(self, "required_hardware_capabilities", tuple(sorted(set(self.required_hardware_capabilities))))


@dataclass(frozen=True, slots=True)
class NegotiationDecision:
    """Stable result explaining whether one model may execute a task."""

    model_id: str
    digest: str
    feasible: bool
    reasons: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()


def _hardware_capabilities(profile: HardwareProfile | None) -> set[str]:
    if profile is None:
        return set()
    capabilities: set[str] = set()
    for accelerator in profile.accelerators:
        capabilities.add(accelerator.kind)
        capabilities.update(accelerator.capabilities)
    for runtime in profile.installed_runtimes:
        capabilities.add(runtime.name)
        capabilities.update(runtime.capabilities)
    return capabilities


class CapabilityNegotiator:
    """Evaluate capabilities without loading models or contacting providers."""

    def __init__(self, hardware: HardwareProfile | None = None) -> None:
        self.hardware = hardware

    def evaluate(self, model: ModelDescriptor, task: TaskRequirements) -> tuple[str, ...]:
        reasons: list[str] = []
        model_capabilities = set(model.capabilities)
        missing = sorted(set(task.required_capabilities) - model_capabilities)
        if missing:
            reasons.append(f"missing model capabilities: {', '.join(missing)}")
        if model.limits.context_tokens < task.minimum_context_tokens:
            reasons.append(
                f"context limit {model.limits.context_tokens} is below required {task.minimum_context_tokens}"
            )
        if task.requires_tools and not model.supports_tools:
            reasons.append("model does not support tool calls")
        if task.requires_schema and not model.supports_schema:
            reasons.append("model does not support structured schema output")
        if task.requires_streaming and not model.supports_streaming:
            reasons.append("model does not support streaming")
        if model.trust == "blocked":
            reasons.append("model is blocked by trust policy")

        hardware_capabilities = _hardware_capabilities(self.hardware)
        missing_hardware = sorted(set(task.required_hardware_capabilities) - hardware_capabilities)
        if missing_hardware:
            reasons.append(f"missing hardware capabilities: {', '.join(missing_hardware)}")
        if (
            self.hardware is not None
            and self.hardware.available_memory_bytes is not None
            and model.limits.memory_bytes is not None
            and model.limits.memory_bytes > self.hardware.available_memory_bytes
        ):
            reasons.append(
                f"model memory requirement {model.limits.memory_bytes} exceeds available hardware memory "
                f"{self.hardware.available_memory_bytes}"
            )
        return tuple(reasons)

    def negotiate(
        self,
        model: ModelDescriptor,
        task: TaskRequirements,
        alternatives: tuple[ModelDescriptor, ...] = (),
    ) -> NegotiationDecision:
        """Return a decision; rejected models never trigger implicit fallback."""
        if not isinstance(model, ModelDescriptor):
            raise TypeError("model must be a ModelDescriptor value.")
        if not isinstance(task, TaskRequirements):
            raise TypeError("task must be a TaskRequirements value.")
        reasons = self.evaluate(model, task)
        compatible = []
        for candidate in alternatives:
            if not isinstance(candidate, ModelDescriptor):
                raise TypeError("alternatives must contain ModelDescriptor values.")
            if not self.evaluate(candidate, task):
                compatible.append(f"{candidate.model_id}@{candidate.digest}")
        return NegotiationDecision(
            model_id=model.model_id,
            digest=model.digest,
            feasible=not reasons,
            reasons=reasons,
            alternatives=tuple(sorted(set(compatible))),
        )
