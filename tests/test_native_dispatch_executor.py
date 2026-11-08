"""Real dormant kernels wired through executor; all native facts/counters are fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import native_dispatch_executor as ne
import request_fit as rf
import runtime_dispatch_boundary as rd
from local_runtime_lifecycle import CapabilityEvidence
from test_request_fit_admission import harness
from test_shared_memory_admission import sample


async def collect(source):
    return [v async for v in source]


async def context(tmp_path, backend):
    if backend == "ollama":
        import test_ollama_lifecycle_bridge as driver
    else:
        import test_lmstudio_lifecycle_bridge as driver
    b = driver.setup(tmp_path)
    req, ml = await driver.warm(b)
    payload = dict(
        messages=[
            dict(role="system", content="SECURITY " + "anchor " * 3000),
            dict(role="user", content="FULL ORIGINAL ಕರ್ನಾಟಕ 😀"),
        ],
        tools=None,
        temperature=0.2,
        max_tokens=512,
        reasoning_mode="high" if backend == "ollama" else "off",
    )
    call = rd.RuntimeDispatchCall(
        backend, req.target.artifact_id, "infer", rf.encode(payload)
    )
    if backend == "ollama":
        native = ne.prepare_ollama_input(call, b.q.profile)
        fp = rf.FitProfile(
            b.q.profile.memory,
            "ollama_native",
            ne._profile_digest(b.q.profile),
            "fake-tokenizer",
            "fake-renderer",
        )
        version = b.q.server_version
    else:
        # Extra installed assertion is deliberately synthetic; not REST proof.
        b.q = replace(
            b.q,
            capabilities=b.q.capabilities
            + (
                CapabilityEvidence(
                    "reasoning_mode_off",
                    "supported",
                    "installed",
                    b.q.runtime_id,
                    b.q.version,
                    req.target.profile_fingerprint,
                    ("fake-test-assumption-only",),
                ),
            ),
        )
        native = ne.prepare_lm_input(call, req, b.q.profile)
        fp = rf.FitProfile(
            b.q.profile.memory,
            "lmstudio_openai",
            b.q.profile.digest,
            "fake-tokenizer",
            "fake-renderer",
        )
        version = b.q.version
    prep = rf.prepare_request(
        req, fp, rf.OutputBudget(512), native.body, origin="coordinated_dispatch"
    )
    harness(b, req, ml, prep, version=version)
    b.bridge.admit_request = b.service.authorizes

    async def fresh_sample():
        return sample(b.memory, now=b.now)

    fit = ne.NativeFitInputs(
        service=b.service,
        profile=fp,
        permit=b.permit,
        memory=ml,
        qualification=b.counter_q,
        counter=b.counter,
        observe=b.observe,
        sample=fresh_sample,
    )

    async def terminal():
        assert b.wire.closed
        b.now += 1
        if backend == "ollama":
            return driver.activity(b, req)
        return driver.activity(b, req, executed_request_digest=native.digest)

    cls = ne.OllamaExecution if backend == "ollama" else ne.LmExecution
    job = cls(
        bridge=b.bridge,
        permit=driver.permit(b, req, request_digest=native.digest),
        activity=driver.activity(b, req),
        terminal=terminal,
        prepared=native,
        fit=fit,
    )
    b.call, b.job, b.driver = call, job, driver
    b.executor = ne.NativeDispatchExecutor(lambda _: b.job)
    return b


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_final_bytes_are_counted_then_sent_only_under_real_warm_admission(
    tmp_path, backend
):
    async def run():
        b = await context(tmp_path, backend)
        try:
            before = len(b.driver.mutations(b))
            result = await collect(b.executor.stream(b.call))
            assert b.count_calls == 1 and b.observe_calls == 2
            assert any(v.get("text") == "READY" for v in result)
            assert result[-1]["type"] == "usage"
            assert len(b.driver.mutations(b)) == before + 1
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").committed_bytes == 600
            assert not b.service.authorizes(
                b.request,
                b.prepared.body_digest,
                b.prepared.profile.native_profile_digest,
            )
        finally:
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "case",
    [
        "unknown-counter",
        "may-load",
        "overflow",
        "changed-input",
        "missing-fit-hook",
        "stale-lease",
        "wrong-profile",
        "wrong-output",
        "reasoning-change",
    ],
)
def test_unqualified_changed_or_oversize_input_is_zero_inference_effect(
    tmp_path, backend, case
):
    async def run():
        b = await context(tmp_path, backend)
        try:
            if case == "unknown-counter":
                b.job = replace(b.job, fit=replace(b.job.fit, qualification=None))
            elif case == "may-load":
                b.job = replace(
                    b.job,
                    fit=replace(
                        b.job.fit, qualification=replace(b.counter_q, may_load=True)
                    ),
                )
            elif case == "overflow":
                b.input_tokens = 8192
            elif case == "missing-fit-hook":
                b.bridge.admit_request = None
            elif case == "stale-lease":
                b.coord.cancel(b.request.request_id, now_ms=b.now)
            elif case == "wrong-profile":
                b.job = replace(
                    b.job,
                    fit=replace(
                        b.job.fit,
                        profile=replace(
                            b.job.fit.profile, native_profile_digest="0" * 64
                        ),
                    ),
                )
            else:
                payload = rf.decode(b.call.payload)
                if case == "changed-input":
                    payload["messages"][1]["content"] = "OMITTED SECURITY?"
                elif case == "wrong-output":
                    payload["max_tokens"] = 4096
                elif case == "reasoning-change":
                    payload["reasoning_mode"] = "xhigh"
                b.call = replace(b.call, payload=rf.encode(payload))
            before = len(b.driver.mutations(b))
            with pytest.raises(ne.NativeFitDenied) as error:
                await collect(b.executor.stream(b.call))
            assert "SECURITY" not in str(error.value.to_mapping())
            assert len(b.driver.mutations(b)) == before
            if case in (
                "unknown-counter",
                "may-load",
                "changed-input",
                "missing-fit-hook",
                "stale-lease",
                "wrong-profile",
                "wrong-output",
                "reasoning-change",
            ):
                assert b.count_calls == 0
            else:
                assert error.value.to_mapping()["code"] == "context_exceeded"
                assert error.value.to_mapping()["fit"]["input_tokens"] == 8192
        finally:
            b.coord.close()

    asyncio.run(run())


def test_absent_executor_plan_and_controls_are_unavailable_not_legacy():
    executor = ne.NativeDispatchExecutor()
    call = rd.RuntimeDispatchCall("ollama", "model", "infer")
    with pytest.raises(rd.RuntimeDispatchDenied):
        asyncio.run(collect(executor.stream(call)))
    with pytest.raises(rd.RuntimeDispatchDenied):
        asyncio.run(executor.command(call))


def test_reasoning_and_content_are_not_structural_tool_authority():
    consumer = ne.CanonicalNativePackets(None)
    text = '<tool_call>{"name":"run_bash","arguments":{}}</tool_call>'
    assert consumer.ollama(dict(type="reasoning", text=text)) == [
        dict(type="reasoning", text=text)
    ]
    assert consumer.calls == {} and consumer.held == []


def test_lm_native_fragments_keep_ids_and_exact_json_until_terminal():
    consumer = ne.CanonicalNativePackets([{"function": {"name": "read"}}])

    def packet(d, reason=None):
        return rf.encode(dict(choices=[dict(delta=d, finish_reason=reason)]))

    assert (
        consumer.lm(packet({"reasoning_content": "never <tool_call>execute me"}))[0][
            "type"
        ]
        == "reasoning"
    )
    assert (
        consumer.lm(
            packet(
                {
                    "tool_calls": [
                        dict(
                            index=0,
                            id="native-id",
                            type="function",
                            function={"name": "re", "arguments": '{"path":'},
                        )
                    ]
                }
            )
        )
        == []
    )
    assert (
        consumer.lm(
            packet(
                {
                    "tool_calls": [
                        dict(index=0, function={"name": "ad", "arguments": '"<>&"}'})
                    ]
                },
                "tool_calls",
            )
        )
        == []
    )
    result = consumer.finish("lmstudio")
    assert result[0] == dict(
        type="tool_call",
        index=0,
        id="native-id",
        name="read",
        arguments={"path": "<>&"},
    )
    assert result[-1]["completion_tokens"] is None and result[-1]["decode_tps"] is None


@pytest.mark.parametrize(
    "case", ["partial-json", "unadvertised", "missing-id", "incomplete"]
)
def test_malformed_lm_tool_packets_never_publish_a_call(case):
    consumer = ne.CanonicalNativePackets([{"function": {"name": "read"}}])
    call = dict(index=0, id="native-id", function=dict(name="read", arguments="{}"))
    if case == "partial-json":
        call["function"]["arguments"] = "{"
    elif case == "unadvertised":
        call["function"]["name"] = "write"
    elif case == "missing-id":
        del call["id"]
    consumer.lm(
        rf.encode(
            dict(
                choices=[
                    dict(
                        delta={"tool_calls": [call]},
                        finish_reason=None if case == "incomplete" else "tool_calls",
                    )
                ]
            )
        )
    )
    with pytest.raises(rf.RequestFitDenied):
        consumer.finish("lmstudio")
