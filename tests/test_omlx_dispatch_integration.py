"""LC08 source-only oMLX composition. Every installed/native fact is synthetic."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_inference_scheduler as ns
import native_transition_driver as nt
import runtime_dispatch_boundary as rd
from test_native_inference_scheduler import collect, warm
from test_native_transition_driver import fixture, admission, job
import test_omlx_lifecycle_bridge as source
from test_shared_memory_admission import profile, sample

pytestmark = source.pytestmark


def environment(tmp_path, monkeypatch, backend):
    env = source.setup(tmp_path)
    coord, memory, fleet, clock, wire, bridge = env
    if backend == "ollama":
        import test_ollama_lifecycle_bridge as target
    else:
        import test_lmstudio_lifecycle_bridge as target
    monkeypatch.setattr(target, "opened", lambda _: (coord, memory, fleet))
    b = fixture(tmp_path, backend)
    return env, b


def source_job(env, step, mem):
    coord, memory, fleet, clock, wire, bridge = env
    permit = source.permit(coord, step, clock)

    async def observed():
        return sample(memory, now=clock.now)

    async def proof(observation):
        # An ACK/status never reduces admission. Even after physical completion,
        # the source's reservation remains charged until independent settlement.
        assert memory.view("shared").committed_bytes == 600
        return fleet.evidence(coord, clock.now, reference=observation.reference)

    return nt.OmlxSourceTransition(
        bridge=bridge,
        permit=permit,
        profile=profile(),
        activity=source.activity(coord, permit, clock),
        memory=mem,
        sample=observed,
        proof=proof,
    )


def scheduler(env, b):
    def resolve(call):
        ready = replace(admission(b), call=call)

        def transition(step, mem):
            if step.kind in ("drain", "unload"):
                return source_job(env, step, mem)
            assert env[1].view("shared").committed_bytes == (
                0 if step.kind == "load" else 600
            )
            assert "omlx" not in env[2].models
            return job(b, step, mem)

        return ns.NativeInferencePlan(
            admission=ready,
            domain_id="shared",
            deadline_ms=40000,
            transition=transition,
            warm=lambda req, mem: warm(b, ready, req, mem),
        )

    return ns.NativeInferenceScheduler(b.transition, resolve)


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("ack", [200, 202])
def test_scheduler_releases_owned_omlx_before_target_load_and_exact_inference(
    tmp_path, monkeypatch, backend, ack
):
    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        with b.coord:
            await source.start_loaded(*env)
            env[4].calls.clear()
            if ack == 202:
                env[4].reply = (
                    202,
                    {"status": "unloading", "model_id": source.A.artifact_id},
                )

                async def release(request):
                    assert request.path.endswith("/unload")
                    env[2].models.pop("omlx")

                env[4].hook = release
            events = await collect(scheduler(env, b).stream(admission(b).call))
            assert events[-1]["type"] == "usage"
            assert b.count_calls == 1
            assert [r.path for r in env[4].posts] == ["/v1/models/model-a/unload"]
            assert len(b.driver.mutations(b)) == 2  # target load + native inference
            assert b.coord.snapshot("shared").resident == b.q.profile.memory.target
            assert b.memory.view("shared").committed_bytes == 600
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_missing_target_counter_prevents_even_source_drain(
    tmp_path, monkeypatch, backend
):
    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        with b.coord:
            await source.start_loaded(*env)
            env[4].calls.clear()
            execute = scheduler(env, b)
            original = execute.plan
            execute.plan = lambda call: replace(
                original(call), admission=replace(admission(b), counter=None)
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(execute.stream(admission(b).call))
            assert env[4].calls == [] and b.wire.calls == []
            assert env[2].models == {"omlx": source.A}
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


async def unloaded_stage(env, b):
    lease = await source.start_loaded(*env)
    env[4].calls.clear()
    queued = b.transition.enqueue(
        admission(b), "target-request", "shared", deadline_ms=40000
    )
    drain = b.coord.claim_next("shared", now_ms=b.now)
    await b.transition.advance(drain, queued, source_job(env, drain, lease))
    unload = b.coord.claim_next("shared", now_ms=b.now)
    assert unload.kind == "unload"
    return queued, unload, lease


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "failure",
    [
        "ack_only",
        "native_failure",
        "missing_proof",
        "proof_failure",
        "incomplete_proof",
        "missing_sample",
        "attributed_live",
        "profile_binding",
        "profile_context",
        "profile_target",
        "permit_scope",
        "wrong_coordinator",
        "wrong_memory",
        "not_authorized",
        "unqualified",
        "live_stream",
        "unknown_helpers",
        "pinned",
        "missing_memory",
    ],
)
def test_source_uncertainty_never_sends_target_load(
    tmp_path, monkeypatch, backend, failure
):
    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        with b.coord:
            queued, step, lease = await unloaded_stage(env, b)
            current = source_job(env, step, lease)
            if failure == "ack_only":
                env[4].reply = (200, {"status": "ok", "model_id": source.A.artifact_id})
            elif failure == "native_failure":
                env[4].reply = (500, {"detail": "secret-provider-error"})
            elif failure == "missing_proof":
                current = replace(current, proof=None)
            elif failure == "proof_failure":

                async def broken(obs):
                    raise RuntimeError("secret-provider-error")

                current = replace(current, proof=broken)
            elif failure == "incomplete_proof":

                async def incomplete(obs):
                    proof = env[2].evidence(b.coord, b.now, reference=obs.reference)
                    return replace(proof, inventories=proof.inventories[1:])

                current = replace(current, proof=incomplete)
            elif failure == "missing_sample":
                current = replace(current, sample=None)
            elif failure == "attributed_live":

                async def live():
                    return sample(
                        b.memory, now=b.now, attributed=((lease.lease_id, 1),)
                    )

                current = replace(current, sample=live)
            elif failure == "profile_binding":
                current = replace(current, profile=profile(amount=601))
            elif failure == "profile_context":
                current = replace(
                    current, profile=replace(profile(), context_tokens=4096)
                )
            elif failure == "profile_target":
                current = replace(
                    current, profile=profile(target=b.q.profile.memory.target)
                )
            elif failure == "permit_scope":
                current = replace(
                    current,
                    permit=replace(
                        current.permit, step=replace(step, operation_id="wrong-step")
                    ),
                )
            elif failure == "wrong_coordinator":
                monkeypatch.setattr(env[5], "_coordinator", object())
            elif failure == "wrong_memory":
                monkeypatch.setattr(env[5], "_memory", object())
            elif failure == "not_authorized":
                current = replace(
                    current, permit=replace(current.permit, authorized=False)
                )
            elif failure == "unqualified":
                current = replace(
                    current,
                    permit=replace(
                        current.permit,
                        qualification=replace(
                            current.permit.qualification, capabilities=()
                        ),
                    ),
                )
            elif failure in ("live_stream", "unknown_helpers"):
                activity = replace(
                    current.activity,
                    **(
                        {"active_streams": 1}
                        if failure == "live_stream"
                        else {"helper_visibility_complete": False}
                    ),
                )
                current = replace(current, activity=activity)
            elif failure == "pinned":
                env[4].row_changes["pinned"] = True
            elif failure == "missing_memory":
                current = replace(current, memory=None)
            with pytest.raises(rd.RuntimeDispatchDenied) as caught:
                await b.transition.advance(step, queued, current)
            assert "secret-provider-error" not in str(caught.value)
            assert b.wire.calls == [] and getattr(b, "count_calls", 0) == 0
            next_step = b.coord.claim_next("shared", now_ms=b.now)
            if next_step is not None:
                # Physical proof can advance residency before independent memory
                # settlement fails. The paused ledger must still prevent a send.
                assert failure == "attributed_live" and next_step.kind == "load"
                assert b.memory.view("shared").state == "paused"
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await b.transition.advance(next_step, queued, job(b, next_step))
                assert b.wire.calls == []
            assert b.memory.view("shared").committed_bytes == 600
            # A rejected binding never adopts or modifies the supplied lease;
            # the uncertain actual coordinator step still fences the domain.
            assert b.coord.snapshot("shared").state == "reconciling"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("phase", ["send", "proof"])
def test_cancelled_omlx_stage_retains_hold_and_cannot_replay(
    tmp_path, monkeypatch, backend, phase
):
    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        with b.coord:
            queued, step, lease = await unloaded_stage(env, b)
            current = source_job(env, step, lease)
            entered = asyncio.Event()

            async def hang(value):
                entered.set()
                await asyncio.Future()

            if phase == "send":
                env[4].hook = hang
            else:
                current = replace(current, proof=hang)
            task = asyncio.create_task(b.transition.advance(step, queued, current))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(env[4].posts) == 1
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, queued, current)
            assert len(env[4].posts) == 1 and b.wire.calls == []
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "paused"
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["load", "verify"])
def test_omlx_source_job_cannot_be_used_for_target_steps(tmp_path, monkeypatch, kind):
    async def run():
        env, b = environment(tmp_path, monkeypatch, "ollama")
        with b.coord:
            ready = admission(b)
            queued = b.transition.enqueue(
                ready, "cold-target", "shared", deadline_ms=40000
            )
            step = b.coord.claim_next("shared", now_ms=b.now)
            mem = None
            if kind == "verify":
                result = await b.transition.advance(step, queued, job(b, step))
                mem = result.memory
                step = b.coord.claim_next("shared", now_ms=b.now)
            current = source_job(env, step, mem)
            current = replace(current, profile=b.q.profile.memory)
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, queued, current)
            assert env[4].calls == [] and len(b.wire.calls) == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "activity",
    [
        {"active_streams": 1},
        {"active_streams": None},
        {"helper_visibility_complete": False},
    ],
)
def test_unquiesced_source_refuses_drain_before_any_native_io(
    tmp_path, monkeypatch, backend, activity
):
    async def run():
        env, b = environment(tmp_path, monkeypatch, backend)
        with b.coord:
            lease = await source.start_loaded(*env)
            env[4].calls.clear()
            queued = b.transition.enqueue(
                admission(b), "target-request", "shared", deadline_ms=40000
            )
            step = b.coord.claim_next("shared", now_ms=b.now)
            current = source_job(env, step, lease)
            current = replace(current, activity=replace(current.activity, **activity))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, queued, current)
            assert env[4].calls == [] and b.wire.calls == []
            assert b.memory.view("shared").committed_bytes == 600
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())


def test_hung_source_proof_is_bounded_without_release_or_retry(tmp_path, monkeypatch):
    async def run():
        env, b = environment(tmp_path, monkeypatch, "ollama")
        with b.coord:
            queued, step, lease = await unloaded_stage(env, b)
            current = source_job(env, step, lease)
            cancelled = asyncio.Event()

            async def hang(obs):
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            current = replace(current, proof=hang)
            # The outer deadline is a test watchdog; the adapter's own five-second
            # producer bound must refuse first. It is NOT an inference timeout.
            async with asyncio.timeout(10):
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await b.transition.advance(step, queued, current)
            assert cancelled.is_set()
            assert len(env[4].posts) == 1 and b.wire.calls == []
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "paused"
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())
