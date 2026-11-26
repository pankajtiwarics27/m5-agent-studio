"""AP-032 — release qualification and independent-agent handoff.

Every fixture is built here; no clock, no filesystem, no network, no CI API. The assertions are about
what a release pack is allowed to claim and what it must refuse to call done.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest

import release_qualification as rq


NOW = 1_767_225_600
HEAD = "a" * 40
BASE = "b" * 40
OTHER = "c" * 40
DIGEST = "d" * 64


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def evidence(**kw: Any) -> rq.Evidence:
    params: dict[str, Any] = {
        "evidence_id": "ev-1",
        "label": "Held-out website cases v3",
        "kind": "held-out",
        "scope": ("website",),
        "head_sha": HEAD,
        "passed": True,
    }
    params.update(kw)
    return rq.Evidence(**params)


def claim(**kw: Any) -> rq.Claim:
    params: dict[str, Any] = {
        "claim_id": "cl-1",
        "statement": "The website workflow produces a reviewed page",
        "scope": ("website",),
        "evidence_id": "ev-1",
    }
    params.update(kw)
    return rq.Claim(**params)


def suite(**kw: Any) -> rq.CaseSuite:
    params: dict[str, Any] = {
        "suite_id": "s-1",
        "version": "3.0.0",
        "kind": "held-out",
        "workflow": "website",
        "cases": 40,
        "passed": 40,
    }
    params.update(kw)
    return rq.CaseSuite(**params)


def all_suites(workflow: str = "website") -> tuple[rq.CaseSuite, ...]:
    return tuple(
        suite(suite_id=f"s-{kind}", kind=kind, workflow=workflow) for kind in ("held-out", "hostile", "failure", "restart", "resource")
    )


def workflow(**kw: Any) -> rq.WorkflowQualification:
    params: dict[str, Any] = {
        "workflow": "website",
        "suites": all_suites(),
        "artifact_review_evidence_id": "ev-review",
        "artifact_digest": DIGEST,
    }
    params.update(kw)
    return rq.WorkflowQualification(**params)


def approval(**kw: Any) -> rq.Approval:
    params: dict[str, Any] = {
        "kind": "security",
        "approver": "reviewer:independent-security",
        "author": "arena:agent",
        "approved_at": NOW - 3600,
        "evidence_digest": DIGEST,
    }
    params.update(kw)
    return rq.Approval(**params)


def surface(**kw: Any) -> rq.PreservationSurface:
    params: dict[str, Any] = {
        "surface_id": "chat",
        "label": "Chat",
        "verdict": "verified",
        "evidence_id": "ev-preserve",
    }
    params.update(kw)
    return rq.PreservationSurface(**params)


def drill(**kw: Any) -> rq.DrillRecord:
    params: dict[str, Any] = {"kind": "rollback", "exercised": True}
    params.update(kw)
    return rq.DrillRecord(**params)


def restore_drill(**kw: Any) -> rq.DrillRecord:
    params: dict[str, Any] = {"kind": "restore", "exercised": True, "restored_digest": DIGEST, "source_digest": DIGEST}
    params.update(kw)
    return rq.DrillRecord(**params)


def artifact(**kw: Any) -> rq.ArtifactCheck:
    params: dict[str, Any] = {
        "artifact_id": "art-1",
        "path": "docs/product-roadmap-status.md",
        "digest": DIGEST,
        "licence": "MIT",
    }
    params.update(kw)
    return rq.ArtifactCheck(**params)


def deferral(**kw: Any) -> rq.DeferredSpecialisation:
    params: dict[str, Any] = {
        "specialisation": "video",
        "revision_id": "rev-defer-video",
        "authorised_by": "owner:project-owner",
        "reason": "Owner selected the website pilot for launch; video stays deferred.",
        "authorised_at": NOW - 86_400,
    }
    params.update(kw)
    return rq.DeferredSpecialisation(**params)


def pack(**kw: Any) -> rq.QualificationPack:
    params: dict[str, Any] = {
        "head_sha": HEAD,
        "base_sha": BASE,
        "claims": (claim(),),
        "evidence": (evidence(), evidence(evidence_id="ev-review", kind="review")),
        "selected": ("website",),
        "deferred": (deferral(),),
        "preservation": (surface(),),
        "workflows": (workflow(),),
        "approvals": (approval(), approval(kind="screenshot", approver="reviewer:visual-review")),
        "ci": rq.CiVerdict(head_sha=HEAD, run_id=456_365_194_28, conclusion="success"),
        "drills": (drill(), drill(kind="migration"), restore_drill()),
        "artifacts": (artifact(),),
        "now": NOW,
    }
    params.update(kw)
    return rq.QualificationPack(**params)


# ---------------------------------------------------------------------------
# AC1 — a deferral is an authorised ledger revision, not a hole.
# ---------------------------------------------------------------------------


def test_a_complete_pack_is_qualified() -> None:
    built = pack()
    assert built.qualified is True
    assert built.decision == "release-qualified"
    assert built.blocking_reasons == ()


def test_the_unselected_pilot_must_be_deferred_not_omitted() -> None:
    built = pack(deferred=())
    assert built.unaccounted_specialisations == ("video",)
    assert any("neither qualified nor deferred" in reason for reason in built.blocking_reasons)
    assert built.qualified is False


def test_a_deferral_names_a_human_owner() -> None:
    with pytest.raises(rq.DeferralRefused, match="named human"):
        deferral(authorised_by="the team")


def test_a_reviewer_cannot_decide_what_ships() -> None:
    with pytest.raises(rq.DeferralRefused, match="owner decision"):
        deferral(authorised_by="reviewer:independent-security")


def test_a_deferral_needs_a_reason() -> None:
    with pytest.raises(rq.ReleaseQualificationError, match="reason must not be empty"):
        deferral(reason="   ")


def test_an_unknown_specialisation_cannot_be_deferred() -> None:
    with pytest.raises(rq.DeferralRefused, match="unknown specialisation"):
        deferral(specialisation="podcast")


def test_the_same_specialisation_cannot_be_deferred_twice() -> None:
    with pytest.raises(rq.DeferralRefused, match="deferred twice"):
        pack(deferred=(deferral(), deferral(revision_id="rev-2")))


def test_a_specialisation_cannot_be_both_selected_and_deferred() -> None:
    with pytest.raises(rq.DeferralRefused, match="both selected for launch and deferred"):
        pack(deferred=(deferral(specialisation="website"),))


def test_a_release_with_no_scope_has_no_decision() -> None:
    assert pack(selected=()).qualified is False
    assert any("no scope" in reason for reason in pack(selected=()).blocking_reasons)


def test_deferring_the_shipped_pilot_leaves_nothing_selected() -> None:
    built = pack(selected=("video",), deferred=(deferral(specialisation="website"),), workflows=(workflow(workflow="video", suites=all_suites("video")),))
    assert built.qualified is True
    assert built.unaccounted_specialisations == ()


# ---------------------------------------------------------------------------
# AC2 — preservation is verified, never assumed.
# ---------------------------------------------------------------------------


def test_every_preserved_surface_carries_an_explicit_verdict() -> None:
    with pytest.raises(rq.PreservationRefused, match="unknown preservation verdict"):
        surface(verdict="probably")


def test_a_verified_surface_needs_evidence() -> None:
    with pytest.raises(rq.PreservationRefused, match="without an observation is an opinion"):
        surface(evidence_id="")


def test_not_applicable_needs_an_argument() -> None:
    with pytest.raises(rq.PreservationRefused, match="needs an argument"):
        surface(verdict="not-applicable")


def test_a_missing_surface_blocks_an_otherwise_green_release() -> None:
    built = pack(preservation=(surface(), surface(surface_id="chat-2", verdict="missing", evidence_id="")))
    assert built.qualified is False
    assert any("failed verification" in reason for reason in built.blocking_reasons)


def test_a_partially_verified_surface_blocks() -> None:
    built = pack(preservation=(surface(verdict="partially-verified"),))
    assert surface(verdict="partially-verified").intact is False
    assert built.qualified is False


def test_reporting_no_preserved_surface_at_all_blocks() -> None:
    built = pack(preservation=())
    assert any("unverified, not preserved" in reason for reason in built.blocking_reasons)


# ---------------------------------------------------------------------------
# AC3 — versioned held-out, hostile, failure, restart and resource cases.
# ---------------------------------------------------------------------------


def test_a_suite_tuned_against_its_own_version_cannot_be_held_out() -> None:
    with pytest.raises(rq.SuiteRefused, match="cannot also be held-out evidence"):
        suite(tuned_against_version="3.0.0")


def test_a_tuned_suite_may_be_any_other_kind() -> None:
    assert suite(kind="resource", tuned_against_version="2.0.0").held_out is False


def test_held_out_requires_no_tuning() -> None:
    assert suite().held_out is True


def test_a_workflow_mixing_case_versions_is_refused() -> None:
    with pytest.raises(rq.SuiteRefused, match="mixes case versions"):
        workflow(suites=(suite(), suite(suite_id="s-2", kind="hostile", version="2.0.0")))


def test_a_workflow_with_no_cases_is_refused() -> None:
    with pytest.raises(rq.SuiteRefused, match="qualification with no cases"):
        workflow(suites=())


def test_a_suite_filed_under_the_wrong_workflow_is_refused() -> None:
    with pytest.raises(rq.SuiteRefused, match="belongs to"):
        workflow(workflow="video", suites=all_suites("website"))


def test_a_suite_cannot_pass_more_than_it_ran() -> None:
    with pytest.raises(rq.SuiteRefused, match="more cases than it ran"):
        suite(cases=10, passed=11)


def test_missing_case_families_block_the_pack() -> None:
    partial = workflow(suites=(suite(), suite(suite_id="s-2", kind="hostile")))
    built = pack(workflows=(partial,))
    assert partial.missing_families == ("failure", "restart", "resource")
    assert any("was not run against" in reason for reason in built.blocking_reasons)


def test_a_failing_case_blocks_the_pack() -> None:
    built = pack(workflows=(workflow(suites=(*all_suites()[:-1], suite(suite_id="s-resource", kind="resource", passed=39))),))
    assert built.qualified is False
    assert any("failing case" in reason for reason in built.blocking_reasons)


def test_the_artifact_review_is_filed_with_the_workflow() -> None:
    assert workflow().to_mapping()["artifact_review_evidence_id"] == "ev-review"
    assert len(workflow().artifact_digest) == 64


# ---------------------------------------------------------------------------
# AC4 — approvals are real, current and independent.
# ---------------------------------------------------------------------------


def test_the_author_cannot_approve_their_own_release() -> None:
    with pytest.raises(rq.ApprovalRefused, match="cannot sign the security approval"):
        approval(approver="arena:agent", author="arena:agent")


def test_an_agent_name_cannot_be_the_approver() -> None:
    with pytest.raises(rq.ApprovalRefused, match="reads as an agent"):
        approval(approver="user:claude-reviewer")


def test_an_approver_must_be_a_named_human() -> None:
    with pytest.raises(rq.ApprovalRefused, match="not a named human reviewer"):
        approval(approver="security team")


def test_a_stale_approval_blocks() -> None:
    stale = approval(approved_at=NOW - 40 * 24 * 3600)
    built = pack(approvals=(stale, approval(kind="screenshot", approver="reviewer:visual-review")))
    assert stale.is_current(NOW) is False
    assert any("stale" in reason for reason in built.blocking_reasons)


def test_an_approval_from_the_future_is_not_current() -> None:
    assert approval(approved_at=NOW + 3600).is_current(NOW) is False


def test_both_approval_kinds_are_required() -> None:
    built = pack(approvals=(approval(),))
    assert built.missing_approvals == ("screenshot",)
    assert any("approvals missing" in reason for reason in built.blocking_reasons)


def test_two_signatures_of_one_kind_do_not_replace_the_other() -> None:
    with pytest.raises(rq.ApprovalRefused, match="second signature"):
        pack(
            approvals=(
                approval(),
                approval(approver="reviewer:another-security"),
                approval(kind="screenshot", approver="reviewer:visual-review"),
            )
        )


def test_screenshot_review_is_a_separate_gate_from_security() -> None:
    built = pack(approvals=(approval(kind="screenshot", approver="reviewer:visual-review"),))
    assert built.missing_approvals == ("security",)


def test_the_approval_mapping_states_current_and_expiry() -> None:
    mapping = approval().to_mapping(now=NOW)
    assert mapping["current"] is True
    assert mapping["expires_at"] > mapping["approved_at"]


def test_an_unknown_approval_kind_is_refused() -> None:
    with pytest.raises(rq.ApprovalRefused, match="unknown approval kind"):
        approval(kind="performance")


# ---------------------------------------------------------------------------
# AC5 — CI green at the head being shipped; claims no wider than the tests.
# ---------------------------------------------------------------------------


def test_ci_at_another_commit_does_not_qualify_the_head() -> None:
    built = pack(ci=rq.CiVerdict(head_sha=OTHER, run_id=1, conclusion="success"))
    assert built.ci.qualifies(HEAD) is False
    assert any("a green run elsewhere is a different build" in reason for reason in built.blocking_reasons)


def test_a_failing_ci_run_blocks_even_at_the_right_head() -> None:
    built = pack(ci=rq.CiVerdict(head_sha=HEAD, run_id=1, conclusion="failure"))
    assert built.qualified is False


def test_a_claim_cannot_be_wider_than_its_evidence() -> None:
    with pytest.raises(rq.ClaimRefused, match="a claim may not be wider than what was tested"):
        pack(claims=(claim(scope=("website", "video")),))


def test_wildcard_evidence_covers_every_scope() -> None:
    built = pack(claims=(claim(scope=("website", "video")),), evidence=(evidence(scope=("*",)), evidence(evidence_id="ev-review", kind="review")))
    assert len(built.published_claims) == 1


def test_a_claim_citing_absent_evidence_is_refused() -> None:
    with pytest.raises(rq.ClaimRefused, match="which the pack does not contain"):
        pack(claims=(claim(evidence_id="ev-ghost"),))


def test_failing_evidence_publishes_no_claim() -> None:
    built = pack(evidence=(evidence(passed=False), evidence(evidence_id="ev-review", kind="review")))
    assert built.published_claims == ()
    assert [item.claim_id for item in built.uncovered_claims] == ["cl-1"]
    assert any("lack passing evidence at this head" in reason for reason in built.blocking_reasons)


def test_evidence_at_another_head_publishes_no_claim() -> None:
    built = pack(evidence=(evidence(head_sha=OTHER), evidence(evidence_id="ev-review", kind="review")))
    assert built.published_claims == ()
    assert built.qualified is False


def test_the_pack_reports_claimed_against_published_counts() -> None:
    mapping = pack().to_mapping()["claims"]
    assert mapping["claimed_count"] == 1
    assert mapping["published_count"] == 1
    assert mapping["published"][0]["claim_id"] == "cl-1"


def test_an_empty_scope_claim_is_refused() -> None:
    with pytest.raises(rq.ClaimRefused, match="covers nothing"):
        claim(scope=())


def test_the_same_evidence_id_cannot_be_used_twice() -> None:
    with pytest.raises(rq.ReleaseQualificationError, match="used twice"):
        pack(evidence=(evidence(), evidence()))


def test_base_and_head_must_differ() -> None:
    with pytest.raises(rq.ReleaseQualificationError, match="base and head are the same commit"):
        pack(base_sha=HEAD)


# ---------------------------------------------------------------------------
# AC6 — drills exercised; artifacts clean; handoff honest.
# ---------------------------------------------------------------------------


def test_rollback_migration_and_restore_must_be_exercised() -> None:
    built = pack(drills=(drill(), restore_drill()))
    assert built.missing_drills == ("migration",)
    assert any("drills not exercised" in reason for reason in built.blocking_reasons)


def test_a_restore_that_was_not_compared_proves_nothing() -> None:
    with pytest.raises(rq.DrillRefused, match="record both digests"):
        restore_drill(source_digest="")


def test_a_restore_with_mismatched_digests_does_not_prove_restore() -> None:
    built = pack(drills=(drill(), drill(kind="migration"), restore_drill(restored_digest="f" * 64)))
    assert restore_drill(restored_digest="f" * 64).proves_restore is False
    assert any("restored bytes were never compared" in reason for reason in built.blocking_reasons)


def test_a_documented_but_unexercised_drill_does_not_count() -> None:
    built = pack(drills=(drill(), drill(kind="migration"), restore_drill(exercised=False)))
    assert built.missing_drills == ("restore",)
    assert built.qualified is False


def test_an_unlicensed_artifact_cannot_ship() -> None:
    with pytest.raises(rq.ArtifactRefused, match="unlicensed output"):
        artifact(licence="unknown")


def test_a_secret_finding_blocks_the_pack() -> None:
    built = pack(artifacts=(artifact(secret_findings=2),))
    assert artifact(secret_findings=2).clean is False
    assert any("secret findings" in reason for reason in built.blocking_reasons)


def test_an_artifact_needs_a_digest() -> None:
    with pytest.raises(rq.ReleaseQualificationError, match="SHA-256 hex digest"):
        artifact(digest="")


def test_the_handoff_states_that_the_author_cannot_approve() -> None:
    handoff = pack().to_mapping()["handoff"]
    assert handoff["author_can_approve"] is False
    assert handoff["claim_scope_matches_tested_scope"] is True


def test_the_pack_is_reproducible_and_serializable() -> None:
    first, second = pack(), pack()
    assert first.fingerprint() == second.fingerprint()
    json.loads(json.dumps(first.to_mapping()))


def test_every_blocking_reason_is_reported_not_just_the_first() -> None:
    broken = pack(
        deferred=(),
        claims=(claim(),),
        preservation=(surface(verdict="missing"),),
        workflows=(),
        approvals=(),
        ci=rq.CiVerdict(head_sha=OTHER, run_id=1, conclusion="failure"),
        drills=(),
        artifacts=(artifact(secret_findings=1),),
    )
    reasons = broken.blocking_reasons
    assert len(reasons) >= 7
    assert any("neither qualified nor deferred" in reason for reason in reasons)
    assert any("no preserved surface" in reason or "failed verification" in reason for reason in reasons)
    assert any("no complete qualification" in reason for reason in reasons)
    assert any("approvals missing" in reason for reason in reasons)
    assert any("drills not exercised" in reason for reason in reasons)
    assert any("secret findings" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    tree = ast.parse(open("release_qualification.py", encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}


def test_now_is_a_required_parameter() -> None:
    source = open("release_qualification.py", encoding="utf-8").read()
    assert "time.time()" not in source
    assert "import time" not in source


# ---------------------------------------------------------------------------
# The public factory (previously untested: every test built the dataclass directly).
# ---------------------------------------------------------------------------


def test_qualify_release_is_the_supported_entry_point() -> None:
    """The documented constructor must behave like the dataclass it wraps."""
    built = rq.qualify_release(
        head_sha=HEAD,
        base_sha=BASE,
        claims=(claim(),),
        evidence=(evidence(), evidence(evidence_id="ev-review", kind="review")),
        selected=("website",),
        ci=rq.CiVerdict(head_sha=HEAD, run_id=456_365_194_28, conclusion="success"),
        now=NOW,
        deferred=(deferral(),),
        preservation=(surface(),),
        workflows=(workflow(),),
        approvals=(approval(), approval(kind="screenshot", approver="reviewer:visual-review")),
        drills=(drill(), drill(kind="migration"), restore_drill()),
        artifacts=(artifact(),),
    )
    assert built.decision == "release-qualified"
    assert built.fingerprint() == pack().fingerprint()


def test_qualify_release_defaults_to_an_honest_block() -> None:
    """Called with only the required arguments, it must not invent a qualification."""
    built = rq.qualify_release(
        head_sha=HEAD,
        base_sha=BASE,
        claims=(),
        evidence=(),
        selected=(),
        ci=rq.CiVerdict(head_sha=HEAD, run_id=1, conclusion="success"),
        now=NOW,
    )
    assert built.qualified is False
    assert built.decision == "release-blocked"
    assert any("no scope" in reason for reason in built.blocking_reasons)
    assert any("approvals missing" in reason for reason in built.blocking_reasons)
    assert any("drills not exercised" in reason for reason in built.blocking_reasons)


def test_qualify_release_records_the_author_that_cannot_approve() -> None:
    built = rq.qualify_release(
        head_sha=HEAD, base_sha=BASE, claims=(), evidence=(), selected=(),
        ci=rq.CiVerdict(head_sha=HEAD, run_id=1, conclusion="success"), now=NOW,
        author="arena:ab195e0f",
    )
    assert built.to_mapping()["handoff"]["author"] == "arena:ab195e0f"
    assert built.to_mapping()["handoff"]["author_can_approve"] is False
