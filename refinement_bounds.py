"""AP-027 — bound refinement, keep the incumbent, and know when to stop.

Refinement loops fail in three familiar ways, and all three look like effort.

* **The loop that cannot lose.** Ask a reviewer to "improve this" and it hands back something. Ask
  again and it hands back something else. Without the unchanged output on the ballot, every round
  produces a candidate by construction and the question "was this better?" is never asked. So the
  incumbent is a *candidate here*: :meth:`RefinementSession.apply` refuses a round whose ballot omits
  it, refuses a "revision" whose content is identical to it (that is the incumbent wearing a new
  name), and refuses a round that does not defend the currently accepted version.

* **The independent reviewer that is the same model.** A second pass by the same model on the same
  context is not a second opinion; it is the first opinion, restated. :class:`ReviewerContext` names
  the model family and the session a verdict came from. A verdict by the session that wrote the
  variant is refused as self-grading, a comparison needs at least two sessions, and every round
  *records* its bias state: when every reviewer shares the producer's model family the report says
  ``possible``, and it never says ``none`` — a single model cannot certify itself unbiased.

* **The improvement that traded away a fact.** Style scores are continuous, cheap and easy to move;
  a hard requirement is a yes or a no. Any ranking that mixes them lets a 0.99 style score outrank a
  failed safety check, so the two are separated: :class:`Requirement` is hard by *kind* (factual,
  functional, safety) rather than by a flag someone may forget to set, a variant that fails one is
  ineligible and its style score is not consulted, and a hard result cannot be recorded as passing
  without evidence.

Two further refusals. The iteration and reviewer budget is counted **across every reviewer and every
variant** and checked *before* a round is scored, so three reviewers judging four variants costs
twelve calls whether or not anyone thinks of it as one "iteration"; and when the work genuinely
cannot be done, :class:`Escalation` names the missing capability and the person who must decide, the
report's status becomes ``partial``, and :meth:`RefinementReport.claim_success` raises. A partial
result is an acceptable outcome; a fabricated success is not.

Nothing here reads a clock, calls a model or opens a socket. Rounds, scores and verdicts are values
the caller supplies; this module decides what they may be used to claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "RefinementError",
    "SelectionRefused",
    "ComparisonRefused",
    "EvidenceRefused",
    "BudgetSpent",
    "FabricatedSuccessRefused",
    "Requirement",
    "RequirementResult",
    "HARD_REQUIREMENT_KINDS",
    "REQUIREMENT_KINDS",
    "ReviewerContext",
    "Variant",
    "VARIANT_ORIGINS",
    "ReviewerVerdict",
    "RefinementLimits",
    "RefinementRound",
    "VariantOutcome",
    "RoundOutcome",
    "StopDecision",
    "STOP_KINDS",
    "Escalation",
    "BIAS_STATES",
    "REPORT_STATUSES",
    "RefinementSession",
    "RefinementReport",
]

RB_SCHEMA_VERSION = 1
RB_MAX_TEXT = 2000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HUMAN_RE = re.compile(r"^(owner|user|reviewer):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Requirement kinds. The first three are hard: they are pass/fail facts about the output, and no
#: amount of style can compensate for them.
HARD_REQUIREMENT_KINDS: tuple[str, ...] = ("factual", "functional", "safety")
REQUIREMENT_KINDS: tuple[str, ...] = tuple(HARD_REQUIREMENT_KINDS) + ("style",)

#: Where a ballot entry came from. ``incumbent`` is the unchanged output and is the only origin that
#: may carry it, which is what makes "did we actually change anything?" answerable.
VARIANT_ORIGINS: tuple[str, ...] = ("incumbent", "revision", "synthesis")

#: How a set of verdicts is judged for shared-model bias. There is deliberately no ``none``: a
#: comparison where an independent context exists is *mitigated*, not certified.
BIAS_STATES: tuple[str, ...] = ("possible", "mitigated_by_independent_context")

#: Reasons a refinement session stopped, or a reason it has not stopped yet.
STOP_KINDS: tuple[str, ...] = (
    "goal_met",
    "no_progress",
    "budget_exhausted",
    "rounds_exhausted",
    "capability_gap",
    "insufficient_evidence",
)

#: ``partial`` means something the caller asked for was not established. It is never a synonym for
#: failure, and it is never reported as ``delivered``.
REPORT_STATUSES: tuple[str, ...] = ("delivered", "partial")


class RefinementError(ValueError):
    """A refinement record is malformed, or claims something it cannot support."""


class SelectionRefused(RefinementError):
    """A ballot, a verdict or a ranking breaks the comparison contract."""


class ComparisonRefused(RefinementError):
    """A round cannot be judged: no incumbent on the ballot, or no independent context."""


class EvidenceRefused(RefinementError):
    """A hard requirement was reported as passing without the evidence that shows it."""


class BudgetSpent(RefinementError):
    """The bounded refinement budget is exhausted across reviewers or variants."""


class FabricatedSuccessRefused(RefinementError):
    """A partial result was presented as a delivered one."""


def _text(value: object, field_name: str, *, limit: int = RB_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise RefinementError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise RefinementError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise RefinementError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise RefinementError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _identifier(value: object, field_name: str) -> str:
    text = _text(value, field_name, limit=120)
    if not _ID_RE.fullmatch(text):
        raise RefinementError(f"{field_name} must be a bounded opaque identifier.")
    return text


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise RefinementError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _unit(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RefinementError(f"{field_name} must be a number between 0 and 1.")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise RefinementError(f"{field_name} must be a number between 0 and 1.")
    return number


def _count(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefinementError(f"{field_name} must be an integer.")
    if value < minimum:
        raise RefinementError(f"{field_name} must be at least {minimum}.")
    return value


# ---------------------------------------------------------------------------
# AC3 — hard requirements are hard by kind, and evidence is not optional.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requirement:
    """One thing the output must be, or must keep being.

    ``kind`` decides whether it is hard. There is no ``hard`` flag to set or forget: a factual,
    functional or safety requirement is a gate, a style requirement describes the rubric that
    :attr:`ReviewerVerdict.style_score` stands for, and neither can be re-classified by a caller.
    """

    requirement_id: str
    kind: str
    statement: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _identifier(self.requirement_id, "requirement_id"))
        if self.kind not in REQUIREMENT_KINDS:
            raise RefinementError(
                f"unknown requirement kind {self.kind!r}; a requirement is one of {list(REQUIREMENT_KINDS)}."
            )
        object.__setattr__(self, "statement", _text(self.statement, "requirement statement", limit=600))

    @property
    def is_hard(self) -> bool:
        return self.kind in HARD_REQUIREMENT_KINDS

    def to_mapping(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "statement": self.statement,
            "hard": self.is_hard,
        }


@dataclass(frozen=True, slots=True)
class RequirementResult:
    """A reviewer's verdict on one requirement, with the evidence for it.

    ``passed=True`` without evidence is refused: an unsupported pass is the exact shape a fabricated
    compliance report takes, and it is indistinguishable from a checked one once it is written down.
    A failed requirement needs no evidence -- the failure is its own finding.
    """

    requirement_id: str
    passed: bool
    evidence: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirement_id", _identifier(self.requirement_id, "requirement_id"))
        if not isinstance(self.passed, bool):
            raise RefinementError("passed must be a boolean.")
        evidence = str(self.evidence or "").strip()
        if self.passed and not evidence:
            raise EvidenceRefused(
                f"requirement {self.requirement_id!r} was reported as passing with no evidence; a pass "
                "that nothing supports is a claim, not a check."
            )
        object.__setattr__(self, "evidence", evidence[:RB_MAX_TEXT])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "passed": self.passed,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# AC1/AC2 — the ballot, and who is judging it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewerContext:
    """Where a verdict came from: which model family, which session, which rubric.

    ``session_id`` is what makes independence checkable. Two verdicts from one session are one
    opinion; a verdict from the session that authored the variant is self-grading.
    """

    model_family: str
    session_id: str
    rubric_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_family", _identifier(self.model_family, "model_family"))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session_id"))
        object.__setattr__(self, "rubric_digest", _digest(self.rubric_digest, "rubric_digest"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "session_id": self.session_id,
            "rubric_digest": self.rubric_digest,
        }


@dataclass(frozen=True, slots=True)
class Variant:
    """One entry on the ballot: the incumbent, a revision, or a synthesis.

    ``origin="incumbent"`` means "this is the output we already had, unchanged". A revision claiming
    the incumbent's digest is refused at the round, because a no-op dressed as a change is exactly
    what an unopposed refinement loop produces.
    """

    variant_id: str
    origin: str
    revision: int
    content_digest: str
    authored_by_session: str = "incumbent"

    def __post_init__(self) -> None:
        object.__setattr__(self, "variant_id", _identifier(self.variant_id, "variant_id"))
        if self.origin not in VARIANT_ORIGINS:
            raise RefinementError(f"unknown variant origin {self.origin!r}; expected one of {list(VARIANT_ORIGINS)}.")
        object.__setattr__(self, "revision", _count(self.revision, "revision"))
        if self.origin == "incumbent" and self.revision != 0:
            raise RefinementError("the incumbent is revision 0; it is the output nothing has changed yet.")
        if self.origin != "incumbent" and self.revision < 1:
            raise RefinementError("a revision or synthesis is revision 1 or later.")
        object.__setattr__(self, "content_digest", _digest(self.content_digest, "content_digest"))
        object.__setattr__(
            self, "authored_by_session", _identifier(self.authored_by_session, "authored_by_session")
        )

    @property
    def is_incumbent(self) -> bool:
        return self.origin == "incumbent"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "origin": self.origin,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "authored_by_session": self.authored_by_session,
        }


@dataclass(frozen=True, slots=True)
class ReviewerVerdict:
    """One reviewer's score for one variant, plus its per-requirement results."""

    reviewer_id: str
    variant_id: str
    context: ReviewerContext
    style_score: float
    requirement_results: tuple[RequirementResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewer_id", _identifier(self.reviewer_id, "reviewer_id"))
        object.__setattr__(self, "variant_id", _identifier(self.variant_id, "variant_id"))
        if not isinstance(self.context, ReviewerContext):
            raise RefinementError("a verdict needs the ReviewerContext it was produced in.")
        object.__setattr__(self, "style_score", _unit(self.style_score, "style_score"))
        results = tuple(self.requirement_results)
        for item in results:
            if not isinstance(item, RequirementResult):
                raise RefinementError("requirement_results must hold RequirementResult records.")
        seen = [item.requirement_id for item in results]
        if len(set(seen)) != len(seen):
            raise SelectionRefused("a reviewer reported two results for one requirement.")
        object.__setattr__(self, "requirement_results", results)

    def result_for(self, requirement_id: str) -> RequirementResult | None:
        for item in self.requirement_results:
            if item.requirement_id == requirement_id:
                return item
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "variant_id": self.variant_id,
            "context": self.context.to_mapping(),
            "style_score": self.style_score,
            "requirement_results": [item.to_mapping() for item in self.requirement_results],
        }


# ---------------------------------------------------------------------------
# AC4 — the bound, counted across every reviewer and every variant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefinementLimits:
    """Ceilings for a whole refinement session, not per reviewer and not per variant.

    ``min_improvement`` is the margin a new candidate must beat the accepted version by before it is
    an improvement. Without a margin, a score that moved by 0.001 because the rubric was re-read
    becomes "better", and the loop never stops moving.
    """

    max_rounds: int
    max_reviewer_calls: int
    max_tokens: int
    min_improvement: float = 0.02
    no_progress_rounds: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_rounds", _count(self.max_rounds, "max_rounds", minimum=1))
        object.__setattr__(
            self, "max_reviewer_calls", _count(self.max_reviewer_calls, "max_reviewer_calls", minimum=1)
        )
        object.__setattr__(self, "max_tokens", _count(self.max_tokens, "max_tokens", minimum=1))
        object.__setattr__(self, "min_improvement", _unit(self.min_improvement, "min_improvement"))
        object.__setattr__(
            self, "no_progress_rounds", _count(self.no_progress_rounds, "no_progress_rounds", minimum=1)
        )
        if self.no_progress_rounds > self.max_rounds:
            raise RefinementError(
                "a no-progress limit beyond the round limit can never trigger; the plateau rule would "
                "be dead code that reads like a safeguard."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "max_reviewer_calls": self.max_reviewer_calls,
            "max_tokens": self.max_tokens,
            "min_improvement": self.min_improvement,
            "no_progress_rounds": self.no_progress_rounds,
        }


@dataclass(frozen=True, slots=True)
class RefinementRound:
    """One round's ballot, verdicts and declared spend.

    ``tokens_spent`` is declared rather than measured here, because this module never calls a model;
    a round that under-declares its spend is a caller defect the budget cannot detect, which is
    recorded in the report's limitations rather than hidden.
    """

    round_index: int
    variants: tuple[Variant, ...]
    verdicts: tuple[ReviewerVerdict, ...]
    requirements: tuple[Requirement, ...]
    tokens_spent: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "round_index", _count(self.round_index, "round_index", minimum=1))
        variants = tuple(self.variants)
        for entry in variants:
            if not isinstance(entry, Variant):
                raise RefinementError("variants must hold Variant records.")
        ids = [entry.variant_id for entry in variants]
        if len(set(ids)) != len(ids):
            raise SelectionRefused("two ballot entries share a variant id.")
        object.__setattr__(self, "variants", variants)
        verdicts = tuple(self.verdicts)
        for scored in verdicts:
            if not isinstance(scored, ReviewerVerdict):
                raise RefinementError("verdicts must hold ReviewerVerdict records.")
        object.__setattr__(self, "verdicts", verdicts)
        requirements = tuple(self.requirements)
        for rule in requirements:
            if not isinstance(rule, Requirement):
                raise RefinementError("requirements must hold Requirement records.")
        seen = [rule.requirement_id for rule in requirements]
        if len(set(seen)) != len(seen):
            raise SelectionRefused("a requirement is declared twice in one round.")
        object.__setattr__(self, "requirements", requirements)
        object.__setattr__(self, "tokens_spent", _count(self.tokens_spent, "tokens_spent"))

    @property
    def hard_requirements(self) -> tuple[Requirement, ...]:
        return tuple(item for item in self.requirements if item.is_hard)

    def verdicts_for(self, variant_id: str) -> tuple[ReviewerVerdict, ...]:
        return tuple(item for item in self.verdicts if item.variant_id == variant_id)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "variants": [item.to_mapping() for item in self.variants],
            "verdicts": [item.to_mapping() for item in self.verdicts],
            "requirements": [item.to_mapping() for item in self.requirements],
            "tokens_spent": self.tokens_spent,
        }


# ---------------------------------------------------------------------------
# Outcomes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantOutcome:
    """What one ballot entry earned, and why it did or did not survive the gates."""

    variant_id: str
    eligible: bool
    style_score: float | None
    blocking_reasons: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "eligible": self.eligible,
            "style_score": self.style_score,
            "blocking_reasons": list(self.blocking_reasons),
        }


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """One judged round: the gates, the ranking, the bias state and what it cost."""

    round_index: int
    outcomes: tuple[VariantOutcome, ...]
    accepted_before: str
    accepted_after: str
    improved: bool
    reason: str
    shared_model_bias: str
    reviewer_calls: int
    tokens_spent: int
    accepted_eligible: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "variants": [item.to_mapping() for item in self.outcomes],
            "accepted_before": self.accepted_before,
            "accepted_after": self.accepted_after,
            "improved": self.improved,
            "reason": self.reason,
            "shared_model_bias": self.shared_model_bias,
            "reviewer_calls": self.reviewer_calls,
            "tokens_spent": self.tokens_spent,
            "accepted_eligible": self.accepted_eligible,
        }


@dataclass(frozen=True, slots=True)
class StopDecision:
    """Whether to keep refining, and what is preserved either way."""

    kind: str
    reason: str
    should_stop: bool
    change_justified: bool
    preserved_variant_id: str
    preserved_content_digest: str

    def __post_init__(self) -> None:
        if self.kind not in STOP_KINDS:
            raise RefinementError(f"unknown stop kind {self.kind!r}; expected one of {list(STOP_KINDS)}.")
        if self.change_justified and self.kind != "goal_met":
            raise SelectionRefused(
                "a change is justified only by a met goal; a change recorded under "
                f"{self.kind!r} is the loop talking itself into an edit."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "should_stop": self.should_stop,
            "change_justified": self.change_justified,
            "preserved_variant_id": self.preserved_variant_id,
            "preserved_content_digest": self.preserved_content_digest,
        }


@dataclass(frozen=True, slots=True)
class Escalation:
    """A capability this session does not have, who must decide, and what was tried.

    ``capability`` is a concrete missing ability rather than a complaint, ``escalate_to`` is a named
    human, and ``evidence`` says what shows the gap. "It was too hard" is not an escalation.
    """

    capability: str
    why_needed: str
    evidence: str
    escalate_to: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _text(self.capability, "capability", limit=300))
        object.__setattr__(self, "why_needed", _text(self.why_needed, "why_needed", limit=800))
        object.__setattr__(self, "evidence", _text(self.evidence, "evidence", limit=800))
        target = _text(self.escalate_to, "escalate_to", limit=80)
        if not _HUMAN_RE.fullmatch(target):
            raise ComparisonRefused(
                "an escalation must name a human decision owner (owner:, user: or reviewer:); an "
                "escalation to nobody is a note that nobody reads."
            )
        object.__setattr__(self, "escalate_to", target)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "why_needed": self.why_needed,
            "evidence": self.evidence,
            "escalate_to": self.escalate_to,
        }


# ---------------------------------------------------------------------------
# The session.
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 6)


@dataclass(frozen=True, slots=True)
class RefinementSession:
    """The bounded state of one refinement effort.

    Frozen, and advanced by :meth:`apply`, so "which version was accepted when" is a value the
    caller can keep, log or compare rather than mutable state to be reasoned about afterwards.
    """

    incumbent: Variant
    limits: RefinementLimits
    producer_model_family: str
    accepted: Variant
    accepted_style: float | None = None
    rounds: tuple[RoundOutcome, ...] = ()
    no_progress_streak: int = 0
    reviewer_calls: int = 0
    tokens_spent: int = 0
    escalations: tuple[Escalation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.incumbent, Variant) or not self.incumbent.is_incumbent:
            raise ComparisonRefused(
                "a refinement session starts from an incumbent: the output that already exists is the "
                "baseline every round is judged against."
            )
        if not isinstance(self.limits, RefinementLimits):
            raise RefinementError("a session needs RefinementLimits; an unbounded refinement loop is not a plan.")
        object.__setattr__(
            self, "producer_model_family", _identifier(self.producer_model_family, "producer_model_family")
        )
        if not isinstance(self.accepted, Variant):
            raise RefinementError("accepted must be a Variant.")
        for judged in self.rounds:
            if not isinstance(judged, RoundOutcome):
                raise RefinementError("rounds must hold RoundOutcome records.")
        for gap in self.escalations:
            if not isinstance(gap, Escalation):
                raise RefinementError("escalations must hold Escalation records.")

    @classmethod
    def start(
        cls,
        *,
        incumbent: Variant,
        limits: RefinementLimits,
        producer_model_family: str,
    ) -> "RefinementSession":
        """Begin from the incumbent, with nothing accepted but the incumbent itself."""
        return cls(
            incumbent=incumbent,
            limits=limits,
            producer_model_family=producer_model_family,
            accepted=incumbent,
        )

    # -- validation helpers -------------------------------------------------

    def _check_ballot(self, round_record: RefinementRound) -> None:
        variants = {item.variant_id: item for item in round_record.variants}
        incumbents = [item for item in round_record.variants if item.is_incumbent]
        if len(incumbents) != 1:
            raise ComparisonRefused(
                "the ballot must contain the incumbent exactly once: the unchanged output is always a "
                "candidate, and a round where every entry is new asks only whether the new ones differ."
            )
        incumbent = incumbents[0]
        if incumbent.content_digest != self.incumbent.content_digest:
            raise ComparisonRefused(
                "the ballot's incumbent is not this session's incumbent; a round judged against a "
                "different baseline is not a round in this session."
            )
        if incumbent.variant_id != self.incumbent.variant_id:
            raise ComparisonRefused("the ballot renamed the incumbent; the baseline cannot change identity mid-session.")
        for item in round_record.variants:
            if item is incumbent:
                continue
            if item.content_digest == self.incumbent.content_digest:
                raise SelectionRefused(
                    f"variant {item.variant_id!r} has the incumbent's content; an unchanged output is the "
                    "incumbent, not a revision, and counting it as a change is how a no-op round reports a win."
                )
        if self.accepted.variant_id not in variants:
            raise SelectionRefused(
                f"the accepted version {self.accepted.variant_id!r} must be on the ballot in every round; "
                "a refinement loop that does not defend what it accepted can only move forward."
            )
        if self.accepted.content_digest != variants[self.accepted.variant_id].content_digest:
            raise SelectionRefused("the accepted version on the ballot has different content from the accepted one.")
        for verdict in round_record.verdicts:
            if verdict.variant_id not in variants:
                raise SelectionRefused(f"a verdict names {verdict.variant_id!r}, which is not on the ballot.")
        for item in round_record.variants:
            if not round_record.verdicts_for(item.variant_id):
                raise SelectionRefused(
                    f"variant {item.variant_id!r} has no verdict; an unjudged candidate is not a candidate, "
                    "and a round that scores only its favourites is a preference."
                )
        for verdict in round_record.verdicts:
            authored_by = variants[verdict.variant_id].authored_by_session
            if verdict.context.session_id == authored_by:
                raise ComparisonRefused(
                    f"reviewer {verdict.reviewer_id!r} is scoring {verdict.variant_id!r} from the session "
                    "that wrote it; self-grading is not a review."
                )
        sessions = {verdict.context.session_id for verdict in round_record.verdicts}
        if len(round_record.variants) > 1 and len(sessions) < 2:
            raise ComparisonRefused(
                "a comparison between variants needs verdicts from at least two sessions; one session "
                "judging both the incumbent and its replacement is one opinion counted twice."
            )
        for requirement in round_record.requirements:
            if not requirement.is_hard:
                continue
            for verdict in round_record.verdicts:
                if verdict.result_for(requirement.requirement_id) is None:
                    # Missing is not passing: the variant is ineligible, handled in _outcome_for.
                    continue

    def _bias_state(self, round_record: RefinementRound) -> str:
        families = {verdict.context.model_family for verdict in round_record.verdicts}
        sessions = {verdict.context.session_id for verdict in round_record.verdicts}
        if families - {self.producer_model_family} and len(sessions) >= 2:
            return "mitigated_by_independent_context"
        return "possible"

    def _outcome_for(self, round_record: RefinementRound, variant: Variant) -> VariantOutcome:
        verdicts = round_record.verdicts_for(variant.variant_id)
        reasons: list[str] = []
        if not verdicts:
            return VariantOutcome(
                variant_id=variant.variant_id,
                eligible=False,
                style_score=None,
                blocking_reasons=("no reviewer scored this variant",),
            )
        for requirement in round_record.hard_requirements:
            for verdict in verdicts:
                result = verdict.result_for(requirement.requirement_id)
                if result is None:
                    reasons.append(
                        f"{verdict.reviewer_id} did not report the {requirement.kind} requirement "
                        f"{requirement.requirement_id!r}; a missing hard check is not a pass"
                    )
                elif not result.passed:
                    reasons.append(
                        f"{verdict.reviewer_id} failed the {requirement.kind} requirement "
                        f"{requirement.requirement_id!r}"
                    )
        style = _mean([verdict.style_score for verdict in verdicts])
        return VariantOutcome(
            variant_id=variant.variant_id,
            eligible=not reasons,
            style_score=style,
            blocking_reasons=tuple(reasons),
        )

    def _spend_check(self, round_record: RefinementRound) -> None:
        if len(self.rounds) + 1 > self.limits.max_rounds:
            raise BudgetSpent(
                f"round {round_record.round_index} exceeds the {self.limits.max_rounds}-round limit; the "
                "budget is a ceiling, not a target to be discovered."
            )
        calls = len(round_record.verdicts)
        if self.reviewer_calls + calls > self.limits.max_reviewer_calls:
            raise BudgetSpent(
                f"this round needs {calls} reviewer calls and the session has spent {self.reviewer_calls} "
                f"of {self.limits.max_reviewer_calls}; the budget counts every reviewer against every "
                "variant, not one 'iteration'."
            )
        if self.tokens_spent + round_record.tokens_spent > self.limits.max_tokens:
            raise BudgetSpent(
                f"this round declares {round_record.tokens_spent} tokens against "
                f"{self.limits.max_tokens - self.tokens_spent} remaining; refinement stops at the ceiling."
            )

    # -- advancing ----------------------------------------------------------

    def apply(self, round_record: RefinementRound) -> "RefinementSession":
        """Judge one round and return the next session state.

        Refusals happen before any scoring is recorded: a round that would breach the budget, or that
        omits the incumbent, costs nothing and leaves the session unchanged.
        """
        if not isinstance(round_record, RefinementRound):
            raise RefinementError("apply needs a RefinementRound.")
        if round_record.round_index != len(self.rounds) + 1:
            raise SelectionRefused(
                f"round {round_record.round_index} arrived where round {len(self.rounds) + 1} was expected; "
                "skipped or repeated rounds hide spend and reset the plateau count."
            )
        self._spend_check(round_record)
        self._check_ballot(round_record)

        outcomes = tuple(self._outcome_for(round_record, variant) for variant in round_record.variants)
        eligible = [item for item in outcomes if item.eligible]
        accepted_outcome = next(item for item in outcomes if item.variant_id == self.accepted.variant_id)
        best: VariantOutcome | None = None
        for item in sorted(eligible, key=lambda outcome: (-(outcome.style_score or 0.0), outcome.variant_id)):
            if best is None:
                best = item
        accepted_after = self.accepted
        accepted_style = self.accepted_style
        improved = False
        baseline_style = self.accepted_style
        if baseline_style is None:
            baseline_style = accepted_outcome.style_score
        if best is not None and best.variant_id != self.accepted.variant_id:
            candidate_style = best.style_score or 0.0
            margin = candidate_style - (baseline_style if baseline_style is not None else 0.0)
            if margin >= self.limits.min_improvement:
                accepted_after = next(
                    variant for variant in round_record.variants if variant.variant_id == best.variant_id
                )
                accepted_style = candidate_style
                improved = True
                reason = (
                    f"{best.variant_id} passed every hard requirement and beats the accepted version by "
                    f"{round(margin, 6)} (margin {self.limits.min_improvement})"
                )
            else:
                reason = (
                    f"{best.variant_id} passed every hard requirement but beats the accepted version by "
                    f"{round(margin, 6)}, below the {self.limits.min_improvement} margin; a score that moved "
                    "is not an improvement"
                )
        elif best is not None:
            reason = f"the accepted version {best.variant_id} remains the strongest eligible candidate"
            accepted_style = best.style_score
        else:
            reason = "no variant passed every hard requirement; nothing is eligible to replace the accepted version"

        outcome = RoundOutcome(
            round_index=round_record.round_index,
            outcomes=outcomes,
            accepted_before=self.accepted.variant_id,
            accepted_after=accepted_after.variant_id,
            improved=improved,
            reason=reason,
            shared_model_bias=self._bias_state(round_record),
            reviewer_calls=len(round_record.verdicts),
            tokens_spent=round_record.tokens_spent,
            accepted_eligible=accepted_outcome.eligible,
        )
        return RefinementSession(
            incumbent=self.incumbent,
            limits=self.limits,
            producer_model_family=self.producer_model_family,
            accepted=accepted_after,
            accepted_style=accepted_style,
            rounds=self.rounds + (outcome,),
            no_progress_streak=0 if improved else self.no_progress_streak + 1,
            reviewer_calls=self.reviewer_calls + len(round_record.verdicts),
            tokens_spent=self.tokens_spent + round_record.tokens_spent,
            escalations=self.escalations,
        )

    def escalate(self, gap: Escalation) -> "RefinementSession":
        """Record a capability this session does not have. The report becomes ``partial``."""
        if not isinstance(gap, Escalation):
            raise RefinementError("escalate needs an Escalation record.")
        return RefinementSession(
            incumbent=self.incumbent,
            limits=self.limits,
            producer_model_family=self.producer_model_family,
            accepted=self.accepted,
            accepted_style=self.accepted_style,
            rounds=self.rounds,
            no_progress_streak=self.no_progress_streak,
            reviewer_calls=self.reviewer_calls,
            tokens_spent=self.tokens_spent,
            escalations=self.escalations + (gap,),
        )

    # -- stopping -----------------------------------------------------------

    def stop_decision(self) -> StopDecision:
        """Why to stop, whether the change is justified, and what is preserved.

        The preserved version is the accepted one, never the most recent candidate: a round that lost
        does not get to replace the version that won.
        """

        def decision(kind: str, reason: str, *, should_stop: bool = True) -> StopDecision:
            justified = kind == "goal_met" and self.accepted.content_digest != self.incumbent.content_digest
            return StopDecision(
                kind=kind,
                reason=reason,
                should_stop=should_stop,
                change_justified=justified,
                preserved_variant_id=self.accepted.variant_id,
                preserved_content_digest=self.accepted.content_digest,
            )

        if self.escalations:
            return decision(
                "capability_gap",
                "the session recorded a capability it does not have: "
                + "; ".join(gap.capability for gap in self.escalations),
            )
        if not self.rounds:
            return decision(
                "insufficient_evidence",
                "no round has been judged yet, so nothing is known about whether a change would help",
                should_stop=False,
            )
        if len(self.rounds) >= self.limits.max_rounds:
            return decision("rounds_exhausted", f"the {self.limits.max_rounds}-round limit is spent")
        if self.reviewer_calls >= self.limits.max_reviewer_calls:
            return decision(
                "budget_exhausted",
                f"{self.reviewer_calls} reviewer calls spent of {self.limits.max_reviewer_calls}",
            )
        if self.tokens_spent >= self.limits.max_tokens:
            return decision("budget_exhausted", f"{self.tokens_spent} tokens spent of {self.limits.max_tokens}")
        if self.no_progress_streak >= self.limits.no_progress_rounds:
            return decision(
                "no_progress",
                f"{self.no_progress_streak} consecutive rounds without a justified improvement "
                f"(limit {self.limits.no_progress_rounds})",
            )
        if self.accepted.content_digest != self.incumbent.content_digest:
            return decision("goal_met", f"the accepted version {self.accepted.variant_id} is a justified change")
        return decision(
            "insufficient_evidence",
            "no round has produced a candidate that passed every hard requirement and cleared the "
            "improvement margin",
            should_stop=False,
        )

    # -- reporting ----------------------------------------------------------

    def report(self, *, wall_seconds: float) -> "RefinementReport":
        """The session's record, including everything that was not established."""
        if isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float)) or wall_seconds < 0:
            raise RefinementError("wall_seconds must be a non-negative number measured by the caller.")
        return RefinementReport(
            session=self,
            stop=self.stop_decision(),
            wall_seconds=float(wall_seconds),
        )


@dataclass(frozen=True, slots=True)
class RefinementReport:
    """A refinement session's account of itself.

    ``status`` is ``partial`` whenever an escalation was recorded or the accepted version lost a hard
    requirement in a later round: something the caller asked for was not established, and
    :meth:`claim_success` says so out loud.
    """

    session: RefinementSession
    stop: StopDecision
    wall_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.session, RefinementSession):
            raise RefinementError("a report needs the RefinementSession it describes.")
        if not isinstance(self.stop, StopDecision):
            raise RefinementError("a report needs its StopDecision.")

    @property
    def status(self) -> str:
        if self.session.escalations:
            return "partial"
        if any(not outcome.accepted_eligible for outcome in self.session.rounds):
            return "partial"
        return "delivered"

    @property
    def recommendation(self) -> str:
        return "accept_revision" if self.stop.change_justified else "keep_incumbent"

    @property
    def undemonstrated_hard_requirements(self) -> tuple[str, ...]:
        """Hard requirements that never passed in any round, or passed for no eligible variant."""
        seen: list[str] = []
        for outcome in self.session.rounds:
            ids = {item.variant_id: item for item in outcome.outcomes}
            for variant_id, item in ids.items():
                if item.eligible:
                    continue
                for reason in item.blocking_reasons:
                    if "requirement" in reason and reason not in seen:
                        seen.append(reason)
        return tuple(seen)

    def assert_no_fabricated_success(self) -> None:
        """Refuse to let a partial result be described as delivered."""
        if self.status == "delivered":
            return
        detail = (
            "; ".join(gap.capability for gap in self.session.escalations)
            if self.session.escalations
            else "the accepted version failed a hard requirement in a later round"
        )
        raise FabricatedSuccessRefused(
            f"this refinement result is {self.status!r} and cannot be reported as delivered: {detail}."
        )

    def claim_success(self) -> str:
        """Return the success sentence, or raise if there is no success to claim."""
        self.assert_no_fabricated_success()
        if self.recommendation == "accept_revision":
            return f"accepted {self.stop.preserved_variant_id} as an improvement over the incumbent"
        return "kept the incumbent; no candidate cleared every gate and the improvement margin"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": RB_SCHEMA_VERSION,
            "status": self.status,
            "recommendation": self.recommendation,
            "incumbent": self.session.incumbent.to_mapping(),
            "accepted": self.session.accepted.to_mapping(),
            "limits": self.session.limits.to_mapping(),
            "rounds": [outcome.to_mapping() for outcome in self.session.rounds],
            "reviewer_calls": self.session.reviewer_calls,
            "tokens_spent": self.session.tokens_spent,
            "wall_seconds": self.wall_seconds,
            "no_progress_streak": self.session.no_progress_streak,
            "stop": self.stop.to_mapping(),
            "escalations": [gap.to_mapping() for gap in self.session.escalations],
            "undemonstrated_hard_requirements": list(self.undemonstrated_hard_requirements),
            "hard_requirements_traded_for_style": False,
            "weights_changed": False,
            "budget_source": "refinement_bounds/v1: rounds, reviewer calls and tokens counted per round "
            "across every reviewer and variant; the caller's ExecutionBudget remains the outer control plane",
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
