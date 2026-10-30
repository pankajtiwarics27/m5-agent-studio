"""Deterministic failure-injection harness for workflow and action-boundary resilience tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import threading
import time
import uuid
from typing import Literal

FailureKind = Literal["crash", "timeout", "provider_loss", "disconnect"]
_KINDS = frozenset({"crash", "timeout", "provider_loss", "disconnect"})

ActionBoundary = Literal[
    "before_intent",
    "after_intent_before_effect",
    "after_effect_before_result_commit",
    "before_task_completion",
]
ACTION_BOUNDARIES: frozenset[str] = frozenset(
    {
        "before_intent",
        "after_intent_before_effect",
        "after_effect_before_result_commit",
        "before_task_completion",
    }
)


class FailureInjectionError(ValueError):
    """Raised when a failure scenario or boundary fault specification is malformed."""


class InjectedBoundaryCrashError(RuntimeError):
    """Hard-crash fault injected at a deterministic execution boundary.

    Explicitly re-raised past ``except Exception:`` soft-failure handlers in
    ``execution_spine`` and ``app.py`` so durable ``in_flight`` ledger rows and
    active worker leases remain in authentic post-SIGKILL state while ASGI test
    portals remain healthy.
    """

    def __init__(
        self,
        detail: str,
        *,
        boundary: ActionBoundary,
        tool_name: str = "",
        action_id: str = "",
        task_id: str = "",
        step_id: str = "",
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.boundary: ActionBoundary = boundary
        self.tool_name = tool_name
        self.action_id = action_id
        self.task_id = task_id
        self.step_id = step_id


@dataclass(frozen=True, slots=True)
class ActionBoundaryFaultSpec:
    """Deterministic specification for injecting crashes at an action boundary."""

    boundary: ActionBoundary
    tool_name: str = ""
    task_id: str = ""
    step_id: str | int | None = None
    max_fires: int = 1
    detail: str = "injected action-boundary crash"

    def __post_init__(self) -> None:
        if self.boundary not in ACTION_BOUNDARIES:
            raise FailureInjectionError(f"unknown action boundary: {self.boundary!r}")
        if "\x00" in self.tool_name or "\x00" in self.task_id or "\x00" in self.detail:
            raise FailureInjectionError("boundary fault text fields must not contain NUL.")
        if isinstance(self.max_fires, bool) or not isinstance(self.max_fires, int) or self.max_fires < 1:
            raise FailureInjectionError("max_fires must be an integer >= 1.")


@dataclass(frozen=True, slots=True)
class ActionBoundaryTriggerEvent:
    """Auditable record of a fired action-boundary fault."""

    rule_id: str
    boundary: ActionBoundary
    tool_name: str
    action_id: str
    task_id: str
    step_id: str
    fired_at: float
    detail: str


class _ArmedBoundaryRule:
    __slots__ = ("rule_id", "spec", "fires_count")

    def __init__(self, rule_id: str, spec: ActionBoundaryFaultSpec) -> None:
        self.rule_id = rule_id
        self.spec = spec
        self.fires_count = 0


class ActionBoundaryFaultInjector:
    """Thread-safe deterministic injector for execution-spine and completion boundaries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[_ArmedBoundaryRule] = []
        self._events: list[ActionBoundaryTriggerEvent] = []

    @property
    def is_armed(self) -> bool:
        return bool(self._rules)

    def arm(self, spec: ActionBoundaryFaultSpec) -> str:
        rule_id = f"fault-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._rules.append(_ArmedBoundaryRule(rule_id, spec))
        return rule_id

    def disarm(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.rule_id != rule_id]
            return len(self._rules) < before

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()
            self._events.clear()

    def events(self) -> tuple[ActionBoundaryTriggerEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def check_boundary(
        self,
        boundary: ActionBoundary,
        *,
        tool_name: str = "",
        action_id: str = "",
        task_id: str = "",
        step_id: str | int | None = None,
    ) -> None:
        if not self._rules:
            return
        step_str = "" if step_id is None else str(step_id)
        matched_spec: ActionBoundaryFaultSpec | None = None
        with self._lock:
            for idx, rule in enumerate(self._rules):
                spec = rule.spec
                if spec.boundary != boundary:
                    continue
                if spec.tool_name and spec.tool_name != tool_name:
                    continue
                if spec.task_id and spec.task_id != task_id:
                    continue
                if spec.step_id is not None and str(spec.step_id) != step_str:
                    continue
                rule.fires_count += 1
                matched_spec = spec
                event = ActionBoundaryTriggerEvent(
                    rule_id=rule.rule_id,
                    boundary=boundary,
                    tool_name=tool_name,
                    action_id=action_id,
                    task_id=task_id,
                    step_id=step_str,
                    fired_at=time.time(),
                    detail=spec.detail,
                )
                self._events.append(event)
                if rule.fires_count >= spec.max_fires:
                    self._rules.pop(idx)
                break
        if matched_spec is not None:
            raise InjectedBoundaryCrashError(
                matched_spec.detail,
                boundary=boundary,
                tool_name=tool_name,
                action_id=action_id,
                task_id=task_id,
                step_id=step_str,
            )


DEFAULT_BOUNDARY_INJECTOR = ActionBoundaryFaultInjector()


def check_action_boundary(
    boundary: ActionBoundary,
    *,
    tool_name: str = "",
    action_id: str = "",
    task_id: str = "",
    step_id: str | int | None = None,
    injector: ActionBoundaryFaultInjector | None = None,
) -> None:
    """Trigger any matching armed crash fault on ``injector`` or ``DEFAULT_BOUNDARY_INJECTOR``."""
    target = DEFAULT_BOUNDARY_INJECTOR if injector is None else injector
    if not target.is_armed:
        return
    target.check_boundary(
        boundary,
        tool_name=tool_name,
        action_id=action_id,
        task_id=task_id,
        step_id=step_id,
    )


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
