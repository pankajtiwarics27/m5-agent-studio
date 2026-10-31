#!/usr/bin/env python3
"""Map every frontend fetch call to the API route it hits (UIUX-P0-03).

    python3 scripts/uiux_route_map.py            # regenerate JSON + docs
    python3 scripts/uiux_route_map.py --check    # fail if either drifted
    python3 scripts/uiux_route_map.py --json     # print the map

`static/index.html` talks to the server through `fetch` calls inside one inline
script; `app.py` mounts 102 routes. Nothing in the repository states which call
hits which route, so every later UI story that touches a pane is guessing about
the contract it depends on. This script derives the mapping from the calls:

* each `fetch(...)` is located, its enclosing function recorded, and its HTTP
  method read from the `method:` option (default `GET`);
* literal URLs are used directly; a URL held in a local `const` is resolved from
  the enclosing function's own assignment; a helper that receives its URL as a
  parameter (`consumeSSE(url, ...)`) is resolved from its call sites, *including
  the call sites of helpers that forward their own parameter into it*
  (`_streamSdlcEndpoint(url, ...)` -> `consumeSSE(url, ...)`), four hops deep.
  Each URL carries the helper chain it travelled, so a forwarded call names the
  entry point that supplied the literal;
* each URL is matched against the route table segment by segment, honouring
  `<param>` and `<param:path>` converters, and distinguishing three outcomes:
  an exact match, a *dynamic* match (the URL supplies a segment the route spells
  literally, e.g. `/api/models/${id}/${act}` against `/load` and `/unload`), and
  no match at all. A dynamic match registers its caller as *possible* on every
  candidate route, never as certain on one of them;
* lines are published in two frames, `script_line` (inside the inline script) and
  `document_line` (in `static/index.html`), because a script line read as a
  document line is off by all the markup above it.

The unmatched and mismatched entries are the point. A call that resolves to no
route is either dead code or a bug, and a call whose method disagrees with the
route is a request that can only fail; both are published as findings instead of
being smoothed into a clean-looking table. Routes with no browser caller are
published too, because "the UI does not use this" is as useful as the converse.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MAP_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-route-map.md"

SCHEMA_VERSION = "1.0.0"
STORY = "UIUX-P0-03"

SCRIPT_RE = re.compile(r"<script\b[^>]*>(?P<body>.*?)</script>", re.DOTALL)
FETCH_RE = re.compile(r"\bfetch\s*\(")
FUNCTION_RE = re.compile(r"(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(")
METHOD_RE = re.compile(r"""method\s*:\s*['"](?P<method>\w+)['"]""")
IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
STRING_RE = re.compile(r"""(["'`])((?:\\.|(?!\1).)*?)\1""")
ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
SERVICE_PREFIXES = ("/api/", "/v1/")
MAX_RESOLUTION_DEPTH = 4


class RouteMapError(RuntimeError):
    """The call sites or the route table cannot be read, so the map cannot be trusted."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_pin() -> str:
    pin = str(json.loads(LEDGER.read_text(encoding="utf-8")).get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise RouteMapError(f"{LEDGER.name}: source_commit_sha is not a full 40-char sha; re-pin the ledger first")
    return pin


def _balanced(text: str, open_paren: int) -> str:
    """Return the text between `text[open_paren]` and its matching close paren."""
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
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : index]
        index += 1
    return text[open_paren + 1 :]


def _urls_in(text: str) -> list[str]:
    """Service paths in a fragment, with `${...}` collapsed to `{param}`."""
    found = []
    for match in STRING_RE.finditer(text):
        value = match.group(2)
        if value.startswith(SERVICE_PREFIXES):
            normalised = re.sub(r"\$\{[^}]*\}", "{param}", value)
            if normalised not in found:
                found.append(normalised)
    return found


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _script_block(html: str) -> tuple[str, int]:
    """The largest inline script, and the offset in the document it starts at."""
    blocks = [(match.group("body"), match.start("body")) for match in SCRIPT_RE.finditer(html)]
    if not blocks:
        raise RouteMapError("no inline script in static/index.html")
    return max(blocks, key=lambda pair: len(pair[0]))


def _split_args(text: str) -> list[str]:
    """Split a call's argument text on its top-level commas."""
    parts: list[str] = []
    depth = 0
    start = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    tail = text[start:]
    if parts or tail.strip():
        parts.append(tail)
    return [part.strip() for part in parts]


def _brace_end(script: str, open_index: int) -> int:
    """Index just past the `}` matching the `{` at `open_index`."""
    depth = 0
    index = open_index
    quote: str | None = None
    while index < len(script):
        char = script[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(script)


def _read_functions(script: str) -> dict[str, dict[str, object]]:
    """Every `function name(params) { ... }` with its parameter names and body span."""
    functions: dict[str, dict[str, object]] = {}
    for match in FUNCTION_RE.finditer(script):
        params_text = _balanced(script, match.end() - 1)
        brace = script.find("{", match.end() - 1 + len(params_text))
        if brace == -1:
            continue
        functions[match.group("name")] = {
            "def_start": match.start(),
            "params": [part for part in _split_args(params_text) if part],
            "body_start": brace,
            "end": _brace_end(script, brace),
        }
    return functions


def _scope_at(script: str, functions: dict[str, dict[str, object]], offset: int) -> dict[str, object]:
    """The innermost function containing `offset`, or a top-level scope."""
    best: dict[str, object] = {"name": "top-level", "params": [], "body_start": 0, "def_start": -1, "end": len(script)}
    for name, info in functions.items():
        if int(info["def_start"]) < offset < int(info["end"]) and int(info["def_start"]) > int(best["def_start"]):
            best = {"name": name, **info}
    return best


def _local_assignment(script: str, name: str, start: int, end: int) -> str | None:
    """The last `const|let|var name = <expr>;` between `start` and `end`."""
    found: str | None = None
    for match in re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*([^;]+);", script[start:end]):
        found = match.group(1)
    return found


def _call_sites(script: str, functions: dict[str, dict[str, object]], name: str) -> list[tuple[str, int]]:
    """Every call to `name`: its argument text and the offset of the call."""
    sites: list[tuple[str, int]] = []
    definition = functions.get(name)
    for match in re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*\(", script):
        if definition is not None and int(definition["def_start"]) == match.start():
            continue
        sites.append((_balanced(script, match.end() - 1), match.start()))
    return sites


def _resolve_expression(
    script: str,
    functions: dict[str, dict[str, object]],
    expression: str,
    scope: dict[str, object],
    at: int,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, list[str]]], str]:
    """Resolve one `fetch` URL argument to the service paths it can hold.

    Returns `(pairs, how)`, where each pair is a URL and the helper chain the
    value travelled through to reach it, and `how` is `literal`, `assignment`,
    `callers` (a helper whose call sites pass literals), `forwarded` (the value
    crossed more than one helper) or `unresolved`. The chain is per URL on
    purpose: two URLs reached through the same sink can come from different
    entry points, and one merged list would describe neither.
    """
    direct = _urls_in(expression)
    if direct:
        return [(url, []) for url in direct], "literal"

    # The same identifier in two functions is two different values, so a guard
    # on the name alone would stop a legitimate forward (`url` inside
    # `consumeSSE` -> `url` inside `_streamSdlcEndpoint`); guard on the pair.
    marker = f"{scope['name']}:{expression}"
    if not IDENT_RE.match(expression) or depth >= MAX_RESOLUTION_DEPTH or marker in seen:
        return [], "unresolved"

    # 1. A local binding in the same function, before the use. A same-named
    #    variable in another function is a different value, so the search never
    #    leaves this function's body.
    assignment = _local_assignment(script, expression, int(scope["body_start"]), at)
    if assignment is not None:
        pairs, _how = _resolve_expression(
            script, functions, assignment.strip(), scope, int(scope["body_start"]), depth + 1, seen | {marker}
        )
        if pairs:
            return pairs, "assignment"

    # 2. A parameter of the enclosing function: follow the argument every call
    #    site passes at that position, recursing into *its* scope. This is what
    #    resolves `consumeSSE(url)` when `url` is itself a helper's parameter.
    params = [str(part) for part in scope["params"]]  # type: ignore[union-attr]
    if expression in params and str(scope["name"]) != "top-level":
        index = params.index(expression)
        collected: list[tuple[str, list[str]]] = []
        hop_kinds: set[str] = set()
        for args_text, call_at in _call_sites(script, functions, str(scope["name"])):
            args = _split_args(args_text)
            if index >= len(args) or not args[index]:
                continue
            caller_scope = _scope_at(script, functions, call_at)
            pairs, how = _resolve_expression(
                script, functions, args[index], caller_scope, call_at, depth + 1, seen | {marker}
            )
            if not pairs:
                continue
            hop_kinds.add(how)
            for url, chain in pairs:
                # The chain reads up from the fetch to the function holding the
                # literal, so a reader sees the entry point, not just the sink.
                collected.append((url, [str(scope["name"]), *(chain or [str(caller_scope["name"])])]))
        if collected:
            merged: list[tuple[str, list[str]]] = []
            index_by_url: dict[str, int] = {}
            for url, chain in collected:
                if url in index_by_url:
                    continue
                index_by_url[url] = len(merged)
                merged.append((url, chain))
            return merged, ("callers" if hop_kinds == {"literal"} else "forwarded")
    return [], "unresolved"


def read_routes(source: str) -> list[dict[str, str]]:
    return [{"method": m.group(1).upper(), "path": m.group(2)} for m in ROUTE_RE.finditer(source)]


def _route_segments(path: str) -> list[str]:
    return [segment for segment in path.split("?")[0].split("/") if segment]


def _route_fit(route_segments: list[str], url_segments: list[str]) -> tuple[bool, bool]:
    """Can a URL fit a route pattern, and would it need a runtime-chosen segment?

    A route parameter absorbs any URL segment. A URL segment the browser picks at
    runtime (`{param}`) can also stand in for a route's *literal* segment — that is
    what makes `/api/models/${id}/${act}` a candidate for both `/load` and
    `/unload` — but such a fit is reported separately, because the route is only
    known once the caller's argument is known.
    """
    index = 0
    guessed = False
    for position, route_segment in enumerate(route_segments):
        is_param = route_segment.startswith("{")
        is_path = route_segment.endswith(":path}")
        if is_param and is_path:
            remaining_literal = sum(1 for s in route_segments[position + 1 :] if not s.startswith("{"))
            if len(url_segments) - index < remaining_literal + 1:
                return False, guessed
            index = len(url_segments) - remaining_literal
            continue
        if index >= len(url_segments):
            return False, guessed
        url_segment = url_segments[index]
        if is_param:
            if url_segment != "{param}":
                pass  # a concrete segment absorbed by a route parameter
        elif url_segment == "{param}":
            guessed = True
        elif route_segment != url_segment:
            return False, guessed
        index += 1
    return index == len(url_segments), guessed


def _match(url: str, method: str, routes: list[dict[str, str]]) -> dict[str, object]:
    url_segments = _route_segments(url)
    fits: list[tuple[dict[str, str], bool]] = []
    for route in routes:
        matched, guessed = _route_fit(_route_segments(route["path"]), url_segments)
        if matched:
            fits.append((route, guessed))

    same_method = [pair for pair in fits if pair[0]["method"] == method]
    exact = [pair for pair in same_method if not pair[1]]
    if exact:
        return {"kind": "exact", "route": exact[0][0]["path"], "candidates": [p[0] for p in same_method]}
    dynamic = [pair for pair in same_method if pair[1]]
    if dynamic:
        return {
            "kind": "dynamic",
            "route": dynamic[0][0]["path"],
            "candidates": [p[0] for p in dynamic],
        }
    if fits:
        return {"kind": "method_mismatch", "route": None, "candidates": [p[0] for p in fits]}
    return {"kind": "no_route", "route": None, "candidates": []}


def build_map() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    script, script_offset = _script_block(html)
    functions = _read_functions(script)

    routes = read_routes(APP.read_text(encoding="utf-8"))
    if not routes:
        raise RouteMapError("no @app.<method> routes in app.py")

    calls: list[dict[str, object]] = []
    for fetch in FETCH_RE.finditer(script):
        argument_text = _balanced(script, fetch.end() - 1)
        first_arg = _split_args(argument_text)[0] if _split_args(argument_text) else ""
        method_match = METHOD_RE.search(argument_text)
        method = method_match.group("method").upper() if method_match else "GET"
        scope = _scope_at(script, functions, fetch.start())
        pairs, source = _resolve_expression(script, functions, first_arg, scope, fetch.start())
        resolutions = [_match(url, method, routes) | {"url": url, "forwarded_via": chain} for url, chain in pairs]
        calls.append(
            {
                "handler": str(scope["name"]),
                "script_line": _line_of(script, fetch.start()),
                "document_line": _line_of(html, script_offset + fetch.start()),
                "method": method,
                "url_argument": first_arg[:120],
                "resolved_from": source,
                "urls": resolutions,
            }
        )

    # Route -> callers, in both directions and both strengths. An exact match
    # proves the route; a dynamic match proves only that the call can reach one
    # of its candidates, so those callers are recorded as possible, on *every*
    # candidate rather than on whichever one the matcher happened to list first.
    certain: dict[tuple[str, str], set[str]] = {}
    possible: dict[tuple[str, str], set[str]] = {}
    for call in calls:
        handler = str(call["handler"])
        method = str(call["method"])
        for resolution in call["urls"]:
            if resolution["kind"] == "exact":
                certain.setdefault((method, str(resolution["route"])), set()).add(handler)
            elif resolution["kind"] == "dynamic":
                for candidate in resolution["candidates"]:
                    possible.setdefault(
                        (str(candidate["method"]), str(candidate["path"])), set()
                    ).add(handler)

    route_rows = []
    for route in routes:
        key = (route["method"], route["path"])
        sure = sorted(certain.get(key, set()))
        maybe = sorted(possible.get(key, set()) - set(sure))
        route_rows.append(
            {
                **route,
                "callers": sure,
                "possible_callers": maybe,
                "caller_certainty": "certain" if sure else ("possible" if maybe else "none"),
                "used_by_frontend": bool(sure or maybe),
            }
        )

    findings = []
    for call in calls:
        if not call["urls"]:
            findings.append(
                {
                    "kind": "unresolved_url",
                    "handler": call["handler"],
                    "method": call["method"],
                    "url": call["url_argument"],
                    "detail": (
                        "the URL argument is not a literal, a local assignment, or a helper parameter "
                        "whose call sites pass one - the caller list for its routes is a lower bound"
                    ),
                }
            )
            continue
        for resolution in call["urls"]:
            if resolution["kind"] in {"exact", "dynamic"}:
                continue
            findings.append(
                {
                    "kind": resolution["kind"],
                    "handler": call["handler"],
                    "method": call["method"],
                    "url": resolution["url"],
                    "detail": (
                        "path exists but not for this method: "
                        if resolution["kind"] == "method_mismatch"
                        else "no route matches this path: "
                    )
                    + ", ".join(f"{c['method']} {c['path']}" for c in resolution["candidates"])[:300],
                }
            )

    url_count = sum(len(call["urls"]) for call in calls)
    exact = sum(1 for call in calls for r in call["urls"] if r["kind"] == "exact")
    dynamic = sum(1 for call in calls for r in call["urls"] if r["kind"] == "dynamic")
    used_routes = sum(1 for row in route_rows if row["used_by_frontend"])
    certain_routes = sum(1 for row in route_rows if row["caller_certainty"] == "certain")
    possible_only = [str(row["path"]) for row in route_rows if row["caller_certainty"] == "possible"]
    unresolved_handlers = [str(call["handler"]) for call in calls if not call["urls"]]
    by_source: dict[str, int] = {}
    for call in calls:
        by_source[str(call["resolved_from"])] = by_source.get(str(call["resolved_from"]), 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_route_map",
        "program": "M5_2_0_UIUX",
        "story": STORY,
        "generator": "scripts/uiux_route_map.py",
        "regenerate_with": "python3 scripts/uiux_route_map.py",
        "check_with": "python3 scripts/uiux_route_map.py --check",
        "statement": (
            "Every fetch call in the inline script mapped to the route it can hit, derived from the "
            "call sites and the route table. Unmatched calls, method mismatches, dynamic matches and "
            "routes with no browser caller are published rather than smoothed over."
        ),
        "line_frames": {
            "script_line": "line inside the inline <script> block",
            "document_line": "line in static/index.html, which is what a reader of that file needs",
        },
        "pinned_source_commit": _ledger_pin(),
        "frontend_sha256": _sha256(FRONTEND),
        "app_sha256": _sha256(APP),
        "caller_resolution": {
            "complete": not unresolved_handlers,
            "unresolved_call_sites": sorted(set(unresolved_handlers)),
            "statement": (
                "Every call site resolved to at least one URL, so a route listed as having no browser "
                "caller really has none at this depth."
                if not unresolved_handlers
                else (
                    "Some call sites did not resolve to a URL, so a route listed as having no browser "
                    "caller may still be called: treat that list as unproven, not as evidence of disuse."
                )
            ),
        },
        "totals": {
            "fetch_call_sites": len(calls),
            "resolved_urls": url_count,
            "exact_matches": exact,
            "dynamic_matches": dynamic,
            "forwarded_call_sites": by_source.get("forwarded", 0),
            "unresolved_call_sites": sum(1 for call in calls if not call["urls"]),
            "findings": len(findings),
            "routes": len(routes),
            "routes_used_by_frontend": used_routes,
            "routes_with_certain_caller": certain_routes,
            "routes_only_possible_caller": len(possible_only),
            "routes_without_frontend_caller": len(routes) - used_routes,
            "resolved_from": dict(sorted(by_source.items())),
        },
        "calls": calls,
        "routes": route_rows,
        "findings": findings,
        "not_verified": [
            "Nothing here was executed. A matched call is a request the browser can construct, not a "
            "route that works: authorization, request bodies and response handling are out of scope.",
            "URLs are matched structurally. A URL whose segment is chosen at runtime is reported as a "
            "dynamic match with its candidate routes instead of being resolved to one, and every "
            "candidate lists that caller as *possible*, never as certain.",
            "A URL forwarded through helper parameters is followed back to the literal that supplies "
            "it, at most four helper hops deep, and the chain is published as `forwarded_via`. A "
            "deeper or indirect hand-off (a value stored on an object, a callback argument) stays "
            "unresolved and makes the caller list a lower bound rather than a proof of disuse.",
            "Line numbers are published in two frames: `script_line` is relative to the inline script, "
            "`document_line` to static/index.html. Reading a script line as a document line is off by "
            "the markup above the script.",
            "Routes without a browser caller are not dead: the CLI, the demo tour and the tests call "
            "the same API.",
        ],
    }


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def render_doc(mapping: dict[str, object]) -> str:
    totals = mapping["totals"]
    resolution = mapping["caller_resolution"]
    lines = [
        "# M5 2.0 UI/UX route map",
        "",
        "Generated by `python3 scripts/uiux_route_map.py` — **never hand-edit this page**.",
        "It is the `UIUX-P0-03` artifact: every `fetch` call in the inline script, the function",
        "it lives in, and the route it can hit — derived from the calls and the route table,",
        "not from handler names.",
        "",
        f"* Pinned source commit: `{mapping['pinned_source_commit']}`.",
        f"* Frontend digest: `{mapping['frontend_sha256'][:16]}…` · `app.py` digest: `{mapping['app_sha256'][:16]}…`.",
        f"* {totals['fetch_call_sites']} call sites resolve to {totals['resolved_urls']} URLs — "
        f"exact: {totals['exact_matches']}, dynamic: {totals['dynamic_matches']}, "
        f"forwarded through a helper: {totals['forwarded_call_sites']}, "
        f"findings: {totals['findings']}, unresolved call sites: {totals['unresolved_call_sites']}.",
        f"* {totals['routes']} routes exist; {totals['routes_used_by_frontend']} have a browser caller "
        f"({totals['routes_with_certain_caller']} proved by a literal argument, "
        f"{totals['routes_only_possible_caller']} only reachable through a runtime-chosen segment) and "
        f"{totals['routes_without_frontend_caller']} have none.",
        "",
        "## What this page is, and is not",
        "",
        "A matched call is a request the browser **can construct**, not a route that works: nothing",
        "here was executed, and authorization, request bodies and response handling are not checked.",
        "A URL segment chosen at runtime is reported as a *dynamic* match with its candidate routes",
        "rather than being resolved to one, and each candidate carries that caller as *possible*,",
        "never as certain. A URL handed to a helper is followed back through the helper's call sites —",
        "the `Via` column names that chain, so a forwarded call cannot silently lose its route.",
        "Routes with no browser caller are not dead code — the CLI, the demo tour and the tests call",
        "the same API.",
        "",
        "Line numbers appear in two frames: `Script` is the line inside the inline `<script>` block,",
        "`Document` is the line in `static/index.html` that a reader of that file needs.",
        "",
        "## Call sites by handler",
        "",
        "| Handler | Script | Document | Method | Resolved from | Via | URL | Match | Route |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for call in mapping["calls"]:
        if not call["urls"]:
            lines.append(
                f"| `{call['handler']}` | {call['script_line']} | {call['document_line']} | {call['method']} "
                f"| {call['resolved_from']} | — | `{_cell(call['url_argument'])}` | unresolved | — |"
            )
            continue
        for item in call["urls"]:
            route = item.get("route") or "—"
            via = " → ".join(f"`{name}`" for name in item["forwarded_via"]) or "—"
            lines.append(
                f"| `{call['handler']}` | {call['script_line']} | {call['document_line']} | {call['method']} "
                f"| {call['resolved_from']} | {via} | `{_cell(item['url'])}` | {item['kind']} | `{_cell(route)}` |"
            )

    lines += ["", "## Findings", ""]
    if not mapping["findings"]:
        lines.append(
            "No unmatched call, no method mismatch and no unresolved URL: every call site resolves to a "
            "route that exists for the method it uses."
        )
    else:
        lines += ["| Kind | Handler | Method | URL | Detail |", "|---|---|---|---|---|"]
        for finding in mapping["findings"]:
            lines.append(
                f"| {finding['kind']} | `{finding['handler']}` | {finding['method']} "
                f"| `{_cell(finding['url'])}` | {_cell(finding['detail'])} |"
            )

    possible = [route for route in mapping["routes"] if route["caller_certainty"] == "possible"]
    lines += ["", "## Routes reached only through a runtime-chosen segment", ""]
    if not possible:
        lines.append("Every route with a browser caller is reached by a literal URL argument.")
    else:
        lines.append(
            f"{len(possible)} route(s) have a caller whose URL segment is chosen at runtime. The caller is "
            "recorded as *possible* because the literal argument at the call site decides which of the "
            "candidate routes is really used."
        )
        lines += ["", "| Method | Route | Possible callers |", "|---|---|---|"]
        for route in possible:
            names = ", ".join(f"`{name}`" for name in route["possible_callers"])
            lines.append(f"| {route['method']} | `{route['path']}` | {names} |")

    unused = [route for route in mapping["routes"] if not route["used_by_frontend"]]
    lines += ["", "## Routes with no browser caller", ""]
    if not resolution["complete"]:
        lines.append(
            f"**Lower bound, not a proof of disuse.** {len(resolution['unresolved_call_sites'])} call site(s) "
            "did not resolve to a URL, so a route below may still be called: "
            + ", ".join(f"`{name}`" for name in resolution["unresolved_call_sites"])
            + "."
        )
    if not unused:
        lines.append("Every route has at least one browser caller.")
    else:
        lines.append(
            f"{len(unused)} of {mapping['totals']['routes']} routes are not called by the inline script. "
            "That is not dead code: the CLI, the demo tour and the tests use the same API."
        )
        lines += ["", "| Method | Route |", "|---|---|"]
        for route in unused:
            lines.append(f"| {route['method']} | `{route['path']}` |")

    lines += ["", "## Not verified", ""]
    lines += [f"* {item}" for item in mapping["not_verified"]]
    lines.append("")
    return "\n".join(lines)


def _rendered() -> tuple[str, str]:
    mapping = build_map()
    return json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", render_doc(mapping)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if either artifact has drifted")
    parser.add_argument("--json", action="store_true", help="print the map and write nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    try:
        payload, page = _rendered()
    except (RouteMapError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(payload)
        return 0

    if args.check:
        drifted = []
        for path, expected in ((MAP_PATH, payload), (DOC_PATH, page)):
            if not path.is_file():
                print(f"error: {path.name} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(path.name)
        if drifted:
            print(
                "error: the UI route map has drifted from the calls or the routes: " + ", ".join(drifted)
                + "\n       Regenerate it (the change belongs to the story):"
                + "\n           python3 scripts/uiux_route_map.py",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            totals = json.loads(payload)["totals"]
            print(
                "UI route map is up to date: "
                f"{totals['fetch_call_sites']} call sites, {totals['exact_matches']} exact and "
                f"{totals['dynamic_matches']} dynamic matches, {totals['findings']} findings, "
                f"{totals['routes_without_frontend_caller']} routes with no browser caller"
            )
        return 0

    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(payload, encoding="utf-8")
    DOC_PATH.write_text(page, encoding="utf-8")
    if not args.quiet:
        totals = json.loads(payload)["totals"]
        print(
            f"Wrote {MAP_PATH.name} and {DOC_PATH.name}: {totals['fetch_call_sites']} call sites, "
            f"{totals['findings']} findings"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
