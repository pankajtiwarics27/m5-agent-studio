from __future__ import annotations

import json

import pytest

from hardware_capabilities import (
    AcceleratorCapability,
    HardwareProfile,
    RuntimeCapability,
)


def test_profile_normalizes_generic_platforms_and_round_trips_as_json() -> None:
    payload = {
        "architecture": " x86_64 ",
        "operating_system": " Linux ",
        "accelerators": [
            {
                "kind": "gpu",
                "vendor": "vendor-b",
                "name": "accelerator-2",
                "memory_bytes": 2 * 1024**3,
                "capabilities": [" fp16 ", "tensor", "fp16"],
            },
            {"kind": "cpu", "vendor": "vendor-a", "capabilities": ["avx"]},
        ],
        "physical_memory_bytes": 16 * 1024**3,
        "available_memory_bytes": 8 * 1024**3,
        "memory_pressure": "warning",
        "installed_runtimes": [{"name": "runtime-b", "version": "2", "capabilities": ["stream"]}],
        "permissions": ["network", "filesystem", "network"],
    }

    profile = HardwareProfile.from_mapping(payload)
    assert profile.architecture == "x86_64"
    assert profile.operating_system == "Linux"
    assert [item.kind for item in profile.accelerators] == ["cpu", "gpu"]
    assert profile.accelerators[1].capabilities == ("fp16", "tensor")
    assert profile.permissions == ("filesystem", "network")
    encoded = json.dumps(profile.to_mapping(), sort_keys=True)
    assert HardwareProfile.from_mapping(json.loads(encoded)) == profile


def test_schema_accepts_other_architecture_and_arbitrary_accelerator_without_chip_enum() -> None:
    profile = HardwareProfile(
        operating_system="ExampleOS",
        architecture="future-architecture",
        accelerators=(
            AcceleratorCapability(
                kind="neural",
                vendor="independent-vendor",
                name="future-device",
                capabilities=("custom-op",),
            ),
        ),
        installed_runtimes=(RuntimeCapability("portable-runtime", "0.1"),),
    )
    assert profile.to_mapping()["architecture"] == "future-architecture"
    assert profile.to_mapping()["accelerators"][0]["kind"] == "neural"


def test_profile_and_nested_capabilities_are_immutable() -> None:
    profile = HardwareProfile("OS", "arch", permissions=("read",))
    with pytest.raises(AttributeError):
        profile.architecture = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        profile.accelerators += (AcceleratorCapability("gpu"),)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("physical_memory_bytes", -1, "physical_memory_bytes"),
        ("available_memory_bytes", 17, "available_memory_bytes"),
        ("memory_pressure", "severe", "memory_pressure"),
        ("schema_version", 2, "schema_version"),
    ],
)
def test_profile_rejects_invalid_normalized_values(field: str, value: object, message: str) -> None:
    payload: dict[str, object] = {
        "operating_system": "OS",
        "architecture": "arch",
        "physical_memory_bytes": 16,
        "available_memory_bytes": 8,
    }
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        HardwareProfile.from_mapping(payload)


def test_profile_rejects_available_memory_above_physical_memory() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        HardwareProfile("OS", "arch", physical_memory_bytes=1, available_memory_bytes=2)


def test_profile_rejects_malformed_nested_capability_and_nul_identifier() -> None:
    with pytest.raises(TypeError, match="accelerator must be an object"):
        HardwareProfile.from_mapping({"operating_system": "OS", "architecture": "arch", "accelerators": ["gpu"]})
    with pytest.raises(ValueError, match="must not contain a NUL"):
        HardwareProfile("OS\x00", "arch")
