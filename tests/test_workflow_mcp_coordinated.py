"""Bounded stdio summon composition; synthetic backends, not native qualification."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app
import runtime_dispatch_boundary as rd


class Backend:
    def __init__(self, kind="content", status="confirmed"):
        self.kind, self.status = kind, status
        self.calls, self.closed, self.commands, self.order = [], [], [], []

    async def stream(self, call):
        self.calls.append(call)
        try:
            if self.kind == "malformed":
                yield {"type": "content"}  # consumer raises, not the source
            else:
                yield dict(
                    type=self.kind,
                    text="ANSWER ಕರ್ನಾಟಕ 😀",
                    id="native-id",
                    name="read",
                    arguments="{}",
                )
                yield dict(type="usage", completion_tokens=1, finish_reason="stop")
        finally:
            self.closed.append(call)
            self.order.append("source_closed")

    async def command(self, call):
        self.commands.append(call)
        self.order.append("cleanup")
        return {"status": self.status}


def install(monkeypatch, backend, *, coordinated=True):
    monkeypatch.setattr(
        app,
        "_LOCAL_RUNTIME_DISPATCH",
        rd.RuntimeDispatchBoundary(
            coordinated, {"omlx": backend} if coordinated else None
        ),
    )
    monkeypatch.setattr(app, "list_cloud_providers", lambda **kwargs: [])
    session = dict(
        request_text="SECURITY MATTER " + "keep ಕರ್ನಾಟಕ 😀 " * 3000 + "LATEST",
        messages=[
            dict(author="Human", content="PRIOR-ANCHOR " * 1000),
            dict(author="critic", content="COMPLETE FEEDBACK"),
        ],
    )
    saved, clients = [], []
    monkeypatch.setattr(app, "get_hall_session", lambda sid: session)

    def save(*args, **kwargs):
        backend.order.append("save")
        saved.append((args, kwargs))

    monkeypatch.setattr(app, "append_hall_message", save)

    class Client:
        def __init__(self, **kwargs):
            clients.append(self)
            self.timeout = kwargs["timeout"]
            self.closed = False

        async def __aenter__(self):
            backend.order.append("client_open")
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            backend.order.append("client_closed")

    monkeypatch.setattr(app.httpx, "AsyncClient", Client)
    return SimpleNamespace(session=session, saved=saved, clients=clients)


def test_mcp_consumer_error_closes_owned_source_before_client_exit(monkeypatch):
    async def run():
        backend = Backend("malformed")
        b = install(monkeypatch, backend)
        with pytest.raises(KeyError, match="text"):
            await app._mcp_summon_model("session", "model-a", "architect")
        assert len(backend.closed) == 1  # before event-loop finalization
        assert backend.order == ["client_open", "source_closed", "client_closed"]
        assert b.saved == backend.commands == [] and b.clients[0].closed

    asyncio.run(run())


def test_mcp_preserves_full_matter_chronicle_role_and_original_options(monkeypatch):
    async def run():
        backend = Backend()
        b = install(monkeypatch, backend)
        result = await app._mcp_summon_model("session", "model-a", "architect")
        transcript = "\n\n".join(
            f"--- [{m['author']}] ---\n{m['content']}" for m in b.session["messages"]
        )
        author = f"{app.AGENT_ROLES['architect']['label']} (model-a)"
        expected = [
            dict(role="system", content=app.AGENT_ROLES["architect"]["system"]),
            dict(
                role="user",
                content=f"### Council Matter:\n{b.session['request_text']}\n\n### Chronicle:\n{transcript}\n\nProvide your feedback as {author}.",
            ),
        ]
        assert len(backend.calls) == 1
        assert json.loads(backend.calls[0].payload) == dict(
            messages=expected,
            tools=None,
            temperature=0.0,
            max_tokens=3200,
            reasoning_mode="off",
        )
        assert result == f"### {author}\n\nANSWER ಕರ್ನಾಟಕ 😀"
        assert b.saved == [
            (
                ("session", author, "architect", "ANSWER ಕರ್ನಾಟಕ 😀"),
                dict(
                    model="model-a",
                    usage=dict(type="usage", completion_tokens=1, finish_reason="stop"),
                ),
            )
        ]
        assert backend.order == [
            "client_open",
            "source_closed",
            "cleanup",
            "client_closed",
            "save",
        ]
        assert b.clients[0].closed and b.clients[0].timeout.read == 600.0

    asyncio.run(run())


@pytest.mark.parametrize("status", ["pending", "refused", "uncertain", "ok"])
def test_mcp_unconfirmed_cleanup_never_appends_or_returns_success(monkeypatch, status):
    async def run():
        backend = Backend(status=status)
        b = install(monkeypatch, backend)
        with pytest.raises(app.HTTPException) as error:
            await app._mcp_summon_model("session", "model-a", "critic")
        assert error.value.status_code == 503
        assert len(backend.calls) == len(backend.closed) == len(backend.commands) == 1
        assert b.saved == [] and b.clients[0].closed
        assert backend.order == [
            "client_open",
            "source_closed",
            "cleanup",
            "client_closed",
        ]

    asyncio.run(run())


@pytest.mark.parametrize("advertised", [False, True])
def test_mcp_text_consumer_refuses_native_tools_without_cleanup_or_save(
    monkeypatch, advertised
):
    async def run():
        backend = Backend("tool_call")
        b = install(monkeypatch, backend)
        token = app._CHAT_NATIVE_TOOLS.set(
            [dict(type="function", function=dict(name="read"))] if advertised else None
        )
        try:
            with pytest.raises(
                rd.RuntimeDispatchDenied, match="canonical_consumer_required"
            ):
                await app._mcp_summon_model("session", "model-a", "critic")
            assert len(backend.calls) == len(backend.closed) == (0 if advertised else 1)
            assert b.saved == backend.commands == [] and b.clients[0].closed
        finally:
            app._CHAT_NATIVE_TOOLS.reset(token)

    asyncio.run(run())


def test_mcp_partial_backend_failure_never_retries_cleans_up_or_saves(monkeypatch):
    async def run():
        class Failed(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="content", text="prefix")
                    raise OSError("private-native-failure")
                finally:
                    self.closed.append(call)

        backend = Failed()
        b = install(monkeypatch, backend)
        with pytest.raises(
            rd.RuntimeDispatchDenied, match="coordinated_execution_unavailable"
        ):
            await app._mcp_summon_model("session", "model-a", "critic")
        assert len(backend.calls) == len(backend.closed) == 1
        assert b.saved == backend.commands == [] and b.clients[0].closed

    asyncio.run(run())


def test_mcp_task_cancellation_closes_source_and_client_without_save(monkeypatch):
    async def run():
        entered = asyncio.Event()

        class Blocked(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="content", text="prefix")
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    self.closed.append(call)
                    self.order.append("source_closed")

        backend = Blocked()
        b = install(monkeypatch, backend)
        task = asyncio.create_task(
            app._mcp_summon_model("session", "model-a", "critic")
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(backend.closed) == 1
        assert b.saved == backend.commands == [] and b.clients[0].closed
        assert backend.order == ["client_open", "source_closed", "client_closed"]

    asyncio.run(run())


def test_mcp_legacy_defaults_role_fallback_and_no_reasoning_retry(monkeypatch):
    async def run():
        backend = Backend()
        b = install(monkeypatch, backend, coordinated=False)
        calls, closed = [], []

        async def legacy(client, model, messages, **options):
            calls.append((model, messages, options))
            try:
                yield dict(type="reasoning", text="thinking only")
            finally:
                closed.append(model)

        async def unload(client, model):
            backend.order.append("cleanup")

        monkeypatch.setattr(app, "stream_any_model", legacy)
        monkeypatch.setattr(app, "_unload_omlx_model", unload)
        output = await app._mcp_summon_model("session", "model-a", "unknown-role")
        assert len(calls) == len(closed) == 1
        assert calls[0][1][0]["content"] == app.AGENT_ROLES["critic"]["system"]
        assert calls[0][2] == dict(
            temperature=0.0, max_tokens=3200, reasoning_mode="off"
        )
        # Existing text-only MCP behavior: do not invent HTTP Hall's reasoning fallback.
        assert output == f"### {app.AGENT_ROLES['critic']['label']} (model-a)\n\n"
        assert b.saved[0][0][2:] == ("unknown-role", "")

    asyncio.run(run())


@pytest.mark.parametrize(
    "policy,status",
    [("plan", "confirmed"), ("standard", "confirmed"), ("standard", "pending")],
)
def test_mcp_authorized_entry_preserves_admission_and_refusal(
    tmp_path, monkeypatch, policy, status
):
    async def run():
        backend = Backend(status=status)
        b = install(monkeypatch, backend)
        monkeypatch.setattr(app, "DB_PATH", tmp_path / "mcp-gate.db")
        monkeypatch.setattr(app, "BASE_DIR", tmp_path)
        monkeypatch.setattr(app, "HARNESS_STORE_DIR", tmp_path / ".harnesses")
        app.init_db()

        async def execute():
            return await app._execute_mcp_stdio_tool(
                "summon_omlx_agent",
                {"session_id": "session", "model": "model-a", "role": "critic"},
                "External",
                policy=policy,
                approved=True,
                session_id="mcp-offline",
            )

        success = policy == "standard" and status == "confirmed"
        if policy == "standard" and status == "pending":
            with pytest.raises(app.HTTPException) as error:
                await execute()
            assert error.value.status_code == 503
        else:
            result = await execute()
            assert result["ok"] is success
            if policy == "plan":
                assert "Denied:" in result["output"]
        assert len(b.saved) == int(success)
        assert (
            len(backend.calls)
            == len(backend.closed)
            == len(backend.commands)
            == int(policy == "standard")
        )
        if policy == "plan":
            assert b.clients == []
        else:
            assert b.clients[0].closed
        rows = app.runtime_persistence.run_read(
            app.STORAGE,
            app.DB_PATH,
            lambda conn: conn.execute(
                "SELECT status,ok FROM tool_audit_events WHERE tool_name='mcp_bridge__summon_omlx_agent' ORDER BY id"
            ).fetchall(),
        )
        assert len(rows) == 1 and bool(rows[0][1]) == success

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["ollama", "lmstudio"])
@pytest.mark.parametrize("mode", ["complete", "refuse", "cancel"])
def test_mcp_actual_dormant_warm_native_bytes_fit_and_cancel(
    tmp_path, monkeypatch, runtime, mode
):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from test_native_dispatch_executor import context

    async def run():
        capture = Backend()
        h = install(monkeypatch, capture)
        monkeypatch.setattr(
            app, "parse_model_route", lambda _: ("omlx", None, "model-a")
        )
        await app._mcp_summon_model("session", "fixture::model-a", "critic")
        payload = json.loads(capture.calls[0].payload)
        h.saved.clear()

        def configure(b):
            if runtime == "ollama":
                profile = replace(
                    b.q.profile,
                    think=False,
                    options_json='{"num_ctx":8192,"num_predict":3200,"temperature":0.0}',
                )
                b.q = replace(b.q, profile=profile)
                from test_ollama_lifecycle_bridge import line, packet, done

                b.wire.packets = [
                    line(packet(message=dict(role="assistant", content="READY"))),
                    line(done()),
                ]

        b = await context(
            tmp_path,
            runtime,
            messages=payload["messages"],
            max_tokens=3200,
            temperature=0.0,
            reasoning_mode="off",
            configure=configure,
            input_tokens=7000 if mode == "refuse" else 1000,
        )
        with b.coord:

            def plan(call):
                assert (call.runtime, call.model, call.action) == (
                    b.call.runtime,
                    b.call.model,
                    b.call.action,
                )
                assert json.loads(call.payload) == json.loads(b.call.payload)
                return b.job

            b.executor.plan = plan
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {runtime: b.executor}, {"fixture": runtime}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: ("cloud", {"alias": "fixture"}, b.call.model),
            )
            before = len(b.driver.mutations(b))
            if mode == "cancel":
                entered = asyncio.Event()
                original = b.wire.stream

                @asynccontextmanager
                async def blocked(*args, **kwargs):
                    async with original(*args, **kwargs) as (status, chunks):

                        async def slow():
                            async for chunk in chunks:
                                yield chunk
                                entered.set()
                                await asyncio.Event().wait()

                        yield status, slow()

                b.wire.stream = blocked
                task = asyncio.create_task(
                    app._mcp_summon_model("session", "fixture::model-a", "critic")
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert h.saved == []
                assert b.coord.snapshot("shared").state == "reconciling"
                assert b.coord.snapshot("shared").active_requests == 1
                assert b.memory.view("shared").state == "paused"
                assert b.memory.view("shared").committed_bytes == 600
            elif mode == "refuse":
                with pytest.raises(rd.RuntimeDispatchDenied, match="context_exceeded"):
                    await app._mcp_summon_model("session", "fixture::model-a", "critic")
                assert h.saved == [] and len(b.driver.mutations(b)) == before
            else:
                result = await app._mcp_summon_model(
                    "session", "fixture::model-a", "critic"
                )
                assert "READY" in result and len(h.saved) == 1
            assert b.count_calls == 1
            assert h.clients[-1].closed
            if mode != "refuse":
                assert b.wire.closed
                assert len(b.driver.mutations(b)) == before + 1
                assert b.driver.mutations(b)[-1][2] == json.loads(b.job.prepared.body)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["confirmed", "pending"])
def test_mcp_stdio_jsonrpc_reports_cleanup_failure_as_error(
    tmp_path, monkeypatch, status
):
    import io
    import sys

    backend = Backend(status=status)
    b = install(monkeypatch, backend)
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "mcp-stdio.db")
    monkeypatch.setattr(app, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app, "HARNESS_STORE_DIR", tmp_path / ".harnesses")
    request = dict(
        jsonrpc="2.0",
        id=17,
        method="tools/call",
        params=dict(name="summon_omlx_agent", arguments=dict(session_id="session")),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(sys, "stdout", output)
    app.run_mcp_stdio_server()
    reply = json.loads(output.getvalue())
    assert reply["id"] == 17 and reply["jsonrpc"] == "2.0"
    assert reply["result"]["isError"] is (status != "confirmed")
    assert len(b.saved) == int(status == "confirmed")
    # Existing inbound defaults are explicit in the schema; do not rewrite them.
    assert backend.calls[0].model == "Gemma-4-21B-REAP-4bit"
    assert (
        json.loads(backend.calls[0].payload)["messages"][0]["content"]
        == app.AGENT_ROLES["critic"]["system"]
    )
    assert len(backend.calls) == len(backend.closed) == len(backend.commands) == 1
    if status == "pending":
        assert "Error:" in reply["result"]["content"][0]["text"]
    assert b.clients[0].closed
