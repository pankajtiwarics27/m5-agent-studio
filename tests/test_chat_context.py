"""The context a Chat reply was given, and what its tools did (UIUX-P2-05).

A reply is the product of a request nobody in the pane can see. These rules hold the
pane to saying what it sent - persona, reasoning mode, tools, skills, message count, and
the directory that will be read, including when that is not the one the reader typed -
and to saying what each tool call did in words, with sizes and times a reader can check,
and the whole argument object one disclosure below.

The mirror defect matters as much as a missing line: a context recomputed at render time
describes the composer as it is now, not the request that went out. `not_recomputed`
catches it.

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
WALK = PROJECT_ROOT / "scripts" / "chat_context_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Context.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-context.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_chat_context_inventory",
        PROJECT_ROOT / "scripts" / "uiux_chat_context_inventory.py",
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


def _details(payload: dict, check: str) -> str:
    return " ".join(f["detail"] for f in payload["findings"] if f["check"] == check)


# ── the artifact is current ──────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_chat_context_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Chat: the context a reply was given")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-05"
    assert data["source"]["routes"] == ["/api/chat/stream"]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the context a reply was given ───────────────────────────────────────────


def test_the_context_names_everything_that_went_out(payload):
    assert set(payload["context_fields"]) == {
        "model",
        "persona",
        "reasoning",
        "tools",
        "workspace",
        "workspace_effective",
        "skills",
        "messages",
    }


def test_the_context_is_captured_once_and_carried(payload):
    context = payload["frontend"]["context"]
    assert context["captured_at_send"] is True
    assert context["attached_to_the_turn"] is True
    assert context["rendered"] is True
    assert context["not_recomputed"] is True, (
        "a context recomputed at render time describes the composer, not the request"
    )


def test_the_context_is_built_from_the_requests_own_values(payload):
    context = payload["frontend"]["context"]
    assert context["persona_from_the_helper"] is True
    assert context["line_names_the_model"] is True
    assert context["persona_label_from_the_select"] is True
    assert context["payload_reads_the_same_values"] is True
    assert context["skills_count_from_the_set"] is True
    assert context["message_count_from_the_request"] is True


def test_a_directory_that_was_not_sent_says_what_will_be_read(payload):
    context = payload["frontend"]["context"]
    assert context["sent_vs_effective"] is True
    assert context["line_names_the_effective_workspace"] is True


def test_a_zero_skill_count_is_noise_and_a_hidden_one_is_a_lie(payload):
    assert payload["frontend"]["context"]["skills_only_when_sent"] is True


# ── what the tools did ──────────────────────────────────────────────────────


def test_every_tool_call_has_a_status_word_and_a_tone(payload):
    tools = payload["frontend"]["tools"]
    assert tools["status_word"] is True
    assert tools["status_rendered"] is True
    assert tools["status_tone"] is True
    assert payload["totals"]["tool_status_tone"] == 2


def test_status_is_not_carried_by_colour(payload):
    assert payload["frontend"]["tools"]["not_colour_only"] is True


def test_a_tool_call_says_its_step_its_time_and_its_size(payload):
    tools = payload["frontend"]["tools"]
    assert tools["step_shown"] is True
    assert tools["elapsed_shown"] is True
    assert tools["args_size_shown"] is True


def test_the_whole_argument_object_is_one_disclosure_below(payload):
    tools = payload["frontend"]["tools"]
    assert tools["full_args_in_body"] is True
    assert tools["args_not_truncated"] is True
    assert tools["output_cap_stated"] is True


def test_the_summary_counts_the_calls_and_the_failures(payload):
    tools = payload["frontend"]["tools"]
    assert tools["summary_counts"] is True
    assert tools["summary_total_ms"] is True
    assert tools["summary_rendered"] is True
    assert tools["rows_rendered"] is True


def test_the_reasoning_disclosure_says_how_much_there_is(payload):
    assert payload["frontend"]["tools"]["reasoning_size"] is True


# ── while it streams ────────────────────────────────────────────────────────


def test_the_clock_is_measured_not_invented(payload):
    timing = payload["frontend"]["timing"]
    assert timing["measured"] is True
    assert timing["started_at_send"] is True
    assert timing["first_token_on_first_token"] is True
    assert timing["final_meta_shows_ttft"] is True
    assert timing["no_timer"] is True
    assert timing["clock_frozen_at_done"] is True
    assert timing["finished_elapsed_uses_it"] is True


def test_a_turn_with_no_token_yet_says_it_is_waiting(payload):
    assert payload["frontend"]["timing"]["streaming_meta_says_waiting"] is True


def test_busy_follows_the_turn_not_the_send_flag(payload):
    busy = payload["frontend"]["busy"]
    assert busy["follows_a_streaming_turn"] is True
    assert busy["keeps_the_send_flag"] is True


def test_the_limit_of_the_clock_is_written_down(payload):
    assert payload["frontend"]["timing"]["snapshot_is_documented"] is True
    assert any("frozen clock is a fact" in limit for limit in payload["limits"])


# ── the pane and the route agree ────────────────────────────────────────────


def test_the_pane_reads_only_the_fields_the_route_sends(payload):
    server = payload["server"]
    assert server["route"] == "/api/chat/stream"
    assert server["event"] == "chat_tool"
    assert set(payload["tool_event_fields"]) <= set(server["fields"])
    assert server["preview_cap"] == payload["tool_output_cap"] == 500


def test_the_surface_has_rules_of_its_own(payload):
    styles = payload["frontend"]["styles"]
    assert all(styles.values()), styles


# ── the walk exists and checks the same things ──────────────────────────────


def test_the_walk_writes_a_failing_tool_call_and_a_live_turn():
    assert WALK.exists(), "scripts/chat_context_walk.mjs is missing"
    source = WALK.read_text(encoding="utf-8")
    for needle in (
        "FAILED",
        "tool calls",
        "byte for byte",
        "first 500 characters",
        "dataset.context",
        "waiting for the first token",
    ):
        assert needle in source, needle


# ── negative: a rule that stops looking ─────────────────────────────────────


def test_a_context_captured_after_the_fact_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const context = chatContextSnapshot(model, apiMessages.length, workspaceDir);\n",
            "    const context = null;\n",
            1,
        )
    )
    assert "context" in _checks(payload)


def test_a_context_recomputed_at_render_time_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      const contextLine = m.context ? chatContextLine(m.context) : "";',
            '      const contextLine = m.context ? chatContextLine(chatContextSnapshot("", 0, "")) : "";',
            1,
        )
    )
    assert "not_recomputed" in _checks(payload) or "context" in _checks(payload), (
        _details(payload, "context")
    )


def test_a_transcript_that_does_not_print_the_context_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'data-context="sent"', 'data-context=""', 1
        )
    )
    assert "context" in _checks(payload)


def test_a_guessed_persona_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      persona: chatPersonaLabel(),", '      persona: "Assistant",', 1
        )
    )
    assert "context" in _checks(payload)


def test_a_request_that_reads_a_different_reasoning_mode_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      reasoning_mode: context.reasoning,",
            '      reasoning_mode: (document.getElementById("chatReasoning") || {}).value || "off",',
            1,
        )
    )
    assert "context" in _checks(payload)


def test_a_lost_workspace_distinction_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      workspace: workspaceSent || "",\n', "", 1
        )
    )
    assert "context-incomplete" in _checks(payload) or "context" in _checks(payload)


def test_a_line_that_does_not_name_the_model_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const bits = [`model ${ctx.model}`, ctx.persona,",
            "    const bits = [ctx.persona,",
            1,
        )
    )
    assert "context" in _checks(payload)


def test_a_transcript_that_reads_as_idle_while_a_turn_arrives_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const streaming = chatHistory.some(m => m.role === "assistant" && !m.usage);\n',
            "",
            1,
        )
    )
    assert "busy" in _checks(payload)


def test_a_line_that_hides_the_skill_count_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (ctx.skills)", "    if (true)", 1
        )
    )
    assert "context" in _checks(payload)


def test_a_tool_row_without_its_status_word_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'data-tone="${tool.ok ? "ok" : "danger"}">${status}</span>',
            'data-tone="${tool.ok ? "ok" : "danger"}"></span>',
            1,
        )
    )
    assert "tools" in _checks(payload)


def test_a_tool_row_statused_by_colour_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const status = tool.ok ? "OK" : "FAILED";',
            '    const status = tool.ok ? "OK" : "FAILED";\n    const tint = "border-color: red";',
            1,
        )
    )
    assert "tools" in _checks(payload)


def test_arguments_cut_off_again_are_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "<pre>${escapeHtml(args)}</pre>",
            "<pre>${escapeHtml(args.slice(0, 72))}</pre>",
            1,
        )
    )
    assert "tools" in _checks(payload)


def test_a_summary_that_stops_counting_failures_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const failed = tools.filter(t => !t.ok).length;",
            "    const failed = 0;",
            1,
        )
    )
    assert "tools" in _checks(payload)


def test_a_timer_invented_behind_the_clock_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const now = performance.now();",
            "    setInterval(() => {}, 100);\n    const now = performance.now();",
            1,
        )
    )
    assert "timing" in _checks(payload)


def test_a_first_token_nobody_stamped_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "        if (!asstMsg.timing.firstTokenAt) asstMsg.timing.firstTokenAt = performance.now();\n",
            "",
            1,
        )
    )
    assert "timing" in _checks(payload)


def test_a_clock_that_keeps_running_after_the_stream_stops_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "        asstMsg.timing.endedAt = performance.now();\n", "", 1
        )
    )
    assert "timing" in _checks(payload)


def test_a_finished_turn_that_ignores_its_stopped_clock_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "(((m.timing.endedAt || performance.now()) - m.timing.startedAt) / 1000)",
            "((performance.now() - m.timing.startedAt) / 1000)",
            1,
        )
    )
    assert "timing" in _checks(payload)


def test_a_stream_that_claims_a_first_token_before_one_arrived_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '"waiting for the first token"', '"first token 0.00s"', 1
        )
    )
    assert "timing" in _checks(payload)


def test_a_field_the_route_does_not_send_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "const elapsed = Number(tool.elapsed_ms) || 0;",
            "const elapsed = Number(tool.duration_ms) || 0;",
            1,
        )
    )
    assert "event-drift" in _checks(payload)


def test_a_route_that_stops_sending_a_field_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '                            "elapsed_ms": res_obj.get("elapsed_ms", 0),\n',
            "",
            1,
        )
    )
    assert "event-drift" in _checks(payload)


def test_a_route_that_changes_its_output_cap_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '"preview": (compressed_out or "")[:500],',
            '"preview": (compressed_out or "")[:400],',
            1,
        )
    )
    assert "event-drift" in _checks(payload), _details(payload, "event-drift")


def test_a_rule_that_vanishes_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            ".chat-context {", ".chat-context-gone {", 1
        )
    )
    assert "style-missing" in _checks(payload)
