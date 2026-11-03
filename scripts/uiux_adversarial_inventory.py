#!/usr/bin/env python3
"""Inventory the refusals a write can meet and check that each one is drawn as a refusal (UIUX-P6-02).

    python3 scripts/uiux_adversarial_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_adversarial_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_adversarial_inventory.py --json     # print the inventory

A refusal is the server's answer, and the panes used to draw their success path over it. The
worst case was not subtle: `applyHarnessWorktree()` read the response body and never the status,
so a session whose policy is read-only `plan` - the server answering **403 Cannot apply worktree
changes while session policy is read-only 'plan'.** - printed **"Applied!"** in the ok tone. The
same shape appeared nine more times: a compaction on a session that no longer exists said
"Compaction checked", a refused delete cleared the pane, a refused Council message had already
been taken out of the composer, and an `if (r.ok) { … }` answered five MCP and OpenContext
writes with silence.

This script inventories the two halves and checks they still meet:

* **the refusals** - every status the security boundary and the handlers can answer a write with,
  the server's own `detail` for it (checked verbatim against `app.py`, because a UI that invents
  a reason is worse than one that shows none), the panes that can meet it, what the UI promises
  to keep, and the proof that both halves still hold: a pytest node for the server contract and a
  browser check for the drawing;
* **the writes** - every `fetch` in the shell that changes state, and how its refusal is handled.
  A write is *guarded* when its handler calls `guardedWrite()`, *reports* when its handler reads
  the status and carries the server's own reason, and a *finding* when it does neither.

The rule the artifact states and the tests enforce: a write is never allowed to look successful
when the server refused it, and a refusal never shows a reason the server did not give.
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
WALK = PROJECT_ROOT / "scripts" / "adversarial_walk.mjs"
TESTS = PROJECT_ROOT / "tests"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Adversarial.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-adversarial.md"

#: `app.py`'s own words for the refusals the boundary emits before a handler runs. The
#: inventory checks each string is still in the source, so a UI contract cannot drift from the
#: answer it is describing. Where the detail is built from context (a missing session id, a
#: policy name) the row carries the pattern the source must still match instead.
REFUSALS = [
    {
        "status": 400,
        "when": "the Host header is not in STUDIO_ALLOWED_HOSTS",
        "detail": "Host is not permitted; configure STUDIO_ALLOWED_HOSTS explicitly.",
        "source": "app.py security_boundary (host allowlist)",
        "reachable": "server-only: a browser that reached the page passed this check already",
        "panes": ["shell"],
        "kept": "nothing was read or written; the page is not this host",
        "contract": "not drawn in a pane - the request never reached a handler, and the browser that could see it is not on an allowed host",
        "proof": {"kind": "pytest", "node": "tests/test_remote_protections.py::test_disallowed_host_header_is_rejected_even_without_auth"},
    },
    {
        "status": 401,
        "when": "the bearer token is missing or wrong",
        "detail": "Authentication required. Send `Authorization: Bearer <STUDIO_API_TOKEN>`.",
        "source": "app.py security_boundary (bearer check)",
        "reachable": "real: the walk swaps the browser's token for a wrong one",
        "panes": ["Harness", "Council", "Chat", "Pipeline", "Compare", "Start"],
        "kept": "the pane keeps its state and the reader's text; nothing was sent",
        "contract": "error presentation naming 401, the boundary's detail as the cause, a Kept: line, and a 'Re-enter the token' action that keeps the pane",
        "proof": {"kind": "walk", "check": "a wrong token is reported as 401 with the boundary's own detail and a way to re-enter it"},
    },
    {
        "status": 401,
        "when": "a stream is refused before it opens (/api/chat/stream and the other SSE routes)",
        "detail": "Authentication required. Send `Authorization: Bearer <STUDIO_API_TOKEN>`.",
        "source": "app.py security_boundary, read by consumeSSE()",
        "reachable": "real: the walk sends from a browser whose token is wrong",
        "panes": ["Chat", "Pipeline", "Compare", "Council"],
        "kept": "the message, the transcript and the partial reply are kept",
        "contract": "the pane states the server's reason instead of `HTTP 401`",
        "proof": {"kind": "walk", "check": "a refused chat stream shows the server's reason, not \"HTTP 401\""},
    },
    {
        "status": 403,
        "when": "a state-changing request carries a cross-origin Origin header",
        "detail": "Cross-origin state-changing request rejected.",
        "source": "app.py security_boundary (origin check)",
        "reachable": "injected: a page cannot forge Origin, so the walk replaces the response at the network layer with this text",
        "panes": ["Council", "Harness"],
        "kept": "the spawn dialog stays open with the request text; the round is not created",
        "contract": "error presentation naming 403 with the server's detail, and the reader's text still in the control",
        "proof": {"kind": "walk", "check": "an injected cross-origin 403 on the Council spawn keeps the modal open with the reader's text"},
    },
    {
        "status": 403,
        "when": "a worktree apply is asked of a session whose policy is read-only 'plan'",
        "detail": "Cannot apply worktree changes while session policy is read-only 'plan'.",
        "source": "app.py _apply_worktree_to_main (PermissionError) -> 403",
        "reachable": "real: the walk creates a plan-policy session and presses Apply",
        "panes": ["Harness"],
        "kept": "the session, its worktree and the main workspace are unchanged; the diff stays open",
        "contract": "the refusal is drawn inside the diff dialog where the button is, and nothing claims the apply happened",
        "proof": {"kind": "walk", "check": "a read-only session's refused apply is drawn with the server's own reason, not as \"Applied!\""},
    },
    {
        "status": 403,
        "when": "an approval decision carries a session id that did not raise the request",
        "detail": "Approval is scoped to its originating session; session_id must match.",
        "source": "app.py api_approve_harness_tool",
        "reachable": "server-only: the pane sends its own session id, so a browser cannot make this without forging the body",
        "panes": ["Harness"],
        "kept": "the pending approval is still pending and no decision was recorded",
        "contract": "not drawn: the pane cannot produce the condition, and the approval card's preflight rejects a stale slot before it posts",
        "proof": {"kind": "pytest", "node": "tests/test_security_and_quality.py::test_approve_endpoint_rejects_cross_session_submission"},
    },
    {
        "status": 404,
        "when": "the session or artifact the write names is no longer on the server",
        "detail": None,
        "detail_pattern": r"Harness session '\{session_id\}' not found\.",
        "source": "app.py get_harness_session_db -> KeyError -> 404 in apply, compact, patch, delete",
        "reachable": "real: the walk deletes a session underneath the open pane and presses Compact",
        "panes": ["Harness", "Council", "Pipeline", "Compare", "Start"],
        "kept": "the pane keeps what it had; the server's message names what is gone",
        "contract": "recovery presentation naming 404, the server's message as the cause, and a Reload from the server action",
        "proof": {"kind": "walk", "check": "a compact on a session the server no longer has says 404 and keeps the reader's place"},
    },
    {
        "status": 409,
        "when": "an approval id is decided twice, or a worktree apply conflicts",
        "detail": "Approval already resolved; re-submission (replay) rejected.",
        "source": "app.py api_approve_harness_tool (replay) and _apply_worktree_to_main (conflict)",
        "reachable": "server-only for the approval (the card disables itself); the apply conflict is a state the walk does not manufacture",
        "panes": ["Harness"],
        "kept": "the first decision stands; the second changed nothing",
        "contract": "recovery presentation naming 409 with the server's detail and a Reload from the server action",
        "proof": {"kind": "pytest", "node": "tests/test_security_and_quality.py::test_approve_endpoint_rejects_replay_of_resolved_id"},
    },
    {
        "status": 413,
        "when": "the request body is over STUDIO_MAX_REQUEST_BYTES",
        "detail": "Request body exceeds the configured size limit.",
        "source": "app.py security_boundary (size check)",
        "reachable": "injected: the walk replaces the response with this text rather than sending megabytes",
        "panes": ["Harness", "Council", "Start", "Chat"],
        "kept": "the pane and the session are unchanged; the request was not partially applied",
        "contract": "recovery presentation naming 413 and the server's detail, and no success toast",
        "proof": {"kind": "walk", "check": "an injected 413 on compaction is drawn as over the size limit, not as \"Compaction checked\""},
    },
    {
        "status": 429,
        "when": "the per-minute window for an expensive route is full",
        "detail": "Rate limit exceeded for expensive workflow requests.",
        "source": "app.py security_boundary (rate window) with a Retry-After header",
        "reachable": "real: the walk fills the window with cheap 400s and then sends",
        "panes": ["Chat", "Council", "Compare", "Pipeline", "Harness"],
        "kept": "the message and the transcript are kept; nothing was sent",
        "contract": "the pane states the server's Retry-After as a countdown, and the Retry control is disabled while the window is closed",
        "proof": {"kind": "walk", "check": "a full rate window is reported with the server's Retry-After as a countdown"},
    },
    {
        "status": 503,
        "when": "no token is configured, so the boundary cannot authenticate anything",
        "detail": "Authentication is unavailable; configure a token and restart Studio.",
        "source": "app.py security_boundary (token present?)",
        "reachable": "injected: the demo always configures a token, so the walk replaces the response with this text",
        "panes": ["Harness"],
        "kept": "nothing was written; the server needs an operator, not a retry",
        "contract": "a presentation naming 503 with the server's detail; no retry that would fail the same way",
        "proof": {"kind": "walk", "check": "an injected 503 is drawn as an error with the server's own detail"},
    },
    {
        "status": 507,
        "when": "a snapshot or apply cannot write because the disk is full",
        "detail": None,
        "detail_pattern": r"SnapshotDiskFullError",
        "source": "app.py apply/create handlers -> 507",
        "reachable": "injected: the walk replaces the response with a disk-full detail",
        "panes": ["Harness", "Start"],
        "kept": "the workspace and the session are unchanged; the diff stays open",
        "contract": "recovery presentation naming 507 with the server's detail and a Reload action",
        "proof": {"kind": "walk", "check": "an injected 507 says the server has no room to write and is drawn as a recovery"},
    },
]

#: Writes whose refusal is answered by the function that made them, with the reason the
#: inventory accepts instead of `guardedWrite`. Everything else must be guarded.
REPORTING_WRITES = {
    "resolveHarnessApproval": "re-checks the live slot, then resyncs the session on a refusal and prints the server's detail",
    "revokeHarnessGrant": "renders the refusal in the grants panel and keeps the grant listed",
    "abortHarnessRun": "states that the server did not confirm the abort and refreshes the recovery panel",
    "harnessRecoveryPost": "returns {ok, status, data} to its caller and never claims a resume it did not get",
    "saveProvider": "toasts the server's detail and leaves the form filled",
    "saveCombo": "toasts the server's detail and leaves the form filled",
    "installSkill": "throws the server's detail into the skill panel's error line",
    "fetchRemoteModels": "toasts the server's detail and keeps the typed endpoint",
    "saveWorkspaceDirectory": "returns {ok, message} so Start can present a failure with a cause",
    "saveMcpServer": "toasts the server's detail and keeps the form",
    "importMcpJson": "toasts the server's detail and keeps the pasted JSON",
    "syncMcpServer": "toasts the server's detail and re-reads the server list",
    "runMcpPlayground": "prints the server's detail in the output box",
    "saveOpenContextDoc": "toasts the server's detail and keeps the editor contents",
    "consumeSSE": "reads the refusal's own detail and Retry-After before it opens the stream, so the pane shows the server's reason and the wait instead of `HTTP <status>`",
}

MUTATING = re.compile(r"method:\s*(?:[\"'](?:POST|PUT|PATCH|DELETE)[\"']|`[^`]*\$\{[^}]*\}[^`]*`)")
FUNCTION = re.compile(r"function\s+([A-Za-z0-9_]+)\s*\(")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_sites(html: str) -> list[dict[str, object]]:
    """Every `fetch()` in the shell that changes state, with the function that makes it.

    The call is read with a balanced-paren scan rather than a line regex: a body, a header
    object and a template URL each span lines, and a line-based scan misses the ones that do.
    """
    sites: list[dict[str, object]] = []
    for match in re.finditer(r"fetch\(", html):
        start = match.start()
        index = match.end()
        depth = 1
        while index < len(html) and depth:
            char = html[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char in "\"'`":
                quote = char
                index += 1
                while index < len(html) and html[index] != quote:
                    if html[index] == "\\":
                        index += 1
                    index += 1
            index += 1
        call = html[start:index]
        if not MUTATING.search(call):
            continue
        owners = list(FUNCTION.finditer(html[:start]))
        owner = owners[-1].group(1) if owners else "(top level)"
        line = html[:start].count("\n") + 1
        sites.append({"line": line, "function": owner, "call": re.sub(r"\s+", " ", call)[:140]})
    return sites


def function_body(html: str, name: str) -> str:
    """The source of one top-level function, from its `function` keyword to its closing brace."""
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", html)
    if not match:
        return ""
    brace = html.index("{", match.end())
    depth = 0
    index = brace
    while index < len(html):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[match.start():index + 1]
        elif char in "\"'`":
            quote = char
            index += 1
            while index < len(html) and html[index] != quote:
                if html[index] == "\\":
                    index += 1
                index += 1
        index += 1
    return html[match.start():]


def classify(sites: list[dict[str, object]], html: str) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Sort every write into guarded / reports / unclassified, and say why."""
    rows: list[dict[str, object]] = []
    findings: list[dict[str, str]] = []
    for site in sites:
        body = function_body(html, str(site["function"]))
        guarded = "guardedWrite(" in body
        reports = (not guarded) and bool(re.search(r"\.ok|\.status|status_code", body)) and ("detail" in body or "message" in body)
        if guarded:
            kind = "guarded"
            note = "calls guardedWrite(), which reads the status before the body and draws the refusal"
        elif reports:
            kind = "reports"
            note = REPORTING_WRITES.get(str(site["function"]), "")
            if not note:
                findings.append(
                    {
                        "check": "unstated-refusal",
                        "detail": f"`{site['function']}` reads the status and the server's detail but the inventory has no line saying how it answers a refusal",
                    }
                )
            elif "host" in body or "presentation" in body:
                note += "; draws or announces it in the pane"
        else:
            kind = "unclassified"
            findings.append(
                {
                    "check": "silent-write",
                    "detail": f"`{site['function']}` (`static/index.html:{site['line']}`) writes and never reads the status: a refusal looks like a write that landed",
                }
            )
            note = "no status check"
        rows.append({
            "function": site["function"],
            "line": site["line"],
            "call": site["call"],
            "kind": kind,
            "note": note,
        })
    return rows, findings


def build_inventory() -> dict[str, object]:
    html = _text(FRONTEND)
    app_source = _text(APP)
    walk_source = _text(WALK)
    findings: list[dict[str, str]] = []

    rows = []
    for refusal in REFUSALS:
        row = dict(refusal)
        detail = row.get("detail")
        if detail:
            if detail not in app_source:
                findings.append(
                    {
                        "check": "invented-detail",
                        "detail": f"the {row['status']} contract quotes `{detail}`, which `app.py` no longer answers",
                    }
                )
        else:
            pattern = row.get("detail_pattern")
            if not pattern or not re.search(str(pattern), app_source):
                findings.append(
                    {
                        "check": "invented-detail",
                        "detail": f"the {row['status']} contract names a pattern `app.py` no longer matches",
                    }
                )
        proof = row["proof"]
        if proof["kind"] == "walk":
            # A check title lives inside a JavaScript string, so its inner quotes are escaped
            # there and literal here. Compare the words, not the escaping.
            if proof["check"] not in walk_source.replace('\\"', '"'):
                findings.append(
                    {
                        "check": "missing-proof",
                        "detail": f"`scripts/adversarial_walk.mjs` no longer asserts `{proof['check']}`",
                    }
                )
            else:
                row["proof_summary"] = "scripts/adversarial_walk.mjs"
        else:
            path, _, name = str(proof["node"]).partition("::")
            test_file = PROJECT_ROOT / path
            if not test_file.is_file() or f"def {name}(" not in _text(test_file):
                findings.append(
                    {
                        "check": "missing-proof",
                        "detail": f"`{proof['node']}` does not exist, so the {row['status']} contract is unproved",
                    }
                )
            else:
                row["proof_summary"] = str(proof["node"])
        rows.append(row)

    # The table the shell paints from must cover every status the boundary can answer with,
    # or a refusal arrives with the fallback words and the reader loses the distinction.
    table = dict(re.findall(r"(\d{3}):\s*\{\s*title:\s*\"([^\"]+)\"\s*\}", html))
    for row in rows:
        status = str(row["status"])
        if status not in table:
            findings.append(
                {
                    "check": "unpainted-status",
                    "detail": f"REFUSAL_WORDS has no words for {status}, which the boundary can answer with",
                }
            )
        elif row["contract"].startswith("not drawn"):
            continue
        elif row["contract"].startswith("error") and status not in {"401", "403"}:
            findings.append(
                {
                    "check": "tone-mismatch",
                    "detail": f"{status} is described as an error presentation but refuseWrite draws error only for 401/403",
                }
            )

    for required in ("guardedWrite(", "refusalReader(", "armRetryCountdown("):
        if required not in html:
            findings.append({"check": "missing-helper", "detail": f"`{required}` is no longer in the shell"})
    if "Retry-After" not in html or "retry-after" not in html.lower():
        findings.append({"check": "ignored-wait", "detail": "the shell no longer reads the Retry-After header"})

    writes, write_findings = classify(write_sites(html), html)
    findings.extend(write_findings)

    guarded = [row for row in writes if row["kind"] == "guarded"]
    reporting = [row for row in writes if row["kind"] == "reports"]
    # Every host a refusal is drawn into: the ones `refuseWrite` is pointed at, plus the chat's
    # own error surface, which predates the helper and answers a stream refusal through it.
    hosts = set(re.findall(r'host:\s*"([A-Za-z0-9_]+)"', html))
    hosts |= set(re.findall(r'presentInto\("([A-Za-z0-9_]+)"', function_body(html, "presentChatError")))
    hosts = sorted(hosts)
    for host in hosts:
        if f'id="{host}"' not in html:
            findings.append({"check": "missing-host", "detail": f"a refusal is drawn into `#{host}`, which the markup does not have"})

    return {
        "schema_version": "1.0.0",
        "document_type": "m5_uiux_adversarial_refusals",
        "program": "M5 2.0 UI/UX transformation",
        "story": "UIUX-P6-02",
        "generated_by": "scripts/uiux_adversarial_inventory.py",
        "regenerate_with": "python3 scripts/uiux_adversarial_inventory.py",
        "check_with": "python3 scripts/uiux_adversarial_inventory.py --check",
        "statement": (
            "Every refusal the server can answer a write with, the server's own detail for it, what the pane "
            "promises to keep, and the walk check or pytest node that proves both halves. A write that does "
            "not read the status is a finding, and so is a refusal whose reason the UI invented."
        ),
        "totals": {
            "refusals": len(rows),
            "statuses": len(sorted({row["status"] for row in rows})),
            "real": sum(1 for row in rows if str(row["reachable"]).startswith("real")),
            "injected": sum(1 for row in rows if str(row["reachable"]).startswith("injected")),
            "server_only": sum(1 for row in rows if str(row["reachable"]).startswith("server-only")),
            "mutating_writes": len(writes),
            "guarded_writes": len(guarded),
            "reporting_writes": len(reporting),
            "presentation_hosts": len(hosts),
        },
        "refusals": rows,
        "writes": writes,
        "presentation_hosts": hosts,
        "findings": findings,
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    lines = [
        "# Adversarial permission and recovery UI — the refusals a write can meet",
        "",
        "Generated by [`scripts/uiux_adversarial_inventory.py`](../scripts/uiux_adversarial_inventory.py)",
        "for `UIUX-P6-02`; the checked artifact is",
        "[`product/M5_2_0_UIUX_Adversarial.json`](../product/M5_2_0_UIUX_Adversarial.json). Do not hand-edit.",
        "",
        "A refusal is the server's answer. The panes used to draw their success path over it: an apply the",
        "server had refused with **403** *Cannot apply worktree changes while session policy is read-only",
        "'plan'.* printed **\"Applied!\"** in the ok tone, a compaction on a session that no longer existed said",
        "*Compaction checked*, a refused delete cleared the pane, and five MCP/OpenContext writes answered a",
        "refusal with `if (r.ok) { … }` and no word at all.",
        "",
        "This page is the map of what replaced that: one reader (`refusalReader`), one table of words",
        "(`REFUSAL_WORDS`), one shape (`refuseWrite` → the shared presentation builder, so a refusal cannot",
        "ship without a cause, a *Kept:* line and a way forward), and one guarded write (`guardedWrite`) that",
        "returns `null` on a refusal so no caller can fall through to its success path.",
        "",
        f"**{totals['refusals']} refusals across {totals['statuses']} statuses** — {totals['real']} the browser",
        f"provokes for real, {totals['injected']} injected at the network layer with `app.py`'s own text, and",
        f"{totals['server_only']} that only a server-side caller can produce. **{totals['mutating_writes']}",
        f"mutating writes**: {totals['guarded_writes']} guarded, {totals['reporting_writes']} that read the",
        f"status and carry the server's detail themselves, and {len(payload['findings'])} findings.",
        "",
        "## The refusals",
        "",
        "| Status | When | The server's own words | How the pane answers | Proof |",
        "|---|---|---|---|---|",
    ]
    for row in payload["refusals"]:
        detail = row.get("detail") or f"`{row.get('detail_pattern')}`"
        detail = str(detail).replace("|", "\\|")
        proof = row.get("proof_summary") or ""
        lines.append(
            f"| {row['status']} | {row['when']} | {detail} | {row['contract']} | `{proof}` |"
        )
    lines += [
        "",
        "The `reachable` column is the honest part: a walk check that says it provoked a refusal must have",
        "provoked it. `real:` means the server answered that way because the walk made the condition true;",
        "`injected:` means the response was replaced at the network layer with the exact text `app.py` emits,",
        "because a page cannot forge an `Origin` header, exceed the body limit without sending megabytes, or",
        "run a server that has no token configured.",
        "",
        "## What a pane promises when it refuses",
        "",
        "* the cause is the server's `detail` (or `message`) **verbatim** — a refusal with no JSON body says so",
        "  rather than inventing one;",
        "* the *Kept:* line names what the refused request did not change, and for the Council it is the",
        "  reader's own text, put back in the composer;",
        "* the way forward is a real control: *Re-enter the token* for a 401, *Retry* for a transient one,",
        "  *Reload from the server* for a 404/409, and for a 429 a **Retry in Ns** control that is disabled",
        "  until the server's own `Retry-After` has passed;",
        "* a success clears the box — a stale refusal left above a write that has since landed states a fact",
        "  the server already contradicted.",
        "",
        "## The writes",
        "",
        "| Handler | Line | How its refusal is handled |",
        "|---|---|---|",
    ]
    for row in payload["writes"]:
        lines.append(f"| `{row['function']}` | {row['line']} | {row['kind']} — {row['note']} |")
    lines += [
        "",
        "`guarded` writes call `guardedWrite()`; `reports` writes read the status and carry the server's own",
        "reason in a toast or a returned value, and the inventory names each one's line; anything else is a",
        "finding, because a write that never reads the status cannot tell a refusal from a success.",
        "",
        "## Presentation hosts",
        "",
        "Every `host:` a refusal is drawn into must exist in the markup:",
        "",
    ]
    lines += [f"* `#{host}`" for host in payload["presentation_hosts"]]
    lines += [
        "",
        "## How this is re-run",
        "",
        "```",
        "node scripts/adversarial_walk.mjs                      # starts its own throwaway demo app",
        "node scripts/adversarial_walk.mjs --json               # the record a CI run reads",
        "node scripts/adversarial_walk.mjs --theme light --screenshots docs/media/adversarial",
        "python3 scripts/uiux_adversarial_inventory.py --check  # this page, against the shell",
        "```",
        "",
        "CI runs the walk through `tests/test_uiux_adversarial_browser.py`, in the browser job, which",
        "starts one isolated demo app and fails the job when any check fails or when the walk stops",
        "checking one of the refusals above. `M5_REQUIRE_BROWSER_TESTS=1` makes a missing browser a",
        "failure instead of a skip, so the job cannot pass by not running.",
        "",
        "The committed captures in `docs/media/adversarial` come from this",
        "walk on this tree, once per theme, and the walk fails the run when the page reports a theme",
        "other than the one asked for: the refused apply with its cause, *Kept:* line and both actions",
        "at 1440x900 and at 390x740, the injected 503, the rate-limited Chat send, and — for contrast —",
        "the `Applied!` toast the pre-story handler drew for the same refused write. A capture that was",
        "not taken is not listed; the published screenshot suite is untouched by this story.",
        "",
        "## What this does not claim",
        "",
        "* it does not decide whether a write is allowed — the server does, and the UI renders the answer;",
        "* the injected refusals exercise the *drawing*, not the boundary: the boundary's own contract is",
        "  proved by the pytest nodes in the table and by the security suite that already owned it;",
        "* three statuses (400 host, 403 session-scoped approval, 409 replay) are not drawable from the shell",
        "  because the pane cannot produce the condition, and the table says so instead of inventing a check.",
        "",
    ]
    if payload["findings"]:
        lines += ["## Findings", ""]
        lines += [f"* **{finding['check']}** — {finding['detail']}" for finding in payload["findings"]]
        lines.append("")
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
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
            print("error: the adversarial inventory has drifted from the shell:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_adversarial_inventory.py")
            return 1
        if payload["findings"]:
            print("error: a refusal and the way it is drawn disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        totals = payload["totals"]
        print(
            f"adversarial refusals OK: {totals['refusals']} refusals across {totals['statuses']} statuses, "
            f"{totals['guarded_writes']}/{totals['mutating_writes']} writes guarded, "
            f"{totals['presentation_hosts']} presentation hosts, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    totals = payload["totals"]
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{totals['refusals']} refusals, {totals['mutating_writes']} writes, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
