"""Dormant bounded cold-to-warm inference composition, no installed producers.

One server-composed backend serializes its requests through terminal native
completion/consumer close. The coordinator remains the domain authority across
other backends/threads; this actor never steals another FIFO request or polls a
pending native operation. Explicit controls deliberately remain unavailable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Any, cast
from uuid import uuid4

from model_lifecycle_coordinator import ModelTarget, RequestLease, TransitionStep
from native_dispatch_executor import (
    Execution,
    NativeDispatchExecutor,
    NativeFitDenied,
    OllamaExecution,
    LmExecution,
)
from native_transition_driver import (
    ColdAdmission,
    LmTransition,
    NativeTransitionDriver,
    OllamaTransition,
)
from request_fit import RequestFitDenied, decode
from runtime_dispatch_boundary import RuntimeDispatchCall, RuntimeDispatchDenied
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import MemoryLease


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeInferencePlan:
    """Trusted synchronous composition only, not transports or generic callbacks.

    Factories must produce concrete, independently scoped native/fit jobs. The
    scheduler does not mint permits or qualify native/counter/memory producers.
    """

    admission: ColdAdmission
    domain_id: str
    deadline_ms: int
    transition: Callable[
        [TransitionStep, MemoryLease | None], OllamaTransition | LmTransition
    ]
    warm: Callable[[RequestLease, MemoryLease], Execution]


class NativeInferenceScheduler:
    def __init__(
        self,
        driver: NativeTransitionDriver,
        plan: Callable[[RuntimeDispatchCall], NativeInferencePlan] | None = None,
    ):
        self.driver, self.plan = driver, plan
        self._lock = asyncio.Lock()

    async def command(self, call: RuntimeDispatchCall) -> dict[str, Any]:
        raise RuntimeDispatchDenied("coordinated_backend_unavailable")

    def _memory(self, domain: str, target: ModelTarget | None) -> MemoryLease:
        if target is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        leases = [
            lease
            for lease in self.driver.memory.view(domain).leases
            if lease.kind == "model"
            and decode(lease.binding.encode())["profile"]["target"] == asdict(target)
        ]
        if len(leases) != 1:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        lease = leases[0]
        row = self.driver.coord._call(
            lambda conn: self.driver.memory._active(conn, lease),
            now_ms=self.driver._now(),
        )
        if row["status"] != "dispatched":
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return lease

    async def stream(self, call: RuntimeDispatchCall) -> AsyncIterator[dict[str, Any]]:
        assert_no_active_write_transaction("native inference scheduler")
        if self.plan is None:
            raise RuntimeDispatchDenied("coordinated_backend_unavailable")
        queued = None
        request = None
        memory = None
        locked = False
        try:
            plan = self.plan(call)
            if type(plan) is not NativeInferencePlan or plan.admission.call != call:
                raise RuntimeDispatchDenied("invalid_dispatch_request")
            plan.admission.validate(self.driver._now())
            # Bound only lock waiting. No new output cap/inference timeout or
            # cancellation-as-physical-terminality assumption is introduced.
            wait = min(60.0, (plan.deadline_ms - self.driver._now()) / 1000)
            if wait <= 0:
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            async with asyncio.timeout(wait):
                await self._lock.acquire()
            locked = True
            queued = self.driver.enqueue(
                plan.admission,
                "native-" + uuid4().hex,
                plan.domain_id,
                deadline_ms=plan.deadline_ms,
            )
            # At most drain/unload/load/verify/request. Never retry a send,
            # spin/poll a pending operation or claim unrelated queued work.
            for _ in range(5):
                plan.admission.validate(self.driver._now())
                item = self.driver.coord.claim_next(
                    plan.domain_id,
                    now_ms=self.driver._now(),
                    request_id=queued.request_id,
                )
                if item is None:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                if isinstance(item, RequestLease):
                    request = item
                    memory = self._memory(plan.domain_id, item.target)
                    assert_no_active_write_transaction("native warm job producer")
                    warm = plan.warm(item, memory)
                    if (
                        type(warm) not in (OllamaExecution, LmExecution)
                        or warm.permit.intent != item
                        or warm.fit.permit.lease != item
                        or warm.fit.memory != memory
                        or warm.fit.profile != plan.admission.profile
                        or warm.fit.qualification != plan.admission.counter
                        or warm.permit.qualification != plan.admission.native
                    ):
                        raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                    async with aclosing(
                        cast(
                            AsyncGenerator[dict[str, Any], None],
                            NativeDispatchExecutor(lambda _: warm).stream(call),
                        )
                    ) as source:
                        async for event in source:
                            yield event
                    terminal = self.driver.coord._call(
                        lambda conn: conn.execute(
                            "SELECT status FROM leases WHERE id=?", (item.lease_id,)
                        ).fetchone(),
                        now_ms=self.driver._now(),
                    )
                    if terminal is None or terminal["status"] != "released":
                        raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                    return
                if item.kind in ("drain", "unload"):
                    memory = self._memory(plan.domain_id, item.source)
                elif item.kind == "load":
                    memory = None
                elif memory is None:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                assert_no_active_write_transaction("native transition job producer")
                try:
                    result = await self.driver.advance(
                        item, queued, plan.transition(item, memory)
                    )
                except BaseException:
                    # A producer can fail AFTER claim but BEFORE the concrete
                    # driver starts. Do not strand a dispatched stage or retry.
                    self.driver.coord.mark_uncertain(
                        item,
                        reference="native_scheduler_stage_interrupted",
                        now_ms=self.driver._now(),
                    )
                    if memory is not None:
                        self.driver.memory.mark_uncertain(
                            memory,
                            reference="native_scheduler_stage_interrupted",
                            now_ms=self.driver._now(),
                        )
                    raise
                memory = result.memory
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        except RequestFitDenied as exc:
            raise NativeFitDenied(exc) from None
        except RuntimeDispatchDenied:
            raise
        except Exception:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None
        finally:
            try:
                if request is not None:
                    # Independent native completion releases its request before
                    # usage/tools. A close AFTER that is not new uncertainty.
                    changed = self.driver.coord.mark_request_uncertain(
                        request,
                        reference="native_scheduler_interrupted",
                        now_ms=self.driver._now(),
                    )
                    if changed and memory is not None:
                        self.driver.memory.mark_uncertain(
                            memory,
                            reference="native_scheduler_interrupted",
                            now_ms=self.driver._now(),
                        )
                elif queued is not None:
                    # Only this actor's queued waiter; never cancel another
                    # request, settle its memory or claim native cancellation.
                    self.driver.coord.cancel(
                        queued.request_id, now_ms=self.driver._now()
                    )
            finally:
                if locked:
                    self._lock.release()
