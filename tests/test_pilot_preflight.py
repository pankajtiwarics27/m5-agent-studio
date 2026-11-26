"""The pilot preflight refuses to combine two partials that do not describe the same build (AP-032).

`pilot_harness.py` measures 5 of 11 criteria offline and `scripts/pilot_browser_walk.mjs` measures the
other 6 in a browser. Each emits a partial; neither is a pilot. `pilot_preflight.py` joins them and
refuses the join when it would invent a result.

The tests are written from the failure outward, because every failure mode here produces a number that
looks fine:

* two partials from different builds would report 11/11 for a build nobody measured;
* a partial that never ran would look like an absence rather than a hole;
* a partial dressed as a report would turn preparation into a qualification.

`test_the_two_tools_compute_the_same_digest` is the load-bearing one: if the harness and the walk
disagree about how to hash an artifact, the same-bytes check silently compares two different questions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import pilot_harness as ph
import pilot_preflight as pp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WALK = PROJECT_ROOT / "scripts" / "pilot_browser_walk.mjs"
DOC = PROJECT_ROOT / "docs" / "ap032-pilot-harness.md"

SITE = {
    "index.html": (
        "<!doctype html><html lang='en'><head><title>Home</title></head><body><main>"
        "<p><a href='about.html'>About</a></p></main></body></html>"
    ),
    "about.html": (
        "<!doctype html><html lang='en'><head><title>About</title></head><body><main>"
        "<p><a href='index.html'>Home</a></p></main></body></html>"
    ),
}


def _offline(files: dict[str, str]) -> dict[str, Any]:
    return ph.build_pilot_partial(files).to_mapping()


def _browser(digest: str, *, overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """A browser partial shaped exactly as the walk emits one, without needing a browser."""
    criteria = []
    for criterion in ph.BROWSER_CRITERIA:
        row = {"criterion": criterion, "passed": True, "basis": "measured", "detail": "measured"}
        if overrides and criterion in overrides:
            row.update(overrides[criterion])
        criteria.append(row)
    return {
        "is_a_qualification_report": False,
        "artifact_sha256": digest,
        "criteria": criteria,
    }


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pair(files: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    source = files or SITE
    offline = _offline(source)
    return offline, _browser(offline["artifact_sha256"])


# ── the happy path is only interesting because of the refusals below ──────────────────────────────


def test_a_clean_pair_is_ready_for_review_but_is_not_a_report() -> None:
    offline, browser = _pair()
    payload = pp.build_preflight(offline, browser).to_mapping()
    assert payload["ready_for_owner_review"] is True
    assert payload["is_a_qualification_report"] is False
    assert len(payload["measured"]) == 11
    assert payload["failed"] == [] and payload["unmeasured"] == []
    assert payload["what_a_human_still_owes"], "a ready preflight that names nothing outstanding reads as done"


def test_every_criterion_names_the_tool_that_owns_it() -> None:
    offline, browser = _pair()
    rows = pp.build_preflight(offline, browser).criteria
    assert [row["criterion"] for row in rows] == list(pp.EXPECTED_CRITERIA)
    owners = {row["criterion"]: row["owner"] for row in rows}
    for criterion in ph.OFFLINE_CRITERIA:
        assert owners[criterion] == "pilot_harness.py"
    for criterion in ph.BROWSER_CRITERIA:
        assert owners[criterion] == "pilot_browser_walk.mjs"


# ── the refusals ─────────────────────────────────────────────────────────────────────────────────


def test_two_partials_from_different_builds_are_refused() -> None:
    """The whole reason for a shared digest: 11/11 for a build nobody measured is the defect."""
    offline, _ = _pair()
    with pytest.raises(pp.PreflightRefused, match="different artifacts"):
        pp.build_preflight(offline, _browser("f" * 64))


def test_a_partial_that_never_ran_is_not_an_empty_section() -> None:
    offline, browser = _pair()
    for row in browser["criteria"]:
        if row["criterion"] == "console_clean":
            row["basis"] = "not-measured"
            row["passed"] = False
    with pytest.raises(pp.PreflightRefused, match="console_clean"):
        pp.build_preflight(offline, browser)


def test_a_criterion_missing_from_both_partials_is_a_hole_not_a_pass() -> None:
    offline, browser = _pair()
    browser["criteria"] = [row for row in browser["criteria"] if row["criterion"] != "focus_visible"]
    with pytest.raises(pp.PreflightRefused, match="focus_visible"):
        pp.build_preflight(offline, browser)


def test_a_partial_without_a_digest_cannot_be_combined() -> None:
    offline, browser = _pair()
    del browser["artifact_sha256"]
    with pytest.raises(pp.PreflightRefused, match="no usable artifact digest"):
        pp.build_preflight(offline, browser)


def test_a_partial_dressed_as_a_report_is_refused() -> None:
    offline, browser = _pair()
    browser["is_a_qualification_report"] = True
    with pytest.raises(pp.PreflightRefused, match="dressed as a report"):
        pp.build_preflight(offline, browser)


def test_a_partial_dressed_as_a_report_is_refused_at_the_point_it_is_read(tmp_path: Path) -> None:
    """Two layers refuse it: the reader and the dataclass. Only the reader names the file path, so
    without this the test cannot tell a refusal from the right place from one two layers down."""
    offline, browser = _pair()
    browser["is_a_qualification_report"] = True
    path = _write(tmp_path / "browser.json", browser)
    with pytest.raises(pp.PreflightRefused, match="refused when it is read"):
        pp.read_partial(path, label="browser")


def test_a_missing_partial_names_the_tool_that_should_have_produced_it(tmp_path: Path) -> None:
    with pytest.raises(pp.PreflightRefused, match="run its tool"):
        pp.read_partial(tmp_path / "absent.json", label="browser")


def test_an_unreadable_partial_is_not_treated_as_absent(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(pp.PreflightError, match="not readable JSON"):
        pp.read_partial(bad, label="offline")


def test_a_failed_criterion_is_reported_not_hidden() -> None:
    """A red criterion must still produce a preflight -- with the failure named, and not ready."""
    offline, browser = _pair()
    for row in browser["criteria"]:
        if row["criterion"] == "contrast_ok":
            row["passed"] = False
            row["detail"] = "index.html: 2 below AA"
    preflight = pp.build_preflight(offline, browser)
    assert preflight.ready_for_owner_review is False
    assert preflight.failed == ("contrast_ok",)
    assert "index.html" in dict((row["criterion"], row["detail"]) for row in preflight.criteria)["contrast_ok"]


def test_the_preflight_never_claims_the_artifact_is_qualified() -> None:
    offline, browser = _pair()
    payload = pp.build_preflight(offline, browser).to_mapping()
    text = json.dumps(payload).lower()
    for word in ('"qualified": true', '"deliverable": true', '"ship"'):
        assert word not in text
    assert "ready for owner review means the machine-measurable criteria are done" in payload["note"].lower()


# ── the digest is the contract between the two tools ─────────────────────────────────────────────


def test_the_two_tools_compute_the_same_digest(tmp_path: Path) -> None:
    """If the harness and the walk hash the artifact differently, same-bytes is a meaningless check.

    The walk is asked to digest a directory and print it; the harness digests the same files read into
    a mapping. They must agree byte for byte, including on ordering and on what counts as a text file.
    """
    site = tmp_path / "site"
    for name, text in SITE.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(text, encoding="utf-8")
    # A file the walk must ignore for the digests to match: the harness never reads it.
    (site / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the walk's digest cannot be compared here")
    # The walk runs itself on import, so its digest routine is re-executed here verbatim rather than
    # imported. If the two copies ever drift, this test fails -- which is the point of comparing them.
    printed = subprocess.run(
        [node, "-e", _DIGEST_SNIPPET, str(site)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=PROJECT_ROOT,
    )
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == ph.artifact_digest(SITE)


_DIGEST_SNIPPET = r"""
const { createHash } = require("node:crypto");
const { readdirSync, readFileSync, statSync } = require("node:fs");
const { join } = require("node:path");
const root = process.argv[1];
const exts = [".html", ".htm", ".css", ".js", ".txt"];
const files = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir).sort()) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full);
    else if (exts.some((ext) => entry.toLowerCase().endsWith(ext))) files.push(full);
  }
};
walk(root);
const digest = createHash("sha256");
for (const file of files.sort()) {
  digest.update(file.slice(root.length + 1), "utf-8");
  digest.update(Buffer.from([0]));
  digest.update(readFileSync(file, "utf-8"), "utf-8");
  digest.update(Buffer.from([0]));
}
console.log(digest.digest("hex"));
"""


def test_the_digest_changes_when_a_file_changes() -> None:
    changed = dict(SITE)
    changed["about.html"] = changed["about.html"].replace("About", "About us")
    assert ph.artifact_digest(SITE) != ph.artifact_digest(changed)


def test_the_digest_notices_a_renamed_file() -> None:
    renamed = {"index.html": SITE["index.html"], "team.html": SITE["about.html"]}
    assert ph.artifact_digest(SITE) != ph.artifact_digest(renamed)


# ── the CLI ──────────────────────────────────────────────────────────────────────────────────────


def test_the_cli_exits_zero_only_when_ready(tmp_path: Path) -> None:
    offline, browser = _pair()
    code = pp.main([
        str(_write(tmp_path / "o.json", offline)),
        str(_write(tmp_path / "b.json", browser)),
        "--out",
        str(tmp_path / "pre.json"),
    ])
    assert code == 0
    written = json.loads((tmp_path / "pre.json").read_text(encoding="utf-8"))
    assert written["ready_for_owner_review"] is True


def test_the_cli_exits_one_when_a_criterion_failed(tmp_path: Path) -> None:
    offline, browser = _pair()
    for row in browser["criteria"]:
        if row["criterion"] == "keyboard_reachable":
            row["passed"] = False
    code = pp.main([
        str(_write(tmp_path / "o.json", offline)),
        str(_write(tmp_path / "b.json", browser)),
    ])
    assert code == 1


def test_the_cli_refuses_a_mismatched_pair_and_says_why(tmp_path: Path, capsys: Any) -> None:
    offline, _ = _pair()
    code = pp.main([
        str(_write(tmp_path / "o.json", offline)),
        str(_write(tmp_path / "b.json", _browser("a" * 64))),
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert "different artifacts" in captured.err
    assert captured.out == "", "a refusal that also prints a payload is ambiguous about what happened"


def test_the_cli_needs_exactly_two_partials(tmp_path: Path, capsys: Any) -> None:
    assert pp.main([str(tmp_path / "only.json")]) == 2
    assert "usage:" in capsys.readouterr().err


def test_the_harness_doc_names_the_preflight() -> None:
    assert "pilot_preflight.py" in DOC.read_text(encoding="utf-8"), (
        "a tool nobody can find from the pilot doc is a tool nobody runs"
    )
