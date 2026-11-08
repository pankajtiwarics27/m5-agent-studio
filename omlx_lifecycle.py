"""Unactivated LC-04 oMLX bridge. No HTTP client, probing, or app hook on import.

Pinned wire semantics are NOT installed qualification. All positive attestations
come from a future trusted server producer; no such producer is supplied here.
"""
from __future__ import annotations

import asyncio
import math
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal, Protocol
from urllib.parse import quote

import runtime_persistence
from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp, RuntimeIdentity
from model_lifecycle_coordinator import (
    LifecycleCoordinator, ModelTarget, ReconciliationEvidence, TransitionStep, ReleaseStep, _integer, _label,
)
from shared_memory_admission import MemoryLease, MemorySample, ModelMemoryProfile, SharedMemoryAdmission

OMLX_SOURCE_REVISION = '2165e75726ba83d9651dbaced61c97728da77bba'
_REQUIRED = ('authenticated_control', 'pinned_transport', 'exclusive_access', 'complete_inventory',
             'helper_visibility', 'stream_quiescence', 'operation_terminality', 'profile_binding')


class OmlxError(RuntimeError):
    """Bounded categories only, never upstream bodies, credentials or file paths."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OmlxRequest:
    method: Literal['GET', 'POST']
    path: str
    timeout_s: float


class OmlxTransport(Protocol):
    """Server-owned authenticated/pinned, no-redirect, bounded-JSON transport.

    Receives only fixed relative paths, never a URL/auth token. May not retry
    mutations. Implementations must enforce the registry's pinned origin/IP and
    response size limits. No production implementation is registered by LC-04.
    """
    def __call__(self, request: OmlxRequest) -> Awaitable[tuple[int, Mapping[str, object]]]: ...


@dataclass(frozen=True, slots=True)
class OmlxQualification:
    runtime: RuntimeIdentity
    target: ModelTarget
    runtime_version: str
    source_revision: str
    context_tokens: int
    capabilities: tuple[CapabilityEvidence, ...]
    stamp: ObservationStamp
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeIdentity) or not isinstance(self.target, ModelTarget):
            raise OmlxError('invalid_qualification')
        for value in (self.runtime_version, self.source_revision, self.reference):
            _label(value)
        _integer(self.context_tokens, positive=True)
        if (not isinstance(self.capabilities, tuple) or not all(isinstance(x, CapabilityEvidence) for x in self.capabilities)
                or len({x.name for x in self.capabilities}) != len(self.capabilities)
                or not isinstance(self.stamp, ObservationStamp)):
            raise OmlxError('invalid_qualification')


def validate_unload_qualification(q: OmlxQualification, profile: ModelMemoryProfile, now: int, *, clock_id: str, max_age: float) -> None:
    """Pure source-only readiness, not an oMLX fit/counter/load qualification."""
    if (not isinstance(q, OmlxQualification) or not isinstance(profile, ModelMemoryProfile)
            or q.target != profile.target or q.context_tokens != profile.context_tokens
            or q.runtime.runtime_id != q.target.runtime_id or q.runtime.backend != 'omlx'
            or q.runtime.location != 'local' or q.runtime.egress != 'local-only'
            or q.runtime.ownership_mode not in ('coordinated', 'isolated') or q.source_revision != OMLX_SOURCE_REVISION):
        raise OmlxError('qualification_scope_mismatch')
    if q.stamp.freshness(now=now/1000, clock_id=clock_id, max_age=max_age) != 'fresh':
        raise OmlxError('qualification_stale')
    caps = {x.name: x for x in q.capabilities}
    if any(name not in caps or caps[name].state_for_installed_scope(runtime_id=q.runtime.runtime_id,
            runtime_version=q.runtime_version, profile_fingerprint=q.target.profile_fingerprint) != 'supported' for name in _REQUIRED):
        raise OmlxError('installed_capability_unknown')


def _source_profile_digest(profile: object) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OmlxPermit:
    """Trusted server policy attestation, not a cryptographic grant or HTTP input."""
    step: TransitionStep | ReleaseStep
    qualification: OmlxQualification
    expires_ms: int
    reference: str
    authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.step, (TransitionStep, ReleaseStep)) or not isinstance(self.qualification, OmlxQualification):
            raise OmlxError('invalid_permit')
        _integer(self.expires_ms, positive=True)
        _label(self.reference)
        if type(self.authorized) is not bool:
            raise OmlxError('invalid_permit')


@dataclass(frozen=True, slots=True)
class OmlxActivity:
    """Scoped native facts NOT inferred from the pool status route."""
    step: TransitionStep | ReleaseStep
    instance_id: str | None
    active_streams: int | None
    stamp: ObservationStamp
    reference: str
    pending_operations_terminal: bool = False
    helper_visibility_complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.step, (TransitionStep, ReleaseStep)) or not isinstance(self.stamp, ObservationStamp):
            raise OmlxError('invalid_activity')
        if self.instance_id is not None:
            _label(self.instance_id)
        if self.active_streams is not None:
            _integer(self.active_streams)
        _label(self.reference)
        if type(self.pending_operations_terminal) is not bool or type(self.helper_visibility_complete) is not bool:
            raise OmlxError('invalid_activity')


@dataclass(frozen=True, slots=True)
class OmlxWireModel:
    model_id: str
    loaded: bool
    loading: bool
    pinned: bool
    helper: bool
    distributed: bool
    external: bool
    engine_type: str
    reported_bytes: int
    architectural_context_tokens: int | None


@dataclass(frozen=True, slots=True)
class OmlxObservation:
    """Wire-only snapshot: no native instance/digest/effective context inferred."""
    step: TransitionStep | ReleaseStep
    models: tuple[OmlxWireModel, ...]
    stamp: ObservationStamp
    reference: str


def parse_omlx_status(payload: Mapping[str, object]) -> tuple[OmlxWireModel, ...]:
    """Pinned /v1/models/status shape; missing flags/counts are not false/zero."""
    try:
        raw = payload['models']
        if not isinstance(raw, list) or len(raw) > 1024:
            raise OmlxError('malformed_status')
        count, loaded_count = payload['model_count'], payload['loaded_count']
        _integer(count)
        _integer(loaded_count)
        if count != len(raw):
            raise OmlxError('incomplete_status')
        rows = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise OmlxError('malformed_status')
            mid, engine = item['id'], item['engine_type']
            _label(mid)
            _label(engine)
            if not isinstance(mid, str) or not isinstance(engine, str):
                raise OmlxError('malformed_status')
            if item.get('source_model_id', mid) != mid:
                raise OmlxError('alias_visibility_unsupported')
            flags = tuple(item[name] for name in ('loaded', 'is_loading', 'pinned', 'is_helper', 'distributed'))
            if any(type(x) is not bool for x in flags):
                raise OmlxError('malformed_status')
            size, context = item['actual_size'], item['model_context_length']
            _integer(size)
            if context is not None:
                _integer(context, positive=True)
            external = item['external_engine']
            if external is not None and not isinstance(external, str):
                raise OmlxError('malformed_status')
            rows.append(OmlxWireModel(mid, bool(flags[0]), bool(flags[1]), bool(flags[2]), bool(flags[3]),
                                      bool(flags[4]), external is not None, engine, int(size), context))
        if len({x.model_id for x in rows}) != len(rows) or loaded_count != sum(x.loaded for x in rows):
            raise OmlxError('incomplete_status')
        return tuple(rows)
    except OmlxError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError):
        raise OmlxError('malformed_status') from None


class OmlxLifecycleBridge:
    """Explicitly constructed fake-tested adapter, never registered with the app.

    No fallback admin route, load loop, hidden tokenizer, forced stream abort,
    sleeps or suppressed failures. Callers must separately satisfy LC-07/08/09
    and installed hardware/security qualification before any real activation.
    """
    def __init__(self, coordinator: LifecycleCoordinator, memory: SharedMemoryAdmission,
                 transport: OmlxTransport, clock_ms: Callable[[], int], *, request_timeout_s: float = 30.0,
                 authorize: Callable[[OmlxPermit], bool] | None = None) -> None:
        if (isinstance(request_timeout_s, bool) or not isinstance(request_timeout_s, (int, float))
                or not 0 < request_timeout_s <= 120 or not math.isfinite(request_timeout_s)):
            raise OmlxError('invalid_timeout')
        if memory._coordinator is not coordinator:
            raise OmlxError('memory_owner_mismatch')
        self._coordinator, self._memory = coordinator, memory
        self._transport, self._clock, self._timeout = transport, clock_ms, request_timeout_s
        self._authorize = authorize

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception:
            raise OmlxError('clock_unavailable') from None
        _integer(now)
        return now

    def _gate(self, permit: OmlxPermit, now: int, *, require_fresh: bool = True) -> OmlxQualification:
        if not isinstance(permit, OmlxPermit) or not permit.authorized or now >= permit.expires_ms:
            raise OmlxError('authorization_unavailable')
        if self._authorize is None:
            raise OmlxError('authorization_unavailable')
        try:
            approved = self._authorize(permit)
        except Exception:
            raise OmlxError('authorization_unavailable') from None
        if approved is not True:
            raise OmlxError('authorization_unavailable')
        q, step = permit.qualification, permit.step
        expected = step.source if isinstance(step, ReleaseStep) or step.kind in ('drain', 'unload') else step.target
        if (q.target != expected or q.runtime.runtime_id != q.target.runtime_id or q.runtime.backend != 'omlx'
                or q.runtime.memory_domain_id != step.domain_id or q.runtime.location != 'local'
                or q.runtime.egress != 'local-only' or q.runtime.ownership_mode not in ('coordinated', 'isolated')
                or q.source_revision != OMLX_SOURCE_REVISION):
            raise OmlxError('qualification_scope_mismatch')
        if require_fresh and q.stamp.freshness(now=now/1000, clock_id=self._coordinator.observer_clock_id,
                             max_age=self._coordinator._max_observation_age) != 'fresh':
            raise OmlxError('qualification_stale')
        required = _REQUIRED + (('blocking_load',) if step.kind == 'load' else ())
        caps = {x.name: x for x in q.capabilities}
        if any(name not in caps or caps[name].state_for_installed_scope(
                runtime_id=q.runtime.runtime_id, runtime_version=q.runtime_version,
                profile_fingerprint=q.target.profile_fingerprint) != 'supported' for name in required):
            raise OmlxError('installed_capability_unknown')
        return q

    def _current(self, permit: OmlxPermit, now: int, *, require_fresh: bool = True, finishing: bool = False) -> int:
        self._gate(permit, now, require_fresh=require_fresh)
        def check(conn: sqlite3.Connection) -> int:
            row = (self._coordinator._active_release(conn, permit.step, now, finishing=finishing)
                   if isinstance(permit.step, ReleaseStep) else self._coordinator._active_step(conn, permit.step, now))
            if row is None:
                raise OmlxError('intent_stale')
            binding = self._coordinator._binding(self._coordinator._domain(conn, permit.step.domain_id),
                                                  permit.qualification.runtime.runtime_id)
            if binding != permit.qualification.runtime:
                raise OmlxError('runtime_binding_mismatch')
            return int(row['deadline'])
        return self._coordinator._call(check, now_ms=now)

    def _activity(self, permit: OmlxPermit, activity: OmlxActivity, now: int) -> bool:
        if not isinstance(activity, OmlxActivity) or activity.step != permit.step:
            raise OmlxError('activity_scope_mismatch')
        expected = None if permit.step.kind == 'load' else permit.step.source_instance_id
        if (activity.instance_id != expected or activity.active_streams is None
                or not activity.pending_operations_terminal or not activity.helper_visibility_complete
                or activity.stamp.freshness(now=now/1000, clock_id=self._coordinator.observer_clock_id,
                                           max_age=self._coordinator._max_observation_age) != 'fresh'):
            raise OmlxError('activity_unverified')
        return activity.active_streams == 0

    async def _request(self, permit: OmlxPermit, method: Literal['GET', 'POST'], path: str, *, finishing: bool = False) -> tuple[int, Mapping[str, object]]:
        now = self._now()
        deadline = self._current(permit, now, finishing=finishing)
        timeout = min(self._timeout, (deadline-now)/1000, (permit.expires_ms-now)/1000)
        if timeout <= 0:
            raise OmlxError('intent_stale')
        runtime_persistence.assert_no_active_write_transaction('oMLX transport')
        try:
            response = await asyncio.wait_for(self._transport(OmlxRequest(method, path, timeout)), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OmlxError('transport_unavailable') from None
        if (not isinstance(response, tuple) or len(response) != 2 or type(response[0]) is not int
                or not 100 <= response[0] <= 599 or not isinstance(response[1], Mapping)):
            raise OmlxError('malformed_response')
        return response

    async def observe(self, permit: OmlxPermit, *, finishing: bool = False) -> OmlxObservation:
        status, payload = await self._request(permit, 'GET', '/v1/models/status', finishing=finishing)
        if status != 200:
            raise OmlxError('observation_unavailable', status_code=status)
        rows = parse_omlx_status(payload)
        self._current(permit, self._now(), finishing=finishing)
        return OmlxObservation(permit.step, rows, ObservationStamp(self._now()/1000,
                                self._coordinator.observer_clock_id), uuid.uuid4().hex)

    @staticmethod
    def _wire_ready(observation: OmlxObservation, step: TransitionStep | ReleaseStep, *, empty: bool) -> None:
        occupied = [x for x in observation.models if x.loaded or x.loading or x.reported_bytes > 0]
        if any(x.loading or x.helper or x.distributed or x.external or x.engine_type not in ('llm', 'vlm') for x in occupied):
            raise OmlxError('resident_visibility_unsupported')
        if empty:
            if occupied:
                raise OmlxError('resident_not_empty')
        elif len(occupied) != 1 or step.source is None or occupied[0].model_id != step.source.artifact_id:
            raise OmlxError('source_instance_unverified')
        elif occupied[0].pinned:
            raise OmlxError('pinned_source')

    def _release_binding(self, conn: sqlite3.Connection, permit: OmlxPermit) -> None:
        step = permit.step
        if not isinstance(step, ReleaseStep):
            raise OmlxError('release_intent_required')
        row = conn.execute("SELECT * FROM memory_reservations WHERE id=?", (step.memory_id,)).fetchone()
        if row is None or row['status'] != 'dispatched' or row['domain'] != step.domain_id or row['kind'] != 'model' or row['owner_epoch'] != self._coordinator._epoch:
            raise OmlxError('source_memory_unverified')
        profile = json.loads(row['binding'])['profile']
        if profile['target'] != asdict(step.source) or profile['context_tokens'] != permit.qualification.context_tokens:
            raise OmlxError('source_memory_unverified')
        record = conn.execute("SELECT reference FROM journal WHERE domain=? AND operation_id=? AND event='omlx_profile_load'", (step.domain_id, row['work_id'])).fetchone()
        if record is None or record['reference'] != _source_profile_digest(profile):
            raise OmlxError('source_profile_unverified')

    def _release_ack(self, permit: OmlxPermit, now: int) -> None:
        step = permit.step
        if not isinstance(step, ReleaseStep):
            raise OmlxError('release_intent_required')
        def ack(conn: sqlite3.Connection) -> None:
            if self._coordinator._active_release(conn, step, now, finishing=True) is None:
                raise OmlxError('intent_stale')
            self._coordinator._event(conn, step.domain_id, 'omlx_ack_release_unload', step.operation_id, permit.reference)
        self._coordinator._call(ack, now_ms=now)

    def drain(self, permit: OmlxPermit, activity: OmlxActivity) -> bool:
        if permit.step.kind != 'drain':
            raise OmlxError('wrong_phase')
        now = self._now()
        self._current(permit, now)
        if not self._activity(permit, activity, now):
            return False
        if isinstance(permit.step, ReleaseStep):
            self._coordinator._call(lambda conn: self._release_binding(conn, permit), now_ms=now)
            return self._coordinator.complete_release(permit.step, reference=activity.reference, now_ms=now)
        return self._coordinator.complete_step(permit.step, reference=activity.reference, now_ms=now)

    def _claim_send(self, permit: OmlxPermit, activity: OmlxActivity, observation: OmlxObservation,
                    lease: MemoryLease | None, sample: MemorySample | None) -> None:
        now = self._now()
        self._current(permit, now)
        if not self._activity(permit, activity, now):
            raise OmlxError('streams_not_drained')
        step = permit.step
        if observation.step != step or observation.stamp.freshness(
                now=now/1000, clock_id=self._coordinator.observer_clock_id,
                max_age=self._coordinator._max_observation_age) != 'fresh':
            raise OmlxError('observation_stale')
        event = 'omlx_send_' + ('release_' if isinstance(step, ReleaseStep) else '') + step.kind
        def claim(conn: sqlite3.Connection) -> None:
            active = (self._coordinator._active_release(conn, step, now) if isinstance(step, ReleaseStep) else self._coordinator._active_step(conn, step, now))
            if active is None:
                raise OmlxError('intent_stale')
            if isinstance(step, ReleaseStep):
                self._release_binding(conn, permit)
            if conn.execute('SELECT 1 FROM journal WHERE operation_id=? AND event=?', (step.operation_id,event)).fetchone():
                raise OmlxError('effect_already_dispatched')
            if conn.execute("SELECT 1 FROM leases WHERE domain=? AND status='active'", (step.domain_id,)).fetchone():
                raise OmlxError('streams_not_drained')
            if step.kind == 'load':
                if lease is None or sample is None or lease.kind != 'model' or lease.work_id != step.operation_id:
                    raise OmlxError('memory_reservation_required')
                if not self._memory._dispatch(conn, lease, sample, now):
                    raise OmlxError('effect_already_dispatched')
                self._coordinator._event(conn, step.domain_id, 'omlx_profile_load', step.operation_id, _source_profile_digest(json.loads(lease.binding)['profile']))
            self._coordinator._event(conn, step.domain_id, event, step.operation_id, permit.reference)
        if step.kind == 'load':
            if sample is None:
                raise OmlxError('memory_reservation_required')
            self._memory._admission_call(claim, sample, now)
        else:
            self._coordinator._call(claim, now_ms=now)

    def _hold(self, step: TransitionStep | ReleaseStep, lease: MemoryLease | None, status_code: int | None = None) -> None:
        now = self._now()
        reference = 'omlx_effect_uncertain' + (f'_http_{status_code}' if status_code is not None else '')
        if isinstance(step, ReleaseStep):
            self._coordinator.mark_release_uncertain(step, reference=reference, now_ms=now)
            return
        def hold(conn: sqlite3.Connection) -> None:
            row = conn.execute('SELECT * FROM operations WHERE id=?', (step.operation_id,)).fetchone()
            domain = self._coordinator._domain(conn, step.domain_id)
            if (row is None or row['owner_epoch'] != self._coordinator._epoch or row['generation'] != step.generation
                    or domain['operation_id'] != step.operation_id or domain['generation'] != step.generation
                    or row['stage'] != step.kind or row['status'] not in ('active', 'uncertain')):
                return
            self._coordinator._uncertain(conn, step.domain_id, step.operation_id, reference)
            if lease is not None:
                self._memory._active(conn, lease)
                conn.execute("UPDATE memory_reservations SET status='uncertain' WHERE id=?", (lease.lease_id,))
                conn.execute("UPDATE memory_domains SET state='paused' WHERE domain=?", (lease.domain_id,))
                self._memory._change(conn, lease.domain_id, 'memory_uncertain', reference)
        self._coordinator._call(hold, now_ms=now)

    async def send(self, permit: OmlxPermit, activity: OmlxActivity, *, lease: MemoryLease | None = None,
                   sample: MemorySample | None = None, profile: ModelMemoryProfile | None = None) -> Literal['pending', 'verify']:
        """One mutation per durable intent. No retry even after client cancellation."""
        q = self._gate(permit, self._now())
        step = permit.step
        if step.kind not in ('load', 'unload'):
            raise OmlxError('wrong_phase')
        if step.kind == 'load':
            if not isinstance(profile, ModelMemoryProfile) or profile.target != q.target or profile.context_tokens != q.context_tokens:
                raise OmlxError('model_profile_mismatch')
            if lease is None or sample is None:
                raise OmlxError('memory_reservation_required')
            try:
                if json.loads(lease.binding).get('profile') != asdict(profile):
                    raise OmlxError('model_profile_mismatch')
            except (ValueError, TypeError, AttributeError):
                raise OmlxError('model_profile_mismatch') from None
        elif lease is not None or sample is not None or profile is not None:
            raise OmlxError('unexpected_load_reservation')
        model = q.target.artifact_id
        if model in ('.', '..'):
            raise OmlxError('invalid_model_route')
        observation = await self.observe(permit)
        self._wire_ready(observation, step, empty=step.kind == 'load')
        self._claim_send(permit, activity, observation, lease, sample)
        path = '/v1/models/' + quote(model, safe='') + '/' + step.kind
        status: int | None = None
        try:
            status, payload = await self._request(permit, 'POST', path)
            now = self._now()
            self._current(permit, now, require_fresh=False, finishing=isinstance(step, ReleaseStep))
            if status not in (200, 202) or payload.get('model_id') != model:
                raise OmlxError('native_operation_failed')
            if status == 202:
                if step.kind != 'unload' or payload.get('status') != 'unloading':
                    raise OmlxError('native_operation_failed')
                if isinstance(step, ReleaseStep):
                    self._release_ack(permit, now)
                elif not self._coordinator.note_pending(step, now_ms=now):
                    raise OmlxError('intent_stale')
                return 'pending'
            if payload.get('status') != 'ok':
                raise OmlxError('native_operation_failed')
            if step.kind == 'unload':
                if isinstance(step, ReleaseStep):
                    self._release_ack(permit, now)
                elif not self._coordinator.note_pending(step, now_ms=now):
                    raise OmlxError('intent_stale')
                return 'pending'
            if not isinstance(step, TransitionStep) or not self._coordinator.complete_step(step, reference='omlx_blocking_load_returned', now_ms=now):
                raise OmlxError('intent_stale')
            return 'verify'
        except asyncio.CancelledError:
            self._hold(step, lease)
            raise
        except Exception:
            self._hold(step, lease, status)
            raise OmlxError('native_operation_uncertain', status_code=status) from None

    async def confirm(self, permit: OmlxPermit, produce: Callable[[OmlxObservation], Awaitable[ReconciliationEvidence]]) -> bool:
        """No proof producer is supplied; status/ACK alone cannot advance the queue."""
        step = permit.step
        if step.kind not in ('unload', 'verify'):
            raise OmlxError('wrong_phase')
        if isinstance(step, ReleaseStep):
            self._current(permit, self._now(), finishing=True)
            def ack(conn: sqlite3.Connection) -> None:
                if conn.execute("SELECT 1 FROM journal WHERE domain=? AND operation_id=? AND owner_epoch=? AND generation=? AND event='omlx_ack_release_unload'", (step.domain_id, step.operation_id, step.owner_epoch, step.generation)).fetchone() is None:
                    raise OmlxError('release_ack_missing')
            self._coordinator._call(ack, now_ms=self._now())
        observation = await self.observe(permit, finishing=isinstance(step, ReleaseStep))
        if step.kind == 'unload':
            self._wire_ready(observation, step, empty=True)
        elif isinstance(step, TransitionStep):
            occupied = [x for x in observation.models if x.loaded or x.loading or x.reported_bytes > 0]
            if (len(occupied) != 1 or occupied[0].model_id != step.target.artifact_id or not occupied[0].loaded
                    or occupied[0].loading or occupied[0].helper or occupied[0].external or occupied[0].distributed):
                raise OmlxError('target_unverified')
        runtime_persistence.assert_no_active_write_transaction('oMLX evidence producer')
        try:
            evidence = await produce(observation)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OmlxError('proof_unavailable') from None
        now = self._now()
        self._current(permit, now, finishing=isinstance(step, ReleaseStep))
        if (not isinstance(evidence, ReconciliationEvidence) or evidence.reference != observation.reference
                or evidence.clock_id != observation.stamp.clock_id):
            raise OmlxError('proof_scope_mismatch')
        inventory = next((x for x in evidence.inventories if x.runtime == permit.qualification.runtime), None)
        if (inventory is None or inventory.runtime_version != permit.qualification.runtime_version or inventory.stamp.clock_id != observation.stamp.clock_id
                or inventory.stamp.observed_at is None or observation.stamp.observed_at is None
                or inventory.stamp.observed_at < observation.stamp.observed_at):
            raise OmlxError('proof_scope_mismatch')
        if isinstance(step, TransitionStep) and step.kind == 'verify' and not any(x.effective_context_tokens == permit.qualification.context_tokens
                                             for x in inventory.instances if x.artifact_id == step.target.artifact_id):
            raise OmlxError('context_unverified')
        if isinstance(step, ReleaseStep):
            return self._coordinator.complete_release(step, reference=observation.reference, now_ms=now, evidence=evidence)
        return self._coordinator.complete_step(step, reference=observation.reference, now_ms=now, evidence=evidence)

    async def tokenize(self) -> None:
        """No cold-load-capable token counter until LC-07's qualified preparation."""
        raise OmlxError('tokenizer_admission_unavailable')
