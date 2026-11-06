#!/usr/bin/env python3
"""Inventory Chat's streaming, tool disclosure, copy, failure and handoff contract (UIV2-20).

    python3 scripts/uiux_chat_recovery_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_chat_recovery_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_chat_recovery_inventory.py --json     # print the inventory

The pane could already stop a stream and draw a failure, but four things it showed were not the
whole truth:

* **Copy copied nothing, silently.** `copyCodeBlock` called `navigator.clipboard.writeText` and threw
  the promise away, so in a browser that refuses the clipboard the button said "Copied!" over an
  empty clipboard. Four code blocks in a reply also offered four identical "Copy" buttons, naming
  none of them.
* **Retry repeated whatever the failed attempt had already done.** A turn that failed *after* tool
  calls had run was retried by re-sending the same message with no mention of the calls that had
  already happened — the one duplicate-effect path this pane may not have.
* **A stopped turn said "Stopped" and nothing else**: not how much of the answer arrived, not that
  the partial text was kept.
* The tool disclosure, its 500-character output cap and the handoff brief existed but had never been
  exercised, because the fixture runtime answered prose and never made a tool call.

What this inventory checks: that copy has a failure path, a fallback, a label that names the block
and an announcement; that a tool call is a disclosure whose status is a word, with its step, time,
argument size, arguments, output and the server's cap; that the turn counts its calls and whether
they worked; that stopping records how much was kept and hands the composer back; that a retry's
plan distinguishes a turn that ran nothing from a turn that ran tools, keeps the failed turn when it
did, and marks the new question; that the handoff brief names the messages, the tool calls and the
context, and posts nothing; and that the walk which proves all of it exists and covers the six
states the ledger asks for.

It fails when a copy can claim success without copying, when a retry can repeat a tool effect
silently, when a stopped turn stops saying how far it got, or when the handoff stops saying what it
carries.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK_PATH = PROJECT_ROOT / "scripts" / "chat_recovery_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-recovery.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "chat-recovery"

STORY = "UIV2-20"
TITLE = "Chat streaming, tools, handoff and recovery UX"

#: The cap app.py puts on what it sends the pane for one tool call.
TOOL_OUTPUT_CAP = 500
#: The states the ledger's visual matrix names.
STATES = ("empty", "populated", "long-content", "loading", "error", "recovery")


def function_body(source: str, name: str) -> str:
    """The body of a top-level `function name(...) { … }`, braces balanced.

    The opening brace is looked for after the parameter list closes: a default parameter such as
    `options = {}` would otherwise be mistaken for the body and every check would read `{}`.
    """
    start = source.find(f"function {name}(")
    if start < 0:
        return ""
    paren = source.index("(", start)
    depth_params = 0
    for position in range(paren, len(source)):
        if source[position] == "(":
            depth_params += 1
        elif source[position] == ")":
            depth_params -= 1
            if depth_params == 0:
                paren = position
                break
    index = source.index("{", paren)
    depth = 0
    for position in range(index, len(source)):
        char = source[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index : position + 1]
    return ""


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]

    copy_block = function_body(script, "copyCodeBlock")
    clipboard = function_body(script, "writeClipboard")
    announce = function_body(script, "copyAnnounce")
    render_tool = function_body(script, "renderChatTool")
    summary = function_body(script, "chatToolSummary")
    render = function_body(script, "renderChatMessages")
    plan = function_body(script, "chatRetryPlan")
    present_error = function_body(script, "presentChatError")
    retry = function_body(script, "retryChatRequest")
    request = function_body(script, "sendChatRequest")
    handoff_brief = function_body(script, "chatHandoffBrief")
    handoff_reason = function_body(script, "chatHandoffReason")
    handoff_update = function_body(script, "updateChatHandoff")
    streaming_meta = function_body(script, "chatStreamingMeta")

    code_bar = re.search(r'<div class="code-bar"><span>.*?</button>', script, re.S)
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # ── copy ─────────────────────────────────────────────────────────────────
    check(
        "a code block's copy control names the block and its size",
        code_bar is not None
        and "aria-label=\"Copy the ${escapeHtml(label)} block (${chars} characters)\"" in script
        and 'data-lang="${escapeHtml(label)}"' in script,
        "aria-label carries the language and the character count; the wrap carries data-lang",
    )
    check(
        "copying awaits the clipboard and reports what happened",
        "await writeClipboard(text)" in copy_block
        and 'btn.dataset.state = copied ? "copied" : "failed"' in copy_block
        and "Copy unavailable" in copy_block,
        "copyCodeBlock() awaits, sets data-state and says Copy unavailable when neither path works",
    )
    check(
        "a refused clipboard is copied through a selection fallback",
        "document.execCommand(\"copy\")" in clipboard and "createElement(\"textarea\")" in clipboard,
        "writeClipboard() falls back to a selected textarea before it reports failure",
    )
    writes = script.count("navigator.clipboard.writeText")
    check(
        "the code-block copy never touches the clipboard directly",
        "navigator.clipboard" not in copy_block
        and "await navigator.clipboard.writeText(text)" in clipboard,
        f"copyCodeBlock() goes through writeClipboard(), which awaits; the sheet's other {writes - 1} "
        "call sites belong to other panes and are unchanged by this story",
    )
    check(
        "a copy is announced, not just painted",
        "aria-live" in announce and "role\", \"status\"" in announce and "copyAnnounce(" in copy_block,
        "copyAnnounce() fills one shared role=status region and copyCodeBlock() calls it",
    )

    # ── tool disclosure ──────────────────────────────────────────────────────
    check(
        "a tool call is a disclosure, not a line of text",
        "<details class=\"think-box chat-tool\"" in render_tool and "<summary" in render_tool,
        "renderChatTool() returns a <details> with a <summary>",
    )
    check(
        "its status is a word, not only a colour",
        'uiStatus("ok").label' in render_tool and 'uiStatus("failed").label' in render_tool,
        "the summary prints the status label",
    )
    check(
        "it states the step, the time and the size of what it was given",
        "step ${step}" in render_tool and "${elapsed} ms" in render_tool and "B of arguments" in render_tool,
        "step, elapsed milliseconds and argument bytes in the summary",
    )
    check(
        "opening it shows the arguments, the output and the server's cap",
        "Arguments</div><pre>" in render_tool
        and "the server sends the first " in render_tool
        and str(TOOL_OUTPUT_CAP) in render_tool,
        f"the output label names the {TOOL_OUTPUT_CAP}-character cap",
    )
    check(
        "the turn counts its tool calls and says whether they worked",
        "tool call" in summary and "failed" in summary and "ms" in summary,
        "chatToolSummary() counts the calls, the failures and the total time",
    )
    check(
        "the tools are rendered into the turn, above the reply",
        "chat-tools-summary" in render and "m.tools.map(renderChatTool)" in render,
        "every tool in `message.tools` is drawn as a disclosure",
    )

    # ── streaming and stop ───────────────────────────────────────────────────
    check(
        "a streaming turn reports elapsed time, first token and size",
        "first token" in streaming_meta and "chars" in streaming_meta,
        "chatStreamingMeta() measures rather than guesses",
    )
    check(
        "stopping records how much of the answer had arrived",
        "asstMsg.stoppedChars = (asstMsg.content || \"\").length" in script,
        "the stopped handler freezes the character count before it renders",
    )
    check(
        "a stopped turn says who stopped it and how much was kept",
        "Stopped by you" in render and "kept in this pane" in render,
        "the meta line names the reader and the characters kept",
    )
    check(
        "stopping hands the composer back with the cursor in the field",
        'document.getElementById("chatInput").focus({ preventScroll: true })' in script,
        "the stop handler focuses the composer when the reader stopped it",
    )

    # ── the retry plan ───────────────────────────────────────────────────────
    check(
        "a retry's meaning is decided by what the failed attempt did",
        "chatRetryPlan" in script and "message.tools.length" in plan and "replace" in plan,
        "chatRetryPlan() reads the failed turn's tool count",
    )
    check(
        "a turn that ran nothing is a plain retry that replaces the placeholder",
        'label: "Retry"' in plan and "replace: true" in plan,
        "no tool calls: Retry, replace",
    )
    check(
        "a turn that ran tools is asked again, not retried silently",
        "Ask again (" in plan and "replace: false" in plan and "not repeated silently" in plan,
        "tool calls: Ask again (N tool calls already ran), keep the failed turn",
    )
    check(
        "the failure card carries the plan and says what will not be repeated",
        "retryPlan" in present_error and "does not repeat them" in present_error,
        "presentChatError() prints the plan's sentence and label",
    )
    check(
        "retrying keeps the failed turn when it did something",
        "chatRetryPlan(assistantMessage)" in retry
        and "if (plan.replace)" in retry
        and "appendUser: true, retry: plan" in retry,
        "retryChatRequest() splices only when the failed turn is a placeholder",
    )
    check(
        "the question asked again says so on the question",
        "asked.retryOf = { tool_calls: options.retry.ran }" in request
        and "m.retryOf" in render
        and "chat-retry-note" in render,
        "the user turn carries retryOf, and the transcript renders it",
    )

    # ── the handoff ──────────────────────────────────────────────────────────
    check(
        "the handoff brief names the messages and the tool calls it carries",
        "Conversation so far:" in handoff_brief and "Tool calls in this conversation" in handoff_brief,
        "chatHandoffBrief() counts the conversation and lists the calls",
    )
    check(
        "the brief carries the context the last reply was sent with",
        "The last reply was sent with" in handoff_brief and "chatContextLine" in handoff_brief,
        "the same context line the transcript shows",
    )
    check(
        "the handoff explains why it is off, or what it will do",
        "Nothing to hand over yet" in handoff_reason
        and "still arriving" in handoff_reason
        and "Already in the Harness composer" in handoff_reason,
        "chatHandoffReason() covers nothing, streaming, handed and ready",
    )
    check(
        "the handoff posts nothing and starts nothing",
        "fetch(" not in handoff_update and "fetch(" not in handoff_brief,
        "the handoff writes a composer's text, never a request",
    )

    # ── the walk and its evidence ────────────────────────────────────────────
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    recorded = len(re.findall(r"record\(", walk))
    check(
        "the browser walk exists and asserts the copy, the tool disclosure and the retry plan",
        recorded >= 20
        and "navigator.clipboard.readText()" in walk
        and "chatRetryPlan" in walk
        and ".chat-tool" in walk
        and "denied by the browser" in walk,
        f"{WALK_PATH.name}: {recorded} recorded checks, clipboard read back, fallback forced, "
        "tool disclosure and retry plan asserted",
    )
    check(
        "the walk covers the six states the ledger asks for",
        all(state in walk for state in ("empty", "populated", "long", "loading", "error", "recovery")),
        "STATES = empty, populated, long, loading, error, recovery",
    )
    check(
        "the walk reads the clipboard rather than trusting the button",
        "navigator.clipboard.readText()" in walk,
        "the copied text is compared to the block's own text",
    )
    captures = sorted(MEDIA_DIR.glob("*.png")) if MEDIA_DIR.exists() else []
    before = sorted((MEDIA_DIR / "before-uiv2-20").rglob("*.png")) if MEDIA_DIR.exists() else []
    check(
        "the state captures are present for every viewport and theme, with a before set",
        len(captures) >= 48 and len(before) >= 16,
        f"{len(captures)} after PNG, {len(before)} before PNG",
    )

    findings = [
        {"check": entry["check"], "detail": entry["detail"]}
        for entry in checks
        if not entry["passed"]
    ]
    return {
        "story": STORY,
        "title": TITLE,
        "schema_version": 1,
        "source": str(FRONTEND.relative_to(PROJECT_ROOT)),
        "states": list(STATES),
        "capabilities": {
            "copy": {
                "control": ".code-bar button",
                "fallback": "selection + document.execCommand('copy')",
                "announced_in": "#copyAnnounce (role=status)",
            },
            "tools": {
                "renderer": "renderChatTool",
                "summary": "chatToolSummary",
                "output_cap": TOOL_OUTPUT_CAP,
            },
            "streaming": {"meta": "chatStreamingMeta", "stop": "stopChatStream"},
            "retry": {"plan": "chatRetryPlan", "card": "presentChatError", "keep_failed_turn": True},
            "handoff": {"brief": "chatHandoffBrief", "reason": "chatHandoffReason", "posts": False},
        },
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# Chat streaming, tools, handoff and recovery",
        "",
        f"Story **{payload['story']}** — {payload['title']}.",
        "",
        "| capability | what it says |",
        "| --- | --- |",
        "| copy | a labelled Copy per code block, awaited, with a selection fallback and one shared",
        "| | `role=status` announcement |",
        "| tools | a disclosure per call: status in a word, step, time, argument bytes, arguments,",
        f"| | output and the server's {payload['capabilities']['tools']['output_cap']}-character cap |",
        "| streaming | measured elapsed time, first token and size; Stop keeps the partial reply and",
        "| | says who stopped it and how much was kept |",
        "| retry | nothing ran → Retry; a tool ran → Ask again (N tool calls already ran), a new",
        "| | question with the failed attempt kept and the note on the question |",
        "| handoff | a brief naming the messages, the tool calls and the context; it posts nothing |",
        "",
        "## Checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} finding(s).",
        "",
        "| check | result | detail |",
        "| --- | --- | --- |",
    ]
    for entry in payload["checks"]:
        lines.append(f"| {entry['check']} | {'pass' if entry['passed'] else '**FAIL**'} | {entry['detail']} |")
    lines += [
        "",
        "## States",
        "",
        ", ".join(f"`{state}`" for state in payload["states"]) + ".",
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_chat_recovery_inventory.py",
        "python3 scripts/uiux_chat_recovery_inventory.py --check",
        "```",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale or a rule is broken")
    parser.add_argument("--json", action="store_true", help="print the inventory instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    payload = build_inventory()

    if args.json:
        print(dumps(payload), end="")
    else:
        rendered_json = dumps(payload)
        rendered_doc = render_doc(payload)
        stale = (
            not JSON_PATH.exists()
            or JSON_PATH.read_text(encoding="utf-8") != rendered_json
            or not DOC_PATH.exists()
            or DOC_PATH.read_text(encoding="utf-8") != rendered_doc
        )
        if args.check:
            if stale:
                print(
                    "the chat-recovery inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    findings = payload["findings"]
    if findings:
        for entry in findings:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
