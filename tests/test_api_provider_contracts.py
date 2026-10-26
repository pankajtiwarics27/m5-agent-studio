from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio


def _configure_local_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "api-contracts.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()


async def _successful_stream(*_args: object, **_kwargs: object) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "reasoning", "text": "thinking"}
    yield {"type": "content", "text": "answer"}
    yield {"type": "usage", "completion_tokens": 1}


def test_openai_api_stream_preserves_reasoning_content_and_done_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_local_api(tmp_path, monkeypatch)
    monkeypatch.setattr(studio, "stream_any_model", _successful_stream)

    with TestClient(studio.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "fixture-model", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"reasoning_content": "thinking"' in response.text
    assert '"content": "answer"' in response.text
    assert '"finish_reason": "stop"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


def test_openai_api_stream_converts_provider_failure_to_safe_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_local_api(tmp_path, monkeypatch)

    async def failing_stream(*_args: object, **_kwargs: object) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("provider fixture failed")
        yield  # pragma: no cover

    monkeypatch.setattr(studio, "stream_any_model", failing_stream)
    with TestClient(studio.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "fixture-model", "messages": [{"role": "user", "content": "hello"}], "stream": True},
        )

    assert response.status_code == 200
    assert '"error": {"message": "provider fixture failed"}' in response.text


def test_slow_provider_stream_is_cancelled_by_timeout() -> None:
    cancelled = False

    async def slow_stream() -> AsyncIterator[dict[str, Any]]:
        nonlocal cancelled
        try:
            await asyncio.sleep(60)
            yield {"type": "content", "text": "too late"}
        finally:
            cancelled = True

    async def consume() -> None:
        async for _event in slow_stream():
            pass

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(consume(), timeout=0.01))
    assert cancelled is True
