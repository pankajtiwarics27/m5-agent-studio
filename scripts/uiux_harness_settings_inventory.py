#!/usr/bin/env python3
"""Inventory the Harness's task creation and settings hierarchy (UIUX-P3-01).

    python3 scripts/uiux_harness_settings_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_harness_settings_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_harness_settings_inventory.py --json     # print the inventory

The Harness is where work is admitted, and admission needs two answers before a task is
committed: *what will this run do?* and *can it run at all?* The bar held nine controls
that decided the answer to the first and none of them said it - the policy's meaning, the
critic's role, and the three settings inside a collapsed group were all things a reader
had to know already - and the second had one answer per code path: an empty task was a
toast in two of them and silence in the third.

So this artifact measures three surfaces:

* the **plan**, which prints what Run will use, read from the controls the run payload
  reads, so it cannot describe a run this pane would not start;
* the **Run control**, one predicate (`harnessRunState()`) driving both the button's
  disabled state and the sentence beside it, exactly as `chatSendReason()` drives Send;
* the **brief**, the banner that says what Chat carried and where this run departs from it.

It fails when the plan stops reading a control the payload sends, when a row can print
nothing, when a policy description is copied into the script instead of printed from the
option, when the collapsed group stops stating its own contents or a changed setting is
signalled by colour alone, when Run can be pressed with no task, when a second run can
start while one is in flight, when the empty-task path goes back to being silent, when the
banner appears without a brief, stops comparing, or outlives the task it describes, and
when a control the story must preserve loses its handler or its accessible name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Settings.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-settings.md"

REASONS = ("running", "board", "no-task", "ready")
PLAN_ROWS = ("directory", "policy", "models", "workflow")
ADVANCED_IDS = ("harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps")
NETWORK_CALLS = ("fetch(", "XMLHttpRequest", "EventSource", "navigator.sendBeacon")

# The controls this story must leave exactly as reachable as they were. The value is the
# attribute that proves each one still drives what it drove: a handler, a binding, or a
# live event the rest of the pane listens for.
PRESERVED_CONTROLS: dict[str, str] = {
    "harnessWorkspaceInput": 'onchange="syncProjectDirInputs(this.value)"',
    "harnessRecentDirsSelect": 'onchange="if(this.value)saveWorkspaceDirectory(this.value)"',
    "harnessUseWorktree": 'type="checkbox"',
    "harnessLeadModel": 'onchange="handleModelSelectChange(this, patchActiveHarnessSettings)"',
    "harnessCriticModel": 'onchange="handleModelSelectChange(this, patchActiveHarnessSettings)"',
    "harnessPolicy": 'onchange="patchActiveHarnessSettings()"',
    "harnessWorkflowMode": 'onchange="syncHarnessWorkflowModeUI()"',
    "harnessRunBtn": 'onclick="executeHarnessSelectedWorkflow()"',
    "harnessSessionActionSelect": 'onchange="runHarnessSessionAction(this)"',
    "harnessAbortBtn": 'onclick="abortHarnessRun()"',
    "harnessAdvanced": "<details",
}

# Every control in the bar and the composer that carries no visible text of its own, and so
# has to be named by a label or an aria-label. A control a reader cannot name is a control
# a screen reader cannot announce.
NAMED_CONTROLS = (
    "harnessLeadModel",
    "harnessCriticModel",
    "harnessPolicy",
    "harnessEfficiencyMode",
    "harnessReasoning",
    "harnessMaxSteps",
    "harnessWorkflowMode",
    "harnessRecentDirsSelect",
    "harnessSessionActionSelect",
    "harnessPromptInput",
)

# The server routes these controls write to. None of them is new for this story - the
# point is that the plan reads state, and the pane writes through routes that already
# existed and are still mounted.
ROUTES = (
    ("POST", "/api/workspace/directory"),
    ("PATCH", "/api/harness/sessions/{session_id}"),
    ("POST", "/api/harness/run"),
    ("POST", "/api/harness/gsd-wave"),
    ("POST", "/api/harness/sdlc/run"),
)


def function_body(html: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", html)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(html)):
        char = html[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = html.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(html)):
        char = html[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : offset + 1]
    return html[start:]


def element(html: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", html)
    return match.group(0) if match else ""


def array_literal(html: str, name: str) -> list[str]:
    """The string items of ``const NAME = [...]``, in source order."""
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*\[(.*?)\];", html, re.S)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def payload_ids(html: str) -> set[str]:
    """The harness ids the run requests actually post, read from the payload objects."""
    found: set[str] = set()
    for endpoint in ('"/api/harness/run", {', '"/api/harness/gsd-wave", {'):
        start = html.find(endpoint)
        if start < 0:
            continue
        found |= harness_ids(html[start : start + 2200])
    return found


def harness_ids(body: str) -> set[str]:
    """Every ``harness*`` element id a function reads by any of the pane's three idioms."""
    found = set(re.findall(r'getElementById\("(harness[A-Za-z]+)"\)', body))
    found |= set(re.findall(r'harnessSettingValue\("(harness[A-Za-z]+)"\)', body))
    found |= set(re.findall(r'harnessSelectedText\("(harness[A-Za-z]+)"\)', body))
    return found


def read_frontend(html: str) -> dict:
    plan = function_body(html, "harnessRunPlan")
    render_plan = function_body(html, "renderHarnessRunPlan")
    state = function_body(html, "harnessRunState")
    control = function_body(html, "updateHarnessRunControl")
    brief = function_body(html, "renderHarnessIncomingBrief")
    fit = function_body(html, "fitHarnessComposer")
    clear_brief = function_body(html, "clearHarnessIncomingBrief")
    dispatch = function_body(html, "executeHarnessSelectedWorkflow")
    turn = function_body(html, "runHarnessTurn")
    fresh = function_body(html, "startFreshHarnessDraft")
    handoff = function_body(html, "handOffChatToHarness")
    active = function_body(html, "setHarnessRunActive")
    advanced = function_body(html, "syncHarnessWorkflowModeUI")
    advanced_ids = array_literal(html, "HARNESS_ADVANCED_IDS")
    plan_input_ids = array_literal(html, "HARNESS_PLAN_INPUT_IDS")
    # The listener list spreads the advanced ids rather than repeating them, so expand the
    # spread the way the browser does instead of reading only the quoted items.
    spread = re.search(r"HARNESS_PLAN_INPUT_IDS\s*=\s*\[(.*?)\];", html, re.S)
    if spread and "...HARNESS_ADVANCED_IDS" in spread.group(1):
        plan_input_ids = plan_input_ids + list(advanced_ids)

    # The payload the run posts is the authority on what a run depends on.
    posted = payload_ids(html)
    posted.add("harnessWorkflowMode")  # chooses the path, read by the dispatcher
    plan_reads = harness_ids(plan) | set(advanced_ids)

    advanced_markup = {element_id: element(html, element_id) for element_id in ADVANCED_IDS}
    declared = {}
    for element_id, tag in advanced_markup.items():
        match = re.search(r'data-default="([^"]*)"', tag)
        declared[element_id] = match.group(1) if match else ""
    selected_options = {}
    for element_id in ADVANCED_IDS:
        block = re.search(
            rf'<select id="{element_id}"[^>]*>(.*?)</select>', html, re.S
        )
        chosen = re.search(r'<option value="([^"]*)" selected>', block.group(1)) if block else None
        selected_options[element_id] = chosen.group(1) if chosen else ""

    hint = element(html, "harnessRunHint")
    return {
        "functions": {
            name: bool(function_body(html, name))
            for name in (
                "harnessRunPlan",
                "renderHarnessRunPlan",
                "harnessRunState",
                "updateHarnessRunControl",
                "renderHarnessIncomingBrief",
                "clearHarnessIncomingBrief",
                "fitHarnessComposer",
            )
        },
        "plan": {
            # The plan is only as good as its inputs: it must read every control the run
            # payload reads, and it must not read anything else that could go stale.
            "covers_the_run_payload": posted <= plan_reads,
            "watches_the_same_controls": set(plan_input_ids) == posted,
            "one_row_per_decision": all(f'key: "{row}"' in plan for row in PLAN_ROWS),
            "a_row_cannot_be_empty": plan.count("|| ") == 8,
            "directory_says_which_mode": "isolated git worktree" in plan and "· direct" in plan,
            "policy_is_the_option_label": 'harnessSelectedText("harnessPolicy")' in plan
            and "HARNESS_POLICY_EFFECT" not in html,
            "models_name_the_critic": "critic ${" in plan and '"same"' in plan,
            "workflow_carries_the_advanced_values": "advanced.map((item) => item.value)" in plan,
            "changed_is_a_word": "changed.length ? ` · ${changed.length} changed`" in plan,
            "reads_no_session_state": "activeHarnessSession" not in plan
            and "harnessSnapshot" not in plan,
            "no_network": not any(call in plan for call in NETWORK_CALLS),
            "written_only_on_change": "signature !== harnessPlanSignature" in render_plan,
            "rows_carry_keys_for_tests": 'data-plan="${escapeHtml(row.key)}"' in render_plan,
            "changed_is_not_only_colour": 'data-changed="${' in render_plan
            and 'row.changed ? "true" : "false"' in render_plan
            and "changed.length ? ` · ${changed.length} changed`" in plan,
        },
        "advanced": {
            "is_a_details_group": element(html, "harnessAdvanced").startswith("<details"),
            "summary_names_its_contents": bool(
                re.search(
                    r"<summary><span>Run settings</span><span id=\"harnessAdvancedSummary\"",
                    html,
                )
            ),
            "every_setting_declares_its_default": all(declared.values()),
            "declared_default_is_the_selected_option": all(
                declared[element_id] == selected_options[element_id]
                for element_id in ADVANCED_IDS
            ),
            "defaults_are_the_planned_ids": tuple(advanced_ids) == ADVANCED_IDS,
            "summary_is_written_by_the_plan": "summary.textContent !== text" in render_plan,
            "changed_is_marked_in_words": 'plan.changed ? " · changed" : ""' in render_plan,
        },
        "run_control": {
            "one_predicate": "button.disabled = !state.ready;" in control
            and "const state = harnessRunState();" in control,
            "every_reason_declared": all(f'reason: "{reason}"' in state for reason in REASONS),
            "the_hint_is_that_sentence": "hint.textContent !== state.hint" in control
            and "hint.dataset.reason = state.reason;" in control,
            "the_hint_is_a_live_region": 'role="status"' in hint
            and 'aria-live="polite"' in hint,
            "the_composer_is_described_by_it": 'aria-describedby="harnessRunHint"'
            in element(html, "harnessPromptInput"),
            "no_second_run_while_running": state.strip().find("if (harnessRunActive)") >= 0
            and state.strip().find("if (harnessRunActive)")
            < state.strip().find("if (!task)"),
            "the_board_needs_no_task": 'plan.mode === "sdlc_modal"' in state,
            "no_task_is_not_ready": "ready: false" in state.split('if (!task)')[1][:400],
            "the_dispatcher_asks_first": "const state = harnessRunState();" in dispatch
            and "if (!state.ready) {" in dispatch
            and dispatch.find("const state = harnessRunState();")
            < dispatch.find("runHarnessTurn()"),
            "an_empty_task_is_visible_not_silent": "updateHarnessRunControl();" in turn
            and "if (!prompt) {" in turn,
            "every_path_still_dispatches": all(
                needle in dispatch
                for needle in (
                    "runGsdWaveTurn()",
                    'startSdlcRun("full")',
                    "openSdlcModal()",
                    "runHarnessTurn()",
                )
            ),
            "state_follows_the_run": "updateHarnessRunControl();" in active,
            "state_follows_the_mode": "updateHarnessRunControl();" in advanced,
            "wire_covers_the_controls": "for (const id of HARNESS_PLAN_INPUT_IDS)" in html,
        },
        "brief": {
            "arrives_hidden": "hidden" in element(html, "harnessIncomingBrief"),
            "appears_only_with_a_brief": "if (!harnessIncomingBrief) {" in brief
            and "host.hidden = true;" in brief,
            "counts_what_it_carried": 'chatCount(carried.messages, "message")' in brief
            and 'chatCount(carried.calls, "tool call")' in brief,
            "compares_the_model": "context.model !== runPlan.lead" in brief,
            "compares_the_directory": "context.workspace_effective !== runPlan.dir" in brief,
            "says_nothing_runs": "Nothing starts until you press Run." in brief,
            "says_when_nothing_differs": "same model and project directory" in brief,
            "written_only_on_change": "if (text.textContent !== sentence)" in brief,
            "handoff_sets_it": "harnessIncomingBrief = {" in handoff,
            "a_run_consumes_it": 'if (mode !== "sdlc_modal") clearHarnessIncomingBrief();' in dispatch,
            "a_fresh_draft_clears_it": "clearHarnessIncomingBrief();" in fresh,
            "an_emptied_box_clears_it": "clearHarnessIncomingBrief();" in html
            and "!box.value.trim()" in html,
            "clearing_is_idempotent": "if (!harnessIncomingBrief) return;" in clear_brief,
        },
        "composer": {
            "fits_the_task_it_holds": "box.scrollHeight" in fit
            and "box.style.height = `${" in fit,
            "has_a_floor_and_a_ceiling": "lines * 2 + 12" in fit and "lines * 10 + 12" in fit,
            "follows_the_text": "fitHarnessComposer();" in html
            and html.count("fitHarnessComposer();") >= 3,
            "rests_after_a_run": "fitHarnessComposer();" in turn,
            "manual_resize_is_off": "resize:none" in element(html, "harnessPromptInput"),
        },
        "preserved": {
            element_id: needle in element(html, element_id)
            for element_id, needle in PRESERVED_CONTROLS.items()
        },
        "labelling": {
            element_id: bool(
                re.search(rf'<label[^>]*for="{element_id}"', html)
                or "aria-label=" in element(html, element_id)
            )
            for element_id in NAMED_CONTROLS
        },
        "styles": {
            "plan": ".harness-plan {" in html,
            "plan_rows": ".harness-plan-row[data-changed=\"true\"]" in html,
            "hint": ".harness-run-hint {" in html,
            "hint_tones": '.harness-run-hint[data-reason="no-task"]' in html
            and '.harness-run-hint[data-reason="ready"]' in html,
            "advanced_summary": ".harness-advanced-summary {" in html
            and '.harness-advanced-summary[data-changed="true"]' in html,
            "brief": ".harness-brief {" in html
            and '.harness-brief[data-match="mismatch"]' in html,
            "long_paths_wrap": "overflow-wrap: anywhere" in html,
        },
        "ids_in_the_payload": sorted(posted),
        "ids_the_plan_reads": sorted(plan_reads),
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    for flag, description in (
        (
            "covers_the_run_payload",
            "the plan does not read a control the run payload sends, so it can describe a run this pane would not start",
        ),
        (
            "watches_the_same_controls",
            "the plan is not watching the same controls the payload reads, so a change can leave it stale",
        ),
        ("one_row_per_decision", "the plan is missing one of its four decisions"),
        (
            "a_row_cannot_be_empty",
            "a plan row lost a fallback, so it can print nothing at all",
        ),
        (
            "directory_says_which_mode",
            "the directory row does not say whether the run is direct or isolated",
        ),
        (
            "policy_is_the_option_label",
            "the policy's meaning is described somewhere other than the option a reader selected",
        ),
        ("models_name_the_critic", "the models row does not say who the sub-agent critic is"),
        (
            "workflow_carries_the_advanced_values",
            "the workflow row hides the advanced settings it depends on",
        ),
        ("changed_is_a_word", "a changed setting is signalled without saying so"),
        (
            "reads_no_session_state",
            "the plan is built from remembered session state rather than the controls",
        ),
        ("no_network", "the plan touches the network"),
        ("written_only_on_change", "the plan is rewritten on every render"),
        ("rows_carry_keys_for_tests", "the plan's rows carry no key, so nothing can find them"),
        ("changed_is_not_only_colour", "a changed row is marked by colour alone"),
    ):
        if not frontend["plan"].get(flag):
            fail("plan", description)

    for flag, description in (
        ("is_a_details_group", "the advanced settings are no longer a disclosure group"),
        (
            "summary_names_its_contents",
            "the collapsed group no longer states what a reader is not seeing",
        ),
        (
            "every_setting_declares_its_default",
            "an advanced setting does not declare the value 'changed' is measured against",
        ),
        (
            "declared_default_is_the_selected_option",
            "a declared default is not the option the markup ships selected",
        ),
        ("defaults_are_the_planned_ids", "the advanced id list and the markup disagree"),
        ("summary_is_written_by_the_plan", "the group's summary is not derived from the plan"),
        ("changed_is_marked_in_words", "the group's summary does not say that it was changed"),
    ):
        if not frontend["advanced"].get(flag):
            fail("advanced", description)

    for flag, description in (
        ("one_predicate", "Run's disabled state and its sentence are not the same answer"),
        ("every_reason_declared", "a Run reason is declared nowhere"),
        ("the_hint_is_that_sentence", "the sentence beside Run is not the predicate's own"),
        ("the_hint_is_a_live_region", "the sentence beside Run is not announced when it changes"),
        (
            "the_composer_is_described_by_it",
            "the composer does not point at the sentence that explains Run",
        ),
        (
            "no_second_run_while_running",
            "a second run can start while one is in flight",
        ),
        ("the_board_needs_no_task", "the board mode is treated as if it needed a task"),
        ("no_task_is_not_ready", "Run can be pressed with no task"),
        ("the_dispatcher_asks_first", "Run's entry point does not consult the predicate"),
        (
            "an_empty_task_is_visible_not_silent",
            "an empty task returns without saying anything",
        ),
        ("every_path_still_dispatches", "a workflow mode no longer reaches its own path"),
        ("state_follows_the_run", "Run is not re-evaluated when a run starts or ends"),
        ("state_follows_the_mode", "Run is not re-evaluated when the workflow mode changes"),
        ("wire_covers_the_controls", "nothing updates the plan as the controls change"),
    ):
        if not frontend["run_control"].get(flag):
            fail("run-control", description)

    for flag, description in (
        ("arrives_hidden", "the brief banner is visible before a brief has arrived"),
        ("appears_only_with_a_brief", "the banner renders without a brief"),
        ("counts_what_it_carried", "the banner does not say what Chat carried"),
        ("compares_the_model", "the banner does not compare the model"),
        ("compares_the_directory", "the banner does not compare the project directory"),
        ("says_nothing_runs", "the banner does not say that nothing starts on its own"),
        ("says_when_nothing_differs", "the banner cannot say that this run matches the brief"),
        ("written_only_on_change", "the banner is rewritten on every render"),
        ("handoff_sets_it", "Chat's handoff does not say what it carried"),
        ("a_run_consumes_it", "the banner outlives the task it describes"),
        ("a_fresh_draft_clears_it", "a new draft keeps the previous conversation's banner"),
        ("an_emptied_box_clears_it", "emptying the composer keeps a brief it no longer holds"),
        ("clearing_is_idempotent", "clearing the banner twice would rewrite it"),
    ):
        if not frontend["brief"].get(flag):
            fail("brief", description)

    for flag, description in (
        (
            "fits_the_task_it_holds",
            "the task box does not grow to show the task it holds, so a carried brief is scrolled rather than read",
        ),
        (
            "has_a_floor_and_a_ceiling",
            "the task box has no floor or no ceiling, so it can collapse or take the whole pane",
        ),
        ("follows_the_text", "the task box is not fitted as the text changes"),
        ("rests_after_a_run", "the task box keeps the height of a task that has already run"),
        (
            "manual_resize_is_off",
            "the task box can still be dragged, so a reader's drag fights the fitter",
        ),
    ):
        if not frontend["composer"].get(flag):
            fail("composer", description)

    for element_id, present in frontend["preserved"].items():
        if not present:
            fail(
                "control-lost",
                f"{element_id} no longer carries the handler or binding that made it work",
            )
    for element_id, named in frontend["labelling"].items():
        if not named:
            fail(
                "unnamed-control",
                f"{element_id} has no label and no aria-label, so nothing can name it",
            )

    for flag, description in (
        ("plan", "the plan has no rule"),
        ("plan_rows", "a changed plan row has no rule"),
        ("hint", "the sentence beside Run has no rule"),
        ("hint_tones", "a Run reason has no tone"),
        ("advanced_summary", "the group's summary has no rule"),
        ("brief", "the brief banner has no rule"),
        ("long_paths_wrap", "a long project path has nothing to wrap it"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    routes = []
    for method, path in ROUTES:
        pattern = rf'@app\.{method.lower()}\("{re.escape(path)}"\)'
        mounted = bool(re.search(pattern, server))
        routes.append({"method": method, "path": path, "mounted": mounted})
        if not mounted:
            fail("route-missing", f"{method} {path} is no longer mounted, so a mapped control writes nowhere")
    if "/api/harness/settings" in html or "/api/harness/settings" in server:
        fail(
            "new-route",
            "a settings route exists; the plan reads the controls the existing payload already sends",
        )

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P3-01",
        "statement": (
            "The Harness prints what Run will do before it is pressed - the project directory and whether it is an "
            "isolated worktree, the execution policy, the lead model and the sub-agent critic, and the workflow with "
            "the three settings that used to hide inside a collapsed group - read from the controls the run payload "
            "reads; the collapsed group states its own contents and says in words when one was changed; Run's "
            "disabled state and the sentence beside it come from one predicate, so a missing task is a sentence "
            "rather than silence and a second run cannot start while one is in flight; and when Chat hands a "
            "conversation over, a banner says what it carried and where this run departs from it, and goes when the "
            "task it describes is consumed."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py"},
        "reasons": list(REASONS),
        "plan_rows": list(PLAN_ROWS),
        "routes": routes,
        "frontend": frontend,
        "totals": {
            "reasons": len(REASONS),
            "plan_rows": len(PLAN_ROWS),
            "preserved_controls": len(PRESERVED_CONTROLS),
            "named_controls": len(NAMED_CONTROLS),
            "checks": sum(
                len(frontend[key])
                for key in (
                    "plan",
                    "advanced",
                    "run_control",
                    "brief",
                    "composer",
                    "preserved",
                    "labelling",
                    "styles",
                )
            ),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the plan describes the payload, not the server's reading of it: an empty directory is sent empty and "
            "the run falls back to the configured project root, which the row states rather than guessing",
            "the plan covers the turn path's payload in full and the GSD and SDLC paths' extra fields (wave plan, "
            "story count) stay in their own surfaces; the nine controls they share with the turn are the ones the "
            "plan prints",
            "the HARNESS_POLICY_EFFECT table this story first introduced was removed: the selected option's own "
            "label is the description, so there is exactly one sentence about what a policy permits",
            "the advanced settings are summarised in the workflow row rather than given a fifth row, because they "
            "shape the same decision and a longer plan is a plan a reader skips",
            "the brief banner is a carry, not a promise: it compares the model and the directory the reply was sent "
            "with against the plan as it stands now, and says 'changes' when they differ - it never claims the "
            "conversation will fit",
        ],
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# The Harness: what Run will do, before you press it",
        "",
        "Generated by `python3 scripts/uiux_harness_settings_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## Why the plan is printed",
        "",
        "Nine controls decide a Harness run, and before this story none of them said what the run would do with them.",
        "A reader had to know already that `plan` means read-only, that the critic is a sub-agent the lead delegates",
        "to, and that three more settings were folded into a group that said only \"Run settings\". The composer",
        "now prints the plan above Run:",
        "",
        "| Row | What it prints | Where it is read from |",
        "|---|---|---|",
        "| `directory` | the project directory, and whether the run is `direct` or an `isolated git worktree`; an empty box says \"the server's default project directory\" rather than nothing | `#harnessWorkspaceInput`, `#harnessUseWorktree` |",
        "| `policy` | the selected option's own label, so there is one sentence about what a policy permits | `#harnessPolicy` |",
        "| `models` | the lead model, and the critic (or `same`, decided by comparing the two values, not two labels) | `#harnessLeadModel`, `#harnessCriticModel` |",
        "| `workflow` | the workflow mode with the advanced settings it depends on, and `N changed` in words when one differs from the value the control declares | `#harnessWorkflowMode`, `#harnessEfficiencyMode`, `#harnessReasoning`, `#harnessMaxSteps` |",
        "",
        "The inventory fails if the plan stops reading a control the run payload sends (`covers_the_run_payload`), if",
        "the ids the plan watches and the ids the payload reads diverge (`watches_the_same_controls`), if a row can",
        "print nothing, if the plan starts reading `activeHarnessSession` or `harnessSnapshot` instead of the",
        "controls, or if \"changed\" is signalled by colour alone.",
        "",
        "## The settings that used to hide",
        "",
        "`<details id=\"harnessAdvanced\">` is still a disclosure group, and still the only place those three settings",
        "live. What changed is that its summary now carries its contents - `Ponytail + Concise · Low · 10 steps`, and",
        "`· changed` when one of them differs from the `data-default` its own markup declares. A reader who never",
        "opens the group is no longer missing a decision, and the default is not a constant copied into the script",
        "that could drift from the option the markup ships selected.",
        "",
        "## One predicate for Run",
        "",
        "`harnessRunState()` is the only answer to \"can this run?\", exactly as `chatSendReason()` is for Send. The",
        "button's `disabled` state and the polite live region beside it are the same answer, and the composer points",
        "at that sentence with `aria-describedby`:",
        "",
        "| Reason | When | What the reader is told |",
        "|---|---|---|",
        "| `running` | a run is in flight | Running; Abort stops it and keeps what already streamed, and the run keeps the settings it started with |",
        "| `board` | the workflow mode is the SDLC board | no task is needed here, and nothing runs until you start something in the board |",
        "| `no-task` | the composer is empty | describe the task first; nothing runs until there is one |",
        "| `ready` | there is a task | Run starts one task with the plan above |",
        "",
        "This closed a real inconsistency rather than adding opinion. An empty task used to be a toast in the GSD",
        "path, a different toast in the SDLC path, and a silent `return` in the turn path - three answers to one",
        "question, one of them no answer at all. `executeHarnessSelectedWorkflow()` now consults the predicate before",
        "it dispatches, so Enter in an empty composer says what Run says; and because `harnessRunActive` is the first",
        "branch of the predicate, pressing Enter during a run can no longer start a second one.",
        "",
        "## The task box grows with the task",
        "",
        "A carry is a paragraph, and the composer was two rows: the P2-06 brief arrived in a box that showed its first",
        "two lines and hid the rest. `fitHarnessComposer()` now sizes the box to its content between a two-row floor",
        "and a ten-row ceiling, follows it as the reader types, and returns it to rest when a run takes the task",
        "away. Manual resizing stays off (`resize:none`), because a drag would fight the fitter.",
        "",
        "## The brief from Chat",
        "",
        "UIUX-P2-06 carries a conversation into this composer and stops. This pane now answers it: a banner (hidden",
        "until a brief actually arrives) says how many messages and tool calls were carried, whether any failed, and",
        "where this run departs from them -",
        "",
        "> Carried 2 messages, 10 tool calls (1 failed). This run changes one setting: the project directory (/tmp/a ->",
        "> /tmp/b). Nothing starts until you press Run.",
        "",
        "It compares the recorded context's model and effective directory against the plan as it stands, so changing a",
        "control after the handoff updates the sentence instead of leaving a stale claim. It goes when the task it",
        "describes goes: a run consumes it, **+ New session** clears it, and emptying the composer clears it.",
        "",
        "## The controls this story had to leave alone",
        "",
        (
            f"Every control in the bar, the sidebar and the composer keeps the handler it had - the inventory names "
            f"each of them with the attribute that proves it ({len(PRESERVED_CONTROLS)} controls), because \"still "
            f"reachable and functionally equivalent\" is the acceptance criterion this story is judged on. The one "
            f"markup change outside the plan is accessibility: four `<label>`s in the bar had no `for`, so the lead "
            f"model, the critic, the policy and the max steps were named by nothing, and the task box itself had "
            f"no name at all. Every control in the bar and the composer that carries no visible text of its own "
            f"({len(NAMED_CONTROLS)}) is now named by a label or an `aria-label`, and the inventory fails on one "
            f"that is not."
        ),
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        f"`--check` re-derives {payload['totals']['checks']} checks from the source and fails on any of them. "
        "`node scripts/harness_settings_walk.mjs` drives the same surface in a browser: it reads the plan, changes",
        "each control and re-reads it, collapses and reopens the advanced group, presses Run with no task, hands a",
        "conversation over from Chat to reach the banner, and photographs the states in",
        "[`media/harness/`](media/harness/README.md).",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`.",
        "2. `python3 scripts/uiux_harness_settings_inventory.py`.",
        "3. `node scripts/harness_settings_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the artifacts drifted or a rule broke",
    )
    parser.add_argument("--json", action="store_true", help="print the inventory")
    args = parser.parse_args(argv)

    payload = build_inventory()
    text = dumps(payload)
    doc = render_doc(payload)

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        problems = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is stale")
        if problems:
            print("error: the Harness settings inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_harness_settings_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the Harness's task creation and settings hierarchy disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"harness settings OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['reasons']} reasons, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['checks']} checks, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
