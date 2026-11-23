"""AP-016 offline tests: reproducible challenger and workflow evaluations.

Nothing here runs a model, workflow or provider. Runs and scores are values the
caller supplies; the module decides what they may be used to claim — and refuses
to claim much.
"""

from __future__ import annotations

from typing import Any

import pytest

import challenger_evaluation as E
from challenger_evaluation import (
    ChallengerEvaluationError,
    EvaluationDesign,
    Evidence,
    NoOpIncumbent,
    PromotionCandidate,
    PromotionRefused,
    RunRecord,
    ScoredOutcome,
    ScorerFailure,
    ScorerFailed,
    SourcePin,
    StrategyBudget,
    StrategyResult,
    StrategyRuns,
    Variability,
    build_report,
    compare_budgets,
    measure_metrics,
    refuse_promotion,
    render_report,
    require_trustworthy,
    run_scorer,
    verify_source_pins,
)


def d(ch: str = "a") -> str:
    return "sha256:" + ch * 64


def record(
    run_id: str = "r1",
    strategy: str = "single_agent",
    *,
    task: str = "t1",
    partition: str = "held_out",
    outcome: str = "completed",
    reason: str | None = None,
    latency: float = 100.0,
    cost: float = 0.1,
    tokens: int = 500,
    interventions: int = 0,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        strategy=strategy,  # type: ignore[arg-type]
        task_id=task,
        partition=partition,  # type: ignore[arg-type]
        task_digest=d("a"),
        config_digest=d("b"),
        tool_digest=d("c"),
        model_digest=d("d"),
        environment_digest=d("e"),
        toolchain_digest=d("f"),
        outcome=outcome,  # type: ignore[arg-type]
        failure_reason=reason,
        latency_ms=latency,
        cost=cost,
        tokens=tokens,
        interventions=interventions,
    )


def evidence(*, independent: bool = True) -> tuple[Evidence, ...]:
    items = [Evidence(kind="deterministic_check", source="pytest", passes=True)]
    if independent:
        items.append(Evidence(kind="user_judgement", source="owner review"))
    return tuple(items)


def outcome(
    run_id: str = "r1",
    strategy: str = "specialists",
    *,
    score: float = 0.8,
    partition: str = "held_out",
) -> ScoredOutcome:
    return ScoredOutcome(
        run_id=run_id,
        strategy=strategy,  # type: ignore[arg-type]
        task_id="t1",
        partition=partition,  # type: ignore[arg-type]
        score=score,
        evidence=evidence(),
        scorer_id="rubric-v1",
    )


def budget(
    strategy: str = "specialists", *, tokens: int = 10_000, cost: float = 1.0
) -> StrategyBudget:
    return StrategyBudget(
        strategy=strategy,  # type: ignore[arg-type]
        max_tokens=tokens,
        max_cost=cost,
        max_latency_ms=1000.0,
        max_interventions=5,
    )


def design(**overrides: Any) -> EvaluationDesign:
    base: dict[str, Any] = {
        "design_id": "design-1",
        "task_partitions": {"t1": "held_out", "t2": "development"},
        "incumbents": (NoOpIncumbent(task_id="t1", partition="held_out"),),
        "budgets": (budget("single_agent"), budget("specialists")),
        "source_revision": d("f"),
    }
    base.update(overrides)
    return EvaluationDesign(**base)


def results(**overrides: Any) -> tuple[StrategyResult, ...]:
    base = {
        "specialists": StrategyResult(
            strategy="specialists", outcomes=(outcome("s1"),)
        ),
        "single_agent": StrategyResult(
            strategy="single_agent", outcomes=(outcome("a1", "single_agent"),)
        ),
        "no_op_incumbent": StrategyResult(strategy="no_op_incumbent", outcomes=()),
    }
    base.update(overrides)
    return tuple(base.values())


# ---------------------------------------------------------------------------
# AC1 — run records pin everything and keep every failure
# ---------------------------------------------------------------------------


def test_a_run_pins_task_config_tool_model_and_environment():
    payload = record().to_mapping()
    for key in (
        "task_digest",
        "config_digest",
        "tool_digest",
        "model_digest",
        "environment_digest",
        "toolchain_digest",
    ):
        assert payload[key].startswith("sha256:"), key


def test_a_malformed_digest_is_refused():
    with pytest.raises(ChallengerEvaluationError, match="sha256"):
        RunRecord(
            run_id="r",
            strategy="single_agent",
            task_id="t",
            partition="held_out",
            task_digest="not-a-digest",
            config_digest=d("b"),
            tool_digest=d("c"),
            model_digest=d("d"),
            environment_digest=d("e"),
            toolchain_digest=d("f"),
            outcome="completed",
        )


def test_a_failure_must_state_why():
    with pytest.raises(ChallengerEvaluationError, match="must state why"):
        record(outcome="failed")


def test_a_completed_run_has_no_failure_reason():
    with pytest.raises(ChallengerEvaluationError, match="no failure reason"):
        record(outcome="completed", reason="none")


def test_failures_are_records_not_absences():
    """Dropping a failed run is detectable: the attempted count does not match."""
    kept_only_successes = (record("r1", "specialists"),)
    with pytest.raises(
        ChallengerEvaluationError, match="attempted 3 runs but recorded 1"
    ):
        StrategyRuns(strategy="specialists", attempted=3, records=kept_only_successes)


def test_a_strategy_reports_its_failure_rate_over_attempts():
    runs = StrategyRuns(
        strategy="specialists",
        attempted=2,
        records=(
            record("r1", "specialists"),
            record("r2", "specialists", outcome="failed", reason="boom"),
        ),
    )
    assert runs.failure_rate == 0.5
    assert len(runs.failures) == 1
    assert runs.to_mapping()["recorded"] == 2


def test_duplicate_run_ids_within_a_strategy_are_refused():
    with pytest.raises(ChallengerEvaluationError, match="duplicate run ids"):
        StrategyRuns(
            strategy="specialists",
            attempted=2,
            records=(record("x", "specialists"), record("x", "specialists")),
        )


def test_a_record_from_another_strategy_is_refused():
    with pytest.raises(ChallengerEvaluationError, match="belong to this strategy"):
        StrategyRuns(
            strategy="specialists", attempted=1, records=(record("r1", "single_agent"),)
        )


# ---------------------------------------------------------------------------
# AC2 — comparable budgets, no hidden differences
# ---------------------------------------------------------------------------


def test_equal_budgets_are_comparable():
    comparison = compare_budgets(budget("single_agent"), budget("specialists"))
    assert comparison.comparable is True
    assert comparison.differences["max_tokens"] == 0.0


def test_a_large_budget_difference_is_refused():
    comparison = compare_budgets(
        budget("single_agent", tokens=1000), budget("specialists", tokens=10_000)
    )
    assert comparison.comparable is False
    assert "max_tokens" in comparison.reason
    assert comparison.to_mapping()["hidden_budget_difference_permitted"] is False


def test_a_difference_within_tolerance_is_allowed():
    comparison = compare_budgets(
        budget("single_agent", tokens=10_000),
        budget("specialists", tokens=10_500),
        tolerance=0.10,
    )
    assert comparison.comparable is True


def test_every_budget_dimension_is_compared():
    tight = StrategyBudget(
        "single_agent",
        max_tokens=1000,
        max_cost=1.0,
        max_latency_ms=100.0,
        max_interventions=1,
    )
    loose = StrategyBudget(
        "specialists",
        max_tokens=1000,
        max_cost=1.0,
        max_latency_ms=10_000.0,
        max_interventions=9,
    )
    comparison = compare_budgets(tight, loose)
    assert set(comparison.differences) == {
        "max_tokens",
        "max_cost",
        "max_latency_ms",
        "max_interventions",
    }
    assert comparison.comparable is False


def test_a_zero_budget_is_refused_outright():
    with pytest.raises(ChallengerEvaluationError):
        StrategyBudget(
            "specialists",
            max_tokens=0,
            max_cost=1.0,
            max_latency_ms=1.0,
            max_interventions=1,
        )


# ---------------------------------------------------------------------------
# AC3 — deterministic checks and independent evidence accompany scores
# ---------------------------------------------------------------------------


def test_a_score_with_no_evidence_is_refused():
    with pytest.raises(ChallengerEvaluationError, match="assertion"):
        ScoredOutcome(
            run_id="r",
            strategy="specialists",
            task_id="t",
            partition="held_out",
            score=0.9,
            evidence=(),
            scorer_id="self",
        )


def test_a_score_with_only_a_deterministic_check_is_not_justified():
    only_check = ScoredOutcome(
        run_id="r",
        strategy="specialists",
        task_id="t",
        partition="held_out",
        score=0.9,
        evidence=(Evidence(kind="deterministic_check", source="pytest", passes=True),),
        scorer_id="rubric",
    )
    assert only_check.is_justified is False
    assert "no independent" in (only_check.problem() or "")


def test_a_score_with_only_model_opinion_is_not_justified():
    model_opinion = ScoredOutcome(
        run_id="r",
        strategy="specialists",
        task_id="t",
        partition="held_out",
        score=0.9,
        evidence=(Evidence(kind="user_judgement", source="someone"),),
        scorer_id="self",
    )
    assert model_opinion.is_justified is False
    assert "no deterministic check" in (model_opinion.problem() or "")


def test_a_fully_evidenced_score_is_justified():
    assert outcome().is_justified is True
    assert outcome().problem() is None


def test_a_failing_deterministic_check_invalidates_the_score():
    failing = ScoredOutcome(
        run_id="r",
        strategy="specialists",
        task_id="t",
        partition="held_out",
        score=1.0,
        evidence=(
            Evidence(kind="deterministic_check", source="pytest", passes=False),
            Evidence(kind="user_judgement", source="owner"),
        ),
        scorer_id="rubric",
    )
    assert failing.is_justified is False
    assert "check failed" in (failing.problem() or "")


def test_a_deterministic_check_must_report_a_result():
    with pytest.raises(ChallengerEvaluationError, match="whether it passed"):
        Evidence(kind="deterministic_check", source="pytest")


def test_independent_evidence_kinds_are_named():
    assert Evidence(kind="user_judgement", source="owner").is_independent is True
    assert (
        Evidence(kind="factual_verification", source="source doc").is_independent
        is True
    )
    assert (
        Evidence(
            kind="deterministic_check", source="pytest", passes=True
        ).is_independent
        is False
    )


# ---------------------------------------------------------------------------
# AC5 — scorer failure, missing evidence and stale hashes fail closed
# ---------------------------------------------------------------------------


def test_a_raising_scorer_becomes_a_recorded_failure():
    def broken(_: RunRecord) -> float:
        raise RuntimeError("rubric exploded")

    scored, failure = run_scorer(broken, record(), evidence=evidence())
    assert scored is None
    assert failure is not None
    assert failure.state == "failed"
    assert "rubric exploded" in failure.reason


def test_a_scorer_failure_never_contributes_a_score():
    _, failure = run_scorer(lambda _: 0.0, record(), evidence=())
    payload = failure.to_mapping()  # type: ignore[union-attr]
    assert payload["contributes_a_score"] is False
    assert payload["counted_as_zero"] is False


def test_a_score_without_independent_evidence_is_a_missing_evidence_failure():
    scored, failure = run_scorer(
        lambda _: 0.9, record(), evidence=evidence(independent=False)
    )
    assert scored is None
    assert failure is not None
    assert failure.state == "missing_evidence"


def test_an_out_of_range_score_fails_closed():
    _, failure = run_scorer(lambda _: 1.5, record(), evidence=evidence())
    assert failure is not None
    assert failure.state == "missing_evidence"


def test_a_justified_score_is_returned_without_failure():
    scored, failure = run_scorer(lambda _: 0.75, record(), evidence=evidence())
    assert failure is None
    assert scored is not None
    assert scored.score == 0.75


def test_stale_source_hashes_fail_closed():
    pins = (SourcePin(path="module.py", digest=d("a")),)
    with pytest.raises(ScorerFailed, match="not reproducible"):
        verify_source_pins(pins, observed={"module.py": d("b")})


def test_a_missing_source_file_fails_closed():
    pins = (SourcePin(path="module.py", digest=d("a")),)
    with pytest.raises(ScorerFailed, match="missing"):
        verify_source_pins(pins, observed={})


def test_matching_source_hashes_pass():
    pins = (SourcePin(path="module.py", digest=d("a")),)
    verify_source_pins(pins, observed={"module.py": d("a")})


def test_pins_without_observed_hashes_are_refused_by_the_builder():
    with pytest.raises(
        ChallengerEvaluationError, match="cannot be verified by assumption"
    ):
        build_report(
            design=design(),
            results=results(),
            source_pins=(SourcePin(path="m.py", digest=d("a")),),
        )


# ---------------------------------------------------------------------------
# AC4 — the no-op incumbent and held-out tasks always participate
# ---------------------------------------------------------------------------


def test_a_design_without_held_out_tasks_is_refused():
    with pytest.raises(ChallengerEvaluationError, match="no held-out tasks"):
        design(task_partitions={"t1": "development", "t2": "validation"})


def test_a_design_without_a_no_op_incumbent_is_refused():
    with pytest.raises(
        ChallengerEvaluationError, match="no-op incumbent must participate"
    ):
        design(incumbents=())


def test_an_incumbent_for_a_task_outside_the_design_is_refused():
    with pytest.raises(
        ChallengerEvaluationError, match="task the design does not contain"
    ):
        design(incumbents=(NoOpIncumbent(task_id="nope", partition="held_out"),))


def test_a_design_must_disclose_budgets():
    with pytest.raises(ChallengerEvaluationError, match="budgets must be disclosed"):
        design(budgets=())


def test_the_design_reports_its_held_out_tasks():
    assert design().held_out_task_ids == ("t1",)
    assert design().to_mapping()["self_grading_permitted"] is False


def test_a_comparison_needs_the_incumbent_as_a_result_not_only_in_the_design():
    partial = tuple(r for r in results() if r.strategy != "no_op_incumbent")
    with pytest.raises(ChallengerEvaluationError, match="not only in the design"):
        build_report(design=design(), results=partial)


def test_a_comparison_needs_at_least_one_challenger():
    only_control = (results()[-1],)
    with pytest.raises(ChallengerEvaluationError, match="at least one challenger"):
        build_report(design=design(), results=only_control)


def test_the_incumbent_is_the_zero_baseline_a_challenger_must_beat():
    incumbent = NoOpIncumbent(task_id="t1", partition="held_out")
    assert incumbent.score == 0.0
    assert "does nothing" in incumbent.note


# ---------------------------------------------------------------------------
# AC6 — variability, metrics and no promotion on a fake score
# ---------------------------------------------------------------------------


def test_variability_reports_median_and_spread_not_a_mean():
    var = Variability.of([0.2, 0.8, 0.5])
    payload = var.to_mapping()
    assert payload["median"] == 0.5
    assert payload["spread"] == pytest.approx(0.6)
    assert payload["mean_only"] is False


def test_variability_needs_observations():
    with pytest.raises(ChallengerEvaluationError):
        Variability.of([])


def test_cost_and_latency_and_interventions_are_reported_alongside_quality():
    metrics = measure_metrics(
        latencies_ms=[100, 200], costs=[0.1, 0.2], interventions=[0, 3]
    )
    assert set(metrics) == {"latency_ms", "cost", "interventions"}
    assert metrics["interventions"].median == 1.5


def test_metrics_without_observations_are_refused():
    with pytest.raises(ChallengerEvaluationError):
        measure_metrics(latencies_ms=[], costs=[0.1], interventions=[0])


def test_a_report_with_comparable_budgets_and_justified_scores_is_trustworthy():
    report = build_report(design=design(), results=results())
    assert report.is_trustworthy is True
    assert report.blockers == ()


def test_unequal_budgets_make_the_report_untrustworthy():
    report = build_report(
        design=design(
            budgets=(
                budget("single_agent", tokens=1000),
                budget("specialists", tokens=50_000),
            )
        ),
        results=results(),
    )
    assert report.is_trustworthy is False
    assert "not a comparison" in report.blockers[0]


def test_any_scorer_failure_makes_the_report_untrustworthy():
    failed = StrategyResult(
        strategy="specialists",
        outcomes=(),
        scorer_failures=(
            ScorerFailure(
                run_id="s1", strategy="specialists", state="failed", reason="boom"
            ),
        ),
    )
    report = build_report(
        design=design(), results=(*results()[:1], failed, results()[2])
    )
    assert report.is_trustworthy is False
    assert "boom" in report.blockers[0]


def test_require_trustworthy_raises_rather_than_warning():
    report = build_report(
        design=design(
            budgets=(
                budget("single_agent", tokens=1000),
                budget("specialists", tokens=50_000),
            )
        ),
        results=results(),
    )
    with pytest.raises(ScorerFailed, match="not publishable"):
        require_trustworthy(report)


def test_promotion_without_verification_is_refused():
    candidate = PromotionCandidate(
        strategy="specialists", task_id="t1", claimed_score=0.99, verified=False
    )
    with pytest.raises(PromotionRefused, match="never independently verified"):
        refuse_promotion(candidate)


def test_promotion_from_an_untrustworthy_report_is_refused():
    report = build_report(
        design=design(
            budgets=(
                budget("single_agent", tokens=1000),
                budget("specialists", tokens=50_000),
            )
        ),
        results=results(),
    )
    verified = PromotionCandidate(
        strategy="specialists",
        task_id="t1",
        claimed_score=0.9,
        verified=True,
        verified_by="owner",
    )
    with pytest.raises(PromotionRefused, match="not trustworthy"):
        refuse_promotion(verified, report=report)


def test_a_verified_candidate_from_a_trustworthy_report_may_be_promoted():
    report = build_report(design=design(), results=results())
    verified = PromotionCandidate(
        strategy="specialists",
        task_id="t1",
        claimed_score=0.9,
        verified=True,
        verified_by="owner",
    )
    refuse_promotion(verified, report=report)  # does not raise


def test_verification_must_name_who_verified():
    with pytest.raises(ChallengerEvaluationError, match="name who verified"):
        PromotionCandidate(
            strategy="specialists", task_id="t1", claimed_score=0.9, verified=True
        )


# ---------------------------------------------------------------------------
# rendering and integrity
# ---------------------------------------------------------------------------


def test_the_rendered_report_is_deterministic_json():
    report = build_report(design=design(), results=results())
    first = render_report(report)
    assert first == render_report(report)
    assert first.endswith("\n")


def test_the_rendered_report_states_every_boundary():
    payload = __import__("json").loads(
        render_report(build_report(design=design(), results=results()))
    )
    rules = payload["evaluation_rules"]
    assert rules["self_grading_permitted"] is False
    assert rules["model_score_without_independent_evidence"] is False
    assert rules["scorer_failure_contributes_zero"] is False
    assert rules["hidden_budget_difference_permitted"] is False
    assert rules["failed_runs_may_be_omitted"] is False
    assert rules["promotion_without_verification_permitted"] is False
    assert len(payload["does_not_prove"]) == 3


def test_report_digest_is_stable():
    assert (
        build_report(design=design(), results=results()).digest
        == build_report(design=design(), results=results()).digest
    )


def test_a_partial_budget_disclosure_is_refused():
    with pytest.raises(ChallengerEvaluationError, match="both challenger strategies"):
        build_report(
            design=design(budgets=(budget("single_agent"),)),
            results=results(),
        )


def test_no_module_verb_runs_a_workflow_or_contacts_a_provider():
    forbidden = {
        "run_workflow",
        "execute",
        "call_model",
        "download",
        "activate",
        "dispatch",
    }
    assert not (forbidden & {n for n in dir(E) if not n.startswith("_")})
