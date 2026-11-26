"""The pilot's three criterion lists and its layout matrix must stay one agreement.

`website_quality.CRITERIA`, `pilot_preflight.EXPECTED_CRITERIA` and the harness/walk split each name
the pilot's eleven criteria. They agree today. Nothing forced them to: a criterion renamed in one place
would not fail anything — it would quietly retire a check, because the preflight only *joins* what the
tools emit and the report only *checks* what it is given. An omission that nothing catches is the
defect this repository exists to prevent.

These tests pin the agreement and the reason it holds. They also pin the honest negative: the layout
matrix `PilotReport` demands has no measuring tool in this repository, so no command can produce a
report, and the runbook's step 6 must not read like one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pilot_harness
import pilot_preflight
import website_quality as wq

ROOT = Path(__file__).resolve().parent.parent


def test_the_three_criterion_lists_are_the_same_eleven() -> None:
    """A rename in any one of the three must fail here rather than silently retire a criterion."""
    report_side = set(wq.CRITERIA)
    preflight_side = set(pilot_preflight.EXPECTED_CRITERIA)
    tools_side = set(pilot_harness.OFFLINE_CRITERIA) | set(pilot_harness.BROWSER_CRITERIA)

    assert report_side == preflight_side, (
        "the pilot report and the preflight disagree: "
        f"only in website_quality {sorted(report_side - preflight_side)}, "
        f"only in pilot_preflight {sorted(preflight_side - report_side)}"
    )
    assert preflight_side == tools_side, (
        "the preflight and the two tools disagree: "
        f"only in pilot_preflight {sorted(preflight_side - tools_side)}, "
        f"only in the harness or walk {sorted(tools_side - preflight_side)}"
    )


def test_owner_assignments_name_exactly_the_two_halves() -> None:
    """Each outcome row's owner must be the tool that actually produced it.

    A loop-built dict cannot have duplicate keys, so asserting "one owner each" is trivially true and
    guards nothing. What can go wrong is the two comprehensions disagreeing with the list they iterate:
    an owner dict that named a criterion twice would make the join read the wrong partial.
    """
    owners = pilot_preflight.OWNERS
    assert set(owners) == set(pilot_preflight.EXPECTED_CRITERIA), (
        "the owner table does not cover exactly the expected criteria: "
        f"missing {sorted(set(pilot_preflight.EXPECTED_CRITERIA) - set(owners))}, "
        f"extra {sorted(set(owners) - set(pilot_preflight.EXPECTED_CRITERIA))}"
    )
    for criterion in pilot_harness.OFFLINE_CRITERIA:
        assert owners[criterion] == "pilot_harness.py", f"{criterion} is offline but owned elsewhere"
    for criterion in pilot_harness.BROWSER_CRITERIA:
        assert owners[criterion] == "pilot_browser_walk.mjs", f"{criterion} is browser-owned but not"


def test_the_join_reads_each_criterion_from_its_owner_partial() -> None:
    """The preflight must not look for a browser criterion in the offline partial, or vice versa."""
    source = (ROOT / "pilot_preflight.py").read_text(encoding="utf-8")
    assert re.search(
        r"source\s*=\s*offline_rows\s+if\s+criterion\s+in\s+OFFLINE_CRITERIA\s+else\s+browser_rows", source
    ), (
        "the preflight no longer routes each criterion to its owning partial; a criterion read from "
        "the wrong partial would be reported as unmeasured by its real measurement"
    )


def test_the_two_halves_do_not_overlap() -> None:
    """A criterion in both partials is ambiguous: the join would have to choose a winner."""
    offline = set(pilot_harness.OFFLINE_CRITERIA)
    browser = set(pilot_harness.BROWSER_CRITERIA)
    assert not offline & browser, f"claimed by both tools: {sorted(offline & browser)}"
    assert len(offline) + len(browser) == len(wq.CRITERIA)


def test_the_harness_measures_only_its_own_half() -> None:
    """The harness must not emit an outcome for a criterion it does not own.

    Guarding this by comparing the two tuples only checks the tuples. The harness rows are built by
    iterating a list inside `audit_artifact`, so make the loop cover both halves and the emitted rows,
    not the constants, are what changes.
    """
    partial = pilot_harness.build_pilot_partial({"index.html": "<!doctype html><html><p>hello</p></html>"})
    emitted = {row.criterion for row in partial.outcomes}
    assert emitted == set(pilot_harness.OFFLINE_CRITERIA) | set(pilot_harness.BROWSER_CRITERIA), (
        "the harness emits rows for: " + ", ".join(sorted(emitted))
    )
    source = (ROOT / "pilot_harness.py").read_text(encoding="utf-8")
    assert re.search(r"for criterion in BROWSER_CRITERIA:\n\s+outcomes\.append", source), (
        "the harness no longer emits an explicit row for each browser criterion it cannot measure; "
        "a criterion it stopped mentioning would read as absent rather than unmeasured"
    )


def test_the_harness_never_passes_a_criterion_it_cannot_measure() -> None:
    """The harness emits all eleven; the six it cannot measure must not be `passed=True`.

    The artifact is written here rather than read from a fixture, so this test has nothing to skip on:
    a missing fixture must not be able to turn it into a pass.
    """
    partial = pilot_harness.build_pilot_partial(
        {
            "index.html": (
                "<!doctype html><html><head><title>t</title></head><body>"
                '<h1>Hi</h1><a href="/about.html">about</a></body></html>'
            ),
            "about.html": '<!doctype html><html><body><a href="/index.html">home</a></body></html>',
        }
    )
    rows = {row["criterion"]: row for row in partial.to_mapping()["outcomes"]}
    assert set(rows) == set(wq.CRITERIA), "the harness stopped emitting a row for every criterion"
    for criterion in pilot_harness.OFFLINE_CRITERIA:
        assert rows[criterion]["basis"] == "measured"
    for criterion in pilot_harness.BROWSER_CRITERIA:
        row = rows[criterion]
        assert row["passed"] is False, f"{criterion} was passed by the harness, which cannot measure it"
        assert row["basis"] == "cannot-measure-here", (
            f"{criterion} has basis {row['basis']!r}; an unmeasured criterion must say so"
        )


def test_the_layout_matrix_has_no_measuring_tool() -> None:
    """If something starts measuring zoom, this test should fail so the runbook gets rewritten.

    Today the claim in the docs is "nothing here measures the layout matrix". That claim is only worth
    making if it is checked: a script that gained viewport/zoom measurement would make step 6 runnable
    and the doc stale.
    """
    scripts = sorted((ROOT / "scripts").glob("*.mjs")) + sorted((ROOT / "scripts").glob("*.py"))
    measurers = []
    for script in scripts:
        if script.name in {"pilot_browser_walk.mjs", "pilot_preflight.py"}:
            continue
        text = script.read_text(encoding="utf-8", errors="replace")
        if re.search(r"zoomFactor|page\.setViewport.*zoom|emulateMediaFeatures.*zoom", text):
            measurers.append(script.name)
    assert not measurers, (
        "these scripts appear to measure page zoom; the runbook says no tool measures the layout "
        f"matrix, so either the doc or this test is now wrong: {measurers}"
    )


def test_pilot_report_demands_all_six_cells_so_no_partial_can_be_filed() -> None:
    """A report with a partial layout must refuse to exist, not merely report `deliverable` false."""
    one_cell = (wq.LayoutCell(viewport=wq.Viewport.named("desktop"), zoom=1, clipped=False, overlapping=False),)
    outcomes = {criterion: True for criterion in wq.CRITERIA}
    try:
        wq.PilotReport(artifact_sha256="0" * 64, outcomes=outcomes, layout=one_cell, owner_review=None)
    except wq.LayoutRefused as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError(
            "a report with one layout cell was constructed; an unmeasured cell would then be "
            "indistinguishable from a cell that did not clip"
        )


def test_all_six_cells_construct_and_still_are_not_deliverable() -> None:
    """The complete matrix is constructible, and still not a qualification without an owner review."""
    cells = tuple(
        wq.LayoutCell(viewport=wq.Viewport.named(name), zoom=zoom, clipped=False, overlapping=False)
        for name, _w, _h in wq.VIEWPORT_MATRIX
        for zoom in (1, wq.ZOOM_FACTOR)
    )
    assert len(cells) == 6
    outcomes = {criterion: True for criterion in wq.CRITERIA}
    report = wq.PilotReport(artifact_sha256="0" * 64, outcomes=outcomes, layout=cells, owner_review=None)
    assert report.deliverable() is False, (
        "a report with no owner review claimed to be deliverable; an agent-shaped pass would then "
        "be indistinguishable from a reviewed pilot"
    )


def test_step_six_of_the_runbook_does_not_pretend_to_be_a_command() -> None:
    """The runbook step that produces the report must say it is not runnable as written."""
    doc = (ROOT / "docs" / "ap032-pilot-harness.md").read_text(encoding="utf-8")
    step_six = re.search(r"\n6\. (.+?)(?=\n7\. |\n## )", doc, re.DOTALL)
    assert step_six, "step 6 of the pilot runbook is missing; the procedure has lost a step"
    body = step_six.group(1)
    assert "not a command" in body, (
        "step 6 does not say it is not a command, so a reader will try to run it: " + body[:200]
    )
    assert "layout" in body, "step 6 does not mention the layout matrix a human has to measure"
    # And the claim it makes about the library must still be true.
    library = (ROOT / "website_quality.py").read_text(encoding="utf-8")
    assert "def main(" not in library, (
        "website_quality gained a main(); step 6 of the runbook says there is no CLI, so update both"
    )
    assert "__main__" not in library
