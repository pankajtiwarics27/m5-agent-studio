from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app as studio


def _sse(payloads: list[dict[str, object]]) -> bytes:
    return b"".join(("data: " + json.dumps(item) + "\n\n").encode() for item in payloads) + b"data: [DONE]\n\n"


def test_openai_compatible_local_provider_stream_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 1}},
        ]))

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            provider = {"alias": "local-fixture", "base_url": "http://127.0.0.1:8000/v1", "effective_key": ""}
            return [item async for item in studio._stream_cloud_openai(client, provider, "fixture-model", [{"role": "user", "content": "hi"}], 0, 32)]

    chunks = asyncio.run(run())
    assert any(c.get("type") == "content" and c.get("text") == "hello" for c in chunks)
    assert chunks[-1]["type"] == "usage"
    assert len(seen) == 1 and seen[0].url.path.endswith("/chat/completions")


def test_omlx_local_completion_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setattr(studio, "OMLX_BASE", "http://127.0.0.1:8000")

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
            {"choices": [{"delta": {"content": "local reply"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}},
        ]))

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return [item async for item in studio._stream_omlx_completion(client, "fixture-model", [{"role": "user", "content": "hi"}], max_tokens=20)]

    chunks = asyncio.run(run())
    assert any(c.get("type") == "content" and c.get("text") == "local reply" for c in chunks)
    assert len(seen) == 2


def test_mcp_http_rpc_modern_contract_and_redirect_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    seen: list[httpx.Request] = []
    srv = {"alias": "fixture-mcp", "url": "https://mcp.example.test/mcp", "headers": {}, "effective_token": "", "auth_header": "Authorization"}

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read"}]}})

    async def run_ok() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler), follow_redirects=False) as client:
            return await studio._mcp_http_rpc(client, srv, "tools/list")

    result = asyncio.run(run_ok())
    assert result == {"tools": [{"name": "read"}]}
    assert seen[0].headers["MCP-Protocol-Version"] == "2026-07-28"

    redirect_calls: list[httpx.Request] = []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirect_calls.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9000/secret"})

    async def run_redirect() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler), follow_redirects=False) as client:
            await studio._mcp_http_rpc(client, srv, "tools/list")

    with pytest.raises(RuntimeError, match="Redirect from"):
        asyncio.run(run_redirect())
    assert len(redirect_calls) == 1


async def _allow_url(*_args: object, **_kwargs: object) -> None:
    return None


def test_omlx_prefill_memory_aborted_auto_compacts_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_attempts: list[dict[str, object]] = []
    monkeypatch.setattr(studio, "OMLX_BASE", "http://127.0.0.1:8000")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        if request.url.path == "/admin/api/active-models":
            return httpx.Response(200, json={"active_models": {"models": []}})
        if request.url.path == "/admin/api/cache/clear-ssd":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content.decode("utf-8"))
            chat_attempts.append(body)
            if len(chat_attempts) == 1:
                err_pkt = {
                    "error": {
                        "message": "oMLX memory guard aborted this request mid-prefill: process memory limit exceeded",
                        "code": "prefill_memory_aborted",
                    }
                }
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=f"data: {json.dumps(err_pkt)}\n\ndata: [DONE]\n\n".encode(),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse([
                    {"choices": [{"delta": {"content": "recovered after prefill compaction"}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 4}},
                ]),
            )
        return httpx.Response(200, json={})

    long_messages = [
        {"role": "system", "content": "System instructions " * 150},
        {"role": "user", "content": "Old context " * 800},
        {"role": "assistant", "content": "Old tool output " * 800},
        {"role": "user", "content": "Final question"},
    ]

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return [
                item
                async for item in studio._stream_omlx_completion(
                    client, "fixture-model", long_messages, max_tokens=4096, reasoning_mode="low"
                )
            ]

    chunks = asyncio.run(run())
    assert len(chat_attempts) == 2
    assert any(c.get("type") == "content" and "recovered after prefill compaction" in str(c.get("text")) for c in chunks)


def test_loaded_only_catalog_and_direct_user_project_directory(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    base = Path(str(tmp_path))
    monkeypatch.setattr(studio, "DB_PATH", base / "studio.db")
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", base / ".harnesses")
    studio.init_db()

    models = [
        {"id": "loaded-local-model", "loaded": True, "is_loading": False, "size_gb": 12.5, "mtp_enabled": False},
        {"id": "unloaded-local-model", "loaded": False, "is_loading": False, "size_gb": 19.0, "mtp_enabled": False},
    ]
    cat = studio._build_unified_catalog(models, loaded_only=True)
    local_ids = [item["id"] for item in cat if item.get("source") == "local"]
    assert "loaded-local-model" in local_ids
    assert "unloaded-local-model" not in local_ids

    target_project_dir = base / "user-projects" / "new-storefront"
    assert not target_project_dir.exists()
    sess = studio.create_harness_session_db(
        title="Direct User Dir Build",
        workspace_dir=str(target_project_dir),
        use_worktree=False,
        policy="standard",
        lead_model="loaded-local-model",
        critic_model="loaded-local-model",
    )
    assert target_project_dir.is_dir()
    assert sess["workspace_dir"] == str(target_project_dir.resolve())
    assert sess["worktree_dir"] == str(target_project_dir.resolve())
    assert ".harnesses" not in sess["worktree_dir"]
    assert sess["use_worktree"] is False

