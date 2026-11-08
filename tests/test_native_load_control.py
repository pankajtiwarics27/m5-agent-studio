"""Dormant explicit load consumer: real kernels, fake installed/native facts only."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_inference_scheduler as ns
import native_transition_driver as nt
import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture, admission, job
from test_native_inference_scheduler import scheduler as inference_scheduler, collect
from test_shared_memory_admission import sample


def call(b):
    return rd.RuntimeDispatchCall(
        b.backend, b.q.profile.memory.target.artifact_id, "load"
    )


def completion(b, lease, mem, **changes):
    async def measured():
        return sample(b.memory, now=b.now)

    cls = nt.OllamaLoadCompletion if b.backend == "ollama" else nt.LmLoadCompletion
    return replace(
        cls(
            bridge=b.bridge,
            permit=b.driver.permit(b, lease),
            memory=mem,
            sample=measured,
            proof=lambda obs: b.driver.proof(b, obs),
        ),
        **changes,
    )


def scheduler(b, *, alter=None):
    existing = inference_scheduler(b)

    def load_plan(owned):
        plan = ns.NativeLoadPlan(
            admission=nt.LoadAdmission(owned, b.q),
            domain_id="shared",
            deadline_ms=40000,
            transition=lambda step, mem: job(b, step, mem),
            completion=lambda lease, mem: completion(b, lease, mem),
        )
        return alter(plan) if alter else plan

    existing.load_plan = load_plan
    return existing


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_real_cold_then_warm_load_has_no_inference_count_and_reuses_model(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            boundary = rd.RuntimeDispatchBoundary(True, {backend: execute})
            assert await boundary.command(backend, call(b).model, "load") == {
                "status": "confirmed"
            }
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "ready"
            assert len(b.driver.mutations(b)) == 1
            assert getattr(b, "count_calls", 0) == 0
            assert await execute.command(call(b)) == {"status": "confirmed"}
            assert len(b.driver.mutations(b)) == 1
            assert getattr(b, "count_calls", 0) == 0
            assert (await collect(execute.stream(admission(b).call)))[-1][
                "type"
            ] == "usage"
            assert b.count_calls == 1 and len(b.driver.mutations(b)) == 2
            assert b.coord.snapshot("shared").active_requests == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "unqualified",
        "changed_call",
        "wrong_runtime",
        "payload",
        "wrong_plan",
    ],
)
def test_bad_load_readiness_refuses_before_queue_or_probe(tmp_path, backend, fault):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            def alter(plan):
                if fault == "wrong_plan":
                    return object()
                ready = plan.admission
                if fault in ("expired", "unqualified"):
                    ready = replace(
                        ready,
                        native=replace(
                            b.q,
                            **(
                                {"expires_ms": 1000}
                                if fault == "expired"
                                else {"capabilities": ()}
                            ),
                        ),
                    )
                else:
                    owned = replace(
                        ready.call,
                        **(
                            {"model": "another-model"}
                            if fault == "changed_call"
                            else {"runtime": "omlx"}
                            if fault == "wrong_runtime"
                            else {"payload": b'{"num_ctx":16384}'}
                        ),
                    )
                    ready = replace(ready, call=owned)
                return replace(plan, admission=ready)

            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b, alter=alter).command(call(b))
            assert b.wire.calls == [] and b.coord.snapshot("shared").queued == 0
            assert b.memory.view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "ack_only",
        "proof_error",
        "wrong_instance",
        "wrong_scope",
        "context",
        "native_version",
        "profile",
        "memory",
        "missing_sample",
        "wrong_permit",
        "factory",
        "native_profile_record",
        "pressure",
        "late_action",
        "denied",
    ],
)
def test_load_completion_uncertainty_never_reports_confirmed(tmp_path, backend, fault):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            def produce(lease, mem):
                if fault == "factory":
                    raise RuntimeError("provider-secret")
                current = completion(b, lease, mem)
                original = current.proof

                async def proof(obs):
                    evidence = await original(obs)
                    if fault == "ack_only":
                        return {"status": "ok"}
                    if fault == "proof_error":
                        raise RuntimeError("provider-secret")
                    if fault == "wrong_scope":
                        return replace(
                            evidence,
                            scope=replace(evidence.scope, operation_id="other-control"),
                        )
                    if fault in ("wrong_instance", "context", "native_version"):
                        inventories = []
                        for inv in evidence.inventories:
                            if inv.runtime.runtime_id == backend:
                                instances = tuple(
                                    replace(
                                        i,
                                        **(
                                            {"instance_id": "other-instance"}
                                            if fault == "wrong_instance"
                                            else {"effective_context_tokens": 4096}
                                            if fault == "context"
                                            else {}
                                        ),
                                    )
                                    for i in inv.instances
                                )
                                inv = replace(
                                    inv,
                                    instances=instances,
                                    **(
                                        {"runtime_version": "other-version"}
                                        if fault == "native_version"
                                        else {}
                                    ),
                                )
                            inventories.append(inv)
                        evidence = replace(evidence, inventories=tuple(inventories))
                        if fault == "wrong_instance":
                            evidence = replace(
                                evidence,
                                owned_instance_ids=((backend, "other-instance"),),
                            )
                    if fault == "late_action":
                        from test_shared_memory_admission import (
                            reserve_action,
                            footprint,
                        )

                        # Small action fits, but invalidates the pre-proof memory
                        # sample's revision. Final SAME-write memory gate must refuse.
                        reserve_action(
                            b.memory,
                            "shared",
                            "late-tool",
                            footprint(50),
                            sample(b.memory, now=b.now),
                            now_ms=b.now,
                        )
                    return evidence

                current = replace(current, proof=proof)
                if fault == "profile":
                    current = replace(
                        current,
                        permit=replace(
                            current.permit,
                            qualification=replace(
                                b.q, reference="other-qualified-scope"
                            ),
                        ),
                    )
                elif fault == "memory":
                    current = replace(
                        current, memory=replace(mem, work_id="not-the-real-model")
                    )
                elif fault == "missing_sample":
                    current = replace(current, sample=None)
                elif fault == "wrong_permit":
                    current = replace(
                        current,
                        permit=replace(
                            current.permit,
                            intent=replace(lease, instance_id="wrong-instance"),
                        ),
                    )
                elif fault == "native_profile_record":
                    b.coord._call(
                        lambda conn: conn.execute(
                            "UPDATE journal SET reference='wrong-profile' WHERE event IN ('ollama_profile_load','lmstudio_profile_load')"
                        ),
                        now_ms=b.now,
                    )
                elif fault == "pressure":

                    async def pressure():
                        return sample(b.memory, now=b.now, pressure="critical")

                    current = replace(current, sample=pressure)
                elif fault == "denied":
                    b.authorized = False
                return current

            execute = scheduler(b, alter=lambda plan: replace(plan, completion=produce))
            with pytest.raises(rd.RuntimeDispatchDenied) as caught:
                await execute.command(call(b))
            assert "provider-secret" not in str(caught.value)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes >= 600
            assert b.memory.view("shared").state == "paused"
            assert getattr(b, "count_calls", 0) == 0
            assert len(b.driver.mutations(b)) == 1
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("active", ["inference", "control"])
def test_same_actor_serializes_control_and_inference_until_terminal(
    tmp_path, backend, active
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            entered, release = asyncio.Event(), asyncio.Event()
            execute = scheduler(b)
            if active == "control":
                original = execute.load_plan

                def delayed(owned):
                    plan = original(owned)

                    def final(lease, mem):
                        current = plan.completion(lease, mem)

                        async def proof(obs):
                            entered.set()
                            await release.wait()
                            return await current.proof(obs)

                        return replace(current, proof=proof)

                    return replace(plan, completion=final)

                execute.load_plan = delayed
                first = asyncio.create_task(execute.command(call(b)))
                await entered.wait()
                second = asyncio.create_task(collect(execute.stream(admission(b).call)))
            else:
                # Warm executor's terminal producer is independently gated.
                original = execute.plan

                def delayed(owned):
                    plan = original(owned)

                    def final(req, mem):
                        current = plan.warm(req, mem)

                        async def terminal():
                            entered.set()
                            await release.wait()
                            return await current.terminal()

                        return replace(current, terminal=terminal)

                    return replace(plan, warm=final)

                execute.plan = delayed
                first = asyncio.create_task(collect(execute.stream(admission(b).call)))
                await entered.wait()
                second = asyncio.create_task(execute.command(call(b)))
            await asyncio.sleep(0)
            assert not second.done() and b.coord.snapshot("shared").active_requests == 1
            release.set()
            await first
            await second
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").state == "ready"
            assert b.count_calls == 1 and len(b.driver.mutations(b)) == 2

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_cancelled_final_proof_retains_control_and_cannot_replay(tmp_path, backend):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            entered = asyncio.Event()

            def final(lease, mem):
                async def hang(obs):
                    entered.set()
                    await asyncio.Future()

                return completion(b, lease, mem, proof=hang)

            execute = scheduler(b, alter=lambda plan: replace(plan, completion=final))
            task = asyncio.create_task(execute.command(call(b)))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            before = len(b.driver.mutations(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert len(b.driver.mutations(b)) == before == 1
            assert b.memory.view("shared").state == "paused"
            assert b.coord.snapshot("shared").active_requests == 1

    asyncio.run(run())


@pytest.mark.parametrize("source_backend", ["ollama", "lmstudio", "omlx"])
def test_cross_runtime_explicit_load_waits_for_real_source_release(
    tmp_path, monkeypatch, source_backend
):
    async def run():
        if source_backend == "omlx":
            import test_omlx_dispatch_integration as ox

            env, target = ox.environment(tmp_path, monkeypatch, "ollama")
            execute = scheduler(target)
            with target.coord:
                await ox.source.start_loaded(*env)
                original = execute.load_plan

                def switched(owned):
                    plan = original(owned)
                    return replace(
                        plan,
                        transition=lambda step, mem: (
                            ox.source_job(env, step, mem)
                            if step.kind in ("drain", "unload")
                            else job(target, step, mem)
                        ),
                    )

                execute.load_plan = switched
                assert await execute.command(call(target)) == {"status": "confirmed"}
                assert "omlx" not in env[2].models
                assert len(target.driver.mutations(target)) == 1
                assert target.memory.view("shared").committed_bytes == 600
            return
        source = fixture(tmp_path, source_backend)
        other = "lmstudio" if source_backend == "ollama" else "ollama"
        if other == "lmstudio":
            import test_lmstudio_lifecycle_bridge as other_driver
        else:
            import test_ollama_lifecycle_bridge as other_driver
        monkeypatch.setattr(
            other_driver,
            "opened",
            lambda _: (source.coord, source.memory, source.fleet),
        )
        target = fixture(tmp_path, other)
        with source.coord:
            execute = scheduler(source)
            assert await execute.command(call(source)) == {"status": "confirmed"}
            target_resolve = scheduler(target).load_plan

            def switched(owned):
                plan = target_resolve(owned)

                def step_job(step, mem):
                    if step.kind in ("drain", "unload"):
                        return job(source, step, mem)
                    assert source.memory.view("shared").committed_bytes == (
                        0 if step.kind == "load" else 600
                    )
                    assert source_backend not in source.fleet.models
                    return job(target, step, mem)

                return replace(plan, transition=step_job)

            execute.load_plan = switched
            assert await execute.command(call(target)) == {"status": "confirmed"}
            assert (
                source.coord.snapshot("shared").resident
                == target.q.profile.memory.target
            )
            assert source.memory.view("shared").committed_bytes == 600
            assert len(target.driver.mutations(target)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_independent_control_actor_refuses_busy_inference_without_touching_owner(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            active = scheduler(b)
            entered, release = asyncio.Event(), asyncio.Event()
            original = active.plan

            def delayed(owned):
                plan = original(owned)

                def final(req, mem):
                    current = plan.warm(req, mem)

                    async def terminal():
                        entered.set()
                        await release.wait()
                        return await current.terminal()

                    return replace(current, terminal=terminal)

                return replace(plan, warm=final)

            active.plan = delayed
            owner = asyncio.create_task(collect(active.stream(admission(b).call)))
            await entered.wait()
            calls = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert len(b.wire.calls) == calls
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.memory.view("shared").state == "ready"
            release.set()
            assert (await owner)[-1]["type"] == "usage"
            assert b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "phase", ["waiter", "stage_factory", "expired_final", "cancelled_final"]
)
def test_control_interruptions_do_not_invent_native_completion(
    tmp_path, backend, phase
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            if phase == "waiter":
                await execute._lock.acquire()
                task = asyncio.create_task(execute.command(call(b)))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert b.wire.calls == [] and b.coord.snapshot("shared").queued == 0
                assert execute._lock.locked()
                execute._lock.release()
            elif phase == "stage_factory":

                def broken(step, mem):
                    raise RuntimeError("provider-secret")

                execute = scheduler(
                    b, alter=lambda plan: replace(plan, transition=broken)
                )
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await execute.command(call(b))
                assert (
                    b.wire.calls == []
                    and b.coord.snapshot("shared").state == "reconciling"
                )
            else:

                def final(lease, mem):
                    current = completion(b, lease, mem)

                    async def proof(obs):
                        if phase == "expired_final":
                            b.now = 40000
                        else:
                            assert (
                                b.coord.cancel(lease.request_id, now_ms=b.now)
                                == "pending_terminal"
                            )
                        return await current.proof(obs)

                    return replace(current, proof=proof)

                execute = scheduler(
                    b, alter=lambda plan: replace(plan, completion=final)
                )
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await execute.command(call(b))
                assert b.memory.view("shared").state == "paused"
                assert b.memory.view("shared").committed_bytes == 600
                assert b.coord.snapshot("shared").active_requests == 1

    asyncio.run(run())


@pytest.mark.parametrize("action", ["unload", "unload_all", "clear_cache"])
def test_unsupported_controls_refuse_even_with_load_plan(tmp_path, action):
    async def run():
        b = fixture(tmp_path, "ollama")
        with b.coord:
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(replace(call(b), action=action))
            assert b.wire.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("failure", ["foreign_fifo", "expiry_after_wait"])
def test_control_waiting_never_steals_fifo_or_uses_expired_readiness(
    tmp_path, backend, failure
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            if failure == "foreign_fifo":
                b.coord.enqueue(
                    "foreign",
                    "shared",
                    b.q.profile.memory.target,
                    deadline_ms=100000,
                    now_ms=b.now,
                )
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await execute.command(call(b))
                assert b.coord.snapshot("shared").queued == 1
            else:
                await execute._lock.acquire()
                task = asyncio.create_task(execute.command(call(b)))
                await asyncio.sleep(0)
                b.now = 50000
                execute._lock.release()
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await task
                assert b.coord.snapshot("shared").queued == 0
            assert b.wire.calls == [] and b.memory.view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_generic_completion_bridge_is_not_authority(tmp_path, backend):
    from types import SimpleNamespace

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            invoked = []

            async def generic(*args, **kwargs):
                invoked.append(True)
                return True

            fake = SimpleNamespace(
                coordinator=b.coord,
                coord=b.coord,
                memory=b.memory,
                confirm_load=generic,
            )
            execute = scheduler(
                b,
                alter=lambda plan: replace(
                    plan,
                    completion=lambda lease, mem: completion(
                        b, lease, mem, bridge=fake
                    ),
                ),
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call(b))
            assert invoked == [] and b.memory.view("shared").state == "paused"

    asyncio.run(run())


def test_omlx_load_target_is_not_qualified_by_its_source_lifecycle_bridge(tmp_path):
    from test_omlx_lifecycle_bridge import setup, permit
    from test_shared_memory_admission import load_step

    async def run():
        env = setup(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            step = load_step(coord)
            qualifier = permit(coord, step, clock).qualification
            owned = rd.RuntimeDispatchCall("omlx", qualifier.target.artifact_id, "load")
            execute = ns.NativeInferenceScheduler(
                nt.NativeTransitionDriver(coord, memory, clock_ms=clock),
                load_plan=lambda c: ns.NativeLoadPlan(
                    admission=nt.LoadAdmission(c, qualifier),
                    domain_id="shared",
                    deadline_ms=40000,
                    transition=lambda *args: None,
                    completion=lambda *args: None,
                ),
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(owned)
            assert wire.calls == [] and memory.view("shared").committed_bytes == 0

    asyncio.run(run())
