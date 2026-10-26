"""Explicit privacy, network, and billing policy for model fallback."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


class RoutingPolicyError(ValueError):
    """Raised when a policy or consent record is malformed."""


class FallbackDenied(RuntimeError):
    """Raised by callers that require a selected fallback and received none."""


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RoutingPolicyError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class FallbackCandidate:
    """A candidate's externally observable network and billing requirements."""

    model_id: str
    provider: str
    network_required: bool = False
    billable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _non_empty(self.model_id, "model_id"))
        object.__setattr__(self, "provider", _non_empty(self.provider, "provider"))
        if not isinstance(self.network_required, bool) or not isinstance(self.billable, bool):
            raise RoutingPolicyError("network_required and billable must be booleans.")


@dataclass(frozen=True, slots=True)
class FallbackPolicy:
    """Operator policy; both network and billing defaults are deny."""

    allow_network_fallback: bool = False
    allow_billable_fallback: bool = False
    permitted_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allow_network_fallback, bool) or not isinstance(self.allow_billable_fallback, bool):
            raise RoutingPolicyError("fallback policy permissions must be booleans.")
        providers = tuple(sorted({_non_empty(provider, "permitted_providers") for provider in self.permitted_providers}))
        object.__setattr__(self, "permitted_providers", providers)


@dataclass(frozen=True, slots=True)
class FallbackConsent:
    """Per-request explicit consent, identified for audit and replay review."""

    consent_id: str | None = None
    network_approved: bool = False
    billable_approved: bool = False

    def __post_init__(self) -> None:
        if self.consent_id is not None:
            object.__setattr__(self, "consent_id", _non_empty(self.consent_id, "consent_id"))
        if not isinstance(self.network_approved, bool) or not isinstance(self.billable_approved, bool):
            raise RoutingPolicyError("consent approvals must be booleans.")
        if (self.network_approved or self.billable_approved) and self.consent_id is None:
            raise RoutingPolicyError("approved fallback consent requires consent_id.")


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """Policy result with eligible candidates and explicit exclusion reasons."""

    selected: FallbackCandidate | None
    eligible: tuple[FallbackCandidate, ...]
    exclusions: tuple[str, ...]

    def require_selection(self) -> FallbackCandidate:
        if self.selected is None:
            raise FallbackDenied("no fallback candidate satisfies policy and consent.")
        return self.selected


def _exclusion(candidate: FallbackCandidate, policy: FallbackPolicy, consent: FallbackConsent) -> str | None:
    if policy.permitted_providers and candidate.provider not in policy.permitted_providers:
        return f"{candidate.model_id}: provider is not permitted by policy"
    if candidate.network_required:
        if not policy.allow_network_fallback:
            return f"{candidate.model_id}: network fallback disabled by policy"
        if not consent.network_approved or consent.consent_id is None:
            return f"{candidate.model_id}: network fallback requires explicit consent"
    if candidate.billable:
        if not policy.allow_billable_fallback:
            return f"{candidate.model_id}: billable fallback disabled by policy"
        if not consent.billable_approved or consent.consent_id is None:
            return f"{candidate.model_id}: billable fallback requires explicit consent"
    return None


def evaluate_fallback(
    candidates: Sequence[FallbackCandidate],
    policy: FallbackPolicy | None = None,
    consent: FallbackConsent | None = None,
) -> FallbackDecision:
    """Select the first eligible candidate without performing any fallback."""
    active_policy = policy or FallbackPolicy()
    active_consent = consent or FallbackConsent()
    eligible: list[FallbackCandidate] = []
    exclusions: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, FallbackCandidate):
            raise TypeError("candidates must contain FallbackCandidate values.")
        reason = _exclusion(candidate, active_policy, active_consent)
        if reason is None:
            eligible.append(candidate)
        else:
            exclusions.append(reason)
    return FallbackDecision(eligible[0] if eligible else None, tuple(eligible), tuple(exclusions))
