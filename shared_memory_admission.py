"""Dormant LC-03 accounting, NOT execution authority or a native memory collector.

Uses LC-02's lock/epoch/short transaction boundary. No app integration, I/O on
import, automatic release on cancellation, clock discovery or hardware defaults.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal, TypeVar

from local_runtime_lifecycle import ObservationStamp
from model_lifecycle_coordinator import LifecycleCoordinator, ModelTarget, TransitionStep, _integer, _label

if TYPE_CHECKING:
    from execution_profiles import ResourceFootprint

_T = TypeVar('_T')


class MemoryDenied(RuntimeError):
    """Bounded denial category, with no model, file, or upstream content."""


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    limit_bytes: int
    reserve_bytes: int
    reference: str

    def __post_init__(self) -> None:
        _integer(self.limit_bytes, positive=True)
        _integer(self.reserve_bytes, positive=True)
        _label(self.reference)


@dataclass(frozen=True, slots=True)
class ModelMemoryProfile:
    """Trusted qualified upper bounds, not artifact size or a fit estimator.

    All components are explicit, including a justified zero. The conservative
    sum retains load-transient capacity even while warm. No context resizing.
    """
    target: ModelTarget
    context_tokens: int
    weights_bytes: int
    kv_bytes: int
    workspace_bytes: int
    runtime_bytes: int
    auxiliary_bytes: int
    load_transient_bytes: int
    qualification_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, ModelTarget):
            raise MemoryDenied('invalid_profile')
        _integer(self.context_tokens, positive=True)
        _label(self.qualification_ref)
        for amount in self.components:
            _integer(amount)
        _integer(self.peak_bytes, positive=True)

    @property
    def components(self) -> tuple[int, ...]:
        return (self.weights_bytes, self.kv_bytes, self.workspace_bytes, self.runtime_bytes,
                self.auxiliary_bytes, self.load_transient_bytes)

    @property
    def peak_bytes(self) -> int:
        return sum(self.components)


@dataclass(frozen=True, slots=True)
class ActionMemoryBound:
    """Separate trusted upper-bound attestation, never inferred from tool defaults."""
    footprint_digest: str
    peak_bytes: int
    qualification_ref: str

    def __post_init__(self) -> None:
        _label(self.footprint_digest)
        if len(self.footprint_digest) != 64 or any(c not in '0123456789abcdef' for c in self.footprint_digest):
            raise MemoryDenied('invalid_action_bound')
        _integer(self.peak_bytes, positive=True)
        _label(self.qualification_ref)


@dataclass(frozen=True, slots=True)
class MemoryScope:
    domain_id: str
    owner_epoch: int
    revision: int
    lifecycle_generation: int
    clock_id: str

    def __post_init__(self) -> None:
        _label(self.domain_id)
        _label(self.clock_id)
        _integer(self.owner_epoch, positive=True)
        _integer(self.revision)
        _integer(self.lifecycle_generation)


@dataclass(frozen=True, slots=True)
class MemorySample:
    scope: MemoryScope
    stamp: ObservationStamp
    total_bytes: int | None
    available_bytes: int | None
    pressure: Literal['normal', 'elevated', 'critical', 'unknown']
    attributed_bytes: tuple[tuple[str, int], ...]
    reference: str
    complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MemoryScope) or not isinstance(self.stamp, ObservationStamp):
            raise MemoryDenied('invalid_sample')
        for value in (self.total_bytes, self.available_bytes):
            if value is not None:
                _integer(value)
        if self.pressure not in ('normal', 'elevated', 'critical', 'unknown') or type(self.complete) is not bool:
            raise MemoryDenied('invalid_sample')
        if not isinstance(self.attributed_bytes, tuple):
            raise MemoryDenied('invalid_attribution')
        for pair in self.attributed_bytes:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise MemoryDenied('invalid_attribution')
            _label(pair[0])
            _integer(pair[1])
        _label(self.reference)


@dataclass(frozen=True, slots=True)
class MemoryLease:
    lease_id: str
    domain_id: str
    work_id: str
    kind: str
    peak_bytes: int
    binding: str
    owner_epoch: int


@dataclass(frozen=True, slots=True)
class MemoryView:
    scope: MemoryScope
    policy: MemoryPolicy
    state: str
    leases: tuple[MemoryLease, ...]
    undispatched_ids: tuple[str, ...]

    @property
    def committed_bytes(self) -> int:
        return sum(x.peak_bytes for x in self.leases)


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    lease_id: str
    disposition: Literal['retain', 'release']
    reference: str
    terminal: bool = False
    ownership_verified: bool = False

    def __post_init__(self) -> None:
        _label(self.lease_id)
        _label(self.reference)
        if (self.disposition not in ('retain', 'release') or type(self.terminal) is not bool
                or type(self.ownership_verified) is not bool):
            raise MemoryDenied('invalid_resolution')


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _lease(row: sqlite3.Row) -> MemoryLease:
    for name in ('id', 'domain', 'work_id'):
        _label(row[name])
    _integer(row['peak_bytes'], positive=True)
    _integer(row['owner_epoch'], positive=True)
    raw = json.loads(row['binding'])
    if row['kind'] == 'model':
        if set(raw) != {'profile', 'step'}:
            raise MemoryDenied('memory_binding_invalid')
        data = dict(raw['profile'])
        data['target'] = ModelTarget(**data['target'])
        profile = ModelMemoryProfile(**data)
        step = _model_step(row['binding'])
        if (profile.peak_bytes != row['peak_bytes'] or profile.target != step.target or step.kind != 'load'
                or step.operation_id != row['work_id'] or step.domain_id != row['domain']
                or step.owner_epoch > row['owner_epoch']):
            raise MemoryDenied('memory_binding_invalid')
    elif row['kind'] == 'action':
        if set(raw) != {'footprint_digest', 'memory_bytes', 'action_slots', 'subprocess_slots', 'mutation_slots', 'peak_bytes', 'qualification_ref'}:
            raise MemoryDenied('memory_binding_invalid')
        digest = raw['footprint_digest']
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                or raw['peak_bytes'] != row['peak_bytes'] or raw['memory_bytes'] != row['peak_bytes']):
            raise MemoryDenied('memory_binding_invalid')
        ActionMemoryBound(digest, raw['peak_bytes'], raw['qualification_ref'])
        for name in ('memory_bytes', 'action_slots'):
            _integer(raw[name], positive=True)
        for name in ('subprocess_slots', 'mutation_slots'):
            _integer(raw[name])
    else:
        raise MemoryDenied('memory_binding_invalid')
    return MemoryLease(row['id'], row['domain'], row['work_id'], row['kind'], row['peak_bytes'],
                       row['binding'], row['owner_epoch'])


def _model_step(binding: str) -> TransitionStep:
    raw = json.loads(binding)['step']
    raw['target'] = ModelTarget(**raw['target'])
    if raw['source'] is not None:
        raw['source'] = ModelTarget(**raw['source'])
    return TransitionStep(**raw)


class SharedMemoryAdmission:
    """One ledger for cooperating model/action work within a declared domain.

    Internal LC-02 extension: uses its fenced transaction, not a second lock or
    independent pool. Consumers must separately pass policy/grants/barriers,
    native qualification, full-request fit and all-path enforcement gates.
    """
    def __init__(self, coordinator: LifecycleCoordinator) -> None:
        self._coordinator = coordinator

    def _call(self, fn: Callable[[sqlite3.Connection], _T], now_ms: int | None = None) -> _T:
        return self._coordinator._call(fn, now_ms=now_ms)

    def _admission_call(self, fn: Callable[[sqlite3.Connection], _T], sample: MemorySample, now_ms: int) -> _T:
        # Commit an unsafe-observation hold before surfacing its denial. All
        # checks precede reservation changes; this is not a partial-effect retry.
        def attempt(conn: sqlite3.Connection) -> _T | MemoryDenied:
            try:
                return fn(conn)
            except MemoryDenied as exc:
                if str(exc) not in {'memory_capacity_unknown', 'memory_pressure_unsafe',
                                    'memory_attribution_incomplete', 'memory_bound_exceeded', 'memory_unexpected_allocation'}:
                    raise
                conn.execute("UPDATE memory_domains SET state='paused' WHERE domain=?", (sample.scope.domain_id,))
                self._change(conn, sample.scope.domain_id, 'memory_observation_unsafe', sample.reference)
                return exc
        result = self._call(attempt, now_ms)
        if isinstance(result, MemoryDenied):
            raise result
        return result

    def _view(self, conn: sqlite3.Connection, domain_id: str) -> MemoryView:
        row = conn.execute('SELECT * FROM memory_domains WHERE domain=?', (domain_id,)).fetchone()
        if row is None:
            raise MemoryDenied('memory_domain_unconfigured')
        epoch = conn.execute('SELECT epoch FROM owner WHERE singleton=1').fetchone()[0]
        rows = tuple(conn.execute(
            "SELECT * FROM memory_reservations WHERE domain=? AND status!='released' ORDER BY id", (domain_id,)))
        leases = tuple(_lease(x) for x in rows)
        pending = tuple(x['id'] for x in rows if x['status'] == 'reserved')
        return MemoryView(MemoryScope(domain_id, epoch, row['revision'], self._coordinator._domain(conn, domain_id)['generation'], self._coordinator.observer_clock_id),
                          MemoryPolicy(row['limit_bytes'], row['reserve_bytes'], row['policy_ref']), row['state'], leases, pending)

    def view(self, domain_id: str) -> MemoryView:
        _label(domain_id)
        return self._call(lambda conn: self._view(conn, domain_id))

    def _change(self, conn: sqlite3.Connection, domain_id: str, event: str, reference: str) -> None:
        revision = self._view(conn, domain_id).scope.revision + 1
        _integer(revision)
        conn.execute('UPDATE memory_domains SET revision=? WHERE domain=?', (revision, domain_id))
        self._coordinator._event(conn, domain_id, event, ref=reference)

    def configure(self, domain_id: str, policy: MemoryPolicy, *, now_ms: int) -> None:
        _label(domain_id)
        if not isinstance(policy, MemoryPolicy):
            raise MemoryDenied('invalid_policy')
        def configure(conn: sqlite3.Connection) -> None:
            self._coordinator._domain(conn, domain_id)
            row = conn.execute('SELECT * FROM memory_domains WHERE domain=?', (domain_id,)).fetchone()
            if row is None and conn.execute('SELECT 1 FROM memory_domains').fetchone() is not None:
                raise MemoryDenied('memory_domain_conflict')
            if row is not None:
                if self._view(conn, domain_id).policy != policy:
                    raise MemoryDenied('memory_policy_changed')
                return
            conn.execute("INSERT INTO memory_domains VALUES(?,?,?,0,'reconciling',?)",
                         (domain_id, policy.limit_bytes, policy.reserve_bytes, policy.reference))
            self._change(conn, domain_id, 'memory_configured', policy.reference)
        self._call(configure, now_ms)

    def _validate(self, view: MemoryView, sample: MemorySample, now_ms: int, *, admission: bool) -> dict[str, int]:
        if not isinstance(sample, MemorySample) or sample.scope != view.scope:
            raise MemoryDenied('memory_scope_stale')
        if sample.stamp.freshness(now=now_ms / 1000, clock_id=view.scope.clock_id,
                                  max_age=self._coordinator._max_observation_age) != 'fresh':
            raise MemoryDenied('memory_observation_stale')
        if (not sample.complete or sample.total_bytes is None or sample.available_bytes is None
                or sample.total_bytes <= 0 or sample.available_bytes > sample.total_bytes):
            raise MemoryDenied('memory_capacity_unknown')
        attributed = dict(sample.attributed_bytes)
        if (len(attributed) != len(sample.attributed_bytes)
                or set(attributed) != {x.lease_id for x in view.leases}
                or sum(attributed.values()) > sample.total_bytes - sample.available_bytes):
            raise MemoryDenied('memory_attribution_incomplete')
        if any(attributed[lease_id] != 0 for lease_id in view.undispatched_ids):
            raise MemoryDenied('memory_unexpected_allocation')
        if any(attributed[x.lease_id] > x.peak_bytes for x in view.leases):
            raise MemoryDenied('memory_bound_exceeded')
        if admission and sample.pressure != 'normal':
            raise MemoryDenied('memory_pressure_unsafe')
        return attributed

    @staticmethod
    def _fits(view: MemoryView, sample: MemorySample, attributed: dict[str, int], extra: int) -> None:
        assert sample.available_bytes is not None
        if view.committed_bytes + extra > view.policy.limit_bytes:
            raise MemoryDenied('memory_budget_exceeded')
        unallocated = sum(x.peak_bytes - attributed[x.lease_id] for x in view.leases)
        if unallocated + extra + view.policy.reserve_bytes > sample.available_bytes:
            raise MemoryDenied('memory_headroom_insufficient')

    def _ready(self, conn: sqlite3.Connection, view: MemoryView) -> None:
        domain = self._coordinator._domain(conn, view.scope.domain_id)
        if view.state != 'ready' or domain['state'] == 'reconciling':
            raise MemoryDenied('memory_reconciliation_required')

    def _reserve(self, conn: sqlite3.Connection, domain_id: str, work_id: str, kind: str,
                 peak: int, binding: str, sample: MemorySample, now_ms: int) -> MemoryLease:
        view = self._view(conn, domain_id)
        self._ready(conn, view)
        attributed = self._validate(view, sample, now_ms, admission=True)
        previous = conn.execute('SELECT * FROM memory_reservations WHERE domain=? AND work_id=?',
                                (domain_id, work_id)).fetchone()
        if previous is not None:
            if (previous['status'] == 'released' or previous['owner_epoch'] != view.scope.owner_epoch
                    or (previous['kind'], previous['peak_bytes'], previous['binding']) != (kind, peak, binding)):
                raise MemoryDenied('memory_idempotency_conflict')
            self._fits(view, sample, attributed, 0)
            return _lease(previous)
        if kind == 'model' and any(x.kind == 'model' for x in view.leases):
            raise MemoryDenied('memory_model_already_reserved')
        self._fits(view, sample, attributed, peak)
        lease = MemoryLease(uuid.uuid4().hex, domain_id, work_id, kind, peak, binding, view.scope.owner_epoch)
        conn.execute("INSERT INTO memory_reservations VALUES(?,?,?,?,?,?,?,'reserved')",
                     (lease.lease_id, domain_id, work_id, kind, peak, binding, lease.owner_epoch))
        self._change(conn, domain_id, 'memory_reserved', work_id)
        return lease

    def reserve_model(self, step: TransitionStep, profile: ModelMemoryProfile, sample: MemorySample,
                      *, now_ms: int) -> MemoryLease:
        if not isinstance(step, TransitionStep) or not isinstance(profile, ModelMemoryProfile) or profile.target != step.target:
            raise MemoryDenied('model_profile_mismatch')
        def reserve(conn: sqlite3.Connection) -> MemoryLease:
            if step.kind != 'load' or self._coordinator._active_step(conn, step, now_ms) is None:
                raise MemoryDenied('lifecycle_intent_stale')
            return self._reserve(conn, step.domain_id, step.operation_id, 'model', profile.peak_bytes,
                                 _json({'profile': asdict(profile), 'step': asdict(step)}), sample, now_ms)
        return self._admission_call(reserve, sample, now_ms)

    @staticmethod
    def _action_binding(footprint: ResourceFootprint, bound: ActionMemoryBound | None) -> tuple[int, str]:
        from execution_profiles import ResourceFootprint
        if not isinstance(footprint, ResourceFootprint):
            raise MemoryDenied('invalid_action_footprint')
        request = footprint.to_resource_request()
        _integer(footprint.subprocess_slots)
        _integer(footprint.mutation_slots)
        digest = hashlib.sha256(_json(footprint.to_mapping()).encode()).hexdigest()
        if not isinstance(bound, ActionMemoryBound):
            raise MemoryDenied('action_bound_unknown')
        if bound.footprint_digest != digest or bound.peak_bytes != request.memory_bytes:
            raise MemoryDenied('action_bound_mismatch')
        # Subprocess/mutation slot counters are NOT additional physical pools.
        return bound.peak_bytes, _json({'footprint_digest': digest, 'memory_bytes': request.memory_bytes,
                                          'peak_bytes': bound.peak_bytes, 'qualification_ref': bound.qualification_ref,
                                          'action_slots': request.slots, 'subprocess_slots': footprint.subprocess_slots,
                                          'mutation_slots': footprint.mutation_slots})

    def reserve_action(self, domain_id: str, work_id: str, footprint: ResourceFootprint,
                       sample: MemorySample, *, bound: ActionMemoryBound | None = None, now_ms: int) -> MemoryLease:
        _label(domain_id)
        _label(work_id)
        peak, binding = self._action_binding(footprint, bound)
        return self._admission_call(lambda conn: self._reserve(conn, domain_id, work_id, 'action', peak, binding, sample, now_ms), sample, now_ms)

    def _active(self, conn: sqlite3.Connection, lease: MemoryLease) -> sqlite3.Row:
        if not isinstance(lease, MemoryLease):
            raise MemoryDenied('memory_lease_fenced')
        row = conn.execute("SELECT * FROM memory_reservations WHERE id=? AND status!='released'", (lease.lease_id,)).fetchone()
        view = self._view(conn, lease.domain_id)
        if row is None or _lease(row) != lease or lease.owner_epoch != view.scope.owner_epoch:
            raise MemoryDenied('memory_lease_fenced')
        return row

    def dispatch(self, lease: MemoryLease, sample: MemorySample, *, now_ms: int) -> bool:
        """Persist before any external effect; repeated dispatch never means replay."""
        return self._admission_call(lambda conn: self._dispatch(conn, lease, sample, now_ms), sample, now_ms)

    def _dispatch(self, conn: sqlite3.Connection, lease: MemoryLease, sample: MemorySample, now_ms: int) -> bool:
        """Internal composition seam: caller owns the same fenced transaction."""
        row = self._active(conn, lease)
        view = self._view(conn, lease.domain_id)
        self._ready(conn, view)
        attributed = self._validate(view, sample, now_ms, admission=True)
        self._fits(view, sample, attributed, 0)
        if row['status'] != 'reserved':
            return False
        if lease.kind == 'model':
            step = _model_step(lease.binding)
            if self._coordinator._active_step(conn, step, now_ms) is None:
                raise MemoryDenied('lifecycle_intent_stale')
        conn.execute("UPDATE memory_reservations SET status='dispatched' WHERE id=?", (lease.lease_id,))
        self._change(conn, lease.domain_id, 'memory_dispatched', lease.work_id)
        return True

    def expand_action(self, lease: MemoryLease, footprint: ResourceFootprint, sample: MemorySample,
                      *, bound: ActionMemoryBound | None = None, now_ms: int) -> MemoryLease:
        """Reserve growth BEFORE allocation; does not expand grants/barriers."""
        peak, binding = self._action_binding(footprint, bound)
        def expand(conn: sqlite3.Connection) -> MemoryLease:
            self._active(conn, lease)
            view = self._view(conn, lease.domain_id)
            self._ready(conn, view)
            if lease.kind != 'action' or peak <= lease.peak_bytes:
                raise MemoryDenied('memory_expansion_invalid')
            attributed = self._validate(view, sample, now_ms, admission=True)
            self._fits(view, sample, attributed, peak - lease.peak_bytes)
            conn.execute('UPDATE memory_reservations SET peak_bytes=?,binding=? WHERE id=?', (peak, binding, lease.lease_id))
            self._change(conn, lease.domain_id, 'memory_expanded', lease.work_id)
            return MemoryLease(lease.lease_id, lease.domain_id, lease.work_id, 'action', peak, binding, lease.owner_epoch)
        return self._admission_call(expand, sample, now_ms)

    def mark_uncertain(self, lease: MemoryLease, *, reference: str, now_ms: int) -> None:
        _label(reference)
        def uncertain(conn: sqlite3.Connection) -> None:
            self._active(conn, lease)
            conn.execute("UPDATE memory_reservations SET status='uncertain' WHERE id=?", (lease.lease_id,))
            conn.execute("UPDATE memory_domains SET state='paused' WHERE domain=?", (lease.domain_id,))
            self._change(conn, lease.domain_id, 'memory_uncertain', reference)
        self._call(uncertain, now_ms)

    def _model_release_verified(self, conn: sqlite3.Connection, lease: MemoryLease) -> None:
        if lease.kind != 'model':
            return
        domain = self._coordinator._domain(conn, lease.domain_id)
        operation = conn.execute('SELECT status FROM operations WHERE id=?', (lease.work_id,)).fetchone()
        if domain['resident'] is not None or operation is None or operation['status'] not in ('complete', 'reconciled'):
            raise MemoryDenied('model_release_unverified')

    def settle(self, lease: MemoryLease, sample: MemorySample, *, terminal: bool = False,
               reference: str, now_ms: int) -> None:
        """Terminal AND attributed zero are required; cancel/timeout is neither."""
        _label(reference)
        def settle(conn: sqlite3.Connection) -> None:
            self._active(conn, lease)
            view = self._view(conn, lease.domain_id)
            attributed = self._validate(view, sample, now_ms, admission=False)
            if terminal is not True or attributed[lease.lease_id] != 0:
                raise MemoryDenied('memory_release_unverified')
            self._model_release_verified(conn, lease)
            conn.execute("UPDATE memory_reservations SET status='released' WHERE id=?", (lease.lease_id,))
            self._change(conn, lease.domain_id, 'memory_released', reference)
        self._call(settle, now_ms)

    def reconcile(self, domain_id: str, sample: MemorySample, resolutions: tuple[MemoryResolution, ...],
                  *, now_ms: int) -> tuple[MemoryLease, ...]:
        """Retain qualified live bounds or retire terminal zero allocations, never replay."""
        _label(domain_id)
        if not isinstance(resolutions, tuple) or not all(isinstance(x, MemoryResolution) for x in resolutions):
            raise MemoryDenied('invalid_resolution')
        def reconcile(conn: sqlite3.Connection) -> tuple[MemoryLease, ...]:
            view = self._view(conn, domain_id)
            attributed = self._validate(view, sample, now_ms, admission=False)
            outcomes = {x.lease_id: x for x in resolutions}
            if len(outcomes) != len(resolutions) or set(outcomes) != {x.lease_id for x in view.leases}:
                raise MemoryDenied('memory_recovery_incomplete')
            for lease in view.leases:
                outcome = outcomes[lease.lease_id]
                if outcome.disposition == 'release':
                    if not outcome.terminal or attributed[lease.lease_id] != 0:
                        raise MemoryDenied('memory_release_unverified')
                    self._model_release_verified(conn, lease)
                    conn.execute("UPDATE memory_reservations SET status='released' WHERE id=?", (lease.lease_id,))
                else:
                    if not outcome.ownership_verified:
                        raise MemoryDenied('memory_ownership_unverified')
                    if lease.kind == 'model':
                        from model_lifecycle_coordinator import _decode
                        target = _model_step(lease.binding).target
                        domain = self._coordinator._domain(conn, domain_id)
                        if domain['state'] == 'reconciling' or _decode(domain['resident']) != target:
                            raise MemoryDenied('model_ownership_unverified')
                    conn.execute("UPDATE memory_reservations SET status='dispatched',owner_epoch=? WHERE id=?",
                                 (view.scope.owner_epoch, lease.lease_id))
            conn.execute("UPDATE memory_domains SET state='ready' WHERE domain=?", (domain_id,))
            self._change(conn, domain_id, 'memory_reconciled', sample.reference)
            return self._view(conn, domain_id).leases
        return self._call(reconcile, now_ms)
