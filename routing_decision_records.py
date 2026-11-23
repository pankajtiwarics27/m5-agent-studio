"""Policy-aware routing decisions and resource-safe scheduling (AP-014).

The story's objective is narrow and the module is built to match it: *select
eligible models per task stage without bypassing existing lifecycle
qualification.* This module therefore composes gates; it does not dispatch.

Four properties are load-bearing and each is structural rather than advisory:

* **Every gate is evaluated, in order, before dispatch is considered (AC1).**
  Compatibility, measured quality, grants and actual resource fit are checked
  one after another and *all* results are recorded — a candidate rejected for an
  expired grant is still reported as fitting the host, because an operator
  reading the record needs both facts. A rejection short-circuits the decision
  but never hides the gates it never reached.

* **Local inference stays local (AC3).** A hosted candidate is refused at
  construction, not quietly excluded at dispatch time. When no local candidate
  qualifies, the decision is a *scoped limitation*: here is what is unavailable,
  here is the local shortfall, and here is what would have to be true to proceed.
  There is no fallback path to a hosted model to be found, because no type in
  this module can represent one.

* **Accounting is not completion (AC4).** ``AccountingState`` distinguishes what
  was *observed* from what was *charged*. A load or unload that the runtime
  reports as finished is an observation, not proof that the slot is idle, so
  uncertainty stays charged until something attested it. :func:`accounting_gap`
  reports the difference in bytes and the module refuses to present a
  "not observed to be resident" as "not resident".

* **Simultaneous residency is never introduced (AC2).** The warm/cold scheduler
  plans a **strictly sequential** sequence and refuses to plan a second model
  while one is resident — including when the plan would be "faster". Dormant
  native activation stays dormant: the plan names no activation step.

Existing authority is reused rather than restated: grants are checked through
``scoped_grants`` scope subsumption, resource capacity through
``resource_admission`` semantics, and measured capability through the AP-013
profile contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from model_capability_profiles import CapabilityProfile, TaskKind
from scoped_grants import scope_subsumes

SCHEMA_VERSION = 1

GateName = Literal[
    "compatibility", "measured_quality", "grants", "residency", "resource_fit"
]
#: Evaluated in this order, every time, and each result is recorded.
GATE_ORDER: tuple[GateName, ...] = (
    "compatibility",
    "measured_quality",
    "grants",
    "residency",
    "resource_fit",
)
GateStatus = Literal["passed", "failed", "not_reached", "unknown"]
StageKind = Literal["draft", "revise", "review", "extract"]
STAGE_KINDS: tuple[StageKind, ...] = ("draft", "revise", "review", "extract")
PlanAction = Literal["load", "keep_resident", "unload", "switch"]
AccountingState = Literal["observed", "charged_only", "settled", "unknown"]
#: Actions that would leave two models resident at once.
_RESIDENCY_ACTIONS = frozenset({"load", "switch"})


class RoutingDecisionError(ValueError):
    """Raised when a routing input or recorded decision is invalid."""


class HostedRoutingRefused(RoutingDecisionError):
    """Raised when a candidate or fallback names a hosted model (AC3)."""


class SimultaneousResidencyRefused(RoutingDecisionError):
    """Raised when a plan would hold two models resident (AC2)."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(
    value: object, field_name: str, *, optional: bool = False, max_length: int = 300
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise RoutingDecisionError(f"{field_name} must not be empty.")
    if "\x00" in result:
        raise RoutingDecisionError(f"{field_name} must not contain a NUL character.")
    if len(result) > max_length:
        raise RoutingDecisionError(
            f"{field_name} must be at most {max_length} characters."
        )
    return result or None


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutingDecisionError(f"{field_name} must be a non-negative integer.")
    return value


# ---------------------------------------------------------------------------
# AC3 — locality is a property of the candidate, not a policy flag
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalCandidate:
    """A local model offered for a stage. Hosted candidates cannot exist here.

    ``provider`` is recorded for audit but must be local: a hosted provider is
    refused at construction so that no later gate, fallback or "quality rescue"
    can reach one. That is deliberately stronger than an eligibility check.
    """

    model_id: str
    provider: str
    profile: CapabilityProfile
    resident_bytes: int
    grants: tuple[str, ...] = ()
    is_hosted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "provider", _text(self.provider, "provider"))
        if self.is_hosted:
            raise HostedRoutingRefused(
                f"candidate {self.model_id!r} names a hosted model; all model inference in this "
                "program stays local, and a hosted fallback is refused even when local fit or "
                "quality is insufficient (AC3). Report a scoped limitation instead."
            )
        if not isinstance(self.profile, CapabilityProfile):
            raise TypeError("profile must be a CapabilityProfile value.")
        object.__setattr__(
            self,
            "resident_bytes",
            _non_negative_int(self.resident_bytes, "resident_bytes"),
        )
        object.__setattr__(self, "grants", tuple(sorted(set(self.grants))))

    @property
    def locality(self) -> str:
        return "local"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "locality": self.locality,
            "configuration_digest": self.profile.identity.digest,
            "resident_bytes": self.resident_bytes,
            "grants": list(self.grants),
            "is_hosted": False,
        }


#: The full text of the refusal, so callers and tests never need to paraphrase it.
HOSTED_FALLBACK_REFUSAL = (
    "there is no hosted fallback in this program. When no local model meets fit or quality, the "
    "routing decision reports a scoped limitation naming the local shortfall; it never substitutes "
    "a hosted model (AC3)."
)

#: Always false. There is no branch anywhere in this module that can return true.
HOSTED_FALLBACK_AVAILABLE = False


def refuse_hosted_fallback() -> HostedRoutingRefused:
    """The one answer to "what if no local model qualifies?" (AC3).

    Raises rather than returning, so a caller cannot branch on an answer and
    then proceed with a hosted model anyway.
    """
    raise HostedRoutingRefused(HOSTED_FALLBACK_REFUSAL)


# ---------------------------------------------------------------------------
# AC1 — the five gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict, with the reason an operator needs to read."""

    gate: GateName
    status: GateStatus
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.gate not in GATE_ORDER:
            raise RoutingDecisionError(f"unknown gate: {self.gate!r}.")
        if self.status not in ("passed", "failed", "not_reached", "unknown"):
            raise RoutingDecisionError(f"unknown gate status: {self.status!r}.")
        if self.status in ("failed", "unknown") and not self.reason:
            raise RoutingDecisionError(f"a {self.status} gate must state a reason.")
        object.__setattr__(self, "detail", dict(self.detail))

    @property
    def is_blocking(self) -> bool:
        """``unknown`` blocks. Not knowing a gate passed is not passing it."""
        return self.status in ("failed", "unknown")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "blocking": self.is_blocking,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def compatibility_gate(
    candidate: LocalCandidate, *, required_features: Sequence[str]
) -> GateResult:
    """Runtime support for every required feature, for this exact configuration."""
    unsupported = [
        f for f in required_features if candidate.profile.supports(f) == "unsupported"
    ]
    if unsupported:
        return GateResult(
            "compatibility",
            "failed",
            f"this runtime does not support {', '.join(sorted(unsupported))} for this configuration.",
            {"unsupported": sorted(unsupported)},
        )
    unknown = [
        f for f in required_features if candidate.profile.supports(f) == "unknown"
    ]
    if unknown:
        return GateResult(
            "compatibility",
            "unknown",
            f"runtime support for {', '.join(sorted(unknown))} was never verified; untested is not "
            "supported.",
            {"unverified": sorted(unknown)},
        )
    return GateResult(
        "compatibility", "passed", "runtime supports every required feature."
    )


def quality_gate(
    candidate: LocalCandidate, *, stage: TaskKind, minimum: float, now: str
) -> GateResult:
    """Measured quality for this stage, refused when it is not a local measurement."""
    state, item, reason = candidate.profile.capability(stage)
    if state != "measured" or item is None:
        return GateResult(
            "measured_quality",
            "unknown",
            f"{reason} A claim, a family reputation or a parameter count is not measured quality.",
            {"required_minimum": minimum},
        )
    if item.value < minimum:
        return GateResult(
            "measured_quality",
            "failed",
            f"measured {stage} quality {item.value} is below the required {minimum}.",
            {
                "value": item.value,
                "required_minimum": minimum,
                "confident": item.confident,
            },
        )
    if not item.confident:
        return GateResult(
            "measured_quality",
            "unknown",
            f"measured {stage} quality {item.value} meets the minimum but rests on "
            f"{item.sample_count} probe(s) with spread {item.spread}; treat as indicative, not qualified.",
            {"value": item.value, "required_minimum": minimum, "confident": False},
        )
    return GateResult(
        "measured_quality",
        "passed",
        f"measured {stage} quality {item.value} meets the required {minimum}.",
        {"value": item.value, "required_minimum": minimum, "confident": True},
    )


def grant_gate(
    candidate: LocalCandidate, *, required_grants: Sequence[str]
) -> GateResult:
    """The candidate's grants must subsume every grant its stage requires (AC1)."""
    missing = [
        g
        for g in required_grants
        if not any(scope_subsumes(grant, g) for grant in candidate.grants)
    ]
    if missing:
        return GateResult(
            "grants",
            "failed",
            f"the candidate holds no grant covering {', '.join(sorted(missing))}.",
            {"missing_grants": sorted(missing)},
        )
    return GateResult("grants", "passed", "every required grant is held.")


def residency_gate(
    candidate: LocalCandidate, *, resident_model_id: str | None
) -> GateResult:
    """Sequential residency only: another model resident is a refusal (AC2)."""
    if resident_model_id is not None and resident_model_id != candidate.model_id:
        return GateResult(
            "residency",
            "failed",
            f"{resident_model_id!r} is resident; this program runs models sequentially and will not "
            f"hold two resident models at once (AC2).",
            {"resident_model_id": resident_model_id},
        )
    return GateResult(
        "residency",
        "passed",
        "no other model is resident."
        if resident_model_id is None
        else "this model is already resident.",
        {"resident_model_id": resident_model_id},
    )


def resource_fit_gate(candidate: LocalCandidate, *, available_bytes: int) -> GateResult:
    """Actual fit against what is free right now, not against total capacity."""
    if available_bytes < 0:
        raise RoutingDecisionError("available_bytes must not be negative.")
    if candidate.resident_bytes > available_bytes:
        return GateResult(
            "resource_fit",
            "failed",
            f"the configuration needs {candidate.resident_bytes} bytes but only {available_bytes} are free.",
            {
                "required_bytes": candidate.resident_bytes,
                "available_bytes": available_bytes,
            },
        )
    return GateResult(
        "resource_fit",
        "passed",
        "the configuration fits the currently free memory.",
        {
            "required_bytes": candidate.resident_bytes,
            "available_bytes": available_bytes,
        },
    )


# ---------------------------------------------------------------------------
# The decision record (AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageRequirement:
    """What one task stage needs, stated before any candidate is considered."""

    stage: StageKind
    task_kind: TaskKind
    minimum_quality: float
    required_features: tuple[str, ...] = ()
    required_grants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.stage not in STAGE_KINDS:
            raise RoutingDecisionError(f"unknown stage: {self.stage!r}.")
        if isinstance(self.minimum_quality, bool) or not isinstance(
            self.minimum_quality, (int, float)
        ):
            raise TypeError("minimum_quality must be a number.")
        if not 0.0 <= float(self.minimum_quality) <= 1.0:
            raise RoutingDecisionError("minimum_quality must be within [0, 1].")
        object.__setattr__(
            self, "minimum_quality", round(float(self.minimum_quality), 6)
        )
        object.__setattr__(
            self, "required_features", tuple(sorted(set(self.required_features)))
        )
        object.__setattr__(
            self, "required_grants", tuple(sorted(set(self.required_grants)))
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "task_kind": self.task_kind,
            "minimum_quality": self.minimum_quality,
            "required_features": list(self.required_features),
            "required_grants": list(self.required_grants),
        }


@dataclass(frozen=True, slots=True)
class ScopedLimitation:
    """What this program cannot do locally, stated instead of falling back (AC3).

    A limitation is a first-class outcome, not an error message: it names the
    stage, the local shortfall, and what would have to change. It never contains
    a suggested hosted substitute.
    """

    stage: StageKind
    shortfall: str
    remedy: str

    def __post_init__(self) -> None:
        if self.stage not in STAGE_KINDS:
            raise RoutingDecisionError(f"unknown stage: {self.stage!r}.")
        object.__setattr__(self, "shortfall", _text(self.shortfall, "shortfall"))
        object.__setattr__(self, "remedy", _text(self.remedy, "remedy"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "shortfall": self.shortfall,
            "remedy": self.remedy,
            "hosted_fallback_offered": False,
        }


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """Every gate for one candidate, including the gates it never reached."""

    model_id: str
    gates: tuple[GateResult, ...]
    selected: bool
    summary: str

    def __post_init__(self) -> None:
        if tuple(result.gate for result in self.gates) != GATE_ORDER:
            raise RoutingDecisionError(
                "a verdict must record every gate in gate order, including those not reached."
            )
        object.__setattr__(self, "summary", _text(self.summary, "summary"))

    @property
    def blocked_by(self) -> tuple[GateName, ...]:
        return tuple(result.gate for result in self.gates if result.is_blocking)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "selected": self.selected,
            "summary": self.summary,
            "blocked_by": list(self.blocked_by),
            "gates": [result.to_mapping() for result in self.gates],
        }


@dataclass(frozen=True, slots=True)
class RoutingDecisionRecord:
    """Why this model was selected and every other candidate was not (AC5).

    ``rejections`` is exhaustive: a candidate is never dropped without a stated
    blocking gate, so "no model was chosen" is always explainable.
    """

    decision_id: str
    stage: StageRequirement
    selected_model_id: str | None
    verdicts: tuple[CandidateVerdict, ...]
    limitation: ScopedLimitation | None
    manual_override_applied: bool = False
    decided_at: str = ""
    schedule: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _text(self.decision_id, "decision_id"))
        object.__setattr__(
            self,
            "decided_at",
            _text(self.decided_at, "decided_at", optional=True) or "",
        )
        if not isinstance(self.stage, StageRequirement):
            raise TypeError("stage must be a StageRequirement value.")
        object.__setattr__(self, "verdicts", tuple(self.verdicts))
        if not self.verdicts:
            raise RoutingDecisionError(
                "a decision must record at least one candidate verdict."
            )
        selected = [v for v in self.verdicts if v.selected]
        if len(selected) > 1:
            raise RoutingDecisionError("a decision selects at most one candidate.")
        if self.selected_model_id is not None and not selected:
            raise RoutingDecisionError("a selected model needs a selected verdict.")
        if self.selected_model_id is None and selected:
            raise RoutingDecisionError(
                "a selected verdict contradicts an empty selection."
            )
        if self.selected_model_id is None and self.limitation is None:
            raise RoutingDecisionError(
                "a decision that selects nothing must record a scoped limitation (AC3), not silence."
            )
        if self.selected_model_id is not None and self.limitation is not None:
            raise RoutingDecisionError(
                "a decision that selected a model has no limitation to report."
            )
        object.__setattr__(
            self, "schedule", tuple(dict(item) for item in self.schedule)
        )

    @property
    def rejections(self) -> dict[str, tuple[GateName, ...]]:
        return {v.model_id: v.blocked_by for v in self.verdicts if not v.selected}

    @property
    def is_limited(self) -> bool:
        return self.selected_model_id is None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "stage": self.stage.to_mapping(),
            "selected_model_id": self.selected_model_id,
            "is_limited": self.is_limited,
            "manual_override_applied": self.manual_override_applied,
            "verdicts": [verdict.to_mapping() for verdict in self.verdicts],
            "rejections": {
                model: list(gates) for model, gates in sorted(self.rejections.items())
            },
            "limitation": self.limitation.to_mapping() if self.limitation else None,
            "schedule": [dict(item) for item in self.schedule],
            "gate_order": list(GATE_ORDER),
            "hosted_fallback_available": False,
            "decided_at": self.decided_at,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def _evaluate_candidate(
    candidate: LocalCandidate,
    stage: StageRequirement,
    *,
    resident_model_id: str | None,
    available_bytes: int,
    now: str,
) -> tuple[GateResult, ...]:
    """Run the gates in order, recording ``not_reached`` for the rest.

    The first blocking gate stops evaluation of *further* gates — but every gate
    is still present in the result, marked ``not_reached``, so a reader can see
    what was never asked rather than mistaking silence for a pass.
    """
    results: list[GateResult] = []
    blocked = False
    for gate in GATE_ORDER:
        if blocked:
            results.append(
                GateResult(
                    gate, "not_reached", "an earlier gate blocked this candidate."
                )
            )
            continue
        if gate == "compatibility":
            result = compatibility_gate(
                candidate, required_features=stage.required_features
            )
        elif gate == "measured_quality":
            result = quality_gate(
                candidate, stage=stage.task_kind, minimum=stage.minimum_quality, now=now
            )
        elif gate == "grants":
            result = grant_gate(candidate, required_grants=stage.required_grants)
        elif gate == "residency":
            result = residency_gate(candidate, resident_model_id=resident_model_id)
        else:
            result = resource_fit_gate(candidate, available_bytes=available_bytes)
        results.append(result)
        blocked = result.is_blocking
    return tuple(results)


def _summary(results: Sequence[GateResult]) -> str:
    blocking = [r for r in results if r.is_blocking]
    if not blocking:
        return "every gate passed."
    first = blocking[0]
    return f"{first.gate}: {first.reason}"


def decide_route(
    *,
    decision_id: str,
    stage: StageRequirement,
    candidates: Sequence[LocalCandidate],
    resident_model_id: str | None,
    available_bytes: int,
    now: str,
    prefer: str | None = None,
) -> RoutingDecisionRecord:
    """Select at most one local model for ``stage``, or report a limitation.

    ``prefer`` is not an override: it only breaks ties between candidates that
    already passed every gate. An override that could select a candidate which
    failed a gate would bypass lifecycle qualification, which is the thing this
    story forbids.
    """
    if not candidates:
        raise RoutingDecisionError("at least one local candidate is required.")
    if prefer is not None and not any(c.model_id == prefer for c in candidates):
        raise RoutingDecisionError(
            f"preferred model {prefer!r} is not among the candidates."
        )
    verdicts: list[CandidateVerdict] = []
    passing: list[tuple[LocalCandidate, float]] = []
    for candidate in candidates:
        results = _evaluate_candidate(
            candidate,
            stage,
            resident_model_id=resident_model_id,
            available_bytes=available_bytes,
            now=now,
        )
        ok = not any(result.is_blocking for result in results)
        verdicts.append(
            CandidateVerdict(
                model_id=candidate.model_id,
                gates=results,
                selected=False,
                summary=_summary(results),
            )
        )
        if ok:
            _state, item, _reason = candidate.profile.capability(stage.task_kind)
            passing.append((candidate, item.value if item else 0.0))

    if not passing:
        limitation = ScopedLimitation(
            stage=stage.stage,
            shortfall=(
                f"no local candidate passes every gate for the {stage.stage} stage "
                f"({stage.task_kind} at >= {stage.minimum_quality})"
            ),
            remedy=(
                "measure a local configuration's capability on this host, free the resident model, or "
                "relax the stated requirement. A hosted model is not an available remedy (AC3)."
            ),
        )
        return RoutingDecisionRecord(
            decision_id=decision_id,
            stage=stage,
            selected_model_id=None,
            verdicts=tuple(verdicts),
            limitation=limitation,
            decided_at=now,
        )

    def rank(entry: tuple[LocalCandidate, float]) -> tuple[float, int, str]:
        candidate, quality = entry
        preferred = 0 if (prefer is not None and candidate.model_id == prefer) else 1
        # Higher quality first; then the preferred candidate; then a stable id order.
        return (-quality, preferred, candidate.model_id)

    passing.sort(key=rank)
    winner, _quality = passing[0]
    final: list[CandidateVerdict] = []
    for verdict in verdicts:
        if verdict.model_id == winner.model_id:
            final.append(
                CandidateVerdict(
                    model_id=verdict.model_id,
                    gates=verdict.gates,
                    selected=True,
                    summary="every gate passed and this candidate ranked highest for the stage.",
                )
            )
        elif not verdict.blocked_by:
            final.append(
                CandidateVerdict(
                    model_id=verdict.model_id,
                    gates=verdict.gates,
                    selected=False,
                    summary="every gate passed but another candidate ranked higher for the stage.",
                )
            )
        else:
            final.append(verdict)
    return RoutingDecisionRecord(
        decision_id=decision_id,
        stage=stage,
        selected_model_id=winner.model_id,
        verdicts=tuple(final),
        limitation=None,
        decided_at=now,
    )


# ---------------------------------------------------------------------------
# AC2/AC4 — scheduling, residency and accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResidencyObservation:
    """What the runtime reported about a model, and when it reported it."""

    model_id: str
    state: Literal["resident", "released", "unknown"]
    observed_at: str
    reported_bytes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        if self.state not in ("resident", "released", "unknown"):
            raise RoutingDecisionError(
                f"unknown residency observation: {self.state!r}."
            )
        object.__setattr__(self, "observed_at", _text(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "reported_bytes",
            _non_negative_int(self.reported_bytes, "reported_bytes"),
        )

    @property
    def is_evidence_of_idleness(self) -> bool:
        """A ``released`` observation is evidence; ``unknown`` is not."""
        return self.state == "released"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "state": self.state,
            "observed_at": self.observed_at,
            "reported_bytes": self.reported_bytes,
            "is_evidence_of_idleness": self.is_evidence_of_idleness,
        }


@dataclass(frozen=True, slots=True)
class MemoryAccounting:
    """Charged vs observed memory, kept apart so uncertainty stays charged (AC4).

    The charged figure is what the program must assume is unavailable. The
    observed figure is what the runtime last reported. When they disagree, the
    *larger* is the safe assumption and :attr:`safe_free_bytes` uses it.
    """

    capacity_bytes: int
    charged_bytes: int
    observed_bytes: int | None = None
    observation: ResidencyObservation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capacity_bytes",
            _non_negative_int(self.capacity_bytes, "capacity_bytes"),
        )
        object.__setattr__(
            self,
            "charged_bytes",
            _non_negative_int(self.charged_bytes, "charged_bytes"),
        )
        if self.observed_bytes is not None:
            object.__setattr__(
                self,
                "observed_bytes",
                _non_negative_int(self.observed_bytes, "observed_bytes"),
            )

    @property
    def state(self) -> AccountingState:
        if self.observation is None:
            return "unknown"
        if self.observed_bytes is None:
            return "charged_only"
        if self.observation.state == "unknown":
            return "charged_only"
        return "settled" if self.observation.state == "released" else "observed"

    @property
    def effective_charged_bytes(self) -> int:
        """What must be assumed unavailable.

        Raised to the observed figure when the runtime reports more than was
        charged, because assuming less than was charged is never safe. Lowered
        to zero only when the runtime *released* the model and reported nothing
        resident -- that is the single case where the bytes are evidenced free.
        Any other state, including an unknown one, leaves the charge standing.
        """
        if self.observed_bytes is None:
            return self.charged_bytes
        released_and_clear = (
            self.observation is not None
            and self.observation.state == "released"
            and self.observed_bytes == 0
        )
        if released_and_clear:
            return 0
        return max(self.charged_bytes, self.observed_bytes)

    @property
    def safe_free_bytes(self) -> int:
        return max(0, self.capacity_bytes - self.effective_charged_bytes)

    @property
    def uncertainty_bytes(self) -> int:
        return max(0, self.effective_charged_bytes - self.charged_bytes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capacity_bytes": self.capacity_bytes,
            "charged_bytes": self.charged_bytes,
            "observed_bytes": self.observed_bytes,
            "effective_charged_bytes": self.effective_charged_bytes,
            "safe_free_bytes": self.safe_free_bytes,
            "uncertainty_bytes": self.uncertainty_bytes,
            "state": self.state,
            "observation": self.observation.to_mapping() if self.observation else None,
        }


@dataclass(frozen=True, slots=True)
class AccountingGap:
    """Charged bytes against the bytes the runtime actually evidenced (AC4).

    A positive difference is memory charged that nothing has shown to be
    occupied. It is *reported*, never silently released.
    """

    charged_bytes: int
    evidenced_bytes: int
    state: AccountingState
    explanation: str

    @property
    def is_settled(self) -> bool:
        """Settled means the runtime resolved it, not that the numbers are equal.

        A released model can still be carrying a charge that the observation has
        now resolved; that gap is settled and reported, not outstanding.
        """
        return self.state == "settled"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "charged_bytes": self.charged_bytes,
            "evidenced_bytes": self.evidenced_bytes,
            "difference_bytes": self.charged_bytes - self.evidenced_bytes,
            "state": self.state,
            "settled": self.is_settled,
            "explanation": self.explanation,
        }


def accounting_gap(accounting: MemoryAccounting) -> AccountingGap:
    """Report unload accounting honestly; a finished unload is not proof of idleness."""
    state = accounting.state
    if accounting.observation is None:
        explanation = (
            "no residency observation exists, so the charge stands. A load or unload that the "
            "runtime reports as finished is an observation, not evidence that the slot is idle (AC4)."
        )
    elif accounting.observation.state == "unknown":
        explanation = "the runtime could not report residency, so the charge stands: uncertainty remains charged."
    elif accounting.observation.state == "resident":
        explanation = (
            "the runtime still reports model bytes resident; the charge stands and no new model "
            "may be loaded until it is released (AC2)."
        )
    else:
        explanation = "the runtime reported the model released; the charge may be settled against that observation."
    observed_occupied = (
        accounting.observed_bytes
        if accounting.observation is not None and accounting.observed_bytes is not None
        else 0
    )
    return AccountingGap(
        charged_bytes=accounting.charged_bytes,
        evidenced_bytes=observed_occupied,
        state=state,
        explanation=explanation,
    )


@dataclass(frozen=True, slots=True)
class ScheduleStep:
    """One step of a strictly sequential execution plan (AC2)."""

    index: int
    stage: StageKind
    action: PlanAction
    model_id: str
    unloads: tuple[str, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.action not in ("load", "keep_resident", "unload", "switch"):
            raise RoutingDecisionError(f"unknown plan action: {self.action!r}.")
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "index", _non_negative_int(self.index, "index"))
        object.__setattr__(self, "unloads", tuple(sorted(set(self.unloads))))
        if self.action == "switch" and not self.unloads:
            raise SimultaneousResidencyRefused(
                "a switch must name the model it unloads first; swapping without an unload would "
                "hold two models resident (AC2)."
            )
        if self.action in ("load", "switch") and self.model_id in self.unloads:
            raise SimultaneousResidencyRefused(
                f"step {self.index} loads {self.model_id!r} and unloads it in the same step (AC2)."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "stage": self.stage,
            "action": self.action,
            "model_id": self.model_id,
            "unloads": list(self.unloads),
            "simultaneous_residency": False,
            "activates_native_lifecycle": False,
            "rationale": self.rationale,
        }


def validate_sequential_plan(steps: Sequence[ScheduleStep]) -> None:
    """Refuse any plan that could have two models resident at once (AC2)."""
    resident: set[str] = set()
    for step in steps:
        for model in step.unloads:
            resident.discard(model)
        if step.action in _RESIDENCY_ACTIONS:
            if resident:
                raise SimultaneousResidencyRefused(
                    f"step {step.index} would load {step.model_id!r} while {sorted(resident)} are "
                    "still resident (AC2)."
                )
            resident.add(step.model_id)
        elif step.action == "unload":
            resident.discard(step.model_id)


def plan_sequential_schedule(
    *,
    decisions: Sequence[RoutingDecisionRecord],
    initially_resident: str | None = None,
) -> tuple[ScheduleStep, ...]:
    """Plan one stage at a time, unloading before loading, never in parallel.

    The plan is deliberately the slow one. A faster plan that co-resides two
    models is not expressible: every load is preceded by the unload it needs.
    """
    steps: list[ScheduleStep] = []
    resident = initially_resident
    for index, decision in enumerate(decisions):
        if decision.selected_model_id is None:
            limitation = decision.limitation
            raise RoutingDecisionError(
                f"cannot schedule the {decision.stage.stage} stage: "
                f"{limitation.shortfall if limitation else 'no model was selected'}."
            )
        target = decision.selected_model_id
        if resident == target:
            steps.append(
                ScheduleStep(
                    index=index,
                    stage=decision.stage.stage,
                    action="keep_resident",
                    model_id=target,
                    rationale="the selected model is already resident for this stage.",
                )
            )
            continue
        if resident is None:
            steps.append(
                ScheduleStep(
                    index=index,
                    stage=decision.stage.stage,
                    action="load",
                    model_id=target,
                    rationale="no model is resident, so this stage loads its selected model.",
                )
            )
        else:
            steps.append(
                ScheduleStep(
                    index=index,
                    stage=decision.stage.stage,
                    action="switch",
                    model_id=target,
                    unloads=(resident,),
                    rationale=(
                        f"{resident!r} is unloaded before {target!r} loads, so the two are never "
                        "resident together (AC2)."
                    ),
                )
            )
        resident = target
    validate_sequential_plan(steps)
    return tuple(steps)


# ---------------------------------------------------------------------------
# AC6 — comparison against a baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineComparison:
    """Candidate plan vs existing baseline on quality, latency, switches and cost.

    Each metric is compared on its own terms rather than collapsed into one
    score, because a plan that wins on latency by adding switches and losing
    quality is a different trade, not a better number.
    """

    baseline_quality: float
    candidate_quality: float
    baseline_latency_ms: float
    candidate_latency_ms: float
    baseline_switches: int
    candidate_switches: int
    baseline_cost: float
    candidate_cost: float

    def __post_init__(self) -> None:
        for name in ("baseline_quality", "candidate_quality"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise RoutingDecisionError(f"{name} must be within [0, 1].")
            object.__setattr__(self, name, round(float(value), 6))
        for name in (
            "baseline_latency_ms",
            "candidate_latency_ms",
            "baseline_cost",
            "candidate_cost",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) < 0
            ):
                raise RoutingDecisionError(f"{name} must be a non-negative number.")
            object.__setattr__(self, name, round(float(value), 6))
        for name in ("baseline_switches", "candidate_switches"):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))

    @property
    def deltas(self) -> dict[str, float]:
        return {
            "quality": round(self.candidate_quality - self.baseline_quality, 6),
            "latency_ms": round(
                self.candidate_latency_ms - self.baseline_latency_ms, 6
            ),
            "switches": float(self.candidate_switches - self.baseline_switches),
            "cost": round(self.candidate_cost - self.baseline_cost, 6),
        }

    @property
    def is_pareto_improvement(self) -> bool:
        """Better quality, no worse latency, no more switches, no more cost."""
        d = self.deltas
        return (
            d["quality"] > 0
            and d["latency_ms"] <= 0
            and d["switches"] <= 0
            and d["cost"] <= 0
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "baseline": {
                "quality": self.baseline_quality,
                "latency_ms": self.baseline_latency_ms,
                "switches": self.baseline_switches,
                "cost": self.baseline_cost,
            },
            "candidate": {
                "quality": self.candidate_quality,
                "latency_ms": self.candidate_latency_ms,
                "switches": self.candidate_switches,
                "cost": self.candidate_cost,
            },
            "deltas": self.deltas,
            "pareto_improvement": self.is_pareto_improvement,
            "collapsed_into_single_score": False,
            "note": (
                "quality, latency, switch count and cost are compared separately; a faster plan that "
                "adds switches or loses quality is a trade, not an improvement (AC6)."
            ),
        }


# ---------------------------------------------------------------------------
# Manual override boundaries (AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManualOverrideRequest:
    """An operator's explicit model choice, bounded so it cannot bypass a gate."""

    model_id: str
    requested_by: str
    rationale: str
    accepts_gate_failure: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(
            self, "requested_by", _text(self.requested_by, "requested_by")
        )
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        if self.accepts_gate_failure:
            raise RoutingDecisionError(
                "a manual override cannot accept a failed gate; selecting an unqualified model would "
                "bypass lifecycle qualification (AC5)."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "requested_by": self.requested_by,
            "rationale": self.rationale,
            "accepts_gate_failure": False,
            "can_bypass_gates": False,
        }


def apply_manual_override(
    record: RoutingDecisionRecord, override: ManualOverrideRequest
) -> RoutingDecisionRecord:
    """Honour an override only within the gates the decision already passed."""
    if record.selected_model_id == override.model_id:
        return record
    target = next((v for v in record.verdicts if v.model_id == override.model_id), None)
    if target is None:
        raise RoutingDecisionError(
            f"{override.model_id!r} was not a candidate for this stage."
        )
    if target.blocked_by:
        raise RoutingDecisionError(
            f"cannot override to {override.model_id!r}: it is blocked by "
            f"{', '.join(target.blocked_by)}, and an override may not bypass lifecycle qualification (AC5)."
        )
    verdicts = tuple(
        CandidateVerdict(
            model_id=v.model_id,
            gates=v.gates,
            selected=v.model_id == override.model_id,
            summary=(
                f"selected by {override.requested_by}: {override.rationale}"
                if v.model_id == override.model_id
                else "not selected: the operator chose another candidate."
            ),
        )
        for v in record.verdicts
    )
    return RoutingDecisionRecord(
        decision_id=record.decision_id,
        stage=record.stage,
        selected_model_id=override.model_id,
        verdicts=verdicts,
        limitation=None,
        manual_override_applied=True,
        decided_at=record.decided_at,
        schedule=record.schedule,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_decisions(records: Sequence[RoutingDecisionRecord]) -> str:
    """Deterministic JSON for review: sorted keys, stable order, trailing newline."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "decisions": [record.to_mapping() for record in records],
        "routing_rules": {
            "gate_order": list(GATE_ORDER),
            "every_gate_is_recorded": True,
            "unknown_gate_blocks": True,
            "hosted_fallback_available": False,
            "simultaneous_residency_permitted": False,
            "activates_native_lifecycle": False,
            "accounting_implies_completion": False,
            "manual_override_can_bypass_a_gate": False,
        },
        "does_not_prove": [
            "a routing decision selects a model; it does not load it, dispatch it or prove it "
            "produces good output",
            "a passed quality gate rests on whatever probes the caller measured, on one configuration",
            "resource fit is computed from charged and observed bytes, not from a hardware reservation",
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
