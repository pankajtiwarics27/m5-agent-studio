from __future__ import annotations

import asyncio

import pytest

from failure_injection import (
    ActionBoundaryFaultInjector,
    ActionBoundaryFaultSpec,
    FailureInjectionError,
    FailureScenario,
    InjectedBoundaryCrashError,
    run_failure_scenario,
    run_failure_suite,
)


async def _inject(scenario: FailureScenario) -> None:
    if scenario.kind == "crash":
        raise RuntimeError("injected crash detail")
    if scenario.kind == "provider_loss":
        raise ConnectionError("private provider detail")
    if scenario.kind == "disconnect":
        raise asyncio.CancelledError
    await asyncio.sleep(60)


def test_failure_suite_classifies_crash_timeout_provider_loss_and_disconnect() -> None:
    scenarios = tuple(
        FailureScenario(name, kind)
        for name, kind in (
            ("crash", "crash"),
            ("timeout", "timeout"),
            ("provider", "provider_loss"),
            ("disconnect", "disconnect"),
        )
    )
    outcomes = asyncio.run(run_failure_suite(scenarios, _inject))
    assert all(outcome.observed and outcome.recovered for outcome in outcomes)
    assert [outcome.kind for outcome in outcomes] == ["crash", "timeout", "provider_loss", "disconnect"]
    assert "private provider detail" not in " ".join(outcome.detail for outcome in outcomes)


def test_unexpected_failure_kind_is_not_reported_as_recovered() -> None:
    async def provider_failure(_scenario: FailureScenario) -> None:
        raise ConnectionError("provider")

    outcome = asyncio.run(run_failure_scenario(FailureScenario("wrong-kind", "crash"), provider_failure))
    assert outcome.observed is False
    assert outcome.recovered is False


def test_invalid_scenario_and_empty_suite_fail_closed() -> None:
    with pytest.raises(FailureInjectionError, match="unknown failure kind"):
        FailureScenario("bad", "unknown")  # type: ignore[arg-type]
    with pytest.raises(FailureInjectionError, match="must not be empty"):
        asyncio.run(run_failure_suite((), _inject))


def test_action_boundary_fault_injector_matches_filters_and_auto_disarms() -> None:
    injector = ActionBoundaryFaultInjector()
    assert injector.is_armed is False
    injector.arm(
        ActionBoundaryFaultSpec(
            boundary="after_effect_before_result_commit",
            tool_name="write_file",
            task_id="task-1",
            step_id=2,
            max_fires=1,
            detail="simulated crash before commit",
        )
    )
    assert injector.is_armed is True

    # Non-matching boundary/tool/step does not fire.
    injector.check_boundary("before_intent", tool_name="write_file", task_id="task-1", step_id=2)
    injector.check_boundary("after_effect_before_result_commit", tool_name="read_file", task_id="task-1", step_id=2)
    injector.check_boundary("after_effect_before_result_commit", tool_name="write_file", task_id="task-1", step_id=1)

    with pytest.raises(InjectedBoundaryCrashError) as exc_info:
        injector.check_boundary(
            "after_effect_before_result_commit",
            tool_name="write_file",
            action_id="act-42",
            task_id="task-1",
            step_id=2,
        )
    assert exc_info.value.boundary == "after_effect_before_result_commit"
    assert exc_info.value.action_id == "act-42"
    assert injector.is_armed is False
    assert len(injector.events()) == 1
    assert injector.events()[0].action_id == "act-42"

    # Subsequent calls do not fire once max_fires=1 was consumed.
    injector.check_boundary(
        "after_effect_before_result_commit",
        tool_name="write_file",
        action_id="act-42",
        task_id="task-1",
        step_id=2,
    )


def test_action_boundary_fault_spec_validation_fails_closed() -> None:
    with pytest.raises(FailureInjectionError, match="unknown action boundary"):
        ActionBoundaryFaultSpec(boundary="invalid_boundary")  # type: ignore[arg-type]
    with pytest.raises(FailureInjectionError, match="max_fires"):
        ActionBoundaryFaultSpec(boundary="before_intent", max_fires=0)
    with pytest.raises(FailureInjectionError, match="NUL"):
        ActionBoundaryFaultSpec(boundary="before_intent", tool_name="bad\x00tool")

