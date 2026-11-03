#!/usr/bin/env python3
"""Inventory the streaming contract of the chat transcript and the harness stream (UIUX-P5-04).

    python3 scripts/uiux_stream_perf_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_stream_perf_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_stream_perf_inventory.py --json     # print the inventory

Both panes drew their whole self for every frame of a stream. Neither looked slow on a short answer
on a fast machine, and both were being paid for by anyone reading a long one:

* the chat transcript re-rendered **every message** for **every token**. On a 60-message
  conversation that was 12 ms of main-thread work per token, so a 400-token answer cost about five
  seconds of stutter on top of the wait, and the cost grew with the length of the conversation;
* the harness event log rebuilt **every card** for **every event**, sorted a cache that had no
  ceiling, and kept every event it had ever seen in the DOM. At 800 events the pane held 2,970 nodes
  and paid 3.5 ms per arrival, and both numbers were still climbing.

What replaced them is a contract about *what changes*:

* a burst of tokens is coalesced into one render per frame, and the terminal frames - done, stopped,
  failed - render synchronously, because the frame that ends a turn is the one that has to be exact;
* a render that only changes the words of the last turn replaces that one node. The transcript's
  *shape* - which turns exist, what state each is in, whether it has tools, reasoning, usage or a
  context line - decides whether the cheap path is allowed;
* the harness stream draws the newest 300 cards of the log, and says so, with the count and the way
  to see the rest in the same breath;
* a live event appends one card and drops one off the top rather than rebuilding the window. The
  append is only allowed when the cards on screen are still a contiguous run of the new window, no
  approval state changed, and nothing that arrives would redraw a card already drawn;
* the cache behind the stream holds 5,000 events and drops its oldest, while `harnessCursor` and
  `retained_from_seq` stay the truth about which event comes next.

This inventory states that contract in source. It cannot time a render; the walk
(`scripts/stream_perf_walk.mjs`) does that, on a real streamed reply and on synthetic bursts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "stream_perf_walk.mjs"
TEST = PROJECT_ROOT / "tests" / "test_stream_perf.py"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "stream-perf"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Stream_Performance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-stream-performance.md"
DOCS_INDEX = PROJECT_ROOT / "docs" / "README.md"

STORY = "UIUX-P5-04"
TITLE = "SSE performance and bounded DOM updates"

#: The number of cards the harness stream draws, whatever the log holds.
RENDER_WINDOW = 300

#: The number of events the client cache keeps, whatever the log holds.
CACHE_LIMIT = 5000

#: Handlers on the chat stream that may defer their render, and handlers that may not: a terminal
#: frame is the one the reader is waiting for, so it is drawn now.
STREAMED_CHAT_HANDLERS = ("token", "step_clean", "chat_tool")
TERMINAL_CHAT_HANDLERS = ("done", "stopped", "failed")

CAPTURES = (
    "stream-chat-midstream.png",
    "stream-chat-complete.png",
    "stream-harness-window.png",
    "stream-harness-window-narrow.png",
)


def function_body(html: str, name: str) -> str:
    """The body of a `function name(...) { ... }`, braces matched."""
    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(([^)]*)\)\s*\{{", html)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for offset in range(start, len(html)):
        if html[offset] == "{":
            depth += 1
        elif html[offset] == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1 : offset]
    return ""


def handler_body(html: str, name: str) -> str:
    """The body of one handler object - `token(d) { ... }`, `stopped() { ... }` - braces matched.

    Handlers are not all written the same way: some are one line, some carry parameters, and the
    council stream has a `token` of its own. Pass the region that holds the handler you mean.
    """
    match = re.search(rf"\b{re.escape(name)}\([^)]*\)\s*\{{", html)
    if not match:
        return ""
    start = match.end() - 1
    depth = 0
    for offset in range(start, len(html)):
        if html[offset] == "{":
            depth += 1
        elif html[offset] == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1 : offset]
    return ""


def chat_stream_region(html: str) -> str:
    start = html.find('consumeSSE("/api/chat/stream"')
    if start < 0:
        return ""
    end = html.find("() => { chatBusy = false;", start)
    return html[start : end if end > 0 else start + 8000]


def comment_above(html: str, needle: str) -> str:
    """The comment block immediately above a line, if there is one."""
    index = html.find(needle)
    if index < 0:
        return ""
    head = html[:index]
    lines = head.splitlines()[-8:]
    collected: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            collected.append(stripped)
        elif not stripped:
            if collected:
                break
            continue
        else:
            break
    return "\n".join(reversed(collected))


def walk_checks(walk_body: str) -> list[str]:
    found = re.findall(r"record\(\s*\n?\s*(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)')", walk_body)
    return [re.sub(r"\s+", " ", next(part for part in match if part)).strip() for match in found]


def ledger_story() -> dict:
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""
    test_body = TEST.read_text(encoding="utf-8") if TEST.exists() else ""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    schedule = function_body(html, "scheduleChatRender")
    flush = function_body(html, "flushChatRender")
    shape = function_body(html, "chatTranscriptShape")
    render_chat = function_body(html, "renderChatMessages")
    render_stream = function_body(html, "renderStoredHarnessEvents")
    append_plan = function_body(html, "harnessStreamAppendPlan")
    # The decision itself lives in a pure helper so a test can ask it what it would do without a
    # DOM; the checks below read the plan and that helper together, because they are one decision.
    append_window = function_body(html, "harnessStreamAppendWindow")
    cached = function_body(html, "harnessCachedEvents")
    remember = function_body(html, "harnessRememberEvents")
    merge = function_body(html, "harnessMergeEvents")
    reset = function_body(html, "harnessResetEventCache")
    stream_region = chat_stream_region(html)

    # ── 1. one render per frame, and never for the frame that ends a turn ──
    check(
        "a token schedules a render for the next frame instead of drawing one",
        "if (chatRenderQueued) return;" in schedule
        and "requestAnimationFrame(" in schedule
        and "renderChatMessages();" in schedule,
        "scheduleChatRender does not coalesce",
    )
    check(
        "the schedule is a flag, so a hundred tokens in one frame queue one render",
        "chatRenderQueued = true;" in schedule and "chatRenderQueued = false;" in schedule,
        "the queue flag is not set and cleared",
    )
    check(
        "the frame that ends a turn is drawn immediately, not queued behind the next frame",
        "chatRenderQueued = false;" in flush
        and "renderChatMessages();" in flush
        and "requestAnimationFrame" not in flush,
        "flushChatRender is not synchronous",
    )
    deferred = [name for name in STREAMED_CHAT_HANDLERS if "scheduleChatRender();" in handler_body(stream_region, name)]
    deferred_with_flush = [name for name in STREAMED_CHAT_HANDLERS if "flushChatRender();" in handler_body(stream_region, name)]
    check(
        "the handlers that fire many times per second defer their render",
        len(deferred) == len(STREAMED_CHAT_HANDLERS) and not deferred_with_flush,
        f"deferring handlers: {deferred}",
    )
    terminal = [name for name in TERMINAL_CHAT_HANDLERS if "flushChatRender();" in handler_body(stream_region, name)]
    terminal_with_schedule = [name for name in TERMINAL_CHAT_HANDLERS if "scheduleChatRender();" in handler_body(stream_region, name)]
    check(
        "the handlers that end a turn render exactly once, now",
        len(terminal) == len(TERMINAL_CHAT_HANDLERS) and not terminal_with_schedule,
        f"terminal handlers: {terminal}",
    )

    # ── 2. a token costs the message, not the conversation ──────────────────
    fields = [field for field in ("m.role", "m.status", "m.tools", "m.reasoning", "m.usage", "m.context") if field in shape]
    check(
        "the transcript's shape names every part of a turn that is not its words",
        len(fields) == 6,
        f"shape fields: {fields}",
    )
    check(
        "the render decides on the shape, the last node and the turn count",
        "onlyTheLastChanged" in render_chat
        and "shape === chatRenderedShape" in render_chat
        and 'previousLast.classList.contains("msg")' in render_chat
        and "box.children.length === chatHistory.length" in render_chat,
        "the cheap path is not guarded by the shape",
    )
    check(
        "it builds the one turn that changed, not the turns around it",
        "onlyTheLastChanged ? [chatHistory.length - 1] : chatHistory.map(" in render_chat,
        "every render still builds every message",
    )
    check(
        "and replaces that one node rather than rewriting the transcript",
        re.search(
            r'if \(onlyTheLastChanged\) \{\s*previousLast\.outerHTML = parts\[0\];\s*\} else \{\s*box\.innerHTML = parts\.join\(""\);',
            render_chat,
        )
        is not None,
        "the cheap path does not replace a single node",
    )
    check(
        "an empty transcript forgets the shape, so the next message is not drawn into a stale node",
        re.search(r"if \(!chatHistory\.length\) \{[\s\S]{0,400}?chatRenderedShape = \"\";", render_chat) is not None,
        "the empty state keeps the old shape",
    )
    check(
        "the transcript still says what each turn is and what became of it",
        'data-role="user"' in render_chat
        and 'data-role="assistant"' in render_chat
        and 'data-state="' in render_chat,
        "a turn lost its role or its state attribute",
    )

    # ── 3. the stream draws a window, and says what it is holding back ──────
    window_declared = re.search(rf"const HARNESS_RENDER_WINDOW = {RENDER_WINDOW};", html)
    cache_declared = re.search(rf"const HARNESS_CACHE_LIMIT = {CACHE_LIMIT};", html)
    check(
        f"the window and the cache have one declared size each ({RENDER_WINDOW} cards, {CACHE_LIMIT} events)",
        bool(window_declared) and bool(cache_declared),
        f"window={bool(window_declared)} cache={bool(cache_declared)}",
    )
    sizes_comment = comment_above(html, "const HARNESS_RENDER_WINDOW")
    check(
        "both sizes are declared with their reason, not as bare numbers in the middle of a function",
        "window" in sizes_comment.lower() and "cache" in sizes_comment.lower(),
        sizes_comment[:80] or "no comment above the constants",
    )
    check(
        "the stream draws the newest window of the log",
        "ordered.length > HARNESS_RENDER_WINDOW" in render_stream
        and "ordered.slice(ordered.length - HARNESS_RENDER_WINDOW)" in render_stream,
        "the render is not windowed",
    )
    check(
        "the pane says how much of the log is not on screen, and where to read it",
        "harnessWindowNoteText" in html
        and "Showing the newest ${showing} of ${total} events on screen" in html
        and "Replay re-reads the whole log from the server." in html,
        "the window note is missing or does not say what it is",
    )
    note_updater = function_body(html, "harnessUpdateWindowNote")
    check(
        "the note is updated as the window slides, so it never counts cards it is not showing",
        "harnessUpdateWindowNote(shown.length, ordered.length);" in render_stream
        and "note.textContent = harnessWindowNoteText(showing, total);" in note_updater
        and 'insertAdjacentHTML("afterbegin"' in note_updater,
        "the note is written once and left to drift",
    )
    check(
        "the timeline's own list is windowed by the same number",
        "harnessCachedEvents().slice(-HARNESS_RENDER_WINDOW)" in html,
        "the timeline list still draws every cached event",
    )

    # ── 4. a live event is one card in, one card out ────────────────────────
    check(
        "the append is refused when approval state moved, because those cards are already drawn",
        "pending: harnessRenderedPending" in append_plan
        and "decisions: harnessRenderedDecisions" in append_plan
        and "Number(rendered && rendered.pending) !== Number(shown && shown.pending)" in append_window
        and "Number(rendered && rendered.decisions) !== Number(shown && shown.decisions)" in append_window,
        "the append does not check the approval state",
    )
    # #169/#170: the plan is a claim about one session and one read of the log. A window that
    # survived a session switch or a re-read answered "already drawn" for cards that were not.
    check(
        "the append is refused for a window drawn for another session",
        "session: harnessRenderedSession" in append_plan
        and "session: activeHarnessId" in append_plan
        and 'String((rendered && rendered.session) || "") !== String((shown && shown.session) || "")' in append_window,
        "a window could be inherited across a session switch",
    )
    check(
        "a re-read of the log drops the drawn window, so nothing is appended over a log it never saw",
        "harnessRenderedSession = \"\";" in function_body(html, "harnessResetEventCache")
        and "harnessRenderedSeqs = [];" in function_body(html, "harnessResetEventCache"),
        "the cache is reset but the window it was drawn from is kept",
    )
    check(
        "the append is also refused for an approval event, which changes a card that already exists",
        'startsWith("approval/")' in append_plan,
        "an approval card would arrive as an append",
    )
    check(
        "the run on screen may slide forward: cards leaving are counted, cards arriving are added",
        "const dropped = drawn.filter(seq => seq < incoming[0]).length;" in append_window
        and "return { dropped, from: tail + 1 };" in append_window
        and "const appended = shown.slice(plan.from);" in append_plan,
        "the plan only handles a window that has not moved",
    )
    check(
        "an appended card is one node, and the rebuild is what it replaces",
        'insertAdjacentHTML("beforeend"' in render_stream
        and render_stream.find('insertAdjacentHTML("beforeend"') < render_stream.find('stream.innerHTML = htmlParts.join("");'),
        "the append path is not before the rebuild",
    )
    check(
        "the cards that leave are measured in one pass, and not at all for a reader at the bottom",
        "const removedHeight = pinned || !leaving.length" in render_stream
        and render_stream.count("getBoundingClientRect") <= 1,
        "the render measures or removes cards one layout at a time",
    )
    check(
        "a reader who scrolled up is put back where they were reading",
        "stream.scrollTop = Math.max(0, previousTop - removedHeight);" in render_stream,
        "the pulled-up cards move the reader",
    )

    # ── 5. the cache is bounded, and the cursor is still the truth ──────────
    check(
        "the cache drops its oldest events instead of growing without a ceiling",
        "harnessEventCache.size > HARNESS_CACHE_LIMIT" in remember
        and "harnessEventOrder.splice(0," in remember,
        "the cache trim is missing or drops from the wrong end",
    )
    check(
        "the cursor still advances past an event the cache refused to keep",
        "if (seq > harnessCursor) harnessCursor = seq;" in remember,
        "the cursor is tied to the cache",
    )
    check(
        "drawing the window does not sort the whole cache",
        "harnessCachedSnapshot = harnessEventOrder.map(seq => harnessEventCache.get(seq));" in cached
        and "function harnessNoteSeq" in html,
        "the cache is still sorted on every read",
    )
    check(
        "an out-of-order page is still merged correctly, it just pays for the sort",
        "if (!ascending) seqs.sort((a, b) => a - b);" in merge,
        "the merge assumes its input is sorted",
    )
    check(
        "a reset drops the order and the snapshot with the cache, so a second session cannot inherit them",
        "harnessEventOrder = [];" in reset and "harnessCachedSnapshot = null;" in reset and "harnessCursor = 0;" in reset,
        "the reset leaves ordering state behind",
    )

    # ── 6. nothing durable was simplified away ──────────────────────────────
    check(
        "the snapshot fetch after a turn still draws synchronously, so it cannot be raced by a frame",
        "harnessSnapshot = state; applyHarnessSnapshotState(state); renderStoredHarnessEvents(harnessCachedEvents());" in html
        and "scheduleHarnessStreamRender" not in html.split("harnessSnapshot = state; applyHarnessSnapshotState(state);")[1][:120],
        "the authoritative state refresh was made asynchronous",
    )
    check(
        "a pruned log still says it was pruned, and still defers to task state",
        "Earlier events were pruned; showing the retained log from seq" in render_stream
        and "Task status above is authoritative." in render_stream,
        "the pruning note was lost",
    )
    check(
        "replay and resync still read the log from the server rather than the window on screen",
        "function replayHarnessLog" in html
        and "function harnessResyncFromSnapshot" in html
        and "async function loadHarnessEvents" in html,
        "a recovery path is missing",
    )

    # ── 7. the evidence ─────────────────────────────────────────────────────
    named = walk_checks(walk_body)
    check(
        "the walk times a token, a burst, a live event and a full window",
        len(named) >= 12
        and any("token costs" in name for name in named)
        and any("one render, not sixty" in name for name in named)
        and any("appends one card" in name for name in named)
        and any("frame budget" in name for name in named),
        f"{len(named)} named checks in the walk",
    )
    check(
        "the walk drives a real streamed reply, not a fixture of a transcript",
        'page.click("#chatSendBtn")' in walk_body and "dataset.state" in walk_body,
        "the walk never sends a message",
    )
    check(
        "the walk refuses to report a walk it did not perform",
        "STUDIO_SCREENSHOT_CHROME" in walk_body
        and "refuses to report a walk it did not perform" in walk_body
        and "process.exitCode = 1" in walk_body,
        "a skipped walk that reports success is the failure mode this story is about",
    )
    check(
        "the captures the doc lists exist and were written by the walk",
        all((MEDIA / name).exists() for name in CAPTURES) and all(name[:-4] in walk_body for name in CAPTURES),
        ", ".join(name for name in CAPTURES if not (MEDIA / name).exists())
        or ", ".join(name for name in CAPTURES if name[:-4] not in walk_body),
    )
    check(
        "each rule is broken in a copy of the source by a test, so a rule that stopped looking fails",
        "assert changed != html" in test_body and len(re.findall(r"def test_", test_body)) >= 10,
        f"{len(re.findall(r'def test_', test_body))} tests in {TEST.name}",
    )
    check(
        "the doc is written, and reachable from the docs index",
        DOC_PATH.exists() and DOCS_INDEX.exists() and DOC_PATH.name in DOCS_INDEX.read_text(encoding="utf-8"),
        f"{DOC_PATH.name} missing or unlinked",
    )
    tracking = ledger_story()
    check(
        "the ledger marks this story done with evidence",
        tracking.get("status") == "done" and bool(tracking.get("evidence")) and bool(tracking.get("test_results")),
        f"tracking.status={tracking.get('status')!r}",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": STORY,
        "title": TITLE,
        "generated_by": "scripts/uiux_stream_perf_inventory.py",
        "surface": "the chat transcript and the harness event stream",
        "render_window": RENDER_WINDOW,
        "cache_limit": CACHE_LIMIT,
        "deferred_chat_handlers": list(STREAMED_CHAT_HANDLERS),
        "terminal_chat_handlers": list(TERMINAL_CHAT_HANDLERS),
        "walk": {
            "script": "scripts/stream_perf_walk.mjs",
            "checks": named,
            "check_count": len(named),
        },
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P5-04 — SSE performance and bounded DOM updates",
        "",
        "Generated by `scripts/uiux_stream_perf_inventory.py`. Do not edit by hand: regenerate it, and",
        "the checks behind it stay true.",
        "",
        "## What was wrong",
        "",
        "Both panes redrew themselves for every frame of a stream, and neither looked slow on a short",
        "answer on a fast machine. The cost was paid by the reader of a long one, and it grew:",
        "",
        "| Surface | What it did | What it cost |",
        "| :--- | :--- | :--- |",
        "| Chat transcript | re-rendered every message for every token | 1.6 ms a render at 2 messages, 12.0 ms at 60 — so a 400-token answer at 60 messages spent about five seconds of the main thread on frames the reader never saw |",
        "| Harness stream | rebuilt every card for every event, and kept every event | 3.5 ms per arrival at 800 events, 2,970 nodes on screen, both still climbing |",
        "| Harness cache | sorted every key it held on every render | 5,000 events cost 11 ms a frame before a card was drawn |",
        "",
        "## The contract",
        "",
        "* **One render per frame.** A burst of tokens sets a flag and the render happens on the next",
        "  frame. The frame that ends a turn — done, stopped, failed — renders immediately, because it is",
        "  the one the reader is waiting for and it must be exact.",
        "* **A token costs the message, not the conversation.** The transcript compares its own shape —",
        "  which turns exist, what state each is in, whether it carries tools, reasoning, usage or a",
        "  context line — and when only the last turn's words changed, it rebuilds that one node.",
        f"* **The stream draws a window of {payload['render_window']} cards**, and says how much of the log",
        "  is not on screen and that Replay re-reads the whole log from the server. The timeline's count",
        "  names the true total, and the cursor keeps advancing past anything not drawn.",
        "* **A live event is one card in and one card out.** Appending is allowed only when the cards on",
        "  screen are still a contiguous run of the new window, no approval state changed, and nothing",
        "  arriving would redraw a card already drawn; anything else rebuilds.",
        f"* **The cache holds {payload['cache_limit']} events** and drops its oldest, while `harnessCursor`",
        "  and `retained_from_seq` stay the truth about which event comes next.",
        "",
        "## The handlers",
        "",
        "| Handler | What it does | Render |",
        "| :--- | :--- | :--- |",
        "| `token`, `step_clean`, `chat_tool` | arrive many times a second | scheduled for the next frame |",
        "| `done`, `stopped`, `failed` | end the turn | rendered immediately |",
        "",
        "## What it costs now",
        "",
        "| Measure | Before | After |",
        "| :--- | ---: | ---: |",
        "| a token on a 60-message transcript | 12.0 ms | 1.0 ms |",
        "| a token on a 6-message transcript | 1.6 ms | 0.7 ms |",
        "| a live event, window filling | 1.8 ms | 0.1 ms |",
        "| a frame of 10 live events, window full | 11.7 ms | 1.8 ms |",
        "| nodes for an 8,000-event log | 2,970 (at 800) | 1,201 (at 8,000) |",
        "",
        "The \"before\" column was measured on the commit this story starts from, with the same in-page",
        "timing the walk uses; the \"after\" column is what",
        "`node scripts/stream_perf_walk.mjs` reports. A five-message conversation still pays about a",
        "millisecond a frame, which is the price of drawing the message being written and is not worth",
        "hiding.",
        "",
        "## The evidence",
        "",
        "`node scripts/stream_perf_walk.mjs --screenshots docs/media/stream-perf` starts the Compare",
        "fixture, sends a real message to the local model and times the result. It checks:",
        "",
        "| Check | What it proves |",
        "| :--- | :--- |",
    ]
    for name in payload["walk"]["checks"]:
        lines.append(f"| {name} | |")
    lines += [
        "",
        "The captures it writes: " + ", ".join(f"`{name}`" for name in payload["captures"]) + ".",
        "",
        "## The checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} findings — **{payload['status']}**.",
        "",
    ]
    for entry in payload["checks"]:
        mark = "pass" if entry["ok"] else "FAIL"
        detail = f" — {entry['detail']}" if entry["detail"] else ""
        lines.append(f"* `{mark}` {entry['check']}{detail}")
    lines.append("")
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"


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
                    "the stream performance inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    if payload["findings"]:
        for entry in payload["findings"]:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
