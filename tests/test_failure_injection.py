from __future__ import annotations

import asyncio

import pytest

from failure_injection import (
    FailureInjectionError,
    FailureScenario,
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
