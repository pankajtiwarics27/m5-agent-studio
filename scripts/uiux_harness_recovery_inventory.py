#!/usr/bin/env python3
"""Inventory how a stopped Harness run is stated and what can be done about it (UIUX-P3-05).

    python3 scripts/uiux_harness_recovery_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_harness_recovery_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_harness_recovery_inventory.py --json     # print the inventory

A run that stopped used to be indistinguishable from a run that never started. Pausing wrote one
mono line into the log, cancelling wrote another, the prepared-resume line was a third, and none
of them said what had survived, what it cost to go on, or what a reader could press. The routes
built for exactly this moment - `POST …/pause`, `…/cancel`, `…/resume`, `…/contain` - were
reachable from the API and from nowhere a person could see, and a worker that died left the task
saying `running` with no stream attached, which the pane drew as a running task.

So this artifact measures five things:

* **the surface**, a panel under the session strip that draws the state a run was left in from
  `GET …/state` and `GET …/task` - the status, the transition and its reason, the newest
  checkpoint, the recovery memory, the task budget and the containment store;
* **the actions**, the four routes that answer this state, each one a press on a real endpoint,
  with cancel behind a confirmation and a refusal drawn as a disabled button carrying its reason
  rather than as a button that fails;
* **the refusals**, every reason a resume would be rejected, stated before the press and in the
  runtime's own vocabulary;
* **the events**, the three recovery events drawn as cards where they happened, instead of a
  one-line reason;
* **the continuation**, the one thing the pane could never do even with the routes reachable:
  `/run` was always posted without `resume`, so a "resume" started the turn over.

It fails when a second recovery renderer appears, when a stopped run is drawn as a running one,
when a control is offered that the state refuses, when a refusal is stated without its reason,
when the panel invents a route the server does not mount, when a value from the record reaches
the page without being escaped, and when the routes this story had to preserve change shape.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-recovery.md"

CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

FUNCTIONS = (
    "harnessRecoveryStatus",
    "harnessRecoveryLastTransition",
    "harnessRecoveryUnresolvedActions",
    "harnessRecoveryResumeRefusals",
    "harnessRecoveryFacts",
    "harnessRecoveryFactsHtml",
    "harnessRecoveryReasonPhrase",
    "harnessRecoveryStory",
    "harnessRecoverySay",
    "renderHarnessRecovery",
    "focusHarnessRecovery",
    "refreshHarnessRecovery",
    "harnessRecoveryRefusalDetail",
    "harnessRecoveryPost",
    "harnessRecoveryAct",
    "harnessLastPrompt",
    "harnessResumePrepared",
    "renderHarnessRecoveryEventHtml",
)

# The states a run can be left in, and what the story must say about each.
STATES = (
    ("running", "this pane is running it"),
    ("running", "this pane is not the run that is running it"),
    ("paused", "Paused at a safe boundary"),
    ("failed", "The run failed"),
    ("blocked", "Blocked on effects the runtime cannot account for"),
    ("cancelled", 'title: "Cancelled"'),
)

# Every reason a resume is refused, in the pane's own words.
REFUSAL_TOKENS = (
    "a run is in progress",
    "blocked on effects the runtime cannot account for",
    "a cancelled task is not resumable",
    "the task is already complete",
    "a cancellation is already pending",
    "the task budget is exhausted",
    "the session is quarantined",
    "persisted barrier(s) hold this workspace",
    "consequential action(s) were left unresolved",
    "failed its integrity check",
    "the recovery memory could not be read",
)

# Routes this story had to preserve, and the one it added nothing to.
PRESERVED_ROUTES = (
    "api_pause_harness_session",
    "api_cancel_harness_session",
    "api_resume_harness_session",
    "api_contain_harness_session",
    "api_abort_harness",
    "api_get_harness_task_state",
    "api_get_harness_session_state",
)


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


def element(source: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def mounted_routes(server: str) -> set[str]:
    """Every route the server mounts, with each path parameter collapsed to `{}`."""
    found = set()
    for match in re.finditer(r'@app\.(?:get|post|put|patch|delete)\("(/[^"]*)"\)', server):
        found.add(re.sub(r"\{[^}]*\}", "{}", match.group(1)))
    return found


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal the pane builds, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def route_matches(called: str, mounted: str) -> bool:
    """Whether a path the pane builds can be answered by a mounted route."""
    a = [segment for segment in called.split("/") if segment]
    b = [segment for segment in mounted.split("/") if segment]
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == "{}" or y == "{}":
            continue
        if x != y:
            return False
    return True


def story_branch(story: str, start: str, end: str) -> str:
    """The slice of a story function between two markers."""
    begin = story.find(start)
    if begin < 0:
        return ""
    finish = story.find(end, begin + len(start))
    return story[begin : finish if finish >= 0 else len(story)]


# Expressions that only choose an attribute, never carry a value from the record.
SAFE_EXPRESSIONS = (
    """fact.mono ? ' class=\"mono\"' : \"\"""",
)


def _expressions(source: str) -> list[str]:
    """Every `${…}` in ``source``, with its braces balanced so nesting does not cut it short."""
    found: list[str] = []
    index = 0
    while True:
        start = source.find("${", index)
        if start < 0:
            return found
        depth = 0
        cursor = start + 1
        while cursor < len(source):
            char = source[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        found.append(source[start + 2 : cursor])
        index = cursor


def unescaped_interpolations(source: str) -> list[str]:
    """`${…}` expressions that reach markup without `escapeHtml(` or a numeric coercion.

    A nested template literal is not judged as a whole: its own `${…}` segments are what
    reach the page, so the scan recurses into it.
    """
    offenders: list[str] = []
    for expression in _expressions(source):
        if "${" in expression:
            offenders.extend(unescaped_interpolations(expression))
            continue
        stripped = expression.strip()
        if stripped in SAFE_EXPRESSIONS:
            continue
        if stripped.startswith(("escapeHtml(", "Number(", "String(", "Boolean(")):
            continue
        # A fragment this file already escaped is spliced in as-is: the `…Html` suffix is the
        # convention the shell uses for exactly that, so it is what the check trusts.
        if re.fullmatch(r"[A-Za-z_$][\w$]*Html", stripped):
            continue
        offenders.append(stripped)
    return offenders


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")

    story = function_body(html, "harnessRecoveryStory")
    refusals = function_body(html, "harnessRecoveryResumeRefusals")
    facts = function_body(html, "harnessRecoveryFacts")
    act = function_body(html, "harnessRecoveryAct")
    render = function_body(html, "renderHarnessRecovery")
    refresh = function_body(html, "refreshHarnessRecovery")
    card = function_body(html, "renderHarnessRecoveryEventHtml")
    post = function_body(html, "harnessRecoveryPost")
    say = function_body(html, "harnessRecoverySay")
    present = function_body(html, "presentInto")
    resync = function_body(html, "harnessResyncFromSnapshot")
    fresh = function_body(html, "startFreshHarnessDraft")
    abort = function_body(html, "abortHarnessRun")
    run_turn = function_body(html, "runHarnessTurn")
    detail = function_body(html, "harnessRecoveryRefusalDetail")
    phrase = function_body(html, "harnessRecoveryReasonPhrase")
    reason_table = html[html.find("const HARNESS_RECOVERY_REASONS = {") : html.find("const HARNESS_RECOVERY_REASONS = {") + 1400]

    panel = element(html, "harnessRecoveryPanel")
    note = element(html, "harnessRecoveryNote")
    story_host = element(html, "harnessRecoveryStory")
    facts_host = element(html, "harnessRecoveryFacts")

    mounted = mounted_routes(server)
    called = called_routes(html)
    blocked_branch = story_branch(story, '} else if (status === "blocked") {', '} else if (status === "cancelled") {')
    cancelled_branch = story_branch(story, '} else if (status === "cancelled") {', "if (!story) return null;")
    paused_branch = story_branch(story, '} else if (status === "paused") {', '} else if (status === "failed") {')
    failed_branch = story_branch(story, '} else if (status === "failed") {', '} else if (status === "blocked") {')
    prepared_branch = story_branch(story, "if (prepared) {", "} else if (status === \"running\") {")
    running_branch = story_branch(story, '} else if (status === "running") {', '} else if (status === "paused") {')

    checks: list[dict] = []

    def check(name: str, ok: bool, detail_text: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail_text if not ok else ""})

    # ── the surface ───────────────────────────────────────────────────────────
    check(
        "one recovery surface, under the session strip",
        html.count('id="harnessRecoveryPanel"') == 1
        and html.find('id="harnessInfoBanner"') < html.find('id="harnessRecoveryPanel"') < html.find('id="harnessTimeline"'),
        "the panel must sit between the session strip and the durable log",
    )
    check(
        "the surface is a labelled group that starts hidden",
        'role="group"' in panel and 'aria-label="Recovery"' in panel and "hidden" in panel,
        panel,
    )
    check(
        "the surface has a story host, a facts list and a live note",
        bool(story_host) and bool(facts_host) and bool(note) and 'role="status"' in note and 'aria-live="polite"' in note,
        f"{story_host} | {facts_host} | {note}",
    )
    check(
        "every function the surface needs exists once",
        all(html.count(f"function {name}(") == 1 for name in FUNCTIONS),
        ", ".join(name for name in FUNCTIONS if html.count(f"function {name}(") != 1),
    )
    check(
        "the surface reads only the two routes that already existed",
        "/task`" in refresh and "/state`" in refresh and refresh.count("fetch(") == 2,
        refresh[:200],
    )
    check(
        "the panel is refreshed by every authoritative resync",
        "await refreshHarnessRecovery();" in resync,
        "a resync that leaves the panel stale is a panel that lies after a reload",
    )
    check(
        "a new draft clears the surface instead of showing the last session's state",
        "harnessRecoveryPayload = null;" in fresh and "renderHarnessRecovery();" in fresh,
        fresh[-400:],
    )

    # ── the states ────────────────────────────────────────────────────────────
    check(
        "every state a run can be left in has its own story",
        bool(prepared_branch) and bool(running_branch) and bool(paused_branch)
        and bool(failed_branch) and bool(blocked_branch) and bool(cancelled_branch),
        "one of the branches is missing from harnessRecoveryStory()",
    )
    check(
        "no story can be drawn without a title, a cause, a kept line and an action",
        story.count("kind: ") == story.count("cause: ") == story.count("context: ") == story.count("actions: ") == 7,
        f"kind={story.count('kind: ')} cause={story.count('cause: ')} context={story.count('context: ')} actions={story.count('actions: ')}",
    )
    check(
        "a running task this pane is not running is not drawn as a live one",
        "this pane is not the run that is running it" in running_branch
        and "Boolean(harnessRunActive)" in story
        and "harnessEventStreamAbort" not in story,
        "an open log stream is not evidence that a worker is alive",
    )
    check(
        "a blocked task offers cancel and draws resume as unavailable",
        'id: "cancel"' in blocked_branch and "disabled: true" in blocked_branch,
        blocked_branch[:240],
    )
    check(
        "a cancelled task offers no resume at all",
        'id: "resume"' not in cancelled_branch and 'id: "fresh"' in cancelled_branch,
        "a cancelled task cannot be resumed, so the surface must not offer it",
    )
    check(
        "a prepared resume is drawn only on a task that is still paused or failed",
        '(status === "paused" || status === "failed")' in story,
        "a preparation that outlives its state is a stale promise",
    )
    check(
        "the reason code is printed beside the phrase a reader gets",
        "harnessRecoveryReasonPhrase" in paused_branch
        and "(${code})" in phrase
        and reason_table.count(': "') >= 5,
        reason_table[:240],
    )
    check(
        "the facts are the record's own fields, and a missing one is named as missing",
        all(token in facts for token in (
            "doc.checkpoints",
            "doc.latest_checkpoint",
            "recovery.budget",
            "containment.persisted_barriers",
            "budget.recoveries_used",
            "milestones",
        ))
        and "none recorded — nothing here can be resumed" in facts,
        facts[:240],
    )

    # ── the actions ───────────────────────────────────────────────────────────
    check(
        "one request helper, and it posts to the session's own routes",
        "const response = await fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}/${path}`" in post,
        post[:300],
    )
    check(
        "pause, cancel and resume are the presses the surface makes",
        all(f'harnessRecoveryPost("{verb}"' in act for verb in ("pause", "resume", "cancel"))
        and "const response = await fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}/${path}`" in post,
        ", ".join(
            verb for verb in ("pause", "resume", "cancel") if f'harnessRecoveryPost("{verb}"' not in act
        ),
    )
    check(
        "abort still has exactly one call site",
        html.count("/api/harness/abort/") == 1,
        "a second abort call site would be a second way to stop following, with its own rules",
    )
    check(
        "abort says what it does not do",
        "ends this view, not the work" in abort,
        "Abort reading as \"stop the work\" is how a reader loses a task",
    )
    check(
        "cancel takes two presses and any other action disarms it",
        "HARNESS_RECOVERY_ACTION_LABELS.confirm_cancel" in story
        and 'if (id !== "cancel") harnessRecoveryArmedCancel = 0;' in act,
        "a one-press cancel wearing a two-press label is worse than no confirmation",
    )
    check(
        "a refused action is refused before any request is made",
        "if (action && action.disabled)" in act
        and act.find("if (action && action.disabled)") < act.find("harnessRecoveryPost("),
        "the press must not reach the network",
    )
    check(
        "a refusal is drawn as a disabled button with its reason, never as a hidden one",
        'button.disabled = true' in present
        and 'aria-disabled", "true"' in present
        and "aria-describedby" in present
        and 'className = "presentation-why"' in present,
        "an action a reader cannot take must stay visible with its reason",
    )
    check(
        "the reason is in text as well as in the accessible name",
        "Not available here — " in present and "aria-label" in render,
        "a disabled control with no stated reason is an authoritative refusal a reader cannot see",
    )
    check(
        "focus survives an action",
        "focusAfter" in render and "host.focus()" in render and "primary.focus()" in function_body(html, "focusHarnessRecovery"),
        "pressing a button that is then re-drawn must not drop a keyboard reader at the top of the page",
    )

    # ── the refusals ──────────────────────────────────────────────────────────
    check(
        "every reason a resume would be refused is stated before the press",
        all(token in refusals for token in REFUSAL_TOKENS),
        ", ".join(token for token in REFUSAL_TOKENS if token not in refusals),
    )
    check(
        "the server's own refusal is printed as it arrived",
        all(token in detail for token in ("message", "code", "detail")),
        detail[:240],
    )
    check(
        "a refused resume keeps the surface honest instead of silently reverting",
        "The runtime refused the resume:" in act and 'harnessRecoverySay(' in act,
        "a 409 is an answer, and the surface has to show it",
    )

    # ── the events ────────────────────────────────────────────────────────────
    recorded = function_body(html, "renderStoredHarnessEventHtml")
    check(
        "the three recovery events have exactly one renderer",
        recorded.count('"turn/paused"') == 1
        and recorded.count('"turn/cancelled"') == 1
        and recorded.count('"turn/resume_prepared"') == 1
        and html.count("renderHarnessRecoveryEventHtml(ev)") == 2,
        f"paused={recorded.count(chr(34) + 'turn/paused' + chr(34))} "
        f"cancelled={recorded.count(chr(34) + 'turn/cancelled' + chr(34))} "
        f"prepared={recorded.count(chr(34) + 'turn/resume_prepared' + chr(34))}",
    )
    check(
        "the one-line recovery renderers are gone",
        'Paused at step ${escapeHtml(String(ev.step || 0))}' not in html
        and 'Cancelled — ${escapeHtml(String(ev.reason || ""))}' not in html
        and "Resume prepared — next step ${escapeHtml" not in html,
        "a one-line reason says neither what survived nor what can be pressed",
    )
    check(
        "the card states what the event did and links back to the surface",
        "were contained" in card and "focusHarnessRecovery()" in card and "Nothing has run" in card,
        card[:240],
    )

    # ── the continuation ──────────────────────────────────────────────────────
    check(
        "a prepared resume is what makes the next run a continuation",
        "const resumingFromCheckpoint = harnessResumePrepared();" in run_turn
        and "resume: resumingFromCheckpoint," in run_turn,
        "without this the runtime's resume support is unreachable from the pane",
    )
    check(
        "the preparation is consumed by the request that uses it",
        "if (resumingFromCheckpoint) harnessRecoveryPrepared = null;" in run_turn,
        "a preparation that survives its run would resume a later turn from an older checkpoint",
    )

    # ── honesty of the page itself ────────────────────────────────────────────
    # The story's strings are set through `textContent`, so they are safe by construction; the
    # facts list and the event card build markup, and everything they interpolate must be escaped.
    markup_sources = "\n".join((card, function_body(html, "harnessRecoveryFactsHtml")))
    check(
        "no value from the record reaches the page unescaped",
        not unescaped_interpolations(markup_sources),
        ", ".join(unescaped_interpolations(markup_sources)[:6]),
    )
    check(
        "a note belongs to the session whose action produced it",
        "sessionId: activeHarnessId" in say
        and "harnessRecoveryNote.sessionId === harnessRecoveryPayload.sessionId" in render,
        "carrying a note to another session attributes an action that never happened there",
    )

    # ── the server contracts this story had to preserve ───────────────────────
    pause_route = python_function(server, "api_pause_harness_session")
    cancel_route = python_function(server, "api_cancel_harness_session")
    resume_route = python_function(server, "api_resume_harness_session")
    contain_route = python_function(server, "api_contain_harness_session")
    abort_route = python_function(server, "api_abort_harness")
    task_route = python_function(server, "api_get_harness_task_state")
    state_route = python_function(server, "_harness_session_state_snapshot")
    check(
        "every preserved route is still mounted",
        all(re.search(rf"^(?:async )?def {name}\(", server, re.M) for name in PRESERVED_ROUTES),
        ", ".join(name for name in PRESERVED_ROUTES if not re.search(rf"^(?:async )?def {name}\(", server, re.M)),
    )
    check(
        "pause still defers to a safe boundary and says which it is",
        '"deferred_to_safe_boundary": outcome.deferred_to_safe_boundary,' in pause_route
        and '"pause_requested" if outcome.deferred_to_safe_boundary' in pause_route,
        "a pause that claims to have stopped a run that is still going is the bug this route avoids",
    )
    check(
        "cancel still fences, revokes, contains and reports what it left unresolved",
        all(token in cancel_route for token in (
            "revoke_by_lifecycle_event",
            "contain_by_scope_async",
            "fence_origin",
            '"unresolved_action_ids": list(outcome.unresolved_action_ids),',
            '"contained_processes": len(cancel_reports),',
        )),
        "the pane reads those three fields, so the route must keep returning them",
    )
    check(
        "resume still validates the checkpoint, the effects and the budget before it prepares",
        "prepare_resume(" in resume_route and '"ready_to_resume"' in resume_route and '"next_step"' in resume_route,
        resume_route[:200],
    )
    check(
        "containment and the recovery memory still reach the pane through /task",
        '"recovery": _recovery_summary_or_none(session_id)' in task_route
        and '"containment": _containment_summary_or_none(session_id)' in task_route,
        task_route[:200],
    )
    check(
        "the state route still publishes the task status and its transitions",
        '"task_status": task.state' in state_route and '"transitions"' in state_route,
        state_route[:200],
    )
    check(
        "containment still quarantines and reports what it contained",
        "quarantine_session" in contain_route and "persist_barrier_hold" in contain_route,
        "the surface's blocked story reads the holds this route raises",
    )
    check(
        "no route was added for recovery",
        not [path for path in mounted if "recovery" in path or "recover" in path] and bool(abort_route),
        ", ".join(path for path in mounted if "recovery" in path or "recover" in path),
    )
    check(
        "every route the pane calls is mounted by the server",
        all(any(route_matches(path, route) for route in mounted) for path in called),
        ", ".join(path for path in called if not any(route_matches(path, route) for route in mounted)),
    )
    check(
        "the surface posts to no route outside pause, cancel, resume and abort",
        all(
            re.fullmatch(r"/api/harness/(abort/\{\}|sessions/\{\}/(pause|cancel|resume))", path)
            for path in called
            if path.startswith("/api/harness/") and path.split("/")[-1] in {"pause", "cancel", "resume", "contain"}
        ),
        ", ".join(
            path for path in called
            if path.startswith("/api/harness/") and path.split("/")[-1] in {"pause", "cancel", "resume", "contain"}
        ),
    )

    findings = [{"check": entry["check"], "detail": entry["detail"]} for entry in checks if not entry["ok"]]

    payload = {
        "schema_version": "m5-uiux-harness-recovery/1",
        "document_type": "uiux_story_inventory",
        "program": "M5 2.0 UI/UX",
        "story": "UIUX-P3-05",
        "title": "Harness pause cancel resume and crash recovery UX",
        "generator": "scripts/uiux_harness_recovery_inventory.py",
        "regenerate_with": "python3 scripts/uiux_harness_recovery_inventory.py",
        "check_with": "python3 scripts/uiux_harness_recovery_inventory.py --check",
        "statement": (
            "A stopped run is a state with a cause, a checkpoint and a way forward, and it is drawn "
            "from the record the server already keeps: GET …/state and GET …/task. The four routes "
            "that answer it - pause, cancel, resume and abort - are pressable from the pane, and a "
            "refusal is a disabled button carrying the reason the runtime would give."
        ),
        "functions": {name: bool(function_body(html, name)) for name in FUNCTIONS},
        "surface": {
            "panel": panel,
            "story_host": story_host,
            "facts_host": facts_host,
            "note": note,
            "reads": ["GET /api/harness/sessions/{session_id}/state", "GET /api/harness/sessions/{session_id}/task"],
        },
        "states": [
            "paused at a safe boundary",
            "failed, with the requirement for changed evidence",
            "blocked on unresolved effects, resume refused",
            "cancelled, with no resume offered",
            "running and owned by this pane, pause offered",
            "running and not owned by this pane, drawn as a possibly dead worker",
            "a resume the runtime accepted, drawn as prepared and not as progress",
            "nothing to recover, drawn as nothing",
        ],
        "actions": [
            {"id": "pause", "label": "Pause at a safe boundary", "route": "POST …/pause", "notes": "a deferred pause says so"},
            {"id": "resume", "label": "Resume from the checkpoint", "route": "POST …/resume", "notes": "a refusal is printed as the server sent it"},
            {"id": "continue", "label": "Continue the run from step N", "route": "POST /api/harness/run with resume=true", "notes": "only after an accepted resume"},
            {"id": "cancel", "label": "Cancel this task", "route": "POST …/cancel", "notes": "two presses; reports what it fenced"},
            {"id": "abort", "label": "Abort the run and stop following", "route": "POST /api/harness/abort/{session_id}", "notes": "ends the view, not the work"},
            {"id": "fresh", "label": "Start a new session", "route": "none", "notes": "the only way forward from a cancelled task"},
        ],
        "refusals": list(REFUSAL_TOKENS),
        "events": {
            "turn/paused": "what stopping at a safe boundary did",
            "turn/cancelled": "the reason, the processes contained and the actions left unresolved",
            "turn/resume_prepared": "the step the next turn continues from, and that nothing has run",
        },
        "routes": [
            "POST /api/harness/sessions/{session_id}/pause",
            "POST /api/harness/sessions/{session_id}/cancel",
            "POST /api/harness/sessions/{session_id}/resume",
            "POST /api/harness/abort/{session_id}",
            "GET /api/harness/sessions/{session_id}/state",
            "GET /api/harness/sessions/{session_id}/task",
        ],
        "checks": checks,
        "limits": [
            "the surface draws the record; it cannot tell a live worker from a dead one, and it says "
            "so instead of guessing. The product's own `recover_incomplete_transitions()` settles that "
            "question at startup, and the surface draws its answer (`recovered_incomplete_transition`)",
            "a resume prepares a run and consumes a recovery slot; it starts nothing. The pane says "
            "`nothing has run yet` and only the next `/run` continues the checkpoint",
            "`Contain` has no control in this pane: it is an incident response that quarantines a "
            "session and revokes its grants, and an operator-facing button for it would be a one-press "
            "quarantine. Its effect is drawn - the holds it raises block a resume, and the surface says so",
            "the pending-approval case is P3-03's surface: a pause rejects pending approvals, and the "
            "panel states that rather than redrawing the approval cards",
            "the demo has no model runtime, so the walk cannot press Continue without a provider; the "
            "request the pane would send is read instead, and the walk says it did that",
        ],
        "totals": {
            "checks": len(checks),
            "states": 8,
            "actions": 6,
            "refusals": len(REFUSAL_TOKENS),
            "preserved_routes": len(PRESERVED_ROUTES),
        },
        "findings": findings,
    }
    return payload


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    return "\n".join(
        [
            "# Pause, cancel, resume, and the morning after (UIUX-P3-05)",
            "",
            "> Generated by `scripts/uiux_harness_recovery_inventory.py`. Do not edit by hand: change "
            "the pane and regenerate.",
            "",
            payload["statement"],
            "",
            "## What the pane could not do",
            "",
            "`POST …/pause`, `…/cancel`, `…/resume` and `…/contain` were built for the moment a run "
            "stops, and the pane reached none of them. It had one control, Abort, which ends the "
            "pane's view rather than the work. A paused run was a mono line, a cancelled run was a "
            "mono line, a prepared resume was a mono line, and a worker that died left the task "
            "saying `running` with nothing said about it at all.",
            "",
            "## The surface",
            "",
            "`#harnessRecoveryPanel` sits under the session strip and above the durable log. It is one "
            "presentation - so it cannot be drawn without a cause and a `Kept:` line - over a facts "
            "list and a live note:",
            "",
            "```",
            "⚠ Paused at a safe boundary",
            "  Cause: it stopped between steps: the operator asked the run to stop at the next safe",
            "         boundary (operator_pause_requested). Nothing will move until something asks it to.",
            "  Kept:  step 3 (seq 1, interrupted) is where a resume restarts, and the log above holds",
            "         every step taken before it.",
            "  [Resume from the checkpoint] [Cancel this task]",
            "  Task              paused · epoch 0 · updated 07:12 AM",
            "  Latest checkpoint seq 1 · step 3 · interrupted · 07:12 AM",
            "  Recovery budget   0/3 retries · 0/3 replans · 0/3 rollbacks",
            "  Task budget       0/25 steps · 0/3 recoveries",
            "  Containment       0 in flight · 0 barrier(s) · 0 fenced origin(s)",
            "```",
            "",
            "Two routes feed it, both older than this story: `GET …/state` for the task status, the "
            "transitions and the checkpoints, and `GET …/task` for the recovery memory, the "
            "containment store and the budget. A field the record does not have is drawn as missing "
            "(`none recorded — nothing here can be resumed`), never guessed.",
            "",
            "## The states",
            "",
            "| State | What the surface says |",
            "|---|---|",
            "| `running`, this pane owns it | a pause lands at the next safe boundary; nothing is interrupted mid-action |",
            "| `running`, this pane does not own it | the record cannot tell a run in another tab from a worker that died, and neither can the pane |",
            "| `paused` | the transition's own reason, the checkpoint a resume restarts from, and the two presses that exist |",
            "| `failed` | the failure as the runtime recorded it, and that a resume needs changed evidence |",
            "| `blocked` | the unresolved effect by id, resume refused, and a cancel that turns it into a barrier |",
            "| `cancelled` | what was fenced and revoked, and that no resume exists - a new session is the way forward |",
            "| resume accepted | prepared at step N, `nothing has run yet`, and a control that continues it |",
            "| nothing to recover | no surface at all |",
            "",
            "## The actions",
            "",
            "| Action | Route | What it costs |",
            "|---|---|---|",
            "| Pause at a safe boundary | `POST …/pause` | a deferred pause says it is deferred; pending approvals are rejected |",
            "| Resume from the checkpoint | `POST …/resume` | one recovery slot, and a 409 if the record says no |",
            "| Continue the run from step N | `/api/harness/run` with `resume: true` | only after an accepted resume |",
            "| Cancel this task | `POST …/cancel` | fences the lease, revokes grants, contains processes; irreversible |",
            "| Abort the run and stop following | `POST /api/harness/abort/{session_id}` | the view only; the record and the task are untouched |",
            "| Start a new session | none | the only way forward from a cancelled task |",
            "",
            "Cancel takes two presses, and any other action disarms it: a confirmation that outlives "
            "the reader's attention is a one-press cancel wearing a two-press label.",
            "",
            "## A refusal is a state, not a surprise",
            "",
            "Every reason the runtime would reject a resume is computed from the same record and "
            "stated before the press - the task state, a failed integrity check, unresolved "
            "consequential actions, a persisted barrier, a pending cancellation, an exhausted "
            "budget, a quarantine, unreadable recovery memory. The action stays visible and "
            "disabled, with the reason as its accessible description and the same reason in text:",
            "",
            "```",
            "Not available here — Resume from the checkpoint: the task is blocked on effects the",
            "runtime cannot account for; 1 persisted barrier(s) hold this workspace, so it cannot be",
            "taken exclusively; 1 consequential action(s) were left unresolved (act-seed-…): they must",
            "be reconciled first.",
            "```",
            "",
            "When a press does reach the server and comes back refused, the pane prints the answer it "
            "got - the string `detail` a rejected transition carries, or the `{code, message}` a "
            "rejected handoff carries - instead of reverting silently.",
            "",
            "## The events, where they happened",
            "",
            "`turn/paused`, `turn/cancelled` and `turn/resume_prepared` were three one-line renderers "
            "that stated a reason and nothing else. They are one card now, saying what the event did "
            "(`0 in-flight process(es) were contained`, `2 consequential action(s) were left "
            "unresolved …`) and linking back to the surface that keeps saying it after the log "
            "scrolls past.",
            "",
            "## The continuation the pane could not send",
            "",
            "`/api/harness/run` accepts `resume`, and the pane never sent it - so a resume that had "
            "been accepted by the runtime was followed by a turn that started over from step 1. The "
            "request now carries the flag when a resume is prepared, and consumes the preparation "
            "in the same request.",
            "",
            "## What this story had to preserve",
            "",
            "| Preserved rule | Where it still lives |",
            "|---|---|",
            "| a pause defers to a safe boundary and says which it is | `api_pause_harness_session` |",
            "| a cancel fences the lease, revokes grants, contains processes and reports what stayed unresolved | `api_cancel_harness_session` |",
            "| a resume validates the checkpoint, the unresolved effects and the budget before preparing | `api_resume_harness_session` |",
            "| containment quarantines and raises the holds a resume reads | `api_contain_harness_session` |",
            "| `/task` still publishes the recovery memory and the containment summary | `api_get_harness_task_state` |",
            "| `/state` still publishes the task status and the transition log | `api_get_harness_session_state` |",
            "",
            "No endpoint was added: the surface posts only to pause, cancel, resume and the abort that "
            "already existed, and the inventory fails if a recovery route appears that the server "
            "does not mount.",
            "",
            "## Checks and limits",
            "",
            f"`--check` re-derives {totals['checks']} checks from the source - the markup, the "
            f"{totals['actions']} actions, the {totals['refusals']} refusals, the three event cards and "
            f"the {totals['preserved_routes']} server contracts - and fails on any of them. "
            "`node scripts/harness_recovery_walk.mjs` presses the same controls in a real browser "
            "against `scripts/harness_recovery_seed.py`, which drives the state machine into four "
            "stopped states and then answers with the real routes; it photographs seven states in "
            "[`media/harness-recovery/`](media/harness-recovery/README.md).",
            "",
        ]
        + [f"- {limit}." for limit in payload["limits"]]
        + [
            "",
            "## Regenerating",
            "",
            "1. Change the pane in `static/index.html`.",
            "2. `python3 scripts/uiux_harness_recovery_inventory.py`.",
            "3. `node scripts/harness_recovery_walk.mjs --screenshots docs/media/harness-recovery`.",
            "",
        ]
    )


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
            print("error: the Harness recovery inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_harness_recovery_inventory.py")
            return 1
        if payload["findings"]:
            print("error: how a stopped run is stated disagrees with the source:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"harness recovery OK: {payload['totals']['checks']} checks, "
            f"{payload['totals']['states']} states, {payload['totals']['refusals']} refusals, 0 findings"
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
