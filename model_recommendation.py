"""Deterministic advisory model recommendations over pinned local contracts.

The engine combines capability negotiation, explicit routing/consent policy,
caller-supplied benchmark profiles, and caller-supplied prices. It neither
executes models nor estimates a price that was not provided. Recommendations
are advice only: ``manual_override`` returns the user's explicit selection
without consulting recommendation feasibility.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from comparison_board import CostObservation
from execution_budget import BudgetLimits
from hardware_capabilities import HardwareProfile
from model_calibration import ModelPerformanceProfile
from model_registry import ModelDescriptor
from performance_profiles import ProfileKey, ProfileRecord, os_id_from_hardware
from routing_policy import FallbackCandidate, FallbackConsent, FallbackPolicy, evaluate_fallback

RECOMMENDATION_SCHEMA_VERSION = 1
ConfidenceLevel = Literal["high", "low", "unknown"]
_MIN_SAMPLES = 3
_MAX_RELATIVE_SPREAD = 0.5
_MAX_FAILURE_RATE = 0.25


class RecommendationError(ValueError):
    """Raised when recommendation inputs violate the advisory contract."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _identity(model: ModelDescriptor) -> tuple[str, str]:
    return model.model_id, model.digest


def _hardware_digest(snapshot: Mapping[str, Any]) -> str:
    return _digest(dict(snapshot))


def _profile_matches_key(key: ProfileKey, profile: ModelPerformanceProfile) -> bool:
    inputs = profile.inputs
    hardware = inputs.hardware
    if hardware is None:
        return False
    return (
        key.hardware_digest == _hardware_digest(hardware)
        and key.model_digest == inputs.model_digest
        and key.quantization == (inputs.quantization if inputs.quantization is not None else "unspecified")
        and key.runtime_version == (inputs.runtime_version if inputs.runtime_version is not None else "unspecified")
    )


@dataclass(frozen=True, slots=True)
class RecommendationBudget:
    """An existing execution cost limit plus caller token estimate/currency.

    ``BudgetLimits`` remains the owner of execution enforcement; this wrapper
    only projects its max_cost for advisory comparison against supplied prices.
    """

    limits: BudgetLimits
    currency: str
    estimated_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.limits, BudgetLimits):
            raise TypeError("limits must be a BudgetLimits value.")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha() or self.currency.upper() != self.currency:
            raise RecommendationError("currency must be a three-letter uppercase code.")
        if isinstance(self.estimated_tokens, bool) or not isinstance(self.estimated_tokens, int) or self.estimated_tokens < 0:
            raise RecommendationError("estimated_tokens must be a non-negative integer.")

    @property
    def max_cost(self) -> float:
        return self.limits.max_cost


@dataclass(frozen=True, slots=True)
class BenchmarkConfidence:
    """Confidence derived only from profile aggregates and failure rate."""

    level: ConfidenceLevel
    sample_count: int | None
    relative_spread: float | None
    failure_rate: float | None
    profile_digest: str | None
    note: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "sample_count": self.sample_count,
            "relative_spread": self.relative_spread,
            "failure_rate": self.failure_rate,
            "profile_digest": self.profile_digest,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """Advisory outcome for one exact model build."""

    model: ModelDescriptor
    eligible: bool
    reasons: tuple[str, ...]
    confidence: BenchmarkConfidence
    estimated_cost: float | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model.model_id,
            "model_digest": self.model.digest,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "confidence": self.confidence.to_mapping(),
            "estimated_cost": self.estimated_cost,
        }


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    """Stable, content-addressed ordered recommendations and exclusions."""

    recommendations: tuple[ModelRecommendation, ...]

    @property
    def eligible(self) -> tuple[ModelRecommendation, ...]:
        return tuple(item for item in self.recommendations if item.eligible)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": RECOMMENDATION_SCHEMA_VERSION,
            "recommendations": [item.to_mapping() for item in self.recommendations],
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def _confidence(profile: ModelPerformanceProfile | None) -> BenchmarkConfidence:
    if profile is None:
        return BenchmarkConfidence("unknown", None, None, None, None, "no comparable benchmark profile")
    aggregates = (profile.aggregate("decode_tokens_per_s", "warm"), profile.aggregate("peak_memory_bytes", "warm"))
    if any(item.count == 0 or item.median is None or item.spread is None for item in aggregates):
        return BenchmarkConfidence(
            "low", min(item.count for item in aggregates), None, profile.failure_rate, profile.digest,
            "profile lacks successful warm latency or memory measurements",
        )
    sample_count = min(item.count for item in aggregates)
    ratios = [item.spread / max(abs(item.median), 1e-12) for item in aggregates if item.median is not None and item.spread is not None]
    relative_spread = max(ratios)
    confident = sample_count >= _MIN_SAMPLES and relative_spread <= _MAX_RELATIVE_SPREAD and profile.failure_rate <= _MAX_FAILURE_RATE
    return BenchmarkConfidence(
        "high" if confident else "low",
        sample_count,
        round(relative_spread, 6),
        profile.failure_rate,
        profile.digest,
        "meets sample, spread, and failure-rate thresholds" if confident else "insufficient samples or unstable/failed measurements",
    )


def _validated_profile(
    model: ModelDescriptor,
    entry: tuple[ProfileKey, ProfileRecord] | None,
    *,
    hardware_snapshot: Mapping[str, Any] | None,
    expected_os_id: str | None,
) -> tuple[ModelPerformanceProfile | None, str | None]:
    if entry is None:
        return None, None
    if not isinstance(entry, tuple) or len(entry) != 2 or not isinstance(entry[0], ProfileKey) or not isinstance(entry[1], ProfileRecord):
        raise TypeError("performance entries must be (ProfileKey, ProfileRecord) pairs.")
    key, record = entry
    profile = record.profile
    inputs = profile.inputs
    if not _profile_matches_key(key, profile):
        return None, "benchmark profile does not agree with its key"
    if inputs.model_id != model.model_id or inputs.model_digest != model.digest:
        return None, "benchmark profile is for a different model build"
    if hardware_snapshot is None:
        return None, "hardware snapshot is required to trust benchmark context"
    if key.hardware_digest != _hardware_digest(hardware_snapshot):
        return None, "benchmark hardware context differs from current hardware"
    if expected_os_id is not None and key.os_id != expected_os_id:
        return None, "benchmark operating-system context differs from current hardware"
    if expected_os_id is None and key.os_id != os_id_from_hardware(hardware_snapshot):
        return None, "benchmark operating-system context differs from current hardware"
    return profile, None


def recommend_models(
    candidates: Sequence[ModelDescriptor],
    *,
    task: TaskRequirements,
    hardware: HardwareProfile | None,
    hardware_snapshot: Mapping[str, Any] | None,
    routes: Mapping[tuple[str, str], FallbackCandidate],
    policy: FallbackPolicy | None = None,
    consent: FallbackConsent | None = None,
    performance: Mapping[tuple[str, str], tuple[ProfileKey, ProfileRecord]] | None = None,
    costs: Mapping[tuple[str, str], CostObservation] | None = None,
    budget: RecommendationBudget | None = None,
    os_id: str | None = None,
) -> RecommendationResult:
    """Rank feasible candidates without executing, falling back, or inventing data.

    Benchmark contexts are trusted only when their hardware digest and OS match
    the supplied snapshot. When several profiles exist, all must share the
    first valid profile's hardware/OS/methodology anchor to receive confidence.
    """
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise TypeError("candidates must be a sequence of ModelDescriptor values.")
    if not candidates or any(not isinstance(item, ModelDescriptor) for item in candidates):
        raise RecommendationError("candidates must be a non-empty sequence of ModelDescriptor values.")
    identities = [_identity(item) for item in candidates]
    if len(set(identities)) != len(identities):
        raise RecommendationError("candidate model identities must be unique.")
    if not isinstance(task, TaskRequirements):
        raise TypeError("task must be a TaskRequirements value.")
    if hardware is not None and not isinstance(hardware, HardwareProfile):
        raise TypeError("hardware must be a HardwareProfile value or None.")
    if hardware_snapshot is not None and not isinstance(hardware_snapshot, Mapping):
        raise TypeError("hardware_snapshot must be a mapping or None.")
    if hardware is not None and hardware_snapshot is not None and os_id is None:
        snapshot_os = os_id_from_hardware(hardware_snapshot)
        profile_os = f"{hardware.operating_system} {hardware.architecture}"
        if snapshot_os != profile_os:
            raise RecommendationError("hardware profile and snapshot OS context disagree.")
    routes = {} if routes is None else routes
    if not isinstance(routes, Mapping):
        raise TypeError("routes must be a mapping keyed by (model_id, digest).")
    performance = {} if performance is None else performance
    costs = {} if costs is None else costs
    for mapping, field in ((performance, "performance"), (costs, "costs")):
        if not isinstance(mapping, Mapping):
            raise TypeError(f"{field} must be a mapping keyed by (model_id, digest).")
    if budget is not None and not isinstance(budget, RecommendationBudget):
        raise TypeError("budget must be a RecommendationBudget value or None.")
    if policy is not None and not isinstance(policy, FallbackPolicy):
        raise TypeError("policy must be a FallbackPolicy value or None.")
    if consent is not None and not isinstance(consent, FallbackConsent):
        raise TypeError("consent must be a FallbackConsent value or None.")

    negotiator = CapabilityNegotiator(hardware)
    preliminary: dict[tuple[str, str], tuple[ModelPerformanceProfile | None, str | None]] = {}
    anchor: tuple[str, str, str] | None = None
    for model in sorted(candidates, key=_identity):
        ident = _identity(model)
        entry = performance.get(ident)
        profile, profile_error = _validated_profile(model, entry, hardware_snapshot=hardware_snapshot, expected_os_id=os_id)
        if profile is not None and entry is not None:
            key = entry[0]
            context = (key.hardware_digest, key.os_id, key.methodology_digest)
            if anchor is None:
                anchor = context
            elif context != anchor:
                profile = None
                profile_error = "benchmark context is incomparable with the recommendation anchor"
        preliminary[ident] = (profile, profile_error)

    results: list[ModelRecommendation] = []
    for model in candidates:
        ident = _identity(model)
        reasons = list(negotiator.evaluate(model, task))
        profile, profile_error = preliminary[ident]
        if profile_error:
            reasons.append(profile_error)
        confidence = _confidence(profile)
        if profile is not None and hardware is not None and hardware.available_memory_bytes is not None:
            memory = profile.aggregate("peak_memory_bytes", "warm")
            if memory.median is not None and memory.median > hardware.available_memory_bytes:
                reasons.append("measured warm memory exceeds available hardware memory")

        route = routes.get(ident)
        if route is None:
            reasons.append("privacy route context is missing")
        elif not isinstance(route, FallbackCandidate):
            raise TypeError("routes must contain FallbackCandidate values.")
        elif route.model_id != model.model_id:
            raise RecommendationError("route model_id does not match its candidate identity.")
        else:
            decision = evaluate_fallback((route,), policy, consent)
            if not decision.eligible:
                reasons.extend(decision.exclusions)

        estimated_cost: float | None = None
        if budget is not None:
            if budget.estimated_tokens > budget.limits.max_tokens:
                reasons.append("estimated tokens exceed execution budget")
            observation = costs.get(ident)
            if observation is None:
                reasons.append("budget cannot be checked without a caller-supplied price")
            elif not isinstance(observation, CostObservation):
                raise TypeError("costs must contain CostObservation values.")
            elif observation.currency != budget.currency:
                reasons.append("price currency does not match budget currency")
            else:
                estimated_cost = observation.amount_per_million_tokens * budget.estimated_tokens / 1_000_000
                if estimated_cost > budget.max_cost:
                    reasons.append("estimated cost exceeds budget")

        results.append(ModelRecommendation(model, not reasons, tuple(reasons), confidence, estimated_cost))

    confidence_order = {"high": 0, "low": 1, "unknown": 2}
    results.sort(
        key=lambda item: (
            not item.eligible,
            confidence_order[item.confidence.level],
            item.confidence.relative_spread if item.confidence.relative_spread is not None else math.inf,
            item.confidence.failure_rate if item.confidence.failure_rate is not None else math.inf,
            item.model.model_id,
            item.model.digest,
        )
    )
    return RecommendationResult(tuple(results))


def manual_override(model: ModelDescriptor) -> ModelDescriptor:
    """Return an explicit user selection; advisory recommendation never gates it."""
    if not isinstance(model, ModelDescriptor):
        raise TypeError("manual override must be a ModelDescriptor value.")
    return model
