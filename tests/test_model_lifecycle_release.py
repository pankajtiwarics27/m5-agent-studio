"""Unload-only kernel foundation: no native consumer/qualifier/activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

import model_lifecycle_coordinator as lc
import shared_memory_admission as sm
from local_runtime_lifecycle import ObservationStamp
from test_model_lifecycle_coordinator import A, B
from test_shared_memory_admission import opened, load_step, profile, sample

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner locking")
C = lc.ModelTarget("lmstudio", "model-c", "digest-c", "profile-c")


def loaded(coord, memory, fleet, target=A):
    step = load_step(coord, target)
    model = memory.reserve_model(step, profile(target), sample(memory), now_ms=1000)
    assert memory.dispatch(model, sample(memory), now_ms=1000)
    fleet.apply(coord, step, 1000)
    verify = coord.claim_next("shared", now_ms=1000)
    fleet.apply(coord, verify, 1000)
    request = coord.claim_next("shared", now_ms=1000)
    assert coord.finish_request(
        request, reference="fake-lifecycle-fixture-not-inference", now_ms=1000
    )
    return model


def release(coord, target=A, request="unload", deadline=100000):
    assert (
        coord.enqueue_unload(
            request, "shared", target, deadline_ms=deadline, now_ms=1000
        )
        == "queued"
    )
    step = coord.claim_next("shared", now_ms=1000, request_id=request)
    assert type(step) is lc.ReleaseStep and step.kind == "drain"
    assert not hasattr(step, "target")
    assert coord.complete_release(step, reference="fake-qualified-drain", now_ms=1000)
    unload = coord.claim_next("shared", now_ms=1000, request_id=request)
    assert type(unload) is lc.ReleaseStep and unload.kind == "unload"
    return unload


def physical(coord, fleet, step, now=1000):
    import runtime_persistence as rp

    rp.assert_no_active_write_transaction("fake release effect")
    fleet.models.pop(step.source.runtime_id)
    return fleet.evidence(coord, now)


def status(coord, request="unload"):
    with sqlite3.connect(coord.database) as conn:
        return conn.execute(
            "SELECT status,purpose FROM requests WHERE id=?", (request,)
        ).fetchone()


@pytest.mark.parametrize("target", [A, B, C])
def test_release_has_no_target_and_physical_proof_alone_never_unblocks_load(
    tmp_path, target
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet, target)
        step = release(coord, target)
        assert step.source == target and step.memory_id == model.lease_id
        assert not coord.complete_step(step, reference="wrong-protocol", now_ms=1000)
        coord.enqueue("next", "shared", B, deadline_ms=100000, now_ms=1000)
        proof = physical(coord, fleet, step)
        assert coord.complete_release(
            step, reference="fake-all-runtime-physical", now_ms=1000, evidence=proof
        )
        assert not coord.complete_release(
            step, reference="no-replay", now_ms=1000, evidence=proof
        )
        assert coord.snapshot("shared").resident is None
        assert coord.snapshot("shared").state == "switching"
        assert coord.snapshot("shared").operation_status == "complete"
        assert memory.view("shared").committed_bytes == 600
        assert not coord.finish_unload(step, reference="no-settlement-yet", now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000) is None
        assert coord.claim_next("shared", now_ms=1000, request_id="next") is None
        memory.settle(
            model,
            sample(memory),
            terminal=True,
            reference="independent-zero",
            now_ms=1000,
        )
        assert coord.finish_unload(
            step, reference="actual-held-source-released", now_ms=1000
        )
        assert not coord.finish_unload(step, reference="no-replay", now_ms=1000)
        assert status(coord) == ("completed", "unload")
        assert memory.view("shared").committed_bytes == 0
        assert coord.snapshot("shared").state == "idle"
        next_step = coord.claim_next("shared", now_ms=1000, request_id="next")
        assert not coord.mark_release_uncertain(
            step, reference="old-cannot-poison-successor", now_ms=1000
        )
        assert (
            type(next_step) is lc.TransitionStep
            and next_step.kind == "load"
            and next_step.target == B
        )
        assert fleet.trace == [
            ("load", target.artifact_id),
            ("verify", target.artifact_id),
        ]
        with sqlite3.connect(coord.database) as conn:
            row = conn.execute(
                "SELECT target,goal,source_memory_id,request_id FROM operations WHERE id=?",
                (step.operation_id,),
            ).fetchone()
        assert row == (None, "release", model.lease_id, "unload")


@pytest.mark.parametrize(
    "fault",
    [
        "no_proof",
        "ack",
        "not_terminal",
        "no_headroom",
        "missing_runtime",
        "wrong_scope",
        "stale",
        "still_loaded",
    ],
)
def test_incomplete_physical_proof_retains_source_and_hold(tmp_path, fault):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        step = release(coord)
        proof = physical(coord, fleet, step)
        now = 1000
        if fault == "no_proof":
            proof = None
        elif fault == "ack":
            proof = {"status": "ok"}
        elif fault == "not_terminal":
            proof = replace(proof, operations_terminal=False)
        elif fault == "no_headroom":
            proof = replace(proof, headroom_verified=False)
        elif fault == "missing_runtime":
            proof = replace(proof, inventories=proof.inventories[1:])
        elif fault == "wrong_scope":
            proof = replace(
                proof, scope=replace(proof.scope, operation_id="another-operation")
            )
        elif fault == "stale":
            now = 7000
            proof = replace(
                proof,
                now=7.0,
                inventories=(
                    replace(
                        proof.inventories[0],
                        stamp=ObservationStamp(0, coord.observer_clock_id),
                    ),
                )
                + proof.inventories[1:],
            )
        elif fault == "still_loaded":
            fleet.models[A.runtime_id] = A
            proof = fleet.evidence(coord, now)
        with pytest.raises(lc.CoordinatorError):
            coord.complete_release(
                step, reference="not-enough", now_ms=now, evidence=proof
            )
        assert coord.snapshot("shared").resident == A
        assert memory.view("shared").committed_bytes == 600
        assert coord.claim_next("shared", now_ms=now) is None


@pytest.mark.parametrize(
    "condition", ["nonzero", "nonterminal", "paused", "wrong_memory_id"]
)
def test_failed_independent_settlement_keeps_complete_release_barrier(
    tmp_path, condition
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet)
        step = release(coord)
        assert coord.complete_release(
            step,
            reference="fake-physical",
            now_ms=1000,
            evidence=physical(coord, fleet, step),
        )
        if condition in ("nonzero", "nonterminal"):
            observed = sample(
                memory,
                attributed=((model.lease_id, 1 if condition == "nonzero" else 0),),
            )
            with pytest.raises(sm.MemoryDenied):
                memory.settle(
                    model,
                    observed,
                    terminal=condition != "nonterminal",
                    reference="not-proven",
                    now_ms=1000,
                )
        elif condition == "paused":
            memory.mark_uncertain(model, reference="sample-lost", now_ms=1000)
            memory.settle(
                model,
                sample(memory),
                terminal=True,
                reference="later-zero",
                now_ms=1000,
            )
        else:
            memory.settle(
                model, sample(memory), terminal=True, reference="zero", now_ms=1000
            )
            step = replace(step, memory_id="not-the-bound-model")
        assert not coord.finish_unload(step, reference="no-authority", now_ms=1000)
        coord.enqueue("blocked", "shared", B, deadline_ms=100000, now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000) is None


@pytest.mark.parametrize(
    "field,changed",
    [
        ("owner_epoch", 2),
        ("generation", 99),
        ("request_id", "not-this-request"),
        ("source_instance_id", "wrong-instance"),
        ("memory_id", "wrong-model"),
        ("source", B),
    ],
)
def test_release_identity_cannot_be_rebound(tmp_path, field, changed):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        step = release(coord)
        altered = replace(step, **{field: changed})
        assert not coord.complete_release(altered, reference="forged", now_ms=1000)
        assert not coord.mark_release_uncertain(
            altered, reference="forged", now_ms=1000
        )
        assert not coord.finish_unload(altered, reference="forged", now_ms=1000)
        assert coord.snapshot("shared").state == "switching"
        assert memory.view("shared").committed_bytes == 600


@pytest.mark.parametrize("busy", ["inference", "load"])
def test_active_admission_blocks_release_without_disruption(tmp_path, busy):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet)
        enqueue = coord.enqueue if busy == "inference" else coord.enqueue_load
        enqueue("active", "shared", A, deadline_ms=100000, now_ms=1000)
        active = coord.claim_next("shared", now_ms=1000)
        coord.enqueue_unload("unload", "shared", A, deadline_ms=100000, now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000, request_id="unload") is None
        assert coord.snapshot("shared").resident == A
        assert memory.view("shared").committed_bytes == model.peak_bytes
        if busy == "inference":
            assert coord.finish_request(active, reference="fake-terminal", now_ms=1000)
        else:
            assert coord.finish_load(
                active,
                fleet.evidence(coord, 1000),
                reference="fake-native-terminal",
                now_ms=1000,
            )
        assert (
            type(coord.claim_next("shared", now_ms=1000, request_id="unload"))
            is lc.ReleaseStep
        )


@pytest.mark.parametrize(
    "condition",
    [
        "empty",
        "wrong_source",
        "unknown_memory",
        "reserved_memory",
        "paused_memory",
        "wrong_binding",
    ],
)
def test_unqualified_or_unmatched_source_cannot_create_release_operation(
    tmp_path, condition
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        if condition != "empty":
            model = loaded(coord, memory, fleet)
            if condition == "unknown_memory":
                memory.mark_uncertain(model, reference="unknown", now_ms=1000)
            elif condition == "reserved_memory":
                # Fault injection, not an API which can demote real dispatch.
                coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_reservations SET status='reserved'"
                    ),
                    now_ms=1000,
                )
            elif condition == "wrong_binding":
                binding = json.loads(model.binding)
                binding["profile"]["target"] = asdict(B)
                coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_reservations SET binding=? WHERE id=?",
                        (json.dumps(binding), model.lease_id),
                    ),
                    now_ms=1000,
                )
            elif condition == "paused_memory":
                coord._call(
                    lambda conn: conn.execute(
                        "UPDATE memory_domains SET state='paused'"
                    ),
                    now_ms=1000,
                )
        source = B if condition == "wrong_source" else A
        coord.enqueue_unload(
            "unload", "shared", source, deadline_ms=100000, now_ms=1000
        )
        assert coord.claim_next("shared", now_ms=1000) is None
        assert coord.snapshot("shared").operation_stage is None
        assert status(coord) == ("queued", "unload")


@pytest.mark.parametrize("kind", ["inference", "load"])
def test_purpose_idempotency_and_fifo_do_not_relabel_or_skip_unload(tmp_path, kind):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        coord.enqueue_unload("unload", "shared", A, deadline_ms=100000, now_ms=1000)
        enqueue = coord.enqueue if kind == "inference" else coord.enqueue_load
        with pytest.raises(lc.CoordinatorError, match="idempotency_conflict"):
            enqueue("unload", "shared", A, deadline_ms=100000, now_ms=1000)
        enqueue("behind", "shared", A, deadline_ms=100000, now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000, request_id="behind") is None
        step = coord.claim_next("shared", now_ms=1000, request_id="unload")
        assert type(step) is lc.ReleaseStep
        assert coord.complete_release(step, reference="drained", now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000, request_id="behind") is None
        assert (
            type(coord.claim_next("shared", now_ms=1000, request_id="unload"))
            is lc.ReleaseStep
        )


@pytest.mark.parametrize("condition", ["cancelled", "expired", "cancel_flag"])
def test_late_release_completion_retires_barrier_but_not_cancel_or_expiry_status(
    tmp_path, condition
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet)
        step = release(coord, deadline=1002)
        if condition == "cancelled":
            assert coord.cancel("unload", now_ms=1001) == "cancelled"
        elif condition == "cancel_flag":
            coord._call(
                lambda conn: conn.execute(
                    "UPDATE requests SET cancel_requested=1 WHERE id='unload'"
                ),
                now_ms=1001,
            )
        assert coord.complete_release(
            step,
            reference="native-was-terminal",
            now_ms=1001,
            evidence=physical(coord, fleet, step, now=1001),
        )
        memory.settle(
            model,
            sample(memory, now=1003),
            terminal=True,
            reference="zero",
            now_ms=1003,
        )
        assert coord.finish_unload(step, reference="barrier-retired", now_ms=1003)
        assert status(coord) == (
            "cancelled" if condition == "cancel_flag" else condition,
            "unload",
        )
        assert coord.snapshot("shared").resident is None


@pytest.mark.parametrize("phase", ["drain", "unload", "physical", "settled"])
def test_restart_fences_each_release_phase_and_never_replays(tmp_path, phase):
    coord, memory, fleet = opened(tmp_path)
    model = loaded(coord, memory, fleet)
    coord.enqueue_unload("unload", "shared", A, deadline_ms=100000, now_ms=1000)
    step = coord.claim_next("shared", now_ms=1000)
    if phase != "drain":
        assert coord.complete_release(step, reference="drained", now_ms=1000)
        step = coord.claim_next("shared", now_ms=1000)
    if phase in ("physical", "settled"):
        assert coord.complete_release(
            step,
            reference="physical",
            now_ms=1000,
            evidence=physical(coord, fleet, step),
        )
    if phase == "settled":
        memory.settle(
            model, sample(memory), terminal=True, reference="zero", now_ms=1000
        )
    coord.close()
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert new.snapshot("shared").state == "reconciling"
        assert new.claim_next("shared", now_ms=1001) is None
        assert not new.complete_release(step, reference="old", now_ms=1001)
        assert not new.finish_unload(step, reference="old", now_ms=1001)
        assert not new.mark_release_uncertain(step, reference="old", now_ms=1001)


def test_completed_physical_release_can_be_marked_uncertain_after_settlement_failure(
    tmp_path,
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        step = release(coord)
        assert coord.complete_release(
            step,
            reference="physical",
            now_ms=1000,
            evidence=physical(coord, fleet, step),
        )
        assert coord.mark_release_uncertain(
            step, reference="memory-unknown", now_ms=1000
        )
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.snapshot("shared").operation_status == "uncertain"
        assert memory.view("shared").committed_bytes == 600
        assert not coord.finish_unload(step, reference="no-bypass", now_ms=1000)


def test_concurrent_finish_retires_release_once(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = loaded(coord, memory, fleet)
        step = release(coord)
        assert coord.complete_release(
            step,
            reference="physical",
            now_ms=1000,
            evidence=physical(coord, fleet, step),
        )
        memory.settle(
            model, sample(memory), terminal=True, reference="zero", now_ms=1000
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: coord.finish_unload(step, reference="once", now_ms=1000),
                    range(2),
                )
            )
        assert sorted(results) == [False, True]
        with sqlite3.connect(coord.database) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE event='release_settled'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_older_populated_journal_versions_are_byte_preserved_and_refused(
    tmp_path, version
):
    coord, memory, fleet = opened(tmp_path)
    loaded(coord, memory, fleet)
    database = coord.database
    coord.close()
    with sqlite3.connect(database) as conn:
        conn.execute(f"PRAGMA user_version={version}")
    before = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError, match="journal_schema_unsupported"):
        lc.LifecycleCoordinator(tmp_path / "owner")
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.parametrize("phase", ["drain", "unload"])
def test_late_release_step_cannot_commit_ready_or_retire_hold(tmp_path, phase):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        coord.enqueue_unload("unload", "shared", A, deadline_ms=100000, now_ms=1000)
        step = coord.claim_next("shared", now_ms=1000, step_timeout_ms=1)
        if phase == "unload":
            assert coord.complete_release(step, reference="drained", now_ms=1000)
            step = coord.claim_next("shared", now_ms=1000, step_timeout_ms=1)
            proof = physical(coord, fleet, step, now=1001)
        else:
            proof = None
        assert not coord.complete_release(
            step, reference="late", now_ms=1001, evidence=proof
        )
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.snapshot("shared").operation_status == "uncertain"
        assert memory.view("shared").committed_bytes == 600
        assert not coord.finish_unload(step, reference="no-bypass", now_ms=1001)


@pytest.mark.parametrize(
    "change",
    [
        "target=source",
        "goal='acquire'",
        "stage='load'",
        "source_memory_id=NULL",
        "request_id=NULL",
    ],
)
def test_schema_cannot_relabel_release_as_acquisition_or_remove_bindings(
    tmp_path, change
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        step = release(coord)
        with sqlite3.connect(coord.database) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE operations SET " + change + " WHERE id=?",
                    (step.operation_id,),
                )
        assert coord.snapshot("shared").resident == A


@pytest.mark.parametrize("backend", ["ollama", "lmstudio"])
@pytest.mark.parametrize("consumer", ["load", "inference"])
def test_inference_or_load_actor_cannot_consume_release_step(
    tmp_path, monkeypatch, backend, consumer
):
    import runtime_dispatch_boundary as rd
    from test_native_transition_driver import fixture, admission
    from test_native_load_control import scheduler, call

    async def run():
        b = fixture(tmp_path, backend)
        with b.coord:
            execute = scheduler(b)
            assert await execute.command(call(b)) == {"status": "confirmed"}
            b.wire.calls.clear()
            monkeypatch.setattr(
                b.coord,
                "enqueue_load" if consumer == "load" else "enqueue",
                b.coord.enqueue_unload,
            )
            with pytest.raises(rd.RuntimeDispatchDenied):
                if consumer == "load":
                    await execute.command(call(b))
                else:
                    await anext(execute.stream(admission(b).call))
            assert b.wire.calls == []
            assert b.coord.snapshot("shared").state == "reconciling"
            assert b.coord.snapshot("shared").operation_status == "uncertain"
            assert b.memory.view("shared").committed_bytes == 600

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["ollama", "lmstudio", "omlx"])
def test_existing_native_permits_do_not_implicitly_accept_release_steps(
    tmp_path, backend
):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        loaded(coord, memory, fleet)
        step = release(coord)
        if backend == "omlx":
            from test_omlx_lifecycle_bridge import permit, Clock
            import omlx_lifecycle as native

            with pytest.raises(native.OmlxError, match="invalid_permit"):
                permit(coord, step, Clock())
        else:
            from test_native_transition_driver import fixture

            b = fixture(tmp_path / "independent-fixture", backend)
            try:
                from ollama_protocol import OllamaDenied
                from lmstudio_protocol import LmStudioDenied

                error = OllamaDenied if backend == "ollama" else LmStudioDenied
                with pytest.raises(error, match="invalid_permit"):
                    b.driver.permit(b, step)
            finally:
                b.coord.close()
