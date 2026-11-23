"""AP-017 — bounded specialists and versioned handoffs, offline.

Every fixture here is a value the test constructs. No model is loaded, no runtime
starts, no provider or network is contacted, and no production path is exercised;
the assertions are about the contract's refusals, not about a running team.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

import bounded_specialists as bs
import child_delegation
import execution_budget
from bounded_specialists import (
    JoinLedger,
    DispatchEntry,
    HandoffArtifact,
    ParentBounds,
    ParallelismPlan,
    SpecialistRejectedError,
    SpecialistTask,
    SpecialistTeam,
    TopologyDecision,
    WriteClaim,
    assemble_team,
    assert_no_endorsement_request,
    assert_scopes_disjoint,
    bind_artifact_identity,
    build_reviewer_brief,
    decide_topology,
    render_team,
)
from challenger_evaluation import (
    EvaluationDesign,
    Evidence,
    NoOpIncumbent,
    ScoredOutcome,
    StrategyBudget,
    StrategyResult,
    measure_metrics,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
SOURCE = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def parent_bounds(**overrides: Any) -> ParentBounds:
    values: dict[str, Any] = {
        "parent_policy": "standard",
        "parent_tools": ("read_file", "write_file", "edit_file"),
        "parent_write_scopes": ("src/", "docs/"),
        "remaining_steps": 40,
        "remaining_tool_calls": 200,
        "remaining_tokens": 100_000,
        "remaining_elapsed_s": 900.0,
    }
    values.update(overrides)
    return ParentBounds(**values)


def worker_task(**overrides: Any) -> SpecialistTask:
    values: dict[str, Any] = {
        "task_id": "t-impl",
        "role": "worker",
        "objective": "implement the bounded change",
        "inputs": (HandoffArtifact("brief", DIGEST_A),),
        "expected_output": "a patch plus test evidence",
        "policy": "standard",
        "allowed_tools": ("read_file", "write_file"),
        "write_scopes": ("src/impl/",),
        "budget": execution_budget.TaskBudgetSpec(
            max_steps=10, max_tool_calls=50, max_tokens=20_000, max_elapsed_s=300.0
        ),
        "parent": parent_bounds(),
    }
    values.update(overrides)
    return SpecialistTask(**values)


def reviewer_task(**overrides: Any) -> SpecialistTask:
    values: dict[str, Any] = {
        "task_id": "t-review",
        "role": "reviewer",
        "objective": "review the patch against the brief",
        "inputs": (HandoffArtifact("brief", DIGEST_A),),
        "expected_output": "findings and open questions",
        "policy": "plan",
        "allowed_tools": ("read_file",),
        "write_scopes": (),
        "budget": execution_budget.TaskBudgetSpec(
            max_steps=5, max_tool_calls=10, max_tokens=5_000, max_elapsed_s=120.0
        ),
        "parent": parent_bounds(),
    }
    values.update(overrides)
    return SpecialistTask(**values)


def team(**overrides: Any) -> SpecialistTeam:
    values: dict[str, Any] = {
        "team_id": "team-1",
        "parent_task_id": "parent-1",
        "tasks": (worker_task(), reviewer_task()),
        "claims": (WriteClaim("claim-1", "t-impl", ("src/impl/",), "src/"),),
        "plan": ParallelismPlan(logical_parallelism=2),
    }
    values.update(overrides)
    return assemble_team(**values)


def _design(*, specialists_tokens: int = 1_000) -> EvaluationDesign:
    return EvaluationDesign(
        design_id="design-1",
        task_partitions={"task-1": "held_out"},
        incumbents=(NoOpIncumbent("task-1", "held_out"),),
        budgets=(
            StrategyBudget("single_agent", 1_000, 1.0, 1_000.0, 1),
            StrategyBudget("specialists", specialists_tokens, 1.0, 1_000.0, 1),
        ),
        source_revision=SOURCE,
        budget_tolerance=0.10,
    )


def _evidence() -> tuple[Evidence, ...]:
    return (
        Evidence("deterministic_check", "pytest tests/test_x.py", passes=True),
        Evidence("factual_verification", "ci-run-1"),
    )


def _result(
    strategy: str, score: float, *, run_id: str | None = None
) -> StrategyResult:
    outcome = ScoredOutcome(
        run_id=run_id or f"run-{strategy}",
        strategy=cast(Any, strategy),
        task_id="task-1",
        partition="held_out",
        score=score,
        evidence=_evidence(),
        scorer_id="scorer-1",
    )
    return StrategyResult(cast(Any, strategy), (outcome,))


def _results(*, specialist_score: float = 0.9) -> tuple[StrategyResult, ...]:
    return (
        _result("no_op_incumbent", 0.0),
        _result("single_agent", 0.5),
        _result("specialists", specialist_score),
    )


def _metrics(
    *, cost: float = 0.2, latency: float = 900.0, interventions: int = 1
) -> dict[str, Any]:
    return {
        "single_agent": measure_metrics(
            latencies_ms=[900.0], costs=[0.2], interventions=[1]
        ),
        "specialists": measure_metrics(
            latencies_ms=[latency], costs=[cost], interventions=[interventions]
        ),
    }


# ---------------------------------------------------------------------------
# AC1 — authority and remaining budget are strict parent subsets
# ---------------------------------------------------------------------------


def test_a_bounded_task_accepts_a_strict_subset() -> None:
    task = worker_task()
    assert task.budget.max_steps < task.parent.remaining_steps
    assert task.budget.max_tokens < task.parent.remaining_tokens
    assert set(task.allowed_tools) < set(task.parent.parent_tools)


def test_a_child_policy_above_the_parent_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="exceeds parent policy"):
        worker_task(policy="autonomous")


def test_a_child_may_narrow_the_parent_policy() -> None:
    assert worker_task(policy="plan").policy == "plan"


def test_an_unknown_policy_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="Unknown policy"):
        worker_task(policy=cast(Any, "root"))


def test_a_tool_the_parent_lacks_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="tools the parent does not hold"):
        worker_task(allowed_tools=("read_file", "run_bash"))


def test_a_write_scope_outside_the_parent_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="not within any parent scope"):
        worker_task(write_scopes=("secrets/",))


def test_a_sibling_prefix_is_not_a_parent_scope() -> None:
    # "srcx/" starts with "src" but is not inside "src/"; prefix-matching by
    # string alone would have accepted it.
    with pytest.raises(SpecialistRejectedError, match="not within any parent scope"):
        worker_task(write_scopes=("srcx/",))


def test_equal_budget_is_not_a_strict_subset() -> None:
    with pytest.raises(SpecialistRejectedError, match="strict subset"):
        worker_task(
            budget=execution_budget.TaskBudgetSpec(
                max_steps=40, max_tool_calls=50, max_tokens=20_000, max_elapsed_s=300.0
            )
        )


def test_an_over_budget_step_count_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="strict subset"):
        worker_task(budget=execution_budget.TaskBudgetSpec(max_steps=100))


def test_elapsed_budget_must_be_a_strict_subset() -> None:
    with pytest.raises(SpecialistRejectedError, match="strict subset"):
        worker_task(
            budget=execution_budget.TaskBudgetSpec(
                max_steps=10, max_tool_calls=50, max_tokens=20_000, max_elapsed_s=900.0
            )
        )


def test_parent_bounds_reject_negative_remaining_budget() -> None:
    with pytest.raises(SpecialistRejectedError, match="non-negative"):
        parent_bounds(remaining_tokens=-1)


def test_parent_bounds_reject_negative_remaining_time() -> None:
    with pytest.raises(SpecialistRejectedError, match="must not be negative"):
        parent_bounds(remaining_elapsed_s=-0.5)


def test_team_reservations_must_leave_the_parent_something() -> None:
    # Each member fits on its own, but together they consume the parent's whole
    # remaining token budget, so the join would have nothing to run under.
    tight = parent_bounds(remaining_tokens=30_000)
    a = worker_task(task_id="t-a", parent=tight)
    b = worker_task(
        task_id="t-b",
        write_scopes=("src/other/",),
        parent=tight,
        inputs=(HandoffArtifact("brief-2", DIGEST_B),),
    )
    with pytest.raises(SpecialistRejectedError, match="oversubscribes"):
        assemble_team(
            team_id="team-2",
            parent_task_id="parent-1",
            tasks=(a, b),
            claims=(
                WriteClaim("claim-a", "t-a", ("src/impl/",), "src/"),
                WriteClaim("claim-b", "t-b", ("src/other/",), "src/"),
            ),
        )


def test_a_team_whose_reservations_fit_is_accepted() -> None:
    assert team().total_reserved_tokens() == 25_000


# ---------------------------------------------------------------------------
# AC2 — versioned input/output artifacts and no conflicting writes
# ---------------------------------------------------------------------------


def test_a_handoff_requires_a_versioned_input() -> None:
    with pytest.raises(SpecialistRejectedError, match="versioned input artifact"):
        worker_task(inputs=())


def test_an_artifact_version_must_be_a_digest() -> None:
    with pytest.raises(SpecialistRejectedError, match="sha256 hex digest"):
        HandoffArtifact("brief", "v2")


def test_duplicate_handoff_inputs_are_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="appears twice"):
        worker_task(
            inputs=(
                HandoffArtifact("brief", DIGEST_A),
                HandoffArtifact("brief", DIGEST_B),
            )
        )


def test_two_versions_of_one_artifact_are_distinguishable() -> None:
    v1 = HandoffArtifact("brief", DIGEST_A)
    v2 = HandoffArtifact("brief", DIGEST_B)
    assert v1 != v2
    assert v1.to_mapping()["version_sha256"] != v2.to_mapping()["version_sha256"]


def test_the_handoff_digest_changes_with_the_input_version() -> None:
    other = worker_task(inputs=(HandoffArtifact("brief", DIGEST_B),))
    assert worker_task().handoff_digest != other.handoff_digest


def test_overlapping_write_claims_are_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="overlaps task"):
        assert_scopes_disjoint(
            (
                WriteClaim("claim-1", "t-a", ("src/impl/",), "src/"),
                WriteClaim("claim-2", "t-b", ("src/impl/file.py",), "src/"),
            )
        )


def test_a_nested_scope_conflict_is_detected() -> None:
    with pytest.raises(SpecialistRejectedError, match="overlaps task"):
        assert_scopes_disjoint(
            (
                WriteClaim("claim-1", "t-a", ("src/",), "src/"),
                WriteClaim("claim-2", "t-b", ("src/deep/deeper/",), "src/"),
            )
        )


def test_disjoint_write_claims_are_accepted() -> None:
    claims = assert_scopes_disjoint(
        (
            WriteClaim("claim-1", "t-a", ("src/impl/",), "src/"),
            WriteClaim("claim-2", "t-b", ("src/other/",), "src/"),
        )
    )
    assert len(claims) == 2


def test_a_claim_conflicting_with_an_existing_claim_is_refused() -> None:
    seed = (WriteClaim("claim-0", "t-existing", ("src/impl/",), "src/"),)
    with pytest.raises(SpecialistRejectedError, match="overlaps task"):
        assert_scopes_disjoint(
            (WriteClaim("claim-1", "t-new", ("src/impl/",), "src/"),), seed=seed
        )


def test_a_claim_outside_the_parent_scope_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="outside the parent scope"):
        WriteClaim("claim-1", "t-impl", ("secrets/",), "src/")


def test_an_empty_claim_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="at least one scope"):
        WriteClaim("claim-1", "t-impl", (), "src/")


def test_a_write_scope_without_a_claim_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="holds no claim"):
        assemble_team(
            team_id="team-3",
            parent_task_id="parent-1",
            tasks=(worker_task(),),
            claims=(),
        )


def test_a_claim_for_an_unknown_task_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="not in"):
        team(claims=(WriteClaim("claim-9", "t-ghost", ("src/impl/",), "src/"),))


def test_duplicate_task_ids_are_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="appears twice"):
        team(
            tasks=(worker_task(), worker_task()),
            claims=(WriteClaim("claim-1", "t-impl", ("src/impl/",), "src/"),),
        )


# ---------------------------------------------------------------------------
# AC3 — durable, bounded cancel/failure/restart joins
# ---------------------------------------------------------------------------


def _dispatched(task_id: str = "t-impl", at: float = 1.0) -> JoinLedger:
    return JoinLedger().record(
        DispatchEntry(task_id=task_id, state="dispatched", at=at)
    )


def test_a_join_requires_a_dispatch() -> None:
    with pytest.raises(SpecialistRejectedError, match="without a dispatch"):
        JoinLedger().record(DispatchEntry("t-impl", "completed", 2.0))


def test_a_join_requires_a_settled_outcome() -> None:
    with pytest.raises(SpecialistRejectedError, match="not joinable"):
        _dispatched().record(DispatchEntry("t-impl", "joined", 2.0))


def test_a_dispatched_task_becomes_joinable_once_settled() -> None:
    ledger = _dispatched().record(DispatchEntry("t-impl", "completed", 2.0))
    ledger = ledger.record(DispatchEntry("t-impl", "joined", 3.0))
    join = ledger.reconcile()[0]
    assert join.joined is True
    assert join.discarded is False


def test_a_failed_entry_must_state_why() -> None:
    with pytest.raises(SpecialistRejectedError, match="must state why"):
        DispatchEntry("t-impl", "failed", 2.0)


def test_a_cancelled_entry_must_state_why() -> None:
    with pytest.raises(SpecialistRejectedError, match="must state why"):
        DispatchEntry("t-impl", "cancelled", 2.0)


def test_a_failure_is_recorded_not_dropped() -> None:
    ledger = _dispatched().record(
        DispatchEntry("t-impl", "failed", 2.0, reason="tool timeout")
    )
    join = ledger.reconcile()[0]
    assert join.state == "failed"
    assert join.joined is False
    assert join.reason == "tool timeout"


def test_a_discarded_join_is_distinguishable_from_an_accepted_one() -> None:
    ledger = (
        _dispatched()
        .record(DispatchEntry("t-impl", "completed", 2.0))
        .record(DispatchEntry("t-impl", "joined_discarded", 3.0, reason="superseded"))
    )
    join = ledger.reconcile()[0]
    assert join.joined is True and join.discarded is True


def test_a_joined_task_cannot_be_redispatched() -> None:
    ledger = (
        _dispatched()
        .record(DispatchEntry("t-impl", "completed", 2.0))
        .record(DispatchEntry("t-impl", "joined", 3.0))
    )
    with pytest.raises(SpecialistRejectedError, match="already joined"):
        ledger.record(DispatchEntry("t-impl", "dispatched", 4.0))


def test_a_duplicate_dispatch_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="already dispatched"):
        _dispatched().record(DispatchEntry("t-impl", "dispatched", 2.0))


def test_the_ledger_is_bounded() -> None:
    entries = tuple(
        DispatchEntry(f"t-{i}", "dispatched", float(i))
        for i in range(bs.MAX_LEDGER_ENTRIES + 1)
    )
    with pytest.raises(SpecialistRejectedError, match="exceeds the bound"):
        JoinLedger(entries=entries)


def test_an_open_task_is_the_restart_worklist() -> None:
    ledger = _dispatched("t-impl").record(DispatchEntry("t-impl", "completed", 2.0))
    ledger = ledger.record(DispatchEntry("t-other", "dispatched", 3.0))
    assert ledger.open_task_ids() == ("t-other",)


def test_restart_cancels_open_tasks_with_a_reason() -> None:
    reconciled = _dispatched().after_restart(now=10.0)
    join = reconciled.reconcile()[0]
    assert join.state == "cancelled"
    assert join.reason == "restart_reconciliation"
    assert join.joined is False
    assert reconciled.open_task_ids() == ()


def test_restart_reconciliation_is_visible_on_the_join() -> None:
    join = _dispatched().after_restart(now=10.0).reconcile()[0]
    assert join.reconciled_after_restart is True


def test_restart_does_not_disturb_a_joined_task() -> None:
    ledger = (
        _dispatched()
        .record(DispatchEntry("t-impl", "completed", 2.0))
        .record(DispatchEntry("t-impl", "joined", 3.0))
    )
    assert ledger.after_restart(now=10.0).reconcile()[0].state == "joined"


def test_consumption_is_carried_into_the_join() -> None:
    ledger = _dispatched().record(
        DispatchEntry(
            "t-impl",
            "completed",
            2.0,
            consumed_steps=4,
            consumed_tool_calls=9,
            consumed_tokens=1_200,
        )
    )
    join = ledger.reconcile()[0]
    assert (join.consumed_steps, join.consumed_tool_calls, join.consumed_tokens) == (
        4,
        9,
        1_200,
    )


def test_a_join_is_rebuilt_from_persisted_entries() -> None:
    ledger = (
        _dispatched()
        .record(
            DispatchEntry(
                "t-impl", "completed", 2.0, output=HandoffArtifact("patch", DIGEST_B)
            )
        )
        .record(DispatchEntry("t-impl", "joined", 3.0))
    )
    reloaded = JoinLedger.from_mapping(json.loads(json.dumps(ledger.to_mapping())))
    assert reloaded.digest() == ledger.digest()
    assert reloaded.reconcile()[0].output is not None


def test_the_ledger_digest_is_stable() -> None:
    assert _dispatched().digest() == _dispatched().digest()


# ---------------------------------------------------------------------------
# AC4 — the reviewer gets a brief and evidence, never an endorsement request
# ---------------------------------------------------------------------------


def test_a_brief_without_evidence_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="must carry evidence"):
        build_reviewer_brief(
            brief_id="brief-1",
            reviewer_task_id="t-review",
            builder=worker_task(),
            builder_output=HandoffArtifact("patch", DIGEST_B),
            evidence=(),
        )


def test_a_brief_that_asks_for_endorsement_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="endorse"):
        build_reviewer_brief(
            brief_id="brief-1",
            reviewer_task_id="t-review",
            builder=worker_task(),
            builder_output=HandoffArtifact("patch", DIGEST_B),
            evidence=("please approve this change",),
        )


def test_each_endorsement_phrase_is_caught() -> None:
    for phrase in (
        "sign off on it",
        "just confirm",
        "rubber stamp",
        "endorse the design",
    ):
        with pytest.raises(SpecialistRejectedError, match="endorse"):
            assert_no_endorsement_request(f"Reviewer: {phrase}", field="summary")


def test_ordinary_review_language_is_allowed() -> None:
    assert_no_endorsement_request(
        "Check whether the error path is handled.", field="summary"
    )


def test_a_reviewer_cannot_review_itself() -> None:
    with pytest.raises(SpecialistRejectedError, match="itself"):
        build_reviewer_brief(
            brief_id="brief-1",
            reviewer_task_id="t-impl",
            builder=worker_task(),
            builder_output=HandoffArtifact("patch", DIGEST_B),
            evidence=("tests passed",),
        )


def test_the_brief_carries_the_inputs_and_the_output() -> None:
    brief = build_reviewer_brief(
        brief_id="brief-1",
        reviewer_task_id="t-review",
        builder=worker_task(),
        builder_output=HandoffArtifact("patch", DIGEST_B),
        evidence=("tests passed",),
        open_questions=("is the retry bounded?",),
    )
    assert {a.artifact_id for a in brief.artifacts} == {"brief", "patch"}
    assert brief.to_mapping()["requests_endorsement"] is False


def test_the_brief_records_no_verdict_field() -> None:
    brief = build_reviewer_brief(
        brief_id="brief-1",
        reviewer_task_id="t-review",
        builder=worker_task(),
        builder_output=HandoffArtifact("patch", DIGEST_B),
        evidence=("tests passed",),
    )
    assert "verdict" not in brief.to_mapping()
    assert brief.to_mapping()["verdict_is_agent_decided"] is False


def test_an_advisory_role_cannot_hold_a_write_scope() -> None:
    with pytest.raises(SpecialistRejectedError, match="advisory and cannot hold"):
        reviewer_task(write_scopes=("src/impl/",))


def test_an_advisory_role_cannot_hold_mutating_tools() -> None:
    with pytest.raises(
        SpecialistRejectedError, match="advisory and cannot hold mutating"
    ):
        reviewer_task(allowed_tools=("read_file", "write_file"))


def test_advisory_mutating_tools_come_from_the_delegation_contract() -> None:
    # A tool classified as mutating by child_delegation is mutating here too. The
    # parent holds every one of them, so the refusal is the advisory rule rather
    # than the AC1 subset check firing first.
    permissive = parent_bounds(
        parent_tools=("read_file",)
        + tuple(sorted(child_delegation.CD_MUTATING_FILE_TOOLS))
    )
    for tool in child_delegation.CD_MUTATING_FILE_TOOLS:
        with pytest.raises(SpecialistRejectedError, match="cannot hold mutating"):
            reviewer_task(allowed_tools=("read_file", tool), parent=permissive)


# ---------------------------------------------------------------------------
# AC5 — logical parallelism is not concurrent local inference
# ---------------------------------------------------------------------------


def test_logical_parallelism_does_not_imply_concurrent_inference() -> None:
    plan = ParallelismPlan(logical_parallelism=4)
    assert plan.decodes_concurrently is False
    assert (
        plan.to_mapping()["logical_parallelism_implies_concurrent_inference"] is False
    )


def test_claiming_concurrent_local_inference_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="does not imply"):
        ParallelismPlan(
            logical_parallelism=4,
            concurrent_local_inference=4,
            local_inference_claimed=True,
        )


def test_a_plan_with_concurrent_inference_is_visible_even_unclaimed() -> None:
    assert ParallelismPlan(
        logical_parallelism=2, concurrent_local_inference=2
    ).decodes_concurrently


def test_zero_parallelism_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="at least 1"):
        ParallelismPlan(logical_parallelism=0)


def test_a_plan_must_cover_its_team() -> None:
    with pytest.raises(SpecialistRejectedError, match="understates"):
        team(plan=ParallelismPlan(logical_parallelism=1))


def test_the_default_plan_covers_the_team() -> None:
    assembled = assemble_team(
        team_id="team-4",
        parent_task_id="parent-1",
        tasks=(worker_task(), reviewer_task()),
        claims=(WriteClaim("claim-1", "t-impl", ("src/impl/",), "src/"),),
    )
    assert assembled.plan.logical_parallelism == 2


def test_an_empty_team_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="at least one specialist task"):
        assemble_team(team_id="team-5", parent_task_id="parent-1", tasks=())


# ---------------------------------------------------------------------------
# AC6 — retain a team topology only where measurement says it wins
# ---------------------------------------------------------------------------


def test_a_measured_quality_win_with_no_worse_ops_retains_the_team() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.9),
        metrics_by_strategy=_metrics(),
    )
    assert decision.topology == "specialists"
    assert decision.measured is True
    assert decision.quality_delta == pytest.approx(0.4)


def test_equal_quality_keeps_the_simpler_execution() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.5),
        metrics_by_strategy=_metrics(),
    )
    assert decision.topology == "single_agent"
    assert decision.measured is True


def test_worse_quality_keeps_the_simpler_execution() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.2),
        metrics_by_strategy=_metrics(),
    )
    assert decision.topology == "single_agent"
    assert "do not improve verified quality" in decision.reason


def test_a_quality_win_with_higher_cost_keeps_the_simpler_execution() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.9),
        metrics_by_strategy=_metrics(cost=0.35, latency=1_800.0, interventions=4),
    )
    assert decision.topology == "single_agent"
    assert "cost more" in decision.reason


def test_unmeasured_operational_metrics_keep_the_simpler_execution() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.9),
        metrics_by_strategy=None,
    )
    assert decision.topology == "single_agent"
    assert decision.measured is True
    assert "not measured" in decision.reason


def test_an_untrustworthy_comparison_keeps_the_simpler_execution() -> None:
    unjustified = StrategyResult(
        "specialists",
        (
            ScoredOutcome(
                "run-bad",
                "specialists",
                "task-1",
                "held_out",
                0.99,
                (
                    Evidence("deterministic_check", "pytest", passes=False),
                    Evidence("factual_verification", "ci-run-2"),
                ),
                "scorer-1",
            ),
        ),
    )
    decision = decide_topology(
        design=_design(),
        results=(
            _result("no_op_incumbent", 0.0),
            _result("single_agent", 0.5),
            unjustified,
        ),
        metrics_by_strategy=_metrics(),
    )
    assert decision.topology == "single_agent"
    assert decision.measured is False
    assert "not trustworthy" in decision.reason


def test_unequal_budgets_keep_the_simpler_execution() -> None:
    decision = decide_topology(
        design=_design(specialists_tokens=9_000),
        results=_results(specialist_score=0.9),
        metrics_by_strategy=_metrics(),
    )
    assert decision.topology == "single_agent"
    assert decision.measured is False


def test_a_missing_challenger_is_unmeasured() -> None:
    decision = decide_topology(
        design=_design(),
        results=(_result("no_op_incumbent", 0.0), _result("single_agent", 0.5)),
    )
    assert decision.topology == "single_agent"
    assert decision.measured is False


def test_a_missing_incumbent_is_refused_rather_than_estimated() -> None:
    decision = decide_topology(
        design=_design(),
        results=(_result("single_agent", 0.5), _result("specialists", 0.9)),
    )
    assert decision.topology == "single_agent"
    assert decision.measured is False
    assert "not usable" in decision.reason


def test_an_unmeasured_specialist_topology_cannot_be_constructed() -> None:
    with pytest.raises(SpecialistRejectedError, match="measured evidence"):
        TopologyDecision(
            topology="specialists",
            retained=True,
            reason="the team feels better",
            measured=False,
        )


def test_the_baseline_cannot_be_discarded() -> None:
    with pytest.raises(SpecialistRejectedError, match="nothing simpler"):
        TopologyDecision(
            topology="single_agent", retained=False, reason="no", measured=False
        )


def test_an_unknown_topology_is_refused() -> None:
    with pytest.raises(SpecialistRejectedError, match="Unknown topology"):
        TopologyDecision(
            topology=cast(Any, "swarm"),
            retained=True,
            reason="many agents",
            measured=True,
        )


def test_a_measured_decision_states_its_deltas() -> None:
    decision = decide_topology(
        design=_design(),
        results=_results(specialist_score=0.9),
        metrics_by_strategy=_metrics(),
    )
    mapping = decision.to_mapping()
    assert mapping["quality_delta"] == pytest.approx(0.4)
    assert mapping["measured"] is True


# ---------------------------------------------------------------------------
# Reviewability
# ---------------------------------------------------------------------------


def test_the_rendered_team_is_deterministic_json() -> None:
    first = render_team(team())
    assert first == render_team(team())
    assert first.endswith("\n")
    parsed = json.loads(first)
    assert list(parsed) == sorted(parsed)


def test_the_rendered_team_states_the_boundaries() -> None:
    rules = json.loads(render_team(team()))["specialist_rules"]
    assert rules["authority_is_strict_parent_subset"] is True
    assert rules["overlapping_write_scopes_permitted"] is False
    assert rules["unbounded_join_permitted"] is False
    assert rules["reviewer_may_be_instructed_to_endorse"] is False
    assert rules["logical_parallelism_implies_concurrent_inference"] is False
    assert rules["topology_retained_without_measurement"] is False


def test_the_rendered_team_does_not_overclaim() -> None:
    payload = json.loads(render_team(team()))
    assert len(payload["does_not_prove"]) == 3
    assert any("no model" in line for line in payload["does_not_prove"])


def test_the_rendered_team_carries_its_joins() -> None:
    ledger = _dispatched().record(DispatchEntry("t-impl", "completed", 2.0))
    payload = json.loads(render_team(team(), ledger=ledger))
    assert payload["joins"][0]["state"] == "completed"


def test_the_team_digest_is_stable() -> None:
    assert team().digest() == team().digest()


def test_the_team_digest_changes_when_a_scope_changes() -> None:
    other = team(
        tasks=(worker_task(write_scopes=("src/other/",)), reviewer_task()),
        claims=(WriteClaim("claim-1", "t-impl", ("src/other/",), "src/"),),
    )
    assert other.digest() != team().digest()


def test_the_artifact_binding_uses_the_lineage_identity() -> None:
    artifact = bind_artifact_identity(
        artifact_id="brief", identity_sha256=DIGEST_A, produced_by="planner"
    )
    assert artifact.version_sha256 == DIGEST_A
    assert artifact.to_mapping()["produced_by"] == "planner"


def test_no_module_verb_runs_a_workflow_or_contacts_a_provider() -> None:
    forbidden = (
        "run_workflow",
        "start_runtime",
        "load_model",
        "call_provider",
        "activate",
        "spawn",
        "execute",
        "serve",
        "deploy",
    )
    assert not [name for name in forbidden if hasattr(bs, name)]
