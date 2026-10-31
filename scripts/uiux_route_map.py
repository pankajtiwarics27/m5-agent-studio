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
  parameter (`consumeSSE(url, ...)`) is resolved from its call sites;
* each URL is matched against the route table segment by segment, honouring
  `<param>` and `<param:path>` converters, and distinguishing three outcomes:
  an exact match, a *dynamic* match (the URL supplies a segment the route spells
  literally, e.g. `/api/models/${id}/${act}` against `/load` and `/unload`), and
  no match at all.

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


def _enclosing_function(script: str, offset: int) -> tuple[str, int]:
    """Name of the nearest preceding function and the offset it starts at."""
    best_name, best_at = "top-level", 0
    for match in FUNCTION_RE.finditer(script, 0, offset):
        best_name, best_at = match.group("name"), match.start()
    return best_name, best_at


def _line_of(script: str, offset: int) -> int:
    return script.count("\n", 0, offset) + 1


def _resolve_urls(script: str, first_arg: str, handler: str, scope_start: int, fetch_at: int) -> tuple[list[str], str]:
    """Resolve the URL argument of one fetch call to concrete paths."""
    direct = _urls_in(first_arg)
    if direct:
        return direct, "literal"

    if not IDENT_RE.match(first_arg):
        return [], "unresolved"

    # A local `const url = ...;` inside the enclosing function, but never one from
    # a different function: a same-named variable elsewhere is a different value.
    local = re.search(
        rf"(?:const|let|var)\s+{re.escape(first_arg)}\s*=\s*([^;]+);", script[scope_start:fetch_at]
    )
    if local:
        resolved = _urls_in(local.group(1))
        if resolved:
            return resolved, "assignment"

    # A helper that takes the URL as a parameter: read its call sites instead.
    caller_urls: list[str] = []
    for call in re.finditer(rf"(?<![\w$.]){re.escape(handler)}\s*\(", script):
        if script[max(0, call.start() - 9) : call.start()] == "function ":
            continue  # the definition, not a call
        caller_urls.extend(_urls_in(_balanced(script, call.end() - 1).split(",", 1)[0]))
    caller_urls = list(dict.fromkeys(caller_urls))
    if caller_urls:
        return caller_urls, "callers"
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
    scripts = SCRIPT_RE.findall(html)
    if not scripts:
        raise RouteMapError("no inline script in static/index.html")
    script = max(scripts, key=len)

    routes = read_routes(APP.read_text(encoding="utf-8"))
    if not routes:
        raise RouteMapError("no @app.<method> routes in app.py")

    calls: list[dict[str, object]] = []
    for fetch in FETCH_RE.finditer(script):
        args = _balanced(script, fetch.end() - 1)
        first_arg = args.split(",", 1)[0].strip()
        method_match = METHOD_RE.search(args)
        method = method_match.group("method").upper() if method_match else "GET"
        handler, scope_start = _enclosing_function(script, fetch.start())
        urls, source = _resolve_urls(script, first_arg, handler, scope_start, fetch.start())
        resolutions = [_match(url, method, routes) | {"url": url} for url in urls]
        calls.append(
            {
                "handler": handler,
                "line": _line_of(script, fetch.start()),
                "method": method,
                "url_argument": first_arg[:120],
                "resolved_from": source,
                "urls": resolutions,
            }
        )

    # Route -> callers, so "who uses this endpoint" is answerable both ways.
    callers: dict[tuple[str, str], set[str]] = {}
    for call in calls:
        for resolution in call["urls"]:
            route = resolution.get("route")
            if route:
                callers.setdefault((str(call["method"]), str(route)), set()).add(str(call["handler"]))
    route_rows = []
    for route in routes:
        users = sorted(callers.get((route["method"], route["path"]), set()))
        route_rows.append({**route, "callers": users, "used_by_frontend": bool(users)})

    findings = []
    for call in calls:
        if not call["urls"]:
            findings.append(
                {
                    "kind": "unresolved_url",
                    "handler": call["handler"],
                    "method": call["method"],
                    "url": call["url_argument"],
                    "detail": "the URL argument is neither a literal, a local assignment, nor a helper parameter",
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
        "pinned_source_commit": _ledger_pin(),
        "frontend_sha256": _sha256(FRONTEND),
        "app_sha256": _sha256(APP),
        "totals": {
            "fetch_call_sites": len(calls),
            "resolved_urls": url_count,
            "exact_matches": exact,
            "dynamic_matches": dynamic,
            "unresolved_call_sites": sum(1 for call in calls if not call["urls"]),
            "findings": len(findings),
            "routes": len(routes),
            "routes_used_by_frontend": used_routes,
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
            "dynamic match with its candidate routes instead of being resolved to one.",
            "Routes without a browser caller are not dead: the CLI, the demo tour and the tests call "
            "the same API.",
        ],
    }


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def render_doc(mapping: dict[str, object]) -> str:
    totals = mapping["totals"]
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
        f"findings: {totals['findings']}, unresolved call sites: {totals['unresolved_call_sites']}.",
        f"* {totals['routes']} routes exist; {totals['routes_used_by_frontend']} have a browser caller and "
        f"{totals['routes_without_frontend_caller']} do not.",
        "",
        "## What this page is, and is not",
        "",
        "A matched call is a request the browser **can construct**, not a route that works: nothing",
        "here was executed, and authorization, request bodies and response handling are not checked.",
        "A URL segment chosen at runtime is reported as a *dynamic* match with its candidate routes",
        "rather than being resolved to one. Routes with no browser caller are not dead code — the CLI,",
        "the demo tour and the tests call the same API.",
        "",
        "## Call sites by handler",
        "",
        "| Handler | Line | Method | Resolved from | URL | Match | Route |",
        "|---|---|---|---|---|---|---|",
    ]
    for call in mapping["calls"]:
        if not call["urls"]:
            lines.append(
                f"| `{call['handler']}` | {call['line']} | {call['method']} | {call['resolved_from']} "
                f"| `{_cell(call['url_argument'])}` | unresolved | — |"
            )
            continue
        for resolution in call["urls"]:
            route = resolution.get("route") or "—"
            lines.append(
                f"| `{call['handler']}` | {call['line']} | {call['method']} | {call['resolved_from']} "
                f"| `{_cell(resolution['url'])}` | {resolution['kind']} | `{_cell(route)}` |"
            )

    lines += ["", "## Findings", ""]
    if not mapping["findings"]:
        lines.append("No unmatched call, no method mismatch and no unresolved URL: every call site "
                     "resolves to a route that exists for the method it uses.")
    else:
        lines += ["| Kind | Handler | Method | URL | Detail |", "|---|---|---|---|---|"]
        for finding in mapping["findings"]:
            lines.append(
                f"| {finding['kind']} | `{finding['handler']}` | {finding['method']} "
                f"| `{_cell(finding['url'])}` | {_cell(finding['detail'])} |"
            )

    unused = [route for route in mapping["routes"] if not route["used_by_frontend"]]
    lines += ["", "## Routes with no browser caller", ""]
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
