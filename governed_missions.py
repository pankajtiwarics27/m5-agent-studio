"""AP-029 — governed recurring missions and external delivery.

A schedule is a promise to do something again without being asked, and an external action is a
promise that leaves the machine. Both are ways to accumulate consequences while nobody is watching,
so every rule here is about the difference between *intending* an effect and *causing* it twice.

**A schedule grants only its purpose.** :class:`ScheduleGrant` names one purpose, one project and one
cadence; :class:`RecurringMission` cannot exist without one. Draft creation is not publication, and
the two are different types rather than a boolean: a :class:`DraftArtifact` is what a run produces,
and :func:`requires_external_action` is the only bridge to an external action. A mission whose
scheduled purpose is `draft` can never cause an external effect, whatever it produces.

**An external action is bound to everything that matters.** An :class:`ExternalActionIntent` pins the
account, the destination, the content hash, the policy revision and an expiry. Any change to any of
them — one changed byte of content, a re-pointed destination, a newer policy — invalidates the intent
rather than silently acting on different terms than the ones approved.

**Retries reconcile; they do not resend.** `side_effect_ledger` already owns action identity, replay
policy and the "uncertain" state that refuses a blind replay. This module consumes the same
vocabulary: an action whose outcome is unknown is `uncertain`, an `uncertain` action is reconciled
through a probe rather than re-sent, and reconciliation can conclude `verified_committed`,
`verified_not_applied` (safe to retry) or `unresolved` (stop and escalate). "We will retry and see"
is the one answer that is never available.

**Restarts and missed schedules do not duplicate work.** A run's identity is derived from its mission,
its slot and its purpose, so a second process waking on the same slot produces the same run id and the
ledger refuses it as a duplicate. A missed slot is recorded as *missed* rather than back-filled: firing
three days of a daily mission at once is not a catch-up, it is a burst.

**Runaway activity is stopped by thresholds that fire early.** A :class:`Governor` holds a global
pause, per-project stops and notification thresholds; the governor is consulted *before* a run starts
and before an external action is prepared, and it can refuse to start work even when the schedule is
due. A paused system does not need every producer to remember to check.

**Suggestions are not results.** A :class:`Suggestion` may propose a change and may not claim that
anything caused a business outcome; the word "caused" is refused outright unless it carries a named
experiment. A suggestion never edits public output: it becomes a :class:`DraftArtifact` that a person
promotes, which is the same separation between draft and publication that the program enforces
elsewhere.

Nothing here reads a clock, opens a socket or writes a file: ticks, slot times and outcomes are values
the caller supplies, and the ledger is an in-memory record the caller may persist.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

__all__ = [
    "GovernanceError",
    "ScheduleRefused",
    "PublishRefused",
    "DedupRefused",
    "GovernorRefused",
    "ClaimRefused",
    "SCHEDULE_PURPOSES",
    "CADENCES",
    "EXTERNAL_EFFECT_KINDS",
    "EXTERNAL_ACTION_STATES",
    "RECONCILIATION_VERDICTS",
    "RUN_STATES",
    "SUGGESTION_KINDS",
    "SCHEDULE_TICK_SECONDS",
    "ScheduleGrant",
    "RecurringMission",
    "MissionRun",
    "DraftArtifact",
    "ExternalActionIntent",
    "Reconciliation",
    "RetryDecision",
    "Governor",
    "RunLedger",
    "Suggestion",
    "requires_external_action",
]

GM_SCHEMA_VERSION = 1
GM_MAX_TEXT = 2000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HUMAN_RE = re.compile(r"^(owner|user|reviewer):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: What a schedule may be granted for. ``draft`` twice, deliberately: producing a draft and being
#: allowed to publish it are different purposes, and a grant for one is not a grant for the other.
SCHEDULE_PURPOSES: tuple[str, ...] = ("draft", "publish", "deploy")

#: Cadences, and their period in seconds. A cadence is a whole number of minutes; a "schedule" that
#: fires whenever it likes cannot be reasoned about for missed slots.
CADENCES: Mapping[str, int] = {
    "hourly": 3_600,
    "daily": 86_400,
    "weekly": 604_800,
}

#: The outbound effects this module can describe. Each is a different approval, not a different
#: amount of the same thing.
EXTERNAL_EFFECT_KINDS: tuple[str, ...] = ("publish", "deploy", "notification")

#: Lifecycle of an external action. ``uncertain`` is the state that makes blind retry impossible.
EXTERNAL_ACTION_STATES: tuple[str, ...] = (
    "prepared",
    "dispatched",
    "committed",
    "failed",
    "uncertain",
    "reconciled_committed",
    "reconciled_not_applied",
    "abandoned",
)

#: What a reconciliation probe may conclude.
RECONCILIATION_VERDICTS: tuple[str, ...] = ("verified_committed", "verified_not_applied", "unresolved")

#: Lifecycle of a mission run.
RUN_STATES: tuple[str, ...] = ("started", "completed", "failed", "paused", "missed", "skipped")

#: Kinds of suggestion a run may emit.
SUGGESTION_KINDS: tuple[str, ...] = ("draft_change", "cadence_change", "content_idea")

#: Fixed tick length, matching the hourly cadence floor: slots are derived, never stored.
SCHEDULE_TICK_SECONDS = 3_600

#: Words that assert causation. A metric suggestion may not use them without a named experiment.
_CAUSAL_WORDS: tuple[str, ...] = ("caused", "causes", "resulted in", "drove", "because of", "led to", "grew revenue")


class GovernanceError(ValueError):
    """A schedule, run or external action is malformed or claims more than it holds."""


class ScheduleRefused(GovernanceError):
    """A mission tried to exist or run without the grant that authorises its purpose."""


class PublishRefused(GovernanceError):
    """Something tried to turn a draft into an external effect without authority for it."""


class DedupRefused(GovernanceError):
    """A run or action was re-entered in a way that would duplicate its effect."""


class GovernorRefused(GovernanceError):
    """Activity is paused, stopped or over a notification threshold."""


class ClaimRefused(GovernanceError):
    """A suggestion claimed a causal outcome, or tried to change public output directly."""


def _text(value: object, field_name: str, *, limit: int = GM_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise GovernanceError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise GovernanceError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise GovernanceError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise GovernanceError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _identifier(value: object, field_name: str) -> str:
    text = _text(value, field_name, limit=120)
    if not _ID_RE.fullmatch(text):
        raise GovernanceError(f"{field_name} must be a bounded opaque identifier.")
    return text


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise GovernanceError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _stamp(value: object, field_name: str) -> int:
    """A UTC epoch second supplied by the caller; this module never reads a clock."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GovernanceError(f"{field_name} must be an integer epoch second supplied by the caller.")
    if value < 0:
        raise GovernanceError(f"{field_name} must not be negative.")
    return value


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AC1 — a schedule grants only its purpose; a draft is not a publication.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleGrant:
    """One human decision to let one purpose run in one project on one cadence.

    ``purposes`` is a set of *specific* purposes rather than a "publishing" flag: a grant for
    ``draft`` says nothing about ``publish``, and :meth:`permits` is the only way to ask.
    """

    grant_id: str
    granted_by: str
    project_id: str
    purposes: tuple[str, ...]
    cadence: str
    expires_at: int
    max_runs: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _identifier(self.grant_id, "grant_id"))
        granted_by = _text(self.granted_by, "granted_by", limit=80)
        if not _HUMAN_RE.fullmatch(granted_by):
            raise ScheduleRefused(
                "a recurring schedule must be granted by a named human (owner:, user: or reviewer:); a "
                "schedule nobody authorised is an outbound request on a timer."
            )
        object.__setattr__(self, "granted_by", granted_by)
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        purposes = tuple(dict.fromkeys(_text(item, "purpose", limit=40) for item in self.purposes))
        if not purposes:
            raise ScheduleRefused("a grant must name at least one purpose; 'whatever it needs' is not a purpose.")
        unknown = [item for item in purposes if item not in SCHEDULE_PURPOSES]
        if unknown:
            raise ScheduleRefused(f"unknown schedule purpose {unknown[0]!r}; purposes are {list(SCHEDULE_PURPOSES)}.")
        object.__setattr__(self, "purposes", purposes)
        if self.cadence not in CADENCES:
            raise ScheduleRefused(f"unknown cadence {self.cadence!r}; cadences are {sorted(CADENCES)}.")
        object.__setattr__(self, "expires_at", _stamp(self.expires_at, "expires_at"))
        if isinstance(self.max_runs, bool) or not isinstance(self.max_runs, int) or self.max_runs < 1:
            raise ScheduleRefused("max_runs must be a positive integer; an unbounded schedule is a runaway.")
        if self.expires_at <= 0:
            raise ScheduleRefused("a grant needs a real expiry; an expiry of 0 authorises nothing.")

    @property
    def period_seconds(self) -> int:
        return CADENCES[self.cadence]

    def permits(self, purpose: str, *, now: int, runs_used: int) -> bool:
        """Whether this grant allows one more run of ``purpose`` right now."""
        if purpose not in self.purposes:
            return False
        if runs_used >= self.max_runs:
            return False
        return now < self.expires_at

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "granted_by": self.granted_by,
            "project_id": self.project_id,
            "purposes": list(self.purposes),
            "cadence": self.cadence,
            "expires_at": self.expires_at,
            "max_runs": self.max_runs,
        }


@dataclass(frozen=True, slots=True)
class RecurringMission:
    """A mission that runs on a cadence, for exactly the purposes its grant names."""

    mission_id: str
    project_id: str
    purpose: str
    grant: ScheduleGrant

    def __post_init__(self) -> None:
        if not isinstance(self.grant, ScheduleGrant):
            raise ScheduleRefused("a recurring mission cannot exist without a ScheduleGrant.")
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        if self.project_id != self.grant.project_id:
            raise ScheduleRefused(
                f"this mission is for {self.project_id!r} but its grant names {self.grant.project_id!r}; a "
                "grant cannot be borrowed across projects."
            )
        purpose = _text(self.purpose, "purpose", limit=40)
        if purpose not in SCHEDULE_PURPOSES:
            raise ScheduleRefused(f"unknown purpose {purpose!r}.")
        if purpose not in self.grant.purposes:
            raise ScheduleRefused(
                f"the grant authorises {list(self.grant.purposes)} but this mission's purpose is {purpose!r}; "
                "a schedule may only do what it was granted for."
            )
        object.__setattr__(self, "purpose", purpose)

    @property
    def cadence(self) -> str:
        return self.grant.cadence

    def slot_for(self, now: int) -> int:
        """The cadence slot containing ``now``. Derived, so a restart lands in the same slot."""
        moment = _stamp(now, "now")
        return moment // self.grant.period_seconds

    def run_id_for(self, slot: int) -> str:
        """Deterministic run identity: mission, purpose and slot.

        Two processes waking on the same slot produce the same id, which is what makes a restart a
        duplicate rather than a second delivery.
        """
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ScheduleRefused("slot must be a non-negative integer.")
        return "run-" + _digest_payload(
            {
                "mission_id": self.mission_id,
                "purpose": self.purpose,
                "slot": slot,
                "cadence": self.cadence,
            }
        )[:24]

    def due(self, *, now: int, last_slot: int | None, runs_used: int) -> bool:
        """Whether this mission's slot is due and still permitted by its grant."""
        slot = self.slot_for(now)
        if last_slot is not None and slot <= last_slot:
            return False
        return self.grant.permits(self.purpose, now=now, runs_used=runs_used)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "purpose": self.purpose,
            "cadence": self.cadence,
            "grant": self.grant.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class DraftArtifact:
    """What a draft run produces. It is not published, not queued for publication, and not a promise.

    There is no ``published`` field and no method that sends it: the only way from here to an
    external effect is :func:`requires_external_action`, which demands a separate ``publish`` grant.
    """

    artifact_id: str
    mission_id: str
    project_id: str
    content_digest: str
    title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        object.__setattr__(self, "content_digest", _digest(self.content_digest, "content_digest"))
        object.__setattr__(self, "title", str(self.title or "").strip()[:200])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "content_digest": self.content_digest,
            "title": self.title,
            "published": False,
        }


# ---------------------------------------------------------------------------
# AC2/AC3 — an external action binds its terms and reconciles instead of resending.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalActionIntent:
    """The exact terms an external effect was approved for.

    Every field is part of the binding: :meth:`is_current` is false the moment any of them differs, and
    an action prepared under one set of terms may not be dispatched under another.
    """

    intent_id: str
    kind: str
    account: str
    destination: str
    content_digest: str
    policy_revision: str
    expires_at: int
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _identifier(self.intent_id, "intent_id"))
        kind = _text(self.kind, "kind", limit=40)
        if kind not in EXTERNAL_EFFECT_KINDS:
            raise PublishRefused(f"unknown external effect kind {kind!r}; kinds are {list(EXTERNAL_EFFECT_KINDS)}.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "account", _text(self.account, "account", limit=200))
        object.__setattr__(self, "destination", _text(self.destination, "destination", limit=400))
        object.__setattr__(self, "content_digest", _digest(self.content_digest, "content_digest"))
        object.__setattr__(self, "policy_revision", _identifier(self.policy_revision, "policy_revision"))
        object.__setattr__(self, "expires_at", _stamp(self.expires_at, "expires_at"))
        if self.expires_at <= 0:
            raise PublishRefused("an external action intent needs a real expiry.")
        key = str(self.idempotency_key or "").strip()
        if not key:
            # A provider-supported idempotency key is derived, not optional: without one, a retry after
            # a timeout is a second publish, not the same one.
            key = "m5-" + _digest_payload(
                {
                    "intent_id": self.intent_id,
                    "kind": kind,
                    "account": self.account,
                    "destination": self.destination,
                    "content_digest": self.content_digest,
                    "policy_revision": self.policy_revision,
                }
            )[:40]
        object.__setattr__(self, "idempotency_key", _identifier(key, "idempotency_key"))

    def binding(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "destination": self.destination,
            "content_digest": self.content_digest,
            "policy_revision": self.policy_revision,
        }

    def is_current(self, *, account: str, destination: str, content_digest: str, policy_revision: str, now: int) -> bool:
        """Whether the world still matches the terms this intent was approved under."""
        try:
            current = {
                "account": _text(account, "account", limit=200),
                "destination": _text(destination, "destination", limit=400),
                "content_digest": _digest(content_digest, "content_digest"),
                "policy_revision": _identifier(policy_revision, "policy_revision"),
            }
        except GovernanceError:
            return False
        if current != self.binding():
            return False
        return now < self.expires_at

    def changed_since(self, *, account: str, destination: str, content_digest: str, policy_revision: str) -> tuple[str, ...]:
        """Which bound terms differ, so a refusal can say what changed rather than only that it did."""
        current = {
            "account": account,
            "destination": destination,
            "content_digest": content_digest,
            "policy_revision": policy_revision,
        }
        return tuple(name for name, value in self.binding().items() if current.get(name) != value)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "kind": self.kind,
            "account": self.account,
            "destination": self.destination,
            "content_digest": self.content_digest,
            "policy_revision": self.policy_revision,
            "expires_at": self.expires_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The result of asking the provider what actually happened.

    ``unresolved`` is a real answer and the only safe one when the provider cannot say: it stops the
    action rather than permitting a resend.
    """

    verdict: str
    evidence: str

    def __post_init__(self) -> None:
        if self.verdict not in RECONCILIATION_VERDICTS:
            raise GovernanceError(f"unknown reconciliation verdict {self.verdict!r}.")
        object.__setattr__(self, "evidence", _text(self.evidence, "evidence", limit=600))

    @property
    def retry_allowed(self) -> bool:
        return self.verdict == "verified_not_applied"

    def to_mapping(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "evidence": self.evidence, "retry_allowed": self.retry_allowed}


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """What to do about an action whose outcome is not `committed`."""

    action_id: str
    state: str
    decision: str
    reason: str

    def __post_init__(self) -> None:
        if self.state not in EXTERNAL_ACTION_STATES:
            raise GovernanceError(f"unknown external action state {self.state!r}.")
        if self.decision not in ("retry", "reconcile", "stop"):
            raise GovernanceError(f"unknown retry decision {self.decision!r}.")

    @property
    def sends_again(self) -> bool:
        return self.decision == "retry"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "state": self.state,
            "decision": self.decision,
            "reason": self.reason,
        }


def decide_retry(
    *,
    action_id: str,
    state: str,
    idempotency_supported: bool,
    reconciliation: Reconciliation | None = None,
) -> RetryDecision:
    """Decide whether an unfinished external action may be sent again.

    The order matters: an unknown outcome is reconciled, never resent; a reconciled absence may be
    retried only because the provider can make the retry the *same* operation rather than a new one.
    """
    aid = _identifier(action_id, "action_id")
    if state not in EXTERNAL_ACTION_STATES:
        raise GovernanceError(f"unknown external action state {state!r}.")
    if not isinstance(idempotency_supported, bool):
        raise PublishRefused("idempotency_supported must be a boolean; a guess would hide a second publish.")
    if state in ("committed", "reconciled_committed"):
        return RetryDecision(aid, state, "stop", "the effect already happened exactly once")
    if state == "abandoned":
        return RetryDecision(aid, state, "stop", "the action was abandoned; resending would be a new decision")
    if state in ("prepared", "dispatched", "uncertain"):
        if reconciliation is None:
            return RetryDecision(
                aid,
                state,
                "reconcile",
                f"state {state!r} means the outcome is unknown; an unknown outcome is reconciled, never resent",
            )
        if reconciliation.verdict == "unresolved":
            return RetryDecision(
                aid,
                state,
                "stop",
                "the provider could not say what happened; stopping is the only action that cannot duplicate",
            )
        if reconciliation.verdict == "verified_committed":
            return RetryDecision(aid, state, "stop", "reconciliation confirmed the effect landed")
        if not idempotency_supported:
            return RetryDecision(
                aid,
                state,
                "stop",
                "reconciliation says the effect is absent, but the provider has no idempotency key, so a "
                "retry could duplicate an effect that landed after the probe",
            )
        return RetryDecision(
            aid,
            state,
            "retry",
            "reconciliation verified the effect is absent and the provider supports idempotent retry",
        )
    if state == "failed":
        if not idempotency_supported:
            return RetryDecision(
                aid, state, "stop", "a failed send cannot be retried safely without provider idempotency"
            )
        return RetryDecision(aid, state, "retry", "the send failed outright and the retry is idempotent")
    if state == "reconciled_not_applied":
        if not idempotency_supported:
            return RetryDecision(aid, state, "stop", "no idempotency key, so a resend is a second publish")
        return RetryDecision(aid, state, "retry", "reconciliation verified absence; the retry is idempotent")
    return RetryDecision(aid, state, "stop", f"state {state!r} admits no automatic resend")


def requires_external_action(
    draft: DraftArtifact,
    *,
    grant: ScheduleGrant,
    kind: str,
    account: str,
    destination: str,
    now: int,
    ttl_seconds: int = 3_600,
    runs_used: int = 0,
) -> ExternalActionIntent:
    """Turn a draft into an external action intent, which needs its own `publish` grant.

    This is the only bridge from a draft to an external effect, and it takes a *separate* grant. A
    mission granted only ``draft`` cannot call it successfully, whatever it produced.
    """
    if not isinstance(draft, DraftArtifact):
        raise PublishRefused("requires_external_action needs a DraftArtifact; a run result is not a publication.")
    if not isinstance(grant, ScheduleGrant):
        raise PublishRefused("an external action needs an explicit grant; a draft grant is not one.")
    purpose = "publish" if kind in ("publish", "notification") else "deploy"
    if purpose not in grant.purposes:
        raise PublishRefused(
            f"the grant authorises {list(grant.purposes)} but this {kind} needs {purpose!r}; producing a draft "
            "never implies permission to publish it."
        )
    if grant.project_id != draft.project_id:
        raise PublishRefused("the grant is for another project; authority does not travel between projects.")
    moment = _stamp(now, "now")
    if not grant.permits(purpose, now=moment, runs_used=runs_used):
        raise PublishRefused("the grant has expired or its run cap is spent; it does not authorise this action.")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise PublishRefused("ttl_seconds must be a positive integer; an intent with no expiry never lapses.")
    expires_at = min(moment + ttl_seconds, grant.expires_at)
    return ExternalActionIntent(
        intent_id=f"{draft.artifact_id}-{kind}",
        kind=kind,
        account=account,
        destination=destination,
        content_digest=draft.content_digest,
        policy_revision=grant.grant_id,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# AC4/AC5 — the run ledger, dedup across restarts, and the governor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MissionRun:
    """One attempt to run a mission in one slot.

    ``state="missed"`` is a recorded fact, not an absence: a slot that was skipped because the system
    was down is written down as missed so nobody later believes it was completed.
    """

    run_id: str
    mission_id: str
    project_id: str
    purpose: str
    slot: int
    state: str
    artifacts: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "mission_id", _identifier(self.mission_id, "mission_id"))
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        purpose = _text(self.purpose, "purpose", limit=40)
        if purpose not in SCHEDULE_PURPOSES:
            raise ScheduleRefused(f"unknown purpose {purpose!r}.")
        object.__setattr__(self, "purpose", purpose)
        if isinstance(self.slot, bool) or not isinstance(self.slot, int) or self.slot < 0:
            raise GovernanceError("slot must be a non-negative integer.")
        if self.state not in RUN_STATES:
            raise GovernanceError(f"unknown run state {self.state!r}; states are {list(RUN_STATES)}.")
        object.__setattr__(self, "artifacts", tuple(_identifier(item, "artifact id") for item in self.artifacts))
        object.__setattr__(self, "detail", str(self.detail or "").strip()[:600])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "project_id": self.project_id,
            "purpose": self.purpose,
            "slot": self.slot,
            "state": self.state,
            "artifacts": list(self.artifacts),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Governor:
    """Global pause, per-project stops and notification thresholds.

    ``stopped_projects`` is a hard stop; ``notify_after`` is a soft threshold that does not stop work
    but *requires* a notification before the next run, so a threshold that fires produces a message
    rather than nothing.
    """

    globally_paused: bool = False
    stopped_projects: tuple[str, ...] = ()
    notify_after_runs: int = 0
    notify_recipient: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.globally_paused, bool):
            raise GovernorRefused("globally_paused must be a boolean.")
        object.__setattr__(
            self,
            "stopped_projects",
            tuple(dict.fromkeys(_identifier(item, "project_id") for item in self.stopped_projects)),
        )
        if isinstance(self.notify_after_runs, bool) or not isinstance(self.notify_after_runs, int):
            raise GovernorRefused("notify_after_runs must be an integer.")
        if self.notify_after_runs < 0:
            raise GovernorRefused("notify_after_runs must not be negative.")
        recipient = str(self.notify_recipient or "").strip()
        if self.notify_after_runs and not _HUMAN_RE.fullmatch(recipient):
            raise GovernorRefused(
                "a notification threshold needs a named recipient (owner:, user: or reviewer:); a threshold "
                "nobody is told about is not a safeguard."
            )
        object.__setattr__(self, "notify_recipient", recipient)

    def refuse(self, *, project_id: str, runs_in_window: int = 0) -> str | None:
        """Why work may not run, or ``None``. Consulted before a run starts."""
        if self.globally_paused:
            return "the global pause is engaged; no scheduled work runs until it is released"
        if project_id in self.stopped_projects:
            return f"project {project_id!r} is stopped by the operator"
        if self.notify_after_runs and runs_in_window >= self.notify_after_runs:
            return (
                f"{runs_in_window} runs in the window reached the notification threshold "
                f"({self.notify_after_runs}); {self.notify_recipient} must be told before the next run"
            )
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "globally_paused": self.globally_paused,
            "stopped_projects": list(self.stopped_projects),
            "notify_after_runs": self.notify_after_runs,
            "notify_recipient": self.notify_recipient,
        }


@dataclass(frozen=True, slots=True)
class RunLedger:
    """An in-memory, append-only record of runs and external actions.

    ``claim`` returns a new ledger or refuses; there is no overwrite. A repeated ``run_id`` is a
    duplicate delivery, not a retry, which is exactly what a restart on the same slot would otherwise
    produce.
    """

    runs: Mapping[str, MissionRun] | None = None
    actions: Mapping[str, str] | None = None
    idempotency_keys: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        runs = dict(self.runs or {})
        for key, run in runs.items():
            if not isinstance(run, MissionRun) or run.run_id != key:
                raise GovernanceError("runs must be keyed by their own run_id.")
        object.__setattr__(self, "runs", runs)
        actions = dict(self.actions or {})
        for key, state in actions.items():
            _identifier(key, "action_id")
            if state not in EXTERNAL_ACTION_STATES:
                raise GovernanceError(f"unknown external action state {state!r}.")
        object.__setattr__(self, "actions", actions)
        keys = dict(self.idempotency_keys or {})
        for key, action_id in keys.items():
            _identifier(key, "idempotency key")
            if action_id not in actions:
                raise GovernanceError("an idempotency key must name a recorded action.")
        object.__setattr__(self, "idempotency_keys", keys)

    # -- runs ---------------------------------------------------------------

    def claim_run(self, run: MissionRun) -> "RunLedger":
        """Record a run as started, or refuse if this slot already ran."""
        if not isinstance(run, MissionRun):
            raise DedupRefused("claim_run needs a MissionRun.")
        existing = self._runs().get(run.run_id)
        if existing is not None:
            raise DedupRefused(
                f"run {run.run_id!r} already exists in state {existing.state!r}; a restart inside the same "
                "slot is a duplicate delivery, not a new run"
            )
        runs = dict(self._runs())
        runs[run.run_id] = run
        return replace(self, runs=runs)

    def finish_run(self, run_id: str, *, state: str, artifacts: Iterable[str] = (), detail: str = "") -> "RunLedger":
        """Record how a run ended. A run may be finished exactly once."""
        rid = _identifier(run_id, "run_id")
        existing = self._runs().get(rid)
        if existing is None:
            raise DedupRefused(f"run {rid!r} was never claimed; finishing it would invent a run.")
        if existing.state != "started":
            raise DedupRefused(
                f"run {rid!r} is already {existing.state!r}; a second result for one slot is how two "
                "deliveries get recorded as one"
            )
        if state not in RUN_STATES:
            raise GovernanceError(f"unknown run state {state!r}.")
        if state == "started":
            raise GovernanceError("finishing a run in 'started' records no outcome.")
        finished = replace(existing, state=state, artifacts=tuple(artifacts), detail=detail)
        # Re-validate through the dataclass so artifact ids and detail bounds are checked here too.
        finished = MissionRun(
            run_id=finished.run_id,
            mission_id=finished.mission_id,
            project_id=finished.project_id,
            purpose=finished.purpose,
            slot=finished.slot,
            state=finished.state,
            artifacts=finished.artifacts,
            detail=finished.detail,
        )
        runs = dict(self._runs())
        runs[rid] = finished
        return replace(self, runs=runs)

    def record_missed(self, *, mission: "RecurringMission", slot: int) -> "RunLedger":
        """Write down a slot that did not run, rather than leaving a hole that looks like success."""
        if not isinstance(mission, RecurringMission):
            raise DedupRefused("record_missed needs the RecurringMission that missed its slot.")
        run = MissionRun(
            run_id=mission.run_id_for(slot),
            mission_id=mission.mission_id,
            project_id=mission.project_id,
            purpose=mission.purpose,
            slot=slot,
            state="missed",
            detail="the schedule slot passed without a run; recorded so it is not mistaken for a completed one",
        )
        return self.claim_run(run)

    def runs_for(self, mission_id: str) -> tuple[MissionRun, ...]:
        return tuple(run for run in self._runs().values() if run.mission_id == mission_id)

    def runs_used(self, mission_id: str, *, purpose: str | None = None) -> int:
        return sum(
            1
            for run in self._runs().values()
            if run.mission_id == mission_id and (purpose is None or run.purpose == purpose) and run.state != "missed"
        )

    def last_slot(self, mission_id: str) -> int | None:
        slots = [run.slot for run in self._runs().values() if run.mission_id == mission_id]
        return max(slots) if slots else None

    # -- external actions ---------------------------------------------------

    def record_action(self, intent: "ExternalActionIntent", *, state: str) -> "RunLedger":
        """Record an external action in a state, refusing a second action under one idempotency key."""
        if not isinstance(intent, ExternalActionIntent):
            raise PublishRefused("record_action needs an ExternalActionIntent.")
        if state not in EXTERNAL_ACTION_STATES:
            raise GovernanceError(f"unknown external action state {state!r}.")
        actions = dict(self._actions())
        keys = dict(self._keys())
        recorded_key = {key: value for key, value in keys.items() if value == intent.intent_id}
        for key in recorded_key:
            if key != intent.idempotency_key:
                raise DedupRefused(
                    f"action {intent.intent_id!r} was already recorded under idempotency key {key!r}; "
                    "re-recording it under a new key means the action's terms changed after it was "
                    "approved, which is a different action wearing the same name"
                )
        previous = keys.get(intent.idempotency_key)
        if previous is not None:
            if previous != intent.intent_id:
                raise DedupRefused(
                    f"idempotency key {intent.idempotency_key!r} already belongs to action {previous!r}; the "
                    "same key for a different action would let one send stand in for another"
                )
            if actions.get(previous) in ("committed", "reconciled_committed") and state in ("prepared", "dispatched"):
                raise DedupRefused(
                    f"action {previous!r} already committed under this key; recording a new send for it is a "
                    "duplicate external effect"
                )
        actions[intent.intent_id] = state
        keys[intent.idempotency_key] = intent.intent_id
        return replace(self, actions=actions, idempotency_keys=keys)

    def set_action_state(self, action_id: str, *, state: str) -> "RunLedger":
        aid = _identifier(action_id, "action_id")
        if state not in EXTERNAL_ACTION_STATES:
            raise GovernanceError(f"unknown external action state {state!r}.")
        actions = dict(self._actions())
        if aid not in actions:
            raise DedupRefused(f"action {aid!r} was never recorded; its state cannot be advanced from nothing.")
        actions[aid] = state
        return replace(self, actions=actions)

    def action_state(self, action_id: str) -> str | None:
        return self._actions().get(_identifier(action_id, "action_id"))

    # -- views --------------------------------------------------------------

    def _runs(self) -> Mapping[str, MissionRun]:
        return self.runs or {}

    def _actions(self) -> Mapping[str, str]:
        return self.actions or {}

    def _keys(self) -> Mapping[str, str]:
        return self.idempotency_keys or {}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": GM_SCHEMA_VERSION,
            "runs": [run.to_mapping() for run in self._runs().values()],
            "actions": dict(sorted(self._actions().items())),
            "idempotency_keys": dict(sorted(self._keys().items())),
        }

    def fingerprint(self) -> str:
        return _digest_payload(self.to_mapping())


# ---------------------------------------------------------------------------
# AC6 — suggestions are suggestions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A proposal from a run: a change to make, or an idea to consider.

    It cannot claim causation and it cannot touch public output. A suggestion that says publishing
    something "caused" a revenue change is refused unless it names the experiment that measured it,
    and a suggestion may only ever produce a draft.
    """

    suggestion_id: str
    kind: str
    proposal: str
    experiment_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "suggestion_id", _identifier(self.suggestion_id, "suggestion_id"))
        kind = _text(self.kind, "kind", limit=40)
        if kind not in SUGGESTION_KINDS:
            raise ClaimRefused(f"unknown suggestion kind {kind!r}; kinds are {list(SUGGESTION_KINDS)}.")
        object.__setattr__(self, "kind", kind)
        proposal = _text(self.proposal, "proposal", limit=GM_MAX_TEXT)
        lowered = proposal.lower()
        causal = [word for word in _CAUSAL_WORDS if word in lowered]
        if causal and self.experiment_id is None:
            raise ClaimRefused(
                f"this suggestion claims causation ({causal[0]!r}) with no named experiment; a metric "
                "movement is an observation, and a business outcome it did not measure is a story"
            )
        if self.experiment_id is not None:
            object.__setattr__(self, "experiment_id", _identifier(self.experiment_id, "experiment_id"))
        object.__setattr__(self, "proposal", proposal)

    @property
    def changes_public_output(self) -> bool:
        """A suggestion never changes published output; it becomes a draft a person decides about."""
        return False

    def to_artifact(self, *, mission: "RecurringMission", content_digest: str) -> DraftArtifact:
        if not isinstance(mission, RecurringMission):
            raise ClaimRefused("a suggestion becomes a draft inside a mission, not on its own.")
        return DraftArtifact(
            artifact_id=f"{self.suggestion_id}-draft",
            mission_id=mission.mission_id,
            project_id=mission.project_id,
            content_digest=content_digest,
            title=self.kind,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "kind": self.kind,
            "proposal": self.proposal,
            "experiment_id": self.experiment_id,
            "changes_public_output": False,
        }
