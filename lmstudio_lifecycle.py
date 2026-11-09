"""LC-06 dormant LM Studio v1 boundary; no client, SDK or app registration.

Transport/authority/environment/activity/evidence producers are injected trusted
server boundaries. No source/version string or HTTP ACK establishes their facts.
"""

from __future__ import annotations

import asyncio
import math
import hashlib
import re
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
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import MemoryLease, MemorySample, SharedMemoryAdmission, _load_confirmation_reference
from lmstudio_protocol import (
    SOURCE_PIN,
    LmCatalog,
    LmPreparedInference,
    LmSseDecoder,
    LmStudioDenied,
    LmStudioProfile,
    config,
    decode,
    encode,
    integer,
    label,
    obj,
    parse_catalog,
    prepare_inference,
)

Intent = TransitionStep | RequestLease | LoadLease | ReleaseStep
CAPABILITIES = (
    "native_v1",
    "authenticated_control",
    "exact_instance_routing",
    "profile_binding",
    "strict_input",
    "request_semantics",
    "exclusive_control",
    "complete_visibility",
    "terminality",
)


class LmStudioTransport(Protocol):
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
class LmEnvironment:
    """Trusted installed facts NOT present in list. No producer supplied here.

    Version is normalized by the producer, not guessed from endpoint availability.
    Artifact digest attribution must be complete and current, not file-size based.
    Unknown/default automatic-load/eviction/expiry facts block native effects.
    """

    runtime_id: str
    version: str
    server_identity: str
    stamp: ObservationStamp
    reference: str
    artifact_digests: tuple[tuple[str, str], ...] = ()
    jit_enabled: bool | None = None
    auto_evict_enabled: bool | None = None
    expiry_excluded: bool = False
    exclusive_control: bool = False
    authentication_required: bool | None = None


@dataclass(frozen=True, slots=True)
class LmQualification:
    runtime_id: str
    version: str
    server_identity: str
    source_pin: str
    profile: LmStudioProfile
    expires_ms: int
    reference: str
    capabilities: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.runtime_id,
            self.version,
            self.server_identity,
            self.source_pin,
            self.reference,
        ):
            label(value)
        integer(self.expires_ms, positive=True)
        if not isinstance(self.profile, LmStudioProfile) or not isinstance(
            self.capabilities, tuple
        ):
            raise LmStudioDenied("invalid_qualification")


@dataclass(frozen=True, slots=True)
class LmPermit:
    intent: Intent
    qualification: LmQualification
    expires_ms: int
    reference: str
    request_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.intent, (TransitionStep, RequestLease, LoadLease, ReleaseStep)
        ) or not isinstance(self.qualification, LmQualification):
            raise LmStudioDenied("invalid_permit")
        integer(self.expires_ms, positive=True)
        label(self.reference)
        if isinstance(self.intent, RequestLease):
            if (
                not isinstance(self.request_digest, str)
                or re.fullmatch("[0-9a-f]{64}", self.request_digest) is None
            ):
                raise LmStudioDenied("request_digest_required")
        elif self.request_digest is not None:
            raise LmStudioDenied("unexpected_request_digest")


@dataclass(frozen=True, slots=True)
class LmActivity:
    intent: Intent
    target: ModelTarget
    stamp: ObservationStamp
    reference: str
    instance_id: str | None = None
    active_requests: int | None = None
    ownership_verified: bool = False
    late_effects_excluded: bool = False
    executed_request_digest: str | None = None


@dataclass(frozen=True, slots=True)
class LmObservation:
    environment: LmEnvironment
    catalog: LmCatalog
    stamp: ObservationStamp
    reference: str


def _target(intent: Intent) -> ModelTarget:
    if isinstance(intent, ReleaseStep):
        return intent.source
    if isinstance(intent, TransitionStep) and intent.kind in ("drain", "unload"):
        if intent.source is None:
            raise LmStudioDenied("source_missing")
        return intent.source
    return intent.target


def _key(intent: Intent) -> str:
    return (
        intent.lease_id
        if isinstance(intent, (RequestLease, LoadLease))
        else intent.operation_id
    )


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


def validate_qualification(q: LmQualification, now: int) -> None:
    """Pure installed metadata check, shared by pre-disruption and send gates."""
    integer(now)
    if (
        not isinstance(q, LmQualification)
        or q.source_pin != SOURCE_PIN
        or now >= q.expires_ms
    ):
        raise LmStudioDenied("native_qualification_mismatch")
    if re.fullmatch(r"\d+\.\d+\.\d+", q.version) is None or tuple(
        map(int, q.version.split("."))
    ) < (0, 4, 0):
        raise LmStudioDenied("native_version_unsupported")
    caps = {c.name: c for c in q.capabilities if isinstance(c, CapabilityEvidence)}
    if len(caps) != len(q.capabilities) or any(
        name not in caps
        or caps[name].state_for_installed_scope(
            runtime_id=q.runtime_id,
            runtime_version=q.version,
            profile_fingerprint=q.profile.memory.target.profile_fingerprint,
        )
        != "supported"
        for name in CAPABILITIES
    ):
        raise LmStudioDenied("capability_unqualified")


class LmStudioLifecycleBridge:
    def __init__(
        self,
        coordinator: LifecycleCoordinator,
        memory: SharedMemoryAdmission,
        transport: LmStudioTransport,
        *,
        clock_ms: Callable[[], int],
        environment: Callable[[], Awaitable[LmEnvironment]],
        authorize: Callable[[LmPermit], bool] | None = None,
        timeout_s: float = 60.0,
        admit_request: Callable[[RequestLease, str, str], bool] | None = None,
    ) -> None:
        if memory._coordinator is not coordinator:
            raise LmStudioDenied("coordinator_mismatch")
        if (
            type(timeout_s) not in (int, float)
            or not math.isfinite(timeout_s)
            or not 0 < timeout_s <= 3600
        ):
            raise LmStudioDenied("invalid_timeout")
        self.coord, self.memory, self.transport = coordinator, memory, transport
        self.admit_request = admit_request
        self.clock, self.environment, self.authorize, self.timeout = (
            clock_ms,
            environment,
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
                clock_id=self.coord.observer_clock_id,
                max_age=self.coord._max_observation_age,
            )
            != "fresh"
        ):
            raise LmStudioDenied("observation_stale")

    def _gate(self, permit: LmPermit, now: int) -> None:
        q = permit.qualification
        if re.fullmatch(r"\d+\.\d+\.\d+", q.version) is None or tuple(
            map(int, q.version.split("."))
        ) < (0, 4, 0):
            raise LmStudioDenied("native_version_unsupported")
        if (
            q.source_pin != SOURCE_PIN
            or now >= min(q.expires_ms, permit.expires_ms)
            or _target(permit.intent) != q.profile.memory.target
            or q.runtime_id != _target(permit.intent).runtime_id
        ):
            raise LmStudioDenied("qualification_mismatch")
        validate_qualification(q, now)
        try:
            approved = self.authorize(permit) if self.authorize is not None else False
        except Exception:
            raise LmStudioDenied("authority_unavailable") from None
        if approved is not True:
            raise LmStudioDenied("authority_denied")

    def _active(
        self,
        conn: sqlite3.Connection,
        permit: LmPermit,
        now: int,
        *,
        finishing: bool = False,
    ) -> None:
        self._gate(permit, now)
        intent = permit.intent
        domain = self.coord._domain(conn, intent.domain_id)
        if not any(
            r["runtime_id"] == _target(intent).runtime_id
            and r["backend"] == "lmstudio"
            and r["location"] == "local"
            and r["egress"] == "local-only"
            and r["ownership_mode"] == "coordinated"
            for r in decode(domain["bindings"])
        ):
            raise LmStudioDenied("runtime_not_coordinated_local_lmstudio")
        if isinstance(intent, ReleaseStep):
            if (
                self.coord._active_release(conn, intent, now, finishing=finishing)
                is None
            ):
                raise LmStudioDenied("release_intent_stale_or_busy")
            return
        if isinstance(intent, TransitionStep):
            if (
                self.coord._active_step(conn, intent, now) is None
                or conn.execute(
                    "SELECT 1 FROM leases WHERE domain=? AND status='active'",
                    (intent.domain_id,),
                ).fetchone()
            ):
                raise LmStudioDenied("lifecycle_intent_stale_or_busy")
            return
        row = conn.execute(
            "SELECT * FROM leases WHERE id=?", (intent.lease_id,)
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM requests WHERE id=?", (intent.request_id,)
        ).fetchone()
        if (
            row is None
            or request is None
            or row["status"] != "active"
            or request["status"] != "admitted"
            or request["purpose"]
            != ("load" if type(intent) is LoadLease else "inference")
            or row["request_id"] != intent.request_id
            or row["domain"] != intent.domain_id
            or request["domain"] != intent.domain_id
            or row["owner_epoch"] != intent.owner_epoch
            or intent.owner_epoch != self.coord._epoch
            or row["generation"] != intent.generation
            or domain["generation"] != intent.generation
            or request["target"] != _encode(intent.target)
            or domain["resident"] != _encode(intent.target)
            or domain["resident_instance_id"] != intent.instance_id
            or domain["state"] != "idle"
            or domain["operation_id"] is not None
            or (
                not finishing
                and (request["cancel_requested"] or request["deadline"] <= now)
            )
        ):
            raise LmStudioDenied("inference_intent_stale")

    def _activity(
        self,
        permit: LmPermit,
        activity: LmActivity,
        now: int,
        *,
        after_ms: int | None = None,
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
            not isinstance(activity, LmActivity)
            or activity.intent != intent
            or activity.target != _target(intent)
            or activity.instance_id != instance
            or type(activity.active_requests) is not int
            or activity.active_requests != 0
            or activity.ownership_verified is not True
            or activity.late_effects_excluded is not True
        ):
            raise LmStudioDenied("activity_unproven")
        label(activity.reference)
        self._fresh(activity.stamp, now)
        if after_ms is not None and (
            activity.executed_request_digest != permit.request_digest
            or activity.stamp.observed_at is None
            or activity.stamp.observed_at * 1000 < after_ms
        ):
            raise LmStudioDenied("execution_receipt_unproven")

    @staticmethod
    def _status(status: int) -> None:
        if type(status) is not int or status != 200:
            raise LmStudioDenied("native_http_rejected")

    async def _request(
        self, method: Literal["GET", "POST"], path: str, body: bytes | None = None
    ) -> dict[str, Any]:
        assert_no_active_write_transaction("LM Studio native request")
        try:
            async with asyncio.timeout(self.timeout):
                status, payload = await self.transport.request(
                    method, path, body, self.timeout
                )
            self._status(status)
            result = obj(decode(encode(dict(payload))))
            if "error" in result:
                raise LmStudioDenied("native_response_error")
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LmStudioDenied("native_request_failed") from None

    async def observe(self) -> LmObservation:
        assert_no_active_write_transaction("LM Studio environment producer")
        try:
            async with asyncio.timeout(self.timeout):
                env = await self.environment()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LmStudioDenied("environment_unavailable") from None
        if not isinstance(env, LmEnvironment):
            raise LmStudioDenied("environment_unavailable")
        catalog = parse_catalog(await self._request("GET", "/api/v1/models"))
        return LmObservation(
            env,
            catalog,
            ObservationStamp(self._now() / 1000, self.coord.observer_clock_id),
            "lmstudio-observation-" + uuid.uuid4().hex,
        )

    def _wire(
        self,
        permit: LmPermit,
        observed: LmObservation,
        now: int,
        instance_id: str | None,
    ) -> None:
        env, q = observed.environment, permit.qualification
        self._fresh(observed.stamp, now)
        self._fresh(env.stamp, now)
        label(env.reference)
        if (
            env.runtime_id != q.runtime_id
            or env.version != q.version
            or env.server_identity != q.server_identity
            or env.jit_enabled is not False
            or env.auto_evict_enabled is not False
            or env.expiry_excluded is not True
            or env.exclusive_control is not True
            or env.authentication_required is not True
        ):
            raise LmStudioDenied("environment_unqualified")
        if not isinstance(env.artifact_digests, tuple) or any(
            not isinstance(x, tuple) or len(x) != 2 for x in env.artifact_digests
        ):
            raise LmStudioDenied("artifact_binding_unproven")
        digests = {label(k): label(v) for k, v in env.artifact_digests}
        if (
            len(digests) != len(env.artifact_digests)
            or set(digests) != set(observed.catalog.keys)
            or digests.get(_target(permit.intent).artifact_id)
            != _target(permit.intent).artifact_digest
        ):
            raise LmStudioDenied("artifact_binding_unproven")
        artifact = next(
            (
                x
                for x in observed.catalog.artifacts
                if x.key == q.profile.memory.target.artifact_id
            ),
            None,
        )
        if (
            artifact is None
            or artifact.model_type != "llm"
            or artifact.format != q.profile.format
            or artifact.selected_variant != q.profile.selected_variant
        ):
            raise LmStudioDenied("artifact_type_or_variant_mismatch")
        instances = observed.catalog.instances
        if instance_id is None:
            if instances:
                raise LmStudioDenied("resident_conflict")
        elif (
            len(instances) != 1
            or instances[0].instance_id != instance_id
            or instances[0].artifact_key != _target(permit.intent).artifact_id
            or instances[0].model_type != "llm"
            or instances[0].format != q.profile.format
            or decode(instances[0].config_json) != decode(q.profile.config_json)
        ):
            raise LmStudioDenied("instance_or_config_mismatch")

    def _event(
        self, conn: sqlite3.Connection, permit: LmPermit, kind: str, reference: str
    ) -> None:
        intent = permit.intent
        self.coord._event(
            conn,
            intent.domain_id,
            "lmstudio_" + kind + "_" + _stage(intent),
            _key(intent),
            reference,
        )

    def _load_record(
        self, conn: sqlite3.Connection, permit: LmPermit, operation_id: str
    ) -> str:
        rows = dict(
            conn.execute(
                "SELECT event,reference FROM journal WHERE domain=? AND operation_id=? AND event IN ('lmstudio_profile_load','lmstudio_instance_load')",
                (permit.intent.domain_id, operation_id),
            ).fetchall()
        )
        if (
            rows.get("lmstudio_profile_load") != permit.qualification.profile.digest
            or "lmstudio_instance_load" not in rows
        ):
            raise LmStudioDenied("loaded_profile_unproven")
        return label(rows["lmstudio_instance_load"])

    def _memory(
        self,
        conn: sqlite3.Connection,
        permit: LmPermit,
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
                or decode(row["binding"])["profile"]
                != asdict(permit.qualification.profile.memory)
                or self._load_record(conn, permit, row["work_id"])
                != intent.source_instance_id
            ):
                raise LmStudioDenied("owned_instance_unproven")
            return
        if (
            lease is None
            or sample is None
            or lease.kind != "model"
            or lease.domain_id != intent.domain_id
        ):
            raise LmStudioDenied("memory_required")
        row = self.memory._active(conn, lease)
        if decode(lease.binding)["profile"] != asdict(
            permit.qualification.profile.memory
        ):
            raise LmStudioDenied("memory_profile_mismatch")
        if isinstance(intent, TransitionStep):
            if row["status"] != "reserved" or decode(lease.binding)["step"] != asdict(
                intent
            ):
                raise LmStudioDenied("memory_intent_mismatch")
        elif (
            row["status"] != "dispatched"
            or self._load_record(conn, permit, lease.work_id) != intent.instance_id
        ):
            raise LmStudioDenied("owned_instance_unproven")
        if self.memory._dispatch(conn, lease, sample, now) != isinstance(
            intent, TransitionStep
        ):
            raise LmStudioDenied("memory_dispatch_conflict")

    def _send_marker(
        self,
        permit: LmPermit,
        activity: LmActivity,
        observed: LmObservation,
        digest: str,
        lease: MemoryLease | None,
        sample: MemorySample | None,
    ) -> None:
        now, intent = self._now(), permit.intent

        def commit(conn: sqlite3.Connection) -> None:
            self._active(conn, permit, now)
            self._activity(permit, activity, now)
            instance = (
                intent.instance_id
                if isinstance(intent, (RequestLease, LoadLease))
                else intent.source_instance_id
                if intent.kind == "unload"
                else None
            )
            self._wire(permit, observed, now, instance)
            if conn.execute(
                "SELECT 1 FROM journal WHERE domain=? AND operation_id=? AND event=?",
                (intent.domain_id, _key(intent), "lmstudio_sent_" + _stage(intent)),
            ).fetchone():
                raise LmStudioDenied("send_already_recorded")
            if isinstance(intent, RequestLease):
                try:
                    fit = (
                        self.admit_request(
                            intent, digest, permit.qualification.profile.digest
                        )
                        if self.admit_request is not None
                        else False
                    )
                except Exception:
                    raise LmStudioDenied("request_fit_unavailable") from None
                if fit is not True:
                    raise LmStudioDenied("request_fit_required")
            self._memory(conn, permit, lease, sample, now)
            self._event(conn, permit, "sent", digest)
            if isinstance(intent, TransitionStep) and intent.kind == "load":
                self._event(
                    conn, permit, "profile", permit.qualification.profile.digest
                )

        if sample is None:
            self.coord._call(commit, now_ms=now)
        else:
            self.memory._admission_call(commit, sample, now)

    def _hold(self, permit: LmPermit, lease: MemoryLease | None) -> None:
        self.memory.hold_model(permit.intent, lease, reference="lmstudio_effect_uncertain", now_ms=self._now())

    def drain(self, permit: LmPermit, activity: LmActivity) -> bool:
        if (
            not isinstance(permit.intent, (TransitionStep, ReleaseStep))
            or permit.intent.kind != "drain"
        ):
            raise LmStudioDenied("drain_intent_required")
        now = self._now()
        self.coord._call(lambda conn: self._active(conn, permit, now), now_ms=now)
        self._activity(permit, activity, now)
        if isinstance(permit.intent, ReleaseStep):
            self.coord._call(
                lambda conn: self._memory(conn, permit, None, None, now), now_ms=now
            )
            return self.coord.complete_release(
                permit.intent, reference=activity.reference, now_ms=now
            )
        return self.coord.complete_step(
            permit.intent, reference=activity.reference, now_ms=now
        )

    async def send(
        self,
        permit: LmPermit,
        activity: LmActivity,
        *,
        lease: MemoryLease | None = None,
        sample: MemorySample | None = None,
    ) -> str:
        intent = permit.intent
        if not isinstance(intent, (TransitionStep, ReleaseStep)) or intent.kind not in (
            "load",
            "unload",
        ):
            raise LmStudioDenied("mutation_intent_required")
        now = self._now()
        self.coord._call(lambda conn: self._active(conn, permit, now), now_ms=now)
        observed = await self.observe()
        body = (
            permit.qualification.profile.load_body()
            if intent.kind == "load"
            else encode({"instance_id": intent.source_instance_id})
        )
        self._send_marker(
            permit, activity, observed, hashlib.sha256(body).hexdigest(), lease, sample
        )
        try:
            reply = await self._request("POST", "/api/v1/models/" + intent.kind, body)
            instance_id = label(reply.get("instance_id"))
            if intent.kind == "load":
                if (
                    reply.get("status") != "loaded"
                    or reply.get("type") != "llm"
                    or config(reply.get("load_config"))
                    != permit.qualification.profile.load_config()
                ):
                    raise LmStudioDenied("load_config_unverified")
            elif instance_id != intent.source_instance_id:
                raise LmStudioDenied("unload_instance_mismatch")
            now = self._now()

            def ack(conn: sqlite3.Connection) -> None:
                self._active(
                    conn, permit, now, finishing=isinstance(intent, ReleaseStep)
                )
                self._event(conn, permit, "ack", str(now))
                if intent.kind == "load":
                    self._event(conn, permit, "instance", instance_id)

            self.coord._call(ack, now_ms=now)
            if (
                isinstance(intent, TransitionStep)
                and intent.kind == "load"
                and not self.coord.complete_step(
                    intent, reference="lmstudio_load_ack", now_ms=now
                )
            ):
                raise LmStudioDenied("lifecycle_intent_stale")
            return (
                "verify_required" if intent.kind == "load" else "pending_release_proof"
            )
        except BaseException:
            self._hold(permit, lease)
            raise

    def _proof(
        self,
        permit: LmPermit,
        observed: LmObservation,
        evidence: ReconciliationEvidence,
        *,
        instance: str | None,
    ) -> None:
        if (
            not isinstance(evidence, ReconciliationEvidence)
            or evidence.reference != observed.reference
            or evidence.clock_id != observed.stamp.clock_id
        ):
            raise LmStudioDenied("proof_scope_mismatch")
        inv = next(
            (
                x
                for x in evidence.inventories
                if x.runtime.runtime_id == permit.qualification.runtime_id
            ),
            None,
        )
        if (
            inv is None
            or inv.runtime_version != permit.qualification.version
            or inv.stamp.clock_id != observed.stamp.clock_id
            or inv.stamp.observed_at is None
            or observed.stamp.observed_at is None
            or inv.stamp.observed_at < observed.stamp.observed_at
        ):
            raise LmStudioDenied("proof_scope_mismatch")
        if instance is not None and not any(
            x.instance_id == instance
            and x.artifact_id == _target(permit.intent).artifact_id
            and x.effective_context_tokens
            == permit.qualification.profile.memory.context_tokens
            for x in inv.instances
        ):
            raise LmStudioDenied("proof_instance_config_mismatch")

    async def confirm(
        self,
        permit: LmPermit,
        produce: Callable[[LmObservation], Awaitable[ReconciliationEvidence]],
    ) -> bool:
        intent = permit.intent
        if not isinstance(intent, (TransitionStep, ReleaseStep)) or intent.kind not in (
            "verify",
            "unload",
        ):
            raise LmStudioDenied("confirmation_intent_required")
        now = self._now()

        def check(conn: sqlite3.Connection) -> str | None:
            self._active(conn, permit, now, finishing=isinstance(intent, ReleaseStep))
            stage = "load" if intent.kind == "verify" else _stage(intent)
            if (
                conn.execute(
                    "SELECT 1 FROM journal WHERE domain=? AND operation_id=? AND owner_epoch=? AND generation=? AND event=?",
                    (
                        intent.domain_id,
                        intent.operation_id,
                        intent.owner_epoch,
                        intent.generation,
                        "lmstudio_ack_" + stage,
                    ),
                ).fetchone()
                is None
            ):
                raise LmStudioDenied("ack_missing")
            return (
                self._load_record(conn, permit, intent.operation_id)
                if intent.kind == "verify"
                else None
            )

        instance = self.coord._call(check, now_ms=now)
        observed = await self.observe()
        self._wire(permit, observed, self._now(), instance)
        assert_no_active_write_transaction("LM Studio physical proof producer")
        try:
            async with asyncio.timeout(self.timeout):
                evidence = await produce(observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LmStudioDenied("proof_unavailable") from None
        self._proof(permit, observed, evidence, instance=instance)
        now = self._now()
        self.coord._call(
            lambda conn: self._active(
                conn, permit, now, finishing=isinstance(intent, ReleaseStep)
            ),
            now_ms=now,
        )
        self._wire(permit, observed, now, instance)
        if isinstance(intent, ReleaseStep):
            return self.coord.complete_release(
                intent, reference=observed.reference, now_ms=now, evidence=evidence
            )
        return self.coord.complete_step(
            intent, reference=observed.reference, now_ms=now, evidence=evidence
        )

    async def confirm_load(
        self,
        permit: LmPermit,
        *,
        lease: MemoryLease,
        sample: Callable[[], Awaitable[MemorySample]],
        produce: Callable[[LmObservation], Awaitable[ReconciliationEvidence]],
    ) -> bool:
        """Fresh real control-slot/native/profile/memory proof, never inference.

        Final current authority, model budget and slot completion share one write.
        Native observations/proof/sample production remain outside transactions.
        """
        intent = permit.intent
        if type(intent) is not LoadLease or lease.domain_id != intent.domain_id:
            raise LmStudioDenied("load_control_intent_required")

        def current(conn: sqlite3.Connection, measured: MemorySample) -> None:
            self._active(conn, permit, self._now())
            self.memory._admitted_model(
                conn, lease, permit.qualification.profile.memory, measured, self._now()
            )
            if self._load_record(conn, permit, lease.work_id) != intent.instance_id:
                raise LmStudioDenied("loaded_instance_mismatch")

        async def measured() -> MemorySample:
            assert_no_active_write_transaction("native load memory producer")
            async with asyncio.timeout(5):
                return await sample()

        before = await measured()
        self.coord._call(lambda conn: current(conn, before), now_ms=self._now())
        observed = await self.observe()
        self._wire(permit, observed, self._now(), intent.instance_id)
        after = await measured()
        assert_no_active_write_transaction("native load proof producer")
        try:
            async with asyncio.timeout(5):
                evidence = await produce(observed)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LmStudioDenied("load_control_proof_unavailable") from None
        instance = intent.instance_id
        self._proof(permit, observed, evidence, instance=instance)
        now = self._now()
        self._wire(permit, observed, self._now(), intent.instance_id)

        def finish(conn: sqlite3.Connection) -> bool:
            current(conn, after)
            if not self.coord._finish_load(
                conn, intent, evidence, reference=observed.reference, now_ms=now
            ):
                return False
            self.coord._event(conn, intent.domain_id, 'lmstudio_load_verified', intent.lease_id,
                           _load_confirmation_reference(intent, lease))
            return True

        return self.coord._call(finish, now_ms=now)

    async def infer(
        self,
        permit: LmPermit,
        prepared: LmPreparedInference,
        activity: LmActivity,
        *,
        lease: MemoryLease,
        sample: MemorySample,
        produce_terminal: Callable[[], Awaitable[LmActivity]],
    ) -> AsyncIterator[bytes]:
        """Yield validated compatible-wire JSON packet bytes, not tool executions.

        Exact instance routing, unchanged full request semantics and no JIT must
        be installed-qualified. An echoed response model is NOT routing proof.
        Final tool/usage packets wait for independent execution/terminal receipt.
        """
        intent = permit.intent
        if (
            not isinstance(intent, RequestLease)
            or not isinstance(prepared, LmPreparedInference)
            or not callable(produce_terminal)
        ):
            raise LmStudioDenied("inference_intent_required")
        if (
            prepare_inference(
                intent.instance_id,
                permit.qualification.profile,
                obj(decode(prepared.body)),
            )
            != prepared
            or prepared.digest != permit.request_digest
        ):
            raise LmStudioDenied("prepared_scope_mismatch")
        now = self._now()
        self.coord._call(lambda conn: self._active(conn, permit, now), now_ms=now)
        observed = await self.observe()
        self._send_marker(permit, activity, observed, prepared.digest, lease, sample)
        decoder = LmSseDecoder(intent.instance_id)
        try:
            assert_no_active_write_transaction("LM Studio inference stream")
            async with asyncio.timeout(self.timeout):
                async with self.transport.stream(
                    "/v1/chat/completions", prepared.body, self.timeout
                ) as (status, chunks):
                    self._status(status)
                    async for chunk in chunks:
                        for payload in decoder.feed(chunk):
                            yield payload
                pending = decoder.finish()
                completed_ms = self._now()
                terminal = await produce_terminal()
                observed = await self.observe()
                now = self._now()

                def finish(conn: sqlite3.Connection) -> None:
                    self._active(conn, permit, now, finishing=True)
                    self._activity(permit, terminal, now, after_ms=completed_ms)
                    self._wire(permit, observed, now, intent.instance_id)
                    conn.execute(
                        "UPDATE leases SET status='released' WHERE id=?",
                        (intent.lease_id,),
                    )
                    conn.execute(
                        "UPDATE requests SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'completed' END WHERE id=?",
                        (intent.request_id,),
                    )
                    self._event(conn, permit, "terminal", prepared.digest)
                    self.coord._event(
                        conn,
                        intent.domain_id,
                        "request_finished",
                        ref=terminal.reference,
                    )

                self.coord._call(finish, now_ms=now)
        except (GeneratorExit, asyncio.CancelledError):
            self._hold(permit, lease)
            raise
        except Exception:
            self._hold(permit, lease)
            raise LmStudioDenied("inference_uncertain") from None
        for payload in pending:
            yield payload

    async def cancel_native(self) -> None:
        raise LmStudioDenied("native_cancel_terminality_unqualified")

    async def tokenize(self) -> None:
        raise LmStudioDenied("exact_tokenizer_unqualified")
