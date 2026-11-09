from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app


@pytest.mark.parametrize(
    ("unload_status", "expected"),
    [(200, ["model-a"]), (500, []), (302, [])],
)
def test_other_model_cleanup_reports_only_successful_http_acknowledgements(
    monkeypatch, unload_status, expected
):
    calls: list[httpx.Request] = []

    async def online(_client: httpx.AsyncClient) -> bool:
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/admin/api/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "keep", "loaded": True},
                        {"id": "virtual", "loaded": True, "virtual": True},
                        {"id": "model-a", "loaded": True},
                        {"id": "not-loaded", "loaded": False},
                    ]
                },
            )
        headers = (
            {"location": "https://elsewhere.example/"} if unload_status == 302 else {}
        )
        return httpx.Response(unload_status, headers=headers)

    monkeypatch.setattr(app, "_omlx_is_online", online)
    monkeypatch.setattr(
        app, "_local_runtime_dispatch", lambda: SimpleNamespace(coordinated=False)
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            result = await app._unload_other_omlx_models(client, "keep")
        assert result == expected
        assert len(calls[1:]) == 1
        assert calls[1].url.path.endswith("/model-a/unload")

    asyncio.run(run())


def test_other_model_cleanup_keeps_prior_acknowledgements_on_later_failure(
    monkeypatch,
):
    unload_calls = 0

    async def online(_client: httpx.AsyncClient) -> bool:
        return True

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal unload_calls
        if request.url.path.endswith("/admin/api/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"id": "model-a", "loaded": True},
                        {"id": "model-b", "loaded": True},
                    ]
                },
            )
        unload_calls += 1
        return httpx.Response(200 if unload_calls == 1 else 503)

    monkeypatch.setattr(app, "_omlx_is_online", online)
    monkeypatch.setattr(
        app, "_local_runtime_dispatch", lambda: SimpleNamespace(coordinated=False)
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            result = await app._unload_other_omlx_models(client, "keep")
        assert result == ["model-a"]
        assert unload_calls == 2

    asyncio.run(run())


def test_other_model_cleanup_propagates_cancellation(monkeypatch):
    entered = asyncio.Event()

    async def online(_client: httpx.AsyncClient) -> bool:
        return True

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(app, "_omlx_is_online", online)
    monkeypatch.setattr(
        app, "_local_runtime_dispatch", lambda: SimpleNamespace(coordinated=False)
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            task = asyncio.create_task(app._unload_other_omlx_models(client, "keep"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
