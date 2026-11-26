"""AP-032 pilot preflight — assemble the two partials into the one thing a reviewer can act on.

`pilot_harness.py` measures 5 of the 11 pilot criteria offline. `scripts/pilot_browser_walk.mjs`
measures the other 6 in a browser. Each emits a partial that says `is_a_qualification_report: false`.
Neither is a pilot, and running both is still not a pilot — but running both against *the same bytes*
and refusing to combine them when the bytes differ is the part that can be done before a head exists,
and it is the part a reviewer would otherwise have to check by hand.

That is all this module is:

* **It refuses a mismatched pair.** Both partials name the artifact they measured. If those digests
  differ, the browser file and the offline file describe different builds and combining them would
  invent a result for a build nobody measured. `PreflightRefused` says which side drifted.
* **It refuses an incomplete pair.** Eleven criteria are expected, and the six the walk owns must be
  `basis: "measured"`. A walk that never ran is not a pass, so a missing browser partial is a refusal,
  not an empty section.
* **It does not decide anything.** `ready_for_owner_review` is true only when the *machine* work is
  done; that is a statement about the state of the evidence, not about the artifact. The output says
  which criteria are measured, which failed, and what a human still has to do — it never says "ship".

What a human still owns after a clean preflight: the artifact review itself, the layout and
interaction measurements `website_quality.PilotReport` requires, and the two independent approvals.
This module cannot produce any of them and is built so it cannot appear to have tried.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pilot_harness import BROWSER_CRITERIA, OFFLINE_CRITERIA

__all__ = [
    "PreflightError",
    "PreflightRefused",
    "PilotPreflight",
    "EXPECTED_CRITERIA",
    "build_preflight",
    "read_partial",
]

EXPECTED_CRITERIA: tuple[str, ...] = OFFLINE_CRITERIA + BROWSER_CRITERIA

#: Who owns each criterion's measurement. Recorded so a reader can see which tool to blame when one
#: fails, and so a criterion appearing in the wrong partial is caught rather than merged.
OWNERS: Mapping[str, str] = {
    **{criterion: "pilot_harness.py" for criterion in OFFLINE_CRITERIA},
    **{criterion: "pilot_browser_walk.mjs" for criterion in BROWSER_CRITERIA},
}

_WHAT_A_HUMAN_STILL_OWES = (
    "review the artifact yourself and decide whether it is worth shipping",
    "supply the PilotReport layout and interaction measurements",
    "obtain the independent security approval",
    "obtain the independent screenshot approval",
)


class PreflightError(ValueError):
    """The preflight was built from something that is not a pilot partial."""


class PreflightRefused(PreflightError):
    """The pair cannot be combined without inventing a result."""


@dataclass(frozen=True)
class PilotPreflight:
    """Both partials, proven to describe the same bytes, with nothing decided."""

    artifact_sha256: str
    offline: Mapping[str, Any]
    browser: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_sha256, str) or len(self.artifact_sha256) != 64:
            raise PreflightError("artifact_sha256 must be a 64-character digest")
        for label, partial in (("offline", self.offline), ("browser", self.browser)):
            if not isinstance(partial, Mapping):
                raise PreflightError(f"the {label} partial must be a mapping")
            if partial.get("is_a_qualification_report") is not False:
                raise PreflightRefused(
                    f"the {label} partial is dressed as a report; a preflight is not a pilot and must not "
                    "inherit one"
                )

    @property
    def criteria(self) -> tuple[dict[str, Any], ...]:
        """One row per criterion, in the canonical order, with the owner and the verdict."""
        rows: list[dict[str, Any]] = []
        offline_rows = {str(row["criterion"]): row for row in self.offline.get("criteria", self.offline.get("outcomes", []))}
        browser_rows = {str(row["criterion"]): row for row in self.browser.get("criteria", [])}
        for criterion in EXPECTED_CRITERIA:
            source = offline_rows if criterion in OFFLINE_CRITERIA else browser_rows
            row = source.get(criterion)
            if row is None:
                # A criterion neither partial answers is a hole, not a pass.
                rows.append(
                    {
                        "criterion": criterion,
                        "owner": OWNERS[criterion],
                        "measured": False,
                        "passed": False,
                        "detail": "neither partial reported this criterion; it is not a pass",
                    }
                )
                continue
            basis = str(row.get("basis", "measured"))
            rows.append(
                {
                    "criterion": criterion,
                    "owner": OWNERS[criterion],
                    "measured": basis == "measured",
                    "passed": bool(row.get("passed")),
                    "detail": str(row.get("detail", "")),
                }
            )
        return tuple(rows)

    @property
    def measured(self) -> tuple[str, ...]:
        return tuple(row["criterion"] for row in self.criteria if row["measured"])

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(row["criterion"] for row in self.criteria if not row["measured"])

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(row["criterion"] for row in self.criteria if row["measured"] and not row["passed"])

    @property
    def ready_for_owner_review(self) -> bool:
        """True when every criterion was measured and none failed.

        This says the machine work is finished. It says nothing about the artifact, which is the
        distinction the whole AP-032 pack exists to keep.
        """
        return len(self.measured) == len(EXPECTED_CRITERIA) and not self.failed

    def to_mapping(self) -> dict[str, Any]:
        return {
            "is_a_qualification_report": False,
            "artifact_sha256": self.artifact_sha256,
            "criteria": list(self.criteria),
            "measured": list(self.measured),
            "unmeasured": list(self.unmeasured),
            "failed": list(self.failed),
            "ready_for_owner_review": self.ready_for_owner_review,
            "what_a_human_still_owes": list(_WHAT_A_HUMAN_STILL_OWES),
            "note": (
                "Both partials measured the same artifact digest. Ready for owner review means the "
                "machine-measurable criteria are done, not that the artifact is qualified: the review "
                "and the independent approvals are still outstanding and cannot be produced here."
            ),
        }


def _load(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PreflightRefused(f"no {label} partial at {path}; run its tool before assembling a preflight")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"the {label} partial at {path} is not readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PreflightError(f"the {label} partial at {path} is not a JSON object")
    return payload


def read_partial(path: Path, *, label: str = "pilot") -> dict[str, Any]:
    """Read one partial, refusing anything that is missing, unreadable, or claims to be a report."""
    payload = _load(path, label=label)
    if payload.get("is_a_qualification_report") is not False:
        raise PreflightRefused(
            f"the {label} partial at {path} does not state is_a_qualification_report: false; "
            "a partial dressed as a report is refused when it is read, before anything is combined"
        )
    return payload


def _digest_of(partial: Mapping[str, Any], *, label: str) -> str:
    digest = partial.get("artifact_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise PreflightRefused(
            f"the {label} partial names no usable artifact digest; without one the two halves cannot be "
            "proven to describe the same build"
        )
    return digest


def build_preflight(offline: Mapping[str, Any], browser: Mapping[str, Any]) -> PilotPreflight:
    """Combine two partials, refusing them if they measured different bytes."""
    offline_digest = _digest_of(offline, label="offline")
    browser_digest = _digest_of(browser, label="browser")
    if offline_digest != browser_digest:
        raise PreflightRefused(
            "the offline and browser partials measured different artifacts "
            f"({offline_digest[:12]} vs {browser_digest[:12]}); re-run both against the same build "
            "rather than combining them"
        )
    preflight = PilotPreflight(
        artifact_sha256=offline_digest,
        offline=offline,
        browser=browser,
    )
    if preflight.unmeasured:
        raise PreflightRefused(
            "these criteria were never measured, so the preflight would be reporting a hole as an "
            f"absence: {', '.join(preflight.unmeasured)}"
        )
    return preflight


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: `python pilot_preflight.py <offline.json> <browser.json> [--out preflight.json]`."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    out: Path | None = None
    if "--out" in args:
        index = args.index("--out")
        try:
            out = Path(args[index + 1])
        except IndexError:
            print("--out needs a path", file=sys.stderr)
            return 2
        del args[index : index + 2]
    if len(args) != 2:
        print("usage: pilot_preflight.py <offline-partial.json> <browser-partial.json> [--out file]", file=sys.stderr)
        return 2
    try:
        offline = read_partial(Path(args[0]), label="offline")
        browser = read_partial(Path(args[1]), label="browser")
        payload = build_preflight(offline, browser).to_mapping()
    except PreflightError as exc:
        print(f"pilot preflight refused: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(payload, indent=2, sort_keys=True)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["ready_for_owner_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
