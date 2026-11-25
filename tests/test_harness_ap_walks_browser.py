"""The AP harness walks, run in a real browser as part of the mandatory CI job.

These three walks each start their own isolated demo fixture, stage every record through the
route the product uses, and then drive the shipped UI. They existed since AP-018/AP-019/AP-020
but nothing in CI executed them, so a change to a route contract could break a fixture and the
only symptom was a stale document claiming "22/22 checks passed".

That is not hypothetical. AP-020 replaced the direction route's inline ``benchmarks`` list with
``reference_ids`` looked up in the store, which is the fix the story exists for. The AP-018
seed was still sending the old field, so the walk could not stage its covered task at all. No
test noticed; the regression was found by running the walk by hand. These tests are what makes
that a red build instead of a discovery.

Each walk is asserted on three things, because the count alone is weak evidence: the walk
exits zero, it reports zero failures, and it ran the number of checks its own documentation
claims. A walk that quietly stopped checking would still exit zero.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (script, documented check count). The counts are the ones each walk prints and each doc page
# quotes; if a walk gains a check this list is the thing that has to be updated on purpose.
AP_WALKS = (
    ("scripts/harness_direction_walk.mjs", 22),
    ("scripts/harness_feedback_walk.mjs", 53),
    ("scripts/harness_reference_walk.mjs", 40),
)


@pytest.fixture
def walk_env(tmp_path: Path) -> dict[str, str]:
    """A browser environment, or a hard failure when CI says browsers are required."""
    if not os.environ.get("STUDIO_SCREENSHOT_CHROME") or not os.environ.get(
        "STUDIO_PUPPETEER_CORE"
    ):
        if os.environ.get("M5_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail(
                "browser regressions are required but Chromium or puppeteer-core is not configured"
            )
        pytest.skip(
            "set STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE for browser regressions"
        )
    env = os.environ.copy()
    # The seeds write an isolated workspace under their own temp root; a shared HOME would let
    # one walk read another's app database.
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir()
    env.setdefault("STUDIO_PYTHON", sys.executable)
    return env


@pytest.mark.parametrize(("script", "expected"), AP_WALKS, ids=[Path(s).stem for s, _ in AP_WALKS])
def test_ap_walk_passes_every_check_in_a_real_browser(
    walk_env: dict[str, str], script: str, expected: int
) -> None:
    result = subprocess.run(
        ["node", str(ROOT / script)],
        cwd=ROOT,
        env=walk_env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # The walks print a summary line; parse it rather than trusting the exit code alone.
    assert f"{expected}/{expected} checks passed" in output, output


def test_the_covered_task_gets_its_coverage_from_the_store_not_the_body() -> None:
    """Pin the contract that broke AP-018's fixture, so the fixture cannot drift back.

    The direction route reads coverage from stored, owner-approved references. A seed that
    sends an inline coverage assertion (``benchmarks``) is either rejected or silently ignored,
    and either way the walk would be staging a state the product cannot produce.
    """
    seed = (ROOT / "scripts" / "harness_direction_seed.py").read_text(encoding="utf-8")
    assert "reference_ids" in seed
    assert '"benchmarks"' not in seed
    # Coverage is only real if a reference was recorded through the product's own route.
    assert "/api/reference-benchmarks" in seed
    assert "use_as_reference" in seed

    # And the covered task must live in its own project: a reference covers the project it was
    # approved for, so leaving it in the shared workspace would also cover the unbenchmarked one.
    assert "covered-website" in seed


def test_the_direction_route_refuses_coverage_the_project_does_not_hold() -> None:
    """A caller naming a reference it does not hold is refused, not skipped (AP-020's fix)."""
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    route = app[
        app.index('@app.post("/api/harness/sessions/{session_id}/direction-checkpoint")') :
        app.index('@app.post("/api/harness/sessions/{session_id}/direction-answer")')
    ]
    assert "reference_ids" in route
    # The old shape must be gone from the route that used to trust it.
    assert 'body.get("benchmarks")' not in route
    assert "no owner-approved reference" in route
