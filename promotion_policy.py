"""AP-028 — promote and roll back improvements by risk and evidence.

Promotion is the moment a candidate stops being something the system *evaluated* and becomes
something the system *does*. Everything before this story bounds a proposal; this module decides
whether the proposal is allowed to touch anything, and how to take it back.

**The classifier is not the proposer's.** :func:`classify_change` reads the change's concrete effects
— files touched, capabilities requested, egress hosts, accounts, spend, whether it edits an
evaluator, approval, sandbox or audit control — and maps them onto the repository's existing
`product/M5_Autonomous_Production_Operating_Envelope.json` promotion classes rather than a second
vocabulary invented here. Risk is never an argument a caller passes: a proposer that can say "this is
low risk" has already been trusted with the answer. Any doubt escalates, and a change that declares no
review boundary at all cannot be constructed.

**Automatic promotion is off because the owner said so.** Owner decision U05 (2026-11-17) declined the
conservative `project_lesson` class the proposal offered: all four classes carry
`automatic_promotion=false`, and enabling any of them needs a new owner decision recorded *before*
the flag flips. This module reads those flags through
`autonomous_production_decisions.load_operating_envelope()` instead of hard-coding `False`, so a
future owner decision takes effect without a code change and no code change can enable one. A class
whose flag is off needs an approval ceremony; a class whose flag were ever turned on would *still*
need approved thresholds and held-out results, which is what :meth:`PromotionRequest.refusals` checks
first.

**New effects need their own grant.** Egress, accounts, spend and sensitive defaults are not
improvements to be promoted on quality evidence; they are new authority. A request whose declared
effects include any of them is refused without an existing explicit grant naming that exact effect,
and an owner's "approve" does not substitute for one.

**A candidate cannot edit its exam, and cannot promote an exam edit.** The authority controls are
fingerprinted, the classifier escalates any change that touches them, and the approval type such a
change requires is `independent_and_owner`. `AP-027` already refuses an optimiser that edits its
held-out set during a trial; this refuses the same edit arriving as the *proposal*.

**Stages are real, and a canary is the only thing that makes a success a deployment.** promotion runs
`task` → `project` → `canary` → `default`, each stage naming the narrower thing it can affect. A
canary regression stops the promotion at the stage that failed, records the bounded rollback,
supersedes the candidate's prior acceptance, and marks every artifact derived from that acceptance as
invalidated — an artifact produced under an acceptance that was withdrawn is not evidence, and a
report that leaves it looking like one is worse than the regression.

**Nothing is overwritten.** :class:`PromotionLedger` is append-only: every accepted version keeps its
own content digest and is recoverable, in-flight tasks stay pinned to the version they started with
rather than being migrated under their feet, and a rollback is a new record naming the version it
restored rather than a deletion of the record it replaced.

Nothing here reads a clock, contacts a provider or writes anything: promotion records are values, and
the ledger lives where the caller puts it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import autonomous_production_decisions as apd
import scoped_grants

__all__ = [
    "PromotionError",
    "ClassificationRefused",
    "AuthorityRefused",
    "EvidenceRefused",
    "StageRefused",
    "RollbackRefused",
    "CLASS_IDS",
    "REVIEW_BOUNDARIES",
    "APPROVAL_TYPES",
    "PROMOTION_STAGES",
    "GATED_EFFECTS",
    "AUTHORITY_CONTROL_KINDS",
    "ARTIFACT_STATES",
    "ChangeEffects",
    "Classification",
    "classify_change",
    "Approval",
    "HeldOutSummary",
    "PromotionRequest",
    "StageResult",
    "CanaryRegression",
    "StageOutcome",
    "PromotionRecord",
    "PromotionPolicySnapshot",
    "load_policy",
    "PromotionLedger",
    "PinRegistry",
]

PN_SCHEMA_VERSION = 1
PN_MAX_TEXT = 2000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HUMAN_RE = re.compile(r"^(owner|user|reviewer|security|independent):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The four promotion classes the operating envelope already defines. Kept as a tuple so the module
#: can name an order for reporting, but the authority for a class is the envelope, not this list.
CLASS_IDS: tuple[str, ...] = (
    "shadow_candidate_report",
    "project_lesson",
    "executable_skill_or_adapter",
    "authority_evaluator_or_runtime",
)

#: Review boundaries, weakest first. The classifier may escalate along this order and never descend.
REVIEW_BOUNDARIES: tuple[str, ...] = ("none", "owner", "owner_and_security", "independent_and_owner")

#: What an approval ceremony may consist of. ``automatic`` exists only so the module can *refuse* it
#: by name when the envelope flag is off; it is never a valid approval for a real promotion.
APPROVAL_TYPES: tuple[str, ...] = ("owner", "owner_and_security", "independent_and_owner", "automatic")

#: The staged rollout, narrowest first.
PROMOTION_STAGES: tuple[str, ...] = ("task", "project", "canary", "default")

#: Effects that are new authority rather than a quality improvement. Each needs a grant naming it.
GATED_EFFECTS: tuple[str, ...] = ("egress", "accounts", "spending", "sensitive_defaults")

#: Controls a candidate must never be able to edit in order to pass. Editing one is not a quality
#: change at all: it is a change to the rules the candidate is judged by.
AUTHORITY_CONTROL_KINDS: tuple[str, ...] = (
    "evaluator",
    "approval_policy",
    "sandbox_policy",
    "audit_log",
    "promotion_policy",
)

#: Artifact states downstream of a promotion. ``invalidated`` is not a deletion: the artifact is kept
#: and labelled, because an artifact produced under a withdrawn acceptance is evidence of something.
ARTIFACT_STATES: tuple[str, ...] = ("valid", "invalidated", "superseded")

#: Requested capabilities are only meaningful inside the repository's existing taxonomy.
_CAPABILITIES = scoped_grants.SG_CAPABILITY_TAXONOMY


class PromotionError(ValueError):
    """A promotion record is malformed or claims something it cannot support."""


class ClassificationRefused(PromotionError):
    """A change's risk could not be classified from its declared effects."""


class AuthorityRefused(PromotionError):
    """A promotion needs authority it does not hold: a grant, a ceremony, or a reviewer."""


class EvidenceRefused(PromotionError):
    """A promotion was claimed without the evidence that would support it."""


class StageRefused(PromotionError):
    """A stage was entered out of order, or its evidence does not cover it."""


class RollbackRefused(PromotionError):
    """A rollback named a version that is not recoverable, or a task that is not in flight."""


def _text(value: object, field_name: str, *, limit: int = PN_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise PromotionError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise PromotionError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise PromotionError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise PromotionError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _identifier(value: object, field_name: str) -> str:
    text = _text(value, field_name, limit=120)
    if not _ID_RE.fullmatch(text):
        raise PromotionError(f"{field_name} must be a bounded opaque identifier.")
    return text


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise PromotionError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _digests(value: Sequence[object], field_name: str) -> tuple[str, ...]:
    items = tuple(_digest(item, field_name) for item in value)
    if len(set(items)) != len(items):
        raise PromotionError(f"{field_name} must not repeat a digest.")
    return items


def _texts(value: Sequence[object], field_name: str, *, limit: int = 300) -> tuple[str, ...]:
    items: list[str] = []
    for raw in value:
        text = _text(raw, field_name, limit=limit)
        if text not in items:
            items.append(text)
    return tuple(items)


def _stricter(a: str, b: str) -> str:
    """The stricter of two review boundaries; unknown values are treated as the strictest."""
    if a not in REVIEW_BOUNDARIES:
        return a
    if b not in REVIEW_BOUNDARIES:
        return b
    return REVIEW_BOUNDARIES[max(REVIEW_BOUNDARIES.index(a), REVIEW_BOUNDARIES.index(b))]


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# AC1 — classification happens outside the proposing model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeEffects:
    """What a candidate actually changes, declared as concrete effects rather than as a risk level.

    Every field is something the caller had to observe: the paths, the capabilities, the hosts. There
    is deliberately no ``risk`` or ``confidence`` field — a proposer's own opinion of its risk is the
    one input this classifier must not take.
    """

    paths_touched: tuple[str, ...]
    capabilities_requested: tuple[str, ...] = ()
    egress_hosts: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()
    spend_minor_units: int = 0
    sensitive_defaults: tuple[str, ...] = ()
    authority_controls_touched: tuple[str, ...] = ()
    executable_artifacts: tuple[str, ...] = ()
    changes_retrieval_or_prompting: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths_touched", _texts(self.paths_touched, "path", limit=400))
        if not self.paths_touched:
            raise ClassificationRefused(
                "a change with no declared paths cannot be classified; 'nothing much' is not an effect."
            )
        object.__setattr__(
            self, "capabilities_requested", _texts(self.capabilities_requested, "capability", limit=80)
        )
        unknown = [cap for cap in self.capabilities_requested if cap not in _CAPABILITIES]
        if unknown:
            raise ClassificationRefused(
                f"unknown capability {unknown[0]!r}; capabilities come from the existing scoped-grant "
                f"taxonomy, not from the candidate's own vocabulary."
            )
        object.__setattr__(self, "egress_hosts", _texts(self.egress_hosts, "egress host", limit=200))
        object.__setattr__(self, "accounts", _texts(self.accounts, "account", limit=200))
        object.__setattr__(self, "sensitive_defaults", _texts(self.sensitive_defaults, "sensitive default", limit=200))
        if isinstance(self.spend_minor_units, bool) or not isinstance(self.spend_minor_units, int):
            raise ClassificationRefused("spend_minor_units must be an integer.")
        if self.spend_minor_units < 0:
            raise ClassificationRefused("spend_minor_units must not be negative.")
        controls = _texts(self.authority_controls_touched, "authority control", limit=200)
        for control in controls:
            kind = control.split(":", 1)[0]
            if kind not in AUTHORITY_CONTROL_KINDS:
                raise ClassificationRefused(
                    f"unknown authority control kind {kind!r}; the kinds are {list(AUTHORITY_CONTROL_KINDS)}. "
                    "An unrecognised control is escalated, not ignored."
                )
        object.__setattr__(self, "authority_controls_touched", controls)
        object.__setattr__(self, "executable_artifacts", _texts(self.executable_artifacts, "executable artifact", limit=400))
        if not isinstance(self.changes_retrieval_or_prompting, bool):
            raise ClassificationRefused("changes_retrieval_or_prompting must be a boolean.")

    def gated(self) -> tuple[str, ...]:
        """Which of :data:`GATED_EFFECTS` this change introduces."""
        found: list[str] = []
        if self.egress_hosts:
            found.append("egress")
        if self.accounts:
            found.append("accounts")
        if self.spend_minor_units:
            found.append("spending")
        if self.sensitive_defaults:
            found.append("sensitive_defaults")
        return tuple(found)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "paths_touched": list(self.paths_touched),
            "capabilities_requested": list(self.capabilities_requested),
            "egress_hosts": list(self.egress_hosts),
            "accounts": list(self.accounts),
            "spend_minor_units": self.spend_minor_units,
            "sensitive_defaults": list(self.sensitive_defaults),
            "authority_controls_touched": list(self.authority_controls_touched),
            "executable_artifacts": list(self.executable_artifacts),
            "changes_retrieval_or_prompting": self.changes_retrieval_or_prompting,
        }


@dataclass(frozen=True, slots=True)
class Classification:
    """The risk class a change falls into, and why.

    ``class_id`` is one of the envelope's own four classes, ``review_boundary`` is the envelope's
    boundary for it, and ``escalated_because`` names every effect that raised it. A classification
    that silently landed on a class weaker than one of its own effects would be the failure mode this
    whole story exists to prevent, so the escalation reasons are part of the record.
    """

    class_id: str
    review_boundary: str
    escalated_because: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.class_id not in CLASS_IDS:
            raise ClassificationRefused(f"unknown promotion class {self.class_id!r}.")
        if self.review_boundary not in REVIEW_BOUNDARIES:
            raise ClassificationRefused(
                f"unknown review boundary {self.review_boundary!r}; a class with no boundary cannot be "
                "promoted by anyone."
            )

    @property
    def is_automatic_candidate(self) -> bool:
        """Only the narrowest class could ever be automatic, and only if the owner enables it."""
        return self.class_id == "shadow_candidate_report"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "review_boundary": self.review_boundary,
            "escalated_because": list(self.escalated_because),
        }


def classify_change(effects: ChangeEffects, *, policy: "PromotionPolicySnapshot | None" = None) -> Classification:
    """Map a change's declared effects onto one of the envelope's promotion classes.

    The rule is escalate-only: the classifier starts at the weakest class that could contain the
    change and raises it for every effect that cannot live there. There is no path that lowers a
    class, and an unknown effect is treated as the most serious one it could be.
    """
    if not isinstance(effects, ChangeEffects):
        raise ClassificationRefused("classify_change needs a ChangeEffects record.")
    snapshot = policy if policy is not None else load_policy()
    escalated: list[str] = []

    if effects.authority_controls_touched:
        escalated.append(
            "touches authority controls (" + ", ".join(effects.authority_controls_touched) + ")"
        )
        class_id = "authority_evaluator_or_runtime"
    elif effects.executable_artifacts or set(effects.capabilities_requested) & {"external_mcp", "code_execution"}:
        escalated.append("ships executable behaviour or requests execution capability")
        class_id = "executable_skill_or_adapter"
    elif effects.changes_retrieval_or_prompting or effects.paths_touched:
        class_id = "project_lesson"
        if effects.changes_retrieval_or_prompting:
            escalated.append("changes future retrieval or prompting")
    else:  # pragma: no cover - paths_touched is required, so this arm is unreachable by construction
        class_id = "shadow_candidate_report"

    if effects.gated():
        # New authority does not change the class by itself, but it must never be invisible: it is
        # recorded as an escalation reason so the approval record states what it introduced.
        escalated.append("introduces new authority: " + ", ".join(effects.gated()))

    boundary = snapshot.boundary_for(class_id)
    if boundary not in REVIEW_BOUNDARIES:
        raise ClassificationRefused(
            f"the envelope's boundary for {class_id!r} is {boundary!r}, which this module does not "
            "understand; an unrecognised boundary is refused rather than read as permissive."
        )
    return Classification(class_id=class_id, review_boundary=boundary, escalated_because=tuple(escalated))


# ---------------------------------------------------------------------------
# AC2/AC3 — the policy, the request, and what it needs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromotionPolicySnapshot:
    """The owner-approved promotion policy as the envelope currently reads.

    A snapshot, not a hard-coded constant: U05 is an owner decision with a recorded way to change it,
    and a module that baked in ``False`` would make that decision unchangeable except by editing code.
    """

    automatic_classes: tuple[str, ...]
    boundaries: Mapping[str, str]
    default_effect: str
    decision_id: str

    def __post_init__(self) -> None:
        for class_id in self.automatic_classes:
            if class_id not in CLASS_IDS:
                raise ClassificationRefused(f"the envelope enables automatic promotion for unknown class {class_id!r}.")
        missing = [class_id for class_id in CLASS_IDS if class_id not in dict(self.boundaries)]
        if missing:
            raise ClassificationRefused(
                f"the envelope has no review boundary for {missing[0]!r}; a class without a boundary is "
                "not promotable."
            )
        object.__setattr__(self, "boundaries", {key: str(value) for key, value in self.boundaries.items()})
        if self.default_effect != "deny_promotion":
            raise ClassificationRefused(
                f"the promotion policy's default effect is {self.default_effect!r}; this module only "
                "understands deny-by-default, so anything else is refused rather than guessed at."
            )

    def boundary_for(self, class_id: str) -> str:
        return str(dict(self.boundaries)[class_id])

    @property
    def any_automatic(self) -> bool:
        return bool(self.automatic_classes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "default_effect": self.default_effect,
            "automatic_classes": list(self.automatic_classes),
            "boundaries": dict(sorted(dict(self.boundaries).items())),
        }


def load_policy() -> PromotionPolicySnapshot:
    """Read the owner's promotion policy from the operating-envelope register."""
    bundle = apd.load_operating_envelope()
    return PromotionPolicySnapshot(
        automatic_classes=tuple(sorted(item.id for item in bundle.promotion_classes if item.automatic_promotion)),
        boundaries={item.id: item.review_boundary for item in bundle.promotion_classes},
        default_effect="deny_promotion",
        decision_id="U05",
    )


@dataclass(frozen=True, slots=True)
class Approval:
    """A ceremony step: who approved, what they approved, and the boundary they cleared.

    ``approval_type`` is the *role* the approver cleared. A reviewer role must be held by a reviewer
    (``security:`` or ``independent:``), and the two-party boundaries must be signed by two different
    people -- otherwise "the owner approved and security approved" is one person in two fields.

    ``approval_type="automatic"`` is constructible only so that a caller can be *refused* with a
    message naming it; it never satisfies a requirement.
    """

    approver: str
    approval_type: str
    approved_digest: str
    note: str = ""

    def __post_init__(self) -> None:
        approver = _text(self.approver, "approver", limit=80)
        if not _HUMAN_RE.fullmatch(approver):
            raise AuthorityRefused(
                "an approval must name a human or independent reviewer (owner:, user:, reviewer:, "
                "security: or independent:); a model, a service or 'the system' cannot approve."
            )
        object.__setattr__(self, "approver", approver)
        if self.approval_type not in APPROVAL_TYPES:
            raise AuthorityRefused(f"unknown approval type {self.approval_type!r}.")
        role = approver.split(":", 1)[0]
        if self.approval_type in ("owner_and_security", "independent_and_owner") and role not in ("security", "independent"):
            raise AuthorityRefused(
                f"approval type {self.approval_type!r} needs a security or independent reviewer; "
                f"{approver!r} is a {role} signature and cannot stand in for one."
            )
        if self.approval_type in ("owner",) and role not in ("owner", "user"):
            raise AuthorityRefused(
                f"approval type 'owner' needs the owner (owner:/user:); {approver!r} is a {role} signature."
            )
        object.__setattr__(self, "approved_digest", _digest(self.approved_digest, "approved_digest"))
        object.__setattr__(self, "note", str(self.note or "").strip()[:600])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "approver": self.approver,
            "approval_type": self.approval_type,
            "approved_digest": self.approved_digest,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class HeldOutSummary:
    """The held-out result a promotion leans on, from the evaluator that produced it.

    ``evaluator_digest`` is what makes "relevant" checkable: results produced by a different evaluator
    version, or on a different candidate revision, are not results for this candidate.
    """

    candidate_digest: str
    evaluator_digest: str
    tasks: int
    passed: int
    thresholds_approved_by: str
    threshold_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_digest", _digest(self.candidate_digest, "candidate_digest"))
        object.__setattr__(self, "evaluator_digest", _digest(self.evaluator_digest, "evaluator_digest"))
        for field_name in ("tasks", "passed"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise EvidenceRefused(f"{field_name} must be a non-negative integer.")
        if self.passed > self.tasks:
            raise EvidenceRefused("a held-out summary cannot pass more tasks than it ran.")
        approver = _text(self.thresholds_approved_by, "thresholds_approved_by", limit=80)
        if not _HUMAN_RE.fullmatch(approver):
            raise EvidenceRefused(
                "thresholds must be approved by a named human; thresholds a candidate chose for itself "
                "are not thresholds."
            )
        object.__setattr__(self, "thresholds_approved_by", approver)
        object.__setattr__(self, "threshold_version", _identifier(self.threshold_version, "threshold_version"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_digest": self.candidate_digest,
            "evaluator_digest": self.evaluator_digest,
            "tasks": self.tasks,
            "passed": self.passed,
            "thresholds_approved_by": self.thresholds_approved_by,
            "threshold_version": self.threshold_version,
        }


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    """A candidate asking to become behaviour, with everything it must declare to be considered."""

    candidate_id: str
    candidate_digest: str
    effects: ChangeEffects
    requested_stage: str
    grants: tuple[str, ...] = ()
    approvals: tuple[Approval, ...] = ()
    held_out: HeldOutSummary | None = None
    rollback_target_digest: str | None = None
    supersedes_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _identifier(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "candidate_digest", _digest(self.candidate_digest, "candidate_digest"))
        if not isinstance(self.effects, ChangeEffects):
            raise ClassificationRefused("a promotion request needs its declared ChangeEffects.")
        if self.requested_stage not in PROMOTION_STAGES:
            raise StageRefused(
                f"unknown stage {self.requested_stage!r}; stages are {list(PROMOTION_STAGES)} and a "
                "promotion reaches them in order."
            )
        object.__setattr__(self, "grants", _texts(self.grants, "grant", limit=200))
        approvals = tuple(self.approvals)
        for item in approvals:
            if not isinstance(item, Approval):
                raise AuthorityRefused("approvals must hold Approval records.")
            if item.approved_digest != self.candidate_digest:
                raise AuthorityRefused(
                    f"an approval names digest {item.approved_digest[:12]}, not this candidate "
                    f"({self.candidate_digest[:12]}); an approval for a different revision is not approval."
                )
        object.__setattr__(self, "approvals", approvals)
        if self.held_out is not None:
            if not isinstance(self.held_out, HeldOutSummary):
                raise EvidenceRefused("held_out must be a HeldOutSummary.")
            if self.held_out.candidate_digest != self.candidate_digest:
                raise EvidenceRefused("the held-out summary is about a different candidate revision.")
        if self.rollback_target_digest is not None:
            object.__setattr__(
                self, "rollback_target_digest", _digest(self.rollback_target_digest, "rollback_target_digest")
            )
        if self.supersedes_digest is not None:
            object.__setattr__(self, "supersedes_digest", _digest(self.supersedes_digest, "supersedes_digest"))

    def classification(self, *, policy: PromotionPolicySnapshot | None = None) -> Classification:
        return classify_change(self.effects, policy=policy)

    def refusals(self, *, policy: PromotionPolicySnapshot | None = None) -> tuple[str, ...]:
        """Everything that stops this request from being promoted, stated as reasons.

        Checked before any stage runs, and re-checked at the point of promotion, because "the checks
        live next to the decision" is the only arrangement in which they cannot be routed around.

        The ceremony is sized by the change's **strongest effect**: a retrieval change whose declared
        effects include execution capability needs the executable class's review, even though the
        classifier files it under the lesson class. Erring toward the stricter boundary is the only
        direction this function may err in.
        """
        snapshot = policy if policy is not None else load_policy()
        classification = self.classification(policy=snapshot)
        reasons: list[str] = []
        boundary = self.required_boundary(effects=self.effects, primary=classification.review_boundary)
        if boundary not in REVIEW_BOUNDARIES:
            reasons.append(f"the review boundary {boundary!r} is not understood")
        automatic_allowed = classification.class_id in snapshot.automatic_classes
        if not automatic_allowed:
            reasons.extend(self._ceremony_gaps(boundary))
        elif self.held_out is None:
            reasons.append(
                f"{classification.class_id} automatic promotion is enabled by the owner, but it still needs "
                "approved thresholds and relevant held-out results; neither was supplied"
            )
        elif self.held_out.tasks == 0:
            reasons.append("the held-out summary ran no tasks; an empty run supports no conclusion")
        if self.held_out is None:
            reasons.append("no held-out result was supplied; a promotion without one is an opinion")
        elif self.held_out.tasks == 0:
            reasons.append("the held-out summary ran no tasks; an empty run supports no conclusion")
        reasons.extend(self._grant_gaps())
        if self.rollback_target_digest is None:
            reasons.append(
                "no rollback target was recorded at promotion time; a rollback path that is decided after "
                "a regression is a guess under pressure"
            )
        if classification.class_id == "authority_evaluator_or_runtime" and self.effects.authority_controls_touched:
            if self.requested_stage not in ("canary", "default"):
                reasons.append(
                    "a change to authority controls cannot be promoted straight to a narrow stage; it needs "
                    "a canary that measures it against the controls it edits"
                )
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def required_boundary(*, effects: "ChangeEffects", primary: str) -> str:
        """The strictest boundary any of this change's effects implies."""
        strongest = primary
        if not isinstance(effects, ChangeEffects):
            return strongest
        if effects.authority_controls_touched:
            strongest = _stricter(strongest, "independent_and_owner")
        if effects.executable_artifacts or set(effects.capabilities_requested) & {"external_mcp", "code_execution"}:
            strongest = _stricter(strongest, "owner_and_security")
        if effects.changes_retrieval_or_prompting or effects.paths_touched:
            strongest = _stricter(strongest, "owner")
        if effects.gated():
            strongest = _stricter(strongest, "owner")
        return strongest

    def _ceremony_gaps(self, boundary: str) -> list[str]:
        """Which of the required human approvals are missing, by required boundary.

        Two-party boundaries need two signatures from two people: an owner who is also the security
        reviewer has provided one opinion, and a review boundary that can be satisfied by one person
        signing twice is not a two-party boundary.
        """
        if any(item.approval_type == "automatic" for item in self.approvals):
            return [
                "an 'automatic' approval cannot approve anything: owner decision U05 keeps automatic "
                "promotion off in every class, so a promotion needs a recorded human ceremony"
            ]
        granted = {item.approval_type for item in self.approvals}
        owner_signers = {item.approver for item in self.approvals if item.approval_type == "owner"}
        review_signers = {
            item.approver for item in self.approvals if item.approval_type in ("owner_and_security", "independent_and_owner")
        }
        missing: list[str] = []
        if boundary in ("owner", "owner_and_security", "independent_and_owner") and not owner_signers:
            missing.append("the owner's approval is required for this change and was not recorded")
        if boundary in ("owner_and_security", "independent_and_owner") and not review_signers:
            missing.append("security review is required for this change and was not recorded")
        if boundary in ("owner_and_security", "independent_and_owner") and owner_signers & review_signers:
            # Unreachable through Approval, which refuses a review role under an owner name. Kept as a
            # guard in case that constructor check is ever relaxed: a two-party boundary signed by one
            # person is one opinion.
            shared = sorted(owner_signers & review_signers)[0]
            missing.append(
                f"{shared!r} signed both the owner and the review side of a two-party boundary; that is "
                "one opinion, not two"
            )
        if boundary == "independent_and_owner" and "independent_and_owner" not in granted:
            missing.append(
                "an independent reviewer is required for a change to authority, evaluators or runtime, and "
                "was not recorded; the proposing side cannot supply it"
            )
        return missing

    def _grant_gaps(self) -> list[str]:
        gated = self.effects.gated()
        if not gated:
            return []
        held = {item.split(":", 1)[1] for item in self.grants if ":" in item}
        missing = [effect for effect in gated if effect not in held]
        if not missing:
            return []
        return [
            "this change introduces new authority (" + ", ".join(missing) + ") and no existing explicit "
            "grant names it; owner approval of a promotion is not a grant for a new egress, account, "
            "spend or default"
        ]

    def to_mapping(self, *, policy: PromotionPolicySnapshot | None = None) -> dict[str, Any]:
        classification = self.classification(policy=policy)
        return {
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "effects": self.effects.to_mapping(),
            "requested_stage": self.requested_stage,
            "classification": classification.to_mapping(),
            "grants": list(self.grants),
            "approvals": [item.to_mapping() for item in self.approvals],
            "held_out": self.held_out.to_mapping() if self.held_out else None,
            "rollback_target_digest": self.rollback_target_digest,
            "supersedes_digest": self.supersedes_digest,
            "refusals": list(self.refusals(policy=policy)),
            "classification_by": "promotion_policy/v1 (operating envelope, owner decision U05)",
            "proposer_declared_risk": None,
        }


# ---------------------------------------------------------------------------
# AC4/AC5 — the stages, the canary, the rollback.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageResult:
    """One stage's measured outcome.

    ``tasks`` of zero is not a pass: a stage that ran nothing has not been observed, and a canary with
    no traffic is the most reassuring-looking way to deploy something untested.
    """

    stage: str
    tasks: int
    regressions: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.stage not in PROMOTION_STAGES:
            raise StageRefused(f"unknown stage {self.stage!r}.")
        if isinstance(self.tasks, bool) or not isinstance(self.tasks, int) or self.tasks < 0:
            raise StageRefused("tasks must be a non-negative integer.")
        object.__setattr__(self, "regressions", _texts(self.regressions, "regression", limit=300))
        object.__setattr__(self, "notes", str(self.notes or "").strip()[:600])

    @property
    def observed(self) -> bool:
        return self.tasks > 0

    @property
    def clean(self) -> bool:
        return self.observed and not self.regressions

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "tasks": self.tasks,
            "regressions": list(self.regressions),
            "observed": self.observed,
            "clean": self.clean,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class CanaryRegression:
    """A regression seen at a stage, with what it invalidates and what it rolls back to.

    ``invalidated_artifacts`` is required to be non-empty when a regression is recorded: a canary that
    failed after producing artifacts leaves those artifacts looking valid, and an artifact accepted
    under a withdrawn candidate is evidence for a version that is no longer deployed.
    """

    stage: str
    detail: str
    invalidated_artifacts: tuple[str, ...] = ()
    bounded: bool = True
    no_downstream_artifacts: bool = False

    def __post_init__(self) -> None:
        if self.stage not in PROMOTION_STAGES:
            raise StageRefused(f"unknown stage {self.stage!r}.")
        object.__setattr__(self, "detail", _text(self.detail, "regression detail", limit=600))
        object.__setattr__(self, "invalidated_artifacts", _texts(self.invalidated_artifacts, "artifact", limit=300))
        if not isinstance(self.bounded, bool):
            raise RollbackRefused("bounded must be a boolean: an unbounded rollback is an incident.")
        if not isinstance(self.no_downstream_artifacts, bool):
            raise RollbackRefused("no_downstream_artifacts must be a boolean.")
        if not self.invalidated_artifacts and not self.no_downstream_artifacts:
            raise RollbackRefused(
                "a regression must either name the downstream artifacts it withdraws or state, as a "
                "separate claim, that none existed; silence is indistinguishable from an oversight."
            )
        if not self.bounded:
            raise RollbackRefused(
                "the rollback must be bounded to the promoted stage; an unbounded rollback that touches "
                "unrelated work turns one regression into two."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "detail": self.detail,
            "invalidated_artifacts": list(self.invalidated_artifacts),
            "bounded": self.bounded,
            "no_downstream_artifacts": self.no_downstream_artifacts,
        }


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """The result of walking the stages: how far it got, what it rolled back, what it invalidated."""

    request: PromotionRequest
    classification: Classification
    results: tuple[StageResult, ...]
    reached_stage: str
    regression: CanaryRegression | None
    rolled_back_to: str | None
    invalidated_artifacts: tuple[str, ...]
    evidence_superseded: bool

    def __post_init__(self) -> None:
        if not isinstance(self.request, PromotionRequest):
            raise StageRefused("a stage outcome needs the PromotionRequest it judged.")
        if not isinstance(self.classification, Classification):
            raise StageRefused("a stage outcome needs its Classification.")
        stages = [item.stage for item in self.results]
        expected = [stage for stage in PROMOTION_STAGES if stage in stages]
        if stages != expected:
            raise StageRefused(
                "stages must be reported once each, in order "
                f"({list(PROMOTION_STAGES)}); got {stages}."
            )
        if self.reached_stage not in PROMOTION_STAGES:
            raise StageRefused(f"unknown reached stage {self.reached_stage!r}.")
        if (
            self.regression is not None
            and not self.regression.invalidated_artifacts
            and not self.regression.no_downstream_artifacts
        ):
            raise RollbackRefused(
                "a regression that invalidates nothing is not recorded honestly: name the downstream "
                "artifacts it withdraws, or state that none existed."
            )

    @property
    def promoted(self) -> bool:
        """Promotion means every stage ran clean. Anything less is not a deployment."""
        return self.regression is None and len(self.results) == len(PROMOTION_STAGES) and all(
            item.clean for item in self.results
        )

    @property
    def status(self) -> str:
        if self.promoted:
            return "promoted"
        if self.regression is not None:
            return "rolled_back"
        return "partial"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": PN_SCHEMA_VERSION,
            "candidate_id": self.request.candidate_id,
            "candidate_digest": self.request.candidate_digest,
            "classification": self.classification.to_mapping(),
            "stages": [item.to_mapping() for item in self.results],
            "reached_stage": self.reached_stage,
            "status": self.status,
            "promoted": self.promoted,
            "regression": self.regression.to_mapping() if self.regression else None,
            "rolled_back_to": self.rolled_back_to,
            "invalidated_artifacts": list(self.invalidated_artifacts),
            "evidence_superseded": self.evidence_superseded,
            "proposer_declared_risk": None,
            "authority_outside_proposer": True,
        }


def run_stages(
    request: PromotionRequest,
    *,
    results: Sequence[StageResult],
    regression: CanaryRegression | None = None,
    policy: PromotionPolicySnapshot | None = None,
) -> StageOutcome:
    """Walk the stages in order and stop at the first regression.

    The request's refusals are re-checked here, not only where the request was built, because the
    promotion decision is the moment that matters. A stage after a regression is ignored rather than
    trusted: a promotion that carried on measuring after it broke something is measuring the wrong
    thing.
    """
    if not isinstance(request, PromotionRequest):
        raise StageRefused("run_stages needs a PromotionRequest.")
    snapshot = policy if policy is not None else load_policy()
    blocked = request.refusals(policy=snapshot)
    if blocked:
        raise AuthorityRefused("this promotion cannot run: " + "; ".join(blocked) + ".")
    classification = request.classification(policy=snapshot)
    ordered = list(results)
    for index, item in enumerate(ordered):
        if not isinstance(item, StageResult):
            raise StageRefused("results must hold StageResult records.")
        if index and item.stage == ordered[index - 1].stage:
            raise StageRefused("a stage was reported twice.")
    stages = [item.stage for item in ordered]
    if stages != [stage for stage in PROMOTION_STAGES if stage in stages]:
        raise StageRefused("stages must arrive in promotion order: " + str(list(PROMOTION_STAGES)) + ".")

    reached = ordered[-1].stage if ordered else request.requested_stage
    invalidated: tuple[str, ...] = ()
    rolled_back_to: str | None = None
    superseded = False
    if regression is not None:
        if regression.stage not in stages:
            raise RollbackRefused(
                f"the regression names stage {regression.stage!r}, which never ran; a rollback for a stage "
                "that was not entered is a story about a deployment that did not happen."
            )
        kept: list[StageResult] = []
        for item in ordered:
            kept.append(item)
            if item.stage == regression.stage:
                break
        ordered = kept
        reached = regression.stage
        rolled_back_to = request.rollback_target_digest
        if rolled_back_to is None:
            raise RollbackRefused("no rollback target was recorded; the promotion cannot be undone honestly.")
        invalidated = regression.invalidated_artifacts
        superseded = request.supersedes_digest is not None or request.held_out is not None
    unobserved = [item.stage for item in ordered if not item.observed]
    if unobserved and regression is None:
        raise StageRefused(
            f"stage {unobserved[0]!r} ran no tasks; a stage nobody observed is not a pass, and a "
            "promotion cannot be built out of unobserved stages."
        )
    return StageOutcome(
        request=request,
        classification=classification,
        results=tuple(ordered),
        reached_stage=reached,
        regression=regression,
        rolled_back_to=rolled_back_to,
        invalidated_artifacts=invalidated,
        evidence_superseded=superseded,
    )


# ---------------------------------------------------------------------------
# AC6 — the ledger: nothing overwritten, nothing migrated underneath a task.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    """One append-only entry: an acceptance, a rollback, or a revocation.

    Every record names the version it concerns and the version it restored, so history is a list
    rather than a current value. There is no ``replace`` operation anywhere in this module for exactly
    that reason.
    """

    record_id: str
    candidate_id: str
    candidate_digest: str
    action: str
    stage: str
    restored_digest: str | None = None
    invalidated_artifacts: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_id", _identifier(self.record_id, "record_id"))
        object.__setattr__(self, "candidate_id", _identifier(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "candidate_digest", _digest(self.candidate_digest, "candidate_digest"))
        if self.action not in ("promoted", "rolled_back", "revoked"):
            raise PromotionError(f"unknown ledger action {self.action!r}.")
        if self.stage not in PROMOTION_STAGES:
            raise StageRefused(f"unknown stage {self.stage!r}.")
        if self.restored_digest is not None:
            object.__setattr__(self, "restored_digest", _digest(self.restored_digest, "restored_digest"))
        object.__setattr__(self, "invalidated_artifacts", _texts(self.invalidated_artifacts, "artifact", limit=300))
        object.__setattr__(self, "detail", str(self.detail or "").strip()[:600])
        if self.action in ("rolled_back", "revoked") and self.restored_digest is None:
            raise RollbackRefused(
                f"a {self.action} record must name the version it restored; an undo with no destination "
                "is an outage."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "action": self.action,
            "stage": self.stage,
            "restored_digest": self.restored_digest,
            "invalidated_artifacts": list(self.invalidated_artifacts),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class PromotionLedger:
    """An append-only history of promotions, rollbacks and revocations.

    ``active_digest`` is derived from the records rather than stored, so a caller cannot assert a
    current version the history does not support. Recoverability is a property of the history: every
    digest that was ever active is still there, which is what "prior configurations remain
    recoverable" means when it is written down.
    """

    records: tuple[PromotionRecord, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for record in self.records:
            if not isinstance(record, PromotionRecord):
                raise PromotionError("records must hold PromotionRecord values.")
            if record.record_id in seen:
                raise PromotionError(
                    f"duplicate ledger record id {record.record_id!r}; an append-only history with a "
                    "reused id has been rewritten."
                )
            seen.add(record.record_id)

    def append(self, record: PromotionRecord) -> "PromotionLedger":
        """Return a new ledger with one more record. The old ledger is unchanged and still valid."""
        if not isinstance(record, PromotionRecord):
            raise PromotionError("append needs a PromotionRecord.")
        if any(existing.record_id == record.record_id for existing in self.records):
            raise PromotionError(f"record {record.record_id!r} already exists; nothing is overwritten here.")
        if record.action == "promoted":
            current = self.active_digest()
            if current is not None and record.restored_digest is None:
                current_id = self.active_candidate_id()
                if record.candidate_digest == current:
                    raise PromotionError("this version is already active; re-promoting it records no change.")
                # A promotion that supersedes an active version must say so, in the record.
                raise PromotionError(
                    f"{current_id} is already active; promote a replacement with a record whose "
                    "restored_digest names the version it supersedes, so the change of default is visible."
                )
        if record.action in ("rolled_back", "revoked"):
            current = self.active_digest()
            if current is not None and record.candidate_digest != current:
                raise RollbackRefused(
                    f"this {record.action} concerns version {record.candidate_digest[:12]}, but "
                    f"{current[:12]} is the active one; an undo aimed at a version that is not deployed "
                    "does not undo anything."
                )
            if current is not None and record.restored_digest == current:
                raise RollbackRefused(
                    "this rollback restores the version that is already active; the record would claim an "
                    "undo that undid nothing."
                )
            if record.restored_digest is not None and not self.is_recoverable(record.restored_digest):
                raise RollbackRefused(
                    f"version {record.restored_digest[:12]} was never promoted by this ledger, so it is not "
                    "recoverable; a rollback target must be a version that existed and worked."
                )
        return PromotionLedger(records=self.records + (record,))

    def active_digest(self) -> str | None:
        digest: str | None = None
        for record in self.records:
            if record.action == "promoted":
                digest = record.candidate_digest
            elif record.action in ("rolled_back", "revoked"):
                digest = record.restored_digest
        return digest

    def active_candidate_id(self) -> str | None:
        active = self.active_digest()
        for record in reversed(self.records):
            if record.candidate_digest == active or record.restored_digest == active:
                return record.candidate_id
        return None

    def recoverable_digests(self) -> tuple[str, ...]:
        digests: list[str] = []
        for record in self.records:
            for value in (record.candidate_digest, record.restored_digest):
                if value is not None and value not in digests:
                    digests.append(value)
        return tuple(digests)

    def is_recoverable(self, digest: str) -> bool:
        return _digest(digest, "digest") in self.recoverable_digests()

    def invalidated_artifacts(self) -> tuple[str, ...]:
        names: list[str] = []
        for record in self.records:
            for artifact in record.invalidated_artifacts:
                if artifact not in names:
                    names.append(artifact)
        return tuple(names)

    def artifact_state(self, artifact: str) -> str:
        """``invalidated`` if any record withdrew it, ``valid`` otherwise.

        Deliberately binary plus ``superseded`` on the record itself: an artifact this ledger never
        mentions is *not* evidence that it is valid, and :meth:`unmentioned` exists so a caller cannot
        quietly read silence as a pass.
        """
        if artifact in self.invalidated_artifacts():
            return "invalidated"
        return "unmentioned"

    def unmentioned(self, artifacts: Iterable[str]) -> tuple[str, ...]:
        """Artifacts this ledger has no record of either way."""
        known = set(self.invalidated_artifacts())
        promoted = set(self.recoverable_digests())
        return tuple(item for item in artifacts if item not in known and item not in promoted)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "records": [record.to_mapping() for record in self.records],
            "active_digest": self.active_digest(),
            "active_candidate_id": self.active_candidate_id(),
            "recoverable_digests": list(self.recoverable_digests()),
            "invalidated_artifacts": list(self.invalidated_artifacts()),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_mapping()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PinRegistry:
    """Which version each in-flight task is pinned to (AC6).

    A promotion changes what *new* work gets. A task that is already running stays on the version it
    started with, so a canary regression mid-task cannot change the behaviour underneath a half-written
    artifact. New tasks pick up the active version; pinned tasks never migrate implicitly, and an
    explicit migration is a recorded decision rather than a side effect of a promotion.
    """

    pins: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        pins: Mapping[str, str] = dict(self.pins or {})
        normalized: dict[str, str] = {}
        for task_id, digest in pins.items():
            normalized[_identifier(task_id, "task_id")] = _digest(digest, "pinned digest")
        object.__setattr__(self, "pins", normalized)

    def pin(self, task_id: str, digest: str) -> tuple["PinRegistry", str | None]:
        """Pin a task, returning the new registry and the version it was pinned to when it first ran."""
        tid = _identifier(task_id, "task_id")
        digest_value = _digest(digest, "digest")
        existing = self._pins().get(tid)
        if existing is not None:
            if existing != digest_value:
                raise RollbackRefused(
                    f"task {tid!r} already started on version {existing[:12]}; pinning it to "
                    f"{digest_value[:12]} mid-flight is the migration this rule forbids."
                )
            return self, existing
        pins = dict(self._pins())
        pins[tid] = digest_value
        return PinRegistry(pins=pins), existing

    def _pins(self) -> Mapping[str, str]:
        return self.pins or {}

    def version_for(self, task_id: str, *, active_digest: str) -> str:
        """The version a task may use: its pin if it has one, the active version otherwise."""
        tid = _identifier(task_id, "task_id")
        return self._pins().get(tid, _digest(active_digest, "active_digest"))

    def in_flight_pinned_to(self, digest: str) -> tuple[str, ...]:
        value = _digest(digest, "digest")
        return tuple(sorted(task for task, pinned in self._pins().items() if pinned == value))

    def to_mapping(self) -> dict[str, Any]:
        return {"pins": dict(sorted(self._pins().items()))}
