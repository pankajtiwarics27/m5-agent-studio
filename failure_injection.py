"""Deterministic failure-injection harness for workflow resilience tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

FailureKind = Literal["crash", "timeout", "provider_loss", "disconnect"]
_KINDS = frozenset({"crash", "timeout", "provider_loss", "disconnect"})


class FailureInjectionError(ValueError):
    """Raised when a failure scenario is malformed."""


@dataclass(frozen=True, slots=True)
class FailureScenario:
    name: str
    kind: FailureKind
    timeout_seconds: float = 0.05

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or "\x00" in self.name:
            raise FailureInjectionError("scenario name must be non-empty and contain no NUL.")
        if self.kind not in _KINDS:
            raise FailureInjectionError(f"unknown failure kind: {self.kind}")
        if self.timeout_seconds <= 0:
            raise FailureInjectionError("timeout_seconds must be positive.")


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    name: str
    kind: FailureKind
    observed: bool
    recovered: bool
    detail: str


AsyncOperation = Callable[[FailureScenario], Awaitable[None]]


async def run_failure_scenario(scenario: FailureScenario, operation: AsyncOperation) -> FailureOutcome:
    """Run one injected scenario and classify only expected failure boundaries."""
    try:
        await asyncio.wait_for(operation(scenario), timeout=scenario.timeout_seconds)
    except TimeoutError:
        if scenario.kind != "timeout":
            return FailureOutcome(scenario.name, scenario.kind, False, False, "unexpected timeout")
        return FailureOutcome(scenario.name, scenario.kind, True, True, "operation cancelled at timeout boundary")
    except asyncio.CancelledError:
        if scenario.kind != "disconnect":
            raise
        return FailureOutcome(scenario.name, scenario.kind, True, True, "disconnect cancellation propagated")
    except ConnectionError:
        if scenario.kind != "provider_loss":
            return FailureOutcome(scenario.name, scenario.kind, False, False, "unexpected provider loss")
        return FailureOutcome(scenario.name, scenario.kind, True, True, "provider loss classified for retry/recovery")
    except RuntimeError:
        if scenario.kind != "crash":
            return FailureOutcome(scenario.name, scenario.kind, False, False, "unexpected runtime failure")
        return FailureOutcome(scenario.name, scenario.kind, True, True, "crash isolated without process termination")
    else:
        return FailureOutcome(scenario.name, scenario.kind, False, False, "failure was not observed")


async def run_failure_suite(scenarios: Sequence[FailureScenario], operation: AsyncOperation) -> tuple[FailureOutcome, ...]:
    """Run an ordered suite and return one auditable outcome per scenario."""
    if not scenarios:
        raise FailureInjectionError("scenarios must not be empty.")
    outcomes = [await run_failure_scenario(scenario, operation) for scenario in scenarios]
    return tuple(outcomes)
