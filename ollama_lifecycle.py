"""Dormant LC-05 native Ollama bridge. No registration, client or proof producer.

Injected transports MUST enforce authentication, approved local URL/IP pinning,
no redirects/retries/remote fallback, bounded bodies and cancellation-safe close.
Source review/version strings cannot supply installed qualification or authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Protocol,
)

from local_runtime_lifecycle import CapabilityEvidence, ObservationStamp
from model_lifecycle_coordinator import (
    LifecycleCoordinator,
    LoadLease,
    ModelTarget,
    ReconciliationEvidence,
    RequestLease,
    TransitionStep,
    ReleaseStep,
    _encode,
)
from ollama_protocol import (
    SOURCE_PIN,
    NativeChatDecoder,
    OllamaDenied,
    OllamaProfile,
    PreparedOllamaChat,
    encode,
    integer,
    local_reference,
    obj,
    parse_json,
    text,
    validate_prepared,
)
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import MemoryLease, MemorySample, SharedMemoryAdmission

Intent = TransitionStep | RequestLease | LoadLease | ReleaseStep
REQUIRED_CAPABILITIES = (
    "native_chat",
    "explicit_local_source",
    "profile_identity",
    "strict_input",
    "reasoning_identity",
    "tool_identity",
    "complete_visibility",
    "exclusive_control",
    "terminality",
)


class OllamaTransport(Protocol):
    async def request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int, Mapping[str, Any]]: ...

    def stream(
        self, path: str, body: bytes, timeout_s: float
    ) -> AsyncContextManager[tuple[int, AsyncIterator[bytes]]]: ...


@dataclass(frozen=True, slots=True)
class OllamaQualification:
    runtime_id: str
    server_version: str
    source_pin: str
    profile: OllamaProfile
    expires_ms: int
    reference: str
    capabilities: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.runtime_id,
            self.server_version,
            self.source_pin,
            self.reference,
        ):
            text(value, label=True)
        integer(self.expires_ms, positive=True)
        if not isinstance(self.profile, OllamaProfile) or not isinstance(
            self.capabilities, tuple
        ):
            raise OllamaDenied("invalid_qualification")


@dataclass(frozen=True, slots=True)
class OllamaPermit:
    intent: Intent
    qualification: OllamaQualification
    expires_ms: int
    reference: str
    request_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.intent, (TransitionStep, RequestLease, LoadLease, ReleaseStep)
        ) or not isinstance(self.qualification, OllamaQualification):
            raise OllamaDenied("invalid_permit")
        integer(self.expires_ms, positive=True)
        text(self.reference, label=True)
        if isinstance(self.intent, RequestLease):
            if (
                not isinstance(self.request_digest, str)
                or len(self.request_digest) != 64
                or any(c not in "0123456789abcdef" for c in self.request_digest)
            ):
                raise OllamaDenied("request_digest_required")
        elif self.request_digest is not None:
            raise OllamaDenied("unexpected_request_digest")


@dataclass(frozen=True, slots=True)
class OllamaActivity:
    """Separate trusted current stream/late-effect/instance authority, NOT /ps.

    Conservative defaults deny. No production producer exists in this story.
    """

    intent: Intent
    target: ModelTarget
    stamp: ObservationStamp
    reference: str
    instance_id: str | None = None
    active_requests: int | None = None
    late_effects_excluded: bool = False
    ownership_verified: bool = False


@dataclass(frozen=True, slots=True)
class OllamaObservation:
    runtime_id: str
    version: str
    stamp: ObservationStamp
    reference: str
    # Raw /ps facts only; never synthesize an instance ID or active stream count.
    rows: tuple[Mapping[str, Any], ...]


def _target(intent: Intent) -> ModelTarget:
    if isinstance(intent, ReleaseStep):
        return intent.source
    if isinstance(intent, TransitionStep) and intent.kind in ("drain", "unload"):
        if intent.source is None:
            raise OllamaDenied("source_missing")
        return intent.source
    return intent.target


def _key(intent: Intent) -> str:
    return (
        intent.lease_id
        if isinstance(intent, (RequestLease, LoadLease))
        else intent.operation_id
    )


def _profile_digest(profile: OllamaProfile) -> str:
    return hashlib.sha256(encode(asdict(profile))).hexdigest()


def _stage(intent: Intent) -> str:
    return (
        "infer"
        if isinstance(intent, RequestLease)
        else "load_control"
        if isinstance(intent, LoadLease)
        else "release_" + intent.kind
        if isinstance(intent, ReleaseStep)
        else intent.kind
    )


def validate_qualification(q: OllamaQualification, now: int) -> None:
    """Pure installed metadata check, shared by pre-disruption and send gates."""
    integer(now)
    if (
        not isinstance(q, OllamaQualification)
        or q.source_pin != SOURCE_PIN
        or now >= q.expires_ms
    ):
        raise OllamaDenied("native_qualification_mismatch")
    caps = {c.name: c for c in q.capabilities if isinstance(c, CapabilityEvidence)}
    if len(caps) != len(q.capabilities) or any(
        name not in caps
        or caps[name].state_for_installed_scope(
            runtime_id=q.runtime_id,
            runtime_version=q.server_version,
            profile_fingerprint=q.profile.memory.target.profile_fingerprint,
        )
        != "supported"
        for name in REQUIRED_CAPABILITIES
    ):
        raise OllamaDenied("native_capability_unqualified")


class OllamaLifecycleBridge:
    def __init__(
        self,
        coordinator: LifecycleCoordinator,
        memory: SharedMemoryAdmission,
        transport: OllamaTransport,
        *,
        clock_ms: Callable[[], int],
        authorize: Callable[[OllamaPermit], bool] | None = None,
        timeout_s: float = 60.0,
        admit_request: Callable[[RequestLease, str, str], bool] | None = None,
    ) -> None:
        if memory._coordinator is not coordinator:
            raise OllamaDenied("coordinator_mismatch")
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 3600
        ):
            raise OllamaDenied("invalid_timeout")
        self.coordinator, self.memory, self.transport = coordinator, memory, transport
        self.admit_request = admit_request
        self.clock, self.authorize, self.timeout_s = (
            clock_ms,
            authorize,
            float(timeout_s),
        )

    def _now(self) -> int:
        return integer(self.clock())

    def _fresh(self, stamp: ObservationStamp, now: int) -> None:
        if (
            not isinstance(stamp, ObservationStamp)
            or stamp.freshness(
                now=now / 1000,
                clock_id=self.coordinator.observer_clock_id,
                max_age=self.coordinator._max_observation_age,
            )
            != "fresh"
        ):
            raise OllamaDenied("native_observation_stale")

    def _gate(self, permit: OllamaPermit, now: int) -> None:
        if not isinstance(permit, OllamaPermit):
            raise OllamaDenied("invalid_permit")
        q = permit.qualification
        if (
            q.source_pin != SOURCE_PIN
            or now >= min(q.expires_ms, permit.expires_ms)
            or _target(permit.intent) != q.profile.memory.target
            or q.runtime_id != _target(permit.intent).runtime_id
        ):
            raise OllamaDenied("native_qualification_mismatch")
        validate_qualification(q, now)
        try:
            approved = self.authorize(permit) if self.authorize is not None else False
        except Exception:
            raise OllamaDenied("native_authority_unavailable") from None
        if approved is not True:
            raise OllamaDenied("native_authority_denied")

    def _active(
        self,
        conn: sqlite3.Connection,
        permit: OllamaPermit,
        now: int,
        *,
        finishing: bool = False,
    ) -> sqlite3.Row:
        self._gate(permit, now)
        intent = permit.intent
        domain = self.coordinator._domain(conn, intent.domain_id)
        runtimes = parse_json(domain["bindings"])
        if not any(
            r["runtime_id"] == _target(intent).runtime_id
            and r["backend"] == "ollama"
            and r["location"] == "local"
            and r["egress"] == "local-only"
            and r["ownership_mode"] == "coordinated"
            for r in runtimes
        ):
            raise OllamaDenied("runtime_not_coordinated_local_ollama")
        if isinstance(intent, ReleaseStep):
            release = self.coordinator._active_release(
                conn, intent, now, finishing=finishing
            )
            if release is None:
                raise OllamaDenied("release_intent_stale_or_busy")
            return release
        if isinstance(intent, TransitionStep):
            if conn.execute(
                "SELECT 1 FROM leases WHERE domain=? AND status='active'",
                (intent.domain_id,),
            ).fetchone():
                raise OllamaDenied("streams_not_drained")
            row = self.coordinator._active_step(conn, intent, now)
            if row is None:
                raise OllamaDenied("lifecycle_intent_stale")
            return row
        lease = conn.execute(
            "SELECT * FROM leases WHERE id=?", (intent.lease_id,)
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM requests WHERE id=?", (intent.request_id,)
        ).fetchone()
        if (
            lease is None
            or request is None
            or lease["status"] != "active"
            or request["status"] != "admitted"
            or request["purpose"]
            != ("load" if type(intent) is LoadLease else "inference")
            or lease["request_id"] != intent.request_id
            or lease["domain"] != intent.domain_id
            or request["domain"] != intent.domain_id
            or request["target"] != _encode(intent.target)
            or lease["owner_epoch"] != intent.owner_epoch
            or intent.owner_epoch != self.coordinator._epoch
            or lease["generation"] != intent.generation
            or domain["generation"] != intent.generation
            or domain["resident"] != _encode(intent.target)
            or domain["resident_instance_id"] != intent.instance_id
            or domain["state"] != "idle"
            or domain["operation_id"] is not None
            or (
                not finishing
                and (request["cancel_requested"] or request["deadline"] <= now)
            )
        ):
            raise OllamaDenied("inference_intent_stale")
        return lease

    def _activity(
        self,
        permit: OllamaPermit,
        activity: OllamaActivity,
        now: int,
        *,
        after_ms: int = 0,
    ) -> None:
        intent = permit.intent
        instance = (
            intent.instance_id
            if isinstance(intent, (RequestLease, LoadLease))
            else intent.source_instance_id
            if intent.kind in ("drain", "unload")
            else None
        )
        if (
            not isinstance(activity, OllamaActivity)
            or activity.intent != intent
            or activity.target != _target(intent)
            or activity.instance_id != instance
            or type(activity.active_requests) is not int
            or activity.active_requests != 0
            or activity.late_effects_excluded is not True
            or activity.ownership_verified is not True
        ):
            raise OllamaDenied("native_activity_unproven")
        text(activity.reference, label=True)
        self._fresh(activity.stamp, now)
        if (
            activity.stamp.observed_at is None
            or activity.stamp.observed_at * 1000 < after_ms
        ):
            raise OllamaDenied("native_activity_predates_terminal")

    async def _request(
        self, method: Literal["GET", "POST"], path: str, body: bytes | None = None
    ) -> Mapping[str, Any]:
        assert_no_active_write_transaction("Ollama native transport")
        try:
            async with asyncio.timeout(self.timeout_s):
                status, payload = await self.transport.request(
                    method, path, body, self.timeout_s
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OllamaDenied("native_transport_failure") from None
        self._status(status)
        if not isinstance(payload, Mapping):
            raise OllamaDenied("invalid_native_response")
        # Own a bounded JSON snapshot, not a mutable transport-owned mapping.
        result = obj(parse_json(encode(dict(payload))))
        if "error" in result:
            raise OllamaDenied("native_response_error")
        return result

    @staticmethod
    def _status(status: int) -> None:
        if type(status) is not int or status != 200:
            raise OllamaDenied("native_http_rejected")

    async def catalog(self) -> tuple[Mapping[str, Any], ...]:
        """/tags = installed catalog, including remote stubs; NOT warm residency."""
        payload = await self._request("GET", "/api/tags")
        rows = payload.get("models")
        if not isinstance(rows, list):
            raise OllamaDenied("invalid_native_catalog")
        seen = set()
        for row in rows:
            row = obj(row)
            name = text(row.get("name"), label=True)
            text(row.get("digest"), label=True)
            if name in seen:
                raise OllamaDenied("duplicate_native_artifact")
            seen.add(name)
        return tuple(rows)

    async def describe(self, artifact_name: str) -> Mapping[str, Any]:
        """Read-only /show with explicit local source; NOT load/context proof."""
        return await self._request(
            "POST", "/api/show", encode({"model": local_reference(artifact_name)})
        )

    async def observe(self, runtime_id: str) -> OllamaObservation:
        text(runtime_id, label=True)
        version = text(
            (await self._request("GET", "/api/version")).get("version"), label=True
        )
        rows = (await self._request("GET", "/api/ps")).get("models")
        if not isinstance(rows, list):
            raise OllamaDenied("invalid_native_residency")
        seen = set()
        for row in rows:
            row = obj(row)
            name = text(row.get("name"), label=True)
            if (
                name in seen
                or row.get("model") != name
                or row.get("remote_host")
                or row.get("remote_model")
            ):
                raise OllamaDenied("native_residency_identity_conflict")
            seen.add(name)
            text(row.get("digest"), label=True)
            text(row.get("runner"), label=True)
            integer(row.get("context_length"), positive=True)
            integer(row.get("size"))
            integer(row.get("size_vram"))
            text(row.get("expires_at"), label=True)
        return OllamaObservation(
            runtime_id,
            version,
            ObservationStamp(self._now() / 1000, self.coordinator.observer_clock_id),
            "ollama-observation-" + uuid.uuid4().hex,
            tuple(rows),
        )

    def _observation(
        self,
        permit: OllamaPermit,
        observation: OllamaObservation,
        now: int,
        *,
        absent: bool = False,
    ) -> None:
        q = permit.qualification
        if (
            observation.runtime_id != q.runtime_id
            or observation.version != q.server_version
        ):
            raise OllamaDenied("native_version_mismatch")
        self._fresh(observation.stamp, now)
        rows = observation.rows
        if absent:
            if rows:
                raise OllamaDenied("native_resident_conflict")
        elif (
            len(rows) != 1
            or rows[0]["name"] != q.profile.memory.target.artifact_id
            or rows[0]["digest"] != q.profile.memory.target.artifact_digest
            or rows[0]["runner"] != q.profile.runner
            or rows[0]["context_length"] != q.profile.memory.context_tokens
        ):
            raise OllamaDenied("native_resident_profile_mismatch")

    def _recorded(
        self, conn: sqlite3.Connection, intent: Intent, event: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM journal WHERE domain=? AND operation_id=? AND generation=? AND owner_epoch=? AND event=?",
            (
                intent.domain_id,
                _key(intent),
                intent.generation,
                intent.owner_epoch,
                "ollama_" + event + "_" + _stage(intent),
            ),
        ).fetchone()

    def _event(
        self, conn: sqlite3.Connection, intent: Intent, event: str, reference: str
    ) -> None:
        self.coordinator._event(
            conn,
            intent.domain_id,
            "ollama_" + event + "_" + _stage(intent),
            _key(intent),
            reference,
        )

    def _memory_send(
        self,
        conn: sqlite3.Connection,
        permit: OllamaPermit,
        lease: MemoryLease | None,
        sample: MemorySample | None,
        now: int,
    ) -> None:
        intent = permit.intent
        if (
            isinstance(intent, ReleaseStep)
            or isinstance(intent, TransitionStep)
            and intent.kind == "unload"
        ):
            row = conn.execute(
                "SELECT * FROM memory_reservations WHERE domain=? AND kind='model' AND status!='released'",
                (intent.domain_id,),
            ).fetchone()
            if (
                row is None
                or (
                    isinstance(intent, ReleaseStep)
                    and (row["id"] != intent.memory_id or row["status"] != "dispatched")
                )
                or parse_json(row["binding"])["profile"]
                != asdict(permit.qualification.profile.memory)
            ):
                raise OllamaDenied("native_memory_profile_mismatch")
            self._loaded_profile(conn, permit, row["work_id"])
            return
        if (
            lease is None
            or sample is None
            or lease.kind != "model"
            or lease.domain_id != intent.domain_id
        ):
            raise OllamaDenied("native_memory_lease_missing")
        row = self.memory._active(conn, lease)
        if parse_json(lease.binding)["profile"] != asdict(
            permit.qualification.profile.memory
        ):
            raise OllamaDenied("native_memory_profile_mismatch")
        if isinstance(intent, TransitionStep):
            if (
                parse_json(lease.binding)["step"] != asdict(intent)
                or row["status"] != "reserved"
            ):
                raise OllamaDenied("native_memory_intent_mismatch")
        elif row["status"] != "dispatched":
            raise OllamaDenied("native_memory_not_warm")
        else:
            self._loaded_profile(conn, permit, lease.work_id)
        sent = self.memory._dispatch(conn, lease, sample, now)
        if sent != isinstance(intent, TransitionStep):
            raise OllamaDenied("native_memory_dispatch_conflict")

    def _loaded_profile(
        self, conn: sqlite3.Connection, permit: OllamaPermit, operation_id: str
    ) -> None:
        row = conn.execute(
            "SELECT reference FROM journal WHERE domain=? AND operation_id=? AND event='ollama_profile_load'",
            (permit.intent.domain_id, operation_id),
        ).fetchone()
        if row is None or row["reference"] != _profile_digest(
            permit.qualification.profile
        ):
            raise OllamaDenied("native_load_inference_profile_mismatch")

    def _commit_send(
        self,
        permit: OllamaPermit,
        activity: OllamaActivity,
        observed: OllamaObservation,
        digest: str,
        lease: MemoryLease | None,
        sample: MemorySample | None,
    ) -> None:
        now = self._now()
        intent = permit.intent

        def commit(conn: sqlite3.Connection) -> None:
            self._active(conn, permit, now)
            self._activity(permit, activity, now)
            self._observation(
                permit,
                observed,
                now,
                absent=isinstance(intent, TransitionStep) and intent.kind == "load",
            )
            if self._recorded(conn, intent, "sent") is not None:
                raise OllamaDenied("native_send_already_recorded")
            if isinstance(intent, RequestLease):
                try:
                    fit = (
                        self.admit_request(
                            intent,
                            digest,
                            _profile_digest(permit.qualification.profile),
                        )
                        if self.admit_request is not None
                        else False
                    )
                except Exception:
                    raise OllamaDenied("request_fit_unavailable") from None
                if fit is not True:
                    raise OllamaDenied("request_fit_required")
            self._memory_send(conn, permit, lease, sample, now)
            self._event(conn, intent, "sent", digest)
            if isinstance(intent, TransitionStep) and intent.kind == "load":
                self._event(
                    conn,
                    intent,
                    "profile",
                    _profile_digest(permit.qualification.profile),
                )

        if sample is None:
            self.coordinator._call(commit, now_ms=now)
        else:
            self.memory._admission_call(commit, sample, now)

    def _failure(self, permit: OllamaPermit, lease: MemoryLease | None) -> None:
        now = self._now()
        intent = permit.intent
        try:
            if isinstance(intent, ReleaseStep):
                self.coordinator.mark_release_uncertain(
                    intent, reference="native_release_uncertain", now_ms=now
                )
            elif isinstance(intent, TransitionStep):
                self.coordinator.mark_uncertain(
                    intent, reference="ollama_native_uncertain", now_ms=now
                )
            elif isinstance(intent, LoadLease):
                self.coordinator.mark_load_uncertain(
                    intent, reference="native_load_uncertain", now_ms=now
                )
            else:
                self.coordinator.mark_request_uncertain(
                    intent, reference="ollama_native_uncertain", now_ms=now
                )
        finally:
            if lease is not None:
                self.memory.mark_uncertain(
                    lease, reference="ollama_native_uncertain", now_ms=now
                )

    def drain(self, permit: OllamaPermit, activity: OllamaActivity) -> bool:
        if (
            not isinstance(permit.intent, (TransitionStep, ReleaseStep))
            or permit.intent.kind != "drain"
        ):
            raise OllamaDenied("drain_intent_required")
        now = self._now()
        self.coordinator._call(lambda conn: self._active(conn, permit, now), now_ms=now)
        self._activity(permit, activity, now)
        if isinstance(permit.intent, ReleaseStep):
            self.coordinator._call(
                lambda conn: self._memory_send(conn, permit, None, None, now),
                now_ms=now,
            )
            return self.coordinator.complete_release(
                permit.intent, reference=activity.reference, now_ms=now
            )
        return self.coordinator.complete_step(
            permit.intent, reference=activity.reference, now_ms=now
        )

    async def send(
        self,
        permit: OllamaPermit,
        activity: OllamaActivity,
        *,
        lease: MemoryLease | None = None,
        sample: MemorySample | None = None,
    ) -> str:
        """One native load/unload send. ACK is never physical release authority."""
        intent = permit.intent
        if not isinstance(intent, (TransitionStep, ReleaseStep)) or intent.kind not in (
            "load",
            "unload",
        ):
            raise OllamaDenied("native_mutation_intent_required")
        self.coordinator._call(
            lambda conn: self._active(conn, permit, self._now()), now_ms=self._now()
        )
        observed = await self.observe(permit.qualification.runtime_id)
        body = encode(permit.qualification.profile.body(unload=intent.kind == "unload"))
        digest = hashlib.sha256(body).hexdigest()
        self._commit_send(permit, activity, observed, digest, lease, sample)
        try:
            payload = await self._request("POST", "/api/chat", body)
            message = obj(payload.get("message"))
            if (
                payload.get("done") is not True
                or payload.get("done_reason") != intent.kind
                or payload.get("model") != local_reference(_target(intent).artifact_id)
                or payload.get("remote_host")
                or payload.get("remote_model")
                or message.get("role") != "assistant"
                or message.get("content")
                or message.get("thinking")
                or message.get("tool_calls")
            ):
                raise OllamaDenied("invalid_native_lifecycle_ack")
            now = self._now()

            def accepted(conn: sqlite3.Connection) -> None:
                self._active(
                    conn, permit, now, finishing=isinstance(intent, ReleaseStep)
                )
                self._event(conn, intent, "ack", str(now))

            self.coordinator._call(accepted, now_ms=now)
            if isinstance(intent, TransitionStep) and intent.kind == "load":
                if not self.coordinator.complete_step(
                    intent, reference="ollama_native_load_ack", now_ms=now
                ):
                    raise OllamaDenied("lifecycle_intent_stale")
            return (
                "verify_required" if intent.kind == "load" else "pending_release_proof"
            )
        except BaseException:
            self._failure(permit, lease)
            raise

    def _proof(
        self,
        permit: OllamaPermit,
        observed: OllamaObservation,
        evidence: ReconciliationEvidence,
        *,
        verify: bool,
    ) -> None:
        if (
            not isinstance(evidence, ReconciliationEvidence)
            or evidence.reference != observed.reference
            or evidence.clock_id != observed.stamp.clock_id
        ):
            raise OllamaDenied("native_proof_scope_mismatch")
        inventory = next(
            (
                x
                for x in evidence.inventories
                if x.runtime.runtime_id == permit.qualification.runtime_id
            ),
            None,
        )
        if (
            inventory is None
            or inventory.runtime_version != observed.version
            or inventory.stamp.clock_id != observed.stamp.clock_id
            or inventory.stamp.observed_at is None
            or observed.stamp.observed_at is None
            or inventory.stamp.observed_at < observed.stamp.observed_at
        ):
            raise OllamaDenied("native_proof_scope_mismatch")
        if verify and not any(
            x.artifact_id == _target(permit.intent).artifact_id
            and x.effective_context_tokens
            == permit.qualification.profile.memory.context_tokens
            for x in inventory.instances
        ):
            raise OllamaDenied("native_context_unverified")

    async def confirm(
        self,
        permit: OllamaPermit,
        produce: Callable[[OllamaObservation], Awaitable[ReconciliationEvidence]],
    ) -> bool:
        """Current post-send all-runtime/instance/terminal/physical evidence only."""
        intent = permit.intent
        if not isinstance(intent, (TransitionStep, ReleaseStep)) or intent.kind not in (
            "verify",
            "unload",
        ):
            raise OllamaDenied("native_confirmation_intent_required")
        now = self._now()

        def check(conn: sqlite3.Connection) -> None:
            self._active(conn, permit, now, finishing=isinstance(intent, ReleaseStep))
            if intent.kind == "unload" and self._recorded(conn, intent, "ack") is None:
                raise OllamaDenied("native_ack_missing")
            if intent.kind == "verify":
                if (
                    conn.execute(
                        "SELECT 1 FROM journal WHERE domain=? AND operation_id=? AND owner_epoch=? AND generation=? AND event='ollama_ack_load'",
                        (
                            intent.domain_id,
                            intent.operation_id,
                            intent.owner_epoch,
                            intent.generation,
                        ),
                    ).fetchone()
                    is None
                ):
                    raise OllamaDenied("native_ack_missing")
                self._loaded_profile(conn, permit, intent.operation_id)

        self.coordinator._call(check, now_ms=now)
        observed = await self.observe(permit.qualification.runtime_id)
        self._observation(permit, observed, self._now(), absent=intent.kind == "unload")
        assert_no_active_write_transaction("Ollama native proof producer")
        try:
            async with asyncio.timeout(self.timeout_s):
                evidence = await produce(observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OllamaDenied("native_proof_unavailable") from None
        self._proof(permit, observed, evidence, verify=intent.kind == "verify")
        now = self._now()
        self.coordinator._call(
            lambda conn: self._active(
                conn, permit, now, finishing=isinstance(intent, ReleaseStep)
            ),
            now_ms=now,
        )
        self._observation(permit, observed, now, absent=intent.kind == "unload")
        # Source target owns unload; target qualification owns verify. /ps alone
        # cannot produce the coordinator's exact receipt or physical proof.
        if isinstance(intent, ReleaseStep):
            return self.coordinator.complete_release(
                intent, reference="ollama_native_confirm", now_ms=now, evidence=evidence
            )
        return self.coordinator.complete_step(
            intent, reference="ollama_native_confirm", now_ms=now, evidence=evidence
        )

    async def confirm_load(
        self,
        permit: OllamaPermit,
        *,
        lease: MemoryLease,
        sample: Callable[[], Awaitable[MemorySample]],
        produce: Callable[[OllamaObservation], Awaitable[ReconciliationEvidence]],
    ) -> bool:
        """Fresh real control-slot/native/profile/memory proof, never inference.

        Final current authority, model budget and slot completion share one write.
        Native observations/proof/sample production remain outside transactions.
        """
        intent = permit.intent
        if type(intent) is not LoadLease or lease.domain_id != intent.domain_id:
            raise OllamaDenied("load_control_intent_required")

        def current(conn: sqlite3.Connection, measured: MemorySample) -> None:
            self._active(conn, permit, self._now())
            self.memory._admitted_model(
                conn, lease, permit.qualification.profile.memory, measured, self._now()
            )
            self._loaded_profile(conn, permit, lease.work_id)

        async def measured() -> MemorySample:
            assert_no_active_write_transaction("native load memory producer")
            async with asyncio.timeout(5):
                return await sample()

        before = await measured()
        self.coordinator._call(lambda conn: current(conn, before), now_ms=self._now())
        observed = await self.observe(permit.qualification.runtime_id)
        self._observation(permit, observed, self._now())
        after = await measured()
        assert_no_active_write_transaction("native load proof producer")
        try:
            async with asyncio.timeout(5):
                evidence = await produce(observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OllamaDenied("load_control_proof_unavailable") from None
        self._proof(permit, observed, evidence, verify=True)
        now = self._now()
        self._observation(permit, observed, self._now())

        def finish(conn: sqlite3.Connection) -> bool:
            current(conn, after)
            return self.coordinator._finish_load(
                conn, intent, evidence, reference=observed.reference, now_ms=now
            )

        return self.coordinator._call(finish, now_ms=now)

    async def infer(
        self,
        permit: OllamaPermit,
        prepared: PreparedOllamaChat,
        activity: OllamaActivity,
        *,
        lease: MemoryLease,
        sample: MemorySample,
        produce_terminal: Callable[[], Awaitable[OllamaActivity]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream content; publish structured calls/usage only after proven terminal.

        LC-07 must supply exact full-request fit and unchanged-payload authority
        through the live authorizer. This module supplies neither counter nor grant.
        """
        intent = permit.intent
        if not isinstance(intent, RequestLease) or not callable(produce_terminal):
            raise OllamaDenied("native_inference_intent_required")
        validate_prepared(prepared, permit.qualification.profile)
        if permit.request_digest != prepared.digest:
            raise OllamaDenied("native_request_fit_scope_mismatch")
        self.coordinator._call(
            lambda conn: self._active(conn, permit, self._now()), now_ms=self._now()
        )
        observed = await self.observe(permit.qualification.runtime_id)
        self._commit_send(permit, activity, observed, prepared.digest, lease, sample)
        decoder = NativeChatDecoder(prepared)
        try:
            assert_no_active_write_transaction("Ollama native inference")
            async with asyncio.timeout(self.timeout_s):
                async with self.transport.stream(
                    "/api/chat", prepared.body, self.timeout_s
                ) as (status, chunks):
                    self._status(status)
                    async for chunk in chunks:
                        for event in decoder.feed(chunk):
                            yield event
                terminal_events = decoder.finish()
                completed_ms = self._now()
                terminal = await produce_terminal()
                observed = await self.observe(permit.qualification.runtime_id)
                now = self._now()

                def finish(conn: sqlite3.Connection) -> None:
                    self._active(conn, permit, now, finishing=True)
                    self._activity(permit, terminal, now, after_ms=completed_ms)
                    self._observation(permit, observed, now)
                    # Same exact lease/instance scope as LC-02 finish_request;
                    # keep validation, terminal marker and slot release atomic.
                    conn.execute(
                        "UPDATE leases SET status='released' WHERE id=?",
                        (intent.lease_id,),
                    )
                    conn.execute(
                        "UPDATE requests SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END WHERE id=?",
                        (intent.request_id,),
                    )
                    self._event(conn, intent, "terminal", prepared.digest)
                    self.coordinator._event(
                        conn,
                        intent.domain_id,
                        "request_finished",
                        ref=terminal.reference,
                    )

                self.coordinator._call(finish, now_ms=now)
            # No effects remain. Consumer close during these final events must
            # not turn a confirmed terminal request into fictitious uncertainty.
        except (asyncio.CancelledError, GeneratorExit):
            self._failure(permit, lease)
            raise
        except Exception:
            self._failure(permit, lease)
            raise OllamaDenied("native_inference_uncertain") from None
        for event in terminal_events:
            yield event

    async def tokenize(self) -> None:
        raise OllamaDenied("exact_native_tokenizer_unqualified")
