"""The Start pane's readiness answer (UIUX-P2-01).

The preflight decides what a first-run reader should do next; the pane's job is to
say it once, point at it, and explain the control it disables. These tests pin the
parts a screenshot cannot: that the pane renders the server's verdict instead of
recounting it, that the server never hands the pane an unusable next step, that a
row with no action says why, and that clearing a presentation does not drop a
keyboard user on `<body>`.

Every rule is also broken in a copy of the markup, because a check that passes
because it stopped looking is the same defect it was written to catch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "start_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Onboarding.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-start.md"

sys.path.insert(0, str(PROJECT_ROOT))

import onboarding_preflight as preflight  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_onboarding_inventory",
        PROJECT_ROOT / "scripts" / "uiux_onboarding_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def markup_copy(tmp_path, monkeypatch):
    def _make(transform) -> dict:
        text = FRONTEND.read_text(encoding="utf-8")
        changed = transform(text)
        assert changed != text, (
            "the transform did not change the markup, so the test proves nothing"
        )
        path = tmp_path / "index.html"
        path.write_text(changed, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ──────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_onboarding_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Start readiness and the next step")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-01"
    assert data["steps_contract"]["order"] == list(preflight.STEP_ORDER)


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the property the lead depends on ─────────────────────────────────────────


def test_every_next_step_the_server_can_name_carries_its_action(payload):
    actionable = [row for row in payload["matrix"] if row["next_step"]]
    assert actionable, "no case produced a next step, so this proves nothing"
    for row in actionable:
        assert row["next_step_action"], row["case"]
        assert row["next_step_action_label"], row["case"]
        assert row["next_step_action"] in {
            entry["action"] for entry in payload["actions"]
        }, row["case"]


def test_the_matrix_covers_every_step_and_status(payload):
    seen_steps = {step["id"] for row in payload["matrix"] for step in row["steps"]}
    assert seen_steps == set(preflight.STEP_ORDER)
    assert {"ready", "optional", "action-required", "blocked"} <= set(
        payload["statuses_seen"]
    )
    assert payload["totals"]["cases"] == len(inventory.CASES)


def test_the_matrix_reaches_ready_and_blocked(payload):
    assert any(row["ready"] for row in payload["matrix"]), "no case reaches ready"
    assert any(row["blocked"] for row in payload["matrix"]), (
        "no case reaches a blocked step"
    )
    for row in payload["matrix"]:
        # A ready report cannot also name a next step: next_step is by definition the
        # first step that is not ready, so a contradiction here would be a backend bug.
        assert not (row["ready"] and row["next_step"]), row["case"]


# ── the pane renders the verdict and does not recount it ─────────────────────


def test_the_pane_prints_the_server_counts_and_next_step(payload):
    renderer = payload["frontend"]["renderer"]
    assert renderer["present"]
    assert renderer["uses_next_step"]
    assert renderer["uses_counts"]
    assert renderer["uses_ready"] and renderer["announces_blocked"]
    assert renderer["action_attached"]


def test_the_pane_does_not_compute_readiness(payload):
    readiness = payload["frontend"]["readiness"]
    assert readiness["uses_server_ready"]
    assert not readiness["recomputes_ready"]
    assert not readiness["counts_client_side"]


def test_the_next_row_is_marked_from_the_server_id(payload):
    steps = payload["frontend"]["steps"]
    assert steps["aria_current"]
    assert steps["aria_current_from_next"]
    assert steps["waiting_line"] and steps["waiting_rule"]


def test_run_is_disabled_with_a_stated_reason(payload):
    run = payload["frontend"]["run_button"]
    assert run["described_by"] and run["hint_element"]
    assert run["hint_written"] and run["reason_names_next"]


def test_clearing_a_presentation_restores_focus_to_a_live_node(payload):
    focus = payload["frontend"]["focus"]
    assert focus["clear_restores"]
    assert focus["refresh_names_target"]
    assert focus["rendered_before_focus"], (
        "the lead is focused before it is rendered, so focus would fall to <body>"
    )


def test_every_state_the_renderer_emits_has_a_rule(payload):
    styles = payload["frontend"]["styles"]
    assert all(styles.values()), [name for name, ok in styles.items() if not ok]


# ── the walk exists and knows what to break ──────────────────────────────────


def test_the_walk_holds_the_pane_against_the_preflight():
    source = WALK.read_text(encoding="utf-8")
    assert "/api/onboarding/preflight" in source
    assert "onboardingLead" in source and "aria-current" in source
    assert "request.abort" in source, "the walk never refuses the preflight"
    assert "disabled" in source and "aria-describedby" in source
    assert 'page.keyboard.press("Enter")' in source, (
        "Re-check must be pressed from the keyboard, or focus is never held"
    )


def test_the_evidence_is_described_where_it_lives():
    media = PROJECT_ROOT / "docs" / "media" / "start"
    described = media / "README.md"
    assert described.exists(), "the captures need a note saying how they were taken"
    text = described.read_text(encoding="utf-8")
    assert "start_walk.mjs" in text


# ── negative cases: each rule must be able to fail ───────────────────────────


def _rename_first(
    text: str, marker: str, old: str, new: str, window: int = 1500
) -> str:
    start = text.find(marker)
    assert start > 0, f"{marker!r} is gone"
    block = text[start : start + window]
    assert old in block, f"{marker!r} no longer contains {old!r}"
    return text[:start] + block.replace(old, new, 1) + text[start + window :]


def test_a_lead_that_ignores_next_step_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: _rename_first(
            text,
            "function renderOnboardingLead",
            "const next = report.next_step || null;",
            "const next = null;",
        )
    )
    assert "lead-incomplete" in _checks(payload)


def test_a_pane_that_recounts_steps_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            "list.innerHTML = onboardingReport.steps.map",
            'const readyCount = onboardingReport.steps.filter(step => step.status === "ready").length;\n      list.innerHTML = onboardingReport.steps.map',
            1,
        )
    )
    assert "count-computed-client-side" in _checks(payload)


def test_a_pane_that_decides_readiness_itself_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            "list.innerHTML = onboardingReport.steps.map",
            'const allReady = onboardingReport.steps.every(step => step.status === "ready");\n      list.innerHTML = onboardingReport.steps.map',
            1,
        )
    )
    assert "readiness-recomputed" in _checks(payload)


def test_an_invented_aria_current_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: _rename_first(
            text,
            "list.innerHTML = onboardingReport.steps.map",
            "const isNext = Boolean(next && step.id === next.id);",
            "const isNext = index === 1;",
        )
    )
    assert "aria-current-invented" in _checks(payload)


def test_losing_the_waiting_line_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: _rename_first(
            text,
            "list.innerHTML = onboardingReport.steps.map",
            "onboarding-step-waiting",
            "onboarding-step-quiet",
        )
    )
    assert "waiting-line-missing" in _checks(payload)


def test_a_run_button_with_no_reason_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace('aria-describedby="onboardingRunHint"', "", 1)
    )
    assert "run-reason-missing" in _checks(payload)


def test_a_clear_that_forgets_focus_is_caught(markup_copy):
    payload = markup_copy(
        lambda text: text.replace(
            "const hadFocus = box.contains(document.activeElement);",
            "const hadFocus = false;",
            1,
        )
    )
    assert "focus-lost-on-clear" in _checks(payload)


def test_focusing_a_node_about_to_be_replaced_is_caught(markup_copy):
    def reorder(text: str) -> str:
        old = """      renderOnboardingLead();
      // The Re-check button lives inside the box this clears, so without a target a
      // keyboard user would be left on <body> with nothing announced.
      clearPresentation("onboardingState", { focus: "onboardingLead" });"""
        assert old in text
        return text.replace(
            old,
            """      clearPresentation("onboardingState", { focus: "onboardingLead" });
      renderOnboardingLead();""",
            1,
        )

    payload = markup_copy(reorder)
    assert "focus-target-replaced" in _checks(payload)
