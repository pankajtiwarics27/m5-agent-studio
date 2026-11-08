"""LC-08 offline routing fence; fake backends are not native qualification."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app as studio
import runtime_dispatch_boundary as rd


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.fail = False
        self.status = {"status": "confirmed"}

    async def stream(self, call):
        self.calls.append(call)
        try:
            if self.fail:
                raise RuntimeError("SECRET /private/native/detail")
            yield {"type": "content", "text": "READY"}
            yield {"type": "usage", "completion_tokens": 1, "demo": False}
        finally:
            self.closed = True

    async def command(self, call):
        self.calls.append(call)
        if self.fail:
            raise RuntimeError("SECRET /private/native/detail")
        return self.status


async def collect(gen):
    return [v async for v in gen]


@pytest.fixture
def installed(monkeypatch):
    backends = {k: FakeBackend() for k in ("omlx", "ollama", "lmstudio")}
    boundary = rd.RuntimeDispatchBoundary(
        True,
        backends,
        {"studio-local": "lmstudio"},
        (studio.OMLX_BASE, studio.OLLAMA_BASE),
    )
    monkeypatch.setattr(studio, "_LOCAL_RUNTIME_DISPATCH", boundary)

    def forbidden(*a, **kw):
        raise AssertionError("legacy/demo/probe/sanitize/trim/HTTP must not execute")

    monkeypatch.setattr(studio, "_omlx_is_online", forbidden)
    monkeypatch.setattr(studio, "_trim_messages_for_omlx_prefill", forbidden)
    monkeypatch.setattr(studio.routing_policy, "sanitize_messages_for_wire", forbidden)
    monkeypatch.setattr(studio, "_heavy_model_slot", forbidden)
    return boundary, backends


@pytest.mark.parametrize("runtime", ["omlx", "ollama", "lmstudio"])
def test_coordinated_stream_receives_all_input_without_legacy_transformations(
    installed, monkeypatch, runtime
):
    boundary, backends = installed
    meta = (
        None
        if runtime == "omlx"
        else {
            "alias": "ollama" if runtime == "ollama" else "studio-local",
            "base_url": "http://localhost:1234/v1",
        }
    )
    monkeypatch.setattr(
        studio,
        "parse_model_route",
        lambda model: ("omlx" if runtime == "omlx" else "cloud", meta, "exact-model"),
    )
    messages = [
        dict(
            role="system",
            content="SECURITY skills memory objective handoff " + "A" * 26000,
        ),
        dict(role="user", content="HISTORY ಕರ್ನಾಟಕ 😀"),
        dict(role="assistant", content="<tool_call>read</tool_call>"),
        dict(role="user", content="<tool_response>COMPLETE RESULT</tool_response>"),
        dict(role="user", content="LATEST ORIGINAL"),
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    token = studio._CHAT_NATIVE_TOOLS.set(tools)
    consumer_token = studio._CANONICAL_NATIVE_CONSUMER.set(True)
    try:
        result = asyncio.run(
            collect(
                studio.stream_any_model(object(), "route", messages, 0.3, 7326, "xhigh")
            )
        )
    finally:
        studio._CHAT_NATIVE_TOOLS.reset(token)
        studio._CANONICAL_NATIVE_CONSUMER.reset(consumer_token)
    assert result[0]["text"] == "READY"
    call = backends[runtime].calls[0]
    body = json.loads(call.payload)
    assert body == dict(
        messages=messages,
        tools=tools,
        temperature=0.3,
        max_tokens=7326,
        reasoning_mode="xhigh",
    )
    messages[0]["content"] = "MUTATED LATER"
    assert "SECURITY" in json.loads(call.payload)["messages"][0]["content"]
    assert (
        call.runtime == runtime and call.action == "infer" and backends[runtime].closed
    )


@pytest.mark.parametrize("runtime", ["omlx", "ollama", "lmstudio"])
def test_missing_qualified_backend_is_not_legacy_fallback_or_demo(monkeypatch, runtime):
    boundary = rd.RuntimeDispatchBoundary(
        True, provider_runtimes={"native-lm": "lmstudio"}
    )
    monkeypatch.setattr(studio, "_LOCAL_RUNTIME_DISPATCH", boundary)
    provider = {
        "alias": "ollama" if runtime == "ollama" else "native-lm",
        "base_url": "http://localhost/v1",
    }
    monkeypatch.setattr(
        studio,
        "parse_model_route",
        lambda _: (
            ("omlx", None, "model")
            if runtime == "omlx"
            else ("cloud", provider, "model")
        ),
    )
    with pytest.raises(
        rd.RuntimeDispatchDenied, match="coordinated_backend_unavailable"
    ):
        asyncio.run(
            collect(
                studio.stream_any_model(
                    object(), "model", [{"role": "user", "content": "READY"}]
                )
            )
        )


@pytest.mark.parametrize(
    "func,action",
    [
        ("api_load_model", "load"),
        ("api_unload_model", "unload"),
        ("api_unload_all", "unload_all"),
        ("api_clear_ssd_cache", "clear_cache"),
        ("_unload_omlx_model", "unload"),
    ],
)
def test_control_and_workflow_cleanup_enter_boundary_before_online_probe(
    installed, func, action
):
    _, backends = installed
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=object())))
    fn = getattr(studio, func)
    args = (
        (object(), "exact-model")
        if func.startswith("_")
        else (("exact-model", req) if action in ("load", "unload") else (req,))
    )
    result = asyncio.run(fn(*args))
    assert backends["omlx"].calls[0].action == action
    if result is not None:
        assert json.loads(result.body) == {"status": "confirmed"}


@pytest.mark.parametrize(
    "func",
    ["api_load_model", "api_unload_model", "api_unload_all", "api_clear_ssd_cache"],
)
def test_unqualified_controls_return_fixed_503_not_fake_success(monkeypatch, func):
    monkeypatch.setattr(
        studio, "_LOCAL_RUNTIME_DISPATCH", rd.RuntimeDispatchBoundary(True)
    )
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http=object())))
    args = ("model", req) if func in ("api_load_model", "api_unload_model") else (req,)
    with pytest.raises(HTTPException) as error:
        asyncio.run(getattr(studio, func)(*args))
    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "coordinated_backend_unavailable",
        "retryable": False,
    }


def test_coordinated_combo_refusal_has_no_eviction_output_or_fallback(
    installed, monkeypatch
):
    monkeypatch.setattr(
        studio,
        "parse_model_route",
        lambda _: ("combo", {"targets": ["one", "cloud::two"]}, "test"),
    )
    with pytest.raises(rd.RuntimeDispatchDenied, match="coordinated_combo_unsupported"):
        asyncio.run(collect(studio.stream_any_model(object(), "combo/test", [])))
    assert not any(b.calls for b in installed[1].values())


def test_unowned_auto_eviction_is_not_a_lifecycle_operation(installed):
    with pytest.raises(rd.RuntimeDispatchDenied, match="unowned_eviction_forbidden"):
        asyncio.run(studio._unload_other_omlx_models(object(), "keep"))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "http://127.0.0.1:9999/v1",
        "http://[::1]/v1",
        "http://10.0.0.1/v1",
    ],
)
def test_unknown_local_provider_is_not_assumed_cloud_or_native(url):
    boundary = rd.RuntimeDispatchBoundary(True)
    with pytest.raises(
        rd.RuntimeDispatchDenied, match="coordinated_backend_unavailable"
    ):
        boundary.target("cloud", {"alias": "unknown", "base_url": url})


def test_remote_provider_uses_existing_security_boundary_without_local_backend():
    boundary = rd.RuntimeDispatchBoundary(True)
    assert (
        boundary.target(
            "cloud", {"alias": "authorized-cloud", "base_url": "https://example.com/v1"}
        )
        is None
    )
    with pytest.raises(rd.RuntimeDispatchDenied):
        boundary.target(
            "cloud",
            {
                "alias": "private-proxy",
                "base_url": "https://example.com/v1",
                "allow_private": True,
            },
        )


@pytest.mark.parametrize(
    "helper",
    ["_stream_omlx_completion", "_stream_cloud_openai", "_stream_cloud_anthropic"],
)
def test_direct_legacy_inference_helpers_cannot_bypass_boundary(installed, helper):
    args = (
        (object(), "model", [])
        if helper == "_stream_omlx_completion"
        else (
            object(),
            {"alias": "ollama", "base_url": "http://localhost/v1"},
            "model",
            [],
            0.0,
            512,
        )
    )
    if helper == "_stream_cloud_anthropic":
        args += ("off",)
    with pytest.raises(rd.RuntimeDispatchDenied, match="legacy_transport_blocked"):
        asyncio.run(collect(getattr(studio, helper)(*args)))


@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/chat/completions", "POST"),
        ("/tokenize", "POST"),
        ("/v1/tokenize", "GET"),
        ("/v1/messages/count_tokens", "POST"),
        ("/v1/models/a/load", "POST"),
        ("/v1/models/a/unload", "POST"),
        ("/api/generate", "POST"),
        ("/api/chat", "POST"),
        ("/admin/api/ssd-cache/clear", "POST"),
    ],
)
def test_generic_http_backstop_forbids_runtime_effect_paths(path, method):
    boundary = rd.RuntimeDispatchBoundary(
        True, runtime_origins=("http://localhost:8000",)
    )
    with pytest.raises(rd.RuntimeDispatchDenied, match="legacy_transport_blocked"):
        boundary.guard_http(method, "http://localhost:8000" + path)


@pytest.mark.parametrize("path", ["/api/tags", "/api/ps", "/v1/models"])
def test_observation_routes_do_not_imply_readiness_but_remain_read_only(path):
    rd.RuntimeDispatchBoundary(
        True, runtime_origins=("http://localhost:8000",)
    ).guard_http("GET", "http://localhost:8000" + path)


def test_backend_failure_is_sanitized_and_does_not_retry(installed):
    boundary, backends = installed
    backends["omlx"].fail = True
    with pytest.raises(
        rd.RuntimeDispatchDenied, match="coordinated_execution_unavailable"
    ) as error:
        asyncio.run(
            collect(
                boundary.stream(
                    "omlx",
                    "model",
                    [],
                    tools=None,
                    temperature=0.0,
                    max_tokens=512,
                    reasoning_mode="off",
                )
            )
        )
    assert (
        "SECRET" not in str(error.value.to_mapping())
        and len(backends["omlx"].calls) == 1
    )
    assert backends["omlx"].closed


@pytest.mark.parametrize(
    "status",
    [
        {"status": "ok"},
        {"status": "confirmed", "detail": "SECRET"},
        {"status": "acknowledged"},
    ],
)
def test_raw_ack_or_native_body_is_not_a_canonical_control_reply(installed, status):
    boundary, backends = installed
    backends["omlx"].status = status
    with pytest.raises(rd.RuntimeDispatchDenied):
        asyncio.run(boundary.command("omlx", "model", "unload"))


def test_consumer_close_reaches_backend_without_finishing_or_releasing_invented_authority(
    installed,
):
    boundary, backends = installed

    async def run():
        gen = boundary.stream(
            "omlx",
            "model",
            [],
            tools=None,
            temperature=0.0,
            max_tokens=512,
            reasoning_mode="off",
        )
        assert (await anext(gen))["text"] == "READY"
        await gen.aclose()

    asyncio.run(run())
    assert backends["omlx"].closed


def test_legacy_rollout_default_is_unchanged():
    boundary = rd.RuntimeDispatchBoundary()
    assert boundary.target("omlx", None) is None
    boundary.guard_http("POST", "http://localhost:8000/v1/chat/completions")
    boundary.forbid_legacy()


def test_standalone_fence_has_no_sibling_dependency(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "studio.py", tmp_path / "studio.py")
    code = """import asyncio,json,studio
b=studio.runtime_dispatch_boundary.RuntimeDispatchBoundary(True)
studio._LOCAL_RUNTIME_DISPATCH=b
async def run():
 try:
  await studio._unload_omlx_model(None,'model')
 except studio.HTTPException as e:
  assert e.status_code==503
  print(e.detail['code'])
 else: raise AssertionError('standalone bypass')
asyncio.run(run())
assert studio.runtime_dispatch_boundary is studio
"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "coordinated_backend_unavailable" in result.stdout


@pytest.mark.parametrize("status", ["pending", "uncertain", "refused"])
def test_workflow_cleanup_never_advances_on_ack_pending_or_unknown(installed, status):
    installed[1]["omlx"].status = {"status": status}
    with pytest.raises(HTTPException, match="503"):
        asyncio.run(studio._unload_omlx_model(object(), "model"))


def test_coordinated_preflight_does_not_reuse_process_local_legacy_estimates(installed):
    studio._heavy_model_preflight("model")


def test_pinned_generic_http_fails_before_dns_or_native_transport(
    installed, monkeypatch
):
    import httpx

    calls = []

    async def forbidden(*a, **kw):
        calls.append("native-or-dns")
        raise AssertionError("must not be reached")

    transport = object.__new__(studio._PinnedAsyncHTTPTransport)
    transport._inner = SimpleNamespace(handle_async_request=forbidden)
    monkeypatch.setattr(studio, "_validate_outbound_url", forbidden)
    with pytest.raises(rd.RuntimeDispatchDenied):
        asyncio.run(
            transport.handle_async_request(
                httpx.Request("POST", studio.OMLX_BASE + "/v1/chat/completions")
            )
        )
    assert calls == []


def test_static_entry_point_inventory_is_current_and_new_wire_sites_fail_closed():
    from scripts.runtime_dispatch_inventory import ROOT, OUT, render, inventory

    assert OUT.read_text() == render((ROOT / "app.py").read_text())
    with pytest.raises(ValueError, match="unmapped model wire owner"):
        inventory(
            'async def bypass(client):\n await client.post(f"{OMLX_BASE}/v1/models/a/load")\n'
        )


def test_resolved_private_hostname_cannot_bypass_http_fence_with_private_network_opt_in(
    installed, monkeypatch
):
    import httpx

    calls = []

    async def validate(*a, **kw):
        studio._OUTBOUND_DNS_PINS.set({"native.example": (("127.0.0.1",), True)})

    async def native(*a, **kw):
        calls.append("native")
        raise AssertionError("must not be reached")

    transport = object.__new__(studio._PinnedAsyncHTTPTransport)
    transport._inner = SimpleNamespace(handle_async_request=native)
    monkeypatch.setattr(studio, "_validate_outbound_url", validate)
    with pytest.raises(rd.RuntimeDispatchDenied):
        asyncio.run(
            transport.handle_async_request(
                httpx.Request("POST", "https://native.example/v1/chat/completions")
            )
        )
    assert calls == []


@pytest.mark.parametrize(
    "path", ["/v1%2Ftokenize", "/debug/private-renderer", "/unknown"]
)
def test_known_runtime_unknown_get_or_encoded_counter_is_not_assumed_observation(path):
    boundary = rd.RuntimeDispatchBoundary(
        True, runtime_origins=("http://localhost:8000",)
    )
    with pytest.raises(rd.RuntimeDispatchDenied):
        boundary.guard_http("GET", "http://localhost:8000" + path)


def test_internal_configuration_cannot_accidentally_install_boolean_authority():
    with pytest.raises(rd.RuntimeDispatchDenied):
        rd.RuntimeDispatchBoundary(True, {"omlx": True})


def test_composition_owns_alias_and_origin_containers():
    origins = ["http://localhost:8000"]
    aliases = {"lm": "lmstudio"}
    boundary = rd.RuntimeDispatchBoundary(
        True, provider_runtimes=aliases, runtime_origins=origins
    )
    origins.clear()
    aliases.clear()
    assert boundary.runtime_origins == ("http://localhost:8000",)
    assert boundary.provider_runtimes == {"lm": "lmstudio"}


@pytest.mark.parametrize("case", ["missing-token", "foreign-origin", "authorized"])
def test_composed_control_still_passes_existing_http_auth_origin_boundary(
    installed, tmp_path, monkeypatch, case
):
    from fastapi.testclient import TestClient

    token = "fixture-token-not-a-secret-0123456789"
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", token)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    headers = {} if case == "missing-token" else {"Authorization": "Bearer " + token}
    if case == "foreign-origin":
        headers["Origin"] = "https://untrusted.example"
    with TestClient(studio.app) as client:
        response = client.post("/api/models/model/load", headers=headers)
    assert (
        response.status_code
        == {"missing-token": 401, "foreign-origin": 403, "authorized": 200}[case]
    )
    assert len(installed[1]["omlx"].calls) == int(case == "authorized")
