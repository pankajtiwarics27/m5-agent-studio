"""LC-02 fake fleet only. No installed runtime/native qualification is implied."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import model_lifecycle_coordinator as lc
import runtime_persistence as persistence
from local_runtime_lifecycle import ModelInstanceObservation, ObservationStamp, RuntimeIdentity, RuntimeInventory

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX owner locking; unsupported systems fail closed")
RUNTIMES = tuple(RuntimeIdentity(name, name, location="local", egress="local-only", memory_domain_id="shared",
                                ownership_mode="coordinated") for name in ("omlx", "ollama", "lmstudio"))
A = lc.ModelTarget("omlx", "model-a", "digest-a", "profile-a")
B = lc.ModelTarget("ollama", "model-b", "digest-b", "profile-b")


class Fleet:
    def __init__(self) -> None:
        self.models: dict[str, lc.ModelTarget] = {}
        self.trace: list[tuple[str, str]] = []

    def evidence(self, coord: lc.LifecycleCoordinator, now: int, **changes) -> lc.ReconciliationEvidence:
        observations = []
        for runtime in RUNTIMES:
            target = self.models.get(runtime.runtime_id)
            instances = () if target is None else (
                ModelInstanceObservation(target.artifact_id, instance_id="native-" + target.artifact_id,
                                         artifact_digest=target.artifact_digest, load_profile_fingerprint=target.profile_fingerprint,
                                         state="loaded", effective_context_tokens=8192, active_requests=0),
            )
            observations.append(RuntimeInventory(runtime, "fake-v1", "reachable", "complete",
                                                  ObservationStamp(now / 1000, coord.observer_clock_id), instances))
        evidence = lc.ReconciliationEvidence(tuple(observations), coord.observer_clock_id, now / 1000, 1.0,
                                             "fake-observation", coord.evidence_scope("shared"), True, True,
                                             tuple((x.runtime_id, "native-" + x.artifact_id) for x in self.models.values()))
        return replace(evidence, **changes)

    def apply(self, coord: lc.LifecycleCoordinator, step: lc.TransitionStep, now: int) -> None:
        # Every fake external effect occurs AFTER intent transaction commit.
        persistence.assert_no_active_write_transaction("fake lifecycle effect")
        self.trace.append((step.kind, step.target.artifact_id))
        evidence = None
        if step.kind == "load":
            assert not self.models, "physical overlap: source still allocated"
            self.models[step.target.runtime_id] = step.target
        elif step.kind == "unload":
            assert step.source_instance_id == "native-" + step.source.artifact_id
            self.models.pop(step.source.runtime_id)
            evidence = self.evidence(coord, now)
        elif step.kind == "verify":
            evidence = self.evidence(coord, now)
        assert coord.complete_step(step, reference="fake-terminal-" + step.kind, now_ms=now, evidence=evidence)


def opened(tmp_path: Path, *, queue_limit: int = 32) -> tuple[lc.LifecycleCoordinator, Fleet]:
    coord = lc.LifecycleCoordinator(tmp_path / "owner", queue_limit=queue_limit)
    coord.register_domain("shared", RUNTIMES, now_ms=1000)
    fleet = Fleet()
    coord.reconcile("shared", fleet.evidence(coord, 1000), now_ms=1000)
    return coord, fleet


def enqueue(coord: lc.LifecycleCoordinator, request: str, target: lc.ModelTarget = A, now: int = 1000, deadline: int = 100_000) -> None:
    assert coord.enqueue(request, "shared", target, now_ms=now, deadline_ms=deadline) == "queued"


def reserve(coord: lc.LifecycleCoordinator, fleet: Fleet, now: int = 1001) -> lc.RequestLease:
    for _ in range(5):
        item = coord.claim_next("shared", now_ms=now)
        if isinstance(item, lc.RequestLease):
            return item
        assert isinstance(item, lc.TransitionStep)
        fleet.apply(coord, item, now)
    raise AssertionError("transition did not complete")


def test_new_namespace_and_restart_require_reconciliation(tmp_path: Path) -> None:
    with lc.LifecycleCoordinator(tmp_path / "owner") as coord:
        coord.register_domain("shared", RUNTIMES, now_ms=1000)
        enqueue(coord, "first")
        assert coord.claim_next("shared", now_ms=1000) is None
        assert coord.snapshot("shared").state == "reconciling"
        fleet = Fleet()
        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            coord.reconcile("shared", fleet.evidence(coord, 1000, operations_terminal=False), now_ms=1000)
        coord.reconcile("shared", fleet.evidence(coord, 1000), now_ms=1000)
    with lc.LifecycleCoordinator(tmp_path / "owner") as replacement:
        assert replacement.snapshot("shared").state == "reconciling"
        assert replacement.claim_next("shared", now_ms=1001) is None
        assert replacement.snapshot("shared").queued == 1


def test_fifo_cold_switch_and_warm_residency_are_distinct_from_request_lease(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        enqueue(coord, "b", B)
        first = reserve(coord, fleet)
        assert first.target == A and first.instance_id == "native-model-a"
        assert coord.claim_next("shared", now_ms=1002) is None
        assert coord.finish_request(first, reference="fake-terminal-inference", now_ms=1002)
        assert not coord.finish_request(first, reference="duplicate", now_ms=1002)
        assert coord.snapshot("shared").resident == A  # request exit DOES NOT evict
        second = reserve(coord, fleet, 1003)
        assert second.target == B
        assert [x[0] for x in fleet.trace] == ["load", "verify", "drain", "unload", "load", "verify"]
        assert coord.finish_request(second, reference="done", now_ms=1004)


def test_same_target_waiters_share_load_and_do_not_replay_completed_request(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "one")
        enqueue(coord, "two")
        enqueue(coord, "one")
        first = reserve(coord, fleet)
        coord.finish_request(first, reference="done", now_ms=1002)
        second = reserve(coord, fleet, 1003)
        coord.finish_request(second, reference="done", now_ms=1004)
        assert fleet.trace == [("load", "model-a"), ("verify", "model-a")]
        assert coord.enqueue("one", "shared", A, deadline_ms=100_000, now_ms=1005) == "completed"
        assert coord.claim_next("shared", now_ms=1005) is None
        with pytest.raises(lc.CoordinatorError, match="idempotency_conflict"):
            coord.enqueue("one", "shared", B, deadline_ms=100_000, now_ms=1005)


def test_cancelled_last_waiter_does_not_cancel_dispatched_load(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "cancel-me")
        load = coord.claim_next("shared", now_ms=1001)
        assert isinstance(load, lc.TransitionStep)
        assert coord.cancel("cancel-me", now_ms=1002) == "cancelled"
        enqueue(coord, "other", B, 1002)
        assert coord.claim_next("shared", now_ms=1002) is None
        assert coord.snapshot("shared").operation_stage == "load"
        fleet.apply(coord, load, 1003)
        verify = coord.claim_next("shared", now_ms=1004)
        fleet.apply(coord, verify, 1004)
        next_step = coord.claim_next("shared", now_ms=1005)
        assert next_step.kind == "drain" and next_step.source == A


def test_cancel_active_inference_retains_slot_until_terminal_confirmation(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        lease = reserve(coord, fleet)
        enqueue(coord, "b", B, 1002)
        assert coord.cancel("a", now_ms=1002) == "pending_terminal"
        assert coord.claim_next("shared", now_ms=1003) is None
        assert coord.snapshot("shared").active_requests == 1
        coord.finish_request(lease, reference="native-work-ended", now_ms=1004)
        assert coord.snapshot("shared").resident == A
        assert coord.claim_next("shared", now_ms=1004).kind == "drain"


def test_202_ack_is_not_release_and_missing_runtime_observation_blocks_load(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        lease = reserve(coord, fleet)
        coord.finish_request(lease, reference="done", now_ms=1002)
        enqueue(coord, "b", B, 1002)
        drain = coord.claim_next("shared", now_ms=1003)
        fleet.apply(coord, drain, 1003)
        unload = coord.claim_next("shared", now_ms=1004)
        assert coord.note_pending(unload, now_ms=1004)
        assert coord.claim_next("shared", now_ms=1005) is None
        assert coord.snapshot("shared").resident == A
        with pytest.raises(lc.CoordinatorError, match="resident_conflict"):
            coord.complete_step(unload, reference="false-ack", now_ms=1005, evidence=fleet.evidence(coord, 1005))
        fleet.models.clear()
        evidence = fleet.evidence(coord, 1006)
        with pytest.raises(lc.CoordinatorError, match="inventory_incomplete"):
            coord.complete_step(unload, reference="missing", now_ms=1006,
                                evidence=replace(evidence, inventories=evidence.inventories[:1]))
        assert coord.snapshot("shared").operation_stage == "unload"
        assert coord.complete_step(unload, reference="verified-absent", now_ms=1006, evidence=evidence)
        assert coord.claim_next("shared", now_ms=1007).kind == "load"


@pytest.mark.parametrize("field,value", [("operations_terminal", False), ("headroom_verified", False), ("operations_terminal", 1)])
def test_release_attestations_are_explicit_not_truthy(tmp_path: Path, field: str, value: object) -> None:
    with lc.LifecycleCoordinator(tmp_path / "owner") as coord:
        coord.register_domain("shared", RUNTIMES, now_ms=1000)
        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            coord.reconcile("shared", Fleet().evidence(coord, 1000, **{field: value}), now_ms=1000)


def test_timeout_late_completion_and_empty_snapshot_never_trigger_load_replay(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        step = coord.claim_next("shared", now_ms=1001, step_timeout_ms=10)
        assert coord.claim_next("shared", now_ms=1011) is None
        assert coord.snapshot("shared").operation_status == "uncertain"
        assert not coord.complete_step(step, reference="late", now_ms=1012)
        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            coord.reconcile("shared", fleet.evidence(coord, 1012, operations_terminal=False), now_ms=1012)
        assert coord.claim_next("shared", now_ms=1012) is None
        # Only a new, scoped terminality/headroom attestation unblocks recovery.
        coord.reconcile("shared", fleet.evidence(coord, 1013), now_ms=1013)
        new = coord.claim_next("shared", now_ms=1014)
        assert new.operation_id != step.operation_id
        assert new.generation > step.generation
        assert not coord.complete_step(step, reference="obsolete", now_ms=1014)


def test_restart_fences_old_steps_and_rejects_replayed_evidence(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    enqueue(coord, "a")
    step = coord.claim_next("shared", now_ms=1001)
    coord.close()
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert not new.complete_step(step, reference="stale-owner", now_ms=1002)
        assert new.claim_next("shared", now_ms=1002) is None
        valid = fleet.evidence(new, 1003)
        for evidence in (replace(valid, clock_id=coord.observer_clock_id),
                         replace(valid, scope=replace(valid.scope, owner_epoch=step.owner_epoch)),
                         replace(valid, now=1.0)):
            with pytest.raises(lc.CoordinatorError, match="evidence_scope_mismatch"):
                new.reconcile("shared", evidence, now_ms=1003)
        new.reconcile("shared", valid, now_ms=1003)
    with pytest.raises(lc.CoordinatorError, match="owner_fenced"):
        coord.snapshot("shared")


def test_unknown_external_resident_cannot_be_adopted_or_evicted(tmp_path: Path) -> None:
    with lc.LifecycleCoordinator(tmp_path / "owner") as coord:
        coord.register_domain("shared", RUNTIMES, now_ms=1000)
        fleet = Fleet()
        fleet.models[B.runtime_id] = B
        with pytest.raises(lc.CoordinatorError, match="external_resident_unowned"):
            coord.reconcile("shared", fleet.evidence(coord, 1000), now_ms=1000, adopt=B)
        with pytest.raises(lc.CoordinatorError, match="resident_conflict"):
            coord.reconcile("shared", fleet.evidence(coord, 1000), now_ms=1000)
        assert coord.claim_next("shared", now_ms=1000) is None


def test_verify_requires_exact_target_and_explicit_native_instance_ownership(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        load = coord.claim_next("shared", now_ms=1001)
        fleet.apply(coord, load, 1001)
        verify = coord.claim_next("shared", now_ms=1002)
        evidence = fleet.evidence(coord, 1002)
        with pytest.raises(lc.CoordinatorError, match="ownership_unverified"):
            coord.complete_step(verify, reference="not-owned", now_ms=1002, evidence=replace(evidence, owned_instance_ids=()))
        wrong_instance = replace(evidence.inventories[0].instances[0], load_profile_fingerprint="different")
        wrong = replace(evidence.inventories[0], instances=(wrong_instance,))
        with pytest.raises(lc.CoordinatorError, match="target_unverified"):
            coord.complete_step(verify, reference="wrong-profile", now_ms=1002,
                                evidence=replace(evidence, inventories=(wrong,) + evidence.inventories[1:]))
        assert coord.snapshot("shared").resident is None


def test_queue_bound_expiry_idempotency_and_clock_regression(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path, queue_limit=1)
    with coord:
        enqueue(coord, "a", deadline=1002)
        with pytest.raises(lc.CoordinatorError, match="queue_full"):
            enqueue(coord, "b")
        assert coord.enqueue("a", "shared", A, deadline_ms=1002, now_ms=1002) == "expired"
        enqueue(coord, "b", B, 1003)
        assert coord.cancel("b", now_ms=1003) == "cancelled"
        assert coord.cancel("b", now_ms=1003) == "cancelled"
        with pytest.raises(lc.CoordinatorError, match="clock_regression"):
            coord.claim_next("shared", now_ms=1000)


def test_thread_race_has_one_dispatch_and_bounded_fifo(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: enqueue(coord, f"r{n}", A if n % 2 else B), range(8)))
            claims = list(pool.map(lambda _: coord.claim_next("shared", now_ms=1001), range(8)))
        assert sum(isinstance(x, lc.TransitionStep) for x in claims) == 1
        assert coord.snapshot("shared").queued == 8


def test_another_process_cannot_open_same_owner_namespace(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        code = """import sys
from pathlib import Path
from model_lifecycle_coordinator import LifecycleCoordinator, CoordinatorError
try:
    LifecycleCoordinator(Path(sys.argv[1]))
except CoordinatorError as exc:
    assert str(exc) == 'namespace_busy'
else:
    raise AssertionError('two owners')
"""
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "owner")], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert coord.snapshot("shared").state == "idle"


def test_process_crash_retains_uncertain_intent_and_queue(tmp_path: Path) -> None:
    code = """import os,sys
from pathlib import Path
from model_lifecycle_coordinator import LifecycleCoordinator, ModelTarget
from local_runtime_lifecycle import RuntimeIdentity, RuntimeInventory, ObservationStamp
from model_lifecycle_coordinator import ReconciliationEvidence
c=LifecycleCoordinator(Path(sys.argv[1]))
r=RuntimeIdentity('r','custom',location='local',egress='local-only',memory_domain_id='shared',ownership_mode='coordinated')
c.register_domain('shared',(r,),now_ms=1000)
i=RuntimeInventory(r,'fake-v1','reachable','complete',ObservationStamp(1.0,c.observer_clock_id))
e=ReconciliationEvidence((i,),c.observer_clock_id,1.0,1.0,'fake-proof',c.evidence_scope('shared'),True,True)
c.reconcile('shared',e,now_ms=1000)
c.enqueue('waiting','shared',ModelTarget('r','model','digest','profile'),deadline_ms=100000,now_ms=1000)
assert c.claim_next('shared',now_ms=1001).kind == 'load'
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path / "owner")], check=True, timeout=10)
    with lc.LifecycleCoordinator(tmp_path / "owner") as coord:
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.snapshot("shared").queued == 1
        assert coord.snapshot("shared").operation_stage == "load"
        assert coord.snapshot("shared").operation_status == "uncertain"
        assert coord.claim_next("shared", now_ms=1001) is None


@pytest.mark.parametrize("fault", ["future", "corrupt", "missing-table"])
def test_unknown_or_corrupt_schema_is_not_repaired_or_downgraded(tmp_path: Path, fault: str) -> None:
    coord, _ = opened(tmp_path)
    database = coord.database
    coord.close()
    if fault == "corrupt":
        database.write_bytes(b"not a database")
    else:
        with sqlite3.connect(database) as conn:
            conn.execute("PRAGMA user_version=999" if fault == "future" else "DROP TABLE domains")
    before = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError):
        lc.LifecycleCoordinator(tmp_path / "owner")
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_initial_schema_fault_rolls_back_and_does_not_silently_reinitialize(tmp_path: Path, monkeypatch) -> None:
    def fail(conn):
        conn.execute("CREATE TABLE partial(id INTEGER)")
        raise RuntimeError("private body")
    with monkeypatch.context() as patch:
        patch.setattr(lc, "_initialize_schema", fail)
        with pytest.raises(lc.CoordinatorError, match="journal_unavailable") as error:
            lc.LifecycleCoordinator(tmp_path / "owner")
        assert "private body" not in str(error.value)
    with sqlite3.connect(tmp_path / "owner/journal.sqlite3") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone() is None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    with pytest.raises(lc.CoordinatorError, match="journal_schema_unsupported"):
        lc.LifecycleCoordinator(tmp_path / "owner")


def test_private_permissions_symlinks_and_lock_replacement_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "owner"
    coord, _ = opened(tmp_path)
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (coord.database.stat().st_mode & 0o777) == 0o600
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(lc.CoordinatorError, match="unsafe_namespace"):
        lc.LifecycleCoordinator(alias)
    (root / "owner.lock").rename(root / "old.lock")
    (root / "owner.lock").touch(mode=0o600)
    with pytest.raises(lc.CoordinatorError, match="owner_fenced"):
        coord.snapshot("shared")
    coord.close()


def test_disk_failure_is_redacted_and_cannot_dispatch_an_effect(tmp_path: Path, monkeypatch) -> None:
    coord, _ = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        def fail(*args, **kwargs):
            raise persistence.DatabaseCapacityError("private upstream detail")
        with monkeypatch.context() as patch:
            patch.setattr(persistence, "run_write_transaction", fail)
            with pytest.raises(lc.CoordinatorError, match="journal_unavailable") as error:
                coord.claim_next("shared", now_ms=1001)
            assert str(error.value) == "journal_unavailable"
        assert coord.snapshot("shared").operation_stage is None


def test_binding_changes_unknown_authority_and_invalid_tokens_are_rejected(tmp_path: Path) -> None:
    coord, _ = opened(tmp_path)
    with coord:
        with pytest.raises(lc.CoordinatorError, match="domain_binding_changed"):
            coord.register_domain("shared", RUNTIMES[:1], now_ms=1000)
        with pytest.raises(lc.CoordinatorError):
            lc.RequestLease("lease", "req", "shared", True, 1, A, "native-a")
    with lc.LifecycleCoordinator(tmp_path / "other") as other:
        unknown = replace(RUNTIMES[0], location="unknown")
        other.register_domain("shared", (unknown,), now_ms=1000)
        with pytest.raises(lc.CoordinatorError, match="control_binding_unqualified"):
            enqueue(other, "no")


def test_namespace_path_is_owner_machine_scoped_not_workspace_selected() -> None:
    assert str(lc.owner_namespace_path(Path("/owner"), "Darwin")).endswith("Application Support/M5 Agent Studio/lifecycle")
    assert lc.owner_namespace_path(Path("/owner"), "Linux") == Path("/owner/.local/state/m5-agent-studio/lifecycle")
    with pytest.raises(lc.CoordinatorError, match="locking_platform_unsupported"):
        lc.owner_namespace_path(Path("/owner"), "unsupported")


def test_physical_late_load_blocks_other_target_until_explicit_owned_recovery(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        step = coord.claim_next("shared", now_ms=1001, step_timeout_ms=10)
        coord.cancel("a", now_ms=1002)
        enqueue(coord, "b", B, 1002)
        assert coord.claim_next("shared", now_ms=1011) is None
        fleet.models[A.runtime_id] = A  # physical operation finishes AFTER timeout
        assert not coord.complete_step(step, reference="late-load", now_ms=1012)
        with pytest.raises(lc.CoordinatorError, match="resident_conflict"):
            coord.reconcile("shared", fleet.evidence(coord, 1012), now_ms=1012)
        assert coord.claim_next("shared", now_ms=1012) is None
        coord.reconcile("shared", fleet.evidence(coord, 1013), now_ms=1013, adopt=A)
        lease = reserve(coord, fleet, 1014)
        assert lease.target == B
        assert [kind for kind, _ in fleet.trace] == ["drain", "unload", "load", "verify"]


@pytest.mark.parametrize("defect", ["stale", "unreachable", "partial", "duplicate", "unknown-requests", "helper"])
def test_incomplete_uncertain_or_extra_native_state_never_clears_hold(tmp_path: Path, defect: str) -> None:
    with lc.LifecycleCoordinator(tmp_path / "owner") as coord:
        coord.register_domain("shared", RUNTIMES, now_ms=2000)
        fleet = Fleet()
        evidence = fleet.evidence(coord, 2000)
        observation = evidence.inventories[0]
        if defect == "stale":
            observation = replace(observation, stamp=ObservationStamp(0.0, coord.observer_clock_id))
        elif defect == "unreachable":
            observation = replace(observation, reachability="unreachable")
        elif defect == "partial":
            observation = replace(observation, completeness="partial")
        elif defect == "unknown-requests":
            observation = replace(observation, instances=(ModelInstanceObservation("model", state="absent"),))
        elif defect == "helper":
            observation = replace(observation, instances=(ModelInstanceObservation("helper", state="loaded", active_requests=0),))
        inventories = (observation,) + evidence.inventories[1:]
        if defect == "duplicate":
            inventories += (observation,)
        with pytest.raises(lc.CoordinatorError):
            coord.reconcile("shared", replace(evidence, inventories=inventories), now_ms=2000)
        assert coord.snapshot("shared").state == "reconciling"


def test_forked_child_cannot_use_inherited_owner_or_unlock_parent(tmp_path: Path) -> None:
    coord, _ = opened(tmp_path)
    with coord:
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    coord.snapshot("shared")
                except lc.CoordinatorError as exc:
                    assert str(exc) == "owner_fenced"
                else:
                    os._exit(2)
                coord.close()
                os._exit(0)
            except BaseException:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        with pytest.raises(lc.CoordinatorError, match="namespace_busy"):
            lc.LifecycleCoordinator(tmp_path / "owner")
        assert coord.snapshot("shared").state == "idle"


@pytest.mark.parametrize("kind", ["drain", "unload", "load", "verify"])
def test_restart_at_every_dispatched_effect_boundary_never_replays(tmp_path: Path, kind: str) -> None:
    coord, fleet = opened(tmp_path)
    enqueue(coord, "a")
    lease = reserve(coord, fleet)
    coord.finish_request(lease, reference="done", now_ms=1002)
    enqueue(coord, "b", B, 1002)
    for _ in range(4):
        step = coord.claim_next("shared", now_ms=1003)
        if step.kind == kind:
            break
        fleet.apply(coord, step, 1003)
    assert step.kind == kind
    trace = list(fleet.trace)
    coord.close()
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert new.snapshot("shared").state == "reconciling"
        assert new.snapshot("shared").operation_status == "uncertain"
        assert new.claim_next("shared", now_ms=1004) is None
        assert not new.complete_step(step, reference="old-worker", now_ms=1004)
        assert fleet.trace == trace


def test_restart_retains_warm_identity_and_fences_old_request_lease(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    enqueue(coord, "a")
    old = reserve(coord, fleet)
    coord.close()
    with lc.LifecycleCoordinator(tmp_path / "owner") as new:
        assert not new.finish_request(old, reference="old", now_ms=1002)
        new.reconcile("shared", fleet.evidence(new, 1003), now_ms=1003, adopt=A)
        assert new.snapshot("shared").resident_instance_id == old.instance_id
        enqueue(new, "new", A, 1004)
        current = reserve(new, fleet, 1005)
        assert not new.finish_request(replace(current, instance_id="unrelated"), reference="wrong-instance", now_ms=1005)
        assert new.snapshot("shared").active_requests == 1
        assert new.finish_request(current, reference="done", now_ms=1005)
        assert fleet.trace == [("load", "model-a"), ("verify", "model-a")]


def test_semantically_corrupt_target_is_rejected_without_repair(tmp_path: Path) -> None:
    coord, _ = opened(tmp_path)
    enqueue(coord, "a")
    database = coord.database
    coord.close()
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE requests SET target='not-json'")
    before = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError):
        lc.LifecycleCoordinator(tmp_path / "owner")
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_inference_uncertainty_keeps_domain_and_slot_until_reconciled(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        lease = reserve(coord, fleet)
        enqueue(coord, "b", B, 1002)
        assert coord.mark_request_uncertain(lease, reference="inference-disconnect", now_ms=1002)
        assert coord.snapshot("shared").state == "reconciling"
        assert coord.snapshot("shared").active_requests == 1
        assert coord.claim_next("shared", now_ms=1003) is None
        with pytest.raises(lc.CoordinatorError, match="release_unverified"):
            coord.reconcile("shared", fleet.evidence(coord, 1003, operations_terminal=False), now_ms=1003, adopt=A)
        coord.reconcile("shared", fleet.evidence(coord, 1004), now_ms=1004, adopt=A)
        assert not coord.finish_request(lease, reference="obsolete", now_ms=1004)
        assert coord.claim_next("shared", now_ms=1005).kind == "drain"


def test_evidence_cannot_relax_coordinator_freshness_policy(tmp_path: Path) -> None:
    with lc.LifecycleCoordinator(tmp_path / "owner", observation_max_age_s=1) as coord:
        coord.register_domain("shared", RUNTIMES, now_ms=5000)
        evidence = Fleet().evidence(coord, 5000, max_age=1000)
        old = replace(evidence.inventories[0], stamp=ObservationStamp(0.0, coord.observer_clock_id))
        with pytest.raises(lc.CoordinatorError, match="inventory_unverified"):
            coord.reconcile("shared", replace(evidence, inventories=(old,) + evidence.inventories[1:]), now_ms=5000)


def test_journal_records_epoch_and_generation_without_request_payloads(tmp_path: Path) -> None:
    coord, fleet = opened(tmp_path)
    with coord:
        enqueue(coord, "a")
        lease = reserve(coord, fleet)
        coord.finish_request(lease, reference="done-reference", now_ms=1002)
        with sqlite3.connect(coord.database) as conn:
            rows = conn.execute("SELECT event,owner_epoch,generation FROM journal ORDER BY seq").fetchall()
            assert ("dispatch_load", 1, 1) in rows
            assert ("request_finished", 1, 1) in rows
            columns = {r[1] for r in conn.execute("PRAGMA table_info(journal)")}
            assert not columns.intersection({"prompt", "body", "arguments", "output", "credential"})


def test_fork_with_inherited_locked_mutex_is_bounded_and_cannot_unlock_parent(tmp_path: Path) -> None:
    import signal
    import threading
    coord, _ = opened(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def hold_mutex():
        with coord._mutex:
            entered.set()
            assert release.wait(10)
    with coord, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold_mutex)
        assert entered.wait(5)
        pid = os.fork()
        if pid == 0:
            signal.alarm(3)
            try:
                try:
                    coord.snapshot("shared")
                except lc.CoordinatorError as exc:
                    assert str(exc) == "owner_fenced"
                else:
                    os._exit(2)
                coord.close()
                os._exit(0)
            except BaseException:
                os._exit(3)
        try:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        finally:
            release.set()
        future.result(timeout=5)
        with pytest.raises(lc.CoordinatorError, match="namespace_busy"):
            lc.LifecycleCoordinator(tmp_path / "owner")
