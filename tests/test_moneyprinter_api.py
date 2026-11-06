from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

import app as studio
from capability_negotiation import TaskRequirements
from execution_budget import BudgetLimits, ExecutionBudget, ExecutionCancelled
from local_provider_adapters import MONEYPRINTER_GATEWAY_CAPABILITIES, MoneyPrinterAPIAdapter
from model_discovery import discover_models, gateway_capabilities


def _transport_for(payload: Mapping[str, Any], path: str = "/v1/models", status: int = 200):
    calls: list[tuple[str, str, float]] = []

    async def transport(method: str, requested_path: str, timeout: float) -> tuple[int, Mapping[str, Any]]:
        calls.append((method, requested_path, timeout))
        assert requested_path == path
        return status, payload

    return transport, calls


def _payload(*ids: str, capabilities: list[str] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": ids[0] if ids else "moneyprinter-gateway",
        "digest": "sha256:moneyprinter-fixture",
        "context_length": 8192,
    }
    if capabilities is not None:
        record["capabilities"] = capabilities
    return {"data": [record]}


# --------------------------------------------------------------------------- provider profile


def test_moneyprinterapi_preset_targets_localhost_8080_openai_wire() -> None:
    preset = studio.PROVIDER_PRESETS["moneyprinterapi"]
    assert preset["name"] == "MoneyPrinterAPI"
    assert preset["wire"] == "openai"
    assert preset["tier"] == "gateway"
    assert preset["base_url"] == "http://localhost:8080/v1"
    assert preset["requires_key"] is False
    assert preset["env_key"] == "MONEYPRINTER_API_KEY"
    assert preset["default_models"]


def test_builtin_local_gateway_pin_matches_preset_exactly() -> None:
    assert studio._is_builtin_local_gateway("omniroute", "http://127.0.0.1:20128/v1") is True
    assert studio._is_builtin_local_gateway("moneyprinterapi", "http://localhost:8080/v1") is True
    assert studio._is_builtin_local_gateway("moneyprinterapi", "http://localhost:8080/v1/") is True
    assert studio._is_builtin_local_gateway("moneyprinterapi", "http://localhost:9999/v1") is False
    assert studio._is_builtin_local_gateway("moneyprinterapi", "https://evil.example.test/v1") is False
    assert studio._is_builtin_local_gateway("moneyprinterapi", "http://localhost:8080/v1/../../admin") is False
    assert studio._is_builtin_local_gateway("colibri", "http://127.0.0.1:8001/v1") is False
    assert studio._is_builtin_local_gateway("unknown", "http://localhost:8080/v1") is False
    assert studio._is_builtin_local_gateway(None, "http://localhost:8080/v1") is False


# --------------------------------------------------------------------------- adapter discovery


def test_adapter_discovers_models_with_gateway_capabilities() -> None:
    transport, calls = _transport_for(_payload("qwen-script-gateway"))
    snapshot = asyncio.run(MoneyPrinterAPIAdapter(transport).discover())

    assert snapshot.available is True
    assert snapshot.runtime == "moneyprinter"
    descriptor = snapshot.models[0]
    assert descriptor.source == "moneyprinter"
    assert descriptor.capabilities == tuple(sorted(MONEYPRINTER_GATEWAY_CAPABILITIES))
    assert calls == [("GET", "/v1/models", 5.0)]


def test_adapter_endpoint_capabilities_replace_gateway_defaults() -> None:
    transport, _ = _transport_for(_payload("moneyprinter-gateway", capabilities=["chat", "streaming"]))
    snapshot = asyncio.run(MoneyPrinterAPIAdapter(transport).discover())

    assert snapshot.available is True
    assert snapshot.models[0].capabilities == ("chat", "streaming")


def test_unavailable_gateway_fails_closed_without_leaking_details() -> None:
    async def down(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        raise OSError("secret dial detail with credentials")

    snapshot = asyncio.run(MoneyPrinterAPIAdapter(down).discover())

    assert snapshot.available is False
    assert snapshot.models == ()
    assert snapshot.error == "local runtime unavailable"


def test_discover_models_merges_gateway_capabilities_into_descriptors() -> None:
    transport, _ = _transport_for(_payload("moneyprinter-gateway", capabilities=["chat"]))
    catalog = asyncio.run(discover_models([MoneyPrinterAPIAdapter(transport)]))

    descriptor = catalog.find("moneyprinter-gateway", runtime="moneyprinter")[0]
    assert set(descriptor.capabilities) == {"chat", *MONEYPRINTER_GATEWAY_CAPABILITIES}
    mapping = catalog.to_mapping()
    assert set(mapping) == {"descriptors", "runtimes", "conflicts", "ambiguous_model_ids"}
    assert set(mapping["descriptors"][0]["capabilities"]) == {"chat", *MONEYPRINTER_GATEWAY_CAPABILITIES}


def test_gateway_capability_helper_and_negotiation() -> None:
    assert gateway_capabilities("moneyprinter") == MONEYPRINTER_GATEWAY_CAPABILITIES
    assert gateway_capabilities("ollama") == ()

    transport, _ = _transport_for(_payload("moneyprinter-gateway"))
    adapter = MoneyPrinterAPIAdapter(transport)
    snapshot = asyncio.run(adapter.discover())
    decisions = adapter.negotiate(snapshot, TaskRequirements(required_capabilities=("script-generation",)))
    assert decisions[0].feasible is True


# --------------------------------------------------------------------------- execution budget


def test_budget_exhaustion_fails_closed_before_transport() -> None:
    transport_called = False

    async def transport(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        nonlocal transport_called
        transport_called = True
        return 200, _payload("moneyprinter-gateway")

    budget = ExecutionBudget(BudgetLimits(max_seconds=60.0, max_tokens=0, max_cost=0.0, max_tools=0))
    adapter = MoneyPrinterAPIAdapter(transport, budget=budget)
    snapshot = asyncio.run(adapter.discover())

    assert snapshot.available is False
    assert snapshot.error == "execution budget exceeded"
    assert transport_called is False


def test_budget_tool_units_are_consumed_per_remote_call() -> None:
    transport, _ = _transport_for(_payload("moneyprinter-gateway"))
    budget = ExecutionBudget(BudgetLimits(max_seconds=60.0, max_tokens=0, max_cost=0.0, max_tools=5))
    adapter = MoneyPrinterAPIAdapter(transport, budget=budget)

    snapshot = asyncio.run(adapter.discover())

    assert snapshot.available is True
    assert budget.usage().tools == 1


def test_budget_time_exhaustion_fails_closed() -> None:
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    transport, _ = _transport_for(_payload("moneyprinter-gateway"))
    budget = ExecutionBudget(
        BudgetLimits(max_seconds=5.0, max_tokens=0, max_cost=0.0, max_tools=5),
        clock=lambda: next(ticks),
    )
    adapter = MoneyPrinterAPIAdapter(transport, budget=budget)
    snapshot = asyncio.run(adapter.discover())

    assert snapshot.available is False
    assert snapshot.error == "execution budget exceeded"


def test_cancelled_budget_propagates_from_adapter_and_fanout() -> None:
    transport, _ = _transport_for(_payload("moneyprinter-gateway"))
    budget = ExecutionBudget(BudgetLimits(max_seconds=60.0, max_tokens=0, max_cost=0.0, max_tools=5))
    budget.cancel()
    adapter = MoneyPrinterAPIAdapter(transport, budget=budget)

    with pytest.raises(ExecutionCancelled):
        asyncio.run(adapter.discover())
    with pytest.raises(ExecutionCancelled):
        asyncio.run(discover_models([adapter]))


def test_budget_rejects_non_budget_values() -> None:
    transport, _ = _transport_for(_payload("moneyprinter-gateway"))
    with pytest.raises(TypeError):
        MoneyPrinterAPIAdapter(transport, budget="not-a-budget")  # type: ignore[arg-type]
