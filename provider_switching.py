"""Provider switching decisions with explicit unsupported-feature reporting."""

from __future__ import annotations

from dataclasses import dataclass


class ProviderSwitchError(RuntimeError):
    """Raised when a provider switch would violate request semantics."""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    streaming: bool = True
    tools: bool = False
    structured_output: bool = False
    reasoning: bool = False


@dataclass(frozen=True, slots=True)
class RequestSemantics:
    streaming: bool = False
    tools: bool = False
    structured_output: bool = False
    reasoning: bool = False


@dataclass(frozen=True, slots=True)
class ProviderSwitchDecision:
    source_provider: str
    target_provider: str
    supported: bool
    unsupported_features: tuple[str, ...]
    preserved: RequestSemantics
    reason: str


def switch_provider(
    source: ProviderCapabilities,
    target: ProviderCapabilities,
    request: RequestSemantics,
) -> ProviderSwitchDecision:
    """Check target support without making a request or changing the payload."""
    if not isinstance(source, ProviderCapabilities) or not isinstance(target, ProviderCapabilities):
        raise TypeError("source and target must be ProviderCapabilities values.")
    if not isinstance(request, RequestSemantics):
        raise TypeError("request must be a RequestSemantics value.")
    unsupported = tuple(
        feature
        for feature, requested, available in (
            ("streaming", request.streaming, target.streaming),
            ("tools", request.tools, target.tools),
            ("structured_output", request.structured_output, target.structured_output),
            ("reasoning", request.reasoning, target.reasoning),
        )
        if requested and not available
    )
    supported = not unsupported
    reason = "request semantics preserved" if supported else "unsupported features require explicit caller decision"
    return ProviderSwitchDecision(source.provider, target.provider, supported, unsupported, request, reason)
