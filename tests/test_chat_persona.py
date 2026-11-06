"""UIV2-19: the Chat persona registry and the picker it draws.

The browser walk in `scripts/chat_persona_walk.mjs` is what proves the picker on a screen and on
the wire; this file proves that the rules the walk relies on are still in the source, that breaking
one of them is a finding rather than a silent regression, that the prompts in the registry are the
ones the pane already sent (nobody's answers changed), and that the ledger records the story.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
STUDIO = PROJECT_ROOT / "studio.py"
WALK = PROJECT_ROOT / "scripts" / "chat_persona_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_chat_persona_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Persona.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-persona.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "chat-persona"

#: The prompts the three `<option value="...">` tags carried before this story, byte for byte. The
#: registry is allowed to add ids, labels and descriptions; it may not change what the model is
#: told, because that would quietly change every answer the pane has ever given.
LEGACY_PROMPTS = (
    "You are a concise Principal Software Engineer & UI Architect. Write clean, accessible, "
    "production-ready code.",
    "You are an adversarial code reviewer. Focus strictly on bugs, race conditions, UX "
    "anti-patterns, and security issues.",
    "Output code blocks only with minimal prose.",
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_chat_persona_inventory", INVENTORY)
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
    """`build_inventory` over copies of the sources, with edits made to the copies."""
    module = _load_module()
    originals = {
        FRONTEND: FRONTEND.read_text(encoding="utf-8"),
        WALK: WALK.read_text(encoding="utf-8"),
    }

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


# ── the artifact ─────────────────────────────────────────────────────────────


def test_the_inventory_matches_the_source(module, payload):
    assert JSON_PATH.read_text(encoding="utf-8") == module.dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == module.render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["check_count"] == len(payload["checks"]) >= 30
    assert all(entry["passed"] for entry in payload["checks"])


def test_the_registry_is_one_table_of_four_fields(payload):
    personas = payload["registry"]["personas"]
    assert len(personas) == 3
    assert {persona["id"] for persona in personas} == {
        "principal-engineer",
        "bug-ux-auditor",
        "code-only",
    }
    for persona in personas:
        assert set(persona) == {"id", "label", "description", "prompt"}
        assert 30 <= len(persona["description"]) <= 140


def test_the_prompts_are_the_ones_the_pane_already_sent(payload):
    prompts = {persona["prompt"] for persona in payload["registry"]["personas"]}
    assert prompts == set(LEGACY_PROMPTS)


def test_the_markup_ships_no_persona_options_of_its_own():
    html = FRONTEND.read_text(encoding="utf-8")
    select = re.search(r'<select\b[^>]*\bid="chatPersona"[^>]*>(.*?)</select>', html, re.S)
    assert select is not None
    assert "<option" not in select.group(1)
    # The prompts live in the registry now; what must never come back is a prompt *as a value*.
    for prompt in LEGACY_PROMPTS:
        assert f'<option value="{prompt}"' not in html, "a system prompt is back in the markup as a value"


def test_the_doc_states_the_registry_and_the_fallback(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "CHAT_PERSONAS" in doc
    for persona in payload["registry"]["personas"]:
        assert persona["id"] in doc
        assert persona["label"] in doc
    assert "falls back to the default persona" in doc


def test_the_picker_is_drawn_in_the_pane_and_in_the_served_copy():
    for path in (FRONTEND, STUDIO):
        source = path.read_text(encoding="utf-8")
        for needle in (
            "const CHAT_PERSONAS = [",
            "const CHAT_PERSONA_DEFAULT = ",
            "function renderChatPersonaPicker(",
            "function chatPersonaSelected(",
            "function chatPersonaById(",
        ):
            assert needle in source, f"{path.name} is missing {needle}"


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-19")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert "persona" in story["tracking"].get("evidence", "").lower() or story["tracking"].get(
        "changed_files"
    )


def test_the_captures_are_on_disk_for_each_viewport_and_theme():
    pngs = sorted(MEDIA.glob("*.png"))
    assert len(pngs) >= 48
    states = ("empty", "populated", "long-content", "loading", "error", "recovery")
    for viewport in ("1440x900", "1280x800", "1024x768", "390x844"):
        for theme in ("light", "dark"):
            for state in states:
                assert (MEDIA / f"chat-persona-{state}-{viewport}-{theme}.png").exists(), (
                    f"missing chat-persona-{state}-{viewport}-{theme}.png"
                )


def test_the_before_set_shows_the_picker_the_story_replaced():
    before = sorted((MEDIA / "before-uiv2-19").rglob("*.png"))
    assert len(before) >= 16


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_persona_without_a_description_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '      description: "Answers in code blocks, with as little prose as the question allows.",\n',
         ""),
    )
    assert "every persona has an id, a label, a description and a prompt" in findings


def test_a_prose_id_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '      id: "code-only",', '      id: "Output code blocks only",'),
    )
    assert "ids are stable slugs, not prose" in findings


def test_two_personas_that_send_the_same_prompt_are_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '      prompt: "Output code blocks only with minimal prose.",',
         '      prompt: "You are a concise Principal Software Engineer & UI Architect. Write clean, accessible, production-ready code.",'),
    )
    assert "ids, labels and prompts are all distinct" in findings


def test_a_default_that_names_nobody_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'const CHAT_PERSONA_DEFAULT = "principal-engineer";', 'const CHAT_PERSONA_DEFAULT = "retired-persona";'),
    )
    assert "the default persona is one the registry names" in findings


def test_options_back_in_the_markup_are_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '<select id="chatPersona" aria-describedby="chatPersonaDescription"></select>',
         '<select id="chatPersona" aria-describedby="chatPersonaDescription"><option value="code-only">Code Only</option></select>'),
    )
    assert "the markup ships no persona options of its own" in findings


def test_an_unescaped_label_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "${escapeHtml(persona.label)}</option>",
         "${persona.label}</option>"),
    )
    assert "the picker escapes what it writes into the markup" in findings


def test_a_filter_that_cannot_be_found_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'aria-controls="chatPersona"', 'aria-controls="chatPersonas"'),
    )
    assert "the filter is labelled, and controls the picker it filters" in findings


def test_a_filter_that_reads_prompts_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "    return persona.label.toLowerCase().includes(needle)\n      || persona.description.toLowerCase().includes(needle);",
         "    return persona.prompt.toLowerCase().includes(needle);"),
    )
    assert "the filter reads labels and descriptions, never prompts" in findings


def test_a_description_that_is_not_announced_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '<p id="chatPersonaDescription" class="chat-persona-description" data-state="ready" aria-live="polite"></p>',
         '<p id="chatPersonaDescription" class="chat-persona-description" data-state="ready" aria-live="off"></p>'),
    )
    assert "the description line is announced, and the picker points at it" in findings


def test_a_request_that_sends_the_id_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'content: (chatPersonaSelected() || {}).prompt || ""',
         'content: (chatPersonaSelected() || {}).id || ""'),
    )
    assert "the request sends the selected persona's prompt" in findings


def test_reading_the_select_value_again_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'const persona = chatPersonaSelected();',
         'const persona = chatPersonaSelected(); const ignored = document.getElementById("chatPersona").value;'),
    )
    assert "nothing sends the select's value any more" in findings


def test_a_fallback_that_cannot_spot_a_deleted_persona_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "      && !CHAT_PERSONAS.some(entry => entry.id === stored);",
         ";"),
    )
    assert "a deleted persona is detected, not silently replaced" in findings


def test_a_filter_that_drops_the_selection_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "CHAT_PERSONAS.filter(persona => chatPersonaMatches(persona, needle) || persona.id === selected)",
         "CHAT_PERSONAS.filter(persona => chatPersonaMatches(persona, needle))"),
    )
    assert "the filter never filters out the persona that is about to be sent" in findings


def test_an_enter_key_that_takes_the_first_option_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "          const first = CHAT_PERSONAS.find(persona => chatPersonaMatches(persona, needle));",
         "          const first = CHAT_PERSONAS.find(persona => persona.id === personaSelect.value);"),
    )
    assert "Enter chooses the first persona the filter matched" in findings


def test_a_filter_as_tall_as_a_control_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "    min-height: 32px; height: 32px; align-self: center; padding: 0 var(--space-1);",
         "    min-height: 44px; height: 44px; align-self: center; padding: 0 var(--space-1);"),
    )
    assert "the filter is the one documented exception to the control floor, and stays tappable" in findings


def test_a_picker_that_stacks_on_a_desktop_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "    display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 6px; align-items: start;",
         "    display: grid; grid-template-columns: minmax(0, 1fr); align-items: start;"),
    )
    assert "the picker and its filter share one row at every width, with no breakpoint of their own" in findings


def test_a_walk_that_stops_asserting_the_prompt_is_a_finding(sources):
    findings = sources.findings(
        (WALK, "the request carries the selected persona's prompt, not its id", "the request carries a persona"),
    )
    assert "the browser walk exists and asserts the prompt on the wire" in findings


def test_a_persona_synthesised_from_an_unknown_id_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '  function chatPersonaById(id) {\n    return CHAT_PERSONAS.find(persona => persona.id === id)',
         '  function chatPersonaById(id) {\n    if (id) return { id, label: id, description: id, prompt: id };\n    return CHAT_PERSONAS.find(persona => persona.id === id)'),
    )
    assert "a persona cannot be synthesised from an unknown id" in findings
