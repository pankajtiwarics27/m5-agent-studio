from __future__ import annotations

import pytest

from hardware_validation import GIB, HardwareValidationMatrix, ValidationTask


def test_default_matrix_completes_representative_tasks_for_all_tiers() -> None:
    calls: list[tuple[int, str]] = []

    def execute(profile, task) -> None:
        calls.append((profile.physical_memory_bytes // GIB, task.name))
        assert task.estimated_memory_bytes <= profile.available_memory_bytes

    summary = HardwareValidationMatrix().run(execute)

    assert summary.all_passed is True
    assert summary.passed == 9
    assert summary.failed == 0
    assert summary.skipped == 0
    assert sorted({tier for tier, _task in calls}) == [16, 24, 48]


def test_capacity_preflight_skips_oom_prone_task_without_calling_executor() -> None:
    calls: list[str] = []
    matrix = HardwareValidationMatrix((16,), (ValidationTask("fits", GIB), ValidationTask("too-large", 17 * GIB)))
    summary = matrix.run(lambda _profile, task: calls.append(task.name))

    assert calls == ["fits"]
    assert summary.passed == 1
    assert summary.skipped == 1
    assert summary.outcomes[1].reason.startswith("preflight rejected")


def test_executor_oom_and_other_failures_are_reported() -> None:
    def execute(_profile, task) -> None:
        if task.name == "oom":
            raise MemoryError
        raise RuntimeError("private failure detail")

    summary = HardwareValidationMatrix((16,), (ValidationTask("oom", GIB), ValidationTask("error", GIB))).run(execute)
    assert summary.failed == 2
    assert summary.outcomes[0].reason == "executor reported out of memory"
    assert summary.outcomes[1].reason == "executor failed"
    assert "private failure detail" not in " ".join(outcome.reason for outcome in summary.outcomes)


def test_matrix_rejects_invalid_definitions() -> None:
    with pytest.raises(ValueError, match="memory_tiers"):
        HardwareValidationMatrix((0,))
    with pytest.raises(ValueError, match="estimated_memory"):
        ValidationTask("invalid", 0)
    with pytest.raises(ValueError, match="tasks"):
        HardwareValidationMatrix((16,), ())
