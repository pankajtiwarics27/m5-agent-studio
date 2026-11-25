"""AP-023 — deliver sourced original short-video drafts.

A short video is the easiest artifact in this programme to fake, because the two things that make
it worth watching are invisible in the file that gets delivered: *why this topic* and *why these
words*. A rendered MP4 with a confident voiceover proves only that ffmpeg ran.

This module is the record that has to exist beside the MP4, and it refuses three specific
shortcuts:

1. **A trend nobody measured.** "This topic is trending" is a factual claim about the world at a
   time and in a place. Either a dated, regional, sourced signal supports it, or it is labelled a
   candidate or unknown — and a candidate cannot be narrated as a trend. The failure mode is a
   video that opens with "everyone is talking about…" because the model thought it sounded right.
2. **A script that drifted.** The owner approved an angle, a voice and a visual direction. Every
   storyboard scene has to serve one of them, and a script that quietly changes the angle is
   refused unless the owner approved the change on the record. Drift is not detectable by
   watching the video, because the video is the drifted thing.
3. **Rights nobody checked.** Footage, music, fonts and voices each carry where they came from and
   what may be done with them, reusing AP-021's provenance vocabulary rather than inventing a
   second one. A voice carries one extra question that music does not: *whose voice is this, and
   did they agree?* Unauthorised cloning is not a warning, it is unrecordable.

Two more rules are structural rather than editorial. Before adding a *second* video integration,
the three MoneyPrinterTurbo surfaces that already exist must be dispositioned — reused, or
declared unsuitable with a reason. And a draft is only complete when a probe found a real
playable file with captions that actually line up; upload is a separate grant that this module
cannot produce.

Nothing here reads a clock, opens a file or touches the network. Rendering and probing are
injected through :class:`VideoRenderer` and :class:`PlaybackProbe`, the same port pattern AP-010,
AP-021 and AP-022 use, so the decision logic runs identically against a recording double in CI and
a real ffmpeg pipeline in the world.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Protocol, Sequence, cast

from autonomous_production_research import ClaimRecord, ResearchBundle
from website_draft import AssetProvenance

__all__ = [
    "VideoDraftError",
    "TrendRefused",
    "EvidenceRefused",
    "DirectionRefused",
    "RightsRefused",
    "SecondIntegrationRefused",
    "PlaybackRefused",
    "UploadRefused",
    "TopicSignal",
    "TopicPopularity",
    "assess_topic",
    "contradicted_claim_texts",
    "NarrationClaim",
    "CreativeDirection",
    "StoryboardScene",
    "OwnerDirectionChange",
    "Script",
    "VoiceConsent",
    "MediaAsset",
    "AdapterDisposition",
    "AdapterAudit",
    "EXISTING_MONEYPRINTER_SURFACES",
    "audit_existing_integrations",
    "CaptionCue",
    "PlaybackProbe",
    "VideoRenderer",
    "VideoDraft",
    "UploadGrant",
    "render_video_draft",
    "POPULARITY_STATUSES",
    "MEDIA_ROLES",
    "VOICE_CONSENT_KINDS",
    "MAX_SIGNAL_AGE_DAYS",
]

VD_SCHEMA_VERSION = 1
VD_MAX_TEXT = 2000
VD_MAX_SIGNALS = 24
VD_MAX_SCENES = 60
VD_MAX_CAPTIONS = 400
VD_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,119}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: How an unsupported topic may be described. ``candidate`` is a single dated signal or a set of
#: them that does not meet the support bar; ``unknown`` is no usable signal at all. Both are
#: recordable, and neither may be narrated as a trend.
POPULARITY_STATUSES: tuple[str, ...] = ("supported", "candidate", "unknown")

#: Assets that may appear in a rendered draft. ``voice`` is the one role with an extra question
#: attached, which is why :class:`VoiceConsent` exists rather than a free-text note.
MEDIA_ROLES: tuple[str, ...] = ("footage", "music", "font", "voice", "graphic")

#: How a voice may be used. The unauthorised variants are named so that refusing them is a
#: deliberate line in the code rather than a comment in a review.
VOICE_CONSENT_KINDS: tuple[str, ...] = (
    "self_recorded",
    "licensed_voice",
    "owner_cloned_with_consent",
)

#: The three MoneyPrinterTurbo integration surfaces that already exist in this repository
#: (``moneyprinter_tool.py``, ``moneyprinter_mcp.py`` and the ``MoneyPrinterAPI`` provider
#: profile). Adding a fourth surface is the thing AC4 asks to be justified first.
EXISTING_MONEYPRINTER_SURFACES: tuple[str, ...] = (
    "cli_tool",
    "mcp_stdio_server",
    "openai_gateway_profile",
)

#: A popularity signal older than this cannot support "trending now"; it is downgraded to a
#: candidate rather than being counted as support.
MAX_SIGNAL_AGE_DAYS = 90

#: Substrings that mark a consent kind as unauthorised voice cloning. Kept as a named list so the
#: refusal reads as policy rather than as a typo in an enum.
_UNAUTHORISED_VOICE_MARKERS: tuple[str, ...] = ("clone", "cloned_without", "impersonat", "deepfake")

#: Words that deny the popularity marker that follows them. "not trending yet" is the honest
#: sentence for a candidate, and it must not be refused as if it were the dishonest one.
_NEGATIONS: tuple[str, ...] = ("not ", "isn't", "is not", "never", "no evidence", "cannot say", "unclear if")


class VideoDraftError(ValueError):
    """The video draft is malformed, unsourced, out of rights, or claims something it cannot."""


class TrendRefused(VideoDraftError):
    """A popularity claim rests on no dated, regional, sourced signal."""


class EvidenceRefused(VideoDraftError):
    """A claim has no evidence, or an uncertainty or contradiction was hidden."""


class DirectionRefused(VideoDraftError):
    """The script no longer serves the approved angle, voice or visual direction."""


class RightsRefused(VideoDraftError):
    """An asset's provenance or a voice's consent does not permit its use."""


class SecondIntegrationRefused(VideoDraftError):
    """A new integration was proposed without auditing the ones that already exist."""


class PlaybackRefused(VideoDraftError):
    """The rendered output is not a real playable file, or its captions do not line up."""


class UploadRefused(VideoDraftError):
    """Something tried to record an upload, publication or channel grant."""


def _is_negated(line: str, index: int) -> bool:
    """Whether a marker at ``index`` is being denied rather than asserted.

    Only the clause the marker sits in counts. "It is not hype: this topic is trending" denies
    *hype*, not the trend, and a guardrail that reads across a colon would accept the second claim
    because of the first clause's honesty. That is the difference between a check and a spell.
    """
    window = line[max(0, index - 60):index]
    for separator in (".", ";", ":"):
        window = window.rsplit(separator, 1)[-1]
    return any(negation in window for negation in _NEGATIONS)


def _vd_text(value: object, field_name: str, limit: int = VD_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise VideoDraftError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise VideoDraftError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise VideoDraftError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise VideoDraftError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _vd_optional_text(value: object, field_name: str, limit: int = VD_MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if "\x00" in text:
        raise VideoDraftError(f"{field_name} must not contain NUL characters.")
    if len(text) > limit:
        raise VideoDraftError(f"{field_name} must be at most {limit} characters.")
    return text


def _vd_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise VideoDraftError(
            f"{field_name} must be a stable identifier (letters, digits, space, . _ : / -)."
        )
    return value.strip()


def _vd_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise VideoDraftError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _vd_date(value: object, field_name: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise VideoDraftError(f"{field_name} must be an ISO date (YYYY-MM-DD).")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise VideoDraftError(f"{field_name} must be a real calendar date.") from exc


# ---------------------------------------------------------------------------
# AC1 — topic popularity is dated, regional and sourced, or it is labelled.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopicSignal:
    """One observation of one source about how popular a topic was, in a place, at a time.

    A signal is deliberately narrower than a claim: it names the resolved source id from the
    research bundle, the calendar date it describes, the region it describes and the metric it
    measured. A number without a date and a place is a vibe, and a vibe is how "trending" gets
    invented.
    """

    source_id: str
    as_of: str
    region: str
    metric: str
    value: float
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _vd_id(self.source_id, "signal source_id"))
        _vd_date(self.as_of, "signal as_of")
        object.__setattr__(self, "as_of", self.as_of.strip())
        region = _vd_text(self.region, "signal region", 40)
        if region.lower() in {"world", "everywhere", "any", "*"}:
            raise TrendRefused(
                f"signal region {region!r} is not a region; name the market the number describes "
                "('global' is acceptable, 'everywhere' is not a measurement)."
            )
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "metric", _vd_text(self.metric, "signal metric", 80))
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise TrendRefused("signal value must be a number; a trend claimed without a number is an opinion.")
        if self.value < 0:
            raise TrendRefused("signal value must not be negative.")
        object.__setattr__(self, "note", _vd_optional_text(self.note, "signal note", 400))

    def describes(self, *, region: str, on: date, max_age_days: int) -> bool:
        """Whether this signal is about the same market and still recent enough to count."""
        same_region = self.region.lower() in {region.lower(), "global"}
        age = (on - _vd_date(self.as_of, "signal as_of")).days
        return same_region and 0 <= age <= max_age_days


@dataclass(frozen=True, slots=True)
class TopicPopularity:
    """What the run can honestly say about a topic's popularity, and why.

    Built only by :func:`assess_topic`, so the status and the signals behind it cannot be paired
    up arbitrarily by a caller. ``status`` is derived: ``supported`` needs two or more distinct
    in-region, in-window, bundle-resident sources. Everything else is ``candidate`` or
    ``unknown`` and carries the reason.
    """

    topic: str
    status: str
    signals: tuple[TopicSignal, ...]
    as_of: str
    region: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic", _vd_text(self.topic, "topic", 300))
        if self.status not in POPULARITY_STATUSES:
            raise TrendRefused(
                f"popularity status {self.status!r} is unknown; expected one of {list(POPULARITY_STATUSES)}."
            )
        _vd_date(self.as_of, "popularity as_of")
        object.__setattr__(self, "region", _vd_text(self.region, "popularity region", 40))
        if not isinstance(self.signals, tuple):
            raise TrendRefused("signals must be a tuple of TopicSignal records.")
        for signal in self.signals:
            if not isinstance(signal, TopicSignal):
                raise TrendRefused("signals must be TopicSignal records.")
        if len(self.signals) > VD_MAX_SIGNALS:
            raise TrendRefused(f"at most {VD_MAX_SIGNALS} signals may support one topic.")
        if self.status != "supported" and not self.reason.strip():
            raise TrendRefused(
                f"a {self.status} topic must say why it is not supported; an unexplained downgrade "
                "is indistinguishable from an oversight."
            )

    @property
    def may_be_narrated_as_trending(self) -> bool:
        return self.status == "supported"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "status": self.status,
            "as_of": self.as_of,
            "region": self.region,
            "reason": self.reason,
            "signals": [
                {
                    "source_id": signal.source_id,
                    "as_of": signal.as_of,
                    "region": signal.region,
                    "metric": signal.metric,
                    "value": signal.value,
                    "note": signal.note,
                }
                for signal in self.signals
            ],
        }


def assess_topic(
    topic: str,
    signals: Sequence[TopicSignal],
    *,
    bundle: ResearchBundle,
    region: str,
    as_of: str,
    max_age_days: int = MAX_SIGNAL_AGE_DAYS,
) -> TopicPopularity:
    """Derive the honest popularity status for a topic from dated, sourced signals.

    Three things are refused rather than downgraded, because each makes the number meaningless
    rather than merely weak: a signal whose source is not in the research bundle (an uncited
    trend), a signal dated in the future relative to the assessment (a clock or a source is
    wrong), and a signal for a different region counted as if it described this one.
    """
    topic_text = _vd_text(topic, "topic", 300)
    region_text = _vd_text(region, "region", 40)
    on = _vd_date(as_of, "as_of")
    if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or max_age_days < 1:
        raise TrendRefused("max_age_days must be a positive integer.")

    seen: list[TopicSignal] = []
    for raw in signals or ():
        if not isinstance(raw, TopicSignal):
            raise TrendRefused("signals must be TopicSignal records.")
        if bundle.source_by_id(raw.source_id) is None:
            raise TrendRefused(
                f"signal from {raw.source_id!r} is not in the research bundle; a popularity claim "
                "has to rest on a source the run actually retrieved."
            )
        signal_date = _vd_date(raw.as_of, "signal as_of")
        if signal_date > on:
            raise TrendRefused(
                f"signal from {raw.source_id!r} is dated {raw.as_of}, after the assessment date "
                f"{as_of}; a future signal means the clock or the source is wrong."
            )
        if raw.region.lower() not in {region_text.lower(), "global"}:
            raise TrendRefused(
                f"signal from {raw.source_id!r} describes {raw.region!r}, not {region_text!r}; "
                "popularity in another market is not popularity here."
            )
        if raw not in seen:
            seen.append(raw)

    in_window = [s for s in seen if s.describes(region=region_text, on=on, max_age_days=max_age_days)]
    distinct = {s.source_id for s in in_window}
    if len(distinct) >= 2:
        status = "supported"
        reason = f"{len(distinct)} independent sources dated within {max_age_days} days in {region_text}."
    elif len(distinct) == 1:
        status = "candidate"
        reason = (
            "one dated source is a candidate, not support: a single observation cannot separate a "
            "trend from that source's own interest."
        )
    else:
        status = "unknown"
        reason = (
            f"no signal inside the {max_age_days}-day window for {region_text}; the draft may "
            "proceed, and it may not call the topic trending."
        )
    return TopicPopularity(
        topic=topic_text,
        status=status,
        signals=tuple(in_window),
        as_of=as_of.strip(),
        region=region_text,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# AC2 — evidence links, and contradictions stay visible.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NarrationClaim:
    """One thing the narration asserts, and the evidence behind it.

    ``important`` is the caller's own judgement, and it is a commitment: an important claim with
    no supporting source is refused at construction, so the run cannot discover the gap while
    rendering. ``uncertainty`` is where a contested claim says so out loud rather than being
    silently smoothed into confident narration.
    """

    text: str
    source_ids: tuple[str, ...] = ()
    important: bool = False
    uncertainty: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _vd_text(self.text, "claim text", 600))
        if not isinstance(self.important, bool):
            raise EvidenceRefused("important must be a boolean.")
        ids: list[str] = []
        for raw in self.source_ids:
            text = _vd_id(raw, "claim source_id")
            if text not in ids:
                ids.append(text)
        object.__setattr__(self, "source_ids", tuple(ids))
        object.__setattr__(self, "uncertainty", _vd_optional_text(self.uncertainty, "claim uncertainty", 600))
        if self.important and not ids:
            raise EvidenceRefused(
                f"important claim {self.text[:60]!r} names no source; 'important' means the video "
                "asserts something consequential, and a consequential assertion needs evidence."
            )

    def is_supported(self, bundle: ResearchBundle) -> bool:
        return any(bundle.source_by_id(sid) is not None for sid in self.source_ids)


def contradicted_claim_texts(bundle: ResearchBundle) -> tuple[str, ...]:
    """The claim texts AP-009's research recorded as contradicted.

    Reading them off the bundle rather than asking the caller to remember is the same rule AP-022
    applies to scanning: a fact the record depends on is collected by the module, not trusted to
    whoever assembled the call.
    """
    if not isinstance(bundle, ResearchBundle):
        raise EvidenceRefused("bundle must be an autonomous_production_research.ResearchBundle.")
    seen: list[str] = []
    for claim in bundle.claims:
        if not isinstance(claim, ClaimRecord):
            raise EvidenceRefused("bundle claims must be ClaimRecord records.")
        if claim.status == "contradicted" and claim.text not in seen:
            seen.append(claim.text)
    return tuple(seen)


# ---------------------------------------------------------------------------
# AC3 — the script and storyboard preserve the approved direction.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreativeDirection:
    """The angle, voice and visual direction the owner approved before production.

    ``forbidden_moves`` is required and non-empty for the same reason AP-021 requires an
    out-of-scope list: a brief that forbids nothing has not been narrowed, and an unnarrowed brief
    is exactly what a script drifts away from.
    """

    angle: str
    voice: str
    visual_direction: str
    required_elements: tuple[str, ...]
    forbidden_moves: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "angle", _vd_text(self.angle, "angle", 600))
        object.__setattr__(self, "voice", _vd_text(self.voice, "voice", 600))
        object.__setattr__(self, "visual_direction", _vd_text(self.visual_direction, "visual_direction", 600))
        object.__setattr__(self, "required_elements", self._texts(self.required_elements, "required_elements"))
        object.__setattr__(self, "forbidden_moves", self._texts(self.forbidden_moves, "forbidden_moves"))
        if not self.required_elements:
            raise DirectionRefused(
                "required_elements must name at least one thing the video has to contain; a "
                "storyboard cannot be judged against an empty list."
            )
        if not self.forbidden_moves:
            raise DirectionRefused(
                "forbidden_moves must name at least one thing the video must not do; a direction "
                "that forbids nothing is not a direction."
            )

    @staticmethod
    def _texts(values: Iterable[object], field_name: str) -> tuple[str, ...]:
        out: list[str] = []
        for raw in values or ():
            text = _vd_text(raw, field_name, 400)
            if text not in out:
                out.append(text)
        return tuple(out)

    def element_names(self) -> tuple[str, ...]:
        """Every part of the direction a scene may claim to serve."""
        return ("angle", "voice", "visual_direction", *self.required_elements)

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "angle": self.angle,
                "voice": self.voice,
                "visual_direction": self.visual_direction,
                "required_elements": list(self.required_elements),
                "forbidden_moves": list(self.forbidden_moves),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoryboardScene:
    """One shot: how long it runs, which part of the direction it serves, and what it claims."""

    scene_id: str
    seconds: float
    purpose: str
    direction_element: str
    claims: tuple[NarrationClaim, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scene_id", _vd_id(self.scene_id, "scene_id"))
        if not isinstance(self.seconds, (int, float)) or isinstance(self.seconds, bool):
            raise VideoDraftError("scene seconds must be a number.")
        if self.seconds <= 0 or self.seconds > 120:
            raise VideoDraftError("scene seconds must be greater than 0 and at most 120.")
        object.__setattr__(self, "purpose", _vd_text(self.purpose, "scene purpose", 400))
        object.__setattr__(self, "direction_element", _vd_text(self.direction_element, "direction_element", 200))
        claims: list[NarrationClaim] = []
        for raw in self.claims:
            if not isinstance(raw, NarrationClaim):
                raise EvidenceRefused("scene claims must be NarrationClaim records.")
            claims.append(raw)
        object.__setattr__(self, "claims", tuple(claims))


@dataclass(frozen=True, slots=True)
class OwnerDirectionChange:
    """A change to the approved direction, with a named person behind it and a reason.

    Machines cannot create one of these; the actor must be namespaced the same way AP-020's and
    AP-022's approvals are, so a direction change is attributable or it does not exist.
    """

    actor: str
    reason: str
    approved_at: str

    def __post_init__(self) -> None:
        actor = _vd_text(self.actor, "actor", 200)
        if actor.split(":", 1)[0].strip().lower() not in {"owner", "user", "reviewer"}:
            raise DirectionRefused(
                f"direction change actor {actor!r} is not a person; use owner:, user: or reviewer:."
            )
        if not actor.split(":", 1)[1].strip():
            raise DirectionRefused("direction change actor must name who, not just the role prefix.")
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "reason", _vd_text(self.reason, "reason", 600))
        _vd_date(self.approved_at, "approved_at")
        object.__setattr__(self, "approved_at", self.approved_at.strip())


@dataclass(frozen=True, slots=True)
class Script:
    """Narration, storyboard and the direction they were built against.

    ``uncertainties`` names the claim texts the narration does not resolve. It exists so a
    contradiction found during research has somewhere to stay visible instead of being averaged
    into confident copy.
    """

    direction: CreativeDirection
    narration: tuple[str, ...]
    scenes: tuple[StoryboardScene, ...]
    uncertainties: tuple[str, ...] = ()
    direction_change: OwnerDirectionChange | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.direction, CreativeDirection):
            raise DirectionRefused("a script needs the CreativeDirection it was written against.")
        lines: list[str] = []
        for raw in self.narration:
            lines.append(_vd_text(raw, "narration line", 1000))
        if not lines:
            raise VideoDraftError("a script needs at least one narration line.")
        object.__setattr__(self, "narration", tuple(lines))
        scenes: list[StoryboardScene] = []
        for candidate_scene in self.scenes:
            if not isinstance(candidate_scene, StoryboardScene):
                raise DirectionRefused("scenes must be StoryboardScene records.")
            scenes.append(candidate_scene)
        if not scenes:
            raise DirectionRefused("a script needs at least one storyboard scene.")
        if len(scenes) > VD_MAX_SCENES:
            raise VideoDraftError(f"a script may have at most {VD_MAX_SCENES} scenes.")
        ids = [scene.scene_id for scene in scenes]
        if len(ids) != len(set(ids)):
            raise VideoDraftError("scene ids must be unique; two scenes with one id cannot be cut.")
        allowed = set(self.direction.element_names())
        for scene in scenes:
            if scene.direction_element not in allowed:
                raise DirectionRefused(
                    f"scene {scene.scene_id!r} serves {scene.direction_element!r}, which is not part "
                    f"of the approved direction {sorted(allowed)}; a scene that serves nothing "
                    "approved is drift with a storyboard attached."
                )
        object.__setattr__(self, "scenes", tuple(scenes))
        uncertainties = [u.strip() for u in self.uncertainties if str(u).strip()]
        object.__setattr__(self, "uncertainties", tuple(uncertainties))
        if self.direction_change is not None and not isinstance(self.direction_change, OwnerDirectionChange):
            raise DirectionRefused("direction_change must be an OwnerDirectionChange or None.")

    @property
    def duration_seconds(self) -> float:
        return round(sum(scene.seconds for scene in self.scenes), 3)

    def uncovered_elements(self) -> tuple[str, ...]:
        """Direction elements no scene serves. Empty means the storyboard covers the brief."""
        served = {scene.direction_element for scene in self.scenes}
        return tuple(name for name in self.direction.element_names() if name not in served)

    def claims(self) -> tuple[NarrationClaim, ...]:
        return tuple(claim for scene in self.scenes for claim in scene.claims)


# ---------------------------------------------------------------------------
# AC5 — rights and provenance for every asset, and voices specifically.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceConsent:
    """Whose voice this is and how it may be used.

    ``unauthorised_clone`` is refused at construction rather than flagged, because a cloned voice
    without consent is not a defect to be triaged later — it is not a thing this programme may
    produce at all. Nothing here can be re-read into permission: this record grants none.
    """

    kind: str
    subject: str
    evidence: str = ""

    def __post_init__(self) -> None:
        kind = _vd_text(self.kind, "consent kind", 60)
        if kind not in VOICE_CONSENT_KINDS:
            lowered = kind.lower()
            if any(marker in lowered for marker in _UNAUTHORISED_VOICE_MARKERS):
                raise RightsRefused(
                    f"voice consent {kind!r} describes cloning without consent; that is not a state "
                    "this programme records, it is one it refuses to produce."
                )
            raise RightsRefused(
                f"voice consent {kind!r} is unknown; expected one of {list(VOICE_CONSENT_KINDS)}."
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "subject", _vd_text(self.subject, "consent subject", 200))
        evidence = _vd_optional_text(self.evidence, "consent evidence", 400)
        object.__setattr__(self, "evidence", evidence)
        if kind == "owner_cloned_with_consent" and not evidence:
            raise RightsRefused(
                "cloned-with-consent needs the consent record named in evidence; the word 'consent' "
                "in a field is not a consent."
            )


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """One footage, music, font, voice or graphic asset in the rendered draft.

    Provenance is AP-021's :class:`AssetProvenance`, composed rather than re-implemented, so the
    rights vocabulary and its refusals are shared across media and websites. This class adds the
    role and the one question that only a voice has.
    """

    asset: AssetProvenance
    role: str
    voice: VoiceConsent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.asset, AssetProvenance):
            raise RightsRefused("a media asset wraps an AssetProvenance; the source is not optional.")
        role = _vd_text(self.role, "asset role", 40)
        if role not in MEDIA_ROLES:
            raise RightsRefused(f"asset role {role!r} is unknown; expected one of {list(MEDIA_ROLES)}")
        object.__setattr__(self, "role", role)
        if role == "voice":
            if not isinstance(self.voice, VoiceConsent):
                raise RightsRefused(
                    f"voice asset {self.asset.path!r} names no consent; whose voice it is and who "
                    "agreed is the whole question a voice poses."
                )
        elif self.voice is not None:
            raise RightsRefused(
                f"asset {self.asset.path!r} is {role} but carries voice consent; mixing the two "
                "records makes neither auditable."
            )

    def blocks_delivery(self) -> bool:
        return self.asset.blocks_delivery()


# ---------------------------------------------------------------------------
# AC4 — audit the integrations that exist before adding another one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterDisposition:
    """What the run did about one existing MoneyPrinterTurbo surface."""

    surface: str
    disposition: str
    reason: str = ""

    def __post_init__(self) -> None:
        surface = _vd_text(self.surface, "surface", 80)
        if surface not in EXISTING_MONEYPRINTER_SURFACES:
            raise SecondIntegrationRefused(
                f"disposition for {surface!r} is not an existing surface "
                f"{list(EXISTING_MONEYPRINTER_SURFACES)}; the audit is about what already exists."
            )
        object.__setattr__(self, "surface", surface)
        disposition = _vd_text(self.disposition, "disposition", 40)
        if disposition not in {"reused", "unsuitable"}:
            raise SecondIntegrationRefused(
                f"disposition {disposition!r} is unknown; a surface is either reused or declared "
                "unsuitable with a reason."
            )
        object.__setattr__(self, "disposition", disposition)
        reason = _vd_optional_text(self.reason, "disposition reason", 600)
        if disposition == "unsuitable" and not reason:
            raise SecondIntegrationRefused(
                f"{surface} was declared unsuitable with no reason; 'we did not look' and 'it cannot "
                "do this' are different findings and only one of them justifies new code."
            )
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True)
class AdapterAudit:
    """The record AC4 asks for: what already existed, what it can do, and what happens next.

    ``adds_second_integration`` is derived. A new adapter is permitted only when every existing
    surface has been dispositioned and none of them is reused — otherwise the honest move is to
    extend the surface that already works.
    """

    candidate: str
    candidate_kind: str
    dispositions: tuple[AdapterDisposition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", _vd_text(self.candidate, "candidate", 120))
        object.__setattr__(self, "candidate_kind", _vd_text(self.candidate_kind, "candidate_kind", 80))
        seen: dict[str, AdapterDisposition] = {}
        for raw in self.dispositions:
            if not isinstance(raw, AdapterDisposition):
                raise SecondIntegrationRefused("dispositions must be AdapterDisposition records.")
            if raw.surface in seen:
                raise SecondIntegrationRefused(
                    f"{raw.surface} is dispositioned twice; one surface, one answer."
                )
            seen[raw.surface] = raw
        missing = [s for s in EXISTING_MONEYPRINTER_SURFACES if s not in seen]
        if missing:
            raise SecondIntegrationRefused(
                f"the audit skipped {missing}; a second integration cannot be justified by not "
                "looking at the first one."
            )
        object.__setattr__(self, "dispositions", tuple(seen[s] for s in EXISTING_MONEYPRINTER_SURFACES))

    @property
    def reused(self) -> tuple[str, ...]:
        return tuple(d.surface for d in self.dispositions if d.disposition == "reused")

    @property
    def adds_second_integration(self) -> bool:
        if self.candidate in EXISTING_MONEYPRINTER_SURFACES:
            return False
        return not self.reused

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "candidate_kind": self.candidate_kind,
            "adds_second_integration": self.adds_second_integration,
            "reused": list(self.reused),
            "dispositions": [
                {"surface": d.surface, "disposition": d.disposition, "reason": d.reason}
                for d in self.dispositions
            ],
        }


def audit_existing_integrations(
    dispositions: Sequence[AdapterDisposition],
    *,
    candidate: str,
    candidate_kind: str,
) -> AdapterAudit:
    """Build the audit, refusing a second integration that reuses an existing surface.

    The refusal is the point of the story's fourth criterion. Writing a fourth MoneyPrinterTurbo
    client while a working CLI contract sits in the repository is not extra capability, it is a
    second source of truth about the same external tool.
    """
    audit = AdapterAudit(
        candidate=candidate,
        candidate_kind=candidate_kind,
        dispositions=tuple(dispositions),
    )
    if audit.candidate not in EXISTING_MONEYPRINTER_SURFACES and audit.reused:
        raise SecondIntegrationRefused(
            f"candidate {audit.candidate!r} would be a second integration while "
            f"{list(audit.reused)} is reused; extend the surface that already works instead."
        )
    return audit


# ---------------------------------------------------------------------------
# AC6 — the rendered draft is playable, captioned, and not uploaded.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """One subtitle cue. Timing has to line up with the video, not with the paragraph."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 0:
            raise PlaybackRefused("caption index must be a non-negative integer.")
        for attr in ("start_seconds", "end_seconds"):
            value = getattr(self, attr)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise PlaybackRefused(f"caption {attr} must be a non-negative number.")
        if self.end_seconds <= self.start_seconds:
            raise PlaybackRefused("a caption must end after it starts.")
        object.__setattr__(self, "text", _vd_text(self.text, "caption text", 500))


class PlaybackProbe(Protocol):
    """The execution this module refuses to perform itself.

    A real implementation runs ffprobe against the rendered file and returns container, duration,
    stream and caption facts. The module never opens a file, so the same rules run against a
    recording double in CI.
    """

    def inspect(self, path: str, sha256: str) -> Mapping[str, Any]:
        """Return the observed facts about the rendered file at ``path``."""
        ...


class VideoRenderer(Protocol):
    """The renderer this module drives, and does not contain."""

    def render(self, spec: Mapping[str, Any]) -> Mapping[str, Any]:
        """Render ``spec`` and return the path, digest and caption payload."""
        ...


@dataclass(frozen=True, slots=True)
class UploadGrant:
    """Upload is a separate grant, and this record can only ever hold ``False``.

    There is no constructor argument and no mapping that makes it true, exactly like AP-021's
    :class:`~website_draft.DeploymentGrant`. A draft workflow has no code path that publishes, so
    the honest representation is a constant.
    """

    upload: bool = field(default=False, init=False)
    publication: bool = field(default=False, init=False)
    channel_credentials: bool = field(default=False, init=False)

    @property
    def granted(self) -> bool:
        return self.upload or self.publication or self.channel_credentials

    def refuse(self, action: str) -> None:
        raise UploadRefused(
            f"{action!r} is not part of a video draft: upload, publication and channel credentials "
            "are separate grants (AP-029), and nothing here can provide them."
        )


@dataclass(frozen=True, slots=True)
class VideoDraft:
    """A rendered short-video draft, everything behind it, and the reasons it may not ship.

    ``readiness()`` is the derived verdict and ``deliverable()`` is its complement. Neither can be
    assigned, so a run cannot decide it is finished: the reasons list is computed from the topic,
    the claims, the direction, the assets, the audit and the playback facts.
    """

    topic: TopicPopularity
    direction: CreativeDirection
    script: Script
    assets: tuple[MediaAsset, ...]
    audit: AdapterAudit
    path: str
    artifact_sha256: str
    duration_seconds: float
    captions: tuple[CaptionCue, ...]
    playable: bool
    probe_detail: str = ""
    runs_evaluated: int = 1
    intervention_minutes: float = 0.0
    limitations: tuple[str, ...] = ()
    observed_contradictions: tuple[str, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.topic, TopicPopularity):
            raise TrendRefused("a draft needs a TopicPopularity; the trend question is not optional.")
        if not isinstance(self.direction, CreativeDirection):
            raise DirectionRefused("a draft needs the approved CreativeDirection.")
        if not isinstance(self.script, Script):
            raise DirectionRefused("a draft needs a Script.")
        object.__setattr__(self, "path", _vd_text(self.path, "path", 1000))
        object.__setattr__(self, "artifact_sha256", _vd_digest(self.artifact_sha256, "artifact_sha256"))
        if not isinstance(self.duration_seconds, (int, float)) or isinstance(self.duration_seconds, bool):
            raise PlaybackRefused("duration_seconds must be a number.")
        if self.duration_seconds <= 0:
            raise PlaybackRefused("duration_seconds must be positive; a zero-length draft is not a draft.")
        if not isinstance(self.playable, bool):
            raise PlaybackRefused("playable must be a boolean; 'probably plays' is not one.")
        object.__setattr__(self, "probe_detail", _vd_optional_text(self.probe_detail, "probe detail", 600))
        if not isinstance(self.runs_evaluated, int) or isinstance(self.runs_evaluated, bool) or self.runs_evaluated < 1:
            raise VideoDraftError("runs_evaluated must be a positive integer; every run is reported.")
        if not isinstance(self.intervention_minutes, (int, float)) or isinstance(self.intervention_minutes, bool):
            raise VideoDraftError("intervention_minutes must be a number.")
        if self.intervention_minutes < 0:
            raise VideoDraftError("intervention_minutes must not be negative.")
        object.__setattr__(self, "limitations", tuple(str(x).strip() for x in self.limitations if str(x).strip()))
        object.__setattr__(
            self,
            "observed_contradictions",
            tuple(str(x).strip() for x in self.observed_contradictions if str(x).strip()),
        )
        assets: list[MediaAsset] = []
        for raw in self.assets:
            if not isinstance(raw, MediaAsset):
                raise RightsRefused("assets must be MediaAsset records.")
            assets.append(raw)
        paths = [asset.asset.path for asset in assets]
        if len(paths) != len(set(paths)):
            raise RightsRefused("one asset path, one record; a duplicated asset hides which rights apply.")
        object.__setattr__(self, "assets", tuple(assets))
        cues: list[CaptionCue] = []
        previous_end = -1.0
        for candidate_cue in self.captions:
            if not isinstance(candidate_cue, CaptionCue):
                raise PlaybackRefused("captions must be CaptionCue records.")
            if candidate_cue.start_seconds < previous_end:
                raise PlaybackRefused(
                    f"caption {candidate_cue.index} starts before the previous cue ends; overlapping "
                    "cues are unreadable and mean the timing was guessed."
                )
            previous_end = candidate_cue.end_seconds
            cues.append(candidate_cue)
        if len(cues) > VD_MAX_CAPTIONS:
            raise PlaybackRefused(f"at most {VD_MAX_CAPTIONS} caption cues are supported.")
        for cue in cues:
            if cue.end_seconds > self.duration_seconds + 0.5:
                raise PlaybackRefused(
                    f"caption {cue.index} ends at {cue.end_seconds}s, after the {self.duration_seconds}s "
                    "video; a cue nobody can see is not a caption."
                )
        object.__setattr__(self, "captions", tuple(cues))
        # Direction drift and hidden uncertainty are checked at construction: both are facts about
        # the draft, not findings that appear later.
        if self.script.direction.fingerprint() != self.direction.fingerprint():
            if self.script.direction_change is None:
                raise DirectionRefused(
                    "the script was written against a different direction than the approved one, and "
                    "no owner change is recorded; drifting from the brief is not a revision."
                )
        self._refuse_popularity_overreach()
        hidden = self.hidden_contradictions()
        if hidden:
            raise EvidenceRefused(
                f"research contradicts {list(hidden)[:2]}; the script states no uncertainty about it. "
                "A contradiction that disappears between research and narration was not resolved, it "
                "was deleted."
            )

    def _refuse_popularity_overreach(self) -> None:
        """A popularity claim in the narration needs a supported status behind it.

        The scan is negation-aware on purpose. "This topic is not trending yet" is the *honest*
        sentence for a candidate, and a plain substring check would refuse it -- punishing the exact
        narration this rule exists to encourage, which is how a guardrail teaches a model to phrase
        around it instead of complying.
        """
        if self.topic.may_be_narrated_as_trending:
            return
        markers = ("trending", "everyone is talking", "going viral", "massive demand", "exploding")
        for line in self.script.narration:
            lowered = line.lower()
            for marker in markers:
                for match in re.finditer(re.escape(marker), lowered):
                    if _is_negated(lowered, match.start()):
                        continue
                    raise TrendRefused(
                        f"the narration calls the topic {marker!r} while the popularity status is "
                        f"{self.topic.status!r} ({self.topic.reason}). A labelled candidate cannot be "
                        "narrated as a trend."
                    )

    # -- derived verdict -----------------------------------------------------------------

    def evidence_gaps(self) -> tuple[str, ...]:
        """Important claims with no evidence behind them."""
        gaps: list[str] = []
        for claim in self.script.claims():
            if claim.important and not claim.source_ids:
                gaps.append(claim.text)
        return tuple(gaps)

    def rights_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        for asset in self.assets:
            if asset.role == "voice":
                if asset.voice is None or asset.voice.kind not in VOICE_CONSENT_KINDS:
                    gaps.append(f"{asset.asset.path}: voice consent missing")
            if asset.blocks_delivery():
                gaps.append(f"{asset.asset.path}: rights unknown ({asset.asset.license_id})")
            if asset.role == "font" and asset.asset.basis != "authored_in_run" and not asset.asset.license_id:
                gaps.append(f"{asset.asset.path}: font used with no licence named")
        return tuple(gaps)

    def caption_gaps(self) -> tuple[str, ...]:
        if not self.captions:
            return ("no captions: a draft without captions is not the package the story promises",)
        return ()

    def readiness(self) -> tuple[str, ...]:
        """Every reason this draft may not be delivered, in the order a reviewer should fix them."""
        reasons: list[str] = list(self.evidence_gaps())
        reasons.extend(self.rights_gaps())
        reasons.extend(self.caption_gaps())
        if not self.playable:
            reasons.append(f"the rendered file did not probe as playable ({self.probe_detail or 'no detail'})")
        uncovered = self.script.uncovered_elements()
        if uncovered:
            reasons.append(f"the storyboard never serves {list(uncovered)}")
        for text in self.hidden_contradictions():
            reasons.append(
                f"the research contradicts {text[:80]!r} and the script states no uncertainty"
            )
        return tuple(reasons)

    def hidden_contradictions(self) -> tuple[str, ...]:
        """Contradictions the research found that the script never states out loud."""
        stated = " ".join(self.script.uncertainties).lower()
        hidden: list[str] = []
        for text in self.observed_contradictions:
            first = text.strip().lower()[:60]
            if first and first not in stated:
                hidden.append(text)
        return tuple(hidden)

    def deliverable(self) -> bool:
        return not self.readiness()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": VD_SCHEMA_VERSION,
            "run_id": self.run_id,
            "topic": self.topic.to_mapping(),
            "direction": {
                "angle": self.direction.angle,
                "voice": self.direction.voice,
                "visual_direction": self.direction.visual_direction,
                "required_elements": list(self.direction.required_elements),
                "forbidden_moves": list(self.direction.forbidden_moves),
            },
            "script": {
                "narration": list(self.script.narration),
                "scenes": [
                    {
                        "scene_id": scene.scene_id,
                        "seconds": scene.seconds,
                        "purpose": scene.purpose,
                        "direction_element": scene.direction_element,
                    }
                    for scene in self.script.scenes
                ],
                "uncertainties": list(self.script.uncertainties),
                "duration_seconds": self.script.duration_seconds,
                "direction_change": (
                    {
                        "actor": self.script.direction_change.actor,
                        "reason": self.script.direction_change.reason,
                        "approved_at": self.script.direction_change.approved_at,
                    }
                    if self.script.direction_change
                    else None
                ),
            },
            "assets": [
                {
                    "path": asset.asset.path,
                    "role": asset.role,
                    "basis": asset.asset.basis,
                    "source": asset.asset.source,
                    "license_id": asset.asset.license_id,
                    "sha256": asset.asset.sha256,
                    "voice": (
                        {"kind": asset.voice.kind, "subject": asset.voice.subject, "evidence": asset.voice.evidence}
                        if asset.voice
                        else None
                    ),
                }
                for asset in self.assets
            ],
            "adapter_audit": self.audit.to_mapping(),
            "artifact": {
                "path": self.path,
                "sha256": self.artifact_sha256,
                "duration_seconds": self.duration_seconds,
                "playable": self.playable,
                "probe_detail": self.probe_detail,
                "captions": [
                    {"index": c.index, "start": c.start_seconds, "end": c.end_seconds, "text": c.text}
                    for c in self.captions
                ],
            },
            "runs_evaluated": self.runs_evaluated,
            "intervention_minutes": self.intervention_minutes,
            "limitations": list(self.limitations),
            "observed_contradictions": list(self.observed_contradictions),
            "readiness": list(self.readiness()),
            "deliverable": self.deliverable(),
            "upload_granted": False,
            "publication_granted": False,
        }


def _parse_captions(raw: object, duration: float) -> tuple[CaptionCue, ...]:
    if raw is None:
        raise PlaybackRefused(
            "the renderer returned no caption payload; captions are part of the deliverable, not an "
            "optional extra."
        )
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PlaybackRefused("caption payload must be a list of cue objects.")
    cues: list[CaptionCue] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PlaybackRefused("each caption cue must be an object.")
        cues.append(
            CaptionCue(
                index=index,
                start_seconds=cast(float, item.get("start")),
                end_seconds=cast(float, item.get("end")),
                text=cast(str, item.get("text")),
            )
        )
    return tuple(cues)


def render_video_draft(
    *,
    renderer: VideoRenderer,
    probe: PlaybackProbe,
    bundle: ResearchBundle,
    topic: TopicPopularity,
    direction: CreativeDirection,
    script: Script,
    assets: Sequence[MediaAsset],
    audit: AdapterAudit,
    aspect: str = "9:16",
    runs_evaluated: int = 1,
    intervention_minutes: float = 0.0,
    limitations: Sequence[str] = (),
    observed_contradictions: Sequence[str] = (),
    run_id: str = "",
) -> VideoDraft:
    """Render the draft, probe the file, and return a record that says what it is worth.

    The report always comes back: a render that failed to produce a playable file is a result, not
    an exception, because an exception is something a caller can catch and report as success. Only
    a malformed render payload raises, and the probe is what decides whether the bytes play.
    """
    if aspect not in {"9:16", "16:9"}:
        raise VideoDraftError("aspect must be 9:16 or 16:9.")
    if not isinstance(bundle, ResearchBundle):
        raise EvidenceRefused("render_video_draft needs the ResearchBundle its claims cite.")
    for cited in script.claims():
        for source_id in cited.source_ids:
            if bundle.source_by_id(source_id) is None:
                raise EvidenceRefused(
                    f"claim {cited.text[:60]!r} cites {source_id!r}, which the run never retrieved; a "
                    "video cannot rest on a source that was not actually read."
                )
    if not isinstance(audit, AdapterAudit):
        raise SecondIntegrationRefused("a render needs the integration audit behind it.")
    for asset in assets:
        if not isinstance(asset, MediaAsset):
            raise RightsRefused("assets must be MediaAsset records.")
    spec = {
        "aspect": aspect,
        "topic": topic.topic,
        "narration": list(script.narration),
        "scenes": [
            {"scene_id": scene.scene_id, "seconds": scene.seconds, "purpose": scene.purpose}
            for scene in script.scenes
        ],
        "assets": [asset.asset.path for asset in assets],
        "engine": audit.candidate,
    }
    rendered = renderer.render(spec)
    if not isinstance(rendered, Mapping):
        raise PlaybackRefused("the renderer must return a mapping describing the output.")
    path = _vd_text(rendered.get("path"), "rendered path", 1000)
    digest = _vd_digest(rendered.get("sha256"), "rendered sha256")
    declared = rendered.get("duration_seconds")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool) or declared <= 0:
        raise PlaybackRefused("the renderer must report the duration it produced.")

    observed = probe.inspect(path, digest)
    if not isinstance(observed, Mapping):
        raise PlaybackRefused("the probe must return a mapping of observations.")
    if str(observed.get("sha256") or "") != digest:
        raise PlaybackRefused(
            "the probe hashed a different file than the renderer reported; a record bound to the "
            "wrong bytes describes a different video."
        )
    duration = observed.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        raise PlaybackRefused("the probe must report a positive duration.")
    playable = observed.get("playable")
    if not isinstance(playable, bool):
        raise PlaybackRefused("the probe must report playable as a boolean.")
    captions = _parse_captions(observed.get("captions"), float(duration))
    if "captions" in rendered:
        # A renderer that reports one set of cues and a probe that observes another means the file
        # and the package disagree. Silently preferring the probe would hide that, which is exactly
        # the silent-skip failure an earlier module shipped.
        rendered_cues = _parse_captions(rendered.get("captions"), float(duration))
        if [(c.start_seconds, c.end_seconds, c.text) for c in rendered_cues] != [
            (c.start_seconds, c.end_seconds, c.text) for c in captions
        ]:
            raise PlaybackRefused(
                "the renderer reported captions the probe does not observe in the file; the subtitle "
                "package and the video disagree about what is said when."
            )

    return VideoDraft(
        topic=topic,
        direction=direction,
        script=script,
        assets=tuple(assets),
        audit=audit,
        path=path,
        artifact_sha256=digest,
        duration_seconds=float(duration),
        captions=captions,
        playable=playable,
        probe_detail=_vd_optional_text(observed.get("detail"), "probe detail", 600),
        runs_evaluated=runs_evaluated,
        intervention_minutes=intervention_minutes,
        limitations=tuple(limitations),
        observed_contradictions=tuple(observed_contradictions),
        run_id=run_id,
    )
