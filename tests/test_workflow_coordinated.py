"""Council/Debate application seams with fake coordinated backends, not native proof."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app
import runtime_dispatch_boundary as rd
from workflow_orchestration import WorkflowStage


class Backend:
    def __init__(self, kind="content", status="confirmed"):
        self.kind, self.status = kind, status
        self.calls, self.commands, self.closed = [], [], []

    async def stream(self, call):
        self.calls.append(call)
        try:
            yield {
                "type": self.kind,
                "text": "REASONING" if self.kind == "reasoning" else "ANSWER",
                "id": "native-id",
                "name": "read",
                "arguments": "{}",
            }
            yield {"type": "usage", "completion_tokens": 1, "finish_reason": "stop"}
        finally:
            self.closed.append(call)

    async def command(self, call):
        self.commands.append(call)
        return {"status": self.status}


def install(monkeypatch, backend):
    monkeypatch.setattr(app, "list_cloud_providers", lambda **kwargs: [])
    monkeypatch.setattr(
        app,
        "_LOCAL_RUNTIME_DISPATCH",
        rd.RuntimeDispatchBoundary(True, {"omlx": backend}),
    )

    async def no_probe(*args, **kwargs):
        raise AssertionError("coordinated workflow reached legacy probe")

    monkeypatch.setattr(app, "_omlx_is_online", no_probe)
    monkeypatch.setattr(app, "record_perf", lambda *args, **kwargs: None)


async def route(monkeypatch, workflow, *, auto_unload=False):
    skills = "\nSKILL-ANCHOR ಕರ್ನಾಟಕ 😀\n"

    async def build(*args):
        return skills, []

    monkeypatch.setattr(app, "build_skills_context", build)
    saved = []
    monkeypatch.setattr(
        app, "save_run", lambda *args: saved.append(args) or "offline-run"
    )
    text = "SECURITY-INPUT " + "keep all text ಕರ್ನಾಟಕ 😀 " * 3000 + "LATEST-END"
    payload = (
        {
            "task": text,
            "architect_model": "model-a",
            "coder_model": "model-b",
            "critic_model": "model-c",
            "judge_model": "model-d",
            "reasoning_mode": "high",
            "auto_unload": auto_unload,
        }
        if workflow == "council"
        else {
            "prompt": text,
            "models": ["model-a", "model-b"],
            "judge_model": "model-c",
            "auto_unload": auto_unload,
        }
    )

    async def body():
        return payload

    async def connected():
        return False

    request = SimpleNamespace(
        json=body,
        is_disconnected=connected,
        app=SimpleNamespace(state=SimpleNamespace(http=object())),
    )
    response = await (
        app.api_council_stream(request)
        if workflow == "council"
        else app.api_debate_stream(request)
    )
    packets = [item async for item in response.body_iterator]
    return (
        "".join(item.decode() if isinstance(item, bytes) else item for item in packets),
        saved,
        text,
        skills,
    )


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_coordinated_reasoning_only_workflow_never_retries_with_changed_budget(
    monkeypatch, workflow
):
    async def run():
        backend = Backend("reasoning")
        install(monkeypatch, backend)
        output, saved, _, _ = await route(monkeypatch, workflow)
        expected = 4 if workflow == "council" else 3
        assert len(backend.calls) == expected
        assert [json.loads(c.payload)["max_tokens"] for c in backend.calls] == (
            [2400, 4096, 2400, 6144] if workflow == "council" else [2400, 2400, 3200]
        )
        assert "event: error" not in output and len(saved) == 1

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_workflow_consumer_close_immediately_reaches_coordinated_source(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        orchestrator = app._workflow_orchestrator(object(), workflow)
        stage = WorkflowStage(
            1, "one", "One", "model-a", "system", lambda _: "task", 73
        )
        events = orchestrator.run_stages([stage], workflow=workflow, auto_unload=True)
        assert (await anext(events)).kind == "stage_start"
        assert (await anext(events)).kind == "token"
        await events.aclose()
        # Assert before event-loop shutdown/async-generator GC can conceal a leak.
        assert len(backend.closed) == 1
        assert backend.commands == []

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_workflow_disconnect_closes_coordinated_source_before_return(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        checks = 0

        async def disconnected():
            nonlocal checks
            checks += 1
            return checks > 1

        stage = WorkflowStage(
            1, "one", "One", "model-a", "system", lambda _: "task", 73
        )
        events = [
            v
            async for v in app._workflow_orchestrator(object(), workflow).run_stages(
                [stage],
                workflow=workflow,
                auto_unload=True,
                is_disconnected=disconnected,
            )
        ]
        assert [v.kind for v in events] == ["stage_start", "disconnected"]
        assert len(backend.closed) == 1 and backend.commands == []

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_route_preserves_full_stage_input_and_prior_stage_context(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        output, saved, text, skills = await route(monkeypatch, workflow)
        calls = [json.loads(call.payload) for call in backend.calls]
        assert len(calls) == (4 if workflow == "council" else 3)
        for index, call in enumerate(calls):
            assert text in call["messages"][1]["content"]
            assert (
                len(call["messages"]) == 2 and call["messages"][0]["role"] == "system"
            )
            assert (skills in call["messages"][0]["content"]) is not (
                workflow == "council" and index == 2
            )
            assert call["tools"] is None
        if workflow == "council":
            assert (
                calls[0]["messages"][0]["content"]
                == app.AGENT_ROLES["architect"]["system"] + skills
            )
            assert (
                calls[1]["messages"][1]["content"]
                == f"Matter / Task:\n{text}\n\nSeat 1 (Architect / Strategist) Output:\nANSWER"
            )
            assert [v["reasoning_mode"] for v in calls] == [
                "high",
                "off",
                "off",
                "high",
            ]
        else:
            assert calls[0]["messages"][1]["content"] == text
            assert (
                "=== Opinion 1 (model-a) ===\nANSWER"
                in calls[-1]["messages"][1]["content"]
            )
            assert (
                "=== Opinion 2 (model-b) ===\nANSWER"
                in calls[-1]["messages"][1]["content"]
            )
        assert "event: error" not in output and len(saved) == 1

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
@pytest.mark.parametrize("status", ["pending", "uncertain", "refused", "ok"])
def test_unconfirmed_cleanup_cannot_start_next_stage_or_save_success(
    monkeypatch, workflow, status
):
    async def run():
        backend = Backend(status=status)
        install(monkeypatch, backend)
        output, saved, _, _ = await route(monkeypatch, workflow, auto_unload=True)
        assert len(backend.calls) == len(backend.commands) == 1
        assert "event: error" in output and f"event: {workflow}_done" not in output
        assert saved == []

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_text_workflow_refuses_unexpected_native_tool_event_without_success(
    monkeypatch, workflow
):
    async def run():
        backend = Backend("tool_call")
        install(monkeypatch, backend)
        output, saved, _, _ = await route(monkeypatch, workflow)
        assert len(backend.calls) == len(backend.closed) == 1
        assert "canonical_consumer_required" in output
        assert saved == [] and backend.commands == []
        assert f"event: {workflow}_done" not in output

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_advertised_tools_refuse_before_workflow_backend_planning(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        token = app._CHAT_NATIVE_TOOLS.set(
            [{"type": "function", "function": {"name": "read"}}]
        )
        try:
            output, saved, _, _ = await route(monkeypatch, workflow)
            assert "canonical_consumer_required" in output
            assert saved == [] and backend.calls == backend.commands == []
        finally:
            app._CHAT_NATIVE_TOOLS.reset(token)

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_confirmed_cleanup_permits_next_stage_without_claiming_native_proof(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        output, saved, _, _ = await route(monkeypatch, workflow, auto_unload=True)
        count = 4 if workflow == "council" else 3
        assert (
            len(backend.calls) == len(backend.commands) == len(backend.closed) == count
        )
        assert [c.model for c in backend.calls] == [c.model for c in backend.commands]
        assert len(saved) == 1 and f"event: {workflow}_done" in output

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_response_consumer_close_reaches_backend_without_saving_run(
    monkeypatch, workflow
):
    async def run():
        backend = Backend()
        install(monkeypatch, backend)
        saved = []
        monkeypatch.setattr(app, "save_run", lambda *args: saved.append(args))

        async def skills(*args):
            return "", []

        monkeypatch.setattr(app, "build_skills_context", skills)

        async def body():
            return {
                "task": "task",
                "prompt": "task",
                "models": ["model-a"],
                "architect_model": "model-a",
            }

        async def connected():
            return False

        request = SimpleNamespace(
            json=body,
            is_disconnected=connected,
            app=SimpleNamespace(state=SimpleNamespace(http=object())),
        )
        response = await (
            app.api_council_stream(request)
            if workflow == "council"
            else app.api_debate_stream(request)
        )
        source = response.body_iterator
        while "event: token" not in await anext(source):
            pass
        await source.aclose()
        assert len(backend.closed) == 1 and backend.commands == [] and saved == []

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_actual_task_cancellation_closes_source_without_cleanup_or_success(
    monkeypatch, workflow
):
    async def run():
        entered = asyncio.Event()

        class Blocked(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield {"type": "content", "text": "prefix"}
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    self.closed.append(call)

        backend = Blocked()
        install(monkeypatch, backend)
        saved = []
        monkeypatch.setattr(app, "save_run", lambda *args: saved.append(args))
        stage = WorkflowStage(
            1, "one", "One", "model-a", "system", lambda _: "task", 73
        )

        async def consume():
            return [
                v
                async for v in app._workflow_orchestrator(
                    object(), workflow
                ).run_stages([stage], workflow=workflow, auto_unload=True)
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(backend.closed) == 1 and backend.commands == [] and saved == []

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_application_stage_reaches_actual_warm_fit_and_native_final_wire(
    tmp_path, monkeypatch, backend
):
    from test_native_dispatch_executor import context

    async def run():
        b = await context(tmp_path, backend)
        with b.coord:
            payload = json.loads(b.call.payload)
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
            monkeypatch.setattr(app, "record_perf", lambda *args, **kwargs: None)
            stage = WorkflowStage(
                1,
                "one",
                "One",
                "fixture-model",
                payload["messages"][0]["content"],
                lambda _: payload["messages"][1]["content"],
                payload["max_tokens"],
                payload["reasoning_mode"],
                payload["temperature"],
            )
            events = [
                v
                async for v in app._workflow_orchestrator(
                    object(), "council"
                ).run_stages([stage], workflow="council", auto_unload=False)
            ]
            result = next(v.result for v in events if v.kind == "stage_done")
            assert result.content == "READY" and b.count_calls == 1
            assert b.coord.snapshot("shared").active_requests == 0
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


def test_owner_matrix_matches_current_static_inventory_without_promoting_coverage():
    from pathlib import Path
    from scripts.runtime_dispatch_inventory import inventory

    root = Path(__file__).resolve().parents[1]
    matrix = json.loads(
        (root / "tests/fixtures/runtime_dispatch/workflow_coverage.json").read_text()
    )
    rows = inventory((root / "app.py").read_text())
    owners = matrix["owners"]
    assert len(owners) == len({row["owner"] for row in owners})
    assert {row["owner"] for row in owners} == {owner for owner, _, _ in rows}
    assert matrix["assembly_owners"] == [
        "api_council_stream.event_gen",
        "api_debate_stream.event_gen",
    ]
    for row in owners:
        assert row["seams"] == sorted(
            {seam for owner, seam, _ in rows if owner == row["owner"]}
        )
        assert (root / row["evidence"]).is_file()
        assessments = {
            "_workflow_orchestrator.stream_model": "bounded_workflow_adapter",
            "_workflow_orchestrator.unload_model": "bounded_workflow_adapter",
            "api_hall_summon.event_gen": "bounded_hall_http_summon",
            "_mcp_summon_model": "bounded_mcp_stdio_summon",
            "api_workspace_picker_suggest": "bounded_workspace_suggestion",
            "_unload_omlx_model": "bounded_legacy_cleanup_failure",
            "api_unload_model": "bounded_legacy_unload_route",
            "api_load_model": "bounded_legacy_load_route",
            "api_unload_all": "bounded_legacy_unload_all_route",
        }
        assert row["assessment"] == assessments.get(
            row["owner"], "inventory_only_in_this_audit"
        )


def test_standalone_workflow_adapter_has_no_sibling_fallback(tmp_path):
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "studio.py", tmp_path / "studio.py")
    code = """import importlib.abc,sys,pathlib
root=pathlib.Path(ROOT)
sys.path[:]=[p for p in sys.path if pathlib.Path(p or '.').resolve()!=root]
blocked={'app','workflow_orchestration','runtime_dispatch_boundary'}
class NoFallback(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in blocked:raise AssertionError('sibling fallback: '+fullname)
sys.meta_path.insert(0,NoFallback())
import studio
assert not blocked.intersection(sys.modules)
# Test-only aliases after proving the standalone imported no public sibling.
from types import SimpleNamespace
sys.modules['app']=studio
sys.modules['workflow_orchestration']=SimpleNamespace(WorkflowStage=studio.WorkflowStage)
sys.modules['runtime_dispatch_boundary']=studio.runtime_dispatch_boundary
sys.path.insert(0,str(root/'tests'))
import pytest
from test_workflow_coordinated import test_coordinated_reasoning_only_workflow_never_retries_with_changed_budget,test_response_consumer_close_reaches_backend_without_saving_run,test_unconfirmed_cleanup_cannot_start_next_stage_or_save_success
from test_workflow_hall_coordinated import test_hall_reasoning_only_never_retries_changed_budget_in_coordinated_mode,test_hall_close_or_disconnect_immediately_closes_source_without_saving,test_hall_cleanup_failure_keeps_completed_transcript_but_never_reports_summon_success
from test_workflow_mcp_coordinated import test_mcp_consumer_error_closes_owned_source_before_client_exit,test_mcp_preserves_full_matter_chronicle_role_and_original_options,test_mcp_unconfirmed_cleanup_never_appends_or_returns_success
from test_workflow_picker_coordinated import test_picker_oversize_output_closes_source_before_return_and_restores_tools,test_picker_preserves_entire_valid_description_and_fixed_options_without_authority,test_picker_native_tool_call_is_refused_not_executed
from test_workflow_unload_legacy import test_online_legacy_cleanup_rejects_unconfirmed_http_status_without_leaking_body
from test_workflow_unload_route import test_online_unload_route_refuses_non_success_status_privately
from test_workflow_load_route import test_online_load_route_refuses_non_success_status_privately
from test_workflow_unload_all_route import test_unload_all_route_refuses_unconfirmed_individual_cleanup_privately
with pytest.MonkeyPatch.context() as patch:
 test_unload_all_route_refuses_unconfirmed_individual_cleanup_privately(patch,500)
with pytest.MonkeyPatch.context() as patch:
 test_online_load_route_refuses_non_success_status_privately(patch,500)
with pytest.MonkeyPatch.context() as patch:
 test_online_unload_route_refuses_non_success_status_privately(patch,500)
with pytest.MonkeyPatch.context() as patch:
 test_online_legacy_cleanup_rejects_unconfirmed_http_status_without_leaking_body(patch,500)
with pytest.MonkeyPatch.context() as patch:
 test_picker_oversize_output_closes_source_before_return_and_restores_tools(patch)
with pytest.MonkeyPatch.context() as patch:
 test_picker_preserves_entire_valid_description_and_fixed_options_without_authority(patch)
with pytest.MonkeyPatch.context() as patch:
 test_picker_native_tool_call_is_refused_not_executed(patch)
with pytest.MonkeyPatch.context() as patch:
 test_mcp_consumer_error_closes_owned_source_before_client_exit(patch)
with pytest.MonkeyPatch.context() as patch:
 test_mcp_preserves_full_matter_chronicle_role_and_original_options(patch)
with pytest.MonkeyPatch.context() as patch:
 test_mcp_unconfirmed_cleanup_never_appends_or_returns_success(patch,"pending")
with pytest.MonkeyPatch.context() as patch:
 test_hall_reasoning_only_never_retries_changed_budget_in_coordinated_mode(patch)
with pytest.MonkeyPatch.context() as patch:
 test_hall_close_or_disconnect_immediately_closes_source_without_saving(patch,"close")
with pytest.MonkeyPatch.context() as patch:
 test_hall_cleanup_failure_keeps_completed_transcript_but_never_reports_summon_success(patch,"pending")
for workflow in ('council','debate'):
 with pytest.MonkeyPatch.context() as patch:
  test_coordinated_reasoning_only_workflow_never_retries_with_changed_budget(patch,workflow)
 with pytest.MonkeyPatch.context() as patch:
  test_response_consumer_close_reaches_backend_without_saving_run(patch,workflow)
 with pytest.MonkeyPatch.context() as patch:
  test_unconfirmed_cleanup_cannot_start_next_stage_or_save_success(patch,workflow,'pending')
print('isolated workflow retry/close/cleanup passed')
""".replace("ROOT", repr(str(root)))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert "isolated workflow retry/close/cleanup passed" in result.stdout


def test_legacy_application_adapter_keeps_reasoning_only_recovery(monkeypatch):
    async def run():
        calls = []
        monkeypatch.setattr(
            app, "_LOCAL_RUNTIME_DISPATCH", rd.RuntimeDispatchBoundary()
        )
        monkeypatch.setattr(app, "list_cloud_providers", lambda **kwargs: [])
        monkeypatch.setattr(app, "record_perf", lambda *args, **kwargs: None)

        async def legacy(client, model, messages, temperature, max_tokens, reasoning):
            calls.append((max_tokens, reasoning))
            yield {
                "type": "reasoning" if reasoning != "off" else "content",
                "text": "text",
            }

        monkeypatch.setattr(app, "stream_any_model", legacy)
        stage = WorkflowStage(
            1, "one", "One", "model-a", "system", lambda _: "task", 73, "high"
        )
        events = [
            v
            async for v in app._workflow_orchestrator(object(), "council").run_stages(
                [stage], workflow="council", auto_unload=False
            )
        ]
        assert calls == [(73, "high"), (4096, "off")]
        assert events[-1].result.content == "text"

    asyncio.run(run())


def test_iterator_only_provider_contract_remains_supported():
    from workflow_orchestration import WorkflowOrchestrator

    async def run():
        class IteratorOnly:
            sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return {"type": "content", "text": "answer"}

        async def unload(model):
            raise AssertionError("unexpected unload")

        orchestrator = WorkflowOrchestrator(
            stream_model=lambda *args: IteratorOnly(),
            record_usage=lambda *args: None,
            is_local_model=lambda _: False,
            unload_model=unload,
        )
        stage = WorkflowStage(
            1, "one", "One", "model-a", "system", lambda _: "task", 73
        )
        result = [
            v
            async for v in orchestrator.run_stages(
                [stage], workflow="council", auto_unload=False
            )
        ]
        assert result[-1].result.content == "answer"

    asyncio.run(run())


@pytest.mark.parametrize("workflow", ["council", "debate"])
def test_partial_backend_failure_cannot_cleanup_retry_or_save_whole_run(
    monkeypatch, workflow
):
    async def run():
        class Failed(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield {"type": "reasoning", "text": "prefix"}
                    raise OSError("private native failure")
                finally:
                    self.closed.append(call)

        backend = Failed()
        install(monkeypatch, backend)
        output, saved, _, _ = await route(monkeypatch, workflow, auto_unload=True)
        assert "coordinated_execution_unavailable" in output
        assert "private native failure" not in output
        assert len(backend.calls) == len(backend.closed) == 1
        assert saved == [] and backend.commands == []
        assert f"event: {workflow}_done" not in output

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_application_stage_close_reaches_native_bridge_and_retains_uncertain_charge(
    tmp_path, monkeypatch, backend
):
    from test_native_dispatch_executor import context

    async def run():
        b = await context(tmp_path, backend)
        with b.coord:
            payload = json.loads(b.call.payload)
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
            monkeypatch.setattr(app, "record_perf", lambda *args, **kwargs: None)
            stage = WorkflowStage(
                1,
                "one",
                "One",
                "fixture-model",
                payload["messages"][0]["content"],
                lambda _: payload["messages"][1]["content"],
                payload["max_tokens"],
                payload["reasoning_mode"],
                payload["temperature"],
            )
            events = app._workflow_orchestrator(object(), "council").run_stages(
                [stage], workflow="council", auto_unload=False
            )
            assert (await anext(events)).kind == "stage_start"
            assert (await anext(events)).kind == "token"
            await events.aclose()
            assert b.wire.closed and b.count_calls == 1
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.memory.view("shared").state == "paused"
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())
