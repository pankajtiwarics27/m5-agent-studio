#!/usr/bin/env python3
"""Inventory the Start pane's recent work and task handoff (UIUX-P2-02).

    python3 scripts/uiux_recent_work_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_recent_work_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_recent_work_inventory.py --json     # print the inventory

Start exists to begin work, and until this story it could not show a single thing
that had already been done: the Harness kept every session, and the pane you land on
had no idea they existed. The checklist said what was ready and nothing said what was
recent.

The fix is a read, not a second store. Start asks the Harness for its own session
list, renders the five newest as buttons that open the session where it lives, and
offers a composer that carries a task into the Harness without running it. That
shape is what the rules below protect:

* **every status the server writes has words and a tone.** The server writes
  ``idle``, ``running``, ``waiting_approval``, ``completed``, ``failed``, ``cancelled``,
  ``paused``, ``blocked``, ``pending`` and ``error``; Start paints them through
  ``harnessStatusBadge()``, the same table the Harness uses. A status with no row in
  that table would appear on Start as a bare lowercase word while the Harness showed
  a badge - two vocabularies for one state.
* **Start owns nothing.** No POST, PATCH or DELETE: a row opens a session, it does
  not create, retitle or delete one, and the pane never becomes a second place that
  decides what a session is.
* **the list is bounded.** ``START_RECENT_LIMIT`` slices the response, because a pane
  that grows with every session is a pane that eventually takes a second to paint.
* **a row is a button.** Keyboard reachability is not a property a div with an
  onclick can have.
* **empty and unreadable both say something true**, through the presentation
  component, with a cause, what is kept and an action.
* **the launcher hands off, it does not run.** It writes the text into the Harness
  composer and stops, because the workspace, worktree decision and policy are chosen
  there; and its note quotes the preflight's verdict so a task is not handed over
  into a Run that will refuse.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Recent_Work.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-recent-work.md"

SESSION_ROUTE = "/api/harness/sessions"
RECENT_FUNCTIONS = (
    "renderStartRecent",
    "loadStartRecent",
    "openHarnessSessionFromStart",
    "launchTaskFromStart",
    "updateStartTaskHint",
)


class RecentWorkError(Exception):
    """The pane and the Harness disagree."""


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def function_body(html: str, name: str) -> str:
    """The balanced body of ``function name(...)`` (or of an arrow/async form).

    The parameter list is skipped by balancing parentheses, not by looking for the
    first ``{``: a default parameter (``options = {}``) would otherwise be mistaken
    for the body and every check below would read an empty function.
    """
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", html)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth_paren = 0
    close_paren = -1
    for offset in range(open_paren, len(html)):
        char = html[offset]
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
            if depth_paren == 0:
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


def server_statuses(app_source: str) -> list[dict[str, object]]:
    """Every status the server writes *to a harness session*, with where it was seen.

    Scoped to ``_update_harness_session_state(... status="…")`` and the column
    default. A bare ``status="staged"`` elsewhere in a 21k-line file is an edit
    transaction or a sandbox lease, and counting those here would demand a harness
    state for something that is not a harness state.
    """
    statuses: dict[str, list[int]] = {}
    for call in re.finditer(r"_update_harness_session_state\(", app_source):
        chunk = app_source[call.start() : call.start() + 400]
        for match in re.finditer(r'status\s*=\s*"([a-z_]+)"', chunk):
            statuses.setdefault(match.group(1), []).append(_line(app_source, call.start() + match.start()))
    # The column default, read from the harness_sessions table specifically: every
    # other table in this file has a `status` column too, and hall_sessions' default
    # of "active" is not a harness state.
    table = app_source.find("CREATE TABLE IF NOT EXISTS harness_sessions")
    if table >= 0:
        default = re.search(r"status TEXT NOT NULL DEFAULT '([a-z_]+)'", app_source[table : table + 900])
        if default:
            statuses.setdefault(default.group(1), []).append(_line(app_source, table + default.start()))
    return [
        {"status": status, "first_line": min(lines), "occurrences": len(lines)}
        for status, lines in sorted(statuses.items())
    ]


def status_table(html: str) -> dict[str, dict[str, str]]:
    """The harness state table: state -> {tone, label}, read from its one source."""
    body = function_body(html, "harnessStatusBadge")
    pairs = re.findall(r'(\w+):\s*\["(\w+)",\s*"([^"]+)"\]', body)
    return {state: {"tone": tone, "label": label} for state, tone, label in pairs}


def presentation_calls(body: str) -> list[dict[str, object]]:
    """Each presentInto() in one function, with the slots it fills."""
    calls = []
    for match in re.finditer(r'presentInto\("([\w-]+)",', body):
        start = body.find("{", match.end())
        depth = 0
        block = ""
        for offset in range(start, len(body)):
            char = body[offset]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    block = body[start : offset + 1]
                    break
        kind = re.search(r'\bkind:\s*"(\w+)"', block)
        calls.append(
            {
                "container": match.group(1),
                "kind": kind.group(1) if kind else None,
                "has_cause": bool(re.search(r"\bcause:\s*\S", block)),
                "has_context": bool(re.search(r"\bcontext:\s*\S", block)),
                "actions": re.findall(r'\blabel:\s*"([^"]+)"', block),
            }
        )
    return calls


def read_frontend(html: str) -> dict:
    bodies = {name: function_body(html, name) for name in RECENT_FUNCTIONS}
    renderer = bodies["renderStartRecent"]
    loader = bodies["loadStartRecent"]
    launcher = bodies["launchTaskFromStart"]
    hint = bodies["updateStartTaskHint"]
    recent_block = "\n".join(bodies.values())
    presentations = presentation_calls(loader) + presentation_calls(renderer)
    routes = sorted(set(re.findall(r'"(/api/[^"]*)"', recent_block)))
    methods = sorted(set(re.findall(r'method:\s*"(GET|POST|PATCH|PUT|DELETE)"', recent_block)))
    return {
        "functions": {name: bool(body) for name, body in bodies.items()},
        "routes": routes,
        "methods": methods,
        "limit": {
            "declared": "const START_RECENT_LIMIT =" in html,
            "applied": "slice(0, START_RECENT_LIMIT)" in recent_block,
            "value": (re.search(r"const START_RECENT_LIMIT = (\d+);", html) or [None, None])[1],
        },
        "presentations": presentations,
        "renderer": {
            "button_rows": 'createElement("button")' in renderer,
            "no_inline_onclick": "onclick=" not in renderer,
            "uses_state_table": "= harnessStatusBadge(" in renderer,
            "paints_status_via_setStatus": "setStatus(pill," in renderer,
            "no_badge_class": 'className = "badge' not in renderer,
            "escapes_text": "textContent" in renderer,
        },
        "launcher": {
            "opens_harness": 'switchTab("harness")' in launcher,
            "writes_composer": "harnessPromptInput" in launcher,
            "fetches_nothing": "fetch(" not in launcher,
            "runs_nothing": not re.search(r"\brunHarness|startHarnessRun|/api/harness/sessions/.+run", launcher),
            "focuses_composer": ".focus()" in launcher,
        },
        "hint": {
            "reads_preflight": "onboardingReport" in hint,
            "names_next_step": "next_step" in hint,
            "states_ready": "onboardingReport.ready" in hint,
            "described_by": 'aria-describedby="startTaskHint"' in html,
            "element": 'id="startTaskHint"' in html,
            "refreshed_on_recheck": "updateStartTaskHint();" in html,
        },
        "startup": {
            "loads_on_start": "loadStartRecent();" in html,
            "refreshes_on_tab": 'if (Date.now() - startRecentReadAt > 15000) loadStartRecent({ silent: true });' in html,
        },
        "styles": {
            "rows": ".start-recent-row {" in html,
            "focus_visible": ".start-recent-row:focus-visible" in html,
            "launch": ".start-launch {" in html,
            "loading": ".start-recent-loading {" in html,
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    app_source = APP.read_text(encoding="utf-8")
    statuses = server_statuses(app_source)
    table = status_table(html)
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. Every status the server writes must have words and a tone.
    for entry in statuses:
        if entry["status"] not in table:
            fail(
                "status-without-tone",
                f"the server writes {entry['status']!r} (app.py line {entry['first_line']}) but harnessStatusBadge() has no row for it",
            )

    # 2. Start reads the Harness's list and nothing else.
    if frontend["routes"] != [SESSION_ROUTE]:
        fail("recent-work-route", f"the recent-work code reads {frontend['routes']}, expected exactly [{SESSION_ROUTE!r}]")
    if frontend["methods"]:
        fail("client-owns-sessions", f"Start issues {frontend['methods']} against the session API; it must only read")
    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    # 3. Bounded rendering.
    if not (frontend["limit"]["declared"] and frontend["limit"]["applied"]):
        fail("unbounded-list", "the recent list is rendered without START_RECENT_LIMIT")

    # 4. A row is a button, painted by the shared table.
    renderer = frontend["renderer"]
    for flag, description in (
        ("button_rows", "rows are not <button> elements, so they cannot be reached by keyboard"),
        ("no_inline_onclick", "a row carries an inline onclick instead of a listener"),
        ("uses_state_table", "rows do not use harnessStatusBadge(), so Start has its own state vocabulary"),
        ("paints_status_via_setStatus", "rows do not paint through setStatus()"),
        ("no_badge_class", "a row writes the legacy .badge class"),
        ("escapes_text", "row text is written as markup rather than textContent"),
    ):
        if not renderer.get(flag):
            fail("row-not-a-control", description)

    # 5. Empty and unreadable both present a cause, a kept line and an action.
    kinds = {call["kind"] for call in frontend["presentations"]}
    for kind in ("empty", "error"):
        if kind not in kinds:
            fail("presentation-missing", f"no {kind} presentation for the recent list")
    for call in frontend["presentations"]:
        if not (call["has_cause"] and call["has_context"] and call["actions"]):
            fail("presentation-incomplete", f'{call["kind"]} presentation into #{call["container"]} is missing cause, context or actions')

    # 6. The launcher hands off; it does not run.
    launcher = frontend["launcher"]
    for flag, description in (
        ("opens_harness", "the launcher does not open the Harness"),
        ("writes_composer", "the launcher does not put the text in the Harness composer"),
        ("fetches_nothing", "the launcher makes a request, so it is starting something rather than handing off"),
        ("runs_nothing", "the launcher calls a run endpoint"),
        ("focuses_composer", "the launcher does not focus the composer it filled"),
    ):
        if not launcher.get(flag):
            fail("launcher-runs", description)

    # 7. The handoff's note quotes the preflight verdict, and is the button's description.
    hint = frontend["hint"]
    for flag, description in (
        ("reads_preflight", "the launcher note ignores the preflight"),
        ("names_next_step", "the launcher note does not name the step holding Run back"),
        ("states_ready", "the launcher note does not distinguish ready from not ready"),
        ("described_by", "the composer's description is not wired to the note"),
        ("element", "there is no #startTaskHint element"),
        ("refreshed_on_recheck", "the note is not refreshed when the preflight is re-read, so it can go stale"),
    ):
        if not hint.get(flag):
            fail("launcher-hint-hollow", description)

    # 8. It is loaded at startup and refreshed on the way in.
    if not frontend["startup"]["loads_on_start"]:
        fail("not-loaded", "the recent list is never read at startup, so Start opens without it")
    if not frontend["startup"]["refreshes_on_tab"]:
        fail("never-refreshed", "the recent list is never refreshed when Start is selected")

    # 9. What the renderer emits has rules.
    for flag, description in (
        ("rows", "a recent row has no rule"),
        ("focus_visible", "a focused row has no visible focus"),
        ("launch", "the launcher row has no rule"),
        ("loading", "the loading line has no rule"),
    ):
        if not frontend["styles"].get(flag):
            fail("style-missing", description)

    coverage = [
        {
            "status": entry["status"],
            "tone": table.get(entry["status"], {}).get("tone"),
            "label": table.get(entry["status"], {}).get("label"),
            "app_line": entry["first_line"],
            "occurrences": entry["occurrences"],
        }
        for entry in statuses
    ]
    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-02",
        "statement": (
            "Start shows the five most recent Harness sessions as buttons that open them where they live, painted through "
            "the same state table the Harness uses, and offers a composer that carries a task into the Harness without "
            "running it. Start reads the session list; it never creates, retitles or deletes a session."
        ),
        "source": {"frontend": "static/index.html", "server": "app.py", "route": SESSION_ROUTE, "method": "GET"},
        "status_coverage": coverage,
        "session_fields_used": ["id", "title", "status", "step_count", "policy", "workspace_dir", "updated_at"],
        "limit": frontend["limit"],
        "presentations": frontend["presentations"],
        "frontend": frontend,
        "totals": {
            "server_statuses": len(statuses),
            "statuses_with_tone": sum(1 for row in coverage if row["tone"]),
            "recent_limit": int(frontend["limit"]["value"] or 0),
            "routes_read": len(frontend["routes"]),
            "write_methods": len(frontend["methods"]),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the recent list is the Harness's own ordering (updated_at DESC), not a separate notion of recency invented here",
            "the launcher hands a task over; whether it runs, and with which workspace and policy, is decided in the Harness",
            "the browser walk creates and deletes its own sessions, so nothing in this artifact depends on demo state",
            "a session's step count is what the server stored, not a count the pane recomputes from events",
        ],
    }


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    lines = [
        "# Recent work and the task handoff",
        "",
        "Generated by `python3 scripts/uiux_recent_work_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## What Start reads",
        "",
        f"`{payload['source']['method']} {payload['source']['route']}` returns the sessions already ordered by `updated_at DESC`, plus the default "
        "workspace and the recent directories the Harness uses. Start takes the first "
        f"{totals['recent_limit']} and renders one row per session from "
        f"{', '.join(f'`{field}`' for field in payload['session_fields_used'])}. Nothing is copied into a second store: a "
        "row is a button that opens that session in the Harness, so the pane cannot drift from the sessions it shows.",
        "",
        "## Every state the server writes has words",
        "",
        "| Status | Tone | Label | Written at |",
        "|---|---|---|---|",
    ]
    for row in payload["status_coverage"]:
        lines.append(
            f"| `{row['status']}` | {row['tone'] or '—'} | {row['label'] or '—'} | app.py:{row['app_line']} ({row['occurrences']}×) |"
        )
    lines += [
        "",
        f"{totals['statuses_with_tone']} of {totals['server_statuses']} statuses the server writes have a row in "
        "`harnessStatusBadge()`, the table every harness surface paints a run state with. A status without one would appear "
        "on Start as a bare lowercase word while the Harness showed a badge — two vocabularies for one state.",
        "",
        "## The three states of the recent list",
        "",
        "| State | What is shown |",
        "|---|---|",
        "| sessions exist | a bounded list of buttons, newest first, each with title, step count, policy, workspace folder tail and its state pill |",
        "| none yet | the empty presentation: *No recent work yet*, why, what appears once one runs, and actions to start a session or open the Harness |",
        "| unreadable | the error presentation: the cause, *nothing was changed; the task composer below still opens a task*, and Retry / Open Harness |",
        "",
        "## The launcher is a handoff",
        "",
        "`launchTaskFromStart()` writes the text into `#harnessPromptInput`, dispatches `input` so the composer's own "
        "listeners see it, focuses it and stops. It makes no request and calls nothing that runs: the workspace, the "
        "worktree decision and the execution policy are chosen in the Harness, and a run begun from a pane that cannot show "
        "them is a run begun with the wrong ones.",
        "",
        "The note under the composer quotes the preflight's verdict — ready, or not ready with the next step named — and it "
        "is refreshed whenever the preflight is re-read, so it cannot say *ready* an hour after the lead stopped saying it. "
        "The same text is the composer's `aria-describedby`.",
        "",
        "## Rules this artifact enforces",
        "",
        "`--check` fails when the server writes a status with no tone or label, when the recent-work code reads any route "
        "other than the session list or issues a write method at all, when the list is rendered unbounded, when a row is "
        "not a `<button>` or is painted outside `setStatus()`/`harnessStatusBadge()`, when either presentation loses its "
        "cause or action, when the launcher makes a request or calls a run, when its note stops quoting the preflight, or "
        "when a state the renderer emits has no rule.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "The behaviour is measured by `node scripts/start_recent_walk.mjs`, which creates a session through the API the "
        "Harness uses, asserts the row and its state pill, opens it from the row, types a task and asserts that the "
        "composer received it while no session was created and nothing ran, then deletes what it created, exercises the "
        f"empty presentation and finally refuses `{payload['source']['route']}` to assert the error presentation and Retry.",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html` or a status the server writes in `app.py`.",
        "2. `python3 scripts/uiux_recent_work_inventory.py`.",
        "3. `node scripts/start_recent_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
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
            print("error: the recent-work inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_recent_work_inventory.py")
            return 1
        if payload["findings"]:
            print("error: Start and the Harness disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"recent work OK: {payload['totals']['server_statuses']} server statuses all toned, "
            f"limit {payload['totals']['recent_limit']}, {payload['totals']['routes_read']} route read, "
            f"{payload['totals']['write_methods']} writes, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['server_statuses']} statuses, limit {payload['totals']['recent_limit']}, "
        f"{len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
