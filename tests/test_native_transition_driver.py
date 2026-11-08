"""Offline concrete cold-step integration. All native/installed facts are fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_transition_driver as nt
import native_dispatch_executor as ne
import request_fit as rf
import request_fit_admission as ra
import runtime_dispatch_boundary as rd
from local_runtime_lifecycle import CapabilityEvidence
from model_lifecycle_coordinator import RequestLease
from test_shared_memory_admission import sample


def fixture(tmp_path, backend):
    if backend == "ollama":
        import test_ollama_lifecycle_bridge as driver
    else:
        import test_lmstudio_lifecycle_bridge as driver
    b = driver.setup(tmp_path)
    if backend == "lmstudio":
        b.q = replace(
            b.q,
            capabilities=b.q.capabilities
            + (
                CapabilityEvidence(
                    "reasoning_mode_off",
                    "supported",
                    "installed",
                    "lmstudio",
                    b.q.version,
                    b.q.profile.memory.target.profile_fingerprint,
                    ("fake-only-off-mode",),
                ),
            ),
        )
    b.driver = driver
    b.backend = backend
    b.transition = nt.NativeTransitionDriver(b.coord, b.memory, clock_ms=lambda: b.now)
    return b


def admission(b, *, tools=None):
    call = rd.RuntimeDispatchCall(
        b.backend,
        b.q.profile.memory.target.artifact_id,
        "infer",
        rf.encode(
            dict(
                messages=[
                    dict(role="system", content="SECURITY " + "anchor " * 3000),
                    dict(role="user", content="ಕರ್ನಾಟಕ 😀 latest"),
                ],
                tools=tools,
                temperature=0.2,
                max_tokens=512,
                reasoning_mode="high" if b.backend == "ollama" else "off",
            )
        ),
    )
    fp = rf.FitProfile(
        b.q.profile.memory,
        "ollama_native" if b.backend == "ollama" else "lmstudio_openai",
        ne._profile_digest(b.q.profile)
        if b.backend == "ollama"
        else b.q.profile.digest,
        "fake-tokenizer",
        "fake-renderer",
    )
    version = b.q.server_version if b.backend == "ollama" else b.q.version
    counter = ra.CounterQualification(
        version,
        b.q.server_identity if b.backend == "lmstudio" else "fake-process",
        fp.native_profile_digest,
        fp.tokenizer_revision,
        fp.renderer_revision,
        "exact",
        frozenset(
            {
                "messages",
                "template",
                "request_settings",
                "reasoning",
                "tool_history",
                "tools",
            }
        ),
        50000,
        "fake-counter-scope",
        tuple(
            CapabilityEvidence(
                name,
                "supported",
                "installed",
                b.backend,
                version,
                fp.memory.target.profile_fingerprint,
                ("fake-only-no-native-qualification",),
            )
            for name in ra.CAPABILITIES
        ),
        False,
        True,
        True,
        "included",
    )
    return nt.ColdAdmission(call, fp, b.q, counter)


def job(b, step, memory=None, **changes):
    cls = nt.OllamaTransition if b.backend == "ollama" else nt.LmTransition

    async def observed():
        return sample(b.memory, now=b.now)

    return replace(
        cls(
            bridge=b.bridge,
            permit=b.driver.permit(b, step),
            activity=b.driver.activity(b, step),
            memory=memory,
            sample=observed,
            proof=lambda obs: b.driver.proof(b, obs),
        ),
        **changes,
    )


async def loaded(b, queued):
    load = b.coord.claim_next("shared", now_ms=b.now)
    assert load.kind == "load"
    result = await b.transition.advance(load, queued, job(b, load))
    assert b.memory.view("shared").committed_bytes == 600
    verify = b.coord.claim_next("shared", now_ms=b.now)
    assert verify.kind == "verify"
    result = await b.transition.advance(verify, queued, job(b, verify, result.memory))
    req = b.coord.claim_next("shared", now_ms=b.now)
    assert isinstance(req, RequestLease)
    return req, result.memory


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_empty_domain_load_then_warm_exact_fit_inference(tmp_path, backend):
    from test_request_fit_admission import harness

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            ready = admission(b)
            queued = b.transition.enqueue(
                ready, "cold-request", "shared", deadline_ms=100000
            )
            assert b.wire.calls == []  # Readiness does not even probe.
            req, mem = await loaded(b, queued)
            prepared = (
                ne.prepare_ollama_input(ready.call, b.q.profile)
                if backend == "ollama"
                else ne.prepare_lm_input(ready.call, req, b.q.profile)
            )
            fp = ready.profile
            exact = rf.prepare_request(
                req,
                fp,
                rf.OutputBudget(512),
                prepared.body,
                origin="coordinated_dispatch",
            )
            harness(
                b,
                req,
                mem,
                exact,
                version=b.q.server_version if backend == "ollama" else b.q.version,
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
                    **(
                        {}
                        if backend == "ollama"
                        else {"executed_request_digest": prepared.digest}
                    ),
                )

            inputs = ne.NativeFitInputs(
                service=b.service,
                profile=fp,
                permit=b.permit,
                memory=mem,
                qualification=b.counter_q,
                counter=b.counter,
                observe=b.observe,
                sample=observed,
            )
            cls = ne.OllamaExecution if backend == "ollama" else ne.LmExecution
            execute = cls(
                bridge=b.bridge,
                permit=b.driver.permit(b, req, request_digest=prepared.digest),
                activity=b.driver.activity(b, req),
                terminal=terminal,
                prepared=prepared,
                fit=inputs,
            )
            events = [
                v
                async for v in ne.NativeDispatchExecutor(lambda _: execute).stream(
                    ready.call
                )
            ]
            assert events[-1]["type"] == "usage" and b.count_calls == 1
            assert b.coord.snapshot("shared").active_requests == 0
            assert (
                len(b.fleet.models) == 1
                and b.memory.view("shared").committed_bytes == 600
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "case",
    [
        "missing-counter",
        "may-load",
        "expired-counter",
        "unknown-template",
        "missing-native-cap",
        "expired-native",
        "wrong-profile",
        "output-change",
        "reasoning-change",
    ],
)
def test_bad_readiness_does_not_queue_probe_or_disturb_resident(
    tmp_path, backend, case
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            req, _ = await b.driver.warm(b)
            assert b.coord.finish_request(req, reference="fake-terminal", now_ms=b.now)
            ready = admission(b)
            if case == "missing-counter":
                ready = replace(ready, counter=None)
            elif case == "may-load":
                ready = replace(ready, counter=replace(ready.counter, may_load=True))
            elif case == "expired-counter":
                ready = replace(ready, counter=replace(ready.counter, expires_ms=b.now))
            elif case == "unknown-template":
                ready = replace(
                    ready,
                    counter=replace(
                        ready.counter, supported_features=frozenset({"messages"})
                    ),
                )
            elif case == "missing-native-cap":
                ready = replace(ready, native=replace(b.q, capabilities=()))
            elif case == "expired-native":
                ready = replace(ready, native=replace(b.q, expires_ms=b.now))
            elif case == "wrong-profile":
                ready = replace(
                    ready,
                    profile=replace(ready.profile, native_profile_digest="0" * 64),
                )
            else:
                value = rf.decode(ready.call.payload)
                value["max_tokens" if case == "output-change" else "reasoning_mode"] = (
                    10000 if case == "output-change" else "xhigh"
                )
                ready = replace(
                    ready, call=replace(ready.call, payload=rf.encode(value))
                )
            before = b.coord.snapshot("shared"), len(b.wire.calls), dict(b.fleet.models)
            with pytest.raises(rd.RuntimeDispatchDenied):
                b.transition.enqueue(ready, "bad", "shared", deadline_ms=100000)
            assert (
                b.coord.snapshot("shared"),
                len(b.wire.calls),
                b.fleet.models,
            ) == before
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "case", ["missing-proof", "bad-proof", "nonzero-memory", "cancelled-proof"]
)
def test_unload_ack_never_releases_without_both_physical_proof_and_zero_attribution(
    tmp_path, backend, case
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            req, mem = await b.driver.warm(b)
            b.coord.finish_request(req, reference="fake-terminal", now_ms=b.now)
            # Same native model, different admitted load profile fingerprint.
            target = replace(
                b.q.profile.memory.target, profile_fingerprint="other-profile"
            )
            native_profile = replace(
                b.q.profile, memory=replace(b.q.profile.memory, target=target)
            )
            old = b.q
            b.q = replace(
                old,
                profile=native_profile,
                capabilities=tuple(
                    replace(c, profile_fingerprint=target.profile_fingerprint)
                    for c in old.capabilities
                ),
            )
            ready = admission(b)
            queued = b.transition.enqueue(ready, "switch", "shared", deadline_ms=100000)
            b.q = old
            drain = b.coord.claim_next("shared", now_ms=b.now)
            await b.transition.advance(drain, queued, job(b, drain, mem))
            unload = b.coord.claim_next("shared", now_ms=b.now)

            async def bad_proof(obs):
                return replace(await b.driver.proof(b, obs), operations_terminal=False)

            async def cancelled(obs):
                raise asyncio.CancelledError()

            async def nonzero():
                return sample(b.memory, now=b.now, attributed=((mem.lease_id, 600),))

            current = job(b, unload, mem)
            if case == "missing-proof":
                current = replace(current, proof=None)
            elif case == "bad-proof":
                current = replace(current, proof=bad_proof)
            elif case == "nonzero-memory":
                current = replace(current, sample=nonzero)
            else:
                current = replace(current, proof=cancelled)
            with pytest.raises(
                asyncio.CancelledError
                if case == "cancelled-proof"
                else rd.RuntimeDispatchDenied
            ):
                await b.transition.advance(unload, queued, current)
            assert b.memory.view("shared").committed_bytes == 600
            assert (
                b.coord.snapshot("shared").state == "reconciling"
                or b.memory.view("shared").state == "paused"
            )
            assert (
                b.coord.claim_next("shared", now_ms=b.now) is None
                or b.memory.view("shared").state == "paused"
            )

    asyncio.run(run())


@pytest.mark.parametrize("source_backend", ["ollama", "lmstudio"])
def test_cross_runtime_cold_switch_blocks_racing_load_until_real_release(
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
        with source.coord:
            queued_source = source.transition.enqueue(
                admission(source), "source", "shared", deadline_ms=100000
            )
            active, source_mem = await loaded(source, queued_source)
            ready = admission(target)
            queued = target.transition.enqueue(
                ready, "target", "shared", deadline_ms=100000
            )
            assert source.coord.claim_next("shared", now_ms=source.now) is None
            assert len(source.fleet.models) == 1 and target.wire.calls == []
            assert source.coord.finish_request(
                active, reference="fake-native-terminal", now_ms=source.now
            )
            drain = source.coord.claim_next("shared", now_ms=source.now)
            assert drain.kind == "drain"
            await source.transition.advance(
                drain, queued, job(source, drain, source_mem)
            )
            unload = source.coord.claim_next("shared", now_ms=source.now)
            original = source.driver.proof

            async def observed_release(obs):
                # Native ACK already applied, but neither operation nor memory
                # may advance until this independently qualified fake fact.
                assert not source.fleet.models
                assert source.memory.view("shared").committed_bytes == 600
                assert source.coord.claim_next("shared", now_ms=source.now) is None
                assert target.wire.calls == []
                return await original(source, obs)

            await source.transition.advance(
                unload, queued, job(source, unload, source_mem, proof=observed_release)
            )
            assert source.memory.view("shared").committed_bytes == 0
            assert not source.fleet.models
            req, mem = await loaded(target, queued)
            assert req.target.runtime_id == other and len(source.fleet.models) == 1
            assert source.memory.view("shared").committed_bytes == 600
            assert mem.lease_id != source_mem.lease_id

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "case",
    ["cancelled-queue", "wrong-step", "missing-authority", "counter-expired", "replay"],
)
def test_invalid_or_replayed_cold_stage_never_sends_an_extra_native_load(
    tmp_path, backend, case
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            ready = admission(b)
            queued = b.transition.enqueue(ready, "cold", "shared", deadline_ms=100000)
            step = b.coord.claim_next("shared", now_ms=b.now)
            current = job(b, step)
            if case == "cancelled-queue":
                b.coord.cancel("cold", now_ms=b.now)
            elif case == "wrong-step":
                step = replace(step, generation=step.generation + 1)
            elif case == "missing-authority":
                b.authorized = False
            elif case == "counter-expired":
                b.now = 50000
            else:
                await b.transition.advance(step, queued, current)
            before = len(b.driver.mutations(b))
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, queued, current)
            assert len(b.driver.mutations(b)) == before

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("case", ["counter-version", "tool-feature-coverage"])
def test_pre_disruption_counter_scope_includes_native_version_and_full_tools(
    tmp_path, backend, case
):
    b = fixture(tmp_path, backend)
    with b.coord:
        tools = [
            {
                "type": "function",
                "function": {"name": "read", "parameters": {"type": "object"}},
            }
        ]
        ready = admission(b, tools=tools)
        q = (
            replace(ready.counter, version="other-installed-version")
            if case == "counter-version"
            else replace(
                ready.counter,
                supported_features=ready.counter.supported_features - {"tools"},
            )
        )
        with pytest.raises(rd.RuntimeDispatchDenied):
            b.transition.enqueue(
                replace(ready, counter=q), "blocked", "shared", deadline_ms=100000
            )
        assert not b.wire.calls and b.coord.snapshot("shared").queued == 0


def test_lm_counter_process_identity_must_match_before_queueing(tmp_path):
    b = fixture(tmp_path, "lmstudio")
    with b.coord:
        ready = admission(b)
        ready = replace(
            ready, counter=replace(ready.counter, server_identity="different-process")
        )
        with pytest.raises(rd.RuntimeDispatchDenied):
            b.transition.enqueue(ready, "blocked", "shared", deadline_ms=100000)
        assert not b.wire.calls and b.coord.snapshot("shared").queued == 0
