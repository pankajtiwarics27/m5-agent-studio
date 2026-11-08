"""Dormant LC-07 post-residency counting/fit authority. No production producers.

Counts are scoped trusted attestations, not self-authenticating native facts.
All callbacks doing I/O run outside SQLite; dispatch lookup is memory-only.
"""

from __future__ import annotations

import asyncio
import math
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from model_lifecycle_coordinator import LifecycleCoordinator, RequestLease, _encode
from request_fit import (
    Confidence,
    FitDiagnostic,
    FitProfile,
    PreparedRequest,
    RequestFitDenied,
    assess_fit,
    digest,
    number,
    reference,
    verify_prepared,
)
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import (
    MemoryDenied,
    MemoryLease,
    MemorySample,
    SharedMemoryAdmission,
)

CAPABILITIES = (
    "full_request_counter",
    "template_parity",
    "strict_input",
    "reserve_semantics",
    "bounded_count_workspace",
)


@dataclass(frozen=True, slots=True)
class FitPermit:
    lease: RequestLease
    body_digest: str
    expires_ms: int
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, RequestLease):
            raise RequestFitDenied("invalid_request")
        digest(self.body_digest)
        number(self.expires_ms, positive=True)
        reference(self.reference)


@dataclass(frozen=True, slots=True)
class CounterQualification:
    version: str
    server_identity: str
    native_profile_digest: str
    tokenizer_revision: str
    renderer_revision: str
    confidence: Confidence
    supported_features: frozenset[str]
    expires_ms: int
    reference: str
    capabilities: tuple[CapabilityEvidence, ...] = ()
    may_load: bool | None = None
    workspace_within_model_peak: bool = False
    preserves_request: bool = False
    output_accounting: str | None = None


@dataclass(frozen=True, slots=True)
class ResidentBinding:
    lease: RequestLease
    version: str
    server_identity: str
    native_profile_digest: str
    tokenizer_revision: str
    renderer_revision: str
    effective_context_tokens: int | None
    stamp: ObservationStamp
    reference: str
    exclusive_resident_verified: bool = False
    ownership_verified: bool = False
    native_controls_verified: bool = False
    active_requests: int | None = None
    late_effects_excluded: bool = False


@dataclass(frozen=True, slots=True)
class FitObservation:
    binding: ResidentBinding
    memory_sample: MemorySample


@dataclass(frozen=True, slots=True)
class CountReceipt:
    binding: ResidentBinding
    body_digest: str
    input_tokens: int
    confidence: Confidence
    covered_features: frozenset[str]
    stamp: ObservationStamp
    reference: str
    template_included: bool = False
    terminal: bool = False
    component_tokens: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class FitApproval:
    lease: RequestLease
    body_digest: str
    native_profile_digest: str
    expires_ms: int
    diagnostic: FitDiagnostic
    reference: str


def validate_counter_metadata(
    profile: FitProfile,
    qualification: CounterQualification | None,
    *,
    features: frozenset[str],
    now_ms: int,
) -> None:
    """Pure stage-one filter before source disruption; no lease, probe or I/O.

    Qualification is an installed producer assertion, not native proof. Passing
    this filter does not reserve memory, load the target, count or confer fit.
    Repeat it with the final feature set under warm residency before counting.
    """
    number(now_ms)
    if (
        not isinstance(profile, FitProfile)
        or not isinstance(features, frozenset)
        or not {"messages", "template", "request_settings"} <= features
    ):
        raise RequestFitDenied("counter_unqualified")
    q, now = qualification, now_ms
    if not isinstance(q, CounterQualification):
        raise RequestFitDenied("counter_unqualified")
    number(q.expires_ms, positive=True)
    if (
        q.expires_ms <= now
        or q.native_profile_digest != profile.native_profile_digest
        or profile.tokenizer_revision is None
        or q.tokenizer_revision != profile.tokenizer_revision
        or profile.renderer_revision is None
        or q.renderer_revision != profile.renderer_revision
        or q.confidence not in ("exact", "qualified_upper_bound")
        or not isinstance(q.supported_features, frozenset)
        or not features <= q.supported_features
        or q.may_load is not False
        or q.workspace_within_model_peak is not True
        or q.preserves_request is not True
        or q.output_accounting != profile.output_accounting
    ):
        raise RequestFitDenied("counter_unqualified")
    for value in (
        q.version,
        q.server_identity,
        q.tokenizer_revision,
        q.renderer_revision,
        q.reference,
    ):
        reference(value)
    number(q.expires_ms, positive=True)
    if not isinstance(q.capabilities, tuple):
        raise RequestFitDenied("counter_unqualified")
    caps = {c.name: c for c in q.capabilities if isinstance(c, CapabilityEvidence)}
    if len(caps) != len(q.capabilities) or any(
        name not in caps
        or caps[name].state_for_installed_scope(
            runtime_id=profile.memory.target.runtime_id,
            runtime_version=q.version,
            profile_fingerprint=profile.memory.target.profile_fingerprint,
        )
        != "supported"
        for name in CAPABILITIES
    ):
        raise RequestFitDenied("counter_unqualified")


class RequestFitAdmission:
    def __init__(
        self,
        coordinator: LifecycleCoordinator,
        memory: SharedMemoryAdmission,
        *,
        clock_ms: Callable[[], int],
        authorize: Callable[[FitPermit], bool] | None = None,
        timeout_s: float = 60.0,
        approval_ttl_ms: int = 5000,
    ) -> None:
        if memory._coordinator is not coordinator:
            raise RequestFitDenied("profile_mismatch")
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 3600
        ):
            raise RequestFitDenied("invalid_request")
        number(approval_ttl_ms, positive=True)
        if approval_ttl_ms > 5000:
            raise RequestFitDenied("invalid_request")
        self.coord, self.memory, self.clock = coordinator, memory, clock_ms
        self.authorize, self.timeout, self.ttl = (
            authorize,
            float(timeout_s),
            approval_ttl_ms,
        )
        self._issued: dict[str, FitApproval] = {}
        self._mutex = threading.RLock()

    def _now(self) -> int:
        return number(self.clock())

    def _fresh(self, stamp: ObservationStamp, now: int) -> None:
        if (
            not isinstance(stamp, ObservationStamp)
            or stamp.freshness(
                now=now / 1000,
                clock_id=self.coord.observer_clock_id,
                max_age=self.coord._max_observation_age,
            )
            != "fresh"
        ):
            raise RequestFitDenied("binding_unverified")

    def _active(
        self,
        conn: sqlite3.Connection,
        permit: FitPermit,
        prepared: PreparedRequest,
        now: int,
    ) -> None:
        lease = permit.lease
        if (
            permit.body_digest != prepared.body_digest
            or prepared.lease != lease
            or now >= permit.expires_ms
        ):
            raise RequestFitDenied("profile_mismatch")
        try:
            authorized = self.authorize(permit) if self.authorize is not None else False
        except Exception:
            raise RequestFitDenied("authority_denied") from None
        if authorized is not True:
            raise RequestFitDenied("authority_denied")
        domain = self.coord._domain(conn, lease.domain_id)
        row = conn.execute(
            "SELECT * FROM leases WHERE id=?", (lease.lease_id,)
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM requests WHERE id=?", (lease.request_id,)
        ).fetchone()
        if (
            row is None
            or request is None
            or row["status"] != "active"
            or request["status"] != "admitted"
            or request["purpose"] != "inference"
            or row["request_id"] != lease.request_id
            or row["domain"] != lease.domain_id
            or request["domain"] != lease.domain_id
            or row["owner_epoch"] != lease.owner_epoch
            or lease.owner_epoch != self.coord._epoch
            or row["generation"] != lease.generation
            or domain["generation"] != lease.generation
            or domain["resident"] != _encode(lease.target)
            or request["target"] != _encode(lease.target)
            or domain["resident_instance_id"] != lease.instance_id
            or domain["state"] != "idle"
            or domain["operation_id"] is not None
            or request["cancel_requested"]
            or request["deadline"] <= now
        ):
            raise RequestFitDenied("lease_stale")
        expected_backend = {
            "omlx_openai": "omlx",
            "ollama_native": "ollama",
            "lmstudio_openai": "lmstudio",
        }[prepared.profile.wire]

        if not any(
            r["runtime_id"] == lease.target.runtime_id
            and r["backend"] == expected_backend
            and r["location"] == "local"
            and r["egress"] == "local-only"
            and r["ownership_mode"] == "coordinated"
            for r in json.loads(domain["bindings"])
        ):
            raise RequestFitDenied("binding_unverified")

    def _qualified(
        self, prepared: PreparedRequest, q: CounterQualification | None, now: int
    ) -> None:
        validate_counter_metadata(
            prepared.profile, q, features=prepared.features, now_ms=now
        )

    def _binding(
        self,
        prepared: PreparedRequest,
        q: CounterQualification,
        observed: FitObservation,
        now: int,
    ) -> None:
        if not isinstance(observed, FitObservation) or not isinstance(
            observed.binding, ResidentBinding
        ):
            raise RequestFitDenied("binding_unverified")
        b = observed.binding
        self._fresh(b.stamp, now)
        reference(b.reference)
        if (
            b.lease != prepared.lease
            or b.version != q.version
            or b.server_identity != q.server_identity
            or b.native_profile_digest != q.native_profile_digest
            or b.tokenizer_revision != q.tokenizer_revision
            or b.renderer_revision != q.renderer_revision
            or type(b.effective_context_tokens) is not int
            or b.effective_context_tokens != prepared.profile.memory.context_tokens
            or b.exclusive_resident_verified is not True
            or b.ownership_verified is not True
            or b.native_controls_verified is not True
            or type(b.active_requests) is not int
            or b.active_requests != 0
            or b.late_effects_excluded is not True
        ):
            raise RequestFitDenied("binding_unverified")

    def _memory(
        self,
        conn: sqlite3.Connection,
        prepared: PreparedRequest,
        lease: MemoryLease,
        observed: FitObservation,
        now: int,
    ) -> None:
        if (
            not isinstance(lease, MemoryLease)
            or lease.kind != "model"
            or lease.domain_id != prepared.lease.domain_id
        ):
            raise RequestFitDenied("binding_unverified")
        row = self.memory._active(conn, lease)

        if row["status"] != "dispatched" or json.loads(lease.binding)[
            "profile"
        ] != asdict(prepared.profile.memory):
            raise RequestFitDenied("binding_unverified")
        event = {
            "ollama_native": "ollama_profile_load",
            "lmstudio_openai": "lmstudio_profile_load",
        }.get(prepared.profile.wire)
        if event:
            profile = conn.execute(
                "SELECT reference FROM journal WHERE domain=? AND operation_id=? AND event=?",
                (lease.domain_id, lease.work_id, event),
            ).fetchone()
            if (
                profile is None
                or profile["reference"] != prepared.profile.native_profile_digest
            ):
                raise RequestFitDenied("profile_mismatch")
        if self.memory._dispatch(conn, lease, observed.memory_sample, now):
            raise RequestFitDenied("binding_unverified")

    def _hold(self, permit: FitPermit, lease: MemoryLease) -> None:
        try:
            self.coord.mark_request_uncertain(
                permit.lease,
                reference="request_fit_count_uncertain",
                now_ms=self._now(),
            )
        finally:
            self.memory.mark_uncertain(
                lease, reference="request_fit_count_uncertain", now_ms=self._now()
            )

    def revoke(self, lease: RequestLease) -> None:
        with self._mutex:
            self._issued.pop(lease.lease_id, None)

    def authorizes(
        self, lease: RequestLease, body_digest: str, native_profile_digest: str
    ) -> bool:
        """Memory-only start check, safe inside an adapter's fenced transaction.

        NOT a substitute for the adapter's current native/profile/memory checks
        or its live policy/grant authorizer. Not used as a stream-end TTL gate.
        """
        with self._mutex:
            approval = self._issued.get(lease.lease_id)
            return bool(
                approval is not None
                and approval.lease == lease
                and approval.body_digest == body_digest
                and approval.native_profile_digest == native_profile_digest
                and self._now() < approval.expires_ms
                and not self.coord._closed
                and os.getpid() == self.coord._pid
                and lease.owner_epoch == self.coord._epoch
            )

    async def _evaluate(
        self,
        permit: FitPermit,
        prepared: PreparedRequest,
        *,
        lease: MemoryLease,
        qualification: CounterQualification | None = None,
        counter: Callable[
            [PreparedRequest, CounterQualification], Awaitable[CountReceipt]
        ]
        | None = None,
        observe: Callable[[], Awaitable[FitObservation]],
    ) -> FitApproval:
        self.revoke(permit.lease)
        now = self._now()
        with self._mutex:
            self._issued = {k: v for k, v in self._issued.items() if v.expires_ms > now}
            if len(self._issued) >= 256:
                raise RequestFitDenied("approval_stale")
        self.coord._call(
            lambda conn: self._active(conn, permit, prepared, now), now_ms=now
        )
        self._qualified(prepared, qualification, now)
        if counter is None:
            raise RequestFitDenied("counter_unqualified")
        assert qualification is not None
        q = qualification
        assert_no_active_write_transaction("request fit observation")
        try:
            async with asyncio.timeout(self.timeout):
                before = await observe()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RequestFitDenied("binding_unverified") from None
        now = self._now()

        def start(conn: sqlite3.Connection) -> None:
            self._active(conn, permit, prepared, now)
            self._qualified(prepared, q, now)
            self._binding(prepared, q, before, now)
            if conn.execute(
                "SELECT 1 FROM journal WHERE operation_id=? AND event='request_fit_count_sent'",
                (permit.lease.lease_id,),
            ).fetchone():
                raise RequestFitDenied("counter_already_dispatched")
            self._memory(conn, prepared, lease, before, now)
            self.coord._event(
                conn,
                permit.lease.domain_id,
                "request_fit_count_sent",
                permit.lease.lease_id,
                prepared.body_digest,
            )

        if not isinstance(before, FitObservation):
            raise RequestFitDenied("binding_unverified")
        self.memory._admission_call(start, before.memory_sample, now)
        dispatched = now
        try:
            assert_no_active_write_transaction("full request counter")
            async with asyncio.timeout(self.timeout):
                receipt = await counter(prepared, q)
                after = await observe()
            now = self._now()
            if (
                not isinstance(receipt, CountReceipt)
                or receipt.binding != before.binding
                or receipt.body_digest != prepared.body_digest
                or receipt.confidence != q.confidence
                or not isinstance(receipt.covered_features, frozenset)
                or receipt.covered_features != prepared.features
                or receipt.template_included is not True
                or receipt.terminal is not True
            ):
                raise RequestFitDenied("count_unverified")
            self._fresh(receipt.stamp, now)
            reference(receipt.reference)
            if (
                receipt.stamp.observed_at is None
                or receipt.stamp.observed_at * 1000 < dispatched
            ):
                raise RequestFitDenied("count_unverified")

            def finish(conn: sqlite3.Connection) -> FitApproval | RequestFitDenied:
                self._active(conn, permit, prepared, now)
                self._qualified(prepared, q, now)
                self._binding(prepared, q, after, now)
                if (
                    after.binding.stamp.observed_at is None
                    or receipt.stamp.observed_at is None
                    or after.binding.stamp.observed_at < receipt.stamp.observed_at
                ):
                    raise RequestFitDenied("binding_unverified")
                self._memory(conn, prepared, lease, after, now)
                context = after.binding.effective_context_tokens
                assert context is not None
                try:
                    result = assess_fit(
                        prepared,
                        effective_context=context,
                        input_tokens=receipt.input_tokens,
                        confidence=receipt.confidence,
                        component_tokens=receipt.component_tokens,
                    )
                except RequestFitDenied as exc:
                    if exc.code != "context_exceeded":
                        raise
                    self.coord._event(
                        conn,
                        permit.lease.domain_id,
                        "request_fit_refused",
                        permit.lease.lease_id,
                        "context_exceeded",
                    )
                    return exc
                stamp = after.binding.stamp.observed_at
                assert stamp is not None
                expires = min(
                    permit.expires_ms,
                    q.expires_ms,
                    now + self.ttl,
                    int(stamp * 1000 + self.coord._max_observation_age * 1000),
                )
                if expires <= now:
                    raise RequestFitDenied("approval_stale")
                self.coord._event(
                    conn,
                    permit.lease.domain_id,
                    "request_fit_verified",
                    permit.lease.lease_id,
                    prepared.body_digest,
                )
                return FitApproval(
                    permit.lease,
                    prepared.body_digest,
                    prepared.profile.native_profile_digest,
                    expires,
                    result,
                    uuid.uuid4().hex,
                )

            result = self.memory._admission_call(finish, after.memory_sample, now)
        except (asyncio.CancelledError, GeneratorExit):
            self._hold(permit, lease)
            raise
        except Exception:
            self._hold(permit, lease)
            raise RequestFitDenied("count_uncertain") from None
        if isinstance(result, RequestFitDenied):
            # Fully counted, terminal refusal: no inference/automatic retry,
            # lease release, truncation, resize, fallback or memory settlement.
            raise result
        with self._mutex:
            now = self._now()
            self._issued = {k: v for k, v in self._issued.items() if v.expires_ms > now}
            if len(self._issued) >= 256 or result.expires_ms <= now:
                raise RequestFitDenied("approval_stale")
            self._issued[permit.lease.lease_id] = result
        return result

    async def evaluate(
        self,
        permit: FitPermit,
        prepared: PreparedRequest,
        *,
        lease: MemoryLease,
        qualification: CounterQualification | None = None,
        counter: Callable[
            [PreparedRequest, CounterQualification], Awaitable[CountReceipt]
        ]
        | None = None,
        observe: Callable[[], Awaitable[FitObservation]],
    ) -> FitApproval:
        verify_prepared(prepared)
        try:
            return await self._evaluate(
                permit,
                prepared,
                lease=lease,
                qualification=qualification,
                counter=counter,
                observe=observe,
            )
        except Exception as exc:
            if isinstance(exc, RequestFitDenied) and exc.diagnostic is not None:
                raise
            code = (
                exc.code
                if isinstance(exc, RequestFitDenied)
                else "memory_unavailable"
                if isinstance(exc, MemoryDenied)
                else "admission_unavailable"
            )
            diagnostic = FitDiagnostic(
                prepared.profile.memory.context_tokens,
                None,
                None,
                prepared.budget.output_tokens,
                prepared.budget.reasoning_tokens,
                prepared.budget.reserve,
                None,
                tuple((k, None) for k in sorted(prepared.components)),
            )
            raise RequestFitDenied(code, diagnostic) from None
