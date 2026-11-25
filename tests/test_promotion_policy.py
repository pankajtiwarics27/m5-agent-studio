"""AP-028 — promotion by risk and evidence, with rollback and version pinning.

Every fixture is a value this file constructs against the repository's real operating-envelope
register (owner decision U05). No route, runtime, provider or network is contacted.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest

import promotion_policy as pp


def d(char: str) -> str:
    return char * 64


def policy() -> pp.PromotionPolicySnapshot:
    return pp.load_policy()


def effects(**kw: Any) -> pp.ChangeEffects:
    params: dict[str, Any] = {"paths_touched": ("docs/lessons.md",), "changes_retrieval_or_prompting": True}
    params.update(kw)
    return pp.ChangeEffects(**params)


def owner_ok(digest: str = None) -> pp.Approval:  # type: ignore[assignment]
    return pp.Approval(approver="owner:project-owner", approval_type="owner", approved_digest=digest or d("b"))


def security_ok(digest: str = None) -> pp.Approval:  # type: ignore[assignment]
    return pp.Approval(
        approver="security:reviewer-2", approval_type="owner_and_security", approved_digest=digest or d("b")
    )


def independent_ok(digest: str = None) -> pp.Approval:  # type: ignore[assignment]
    return pp.Approval(
        approver="independent:reviewer-9",
        approval_type="independent_and_owner",
        approved_digest=digest or d("b"),
    )


def held_out(**kw: Any) -> pp.HeldOutSummary:
    params: dict[str, Any] = {
        "candidate_digest": d("b"),
        "evaluator_digest": d("e"),
        "tasks": 12,
        "passed": 12,
        "thresholds_approved_by": "owner:project-owner",
        "threshold_version": "thresholds-v3",
    }
    params.update(kw)
    return pp.HeldOutSummary(**params)


def request(**kw: Any) -> pp.PromotionRequest:
    params: dict[str, Any] = {
        "candidate_id": "candidate-1",
        "candidate_digest": d("b"),
        "effects": effects(),
        "requested_stage": "default",
        "approvals": (owner_ok(),),
        "held_out": held_out(),
        "rollback_target_digest": d("a"),
    }
    params.update(kw)
    return pp.PromotionRequest(**params)


def stages(*, clean: bool = True) -> tuple[pp.StageResult, ...]:
    return tuple(
        pp.StageResult(stage=stage, tasks=8, regressions=() if clean else (f"{stage} regression",))
        for stage in pp.PROMOTION_STAGES
    )


# ---------------------------------------------------------------------------
# AC1 — the classifier is outside the proposing model.
# ---------------------------------------------------------------------------


def test_classification_reads_effects_not_a_declared_risk() -> None:
    mapping = request().to_mapping()
    assert mapping["proposer_declared_risk"] is None
    assert mapping["classification_by"].startswith("promotion_policy/v1")


def test_a_change_with_no_declared_paths_cannot_be_classified() -> None:
    with pytest.raises(pp.ClassificationRefused, match="no declared paths"):
        pp.ChangeEffects(paths_touched=())


def test_an_unknown_capability_is_refused_rather_than_trusted() -> None:
    with pytest.raises(pp.ClassificationRefused, match="unknown capability"):
        effects(capabilities_requested=("full_disk_access",))


def test_capabilities_come_from_the_existing_taxonomy() -> None:
    classified = pp.classify_change(effects(capabilities_requested=("workspace_read",)))
    assert classified.class_id == "project_lesson"


def test_editing_an_authority_control_escalates_to_the_strongest_class() -> None:
    classified = pp.classify_change(effects(authority_controls_touched=("evaluator:held-out-v2",)))
    assert classified.class_id == "authority_evaluator_or_runtime"
    assert classified.review_boundary == "independent_and_owner"
    assert "authority controls" in classified.escalated_because[0]


def test_an_unrecognised_authority_control_kind_is_refused() -> None:
    with pytest.raises(pp.ClassificationRefused, match="unknown authority control kind"):
        effects(authority_controls_touched=("vibes:something",))


def test_shipping_executable_behaviour_needs_security_review() -> None:
    classified = pp.classify_change(
        effects(executable_artifacts=("tools/new_adapter.py",), changes_retrieval_or_prompting=False, paths_touched=("tools/new_adapter.py",))
    )
    assert classified.class_id == "executable_skill_or_adapter"
    assert classified.review_boundary == "owner_and_security"


def test_a_retrieval_change_is_a_project_lesson() -> None:
    classified = pp.classify_change(effects())
    assert classified.class_id == "project_lesson"
    assert classified.review_boundary == "owner"


def test_no_class_is_ever_lowered_by_a_later_effect() -> None:
    weak = pp.classify_change(effects(changes_retrieval_or_prompting=False, paths_touched=("docs/a.md",)))
    storng = pp.classify_change(
        effects(paths_touched=("docs/a.md",), changes_retrieval_or_prompting=False, executable_artifacts=("x.py",))
    )
    assert pp.REVIEW_BOUNDARIES.index(storng.review_boundary) > pp.REVIEW_BOUNDARIES.index(weak.review_boundary)


def test_a_change_that_declares_new_authority_says_so_in_the_classification() -> None:
    classified = pp.classify_change(effects(egress_hosts=("example.com",)))
    assert any("new authority" in reason for reason in classified.escalated_because)


# ---------------------------------------------------------------------------
# AC2 — automatic promotion needs an owner-enabled class *and* thresholded evidence.
# ---------------------------------------------------------------------------


def test_the_policy_is_read_from_the_owner_register_not_hard_coded() -> None:
    snapshot = policy()
    assert snapshot.decision_id == "U05"
    assert snapshot.automatic_classes == ()
    assert snapshot.boundary_for("project_lesson") == "owner"
    assert snapshot.boundary_for("authority_evaluator_or_runtime") == "independent_and_owner"


def test_owner_decision_keeps_every_class_manual() -> None:
    assert policy().any_automatic is False


def test_a_policy_that_does_not_deny_by_default_is_refused() -> None:
    with pytest.raises(pp.ClassificationRefused, match="default effect"):
        pp.PromotionPolicySnapshot(
            automatic_classes=(), boundaries={c: "owner" for c in pp.CLASS_IDS}, default_effect="allow", decision_id="x"
        )


def test_a_policy_missing_a_boundary_is_refused() -> None:
    with pytest.raises(pp.ClassificationRefused, match="no review boundary"):
        pp.PromotionPolicySnapshot(
            automatic_classes=(), boundaries={"project_lesson": "owner"}, default_effect="deny_promotion", decision_id="x"
        )


def test_an_automatic_approval_approves_nothing() -> None:
    gap = pp.PromotionRequest(
        candidate_id="c1",
        candidate_digest=d("b"),
        effects=effects(),
        requested_stage="default",
        approvals=(pp.Approval(approver="owner:project-owner", approval_type="automatic", approved_digest=d("b")),),
        held_out=held_out(),
        rollback_target_digest=d("a"),
    )
    assert any("cannot approve anything" in reason for reason in gap.refusals())


def test_automatic_promotion_would_still_need_held_out_results() -> None:
    """A hypothetical owner-enabled class: the flag is not the only gate."""
    enabled = pp.PromotionPolicySnapshot(
        automatic_classes=("project_lesson",),
        boundaries={c: "owner" for c in pp.CLASS_IDS},
        default_effect="deny_promotion",
        decision_id="U05-hypothetical",
    )
    without = pp.PromotionRequest(
        candidate_id="c1",
        candidate_digest=d("b"),
        effects=effects(),
        requested_stage="default",
        rollback_target_digest=d("a"),
    )
    reasons = without.refusals(policy=enabled)
    assert any("approved thresholds and relevant held-out results" in reason for reason in reasons)


def test_an_enabled_class_with_evidence_still_needs_a_rollback_target() -> None:
    enabled = pp.PromotionPolicySnapshot(
        automatic_classes=("project_lesson",),
        boundaries={c: "owner" for c in pp.CLASS_IDS},
        default_effect="deny_promotion",
        decision_id="U05-hypothetical",
    )
    without = pp.PromotionRequest(
        candidate_id="c1",
        candidate_digest=d("b"),
        effects=effects(),
        requested_stage="default",
        held_out=held_out(),
    )
    assert any("rollback target" in reason for reason in without.refusals(policy=enabled))


def test_held_out_results_about_another_revision_are_refused() -> None:
    with pytest.raises(pp.EvidenceRefused, match="different candidate revision"):
        request(held_out=held_out(candidate_digest=d("f")))


def test_thresholds_must_be_approved_by_a_named_human() -> None:
    with pytest.raises(pp.EvidenceRefused, match="named human"):
        held_out(thresholds_approved_by="the optimiser")


def test_an_empty_held_out_run_supports_no_conclusion() -> None:
    summary = held_out(tasks=0, passed=0)
    gap = request(held_out=summary)
    reasons = gap.refusals()
    assert any("ran no tasks" in reason for reason in reasons)


def test_a_held_out_summary_cannot_pass_more_than_it_ran() -> None:
    with pytest.raises(pp.EvidenceRefused, match="more tasks than it ran"):
        held_out(tasks=3, passed=4)


# ---------------------------------------------------------------------------
# AC3 — new egress, accounts, spend and defaults need their own grant.
# ---------------------------------------------------------------------------


def test_a_new_egress_host_without_a_grant_is_refused() -> None:
    gap = request(effects=effects(egress_hosts=("example.com",)))
    reasons = gap.refusals()
    assert any("new authority (egress)" in reason for reason in reasons)
    assert any("owner approval of a promotion is not a grant" in reason for reason in reasons)


def test_a_grant_naming_the_effect_satisfies_it() -> None:
    allowed = request(effects=effects(egress_hosts=("example.com",)), grants=("grant-7:egress",))
    assert not any("egress" in reason for reason in allowed.refusals())


def test_a_grant_for_a_different_effect_does_not_cover_this_one() -> None:
    wrong = request(effects=effects(spend_minor_units=500), grants=("grant-7:egress",))
    assert any("new authority (spending)" in reason for reason in wrong.refusals())


def test_spend_is_new_authority_too() -> None:
    assert "spending" in effects(spend_minor_units=1).gated()
    assert "accounts" in effects(accounts=("acct-2",)).gated()
    assert "sensitive_defaults" in effects(sensitive_defaults=("tools_enabled_on",)).gated()


def test_a_negative_spend_is_refused() -> None:
    with pytest.raises(pp.ClassificationRefused, match="must not be negative"):
        effects(spend_minor_units=-1)


def test_an_approval_must_name_a_human_or_independent_reviewer() -> None:
    with pytest.raises(pp.AuthorityRefused, match="cannot approve"):
        pp.Approval(approver="model:local-7", approval_type="owner", approved_digest=d("b"))


def test_an_approval_for_another_revision_is_not_approval() -> None:
    with pytest.raises(pp.AuthorityRefused, match="not this candidate"):
        request(approvals=(owner_ok(d("f")),))


def test_security_signature_cannot_be_an_owner_signature() -> None:
    with pytest.raises(pp.AuthorityRefused, match="needs a security or independent reviewer"):
        pp.Approval(approver="owner:project-owner", approval_type="owner_and_security", approved_digest=d("b"))


def test_one_person_cannot_sign_both_sides_of_a_two_party_boundary() -> None:
    with pytest.raises(pp.AuthorityRefused, match="needs a security or independent reviewer"):
        pp.Approval(approver="owner:project-owner", approval_type="owner_and_security", approved_digest=d("b"))


def test_an_owner_who_is_also_the_reviewer_is_one_opinion() -> None:
    shared = pp.PromotionRequest(
        candidate_id="c1",
        candidate_digest=d("b"),
        effects=effects(executable_artifacts=("tools/x.py",), paths_touched=("tools/x.py",), changes_retrieval_or_prompting=False),
        requested_stage="default",
        approvals=(owner_ok(), security_ok()),
        held_out=held_out(),
        rollback_target_digest=d("a"),
    )
    assert shared.refusals() == ()


def test_authority_changes_need_an_independent_reviewer() -> None:
    authority = request(
        effects=effects(authority_controls_touched=("evaluator:held-out-v3",)),
        approvals=(owner_ok(), security_ok()),
    )
    assert any("independent reviewer is required" in reason for reason in authority.refusals())
    approved = pp.PromotionRequest(
        candidate_id="c1",
        candidate_digest=d("b"),
        effects=effects(authority_controls_touched=("evaluator:held-out-v3",)),
        requested_stage="canary",
        approvals=(owner_ok(), independent_ok()),
        held_out=held_out(),
        rollback_target_digest=d("a"),
    )
    assert approved.refusals() == ()


# ---------------------------------------------------------------------------
# AC4 — a candidate cannot edit the controls it passes by.
# ---------------------------------------------------------------------------


def test_an_authority_edit_cannot_be_promoted_straight_to_a_narrow_stage() -> None:
    narrow = request(
        effects=effects(authority_controls_touched=("approval_policy:v1",)),
        approvals=(owner_ok(), independent_ok()),
        requested_stage="task",
    )
    assert any("needs a canary" in reason for reason in narrow.refusals())


def test_authority_controls_are_a_closed_set() -> None:
    assert "evaluator" in pp.AUTHORITY_CONTROL_KINDS
    assert "sandbox_policy" in pp.AUTHORITY_CONTROL_KINDS
    assert "audit_log" in pp.AUTHORITY_CONTROL_KINDS
    assert "promotion_policy" in pp.AUTHORITY_CONTROL_KINDS


def test_an_authority_edit_needs_the_strongest_review_even_if_it_looks_like_a_lesson() -> None:
    classified = pp.classify_change(effects(authority_controls_touched=("audit_log:v2",)))
    assert classified.review_boundary == "independent_and_owner"


# ---------------------------------------------------------------------------
# AC5 — a canary regression rolls back, bounded, and invalidates downstream evidence.
# ---------------------------------------------------------------------------


def test_every_stage_running_clean_and_in_order_is_a_promotion() -> None:
    outcome = pp.run_stages(request(), results=stages())
    assert outcome.promoted is True
    assert outcome.status == "promoted"
    assert outcome.regression is None


def test_stages_out_of_order_are_refused() -> None:
    with pytest.raises(pp.StageRefused, match="promotion order"):
        pp.run_stages(request(), results=(stages()[1], stages()[0]))


def test_a_stage_nobody_observed_is_not_a_pass() -> None:
    unobserved = tuple(
        pp.StageResult(stage=stage, tasks=0) for stage in pp.PROMOTION_STAGES
    )
    with pytest.raises(pp.StageRefused, match="ran no tasks"):
        pp.run_stages(request(), results=unobserved)


def test_a_canary_regression_stops_the_promotion_at_the_failing_stage() -> None:
    regression = pp.CanaryRegression(
        stage="canary", detail="5% of canary tasks failed schema validation", invalidated_artifacts=("art-9",)
    )
    outcome = pp.run_stages(request(supersedes_digest=d("c")), results=stages(), regression=regression)
    assert outcome.promoted is False
    assert outcome.status == "rolled_back"
    assert outcome.reached_stage == "canary"
    assert [item.stage for item in outcome.results] == ["task", "project", "canary"]
    assert outcome.rolled_back_to == d("a")
    assert outcome.invalidated_artifacts == ("art-9",)
    assert outcome.evidence_superseded is True


def test_a_regression_that_says_nothing_about_artifacts_is_not_honest() -> None:
    with pytest.raises(pp.RollbackRefused, match="silence is indistinguishable"):
        pp.CanaryRegression(stage="canary", detail="something broke")


def test_a_regression_can_state_that_no_artifact_existed() -> None:
    clean = pp.CanaryRegression(
        stage="canary", detail="the canary never produced an artifact", no_downstream_artifacts=True
    )
    outcome = pp.run_stages(request(supersedes_digest=d("c")), results=stages(), regression=clean)
    assert outcome.status == "rolled_back"
    assert outcome.invalidated_artifacts == ()


def test_an_unbounded_rollback_is_refused() -> None:
    with pytest.raises(pp.RollbackRefused, match="bounded"):
        pp.CanaryRegression(stage="canary", detail="broke", invalidated_artifacts=("a",), bounded=False)


def test_a_regression_at_a_stage_that_never_ran_is_refused() -> None:
    regression = pp.CanaryRegression(stage="default", detail="broke", invalidated_artifacts=("a",))
    with pytest.raises(pp.RollbackRefused, match="never ran"):
        pp.run_stages(request(), results=stages()[:2], regression=regression)


def test_a_promotion_without_a_rollback_target_cannot_run() -> None:
    with pytest.raises(pp.AuthorityRefused, match="rollback target"):
        pp.run_stages(request(rollback_target_digest=None), results=stages())


def test_a_stage_result_with_regressions_is_not_clean() -> None:
    dirty = pp.StageResult(stage="task", tasks=4, regressions=("one task regressed",))
    assert dirty.clean is False
    assert dirty.observed is True


# ---------------------------------------------------------------------------
# AC6 — prior versions stay recoverable; in-flight tasks keep their version.
# ---------------------------------------------------------------------------


def record(record_id: str, action: str, digest: str, *, stage: str = "default", restored: str | None = None, **kw: Any) -> pp.PromotionRecord:
    return pp.PromotionRecord(
        record_id=record_id,
        candidate_id=f"candidate-{record_id}",
        candidate_digest=digest,
        action=action,
        stage=stage,
        restored_digest=restored,
        **kw,
    )


def ledger() -> pp.PromotionLedger:
    return pp.PromotionLedger(records=(record("r1", "promoted", d("b"), restored=d("a")),))


def test_the_ledger_is_append_only() -> None:
    base = ledger()
    grown = base.append(record("r2", "promoted", d("c"), restored=d("b")))
    assert len(base.records) == 1
    assert len(grown.records) == 2


def test_a_reused_record_id_is_refused() -> None:
    with pytest.raises(pp.PromotionError, match="already exists"):
        ledger().append(record("r1", "promoted", d("c"), restored=d("b")))


def test_duplicate_ids_in_a_constructed_ledger_are_refused() -> None:
    with pytest.raises(pp.PromotionError, match="duplicate ledger record id"):
        pp.PromotionLedger(records=(record("r1", "promoted", d("b")), record("r1", "promoted", d("c"))))


def test_a_rollback_restores_a_version_that_existed() -> None:
    promoted = ledger().append(record("r2", "promoted", d("c"), restored=d("b")))
    rolled = promoted.append(record("r3", "rolled_back", d("c"), restored=d("b")))
    assert rolled.active_digest() == d("b")
    assert rolled.is_recoverable(d("b")) and rolled.is_recoverable(d("a")) and rolled.is_recoverable(d("c"))


def test_an_undo_aimed_at_a_version_that_is_not_deployed_is_refused() -> None:
    with pytest.raises(pp.RollbackRefused, match="not deployed"):
        ledger().append(record("r2", "rolled_back", d("c"), restored=d("a")))


def test_a_rollback_to_a_version_never_promoted_is_refused() -> None:
    promoted = ledger().append(record("r2", "promoted", d("c"), restored=d("b")))
    with pytest.raises(pp.RollbackRefused, match="not recoverable"):
        promoted.append(record("r3", "rolled_back", d("c"), restored=d("f")))


def test_a_rollback_with_no_destination_is_refused() -> None:
    with pytest.raises(pp.RollbackRefused, match="must name the version it restored"):
        record("r2", "rolled_back", d("c"))


def test_a_rollback_that_undoes_nothing_is_refused() -> None:
    promoted = ledger().append(record("r2", "promoted", d("c"), restored=d("b")))
    with pytest.raises(pp.RollbackRefused, match="already active"):
        promoted.append(record("r3", "rolled_back", d("c"), restored=d("c")))


def test_re_promoting_the_active_version_records_no_change() -> None:
    with pytest.raises(pp.PromotionError, match="already active"):
        ledger().append(record("r2", "promoted", d("b")))


def test_a_replacement_promotion_must_name_what_it_supersedes() -> None:
    with pytest.raises(pp.PromotionError, match="names the version it supersedes"):
        ledger().append(record("r2", "promoted", d("c")))


def test_recovered_versions_are_listed_so_a_rollback_target_is_checkable() -> None:
    mapping = ledger().to_mapping()
    assert mapping["recoverable_digests"] == [d("b"), d("a")]
    assert mapping["active_digest"] == d("b")


def test_an_artifact_no_record_mentions_is_not_called_valid() -> None:
    base = ledger()
    assert base.artifact_state("art-9") == "unmentioned"
    withdrawn = base.append(record("r2", "rolled_back", d("b"), restored=d("a"), invalidated_artifacts=("art-9",)))
    assert withdrawn.artifact_state("art-9") == "invalidated"
    assert withdrawn.unmentioned(("art-9", "art-10")) == ("art-10",)


def test_the_ledger_fingerprint_is_reproducible() -> None:
    assert ledger().fingerprint() == ledger().fingerprint()
    json.loads(json.dumps(ledger().to_mapping()))


def test_an_in_flight_task_keeps_the_version_it_started_on() -> None:
    registry = pp.PinRegistry()
    pinned, first = registry.pin("task-1", d("b"))
    assert first is None
    assert pinned.version_for("task-1", active_digest=d("c")) == d("b")


def test_a_new_task_picks_up_the_active_version() -> None:
    assert pp.PinRegistry().version_for("task-new", active_digest=d("c")) == d("c")


def test_a_pinned_task_cannot_be_migrated_implicitly() -> None:
    registry, _ = pp.PinRegistry().pin("task-1", d("b"))
    with pytest.raises(pp.RollbackRefused, match="mid-flight"):
        registry.pin("task-1", d("c"))


def test_re_pinning_the_same_version_is_idempotent() -> None:
    registry, _ = pp.PinRegistry().pin("task-1", d("b"))
    again, existing = registry.pin("task-1", d("b"))
    assert existing == d("b")
    assert again.to_mapping() == registry.to_mapping()


def test_in_flight_tasks_on_a_version_are_reported_for_a_rollback() -> None:
    registry, _ = pp.PinRegistry().pin("task-1", d("b"))
    registry, _ = registry.pin("task-2", d("c"))
    assert registry.in_flight_pinned_to(d("b")) == ("task-1",)


# ---------------------------------------------------------------------------
# Discipline: the module owns no clock, socket or writer.
# ---------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    tree = ast.parse(open("promotion_policy.py", encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "typing",
        "autonomous_production_decisions",
        "scoped_grants",
    }


def test_there_is_no_operation_that_replaces_a_ledger_record() -> None:
    tree = ast.parse(open("promotion_policy.py", encoding="utf-8").read())
    methods = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    forbidden = {"replace", "delete", "remove", "update", "overwrite"}
    assert not (forbidden & set(methods))
