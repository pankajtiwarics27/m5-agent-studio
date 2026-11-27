#!/usr/bin/env python3
"""Scoped feedback: turn a comment on one thing into a bounded revision, never a policy.

The failure this module exists to prevent is *generalisation*. An owner says "this heading
is too loud" about one heading, and the agent quietly rewrites every heading in the product,
or writes "user prefers short headings" into a preference store nobody can inspect or undo.
Both are the same mistake: a bounded observation silently became an unbounded rule.

So a feedback record here has exactly one scope, it is stated, and it is stored. Three kinds:

``element``  one region, passage, scene or timestamp of one artifact version
``artifact`` the whole of one artifact version
``preference`` something that outlives one artifact -- and therefore must be *asked for*

That last rule is the point. A preference is never inferred from an element or artifact
comment; ``promote`` requires an explicit basis and returns the scope it now has, so the
widening is a recorded decision rather than an accident. ``PreferenceDrift`` is raised when a
consumer tries to read a scoped correction as if it were general.

Everything else is ordinary care:

* the owner's exact wording is kept verbatim beside any interpretation, because a
  paraphrase that replaces the source cannot be audited;
* an interpretation that the agent had to make is *material* unless the wording left nothing
  to decide, and a material interpretation must be confirmed before it becomes a revision;
* conflicting feedback on the same scope is reported, not merged into an average;
* a revision invalidates only evidence that depended on the version it replaced.

No clock, no file, no model: this module decides and describes. Storage and rendering live in
the application, so a decision cannot claim work it did not do.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

PS_SCHEMA_VERSION = 1

# Scope kinds, narrow to wide. A revision may narrow; only ``promote`` may widen, and only
# with a stated basis.
SCOPE_KINDS = ("element", "artifact", "preference")

# How a piece of feedback describes the thing it is about. "element" needs a locator; the
# others do not, so a plain-text comment is never blocked for want of a selection.
LOCATOR_KINDS = ("element", "text-range", "scene", "timestamp")

# What the owner did, when they used a control rather than typing. These are stored as the
# action and expanded into wording here, so the record keeps the control that was pressed
# and not only its prose.
QUICK_ACTIONS = {
    "louder": "make this more prominent",
    "quieter": "make this less prominent",
    "shorter": "make this shorter",
    "longer": "make this longer",
    "warmer": "make this warmer in tone",
    "plain": "use plainer language",
    "keep": "keep this as it is",
    "undo": "revert this to the previous version",
}

PS_MAX_TEXT = 2000
PS_MAX_LOCATOR = 300
_DC_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class FeedbackError(ValueError):
    """Feedback was malformed, out of scope, or would silently widen its own reach."""


class FeedbackConflict(RuntimeError):
    """Feedback cannot be safely combined or written without a conflicting identity."""


class PreferenceDrift(RuntimeError):
    """A scoped correction was about to be applied as if it were a general preference."""


def _ps_text(value: str, field_name: str, limit: int = PS_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise FeedbackError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise FeedbackError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise FeedbackError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise FeedbackError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _ps_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _DC_DIGEST_RE.fullmatch(value.strip().lower()):
        raise FeedbackError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class ElementSelection:
    """Where in an artifact version the comment points, when the owner selected something.

    A selection is optional by design: precision must never be the price of reporting a
    problem. What is *not* optional is that a selection names the version it was made on,
    because a selection without a version cannot be checked for staleness later.
    """

    locator_kind: str
    locator: str
    artifact_sha256: str
    selection_sha256: str = ""

    def __post_init__(self) -> None:
        if self.locator_kind not in LOCATOR_KINDS:
            raise FeedbackError(
                f"unknown selection kind {self.locator_kind!r}; expected one of {sorted(LOCATOR_KINDS)}"
            )
        _ps_text(self.locator, "locator", PS_MAX_LOCATOR)
        _ps_digest(self.artifact_sha256, "artifact_sha256")
        if self.selection_sha256:
            _ps_digest(self.selection_sha256, "selection_sha256")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "locator_kind": self.locator_kind,
            "locator": self.locator,
            "artifact_sha256": self.artifact_sha256,
            "selection_sha256": self.selection_sha256,
        }

    @classmethod
    def from_mapping(cls, recorded: Mapping[str, Any]) -> "ElementSelection":
        if not isinstance(recorded, Mapping):
            raise FeedbackError("a recorded selection must be a mapping")
        return cls(
            locator_kind=str(recorded.get("locator_kind") or ""),
            locator=str(recorded.get("locator") or ""),
            artifact_sha256=str(recorded.get("artifact_sha256") or ""),
            selection_sha256=str(recorded.get("selection_sha256") or ""),
        )


@dataclass(frozen=True, slots=True)
class Feedback:
    """One thing the owner said about one thing.

    ``verbatim`` is what they wrote and is never rewritten. ``interpretation`` is what the
    agent understood, kept beside the original rather than instead of it, and ``material``
    records whether that understanding involved a judgement the owner should confirm.
    """

    feedback_id: str
    artifact_id: str
    artifact_sha256: str
    scope_kind: str
    verbatim: str
    # Which run the comment was given in. Two sessions can produce artifacts with the same
    # name; without this, a correction about one run's hero.html would be shown to the reader
    # of another run's hero.html, at the same version, and look entirely legitimate.
    session_id: str
    interpretation: str = ""
    material: bool = False
    selection: ElementSelection | None = None
    quick_action: str = ""
    confirmed: bool = False
    confirmed_by: str = ""
    superseded_by: str = ""
    deleted: bool = False

    def __post_init__(self) -> None:
        _ps_text(self.feedback_id, "feedback_id", 120)
        _ps_text(self.artifact_id, "artifact_id", PS_MAX_LOCATOR)
        _ps_digest(self.artifact_sha256, "artifact_sha256")
        if self.scope_kind not in SCOPE_KINDS:
            raise FeedbackError(
                f"unknown scope {self.scope_kind!r}; expected one of {SCOPE_KINDS}"
            )
        _ps_text(self.verbatim, "verbatim")
        _ps_text(self.session_id, "session_id", 120)
        if self.interpretation:
            _ps_text(self.interpretation, "interpretation")
        if self.quick_action and self.quick_action not in QUICK_ACTIONS:
            raise FeedbackError(f"unknown quick action {self.quick_action!r}")
        # A preference is a claim about everything, so it may not arrive wearing a selection:
        # accepting one would let a comment about one region become a rule about all of them.
        if self.scope_kind == "preference" and self.selection is not None:
            raise FeedbackError(
                "a preference cannot carry a selection; promote an element or artifact "
                "correction instead, so the widening is recorded"
            )
        if (
            self.selection is not None
            and self.selection.artifact_sha256 != self.artifact_sha256
        ):
            raise FeedbackError(
                "the selection was made on a different artifact version than the feedback pins; "
                "that is exactly the stale-selection case, and it is refused here rather than applied later"
            )
        if self.confirmed and not (self.confirmed_by or "").strip():
            raise FeedbackError(
                "a confirmed interpretation must name who confirmed it."
            )

    # ── identity ────────────────────────────────────────────────────────────────────────

    @property
    def is_live(self) -> bool:
        """Whether this feedback still stands: not deleted, not replaced by a revision."""
        return not self.deleted and not self.superseded_by

    @property
    def needs_confirmation(self) -> bool:
        """A material interpretation that nobody has confirmed is not yet a revision."""
        return self.material and not self.confirmed

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": PS_SCHEMA_VERSION,
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "scope_kind": self.scope_kind,
            "verbatim": self.verbatim,
            "interpretation": self.interpretation,
            "material": self.material,
            "selection": self.selection.to_mapping() if self.selection else None,
            "quick_action": self.quick_action,
            "confirmed": self.confirmed,
            "confirmed_by": self.confirmed_by,
            "superseded_by": self.superseded_by,
            "deleted": self.deleted,
            # Never inferred from the record: a correction is not acceptance, and the reader
            # must not be able to mistake one for the other by looking at a flag.
            "is_final_acceptance": False,
            "grants_publish_permission": False,
        }

    @classmethod
    def from_mapping(cls, recorded: Mapping[str, Any]) -> "Feedback":
        if not isinstance(recorded, Mapping):
            raise FeedbackError("a recorded feedback item must be a mapping")
        version = recorded.get("schema_version")
        if version != PS_SCHEMA_VERSION:
            raise FeedbackError(
                f"recorded feedback has schema {version!r}, expected {PS_SCHEMA_VERSION!r}"
            )
        for flag in ("is_final_acceptance", "grants_publish_permission"):
            if recorded.get(flag, False) is not False:
                raise FeedbackError(
                    f"recorded feedback claims {flag}; a correction never carries that"
                )
        selection = recorded.get("selection")
        return cls(
            feedback_id=str(recorded.get("feedback_id") or ""),
            # Required, not defaulted: a record that does not name the run it was given in
            # cannot be attributed to a reader's session, and guessing would show one run's
            # correction to another run's reader.
            session_id=str(recorded.get("session_id") or ""),
            artifact_id=str(recorded.get("artifact_id") or ""),
            artifact_sha256=str(recorded.get("artifact_sha256") or ""),
            scope_kind=str(recorded.get("scope_kind") or ""),
            verbatim=str(recorded.get("verbatim") or ""),
            interpretation=str(recorded.get("interpretation") or ""),
            material=bool(recorded.get("material", False)),
            selection=ElementSelection.from_mapping(selection)
            if isinstance(selection, Mapping)
            else None,
            quick_action=str(recorded.get("quick_action") or ""),
            confirmed=bool(recorded.get("confirmed", False)),
            confirmed_by=str(recorded.get("confirmed_by") or ""),
            superseded_by=str(recorded.get("superseded_by") or ""),
            deleted=bool(recorded.get("deleted", False)),
        )


def capture(
    *,
    feedback_id: str,
    session_id: str,
    artifact_id: str,
    artifact_sha256: str,
    verbatim: str = "",
    selection: ElementSelection | None = None,
    quick_action: str = "",
    interpretation: str = "",
    material: bool | None = None,
    scope_kind: str | None = None,
) -> Feedback:
    """Record what the owner said, with the narrowest scope their wording supports.

    A quick action is a complete statement on its own, so it satisfies the "something was
    actually said" requirement without typing -- the pane is not allowed to demand prose. If
    neither words nor an action were given, there is nothing to record and this refuses.
    """
    identifier = _ps_text(feedback_id, "feedback_id", 120)
    run = _ps_text(session_id, "session_id", 120)
    name = _ps_text(artifact_id, "artifact_id", PS_MAX_LOCATOR)
    digest = _ps_digest(artifact_sha256, "artifact_sha256")

    action = (quick_action or "").strip()
    if action and action not in QUICK_ACTIONS:
        raise FeedbackError(
            f"unknown quick action {action!r}; expected one of {sorted(QUICK_ACTIONS)}"
        )
    words = (verbatim or "").strip()
    if not words and not action:
        raise FeedbackError(
            "feedback needs either words or a quick action; an empty record claims the owner said something"
        )

    # The scope follows what the owner pointed at, not what the agent hopes to change.
    resolved_scope = scope_kind or ("element" if selection is not None else "artifact")
    if resolved_scope not in SCOPE_KINDS:
        raise FeedbackError(
            f"unknown scope {resolved_scope!r}; expected one of {SCOPE_KINDS}"
        )
    if resolved_scope == "element" and selection is None:
        raise FeedbackError(
            "element-scoped feedback needs a locator; use artifact scope for a comment about the whole artifact"
        )
    if resolved_scope == "preference":
        raise FeedbackError(
            "a preference cannot be captured from one comment about one artifact. Record the "
            "correction at artifact scope, then promote it explicitly if the owner means it generally"
        )

    # Was a judgement needed? A bare quick action is unambiguous -- it names its own change.
    # Words are material unless the caller, who knows the wording, says otherwise, because
    # the safe default for an interpretation is "ask".
    if material is None:
        material = bool(words) and not action

    understood = (interpretation or "").strip()
    if not understood:
        understood = QUICK_ACTIONS[action] if action else ""

    return Feedback(
        feedback_id=identifier,
        session_id=run,
        artifact_id=name,
        artifact_sha256=digest,
        scope_kind=resolved_scope,
        verbatim=words or QUICK_ACTIONS[action],
        interpretation=understood,
        material=bool(material),
        selection=selection,
        quick_action=action,
    )


def confirm(feedback: Feedback, *, confirmed_by: str) -> Feedback:
    """Accept a material interpretation as the reading the owner meant.

    This confirms the *interpretation*, never the artifact: the flags it writes are about
    reading the words correctly, and nothing here accepts, publishes or approves work.
    """
    who = _ps_text(confirmed_by, "confirmed_by", 120)
    if not feedback.material:
        raise FeedbackError(
            "this interpretation is not material, so it needs no confirmation"
        )
    if feedback.confirmed:
        raise FeedbackError("this interpretation is already confirmed")
    return replace(feedback, confirmed=True, confirmed_by=who)


def supersede(previous: Feedback, *, by: Feedback) -> Feedback:
    """Mark earlier feedback as replaced by a revision, keeping it for the record.

    Superseding is not deletion. The old words stay readable, because "what did they ask for
    first, and when did it change" is a question somebody will need to answer.
    """
    if previous.feedback_id == by.feedback_id:
        raise FeedbackError("feedback cannot supersede itself")
    if previous.artifact_id != by.artifact_id:
        raise FeedbackError(
            "a revision of one artifact cannot supersede feedback on another"
        )
    if previous.deleted:
        raise FeedbackError(
            "deleted feedback cannot be superseded; it is out of the live set"
        )
    return replace(previous, superseded_by=by.feedback_id)


def promote(
    feedback: Feedback,
    *,
    basis: str,
    promoted_by: str,
    scope_kind: str = "preference",
) -> Feedback:
    """Widen a correction into a preference — deliberately, with a stated reason.

    This is the only path from "this one thing" to "everything". It requires a basis and a
    person, so a generalisation always has an author and an argument, and it is recorded as a
    new item rather than quietly changing the old one's meaning.
    """
    reason = _ps_text(basis, "basis", PS_MAX_TEXT)
    who = _ps_text(promoted_by, "promoted_by", 120)
    if scope_kind not in ("artifact", "preference"):
        raise FeedbackError(
            "promotion widens to artifact or preference scope, never to element scope"
        )
    if feedback.scope_kind == "preference":
        raise FeedbackError(
            "this is already a preference; promoting it again proves nothing"
        )
    if scope_kind == "artifact" and feedback.scope_kind == "artifact":
        raise FeedbackError(
            "this already applies to the whole artifact; there is nothing to widen"
        )
    if feedback.selection is not None:
        raise FeedbackError(
            "a correction bound to a selection cannot be promoted; the selection would be lost "
            "silently, which is the drift this refuses"
        )
    if feedback.needs_confirmation:
        raise FeedbackError(
            "confirm what the feedback means before widening it; a generalisation on an "
            "unconfirmed interpretation amplifies a guess"
        )
    return replace(
        feedback,
        feedback_id=f"{feedback.feedback_id}@{scope_kind}",
        scope_kind=scope_kind,
        verbatim=feedback.verbatim,
        interpretation=feedback.interpretation or reason,
        # The promoted item is its own statement, so it is no longer "material": the basis is
        # the confirmation, and it names who gave it.
        material=False,
        confirmed=True,
        confirmed_by=who,
        superseded_by="",
    )


def live(feedback: Sequence[Feedback]) -> list[Feedback]:
    """Everything that still stands, in the order it was recorded."""
    return [item for item in feedback if item.is_live]


def pending_confirmation(feedback: Sequence[Feedback]) -> list[Feedback]:
    """Live feedback whose interpretation the owner has not confirmed yet.

    A revision waits on these. Building on an unconfirmed reading is how a misunderstanding
    survives into a deliverable, where it is much more expensive to find.
    """
    return [item for item in feedback if item.is_live and item.needs_confirmation]


def _subject(feedback: Feedback) -> tuple[str, str]:
    """The thing a correction is about: the artifact, and the region within it if any."""
    if feedback.selection is not None:
        return (feedback.artifact_id, feedback.selection.locator)
    return (feedback.artifact_id, "")


def conflicts(feedback: Sequence[Feedback]) -> list[tuple[Feedback, Feedback]]:
    """Pairs of live corrections that cannot both be acted on.

    Only the same subject counts, and only when one asks to keep and the other to change --
    an owner repeating themselves is not a conflict, and two different desired changes to one
    region is a real question that an average would answer wrongly.
    """
    found: list[tuple[Feedback, Feedback]] = []
    items = live(feedback)
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if _subject(first) != _subject(second):
                continue
            first_keeps = first.quick_action == "keep"
            second_keeps = second.quick_action == "keep"
            if first_keeps != second_keeps:
                found.append((first, second))
                continue
            if (
                not first_keeps
                and not second_keeps
                and first.verbatim != second.verbatim
            ):
                found.append((first, second))
    return found


@dataclass(frozen=True, slots=True)
class Revision:
    """What a set of corrections amounts to for one artifact version.

    ``ready`` is the whole answer: a revision may be built only when nothing is pending a
    confirmation and nothing is in conflict. Both are reported with the items that caused
    them, so the pane can say which question to answer rather than only that it is blocked.
    """

    artifact_id: str
    base_sha256: str
    changes: tuple[Feedback, ...]
    conflicts: tuple[tuple[str, str], ...] = ()
    awaiting_confirmation: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return (
            not self.conflicts and not self.awaiting_confirmation and bool(self.changes)
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "base_sha256": self.base_sha256,
            "changes": [item.to_mapping() for item in self.changes],
            "conflicts": [list(pair) for pair in self.conflicts],
            "awaiting_confirmation": list(self.awaiting_confirmation),
            "ready": self.ready,
            # A revision is a plan to change work. It is not acceptance of the result, and the
            # consumer that reads it must ask again once the revision exists.
            "is_final_acceptance": False,
            "grants_publish_permission": False,
        }


def plan_revision(
    *,
    artifact_id: str,
    base_sha256: str,
    feedback: Sequence[Feedback],
) -> Revision:
    """Gather the live corrections that apply to one version, and say whether they can be built.

    Feedback recorded against a *different* version is not silently included: it is stale, and
    the caller learns that from :func:`stale_for`. Including it would apply "make the heading
    quieter" to a heading nobody looked at.
    """
    name = _ps_text(artifact_id, "artifact_id", PS_MAX_LOCATOR)
    digest = _ps_digest(base_sha256, "base_sha256")
    applicable = [
        item
        for item in live(feedback)
        if item.artifact_id == name and item.artifact_sha256 == digest
    ]
    pairs = tuple(
        (first.feedback_id, second.feedback_id)
        for first, second in conflicts(applicable)
    )
    pending = tuple(item.feedback_id for item in applicable if item.needs_confirmation)
    return Revision(
        artifact_id=name,
        base_sha256=digest,
        changes=tuple(applicable),
        conflicts=pairs,
        awaiting_confirmation=pending,
    )


def stale_for(
    *, feedback: Sequence[Feedback], artifact_id: str, current_sha256: str
) -> list[Feedback]:
    """Live feedback about this artifact that was written against a version that has moved on.

    Reported rather than dropped: the owner's words still stand as a question, but they no
    longer describe the thing on screen, and applying them would be guessing.
    """
    name = _ps_text(artifact_id, "artifact_id", PS_MAX_LOCATOR)
    digest = _ps_digest(current_sha256, "current_sha256")
    return [
        item
        for item in live(feedback)
        if item.artifact_id == name and item.artifact_sha256 != digest
    ]


def affected_evidence(
    *,
    references: Iterable[Mapping[str, Any]],
    invalidated_version: str,
) -> list[Mapping[str, Any]]:
    """Which recorded evidence stops standing when one version is replaced.

    Only what names that version is invalidated. A checkpoint, approval or verification that
    referenced some other version still describes what it described; sweeping it away with
    the rest would destroy evidence that is still true, and keeping it when it referenced the
    replaced version would keep a claim about bytes that no longer exist.
    """
    digest = _ps_digest(invalidated_version, "invalidated_version")
    found: list[Mapping[str, Any]] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        if str(reference.get("artifact_sha256") or "").strip().lower() == digest:
            found.append(reference)
    return found


def assert_not_generalised(record: Feedback, *, applying_scope: str) -> None:
    """Refuse to read a scoped correction as a wider rule.

    The consumer states the scope it is about to apply. If that is wider than what the owner
    actually said, this raises rather than letting the correction expand in transit -- which
    is where drift actually happens, not in the store.
    """
    if applying_scope not in SCOPE_KINDS:
        raise FeedbackError(
            f"unknown scope {applying_scope!r}; expected one of {SCOPE_KINDS}"
        )
    order = {kind: index for index, kind in enumerate(SCOPE_KINDS)}
    if order[applying_scope] > order[record.scope_kind]:
        raise PreferenceDrift(
            f"feedback {record.feedback_id!r} was given at {record.scope_kind!r} scope and is being "
            f"applied at {applying_scope!r}; widening requires promote() with a stated basis"
        )


__all__ = [
    "LOCATOR_KINDS",
    "PS_MAX_TEXT",
    "PS_SCHEMA_VERSION",
    "QUICK_ACTIONS",
    "SCOPE_KINDS",
    "ElementSelection",
    "Feedback",
    "FeedbackConflict",
    "FeedbackError",
    "PreferenceDrift",
    "Revision",
    "affected_evidence",
    "assert_not_generalised",
    "capture",
    "confirm",
    "conflicts",
    "live",
    "pending_confirmation",
    "plan_revision",
    "promote",
    "stale_for",
    "supersede",
]


# ── persistence ─────────────────────────────────────────────────────────────────────────
# A plain SQLite table in the application's own database, following side_effect_ledger's
# shape: no second store, no daemon, no cache that can disagree with the record. Deletion is
# a flag rather than a DELETE so "the owner removed this" stays answerable after they did.


class FeedbackStore:
    """Durable feedback records, keyed by id, in the application database."""

    def __init__(self, database: Any) -> None:
        import sqlite3
        from pathlib import Path

        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scoped_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    artifact_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    verbatim TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (scope_kind IN ('element', 'artifact', 'preference')),
                    CHECK (length(artifact_sha256) = 64)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS scoped_feedback_artifact ON scoped_feedback (artifact_id, artifact_sha256)"
            )
            # A database created before the run was recorded gets the column, not a rewrite: an
            # empty session_id is an old row, and it is never shown as belonging to a run.
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(scoped_feedback)")
            }
            if "session_id" not in columns:
                connection.execute(
                    "ALTER TABLE scoped_feedback ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS scoped_feedback_session ON scoped_feedback (session_id, artifact_id, artifact_sha256)"
            )

    def _connect(self):
        import sqlite3

        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, record: Feedback) -> Feedback:
        """Insert a new feedback identity, atomically refusing any existing identity."""
        payload = json.dumps(record.to_mapping(), sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scoped_feedback (
                    feedback_id, session_id, artifact_id, artifact_sha256, scope_kind, verbatim, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO NOTHING
                """,
                (
                    record.feedback_id,
                    record.session_id,
                    record.artifact_id,
                    record.artifact_sha256,
                    record.scope_kind,
                    record.verbatim,
                    payload,
                ),
            )
            if cursor.rowcount != 1:
                raise FeedbackConflict("feedback identity already exists; no new feedback was recorded")
        return record

    def _put_on_connection(self, connection: Any, record: Feedback) -> Feedback:
        """Write using the caller's transaction, retaining the record's existing binding."""
        payload = json.dumps(record.to_mapping(), sort_keys=True)
        cursor = connection.execute(
                """
                INSERT INTO scoped_feedback (
                    feedback_id, session_id, artifact_id, artifact_sha256, scope_kind, verbatim, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    artifact_id = excluded.artifact_id,
                    artifact_sha256 = excluded.artifact_sha256,
                    scope_kind = excluded.scope_kind,
                    verbatim = excluded.verbatim,
                    record_json = excluded.record_json,
                    updated_at = CURRENT_TIMESTAMP
                WHERE scoped_feedback.session_id = excluded.session_id
                  AND scoped_feedback.artifact_id = excluded.artifact_id
                  AND scoped_feedback.artifact_sha256 = excluded.artifact_sha256
                  AND scoped_feedback.scope_kind = excluded.scope_kind
                """,
                (
                    record.feedback_id,
                    record.session_id,
                    record.artifact_id,
                    record.artifact_sha256,
                    record.scope_kind,
                    record.verbatim,
                    payload,
                ),
        )
        if cursor.rowcount != 1:
            raise FeedbackConflict("feedback identity has a different recorded binding")
        return record

    def put(self, record: Feedback) -> Feedback:
        """Insert or update a record without changing its session, artifact, version or scope."""
        with self._connect() as connection:
            return self._put_on_connection(connection, record)

    def get(self, feedback_id: str) -> Feedback | None:
        identifier = _ps_text(feedback_id, "feedback_id", 120)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM scoped_feedback WHERE feedback_id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return Feedback.from_mapping(json.loads(row["record_json"]))

    def list(
        self, *, artifact_id: str | None = None, session_id: str | None = None
    ) -> list[Feedback]:
        """Read records, optionally narrowed to one artifact and to the run they belong to.

        ``session_id`` is not a convenience: two runs can each produce an artifact of the same
        name at the same version, and a reader looking at one run must not be shown the other
        run's corrections as if they applied to it.
        """
        clauses: list[str] = []
        params: list[str] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(_ps_text(session_id, "session_id", 120))
        if artifact_id is not None:
            clauses.append("artifact_id = ?")
            params.append(_ps_text(artifact_id, "artifact_id", PS_MAX_LOCATOR))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM scoped_feedback{where} ORDER BY created_at, feedback_id",
                tuple(params),
            ).fetchall()
        return [Feedback.from_mapping(json.loads(row["record_json"])) for row in rows]

    def _read_bound(self, connection: Any, identifier: str, session_id: str | None) -> Feedback | None:
        row = connection.execute(
            "SELECT session_id, artifact_id, artifact_sha256, scope_kind, record_json "
            "FROM scoped_feedback WHERE feedback_id = ?", (identifier,)
        ).fetchone()
        if row is None or (session_id is not None and row["session_id"] != session_id):
            return None
        existing = Feedback.from_mapping(json.loads(row["record_json"]))
        binding = (existing.feedback_id, existing.session_id, existing.artifact_id, existing.artifact_sha256, existing.scope_kind)
        if binding != (identifier, row["session_id"], row["artifact_id"], row["artifact_sha256"], row["scope_kind"]):
            raise FeedbackConflict("feedback record does not match its stored binding")
        return existing

    def _update_atomic(
        self, feedback_id: str, session_id: str | None, change: Callable[[Feedback], Feedback]
    ) -> Feedback | None:
        """Serialize a read and its update; never transfer a record or recreate a missing one."""
        identifier = _ps_text(feedback_id, "feedback_id", 120)
        if session_id is not None:
            session_id = _ps_text(session_id, "session_id", 120)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read_bound(connection, identifier, session_id)
            if existing is None:
                return None
            binding = (existing.feedback_id, existing.session_id, existing.artifact_id, existing.artifact_sha256, existing.scope_kind)
            updated = change(existing)
            if binding != (updated.feedback_id, updated.session_id, updated.artifact_id, updated.artifact_sha256, updated.scope_kind):
                raise FeedbackConflict("feedback update attempted to change its binding")
            cursor = connection.execute(
                "UPDATE scoped_feedback SET verbatim = ?, record_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE feedback_id = ?",
                (updated.verbatim, json.dumps(updated.to_mapping(), sort_keys=True), identifier),
            )
            if cursor.rowcount != 1:
                raise FeedbackConflict("feedback record changed during its update")
            return updated

    def promote_for_session(
        self, feedback_id: str, session_id: str, *, basis: str, promoted_by: str, scope_kind: str
    ) -> Feedback | None:
        """Promote the current source and write its derived record in one transaction."""
        identifier = _ps_text(feedback_id, "feedback_id", 120)
        session = _ps_text(session_id, "session_id", 120)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._read_bound(connection, identifier, session)
            if existing is None:
                return None
            promoted = promote(existing, basis=basis, promoted_by=promoted_by, scope_kind=scope_kind)
            return self._put_on_connection(connection, promoted)

    def confirm_for_session(self, feedback_id: str, session_id: str, *, confirmed_by: str) -> Feedback | None:
        return self._update_atomic(feedback_id, session_id, lambda existing: confirm(existing, confirmed_by=confirmed_by))

    def withdraw_for_session(self, feedback_id: str, session_id: str) -> Feedback | None:
        return self._update_atomic(feedback_id, session_id, lambda existing: replace(existing, deleted=True))

    def delete(self, feedback_id: str) -> Feedback:
        """Withdraw a record without removing its words; repeated withdrawal stays idempotent."""
        updated = self._update_atomic(feedback_id, None, lambda existing: replace(existing, deleted=True))
        if updated is None:
            raise FeedbackError(f"no feedback {feedback_id!r} to delete")
        return updated
