#!/usr/bin/env python3
"""Inventory the Council's state consistency and stream recovery (UIV2-15).

    python3 scripts/uiux_council_state_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_council_state_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_council_state_inventory.py --json     # print the inventory

Three drifts met in the Council pane, and none of them was a drawing problem:

* **a catalogue poll could rebuild a picker over the id it held.** `syncUnifiedSelectors()`
  rewrote every model picker whenever the catalogue's ids changed, and any picker whose id was
  not in the new read fell through to the catalogue default. The four seat pickers were
  protected (the id was kept once under "Unavailable"); `chatModel`, `c-arch`, `d-judge` and the
  harness pickers were not, and a role change in the single-seat card overwrote a picked model
  with the substring search's favourite for the new role. The next dispatch then spent a real
  inference on a model nobody picked, labelled with its name.
* **a stream could end without telling anyone.** `consumeSSE()` calls `handlers.closed()` when
  the response ends with no `done`/`error` frame, and the summon passed no such handler: the
  "Connecting..." / "Streaming..." card stayed on screen for as long as the page was open, and
  nothing said the reply had not been saved. A re-read could not say it either, because the
  session payload carried no turn state.
* **a session's own state could be ignored.** A closed session accepted a summon and answered
  over its pinned conclusion (and with `pin_as_conclusion` replaced it), the summon route
  silently substituted a model (`Qwen3.8-27B-oQ4e-mtp`) and a seat (`architect`) when a body did
  not name them, and `selectHallSession()` returned on `!r.ok` without a word - leaving the
  previous transcript on screen under the new title.

What this inventory checks: the two turn facts the server now records, the three refusals it now
makes instead of substituting, the picker rule that keeps a pick across a catalogue refresh, the
recovery path a dropped stream takes, and the preservation of everything the pane already did
(the composer, the note-only path, the verdict block, the conclusion pin, the seats' order).

It fails when a picker can be silently re-pointed, when a stream can end without a stated
outcome, when a closed or deleted session can be dispatched into, when the server substitutes a
model or a seat, or when the turn facts stop travelling with the session read.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_State.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-state.md"

STORY = "UIV2-15"
TITLE = "Council state consistency and streaming recovery"

# The catalogued pickers, and the five a Council dispatch reads.
PICKERS = (
    "harnessLeadModel", "harnessCriticModel", "hallModelSelect",
    "hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge",
    "c-arch", "c-coder", "c-critic", "c-judge", "d-judge", "chatModel",
)
COUNCIL_PICKERS = ("hallModelSelect", "hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge")

# The turn facts a session read carries, and what each one lets the pane say.
TURN_FACTS = {
    "active_turn": "a seat is answering on the server right now",
    "abandoned_turn": "a stream ended and its reply never reached the transcript",
}

# The refusals this story added, and the substitution each replaces.
REFUSALS = (
    ("400", "a summon that names no model", "the route defaulted to Qwen3.8-27B-oQ4e-mtp"),
    ("400", "a summon that names no known role", "AGENT_ROLES.get(role_key, architect) made it Seat 1"),
    ("409", "a summon into a closed session", "the answer was written over the pinned conclusion"),
)

HALL_ROUTES = (
    ("GET", "/api/hall/sessions"),
    ("POST", "/api/hall/sessions"),
    ("DELETE", "/api/hall/sessions"),
    ("GET", "/api/hall/sessions/{session_id}"),
    ("DELETE", "/api/hall/sessions/{session_id}"),
    ("POST", "/api/hall/sessions/{session_id}/message"),
    ("POST", "/api/hall/sessions/{session_id}/close"),
    ("POST", "/api/hall/sessions/{session_id}/summon"),
)


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(source)):
        char = source[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = source.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    return source[start:]


def python_function(source: str, name: str) -> str:
    """The indented body of ``def name(...)`` in Python source, by dedent."""
    match = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M)
    if not match:
        return ""
    depth = 0
    index = match.start()
    while index < len(source):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    colon = source.find(":", index)
    if colon < 0:
        return ""
    body_start = source.find("\n", colon)
    if body_start < 0:
        return ""
    collected: list[str] = []
    for line in source[body_start + 1 :].splitlines():
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def route_handler(source: str, path: str) -> str:
    """The ``api_hall_*`` handler registered for ``path``, if any."""
    match = re.search(rf'@app\.\w+\("{re.escape(path)}"\)\s*\n(?:async\s+)?def\s+(\w+)', source)
    return python_function(source, match.group(1)) if match else ""


def code_only(source: str) -> str:
    """``source`` without its comment lines: prose about old behaviour is not that behaviour."""
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))


def mounted_routes(source: str) -> list[tuple[str, str]]:
    return [
        (method.upper(), path)
        for method, path in re.findall(r'@app\.(get|post|put|delete|patch)\("([^"]+)"\)', source)
    ]


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    findings: list[dict[str, str]] = []
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail if not ok else ""})
        if not ok:
            findings.append({"check": name, "detail": detail})

    sync = function_body(html, "syncUnifiedSelectors")
    role_change = html[html.find('document.getElementById("hallRoleSelect").addEventListener'):][:1400]
    select_session = function_body(html, "selectHallSession")
    reconcile = function_body(html, "hallReconcileTurn")
    summon = function_body(html, "summonAgentPromise")
    round_body = function_body(html, "runHallFullRound")
    readiness = function_body(html, "renderCouncilReadiness")
    composer = function_body(html, "postHumanHallMessage")
    gone = function_body(html, "presentHallSessionGone")
    unreadable = function_body(html, "presentHallSessionUnreadable")
    closed = function_body(html, "presentHallClosedSession")
    running = function_body(html, "presentHallTurnRunning")
    lost = function_body(html, "presentHallTurnLost")
    stop_watch = function_body(html, "stopHallTurnWatch")

    summon_route = route_handler(server, "/api/hall/sessions/{session_id}/summon")
    append_body = python_function(server, "append_hall_message")
    read_body = python_function(server, "get_hall_session")
    turn_state = python_function(server, "hall_turn_state")

    # 1. The picker rule: one held id, for every picker, across a catalogue refresh.
    check(
        "the Council pickers a dispatch reads are named once, in one list",
        'const COUNCIL_PICKER_IDS = ["hallModelSelect", "hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge"]' in html,
        "the five pickers must be declared once so the freeze and the tests read the same list",
    )
    check(
        "a catalogue poll does not rebuild the Council pickers while a dispatch holds them",
        "COUNCIL_PICKER_IDS.includes(elId) && (councilRoundInProgress || councilSummonInProgress)" in sync
        and "continue;" in sync,
        "a rebuild mid-round can only cost the reader the pick it shows",
    )
    check(
        "every picker keeps the id it holds, not only the four Council seats",
        "const held = !firstInit && prevVal && prevVal !== \"__custom__\";" in sync
        and "if (held) sel.value = prevVal;" in sync
        and "councilPicker" not in sync,
        "a picker that falls through to the catalogue default dispatches a model nobody picked",
    )
    check(
        "an id the current read does not carry is kept once, under Unavailable",
        'label="Unavailable"' in sync
        and '>[Unavailable] ${escapeHtml(prevVal)}<' in sync
        and "const unavailable = Boolean(held) && !unifiedCatalog.some(item => item.id === prevVal);" in sync,
        "an absent id must stay visible and named rather than be replaced",
    )
    check(
        "a rebuild keeps the keyboard focus it found",
        "const hadFocus = sel === document.activeElement;" in sync
        and "if (hadFocus) sel.focus();" in sync,
        "a poll the reader did not ask for must not move their focus out of the control",
    )
    check(
        "choosing a role does not silently replace the picked model",
        "if (current && current !== \"__custom__\" && unifiedCatalog.some(item => item.id === current)) return;" in role_change,
        "the role handler used to overwrite the picker with the substring search's favourite",
    )
    check(
        "a custom-id marker survives a catalogue refresh",
        'else if (prevVal === "__custom__") sel.value = "__custom__";' in sync,
        "a refresh used to reset the marker to a catalogue default",
    )

    # 2. The two turn facts, and where they come from.
    check(
        "the server keeps a registry for the running turn and the last lost one",
        "_HALL_ACTIVE_TURNS: dict[str, dict[str, Any]] = {}" in server
        and "_HALL_ABANDONED_TURNS: dict[str, dict[str, Any]] = {}" in server
        and "def hall_turn_state(session_id: str) -> dict[str, Any]:" in server,
        "without a registry a re-read cannot distinguish running from lost",
    )
    check(
        "both facts travel with the session read the pane already makes",
        'sess.update(hall_turn_state(session_id))' in read_body
        and '"active_turn"' in turn_state
        and '"abandoned_turn"' in turn_state,
        "the pane reads one route; the turn facts must be on it",
    )
    check(
        "the summon records the turn before it streams and clears it in its own finally",
        "_HALL_ACTIVE_TURNS[session_id] = {" in summon_route
        and "_HALL_ACTIVE_TURNS.pop(session_id, None)" in summon_route,
        "a turn that is not recorded cannot be reconciled by a client that lost the stream",
    )
    check(
        "a stream that ends without saving is recorded as abandoned, not left silent",
        "if not saved and not terminal_sent and session_id not in _HALL_ABANDONED_TURNS:" in summon_route
        and '"streamed_chars": len("".join(buf)) + len("".join(rbuf))' in summon_route,
        "the next read has to be able to say the reply was never written",
    )
    check(
        "a saved turn clears the lost record it answers",
        "_HALL_ABANDONED_TURNS.pop(session_id, None)" in summon_route,
        "a stale 'not saved' label would outlive the retry that succeeded",
    )
    check(
        "deleting or clearing a session drops its turn records",
        "_HALL_ACTIVE_TURNS.pop(session_id, None)" in python_function(server, "delete_hall_session_db")
        and "_HALL_ABANDONED_TURNS.pop(sid, None)" in python_function(server, "clear_hall_sessions_db"),
        "an id reused later must not inherit the last session's labels",
    )

    # 3. The refusals that replace a substitution.
    check(
        "a summon that names no model is refused instead of defaulted",
        "if not model:" in summon_route
        and "the server will not pick one" in summon_route
        and '"Qwen3.8-27B-oQ4e-mtp"' not in code_only(summon_route),
        "the route used to answer for a model the body never named",
    )
    check(
        "a summon that names no known seat is refused instead of becoming Seat 1",
        "if role_key not in AGENT_ROLES:" in summon_route
        and 'AGENT_ROLES.get(role_key, AGENT_ROLES["architect"])' not in code_only(summon_route),
        "an unknown role used to be dispatched as the Architect",
    )
    check(
        "a summon into a closed session is refused, with the way back named",
        'if sess.get("status") == "closed":' in summon_route
        and "status_code=409" in summon_route
        and "Send a Human message to reopen the session" in summon_route,
        "a closed session's pinned conclusion must not be answered over",
    )
    check(
        "a Human message reopens a closed session and keeps the pinned conclusion",
        "if role_key == \"human\":" in append_body
        and "SET status = 'active' WHERE id = ? AND status = 'closed'" in append_body
        and "conclusion_text" not in append_body,
        "the reopen must not clear the verdict, only the status",
    )
    hall_routes_now = {route for route in mounted_routes(server) if route[1].startswith("/api/hall/")}
    check(
        "the story adds no route: the Council surface is the eight routes it already had",
        hall_routes_now == set(HALL_ROUTES),
        f"the Council routes are now {sorted(hall_routes_now)}",
    )

    # 4. The recovery path in the pane.
    unread_block = ""
    if "if (!read.ok) {" in select_session:
        unread_block = select_session[select_session.find("if (!read.ok) {"):][:800]
    check(
        "a failed session read is stated instead of skipped",
        "await readHallSession(id)" in select_session
        and "presentHallSessionGone(" in unread_block
        and "presentHallSessionUnreadable(" in unread_block
        and "return false;" in unread_block
        and "if (!r.ok) return;" not in select_session,
        "silence left the previous transcript on screen under the new title",
    )
    check(
        "a session that no longer exists is named as gone, with the id",
        "no longer exists (404)" in gone and "deleted or cleared" in gone and "Start a new session" in gone,
        "a deleted session must be a state a reader can act on",
    )
    check(
        "an unreadable session keeps what is on screen and says it is not known to be current",
        "is kept, and it is not known to be current" in unreadable,
        "a failed read is not proof that the transcript was lost",
    )
    check(
        "the dropped-stream handler reconciles against the server",
        "async closed()" in summon and "await hallReconcileTurn(" in summon,
        "`consumeSSE` reports a stream that ended without a terminal frame; the pane must answer it",
    )
    check(
        "the text that arrived before the drop is kept, drawn as not saved",
        "Stream ended · not saved" in summon,
        "the reader must keep what arrived without mistaking it for a transcript entry",
    )
    check(
        "the reconcile polls while the server says a seat is answering, and stops",
        "hallTurnWatch = setTimeout(" in reconcile
        and "stopHallTurnWatch();" in reconcile
        and "clearTimeout(hallTurnWatch)" in stop_watch
        and "if (id !== activeHallId) return null;" in reconcile,
        "an unbounded poll, or one that outlives the reader's choice of session, is its own drift",
    )
    check(
        "a running turn is stated as running, not as a failed one",
        "is still answering" in running and "re-read every second until the turn ends" in running,
        "the first question after a drop is whether the seat is still working",
    )
    check(
        "a lost turn names the seat, the model and the discarded characters",
        "was not saved" in lost
        and "none of them reached the transcript" in lost
        and "Ask this seat again" in lost,
        "the reader has to know what did not happen before deciding to retry",
    )
    check(
        "a closed or vanished session cannot be dispatched into, and the reason is on the control",
        "const dispatchBlocked = hallSessionClosed() || hallSessionVanished;" in readiness
        and "round.disabled = invocationBusy || dispatchBlocked || !allSeatsCanInvoke;" in readiness
        and "singleButton.disabled = invocationBusy || dispatchBlocked || !singleCanInvoke;" in readiness
        and "conclusion.disabled = invocationBusy || dispatchBlocked ||" in readiness,
        "readiness reads the machine, not the session: every seat is ready in a closed room",
    )
    check(
        "the closed guard runs before readiness, at both dispatch entry points",
        "if (hallSessionClosed())" in summon
        and summon.find("hallSessionClosed()") < summon.find("councilSeatReadiness(modelId)")
        and "if (hallSessionClosed())" in round_body
        and round_body.find("hallSessionClosed()") < round_body.find("refreshCouncilReadiness()"),
        "a 409 reached as a toast is a worse answer than the pane's own explanation",
    )
    check(
        "the closed presentation offers the write that reopens the session",
        "Write a note to reopen" in closed and "hallHumanInput" in closed,
        "a refusal without the way back is a dead end",
    )
    check(
        "a reopen by message is reported, with the pinned verdict kept",
        "reopenedNow" in composer and "This session is open again" in composer and "is kept" in composer,
        "a state change the reader did not name must not pass as an ordinary note",
    )
    check(
        "the round reconciles after it ends, so a lost seat is not hidden",
        "await hallReconcileTurn();" in round_body,
        "the round's own failure path used to end in a toast",
    )

    # 5. What the pane already did, and still does.
    check(
        "the composer's note-only path stays available in every one of these states",
        'sendButton.disabled = !noteOnly && (invocationBusy || !allSeatsCanInvoke);' in readiness
        and "postHumanHallMessage" in html,
        "a state guard must not remove the write that reopens a closed session",
    )
    check(
        "the pinned conclusion and the verdict block are untouched by this story",
        'id="hallConclusionBanner"' in html
        and "function renderHallVerdict()" in html
        and "hallVerdictSummary" in html,
        "recovery must not cost the reader the conclusion or the disagreements",
    )
    check(
        "one summon call site, one stream reader",
        html.count("consumeSSE(`/api/hall/sessions/${encodeURIComponent(activeHallId)}/summon`") == 1,
        "a second call site would be a second place to keep in step",
    )
    check(
        "the seat order and the reasoning modes the round posts are unchanged",
        '"hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge"' in html
        and 'seat.key === "architect" || seat.key === "judge" ? rm : "off"' in round_body,
        "this story changes what a dispatch may do, not who sits in it",
    )

    return {
        "story": STORY,
        "title": TITLE,
        "generated_by": "scripts/uiux_council_state_inventory.py",
        "pickers": [{"id": name, "council": name in COUNCIL_PICKERS} for name in PICKERS],
        "turn_facts": [{"fact": fact, "meaning": meaning} for fact, meaning in TURN_FACTS.items()],
        "refusals": [
            {"status": status, "refused": refused, "replaces": replaced}
            for status, refused, replaced in REFUSALS
        ],
        "hall_routes": [{"method": method, "path": path} for method, path in HALL_ROUTES],
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "blocked",
    }


# ── the doc ────────────────────────────────────────────────────────────────


def render_doc(payload: dict) -> str:
    pickers = payload["pickers"]
    council = [row["id"] for row in pickers if row["council"]]
    facts = payload["turn_facts"]
    refusals = payload["refusals"]
    lines = [
        f"# Council state consistency and streaming recovery ({STORY})",
        "",
        "Generated by `scripts/uiux_council_state_inventory.py` — **do not hand-edit**.",
        "Run `python3 scripts/uiux_council_state_inventory.py --check` to see whether this doc,",
        "the JSON beside it and the source still agree.",
        "",
        "## What was broken",
        "",
        "1. **A catalogue poll could re-point a picker.** `syncUnifiedSelectors()` rebuilt every",
        "   model picker whenever the catalogue's ids changed, and a picker whose id was not in the",
        "   new read fell through to the catalogue default. Only the four seat pickers kept their",
        "   id. A role change in the single-seat card also overwrote the picked model with the",
        "   substring search's favourite for the new role. The next dispatch then ran a model",
        "   nobody picked and labelled the answer with that model's name.",
        "2. **A stream could end without saying so.** `consumeSSE()` reports a response that ends",
        "   with no terminal frame through `handlers.closed()`, and the summon passed none: the",
        "   \"Connecting...\" card stayed up for the life of the page, and the reply it described was",
        "   never written to the transcript.",
        "3. **A session's own state was ignored.** A closed session accepted a summon and answered",
        "   over the conclusion that was pinned in it; the summon route substituted",
        "   `Qwen3.8-27B-oQ4e-mtp` for an unnamed model and Seat 1 for an unnamed role; and",
        "   `selectHallSession()` returned on a failed read without a word.",
        "",
        "## The two turn facts a session read now carries",
        "",
        "| Fact | What it lets the pane say |",
        "| --- | --- |",
    ]
    for fact in facts:
        lines.append(f"| `{fact['fact']}` | {fact['meaning']} |")
    lines += [
        "",
        "Both are recorded by the summon route and travel with `GET /api/hall/sessions/{id}` —",
        "the read the pane already makes. Neither is a new route, and neither can be written from",
        "outside the turn it describes.",
        "",
        "## The refusals that replaced a substitution",
        "",
        "| Status | Refused | What used to happen |",
        "| --- | --- | --- |",
    ]
    for row in refusals:
        lines.append(f"| `{row['status']}` | {row['refused']} | {row['replaces']} |")
    lines += [
        "",
        "A Human message reopens a closed session on the server (`append_hall_message`), keeps the",
        "pinned conclusion, and the pane reports the reopen — a state change the reader did not",
        "ask for by name is stated, not slipped in.",
        "",
        "## The pickers",
        "",
        "Every picker keeps the id it holds across a catalogue refresh; an id the current read does",
        "not carry stays visible once, under `Unavailable`, and the seat row says what it can do.",
        f"The five a Council dispatch reads are frozen while the dispatch runs: {', '.join('`' + p + '`' for p in council)}.",
        "A rebuild also keeps the keyboard focus it found, because the poll is not the reader's.",
        "",
        "## The recovery path",
        "",
        "* a stream that ends without a terminal frame reconciles against the server instead of",
        "  leaving its own \"Streaming...\" card up;",
        "* while `active_turn` is set the pane says which seat is answering and re-reads once a",
        "  second, stopping when the turn ends or the reader changes session;",
        "* when `abandoned_turn` is set the pane states the seat, the model and the number of",
        "  characters that arrived and were discarded, keeps the partial text as *not saved*, and",
        "  offers the seat again;",
        "* a session that no longer exists is named as gone, with the id and the way forward;",
        "* a closed session cannot be dispatched into from the pane or the route, and the control's",
        "  reason says why — while the composer, whose message reopens it, stays available.",
        "",
        "## What this story does not do",
        "",
        "* it does not resume a stream mid-token: the reply that was lost is retried as a turn, not",
        "  reconstructed from a buffer the server no longer has;",
        "* it does not reopen a session on its own, and it never clears a pinned conclusion when one",
        "  is reopened by a message;",
        "* it adds no route, no migration and no column;",
        "* it does not change who sits in a round, the seats' order, the reasoning modes, the",
        "  verdict block or the conclusion pin.",
        "",
        "## Checks",
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
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


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
                    "the council-state inventory has drifted from the source: "
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
