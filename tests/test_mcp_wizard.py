"""UIV2-22: the MCP guided connect wizard.

The browser walk in `scripts/mcp_wizard_walk.mjs` is what proves the guide on a screen — four steps,
a refusal that names the field, a password field that is never pre-filled, a review that never echoes
the token, a consent that is ticked rather than assumed, and a connection test whose result appears
where the button is. This file proves that the rules the walk relies on are still in the source, that
breaking one of them becomes a finding rather than a silent regression, that the walk can photograph
the form this story replaced, that the captures are the ones the story claims, that nothing the
story may not touch moved, and that the ledger records it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "mcp_wizard_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_mcp_wizard_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Wizard.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-2-mcp-wizard.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "mcp-wizard-uiv2-22"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
STATES = ("empty", "credentials", "policy", "review", "loading", "success", "error", "recovery", "keyboard", "long")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_mcp_wizard_inventory", INVENTORY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module() -> ModuleType:
    return _load_module()


@pytest.fixture(scope="module")
def payload(module: ModuleType) -> dict:
    return module.build_inventory()


@pytest.fixture
def sources():
    """`build_inventory` over the real sources, with edits made and then undone."""

    def factory():
        module = _load_module()
        originals = {path: path.read_text(encoding="utf-8") for path in (FRONTEND, APP)}

        class Sources:
            def findings(self, *replacements):
                for path, original in originals.items():
                    path.write_text(original, encoding="utf-8")
                for path, old, new in replacements:
                    edited = path.read_text(encoding="utf-8")
                    assert old in edited, f"{path.name} no longer contains {old[:70]!r}"
                    path.write_text(edited.replace(old, new, 1), encoding="utf-8")
                try:
                    return {entry["check"] for entry in module.build_inventory()["findings"]}
                finally:
                    for path, original in originals.items():
                        path.write_text(original, encoding="utf-8")

        return Sources()

    return factory()


# ── the artifact ─────────────────────────────────────────────────────────────


def test_the_inventory_matches_the_source(module, payload):
    assert JSON_PATH.read_text(encoding="utf-8") == module.dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == module.render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["check_count"] == len(payload["checks"]) >= 30
    assert all(entry["passed"] for entry in payload["checks"])


def test_the_guide_asks_four_questions_in_order(payload):
    assert [step["id"] for step in payload["steps"]] == [1, 2, 3, 4]
    assert [step["label"] for step in payload["steps"]] == [
        "Server",
        "Transport and credentials",
        "Policy and consent",
        "Review and connect",
    ]


def test_the_inventory_names_the_controls_it_preserves_and_the_ones_it_adds(payload):
    assert len(payload["controls"]["preserved"]) == 20
    assert "mcpStdioConsent" in payload["controls"]["wizard"]
    assert "mcpReviewList" in payload["controls"]["wizard"]
    assert "mcpWizardResult" in payload["controls"]["wizard"]


# ── what the walk has to be able to do ───────────────────────────────────────


def test_the_walk_covers_every_state_and_records_its_checks():
    walk = WALK.read_text(encoding="utf-8")
    for state in STATES:
        assert f'"{state}"' in walk, f"the walk never mentions the {state} state"
    assert walk.count("record(") >= 30
    assert "secretInDom" in walk and "secretConsole" in walk


def test_the_walk_opts_into_stdio_and_a_memory_keyring_only_for_its_own_fixture():
    walk = WALK.read_text(encoding="utf-8")
    assert 'STUDIO_ALLOW_MCP_STDIO: "1"' in walk
    assert "sitecustomize.py" in walk
    assert "keyring.set_password = _set" in walk
    # The consent the guide asks for is additional to the operator's opt-in, never instead of it:
    # the page may name the variable in the sentence that disclaims it, and may do nothing else.
    html = FRONTEND.read_text(encoding="utf-8")
    assert html.count("STUDIO_ALLOW_MCP_STDIO") == 1
    assert "it cannot grant that permission" in html
    assert '"STUDIO_ALLOW_MCP_STDIO"' not in html
    assert "env.STUDIO_ALLOW_MCP_STDIO" not in html


def test_the_walk_holds_the_save_in_flight_to_photograph_the_testing_state():
    walk = WALK.read_text(encoding="utf-8")
    assert "setRequestInterception(true)" in walk
    assert "async function holdSave" in walk
    assert "data-state" in walk or "dataset.state" in walk


def test_the_walk_can_photograph_the_old_one_shot_form_too():
    walk = WALK.read_text(encoding="utf-8")
    # A "before" set is only honest if the same instrument can run against the pre-wizard editor.
    assert "--capture-only" in walk
    assert "captureOnly" in walk
    assert 'typeof mcpWizardGo === "function"' in walk
    before = sorted((MEDIA / "before-uiv2-22").rglob("*.png"))
    assert len(before) >= 16


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_two_visible_steps_are_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'id="mcpStep2" data-wizard-step="2" role="group" aria-labelledby="mcpWizardHeading2" hidden>',
         'id="mcpStep2" data-wizard-step="2" role="group" aria-labelledby="mcpWizardHeading2">'),
    )
    assert "one step is on screen at a time, and starting over goes back to the first" in findings


def test_a_step_that_advances_without_being_satisfied_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "if (mcpWizardProblem(mcpWizardStep)) return false;", "void mcpWizardStep;"),
    )
    assert "no step can advance until it is satisfied, and the refusal is words, not a shake" in findings


def test_a_save_that_skips_the_guard_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "for (const step of [1, 2, 3]) {", "for (const step of []) {"),
    )
    assert "saving re-checks every earlier step, so a field edited after passing it cannot slip through" in findings


def test_a_review_that_echoes_the_token_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'const typedToken = Boolean(mcpWizardField("mcpToken") && mcpWizardField("mcpToken").value);',
         'const typedToken = mcpWizardField("mcpToken") && mcpWizardField("mcpToken").value;'),
    )
    assert "the review never reads the typed token back out" in findings


def test_a_consent_that_is_assumed_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'if (!consent || !consent.checked) return "Read the command and tick the consent box: this server runs a program on your machine.";',
         'if (!consent) return "Read the command and tick the consent box: this server runs a program on your machine.";'),
    )
    assert "a local command needs consent that is given, not implied" in findings


def test_a_consent_that_claims_to_grant_permission_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "This screen records your consent; it cannot grant that permission, and it does not weaken the sandbox.",
         "This screen grants you permission to run it."),
    )
    assert "the consent says it cannot grant the operator's permission" in findings


def test_a_token_field_that_is_not_masked_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'type="password" id="mcpToken" autocomplete="off"', 'type="text" id="mcpToken" autocomplete="off"'),
    )
    assert "the token is a password field that is never pre-filled or auto-completed" in findings


def test_raw_json_on_the_guided_path_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<details class="mcp-advanced" id="mcpAdvancedJson">', '<details class="mcp-advanced" id="mcpAdvancedJson" open>'),
    )
    assert "raw headers and environment JSON are one closed disclosure, off the guided path" in findings


def test_a_result_that_is_never_rendered_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "Saved, but the connection test failed — ${d.sync_error}", "Saved."),
    )
    assert "the result is visible where the button is, and says what was found or what failed" in findings


def test_a_policy_without_its_cost_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'if (policy === "always_ask") return "Every call waits for your approval in the Harness before it runs.";',
         'if (policy === "always_ask") return "";'),
    )
    assert "the policy step explains what each policy costs" in findings


def test_a_rail_that_does_not_say_where_you_are_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'item.setAttribute("aria-current", entry.id === target ? "step" : "false");', "void entry.id;"),
    )
    assert "the rail says where you are, in a live region, and marks the step it is on" in findings


def test_an_added_mcp_route_is_a_finding(sources):
    findings = sources.findings(
        (APP, '@app.post("/api/mcp/servers/{alias}/call")', '@app.post("/api/mcp/servers/{alias}/call")\n@app.post("/api/mcp/anything")'),
    )
    assert "the six MCP routes are unchanged and none were added" in findings


def test_a_page_that_masks_a_secret_itself_is_a_finding(sources):
    findings = sources.findings(
        (APP, 'd.pop("auth_token"', 'd.get("auth_token"'),
    )
    assert "the served payload is what masks a secret, not the page" in findings


def test_a_guide_that_drops_a_control_the_old_form_had_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'id="mcpEnvJson"', 'id="mcpEnvJsonGone"'),
    )
    assert "every control the old form had is still in the guide" in findings


# ── what this story may not change ──────────────────────────────────────────


def test_the_stdio_launch_gate_is_untouched():
    app = APP.read_text(encoding="utf-8")
    assert app.count("STUDIO_ALLOW_MCP_STDIO") >= 2
    assert "MCP stdio process launch is disabled by default" in app


def test_the_workspace_still_has_all_of_its_panes():
    html = FRONTEND.read_text(encoding="utf-8")
    for pane in ("pane-start", "pane-harness", "pane-hall", "pane-council", "pane-debate", "pane-chat"):
        assert f'id="{pane}"' in html


def test_no_react_or_new_dependency_is_introduced():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "react" not in pyproject.lower()
    assert "react-dom" not in FRONTEND.read_text(encoding="utf-8")


def test_the_hub_this_story_edits_is_still_a_hub():
    html = FRONTEND.read_text(encoding="utf-8")
    assert html.count('role="tablist"') >= 1
    assert 'id="mcpCatalogGrid"' in html
    assert 'id="mcpHubSearch"' in html


# ── the evidence ────────────────────────────────────────────────────────────


def test_the_captures_are_on_disk_for_each_viewport_and_theme():
    pngs = sorted(MEDIA.glob("*.png"))
    assert len(pngs) >= 80
    for viewport in VIEWPORTS:
        for theme in THEMES:
            for state in STATES:
                assert (MEDIA / f"mcp-wizard-{state}-{viewport}-{theme}.png").exists(), (
                    f"missing mcp-wizard-{state}-{viewport}-{theme}.png"
                )


def test_the_before_set_covers_the_same_matrix():
    pngs = sorted((MEDIA / "before-uiv2-22").rglob("*.png"))
    assert len(pngs) >= 80
    for viewport in VIEWPORTS:
        for theme in THEMES:
            for state in STATES:
                assert (MEDIA / "before-uiv2-22" / f"mcp-wizard-{state}-{viewport}-{theme}.png").exists()


def test_the_media_readme_says_what_each_state_is_evidence_for():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    for state in STATES:
        assert f"`{state}`" in readme
    assert "before-uiv2-22" in readme
    assert "38/38" in readme


def test_the_doc_states_each_step_and_rule(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    for step in payload["steps"]:
        assert step["label"].replace(" and ", " & ") in doc
    assert "STUDIO_ALLOW_MCP_STDIO" in doc
    assert "unchanged-token" in doc


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-22")
    assert story["tracking"]["status"] == "done"
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]
    assert story["tracking"]["acceptance_evidence"]
    assert story["tracking"]["blockers"] == []


def test_the_ledger_still_holds_the_story_this_one_depends_on():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-21")
    assert story["tracking"]["status"] == "done"
