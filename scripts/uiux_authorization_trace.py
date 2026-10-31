#!/usr/bin/env python3
"""Trace authorization and durable state for mapped browser route candidates (UIUX-P0-04).

    python3 scripts/uiux_authorization_trace.py            # regenerate JSON + page
    python3 scripts/uiux_authorization_trace.py --check    # fail if either drifted
    python3 scripts/uiux_authorization_trace.py --json     # print the trace

`UIUX-P0-03` provides a lexical map of browser route candidates. For each
state-changing candidate, this asks **what can refuse it, and what can it change?**
The trace inherits the route map's uncertainty and does not prove coverage.

The trace is derived, not asserted:

* the shared boundary is read out of the `security_boundary` middleware itself —
  host allowlist, cross-origin rejection, bearer token, rate limit and body-size
  limit, with the status code and message the server actually returns;
* every route's handler is located from its decorator, and the refusals inside
  it are read from its own `HTTPException` calls — status code and the detail
  expression that produces the message;
* the first-party subsystems each handler references are classified once, in
  `SUBSYSTEM_ROLES`, into *authority* (can refuse or constrain the action),
  *state* (durable state it reads or writes) and *support* (everything else), and
  a test fails if a subsystem the handlers use has no classification;
* durable writes are counted from the SQL the handler body contains, and
  outbound effects (subprocess, HTTP client, OS keyring) are recorded, because a
  UI that says "this stays local" has to know which route can leave the machine.

What it deliberately does not claim: that a reference *is* a guard. A handler
that mentions `scoped_grants` may consult it on one branch only, and a check made
in a helper it calls is invisible here. The trace is a map of what to read and
what the UI must render from the server, not a proof of enforcement. Nothing is
executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "app.py"
ROUTE_MAP = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
MANIFEST = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
TRACE_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Authorization_Trace.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-authorization-trace.md"

SCHEMA_VERSION = "1.0.0"
STORY = "UIUX-P0-04"

ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
MIDDLEWARE_DECORATOR = "@app.middleware(\"http\")"
STATUS_RE = re.compile(r"status_code\s*=\s*(\d+)")
DETAIL_RE = re.compile(r'"detail":\s*"([^"]+)"')
HTTP_EXCEPTION = "HTTPException("
LOCAL_IMPORT_RE = re.compile(r"^(?:import|from)\s+([A-Za-z_]\w*)")
SQL_WRITE_RE = re.compile(r"\b(?:insert\s+into|update\s+[a-z_]+|delete\s+from|replace\s+into)\b", re.IGNORECASE)
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}

# One classification per first-party subsystem `app.py` imports. The role is a
# statement about what the module is for, not a claim about any single call site;
# `support` is the honest default for logic that neither refuses nor persists.
SUBSYSTEM_ROLES: dict[str, tuple[str, str]] = {
    "execution_spine": ("authority", "admission decisions: whether a task may run, and against which budget"),
    "model_admission": ("authority", "decides whether a model may be loaded or used"),
    "resource_admission": ("authority", "decides whether the host can take another concurrent unit of work"),
    "scoped_grants": ("authority", "issues, narrows and revokes scoped capabilities for a session"),
    "sandbox_boundary": ("authority", "decides which isolation backend is in force and closes the boundary on failure"),
    "process_supervision": ("authority", "registers and contains subprocesses"),
    "workspace_quarantine": ("authority", "quarantines workspace changes pending review"),
    "execution_budget": ("authority", "caps tokens, steps and wall clock for a run"),
    "execution_profiles": ("authority", "the execution policy a run is held to"),
    "routing_policy": ("authority", "routing consent: which provider may serve which request"),
    "security_release_gate": ("authority", "the release gate for security findings"),
    "trusted_transformations": ("authority", "bounded, reviewed code transformations"),
    "runtime_persistence": ("state", "durable runtime records"),
    "workflow_state": ("state", "durable workflow and task state"),
    "task_events": ("state", "the durable event log the UI replays"),
    "context_store": ("state", "the SQLite/FTS5 context store"),
    "artifact_lineage": ("state", "artifact provenance records"),
    "workspace_snapshots": ("state", "workspace snapshots and restore points"),
    "side_effect_ledger": ("state", "the idempotency and side-effect ledger"),
    "edit_transactions": ("state", "transactional edits"),
    "edit_rollback": ("state", "edit rollback records"),
    "history_privacy": ("state", "history retention and export"),
    "evidence_lessons": ("state", "retained lessons and their provenance"),
    "workflow_checkpoints": ("state", "checkpoints a run resumes from"),
    "verification_evidence": ("state", "verification results attached to a task"),
    "recovery_guard": ("state", "crash-recovery state"),
    "child_delegation": ("support", "delegation runs and their benchmarks"),
    "mcp_client": ("support", "MCP transports and tool calls"),
    "routing_experiments": ("support", "routing experiments and measurements"),
    "failure_injection": ("support", "fault injection used by tests"),
    "onboarding_preflight": ("support", "local readiness probing"),
    "workflow_orchestration": ("support", "multi-stage workflow sequencing"),
}


class TraceError(RuntimeError):
    """The source cannot be read the way the trace requires, so it is not published."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_pin() -> str:
    pin = str(json.loads(LEDGER.read_text(encoding="utf-8")).get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise TraceError(f"{LEDGER.name}: source_commit_sha is not a full 40-char sha; re-pin the ledger first")
    return pin


def _balanced(text: str, open_paren: int) -> str:
    """Text between ``text[open_paren]`` and its matching close paren."""
    depth = 0
    index = open_paren
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
        index += 1
    return text[open_paren + 1 :]


def _clean(text: str, limit: int = 180) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _module_of(line: str) -> str | None:
    match = LOCAL_IMPORT_RE.match(line.strip())
    return match.group(1) if match else None


def _local_subsystems(source: str) -> list[str]:
    """First-party modules `app.py` imports, in source order."""
    stems = {path.stem for path in PROJECT_ROOT.glob("*.py")}
    found: list[str] = []
    for line in source.splitlines():
        name = _module_of(line)
        if name and name in stems and name not in found:
            found.append(name)
    return found


def _module_block_end(lines: list[str], start: int) -> int:
    """Index of the first later module-level statement after a def block."""
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        end += 1
    return end


def _def_signature(lines: list[str], index: int) -> tuple[str, int]:
    """Handler name and the index of the last line of its signature."""
    match = DEF_RE.match(lines[index])
    if not match:
        raise TraceError(f"line {index + 1}: expected a function definition, found {lines[index][:60]!r}")
    depth = lines[index].count("(") - lines[index].count(")")
    last = index
    while depth > 0:
        last += 1
        if last >= len(lines):
            raise TraceError(f"{match.group(1)}: unterminated signature")
        depth += lines[last].count("(") - lines[last].count(")")
    return match.group(1), last


def read_middleware(source: str) -> dict[str, object]:
    """The shared boundary: what every request passes through before a handler runs."""
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(MIDDLEWARE_DECORATOR):
            break
    else:
        raise TraceError('no @app.middleware("http") boundary in app.py')

    start = index + 1
    while start < len(lines) and not DEF_RE.match(lines[start]):
        start += 1
    name, sig_end = _def_signature(lines, start)
    end = _module_block_end(lines, sig_end)
    body = "\n".join(lines[sig_end:end])

    refusals = []
    for match in STATUS_RE.finditer(body):
        before = body[: match.start()]
        detail = None
        for candidate in DETAIL_RE.finditer(before[-200:]):
            detail = candidate.group(1)
        refusals.append({"status": int(match.group(1)), "detail": _clean(detail or "")})

    protected_prefixes = re.findall(r'path\.startswith\(\(([^)]*)\)\)', body)
    prefixes = [part.strip().strip("\"'") for part in protected_prefixes[0].split(",")] if protected_prefixes else []

    return {
        "function": name,
        "line": start + 1,
        "refusals": refusals,
        "protected_prefixes": prefixes,
        "constant_time_token_compare": "compare_digest" in body,
        "auth_can_be_disabled": "STUDIO_AUTH_DISABLED" in body,
        "body_size_limit_variable": "STUDIO_MAX_REQUEST_BYTES" in body,
        "rate_limit_variable": "_RATE_LIMIT_PATHS" in body,
        "rate_limit_paths": _rate_limit_paths(lines),
    }


def _rate_limit_paths(lines: list[str]) -> list[str]:
    """The paths the middleware rate-limits, read from the set it tests against."""
    for index, line in enumerate(lines):
        if line.startswith("_RATE_LIMIT_PATHS"):
            block = [line]
            while not block[-1].rstrip().endswith("}"):
                index += 1
                if index >= len(lines):
                    raise TraceError("_RATE_LIMIT_PATHS is not a closed set literal")
                block.append(lines[index])
            return re.findall(r'"(/[^"]+)"', "\n".join(block))
    return []


def read_routes(source: str) -> list[dict[str, object]]:
    """Every `@app.<method>` route with the handler it decorates."""
    lines = source.splitlines()
    routes: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        match = ROUTE_RE.search(line)
        if not match:
            continue
        start = index + 1
        while start < len(lines) and not DEF_RE.match(lines[start]):
            start += 1
        if start >= len(lines):
            raise TraceError(f"route {match.group(2)!r} has no handler")
        name, sig_end = _def_signature(lines, start)
        end = _module_block_end(lines, sig_end)
        signature = _clean(" ".join(part.strip() for part in lines[start:sig_end + 1]), 400)
        routes.append(
            {
                "method": match.group(1).upper(),
                "path": match.group(2),
                "handler": name,
                "line": start + 1,
                "signature": signature,
                "body": "\n".join(lines[sig_end:end]),
            }
        )
    if not routes:
        raise TraceError("no @app.<method> routes in app.py")
    return routes


def read_functions(source: str) -> dict[str, str]:
    """Every module-level function body, so a handler's helpers can be read one level down."""
    lines = source.splitlines()
    functions: dict[str, str] = {}
    for index, line in enumerate(lines):
        if not line.startswith(("def ", "async def ")):
            continue
        match = DEF_RE.match(line)
        if not match:
            continue
        try:
            _, sig_end = _def_signature(lines, index)
        except TraceError:
            continue
        end = _module_block_end(lines, sig_end)
        functions[match.group(1)] = "\n".join(lines[sig_end:end])
    return functions


def _called_helpers(body: str, functions: dict[str, str], owner: str) -> list[str]:
    """Names defined in app.py that this body calls, in order of first appearance."""
    seen: list[str] = []
    for match in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", body):
        name = match.group(1)
        if name != owner and name in functions and name not in seen:
            seen.append(name)
    return seen


def _response_kind(signature: str) -> str:
    match = re.search(r"->\s*([A-Za-z_][\w\.\[\], |]*)", signature)
    if not match:
        return "unannotated"
    annotation = match.group(1).strip()
    if "StreamingResponse" in annotation:
        return "stream"
    if "JSONResponse" in annotation:
        return "json"
    if "HTMLResponse" in annotation:
        return "html"
    return annotation.lower()


def _refusals(body: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for match in re.finditer(re.escape(HTTP_EXCEPTION), body):
        args = _balanced(body, match.end() - 1)
        status = STATUS_RE.search(args)
        detail = re.search(r"detail\s*=\s*(.+?)(?:,\s*\w+\s*=|$)", args, re.DOTALL)
        found.append(
            {
                "status": int(status.group(1)) if status else None,
                "detail": _clean(detail.group(1)) if detail else "",
            }
        )
    return found


def _subsystems(body: str, modules: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {"authority": [], "state": [], "support": []}
    for module in modules:
        if re.search(rf"(?<![\w.]){re.escape(module)}\.", body):
            role = SUBSYSTEM_ROLES[module][0]
            buckets[role].append(module)
    return buckets


def _shared_gates(method: str, path: str, middleware: dict[str, object]) -> list[dict[str, object]]:
    gates: list[dict[str, object]] = []
    prefixes = tuple(str(prefix) for prefix in middleware["protected_prefixes"])  # type: ignore[arg-type]
    gates.append({"gate": "host_allowlist", "status": 400, "when": "always, when a Host header is present"})
    if method not in {"GET", "HEAD", "OPTIONS"}:
        gates.append({"gate": "origin_check", "status": 403, "when": "always, for a state-changing method"})
    if path.startswith(prefixes):
        gates.append({"gate": "bearer_token", "status": 401, "when": "always, unless the token is unconfigured (503)"})
    if method == "POST" and path in (middleware["rate_limit_paths"] or []):
        gates.append({"gate": "rate_limit", "status": 429, "when": "more requests than the window allows"})
    gates.append({"gate": "body_size_limit", "status": 413, "when": "a declared Content-Length exceeds the limit"})
    return gates


def build_trace() -> dict[str, object]:
    source = APP.read_text(encoding="utf-8")
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    middleware = read_middleware(source)
    routes = read_routes(source)
    modules = _local_subsystems(source)
    unclassified = [module for module in modules if module not in SUBSYSTEM_ROLES]
    if unclassified:
        raise TraceError(
            "app.py imports first-party modules with no classification: " + ", ".join(unclassified)
        )
    functions = read_functions(source)

    by_route = {(route["method"], route["path"]): route for route in routes}

    # Which browser handlers reach which route, from the P0-03 map.
    callers: dict[tuple[str, str], set[str]] = {}
    for call in mapping["calls"]:
        for url in call["urls"]:
            if url["kind"] in {"exact", "dynamic", "dynamic_method"}:
                # A structural match may have multiple candidates. Do not drop
                # possible routes just because the map has a primary match.
                for candidate in url["candidates"]:
                    callers.setdefault((candidate["method"], candidate["path"]), set()).add(call["handler"])

    traced = []
    for (method, path), handlers in callers.items():
        if method not in STATE_CHANGING:
            continue
        route = by_route.get((method, path))
        if route is None:
            raise TraceError(f"{method} {path}: the route map names a route app.py does not mount")
        body = str(route["body"])
        buckets = _subsystems(body, modules)
        refusals = _refusals(body)
        helpers = _called_helpers(body, functions, str(route["handler"]))[:12]
        helper_authority: list[str] = []
        helper_state: list[str] = []
        helper_effects = {"subprocess": False, "outbound_http": False, "os_keyring": False}
        for helper in helpers:
            helper_body = functions[helper]
            helper_buckets = _subsystems(helper_body, modules)
            helper_authority += [module for module in helper_buckets["authority"] if module not in buckets["authority"]]
            helper_state += [module for module in helper_buckets["state"] if module not in buckets["state"]]
            helper_effects["subprocess"] = helper_effects["subprocess"] or bool(re.search(r"\bsubprocess\.", helper_body))
            helper_effects["outbound_http"] = helper_effects["outbound_http"] or bool(re.search(r"\bhttpx\.", helper_body))
            helper_effects["os_keyring"] = helper_effects["os_keyring"] or bool(re.search(r"\bkeyring\.", helper_body))
        traced.append(
            {
                "method": method,
                "path": path,
                "handler": route["handler"],
                "handler_line": route["line"],
                "response": _response_kind(str(route["signature"])),
                "ui_callers": sorted(handlers),
                "shared_gates": _shared_gates(method, path, middleware),
                "handler_refusals": refusals,
                "authority": buckets["authority"],
                "state": buckets["state"],
                "support": buckets["support"],
                "authority_via_helpers": sorted(set(helper_authority)),
                "state_via_helpers": sorted(set(helper_state)),
                "helpers_scanned": helpers,
                "durable_writes": len(SQL_WRITE_RE.findall(body)),
                "effects": {
                    "subprocess": bool(re.search(r"\bsubprocess\.", body)) or helper_effects["subprocess"],
                    "outbound_http": bool(re.search(r"\bhttpx\.", body)) or helper_effects["outbound_http"],
                    "os_keyring": bool(re.search(r"\bkeyring\.", body)) or helper_effects["os_keyring"],
                    "via_helpers": any(helper_effects.values()),
                    "streams": _response_kind(str(route["signature"])) == "stream",
                },
            }
        )
    traced.sort(key=lambda row: (str(row["path"]), str(row["method"])))

    statuses: dict[str, int] = {}
    for row in traced:
        for refusal in row["shared_gates"] + row["handler_refusals"]:
            status = refusal.get("status")
            if isinstance(status, int):
                statuses[str(status)] = statuses.get(str(status), 0) + 1

    unauth = [route for route in routes if not str(route["path"]).startswith(tuple(str(p) for p in middleware["protected_prefixes"]))]  # type: ignore[arg-type]

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_authorization_trace",
        "program": "M5_2_0_UIUX",
        "story": STORY,
        "generator": "scripts/uiux_authorization_trace.py",
        "regenerate_with": "python3 scripts/uiux_authorization_trace.py",
        "check_with": "python3 scripts/uiux_authorization_trace.py --check",
        "statement": (
            "For state-changing route candidates resolved by the browser map: the shared gates every request "
            "passes, handler refusals, referenced authority and durable-state subsystems, and possible effects. "
            "The route map is a lexical scan, not proof of exhaustive browser coverage; a reference is evidence "
            "of a consultation, not proof of enforcement."
        ),
        "pinned_source_commit": _ledger_pin(),
        "app_sha256": _sha256(APP),
        "route_map_sha256": _sha256(ROUTE_MAP),
        "totals": {
            "routes_in_app": len(routes),
            "state_changing_routes_in_app": sum(1 for route in routes if route["method"] in STATE_CHANGING),
            "traced_routes": len(traced),
            "route_map_unresolved_call_sites": mapping["totals"]["unresolved_call_sites"],
            "route_map_unresolved_caller_edges": mapping["totals"]["unresolved_caller_edges"],
            "route_map_unresolved_method_edges": mapping["totals"]["unresolved_method_edges"],
            "traced_by_method": {
                method: sum(1 for row in traced if row["method"] == method)
                for method in sorted({str(row["method"]) for row in traced})
            },
            "routes_with_authority_subsystem": sum(
                1 for row in traced if row["authority"] or row["authority_via_helpers"]
            ),
            "routes_with_authority_directly": sum(1 for row in traced if row["authority"]),
            "routes_with_state_subsystem": sum(
                1 for row in traced if row["state"] or row["state_via_helpers"]
            ),
            "routes_with_state_directly": sum(1 for row in traced if row["state"]),
            "routes_with_durable_writes": sum(1 for row in traced if row["durable_writes"]),
            "routes_that_can_leave_the_machine": sum(
                1
                for row in traced
                if row["effects"]["subprocess"] or row["effects"]["outbound_http"] or row["effects"]["os_keyring"]  # type: ignore[index]
            ),
            "distinct_handler_refusals": len({refusal["status"] for row in traced for refusal in row["handler_refusals"]}),
            "refusals_by_status": dict(sorted(statuses.items(), key=lambda item: item[0])),
            "shared_refusals_by_status": {
                status: sum(
                    1 for row in traced for gate in row["shared_gates"] if str(gate.get("status")) == status
                )
                for status in sorted({str(gate.get("status")) for row in traced for gate in row["shared_gates"]})
            },
            "handler_refusals_by_status": {
                ("dynamic" if status == "None" else status): sum(
                    1
                    for row in traced
                    for refusal in row["handler_refusals"]
                    if ("dynamic" if refusal.get("status") is None else str(refusal.get("status"))) == status
                )
                for status in sorted(
                    {
                        "dynamic" if refusal.get("status") is None else str(refusal.get("status"))
                        for row in traced
                        for refusal in row["handler_refusals"]
                    }
                )
            },
        },
        "shared_boundary": {
            "function": middleware["function"],
            "line": middleware["line"],
            "refusals": middleware["refusals"],
            "protected_prefixes": middleware["protected_prefixes"],
            "constant_time_token_compare": middleware["constant_time_token_compare"],
            "auth_can_be_disabled": middleware["auth_can_be_disabled"],
            "rate_limit_paths": middleware["rate_limit_paths"],
            "unprotected_routes": [
                {"method": route["method"], "path": route["path"], "handler": route["handler"]} for route in unauth
            ],
        },
        "subsystem_roles": {
            module: {"role": role, "reason": reason} for module, (role, reason) in sorted(SUBSYSTEM_ROLES.items())
        },
        "routes": traced,
        "not_verified": [
            "Nothing here was executed: no request was sent, so no refusal was observed. The status codes are "
            "read from the handler's own raise statements and the middleware's own returns.",
            "A subsystem reference is a consultation, not a guarantee: a handler may call it on one branch "
            "only, and a guard made inside a helper the handler calls is invisible to this trace.",
            "Authorization decisions that depend on runtime policy (a grant that was revoked, an approval that "
            "expired) cannot be read from source. The UI must render the server's answer, never infer it.",
            "Only state-changing candidates resolved by the lexical browser map are traced. Unobserved or "
            "unresolved browser paths are not proof that other routes have no caller.",
            "A blank authority or durable-state cell is not a verdict. The scan reads the handler and the "
            "functions it calls by name, one level down; a guard two calls away, or made inside a method on a "
            "class, is not seen. Read the cell as 'nothing at this depth', never as 'unguarded'.",
        ],
    }


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def render_doc(trace: dict[str, object]) -> str:
    totals = trace["totals"]
    boundary = trace["shared_boundary"]
    lines = [
        "# M5 2.0 UI/UX authorization and state trace",
        "",
        "Generated by `python3 scripts/uiux_authorization_trace.py` — **never hand-edit this page**.",
        "It is the `UIUX-P0-04` artifact: for state-changing route candidates resolved by the browser map,",
        "what can refuse them and what durable state they touch, derived from `app.py` and the route map.",
        "",
        f"* Pinned source commit: `{trace['pinned_source_commit']}`.",
        f"* `app.py` digest: `{str(trace['app_sha256'])[:16]}…` · route map digest: `{str(trace['route_map_sha256'])[:16]}…`.",
        f"* {totals['traced_routes']} resolved state-changing route candidates, out of "
        f"{totals['state_changing_routes_in_app']} in `app.py`; "
        f"{totals['route_map_unresolved_call_sites']} fetch site(s) have "
        f"{totals['route_map_unresolved_caller_edges']} unresolved URL or method edge(s) "
        f"({totals['route_map_unresolved_method_edges']} method).",
        f"* {totals['routes_with_authority_subsystem']} reference an authority subsystem, "
        f"{totals['routes_with_state_subsystem']} a durable-state subsystem, "
        f"{totals['routes_with_durable_writes']} contain a SQL write, and "
        f"{totals['routes_that_can_leave_the_machine']} can leave the machine.",
        "",
        "## What this page is, and is not",
        "",
        "A refusal listed here is a status code the handler can raise, read from its own `raise` statement,",
        "plus the gates the shared middleware applies to every request. It is not an observed refusal: no",
        "request was sent. Browser coverage comes from a lexical map and is not a proof of completeness.",
        "When the browser method is unknown, every path-compatible state-changing candidate is included,",
        "not asserted to be executed. Unresolved edges may still hide other routes.",
        "A subsystem reference means the handler mentions that subsystem — it is evidence of",
        "a consultation, not proof that every path through the handler is guarded, and a check inside a helper",
        "is invisible here. The UI rule that follows is the same for every row: **render the server's answer;",
        "never enable an action, an approval or a recovery state on a client-side guess.**",
        "",
        "### Reading this for a UI story",
        "",
        "1. **Render the refusal; never enable on a guess.** Every code in a row can legitimately reach the",
        "   screen. A disabled control must correspond to a real refusal, and an enabled one must not assume",
        "   success.",
        "2. **Approval and recovery are server state.** `/api/harness/approve` is where an approval becomes",
        "   real; a screen may show that a request is waiting, never decide that it was approved.",
        f"3. **Consent copy follows the effect column.** {totals['routes_that_can_leave_the_machine']} of the traced routes can reach a subprocess, an",
        "   outbound HTTP client or the OS keyring, several of them one helper call away. A label that says",
        "   \"local\" has to match what the row says, not what the pane implies.",
        "",
        "## The shared boundary",
        "",
        f"Every request passes `{boundary['function']}()` (`app.py:{boundary['line']}`) before any handler runs.",
        f"It protects everything under {', '.join(f'`{prefix}`' for prefix in boundary['protected_prefixes'])}.",
        "",
        "| Gate | Status | When it can refuse |",
        "|---|---|---|",
    ]
    seen = set()
    for refusal in boundary["refusals"]:
        key = (refusal["status"], refusal["detail"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"| shared middleware | {refusal['status']} | {_cell(refusal['detail'])} |")
    lines += [
        "",
        f"* The bearer token is compared with `secrets.compare_digest`: "
        f"{'yes' if boundary['constant_time_token_compare'] else 'no'}.",
        f"* Authentication can be turned off by configuration: {'yes' if boundary['auth_can_be_disabled'] else 'no'}.",
        f"* Rate-limited paths: {', '.join(f'`{path}`' for path in boundary['rate_limit_paths']) or 'none'}.",
        "",
        "Routes outside the protected prefixes: "
        + ", ".join(f"`{route['method']} {route['path']}`" for route in boundary["unprotected_routes"])
        + " — the shell and the health probe, which the browser loads before it has a token.",
        "",
        "## State-changing calls the browser can make",
        "",
        "| Method | Route | Handler | Browser callers | Response | Refusals | Authority | Durable state | Writes | Effects |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in trace["routes"]:
        refusals = sorted(
            {str(item["status"]) for item in row["shared_gates"] + row["handler_refusals"] if item.get("status")}
        )
        effects = []
        if row["effects"]["subprocess"]:
            effects.append("subprocess")
        if row["effects"]["outbound_http"]:
            effects.append("outbound http")
        if row["effects"]["os_keyring"]:
            effects.append("keyring")
        lines.append(
            f"| {row['method']} | `{_cell(row['path'])}` | `{row['handler']}` | "
            f"{_cell(', '.join(row['ui_callers']))} | {row['response']} | {_cell(' '.join(refusals))} | "
            f"{_cell(', '.join(row['authority']))} | {_cell(', '.join(row['state']))} | "
            f"{row['durable_writes'] or '—'} | {_cell(', '.join(effects))} |"
        )

    lines += ["", "## Refusals the UI has to be able to render", ""]
    lines += [
        "Every traced call can be refused by the shared boundary before its handler runs; the handler adds",
        "the refusals that depend on what is being asked. Both are listed, because a screen that shows",
        "understandable errors needs the whole set.",
        "",
        "| Status | From the shared boundary | From the handler | Routes that can return it |",
        "|---|---|---|---|",
    ]
    for status, count in totals["refusals_by_status"].items():
            routes = [
                row
                for row in trace["routes"]
                if any(str(item.get("status")) == status for item in row["shared_gates"] + row["handler_refusals"])
            ]
            shared = totals["shared_refusals_by_status"].get(status, 0)
            handler = totals["handler_refusals_by_status"].get(status, 0)
            lines.append(
                f"| {status} | {shared} | {handler} | {len(routes)} route(s): "
                + ", ".join(f"`{row['method']} {row['path']}`" for row in routes)
                + " |"
            )
    dynamic = totals["handler_refusals_by_status"].get("dynamic", 0)
    if dynamic:
        lines += [
            "",
            f"{dynamic} handler raises a status code it computes at runtime rather than a literal, so it is",
            "counted separately instead of being guessed into a bucket.",
        ]

    lines += [
        "",
        "## Handlers that can leave the machine",
        "",
        "A screen that says \"this stays local\" has to know which of its buttons cannot promise that.",
        "",
        "| Method | Route | Subprocess | Outbound HTTP | OS keyring |",
        "|---|---|---|---|---|",
    ]
    leaving = [
        row
        for row in trace["routes"]
        if row["effects"]["subprocess"] or row["effects"]["outbound_http"] or row["effects"]["os_keyring"]
    ]
    if not leaving:
        lines.append("| — | No traced route contains a subprocess, an outbound HTTP client or a keyring call. | — | — | — |")
    for row in leaving:
        lines.append(
            f"| {row['method']} | `{_cell(row['path'])}` | {'yes' if row['effects']['subprocess'] else '—'} "
            f"| {'yes' if row['effects']['outbound_http'] else '—'} | {'yes' if row['effects']['os_keyring'] else '—'} |"
        )

    lines += ["", "## Subsystems and their role", "", "| Subsystem | Role | Why |", "|---|---|---|"]
    for module, entry in trace["subsystem_roles"].items():
        lines.append(f"| `{module}` | {entry['role']} | {_cell(entry['reason'])} |")

    lines += ["", "## Not verified", ""]
    lines += [f"* {item}" for item in trace["not_verified"]]
    lines.append("")
    return "\n".join(lines)


def _rendered() -> tuple[str, str]:
    trace = build_trace()
    return json.dumps(trace, indent=2, ensure_ascii=False) + "\n", render_doc(trace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if either artifact has drifted")
    parser.add_argument("--json", action="store_true", help="print the trace and write nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    try:
        payload, page = _rendered()
    except (TraceError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(payload)
        return 0

    if args.check:
        drifted = []
        for path, expected in ((TRACE_PATH, payload), (DOC_PATH, page)):
            if not path.is_file():
                print(f"error: {path.name} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(path.name)
        if drifted:
            print(
                "error: the UI authorization trace has drifted from app.py or the route map: "
                + ", ".join(drifted)
                + "\n       Regenerate it (the change belongs to the story):"
                + "\n           python3 scripts/uiux_authorization_trace.py",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            totals = json.loads(payload)["totals"]
            print(
                "UI authorization trace is up to date: "
                f"{totals['traced_routes']} state-changing routes, "
                f"{totals['routes_with_authority_subsystem']} with an authority subsystem, "
                f"{totals['routes_with_state_subsystem']} with durable state, "
                f"{totals['routes_that_can_leave_the_machine']} that can leave the machine"
            )
        return 0

    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACE_PATH.write_text(payload, encoding="utf-8")
    DOC_PATH.write_text(page, encoding="utf-8")
    if not args.quiet:
        totals = json.loads(payload)["totals"]
        print(
            f"Wrote {TRACE_PATH.name} and {DOC_PATH.name}: "
            f"{totals['traced_routes']} state-changing routes traced"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
