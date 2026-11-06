#!/usr/bin/env python3
"""Map the preservation matrix to the tests that prove it (UIUX-P6-01).

    python3 scripts/uiux_preservation_e2e.py            # regenerate JSON + docs
    python3 scripts/uiux_preservation_e2e.py --check    # fail if either drifted, or a row is unproven
    python3 scripts/uiux_preservation_e2e.py --json     # print the mapping

The ledger has carried a 21-row preservation matrix since `UIUX-P0-06`: for every capability the
redesign touches, what must survive. Until this story, "preserved" meant a human had read the
matrix and believed it. The matrix named rows like *Harness / approvals* and the proof lived in
`scripts/harness_permissions_walk.mjs`, but nothing in the repository connected the two - so a
row could quietly lose its test and the matrix would still say preserved.

This file is that connection, and it is checked from both ends:

* every row of the matrix has exactly one entry here, and every entry names a row that exists -
  a new function with no matrix row is drift, and a matrix row with no proof is a claim;
* a `walk` prover must name a script in `scripts/` **and** a check title that appears verbatim in
  that script. Editing a check's wording therefore breaks this inventory instead of silently
  removing the proof, which is the failure mode this file exists to catch;
* a `pytest` prover must name a node whose test function exists in that file;
* a `route` prover must name a method and a path that `app.py` still mounts - the *contract*
  half of "existing endpoint and permission semantics" - and the suite probes each one live for
  its refusal;
* a `control` prover must name an element that is still in the markup with a handler, because a
  capability whose button lost its `onclick` is not preserved no matter what the server does.

The proofs are deliberately not one per row where one will not do: a capability is a route, a
surface and a behaviour, and a row that names only the middle one is how a redesign keeps the
API and loses the button.

`scripts/preservation_suite.py` executes what this file lists - the walks, the pytest nodes and
the route probes - and prints one line per row. This file is the map; that script is the drive.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SCRIPTS = PROJECT_ROOT / "scripts"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Preservation_E2E.json"
RUN_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Preservation_Run.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-preservation-e2e.md"

STORY = "UIUX-P6-01"

# The matrix row is identified by its tab and its capability, exactly as the ledger spells them.
# `proves` says what the provers together show; a row is not preserved because a file exists.
ROWS: tuple[dict, ...] = (
    {
        "tab": "Start",
        "capability": "Guided first run",
        "proves": (
            "The step the server named is printed once, marked `aria-current`, and the action beside it "
            "is the action the server attached to that step."
        ),
        "provers": (
            ("walk", "start_walk.mjs", "the lead names the server's next step"),
            ("walk", "start_walk.mjs", "exactly one row is aria-current, and it is that step"),
        ),
    },
    {
        "tab": "Start",
        "capability": "readiness summary",
        "proves": (
            "The verdict, the count and the four machine rows are the server's own answers: the pane "
            "repeats them rather than recounting them, and a simulated payload is labelled unverified."
        ),
        "provers": (
            ("walk", "start_walk.mjs", "the lead's state is the server's verdict"),
            ("walk", "start_health_walk.mjs", "the card draws four labelled rows, each with a tone"),
        ),
    },
    {
        "tab": "Start",
        "capability": "run-first-task",
        "proves": (
            "Run is available exactly when the server says ready, is disabled with the reason in text "
            "and in its accessible name when it is not, and the launcher carries the task into the "
            "Harness composer without starting anything."
        ),
        "provers": (
            ("walk", "start_walk.mjs", "Run explains itself in words a reader and a screen reader both get"),
            ("walk", "start_recent_walk.mjs", "the launcher carries the task into the Harness composer"),
            ("walk", "start_recent_walk.mjs", "the launcher starts nothing: no session created, no stream"),
        ),
    },
    {
        "tab": "Start",
        "capability": "recheck",
        "proves": (
            "A refused preflight is presented with a cause and a Re-check that clears it and brings the "
            "list back, with focus returned to the pane rather than dropped on `<body>`."
        ),
        "provers": (
            ("walk", "start_walk.mjs", "the error presentation offers a Re-check"),
            ("walk", "start_walk.mjs", "Re-check clears the presentation and brings the list back"),
            ("walk", "start_walk.mjs", "focus returns to the pane instead of falling to <body>"),
        ),
    },
    {
        "tab": "Start",
        "capability": "provider settings",
        "proves": (
            "The providers panel still opens from the control that advertises it, and the provider "
            "routes keep their contract - the panel is a client of them, not a replacement."
        ),
        "provers": (
            ("walk", "council_readiness_walk.mjs", "the NEEDS KEY action opens the providers panel"),
            ("route", "GET", "/api/providers"),
            ("route", "POST", "/api/providers"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "Session creation",
        "proves": (
            "A session is created through the route the pane already used, the durable state it writes is "
            "the state the timeline reads back, and the new session appears in Start's recent work."
        ),
        "provers": (
            ("walk", "harness_timeline_walk.mjs", "the app creates a session, which is what writes the log this walk reads"),
            ("walk", "start_recent_walk.mjs", "a created session appears in recent work"),
            ("route", "POST", "/api/harness/sessions"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "workspace",
        "proves": (
            "One workspace setting, read by both panes: Start writes the directory through the Harness's "
            "own route and the Harness field and the isolation mode follow it."
        ),
        "provers": (
            ("walk", "harness_settings_walk.mjs", "the directory row names the project directory and the isolation mode"),
            ("walk", "start_health_walk.mjs", "setting a directory updates the row, the Harness field and the preflight"),
            ("route", "POST", "/api/workspace/directory"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "policy",
        "proves": (
            "The execution policy is one option's own label, it moves the plan when it changes, and the "
            "approval card states the policy in force rather than a written-down copy of it."
        ),
        "provers": (
            ("walk", "harness_settings_walk.mjs", "changing the policy moves the policy row"),
            ("walk", "harness_permissions_walk.mjs", "the card names the policy that is asking, and it is the policy in force"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "lead/critic",
        "proves": (
            "Both seats are chosen in the pane, named in the plan, and a change to either moves the row "
            "that states them."
        ),
        "provers": (
            ("walk", "harness_settings_walk.mjs", "the models row names the lead and the critic"),
            ("walk", "harness_settings_walk.mjs", "changing the critic moves the models row"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "streamed tool events",
        "proves": (
            "Events arrive over the stream without a reload, each draws a card, and Replay re-reads the "
            "log from its retained floor without duplicating an event."
        ),
        "provers": (
            ("walk", "harness_timeline_walk.mjs", "twelve policy changes on the API arrive in the view over the stream, with no reload"),
            ("walk", "harness_timeline_walk.mjs", "Replay re-reads the log from its first retained event and rebuilds the view from it"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "approvals",
        "proves": (
            "An approval is one card naming the tool, the arguments, the policy, the capability and the "
            "expiry; the decision is posted with the session that owns it and records exactly what the "
            "pane said it would."
        ),
        "provers": (
            ("walk", "harness_permissions_walk.mjs", "an approval arrives as one card that names the tool and shows the arguments"),
            ("walk", "harness_permissions_walk.mjs", "the decision is posted with the session that owns the approval"),
            ("route", "POST", "/api/harness/approve"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "diffs",
        "proves": (
            "The diff dialog counts the change per file with the parser the server's diff is read by, and "
            "says the diff is read now rather than frozen at a verdict."
        ),
        "provers": (
            ("walk", "harness_evidence_walk.mjs", "the diff dialog counts the change and says it is not frozen at a verdict"),
            ("walk", "harness_evidence_walk.mjs", "the change is counted per file from the same diff the server reports"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "worktree apply",
        "proves": (
            "Applying a diff still refuses the cases it always refused - uncommitted user changes, a "
            "quarantined workspace, an unjoined child, an unresolved effect - and still reports the refusal."
        ),
        "provers": (
            ("pytest", "tests/test_workspace_snapshots.py::test_worktree_apply_refuses_clobbering_uncommitted_user_changes"),
            ("pytest", "tests/test_harness_evidence.py::test_apply_still_refuses_and_reports"),
            ("walk", "harness_settings_walk.mjs", "checking the worktree box says so in the directory row"),
            ("route", "POST", "/api/harness/sessions/{session_id}/apply"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "abort",
        "proves": (
            "Abort is still the button with its handler, and the abort route still resolves the approvals "
            "a run was waiting on instead of leaving them live."
        ),
        "provers": (
            ("pytest", "tests/test_security_and_quality.py::test_abort_resolves_pending_approvals_and_tombstones_replay"),
            ("control", "harnessAbortBtn"),
            ("route", "POST", "/api/harness/abort/{session_id}"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "compaction",
        "proves": (
            "The compact route is still mounted, and the event it writes is drawn as a sentence with the "
            "characters it archived rather than as a field dump."
        ),
        "provers": (
            ("route", "POST", "/api/harness/sessions/{session_id}/compact"),
            ("walk", "harness_timeline_walk.mjs", "the row reads the event in words instead of its field names"),
        ),
    },
    {
        "tab": "Harness",
        "capability": "GSD and SDLC",
        "proves": (
            "The SDLC surface is still in the pane with its handler, and the run still has to pass "
            "verification and state its first line before it is allowed to pass."
        ),
        "provers": (
            ("pytest", "tests/test_security_and_quality.py::test_sdlc_pass_requires_successful_verification_and_explicit_first_line_pass"),
            ("control", "sdlcRunModeSelect"),
            ("route", "POST", "/api/harness/sdlc/run"),
        ),
    },
    {
        "tab": "Council",
        "capability": "Council Hall session spawning and chronicle browsing",
        "proves": (
            "A room can still be opened without invoking a seat, and a session's replies, pinned decision "
            "and dissent are still readable afterwards in the words the pane uses for them."
        ),
        "provers": (
            ("walk", "council_readiness_walk.mjs", "Open Empty Room still creates a session without invoking a seat"),
            ("walk", "council_verdict_walk.mjs", "pinning the Lead Arbiter's verdict states where the decision came from, word for word"),
            ("walk", "council_readiness_walk.mjs", "Post note only remains enabled and saves the note without summoning"),
        ),
    },
    {
        "tab": "Pipeline",
        "capability": "Four-stage orchestrator with architect/coder role selection",
        "proves": (
            "The four seats are a chain the pane states: what each seat reads, what it was allowed, and "
            "which seat it hands its output to - including the two seats the reasoning control reaches."
        ),
        "provers": (
            ("walk", "pipeline_artifacts_walk.mjs", "every stage card carries a row naming what that seat reads"),
            ("walk", "council_deliberation_walk.mjs", "the reasoning control reaches the two seats the route gives it to, and the other two say off"),
        ),
    },
    {
        "tab": "Compare",
        "capability": "Multi-model selection and compare responses",
        "proves": (
            "Two or more models still run against one question, each panel keeps its own reply and status, "
            "and every figure shown is the figure the response carried."
        ),
        "provers": (
            ("walk", "compare_criteria_walk.mjs", "two panels, one answering and one failing: the run line counts both and neither is drawn as slow"),
            ("walk", "compare_provenance_walk.mjs", "every figure on screen is the figure the response carried for that reply"),
            ("walk", "compare_history_walk.mjs", "Revisit restores separate opinions and synthesis without rerunning or changing the server copy"),
        ),
    },
    {
        "tab": "Chat",
        "capability": "Active model and persona selection",
        "proves": (
            "The selected model and persona are what the pane says they are: with no model Send is off and "
            "says why, and with one the response mode is named beside it."
        ),
        "provers": (
            ("walk", "chat_walk.mjs", "with no model the Send button is off and says why"),
            ("walk", "chat_composer_walk.mjs", "empty conversation and labelled response mode"),
        ),
    },
    {
        "tab": "Chat",
        "capability": "conversation interface",
        "proves": (
            "A message is answered by a labelled turn carrying its model and usage, the transcript is not "
            "read aloud on every token, and the context each answer was sent with is printed."
        ),
        "provers": (
            ("walk", "chat_walk.mjs", "a sent message is answered by a labelled turn with its model and its usage"),
            ("walk", "chat_walk.mjs", "the transcript is a log that does not read itself aloud on every token"),
            ("walk", "chat_context_walk.mjs", "every answer prints the context it was sent, composed by the pane's own helper"),
        ),
    },
)

# Walks that read an app somebody else started. The suite starts one throwaway demo instance for
# these; a walk that starts its own fixture is run with no arguments. The classification is a fact
# about the source - `startFixture(` - so it is checked against it rather than trusted.
NEEDS_SERVER = {
    "chat_composer_walk.mjs",
    "chat_context_walk.mjs",
    "chat_handoff_walk.mjs",
    "chat_walk.mjs",
    "compare_history_walk.mjs",
    "council_state_walk.mjs",
    "harness_settings_walk.mjs",
    "harness_timeline_walk.mjs",
    "keyboard_walk.mjs",
    "overlay_walk.mjs",
    "presentation_walk.mjs",
    "start_health_walk.mjs",
    "start_recent_walk.mjs",
    "start_walk.mjs",
    "status_walk.mjs",
    "uiux_interaction_polish_walk.mjs",
}

# Walks that exist for a story but prove no matrix row are not failures - most of them prove a
# *contract* rather than a capability - but they are listed so the gap is visible: a walk nobody
# runs in the suite is a test that decays.
SUPPORT_WALKS = {
    "a11y_walk.mjs": "the accessibility walk (P5-02) proves the same reachability from the keyboard side",
    "chat_handoff_walk.mjs": "the Chat-to-Harness handoff (P2-05) is a new journey, not a preserved capability",
    "overlay_walk.mjs": "the overlay controller (P1-04) is shared machinery the matrix does not enumerate",
    "polish_walk.mjs": "the polish layer (P5-05) proves affordances across all six panes",
    "presentation_walk.mjs": "the presentation contract (P1-05) is the shape failures are drawn in",
    "responsive_walk.mjs": "the layout walk (P5-03) proves the matrix rows still fit a phone",
    "status_walk.mjs": "the status component (P1-03) is the vocabulary every row is drawn in",
    "stream_perf_walk.mjs": "the streaming bound (P5-04) is a cost property, not a capability",
    "terminology_walk.mjs": "the vocabulary walk (P5-01) proves the same states say the same words",
    "keyboard_walk.mjs": "the rail's tab semantics (P1-02) are shell behaviour",
    "pipeline_recovery_walk.mjs": "Pipeline failure recovery (P3-07) extends a capability rather than preserving it",
    "harness_recovery_walk.mjs": "Harness pause/resume/abort recovery (P3-05) extends the abort surface rather than preserving it",
    "compare_continuity_walk.mjs": "Compare continuity (P4-06) keeps a comparison; the capability it belongs to is the comparison itself",
    "uiux_interaction_polish_walk.mjs": "the supplemental P5-05 evidence walk measures hover/focus parity and captures 72 screenshots; it proves presentation, not a capability the matrix enumerates",
    "adversarial_walk.mjs": "the adversarial walk (P6-02) proves how a refusal is drawn, which is a contract about failures rather than a capability the matrix enumerates",
    "chat_persona_walk.mjs": "the Chat persona walk (UIV2-19) proves that the picker is drawn from CHAT_PERSONAS, that a stored choice survives a reload, that a deleted persona falls back to the default, and that the request carries the registry's prompt rather than the picker's value; the capability it belongs to is Chat itself, which the matrix already enumerates from the pane's own controls",
    "mcp_hub_walk.mjs": "the MCP hub walk (UIV2-21) proves that the dialog is three tabs rather than one configuration scroll, that every preset card states its transport, trust, key requirement and approval policy before anything is connected, that search and the filter chips narrow the catalog, that a server which cannot be reached keeps the sync note it really got in both the Connected list and Activity, that a live stdio server syncs its real tools and a test call is recorded with its outcome and latency, and that a stored token never reaches the document; the capability it belongs to is the MCP surface, which the matrix already enumerates from the tool strip and the dialog's own controls",
    "chat_recovery_walk.mjs": "the Chat recovery walk (UIV2-20) proves that a copy only claims success when the text reached the clipboard, that a tool call is disclosed with its status, step, time, arguments, output and the server's cap, that a stopped turn says who stopped it and how much was kept, that a turn which already ran tools is asked again rather than retried silently, and that the handoff posts nothing; the capability it belongs to is Chat itself, which the matrix already enumerates from the pane's own controls",
    "chat_layout_walk.mjs": "the Chat layout walk (UIV2-18) proves the column, the control bar and the composer's position on a screen; the capability it belongs to is Chat itself, which the matrix already enumerates from the pane's own controls",
    "compare_evidence_walk.mjs": "the Compare evidence walk (UIV2-17) proves what the results table, the verdict card and the inspector's evidence lines say about a reply; the capability it belongs to is the comparison itself, which the matrix already enumerates from the pane's own controls",
    "compare_balanced_walk.mjs": "the Compare composition walk (UIV2-16) proves how the question, the folded criteria and the answer columns are drawn; the capability it belongs to is the comparison itself, which the matrix already enumerates from the pane's own controls",
    "council_state_walk.mjs": "the Council state walk (UIV2-15) proves how a refused, reopened, dropped or deleted turn is drawn; the capability it belongs to is the Council session itself, which the matrix already enumerates from the pane's own controls",
}


def matrix_rows() -> list[dict]:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    return ledger["feature_preservation_matrix"]


def walk_source(name: str) -> str:
    path = SCRIPTS / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def app_routes() -> set[tuple[str, str]]:
    """Every ``(METHOD, path)`` ``app.py`` mounts, read from the decorators."""
    source = APP.read_text(encoding="utf-8")
    routes = set()
    for method, path in re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', source):
        routes.add((method.upper(), path.rstrip("/") or "/"))
    return routes


def control_handled(control_id: str) -> tuple[bool, str]:
    """Is this control still in the markup, and is it still wired to something?"""
    html = FRONTEND.read_text(encoding="utf-8")
    pattern = re.compile(r"<([a-z]+)\b([^>]*\bid=\"" + re.escape(control_id) + r"\"[^>]*)>", re.I | re.S)
    match = pattern.search(html)
    if not match:
        return False, f"`#{control_id}` is no longer in the markup"
    attributes = match.group(2)
    wired = (
        "onclick=" in attributes
        or "onchange=" in attributes
        or "onsubmit=" in attributes
        or match.group(1).lower() in ("input", "select", "textarea")
    )
    if not wired:
        return False, f"`#{control_id}` is in the markup but nothing is wired to it"
    return True, f"<{match.group(1)} id=\"{control_id}\"> is present and wired"


def pytest_node_exists(node: str) -> bool:
    path, _, name = node.partition("::")
    target = PROJECT_ROOT / path
    if not target.is_file() or not name:
        return False
    return re.search(rf"^def\s+{re.escape(name)}\s*\(", target.read_text(encoding="utf-8"), re.M) is not None


def last_run() -> dict:
    """The committed record of the last suite run, if there is one.

    Reported, never asserted: a run happened on one machine at one commit, and a map that
    insisted on a fresh record would fail the moment anybody committed anything.
    """
    if not RUN_PATH.is_file():
        return {"recorded": False}
    try:
        record = json.loads(RUN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:  # pragma: no cover - a hand-edited record is not this file's job
        return {"recorded": False}
    walks = record.get("walks", [])
    return {
        "recorded": True,
        "commit": str(record.get("commit", ""))[:7],
        "seconds": record.get("seconds"),
        "totals": record.get("totals", {}),
        "known": [walk for walk in walks if walk.get("known")],
        "failed": [walk for walk in walks if not walk.get("passed") and not walk.get("known")],
        "environment": record.get("environment", {}),
    }


def build_inventory() -> dict:
    # The ledger calls the column `existing`; this file calls the same thing a capability, so the
    # rest of the code has one name for one idea.
    matrix = [{"tab": row["tab"], "capability": row["existing"]} for row in matrix_rows()]
    routes = app_routes()
    findings: list[dict] = []

    mapped = {(row["tab"], row["capability"]): row for row in ROWS}
    keys = [(row["tab"], row["capability"]) for row in ROWS]
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        findings.append({"check": "duplicate-row", "detail": f"`{key[0]} / {key[1]}` is mapped twice"})

    for row in matrix:
        key = (row["tab"], row["capability"])
        if key not in mapped:
            findings.append(
                {
                    "check": "unproven-row",
                    "detail": f"the matrix row `{key[0]} / {key[1]}` has no proof in this inventory",
                }
            )
    known = {(row["tab"], row["capability"]) for row in matrix}
    for key in mapped:
        if key not in known:
            findings.append(
                {
                    "check": "stale-row",
                    "detail": f"this inventory proves `{key[0]} / {key[1]}`, which the ledger's matrix no longer lists",
                }
            )

    checked: list[dict] = []
    pytest_nodes: list[str] = []
    walk_scripts: set[str] = set()
    route_provers: list[dict] = []
    for row in ROWS:
        provers = []
        for prover in row["provers"]:
            kind = prover[0]
            if kind == "walk":
                _, script, check = prover
                source = walk_source(script)
                if not source:
                    findings.append(
                        {"check": "missing-walk", "detail": f"`{row['tab']} / {row['capability']}` names `{script}`, which does not exist"}
                    )
                    continue
                if check not in source:
                    findings.append(
                        {
                            "check": "missing-check",
                            "detail": f"`{script}` no longer asserts `{check}` (named by `{row['tab']} / {row['capability']}`)",
                        }
                    )
                    continue
                walk_scripts.add(script)
                provers.append({"kind": "walk", "script": f"scripts/{script}", "check": check})
            elif kind == "pytest":
                node = prover[1]
                if not pytest_node_exists(node):
                    findings.append(
                        {"check": "missing-test", "detail": f"`{node}` does not exist (named by `{row['tab']} / {row['capability']}`)"}
                    )
                    continue
                pytest_nodes.append(node)
                provers.append({"kind": "pytest", "node": node})
            elif kind == "route":
                _, method, path = prover
                key = (method.upper(), path.rstrip("/") or "/")
                if key not in routes:
                    findings.append(
                        {"check": "missing-route", "detail": f"`{key[0]} {key[1]}` is not mounted (named by `{row['tab']} / {row['capability']}`)"}
                    )
                    continue
                route_provers.append({"method": key[0], "path": key[1], "row": f"{row['tab']} / {row['capability']}"})
                provers.append({"kind": "route", "method": key[0], "path": key[1]})
            elif kind == "control":
                control_id = prover[1]
                ok, detail = control_handled(control_id)
                if not ok:
                    findings.append(
                        {"check": "unwired-control", "detail": f"{detail} (named by `{row['tab']} / {row['capability']}`)"}
                    )
                    continue
                provers.append({"kind": "control", "element": control_id, "detail": detail})
            else:  # pragma: no cover - the table above is the only writer
                raise AssertionError(f"unknown prover kind {kind!r}")
        if not provers:
            findings.append(
                {"check": "no-proof", "detail": f"`{row['tab']} / {row['capability']}` ended with no working proof"}
            )
        checked.append(
            {
                "tab": row["tab"],
                "capability": row["capability"],
                "proves": row["proves"],
                "provers": provers,
                "proof_count": len(provers),
            }
        )

    for name in sorted(walk_scripts | set(SUPPORT_WALKS)):
        source = walk_source(name)
        if not source:
            continue
        starts_itself = re.search(r"(?:async\s+)?function\s+startFixture\s*\(", source) is not None
        if starts_itself and name in NEEDS_SERVER:
            findings.append(
                {
                    "check": "server-classification",
                    "detail": f"`scripts/{name}` starts its own fixture but is listed as needing the suite's server",
                }
            )
        if not starts_itself and name not in NEEDS_SERVER:
            findings.append(
                {
                    "check": "server-classification",
                    "detail": f"`scripts/{name}` does not start a fixture and is not listed as needing the suite's server",
                }
            )

    all_walks = sorted(path.name for path in SCRIPTS.glob("*_walk.mjs"))
    unlisted = [name for name in all_walks if name not in walk_scripts and name not in SUPPORT_WALKS]
    for name in unlisted:
        findings.append(
            {
                "check": "unlisted-walk",
                "detail": f"`scripts/{name}` proves no matrix row and is not listed as a support walk",
            }
        )
    for name in SUPPORT_WALKS:
        if not (SCRIPTS / name).is_file():
            findings.append({"check": "unlisted-walk", "detail": f"`scripts/{name}` is listed as a support walk but does not exist"})
        if name in walk_scripts:
            findings.append(
                {"check": "unlisted-walk", "detail": f"`scripts/{name}` is listed as support and also proves a row; pick one"}
            )

    return {
        "story": STORY,
        "generated_by": "scripts/uiux_preservation_e2e.py",
        "executed_by": "scripts/preservation_suite.py",
        "ledger": {
            "path": "product/M5_2_0_UIUX_Autonomous_Roadmap.json",
            "matrix_rows": len(matrix),
            "tabs": sorted({row["tab"] for row in matrix}),
        },
        "rows": checked,
        "totals": {
            "matrix_rows": len(matrix),
            "mapped_rows": len(checked),
            "provers": sum(row["proof_count"] for row in checked),
            "walk_provers": sum(1 for row in checked for prover in row["provers"] if prover["kind"] == "walk"),
            "pytest_provers": sum(1 for row in checked for prover in row["provers"] if prover["kind"] == "pytest"),
            "route_provers": sum(1 for row in checked for prover in row["provers"] if prover["kind"] == "route"),
            "control_provers": sum(1 for row in checked for prover in row["provers"] if prover["kind"] == "control"),
            "walks": len(walk_scripts),
            "support_walks": len(SUPPORT_WALKS),
            "findings": len(findings),
        },
        "walks": sorted(walk_scripts),
        "needs_server": sorted(NEEDS_SERVER & walk_scripts),
        "support_walks": dict(sorted(SUPPORT_WALKS.items())),
        "pytest_nodes": sorted(set(pytest_nodes)),
        "last_run": last_run(),
        "route_probes": route_provers,
        "findings": findings,
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _run_lines(payload: dict) -> list[str]:
    run = payload.get("last_run") or {"recorded": False}
    if not run.get("recorded"):
        return [
            "No run is recorded yet. `python3 scripts/preservation_suite.py --save "
            "product/M5_2_0_UIUX_Preservation_Run.json` writes one."
        ]
    totals = run.get("totals", {})
    lines = [
        f"Recorded by `scripts/preservation_suite.py --save product/M5_2_0_UIUX_Preservation_Run.json` at "
        f"`{run.get('commit', '?')}` in {run.get('seconds', '?')}s:",
        "",
        f"* {totals.get('passed_rows', 0)} of {totals.get('rows', 0)} rows proved, "
        f"{totals.get('known_failures', 0)} known defects, {totals.get('failed_steps', 0)} unexplained failures",
        f"* python {run.get('environment', {}).get('python', '?')}; "
        f"chrome `{run.get('environment', {}).get('chrome', '?')}`",
        "",
    ]
    known = run.get("known") or []
    if known:
        lines.extend(["| Walk | Tracked as | What it shows |", "|---|---|---|"])
        for walk in known:
            fix = walk["known"]
            lines.append(f"| `{walk['walk']}` | {fix['issue']} (`{fix['owner']}`) | {fix['reason']} |")
        lines.append("")
    failed = run.get("failed") or []
    if failed:
        lines.append("Unexplained failures in that run: " + ", ".join(f"`{walk['walk']}`" for walk in failed))
        lines.append("")
    lines.append(
        "A record is evidence of one run, not a badge: it names the commit and the machine it ran on, and it is "
        "replaced by the next run rather than edited."
    )
    return lines


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    RUN_LINES = _run_lines(payload)
    lines = [
        "# M5 2.0 preservation, proved rather than asserted",
        "",
        "Generated by `python3 scripts/uiux_preservation_e2e.py` — **never hand-edit this page**.",
        "It is the `UIUX-P6-01` map from the ledger's preservation matrix to the tests that hold it up,",
        "executed by [`scripts/preservation_suite.py`](../scripts/preservation_suite.py).",
        "",
        f"* {totals['mapped_rows']} of {totals['matrix_rows']} matrix rows mapped, {totals['provers']} provers, {totals['findings']} findings",
        f"* {totals['walk_provers']} walk checks, {totals['pytest_provers']} pytest nodes, "
        f"{totals['route_provers']} route probes, {totals['control_provers']} wired controls",
        f"* {totals['walks']} walks prove a row; {totals['support_walks']} more are listed with the reason they do not",
        "",
        "## What this page is, and is not",
        "",
        "The matrix has said *preserve this* since `UIUX-P0-06`. Nothing connected a row to a test, so",
        "\"preserved\" was a belief held by whoever last read both. Every row now names its proof, and the",
        "proof is checked from both ends: a row with no proof fails this inventory, and a check title that",
        "was reworded - or a test that was renamed, or a route that was removed - fails it too.",
        "",
        "This page does not say the capabilities work. `scripts/preservation_suite.py` runs the proof and",
        "prints one line per row; only its output is evidence, and only for the run it performed.",
        "",
        "## The rows",
        "",
        "| Tab | Capability | Proofs | What the proofs show |",
        "|---|---|---|---|",
    ]
    for row in payload["rows"]:
        kinds = []
        for prover in row["provers"]:
            if prover["kind"] == "walk":
                kinds.append(f"walk `{prover['script'].split('/')[-1]}`")
            elif prover["kind"] == "pytest":
                kinds.append(f"test `{prover['node'].split('::')[-1]}`")
            elif prover["kind"] == "route":
                kinds.append(f"route `{prover['method']} {prover['path']}`")
            else:
                kinds.append(f"control `#{prover['element']}`")
        lines.append(
            f"| {row['tab']} | {row['capability']} | {row['proof_count']}: {', '.join(kinds)} | {row['proves']} |"
        )
    lines.extend(
        [
            "",
            "## The pytest nodes",
            "",
        ]
    )
    for node in payload["pytest_nodes"]:
        lines.append(f"* `{node}`")
    lines.extend(
        [
            "",
            "## The routes probed",
            "",
            "Each route is probed live by the suite twice: once with no token, which must be refused, and",
            "once with the throwaway token and a session id that does not exist, which must answer rather",
            "than succeed. That is what \"existing endpoint and permission semantics\" means in practice.",
            "",
            "| Method | Route | Row |",
            "|---|---|---|",
        ]
    )
    seen = set()
    for probe in payload["route_probes"]:
        key = (probe["method"], probe["path"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"| {probe['method']} | `{probe['path']}` | {probe['row']} |")
    lines.extend(
        [
            "",
            "## Walks that prove a row",
            "",
            ", ".join(f"`{name}`" for name in payload["walks"]),
            "",
            "## Walks that prove no row, and why",
            "",
            "| Walk | Why it is not a matrix row |",
            "|---|---|",
        ]
    )
    for name, reason in payload["support_walks"].items():
        lines.append(f"| `{name}` | {reason} |")
    lines.extend(
        [
            "",
            "## The last recorded run",
            "",
            *RUN_LINES,
            "",
            "## How to run it",
            "",
            "```bash",
            "python3 scripts/preservation_suite.py --list        # the plan, and what each step proves",
            "python3 scripts/preservation_suite.py               # everything: routes, tests, walks",
            "python3 scripts/preservation_suite.py --only approvals",
            "```",
            "",
            "The suite starts one throwaway demo server for the walks that need one, gives each walk its own",
            "fixture otherwise, and exits non-zero if any proof fails or any row ends unproven.",
            "",
            "## What this does not prove",
            "",
            "* A walk that passes proves the pane answered the way the walk asked, on a demo fixture with no",
            "  model runtime. Live model behaviour is out of scope here and is covered by the runtime",
            "  program's own qualification.",
            "* The matrix rows are a 2026-10-31 reading of the product. A capability added after that date is",
            "  not in this map, so `unlisted-walk` is the only thing that will notice its test.",
            "* Nine of the twenty-one rows are proved partly by a walk that another story owns. If that story",
            "  rewrites its check, this inventory fails - which is the point, but it means a legitimate",
            "  reword is a two-file change.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map the UIUX-P6-01 preservation matrix to its proofs.")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a row is unproven")
    parser.add_argument("--json", action="store_true", help="print the mapping")
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
            print("error: the preservation map has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_preservation_e2e.py")
            return 1
        if payload["findings"]:
            print("error: a capability is no longer proved:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"preservation map OK: {payload['totals']['mapped_rows']}/{payload['totals']['matrix_rows']} rows, "
            f"{payload['totals']['provers']} provers, {payload['totals']['walks']} walks, "
            f"{payload['totals']['support_walks']} support walks, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['mapped_rows']}/{payload['totals']['matrix_rows']} rows, "
        f"{payload['totals']['provers']} provers, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
