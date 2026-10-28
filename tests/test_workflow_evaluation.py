from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

import pytest

from capability_negotiation import TaskRequirements
from execution_budget import BudgetUsage
from model_comparison import EvalConfig, EvalTask, RubricDimension
from workflow_evaluation import (
    CostMeasurement,
    WorkflowEvaluationError,
    WorkflowOutcome,
    WorkflowRun,
    WorkflowStrategy,
    compare_workflows,
)
from workflow_tracing import WorkflowTrace

HARDWARE_DIGEST = "sha256:" + "ab" * 32
ENVIRONMENT = {"os": "Linux x86_64", "runtime": "fixture-1"}


def _task() -> EvalTask:
    return EvalTask("summarize", 1, TaskRequirements(), (RubricDimension("task_success"),))


def _config() -> EvalConfig:
    return EvalConfig(max_output_tokens=128, seed=7, environment=ENVIRONMENT)


def _run(
    run_id: str,
    strategy: str,
    *,
    success: bool = True,
    outcome: str = "completed",
    latency: float = 100.0,
    cost: float = 0.1,
    currency: str = "USD",
    memory: int = 500,
    hardware: str = HARDWARE_DIGEST,
    environment: tuple[tuple[str, str], ...] | dict[str, str] = ENVIRONMENT,
    task: EvalTask | None = None,
    config: EvalConfig | None = None,
) -> WorkflowRun:
    active_task, active_config = task or _task(), config or _config()
    return WorkflowRun(
        run_id=run_id,
        strategy=cast(WorkflowStrategy, strategy),
        task_digest=active_task.digest,
        config_digest=active_config.digest,
        hardware_digest=hardware,
        outcome=cast(WorkflowOutcome, outcome),
        task_success=success,
        latency_ms=latency,
        cost=CostMeasurement(cost, currency),
        peak_memory_bytes=memory,
        environment=environment,
    )


def _runs() -> tuple[WorkflowRun, ...]:
    return (
        _run("single-1", "single_model", latency=100, cost=0.1, memory=400),
        _run("single-2", "single_model", success=False, outcome="failed", latency=200, cost=0.2, memory=600),
        _run("multi-1", "multi_agent", latency=80, cost=0.08, memory=700),
        _run("multi-2", "multi_agent", latency=120, cost=0.12, memory=900),
        _run("multi-3", "multi_agent", success=False, outcome="failed", latency=160, cost=0.16, memory=1100),
    )


def test_budget_usage_and_trace_supply_metrics_without_copying_trace_details() -> None:
    trace = WorkflowTrace("workflow-1", trace_id="trace.123", clock=lambda: 77.0)
    trace.record("workflow.completed", "completed", detail="sensitive prompt text")
    run = WorkflowRun.from_trace_and_budget(
        trace=trace,
        budget_usage=BudgetUsage(elapsed_seconds=1.25, tokens=12, cost=0.03, tools=0, retries=0),
        strategy="single_model",
        task_digest=_task().digest,
        config_digest=_config().digest,
        hardware_digest=HARDWARE_DIGEST,
        task_success=True,
        currency="USD",
        peak_memory_bytes=64,
        environment=ENVIRONMENT,
    )
    assert run.run_id == "trace.123" and run.outcome == "completed"
    assert run.latency_ms == 1250.0 and run.cost == CostMeasurement(0.03, "USD")
    exported = json.dumps(run.to_mapping())
    assert "sensitive prompt text" not in exported and "timestamp" not in exported

    unfinished = WorkflowTrace("workflow-2", trace_id="trace.456")
    with pytest.raises(WorkflowEvaluationError, match="terminal workflow outcome"):
        WorkflowRun.from_trace_and_budget(
            trace=unfinished,
            budget_usage=BudgetUsage(1.0, 0, 0.0, 0, 0),
            strategy="multi_agent",
            task_digest=_task().digest,
            config_digest=_config().digest,
            hardware_digest=HARDWARE_DIGEST,
            task_success=False,
            currency="USD",
            peak_memory_bytes=0,
            environment=ENVIRONMENT,
        )


def test_exact_success_latency_cost_memory_aggregates_and_deltas() -> None:
    comparison = compare_workflows(_task(), _config(), _runs())
    single, multi = comparison.single_model, comparison.multi_agent
    assert (single.run_count, single.successful_runs, single.success_rate) == (2, 1, 0.5)
    assert single.latency_ms.to_mapping() == {"median": 150.0, "spread": 100.0, "minimum": 100.0, "maximum": 200.0, "count": 2}
    assert single.cost.to_mapping() == {"median": 0.15, "spread": 0.1, "minimum": 0.1, "maximum": 0.2, "count": 2}
    assert single.peak_memory_bytes.to_mapping() == {"median": 500.0, "spread": 200.0, "minimum": 400.0, "maximum": 600.0, "count": 2}
    assert (multi.run_count, multi.successful_runs, multi.success_rate) == (3, 2, 0.666667)
    assert multi.latency_ms.to_mapping() == {"median": 120.0, "spread": 80.0, "minimum": 80.0, "maximum": 160.0, "count": 3}
    assert multi.cost.to_mapping() == {"median": 0.12, "spread": 0.08, "minimum": 0.08, "maximum": 0.16, "count": 3}
    assert multi.peak_memory_bytes.to_mapping() == {"median": 900.0, "spread": 400.0, "minimum": 700.0, "maximum": 1100.0, "count": 3}
    assert comparison.deltas.to_mapping() == {
        "success_rate": 0.166667,
        "latency_ms": -30.0,
        "cost": -0.03,
        "peak_memory_bytes": 400.0,
    }
    assert comparison.currency == "USD"
    assert comparison.task_digest == _task().digest
    assert comparison.config_digest == _config().digest


def test_result_is_order_independent_canonical_and_export_isolated() -> None:
    expected = compare_workflows(_task(), _config(), _runs())
    reversed_result = compare_workflows(_task(), _config(), tuple(reversed(_runs())))
    assert expected.to_mapping() == reversed_result.to_mapping()
    assert expected.digest == reversed_result.digest
    assert json.loads(json.dumps(expected.to_mapping())) == expected.to_mapping()
    exported = expected.to_mapping()
    exported["single_model"]["latency_ms"]["median"] = -1
    assert expected.digest == reversed_result.digest
    assert expected.single_model.latency_ms.median == 150.0
    mapping = json.dumps(expected.to_mapping(), sort_keys=True)
    assert "timestamp" not in mapping and "prompt" not in mapping and "secret" not in mapping
    secret_environment = {"note": "prompt text secret value"}
    sensitive_config = EvalConfig(max_output_tokens=128, seed=7, environment=secret_environment)
    sensitive_runs = (
        _run("private-a", "single_model", config=sensitive_config, environment=secret_environment),
        _run("private-b", "multi_agent", config=sensitive_config, environment=secret_environment),
    )
    sensitive_result = compare_workflows(_task(), sensitive_config, sensitive_runs)
    assert "prompt text secret value" not in json.dumps(sensitive_result.to_mapping())


def test_comparison_requires_both_strategies_and_matching_task_config_and_context() -> None:
    runs = _runs()
    with pytest.raises(WorkflowEvaluationError, match="each strategy"):
        compare_workflows(_task(), _config(), (runs[0], runs[1]))
    with pytest.raises(WorkflowEvaluationError, match="task digest"):
        compare_workflows(_task(), _config(), (runs[0], _run("multi", "multi_agent", task=EvalTask("other", 1, TaskRequirements(), (RubricDimension("task_success"),)))))
    with pytest.raises(WorkflowEvaluationError, match="config digest"):
        compare_workflows(_task(), _config(), (runs[0], _run("multi", "multi_agent", config=EvalConfig(256, seed=7, environment=ENVIRONMENT))))
    with pytest.raises(WorkflowEvaluationError, match="hardware contexts"):
        compare_workflows(_task(), _config(), (runs[0], _run("multi", "multi_agent", hardware="sha256:" + "cd" * 32)))
    with pytest.raises(WorkflowEvaluationError, match="environments"):
        compare_workflows(_task(), _config(), (runs[0], _run("multi", "multi_agent", environment={"os": "Other OS"})))
    with pytest.raises(WorkflowEvaluationError, match="one currency"):
        compare_workflows(_task(), _config(), (runs[0], _run("multi", "multi_agent", currency="EUR")))


def test_run_validation_refusals() -> None:
    with pytest.raises(WorkflowEvaluationError, match="canonical sha256"):
        replace(_runs()[0], task_digest="not-a-digest")
    with pytest.raises(WorkflowEvaluationError, match="strategy"):
        _run("bad", "ensemble")
    with pytest.raises(WorkflowEvaluationError, match="failed or cancelled"):
        _run("bad-success", "single_model", outcome="failed", success=True)
    with pytest.raises(TypeError, match="task_success"):
        WorkflowRun("bad-bool", "single_model", _task().digest, _config().digest, HARDWARE_DIGEST, "completed", cast(bool, 1), 1, CostMeasurement(0, "USD"), 1, ENVIRONMENT)
    with pytest.raises(WorkflowEvaluationError, match="finite non-negative"):
        _run("negative-latency", "single_model", latency=-1)
    with pytest.raises(TypeError, match="peak_memory_bytes"):
        _run("bool-memory", "single_model", memory=True)
    with pytest.raises(WorkflowEvaluationError, match="peak_memory_bytes"):
        _run("negative-memory", "single_model", memory=-1)
    with pytest.raises(TypeError, match="cost must be"):
        WorkflowRun("no-cost", "single_model", _task().digest, _config().digest, HARDWARE_DIGEST, "completed", True, 1, cast(CostMeasurement, "0.1"), 1, ENVIRONMENT)
    with pytest.raises(WorkflowEvaluationError, match="currency"):
        CostMeasurement(0.1, "usd")
    with pytest.raises(TypeError, match="cost amount"):
        CostMeasurement(True, "USD")
    with pytest.raises(WorkflowEvaluationError, match="finite non-negative"):
        CostMeasurement(float("inf"), "USD")
    with pytest.raises(WorkflowEvaluationError, match="must not be empty"):
        _run("\x00", "single_model")
    with pytest.raises(WorkflowEvaluationError, match="unique after normalization"):
        _run("duplicate-env", "single_model", environment=(("os", "Linux"), (" os ", "Linux")))


def test_aggregation_refuses_empty_duplicate_and_malformed_runs() -> None:
    with pytest.raises(TypeError, match="EvalTask"):
        compare_workflows(cast(EvalTask, "task"), _config(), _runs())
    with pytest.raises(TypeError, match="EvalConfig"):
        compare_workflows(_task(), cast(EvalConfig, "config"), _runs())
    with pytest.raises(WorkflowEvaluationError, match="non-empty"):
        compare_workflows(_task(), _config(), ())
    with pytest.raises(TypeError, match="WorkflowRun"):
        compare_workflows(_task(), _config(), cast(Sequence[WorkflowRun], (_runs()[0], "bad")))
    duplicate = replace(_runs()[0], strategy="multi_agent")
    with pytest.raises(WorkflowEvaluationError, match="run_id values must be unique"):
        compare_workflows(_task(), _config(), (_runs()[0], duplicate, _runs()[2]))
