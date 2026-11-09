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


def _inventory():
    return {
        "models": [
            {"id": "loaded-a", "loaded": True},
            {"id": "virtual-b", "loaded": True, "virtual": True},
            {"id": "idle-c", "loaded": False},
        ]
    }


@pytest.mark.parametrize("status", [302, 400, 500, 503])
def test_unload_all_route_refuses_unconfirmed_individual_cleanup_privately(
    monkeypatch, status
):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_inventory())
        return httpx.Response(
            status,
            json={"detail": "PRIVATE_UNLOAD_ALL_RESPONSE"},
            headers={"Location": "http://evil.invalid"},
        )

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app.api_unload_all(_request(client))
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_unload_all_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_UNLOAD_ALL_RESPONSE" not in str(error.value.detail)
        assert len(requests) == 2
        assert requests[0].method == "GET"
        assert requests[1].method == "POST"
        assert requests[1].url.path == "/v1/models/loaded-a/unload"

    asyncio.run(run())


@pytest.mark.parametrize("status", [302, 500])
def test_unload_all_route_refuses_unconfirmed_inventory_privately(monkeypatch, status):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json=_inventory(),
            headers={"Location": "http://evil.invalid"},
        )

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app.api_unload_all(_request(client))
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_unload_all_unconfirmed",
            "retryable": False,
        }
        assert len(requests) == 1
        assert requests[0].method == "GET"

    asyncio.run(run())


def test_unload_all_route_normalizes_transport_and_malformed_inventory(monkeypatch):
    _legacy(monkeypatch)

    async def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("PRIVATE_UNLOAD_ALL_RESPONSE", request=request)

    async def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="PRIVATE_UNLOAD_ALL_RESPONSE")

    async def exercise(handler):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app.api_unload_all(_request(client))
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_unload_all_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_UNLOAD_ALL_RESPONSE" not in str(error.value.detail)

    asyncio.run(exercise(transport_failure))
    asyncio.run(exercise(malformed))


def test_unload_all_route_preserves_success_list_and_skips_virtual_or_idle(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_inventory())
        return httpx.Response(204)

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            result = await app.api_unload_all(_request(client))
        assert result.status_code == 200
        assert result.body == b'{"status":"ok","unloaded":["loaded-a"]}'
        assert [request.method for request in requests] == ["GET", "POST"]

    asyncio.run(run())


def test_unload_all_route_cancellation_propagates(monkeypatch):
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_inventory())
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(204)

    _legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            task = asyncio.create_task(app.api_unload_all(_request(client)))
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
