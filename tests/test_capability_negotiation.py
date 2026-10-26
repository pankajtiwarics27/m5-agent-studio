from __future__ import annotations

import pytest

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from hardware_capabilities import AcceleratorCapability, HardwareProfile
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance


def _model(
    model_id: str = "model-a",
    digest: str = "sha256:a",
    *,
    capabilities: tuple[str, ...] = ("text",),
    context_tokens: int = 4096,
    memory_bytes: int | None = 100,
    tools: bool = True,
    schema: bool = True,
    streaming: bool = True,
    trust: str = "trusted",
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=digest,
        source="fixture",
        revision="r1",
        runtime="runtime",
        capabilities=capabilities,
        limits=ModelLimits(context_tokens, memory_bytes=memory_bytes),
        supports_tools=tools,
        supports_schema=schema,
        supports_streaming=streaming,
        trust=trust,  # type: ignore[arg-type]
        provenance=ModelProvenance("fixture", "fixture://model", "verified"),
    )


def _hardware() -> HardwareProfile:
    return HardwareProfile(
        "OS",
        "arch",
        accelerators=(AcceleratorCapability("gpu", capabilities=("tensor",)),),
        available_memory_bytes=200,
    )


def test_matching_model_is_feasible() -> None:
    task = TaskRequirements(
        required_capabilities=("text",),
        required_hardware_capabilities=("tensor",),
        minimum_context_tokens=2048,
        requires_tools=True,
        requires_schema=True,
        requires_streaming=True,
    )
    decision = CapabilityNegotiator(_hardware()).negotiate(_model(), task)
    assert decision.feasible is True
    assert decision.reasons == ()
    assert decision.alternatives == ()


def test_missing_capabilities_are_rejected_with_clear_alternative() -> None:
    task = TaskRequirements(required_capabilities=("vision",))
    decision = CapabilityNegotiator(_hardware()).negotiate(
        _model(), task, alternatives=(_model("model-b", "sha256:b", capabilities=("text", "vision")),)
    )
    assert decision.feasible is False
    assert "missing model capabilities: vision" in decision.reasons
    assert decision.alternatives == ("model-b@sha256:b",)

    hardware_decision = CapabilityNegotiator(_hardware()).negotiate(
        _model(), TaskRequirements(required_hardware_capabilities=("npu",))
    )
    assert "missing hardware capabilities: npu" in hardware_decision.reasons


@pytest.mark.parametrize(
    "model, task, expected",
    [
        (_model(context_tokens=1024), TaskRequirements(minimum_context_tokens=2048), "context limit"),
        (_model(tools=False), TaskRequirements(requires_tools=True), "tool calls"),
        (_model(schema=False), TaskRequirements(requires_schema=True), "structured schema"),
        (_model(streaming=False), TaskRequirements(requires_streaming=True), "streaming"),
        (_model(trust="blocked"), TaskRequirements(), "blocked by trust"),
        (_model(memory_bytes=300), TaskRequirements(), "memory requirement"),
    ],
)
def test_limits_support_trust_and_memory_constraints_are_enforced(
    model: ModelDescriptor, task: TaskRequirements, expected: str
) -> None:
    decision = CapabilityNegotiator(_hardware()).negotiate(model, task)
    assert decision.feasible is False
    assert any(expected in reason for reason in decision.reasons)


def test_invalid_negotiation_inputs_fail_without_fallback() -> None:
    negotiator = CapabilityNegotiator()
    with pytest.raises(ValueError, match="minimum_context_tokens"):
        TaskRequirements(minimum_context_tokens=0)
    with pytest.raises(TypeError, match="model"):
        negotiator.negotiate("model", TaskRequirements())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="alternatives"):
        negotiator.negotiate(_model(), TaskRequirements(), alternatives=("other",))  # type: ignore[arg-type]
