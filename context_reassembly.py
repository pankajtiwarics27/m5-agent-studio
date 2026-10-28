"""Auditable context reassembly within a target model's context window.

AS-055 rebuilds a workflow transcript so it fits the *target* model's budget
after (or before) an AS-052 model switch. Unlike the newest-fitting-messages
policy of ``model_switching.switch_model``, reassembly is tiered: large tool
outputs are dropped before conversation turns, anchors (system/developer
instructions) and the newest message are never dropped, output-token room is
reserved from the target's declared limits, and every dropped message is
reported with its original index, role, token estimate, and reason. If the
mandatory messages alone cannot fit, reassembly fails audibly — no silent or
partial loss of required content occurs, and message bodies are never
mid-truncated (out of scope by design).

Pure data function: no provider call, model load, tokenizer, or network.
Token counts are estimates unless the caller injects an estimator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from model_registry import ModelDescriptor, ModelLimits
from model_switching import ContextMessage

TOOL_ROLES = frozenset({"tool", "function"})
ANCHOR_ROLES = frozenset({"system", "developer"})


class ContextReassemblyError(RuntimeError):
    """Raised when reassembly cannot satisfy the target budget fail-closed."""


TokenEstimator = Callable[[ContextMessage], int]


def _default_estimate(message: ContextMessage) -> int:
    """Word-count heuristic, deliberately consistent with AS-052 trimming."""
    return max(1, len(message.content.split()))


@dataclass(frozen=True, slots=True)
class DroppedMessage:
    """Audit record for one excluded message; the content itself is never copied."""

    index: int
    role: str
    tokens: int
    reason: str

    def to_mapping(self) -> dict[str, Any]:
        return {"index": self.index, "role": self.role, "tokens": self.tokens, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ReassembledContext:
    """The rebuilt transcript plus its exact budget accounting.

    ``kept_indices`` and ``kept_tokens`` align one-to-one with ``messages`` and
    refer to positions in the original context tuple, so the audit fully
    explains the rebuild without copying content into the mapping.
    """

    messages: tuple[ContextMessage, ...]
    kept_indices: tuple[int, ...]
    kept_tokens: tuple[int, ...]
    budget_tokens: int
    used_tokens: int
    dropped: tuple[DroppedMessage, ...]
    reason: str

    @property
    def truncated(self) -> bool:
        return bool(self.dropped)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kept": [
                {"index": index, "role": message.role, "tokens": tokens}
                for index, message, tokens in zip(self.kept_indices, self.messages, self.kept_tokens, strict=True)
            ],
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "truncated": self.truncated,
            "dropped": [record.to_mapping() for record in self.dropped],
            "reason": self.reason,
        }


def _tier(message: ContextMessage, *, is_last: bool) -> int | None:
    """Drop priority: smaller tier drops first; ``None`` marks never-droppable."""
    if is_last or message.role in ANCHOR_ROLES:
        return None
    if message.role in TOOL_ROLES:
        return 0
    if message.role == "assistant":
        return 1
    return 2


def reassemble_context(
    context: tuple[ContextMessage, ...],
    limits: ModelLimits,
    *,
    estimator: TokenEstimator | None = None,
    reserve_output_tokens: int | None = None,
) -> ReassembledContext:
    """Rebuild ``context`` to fit ``limits``, preferring large droppable turns.

    Tool/function outputs are dropped oldest-first, then assistant turns, then
    other droppable messages. Anchors and the newest message are mandatory.
    ``reserve_output_tokens`` defaults to the target's declared maximum output
    so the rebuilt prompt leaves room for the reply.
    """
    if not isinstance(context, tuple) or not all(isinstance(message, ContextMessage) for message in context):
        raise TypeError("context must be a tuple of ContextMessage values.")
    if not isinstance(limits, ModelLimits):
        raise TypeError("limits must be a ModelLimits value.")
    if estimator is not None:
        if not callable(estimator):
            raise TypeError("estimator must be a callable or None.")
    else:
        estimator = _default_estimate
    if reserve_output_tokens is None:
        reserve_output_tokens = limits.max_output_tokens or 0
    if isinstance(reserve_output_tokens, bool) or not isinstance(reserve_output_tokens, int) or reserve_output_tokens < 0:
        raise ContextReassemblyError("reserve_output_tokens must be a non-negative integer.")
    budget = limits.context_tokens - reserve_output_tokens
    if budget <= 0:
        raise ContextReassemblyError("target context window is fully consumed by the output reservation.")

    tokens = [int(estimator(message)) for message in context]
    if any(count < 0 for count in tokens):
        raise ContextReassemblyError("estimator returned a negative token count.")
    total = sum(tokens)
    last_index = len(context) - 1
    drop_order = sorted(
        (
            (tier, index)
            for index, message in enumerate(context)
            if (tier := _tier(message, is_last=index == last_index)) is not None
        )
    )

    dropped_by_index: dict[int, DroppedMessage] = {}
    for tier, index in drop_order:
        if total <= budget:
            break
        message = context[index]
        label = {0: "oldest tool output", 1: "oldest assistant turn", 2: "oldest droppable turn"}[tier]
        dropped_by_index[index] = DroppedMessage(
            index=index,
            role=message.role,
            tokens=tokens[index],
            reason=f"{label} dropped to fit the target budget",
        )
        total -= tokens[index]

    if total > budget:
        raise ContextReassemblyError(
            f"mandatory messages ({total} estimated tokens) exceed the target budget of {budget} tokens."
        )

    kept_indices = tuple(index for index in range(len(context)) if index not in dropped_by_index)
    reason = (
        "context fits the target budget without truncation"
        if not dropped_by_index
        else f"reassembled to {total} estimated tokens within a {budget}-token budget; dropped {len(dropped_by_index)} messages"
    )
    return ReassembledContext(
        messages=tuple(context[index] for index in kept_indices),
        kept_indices=kept_indices,
        kept_tokens=tuple(tokens[index] for index in kept_indices),
        budget_tokens=budget,
        used_tokens=total,
        dropped=tuple(dropped_by_index[index] for index in sorted(dropped_by_index)),
        reason=reason,
    )


def reassemble_for_target(
    target: ModelDescriptor,
    context: tuple[ContextMessage, ...],
    *,
    estimator: TokenEstimator | None = None,
    reserve_output_tokens: int | None = None,
) -> ReassembledContext:
    """Bridge used at switch time: rebuild against the target descriptor's limits."""
    if not isinstance(target, ModelDescriptor):
        raise TypeError("target must be a ModelDescriptor value.")
    return reassemble_context(
        context,
        target.limits,
        estimator=estimator,
        reserve_output_tokens=reserve_output_tokens,
    )
