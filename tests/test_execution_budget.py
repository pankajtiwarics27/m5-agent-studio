from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import execution_budget
import runtime_persistence
from execution_budget import (
    BudgetError,
    BudgetExceeded,
    BudgetLimits,
    ExecutionBudget,
    ExecutionCancelled,
    IsolationEvidence,
    TaskBudgetSpec,
    TaskBudgetState,
    isolation_refusal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_token_cost_tool_and_retry_budgets_are_enforced() -> None:
    budget = ExecutionBudget(BudgetLimits(max_seconds=10, max_tokens=10, max_cost=1.0, max_tools=2, max_retries=1))
    budget.consume(tokens=5, cost=0.5, tools=1)
    assert budget.allow_retry() is True
    assert budget.allow_retry() is False
    with pytest.raises(BudgetExceeded, match="token"):
        budget.consume(tokens=6)

    fresh = ExecutionBudget(BudgetLimits(10, 10, 1.0, 2))
    fresh.consume(tools=2)
    with pytest.raises(BudgetExceeded, match="tool"):
        fresh.consume(tools=1)
    with pytest.raises(BudgetExceeded, match="cost"):
        fresh.consume(cost=1.1)


def test_time_budget_uses_monotonic_clock_and_cancellation_wins() -> None:
    now = [100.0]
    budget = ExecutionBudget(BudgetLimits(5, 10, 1.0, 1), clock=lambda: now[0])
    now[0] = 106.0
    with pytest.raises(BudgetExceeded, match="time"):
        budget.check()

    budget.cancel()
    with pytest.raises(ExecutionCancelled, match="cancelled"):
        budget.check()


def test_invalid_limits_and_usage_fail_closed() -> None:
    with pytest.raises(BudgetError, match="max_seconds"):
        BudgetLimits(0, 1, 1.0, 1)
    budget = ExecutionBudget(BudgetLimits(1, 1, 1.0, 1))
    with pytest.raises(BudgetError, match="increments"):
        budget.consume(tokens=-1)
    with pytest.raises(BudgetError, match="increments"):
        budget.consume(cost=-1)


# ---------------------------------------------------------------------------
# AP-007: bounded-stop limits, measurement gaps and the isolation requirement
# ---------------------------------------------------------------------------

# The persisted form of a default budget before AP-007, byte for byte.
_PRE_AP007_DEFAULT_BUDGET = (
    '{"cancel_requested":false,"elapsed_s":0.0,"exhausted_reason":"","pause_requested":false,'
    '"recoveries_used":0,"spec":{"max_elapsed_s":1800.0,"max_recoveries":3,"max_steps":25,'
    '"max_tokens":500000,"max_tool_calls":120},"steps_used":0,"tokens_used":0,"tool_calls_used":0}'
)


def test_ap007_undeclared_limits_leave_stored_budgets_and_their_identity_unchanged() -> None:
    """A budget that declares none of the new limits is stored exactly as before."""
    assert TaskBudgetState(spec=TaskBudgetSpec()).to_json() == _PRE_AP007_DEFAULT_BUDGET
    reread = TaskBudgetState.from_json(_PRE_AP007_DEFAULT_BUDGET)
    assert reread.to_json() == _PRE_AP007_DEFAULT_BUDGET
    assert reread.spec.declares_bounded_limits is False
    assert reread.spec.isolation_required is False
    assert reread.bounded_stop(now=1e12) is None

    # Counters at zero are omitted too, so writing one back does not change the document.
    assert set(json.loads(TaskBudgetState(spec=TaskBudgetSpec()).to_json())) == set(
        json.loads(_PRE_AP007_DEFAULT_BUDGET)
    )
    # Declaring a limit is an authority change: the captured budget identity moves.
    plain = runtime_persistence.compute_task_budget_identity(_PRE_AP007_DEFAULT_BUDGET)
    for declared in (
        TaskBudgetSpec(max_no_progress=3),
        TaskBudgetSpec(max_repair_attempts=0),
        TaskBudgetSpec(deadline_at=2_000_000_000.0),
        TaskBudgetSpec(max_memory_bytes=1),
        TaskBudgetSpec(max_storage_bytes=1),
        TaskBudgetSpec(isolation_required=True),
    ):
        document = TaskBudgetState(spec=declared).to_json()
        assert runtime_persistence.compute_task_budget_identity(document) != plain
        assert TaskBudgetState.from_json(document).spec == declared
    # Usage and measurements are not authority: they leave the identity alone.
    measured = dataclasses.replace(
        TaskBudgetState(spec=TaskBudgetSpec()), no_progress_count=2, memory_bytes_peak=9, unmeasured=("storage",)
    )
    assert runtime_persistence.compute_task_budget_identity(measured.to_json()) == plain
    assert TaskBudgetState.from_json(measured.to_json()) == measured


def test_ap007_bounded_limit_fields_name_exactly_the_new_spec_fields() -> None:
    """The HTTP boundary refuses these keys, so the list must not drift from the dataclass."""
    legacy = {"max_steps", "max_tool_calls", "max_tokens", "max_elapsed_s", "max_recoveries"}
    declared = {field.name for field in dataclasses.fields(TaskBudgetSpec)}
    assert set(execution_budget.BOUNDED_LIMIT_FIELDS) == declared - legacy
    assert len(set(execution_budget.BOUNDED_LIMIT_FIELDS)) == len(execution_budget.BOUNDED_LIMIT_FIELDS)
    full = TaskBudgetSpec(
        max_no_progress=1,
        max_repair_attempts=1,
        deadline_at=10.0,
        max_memory_bytes=1,
        max_storage_bytes=1,
        isolation_required=True,
    )
    assert set(full.to_dict()) == declared


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_no_progress", -1),
        ("max_no_progress", True),
        ("max_no_progress", "3"),
        ("max_no_progress", 1.5),
        ("max_repair_attempts", -1),
        ("max_repair_attempts", False),
        ("deadline_at", 0),
        ("deadline_at", -5.0),
        ("deadline_at", float("nan")),
        ("deadline_at", float("inf")),
        ("deadline_at", "tomorrow"),
        ("deadline_at", True),
        ("max_memory_bytes", 0),
        ("max_memory_bytes", "1GB"),
        ("max_storage_bytes", 0),
        ("max_storage_bytes", 2.5),
        ("isolation_required", 1),
        ("isolation_required", "true"),
        ("isolation_required", None),
    ],
)
def test_ap007_malformed_limits_fail_closed(field: str, bad: Any) -> None:
    """A limit that cannot be read is an error, never a silently absent limit."""
    with pytest.raises(BudgetError, match=field):
        TaskBudgetSpec(**{field: bad})
    with pytest.raises(BudgetError, match=field):
        TaskBudgetSpec.from_dict({field: bad})
    # The same value in a stored document makes the whole budget unreadable.
    with pytest.raises(BudgetError, match=field):
        TaskBudgetState.from_json(json.dumps({"spec": {"max_steps": 5, field: bad}}))


def test_ap007_malformed_counters_and_markers_fail_closed() -> None:
    for document in (
        {"no_progress_count": -1},
        {"repair_attempts_used": "2"},
        {"memory_bytes_peak": True},
        {"storage_bytes_peak": 1.5},
        {"unmeasured": "memory"},
        {"unmeasured": ["cpu"]},
        {"unmeasured": {"memory": True}},
    ):
        with pytest.raises(BudgetError):
            TaskBudgetState.from_json(json.dumps({"spec": {"max_steps": 5}, **document}))
    assert TaskBudgetState.from_json(
        json.dumps({"spec": {"max_steps": 5}, "unmeasured": ["storage", "memory", "storage"]})
    ).unmeasured == ("memory", "storage")


def test_ap007_bounded_stop_boundaries_are_exact() -> None:
    """At the limit work continues; one past it the stop is required, with a stable reason."""
    spec = TaskBudgetSpec(
        max_no_progress=2,
        max_repair_attempts=1,
        deadline_at=1_000.0,
        max_memory_bytes=100,
        max_storage_bytes=200,
    )
    at_limit = TaskBudgetState(
        spec=spec, no_progress_count=2, repair_attempts_used=1, memory_bytes_peak=100, storage_bytes_peak=200
    )
    assert at_limit.bounded_stop(now=999.99) is None
    assert at_limit.violation_reason(now=999.99) == ""

    def _code(**changed: Any) -> str:
        found = dataclasses.replace(at_limit, **changed).bounded_stop(now=999.99)
        assert found is not None
        return found[0]

    assert _code(no_progress_count=3) == "no_progress"
    assert _code(repair_attempts_used=2) == "repair_attempts"
    assert _code(memory_bytes_peak=101) == "memory"
    assert _code(storage_bytes_peak=201) == "storage"
    assert _code(unmeasured=("memory",)) == "measurement_unavailable"
    assert _code(unmeasured=("storage",)) == "measurement_unavailable"
    assert {_code(no_progress_count=3), _code(unmeasured=("memory",))} <= set(execution_budget.BOUNDED_STOP_CODES)

    # The deadline is reached at the instant itself, and its reason does not depend on "now".
    on_time = at_limit.bounded_stop(now=1_000.0)
    much_later = at_limit.bounded_stop(now=9_999.0)
    assert on_time == much_later == ("deadline", "deadline exceeded (deadline_at=1000.00)")
    assert at_limit.violation_reason(now=1_000.0) == "deadline exceeded (deadline_at=1000.00)"
    # With no time given the wall clock is used, so a forgetful caller still enforces it.
    assert at_limit.bounded_stop() is not None

    # A zero limit allows none: the first occurrence stops.
    strict = TaskBudgetState(spec=TaskBudgetSpec(max_no_progress=0, max_repair_attempts=0))
    assert strict.bounded_stop(now=1.0) is None
    assert dataclasses.replace(strict, no_progress_count=1).bounded_stop(now=1.0) is not None
    assert dataclasses.replace(strict, repair_attempts_used=1).bounded_stop(now=1.0) is not None

    # A marker for a ceiling that is not declared is not a gap.
    undeclared = TaskBudgetState(spec=TaskBudgetSpec(max_memory_bytes=100), unmeasured=("storage",))
    assert undeclared.measurement_gaps == ()
    assert undeclared.bounded_stop(now=1.0) is None


def test_ap007_isolation_needs_literal_proof_and_has_no_host_fallback() -> None:
    proven = IsolationEvidence(execution_mode="sandbox", admitted=True, qualified=True, backend_id="fixture-backend")
    assert isolation_refusal(proven) == ""

    refusals = {
        "nothing presented": isolation_refusal(None),
        "a mapping instead of evidence": isolation_refusal({"qualified": True}),  # type: ignore[arg-type]
        "host mode": isolation_refusal(dataclasses.replace(proven, execution_mode="host")),
        "disabled mode": isolation_refusal(dataclasses.replace(proven, execution_mode="disabled")),
        "no mode": isolation_refusal(dataclasses.replace(proven, execution_mode="")),
        "admission refused": isolation_refusal(dataclasses.replace(proven, admitted=False, error_code="incident")),
        "not qualified": isolation_refusal(dataclasses.replace(proven, qualified=False)),
        "no backend named": isolation_refusal(dataclasses.replace(proven, backend_id="  ")),
        "truthy but not True": isolation_refusal(
            dataclasses.replace(proven, admitted=1, qualified="yes")  # type: ignore[arg-type]
        ),
    }
    for case, refusal in refusals.items():
        assert refusal.startswith("runtime isolation is required"), case
    assert "`host`" in refusals["host mode"]
    assert "incident" in refusals["admission refused"]

    # Admission output is adapted strictly: "allowed" alone never proves isolation.
    allowed_without_backend = SimpleNamespace(allowed=True, container_qualified=False, active_backend=None)
    adapted = IsolationEvidence.from_admission(allowed_without_backend, execution_mode="SANDBOX")
    assert (adapted.execution_mode, adapted.admitted, adapted.qualified, adapted.backend_id) == (
        "sandbox",
        True,
        False,
        "",
    )
    assert isolation_refusal(adapted)
    truthy = SimpleNamespace(allowed=1, container_qualified="yes", active_backend=SimpleNamespace(backend_id="b1"))
    assert isolation_refusal(IsolationEvidence.from_admission(truthy, execution_mode="sandbox"))
    assert isolation_refusal(IsolationEvidence.from_admission(object(), execution_mode="sandbox"))
    qualified = SimpleNamespace(
        allowed=True, container_qualified=True, active_backend=SimpleNamespace(backend_id="fixture-backend")
    )
    assert isolation_refusal(IsolationEvidence.from_admission(qualified, execution_mode="sandbox")) == ""
    assert isolation_refusal(IsolationEvidence.from_admission(qualified, execution_mode="host"))
    # The evidence record names no secret and no reason text when it is serialised.
    assert set(adapted.to_dict()) == {"execution_mode", "admitted", "qualified", "backend_id", "error_code"}


def test_ap007_budget_documents_are_never_rebuilt_field_by_field() -> None:
    """Every update copies the stored budget, so a counter added later cannot be dropped.

    A fresh budget may be built from a spec alone.  Rebuilding an existing one by
    listing its fields is the mistake this guards: it silently resets whatever the
    list forgot, including the counters that decide a bounded stop.
    """
    offenders: list[str] = []
    for name in ("execution_budget.py", "workflow_state.py", "app.py", "child_delegation.py"):
        tree = ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))
        parser_calls: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TaskBudgetState":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "from_json":
                        parser_calls = {id(call) for call in ast.walk(item) if isinstance(call, ast.Call)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in parser_calls:
                continue
            target = node.func
            called = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if called != "TaskBudgetState":
                continue
            if node.args or {keyword.arg for keyword in node.keywords} - {"spec"}:
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == []
