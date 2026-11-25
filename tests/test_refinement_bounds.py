"""AP-027 — bounded refinement, incumbent preservation and honest stopping.

Every fixture is a value this file constructs. No model, provider, runtime or network is contacted:
the assertions are about what the contract refuses to call an improvement.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest

import refinement_bounds as rb


def _digest(char: str) -> str:
    return char * 64


def incumbent(**kw: Any) -> rb.Variant:
    params: dict[str, Any] = {
        "variant_id": "v0",
        "origin": "incumbent",
        "revision": 0,
        "content_digest": _digest("a"),
    }
    params.update(kw)
    return rb.Variant(**params)


def revision(variant_id: str = "v1", *, content: str = "b", authored_by: str = "gen-1", revision_no: int = 1) -> rb.Variant:
    return rb.Variant(
        variant_id=variant_id,
        origin="revision",
        revision=revision_no,
        content_digest=_digest(content),
        authored_by_session=authored_by,
    )


def limits(**kw: Any) -> rb.RefinementLimits:
    params: dict[str, Any] = {"max_rounds": 3, "max_reviewer_calls": 12, "max_tokens": 10_000}
    params.update(kw)
    return rb.RefinementLimits(**params)


def requirement(requirement_id: str = "r-fact", kind: str = "factual") -> rb.Requirement:
    return rb.Requirement(requirement_id=requirement_id, kind=kind, statement=f"the output keeps {kind} integrity")


def hard_pass(requirement_id: str = "r-fact") -> rb.RequirementResult:
    return rb.RequirementResult(requirement_id=requirement_id, passed=True, evidence="checked against source 3")


def hard_fail(requirement_id: str = "r-fact") -> rb.RequirementResult:
    return rb.RequirementResult(requirement_id=requirement_id, passed=False)


def context(session: str = "rev-a", family: str = "local-a", rubric: str = "c") -> rb.ReviewerContext:
    return rb.ReviewerContext(model_family=family, session_id=session, rubric_digest=_digest(rubric))


def verdict(
    variant_id: str,
    style: float,
    *,
    reviewer_id: str = "rev1",
    session: str = "rev-a",
    family: str = "local-a",
    results: tuple[rb.RequirementResult, ...] = (),
) -> rb.ReviewerVerdict:
    return rb.ReviewerVerdict(
        reviewer_id=reviewer_id,
        variant_id=variant_id,
        context=context(session=session, family=family),
        style_score=style,
        requirement_results=results,
    )


def round_one(
    variants: tuple[rb.Variant, ...] | None = None,
    verdicts: tuple[rb.ReviewerVerdict, ...] | None = None,
    requirements: tuple[rb.Requirement, ...] | None = None,
    *,
    index: int = 1,
    tokens: int = 100,
) -> rb.RefinementRound:
    return rb.RefinementRound(
        round_index=index,
        variants=variants if variants is not None else (incumbent(), revision()),
        verdicts=verdicts
        if verdicts is not None
        else (
            verdict("v0", 0.60, results=(hard_pass(),)),
            verdict("v1", 0.80, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_pass(),)),
        ),
        requirements=requirements if requirements is not None else (requirement(),),
        tokens_spent=tokens,
    )


def session(**kw: Any) -> rb.RefinementSession:
    params: dict[str, Any] = {
        "incumbent": incumbent(),
        "limits": limits(),
        "producer_model_family": "local-a",
    }
    params.update(kw)
    return rb.RefinementSession.start(**params)


# ---------------------------------------------------------------------------
# AC1 — the unchanged output is always an eligible candidate.
# ---------------------------------------------------------------------------


def test_the_incumbent_starts_as_the_accepted_version() -> None:
    started = session()
    assert started.accepted.variant_id == "v0"
    assert started.accepted.content_digest == _digest("a")
    assert started.rounds == ()


def test_a_session_cannot_start_from_a_revision() -> None:
    with pytest.raises(rb.ComparisonRefused, match="starts from an incumbent"):
        rb.RefinementSession.start(incumbent=revision(), limits=limits(), producer_model_family="local-a")


def test_a_ballot_without_the_incumbent_is_refused() -> None:
    ballot = round_one(
        variants=(revision("v1"), revision("v2", content="c")),
        verdicts=(
            verdict("v1", 0.9, results=(hard_pass(),)),
            verdict("v2", 0.7, reviewer_id="rev2", session="rev-b", results=(hard_pass(),)),
        ),
    )
    with pytest.raises(rb.ComparisonRefused, match="incumbent exactly once"):
        session().apply(ballot)


def test_a_ballot_with_two_incumbents_is_refused() -> None:
    second = rb.Variant(variant_id="v0b", origin="incumbent", revision=0, content_digest=_digest("a"))
    ballot = round_one(
        variants=(incumbent(), second),
        verdicts=(verdict("v0", 0.6, results=(hard_pass(),)), verdict("v0b", 0.6, reviewer_id="rev2", session="rev-b", results=(hard_pass(),))),
    )
    with pytest.raises(rb.ComparisonRefused, match="incumbent exactly once"):
        session().apply(ballot)


def test_a_revision_that_did_not_change_anything_is_not_a_revision() -> None:
    same = rb.Variant(
        variant_id="v9", origin="revision", revision=1, content_digest=_digest("a"), authored_by_session="gen-1"
    )
    ballot = round_one(
        variants=(incumbent(), same),
        verdicts=(
            verdict("v0", 0.6, results=(hard_pass(),)),
            verdict("v9", 0.95, reviewer_id="rev2", session="rev-b", results=(hard_pass(),)),
        ),
    )
    with pytest.raises(rb.SelectionRefused, match="has the incumbent's content"):
        session().apply(ballot)


def test_the_accepted_version_must_defend_itself_in_every_round() -> None:
    first = session().apply(round_one())
    assert first.accepted.variant_id == "v1"
    ballot = round_one(
        variants=(incumbent(), revision("v2", content="c")),
        verdicts=(
            verdict("v0", 0.6, results=(hard_pass(),)),
            verdict("v2", 0.99, reviewer_id="rev2", session="rev-b", results=(hard_pass(),)),
        ),
        index=2,
    )
    with pytest.raises(rb.SelectionRefused, match="must be on the ballot in every round"):
        first.apply(ballot)


def test_an_unjudged_variant_is_not_a_candidate() -> None:
    ballot = round_one(verdicts=(verdict("v0", 0.6, results=(hard_pass(),)),))
    with pytest.raises(rb.SelectionRefused, match="no verdict"):
        session().apply(ballot)


def test_every_ballot_entry_is_reported_with_its_own_verdict() -> None:
    outcome = session().apply(round_one())
    reported = {item.variant_id for item in outcome.rounds[0].outcomes}
    assert reported == {"v0", "v1"}
    assert all(item.style_score is not None for item in outcome.rounds[0].outcomes)


def test_a_round_where_nothing_is_eligible_is_not_evidence_for_a_change() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, results=(hard_fail(),)),
            verdict("v1", 0.9, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_fail(),)),
        )
    )
    after = session().apply(ballot)
    decision = after.stop_decision()
    assert decision.kind == "insufficient_evidence"
    assert decision.should_stop is False
    assert decision.change_justified is False
    assert after.accepted.variant_id == "v0"


# ---------------------------------------------------------------------------
# AC2 — independence is recorded, and self-grading is refused.
# ---------------------------------------------------------------------------


def test_a_reviewer_from_the_producing_session_is_refused() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, session="rev-a", results=(hard_pass(),)),
            verdict("v1", 0.9, session="gen-1", results=(hard_pass(),)),
        )
    )
    with pytest.raises(rb.ComparisonRefused, match="self-grading"):
        session().apply(ballot)


def test_one_session_judging_both_sides_is_one_opinion() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, session="rev-a", results=(hard_pass(),)),
            verdict("v1", 0.9, reviewer_id="rev2", session="rev-a", results=(hard_pass(),)),
        )
    )
    with pytest.raises(rb.ComparisonRefused, match="at least two sessions"):
        session().apply(ballot)


def test_a_shared_model_family_is_recorded_as_possible_bias() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, session="rev-a", family="local-a", results=(hard_pass(),)),
            verdict("v1", 0.9, reviewer_id="rev2", session="rev-b", family="local-a", results=(hard_pass(),)),
        )
    )
    outcome = session().apply(ballot)
    assert outcome.rounds[0].shared_model_bias == "possible"


def test_a_different_model_family_in_another_session_mitigates_but_never_clears_the_bias() -> None:
    outcome = session().apply(round_one())
    assert outcome.rounds[0].shared_model_bias == "mitigated_by_independent_context"
    assert "none" not in rb.BIAS_STATES


# ---------------------------------------------------------------------------
# AC3 — hard requirements are not tradeable against style.
# ---------------------------------------------------------------------------


def test_a_hard_failure_makes_a_high_style_score_ineligible() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, results=(hard_pass(),)),
            verdict("v1", 1.0, reviewer_id="rev2", session="rev-b", results=(hard_fail(),), family="local-b"),
        )
    )
    outcome = session().apply(ballot)
    scored = {item.variant_id: item for item in outcome.rounds[0].outcomes}
    assert scored["v0"].eligible is True
    assert scored["v1"].eligible is False
    assert "failed the factual requirement" in scored["v1"].blocking_reasons[0]
    assert outcome.rounds[0].accepted_after == "v0"
    assert outcome.rounds[0].improved is False


def test_a_missing_hard_result_is_not_a_pass() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.6, results=(hard_pass(),)),
            verdict("v1", 0.95, reviewer_id="rev2", session="rev-b", results=(), family="local-b"),
        )
    )
    outcome = session().apply(ballot)
    scored = {item.variant_id: item for item in outcome.rounds[0].outcomes}
    assert scored["v1"].eligible is False
    assert "did not report the factual requirement" in scored["v1"].blocking_reasons[0]


def test_a_style_requirement_is_never_hard() -> None:
    style = requirement("r-style", "style")
    assert style.is_hard is False
    assert requirement("r-safety", "safety").is_hard is True
    assert requirement("r-func", "functional").is_hard is True


def test_a_hard_pass_without_evidence_is_refused() -> None:
    with pytest.raises(rb.EvidenceRefused, match="no evidence"):
        rb.RequirementResult(requirement_id="r-fact", passed=True)


def test_a_failed_requirement_needs_no_evidence_to_be_recorded() -> None:
    assert hard_fail().passed is False


def test_an_unknown_requirement_kind_is_refused() -> None:
    with pytest.raises(rb.RefinementError, match="unknown requirement kind"):
        rb.Requirement(requirement_id="r-x", kind="vibes", statement="it should feel good")


def test_style_never_rescues_a_safety_failure() -> None:
    safety = requirement("r-safety", "safety")
    ballot = round_one(
        requirements=(safety,),
        verdicts=(
            verdict("v0", 0.1, results=(hard_pass("r-safety"),)),
            verdict("v1", 1.0, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_fail("r-safety"),)),
        ),
    )
    outcome = session().apply(ballot)
    assert outcome.rounds[0].accepted_after == "v0"
    assert outcome.rounds[0].improved is False


# ---------------------------------------------------------------------------
# AC4 — the budget is bounded across reviewers and variants.
# ---------------------------------------------------------------------------


def test_the_reviewer_budget_counts_every_reviewer_of_every_variant() -> None:
    many = round_one(
        verdicts=tuple(
            verdict("v0", 0.6, reviewer_id=f"rev{i}", session=f"rev-{i}", results=(hard_pass(),)) for i in range(2)
        )
        + tuple(
            verdict("v1", 0.7, reviewer_id=f"rev{i}", session=f"rev-{i}", results=(hard_pass(),)) for i in range(2, 4)
        ),
        requirements=(requirement(),),
    )
    with pytest.raises(rb.BudgetSpent, match="reviewer calls"):
        session(limits=limits(max_reviewer_calls=3)).apply(many)


def test_a_budget_refusal_records_nothing() -> None:
    started = session(limits=limits(max_reviewer_calls=1))
    with pytest.raises(rb.BudgetSpent):
        started.apply(round_one())
    assert started.rounds == ()
    assert started.reviewer_calls == 0


def test_the_round_limit_is_a_ceiling() -> None:
    started = session(limits=limits(max_rounds=1, no_progress_rounds=1))
    after = started.apply(round_one())
    with pytest.raises(rb.BudgetSpent, match="round limit"):
        after.apply(round_one(index=2))


def test_the_token_budget_covers_the_whole_session() -> None:
    started = session(limits=limits(max_tokens=150))
    after = started.apply(round_one(tokens=100))
    with pytest.raises(rb.BudgetSpent, match="tokens"):
        after.apply(round_one(index=2, tokens=100))


def test_a_plateau_limit_beyond_the_round_limit_is_refused() -> None:
    with pytest.raises(rb.RefinementError, match="never trigger"):
        limits(max_rounds=2, no_progress_rounds=3)


def test_rounds_cannot_arrive_out_of_order() -> None:
    with pytest.raises(rb.SelectionRefused, match="was expected"):
        session().apply(round_one(index=2))


# ---------------------------------------------------------------------------
# AC5 — stop when the evidence does not justify change; keep the best version.
# ---------------------------------------------------------------------------


def test_a_justified_improvement_is_accepted() -> None:
    after = session().apply(round_one())
    assert after.accepted.variant_id == "v1"
    assert after.rounds[0].improved is True
    decision = after.stop_decision()
    assert decision.kind == "goal_met"
    assert decision.change_justified is True
    assert decision.preserved_variant_id == "v1"


def test_a_margin_too_small_to_matter_keeps_the_incumbent() -> None:
    ballot = round_one(
        verdicts=(
            verdict("v0", 0.60, results=(hard_pass(),)),
            verdict("v1", 0.61, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_pass(),)),
        )
    )
    after = session().apply(ballot)
    assert after.accepted.variant_id == "v0"
    assert after.rounds[0].improved is False
    assert "below the 0.02 margin" in after.rounds[0].reason


def test_two_rounds_without_progress_stop_the_session() -> None:
    started = session(limits=limits(max_rounds=4, no_progress_rounds=2, max_reviewer_calls=20))
    flat = round_one(
        verdicts=(
            verdict("v0", 0.60, results=(hard_pass(),)),
            verdict("v1", 0.605, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_pass(),)),
        )
    )
    once = started.apply(flat)
    twice = once.apply(
        round_one(
            variants=(incumbent(), revision("v1", content="b"), revision("v2", content="c")),
            verdicts=(
                verdict("v0", 0.60, results=(hard_pass(),)),
                verdict("v1", 0.605, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_pass(),)),
                verdict("v2", 0.61, reviewer_id="rev3", session="rev-c", family="local-c", results=(hard_pass(),)),
            ),
            index=2,
        )
    )
    decision = twice.stop_decision()
    assert decision.kind == "no_progress"
    assert decision.should_stop is True
    assert decision.change_justified is False
    assert decision.preserved_variant_id == "v0"


def test_a_round_that_loses_does_not_replace_the_accepted_version() -> None:
    first = session().apply(round_one())
    assert first.accepted.variant_id == "v1"
    regressing = round_one(
        variants=(incumbent(), revision("v1", content="b"), revision("v2", content="c")),
        verdicts=(
            verdict("v0", 0.30, results=(hard_pass(),)),
            verdict("v1", 0.90, reviewer_id="rev2", session="rev-b", family="local-b", results=(hard_pass(),)),
            verdict("v2", 0.40, reviewer_id="rev3", session="rev-c", family="local-c", results=(hard_pass(),)),
        ),
        index=2,
    )
    second = first.apply(regressing)
    assert second.accepted.variant_id == "v1"
    assert second.rounds[1].improved is False


def test_a_selection_cannot_be_justified_by_a_non_goal_stop() -> None:
    with pytest.raises(rb.SelectionRefused, match="only by a met goal"):
        rb.StopDecision(
            kind="no_progress",
            reason="flat",
            should_stop=True,
            change_justified=True,
            preserved_variant_id="v0",
            preserved_content_digest=_digest("a"),
        )


def test_running_out_of_rounds_preserves_the_accepted_version() -> None:
    started = session(limits=limits(max_rounds=1, no_progress_rounds=1))
    after = started.apply(round_one())
    decision = after.stop_decision()
    assert decision.kind == "rounds_exhausted"
    assert decision.preserved_variant_id == after.accepted.variant_id


def test_no_round_yet_is_not_a_stop_and_not_a_claim() -> None:
    decision = session().stop_decision()
    assert decision.kind == "insufficient_evidence"
    assert decision.should_stop is False
    assert decision.change_justified is False


# ---------------------------------------------------------------------------
# AC6 — honest partial results and precise escalation.
# ---------------------------------------------------------------------------


def test_a_capability_gap_makes_the_result_partial() -> None:
    gap = rb.Escalation(
        capability="render the deck to PDF",
        why_needed="the brief requires a PDF deliverable and no local renderer is installed",
        evidence="renderer probe returned 'not found' for the three local candidates",
        escalate_to="owner:project-owner",
    )
    after = session().apply(round_one()).escalate(gap)
    report = after.report(wall_seconds=42.0)
    assert report.status == "partial"
    assert after.stop_decision().kind == "capability_gap"


def test_a_partial_result_cannot_be_reported_as_delivered() -> None:
    gap = rb.Escalation(
        capability="read the legacy spreadsheet format",
        why_needed="the task's source data is in a format no local parser handles",
        evidence="all three parsers refused the file header",
        escalate_to="user:priya",
    )
    report = session().escalate(gap).report(wall_seconds=1.0)
    with pytest.raises(rb.FabricatedSuccessRefused, match="cannot be reported as delivered"):
        report.claim_success()


def test_an_escalation_must_name_a_human_decision_owner() -> None:
    with pytest.raises(rb.ComparisonRefused, match="human decision owner"):
        rb.Escalation(
            capability="something",
            why_needed="because",
            evidence="it failed",
            escalate_to="the team",
        )


def test_an_escalation_needs_evidence_of_the_gap() -> None:
    with pytest.raises(rb.RefinementError, match="must not be empty"):
        rb.Escalation(
            capability="render PDF",
            why_needed="needed",
            evidence="   ",
            escalate_to="owner:project-owner",
        )


def test_a_delivered_report_states_its_success_plainly() -> None:
    report = session().apply(round_one()).report(wall_seconds=12.5)
    assert report.status == "delivered"
    assert report.recommendation == "accept_revision"
    assert "accepted v1" in report.claim_success()


def test_a_delivered_report_may_still_keep_the_incumbent() -> None:
    report = session().report(wall_seconds=3.0)
    assert report.status == "delivered"
    assert report.recommendation == "keep_incumbent"
    assert "kept the incumbent" in report.claim_success()


def test_the_report_never_claims_a_hard_requirement_was_traded() -> None:
    mapping = session().apply(round_one()).report(wall_seconds=9.0).to_mapping()
    assert mapping["hard_requirements_traded_for_style"] is False
    assert mapping["weights_changed"] is False
    assert mapping["budget_source"].startswith("refinement_bounds/v1")


def test_the_report_is_reproducible() -> None:
    first = session().apply(round_one()).report(wall_seconds=9.0)
    second = session().apply(round_one()).report(wall_seconds=9.0)
    assert first.fingerprint() == second.fingerprint()
    json.loads(json.dumps(first.to_mapping()))


def test_the_report_records_the_spend_it_used() -> None:
    mapping = session().apply(round_one(tokens=250)).report(wall_seconds=1.0).to_mapping()
    assert mapping["reviewer_calls"] == 2
    assert mapping["tokens_spent"] == 250
    assert mapping["limits"]["max_tokens"] == 10_000


def test_a_wall_clock_measurement_has_to_come_from_the_caller() -> None:
    with pytest.raises(rb.RefinementError, match="wall_seconds"):
        session().report(wall_seconds=-1)


# ---------------------------------------------------------------------------
# Discipline: the module owns no clock, socket or model call.
# ---------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    source = open("refinement_bounds.py", encoding="utf-8").read()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}


def test_the_module_has_no_top_level_state_that_outlives_a_session() -> None:
    tree = ast.parse(open("refinement_bounds.py", encoding="utf-8").read())
    mutable_constants = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id != "__all__":
                mutable_constants.append(target.id)
    assert mutable_constants == []
