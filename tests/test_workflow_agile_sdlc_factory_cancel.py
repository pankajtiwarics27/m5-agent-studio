from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app

_SESSION = "sess-lc08-factory-"
_STORY = {
    "id": "STORY-01",
    "title": "fixture story",
    "priority": "P0",
    "passes": False,
    "attempts": 0,
    "acceptance_criteria": ["criterion one"],
    "verification_cmd": "pytest -q fixture",
}


async def _async_return(value):
    return value


async def _async_return_iter(items):
    for item in items:
        yield item


def _base_state():
    return {
        "step_count": 0,
        "events": [],
        "notes": [],
        "backlog_writes": [],
        "leases": [],
        "sprint_closed": False,
        "lead_closed": False,
        "critic_closed": False,
        "task_final": None,
    }


def _request(body):
    async def payload():
        return body

    async def is_disconnected():
        return False

    return SimpleNamespace(
        json=payload,
        is_disconnected=is_disconnected,
        app=SimpleNamespace(state=SimpleNamespace(http=object())),
    )


def _fixture_stub(monkeypatch, tmp_path, state):
    """Route every DB/global collaborator of the SDLC factory in memory."""
    monkeypatch.setattr(app.workspace_quarantine, "find_active_quarantine", lambda *a, **k: None)

    def _session_db(sid, include_events=False):
        return {
            "id": sid,
            "session_id": sid,
            "status": "running",
            "policy": "standard",
            "step_count": state["step_count"],
            "worktree_dir": str(tmp_path),
            "title": "fixture SDLC session",
            "lead_model": "cloudtest::lead",
            "critic_model": "cloudtest::critic",
        }

    monkeypatch.setattr(app, "get_harness_session_db", _session_db)

    def _update(sid, **kwargs):
        state["step_count"] += int(kwargs.get("step_inc") or 0)

    monkeypatch.setattr(app, "_update_harness_session_state", _update)
    monkeypatch.setattr(
        app,
        "_append_harness_event",
        lambda *a, **k: state["events"].append((a, k)),
    )
    monkeypatch.setattr(app, "_compute_git_diff", lambda target_dir: {"status": "clean", "diff": ""})
    monkeypatch.setattr(app, "build_skills_context", lambda *a, **k: _async_return(("", [])))
    monkeypatch.setattr(app, "_fetch_online_inspiration", lambda *a, **k: _async_return("fixture references"))
    monkeypatch.setattr(app, "run_codegraph_tool", lambda *a, **k: "fixture codegraph summary")
    monkeypatch.setattr(app, "build_efficiency_mode_prompt", lambda *a, **k: "")

    files: dict[str, str] = {}

    def _confined_text(_sdlc_dir, filename):
        return files.get(filename, "")

    def _confined_write(_sdlc_dir, filename, content, *, append=False):
        files[filename] = (files.get(filename, "") + content) if append else content
        state["notes"].append(filename)

    monkeypatch.setattr(app, "_read_sdlc_confined_text", _confined_text)
    monkeypatch.setattr(app, "_write_sdlc_confined_text", _confined_write)

    def _write_backlog(_sdlc_dir, goal, stories):
        files["backlog.json"] = json.dumps(stories)
        state["backlog_writes"].append([dict(s) for s in stories])

    monkeypatch.setattr(app, "_write_sdlc_backlog", _write_backlog)
    monkeypatch.setattr(
        app,
        "_read_sdlc_backlog",
        lambda sdlc_dir: json.loads(files.get("backlog.json", json.dumps([dict(_STORY)]))),
    )
    monkeypatch.setattr(
        app,
        "_parse_or_synthesize_backlog_json",
        lambda *a, **k: [dict(_STORY)],
    )

    lease = SimpleNamespace(epoch_record=SimpleNamespace(epoch=7))

    def _acquire(sess, **kwargs):
        coord = SimpleNamespace(
            heartbeat=lambda *a, **k: state["leases"].append(("heartbeat", ())),
            release=lambda *a, **k: state["leases"].append(("release", ())) or True,
        )
        return coord, lease, "worker-1"

    monkeypatch.setattr(app, "_acquire_turn_lease_or_409", _acquire)

    task_machine = SimpleNamespace(
        check_safe_boundary=lambda *a, **k: "complete",
        complete_task=lambda *a, **k: state.__setitem__("task_final", "complete"),
        pause_at_safe_boundary=lambda *a, **k: state.__setitem__("task_final", "pause"),
        cancel_task=lambda *a, **k: state.__setitem__("task_final", "cancel"),
        store=SimpleNamespace(get_task=lambda *a, **k: None),
    )
    monkeypatch.setattr(app, "get_task_state_machine", lambda *a, **k: task_machine)
    monkeypatch.setattr(app, "_collect_step_evidence_refs", lambda *a, **k: [])
    monkeypatch.setattr(app, "is_local_omlx_model", lambda model: False)


def _model_dispatch(monkeypatch, state):
    def dispatch(_client, model, *_args, **_kwargs):
        if model == "cloudtest::lead":
            async def lead():
                try:
                    yield {"type": "content", "text": "fixture plan content"}
                finally:
                    state["lead_closed"] = True
            return lead()
        if model == "cloudtest::critic":
            async def critic():
                try:
                    yield {"type": "content", "text": "fixture critic content"}
                finally:
                    state["critic_closed"] = True
            return critic()
        raise AssertionError(f"unexpected model dispatch: {model}")

    monkeypatch.setattr(app, "stream_any_model", dispatch)


def _body(session_id, **extra):
    body = {
        "session_id": session_id,
        "efficiency_mode": "ponytail+concise",
    }
    body.update(extra)
    return body


def test_consumer_close_mid_sprint_runner_closes_delegated_runner(monkeypatch, tmp_path):
    """Client disconnect at the sprint-runner yield must close the delegated runner."""
    state = _base_state()

    async def runner(**kwargs):
        try:
            yield "sprint-chunk-1"
            await asyncio.Event().wait()
        finally:
            state["sprint_closed"] = True

    _fixture_stub(monkeypatch, tmp_path, state)
    monkeypatch.setattr(app, "_run_sdlc_sprints_stream", lambda **kwargs: runner())

    async def run():
        response = await app.api_run_agile_sdlc_factory(_request(_body(
            _SESSION + "close",
            mode="run_sprints",
            max_stories=1,
            max_steps=2,
        )))
        gen = response.body_iterator
        async for event in gen:
            if event == "sprint-chunk-1":
                break
        await gen.aclose()
        assert state["sprint_closed"], "closing the factory stream must close the delegated sprint runner"

    asyncio.run(run())


def test_phase_one_failure_yields_error_frame_and_finalizes_source(monkeypatch, tmp_path):
    """A research-phase model failure surfaces as an error frame; the source finalizes."""
    state = _base_state()

    async def research_source():
        try:
            yield {"type": "content", "text": "partial research"}
            raise RuntimeError("fixture research failure")
        finally:
            state["critic_closed"] = True

    def dispatch(_client, model, *_args, **_kwargs):
        if model == "cloudtest::critic":
            return research_source()
        if model == "cloudtest::lead":
            return _async_return_iter([{"type": "content", "text": "lead plan"}])
        raise AssertionError(model)

    monkeypatch.setattr(app, "stream_any_model", dispatch)
    _fixture_stub(monkeypatch, tmp_path, state)
    monkeypatch.setattr(app, "_run_sdlc_sprints_stream", lambda **kwargs: _async_return_iter(()))

    async def run():
        response = await app.api_run_agile_sdlc_factory(_request(_body(
            _SESSION + "phase-fail",
            mode="full",
            prompt="fixture product vision",
            lead_model="cloudtest::lead",
            critic_model="cloudtest::critic",
        )))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    final = events[-1]
    assert "error" in final, final[:200]
    assert "fixture research failure" in final, final[:200]
    assert state["critic_closed"], "a phase-model failure must finalize the delegated source"


def test_consumer_cancel_mid_sprint_runner_unwinds_and_closes_runner(monkeypatch, tmp_path):
    """Cancellation while awaiting the sprint runner unwinds and closes it."""
    state = _base_state()
    runner_entered = asyncio.Event()

    async def runner(**kwargs):
        try:
            runner_entered.set()
            await asyncio.Event().wait()
            yield "never-emitted"
        finally:
            state["sprint_closed"] = True

    _fixture_stub(monkeypatch, tmp_path, state)
    monkeypatch.setattr(app, "_run_sdlc_sprints_stream", lambda **kwargs: runner())

    async def run():
        response = await app.api_run_agile_sdlc_factory(_request(_body(
            _SESSION + "cancel",
            mode="run_sprints",
            max_stories=1,
            max_steps=2,
        )))
        gen = response.body_iterator
        first = await gen.__anext__()
        assert "turn_init" in first
        pending = asyncio.create_task(gen.__anext__())
        for _ in range(2000):
            await asyncio.sleep(0.001)
            if runner_entered.is_set():
                break
        assert runner_entered.is_set(), "the factory must reach the sprint-runner await"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert state["sprint_closed"], "cancellation mid-runner must unwind the delegated sprint runner"

    asyncio.run(run())


def test_sprint_runner_exhaustion_streams_chunks_and_completes(monkeypatch, tmp_path):
    """Normal exhaustion streams every chunk, closes the runner and completes the task."""
    state = _base_state()

    async def runner(**kwargs):
        try:
            yield "sprint-chunk-1"
            yield "sprint-chunk-2"
        finally:
            state["sprint_closed"] = True

    _fixture_stub(monkeypatch, tmp_path, state)
    monkeypatch.setattr(app, "_run_sdlc_sprints_stream", lambda **kwargs: runner())

    async def run():
        response = await app.api_run_agile_sdlc_factory(_request(_body(
            _SESSION + "exhaust",
            mode="run_sprints",
            max_stories=1,
            max_steps=2,
        )))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    joined = "\n".join(events)
    assert "sprint-chunk-1" in joined
    assert "sprint-chunk-2" in joined
    assert "turn_done" in events[-1]
    assert state["sprint_closed"], "normal completion must drain the sprint runner"
    assert state["task_final"] == "complete"
    assert any(call[0] == "heartbeat" for call in state["leases"]), "the turn lease must be acquired"


def test_full_mode_drains_every_delegated_phase_source(monkeypatch, tmp_path):
    """A fully-consumed full-mode run drains every delegated phase source."""
    state = _base_state()

    async def runner(**kwargs):
        try:
            yield "sprint-chunk-1"
            yield "sprint-chunk-2"
        finally:
            state["sprint_closed"] = True

    _model_dispatch(monkeypatch, state)
    _fixture_stub(monkeypatch, tmp_path, state)
    monkeypatch.setattr(app, "_run_sdlc_sprints_stream", lambda **kwargs: runner())

    async def run():
        response = await app.api_run_agile_sdlc_factory(_request(_body(
            _SESSION + "boundary",
            mode="full",
            prompt="fixture product vision",
            lead_model="cloudtest::lead",
            critic_model="cloudtest::critic",
        )))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    final = events[-1]
    assert "turn_done" in final
    assert state["lead_closed"] and state["critic_closed"] and state["sprint_closed"], (
        "every delegated phase source must drain in a fully-consumed full-mode run"
    )
    assert state["task_final"] == "complete"
