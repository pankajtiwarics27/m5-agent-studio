from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app

_SESSION = "sess-lc08-gsd-"


async def _async_return(value):
    return value



def _base_state():
    return {
        "step_count": 0,
        "events": [],
        "leases": [],
        "lead_closed": False,
        "critic_closed": False,
        "wave2_closed": False,
        "task_final": None,
        "milestones": [],
        "transitions": [],
        "checkpoints": [],
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
    """Route every DB/global collaborator of the GSD fresh-wave shell in memory."""
    monkeypatch.setattr(app.workspace_quarantine, "find_active_quarantine", lambda *a, **k: None)
    monkeypatch.setattr(app, "_inspect_git_repo", lambda folder: {"is_git": False})
    monkeypatch.setattr(app, "_parse_task_budget_spec", lambda *a, **k: None)
    monkeypatch.setattr(app, "build_pinned_opencontext_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_active_mcp_tools_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_efficiency_mode_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_skills_context", lambda *a, **k: _async_return(("", [])))
    monkeypatch.setattr(app, "run_codegraph_tool", lambda *a, **k: "fixture codegraph summary")
    monkeypatch.setattr(app, "_compute_git_diff", lambda target_dir: {"status": "clean", "diff": ""})
    monkeypatch.setattr(app, "_collect_step_evidence_refs", lambda *a, **k: [])
    monkeypatch.setattr(app, "_load_harness_messages", lambda *a, **k: [])
    monkeypatch.setattr(app, "_save_harness_messages", lambda *a, **k: state["events"].append(("save_messages", a)))

    def _session_db(sid, include_events=False):
        return {
            "id": sid,
            "session_id": sid,
            "status": "running",
            "policy": "standard",
            "step_count": state["step_count"],
            "worktree_dir": str(tmp_path),
            "title": "fixture GSD session",
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

    lease = SimpleNamespace(epoch_record=SimpleNamespace(epoch=7))

    def _acquire(sess, **kwargs):
        coord = SimpleNamespace(
            heartbeat=lambda *a, **k: state["leases"].append(("heartbeat", ())),
            release=lambda *a, **k: state["leases"].append(("release", ())) or True,
        )
        task_machine = SimpleNamespace(
            ensure_milestone=lambda sid, key, title=None: SimpleNamespace(state="pending", task_id=f"ms-{key}"),
            transition_milestone=lambda sid, key, target, **k: state["transitions"].append((key, target)),
            check_safe_boundary=lambda *a, **k: "continue",
            budgets=SimpleNamespace(record_usage=lambda *a, **k: None),
            checkpoints=SimpleNamespace(
                save_checkpoint=lambda *a, **k: state["checkpoints"].append(k) or SimpleNamespace(checkpoint_id="ck-1")
            ),
            complete_task=lambda *a, **k: state.__setitem__("task_final", "complete"),
            pause_at_safe_boundary=lambda *a, **k: state.__setitem__("task_final", "pause"),
            cancel_task=lambda *a, **k: state.__setitem__("task_final", "cancel"),
            store=SimpleNamespace(get_task=lambda *a, **k: None),
        )
        return coord, lease, "worker-1", task_machine, None

    monkeypatch.setattr(app, "_acquire_turn_plan_and_lease_or_409", _acquire)


def _model_dispatch(monkeypatch, state, *, wave1=None, wave2=None, wave3=None):
    """Dispatch per phase: wave 1 + wave 2 use the lead model, wave 3 the critic.

    Per-call sources are consumed in order per model; wave 2 (lead) receives
    several calls in a multi-step scenario, so a queue is used.
    """
    queues = {
        "cloudtest::lead": list(wave1 if wave1 is not None else []) + list(wave2 if wave2 is not None else []),
        "cloudtest::critic": list(wave3 if wave3 is not None else []),
    }

    def dispatch(_client, model, *_args, **_kwargs):
        queue = queues.get(model)
        if not queue:
            raise AssertionError(f"unexpected model dispatch: {model}")
        source = queue.pop(0)
        if isinstance(source, Exception):
            raise source
        return source

    monkeypatch.setattr(app, "stream_any_model", dispatch)


def _model_source(state, role, chunks):
    async def source():
        try:
            for chunk in chunks:
                yield chunk
        finally:
            state[role] = True

    return source()


def _body(session_id, **extra):
    body = {
        "session_id": session_id,
        "prompt": "fixture GSD goal: ship the thing",
        "max_steps": 2,
        "reasoning_mode": "low",
        "efficiency_mode": "ponytail",
    }
    body.update(extra)
    return body


def _content(text):
    return {"type": "content", "text": text}


def test_consumer_close_during_wave_1_closes_delegated_source(monkeypatch, tmp_path):
    """Client disconnect at the Wave 1 step_content frame must close the lead source."""
    state = _base_state()

    async def lead():
        try:
            yield _content("fixture plan chunk")
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, wave1=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "close-w1")))
        gen = response.body_iterator
        async for event in gen:
            if "step_content" in event and "wave\": 1" in event:
                break
        await gen.aclose()
        assert state["lead_closed"], "closing the GSD stream must close the delegated wave 1 source"

    asyncio.run(run())


def test_consumer_close_during_wave_3_closes_delegated_source(monkeypatch, tmp_path):
    """Client disconnect at the Wave 3 step_content frame must close the critic source."""
    state = _base_state()

    async def critic():
        try:
            yield _content("fixture verdict chunk")
            await asyncio.Event().wait()
        finally:
            state["critic_closed"] = True

    _model_dispatch(
        monkeypatch,
        state,
        wave1=[_model_source(state, "lead_closed", [_content("fixture plan")])],
        wave2=[_model_source(state, "lead_closed", [_content("fixture execution")])],
        wave3=[critic()],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "close-w3")))
        gen = response.body_iterator
        async for event in gen:
            if "step_content" in event and "wave\": 3" in event:
                break
        await gen.aclose()
        assert state["critic_closed"], "closing the GSD stream must close the delegated wave 3 source"

    asyncio.run(run())


def test_wave_one_failure_yields_error_frame_and_finalizes_source(monkeypatch, tmp_path):
    """A wave 1 model failure surfaces as an error frame; the source finalizes."""
    state = _base_state()

    async def lead():
        try:
            yield _content("partial plan")
            raise RuntimeError("fixture wave1 failure")
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, wave1=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "fail-w1")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    final = events[-1]
    assert "error" in final, final[:200]
    assert "fixture wave1 failure" in final, final[:200]
    assert state["lead_closed"], "a wave-model failure must finalize the delegated source"


def test_consumer_cancel_during_wave_1_unwinds_and_closes_source(monkeypatch, tmp_path):
    """Cancellation while the wave 1 model source is blocked unwinds and closes it."""
    state = _base_state()
    source_entered = asyncio.Event()

    async def lead():
        try:
            source_entered.set()
            yield _content("fixture plan chunk")
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, wave1=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "cancel-w1")))
        gen = response.body_iterator
        while True:
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if "step_content" in event:
                break
        pending = asyncio.create_task(gen.__anext__())
        for _ in range(2000):
            await asyncio.sleep(0.001)
            if source_entered.is_set():
                break
        assert source_entered.is_set(), "the GSD shell must reach the wave 1 model await"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert state["lead_closed"], "cancellation mid-wave must unwind the delegated model source"

    asyncio.run(run())


def test_full_three_wave_run_drains_every_delegated_source(monkeypatch, tmp_path):
    """A fully-consumed run drains wave 1/2 (lead) and wave 3 (critic) sources."""
    state = _base_state()

    _model_dispatch(
        monkeypatch,
        state,
        wave1=[_model_source(state, "lead_closed", [_content("fixture plan")])],
        wave2=[_model_source(state, "lead_closed", [_content("fixture execution")])],
        wave3=[_model_source(state, "critic_closed", [_content("VERDICT: PASS fixture verdict")])],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "exhaust")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    joined = "\n".join(events)
    final = events[-1]
    assert joined.count("gsd_wave_done") == 3, joined[:400]
    assert "turn_done" in final
    assert state["lead_closed"] and state["critic_closed"], (
        "every delegated wave source must drain in a fully-consumed run"
    )
    assert state["task_final"] == "complete"
    assert any(call[0] == "heartbeat" for call in state["leases"]), "the turn lease must be acquired"
    assert ("wave_1_plan", "completed") in state["transitions"]
    assert ("wave_2_execute", "completed") in state["transitions"]
    assert ("wave_3_verify", "completed") in state["transitions"]


def test_consumer_close_during_wave_2_closes_delegated_source(monkeypatch, tmp_path):
    """Client disconnect at the Wave 2 step_content frame must close the lead source."""
    state = _base_state()

    async def lead():
        try:
            yield _content("fixture execution chunk")
            await asyncio.Event().wait()
        finally:
            state["wave2_closed"] = True

    _model_dispatch(
        monkeypatch,
        state,
        wave1=[_model_source(state, "lead_closed", [_content("fixture plan")])],
        wave2=[lead()],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_gsd_fresh_wave(_request(_body(_SESSION + "close-w2")))
        gen = response.body_iterator
        async for event in gen:
            if "step_content" in event and '"step": 2' in event:
                break
        await gen.aclose()
        assert state["wave2_closed"], "closing the GSD stream must close the delegated wave 2 source"

    asyncio.run(run())
