from __future__ import annotations

import pytest

from capability_negotiation import TaskRequirements
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from model_switching import ContextMessage, ModelSwitchError, switch_model


def _model(model_id: str, context_tokens: int = 20, capabilities: tuple[str, ...] = ("text",)) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=f"sha256:{model_id}",
        source="fixture",
        revision="r1",
        runtime="runtime",
        capabilities=capabilities,
        limits=ModelLimits(context_tokens),
        supports_streaming=True,
        provenance=ModelProvenance("fixture", "fixture://model", "verified"),
    )


def test_compatible_switch_preserves_context_and_reports_no_truncation() -> None:
    context = (ContextMessage("user", "first message"), ContextMessage("assistant", "second message"))
    decision = switch_model(_model("old"), _model("new"), TaskRequirements(required_capabilities=("text",)), context)
    assert decision.previous_model == "old"
    assert decision.target_model == "new"
    assert decision.context == context
    assert decision.truncated_messages == 0


def test_switch_truncation_is_explicit_and_keeps_newest_messages() -> None:
    context = tuple(ContextMessage("user", f"message {index}") for index in range(5))
    decision = switch_model(_model("old"), _model("new", context_tokens=4), TaskRequirements(), context)
    assert decision.context == context[-2:]
    assert decision.truncated_messages == 3
    assert "truncated 3" in decision.reason


def test_incompatible_target_is_rejected_without_switching() -> None:
    with pytest.raises(ModelSwitchError, match="missing model capabilities"):
        switch_model(_model("old"), _model("new", capabilities=("text",)), TaskRequirements(required_capabilities=("vision",)), ())


def test_invalid_switch_inputs_fail_closed() -> None:
    with pytest.raises(TypeError, match="context"):
        switch_model(_model("old"), _model("new"), TaskRequirements(), ("not-message",))  # type: ignore[arg-type]
