"""Supplemental P2-04 Chat stream and recovery contracts after mainline reconciliation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


def _chat_markup() -> str:
    start = FRONTEND.index('<section id="pane-chat"')
    end = FRONTEND.index("</section>", start)
    return FRONTEND[start : end + len("</section>")]


def _chat_handler() -> str:
    start = FRONTEND.index("  const CHAT_ROLES = {")
    end = FRONTEND.index("  // ================= AUTONOMOUS AGENTIC HARNESS", start)
    return FRONTEND[start:end]


def _consume_sse() -> str:
    start = FRONTEND.index("  async function consumeSSE(")
    end = FRONTEND.index("  // ================= AUTO 4-STAGE", start)
    return FRONTEND[start:end]


def test_existing_chat_controls_keep_labels_and_keyboard_guidance() -> None:
    markup = _chat_markup()
    for control in (
        "chatModel",
        "chatPersona",
        "chatReasoning",
        "chatToolsEnabled",
        "chatUnload",
        "chatClearBtn",
        "chatInput",
        "chatStopBtn",
        "chatSendBtn",
    ):
        assert markup.count(f'id="{control}"') == 1, control
    for control in ("chatModel", "chatPersona", "chatReasoning"):
        assert re.search(rf'<label\b[^>]*\bfor="{control}"', markup), control
    assert 'aria-label="Message" aria-describedby="chatComposerHint chatKeyboardHint"' in markup
    assert 'id="chatKeyboardHint"' in markup
    assert 'role="log" aria-label="Conversation" aria-live="off"' in markup
    assert "Shift+Enter adds a line" in markup
    assert "Enter sends" in markup


def test_transcript_retains_role_model_turn_number_local_time_and_state() -> None:
    handler = _chat_handler()
    for marker in (
        'class="chat-turn-role"',
        'class="chat-turn-index"',
        'class="chat-msg-model"',
        'class="chat-turn-time"',
        'class="chat-msg-state"',
        'data-role="user" data-state="sent"',
        'data-role="assistant" data-state="${escapeHtml(state)}"',
    ):
        assert marker in handler, marker
    assert "new Intl.DateTimeFormat" in handler
    assert "createdAt: Date.now()" in handler
    assert 'status: "streaming"' in handler
    for state in ("complete", "stopped", "failed"):
        assert f'asstMsg.status = "{state}"' in handler
    assert 'data-state="${escapeHtml(state)}"' in handler


def test_stop_failure_and_unexpected_close_have_distinct_recovery_states() -> None:
    handler = _chat_handler()
    consume = _consume_sse()
    assert 'function stopChatStream()' in handler
    assert 'stopped() {' in handler
    assert 'failed(message) {' in handler
    assert 'closed() { this.failed("the stream closed before the model answered"); }' in handler
    for state in ("complete", "stopped", "failed"):
        assert f'asstMsg.status = "{state}"' in handler
    assert "handlers.stopped" in consume
    assert "handlers.closed" in consume
    assert "receivedTerminalEvent" in consume
    assert "controller.signal.aborted" in consume
    assert 'class="chat-pending"' in handler


def test_chat_request_payload_contract_and_authorized_route_are_unchanged() -> None:
    handler = _chat_handler()
    for field in (
        "model,",
        "messages: apiMessages",
        "reasoning_mode: context.reasoning",
        "auto_unload:",
        "tools_enabled: toolsEnabled",
        "workspace_dir: workspaceDir",
        "skills: [...selectedSkills]",
    ):
        assert field in handler, field
    assert re.search(r'@app\.post\("/api/chat/stream"\)', APP)
    assert APP.count('"/api/chat/stream"') >= 1


def test_retry_reuses_the_existing_user_turn_after_the_busy_lock_is_released() -> None:
    handler = _chat_handler()
    assert "function retryChatRequest(text, assistantMessage)" in handler
    assert "sendChatRequest(text, { appendUser: false })" in handler
    assert "if (options.appendUser !== false) chatHistory.push" in handler
    assert "lastIndexOf(assistantMessage)" in handler
    assert handler.index("chatBusy = false;", handler.index("function sendChatRequest")) < handler.index(
        "presentChatError(pendingChatFailure", handler.index("function sendChatRequest")
    )


def test_clear_keeps_undo_but_invalidates_a_pending_failure_presentation() -> None:
    handler = _chat_handler()
    assert "let chatRequestGeneration = 0" in FRONTEND
    assert "chatRequestGeneration += 1" in handler
    assert "requestGeneration === chatRequestGeneration" in handler
    assert "const kept = chatHistory.slice()" in handler
    assert 'label: "Undo"' in handler
    assert 'title: "Transcript cleared"' in handler


def test_untrusted_context_is_not_interpolated_into_an_html_attribute() -> None:
    handler = _chat_handler()
    assert 'data-context="sent">Context: ${escapeHtml(contextLine)}' in handler
    assert 'title="${escapeHtml(contextLine)}"' not in handler


def test_narrow_layout_keeps_chat_controls_within_the_viewport() -> None:
    assert ".chat-header-bar > div { min-width: 0; flex: 1 1 160px; }" in FRONTEND
    assert ".chat-header-bar select { width: 100%; min-width: 0; }" in FRONTEND
    assert ".chat-header-bar > div { flex: 1 1 100%; }" in FRONTEND
    assert ".chat-composer textarea { flex: 1 1 100% !important; width: 100%; min-width: 0; }" in FRONTEND
