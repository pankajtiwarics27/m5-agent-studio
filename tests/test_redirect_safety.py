from __future__ import annotations

import asyncio

import httpx
import pytest

import app as studio


def test_redirects_are_rejected_without_exposing_location() -> None:
    response = httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/admin"})
    with pytest.raises(RuntimeError, match="Redirect.*Location omitted") as exc:
        studio._reject_redirect_response(response, "https://provider.example.test/v1")
    assert "127.0.0.1" not in str(exc.value)


def test_openai_compatible_provider_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/steal"})

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
            provider = {"alias": "fixture", "base_url": "https://provider.example.test/v1", "effective_key": ""}
            with pytest.raises(RuntimeError, match="Redirect from"):
                async for _ in studio._stream_cloud_openai(client, provider, "model", [{"role": "user", "content": "hi"}], 0, 16):
                    pass
        assert len(seen) == 1
        assert "provider.example.test" in str(seen[0].url)

    asyncio.run(run())


def test_network_backend_connects_only_to_validated_ip() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.connected_host = ""

        async def connect_tcp(self, host: str, port: int, **kwargs: object) -> object:
            self.connected_host = host
            return object()

    async def run() -> None:
        backend = studio._PinnedNetworkBackend()
        fake = FakeBackend()
        backend._backend = fake
        pins = studio._OUTBOUND_DNS_PINS.set({"provider.example.test": (("203.0.113.15",), False)})
        try:
            await backend.connect_tcp("provider.example.test", 443)
            assert fake.connected_host == "203.0.113.15"
            studio._OUTBOUND_DNS_PINS.set({})
            with pytest.raises(OSError, match="no request-scoped DNS validation pin"):
                await backend.connect_tcp("provider.example.test", 443)
        finally:
            studio._OUTBOUND_DNS_PINS.reset(pins)

    asyncio.run(run())
