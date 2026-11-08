"""Offline LC-03 assumptions/faults; no installed-engine or memory qualification."""
from __future__ import annotations

import hashlib
import os
import json
import subprocess
import sys
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import execution_profiles as ep
import model_lifecycle_coordinator as lc
import runtime_persistence as rp
import shared_memory_admission as sm
from local_runtime_lifecycle import ObservationStamp
from test_model_lifecycle_coordinator import A, B, Fleet, RUNTIMES

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='owner namespace requires POSIX locking')


def footprint(amount=300, *, subprocess_slots=1):
    return ep.ResourceFootprint('conservative_workspace', amount, 1, subprocess_slots, 1, ())


def action_bound(fp, *, amount=None):
    digest = hashlib.sha256(json.dumps(fp.to_mapping(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return sm.ActionMemoryBound(digest,fp.memory_bytes if amount is None else amount,'fake-enforced-upper-bound')


def reserve_action(memory, domain, work, fp, observed, *, now_ms):
    return memory.reserve_action(domain,work,fp,observed,bound=action_bound(fp),now_ms=now_ms)


def expand_action(memory, lease, fp, observed, *, now_ms):
    return memory.expand_action(lease,fp,observed,bound=action_bound(fp),now_ms=now_ms)


def profile(target=A, amount=600):
    return sm.ModelMemoryProfile(target, 8192, amount-100, 20, 20, 20, 20, 20, 'fake-qualified-bound')


def sample(memory, *, available=900, attributed=None, now=1000, **changes):
    view = memory.view('shared')
    if attributed is None:
        attributed = tuple((x.lease_id, 0) for x in view.leases)
    result = sm.MemorySample(view.scope, ObservationStamp(now/1000, view.scope.clock_id), 1000,
                             available, 'normal', attributed, 'fake-physical-observation', True)
    return replace(result, **changes)


def opened(tmp_path, *, limit=900):
    coord = lc.LifecycleCoordinator(tmp_path/'owner')
    coord.register_domain('shared', RUNTIMES, now_ms=1000)
    fleet = Fleet()
    coord.reconcile('shared', fleet.evidence(coord, 1000), now_ms=1000)
    memory = sm.SharedMemoryAdmission(coord)
    memory.configure('shared', sm.MemoryPolicy(limit, 100, 'fake-approved-envelope'), now_ms=1000)
    memory.reconcile('shared', sample(memory), (), now_ms=1000)
    return coord, memory, fleet


def load_step(coord, target=A, *, now=1000):
    coord.enqueue('request-'+target.artifact_id, 'shared', target, deadline_ms=100000, now_ms=now)
    step = coord.claim_next('shared', now_ms=now)
    assert isinstance(step, lc.TransitionStep) and step.kind == 'load'
    return step


def test_combined_model_and_tool_work_is_refused_though_each_fits_alone(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        reserve_action(memory,'shared', 'tool', footprint(300), sample(memory), now_ms=1000)
        step = load_step(coord)
        # Model 600 and tool 300 each fit 900 available minus 100 OS reserve;
        # combined unallocated demand 900 + 100 does not.
        with pytest.raises(sm.MemoryDenied, match='headroom_insufficient'):
            memory.reserve_model(step, profile(), sample(memory), now_ms=1000)
        assert memory.view('shared').committed_bytes == 300


def test_shared_budget_cap_and_physical_headroom_are_independent(tmp_path):
    coord, memory, _ = opened(tmp_path, limit=500)
    with coord:
        with pytest.raises(sm.MemoryDenied, match='budget_exceeded'):
            memory.reserve_model(load_step(coord), profile(), sample(memory), now_ms=1000)
        assert memory.view('shared').committed_bytes == 0


def test_attributed_allocation_is_not_double_counted_but_future_growth_is(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        model = memory.reserve_model(load_step(coord), profile(), sample(memory), now_ms=1000)
        assert memory.dispatch(model, sample(memory), now_ms=1000)
        # 600 already physical; only 300 new tool + 100 reserve is needed.
        observed = sample(memory, available=400, attributed=((model.lease_id, 600),))
        action = reserve_action(memory,'shared', 'tool', footprint(300), observed, now_ms=1000)
        assert memory.view('shared').committed_bytes == 900
        # With only 500 of 600 physical, reserve the remaining 100 too.
        memory.settle(action, sample(memory, available=400, attributed=((model.lease_id, 600),(action.lease_id,0))),
                      terminal=True, reference='fake-tool-terminal', now_ms=1000)
        with pytest.raises(sm.MemoryDenied, match='headroom_insufficient'):
            reserve_action(memory,'shared','another',footprint(300),
                                  sample(memory, available=450, attributed=((model.lease_id, 500),)), now_ms=1000)


@pytest.mark.parametrize('pressure', ['unknown','elevated','critical'])
def test_pressure_blocks_admission_and_dispatch_without_releasing_holds(tmp_path, pressure):
    coord, memory, _ = opened(tmp_path)
    with coord:
        action = reserve_action(memory,'shared', 'tool', footprint(), sample(memory), now_ms=1000)
        with pytest.raises(sm.MemoryDenied, match='pressure_unsafe'):
            memory.dispatch(action, sample(memory, pressure=pressure), now_ms=1000)
        assert memory.view('shared').committed_bytes == 300


@pytest.mark.parametrize('changes,category', [
    ({'total_bytes':None}, 'capacity_unknown'), ({'available_bytes':None}, 'capacity_unknown'),
    ({'available_bytes':1001}, 'capacity_unknown'), ({'total_bytes':0}, 'capacity_unknown'),
    ({'complete':False}, 'capacity_unknown'), ({'stamp':ObservationStamp(0,'old')}, 'observation_stale'),
    ({'stamp':ObservationStamp(2,'wrong')}, 'observation_stale'),
])
def test_unknown_or_invalid_observation_blocks_new_reservations(tmp_path, changes, category):
    coord, memory, _ = opened(tmp_path)
    with coord, pytest.raises(sm.MemoryDenied, match=category):
        reserve_action(memory,'shared','tool',footprint(),sample(memory, **changes), now_ms=1000)


def test_scope_revision_clock_age_and_lifecycle_generation_fence_samples(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        stale = sample(memory)
        load_step(coord)  # Changes lifecycle generation, not memory revision.
        with pytest.raises(sm.MemoryDenied, match='scope_stale'):
            reserve_action(memory,'shared','tool',footprint(),stale,now_ms=1000)
        stale = sample(memory)
        reserve_action(memory,'shared','first',footprint(),stale,now_ms=1000)
        with pytest.raises(sm.MemoryDenied, match='scope_stale'):
            reserve_action(memory,'shared','second',footprint(),stale,now_ms=1000)
        with pytest.raises(sm.MemoryDenied, match='observation_stale'):
            reserve_action(memory,'shared','second',footprint(),sample(memory),now_ms=7000)


def test_missing_duplicate_excess_and_out_of_bound_attributions_are_refused(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        action = reserve_action(memory,'shared','first',footprint(),sample(memory),now_ms=1000)
        memory.dispatch(action,sample(memory),now_ms=1000)
        for attributed, category in [
            ((), 'attribution_incomplete'), (((action.lease_id,1),(action.lease_id,1)), 'attribution_incomplete'),
            (((action.lease_id,200),), 'attribution_incomplete'),  # only 100 total physical usage
            (((action.lease_id,301),), 'bound_exceeded'),
        ]:
            available = 500 if category == 'bound_exceeded' else 900
            with pytest.raises(sm.MemoryDenied, match=category):
                reserve_action(memory,'shared','second',footprint(),sample(memory,available=available,attributed=attributed),now_ms=1000)
            memory.reconcile('shared',sample(memory),
                (sm.MemoryResolution(action.lease_id,'retain','fake-bounded-owner',ownership_verified=True),),now_ms=1000)
        assert memory.view('shared').committed_bytes == 300


def test_action_subprocess_slots_do_not_become_additional_memory_pools(tmp_path):
    coord, memory, _ = opened(tmp_path)
    controller = ep.UnifiedExecutionController(memory_budget_bytes=900)
    with coord:
        fp = footprint(300, subprocess_slots=2)
        barrier = controller.admit_footprint(fp)
        assert barrier.allowed
        lease = reserve_action(memory,'shared','tool',fp,sample(memory),now_ms=1000)
        assert controller.snapshot()['used_memory_bytes'] == lease.peak_bytes == 300
        assert controller.snapshot()['subprocess_slots']['used'] == 2
        # Ledger does not release on controller completion or enlarge grants.
        controller.release(barrier.lease_id)
        assert memory.view('shared').committed_bytes == 300


def test_expansion_is_preallocation_atomic_and_old_token_is_fenced(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        action = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied, match='headroom_insufficient'):
            expand_action(memory,action,footprint(850),sample(memory),now_ms=1000)
        assert memory.view('shared').committed_bytes == 300
        expanded = expand_action(memory,action,footprint(600),sample(memory),now_ms=1000)
        assert expanded.peak_bytes == 600
        with pytest.raises(sm.MemoryDenied, match='lease_fenced'):
            memory.settle(action,sample(memory),terminal=True,reference='old-token',now_ms=1000)
        memory.settle(expanded,sample(memory),terminal=True,reference='fake-terminal',now_ms=1000)
        assert memory.view('shared').committed_bytes == 0


def test_unknown_inference_or_tool_terminal_state_pauses_whole_memory_domain(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        action = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        memory.dispatch(action,sample(memory),now_ms=1000)
        memory.mark_uncertain(action,reference='fake-disconnect',now_ms=1000)
        assert memory.view('shared').state == 'paused'
        with pytest.raises(sm.MemoryDenied,match='reconciliation_required'):
            reserve_action(memory,'shared','next',footprint(),sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='release_unverified'):
            memory.settle(action,sample(memory),reference='cancel-is-not-terminal',now_ms=1000)
        assert memory.view('shared').committed_bytes == 300
        memory.reconcile('shared',sample(memory),
                         (sm.MemoryResolution(action.lease_id,'release','fake-authoritative-terminal',terminal=True),),now_ms=1000)
        assert memory.view('shared').state == 'ready'


def test_restart_retains_bounds_and_requires_complete_recovery_with_new_tokens(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    action = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
    memory.dispatch(action,sample(memory),now_ms=1000)
    old = sample(memory)
    coord.close()
    with lc.LifecycleCoordinator(tmp_path/'owner') as new:
        recovery = sm.SharedMemoryAdmission(new)
        assert recovery.view('shared').state == 'reconciling'
        assert recovery.view('shared').committed_bytes == 300
        with pytest.raises(sm.MemoryDenied,match='scope_stale'):
            recovery.reconcile('shared',old,(),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='recovery_incomplete'):
            recovery.reconcile('shared',sample(recovery),(),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='ownership_unverified'):
            recovery.reconcile('shared',sample(recovery),(sm.MemoryResolution(action.lease_id,'retain','unknown'),),now_ms=1000)
        adopted, = recovery.reconcile('shared',sample(recovery),
            (sm.MemoryResolution(action.lease_id,'retain','fake-owned',ownership_verified=True),),now_ms=1000)
        assert adopted.owner_epoch != action.owner_epoch
        with pytest.raises(sm.MemoryDenied,match='lease_fenced'):
            recovery.settle(action,sample(recovery),terminal=True,reference='stale',now_ms=1000)
        # Base lifecycle still needs reconciliation independently.
        with pytest.raises(sm.MemoryDenied,match='reconciliation_required'):
            reserve_action(recovery,'shared','second',footprint(),sample(recovery),now_ms=1000)
        new.reconcile('shared',fleet.evidence(new,1000),now_ms=1000)
        assert recovery.dispatch(adopted,sample(recovery),now_ms=1000) is False  # Never replay.


def test_model_profile_and_expired_intent_must_match_on_reserve_and_dispatch(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        step = load_step(coord)
        with pytest.raises(sm.MemoryDenied,match='profile_mismatch'):
            memory.reserve_model(step,profile(B),sample(memory),now_ms=1000)
        lease = memory.reserve_model(step,profile(),sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='lifecycle_intent_stale'):
            memory.dispatch(lease,sample(memory,now=62000),now_ms=62000)
        assert memory.view('shared').committed_bytes == 600


def test_duplicate_work_is_exactly_bound_and_cannot_reuse_released_id(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        lease = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        assert reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000) == lease
        with pytest.raises(sm.MemoryDenied,match='idempotency_conflict'):
            reserve_action(memory,'shared','tool',footprint(400),sample(memory),now_ms=1000)
        assert memory.dispatch(lease,sample(memory),now_ms=1000)
        assert memory.dispatch(lease,sample(memory),now_ms=1000) is False
        with pytest.raises(sm.MemoryDenied,match='release_unverified'):
            memory.settle(lease,sample(memory,attributed=((lease.lease_id,100),)),terminal=True,reference='still-physical',now_ms=1000)
        memory.settle(lease,sample(memory),terminal=True,reference='fake-terminal',now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='idempotency_conflict'):
            reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)


def test_same_snapshot_contenders_cannot_race_oversubscription(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        observed = sample(memory)
        def attempt(n):
            try:
                reserve_action(memory,'shared','tool-'+str(n),footprint(500),observed,now_ms=1000)
                return True
            except sm.MemoryDenied:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert sum(pool.map(attempt,range(8))) == 1
        assert memory.view('shared').committed_bytes == 500


def test_single_physical_envelope_policy_and_namespace_are_not_silently_rebound(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        with pytest.raises(sm.MemoryDenied,match='policy_changed'):
            memory.configure('shared',sm.MemoryPolicy(1000,100,'changed'),now_ms=1000)
        other = tuple(replace(x,memory_domain_id='other') for x in RUNTIMES)
        coord.register_domain('other',other,now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='domain_conflict'):
            memory.configure('other',sm.MemoryPolicy(900,100,'same-physical-host'),now_ms=1000)


def test_v1_dormant_journal_is_preserved_and_refused_not_upgraded(tmp_path):
    coord, memory, _ = opened(tmp_path)
    reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
    coord.close()
    db = tmp_path/'owner/journal.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE memory_reservations')
        conn.execute('DROP TABLE memory_domains')
        conn.execute('PRAGMA user_version=1')
    before = hashlib.sha256(db.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError,match='schema_unsupported'):
        lc.LifecycleCoordinator(tmp_path/'owner')
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_storage_fault_rolls_back_reservation_and_redacts_body(tmp_path, monkeypatch):
    coord, memory, _ = opened(tmp_path)
    with coord:
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError('sensitive-upstream-or-storage-body')
        monkeypatch.setattr(memory,'_change',broken)
        with pytest.raises(lc.CoordinatorError,match='^journal_unavailable$'):
            reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        assert memory.view('shared').committed_bytes == 0


def test_fake_effect_only_after_committed_dispatch(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        lease = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        assert memory.dispatch(lease,sample(memory),now_ms=1000)
        rp.assert_no_active_write_transaction('fake bounded action')
        with sqlite3.connect(coord.database) as reader:
            assert reader.execute('SELECT status FROM memory_reservations WHERE id=?',(lease.lease_id,)).fetchone()[0] == 'dispatched'


def test_declared_default_footprint_is_not_a_qualified_physical_upper_bound(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        with pytest.raises(sm.MemoryDenied,match='action_bound_unknown'):
            memory.reserve_action('shared','tool',footprint(),sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='action_bound_mismatch'):
            memory.reserve_action('shared','tool',footprint(),sample(memory),
                                  bound=action_bound(footprint(200)),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='action_bound_mismatch'):
            memory.reserve_action('shared','tool',footprint(),sample(memory),
                                  bound=action_bound(footprint(),amount=500),now_ms=1000)
        # The effective footprint must first declare the full qualified peak so
        # legacy action-pool counters cannot silently reserve a smaller amount.
        qualified = footprint(500)
        lease = memory.reserve_action('shared','tool',qualified,sample(memory),
                                      bound=action_bound(qualified),now_ms=1000)
        assert lease.peak_bytes == 500


def test_unsafe_pressure_observation_is_durable_and_old_favorable_sample_cannot_clear_it(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        favorable = sample(memory)
        with pytest.raises(sm.MemoryDenied,match='pressure_unsafe'):
            reserve_action(memory,'shared','tool',footprint(),sample(memory,pressure='critical'),now_ms=1000)
        with sqlite3.connect(coord.database) as reader:
            assert reader.execute('SELECT state FROM memory_domains').fetchone()[0] == 'paused'
        with pytest.raises(sm.MemoryDenied,match='reconciliation_required'):
            reserve_action(memory,'shared','tool',footprint(),favorable,now_ms=1000)
        memory.reconcile('shared',sample(memory),(),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='scope_stale'):
            reserve_action(memory,'shared','tool',footprint(),favorable,now_ms=1000)


def test_hard_process_exit_after_dispatch_keeps_durable_memory_hold(tmp_path):
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
from test_shared_memory_admission import opened, reserve_action, footprint, sample
coord, memory, fleet = opened(Path(sys.argv[1]))
lease = reserve_action(memory,'shared','crashed-tool',footprint(),sample(memory),now_ms=1000)
assert memory.dispatch(lease,sample(memory),now_ms=1000)
os._exit(0)
"""
    result = subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    with lc.LifecycleCoordinator(tmp_path/'owner') as coord:
        view = sm.SharedMemoryAdmission(coord).view('shared')
        assert view.state == 'reconciling' and view.committed_bytes == 300
        assert len(view.leases) == 1


@pytest.mark.parametrize('field,value', [('weights_bytes',True),('kv_bytes',-1),('context_tokens',0),
                                       ('workspace_bytes',None),('runtime_bytes',2**63)])
def test_invalid_or_unknown_profile_components_are_not_guessed(field,value):
    with pytest.raises((lc.CoordinatorError,sm.MemoryDenied)):
        replace(profile(),**{field:value})


def test_corrupt_stored_bound_is_refused_without_reset(tmp_path):
    coord, memory, _ = opened(tmp_path)
    reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
    coord.close()
    database = tmp_path/'owner/journal.sqlite3'
    with sqlite3.connect(database) as conn:
        conn.execute('UPDATE memory_reservations SET peak_bytes=1')
    before = hashlib.sha256(database.read_bytes()).digest()
    with pytest.raises(lc.CoordinatorError,match='journal_invalid'):
        lc.LifecycleCoordinator(tmp_path/'owner')
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_warm_request_completion_never_releases_model_memory_and_switch_waits_for_both_layers(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        step = load_step(coord)
        model = memory.reserve_model(step,profile(),sample(memory),now_ms=1000)
        assert memory.dispatch(model,sample(memory),now_ms=1000)
        fleet.apply(coord,step,1000)
        verify = coord.claim_next('shared',now_ms=1000)
        fleet.apply(coord,verify,1000)
        request = coord.claim_next('shared',now_ms=1000)
        assert coord.finish_request(request,reference="fake-request-terminal",now_ms=1000)
        assert memory.view('shared').committed_bytes == 600
        with pytest.raises(sm.MemoryDenied,match='model_release_unverified'):
            memory.settle(model,sample(memory),terminal=True,reference='request-is-not-release',now_ms=1000)
        coord.enqueue('switch-b','shared',B,deadline_ms=100000,now_ms=1000)
        drain = coord.claim_next('shared',now_ms=1000)
        fleet.apply(coord,drain,1000)
        unload = coord.claim_next('shared',now_ms=1000)
        fleet.apply(coord,unload,1000)
        next_load = coord.claim_next('shared',now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='model_already_reserved'):
            memory.reserve_model(next_load,profile(B),sample(memory),now_ms=1000)
        # Native empty/terminal lifecycle proof alone did not remove accounting.
        memory.settle(model,sample(memory),terminal=True,reference='fake-physical-release',now_ms=1000)
        replacement = memory.reserve_model(next_load,profile(B),sample(memory),now_ms=1000)
        assert replacement.peak_bytes == 600
        assert memory.dispatch(replacement,sample(memory),now_ms=1000)


def test_failed_load_hold_requires_lifecycle_recovery_before_accounting_release(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    with coord:
        model = memory.reserve_model(load_step(coord),profile(),sample(memory),now_ms=1000)
        memory.dispatch(model,sample(memory),now_ms=1000)
        memory.mark_uncertain(model,reference='fake-load-disconnect',now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='model_release_unverified'):
            memory.reconcile('shared',sample(memory),
                (sm.MemoryResolution(model.lease_id,'release','not-yet-terminal',terminal=True),),now_ms=1000)
        # Timeout does not itself free either physical or accounting hold.
        assert coord.claim_next('shared',now_ms=62000) is None
        coord.reconcile('shared',fleet.evidence(coord,62000),now_ms=62000)
        memory.reconcile('shared',sample(memory,now=62000),
            (sm.MemoryResolution(model.lease_id,'release','fake-qualified-empty',terminal=True),),now_ms=62000)
        assert memory.view('shared').committed_bytes == 0


def test_physical_allocation_before_persisted_dispatch_is_not_credited(tmp_path):
    coord, memory, _ = opened(tmp_path)
    with coord:
        lease = reserve_action(memory,'shared','tool',footprint(),sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='unexpected_allocation'):
            memory.dispatch(lease,sample(memory,attributed=((lease.lease_id,100),)),now_ms=1000)
        assert memory.view('shared').state == 'paused'


def test_combined_policy_cap_cannot_be_evaded_with_fully_attributed_residency(tmp_path):
    coord, memory, _ = opened(tmp_path,limit=700)
    with coord:
        model = memory.reserve_model(load_step(coord),profile(amount=500),sample(memory),now_ms=1000)
        memory.dispatch(model,sample(memory),now_ms=1000)
        with pytest.raises(sm.MemoryDenied,match='budget_exceeded'):
            reserve_action(memory,'shared','tool',footprint(300),
                           sample(memory,available=500,attributed=((model.lease_id,500),)),now_ms=1000)
        assert memory.view('shared').committed_bytes == 500


def test_restart_adopts_warm_memory_only_after_matching_lifecycle_reconciliation(tmp_path):
    coord, memory, fleet = opened(tmp_path)
    step = load_step(coord)
    model = memory.reserve_model(step,profile(),sample(memory),now_ms=1000)
    memory.dispatch(model,sample(memory),now_ms=1000)
    fleet.apply(coord,step,1000)
    fleet.apply(coord,coord.claim_next('shared',now_ms=1000),1000)
    coord.close()
    with lc.LifecycleCoordinator(tmp_path/'owner') as new:
        recovered = sm.SharedMemoryAdmission(new)
        outcomes = (sm.MemoryResolution(model.lease_id,'retain','fake-owned-warm-model',ownership_verified=True),)
        with pytest.raises(sm.MemoryDenied,match='model_ownership_unverified'):
            recovered.reconcile('shared',sample(recovered),outcomes,now_ms=1000)
        new.reconcile('shared',fleet.evidence(new,1000),now_ms=1000,adopt=A)
        adopted, = recovered.reconcile('shared',sample(recovered),outcomes,now_ms=1000)
        assert adopted.owner_epoch != model.owner_epoch
        assert recovered.dispatch(adopted,sample(recovered),now_ms=1000) is False
        assert recovered.view('shared').committed_bytes == 600
        with pytest.raises(sm.MemoryDenied,match='model_release_unverified'):
            recovered.settle(adopted,sample(recovered),terminal=True,reference='still-warm',now_ms=1000)
