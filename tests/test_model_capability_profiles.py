"""AP-013 offline tests: measured model capability and compatibility profiles.

Nothing here starts a runtime, loads a model or reaches a network. Probes are
plain values, which is the point: the module's job is to decide what a
measurement means and to refuse to decide what it does not cover.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

import model_capability_profiles as M
from model_capability_profiles import (
    ArchitectureFootprint,
    CapabilityProfile,
    CapabilityProfileError,
    Claim,
    CompatibilityClaim,
    ConfigurationIdentity,
    ExpertAsAgentClaim,
    InstalledFootprint,
    ManualOverride,
    Measurement,
    ModelCompanion,
    Readiness,
    ResidencyState,
    RoleHypothesis,
    TaskProbe,
    assess_readiness,
    default_role_hypotheses,
    evaluate_hypothesis,
    render_profiles,
    require_capability,
    summarise_footprint,
    summarise_task_probes,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
NOW = "2026-11-21T12:00:00Z"
LATER = "2027-11-21T12:00:00Z"


def identity(**overrides: Any) -> ConfigurationIdentity:
    base: dict[str, Any] = dict(
        model_id="qwen3-coder-30b",
        checkpoint_digest=DIGEST_A,
        checkpoint_revision="main@7f3a",
        runtime="llama.cpp",
        runtime_version="b4010",
        quantization="Q4_K_M",
        template="chatml",
        context_length=32768,
        hardware_fingerprint="hw-fingerprint-1",
    )
    base.update(overrides)
    return ConfigurationIdentity(**base)


def probe(kind="coding", *, score=0.8, config=None, passed=True) -> TaskProbe:
    return TaskProbe(
        task_kind=kind,
        configuration_digest=config or identity().digest,
        score=score,
        passed=passed,
        measured_at=NOW,
    )


def measured(kind="coding", *, ident=None, values=(0.8, 0.8, 0.8)) -> Measurement:
    ident = ident or identity()
    return summarise_task_probes(
        ident,
        [probe(kind, score=v, config=ident.digest) for v in values],
        kind=kind,
        measured_at=NOW,
    )


def profile(**overrides) -> CapabilityProfile:
    base: dict = {"identity": identity()}
    base.update(overrides)
    return CapabilityProfile(**base)


# ---------------------------------------------------------------------------
# AC1 — the profile covers every dimension and is pinned to its configuration
# ---------------------------------------------------------------------------


def test_identity_records_runtime_checkpoint_quantization_template_and_context():
    payload = identity().to_mapping()
    for key in (
        "runtime",
        "runtime_version",
        "checkpoint_digest",
        "checkpoint_revision",
        "quantization",
        "template",
        "context_length",
        "hardware_fingerprint",
    ):
        assert key in payload, key


def test_identity_requires_a_checkpoint_digest_not_a_name():
    with pytest.raises(CapabilityProfileError, match="sha256"):
        identity(checkpoint_digest="qwen3-coder-30b")


def test_quantization_is_optional_and_reported_as_null():
    assert identity(quantization=None).to_mapping()["quantization"] is None


def test_identity_digest_changes_with_quantization():
    assert identity().digest != identity(quantization="Q8_0").digest


def test_identity_digest_changes_with_template():
    """The chat template changes tool and schema behaviour; it is identity."""
    assert identity().digest != identity(template="llama3").digest


def test_identity_digest_changes_with_runtime_version():
    assert identity().digest != identity(runtime_version="b4011").digest


def test_identity_digest_changes_with_hardware():
    assert identity().digest != identity(hardware_fingerprint="hw-2").digest


def test_identity_digest_is_stable_for_the_same_configuration():
    assert identity().digest == identity().digest


def test_context_length_must_be_positive():
    with pytest.raises(CapabilityProfileError):
        identity(context_length=0)


def test_profile_carries_measurement_provenance():
    payload = profile(measurements={"coding": measured()}).to_mapping()
    assert payload["measurements"]["coding"]["provenance"] == "local_probe"
    assert payload["measurements"]["coding"]["qualifies"] is True
    assert payload["identity_digest"] == identity().digest


def test_a_measurement_from_another_configuration_is_refused():
    other = identity(quantization="Q8_0")
    with pytest.raises(CapabilityProfileError, match="different configuration"):
        profile(measurements={"coding": measured(ident=other)})


# ---------------------------------------------------------------------------
# AC2 — companions, installed disk, readiness and residency are separate
# ---------------------------------------------------------------------------


def test_companion_is_separate_from_the_checkpoint():
    projector = ModelCompanion(
        companion_id="mmproj-v1",
        kind="vision_projector",
        digest=DIGEST_B,
        size_bytes=1024,
        present=True,
    )
    payload = projector.to_mapping()
    assert payload["companion_id"] == "mmproj-v1"
    assert payload["kind"] == "vision_projector"
    assert payload["size_bytes"] == 1024


def test_a_missing_companion_makes_a_configuration_unready():
    projector = ModelCompanion(
        companion_id="mmproj",
        kind="vision_projector",
        digest=DIGEST_B,
        size_bytes=10,
        present=False,
    )
    readiness = assess_readiness(
        weights_present=True, companions=[projector], identity=identity()
    )
    assert readiness.is_ready is False
    assert any("not installed" in reason for reason in readiness.reasons)


def test_a_companion_bound_to_another_checkpoint_is_incompatible():
    adapter = ModelCompanion(
        companion_id="lora-x",
        kind="adapter",
        digest=DIGEST_B,
        size_bytes=10,
        bound_to_checkpoint_digest=DIGEST_B,
        present=True,
    )
    ok, reason = adapter.compatible_with(identity())
    assert ok is False
    assert "different checkpoint" in reason


def test_installed_footprint_counts_only_present_companions():
    present = ModelCompanion(
        companion_id="a",
        kind="tokenizer",
        digest=DIGEST_B,
        size_bytes=100,
        present=True,
    )
    absent = ModelCompanion(
        companion_id="b", kind="adapter", digest=DIGEST_B, size_bytes=999, present=False
    )
    footprint = summarise_footprint(
        weights_bytes=1000, companions=[present, absent], kv_cache_bytes_at_context=50
    )
    assert footprint.companions_bytes == 100
    assert footprint.total_bytes == 1150


def test_footprint_negative_bytes_are_refused():
    with pytest.raises(CapabilityProfileError):
        InstalledFootprint(weights_bytes=-1)


def test_readiness_unknown_is_not_ready():
    """Installed is not the same as verified; unknown never reads as ready."""
    readiness = assess_readiness(
        weights_present=True, identity=identity(), runtime_supports_architecture=None
    )
    assert readiness.state == "unknown"
    assert readiness.is_ready is False


def test_readiness_ready_requires_verified_runtime_support():
    readiness = assess_readiness(
        weights_present=True, identity=identity(), runtime_supports_architecture=True
    )
    assert readiness.state == "ready"
    assert readiness.is_ready is True


def test_unsupported_runtime_architecture_is_incompatible():
    readiness = assess_readiness(
        weights_present=True, identity=identity(), runtime_supports_architecture=False
    )
    assert readiness.state == "incompatible"


def test_residency_is_an_observation_with_a_timestamp():
    residency = ResidencyState(
        state="resident", observed_at=NOW, resident_bytes=18_000_000_000
    )
    payload = residency.to_mapping()
    assert payload["state"] == "resident"
    assert payload["observed_at"] == NOW


def test_a_resident_model_must_report_its_bytes():
    with pytest.raises(CapabilityProfileError):
        ResidencyState(state="resident", observed_at=NOW)


def test_a_non_resident_model_cannot_claim_resident_bytes():
    with pytest.raises(CapabilityProfileError):
        ResidencyState(state="cold", observed_at=NOW, resident_bytes=10)


def test_installed_and_loaded_are_independent():
    """A cold model is installed but not resident; neither implies capability."""
    prof = profile(
        footprint=InstalledFootprint(weights_bytes=1000),
        readiness=Readiness(state="ready"),
        residency=ResidencyState(state="cold", observed_at=NOW),
    )
    payload = prof.to_mapping()
    assert payload["readiness"]["state"] == "ready"
    assert payload["residency"]["state"] == "cold"
    assert payload["readiness"]["state"] == "ready"
    assert prof.numeric("coding") is None


# ---------------------------------------------------------------------------
# AC3 — task kinds are measured separately
# ---------------------------------------------------------------------------


def test_the_five_task_kinds_are_named():
    assert M.TASK_KINDS == (
        "writing",
        "coding",
        "architecture",
        "source_handling",
        "schema_tool",
    )


@pytest.mark.parametrize("kind", M.TASK_KINDS)
def test_each_task_kind_measures_to_its_own_slot(kind):
    prof = profile(measurements={kind: measured(kind, values=(0.9, 0.9, 0.9))})
    assert prof.numeric(kind) == 0.9
    other = [k for k in M.TASK_KINDS if k != kind]
    assert all(prof.numeric(k) is None for k in other)


def test_task_kinds_do_not_pool_into_one_score():
    writing = measured("writing", values=(0.95, 0.95, 0.95))
    schema = measured("schema_tool", values=(0.1, 0.1, 0.1))
    prof = profile(measurements={"writing": writing, "schema_tool": schema})
    assert prof.numeric("writing") == 0.95
    assert prof.numeric("schema_tool") == 0.1
    assert prof.numeric("coding") is None


def test_probes_of_different_kinds_are_refused_rather_than_pooled():
    with pytest.raises(CapabilityProfileError, match="must not pool"):
        summarise_task_probes(
            identity(),
            [probe("coding"), probe("writing")],
            kind="coding",
            measured_at=NOW,
        )


def test_a_probe_from_another_configuration_is_refused():
    with pytest.raises(CapabilityProfileError, match="different configuration"):
        summarise_task_probes(
            identity(),
            [probe(config=identity(quantization="Q8_0").digest)],
            kind="coding",
            measured_at=NOW,
        )


def test_failed_probes_still_count_as_samples():
    """A model passing two of three attempts did not score 1.0."""
    probes = [
        probe(score=0.9, passed=True),
        probe(score=0.0, passed=False),
        probe(score=0.9, passed=True),
    ]
    m = summarise_task_probes(identity(), probes, kind="coding", measured_at=NOW)
    assert m.sample_count == 3
    assert m.value == pytest.approx(0.6, abs=1e-6)
    assert m.spread == pytest.approx(0.9, abs=1e-6)


def test_a_single_probe_is_not_confident():
    m = summarise_task_probes(identity(), [probe()], kind="coding", measured_at=NOW)
    assert m.qualifies is True
    assert m.confident is False


def test_unstable_probes_are_not_confident():
    unstable = measured("coding", values=(0.1, 0.9, 0.5))
    assert unstable.confident is False


def test_stable_repeated_probes_are_confident():
    assert measured("coding", values=(0.8, 0.82, 0.79)).confident is True


def test_measured_kinds_and_unknown_kinds_are_reported_separately():
    prof = profile(measurements={"coding": measured("coding")})
    assert prof.measured_kinds == ("coding",)
    assert set(prof.unknown_kinds) == set(M.TASK_KINDS) - {"coding"}


# ---------------------------------------------------------------------------
# AC4 — model cards and imported benchmarks are not local qualification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provenance", ["model_card", "imported_benchmark", "vendor_claim"]
)
def test_a_non_local_measurement_never_qualifies(provenance):
    item = Measurement(
        value=0.99, provenance=provenance, source="vendor page", measured_at=NOW
    )
    assert item.qualifies is False
    assert item.to_mapping()["qualifies"] is False
    assert "not a local measurement" in item.to_mapping()["does_not_qualify_reason"]


def test_a_profile_holding_only_a_model_card_claim_reports_unknown():
    big_claim = Measurement(
        value=0.99,
        provenance="model_card",
        source="model card",
        measured_at=NOW,
        sample_count=5,
    )
    prof = profile(measurements={"writing": big_claim})
    state, _item, reason = prof.capability("writing")
    assert state == "unknown"
    assert "model_card" in reason
    assert prof.numeric("writing") is None


def test_a_model_card_claim_cannot_raise_a_refusal_to_an_allow():
    item = Measurement(
        value=0.99,
        provenance="model_card",
        source="card",
        measured_at=NOW,
        sample_count=9,
    )
    prof = profile(measurements={"coding": item})
    boundary = require_capability(prof, "coding", now=NOW)
    assert boundary.decision == "refuse"
    assert boundary.basis == "unknown"


def test_claims_are_recorded_but_carry_no_value():
    claim = Claim(
        statement="Writing 94/100 on the vendor writing benchmark",
        origin="model_card",
        source="card",
    )
    payload = claim.to_mapping()
    assert payload["qualifies"] is False
    assert "value" not in payload


def test_a_claim_cannot_impersonate_a_local_probe():
    with pytest.raises(CapabilityProfileError, match="Claim records"):
        Claim(statement="fine", origin="local_probe", source="me")


def test_a_claim_mentioning_a_task_still_yields_unknown_with_a_reason():
    prof = profile(
        claims=(
            Claim(
                statement="strong coding ability", origin="model_card", source="card"
            ),
        )
    )
    state, item, reason = prof.capability("coding")
    assert state == "unknown"
    assert item is None
    assert "claims are not qualification" in reason


def test_a_local_probe_must_name_the_configuration_it_measured():
    with pytest.raises(CapabilityProfileError, match="unattributed probe"):
        Measurement(
            value=0.5, provenance="local_probe", source="probe", measured_at=NOW
        )


def test_a_local_measurement_for_this_configuration_does_qualify():
    m = measured("coding")
    assert m.qualifies is True
    assert m.configuration_digest == identity().digest


# ---------------------------------------------------------------------------
# AC5 — unknown fails explicitly; overrides stay bounded
# ---------------------------------------------------------------------------


def test_an_unmeasured_capability_refuses_by_default():
    boundary = require_capability(profile(), "writing", now=NOW)
    assert boundary.decision == "refuse"
    assert boundary.basis == "unknown"
    assert boundary.to_mapping()["is_qualification"] is False


def test_a_measured_confident_capability_allows():
    prof = profile(
        measurements={"writing": measured("writing", values=(0.8, 0.8, 0.8))}
    )
    boundary = require_capability(prof, "writing", now=NOW)
    assert boundary.decision == "allow"
    assert boundary.basis == "measured"


def test_an_indicative_but_unstable_measurement_does_not_allow():
    prof = profile(measurements={"writing": measured("writing", values=(0.2, 0.9))})
    boundary = require_capability(prof, "writing", now=NOW)
    assert boundary.decision == "refuse"
    assert boundary.basis == "indicative"


def test_an_operator_override_degrades_and_never_allows():
    override = ManualOverride(
        task_kind="architecture",
        configuration_digest=identity().digest,
        rationale="no probe exists yet; the operator accepts the risk for this task",
        expires_at=LATER,
        overridden_by="owner",
    )
    prof = profile(overrides=(override,))
    boundary = require_capability(prof, "architecture", now=NOW)
    assert boundary.decision == "degrade"
    assert boundary.basis == "operator_override"
    assert boundary.to_mapping()["is_qualification"] is False


def test_an_expired_override_is_ignored():
    override = ManualOverride(
        task_kind="architecture",
        configuration_digest=identity().digest,
        rationale="stale",
        expires_at="2026-01-01T00:00:00Z",
        overridden_by="owner",
    )
    assert (
        require_capability(
            profile(overrides=(override,)), "architecture", now=NOW
        ).decision
        == "refuse"
    )


def test_an_override_is_bounded_to_one_configuration():
    override = ManualOverride(
        task_kind="coding",
        configuration_digest=identity(quantization="Q8_0").digest,
        rationale="other build",
        expires_at=LATER,
        overridden_by="owner",
    )
    assert (
        require_capability(profile(overrides=(override,)), "coding", now=NOW).decision
        == "refuse"
    )


def test_an_override_cannot_be_used_by_a_caller_that_refuses_it():
    override = ManualOverride(
        task_kind="coding",
        configuration_digest=identity().digest,
        rationale="accepted risk",
        expires_at=LATER,
        overridden_by="owner",
    )
    boundary = require_capability(
        profile(overrides=(override,)), "coding", now=NOW, allow_override=False
    )
    assert boundary.decision == "refuse"


def test_an_override_never_contradicts_a_measurement():
    prof = profile(
        measurements={"coding": measured("coding", values=(0.8, 0.8, 0.8))},
        overrides=(
            ManualOverride(
                task_kind="coding",
                configuration_digest=identity().digest,
                rationale="override",
                expires_at=LATER,
                overridden_by="owner",
            ),
        ),
    )
    treatment, reason = prof.effective("coding", now=NOW)
    assert treatment == "qualified"
    assert "override" not in reason


def test_an_override_requires_a_rationale_owner_and_expiry():
    with pytest.raises(CapabilityProfileError):
        ManualOverride(
            task_kind="coding",
            configuration_digest=identity().digest,
            rationale=" ",
            expires_at=LATER,
            overridden_by="owner",
        )


def test_unknown_capability_is_reported_as_unknown_not_as_zero():
    """Unknown is not "bad at it" any more than it is "good at it"."""
    prof = profile()
    assert prof.numeric("source_handling") is None
    assert prof.capability("source_handling")[0] == "unknown"


# ---------------------------------------------------------------------------
# AC6 — family role suggestions are hypotheses until tested
# ---------------------------------------------------------------------------


def test_default_role_hypotheses_are_untested():
    hypotheses = default_role_hypotheses()
    assert {h.model_family for h in hypotheses} == {"mistral", "qwen", "gemma", "twil"}
    assert all(h.status == "untested" for h in hypotheses)


def test_a_hypothesis_is_never_a_qualification():
    assert roles_never_qualify()


def roles_never_qualify() -> bool:
    return all(h.is_qualification is False for h in default_role_hypotheses())


def test_an_untested_hypothesis_stays_untested_without_a_confident_measurement():
    hypothesis = RoleHypothesis(
        model_family="qwen", role="coding assistant", task_kind="coding"
    )
    evaluated = evaluate_hypothesis(hypothesis, profile())
    assert evaluated.status == "untested"
    assert evaluated.evidence_value is None


def test_a_confident_measurement_can_support_a_hypothesis_for_this_configuration():
    hypothesis = RoleHypothesis(
        model_family="qwen", role="coding assistant", task_kind="coding"
    )
    prof = profile(measurements={"coding": measured("coding", values=(0.8, 0.8, 0.8))})
    evaluated = evaluate_hypothesis(hypothesis, prof)
    assert evaluated.status == "supported"
    assert evaluated.evidence_configuration_digest == identity().digest


def test_a_poor_measurement_contradicts_the_hypothesis():
    hypothesis = RoleHypothesis(
        model_family="gemma", role="writer", task_kind="writing"
    )
    prof = profile(
        measurements={"writing": measured("writing", values=(0.1, 0.1, 0.1))}
    )
    assert evaluate_hypothesis(hypothesis, prof).status == "contradicted"


def test_a_model_card_cannot_support_a_hypothesis():
    hypothesis = RoleHypothesis(model_family="qwen", role="coder", task_kind="coding")
    card = Measurement(
        value=0.99,
        provenance="model_card",
        source="card",
        measured_at=NOW,
        sample_count=9,
    )
    evaluated = evaluate_hypothesis(hypothesis, profile(measurements={"coding": card}))
    assert evaluated.status == "untested"


def test_a_hypothesis_cannot_be_evaluated_twice():
    tested = RoleHypothesis(
        model_family="qwen",
        role="coder",
        task_kind="coding",
        status="supported",
        evidence_configuration_digest=identity().digest,
    )
    with pytest.raises(CapabilityProfileError, match="only an untested"):
        evaluate_hypothesis(tested, profile())


def test_a_supported_hypothesis_is_scoped_to_its_configuration():
    hypothesis = RoleHypothesis(model_family="qwen", role="coder", task_kind="coding")
    prof = profile(measurements={"coding": measured("coding", values=(0.9, 0.9, 0.9))})
    evaluated = evaluate_hypothesis(hypothesis, prof)
    assert evaluated.evidence_configuration_digest == identity().digest
    assert (
        evaluated.evidence_configuration_digest != identity(quantization="Q8_0").digest
    )


# ---------------------------------------------------------------------------
# AC7 — dense vs MoE on resident memory, active compute and quality
# ---------------------------------------------------------------------------


def dense() -> ArchitectureFootprint:
    return ArchitectureFootprint(
        kind="dense", total_parameters=32_000_000_000, active_parameters=32_000_000_000
    )


def moe() -> ArchitectureFootprint:
    return ArchitectureFootprint(
        kind="mixture_of_experts",
        total_parameters=30_000_000_000,
        active_parameters=3_000_000_000,
        expert_count=8,
    )


def test_dense_activates_all_parameters():
    assert dense().activation_ratio == 1.0


def test_a_dense_model_claiming_partial_activation_is_refused():
    with pytest.raises(CapabilityProfileError, match="dense model activates"):
        ArchitectureFootprint(kind="dense", total_parameters=100, active_parameters=10)


def test_moe_reports_total_and_active_separately():
    payload = moe().to_mapping()
    assert payload["total_parameters"] == 30_000_000_000
    assert payload["active_parameters"] == 3_000_000_000
    assert payload["activation_ratio"] == pytest.approx(0.1)


def test_low_active_parameters_never_establish_low_memory():
    payload = moe().to_mapping()
    assert payload["active_parameters_establish_resident_memory"] is False
    assert "do not establish low RAM use" in payload["resident_memory_story"]


def test_moe_with_full_activation_is_recorded_as_dense():
    with pytest.raises(CapabilityProfileError, match="fully-active MoE"):
        ArchitectureFootprint(
            kind="mixture_of_experts",
            total_parameters=100,
            active_parameters=100,
            expert_count=4,
        )


def test_moe_requires_at_least_two_experts():
    with pytest.raises(CapabilityProfileError):
        ArchitectureFootprint(
            kind="mixture_of_experts",
            total_parameters=100,
            active_parameters=10,
            expert_count=1,
        )


def test_active_cannot_exceed_total():
    with pytest.raises(CapabilityProfileError):
        ArchitectureFootprint(kind="dense", total_parameters=10, active_parameters=20)


def test_the_comparison_uses_memory_compute_and_quality_not_parameter_count():
    comparison = M.CandidateComparison(
        left=dense(),
        right=moe(),
        left_quality={"coding": measured("coding", values=(0.8, 0.8, 0.8))},
        right_quality={"coding": measured("coding", values=(0.7, 0.7, 0.7))},
    )
    payload = comparison.to_mapping()
    assert payload["resident_memory_bytes"] == {"left": 0, "right": 0}
    assert payload["active_parameters"] == {
        "left": 32_000_000_000,
        "right": 3_000_000_000,
    }
    assert payload["task_quality"]["left"]["coding"]["value"] == pytest.approx(0.8)
    assert payload["low_active_parameters_establish_low_memory"] is False
    assert "not evidence of lower RAM use" in payload["note"]


def test_the_comparison_shows_quality_that_does_not_qualify():
    card = Measurement(
        value=0.99,
        provenance="model_card",
        source="card",
        measured_at=NOW,
        sample_count=9,
    )
    comparison = M.CandidateComparison(
        left=dense(), right=moe(), right_quality={"coding": card}
    )
    assert (
        comparison.to_mapping()["task_quality"]["right"]["coding"]["qualifies"] is False
    )


# ---------------------------------------------------------------------------
# AC8 — MoE experts are not separately loadable agents
# ---------------------------------------------------------------------------


def test_an_expert_as_agent_claim_is_unsupported_by_default():
    claim = ExpertAsAgentClaim(proposal="run expert 4 as a dedicated copywriter")
    assert claim.supported is False
    assert "AC8" in claim.to_mapping()["refusal_reason"]


def test_an_expert_as_agent_claim_requires_verified_runtime_capability():
    claim = ExpertAsAgentClaim(
        proposal="expert 4 as copywriter",
        runtime_verified=True,
        evidence="runtime docs",
    )
    assert claim.supported is True


def test_verification_without_evidence_is_not_support():
    claim = ExpertAsAgentClaim(proposal="expert 4 as copywriter", runtime_verified=True)
    assert claim.supported is False


def test_experts_are_never_inferred_to_be_separately_loadable():
    assert moe().experts_are_separately_loadable is False
    assert moe().to_mapping()["experts_are_separately_loadable"] is False


# ---------------------------------------------------------------------------
# compatibility and rendering
# ---------------------------------------------------------------------------


def test_runtime_feature_support_is_three_valued():
    prof = profile(
        compatibility=(
            CompatibilityClaim(
                feature="vision", state="supported", evidence="runtime release notes"
            ),
            CompatibilityClaim(
                feature="tools",
                state="unsupported",
                evidence="no tool parser for this template",
            ),
        )
    )
    assert prof.supports("vision") == "supported"
    assert prof.supports("tools") == "unsupported"
    assert prof.supports("audio") == "unknown"


def test_unsupported_and_unknown_are_distinct_compatibility_answers():
    unsupported = profile(
        compatibility=(
            CompatibilityClaim(feature="schema", state="unsupported", evidence="probe"),
        )
    )
    assert unsupported.supports("schema") == "unsupported"
    assert profile().supports("schema") == "unknown"


def test_companion_problems_are_summarised_on_the_profile():
    prof = profile(
        companions=(
            ModelCompanion(
                companion_id="mmproj",
                kind="vision_projector",
                digest=DIGEST_B,
                size_bytes=1,
            ),
        )
    )
    assert len(prof.companion_problems()) == 1
    assert len(prof.to_mapping()["companion_problems"]) == 1


def test_rendered_profiles_are_deterministic_json():
    ident = identity()
    probes = [probe(score=0.8, config=ident.digest) for _ in range(3)]
    prof = profile(
        measurements={
            "coding": summarise_task_probes(
                ident, probes, kind="coding", measured_at=NOW
            )
        }
    )
    first = render_profiles([prof])
    second = render_profiles([prof])
    assert first == second
    payload = json.loads(first)
    assert list(payload) == sorted(payload)
    assert first.endswith("\n")


def test_rendered_profiles_state_what_they_do_not_prove():
    payload = json.loads(render_profiles([profile()]))
    assert len(payload["does_not_prove"]) == 3
    assert payload["qualification_rules"]["claims_are_qualification"] is False
    assert payload["qualification_rules"]["unknown_means_refused"] is True
    assert (
        payload["qualification_rules"]["measurements_migrate_between_configurations"]
        is False
    )


def test_profile_digest_is_stable_and_configuration_specific():
    a = profile(measurements={"coding": measured("coding")})
    assert a.digest == profile(measurements={"coding": measured("coding")}).digest
    other = identity(quantization="Q8_0")
    # The same measurement cannot be attached to a second configuration, so the
    # comparison uses that configuration's own probe.
    b = profile(
        identity=other, measurements={"coding": measured("coding", ident=other)}
    )
    assert a.digest != b.digest
    with pytest.raises(CapabilityProfileError, match="do not migrate"):
        profile(identity=other, measurements={"coding": measured("coding")})


def test_unknown_task_kind_is_refused_everywhere():
    bogus = cast(Any, "vibes")
    with pytest.raises(CapabilityProfileError):
        profile().capability(bogus)
    with pytest.raises(CapabilityProfileError):
        require_capability(profile(), bogus, now=NOW)


def test_no_module_verb_starts_a_runtime_or_downloads():
    """The public surface must not offer load/run/download/install/activate."""
    forbidden = {
        "load_model",
        "download",
        "install",
        "activate",
        "run",
        "start_runtime",
        "pull",
    }
    assert not (forbidden & {name for name in dir(M) if not name.startswith("_")})
