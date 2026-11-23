"""Measured model capability and compatibility profiles (AP-013).

The point of this module is that a *capability* is a measured property of an
exact configuration running on exact hardware, not a property of a brand, a
parameter count, or a model card. Everything here is built to make that
distinction impossible to blur:

* A :class:`CapabilityProfile` is pinned to a :class:`ConfigurationIdentity` —
  checkpoint digest and revision, quantisation, runtime and runtime version,
  template, context length, and a hardware fingerprint. Two configurations of
  the "same" model are different profiles, and a measurement never migrates
  between them.
* Every measured value carries a :class:`Measurement` with its own provenance.
  A model-card claim or an imported third-party benchmark is a
  :class:`Claim` — recorded, and explicitly *not* local qualification. Asking a
  profile for a capability that no local measurement covers returns
  ``UNKNOWN`` and a reason; it never returns a guess, and ``unknown`` never
  silently degrades into "probably fine".
* Task kinds are measured separately. Writing, coding, architecture, source
  handling, and schema/tool reliability do not pool into one score, because a
  model that writes well and cannot emit valid tool calls is not an average of
  the two.
* Dense and mixture-of-experts candidates are compared on total resident
  memory, active computation per token, KV-cache at context, and measured task
  quality. Low active parameters do not imply low RAM: an MoE checkpoint holds
  every expert resident unless the runtime can page them, and this module
  refuses to infer otherwise.
* Role suggestions (Mistral/TwIL/Qwen/Gemma and friends) start as
  :class:`RoleHypothesis` values with status ``untested``. Only a recorded
  profile for that exact configuration can promote one, and a promoted
  hypothesis is scoped to that configuration — never to the family.

The module is I/O-thin and advisory. It starts no runtime, loads no model, and
downloads nothing; probes are injected. It imports the existing registry,
calibration and hardware contracts rather than restating them. Nothing here
activates a model, recommends a purchase, or wires a production caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor

SCHEMA_VERSION = 1

CapabilityState = Literal["measured", "unknown", "partially_measured"]
TaskKind = Literal[
    "writing", "coding", "architecture", "source_handling", "schema_tool"
]
TASK_KINDS: tuple[TaskKind, ...] = (
    "writing",
    "coding",
    "architecture",
    "source_handling",
    "schema_tool",
)
ProvenanceKind = Literal[
    "local_probe",
    "model_card",
    "imported_benchmark",
    "vendor_claim",
    "operator_override",
]
#: Only a local probe on this exact configuration counts as qualification.
QUALIFYING_PROVENANCE: frozenset[str] = frozenset({"local_probe"})
ArchitectureKind = Literal["dense", "mixture_of_experts"]
HypothesisStatus = Literal["untested", "supported", "contradicted"]

_MIN_PROBES = 3
_MAX_SPREAD = 0.5
_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


class CapabilityProfileError(ValueError):
    """Raised when a profile, measurement or comparison input is invalid."""


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
        raise CapabilityProfileError(f"{field_name} must not be empty.")
    if "\x00" in result:
        raise CapabilityProfileError(f"{field_name} must not contain a NUL character.")
    if len(result) > max_length:
        raise CapabilityProfileError(
            f"{field_name} must be at most {max_length} characters."
        )
    return result or None


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise CapabilityProfileError(f"{field_name} must be a positive integer.")
    return value


def _score(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number.")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise CapabilityProfileError(f"{field_name} must be within [0, 1].")
    return round(number, 6)


# ---------------------------------------------------------------------------
# AC1 — the configuration identity a measurement is pinned to
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigurationIdentity:
    """Exactly which model, in which runtime, on which hardware.

    A capability measured here cannot be transferred to a different
    quantisation, runtime version, template or host, and the module refuses to
    pretend otherwise. ``template`` is part of the identity because the chat
    template changes tool-call and schema behaviour more than the weights do.
    """

    model_id: str
    checkpoint_digest: str
    checkpoint_revision: str
    runtime: str
    runtime_version: str
    quantization: str | None
    template: str
    context_length: int
    hardware_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "model_id",
            "checkpoint_revision",
            "runtime",
            "runtime_version",
            "template",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        digest = _text(self.checkpoint_digest, "checkpoint_digest")
        if not _DIGEST_RE.fullmatch(str(digest)):
            raise CapabilityProfileError(
                "checkpoint_digest must be a sha256 hex digest (optionally prefixed sha256:)."
            )
        object.__setattr__(self, "checkpoint_digest", digest)
        object.__setattr__(
            self,
            "quantization",
            _text(self.quantization, "quantization", optional=True),
        )
        object.__setattr__(
            self, "context_length", _positive_int(self.context_length, "context_length")
        )
        object.__setattr__(
            self,
            "hardware_fingerprint",
            _text(self.hardware_fingerprint, "hardware_fingerprint"),
        )

    @classmethod
    def from_descriptor(
        cls,
        descriptor: ModelDescriptor,
        *,
        runtime_version: str,
        template: str,
        hardware: HardwareProfile | str,
        context_length: int | None = None,
        quantization: str | None = None,
    ) -> ConfigurationIdentity:
        """Build the identity from the existing registry and hardware contracts."""
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor value.")
        fingerprint = (
            hardware if isinstance(hardware, str) else hardware_fingerprint(hardware)
        )
        return cls(
            model_id=descriptor.model_id,
            checkpoint_digest=descriptor.digest,
            checkpoint_revision=descriptor.revision,
            runtime=descriptor.runtime,
            runtime_version=runtime_version,
            quantization=quantization
            if quantization is not None
            else descriptor.quantization,
            template=template,
            context_length=context_length
            if context_length is not None
            else descriptor.limits.context_tokens,
            hardware_fingerprint=fingerprint,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "model_id": self.model_id,
            "checkpoint_digest": self.checkpoint_digest,
            "checkpoint_revision": self.checkpoint_revision,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "quantization": self.quantization,
            "template": self.template,
            "context_length": self.context_length,
            "hardware_fingerprint": self.hardware_fingerprint,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())

    @property
    def short_label(self) -> str:
        quant = self.quantization or "unquantized"
        return f"{self.model_id}@{self.checkpoint_revision}/{quant}/{self.runtime} {self.runtime_version}/{self.template}"


def hardware_fingerprint(hardware: HardwareProfile) -> str:
    """Stable fingerprint of a hardware profile, so measurements bind to a host."""
    if not isinstance(hardware, HardwareProfile):
        raise TypeError("hardware must be a HardwareProfile value.")
    payload = hardware.to_mapping()
    return _digest(dict(payload))


# ---------------------------------------------------------------------------
# AC1/AC4 — measurement provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    """One measured value with the provenance that makes it evidence or not.

    ``qualifies`` is derived, never supplied: only ``local_probe`` qualifies.
    A model card or an imported benchmark is recorded faithfully and still does
    not qualify, which is the whole of AC4.
    """

    value: float
    provenance: ProvenanceKind
    source: str
    measured_at: str
    sample_count: int = 1
    spread: float | None = None
    configuration_digest: str | None = None

    def __post_init__(self) -> None:
        if self.provenance not in (
            "local_probe",
            "model_card",
            "imported_benchmark",
            "vendor_claim",
            "operator_override",
        ):
            raise CapabilityProfileError(f"unknown provenance: {self.provenance!r}.")
        object.__setattr__(self, "value", _score(self.value, "value"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "measured_at", _text(self.measured_at, "measured_at"))
        object.__setattr__(
            self, "sample_count", _positive_int(self.sample_count, "sample_count")
        )
        if self.spread is not None:
            if (
                isinstance(self.spread, bool)
                or not isinstance(self.spread, (int, float))
                or self.spread < 0
            ):
                raise CapabilityProfileError(
                    "spread must be a non-negative number or None."
                )
            object.__setattr__(self, "spread", float(self.spread))
        if self.provenance == "local_probe" and self.configuration_digest is None:
            raise CapabilityProfileError(
                "a local probe must name the configuration digest it measured; "
                "an unattributed probe is not local qualification."
            )
        object.__setattr__(
            self,
            "configuration_digest",
            _text(self.configuration_digest, "configuration_digest", optional=True),
        )

    @property
    def qualifies(self) -> bool:
        """Whether this measurement counts as local qualification (AC4)."""
        return self.provenance in QUALIFYING_PROVENANCE

    @property
    def confident(self) -> bool:
        """Enough repeated probes, and stable enough, to trust the number."""
        if self.sample_count < _MIN_PROBES:
            return False
        return self.spread is None or self.spread <= _MAX_SPREAD

    def to_mapping(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "provenance": self.provenance,
            "source": self.source,
            "measured_at": self.measured_at,
            "sample_count": self.sample_count,
            "spread": self.spread,
            "configuration_digest": self.configuration_digest,
            "qualifies": self.qualifies,
            "confident": self.confident,
            "does_not_qualify_reason": None
            if self.qualifies
            else f"{self.provenance} is not a local measurement of this configuration (AC4).",
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """A model-card or imported-benchmark claim, recorded and never counted.

    This type exists so that a caller *can* keep the marketing claim next to the
    measurement without any chance of it being read as one. It carries no
    capability value at all.
    """

    statement: str
    origin: ProvenanceKind
    source: str

    def __post_init__(self) -> None:
        if self.origin not in ("model_card", "imported_benchmark", "vendor_claim"):
            raise CapabilityProfileError(
                "a Claim records a model card, an imported benchmark or a vendor claim; "
                "local probes and operator overrides are Measurement values."
            )
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        object.__setattr__(self, "source", _text(self.source, "source"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "origin": self.origin,
            "source": self.source,
            "qualifies": False,
            "does_not_qualify_reason": "a claim is not a local measurement of this configuration (AC4).",
        }


# ---------------------------------------------------------------------------
# AC2 — companions, installed disk, readiness, residency
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelCompanion:
    """A separate artifact a model needs: projector, tokenizer, adapter, encoder.

    Companions are *separate* from the model checkpoint. A vision projector that
    was never downloaded, or an adapter pinned to a different base revision, is
    a compatibility failure and not a capability gap in the weights.
    """

    companion_id: str
    kind: str
    digest: str
    size_bytes: int
    bound_to_checkpoint_digest: str | None = None
    present: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "companion_id", _text(self.companion_id, "companion_id")
        )
        object.__setattr__(self, "kind", _text(self.kind, "kind"))
        digest = _text(self.digest, "digest")
        if not _DIGEST_RE.fullmatch(str(digest)):
            raise CapabilityProfileError(
                "companion digest must be a sha256 hex digest."
            )
        object.__setattr__(self, "digest", digest)
        object.__setattr__(
            self, "size_bytes", _positive_int(self.size_bytes, "size_bytes")
        )
        object.__setattr__(
            self,
            "bound_to_checkpoint_digest",
            _text(
                self.bound_to_checkpoint_digest,
                "bound_to_checkpoint_digest",
                optional=True,
            ),
        )
        object.__setattr__(self, "present", bool(self.present))

    def compatible_with(self, identity: ConfigurationIdentity) -> tuple[bool, str]:
        if not self.present:
            return False, f"companion {self.companion_id!r} is not installed."
        if (
            self.bound_to_checkpoint_digest
            and self.bound_to_checkpoint_digest != identity.checkpoint_digest
        ):
            return False, (
                f"companion {self.companion_id!r} is bound to a different checkpoint; "
                "an adapter for another base revision is not this model's adapter."
            )
        return True, "companion is present and bound to this checkpoint."

    def to_mapping(self) -> dict[str, Any]:
        return {
            "companion_id": self.companion_id,
            "kind": self.kind,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "bound_to_checkpoint_digest": self.bound_to_checkpoint_digest,
            "present": self.present,
        }


@dataclass(frozen=True, slots=True)
class InstalledFootprint:
    """What this configuration actually occupies on disk.

    Distinct from the claim on a download page: weights, companions and KV-cache
    provision are counted separately so a total can be audited.
    """

    weights_bytes: int
    companions_bytes: int = 0
    kv_cache_bytes_at_context: int = 0

    def __post_init__(self) -> None:
        for name in ("weights_bytes", "companions_bytes", "kv_cache_bytes_at_context"):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))

    @property
    def total_bytes(self) -> int:
        return (
            self.weights_bytes + self.companions_bytes + self.kv_cache_bytes_at_context
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "weights_bytes": self.weights_bytes,
            "companions_bytes": self.companions_bytes,
            "kv_cache_bytes_at_context": self.kv_cache_bytes_at_context,
            "total_bytes": self.total_bytes,
        }


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a non-negative integer.")
    if value < 0:
        raise CapabilityProfileError(f"{field_name} must be a non-negative integer.")
    return value


ReadinessState = Literal["ready", "not_installed", "incompatible", "unknown"]


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether this configuration could be run right now, and why not."""

    state: ReadinessState
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in ("ready", "not_installed", "incompatible", "unknown"):
            raise CapabilityProfileError(f"unknown readiness state: {self.state!r}.")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.state == "ready" and self.reasons:
            raise CapabilityProfileError(
                "a ready configuration has no blocking reasons."
            )

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"

    def to_mapping(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": list(self.reasons)}


Residency = Literal["resident", "cold", "unloaded", "unknown"]


@dataclass(frozen=True, slots=True)
class ResidencyState:
    """Whether the weights are resident right now — an observation, not a capability.

    Conflating "installed" with "loaded" is how a configuration looks capable in
    a plan and then takes minutes to answer, so residency is tracked separately
    and is explicitly time-stamped.
    """

    state: Residency
    observed_at: str
    resident_bytes: int = 0

    def __post_init__(self) -> None:
        if self.state not in ("resident", "cold", "unloaded", "unknown"):
            raise CapabilityProfileError(f"unknown residency state: {self.state!r}.")
        object.__setattr__(self, "observed_at", _text(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "resident_bytes",
            _non_negative_int(self.resident_bytes, "resident_bytes"),
        )
        if self.state == "resident" and self.resident_bytes <= 0:
            raise CapabilityProfileError(
                "a resident model must report its resident bytes."
            )
        if self.state != "resident" and self.resident_bytes:
            raise CapabilityProfileError(
                "a non-resident model must not report resident bytes."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "observed_at": self.observed_at,
            "resident_bytes": self.resident_bytes,
        }


CompatibilityState = Literal["supported", "unsupported", "unknown"]


@dataclass(frozen=True, slots=True)
class CompatibilityClaim:
    """Whether the exact runtime supports a feature, for this configuration.

    "The model has vision" and "this runtime can serve images for this
    checkpoint" are different questions; both must be answered before a
    capability is offered, and unsupported is not the same as untested.
    """

    feature: str
    state: CompatibilityState
    evidence: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature", _text(self.feature, "feature"))
        if self.state not in ("supported", "unsupported", "unknown"):
            raise CapabilityProfileError(
                f"unknown compatibility state: {self.state!r}."
            )
        object.__setattr__(self, "evidence", _text(self.evidence, "evidence"))

    def to_mapping(self) -> dict[str, Any]:
        return {"feature": self.feature, "state": self.state, "evidence": self.evidence}


# ---------------------------------------------------------------------------
# AC7/AC8 — architecture: dense vs mixture-of-experts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchitectureFootprint:
    """Resident memory and active computation for a dense or MoE candidate.

    ``total_parameters`` is what must fit in memory. ``active_parameters`` is
    what costs compute per token. For an MoE these differ by a lot, which is
    exactly why a low active count must never be read as a low RAM figure —
    :meth:`resident_memory_story` says so in words rather than leaving it to
    the reader.
    """

    kind: ArchitectureKind
    total_parameters: int
    active_parameters: int
    expert_count: int = 0
    weights_bytes: int = 0

    def __post_init__(self) -> None:
        if self.kind not in ("dense", "mixture_of_experts"):
            raise CapabilityProfileError(f"unknown architecture kind: {self.kind!r}.")
        object.__setattr__(
            self,
            "total_parameters",
            _positive_int(self.total_parameters, "total_parameters"),
        )
        object.__setattr__(
            self,
            "active_parameters",
            _positive_int(self.active_parameters, "active_parameters"),
        )
        object.__setattr__(
            self, "expert_count", _non_negative_int(self.expert_count, "expert_count")
        )
        object.__setattr__(
            self,
            "weights_bytes",
            _non_negative_int(self.weights_bytes, "weights_bytes"),
        )
        if self.active_parameters > self.total_parameters:
            raise CapabilityProfileError(
                "active_parameters cannot exceed total_parameters."
            )
        if self.kind == "dense" and self.active_parameters != self.total_parameters:
            raise CapabilityProfileError(
                "a dense model activates all of its parameters."
            )
        if self.kind == "mixture_of_experts":
            if self.expert_count < 2:
                raise CapabilityProfileError(
                    "a mixture-of-experts model must name at least two experts."
                )
            if self.active_parameters >= self.total_parameters:
                raise CapabilityProfileError(
                    "a mixture-of-experts model activates a subset of its parameters; "
                    "a fully-active MoE is recorded as dense."
                )

    @property
    def activation_ratio(self) -> float:
        return round(self.active_parameters / self.total_parameters, 6)

    @property
    def experts_are_separately_loadable(self) -> bool:
        """Never inferred. A runtime capability must be verified for this exact runtime."""
        return False

    def resident_memory_story(self) -> str:
        if self.kind == "dense":
            return (
                f"dense: all {self.total_parameters} parameters are resident and active, "
                "so memory and compute scale together."
            )
        return (
            f"mixture of experts: {self.expert_count} experts, {self.total_parameters} total parameters "
            f"resident, {self.active_parameters} active per token ({self.activation_ratio:.1%} activation). "
            "Low active parameters do not establish low RAM use (AC7): every expert is resident "
            "unless this exact runtime is verified to page them."
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "total_parameters": self.total_parameters,
            "active_parameters": self.active_parameters,
            "activation_ratio": self.activation_ratio,
            "expert_count": self.expert_count,
            "weights_bytes": self.weights_bytes,
            "experts_are_separately_loadable": self.experts_are_separately_loadable,
            "resident_memory_story": self.resident_memory_story(),
            "active_parameters_establish_resident_memory": False,
        }


@dataclass(frozen=True, slots=True)
class ExpertAsAgentClaim:
    """A proposal to use one MoE expert as a separate coding or writing agent.

    Recorded so it can be refused by name: no verified runtime capability for
    this exact runtime exists, so the claim stays unsupported. Silently
    splitting an MoE checkpoint into "agents" would be an invented capability.
    """

    proposal: str
    runtime_verified: bool = False
    evidence: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal", _text(self.proposal, "proposal"))
        object.__setattr__(
            self, "evidence", _text(self.evidence, "evidence", optional=True)
        )
        object.__setattr__(self, "runtime_verified", bool(self.runtime_verified))

    @property
    def supported(self) -> bool:
        return self.runtime_verified and bool(self.evidence)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "proposal": self.proposal,
            "runtime_verified": self.runtime_verified,
            "evidence": self.evidence,
            "supported": self.supported,
            "refusal_reason": None
            if self.supported
            else (
                "MoE experts are not separately loadable agents without a specifically verified "
                "runtime capability for this exact runtime (AC8)."
            ),
        }


@dataclass(frozen=True, slots=True)
class CandidateComparison:
    """Dense vs MoE, compared on what actually decides fit and quality."""

    left: ArchitectureFootprint
    right: ArchitectureFootprint
    left_quality: Mapping[str, Measurement] = field(default_factory=dict)
    right_quality: Mapping[str, Measurement] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        def quality(scores: Mapping[str, Measurement]) -> dict[str, Any]:
            return {
                kind: {
                    "value": item.value,
                    "qualifies": item.qualifies,
                    "confident": item.confident,
                }
                for kind, item in sorted(scores.items())
            }

        return {
            "resident_memory_bytes": {
                "left": self.left.weights_bytes,
                "right": self.right.weights_bytes,
            },
            "active_parameters": {
                "left": self.left.active_parameters,
                "right": self.right.active_parameters,
            },
            "total_parameters": {
                "left": self.left.total_parameters,
                "right": self.right.total_parameters,
            },
            "task_quality": {
                "left": quality(self.left_quality),
                "right": quality(self.right_quality),
            },
            "low_active_parameters_establish_low_memory": False,
            "note": (
                "Active parameters decide compute per token; total resident parameters and the KV cache "
                "decide whether the configuration fits. A lower active count on the same host is not "
                "evidence of lower RAM use (AC7)."
            ),
        }


# ---------------------------------------------------------------------------
# AC6 — role hypotheses
# ---------------------------------------------------------------------------


_FAMILY_ROLES: dict[str, str] = {
    "mistral": "general and coding assistant",
    "twil": "long-context source handling",
    "qwen": "coding and tool-calling assistant",
    "gemma": "writing and summarisation assistant",
}


@dataclass(frozen=True, slots=True)
class RoleHypothesis:
    """A role suggestion for a model family. Never a qualification (AC6).

    Hypotheses are created ``untested``. ``evaluate_hypothesis`` can promote one
    to ``supported`` only from a confident local measurement of the *exact*
    configuration — and even then the result is scoped to that configuration,
    not to the family.
    """

    model_family: str
    role: str
    task_kind: TaskKind
    rationale: str = ""
    status: HypothesisStatus = "untested"
    evidence_configuration_digest: str | None = None
    evidence_value: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "model_family", _text(self.model_family, "model_family")
        )
        object.__setattr__(self, "role", _text(self.role, "role"))
        object.__setattr__(
            self, "rationale", _text(self.rationale, "rationale", optional=True) or ""
        )
        if self.task_kind not in TASK_KINDS:
            raise CapabilityProfileError(f"unknown task kind: {self.task_kind!r}.")
        if self.status not in ("untested", "supported", "contradicted"):
            raise CapabilityProfileError(f"unknown hypothesis status: {self.status!r}.")
        if self.status != "untested" and self.evidence_configuration_digest is None:
            raise CapabilityProfileError(
                "a tested hypothesis must name the exact configuration that tested it."
            )

    @property
    def is_qualification(self) -> bool:
        """A hypothesis is never a qualification, whatever its status."""
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "role": self.role,
            "task_kind": self.task_kind,
            "rationale": self.rationale,
            "status": self.status,
            "evidence_configuration_digest": self.evidence_configuration_digest,
            "evidence_value": self.evidence_value,
            "is_qualification": False,
            "note": "A role suggestion remains a hypothesis until tested on this exact configuration (AC6).",
        }


def default_role_hypotheses() -> tuple[RoleHypothesis, ...]:
    """The four named family suggestions, created untested on purpose."""
    return tuple(
        RoleHypothesis(model_family=family, role=role, task_kind="coding")
        for family, role in sorted(_FAMILY_ROLES.items())
    )


def evaluate_hypothesis(
    hypothesis: RoleHypothesis, profile: CapabilityProfile
) -> RoleHypothesis:
    """Test a hypothesis against one measured configuration."""
    if hypothesis.status != "untested":
        raise CapabilityProfileError("only an untested hypothesis can be evaluated.")
    state, measurement, _reason = profile.capability(hypothesis.task_kind)
    if (
        state != "measured"
        or measurement is None
        or not measurement.qualifies
        or not measurement.confident
    ):
        return RoleHypothesis(
            model_family=hypothesis.model_family,
            role=hypothesis.role,
            task_kind=hypothesis.task_kind,
            rationale=hypothesis.rationale,
            status="untested",
            evidence_configuration_digest=profile.identity.digest,
        )
    return RoleHypothesis(
        model_family=hypothesis.model_family,
        role=hypothesis.role,
        task_kind=hypothesis.task_kind,
        rationale=hypothesis.rationale,
        status="supported" if measurement.value >= 0.5 else "contradicted",
        evidence_configuration_digest=profile.identity.digest,
        evidence_value=measurement.value,
    )


# ---------------------------------------------------------------------------
# The profile itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManualOverride:
    """A bounded operator override for one task kind.

    Overrides exist so an operator can say "this configuration does the job"
    when measurement is impossible. They are bounded on purpose: they name the
    configuration, the task kind, a rationale and an expiry, they are reported
    as ``operator_override`` and never as ``measured``, and they cannot change a
    measured value that exists.
    """

    task_kind: TaskKind
    configuration_digest: str
    rationale: str
    expires_at: str
    overridden_by: str

    def __post_init__(self) -> None:
        if self.task_kind not in TASK_KINDS:
            raise CapabilityProfileError(f"unknown task kind: {self.task_kind!r}.")
        for name in (
            "configuration_digest",
            "rationale",
            "expires_at",
            "overridden_by",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))

    def active(self, *, now: str) -> bool:
        return now <= self.expires_at

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "configuration_digest": self.configuration_digest,
            "rationale": self.rationale,
            "expires_at": self.expires_at,
            "overridden_by": self.overridden_by,
            "is_measurement": False,
        }


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """One exact configuration's measured capability and compatibility record.

    ``capability(kind)`` is deliberately three-valued. ``unknown`` is a real
    answer with a reason, and it is the answer whenever no local measurement of
    this exact configuration exists — including when a model card says the model
    is excellent at that task.
    """

    identity: ConfigurationIdentity
    measurements: Mapping[TaskKind, Measurement] = field(default_factory=dict)
    claims: tuple[Claim, ...] = ()
    companions: tuple[ModelCompanion, ...] = ()
    footprint: InstalledFootprint | None = None
    readiness: Readiness | None = None
    residency: ResidencyState | None = None
    compatibility: tuple[CompatibilityClaim, ...] = ()
    architecture: ArchitectureFootprint | None = None
    overrides: tuple[ManualOverride, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ConfigurationIdentity):
            raise TypeError("identity must be a ConfigurationIdentity value.")
        clean: dict[TaskKind, Measurement] = {}
        for kind, item in dict(self.measurements).items():
            if kind not in TASK_KINDS:
                raise CapabilityProfileError(f"unknown task kind: {kind!r}.")
            if not isinstance(item, Measurement):
                raise TypeError(
                    "measurements must map task kinds to Measurement values."
                )
            if (
                item.configuration_digest is not None
                and item.configuration_digest != self.identity.digest
            ):
                raise CapabilityProfileError(
                    f"a measurement for {kind!r} belongs to a different configuration; "
                    "measurements do not migrate between configurations."
                )
            clean[kind] = item
        object.__setattr__(self, "measurements", clean)
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "companions", tuple(self.companions))
        object.__setattr__(self, "compatibility", tuple(self.compatibility))
        object.__setattr__(self, "overrides", tuple(self.overrides))

    # -- capability ---------------------------------------------------------

    def capability(
        self, kind: TaskKind
    ) -> tuple[CapabilityState, Measurement | None, str]:
        """What do we know about this configuration doing ``kind``, and why.

        Returns ``(state, measurement, reason)``. Only a local probe of this
        configuration yields ``measured``; a claim never does.
        """
        if kind not in TASK_KINDS:
            raise CapabilityProfileError(f"unknown task kind: {kind!r}.")
        item = self.measurements.get(kind)
        if item is not None and item.qualifies:
            reason = "measured locally on this configuration"
            if not item.confident:
                reason += (
                    f", but only {item.sample_count} probe(s) and spread {item.spread}; "
                    "treat as indicative, not qualified"
                )
            return "measured", item, reason
        if item is not None:
            return (
                "unknown",
                item,
                f"the only value for {kind!r} came from {item.provenance}, which is not local "
                "qualification (AC4).",
            )
        claiming = [claim for claim in self.claims if kind in claim.statement.lower()]
        if claiming:
            return (
                "unknown",
                None,
                f"no local measurement for {kind!r}; {len(claiming)} claim(s) mention it and claims "
                "are not qualification (AC4).",
            )
        return (
            "unknown",
            None,
            f"no local measurement exists for {kind!r} on this configuration (AC5).",
        )

    def numeric(self, kind: TaskKind) -> float | None:
        """The number, or ``None``. Never a default, never a guess."""
        state, item, _reason = self.capability(kind)
        if state != "measured" or item is None:
            return None
        return item.value

    @property
    def measured_kinds(self) -> tuple[TaskKind, ...]:
        return tuple(
            kind for kind in TASK_KINDS if self.capability(kind)[0] == "measured"
        )

    @property
    def unknown_kinds(self) -> tuple[TaskKind, ...]:
        return tuple(
            kind for kind in TASK_KINDS if self.capability(kind)[0] == "unknown"
        )

    def knows(self, kind: TaskKind) -> bool:
        return self.capability(kind)[0] == "measured"

    # -- compatibility ------------------------------------------------------

    def supports(self, feature: str) -> CompatibilityState:
        """Runtime support for a feature. ``unknown`` is not ``unsupported``."""
        for claim in self.compatibility:
            if claim.feature == feature:
                return claim.state
        return "unknown"

    def companion_problems(self) -> tuple[str, ...]:
        problems = []
        for companion in self.companions:
            ok, reason = companion.compatible_with(self.identity)
            if not ok:
                problems.append(reason)
        return tuple(problems)

    # -- overrides ----------------------------------------------------------

    def override_for(self, kind: TaskKind, *, now: str) -> ManualOverride | None:
        for override in self.overrides:
            if (
                override.task_kind == kind
                and override.configuration_digest == self.identity.digest
                and override.active(now=now)
            ):
                return override
        return None

    def effective(self, kind: TaskKind, *, now: str) -> tuple[str, str]:
        """How this configuration would be treated for ``kind``, and on what basis."""
        state, item, reason = self.capability(kind)
        if state == "measured" and item is not None and item.confident:
            return "qualified", reason
        override = self.override_for(kind, now=now)
        if override is not None:
            return "operator_override", (
                f"operator override by {override.overridden_by}: {override.rationale} "
                f"(expires {override.expires_at}; not a measurement)"
            )
        if state == "measured":
            return "indicative", reason
        return "unknown", reason

    # -- serialisation ------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": self.identity.to_mapping(),
            "identity_digest": self.identity.digest,
            "capabilities": {
                kind: {
                    "state": state,
                    "value": item.value if item else None,
                    "qualifies": item.qualifies if item else False,
                    "confident": item.confident if item else False,
                    "provenance": item.provenance if item else None,
                    "reason": reason,
                }
                for kind in TASK_KINDS
                for state, item, reason in (self.capability(kind),)
            },
            "measurements": {
                kind: item.to_mapping()
                for kind, item in sorted(self.measurements.items())
            },
            "claims": [claim.to_mapping() for claim in self.claims],
            "companions": [companion.to_mapping() for companion in self.companions],
            "companion_problems": list(self.companion_problems()),
            "footprint": self.footprint.to_mapping() if self.footprint else None,
            "readiness": self.readiness.to_mapping() if self.readiness else None,
            "residency": self.residency.to_mapping() if self.residency else None,
            "compatibility": [claim.to_mapping() for claim in self.compatibility],
            "architecture": self.architecture.to_mapping()
            if self.architecture
            else None,
            "overrides": [override.to_mapping() for override in self.overrides],
            "measured_kinds": list(self.measured_kinds),
            "unknown_kinds": list(self.unknown_kinds),
            "claims_are_qualification": False,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


# ---------------------------------------------------------------------------
# AC5 — the boundary an unknown capability creates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityBoundary:
    """What a caller may and may not do with a profile for a required task kind."""

    model_id: str
    task_kind: TaskKind
    decision: str
    basis: str
    reason: str

    def __post_init__(self) -> None:
        if self.decision not in ("allow", "degrade", "refuse"):
            raise CapabilityProfileError(f"unknown decision: {self.decision!r}.")
        if self.basis not in ("measured", "operator_override", "indicative", "unknown"):
            raise CapabilityProfileError(f"unknown basis: {self.basis!r}.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "task_kind": self.task_kind,
            "decision": self.decision,
            "basis": self.basis,
            "reason": self.reason,
            "is_qualification": self.basis == "measured",
        }


def require_capability(
    profile: CapabilityProfile,
    kind: TaskKind,
    *,
    now: str,
    allow_override: bool = True,
) -> CapabilityBoundary:
    """Decide what an unmeasured capability means for a required task kind.

    Unknown fails explicitly (AC5). An operator override turns a refusal into a
    bounded ``degrade`` — it never becomes ``allow`` and never claims to be a
    measurement.
    """
    if not isinstance(profile, CapabilityProfile):
        raise TypeError("profile must be a CapabilityProfile value.")
    treatment, reason = profile.effective(kind, now=now)
    basis = {
        "qualified": "measured",
        "indicative": "indicative",
        "operator_override": "operator_override",
        "unknown": "unknown",
    }[treatment]
    if treatment == "qualified":
        decision = "allow"
    elif treatment == "operator_override" and allow_override:
        decision = "degrade"
    else:
        decision = "refuse"
        if treatment == "operator_override" and not allow_override:
            reason = f"{reason}; this caller does not accept an operator override."
    return CapabilityBoundary(
        model_id=profile.identity.model_id,
        task_kind=kind,
        decision=decision,
        basis=basis,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Probing and aggregation (AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskProbe:
    """One probe of one task kind against one configuration, as the runner saw it."""

    task_kind: TaskKind
    configuration_digest: str
    score: float
    passed: bool
    measured_at: str
    source: str = "local task-quality probe"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.task_kind not in TASK_KINDS:
            raise CapabilityProfileError(f"unknown task kind: {self.task_kind!r}.")
        object.__setattr__(
            self,
            "configuration_digest",
            _text(self.configuration_digest, "configuration_digest"),
        )
        object.__setattr__(self, "score", _score(self.score, "score"))
        object.__setattr__(self, "measured_at", _text(self.measured_at, "measured_at"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "notes", _text(self.notes, "notes", optional=True) or ""
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "configuration_digest": self.configuration_digest,
            "score": self.score,
            "passed": self.passed,
            "measured_at": self.measured_at,
            "source": self.source,
            "notes": self.notes,
        }


def summarise_task_probes(
    identity: ConfigurationIdentity,
    probes: Sequence[TaskProbe],
    *,
    kind: TaskKind,
    measured_at: str,
    source: str = "local task-quality probe",
) -> Measurement:
    """Turn repeated probes of one task kind into a measurement.

    Task kinds never pool: this function takes one kind and refuses probes of
    another. A failed probe is still a sample, and its score counts — a model
    that passes two of three attempts is not a model that scored 1.0.
    """
    if not isinstance(identity, ConfigurationIdentity):
        raise TypeError("identity must be a ConfigurationIdentity value.")
    if kind not in TASK_KINDS:
        raise CapabilityProfileError(f"unknown task kind: {kind!r}.")
    selected = [probe for probe in probes if probe.task_kind == kind]
    if len(selected) != len(probes):
        raise CapabilityProfileError(
            "probes of different task kinds must not pool; measure each kind separately (AC3)."
        )
    if not selected:
        raise CapabilityProfileError(f"no probes supplied for {kind!r}.")
    for probe in selected:
        if probe.configuration_digest != identity.digest:
            raise CapabilityProfileError(
                "a probe was run against a different configuration."
            )
    scores = [probe.score for probe in selected]
    value = round(sum(scores) / len(scores), 6)
    spread = round(max(scores) - min(scores), 6)
    return Measurement(
        value=value,
        provenance="local_probe",
        source=source,
        measured_at=measured_at,
        sample_count=len(selected),
        spread=spread,
        configuration_digest=identity.digest,
    )


# ---------------------------------------------------------------------------
# AC2 — readiness and footprint assembly
# ---------------------------------------------------------------------------


def assess_readiness(
    *,
    weights_present: bool,
    companions: Sequence[ModelCompanion] = (),
    identity: ConfigurationIdentity,
    runtime_supports_architecture: bool | None = None,
) -> Readiness:
    """Decide readiness from installed state, not from hope."""
    reasons: list[str] = []
    if not weights_present:
        reasons.append("model weights are not installed.")
    for companion in companions:
        ok, reason = companion.compatible_with(identity)
        if not ok:
            reasons.append(reason)
    if runtime_supports_architecture is False:
        reasons.append(
            f"runtime {identity.runtime} {identity.runtime_version} does not support this architecture."
        )
    if reasons:
        state: ReadinessState = (
            "not_installed" if not weights_present else "incompatible"
        )
        return Readiness(state=state, reasons=tuple(reasons))
    if runtime_supports_architecture is None:
        return Readiness(
            state="unknown",
            reasons=(
                "weights and companions are present, but runtime support for this architecture "
                "was not verified for this exact runtime version.",
            ),
        )
    return Readiness(state="ready")


def summarise_footprint(
    *,
    weights_bytes: int,
    companions: Sequence[ModelCompanion] = (),
    kv_cache_bytes_at_context: int = 0,
) -> InstalledFootprint:
    """Total disk cost, counting only companions that are actually installed."""
    companions_bytes = sum(
        companion.size_bytes for companion in companions if companion.present
    )
    return InstalledFootprint(
        weights_bytes=weights_bytes,
        companions_bytes=companions_bytes,
        kv_cache_bytes_at_context=kv_cache_bytes_at_context,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_profiles(profiles: Sequence[CapabilityProfile]) -> str:
    """Deterministic JSON for review: sorted keys, stable order, trailing newline."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profiles": [profile.to_mapping() for profile in profiles],
        "qualification_rules": {
            "qualifying_provenance": sorted(QUALIFYING_PROVENANCE),
            "claims_are_qualification": False,
            "unknown_means_refused": True,
            "measurements_migrate_between_configurations": False,
            "low_active_parameters_establish_low_memory": False,
        },
        "does_not_prove": [
            "a profile is evidence about one configuration on one host, not a general claim about the model family",
            "a measurement of one runtime version does not transfer to another runtime or quantisation",
            "no profile activates a model, reserves hardware or estimates a price",
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
