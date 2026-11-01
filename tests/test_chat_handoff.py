"""Chat's handoff to the Harness (UIUX-P2-06).

Chat is where the thinking happens; the Harness is where work is admitted. The crossing
between them is the thing worth testing, because every wrong version of it is plausible:
a button that quietly starts a run, a handoff that posts the conversation before a human
chose anything, a brief that summarises rather than reports, a failure that loses the
transcript, a receipt with no way back.

Each rule is broken in a copy of the source, because a rule that passes because it
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
WALK = PROJECT_ROOT / "scripts" / "chat_handoff_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Handoff.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-handoff.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_chat_handoff_inventory",
        PROJECT_ROOT / "scripts" / "uiux_chat_handoff_inventory.py",
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
        [sys.executable, "scripts/uiux_chat_handoff_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Chat to Harness: an explicit handoff, and a way back")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-06"
    assert data["source"]["routes"] == [], (
        "the handoff is a local carry; a route would make it a request"
    )


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── it is a handoff, not a run ──────────────────────────────────────────────


def test_the_handoff_cannot_run_post_or_clear(payload):
    safety = payload["frontend"]["safety"]
    assert safety["no_network"] is True
    assert safety["no_run"] is True
    assert safety["no_clear"] is True


def test_a_handoff_that_cannot_happen_says_so(payload):
    safety = payload["frontend"]["safety"]
    assert safety["failure_is_presented"] is True
    assert safety["failure_keeps_the_transcript"] is True
    assert safety["failure_offers_a_retry"] is True
    assert safety["every_exit_is_visible"] is True


def test_the_handoff_fills_the_harness_composer_and_goes_there(payload):
    carry = payload["frontend"]["carry"]
    assert carry["fills_the_harness_composer"] is True
    assert carry["focuses_it"] is True
    assert carry["switches_to_the_harness"] is True
    assert carry["returns_what_it_carried"] is True


# ── the brief reports what happened ─────────────────────────────────────────


def test_the_brief_is_built_from_the_recorded_turns(payload):
    brief = payload["frontend"]["brief"]
    assert brief["task_is_the_last_user_message"] is True
    assert brief["counts_messages_and_replies"] is True
    assert brief["counts_calls_and_failures"] is True
    assert brief["uses_the_recorded_context"] is True
    assert brief["prints_the_context_line"] is True


def test_the_brief_says_what_the_tools_did(payload):
    brief = payload["frontend"]["brief"]
    assert brief["statuses_are_words"] is True
    assert brief["says_step_and_time"] is True
    assert brief["says_nothing_ran"] is True


def test_every_bound_the_brief_imposes_is_admitted(payload):
    brief = payload["frontend"]["brief"]
    assert brief["bounds_the_calls"] is True
    assert brief["admits_the_bound"] is True
    assert brief["clips_the_outputs"] is True
    assert brief["admits_the_clip"] is True
    assert brief["admits_the_server_cap"] is True


# ── one sentence, one button ────────────────────────────────────────────────


def test_the_button_and_its_sentence_are_one_answer(payload):
    sentence = payload["frontend"]["sentence"]
    assert sentence["one_predicate"] is True
    assert sentence["button_reads_it"] is True
    assert sentence["reason_counts_what_it_carries"] is True
    assert sentence["waits_while_streaming"] is True
    assert sentence["written_only_on_change"] is True


def test_the_pane_remembers_what_it_handed_over(payload):
    assert payload["frontend"]["sentence"]["remembers_what_was_handed"] is True
    assert payload["frontend"]["transcript"]["clear_resets_the_mark"] is True
    assert payload["frontend"]["transcript"]["undo_resets_the_mark"] is True


def test_the_control_is_reachable_and_described(payload):
    control = payload["frontend"]["control"]
    assert control["is_a_button"] is True
    assert control["type_is_button"] is True
    assert control["starts_disabled"] is True
    assert control["described_by_the_sentence"] is True
    assert control["sentence_is_a_live_region"] is True


# ── the way back ────────────────────────────────────────────────────────────


def test_the_receipt_counts_the_carry_and_offers_a_way_back(payload):
    receipt = payload["frontend"]["receipt"]
    assert receipt["is_presented_not_written"] is True
    assert receipt["cause_counts_the_carry"] is True
    assert receipt["kept_state_is_stated"] is True
    assert receipt["nothing_has_run_is_stated"] is True
    assert receipt["way_back_to_chat"] is True
    assert receipt["way_back_focuses_the_composer"] is True


# ── the walk covers the same ground ─────────────────────────────────────────


def test_the_walk_presses_the_button_and_proves_nothing_was_posted():
    assert WALK.exists(), "scripts/chat_handoff_walk.mjs is missing"
    source = WALK.read_text(encoding="utf-8")
    for needle in (
        "harnessPromptInput",
        "nothing has run",
        "Back to Chat",
        "the carry writes nothing",
        "FAILED",
    ):
        assert needle in source, needle


# ── negative: a rule that stops looking ─────────────────────────────────────


def test_a_handoff_that_posts_the_conversation_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    switchTab("harness");\n    composer.value = brief;',
            '    await fetch("/api/chat/handoff", { method: "POST", body: brief });\n'
            '    switchTab("harness");\n    composer.value = brief;',
            1,
        )
    )
    assert "safety" in _checks(payload)


def test_a_handoff_that_starts_a_run_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    switchTab("harness");\n    composer.value = brief;',
            '    switchTab("harness");\n    executeHarnessSelectedWorkflow();\n    composer.value = brief;',
            1,
        )
    )
    assert "safety" in _checks(payload)


def test_a_handoff_that_clears_the_transcript_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    // The mark, not a message: the sentence beside the button is derived from it, so it",
            "    chatHistory = [];\n    // The mark, not a message: the sentence beside the button is derived from it, so it",
            1,
        )
    )
    assert "safety" in _checks(payload)


def test_a_failure_that_still_claims_the_carry_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      chatHandoffMark = null;\n", "", 1
        )
    )
    assert "safety" in _checks(payload)


def test_a_silent_failure_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '        kind: "error",\n        title: "The handoff did not happen",',
            '        kind: "note",\n        title: "The handoff did not happen",',
            1,
        )
    )
    assert "safety" in _checks(payload)


def test_a_brief_that_summarises_instead_of_reporting_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (stats.context) lines.push(`The last reply was sent with: ${chatContextLine(stats.context)}.`);\n",
            "",
            1,
        )
    )
    assert "brief" in _checks(payload)


def test_a_brief_that_hides_its_clip_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '        lines.push("Outputs above are clipped to 200 characters; Chat holds what the server sent, up to its 500-character cap.");\n',
            "",
            1,
        )
    )
    assert "brief" in _checks(payload)


def test_a_brief_that_copies_every_call_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const shown = stats.callsList.slice(-8);",
            "    const shown = stats.callsList;",
            1,
        )
    )
    assert "brief" in _checks(payload)


def test_a_button_that_can_be_pressed_with_nothing_to_carry_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    button.disabled = state.reason !== "ready";',
            "    button.disabled = false;",
            1,
        )
    )
    assert "sentence" in _checks(payload)


def test_a_live_region_rewritten_every_render_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (signature !== chatHandoffSignature) {", "    if (true) {", 1
        )
    )
    assert "sentence" in _checks(payload)


def test_a_mark_that_survives_clear_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    // A cleared transcript is not the one the Harness was handed, whatever it says.\n    chatHandoffMark = null;\n",
            "",
            1,
        )
    )
    assert "transcript" in _checks(payload)


def test_a_receipt_with_no_way_back_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '          label: "Back to Chat",', '          label: "Dismiss",', 1
        )
    )
    assert "receipt" in _checks(payload)


def test_a_way_back_that_leaves_the_cursor_where_it_was_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '            const input = document.getElementById("chatInput");\n            if (input) input.focus();\n',
            "",
            1,
        )
    )
    assert "receipt" in _checks(payload)


def test_a_handoff_that_does_not_go_to_the_harness_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    switchTab("harness");\n    composer.value = brief;',
            "    composer.value = brief;",
            1,
        )
    )
    assert "carry" in _checks(payload)


def test_a_server_side_handoff_route_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '@app.post("/api/chat/stream")',
            '@app.post("/api/chat/handoff")\nasync def api_chat_handoff():\n    return {}\n\n\n@app.post("/api/chat/stream")',
            1,
        )
    )
    assert "new-route" in _checks(payload)


def test_a_mute_sentence_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'id="chatHandoffHint" class="chat-handoff-hint" data-reason="nothing" role="status" aria-live="polite"',
            'id="chatHandoffHint" class="chat-handoff-hint" data-reason="nothing"',
            1,
        )
    )
    assert "control" in _checks(payload)
