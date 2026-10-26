from __future__ import annotations

import json

import pytest

from capability_negotiation import TaskRequirements
from hardware_capabilities import AcceleratorCapability, HardwareProfile
from model_diagnostics import diagnose_models
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance


def _model(model_id: str, capabilities: tuple[str, ...], memory: int = 100) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=f"sha256:{model_id}",
        source="fixture",
        revision="r1",
        runtime="runtime",
        capabilities=capabilities,
        limits=ModelLimits(context_tokens=4096, memory_bytes=memory),
        supports_tools="tools" in capabilities,
        supports_schema="schema" in capabilities,
        provenance=ModelProvenance("fixture", "fixture://model", "verified"),
    )


def test_diagnostics_show_feasible_models_reasons_and_tradeoffs() -> None:
    hardware = HardwareProfile("OS", "arch", accelerators=(AcceleratorCapability("gpu", capabilities=("tensor",)),), available_memory_bytes=200)
    task = TaskRequirements(required_capabilities=("vision",), required_hardware_capabilities=("tensor",))
    report = diagnose_models((_model("rejected", ("text",)), _model("feasible", ("text", "vision"))), task, hardware)

    assert [item.model_id for item in report.models] == ["feasible", "rejected"]
    assert [item.model_id for item in report.feasible_models] == ["feasible"]
    rejected = report.models[1]
    assert rejected.feasible is False
    assert "missing model capabilities: vision" in rejected.reasons
    assert "runtime=runtime" in rejected.tradeoffs
    assert "memory=100 bytes" in rejected.tradeoffs
    assert json.loads(json.dumps(report.to_mapping()))["feasible_models"][0]["model_id"] == "feasible"


def test_diagnostics_include_hardware_rejection_and_preserve_alternatives() -> None:
    task = TaskRequirements(required_hardware_capabilities=("npu",))
    report = diagnose_models((_model("model", ("text",)),), task, HardwareProfile("OS", "arch"))
    assert report.models[0].feasible is False
    assert report.models[0].reasons == ("missing hardware capabilities: npu",)


def test_diagnostics_reject_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="models"):
        diagnose_models(("not-a-model",), TaskRequirements())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="task"):
        diagnose_models((), "not-a-task")  # type: ignore[arg-type]
