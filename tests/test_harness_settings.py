"""The Harness's task creation and settings hierarchy (UIUX-P3-01).

The Harness is where work is admitted. Before this story the bar held nine controls that
decided a run and none of them said what the run would do with them, and "can this run?"
had one answer per code path - a toast, a different toast, and in the turn path a silent
`return`.

Each rule is broken in a copy of the source, because a rule that passes because it stopped
looking is the defect it was written to catch.
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
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "harness_settings_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Settings.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-settings.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_harness_settings_inventory",
        PROJECT_ROOT / "scripts" / "uiux_harness_settings_inventory.py",
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
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, (
                "the transform did not change the markup, so the test proves nothing"
            )
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, (
                "the transform did not change the server source, so the test proves nothing"
            )
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ─────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_harness_settings_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# The Harness: what Run will do, before you press it")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P3-01"
    assert data["totals"]["findings"] == 0
    assert [row["path"] for row in data["routes"]].count("/api/harness/settings") == 0


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the plan describes the run that will actually happen ────────────────────


def test_the_plan_reads_every_control_the_payload_sends(payload):
    plan = payload["frontend"]["plan"]
    assert plan["covers_the_run_payload"] is True
    assert plan["watches_the_same_controls"] is True, (
        "the ids the plan watches and the ids the run posts have diverged"
    )
    assert set(payload["frontend"]["ids_in_the_payload"]) == {
        "harnessCriticModel",
        "harnessEfficiencyMode",
        "harnessLeadModel",
        "harnessMaxSteps",
        "harnessPolicy",
        "harnessReasoning",
        "harnessUseWorktree",
        "harnessWorkflowMode",
        "harnessWorkspaceInput",
    }


def test_every_row_says_something(payload):
    plan = payload["frontend"]["plan"]
    assert plan["one_row_per_decision"] is True
    assert plan["a_row_cannot_be_empty"] is True
    assert plan["directory_says_which_mode"] is True
    assert plan["models_name_the_critic"] is True
    assert plan["workflow_carries_the_advanced_values"] is True


def test_the_policy_is_the_option_a_reader_selected(payload):
    assert payload["frontend"]["plan"]["policy_is_the_option_label"] is True, (
        "a second description of a policy can drift from the option that was chosen"
    )


def test_the_plan_is_built_from_controls_not_from_memory(payload):
    plan = payload["frontend"]["plan"]
    assert plan["reads_no_session_state"] is True
    assert plan["no_network"] is True
    assert plan["written_only_on_change"] is True
    assert plan["rows_carry_keys_for_tests"] is True


# ── the hidden settings say what they are ──────────────────────────────────


def test_the_advanced_group_states_its_own_contents(payload):
    advanced = payload["frontend"]["advanced"]
    assert advanced["is_a_details_group"] is True
    assert advanced["summary_names_its_contents"] is True
    assert advanced["summary_is_written_by_the_plan"] is True


def test_a_changed_setting_is_named_not_coloured(payload):
    assert payload["frontend"]["advanced"]["changed_is_marked_in_words"] is True
    assert payload["frontend"]["plan"]["changed_is_a_word"] is True
    assert payload["frontend"]["plan"]["changed_is_not_only_colour"] is True


def test_every_default_is_declared_by_the_markup(payload):
    advanced = payload["frontend"]["advanced"]
    assert advanced["every_setting_declares_its_default"] is True
    assert advanced["declared_default_is_the_selected_option"] is True, (
        "a default that disagrees with the selected option would mark a fresh pane as changed"
    )
    assert advanced["defaults_are_the_planned_ids"] is True


# ── one predicate for Run ───────────────────────────────────────────────────


def test_run_and_its_sentence_are_one_answer(payload):
    control = payload["frontend"]["run_control"]
    assert control["one_predicate"] is True
    assert control["every_reason_declared"] is True
    assert control["the_hint_is_that_sentence"] is True
    assert control["the_hint_is_a_live_region"] is True
    assert control["the_composer_is_described_by_it"] is True


def test_a_task_is_required_but_the_board_is_not(payload):
    control = payload["frontend"]["run_control"]
    assert control["no_task_is_not_ready"] is True
    assert control["the_board_needs_no_task"] is True
    assert control["the_dispatcher_asks_first"] is True
    assert control["an_empty_task_is_visible_not_silent"] is True


def test_one_run_at_a_time_and_every_path_still_reachable(payload):
    control = payload["frontend"]["run_control"]
    assert control["no_second_run_while_running"] is True
    assert control["every_path_still_dispatches"] is True
    assert control["state_follows_the_run"] is True
    assert control["state_follows_the_mode"] is True
    assert control["wire_covers_the_controls"] is True


# ── the brief Chat carried ─────────────────────────────────────────────────


def test_the_banner_appears_only_with_a_brief_and_counts_it(payload):
    brief = payload["frontend"]["brief"]
    assert brief["arrives_hidden"] is True
    assert brief["appears_only_with_a_brief"] is True
    assert brief["counts_what_it_carried"] is True
    assert brief["says_nothing_runs"] is True


def test_the_banner_compares_and_can_say_it_matches(payload):
    brief = payload["frontend"]["brief"]
    assert brief["compares_the_model"] is True
    assert brief["compares_the_directory"] is True
    assert brief["says_when_nothing_differs"] is True
    assert brief["written_only_on_change"] is True


def test_the_banner_goes_when_the_task_goes(payload):
    brief = payload["frontend"]["brief"]
    assert brief["handoff_sets_it"] is True
    assert brief["a_run_consumes_it"] is True
    assert brief["a_fresh_draft_clears_it"] is True
    assert brief["an_emptied_box_clears_it"] is True
    assert brief["clearing_is_idempotent"] is True


# ── the task box shows the task ────────────────────────────────────────────


def test_the_task_box_fits_the_task(payload):
    composer = payload["frontend"]["composer"]
    assert composer["fits_the_task_it_holds"] is True
    assert composer["has_a_floor_and_a_ceiling"] is True
    assert composer["follows_the_text"] is True
    assert composer["rests_after_a_run"] is True
    assert composer["manual_resize_is_off"] is True


# ── nothing that worked stopped working ───────────────────────────────────


def test_every_preserved_control_keeps_its_handler(payload):
    lost = [name for name, kept in payload["frontend"]["preserved"].items() if not kept]
    assert lost == [], f"controls lost their handler or binding: {lost}"


def test_every_unnamed_control_has_a_name(payload):
    unnamed = [name for name, named in payload["frontend"]["labelling"].items() if not named]
    assert unnamed == [], f"controls with no accessible name: {unnamed}"


def test_the_routes_the_controls_write_to_are_still_mounted(payload):
    missing = [
        f"{row['method']} {row['path']}" for row in payload["routes"] if not row["mounted"]
    ]
    assert missing == [], f"routes gone: {missing}"


def test_the_styles_the_surfaces_need_exist(payload):
    styles = payload["frontend"]["styles"]
    assert all(styles.values()), [name for name, present in styles.items() if not present]


# ── the walk covers the same ground ────────────────────────────────────────


def test_the_walk_reads_the_plan_and_presses_run():
    assert WALK.exists(), "scripts/harness_settings_walk.mjs is missing"
    source = WALK.read_text(encoding="utf-8")
    for needle in (
        "harnessRunPlanBody",
        "harnessAdvancedSummary",
        "harnessRunHint",
        "harnessIncomingBrief",
        "executeHarnessSelectedWorkflow",
        "handOffChatToHarness",
        "no page errors during the walk",
    ):
        assert needle in source, needle


# ── negative: a rule that stops looking ────────────────────────────────────


def test_a_plan_that_ignores_the_directory_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const dirNode = document.getElementById("harnessWorkspaceInput");\n',
            "",
            1,
        )
    )
    assert "plan" in _checks(payload)


def test_a_plan_row_that_can_be_empty_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "`${dir || \"the server's default project directory\"}`",
            "`${dir}`",
            1,
        )
    )
    assert "plan" in _checks(payload)


def test_a_copied_policy_description_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      {\n        // The option\'s own label is the whole sentence: the plan prints the policy the\n        // select shows rather than a second description of it that could drift.\n        key: "policy",',
            '      {\n        key: "policy",\n        effect: HARNESS_POLICY_EFFECT[policy],',
            1,
        )
    )
    assert "plan" in _checks(payload)


def test_a_summary_that_hides_its_contents_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '<summary><span>Run settings</span><span id="harnessAdvancedSummary" class="harness-advanced-summary"></span></summary>',
            '<summary><span>Run settings</span></summary>',
            1,
        )
    )
    assert "advanced" in _checks(payload)


def test_a_default_that_disagrees_with_the_markup_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'data-default="10"', 'data-default="6"', 1
        )
    )
    assert "advanced" in _checks(payload)


def test_a_button_that_can_run_with_no_task_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (button) button.disabled = !state.ready;",
            "    if (button) button.disabled = false;",
            1,
        )
    )
    assert "run-control" in _checks(payload)


def test_a_second_run_while_running_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    if (harnessRunActive) {\n      return {\n        reason: "running",',
            '    if (false) {\n      return {\n        reason: "running",',
            1,
        )
    )
    assert "run-control" in _checks(payload)


def test_a_silent_empty_task_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (!prompt) {\n      updateHarnessRunControl();\n      input.focus();\n      return;\n    }",
            "    if (!prompt) return;",
            1,
        )
    )
    assert "run-control" in _checks(payload)


def test_a_dispatcher_that_skips_the_predicate_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const state = harnessRunState();\n    if (!state.ready) {',
            "    if (false) {",
            1,
        )
    )
    assert "run-control" in _checks(payload)


def test_a_banner_that_never_compares_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    if (context && context.model && runPlan.lead && context.model !== runPlan.lead) {',
            "    if (false) {",
            1,
        )
    )
    assert "brief" in _checks(payload)


def test_a_banner_that_outlives_the_task_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    if (mode !== "sdlc_modal") clearHarnessIncomingBrief();\n',
            "",
            1,
        )
    )
    assert "brief" in _checks(payload)


def test_a_composer_that_never_grows_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    box.style.height = `${Math.round(wanted)}px`;\n",
            "",
            1,
        )
    )
    assert "composer" in _checks(payload)


def test_a_control_that_lost_its_handler_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '<select id="harnessPolicy" onchange="patchActiveHarnessSettings()">',
            '<select id="harnessPolicy">',
            1,
        )
    )
    assert "control-lost" in _checks(payload)


def test_an_unnamed_control_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '<label class="field-lbl" style="margin-bottom:2px;" for="harnessLeadModel">Lead Agent Model</label>',
            '<label class="field-lbl" style="margin-bottom:2px;">Lead Agent Model</label>',
            1,
        )
    )
    assert "unnamed-control" in _checks(payload)


def test_a_missing_route_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '@app.post("/api/workspace/directory")',
            '@app.post("/api/workspace/dir")',
            1,
        )
    )
    assert "route-missing" in _checks(payload)


def test_a_new_settings_route_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '@app.get("/api/harness/sessions")',
            '@app.post("/api/harness/settings")\nasync def api_harness_settings():\n    return {}\n\n\n@app.get("/api/harness/sessions")',
            1,
        )
    )
    assert "new-route" in _checks(payload)
