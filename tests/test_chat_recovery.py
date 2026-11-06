"""UIV2-20: Chat's streaming, tool disclosure, copy, failures and handoff.

The browser walk in `scripts/chat_recovery_walk.mjs` is what proves the pane on a screen and on the
wire; this file proves that the rules the walk relies on are still in the source, that breaking one
of them is a finding rather than a silent regression, that the fixture the walk needs can actually
make a tool call, that the captures are the ones the story claims, and that the ledger records it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
STUDIO = PROJECT_ROOT / "studio.py"
WALK = PROJECT_ROOT / "scripts" / "chat_recovery_walk.mjs"
FIXTURE = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_chat_recovery_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-recovery.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "chat-recovery"

VIEWPORTS = ("1440x900", "1280x800", "1024x768", "390x844")
THEMES = ("light", "dark")
STATES = ("empty", "populated", "long-content", "loading", "error", "recovery")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_chat_recovery_inventory", INVENTORY)
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
    assert payload["check_count"] == len(payload["checks"]) >= 28
    assert all(entry["passed"] for entry in payload["checks"])


def test_the_capabilities_are_named_and_single_sourced(payload):
    capabilities = payload["capabilities"]
    assert capabilities["tools"]["output_cap"] == 500
    assert capabilities["retry"]["keep_failed_turn"] is True
    assert capabilities["handoff"]["posts"] is False
    for name in ("copy", "tools", "streaming", "retry", "handoff"):
        assert name in capabilities


def test_the_pane_and_the_served_copy_both_carry_the_work():
    for path in (FRONTEND, STUDIO):
        source = path.read_text(encoding="utf-8")
        for needle in (
            "function writeClipboard(",
            "function copyAnnounce(",
            "function chatRetryPlan(",
            "asked.retryOf = { tool_calls: options.retry.ran }",
            "Stopped by you",
        ):
            assert needle in source, f"{path.name} is missing {needle}"


def test_the_fixture_can_make_a_tool_call_and_go_on_to_answer():
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert '"[[use-tool]]"' in fixture
    assert '<tool_call>{"name": "read_file"' in fixture
    # The follow-up request carries the tool result with `<` escaped, so the marker is the payload,
    # not the wrapper: a fixture that looks for `<tool_response` would never fire.
    assert "UNTRUSTED_DATA_JSON" in fixture
    assert "[[code]]" in fixture


def test_the_walk_reads_the_clipboard_and_forces_the_fallback():
    walk = WALK.read_text(encoding="utf-8")
    assert "navigator.clipboard.readText()" in walk
    assert "denied by the browser" in walk
    assert "chatRetryPlan" in walk
    for state in STATES:
        assert state in walk


def test_the_doc_states_each_capability(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    for capability in payload["capabilities"]:
        assert capability in doc.lower()
    assert "Ask again" in doc
    assert "posts nothing" in doc


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-20")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]


def test_the_captures_are_on_disk_for_each_viewport_and_theme():
    pngs = sorted(MEDIA.glob("*.png"))
    assert len(pngs) >= 48
    for viewport in VIEWPORTS:
        for theme in THEMES:
            for state in STATES:
                assert (MEDIA / f"chat-recovery-{state}-{viewport}-{theme}.png").exists(), (
                    f"missing chat-recovery-{state}-{viewport}-{theme}.png"
                )


def test_the_before_set_shows_the_pane_before_the_story():
    before = sorted((MEDIA / "before-uiv2-20").rglob("*.png"))
    assert len(before) >= 16


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_copy_that_does_not_await_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "    const copied = await writeClipboard(text);", "    const copied = writeClipboard(text);"),
    )
    assert "copying awaits the clipboard and reports what happened" in findings


def test_a_copy_without_a_fallback_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '      const copied = document.execCommand("copy");', "      const copied = true;"),
    )
    assert "a refused clipboard is copied through a selection fallback" in findings


def test_a_copy_that_is_not_announced_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '    copyAnnounce(copied\n      ? `Copied ${text.length} characters of ${(wrap && wrap.dataset.lang) || "code"}.`',
         '    void (copied\n      ? `Copied ${text.length} characters of ${(wrap && wrap.dataset.lang) || "code"}.`'),
    )
    assert "a copy is announced, not just painted" in findings


def test_an_unnamed_copy_control_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         'aria-label="Copy the ${escapeHtml(label)} block (${chars} characters)"',
         'aria-label="Copy"'),
    )
    assert "a code block's copy control names the block and its size" in findings


def test_a_tool_status_without_words_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'const status = tool.ok ? uiStatus("ok").label : uiStatus("failed").label;',
         'const status = tool.ok ? "✓" : "✗";'),
    )
    assert "its status is a word, not only a colour" in findings


def test_a_tool_disclosure_that_hides_the_cap_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, ' (the server sends the first 500 characters)', ''),
    )
    assert "opening it shows the arguments, the output and the server's cap" in findings


def test_a_stopped_turn_that_forgets_its_size_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "        asstMsg.stoppedChars = (asstMsg.content || \"\").length;\n", ""),
    )
    assert "stopping records how much of the answer had arrived" in findings


def test_a_stopped_turn_that_does_not_say_who_stopped_it_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "<span>Stopped by you</span>", "<span>Stopped</span>"),
    )
    assert "a stopped turn says who stopped it and how much was kept" in findings


def test_a_retry_that_ignores_the_tool_count_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "    const ran = message && Array.isArray(message.tools) ? message.tools.length : 0;",
         "    const ran = 0;"),
    )
    assert "a retry's meaning is decided by what the failed attempt did" in findings


def test_a_retry_that_always_replaces_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "      replace: false,", "      replace: true,"),
    )
    assert "a turn that ran tools is asked again, not retried silently" in findings


def test_a_retry_that_drops_the_failed_turn_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "    if (plan.replace) {", "    if (true) {"),
    )
    assert "retrying keeps the failed turn when it did something" in findings


def test_a_question_that_does_not_say_it_was_asked_again_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "        asked.retryOf = { tool_calls: options.retry.ran };\n", ""),
    )
    assert "the question asked again says so on the question" in findings


def test_a_card_that_does_not_carry_the_plan_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "    const plan = retryPlan || chatRetryPlan(null);", "    const plan = chatRetryPlan(null);"),
    )
    assert "the failure card carries the plan and says what will not be repeated" in findings


def test_a_handoff_that_posts_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND,
         '    const button = document.getElementById("chatHandoffBtn");\n    const hint = document.getElementById("chatHandoffHint");',
         '    const button = document.getElementById("chatHandoffBtn");\n    fetch("/api/harness/tasks", { method: "POST" });\n    const hint = document.getElementById("chatHandoffHint");'),
    )
    assert "the handoff posts nothing and starts nothing" in findings


def test_a_brief_without_the_tool_calls_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '      lines.push("Tool calls in this conversation:");', ""),
    )
    assert "the handoff brief names the messages and the tool calls it carries" in findings


def test_a_walk_that_stops_reading_the_clipboard_is_a_finding(sources):
    findings = sources.findings(
        (WALK, "navigator.clipboard.readText()", "null"),
    )
    assert "the browser walk exists and asserts the copy, the tool disclosure and the retry plan" in findings
