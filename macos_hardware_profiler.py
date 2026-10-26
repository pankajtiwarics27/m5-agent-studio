"""macOS hardware and memory discovery adapter.

The profiler is intentionally a thin adapter over standard macOS commands. It
returns the generic ``HardwareProfile`` contract and keeps probing injectable,
so callers and tests do not depend on a particular Apple chip or GPU vendor.
Unavailable probes become explicit unknown values rather than fabricated
measurements.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from hardware_capabilities import AcceleratorCapability, HardwareProfile


class CommandRunner(Protocol):
    def __call__(self, command: Sequence[str]) -> str: ...


class MacHardwareProbeError(RuntimeError):
    """Raised when a required injected probe cannot be used."""


def _run_command(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=2.0, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MacHardwareProbeError(f"hardware probe failed: {command[0]}") from exc
    if completed.returncode != 0:
        raise MacHardwareProbeError(f"hardware probe returned {completed.returncode}: {command[0]}")
    return completed.stdout


def _safe_probe(runner: CommandRunner, command: Sequence[str]) -> str:
    try:
        return runner(command)
    except (MacHardwareProbeError, OSError, subprocess.SubprocessError, ValueError):
        return ""


def _integer(text: str) -> int | None:
    match = re.search(r"\b(\d+)\b", text)
    return int(match.group(1)) if match else None


def _page_stats(text: str) -> tuple[int, dict[str, int]]:
    page_size_match = re.search(r"page size of (\d+) bytes", text)
    page_size = int(page_size_match.group(1)) if page_size_match else 16384
    pages: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        value = raw.strip().rstrip(".").replace(" ", "")
        if value.isdigit():
            pages[key.strip()] = int(value)
    return page_size, pages


def _available_from_vm_stat(text: str, physical_memory_bytes: int | None) -> int | None:
    page_size, pages = _page_stats(text)
    if not pages:
        return None
    available_pages = sum(pages.get(key, 0) for key in ("Pages free", "Pages inactive", "Pages speculative"))
    available = available_pages * page_size
    return min(physical_memory_bytes, available) if physical_memory_bytes is not None else available


def _pressure_and_available(
    pressure_text: str, vm_stat_text: str, physical_memory_bytes: int | None
) -> tuple[str, int | None]:
    lowered = pressure_text.lower()
    if "critical" in lowered:
        pressure = "critical"
    elif "warning" in lowered or "high pressure" in lowered:
        pressure = "warning"
    elif "nominal" in lowered or "normal" in lowered:
        pressure = "nominal"
    else:
        free_match = re.search(r"free percentage\s*:\s*([0-9]+(?:\.[0-9]+)?)%", lowered)
        if free_match:
            free_percent = float(free_match.group(1))
            pressure = "critical" if free_percent <= 10 else "warning" if free_percent <= 20 else "nominal"
        else:
            pressure = "unknown"

    available = None
    free_match = re.search(r"free percentage\s*:\s*([0-9]+(?:\.[0-9]+)?)%", lowered)
    if free_match and physical_memory_bytes is not None:
        available = int(physical_memory_bytes * float(free_match.group(1)) / 100)
    if available is None:
        available = _available_from_vm_stat(vm_stat_text, physical_memory_bytes)
    return pressure, available


def _display_records(value: object) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if any(key in value for key in ("sppci_model", "spdisplays_vendor", "_name")):
            records.append(value)
        for child in value.values():
            records.extend(_display_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_display_records(child))
    return records


def _parse_accelerators(text: str) -> tuple[AcceleratorCapability, ...]:
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ()
    accelerators: list[AcceleratorCapability] = []
    seen: set[tuple[str | None, str | None]] = set()
    for record in _display_records(payload):
        name = record.get("sppci_model") or record.get("_name")
        vendor = record.get("spdisplays_vendor") or record.get("sppci_vendor")
        if not isinstance(name, str) and not isinstance(vendor, str):
            continue
        normalized_name = name.strip() if isinstance(name, str) and name.strip() else None
        normalized_vendor = vendor.strip() if isinstance(vendor, str) and vendor.strip() else None
        identity = (normalized_vendor, normalized_name)
        if identity in seen:
            continue
        seen.add(identity)
        accelerators.append(
            AcceleratorCapability(kind="gpu", vendor=normalized_vendor, name=normalized_name)
        )
    return tuple(accelerators)


class MacHardwareProfiler:
    """Collect a normalized profile using macOS probes when available."""

    def __init__(
        self,
        *,
        system_name: str | None = None,
        architecture: str | None = None,
        runner: CommandRunner = _run_command,
    ) -> None:
        self.system_name = system_name or platform.system()
        self.architecture = architecture or platform.machine() or "unknown"
        self.runner = runner

    def collect(self) -> HardwareProfile:
        """Return the current profile, with unknown values for failed probes."""
        if self.system_name != "Darwin":
            return HardwareProfile(operating_system=self.system_name, architecture=self.architecture)

        physical_text = _safe_probe(self.runner, ("/usr/sbin/sysctl", "-n", "hw.memsize"))
        physical_memory = _integer(physical_text)
        pressure_text = _safe_probe(self.runner, ("/usr/bin/memory_pressure", "-Q"))
        vm_stat_text = _safe_probe(self.runner, ("/usr/bin/vm_stat",))
        pressure, available_memory = _pressure_and_available(pressure_text, vm_stat_text, physical_memory)
        display_text = _safe_probe(self.runner, ("/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"))
        return HardwareProfile(
            operating_system="macOS",
            architecture=self.architecture,
            accelerators=_parse_accelerators(display_text),
            physical_memory_bytes=physical_memory,
            available_memory_bytes=available_memory,
            memory_pressure=pressure,
        )


def collect_macos_hardware(
    *,
    system_name: str | None = None,
    architecture: str | None = None,
    runner: CommandRunner = _run_command,
) -> HardwareProfile:
    """Convenience wrapper for one profile collection."""
    return MacHardwareProfiler(system_name=system_name, architecture=architecture, runner=runner).collect()
