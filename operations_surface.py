"""AP-031 — progressive configuration, honest readiness and operable state.

Three failures make a capable system feel untrustworthy, and none of them is a missing feature.

* **Six different words for the same shrug.** "Configured", "available", "ready", "tested",
  "qualified", "experimental", "blocked" and "unavailable" get used interchangeably, so a pane says
  "ready" when it means "a key exists" and the reader learns to trust none of it. Here the states are a
  closed, ordered set and one precedence function collapses them, most-blocking first:
  ``blocked`` > ``unavailable`` > ``experimental`` > ``configured`` > ``tested`` > ``qualified``. Each
  state is derivable only from the observation that supports it: ``qualified`` needs a measured result,
  ``tested`` needs a run that was actually observed, and *absence of an observation is never upgraded*
  — a step nobody ran is ``configured`` at best, never ``qualified``.

* **A readiness screen that costs money to look at.** A "check" that quietly contacts a paid provider,
  or that says "ready" because a socket answered, is how a first-run experience generates a bill.
  :class:`ReadinessCheck` records ``basis`` (``measured`` or ``declared``), whether it spends money and
  whether it leaves the machine; a check that would spend requires an explicit opt-in *before* it is
  allowed to run, and a reported readiness with any unmeasured row says so in the summary rather than
  averaging it away.

* **The settings that matter, hidden in the settings.** Power settings are progressively disclosed
  behind a named level, and the count of what is behind the fold is reported — so "simple by default"
  cannot quietly become "capable things removed". Every existing feature has an entry, and the surface
  exposes them by level rather than deleting them.

Two more lines. Grants, preferences and references are *inspectable at the same place they are
revocable*: :class:`GrantView` can always be revoked and a capability that cannot be revoked says why.
And help/examples come from the same capability inventory the UI renders, so a help page cannot promise
a capability the build does not have; artifact acceptance is a separate field from external authority,
because "this draft is good" never means "you may publish it".

Nothing here reads a clock, calls a provider or renders HTML: the report is a value, and the routes
that serve it are a separate, reviewed surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "OperationsError",
    "StateRefused",
    "DisclosureRefused",
    "GrantRefused",
    "HelpRefused",
    "CAPABILITY_STATES",
    "STATE_PRECEDENCE",
    "CHECK_BASES",
    "DISCLOSURE_LEVELS",
    "PREFERENCE_ACTIONS",
    "CapabilityObservation",
    "CapabilityStatus",
    "status_for",
    "ReadinessCheck",
    "ReadinessReport",
    "build_readiness",
    "GrantView",
    "PreferenceRecord",
    "ReferenceRecord",
    "PreferenceStore",
    "SettingEntry",
    "ProgressiveDisclosure",
    "build_disclosure",
    "HelpEntry",
    "build_help",
    "OperationsReport",
    "build_report",
]

OS_SCHEMA_VERSION = 1
OS_MAX_TEXT = 2000

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HUMAN_RE = re.compile(r"^(owner|user|reviewer):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The six states a capability may be in. Deliberately not a boolean, and deliberately not a numeric
#: score that can be averaged into something meaningless.
CAPABILITY_STATES: tuple[str, ...] = (
    "qualified",
    "tested",
    "configured",
    "experimental",
    "blocked",
    "unavailable",
)

#: Worst-first. Used to collapse several observations of one capability into a single state.
STATE_PRECEDENCE: tuple[str, ...] = (
    "blocked",
    "unavailable",
    "experimental",
    "configured",
    "tested",
    "qualified",
)

#: How a readiness row knows what it knows. ``declared`` is a configuration value; ``measured`` is a
#: result something actually returned.
CHECK_BASES: tuple[str, ...] = ("measured", "declared", "unmeasured")

#: Progressive disclosure levels, narrowest first.
DISCLOSURE_LEVELS: tuple[str, ...] = ("simple", "advanced", "expert")

#: What a reader may do with a stored preference or reference.
PREFERENCE_ACTIONS: tuple[str, ...] = ("correct", "export", "delete")


class OperationsError(ValueError):
    """An operations record is malformed or claims more than its evidence supports."""


class StateRefused(OperationsError):
    """A capability was described with a state that its observation does not support."""


class DisclosureRefused(OperationsError):
    """A setting was hidden without a level to reveal it, or a level was unknown."""


class GrantRefused(OperationsError):
    """A grant view was built without the information needed to inspect or revoke it."""


class HelpRefused(OperationsError):
    """Help described a capability the inventory does not have, or merged acceptance with authority."""


def _text(value: object, field_name: str, *, limit: int = OS_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise OperationsError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise OperationsError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise OperationsError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise OperationsError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _optional_text(value: object, *, limit: int = OS_MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        raise OperationsError(f"text must be at most {limit} characters.")
    return text


def _identifier(value: object, field_name: str) -> str:
    text = _text(value, field_name, limit=120)
    if not _ID_RE.fullmatch(text):
        raise OperationsError(f"{field_name} must be a bounded opaque identifier.")
    return text


def _count(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OperationsError(f"{field_name} must be an integer of at least {minimum}.")
    return value


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# AC1 — the six states are distinct and derived from evidence.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    """What is actually known about one capability, as separate facts rather than a status.

    Every field is a distinct kind of evidence: ``configured`` is a setting, ``tested``/``qualified``
    are results, ``blocked`` and ``unavailable`` are refusals of different kinds (a policy stop versus
    an absence). Keeping them apart is what makes :func:`status_for` honest.
    """

    capability_id: str
    label: str
    configured: bool = False
    tested: bool = False
    qualified: bool = False
    experimental: bool = False
    blocked: bool = False
    unavailable: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _identifier(self.capability_id, "capability_id"))
        object.__setattr__(self, "label", _text(self.label, "label", limit=160))
        for field_name in ("configured", "tested", "qualified", "experimental", "blocked", "unavailable"):
            if not isinstance(getattr(self, field_name), bool):
                raise StateRefused(f"{field_name} must be a boolean.")
        if self.qualified and not self.tested:
            raise StateRefused(
                f"{self.capability_id!r} claims qualification without a test; a qualified capability is one "
                "that was tested here, not one that is expected to work"
            )
        if self.tested and not self.configured:
            raise StateRefused(
                f"{self.capability_id!r} claims a test it could not have run: nothing is configured to test"
            )
        if self.blocked and self.unavailable:
            raise StateRefused(
                f"{self.capability_id!r} is both blocked and unavailable; they are different answers -- "
                "'policy forbids it' versus 'nothing provides it'"
            )
        object.__setattr__(self, "evidence", _optional_text(self.evidence, limit=600))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "configured": self.configured,
            "tested": self.tested,
            "qualified": self.qualified,
            "experimental": self.experimental,
            "blocked": self.blocked,
            "unavailable": self.unavailable,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """The single state a reader sees, plus every observation that produced it."""

    capability_id: str
    label: str
    state: str
    reason: str
    observations: CapabilityObservation

    def __post_init__(self) -> None:
        if self.state not in CAPABILITY_STATES:
            raise StateRefused(f"unknown capability state {self.state!r}; states are {list(CAPABILITY_STATES)}.")
        object.__setattr__(self, "reason", _text(self.reason, "reason", limit=400))

    @property
    def usable(self) -> bool:
        """Whether a reader may rely on it right now. Experimental is usable but not promised."""
        return self.state in ("qualified", "tested", "experimental")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "label": self.label,
            "state": self.state,
            "usable": self.usable,
            "reason": self.reason,
            "observations": self.observations.to_mapping(),
        }


def status_for(observation: CapabilityObservation) -> CapabilityStatus:
    """Collapse one observation into exactly one state, worst-first.

    The order is the point. A capability can be configured *and* blocked; reporting it as configured
    because the configuration exists is the shop-window version of the truth.
    """
    if not isinstance(observation, CapabilityObservation):
        raise StateRefused("status_for needs a CapabilityObservation.")
    if observation.blocked:
        state, reason = "blocked", "a policy or authority rule forbids this here"
    elif observation.unavailable:
        state, reason = "unavailable", "nothing in this build provides it"
    elif observation.experimental:
        state, reason = "experimental", "it works here but is not promised; it may change or be withdrawn"
    elif not observation.configured:
        state, reason = "unavailable", "it is not configured, so this build cannot offer it"
    elif not observation.tested:
        state, reason = "configured", "it is set up but has not been run here"
    elif not observation.qualified:
        state, reason = "tested", "it ran here, but not against a qualification case"
    else:
        state, reason = "qualified", "it passed a measured qualification here"
    if observation.evidence:
        reason = f"{reason}: {observation.evidence}"
    return CapabilityStatus(
        capability_id=observation.capability_id,
        label=observation.label,
        state=state,
        reason=reason,
        observations=observation,
    )


# ---------------------------------------------------------------------------
# AC3 — readiness reports measured results and never hides a cost.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One readiness row: what it claims, how it knows, and what looking at it costs.

    A ``spends_money`` or ``uses_network`` check is refused unless the caller states that the reader
    opted in *before* it ran: a status screen that quietly bills someone, or phones a provider during
    first run, is the failure this field exists to prevent.
    """

    check_id: str
    label: str
    basis: str
    ok: bool | None
    detail: str = ""
    spends_money: bool = False
    uses_network: bool = False
    opt_in_recorded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _identifier(self.check_id, "check_id"))
        object.__setattr__(self, "label", _text(self.label, "label", limit=160))
        if self.basis not in CHECK_BASES:
            raise OperationsError(f"unknown check basis {self.basis!r}; bases are {list(CHECK_BASES)}.")
        if self.ok is not None and not isinstance(self.ok, bool):
            raise OperationsError("ok must be a boolean or None for 'not measured'.")
        object.__setattr__(self, "detail", _optional_text(self.detail, limit=600))
        for field_name in ("spends_money", "uses_network", "opt_in_recorded"):
            if not isinstance(getattr(self, field_name), bool):
                raise OperationsError(f"{field_name} must be a boolean.")
        if self.basis == "measured" and self.ok is None:
            raise OperationsError(
                f"{self.check_id!r} claims a measured basis but reports no result; 'measured' with no answer "
                "is exactly the optimistic default this field exists to stop"
            )
        if self.basis == "unmeasured" and self.ok is not None:
            raise OperationsError(
                f"{self.check_id!r} has no basis but reports a result; a result requires having looked"
            )
        if (self.spends_money or self.uses_network) and (self.basis == "measured" or self.ok is not None):
            if not self.opt_in_recorded:
                raise OperationsError(
                    f"{self.check_id!r} would spend money or contact a network to answer, and no opt-in was "
                    "recorded before it ran; a readiness screen must not bill or phone anyone to look ready"
                )

    @property
    def hidden_cost(self) -> bool:
        return bool(self.spends_money or self.uses_network) and not self.opt_in_recorded

    def to_mapping(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "label": self.label,
            "basis": self.basis,
            "ok": self.ok,
            "detail": self.detail,
            "spends_money": self.spends_money,
            "uses_network": self.uses_network,
            "opt_in_recorded": self.opt_in_recorded,
            "hidden_cost": self.hidden_cost,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """The one-line verdict, the counts behind it and the rows that produced both."""

    checks: tuple[ReadinessCheck, ...]
    headline: str = ""

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if not checks:
            raise OperationsError("a readiness report with no checks says nothing and looks fine")
        seen = [check.check_id for check in checks]
        if len(set(seen)) != len(seen):
            raise OperationsError("a readiness check is reported twice.")
        for check in checks:
            if not isinstance(check, ReadinessCheck):
                raise OperationsError("checks must hold ReadinessCheck values.")
        object.__setattr__(self, "checks", checks)

    @property
    def measured_count(self) -> int:
        return sum(1 for check in self.checks if check.basis == "measured")

    @property
    def unmeasured_count(self) -> int:
        return sum(1 for check in self.checks if check.basis != "measured")

    @property
    def failing(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.ok is False)

    @property
    def ready(self) -> bool:
        """Ready means: every row was measured and passed. Nothing less is called ready here."""
        return self.unmeasured_count == 0 and not self.failing

    @property
    def verdict(self) -> str:
        if self.ready:
            return "ready"
        if self.failing:
            return "needs-you"
        return "partly-verified"

    @property
    def summary(self) -> str:
        if self.ready:
            return f"Ready: all {len(self.checks)} checks were measured and passed."
        if self.failing:
            first = self.failing[0]
            return f"{len(self.failing)} of {len(self.checks)} checks failed; first: {first.label} ({first.detail or 'no detail'})."
        return (
            f"{self.measured_count} of {len(self.checks)} checks were measured; "
            f"{self.unmeasured_count} are unverified, so this is not a clean bill of health."
        )

    @property
    def costs_hidden(self) -> bool:
        return any(check.hidden_cost for check in self.checks)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": OS_SCHEMA_VERSION,
            "verdict": self.verdict,
            "ready": self.ready,
            "headline": self.headline or self.summary,
            "summary": self.summary,
            "measured_count": self.measured_count,
            "unmeasured_count": self.unmeasured_count,
            "failing_ids": [check.check_id for check in self.failing],
            "checks": [check.to_mapping() for check in self.checks],
            "spends_money": any(check.spends_money for check in self.checks),
            "uses_network": any(check.uses_network for check in self.checks),
            "costs_hidden": self.costs_hidden,
        }


def build_readiness(checks: Iterable[ReadinessCheck], *, headline: str = "") -> ReadinessReport:
    return ReadinessReport(checks=tuple(checks), headline=_optional_text(headline, limit=300))


def build_readiness_from_preflight(preflight: Mapping[str, Any]) -> ReadinessReport:
    """Project the existing onboarding preflight into readiness rows without inventing results.

    ``onboarding_preflight`` reports what is configured, not what was measured, so every row it
    produces is ``declared`` — except a runtime probe that actually ran, which is the one thing that
    module can observe. Turning a declared row into a measured one here would be the flattery this
    story is about.
    """
    if not isinstance(preflight, Mapping):
        raise OperationsError("a preflight snapshot must be a mapping.")
    rows: list[ReadinessCheck] = []
    for step in preflight.get("steps", ()):
        if not isinstance(step, Mapping):
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        status = str(step.get("status") or "action-required")
        rows.append(
            ReadinessCheck(
                check_id=f"preflight:{step_id}",
                label=str(step.get("title") or step_id),
                basis="declared",
                ok=status in ("ready", "optional"),
                detail=str(step.get("detail") or ""),
            )
        )
    probe = str(preflight.get("local_probe") or "not-run")
    if probe == "ran":
        rows.append(
            ReadinessCheck(
                check_id="preflight:local-runtime-probe",
                label="Local runtime probe",
                basis="measured",
                ok=bool(preflight.get("local_models_available", 0)),
                detail="the local runtime answered when asked",
            )
        )
    return ReadinessReport(checks=tuple(rows))


# ---------------------------------------------------------------------------
# AC2 — grants are inspectable where they are revocable; preferences are the reader's.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrantView:
    """A grant as the reader needs it: what it allows, until when, and how to stop it.

    ``revocable`` is derived from whether a revocation route exists, and a grant that cannot be revoked
    must say why — an authority control with no off switch is the thing a settings screen must not
    quietly display as normal.
    """

    grant_id: str
    granted_by: str
    capabilities: tuple[str, ...]
    resource_scopes: tuple[str, ...]
    expires_at: int
    remaining_uses: int | None
    revocable: bool
    revocation_blocked_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _identifier(self.grant_id, "grant_id"))
        granter = _text(self.granted_by, "granted_by", limit=80)
        if not _HUMAN_RE.fullmatch(granter):
            raise GrantRefused("a grant view must name who granted it (owner:, user: or reviewer:).")
        object.__setattr__(self, "granted_by", granter)
        object.__setattr__(
            self, "capabilities", tuple(dict.fromkeys(_identifier(item, "capability") for item in self.capabilities))
        )
        if not self.capabilities:
            raise GrantRefused("a grant view with no capabilities describes nothing a reader could inspect.")
        object.__setattr__(
            self,
            "resource_scopes",
            tuple(dict.fromkeys(_text(item, "resource scope", limit=300) for item in self.resource_scopes)),
        )
        object.__setattr__(self, "expires_at", _count(self.expires_at, "expires_at"))
        if self.remaining_uses is not None:
            object.__setattr__(self, "remaining_uses", _count(self.remaining_uses, "remaining_uses"))
        if not isinstance(self.revocable, bool):
            raise GrantRefused("revocable must be a boolean.")
        reason = _optional_text(self.revocation_blocked_reason, limit=400)
        if not self.revocable and not reason:
            raise GrantRefused(
                f"grant {self.grant_id!r} is not revocable and gives no reason; an authority with no off "
                "switch must say why it has none"
            )
        object.__setattr__(self, "revocation_blocked_reason", reason)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "granted_by": self.granted_by,
            "capabilities": list(self.capabilities),
            "resource_scopes": list(self.resource_scopes),
            "expires_at": self.expires_at,
            "remaining_uses": self.remaining_uses,
            "revocable": self.revocable,
            "revocation_blocked_reason": self.revocation_blocked_reason,
            "revoke_route": f"revoke:{self.grant_id}" if self.revocable else "",
        }


@dataclass(frozen=True, slots=True)
class PreferenceRecord:
    """One stored preference. Plain data, so it can be corrected, exported and deleted."""

    key: str
    value: str
    updated_at: int
    source: str = "user"

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _identifier(self.key, "preference key"))
        object.__setattr__(self, "value", _text(self.value, "preference value", limit=1000))
        object.__setattr__(self, "updated_at", _count(self.updated_at, "updated_at"))
        source = _text(self.source, "source", limit=40)
        if source not in ("user", "default", "migrated"):
            raise OperationsError(f"unknown preference source {source!r}.")
        object.__setattr__(self, "source", source)

    def to_mapping(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "updated_at": self.updated_at, "source": self.source}


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    """One stored reference (an approved benchmark, a source, a style guide the reader supplied)."""

    reference_id: str
    title: str
    digest: str
    added_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_id", _identifier(self.reference_id, "reference_id"))
        object.__setattr__(self, "title", _text(self.title, "title", limit=200))
        if not _DIGEST_RE.fullmatch(str(self.digest).strip().lower()):
            raise OperationsError("reference digest must be a lowercase SHA-256 hex digest.")
        object.__setattr__(self, "digest", str(self.digest).strip().lower())
        object.__setattr__(self, "added_at", _count(self.added_at, "added_at"))

    def to_mapping(self) -> dict[str, Any]:
        return {"reference_id": self.reference_id, "title": self.title, "digest": self.digest, "added_at": self.added_at}


@dataclass(frozen=True, slots=True)
class PreferenceStore:
    """The reader's own data: correctable, exportable and deletable, by construction.

    There is no hidden state: :meth:`export` renders everything, :meth:`delete` removes a key and a
    reference, and :meth:`correct` refuses to silently overwrite a value the reader set with a default.
    """

    preferences: Mapping[str, PreferenceRecord] = field(default_factory=dict)
    references: Mapping[str, ReferenceRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        preferences = dict(self.preferences)
        for key, record in preferences.items():
            if not isinstance(record, PreferenceRecord) or record.key != key:
                raise OperationsError("preferences must be keyed by their own key.")
        object.__setattr__(self, "preferences", preferences)
        references = dict(self.references)
        for ref_key, ref_record in references.items():
            if not isinstance(ref_record, ReferenceRecord) or ref_record.reference_id != ref_key:
                raise OperationsError("references must be keyed by their own reference_id.")
        object.__setattr__(self, "references", references)

    def correct(self, *, key: str, value: str, now: int) -> "PreferenceStore":
        preferences = dict(self.preferences)
        preferences[key] = PreferenceRecord(key=key, value=value, updated_at=now, source="user")
        return PreferenceStore(preferences=preferences, references=self.references)

    def delete(self, *, key: str | None = None, reference_id: str | None = None) -> "PreferenceStore":
        if (key is None) == (reference_id is None):
            raise OperationsError(
                "delete takes exactly one of key or reference_id; a delete that guesses its target is how "
                "the wrong preference disappears"
            )
        preferences = dict(self.preferences)
        references = dict(self.references)
        if key is not None:
            if key not in preferences:
                raise OperationsError(f"no preference {key!r} to delete.")
            del preferences[key]
        if reference_id is not None:
            if reference_id not in references:
                raise OperationsError(f"no reference {reference_id!r} to delete.")
            del references[reference_id]
        return PreferenceStore(preferences=preferences, references=references)

    def export(self, *, now: int) -> dict[str, Any]:
        """Everything the reader owns, in one mapping, with a stable digest over it."""
        payload = {
            "schema_version": OS_SCHEMA_VERSION,
            "exported_at": _count(now, "now"),
            "preferences": [record.to_mapping() for _, record in sorted(self.preferences.items())],
            "references": [record.to_mapping() for _, record in sorted(self.references.items())],
        }
        payload["digest"] = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        return payload

    def actions_available(self) -> tuple[str, ...]:
        return PREFERENCE_ACTIONS


# ---------------------------------------------------------------------------
# AC4 — progressive disclosure without removing anything.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingEntry:
    """One setting, its disclosure level and whether it was in the build before this story."""

    setting_id: str
    label: str
    level: str
    existed_before: bool = True
    reversible: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "setting_id", _identifier(self.setting_id, "setting_id"))
        object.__setattr__(self, "label", _text(self.label, "label", limit=200))
        if self.level not in DISCLOSURE_LEVELS:
            raise DisclosureRefused(
                f"unknown disclosure level {self.level!r}; levels are {list(DISCLOSURE_LEVELS)}"
            )
        for field_name in ("existed_before", "reversible"):
            if not isinstance(getattr(self, field_name), bool):
                raise DisclosureRefused(f"{field_name} must be a boolean.")
        object.__setattr__(self, "note", _optional_text(self.note, limit=400))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "setting_id": self.setting_id,
            "label": self.label,
            "level": self.level,
            "existed_before": self.existed_before,
            "reversible": self.reversible,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ProgressiveDisclosure:
    """Every setting, grouped by level, with the hidden count stated in the open."""

    settings: tuple[SettingEntry, ...]
    default_level: str = "simple"

    def __post_init__(self) -> None:
        entries = tuple(self.settings)
        if not entries:
            raise DisclosureRefused("a disclosure with no settings describes a product with no settings")
        seen = [entry.setting_id for entry in entries]
        if len(set(seen)) != len(seen):
            raise DisclosureRefused("a setting is listed twice.")
        for entry in entries:
            if not isinstance(entry, SettingEntry):
                raise DisclosureRefused("settings must hold SettingEntry values.")
        object.__setattr__(self, "settings", entries)
        if self.default_level not in DISCLOSURE_LEVELS:
            raise DisclosureRefused(f"unknown default level {self.default_level!r}.")

    def visible_at(self, level: str) -> tuple[SettingEntry, ...]:
        """Settings a reader at ``level`` can see: their level and everything below it."""
        if level not in DISCLOSURE_LEVELS:
            raise DisclosureRefused(f"unknown disclosure level {level!r}.")
        upto = DISCLOSURE_LEVELS.index(level)
        return tuple(entry for entry in self.settings if DISCLOSURE_LEVELS.index(entry.level) <= upto)

    @property
    def hidden_by_default(self) -> tuple[SettingEntry, ...]:
        visible = {entry.setting_id for entry in self.visible_at(self.default_level)}
        return tuple(entry for entry in self.settings if entry.setting_id not in visible)

    def unreachable(self) -> tuple[str, ...]:
        """Settings no level reveals. Should always be empty; a test asserts it stays that way."""
        reachable = {entry.setting_id for entry in self.visible_at(DISCLOSURE_LEVELS[-1])}
        return tuple(entry.setting_id for entry in self.settings if entry.setting_id not in reachable)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "default_level": self.default_level,
            "levels": list(DISCLOSURE_LEVELS),
            "settings": [entry.to_mapping() for entry in self.settings],
            "hidden_by_default_count": len(self.hidden_by_default),
            "hidden_by_default": [entry.setting_id for entry in self.hidden_by_default],
            "unreachable": list(self.unreachable()),
            "settings_removed": 0,
        }


def build_disclosure(settings: Iterable[SettingEntry], *, default_level: str = "simple") -> ProgressiveDisclosure:
    return ProgressiveDisclosure(settings=tuple(settings), default_level=default_level)


# ---------------------------------------------------------------------------
# AC5/AC6 — costs are stated; help describes the build, and acceptance is not authority.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HelpEntry:
    """One help topic, tied to a capability the inventory actually reports.

    ``describes_state`` must be a state the capability is really in: help that describes a capability as
    available while the inventory says it is unavailable is the specific lie this field prevents.
    ``accepts_artifacts`` and ``grants_external_authority`` are separate booleans with the second fixed
    to ``False`` — accepting a draft is never permission to publish it.
    """

    help_id: str
    title: str
    capability_id: str
    describes_state: str
    accepts_artifacts: bool = False
    grants_external_authority: bool = False
    example: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "help_id", _identifier(self.help_id, "help_id"))
        object.__setattr__(self, "title", _text(self.title, "title", limit=200))
        object.__setattr__(self, "capability_id", _identifier(self.capability_id, "capability_id"))
        if self.describes_state not in CAPABILITY_STATES:
            raise HelpRefused(f"unknown capability state {self.describes_state!r} in a help topic.")
        if not isinstance(self.accepts_artifacts, bool):
            raise HelpRefused("accepts_artifacts must be a boolean.")
        if self.grants_external_authority:
            raise HelpRefused(
                "help cannot grant external authority; accepting an artifact and being allowed to publish it "
                "are different decisions, and no help topic may merge them"
            )
        object.__setattr__(self, "example", _optional_text(self.example, limit=1000))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "help_id": self.help_id,
            "title": self.title,
            "capability_id": self.capability_id,
            "describes_state": self.describes_state,
            "accepts_artifacts": self.accepts_artifacts,
            "grants_external_authority": False,
            "example": self.example,
        }


def build_help(entries: Iterable[HelpEntry], statuses: Sequence[CapabilityStatus]) -> tuple[HelpEntry, ...]:
    """Refuse help that describes a capability in a state the inventory disagrees with."""
    known = {status.capability_id: status for status in statuses}
    checked: list[HelpEntry] = []
    for entry in entries:
        if not isinstance(entry, HelpEntry):
            raise HelpRefused("help entries must be HelpEntry values.")
        status = known.get(entry.capability_id)
        if status is None:
            raise HelpRefused(
                f"help topic {entry.help_id!r} describes {entry.capability_id!r}, which this build does not report; "
                "help may not describe capabilities the inventory does not have"
            )
        if entry.describes_state != status.state and not _compatible_states(entry.describes_state, status.state):
            raise HelpRefused(
                f"help topic {entry.help_id!r} calls {entry.capability_id!r} {entry.describes_state!r}, but the "
                f"inventory reports {status.state!r}"
            )
        checked.append(entry)
    return tuple(checked)


def _compatible_states(described: str, actual: str) -> bool:
    """A help topic may be honest about a *worse* state than the inventory, never a better one."""
    try:
        return STATE_PRECEDENCE.index(described) <= STATE_PRECEDENCE.index(actual)
    except ValueError:  # pragma: no cover - both are validated elsewhere
        return False


@dataclass(frozen=True, slots=True)
class OperationsReport:
    """Everything a reader needs in one value: states, readiness, disclosure, grants, help, costs."""

    statuses: tuple[CapabilityStatus, ...]
    readiness: ReadinessReport
    disclosure: ProgressiveDisclosure
    grants: tuple[GrantView, ...]
    help_topics: tuple[HelpEntry, ...]
    limits: tuple[str, ...] = ()
    store: PreferenceStore = field(default_factory=PreferenceStore)

    def __post_init__(self) -> None:
        if not isinstance(self.readiness, ReadinessReport):
            raise OperationsError("an operations report needs its ReadinessReport.")
        if not isinstance(self.disclosure, ProgressiveDisclosure):
            raise OperationsError("an operations report needs its ProgressiveDisclosure.")
        if not self.statuses:
            raise OperationsError("an operations report with no capabilities says nothing")
        object.__setattr__(self, "limits", tuple(_text(item, "limit", limit=400) for item in self.limits))
        for grant in self.grants:
            if not isinstance(grant, GrantView):
                raise OperationsError("grants must hold GrantView values.")

    @property
    def states_in_use(self) -> tuple[str, ...]:
        return tuple(sorted({status.state for status in self.statuses}, key=STATE_PRECEDENCE.index))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": OS_SCHEMA_VERSION,
            "capabilities": [status.to_mapping() for status in self.statuses],
            "states_in_use": list(self.states_in_use),
            "readiness": self.readiness.to_mapping(),
            "disclosure": self.disclosure.to_mapping(),
            "grants": [grant.to_mapping() for grant in self.grants],
            "help": [topic.to_mapping() for topic in self.help_topics],
            "preferences": {
                "actions": list(self.store.actions_available()),
                "count": len(dict(self.store.preferences)),
                "references": len(dict(self.store.references)),
            },
            "limits": list(self.limits),
            "acceptance_is_not_authority": True,
            "costs_stated_before_running": not self.readiness.costs_hidden,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_mapping()).encode("utf-8")).hexdigest()


def build_report(
    *,
    statuses: Iterable[CapabilityStatus],
    readiness: ReadinessReport,
    disclosure: ProgressiveDisclosure,
    grants: Iterable[GrantView] = (),
    help_entries: Iterable[HelpEntry] = (),
    limits: Iterable[str] = (),
    store: PreferenceStore | None = None,
) -> OperationsReport:
    checked = tuple(statuses)
    if not checked:
        raise OperationsError("an operations report with no capabilities says nothing")
    topics = build_help(help_entries, checked)
    return OperationsReport(
        statuses=checked,
        readiness=readiness,
        disclosure=disclosure,
        grants=tuple(grants),
        help_topics=topics,
        limits=tuple(limits),
        store=store or PreferenceStore(),
    )
