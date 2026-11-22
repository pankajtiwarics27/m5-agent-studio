"""AP-011 offline tests: gap classification, candidate proposals, licensing.

Everything here is pure — no network, no install, no execution. The tests exist
to make the refusals and the honesty properties fail loudly if someone weakens
them later.
"""

from __future__ import annotations

import json

import pytest

import autonomous_production_discovery as d
from autonomous_production_discovery import (
    Assessment,
    CandidateKind,
    CandidateRegistry,
    CapabilityCandidate,
    CapabilityGap,
    DiscoveryError,
    Egress,
    GapKind,
    InstallHooks,
    LicenseAssessment,
    Maintenance,
    Privileges,
    Recommendation,
    RecommendationKind,
    ResourceCost,
    TrustBasis,
    assess_candidate,
    assess_license,
    classify_gap,
    establish_trust,
    render_proposal,
)

NO_GAP = d.NO_GAP


def candidate(
    *, kind: str = CandidateKind.TOOL, cid: str = "cand", **overrides
) -> CapabilityCandidate:
    base = dict(
        candidate_id=cid,
        kind=kind,
        name="Example",
        source_url="https://github.com/example/example",
        source_revision="v1.2.3",
        maintainer="example",
        maintenance=Maintenance(days_since_release=30, evidence="releases page"),
        install_hooks=InstallHooks(),
        resource_cost=ResourceCost(disk_bytes=1024, basis="estimated"),
        egress=Egress(),
        privileges=Privileges(),
        license=assess_license("MIT"),
    )
    base.update(overrides)
    return CapabilityCandidate(**base)


# --------------------------------------------------------------- AC1: gap kinds

def test_no_signals_means_no_gap():
    gap = classify_gap([])
    assert gap.kind == GapKind.NONE
    assert gap.needs_new_dependency is False
    assert gap is NO_GAP


@pytest.mark.parametrize(
    "signal,kind,needs_dep",
    [
        ("missing_facts", GapKind.MISSING_FACTS, False),
        ("wrong_method", GapKind.MISSING_METHOD, False),
        ("context_exceeded", GapKind.CONTEXT, False),
        ("model_incapable", GapKind.MODEL_CAPABILITY, True),
        ("no_tool", GapKind.TOOL, True),
        ("hardware_limit", GapKind.HARDWARE, True),
    ],
)
def test_each_signal_maps_to_its_own_kind(signal, kind, needs_dep):
    gap = classify_gap([signal])
    assert gap.kind == kind
    assert gap.needs_new_dependency is needs_dep


def test_missing_facts_is_never_treated_as_a_dependency_problem():
    """The whole point of AC1: don't buy a tool to fix a missing fact."""
    gap = classify_gap(["missing_facts"])
    assert gap.needs_new_dependency is False
    assert "collect" in gap.remedy


def test_cheapest_explanation_wins_when_several_signals_are_present():
    gap = classify_gap(["hardware_limit", "missing_facts"])
    assert gap.kind == GapKind.MISSING_FACTS


def test_unknown_signal_is_refused():
    with pytest.raises(DiscoveryError, match="unrecognised"):
        classify_gap(["the vibes"])


def test_gap_requires_reason_and_remedy():
    with pytest.raises(DiscoveryError):
        CapabilityGap(kind=GapKind.TOOL, reason="", remedy="x", needs_new_dependency=True)
    with pytest.raises(DiscoveryError):
        CapabilityGap(kind=GapKind.TOOL, reason="x", remedy="", needs_new_dependency=True)


def test_no_gap_cannot_require_a_dependency():
    with pytest.raises(DiscoveryError):
        CapabilityGap(kind=GapKind.NONE, reason="x", remedy="y", needs_new_dependency=True)


def test_note_is_carried_into_the_reason():
    gap = classify_gap(["no_tool"], note="no PDF parser available")
    assert "no PDF parser available" in gap.reason


# --------------------------------------------------------------- AC2: no-gap path

def test_registry_without_gap_cannot_register_candidates():
    registry = CandidateRegistry()
    with pytest.raises(DiscoveryError, match="invent a dependency"):
        registry.register(candidate())


def test_registry_without_gap_recommends_no_new_dependency():
    registry = CandidateRegistry()
    rec = registry.recommendation()
    assert rec.kind == RecommendationKind.NO_NEW_DEPENDENCY
    assert rec.candidates == ()


def test_a_gap_that_needs_no_dependency_recommends_none_even_with_candidates():
    gap = classify_gap(["missing_facts"])
    registry = CandidateRegistry(gap=gap)
    rec = registry.recommendation()
    assert rec.kind == RecommendationKind.NO_NEW_DEPENDENCY
    assert rec.candidates == ()


def test_recommendation_cannot_name_candidates_without_proposing_them():
    with pytest.raises(DiscoveryError):
        Recommendation(kind=RecommendationKind.NO_NEW_DEPENDENCY, gap=NO_GAP, candidates=("a",))
    with pytest.raises(DiscoveryError):
        Recommendation(kind=RecommendationKind.PROPOSE_FOR_REVIEW, gap=NO_GAP)


# --------------------------------------------------------------- AC3: candidate surface

def test_candidate_records_the_full_surface():
    payload = candidate().to_dict()
    for field in (
        "source_revision",
        "maintenance",
        "license",
        "install_hooks",
        "resource_cost",
        "egress",
        "privileges",
    ):
        assert field in payload
    assert payload["source_revision"] == "v1.2.3"


@pytest.mark.parametrize("field_name", ["candidate_id", "name", "source_url", "source_revision"])
def test_candidate_requires_its_identity_fields(field_name):
    with pytest.raises(DiscoveryError):
        candidate(**{field_name: "  "})


def test_candidate_kind_is_validated():
    with pytest.raises(DiscoveryError):
        candidate(kind="magic")


def test_candidate_id_must_be_url_safe():
    with pytest.raises(DiscoveryError):
        candidate(cid="Not Valid!")


def test_unknown_maintenance_is_unknown_not_optimistic():
    c = candidate(maintenance=Maintenance())
    assert c.maintenance.is_stale is None
    assert assess_candidate(c).warnings and any("cannot be judged" in w for w in assess_candidate(c).warnings)


def test_archived_candidate_is_flagged_stale():
    c = candidate(maintenance=Maintenance(archived=True))
    assert c.maintenance.is_stale is True
    assert any("stale" in w for w in assess_candidate(c).warnings)


def test_install_hooks_are_recorded_and_warned_about():
    c = candidate(install_hooks=InstallHooks(post_install_scripts=("setup.sh",)))
    assert c.install_hooks.has_hooks is True
    assert any("install-time hooks" in w for w in assess_candidate(c).warnings)


def test_network_at_install_time_blocks_the_candidate():
    c = candidate(install_hooks=InstallHooks(network_at_install=True))
    assert assess_candidate(c).blockers


def test_resource_cost_rejects_negative_values():
    with pytest.raises(DiscoveryError):
        ResourceCost(disk_bytes=-1)


def test_resource_cost_basis_is_validated():
    with pytest.raises(DiscoveryError):
        ResourceCost(basis="guessed")


def test_egress_outside_the_allowlist_is_flagged_and_blocked_for_reuse():
    c = candidate(egress=Egress(hosts=("evil.example.com",), schemes=("https",)))
    assessment = assess_candidate(c, allowed_hosts=("github.com",))
    assert "egress" in assessment.blocked_reuse
    assert any("outside the granted allowlist" in w for w in assessment.warnings)


def test_egress_inside_the_allowlist_is_still_only_a_warning():
    c = candidate(egress=Egress(hosts=("github.com",), schemes=("https",)))
    assessment = assess_candidate(c, allowed_hosts=("github.com",))
    assert assessment.warnings
    assert any("still requires a grant" in w for w in assessment.warnings)


def test_account_and_spend_requirements_block():
    assert assess_candidate(candidate(egress=Egress(account_required=True))).blockers
    assert assess_candidate(candidate(egress=Egress(spend_required=True))).blockers


def test_secret_access_blocks():
    assert any(
        "secret" in b for b in assess_candidate(candidate(privileges=Privileges(secret_access=True))).blockers
    )


# --------------------------------------------------------------- AC4: trust

def test_popularity_never_establishes_trust():
    c = candidate(popularity_claim="12k stars")
    assert c.trust_basis == TrustBasis.UNVERIFIED
    assert assess_candidate(c).warnings[0].startswith("trust is unverified")
    # And the serialised record says so explicitly.
    assert c.to_dict()["popularity_establishes_trust"] is False


def test_stars_as_evidence_are_rejected():
    with pytest.raises(DiscoveryError, match="never establish trust"):
        establish_trust(candidate(), evidence=["1500 stars"], basis=TrustBasis.VERIFIED_UPSTREAM_RELEASE)


def test_mcp_annotation_as_evidence_is_rejected():
    with pytest.raises(DiscoveryError, match="never establish trust"):
        establish_trust(candidate(), evidence=["MCP server annotation"], basis=TrustBasis.FIRST_PARTY_AUDIT)


def test_download_counts_are_rejected_as_evidence():
    with pytest.raises(DiscoveryError, match="never establish trust"):
        establish_trust(candidate(), evidence=["2M downloads"], basis=TrustBasis.PINNED_SIGNED_ARTIFACT)


def test_verified_evidence_establishes_trust():
    trusted = establish_trust(
        candidate(),
        evidence=["release v1.2.3 signature verified"],
        basis=TrustBasis.VERIFIED_UPSTREAM_RELEASE,
    )
    assert trusted.trust_basis == TrustBasis.VERIFIED_UPSTREAM_RELEASE
    assert not any("unverified" in w for w in assess_candidate(trusted).warnings)


def test_a_trust_basis_without_evidence_is_refused():
    with pytest.raises(DiscoveryError, match="named evidence"):
        establish_trust(candidate(), evidence=[], basis=TrustBasis.VERIFIED_UPSTREAM_RELEASE)


def test_unverified_basis_returns_the_candidate_unchanged():
    c = candidate()
    assert establish_trust(c).trust_basis == TrustBasis.UNVERIFIED


def test_unknown_trust_basis_is_refused_at_construction():
    with pytest.raises(DiscoveryError):
        candidate(trust_basis="vibes")


# --------------------------------------------------------------- AC5: licensing

@pytest.mark.parametrize("spdx", ["MIT", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0"])
def test_reviewed_permissive_licenses_allow_internal_use(spdx):
    assessment = assess_license(spdx)
    assert assessment.status == "reviewed-permissive"
    assert assessment.blocked_reuse == ()
    assert assessment.is_blocking is False


def test_missing_license_is_unlicensed_and_blocks_every_reuse():
    assessment = assess_license(None)
    assert assessment.status == "unlicensed"
    assert assessment.blocked_reuse
    assert assessment.is_blocking is True


def test_empty_license_string_is_unlicensed():
    assert assess_license("   ").status == "unlicensed"


def test_unreviewed_license_is_unknown_and_blocks():
    assessment = assess_license("Weird-1.0")
    assert assessment.status == "unknown"
    assert assessment.blocked_reuse
    assert "has not been reviewed" in (assessment.note or "")


def test_copyleft_allows_internal_use_but_blocks_redistribution():
    assessment = assess_license("GPL-3.0-only", reuse=("internal_use", "redistribution"))
    assert assessment.blocked_reuse == ("redistribution",)
    assert "internal_use" not in assessment.blocked_reuse


def test_agpl_blocks_network_service_reuse():
    assessment = assess_license("AGPL-3.0-only", reuse=("internal_use", "network_service"))
    assert "network_service" in assessment.blocked_reuse


def test_license_assessment_requires_a_reuse_class():
    with pytest.raises(DiscoveryError):
        assess_license("MIT", reuse=())


def test_unknown_license_candidate_is_blocked_for_the_affected_reuse_only():
    c = candidate(license=assess_license("Weird-1.0", reuse=("internal_use", "redistribution")))
    assessment = assess_candidate(c, reuse=("internal_use", "redistribution"))
    assert assessment.blocked_reuse
    assert assessment.is_blocked_entirely is True  # both classes blocked here


def test_unlicensed_candidate_is_blocked_and_never_registered_as_usable():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate(license=assess_license(None)))
    assessment = registry.assess("cand")
    assert assessment.is_blocked_entirely is True
    rec = registry.recommendation()
    assert rec.kind == RecommendationKind.BLOCKED


def test_a_blocked_candidate_does_not_hide_an_acceptable_one():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate(cid="bad", license=assess_license(None)))
    registry.register(candidate(cid="good"))
    rec = registry.recommendation()
    assert rec.kind == RecommendationKind.PROPOSE_FOR_REVIEW
    assert rec.candidates == ("good",)


# --------------------------------------------------------------- AC6: no authority

def test_assessment_refuses_to_grant_anything():
    with pytest.raises(d.ActivationRefusedError):
        Assessment(
            candidate_id="c",
            usable_for_reuse=("internal_use",),
            blocked_reuse=(),
            blockers=(),
            warnings=(),
            authorizes_execution=True,
        )
    with pytest.raises(d.ActivationRefusedError):
        Assessment(
            candidate_id="c",
            usable_for_reuse=(),
            blocked_reuse=(),
            blockers=(),
            warnings=(),
            authorizes_activation=True,
        )
    with pytest.raises(d.ActivationRefusedError):
        Assessment(
            candidate_id="c",
            usable_for_reuse=(),
            blocked_reuse=(),
            blockers=(),
            warnings=(),
            authorizes_installation=True,
        )


def test_registry_has_no_install_execute_or_download_verb():
    forbidden = {"install", "execute", "run", "download", "activate", "enable", "apply"}
    present = forbidden & set(dir(CandidateRegistry))
    assert present == set(), f"registry must not expose {present}"


def test_proposal_states_that_discovery_grants_nothing():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate())
    payload = registry.to_dict()
    assert payload["discovery_grants"] == {
        "execution": False,
        "installation": False,
        "activation": False,
        "network": False,
    }


# --------------------------------------------------------------- registry behaviour

def test_registering_the_same_candidate_twice_is_refused():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate())
    with pytest.raises(DiscoveryError, match="already registered"):
        registry.register(candidate())


def test_unknown_candidate_lookup_is_refused():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    with pytest.raises(DiscoveryError, match="unknown candidate"):
        registry.get("nope")


def test_candidates_are_listed_deterministically():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    for cid in ("zeta", "alpha", "mid"):
        registry.register(candidate(cid=cid))
    assert [c.candidate_id for c in registry.candidates] == ["alpha", "mid", "zeta"]


def test_proposal_output_is_byte_identical_for_identical_inputs():
    def build() -> str:
        registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
        registry.register(candidate())
        return render_proposal(registry)

    assert build() == build()


def test_proposal_digest_is_stable_and_changes_with_content():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate())
    first = registry.digest()
    assert first == registry.digest()
    registry.register(candidate(cid="other", name="Other"))
    assert registry.digest() != first


def test_proposal_is_json_serialisable_with_sorted_keys():
    registry = CandidateRegistry(gap=classify_gap(["no_tool"]))
    registry.register(candidate())
    text = render_proposal(registry)
    assert json.loads(text)["schema_version"] == d.PROPOSAL_SCHEMA_VERSION
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)
    assert text.endswith("\n")


def test_hardware_gap_with_no_candidates_reports_blocked_without_inventing_one():
    registry = CandidateRegistry(gap=classify_gap(["hardware_limit"]))
    rec = registry.recommendation()
    assert rec.kind == RecommendationKind.BLOCKED
    assert "nothing is installed" in rec.reason


def test_candidate_privileges_use_the_repository_capability_vocabulary():
    from scoped_grants import resolve_tool_capabilities

    caps = resolve_tool_capabilities("ctx_run_block", {"block": "python"})
    p = Privileges(code_execution=True, capabilities=caps)
    assert p.is_broad is True
    assert set(caps) <= set(p.to_dict()["capabilities"])


def test_license_assessment_requires_blocked_reuse_when_unknown():
    with pytest.raises(DiscoveryError):
        LicenseAssessment(spdx_id=None, status="unlicensed", blocked_reuse=())
