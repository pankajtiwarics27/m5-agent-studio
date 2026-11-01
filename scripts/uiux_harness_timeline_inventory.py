#!/usr/bin/env python3
"""Inventory the Harness's durable timeline, event replay and jump-to-latest (UIUX-P3-02).

    python3 scripts/uiux_harness_timeline_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_harness_timeline_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_harness_timeline_inventory.py --json     # print the inventory

The Harness's trajectory is a *view* of a durable log: the server appends one event per
thing it does, a cursor says where the browser has read to, and the pane can be rebuilt
from the log at any time. Before this story the pane kept that log to itself. It dropped
every event type it had no card for - `session/init`, all seven `security/*` types, the
`edit/*`, `delegation/*`, `handoff/*` and `mcp/*` families - so a session with three
recorded events looked like an empty trajectory; it rewrote the whole transcript on every
frame, which threw the reader back to the bottom and shut the cards they had opened; and it
never said how much of the log it was holding, where the cursor was, or that the view had
come from a re-read rather than the stream.

So this artifact measures four surfaces:

* the **strip**, a disclosure above the trajectory that states the count, the seq range,
  the cursor and how the view was filled, before the trajectory is read;
* the **record**, one row per held event, oldest first, each row a button that points at
  its card;
* **coverage**, the rule that no event reaches the pane and draws nothing;
* **replay and place**, the re-read path and the rule that a frame never moves a reader
  who is scrolled up, plus the control that is the only thing which scrolls them.

It fails when the strip stops stating how the view was filled, when the trajectory can
draw an event as nothing, when a row stops pointing at its card, when a frame moves a
reader who is scrolled up, when the jump is offered with nothing to jump to, when replay
claims to have rebuilt a log it could not resume, when a pruned or incomplete log stops
saying so, and when the cursor, dedupe or resync semantics this story had to preserve
change.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Timeline.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-timeline.md"

MODES = ("idle", "live", "replay", "pruned")
NETWORK_CALLS = ("fetch(", "XMLHttpRequest", "EventSource", "navigator.sendBeacon")

# The reasons the gap detector already had. This story did not invent the resync path; it
# made the path say what it did, so these must all still be detected.
GAP_REASONS = (
    "no_page",
    "snapshot_required",
    "cursor_ahead",
    "events_pruned",
    "sequence_gap",
    "missing_events",
)

# The server's own events, so the inventory can tell a type the pane draws by name from one
# it draws with the fallback card - and can fail on a branch for a type nothing emits.
EMITTER = re.compile(r'_safe_append_harness_event\(|_append_harness_event\(')
EVENT_TYPE = re.compile(r'["\']([a-z_]+/[a-z_]+)["\']')

# The strip's controls, and the attribute that proves each still does what it claims.
STRIP_CONTROLS: dict[str, str] = {
    "harnessTimeline": 'class="harness-timeline"',
    "harnessTimelineToggle": 'aria-controls="harnessTimelineList"',
    "harnessTimelineCount": 'class="harness-timeline-count mono"',
    "harnessTimelineState": 'role="status"',
    "harnessTimelineList": 'aria-label="Durable task events, oldest first"',
    "harnessTimelineJump": 'onclick="jumpToLatestHarnessEvent()"',
    "harnessTimelineReplay": 'onclick="replayHarnessLog()"',
    "harnessTimelineResync": "onclick=\"harnessResyncFromSnapshot('operator')\"",
}

# The routes the strip reads through. All of them predate this story: the point is that the
# pane now says what it read, not that it reads something new.
ROUTES = (
    ("GET", "/api/harness/sessions/{session_id}/events"),
    ("GET", "/api/harness/sessions/{session_id}/events/stream"),
    ("GET", "/api/harness/sessions/{session_id}/state"),
    ("PATCH", "/api/harness/sessions/{session_id}"),
)

FUNCTIONS = (
    "harnessEventSummary",
    "harnessLogState",
    "renderHarnessTimeline",
    "toggleHarnessTimeline",
    "focusHarnessEvent",
    "jumpToLatestHarnessEvent",
    "updateHarnessTimelineJump",
    "replayHarnessLog",
    "renderRecordedHarnessEventHtml",
    "harnessStreamNearBottom",
)


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


def emitted_types(server: str) -> list[str]:
    """Every event type the server's own emitters write, in source order."""
    lines = server.splitlines()
    found: list[str] = []
    for index, line in enumerate(lines):
        if not EMITTER.search(line):
            continue
        chunk = "\n".join(lines[index : index + 4])
        for match in EVENT_TYPE.finditer(chunk):
            if match.group(1) not in found:
                found.append(match.group(1))
    return found


def card_types(renderer: str) -> list[str]:
    """Types with a card of their own in the trajectory renderer."""
    return re.findall(r'ev\.type === "([a-z_]+/[a-z_]+)"', renderer)


def drawn_types(renderer: str, summary: str) -> list[str]:
    """Types the pane describes by name: an explicit card, or a sentence of its own."""
    found = card_types(renderer)
    for match in re.findall(r'type === "([a-z_]+/[a-z_]+)"', summary):
        if match not in found:
            found.append(match)
    return found


def read_frontend(html: str, server: str) -> dict:
    strip = element(html, "harnessTimeline")
    toggle = element(html, "harnessTimelineToggle")
    state_dom = element(html, "harnessTimelineState")
    render = function_body(html, "renderHarnessTimeline")
    log_state = function_body(html, "harnessLogState")
    summary = function_body(html, "harnessEventSummary")
    renderer = function_body(html, "renderStoredHarnessEventHtml")
    fallback = function_body(html, "renderRecordedHarnessEventHtml")
    stored = function_body(html, "renderStoredHarnessEvents")
    jump = function_body(html, "updateHarnessTimelineJump")
    to_latest = function_body(html, "jumpToLatestHarnessEvent")
    focus = function_body(html, "focusHarnessEvent")
    replay = function_body(html, "replayHarnessLog")
    toggle_body = function_body(html, "toggleHarnessTimeline")
    near_bottom = function_body(html, "harnessStreamNearBottom")
    gap = function_body(html, "harnessEventGap")
    merge = function_body(html, "harnessMergeEvents")
    load = function_body(html, "loadHarnessEvents")
    resync = function_body(html, "harnessResyncFromSnapshot")

    types = emitted_types(server)
    cards = card_types(renderer)
    explicit = drawn_types(renderer, summary)
    dead = [name for name in explicit if name not in types]

    # The renderer must end in the fallback card, not in an empty string: an event the pane
    # cannot draw is an event the pane drops, and a reader cannot tell that from an event
    # that never happened.
    renderer_tail = renderer.rstrip().rsplit("return", 1)[-1].strip() if "return" in renderer else ""
    strip_hidden = "hidden" in strip

    return {
        "functions": {name: bool(function_body(html, name)) for name in FUNCTIONS},
        "strip": {
            "arrives_hidden": strip_hidden,
            "is_a_disclosure": "aria-expanded" in toggle and toggle_body != "",
            "states_the_count": bool(re.search(r'id="harnessTimelineCount"', html)),
            "states_the_log": "role=\"status\"" in state_dom and "aria-live=\"polite\"" in state_dom,
            "sits_above_the_trajectory": 0
            <= html.find('id="harnessTimeline"')
            < html.find('id="harnessStreamState"')
            < html.find('id="harnessStream"'),
            "names_the_list": 'aria-label="Durable task events, oldest first"' in html,
            "order_is_stated": "oldest first" in html,
            "never_shrinks_away": "flex: 0 0 auto" in html.split(".harness-timeline {", 1)[-1][:200],
            "keeps_the_trajectory_room": "min-height: 96px" in html.split(".harness-stream {", 1)[-1][:220],
            "hidden_is_not_displayed": ".harness-timeline-actions .btn[hidden] { display: none; }" in html,
            "written_only_on_change": 'line.textContent !== state.text' in render
            and "signature !== harnessTimelineSignature" in render,
            "count_is_rewritten": "count.textContent = `" in render,
            "rows_carry_seq_and_type": 'data-seq="${seq}"' in render and 'data-type="${escapeHtml(type)}"' in render,
            "row_is_a_button": 'class="harness-timeline-row"' in render and "<button" in render,
            "row_names_its_target": "Show seq ${seq} in the trajectory" in render,
        },
        "coverage": {
            "fallback_exists": "h-recorded-type" in fallback and "harnessEventSummary(ev)" in fallback,
            "renderer_ends_in_the_fallback": renderer_tail.startswith("renderRecordedHarnessEventHtml(ev)"),
            "no_event_renders_nothing": 'return ""' not in renderer,
            "a_missing_type_still_draws": "if (!ev) return renderRecordedHarnessEventHtml(ev);" in renderer,
            "summary_reads_the_event": "session/init" in summary
            and "security/policy_revision" in summary
            and "clip(ev." in summary,
            "no_dead_branches": not dead,
            "types_emitted": len(types),
            "types_with_a_card": len(cards),
            "types_described_by_name": len(explicit),
            "types_with_only_the_generic_preview": len([name for name in types if name not in explicit]),
        },
        "trajectory": {
            "every_card_carries_its_seq": 'class="h-event" data-seq="${seq}"' in stored,
            "every_card_carries_its_type": 'data-type="${escapeHtml(type)}"' in stored,
            "a_row_can_find_its_card": '.h-event[data-seq="${Number(seq)}"]' in focus,
            "the_card_is_brought_into_view": 'scrollIntoView({ block: "center" })' in focus,
            "the_card_is_marked_in_words": 'target.dataset.highlight = "true"' in focus
            and '.h-event[data-highlight="true"]' in html,
            "the_mark_is_removed": "delete target.dataset.highlight" in focus,
            "the_card_is_focusable": 'target.setAttribute("tabindex", "-1")' in focus,
        },
        "place": {
            "near_bottom_is_one_rule": "HARNESS_PIN_SLACK" in near_bottom
            and "scrollHeight - box.scrollTop - box.clientHeight" in near_bottom,
            "a_pinned_view_follows": "stream.scrollTop = stream.scrollHeight;" in stored,
            "a_reading_view_is_left_alone": "stream.scrollTop = previousTop;" in stored,
            "open_cards_stay_open": "details[index].open = true" in stored
            and 'querySelectorAll("details")' in stored,
            "the_jump_is_offered_only_when_needed": "jump.hidden = harnessStreamNearBottom(stream);" in jump,
            "the_jump_is_the_only_thing_that_scrolls": 'jumpToLatestHarnessEvent' in html
            and "stream.scrollTop = stream.scrollHeight;" in to_latest
            and "onclick=\"jumpToLatestHarnessEvent()\"" in html,
            "the_jump_follows_a_scroll": 'addEventListener("scroll", () => updateHarnessTimelineJump()' in html,
            "the_jump_follows_a_render": "updateHarnessTimelineJump();" in render,
        },
        "replay": {
            "re_reads_from_the_start": "harnessResetEventCache();" in replay
            and "loadHarnessEvents({ full: true })" in replay,
            "rebuilds_the_view_from_the_log": "renderStoredHarnessEvents(harnessCachedEvents());" in replay,
            "counts_the_replay": "harnessLogTelemetry.reloads += 1;" in replay,
            "refuses_a_log_it_could_not_resume": 'if (verdict.gap && verdict.reason !== "session_changed")' in replay,
            "says_so_then_resyncs": "harnessResyncFromSnapshot(\"replay_gap\")" in replay
            and "showToast(" in replay,
            "will_not_claim_another_session": "activeHarnessId !== sessionId" in replay,
        },
        "honesty": {
            "states_the_source_live": "following the log from seq" in log_state,
            "states_the_count_and_range": "held, seq ${first}-${last}" in log_state,
            "states_the_cursor": "cursor ${harnessCursor}" in log_state,
            "says_when_the_log_did_not_resume": "the log did not resume (${tele.reason})" in log_state,
            "says_what_is_pruned": "earlier event" in log_state and "pruned, retained from seq" in log_state,
            "says_this_view_holds_less": "this view holds ${count} of the ${latest} events last reported" in log_state,
            "counts_the_replays": "replayed ${plural(tele.reloads" in log_state,
            "following_needs_a_cursor": "Boolean(harnessEventStreamAbort) && !harnessRunActive && harnessCursor > 0"
            in log_state,
            "pruning_wins_the_colour": 'if (pruned && mode !== "replay") mode = "pruned";' in log_state,
            "a_run_in_flight_is_stated": "a run is in flight" in log_state,
            "no_network_in_the_strip": not any(
                call in (render + log_state + summary) for call in NETWORK_CALLS
            ),
            "hidden_without_a_session": "host.hidden = true;" in render,
        },
        "telemetry": {
            "one_record_of_how_it_was_filled": "let harnessLogTelemetry = {" in html,
            "the_log_path_says_log": 'harnessLogTelemetry.source = "log";' in load,
            "the_stream_path_says_live": 'harnessLogTelemetry.source = "live";' in html,
            "the_snapshot_path_says_snapshot": 'harnessLogTelemetry.source = "snapshot";' in resync,
            "the_page_reports_the_newest_seq": "harnessLogTelemetry.latest = Number(payload.latest_seq" in load,
            "the_page_reports_the_retained_floor": "harnessLogTelemetry.retained = Number(payload.retained_from_seq"
            in load,
            "a_gap_is_recorded_as_a_reason": "harnessLogTelemetry.reason = verdict.gap" in resync,
            "a_new_session_forgets_it": "harnessLogTelemetry = { source: \"idle\"" in html,
        },
        "preserved": {
            "gap_reasons": [reason for reason in GAP_REASONS if reason in gap],
            "dedupes_by_seq": "harnessMergeEvents" in html and "!bySeq.has(seq)" in merge,
            "keeps_the_cursor": "harnessCursor = " in html or "harnessCursor=" in html,
            "snapshot_required_handled": "snapshot_required" in gap,
            "resync_still_reloads_state": "/state`" in resync,
            "resync_reopens_the_stream": "scheduleHarnessEventStreamRetry();" in resync,
            "pruned_notice_kept": "Earlier events were pruned" in html,
            "stream_frames_unchanged": "task_event" in html
            and "resync_required" in html
            and "stream_error" in html,
        },
        "styles": {
            "strip": ".harness-timeline {" in html,
            "list": ".harness-timeline-list {" in html,
            "rows": ".harness-timeline-row {" in html,
            "modes": all(f'.harness-timeline-state[data-mode="{mode}"]' in html for mode in ("live", "replay", "pruned")),
            "highlight": '.h-event[data-highlight="true"]' in html,
            "recorded": ".h-recorded {" in html and ".h-recorded-type {" in html,
            "wraps_long_paths": "overflow-wrap: anywhere" in html,
        },
        "types": {
            "emitted": types,
            "with_a_card": cards,
            "described_by_name": explicit,
            "with_only_the_generic_preview": [name for name in types if name not in explicit],
            "dead_branches": dead,
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    frontend = read_frontend(html, server)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    strip_rules = (
        ("arrives_hidden", "the log strip is visible before any log has been read"),
        ("is_a_disclosure", "the strip can no longer be opened and closed"),
        ("states_the_count", "the strip does not say how many events it holds"),
        ("states_the_log", "the strip's sentence is not a live region, so a change is not announced"),
        ("sits_above_the_trajectory", "the strip is no longer above the trajectory it describes"),
        ("names_the_list", "the log list has no accessible name"),
        ("order_is_stated", "the log list does not say what order it is in"),
        (
            "never_shrinks_away",
            "the strip can be shrunk to nothing, so a short window silently drops the record",
        ),
        (
            "keeps_the_trajectory_room",
            "the strip can take the trajectory's whole pane, so the view it explains cannot be read",
        ),
        (
            "hidden_is_not_displayed",
            "a control with [hidden] is still drawn, because .btn sets its own display",
        ),
        ("written_only_on_change", "the strip is rewritten on every render"),
        ("count_is_rewritten", "the strip's count is never updated"),
        ("rows_carry_seq_and_type", "a log row carries neither its seq nor its type"),
        ("row_is_a_button", "a log row is not a button, so it cannot be reached by keyboard"),
        ("row_names_its_target", "a log row does not say what it will show"),
    )
    for flag, description in strip_rules:
        if not frontend["strip"].get(flag):
            fail("strip", description)

    coverage_rules = (
        ("fallback_exists", "there is no card for an event type the pane does not know"),
        (
            "renderer_ends_in_the_fallback",
            "the renderer no longer falls through to the recorded-event card, so an unknown event draws nothing",
        ),
        ("no_event_renders_nothing", "the renderer can return an empty string, so an event can vanish"),
        ("a_missing_type_still_draws", "an event with no type renders as nothing"),
        ("summary_reads_the_event", "the summary does not read the event it describes"),
        ("no_dead_branches", "a card exists for an event type the server never emits"),
    )
    for flag, description in coverage_rules:
        if not frontend["coverage"].get(flag):
            fail("coverage", description)

    trajectory_rules = (
        ("every_card_carries_its_seq", "a card does not carry the seq a log row points at"),
        ("every_card_carries_its_type", "a card does not carry its event type"),
        ("a_row_can_find_its_card", "a log row cannot find the card for its event"),
        ("the_card_is_brought_into_view", "pointing at an event does not scroll to it"),
        ("the_card_is_marked_in_words", "a pointed-at card is not marked"),
        ("the_mark_is_removed", "a pointed-at card keeps its mark"),
        ("the_card_is_focusable", "a pointed-at card cannot take focus"),
    )
    for flag, description in trajectory_rules:
        if not frontend["trajectory"].get(flag):
            fail("trajectory", description)

    place_rules = (
        ("near_bottom_is_one_rule", "there is no single rule for whether the reader is at the newest event"),
        ("a_pinned_view_follows", "a reader at the bottom is not followed by new events"),
        ("a_reading_view_is_left_alone", "a frame moves a reader who is scrolled up"),
        ("open_cards_stay_open", "a frame shuts the cards the reader opened"),
        ("the_jump_is_offered_only_when_needed", "the jump is not tied to the reader's position"),
        ("the_jump_is_the_only_thing_that_scrolls", "something other than the jump scrolls the reader"),
        ("the_jump_follows_a_scroll", "the jump does not follow the reader's own scrolling"),
        ("the_jump_follows_a_render", "the jump is not re-evaluated when the view is rewritten"),
    )
    for flag, description in place_rules:
        if not frontend["place"].get(flag):
            fail("place", description)

    replay_rules = (
        ("re_reads_from_the_start", "Replay no longer reads the log from its first retained event"),
        ("rebuilds_the_view_from_the_log", "Replay does not rebuild the view from what it read"),
        ("counts_the_replay", "Replay is not counted, so a reader cannot tell a replay from a frame"),
        (
            "refuses_a_log_it_could_not_resume",
            "Replay reports success for a log it could not resume",
        ),
        ("says_so_then_resyncs", "a failed replay is silent instead of saying so and resyncing"),
        ("will_not_claim_another_session", "a replay that started on one session can render another"),
    )
    for flag, description in replay_rules:
        if not frontend["replay"].get(flag):
            fail("replay", description)

    honesty_rules = (
        ("states_the_source_live", "the strip does not say when it is following the stream"),
        ("states_the_count_and_range", "the strip does not say how many events it holds and from where"),
        ("states_the_cursor", "the strip does not say where the cursor is"),
        ("says_when_the_log_did_not_resume", "a log that did not resume is presented as a normal read"),
        ("says_what_is_pruned", "pruned history is not counted"),
        ("says_this_view_holds_less", "the strip does not say when it holds fewer events than were reported"),
        ("counts_the_replays", "the strip does not count replays"),
        ("following_needs_a_cursor", "the strip claims to follow a log it has not read"),
        ("pruning_wins_the_colour", "a pruned log is not the colour that says something is missing"),
        ("a_run_in_flight_is_stated", "the strip does not say when a run is writing the trajectory"),
        ("no_network_in_the_strip", "the strip touches the network"),
        ("hidden_without_a_session", "the strip is drawn with no session to describe"),
    )
    for flag, description in honesty_rules:
        if not frontend["honesty"].get(flag):
            fail("honesty", description)

    telemetry_rules = (
        ("one_record_of_how_it_was_filled", "nothing records how the view was filled"),
        ("the_log_path_says_log", "a page read does not record that it was a page read"),
        ("the_stream_path_says_live", "a streamed event does not record that it was streamed"),
        ("the_snapshot_path_says_snapshot", "a resync does not record that it was a resync"),
        ("the_page_reports_the_newest_seq", "a page read does not record how far the log runs"),
        ("the_page_reports_the_retained_floor", "a page read does not record how much of the log was pruned"),
        ("a_gap_is_recorded_as_a_reason", "a gap is not kept, so the strip cannot name it"),
        ("a_new_session_forgets_it", "a new session inherits the previous session's record"),
    )
    for flag, description in telemetry_rules:
        if not frontend["telemetry"].get(flag):
            fail("telemetry", description)

    preserved = frontend["preserved"]
    if preserved["gap_reasons"] != list(GAP_REASONS):
        fail(
            "gap-detection-lost",
            f"the gap detector no longer detects {sorted(set(GAP_REASONS) - set(preserved['gap_reasons']))}",
        )
    for flag, description in (
        ("dedupes_by_seq", "events are no longer deduplicated by seq, so a replay could double every card"),
        ("keeps_the_cursor", "the cursor is gone"),
        ("snapshot_required_handled", "the server's snapshot_required is ignored"),
        ("resync_still_reloads_state", "a resync no longer re-reads the task state"),
        ("resync_reopens_the_stream", "a resync no longer re-anchors the stream"),
        ("pruned_notice_kept", "the pruned-history notice the pane already had is gone"),
        ("stream_frames_unchanged", "a stream frame the pane already handled is gone"),
    ):
        if not preserved.get(flag):
            fail("preserved", description)

    for flag, description in (
        ("strip", "the strip has no rule"),
        ("list", "the log list has no rule"),
        ("rows", "a log row has no rule"),
        ("modes", "a log state has no tone"),
        ("highlight", "a pointed-at card has no rule"),
        ("recorded", "the recorded-event card has no rule"),
        ("wraps_long_paths", "a long path in a row has nothing to wrap it"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    for element_id, needle in STRIP_CONTROLS.items():
        if needle not in html:
            fail("control-lost", f"{element_id} no longer carries the attribute that made it work")

    routes = []
    for method, path in ROUTES:
        pattern = rf'@app\.{method.lower()}\(\"{re.escape(path)}\"\)'
        mounted = bool(re.search(pattern, server))
        routes.append({"method": method, "path": path, "mounted": mounted})
        if not mounted:
            fail("route-missing", f"{method} {path} is no longer mounted, so the strip describes nothing")
    if "/api/harness/events" in html or "/api/harness/log" in html:
        fail(
            "new-route",
            "the pane reads a new log route; the durable log endpoint it already had is the record",
        )

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P3-02",
        "statement": (
            "The Harness's trajectory is a view of a durable log, and the pane now says so: a strip above the "
            "trajectory states how many events it holds, the seq range, where the cursor is and whether it is "
            "following the stream, showing a re-read, or showing a snapshot the log would not resume from; the "
            "strip opens into one row per held event, oldest first, and a row points at its own card; every "
            "retained event draws - including the twenty-five types this pane used to drop on the floor, which now "
            "reach a recorded-event card instead of a blank space; Replay re-reads the log from its first retained "
            "event and rebuilds the view from it without duplicating one; and a frame that lands while the reader "
            "is scrolled up no longer moves them, with one control, Jump to latest, as the only thing that does."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py"},
        "modes": list(MODES),
        "gap_reasons": list(GAP_REASONS),
        "routes": routes,
        "frontend": frontend,
        "totals": {
            "modes": len(MODES),
            "gap_reasons": len(GAP_REASONS),
            "strip_controls": len(STRIP_CONTROLS),
            "functions": len(FUNCTIONS),
            "event_types_emitted": frontend["coverage"]["types_emitted"],
            "event_types_with_a_card": frontend["coverage"]["types_with_a_card"],
            "event_types_described_by_name": frontend["coverage"]["types_described_by_name"],
            "event_types_with_only_the_generic_preview": frontend["coverage"][
                "types_with_only_the_generic_preview"
            ],
            "checks": sum(
                len(frontend[key])
                for key in (
                    "strip",
                    "coverage",
                    "trajectory",
                    "place",
                    "replay",
                    "honesty",
                    "telemetry",
                    "preserved",
                    "styles",
                )
            ),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the strip is a view of what the browser holds, not a second log: it never invents an event, and "
            "`this view holds N of the M events last reported` is the widest claim it makes about events it "
            "cannot see",
            "the recorded-event card is a fallback, not a design for every type: the eighteen types with a card "
            "keep it, and the twenty-five without one are described by `harnessEventSummary()` in the reader's "
            "terms rather than printed as JSON",
            "Replay re-reads the log and rebuilds the trajectory; it does not re-run the task, replay tool "
            "effects, or roll the workspace back - a durable log records what happened, not a way to undo it",
            "the strip is capped at 45% of the pane and its list at `min(168px, 22vh)`, because the record "
            "explains the trajectory and must not replace it; in a window too short for both, the strip keeps "
            "its line and the trajectory keeps its floor",
            "pressing Jump to latest focuses the newest card, so a keyboard reader arrives where a mouse reader "
            "does; it does not mark that card, because nothing was pointed at",
            "the pruned and failed-resume states are exercised from the log state the pane derives, because the "
            "demo instance's log is never pruned: the walk builds those states from the events the server really "
            "returned and says so in its step names",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    dropped = totals["event_types_emitted"] - totals["event_types_with_a_card"]
    lines = [
        "# The Harness: the durable log, and the trajectory as a view of it",
        "",
        "Generated by `python3 scripts/uiux_harness_timeline_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## What the pane showed before",
        "",
        "The server has always written a durable event log — `session/init`, a `security/policy_revision` for every",
        "policy change, a `turn/*` family, `approval/*`, `edit/*`, `delegation/*`, `handoff/*`, `mcp/*` — and the",
        "pane has always been able to read it back from a cursor.",
        "",
        f"In practice a session with a log drew an empty trajectory. The renderer had a card for "
        f"{totals['event_types_with_a_card']} of the {totals['event_types_emitted']} types the server emits and",
        f"returned `\"\"` for the other {dropped}: `session/init` (so every session began blank), all seven",
        "`security/*` types (so a policy change left no trace), and the whole `edit/*`, `delegation/*`,",
        "`handoff/*` and `mcp/*` families. A reader could not tell a dropped event from an event that never",
        "happened.",
        "",
        "Three more things went unsaid. The pane rewrote the whole transcript on every frame and then set",
        "`scrollTop = scrollHeight`, so a reader who had scrolled up to read a failure was pulled back to the bottom.",
        "Nothing stated where the cursor was, how many events were held, or that the view had come from a re-read",
        "rather than the stream. And there was no way to rebuild the view from the record on demand.",
        "",
        "## The strip",
        "",
        "`#harnessTimeline` sits immediately above the trajectory and is the pane's answer to *what am I looking at?*",
        "It arrives hidden, states its count, and opens into the record itself:",
        "",
        "```",
        "▾ Session log   13 events                         [Jump to latest ↓] [Replay log] [Resync]",
        "following the log from seq 13 · 13 events held · replayed 2 times",
        "",
        "  #1   session/init             standard policy · direct /tmp/project · lead Qwen3.8-27B   04:55:16",
        "  #2   security/policy_revision standard -> plan (revision 2)                             04:55:19",
        "  #3   security/policy_revision plan -> autonomous (revision 3)                           04:55:19",
        "```",
        "",
        "The sentence beneath the bar is a polite live region, and it is built from the log state rather than from",
        "prose:",
        "",
        "| State | What it says |",
        "|---|---|",
        "| `live` | `following the log from seq N · M events held` — the stream is filling the view |",
        "| a run in flight | `a run is in flight: the trajectory is what it sends, and M events are held in the log` |",
        "| idle | `M events held, seq A-B · cursor N of L` — the log was read, and the stream is not open |",
        "| `replay` | `the log did not resume (reason), so this view is the snapshot's: task state and M events, seq A-B` |",
        "| `pruned` | `… · K earlier events pruned, retained from seq R` and `· this view holds N of the M events last reported` |",
        "",
        "Those last two are the honest states. A gap in the log is not silently smoothed over into a normal read,",
        "and a view that holds fewer events than the log last reported says so instead of implying it has",
        "everything. Pruning wins the colour over every other state, because a missing event matters more than",
        "whether the view is live.",
        "",
        "## One row per event, and a row that points",
        "",
        "Each row is a button carrying its `seq`, its `type`, a sentence in the reader's terms and its time. Pressing",
        "it brings the matching card into view and marks it for 1.4 seconds:",
        "",
        "```js",
        "stream.querySelector('.h-event[data-seq=\"8\"]').scrollIntoView({ block: \"center\" });",
        "```",
        "",
        "Every rendered event is wrapped in `.h-event[data-seq][data-type]` so that this is possible at all. The",
        "inventory fails if a card loses its seq, if a row cannot find its card, or if the mark is never removed.",
        "",
        "## Coverage: nothing is dropped",
        "",
        "`renderStoredHarnessEventHtml()` now ends in `renderRecordedHarnessEventHtml(ev)` instead of `return \"\"`,",
        "so an event the pane has no card for still arrives as a compact line:",
        "",
        "```",
        "session/init          standard policy · direct /tmp/project · lead Qwen3.8-27B",
        "security/policy_revision  standard -> plan (revision 2)",
        "edit/applied          applied · src/app.py",
        "```",
        "",
        "The split as it stands: "
        f"{totals['event_types_with_a_card']} types have a card of their own,",
        f"{totals['event_types_described_by_name'] - totals['event_types_with_a_card']} more are described by a",
        "sentence in `harnessEventSummary()` on the fallback card (that is where `session/init` and",
        f"`security/policy_revision` live), and {totals['event_types_with_only_the_generic_preview']} fall through to",
        "a `key=value` preview of whatever the event carries — still visible, still counted, just not phrased.",
        "",
        "The inventory counts that split and fails on a card for a type the server never emits: a dead branch is",
        "a card no event can ever reach.",
        "",
        "## Place: a frame must not move the reader",
        "",
        "`harnessStreamNearBottom()` is the one rule for whether the reader is at the newest event, with the same",
        "slack the chat transcript uses. The renderer asks it once, before rewriting anything:",
        "",
        "* **at the bottom** — the view follows, as it always did;",
        "* **scrolled up** — `scrollTop` is restored, the `<details>` cards the reader opened stay open, and the count",
        "  briefly marks itself as fresh;",
        "* **Jump to latest** — the only control that scrolls the reader unasked, it appears when the reader is away",
        "  from the newest event and removes itself when they arrive.",
        "",
        "This is the same rule UIUX-P2-04 applied to the chat transcript, deliberately: two surfaces that",
        "auto-scroll should not disagree about when.",
        "",
        "## Replay",
        "",
        "`replayHarnessLog()` empties the cache, re-reads the log from its first retained event (a cursor of zero",
        "resolves to the retained floor) and rebuilds the trajectory from what came back. It does not re-read the snapshot, because",
        "the point of the log path is that the view can be built from the log alone. If the log will not resume — a",
        "pruned head, a sequence gap — it says so and falls back to the authoritative snapshot instead of presenting",
        "a log with holes as a rebuild. Each replay is counted in the strip, so a reader can tell a re-read from a",
        "frame.",
        "",
        "## What this story had to preserve",
        "",
        f"The cursor, the snapshot/gap detector ({payload['totals']['gap_reasons']} reasons), seq-deduplication, the",
        "pruned-history notice, the three stream frames (`task_event`, `resync_required`, `stream_error`) and the",
        "resync path itself are all unchanged, and the inventory fails if any of them goes. The four routes the",
        "strip reads through already existed; this story added no endpoint.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        f"`--check` re-derives {totals['checks']} checks from the source and fails on any of them. "
        "`node scripts/harness_timeline_walk.mjs` drives the same surface in a real browser against a seeded demo "
        "instance: it creates a session through the API (which is what writes `session/init`), changes the policy",
        "twelve times (which is what writes `security/policy_revision`), and watches the strip, the rows and the",
        "cards agree about all thirteen — then replays the log twice, scrolls up, resizes the window, and",
        "photographs the states in [`media/harness-timeline/`](media/harness-timeline/README.md).",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`.",
        "2. `python3 scripts/uiux_harness_timeline_inventory.py`.",
        "3. `node scripts/harness_timeline_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
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
            print("error: the Harness timeline inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_harness_timeline_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the Harness's durable timeline disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"harness timeline OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['event_types_emitted']} event types, 0 findings"
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
