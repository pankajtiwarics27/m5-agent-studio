"""LC08 explicit-load intent foundation; fake fleet, not a native control consumer."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import fields, replace
from concurrent.futures import ThreadPoolExecutor

import pytest

import model_lifecycle_coordinator as lc
import runtime_dispatch_boundary as rd
from local_runtime_lifecycle import ObservationStamp
from test_model_lifecycle_coordinator import A, B, opened, enqueue, reserve

pytestmark = pytest.mark.skipif(
    __import__("os").name != "posix", reason="POSIX owner locking"
)


def copy_as(cls, lease):
    return cls(*(getattr(lease, f.name) for f in fields(cls)))


def load_reservation(
    coord, fleet, *, request="explicit-a", target=A, now=1001, deadline=100000
):
    assert (
        coord.enqueue_load(request, "shared", target, deadline_ms=deadline, now_ms=now)
        == "queued"
    )
    for _ in range(5):
        item = coord.claim_next("shared", now_ms=now, request_id=request)
        if isinstance(item, lc.LoadLease):
            return item
        assert isinstance(item, lc.TransitionStep)
        fleet.apply(coord, item, now)
    raise AssertionError("no bounded load admission")


def status(coord, request):
    with sqlite3.connect(coord.database) as conn:
        return conn.execute(
            "SELECT status,purpose FROM requests WHERE id=?", (request,)
        ).fetchone()


def test_cold_load_uses_distinct_slot_and_scoped_completion_without_inference(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        lease = load_reservation(coord, fleet)
        assert not isinstance(lease, lc.RequestLease)
        assert status(coord, lease.request_id) == ("admitted", "load")
        assert fleet.trace == [("load", "model-a"), ("verify", "model-a")]
        assert coord.evidence_scope("shared") == lc.EvidenceScope(
            1, lease.generation, lease.lease_id, "load_control"
        )
        assert not coord.finish_request(
            copy_as(lc.RequestLease, lease), reference="no-fake-inference", now_ms=1002
        )
        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            coord.finish_load(lease, None, reference="ack-not-proof", now_ms=1002)
        assert coord.snapshot("shared").active_requests == 1
        assert coord.finish_load(
            lease,
            fleet.evidence(coord, 1002),
            reference="fake-native-proof",
            now_ms=1002,
        )
        assert status(coord, lease.request_id) == ("completed", "load")
        assert coord.snapshot("shared").active_requests == 0
        assert coord.snapshot("shared").resident == A
        assert not coord.finish_load(lease, None, reference="no-replay", now_ms=1002)
        with sqlite3.connect(coord.database) as conn:
            events = [r[0] for r in conn.execute("SELECT event FROM journal")]
        assert "load_finished" in events and "request_finished" not in events


def test_warm_load_still_requires_fresh_proof_and_does_not_load_again(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        first = load_reservation(coord, fleet)
        evidence = fleet.evidence(coord, 1002)
        assert coord.finish_load(first, evidence, reference="first-proof", now_ms=1002)
        second = load_reservation(coord, fleet, request="explicit-second", now=1003)
        assert first.instance_id == second.instance_id
        assert first.lease_id != second.lease_id
        with pytest.raises(lc.CoordinatorError, match="evidence_scope_mismatch"):
            coord.finish_load(
                second,
                replace(evidence, now=1.003),
                reference="cannot-reuse-first-proof",
                now_ms=1003,
            )
        assert coord.finish_load(
            second,
            fleet.evidence(coord, 1003),
            reference="fresh-second-proof",
            now_ms=1003,
        )
        assert fleet.trace == [("load", "model-a"), ("verify", "model-a")]


@pytest.mark.parametrize("first_kind", ["load", "inference"])
def test_mixed_fifo_cannot_skip_or_overlap_control_and_inference(tmp_path, first_kind):
    coord, fleet = opened(tmp_path)
    with coord:
        if first_kind == "load":
            first = load_reservation(coord, fleet)
            enqueue(coord, "second", B, now=1002)
        else:
            enqueue(coord, "first")
            first = reserve(coord, fleet)
            coord.enqueue_load("second", "shared", B, deadline_ms=100000, now_ms=1002)
        assert coord.claim_next("shared", now_ms=1002, request_id="second") is None
        if first_kind == "load":
            assert coord.finish_load(
                first,
                fleet.evidence(coord, 1002),
                reference="control-terminal",
                now_ms=1002,
            )
        else:
            assert coord.finish_request(
                first, reference="inference-terminal", now_ms=1002
            )
        for _ in range(5):
            next_item = coord.claim_next("shared", now_ms=1003, request_id="second")
            if isinstance(next_item, lc.TransitionStep):
                fleet.apply(coord, next_item, 1003)
            else:
                break
        assert type(next_item) is (
            lc.RequestLease if first_kind == "load" else lc.LoadLease
        )
        assert next_item.target == B


@pytest.mark.parametrize("kind", ["load", "inference"])
def test_request_identity_includes_durable_purpose(tmp_path, kind):
    coord, _ = opened(tmp_path)
    with coord:
        enqueue_first = coord.enqueue_load if kind == "load" else coord.enqueue
        enqueue_other = coord.enqueue if kind == "load" else coord.enqueue_load
        assert (
            enqueue_first("same", "shared", A, deadline_ms=10000, now_ms=1000)
            == "queued"
        )
        assert (
            enqueue_first("same", "shared", A, deadline_ms=10000, now_ms=1001)
            == "queued"
        )
        with pytest.raises(lc.CoordinatorError, match="idempotency_conflict"):
            enqueue_other("same", "shared", A, deadline_ms=10000, now_ms=1001)
        assert coord.snapshot("shared").queued == 1


@pytest.mark.parametrize(
    "change",
    [
        "ack",
        "missing_runtime",
        "wrong_instance",
        "wrong_profile",
        "unowned",
        "not_terminal",
        "no_headroom",
        "busy",
        "stale",
        "wrong_clock",
        "wrong_scope",
    ],
)
def test_load_completion_refuses_incomplete_or_mismatched_proof(tmp_path, change):
    coord, fleet = opened(tmp_path)
    with coord:
        lease = load_reservation(coord, fleet)
        evidence = fleet.evidence(coord, 1002)
        if change == "ack":
            evidence = {"status": "ok"}
        elif change == "missing_runtime":
            evidence = replace(evidence, inventories=evidence.inventories[1:])
        elif change in ("wrong_instance", "wrong_profile", "busy"):
            inventory = evidence.inventories[0]
            changes = (
                {"instance_id": "not-the-owned-instance"}
                if change == "wrong_instance"
                else {"load_profile_fingerprint": "other-profile"}
                if change == "wrong_profile"
                else {"active_requests": 1}
            )
            instance = replace(inventory.instances[0], **changes)
            evidence = replace(
                evidence,
                inventories=(replace(inventory, instances=(instance,)),)
                + evidence.inventories[1:],
            )
            if change == "wrong_instance":
                evidence = replace(
                    evidence, owned_instance_ids=((A.runtime_id, instance.instance_id),)
                )
        elif change == "unowned":
            evidence = replace(evidence, owned_instance_ids=())
        elif change == "not_terminal":
            evidence = replace(evidence, operations_terminal=False)
        elif change == "no_headroom":
            evidence = replace(evidence, headroom_verified=False)
        elif change == "stale":
            inventory = replace(
                evidence.inventories[0], stamp=ObservationStamp(0, evidence.clock_id)
            )
            evidence = replace(
                evidence, inventories=(inventory,) + evidence.inventories[1:]
            )
            # Freshness limit is five seconds, so make this timestamp genuinely stale.
            evidence = replace(evidence, now=7.0)
        elif change == "wrong_clock":
            evidence = replace(evidence, clock_id="other-observer")
        elif change == "wrong_scope":
            evidence = replace(
                evidence, scope=replace(evidence.scope, operation_id="other-control")
            )
        now = 7000 if change == "stale" else 1002
        with pytest.raises(lc.CoordinatorError):
            coord.finish_load(lease, evidence, reference="not-enough", now_ms=now)
        assert status(coord, lease.request_id)[0] == "admitted"
        assert coord.snapshot("shared").active_requests == 1
        enqueue(coord, "blocked", B, now=now)
        assert coord.claim_next("shared", now_ms=now, request_id="blocked") is None


@pytest.mark.parametrize("condition", ["cancelled", "expired"])
def test_cancel_or_deadline_is_not_native_terminality_or_completed_control(
    tmp_path, condition
):
    coord, fleet = opened(tmp_path)
    with coord:
        lease = load_reservation(coord, fleet, deadline=1003)
        if condition == "cancelled":
            assert coord.cancel(lease.request_id, now_ms=1002) == "pending_terminal"
        assert coord.claim_next("shared", now_ms=1004) is None
        assert status(coord, lease.request_id)[0] == "admitted"
        assert coord.finish_load(
            lease,
            fleet.evidence(coord, 1004),
            reference="independent-terminal",
            now_ms=1004,
        )
        assert status(coord, lease.request_id)[0] == condition
        assert coord.snapshot("shared").resident == A


def test_cancel_queued_load_does_not_cancel_dispatched_native_load(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        coord.enqueue_load("cancel", "shared", A, deadline_ms=10000, now_ms=1000)
        load = coord.claim_next("shared", now_ms=1001)
        assert coord.cancel("cancel", now_ms=1002) == "cancelled"
        enqueue(coord, "next", B, now=1002)
        assert coord.claim_next("shared", now_ms=1002, request_id="next") is None
        assert coord.snapshot("shared").operation_stage == "load"
        fleet.apply(coord, load, 1003)
        verify = coord.claim_next("shared", now_ms=1003)
        fleet.apply(coord, verify, 1003)
        next_step = coord.claim_next("shared", now_ms=1004, request_id="next")
        assert next_step.kind == "drain" and next_step.source == A


def test_interrupted_load_holds_until_reconciliation_not_caller_finish(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        lease = load_reservation(coord, fleet)
        assert not coord.mark_request_uncertain(
            copy_as(lc.RequestLease, lease), reference="wrong-purpose", now_ms=1002
        )
        assert coord.mark_load_uncertain(
            lease, reference="control-disconnect", now_ms=1002
        )
        assert coord.snapshot("shared").state == "reconciling"
        assert not coord.finish_load(
            lease, fleet.evidence(coord, 1002), reference="no-bypass", now_ms=1002
        )
        assert coord.snapshot("shared").active_requests == 1


@pytest.mark.parametrize("admitted", [False, True])
def test_restart_retains_control_purpose_and_fences_old_load_lease(tmp_path, admitted):
    coord, fleet = opened(tmp_path)
    if admitted:
        old = load_reservation(coord, fleet)
    else:
        coord.enqueue_load("pending", "shared", A, deadline_ms=100000, now_ms=1001)
    database = coord.database
    coord.close()
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert new.claim_next("shared", now_ms=1002) is None
        with sqlite3.connect(database) as conn:
            result = conn.execute("SELECT status,purpose FROM requests").fetchone()
        assert result == ("interrupted" if admitted else "queued", "load")
        if admitted:
            assert not new.finish_load(
                old, fleet.evidence(new, 1002), reference="old-owner", now_ms=1002
            )
            assert not new.mark_load_uncertain(old, reference="old-owner", now_ms=1002)


def test_v2_populated_journal_is_preserved_not_silently_migrated(tmp_path):
    coord, _ = opened(tmp_path)
    enqueue(coord, "historical")
    database = coord.database
    coord.close()
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE requests DROP COLUMN purpose")
        conn.execute("PRAGMA user_version=2")
    before = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError, match="journal_schema_unsupported"):
        lc.LifecycleCoordinator(tmp_path / "owner")
    assert hashlib.sha256(database.read_bytes()).digest() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT id,status FROM requests").fetchone() == (
            "historical",
            "queued",
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("entrypoint", ["executor", "native_infer"])
def test_forged_inference_wrapper_cannot_count_or_send_using_control_slot(
    tmp_path, backend, entrypoint
):
    from test_native_transition_driver import fixture, admission, loaded
    from test_native_inference_scheduler import warm, collect
    from native_dispatch_executor import NativeDispatchExecutor

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            queued = b.transition.enqueue(
                admission(b), "initial", "shared", deadline_ms=40000
            )
            first, mem = await loaded(b, queued)
            assert b.coord.finish_request(
                first, reference="fixture-no-native-inference", now_ms=b.now
            )
            b.coord.enqueue_load(
                "explicit-load", "shared", first.target, deadline_ms=40000, now_ms=b.now
            )
            control = b.coord.claim_next("shared", now_ms=b.now)
            assert type(control) is lc.LoadLease
            forged = copy_as(lc.RequestLease, control)
            native_job = warm(b, admission(b), forged, mem)
            b.wire.calls.clear()
            if entrypoint == "executor":
                with pytest.raises(rd.RuntimeDispatchDenied):
                    await collect(
                        NativeDispatchExecutor(lambda _: native_job).stream(
                            admission(b).call
                        )
                    )
            else:
                import ollama_lifecycle as ol
                import lmstudio_lifecycle as lm

                with pytest.raises(
                    (ol.OllamaDenied, lm.LmStudioDenied), match="inference_intent_stale"
                ):
                    await collect(
                        b.bridge.infer(
                            native_job.permit,
                            native_job.prepared,
                            native_job.activity,
                            lease=mem,
                            sample=await native_job.fit.sample(),
                            produce_terminal=native_job.terminal,
                        )
                    )
            assert b.count_calls == 0 and b.wire.calls == []
            assert status(b.coord, "explicit-load") == ("admitted", "load")
            assert not b.coord.finish_request(
                forged, reference="not-inference", now_ms=b.now
            )

    asyncio.run(run())


def test_concurrent_control_completion_releases_slot_once(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        lease = load_reservation(coord, fleet)
        proof = fleet.evidence(coord, 1002)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: coord.finish_load(
                        lease, proof, reference="same-proof", now_ms=1002
                    ),
                    range(2),
                )
            )
        assert sorted(results) == [False, True]
        with sqlite3.connect(coord.database) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE event='load_finished'"
                ).fetchone()[0]
                == 1
            )
        assert coord.snapshot("shared").resident == A


def test_control_proof_cannot_finish_inference_even_with_rewrapped_fields(tmp_path):
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "inference")
        request = reserve(coord, fleet)
        assert not coord.finish_load(
            copy_as(lc.LoadLease, request), None, reference="wrong-purpose", now_ms=1002
        )
        assert not coord.mark_load_uncertain(
            copy_as(lc.LoadLease, request), reference="wrong-purpose", now_ms=1002
        )
        assert status(coord, "inference") == ("admitted", "inference")
        assert coord.snapshot("shared").state == "idle"
        assert coord.finish_request(
            request, reference="real-inference-terminal", now_ms=1002
        )


def test_control_queue_bound_and_expiry_share_fifo_budget(tmp_path):
    coord, fleet = opened(tmp_path, queue_limit=1)
    with coord:
        coord.enqueue_load("control", "shared", A, deadline_ms=1002, now_ms=1000)
        with pytest.raises(lc.CoordinatorError, match="queue_full"):
            enqueue(coord, "inference", B, now=1001)
        enqueue(coord, "inference", B, now=1003)
        assert status(coord, "control") == ("expired", "load")
        assert coord.claim_next("shared", now_ms=1003, request_id="control") is None
        step = coord.claim_next("shared", now_ms=1003, request_id="inference")
        assert step.kind == "load" and step.target == B


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_inference_driver_cannot_consume_load_purpose_step(tmp_path, backend):
    from test_native_transition_driver import fixture, admission, job
    from native_transition_driver import QueuedColdRequest

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            ready = admission(b)
            b.coord.enqueue_load(
                "load-only",
                "shared",
                ready.profile.memory.target,
                deadline_ms=40000,
                now_ms=b.now,
            )
            step = b.coord.claim_next("shared", now_ms=b.now, request_id="load-only")
            miswired = QueuedColdRequest(ready, "load-only", "shared", 40000)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await b.transition.advance(step, miswired, job(b, step))
            assert b.wire.calls == [] and b.memory.view("shared").committed_bytes == 0
            assert b.coord.claim_next("shared", now_ms=b.now) is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
def test_miswired_inference_scheduler_holds_control_slot_without_inference(
    tmp_path, monkeypatch, backend
):
    from test_native_transition_driver import fixture, admission, loaded
    from test_native_inference_scheduler import scheduler, collect

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            queued = b.transition.enqueue(
                admission(b), "initial", "shared", deadline_ms=40000
            )
            first, _ = await loaded(b, queued)
            assert b.coord.finish_request(
                first, reference="fixture-terminal", now_ms=b.now
            )
            b.wire.calls.clear()
            monkeypatch.setattr(b.coord, "enqueue", b.coord.enqueue_load)
            with pytest.raises(rd.RuntimeDispatchDenied):
                await collect(scheduler(b).stream(admission(b).call))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").active_requests == 1
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())
