"""LC08 bounded cold transition consumer; no default actors/producers or activation.

This is a step driver, NOT a global polling scheduler or a completed control API.
It requires real coordinator steps and concrete native bridge permits. Native
ACKs cannot settle memory or be returned as successful explicit controls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

import lmstudio_lifecycle as lm
import ollama_lifecycle as ol
import omlx_lifecycle as ox
from model_lifecycle_coordinator import (
    LifecycleCoordinator,
    TransitionStep,
    ReconciliationEvidence,
    _encode,
)
from native_dispatch_executor import (
    application_input,
    prepare_ollama_input,
    NativeFitDenied,
)
from request_fit import FitProfile, RequestFitDenied, request_components, decode, number
from request_fit_admission import CounterQualification, validate_counter_metadata
from runtime_dispatch_boundary import RuntimeDispatchCall, RuntimeDispatchDenied
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import (
    SharedMemoryAdmission,
    MemoryLease,
    MemorySample,
    ModelMemoryProfile,
)

Qualification = ol.OllamaQualification | lm.LmQualification


@dataclass(frozen=True, slots=True)
class ColdAdmission:
    """Owned input/readiness assertion, never an inference ticket or load permit."""

    call: RuntimeDispatchCall
    profile: FitProfile
    native: Qualification
    counter: CounterQualification | None

    def validate(self, now: int) -> None:
        inputs = application_input(self.call)
        q = self.native
        if isinstance(q, ol.OllamaQualification):
            ol.validate_qualification(q, now)
            wire = "ollama_native"
            digest = ol._profile_digest(q.profile)
            body = decode(prepare_ollama_input(self.call, q.profile).body)
        elif isinstance(q, lm.LmQualification):
            lm.validate_qualification(q, now)
            wire = "lmstudio_openai"
            digest = q.profile.digest
            if (
                self.call.runtime != "lmstudio"
                or self.call.model != q.profile.memory.target.artifact_id
                or inputs["reasoning_mode"] != "off"
            ):
                raise RequestFitDenied("profile_mismatch")
            if not any(
                c.name == "reasoning_mode_off"
                and c.state_for_installed_scope(
                    runtime_id=q.runtime_id,
                    runtime_version=q.version,
                    profile_fingerprint=q.profile.memory.target.profile_fingerprint,
                )
                == "supported"
                for c in q.capabilities
            ):
                raise RequestFitDenied("profile_mismatch")
            # No made-up instance selector, native request, or token count here.
            # Exact instance/wire validation and fit must run AFTER sole residency.
            body = dict(messages=inputs["messages"])
            if inputs["tools"] is not None:
                body["tools"] = inputs["tools"]
            if any(
                not isinstance(m.get("content"), str)
                and not (
                    m.get("role") == "assistant"
                    and m.get("tool_calls")
                    and m.get("content") is None
                )
                for m in inputs["messages"]
                if isinstance(m, dict)
            ):
                raise RequestFitDenied("unsupported_media")
        else:
            raise RequestFitDenied("profile_mismatch")
        if (
            q.runtime_id != q.profile.memory.target.runtime_id
            or self.profile.memory != q.profile.memory
            or self.profile.wire != wire
            or self.profile.native_profile_digest != digest
            or inputs["max_tokens"] > self.profile.memory.context_tokens
        ):
            raise RequestFitDenied("profile_mismatch")
        version = (
            q.server_version if isinstance(q, ol.OllamaQualification) else q.version
        )
        if self.counter is not None and (
            self.counter.version != version
            or isinstance(q, lm.LmQualification)
            and self.counter.server_identity != q.server_identity
        ):
            raise RequestFitDenied("counter_unqualified")
        features, _ = request_components(body, self.profile.wire)
        validate_counter_metadata(
            self.profile, self.counter, features=features, now_ms=now
        )


@dataclass(frozen=True, slots=True)
class QueuedColdRequest:
    admission: ColdAdmission
    request_id: str
    domain_id: str
    deadline_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OllamaTransition:
    bridge: ol.OllamaLifecycleBridge
    permit: ol.OllamaPermit
    activity: ol.OllamaActivity | None = None
    memory: MemoryLease | None = None
    sample: Callable[[], Awaitable[MemorySample]] | None = None
    proof: (
        Callable[[ol.OllamaObservation], Awaitable[ReconciliationEvidence]] | None
    ) = None

    def _drain(self) -> bool:
        if self.activity is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return self.bridge.drain(self.permit, self.activity)

    async def _send(
        self, *, lease: MemoryLease | None = None, sample: MemorySample | None = None
    ) -> str:
        if self.activity is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return await self.bridge.send(
            self.permit, self.activity, lease=lease, sample=sample
        )

    async def _confirm(self) -> bool:
        if self.proof is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return await self.bridge.confirm(self.permit, self.proof)


@dataclass(frozen=True, slots=True, kw_only=True)
class LmTransition:
    bridge: lm.LmStudioLifecycleBridge
    permit: lm.LmPermit
    activity: lm.LmActivity | None = None
    memory: MemoryLease | None = None
    sample: Callable[[], Awaitable[MemorySample]] | None = None
    proof: Callable[[lm.LmObservation], Awaitable[ReconciliationEvidence]] | None = None

    def _drain(self) -> bool:
        if self.activity is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return self.bridge.drain(self.permit, self.activity)

    async def _send(
        self, *, lease: MemoryLease | None = None, sample: MemorySample | None = None
    ) -> str:
        if self.activity is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return await self.bridge.send(
            self.permit, self.activity, lease=lease, sample=sample
        )

    async def _confirm(self) -> bool:
        if self.proof is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return await self.bridge.confirm(self.permit, self.proof)


@dataclass(frozen=True, slots=True, kw_only=True)
class OmlxSourceTransition:
    """Owned oMLX source release only; NOT an oMLX load/inference qualification.

    LC04's qualification has no fit/memory profile. The separately supplied
    model profile must match both that qualification and the actual reservation.
    No tokenizer, allocated-context inference, or synthetic FitProfile is used.
    """

    bridge: ox.OmlxLifecycleBridge
    permit: ox.OmlxPermit
    profile: ModelMemoryProfile
    activity: ox.OmlxActivity | None = None
    memory: MemoryLease | None = None
    sample: Callable[[], Awaitable[MemorySample]] | None = None
    proof: Callable[[ox.OmlxObservation], Awaitable[ReconciliationEvidence]] | None = (
        None
    )

    def _drain(self) -> bool:
        if self.activity is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return self.bridge.drain(self.permit, self.activity)

    async def _send(
        self, *, lease: MemoryLease | None = None, sample: MemorySample | None = None
    ) -> str:
        if (
            self.permit.step.kind != "unload"
            or self.activity is None
            or lease is not None
            or sample is not None
        ):
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        # Normalize the bridge's ACK vocabulary, not its physical authority.
        if await self.bridge.send(self.permit, self.activity) != "pending":
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        return "pending_release_proof"

    async def _confirm(self) -> bool:
        if self.permit.step.kind != "unload" or self.proof is None:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        proof = self.proof

        async def bounded(observation: ox.OmlxObservation) -> ReconciliationEvidence:
            async with asyncio.timeout(5):
                return await proof(observation)

        return await self.bridge.confirm(self.permit, bounded)


TransitionJob = OllamaTransition | LmTransition | OmlxSourceTransition


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """ONLY this stage completed, not a completed explicit load/unload command."""

    step: TransitionStep
    memory: MemoryLease | None


class NativeTransitionDriver:
    def __init__(
        self,
        coordinator: LifecycleCoordinator,
        memory: SharedMemoryAdmission,
        *,
        clock_ms: Callable[[], int],
    ):
        if memory._coordinator is not coordinator:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable")
        self.coord, self.memory, self.clock = coordinator, memory, clock_ms

    def _now(self) -> int:
        return number(self.clock())

    def enqueue(
        self,
        admission: ColdAdmission,
        request_id: str,
        domain_id: str,
        *,
        deadline_ms: int,
    ) -> QueuedColdRequest:
        assert_no_active_write_transaction("cold request readiness")
        try:
            admission.validate(self._now())  # BEFORE enqueue/generation/claim/probe.
            status = self.coord.enqueue(
                request_id,
                domain_id,
                admission.profile.memory.target,
                deadline_ms=deadline_ms,
                now_ms=self._now(),
            )
            if status != "queued":
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            return QueuedColdRequest(admission, request_id, domain_id, deadline_ms)
        except RequestFitDenied as exc:
            raise NativeFitDenied(exc) from None
        except Exception:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None

    async def advance(
        self,
        step: TransitionStep,
        queued: QueuedColdRequest,
        job: TransitionJob,
    ) -> TransitionResult:
        assert_no_active_write_transaction("native transition")
        held = None
        valid = False
        try:
            # Check the actual dispatched step before accepting any supplied job.
            def current(conn):
                if self.coord._active_step(conn, step, self._now()) is None:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                row = conn.execute(
                    "SELECT * FROM requests WHERE id=?", (queued.request_id,)
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "queued"
                    or row["cancel_requested"]
                    or row["domain"] != step.domain_id
                    or queued.domain_id != step.domain_id
                    or row["deadline"] != queued.deadline_ms
                    or row["deadline"] <= self._now()
                    or row["target"] != _encode(queued.admission.profile.memory.target)
                    or step.target != queued.admission.profile.memory.target
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")

            self.coord._call(current, now_ms=self._now())
            valid = True
            queued.admission.validate(self._now())
            if type(job) not in (OllamaTransition, LmTransition, OmlxSourceTransition):
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            if isinstance(job, OmlxSourceTransition):
                if (
                    not isinstance(job.bridge, ox.OmlxLifecycleBridge)
                    or step.kind not in ("drain", "unload")
                    or job.permit.step != step
                    or not isinstance(job.profile, ModelMemoryProfile)
                    or job.profile.target != job.permit.qualification.target
                    or job.profile.context_tokens
                    != job.permit.qualification.context_tokens
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                coordinator, memory = job.bridge._coordinator, job.bridge._memory
                profile = job.profile
            else:
                if (
                    job.permit.intent != step
                    or isinstance(job, OllamaTransition)
                    and not isinstance(job.bridge, ol.OllamaLifecycleBridge)
                    or isinstance(job, LmTransition)
                    and not isinstance(job.bridge, lm.LmStudioLifecycleBridge)
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                coordinator = (
                    job.bridge.coordinator
                    if isinstance(job, OllamaTransition)
                    else job.bridge.coord
                )
                memory = job.bridge.memory
                profile = job.permit.qualification.profile.memory
            if coordinator is not self.coord or memory is not self.memory:
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            subject = step.source if step.kind in ("drain", "unload") else step.target
            if profile.target != subject:
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            if (
                step.kind in ("load", "verify")
                and job.permit.qualification != queued.admission.native
            ):
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            if job.memory is not None:
                existing = job.memory

                def memory_binding(conn):
                    self.memory._active(conn, existing)
                    if (
                        existing.kind != "model"
                        or existing.domain_id != step.domain_id
                        or decode(existing.binding.encode())["profile"]
                        != asdict(profile)
                    ):
                        raise RuntimeDispatchDenied("coordinated_execution_unavailable")

                self.coord._call(memory_binding, now_ms=self._now())
                held = existing

            async def sample() -> MemorySample:
                if job.sample is None:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                assert_no_active_write_transaction("native transition memory producer")
                async with asyncio.timeout(5):
                    return await job.sample()

            if step.kind == "drain":
                if job.activity is None or job._drain() is not True:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            elif step.kind == "load":
                if held is not None or job.activity is None:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                held = self.memory.reserve_model(
                    step,
                    queued.admission.profile.memory,
                    await sample(),
                    now_ms=self._now(),
                )
                if (
                    await job._send(lease=held, sample=await sample())
                    != "verify_required"
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            elif step.kind == "unload":
                if (
                    held is None
                    or job.activity is None
                    or job.proof is None
                    or job.sample is None
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                if await job._send() != "pending_release_proof":
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                if await job._confirm() is not True:
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
                # Actual domain-wide physical release precedes attributed zero.
                self.memory.settle(
                    held,
                    await sample(),
                    terminal=True,
                    reference="native_transition_release_verified",
                    now_ms=self._now(),
                )
                held = None
            elif step.kind == "verify":
                if (
                    held is None
                    or job.proof is None
                    or await job._confirm() is not True
                ):
                    raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            else:
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            return TransitionResult(step, held)
        except BaseException as exc:
            # No retry/release/next-step inference on an interrupted stage. Native
            # bridges additionally retain their own send markers and late effects.
            if valid:
                self.coord.mark_uncertain(
                    step, reference="native_transition_uncertain", now_ms=self._now()
                )
                if held is not None:
                    self.memory.mark_uncertain(
                        held,
                        reference="native_transition_uncertain",
                        now_ms=self._now(),
                    )
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, RequestFitDenied):
                raise NativeFitDenied(exc) from None
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None
