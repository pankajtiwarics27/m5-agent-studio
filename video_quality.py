"""AP-024 — qualify video technical and editorial quality.

AP-023 answers "did the workflow produce a sourced, rights-cleared, playable draft?" This module
answers the question that follows and is much harder to automate: **is the video any good, and did
anyone actually watch it?**

A rendered file can decode, carry the right duration and still be worthless. It can be black for
four seconds in the middle, silent through the one sentence that matters, narrated with a
mispronounced product name, timed with subtitle cues that sit under the wrong scene, and paced so
that nothing happens until the last three seconds. None of that fails a decode check.

Three rules shape the record.

**A watched sample is not a watched video.** Coverage is measured as *ranges of the timeline*, not
as a boolean. ``WatchedCoverage`` is complete only when the ranges span the whole duration with no
gap, and a ``final`` review on incomplete coverage is refused. "I watched the first 30 seconds and
it looked good" is a real sentence a reviewer says, and it is not a review of a 90-second video.

**Every editorial axis carries its evidence and its rubric anchor.** A score of "4" means nothing
on its own; the record requires what the reviewer saw at that score, against a named anchor. The
temptation is a single ``quality: 4/5``, which is a number nobody can audit or disagree with.

**Missing rights or an unsupported consequential claim blocks readiness, whatever the score.**
This is AP-021's and AP-023's rule applied at the last gate: a beautifully made video that uses
footage nobody cleared is not ready. The reasons are computed, never assigned, so a run cannot
decide its own video is finished.

No revenue, monetisation or virality claim appears anywhere: those are named in
``FORBIDDEN_GUARANTEES`` and refused by name, because a platform can change the distribution of a
video overnight and no programme can promise otherwise.

Nothing here reads a clock, opens a file or touches the network; the media facts arrive through
:class:`MediaProbe`, the same port pattern the rest of the programme uses.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence, cast

from autonomous_production_research import ResearchBundle
from video_draft import MediaAsset, NarrationClaim, TopicPopularity

__all__ = [
    "VideoQualityError",
    "ProbeRefused",
    "MediaRefused",
    "AudioRefused",
    "EditorialRefused",
    "CoverageRefused",
    "ReadinessRefused",
    "GuaranteeRefused",
    "MEDIA_CHECKS",
    "AUDIO_CHECKS",
    "EDITORIAL_AXES",
    "EDITORIAL_KINDS",
    "EDITORIAL_ANCHORS",
    "FORBIDDEN_GUARANTEES",
    "EDITORIAL_FLOOR",
    "LOUDNESS_TARGET_LUFS",
    "TRUE_PEAK_CEILING_DBTP",
    "FrameScan",
    "AudioLevels",
    "SubtitleScan",
    "MediaProbe",
    "MediaReport",
    "EditorialAxisScore",
    "EditorialReview",
    "WatchedRange",
    "WatchedCoverage",
    "PlatformConstraint",
    "PilotReview",
    "evaluate_video",
    "refuse_guarantees",
]

VQ_SCHEMA_VERSION = 1
VQ_MAX_TEXT = 2000

#: Technical checks that must actually run on the rendered file. Each is named so a report can say
#: *which* one failed instead of "the video is bad".
MEDIA_CHECKS: tuple[str, ...] = (
    "decodes",
    "dimensions_measured",
    "aspect_matches",
    "duration_matches",
    "no_black_frames",
    "no_frozen_frames",
    "no_missing_frames",
    "subtitle_timing",
    "subtitle_bounds",
)

#: Audio checks. ``pronunciation_reviewed`` and ``narration_matches_script`` are human judgements
#: recorded as checks, not measurements, because no probe can hear a mispronounced brand name.
AUDIO_CHECKS: tuple[str, ...] = (
    "no_silence_gaps",
    "no_clipping",
    "loudness_in_range",
    "pronunciation_reviewed",
    "narration_matches_script",
)

#: A review is either the final judgement of the whole pilot or an explicit sample. The vocabulary
#: exists so "I looked at the first thirty seconds" is recordable *and* visibly insufficient,
#: instead of having to be either overclaimed as a review or hidden.
EDITORIAL_KINDS: tuple[str, ...] = ("final", "sample")

#: The editorial axes. Each is scored by a person against a written anchor, with the evidence that
#: supports the score. There is deliberately no composite "quality score": averaging four
#: judgements into one number is how an unusable video gets a passing grade.
EDITORIAL_AXES: tuple[str, ...] = ("scene_relevance", "pacing", "originality", "audience_fit")

#: Anything below this score on any axis blocks readiness. 3 is "adequate, with the noted flaws"
#: on the 1-5 anchor scale documented in ``EDITORIAL_ANCHORS``.
EDITORIAL_FLOOR = 3

#: Integrated loudness target band, in LUFS. Quoted as a band rather than a point because a short
#: piece legitimately varies; ``-14`` is the common streaming deliverable.
LOUDNESS_TARGET_LUFS: tuple[float, float] = (-16.0, -12.0)

#: True-peak ceiling, in dBTP. Above 0 the file will clip on some encoders and players.
TRUE_PEAK_CEILING_DBTP = -1.0

#: The 1-5 anchor scale every editorial score is judged against. Shown in the report so a reader
#: can see what "4" meant, and so two reviews can be compared at all.
EDITORIAL_ANCHORS: Mapping[int, str] = {
    1: "fails the brief: wrong subject, wrong audience or unusable",
    2: "recognisable attempt with a defect that would stop delivery",
    3: "adequate: it does the job, with flaws that are named",
    4: "good: the reviewer would publish it as-is",
    5: "exemplary: worth copying as a reference next time",
}

#: Claims this story refuses to make, named so their absence is a decision rather than an
#: oversight. None is measurable from inside this programme, and a platform's distribution is not
#: ours to promise.
FORBIDDEN_GUARANTEES: tuple[str, ...] = (
    "monetisation",
    "ad_revenue",
    "rpm",
    "cpm",
    "virality",
    "algorithm_boost",
    "platform_compliance",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class VideoQualityError(ValueError):
    """The qualification record is malformed or claims something it cannot support."""


class ProbeRefused(VideoQualityError):
    """The probe returned facts that do not describe the rendered file."""


class MediaRefused(VideoQualityError):
    """A technical measurement is missing, impossible, or contradicts another measurement."""


class AudioRefused(VideoQualityError):
    """An audio measurement is missing or out of range in a way that blocks delivery."""


class EditorialRefused(VideoQualityError):
    """An editorial score has no evidence, no anchor, or was given by a machine."""


class CoverageRefused(VideoQualityError):
    """A review claims to cover the whole video and the watched ranges do not."""


class ReadinessRefused(VideoQualityError):
    """A readiness field was set directly instead of being derived."""


class GuaranteeRefused(VideoQualityError):
    """The report tries to promise monetisation, virality or platform compliance."""


def _vq_text(value: object, field_name: str, limit: int = VQ_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise VideoQualityError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise VideoQualityError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise VideoQualityError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise VideoQualityError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _vq_optional(value: object, field_name: str, limit: int = VQ_MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if "\x00" in text:
        raise VideoQualityError(f"{field_name} must not contain NUL characters.")
    if len(text) > limit:
        raise VideoQualityError(f"{field_name} must be at most {limit} characters.")
    return text


def _vq_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise VideoQualityError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _vq_number(value: object, field_name: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise VideoQualityError(f"{field_name} must be a number; 'mostly' is not a measurement.")
    number = float(value)
    if minimum is not None and number < minimum:
        raise VideoQualityError(f"{field_name} must be at least {minimum}.")
    return number


# ---------------------------------------------------------------------------
# AC1 — the file: decode, dimensions, duration, damaged frames, subtitles.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrameScan:
    """What a frame scan found, as counts and positions rather than a verdict.

    Black, frozen and missing frames are separate because they have different causes. A black
    frame means the edit lost its picture; a frozen frame means the encoder repeated one; a missing
    frame means the timestamps jumped. One ``damaged: true`` would hide which.
    """

    black_frames: int
    frozen_frames: int
    missing_frames: int
    first_defect_seconds: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        for attr in ("black_frames", "frozen_frames", "missing_frames"):
            value = getattr(self, attr)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MediaRefused(f"{attr} must be a non-negative integer.")
        if self.first_defect_seconds is not None:
            object.__setattr__(
                self,
                "first_defect_seconds",
                _vq_number(self.first_defect_seconds, "first_defect_seconds", minimum=0.0),
            )
        elif self.defect_count() > 0:
            raise MediaRefused(
                "a scan that found defects must say where the first one is; a count with no position "
                "gives an editor nothing to look at."
            )
        object.__setattr__(self, "note", _vq_optional(self.note, "frame scan note", 600))

    def defect_count(self) -> int:
        return self.black_frames + self.frozen_frames + self.missing_frames

    def to_mapping(self) -> dict[str, Any]:
        return {
            "black_frames": self.black_frames,
            "frozen_frames": self.frozen_frames,
            "missing_frames": self.missing_frames,
            "defect_count": self.defect_count(),
            "first_defect_seconds": self.first_defect_seconds,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class AudioLevels:
    """Integrated loudness, true peak and clipping events for the whole file."""

    integrated_lufs: float
    true_peak_dbtp: float
    clipping_events: int = 0
    silence_seconds: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "integrated_lufs", _vq_number(self.integrated_lufs, "integrated_lufs"))
        object.__setattr__(self, "true_peak_dbtp", _vq_number(self.true_peak_dbtp, "true_peak_dbtp"))
        if not isinstance(self.clipping_events, int) or isinstance(self.clipping_events, bool) or self.clipping_events < 0:
            raise AudioRefused("clipping_events must be a non-negative integer.")
        object.__setattr__(
            self, "silence_seconds", _vq_number(self.silence_seconds, "silence_seconds", minimum=0.0)
        )

    def loudness_in_range(self) -> bool:
        low, high = LOUDNESS_TARGET_LUFS
        return low <= self.integrated_lufs <= high

    def peak_ok(self) -> bool:
        return self.true_peak_dbtp <= TRUE_PEAK_CEILING_DBTP

    def to_mapping(self) -> dict[str, Any]:
        return {
            "integrated_lufs": self.integrated_lufs,
            "true_peak_dbtp": self.true_peak_dbtp,
            "clipping_events": self.clipping_events,
            "silence_seconds": self.silence_seconds,
            "target_lufs": list(LOUDNESS_TARGET_LUFS),
            "true_peak_ceiling_dbtp": TRUE_PEAK_CEILING_DBTP,
        }


@dataclass(frozen=True, slots=True)
class SubtitleScan:
    """Subtitle timing and bounds, measured against the video rather than against prose."""

    cue_count: int
    out_of_bounds: int = 0
    overlaps: int = 0
    unreadable_cues: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        for attr in ("cue_count", "out_of_bounds", "overlaps", "unreadable_cues"):
            value = getattr(self, attr)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MediaRefused(f"{attr} must be a non-negative integer.")
        if self.cue_count == 0:
            raise MediaRefused(
                "a subtitle scan with no cues is not a passing scan; captions are part of the package."
            )
        object.__setattr__(self, "note", _vq_optional(self.note, "subtitle scan note", 600))

    def clean(self) -> bool:
        return self.out_of_bounds == 0 and self.overlaps == 0 and self.unreadable_cues == 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "cue_count": self.cue_count,
            "out_of_bounds": self.out_of_bounds,
            "overlaps": self.overlaps,
            "unreadable_cues": self.unreadable_cues,
            "clean": self.clean(),
            "note": self.note,
        }


class MediaProbe(Protocol):
    """The measurement this module refuses to perform itself.

    A real implementation runs ffprobe plus a frame scan and a loudness pass. The module never
    opens a file, so the same rules run against a recording double in CI.
    """

    def inspect(self, path: str, sha256: str) -> Mapping[str, Any]:
        """Return the observed media facts for the file at ``path``."""
        ...


@dataclass(frozen=True, slots=True)
class MediaReport:
    """Everything measured about the rendered file, parsed strictly."""

    artifact_sha256: str
    duration_seconds: float
    width: int
    height: int
    aspect: str
    frames: FrameScan
    audio: AudioLevels
    subtitles: SubtitleScan
    checks: Mapping[str, bool] = field(default_factory=dict)
    decodes: bool = True
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_sha256", _vq_digest(self.artifact_sha256, "artifact_sha256"))
        object.__setattr__(
            self, "duration_seconds", _vq_number(self.duration_seconds, "duration_seconds", minimum=0.001)
        )
        for attr in ("width", "height"):
            value = getattr(self, attr)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise MediaRefused(f"{attr} must be a positive integer.")
        aspect = _vq_text(self.aspect, "aspect", 16)
        if aspect not in {"9:16", "16:9"}:
            raise MediaRefused(f"aspect {aspect!r} is unknown; expected 9:16 or 16:9.")
        object.__setattr__(self, "aspect", aspect)
        for attr, kind in (("frames", FrameScan), ("audio", AudioLevels), ("subtitles", SubtitleScan)):
            if not isinstance(getattr(self, attr), kind):
                raise MediaRefused(f"{attr} must be a {kind.__name__} value.")
        if not isinstance(self.decodes, bool):
            raise MediaRefused("decodes must be a boolean; a file that 'mostly' decodes does not.")
        unknown = sorted(set(self.checks) - set(MEDIA_CHECKS) - set(AUDIO_CHECKS))
        if unknown:
            raise MediaRefused(
                f"checks name checks this rubric does not have: {unknown}; an invented check "
                "measures nothing."
            )
        object.__setattr__(self, "checks", {str(k): bool(v) for k, v in self.checks.items()})

    def measured_aspect(self) -> str:
        """The aspect the pixels actually are, to within a rounding tolerance."""
        return "9:16" if self.height > self.width else "16:9"

    def failed_checks(self) -> tuple[str, ...]:
        failed: list[str] = []
        if not self.decodes:
            failed.append("decodes")
        failed.extend(name for name in MEDIA_CHECKS if self.checks.get(name) is False)
        failed.extend(name for name in AUDIO_CHECKS if self.checks.get(name) is False)
        return tuple(failed)

    def missing_checks(self) -> tuple[str, ...]:
        return tuple(name for name in (*MEDIA_CHECKS, *AUDIO_CHECKS) if name not in self.checks)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "aspect": self.aspect,
            "measured_aspect": self.measured_aspect(),
            "decodes": self.decodes,
            "frames": self.frames.to_mapping(),
            "audio": self.audio.to_mapping(),
            "subtitles": self.subtitles.to_mapping(),
            "checks": dict(self.checks),
            "failed_checks": list(self.failed_checks()),
            "missing_checks": list(self.missing_checks()),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# AC2/AC3 — editorial review with anchors and evidence.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditorialAxisScore:
    """One axis, one score, one written reason and the anchor it was judged against."""

    axis: str
    score: int
    evidence: str

    def __post_init__(self) -> None:
        axis = _vq_text(self.axis, "axis", 40)
        if axis not in EDITORIAL_AXES:
            raise EditorialRefused(
                f"axis {axis!r} is unknown; expected one of {list(EDITORIAL_AXES)}. An axis nobody "
                "named cannot be scored."
            )
        object.__setattr__(self, "axis", axis)
        if not isinstance(self.score, int) or isinstance(self.score, bool) or not 1 <= self.score <= 5:
            raise EditorialRefused(
                f"score for {axis} must be an integer 1-5 against EDITORIAL_ANCHORS; a 7 or a "
                "'good' has no anchor to compare with."
            )
        object.__setattr__(self, "evidence", _vq_text(self.evidence, "axis evidence", 1000))

    def anchor(self) -> str:
        return EDITORIAL_ANCHORS[self.score]

    def blocks_readiness(self) -> bool:
        return self.score < EDITORIAL_FLOOR

    def to_mapping(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "score": self.score,
            "anchor": self.anchor(),
            "evidence": self.evidence,
            "blocks_readiness": self.blocks_readiness(),
        }


@dataclass(frozen=True, slots=True)
class EditorialReview:
    """A person's judgement of the video, axis by axis, never a single composite score.

    ``actor`` must be namespaced the way every other human decision in this programme is, so a
    score is attributable or it does not exist. ``watched_before_scoring`` is required: a reviewer
    who scored before watching is describing expectations, not the video.
    """

    actor: str
    scores: tuple[EditorialAxisScore, ...]
    watched_before_scoring: bool
    kind: str = "final"

    def __post_init__(self) -> None:
        who = _vq_text(self.actor, "review actor", 200)
        prefix, _, name = who.partition(":")
        if prefix.strip().lower() not in {"owner", "user", "reviewer"} or not name.strip():
            raise EditorialRefused(
                f"review actor {who!r} is not a named person (owner:, user: or reviewer:); an agent "
                "cannot review its own video."
            )
        object.__setattr__(self, "actor", who)
        if not isinstance(self.watched_before_scoring, bool):
            raise EditorialRefused("watched_before_scoring must be a boolean.")
        kind = _vq_text(self.kind, "review kind", 20)
        if kind not in EDITORIAL_KINDS:
            raise EditorialRefused(
                f"review kind {kind!r} is unknown; expected one of {list(EDITORIAL_KINDS)}."
            )
        object.__setattr__(self, "kind", kind)
        if not self.watched_before_scoring:
            raise EditorialRefused(
                "an editorial score given before watching describes the expectation, not the video."
            )
        seen: dict[str, EditorialAxisScore] = {}
        for raw in self.scores:
            if not isinstance(raw, EditorialAxisScore):
                raise EditorialRefused("scores must be EditorialAxisScore records.")
            if raw.axis in seen:
                raise EditorialRefused(f"{raw.axis} is scored twice; one axis, one judgement.")
            seen[raw.axis] = raw
        missing = [axis for axis in EDITORIAL_AXES if axis not in seen]
        if missing:
            raise EditorialRefused(
                f"these axes have no score: {missing}. An axis nobody scored is not an axis that "
                "passed; the report has to say so rather than average around it."
            )
        object.__setattr__(self, "scores", tuple(seen[axis] for axis in EDITORIAL_AXES))

    def blocking_axes(self) -> tuple[str, ...]:
        return tuple(score.axis for score in self.scores if score.blocks_readiness())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "watched_before_scoring": self.watched_before_scoring,
            "kind": self.kind,
            "scores": [score.to_mapping() for score in self.scores],
            "blocking_axes": list(self.blocking_axes()),
        }


# ---------------------------------------------------------------------------
# AC5 — the entire pilot is watched; a sample is not a final review.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchedRange:
    """One stretch of timeline a person actually watched."""

    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_seconds", _vq_number(self.start_seconds, "start_seconds", minimum=0.0))
        object.__setattr__(self, "end_seconds", _vq_number(self.end_seconds, "end_seconds", minimum=0.0))
        if self.end_seconds <= self.start_seconds:
            raise CoverageRefused("a watched range must end after it starts.")

    def covers(self, start: float, end: float) -> bool:
        return self.start_seconds <= start and self.end_seconds >= end


@dataclass(frozen=True, slots=True)
class WatchedCoverage:
    """Which parts of the video a person watched, measured on the timeline.

    Completeness is a property of the *ranges*, not a flag. Gaps are computed by sorting and
    merging, so "I watched it all" is only true when the numbers say the whole duration is covered.
    """

    duration_seconds: float
    ranges: tuple[WatchedRange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "duration_seconds", _vq_number(self.duration_seconds, "duration_seconds", minimum=0.001)
        )
        ranges: list[WatchedRange] = []
        for raw in self.ranges:
            if not isinstance(raw, WatchedRange):
                raise CoverageRefused("ranges must be WatchedRange records.")
            if raw.end_seconds > self.duration_seconds + 0.5:
                raise CoverageRefused(
                    f"watched range ends at {raw.end_seconds}s, after the {self.duration_seconds}s "
                    "video; a range nobody could have watched is not coverage."
                )
            ranges.append(raw)
        object.__setattr__(self, "ranges", tuple(sorted(ranges, key=lambda r: r.start_seconds)))

    def gaps(self, *, tolerance: float = 0.5) -> tuple[tuple[float, float], ...]:
        """Unwatched stretches longer than ``tolerance``, in order."""
        gaps: list[tuple[float, float]] = []
        cursor = 0.0
        for span in self.ranges:
            if span.start_seconds - cursor > tolerance:
                gaps.append((round(cursor, 3), round(span.start_seconds, 3)))
            cursor = max(cursor, span.end_seconds)
        if self.duration_seconds - cursor > tolerance:
            gaps.append((round(cursor, 3), round(self.duration_seconds, 3)))
        return tuple(gaps)

    def watched_seconds(self) -> float:
        total = 0.0
        cursor = 0.0
        for span in self.ranges:
            start = max(cursor, span.start_seconds)
            if span.end_seconds > start:
                total += span.end_seconds - start
                cursor = span.end_seconds
        return round(min(total, self.duration_seconds), 3)

    def is_complete(self) -> bool:
        return self.watched_seconds() >= self.duration_seconds - 0.5

    def coverage_ratio(self) -> float:
        return round(self.watched_seconds() / self.duration_seconds, 3)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "ranges": [
                {"start": r.start_seconds, "end": r.end_seconds} for r in self.ranges
            ],
            "watched_seconds": self.watched_seconds(),
            "coverage_ratio": self.coverage_ratio(),
            "gaps": [{"start": s, "end": e} for s, e in self.gaps()],
            "complete": self.is_complete(),
        }


# ---------------------------------------------------------------------------
# AC6 — platform constraints recorded; no monetisation guarantee.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformConstraint:
    """One platform rule the draft has to respect, and whether the draft meets it.

    This is a record of a constraint, not a guarantee of compliance: naming a rule is not evidence
    that the published video satisfies it, and the report says so.
    """

    platform: str
    constraint: str
    met: bool
    evidence: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", _vq_text(self.platform, "platform", 80))
        object.__setattr__(self, "constraint", _vq_text(self.constraint, "constraint", 600))
        if not isinstance(self.met, bool):
            raise VideoQualityError("constraint met must be a boolean.")
        evidence = _vq_optional(self.evidence, "constraint evidence", 600)
        if not self.met and not evidence:
            raise VideoQualityError(
                f"the {self.platform} constraint {self.constraint[:60]!r} is unmet and unnamed; an "
                "unmet rule without a stated gap is a reviewer guessing."
            )
        object.__setattr__(self, "evidence", evidence)

    def blocks_readiness(self) -> bool:
        return not self.met

    def to_mapping(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "constraint": self.constraint,
            "met": self.met,
            "evidence": self.evidence,
            "blocks_readiness": self.blocks_readiness(),
        }


# ---------------------------------------------------------------------------
# The review.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PilotReview:
    """One pilot's technical and editorial verdict, with everything it could not settle.

    ``ready()`` is derived: the media has to be clean, every audio check has to have run, a person
    has to have watched the **whole** thing and scored every axis at or above the floor, no asset
    may have unresolved rights, and no consequential claim may be unsupported. Nothing here can be
    set.
    """

    media: MediaReport
    topic: TopicPopularity
    bundle: ResearchBundle
    claims: tuple[NarrationClaim, ...]
    assets: tuple[MediaAsset, ...]
    coverage: WatchedCoverage
    editorial: EditorialReview | None = None
    constraints: tuple[PlatformConstraint, ...] = ()
    runs_evaluated: int = 1
    intervention_minutes: float = 0.0
    limitations: tuple[str, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.media, MediaReport):
            raise MediaRefused("a review needs a MediaReport; unmeasured video is not reviewed video.")
        if not isinstance(self.topic, TopicPopularity):
            raise VideoQualityError("a review needs the TopicPopularity AP-023 established.")
        if not isinstance(self.bundle, ResearchBundle):
            raise ReadinessRefused(
                "a review needs the ResearchBundle its consequential claims cite; 'I checked the "
                "sources' without the sources is a claim about a claim."
            )
        if not isinstance(self.coverage, WatchedCoverage):
            raise CoverageRefused("a review needs WatchedCoverage; 'I watched it' is not a record.")
        if abs(self.coverage.duration_seconds - self.media.duration_seconds) > 0.5:
            raise CoverageRefused(
                "the coverage describes a different duration than the file; the reviewer watched "
                "something else."
            )
        claims: list[NarrationClaim] = []
        for raw_claim in self.claims:
            if not isinstance(raw_claim, NarrationClaim):
                raise ReadinessRefused("claims must be NarrationClaim records.")
            claims.append(raw_claim)
        object.__setattr__(self, "claims", tuple(claims))
        assets: list[MediaAsset] = []
        for raw_asset in self.assets:
            if not isinstance(raw_asset, MediaAsset):
                raise ReadinessRefused("assets must be MediaAsset records.")
            assets.append(raw_asset)
        object.__setattr__(self, "assets", tuple(assets))
        if self.editorial is not None and not isinstance(self.editorial, EditorialReview):
            raise EditorialRefused("editorial must be an EditorialReview record.")
        constraints: list[PlatformConstraint] = []
        for raw_constraint in self.constraints:
            if not isinstance(raw_constraint, PlatformConstraint):
                raise VideoQualityError("constraints must be PlatformConstraint records.")
            constraints.append(raw_constraint)
        object.__setattr__(self, "constraints", tuple(constraints))
        if not isinstance(self.runs_evaluated, int) or isinstance(self.runs_evaluated, bool) or self.runs_evaluated < 1:
            raise VideoQualityError("runs_evaluated must be a positive integer; every pilot is reported.")
        object.__setattr__(
            self, "intervention_minutes", _vq_number(self.intervention_minutes, "intervention_minutes", minimum=0.0)
        )
        object.__setattr__(self, "limitations", tuple(str(x).strip() for x in self.limitations if str(x).strip()))

    # -- derived verdict -----------------------------------------------------------------

    def rights_gaps(self) -> tuple[str, ...]:
        return tuple(
            f"{asset.asset.path}: {asset.asset.basis} rights ({asset.asset.license_id or 'no owner recorded'})"
            for asset in self.assets
            if asset.blocks_delivery()
        )

    def unsupported_claims(self) -> tuple[str, ...]:
        """Consequential claims that cite nothing the run actually retrieved.

        Blocking regardless of how good the video looks, and checked here as well as in AP-023
        because this is the last gate before a video is called ready: a claim that cited a source
        which never resolved is unsupported, and the reviewer is not expected to notice that by
        watching.
        """
        return tuple(
            claim.text
            for claim in self.claims
            if claim.important and not claim.is_supported(self.bundle)
        )

    def blocking_constraints(self) -> tuple[str, ...]:
        return tuple(f"{c.platform}: {c.constraint}" for c in self.constraints if c.blocks_readiness())

    def readiness(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.media.decodes:
            reasons.append("the rendered file does not decode")
        reasons.extend(f"media check failed: {name}" for name in self.media.failed_checks() if name != "decodes")
        reasons.extend(f"media check never ran: {name}" for name in self.media.missing_checks())
        if self.media.measured_aspect() != self.media.aspect:
            reasons.append(
                f"the file is {self.media.width}x{self.media.height} ({self.media.measured_aspect()}) but "
                f"the draft claims {self.media.aspect}"
            )
        if self.media.frames.defect_count() > 0:
            reasons.append(
                f"the frame scan found {self.media.frames.black_frames} black, "
                f"{self.media.frames.frozen_frames} frozen and {self.media.frames.missing_frames} "
                f"missing frames, first at {self.media.frames.first_defect_seconds}s"
            )
        if not self.media.audio.loudness_in_range():
            reasons.append(
                f"integrated loudness {self.media.audio.integrated_lufs} LUFS is outside the "
                f"{LOUDNESS_TARGET_LUFS[0]}..{LOUDNESS_TARGET_LUFS[1]} band"
            )
        if not self.media.audio.peak_ok():
            reasons.append(
                f"true peak {self.media.audio.true_peak_dbtp} dBTP exceeds the "
                f"{TRUE_PEAK_CEILING_DBTP} ceiling"
            )
        if self.media.audio.clipping_events > 0:
            reasons.append(f"the audio clips {self.media.audio.clipping_events} times")
        if not self.media.subtitles.clean():
            reasons.append(
                "the subtitle scan is not clean "
                f"({self.media.subtitles.out_of_bounds} out of bounds, {self.media.subtitles.overlaps} "
                f"overlapping, {self.media.subtitles.unreadable_cues} unreadable)"
            )
        reasons.extend(self.rights_gaps())
        for text in self.unsupported_claims():
            reasons.append(f"consequential claim has no evidence: {text[:80]!r}")
        if not self.coverage.is_complete():
            gaps = ", ".join(f"{start}-{end}s" for start, end in self.coverage.gaps()) or "none"
            reasons.append(
                f"nobody watched the whole pilot ({self.coverage.coverage_ratio():.0%} covered; "
                f"unwatched {gaps})"
            )
        if self.editorial is None:
            reasons.append("no editorial review: a rendered file is not a reviewed video")
        elif self.editorial.kind == "sample":
            reasons.append(
                "the editorial review is a sample of the pilot; a sample is recorded here and cannot "
                "stand in for the final review"
            )
        else:
            reasons.extend(f"editorial axis below the floor: {axis}" for axis in self.editorial.blocking_axes())
        reasons.extend(f"unmet platform constraint: {item}" for item in self.blocking_constraints())
        return tuple(reasons)

    def ready(self) -> bool:
        return not self.readiness()

    def watched_sample_only(self) -> bool:
        """True when a review exists but the coverage is partial: a sample, not a review."""
        return self.editorial is not None and not self.coverage.is_complete()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": VQ_SCHEMA_VERSION,
            "run_id": self.run_id,
            "artifact_sha256": self.media.artifact_sha256,
            "media": self.media.to_mapping(),
            "topic": self.topic.to_mapping(),
            "claims": [
                {
                    "text": claim.text,
                    "source_ids": list(claim.source_ids),
                    "important": claim.important,
                    "uncertainty": claim.uncertainty,
                }
                for claim in self.claims
            ],
            "assets": [
                {
                    "path": asset.asset.path,
                    "role": asset.role,
                    "basis": asset.asset.basis,
                    "license_id": asset.asset.license_id,
                }
                for asset in self.assets
            ],
            "sources_cited": [
                source_id for claim in self.claims for source_id in claim.source_ids
            ],
            "coverage": self.coverage.to_mapping(),
            "editorial": self.editorial.to_mapping() if self.editorial else None,
            "constraints": [c.to_mapping() for c in self.constraints],
            "runs_evaluated": self.runs_evaluated,
            "intervention_minutes": self.intervention_minutes,
            "limitations": list(self.limitations),
            "readiness": list(self.readiness()),
            "ready": self.ready(),
            "watched_sample_only": self.watched_sample_only(),
            "guarantees_made": [],
            "guarantees_refused": list(FORBIDDEN_GUARANTEES),
            "upload_granted": False,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_media(observed: Mapping[str, Any], artifact_sha256: str) -> MediaReport:
    checks = observed.get("checks")
    if not isinstance(checks, Mapping):
        raise ProbeRefused("the probe must report a checks mapping; omitted checks are not passes.")
    frames = observed.get("frames")
    audio = observed.get("audio")
    subtitles = observed.get("subtitles")
    for name, value in (("frames", frames), ("audio", audio), ("subtitles", subtitles)):
        if not isinstance(value, Mapping):
            raise ProbeRefused(f"the probe must report {name} as an object.")
    frames_map = cast(Mapping[str, Any], frames)
    audio_map = cast(Mapping[str, Any], audio)
    subtitle_map = cast(Mapping[str, Any], subtitles)
    duration = observed.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ProbeRefused("the probe must report duration_seconds as a number.")
    return MediaReport(
        artifact_sha256=artifact_sha256,
        duration_seconds=float(duration),
        width=cast(int, observed.get("width")),
        height=cast(int, observed.get("height")),
        aspect=cast(str, observed.get("aspect")),
        decodes=cast(bool, observed.get("decodes")),
        frames=FrameScan(
            black_frames=cast(int, frames_map.get("black_frames")),
            frozen_frames=cast(int, frames_map.get("frozen_frames")),
            missing_frames=cast(int, frames_map.get("missing_frames")),
            first_defect_seconds=cast("float | None", frames_map.get("first_defect_seconds")),
            note=cast(str, frames_map.get("note") or ""),
        ),
        audio=AudioLevels(
            integrated_lufs=cast(float, audio_map.get("integrated_lufs")),
            true_peak_dbtp=cast(float, audio_map.get("true_peak_dbtp")),
            clipping_events=cast(int, audio_map.get("clipping_events") or 0),
            silence_seconds=cast(float, audio_map.get("silence_seconds") or 0.0),
        ),
        subtitles=SubtitleScan(
            cue_count=cast(int, subtitle_map.get("cue_count")),
            out_of_bounds=cast(int, subtitle_map.get("out_of_bounds") or 0),
            overlaps=cast(int, subtitle_map.get("overlaps") or 0),
            unreadable_cues=cast(int, subtitle_map.get("unreadable_cues") or 0),
            note=cast(str, subtitle_map.get("note") or ""),
        ),
        checks={str(k): bool(v) for k, v in checks.items()},
        notes=tuple(str(x) for x in (observed.get("notes") or ())),
    )


def evaluate_video(
    *,
    probe: MediaProbe,
    artifact_sha256: str,
    path: str,
    topic: TopicPopularity,
    bundle: ResearchBundle,
    claims: Sequence[NarrationClaim],
    assets: Sequence[MediaAsset],
    coverage: WatchedCoverage,
    editorial: EditorialReview | None = None,
    constraints: Sequence[PlatformConstraint] = (),
    runs_evaluated: int = 1,
    intervention_minutes: float = 0.0,
    limitations: Sequence[str] = (),
    run_id: str = "",
) -> PilotReview:
    """Measure the file, attach the review, and return a verdict that states its own limits.

    The review always comes back: an unqualified video is a result, not an error, because an error
    is something a caller can catch and report as success. Only a malformed probe raises.
    """
    if not isinstance(artifact_sha256, str) or not _DIGEST_RE.fullmatch(artifact_sha256):
        raise ProbeRefused("artifact_sha256 must be a lowercase SHA-256 digest.")
    observed = probe.inspect(_vq_text(path, "path", 1000), artifact_sha256)
    if not isinstance(observed, Mapping):
        raise ProbeRefused("the probe must return a mapping of observations.")
    reported = str(observed.get("sha256") or "")
    if reported != artifact_sha256:
        raise ProbeRefused(
            "the probe measured a different file than the one asked about; a review bound to the "
            "wrong bytes describes a different video."
        )
    media = _parse_media(observed, artifact_sha256)
    review = PilotReview(
        media=media,
        topic=topic,
        bundle=bundle,
        claims=tuple(claims),
        assets=tuple(assets),
        coverage=coverage,
        editorial=editorial,
        constraints=tuple(constraints),
        runs_evaluated=runs_evaluated,
        intervention_minutes=intervention_minutes,
        limitations=tuple(limitations),
        run_id=run_id,
    )
    # A review that presents itself as *final* while the watched ranges leave the pilot unwatched
    # is refused outright. Marking it partial would let a caller read the verdict as "ready except
    # for a detail"; the detail is that nobody has seen the end of the video.
    if editorial is not None and editorial.kind == "final" and not review.coverage.is_complete():
        gaps = ", ".join(f"{start}-{end}s" for start, end in review.coverage.gaps()) or "unknown"
        raise CoverageRefused(
            f"the editorial review is final but the watched coverage is "
            f"{review.coverage.coverage_ratio():.0%} (unwatched {gaps}); record it as a sample, or "
            "watch the pilot before calling the review final."
        )
    return review


def refuse_guarantees(claims: Iterable[str]) -> None:
    """Reject any attempt to attach a monetisation or distribution guarantee to a review."""
    for claim in claims:
        normalized = str(claim).strip().lower().replace(" ", "_")
        if normalized in FORBIDDEN_GUARANTEES:
            raise GuaranteeRefused(
                f"{claim!r} is not something this programme can promise. Reporting it would mean "
                f"inventing a platform outcome; refused guarantees are {list(FORBIDDEN_GUARANTEES)}."
            )
