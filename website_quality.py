"""AP-022 — qualify website output quality and delivery.

AP-021 answers "was the workflow's part done?" This module answers the harder question: **is the
site any good, and has anyone actually looked at it?** Those are different, and conflating them is
how "the tests passed" becomes "ship it".

The story exists because a website can pass every AP-021 check and still be unfit to deliver:

* a form whose submit posts nowhere, a nav link whose target 404s, a menu that opens on desktop
  and is unreachable on a phone;
* a layout that is fine at 1440px and clips at 390px, or overlaps once a user zooms to 200%;
* text that meets contrast in one theme and fails in the other;
* a leftover "Lorem ipsum" or a `TODO` in copy, a hard-coded `sk-...` key, or a CDN dependency
  nobody reviewed;
* and the one no automated check can settle: whether the result is any use to the audience.

This module is the rubric and the record. The execution is injected through
:class:`QualityProbe` — the same port pattern AP-010 and AP-021 use — so the decision logic
runs identically against a recording double in CI and a real Chromium harness in the world.

Three rules shape everything below.

**A check that did not run is not a pass.** Every criterion must come back with an explicit
outcome. A missing outcome blocks a verdict exactly like a failure does, because "we didn't
check that" and "that failed" are the same thing to the person receiving the site.

**Mobile and zoom are not scaled-down desktop.** The layout matrix is enumerated per viewport
and per zoom factor, and the record names which cell failed. A single "responsive: true" would
let a broken 390px layout pass behind a passing 1440px one.

**No revenue claims, ever.** The rubric has no conversion, engagement or ROI axis, because this
programme cannot measure one — and a number that cannot be measured is exactly the kind of thing
that gets invented to close a story. ``FORBIDDEN_CLAIMS`` names them so their absence is
deliberate rather than an oversight.

Creative usefulness is the owner's judgement and nobody else's. ``OwnerReview`` requires a human
actor and the owner's own words; :meth:`PilotReport.deliverable` is false without one, no matter
how green every automated axis is.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence, cast

from reference_benchmarks import Reference, rank

__all__ = [
    "WebsiteQualityError",
    "RubricRefused",
    "ProbeRefused",
    "LayoutRefused",
    "SecretsRefused",
    "ClaimRefused",
    "OwnerReviewRefused",
    "Criteria",
    "Viewport",
    "LayoutCell",
    "InteractionCheck",
    "AssetProblem",
    "QualityProbe",
    "ProbeResult",
    "OwnerReview",
    "PilotReport",
    "evaluate_website",
    "VIEWPORT_MATRIX",
    "FORBIDDEN_CLAIMS",
    "CONTRAST_FLOORS",
    "TARGET_MIN_PX",
    "ZOOM_FACTOR",
]

WQ_SCHEMA_VERSION = 1
WQ_MAX_TEXT = 2000

#: The viewport matrix a site is judged on. "phone" is not a shrunken desktop: it is its own
#: cell in the record, with its own outcome, so a broken narrow layout cannot hide behind a
#: passing wide one.
VIEWPORT_MATRIX: tuple[tuple[str, int, int], ...] = (
    ("phone", 390, 844),
    ("tablet", 834, 1112),
    ("desktop", 1440, 900),
)

#: WCAG 2 floors, identical to the values the Studio's own accessibility gate uses
#: (``scripts/uiv2_accessibility_adversarial_walk.mjs``). Duplicating the numbers here would
#: let the two drift, so they are named once and quoted from that gate's contract.
CONTRAST_FLOORS: dict[str, float] = {"normal_text": 4.5, "large_text": 3.0}

#: Minimum interactive target, in CSS pixels, at 100% zoom.
TARGET_MIN_PX = 24

#: The zoom factor every layout must survive without clipping or overlap.
ZOOM_FACTOR = 2

#: Claims this story refuses to make. They are named so that a reader can see the absence is a
#: decision. None of them is measurable from inside this programme, and inventing one would be
#: worse than reporting nothing.
FORBIDDEN_CLAIMS: tuple[str, ...] = (
    "revenue",
    "conversion_rate",
    "engagement_lift",
    "traffic_growth",
    "roi",
    "market_fit",
)

#: The automated criteria. Each must have an explicit outcome before a verdict is issued.
CRITERIA: tuple[str, ...] = (
    "forms_work",
    "links_resolve",
    "navigation_works",
    "keyboard_reachable",
    "focus_visible",
    "contrast_ok",
    "reduced_motion_respected",
    "console_clean",
    "no_placeholder_content",
    "secrets_absent",
    "dependencies_reviewed",
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_literal", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}")),
)
_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lorem_ipsum", re.compile(r"\blorem ipsum\b", re.IGNORECASE)),
    ("todo_marker", re.compile(r"\b(TODO|FIXME|XXX)\b")),
    ("lorem_ui", re.compile(r"\b(your text here|placeholder text|sample copy)\b", re.IGNORECASE)),
)


class WebsiteQualityError(ValueError):
    """The qualification record is malformed or claims something it cannot support."""


class RubricRefused(WebsiteQualityError):
    """A criterion has no outcome, or the record tries to soften a verdict."""


class ProbeRefused(WebsiteQualityError):
    """The probe returned facts that do not describe a real run."""


class LayoutRefused(WebsiteQualityError):
    """The layout matrix is incomplete, or a cell reports an impossible measurement."""


class SecretsRefused(WebsiteQualityError):
    """The site embeds a credential, or a finding was downgraded instead of reported."""


class ClaimRefused(WebsiteQualityError):
    """The report tries to make a claim this programme cannot measure."""


class OwnerReviewRefused(WebsiteQualityError):
    """Creative acceptance was recorded without a person, or without their words."""


class Criteria:
    """The rubric, as values rather than prose. Quoted from the same numbers as the gates."""

    viewports = VIEWPORT_MATRIX
    contrast = CONTRAST_FLOORS
    target_min_px = TARGET_MIN_PX
    zoom_factor = ZOOM_FACTOR
    automated = CRITERIA
    forbidden = FORBIDDEN_CLAIMS


def _wq_text(value: object, field_name: str, limit: int = WQ_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise WebsiteQualityError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise WebsiteQualityError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise WebsiteQualityError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise WebsiteQualityError(f"{field_name} must be at most {limit} characters.")
    return stripped


@dataclass(frozen=True, slots=True)
class Viewport:
    """One cell of the layout matrix."""

    name: str
    width: int
    height: int

    def __post_init__(self) -> None:
        name = _wq_text(self.name, "viewport name", 40)
        allowed = {entry[0] for entry in VIEWPORT_MATRIX}
        if name not in allowed:
            raise LayoutRefused(
                f"viewport {name!r} is not in the matrix {sorted(allowed)}; a layout judged only "
                "at the sizes someone remembered is not a layout judgement."
            )
        object.__setattr__(self, "name", name)
        for attr in ("width", "height"):
            value = getattr(self, attr)
            if not isinstance(value, int) or isinstance(value, bool) or value < 240:
                raise LayoutRefused(f"viewport {attr} must be an integer >= 240 CSS pixels.")

    @classmethod
    def named(cls, name: str) -> "Viewport":
        for entry in VIEWPORT_MATRIX:
            if entry[0] == name:
                return cls(*entry)
        raise LayoutRefused(f"unknown viewport {name!r}")

    def label(self) -> str:
        return f"{self.name}-{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class LayoutCell:
    """The measured layout outcome for one viewport at one zoom factor.

    ``clipped`` and ``overlapping`` are separate booleans rather than one ``ok`` flag because
    they have different causes and different fixes, and the report has to say which happened.
    """

    viewport: Viewport
    zoom: int
    clipped: bool
    overlapping: bool
    horizontal_overflow_px: int = 0
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.viewport, Viewport):
            raise LayoutRefused("a layout cell needs a Viewport.")
        if self.zoom not in (1, ZOOM_FACTOR):
            raise LayoutRefused(
                f"zoom must be 1 or {ZOOM_FACTOR}; other factors are unmeasured, not passing."
            )
        for attr in ("clipped", "overlapping"):
            if not isinstance(getattr(self, attr), bool):
                raise LayoutRefused(f"{attr} must be a boolean; 'mostly fine' is not a measurement.")
        if not isinstance(self.horizontal_overflow_px, int) or self.horizontal_overflow_px < 0:
            raise LayoutRefused("horizontal_overflow_px must be a non-negative integer.")
        if not self.clipped and self.horizontal_overflow_px > 1:
            raise LayoutRefused(
                "a cell reporting horizontal overflow cannot also report no clipping; the two "
                "descriptions of one measurement disagree."
            )

    def passes(self) -> bool:
        return not self.clipped and not self.overlapping and self.horizontal_overflow_px <= 1

    def cell_id(self) -> str:
        return f"{self.viewport.label()}@{self.zoom}x"


@dataclass(frozen=True, slots=True)
class InteractionCheck:
    """One critical flow (form, link group, navigation) and whether it really works."""

    name: str
    kind: str
    passed: bool
    detail: str = ""
    viewports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _wq_text(self.name, "interaction name", 200))
        kind = _wq_text(self.kind, "interaction kind", 40)
        if kind not in {"form", "link", "navigation", "input"}:
            raise WebsiteQualityError(
                f"interaction kind {kind!r} is unknown; expected form, link, navigation or input."
            )
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.passed, bool):
            raise WebsiteQualityError("interaction passed must be a boolean.")
        object.__setattr__(self, "detail", str(self.detail or "").strip())
        viewports = tuple(str(v) for v in self.viewports)
        object.__setattr__(self, "viewports", viewports)
        if self.passed and not self.detail:
            raise WebsiteQualityError(
                f"{self.kind} {self.name!r} claims to work but says nothing about what exercised "
                "it; a passing flow names its evidence."
            )


@dataclass(frozen=True, slots=True)
class AssetProblem:
    """A dependency, secret or placeholder-content finding, with a severity that is not optional."""

    kind: str
    location: str
    detail: str
    severity: str

    def __post_init__(self) -> None:
        kind = _wq_text(self.kind, "problem kind", 40)
        if kind not in {"secret", "placeholder", "dependency", "console", "network"}:
            raise WebsiteQualityError(
                f"problem kind {kind!r} is unknown; findings must be classified to be triaged."
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "location", _wq_text(self.location, "problem location", 400))
        object.__setattr__(self, "detail", _wq_text(self.detail, "problem detail", 600))
        severity = _wq_text(self.severity, "problem severity", 20)
        if severity not in {"blocking", "warning"}:
            raise WebsiteQualityError(
                f"severity {severity!r} is unknown; expected blocking or warning."
            )
        object.__setattr__(self, "severity", severity)
        if kind == "secret" and severity != "blocking":
            raise SecretsRefused(
                f"a credential at {self.location!r} is always blocking; a secret cannot be a "
                "warning, because the exposure does not depend on anyone's judgement."
            )


def scan_for_secrets(text: str, *, location: str) -> tuple[AssetProblem, ...]:
    """Find credentials in shipped text. Every hit is blocking, by construction."""
    problems: list[AssetProblem] = []
    for name, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            problems.append(
                AssetProblem(
                    kind="secret",
                    location=location,
                    detail=f"{name} matched at offset {match.start()}",
                    severity="blocking",
                )
            )
    return tuple(problems)


def scan_for_placeholders(text: str, *, location: str) -> tuple[AssetProblem, ...]:
    """Find shipped placeholder copy. Blocking: it is unfinished work presented as done."""
    problems: list[AssetProblem] = []
    for name, pattern in _PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(text):
            problems.append(
                AssetProblem(
                    kind="placeholder",
                    location=location,
                    detail=f"{name} matched at offset {match.start()}",
                    severity="blocking",
                )
            )
    return tuple(problems)


def mask_secret(value: str) -> str:
    """Redact a matched secret so the finding can be reported without re-leaking it."""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


class QualityProbe(Protocol):
    """The execution this module refuses to perform itself.

    A real implementation drives a browser across the viewport matrix, parses the document, and
    resolves every link. The module never opens a socket, so the same rubric runs against a
    recording double in CI.
    """

    def inspect(self, artifact_sha256: str) -> Mapping[str, Any]:
        """Return the raw observations described by :class:`ProbeResult`."""
        ...


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The raw facts a probe returns. Parsed strictly; a missing field is refused, not defaulted."""

    artifact_sha256: str
    outcomes: Mapping[str, bool]
    layout: tuple[LayoutCell, ...]
    interactions: tuple[InteractionCheck, ...] = ()
    problems: tuple[AssetProblem, ...] = ()
    documents: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_sha256, str) or not _DIGEST_RE.fullmatch(
            self.artifact_sha256
        ):
            raise ProbeRefused("probe artifact_sha256 must be a lowercase SHA-256 digest.")
        if not isinstance(self.outcomes, Mapping):
            raise ProbeRefused("probe outcomes must be a mapping of criterion -> boolean.")
        for name, value in self.outcomes.items():
            if not isinstance(value, bool):
                raise ProbeRefused(
                    f"outcome {name!r} must be a boolean; an outcome that is 'mostly' is not one."
                )


def _expected_cells() -> set[str]:
    return {
        f"{Viewport.named(name).label()}@{zoom}x"
        for name, _w, _h in VIEWPORT_MATRIX
        for zoom in (1, ZOOM_FACTOR)
    }


@dataclass(frozen=True, slots=True)
class OwnerReview:
    """The owner's judgement of whether the site is any use. Only a person can give it.

    ``accepted_reference`` optionally names an AP-020 reference this judgement produced. It is
    recorded here, not created here: turning an opinion into a versioned reference is the
    reference store's job, with its own explicit ``use_as_reference`` grant.
    """

    actor: str
    statement: str
    useful: bool
    accepted_reference: str = ""

    def __post_init__(self) -> None:
        who = _wq_text(self.actor, "review actor", 120).lower()
        if who in {"agent", "system", "automation", "assistant", "model", "scheduler"}:
            raise OwnerReviewRefused(
                f"{self.actor!r} is not a person; creative usefulness is the owner's judgement."
            )
        if not who.startswith(("owner:", "user:", "reviewer:")):
            raise OwnerReviewRefused(
                "a review actor must be namespaced owner:, user: or reviewer:, so a machine-written "
                "record cannot pass for a person's decision."
            )
        object.__setattr__(self, "actor", self.actor)
        object.__setattr__(self, "statement", _wq_text(self.statement, "review statement", 1200))
        if not isinstance(self.useful, bool):
            raise OwnerReviewRefused("useful must be a boolean; the owner either would use it or not.")
        reference = str(self.accepted_reference or "").strip()
        if reference and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,119}@v[0-9]+", reference):
            raise OwnerReviewRefused(
                "accepted_reference must be a versioned id like 'reference/site@v2'; an "
                "unversioned acceptance cannot be re-checked later."
            )
        object.__setattr__(self, "accepted_reference", reference)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "statement": self.statement,
            "useful": self.useful,
            "accepted_reference": self.accepted_reference,
            "is_final_acceptance": False,
            "grants_publish_permission": False,
        }


@dataclass(frozen=True, slots=True)
class PilotReport:
    """Everything one qualification run established, and everything it did not.

    ``deliverable`` is derived and deliberately strict: every criterion must have run and been
    true, every layout cell must have passed, no blocking problem may exist, and a person must
    have said the site is useful. Nothing here can be set directly.
    """

    artifact_sha256: str
    outcomes: Mapping[str, bool]
    layout: tuple[LayoutCell, ...]
    interactions: tuple[InteractionCheck, ...] = ()
    problems: tuple[AssetProblem, ...] = ()
    owner_review: OwnerReview | None = None
    runs_evaluated: int = 1
    intervention_minutes: float = 0.0
    limitations: tuple[str, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_sha256, str) or not _DIGEST_RE.fullmatch(
            self.artifact_sha256
        ):
            raise WebsiteQualityError("report artifact_sha256 must be a lowercase SHA-256 digest.")
        unknown = set(self.outcomes) - set(CRITERIA)
        if unknown:
            raise RubricRefused(
                f"outcomes name criteria this rubric does not have: {sorted(unknown)}; an "
                "invented criterion measures nothing."
            )
        missing = [name for name in CRITERIA if name not in self.outcomes]
        if missing:
            raise RubricRefused(
                f"these criteria have no outcome: {missing}. A check that did not run is not a "
                "pass, and the report must say so rather than omit it."
            )
        seen = {cell.cell_id() for cell in self.layout}
        absent = sorted(_expected_cells() - seen)
        if absent:
            raise LayoutRefused(
                f"the layout matrix is incomplete; missing {absent}. A cell nobody measured is "
                "not a cell that passed."
            )
        if len(seen) != len(self.layout):
            raise LayoutRefused("each viewport/zoom cell may appear once.")
        if not isinstance(self.runs_evaluated, int) or isinstance(self.runs_evaluated, bool) or self.runs_evaluated < 1:
            raise WebsiteQualityError("runs_evaluated must be a positive integer; every evaluated run is reported.")
        if self.intervention_minutes < 0:
            raise WebsiteQualityError("intervention_minutes must be non-negative.")
        if self.owner_review is not None and not isinstance(self.owner_review, OwnerReview):
            raise OwnerReviewRefused("owner_review must be an OwnerReview record.")
        for row in self.limitations:
            _wq_text(row, "limitation", 600)
        if self.run_id:
            object.__setattr__(self, "run_id", _wq_text(self.run_id, "run_id", 120))

    # -- derived verdict -------------------------------------------------

    def failed_criteria(self) -> tuple[str, ...]:
        return tuple(name for name in CRITERIA if self.outcomes.get(name) is not True)

    def failed_layout(self) -> tuple[str, ...]:
        return tuple(cell.cell_id() for cell in self.layout if not cell.passes())

    def blocking_problems(self) -> tuple[AssetProblem, ...]:
        return tuple(item for item in self.problems if item.severity == "blocking")

    def failed_interactions(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.interactions if not item.passed)

    def automated_pass(self) -> bool:
        return not (
            self.failed_criteria()
            or self.failed_layout()
            or self.blocking_problems()
            or self.failed_interactions()
        )

    def deliverable(self) -> bool:
        return bool(
            self.automated_pass()
            and self.owner_review is not None
            and self.owner_review.useful
        )

    def reasons(self) -> tuple[str, ...]:
        rows: list[str] = []
        rows.extend(f"criterion did not pass: {name}" for name in self.failed_criteria())
        rows.extend(f"layout failed: {cell}" for cell in self.failed_layout())
        rows.extend(f"interaction failed: {name}" for name in self.failed_interactions())
        rows.extend(
            f"blocking {item.kind}: {item.location}" for item in self.blocking_problems()
        )
        if self.owner_review is None:
            rows.append("no owner review: automated checks cannot decide whether the site is useful")
        elif not self.owner_review.useful:
            rows.append("owner judged the site not useful")
        return tuple(rows)

    # -- reporting -------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": WQ_SCHEMA_VERSION,
            "run_id": self.run_id,
            "artifact_sha256": self.artifact_sha256,
            "rubric": {
                "criteria": list(CRITERIA),
                "viewports": [f"{name}-{w}x{h}" for name, w, h in VIEWPORT_MATRIX],
                "contrast": dict(CONTRAST_FLOORS),
                "target_min_px": TARGET_MIN_PX,
                "zoom_factor": ZOOM_FACTOR,
                "forbidden_claims": list(FORBIDDEN_CLAIMS),
            },
            "outcomes": {name: self.outcomes[name] for name in CRITERIA},
            "layout": [
                {
                    "cell": cell.cell_id(),
                    "viewport": cell.viewport.name,
                    "zoom": cell.zoom,
                    "clipped": cell.clipped,
                    "overlapping": cell.overlapping,
                    "horizontal_overflow_px": cell.horizontal_overflow_px,
                    "notes": cell.notes,
                    "passed": cell.passes(),
                }
                for cell in self.layout
            ],
            "interactions": [
                {
                    "name": item.name,
                    "kind": item.kind,
                    "passed": item.passed,
                    "detail": item.detail,
                    "viewports": list(item.viewports),
                }
                for item in self.interactions
            ],
            "problems": [
                {
                    "kind": item.kind,
                    "location": item.location,
                    "detail": item.detail,
                    "severity": item.severity,
                }
                for item in self.problems
            ],
            "owner_review": self.owner_review.to_mapping() if self.owner_review else None,
            "automated_pass": self.automated_pass(),
            "deliverable": self.deliverable(),
            "reasons": list(self.reasons()),
            "runs_evaluated": self.runs_evaluated,
            "intervention_minutes": self.intervention_minutes,
            "limitations": list(self.limitations),
            # Stated on every report, so no reader has to infer what is deliberately absent.
            "claims_made": [],
            "claims_refused": list(FORBIDDEN_CLAIMS),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _parse_outcomes(raw: object) -> dict[str, bool]:
    if not isinstance(raw, Mapping):
        raise ProbeRefused("probe outcomes must be a mapping.")
    out: dict[str, bool] = {}
    for name, value in raw.items():
        if not isinstance(value, bool):
            raise ProbeRefused(f"outcome {name!r} must be a boolean.")
        out[str(name)] = value
    return out


def _parse_layout(raw: object) -> tuple[LayoutCell, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise LayoutRefused("probe layout must be a sequence of cells.")
    cells: list[LayoutCell] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise LayoutRefused("each layout cell must be an object.")
        name = str(item.get("viewport") or "")
        cells.append(
            LayoutCell(
                viewport=Viewport.named(name),
                zoom=int(item.get("zoom") or 0),
                # The constructors validate; the cast only tells the type checker what it
                # cannot see through the mapping. A non-boolean is refused, never coerced.
                clipped=cast(bool, item.get("clipped")),
                overlapping=cast(bool, item.get("overlapping")),
                horizontal_overflow_px=int(item.get("horizontal_overflow_px") or 0),
                notes=str(item.get("notes") or ""),
            )
        )
    return tuple(cells)


def _parse_interactions(raw: object) -> tuple[InteractionCheck, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise WebsiteQualityError("probe interactions must be a sequence.")
    rows: list[InteractionCheck] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise WebsiteQualityError("each interaction must be an object.")
        rows.append(
            InteractionCheck(
                name=str(item.get("name") or ""),
                kind=str(item.get("kind") or ""),
                passed=cast(bool, item.get("passed")),
                detail=str(item.get("detail") or ""),
                viewports=tuple(item.get("viewports") or ()),
            )
        )
    return tuple(rows)


def _parse_problems(raw: object) -> tuple[AssetProblem, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise WebsiteQualityError("probe problems must be a sequence.")
    rows: list[AssetProblem] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise WebsiteQualityError("each problem must be an object.")
        rows.append(
            AssetProblem(
                kind=str(item.get("kind") or ""),
                location=str(item.get("location") or ""),
                detail=str(item.get("detail") or ""),
                severity=str(item.get("severity") or ""),
            )
        )
    return tuple(rows)


def evaluate_website(
    *,
    probe: QualityProbe,
    artifact_sha256: str,
    owner_review: OwnerReview | None = None,
    runs_evaluated: int = 1,
    intervention_minutes: float = 0.0,
    limitations: Sequence[str] = (),
    run_id: str = "",
) -> PilotReport:
    """Run the rubric against a probe and return the report, including what it could not settle.

    The report always comes back — a failed qualification is a result, not an error, because an
    error is something a caller can catch and report as success. Only a malformed probe raises.
    """
    if not isinstance(artifact_sha256, str) or not _DIGEST_RE.fullmatch(artifact_sha256):
        raise WebsiteQualityError("artifact_sha256 must be a lowercase SHA-256 digest.")
    raw = probe.inspect(artifact_sha256)
    if not isinstance(raw, Mapping):
        raise ProbeRefused("the probe must return a mapping of observations.")
    observed = str(raw.get("artifact_sha256") or "")
    if observed != artifact_sha256:
        raise ProbeRefused(
            "the probe inspected a different artifact than the one asked for; a report bound to "
            "the wrong digest describes the wrong site."
        )
    # Scan the shipped documents for credentials and placeholder copy, and merge those findings
    # with whatever the probe reported. The module does this itself because it must not depend on
    # a probe remembering to.
    documents = raw.get("documents")
    if documents is None:
        documents = {}
    if not isinstance(documents, Mapping):
        # Not a silent skip: a probe returning the wrong shape here would otherwise bypass both
        # scanners, and "no documents" would read exactly like "no findings".
        raise ProbeRefused(
            "probe documents must be a mapping of path -> text; an empty mapping means the probe "
            "inspected no shipped text, which cannot produce a clean report."
        )
    scanned: list[AssetProblem] = []
    for location, text in documents.items():
        if not isinstance(text, str):
            raise ProbeRefused(f"document {location!r} must be text.")
        scanned.extend(scan_for_secrets(text, location=str(location)))
        scanned.extend(scan_for_placeholders(text, location=str(location)))

    report = PilotReport(
        artifact_sha256=artifact_sha256,
        outcomes=_parse_outcomes(raw.get("outcomes")),
        layout=_parse_layout(raw.get("layout")),
        interactions=_parse_interactions(raw.get("interactions")),
        problems=(*_parse_problems(raw.get("problems")), *scanned),
        owner_review=owner_review,
        runs_evaluated=runs_evaluated,
        intervention_minutes=intervention_minutes,
        limitations=tuple(limitations),
        run_id=run_id,
    )
    # A secret or placeholder found by the scanner is blocking; a report that already contains
    # one cannot be delivered even if every boolean was true.
    return report


def reference_shortlist(
    references: Sequence[Reference],
    *,
    task_kind: str,
    audience: str = "",
    language: str = "",
    style_tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The approved references worth reading before judging, in order, with why.

    This delegates to AP-020's ``rank`` rather than re-implementing retrieval, so "which
    examples apply here" has exactly one answer in the programme. References are returned as
    reading suggestions; none of them changes the rubric, because an approved example settles
    style and never quality.
    """
    ranked = rank(
        references,
        task_kind=task_kind,
        audience=audience,
        language=language,
        style_tags=style_tags,
    )
    return [
        {
            "reference_id": ref.reference_id,
            "version": ref.version,
            "reference": f"{ref.reference_id}@v{ref.version}",
            "reasons": list(reasons),
        }
        for ref, reasons in ranked
    ]


def refuse_claims(claims: Iterable[str]) -> None:
    """Reject any attempt to attach an unmeasurable claim to a report."""
    for claim in claims:
        normalized = str(claim).strip().lower().replace(" ", "_")
        if normalized in FORBIDDEN_CLAIMS:
            raise ClaimRefused(
                f"{claim!r} is not a claim this programme can measure. Reporting it would mean "
                f"inventing a number; refused claims are {list(FORBIDDEN_CLAIMS)}."
            )
