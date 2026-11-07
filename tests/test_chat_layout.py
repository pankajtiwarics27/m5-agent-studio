"""UIV2-18: Chat's conversation column, its controls and its composer.

The browser walk in `scripts/chat_layout_walk.mjs` is what proves the pane on a screen; this file
proves that the rules the walk relies on are still in the source, that breaking one of them is a
finding rather than a silent regression, that the captures on disk are the ones the story claims,
and that the ledger records the story it says it does.
"""

from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "chat_layout_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_chat_layout_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Layout.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-layout.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "chat-layout"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
#: The states the ledger's visual matrix names, in the names the captures use.
STATES = ("empty", "populated", "long-content", "loading", "error", "recovery")
#: The two states the before set was captured in: the ribbon and the empty pane.
BEFORE_STATES = ("empty", "populated")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_chat_layout_inventory", INVENTORY)
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
    """`build_inventory` over a copy of the sources, with edits made to the copies."""
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


def test_the_inventory_matches_the_source(payload):
    assert JSON_PATH.read_text(encoding="utf-8") == _load_module().dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == _load_module().render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["status"] == "clean"
    assert payload["check_count"] == len(payload["checks"]) >= 25
    assert all(entry["ok"] for entry in payload["checks"])


def test_the_doc_states_the_column_and_the_states(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert f"{payload['column']['px']}px" in doc
    assert "padding-inline" in doc
    for state in payload["states"]:
        assert state in doc
    assert "do not" in doc and "hand-edit" in doc


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-18")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]
    assert story["tracking"]["tests_executed"]


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_column_outside_the_band_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "  --chat-column: 820px;", "  --chat-column: 1200px;"),
    )
    assert "the conversation width is one token, inside the 720-900px readable band" in findings


def test_a_second_column_width_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         ".chat-messages { scroll-padding-bottom: 16px; overscroll-behavior: contain; }",
         ".chat-messages { --chat-column: 900px; scroll-padding-bottom: 16px; overscroll-behavior: contain; }"),
    )
    assert "there is exactly one place the column width is written" in findings


def test_a_row_that_leaves_the_column_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "  .chat-transcript-bar,\n  .chat-messages,", "  .chat-messages,"),
    )
    assert "the transcript, its bar and the composer all take that column" in findings


def test_a_column_nudged_from_one_side_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "    padding-inline: max(20px, calc((100% - var(--chat-column)) / 2));",
         "    margin-left: max(20px, calc((100% - var(--chat-column)) / 2));"),
    )
    assert "the column is centred symmetrically, not nudged from one side" in findings


def test_controls_squeezed_into_the_column_are_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".chat-header-bar { padding-inline: 16px; }", ".chat-header-bar { padding-inline: 210px; }"),
    )
    assert "the controls keep the pane's width instead of squeezing into the column" in findings


def test_a_wrapping_control_bar_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".chat-header-bar { flex-wrap: nowrap; gap: 10px; padding-block: 8px; }",
         ".chat-header-bar { flex-wrap: wrap; gap: 10px; padding-block: 8px; }"),
    )
    assert "above 1200px the pickers are one row" in findings


def test_a_label_that_wraps_the_bar_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;",
         "margin-bottom: 2px; white-space: normal;"),
    )
    assert "and their labels cannot wrap the bar onto a second row" in findings


def test_a_private_control_height_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "    .chat-header-bar .chk-lbl { align-self: center; }",
         "    .chat-header-bar .chk-lbl { align-self: center; }\n    .chat-header-bar select { height: 32px; }"),
    )
    assert "the pickers keep the design system's control height rather than a private one" in findings


def test_five_rows_on_a_phone_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".chat-header-bar > div:not(:first-child) { flex: 1 1 120px; min-width: 0; }",
         ".chat-header-bar > div:not(:first-child) { flex: 1 1 100%; min-width: 0; }"),
    )
    assert "on a phone the controls are three rows, not five" in findings


def test_a_composer_that_can_leave_the_screen_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "position: sticky; bottom: 0; z-index: 2;", "position: relative;"),
    )
    assert "the composer is pinned to the bottom of the pane" in findings


def test_a_composer_under_the_home_indicator_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "padding-bottom: max(11px, env(safe-area-inset-bottom, 0px));", "padding-bottom: 11px;"),
    )
    assert "and keeps clear of a phone's home indicator" in findings


def test_a_field_the_keyboard_can_cover_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "#chatInput { scroll-margin-bottom: 110px; }", "#chatInput { }"),
    )
    assert "the field can scroll itself above an on-screen keyboard" in findings


def test_a_vh_shell_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "height: calc(100dvh - var(--header-height)); grid-template-columns",
         "height: calc(100vh - var(--header-height)); grid-template-columns"),
    )
    assert "the shell is measured in dvh, so the keyboard shrinks the pane" in findings


def test_a_bubble_that_may_grow_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".msg { min-width: 0; overflow-wrap: anywhere; }", ".msg { min-width: 0; }"),
    )
    assert "a bubble may not grow wider than its column" in findings


def test_a_cut_code_block_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".msg pre { max-width: 100%; overflow-x: auto; }", ".msg pre { max-width: 100%; }"),
    )
    assert "a code block scrolls inside its bubble, never off the pane" in findings


def test_an_unclamped_table_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ".msg img, .msg video, .msg table { max-width: 100%; }", ".msg img, .msg video { max-width: 100%; }"),
    )
    assert "images, video and tables are clamped to the bubble" in findings


def test_a_long_empty_state_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         "<span>Pick a model, then type below; replies and reasoning appear here.</span>",
         "<span>Pick a model, then type below; replies and reasoning appear here, and every reply is "
         "kept for the next question in the same conversation so nothing has to be repeated.</span>"),
    )
    assert "the empty state is one sentence, short enough to read at a glance" in findings


def test_two_empty_states_saying_different_things_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'box.innerHTML = `<div class="chat-empty-state"><strong>Nothing has been said yet</strong><span>Pick a model, then type below; replies and reasoning appear here.</span></div>`;',
         'box.innerHTML = `<div class="chat-empty-state"><strong>Nothing has been said yet</strong><span>Choose a model above and type below.</span></div>`;'),
    )
    assert "the markup and the renderer say it in the same words" in findings


def test_a_lost_control_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'id="chatModel"', 'id="chatModelLegacy"'),
    )
    assert "every control the pane had is still there and still wired" in findings


def test_a_changed_keyboard_contract_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '{ if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } }',
         '{ if (e.key === "Enter") { e.preventDefault(); sendChat(); } }'),
    )
    assert "Enter sends, Shift+Enter adds a line - the contract the hint promises" in findings


def test_a_clear_that_cannot_be_undone_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'title: "Transcript cleared"', 'title: "Transcript emptied"'),
    )
    assert "Clear is still undoable" in findings


def test_a_second_stream_call_site_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'consumeSSE("/api/chat/stream"', 'consumeSSE("/api/chat/stream", {}); consumeSSE("/api/chat/stream"'),
    )
    assert "the pane still posts one chat stream and invents no second route" in findings


def test_a_rail_left_open_on_chat_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'layout.classList.add("rail-collapsed")', 'layout.classList.remove("rail-collapsed")'),
    )
    assert "Chat still opens without a telemetry rail" in findings


def test_a_transcript_that_reads_itself_aloud_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'aria-live="off" aria-relevant="additions"', 'aria-live="polite" aria-relevant="additions"'),
    )
    assert "the transcript is still a log that does not read itself aloud" in findings


def test_a_walk_that_stops_asserting_a_state_is_a_finding(sources):
    findings = sources.findings(
        (WALK, 'state("loading")', 'state("never")'),
    )
    assert "the walk asserts every state the captures show" in findings


def test_a_walk_with_a_path_of_its_own_is_a_finding(sources):
    findings = sources.findings(
        (WALK, 'process.env.STUDIO_PYTHON || "python3"', 'process.env.STUDIO_PYTHON || "/tmp/python3"'),
    )
    assert "the walk needs no environment of its own" in findings


def test_a_walk_that_reads_the_token_instead_of_the_browser_is_a_finding(sources):
    findings = sources.findings(
        (WALK, 'const STATES = ["empty"', 'const COLUMN = "--chat-column";\nconst STATES = ["empty"'),
    )
    assert "the walk measures the column it promised, not a screenshot's pixels" in findings


def test_a_walk_that_never_goes_to_phone_width_is_a_finding(sources):
    findings = sources.findings(
        (WALK, "width: 390, height: 844", "width: 420, height: 900"),
    )
    assert "the walk knows the phone layout it photographs" in findings


# ── the captures ────────────────────────────────────────────────────────────


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:33]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("theme", THEMES)
def test_the_captures_exist_for_every_state_and_viewport(viewport, theme):
    width, height = (int(part) for part in viewport.split("x"))
    for state in STATES:
        path = MEDIA / f"chat-{state}-{viewport}-{theme}.png"
        assert path.exists(), path.name
        assert _png_size(path) == (width, height), path.name


def test_the_before_set_is_the_ribbon_from_main():
    before = MEDIA / "before-uiv2-18"
    assert before.is_dir()
    for viewport in VIEWPORTS:
        for theme in THEMES:
            folder = before / f"{viewport}-{theme}"
            for state in BEFORE_STATES:
                path = folder / f"chat-{state}-{viewport}-{theme}.png"
                assert path.exists(), str(path.relative_to(MEDIA))
                width, height = (int(part) for part in viewport.split("x"))
                assert _png_size(path) == (width, height), path.name


def test_the_readme_names_the_producer_and_the_states():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    assert "scripts/chat_layout_walk.mjs" in readme
    assert "council_readiness_seed.py" in readme
    for state in STATES:
        assert state in readme
