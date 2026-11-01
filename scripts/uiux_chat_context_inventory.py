#!/usr/bin/env python3
"""Inventory Chat's model context and tool transparency (UIUX-P2-05).

    python3 scripts/uiux_chat_context_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_chat_context_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_chat_context_inventory.py --json     # print the inventory

A reply is the product of a request the reader never sees. This pane sent a persona, a
reasoning mode, a tools flag with a directory, the active skills and every earlier
message - and printed none of it. When the model called tools, the transcript said so
with a border colour and a JSON blob cut off at 72 characters, and a failed call looked
like a slightly redder successful one. While a reply streamed, the turn said only
"streaming...".

What this story adds, and what this artifact checks:

* **the context** each answer was given, on one line, built by `chatContextSnapshot()`
  from the same helpers that build the request - including the difference between the
  directory that was sent (possibly nothing) and the one the server will read instead;
* **tool transparency**: a status *word* per call (colour alone is not a status), the
  step, the elapsed time and the size of the arguments, with the full arguments and the
  server's capped output one disclosure below, and a summary line that counts the calls
  and the failures instead of making the reader open every one;
* **streaming transparency**: how long the turn has been going and when the first token
  arrived, measured with `performance.now()` at the moment the pane saw it - a snapshot
  per render, not a timer, and never a number nobody measured.

The rules fail when a turn can be drawn without its context, when the context is
recomputed at render time instead of captured at send time, when a tool loses its status
word, when the arguments are truncated in the data (the summary may show a prefix; the
body may not), when the summary counts drift from the list, when a timing is invented,
or when the pane reads a `chat_tool` field the server does not send.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Context.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-context.md"

TONES = ("ok", "warn", "danger", "info", "busy", "neutral")
# The fields the route yields on a chat_tool event; the pane must not read others.
TOOL_EVENT_FIELDS = ("step", "tool", "args", "ok", "elapsed_ms", "preview")


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


def tool_event_contract(app_source: str) -> dict:
    """What ``/api/chat/stream`` yields on ``chat_tool``, read from the handler."""
    start = app_source.find("async def api_chat_stream")
    body = ""
    if start >= 0:
        end = app_source.find("\n@app.", start)
        body = app_source[start : end if end > start else start + 6000]
    block = re.search(r'_sse\(\s*"chat_tool",\s*\{(.*?)\n\s*\},', body, re.S)
    fields = sorted(re.findall(r'"(\w+)":', block.group(1))) if block else []
    preview_cap = re.search(r'"preview":\s*\(compressed_out or ""\)\[:(\d+)\]', body)
    return {
        "route": "/api/chat/stream" if start >= 0 else None,
        "event": "chat_tool" if block else None,
        "fields": fields,
        "preview_cap": int(preview_cap.group(1)) if preview_cap else None,
    }


def read_frontend(html: str) -> dict:
    render = function_body(html, "renderChatMessages")
    request = function_body(html, "sendChatRequest")
    snapshot = function_body(html, "chatContextSnapshot")
    line = function_body(html, "chatContextLine")
    tool = function_body(html, "renderChatTool")
    summary = function_body(html, "chatToolSummary")
    streaming = function_body(html, "chatStreamingMeta")
    persona = function_body(html, "chatPersonaLabel")
    return {
        "functions": {
            name: bool(function_body(html, name))
            for name in (
                "chatContextSnapshot",
                "chatContextLine",
                "renderChatTool",
                "chatToolSummary",
                "chatStreamingMeta",
            )
        },
        "context": {
            # Captured when the request is built, not recomputed at render time.
            "captured_at_send": "const context = chatContextSnapshot(" in request,
            # The mirror defect: a line recomputed at render time describes the composer
            # now, not the request that was sent.
            "not_recomputed": "chatContextSnapshot(" not in render,
            "persona_from_the_helper": "persona: chatPersonaLabel()," in snapshot,
            "attached_to_the_turn": "context," in request and "context" in render,
            "rendered": 'data-context="sent"' in render,
            "fields": sorted(
                {
                    field
                    for field in (
                        "model",
                        "persona",
                        "reasoning",
                        "tools",
                        "workspace",
                        "workspace_effective",
                        "skills",
                        "messages",
                    )
                    if re.search(rf"^\s*{field}:", snapshot, re.M)
                    or re.search(rf"^\s*{field},\s*$", snapshot, re.M)
                }
            ),
            "sent_vs_effective": "workspace:" in snapshot
            and "workspace_effective:" in snapshot,
            "payload_reads_the_same_values": "reasoning_mode: context.reasoning"
            in request,
            "persona_label_from_the_select": "selectedOptions" in persona
            or "selectedIndex" in persona,
            "skills_count_from_the_set": "selectedSkills.size" in snapshot,
            "message_count_from_the_request": "chatContextSnapshot(model, apiMessages.length"
            in request,
            "line_names_the_model": "model ${ctx.model}" in line,
            "line_names_the_effective_workspace": "workspace_effective" in line,
            # A zero-skill line is noise; a line that hides a non-zero count is a lie.
            "skills_only_when_sent": "if (ctx.skills)" in line,
        },
        "tools": {
            "status_word": 'const status = tool.ok ? "OK" : "FAILED";' in tool,
            "status_rendered": "chat-tool-status" in tool and "${status}" in tool,
            "status_tone": 'data-tone="${tool.ok ? "ok" : "danger"}"' in tool,
            "not_colour_only": "border-color:" not in tool,
            "step_shown": "step ${step}" in tool,
            "elapsed_shown": "${elapsed} ms" in tool,
            "args_size_shown": "args.length} B of arguments" in tool,
            "full_args_in_body": "JSON.stringify(tool.args || {}, null, 1)" in tool,
            "args_not_truncated": ".slice(" not in tool
            and "JSON.stringify(tool.args || {}, null, 1)" in tool,
            "output_cap_stated": "first 500 characters" in tool,
            "summary_counts": "tools.filter(t => !t.ok)" in summary
            and "tools.length" in summary,
            "summary_total_ms": "reduce(" in summary,
            "summary_rendered": "chatToolSummary(m.tools)" in render,
            "rows_rendered": "m.tools.map(renderChatTool)" in render,
            "reasoning_size": "Reasoning Trace · ${m.reasoning.length} chars" in render,
            "turn_marks_failed_turns": 'data-tool-status="' in tool,
        },
        "busy": {
            # aria-busy must follow the turn, not only the send flag.
            "follows_a_streaming_turn": 'chatHistory.some(m => m.role === "assistant" && !m.usage)'
            in html,
            "keeps_the_send_flag": "chatBusy || streaming" in html,
        },
        "timing": {
            "measured": "performance.now()" in request
            and "performance.now()" in streaming,
            "started_at_send": "startedAt: performance.now()" in request,
            "first_token_on_first_token": "if (!asstMsg.timing.firstTokenAt) asstMsg.timing.firstTokenAt = performance.now();"
            in request,
            "streaming_meta_says_waiting": "waiting for the first token" in streaming,
            "final_meta_shows_ttft": "first token ${" in render,
            "no_timer": "setInterval" not in streaming + render,
            # The finished turn's elapsed time stops when the stream did, not when the
            # pane happened to render next.
            "clock_frozen_at_done": "asstMsg.timing.endedAt = performance.now();"
            in request,
            "finished_elapsed_uses_it": "m.timing.endedAt || performance.now()"
            in render,
            "snapshot_is_documented": "snapshot taken at each render" in html,
        },
        "payload": {
            "fields": sorted(
                {
                    field
                    for field in (
                        "model",
                        "messages",
                        "reasoning_mode",
                        "auto_unload",
                        "tools_enabled",
                        "workspace_dir",
                        "skills",
                    )
                    if re.search(rf"^\s*{field}", request, re.M)
                }
            ),
            "tool_fields_read": sorted(set(re.findall(r"tool\.(\w+)", tool))),
        },
        "styles": {
            "context": ".chat-context {" in html,
            "status": ".chat-tool-status {" in html,
            "status_tones": '.chat-tool-status[data-tone="danger"]' in html
            and '.chat-tool-status[data-tone="ok"]' in html,
            "summary": ".chat-tools-summary {" in html,
            "label": ".chat-tool-label {" in html,
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = tool_event_contract(APP.read_text(encoding="utf-8"))
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    # 1. The context is captured once, from the request's own values.
    context = frontend["context"]
    for flag, description in (
        ("captured_at_send", "the context is not captured when the request is built"),
        ("attached_to_the_turn", "the turn does not carry the context it was sent"),
        ("rendered", "the transcript does not print the context"),
        (
            "not_recomputed",
            "the context line is recomputed at render time instead of being the one that was sent",
        ),
        (
            "persona_from_the_helper",
            "the persona in the line is not read from the persona control",
        ),
        (
            "sent_vs_effective",
            "the pane cannot tell a sent directory from the one the server will read",
        ),
        (
            "payload_reads_the_same_values",
            "the request and the context line can disagree about the reasoning mode",
        ),
        (
            "persona_label_from_the_select",
            "the persona in the line is not the selected persona",
        ),
        ("skills_count_from_the_set", "the skills count is not the set that is sent"),
        (
            "message_count_from_the_request",
            "the message count is not the request's own count",
        ),
        (
            "line_names_the_model",
            "the line does not name the model the request was sent to",
        ),
        (
            "line_names_the_effective_workspace",
            "the line does not say which directory will be read",
        ),
        (
            "skills_only_when_sent",
            "the line would hide an active skill count, or print a zero one",
        ),
    ):
        if not context.get(flag):
            fail("context", description)
    expected_fields = {
        "model",
        "persona",
        "reasoning",
        "tools",
        "workspace",
        "workspace_effective",
        "skills",
        "messages",
    }
    missing = expected_fields - set(context["fields"])
    if missing:
        fail("context-incomplete", f"the context snapshot is missing {sorted(missing)}")

    # 2. Tool calls: a word, a size, and the whole argument object one click down.
    tools = frontend["tools"]
    for flag, description in (
        ("status_word", "a tool call has no status word"),
        ("status_rendered", "the status word is not drawn"),
        ("status_tone", "the status word carries no tone"),
        (
            "not_colour_only",
            "a tool call is statused by border colour, which a reader who cannot see colour loses",
        ),
        ("step_shown", "a tool call does not say which step it belongs to"),
        ("elapsed_shown", "a tool call does not say how long it took"),
        ("args_size_shown", "a tool call does not say how large its arguments were"),
        ("full_args_in_body", "the full arguments are not available"),
        (
            "args_not_truncated",
            "the arguments are truncated in the data, so the rest is unreachable",
        ),
        (
            "output_cap_stated",
            "the pane does not say the server caps the output it sends",
        ),
        ("summary_counts", "the tool summary does not count calls and failures"),
        ("summary_total_ms", "the tool summary does not total the time"),
        ("summary_rendered", "the summary is not drawn"),
        ("rows_rendered", "the tool rows are not drawn"),
        ("reasoning_size", "the reasoning disclosure does not say how large it is"),
    ):
        if not tools.get(flag):
            fail("tools", description)
    for tone in re.findall(
        r'data-tone="([a-z]+)"', function_body(html, "renderChatTool")
    ):
        if tone not in TONES:
            fail("tone-unknown", f"a tool status uses tone {tone!r}")

    # 2b. Busy is the turn's state, not the pane's.
    busy = frontend["busy"]
    if not busy["follows_a_streaming_turn"]:
        fail(
            "busy",
            "the transcript reports busy from the send flag alone, so a turn arriving without a send reads as idle",
        )
    if not busy["keeps_the_send_flag"]:
        fail(
            "busy",
            "the transcript drops the send flag, so a send that has not yet made a turn reads as idle",
        )

    # 3. Timing is measured, and says so while there is nothing to measure yet.
    timing = frontend["timing"]
    for flag, description in (
        ("measured", "a timing is not measured"),
        ("started_at_send", "the turn does not record when the request went out"),
        (
            "first_token_on_first_token",
            "the first-token time is not taken from the first token",
        ),
        (
            "streaming_meta_says_waiting",
            "a streaming turn claims a first-token time before one arrived",
        ),
        (
            "final_meta_shows_ttft",
            "the finished turn does not show the first-token time",
        ),
        ("no_timer", "a timer drives the numbers, so they move without a render"),
        (
            "clock_frozen_at_done",
            "the finished turn's clock is not stopped when the stream stops",
        ),
        (
            "finished_elapsed_uses_it",
            "the finished turn keeps counting seconds after the stream stopped",
        ),
    ):
        if not timing.get(flag):
            fail("timing", description)

    # 4. The pane reads only fields the server sends.
    unknown = [
        field
        for field in frontend["payload"]["tool_fields_read"]
        if field not in TOOL_EVENT_FIELDS
    ]
    if unknown:
        fail(
            "event-drift",
            f"the pane reads {unknown}, which the chat_tool event does not send",
        )
    if server["fields"] and not set(TOOL_EVENT_FIELDS) <= set(server["fields"]):
        missing = sorted(set(TOOL_EVENT_FIELDS) - set(server["fields"]))
        fail("event-drift", f"the chat_tool event no longer sends {missing}")
    if server["preview_cap"] and server["preview_cap"] != 500:
        fail(
            "event-drift",
            f"the server now caps tool output at {server['preview_cap']} characters, which the pane still describes as 500",
        )

    # 5. What is drawn has rules.
    for flag, description in (
        ("context", "the context line has no rule"),
        ("status", "the tool status has no rule"),
        ("status_tones", "the status tones have no rules"),
        ("summary", "the tool summary has no rule"),
        ("label", "the tool body labels have no rule"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-05",
        "statement": (
            "Every Chat answer records the context it was sent - persona, reasoning mode, tools, skills, message count "
            "and the directory that will be read - and prints it; every tool call prints a status word, its step, its "
            "time and the size of its arguments, with the arguments and the server's capped output one disclosure "
            "below; and a streaming turn reports how long it has been going and when the first token arrived, measured."
        ),
        "source": {
            "frontend": "static/index.html",
            "server": "app.py",
            "routes": [server["route"]] if server["route"] else [],
        },
        "server": server,
        "context_fields": frontend["context"]["fields"],
        "tool_event_fields": TOOL_EVENT_FIELDS,
        "tool_output_cap": server["preview_cap"],
        "frontend": frontend,
        "totals": {
            "context_fields": len(frontend["context"]["fields"]),
            "tool_event_fields": len(server["fields"]),
            "tool_status_tone": 2,
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the streaming clock is a snapshot taken at each render: the pane has no timer, so the number stops moving "
            "when no token arrives. That is deliberate - a frozen clock is a fact, a guessed tick is not - and it is "
            "stated here and on the page",
            "the tool output is whatever the server sent, capped at 500 characters in app.py; the pane says so instead "
            "of implying the model saw more",
            "the first-token time is when the browser applied the first token it received, which includes the network "
            "and the pane's own render cost - it is not the model's TTFT, and the perf record in the rail is where the "
            "model's own number lives",
            "the walk writes the turns it judges, because a scripted demo reply does not call tools; the real send it "
            "also performs proves the context line on a live request",
        ],
    }


def render_doc(payload: dict) -> str:
    fields = "\n".join(
        f"| `{field}` | {description} |"
        for field, description in (
            (
                "model",
                "the model the request was sent to, which is not always the one that answers",
            ),
            ("persona", "the selected persona's label, not its prompt"),
            ("reasoning", "the reasoning mode the request carried"),
            ("tools", "tools on or off"),
            ("workspace", "the directory the pane literally sent - possibly nothing"),
            (
                "workspace_effective",
                "what the server will read instead when nothing was sent",
            ),
            (
                "skills",
                "how many active skills went with it, printed only when there were any",
            ),
            (
                "messages",
                "how many messages the request carried, system prompt included",
            ),
        )
    )
    lines = [
        "# Chat: the context a reply was given, and what its tools did",
        "",
        "Generated by `python3 scripts/uiux_chat_context_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## The context line",
        "",
        "| Field | What it is |",
        "|---|---|",
        fields,
        "",
        "`chatContextSnapshot()` is called once, when the request is built, and the snapshot is stored on the turn: "
        "recomputing it at render time would describe the composer as it is now, not the request that was sent. The "
        "line is printed above the reasoning and the tools, with the whole sentence in its `title`. It opens with",
        "the model because the turn header names the model that answered: if the server ever falls back, the header",
        "and the context disagree, and a copied line names what was actually asked for.",
        "",
        "The two workspace fields exist because they are two different facts. `workspace` is what the pane sent - often "
        "nothing, because the field is empty. `workspace_effective` is what `POST /api/chat/stream` will read in that "
        "case, `WORKSPACE_ROOT`. Printing only the second would hide what was sent; printing only the first would "
        "leave the reader guessing.",
        "",
        "## Tool calls",
        "",
        "A call used to be a border colour: cyan-ish for a success, red-ish for a failure, and a JSON blob cut off at "
        "72 characters. Now each row is:",
        "",
        "- a **status word** - `OK` or `FAILED` - with a tone, so colour is reinforcement rather than the message;",
        "- the tool name, its `step`, its elapsed time, and the byte length of its arguments;",
        "- a disclosure holding the **whole argument object** and the server's output, labelled with the cap the server "
        f"applies ({payload['tool_output_cap']} characters in `app.py`).",
        "",
        "Above the rows, one line counts them: `2 tool calls · 1 failed · 412 ms`. A reader who does not open a single "
        "disclosure still knows whether the answer used tools, and whether any of them failed.",
        "",
        "The pane reads exactly the fields the route yields on a `chat_tool` event - "
        f"`{'`, `'.join(payload['tool_event_fields'])}` - and the inventory fails if either side changes.",
        "",
        "## While it streams",
        "",
        "A turn in flight says `streaming… · 3.2s · first token 0.41s · 128 chars`, and the finished turn keeps the "
        "elapsed and first-token figures beside the model's own tok/s and token count.",
        "",
        "Both numbers are taken with `performance.now()`: the request records when it went out, and the first token "
        "records itself when the pane applies it. Before a token arrives the turn says *waiting for the first token* "
        "rather than showing a zero. The clock is a snapshot per render - the pane has no timer - so it stops when the "
        "stream does, which is the honest behaviour for a figure nobody is measuring. A finished turn freezes its",
        "elapsed time when the stream ends (`endedAt`), so a later re-render cannot keep counting seconds; and",
        "`aria-busy` on the transcript follows a turn that has no usage yet, not only the pane's own send flag, so a",
        "turn arriving from a resumed stream is not announced as idle.",
        "",
        "## What it looks like",
        "",
        "Captured by `scripts/chat_context_walk.mjs --screenshots docs/media/chat` against",
        "`python3 scripts/demo_tour.py --serve`, through this environment's npm Chromium:",
        "",
        "| Capture | What it shows |",
        "|---|---|",
        "| [`context-tools.png`](media/chat/context-tools.png) | One answer with the context line, `2 tool calls · 1 failed · 127 ms`, an `OK` row and a `FAILED` row with their step, time and argument sizes, and `1.8s · first token 0.41s` below |",
        "| [`streaming.png`](media/chat/streaming.png) | A turn in flight: `streaming… · 1.2s · waiting for the first token · 128 chars`, and the finished turn above it still showing its own figures |",
        "| [`sent-context.png`](media/chat/sent-context.png) | A real send, with the context recorded on it: the model, the persona, and the workspace the server read |",
        "| [`narrow-context.png`](media/chat/narrow-context.png) | The same surface at 900px: no horizontal overflow, the context line and tool rows intact |",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "`node scripts/chat_context_walk.mjs` writes a turn with a passing and a failing tool call and holds the pane "
        "against it: statuses, counts, full arguments, the context line; then it sends a real message and checks the "
        "context recorded on that turn against the composer it was sent from.",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`, or a field in the `chat_tool` event.",
        "2. `python3 scripts/uiux_chat_context_inventory.py`.",
        "3. `node scripts/chat_context_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
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
            print("error: the chat context inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_chat_context_inventory.py")
            return 1
        if payload["findings"]:
            print(
                "error: Chat's context line, tool rows and timings disagree with the source:"
            )
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"chat context OK: {payload['totals']['context_fields']} context fields, "
            f"{payload['totals']['tool_event_fields']} chat_tool fields, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['context_fields']} context fields, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
