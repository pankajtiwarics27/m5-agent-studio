"""FIFO identity must precede operation producers and effectful stale gates."""

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
@pytest.mark.parametrize("goal", ["acquire", "release"])
def test_foreign_operation_is_not_produced_or_poisoned_by_another_actor(
    tmp_path, monkeypatch, backend, actor, goal
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            b.wire.calls.clear()
            target = b.q.profile.memory.target
            if goal == "acquire":
                b.coord.enqueue(
                    "foreign",
                    "shared",
                    replace(target, artifact_id="foreign-model"),
                    deadline_ms=40000,
                    now_ms=b.now,
                )
            else:
                b.coord.enqueue_unload(
                    "foreign", "shared", target, deadline_ms=40000, now_ms=b.now
                )
            claim = b.coord.claim_next
            claimed = []

            def miswired(domain, **kw):
                item = claim(domain, **dict(kw, request_id="foreign"))
                claimed.append(item)
                return item

            monkeypatch.setattr(b.coord, "claim_next", miswired)
            produced = []

            def forbidden(*args):
                produced.append(True)
                raise RuntimeError("foreign-operation-producer")

            if actor == "inference":
                execute = inference_scheduler(
                    b, change_plan=lambda plan: replace(plan, transition=forbidden)
                )
                operation = collect(execute.stream(admission(b).call))
            elif actor == "load":
                execute = load_scheduler(
                    b, alter=lambda plan: replace(plan, transition=forbidden)
                )
                operation = execute.command(load_call(b))
            else:
                execute = unload_scheduler(
                    b, alter=lambda plan: replace(plan, release=forbidden)
                )
                operation = execute.command(unload_call(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await operation
            assert produced == [] and b.wire.calls == []
            assert len(claimed) == 1

            def rows(conn):
                return (
                    conn.execute(
                        "SELECT status,cancel_requested FROM requests WHERE id='foreign'"
                    ).fetchone(),
                    conn.execute(
                        "SELECT status,stage,dispatched FROM operations WHERE id=?",
                        (claimed[0].operation_id,),
                    ).fetchone(),
                    conn.execute(
                        "SELECT status FROM requests WHERE id!='foreign' ORDER BY seq DESC LIMIT 1"
                    ).fetchone(),
                )

            foreign, op, own = b.coord._call(rows)
            assert foreign["status"] == "queued" and not foreign["cancel_requested"]
            assert (
                op["status"] == "active" and op["stage"] == "drain" and op["dispatched"]
            )
            assert own["status"] == "cancelled"
            assert b.coord.snapshot("shared").state == "switching"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert not execute._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("late", [False, True])
def test_direct_acquire_driver_checks_actual_fifo_owner_before_native_or_stale_gate(
    tmp_path, backend, late
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            ready = admission(b)
            b.coord.enqueue(
                "foreign",
                "shared",
                ready.memory.target,
                deadline_ms=100000,
                now_ms=b.now,
            )
            queued = b.transition.enqueue(ready, "owned", "shared", deadline_ms=100000)
            step = b.coord.claim_next("shared", request_id="foreign", now_ms=b.now)
            if late:
                b.now += 61000
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, queued, None)
            assert b.wire.calls == []
            assert (
                b.coord.snapshot("shared").state == "switching"
                and b.coord.snapshot("shared").operation_status == "active"
            )
            assert b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("actor", ["inference", "load"])
def test_same_target_does_not_make_another_fifo_acquire_claim_ours(
    tmp_path, monkeypatch, backend, actor
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
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
            produced = []

            def forbidden(*args):
                produced.append(True)
                raise RuntimeError("foreign")

            if actor == "inference":
                execute = inference_scheduler(
                    b, change_plan=lambda plan: replace(plan, transition=forbidden)
                )
                operation = collect(execute.stream(admission(b).call))
            else:
                execute = load_scheduler(
                    b, alter=lambda plan: replace(plan, transition=forbidden)
                )
                operation = execute.command(load_call(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await operation
            assert produced == [] and b.wire.calls == []
            assert b.coord.snapshot("shared").operation_status == "active"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault", ["generation", "epoch", "operation", "target", "source", "subclass"]
)
def test_claim_identity_check_cannot_mutate_the_real_operation_for_a_forged_step(
    tmp_path, backend, fault
):
    from model_lifecycle_coordinator import TransitionStep

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            queued = b.transition.enqueue(
                admission(b), "owned", "shared", deadline_ms=40000
            )
            step = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
            b.transition.check_claim_owner(step, queued)
            if fault == "subclass":

                class Pretend(TransitionStep):
                    pass

                forged = Pretend(
                    step.operation_id,
                    step.domain_id,
                    step.generation,
                    step.owner_epoch,
                    step.kind,
                    step.source,
                    step.source_instance_id,
                    step.target,
                )
            else:
                changes = {
                    "generation": {"generation": step.generation + 1},
                    "epoch": {"owner_epoch": step.owner_epoch + 1},
                    "operation": {"operation_id": "unknown"},
                    "target": {"target": replace(step.target, artifact_id="other")},
                    "source": {
                        "source": step.target,
                        "source_instance_id": "other-instance",
                    },
                }[fault]
                forged = replace(step, **changes)
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                b.transition.check_claim_owner(forged, queued)
            assert b.coord.snapshot("shared") == before
            assert b.wire.calls == [] and b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_direct_release_driver_does_not_consume_or_expire_foreign_release(
    tmp_path, backend
):
    import native_transition_driver as nt

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            ready = nt.UnloadAdmission(unload_call(b), b.q)
            b.transition.enqueue_unload(ready, "foreign", "shared", deadline_ms=100000)
            queued = b.transition.enqueue_unload(
                ready, "owned", "shared", deadline_ms=100000
            )
            step = b.coord.claim_next("shared", request_id="foreign", now_ms=b.now)
            b.wire.calls.clear()
            b.now += 61000
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, None)
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").operation_status == "active"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("field", ["request_id", "memory_id"])
def test_release_claim_must_match_its_exact_durable_request_and_source_hold(
    tmp_path, backend, field
):
    import native_transition_driver as nt

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await load_scheduler(b).command(load_call(b)) == {
                "status": "confirmed"
            }
            queued = b.transition.enqueue_unload(
                nt.UnloadAdmission(unload_call(b), b.q),
                "owned",
                "shared",
                deadline_ms=40000,
            )
            step = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
            b.transition.check_claim_owner(step, queued)
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                b.transition.check_claim_owner(
                    replace(step, **{field: "foreign"}), queued
                )
            assert (
                b.coord.snapshot("shared") == before
                and b.memory.view("shared").state == "ready"
            )

    asyncio.run(run())
