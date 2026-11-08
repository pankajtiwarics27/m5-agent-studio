"""oMLX unload-only composition; installed/native facts are synthetic, never inferred."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_inference_scheduler as ns
import native_transition_driver as nt
import omlx_lifecycle as ox
import runtime_dispatch_boundary as rd
from shared_memory_admission import _model_step
from test_shared_memory_admission import profile, sample
import test_omlx_lifecycle_bridge as source

pytestmark = source.pytestmark


def call():
    return rd.RuntimeDispatchCall("omlx", source.A.artifact_id, "unload")


async def loaded(tmp_path):
    env = source.setup(tmp_path)
    lease = await source.start_loaded(*env)
    env[4].calls.clear()
    return env, lease


def admission(env, lease):
    coord, memory, fleet, clock, wire, bridge = env
    # This is the actual original source load binding, not a fake acquire-to-self.
    q = source.permit(coord, _model_step(lease.binding), clock).qualification
    return nt.OmlxUnloadAdmission(
        call(), q, profile(), coord.observer_clock_id, coord._max_observation_age
    )


def job(env, ready, step, mem):
    coord, memory, fleet, clock, wire, bridge = env
    permit = ox.OmlxPermit(
        step, ready.native, 100000, "fake-current-source-grant", True
    )

    async def measured():
        return sample(memory, now=clock.now)

    async def proof(obs):
        assert memory.view("shared").committed_bytes == 600
        return fleet.evidence(coord, clock.now, reference=obs.reference)

    return nt.OmlxRelease(
        bridge=bridge,
        permit=permit,
        profile=ready.profile,
        activity=source.activity(coord, permit, clock),
        memory=mem,
        sample=measured,
        proof=proof,
    )


def scheduler(env, lease, *, alter=None):
    coord, memory, fleet, clock, wire, bridge = env
    ready = admission(env, lease)

    def resolve(owned):
        plan = ns.NativeUnloadPlan(
            admission=replace(ready, call=owned),
            domain_id="shared",
            deadline_ms=40000,
            release=lambda step, mem: job(env, ready, step, mem),
        )
        return alter(plan) if alter else plan

    return ns.NativeInferenceScheduler(
        nt.NativeTransitionDriver(coord, memory, clock_ms=clock), unload_plan=resolve
    )


def row(env):
    return env[0]._call(
        lambda conn: conn.execute(
            "SELECT * FROM requests WHERE purpose='unload' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    )


@pytest.mark.parametrize("status", [200, 202])
def test_real_omlx_unload_only_requires_physical_absence_independent_zero_and_durable_retirement(
    tmp_path, status
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            if status == 202:

                async def pending(req):
                    fleet.models.pop("omlx")
                    wire.reply = (
                        202,
                        {"status": "unloading", "model_id": source.A.artifact_id},
                    )

                wire.hook = pending

            def alter(plan):
                produce = plan.release

                def release(step, mem):
                    assert not hasattr(step, "target")
                    current = produce(step, mem)

                    async def zero():
                        assert coord.snapshot("shared").resident is None
                        assert coord.snapshot("shared").state == "switching"
                        assert coord.snapshot("shared").operation_status == "complete"
                        assert memory.view("shared").committed_bytes == 600
                        coord.enqueue_load(
                            "next",
                            "shared",
                            source.B,
                            deadline_ms=40000,
                            now_ms=clock.now,
                        )
                        assert (
                            coord.claim_next(
                                "shared", now_ms=clock.now, request_id="next"
                            )
                            is None
                        )
                        return sample(memory, now=clock.now)

                    return replace(current, sample=zero)

                return replace(plan, release=release)

            execute = scheduler(env, lease, alter=alter)
            boundary = rd.RuntimeDispatchBoundary(True, {"omlx": execute})
            assert await boundary.command("omlx", call().model, "unload") == {
                "status": "confirmed"
            }
            assert len(wire.posts) == 1 and wire.posts[0].path.endswith("/unload")
            assert (
                memory.view("shared").committed_bytes == 0
                and memory.view("shared").state == "ready"
            )
            assert (
                coord.snapshot("shared").state == "idle"
                and row(env)["status"] == "completed"
            )
            assert not fleet.models
            before = len(wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call())
            assert len(wire.calls) == before  # No already-absent success or re-probe.
            step = coord.claim_next("shared", now_ms=clock.now, request_id="next")
            assert step.kind == "load" and step.target == source.B

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "capabilities",
        "stale",
        "revision",
        "clock",
        "policy_age",
        "context",
        "memory_profile",
        "wrong_runtime",
        "remote",
        "payload",
        "wrong_action",
    ],
)
def test_omlx_unload_readiness_refuses_before_enqueue_probe_or_disruption(
    tmp_path, fault
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:

            def alter(plan):
                ready = plan.admission
                if fault in (
                    "capabilities",
                    "stale",
                    "revision",
                    "wrong_runtime",
                    "remote",
                ):
                    q = ready.native
                    q = replace(
                        q,
                        **(
                            {"capabilities": ()}
                            if fault == "capabilities"
                            else {"stamp": replace(q.stamp, observed_at=0)}
                            if fault == "stale"
                            else {"source_revision": "unqualified"}
                            if fault == "revision"
                            else {"runtime": replace(q.runtime, backend="ollama")}
                            if fault == "wrong_runtime"
                            else {"runtime": replace(q.runtime, location="remote")}
                        ),
                    )
                    ready = replace(ready, native=q)
                    if fault == "stale":
                        clock.now = 6000
                elif fault == "clock":
                    ready = replace(
                        ready,
                        clock_id="foreign-clock",
                        native=replace(
                            ready.native,
                            stamp=replace(ready.native.stamp, clock_id="foreign-clock"),
                        ),
                    )
                elif fault == "policy_age":
                    ready = replace(ready, max_age=9999)
                elif fault in ("context", "memory_profile"):
                    ready = replace(
                        ready,
                        profile=replace(
                            ready.profile,
                            **(
                                {"context_tokens": 4096}
                                if fault == "context"
                                else {"weights_bytes": ready.profile.weights_bytes + 1}
                            ),
                        ),
                    )
                else:
                    ready = replace(
                        ready,
                        call=replace(
                            ready.call,
                            **(
                                {"payload": b'{"unload":true}'}
                                if fault == "payload"
                                else {"action": "load"}
                            ),
                        ),
                    )
                return replace(plan, admission=ready)

            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(env, lease, alter=alter).command(call())
            assert wire.calls == [] and row(env) is None
            assert (
                coord.snapshot("shared").state == "idle"
                and memory.view("shared").committed_bytes == 600
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "pinned",
        "helper",
        "distributed",
        "external",
        "loading",
        "original_profile",
        "wrong_profile_job",
        "wrong_memory",
        "activity",
        "denied",
        "malformed_ack",
        "not_absent",
        "ack_only",
        "scope",
        "version",
        "terminal",
        "nonzero",
        "sample_error",
        "factory",
    ],
)
def test_omlx_unload_uncertainty_never_confirms_releases_memory_or_replays(
    tmp_path, fault
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            if fault in ("pinned", "helper", "distributed", "external", "loading"):
                wire.row_changes.update(
                    {
                        "pinned": {"pinned": True},
                        "helper": {"is_helper": True},
                        "distributed": {"distributed": True},
                        "external": {"external_engine": "unknown"},
                        "loading": {"is_loading": True},
                    }[fault]
                )
            if fault == "original_profile":
                coord._call(
                    lambda conn: conn.execute(
                        "DELETE FROM journal WHERE event='omlx_profile_load'"
                    )
                )
            if fault == "denied":
                bridge._authorize = None
            if fault == "malformed_ack":
                wire.reply = (200, {"status": "ok", "model_id": "wrong-model"})
            if fault == "not_absent":
                wire.reply = (200, {"status": "ok", "model_id": source.A.artifact_id})

            def alter(plan):
                produce = plan.release

                def release(step, mem):
                    if fault == "factory":
                        raise RuntimeError("secret")
                    current = produce(step, mem)
                    if fault == "wrong_profile_job":
                        return replace(
                            current,
                            profile=replace(
                                current.profile,
                                weights_bytes=current.profile.weights_bytes + 1,
                            ),
                        )
                    if fault == "wrong_memory":
                        return replace(current, memory=replace(mem, lease_id="wrong"))
                    if fault == "activity":
                        return replace(
                            current,
                            activity=replace(current.activity, active_streams=None),
                        )
                    original = current.proof

                    async def proof(obs):
                        evidence = await original(obs)
                        if fault == "ack_only":
                            return {"status": "ok"}
                        if fault == "scope":
                            return replace(
                                evidence,
                                scope=replace(evidence.scope, operation_id="wrong"),
                            )
                        if fault == "version":
                            return replace(
                                evidence,
                                inventories=tuple(
                                    replace(i, runtime_version="changed")
                                    if i.runtime.runtime_id == "omlx"
                                    else i
                                    for i in evidence.inventories
                                ),
                            )
                        if fault == "terminal":
                            return replace(evidence, operations_terminal=False)
                        return evidence

                    async def measured():
                        if fault == "sample_error":
                            raise OSError("secret")
                        return sample(
                            memory, now=clock.now, attributed=((mem.lease_id, 1),)
                        )

                    if fault in ("ack_only", "scope", "version", "terminal"):
                        return replace(current, proof=proof)
                    if fault in ("nonzero", "sample_error"):
                        return replace(current, sample=measured)
                    return current

                return replace(plan, release=release)

            execute = scheduler(env, lease, alter=alter)
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                await execute.command(call())
            assert "secret" not in str(error.value)
            assert (
                coord.snapshot("shared").state == "reconciling"
                and coord.snapshot("shared").operation_status == "uncertain"
            )
            assert (
                memory.view("shared").committed_bytes == 600
                and memory.view("shared").state == "paused"
            )
            assert row(env)["status"] == "cancelled" and not execute._lock.locked()
            before = len(wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call())
            assert len(wire.calls) == before

    asyncio.run(run())


@pytest.mark.parametrize("condition", ["cancelled", "expired"])
def test_post_send_omlx_cleanup_retires_actual_resources_without_api_success(
    tmp_path, condition
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:

            async def after_send(req):
                if condition == "cancelled":
                    coord.cancel(row(env)["id"], now_ms=clock.now)
                else:
                    clock.now = 1003

            wire.hook = after_send
            execute = scheduler(
                env, lease, alter=lambda plan: replace(plan, deadline_ms=1002)
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(call())
            assert row(env)["status"] == condition
            assert coord.snapshot("shared").state == "idle"
            assert (
                memory.view("shared").committed_bytes == 0
                and memory.view("shared").state == "ready"
            )

    asyncio.run(run())


def test_omlx_source_unload_does_not_require_target_load_or_inference_qualification(
    tmp_path,
):
    async def run():
        env, lease = await loaded(tmp_path)
        with env[0]:

            def alter(plan):
                ready = plan.admission
                q = replace(
                    ready.native,
                    capabilities=tuple(
                        c
                        for c in ready.native.capabilities
                        if c.name != "blocking_load"
                    ),
                )
                ready = replace(ready, native=q)
                return replace(
                    plan,
                    admission=ready,
                    release=lambda step, mem: job(env, ready, step, mem),
                )

            assert await scheduler(env, lease, alter=alter).command(call()) == {
                "status": "confirmed"
            }
            with pytest.raises(rd.RuntimeDispatchDenied):
                nt.LoadAdmission(
                    replace(call(), action="load"), admission(env, lease).native
                ).validate(env[3].now)
            assert env[1].view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["proof", "sample"])
def test_cancelled_omlx_producer_retains_charged_uncertain_source_and_no_replay(
    tmp_path, phase
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            entered = asyncio.Event()

            async def blocked(*args):
                entered.set()
                await asyncio.Future()

            def alter(plan):
                produce = plan.release

                def release(step, mem):
                    current = produce(step, mem)
                    return replace(current, **{phase: blocked})

                return replace(plan, release=release)

            execute = scheduler(env, lease, alter=alter)
            task = asyncio.create_task(execute.command(call()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert coord.snapshot("shared").state == "reconciling"
            assert (
                memory.view("shared").committed_bytes == 600
                and memory.view("shared").state == "paused"
            )
            assert row(env)["status"] == "cancelled" and not execute._lock.locked()
            assert len(wire.posts) == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_independent_target_cannot_send_until_omlx_actual_source_settlement_finishes(
    tmp_path, monkeypatch, backend
):
    from test_omlx_dispatch_integration import environment
    from test_native_load_control import (
        scheduler as target_scheduler,
        call as target_call,
    )

    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            lease = await source.start_loaded(*env)
            wire.calls.clear()
            entered, release = asyncio.Event(), asyncio.Event()

            async def blocked():
                entered.set()
                await release.wait()
                return sample(memory, now=clock.now)

            def alter(plan):
                produce = plan.release
                return replace(
                    plan,
                    release=lambda step, mem: replace(
                        produce(step, mem), sample=blocked
                    ),
                )

            execute = scheduler(env, lease, alter=alter)
            task = asyncio.create_task(execute.command(call()))
            await entered.wait()
            with pytest.raises(rd.RuntimeDispatchDenied):
                await target_scheduler(b).command(target_call(b))
            assert b.wire.calls == [] and memory.view("shared").committed_bytes == 600
            assert coord.snapshot("shared").state == "switching"
            release.set()
            assert await task == {"status": "confirmed"}
            assert await target_scheduler(b).command(target_call(b)) == {
                "status": "confirmed"
            }
            assert memory.view("shared").committed_bytes == 600
            assert fleet.models == {backend: b.q.profile.memory.target}

    asyncio.run(run())


def test_omlx_native_confirmation_requires_real_current_release_ack_before_probe(
    tmp_path,
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            driver = nt.NativeTransitionDriver(coord, memory, clock_ms=clock)
            ready = admission(env, lease)
            queued = driver.enqueue_unload(
                ready, "release", "shared", deadline_ms=40000
            )
            drain = coord.claim_next("shared", now_ms=clock.now)
            await driver.advance_release(drain, queued, job(env, ready, drain, lease))
            unload = coord.claim_next("shared", now_ms=clock.now)
            current = job(env, ready, unload, lease)
            with pytest.raises(ox.OmlxError, match="release_ack_missing"):
                await bridge.confirm(current.permit, current.proof)
            assert wire.calls == [] and memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["old_job", "generic_bridge", "missing_sample", "missing_proof"]
)
def test_wrong_omlx_release_job_cannot_supply_native_authority(tmp_path, kind):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:

            def alter(plan):
                produce = plan.release

                def release(step, mem):
                    current = produce(step, mem)
                    if kind == "old_job":
                        return nt.OmlxSourceTransition(
                            bridge=bridge,
                            permit=current.permit,
                            profile=current.profile,
                            memory=mem,
                            activity=current.activity,
                        )
                    if kind == "generic_bridge":
                        return replace(current, bridge=object())
                    return replace(
                        current,
                        **{"sample" if kind == "missing_sample" else "proof": None},
                    )

                return replace(plan, release=release)

            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(env, lease, alter=alter).command(call())
            assert wire.calls == [] and memory.view("shared").committed_bytes == 600
            assert coord.snapshot("shared").state == "reconciling"

    asyncio.run(run())


def test_omlx_profile_record_cannot_be_reused_for_different_actual_source_bound(
    tmp_path,
):
    async def run():
        env, lease = await loaded(tmp_path)
        coord, memory, fleet, clock, wire, bridge = env
        with coord:
            coord._call(
                lambda conn: conn.execute(
                    "UPDATE journal SET reference='wrong-profile' WHERE event='omlx_profile_load'"
                )
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(env, lease).command(call())
            assert wire.calls == [] and memory.view("shared").committed_bytes == 600

    asyncio.run(run())
