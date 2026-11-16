from __future__ import annotations

import asyncio

import pytest

import app


_WORKSPACE_GOAL = "fixture goal"
_LEAD = "cloudtest::fixture-lead"
_CRITIC = "cloudtest::fixture-critic"

_STORY = {
    "id": "STORY-1",
    "title": "fixture story",
    "priority": "P0",
    "passes": False,
    "attempts": 0,
    "acceptance_criteria": ["criterion one"],
    "verification_cmd": "pytest -q fixture",
}


def _fixture_stub(monkeypatch, state: dict, stories: list[dict]) -> None:
    """Route every DB/global collaborator of the sprint runner in memory."""
    monkeypatch.setattr(
        app,
        "_read_sdlc_backlog",
        lambda sdlc_dir: [dict(story, acceptance_criteria=list(story.get("acceptance_criteria") or [])) for story in stories],
    )
    monkeypatch.setattr(app, "_read_sdlc_confined_text", lambda sdlc_dir, filename: "")
    monkeypatch.setattr(app, "_write_sdlc_confined_text", lambda *a, **k: state["notes"].append((a, k)))
    monkeypatch.setattr(app, "_write_sdlc_backlog", lambda sdlc_dir, goal, stories: state["backlog_writes"].append([dict(s) for s in stories]))
    monkeypatch.setattr(app, "build_pinned_opencontext_prompt", lambda: "")
    monkeypatch.setattr(app, "build_active_mcp_tools_prompt", lambda: "")
    monkeypatch.setattr(app, "get_harness_session_db", lambda sid, include_events=False: {
        "id": sid,
        "session_id": sid,
        "status": "running",
        "policy": "standard",
        "step_count": state["step_count"],
        "worktree_dir": state["wt_dir"],
    })
    monkeypatch.setattr(app, "_update_harness_session_state", lambda sid, **k: state.__setitem__("step_count", state["step_count"] + int(k.get("step_inc") or 0)))
    monkeypatch.setattr(app, "_append_harness_event", lambda *a, **k: state["events"].append((a, k)))
    monkeypatch.setattr(app, "capture_mission_authority_envelope", lambda *a, **k: None)
    monkeypatch.setattr(app, "tool_requires_approval", lambda *a, **k: False)
    monkeypatch.setattr(app, "_compute_git_diff", lambda target_dir: {"status": "clean", "diff": ""})

    async def _exec_tool(client, sess, tool_name, tool_args, **kwargs):
        state["tool_calls"].append((tool_name, tool_args.get("command") if isinstance(tool_args, dict) else None))
        return {"ok": True, "output": "1 passed", "elapsed_ms": 1}

    monkeypatch.setattr(app, "_execute_harness_tool", _exec_tool)


def _model_dispatch(monkeypatch, state: dict, worker_body, qa_body):
    """Stub `stream_any_model` with per-model synthetic sources that observe closure."""

    def dispatch(_client, model, *_args, **_kwargs):
        if model == _LEAD:
            async def worker():
                try:
                    async for item in worker_body():
                        yield item
                finally:
                    state["worker_closed"] = True

            return worker()
        if model == _CRITIC:
            async def critic():
                try:
                    async for item in qa_body():
                        yield item
                finally:
                    state["critic_closed"] = True

            return critic()
        raise AssertionError(f"unexpected model dispatch: {model}")

    monkeypatch.setattr(app, "stream_any_model", dispatch)


def _run_generator(state, sid="sess-lc08-sprints", max_steps=1):
    return app._run_sdlc_sprints_stream(
        object(),
        sid,
        state["wt_path"],
        state["sdlc_path"],
        _WORKSPACE_GOAL,
        _LEAD,
        _CRITIC,
        "standard",
        False,
        "",
        "",
        max_steps_per_story=max_steps,
    )


def _base_state(tmp_path) -> dict:
    state = {
        "step_count": 0,
        "events": [],
        "notes": [],
        "backlog_writes": [],
        "tool_calls": [],
        "worker_closed": False,
        "critic_closed": False,
        "wt_dir": None,
        "sdlc_dir": None,
        "wt_path": None,
        "sdlc_path": None,
    }
    wt = tmp_path / "worktree"
    wt.mkdir(parents=True, exist_ok=True)
    sdlc = wt / ".sdlc"
    sdlc.mkdir()
    state["wt_dir"] = str(wt)
    state["sdlc_dir"] = str(sdlc)
    state["wt_path"] = wt
    state["sdlc_path"] = sdlc
    return state


def test_consumer_close_mid_worker_closes_delegated_model_source(monkeypatch, tmp_path):
    """Closing the sprint iterator mid-worker must close the delegated worker source."""
    state = _base_state(tmp_path)

    async def worker_body():
        yield {"type": "content", "text": "partial worker output"}
        await asyncio.Event().wait()

    async def qa_body():
        yield {"type": "content", "text": "VERDICT: PASS\nall criteria met"}

    _fixture_stub(monkeypatch, state, [_STORY])
    _model_dispatch(monkeypatch, state, worker_body, qa_body)

    async def run():
        agen = _run_generator(state, sid="sess-lc08-worker-close")
        async for event in agen:
            if "step_content" in event:
                break
        await agen.aclose()
        assert state["worker_closed"], "closing the sprint iterator mid-worker must close the delegated worker source"

    asyncio.run(run())


def test_worker_error_after_content_propagates_and_closes_source(monkeypatch, tmp_path):
    """A worker-model failure after content must propagate with the source finalized."""
    state = _base_state(tmp_path)

    async def worker_body():
        yield {"type": "content", "text": "partial worker output"}
        raise RuntimeError("fixture worker failure")

    async def qa_body():
        yield {"type": "content", "text": "VERDICT: PASS\nall criteria met"}

    _fixture_stub(monkeypatch, state, [_STORY])
    _model_dispatch(monkeypatch, state, worker_body, qa_body)

    async def run():
        agen = _run_generator(state, sid="sess-lc08-worker-error")
        seen_content = False
        with pytest.raises(RuntimeError, match="fixture worker failure"):
            async for event in agen:
                if "step_content" in event:
                    seen_content = True
        assert seen_content
        assert state["worker_closed"], "a worker-model failure must not leave the delegated worker source open"

    asyncio.run(run())


def test_consumer_cancel_mid_critic_closes_delegated_qa_source(monkeypatch, tmp_path):
    """Cancelling the consumer while awaiting the independent QA source must close it."""
    state = _base_state(tmp_path)
    critic_entered = asyncio.Event()

    async def worker_body():
        yield {"type": "content", "text": "worker produced the change"}

    async def qa_body():
        critic_entered.set()
        await asyncio.Event().wait()
        yield {"type": "content", "text": "VERDICT: PASS"}

    _fixture_stub(monkeypatch, state, [_STORY])
    _model_dispatch(monkeypatch, state, worker_body, qa_body)

    async def run():
        agen = _run_generator(state, sid="sess-lc08-critic-cancel", max_steps=1)
        events = []
        async for event in agen:
            events.append(event)
            if "step_parsed" in event:
                break
        pending = asyncio.create_task(agen.__anext__())
        for _ in range(2000):
            await asyncio.sleep(0.001)
            if critic_entered.is_set():
                break
        assert critic_entered.is_set(), "the sprint runner must reach the independent QA step"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert state["critic_closed"], "cancellation mid-critic must close the delegated QA source"
        await agen.aclose()

    asyncio.run(run())


def test_sprint_story_pass_exhausts_and_closes_both_model_sources(monkeypatch, tmp_path):
    """Normal completion remains clean; both delegated sources end their frames."""
    state = _base_state(tmp_path)

    async def worker_body():
        yield {"type": "content", "text": "worker produced the change"}

    async def qa_body():
        yield {"type": "content", "text": "VERDICT: PASS\nevery criterion is supported by evidence"}

    _fixture_stub(monkeypatch, state, [_STORY])
    _model_dispatch(monkeypatch, state, worker_body, qa_body)

    async def run():
        agen = _run_generator(state, sid="sess-lc08-exhaustion", max_steps=1)
        return [event async for event in agen]

    events = asyncio.run(run())
    final = [event for event in events if "sdlc_story_done" in event]
    assert final, "the sprint runner must complete the story"
    assert '"passes": true' in final[-1]
    assert state["worker_closed"], "normal completion must drain the worker source"
    assert state["critic_closed"], "normal completion must drain the QA source"
    assert state["tool_calls"] == [("run_bash", "pytest -q fixture")]
    assert any(state["backlog_writes"]), "a passing story must be written back to the backlog"
    assert state["backlog_writes"][-1][0]["passes"] is True
