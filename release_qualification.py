"""AP-032 — qualify the production release and the independent-agent handoff.

A release is not a date; it is a set of claims, each with the evidence that supports it and the scope
that evidence covers. This module builds that pack as a value and refuses to produce one that claims
more than was tested.

Six rules, each closing a way a release pack lies without anyone intending it:

1. **Scope is an intersection, not a wish.** A claim is *published* only where a passing piece of
   evidence covers its whole scope. ``Claim`` carries the scope it asserts and the evidence id that
   supports it; a claim whose evidence is narrower than the claim is refused at construction, and a
   claim with no evidence is refused outright. Nothing is trimmed silently: :attr:`QualificationPack.
   published_claims` shows exactly what survives, and :attr:`uncovered_claims` shows what did not.

2. **A deferred specialisation is a ledger fact, not a hole.** If only one specialisation ships, the
   other must appear in :class:`DeferredSpecialisation` with an authorised revision naming a human
   owner and a reason. A specialisation that is neither qualified nor deferred **blocks** the release —
   an unmentioned pilot is the fake completion this story exists to prevent.

3. **Preservation is verified, never assumed.** Every preserved surface carries an explicit verdict;
   there is no default. A surface with no verdict blocks, and a surface reported as missing blocks even
   when the rest of the pack is green.

4. **Held-out cases are versioned and were not tuned here.** A :class:`CaseSuite` names a version and
   declares whether it was used to tune the implementation. A suite that was tuned on cannot be
   presented as held-out evidence for the same version; re-labelling it is what this field refuses.

5. **An approval is real, current and not the author's.** :class:`Approval` refuses the author as
   approver, refuses an agent as approver, and expires. Independent sign-off cannot be substituted by
   the author's own evidence, however thorough.

6. **CI green means green at the head being shipped.** A green run at another commit is a different
   build; :class:`CiVerdict` refuses to qualify a head it did not run against.

Plus the drills — rollback, migration and restore — each recorded as *exercised*, and restore only
counts when the restored bytes were compared with the source. Artifacts are checked for secrets and
licence before they are named in the pack.

Pure stdlib, no clock of its own (``now`` is required), no filesystem, no network. It renders nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ReleaseQualificationError",
    "ClaimRefused",
    "DeferralRefused",
    "PreservationRefused",
    "SuiteRefused",
    "ApprovalRefused",
    "CiRefused",
    "DrillRefused",
    "ArtifactRefused",
    "SPECIALISATIONS",
    "VERDICTS",
    "DRILLS",
    "RESTORE_COMPARISONS",
    "APPROVAL_KINDS",
    "Evidence",
    "Claim",
    "DeferredSpecialisation",
    "PreservationSurface",
    "CaseSuite",
    "WorkflowQualification",
    "Approval",
    "CiVerdict",
    "DrillRecord",
    "ArtifactCheck",
    "QualificationPack",
    "qualify_release",
]

RQ_SCHEMA_VERSION = 1
RQ_MAX_TEXT = 1200
RQ_DEFAULT_APPROVAL_TTL_SECONDS = 30 * 24 * 60 * 60

#: The two owner-selectable launch specialisations. Ship one, defer the other in the ledger.
SPECIALISATIONS: tuple[str, ...] = ("website", "video")

#: Every preserved surface must carry one of these; there is no default.
VERDICTS: tuple[str, ...] = ("verified", "partially-verified", "missing", "not-applicable")

#: The drills a release must actually perform, not document.
DRILLS: tuple[str, ...] = ("rollback", "migration", "restore")

#: What a restore's digests are taken over. A SQLite restore is logically identical but not
#: byte-identical, so the basis has to be named rather than assumed.
RESTORE_COMPARISONS: tuple[str, ...] = ("byte-identical", "logical-content")

#: Separate independent approvals. Screenshot review is not a security review.
APPROVAL_KINDS: tuple[str, ...] = ("security", "screenshot")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HUMAN_RE = re.compile(r"^(owner|user|reviewer):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: A scope is a set of capability names. ``*`` covers everything; nothing else does.
SCOPE_WILDCARD = "*"

_AGENT_RE = re.compile(
    r"\b(claude|chatgpt|gpt-?[0-9]|gemini|grok|qwen|kimi|copilot|cursor|ai\s+agent|autonomous\s+agent|"
    r"self-review|author)\b",
    re.IGNORECASE,
)


class ReleaseQualificationError(ValueError):
    """The pack is malformed or claims more than its evidence covers."""


class ClaimRefused(ReleaseQualificationError):
    """A claim is unsupported, or wider than the evidence behind it."""


class DeferralRefused(ReleaseQualificationError):
    """A deferral is unauthorised or describes something that was never selected."""


class PreservationRefused(ReleaseQualificationError):
    """A preserved surface has no verdict, or its verdict contradicts the pack."""


class SuiteRefused(ReleaseQualificationError):
    """A case suite is unversioned, or presents tuned cases as held-out evidence."""


class ApprovalRefused(ReleaseQualificationError):
    """An approval is self-signed, agent-authored, wrong-kind or stale."""


class CiRefused(ReleaseQualificationError):
    """A CI verdict does not belong to the head being shipped."""


class DrillRefused(ReleaseQualificationError):
    """A rollback, migration or restore drill was not actually exercised."""


class ArtifactRefused(ReleaseQualificationError):
    """A shipped artifact carries a secret, an unknown licence or no digest."""


def _text(value: object, name: str, *, limit: int = RQ_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ReleaseQualificationError(f"{name} must be a string.")
    if "\x00" in value:
        raise ReleaseQualificationError(f"{name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise ReleaseQualificationError(f"{name} must not be empty.")
    if len(stripped) > limit:
        raise ReleaseQualificationError(f"{name} must be at most {limit} characters.")
    return stripped


def _optional(value: object, *, limit: int = RQ_MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        raise ReleaseQualificationError(f"text must be at most {limit} characters.")
    return text


def _identifier(value: object, name: str) -> str:
    text = _text(value, name, limit=120)
    if not _ID_RE.fullmatch(text):
        raise ReleaseQualificationError(f"{name} must be a bounded opaque identifier.")
    return text


def _sha(value: object, name: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(text):
        raise ReleaseQualificationError(f"{name} must be a full 40-character lowercase commit SHA.")
    return text


def _digest(value: object, name: str) -> str:
    text = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(text):
        raise ReleaseQualificationError(f"{name} must be a lowercase SHA-256 hex digest.")
    return text


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReleaseQualificationError(f"{name} must be an integer of at least {minimum}.")
    return value


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _scope(value: Iterable[str], name: str) -> tuple[str, ...]:
    items = tuple(
        dict.fromkeys(
            _text(item, name, limit=80) if str(item) == SCOPE_WILDCARD else _identifier(item, name) for item in value
        )
    )
    if not items:
        raise ClaimRefused(f"{name} must name at least one capability; an empty scope covers nothing.")
    return items


def _covers(evidence_scope: Sequence[str], claim_scope: Sequence[str]) -> bool:
    if SCOPE_WILDCARD in evidence_scope:
        return True
    return set(claim_scope).issubset(set(evidence_scope))


# ---------------------------------------------------------------------------
# Evidence and claims.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """One measured result, with the scope it actually covers and the head it ran at."""

    evidence_id: str
    label: str
    kind: str
    scope: tuple[str, ...]
    head_sha: str
    passed: bool
    exclusions: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _identifier(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "label", _text(self.label, "label", limit=200))
        kind = _text(self.kind, "kind", limit=60)
        if kind not in ("held-out", "hostile", "failure", "restart", "resource", "review", "ci", "manual"):
            raise ReleaseQualificationError(f"unknown evidence kind {kind!r}.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scope", _scope(self.scope, "evidence scope"))
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "evidence head_sha"))
        if not isinstance(self.passed, bool):
            raise ReleaseQualificationError("passed must be a boolean.")
        object.__setattr__(
            self, "exclusions", tuple(dict.fromkeys(str(item).strip() for item in self.exclusions if str(item).strip()))
        )
        object.__setattr__(self, "detail", _optional(self.detail, limit=800))

    def covers(self, claim_scope: Sequence[str]) -> bool:
        return _covers(self.scope, claim_scope)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "label": self.label,
            "kind": self.kind,
            "scope": list(self.scope),
            "head_sha": self.head_sha,
            "passed": self.passed,
            "exclusions": list(self.exclusions),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """Something the release says it can do, and the evidence that says so.

    The scope check lives in the constructor because a claim wider than its evidence is not a
    documentation problem — it is the release lying about what was tested.
    """

    claim_id: str
    statement: str
    scope: tuple[str, ...]
    evidence_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _identifier(self.claim_id, "claim_id"))
        object.__setattr__(self, "statement", _text(self.statement, "statement", limit=400))
        object.__setattr__(self, "scope", _scope(self.scope, "claim scope"))
        object.__setattr__(self, "evidence_id", _identifier(self.evidence_id, "evidence_id"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "scope": list(self.scope),
            "evidence_id": self.evidence_id,
        }


# ---------------------------------------------------------------------------
# AC1 — a deferred specialisation is an authorised ledger fact.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeferredSpecialisation:
    """A pilot that is not shipping, recorded as a revision rather than left out of the pack."""

    specialisation: str
    revision_id: str
    authorised_by: str
    reason: str
    authorised_at: int

    def __post_init__(self) -> None:
        if self.specialisation not in SPECIALISATIONS:
            raise DeferralRefused(
                f"unknown specialisation {self.specialisation!r}; specialisations are {list(SPECIALISATIONS)}"
            )
        object.__setattr__(self, "revision_id", _identifier(self.revision_id, "revision_id"))
        authoriser = _text(self.authorised_by, "authorised_by", limit=80)
        if not _HUMAN_RE.fullmatch(authoriser):
            raise DeferralRefused(
                "a deferral must be authorised by a named human (owner:, user: or reviewer:); deferring a pilot "
                "is an owner decision, not an implementation convenience"
            )
        if authoriser.startswith("reviewer:"):
            raise DeferralRefused(
                "a deferral is an owner decision; a reviewer may gate quality but cannot decide what ships"
            )
        object.__setattr__(self, "authorised_by", authoriser)
        object.__setattr__(self, "reason", _text(self.reason, "reason", limit=600))
        object.__setattr__(self, "authorised_at", _integer(self.authorised_at, "authorised_at"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "specialisation": self.specialisation,
            "revision_id": self.revision_id,
            "authorised_by": self.authorised_by,
            "reason": self.reason,
            "authorised_at": self.authorised_at,
            "status": "deferred",
        }


# ---------------------------------------------------------------------------
# AC2 — preservation is verified, never assumed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreservationSurface:
    """One previously shipped surface and whether it still works.

    ``verdict`` is required and has no default: a surface nobody checked cannot enter the pack as
    though it were fine.
    """

    surface_id: str
    label: str
    verdict: str
    evidence_id: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_id", _identifier(self.surface_id, "surface_id"))
        object.__setattr__(self, "label", _text(self.label, "label", limit=200))
        if self.verdict not in VERDICTS:
            raise PreservationRefused(
                f"unknown preservation verdict {self.verdict!r}; verdicts are {list(VERDICTS)}"
            )
        if self.verdict == "not-applicable" and not _optional(self.detail):
            raise PreservationRefused(
                f"{self.surface_id!r} was declared not-applicable with no reason; 'not applicable' is a claim "
                "about a surface and needs an argument"
            )
        if self.verdict in ("verified", "partially-verified") and not self.evidence_id:
            raise PreservationRefused(
                f"{self.surface_id!r} is reported {self.verdict!r} with no evidence; a verdict without an "
                "observation is an opinion"
            )
        object.__setattr__(self, "evidence_id", _optional(self.evidence_id, limit=120))
        object.__setattr__(self, "detail", _optional(self.detail, limit=600))

    @property
    def intact(self) -> bool:
        return self.verdict in ("verified", "not-applicable")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "label": self.label,
            "verdict": self.verdict,
            "evidence_id": self.evidence_id,
            "detail": self.detail,
            "intact": self.intact,
        }


# ---------------------------------------------------------------------------
# AC3 — versioned held-out, hostile, failure, restart and resource cases.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseSuite:
    """A versioned case set, and whether it was used to tune the thing it now grades."""

    suite_id: str
    version: str
    kind: str
    workflow: str
    cases: int
    passed: int
    tuned_against_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "suite_id", _identifier(self.suite_id, "suite_id"))
        object.__setattr__(self, "version", _text(self.version, "version", limit=60))
        kind = _text(self.kind, "kind", limit=40)
        if kind not in ("held-out", "hostile", "failure", "restart", "resource"):
            raise SuiteRefused(
                f"unknown case kind {kind!r}; kinds are held-out, hostile, failure, restart, resource"
            )
        object.__setattr__(self, "kind", kind)
        if self.workflow not in SPECIALISATIONS:
            raise SuiteRefused(f"unknown workflow {self.workflow!r}; workflows are {list(SPECIALISATIONS)}")
        object.__setattr__(self, "cases", _integer(self.cases, "cases", minimum=1))
        object.__setattr__(self, "passed", _integer(self.passed, "passed"))
        if self.passed > self.cases:
            raise SuiteRefused("a suite cannot pass more cases than it ran.")
        tuned = _optional(self.tuned_against_version, limit=60)
        if kind == "held-out" and tuned:
            raise SuiteRefused(
                f"suite {self.suite_id!r} was tuned against {tuned!r} and cannot also be held-out evidence for "
                f"{self.version!r}; a suite that shaped the implementation does not grade it"
            )
        object.__setattr__(self, "tuned_against_version", tuned)

    @property
    def held_out(self) -> bool:
        return self.kind == "held-out" and not self.tuned_against_version

    @property
    def clean(self) -> bool:
        return self.passed == self.cases

    def to_mapping(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "version": self.version,
            "kind": self.kind,
            "workflow": self.workflow,
            "cases": self.cases,
            "passed": self.passed,
            "failed": self.cases - self.passed,
            "tuned_against_version": self.tuned_against_version,
            "held_out": self.held_out,
            "clean": self.clean,
        }


@dataclass(frozen=True, slots=True)
class WorkflowQualification:
    """One selected workflow: its suites, its artifact review and the version it was graded at."""

    workflow: str
    suites: tuple[CaseSuite, ...]
    artifact_review_evidence_id: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if self.workflow not in SPECIALISATIONS:
            raise SuiteRefused(f"unknown workflow {self.workflow!r}.")
        suites = tuple(self.suites)
        if not suites:
            raise SuiteRefused(
                f"workflow {self.workflow!r} has no cases; a qualification with no cases is a launch date"
            )
        for suite in suites:
            if not isinstance(suite, CaseSuite):
                raise SuiteRefused("suites must hold CaseSuite values.")
            if suite.workflow != self.workflow:
                raise SuiteRefused(
                    f"suite {suite.suite_id!r} belongs to {suite.workflow!r} but is filed under {self.workflow!r}"
                )
        versions = {suite.version for suite in suites}
        if len(versions) != 1:
            raise SuiteRefused(
                f"workflow {self.workflow!r} mixes case versions {sorted(versions)}; a qualification is against "
                "one version, or it is against none"
            )
        object.__setattr__(self, "suites", suites)
        object.__setattr__(self, "artifact_review_evidence_id", _identifier(self.artifact_review_evidence_id, "evidence_id"))
        object.__setattr__(self, "artifact_digest", _digest(self.artifact_digest, "artifact_digest"))

    @property
    def version(self) -> str:
        """The one case version this workflow was graded against."""
        return self.suites[0].version

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(sorted({suite.kind for suite in self.suites}))

    @property
    def missing_families(self) -> tuple[str, ...]:
        return tuple(kind for kind in ("held-out", "hostile", "failure", "restart", "resource") if kind not in self.families)

    @property
    def failures(self) -> int:
        return sum(suite.cases - suite.passed for suite in self.suites)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "version": self.version,
            "families": list(self.families),
            "missing_families": list(self.missing_families),
            "cases": sum(suite.cases for suite in self.suites),
            "failures": self.failures,
            "artifact_review_evidence_id": self.artifact_review_evidence_id,
            "artifact_digest": self.artifact_digest,
            "suites": [suite.to_mapping() for suite in self.suites],
        }


# ---------------------------------------------------------------------------
# AC4 — real, current, independent approvals.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approval:
    """An independent sign-off for one gate, with a timestamp and an explicit expiry.

    Three refusals, all about substitution: the author cannot approve their own work, an agent name
    cannot appear as the approver, and a stale approval is not current.
    """

    kind: str
    approver: str
    author: str
    approved_at: int
    evidence_digest: str
    ttl_seconds: int = RQ_DEFAULT_APPROVAL_TTL_SECONDS
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in APPROVAL_KINDS:
            raise ApprovalRefused(f"unknown approval kind {self.kind!r}; kinds are {list(APPROVAL_KINDS)}")
        approver = _text(self.approver, "approver", limit=80)
        author = _text(self.author, "author", limit=80)
        if approver == author:
            raise ApprovalRefused(
                f"the author ({author!r}) cannot sign the {self.kind} approval for their own release; "
                "author evidence is not independent acceptance"
            )
        if not _HUMAN_RE.fullmatch(approver):
            raise ApprovalRefused(
                f"approver {approver!r} is not a named human reviewer; an approval needs a person who can be asked"
            )
        if _AGENT_RE.search(approver):
            raise ApprovalRefused(f"approver {approver!r} reads as an agent; independence is a human property here")
        object.__setattr__(self, "approver", approver)
        object.__setattr__(self, "author", author)
        object.__setattr__(self, "approved_at", _integer(self.approved_at, "approved_at"))
        object.__setattr__(self, "evidence_digest", _digest(self.evidence_digest, "evidence_digest"))
        object.__setattr__(self, "ttl_seconds", _integer(self.ttl_seconds, "ttl_seconds", minimum=1))
        object.__setattr__(self, "note", _optional(self.note, limit=600))

    def is_current(self, now: int) -> bool:
        return 0 <= _integer(now, "now") - self.approved_at <= self.ttl_seconds

    def to_mapping(self, *, now: int) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "approver": self.approver,
            "author": self.author,
            "approved_at": self.approved_at,
            "expires_at": self.approved_at + self.ttl_seconds,
            "current": self.is_current(now),
            "evidence_digest": self.evidence_digest,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# AC5 — CI green at the head being shipped.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CiVerdict:
    """The CI result for one commit, which must be the commit in the pack."""

    head_sha: str
    run_id: int
    conclusion: str
    url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "run_id", _integer(self.run_id, "run_id", minimum=1))
        conclusion = _text(self.conclusion, "conclusion", limit=40)
        if conclusion not in ("success", "failure", "cancelled", "timed_out", "neutral", "skipped"):
            raise CiRefused(f"unknown CI conclusion {conclusion!r}.")
        object.__setattr__(self, "conclusion", conclusion)
        object.__setattr__(self, "url", _optional(self.url, limit=400))

    def qualifies(self, head_sha: str) -> bool:
        return self.conclusion == "success" and self.head_sha == head_sha

    def to_mapping(self) -> dict[str, Any]:
        return {
            "head_sha": self.head_sha,
            "run_id": self.run_id,
            "conclusion": self.conclusion,
            "url": self.url,
        }


# ---------------------------------------------------------------------------
# AC6 — drills were exercised; artifacts are clean.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrillRecord:
    """A rollback, migration or restore drill. A restore counts only when it was compared.

    ``comparison`` names *what* the digests are taken over, because that changes the answer and
    the wrong choice reports a false failure. A SQLite backup restored through the online backup
    API is logically identical but **not** byte-identical: page layout, freelist state and header
    fields legitimately differ, so two ``sha256`` file digests of the same data will not match.
    Measuring this repository's own ``backup_database``/``restore_database`` round-trip gives
    equal logical digests and unequal file digests. Recording a file-digest mismatch as a failed
    restore would therefore block a release for a correct restore; recording a logical match as a
    byte match would overclaim. Both are refused by naming the basis.
    """

    kind: str
    exercised: bool
    restored_digest: str = ""
    source_digest: str = ""
    comparison: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in DRILLS:
            raise DrillRefused(f"unknown drill {self.kind!r}; drills are {list(DRILLS)}")
        if not isinstance(self.exercised, bool):
            raise DrillRefused("exercised must be a boolean.")
        restored = _optional(self.restored_digest, limit=80).lower()
        source = _optional(self.source_digest, limit=80).lower()
        comparison = _optional(self.comparison, limit=40)
        if comparison and comparison not in RESTORE_COMPARISONS:
            raise DrillRefused(
                f"unknown restore comparison {comparison!r}; comparisons are {list(RESTORE_COMPARISONS)}"
            )
        if self.kind == "restore" and self.exercised:
            if not restored or not source:
                raise DrillRefused(
                    "a restore drill that was not compared against the source proves only that a command ran; "
                    "record both digests"
                )
            if not _DIGEST_RE.fullmatch(restored) or not _DIGEST_RE.fullmatch(source):
                raise DrillRefused("restore digests must be lowercase SHA-256 hex digests.")
            if not comparison:
                raise DrillRefused(
                    "a restore drill must name what its digests are taken over (byte-identical or "
                    "logical-content); without it the reader cannot tell a real mismatch from the expected "
                    "difference between two SQLite files holding the same rows"
                )
        object.__setattr__(self, "restored_digest", restored)
        object.__setattr__(self, "source_digest", source)
        object.__setattr__(self, "comparison", comparison)
        object.__setattr__(self, "detail", _optional(self.detail, limit=600))

    @property
    def proves_restore(self) -> bool:
        return (
            self.kind == "restore"
            and self.exercised
            and bool(self.restored_digest)
            and bool(self.comparison)
            and self.restored_digest == self.source_digest
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "exercised": self.exercised,
            "restored_digest": self.restored_digest,
            "source_digest": self.source_digest,
            "comparison": self.comparison,
            "detail": self.detail,
            "proves_restore": self.proves_restore,
        }


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    """One shipped artifact, its digest, its licence and whether a secret scan found anything."""

    artifact_id: str
    path: str
    digest: str
    licence: str
    secret_findings: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "path", _text(self.path, "path", limit=400))
        object.__setattr__(self, "digest", _digest(self.digest, "digest"))
        licence = _text(self.licence, "licence", limit=80)
        if licence.lower() in ("unknown", "none", "unlicensed", "tbd", "proprietary-uncleared"):
            raise ArtifactRefused(
                f"artifact {self.artifact_id!r} has licence {licence!r}; an unlicensed output cannot ship"
            )
        object.__setattr__(self, "licence", licence)
        object.__setattr__(self, "secret_findings", _integer(self.secret_findings, "secret_findings"))

    @property
    def clean(self) -> bool:
        return self.secret_findings == 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "digest": self.digest,
            "licence": self.licence,
            "secret_findings": self.secret_findings,
            "clean": self.clean,
        }


# ---------------------------------------------------------------------------
# The pack.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualificationPack:
    """Everything the release asserts, and every reason it may not."""

    head_sha: str
    base_sha: str
    claims: tuple[Claim, ...]
    evidence: tuple[Evidence, ...]
    selected: tuple[str, ...]
    deferred: tuple[DeferredSpecialisation, ...]
    preservation: tuple[PreservationSurface, ...]
    workflows: tuple[WorkflowQualification, ...]
    approvals: tuple[Approval, ...]
    ci: CiVerdict
    drills: tuple[DrillRecord, ...]
    artifacts: tuple[ArtifactCheck, ...]
    now: int
    author: str = "arena:agent"

    def __post_init__(self) -> None:
        object.__setattr__(self, "head_sha", _sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "base_sha", _sha(self.base_sha, "base_sha"))
        if self.head_sha == self.base_sha:
            raise ReleaseQualificationError(
                "base and head are the same commit; a release compares what changed with what did not"
            )
        object.__setattr__(self, "now", _integer(self.now, "now"))
        object.__setattr__(self, "author", _text(self.author, "author", limit=80))
        evidence = tuple(self.evidence)
        by_id = {item.evidence_id: item for item in evidence}
        if len(by_id) != len(evidence):
            raise ReleaseQualificationError("an evidence id is used twice.")
        claims = tuple(self.claims)
        for claim in claims:
            if not isinstance(claim, Claim):
                raise ReleaseQualificationError("claims must hold Claim values.")
            support = by_id.get(claim.evidence_id)
            if support is None:
                raise ClaimRefused(
                    f"claim {claim.claim_id!r} cites evidence {claim.evidence_id!r}, which the pack does not contain"
                )
            if not support.covers(claim.scope):
                raise ClaimRefused(
                    f"claim {claim.claim_id!r} asserts {list(claim.scope)} but its evidence {support.evidence_id!r} "
                    f"only covers {list(support.scope)}; a claim may not be wider than what was tested"
                )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "claims", claims)
        selected = tuple(dict.fromkeys(self.selected))
        for name in selected:
            if name not in SPECIALISATIONS:
                raise ReleaseQualificationError(f"unknown selected specialisation {name!r}.")
        object.__setattr__(self, "selected", selected)
        deferred = tuple(self.deferred)
        names = [item.specialisation for item in deferred]
        if len(set(names)) != len(names):
            raise DeferralRefused("a specialisation is deferred twice.")
        overlap = set(names) & set(selected)
        if overlap:
            raise DeferralRefused(
                f"{sorted(overlap)} is both selected for launch and deferred; one revision decides, not two"
            )
        object.__setattr__(self, "deferred", deferred)
        object.__setattr__(self, "preservation", tuple(self.preservation))
        object.__setattr__(self, "workflows", tuple(self.workflows))
        approvals = tuple(self.approvals)
        kinds = [item.kind for item in approvals]
        if len(set(kinds)) != len(kinds):
            raise ApprovalRefused(
                "two approvals of the same kind are recorded; a second signature does not replace an independent one"
            )
        object.__setattr__(self, "approvals", approvals)
        if not isinstance(self.ci, CiVerdict):
            raise CiRefused("a pack needs its CI verdict.")
        object.__setattr__(self, "drills", tuple(self.drills))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    # -- derived views ----------------------------------------------------

    @property
    def published_claims(self) -> tuple[Claim, ...]:
        """Claims whose evidence passed, ran at this head and covers their scope."""
        supported: list[Claim] = []
        for claim in self.claims:
            support = self.evidence_by_id[claim.evidence_id]
            if support.passed and support.head_sha == self.head_sha and support.covers(claim.scope):
                supported.append(claim)
        return tuple(supported)

    @property
    def uncovered_claims(self) -> tuple[Claim, ...]:
        published = {claim.claim_id for claim in self.published_claims}
        return tuple(claim for claim in self.claims if claim.claim_id not in published)

    @property
    def evidence_by_id(self) -> dict[str, Evidence]:
        return {item.evidence_id: item for item in self.evidence}

    @property
    def unaccounted_specialisations(self) -> tuple[str, ...]:
        """Specialisations neither selected nor deferred. Should always be empty."""
        accounted = set(self.selected) | {item.specialisation for item in self.deferred}
        return tuple(name for name in SPECIALISATIONS if name not in accounted)

    @property
    def missing_drills(self) -> tuple[str, ...]:
        done = {drill.kind for drill in self.drills if drill.exercised}
        return tuple(kind for kind in DRILLS if kind not in done)

    @property
    def stale_approvals(self) -> tuple[str, ...]:
        return tuple(item.kind for item in self.approvals if not item.is_current(self.now))

    @property
    def missing_approvals(self) -> tuple[str, ...]:
        have = {item.kind for item in self.approvals}
        return tuple(kind for kind in APPROVAL_KINDS if kind not in have)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this pack may not ship, worst first. Empty means qualified."""
        reasons: list[str] = []
        if not self.selected:
            reasons.append("no specialisation is selected for launch; the release has no scope")
        if self.unaccounted_specialisations:
            reasons.append(
                f"{list(self.unaccounted_specialisations)} is neither qualified nor deferred by an authorised "
                "ledger revision; leaving it unmentioned is not the same as deferring it"
            )
        if self.uncovered_claims:
            reasons.append(
                f"{len(self.uncovered_claims)} claim(s) lack passing evidence at this head covering their scope: "
                f"{[claim.claim_id for claim in self.uncovered_claims]}"
            )
        broken = [surface.surface_id for surface in self.preservation if not surface.intact]
        if not self.preservation:
            reasons.append("no preserved surface is reported; existing features are unverified, not preserved")
        elif broken:
            reasons.append(f"preserved surfaces failed verification: {broken}")
        for workflow in self.workflows:
            if workflow.failures:
                reasons.append(f"{workflow.workflow} has {workflow.failures} failing case(s)")
            if workflow.missing_families:
                reasons.append(
                    f"{workflow.workflow} was not run against {list(workflow.missing_families)} cases"
                )
        qualified = {workflow.workflow for workflow in self.workflows if not workflow.failures and not workflow.missing_families}
        for name in self.selected:
            if name not in qualified:
                reasons.append(f"selected specialisation {name!r} has no complete qualification")
        if self.missing_approvals:
            reasons.append(f"independent approvals missing: {list(self.missing_approvals)}")
        if self.stale_approvals:
            reasons.append(f"independent approvals are stale: {list(self.stale_approvals)}")
        if not self.ci.qualifies(self.head_sha):
            reasons.append(
                f"CI at {self.ci.head_sha[:12]} is {self.ci.conclusion}; the pack ships {self.head_sha[:12]}, "
                "and a green run elsewhere is a different build"
            )
        if self.missing_drills:
            reasons.append(f"drills not exercised: {list(self.missing_drills)}")
        if not any(drill.proves_restore for drill in self.drills):
            reasons.append("restore was not proven: the restored bytes were never compared with the source")
        dirty = [artifact.artifact_id for artifact in self.artifacts if not artifact.clean]
        if dirty:
            reasons.append(f"artifacts carry secret findings: {dirty}")
        return tuple(reasons)

    @property
    def qualified(self) -> bool:
        return not self.blocking_reasons

    @property
    def decision(self) -> str:
        return "release-qualified" if self.qualified else "release-blocked"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": RQ_SCHEMA_VERSION,
            "decision": self.decision,
            "qualified": self.qualified,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "selected": list(self.selected),
            "deferred": [item.to_mapping() for item in self.deferred],
            "blocking_reasons": list(self.blocking_reasons),
            "claims": {
                "published": [claim.to_mapping() for claim in self.published_claims],
                "uncovered": [claim.claim_id for claim in self.uncovered_claims],
                "published_count": len(self.published_claims),
                "claimed_count": len(self.claims),
            },
            "evidence": [item.to_mapping() for item in self.evidence],
            "preservation": [surface.to_mapping() for surface in self.preservation],
            "workflows": [workflow.to_mapping() for workflow in self.workflows],
            "approvals": [item.to_mapping(now=self.now) for item in self.approvals],
            "ci": self.ci.to_mapping(),
            "drills": [drill.to_mapping() for drill in self.drills],
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
            "handoff": {
                "author": self.author,
                "author_can_approve": False,
                "claim_scope_matches_tested_scope": not self.uncovered_claims,
            },
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_mapping()).encode("utf-8")).hexdigest()


def qualify_release(
    *,
    head_sha: str,
    base_sha: str,
    claims: Iterable[Claim],
    evidence: Iterable[Evidence],
    selected: Iterable[str],
    ci: CiVerdict,
    now: int,
    deferred: Iterable[DeferredSpecialisation] = (),
    preservation: Iterable[PreservationSurface] = (),
    workflows: Iterable[WorkflowQualification] = (),
    approvals: Iterable[Approval] = (),
    drills: Iterable[DrillRecord] = (),
    artifacts: Iterable[ArtifactCheck] = (),
    author: str = "arena:agent",
) -> QualificationPack:
    return QualificationPack(
        head_sha=head_sha,
        base_sha=base_sha,
        claims=tuple(claims),
        evidence=tuple(evidence),
        selected=tuple(selected),
        deferred=tuple(deferred),
        preservation=tuple(preservation),
        workflows=tuple(workflows),
        approvals=tuple(approvals),
        ci=ci,
        drills=tuple(drills),
        artifacts=tuple(artifacts),
        now=now,
        author=author,
    )
