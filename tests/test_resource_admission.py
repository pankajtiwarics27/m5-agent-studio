from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from resource_admission import (
    AdmissionDenied,
    AdmissionError,
    ResourceAdmissionScheduler,
    ResourceCapacity,
    ResourceRequest,
)


def test_admission_rejects_oversubscription_and_release_restores_capacity() -> None:
    scheduler = ResourceAdmissionScheduler(ResourceCapacity(memory_bytes=100, slots=2))
    first = scheduler.admit(ResourceRequest(memory_bytes=60, slots=1))

    assert scheduler.try_admit(ResourceRequest(memory_bytes=50, slots=1)) is None
    assert scheduler.snapshot().available_memory_bytes == 40
    assert scheduler.cancel(first) is True
    assert scheduler.cancel(first) is False
    assert scheduler.snapshot().used_memory_bytes == 0
    assert scheduler.snapshot().available_slots == 2


def test_context_manager_releases_after_failure() -> None:
    scheduler = ResourceAdmissionScheduler(ResourceCapacity(memory_bytes=100))
    with pytest.raises(RuntimeError, match="operation failed"), scheduler.reservation(ResourceRequest(memory_bytes=25)):
        assert scheduler.snapshot().used_memory_bytes == 25
        raise RuntimeError("operation failed")
    assert scheduler.snapshot().active_leases == 0
    assert scheduler.snapshot().available_memory_bytes == 100


def test_concurrent_admission_is_atomic_and_never_oversubscribes() -> None:
    scheduler = ResourceAdmissionScheduler(ResourceCapacity(memory_bytes=200, slots=2))
    contenders = 8
    barrier = Barrier(contenders)

    def attempt() -> bool:
        lease = scheduler.try_admit(ResourceRequest(memory_bytes=100))
        barrier.wait()
        if lease is not None:
            scheduler.release(lease)
        return lease is not None

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        admitted = list(pool.map(lambda _item: attempt(), range(contenders)))

    assert sum(admitted) == 2
    assert scheduler.snapshot().used_memory_bytes == 0
    assert scheduler.snapshot().active_leases == 0


def test_invalid_capacity_or_request_is_rejected() -> None:
    with pytest.raises(AdmissionError, match="request.memory_bytes"):
        ResourceRequest(memory_bytes=0)
    with pytest.raises(AdmissionError, match="request.slots"):
        ResourceRequest(memory_bytes=1, slots=0)
    with pytest.raises(AdmissionError, match="capacity.memory_bytes"):
        ResourceCapacity(memory_bytes=0)


def test_admit_reports_explicit_denial() -> None:
    scheduler = ResourceAdmissionScheduler(ResourceCapacity(memory_bytes=10))
    with pytest.raises(AdmissionDenied, match="exceeds"):
        scheduler.admit(ResourceRequest(memory_bytes=11))
