from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app as studio


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://localhost:11434/", "http://localhost:11434"),
        ("https://ollama.tailnet.example.ts.net:8443", "https://ollama.tailnet.example.ts.net:8443"),
        ("http://192.168.1.50:11434", "http://192.168.1.50:11434"),
    ],
)
def test_ollama_url_accepts_loopback_and_configured_remote(value: str, expected: str) -> None:
    assert studio._config_ollama_url({"OLLAMA_BASE_URL": value}) == expected


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.test/models",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434/api?x=1",
        "http://127.0.0.1:11434/#frag",
        "http://127.0.0.1:notaport",
        "not-a-url",
    ],
)
def test_ollama_url_rejects_unsafe_or_invalid_values(value: str) -> None:
    with pytest.raises(studio.ConfigurationError):
        studio._config_ollama_url({"OLLAMA_BASE_URL": value})


def test_load_settings_defaults_ollama_to_loopback(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    base.mkdir()
    cwd.mkdir()
    home.mkdir()
    settings = studio.load_settings({}, base_dir=base, cwd=cwd, home=home)
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    settings = studio.load_settings({"OLLAMA_BASE_URL": "https://ollama.example.internal:8443/"}, base_dir=base, cwd=cwd, home=home)
    assert settings.ollama_base_url == "https://ollama.example.internal:8443"


# ------------------------------------------------------------------ probe


def _ollama_tags_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "models": [
                {"name": "llama3.2:3b", "size": 2_000_000_000, "modified_at": "2026-11-01T00:00:00Z", "details": {"quantization_level": "Q4_K_M"}},
                {"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "details": {"quantization_level": "Q4_K_M"}},
                {"name": "", "size": 1},
                "not-a-record",
            ]
        },
    )


def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "_ollama_probe_cache", {"expires": 0.0, "snapshot": None})


def test_ollama_snapshot_reports_models_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://127.0.0.1:11434")

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return _ollama_tags_response()

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return await studio._probe_ollama(client)

    snapshot = asyncio.run(run())
    assert snapshot["online"] is True
    assert [m["id"] for m in snapshot["models"]] == ["llama3.2:3b", "qwen2.5-coder:7b"]
    assert snapshot["models"][0]["size_gb"] == pytest.approx(1.86, abs=0.01)
    assert snapshot["models"][0]["quantization"] == "Q4_K_M"
    assert snapshot["error"] is None


def test_ollama_snapshot_reports_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://127.0.0.1:11434")

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return await studio._probe_ollama(client)

    snapshot = asyncio.run(run())
    assert snapshot["online"] is False
    assert snapshot["error"] == "HTTP 500"
    assert snapshot["models"] == []


def test_ollama_snapshot_reports_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://127.0.0.1:11434")

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return await studio._probe_ollama(client)

    snapshot = asyncio.run(run())
    assert snapshot["online"] is False
    assert snapshot["error"] == "unreachable"


def test_ollama_probe_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://127.0.0.1:11434")
    calls = {"count": 0}

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _ollama_tags_response()

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            await studio._probe_ollama(client)
            await studio._probe_ollama(client)
            # Expire the cached snapshot; the next probe must hit the server again.
            studio._ollama_probe_cache["expires"] = 0.0
            await studio._probe_ollama(client)

    asyncio.run(run())
    assert calls["count"] == 2


# ------------------------------------------------------------------ routing


def test_parse_model_route_supports_ollama_prefixes(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    studio.init_db()
    for spec in ("ollama::llama3.2:3b", "ollama/llama3.2:3b"):
        kind, provider, remote = studio.parse_model_route(spec)
        assert kind == "cloud"
        assert provider is not None
        assert provider["alias"] == "ollama"
        assert provider["builtin_private_ok"] is True
        assert provider["base_url"] == f"{studio.OLLAMA_BASE}/v1"
        assert remote == "llama3.2:3b"


def test_is_local_omlx_model_excludes_ollama(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    studio.init_db()
    assert studio.is_local_omlx_model("ollama::llama3.2:3b") is False
    assert studio.is_local_omlx_model("ollama/llama3.2:3b") is False
    assert studio.is_local_omlx_model("omlx::fixture-model") is True
    assert studio.is_local_omlx_model("plain-local-model") is True


# ------------------------------------------------------------------ catalog


def test_unified_catalog_lists_all_local_models_not_only_loaded(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    studio.init_db()
    models = [
        {"id": "loaded-model", "loaded": True, "is_loading": False, "size_gb": 10.0},
        {"id": "resident-soon", "loaded": False, "is_loading": True, "size_gb": 8.0},
        {"id": "on-disk-model", "loaded": False, "is_loading": False, "size_gb": 19.0},
    ]
    catalog = studio._build_unified_catalog(models, loaded_only=False)
    ids = [item["id"] for item in catalog if item.get("source") == "local"]
    assert ids == ["loaded-model", "resident-soon", "on-disk-model"]
    # The loaded-only filter still exists for call sites that want it.
    catalog_loaded = studio._build_unified_catalog(models, loaded_only=True)
    ids_loaded = [item["id"] for item in catalog_loaded if item.get("source") == "local"]
    assert ids_loaded == ["loaded-model", "resident-soon"]
    resident = next(item for item in catalog if item["id"] == "loaded-model")
    on_demand = next(item for item in catalog if item["id"] == "on-disk-model")
    assert "Loaded" in resident["label"] and "On-Demand" in on_demand["label"]


def test_catalog_with_ollama_merges_entries_and_respects_offline() -> None:
    base = [{"id": "omlx::fixture", "label": "x", "group": "Local M5 (Loaded in RAM)", "source": "local", "provider": "omlx", "size_gb": 1.0}]
    online_snapshot = {
        "online": True,
        "base_url": "http://127.0.0.1:11434",
        "models": [{"id": "llama3.2:3b", "size_gb": 1.9, "quantization": "Q4_K_M", "modified_at": None}],
        "error": None,
    }
    merged = studio._catalog_with_ollama(base, online_snapshot)
    ollama_entries = [item for item in merged if item.get("source") == "ollama"]
    assert [item["id"] for item in ollama_entries] == ["ollama::llama3.2:3b"]
    assert ollama_entries[0]["group"] == "Ollama (Local)"
    assert ollama_entries[0]["provider"] == "ollama"
    offline = studio._catalog_with_ollama(base, {"online": False, "base_url": "x", "models": [], "error": "unreachable"})
    assert offline == base


def test_ollama_dispatch_sends_no_authorization_and_uses_virtual_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://192.0.2.10:11434")

    async def allow_validate(url: str, allow_private: bool = False) -> None:
        assert allow_private is True
        return None

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_validate)

    def _sse(payloads: list[dict[str, object]]) -> bytes:
        return b"".join(("data: " + json.dumps(item) + "\n\n").encode() for item in payloads) + b"data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 1}},
        ]))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            provider = studio.ollama_virtual_provider()
            async for _ in studio._stream_cloud_openai(client, provider, "llama3.2:3b", [{"role": "user", "content": "hi"}], 0, 16):
                pass

    asyncio.run(run())
    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == "192.0.2.10"
    assert request.url.path == "/v1/chat/completions"
    assert "authorization" not in {key.lower() for key in request.headers.keys()}


# ------------------------------------------------------------------ telemetry endpoint (fake runtime + Ollama)


def test_telemetry_demo_payload_carries_ollama_and_all_local_models(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    studio.init_db()
    monkeypatch.setattr(studio, "OLLAMA_BASE", "http://127.0.0.1:11434")
    _fresh_cache(monkeypatch)

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    async def fail_omlx(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("omlx down")

    def mixed_validate(url: str, allow_private: bool = False) -> Any:
        if "11434" in url:
            return allow_url(url, allow_private=allow_private)
        return fail_omlx(url, allow_private=allow_private)

    monkeypatch.setattr(studio, "_validate_outbound_url", mixed_validate)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return _ollama_tags_response()
        return httpx.Response(404)

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=client)))
            response = await studio.api_telemetry(request)  # type: ignore[arg-type]
            return json.loads(response.body)

    payload = asyncio.run(run())
    assert payload["telemetry_source"] == "simulated_demo"
    assert payload["ollama"]["online"] is True
    catalog_ids = [item["id"] for item in payload["catalog"]]
    assert "ollama::llama3.2:3b" in catalog_ids
    assert "ollama::qwen2.5-coder:7b" in catalog_ids
    # Demo models are selectable even though none are resident.
    assert any(item.get("source") == "local" for item in payload["catalog"])
