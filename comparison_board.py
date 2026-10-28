"""Comparison board view-model for the model comparison and benchmark UI.

AS-061 assembles what the studio surface shows when a user puts candidate
models side by side: one row per model with four columns - quality (AS-056
comparison), latency and memory (AS-060 vault profiles, pinned to a shared
hardware/OS/methodology context), and cost (caller-supplied observations;
the board never computes prices) - and it carries the honesty of the
underlying layers all the way to the pixel. Every cell states exactly one of
``measured``, ``missing``, ``stale``, ``incomparable`` or ``excluded`` with a
reason, so an absent benchmark reads as "no measurement on record" instead
of an implied zero, and a score or profile pinned to a superseded build is
flagged rather than silently presented as current.

The module is presentation plumbing, not policy: it runs no evaluations,
touches no files, and trusts nothing it did not check. It re-verifies that a
performance entry agrees with its own key and that every entry shares the
context anchor before calling numbers comparable, and the whole board is a
deterministic content-addressed mapping (no wall-clock), so two machines
with the same inputs render byte-identical boards.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from model_calibration import ModelPerformanceProfile
from model_comparison import ComparisonResult
from model_registry import ModelDescriptor
from performance_profiles import ProfileKey, ProfileRecord

BOARD_SCHEMA_VERSION = 1
BOARD_METRICS = ("quality", "latency", "memory", "cost")
CELL_STATES = ("measured", "missing", "stale", "incomparable", "excluded")
STALE_VERDICTS = frozenset({"superseded", "invalidated"})
VALID_VERDICTS = frozenset({"active", "missing", "superseded", "observed", "invalidated", "unknown"})
_TIMING_METRICS = ("ttft_ms", "prefill_tokens_per_s", "decode_tokens_per_s")
_MEMORY_METRIC = "peak_memory_bytes"
_CURRENCY = re.compile(r"[A-Z]{3}")
_MAX_NOTE = 300


class ComparisonBoardError(ValueError):
    """Raised when board inputs violate the comparison contract."""


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bounded_note(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_NOTE:
        collapsed = collapsed[: _MAX_NOTE - 1] + "…"
    return collapsed


@dataclass(frozen=True, slots=True)
class CostObservation:
    """One caller-supplied price point; the board neither derives nor judges it."""

    amount_per_million_tokens: float
    currency: str
    source: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_per_million_tokens, bool) or not isinstance(self.amount_per_million_tokens, (int, float)):
            raise TypeError("amount_per_million_tokens must be a number.")
        amount = float(self.amount_per_million_tokens)
        if math.isnan(amount) or math.isinf(amount) or amount < 0:
            raise ComparisonBoardError("amount_per_million_tokens must be a finite non-negative number.")
        object.__setattr__(self, "amount_per_million_tokens", round(amount, 6))
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ComparisonBoardError("currency must be a three-letter uppercase code.")
        if not isinstance(self.source, str):
            raise TypeError("source must be a string.")
        source = " ".join(self.source.split())
        if not source:
            raise ComparisonBoardError("source must not be empty.")
        object.__setattr__(self, "source", source[:128])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": BOARD_SCHEMA_VERSION,
            "amount_per_million_tokens": self.amount_per_million_tokens,
            "currency": self.currency,
            "source": self.source,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class BoardCell:
    """One metric column of one model row, with its honest state."""

    metric: str
    state: str
    summary: dict[str, Any] | None = None
    source_digest: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.metric not in BOARD_METRICS:
            raise ComparisonBoardError(f"unknown board metric: {self.metric}.")
        if self.state not in CELL_STATES:
            raise ComparisonBoardError(f"unknown cell state: {self.state}.")
        if self.note is not None:
            object.__setattr__(self, "note", _bounded_note(self.note))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "state": self.state,
            "summary": self.summary,
            "source_digest": self.source_digest,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One candidate model with its four comparison cells."""

    model_id: str
    digest: str
    rank: int | None
    verdict: str | None
    cells: tuple[BoardCell, ...]
    excluded_reasons: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return self.rank is not None

    def cell(self, metric: str) -> BoardCell:
        if metric not in BOARD_METRICS:
            raise ComparisonBoardError(f"unknown board metric: {metric}.")
        return next(item for item in self.cells if item.metric == metric)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "digest": self.digest,
            "rank": self.rank,
            "verdict": self.verdict,
            "excluded_reasons": list(self.excluded_reasons),
            "cells": [item.to_mapping() for item in self.cells],
        }


@dataclass(frozen=True, slots=True)
class ComparisonBoard:
    """Deterministic, content-addressed view-model over one board render."""

    rows: tuple[BoardRow, ...]
    task_digest: str | None = None
    config_digest: str | None = None
    disclosures: tuple[str, ...] = ()

    def row(self, model_id: str, digest: str | None = None) -> BoardRow:
        for item in self.rows:
            if item.model_id == model_id and (digest is None or item.digest == digest):
                return item
        raise ComparisonBoardError(f"model {model_id!r} is not on this board.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": BOARD_SCHEMA_VERSION,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "disclosures": list(self.disclosures),
            "rows": [item.to_mapping() for item in self.rows],
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_mapping())


def _hardware_digest(hardware: Mapping[str, Any] | None) -> str:
    return _canonical_digest(dict(hardware)) if hardware is not None else _canonical_digest({})


def _profile_agrees_with_key(key: ProfileKey, profile: ModelPerformanceProfile) -> bool:
    inputs = profile.inputs
    return (
        key.hardware_digest == _hardware_digest(inputs.hardware)
        and key.model_digest == inputs.model_digest
        and key.quantization == (inputs.quantization if inputs.quantization is not None else "unspecified")
        and key.runtime_version == (inputs.runtime_version if inputs.runtime_version is not None else "unspecified")
    )


def _aggregates_summary(profile: ModelPerformanceProfile, metrics: Sequence[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for kind in ("warm", "cold"):
        block: dict[str, Any] = {}
        for metric in metrics:
            aggregate = profile.aggregate(metric, kind)
            block[metric] = {"median": aggregate.median, "spread": aggregate.spread, "count": aggregate.count}
        summary[kind] = block
    return summary


def _total_count(profile: ModelPerformanceProfile, metrics: Sequence[str]) -> int:
    return sum(profile.aggregate(metric, kind).count for kind in ("warm", "cold") for metric in metrics)


def _lookup_ranked(comparison: ComparisonResult, model_id: str) -> Any:
    for entry in comparison.ranked:
        if entry.score.model.model_id == model_id:
            return entry
    return None


def _quality_cell(candidate: ModelDescriptor, comparison: ComparisonResult | None, verdict: str | None) -> BoardCell:
    if comparison is None:
        return BoardCell("quality", "missing", note="no evaluation scores on record")
    scored = _lookup_ranked(comparison, candidate.model_id)
    if scored is not None:
        summary = {
            "rank": scored.rank,
            "overall": scored.score.overall,
            "dimensions": [item.to_mapping() for item in scored.score.dimensions],
            "task_digest": comparison.task_digest,
            "config_digest": comparison.config_digest,
        }
        source = scored.score.digest
        if scored.score.model.digest != candidate.digest:
            return BoardCell(
                "quality", "stale", summary=summary, source_digest=source,
                note="score was recorded against a different build of this model id",
            )
        if verdict in STALE_VERDICTS:
            return BoardCell("quality", "stale", summary=summary, source_digest=source, note=f"revision {verdict}")
        return BoardCell("quality", "measured", summary=summary, source_digest=source)
    for item in comparison.excluded:
        if item.model_id == candidate.model_id:
            note = "excluded from ranking: " + "; ".join(item.reasons)
            if item.digest != candidate.digest:
                note += " (recorded against a different build)"
            return BoardCell("quality", "excluded", note=note)
    return BoardCell("quality", "missing", note="no score for this model in the comparison")


def _performance_cell(
    candidate: ModelDescriptor,
    metric: str,
    entry: tuple[ProfileKey, ProfileRecord] | None,
    anchor: ProfileKey | None,
    verdict: str | None,
) -> BoardCell:
    if entry is None:
        return BoardCell(metric, "missing", note="no calibration profile on record for this model")
    key, record = entry
    profile = record.profile
    source = profile.digest
    if key.model_digest != candidate.digest:
        return BoardCell(metric, "stale", source_digest=source, note="profile was pinned to a different model digest than the candidate")
    if anchor is not None:
        for field in ("hardware_digest", "os_id", "methodology_digest"):
            if getattr(key, field) != getattr(anchor, field):
                label = field.removesuffix("_digest")
                return BoardCell(
                    metric, "incomparable", source_digest=source,
                    note=f"measured under a different {label} than the board's first profiled model",
                )
    metrics = _TIMING_METRICS if metric == "latency" else (_MEMORY_METRIC,)
    if _total_count(profile, metrics) == 0:
        return BoardCell(
            metric, "missing", source_digest=source,
            note=f"profile carries no successful {metric} samples (failure rate {profile.failure_rate})",
        )
    summary = _aggregates_summary(profile, metrics)
    summary["profile_version"] = record.version
    summary["inputs_digest"] = profile.inputs.digest
    summary["failure_rate"] = profile.failure_rate
    if verdict in STALE_VERDICTS:
        return BoardCell(metric, "stale", summary=summary, source_digest=source, note=f"revision {verdict}")
    return BoardCell(metric, "measured", summary=summary, source_digest=source)


def _cost_cell(model_id: str, costs: Mapping[str, CostObservation] | None, verdict: str | None) -> BoardCell:
    if costs is None or model_id not in costs:
        return BoardCell("cost", "missing", note="no cost observation on record")
    observation = costs[model_id]
    summary = {
        "amount_per_million_tokens": observation.amount_per_million_tokens,
        "currency": observation.currency,
        "source": observation.source,
    }
    if verdict in STALE_VERDICTS:
        return BoardCell("cost", "stale", summary=summary, source_digest=observation.digest, note=f"revision {verdict}")
    return BoardCell("cost", "measured", summary=summary, source_digest=observation.digest)


def build_board(
    candidates: Sequence[ModelDescriptor],
    *,
    comparison: ComparisonResult | None = None,
    performance: Mapping[str, tuple[ProfileKey, ProfileRecord]] | None = None,
    costs: Mapping[str, CostObservation] | None = None,
    staleness: Mapping[str, str] | None = None,
) -> ComparisonBoard:
    """Assemble the four-column comparison board for an ordered candidate list.

    ``performance`` maps a candidate's ``model_id`` to the ``(ProfileKey,
    ProfileRecord)`` pair as loaded from the AS-060 vault; the board
    re-checks key/profile agreement and treats the first profiled
    candidate's hardware, OS and methodology as the comparability anchor.
    ``staleness`` maps ``model_id`` to a verdict from the AS-057 ledger's
    ``status_for``; ``superseded`` and ``invalidated`` flip measured cells to
    ``stale`` while keeping the (faded) numbers available to the UI.
    """
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence) or not candidates:
        raise ComparisonBoardError("candidates must be a non-empty sequence of ModelDescriptor values.")
    for item in candidates:
        if not isinstance(item, ModelDescriptor):
            raise TypeError("candidates must contain ModelDescriptor values.")
    identities = [(item.model_id, item.digest) for item in candidates]
    if len(set(identities)) != len(identities):
        raise ComparisonBoardError("duplicate candidate identity on the board.")
    candidate_ids = {item.model_id for item in candidates}

    if comparison is not None and not isinstance(comparison, ComparisonResult):
        raise TypeError("comparison must be a ComparisonResult value.")
    if performance is not None:
        if not isinstance(performance, Mapping):
            raise TypeError("performance must be a mapping from candidate model_id to its entry.")
        for model_id, value in performance.items():
            if model_id not in candidate_ids:
                raise ComparisonBoardError(f"performance references {model_id!r}, which is not a candidate on this board.")
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
                raise ComparisonBoardError("performance entries must be (ProfileKey, ProfileRecord) pairs.")
            key, record = value
            if not isinstance(key, ProfileKey) or not isinstance(record, ProfileRecord):
                raise TypeError("performance entries must contain ProfileKey and ProfileRecord values.")
            if not _profile_agrees_with_key(key, record.profile):
                raise ComparisonBoardError(f"performance profile for {model_id!r} does not agree with its key.")
    if costs is not None:
        if not isinstance(costs, Mapping):
            raise TypeError("costs must be a mapping from candidate model_id to its observation.")
        for cost_id, cost_value in costs.items():
            if cost_id not in candidate_ids:
                raise ComparisonBoardError(f"costs references {cost_id!r}, which is not a candidate on this board.")
            if not isinstance(cost_value, CostObservation):
                raise TypeError("cost entries must be CostObservation values.")
    if staleness is not None:
        if not isinstance(staleness, Mapping):
            raise TypeError("staleness must be a mapping from candidate model_id to a verdict.")
        for stale_id, stale_verdict in staleness.items():
            if stale_id not in candidate_ids:
                raise ComparisonBoardError(f"staleness references {stale_id!r}, which is not a candidate on this board.")
            if stale_verdict not in VALID_VERDICTS:
                accepted = ", ".join(sorted(VALID_VERDICTS))
                raise ComparisonBoardError(f"staleness verdict for {stale_id!r} must be one of: {accepted}.")

    anchor: ProfileKey | None = None
    if performance is not None:
        for item in candidates:
            entry = performance.get(item.model_id)
            if entry is not None:
                anchor = entry[0]
                break

    rows: list[BoardRow] = []
    for item in candidates:
        verdict = staleness.get(item.model_id, "unknown") if staleness is not None else None
        entry = performance.get(item.model_id) if performance is not None else None
        quality = _quality_cell(item, comparison, verdict)
        latency = _performance_cell(item, "latency", entry, anchor, verdict)
        memory = _performance_cell(item, "memory", entry, anchor, verdict)
        cost = _cost_cell(item.model_id, costs, verdict)
        ranked = _lookup_ranked(comparison, item.model_id) if comparison is not None else None
        rank = ranked.rank if ranked is not None and ranked.score.model.digest == item.digest else None
        excluded = tuple(
            reason
            for one in (comparison.excluded if comparison is not None else ())
            if one.model_id == item.model_id
            for reason in one.reasons
        )
        rows.append(
            BoardRow(
                model_id=item.model_id,
                digest=item.digest,
                rank=rank,
                verdict=verdict,
                cells=(quality, latency, memory, cost),
                excluded_reasons=excluded,
            )
        )

    disclosures: list[str] = []
    if comparison is not None:
        for difference in comparison.environment_differences:
            rendered = "; ".join(f"{model_ref}={value}" for model_ref, value in difference.values)
            disclosures.append(_bounded_note(f"environment differs on {difference.key}: {rendered}"))
        scored_ids = {one.score.model.model_id for one in comparison.ranked} | {one.model_id for one in comparison.excluded}
        outside = sorted(scored_ids - candidate_ids)
        if outside:
            disclosures.append(_bounded_note("comparison covers models outside the candidate list: " + ", ".join(outside)))

    return ComparisonBoard(
        rows=tuple(rows),
        task_digest=comparison.task_digest if comparison is not None else None,
        config_digest=comparison.config_digest if comparison is not None else None,
        disclosures=tuple(disclosures),
    )


__all__ = [
    "BOARD_METRICS",
    "BOARD_SCHEMA_VERSION",
    "CELL_STATES",
    "BoardCell",
    "BoardRow",
    "ComparisonBoard",
    "ComparisonBoardError",
    "CostObservation",
    "build_board",
]
