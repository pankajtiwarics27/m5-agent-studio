"""AP-014 offline tests: policy-aware routing and resource-safe scheduling.

Nothing here loads a model, starts a runtime or issues inference. Candidates are
plain values with a capability profile; the module's job is to decide — and to
record why not.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

import model_capability_profiles as M
import routing_decision_records as R
from routing_decision_records import (
    GATE_ORDER,
    AccountingGap,
    BaselineComparison,
    CandidateVerdict,
    GateResult,
    LocalCandidate,
    ManualOverrideRequest,
    MemoryAccounting,
    ResidencyObservation,
    RoutingDecisionError,
    RoutingDecisionRecord,
    ScheduleStep,
    ScopedLimitation,
    StageRequirement,
    accounting_gap,
    apply_manual_override,
    compatibility_gate,
    decide_route,
    grant_gate,
    plan_sequential_schedule,
    quality_gate,
    refuse_hosted_fallback,
    render_decisions,
    residency_gate,
    resource_fit_gate,
    validate_sequential_plan,
)

DIGEST = "a" * 64
NOW = "2026-11-22T03:00:00Z"


def identity(
    model_id: str = "qwen3-coder-30b", **overrides: Any
) -> M.ConfigurationIdentity:
    base: dict[str, Any] = dict(
        model_id=model_id,
        checkpoint_digest=DIGEST,
        checkpoint_revision="main@1f2e",
        runtime="llama.cpp",
        runtime_version="b4010",
        quantization="Q4_K_M",
        template="chatml",
        context_length=32768,
        hardware_fingerprint="hw-1",
    )
    base.update(overrides)
    return M.ConfigurationIdentity(**base)


def measured_profile(
    model_id: str = "qwen3-coder-30b",
    *,
    kind: M.TaskKind = "coding",
    values: tuple[float, ...] = (0.8, 0.8, 0.8),
    features: tuple[tuple[str, M.CompatibilityState], ...] = (),
) -> M.CapabilityProfile:
    ident = identity(model_id)
    probes = [
        M.TaskProbe(
            task_kind=kind,
            configuration_digest=ident.digest,
            score=v,
            passed=True,
            measured_at=NOW,
        )
        for v in values
    ]
    measurement = M.summarise_task_probes(ident, probes, kind=kind, measured_at=NOW)
    return M.CapabilityProfile(
        identity=ident,
        measurements={kind: measurement},
        compatibility=tuple(
            M.CompatibilityClaim(feature=feature, state=state, evidence="probe")
            for feature, state in features
        ),
    )


def candidate(
    model_id: str = "qwen3-coder-30b",
    *,
    profile: M.CapabilityProfile | None = None,
    resident_bytes: int = 1000,
    grants: tuple[str, ...] = ("path:/ws:out",),
) -> LocalCandidate:
    return LocalCandidate(
        model_id=model_id,
        provider="local-omlx",
        profile=profile or measured_profile(model_id),
        resident_bytes=resident_bytes,
        grants=grants,
    )


def stage(
    *,
    stage_kind: R.StageKind = "draft",
    task_kind: M.TaskKind = "coding",
    minimum: float = 0.5,
    features: tuple[str, ...] = (),
    grants: tuple[str, ...] = ("path:/ws:out",),
) -> StageRequirement:
    return StageRequirement(
        stage=stage_kind,
        task_kind=task_kind,
        minimum_quality=minimum,
        required_features=features,
        required_grants=grants,
    )


def decision(
    *,
    candidates: list[LocalCandidate] | None = None,
    stage_req: StageRequirement | None = None,
    resident: str | None = None,
    available: int = 10_000,
    prefer: str | None = None,
) -> RoutingDecisionRecord:
    return decide_route(
        decision_id="dec-1",
        stage=stage_req or stage(),
        candidates=candidates if candidates is not None else [candidate()],
        resident_model_id=resident,
        available_bytes=available,
        now=NOW,
        prefer=prefer,
    )


# ---------------------------------------------------------------------------
# AC1 — every gate precedes dispatch, and every gate is recorded
# ---------------------------------------------------------------------------


def test_the_five_gates_run_in_a_fixed_order():
    assert GATE_ORDER == (
        "compatibility",
        "measured_quality",
        "grants",
        "residency",
        "resource_fit",
    )


def test_a_clean_candidate_passes_every_gate():
    record = decision()
    gates = record.verdicts[0].gates
    assert tuple(g.gate for g in gates) == GATE_ORDER
    assert all(g.status == "passed" for g in gates)


def test_the_decision_records_every_gate_even_after_a_block():
    """Silence is not a pass: unreached gates are recorded as not_reached."""
    record = decision(candidates=[candidate(grants=())])
    gates = record.verdicts[0].gates
    assert tuple(g.gate for g in gates) == GATE_ORDER
    assert gates[0].status == "passed"
    assert gates[2].status == "failed"  # grants
    assert gates[3].status == "not_reached"
    assert gates[4].status == "not_reached"


def test_an_unverified_feature_blocks_rather_than_passing():
    cand = candidate(profile=measured_profile(features=(("vision", "unknown"),)))
    result = compatibility_gate(cand, required_features=["vision"])
    assert result.status == "unknown"
    assert result.is_blocking is True


def test_an_unsupported_feature_fails_the_compatibility_gate():
    cand = candidate(profile=measured_profile(features=(("schema", "unsupported"),)))
    result = compatibility_gate(cand, required_features=["schema"])
    assert result.status == "failed"
    assert "does not support" in result.reason


def test_a_required_feature_never_mentioned_is_unknown_not_passed():
    result = compatibility_gate(candidate(), required_features=["audio"])
    assert result.status == "unknown"


def test_quality_below_the_minimum_fails():
    result = quality_gate(candidate(), stage="coding", minimum=0.9, now=NOW)
    assert result.status == "failed"
    assert "below the required" in result.reason


def test_a_claim_is_not_quality_evidence():
    ident = identity()
    card = M.Measurement(
        value=0.99,
        provenance="model_card",
        source="card",
        measured_at=NOW,
        sample_count=9,
    )
    prof = M.CapabilityProfile(identity=ident, measurements={"coding": card})
    result = quality_gate(candidate(profile=prof), stage="coding", minimum=0.5, now=NOW)
    assert result.status == "unknown"
    assert "not measured quality" in result.reason


def test_unstable_quality_blocks_as_indicative():
    prof = measured_profile(values=(0.9, 0.2))
    result = quality_gate(candidate(profile=prof), stage="coding", minimum=0.5, now=NOW)
    assert result.status == "unknown"
    assert "indicative" in result.reason


def test_an_unmeasured_stage_kind_is_unknown():
    result = quality_gate(candidate(), stage="writing", minimum=0.1, now=NOW)
    assert result.status == "unknown"


def test_a_missing_grant_fails_the_grant_gate():
    result = grant_gate(
        candidate(grants=("path:/ws:other",)), required_grants=["path:/ws:out"]
    )
    assert result.status == "failed"
    assert result.detail["missing_grants"] == ["path:/ws:out"]


def test_a_broader_grant_subsumes_a_narrower_requirement():
    result = grant_gate(
        candidate(grants=("workspace:/ws",)), required_grants=["path:/ws:out"]
    )
    assert result.status == "passed"


def test_residency_gate_refuses_a_different_resident_model():
    result = residency_gate(candidate("a"), resident_model_id="b")
    assert result.status == "failed"
    assert "will not hold two resident models" in result.reason


def test_the_resident_model_may_serve_the_next_stage():
    result = residency_gate(candidate("a"), resident_model_id="a")
    assert result.status == "passed"


def test_resource_fit_uses_free_memory_not_total():
    result = resource_fit_gate(candidate(resident_bytes=1000), available_bytes=999)
    assert result.status == "failed"
    assert result.detail["available_bytes"] == 999


def test_negative_available_memory_is_refused():
    with pytest.raises(RoutingDecisionError):
        resource_fit_gate(candidate(), available_bytes=-1)


def test_a_candidate_blocked_on_grants_still_reports_that_it_would_fit():
    """An operator needs both facts: no grant, and the host would have held it."""
    record = decision(candidates=[candidate(grants=())], available=10_000)
    gates = {g.gate: g for g in record.verdicts[0].gates}
    assert gates["grants"].status == "failed"
    assert gates["resource_fit"].status == "not_reached"
    assert "required_bytes" not in gates["resource_fit"].detail


# ---------------------------------------------------------------------------
# AC3 — local only; hosted fallback refused; scoped limitation instead
# ---------------------------------------------------------------------------


def test_a_hosted_candidate_is_refused_at_construction():
    with pytest.raises(R.HostedRoutingRefused, match="stays local"):
        LocalCandidate(
            model_id="gpt-x",
            provider="hosted",
            profile=measured_profile(),
            resident_bytes=0,
            is_hosted=True,
        )


def test_there_is_no_hosted_fallback_to_call():
    with pytest.raises(R.HostedRoutingRefused, match="no hosted fallback"):
        refuse_hosted_fallback()
    assert R.HOSTED_FALLBACK_AVAILABLE is False
    assert "no hosted fallback" in R.HOSTED_FALLBACK_REFUSAL


def test_when_nothing_qualifies_the_decision_reports_a_limitation():
    record = decision(candidates=[candidate()], stage_req=stage(minimum=0.99))
    assert record.selected_model_id is None
    assert record.is_limited is True
    assert record.limitation is not None
    payload = record.limitation.to_mapping()
    assert payload["hosted_fallback_offered"] is False
    assert "hosted model is not an available remedy" in payload["remedy"]


def test_a_limitation_names_the_local_shortfall():
    record = decision(stage_req=stage(minimum=0.99))
    limitation = cast(ScopedLimitation, record.limitation)
    assert "no local candidate passes every gate" in limitation.shortfall


def test_a_decision_that_selects_nothing_without_a_limitation_is_refused():
    with pytest.raises(RoutingDecisionError, match="scoped limitation"):
        RoutingDecisionRecord(
            decision_id="d",
            stage=stage(),
            selected_model_id=None,
            verdicts=(
                CandidateVerdict(
                    model_id="a",
                    gates=tuple(GateResult(g, "failed", "no") for g in GATE_ORDER),
                    selected=False,
                    summary="blocked",
                ),
            ),
            limitation=None,
            decided_at=NOW,
        )


def test_no_module_type_offers_a_hosted_provider_path():
    """No public verb invites a hosted model, so none can be reached."""
    forbidden = {"hosted", "cloud", "remote", "fallback_to_hosted", "escalate"}
    public = {name for name in dir(R) if not name.startswith("_")}
    assert not (forbidden & public)


# ---------------------------------------------------------------------------
# AC2 — no simultaneous residency, no dormant native activation
# ---------------------------------------------------------------------------


def test_a_switch_must_name_what_it_unloads():
    with pytest.raises(
        R.SimultaneousResidencyRefused, match="must name the model it unloads"
    ):
        ScheduleStep(index=0, stage="draft", action="switch", model_id="b")


def test_a_step_cannot_load_and_unload_the_same_model():
    with pytest.raises(R.SimultaneousResidencyRefused):
        ScheduleStep(
            index=0, stage="draft", action="load", model_id="a", unloads=("a",)
        )


def test_loading_while_another_model_is_resident_is_refused():
    steps = [
        ScheduleStep(index=0, stage="draft", action="load", model_id="a"),
        ScheduleStep(index=1, stage="revise", action="load", model_id="b"),
    ]
    with pytest.raises(R.SimultaneousResidencyRefused, match="still resident"):
        validate_sequential_plan(steps)


def test_a_sequential_plan_unloads_before_each_load():
    records = [
        decision(candidates=[candidate("a")], stage_req=stage(stage_kind="draft")),
        decision(candidates=[candidate("b")], stage_req=stage(stage_kind="revise")),
    ]
    steps = plan_sequential_schedule(decisions=records)
    assert [s.action for s in steps] == ["load", "switch"]
    assert steps[1].unloads == ("a",)
    assert steps[1].model_id == "b"


def test_a_repeated_model_is_kept_resident_rather_than_reloaded():
    records = [
        decision(candidates=[candidate("a")], stage_req=stage(stage_kind="draft")),
        decision(candidates=[candidate("a")], stage_req=stage(stage_kind="review")),
    ]
    steps = plan_sequential_schedule(decisions=records)
    assert [s.action for s in steps] == ["load", "keep_resident"]


def test_a_plan_that_already_holds_a_model_switches_rather_than_stacking():
    records = [
        decision(candidates=[candidate("b")], stage_req=stage(stage_kind="draft"))
    ]
    steps = plan_sequential_schedule(decisions=records, initially_resident="a")
    assert steps[0].action == "switch"
    assert steps[0].unloads == ("a",)


def test_scheduling_a_limited_stage_is_refused_not_improvised():
    limited = decision(stage_req=stage(minimum=0.99))
    with pytest.raises(RoutingDecisionError, match="cannot schedule"):
        plan_sequential_schedule(decisions=[limited])


def test_no_plan_step_activates_native_lifecycle():
    records = [decision(candidates=[candidate("a")])]
    steps = plan_sequential_schedule(decisions=records)
    assert all(
        step.to_mapping()["activates_native_lifecycle"] is False for step in steps
    )
    assert all(step.to_mapping()["simultaneous_residency"] is False for step in steps)


# ---------------------------------------------------------------------------
# AC4 — accounting is not physical completion; uncertainty stays charged
# ---------------------------------------------------------------------------


def test_unreported_runtime_leaves_the_charge_standing():
    accounting = MemoryAccounting(capacity_bytes=10_000, charged_bytes=4_000)
    assert accounting.state == "unknown"
    assert accounting.safe_free_bytes == 6_000
    gap = accounting_gap(accounting)
    assert gap.is_settled is False
    assert "no residency observation exists" in gap.explanation


def test_an_unload_reported_as_finished_is_an_observation_not_proof():
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=4_000,
        observed_bytes=0,
        observation=ResidencyObservation(
            model_id="a", state="unknown", observed_at=NOW
        ),
    )
    assert accounting.state == "charged_only"
    assert accounting.safe_free_bytes == 6_000
    assert "uncertainty remains charged" in accounting_gap(accounting).explanation


def test_a_resident_model_keeps_its_bytes_charged():
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=0,
        observed_bytes=4_000,
        observation=ResidencyObservation(
            model_id="a", state="resident", observed_at=NOW, reported_bytes=4_000
        ),
    )
    assert accounting.safe_free_bytes == 6_000
    assert accounting.uncertainty_bytes == 4_000
    assert (
        "still reports model bytes resident" in accounting_gap(accounting).explanation
    )


def test_only_a_released_observation_settles_the_charge():
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=4_000,
        observed_bytes=0,
        observation=ResidencyObservation(
            model_id="a", state="released", observed_at=NOW
        ),
    )
    assert accounting.state == "settled"
    # A released model is the one case where the bytes really are free again.
    assert accounting.safe_free_bytes == 10_000
    assert accounting_gap(accounting).is_settled is True


def test_the_safe_assumption_is_the_larger_of_charged_and_observed():
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=1_000,
        observed_bytes=4_000,
        observation=ResidencyObservation(
            model_id="a", state="resident", observed_at=NOW, reported_bytes=4_000
        ),
    )
    assert accounting.effective_charged_bytes == 4_000
    assert accounting.safe_free_bytes == 6_000


def test_a_charge_above_observed_is_never_lowered():
    """Lowering a charge because the runtime reported less would free bytes we did not get back."""
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=5_000,
        observed_bytes=1_000,
        observation=ResidencyObservation(
            model_id="a", state="resident", observed_at=NOW, reported_bytes=1_000
        ),
    )
    assert accounting.effective_charged_bytes == 5_000


def test_a_gap_reports_the_unexplained_difference():
    accounting = MemoryAccounting(
        capacity_bytes=10_000,
        charged_bytes=5_000,
        observed_bytes=1_000,
        observation=ResidencyObservation(
            model_id="a", state="resident", observed_at=NOW
        ),
    )
    gap = accounting_gap(accounting)
    assert isinstance(gap, AccountingGap)
    # The charge stays at 5000 while the runtime evidences only 1000, so the
    # unexplained 4000 is reported rather than assumed free.
    assert accounting.effective_charged_bytes == 5_000
    assert gap.to_mapping()["difference_bytes"] == 4_000
    assert gap.to_mapping()["settled"] is False


def test_an_unknown_observation_is_not_evidence_of_idleness():
    observation = ResidencyObservation(model_id="a", state="unknown", observed_at=NOW)
    assert observation.is_evidence_of_idleness is False
    assert (
        ResidencyObservation(
            model_id="a", state="released", observed_at=NOW
        ).is_evidence_of_idleness
        is True
    )


# ---------------------------------------------------------------------------
# AC5 — explanations and manual override boundaries
# ---------------------------------------------------------------------------


def test_every_rejected_candidate_carries_a_blocking_gate():
    record = decision(
        candidates=[
            candidate("a"),
            candidate("b", grants=()),
            candidate("c", resident_bytes=99_999),
        ],
    )
    for model_id, blocked in record.rejections.items():
        assert blocked, f"{model_id} was rejected without a stated gate"


def test_the_rejections_are_named_in_the_decision_record():
    record = decision(candidates=[candidate("a"), candidate("b", grants=())])
    assert record.selected_model_id == "a"
    payload = record.to_mapping()
    assert payload["rejections"]["b"] == ["grants"]
    assert payload["selected_model_id"] == "a"


def test_the_highest_measured_quality_wins():
    weak = candidate("weak", profile=measured_profile("weak", values=(0.6, 0.6, 0.6)))
    strong = candidate(
        "strong", profile=measured_profile("strong", values=(0.9, 0.9, 0.9))
    )
    assert decision(candidates=[weak, strong]).selected_model_id == "strong"


def test_preference_only_breaks_ties_between_candidates_that_passed():
    weak = candidate("weak", profile=measured_profile("weak", values=(0.6, 0.6, 0.6)))
    strong = candidate(
        "strong", profile=measured_profile("strong", values=(0.9, 0.9, 0.9))
    )
    assert (
        decision(candidates=[weak, strong], prefer="weak").selected_model_id == "strong"
    )


def test_preference_selects_among_equal_candidates():
    a = candidate("a", profile=measured_profile("a", values=(0.7, 0.7, 0.7)))
    b = candidate("b", profile=measured_profile("b", values=(0.7, 0.7, 0.7)))
    assert decision(candidates=[a, b], prefer="b").selected_model_id == "b"


def test_preferring_an_unknown_model_is_refused():
    with pytest.raises(RoutingDecisionError, match="not among the candidates"):
        decision(prefer="nope")


def test_an_override_cannot_select_a_blocked_candidate():
    record = decision(candidates=[candidate("a"), candidate("b", grants=())])
    override = ManualOverrideRequest(
        model_id="b", requested_by="owner", rationale="prefer b"
    )
    with pytest.raises(
        RoutingDecisionError, match="may not bypass lifecycle qualification"
    ):
        apply_manual_override(record, override)


def test_an_override_may_choose_among_candidates_that_passed():
    a = candidate("a", profile=measured_profile("a", values=(0.9, 0.9, 0.9)))
    b = candidate("b", profile=measured_profile("b", values=(0.6, 0.6, 0.6)))
    record = decision(candidates=[a, b])
    assert record.selected_model_id == "a"
    overridden = apply_manual_override(
        record,
        ManualOverrideRequest(
            model_id="b", requested_by="owner", rationale="b has the better voice"
        ),
    )
    assert overridden.selected_model_id == "b"
    assert overridden.manual_override_applied is True


def test_an_override_cannot_accept_a_gate_failure():
    with pytest.raises(RoutingDecisionError, match="cannot accept a failed gate"):
        ManualOverrideRequest(
            model_id="a",
            requested_by="owner",
            rationale="just do it",
            accepts_gate_failure=True,
        )


def test_an_override_for_a_non_candidate_is_refused():
    record = decision()
    with pytest.raises(RoutingDecisionError, match="was not a candidate"):
        apply_manual_override(
            record,
            ManualOverrideRequest(model_id="zzz", requested_by="owner", rationale="x"),
        )


def test_an_override_records_who_and_why():
    a = candidate("a", profile=measured_profile("a", values=(0.9, 0.9, 0.9)))
    b = candidate("b", profile=measured_profile("b", values=(0.6, 0.6, 0.6)))
    record = apply_manual_override(
        decision(candidates=[a, b]),
        ManualOverrideRequest(
            model_id="b", requested_by="owner", rationale="voice matters"
        ),
    )
    selected = next(v for v in record.verdicts if v.selected)
    assert "owner" in selected.summary and "voice matters" in selected.summary
    assert record.to_mapping()["manual_override_applied"] is True


# ---------------------------------------------------------------------------
# AC6 — comparison against the baseline, metric by metric
# ---------------------------------------------------------------------------


def test_the_comparison_reports_each_metric_separately():
    comparison = BaselineComparison(
        baseline_quality=0.7,
        candidate_quality=0.8,
        baseline_latency_ms=900,
        candidate_latency_ms=800,
        baseline_switches=2,
        candidate_switches=1,
        baseline_cost=1.0,
        candidate_cost=0.5,
    )
    payload = comparison.to_mapping()
    assert payload["deltas"]["quality"] == pytest.approx(0.1)
    assert payload["deltas"]["latency_ms"] == pytest.approx(-100)
    assert payload["collapsed_into_single_score"] is False
    assert payload["pareto_improvement"] is True


def test_a_faster_plan_that_adds_switches_is_not_an_improvement():
    comparison = BaselineComparison(
        baseline_quality=0.7,
        candidate_quality=0.8,
        baseline_latency_ms=900,
        candidate_latency_ms=400,
        baseline_switches=1,
        candidate_switches=4,
        baseline_cost=1.0,
        candidate_cost=1.0,
    )
    assert comparison.is_pareto_improvement is False
    assert comparison.deltas["switches"] == 3


def test_losing_quality_is_never_a_pareto_improvement():
    comparison = BaselineComparison(
        baseline_quality=0.9,
        candidate_quality=0.8,
        baseline_latency_ms=900,
        candidate_latency_ms=100,
        baseline_switches=0,
        candidate_switches=0,
        baseline_cost=0.0,
        candidate_cost=0.0,
    )
    assert comparison.is_pareto_improvement is False


def test_quality_outside_the_unit_interval_is_refused():
    with pytest.raises(RoutingDecisionError):
        BaselineComparison(
            baseline_quality=1.2,
            candidate_quality=0.5,
            baseline_latency_ms=1,
            candidate_latency_ms=1,
            baseline_switches=0,
            candidate_switches=0,
            baseline_cost=0.0,
            candidate_cost=0.0,
        )


# ---------------------------------------------------------------------------
# rendering and integrity
# ---------------------------------------------------------------------------


def test_rendered_decisions_are_deterministic_json():
    record = decision()
    first = render_decisions([record])
    second = render_decisions([record])
    assert first == second
    payload = json.loads(first)
    assert list(payload) == sorted(payload)
    assert first.endswith("\n")


def test_rendered_decisions_state_the_boundaries():
    payload = json.loads(render_decisions([decision()]))
    rules = payload["routing_rules"]
    assert rules["hosted_fallback_available"] is False
    assert rules["simultaneous_residency_permitted"] is False
    assert rules["accounting_implies_completion"] is False
    assert rules["manual_override_can_bypass_a_gate"] is False
    assert rules["unknown_gate_blocks"] is True
    assert len(payload["does_not_prove"]) == 3


def test_a_decision_without_candidates_is_refused():
    with pytest.raises(RoutingDecisionError, match="at least one local candidate"):
        decision(candidates=[])


def test_two_selected_verdicts_are_refused():
    gates = tuple(GateResult(g, "passed") for g in GATE_ORDER)
    with pytest.raises(RoutingDecisionError, match="at most one"):
        RoutingDecisionRecord(
            decision_id="d",
            stage=stage(),
            selected_model_id="a",
            verdicts=(
                CandidateVerdict("a", gates, True, "ok"),
                CandidateVerdict("b", gates, True, "ok"),
            ),
            limitation=None,
            decided_at=NOW,
        )


def test_a_selected_model_without_a_verdict_is_refused():
    gates = tuple(GateResult(g, "failed", "no") for g in GATE_ORDER)
    with pytest.raises(RoutingDecisionError, match="needs a selected verdict"):
        RoutingDecisionRecord(
            decision_id="d",
            stage=stage(),
            selected_model_id="a",
            verdicts=(CandidateVerdict("b", gates, False, "blocked"),),
            limitation=None,
            decided_at=NOW,
        )


def test_unknown_stage_is_refused():
    with pytest.raises(RoutingDecisionError, match="unknown stage"):
        StageRequirement(
            stage=cast(Any, "publish"), task_kind="coding", minimum_quality=0.5
        )


def test_decision_digest_is_stable():
    record = decision()
    assert record.digest == decision().digest


def test_a_gate_failure_must_state_a_reason():
    with pytest.raises(RoutingDecisionError, match="must state a reason"):
        GateResult("grants", "failed", "")


def test_limitation_without_fallback_is_the_only_local_answer():
    record = decision(stage_req=stage(minimum=0.99))
    assert record.to_mapping()["hosted_fallback_available"] is False
    assert isinstance(record.limitation, ScopedLimitation)
