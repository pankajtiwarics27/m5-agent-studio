"""Cold-to-warm scheduling through real kernels; native/evidence inputs are fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_dispatch_executor as ne
import native_inference_scheduler as ns
import request_fit as rf
import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture, admission, job
from test_request_fit_admission import harness
from test_shared_memory_admission import sample


async def collect(source):
    return [v async for v in source]


def warm(b, ready, req, mem):
    p = (
        ne.prepare_ollama_input(ready.call, b.q.profile)
        if b.backend == "ollama"
        else ne.prepare_lm_input(ready.call, req, b.q.profile)
    )
    exact = rf.prepare_request(
        req, ready.profile, rf.OutputBudget(512), p.body, origin="coordinated_dispatch"
    )
    harness(
        b,
        req,
        mem,
        exact,
        version=b.q.server_version if b.backend == "ollama" else b.q.version,
    )
    b.counter_q = ready.counter
    b.obs_changes["server_identity"] = ready.counter.server_identity
    b.bridge.admit_request = b.service.authorizes

    async def observed():
        return sample(b.memory, now=b.now)

    async def terminal():
        assert b.wire.closed
        b.now += 1
        return b.driver.activity(
            b,
            req,
            **({} if b.backend == "ollama" else {"executed_request_digest": p.digest}),
        )

    fit = ne.NativeFitInputs(
        service=b.service,
        profile=ready.profile,
        permit=b.permit,
        memory=mem,
        qualification=b.counter_q,
        counter=b.counter,
        observe=b.observe,
        sample=observed,
    )
    cls = ne.OllamaExecution if b.backend == "ollama" else ne.LmExecution
    return cls(
        bridge=b.bridge,
        permit=b.driver.permit(b, req, request_digest=p.digest),
        activity=b.driver.activity(b, req),
        terminal=terminal,
        prepared=p,
        fit=fit,
    )


def scheduler(b, *, change_plan=None):
    def resolve(call):
        ready = replace(admission(b), call=call)
        plan = ns.NativeInferencePlan(
            admission=ready,
            domain_id="shared",
            deadline_ms=40000,
            transition=lambda step, mem: job(b, step, mem),
            warm=lambda req, mem: warm(b, ready, req, mem),
        )
        return change_plan(plan) if change_plan else plan

    return ns.NativeInferenceScheduler(b.transition, resolve)


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_scheduler_composes_cold_then_warm_inference_without_second_load(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            call = admission(b).call
            assert (await collect(execute.stream(call)))[-1]["type"] == "usage"
            first_mutations = len(b.driver.mutations(b))
            assert b.count_calls == 1 and first_mutations == 2  # cold load + inference
            assert (await collect(execute.stream(call)))[-1]["type"] == "usage"
            assert (
                len(b.driver.mutations(b)) == first_mutations + 1
            )  # warm inference only
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.coord.snapshot("shared").state == "idle"
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_concurrent_request_waits_until_source_terminal_then_reuses_residency(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            call = admission(b).call
            first = execute.stream(call)
            assert (await anext(first))["type"] in ("content", "reasoning")
            before = len(b.driver.mutations(b))
            second = asyncio.create_task(collect(execute.stream(call)))
            # Let the next actor reach its lock wait, without time-based polling.
            await asyncio.sleep(0)
            assert not second.done() and len(b.driver.mutations(b)) == before
            assert (await collect(first))[-1]["type"] == "usage"
            assert (await second)[-1]["type"] == "usage"
            assert len(b.driver.mutations(b)) == before + 1
            assert b.coord.snapshot("shared").active_requests == 0

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_cancel_waiter_does_not_cancel_active_owner_or_native_request(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            call = admission(b).call
            source = execute.stream(call)
            await anext(source)
            waiter = asyncio.create_task(collect(execute.stream(call)))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert b.coord.snapshot("shared").active_requests == 1
            assert (await collect(source))[-1]["type"] == "usage"
            assert b.coord.snapshot("shared").state == "idle"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_close_active_scheduler_reaches_bridge_and_blocks_following_load(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            call = admission(b).call
            source = execute.stream(call)
            await anext(source)
            await source.aclose()
            assert b.wire.closed and b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
            before = len(b.driver.mutations(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(execute.stream(call))
            assert len(b.driver.mutations(b)) == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_scheduler_never_claims_or_cancels_an_unrelated_fifo_owner(tmp_path, backend):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            b.coord.enqueue(
                "external-control",
                "shared",
                b.q.profile.memory.target,
                deadline_ms=100000,
                now_ms=b.now,
            )
            before = b.coord.snapshot("shared")
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(scheduler(b).stream(admission(b).call))
            after = b.coord.snapshot("shared")
            assert before == after and not b.wire.calls
            claimed = b.coord.claim_next(
                "shared", now_ms=b.now, request_id="external-control"
            )
            assert claimed.kind == "load"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "case", ["unqualified", "changed-call", "bad-warm", "missing-plan"]
)
def test_scheduler_defaults_and_handoff_refuse_without_inference(
    tmp_path, backend, case
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            def change(plan):
                if case == "unqualified":
                    return replace(
                        plan, admission=replace(plan.admission, counter=None)
                    )
                if case == "changed-call":
                    return replace(
                        plan,
                        admission=replace(
                            plan.admission,
                            call=replace(plan.admission.call, model="other"),
                        ),
                    )
                if case == "bad-warm":
                    return replace(
                        plan,
                        warm=lambda req, mem: replace(
                            warm(b, plan.admission, req, mem),
                            fit=replace(
                                warm(b, plan.admission, req, mem).fit,
                                qualification=None,
                            ),
                        ),
                    )
                return plan

            execute = scheduler(b, change_plan=change)
            if case == "missing-plan":
                execute = ns.NativeInferenceScheduler(b.transition)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(execute.stream(admission(b).call))
            if case in ("unqualified", "changed-call", "missing-plan"):
                assert not b.wire.calls
            else:
                assert (
                    b.count_calls == 0 and len(b.driver.mutations(b)) == 1
                )  # load, no inference
                assert b.coord.snapshot("shared").state == "reconciling"
            with pytest.raises(rd.RuntimeDispatchDenied):
                await execute.command(rd.RuntimeDispatchCall(backend, "model", "load"))

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("stream", [False, True])
def test_existing_application_gateway_can_consume_scheduler_with_fake_native_producers(
    tmp_path, monkeypatch, backend, stream
):
    import json
    from types import SimpleNamespace
    import app

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {backend: execute}, {"fixture": backend}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: (
                    "cloud",
                    {"alias": "fixture"},
                    b.q.profile.memory.target.artifact_id,
                ),
            )
            inputs = rf.decode(admission(b).call.payload)
            body = dict(
                model="fixture/model",
                messages=inputs["messages"],
                temperature=0.2,
                max_tokens=512,
                reasoning_effort=inputs["reasoning_mode"],
                stream=stream,
            )

            async def request_json():
                return body

            response = await app.openai_gateway_chat(
                SimpleNamespace(
                    json=request_json,
                    app=SimpleNamespace(state=SimpleNamespace(http=object())),
                )
            )
            if stream:
                text = "".join([v async for v in response.body_iterator])
                assert "[DONE]" in text and "READY" in text
            else:
                result = json.loads(response.body)
                assert result["choices"][0]["message"]["content"] == "READY"
            assert b.count_calls == 1 and len(b.driver.mutations(b)) == 2
            assert b.coord.snapshot("shared").active_requests == 0
            assert (
                app._CHAT_NATIVE_TOOLS.get() is None
                and not app._CANONICAL_NATIVE_CONSUMER.get()
            )

    asyncio.run(run())


@pytest.mark.parametrize("source_backend", ["ollama", "lmstudio"])
def test_scheduler_serializes_cross_runtime_cold_inference_until_physical_release(
    tmp_path, monkeypatch, source_backend
):
    async def run():
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
        bundles = {source_backend: source, other: target}

        def now():
            return max(source.now, target.now)

        for b in bundles.values():
            b.transition.clock = now
            b.bridge.clock = now

        def resolve(call):
            b = bundles[call.runtime]
            b.now = now()
            ready = replace(admission(b), call=call)

            def transition(step, mem):
                subject = (
                    step.source if step.kind in ("drain", "unload") else step.target
                )
                selected = bundles[subject.runtime_id]
                selected.now = now()
                return job(selected, step, mem)

            def prepare(req, mem):
                b.now = now()
                return warm(b, ready, req, mem)

            return ns.NativeInferencePlan(
                admission=ready,
                domain_id="shared",
                deadline_ms=40000,
                transition=transition,
                warm=prepare,
            )

        with source.coord:
            execute = ns.NativeInferenceScheduler(source.transition, resolve)
            first = execute.stream(admission(source).call)
            await anext(first)
            second = asyncio.create_task(
                collect(execute.stream(admission(target).call))
            )
            await asyncio.sleep(0)
            assert not target.wire.calls and len(source.fleet.models) == 1
            assert (await collect(first))[-1]["type"] == "usage"
            assert (await second)[-1]["type"] == "usage"
            assert len(source.fleet.models) == 1 and other in source.fleet.models
            assert len(source.driver.mutations(source)) == 3  # load, inference, unload
            assert len(target.driver.mutations(target)) == 2  # load then inference
            assert source.coord.snapshot("shared").active_requests == 0
            assert source.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_close_after_verified_usage_does_not_poison_warm_residency(tmp_path, backend):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            source = execute.stream(admission(b).call)
            async for event in source:
                if event["type"] == "usage":
                    break
            await source.aclose()
            assert b.coord.snapshot("shared").state == "idle"
            assert b.memory.view("shared").state == "ready"
            assert (await collect(execute.stream(admission(b).call)))[-1][
                "type"
            ] == "usage"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_stage_factory_failure_after_claim_requires_reconciliation_not_retry(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            def fail(*args):
                raise RuntimeError("SECRET producer details")

            execute = scheduler(
                b, change_plan=lambda plan: replace(plan, transition=fail)
            )
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                await collect(execute.stream(admission(b).call))
            assert "SECRET" not in str(error.value) and not b.wire.calls
            assert b.coord.snapshot("shared").state == "reconciling"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_lock_wait_deadline_refuses_without_queueing_or_interrupting_owner(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            short = False

            def limits(plan):
                return replace(plan, deadline_ms=b.now + 1) if short else plan

            execute = scheduler(b, change_plan=limits)
            source = execute.stream(admission(b).call)
            await anext(source)
            before = len(b.driver.mutations(b))
            short = True
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(execute.stream(admission(b).call))
            assert (
                b.coord.snapshot("shared").queued == 0
                and b.coord.snapshot("shared").active_requests == 1
            )
            assert len(b.driver.mutations(b)) == before
            assert (await collect(source))[-1]["type"] == "usage"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_scheduler_terminal_fit_refusal_never_retries_trims_or_substitutes(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            def configure(plan):
                original = plan.warm

                def overflow(req, mem):
                    result = original(req, mem)
                    b.input_tokens = 8192
                    return result

                return replace(plan, warm=overflow)

            execute = scheduler(b, change_plan=configure)
            with pytest.raises(ne.NativeFitDenied) as error:
                await collect(execute.stream(admission(b).call))
            assert error.value.to_mapping()["code"] == "context_exceeded"
            assert error.value.to_mapping()["fit"]["input_tokens"] == 8192
            assert (
                b.count_calls == 1 and len(b.driver.mutations(b)) == 1
            )  # cold load only
            assert b.memory.view("shared").committed_bytes == 600
            assert b.coord.snapshot("shared").state == "reconciling"

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_counter_readiness_is_rechecked_after_actor_wait_before_queueing(
    tmp_path, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            waiting = False

            def limits(plan):
                if waiting:
                    return replace(
                        plan,
                        admission=replace(
                            plan.admission,
                            counter=replace(
                                plan.admission.counter, expires_ms=b.now + 1
                            ),
                        ),
                    )
                return plan

            execute = scheduler(b, change_plan=limits)
            source = execute.stream(admission(b).call)
            await anext(source)
            before = len(b.driver.mutations(b))
            waiting = True
            waiter = asyncio.create_task(collect(execute.stream(admission(b).call)))
            await asyncio.sleep(0)
            assert not waiter.done()
            assert (await collect(source))[-1][
                "type"
            ] == "usage"  # terminal advances fake clock
            with pytest.raises(ne.NativeFitDenied):
                await waiter
            assert len(b.driver.mutations(b)) == before
            assert b.coord.snapshot("shared").queued == 0
            assert b.coord.snapshot("shared").state == "idle"

    asyncio.run(run())
