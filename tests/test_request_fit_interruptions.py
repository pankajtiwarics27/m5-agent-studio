"""Offline counter interruption accounting, not installed tokenizer qualification."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import model_lifecycle_coordinator as lc
import request_fit as rf
from test_request_fit_admission import setup, evaluate
from test_native_dispatch_executor import context


async def counter_context(tmp_path, backend):
    return setup(tmp_path) if backend == "generic" else await context(tmp_path, backend)


@pytest.mark.parametrize("backend", ["generic", "ollama", "lmstudio"])
@pytest.mark.parametrize(
    "callback", ["receipt", "error", "cancel", "observation-error"]
)
def test_late_counter_cleanup_cannot_pause_an_independent_successor(
    tmp_path, backend, callback
):
    async def run():
        b = await counter_context(tmp_path, backend)
        with b.coord:

            def advance():
                # Fake independent terminal completion, not counter proof of release.
                assert b.coord.finish_request(
                    b.request, reference="fake-independent-terminal", now_ms=b.now
                )
                b.coord.enqueue_load(
                    "successor",
                    "shared",
                    b.request.target,
                    deadline_ms=40000,
                    now_ms=b.now,
                )
                successor = b.coord.claim_next(
                    "shared", request_id="successor", now_ms=b.now
                )
                assert type(successor) is lc.LoadLease
                if callback == "observation-error":
                    b.sample_changes["invalid-key"] = True

            b.counter_hook = advance
            if callback == "error":
                b.count_error = OSError("PRIVATE-counter-payload")
            elif callback == "cancel":
                b.count_error = asyncio.CancelledError()
            with pytest.raises((rf.RequestFitDenied, asyncio.CancelledError)) as caught:
                await evaluate(b)
            assert "PRIVATE" not in str(caught.value)
            assert b.count_calls == 1
            assert b.coord.snapshot("shared").state == "idle"
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.memory.view("shared").state == "ready"
            assert b.memory.view("shared").committed_bytes == 600
            assert not b.service.authorizes(
                b.request,
                b.prepared.body_digest,
                b.prepared.profile.native_profile_digest,
            )
            row = b.coord._call(
                lambda conn: conn.execute(
                    "SELECT status,cancel_requested FROM requests WHERE id='successor'"
                ).fetchone()
            )
            assert row["status"] == "admitted" and not row["cancel_requested"]

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["generic", "ollama", "lmstudio"])
def test_counter_hold_storage_failure_rolls_back_intent_and_memory_together(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = await counter_context(tmp_path, backend)
        with b.coord:
            before = b.coord.snapshot("shared")
            journal = b.coord._call(
                lambda conn: conn.execute("SELECT count(*) FROM journal").fetchone()[0]
            )

            def fail(*args):
                raise sqlite3.OperationalError("private-ledger-error")

            monkeypatch.setattr(b.memory, "_change", fail)
            with pytest.raises(lc.CoordinatorError, match="journal_unavailable"):
                b.service._hold(b.permit, b.mem)
            assert b.coord.snapshot("shared") == before
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert (
                b.coord._call(
                    lambda conn: conn.execute(
                        "SELECT count(*) FROM journal"
                    ).fetchone()[0]
                )
                == journal
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["generic", "ollama", "lmstudio"])
@pytest.mark.parametrize("state", ["live", "cancelled", "expired"])
@pytest.mark.parametrize("error", ["timeout", "disconnect", "cancel"])
def test_current_counter_failure_retains_charge_slot_and_no_replay(
    tmp_path, backend, state, error
):
    async def run():
        b = await counter_context(tmp_path, backend)
        with b.coord:

            def interrupted():
                if state == "cancelled":
                    b.coord.cancel(b.request.request_id, now_ms=b.now)
                elif state == "expired":
                    b.now = 200000

            b.counter_hook = interrupted
            b.count_error = {
                "timeout": TimeoutError("PRIVATE"),
                "disconnect": OSError("PRIVATE"),
                "cancel": asyncio.CancelledError(),
            }[error]
            with pytest.raises((rf.RequestFitDenied, asyncio.CancelledError)) as caught:
                await evaluate(b)
            assert "PRIVATE" not in str(caught.value)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.memory.view("shared").state == "paused"
            assert b.memory.view("shared").committed_bytes == 600
            row = b.coord._call(
                lambda conn: conn.execute(
                    "SELECT status FROM leases WHERE id=?", (b.request.lease_id,)
                ).fetchone()
            )
            assert row["status"] == "active"
            assert not b.service.authorizes(
                b.request,
                b.prepared.body_digest,
                b.prepared.profile.native_profile_digest,
            )
            with pytest.raises(rf.RequestFitDenied):
                await evaluate(b)
            assert b.count_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["generic", "ollama", "lmstudio"])
@pytest.mark.parametrize("stale", [False, True])
def test_real_task_cancellation_after_count_dispatch_scopes_its_hold(
    tmp_path, backend, stale
):
    async def run():
        b = await counter_context(tmp_path, backend)
        with b.coord:
            entered = asyncio.Event()

            async def blocked(prepared, qualification):
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                b.service.evaluate(
                    b.permit,
                    b.prepared,
                    lease=b.mem,
                    qualification=b.counter_q,
                    counter=blocked,
                    observe=b.observe,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            if stale:
                assert b.coord.finish_request(
                    b.request, reference="fake-independent-terminal", now_ms=b.now
                )
                b.coord.enqueue_load(
                    "successor",
                    "shared",
                    b.request.target,
                    deadline_ms=40000,
                    now_ms=b.now,
                )
                assert (
                    type(
                        b.coord.claim_next(
                            "shared", request_id="successor", now_ms=b.now
                        )
                    )
                    is lc.LoadLease
                )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert b.memory.view("shared").committed_bytes == 600
            assert b.memory.view("shared").state == ("ready" if stale else "paused")
            assert b.coord.snapshot("shared").state == (
                "idle" if stale else "reconciling"
            )
            assert not b.service.authorizes(
                b.request,
                b.prepared.body_digest,
                b.prepared.profile.native_profile_digest,
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["generic", "ollama", "lmstudio"])
def test_fit_hold_is_one_fenced_write_with_no_producer(tmp_path, monkeypatch, backend):
    async def run():
        b = await counter_context(tmp_path, backend)
        with b.coord:
            writes = []
            original = b.coord._call

            def record(*args, **kwargs):
                writes.append(True)
                return original(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(b.coord, "_call", record)
                b.service._hold(b.permit, b.mem)
            assert len(writes) == 1
            assert b.count_calls == b.observe_calls == 0
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.memory.view("shared").state == "paused"

    asyncio.run(run())
