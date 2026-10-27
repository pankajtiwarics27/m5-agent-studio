"""Per-task model switching with capability checks and auditable truncation."""

from __future__ import annotations

from dataclasses import dataclass

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor


class ModelSwitchError(RuntimeError):
    """Raised when a target model cannot safely replace the current model."""


@dataclass(frozen=True, slots=True)
class ContextMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class SwitchDecision:
    previous_model: str
    target_model: str
    context: tuple[ContextMessage, ...]
    truncated_messages: int
    reason: str


def switch_model(
    current_model: ModelDescriptor,
    target_model: ModelDescriptor,
    task: TaskRequirements,
    context: tuple[ContextMessage, ...],
    *,
    hardware: HardwareProfile | None = None,
) -> SwitchDecision:
    """Validate target compatibility and preserve the newest fitting context."""
    if not isinstance(current_model, ModelDescriptor) or not isinstance(target_model, ModelDescriptor):
        raise TypeError("current_model and target_model must be ModelDescriptor values.")
    if not isinstance(task, TaskRequirements):
        raise TypeError("task must be a TaskRequirements value.")
    if not isinstance(context, tuple) or not all(isinstance(message, ContextMessage) for message in context):
        raise TypeError("context must be a tuple of ContextMessage values.")
    reasons = CapabilityNegotiator(hardware).evaluate(target_model, task)
    if reasons:
        raise ModelSwitchError("target model is incompatible: " + " | ".join(reasons))

    budget = target_model.limits.context_tokens
    kept: list[ContextMessage] = []
    used = 0
    for message in reversed(context):
        message_tokens = max(1, len(message.content.split()))
        if used + message_tokens > budget:
            break
        kept.append(message)
        used += message_tokens
    kept.reverse()
    truncated = len(context) - len(kept)
    reason = "target compatible; context preserved"
    if truncated:
        reason = f"target compatible; truncated {truncated} oldest context messages to fit target limit"
    return SwitchDecision(
        current_model.model_id,
        target_model.model_id,
        tuple(kept),
        truncated,
        reason,
    )
