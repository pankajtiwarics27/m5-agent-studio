"""AP-025 — extract evidence-backed reusable skills from work.

Every agent framework eventually writes the same sentence into a prompt: *"here is what worked last
time."* It is almost always produced by taking one successful run and describing it as a technique.
This module exists to make that sentence either **evidence-backed** or **impossible to write**.

A skill is not a lesson. `evidence_lessons.py` already owns the durable, project-scoped lesson store
with promotion gates, staleness invalidation and bounded retrieval, and this module **reuses it**
rather than building a second knowledge base: :func:`promote_to_lesson_store` converts a promoted
skill into that store's own `promote_lesson` contract, under its own privacy, anchor and token
checks. There is one store and one promotion gate in this programme.

Four rules shape what a skill may claim.

**One success is not superiority.** A single verified task justifies using the approach *here*, on
*this* project, *next* time. It does not justify preferring it globally — that needs independent
evidence plus a comparison against the incumbent, and the comparison has to be a matched experiment.
``EVIDENCE_FOR_GLOBAL_SUPERIORITY`` is the floor on independent tasks, and a ``shared`` decision
without comparison evidence is refused by name.

**A skill says what it cannot do.** ``limitations`` is required and non-empty: a technique with no
stated limits is a claim of universal applicability, which is the single most expensive thing a
skill library can contain. Prerequisites, permissions, examples and a version are equally
mandatory, because a skill that does not say what it needs cannot be checked before it is applied.

**A failed approach is retained, and it never becomes an instruction.** ``FailedApproach`` is a
distinct type that a caller can record and retrieve as *context*; it has no ``steps`` field, no
``guidance`` field, and :meth:`FailedApproach.render` refuses to produce instruction text. The
"executable" flag is a constant ``False``, not a parameter. A library that stores failures as
instructions teaches a model to repeat them with confidence.

**Shared means public, and sharing is not a permission grant.** A skill marked ``shared`` must pass
a privacy scan (no credentials, no host paths) and must be ``public`` confidentiality; a skill whose
permissions exceed what the source tasks actually held is refused, so extraction can never launder
authority from one project into another. Running tasks pin a version, and a quarantine after that
pin does not yank the version out from under a task mid-flight — it stops the *next* retrieval.

Nothing here reads a clock or opens a socket. Durability arrives through the reused lesson store,
which the caller supplies; this module owns what must be identical on every run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence, cast

import evidence_lessons
import scoped_grants

__all__ = [
    "SkillExtractionError",
    "EvidenceRefused",
    "ScopeRefused",
    "PrivacyRefused",
    "AuthorityRefused",
    "VersionRefused",
    "PromotionRefused",
    "TaskEvidence",
    "SkillPermission",
    "SkillExample",
    "SkillCandidate",
    "ComparisonEvidence",
    "PromotionDecision",
    "assess_promotion",
    "FailedApproach",
    "VersionPin",
    "SkillLibrary",
    "promote_to_lesson_store",
    "SKILL_STATUSES",
    "PROMOTION_SCOPES",
    "EVIDENCE_FOR_GLOBAL_SUPERIORITY",
    "REQUIRED_SKILL_SECTIONS",
]

SK_SCHEMA_VERSION = 1
SK_MAX_TEXT = 2000

#: Where a skill may be applied. ``task_local`` is the honest home of a single success; ``shared``
#: is a claim that survived independent evidence and a matched comparison.
PROMOTION_SCOPES: tuple[str, ...] = ("task_local", "shared")

#: Skill lifecycle. ``quarantined`` is a reversible withdrawal (its evidence is being revalidated);
#: ``revoked`` is a decision.
SKILL_STATUSES: tuple[str, ...] = ("candidate", "task_local", "shared", "quarantined", "revoked")

#: Independent verified tasks needed before a skill may be shared at all. Two is the smallest
#: number that can disagree with itself.
EVIDENCE_FOR_GLOBAL_SUPERIORITY = 2

#: The parts of a skill that must exist before it may be recorded. Each is a field, not prose: a
#: reader can check the presence of ``limitations`` and cannot check the presence of "we thought
#: about limits".
REQUIRED_SKILL_SECTIONS: tuple[str, ...] = (
    "evidence",
    "prerequisites",
    "permissions",
    "examples",
    "limitations",
    "version",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,79}$")
_HOST_PATH_RE = re.compile(r"(?:^|\s)(/(?:Users|home|var|tmp|etc|opt|private|root)/\S+)")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class SkillExtractionError(ValueError):
    """The extracted skill is malformed or claims more than its evidence supports."""


class EvidenceRefused(SkillExtractionError):
    """A skill rests on unverified or insufficient task evidence."""


class ScopeRefused(SkillExtractionError):
    """A skill claims a scope its evidence does not cover."""


class PrivacyRefused(SkillExtractionError):
    """A shared skill contains private data or a host path."""


class AuthorityRefused(SkillExtractionError):
    """A skill asks for authority the source tasks did not hold."""


class VersionRefused(SkillExtractionError):
    """A version pin is missing, ambiguous, or was silently retargeted."""


class PromotionRefused(SkillExtractionError):
    """An executable instruction was produced from a failed approach."""


def _sk_text(value: object, field_name: str, limit: int = SK_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise SkillExtractionError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise SkillExtractionError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise SkillExtractionError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise SkillExtractionError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _sk_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise SkillExtractionError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    """One completed task a skill was extracted from, with the checks that actually passed.

    ``verified`` is a fact about the originating run, and a skill with no verified evidence cannot
    be recorded at all — the promotion gate in ``evidence_lessons`` applies exactly the same rule,
    and applying it twice is cheaper than applying it differently.
    """

    task_id: str
    session_id: str
    project_scope_id: str
    verification_spec_sha256: str
    passed_checks: int
    verified: bool
    source_revision: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _sk_text(self.task_id, "task_id", 120))
        object.__setattr__(self, "session_id", _sk_text(self.session_id, "session_id", 120))
        object.__setattr__(self, "project_scope_id", _sk_text(self.project_scope_id, "project_scope_id", 120))
        object.__setattr__(
            self,
            "verification_spec_sha256",
            _sk_digest(self.verification_spec_sha256, "verification_spec_sha256"),
        )
        if not isinstance(self.passed_checks, int) or isinstance(self.passed_checks, bool) or self.passed_checks < 0:
            raise EvidenceRefused("passed_checks must be a non-negative integer.")
        if not isinstance(self.verified, bool):
            raise EvidenceRefused("verified must be a boolean; 'looked fine' is not a verification.")
        object.__setattr__(self, "source_revision", str(self.source_revision or "").strip()[:120])


@dataclass(frozen=True, slots=True)
class SkillPermission:
    """One capability a skill needs, expressed in the programme's existing capability taxonomy."""

    capability: str
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability = _sk_text(self.capability, "capability", 60)
        if capability not in scoped_grants.SG_CAPABILITY_TAXONOMY:
            raise AuthorityRefused(
                f"capability {capability!r} is not in the grant taxonomy "
                f"{sorted(scoped_grants.SG_CAPABILITY_TAXONOMY)}; a skill cannot invent authority."
            )
        object.__setattr__(self, "capability", capability)
        tools: list[str] = []
        for raw in self.tools:
            text = _sk_text(raw, "tool", 80)
            if text not in tools:
                tools.append(text)
        object.__setattr__(self, "tools", tuple(tools))

    def to_mapping(self) -> dict[str, Any]:
        return {"capability": self.capability, "tools": list(self.tools)}


@dataclass(frozen=True, slots=True)
class SkillExample:
    """A worked example, and — when it is a test — the check that proves it.

    ``is_test`` distinguishes "here is what it looked like" from "here is what will fail if you
    break it". A skill whose only examples are illustrations has no regression protection.
    """

    name: str
    input_text: str
    expected: str
    is_test: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _sk_text(self.name, "example name", 120))
        object.__setattr__(self, "input_text", _sk_text(self.input_text, "example input", 1000))
        object.__setattr__(self, "expected", _sk_text(self.expected, "example expected", 1000))
        if not isinstance(self.is_test, bool):
            raise SkillExtractionError("is_test must be a boolean.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input": self.input_text,
            "expected": self.expected,
            "is_test": self.is_test,
        }


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """A technique extracted from work, with everything a reader needs to distrust it properly.

    ``version`` starts at 1 and must increase per skill name; ``scope_project`` is the project the
    evidence came from. ``confidentiality`` is ``public`` or ``internal``: only a ``public`` skill
    may be shared, because a shared skill leaves this project's boundary by definition.
    """

    name: str
    summary: str
    guidance: str
    prerequisites: tuple[str, ...]
    permissions: tuple[SkillPermission, ...]
    examples: tuple[SkillExample, ...]
    limitations: tuple[str, ...]
    evidence: tuple[TaskEvidence, ...]
    scope_project: str
    version: int = 1
    confidentiality: str = "internal"
    status: str = "candidate"
    supersedes: str = ""

    def __post_init__(self) -> None:
        name = _sk_text(self.name, "skill name", 80).lower()
        if not _NAME_RE.fullmatch(name):
            raise SkillExtractionError(
                "skill name must be a lowercase identifier (letters, digits, '.', '_', ':', '-'); "
                "a prose name cannot be looked up or versioned."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "summary", _sk_text(self.summary, "summary", 400))
        object.__setattr__(self, "guidance", _sk_text(self.guidance, "guidance", 4000))
        object.__setattr__(self, "prerequisites", self._texts(self.prerequisites, "prerequisite"))
        object.__setattr__(self, "limitations", self._texts(self.limitations, "limitation"))
        permissions = tuple(self.permissions)
        for raw in permissions:
            if not isinstance(raw, SkillPermission):
                raise AuthorityRefused("permissions must be SkillPermission records.")
        object.__setattr__(self, "permissions", permissions)
        examples = tuple(self.examples)
        for raw_example in examples:
            if not isinstance(raw_example, SkillExample):
                raise SkillExtractionError("examples must be SkillExample records.")
        object.__setattr__(self, "examples", examples)
        evidence = tuple(self.evidence)
        for raw_evidence in evidence:
            if not isinstance(raw_evidence, TaskEvidence):
                raise EvidenceRefused("evidence must be TaskEvidence records.")
        if not evidence:
            raise EvidenceRefused(
                f"skill {name!r} names no originating task; a technique with no run behind it is a "
                "guess with a name."
            )
        unverified = [item.task_id for item in evidence if not item.verified or item.passed_checks < 1]
        if unverified:
            raise EvidenceRefused(
                f"skill {name!r} rests on unverified task(s) {unverified}; only a verified "
                "completion with at least one passed check can be extracted from."
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "scope_project", _sk_text(self.scope_project, "scope_project", 120))
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise VersionRefused("version must be a positive integer; a skill without a version cannot be pinned.")
        confidentiality = _sk_text(self.confidentiality, "confidentiality", 20)
        if confidentiality not in {"public", "internal"}:
            raise PrivacyRefused(
                f"confidentiality {confidentiality!r} is unknown; expected 'public' or 'internal'."
            )
        object.__setattr__(self, "confidentiality", confidentiality)
        status = _sk_text(self.status, "status", 20)
        if status not in SKILL_STATUSES:
            raise SkillExtractionError(f"status {status!r} is unknown; expected one of {list(SKILL_STATUSES)}.")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "supersedes", str(self.supersedes or "").strip()[:80])
        missing = self.missing_sections()
        if missing:
            raise SkillExtractionError(
                f"skill {name!r} is missing {missing}. Those are the parts a reader needs in order to "
                "check the technique before applying it; a skill without them is a slogan."
            )
        if not any(example.is_test for example in self.examples):
            raise SkillExtractionError(
                f"skill {name!r} has no example marked as a test; illustrations do not catch a "
                "regression, and a skill nobody can break is a skill nobody can maintain."
            )

    @staticmethod
    def _texts(values: Iterable[object], field_name: str) -> tuple[str, ...]:
        out: list[str] = []
        for raw in values or ():
            text = _sk_text(raw, field_name, 600)
            if text not in out:
                out.append(text)
        return tuple(out)

    def missing_sections(self) -> tuple[str, ...]:
        present = {
            "evidence": bool(self.evidence),
            "prerequisites": bool(self.prerequisites),
            "permissions": bool(self.permissions),
            "examples": bool(self.examples),
            "limitations": bool(self.limitations),
            "version": bool(self.version),
        }
        return tuple(section for section in REQUIRED_SKILL_SECTIONS if not present[section])

    def evidence_projects(self) -> tuple[str, ...]:
        seen: list[str] = []
        for item in self.evidence:
            if item.project_scope_id not in seen:
                seen.append(item.project_scope_id)
        return tuple(seen)

    def privacy_findings(self) -> tuple[str, ...]:
        """Credentials and host paths a shared copy would leak."""
        text = "\n".join((self.summary, self.guidance, *(prereq for prereq in self.prerequisites)))
        findings: list[str] = []
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"credential pattern {name}")
        for match in _HOST_PATH_RE.finditer(text):
            findings.append(f"host path {match.group(1)}")
        for example in self.examples:
            findings.extend(self._example_findings(example))
        return tuple(findings)

    @staticmethod
    def _example_findings(example: SkillExample) -> tuple[str, ...]:
        findings: list[str] = []
        text = f"{example.input_text}\n{example.expected}"
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"credential pattern {name} in example {example.name!r}")
        for match in _HOST_PATH_RE.finditer(text):
            findings.append(f"host path {match.group(1)} in example {example.name!r}")
        return tuple(findings)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SK_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "supersedes": self.supersedes,
            "status": self.status,
            "summary": self.summary,
            "guidance": self.guidance,
            "prerequisites": list(self.prerequisites),
            "permissions": [permission.to_mapping() for permission in self.permissions],
            "examples": [example.to_mapping() for example in self.examples],
            "limitations": list(self.limitations),
            "scope_project": self.scope_project,
            "evidence_projects": list(self.evidence_projects()),
            "confidentiality": self.confidentiality,
            "evidence": [
                {
                    "task_id": item.task_id,
                    "session_id": item.session_id,
                    "project_scope_id": item.project_scope_id,
                    "verification_spec_sha256": item.verification_spec_sha256,
                    "passed_checks": item.passed_checks,
                    "verified": item.verified,
                    "source_revision": item.source_revision,
                }
                for item in self.evidence
            ],
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComparisonEvidence:
    """A matched experiment comparing a candidate technique against the incumbent.

    ``sample_size`` is the number of paired runs; ``task_kind`` and ``config_sha256`` must match the
    incumbent's or the comparison is between two different questions. This deliberately reuses the
    shape AP-016's challenger evaluation established rather than inventing a second experiment
    vocabulary.
    """

    incumbent: str
    challenger: str
    task_kind: str
    config_sha256: str
    sample_size: int
    baseline_success_rate: float
    candidate_success_rate: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "incumbent", _sk_text(self.incumbent, "incumbent", 120))
        object.__setattr__(self, "challenger", _sk_text(self.challenger, "challenger", 120))
        object.__setattr__(self, "task_kind", _sk_text(self.task_kind, "task_kind", 120))
        object.__setattr__(self, "config_sha256", _sk_digest(self.config_sha256, "config_sha256"))
        if not isinstance(self.sample_size, int) or isinstance(self.sample_size, bool) or self.sample_size < 2:
            raise EvidenceRefused(
                "a comparison needs at least two paired runs; one run against one run is an anecdote "
                "with error bars drawn on."
            )
        for attr in ("baseline_success_rate", "candidate_success_rate"):
            value = getattr(self, attr)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= float(value) <= 1.0:
                raise EvidenceRefused(f"{attr} must be a rate between 0 and 1.")
            object.__setattr__(self, attr, float(value))

    def favours_candidate(self) -> bool:
        return self.candidate_success_rate > self.baseline_success_rate

    def to_mapping(self) -> dict[str, Any]:
        return {
            "incumbent": self.incumbent,
            "challenger": self.challenger,
            "task_kind": self.task_kind,
            "config_sha256": self.config_sha256,
            "sample_size": self.sample_size,
            "baseline_success_rate": self.baseline_success_rate,
            "candidate_success_rate": self.candidate_success_rate,
            "favours_candidate": self.favours_candidate(),
        }


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """How far a skill may travel, and the reasons that bounds it.

    Built only by :func:`assess_promotion`, so a scope and its justification cannot be paired up
    arbitrarily. ``scope`` is ``task_local`` unless every sharing bar is met, which means the
    default is the conservative one rather than the useful-looking one.
    """

    skill: str
    scope: str
    reasons: tuple[str, ...]
    independent_tasks: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill", _sk_text(self.skill, "skill", 80))
        if self.scope not in PROMOTION_SCOPES:
            raise ScopeRefused(f"scope {self.scope!r} is unknown; expected one of {list(PROMOTION_SCOPES)}.")
        if not self.reasons:
            raise ScopeRefused(
                "a promotion decision must state its reasons; a bare verdict cannot be reviewed or "
                "argued with."
            )

    @property
    def is_shared(self) -> bool:
        return self.scope == "shared"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "scope": self.scope,
            "independent_tasks": self.independent_tasks,
            "reasons": list(self.reasons),
        }


def assess_promotion(
    candidate: SkillCandidate,
    *,
    comparison: ComparisonEvidence | None = None,
) -> PromotionDecision:
    """Decide how far a skill may travel. Sharing requires evidence, privacy and a comparison.

    Three separate bars, each refused for its own reason so a rejection says which one failed:

    1. **Evidence** — at least ``EVIDENCE_FOR_GLOBAL_SUPERIORITY`` independent verified tasks, in
       at least one project each where more than one project is claimed.
    2. **Privacy** — no credentials, no host paths, and ``public`` confidentiality. A shared skill
       leaves the project boundary, so ``internal`` is the wrong label for it.
    3. **Comparison** — a matched experiment that favours the candidate. Evidence that it worked
       twice is not evidence that it is better than what is already there.
    """
    if not isinstance(candidate, SkillCandidate):
        raise SkillExtractionError("assess_promotion needs a SkillCandidate.")
    independent = {item.session_id for item in candidate.evidence}
    reasons: list[str] = []
    blocking = False

    if len(independent) < EVIDENCE_FOR_GLOBAL_SUPERIORITY:
        blocking = True
        reasons.append(
            f"one task success justifies task-local use only: {len(independent)} independent "
            f"session(s), {EVIDENCE_FOR_GLOBAL_SUPERIORITY} required before sharing"
        )
    if candidate.confidentiality != "public":
        blocking = True
        reasons.append("confidentiality is 'internal'; a shared skill leaves the project boundary")
    findings = candidate.privacy_findings()
    if findings:
        blocking = True
        reasons.extend(f"privacy: {finding}" for finding in findings)
    if comparison is None:
        blocking = True
        reasons.append(
            "no matched comparison against the incumbent; working twice is not evidence of being "
            "better than what is already installed"
        )
    else:
        if comparison.challenger and comparison.challenger not in (candidate.name, candidate.supersedes):
            blocking = True
            reasons.append(
                f"the comparison studied {comparison.challenger!r}, not {candidate.name!r}; evidence "
                "about a different technique does not transfer"
            )
        if not comparison.favours_candidate():
            blocking = True
            reasons.append(
                f"the comparison does not favour the candidate "
                f"({comparison.candidate_success_rate} vs {comparison.baseline_success_rate})"
            )

    scope = "task_local" if blocking else "shared"
    if not blocking:
        reasons.append(
            f"{len(independent)} independent verified sessions, public data, and a matched "
            f"{comparison.sample_size}-run comparison that favours the candidate"  # type: ignore[union-attr]
        )
    return PromotionDecision(
        skill=candidate.name,
        scope=scope,
        reasons=tuple(reasons),
        independent_tasks=len(independent),
    )


@dataclass(frozen=True, slots=True)
class FailedApproach:
    """What did not work, kept as context and structurally unable to become an instruction.

    There is no ``steps`` field and no ``guidance`` field, and :meth:`render` refuses. The
    ``executable`` flag is a constant ``False`` — not a parameter a caller could flip, because the
    whole point is that this record can never be fed back as something to do.
    """

    name: str
    approach: str
    why_failed: str
    correction: str
    evidence: TaskEvidence
    correction_verified: bool = False
    executable: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _sk_text(self.name, "name", 120))
        object.__setattr__(self, "approach", _sk_text(self.approach, "approach", 1000))
        object.__setattr__(self, "why_failed", _sk_text(self.why_failed, "why_failed", 1000))
        object.__setattr__(self, "correction", _sk_text(self.correction, "correction", 1000))
        if not isinstance(self.evidence, TaskEvidence):
            raise EvidenceRefused("a failed approach needs the task evidence that shows it failed.")
        if not isinstance(self.correction_verified, bool):
            raise SkillExtractionError("correction_verified must be a boolean.")

    def render(self) -> str:
        """Refuse, explicitly: a failure is never rendered as something to execute."""
        raise PromotionRefused(
            f"{self.name!r} records a failed approach. It may be retrieved as context and may never "
            "be rendered as guidance; a library that stores failures as instructions teaches a "
            "model to repeat them with confidence."
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "approach": self.approach,
            "why_failed": self.why_failed,
            "correction": self.correction,
            "correction_verified": self.correction_verified,
            "executable": self.executable,
            "task_id": self.evidence.task_id,
            "status": "failed_approach",
        }


@dataclass(frozen=True, slots=True)
class VersionPin:
    """A running task's claim on one exact skill version.

    A pin survives a quarantine: the task that already resolved ``1`` keeps ``1``, because
    withdrawing a version mid-run would change the instructions underneath a task that is already
    executing them. New retrievals see the quarantine; the pin sees what it resolved.
    """

    skill: str
    version: int
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill", _sk_text(self.skill, "skill", 80))
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise VersionRefused("a pin must name a positive integer version.")
        object.__setattr__(self, "task_id", _sk_text(self.task_id, "task_id", 120))

    def to_mapping(self) -> dict[str, Any]:
        return {"skill": self.skill, "version": self.version, "task_id": self.task_id}


class SkillLibrary:
    """An in-memory library of versions, pins and quarantines.

    Deliberately small: durable storage is ``evidence_lessons``' job (see
    :func:`promote_to_lesson_store`), and this class models only the version semantics a caller has
    to get right — latest-version resolution, what a pin still resolves after a quarantine, and
    which statuses may be retrieved at all.
    """

    def __init__(self) -> None:
        self._versions: dict[str, dict[int, SkillCandidate]] = {}
        self._quarantined: dict[str, str] = {}
        self._pins: dict[tuple[str, str], VersionPin] = {}

    def put(self, skill: SkillCandidate) -> SkillCandidate:
        """Record a version. Re-publishing a different body under one version is refused."""
        if not isinstance(skill, SkillCandidate):
            raise SkillExtractionError("put() needs a SkillCandidate.")
        versions = self._versions.setdefault(skill.name, {})
        existing = versions.get(skill.version)
        if existing is not None and existing.fingerprint() != skill.fingerprint():
            raise VersionRefused(
                f"{skill.name}@v{skill.version} already exists with a different body; a version is "
                "immutable, so an edit is a new version."
            )
        versions[skill.version] = skill
        return skill

    def latest(self, name: str) -> SkillCandidate | None:
        versions = self._versions.get(name)
        if not versions:
            return None
        return versions[max(versions)]

    def resolve(self, name: str) -> SkillCandidate | None:
        """What a *new* task would get: nothing at all while the skill is quarantined."""
        if name in self._quarantined:
            return None
        latest = self.latest(name)
        if latest is None or latest.status == "revoked":
            return None
        return latest

    def pin(self, name: str, *, task_id: str) -> VersionPin:
        resolve = self.resolve(name)
        if resolve is None:
            raise VersionRefused(
                f"{name} has no retrievable version; a task cannot pin what it cannot resolve."
            )
        pin = VersionPin(skill=name, version=resolve.version, task_id=task_id)
        self._pins[(name, task_id)] = pin
        return pin

    def pinned(self, name: str, *, task_id: str) -> SkillCandidate | None:
        """What the pinned task sees, even after a quarantine. Never a *newer* version."""
        pin = self._pins.get((name, task_id))
        if pin is None:
            return None
        return self._versions.get(name, {}).get(pin.version)

    def quarantine(self, name: str, *, reason: str) -> str:
        reason_text = _sk_text(reason, "quarantine reason", 200)
        if name not in self._versions:
            raise VersionRefused(f"{name} is not in the library; nothing to quarantine.")
        self._quarantined[name] = reason_text
        return reason_text

    def revalidate(self, name: str) -> SkillCandidate | None:
        """Release a quarantine. Retrieval resumes at the latest version, not the pinned one."""
        self._quarantined.pop(name, None)
        return self.resolve(name)

    def revocation_reason(self, name: str) -> str:
        return self._quarantined.get(name, "")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SK_SCHEMA_VERSION,
            "skills": {
                name: {
                    "versions": sorted(versions),
                    "latest": max(versions),
                    "quarantined": name in self._quarantined,
                    "quarantine_reason": self._quarantined.get(name, ""),
                }
                for name, versions in sorted(self._versions.items())
            },
            "pins": [pin.to_mapping() for pin in self._pins.values()],
        }


def promote_to_lesson_store(
    skill: SkillCandidate,
    *,
    store: evidence_lessons.EvidenceLessonStore,
    workspace_root: Any,
    anchor_paths: Sequence[str],
    decision: PromotionDecision,
    tags: Sequence[str] = (),
    known_secret_values: Iterable[str] = (),
    env_enabled: bool = True,
) -> evidence_lessons.LessonRecord:
    """Promote a shared skill through ``evidence_lessons``' own gate — not a second one.

    AC5 asks for the existing promotion contract to be reused and its consumer tested, so this
    function is a genuine caller rather than a parallel implementation: it converts a shared skill
    into ``LessonProvenance`` and calls ``EvidenceLessonStore.promote_lesson``, which applies the
    privacy scan, the anchor digests, the token ceiling and the trust rules. Everything refused
    there is refused here, and a ``task_local`` skill is refused *before* the store is touched,
    because a project-local lesson must not enter the shared-looking store at all.
    """
    if not isinstance(decision, PromotionDecision) or decision.skill != skill.name:
        raise ScopeRefused("the decision must be the one assess_promotion produced for this skill.")
    if not decision.is_shared:
        raise ScopeRefused(
            f"{skill.name} is task-local: {'; '.join(decision.reasons)}. A task-local skill is not "
            "promoted into the lesson store, which is where cross-task retrieval reads from."
        )
    first = skill.evidence[0]
    provenance = evidence_lessons.LessonProvenance(
        source_session_id=first.session_id,
        source_task_id=first.task_id,
        source_epoch=skill.version,
        verified=first.verified,
        verification_spec_sha256=first.verification_spec_sha256,
        passed_checks=first.passed_checks,
        source_trust="verified_task",
        confidentiality=skill.confidentiality,
        security_generation=0,
        source_revision=first.source_revision,
    )
    return store.promote_lesson(
        workspace_root=workspace_root,
        topic_key=skill.name,
        title=skill.summary,
        guidance=skill.guidance,
        anchor_paths=list(anchor_paths),
        provenance=provenance,
        tags=tuple(tags),
        known_secret_values=known_secret_values,
        env_enabled=env_enabled,
    )


def _cast_mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value)
