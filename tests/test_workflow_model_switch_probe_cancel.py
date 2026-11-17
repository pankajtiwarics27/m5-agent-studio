"""LC08: what happens to the semantic-confirmation probe's delegated source.

`execute_conservative_model_switch_async._run_probe_stream` reads the target
model through a bare `async for` over `stream_any_model` on the high-risk
branch of a conservative switch. The five prior LC08 audits each proved an SSE
shell that abandoned a delegated source mid-frame and left it to interpreter
shutdown. **This owner does not have that defect**, and this module exists to
pin that measured fact rather than to claim a fix:

* the helper has no `yield` in its loop body, so there is no consumer that can
  stop reading mid-frame — the whole switch call either returns or raises;
* a probe failure or a cancellation while suspended on `__anext__` unwinds the
  generator frame, so its `finally` runs before the caller resumes.

The negative controls below are the point: a truncated or mismatched probe
transcript must never be accepted as confirmation, so "the source closed" can
never be mistaken for "the handoff was verified".

Everything here is offline. The harness store, DB, workspace, heavy-model
admission and every model source are substituted in memory or in `tmp_path`; no
database, network, subprocess or real model is touched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

import app as studio

AUTH_TOKEN = "t" * 40
_LEAD_FROM = "local-alpha-27b"
_CRITIC = "local-critic-21b"
_LEAD_TO = "local-beta-32b"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the switch's durable state and heavy-model admission."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store)
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio_test.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)

    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_heavy_model_admission(
        studio.model_admission.HeavyModelAdmission(
            studio.model_admission.HeavyModelLimits(
                memory_bytes=24 * 1024**3,
                request_bytes=18 * 1024**3,
                slots=1,
            )
        )
    )
    studio.init_db()
    yield ws
    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)


def _session(ws: Path) -> str:
    return studio.create_harness_session_db(
        title="probe audit session",
        workspace_dir=str(ws),
        use_worktree=False,
        policy="autonomous",
        lead_model=_LEAD_FROM,
        critic_model=_CRITIC,
    )["id"]


def _bundle_from_prompt(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Recover the authoritative bundle the real prompt builder embedded."""
    tail = messages[-1]["content"]
    return json.loads(tail.split("bundle:\n", 1)[1])


def _confirmation_text(bundle: dict[str, Any]) -> str:
    """Build an accurate confirmation for whatever bundle the probe was sent."""
    return "```json\n" + json.dumps(
        {
            "core_sha256": bundle["core_sha256"],
            "policy": bundle["policy"],
            "next_step": bundle["next_step"],
            "referenced_paths": bundle["referenced_paths"],
            "ack_no_kv_cache": True,
            "restated_objective": bundle["objective"],
        }
    ) + "\n```"


class _Probe:
    """A synthetic probe source that records its own finalization."""

    def __init__(self, state: dict[str, int], *, behaviour: str, accurate: bool = True) -> None:
        self.state = state
        self.behaviour = behaviour
        self.accurate = accurate

    async def _source(self, messages: list[dict[str, str]]) -> AsyncIterator[dict[str, Any]]:
        try:
            bundle = _bundle_from_prompt(messages)
            text = _confirmation_text(bundle) if self.accurate else '{"core_sha256": "wrong"}'
            if self.behaviour == "fail_after_partial":
                yield {"type": "content", "text": text[: len(text) // 2]}
                raise RuntimeError("fixture probe transport failure")
            yield {"type": "content", "text": text}
            if self.behaviour == "hang_after_partial":
                await asyncio.Event().wait()
        finally:
            self.state["closed"] += 1


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, int],
    *,
    behaviour: str,
    accurate: bool = True,
    retain: list[Any] | None = None,
):
    """Serve one synthetic probe source and count how many were handed out."""
    probe = _Probe(state, behaviour=behaviour, accurate=accurate)

    def _stream(_client: Any, _model: str, messages: list[dict[str, str]], **_kw: Any):
        state["calls"] += 1
        source = probe._source(messages)
        if retain is not None:
            retain.append(source)
        return source

    monkeypatch.setattr(studio, "stream_any_model", _stream)


def _new_state() -> dict[str, int]:
    return {"calls": 0, "closed": 0, "started": 0}


async def _switch(sid: str) -> dict[str, Any]:
    return await studio.execute_conservative_model_switch_async(
        None,
        sid,
        target_lead_model=_LEAD_TO,
        target_critic_model=_CRITIC,
        referenced_files=["src/module.py"],
        target_context_tokens=30,
        max_switch_attempts=2,
    )


def test_probe_failure_closes_source_and_fails_closed(env, monkeypatch):
    """A probe failure must finalize the source and never confirm the handoff."""
    state = _new_state()
    retain: list[Any] = []
    _dispatch(monkeypatch, state, behaviour="fail_after_partial", retain=retain)
    sid = _session(env)

    async def run():
        with pytest.raises(studio.routing_policy.HandoffRejectedError) as exc:
            await _switch(sid)
        # Observed while the loop is still alive, so shutdown finalization cannot
        # be credited for the close.
        assert state["calls"] == 1, "the probe must be dispatched exactly once"
        assert state["closed"] == 1, "a probe failure must finalize the delegated source"
        return exc.value

    err = asyncio.run(run())
    assert err.code == "handoff_semantic_confirmation_failed", err.code
    # Fail closed: the switch did not silently commit.
    assert studio.get_harness_session_db(sid)["lead_model"] == _LEAD_FROM
    assert studio.get_harness_session_db(sid)["status"] == "paused"


def test_cancellation_during_probe_closes_source(env, monkeypatch):
    """Cancelling the switch while suspended on the probe must close the source."""
    state = _new_state()
    retain: list[Any] = []
    _dispatch(monkeypatch, state, behaviour="hang_after_partial", retain=retain)
    sid = _session(env)

    async def run():
        task = asyncio.create_task(_switch(sid))
        # Wait for the probe to be entered rather than sleeping a fixed time.
        for _ in range(500):
            await asyncio.sleep(0.005)
            if state["calls"] == 1:
                break
        assert state["calls"] == 1, "the probe must be reached before the cancel"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["closed"] == 1, "cancellation must unwind the delegated source"

    asyncio.run(run())


def test_truncated_probe_transcript_does_not_confirm(env, monkeypatch):
    """An inaccurate probe answer must be rejected, not accepted as confirmation."""
    state = _new_state()
    _dispatch(monkeypatch, state, behaviour="complete", accurate=False)
    sid = _session(env)

    async def run():
        with pytest.raises(studio.routing_policy.HandoffRejectedError) as exc:
            await _switch(sid)
        assert state["closed"] == state["calls"] == 1
        return exc.value

    err = asyncio.run(run())
    assert err.code == "handoff_semantic_confirmation_failed"
    assert studio.get_harness_session_db(sid)["lead_model"] == _LEAD_FROM


def test_accurate_probe_confirms_and_source_drains(env, monkeypatch):
    """Positive control: the probe path still verifies and commits."""
    state = _new_state()
    _dispatch(monkeypatch, state, behaviour="complete", accurate=True)
    sid = _session(env)

    async def run():
        outcome = await _switch(sid)
        assert state["closed"] == state["calls"] == 1
        return outcome

    outcome = asyncio.run(run())
    handoff = outcome["handoff"]
    assert handoff["harness_extensions"]["semantic_confirmation"]["status"] == "verified"
    assert handoff["harness_extensions"]["semantic_confirmation"]["mode"] == "target_model_probe"
    assert studio.get_harness_session_db(sid)["lead_model"] == _LEAD_TO


def test_explicit_attestation_never_enters_the_probe_path(env, monkeypatch):
    """With an explicit attestation the prompt builder and probe are never reached.

    `build_semantic_confirmation_prompt` is only called on the probe branch, so
    patching it to fail makes the branch discriminator explicit: if the probe
    path were entered, this test would fail loudly instead of quietly passing.
    """
    sid = _session(env)
    calls = {"stream": 0}

    def _never(*_a: Any, **_k: Any):
        raise AssertionError("the probe path must not be entered when an attestation is supplied")

    monkeypatch.setattr(studio.routing_policy, "build_semantic_confirmation_prompt", _never)
    monkeypatch.setattr(
        studio,
        "stream_any_model",
        lambda *_a, **_k: calls.__setitem__("stream", calls["stream"] + 1),
    )

    async def run():
        # The supplied attestation is deliberately incomplete, so the switch must
        # still reject it. The assertion under test is *which branch ran*, not the
        # verdict: branch selection must not depend on whether the payload is valid.
        with pytest.raises(studio.routing_policy.HandoffRejectedError):
            await studio.execute_conservative_model_switch_async(
                None,
                sid,
                target_lead_model=_LEAD_TO,
                target_critic_model=_CRITIC,
                referenced_files=["src/module.py"],
                target_context_tokens=30,
                max_switch_attempts=2,
                semantic_confirmation='{"core_sha256": "supplied-by-operator"}',
            )

    asyncio.run(run())
    assert calls["stream"] == 0, "no probe source may be created on the attestation path"
