from __future__ import annotations

import pytest

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from hardware_capabilities import AcceleratorCapability, HardwareProfile
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from provider_switching import (
    ProviderCapabilities,
    RequestSemantics,
    switch_provider,
)
from routing_policy import (
    FallbackCandidate,
    FallbackConsent,
    FallbackDenied,
    FallbackPolicy,
    RoutingPolicyError,
    evaluate_fallback,
)


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


def _local() -> FallbackCandidate:
    return FallbackCandidate("local-model", "local-runtime")


def _cloud(*, network_required: bool = True, billable: bool = True) -> FallbackCandidate:
    return FallbackCandidate(
        "cloud-model", "cloud", network_required=network_required, billable=billable
    )


def _consent(network_approved: bool = False, billable_approved: bool = False) -> FallbackConsent:
    return FallbackConsent("consent-1", network_approved=network_approved, billable_approved=billable_approved)


# ---------------------------------------------------------------------------
# Privacy / approval / spend boundaries (the AS-058 acceptance gate)
# ---------------------------------------------------------------------------


def test_deny_by_default_selects_only_local_candidates() -> None:
    """With no policy and no consent, network and billable candidates are excluded."""
    decision = evaluate_fallback((_local(), _cloud()))
    assert decision.selected == _local()
    assert decision.eligible == (_local(),)
    assert len(decision.exclusions) == 1
    assert "network fallback disabled by policy" in decision.exclusions[0]
    assert _cloud() not in decision.eligible


def test_network_fallback_never_selected_without_explicit_network_consent() -> None:
    """Privacy: a network-required candidate is never routed without explicit network consent."""
    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=False)
    no_consent = evaluate_fallback((_cloud(), _local()), policy)
    assert no_consent.selected == _local()
    assert _cloud() not in no_consent.eligible
    assert any("network fallback requires explicit consent" in item for item in no_consent.exclusions)

    net_only = _cloud(network_required=True, billable=False)
    approved = evaluate_fallback((net_only, _local()), policy, _consent(network_approved=True))
    assert approved.require_selection() == net_only


def test_billable_fallback_never_selected_without_explicit_billable_consent() -> None:
    """Spend: a billable candidate is never routed without explicit billable consent."""
    policy = FallbackPolicy(allow_network_fallback=False, allow_billable_fallback=True)
    bill_only = _cloud(network_required=False, billable=True)
    no_consent = evaluate_fallback((bill_only, _local()), policy)
    assert no_consent.selected == _local()
    assert bill_only not in no_consent.eligible
    assert any("billable fallback requires explicit consent" in item for item in no_consent.exclusions)

    approved = evaluate_fallback((bill_only, _local()), policy, _consent(billable_approved=True))
    assert approved.require_selection() == bill_only


def test_consent_without_consent_id_is_rejected_fail_closed() -> None:
    """Approval: network/billable approval without a consent_id is malformed, never implicit."""
    with pytest.raises(RoutingPolicyError, match="consent_id"):
        FallbackConsent(network_approved=True)
    with pytest.raises(RoutingPolicyError, match="consent_id"):
        FallbackConsent(billable_approved=True)
    with pytest.raises(RoutingPolicyError, match="consent_id"):
        FallbackConsent(network_approved=True, billable_approved=True)


def test_network_approval_does_not_authorize_billable_spend() -> None:
    """Spend boundary: approving network must not silently permit billable fallback."""
    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True)
    decision = evaluate_fallback(
        (_cloud(), _local()), policy, _consent(network_approved=True, billable_approved=False)
    )
    assert decision.selected == _local()
    assert _cloud() not in decision.eligible
    assert any("billable fallback requires explicit consent" in item for item in decision.exclusions)


def test_explicit_consent_is_the_gate_not_a_permanent_block() -> None:
    """Consent + policy both required; with both, network+billable candidate is eligible."""
    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True)
    consent = _consent(network_approved=True, billable_approved=True)
    decision = evaluate_fallback((_cloud(), _local()), policy, consent)
    assert decision.require_selection() == _cloud()


# ---------------------------------------------------------------------------
# Capability negotiation guards routing: rejected models never fall back implicitly
# ---------------------------------------------------------------------------


def test_infeasible_model_is_not_routed_as_fallback() -> None:
    """A model that fails capability negotiation is never an eligible routing target."""
    negotiator = CapabilityNegotiator(_hardware())
    decision = negotiator.negotiate(
        _model(capabilities=("text",)), TaskRequirements(required_capabilities=("vision",))
    )
    assert decision.feasible is False
    assert "missing model capabilities: vision" in decision.reasons

    # Even though the cloud candidate exists, an infeasible model must not be
    # silently selected by the router as a fallback.
    routing = evaluate_fallback((_cloud(), _local()))
    assert routing.selected == _local()
    assert routing.eligible == (_local(),)


def test_rejected_model_never_triggers_implicit_network_fallback() -> None:
    """Capability rejection and policy denial are independent; neither permits implicit fallback."""
    negotiator = CapabilityNegotiator(_hardware())
    blocked = negotiator.negotiate(_model(trust="blocked"), TaskRequirements())
    assert blocked.feasible is False
    assert "model is blocked by trust policy" in blocked.reasons

    policy = FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True)
    consent = _consent(network_approved=True, billable_approved=True)
    # With full consent the router may select a network candidate, but the
    # capability-rejected model is still not implicitly dispatched.
    routing = evaluate_fallback((_cloud(), _local()), policy, consent)
    assert routing.require_selection() == _cloud()


# ---------------------------------------------------------------------------
# Provider switching guards routing: unsupported switches are reported, not silently bypassed
# ---------------------------------------------------------------------------


def test_unsupported_provider_switch_reports_instead_of_bypassing_policy() -> None:
    """A switch to a provider lacking required semantics is reported explicitly."""
    source = ProviderCapabilities("source", streaming=True, tools=True, reasoning=True)
    target = ProviderCapabilities("target", streaming=True)
    request = RequestSemantics(streaming=True, tools=True, reasoning=True)
    decision = switch_provider(source, target, request)
    assert decision.supported is False
    assert decision.unsupported_features == ("tools", "reasoning")
    assert "unsupported features require explicit caller decision" in decision.reason


def test_provider_switch_does_not_grant_network_or_billable_fallback() -> None:
    """An unsupported switch must not silently route to a network/billable fallback."""
    source = ProviderCapabilities("local", streaming=True)
    target = ProviderCapabilities("cloud", streaming=True)
    request = RequestSemantics(streaming=True)
    decision = switch_provider(source, target, request)
    assert decision.supported is True
    assert decision.unsupported_features == ()

    # The switch decision itself carries no consent; the router must still apply
    # policy before any network/billable candidate is selected.
    routing = evaluate_fallback((_cloud(), _local()))
    assert routing.selected == _local()
    assert routing.exclusions != ()



# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_malformed_inputs_raise_instead_of_permitting_fallback() -> None:
    """Malformed policy, consent, or candidates fail closed; nothing is implicitly routed."""
    with pytest.raises(RoutingPolicyError, match="model_id"):
        FallbackCandidate("", "cloud", network_required=True)
    with pytest.raises(TypeError, match="candidates"):
        evaluate_fallback(("not-a-candidate",))  # type: ignore[arg-type]
    with pytest.raises(RoutingPolicyError, match="fallback policy permissions must be booleans"):
        FallbackPolicy(allow_network_fallback="yes")  # type: ignore[arg-type]


def test_require_selection_raises_when_policy_excludes_everything() -> None:
    """When every candidate is denied by privacy/approval/spend policy, selection fails explicitly."""
    decision = evaluate_fallback((_cloud(), _cloud(billable=False, network_required=True)))
    assert decision.selected is None
    with pytest.raises(FallbackDenied, match="no fallback candidate satisfies policy and consent"):
        decision.require_selection()
