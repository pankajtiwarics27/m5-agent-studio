"""Explicit-load success needs durable native confirmation, not callback booleans."""

from __future__ import annotations

import asyncio

import pytest

import native_transition_driver as nt
import runtime_dispatch_boundary as rd
from test_native_transition_driver import fixture
from test_native_load_control import scheduler, call


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_completed_request_row_and_true_callback_are_not_native_confirmation(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:

            async def pretend(self):
                b.coord._call(
                    lambda conn: conn.execute(
                        "UPDATE requests SET status='completed' WHERE id=?",
                        (self.permit.intent.request_id,),
                    )
                )
                return True

            cls = (
                nt.OllamaLoadCompletion if backend == "ollama" else nt.LmLoadCompletion
            )
            monkeypatch.setattr(cls, "complete", pretend)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_receipt",
        "wrong_reference",
        "duplicate_receipt",
        "wrong_provider",
        "receipt_epoch",
        "receipt_generation",
        "slot_status",
        "slot_generation",
        "slot_epoch",
        "slot_id",
        "request_target",
        "request_deadline",
        "cancelled",
        "memory_peak",
        "memory_status",
    ],
)
def test_native_true_result_with_mismatched_durable_identity_cannot_confirm(
    tmp_path, monkeypatch, backend, fault
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            cls = (
                nt.OllamaLoadCompletion if backend == "ollama" else nt.LmLoadCompletion
            )
            original = cls.complete

            async def tampered(self):
                assert await original(self) is True

                def change(conn):
                    receipt = backend + "_load_verified"
                    if fault == "missing_receipt":
                        conn.execute("DELETE FROM journal WHERE event=?", (receipt,))
                    elif fault == "wrong_reference":
                        conn.execute(
                            "UPDATE journal SET reference='wrong-control-and-hold' WHERE event=?",
                            (receipt,),
                        )
                    elif fault == "duplicate_receipt":
                        conn.execute(
                            "INSERT INTO journal(domain,operation_id,event,reference,owner_epoch,generation) SELECT domain,operation_id,event,reference,owner_epoch,generation FROM journal WHERE event=?",
                            (receipt,),
                        )
                    elif fault == "wrong_provider":
                        conn.execute(
                            "UPDATE journal SET event='other_load_verified' WHERE event=?",
                            (receipt,),
                        )
                    elif fault.startswith("receipt_"):
                        conn.execute(
                            "UPDATE journal SET "
                            + (
                                "owner_epoch"
                                if fault.endswith("epoch")
                                else "generation"
                            )
                            + "=999 WHERE event=?",
                            (receipt,),
                        )
                    elif fault.startswith("slot_"):
                        column, value = {
                            "slot_status": ("status", "active"),
                            "slot_epoch": ("owner_epoch", 999),
                            "slot_generation": ("generation", 999),
                            "slot_id": ("id", "other-control"),
                        }[fault]
                        conn.execute(
                            "UPDATE leases SET " + column + "=? WHERE id=?",
                            (value, self.permit.intent.lease_id),
                        )
                    elif fault == "request_target":
                        conn.execute(
                            "UPDATE requests SET target='{}' WHERE id=?",
                            (self.permit.intent.request_id,),
                        )
                    elif fault == "request_deadline":
                        conn.execute(
                            "UPDATE requests SET deadline=49000 WHERE id=?",
                            (self.permit.intent.request_id,),
                        )
                    elif fault == "cancelled":
                        conn.execute(
                            "UPDATE requests SET cancel_requested=1 WHERE id=?",
                            (self.permit.intent.request_id,),
                        )
                    elif fault == "memory_peak":
                        conn.execute(
                            "UPDATE memory_reservations SET peak_bytes=peak_bytes+1 WHERE id=?",
                            (self.memory.lease_id,),
                        )
                    else:
                        conn.execute(
                            "UPDATE memory_reservations SET status='reserved' WHERE id=?",
                            (self.memory.lease_id,),
                        )

                b.coord._call(change)
                return True

            monkeypatch.setattr(cls, "complete", tampered)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_foreign_fifo_control_cannot_be_completed_or_poisoned_by_load_actor(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await scheduler(b).command(call(b)) == {"status": "confirmed"}
            b.wire.calls.clear()
            b.coord.enqueue_load(
                "foreign",
                "shared",
                b.q.profile.memory.target,
                deadline_ms=40000,
                now_ms=b.now,
            )
            original = b.coord.claim_next

            def wrong(domain, **kwargs):
                return original(domain, **dict(kwargs, request_id="foreign"))

            monkeypatch.setattr(b.coord, "claim_next", wrong)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            foreign = b.coord._call(
                lambda conn: conn.execute(
                    "SELECT status FROM requests WHERE id='foreign'"
                ).fetchone()
            )
            assert foreign["status"] == "admitted"
            assert (
                b.coord.snapshot("shared").state == "idle"
                and b.coord.snapshot("shared").active_requests == 1
            )
            assert (
                b.memory.view("shared").state == "ready"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert b.wire.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize(
    "successor", ["inference", "uncertain_inference", "unload", "reload"]
)
def test_historical_load_confirmation_survives_independent_successor_without_poisoning(
    tmp_path, monkeypatch, backend, successor
):
    from test_native_unload_control import scheduler as unload_scheduler
    from test_native_unload_steps import call as unload_call

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            cls = (
                nt.OllamaLoadCompletion if backend == "ollama" else nt.LmLoadCompletion
            )
            original = cls.complete
            advanced = False

            async def completed(self):
                nonlocal advanced
                result = await original(self)
                if advanced:
                    return result
                advanced = True
                if successor in ("inference", "uncertain_inference"):
                    b.coord.enqueue(
                        "successor",
                        "shared",
                        self.permit.intent.target,
                        deadline_ms=40000,
                        now_ms=b.now,
                    )
                    current = b.coord.claim_next(
                        "shared", request_id="successor", now_ms=b.now
                    )
                    if successor == "uncertain_inference":
                        assert b.coord.mark_request_uncertain(
                            current,
                            reference="fake-successor-interruption",
                            now_ms=b.now,
                        )
                        b.memory.mark_uncertain(
                            self.memory,
                            reference="fake-successor-interruption",
                            now_ms=b.now,
                        )
                else:
                    assert await unload_scheduler(b).command(unload_call(b)) == {
                        "status": "confirmed"
                    }
                    if successor == "reload":
                        assert await scheduler(b).command(call(b)) == {
                            "status": "confirmed"
                        }
                return result

            monkeypatch.setattr(cls, "complete", completed)
            execute = scheduler(b)
            assert await execute.command(call(b)) == {"status": "confirmed"}
            assert not execute._lock.locked()
            snapshot = b.coord.snapshot("shared")
            assert snapshot.state == (
                "reconciling" if successor == "uncertain_inference" else "idle"
            )
            assert snapshot.active_requests == (1 if "inference" in successor else 0)
            assert b.memory.view("shared").state == (
                "paused" if successor == "uncertain_inference" else "ready"
            )
            assert b.memory.view("shared").committed_bytes == (
                0 if successor == "unload" else 600
            )

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_native_receipt_write_failure_rolls_back_control_completion_and_retains_charge(
    tmp_path, monkeypatch, backend
):
    import sqlite3

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            original = b.coord._event

            def fail(conn, domain, event, *args, **kwargs):
                if event == backend + "_load_verified":
                    raise sqlite3.OperationalError("fake-private-storage-failure")
                return original(conn, domain, event, *args, **kwargs)

            monkeypatch.setattr(b.coord, "_event", fail)
            execute = scheduler(b)
            with pytest.raises(rd.RuntimeDispatchDenied) as error:
                await execute.command(call(b))
            assert "fake-private" not in str(error.value)

            def rows(conn):
                return (
                    conn.execute(
                        "SELECT status FROM requests WHERE purpose='load'"
                    ).fetchone()[0],
                    conn.execute("SELECT status FROM leases").fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM journal WHERE event IN ('load_finished',?)",
                        (backend + "_load_verified",),
                    ).fetchone()[0],
                )

            assert b.coord._call(rows) == ("admitted", "active", 0)
            assert b.coord.snapshot("shared").state == "reconciling"
            assert (
                b.memory.view("shared").state == "paused"
                and b.memory.view("shared").committed_bytes == 600
            )
            assert not execute._lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_generic_kernel_finish_is_not_native_profile_memory_confirmation(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            cls = (
                nt.OllamaLoadCompletion if backend == "ollama" else nt.LmLoadCompletion
            )

            async def generic(self):
                observed = await (
                    b.bridge.observe(b.q.runtime_id)
                    if backend == "ollama"
                    else b.bridge.observe()
                )
                evidence = await self.proof(observed)
                assert b.coord.finish_load(
                    self.permit.intent,
                    evidence,
                    reference=observed.reference,
                    now_ms=b.now,
                )
                return True

            monkeypatch.setattr(cls, "complete", generic)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_previous_native_control_fingerprint_cannot_be_replayed_for_new_control(
    tmp_path, monkeypatch, backend
):
    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            assert await scheduler(b).command(call(b)) == {"status": "confirmed"}
            old = b.coord._call(
                lambda conn: conn.execute(
                    "SELECT reference FROM journal WHERE event=?",
                    (backend + "_load_verified",),
                ).fetchone()[0]
            )
            cls = (
                nt.OllamaLoadCompletion if backend == "ollama" else nt.LmLoadCompletion
            )
            original = cls.complete

            async def replay(self):
                assert await original(self) is True
                b.coord._call(
                    lambda conn: conn.execute(
                        "UPDATE journal SET reference=? WHERE event=? AND operation_id=?",
                        (old, backend + "_load_verified", self.permit.intent.lease_id),
                    )
                )
                return True

            monkeypatch.setattr(cls, "complete", replay)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await scheduler(b).command(call(b))
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())
