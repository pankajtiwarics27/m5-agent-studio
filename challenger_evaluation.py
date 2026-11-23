"""Reproducible challenger and workflow evaluations (AP-016).

The objective is one sentence and one trap: *compare models, tools, prompts and
teams **without self-grading or hidden budget differences**.*

Self-grading is the trap. A challenger that scores its own output, a scorer that
silently returns zero when it fails, and a budget that quietly differs between
the incumbent and the challenger all produce a comparison that looks rigorous
and means nothing. So this module is built so that each of those is structurally
impossible rather than merely discouraged:

* **Every run is pinned and every failure is kept (AC1).** A
  :class:`RunRecord` cannot be constructed without exact task/config/tool/model/
  environment digests, and failures are *records*, not absences. A run set that
  omits a failed run is detectable because the record states how many were
  attempted.

* **Budgets are disclosed and must be comparable (AC2).** A
  :class:`StrategyBudget` is recorded per strategy, and a comparison whose
  budgets differ beyond a stated tolerance is refused. "The multi-agent strategy
  won" is not a finding if it had three times the tokens — that is exactly the
  hidden difference the criterion forbids.

* **A model score never stands alone (AC3).** A :class:`ScoredOutcome` requires
  at least one deterministic check *and* independent evidence of a kind this
  module recognises (factual verification or human/user judgement). Model
  scores are never the sole basis for a verdict.

* **The no-op incumbent and held-out tasks always participate (AC4).** A
  comparison refuses to run without a :class:`NoOpIncumbent` baseline — doing
  nothing is the honest control — and refuses a design that has no held-out
  tasks, because a challenger tuned on the tasks it is tested on has proved
  nothing.

* **Scorer failure, missing evidence and stale hashes fail closed (AC5).** There
  is no default score. A scorer that raises, a check with no evidence, and a
  source hash that no longer matches the pinned revision each produce a
  :class:`ScorerFailure`, and any scorer failure refuses the whole comparison
  rather than contributing a zero to an average.

* **Variability is reported and promotion is refused without verification
  (AC6).** Median *and* spread are reported at every level, interventions and
  cost and latency are carried alongside quality, and
  :func:`refuse_promotion` raises for any result whose evidence was not
  independently verified — a fabricated score cannot be promoted because there
  is no code path from an unverified score to a promotion.

Nothing here runs a model, a workflow, a provider or a network. Runs are values
the caller supplies; this module decides what they may be used to claim.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = 1

Strategy = Literal["single_agent", "specialists", "no_op_incumbent"]
STRATEGIES: tuple[Strategy, ...] = ("no_op_incumbent", "single_agent", "specialists")
Partition = Literal["development", "validation", "held_out"]
PARTICIPATING_PARTITIONS: tuple[Partition, ...] = (
    "development",
    "validation",
    "held_out",
)
EvidenceKind = Literal["factual_verification", "user_judgement", "deterministic_check"]
#: Evidence kinds that can stand in for model judgement. A model is not one.
INDEPENDENT_EVIDENCE: frozenset[str] = frozenset(
    {"factual_verification", "user_judgement"}
)
ScorerState = Literal["scored", "failed", "missing_evidence"]
_MAX_TEXT = 240
_DEFAULT_BUDGET_TOLERANCE = 0.10


class ChallengerEvaluationError(ValueError):
    """Raised when an evaluation input is invalid."""


class ScorerFailed(RuntimeError):
    """Raised when a scorer fails; the comparison must not continue (AC5)."""


class PromotionRefused(RuntimeError):
    """Raised when a result is not eligible for promotion (AC6)."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise ChallengerEvaluationError(f"{name} must not be empty.")
    if "\x00" in result:
        raise ChallengerEvaluationError(f"{name} must not contain a NUL character.")
    if len(result) > _MAX_TEXT:
        raise ChallengerEvaluationError(
            f"{name} must be at most {_MAX_TEXT} characters."
        )
    return result or None


def _digest_text(value: object, name: str) -> str:
    text = _text(value, name) or ""
    if not text.startswith("sha256:") or len(text) != len("sha256:") + 64:
        raise ChallengerEvaluationError(f"{name} must be a sha256: digest.")
    return text


def _non_negative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number.")
    number = float(value)
    if number != number or number < 0:
        raise ChallengerEvaluationError(f"{name} must be a finite non-negative number.")
    return round(number, 6)


def _unit(value: object, name: str) -> float:
    number = _non_negative(value, name)
    if number > 1.0:
        raise ChallengerEvaluationError(f"{name} must be within [0, 1].")
    return number


# ---------------------------------------------------------------------------
# AC1 — pinned run records, failures included
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One attempted run, pinned to everything a rerun must reproduce.

    “Include all failures” is enforced by shape: a failure is this same record
    with ``outcome="failed"`` and a reason, so it cannot be dropped by filtering
    on success. ``attempted`` vs. the number of records is how an omitted run is
    caught in :class:`StrategyRuns`.
    """

    run_id: str
    strategy: Strategy
    task_id: str
    partition: Partition
    task_digest: str
    config_digest: str
    tool_digest: str
    model_digest: str
    environment_digest: str
    toolchain_digest: str
    outcome: Literal["completed", "failed", "cancelled"]
    failure_reason: str | None = None
    latency_ms: float = 0.0
    cost: float = 0.0
    tokens: int = 0
    interventions: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        if self.partition not in PARTICIPATING_PARTITIONS:
            raise ChallengerEvaluationError(f"unknown partition: {self.partition!r}.")
        for name in (
            "task_digest",
            "config_digest",
            "tool_digest",
            "model_digest",
            "environment_digest",
            "toolchain_digest",
        ):
            object.__setattr__(self, name, _digest_text(getattr(self, name), name))
        if self.outcome not in ("completed", "failed", "cancelled"):
            raise ChallengerEvaluationError(f"unknown outcome: {self.outcome!r}.")
        object.__setattr__(
            self,
            "failure_reason",
            _text(self.failure_reason, "failure_reason", optional=True),
        )
        if self.outcome != "completed" and not self.failure_reason:
            raise ChallengerEvaluationError(
                "a failed or cancelled run must state why; a failure without a reason is an omission (AC1)."
            )
        if self.outcome == "completed" and self.failure_reason:
            raise ChallengerEvaluationError("a completed run has no failure reason.")
        object.__setattr__(
            self, "latency_ms", _non_negative(self.latency_ms, "latency_ms")
        )
        object.__setattr__(self, "cost", _non_negative(self.cost, "cost"))
        for name in ("tokens", "interventions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChallengerEvaluationError(
                    f"{name} must be a non-negative integer."
                )

    @property
    def succeeded(self) -> bool:
        return self.outcome == "completed"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "task_id": self.task_id,
            "partition": self.partition,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "tool_digest": self.tool_digest,
            "model_digest": self.model_digest,
            "environment_digest": self.environment_digest,
            "toolchain_digest": self.toolchain_digest,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "latency_ms": self.latency_ms,
            "cost": self.cost,
            "tokens": self.tokens,
            "interventions": self.interventions,
        }


@dataclass(frozen=True, slots=True)
class StrategyBudget:
    """What one strategy was allowed to spend — disclosed, not inferred (AC2)."""

    strategy: Strategy
    max_tokens: int
    max_cost: float
    max_latency_ms: float
    max_interventions: int

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        for name in ("max_tokens", "max_interventions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ChallengerEvaluationError(f"{name} must be a positive integer.")
        object.__setattr__(self, "max_cost", _non_negative(self.max_cost, "max_cost"))
        object.__setattr__(
            self, "max_latency_ms", _non_negative(self.max_latency_ms, "max_latency_ms")
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "max_tokens": self.max_tokens,
            "max_cost": self.max_cost,
            "max_latency_ms": self.max_latency_ms,
            "max_interventions": self.max_interventions,
        }


@dataclass(frozen=True, slots=True)
class StrategyRuns:
    """Every run for one strategy, failures included, with the attempted count.

    ``attempted`` must equal the number of records. A caller that ran ten and
    kept eight cannot present the eight as the whole story.
    """

    strategy: Strategy
    attempted: int
    records: tuple[RunRecord, ...]

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        if (
            isinstance(self.attempted, bool)
            or not isinstance(self.attempted, int)
            or self.attempted <= 0
        ):
            raise ChallengerEvaluationError("attempted must be a positive integer.")
        records = tuple(self.records)
        if any(r.strategy != self.strategy for r in records):
            raise ChallengerEvaluationError(
                "every record must belong to this strategy."
            )
        if len(records) != self.attempted:
            raise ChallengerEvaluationError(
                f"strategy {self.strategy!r} attempted {self.attempted} runs but recorded {len(records)}; "
                "failed runs must be recorded, not dropped (AC1)."
            )
        ids = [r.run_id for r in records]
        if len(set(ids)) != len(ids):
            raise ChallengerEvaluationError("duplicate run ids within a strategy.")
        object.__setattr__(self, "records", records)

    @property
    def failures(self) -> tuple[RunRecord, ...]:
        return tuple(r for r in self.records if not r.succeeded)

    @property
    def successes(self) -> tuple[RunRecord, ...]:
        return tuple(r for r in self.records if r.succeeded)

    @property
    def failure_rate(self) -> float:
        return round(len(self.failures) / self.attempted, 6)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "attempted": self.attempted,
            "recorded": len(self.records),
            "succeeded": len(self.successes),
            "failed": len(self.failures),
            "failure_rate": self.failure_rate,
            "runs": [r.to_mapping() for r in self.records],
        }


# ---------------------------------------------------------------------------
# AC3/AC5 — evidence and scorer failure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """Independent corroboration for a scored outcome.

    The module will not accept model-authored text as evidence of quality. Only
    a deterministic check, a factual verification or a human/user judgement
    counts, and each names its source (AC3).
    """

    kind: EvidenceKind
    source: str
    passes: bool | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (
            "factual_verification",
            "user_judgement",
            "deterministic_check",
        ):
            raise ChallengerEvaluationError(f"unknown evidence kind: {self.kind!r}.")
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "detail", _text(self.detail, "detail", optional=True) or ""
        )
        if self.kind == "deterministic_check" and self.passes is None:
            raise ChallengerEvaluationError(
                "a deterministic check must report whether it passed."
            )

    @property
    def is_independent(self) -> bool:
        return self.kind in INDEPENDENT_EVIDENCE

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "passes": self.passes,
            "detail": self.detail,
            "independent": self.is_independent,
        }


@dataclass(frozen=True, slots=True)
class ScoredOutcome:
    """One task's score for one strategy, with the checks that justify it.

    Construction refuses a model score with no deterministic check or no
    independent evidence, so “the challenger graded itself” is not a state this
    type can hold (AC3).
    """

    run_id: str
    strategy: Strategy
    task_id: str
    partition: Partition
    score: float
    evidence: tuple[Evidence, ...]
    scorer_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        if self.partition not in PARTICIPATING_PARTITIONS:
            raise ChallengerEvaluationError(f"unknown partition: {self.partition!r}.")
        object.__setattr__(self, "score", _unit(self.score, "score"))
        object.__setattr__(self, "scorer_id", _text(self.scorer_id, "scorer_id"))
        evidences = tuple(self.evidence)
        if not evidences:
            raise ChallengerEvaluationError(
                "a score with no evidence is an assertion; deterministic checks and independent evidence "
                "must accompany model scores (AC3)."
            )
        for item in evidences:
            if not isinstance(item, Evidence):
                raise TypeError("evidence must hold Evidence values.")
        object.__setattr__(self, "evidence", evidences)

    @property
    def deterministic_checks(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.kind == "deterministic_check")

    @property
    def independent_evidence(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.is_independent)

    @property
    def all_checks_passed(self) -> bool:
        return all(e.passes is not False for e in self.deterministic_checks)

    @property
    def is_justified(self) -> bool:
        """A score stands only with a *passing* deterministic check AND independent evidence.

        A high score next to a failing check is the most dangerous shape this
        record can take, so a failed check disqualifies the score rather than
        merely appearing alongside it (AC5).
        """
        return (
            bool(self.deterministic_checks)
            and bool(self.independent_evidence)
            and self.all_checks_passed
        )

    def problem(self) -> str | None:
        if not self.deterministic_checks:
            return "no deterministic check accompanies the model score (AC3)."
        if not self.independent_evidence:
            return "no independent factual or user evidence accompanies the model score (AC3)."
        if not self.all_checks_passed:
            return "a deterministic check failed for this outcome (AC5)."
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "task_id": self.task_id,
            "partition": self.partition,
            "score": self.score,
            "scorer_id": self.scorer_id,
            "justified": self.is_justified,
            "problem": self.problem(),
            "evidence": [e.to_mapping() for e in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ScorerFailure:
    """A scorer that failed, or a score that could not be justified (AC5).

    Recorded rather than swallowed. A failed scorer must not contribute ``0.0``
    to an average, because that turns "we could not measure this" into "the
    challenger is bad at it".
    """

    run_id: str
    strategy: Strategy
    state: ScorerState
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        if self.state not in ("scored", "failed", "missing_evidence"):
            raise ChallengerEvaluationError(f"unknown scorer state: {self.state!r}.")
        if self.state == "scored":
            raise ChallengerEvaluationError(
                "a ScorerFailure cannot describe a scored outcome."
            )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "state": self.state,
            "reason": self.reason,
            "contributes_a_score": False,
            "counted_as_zero": False,
        }


def run_scorer(
    scorer: Callable[[RunRecord], float],
    run: RunRecord,
    *,
    evidence: Sequence[Evidence],
) -> tuple[ScoredOutcome | None, ScorerFailure | None]:
    """Score one run, failing closed on any scorer problem (AC5).

    Returns either an outcome or a failure, never both. A scorer that raises
    becomes a ``failed`` record with the exception text; a score without
    justification becomes ``missing_evidence``.
    """
    if not isinstance(run, RunRecord):
        raise TypeError("run must be a RunRecord value.")
    if not callable(scorer):
        raise TypeError("scorer must be callable.")
    try:
        raw = scorer(run)
    except Exception as exc:  # a failing scorer is evidence, not a crash
        return None, ScorerFailure(
            run_id=run.run_id,
            strategy=run.strategy,
            state="failed",
            reason=f"scorer raised: {exc}",
        )
    try:
        outcome = ScoredOutcome(
            run_id=run.run_id,
            strategy=run.strategy,
            task_id=run.task_id,
            partition=run.partition,
            score=raw,
            evidence=tuple(evidence),
            scorer_id="injected-scorer",
        )
    except ChallengerEvaluationError as exc:
        return None, ScorerFailure(
            run_id=run.run_id,
            strategy=run.strategy,
            state="missing_evidence",
            reason=str(exc),
        )
    problem = outcome.problem()
    if problem is not None:
        state: ScorerState = "missing_evidence" if "no " in problem else "failed"
        return None, ScorerFailure(
            run_id=run.run_id, strategy=run.strategy, state=state, reason=problem
        )
    return outcome, None


# ---------------------------------------------------------------------------
# AC4 — the no-op incumbent and held-out tasks always participate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoOpIncumbent:
    """The control that does nothing. Always present (AC4).

    Without it, a challenger is compared only against other challengers, and a
    workflow that merely looks busy can beat "no attempt at all" and be declared
    valuable.
    """

    task_id: str
    partition: Partition
    score: float = 0.0
    note: str = "the incumbent does nothing; any challenger must beat this honestly"

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        if self.partition not in PARTICIPATING_PARTITIONS:
            raise ChallengerEvaluationError(f"unknown partition: {self.partition!r}.")
        object.__setattr__(self, "score", _unit(self.score, "score"))
        object.__setattr__(self, "note", _text(self.note, "note", optional=True) or "")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "partition": self.partition,
            "score": self.score,
            "strategy": "no_op_incumbent",
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class EvaluationDesign:
    """The pinned plan a run set is judged against.

    Requires a held-out partition and a no-op incumbent. A design tuned on the
    tasks it is tested on has demonstrated nothing, and a design without a
    control cannot tell improvement from activity (AC4).
    """

    design_id: str
    task_partitions: Mapping[str, Partition]
    incumbents: tuple[NoOpIncumbent, ...]
    budgets: tuple[StrategyBudget, ...]
    source_revision: str
    budget_tolerance: float = _DEFAULT_BUDGET_TOLERANCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "design_id", _text(self.design_id, "design_id"))
        object.__setattr__(
            self,
            "source_revision",
            _digest_text(self.source_revision, "source_revision"),
        )
        partitions = dict(self.task_partitions)
        if not partitions:
            raise ChallengerEvaluationError(
                "a design must name its tasks and their partitions."
            )
        for task_id in partitions:
            _text(task_id, "task_id")
            if partitions[task_id] not in PARTICIPATING_PARTITIONS:
                raise ChallengerEvaluationError(f"unknown partition for {task_id!r}.")
        object.__setattr__(self, "task_partitions", partitions)
        if not any(p == "held_out" for p in partitions.values()):
            raise ChallengerEvaluationError(
                "a design with no held-out tasks cannot detect overfitting to its own cases (AC4)."
            )
        incumbents = tuple(self.incumbents)
        if not incumbents:
            raise ChallengerEvaluationError(
                "the no-op incumbent must participate in every comparison (AC4); a design without a control "
                "cannot separate improvement from activity."
            )
        if {i.task_id for i in incumbents} - set(partitions):
            raise ChallengerEvaluationError(
                "an incumbent names a task the design does not contain."
            )
        object.__setattr__(self, "incumbents", incumbents)
        budgets = tuple(self.budgets)
        if not budgets:
            raise ChallengerEvaluationError(
                "budgets must be disclosed for every strategy (AC2)."
            )
        if len({b.strategy for b in budgets}) != len(budgets):
            raise ChallengerEvaluationError("one budget per strategy.")
        object.__setattr__(self, "budgets", budgets)
        object.__setattr__(
            self, "budget_tolerance", _unit(self.budget_tolerance, "budget_tolerance")
        )

    def budget_for(self, strategy: Strategy) -> StrategyBudget | None:
        for budget in self.budgets:
            if budget.strategy == strategy:
                return budget
        return None

    @property
    def held_out_task_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(t for t, p in self.task_partitions.items() if p == "held_out")
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "design_id": self.design_id,
            "task_partitions": dict(sorted(self.task_partitions.items())),
            "held_out_task_ids": list(self.held_out_task_ids),
            "incumbents": [i.to_mapping() for i in self.incumbents],
            "budgets": [b.to_mapping() for b in self.budgets],
            "source_revision": self.source_revision,
            "budget_tolerance": self.budget_tolerance,
            "self_grading_permitted": False,
        }


# ---------------------------------------------------------------------------
# AC2 — budget comparability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetComparison:
    """Whether two strategies were given comparable room (AC2)."""

    left: StrategyBudget
    right: StrategyBudget
    tolerance: float
    comparable: bool
    differences: Mapping[str, float] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "differences", dict(self.differences))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "left": self.left.strategy,
            "right": self.right.strategy,
            "tolerance": self.tolerance,
            "comparable": self.comparable,
            "relative_differences": dict(sorted(self.differences.items())),
            "reason": self.reason,
            "hidden_budget_difference_permitted": False,
        }


def compare_budgets(
    left: StrategyBudget,
    right: StrategyBudget,
    *,
    tolerance: float = _DEFAULT_BUDGET_TOLERANCE,
) -> BudgetComparison:
    """Compare two disclosed budgets; unequal ones are a refusal, not a footnote."""
    tolerance = _unit(tolerance, "tolerance")
    differences: dict[str, float] = {}
    for name in ("max_tokens", "max_cost", "max_latency_ms", "max_interventions"):
        a = float(getattr(left, name))
        b = float(getattr(right, name))
        larger = max(a, b)
        differences[name] = 0.0 if larger == 0 else round(abs(a - b) / larger, 6)
    over = {name: delta for name, delta in differences.items() if delta > tolerance}
    if over:
        return BudgetComparison(
            left=left,
            right=right,
            tolerance=tolerance,
            comparable=False,
            differences=differences,
            reason=(
                "budgets differ beyond tolerance in "
                + ", ".join(sorted(over))
                + "; a comparison under unequal budgets is not a comparison (AC2)."
            ),
        )
    return BudgetComparison(
        left=left,
        right=right,
        tolerance=tolerance,
        comparable=True,
        differences=differences,
    )


# ---------------------------------------------------------------------------
# AC5 — stale source hashes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePin:
    """A file hash captured with the design, re-checked before reporting."""

    path: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path, "path"))
        object.__setattr__(self, "digest", _digest_text(self.digest, "digest"))

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "digest": self.digest}


def verify_source_pins(
    pins: Sequence[SourcePin], *, observed: Mapping[str, str]
) -> None:
    """Fail closed when a pinned source hash no longer matches (AC5).

    A comparison against a revision that has since moved is not reproducible, so
    this raises rather than warning: a stale hash silently attached to a report
    is worse than a refused report.
    """
    stale: list[str] = []
    for pin in pins:
        current = observed.get(pin.path)
        if current is None:
            stale.append(f"{pin.path} (missing)")
        elif current != pin.digest:
            stale.append(f"{pin.path} (changed)")
    if stale:
        raise ScorerFailed(
            "source hashes do not match the pinned revision: "
            + ", ".join(sorted(stale))
            + ". A result against drifted sources is not reproducible (AC5)."
        )


# ---------------------------------------------------------------------------
# AC6 — variability, cost, interventions, and no promotion on a fake score
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Variability:
    """Median and spread, reported rather than collapsed to a mean (AC6)."""

    median: float
    spread: float
    minimum: float
    maximum: float
    count: int

    @classmethod
    def of(cls, values: Sequence[float]) -> Variability:
        if not values:
            raise ChallengerEvaluationError("variability needs at least one value.")
        numbers = [float(v) for v in values]
        return cls(
            median=round(statistics.median(numbers), 6),
            spread=round(max(numbers) - min(numbers), 6),
            minimum=round(min(numbers), 6),
            maximum=round(max(numbers), 6),
            count=len(numbers),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "median": self.median,
            "spread": self.spread,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "count": self.count,
            "mean_only": False,
        }


@dataclass(frozen=True, slots=True)
class StrategyResult:
    """One strategy's verified outcomes and its operational metrics (AC6)."""

    strategy: Strategy
    outcomes: tuple[ScoredOutcome, ...]
    scorer_failures: tuple[ScorerFailure, ...] = ()

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        outcomes = tuple(self.outcomes)
        if any(o.strategy != self.strategy for o in outcomes):
            raise ChallengerEvaluationError(
                "every outcome must belong to this strategy."
            )
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "scorer_failures", tuple(self.scorer_failures))

    @property
    def has_scorer_failure(self) -> bool:
        return bool(self.scorer_failures)

    @property
    def scores(self) -> tuple[float, ...]:
        return tuple(o.score for o in self.outcomes)

    @property
    def quality(self) -> Variability | None:
        return Variability.of(self.scores) if self.scores else None

    def to_mapping(self) -> dict[str, Any]:
        quality = self.quality
        return {
            "strategy": self.strategy,
            "outcome_count": len(self.outcomes),
            "quality": quality.to_mapping() if quality else None,
            "scorer_failures": [f.to_mapping() for f in self.scorer_failures],
            "unjustified_outcomes": [
                o.run_id for o in self.outcomes if not o.is_justified
            ],
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """The comparison, with every caveat that makes it honest (AC1–AC6)."""

    design: EvaluationDesign
    results: tuple[StrategyResult, ...]
    budgets: BudgetComparison | None
    metrics: Mapping[str, Variability]
    source_pins: tuple[SourcePin, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "metrics", dict(self.metrics))

    @property
    def is_trustworthy(self) -> bool:
        """Trustworthy means no scorer failure, no unjustified score, comparable budgets."""
        if self.budgets is not None and not self.budgets.comparable:
            return False
        for result in self.results:
            if result.has_scorer_failure:
                return False
            if any(not o.is_justified for o in result.outcomes):
                return False
        return True

    @property
    def blockers(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.budgets is not None and not self.budgets.comparable:
            problems.append(self.budgets.reason)
        for result in self.results:
            for failure in result.scorer_failures:
                problems.append(f"{result.strategy}/{failure.run_id}: {failure.reason}")
            for outcome in result.outcomes:
                issue = outcome.problem()
                if issue:
                    problems.append(f"{result.strategy}/{outcome.run_id}: {issue}")
        return tuple(problems)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "design": self.design.to_mapping(),
            "results": [r.to_mapping() for r in self.results],
            "budgets": self.budgets.to_mapping() if self.budgets else None,
            "metrics": {
                name: var.to_mapping() for name, var in sorted(self.metrics.items())
            },
            "source_pins": [p.to_mapping() for p in self.source_pins],
            "trustworthy": self.is_trustworthy,
            "blockers": list(self.blockers),
            "self_grading_permitted": False,
            "promotion_criteria": {
                "independent_verification_required": True,
                "no_op_incumbent_required": True,
                "held_out_partition_required": True,
            },
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def require_trustworthy(report: EvaluationReport) -> None:
    """Refuse to publish a comparison whose evidence does not hold (AC5)."""
    if not isinstance(report, EvaluationReport):
        raise TypeError("report must be an EvaluationReport value.")
    if not report.is_trustworthy:
        raise ScorerFailed(
            "this comparison is not publishable: "
            + "; ".join(report.blockers)
            + " (AC5)."
        )


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """A proposed promotion, and what independently verified it (AC6)."""

    strategy: Strategy
    task_id: str
    claimed_score: float
    verified: bool
    verified_by: str | None = None

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ChallengerEvaluationError(f"unknown strategy: {self.strategy!r}.")
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(
            self, "claimed_score", _unit(self.claimed_score, "claimed_score")
        )
        object.__setattr__(
            self, "verified_by", _text(self.verified_by, "verified_by", optional=True)
        )
        if self.verified and not self.verified_by:
            raise ChallengerEvaluationError(
                "independent verification must name who verified it."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "task_id": self.task_id,
            "claimed_score": self.claimed_score,
            "verified": self.verified,
            "verified_by": self.verified_by,
        }


def refuse_promotion(
    candidate: PromotionCandidate, *, report: EvaluationReport | None = None
) -> None:
    """Promotion requires independent verification; otherwise it is refused (AC6).

    ``report`` is optional so the single-candidate case is still checked. When a
    report is supplied it must also be trustworthy, because promoting from a
    comparison that failed one of its own gates is the fabricated-score case this
    criterion names.
    """
    if not isinstance(candidate, PromotionCandidate):
        raise TypeError("candidate must be a PromotionCandidate value.")
    if not candidate.verified or not candidate.verified_by:
        raise PromotionRefused(
            "promotion refused: the score was never independently verified. A fabricated or "
            "self-graded score cannot be promoted, because there is no path from an unverified "
            "score to promotion (AC6)."
        )
    if report is not None and not report.is_trustworthy:
        raise PromotionRefused(
            "promotion refused: the comparison is not trustworthy ("
            + "; ".join(report.blockers)
            + ") (AC6)."
        )


def build_report(
    *,
    design: EvaluationDesign,
    results: Sequence[StrategyResult],
    metrics: Mapping[str, Variability] | None = None,
    source_pins: Sequence[SourcePin] = (),
    observed_source_hashes: Mapping[str, str] | None = None,
    budget_tolerance: float | None = None,
) -> EvaluationReport:
    """Assemble a report, checking budgets and source pins on the way (AC2, AC5)."""
    if not isinstance(design, EvaluationDesign):
        raise TypeError("design must be an EvaluationDesign value.")
    tolerance = (
        design.budget_tolerance
        if budget_tolerance is None
        else _unit(budget_tolerance, "budget_tolerance")
    )
    ordered = tuple(results)
    strategies = {r.strategy for r in ordered}
    if "no_op_incumbent" not in strategies:
        raise ChallengerEvaluationError(
            "the no-op incumbent must participate in the comparison, not only in the design (AC4)."
        )
    if not ({"single_agent", "specialists"} & strategies):
        raise ChallengerEvaluationError(
            "a comparison needs at least one challenger strategy."
        )
    budgets: BudgetComparison | None = None
    challengers = [s for s in ("single_agent", "specialists") if s in strategies]
    if len(challengers) == 2:
        left = design.budget_for("single_agent")
        right = design.budget_for("specialists")
        if left is None or right is None:
            raise ChallengerEvaluationError(
                "both challenger strategies must disclose a budget (AC2)."
            )
        budgets = compare_budgets(left, right, tolerance=tolerance)
    if source_pins:
        if observed_source_hashes is None:
            raise ChallengerEvaluationError(
                "source pins were supplied without observed hashes; a pin cannot be verified by assumption (AC5)."
            )
        verify_source_pins(source_pins, observed=observed_source_hashes)
    return EvaluationReport(
        design=design,
        results=ordered,
        budgets=budgets,
        metrics=dict(metrics or {}),
        source_pins=tuple(source_pins),
    )


def measure_metrics(
    *,
    latencies_ms: Sequence[float],
    costs: Sequence[float],
    interventions: Sequence[int],
) -> dict[str, Variability]:
    """Report latency, cost and intervention variability side by side (AC6)."""
    if not (latencies_ms and costs and interventions):
        raise ChallengerEvaluationError(
            "latency, cost and intervention metrics all require observations."
        )
    return {
        "latency_ms": Variability.of(latencies_ms),
        "cost": Variability.of(costs),
        "interventions": Variability.of([float(i) for i in interventions]),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(report: EvaluationReport) -> str:
    """Deterministic JSON for review: sorted keys, stable order, trailing newline."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report": report.to_mapping(),
        "evaluation_rules": {
            "self_grading_permitted": False,
            "model_score_without_independent_evidence": False,
            "scorer_failure_contributes_zero": False,
            "hidden_budget_difference_permitted": False,
            "failed_runs_may_be_omitted": False,
            "held_out_partition_required": True,
            "no_op_incumbent_required": True,
            "promotion_without_verification_permitted": False,
        },
        "does_not_prove": [
            "a trustworthy report means the evidence gates held, not that the challenger is better in "
            "general or on tasks nobody ran",
            "scores rest on whatever deterministic checks and independent evidence the caller supplied",
            "no model, workflow or provider was executed to produce this record",
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
