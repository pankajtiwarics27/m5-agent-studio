"""Bounded Hall summon composition: fake backends, not installed qualification."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app
import runtime_dispatch_boundary as rd


USAGE = dict(
    type="usage",
    decode_tps=1.0,
    prefill_tps=1.0,
    ttft=0.1,
    completion_tokens=1,
    cached_tokens=0,
    finish_reason="stop",
)


class HallBackend:
    def __init__(self, kind="content", status="confirmed"):
        self.kind, self.status = kind, status
        self.calls, self.closed, self.commands = [], [], []

    async def stream(self, call):
        self.calls.append(call)
        try:
            yield dict(
                type=self.kind,
                text="THINKING" if self.kind == "reasoning" else "ANSWER",
                id="native-id",
                name="read",
                arguments="{}",
            )
            yield dict(USAGE)
        finally:
            self.closed.append(call)

    async def command(self, call):
        self.commands.append(call)
        return {"status": self.status}


def hall_fixture(monkeypatch, backend, *, coordinated=True):
    monkeypatch.setattr(
        app,
        "_LOCAL_RUNTIME_DISPATCH",
        rd.RuntimeDispatchBoundary(
            coordinated, {"omlx": backend} if coordinated else None
        ),
    )
    monkeypatch.setattr(app, "list_cloud_providers", lambda **kwargs: [])
    monkeypatch.setattr(app, "_HALL_RUNNING_SUMMONS", set())
    monkeypatch.setattr(app, "_HALL_ACTIVE_TURNS", {})
    monkeypatch.setattr(app, "_HALL_ABANDONED_TURNS", {})
    session = dict(
        id="hall-offline",
        status="open",
        request_text="SECURITY MATTER " + "keep ಕರ್ನಾಟಕ 😀 " * 3000 + "LATEST",
        context_files=["evidence.md"],
        messages=[
            dict(author="Human", content="PRIOR-ANCHOR " * 1000),
            dict(author="critic", content="COMPLETE FEEDBACK"),
        ],
    )
    saved, pinned, recorded = [], [], []
    monkeypatch.setattr(app, "get_hall_session", lambda sid: session)

    def save(*args, **kwargs):
        saved.append((args, kwargs))
        return dict(id="fake-message", author=args[1], content=args[3])

    monkeypatch.setattr(app, "append_hall_message", save)
    monkeypatch.setattr(app, "close_hall_session_db", lambda *args: pinned.append(args))
    monkeypatch.setattr(
        app, "record_perf", lambda *args, **kwargs: recorded.append((args, kwargs))
    )
    skill = "\nSKILL-ANCHOR ಕರ್ನಾಟಕ 😀"
    workspace = "\nWORKSPACE-CONTEXT " + "full context " * 2000

    async def skills(ids, matter):
        assert ids == ["fixture-skill"] and matter == session["request_text"]
        return skill, []

    def read(files):
        assert files == session["context_files"]
        return workspace

    monkeypatch.setattr(app, "build_skills_context", skills)
    monkeypatch.setattr(app, "read_workspace_context_block", read)

    async def no_probe(*args, **kwargs):
        raise AssertionError("legacy probe reached")

    if coordinated:
        monkeypatch.setattr(app, "_omlx_is_online", no_probe)
    return SimpleNamespace(
        session=session,
        saved=saved,
        pinned=pinned,
        recorded=recorded,
        skill=skill,
        workspace=workspace,
    )


async def response(
    *, disconnect=False, auto_unload=False, pin=False, model="model-a", reasoning="high"
):
    async def body():
        return dict(
            model=model,
            role_key="architect",
            reasoning_mode=reasoning,
            skills=["fixture-skill"],
            auto_unload=auto_unload,
            pin_as_conclusion=pin,
        )

    async def disconnected():
        return disconnect

    return await app.api_hall_summon(
        "hall-offline",
        SimpleNamespace(
            json=body,
            is_disconnected=disconnected,
            app=SimpleNamespace(state=SimpleNamespace(http=object())),
        ),
    )


async def collect(reply):
    return "".join(
        [v.decode() if isinstance(v, bytes) else v async for v in reply.body_iterator]
    )


def assert_idle():
    assert "hall-offline" not in app._HALL_RUNNING_SUMMONS
    assert "hall-offline" not in app._HALL_ACTIVE_TURNS


def test_hall_reasoning_only_never_retries_changed_budget_in_coordinated_mode(
    monkeypatch,
):
    async def run():
        backend = HallBackend("reasoning")
        b = hall_fixture(monkeypatch, backend)
        output = await collect(await response())
        assert len(backend.calls) == 1
        payload = json.loads(backend.calls[0].payload)
        assert payload["max_tokens"] == 4600 and payload["reasoning_mode"] == "high"
        assert "event: summon_done" in output and len(b.saved) == 1
        assert b.saved[0][0][3] == "THINKING"
        assert_idle()

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["close", "disconnect"])
def test_hall_close_or_disconnect_immediately_closes_source_without_saving(
    monkeypatch, stop
):
    async def run():
        backend = HallBackend()
        b = hall_fixture(monkeypatch, backend)
        reply = await response(
            disconnect=stop == "disconnect", auto_unload=True, pin=True
        )
        if stop == "close":
            assert "summon_start" in await anext(reply.body_iterator)
            assert "event: token" in await anext(reply.body_iterator)
            await reply.body_iterator.aclose()
        else:
            output = await collect(reply)
            assert "summon_done" not in output
        assert len(backend.closed) == 1
        assert b.saved == b.pinned == backend.commands == []
        assert "hall-offline" in app._HALL_ABANDONED_TURNS
        assert_idle()

    asyncio.run(run())


def test_hall_full_input_preserves_matter_workspace_chronicle_skills_and_options(
    monkeypatch,
):
    async def run():
        backend = HallBackend()
        b = hall_fixture(monkeypatch, backend)
        output = await collect(await response())
        payload = json.loads(backend.calls[0].payload)
        transcript = "\n\n".join(
            f"--- [{m['author']}] ---\n{m['content']}" for m in b.session["messages"]
        )
        author = f"{app.AGENT_ROLES['architect']['label']} (model-a)"
        expected = f"### Council Matter:\n{b.session['request_text']}{b.workspace}\n\n### Council Hall Proceeding Log So Far:\n{transcript}\n\nNow contribute your response as **{author}**. Build directly on prior feedback and challenge flaws."
        assert payload == dict(
            messages=[
                dict(
                    role="system",
                    content=app.AGENT_ROLES["architect"]["system"] + b.skill,
                ),
                dict(role="user", content=expected),
            ],
            tools=None,
            temperature=0.0,
            max_tokens=4600,
            reasoning_mode="high",
        )
        assert len(b.saved) == len(b.recorded) == len(backend.closed) == 1
        assert "summon_done" in output and not app._HALL_ABANDONED_TURNS
        assert_idle()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["pending", "uncertain", "refused", "ok"])
def test_hall_cleanup_failure_keeps_completed_transcript_but_never_reports_summon_success(
    monkeypatch, status
):
    async def run():
        backend = HallBackend(status=status)
        b = hall_fixture(monkeypatch, backend)
        output = await collect(await response(auto_unload=True, pin=True))
        # Hall intentionally saves/pins the completed answer before optional cleanup.
        assert (
            len(b.saved)
            == len(b.pinned)
            == len(backend.calls)
            == len(backend.commands)
            == 1
        )
        assert "event: error" in output and "summon_done" not in output
        assert not app._HALL_ABANDONED_TURNS
        assert_idle()

    asyncio.run(run())


@pytest.mark.parametrize("advertised", [False, True])
def test_hall_text_consumer_refuses_native_tools_without_transcript_or_cleanup(
    monkeypatch, advertised
):
    async def run():
        backend = HallBackend("tool_call")
        b = hall_fixture(monkeypatch, backend)
        token = app._CHAT_NATIVE_TOOLS.set(
            [dict(type="function", function=dict(name="read"))] if advertised else None
        )
        try:
            output = await collect(await response(auto_unload=True, pin=True))
            assert "canonical_consumer_required" in output
            assert len(backend.calls) == (0 if advertised else 1)
            assert b.saved == b.pinned == backend.commands == []
            assert "summon_done" not in output
            assert_idle()
        finally:
            app._CHAT_NATIVE_TOOLS.reset(token)

    asyncio.run(run())


def test_hall_legacy_retry_retains_options_and_closes_both_attempts(monkeypatch):
    async def run():
        b = hall_fixture(monkeypatch, HallBackend(), coordinated=False)
        calls, closed = [], []

        async def legacy(client, model, messages, **kwargs):
            calls.append((messages, kwargs))
            try:
                yield dict(
                    type="reasoning" if len(calls) == 1 else "content", text="legacy"
                )
            finally:
                closed.append(kwargs)

        monkeypatch.setattr(app, "stream_any_model", legacy)
        output = await collect(await response())
        assert [c[1] for c in calls] == [
            dict(temperature=0.0, max_tokens=4600, reasoning_mode="high"),
            dict(temperature=0.0, max_tokens=4096, reasoning_mode="off"),
        ]
        assert calls[0][0] == calls[1][0] and len(closed) == 2
        assert len(b.saved) == 1 and "summon_done" in output
        assert_idle()

    asyncio.run(run())


def test_hall_consumer_close_during_legacy_retry_closes_retry_and_records_unsaved(
    monkeypatch,
):
    async def run():
        b = hall_fixture(monkeypatch, HallBackend(), coordinated=False)
        calls, closed = [], []

        async def legacy(*args, **kwargs):
            calls.append(kwargs)
            try:
                yield dict(
                    type="reasoning" if len(calls) == 1 else "content", text="prefix"
                )
            finally:
                closed.append(kwargs)

        monkeypatch.setattr(app, "stream_any_model", legacy)
        reply = await response(auto_unload=True, pin=True)
        for _ in range(3):
            await anext(reply.body_iterator)
        await reply.body_iterator.aclose()
        assert len(calls) == len(closed) == 2
        assert b.saved == b.pinned == []
        assert "hall-offline" in app._HALL_ABANDONED_TURNS
        assert_idle()

    asyncio.run(run())


def test_hall_task_cancellation_closes_source_without_cleanup_or_save(monkeypatch):
    async def run():
        entered = asyncio.Event()

        class Blocked(HallBackend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="content", text="prefix")
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    self.closed.append(call)

        backend = Blocked()
        b = hall_fixture(monkeypatch, backend)
        task = asyncio.create_task(collect(await response(auto_unload=True, pin=True)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(backend.closed) == 1
        assert b.saved == b.pinned == backend.commands == []
        assert app._HALL_ABANDONED_TURNS["hall-offline"]["streamed_chars"] == len(
            "prefix"
        )
        assert_idle()

    asyncio.run(run())


def test_hall_confirmed_cleanup_follows_save_pin_and_source_close(monkeypatch):
    async def run():
        class Ordered(HallBackend):
            async def command(self, call):
                assert len(self.closed) == len(b.saved) == len(b.pinned) == 1
                assert "hall-offline" in app._HALL_ACTIVE_TURNS
                return await super().command(call)

        backend = Ordered()
        b = hall_fixture(monkeypatch, backend)
        output = await collect(await response(auto_unload=True, pin=True))
        assert len(backend.commands) == 1
        assert "summon_done" in output and "event: error" not in output
        assert not app._HALL_ABANDONED_TURNS
        assert_idle()

    asyncio.run(run())


def test_hall_partial_native_failure_never_saves_pins_or_cleans_up(monkeypatch):
    async def run():
        class Failed(HallBackend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="reasoning", text="prefix")
                    raise OSError("private-native-failure")
                finally:
                    self.closed.append(call)

        backend = Failed()
        b = hall_fixture(monkeypatch, backend)
        output = await collect(await response(auto_unload=True, pin=True))
        assert "coordinated_execution_unavailable" in output
        assert "private-native-failure" not in output and "summon_done" not in output
        assert len(backend.calls) == len(backend.closed) == 1
        assert b.saved == b.pinned == backend.commands == []
        assert_idle()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("stop", ["complete", "close", "refuse"])
def test_hall_actual_dormant_warm_native_fit_and_close(
    tmp_path, monkeypatch, backend, stop
):
    from test_native_dispatch_executor import context

    async def run():
        capture = HallBackend()
        h = hall_fixture(monkeypatch, capture)
        await collect(await response(reasoning="off"))
        payload = json.loads(capture.calls[0].payload)

        def configure(b):
            from dataclasses import replace

            if backend == "ollama":
                # Explicit fake approved profile for Hall options, not live qualification.
                profile = replace(
                    b.q.profile,
                    think=False,
                    options_json='{"num_ctx":8192,"num_predict":4600,"temperature":0.0}',
                )
                b.q = replace(b.q, profile=profile)
                from test_ollama_lifecycle_bridge import line, packet, done

                b.wire.packets = [
                    line(packet(message=dict(role="assistant", content="READY"))),
                    line(done()),
                ]

        b = await context(
            tmp_path,
            backend,
            messages=payload["messages"],
            max_tokens=payload["max_tokens"],
            temperature=payload["temperature"],
            reasoning_mode=payload["reasoning_mode"],
            configure=configure,
            input_tokens=7000 if stop == "refuse" else 1000,
        )
        with b.coord:
            h.saved.clear()
            h.recorded.clear()
            monkeypatch.setattr(
                app,
                "_LOCAL_RUNTIME_DISPATCH",
                rd.RuntimeDispatchBoundary(
                    True, {backend: b.executor}, {"fixture": backend}
                ),
            )
            monkeypatch.setattr(
                app,
                "parse_model_route",
                lambda _: ("cloud", {"alias": "fixture"}, b.call.model),
            )

            def plan(call):
                assert (call.runtime, call.model, call.action) == (
                    b.call.runtime,
                    b.call.model,
                    b.call.action,
                )
                assert json.loads(call.payload) == json.loads(b.call.payload)
                return b.job

            b.executor.plan = plan
            before = len(b.driver.mutations(b))
            reply = await response(model="model-a", reasoning="off")
            if stop == "close":
                assert "summon_start" in await anext(reply.body_iterator)
                assert "event: token" in await anext(reply.body_iterator)
                await reply.body_iterator.aclose()
                assert h.saved == []
                assert b.coord.snapshot("shared").state == "reconciling"
                assert b.coord.snapshot("shared").active_requests == 1
                assert b.memory.view("shared").state == "paused"
                assert b.memory.view("shared").committed_bytes == 600
            elif stop == "refuse":
                output = await collect(reply)
                assert "context_exceeded" in output and "summon_done" not in output
                assert len(b.driver.mutations(b)) == before
                assert h.saved == []
            else:
                output = await collect(reply)
                assert "summon_done" in output and "event: error" not in output
                assert len(h.saved) == 1
            assert b.count_calls == 1
            if stop != "refuse":
                assert b.wire.closed
                assert len(b.driver.mutations(b)) == before + 1
                sent = b.driver.mutations(b)[-1][2]
                assert sent == json.loads(b.job.prepared.body)
            assert_idle()

    asyncio.run(run())
