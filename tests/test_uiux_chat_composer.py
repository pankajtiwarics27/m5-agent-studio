"""UIUX-P2-04: Chat mode, transcript hierarchy and stream-state contracts."""

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
    start = FRONTEND.index("  function chatTime(")
    end = FRONTEND.index("  // ================= AUTONOMOUS AGENTIC HARNESS", start)
    return FRONTEND[start:end]


def test_existing_chat_controls_remain_present_and_labelled() -> None:
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
    for control in ("chatModel", "chatPersona", "chatReasoning", "chatInput"):
        assert re.search(rf'<label\b[^>]*\bfor="{control}"', markup), control
    assert "Workspace + MCP Tools" in markup
    assert "Unload M5 after reply" in markup
    assert "onclick=\"clearChat()\"" in markup
    assert "onclick=\"stopActiveStream()\"" in markup
    assert "onclick=\"sendChat()\"" in markup


def test_mode_and_composer_are_visible_and_keyboard_described() -> None:
    markup = _chat_markup()
    assert 'role="group" aria-label="Response mode"' in markup
    assert 'aria-label="Conversation options"' in markup
    assert 'role="region" aria-label="Conversation transcript"' in markup
    assert 'aria-describedby="chatComposerHint"' in markup
    assert "Enter to send" in markup
    assert "Shift+Enter for a new line" in markup
    assert "No messages yet" in markup


def test_transcript_marks_role_model_time_status_and_usage() -> None:
    handler = _chat_handler()
    for marker in (
        'class="chat-msg-role"',
        'class="chat-msg-model"',
        'class="chat-msg-time"',
        'class="chat-msg-state"',
        'completion_tokens',
        'decode_tps',
        'data-role="user"',
        'data-role="assistant"',
    ):
        assert marker in handler, marker
    assert "new Intl.DateTimeFormat" in handler
    assert 'data-state="${escapeHtml(state)}"' in handler
    assert 'escapeHtml(m.model)' in handler
    assert 'title="${escapeHtml(m.model)}"' not in handler, "untrusted model text must not enter an attribute"
    assert 'renderMarkdown(m.content || "")' in handler


def test_stream_has_explicit_success_stop_failure_and_unexpected_close_states() -> None:
    handler = _chat_handler()
    for state in ("streaming", "complete", "stopped", "failed"):
        assert f'status: "{state}"' in handler or f'asstMsg.status = "{state}"' in handler
    assert "presentChatError(pendingChatFailure" in handler
    assert handler.index("setChatBusy(false)") < handler.index("presentChatError(pendingChatFailure"), "Retry must not appear while Send is locked"
    assert 'closed() { this.failed("the stream closed before the model answered"); }' in handler
    consume = FRONTEND[FRONTEND.index("  async function consumeSSE(") : FRONTEND.index("  // ================= AUTO 4-STAGE", FRONTEND.index("  async function consumeSSE("))]
    assert "handlers.stopped" in consume
    assert "handlers.closed" in consume
    assert "receivedTerminalEvent" in consume


def test_chat_request_contract_is_unchanged() -> None:
    handler = _chat_handler()
    assert 'consumeSSE("/api/chat/stream"' in handler
    for field in (
        "model,",
        "messages: apiMessages",
        "reasoning_mode:",
        "auto_unload:",
        "tools_enabled:",
        "workspace_dir:",
        "skills:",
    ):
        assert field in handler, field
    assert re.search(r'@app\.post\("/api/chat/stream"\)', APP)
    assert APP.count('"/api/chat/stream"') >= 1


def test_retry_reuses_the_existing_user_turn_without_duplicating_it() -> None:
    handler = _chat_handler()
    assert "function retryChatRequest(text, assistantMessage)" in handler
    assert "sendChatRequest(text, { appendUser: false })" in handler
    assert "if (options.appendUser !== false) chatHistory.push" in handler
    assert "lastIndexOf(assistantMessage)" in handler


def test_clear_invalidates_late_failure_presentation() -> None:
    handler = _chat_handler()
    assert "let chatRequestGeneration = 0" in FRONTEND
    assert "chatRequestGeneration += 1" in handler
    assert "requestGeneration === chatRequestGeneration" in handler


def test_narrow_chat_layout_can_shrink_controls_without_horizontal_overflow() -> None:
    assert "grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr)" in FRONTEND
    assert "grid-template-columns: minmax(0, 1fr);" in FRONTEND
    assert ".chat-mode-field select { width: 100%; min-width: 0; }" in FRONTEND
    assert ".chat-composer-row { grid-template-columns: minmax(0, 1fr); }" in FRONTEND
    assert ".chat-messages { flex: 1; min-height: 0;" in FRONTEND
