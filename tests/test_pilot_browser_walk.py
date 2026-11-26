"""The pilot browser walk measures what `pilot_harness.py` refuses to guess (AP-032).

`pilot_harness.py` returns six criteria as `cannot-measure-here` because a focus ring, a contrast
ratio, an animation and a console error do not exist until a browser renders the page.
`scripts/pilot_browser_walk.mjs` measures them by driving Chromium at a `file://` artifact.

The tests are split in two on purpose:

* **Always run.** The walk's own contract is read from its source: it refuses to report a walk it did
  not perform, it never emits a pass without evidence, and it names the failing document. These catch
  the failure mode that matters most -- a walk that skips on a missing dependency and exits 0.
* **Run when a browser is present.** `M5_PILOT_BROWSER` points at a Chromium binary; without it the
  browser tests *skip with the reason*, which is honest, and the walk itself is never invoked. A test
  that silently passed in the absence of a browser would be the defect, not the coverage.

The browser tests assert both directions: a good artifact clears all six, and a broken one fails the
criterion whose defect it contains, naming the page.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WALK = PROJECT_ROOT / "scripts" / "pilot_browser_walk.mjs"
HARNESS = PROJECT_ROOT / "pilot_harness.py"
DOC = PROJECT_ROOT / "docs" / "ap032-pilot-harness.md"
BROWSER_DIR = PROJECT_ROOT / "tests" / "browser"

CHROME = os.environ.get("M5_PILOT_BROWSER", "")


def _walk_source() -> str:
    return WALK.read_text(encoding="utf-8")


# ── the walk's contract, read from its source; always runs ────────────────────────────────────────


def test_the_walk_exists_and_is_wired_to_the_harness_doc() -> None:
    assert WALK.is_file(), "the six browser criteria have no walk"
    assert "pilot_browser_walk.mjs" in DOC.read_text(encoding="utf-8"), (
        "the walk is not referenced from the harness doc, so nobody would find it"
    )


def test_the_harness_still_refuses_the_six_criteria_the_walk_measures() -> None:
    """The split only holds if each side names the same six. A drift here would double-count or drop one."""
    import pilot_harness as ph

    source = _walk_source()
    for criterion in ph.BROWSER_CRITERIA:
        assert criterion in source, f"{criterion} is refused by the harness but unmeasured by the walk"
    assert "CHECKS = [" in source


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform() -> None:
    source = _walk_source()
    assert "refuses to report a walk it did not perform" in source
    assert "STUDIO_SCREENSHOT_CHROME" in source
    # The two ways this walk could lie: exit 0 without a browser, or exit 0 without a page.
    assert "process.exit(0)" not in source.replace("process.exit(0);\n", ""), (
        "the walk can exit successfully without measuring"
    )
    assert "a walk of nothing proves nothing" in source


def test_a_missing_measurement_is_never_a_pass() -> None:
    source = _walk_source()
    assert 'basis: "not-measured"' in source
    assert "passed: false" in source
    # Every criterion in CHECKS has to appear in the aggregation, not a hand-written subset.
    assert "CHECKS.map(" in source


def test_the_walk_names_the_document_that_failed_and_counts_the_ones_that_passed() -> None:
    """One green page must not read as 'the artifact passed'."""
    source = _walk_source()
    assert "document(s) failed" in source
    assert "document(s) passed" in source
    assert "rows.length" in source, "a pass that does not count its documents cannot be checked"


def test_each_criterion_is_measured_not_assumed() -> None:
    source = _walk_source()
    for marker in (
        "keyboard.press(\"Tab\")",       # reachability is real key presses
        "getComputedStyle",              # focus ring and contrast are computed styles
        "prefers-reduced-motion",        # motion is emulated, not declared
        "pageerror",                     # console errors are observed, not scanned for
        "ERR_FILE_NOT_FOUND",            # links are followed, not string-matched
        "mouseOnly",                     # a click handler on a div is a mouse-only control
    ):
        assert marker in source, f"nothing in the walk measures {marker!r}"


# ── the walk's behaviour on a real browser; skips visibly without one ─────────────────────────────

needs_browser = pytest.mark.skipif(
    not (CHROME and Path(CHROME).is_file()),
    reason="M5_PILOT_BROWSER is not set to a Chromium binary; the walk is not invoked at all",
)

GOOD_SITE = {
    "index.html": """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Field Notes</title>
<style>
 body{font-family:system-ui;background:#ffffff;color:#111827;margin:0}
 main{padding:24px;max-width:60ch}
 button{background:#1d4ed8;color:#ffffff;border:0;padding:10px 16px}
 button:focus-visible,a:focus-visible{outline:2px solid #111827;outline-offset:2px}
</style></head><body><main><p>We visit sites.</p>
<p><a href="about.html">About</a></p>
<form action="/subscribe" method="post"><button type="submit">Join</button></form>
</main></body></html>""",
    "about.html": """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>About</title>
<style>body{background:#fff;color:#111;font-family:system-ui}a:focus-visible{outline:2px solid #000}
main{padding:24px}</style></head><body><main><h2>About</h2>
<a href="index.html">Home</a></main></body></html>""",
}


def _write_site(root: Path, pages: dict[str, str]) -> Path:
    for name, html in pages.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
    return root


def _run_walk(root: Path, tmp: Path) -> tuple[int, dict[str, object]]:
    out = tmp / "partial.json"
    env = dict(os.environ)
    env["STUDIO_SCREENSHOT_CHROME"] = CHROME
    env["STUDIO_PUPPETEER_CORE"] = str(BROWSER_DIR / "node_modules" / "puppeteer-core")
    # The pinned Chromium in this environment needs its own shared libraries on the path.
    libs = Path(CHROME).parent.parent / "libs" / "lib"
    if libs.is_dir():
        env["LD_LIBRARY_PATH"] = f"{libs}:{env.get('LD_LIBRARY_PATH', '')}"
    completed = subprocess.run(
        ["node", str(WALK), str(root), "--out", str(out)],
        cwd=BROWSER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    payload = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    return completed.returncode, payload


def _rows(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    criteria = payload["criteria"]
    assert isinstance(criteria, list)
    return {str(row["criterion"]): row for row in criteria}


@needs_browser
def test_a_clean_artifact_clears_all_six(tmp_path: Path) -> None:
    site = _write_site(tmp_path / "site", GOOD_SITE)
    code, payload = _run_walk(site, tmp_path)
    rows = _rows(payload)
    assert payload["measured_all_six"] is True
    assert payload["is_a_qualification_report"] is False
    assert code == 0 and payload["cleared"] is True, {
        name: row["detail"] for name, row in rows.items() if not row["passed"]
    }
    assert all(row["basis"] == "measured" for row in rows.values())


@needs_browser
def test_a_mouse_only_control_fails_keyboard_reachability(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["index.html"] = pages["index.html"].replace(
        "<p>We visit sites.</p>", '<div onclick="void 0">Open the menu</div>'
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["keyboard_reachable"]
    assert row["passed"] is False and row["basis"] == "measured"
    assert "mouse-only=1" in str(row["detail"])


@needs_browser
def test_a_control_without_a_focus_ring_fails_focus_visible(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    # Append at the end of the stylesheet, or the site's own focus rule would still win.
    pages["index.html"] = pages["index.html"].replace(
        "</style>", " button:focus-visible{outline:none!important}</style>"
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["focus_visible"]
    assert row["passed"] is False
    assert "without-a-focus-indicator=1" in str(row["detail"])


@needs_browser
def test_low_contrast_text_fails_contrast_ok(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["index.html"] = pages["index.html"].replace(
        "<p>We visit sites.</p>", '<p style="color:#c8c8c8">pale on white</p>'
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["contrast_ok"]
    assert row["passed"] is False
    assert "below AA" in str(row["detail"])


@needs_browser
def test_an_animation_that_survives_reduced_motion_fails(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["index.html"] = pages["index.html"].replace(
        "</style>", " .spin{animation:none;transition:opacity 1.4s}</style>"
    ).replace("<p>We visit sites.</p>", '<p class="spin">moves anyway</p>')
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["reduced_motion_respected"]
    assert row["passed"] is False
    assert "over 0.01s=1" in str(row["detail"])


@needs_browser
def test_a_console_error_fails_console_clean(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["index.html"] = pages["index.html"].replace(
        "</main>", '<script>console.error("kaboom")</script></main>'
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["console_clean"]
    assert row["passed"] is False
    assert "kaboom" in str(row["detail"])


@needs_browser
def test_a_broken_internal_link_fails_navigation_works(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["index.html"] = pages["index.html"].replace(
        '<p><a href="about.html">About</a></p>', '<p><a href="missing.html">Missing</a></p>'
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    row = _rows(payload)["navigation_works"]
    assert row["passed"] is False
    assert "links-broken=1" in str(row["detail"])


@needs_browser
def test_the_detail_names_the_page_that_failed(tmp_path: Path) -> None:
    pages = dict(GOOD_SITE)
    pages["about.html"] = pages["about.html"].replace(
        'a:focus-visible{outline:2px solid #000}', "a:focus-visible{outline:none}"
    )
    code, payload = _run_walk(_write_site(tmp_path / "site", pages), tmp_path)
    detail = str(_rows(payload)["focus_visible"]["detail"])
    assert "about.html" in detail, f"the failure does not say which page: {detail}"


@needs_browser
def test_an_artifact_with_no_html_is_refused_rather_than_passing(tmp_path: Path) -> None:
    site = tmp_path / "empty"
    site.mkdir()
    (site / "notes.txt").write_text("nothing to walk", encoding="utf-8")
    completed = subprocess.run(
        ["node", str(WALK), str(site)],
        cwd=BROWSER_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "STUDIO_SCREENSHOT_CHROME": CHROME},
    )
    assert completed.returncode == 2
    assert "proves nothing" in completed.stderr


@needs_browser
def test_the_walk_refuses_without_a_browser(tmp_path: Path) -> None:
    """A walk that skipped the browser and reported success is the failure this asserts against."""
    site = _write_site(tmp_path / "site", GOOD_SITE)
    completed = subprocess.run(
        ["node", str(WALK), str(site)],
        cwd=BROWSER_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "STUDIO_SCREENSHOT_CHROME": str(tmp_path / "no-such-chrome")},
    )
    assert completed.returncode == 2
    assert "walk it did not perform" in completed.stderr
