from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

import app
import runtime_dispatch_boundary as rd


def _use_legacy(monkeypatch):
    monkeypatch.setattr(app, "OMLX_BASE", "http://127.0.0.1:8000")
    monkeypatch.setattr(app, "_local_runtime_dispatch", rd.RuntimeDispatchBoundary)

    async def online(_client):
        return True

    monkeypatch.setattr(app, "_omlx_is_online", online)


@pytest.mark.parametrize("status", [302, 400, 500, 503])
def test_online_legacy_cleanup_rejects_unconfirmed_http_status_without_leaking_body(
    monkeypatch, status
):
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            json={"detail": "PRIVATE_BACKEND_RESPONSE"},
            headers={"Location": "http://evil.invalid"},
        )

    _use_legacy(monkeypatch)
    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app._unload_omlx_model(client, "fixture-model")
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_cleanup_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_BACKEND_RESPONSE" not in str(error.value.detail)
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert requests[0].url.path == "/v1/models/fixture-model/unload"
        assert sleeps == []

    asyncio.run(run())


def test_online_legacy_cleanup_converts_transport_failure_to_private_unconfirmed_error(
    monkeypatch,
):
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("PRIVATE_BACKEND_RESPONSE", request=request)

    _use_legacy(monkeypatch)
    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            with pytest.raises(HTTPException) as error:
                await app._unload_omlx_model(client, "fixture-model")
        assert error.value.status_code == 503
        assert error.value.detail == {
            "code": "legacy_cleanup_unconfirmed",
            "retryable": False,
        }
        assert "PRIVATE_BACKEND_RESPONSE" not in str(error.value.detail)
        assert len(requests) == 1
        assert sleeps == []

    asyncio.run(run())


def test_legacy_cleanup_cancellation_is_not_converted_to_success_or_http_error(
    monkeypatch,
):
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(204)

    _use_legacy(monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            task = asyncio.create_task(app._unload_omlx_model(client, "fixture-model"))
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_online_legacy_cleanup_returns_only_after_successful_response(monkeypatch):
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def no_sleep(seconds):
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    _use_legacy(monkeypatch)
    monkeypatch.setattr(app.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            result = await app._unload_omlx_model(client, "fixture-model")
        assert result is None
        assert len(requests) == 1
        assert sleeps == [1.0]

    asyncio.run(run())


def test_coordinated_policy_still_fails_closed_before_legacy_http(monkeypatch):
    attempted: list[str] = []

    class Trap:
        async def post(self, *args, **kwargs):
            attempted.append("post")
            raise AssertionError("coordinated cleanup must not use legacy HTTP")

    monkeypatch.setattr(
        app, "_local_runtime_dispatch", lambda: rd.RuntimeDispatchBoundary(True)
    )

    async def run():
        with pytest.raises(HTTPException) as error:
            await app._unload_omlx_model(Trap(), "fixture-model")
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "coordinated_backend_unavailable"

    asyncio.run(run())
    assert attempted == []
