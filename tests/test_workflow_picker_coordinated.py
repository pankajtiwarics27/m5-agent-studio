"""Bounded folder-name suggestion owner; offline dispatch, never folder authority."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app
import runtime_dispatch_boundary as rd


class Backend:
    def __init__(self, packets=None):
        self.packets = (
            packets
            if packets is not None
            else [dict(type="content", text='{"name":"safe-project"}')]
        )
        self.calls, self.closed, self.commands = [], [], []

    async def stream(self, call):
        self.calls.append(call)
        try:
            for packet in self.packets:
                yield packet
        finally:
            self.closed.append(call)

    async def command(self, call):
        self.commands.append(call)
        raise AssertionError("suggestion cannot authorize model cleanup")


def install(monkeypatch, backend):
    monkeypatch.setattr(
        app,
        "_LOCAL_RUNTIME_DISPATCH",
        rd.RuntimeDispatchBoundary(True, {"omlx": backend}),
    )
    monkeypatch.setattr(app, "list_cloud_providers", lambda **kwargs: [])
    actions = []

    def no_create(*args, **kwargs):
        actions.append((args, kwargs))
        raise AssertionError("suggestion is not folder creation authority")

    monkeypatch.setattr(app.workspace_paths, "create_folder", no_create)
    return actions


async def suggest(**overrides):
    payload = dict(confirmed=True, model="model-a", description="a diary") | overrides

    async def body():
        return payload

    request = SimpleNamespace(
        json=body, app=SimpleNamespace(state=SimpleNamespace(http=object()))
    )
    return await app.api_workspace_picker_suggest(request)


def test_picker_oversize_output_closes_source_before_return_and_restores_tools(
    monkeypatch,
):
    async def run():
        backend = Backend([dict(type="content", text="x" * 4097)])
        actions = install(monkeypatch, backend)
        inherited = [dict(type="function", function=dict(name="read"))]
        token = app._CHAT_NATIVE_TOOLS.set(inherited)
        try:
            with pytest.raises(app.HTTPException) as error:
                await suggest()
            assert error.value.status_code == 400 and "too long" in error.value.detail
            assert len(backend.calls) == len(backend.closed) == 1
            assert backend.commands == actions == []
            assert app._CHAT_NATIVE_TOOLS.get() is inherited
        finally:
            app._CHAT_NATIVE_TOOLS.reset(token)

    asyncio.run(run())


def test_picker_preserves_entire_valid_description_and_fixed_options_without_authority(
    monkeypatch,
):
    async def run():
        backend = Backend()
        actions = install(monkeypatch, backend)
        description = ("SECURITY ಕರ್ನಾಟಕ 😀 " * 100)[:1000]
        assert len(description) == 1000
        inherited = [dict(type="function", function=dict(name="read"))]
        token = app._CHAT_NATIVE_TOOLS.set(inherited)
        try:
            result = await suggest(description=" \n" + description + "\n ")
            assert json.loads(result.body) == dict(
                name="safe-project", created=False, workspace_changed=False
            )
            assert len(backend.calls) == len(backend.closed) == 1
            call = backend.calls[0]
            assert call.model == "model-a"
            assert json.loads(call.payload) == dict(
                messages=[
                    dict(
                        role="system",
                        content='Suggest a short portable project folder NAME for the description. Return only JSON {"name":"example-project"}. No paths, hidden names, commands or extra fields. This is a suggestion, not an action.',
                    ),
                    dict(role="user", content=description),
                ],
                tools=None,
                temperature=0.0,
                max_tokens=128,
                reasoning_mode="off",
            )
            assert backend.commands == actions == []
            assert app._CHAT_NATIVE_TOOLS.get() is inherited
        finally:
            app._CHAT_NATIVE_TOOLS.reset(token)

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        dict(confirmed=False),
        dict(confirmed=1),
        dict(confirmed="true"),
        dict(description=""),
        dict(description="x" * 1001),
        dict(model=""),
        dict(model="x" * 513),
    ],
)
def test_picker_invalid_consent_or_input_never_dispatches(monkeypatch, payload):
    async def run():
        backend = Backend()
        actions = install(monkeypatch, backend)
        with pytest.raises(app.HTTPException) as error:
            await suggest(**payload)
        assert error.value.status_code == 400
        assert backend.calls == backend.commands == actions == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "packet",
    [
        dict(type="reasoning", text='{"name":"not-authority"}'),
        dict(type="content", text='{"name":"../escape"}'),
        dict(type="content", text='{"name":"safe","command":"mkdir x"}'),
    ],
)
def test_picker_reasoning_or_unsafe_text_never_becomes_folder_authority(
    monkeypatch, packet
):
    async def run():
        backend = Backend([packet])
        actions = install(monkeypatch, backend)
        with pytest.raises(app.HTTPException) as error:
            await suggest()
        assert error.value.status_code == 400
        assert len(backend.calls) == len(backend.closed) == 1
        assert backend.commands == actions == []

    asyncio.run(run())


def test_picker_native_tool_call_is_refused_not_executed(monkeypatch):
    async def run():
        backend = Backend(
            [
                dict(
                    type="tool_call",
                    id="native-id",
                    name="create_folder",
                    arguments='{"name":"not-authority"}',
                )
            ]
        )
        actions = install(monkeypatch, backend)
        with pytest.raises(app.HTTPException) as error:
            await suggest()
        assert error.value.status_code == 502
        assert len(backend.calls) == len(backend.closed) == 1
        assert backend.commands == actions == []

    asyncio.run(run())


def test_picker_partial_backend_failure_is_private_and_never_retried(monkeypatch):
    async def run():
        class Failed(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="content", text='{"name":')
                    raise OSError("PRIVATE_PROVIDER_FAILURE")
                finally:
                    self.closed.append(call)

        backend = Failed()
        actions = install(monkeypatch, backend)
        with pytest.raises(app.HTTPException) as error:
            await suggest()
        assert error.value.status_code == 502
        assert "PRIVATE_PROVIDER_FAILURE" not in error.value.detail
        assert len(backend.calls) == len(backend.closed) == 1
        assert backend.commands == actions == []

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_picker_task_cancellation_or_timeout_closes_source_and_restores_context(
    monkeypatch, stop
):
    async def run():
        entered = asyncio.Event()
        inherited = [dict(type="function", function=dict(name="read"))]

        class Blocked(Backend):
            async def stream(self, call):
                self.calls.append(call)
                try:
                    yield dict(type="content", text='{"name":')
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    self.closed.append(call)

        backend = Blocked()
        actions = install(monkeypatch, backend)
        if stop == "timeout":
            real_timeout = asyncio.timeout

            def short_timeout(seconds):
                assert seconds == 45
                return real_timeout(0.01)

            monkeypatch.setattr(app.asyncio, "timeout", short_timeout)

        async def consume():
            token = app._CHAT_NATIVE_TOOLS.set(inherited)
            try:
                if stop == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await suggest()
                else:
                    with pytest.raises(app.HTTPException) as error:
                        await suggest()
                    assert error.value.status_code == 502
                assert app._CHAT_NATIVE_TOOLS.get() is inherited
            finally:
                app._CHAT_NATIVE_TOOLS.reset(token)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=5)
        if stop == "cancel":
            task.cancel()
        await task
        assert len(backend.calls) == len(backend.closed) == 1
        assert backend.commands == actions == []

    asyncio.run(run())


@pytest.mark.parametrize("runtime", ["ollama", "lmstudio"])
@pytest.mark.parametrize("mode", ["complete", "refuse", "cancel"])
def test_picker_actual_dormant_warm_native_fit_bytes_and_cancel(
    tmp_path, monkeypatch, runtime, mode
):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from test_native_dispatch_executor import context

    async def run():
        capture = Backend()
        actions = install(monkeypatch, capture)
        description = ("Keep every character ಕರ್ನಾಟಕ 😀 " * 60)[:1000]
        await suggest(description=description)
        payload = json.loads(capture.calls[0].payload)

        def configure(b):
            if runtime == "ollama":
                profile = replace(
                    b.q.profile,
                    think=False,
                    options_json='{"num_ctx":8192,"num_predict":128,"temperature":0.0}',
                )
                b.q = replace(b.q, profile=profile)
                from test_ollama_lifecycle_bridge import line, packet, done

                b.wire.packets = [
                    line(
                        packet(
                            message=dict(
                                role="assistant", content='{"name":"safe-project"}'
                            )
                        )
                    ),
                    line(done()),
                ]
            else:
                from test_lmstudio_lifecycle_bridge import sse, packet, final

                b.wire.chunks = [
                    sse(
                        packet(
                            choices=[
                                dict(
                                    index=0,
                                    delta=dict(content='{"name":"safe-project"}'),
                                    finish_reason=None,
                                )
                            ]
                        )
                    ),
                    final(),
                ]

        b = await context(
            tmp_path,
            runtime,
            messages=payload["messages"],
            max_tokens=128,
            temperature=0.0,
            reasoning_mode="off",
            configure=configure,
            input_tokens=8100 if mode == "refuse" else 1000,
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
            inherited = [dict(type="function", function=dict(name="read"))]

            async def consume():
                token = app._CHAT_NATIVE_TOOLS.set(inherited)
                try:
                    return await suggest(description=description)
                finally:
                    assert app._CHAT_NATIVE_TOOLS.get() is inherited
                    app._CHAT_NATIVE_TOOLS.reset(token)

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
                task = asyncio.create_task(consume())
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert b.coord.snapshot("shared").state == "reconciling"
                assert b.coord.snapshot("shared").active_requests == 1
                assert b.memory.view("shared").state == "paused"
                assert b.memory.view("shared").committed_bytes == 600
            elif mode == "refuse":
                with pytest.raises(app.HTTPException) as error:
                    await consume()
                assert error.value.status_code == 502
                assert len(b.driver.mutations(b)) == before
            else:
                result = await consume()
                assert json.loads(result.body) == dict(
                    name="safe-project", created=False, workspace_changed=False
                )
            assert b.count_calls == 1 and actions == []
            if mode != "refuse":
                assert b.wire.closed
                assert len(b.driver.mutations(b)) == before + 1
                assert b.driver.mutations(b)[-1][2] == json.loads(b.job.prepared.body)

    asyncio.run(run())
