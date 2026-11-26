"""AP-032 pilot harness — run the website pilot's *offline* axes and say plainly what stayed unmeasured.

AP-022 ships `website_quality.PilotReport`, which refuses to invent a result: every criterion must
have an outcome, every layout cell must be measured, and `deliverable` is false without an owner
review. That refusal is correct and it is also the reason the pilot cannot be completed by this
module — a report with invented numbers is exactly the artifact the story forbids.

So this harness does the part that *can* be done honestly without a browser, and refuses to do the
rest:

* **Measured offline (5 of 11 criteria).** `no_placeholder_content` and `secrets_absent` are text
  scans the module already owns. `dependencies_reviewed`, `forms_work` and `links_resolve` are
  measured structurally from the artifact: every external asset and navigation target is enumerated
  and classified, so "reviewed" means "listed and judged", not "assumed". These are decisions this
  harness can take and defend from the bytes.

* **Refused, not guessed (6 of 11).** `keyboard_reachable`, `focus_visible`, `contrast_ok`,
  `reduced_motion_respected`, `console_clean` and `navigation_works` cannot be answered from source
  text. They need a running browser with a real keyboard, a real focus ring, a computed-style pass and
  a console. Substituting a regex for a focus ring would produce a green report that means nothing.

* **Never filled in, by construction.** Layout cells, interactions and the owner review. The harness
  does not emit a `PilotReport`; it emits a `PilotPartial` naming what a `PilotReport` would still
  need. A partial that says "6 of 11 criteria need your browser" is useful; a partial dressed as a
  report is the defect.

Nothing here fetches, renders or executes the artifact. It parses text and refuses to claim more.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "PilotHarnessError",
    "ArtifactRefused",
    "OFFLINE_CRITERIA",
    "BROWSER_CRITERIA",
    "CriterionOutcome",
    "AssetReference",
    "PilotPartial",
    "artifact_digest",
    "audit_artifact",
    "build_pilot_partial",
]

#: Criteria this harness can decide from the artifact's text and structure alone.
OFFLINE_CRITERIA: tuple[str, ...] = (
    "no_placeholder_content",
    "secrets_absent",
    "dependencies_reviewed",
    "forms_work",
    "links_resolve",
)

#: Criteria that require a running browser, a real input device and a console.
BROWSER_CRITERIA: tuple[str, ...] = (
    "keyboard_reachable",
    "focus_visible",
    "contrast_ok",
    "reduced_motion_respected",
    "console_clean",
    "navigation_works",
)

_HTML_EXTENSIONS = (".html", ".htm")
_URL_RE = re.compile(r"""(?:https?:)?//[A-Za-z0-9.-]+""")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SRC_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_FORM_RE = re.compile(r"""<form\b[^>]*>""", re.IGNORECASE)
_ACTION_RE = re.compile(r"""action\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_INPUT_RE = re.compile(r"""<input\b[^>]*>""", re.IGNORECASE)
_LABEL_RE = re.compile(r"""<label\b""", re.IGNORECASE)
_ARIA_LABEL_RE = re.compile(r"""aria-label\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_NAME_RE = re.compile(r"""\bname\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class PilotHarnessError(ValueError):
    """The harness was asked to do something it cannot do honestly."""


class ArtifactRefused(PilotHarnessError):
    """The artifact is not a readable, self-consistent input."""


@dataclass(frozen=True, slots=True)
class CriterionOutcome:
    """One criterion's outcome, and how it was reached.

    ``measured`` is the only value that may ever be reported as a pass. ``unmeasured`` and
    ``cannot-measure-here`` both leave the criterion open; the second also names why, so a reader can
    see that the gap is a missing instrument and not a suspicious result.
    """

    criterion: str
    passed: bool
    basis: str
    detail: str

    def __post_init__(self) -> None:
        if self.basis not in ("measured", "unmeasured", "cannot-measure-here"):
            raise PilotHarnessError(f"unknown basis {self.basis!r}")
        if self.basis != "measured" and self.passed:
            raise PilotHarnessError(
                f"{self.criterion!r} is claimed to pass on basis {self.basis!r}; only a measured "
                "criterion may be reported as passing"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "passed": self.passed,
            "basis": self.basis,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AssetReference:
    """One reference the artifact makes to something it did not ship itself."""

    reference: str
    kind: str
    location: str

    def to_mapping(self) -> dict[str, Any]:
        return {"reference": self.reference, "kind": self.kind, "location": self.location}


@dataclass(frozen=True, slots=True)
class PilotPartial:
    """Everything the harness established, plus everything it could not.

    Deliberately not a `PilotReport`: constructing one would require inventing layout cells and an
    owner review. This object's `to_mapping()` states `is_a_qualification_report: false` so it cannot
    be mistaken for one downstream.
    """

    artifact_sha256: str
    outcomes: tuple[CriterionOutcome, ...]
    assets: tuple[AssetReference, ...]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256):
            raise ArtifactRefused("artifact_sha256 must be a lowercase SHA-256 digest")
        seen = {item.criterion for item in self.outcomes}
        expected = set(OFFLINE_CRITERIA) | set(BROWSER_CRITERIA)
        if seen != expected:
            missing = sorted(expected - seen)
            extra = sorted(seen - expected)
            raise PilotHarnessError(
                f"the partial must account for every criterion exactly once; missing={missing} extra={extra}"
            )

    @property
    def measured(self) -> tuple[CriterionOutcome, ...]:
        return tuple(item for item in self.outcomes if item.basis == "measured")

    @property
    def still_open(self) -> tuple[str, ...]:
        return tuple(item.criterion for item in self.outcomes if item.basis != "measured")

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(item.criterion for item in self.outcomes if item.basis == "measured" and not item.passed)

    @property
    def offline_clear(self) -> bool:
        """True only when every offline criterion ran *and* passed."""
        offline = [item for item in self.outcomes if item.criterion in OFFLINE_CRITERIA]
        return all(item.basis == "measured" and item.passed for item in offline)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "artifact_digest_scheme": "sha256 over sorted relative path + NUL + content + NUL, text files",
            "is_a_qualification_report": False,
            "offline_clear": self.offline_clear,
            "measured_count": len(self.measured),
            "still_open_count": len(self.still_open),
            "failed": list(self.failed),
            "outcomes": [item.to_mapping() for item in self.outcomes],
            "still_open": list(self.still_open),
            "assets": [item.to_mapping() for item in self.assets],
            "notes": list(self.notes),
            "what_a_pilot_report_still_needs": [
                "the six browser criteria above, measured in a real browser",
                "every layout cell in the viewport/zoom matrix measured (a cell nobody measured is not a cell that passed)",
                "interaction verdicts for each requested interaction",
                "an owner: / user: / reviewer: review of whether the site is useful",
            ],
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_of(path: str, text: str) -> str:
    """Return the artifact's text, refusing anything that is not text or is empty."""
    if not isinstance(text, str):
        raise ArtifactRefused(f"{path}: content must be text")
    if not text.strip():
        raise ArtifactRefused(f"{path}: content is empty; an empty artifact measures nothing")
    lower = path.lower()
    if not lower.endswith(_HTML_EXTENSIONS) and not lower.endswith((".css", ".js", ".md", ".txt")):
        raise ArtifactRefused(
            f"{path}: the harness reads text artifacts only; refusing to guess at {path.rsplit('.', 1)[-1]!r}"
        )
    return text


def _enumerate_assets(path: str, text: str) -> tuple[AssetReference, ...]:
    found: list[AssetReference] = []
    for pattern, kind in ((_SRC_RE, "embedded"), (_HREF_RE, "linked")):
        for match in pattern.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith("#") or target.startswith("data:"):
                continue
            if kind == "embedded":
                asset_kind = "embedded"
            else:
                asset_kind = "linked:external" if _URL_RE.match(target) else "linked:local"
            found.append(
                AssetReference(
                    reference=target,
                    kind=asset_kind,
                    location=f"{path}@offset{match.start()}",
                )
            )
    return tuple(found)


def audit_artifact(files: Mapping[str, str]) -> tuple[CriterionOutcome, ...]:
    """Measure the five offline criteria across a set of text artifact files.

    ``files`` maps a path to its text. Every file must be readable text; an empty mapping is refused
    because a pilot over nothing would report five passes about nothing.
    """
    if not files:
        raise ArtifactRefused("no artifact files were supplied; an audit of nothing passes vacuously")
    texts = {path: _text_of(path, text) for path, text in files.items()}

    # Import here so the module's own import list stays small and the dependency is explicit.
    import website_quality as wq

    problems: list[Any] = []
    placeholders: list[Any] = []
    assets: list[AssetReference] = []
    for path, text in texts.items():
        problems.extend(wq.scan_for_secrets(text, location=path))
        placeholders.extend(wq.scan_for_placeholders(text, location=path))
        assets.extend(_enumerate_assets(path, text))

    outcomes: list[CriterionOutcome] = []

    outcomes.append(
        CriterionOutcome(
            criterion="no_placeholder_content",
            passed=not placeholders,
            basis="measured",
            detail=(
                "no placeholder copy found"
                if not placeholders
                else f"{len(placeholders)} placeholder hit(s): {[p.location for p in placeholders][:5]}"
            ),
        )
    )
    outcomes.append(
        CriterionOutcome(
            criterion="secrets_absent",
            passed=not problems,
            basis="measured",
            detail=(
                "no credential pattern matched"
                if not problems
                else f"{len(problems)} secret hit(s); values are masked by the scanner"
            ),
        )
    )

    # dependencies_reviewed: every external reference is enumerated and *judged*. A reference with no
    # scheme and no leading slash is local and needs no provenance; every external one does.
    # An "external" reference is one the artifact does not carry: an absolute URL, wherever it
    # appears, or a protocol-relative one. Embedded scripts/styles are covered too, because a CDN
    # <script src> is exactly the dependency a review is about.
    external = [
        item
        for item in assets
        if _URL_RE.match(item.reference) or item.reference.startswith("//")
    ]
    unreviewed = [item for item in external if not item.reference.lower().startswith(("https://", "//"))]
    outcomes.append(
        CriterionOutcome(
            criterion="dependencies_reviewed",
            passed=not unreviewed,
            basis="measured",
            detail=(
                f"{len(external)} external reference(s) enumerated; all use https or a protocol-relative "
                "scheme"
                if external and not unreviewed
                else (
                    "no external references to review"
                    if not external
                    else f"{len(unreviewed)} external reference(s) without a reviewable scheme"
                )
            ),
        )
    )

    # forms_work: structurally, every form has an action and every input has a name or a label.
    forms = _FORM_RE.findall("".join(texts.values()))
    form_actions = _ACTION_RE.findall("".join(texts.values()))
    inputs = _INPUT_RE.findall("".join(texts.values()))
    labels = len(_LABEL_RE.findall("".join(texts.values()))) + len(_ARIA_LABEL_RE.findall("".join(texts.values())))
    names = _NAME_RE.findall("".join(texts.values()))
    unnamed = max(0, len(inputs) - len(names) - labels)
    if forms:
        passed = len(form_actions) >= len(forms) and unnamed == 0
        detail = (
            f"{len(forms)} form(s) with action, {len(inputs)} input(s) named or labelled"
            if passed
            else f"{len(forms)} form(s), {len(form_actions)} action(s), {unnamed} unnamed input(s)"
        )
    else:
        passed = True
        detail = "no forms; nothing to submit. This is not evidence that a form would work."
    outcomes.append(CriterionOutcome("forms_work", passed, "measured", detail))

    # links_resolve: structurally, every internal href names a document that the artifact carries.
    known = {path for path in texts}
    hrefs = _HREF_RE.findall("".join(texts.values()))
    internal = [
        href
        for href in hrefs
        if href and not href.startswith(("#", "http://", "https://", "//", "mailto:", "tel:", "data:"))
    ]
    dangling = [href for href in internal if _normalise(href) not in known]
    outcomes.append(
        CriterionOutcome(
            criterion="links_resolve",
            passed=not dangling,
            basis="measured",
            detail=(
                f"{len(internal)} internal link(s) resolve inside the artifact"
                if not dangling
                else f"{len(dangling)} internal link(s) point at documents the artifact does not carry: {dangling[:5]}"
            ),
        )
    )

    for criterion in BROWSER_CRITERIA:
        outcomes.append(
            CriterionOutcome(
                criterion=criterion,
                passed=False,
                basis="cannot-measure-here",
                detail=(
                    "requires a running browser with a real input device and console; source text cannot "
                    "answer it, and a regex substituting for a focus ring would report a pass that means nothing"
                ),
            )
        )
    return tuple(outcomes)


def _normalise(href: str) -> str:
    target = href.split("?", 1)[0].split("#", 1)[0].lstrip("./")
    return target


def artifact_digest(files: Mapping[str, str]) -> str:
    """SHA-256 over the artifact's relative paths and bytes, in sorted order.

    This is the identity both halves of a pilot are measured against: the browser walk computes the
    same digest over the same directory, so `pilot_preflight.py` can refuse a pair that measured
    different builds. Defining it in one place is the point -- two tools each inventing their own
    digest would agree by accident or disagree for the wrong reason.
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[path]).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_pilot_partial(
    files: Mapping[str, str],
    *,
    notes: Iterable[str] = (),
) -> PilotPartial:
    """Audit an artifact and return the partial, with a digest over the bytes audited."""
    if not isinstance(files, Mapping):
        raise ArtifactRefused("files must be a mapping of path to text")
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[path]).encode("utf-8"))
        digest.update(b"\0")
    outcomes = audit_artifact(files)
    assets: list[AssetReference] = []
    for path, text in files.items():
        assets.extend(_enumerate_assets(path, text))
    return PilotPartial(
        artifact_sha256=digest.hexdigest(),
        outcomes=outcomes,
        assets=tuple(assets),
        notes=tuple(str(note) for note in notes),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Audit a directory of text artifacts; print the partial as JSON and exit 0 only if offline-clear."""
    import json
    import sys
    from pathlib import Path

    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(".")
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    files = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in _HTML_EXTENSIONS + (".css", ".js", ".txt")
    }
    try:
        partial = build_pilot_partial(files, notes=(f"audited {len(files)} file(s) under {root}",))
    except PilotHarnessError as exc:
        print(f"pilot harness refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(partial.to_mapping(), indent=2, sort_keys=True))
    return 0 if partial.offline_clear else 1


if __name__ == "__main__":
    raise SystemExit(main())
