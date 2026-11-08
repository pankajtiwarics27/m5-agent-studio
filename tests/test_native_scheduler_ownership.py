"""Miswired claimed leases cannot transfer FIFO ownership or poison foreign work."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture, admission
from test_native_inference_scheduler import scheduler as inference_scheduler, collect
from test_native_load_control import scheduler as load_scheduler, call as load_call
from test_native_unload_control import scheduler as unload_scheduler
from test_native_unload_steps import call as unload_call


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("actor", ["inference", "load", "unload"])
@pytest.mark.parametrize("foreign_purpose", ["inference", "load"])
def test_foreign_fifo_lease_is_refused_without_producer_send_or_poisoning(
    tmp_path, monkeypatch, backend, actor, foreign_purpose
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            b.wire.calls.clear()
            enqueue = (
                b.coord.enqueue
                if foreign_purpose == "inference"
                else b.coord.enqueue_load
            )
            enqueue(
                "foreign",
                "shared",
                b.q.profile.memory.target,
                deadline_ms=40000,
                now_ms=b.now,
            )
            claim = b.coord.claim_next

            def miswired(domain, **kwargs):
                return claim(domain, **dict(kwargs, request_id="foreign"))

            monkeypatch.setattr(b.coord, "claim_next", miswired)
            produced = []

            def forbidden(*args):
                produced.append(True)
                raise RuntimeError("foreign-producer-must-not-run")

            if actor == "inference":
                execute = inference_scheduler(
                    b, change_plan=lambda plan: replace(plan, warm=forbidden)
                )
                operation = collect(execute.stream(admission(b).call))
            elif actor == "load":
                execute = load_scheduler(
                    b, alter=lambda plan: replace(plan, completion=forbidden)
                )
                operation = execute.command(load_call(b))
            else:
                execute = unload_scheduler(
                    b, alter=lambda plan: replace(plan, release=forbidden)
                )
                operation = execute.command(unload_call(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await operation
            assert (
                produced == []
                and b.wire.calls == []
                and getattr(b, "count_calls", 0) == 0
            )

            def rows(conn):
                foreign = conn.execute(
                    "SELECT * FROM requests WHERE id='foreign'"
                ).fetchone()
                own = conn.execute(
                    "SELECT * FROM requests WHERE purpose=? AND id!='foreign' ORDER BY seq DESC LIMIT 1",
                    (actor,),
                ).fetchone()
                slot = conn.execute(
                    "SELECT status FROM leases WHERE request_id='foreign'"
                ).fetchone()
                return foreign, own, slot

            foreign, own, slot = b.coord._call(rows)
            assert (
                foreign["status"] == "admitted"
                and not foreign["cancel_requested"]
                and slot["status"] == "active"
            )
            assert own["status"] == "cancelled"
            assert (
                b.coord.snapshot("shared").state == "idle"
                and b.coord.snapshot("shared").active_requests == 1
            )
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert not execute._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_real_foreign_inference_lease_cannot_send_this_call_under_another_request(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            b.wire.calls.clear()
            b.coord.enqueue(
                "foreign",
                "shared",
                b.q.profile.memory.target,
                deadline_ms=40000,
                now_ms=b.now,
            )
            claim = b.coord.claim_next
            monkeypatch.setattr(
                b.coord,
                "claim_next",
                lambda domain, **kw: claim(domain, **dict(kw, request_id="foreign")),
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(inference_scheduler(b).stream(admission(b).call))
            assert b.wire.calls == [] and getattr(b, "count_calls", 0) == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("foreign", ["coordinator", "memory"])
def test_warm_handoff_requires_the_schedulers_actual_owner_objects_before_count(
    tmp_path, backend, foreign
):
    import copy
    from shared_memory_admission import SharedMemoryAdmission

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            b.wire.calls.clear()

            def change(plan):
                def handoff(req, mem):
                    job = plan.warm(req, mem)
                    service = copy.copy(job.fit.service)
                    if foreign == "coordinator":
                        service.coord = copy.copy(b.coord)
                    else:
                        service.memory = SharedMemoryAdmission(service.coord)
                    bridge = copy.copy(job.bridge)
                    if backend == "ollama":
                        bridge.coordinator = service.coord
                    else:
                        bridge.coord = service.coord
                    bridge.memory = service.memory
                    bridge.admit_request = service.authorizes
                    return replace(
                        job, bridge=bridge, fit=replace(job.fit, service=service)
                    )

                return replace(plan, warm=handoff)

            execute = inference_scheduler(b, change_plan=change)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(execute.stream(admission(b).call))
            assert b.wire.calls == [] and getattr(b, "count_calls", 0) == 0
            assert b.coord.snapshot("shared").state == "reconciling"
            assert (
                b.memory.view("shared").committed_bytes == 600
                and b.memory.view("shared").state == "paused"
            )
            assert not execute._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "field", ["request_id", "domain_id", "target", "owner_epoch", "subclass"]
)
def test_consumer_identity_fence_refuses_counterfeit_scope_without_claiming_authority(
    tmp_path, backend, field
):
    from model_lifecycle_coordinator import RequestLease

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            ready = admission(b)
            queued = b.transition.enqueue(ready, "owned", "shared", deadline_ms=40000)
            request = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
            execute = inference_scheduler(b)
            execute._check_lease_owner(queued, request)
            if field == "subclass":

                class Pretend(RequestLease):
                    pass

                from dataclasses import asdict

                changes = asdict(request)
                changes["target"] = request.target
                forged = Pretend(**changes)
            else:
                forged = replace(
                    request,
                    **{
                        field: {
                            "request_id": "foreign",
                            "domain_id": "foreign-domain",
                            "target": replace(
                                request.target, artifact_id="foreign-model"
                            ),
                            "owner_epoch": request.owner_epoch + 1,
                        }[field]
                    },
                )
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                execute._check_lease_owner(queued, forged)
            assert b.coord.snapshot("shared") == before
            assert b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("purpose", ["inference", "load"])
def test_own_wrong_purpose_unload_slot_still_holds_uncertainty_without_send(
    tmp_path, monkeypatch, backend, purpose
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            b.wire.calls.clear()
            monkeypatch.setattr(
                b.coord,
                "enqueue_unload",
                b.coord.enqueue if purpose == "inference" else b.coord.enqueue_load,
            )
            execute = unload_scheduler(b)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(unload_call(b))
            assert b.wire.calls == [] and getattr(b, "count_calls", 0) == 0
            assert (
                b.coord.snapshot("shared").state == "reconciling"
                and b.coord.snapshot("shared").active_requests == 1
            )
            assert b.memory.view("shared").committed_bytes == 600
            assert not execute._lock.locked()

    asyncio.run(run())
