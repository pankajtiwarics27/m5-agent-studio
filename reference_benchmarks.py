"""AP-020: versioned, owner-approved reference benchmarks.

A reference is an accepted example the system may reuse. The thing that makes it worth
reusing is not that it exists; it is that **an owner accepted it, for a stated scope, at a
stated version**. Everything in this module exists to keep those three facts from decaying.

Four rules shape it, and each one closes a specific way the reuse of examples goes wrong:

1. *An agent cannot approve anything.* :func:`accept` requires a named human and a statement
   they actually made. There is no code path that fabricates one -- no default approver, no
   "system" actor, and :meth:`Reference.from_mapping` refuses a stored record whose approval
   was not given by a person. A reference whose approval cannot be traced is not a reference.

2. *Approval is scoped, and the scopes do not imply each other.* Continuing in a direction,
   finally accepting an artifact, adopting it as a reference, and being allowed to publish it
   are four separate grants. Approving a reference says only "this may be shown as an example
   of this kind of thing" -- it does not accept future work, does not adopt anything else, and
   carries no publish permission.

3. *History is appended to, never rewritten.* An improvement is a **new version** with its own
   id; the version it came from keeps its acceptance exactly as recorded. Superseding links
   forward, so the old version stays readable and its acceptance stays true of the version it
   was given for.

4. *A matching style reference is not evidence about changed facts.* :meth:`Reference.covers`
   answers "was this kind of thing approved, and were these specific checks measured on it",
   and :func:`verification_still_required` names what a reference can never stand in for.
   A reference can settle a look; it cannot settle whether new code works.

Retrieval follows the same discipline: :func:`rank` filters by audience, language, task kind
and style, so a reference approved for a Hindi consumer audience is not offered for an English
internal document. It returns the references and the reasons, never a mandate -- nothing here
forces a new artifact to resemble an old one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

RB_SCHEMA_VERSION = 1
RB_MAX_TEXT = 2000
RB_MAX_ID = 120
RB_MAX_TAGS = 24

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: The four grants. They are separate fields on every record and no function here derives one
#: from another; a caller that wants two of them must record two of them.
APPROVAL_KINDS: tuple[str, ...] = (
    "continue_direction",  # carry on in this direction for this run
    "final_acceptance",  # this artifact is accepted as delivered
    "use_as_reference",  # this may be reused as an example
    "publish",  # this may be published, deployed or spent on
)

REFERENCE_STATUSES: tuple[str, ...] = ("active", "superseded", "revoked")

#: Checks a reference can ever speak to. A reference is visual/craft evidence, so these are the
#: axes an accepted example can legitimately settle.
REFERENCE_SETTLES: tuple[str, ...] = (
    "audience",
    "language",
    "layout_style",
    "tone",
    "structure",
)

#: What a reference can never stand in for, whatever it was approved for. Named explicitly so
#: "we have a reference for this" cannot be heard as "we do not need to test this".
REFERENCE_CANNOT_SETTLE: tuple[str, ...] = (
    "changed_code_works",
    "changed_facts_are_true",
    "changed_assets_are_licensed",
    "security_sign_off",
    "screenshot_approval",
    "final_acceptance_of_new_work",
    "publish_permission",
)


class ReferenceError(ValueError):
    """A reference was malformed, out of scope, or was asked to stand in for something it cannot."""


class ApprovalRefused(ReferenceError):
    """An approval was missing, unnamed, or claimed by something that is not a person."""


class AcceptanceRewriteRefused(ReferenceError):
    """A change tried to alter an acceptance that was already given for a version."""


def _rb_text(value: object, field_name: str, limit: int = RB_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ReferenceError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise ReferenceError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise ReferenceError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise ReferenceError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _rb_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise ReferenceError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _rb_tuple(
    values: Iterable[object], field_name: str, *, limit: int = RB_MAX_TAGS
) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values or ():
        text = _rb_text(raw, field_name, 80)
        if text not in out:
            out.append(text)
    if len(out) > limit:
        raise ReferenceError(f"{field_name} must have at most {limit} entries.")
    return tuple(sorted(out))


def _rb_canonical(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class OwnerApproval:
    """Who accepted this, in their own words, and for what.

    ``statement`` is the owner's actual wording, kept verbatim so a later reader can judge what
    was agreed rather than trusting a label. ``actor`` is always a human identifier; the module
    refuses "agent", "system", "automation" and an empty value, because the whole point of the
    record is that a person, not the system, approved the example.
    """

    actor: str
    statement: str
    granted: tuple[str, ...]
    scope_note: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        who = _rb_text(self.actor, "approval actor", 120).lower()
        if who in {"agent", "system", "automation", "assistant", "model", "scheduler"}:
            raise ApprovalRefused(
                f"{self.actor!r} is not a person; an approval names the human who gave it"
            )
        if not who.startswith(("owner:", "user:", "reviewer:")):
            raise ApprovalRefused(
                "an approval actor must be namespaced as owner:, user: or reviewer:, so a "
                "machine-written record cannot pass for a person's decision"
            )
        _rb_text(self.statement, "approval statement")
        if not self.granted:
            raise ApprovalRefused("an approval must grant at least one thing")
        for kind in self.granted:
            if kind not in APPROVAL_KINDS:
                raise ApprovalRefused(
                    f"unknown approval kind {kind!r}; expected one of {APPROVAL_KINDS}"
                )

    def grants(self, kind: str) -> bool:
        return kind in self.granted

    def to_mapping(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "statement": self.statement,
            "granted": list(self.granted),
            "scope_note": self.scope_note,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OwnerApproval":
        if not isinstance(data, Mapping):
            raise ApprovalRefused("a recorded approval must be a mapping")
        return cls(
            actor=str(data.get("actor") or ""),
            statement=str(data.get("statement") or ""),
            granted=tuple(str(g) for g in (data.get("granted") or ())),
            scope_note=str(data.get("scope_note") or ""),
            recorded_at=str(data.get("recorded_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    """The exact artifact a reference points at, and the checks actually measured on it.

    ``content_sha256`` alone answers "what are the bytes". ``identity_sha256`` is the version
    identity derived from content plus producer provenance, when the producer supplied it, and
    ``identity_basis`` says which of the two this record is entitled to claim -- a content-only
    identity never presents itself as a full one.
    """

    artifact_id: str
    content_sha256: str
    identity_sha256: str = ""
    identity_basis: str = "content_only"
    producer: str = ""
    producer_version: str = ""
    config_sha256: str = ""
    measured_checks: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _rb_text(self.artifact_id, "artifact_id", RB_MAX_ID)
        _rb_digest(self.content_sha256, "content_sha256")
        if self.identity_sha256:
            _rb_digest(self.identity_sha256, "identity_sha256")
        if self.identity_basis not in {"content_only", "content+provenance"}:
            raise ReferenceError(
                f"unknown identity basis {self.identity_basis!r}; expected content_only or content+provenance"
            )
        if self.identity_basis == "content+provenance" and not self.identity_sha256:
            raise ReferenceError(
                "a content+provenance identity must carry its identity digest"
            )
        if self.config_sha256:
            _rb_digest(self.config_sha256, "config_sha256")

    @property
    def passed_checks(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, ok in self.measured_checks.items() if ok))

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, ok in self.measured_checks.items() if not ok))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "identity_sha256": self.identity_sha256,
            "identity_basis": self.identity_basis,
            "producer": self.producer,
            "producer_version": self.producer_version,
            "config_sha256": self.config_sha256,
            "measured_checks": dict(
                sorted((str(k), bool(v)) for k, v in self.measured_checks.items())
            ),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ArtifactVersion":
        if not isinstance(data, Mapping):
            raise ReferenceError("a recorded artifact version must be a mapping")
        checks = data.get("measured_checks") or {}
        if not isinstance(checks, Mapping):
            raise ReferenceError("measured_checks must be a mapping")
        return cls(
            artifact_id=str(data.get("artifact_id") or ""),
            content_sha256=str(data.get("content_sha256") or ""),
            identity_sha256=str(data.get("identity_sha256") or ""),
            identity_basis=str(data.get("identity_basis") or "content_only"),
            producer=str(data.get("producer") or ""),
            producer_version=str(data.get("producer_version") or ""),
            config_sha256=str(data.get("config_sha256") or ""),
            measured_checks={str(k): bool(v) for k, v in checks.items()},
        )


@dataclass(frozen=True, slots=True)
class Reference:
    """One accepted example, at one version, with the brief that produced it.

    ``brief`` is the original request, kept as it was written: a reference without its brief
    teaches a look without teaching what it was for, and the next task that reuses it inherits
    a style whose reason nobody recorded.
    """

    reference_id: str
    version: int
    project_scope_id: str
    task_kind: str
    brief: str
    artifact: ArtifactVersion
    approval: OwnerApproval
    audience: str = ""
    language: str = ""
    style_tags: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    status: str = "active"
    supersedes: str = ""
    superseded_by: str = ""
    revocation_reason: str = ""
    is_final_acceptance: bool = False  # for the *reference*, never for the artifact
    grants_publish_permission: bool = False
    schema_version: int = RB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _rb_text(self.reference_id, "reference_id", RB_MAX_ID)
        if not isinstance(self.version, int) or self.version < 1:
            raise ReferenceError("a reference version is a positive integer")
        _rb_text(self.project_scope_id, "project_scope_id", RB_MAX_ID)
        _rb_text(self.task_kind, "task_kind", 80)
        _rb_text(self.brief, "brief")
        if self.status not in REFERENCE_STATUSES:
            raise ReferenceError(f"unknown reference status {self.status!r}")
        # A reference never carries permission over future work. These two fields exist so the
        # refusal is visible in the record rather than implied by its absence.
        if self.is_final_acceptance or self.grants_publish_permission:
            raise ReferenceError(
                "a reference carries no acceptance of new work and no publish permission"
            )
        if not self.approval.grants("use_as_reference"):
            raise ApprovalRefused(
                "this approval does not grant use_as_reference; accepting an artifact is not "
                "the same decision as adopting it as an example"
            )
        _rb_tuple(self.checks, "checks", limit=64)
        _rb_tuple(self.style_tags, "style_tags")

    # -- identity -------------------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return self.status == "active"

    def fingerprint(self) -> str:
        return _rb_canonical(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reference_id": self.reference_id,
            "version": self.version,
            "project_scope_id": self.project_scope_id,
            "task_kind": self.task_kind,
            "brief": self.brief,
            "artifact": self.artifact.to_mapping(),
            "approval": self.approval.to_mapping(),
            "audience": self.audience,
            "language": self.language,
            "style_tags": list(self.style_tags),
            "checks": list(self.checks),
            "status": self.status,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "revocation_reason": self.revocation_reason,
            "is_final_acceptance": False,
            "grants_publish_permission": False,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Reference":
        if not isinstance(data, Mapping):
            raise ReferenceError("a recorded reference must be a mapping")
        version = data.get("schema_version")
        if version != RB_SCHEMA_VERSION:
            raise ReferenceError(
                f"recorded reference has schema {version!r}, expected {RB_SCHEMA_VERSION!r}"
            )
        # A stored record that claims permission over future work is refused rather than read:
        # a hand-edited row must not become authority.
        for flag in ("is_final_acceptance", "grants_publish_permission"):
            if data.get(flag, False) is not False:
                raise ReferenceError(
                    f"recorded reference claims {flag}; a reference never carries that"
                )
        artifact = data.get("artifact")
        approval = data.get("approval")
        if not isinstance(artifact, Mapping):
            raise ReferenceError(
                "a reference must record the exact artifact it points at"
            )
        if not isinstance(approval, Mapping):
            raise ReferenceError("a reference must record its approval")
        return cls(
            reference_id=str(data.get("reference_id") or ""),
            version=int(data.get("version") or 0),
            project_scope_id=str(data.get("project_scope_id") or ""),
            task_kind=str(data.get("task_kind") or ""),
            brief=str(data.get("brief") or ""),
            artifact=ArtifactVersion.from_mapping(artifact),
            approval=OwnerApproval.from_mapping(approval),
            audience=str(data.get("audience") or ""),
            language=str(data.get("language") or ""),
            style_tags=tuple(str(t) for t in (data.get("style_tags") or ())),
            checks=tuple(str(c) for c in (data.get("checks") or ())),
            status=str(data.get("status") or "active"),
            supersedes=str(data.get("supersedes") or ""),
            superseded_by=str(data.get("superseded_by") or ""),
            revocation_reason=str(data.get("revocation_reason") or ""),
        )

    # -- what a reference may and may not settle ------------------------------------------

    def covers(
        self, *, task_kind: str, checks: Sequence[str] = ()
    ) -> tuple[bool, tuple[str, ...]]:
        """Whether this reference settles ``task_kind``, and which requested checks it does not.

        Coverage is deliberately narrow: the task kind must match, and every check the caller
        wants settled must have been *measured and passed* on this exact version. The second
        return value names what is missing, so a caller can say "covered except layout" instead
        of treating a partial match as no match or as a full one.
        """
        missing: list[str] = []
        if task_kind != self.task_kind:
            missing.append(f"task_kind:{task_kind}")
        for name in checks:
            if name not in self.artifact.passed_checks:
                missing.append(f"check:{name}")
        return (not missing, tuple(missing))


def accept(
    *,
    reference_id: str,
    version: int,
    project_scope_id: str,
    task_kind: str,
    brief: str,
    artifact: ArtifactVersion,
    approval: OwnerApproval,
    audience: str = "",
    language: str = "",
    style_tags: Sequence[str] = (),
    checks: Sequence[str] = (),
    supersedes: str = "",
) -> Reference:
    """Record an owner's decision to reuse one accepted artifact as an example.

    The approval must name a person and must include ``use_as_reference``. An approval that
    accepted the artifact but did not adopt it as a reference is refused, because those are
    different decisions and conflating them is how "the owner liked it" becomes "we may copy it".
    """
    if version < 1:
        raise ReferenceError("a reference version is a positive integer")
    if not artifact.passed_checks and checks:
        raise ReferenceError(
            "checks cannot be claimed for a version that recorded no passed checks"
        )
    for name in checks:
        if name not in artifact.passed_checks:
            raise ReferenceError(
                f"check {name!r} was not measured and passed on this artifact version"
            )
    return Reference(
        reference_id=reference_id,
        version=version,
        project_scope_id=project_scope_id,
        task_kind=task_kind,
        brief=brief,
        artifact=artifact,
        approval=approval,
        audience=audience,
        language=language,
        style_tags=tuple(style_tags),
        checks=tuple(checks),
        supersedes=supersedes,
    )


def enhance(
    previous: Reference,
    *,
    new_version: int,
    artifact: ArtifactVersion,
    approval: OwnerApproval,
    brief: str = "",
    style_tags: Sequence[str] | None = None,
    checks: Sequence[str] | None = None,
) -> Reference:
    """Produce the next version of a reference, leaving the previous one's acceptance intact.

    The new version needs **its own** approval: an owner who accepted version 1 did not thereby
    accept version 2, and the previous record is returned unchanged for the caller to keep. What
    changes is what the owner approved; what stays is that version 1 was approved, by whom, and
    for what.
    """
    if new_version <= previous.version:
        raise ReferenceError(
            f"new version {new_version} must be greater than {previous.version}; a reference "
            "is amended by adding a version, never by overwriting one"
        )
    if previous.status != "active":
        raise ReferenceError(
            f"reference version {previous.version} is {previous.status}; only an active version can be enhanced"
        )
    successor = accept(
        reference_id=previous.reference_id,
        version=new_version,
        project_scope_id=previous.project_scope_id,
        task_kind=previous.task_kind,
        brief=brief or previous.brief,
        artifact=artifact,
        approval=approval,
        audience=previous.audience,
        language=previous.language,
        style_tags=previous.style_tags if style_tags is None else style_tags,
        checks=previous.checks if checks is None else checks,
        supersedes=f"{previous.reference_id}@v{previous.version}",
    )
    return successor


def supersede(previous: Reference, successor: Reference) -> Reference:
    """Return the *previous* version marked superseded-by a successor, acceptance untouched.

    Only the link forward changes. The previous version's artifact, approval, brief and checks
    are exactly what they were -- that is what "prior acceptance cannot be silently rewritten"
    means in practice.
    """
    if successor.reference_id != previous.reference_id:
        raise ReferenceError(
            "a successor must be another version of the same reference"
        )
    if successor.version <= previous.version:
        raise ReferenceError("a successor must have a greater version")
    if previous.status == "superseded" and previous.superseded_by:
        raise AcceptanceRewriteRefused(
            f"version {previous.version} is already superseded by {previous.superseded_by}; "
            "the link forward is part of the record and does not move"
        )
    return replace(
        previous,
        status="superseded",
        superseded_by=f"{successor.reference_id}@v{successor.version}",
    )


def revoke(reference: Reference, *, reason: str, actor: str) -> Reference:
    """Withdraw a reference from use. The record stays; only its status changes."""
    who = _rb_text(actor, "actor", 120).lower()
    if not who.startswith(("owner:", "user:", "reviewer:")):
        raise ApprovalRefused("a revocation names the person who made it")
    if reference.status == "superseded":
        raise ReferenceError(
            "a superseded version is already out of use; revoke the version that replaced it"
        )
    return replace(
        reference, status="revoked", revocation_reason=_rb_text(reason, "reason", 500)
    )


def verification_still_required(*, changed: Sequence[str]) -> tuple[str, ...]:
    """What a reference cannot stand in for, given what actually changed.

    This is the function that keeps "we have a reference for this kind of page" from being heard
    as "this page is fine". Any change to code, facts or assets brings obligations a style
    reference can never discharge, and they are returned rather than assumed.
    """
    required: list[str] = []
    for name in changed or ():
        if name in {"code", "implementation", "behaviour", "behavior"}:
            required.extend(["changed_code_works", "security_sign_off"])
        elif name in {"facts", "data", "copy", "claims"}:
            required.append("changed_facts_are_true")
        elif name in {"assets", "images", "media", "fonts"}:
            required.append("changed_assets_are_licensed")
        elif name in {"screenshots", "screenshot"}:
            required.append("screenshot_approval")
    required.extend(["final_acceptance_of_new_work", "publish_permission"])
    seen: list[str] = []
    for item in required:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def rank(
    references: Sequence[Reference],
    *,
    task_kind: str,
    audience: str = "",
    language: str = "",
    style_tags: Sequence[str] = (),
) -> list[tuple[Reference, tuple[str, ...]]]:
    """Order live references by how well they match, with the reasons for each score.

    Nothing is excluded for a soft mismatch: a reference approved for another audience is still
    returned, ranked lower and labelled with why, so a caller can decide. Hard mismatches --
    task kind and language -- are labelled too rather than dropped, because a caller asking for
    an English page should be told that the only references it has are in Hindi.

    The return value is a *reading order*, not an instruction: layout is explicitly not a
    criterion, so a caller cannot be pushed toward reproducing a previous design.
    """
    scored: list[tuple[int, Reference, tuple[str, ...]]] = []
    wanted_tags = {t.strip().lower() for t in style_tags if t and t.strip()}
    for reference in references:
        if not reference.is_live:
            continue
        reasons: list[str] = []
        score = 0
        if reference.task_kind == task_kind:
            score += 4
            reasons.append("same task kind")
        else:
            reasons.append(f"different task kind ({reference.task_kind})")
        if language:
            if reference.language.lower() == language.lower():
                score += 2
                reasons.append("same language")
            else:
                reasons.append(
                    f"different language ({reference.language or 'unrecorded'})"
                )
        if audience:
            if reference.audience.lower() == audience.lower():
                score += 2
                reasons.append("same audience")
            else:
                reasons.append(
                    f"different audience ({reference.audience or 'unrecorded'})"
                )
        shared = wanted_tags & {tag.lower() for tag in reference.style_tags}
        if shared:
            score += len(shared)
            reasons.append("shared style: " + ", ".join(sorted(shared)))
        # `layout` is never scored: a reference supplies a direction, not a template.
        scored.append((score, reference, tuple(reasons)))
    scored.sort(key=lambda row: (-row[0], row[1].reference_id, -row[1].version))
    return [(reference, reasons) for _score, reference, reasons in scored]


def latest_by_reference(references: Sequence[Reference]) -> dict[str, Reference]:
    """The current version of each reference id, which is what a caller normally wants."""
    newest: dict[str, Reference] = {}
    for reference in references:
        current = newest.get(reference.reference_id)
        if current is None or reference.version > current.version:
            newest[reference.reference_id] = reference
    return newest


class ReferenceStore:
    """Durable reference records, one row per (reference id, version).

    Versions are never updated in place. :meth:`put` refuses to replace a row whose acceptance
    differs from what is stored, so an improvement has to arrive as a new version -- the storage
    layer is where "prior acceptance cannot be silently rewritten" is actually enforced, not
    just where it is described.
    """

    def __init__(self, database: Any) -> None:
        import sqlite3
        from pathlib import Path

        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_benchmarks (
                    reference_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    project_scope_id TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (reference_id, version),
                    CHECK (version >= 1),
                    CHECK (status IN ('active', 'superseded', 'revoked')),
                    CHECK (length(artifact_sha256) = 64)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reference_benchmarks_scope "
                "ON reference_benchmarks (project_scope_id, task_kind, status)"
            )

    def _connect(self):
        import sqlite3

        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def put(self, record: Reference) -> Reference:
        """Insert one version, or re-state an identical one. A changed acceptance is refused."""
        payload = json.dumps(record.to_mapping(), sort_keys=True)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT record_json FROM reference_benchmarks WHERE reference_id = ? AND version = ?",
                (record.reference_id, record.version),
            ).fetchone()
            if existing is not None:
                stored = Reference.from_mapping(json.loads(existing["record_json"]))
                if stored.approval.to_mapping() != record.approval.to_mapping():
                    raise AcceptanceRewriteRefused(
                        f"{record.reference_id}@v{record.version} already records an approval by "
                        f"{stored.approval.actor}; an acceptance is not amended in place -- add a version"
                    )
                if stored.artifact.content_sha256 != record.artifact.content_sha256:
                    raise AcceptanceRewriteRefused(
                        f"{record.reference_id}@v{record.version} points at a different artifact "
                        "version; add a version rather than redirecting an accepted one"
                    )
            connection.execute(
                """
                INSERT INTO reference_benchmarks (
                    reference_id, version, project_scope_id, task_kind, status,
                    artifact_sha256, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reference_id, version) DO UPDATE SET
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record.reference_id,
                    record.version,
                    record.project_scope_id,
                    record.task_kind,
                    record.status,
                    record.artifact.content_sha256,
                    payload,
                ),
            )
        return record

    def get(self, reference_id: str, version: int) -> Reference | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM reference_benchmarks WHERE reference_id = ? AND version = ?",
                (_rb_text(reference_id, "reference_id", RB_MAX_ID), int(version)),
            ).fetchone()
        return (
            None
            if row is None
            else Reference.from_mapping(json.loads(row["record_json"]))
        )

    def history(self, reference_id: str) -> list[Reference]:
        """Every version of one reference, oldest first. An append-only history, by definition."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM reference_benchmarks WHERE reference_id = ? ORDER BY version",
                (_rb_text(reference_id, "reference_id", RB_MAX_ID),),
            ).fetchall()
        return [Reference.from_mapping(json.loads(row["record_json"])) for row in rows]

    def list(
        self, *, project_scope_id: str | None = None, status: str | None = None
    ) -> list[Reference]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_scope_id is not None:
            clauses.append("project_scope_id = ?")
            params.append(_rb_text(project_scope_id, "project_scope_id", RB_MAX_ID))
        if status is not None:
            if status not in REFERENCE_STATUSES:
                raise ReferenceError(f"unknown reference status {status!r}")
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM reference_benchmarks{where} ORDER BY reference_id, version",
                tuple(params),
            ).fetchall()
        return [Reference.from_mapping(json.loads(row["record_json"])) for row in rows]
