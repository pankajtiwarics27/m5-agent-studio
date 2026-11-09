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


async def context(tmp_path, backend, *, tools=None, messages=None):
    if backend == "ollama":
        import test_ollama_lifecycle_bridge as driver
    else:
        import test_lmstudio_lifecycle_bridge as driver
    b = driver.setup(tmp_path)
    req, ml = await driver.warm(b)
    payload = dict(
        messages=messages
        if messages is not None
        else [
            dict(role="system", content="SECURITY " + "anchor " * 3000),
            dict(role="user", content="FULL ORIGINAL ಕರ್ನಾಟಕ 😀"),
        ],
        tools=tools,
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
        arguments_json='{"path":"<>&"}',
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


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("terminal_ok", [True, False])
def test_application_gateway_preserves_canonical_native_ids_and_argument_bytes(
    tmp_path, monkeypatch, backend, stream, terminal_ok
):
    import json
    from types import SimpleNamespace
    import app as app

    tools = [
        dict(
            type="function",
            function=dict(
                name="read",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        )
    ]

    async def run():
        messages = [
            dict(role="system", content="SECURITY " + "anchor " * 3000),
            dict(
                role="assistant",
                content="",
                reasoning_content="prior reasoning, not executable",
                tool_calls=[
                    dict(
                        id="previous-native-id",
                        type="function",
                        function=dict(name="read", arguments='{"path":"original"}'),
                    )
                ],
            ),
            dict(
                role="tool",
                tool_call_id="previous-native-id",
                content="ORIGINAL RESULT ಕರ್ನಾಟಕ 😀",
            ),
            dict(role="user", content="LATEST USER — do not drop history"),
        ]
        b = await context(tmp_path, backend, tools=tools, messages=messages)
        if not terminal_ok:

            async def failed_terminal():
                raise RuntimeError("SECRET terminal response")

            b.job = replace(b.job, terminal=failed_terminal)
        try:
            if backend == "ollama":
                from test_ollama_native_protocol import packet, done, line

                b.wire.packets = [
                    line(
                        packet(
                            message=dict(
                                role="assistant",
                                content="",
                                tool_calls=[
                                    dict(
                                        id="native-call-id",
                                        function=dict(
                                            name="read", arguments={"path": "<>&"}
                                        ),
                                    )
                                ],
                            )
                        )
                    ),
                    line(done()),
                ]
                arguments = json.dumps({"path": "<>&"}, ensure_ascii=False)
            else:
                from test_lmstudio_native_protocol import sse, packet

                arguments = ' { "path" : "<>&" } '
                b.wire.chunks = [
                    sse(
                        packet(
                            choices=[
                                dict(
                                    index=0,
                                    delta=dict(
                                        tool_calls=[
                                            dict(
                                                index=0,
                                                id="native-call-id",
                                                type="function",
                                                function=dict(
                                                    name="read", arguments=arguments
                                                ),
                                            )
                                        ]
                                    ),
                                    finish_reason="tool_calls",
                                )
                            ]
                        )
                    ),
                    b"data: [DONE]\n\n",
                ]
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {backend: b.executor}, {"fixture": backend}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: (
                    "cloud",
                    {"alias": "fixture", "base_url": "http://localhost/v1"},
                    b.call.model,
                ),
            )
            body = rf.decode(b.call.payload)
            body = dict(
                model="fixture/model",
                messages=body["messages"],
                tools=tools,
                temperature=0.2,
                max_tokens=512,
                reasoning_effort=body["reasoning_mode"],
                stream=stream,
            )

            async def request_body():
                return body

            request = SimpleNamespace(
                json=request_body,
                app=SimpleNamespace(state=SimpleNamespace(http=object())),
            )
            if not terminal_ok:
                from fastapi import HTTPException

                if stream:
                    response = await app.openai_gateway_chat(request)
                    text = "".join([piece async for piece in response.body_iterator])
                    assert '"error"' in text and "[DONE]" not in text
                    assert '"tool_calls"' not in text and "SECRET" not in text
                else:
                    with pytest.raises(HTTPException) as error:
                        await app.openai_gateway_chat(request)
                    assert error.value.status_code == 503 and "SECRET" not in str(
                        error.value.detail
                    )
                assert (
                    b.count_calls == 1
                    and b.coord.snapshot("shared").state == "reconciling"
                )
                assert b.memory.view("shared").committed_bytes == 600
                assert (
                    app._CHAT_NATIVE_TOOLS.get() is None
                    and not app._CANONICAL_NATIVE_CONSUMER.get()
                )
                return
            response = await app.openai_gateway_chat(request)
            if stream:
                text = "".join([piece async for piece in response.body_iterator])
                assert "data: [DONE]" in text
                packets = [
                    json.loads(line[6:])
                    for line in text.splitlines()
                    if line.startswith("data: {")
                ]
                if backend == "lmstudio":
                    assert packets[-1]["usage"] is None
                call = next(
                    p["choices"][0]["delta"]["tool_calls"][0]
                    for p in packets
                    if p["choices"][0]["delta"].get("tool_calls")
                )
            else:
                packet = json.loads(response.body)
                if backend == "lmstudio":
                    assert packet["usage"] is None
                call = packet["choices"][0]["message"]["tool_calls"][0]
            assert call["id"] == "native-call-id"
            assert call["function"] == {"name": "read", "arguments": arguments}
            assert (
                b.count_calls == 1 and b.coord.snapshot("shared").active_requests == 0
            )
            assert (
                app._CHAT_NATIVE_TOOLS.get() is None
                and not app._CANONICAL_NATIVE_CONSUMER.get()
            )
        finally:
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_consumer_close_retains_native_uncertainty_and_peak_not_false_completion(
    tmp_path, backend
):
    async def run():
        b = await context(tmp_path, backend)
        try:
            source = b.executor.stream(b.call)
            assert (await anext(source))["type"] in ("content", "reasoning")
            await source.aclose()
            assert b.wire.closed and b.memory.view("shared").committed_bytes == 600
            assert b.coord.snapshot("shared").state == "reconciling"
        finally:
            b.coord.close()

    asyncio.run(run())


def test_standalone_native_namespaces_execute_real_kernels_without_repo_module_imports(
    tmp_path,
):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "studio.py", tmp_path / "studio.py")
    code = """import sys,pathlib,importlib.abc,asyncio
root=pathlib.Path(REPO)
sys.path[:]=[p for p in sys.path if pathlib.Path(p or '.').resolve()!=root]
public={'local_runtime_lifecycle','model_lifecycle_coordinator','shared_memory_admission','omlx_lifecycle','ollama_protocol','ollama_lifecycle','lmstudio_protocol','lmstudio_lifecycle','request_fit','request_fit_admission','native_dispatch_executor','native_transition_driver','native_inference_scheduler'}
class NoSiblingFallback(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in public:raise AssertionError('standalone sibling import: '+fullname)
sys.meta_path.insert(0,NoSiblingFallback())
import studio
n=studio.native_dispatch_executor
assert n.__file__.startswith('<embedded-native/')
pkg=sys.modules[n.__package__]
assert sys.modules[n.__package__+'.runtime_persistence'] is studio
assert sys.modules[n.__package__+'.runtime_dispatch_boundary'] is studio
assert not public.intersection(sys.modules)
# All executor/fit/native no-I/O guards share the actual standalone transaction scope.
with studio._write_transaction_scope():
 try:n.assert_no_active_write_transaction('probe')
 except studio.TransactionDisciplineError:pass
 else:raise AssertionError('split transaction tracker')
# Import offline test producers only AFTER the isolated no-sibling assertion;
# aliases below are test composition, never production bundle registration.
for name in public:sys.modules[name]=getattr(pkg,name)
sys.modules['runtime_persistence']=studio
sys.modules['runtime_dispatch_boundary']=studio
sys.modules['execution_profiles']=studio
sys.path.insert(0,str(root/'tests'))
from test_native_dispatch_executor import context,collect
async def run():
 for backend in ('ollama','lmstudio'):
  b=await context(pathlib.Path(backend),backend)
  try:
   result=await collect(b.executor.stream(b.call))
   assert result[-1]['type']=='usage' and b.count_calls==1
   assert b.coord.snapshot('shared').active_requests==0
  finally:b.coord.close()
asyncio.run(run())
from test_native_transition_driver import fixture,admission,loaded
async def cold():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('cold-'+backend),backend)
  try:
   queued=b.transition.enqueue(admission(b),'isolated-cold','shared',deadline_ms=100000)
   req,mem=await loaded(b,queued)
   assert req.target.runtime_id==backend and mem.kind=='model'
   assert b.memory.view('shared').committed_bytes==600
  finally:b.coord.close()
asyncio.run(cold())
from test_native_inference_scheduler import scheduler
async def scheduled():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('scheduled-'+backend),backend)
  try:
   result=await collect(scheduler(b).stream(admission(b).call))
   assert result[-1]['type']=='usage' and b.count_calls==1
   assert b.coord.snapshot('shared').active_requests==0
  finally:b.coord.close()
asyncio.run(scheduled())
from test_omlx_dispatch_integration import environment,scheduler as omlx_scheduler,source
from pytest import MonkeyPatch
async def omlx_source():
 for backend in ('ollama','lmstudio'):
  with MonkeyPatch.context() as patch:
   env,b=environment(pathlib.Path('omlx-source-'+backend),patch,backend)
   with b.coord:
    await source.start_loaded(*env)
    env[4].calls.clear()
    result=await collect(omlx_scheduler(env,b).stream(admission(b).call))
    assert result[-1]['type']=='usage' and b.count_calls==1
    assert len(env[4].posts)==1 and env[4].posts[0].path.endswith('/unload')
    assert 'omlx' not in env[2].models
asyncio.run(omlx_source())
from test_model_lifecycle_controls import load_reservation
async def explicit_load_kernel():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('load-control-'+backend),backend)
  with b.coord:
   result=await collect(scheduler(b).stream(admission(b).call))
   assert result[-1]['type']=='usage' and b.count_calls==1
   b.now+=1
   control=load_reservation(b.coord,b.fleet,target=b.q.profile.memory.target,now=b.now)
   assert type(control) is pkg.model_lifecycle_coordinator.LoadLease
   assert not isinstance(control,pkg.model_lifecycle_coordinator.RequestLease)
   assert b.coord.finish_load(control,b.fleet.evidence(b.coord,b.now),reference='fake-load-proof',now_ms=b.now)
   assert b.count_calls==1 and b.memory.view('shared').committed_bytes==600
asyncio.run(explicit_load_kernel())
from test_native_load_control import scheduler as load_scheduler,call as load_call
async def native_load_command():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('native-load-'+backend),backend)
  with b.coord:
   execute=load_scheduler(b)
   assert await execute.command(load_call(b))=={'status':'confirmed'}
   assert await execute.command(load_call(b))=={'status':'confirmed'}
   assert len(b.driver.mutations(b))==1 and getattr(b,'count_calls',0)==0
   assert (await collect(execute.stream(admission(b).call)))[-1]['type']=='usage'
   assert b.count_calls==1 and b.memory.view('shared').committed_bytes==600
   cls=pkg.native_transition_driver.OllamaLoadCompletion if backend=='ollama' else pkg.native_transition_driver.LmLoadCompletion
   original=cls.complete
   async def missing_receipt(job):
    assert await original(job) is True
    b.coord._call(lambda conn:conn.execute('DELETE FROM journal WHERE operation_id=? AND event=?',(job.permit.intent.lease_id,backend+'_load_verified')))
    return True
   cls.complete=missing_receipt
   try:
    await execute.command(load_call(b))
    raise AssertionError('missing private native receipt accepted')
   except pkg.runtime_dispatch_boundary.RuntimeDispatchDenied: pass
   finally: cls.complete=original
asyncio.run(native_load_command())
from test_model_lifecycle_release import opened as release_opened,loaded as release_loaded,release as release_step,physical as release_physical,sample as release_sample
coord,memory,fleet=release_opened(pathlib.Path('release-only-kernel'))
with coord:
 model=release_loaded(coord,memory,fleet)
 step=release_step(coord)
 assert type(step) is pkg.model_lifecycle_coordinator.ReleaseStep and not hasattr(step,'target')
 assert coord.complete_release(step,reference='fake-physical',now_ms=1000,evidence=release_physical(coord,fleet,step))
 assert coord.claim_next('shared',now_ms=1000) is None
 assert not coord.finish_unload(step,reference='held-memory',now_ms=1000)
 memory.settle(model,release_sample(memory,now=1000),terminal=True,reference='actual-zero',now_ms=1000)
 assert coord.finish_unload(step,reference='barrier-retired',now_ms=1000)
 assert coord.snapshot('shared').state=='idle'
from test_native_unload_steps import enqueue as native_release_enqueue,drained as native_release_drained,job as native_release_job
async def native_release_steps():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('native-release-'+backend),backend)
  with b.coord:
   assert await load_scheduler(b).command(load_call(b))=={'status':'confirmed'}
   b.wire.calls.clear()
   queued=native_release_enqueue(b)
   step=await native_release_drained(b,queued)
   result=await b.transition.advance_release(step,queued,native_release_job(b,step,queued))
   assert type(result) is pkg.native_transition_driver.ReleaseResult and result.retired
   assert b.memory.view('shared').committed_bytes==0 and b.coord.snapshot('shared').state=='idle'
   assert len(b.driver.mutations(b))==1 and getattr(b,'count_calls',0)==0
asyncio.run(native_release_steps())
from test_native_unload_control import scheduler as unload_scheduler
from test_native_unload_steps import call as unload_call
async def native_unload_command():
 for backend in ('ollama','lmstudio'):
  b=fixture(pathlib.Path('native-unload-command-'+backend),backend)
  with b.coord:
   execute=unload_scheduler(b)
   assert await execute.command(load_call(b))=={'status':'confirmed'}
   b.wire.calls.clear()
   assert await execute.command(unload_call(b))=={'status':'confirmed'}
   assert b.memory.view('shared').committed_bytes==0 and b.coord.snapshot('shared').state=='idle'
   assert len(b.driver.mutations(b))==1 and getattr(b,'count_calls',0)==0
asyncio.run(native_unload_command())
from test_omlx_unload_control import loaded as loaded_omlx,scheduler as omlx_unload_scheduler,call as omlx_unload_call
async def native_omlx_unload_command():
 env,lease=await loaded_omlx(pathlib.Path('native-omlx-unload-command'))
 with env[0]:
  execute=omlx_unload_scheduler(env,lease)
  assert await execute.command(omlx_unload_call())=={'status':'confirmed'}
  assert env[1].view('shared').committed_bytes==0 and env[0].snapshot('shared').state=='idle'
  assert len(env[4].posts)==1 and not env[2].models
asyncio.run(native_omlx_unload_command())
import pytest
from test_native_scheduler_ownership import test_real_foreign_inference_lease_cannot_send_this_call_under_another_request
for backend in ('ollama','lmstudio'):
 with pytest.MonkeyPatch.context() as patch:
  test_real_foreign_inference_lease_cannot_send_this_call_under_another_request(pathlib.Path('private-foreign-lease-'+backend),patch,backend)
from test_native_transition_ownership import test_same_target_does_not_make_another_fifo_acquire_claim_ours
for backend in ('ollama','lmstudio'):
 for actor in ('inference','load'):
  with pytest.MonkeyPatch.context() as patch:
   test_same_target_does_not_make_another_fifo_acquire_claim_ours(pathlib.Path('private-foreign-transition-'+backend+'-'+actor),patch,backend,actor)
print('isolated native executor and shared transaction guards passed')
""".replace("REPO", repr(str(root)))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert "isolated native executor" in result.stdout


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_concurrent_same_lease_count_cannot_replay_or_double_native_send(
    tmp_path, backend
):
    async def run():
        b = await context(tmp_path, backend)
        release, entered = asyncio.Event(), asyncio.Event()
        original = b.job.fit.counter

        async def held_counter(prepared, qualification):
            # The shared fixture normally reads b.last_binding immediately.
            # Preserve THIS invocation's observed binding across the barrier;
            # a rejected concurrent attempt may observe a newer reference.
            binding = b.last_binding
            entered.set()
            await release.wait()
            return replace(await original(prepared, qualification), binding=binding)

        b.job = replace(b.job, fit=replace(b.job.fit, counter=held_counter))
        before = len(b.driver.mutations(b))
        first = asyncio.create_task(collect(b.executor.stream(b.call)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(ne.NativeFitDenied) as error:
                await collect(b.executor.stream(b.call))
            assert error.value.to_mapping()["code"] == "counter_already_dispatched"
            release.set()
            assert (await first)[-1]["type"] == "usage"
            assert b.count_calls == 1 and len(b.driver.mutations(b)) == before + 1
        finally:
            release.set()
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_queued_control_cannot_own_transition_while_real_native_stream_is_active(
    tmp_path, backend
):
    from test_model_lifecycle_coordinator import B, A

    async def run():
        b = await context(tmp_path, backend)
        try:
            source = b.executor.stream(b.call)
            await anext(source)
            other = B if b.request.target != B else A
            b.coord.enqueue(
                "explicit-next", "shared", other, deadline_ms=100000, now_ms=b.now
            )
            assert b.coord.claim_next("shared", now_ms=b.now) is None
            assert b.coord.snapshot("shared").active_requests == 1
            assert len(b.fleet.models) == 1
            result = await collect(source)
            assert result[-1]["type"] == "usage"
            next_step = b.coord.claim_next("shared", now_ms=b.now)
            assert next_step.kind == "drain" and next_step.target == other
            assert len(b.fleet.models) == 1  # No ACK, inferred release or target load.
            assert b.memory.view("shared").committed_bytes == 600
        finally:
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_unproven_native_terminality_withholds_tools_usage_and_keeps_holds(
    tmp_path, backend
):
    async def run():
        b = await context(tmp_path, backend)

        async def unproven():
            raise RuntimeError("SECRET terminal producer")

        b.job = replace(b.job, terminal=unproven)
        seen = []
        try:
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                async for event in b.executor.stream(b.call):
                    seen.append(event)
            assert "SECRET" not in str(error.value)
            assert not any(e["type"] in ("usage", "tool_call") for e in seen)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
        finally:
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_gateway_consumer_close_propagates_immediately_through_all_iterators(
    tmp_path, monkeypatch, backend
):
    import app
    from types import SimpleNamespace

    async def run():
        b = await context(tmp_path, backend)
        try:
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {backend: b.executor}, {"fixture": backend}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: ("cloud", {"alias": "fixture"}, b.call.model),
            )
            inputs = rf.decode(b.call.payload)
            body = dict(
                model="fixture/model",
                messages=inputs["messages"],
                temperature=0.2,
                max_tokens=512,
                reasoning_effort=inputs["reasoning_mode"],
                stream=True,
            )
            response = await app._coordinated_gateway_chat(
                SimpleNamespace(
                    app=SimpleNamespace(state=SimpleNamespace(http=object()))
                ),
                body,
            )
            first = await anext(response.body_iterator)
            assert first.startswith("data: ") and "[DONE]" not in first
            await response.body_iterator.aclose()
            assert b.wire.closed
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").committed_bytes == 600
            assert (
                app._CHAT_NATIVE_TOOLS.get() is None
                and not app._CANONICAL_NATIVE_CONSUMER.get()
            )
        finally:
            b.coord.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        {"response_format": {"type": "json_object"}},
        {"tool_choice": "required"},
        {"stream": "true"},
        {"max_tokens": 512, "max_completion_tokens": 4096},
    ],
)
def test_coordinated_gateway_rejects_unsupported_fields_before_count_or_send(
    monkeypatch, fields
):
    import app
    from fastapi import HTTPException
    from types import SimpleNamespace

    async def forbidden(*a, **k):
        raise AssertionError("invalid gateway input reached inference")
        yield

    monkeypatch.setattr(app, "stream_any_model", forbidden)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            app._coordinated_gateway_chat(
                SimpleNamespace(),
                dict(
                    model="local", messages=[{"role": "user", "content": "x"}], **fields
                ),
            )
        )
    assert error.value.status_code == 400


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_legacy_text_tool_consumer_refuses_before_native_planning(
    tmp_path, monkeypatch, backend
):
    import app

    async def run():
        b = await context(tmp_path, backend)
        try:
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {backend: b.executor}, {"fixture": backend}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: ("cloud", {"alias": "fixture"}, b.call.model),
            )
            token = app._CHAT_NATIVE_TOOLS.set(
                [{"type": "function", "function": {"name": "read"}}]
            )
            try:
                with pytest.raises(rd.RuntimeDispatchDenied) as error:
                    await collect(
                        app.stream_any_model(object(), "fixture", [], 0.2, 512, "off")
                    )
                assert error.value.code == "canonical_consumer_required"
                assert b.count_calls == 0
            finally:
                app._CHAT_NATIVE_TOOLS.reset(token)
        finally:
            b.coord.close()

    asyncio.run(run())
