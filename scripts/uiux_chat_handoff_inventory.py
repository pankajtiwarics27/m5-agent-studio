#!/usr/bin/env python3
"""Inventory Chat's handoff to the Harness (UIUX-P2-06).

    python3 scripts/uiux_chat_handoff_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_chat_handoff_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_chat_handoff_inventory.py --json     # print the inventory

Chat is where the thinking happens and the Harness is where work is admitted: it owns
the workspace, the execution policy and the approval surface. So the crossing between
them is the interesting part, and every way of getting it wrong is a real defect:

* a silent handoff, where pressing a button starts a run nobody chose;
* a handoff that sends the conversation to a server before a human has decided anything;
* a handoff that carries a summary this pane invented instead of what the turns recorded;
* a handoff that loses the transcript, or fails without saying so;
* a handoff that leaves the reader in the wrong pane with no way back.

This artifact reads `handOffChatToHarness()` and the brief it builds, and fails when the
button can run something, when the handler can reach the network or start a run, when it
can empty the transcript, when the brief stops coming from the recorded turns (their
statuses, their times, their outputs and the P2-05 context line), when it copies more
tool output than it admits to, when it lists an unbounded number of calls, when the
sentence beside the button and the button's own state can disagree, or when the receipt
it leaves has no way back to Chat.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Handoff.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-handoff.md"

# The Harness admits work; these are the calls that would admit it from here.
RUNNING_CALLS = ("executeHarness", "harnessRunBtn", "startHarness", "runHarness")
NETWORK_CALLS = ("fetch(", "XMLHttpRequest", "EventSource", "navigator.sendBeacon")
REASONS = ("nothing", "streaming", "ready", "handed")


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


def element(html: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", html)
    return match.group(0) if match else ""


def read_frontend(html: str) -> dict:
    handler = function_body(html, "handOffChatToHarness")
    brief = function_body(html, "chatHandoffBrief")
    reason = function_body(html, "chatHandoffReason")
    update = function_body(html, "updateChatHandoff")
    stats = function_body(html, "chatHandoffStats")
    clear = function_body(html, "clearChat")
    composer_body = function_body(html, "updateChatComposer")
    button = element(html, "chatHandoffBtn")
    back_match = re.search(
        r'label: "Back to Chat",(.*?)label: "Open Harness"', handler, re.S
    )
    way_back = back_match.group(1) if back_match else ""
    hint = element(html, "chatHandoffHint")
    return {
        "functions": {
            name: bool(function_body(html, name))
            for name in (
                "chatHandoffStats",
                "chatHandoffBrief",
                "chatHandoffReason",
                "updateChatHandoff",
                "handOffChatToHarness",
            )
        },
        "control": {
            "is_a_button": button.startswith("<button"),
            "type_is_button": 'type="button"' in button,
            "starts_disabled": "disabled" in button,
            "described_by_the_sentence": 'aria-describedby="chatHandoffHint"' in button,
            "has_a_title": "title=" in button,
            "handler_wired": 'onclick="handOffChatToHarness()"' in button,
            "sentence_is_a_live_region": 'role="status"' in hint
            and 'aria-live="polite"' in hint,
            "sentence_near_the_button": bool(button) and bool(hint),
        },
        "safety": {
            # The whole story in five booleans: it carries, it does not run, it does not
            # post, it does not clear, and it says so when it cannot carry.
            "no_network": not any(call in handler for call in NETWORK_CALLS),
            "no_run": not any(call in handler for call in RUNNING_CALLS),
            "no_clear": "chatHistory = []" not in handler
            and "chatHistory.length = 0" not in handler,
            "every_exit_is_visible": handler.count("return ") >= 3,
            "failure_is_presented": 'kind: "error"' in handler
            and '"The handoff did not happen"' in handler,
            "failure_keeps_the_transcript": "untouched" in handler,
            "failure_offers_a_retry": 'label: "Retry"' in handler,
            "failure_forgets_the_carry": handler.count("chatHandoffMark = null;") == 1,
        },
        "carry": {
            "fills_the_harness_composer": "composer.value = brief;" in handler,
            "focuses_it": "composer.focus();" in handler
            and handler.count("composer.focus()") == 1,
            "switches_to_the_harness": 'switchTab("harness");' in handler,
            "returns_what_it_carried": "return composer.value;" in handler,
            "no_task_no_handoff": "if (!brief) {" in handler,
        },
        "brief": {
            "task_is_the_last_user_message": 'chatHistory.filter(m => m.role === "user"'
            in stats,
            "counts_messages_and_replies": "messages: chatHistory.length" in stats
            and "replies: replies.length" in stats,
            "counts_calls_and_failures": "calls: calls.length" in stats
            and "failed: calls.filter(t => !t.ok).length" in stats,
            "uses_the_recorded_context": "withContext[withContext.length - 1].context"
            in stats,
            "prints_the_context_line": "chatContextLine(stats.context)" in brief,
            "says_nothing_ran": "nothing has run" in brief,
            "statuses_are_words": '"OK" : "FAILED"' in brief,
            "says_step_and_time": "step ${step}" in brief and "ms" in brief,
            "bounds_the_calls": ".slice(-8)" in brief,
            "admits_the_bound": "earlier call(s) are in the Chat transcript" in brief,
            "clips_the_outputs": "chatHandoffClip(tool.preview, 200)" in brief,
            "admits_the_clip": "clipped to 200 characters" in brief,
            "admits_the_server_cap": "500-character cap" in brief,
            "points_at_the_transcript": "in the Chat pane" in brief,
        },
        "sentence": {
            "one_predicate": "const state = chatHandoffReason(stats);" in update,
            "button_reads_it": 'button.disabled = state.reason !== "ready";' in update,
            "every_reason_declared": all(
                f'reason: "{name}"' in reason for name in REASONS
            ),
            "reason_counts_what_it_carries": "Carries ${chatCount(stats.messages"
            in reason,
            "waits_while_streaming": "if (chatBusy)" in reason,
            "remembers_what_was_handed": "chatHandoffMark.messages === stats.messages"
            in reason,
            "written_only_on_change": "signature !== chatHandoffSignature" in update,
            "one_hook_from_the_composer": "updateChatHandoff();" in composer_body
            and composer_body.count("updateChatHandoff();") == 1,
        },
        "receipt": {
            "is_presented_not_written": 'kind: "recovery"' in handler
            and '"Handed to the Harness"' in handler,
            "cause_counts_the_carry": "you pressed Open in Harness with ${chatCount(stats.messages"
            in handler,
            "kept_state_is_stated": "this transcript is kept" in handler,
            "nothing_has_run_is_stated": "nothing has run" in handler,
            "way_back_to_chat": 'label: "Back to Chat"' in handler,
            "way_back_focuses_the_composer": 'clearPresentation("chatState");'
            in way_back
            and "input.focus();" in way_back,
            "way_back_is_its_own_action": bool(way_back),
            "mark_is_set": "chatHandoffMark = { messages: stats.messages, calls: stats.calls };"
            in handler,
        },
        "transcript": {
            "clear_resets_the_mark": "chatHandoffMark = null;" in clear,
            "undo_resets_the_mark": clear.count("chatHandoffMark = null;") >= 2,
        },
        "styles": {
            "sentence": ".chat-handoff-hint {" in html,
            "tones": '.chat-handoff-hint[data-reason="ready"]' in html
            and '.chat-handoff-hint[data-reason="handed"]' in html
            and '.chat-handoff-hint[data-reason="failed"]' in html,
            "button": ".chat-handoff-btn {" in html,
            "narrow_safe": "text-overflow: ellipsis" in html,
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    for flag, description in (
        (
            "is_a_button",
            "the handoff is not a button, so it is not reachable by keyboard like one",
        ),
        (
            "type_is_button",
            "the handoff button has no explicit type, so it can submit a form",
        ),
        ("starts_disabled", "the handoff starts enabled with an empty transcript"),
        ("described_by_the_sentence", "the button does not point at its explanation"),
        (
            "has_a_title",
            "the button has no title, so the short label is all a hover gets",
        ),
        ("handler_wired", "the button is not wired to the handoff"),
        (
            "sentence_is_a_live_region",
            "the sentence beside the button is not announced when it changes",
        ),
    ):
        if not frontend["control"].get(flag):
            fail("control", description)

    for flag, description in (
        (
            "no_network",
            "the handoff can reach the network, so the conversation leaves the machine before a human decided anything",
        ),
        ("no_run", "the handoff can start a Harness run"),
        ("no_clear", "the handoff can empty the transcript"),
        ("failure_is_presented", "a handoff that cannot happen is silent"),
        (
            "failure_keeps_the_transcript",
            "a failed handoff does not say whether anything was lost",
        ),
        ("failure_offers_a_retry", "a failed handoff offers no way to try again"),
        (
            "failure_forgets_the_carry",
            "a failed handoff still claims the conversation is in a composer",
        ),
        (
            "every_exit_is_visible",
            "the handoff returns without telling the reader what happened",
        ),
    ):
        if not frontend["safety"].get(flag):
            fail("safety", description)

    for flag, description in (
        ("fills_the_harness_composer", "the brief is not put in the Harness composer"),
        ("focuses_it", "the cursor is not put where the work is"),
        (
            "switches_to_the_harness",
            "the handoff does not take the reader to what it filled",
        ),
        (
            "returns_what_it_carried",
            "the handoff returns nothing, so nothing can be checked",
        ),
        ("no_task_no_handoff", "a conversation with no task is carried anyway"),
    ):
        if not frontend["carry"].get(flag):
            fail("carry", description)

    for flag, description in (
        (
            "task_is_the_last_user_message",
            "the brief is not the task the reader last asked",
        ),
        ("counts_messages_and_replies", "the brief does not count what it carries"),
        (
            "counts_calls_and_failures",
            "the brief does not count the tool calls and their failures",
        ),
        (
            "uses_the_recorded_context",
            "the brief does not use the context recorded on the turn",
        ),
        (
            "prints_the_context_line",
            "the brief does not carry the model and directory the reply was sent with",
        ),
        ("says_nothing_ran", "the brief does not say that nothing has run"),
        ("statuses_are_words", "the brief reports a tool call without a status word"),
        (
            "says_step_and_time",
            "the brief reports a tool call without its step or its time",
        ),
        ("bounds_the_calls", "the brief lists an unbounded number of tool calls"),
        (
            "admits_the_bound",
            "the brief lists fewer calls than the conversation has and does not say so",
        ),
        ("clips_the_outputs", "the brief copies unbounded tool output"),
        ("admits_the_clip", "the brief clips the output without saying it did"),
        (
            "admits_the_server_cap",
            "the brief clips the output without naming the server's own cap",
        ),
        ("points_at_the_transcript", "the brief does not say where the rest of it is"),
    ):
        if not frontend["brief"].get(flag):
            fail("brief", description)

    for flag, description in (
        (
            "one_predicate",
            "the button's state and its sentence are not the same answer",
        ),
        (
            "button_reads_it",
            "the button can be enabled while the sentence says it cannot",
        ),
        ("every_reason_declared", "a handoff reason is declared nowhere"),
        (
            "reason_counts_what_it_carries",
            "the sentence does not say what it will carry",
        ),
        (
            "waits_while_streaming",
            "the handoff can be pressed into a half-written reply",
        ),
        (
            "remembers_what_was_handed",
            "the pane forgets that it already handed this conversation over",
        ),
        ("written_only_on_change", "the live region is rewritten on every render"),
        (
            "one_hook_from_the_composer",
            "nothing updates the handoff as the conversation changes",
        ),
    ):
        if not frontend["sentence"].get(flag):
            fail("sentence", description)

    for flag, description in (
        (
            "is_presented_not_written",
            "the handoff leaves no receipt the presentation layer owns",
        ),
        ("cause_counts_the_carry", "the receipt does not say what was carried"),
        ("kept_state_is_stated", "the receipt does not say the transcript is kept"),
        ("nothing_has_run_is_stated", "the receipt does not say that nothing has run"),
        ("way_back_to_chat", "the receipt offers no way back to Chat"),
        (
            "way_back_is_its_own_action",
            "the receipt has no Back to Chat action of its own",
        ),
        (
            "way_back_focuses_the_composer",
            "the way back does not return the cursor to the composer",
        ),
        ("mark_is_set", "the pane does not remember what it handed over"),
    ):
        if not frontend["receipt"].get(flag):
            fail("receipt", description)

    for flag, description in (
        (
            "clear_resets_the_mark",
            "Clear keeps the handoff mark, so an empty pane claims it was handed over",
        ),
        ("undo_resets_the_mark", "Undo restores the turns but keeps the handoff mark"),
    ):
        if not frontend["transcript"].get(flag):
            fail("transcript", description)

    for flag, description in (
        ("sentence", "the sentence beside the handoff has no rule"),
        ("tones", "a handoff reason has no tone"),
        ("button", "the handoff button has no rule"),
        ("narrow_safe", "the sentence has no way to fit a narrow pane"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    # The Harness is the admission surface: the handoff must not have grown a route of
    # its own while the story was being written.
    handoff_routes = sorted(
        set(
            re.findall(
                r'"(/api/harness/[^"]*)"', function_body(html, "handOffChatToHarness")
            )
        )
    )
    if handoff_routes:
        fail(
            "new-route",
            f"the handoff names {handoff_routes}, which is not a handoff any more",
        )
    if "def api_chat_handoff" in server or "/api/chat/handoff" in server:
        fail(
            "new-route",
            "a server-side handoff route exists; the handoff is a local carry, not a request",
        )

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-06",
        "statement": (
            "Chat hands a conversation to the Harness explicitly: one button that carries the last task, the "
            "conversation's counts, the context the reply was sent with and every tool call with its status, step, "
            "time and clipped output into the Harness composer - where the workspace, the policy and the approval "
            "surface live - and then stops, without a request, without a run and without clearing anything; the pane "
            "leaves a receipt with the counts and a way back to Chat, and a handoff that cannot happen says so and "
            "keeps the transcript."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py", "routes": []},
        "reasons": list(REASONS),
        "brief": {
            "carries": [
                "the last user task",
                "message and reply counts",
                "tool-call and failure counts",
                "the recorded context line",
                "every tool call with its status, step, time and 200-character output",
                "a pointer to the transcript",
            ],
            "bounds": {
                "tool_calls": 8,
                "output_characters": 200,
                "server_output_cap": 500,
            },
        },
        "frontend": frontend,
        "totals": {
            "reasons": len(REASONS),
            "checks": sum(
                len(frontend[key])
                for key in (
                    "control",
                    "safety",
                    "carry",
                    "brief",
                    "sentence",
                    "receipt",
                    "transcript",
                )
            ),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the handoff is a local carry. It reads the turns the pane already holds and writes them into another "
            "pane's composer; it does not persist a draft, so a reload loses the brief - the transcript in Chat is "
            "the durable copy, and the receipt says so",
            "the Harness decides everything that matters after the carry: workspace, workflow mode, execution policy "
            "and approvals. The handoff cannot set those, and the brief says nothing has run",
            "the brief clips tool output at 200 characters although the server sends up to 500, so the pane says the "
            "rest is in the transcript rather than pretending it carried the whole thing",
            "the walk writes a conversation for the carry cases because this host has no model runtime; the harness "
            "composer it fills is the real one, and the walk proves the carry posts nothing",
        ],
    }


def render_doc(payload: dict) -> str:
    bounds = payload["brief"]["bounds"]
    lines = [
        "# Chat to Harness: an explicit handoff, and a way back",
        "",
        "Generated by `python3 scripts/uiux_chat_handoff_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## Why it is a handoff and not a button that runs",
        "",
        "The Harness owns the project directory, the workflow mode, the execution policy and the approval surface. A run",
        "started from Chat would be a run with all four chosen by a pane that shows none of them. So the crossing carries",
        "a brief into the Harness composer and stops there: no request, no run, and no clearing of the transcript. The",
        "inventory fails if `handOffChatToHarness()` grows a `fetch`, an `EventSource`, a run call, or a way to empty",
        "`chatHistory`.",
        "",
        "## What the brief carries",
        "",
    ]
    for item in payload["brief"]["carries"]:
        lines.append(f"- {item}")
    lines += [
        "",
        f"It is bounded on purpose: at most {bounds['tool_calls']} tool calls, each output clipped to",
        f"{bounds['output_characters']} characters, and both bounds are stated in the brief itself — the server's own cap",
        f"is {bounds['server_output_cap']} characters, and the brief says the transcript holds what was sent.",
        "",
        "## One sentence, one button",
        "",
        "`chatHandoffReason()` is the only predicate, exactly like `chatSendReason()`: the button's `disabled` state and",
        "the sentence beside it are the same answer, and the sentence is a polite live region written only when it",
        "changes. The reasons are:",
        "",
        "| Reason | When | What the reader is told |",
        "|---|---|---|",
        "| `nothing` | no message yet | send a message first; nothing to carry |",
        "| `streaming` | a reply is still arriving | the handoff waits, so the brief is not half a conversation |",
        "| `ready` | there is a task | the counts that will be carried, and that nothing runs until Run |",
        "| `handed` | the transcript still matches the last handoff | this conversation is already in the composer |",
        "",
        "`chatHandoffMark` is what makes the fourth honest: Clear and Undo both reset it, so an emptied pane cannot",
        "claim it was handed over, and a longer conversation is offered again.",
        "",
        "## The receipt",
        "",
        "The handoff presents a `recovery` into the Chat pane's own container: the counts it carried, the sentence that",
        "nothing has run, that this transcript is kept, and **Back to Chat** / **Open Harness**. Back to Chat dismisses",
        "the receipt and returns the cursor to the composer, so the return trip is one keystroke, not a hunt.",
        "",
        "A handoff that cannot happen is the case this surface exists for: if the Harness composer is not on the page,",
        "the pane presents an `error` with its cause, says the conversation and its tool calls are untouched, and offers",
        "**Retry** and **Dismiss**. Nothing is cleared, and the retry runs the same function.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "`node scripts/chat_handoff_walk.mjs` presses the button in a browser against a real Harness composer, records",
        "every request the page makes to prove the carry posts nothing, reads the brief line by line, follows Back to",
        "Chat with the keyboard, removes the composer to reach the failure, and photographs the states in",
        "[`media/chat/`](media/chat/README.md).",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`.",
        "2. `python3 scripts/uiux_chat_handoff_inventory.py`.",
        "3. `node scripts/chat_handoff_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
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
            print("error: the Chat handoff inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_chat_handoff_inventory.py")
            return 1
        if payload["findings"]:
            print("error: Chat's handoff to the Harness disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"chat handoff OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['reasons']} reasons, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['checks']} checks, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
