from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
from workflow_orchestration import WorkflowEvent, WorkflowOrchestrator, WorkflowStage


def _run(events: AsyncIterator[WorkflowEvent]) -> list[WorkflowEvent]:
    async def collect() -> list[WorkflowEvent]:
        return [event async for event in events]

    return asyncio.run(collect())


def _stage(stage_id: int | str, model: str, prompt: str = "task") -> WorkflowStage:
    return WorkflowStage(stage_id, str(stage_id), str(stage_id), model, "system", lambda _ctx: prompt, 64)


def test_sequential_stages_preserve_context_usage_and_cleanup() -> None:
    calls: list[tuple[str, str, str]] = []
    recorded: list[tuple[str, str, Mapping[str, Any]]] = []
    unloaded: list[str] = []

    def stream(model: str, messages: list[dict[str, str]], _temperature: float, _max_tokens: int, reasoning: str) -> AsyncIterator[Mapping[str, Any]]:
        async def generate() -> AsyncIterator[Mapping[str, Any]]:
            calls.append((model, messages[-1]["content"], reasoning))
            yield {"type": "content", "text": model}
            yield {"type": "usage", "completion_tokens": 1, "decode_tps": 10}

        return generate()

    async def unload(model: str) -> None:
        unloaded.append(model)

    orchestrator = WorkflowOrchestrator(
        stream_model=stream,
        record_usage=lambda workflow, model, usage: recorded.append((workflow, model, usage)),
        is_local_model=lambda model: model.startswith("local"),
        unload_model=unload,
    )
    stages = [
        _stage(1, "local-one"),
        WorkflowStage(2, "second", "Second", "cloud-two", "system", lambda context: context["1"], 64),
    ]

    events = _run(orchestrator.run_stages(stages, workflow="council", auto_unload=True))

    done = [event.result for event in events if event.kind == "stage_done"]
    assert [result.content for result in done if result is not None] == ["local-one", "cloud-two"]
    assert calls[1][1] == "local-one"
    assert unloaded == ["local-one"]
    assert [(workflow, model) for workflow, model, _usage in recorded] == [
        ("council", "local-one"),
        ("council", "cloud-two"),
    ]
    assert [event.kind for event in events] == [
        "stage_start", "token", "stage_done", "unloading", "stage_start", "token", "stage_done"
    ]


def test_disconnect_is_fail_closed_before_provider_invocation() -> None:
    calls = 0

    def stream(*_args: object) -> AsyncIterator[Mapping[str, Any]]:
        async def generate() -> AsyncIterator[Mapping[str, Any]]:
            nonlocal calls
            calls += 1
            yield {"type": "content", "text": "must not run"}

        return generate()

    async def disconnected() -> bool:
        return True

    orchestrator = WorkflowOrchestrator(
        stream_model=stream,
        record_usage=lambda *_args: None,
        is_local_model=lambda _model: True,
        unload_model=lambda _model: asyncio.sleep(0),
    )

    events = _run(orchestrator.run_stages([_stage(1, "local")], workflow="chat", auto_unload=True, is_disconnected=disconnected))

    assert [event.kind for event in events] == ["disconnected"]
    assert calls == 0


def test_provider_failure_propagates_and_a_later_run_can_recover() -> None:
    failed = True

    def stream(*_args: object) -> AsyncIterator[Mapping[str, Any]]:
        async def generate() -> AsyncIterator[Mapping[str, Any]]:
            nonlocal failed
            if failed:
                failed = False
                raise RuntimeError("provider unavailable")
            yield {"type": "content", "text": "recovered"}

        return generate()

    orchestrator = WorkflowOrchestrator(
        stream_model=stream,
        record_usage=lambda *_args: None,
        is_local_model=lambda _model: False,
        unload_model=lambda _model: asyncio.sleep(0),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _run(orchestrator.run_stages([_stage(1, "provider")], workflow="debate", auto_unload=False))

    events = _run(orchestrator.run_stages([_stage(1, "provider")], workflow="debate", auto_unload=False))
    result = next(event.result for event in events if event.kind == "stage_done")
    assert result is not None and result.content == "recovered"


def test_local_reasoning_only_stage_retries_without_thinking() -> None:
    modes: list[str] = []

    def stream(_model: str, _messages: list[dict[str, str]], _temperature: float, _max_tokens: int, reasoning: str) -> AsyncIterator[Mapping[str, Any]]:
        async def generate() -> AsyncIterator[Mapping[str, Any]]:
            modes.append(reasoning)
            if reasoning != "off":
                yield {"type": "reasoning", "text": "thinking"}
                yield {"type": "usage", "completion_tokens": 1}
            else:
                yield {"type": "content", "text": "answer"}
                yield {"type": "usage", "completion_tokens": 1}

        return generate()

    orchestrator = WorkflowOrchestrator(
        stream_model=stream,
        record_usage=lambda *_args: None,
        is_local_model=lambda _model: True,
        unload_model=lambda _model: asyncio.sleep(0),
    )

    events = _run(orchestrator.run_stages([WorkflowStage(1, "local", "Local", "local", "system", lambda _c: "task", 64, "high")], workflow="council", auto_unload=False))
    result = next(event.result for event in events if event.kind == "stage_done")
    assert modes == ["high", "off"]
    assert result is not None and result.content == "answer" and result.reasoning == "thinking"


def test_council_and_debate_routes_keep_existing_sse_event_contract(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_skills(*_args: object) -> tuple[str, list[dict[str, Any]]]:
        return "", []

    async def fake_stream(
        _client: Any,
        model: str,
        messages: list[dict[str, str]],
        _temperature: float = 0.0,
        _max_tokens: int = 4096,
        _reasoning_mode: str = "off",
    ) -> AsyncIterator[dict[str, Any]]:
        del messages
        yield {"type": "content", "text": f"reply:{model}"}
        yield {
            "type": "usage",
            "decode_tps": 1.0,
            "prefill_tps": 1.0,
            "ttft": 0.1,
            "completion_tokens": 1,
            "cached_tokens": 0,
        }

    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "workflow-api.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    monkeypatch.setattr(studio, "build_skills_context", no_skills)
    monkeypatch.setattr(studio, "stream_any_model", fake_stream)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()

    with TestClient(studio.app) as client:
        council = client.post(
            "/api/council/stream",
            json={
                "task": "fixture task",
                "architect_model": "model-a",
                "coder_model": "model-b",
                "critic_model": "model-c",
                "judge_model": "model-d",
                "auto_unload": False,
            },
        )
        debate = client.post(
            "/api/debate/stream",
            json={
                "prompt": "fixture prompt",
                "models": ["model-a", "model-b"],
                "judge_model": "model-c",
                "auto_unload": False,
            },
        )

    assert council.status_code == 200
    assert "event: stage_start" in council.text
    assert '"stage": 4' in council.text
    assert "event: council_done" in council.text
    assert debate.status_code == 200
    assert "event: panel_start" in debate.text
    assert '"slot": "synthesis"' in debate.text
    assert "event: synthesis_done" in debate.text
    assert "event: debate_done" in debate.text
    with sqlite3.connect(studio.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode IN ('council', 'debate')").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM perf_samples WHERE mode IN ('council', 'debate')").fetchone()[0] == 7
