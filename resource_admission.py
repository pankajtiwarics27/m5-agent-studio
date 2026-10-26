"""Atomic resource admission and lease lifecycle primitives."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class AdmissionError(ValueError):
    """Raised when an admission request or capacity definition is invalid."""


class AdmissionDenied(RuntimeError):
    """Raised when a request cannot fit within the current capacity."""


def _positive(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdmissionError(f"{field} must be a positive integer.")
    return value


@dataclass(frozen=True, slots=True)
class ResourceCapacity:
    """Total resources available to concurrently admitted work."""

    memory_bytes: int
    slots: int = 1

    def __post_init__(self) -> None:
        _positive(self.memory_bytes, "capacity.memory_bytes")
        _positive(self.slots, "capacity.slots")


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """Resources reserved by one operation."""

    memory_bytes: int
    slots: int = 1

    def __post_init__(self) -> None:
        _positive(self.memory_bytes, "request.memory_bytes")
        _positive(self.slots, "request.slots")


@dataclass(frozen=True, slots=True)
class AdmissionLease:
    """Opaque reservation handle returned after successful admission."""

    lease_id: str
    request: ResourceRequest


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Consistent point-in-time capacity and usage view."""

    capacity: ResourceCapacity
    used_memory_bytes: int
    used_slots: int
    active_leases: int

    @property
    def available_memory_bytes(self) -> int:
        return self.capacity.memory_bytes - self.used_memory_bytes

    @property
    def available_slots(self) -> int:
        return self.capacity.slots - self.used_slots


class ResourceAdmissionScheduler:
    """Admit work atomically and track cancellation/release by lease identity."""

    def __init__(self, capacity: ResourceCapacity) -> None:
        self.capacity = capacity
        self._lock = threading.Lock()
        self._leases: dict[str, AdmissionLease] = {}
        self._used_memory_bytes = 0
        self._used_slots = 0

    def snapshot(self) -> ResourceSnapshot:
        with self._lock:
            return ResourceSnapshot(
                capacity=self.capacity,
                used_memory_bytes=self._used_memory_bytes,
                used_slots=self._used_slots,
                active_leases=len(self._leases),
            )

    def try_admit(self, request: ResourceRequest) -> AdmissionLease | None:
        """Atomically reserve a request, or return ``None`` if it cannot fit."""
        if not isinstance(request, ResourceRequest):
            raise TypeError("request must be a ResourceRequest value.")
        with self._lock:
            if (
                self._used_memory_bytes + request.memory_bytes > self.capacity.memory_bytes
                or self._used_slots + request.slots > self.capacity.slots
            ):
                return None
            lease = AdmissionLease(uuid.uuid4().hex, request)
            self._leases[lease.lease_id] = lease
            self._used_memory_bytes += request.memory_bytes
            self._used_slots += request.slots
            return lease

    def admit(self, request: ResourceRequest) -> AdmissionLease:
        """Reserve a request or raise an explicit denial."""
        lease = self.try_admit(request)
        if lease is None:
            raise AdmissionDenied("resource request exceeds currently available capacity.")
        return lease

    def release(self, lease: AdmissionLease) -> bool:
        """Release a lease once; repeated or unknown releases are harmless."""
        if not isinstance(lease, AdmissionLease):
            raise TypeError("lease must be an AdmissionLease value.")
        with self._lock:
            active = self._leases.pop(lease.lease_id, None)
            if active is None:
                return False
            self._used_memory_bytes -= active.request.memory_bytes
            self._used_slots -= active.request.slots
            return True

    def cancel(self, lease: AdmissionLease) -> bool:
        """Cancel admitted work and return its resources to the pool."""
        return self.release(lease)

    @contextmanager
    def reservation(self, request: ResourceRequest) -> Iterator[AdmissionLease]:
        """Release a successful lease even when the operation is cancelled."""
        lease = self.admit(request)
        try:
            yield lease
        finally:
            self.release(lease)
