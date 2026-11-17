"""LC08: Harness turn shell (`api_run_harness_turn.event_gen`) delegated-source audit.

Offline, in-memory audit of who closes the per-step delegated model source
when the outer Harness SSE frame ends early. The per-step `stream_any_model`
call is now wrapped in `contextlib.aclosing`, in parity with the combo,
sprint-runner, factory and GSD audits. No GSD prompt, tool, approval,
checkpoint, budget, lease or error-text semantics change; no provider,
native engine or installed harness is exercised.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app

_SESSION = "sess-lc08-harness-"


async def _async_return(value):
    return value


def _base_state():
    return {
        "step_count": 0,
        "events": [],
        "leases": [],
        "lead_closed": False,
        "task_final": None,
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
    """Route every DB/global collaborator of the Harness turn shell in memory."""
    monkeypatch.setattr(app, "get_portable_handoff_store", lambda *a, **k: SimpleNamespace(
        get_latest_handoff=lambda *a2, **k2: None,
    ))
    monkeypatch.setattr(app, "_current_security_generation", lambda: None)
    monkeypatch.setattr(app, "_env_evidence_lessons_enabled", lambda: False)
    monkeypatch.setattr(app, "_parse_task_budget_spec", lambda *a, **k: None)
    monkeypatch.setattr(app, "_parse_task_verification_spec", lambda *a, **k: None)
    monkeypatch.setattr(app, "build_pinned_opencontext_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_active_mcp_tools_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_efficiency_mode_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_skills_context", lambda *a, **k: _async_return(("", [])))
    monkeypatch.setattr(app, "_compute_git_diff", lambda target_dir: {"status": "clean", "diff": ""})
    monkeypatch.setattr(app, "_collect_step_evidence_refs", lambda *a, **k: [])
    monkeypatch.setattr(app, "_load_harness_messages", lambda *a, **k: [])
    monkeypatch.setattr(app, "_save_harness_messages", lambda *a, **k: state["events"].append(("save_messages", a)))
    monkeypatch.setattr(
        app,
        "_workspace_snapshot_store",
        lambda: SimpleNamespace(
            capture_baseline=lambda *a, **k: SimpleNamespace(summary_dict=lambda: {"ok": True}),
        ),
    )

    def _lesson_store():
        return SimpleNamespace(
            retrieve_for_decision_boundary=lambda *a, **k: SimpleNamespace(
                lessons=[],
                rendered_prompt="",
                to_mapping=lambda: {"lessons": []},
            ),
            record_retrieval_outcome=lambda *a, **k: [],
        )

    monkeypatch.setattr(app, "get_evidence_lesson_store", _lesson_store)

    def _session_db(sid, include_events=False):
        return {
            "id": sid,
            "session_id": sid,
            "status": "running",
            "policy": "standard",
            "step_count": state["step_count"],
            "worktree_dir": str(tmp_path),
            "title": "fixture Harness session",
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
        lambda *a, **k: state["events"].append((a, k)) or {"event_id": f"ev-{len(state['events'])}"},
    )

    lease = SimpleNamespace(epoch_record=SimpleNamespace(epoch=7))

    verification_report = SimpleNamespace(
        verified=True,
        spec_sha256="",
        stagnation=None,
        acceptance_coverage={},
        failure_code=None,
        modified_files=[],
        source_revision="",
        to_dict=lambda: {"verified": True},
    )

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
            verify_task_completion=lambda *a, **k: verification_report,
            store=SimpleNamespace(get_task=lambda *a, **k: None),
        )
        return coord, lease, "worker-1", task_machine, None

    monkeypatch.setattr(app, "_acquire_turn_plan_and_lease_or_409", _acquire)
    monkeypatch.setattr(
        app,
        "get_task_state_machine",
        lambda: SimpleNamespace(
            store=SimpleNamespace(get_task=lambda *a, **k: None),
            get_verification_spec=lambda *a, **k: None,
        ),
    )


def _model_dispatch(monkeypatch, state, *, steps=None):
    """Dispatch per step: one lead-model source per queued call, in order."""
    queue = list(steps if steps is not None else [])

    def dispatch(_client, model, *_args, **_kwargs):
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
        "prompt": "fixture Harness goal: ship the thing",
        "max_steps": 1,
        "reasoning_mode": "low",
        "efficiency_mode": "ponytail",
    }
    body.update(extra)
    return body


def _content(text):
    return {"type": "content", "text": text}


def test_consumer_close_during_model_stream_closes_delegated_source(monkeypatch, tmp_path):
    """Client disconnect at the step_content frame must close the lead source."""
    state = _base_state()

    async def lead():
        try:
            yield _content("fixture step chunk")
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, steps=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "close-step")))
        gen = response.body_iterator
        async for event in gen:
            if "step_content" in event:
                break
        await gen.aclose()
        assert state["lead_closed"], "closing the Harness stream must close the delegated model source"

    asyncio.run(run())


def test_consumer_close_during_step_reasoning_closes_delegated_source(monkeypatch, tmp_path):
    """Client disconnect at the step_reasoning frame must close the lead source."""
    state = _base_state()

    async def lead():
        try:
            yield {"type": "reasoning", "text": "fixture reasoning"}
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, steps=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "close-reasoning")))
        gen = response.body_iterator
        async for event in gen:
            if "step_reasoning" in event:
                break
        await gen.aclose()
        assert state["lead_closed"], "closing the Harness stream must close the delegated model source"

    asyncio.run(run())


def test_abort_flag_break_closes_delegated_source(monkeypatch, tmp_path):
    """The abort-flag break inside the consume loop must still close the source."""
    state = _base_state()

    async def lead():
        try:
            # A concurrent /api/harness/abort lands while this source is producing.
            app._HARNESS_ABORT_FLAGS.add(_SESSION + "abort")
            yield _content("fixture step chunk")
            yield _content("fixture step chunk 2")
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, steps=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "abort")))
        gen = response.body_iterator
        chunks = [event async for event in gen]
        assert state["lead_closed"], "the abort break must finalize the delegated model source"
        return chunks

    events = asyncio.run(run())
    joined = "\n".join(events)
    assert "step_content" not in joined


def test_model_failure_yields_error_frame_and_finalizes_source(monkeypatch, tmp_path):
    """A model failure after partial content reaches the error frame; the source finalizes."""
    state = _base_state()

    async def lead():
        try:
            yield _content("partial step")
            raise RuntimeError("fixture harness model failure")
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, steps=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "fail-step")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    final = events[-1]
    assert "error" in final, final[:200]
    assert "fixture harness model failure" in final, final[:200]
    assert state["lead_closed"], "a model failure must finalize the delegated source"


def test_consumer_cancel_while_blocked_on_model_source_unwinds_and_closes(monkeypatch, tmp_path):
    """Cancellation while awaiting the model source unwinds and closes it."""
    state = _base_state()
    source_entered = asyncio.Event()

    async def lead():
        try:
            source_entered.set()
            yield _content("fixture step chunk")
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] = True

    _model_dispatch(monkeypatch, state, steps=[lead()])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "cancel-step")))
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
        assert source_entered.is_set(), "the Harness shell must reach the model-source await"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert state["lead_closed"], "cancellation mid-step must unwind the delegated model source"

    asyncio.run(run())


def test_full_turn_run_drains_delegated_source_and_completes_task(monkeypatch, tmp_path):
    """A fully-consumed turn drains the per-step source, emits turn_done and completes."""
    state = _base_state()

    _model_dispatch(
        monkeypatch,
        state,
        steps=[_model_source(state, "lead_closed", [_content("fixture step content")])],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "exhaust")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    joined = "\n".join(events)
    final = events[-1]
    assert "turn_done" in final, joined[:400]
    assert state["lead_closed"], "the delegated step source must drain in a fully-consumed run"
    assert state["task_final"] == "complete"
    assert any(call[0] == "heartbeat" for call in state["leases"]), "the turn lease must be acquired"
    assert any(e[0][1] == "turn/start" for e in state["events"])
    assert any(e[0][1] == "turn/end" for e in state["events"])
