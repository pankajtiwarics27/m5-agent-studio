from __future__ import annotations

import asyncio

import pytest

import app


_FIRST_CHUNK = {"type": "content", "text": "local-first-chunk"}
_MESSAGES = [{"role": "user", "content": "fixture prompt"}]


def _combo(monkeypatch, targets: list[str]) -> None:
    # Keep route resolution fully in memory; no provider or DB access.
    monkeypatch.setattr(app, "list_cloud_providers", lambda *a, **k: [])
    monkeypatch.setattr(
        app,
        "list_model_combos",
        lambda: [
            {
                "alias": "fx",
                "name": "fixture bundle",
                "model_id": "combo/fx",
                "targets": list(targets),
                "updated_at": 0,
            }
        ],
    )


def test_combo_consumer_close_closes_delegated_inner_source(monkeypatch):
    """Closing the outer dispatcher must close the delegated model source."""
    state = {"closed": False}

    async def source(_client, _model, *_args, **_kwargs):
        try:
            yield _FIRST_CHUNK
            await asyncio.Event().wait()
        finally:
            state["closed"] = True

    _combo(monkeypatch, ["omlx/fixture-target"])
    monkeypatch.setattr(app, "_stream_omlx_completion", lambda *a, **k: source(*a, **k))

    async def run():
        agen = app.stream_any_model(object(), "combo/fx", _MESSAGES)
        first = await agen.__anext__()
        assert first is _FIRST_CHUNK
        await agen.aclose()
        assert state["closed"], "closing the combo dispatcher must close the delegated local source"

    asyncio.run(run())


def test_combo_error_after_content_closes_delegated_inner_source(monkeypatch):
    """An error after content must not abandon the delegated model source open."""
    state = {"closed": False}

    async def source(_client, _model, *_args, **_kwargs):
        try:
            yield _FIRST_CHUNK
            raise RuntimeError("fixture downstream failure")
        finally:
            state["closed"] = True

    _combo(monkeypatch, ["omlx/fixture-target"])
    monkeypatch.setattr(app, "_stream_omlx_completion", lambda *a, **k: source(*a, **k))

    async def run():
        agen = app.stream_any_model(object(), "combo/fx", _MESSAGES)
        first = await agen.__anext__()
        assert first is _FIRST_CHUNK
        with pytest.raises(RuntimeError, match="fixture downstream failure"):
            await agen.__anext__()
        assert state["closed"], "the delegated source must be closed when the failure propagates"

    asyncio.run(run())


def test_combo_failure_before_content_continues_and_leaves_failed_source_closed(monkeypatch):
    """A fixture target that fails before emitting anything must be closed before the next target runs."""
    first_closed = {"v": False}
    order: list[str] = []

    async def failing(_client, _model, *_args, **_kwargs):
        try:
            raise RuntimeError("fixture target failure")
        finally:
            first_closed["v"] = True
        yield {"type": "content", "text": "never emitted"}

    async def succeeding(_client, _model, *_args, **_kwargs):
        try:
            order.append("success-source-started")
            yield {"type": "content", "text": "second-target-chunk"}
        finally:
            order.append("success-source-closed")

    def dispatch(_client, model, *_args, **_kwargs):
        return failing(_client, model, *_args, **_kwargs) if model == "t-fail" else succeeding(_client, model, *_args, **_kwargs)

    _combo(monkeypatch, ["omlx/t-fail", "omlx/t-good"])
    monkeypatch.setattr(app, "_stream_omlx_completion", dispatch)

    async def run():
        events = [
            item
            async for item in app.stream_any_model(object(), "combo/fx", _MESSAGES)
        ]
        texts = [item.get("text") for item in events if item.get("type") == "content"]
        assert "second-target-chunk" in texts
        assert first_closed["v"], "the failed target's source must be closed before the successor runs"
        assert order[0] == "success-source-started"

    asyncio.run(run())


def test_full_combo_consumption_closes_delegated_inner_source(monkeypatch):
    """Normal exhaustion remains clean and leaves the delegated source closed."""
    state = {"closed": False}

    async def source(_client, _model, *_args, **_kwargs):
        try:
            yield _FIRST_CHUNK
        finally:
            state["closed"] = True

    _combo(monkeypatch, ["omlx/fixture-target"])
    monkeypatch.setattr(app, "_stream_omlx_completion", lambda *a, **k: source(*a, **k))

    async def run():
        events = [
            item
            async for item in app.stream_any_model(object(), "combo/fx", _MESSAGES)
        ]
        assert any(item is _FIRST_CHUNK for item in events)
        assert state["closed"]

    asyncio.run(run())
