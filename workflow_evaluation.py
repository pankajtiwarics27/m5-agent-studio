"""Deterministic comparison of measured single-model and multi-agent workflows.

This module only validates and aggregates caller-supplied run measurements. It
never executes a workflow, derives a price, reads a trace, or contacts a model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from execution_budget import BudgetUsage
from model_comparison import EvalConfig, EvalTask
from workflow_tracing import WorkflowTrace

WORKFLOW_EVALUATION_SCHEMA_VERSION = 1
WorkflowStrategy = Literal["single_model", "multi_agent"]
WorkflowOutcome = Literal["completed", "failed", "cancelled"]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_TEXT_LIMIT = 128


class WorkflowEvaluationError(ValueError):
    """Raised when workflow measurements are invalid or incomparable."""


def _text(value: object, field: str, *, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    normalized = " ".join("".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split())
    if not normalized:
        raise WorkflowEvaluationError(f"{field} must not be empty.")
    if len(normalized) > limit:
        raise WorkflowEvaluationError(f"{field} must be at most {limit} characters.")
    return normalized


def _digest_text(value: object, field: str) -> str:
    normalized = _text(value, field)
    if not _DIGEST.fullmatch(normalized):
        raise WorkflowEvaluationError(f"{field} must be a canonical sha256 digest.")
    return normalized


def _finite_non_negative(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a finite non-negative number.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise WorkflowEvaluationError(f"{field} must be a finite non-negative number.")
    return number


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _environment_digest(value: object) -> str:
    return _canonical_digest({"environment": dict(_environment(value))})


def _environment(value: object) -> tuple[tuple[str, str], ...]:
    if isinstance(value, Mapping):
        pairs = [(_text(key, "environment key", limit=64), _text(item, "environment value")) for key, item in value.items()]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        pairs = []
        for item in value:
            if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
                raise WorkflowEvaluationError("environment entries must be two-string key/value pairs.")
            pairs.append((_text(item[0], "environment key", limit=64), _text(item[1], "environment value")))
    else:
        raise TypeError("environment must be a mapping or a sequence of key/value pairs.")
    if len({key for key, _ in pairs}) != len(pairs):
        raise WorkflowEvaluationError("environment keys must be unique after normalization.")
    return tuple(sorted(pairs))


@dataclass(frozen=True, slots=True)
class CostMeasurement:
    """One caller-observed cost value; no price or exchange rate is inferred."""

    amount: float
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _finite_non_negative(self.amount, "cost amount"))
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise WorkflowEvaluationError("currency must be a three-letter uppercase code.")

    def to_mapping(self) -> dict[str, Any]:
        return {"amount": self.amount, "currency": self.currency}


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One measured run, identified by task/config/context rather than prompt."""

    run_id: str
    strategy: WorkflowStrategy
    task_digest: str
    config_digest: str
    hardware_digest: str
    outcome: WorkflowOutcome
    task_success: bool
    latency_ms: float
    cost: CostMeasurement
    peak_memory_bytes: int
    environment: tuple[tuple[str, str], ...] | Mapping[str, str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        if not _OPAQUE_ID.fullmatch(self.run_id):
            raise WorkflowEvaluationError("run_id must be a bounded opaque identifier.")
        if self.strategy not in ("single_model", "multi_agent"):
            raise WorkflowEvaluationError("strategy must be single_model or multi_agent.")
        object.__setattr__(self, "task_digest", _digest_text(self.task_digest, "task_digest"))
        object.__setattr__(self, "config_digest", _digest_text(self.config_digest, "config_digest"))
        object.__setattr__(self, "hardware_digest", _digest_text(self.hardware_digest, "hardware_digest"))
        if self.outcome not in ("completed", "failed", "cancelled"):
            raise WorkflowEvaluationError("outcome must be completed, failed, or cancelled.")
        if not isinstance(self.task_success, bool):
            raise TypeError("task_success must be a boolean.")
        if self.outcome != "completed" and self.task_success:
            raise WorkflowEvaluationError("failed or cancelled workflow runs cannot be task successes.")
        object.__setattr__(self, "latency_ms", _finite_non_negative(self.latency_ms, "latency_ms"))
        if not isinstance(self.cost, CostMeasurement):
            raise TypeError("cost must be a CostMeasurement value.")
        if isinstance(self.peak_memory_bytes, bool) or not isinstance(self.peak_memory_bytes, int):
            raise TypeError("peak_memory_bytes must be a non-negative integer.")
        if self.peak_memory_bytes < 0:
            raise WorkflowEvaluationError("peak_memory_bytes must be a non-negative integer.")
        object.__setattr__(self, "environment", _environment(self.environment))

    @classmethod
    def from_trace_and_budget(
        cls,
        *,
        trace: WorkflowTrace,
        budget_usage: BudgetUsage,
        strategy: WorkflowStrategy,
        task_digest: str,
        config_digest: str,
        hardware_digest: str,
        task_success: bool,
        currency: str,
        peak_memory_bytes: int,
        environment: tuple[tuple[str, str], ...] | Mapping[str, str] = (),
    ) -> WorkflowRun:
        """Build a run from terminal trace status and execution-budget usage.

        Trace event details and timestamps are intentionally not copied; only
        the terminal status and opaque trace ID are used.
        """
        if not isinstance(trace, WorkflowTrace):
            raise TypeError("trace must be a WorkflowTrace value.")
        if not isinstance(budget_usage, BudgetUsage):
            raise TypeError("budget_usage must be a BudgetUsage value.")
        events = trace.events()
        if not events or events[-1].status not in ("completed", "failed", "cancelled"):
            raise WorkflowEvaluationError("trace must contain a terminal workflow outcome.")
        return cls(
            run_id=trace.trace_id,
            strategy=strategy,
            task_digest=task_digest,
            config_digest=config_digest,
            hardware_digest=hardware_digest,
            outcome=events[-1].status,
            task_success=task_success,
            latency_ms=budget_usage.elapsed_seconds * 1000,
            cost=CostMeasurement(budget_usage.cost, currency),
            peak_memory_bytes=peak_memory_bytes,
            environment=environment,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "hardware_digest": self.hardware_digest,
            "outcome": self.outcome,
            "task_success": self.task_success,
            "latency_ms": self.latency_ms,
            "cost": self.cost.to_mapping(),
            "peak_memory_bytes": self.peak_memory_bytes,
            "environment_digest": _environment_digest(self.environment),
        }


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """Median, spread, bounds, and sample count for one measured dimension."""

    median: float
    spread: float
    minimum: float
    maximum: float
    count: int

    def to_mapping(self) -> dict[str, float | int]:
        return {
            "median": self.median,
            "spread": self.spread,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class StrategySummary:
    """Measured success and resource metrics for one workflow strategy."""

    strategy: WorkflowStrategy
    run_count: int
    successful_runs: int
    success_rate: float
    latency_ms: MetricSummary
    cost: MetricSummary
    peak_memory_bytes: MetricSummary

    def to_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "run_count": self.run_count,
            "successful_runs": self.successful_runs,
            "success_rate": self.success_rate,
            "latency_ms": self.latency_ms.to_mapping(),
            "cost": self.cost.to_mapping(),
            "peak_memory_bytes": self.peak_memory_bytes.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class WorkflowDeltas:
    """Multi-agent minus single-model; lower resource deltas are favorable."""

    success_rate: float
    latency_ms: float
    cost: float
    peak_memory_bytes: float

    def to_mapping(self) -> dict[str, float]:
        return {
            "success_rate": self.success_rate,
            "latency_ms": self.latency_ms,
            "cost": self.cost,
            "peak_memory_bytes": self.peak_memory_bytes,
        }


@dataclass(frozen=True, slots=True)
class WorkflowComparison:
    """Deterministic, context-pinned comparison for both workflow strategies."""

    task_digest: str
    config_digest: str
    hardware_digest: str
    currency: str
    environment_digest: str
    single_model: StrategySummary
    multi_agent: StrategySummary
    deltas: WorkflowDeltas

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": WORKFLOW_EVALUATION_SCHEMA_VERSION,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "hardware_digest": self.hardware_digest,
            "currency": self.currency,
            "environment_digest": self.environment_digest,
            "single_model": self.single_model.to_mapping(),
            "multi_agent": self.multi_agent.to_mapping(),
            "multi_agent_minus_single_model": self.deltas.to_mapping(),
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_mapping())


def _summary(values: Sequence[float]) -> MetricSummary:
    return MetricSummary(
        median=round(float(statistics.median(values)), 6),
        spread=round(max(values) - min(values), 6),
        minimum=round(min(values), 6),
        maximum=round(max(values), 6),
        count=len(values),
    )


def _strategy_summary(strategy: WorkflowStrategy, runs: Sequence[WorkflowRun]) -> StrategySummary:
    successes = sum(run.task_success for run in runs)
    return StrategySummary(
        strategy=strategy,
        run_count=len(runs),
        successful_runs=successes,
        success_rate=round(successes / len(runs), 6),
        latency_ms=_summary([run.latency_ms for run in runs]),
        cost=_summary([run.cost.amount for run in runs]),
        peak_memory_bytes=_summary([float(run.peak_memory_bytes) for run in runs]),
    )


def compare_workflows(
    task: EvalTask,
    config: EvalConfig,
    runs: Sequence[WorkflowRun],
) -> WorkflowComparison:
    """Compare both strategies using same task/config/hardware/environment.

    Inputs are measurements from an external executor. Every dimension is
    required so absent cost or memory is never silently represented as zero.
    """
    if not isinstance(task, EvalTask):
        raise TypeError("task must be an EvalTask value.")
    if not isinstance(config, EvalConfig):
        raise TypeError("config must be an EvalConfig value.")
    if isinstance(runs, (str, bytes)) or not isinstance(runs, Sequence) or not runs:
        raise WorkflowEvaluationError("runs must be a non-empty sequence of WorkflowRun values.")
    if not all(isinstance(run, WorkflowRun) for run in runs):
        raise TypeError("runs must contain WorkflowRun values.")
    run_ids = [run.run_id for run in runs]
    if len(set(run_ids)) != len(run_ids):
        raise WorkflowEvaluationError("run_id values must be unique.")
    if any(run.task_digest != task.digest for run in runs):
        raise WorkflowEvaluationError("runs must match the supplied versioned task digest.")
    if any(run.config_digest != config.digest for run in runs):
        raise WorkflowEvaluationError("runs must match the supplied reproducible config digest.")
    hardware_digests = {run.hardware_digest for run in runs}
    if len(hardware_digests) != 1:
        raise WorkflowEvaluationError("runs from different hardware contexts are incomparable.")
    environments = {run.environment for run in runs}
    expected_environment = _environment(config.environment)
    if len(environments) != 1 or next(iter(environments)) != expected_environment:
        raise WorkflowEvaluationError("run environments must match each other and the evaluation config.")
    currencies = {run.cost.currency for run in runs}
    if len(currencies) != 1:
        raise WorkflowEvaluationError("run costs must use one currency.")

    single_runs = tuple(sorted((run for run in runs if run.strategy == "single_model"), key=lambda item: item.run_id))
    multi_runs = tuple(sorted((run for run in runs if run.strategy == "multi_agent"), key=lambda item: item.run_id))
    if not single_runs or not multi_runs:
        raise WorkflowEvaluationError("comparison requires at least one run for each strategy.")
    single = _strategy_summary("single_model", single_runs)
    multi = _strategy_summary("multi_agent", multi_runs)
    deltas = WorkflowDeltas(
        success_rate=round(multi.success_rate - single.success_rate, 6),
        latency_ms=round(multi.latency_ms.median - single.latency_ms.median, 6),
        cost=round(multi.cost.median - single.cost.median, 6),
        peak_memory_bytes=round(multi.peak_memory_bytes.median - single.peak_memory_bytes.median, 6),
    )
    return WorkflowComparison(
        task_digest=task.digest,
        config_digest=config.digest,
        hardware_digest=next(iter(hardware_digests)),
        currency=next(iter(currencies)),
        environment_digest=_environment_digest(expected_environment),
        single_model=single,
        multi_agent=multi,
        deltas=deltas,
    )
