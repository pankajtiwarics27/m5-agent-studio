"""Chat's transcript and composer (UIUX-P2-04).

A chat pane looks finished as soon as it can send something. The parts a screenshot
cannot pin are the ones that make it usable: that every turn says who is speaking and
which model answered, that the pane follows the stream only when the reader was already
at the bottom, that the disabled Send button and the sentence under it are the same
answer, that the tools sentence describes what the route actually does, and that Clear
can be undone.

Each rule is broken in a copy of the markup, because a rule that passes because it
stopped looking is the defect it was written to catch.
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
WALK = PROJECT_ROOT / "scripts" / "chat_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_chat_inventory", PROJECT_ROOT / "scripts" / "uiux_chat_inventory.py"
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


# ── the artifact is current ──────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_chat_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Chat: the transcript and the composer")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-04"
    assert data["source"]["routes"] == ["/api/chat/stream"]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── who is speaking ─────────────────────────────────────────────────────────


def test_every_role_has_a_label_and_a_tone(payload):
    roles = payload["roles"]
    assert set(roles) == {"user", "assistant"}
    for role, row in roles.items():
        assert row["label"], role
        assert row["tone"] in inventory.TONES, (role, row["tone"])


def test_every_turn_is_labelled_and_numbered(payload):
    transcript = payload["frontend"]["transcript"]
    assert payload["frontend"]["roles"]["emitted"] == ["assistant", "user"]
    assert payload["frontend"]["roles"]["labelled"] is True
    assert payload["frontend"]["roles"]["numbered"] is True
    assert transcript["streaming_marked"] is True


def test_the_model_is_named_above_its_answer(payload):
    assert payload["frontend"]["roles"]["model_in_header"] is True


# ── following the reader ────────────────────────────────────────────────────


def test_the_transcript_follows_the_reader_not_the_stream(payload):
    transcript = payload["frontend"]["transcript"]
    assert transcript["pinned_before_render"] is True
    assert transcript["scroll_guarded"] is True
    assert transcript["no_unconditional_scroll"] is True
    assert transcript["slack_constant"] is True
    assert transcript["jump_control"] is True
    assert transcript["jump_hidden_when_pinned"] is True


def test_the_transcript_is_a_log_that_is_not_read_aloud(payload):
    transcript = payload["frontend"]["transcript"]
    assert transcript["log_role"] is True
    assert transcript["live_off"] is True, (
        "a per-token re-render inside a live region is unlistenable"
    )
    assert transcript["aria_busy"] is True


def test_an_empty_transcript_says_what_will_appear(payload):
    assert payload["frontend"]["transcript"]["empty_says_what_appears"] is True


# ── the composer's single answer ────────────────────────────────────────────


def test_one_predicate_drives_the_button_and_the_sentence(payload):
    composer = payload["frontend"]["composer"]
    assert composer["one_predicate"] is True
    assert composer["button_reads_it"] is True
    assert composer["no_second_rule"] is True
    assert composer["live_region"] is True


def test_every_reason_is_declared(payload):
    assert payload["reasons"] == [
        "no-model",
        "ready",
        "runtime-offline",
        "streaming",
        "tools-readonly",
    ]
    for tone in payload["tones_used"]:
        assert tone in inventory.TONES


def test_the_hint_is_only_rewritten_when_the_answer_changes(payload):
    assert payload["frontend"]["composer"]["hint_written_on_change"] is True


def test_the_tools_sentence_describes_the_route(payload):
    composer = payload["frontend"]["composer"]
    assert composer["tools_says_readonly"] is True
    assert composer["tools_names_a_directory"] is True
    assert payload["server"]["policy"] == "plan"
    assert payload["server"]["workspace_fallback"] == "WORKSPACE_ROOT"


def test_the_runtime_verdict_is_read_not_guessed(payload):
    composer = payload["frontend"]["composer"]
    assert composer["offline_reads_telemetry"] is True
    assert composer["local_only_for_offline"] is True


def test_the_client_sends_what_the_route_reads(payload):
    sent = payload["frontend"]["stream"]["payload_fields"]
    for field in payload["server"]["fields"]:
        if field in payload["server"]["optional_fields"]:
            continue
        assert field in sent, field


# ── clear, and the way back ─────────────────────────────────────────────────


def test_clear_is_undoable(payload):
    clear = payload["frontend"]["clear"]
    assert clear["undoable"] is True
    assert clear["keeps_a_copy"] is True
    assert clear["restores"] is True
    assert clear["noop_when_empty"] is True
    assert clear["disabled_when_empty"] is True


# ── keyboard ────────────────────────────────────────────────────────────────


def test_enter_sends_and_shift_enter_does_not(payload):
    keyboard = payload["frontend"]["keyboard"]
    assert keyboard["enter_sends"] is True
    assert keyboard["shift_is_a_newline"] is True
    assert keyboard["one_handler"] is True
    assert keyboard["no_markup_handler"] is True
    assert keyboard["placeholder_matches"] is True
    assert keyboard["described_by_hint"] is True


# ── the stream's busy state ─────────────────────────────────────────────────


def test_every_way_out_of_a_stream_clears_the_busy_state(payload):
    stream = payload["frontend"]["stream"]
    assert stream["busy_flag"] is True
    assert stream["busy_cleared_in_finally"] is True
    assert stream["stop_clears_busy"] is True


# ── the walk proves what the story claims ───────────────────────────────────


def test_the_walk_writes_a_transcript_and_holds_the_pane_against_it():
    walk = WALK.read_text(encoding="utf-8")
    assert "chatHistory = [" in walk, "the walk must write the turns it then judges"
    assert "chat-turn-role" in walk and "chat-turn-index" in walk
    assert "dataset.role" in walk
    assert "scrollTop" in walk, "the scroll rule can only be proven by scrolling"


def test_the_walk_proves_the_failure_surface_by_refusing_the_route():
    walk = WALK.read_text(encoding="utf-8")
    assert "setRequestInterception(true)" in walk
    assert "api\\/chat\\/stream" in walk
    assert "Retry" in walk
    assert "Undo" in walk


# ── negative cases: each rule must be able to fail ──────────────────────────


def test_a_role_without_a_tone_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'user: { label: "You", tone: "info" }',
            'user: { label: "You", tone: "purple" }',
            1,
        )
    )
    assert "tone-unknown" in _checks(payload)


def test_a_role_outside_the_table_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'return `<div class="msg user" data-role="user" data-state="sent">${head}${renderMarkdown(m.content)}</div>`;',
            'return `<div class="msg user" data-role="operator" data-state="sent">${head}${renderMarkdown(m.content)}</div>`;',
            1,
        )
    )
    assert "role-unlisted" in _checks(payload)


def test_a_turn_without_a_number_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '        + `<span class="chat-turn-index">${index + 1}</span>`\n',
            "",
            1,
        )
    )
    assert "turn-unnumbered" in _checks(payload)


def test_a_model_named_below_its_answer_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '        + (m.role === "assistant" && m.model ? `<code class="chat-msg-model">${escapeHtml(m.model)}</code>` : "")\n',
            "",
            1,
        )
    )
    assert "model-below-the-fold" in _checks(payload)


def test_a_button_with_its_own_rule_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (send) send.disabled = Boolean(chatSendReason());",
            "    if (send) send.disabled = !chatHistory.length;",
            1,
        )
    )
    assert "button-differs" in _checks(payload)


def test_a_hint_rewritten_every_render_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "if (line && signature !== chatHintSignature) {", "if (line) {", 1
        )
    )
    assert "live-region-chatter" in _checks(payload)


def test_an_unconditional_scroll_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (pinned) box.scrollTop = box.scrollHeight;",
            "    box.scrollTop = box.scrollHeight;",
            1,
        )
    )
    assert "transcript" in _checks(payload)


def test_a_transcript_inside_a_live_region_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'aria-live="off" aria-relevant="additions"',
            'aria-live="polite" aria-relevant="additions"',
            1,
        )
    )
    assert "transcript" in _checks(payload)


def test_a_clear_that_cannot_be_undone_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      kind: "recovery",\n      title: "Transcript cleared",',
            '      kind: "error",\n      title: "Transcript cleared",',
            1,
        )
    )
    assert "clear" in _checks(payload)


def test_clear_enabled_on_an_empty_transcript_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (clear) clear.disabled = !chatHistory.length;", "", 1
        )
    )
    assert "clear" in _checks(payload)


def test_shift_enter_sending_is_caught(sources):
    # The chat handler specifically: other composers on the shell bind Enter too, so a
    # transform that edits the first match would prove nothing about this pane.
    payload = sources(
        html_transform=lambda text: text.replace(
            'getElementById("chatInput").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey)',
            'getElementById("chatInput").addEventListener("keydown", e => { if (e.key === "Enter")',
            1,
        )
    )
    assert "keyboard" in _checks(payload)


def test_an_inline_key_handler_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'placeholder="Message your selected model… (Enter sends, Shift+Enter adds a line)"',
            'placeholder="Message your selected model… (Enter sends, Shift+Enter adds a line)" onkeydown="sendChat()"',
            1,
        )
    )
    assert "keyboard" in _checks(payload)


def test_a_placeholder_that_promises_the_wrong_key_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "(Enter sends, Shift+Enter adds a line)",
            "(Enter to send)",
            1,
        )
    )
    assert "keyboard" in _checks(payload)


def test_a_stale_tools_claim_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '"policy": "plan",  # Strictly read-only inside Direct Chat!',
            '"policy": "standard",  # Strictly read-only inside Direct Chat!',
            1,
        )
    )
    assert "tool-claim" in _checks(payload)


def test_a_guessed_runtime_failure_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    return lastTelemetry.omlx_online === false;",
            "    return false;",
            1,
        )
    )
    assert "runtime-guess" in _checks(payload)


def test_a_busy_state_that_is_never_cleared_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "chatBusy = false; renderChatMessages();",
            "renderChatMessages();",
            1,
        )
    )
    assert "busy" in _checks(payload)


def test_a_payload_that_drops_a_field_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      workspace_dir: workspaceDir,\n", "", 1
        )
    )
    assert "payload-drift" in _checks(payload)


def test_a_transcript_that_never_says_what_appears_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "Choose a model above and type below; replies and reasoning appear here.",
            "Session cleared.",
        )
    )
    assert "transcript" in _checks(payload)
