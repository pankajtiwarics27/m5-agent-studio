"""Explicit release step consumer; fake facts only, no canonical unload actor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import model_lifecycle_coordinator as lc
import native_transition_driver as nt
from ollama_protocol import OllamaDenied
from lmstudio_protocol import LmStudioDenied
import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture
from test_native_load_control import scheduler, call as load_call
from test_shared_memory_admission import sample


def call(b):
    return replace(load_call(b), action="unload")


async def loaded(tmp_path, backend):
    b = fixture(tmp_path, backend)
    assert await scheduler(b).command(load_call(b)) == {"status": "confirmed"}
    b.wire.calls.clear()
    return b


def enqueue(b, **changes):
    ready = changes.pop("admission", nt.UnloadAdmission(call(b), b.q))
    return b.transition.enqueue_unload(
        ready, "release", "shared", deadline_ms=changes.pop("deadline", 40000)
    )


def job(b, step, queued, **changes):
    async def measured():
        return sample(b.memory, now=b.now)

    cls = nt.OllamaRelease if b.backend == "ollama" else nt.LmRelease
    return replace(
        cls(
            bridge=b.bridge,
            permit=b.driver.permit(b, step),
            activity=b.driver.activity(b, step),
            memory=queued.memory,
            sample=measured,
            proof=lambda obs: b.driver.proof(b, obs),
        ),
        **changes,
    )


async def drained(b, queued):
    step = b.coord.claim_next("shared", now_ms=b.now, request_id=queued.request_id)
    assert (
        type(step) is lc.ReleaseStep
        and step.kind == "drain"
        and not hasattr(step, "target")
    )
    result = await b.transition.advance_release(step, queued, job(b, step, queued))
    assert not result.retired and result.memory == queued.memory
    return b.coord.claim_next("shared", now_ms=b.now, request_id=queued.request_id)


def status(b):
    return b.coord._call(
        lambda conn: conn.execute(
            "SELECT status FROM requests WHERE id='release'"
        ).fetchone()[0]
    )


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_native_release_preserves_barrier_until_independent_actual_zero_then_never_reloads(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = await drained(b, queued)
            current = job(b, step, queued)
            original = current.proof

            async def proof(obs):
                assert b.memory.view("shared").committed_bytes == 600
                assert (
                    b.coord.snapshot("shared").resident
                    == queued.admission.memory.target
                )
                return await original(obs)

            async def zero():
                assert b.coord.snapshot("shared").resident is None
                assert b.coord.snapshot("shared").operation_status == "complete"
                assert b.coord.snapshot("shared").state == "switching"
                assert b.memory.view("shared").committed_bytes == 600
                b.coord.enqueue_load(
                    "next",
                    "shared",
                    queued.admission.memory.target,
                    deadline_ms=40000,
                    now_ms=b.now,
                )
                assert (
                    b.coord.claim_next("shared", now_ms=b.now, request_id="next")
                    is None
                )
                assert not b.coord.finish_unload(
                    step, reference="physical-is-not-zero", now_ms=b.now
                )
                return sample(b.memory, now=b.now)

            result = await b.transition.advance_release(
                step, queued, replace(current, proof=proof, sample=zero)
            )
            assert result.retired and result.memory is None
            assert b.memory.view("shared").committed_bytes == 0
            assert b.coord.snapshot("shared").state == "idle"
            assert status(b) == "completed"
            assert backend not in b.fleet.models
            assert len(b.driver.mutations(b)) == 1
            assert not b.coord.mark_release_uncertain(
                step, reference="old", now_ms=b.now
            )
            if backend == "ollama":
                body = b.driver.mutations(b)[0][2]
                assert body["keep_alive"] == 0 and body["messages"] == []
            else:
                assert b.driver.mutations(b)[0][2] == {
                    "instance_id": queued.source_instance_id
                }
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, current)
            assert len(b.driver.mutations(b)) == 1
            # A new load is an independently claimed acquire, never a release stage.
            next_step = b.coord.claim_next("shared", now_ms=b.now, request_id="next")
            assert type(next_step) is lc.TransitionStep and next_step.kind == "load"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "unqualified",
        "payload",
        "model",
        "runtime",
        "load_instead",
        "empty",
        "unknown",
        "paused",
        "profile",
    ],
)
def test_release_readiness_refuses_before_queue_probe_or_disruption(
    tmp_path, backend, fault
):
    async def run():
        b = (
            fixture(tmp_path, backend)
            if fault == "empty"
            else await loaded(tmp_path, backend)
        )
        with b.coord:
            ready = nt.UnloadAdmission(call(b), b.q)
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
            elif fault in ("payload", "model", "runtime", "load_instead"):
                ready = replace(
                    ready,
                    call=replace(
                        ready.call,
                        **(
                            {"payload": b'{"fake":true}'}
                            if fault == "payload"
                            else {"model": "other"}
                            if fault == "model"
                            else {"runtime": "omlx"}
                            if fault == "runtime"
                            else {"action": "load"}
                        ),
                    ),
                )
            elif fault == "profile":
                ready = replace(
                    ready,
                    native=replace(
                        b.q,
                        profile=replace(
                            b.q.profile,
                            memory=replace(
                                b.q.profile.memory,
                                weights_bytes=b.q.profile.memory.weights_bytes + 1,
                            ),
                        ),
                    ),
                )
            elif fault in ("unknown", "paused"):
                b.coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_reservations SET status='uncertain'"
                    )
                ) if fault == "unknown" else b.coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_domains SET state='paused'"
                    )
                )
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                enqueue(b, admission=ready)
            assert b.wire.calls == []
            assert b.coord.snapshot("shared") == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "ack_only",
        "scope",
        "terminal",
        "missing_runtime",
        "still_loaded",
        "version",
        "zero_nonzero",
        "sample_error",
        "sample_timeout",
        "wrong_job",
        "generic_bridge",
        "foreign_memory",
        "activity",
        "denied",
        "profile_record",
    ],
)
def test_uncertainty_never_retires_or_replays_native_release(tmp_path, backend, fault):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = await drained(b, queued)
            current = job(b, step, queued)
            original = current.proof

            async def proof(obs):
                evidence = await original(obs)
                if fault == "ack_only":
                    return {"status": "ok"}
                if fault == "scope":
                    return replace(
                        evidence, scope=replace(evidence.scope, operation_id="wrong")
                    )
                if fault == "terminal":
                    return replace(evidence, operations_terminal=False)
                if fault == "missing_runtime":
                    return replace(evidence, inventories=evidence.inventories[1:])
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
                if fault == "sample_timeout":
                    raise TimeoutError("secret")
                return sample(
                    b.memory, now=b.now, attributed=((queued.memory.lease_id, 1),)
                )

            if fault in ("ack_only", "scope", "terminal", "missing_runtime", "version"):
                current = replace(current, proof=proof)
            elif fault in ("zero_nonzero", "sample_error", "sample_timeout"):
                current = replace(current, sample=measured)
            elif fault == "still_loaded":
                if backend == "ollama":
                    b.wire.apply_effect = False
                else:
                    b.wire.apply = False
            elif fault == "wrong_job":
                current = (
                    nt.OllamaTransition(bridge=b.bridge, permit=current.permit)
                    if backend == "ollama"
                    else nt.LmTransition(bridge=b.bridge, permit=current.permit)
                )
            elif fault == "generic_bridge":
                current = replace(
                    current,
                    bridge=SimpleNamespace(
                        coordinator=b.coord, coord=b.coord, memory=b.memory
                    ),
                )
            elif fault == "foreign_memory":
                current = replace(
                    current, memory=replace(queued.memory, lease_id="wrong-hold")
                )
            elif fault == "activity":
                current = replace(
                    current, activity=replace(current.activity, active_requests=None)
                )
            elif fault == "denied":
                b.authorized = False
            elif fault == "profile_record":
                b.coord._call(
                    lambda conn: conn.execute(
                        "DELETE FROM journal WHERE event IN ('ollama_profile_load','lmstudio_profile_load')"
                    )
                )
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                await b.transition.advance_release(step, queued, current)
            assert "secret" not in str(error.value)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").operation_status == "uncertain"
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "paused"
            assert status(b) != "completed"
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, current)
            assert len(b.wire.calls) == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("condition", ["cancelled", "expired"])
def test_post_send_cancel_or_expiry_can_release_resources_but_not_complete_command(
    tmp_path, backend, condition
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = await drained(b, queued)

            def after_send(method, path):
                if method == "POST":
                    if condition == "cancelled":
                        b.coord.cancel(queued.request_id, now_ms=b.now)
                    else:
                        b.now = queued.deadline_ms

            b.wire.hook = after_send
            result = await b.transition.advance_release(
                step, queued, job(b, step, queued)
            )
            assert result.retired
            assert status(b) == condition
            assert b.coord.snapshot("shared").state == "idle"
            assert b.memory.view("shared").committed_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_no_canonical_unload_actor_or_inference_authority_is_installed(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.wire.calls == []
            queued = enqueue(b)
            step = b.coord.claim_next("shared", now_ms=b.now)
            permit = b.driver.permit(b, step)
            with pytest.raises((OllamaDenied, LmStudioDenied)):
                await b.bridge.confirm_load(
                    permit,
                    lease=queued.memory,
                    sample=lambda: None,
                    produce=lambda obs: None,
                )
            assert b.wire.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "field",
    ["request_id", "source_generation", "source_instance_id", "deadline_ms", "memory"],
)
def test_queued_release_identity_cannot_be_rebound_to_a_current_operation(
    tmp_path, backend, field
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = b.coord.claim_next("shared", now_ms=b.now)
            altered = replace(
                queued,
                **{
                    field: "different"
                    if field in ("request_id", "source_instance_id")
                    else 99
                    if field == "source_generation"
                    else 39999
                    if field == "deadline_ms"
                    else replace(queued.memory, lease_id="wrong")
                },
            )
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, altered, job(b, step, queued))
            assert b.wire.calls == [] and b.coord.snapshot("shared") == before
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "ready"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("condition", ["cancelled", "expired", "paused"])
def test_pre_send_lost_authority_never_probes_or_releases_hold(
    tmp_path, backend, condition
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = b.coord.claim_next("shared", now_ms=b.now)
            if condition == "cancelled":
                b.coord.cancel(queued.request_id, now_ms=b.now)
            elif condition == "expired":
                b.now = queued.deadline_ms
            else:
                b.coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_domains SET state='paused'"
                    )
                )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, job(b, step, queued))
            assert b.wire.calls == []
            assert b.memory.view("shared").committed_bytes == 600
            assert b.coord.snapshot("shared").resident == queued.admission.memory.target
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_unproven_original_native_profile_cannot_even_complete_release_drain(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = b.coord.claim_next("shared", now_ms=b.now)
            b.coord._call(
                lambda conn: conn.execute(
                    "DELETE FROM journal WHERE event IN ('ollama_profile_load','lmstudio_profile_load')"
                )
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, job(b, step, queued))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_cancelled_proof_producer_keeps_real_source_charge_and_no_replay(
    tmp_path, backend
):
    async def run():
        b = await loaded(tmp_path, backend)
        with b.coord:
            queued = enqueue(b)
            step = await drained(b, queued)
            entered = asyncio.Event()

            async def blocked(obs):
                entered.set()
                await asyncio.Future()

            current = job(b, step, queued, proof=blocked)
            task = asyncio.create_task(
                b.transition.advance_release(step, queued, current)
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(b.driver.mutations(b)) == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == "paused"
            before = len(b.wire.calls)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance_release(step, queued, current)
            assert len(b.wire.calls) == before

    asyncio.run(run())
