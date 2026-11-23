"""Continuation contracts: context, preferences and artifact continuity (AP-015).

The story's objective is a *test*: *does the work continue correctly after
compression, retrieval and model handoff?* That question has a trap in it, and
this module is built around avoiding the trap. A summary can read beautifully
and still lose the one approval that authorised the rest of the work, so
**readability is not continuity**. Continuity is verified by probing whether the
successor can still answer questions that depend on decisions and artifact
trails, not by asking whether the recap reads well.

Five properties are load-bearing, and each is structural rather than advisory:

* **Continuity has a checklist, and it can fail (AC1).** A
  :class:`ContinuationState` names what must survive: the brief, unresolved
  failures, current artifact versions, approvals, authority and the outstanding
  decisions. A successor that survives none of them is not "mostly fine" — it
  is refused, and the refusal names exactly what is missing. Every element is
  either ``survived`` or ``reconstructed`` (with the source of the
  reconstruction); there is no third state, and silence is not survival.

* **Fitting never silently drops required input (AC2).** This module does not
  re-implement fitting. It delegates to ``context_reassembly``, whose contract
  already fails audibly when the mandatory messages cannot fit, and it then
  *audits* the result: if anything on the required list was dropped, continuity
  is refused. That is the difference between "the request fits" and "the request
  still contains what the work needs".

* **Probes test decisions and artifact trails, not readability (AC3).** Every
  :class:`ContinuationProbe` declares a ``probe_kind`` — ``decision``,
  ``artifact``, ``approval``, ``authority``, ``failure`` or ``preference`` — and
  a probe whose expected answer is a summary-quality rating is refused at
  construction. A probe bank also reports its own coverage, so a bank of six
  probes that all test the same decision cannot masquerade as thorough.

* **Retrieval remembers where it came from and cannot be promoted (AC4).**
  A :class:`RecalledItem` carries provenance and a trust class. A recovered
  lesson keeps its original trust: an ``untrusted`` source stays untrusted
  through recall, and a :class:`RecalledInstruction` — text that *instructs* —
  is refused entirely unless its provenance is trusted and verified. Recall can
  restore what the project already knew; it cannot mint authority.

* **Recall does not cross project or privacy boundaries (AC5).** Each item is
  tagged with the project scope it may be recalled in and a confidentiality
  class. :func:`recall_for` filters on scope identity, and secret or restricted
  items are refused outside their own scope even when their ids match. A
  cross-project hit is reported as *excluded*, never as a near-miss to be
  reviewed.

Handoff is explicit about what does not travel (AC6): the portable state is
recorded as data, and portability of a KV cache is asserted ``False`` everywhere
because no runtime here offers it. Nothing in this module loads a model, starts
a runtime, reads a private store or wires a production caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

SCHEMA_VERSION = 1

RequiredKind = Literal[
    "brief", "unresolved_failure", "artifact", "approval", "authority", "decision"
]
REQUIRED_KINDS: tuple[RequiredKind, ...] = (
    "brief",
    "unresolved_failure",
    "artifact",
    "approval",
    "authority",
    "decision",
)
ContinuityStatus = Literal["survived", "reconstructed", "lost"]
ProbeKind = Literal[
    "decision", "artifact", "approval", "authority", "failure", "preference"
]
TrustClass = Literal["trusted", "verified_task", "untrusted", "secret"]
Confidentiality = Literal["public", "internal", "restricted", "secret"]
#: Trust classes that may travel with retrieved instruction text.
INSTRUCTIVE_TRUST: frozenset[str] = frozenset({"trusted", "verified_task"})
_UNIT_INTERVAL = (0.0, 1.0)
_MAX_TEXT = 300


class ContinuityError(ValueError):
    """Raised when a continuity input or recorded state is invalid."""


class ContinuityRefused(RuntimeError):
    """Raised when work must not continue because required state was lost."""


class RecallRefused(RuntimeError):
    """Raised when recall would cross a project, privacy or trust boundary."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(
    value: object, name: str, *, optional: bool = False, max_length: int = _MAX_TEXT
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise ContinuityError(f"{name} must not be empty.")
    if "\x00" in result:
        raise ContinuityError(f"{name} must not contain a NUL character.")
    if len(result) > max_length:
        raise ContinuityError(f"{name} must be at most {max_length} characters.")
    return result or None


def _fraction(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number.")
    number = float(value)
    if not _UNIT_INTERVAL[0] <= number <= _UNIT_INTERVAL[1]:
        raise ContinuityError(f"{name} must be within [0, 1].")
    return round(number, 6)


# ---------------------------------------------------------------------------
# AC1 — the continuity checklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequiredElement:
    """One thing that must survive compression, retrieval or handoff.

    ``digest`` rather than content, matching the repository's hash-only audit
    convention: continuity is verified against identity, not by copying text
    that may itself be sensitive.
    """

    kind: RequiredKind
    element_id: str
    digest: str
    critical: bool = True

    def __post_init__(self) -> None:
        if self.kind not in REQUIRED_KINDS:
            raise ContinuityError(f"unknown required kind: {self.kind!r}.")
        object.__setattr__(self, "element_id", _text(self.element_id, "element_id"))
        object.__setattr__(self, "digest", _text(self.digest, "digest"))
        if not self.digest.startswith("sha256:"):
            raise ContinuityError("element digest must be a sha256: digest.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "element_id": self.element_id,
            "digest": self.digest,
            "critical": self.critical,
        }


@dataclass(frozen=True, slots=True)
class ElementStatus:
    """What happened to one required element, and how it came back.

    ``reconstructed`` requires a ``source``: something was rebuilt from an
    artifact, a log or a store, and saying so without naming where it came from
    would be an assertion, not a reconstruction.
    """

    element: RequiredElement
    status: ContinuityStatus
    source: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("survived", "reconstructed", "lost"):
            raise ContinuityError(f"unknown continuity status: {self.status!r}.")
        object.__setattr__(self, "source", _text(self.source, "source", optional=True))
        object.__setattr__(self, "note", _text(self.note, "note", optional=True) or "")
        if self.status == "reconstructed" and not self.source:
            raise ContinuityError(
                "a reconstructed element must name where it was reconstructed from; an unnamed "
                "reconstruction is an assertion (AC1)."
            )
        if self.status == "survived" and self.source:
            raise ContinuityError(
                "a survived element was never lost, so it has no reconstruction source."
            )

    @property
    def is_continuous(self) -> bool:
        return self.status in ("survived", "reconstructed")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.element.kind,
            "element_id": self.element.element_id,
            "critical": self.element.critical,
            "status": self.status,
            "continuous": self.is_continuous,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ContinuationState:
    """The checklist a successor must satisfy before the work continues.

    Continuity is *not* a score. If a critical element is ``lost``, the state is
    not 83% continuous — it is refused, and the refusal names the element. A
    partial pass here is how an approval quietly disappears behind a good
    summary.
    """

    task_id: str
    project_scope: str
    element_statuses: tuple[ElementStatus, ...]
    handoff_reason: str = ""
    predecessor_model_id: str | None = None
    successor_model_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(
            self, "project_scope", _text(self.project_scope, "project_scope")
        )
        object.__setattr__(
            self,
            "handoff_reason",
            _text(self.handoff_reason, "handoff_reason", optional=True) or "",
        )
        object.__setattr__(
            self,
            "predecessor_model_id",
            _text(self.predecessor_model_id, "predecessor_model_id", optional=True),
        )
        object.__setattr__(
            self,
            "successor_model_id",
            _text(self.successor_model_id, "successor_model_id", optional=True),
        )
        statuses = tuple(self.element_statuses)
        if not statuses:
            raise ContinuityError(
                "a continuation state must record at least one required element."
            )
        seen: set[tuple[str, str]] = set()
        for status in statuses:
            if not isinstance(status.element, RequiredElement):
                raise TypeError("element_statuses must hold ElementStatus values.")
            key = (status.element.kind, status.element.element_id)
            if key in seen:
                raise ContinuityError(
                    f"duplicate continuity record for {key[0]}:{key[1]}."
                )
            seen.add(key)
        object.__setattr__(self, "element_statuses", statuses)

    @property
    def lost(self) -> tuple[ElementStatus, ...]:
        return tuple(s for s in self.element_statuses if s.status == "lost")

    @property
    def critical_lost(self) -> tuple[ElementStatus, ...]:
        return tuple(s for s in self.lost if s.element.critical)

    @property
    def is_continuous(self) -> bool:
        """Continuous only when nothing critical was lost."""
        return not self.critical_lost

    @property
    def reconstructed(self) -> tuple[ElementStatus, ...]:
        return tuple(s for s in self.element_statuses if s.status == "reconstructed")

    def by_kind(self) -> dict[str, tuple[ElementStatus, ...]]:
        grouped: dict[str, list[ElementStatus]] = {}
        for status in self.element_statuses:
            grouped.setdefault(status.element.kind, []).append(status)
        return {kind: tuple(items) for kind, items in sorted(grouped.items())}

    def missing_kinds(
        self, required: Sequence[RequiredKind] = REQUIRED_KINDS
    ) -> tuple[RequiredKind, ...]:
        """Kinds the checklist never even considered.

        Absence is not survival: a checklist that forgets approvals cannot
        certify that they survived.
        """
        present = {s.element.kind for s in self.element_statuses if s.is_continuous}
        return tuple(kind for kind in required if kind not in present)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "project_scope": self.project_scope,
            "handoff_reason": self.handoff_reason,
            "predecessor_model_id": self.predecessor_model_id,
            "successor_model_id": self.successor_model_id,
            "elements": [status.to_mapping() for status in self.element_statuses],
            "lost": [status.element.element_id for status in self.lost],
            "critical_lost": [
                status.element.element_id for status in self.critical_lost
            ],
            "missing_kinds": list(self.missing_kinds()),
            "is_continuous": self.is_continuous,
            "continuity_is_a_score": False,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def require_continuity(
    state: ContinuationState,
    *,
    required: Sequence[RequiredKind] = REQUIRED_KINDS,
) -> None:
    """Refuse to continue when required state was lost.

    Raises rather than returning a verdict so a caller cannot read a boolean,
    decide the loss is acceptable, and carry on (AC1).
    """
    if not isinstance(state, ContinuationState):
        raise TypeError("state must be a ContinuationState value.")
    problems: list[str] = []
    if state.critical_lost:
        problems.append(
            "lost critical elements: "
            + ", ".join(s.element.element_id for s in state.critical_lost)
        )
    missing = state.missing_kinds(required)
    if missing:
        problems.append("required kinds never accounted for: " + ", ".join(missing))
    if problems:
        raise ContinuityRefused(
            "work must not continue: "
            + "; ".join(problems)
            + ". Reconstruct these explicitly or "
            "stop the task (AC1)."
        )


# ---------------------------------------------------------------------------
# AC2 — fitting must not silently drop required input
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FitAudit:
    """What the fit pass did to the required elements (AC2).

    ``fits`` is the reassembly contract's own verdict. ``dropped_required`` is
    this module's independent finding: the fit may have succeeded, and still
    have discarded something the work needs.
    """

    budget_tokens: int
    used_tokens: int
    dropped_indices: tuple[int, ...] = ()
    dropped_required: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        for name in ("budget_tokens", "used_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContinuityError(f"{name} must be a non-negative integer.")
        object.__setattr__(
            self, "dropped_indices", tuple(sorted(set(self.dropped_indices)))
        )
        object.__setattr__(
            self, "dropped_required", tuple(sorted(set(self.dropped_required)))
        )
        object.__setattr__(
            self, "reason", _text(self.reason, "reason", optional=True) or ""
        )

    @property
    def truncated(self) -> bool:
        return bool(self.dropped_indices)

    @property
    def is_safe(self) -> bool:
        """Safe means nothing required was dropped — not that the fit succeeded."""
        return not self.dropped_required

    def to_mapping(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens,
            "truncated": self.truncated,
            "dropped_indices": list(self.dropped_indices),
            "dropped_required": list(self.dropped_required),
            "safe": self.is_safe,
            "reason": self.reason,
            "reassembly_contract": "context_reassembly.reassemble_context",
        }


def audit_fit(
    *,
    budget_tokens: int,
    used_tokens: int,
    dropped_indices: Sequence[int] = (),
    required_indices: Mapping[str, int] | None = None,
    reason: str = "",
) -> FitAudit:
    """Audit a reassembly result against the indices the work requires.

    ``required_indices`` maps an element id to the transcript index that carries
    it. Anything in that map whose index was dropped becomes an unsafe finding,
    which is what makes silent loss of required input impossible to miss (AC2).
    """
    dropped = set(dropped_indices)
    lost: list[str] = []
    for element_id, index in (required_indices or {}).items():
        if index in dropped:
            lost.append(element_id)
    return FitAudit(
        budget_tokens=budget_tokens,
        used_tokens=used_tokens,
        dropped_indices=tuple(dropped_indices),
        dropped_required=tuple(lost),
        reason=reason,
    )


def require_fit_is_safe(audit: FitAudit) -> None:
    """Refuse a continuation whose required input was fitted away (AC2)."""
    if not isinstance(audit, FitAudit):
        raise TypeError("audit must be a FitAudit value.")
    if not audit.is_safe:
        raise ContinuityRefused(
            "required input was dropped to fit the context: "
            + ", ".join(audit.dropped_required)
            + ". Refusing to continue with a context that lost required content (AC2)."
        )


# ---------------------------------------------------------------------------
# AC3 — probes that test decisions and trails, not readability
# ---------------------------------------------------------------------------


_READABILITY_HINTS = (
    "readab",
    "summary quality",
    "how well",
    "understandable",
    "coherent",
    "well-written",
)


@dataclass(frozen=True, slots=True)
class ContinuationProbe:
    """One question a successor must answer to prove continuity.

    ``expected_terms`` are the specific facts the answer must contain. A probe
    whose expected answer describes the summary rather than the work is refused:
    a readable recap is exactly the failure mode this criterion exists for.
    """

    probe_id: str
    probe_kind: ProbeKind
    question: str
    expected_terms: tuple[str, ...]
    element_id: str

    def __post_init__(self) -> None:
        if self.probe_kind not in (
            "decision",
            "artifact",
            "approval",
            "authority",
            "failure",
            "preference",
        ):
            raise ContinuityError(f"unknown probe kind: {self.probe_kind!r}.")
        object.__setattr__(self, "probe_id", _text(self.probe_id, "probe_id"))
        object.__setattr__(
            self, "question", _text(self.question, "question", max_length=400)
        )
        object.__setattr__(self, "element_id", _text(self.element_id, "element_id"))
        terms = tuple(
            sorted({t.strip().lower() for t in self.expected_terms if t and t.strip()})
        )
        if not terms:
            raise ContinuityError(
                "a probe must state what a correct answer contains (AC3)."
            )
        object.__setattr__(self, "expected_terms", terms)
        lowered = f"{self.question} {' '.join(terms)}".lower()
        if any(hint in lowered for hint in _READABILITY_HINTS):
            raise ContinuityError(
                "a continuation probe must test a decision, an artifact trail, an approval, authority, a "
                "failure or a preference -- not how readable the summary is (AC3)."
            )

    def score(self, answer: str) -> float:
        """Fraction of expected facts the answer contains."""
        if not isinstance(answer, str):
            raise TypeError("answer must be a string.")
        lowered = answer.lower()
        hits = sum(1 for term in self.expected_terms if term in lowered)
        return round(hits / len(self.expected_terms), 6)

    def passes(self, answer: str) -> bool:
        return self.score(answer) >= 1.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "probe_kind": self.probe_kind,
            "question": self.question,
            "element_id": self.element_id,
            "expected_terms": list(self.expected_terms),
        }


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """One probe's result against one successor's answer."""

    probe: ContinuationProbe
    answer: str
    score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _fraction(self.score, "score"))
        object.__setattr__(
            self, "answer", _text(self.answer, "answer", optional=True) or ""
        )

    @property
    def passed(self) -> bool:
        return self.score >= 1.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe.probe_id,
            "probe_kind": self.probe.probe_kind,
            "element_id": self.probe.element_id,
            "score": self.score,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ProbeBank:
    """A set of probes, with coverage reported rather than assumed."""

    probes: tuple[ContinuationProbe, ...]

    def __post_init__(self) -> None:
        probes = tuple(self.probes)
        ids = [p.probe_id for p in probes]
        if len(set(ids)) != len(ids):
            raise ContinuityError("duplicate probe ids in the bank.")
        object.__setattr__(self, "probes", probes)

    def coverage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for probe in self.probes:
            counts[probe.probe_kind] = counts.get(probe.probe_kind, 0) + 1
        return dict(sorted(counts.items()))

    def uncovered(
        self,
        required: Sequence[ProbeKind] = (
            "decision",
            "artifact",
            "approval",
            "authority",
        ),
    ) -> tuple[str, ...]:
        """Kinds the bank never probes.

        A bank of ten probes that all test one decision is not thorough, and
        this method is how that is caught rather than praised.
        """
        present = {probe.probe_kind for probe in self.probes}
        return tuple(kind for kind in required if kind not in present)

    def run(self, answers: Mapping[str, str]) -> tuple[ProbeOutcome, ...]:
        outcomes = []
        for probe in self.probes:
            answer = answers.get(probe.probe_id, "")
            outcomes.append(
                ProbeOutcome(probe=probe, answer=answer, score=probe.score(answer))
            )
        return tuple(outcomes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "probe_count": len(self.probes),
            "coverage": self.coverage(),
            "uncovered_required_kinds": list(self.uncovered()),
            "probes": [probe.to_mapping() for probe in self.probes],
        }


def require_probes_pass(outcomes: Sequence[ProbeOutcome]) -> None:
    """Continuity is proven only when every probe is answered completely (AC3)."""
    failed = [o for o in outcomes if not o.passed]
    if not outcomes:
        raise ContinuityError(
            "a continuation verdict needs at least one probe outcome."
        )
    if failed:
        raise ContinuityRefused(
            "continuation probes did not pass: "
            + ", ".join(f"{o.probe.probe_id} ({o.score:.2f})" for o in failed)
            + ". A readable summary is not continuity (AC3)."
        )


# ---------------------------------------------------------------------------
# AC4/AC5 — recall with provenance, trust and project isolation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecalledItem:
    """Something remembered from earlier work, with its provenance intact.

    Recall never improves an item's standing: ``trust`` travels with the item
    exactly as it was recorded, so an untrusted source stays untrusted after
    being retrieved (AC4).
    """

    item_id: str
    project_scope: str
    trust: TrustClass
    confidentiality: Confidentiality
    source: str
    summary: str
    verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id"))
        object.__setattr__(
            self, "project_scope", _text(self.project_scope, "project_scope")
        )
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "summary", _text(self.summary, "summary", max_length=600)
        )
        if self.trust not in ("trusted", "verified_task", "untrusted", "secret"):
            raise ContinuityError(f"unknown trust class: {self.trust!r}.")
        if self.confidentiality not in ("public", "internal", "restricted", "secret"):
            raise ContinuityError(
                f"unknown confidentiality class: {self.confidentiality!r}."
            )
        if self.trust == "secret" and self.confidentiality != "secret":
            raise ContinuityError(
                "a secret-trust item must be classed secret; downgrading it is not recall."
            )

    @property
    def is_instructive(self) -> bool:
        """Only trusted, verified provenance may carry instruction text (AC4)."""
        return self.trust in INSTRUCTIVE_TRUST and self.verified

    def to_mapping(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "project_scope": self.project_scope,
            "trust": self.trust,
            "confidentiality": self.confidentiality,
            "source": self.source,
            "verified": self.verified,
            "is_instructive": self.is_instructive,
            "trust_upgraded_by_recall": False,
        }


@dataclass(frozen=True, slots=True)
class RecalledInstruction:
    """Recalled text that *tells the successor what to do*.

    This is the narrow, explicit door instructions must come through, and it is
    refused unless provenance is trusted and verified. Free-text recall cannot
    smuggle an instruction in, because instruction text has its own type (AC4).
    """

    text: str
    item: RecalledItem

    def __post_init__(self) -> None:
        if not isinstance(self.item, RecalledItem):
            raise TypeError("item must be a RecalledItem value.")
        object.__setattr__(self, "text", _text(self.text, "text", max_length=600))
        if not self.item.is_instructive:
            raise RecallRefused(
                f"recalled instruction from {self.item.item_id!r} has {self.item.trust!r} provenance; "
                "retrieved memory cannot upgrade untrusted instructions (AC4)."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "item_id": self.item.item_id,
            "trust": self.item.trust,
            "verified": self.item.verified,
            "provenance": self.item.source,
        }


@dataclass(frozen=True, slots=True)
class RecallResult:
    """What recall returned, and what it deliberately withheld (AC5)."""

    project_scope: str
    items: tuple[RecalledItem, ...]
    excluded: tuple[Mapping[str, str], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "project_scope": self.project_scope,
            "items": [item.to_mapping() for item in self.items],
            "excluded": [dict(entry) for entry in self.excluded],
            "cross_project_leak": False,
        }


def recall_for(
    items: Sequence[RecalledItem],
    *,
    project_scope: str,
    confidentiality_ceiling: Confidentiality = "internal",
) -> RecallResult:
    """Return only what this project may recall, recording what was withheld.

    Two independent filters apply, in this order: project scope identity, then
    confidentiality. A cross-project item is excluded even when it is public —
    a private project's existence is itself information (AC5).
    """
    scope = _text(project_scope, "project_scope")
    assert scope is not None  # _text raises on empty input
    order = {"public": 0, "internal": 1, "restricted": 2, "secret": 3}
    if confidentiality_ceiling not in order:
        raise ContinuityError(
            f"unknown confidentiality ceiling: {confidentiality_ceiling!r}."
        )
    kept: list[RecalledItem] = []
    excluded: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, RecalledItem):
            raise TypeError("items must be RecalledItem values.")
        if item.project_scope != scope:
            excluded.append(
                {"item_id": item.item_id, "reason": "different project scope"}
            )
            continue
        if order[item.confidentiality] > order[confidentiality_ceiling]:
            excluded.append(
                {
                    "item_id": item.item_id,
                    "reason": f"confidentiality {item.confidentiality} exceeds the {confidentiality_ceiling} ceiling",
                }
            )
            continue
        kept.append(item)
    return RecallResult(
        project_scope=scope, items=tuple(kept), excluded=tuple(excluded)
    )


# ---------------------------------------------------------------------------
# AC6 — handoff without assuming KV-cache portability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortableState:
    """What may cross a model switch. Explicitly not the KV cache (AC6).

    The artifact list is what a successor can *re-read*; it is not a claim that
    the predecessor's internal state can be resumed, because no runtime here
    offers that and assuming it would produce a continuation that looks fine and
    forgets what it was doing mid-step.
    """

    task_id: str
    project_scope: str
    artifact_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    approval_ids: tuple[str, ...] = ()
    preference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(
            self, "project_scope", _text(self.project_scope, "project_scope")
        )
        for name in ("artifact_ids", "decision_ids", "approval_ids", "preference_ids"):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))

    @property
    def kv_cache_portable(self) -> bool:
        """Always false. No runtime in this program offers cache portability."""
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_scope": self.project_scope,
            "artifact_ids": list(self.artifact_ids),
            "decision_ids": list(self.decision_ids),
            "approval_ids": list(self.approval_ids),
            "preference_ids": list(self.preference_ids),
            "kv_cache_portable": False,
            "resumes_predecessor_internal_state": False,
        }


@dataclass(frozen=True, slots=True)
class SwitchVerdict:
    """The result of validating a handoff against a continuity checklist."""

    portable: PortableState
    state: ContinuationState
    probe_outcomes: tuple[ProbeOutcome, ...] = ()

    def __post_init__(self) -> None:
        if self.portable.task_id != self.state.task_id:
            raise ContinuityError(
                "portable state and continuation state describe different tasks."
            )
        if self.portable.project_scope != self.state.project_scope:
            raise ContinuityError(
                "portable state and continuation state describe different projects."
            )

    @property
    def is_valid(self) -> bool:
        return self.state.is_continuous and all(o.passed for o in self.probe_outcomes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "portable": self.portable.to_mapping(),
            "continuity": self.state.to_mapping(),
            "probes": [outcome.to_mapping() for outcome in self.probe_outcomes],
            "valid": self.is_valid,
            "kv_cache_portable": False,
        }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_continuity(states: Sequence[ContinuationState]) -> str:
    """Deterministic JSON for review: sorted keys, stable order, trailing newline."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "states": [state.to_mapping() for state in states],
        "continuity_rules": {
            "required_kinds": list(REQUIRED_KINDS),
            "readability_is_continuity": False,
            "silent_truncation_permitted": False,
            "recall_upgrades_trust": False,
            "cross_project_recall_permitted": False,
            "kv_cache_portable": False,
        },
        "does_not_prove": [
            "a continuous state shows the checklist was satisfied, not that the successor produces "
            "the same quality of work",
            "probes are only as good as the questions asked; coverage is reported, not assumed",
            "no model was loaded and no handoff was executed to produce this record",
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
