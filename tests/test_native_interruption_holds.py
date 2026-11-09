"""Late callbacks cannot hold memory after losing their exact current intent."""

from __future__ import annotations

import asyncio

import pytest

import native_transition_driver as nt
import runtime_dispatch_boundary as rd
from shared_memory_admission import _model_step
from test_native_dispatch_executor import context
from test_native_transition_driver import fixture, admission, job
from test_native_load_control import scheduler as load_scheduler, call as load_call
from test_native_inference_scheduler import scheduler as inference_scheduler, collect


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("old", ["request", "operation"])
def test_late_native_failure_cannot_pause_a_successors_live_model(
    tmp_path, backend, old
):
    async def run():
        b = await context(tmp_path, backend)
        with b.coord:
            request, memory = b.job.permit.intent, b.job.fit.memory
            assert b.coord.finish_request(
                request, reference="fake-proven-terminal", now_ms=b.now
            )
            b.coord.enqueue_load(
                "successor", "shared", request.target, deadline_ms=40000, now_ms=b.now
            )
            successor = b.coord.claim_next(
                "shared", request_id="successor", now_ms=b.now
            )
            intent = request if old == "request" else _model_step(memory.binding)
            permit = b.job.permit if old == "request" else b.driver.permit(b, intent)
            before = b.coord.snapshot("shared")
            failure = b.bridge._failure if backend == "ollama" else b.bridge._hold
            failure(permit, memory)
            assert b.coord.snapshot("shared") == before
            assert b.memory.view("shared").state == "ready"
            assert b.memory.view("shared").committed_bytes == 600
            assert successor.request_id == "successor"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_direct_stage_failure_after_real_load_advance_does_not_pause_next_verify(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            queued = b.transition.enqueue(
                admission(b), "owned", "shared", deadline_ms=40000
            )
            step = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
            cls = nt.OllamaTransition if backend == "ollama" else nt.LmTransition
            original = cls._send

            async def late(self, **kwargs):
                assert await original(self, **kwargs) == "verify_required"
                raise OSError("private-late-callback")

            with monkeypatch.context() as patch:
                patch.setattr(cls, "_send", late)
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await b.transition.advance(step, queued, job(b, step))
            assert b.coord.snapshot("shared").operation_stage == "verify"
            assert b.coord.snapshot("shared").operation_status == "active"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            memory = b.memory.view("shared").leases[0]
            verify = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
            await b.transition.advance(verify, queued, job(b, verify, memory))
            assert b.coord.snapshot("shared").state == "idle"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("actor", ["inference", "load"])
def test_actor_failure_after_real_verify_cannot_poison_independent_successor(
    tmp_path, monkeypatch, backend, actor
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            cls = nt.OllamaTransition if backend == "ollama" else nt.LmTransition
            original = cls._confirm

            async def late(self):
                result = await original(self)
                if self.permit.intent.kind == "verify":
                    request_id = b.coord._call(
                        lambda conn: conn.execute(
                            "SELECT id FROM requests WHERE status='queued' ORDER BY seq LIMIT 1"
                        ).fetchone()[0]
                    )
                    b.coord.cancel(request_id, now_ms=b.now)
                    b.coord.enqueue(
                        "successor",
                        "shared",
                        b.q.profile.memory.target,
                        deadline_ms=40000,
                        now_ms=b.now,
                    )
                    b.coord.claim_next("shared", request_id="successor", now_ms=b.now)
                    raise OSError("private-late-verify-callback")
                return result

            monkeypatch.setattr(cls, "_confirm", late)
            execute = (
                inference_scheduler(b) if actor == "inference" else load_scheduler(b)
            )
            operation = (
                collect(execute.stream(admission(b).call))
                if actor == "inference"
                else execute.command(load_call(b))
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await operation
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


async def held_intent(tmp_path, backend, role):
    from dataclasses import replace

    b = await context(tmp_path, backend)
    intent, memory = b.job.permit.intent, b.job.fit.memory
    if role != "request":
        b.coord.finish_request(intent, reference="fake-fixture-terminal", now_ms=b.now)
        target = intent.target
        if role == "load":
            b.coord.enqueue_load(
                "owned", "shared", target, deadline_ms=40000, now_ms=b.now
            )
        elif role == "release":
            b.coord.enqueue_unload(
                "owned", "shared", target, deadline_ms=40000, now_ms=b.now
            )
        else:
            b.coord.enqueue(
                "owned",
                "shared",
                replace(target, artifact_id="next-model"),
                deadline_ms=40000,
                now_ms=b.now,
            )
        intent = b.coord.claim_next("shared", request_id="owned", now_ms=b.now)
    b.wire.calls.clear()
    return b, intent, memory


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("role", ["request", "load", "transition", "release"])
@pytest.mark.parametrize("state", ["live", "cancelled", "expired"])
def test_owned_interruption_atomically_holds_slot_and_actual_model_without_retiring(
    tmp_path, backend, role, state
):
    async def run():
        b, intent, memory = await held_intent(tmp_path, backend, role)
        with b.coord:
            if state == "cancelled":
                b.coord.cancel(
                    intent.request_id if hasattr(intent, "request_id") else "owned",
                    now_ms=b.now,
                )
            elif state == "expired":
                b.now = 200000
            assert b.memory.hold_model(
                intent, memory, reference="fake-owned-failure", now_ms=b.now
            )
            assert b.memory.hold_model(
                intent, memory, reference="fake-duplicate-failure", now_ms=b.now
            )
            assert b.coord.snapshot("shared").state == "reconciling"
            assert (
                b.memory.view("shared").state == "paused"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert b.wire.calls == []
            row = b.coord._call(
                lambda conn: conn.execute(
                    "SELECT status FROM memory_reservations WHERE id=?",
                    (memory.lease_id,),
                ).fetchone()
            )
            assert row["status"] == "uncertain"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("role", ["request", "load", "transition", "release"])
def test_hold_write_failure_rolls_back_both_coordinator_and_memory(
    tmp_path, monkeypatch, backend, role
):
    import sqlite3
    from model_lifecycle_coordinator import CoordinatorError

    async def run():
        b, intent, memory = await held_intent(tmp_path, backend, role)
        with b.coord:
            before = b.coord.snapshot("shared")
            count = b.coord._call(
                lambda conn: conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            )

            def fail(*args):
                raise sqlite3.OperationalError("private-storage-error")

            monkeypatch.setattr(b.memory, "_change", fail)
            with pytest.raises(CoordinatorError, match="journal_unavailable"):
                b.memory.hold_model(
                    intent, memory, reference="fake-atomic-write", now_ms=b.now
                )
            assert b.coord.snapshot("shared") == before
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert (
                b.coord._call(
                    lambda conn: conn.execute(
                        "SELECT COUNT(*) FROM journal"
                    ).fetchone()[0]
                )
                == count
            )

    asyncio.run(run())


@pytest.mark.parametrize("settled", [False, True])
def test_old_unload_still_owns_only_the_unpaid_source_settlement_not_a_released_hold(
    tmp_path, settled
):
    from test_model_lifecycle_release import opened, loaded
    from test_model_lifecycle_coordinator import B
    from test_shared_memory_admission import sample

    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet)
        coord.enqueue("switch", "shared", B, deadline_ms=40000, now_ms=1000)
        drain = coord.claim_next("shared", request_id="switch", now_ms=1000)
        assert coord.complete_step(drain, reference="fake-drained", now_ms=1000)
        unload = coord.claim_next("shared", request_id="switch", now_ms=1000)
        fleet.models.clear()
        assert coord.complete_step(
            unload,
            reference="fake-absence",
            now_ms=1000,
            evidence=fleet.evidence(coord, 1000),
        )
        assert coord.snapshot("shared").operation_stage == "load"
        if settled:
            memory.settle(
                model, sample(memory), terminal=True, reference="fake-zero", now_ms=1000
            )
        before = coord.snapshot("shared")
        assert memory.hold_model(
            unload, model, reference="fake-late-settlement", now_ms=1000
        ) is (not settled)
        if settled:
            assert (
                coord.snapshot("shared") == before
                and memory.view("shared").state == "ready"
            )
            assert memory.view("shared").committed_bytes == 0
        else:
            assert (
                coord.snapshot("shared").state == "reconciling"
                and memory.view("shared").state == "paused"
            )
            assert memory.view("shared").committed_bytes == 600


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("field", ["lease_id", "owner_epoch", "work_id", "binding"])
def test_wrong_model_identity_cannot_partially_hold_the_right_intent(
    tmp_path, backend, field
):
    from dataclasses import replace
    from shared_memory_admission import MemoryDenied

    async def run():
        b, intent, memory = await held_intent(tmp_path, backend, "request")
        with b.coord:
            before = b.coord.snapshot("shared")
            value = (
                memory.owner_epoch + 1
                if field == "owner_epoch"
                else "foreign"
                if field != "binding"
                else "{}"
            )
            with pytest.raises(MemoryDenied):
                b.memory.hold_model(
                    intent,
                    replace(memory, **{field: value}),
                    reference="fake-wrong-model",
                    now_ms=b.now,
                )
            assert (
                b.coord.snapshot("shared") == before
                and b.memory.view("shared").state == "ready"
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_current_release_still_owns_its_barrier_after_drain_advances(tmp_path, backend):
    async def run():
        b, intent, memory = await held_intent(tmp_path, backend, "release")
        with b.coord:
            assert b.coord.complete_release(
                intent, reference="fake-drained", now_ms=b.now
            )
            assert b.coord.snapshot("shared").operation_stage == "unload"
            assert b.memory.hold_model(
                intent, memory, reference="fake-incomplete-release", now_ms=b.now
            )
            assert (
                b.coord.snapshot("shared").state == "reconciling"
                and b.memory.view("shared").state == "paused"
            )
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("current", [False, True])
def test_source_only_omlx_failure_respects_actual_operation_ownership(
    tmp_path, current
):
    from test_omlx_lifecycle_bridge import setup, start_loaded, switch_unload
    from test_model_lifecycle_coordinator import A

    async def run():
        env = setup(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            if current:
                model, permit = await switch_unload(env)
                intent = permit.step
            else:
                model = await start_loaded(*env)
                intent = _model_step(model.binding)
                coord.enqueue_load(
                    "successor", "shared", A, deadline_ms=40000, now_ms=clock.now
                )
                assert (
                    coord.claim_next("shared", request_id="successor", now_ms=clock.now)
                    is not None
                )
            before = coord.snapshot("shared")
            calls = len(wire.calls)
            bridge._hold(intent, model)
            assert len(wire.calls) == calls
            assert memory.view("shared").committed_bytes == 600
            if current:
                assert (
                    coord.snapshot("shared").state == "reconciling"
                    and memory.view("shared").state == "paused"
                )
            else:
                assert (
                    coord.snapshot("shared") == before
                    and memory.view("shared").state == "ready"
                )

    asyncio.run(run())
