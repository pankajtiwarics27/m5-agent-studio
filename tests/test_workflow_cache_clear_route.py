from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import app
import runtime_dispatch_boundary as rd


def _request(client):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=client)))


def _legacy(monkeypatch):
    monkeypatch.setattr(app, "OMLX_BASE", "http://127.0.0.1:8000")
    monkeypatch.setattr(app, "_local_runtime_dispatch", rd.RuntimeDispatchBoundary)

    async def online(_client):
        return True

    monkeypatch.setattr(app, "_omlx_is_online", online)


@pytest.mark.parametrize("status", [302, 400, 500, 503])
def test_online_cache_clear_route_refuses_non_success_privately(monkeypatch, status):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json={"detail": "PRIVATE_CACHE_RESPONSE"},
            headers={"Location": "http://evil.invalid"},
        )

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app.api_clear_ssd_cache(_request(client))
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_cache_clear_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_CACHE_RESPONSE" not in str(error.value.detail)
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert requests[0].url.path == "/admin/api/ssd-cache/clear"

    asyncio.run(run())


def test_online_cache_clear_route_normalizes_transport_and_invalid_json_privately(
    monkeypatch,
):
    _legacy(monkeypatch)

    async def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("PRIVATE_CACHE_RESPONSE", request=request)

    async def invalid_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="PRIVATE_CACHE_RESPONSE")

    async def exercise(handler):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app.api_clear_ssd_cache(_request(client))
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_cache_clear_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_CACHE_RESPONSE" not in str(error.value.detail)

    asyncio.run(exercise(transport_failure))
    asyncio.run(exercise(invalid_json))


def test_online_cache_clear_route_preserves_success_json(monkeypatch):
    _legacy(monkeypatch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "total_deleted": 12})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            result = await app.api_clear_ssd_cache(_request(client))
        assert result.status_code == 200
        assert result.body == b'{"status":"ok","total_deleted":12}'

    asyncio.run(run())


def test_cache_clear_route_cancellation_propagates(monkeypatch):
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"status": "ok"})

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            task = asyncio.create_task(app.api_clear_ssd_cache(_request(client)))
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_cache_clear_route_keeps_explicit_demo_marker_without_http(monkeypatch):
    attempted: list[str] = []

    class Trap:
        async def post(self, *args, **kwargs):
            attempted.append("post")
            raise AssertionError("offline demo must not call backend")

    async def offline(_client):
        return False

    monkeypatch.setattr(app, "_local_runtime_dispatch", rd.RuntimeDispatchBoundary)
    monkeypatch.setattr(app, "_omlx_is_online", offline)

    async def run():
        result = await app.api_clear_ssd_cache(_request(Trap()))
        assert result.status_code == 200
        assert result.body == b'{"status":"ok","total_deleted":46,"demo":true}'

    asyncio.run(run())
    assert attempted == []
