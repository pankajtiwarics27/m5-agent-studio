from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from macos_hardware_profiler import MacHardwareProfiler, collect_macos_hardware


def _runner_for(values: dict[tuple[str, ...], str]):
    calls: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> str:
        key = tuple(command)
        calls.append(key)
        return values[key]

    return runner, calls


def test_macos_profiler_normalizes_memory_pressure_and_accelerators() -> None:
    display = {
        "SPDisplaysDataType": [
            {"sppci_model": "Generic Display Accelerator", "spdisplays_vendor": "Vendor A"},
            {"sppci_model": "Generic Display Accelerator", "spdisplays_vendor": "Vendor A"},
        ]
    }
    runner, calls = _runner_for(
        {
            ("/usr/sbin/sysctl", "-n", "hw.memsize"): str(16 * 1024**3),
            ("/usr/bin/memory_pressure", "-Q"): "System-wide memory free percentage: 15%\n",
            ("/usr/bin/vm_stat",): "page size of 4096 bytes\nPages free: 100.\nPages inactive: 200.\n",
            ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"): json.dumps(display),
        }
    )

    profile = MacHardwareProfiler(system_name="Darwin", architecture="generic-arch", runner=runner).collect()

    assert profile.operating_system == "macOS"
    assert profile.architecture == "generic-arch"
    assert profile.physical_memory_bytes == 16 * 1024**3
    assert profile.available_memory_bytes == int(16 * 1024**3 * 0.15)
    assert profile.memory_pressure == "warning"
    assert len(profile.accelerators) == 1
    assert profile.accelerators[0].kind == "gpu"
    assert len(calls) == 4


def test_macos_profiler_uses_vm_stat_when_pressure_percentage_is_missing() -> None:
    runner, _ = _runner_for(
        {
            ("/usr/sbin/sysctl", "-n", "hw.memsize"): "1000",
            ("/usr/bin/memory_pressure", "-Q"): "pressure unavailable",
            ("/usr/bin/vm_stat",): "page size of 10 bytes\nPages free: 3.\nPages inactive: 4.\nPages speculative: 5.\n",
            ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"): "{}",
        }
    )

    profile = collect_macos_hardware(system_name="Darwin", architecture="arch", runner=runner)

    assert profile.memory_pressure == "unknown"
    assert profile.available_memory_bytes == 120


def test_profiler_fails_closed_for_missing_probes_and_non_macos() -> None:
    def missing(_command: Sequence[str]) -> str:
        raise OSError("command unavailable")

    mac_profile = collect_macos_hardware(system_name="Darwin", architecture="arch", runner=missing)
    assert mac_profile.physical_memory_bytes is None
    assert mac_profile.available_memory_bytes is None
    assert mac_profile.memory_pressure == "unknown"
    assert mac_profile.accelerators == ()

    calls: list[tuple[str, ...]] = []

    def unexpected(command: Sequence[str]) -> str:
        calls.append(tuple(command))
        raise AssertionError("non-macOS must not run macOS probes")

    other = collect_macos_hardware(system_name="Linux", architecture="x86_64", runner=unexpected)
    assert other.operating_system == "Linux"
    assert other.architecture == "x86_64"
    assert calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("System-wide memory free percentage: 9%", "critical"), ("System-wide memory free percentage: 50%", "nominal")],
)
def test_pressure_thresholds_are_normalized(text: str, expected: str) -> None:
    from macos_hardware_profiler import _pressure_and_available

    assert _pressure_and_available(text, "", 100)[0] == expected
