"""Time, token, cost, tool, cancellation, and durable recovery execution budgets.

AP-007 adds optional *bounded-stop* limits to the same durable budget document:
no-progress, repair attempts, an absolute deadline, memory and storage ceilings,
and an isolation requirement.  A limit that is not declared is not persisted, so
documents and budget identities written before AP-007 are byte-for-byte unchanged.

AP-007-F2 makes isolation proof mandatory for consequential actions of
mission-owned work, whether or not a budget declares the requirement, and judges
the proof against a recorded :class:`IsolationAcceptance`.  No acceptance is
recorded in this build, so every such dispatch is refused.

AP-007-F3 adds two conditions to that proof.  The qualification must rest on a
report that stated its own generation time, and the proof must come from the
recorded target host: the host of the qualification run and the host observed for
the dispatch must both be the host the acceptance names.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import runtime_persistence


class BudgetError(ValueError):
    """Raised when a budget or usage value is invalid."""


class BudgetExceeded(RuntimeError):
    """Raised when an operation would exceed an execution or recovery budget."""


class ExecutionCancelled(RuntimeError):
    """Raised when cancellation has been requested."""


class IsolationUnavailable(RuntimeError):
    """Raised when a task requires runtime isolation that is absent or unqualified.

    The refusal is final for that dispatch: there is no host-execution fallback.
    """


#: Machine-readable reasons a bounded stop can be committed (AP-007).
BOUNDED_STOP_CODES: tuple[str, ...] = (
    "no_progress",
    "repair_attempts",
    "deadline",
    "memory",
    "storage",
    "measurement_unavailable",
)
#: Resources whose declared ceiling needs a reading before work may continue.
MEASURED_RESOURCES: tuple[str, ...] = ("memory", "storage")
#: Budget-spec keys introduced by AP-007.  They are declared by server code only.
#: The HTTP boundary refuses them: no route measures progress, repair, memory or
#: storage yet, and no isolated runtime is qualified, so accepting a limit there
#: would promise protection that nothing enforces.
BOUNDED_LIMIT_FIELDS: tuple[str, ...] = (
    "max_no_progress",
    "max_repair_attempts",
    "deadline_at",
    "max_memory_bytes",
    "max_storage_bytes",
    "isolation_required",
)


#: Activation steps of isolated execution, in the order they are activated
#: (AP-007-F2).  A step is usable only when a recorded :class:`IsolationAcceptance`
#: names it; every step that is not named stays refused.
CONTAINED_EXECUTION = "contained_execution"
BROWSER_WORKER = "browser_worker"
NATIVE_LIFECYCLE = "native_lifecycle"
ISOLATION_STEPS: tuple[str, ...] = (CONTAINED_EXECUTION, BROWSER_WORKER, NATIVE_LIFECYCLE)

_ACCEPTANCE_RUNTIME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
#: A full content digest, optionally prefixed with the repository it was pulled from.
#: A shortened id is not a pin.
_ACCEPTANCE_DIGEST_RE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}@)?sha256:[0-9a-f]{64}")
#: A host fingerprint: the digest of a machine description, as
#: ``sandbox_boundary.qualification_host_fingerprint`` writes it (AP-007-F3).
_HOST_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _acceptance_text(value: Any, name: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise BudgetError(f"{name} must be a string.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_len or any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise BudgetError(f"{name} must be 1 to {max_len} printable characters.")
    return cleaned


def _positive_finite_time(value: Any) -> float:
    """Return ``value`` as epoch seconds, or ``0.0`` when it is not a usable time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    stamp = float(value)
    return stamp if math.isfinite(stamp) and stamp > 0 else 0.0


def _host_fingerprint(value: Any) -> str:
    """Return ``value`` as a host fingerprint, or ``""`` when it is not a well-formed one."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    return cleaned if _HOST_FINGERPRINT_RE.fullmatch(cleaned) else ""


@dataclass(frozen=True, slots=True)
class IsolationAcceptance:
    """What a recorded owner decision accepts as runtime isolation (AP-007-F2).

    Server composition hands one to :class:`DurableTaskBudgetManager`.  It is never
    parsed from a request body, from model tool arguments or from a planning
    document.  While no decision is recorded there is none, and every dispatch that
    needs isolation is refused.

    The record pins one container backend (runtime, runtime version and image
    digest), the earliest qualification time it accepts, and the activation steps it
    covers.  Steps are activated in the order of :data:`ISOLATION_STEPS`: a later
    step cannot be recorded without the earlier ones.

    AP-007-F3: the record also names the target host, as the fingerprint of its
    machine description (``sandbox_boundary.qualification_host_fingerprint``).  The
    fingerprint identifies a kind of machine, not one unit: two machines of the same
    model and memory size share it.
    """

    decision_ref: str
    runtime: str
    runtime_version: str
    image_digest: str
    qualified_not_before: float
    target_host: str
    activated_steps: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_ref", _acceptance_text(self.decision_ref, "decision_ref", max_len=200))
        runtime = _acceptance_text(self.runtime, "runtime", max_len=64).lower()
        if not _ACCEPTANCE_RUNTIME_RE.fullmatch(runtime):
            raise BudgetError("runtime must be a container runtime name such as `docker`.")
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(
            self, "runtime_version", _acceptance_text(self.runtime_version, "runtime_version", max_len=80)
        )
        digest = _acceptance_text(self.image_digest, "image_digest", max_len=300)
        if not _ACCEPTANCE_DIGEST_RE.fullmatch(digest):
            raise BudgetError("image_digest must be a full sha256 content digest.")
        object.__setattr__(self, "image_digest", digest)
        not_before = _positive_finite_time(self.qualified_not_before)
        if not not_before:
            raise BudgetError("qualified_not_before must be a positive finite epoch timestamp.")
        object.__setattr__(self, "qualified_not_before", not_before)
        target_host = _host_fingerprint(_acceptance_text(self.target_host, "target_host", max_len=80))
        if not target_host:
            raise BudgetError("target_host must be a host fingerprint: `sha256:` and 64 lowercase hex digits.")
        object.__setattr__(self, "target_host", target_host)
        steps = self.activated_steps
        if isinstance(steps, (str, bytes)) or not isinstance(steps, (frozenset, set, tuple, list)):
            raise BudgetError("activated_steps must be a collection of step names.")
        if any(not isinstance(step, str) or step not in ISOLATION_STEPS for step in steps):
            raise BudgetError(f"activated_steps must name steps from {ISOLATION_STEPS}.")
        activated = frozenset(steps)
        if activated != frozenset(ISOLATION_STEPS[: len(activated)]):
            raise BudgetError(
                f"activated_steps must follow the activation order {ISOLATION_STEPS}: "
                "a later step cannot be recorded without the earlier ones."
            )
        object.__setattr__(self, "activated_steps", activated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_ref": self.decision_ref,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "image_digest": self.image_digest,
            "qualified_not_before": self.qualified_not_before,
            "target_host": self.target_host,
            "activated_steps": [step for step in ISOLATION_STEPS if step in self.activated_steps],
        }


@dataclass(frozen=True, slots=True)
class IsolationEvidence:
    """Server-observed runtime isolation state for one dispatch (AP-007).

    Coordination evidence, never user or model authority: server code builds it
    from the sandbox admission decision; it is never parsed from a request body or
    from model tool arguments.

    AP-007-F2: the evidence also names the backend's runtime, runtime version and
    image digest, the qualification it rests on with the time of the qualification
    run, and the one activation step it proves.  They are compared with the recorded
    :class:`IsolationAcceptance`.

    AP-007-F3: the evidence names two hosts, each as a host fingerprint.
    ``qualified_host`` is the host the qualification report was produced on, as the
    qualification record says.  ``observed_host`` is the host the server observed
    itself to run on for this dispatch.  Both must be the recorded target host.
    """

    execution_mode: str
    admitted: bool
    qualified: bool
    backend_id: str = ""
    error_code: str = ""
    reason: str = ""
    runtime: str = ""
    runtime_version: str = ""
    image_digest: str = ""
    qualification_id: str = ""
    qualified_at: float = 0.0
    step: str = CONTAINED_EXECUTION
    qualified_host: str = ""
    observed_host: str = ""

    @classmethod
    def from_admission(
        cls,
        decision: Any,
        *,
        execution_mode: str,
        qualification: Any = None,
        observed_host: Any = None,
    ) -> IsolationEvidence:
        """Adapt a ``sandbox_boundary.SecurityAdmissionDecision`` without importing it.

        Only the literal ``True`` counts: a missing attribute, a truthy non-boolean
        or an absent backend reads as "not admitted / not qualified".

        ``qualification`` is the active backend's ``BackendQualificationRecord``.  It
        is used only when it names that backend and its verdict is ``qualified``;
        otherwise the evidence carries no qualification and cannot be accepted.  A
        qualification is as old as the run that produced it: ``qualified_at`` is the
        earlier of the time the record was written and the time its report was
        generated, so recording an old report again does not make it fresh.  A record
        without both times carries no qualification time.  A sandbox admission proves
        the contained-execution step and no other.

        AP-007-F3: the record must say that its report stated a generation time of
        its own (``report_time_stated`` is the literal ``True``).  When a report
        states none, the sandbox layer records the time of ingestion, which says
        nothing about the age of the run; such a record carries no qualification
        time here.  ``qualified_host`` is the record's ``host_fingerprint``.
        ``observed_host`` is the fingerprint of the host this server runs on.  Server
        code observes it; it is never taken from a request or from the record.  A
        value that is not a well-formed fingerprint counts as absent.
        """
        backend = getattr(decision, "active_backend", None)
        backend_id = str(getattr(backend, "backend_id", "") or "")[:64]
        qualification_id = ""
        qualified_at = 0.0
        qualified_host = ""
        if (
            backend_id
            and getattr(qualification, "verdict", None) == "qualified"
            and str(getattr(qualification, "backend_id", "") or "") == backend_id
        ):
            qualification_id = str(getattr(qualification, "qualification_id", "") or "").strip()[:120]
            recorded_at = _positive_finite_time(getattr(qualification, "qualified_at", None))
            run_at = _positive_finite_time(getattr(qualification, "report_generated_at", None))
            time_stated = getattr(qualification, "report_time_stated", None) is True
            qualified_at = min(recorded_at, run_at) if time_stated and recorded_at and run_at else 0.0
            qualified_host = _host_fingerprint(getattr(qualification, "host_fingerprint", None))
        return cls(
            execution_mode=str(execution_mode or "").strip().lower(),
            admitted=getattr(decision, "allowed", None) is True,
            qualified=getattr(decision, "container_qualified", None) is True,
            backend_id=backend_id,
            error_code=str(getattr(decision, "error_code", "") or "")[:80],
            reason=str(getattr(decision, "reason", "") or "")[:300],
            runtime=str(getattr(backend, "runtime", "") or "").strip().lower()[:64],
            runtime_version=str(getattr(backend, "runtime_version", "") or "").strip()[:80],
            image_digest=str(getattr(backend, "image_digest", "") or "").strip()[:300],
            qualification_id=qualification_id,
            qualified_at=qualified_at,
            step=CONTAINED_EXECUTION,
            qualified_host=qualified_host,
            observed_host=_host_fingerprint(observed_host),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "admitted": self.admitted,
            "qualified": self.qualified,
            "backend_id": self.backend_id,
            "error_code": self.error_code,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "image_digest": self.image_digest,
            "qualification_id": self.qualification_id,
            "qualified_at": self.qualified_at,
            "step": self.step,
            "qualified_host": self.qualified_host,
            "observed_host": self.observed_host,
        }


def isolation_refusal(
    evidence: IsolationEvidence | None,
    *,
    acceptance: IsolationAcceptance | None = None,
    step: str = CONTAINED_EXECUTION,
) -> str:
    """Return why isolated execution must be refused, or ``""`` when it is proven.

    There is no downgrade path: ``host`` and ``disabled`` modes are never isolation,
    and missing evidence is a refusal rather than an assumption.

    AP-007-F2: proof is judged against a recorded ``acceptance``.  Without one
    nothing is accepted.  ``step`` is the activation step the dispatch belongs to; it
    must be activated by the acceptance, and the evidence must prove that step.  The
    backend must be the pinned container runtime, runtime version and image, and its
    qualification must not predate the acceptance.  Contained execution is the only
    step this build defines evidence for, so the later steps are always refused.

    AP-007-F3: a qualification without a usable time of its run is refused with its
    own reason, and so is proof that does not come from the recorded target host.
    The host of the qualification run and the host observed for the dispatch must
    each be a well-formed fingerprint equal to the one the acceptance names.
    """
    wanted = step if isinstance(step, str) else ""
    if wanted not in ISOLATION_STEPS:
        return "runtime isolation is required but the dispatch names no known activation step"
    if not isinstance(acceptance, IsolationAcceptance):
        return "runtime isolation is required but no accepted isolation backend is recorded"
    if wanted not in acceptance.activated_steps:
        return f"runtime isolation is required but the `{wanted}` step is not activated by the recorded acceptance"
    if wanted != CONTAINED_EXECUTION:
        return f"runtime isolation is required but no evidence is defined for the `{wanted}` step"
    if not isinstance(evidence, IsolationEvidence):
        return "runtime isolation is required but no isolation evidence was presented"
    if evidence.step != wanted:
        proven = evidence.step if isinstance(evidence.step, str) and evidence.step in ISOLATION_STEPS else "unknown"
        return f"runtime isolation is required but the evidence proves the `{proven}` step, not `{wanted}`"
    mode = str(evidence.execution_mode or "").strip().lower()
    if mode != "sandbox":
        return f"runtime isolation is required but the execution mode is `{mode or 'unknown'}`, not `sandbox`"
    if evidence.admitted is not True:
        detail = evidence.error_code or "no reason recorded"
        return f"runtime isolation is required but sandbox admission is refused ({detail})"
    if evidence.qualified is not True or not str(evidence.backend_id or "").strip():
        return "runtime isolation is required but no qualified sandbox backend is active"
    observed = (
        str(evidence.runtime or "").strip().lower(),
        str(evidence.runtime_version or "").strip(),
        str(evidence.image_digest or "").strip(),
    )
    if observed != (acceptance.runtime, acceptance.runtime_version, acceptance.image_digest):
        return "runtime isolation is required but the active backend is not the accepted runtime and image"
    if not str(evidence.qualification_id or "").strip():
        return "runtime isolation is required but the active backend names no qualification"
    qualified_at = _positive_finite_time(evidence.qualified_at)
    if not qualified_at:
        return "runtime isolation is required but the backend qualification states no usable time for its run"
    if qualified_at < acceptance.qualified_not_before:
        return "runtime isolation is required but the backend qualification predates the recorded acceptance"
    qualified_host = _host_fingerprint(evidence.qualified_host)
    if not qualified_host:
        return "runtime isolation is required but the backend qualification names no host"
    if qualified_host != acceptance.target_host:
        return "runtime isolation is required but the backend qualification was not run on the accepted target host"
    observed_host = _host_fingerprint(evidence.observed_host)
    if not observed_host:
        return "runtime isolation is required but the host of this dispatch was not observed"
    if observed_host != acceptance.target_host:
        return "runtime isolation is required but this host is not the accepted target host"
    return ""


@dataclass(frozen=True, slots=True)
class BoundedStopVerdict:
    """Outcome of one bounded observation against leaf and root limits (AP-007)."""

    stopped: bool
    code: str
    reason: str
    scope: str
    task_id: str
    leaf: TaskBudgetState
    root: TaskBudgetState

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "code": self.code,
            "reason": self.reason,
            "scope": self.scope,
            "task_id": self.task_id,
        }


class BoundedStopRequired(BudgetExceeded):
    """Raised after a bounded stop has been durably committed (AP-007)."""

    def __init__(self, verdict: BoundedStopVerdict) -> None:
        super().__init__(f"{verdict.scope} task {verdict.task_id!r}: {verdict.reason}")
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_seconds: float
    max_tokens: int
    max_cost: float
    max_tools: int
    max_retries: int = 0

    def __post_init__(self) -> None:
        if self.max_seconds <= 0 or self.max_tokens < 0 or self.max_cost < 0 or self.max_tools < 0 or self.max_retries < 0:
            raise BudgetError("budget limits must be non-negative, with max_seconds positive.")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    elapsed_seconds: float
    tokens: int
    cost: float
    tools: int
    retries: int


class ExecutionBudget:
    """Thread-safe budget guard with monotonic time and explicit cancellation."""

    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._tokens = 0
        self._cost = 0.0
        self._tools = 0
        self._retries = 0
        self._cancelled = False
        self._lock = threading.RLock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def _usage(self) -> BudgetUsage:
        return BudgetUsage(self._clock() - self._started, self._tokens, self._cost, self._tools, self._retries)

    def usage(self) -> BudgetUsage:
        with self._lock:
            return self._usage()

    def check(self) -> BudgetUsage:
        with self._lock:
            usage = self._usage()
            if self._cancelled:
                raise ExecutionCancelled("execution was cancelled.")
            if usage.elapsed_seconds > self.limits.max_seconds:
                raise BudgetExceeded("time budget exceeded.")
            if usage.tokens > self.limits.max_tokens:
                raise BudgetExceeded("token budget exceeded.")
            if usage.cost > self.limits.max_cost:
                raise BudgetExceeded("cost budget exceeded.")
            if usage.tools > self.limits.max_tools:
                raise BudgetExceeded("tool budget exceeded.")
            if usage.retries > self.limits.max_retries:
                raise BudgetExceeded("retry budget exceeded.")
            return usage

    def consume(self, *, tokens: int = 0, cost: float = 0.0, tools: int = 0) -> BudgetUsage:
        if isinstance(tokens, bool) or tokens < 0 or isinstance(tools, bool) or tools < 0 or cost < 0:
            raise BudgetError("usage increments must be non-negative.")
        with self._lock:
            self._tokens += tokens
            self._cost += cost
            self._tools += tools
            return self.check()

    def retry(self) -> BudgetUsage:
        with self._lock:
            self._retries += 1
            return self.check()

    def allow_retry(self) -> bool:
        try:
            self.retry()
        except BudgetExceeded:
            return False
        return True


def _non_neg_int(value: Any, name: str, *, min_value: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < min_value:
        raise BudgetError(f"{name} must be an integer >= {min_value}.")
    return int(value)


def _non_neg_float(value: Any, name: str, *, strictly_positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError(f"{name} must be numeric.")
    val = float(value)
    if strictly_positive and val <= 0:
        raise BudgetError(f"{name} must be positive.")
    if not strictly_positive and val < 0:
        raise BudgetError(f"{name} must be non-negative.")
    return val


def _optional_limit(raw: Any, key: str, *, min_value: int) -> int | None:
    """Parse an optional integer ceiling; anything but a real integer fails closed."""
    value = raw.get(key) if isinstance(raw, dict) else None
    if value is None:
        return None
    return _non_neg_int(value, key, min_value=min_value)


def _optional_deadline(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError("deadline_at must be numeric epoch seconds.")
    deadline = float(value)
    if not math.isfinite(deadline) or deadline <= 0:
        raise BudgetError("deadline_at must be a positive finite epoch timestamp.")
    return deadline


def _measured_markers(value: Any) -> tuple[str, ...]:
    if value is None or value == ():
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise BudgetError("unmeasured must be a list of resource names.")
    cleaned: list[str] = []
    for item in value:
        if item not in MEASURED_RESOURCES:
            raise BudgetError(f"unmeasured resource must be one of {MEASURED_RESOURCES}.")
        if item not in cleaned:
            cleaned.append(str(item))
    return tuple(sorted(cleaned))


@dataclass(frozen=True, slots=True)
class TaskBudgetSpec:
    """Durable root-task and crash-recovery budget bounds (M5-P1-05)."""

    max_steps: int = 25
    max_tool_calls: int = 120
    max_tokens: int = 500_000
    max_elapsed_s: float = 1_800.0
    max_recoveries: int = 3
    # AP-007 bounded-stop limits.  ``None`` / ``False`` means "not declared": the key
    # is omitted from the persisted document, which keeps every pre-AP-007 budget
    # identity stable while making a newly declared limit an authority change.
    max_no_progress: int | None = None
    max_repair_attempts: int | None = None
    deadline_at: float | None = None
    max_memory_bytes: int | None = None
    max_storage_bytes: int | None = None
    isolation_required: bool = False

    def __post_init__(self) -> None:
        _non_neg_int(self.max_steps, "max_steps", min_value=1)
        _non_neg_int(self.max_tool_calls, "max_tool_calls", min_value=0)
        _non_neg_int(self.max_tokens, "max_tokens", min_value=0)
        _non_neg_float(self.max_elapsed_s, "max_elapsed_s", strictly_positive=True)
        _non_neg_int(self.max_recoveries, "max_recoveries", min_value=0)
        if self.max_no_progress is not None:
            _non_neg_int(self.max_no_progress, "max_no_progress", min_value=0)
        if self.max_repair_attempts is not None:
            _non_neg_int(self.max_repair_attempts, "max_repair_attempts", min_value=0)
        _optional_deadline(self.deadline_at)
        if self.max_memory_bytes is not None:
            _non_neg_int(self.max_memory_bytes, "max_memory_bytes", min_value=1)
        if self.max_storage_bytes is not None:
            _non_neg_int(self.max_storage_bytes, "max_storage_bytes", min_value=1)
        if not isinstance(self.isolation_required, bool):
            raise BudgetError("isolation_required must be a boolean.")

    @property
    def declares_bounded_limits(self) -> bool:
        return any(
            value is not None
            for value in (
                self.max_no_progress,
                self.max_repair_attempts,
                self.deadline_at,
                self.max_memory_bytes,
                self.max_storage_bytes,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_elapsed_s": round(self.max_elapsed_s, 4),
            "max_recoveries": self.max_recoveries,
        }
        if self.max_no_progress is not None:
            payload["max_no_progress"] = self.max_no_progress
        if self.max_repair_attempts is not None:
            payload["max_repair_attempts"] = self.max_repair_attempts
        if self.deadline_at is not None:
            payload["deadline_at"] = round(self.deadline_at, 4)
        if self.max_memory_bytes is not None:
            payload["max_memory_bytes"] = self.max_memory_bytes
        if self.max_storage_bytes is not None:
            payload["max_storage_bytes"] = self.max_storage_bytes
        if self.isolation_required:
            payload["isolation_required"] = True
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskBudgetSpec:
        raw = data or {}
        isolation = raw.get("isolation_required", False)
        if not isinstance(isolation, bool):
            raise BudgetError("isolation_required must be a boolean.")
        return cls(
            max_steps=int(raw.get("max_steps", 25)),
            max_tool_calls=int(raw.get("max_tool_calls", 120)),
            max_tokens=int(raw.get("max_tokens", 500_000)),
            max_elapsed_s=float(raw.get("max_elapsed_s", 1_800.0)),
            max_recoveries=int(raw.get("max_recoveries", 3)),
            max_no_progress=_optional_limit(raw, "max_no_progress", min_value=0),
            max_repair_attempts=_optional_limit(raw, "max_repair_attempts", min_value=0),
            deadline_at=_optional_deadline(raw.get("deadline_at")),
            max_memory_bytes=_optional_limit(raw, "max_memory_bytes", min_value=1),
            max_storage_bytes=_optional_limit(raw, "max_storage_bytes", min_value=1),
            isolation_required=isolation,
        )


@dataclass(frozen=True, slots=True)
class TaskBudgetState:
    """Persisted task + recovery budget counters and safe-boundary flags."""

    spec: TaskBudgetSpec
    steps_used: int = 0
    tool_calls_used: int = 0
    tokens_used: int = 0
    elapsed_s: float = 0.0
    recoveries_used: int = 0
    pause_requested: bool = False
    cancel_requested: bool = False
    exhausted_reason: str = ""
    verification_contract: dict[str, Any] | None = None
    # AP-007 bounded-stop counters; persisted only when they are not at their default.
    no_progress_count: int = 0
    repair_attempts_used: int = 0
    memory_bytes_peak: int = 0
    storage_bytes_peak: int = 0
    unmeasured: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "spec": self.spec.to_dict(),
            "steps_used": self.steps_used,
            "tool_calls_used": self.tool_calls_used,
            "tokens_used": self.tokens_used,
            "elapsed_s": round(self.elapsed_s, 4),
            "recoveries_used": self.recoveries_used,
            "pause_requested": self.pause_requested,
            "cancel_requested": self.cancel_requested,
            "exhausted_reason": self.exhausted_reason,
        }
        if self.no_progress_count:
            payload["no_progress_count"] = self.no_progress_count
        if self.repair_attempts_used:
            payload["repair_attempts_used"] = self.repair_attempts_used
        if self.memory_bytes_peak:
            payload["memory_bytes_peak"] = self.memory_bytes_peak
        if self.storage_bytes_peak:
            payload["storage_bytes_peak"] = self.storage_bytes_peak
        if self.unmeasured:
            payload["unmeasured"] = list(self.unmeasured)
        return payload

    def to_json(self) -> str:
        payload = self.to_dict()
        if self.verification_contract is not None:
            payload["verification_contract"] = self.verification_contract
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(
        cls,
        raw: str | None,
        *,
        default_spec: TaskBudgetSpec | None = None,
    ) -> TaskBudgetState:
        fallback = default_spec or TaskBudgetSpec()
        text = (raw or "").strip()
        if not text or text == "{}":
            return cls(spec=fallback)
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise BudgetError("persisted task budget JSON is malformed.") from exc
        if not isinstance(parsed, dict):
            raise BudgetError("persisted task budget JSON must be an object.")
        spec_raw = parsed.get("spec")
        if isinstance(spec_raw, dict):
            spec = TaskBudgetSpec.from_dict(spec_raw)
        else:
            spec = TaskBudgetSpec(
                max_steps=int(parsed.get("max_steps", fallback.max_steps)),
                max_tool_calls=int(parsed.get("max_tool_calls", fallback.max_tool_calls)),
                max_tokens=int(parsed.get("max_tokens", fallback.max_tokens)),
                max_elapsed_s=float(parsed.get("max_elapsed_s", fallback.max_elapsed_s)),
                max_recoveries=int(parsed.get("max_recoveries", fallback.max_recoveries)),
            )
        vc_raw = parsed.get("verification_contract")
        vc_dict = dict(vc_raw) if isinstance(vc_raw, dict) else None
        return cls(
            spec=spec,
            steps_used=_non_neg_int(int(parsed.get("steps_used", 0)), "steps_used"),
            tool_calls_used=_non_neg_int(int(parsed.get("tool_calls_used", 0)), "tool_calls_used"),
            tokens_used=_non_neg_int(int(parsed.get("tokens_used", 0)), "tokens_used"),
            elapsed_s=_non_neg_float(float(parsed.get("elapsed_s", 0.0)), "elapsed_s"),
            recoveries_used=_non_neg_int(int(parsed.get("recoveries_used", 0)), "recoveries_used"),
            pause_requested=bool(parsed.get("pause_requested", False)),
            cancel_requested=bool(parsed.get("cancel_requested", False)),
            exhausted_reason=str(parsed.get("exhausted_reason", "")),
            verification_contract=vc_dict,
            no_progress_count=_non_neg_int(parsed.get("no_progress_count", 0), "no_progress_count"),
            repair_attempts_used=_non_neg_int(parsed.get("repair_attempts_used", 0), "repair_attempts_used"),
            memory_bytes_peak=_non_neg_int(parsed.get("memory_bytes_peak", 0), "memory_bytes_peak"),
            storage_bytes_peak=_non_neg_int(parsed.get("storage_bytes_peak", 0), "storage_bytes_peak"),
            unmeasured=_measured_markers(parsed.get("unmeasured")),
        )

    @property
    def measurement_gaps(self) -> tuple[str, ...]:
        """Declared ceilings whose most recent measurement attempt failed."""
        limits = {"memory": self.spec.max_memory_bytes, "storage": self.spec.max_storage_bytes}
        return tuple(name for name in self.unmeasured if limits.get(name) is not None)

    def bounded_stop(self, *, now: float | None = None) -> tuple[str, str] | None:
        """Return ``(code, reason)`` when a declared AP-007 limit requires a stop.

        The verdict is derived from persisted counters and the clock, never from a
        caller's claim, so it is the same after a restart.  ``now`` defaults to the
        wall clock: a caller that forgets to pass it still enforces the deadline.
        """
        spec = self.spec
        if spec.max_no_progress is not None and self.no_progress_count > spec.max_no_progress:
            return (
                "no_progress",
                f"no-progress limit exceeded ({self.no_progress_count}/{spec.max_no_progress} "
                "consecutive observations without progress)",
            )
        if spec.max_repair_attempts is not None and self.repair_attempts_used > spec.max_repair_attempts:
            return (
                "repair_attempts",
                f"repair-attempt limit exceeded ({self.repair_attempts_used}/{spec.max_repair_attempts})",
            )
        if spec.deadline_at is not None:
            current = time.time() if now is None else float(now)
            if current >= spec.deadline_at:
                # The reason names only the persisted deadline, so it is identical on
                # every evaluation and a stored report stays hash-stable.
                return "deadline", f"deadline exceeded (deadline_at={spec.deadline_at:.2f})"
        for resource, limit, peak in (
            ("memory", spec.max_memory_bytes, self.memory_bytes_peak),
            ("storage", spec.max_storage_bytes, self.storage_bytes_peak),
        ):
            if limit is None:
                continue
            if resource in self.unmeasured:
                return (
                    "measurement_unavailable",
                    f"{resource} limit is declared but {resource} use could not be measured",
                )
            if peak > limit:
                return resource, f"{resource} limit exceeded ({peak}/{limit} bytes)"
        return None

    def violation_reason(self, *, check_recovery: bool = False, now: float | None = None) -> str:
        if self.steps_used > self.spec.max_steps:
            return f"step budget exceeded ({self.steps_used}/{self.spec.max_steps})"
        if self.tool_calls_used > self.spec.max_tool_calls:
            return f"tool budget exceeded ({self.tool_calls_used}/{self.spec.max_tool_calls})"
        if self.tokens_used > self.spec.max_tokens:
            return f"token budget exceeded ({self.tokens_used}/{self.spec.max_tokens})"
        if self.elapsed_s > self.spec.max_elapsed_s:
            return f"time budget exceeded ({self.elapsed_s:.2f}s/{self.spec.max_elapsed_s:.2f}s)"
        if check_recovery and self.recoveries_used > self.spec.max_recoveries:
            return f"recovery budget exceeded ({self.recoveries_used}/{self.spec.max_recoveries})"
        bounded = self.bounded_stop(now=now)
        if bounded is not None:
            return bounded[1]
        return ""


def _mission_owns_chain(conn: sqlite3.Connection, task_ids: Sequence[str]) -> bool:
    """True when a task of the leaf-to-root chain is a task of a mission plan (AP-007-F2).

    This is the ownership rule of the mission stop and of the late-result fence: the
    task is a task of a mission plan, or a descendant of one.  The task a plan hangs
    from is not a task of the plan.  A store without mission tables owns no mission
    work.
    """
    if not task_ids:
        return False
    mission_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_mission_tasks'"
    ).fetchone()
    if mission_tables is None:
        return False
    for task_id in task_ids:
        owned = conn.execute(
            "SELECT 1 FROM runtime_mission_tasks WHERE task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        if owned is not None:
            return True
    return False


class DurableTaskBudgetManager:
    """SQLite-backed task and recovery budget enforcement over ``runtime_tasks.budget_json``.

    ``isolation_acceptance`` is the recorded acceptance that isolation proof is judged
    against (AP-007-F2).  It is fixed when the server composes the manager and is not
    supplied per dispatch.  The default is none, which refuses every dispatch that
    needs isolation.
    """

    def __init__(
        self,
        storage: runtime_persistence.StorageProvider,
        db_path: Path,
        *,
        retry_policy: runtime_persistence.BusyRetryPolicy = runtime_persistence.DEFAULT_BUSY_RETRY_POLICY,
        isolation_acceptance: IsolationAcceptance | None = None,
    ) -> None:
        if isolation_acceptance is not None and not isinstance(isolation_acceptance, IsolationAcceptance):
            raise BudgetError("isolation_acceptance must be an IsolationAcceptance or None.")
        self._storage = storage
        self._db_path = db_path
        self._retry_policy = retry_policy
        self._isolation_acceptance = isolation_acceptance

    @property
    def isolation_acceptance(self) -> IsolationAcceptance | None:
        """The recorded acceptance this manager judges isolation proof against, if any."""
        return self._isolation_acceptance

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id.strip() or "\x00" in task_id:
            raise BudgetError("task_id must be a non-empty string without NUL characters.")
        return task_id.strip()

    @staticmethod
    def _load_task_chain(
        conn: sqlite3.Connection,
        tid: str,
        expected_session: str,
    ) -> tuple[dict[str, sqlite3.Row], str]:
        """Walk the durable parent chain and return ``(rows, authoritative_root_id)``."""
        rows: dict[str, sqlite3.Row] = {}
        cursor_id = tid
        seen: set[str] = set()
        while True:
            if cursor_id in seen:
                raise BudgetError(f"Runtime task parent cycle detected at {cursor_id!r}.")
            seen.add(cursor_id)
            row = conn.execute(
                """
                SELECT task_id, session_id, parent_task_id, current_epoch, budget_json
                FROM runtime_tasks
                WHERE task_id = ?
                """,
                (cursor_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {cursor_id!r} does not exist.")
            rows[cursor_id] = row
            row_session = str(row["session_id"] or "")
            # Older task rows predate mandatory session binding and contain
            # an empty value.  They are bound by the typed request envelope;
            # any non-empty durable identity remains authoritative.
            if row_session and row_session != expected_session:
                raise BudgetError(
                    f"Task {cursor_id!r} belongs to session {row_session!r}, not {expected_session!r}."
                )
            parent_id = str(row["parent_task_id"] or "").strip()
            if not parent_id:
                return rows, cursor_id
            cursor_id = parent_id

    @staticmethod
    def _parse_task_chain(rows: dict[str, sqlite3.Row]) -> dict[str, TaskBudgetState]:
        """Parse every budget from the leaf up to the root.

        Cancellation and the isolation requirement bind the whole chain, not only its
        two ends: a child of a cancelled or isolation-bound task must not run because
        its own document and the root's happen to be silent.  One unreadable document
        fails the whole chain closed.
        """
        return {task_id: TaskBudgetState.from_json(str(row["budget_json"])) for task_id, row in rows.items()}

    def get_budget(
        self,
        task_id: str,
        *,
        default_spec: TaskBudgetSpec | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)

        def _read(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            return TaskBudgetState.from_json(str(row["budget_json"]), default_spec=default_spec)

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def ensure_budget(
        self,
        task_id: str,
        *,
        spec: TaskBudgetSpec | None = None,
        override_spec: bool = False,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            raw = str(row["budget_json"] or "{}").strip()
            current = TaskBudgetState.from_json(raw, default_spec=spec)
            effective_spec = spec if (override_spec and spec is not None) else current.spec
            updated = replace(
                current,
                spec=effective_spec,
            )
            encoded = updated.to_json()
            if encoded != raw:
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (encoded, ts, tid),
                )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def set_control_flags(
        self,
        task_id: str,
        *,
        pause_requested: bool | None = None,
        cancel_requested: bool | None = None,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            updated = replace(
                current,
                pause_requested=current.pause_requested if pause_requested is None else bool(pause_requested),
                cancel_requested=current.cancel_requested if cancel_requested is None else bool(cancel_requested),
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (updated.to_json(), ts, tid),
            )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def record_usage(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        steps_delta: int = 0,
        tool_calls_delta: int = 0,
        tokens_delta: int = 0,
        elapsed_delta_s: float = 0.0,
        enforce: bool = True,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        _non_neg_int(steps_delta, "steps_delta")
        _non_neg_int(tool_calls_delta, "tool_calls_delta")
        _non_neg_int(tokens_delta, "tokens_delta")
        _non_neg_float(elapsed_delta_s, "elapsed_delta_s")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> tuple[TaskBudgetState, str, bool]:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT current_epoch, budget_json FROM runtime_tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            if epoch is not None and int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot mutate budget for task {tid!r} at current_epoch={row['current_epoch']}."
                )
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            candidate = replace(
                current,
                steps_used=current.steps_used + steps_delta,
                tool_calls_used=current.tool_calls_used + tool_calls_delta,
                tokens_used=current.tokens_used + tokens_delta,
                elapsed_s=round(current.elapsed_s + elapsed_delta_s, 6),
            )
            violation = candidate.violation_reason(check_recovery=False, now=ts)
            final_state = (
                replace(
                    candidate,
                    exhausted_reason=violation or candidate.exhausted_reason,
                )
                if violation
                else candidate
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (final_state.to_json(), ts, tid),
            )
            return final_state, violation, final_state.cancel_requested

        state, violation, cancelled = runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
        if enforce and cancelled:
            raise ExecutionCancelled("task cancellation requested.")
        if enforce and violation:
            raise BudgetExceeded(violation)
        return state

    def charge_hierarchical_tool_call(
        self,
        task_id: str,
        *,
        root_task_id: str,
        session_id: str,
        epoch: int | None = None,
        now: float | None = None,
        isolation: IsolationEvidence | None = None,
        consequential: bool = True,
        isolation_step: str = CONTAINED_EXECUTION,
    ) -> tuple[TaskBudgetState, TaskBudgetState]:
        """Atomically validate and charge one action to its leaf and root budgets.

        The durable parent chain, rather than caller input, identifies the root.
        A root action is charged exactly once.  A child action updates leaf and
        root in the same SQLite transaction, so a crash or concurrent admission
        can never debit only one side.  Any stale epoch, identity mismatch,
        cancellation, malformed budget, or exhausted candidate rolls the whole
        transaction back and fails closed.

        AP-007: when any task from the leaf up to the root declares
        ``isolation_required``, the charge is refused with :class:`IsolationUnavailable`
        unless ``isolation`` proves a qualified sandbox.  The default is refusal, so a
        caller that presents nothing can never reach a host-execution path for such a
        task.  Cancellation is checked on the same whole chain.

        AP-007-F2: for mission-owned work the proof is mandatory, whether or not a
        budget declares the requirement.  A charge needs proof when the chain contains
        a task of a mission plan and the action is ``consequential``, or belongs to an
        ``isolation_step`` other than contained execution.  Both arguments default to
        the refusing side: a caller that does not say what it dispatches is treated as
        dispatching a consequential action.  Proof is judged against the manager's
        recorded acceptance (see :func:`isolation_refusal`).  A charge for work that no
        mission owns is not affected.
        """

        tid = self._validate_task_id(task_id)
        expected_root = self._validate_task_id(root_task_id)
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise BudgetError("session_id must be a non-empty string without NUL characters.")
        expected_session = session_id.strip()
        if epoch is not None:
            _non_neg_int(epoch, "epoch", min_value=1)
        if not isinstance(consequential, bool):
            raise BudgetError("consequential must be a boolean.")
        if not isinstance(isolation_step, str) or isolation_step not in ISOLATION_STEPS:
            raise BudgetError(f"isolation_step must be one of {ISOLATION_STEPS}.")
        ts = time.time() if now is None else float(now)
        acceptance = self._isolation_acceptance

        def _tx(conn: sqlite3.Connection) -> tuple[TaskBudgetState, TaskBudgetState]:
            conn.row_factory = sqlite3.Row
            rows, authoritative_root = self._load_task_chain(conn, tid, expected_session)

            if authoritative_root != expected_root:
                raise BudgetError(
                    f"Stale or mismatched root task {expected_root!r}; authoritative root is {authoritative_root!r}."
                )
            leaf_row = rows[tid]
            if epoch is not None and int(leaf_row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot charge task {tid!r} at current_epoch={leaf_row['current_epoch']}."
                )

            chain = self._parse_task_chain(rows)
            leaf_current = chain[tid]
            root_current = chain[authoritative_root]
            if any(state.cancel_requested for state in chain.values()):
                raise ExecutionCancelled("task or root-task cancellation requested.")
            needs_proof = any(state.spec.isolation_required for state in chain.values())
            if not needs_proof and (consequential or isolation_step != CONTAINED_EXECUTION):
                needs_proof = _mission_owns_chain(conn, tuple(rows))
            if needs_proof:
                refusal = isolation_refusal(isolation, acceptance=acceptance, step=isolation_step)
                if refusal:
                    raise IsolationUnavailable(
                        f"{refusal}; dependent execution is blocked and the host shell is not used."
                    )

            leaf_candidate = replace(
                leaf_current,
                tool_calls_used=leaf_current.tool_calls_used + 1,
            )
            leaf_violation = leaf_candidate.violation_reason(check_recovery=False, now=ts)
            if leaf_violation:
                raise BudgetExceeded(f"leaf task {tid!r}: {leaf_violation}")

            if authoritative_root == tid:
                root_candidate = leaf_candidate
            else:
                root_candidate = replace(
                    root_current,
                    tool_calls_used=root_current.tool_calls_used + 1,
                )
                root_violation = root_candidate.violation_reason(check_recovery=False, now=ts)
                if root_violation:
                    raise BudgetExceeded(
                        f"root task {authoritative_root!r}: {root_violation}"
                    )

            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (leaf_candidate.to_json(), ts, tid),
            )
            if authoritative_root != tid:
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (root_candidate.to_json(), ts, authoritative_root),
                )
            return leaf_candidate, root_candidate

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def record_bounded_observation(
        self,
        task_id: str,
        *,
        root_task_id: str,
        session_id: str,
        epoch: int | None = None,
        progressed: bool | None = None,
        repair_attempt: bool = False,
        memory_bytes: int | None = None,
        storage_bytes: int | None = None,
        unmeasured: Sequence[str] = (),
        enforce: bool = True,
        now: float | None = None,
    ) -> BoundedStopVerdict:
        """Atomically apply one progress/repair/resource observation to leaf and root.

        ``progressed`` resets (``True``) or extends (``False``) the consecutive
        no-progress count; ``None`` carries no progress signal.  Memory and storage
        readings are absolute byte counts, not deltas, and raise the recorded peak.
        ``unmeasured`` names a resource whose declared ceiling could not be read: the
        work stops instead of assuming headroom.  The root aggregates every leaf, so
        repeated repair cannot be hidden by spreading it over nodes or plan versions.

        These are server-observed coordination facts.  They are never taken from a
        request body or from model tool arguments.

        Fail-closed properties:

        * a stale, fenced or missing worker epoch cannot record anything, so a late
          worker can neither reset the no-progress count nor hide a ceiling breach;
        * once a stop is required the document is not mutated again, so the stop
          cannot be undone by a later "progress" report.  The one exception is a
          ``measurement_unavailable`` stop: an observation that carries a real
          reading for every unmeasured resource replaces the marker, and that
          reading is then judged against the ceiling like any other;
        * the stop reason is committed before :class:`BoundedStopRequired` is raised,
          so a crash between the two leaves the stop durable.
        """

        tid = self._validate_task_id(task_id)
        expected_root = self._validate_task_id(root_task_id)
        if not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id:
            raise BudgetError("session_id must be a non-empty string without NUL characters.")
        expected_session = session_id.strip()
        if epoch is not None:
            _non_neg_int(epoch, "epoch", min_value=1)
        if progressed is not None and not isinstance(progressed, bool):
            raise BudgetError("progressed must be True, False or None.")
        if not isinstance(repair_attempt, bool):
            raise BudgetError("repair_attempt must be a boolean.")
        readings: dict[str, int] = {}
        for name, value in (("memory", memory_bytes), ("storage", storage_bytes)):
            if value is not None:
                readings[name] = _non_neg_int(value, f"{name}_bytes")
        missing = _measured_markers(tuple(unmeasured))
        overlap = sorted(set(missing) & set(readings))
        if overlap:
            raise BudgetError(f"{overlap[0]} cannot be both measured and unmeasured in one observation.")
        ts = time.time() if now is None else float(now)

        def _apply(state: TaskBudgetState) -> TaskBudgetState:
            count = state.no_progress_count
            if progressed is True:
                count = 0
            elif progressed is False:
                count += 1
            declared = {
                "memory": state.spec.max_memory_bytes is not None,
                "storage": state.spec.max_storage_bytes is not None,
            }
            markers = set(state.unmeasured) - set(readings)
            markers.update(name for name in missing if declared[name])
            return replace(
                state,
                no_progress_count=count,
                repair_attempts_used=state.repair_attempts_used + (1 if repair_attempt else 0),
                memory_bytes_peak=max(state.memory_bytes_peak, readings.get("memory", 0)),
                storage_bytes_peak=max(state.storage_bytes_peak, readings.get("storage", 0)),
                unmeasured=tuple(sorted(markers)),
            )

        def _tx(conn: sqlite3.Connection) -> BoundedStopVerdict:
            conn.row_factory = sqlite3.Row
            rows, authoritative_root = self._load_task_chain(conn, tid, expected_session)
            if authoritative_root != expected_root:
                raise BudgetError(
                    f"Stale or mismatched root task {expected_root!r}; authoritative root is {authoritative_root!r}."
                )
            leaf_row = rows[tid]
            current_epoch = int(leaf_row["current_epoch"])
            if current_epoch > 0:
                if epoch is None or int(epoch) != current_epoch:
                    raise runtime_persistence.StaleEpochError(
                        f"Stale or missing epoch {epoch} cannot record an observation for task {tid!r} "
                        f"at current_epoch={current_epoch}."
                    )
                lease = conn.execute(
                    "SELECT status FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, current_epoch),
                ).fetchone()
                if lease is None or str(lease["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Task {tid!r} epoch {current_epoch} is "
                        f"{str(lease['status']) if lease is not None else 'missing'}, not active; "
                        "a fenced worker cannot record an observation."
                    )
            elif epoch is not None:
                raise runtime_persistence.StaleEpochError(
                    f"Task {tid!r} has no worker epoch; epoch {epoch} cannot record an observation."
                )

            chain = self._parse_task_chain(rows)
            leaf_current = chain[tid]
            same = authoritative_root == tid
            root_current = chain[authoritative_root]
            if any(state.cancel_requested for state in chain.values()):
                raise ExecutionCancelled("task or root-task cancellation requested.")

            # A stop that is already required is final: never mutate past it.  Only a
            # measurement gap can be closed, and only by readings that cover it fully.
            for scope, owner, state in (("leaf", tid, leaf_current), ("root", authoritative_root, root_current)):
                prior = state.bounded_stop(now=ts)
                if prior is None:
                    continue
                if prior[0] == "measurement_unavailable" and set(state.measurement_gaps) <= set(readings):
                    continue
                return BoundedStopVerdict(True, prior[0], prior[1], scope, owner, leaf_current, root_current)

            leaf_next = _apply(leaf_current)
            leaf_stop = leaf_next.bounded_stop(now=ts)
            if leaf_stop is not None:
                leaf_next = replace(leaf_next, exhausted_reason=leaf_stop[1])
            if same:
                root_next, root_stop = leaf_next, None
            else:
                root_next = _apply(root_current)
                root_stop = root_next.bounded_stop(now=ts)
                if root_stop is not None:
                    root_next = replace(root_next, exhausted_reason=root_stop[1])
            if leaf_stop is not None:
                verdict = BoundedStopVerdict(True, leaf_stop[0], leaf_stop[1], "leaf", tid, leaf_next, root_next)
            elif root_stop is not None:
                verdict = BoundedStopVerdict(
                    True, root_stop[0], root_stop[1], "root", authoritative_root, leaf_next, root_next
                )
            else:
                verdict = BoundedStopVerdict(False, "", "", "", "", leaf_next, root_next)

            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (leaf_next.to_json(), ts, tid),
            )
            if not same:
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (root_next.to_json(), ts, authoritative_root),
                )
            return verdict

        verdict = runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
        if enforce and verdict.stopped:
            raise BoundedStopRequired(verdict)
        return verdict

    def consume_recovery(
        self,
        task_id: str,
        *,
        now: float | None = None,
    ) -> TaskBudgetState:
        """Atomically consume 1 durable recovery attempt; commit exhaustion before raising."""
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> tuple[TaskBudgetState, str]:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            next_recoveries = current.recoveries_used + 1
            violation = ""
            if next_recoveries > current.spec.max_recoveries:
                violation = f"recovery budget exceeded ({next_recoveries}/{current.spec.max_recoveries})"
            persisted = replace(
                current,
                recoveries_used=current.recoveries_used if violation else next_recoveries,
                pause_requested=False if not violation else current.pause_requested,
                exhausted_reason=violation or current.exhausted_reason,
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (persisted.to_json(), ts, tid),
            )
            return persisted, violation

        state, violation = runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
        if violation:
            raise BudgetExceeded(violation)
        return state

    def set_verification_contract(
        self,
        task_id: str,
        contract: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> TaskBudgetState:
        """Persist or clear the authoritative task verification contract inside ``budget_json``."""
        tid = self._validate_task_id(task_id)
        if contract is not None and not isinstance(contract, dict):
            raise BudgetError("verification_contract must be a dict or None.")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            updated = replace(
                current,
                verification_contract=dict(contract) if contract is not None else None,
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (updated.to_json(), ts, tid),
            )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
