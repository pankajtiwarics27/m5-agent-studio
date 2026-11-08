"""Optional native unload actor: real dormant kernels; fake installed facts only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import native_inference_scheduler as ns
import native_transition_driver as nt
import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture, admission
from test_native_load_control import scheduler as load_scheduler, call as load_call
from test_native_unload_steps import call, job
from test_native_inference_scheduler import collect
from test_shared_memory_admission import sample


def scheduler(b, *, alter=None):
    execute = load_scheduler(b)

    def resolve(owned):
        plan = ns.NativeUnloadPlan(
            admission=nt.UnloadAdmission(owned, b.q),
            domain_id="shared",
            deadline_ms=40000,
            release=lambda step, mem: job(b, step, SimpleNamespace(memory=mem)),
        )
        return alter(plan) if alter else plan

    execute.unload_plan = resolve
    return execute


async def loaded(tmp_path, backend):
    b = fixture(tmp_path, backend)
    assert await load_scheduler(b).command(load_call(b)) == {"status": "confirmed"}
    b.wire.calls.clear()
    return b


def request_row(b):
    return b.coord._call(
        lambda conn: conn.execute(
            "SELECT * FROM requests WHERE purpose='unload' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    )


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_optional_unload_command_confirms_only_after_actual_zero_and_retires_without_poisoning_next_load(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            boundary = rd.RuntimeDispatchBoundary(True, {backend: execute})
            assert await boundary.command(backend, call(b).model, "unload") == {
                "status": "confirmed"
            }
            assert b.coord.snapshot("shared").resident is None
            assert b.coord.snapshot("shared").state == "idle"
            assert (
                b.memory.view("shared").committed_bytes == 0
                and b.memory.view("shared").state == "ready"
            )
            assert request_row(b)["status"] == "completed"
            assert len(b.driver.mutations(b)) == 1 and getattr(b, "count_calls", 0) == 0
            assert not execute._lock.locked()
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert (
                len(b.wire.calls) == before
            )  # Already absent is NOT synthetic success.
            assert await execute.command(load_call(b)) == {"status": "confirmed"}
            assert b.memory.view("shared").committed_bytes == 600
            assert (await collect(execute.stream(admission(b).call)))[-1][
                "type"
            ] == "usage"
            assert b.count_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "unqualified",
        "wrong_call",
        "payload",
        "wrong_plan",
        "load_admission",
        "wrong_model",
        "deadline",
    ],
)
def test_bad_unload_plan_refuses_before_queue_probe_or_source_disruption(
    tmp_path, backend, fault
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:

            def alter(plan):
                if fault == "wrong_plan":
                    return object()
                if fault == "deadline":
                    return replace(plan, deadline_ms=b.now)
                ready = plan.admission
                if fault == "load_admission":
                    ready = nt.LoadAdmission(load_call(b), b.q)
                elif fault in ("expired", "unqualified"):
                    ready = replace(
                        ready,
                        native=replace(
                            b.q,
                            **(
                                {"expires_ms": b.now}
                                if fault == "expired"
                                else {"capabilities": ()}
                            ),
                        ),
                    )
                else:
                    ready = replace(
                        ready,
                        call=replace(
                            ready.call,
                            **(
                                {"action": "load"}
                                if fault == "wrong_call"
                                else {"payload": b'{"keep_alive":0}'}
                                if fault == "payload"
                                else {"model": "other"}
                            ),
                        ),
                    )
                return replace(plan, admission=ready)

            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b, alter=alter).command(call(b))
            assert b.wire.calls == [] and request_row(b) is None
            assert b.coord.snapshot("shared") == before
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "factory_drain",
        "factory_unload",
        "wrong_job",
        "generic_bridge",
        "wrong_memory",
        "missing_proof",
        "ack_only",
        "wrong_scope",
        "not_terminal",
        "version",
        "nonzero",
        "sample_error",
        "activity",
        "denied",
    ],
)
def test_unload_actor_failure_preserves_uncertainty_and_charges_without_confirm_or_replay(
    tmp_path, backend, fault
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:

            def release(step, mem):
                if (
                    fault == "factory_drain"
                    or fault == "factory_unload"
                    and step.kind == "unload"
                ):
                    raise RuntimeError("secret")
                current = job(b, step, SimpleNamespace(memory=mem))
                if step.kind != "unload":
                    return current
                original = current.proof

                async def proof(obs):
                    evidence = await original(obs)
                    if fault == "ack_only":
                        return {"status": "ok"}
                    if fault == "wrong_scope":
                        return replace(
                            evidence,
                            scope=replace(evidence.scope, operation_id="foreign"),
                        )
                    if fault == "not_terminal":
                        return replace(evidence, operations_terminal=False)
                    if fault == "version":
                        return replace(
                            evidence,
                            inventories=tuple(
                                replace(i, runtime_version="changed")
                                if i.runtime.runtime_id == backend
                                else i
                                for i in evidence.inventories
                            ),
                        )
                    return evidence

                async def measured():
                    if fault == "sample_error":
                        raise OSError("secret")
                    return sample(b.memory, now=b.now, attributed=((mem.lease_id, 1),))

                if fault == "wrong_job":
                    return object()
                if fault == "generic_bridge":
                    return replace(
                        current,
                        bridge=SimpleNamespace(
                            coord=b.coord, coordinator=b.coord, memory=b.memory
                        ),
                    )
                if fault == "wrong_memory":
                    return replace(current, memory=replace(mem, lease_id="foreign"))
                if fault == "missing_proof":
                    return replace(current, proof=None)
                if fault in ("ack_only", "wrong_scope", "not_terminal", "version"):
                    return replace(current, proof=proof)
                if fault in ("nonzero", "sample_error"):
                    return replace(current, sample=measured)
                if fault == "activity":
                    return replace(
                        current,
                        activity=replace(current.activity, active_requests=None),
                    )
                if fault == "denied":
                    b.authorized = False
                return current

            execute = scheduler(b, alter=lambda plan: replace(plan, release=release))
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                await execute.command(call(b))
            assert "secret" not in str(error.value)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").operation_status == "uncertain"
            assert (
                b.memory.view("shared").committed_bytes == 600
                and b.memory.view("shared").state == "paused"
            )
            assert request_row(b)["status"] == "cancelled"
            assert not execute._lock.locked()
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert len(b.wire.calls) == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("condition", ["cancelled", "expired"])
def test_post_send_cleanup_never_reports_confirmed_for_cancelled_or_expired_request(
    tmp_path, backend, condition
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:

            def after_send(method, path):
                if method == "POST":
                    if condition == "cancelled":
                        b.coord.cancel(request_row(b)["id"], now_ms=b.now)
                    else:
                        b.now = 40000

            b.wire.hook = after_send
            execute = scheduler(b)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert request_row(b)["status"] == condition
            assert b.coord.snapshot("shared").state == "idle"
            assert (
                b.memory.view("shared").committed_bytes == 0
                and b.memory.view("shared").state == "ready"
            )
            assert not execute._lock.locked()
            # Neither the successful retirement nor failed API status poisons a successor.
            b.wire.hook = None
            if condition == "expired":
                original = execute.load_plan
                execute.load_plan = lambda owned: replace(
                    original(owned), deadline_ms=49000
                )
            assert await execute.command(load_call(b)) == {"status": "confirmed"}

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_unload_cancelled_during_actual_settlement_keeps_barrier_and_model_charge(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            entered = asyncio.Event()

            async def blocked():
                assert b.coord.snapshot("shared").operation_status == "complete"
                entered.set()
                await asyncio.Future()

            execute = scheduler(
                b,
                alter=lambda plan: replace(
                    plan,
                    release=lambda step, mem: job(
                        b, step, SimpleNamespace(memory=mem), sample=blocked
                    ),
                ),
            )
            task = asyncio.create_task(execute.command(call(b)))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not execute._lock.locked()
            assert request_row(b)["status"] == "cancelled"
            assert b.coord.snapshot("shared").state == "reconciling"
            assert (
                b.memory.view("shared").committed_bytes == 600
                and b.memory.view("shared").state == "paused"
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_fake_retired_result_cannot_replace_actual_request_memory_and_retirement_receipt(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            original = b.transition.advance_release

            async def false_retired(step, queued, current):
                if step.kind == "drain":
                    return await original(step, queued, current)
                return nt.ReleaseResult(step, None, True)

            monkeypatch.setattr(b.transition, "advance_release", false_retired)
            execute = scheduler(b)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "reconciling"
            assert (
                b.memory.view("shared").committed_bytes == 600
                and b.memory.view("shared").state == "paused"
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_source_load_and_unload_on_same_actor_serialize_through_native_completion(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            original = execute.load_plan
            entered, release = asyncio.Event(), asyncio.Event()

            def hold_load(owned):
                plan = original(owned)
                complete = plan.completion

                def factory(lease, mem):
                    current = complete(lease, mem)
                    proof = current.proof

                    async def blocked(obs):
                        entered.set()
                        await release.wait()
                        return await proof(obs)

                    return replace(current, proof=blocked)

                return replace(plan, completion=factory)

            execute.load_plan = hold_load
            first = asyncio.create_task(execute.command(load_call(b)))
            await entered.wait()
            before = len(b.wire.calls)
            second = asyncio.create_task(execute.command(call(b)))
            await asyncio.sleep(0)
            assert not second.done() and len(b.wire.calls) == before
            release.set()
            assert await first == {"status": "confirmed"}
            assert await second == {"status": "confirmed"}
            assert (
                b.coord.snapshot("shared").state == "idle"
                and b.memory.view("shared").committed_bytes == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("first_action", ["infer", "unload"])
def test_same_actor_inference_and_unload_wait_for_actual_terminality_or_settlement(
    tmp_path, backend, first_action
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            entered, release = asyncio.Event(), asyncio.Event()
            execute = scheduler(b)
            if first_action == "infer":
                original = execute.plan

                def hold(owned):
                    plan = original(owned)
                    warm = plan.warm

                    def factory(req, mem):
                        current = warm(req, mem)
                        terminal = current.terminal

                        async def blocked():
                            entered.set()
                            await release.wait()
                            return await terminal()

                        return replace(current, terminal=blocked)

                    return replace(plan, warm=factory)

                execute.plan = hold
                first = asyncio.create_task(collect(execute.stream(admission(b).call)))
                await entered.wait()
                before = len(b.wire.calls)
                second = asyncio.create_task(execute.command(call(b)))
            else:
                original = execute.unload_plan

                def hold(owned):
                    plan = original(owned)
                    produce = plan.release

                    def factory(step, mem):
                        current = produce(step, mem)

                        async def blocked():
                            assert b.coord.snapshot("shared").resident is None
                            assert b.memory.view("shared").committed_bytes == 600
                            entered.set()
                            await release.wait()
                            return sample(b.memory, now=b.now)

                        return replace(current, sample=blocked)

                    return replace(plan, release=factory)

                execute.unload_plan = hold
                first = asyncio.create_task(execute.command(call(b)))
                await entered.wait()
                before = len(b.wire.calls)
                second = asyncio.create_task(collect(execute.stream(admission(b).call)))
            await asyncio.sleep(0)
            assert not second.done() and len(b.wire.calls) == before
            release.set()
            one, two = await first, await second
            assert (one if first_action == "infer" else two)[-1]["type"] == "usage"
            assert (two if first_action == "infer" else one) == {"status": "confirmed"}
            assert b.count_calls == 1 and not execute._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_independent_load_actor_cannot_send_while_release_waits_for_actual_zero(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            entered, release = asyncio.Event(), asyncio.Event()

            async def blocked():
                entered.set()
                await release.wait()
                return sample(b.memory, now=b.now)

            execute = scheduler(
                b,
                alter=lambda plan: replace(
                    plan,
                    release=lambda step, mem: job(
                        b, step, SimpleNamespace(memory=mem), sample=blocked
                    ),
                ),
            )
            task = asyncio.create_task(execute.command(call(b)))
            await entered.wait()
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(load_call(b))
            assert len(b.wire.calls) == before
            assert b.coord.snapshot("shared").state == "switching"
            assert b.memory.view("shared").committed_bytes == 600
            release.set()
            assert await task == {"status": "confirmed"}
            assert b.memory.view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_foreign_fifo_owner_is_neither_skipped_nor_poisoned_by_unload_actor(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            b.coord.enqueue(
                "foreign",
                "shared",
                b.q.profile.memory.target,
                deadline_ms=40000,
                now_ms=b.now,
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "idle"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert request_row(b)["status"] == "cancelled"
            first = b.coord.claim_next("shared", now_ms=b.now, request_id="foreign")
            assert first.request_id == "foreign"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_historical_unload_completion_does_not_poison_independent_successor_claim(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            original = execute._confirmed_unload

            def next_claim(queued, step):
                b.coord.enqueue_load(
                    "successor",
                    "shared",
                    b.q.profile.memory.target,
                    deadline_ms=40000,
                    now_ms=b.now,
                )
                b.next = b.coord.claim_next(
                    "shared", now_ms=b.now, request_id="successor"
                )
                original(queued, step)

            monkeypatch.setattr(execute, "_confirmed_unload", next_claim)
            assert await execute.command(call(b)) == {"status": "confirmed"}
            assert b.next.kind == "load"
            assert b.coord.snapshot("shared").operation_status == "active"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("purpose", ["load", "inference"])
def test_miswired_unload_producer_cannot_consume_an_inference_or_load_slot(
    tmp_path, monkeypatch, backend, purpose
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            monkeypatch.setattr(
                b.coord,
                "enqueue_unload",
                b.coord.enqueue_load if purpose == "load" else b.coord.enqueue,
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("condition", ["cancelled", "timeout"])
def test_lock_wait_interruption_does_not_enqueue_or_poison_source_or_unlock_other_owner(
    tmp_path, backend, condition
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            execute = scheduler(
                b,
                alter=(lambda plan: replace(plan, deadline_ms=b.now + 1))
                if condition == "timeout"
                else None,
            )
            await execute._lock.acquire()
            task = asyncio.create_task(execute.command(call(b)))
            if condition == "cancelled":
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await task
            assert execute._lock.locked()
            execute._lock.release()
            assert request_row(b) is None and b.wire.calls == []
            assert b.coord.snapshot("shared").state == "idle"
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("action", ["unload_all", "clear_cache", "infer"])
def test_other_controls_cannot_borrow_optional_unload_plan(tmp_path, backend, action):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(replace(call(b), action=action))
            assert b.wire.calls == [] and request_row(b) is None

    asyncio.run(run())


def test_omlx_source_qualification_cannot_be_relabelled_as_unload_command_plan(
    tmp_path,
):
    from test_omlx_lifecycle_bridge import setup, permit
    from test_shared_memory_admission import load_step

    async def run():
        coord, memory, fleet, clock, wire, bridge = setup(tmp_path)
        with coord:
            step = load_step(coord)
            q = permit(coord, step, clock).qualification
            owned = rd.RuntimeDispatchCall("omlx", q.target.artifact_id, "unload")
            execute = ns.NativeInferenceScheduler(
                nt.NativeTransitionDriver(coord, memory, clock_ms=clock),
                unload_plan=lambda c: ns.NativeUnloadPlan(
                    admission=nt.UnloadAdmission(c, q),
                    domain_id="shared",
                    deadline_ms=40000,
                    release=lambda *args: None,
                ),
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(owned)
            assert wire.calls == [] and memory.view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_completed_row_and_released_memory_without_retirement_receipt_cannot_confirm(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            original = execute._confirmed_unload

            def missing_receipt(queued, step):
                b.coord._call(
                    lambda conn: conn.execute(
                        "DELETE FROM journal WHERE operation_id=? AND event='release_settled'",
                        (step.operation_id,),
                    )
                )
                original(queued, step)

            monkeypatch.setattr(execute, "_confirmed_unload", missing_receipt)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert b.coord.snapshot("shared").state == "idle"
            assert (
                b.memory.view("shared").committed_bytes == 0
                and b.memory.view("shared").state == "ready"
            )

    asyncio.run(run())
