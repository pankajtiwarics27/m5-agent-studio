from __future__ import annotations

from typing import cast

import pytest

from capability_negotiation import TaskRequirements
from comparison_board import CostObservation
from execution_budget import BudgetLimits
from hardware_capabilities import HardwareProfile
from model_calibration import CalibrationInputs, CalibrationSample, calibrate
from model_recommendation import RecommendationBudget, RecommendationError, manual_override, recommend_models
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from performance_profiles import BenchmarkMethodology, ProfileKey, ProfileRecord
from routing_policy import FallbackCandidate, FallbackConsent, FallbackPolicy

HARDWARE_SNAPSHOT = {"schema_version": 1, "operating_system": "Linux", "architecture": "x86_64"}
METHODOLOGY = BenchmarkMethodology("recommendation-tests", 1)


def _model(name: str = "alpha", digest: str = "sha256:aa", *, memory: int | None = 100) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=name,
        digest=digest,
        source="local",
        revision="r1",
        runtime="generic-runtime",
        limits=ModelLimits(context_tokens=4096, memory_bytes=memory),
        provenance=ModelProvenance("fixture", verification="verified"),
        quantization="Q4",
        capabilities=("chat",),
        trust="trusted",
    )


def _profile_entry(
    model: ModelDescriptor,
    *,
    values: tuple[tuple[float, int], ...] = ((40.0, 80), (42.0, 84), (38.0, 76)),
    hardware: dict | None = None,
    failures: int = 0,
):
    snapshot = HARDWARE_SNAPSHOT if hardware is None else hardware
    inputs = CalibrationInputs(
        model_id=model.model_id,
        model_digest=model.digest,
        runtime=model.runtime,
        runtime_version="1.2",
        quantization=model.quantization,
        context_length=4096,
        hardware=snapshot,
    )
    successful = tuple(
        CalibrationSample(
            sequence=index,
            kind="warm",
            ok=True,
            decode_tokens_per_s=latency,
            peak_memory_bytes=memory,
        )
        for index, (latency, memory) in enumerate(values, 1)
    )
    failed = tuple(
        CalibrationSample(sequence=len(values) + offset, kind="warm", ok=False, error="probe failure: TimeoutError")
        for offset in range(1, failures + 1)
    )
    samples = successful + failed
    profile = calibrate(inputs, samples)
    return ProfileKey.derive(profile, methodology=METHODOLOGY), ProfileRecord(1, "sha256:genesis", profile)


def _routes(*models: ModelDescriptor) -> dict[tuple[str, str], FallbackCandidate]:
    return {
        (model.model_id, model.digest): FallbackCandidate(model.model_id, "local", network_required=False)
        for model in models
    }


def _recommend(models: tuple[ModelDescriptor, ...], **kwargs):
    return recommend_models(
        models,
        task=TaskRequirements(required_capabilities=("chat",)),
        hardware=HardwareProfile("Linux", "x86_64", available_memory_bytes=1000),
        hardware_snapshot=HARDWARE_SNAPSHOT,
        routes=_routes(*models),
        **kwargs,
    )


def test_exact_confidence_calculation_and_order_are_deterministic() -> None:
    first, second = _model("alpha"), _model("beta", "sha256:bb")
    perf = {(first.model_id, first.digest): _profile_entry(first), (second.model_id, second.digest): _profile_entry(second)}
    result = _recommend((second, first), performance=perf)
    confidence = result.eligible[0].confidence
    assert [item.model.model_id for item in result.eligible] == ["alpha", "beta"]
    assert confidence.level == "high"
    assert confidence.sample_count == 3
    assert confidence.relative_spread == 0.1  # max(4/40, 8/80) from warm aggregates
    assert confidence.failure_rate == 0.0
    assert confidence.profile_digest == perf[("alpha", "sha256:aa")][1].profile_digest
    assert result.digest == _recommend((first, second), performance=perf).digest


def test_capability_memory_privacy_and_budget_exclusions_are_explicit() -> None:
    model = _model(memory=200)
    route = FallbackCandidate(model.model_id, "cloud", network_required=True, billable=True)
    result = recommend_models(
        (model,),
        task=TaskRequirements(required_capabilities=("vision",)),
        hardware=HardwareProfile("Linux", "x86_64", available_memory_bytes=100),
        hardware_snapshot=HARDWARE_SNAPSHOT,
        routes={(model.model_id, model.digest): route},
        policy=FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True),
        consent=FallbackConsent(),
        budget=RecommendationBudget(BudgetLimits(60, 10000, 0.001, 4), "USD", 2000),
    )
    item = result.recommendations[0]
    assert not item.eligible
    assert item.reasons == (
        "missing model capabilities: vision",
        "model memory requirement 200 exceeds available hardware memory 100",
        f"{model.model_id}: network fallback requires explicit consent",
        "budget cannot be checked without a caller-supplied price",
    )


def test_budget_uses_only_supplied_price_and_exact_token_estimate() -> None:
    model = _model()
    result = _recommend(
        (model,),
        costs={(model.model_id, model.digest): CostObservation(2.5, "USD", "fixture price")},
        budget=RecommendationBudget(BudgetLimits(60, 10000, 0.005, 4), "USD", 2000),
    )
    item = result.recommendations[0]
    assert item.eligible
    assert item.estimated_cost == 0.005
    over = _recommend(
        (model,),
        costs={(model.model_id, model.digest): CostObservation(2.5, "USD", "fixture price")},
        budget=RecommendationBudget(BudgetLimits(60, 10000, 0.004999, 4), "USD", 2000),
    ).recommendations[0]
    assert not over.eligible and "estimated cost exceeds budget" in over.reasons
    mismatch = _recommend(
        (model,),
        costs={(model.model_id, model.digest): CostObservation(0.01, "EUR", "fixture price")},
        budget=RecommendationBudget(BudgetLimits(60, 10000, 1, 4), "USD", 1000),
    ).recommendations[0]
    assert not mismatch.eligible and "price currency does not match budget currency" in mismatch.reasons
    token_over = _recommend(
        (model,),
        costs={(model.model_id, model.digest): CostObservation(0, "USD", "fixture price")},
        budget=RecommendationBudget(BudgetLimits(60, 1999, 1, 4), "USD", 2000),
    ).recommendations[0]
    assert not token_over.eligible and "estimated tokens exceed execution budget" in token_over.reasons


def test_profile_key_tampering_and_incomparable_methodology_do_not_raise_confidence() -> None:
    from performance_profiles import ProfileKey

    first, second = _model("alpha"), _model("beta", "sha256:bb")
    first_entry = _profile_entry(first)
    second_entry = _profile_entry(second)
    wrong_key = ProfileKey(
        hardware_digest=first_entry[0].hardware_digest,
        os_id=first_entry[0].os_id,
        model_digest="sha256:wrong",
        quantization=first_entry[0].quantization,
        runtime_version=first_entry[0].runtime_version,
        methodology_digest=first_entry[0].methodology_digest,
    )
    tampered = (wrong_key, first_entry[1])
    result = _recommend((first,), performance={(first.model_id, first.digest): tampered})
    assert result.recommendations[0].confidence.level == "unknown"
    assert "benchmark profile does not agree with its key" in result.recommendations[0].reasons

    other_method = BenchmarkMethodology("other-method", 1)
    second_profile = second_entry[1].profile
    second_key = ProfileKey.derive(second_profile, methodology=other_method)
    second_entry = (second_key, second_entry[1])
    mixed = _recommend(
        (first, second),
        performance={(first.model_id, first.digest): first_entry, (second.model_id, second.digest): second_entry},
    )
    alpha = next(item for item in mixed.recommendations if item.model.model_id == "alpha")
    beta = next(item for item in mixed.recommendations if item.model.model_id == "beta")
    assert alpha.confidence.level == "high"  # deterministic first profiled candidate is the anchor
    assert beta.confidence.level == "unknown"
    assert "incomparable" in " ".join(beta.reasons)


def test_benchmark_context_mismatch_is_not_trusted_and_not_compared() -> None:
    first, second = _model("alpha"), _model("beta", "sha256:bb")
    other_hardware = {"schema_version": 1, "operating_system": "OtherOS", "architecture": "x86_64"}
    result = _recommend(
        (first, second),
        performance={
            (first.model_id, first.digest): _profile_entry(first),
            (second.model_id, second.digest): _profile_entry(second, hardware=other_hardware),
        },
    )
    beta = next(item for item in result.recommendations if item.model.model_id == "beta")
    assert beta.confidence.level == "unknown"
    assert "benchmark hardware context differs from current hardware" in beta.reasons


def test_measured_memory_and_hardware_snapshot_consistency_are_enforced() -> None:
    model = _model(memory=None)
    entry = _profile_entry(model)
    result = recommend_models(
        (model,),
        task=TaskRequirements(),
        hardware=HardwareProfile("Linux", "x86_64", physical_memory_bytes=100, available_memory_bytes=50),
        hardware_snapshot=HARDWARE_SNAPSHOT,
        routes=_routes(model),
        performance={(model.model_id, model.digest): entry},
    )
    assert "measured warm memory exceeds available hardware memory" in result.recommendations[0].reasons
    with pytest.raises(RecommendationError, match="OS context disagree"):
        recommend_models(
            (model,),
            task=TaskRequirements(),
            hardware=HardwareProfile("OtherOS", "x86_64"),
            hardware_snapshot=HARDWARE_SNAPSHOT,
            routes=_routes(model),
        )


def test_explicit_network_and_billing_consent_is_required_and_sufficient() -> None:
    model = _model()
    route = FallbackCandidate(model.model_id, "cloud-provider", network_required=True, billable=True)
    kwargs = dict(
        task=TaskRequirements(),
        hardware=None,
        hardware_snapshot=HARDWARE_SNAPSHOT,
        routes={(model.model_id, model.digest): route},
        policy=FallbackPolicy(allow_network_fallback=True, allow_billable_fallback=True),
    )
    denied = recommend_models((model,), consent=FallbackConsent(), **kwargs).recommendations[0]
    assert not denied.eligible
    assert denied.reasons == (f"{model.model_id}: network fallback requires explicit consent",)
    allowed = recommend_models(
        (model,), consent=FallbackConsent("consent-123", network_approved=True, billable_approved=True), **kwargs
    ).recommendations[0]
    assert allowed.eligible and allowed.reasons == ()


def test_low_confidence_from_spread_and_failures_and_missing_profile() -> None:
    model = _model()
    noisy = _profile_entry(model, values=((10.0, 10), (50.0, 50), (30.0, 30)))
    result = _recommend((model,), performance={(model.model_id, model.digest): noisy})
    assert result.recommendations[0].confidence.level == "low"
    assert result.recommendations[0].confidence.relative_spread == 1.333333
    threshold_profile = _profile_entry(model, failures=1)
    threshold = _recommend((model,), performance={(model.model_id, model.digest): threshold_profile}).recommendations[0].confidence
    assert threshold.failure_rate == 0.25 and threshold.level == "high"
    failed_profile = _profile_entry(model, failures=2)
    failed = _recommend((model,), performance={(model.model_id, model.digest): failed_profile}).recommendations[0].confidence
    assert failed.failure_rate == 0.4 and failed.level == "low"
    missing = _recommend((model,)).recommendations[0].confidence
    assert missing.level == "unknown" and missing.profile_digest is None


def test_route_context_is_fail_closed_and_override_bypasses_advisory_gate() -> None:
    model = _model()
    denied = recommend_models(
        (model,),
        task=TaskRequirements(),
        hardware=None,
        hardware_snapshot=HARDWARE_SNAPSHOT,
        routes={},
    ).recommendations[0]
    assert not denied.eligible and denied.reasons == ("privacy route context is missing",)
    assert manual_override(model) is model
    with pytest.raises(TypeError, match="manual override"):
        manual_override(cast(ModelDescriptor, "alpha"))


def test_all_input_validation_refusals() -> None:
    model = _model()
    common = dict(task=TaskRequirements(), hardware=None, hardware_snapshot=HARDWARE_SNAPSHOT, routes=_routes(model))
    with pytest.raises(RecommendationError, match="non-empty"):
        recommend_models((), **common)
    with pytest.raises(RecommendationError, match="unique"):
        recommend_models((model, model), **common)
    with pytest.raises(TypeError, match="TaskRequirements"):
        recommend_models((model,), task=cast(TaskRequirements, "task"), hardware=None, hardware_snapshot=HARDWARE_SNAPSHOT, routes=_routes(model))
    with pytest.raises(TypeError, match="FallbackCandidate"):
        recommend_models((model,), **{**common, "routes": {(model.model_id, model.digest): "route"}})
    with pytest.raises(RecommendationError, match="does not match"):
        recommend_models(
            (model,), **{**common, "routes": {(model.model_id, model.digest): FallbackCandidate("other", "local")}}
        )
    with pytest.raises(TypeError, match="(ProfileKey, ProfileRecord)"):
        recommend_models((model,), **{**common, "performance": {(model.model_id, model.digest): "bad"}})
    with pytest.raises(RecommendationError, match="currency"):
        RecommendationBudget(BudgetLimits(60, 1000, 1, 2), "usd", 10)
    with pytest.raises(RecommendationError, match="estimated_tokens"):
        RecommendationBudget(BudgetLimits(60, 1000, 1, 2), "USD", -1)
    with pytest.raises(TypeError, match="CostObservation"):
        _recommend(
            (model,),
            costs={(model.model_id, model.digest): "free"},
            budget=RecommendationBudget(BudgetLimits(60, 1000, 1, 2), "USD", 100),
        )
    with pytest.raises(TypeError, match="HardwareProfile"):
        recommend_models((model,), **{**common, "hardware": "machine"})
    with pytest.raises(TypeError, match="hardware_snapshot"):
        recommend_models((model,), **{**common, "hardware_snapshot": "machine"})
    with pytest.raises(TypeError, match="routes must be a mapping"):
        recommend_models((model,), **{**common, "routes": "routes"})
    with pytest.raises(TypeError, match="performance must be a mapping"):
        recommend_models((model,), **{**common, "performance": "profiles"})
    with pytest.raises(TypeError, match="costs must be a mapping"):
        recommend_models((model,), **{**common, "costs": "prices"})
    with pytest.raises(TypeError, match="RecommendationBudget"):
        recommend_models((model,), **{**common, "budget": "budget"})
    with pytest.raises(TypeError, match="FallbackPolicy"):
        recommend_models((model,), **{**common, "policy": "policy"})
    with pytest.raises(TypeError, match="FallbackConsent"):
        recommend_models((model,), **{**common, "consent": "consent"})
    with pytest.raises(TypeError, match="limits must be a BudgetLimits"):
        RecommendationBudget("limits", "USD", 10)
