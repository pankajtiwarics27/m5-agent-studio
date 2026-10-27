"""Time, token, cost, tool, cancellation, and retry execution budgets."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class BudgetError(ValueError):
    """Raised when a budget or usage value is invalid."""


class BudgetExceeded(RuntimeError):
    """Raised when an operation would exceed an execution budget."""


class ExecutionCancelled(RuntimeError):
    """Raised when cancellation has been requested."""


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_seconds: float
    max_tokens: int
    max_cost: float
    max_tools: int
    max_retries: int = 0

    def __post_init__(self) -> None:
        if self.max_seconds <= 0 or self.max_tokens < 0 or self.max_cost < 0 or self.max_tools < 0 or self.max_retries < 0:
            raise BudgetError("budget limits must be non-negative, with max_seconds positive.")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    elapsed_seconds: float
    tokens: int
    cost: float
    tools: int
    retries: int


class ExecutionBudget:
    """Thread-safe budget guard with monotonic time and explicit cancellation."""

    def __init__(self, limits: BudgetLimits, *, clock: callable = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._tokens = 0
        self._cost = 0.0
        self._tools = 0
        self._retries = 0
        self._cancelled = False
        self._lock = threading.RLock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def _usage(self) -> BudgetUsage:
        return BudgetUsage(self._clock() - self._started, self._tokens, self._cost, self._tools, self._retries)

    def usage(self) -> BudgetUsage:
        with self._lock:
            return self._usage()

    def check(self) -> BudgetUsage:
        with self._lock:
            usage = self._usage()
            if self._cancelled:
                raise ExecutionCancelled("execution was cancelled.")
            if usage.elapsed_seconds > self.limits.max_seconds:
                raise BudgetExceeded("time budget exceeded.")
            if usage.tokens > self.limits.max_tokens:
                raise BudgetExceeded("token budget exceeded.")
            if usage.cost > self.limits.max_cost:
                raise BudgetExceeded("cost budget exceeded.")
            if usage.tools > self.limits.max_tools:
                raise BudgetExceeded("tool budget exceeded.")
            if usage.retries > self.limits.max_retries:
                raise BudgetExceeded("retry budget exceeded.")
            return usage

    def consume(self, *, tokens: int = 0, cost: float = 0.0, tools: int = 0) -> BudgetUsage:
        if isinstance(tokens, bool) or tokens < 0 or isinstance(tools, bool) or tools < 0 or cost < 0:
            raise BudgetError("usage increments must be non-negative.")
        with self._lock:
            self._tokens += tokens
            self._cost += cost
            self._tools += tools
            return self.check()

    def retry(self) -> BudgetUsage:
        with self._lock:
            self._retries += 1
            return self.check()

    def allow_retry(self) -> bool:
        try:
            self.retry()
        except BudgetExceeded:
            return False
        return True
