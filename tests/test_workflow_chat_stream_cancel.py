from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app


@pytest.mark.parametrize(
    "surface",
    [
        "api_chat_stream",
        "openai_gateway_chat",
        "openai_gateway_chat_tools",
        "coordinated_gateway",
    ],
)
def test_chat_stream_consumer_close_closes_delegated_model_source(monkeypatch, surface):
    state = {"closed": False}
    first_event = (
        {"type": "reasoning", "text": "first thought"}
        if surface == "openai_gateway_chat_tools"
        else {"type": "content", "text": "first chunk"}
    )

    async def model_source(*_args, **_kwargs):
        try:
            yield first_event
            await asyncio.Event().wait()
        finally:
            state["closed"] = True

    async def payload():
        body = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "tools_enabled": False,
            "skills": [],
        }
        if surface == "openai_gateway_chat_tools":
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "read-only fixture",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        return body

    async def connected():
        return False

    request = SimpleNamespace(
        json=payload,
        is_disconnected=connected,
        app=SimpleNamespace(state=SimpleNamespace(http=object())),
    )
    monkeypatch.setattr(app, "stream_any_model", model_source)
    monkeypatch.setattr(app, "_heavy_model_preflight", lambda _model: None)

    async def run():
        if surface == "coordinated_gateway":
            coordinated_body = {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }
            response = await app._coordinated_gateway_chat(request, coordinated_body)
        elif surface == "api_chat_stream":

            async def no_skills(*_args, **_kwargs):
                return "", []

            monkeypatch.setattr(app, "build_skills_context", no_skills)
            response = await app.api_chat_stream(request)
        else:
            response = await app.openai_gateway_chat(request)
        first = await anext(response.body_iterator)
        expected_text = (
            "first thought" if surface == "openai_gateway_chat_tools" else "first chunk"
        )
        assert expected_text in first
        await response.body_iterator.aclose()
        assert state["closed"]

    asyncio.run(run())
