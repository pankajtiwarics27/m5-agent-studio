from __future__ import annotations

import pytest

from routing_policy import (
    FallbackCandidate,
    FallbackConsent,
    FallbackDenied,
    FallbackPolicy,
    RoutingPolicyError,
    evaluate_fallback,
)


def test_local_candidate_is_selected_by_default_without_network_or_billing() -> None:
    local = FallbackCandidate("local-model", "local-runtime")
    cloud = FallbackCandidate("cloud-model", "cloud", network_required=True, billable=True)
    decision = evaluate_fallback((local, cloud))
    assert decision.selected == local
    assert decision.eligible == (local,)
    assert any("cloud-model: network fallback disabled" in item for item in decision.exclusions)


def test_network_and_billable_fallback_require_policy_and_explicit_consent() -> None:
    candidate = FallbackCandidate("cloud-model", "cloud", network_required=True, billable=True)
    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True)

    without_consent = evaluate_fallback((candidate,), policy)
    assert without_consent.selected is None
    assert "explicit consent" in " ".join(without_consent.exclusions)

    consent = FallbackConsent("consent-1", network_approved=True, billable_approved=True)
    approved = evaluate_fallback((candidate,), policy, consent)
    assert approved.require_selection() == candidate


def test_network_only_consent_does_not_authorize_billable_fallback() -> None:
    candidate = FallbackCandidate("paid-model", "cloud", network_required=True, billable=True)
    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True)
    decision = evaluate_fallback((candidate,), policy, FallbackConsent("consent-2", network_approved=True))
    assert decision.selected is None
    assert any("billable fallback requires explicit consent" in item for item in decision.exclusions)


def test_provider_allowlist_and_no_selection_are_explicit() -> None:
    candidate = FallbackCandidate("cloud-model", "unapproved-cloud", network_required=True)
    policy = FallbackPolicy(allow_network_fallback=True, permitted_providers=("approved-cloud",))
    decision = evaluate_fallback((candidate,), policy, FallbackConsent("consent-3", network_approved=True))
    assert decision.selected is None
    with pytest.raises(FallbackDenied, match="no fallback candidate"):
        decision.require_selection()


def test_malformed_policy_consent_and_candidates_fail_closed() -> None:
    with pytest.raises(RoutingPolicyError, match="consent_id"):
        FallbackConsent(network_approved=True)
    with pytest.raises(RoutingPolicyError, match="model_id"):
        FallbackCandidate("", "local")
    with pytest.raises(TypeError, match="candidates"):
        evaluate_fallback(("not-a-candidate",))  # type: ignore[arg-type]
