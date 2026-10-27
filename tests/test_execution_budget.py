from __future__ import annotations

import pytest

from execution_budget import (
    BudgetError,
    BudgetExceeded,
    BudgetLimits,
    ExecutionBudget,
    ExecutionCancelled,
)


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
