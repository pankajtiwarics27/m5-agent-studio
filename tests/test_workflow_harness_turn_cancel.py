"""LC08: who closes the Harness turn's delegated model source.

The Harness `Turn -> Step` shell (`api_run_harness_turn.event_gen`) read the lead
model through a bare `async for` over `stream_any_model`. Two ways of leaving
that loop left the delegated source suspended inside its own post-yield await:

* the SSE consumer going away at a `step_content` frame, which is what a browser
  disconnect looks like from the bridge's side; and
* the shell's own abort check `break`ing out of the loop body while the source
  was still live, which is the in-process path the Stop control takes.

In both cases only interpreter-shutdown finalization closed the source, long
after the response stream reported closed and after this suite's assertions run.
The first two cases below are the red/green proof: they fail on the unchanged
source and pass once the delegated call is inside `contextlib.aclosing`.

Everything here is offline. The session DB, message store, event log, worker
lease, task state machine, handoff/grant/lesson stores, snapshots, budget
recorder and tool executor are substituted in memory; the model sources are
synthetic async iterators that record their own closure. No database,
subprocess, network or workspace content is touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app

_SESSION = "sess-lc08-harness-"
_LEAD = "cloudtest::lead"


async def _async_return(value):
    return value


def _base_state(**overrides):
    state = {
        "step_count": 0,
        "events": [],
        "leases": [],
        "saves": [],
        "baselines": 0,
        "lead_closed": 0,
        "tools": [],
        "checkpoints": [],
        "usage": [],
        "task_final": None,
        "session_status": [],
        "verification": SimpleNamespace(
            verified=True,
            failure_code=None,
            stagnation=None,
            spec_sha256=None,
            acceptance_coverage=(),
            modified_files=(),
            source_revision="fixture-rev",
            to_dict=lambda: {"verified": True, "spec_sha256": None},
            to_mapping=lambda: {"verified": True},
        ),
        "aborted_during_stream": False,
        "retained": [],
    }
    state.update(overrides)
    return state


def _request(body):
    async def payload():
        return body

    return SimpleNamespace(
        json=payload,
        app=SimpleNamespace(state=SimpleNamespace(http=object())),
    )


def _session(sid, tmp_path, state):
    return {
        "id": sid,
        "session_id": sid,
        "status": "running",
        "policy": "standard",
        "step_count": state["step_count"],
        "worktree_dir": str(tmp_path),
        "workspace_dir": str(tmp_path),
        "title": "fixture harness turn",
        "lead_model": _LEAD,
        "critic_model": "cloudtest::critic",
    }


def _fixture_stub(monkeypatch, tmp_path, state):
    """Route every DB/global/network collaborator of the Harness turn in memory."""
    monkeypatch.setattr(
        app, "get_harness_session_db", lambda sid, include_events=False: _session(sid, tmp_path, state)
    )
    monkeypatch.setattr(app, "create_harness_session_db", lambda **k: _session(k.get("id") or f"{_SESSION}new", tmp_path, state))
    monkeypatch.setattr(app, "_set_harness_session_policy", lambda *a, **k: None)
    monkeypatch.setattr(
        app,
        "_update_harness_session_state",
        lambda sid, **k: state["session_status"].append(dict(k)),
    )
    monkeypatch.setattr(app, "_load_harness_messages", lambda *a, **k: [])
    monkeypatch.setattr(app, "_save_harness_messages", lambda sid, msgs: state["saves"].append(list(msgs)))
    monkeypatch.setattr(app, "_append_harness_event", lambda *a, **k: state["events"].append((a, k)) or {"id": len(state["events"])})
    monkeypatch.setattr(app, "_safe_append_harness_event", lambda *a, **k: state["events"].append((a, k)))
    monkeypatch.setattr(app, "get_portable_handoff_store", lambda: SimpleNamespace(get_latest_handoff=lambda *a, **k: None))
    monkeypatch.setattr(app, "get_scoped_grant_store", lambda: SimpleNamespace(bump_session_policy_revision=lambda *a, **k: (1, ())))
    monkeypatch.setattr(
        app,
        "get_evidence_lesson_store",
        lambda: SimpleNamespace(
            retrieve_for_decision_boundary=lambda **k: SimpleNamespace(lessons=(), rendered_prompt=""),
            record_retrieval_outcome=lambda *a, **k: (),
            promote_lesson=lambda **k: None,
        ),
    )
    monkeypatch.setattr(app, "build_skills_context", lambda *a, **k: _async_return(("", [])))
    monkeypatch.setattr(app, "build_active_mcp_tools_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_pinned_opencontext_prompt", lambda *a, **k: "")
    monkeypatch.setattr(app, "build_efficiency_mode_prompt", lambda *a, **k: "")
    monkeypatch.setattr(
        app,
        "_workspace_snapshot_store",
        lambda: SimpleNamespace(
            capture_baseline=lambda *a, **k: state.__setitem__("baselines", state["baselines"] + 1)
            or SimpleNamespace(summary_dict=lambda: {"snapshot": "fixture"})
        ),
    )
    monkeypatch.setattr(app, "_compute_git_diff", lambda target_dir: {"status": "clean", "diff": ""})
    monkeypatch.setattr(app, "_collect_step_evidence_refs", lambda *a, **k: [])
    monkeypatch.setattr(app, "capture_mission_authority_envelope", lambda *a, **k: None)
    monkeypatch.setattr(app, "tool_requires_approval", lambda *a, **k: False)
    monkeypatch.setattr(
        app, "sandbox_tool_output_if_large", lambda sid, name, out, **k: (out, {"sandboxed": False})
    )
    monkeypatch.setattr(app, "is_local_omlx_model", lambda spec: False)
    monkeypatch.setattr(app, "record_perf", lambda *a, **k: None)
    monkeypatch.setattr(app, "_current_security_generation", lambda: None)

    async def _exec_tool(client, sess, name, args, **kwargs):
        state["tools"].append((name, args))
        return {"ok": True, "output": "fixture tool output", "elapsed_ms": 1}

    monkeypatch.setattr(app, "_execute_harness_tool", _exec_tool)

    lease = SimpleNamespace(epoch_record=SimpleNamespace(epoch=11))

    def _acquire(sess, **kwargs):
        coord = SimpleNamespace(
            heartbeat=lambda *a, **k: state["leases"].append("heartbeat"),
            release=lambda *a, **k: state["leases"].append(("release", k.get("next_state"))),
        )
        task_machine = SimpleNamespace(
            check_safe_boundary=lambda *a, **k: "cancel" if state["aborted_during_stream"] else "continue",
            budgets=SimpleNamespace(record_usage=lambda *a, **k: state["usage"].append(k)),
            checkpoints=SimpleNamespace(
                save_checkpoint=lambda *a, **k: state["checkpoints"].append(k) or SimpleNamespace(checkpoint_id="ck-1"),
                load_latest_checkpoint=lambda *a, **k: None,
            ),
            complete_task=lambda *a, **k: state.__setitem__("task_final", "complete"),
            pause_at_safe_boundary=lambda *a, **k: state.__setitem__("task_final", "pause"),
            cancel_task=lambda *a, **k: state.__setitem__("task_final", "cancel"),
            verify_task_completion=lambda *a, **k: state["verification"],
            get_verification_spec=lambda *a, **k: None,
            set_verification_spec=lambda *a, **k: None,
            store=SimpleNamespace(get_task=lambda *a, **k: SimpleNamespace(state="running")),
        )
        return coord, lease, "worker-1", task_machine, None

    monkeypatch.setattr(app, "_acquire_turn_plan_and_lease_or_409", _acquire)
    monkeypatch.setattr(
        app,
        "get_task_state_machine",
        lambda: SimpleNamespace(
            get_verification_spec=lambda *a, **k: None,
            store=SimpleNamespace(get_task=lambda *a, **k: SimpleNamespace(state="running")),
        ),
    )


def _dispatch(monkeypatch, sources, *, retain=None):
    """Serve one synthetic source per `stream_any_model` call, in order.

    `retain` keeps a strong reference to every source handed to the shell, so a
    red result cannot be masked by garbage-collection finalization of an
    abandoned async generator: an unclosed source stays observable.
    """
    queue = list(sources)

    def _stream(_client, model, *_args, **_kwargs):
        assert model == _LEAD, f"unexpected model dispatch: {model}"
        if not queue:
            raise AssertionError("unexpected extra model dispatch")
        source = queue.pop(0)
        if isinstance(source, Exception):
            raise source
        if retain is not None:
            retain.append(source)
        return source

    monkeypatch.setattr(app, "stream_any_model", _stream)


def _blocking_source(state, chunks, *, close_key="lead_closed"):
    """A source that emits `chunks` then blocks forever, recording its own close."""

    async def source():
        try:
            for chunk in chunks:
                yield chunk
            await asyncio.Event().wait()
        finally:
            state[close_key] += 1

    return source()


def _draining_source(state, chunks, *, close_key="lead_closed"):
    async def source():
        try:
            for chunk in chunks:
                yield chunk
        finally:
            state[close_key] += 1

    return source()


def _content(text):
    return {"type": "content", "text": text}


def _body(session_id, **extra):
    body = {
        "session_id": session_id,
        "prompt": "fixture harness goal",
        "max_steps": 2,
        "reasoning_mode": "low",
        "efficiency_mode": "ponytail",
    }
    body.update(extra)
    return body


def _tool_call_body(session_id, **extra):
    return _body(
        session_id,
        **{
            "verification": {"commands": ["true"], "mode": "commands_only"},
            **extra,
        },
    )


# --------------------------------------------------------------------------
# Red/green proofs
# --------------------------------------------------------------------------


def test_consumer_close_during_step_closes_delegated_source(monkeypatch, tmp_path):
    """A client disconnect at `step_content` must close the delegated source."""
    state = _base_state()
    _dispatch(
        monkeypatch,
        [_blocking_source(state, [_content("fixture step chunk")])],
        retain=state["retained"],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "close-step")))
        gen = response.body_iterator
        async for event in gen:
            if "step_content" in event:
                break
        await asyncio.wait_for(gen.aclose(), timeout=5.0)
        assert state["lead_closed"] == 1, (
            "closing the Harness stream must close the delegated model source "
            "before the consumer's aclose() returns"
        )

    asyncio.run(run())


def test_abort_break_closes_delegated_source(monkeypatch, tmp_path):
    """The shell's own abort `break` must close the source it stops reading."""
    state = _base_state()
    sid = _SESSION + "abort-break"

    async def source():
        try:
            yield _content("first chunk turns the abort flag on")
            # The in-process Stop path: the shell's own abort set is updated while
            # the step is streaming, so its next loop-body check breaks.
            app._HARNESS_ABORT_FLAGS.add(sid)
            state["aborted_during_stream"] = True
            yield _content("second chunk is abandoned by the break")
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] += 1

    _dispatch(monkeypatch, [source()], retain=state["retained"])
    _fixture_stub(monkeypatch, tmp_path, state)
    app._HARNESS_ABORT_FLAGS.discard(sid)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(sid, max_steps=3)))
        gen = response.body_iterator
        seen: list[str] = []
        try:
            # Drain until the shell has moved past the abandoned step, i.e. until it
            # emits a frame that comes *after* the `break` left the model loop.
            while True:
                event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
                seen.append(event)
                name = event.split("\n", 1)[0]
                if "cancelled" in name or "paused" in name or "turn_done" in name:
                    break
            # Asserted inside the running loop, so the result cannot be attributed to
            # `asyncio.run`'s shutdown-time async-generator finalization.
            assert state["lead_closed"] == 1, (
                "the shell breaking out of the model loop must close the delegated "
                "source before the turn continues"
            )
        finally:
            await asyncio.wait_for(gen.aclose(), timeout=5.0)
        return seen

    try:
        events = asyncio.run(run())
    finally:
        app._HARNESS_ABORT_FLAGS.discard(sid)

    joined = "\n".join(events)
    assert "step_content" in joined, joined[:400]
    assert state["task_final"] in {"cancel", "pause"}, state["task_final"]


# --------------------------------------------------------------------------
# Regression coverage around the same loop
# --------------------------------------------------------------------------


def test_step_source_failure_yields_error_frame_and_closes_source(monkeypatch, tmp_path):
    state = _base_state()

    async def source():
        try:
            yield _content("partial step")
            raise RuntimeError("fixture model failure")
        finally:
            state["lead_closed"] += 1

    _dispatch(monkeypatch, [source()], retain=state["retained"])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "fail-step")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    assert "error" in events[-1], events[-1][:300]
    assert "fixture model failure" in events[-1], events[-1][:300]
    assert state["lead_closed"] == 1, "a model failure must finalize the delegated source"


def test_cancellation_while_awaiting_source_unwinds_and_closes(monkeypatch, tmp_path):
    state = _base_state()
    entered = asyncio.Event()

    async def source():
        try:
            yield _content("fixture chunk")
            entered.set()
            await asyncio.Event().wait()
        finally:
            state["lead_closed"] += 1

    _dispatch(monkeypatch, [source()], retain=state["retained"])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "cancel-step")))
        gen = response.body_iterator
        while True:
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if "step_content" in event:
                break
        pending = asyncio.create_task(gen.__anext__())
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert state["lead_closed"] == 1, "cancellation mid-step must unwind the delegated source"
        assert state["leases"], "the turn lease must be acquired"

    asyncio.run(run())


def test_fully_consumed_step_drains_source_and_completes(monkeypatch, tmp_path):
    state = _base_state()
    _dispatch(
        monkeypatch,
        [_draining_source(state, [_content("fixture answer")])],
        retain=state["retained"],
    )
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "drain")))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    joined = "\n".join(events)
    assert "step_assistant_done" in joined, joined[:500]
    assert "turn_done" in events[-1], events[-1][:300]
    assert state["lead_closed"] == 1
    assert state["task_final"] == "complete", state["task_final"]


def test_tool_step_drains_source_before_executing_the_tool(monkeypatch, tmp_path):
    """The wrap must not move a tool effect ahead of the model's final frame."""
    state = _base_state()

    async def source():
        try:
            yield _content(
                "<tool_call>{\"name\": \"run_command\", \"arguments\": {\"command\": \"true\"}}</tool_call>"
            )
        finally:
            state["lead_closed"] += 1
            state.setdefault("closed_before_tool", not state["tools"])

    _dispatch(monkeypatch, [source()], retain=state["retained"])
    _fixture_stub(monkeypatch, tmp_path, state)

    async def run():
        response = await app.api_run_harness_turn(_request(_body(_SESSION + "tool-step", max_steps=1)))
        return [event async for event in response.body_iterator]

    events = asyncio.run(run())
    joined = "\n".join(events)
    assert "tool_call" in joined and "tool_result" in joined, joined[:600]
    assert state["lead_closed"] == 1
    assert state["tools"] == [("run_command", {"command": "true"})], state["tools"]
    assert state["closed_before_tool"] is True, (
        "the delegated source must be drained before any tool effect runs"
    )
