"""Reproducible hardware-tier validation matrix with OOM preflight."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from hardware_capabilities import HardwareProfile

GIB = 1024**3
ValidationStatus = Literal["passed", "failed", "skipped_capacity"]


@dataclass(frozen=True, slots=True)
class ValidationTask:
    name: str
    estimated_memory_bytes: int

    def __post_init__(self) -> None:
        if not self.name.strip() or "\x00" in self.name:
            raise ValueError("task name must be non-empty and contain no NUL.")
        if isinstance(self.estimated_memory_bytes, bool) or self.estimated_memory_bytes <= 0:
            raise ValueError("estimated_memory_bytes must be positive.")


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    memory_gib: int
    task: str
    status: ValidationStatus
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    outcomes: tuple[ValidationOutcome, ...]

    @property
    def passed(self) -> int:
        return sum(outcome.status == "passed" for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(outcome.status == "failed" for outcome in self.outcomes)

    @property
    def skipped(self) -> int:
        return sum(outcome.status == "skipped_capacity" for outcome in self.outcomes)

    @property
    def all_passed(self) -> bool:
        return bool(self.outcomes) and self.failed == 0 and self.skipped == 0


Executor = Callable[[HardwareProfile, ValidationTask], None]


class HardwareValidationMatrix:
    """Run the same representative tasks against generic memory-tier profiles."""

    def __init__(
        self,
        memory_tiers_gib: Sequence[int] = (16, 24, 48),
        tasks: Sequence[ValidationTask] = (
            ValidationTask("short-context", 1 * GIB),
            ValidationTask("tool-context", 4 * GIB),
            ValidationTask("long-context", 8 * GIB),
        ),
    ) -> None:
        if not memory_tiers_gib or any(isinstance(tier, bool) or tier <= 0 for tier in memory_tiers_gib):
            raise ValueError("memory_tiers_gib must contain positive integers.")
        if not tasks:
            raise ValueError("tasks must not be empty.")
        self.memory_tiers_gib = tuple(memory_tiers_gib)
        self.tasks = tuple(tasks)

    def run(self, execute: Executor) -> ValidationSummary:
        """Execute only tasks that fit the tier; classify runtime failures safely."""
        outcomes: list[ValidationOutcome] = []
        for memory_gib in self.memory_tiers_gib:
            profile = HardwareProfile(
                operating_system="validation",
                architecture="generic",
                physical_memory_bytes=memory_gib * GIB,
                available_memory_bytes=memory_gib * GIB,
            )
            available_memory = profile.available_memory_bytes
            for task in self.tasks:
                if available_memory is not None and task.estimated_memory_bytes > available_memory:
                    outcomes.append(
                        ValidationOutcome(
                            memory_gib,
                            task.name,
                            "skipped_capacity",
                            "preflight rejected task above available memory",
                        )
                    )
                    continue
                try:
                    execute(profile, task)
                except MemoryError:
                    outcomes.append(ValidationOutcome(memory_gib, task.name, "failed", "executor reported out of memory"))
                except Exception:  # noqa: BLE001 - validation must classify arbitrary executor failures safely
                    outcomes.append(ValidationOutcome(memory_gib, task.name, "failed", "executor failed"))
                else:
                    outcomes.append(ValidationOutcome(memory_gib, task.name, "passed"))
        return ValidationSummary(tuple(outcomes))
