from __future__ import annotations

import json

import pytest

from capability_negotiation import TaskRequirements
from context_reassembly import (
    ContextReassemblyError,
    ReassembledContext,
    reassemble_context,
    reassemble_for_target,
)
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from model_switching import ContextMessage, switch_model


def _descriptor(model_id: str, *, context_tokens: int = 4096, max_output_tokens: int | None = None) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=f"sha256:{model_id}",
        source="ollama",
        revision="rev",
        runtime="ollama",
        limits=ModelLimits(context_tokens=context_tokens, max_output_tokens=max_output_tokens),
        capabilities=("chat", "streaming"),
        supports_streaming=True,
        provenance=ModelProvenance("ollama", model_id, "unverified"),
    )


def _message(role: str, words: int, *, secret: str = "") -> ContextMessage:
    return ContextMessage(role, (f"{role} " * max(1, words)).strip() + (f" SECRET-{secret}" if secret else ""))


def test_small_context_is_kept_intact_chronologically() -> None:
    context = (_message("system", 2), _message("user", 1), _message("assistant", 1), _message("user", 1))
    result = reassemble_context(context, ModelLimits(context_tokens=100))

    assert isinstance(result, ReassembledContext)
    assert result.messages == context
    assert result.kept_indices == (0, 1, 2, 3)
    assert result.dropped == ()
    assert result.truncated is False
    assert result.budget_tokens == 100
    assert result.used_tokens == sum(result.kept_tokens)
    assert result.reason == "context fits the target budget without truncation"


def test_empty_context_produces_an_empty_fit() -> None:
    result = reassemble_context((), ModelLimits(context_tokens=8))
    assert result.messages == () and result.used_tokens == 0 and not result.truncated


def test_output_reservation_is_subtracted_from_the_window() -> None:
    # Three 10-word messages; window 35 minus default 20-token output reserve = 15.
    context = (_message("system", 5), _message("user", 10), _message("assistant", 10))
    limits = ModelLimits(context_tokens=35, max_output_tokens=20)
    result = reassemble_context(context, limits)

    assert result.budget_tokens == 15
    assert result.used_tokens == 15  # system(5) + assistant(10); oldest user dropped
    assert [record.index for record in result.dropped] == [1]
    assert result.dropped[0].role == "user"

    # Exact-boundary: reserving 5 leaves 30 tokens, which fits everything.
    no_drop = reassemble_context(context, limits, reserve_output_tokens=5)
    assert no_drop.dropped == () and no_drop.used_tokens == 25

    one_over = reassemble_context(context, ModelLimits(context_tokens=25), reserve_output_tokens=10)
    assert one_over.budget_tokens == 15 and len(one_over.dropped) == 1


def test_tool_outputs_drop_before_conversation_turns() -> None:
    context = (
        _message("system", 2),
        _message("tool", 30),
        _message("assistant", 4),
        _message("user", 4),
        _message("user", 2),  # newest: mandatory
    )
    result = reassemble_context(context, ModelLimits(context_tokens=4))

    assert [record.index for record in result.dropped] == [1, 2, 3]
    assert result.messages == (context[0], context[4])
    assert "oldest tool output" in result.dropped[0].reason
    assert "oldest assistant turn" in result.dropped[1].reason
    assert "oldest droppable turn" in result.dropped[2].reason
    assert result.truncated is True


def test_oldest_first_within_a_tier() -> None:
    context = (_message("tool", 10, secret="A"), _message("tool", 10, secret="B"), _message("user", 5))
    result = reassemble_context(context, ModelLimits(context_tokens=20))

    assert [record.index for record in result.dropped] == [0]
    assert result.dropped[0].reason == "oldest tool output dropped to fit the target budget"


def test_function_role_counts_as_tool_and_unknown_roles_are_droppable() -> None:
    context = (_message("function", 10), _message("reviewer", 10), _message("user", 5))
    result = reassemble_context(context, ModelLimits(context_tokens=15))

    # function (tier 0) drops before the unknown role (tier 2); the newest stays.
    assert [record.index for record in result.dropped] == [0]
    assert result.messages == (context[1], context[2])


def test_newest_message_is_never_dropped() -> None:
    context = (_message("system", 1), _message("tool", 50))
    with pytest.raises(ContextReassemblyError, match="mandatory messages"):
        reassemble_context(context, ModelLimits(context_tokens=10))


def test_anchors_are_never_dropped_and_oversized_anchors_fail_audibly() -> None:
    context = (_message("system", 30, secret="SYS"), _message("user", 40, secret="USR"), _message("user", 5))
    with pytest.raises(ContextReassemblyError, match=r"exceed the target budget of \d+ tokens"):
        reassemble_context(context, ModelLimits(context_tokens=20))


def test_injected_estimator_drives_the_accounting() -> None:
    # A char-length estimator: 400 chars vs a 200-token budget drops nothing...
    context = (_message("tool", 1), _message("user", 1), _message("tool", 4), _message("user", 1))
    result = reassemble_context(context, ModelLimits(context_tokens=200), estimator=lambda message: len(message.content))

    assert result.dropped == () and result.budget_tokens == 200
    assert result.used_tokens == sum(len(message.content) for message in context)

    # ...while a 1-token-per-message estimator with a tiny budget drops tools first.
    tight = reassemble_context(context, ModelLimits(context_tokens=2), estimator=lambda message: 1)
    assert [record.index for record in tight.dropped] == [0, 2]


def test_audit_records_content_privacy_and_mapping_shape() -> None:
    context = (_message("tool", 40, secret="LEAK"), _message("user", 2, secret="LAST"))
    result = reassemble_context(context, ModelLimits(context_tokens=5))

    mapping = result.to_mapping()
    assert set(mapping) == {"kept", "budget_tokens", "used_tokens", "truncated", "dropped", "reason"}
    assert mapping["dropped"][0] == {"index": 0, "role": "tool", "tokens": 41, "reason": "oldest tool output dropped to fit the target budget"}
    assert mapping["kept"] == [{"index": 1, "role": "user", "tokens": 3}]
    # Audit holds positions and sizes only — never message bodies.
    assert "LEAK" not in json.dumps(mapping)

    # The kept tuple still carries the real content for the caller.
    assert result.messages == (context[1],)


def test_reassembly_is_deterministic() -> None:
    context = tuple(_message(role, size) for role, size in (("system", 3), ("tool", 9), ("user", 7), ("assistant", 9), ("user", 3)))
    limits = ModelLimits(context_tokens=14)
    first = reassemble_context(context, limits).to_mapping()
    second = reassemble_context(context, limits).to_mapping()
    assert first == second
    assert first["kept"] == sorted(first["kept"], key=lambda item: item["index"])
    assert first["dropped"] == sorted(first["dropped"], key=lambda item: item["index"])


def test_descriptor_bridge_applies_target_limits() -> None:
    target = _descriptor("tiny:latest", context_tokens=12, max_output_tokens=4)
    context = (_message("system", 2), _message("tool", 20), _message("user", 4))
    result = reassemble_for_target(target, context)

    assert result.budget_tokens == 8
    assert [record.index for record in result.dropped] == [1]
    assert result.messages == (context[0], context[2])


def test_invalid_inputs_are_rejected_before_any_accounting() -> None:
    with pytest.raises(TypeError, match="tuple of ContextMessage"):
        reassemble_context([_message("user", 1)], ModelLimits(context_tokens=8))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tuple of ContextMessage"):
        reassemble_context(("plain string",), ModelLimits(context_tokens=8))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ModelLimits"):
        reassemble_context((), 4096)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable or None"):
        reassemble_context((), ModelLimits(context_tokens=8), estimator=42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ModelDescriptor"):
        reassemble_for_target("tiny:latest", (), )  # type: ignore[arg-type]


def test_invalid_budget_and_estimator_results_fail_closed() -> None:
    with pytest.raises(ContextReassemblyError, match="non-negative integer"):
        reassemble_context((), ModelLimits(context_tokens=8), reserve_output_tokens=-1)
    with pytest.raises(ContextReassemblyError, match="non-negative integer"):
        reassemble_context((), ModelLimits(context_tokens=8), reserve_output_tokens=True)
    with pytest.raises(ContextReassemblyError, match="fully consumed"):
        reassemble_context((), ModelLimits(context_tokens=8), reserve_output_tokens=8)
    with pytest.raises(ContextReassemblyError, match="negative token count"):
        reassemble_context((_message("user", 1),), ModelLimits(context_tokens=8), estimator=lambda message: -5)


def test_switched_context_reassembles_without_further_loss() -> None:
    """AS-052/AS-055 consistency: a context that already survived switch_model
    for the same budget is reassembled intact (zero additional drops)."""
    current = _descriptor("big:latest", context_tokens=8192)
    target = _descriptor("small:latest", context_tokens=30)
    context = tuple(_message(role, size) for role, size in (("system", 2), ("tool", 20), ("user", 8), ("assistant", 8)))

    decision = switch_model(current, target, TaskRequirements(), context)
    rebuilt = reassemble_for_target(target, decision.context, reserve_output_tokens=0)

    assert rebuilt.dropped == ()
    assert rebuilt.messages == decision.context
    assert rebuilt.used_tokens <= target.limits.context_tokens
    assert len(decision.context) < len(context) or decision.truncated_messages == 0
