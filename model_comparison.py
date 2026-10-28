"""Versioned evaluation tasks, reproducible configs, and model comparison.

AS-056 defines the quality/compatibility comparison contract on top of the
AS-017 registry and AS-051 negotiation, honoring the roadmap's model
benchmarking safeguards:

- every score pins the *versioned task* and the *reproducible configuration*
  that produced it (both carry canonical digests);
- quality dimensions are aggregated as median and spread over repeated runs,
  never as a single cherry-picked sample;
- comparison only ranks mutually compatible evaluations (same task version
  and same config digest) and hard-filters models that fail the task's
  capability requirements (plus an optional hardware profile) before ranking;
- differences in the disclosed measurement environment are reported, never
  silently averaged away.

Pure data contracts: nothing here executes a model, contacts a provider, or
runs benchmarks. Measurement results are supplied by an executor (the local
benchmark stories), which keeps this module free of I/O and timing bias.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from capability_negotiation import CapabilityNegotiator, TaskRequirements
from hardware_capabilities import HardwareProfile
from model_registry import ModelDescriptor

_MAX_ID_LENGTH = 128
_ROUND_DIGITS = 6


class ModelComparisonError(ValueError):
    """Raised when an evaluation object is invalid or scores are not comparable."""


def _round(value: float) -> float:
    return round(value, _ROUND_DIGITS)


def _text(value: object, field: str, *, max_length: int = _MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result:
        raise ModelComparisonError(f"{field} must not be empty.")
    if len(result) > max_length:
        raise ModelComparisonError(f"{field} must be at most {max_length} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ModelComparisonError(f"{field} must not contain control characters.")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelComparisonError(f"{field} must be a positive integer.")
    return value


def _unit_interval(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelComparisonError(f"{field} must be a number in [0, 1].")
    number = float(value)
    if math.isnan(number) or math.isinf(number) or number < 0.0 or number > 1.0:
        raise ModelComparisonError(f"{field} must be a number in [0, 1].")
    return number


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RubricDimension:
    """One weighted quality dimension of an evaluation task."""

    name: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "rubric dimension name", max_length=64))
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise TypeError("weight must be a number.")
        weight = float(self.weight)
        if math.isnan(weight) or math.isinf(weight) or weight <= 0:
            raise ModelComparisonError("weight must be a positive finite number.")
        object.__setattr__(self, "weight", weight)

    def to_mapping(self) -> dict[str, Any]:
        return {"name": self.name, "weight": self.weight}


@dataclass(frozen=True, slots=True)
class EvalTask:
    """A versioned evaluation task: capability requirements plus a weighted rubric."""

    task_id: str
    task_version: int
    requirements: TaskRequirements
    rubric: tuple[RubricDimension, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "task_version", _positive_int(self.task_version, "task_version"))
        if not isinstance(self.requirements, TaskRequirements):
            raise TypeError("requirements must be a TaskRequirements value.")
        if isinstance(self.rubric, (str, bytes)) or not isinstance(self.rubric, Sequence):
            raise TypeError("rubric must be a sequence of RubricDimension values.")
        dimensions = tuple(RubricDimension(**item) if isinstance(item, Mapping) else item for item in self.rubric)
        if not dimensions:
            raise ModelComparisonError("rubric must contain at least one dimension.")
        for dimension in dimensions:
            if not isinstance(dimension, RubricDimension):
                raise TypeError("rubric must contain RubricDimension values.")
        names = [dimension.name for dimension in dimensions]
        if len(set(names)) != len(names):
            raise ModelComparisonError("rubric dimension names must be unique.")
        object.__setattr__(self, "rubric", tuple(sorted(dimensions, key=lambda item: item.name)))

    def dimension_names(self) -> tuple[str, ...]:
        return tuple(dimension.name for dimension in self.rubric)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "requirements": asdict(self.requirements),
            "rubric": [dimension.to_mapping() for dimension in self.rubric],
        }

    @property
    def digest(self) -> str:
        """Canonical identity of this exact task version; scores compare by it."""
        return _canonical_digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class EvalConfig:
    """The reproducible sampling/execution configuration behind a score.

    ``digest`` covers every knob a rerun must repeat. ``environment`` records
    where the run happened; it is disclosed on comparison but excluded from
    the digest, so two machines rerunning the same config stay comparable.
    """

    max_output_tokens: int
    temperature: float = 0.0
    context_length: int | None = None
    seed: int | None = None
    concurrency: int = 1
    environment: tuple[tuple[str, str], ...] | Mapping[str, str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_output_tokens", _positive_int(self.max_output_tokens, "max_output_tokens"))
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise TypeError("temperature must be a number.")
        temperature = float(self.temperature)
        if math.isnan(temperature) or math.isinf(temperature) or temperature < 0.0 or temperature > 2.0:
            raise ModelComparisonError("temperature must be within [0, 2].")
        object.__setattr__(self, "temperature", temperature)
        if self.context_length is not None:
            object.__setattr__(self, "context_length", _positive_int(self.context_length, "context_length"))
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0):
            raise ModelComparisonError("seed must be a non-negative integer or None.")
        object.__setattr__(self, "concurrency", _positive_int(self.concurrency, "concurrency"))
        raw: tuple[tuple[str, str], ...] | Mapping[str, str] | Sequence[object] = self.environment
        pairs: list[tuple[str, str]] = []
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                field = _text(key, "environment key", max_length=64)
                pairs.append((field, _text(value, f"environment[{field!r}]")))
        elif isinstance(raw, str) or not isinstance(raw, Sequence):
            raise TypeError("environment must be an object or a sequence of key/value pairs.")
        else:
            for item in raw:
                if isinstance(item, str) or not isinstance(item, Sequence) or len(item) != 2:
                    raise ModelComparisonError("environment pairs must be (key, value) of two strings.")
                pairs.append((_text(item[0], "environment key", max_length=64), _text(item[1], "environment value")))
        if len({key for key, _ in pairs}) != len(pairs):
            raise ModelComparisonError("environment keys must be unique after normalization.")
        object.__setattr__(self, "environment", tuple(sorted(pairs)))

    def environment_mapping(self) -> dict[str, str]:
        return dict(self.environment)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "context_length": self.context_length,
            "seed": self.seed,
            "concurrency": self.concurrency,
            "environment": self.environment_mapping(),
        }

    @property
    def digest(self) -> str:
        """Canonical identity of the reproducible knobs; environment is disclosed, not hashed."""
        return _canonical_digest(
            {
                "max_output_tokens": self.max_output_tokens,
                "temperature": self.temperature,
                "context_length": self.context_length,
                "seed": self.seed,
                "concurrency": self.concurrency,
            }
        )


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """Median and spread of one rubric dimension across repeated runs.

    Aggregates are derived here from the raw run values — never accepted from
    callers — so reported numbers always agree with their audit trail.
    """

    name: str
    runs: tuple[float, ...]
    median: float = 0.0
    spread: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "dimension name", max_length=64))
        if isinstance(self.runs, (str, bytes)) or not isinstance(self.runs, Sequence) or not self.runs:
            raise ModelComparisonError(f"dimension {self.name!r} needs at least one measured run value.")
        values = tuple(_unit_interval(value, f"{self.name} run value") for value in self.runs)
        object.__setattr__(self, "runs", values)
        object.__setattr__(self, "median", _round(statistics.median(values)))
        object.__setattr__(self, "spread", _round(max(values) - min(values)))

    def to_mapping(self) -> dict[str, Any]:
        return {"name": self.name, "median": self.median, "spread": self.spread, "runs": list(self.runs)}


@dataclass(frozen=True, slots=True)
class EvalScore:
    """One model's quality and compatibility result for a pinned task version
    and reproducible configuration, aggregated over repeated runs."""

    model: ModelDescriptor
    task: EvalTask
    config: EvalConfig
    dimensions: tuple[DimensionScore, ...]
    compatibility: tuple[str, ...] = ()
    inputs_digest: str | None = None

    def __post_init__(self) -> None:
        for field_name, value, expected in (
            ("model", self.model, ModelDescriptor),
            ("task", self.task, EvalTask),
            ("config", self.config, EvalConfig),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{field_name} must be a {expected.__name__} value.")
        if self.inputs_digest is not None:
            object.__setattr__(self, "inputs_digest", _text(self.inputs_digest, "inputs_digest"))
        if not isinstance(self.dimensions, tuple) or not all(isinstance(item, DimensionScore) for item in self.dimensions):
            raise TypeError("dimensions must be a tuple of DimensionScore values.")
        names = tuple(item.name for item in self.dimensions)
        if names != self.task.dimension_names():
            raise ModelComparisonError("dimensions must match the task rubric exactly (sorted by name).")
        if not isinstance(self.compatibility, tuple) or not all(isinstance(reason, str) and reason for reason in self.compatibility):
            raise ModelComparisonError("compatibility must be a tuple of non-empty reason strings.")

    @property
    def overall(self) -> float:
        """Weighted mean of the dimension medians under the task's rubric weights.

        Derived on access — it cannot drift from its inputs or be overridden.
        """
        weights = {dimension.name: dimension.weight for dimension in self.task.rubric}
        totals = sum(weights[item.name] for item in self.dimensions)
        weighted = sum(item.median * weights[item.name] for item in self.dimensions)
        return _round(weighted / totals)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model": self.model.to_mapping(),
            "task": self.task.to_mapping(),
            "task_digest": self.task.digest,
            "config": self.config.to_mapping(),
            "config_digest": self.config.digest,
            "dimensions": [item.to_mapping() for item in self.dimensions],
            "overall": self.overall,
            "compatibility": list(self.compatibility),
            "inputs_digest": self.inputs_digest,
        }

    @property
    def digest(self) -> str:
        """Canonical identity of this exact evaluation record."""
        return _canonical_digest(self.to_mapping())


def _require_values(dimensions: Mapping[str, Sequence[float]], name: str) -> Sequence[float]:
    values = dimensions.get(name)
    if values is None:
        raise ModelComparisonError(f"missing measured dimension: {name}.")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ModelComparisonError(f"{name} must be a sequence of measured run values.")
    return values


def score_evaluation(
    model: ModelDescriptor,
    task: EvalTask,
    config: EvalConfig,
    dimensions: Mapping[str, Sequence[float]],
    *,
    inputs_digest: str | None = None,
    hardware: HardwareProfile | None = None,
) -> EvalScore:
    """Validate one executor-produced result set into a comparable score.

    ``dimensions`` maps each rubric dimension name to the values measured
    across repeated runs; the medians, spreads, and weighted overall are
    computed inside the value objects, not trusted from callers.
    Compatibility reasons are recorded rather than raised: an incompatible
    model may still hold a quality score, and comparison hard-filters it.
    """
    if not isinstance(model, ModelDescriptor):
        raise TypeError("model must be a ModelDescriptor value.")
    if not isinstance(task, EvalTask):
        raise TypeError("task must be an EvalTask value.")
    if not isinstance(config, EvalConfig):
        raise TypeError("config must be an EvalConfig value.")
    if isinstance(dimensions, (str, bytes)) or not isinstance(dimensions, Mapping):
        raise TypeError("dimensions must be an object of dimension name to run values.")
    unknown = sorted(set(dimensions) - set(task.dimension_names()))
    if unknown:
        raise ModelComparisonError(f"dimensions not in the task rubric: {', '.join(unknown)}.")
    results = tuple(
        DimensionScore(name=dimension.name, runs=tuple(_require_values(dimensions, dimension.name)))
        for dimension in task.rubric
    )
    reasons = CapabilityNegotiator(hardware).evaluate(model, task.requirements)
    return EvalScore(
        model=model,
        task=task,
        config=config,
        dimensions=results,
        compatibility=tuple(reasons),
        inputs_digest=inputs_digest,
    )


@dataclass(frozen=True, slots=True)
class ComparisonEntry:
    """One ranked, compatible evaluation in the comparison."""

    rank: int
    score: EvalScore

    def to_mapping(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "model_id": self.score.model.model_id,
            "digest": self.score.model.digest,
            "overall": self.score.overall,
            "dimensions": [item.to_mapping() for item in self.score.dimensions],
            "score_digest": self.score.digest,
        }


@dataclass(frozen=True, slots=True)
class ExcludedEvaluation:
    """A scored model filtered out before ranking, with its exact reasons."""

    model_id: str
    digest: str
    reasons: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "digest": self.digest, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class EnvironmentDifference:
    """One environment field whose values differ across the compared runs."""

    key: str
    values: tuple[tuple[str, str], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"key": self.key, "values": [[model_ref, value] for model_ref, value in self.values]}


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Deterministic ranking over mutually compatible evaluations only."""

    task_digest: str
    config_digest: str
    ranked: tuple[ComparisonEntry, ...]
    excluded: tuple[ExcludedEvaluation, ...]
    environment_differences: tuple[EnvironmentDifference, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "ranked": [entry.to_mapping() for entry in self.ranked],
            "excluded": [item.to_mapping() for item in self.excluded],
            "environment_differences": [item.to_mapping() for item in self.environment_differences],
        }


def compare_evaluations(
    scores: Sequence[EvalScore],
    *,
    hardware: HardwareProfile | None = None,
) -> ComparisonResult:
    """Rank models on one task version and config, filtering by compatibility."""
    if isinstance(scores, (str, bytes)) or not isinstance(scores, Sequence) or not scores:
        raise ModelComparisonError("scores must be a non-empty sequence of EvalScore values.")
    for score in scores:
        if not isinstance(score, EvalScore):
            raise TypeError("scores must contain EvalScore values.")
    task_digests = {score.task.digest for score in scores}
    if len(task_digests) > 1:
        raise ModelComparisonError("scores reference different task versions; only compatible tasks are comparable.")
    config_digests = {score.config.digest for score in scores}
    if len(config_digests) > 1:
        raise ModelComparisonError(
            f"scores were produced under {len(config_digests)} distinct configs; a comparison uses one reproducible config."
        )
    identities = [(score.model.model_id, score.model.digest) for score in scores]
    if len(set(identities)) != len(identities):
        raise ModelComparisonError("duplicate model identity in comparison; one score per model is required.")

    task = scores[0].task
    negotiator = CapabilityNegotiator(hardware)
    ranked_scores: list[EvalScore] = []
    excluded: list[ExcludedEvaluation] = []
    for score in scores:
        reasons = sorted({*negotiator.evaluate(score.model, task.requirements), *score.compatibility})
        if reasons:
            excluded.append(ExcludedEvaluation(score.model.model_id, score.model.digest, tuple(reasons)))
        else:
            ranked_scores.append(score)

    ordered = sorted(ranked_scores, key=lambda item: (-item.overall, item.model.model_id, item.model.digest))
    entries = tuple(ComparisonEntry(rank + 1, score) for rank, score in enumerate(ordered))

    per_key: dict[str, dict[str, str]] = {}
    for score in scores:
        reference = f"{score.model.model_id}@{score.model.digest}"
        for key, value in sorted(dict(score.config.environment).items()):
            per_key.setdefault(key, {})[reference] = value
    differences = tuple(
        EnvironmentDifference(key=key, values=tuple(sorted(seen.items())))
        for key, seen in sorted(per_key.items())
        if len(set(seen.values())) > 1
    )

    return ComparisonResult(
        task_digest=task.digest,
        config_digest=scores[0].config.digest,
        ranked=entries,
        excluded=tuple(excluded),
        environment_differences=differences,
    )
