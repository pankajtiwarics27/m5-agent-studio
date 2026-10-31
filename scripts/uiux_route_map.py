#!/usr/bin/env python3
"""Statically map browser fetch calls in ``static/index.html`` (UIUX-P0-03).

    python3 scripts/uiux_route_map.py            # regenerate JSON + docs
    python3 scripts/uiux_route_map.py --check    # fail if either drifted
    python3 scripts/uiux_route_map.py --json     # print the map

Follow supported literal, assignment and parameter-forwarding expressions to
service decorators in ``app.py``. Preserve helper chains, dynamic candidates,
script/document line frames, and every unresolved URL or method edge, including
partial ones. Unknown methods retain path-compatible candidates for all methods.

This is a bounded lexical scan, not a complete JavaScript parser or a runtime
observation. A route without a resolved caller is not proof of an unused route;
a match is not proof that a call works. Auth and response semantics are
addressed separately by UIUX-P0-04.
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

SCHEMA_VERSION = "2.0.0"
STORY = "UIUX-P0-03"

SCRIPT_RE = re.compile(r"<script\b[^>]*>(?P<body>.*?)</script>", re.DOTALL)
FETCH_RE = re.compile(r"\bfetch\s*\(")
FUNCTION_RE = re.compile(r"(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(")
PROPERTY_RE = re.compile(r"""^(?P<key>[A-Za-z_$][\w$]*|"[A-Za-z_$][\w$]*"|'[A-Za-z_$][\w$]*')\s*:\s*(?P<value>.*)$""", re.DOTALL)
METHOD_LITERAL_RE = re.compile(r"""(?P<quote>["'])(?P<method>[A-Za-z]+)(?P=quote)""")
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


def _conditional_arms(expression: str) -> tuple[str, str] | None:
    """Return the two top-level ternary branches, if recognizable."""
    quote: str | None = None
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    question: int | None = None
    nested = 0
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char == "?":
            # Optional chaining and null-coalescing are not ternary questions.
            if (index + 1 < len(expression) and expression[index + 1] in "?.") or (
                index > 0 and expression[index - 1] == "?"
            ):
                index += 1
                continue
            if question is None:
                question = index
            else:
                nested += 1
        elif not stack and char == ":" and question is not None:
            if nested:
                nested -= 1
            else:
                return expression[question + 1:index].strip(), expression[index + 1:].strip()
        index += 1
    return None


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


def _contains_comment(text: str) -> bool:
    """A comment outside a quoted value makes the options syntax unsupported."""
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "/" and index + 1 < len(text) and text[index + 1] in "/*":
            return True
        index += 1
    return False


def _fetch_method(arguments: list[str]) -> tuple[str | None, str]:
    """Read only a recognizable top-level RequestInit method, or report unknown.

    GET is the default when there is no options argument or the literal options
    object has no method. Variables, computed properties, spreads, comments and
    dynamic methods are not guessed. Nested `headers.method` is not a method.
    """
    if len(arguments) < 2:
        return "GET", ""
    options = arguments[1].strip()
    if not options.startswith("{") or not options.endswith("}") or _brace_end(options, 0) != len(options):
        return None, "fetch options are not a recognizable object literal"
    if _contains_comment(options):
        return None, "fetch options contain unsupported comments"

    method: str | None = "GET"
    for prop in _split_args(options[1:-1]):
        if not prop:
            continue
        if IDENT_RE.fullmatch(prop):
            if prop == "method":
                method = None  # object shorthand can hold any value
            continue
        match = PROPERTY_RE.fullmatch(prop)
        if not match:
            return None, "fetch options contain an unsupported property or spread"
        key = match.group("key").strip("'\"")
        if key == "__proto__":
            return None, "fetch options can inherit an unknown method"
        if key != "method":
            continue
        literal = METHOD_LITERAL_RE.fullmatch(match.group("value").strip())
        if not literal:
            method = None
            continue
        value = literal.group("method")
        # Fetch normalizes these standard methods; a mixed/lowercase extension
        # method may be case-sensitive, so do not infer it as an upper-case route.
        canonical = value.upper()
        method = canonical if value == canonical or canonical in {"DELETE", "GET", "HEAD", "OPTIONS", "POST", "PUT"} else None
    return (method, "" if method is not None else "fetch method is not a supported literal")


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


def _local_assignment(script: str, name: str, start: int, end: int) -> tuple[str, int] | None:
    """The last declaration expression and its position in this function."""
    matches = list(re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*([^;]+);", script[start:end]))
    if not matches:
        return None
    last = matches[-1]
    return last.group(1).strip(), start + last.start(1)


def _may_be_reassigned(script: str, name: str, start: int, end: int, has_declaration: bool) -> bool:
    """A later mutation is unknown, not proof the declaration still holds."""
    scope = script[start:end]
    writes = list(re.finditer(
        rf"(?<![\w$.]){re.escape(name)}\s*(?:\+\+|--|(?:[+*/%&|^?-]|\|\||&&|\?\?)?=(?!=|>))", scope
    ))
    prefix_update = re.search(rf"(?:\+\+|--)\s*{re.escape(name)}\b", scope)
    return bool(prefix_update) or len(writes) > (1 if has_declaration else 0)


def _call_sites(script: str, name: str) -> list[tuple[str, int]]:
    """Every call to `name`: its argument text and the offset of the call."""
    sites: list[tuple[str, int]] = []
    for match in re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*\(", script):
        if re.search(r"\bfunction\s*$", script[:match.start()]):
            continue  # definition, not a call
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
) -> tuple[list[tuple[str, list[str]]], str, list[dict[str, object]]]:
    """Return resolved (URL, helper chain) pairs, their source, and unknown edges.

    Preserve every failed branch, even if another branch resolved. This bounded
    lexical walk is not a complete JavaScript parser or a runtime observation.
    """
    expression = expression.strip()

    def unresolved(detail: str) -> list[dict[str, object]]:
        return [{"handler": str(scope["name"]), "script_line": _line_of(script, at),
                 "argument": expression[:120], "detail": detail}]

    while expression.startswith("("):
        inner = _balanced(expression, 0)
        if expression != f"({inner})":
            break
        expression = inner.strip()

    if STRING_RE.fullmatch(expression):
        direct = _urls_in(expression)
        if direct:
            return [(url, []) for url in direct], "literal", []
        return [], "unresolved", unresolved("literal is not a recognized service URL")

    arms = _conditional_arms(expression)
    if arms:
        if depth >= MAX_RESOLUTION_DEPTH:
            return [], "unresolved", unresolved("URL expression depth limit reached")
        collected: list[tuple[str, list[str]]] = []
        issues: list[dict[str, object]] = []
        for arm in arms:
            pairs, _how, unknown = _resolve_expression(script, functions, arm, scope, at, depth + 1, seen)
            collected.extend(pairs)
            issues.extend(unknown)
        return collected, "literal" if collected else "unresolved", issues

    if not IDENT_RE.fullmatch(expression):
        return [], "unresolved", unresolved("unsupported URL expression")
    marker = f"{scope['name']}:{expression}"
    if marker in seen:
        return [], "unresolved", unresolved("cycle in caller/assignment forwarding")
    if depth >= MAX_RESOLUTION_DEPTH:
        return [], "unresolved", unresolved("caller/assignment forwarding depth limit reached")

    start = int(scope["body_start"])
    assignment = _local_assignment(script, expression, start, at)
    mutation_issues = (
        unresolved("URL may be reassigned before use")
        if _may_be_reassigned(script, expression, start, at, assignment is not None) else []
    )
    if assignment is not None:
        pairs, _how, issues = _resolve_expression(
            script, functions, assignment[0], scope, assignment[1], depth + 1, seen | {marker}
        )
        return pairs, "assignment" if pairs else "unresolved", mutation_issues + issues

    # Follow the corresponding argument at every call site, not just the
    # successful ones. Different caller chains may supply the same URL.
    params = [str(part) for part in scope["params"]]  # type: ignore[union-attr]
    if expression in params and str(scope["name"]) != "top-level":
        index = params.index(expression)
        collected = []
        issues = mutation_issues
        hop_kinds: set[str] = set()
        sites = _call_sites(script, str(scope["name"]))
        if not sites:
            issues.extend(unresolved("no statically located callers for parameter"))
        for args_text, call_at in sites:
            args = _split_args(args_text)
            caller_scope = _scope_at(script, functions, call_at)
            if index >= len(args) or not args[index]:
                issues.append({"handler": str(caller_scope["name"]), "script_line": _line_of(script, call_at),
                               "argument": "", "detail": f"missing argument for {scope['name']} parameter {index}"})
                continue
            pairs, how, unknown = _resolve_expression(
                script, functions, args[index], caller_scope, call_at, depth + 1, seen | {marker}
            )
            issues.extend(unknown)
            if pairs:
                hop_kinds.add(how)
            for url, chain in pairs:
                pair = (url, [str(scope["name"]), *(chain or [str(caller_scope["name"])])])
                if pair not in collected:
                    collected.append(pair)
        how = ("callers" if hop_kinds == {"literal"} else "forwarded") if collected else "unresolved"
        return collected, how, issues
    return [], "unresolved", mutation_issues + unresolved("not a supported local assignment or named parameter")


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


def _match_unknown_method(url: str, routes: list[dict[str, str]]) -> dict[str, object]:
    """Preserve every path-compatible route when the request method is unknown."""
    url_segments = _route_segments(url)
    candidates = [route for route in routes if _route_fit(_route_segments(route["path"]), url_segments)[0]]
    return {
        "kind": "dynamic_method" if candidates else "no_route",
        "route": None,
        "candidates": candidates,
    }


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
        arguments = _split_args(argument_text)
        first_arg = arguments[0] if arguments else ""
        method, method_issue = _fetch_method(arguments)
        scope = _scope_at(script, functions, fetch.start())
        pairs, source, issues = _resolve_expression(script, functions, first_arg, scope, fetch.start())
        if method is None:
            issues.append({
                "kind": "unresolved_method", "handler": str(scope["name"]),
                "script_line": _line_of(script, fetch.start()),
                "argument": arguments[1][:120] if len(arguments) > 1 else "",
                "detail": method_issue,
            })
        for issue in issues:
            issue["document_line"] = _line_of(html, script_offset) + int(issue["script_line"]) - 1
        resolutions = [
            (_match_unknown_method(url, routes) if method is None else _match(url, method, routes))
            | {"url": url, "forwarded_via": chain}
            for url, chain in pairs
        ]
        calls.append(
            {
                "handler": str(scope["name"]),
                "script_line": _line_of(script, fetch.start()),
                "document_line": _line_of(html, script_offset + fetch.start()),
                "method": method,
                "url_argument": first_arg[:120],
                "resolved_from": source,
                "urls": resolutions,
                "unresolved_edges": issues,
            }
        )

    # A unique structural match and an ambiguous candidate are different kinds
    # of static evidence; neither proves runtime execution. Index every possible
    # candidate even if the matcher selected a primary route.
    certain: dict[tuple[str, str], set[str]] = {}
    possible: dict[tuple[str, str], set[str]] = {}
    for call in calls:
        handler = str(call["handler"])
        method = str(call["method"])
        for resolution in call["urls"]:
            if resolution["kind"] not in {"exact", "dynamic", "dynamic_method"}:
                continue
            candidates = resolution["candidates"]
            if resolution["kind"] == "exact" and len(candidates) == 1:
                certain.setdefault((method, str(resolution["route"])), set()).add(handler)
            else:
                for candidate in candidates:
                    possible.setdefault((str(candidate["method"]), str(candidate["path"])), set()).add(handler)

    incomplete = any(not call["urls"] or call["unresolved_edges"] for call in calls)
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
                "caller_certainty": "certain" if sure else (
                    "possible" if maybe else ("unknown" if incomplete else "none")
                ),
                "used_by_frontend": True if sure or maybe else (None if incomplete else False),
            }
        )

    findings = []
    for call in calls:
        for edge in call["unresolved_edges"]:
            findings.append({
                "kind": edge.get("kind") or (
                    "unresolved_caller" if call["resolved_from"] in {"callers", "forwarded"} else "unresolved_url"
                ),
                "handler": edge["handler"], "document_line": edge["document_line"], "method": call["method"],
                "url": edge["argument"], "detail": edge["detail"],
            })
        for resolution in call["urls"]:
            if resolution["kind"] in {"exact", "dynamic", "dynamic_method"}:
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
    dynamic_method = sum(1 for call in calls for r in call["urls"] if r["kind"] == "dynamic_method")
    used_routes = sum(1 for row in route_rows if row["used_by_frontend"] is True)
    unknown_routes = sum(1 for row in route_rows if row["used_by_frontend"] is None)
    unobserved_routes = sum(1 for row in route_rows if row["used_by_frontend"] is False)
    certain_routes = sum(1 for row in route_rows if row["caller_certainty"] == "certain")
    possible_only = [str(row["path"]) for row in route_rows if row["caller_certainty"] == "possible"]
    unresolved_handlers = [str(call["handler"]) for call in calls if not call["urls"] or call["unresolved_edges"]]
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
            "A lexical inventory of fetch calls, statically resolved routes and possible dynamic candidates. "
            "Unresolved caller edges and routes without a resolved caller are reported without asserting "
            "runtime reachability or complete JavaScript analysis."
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
                "No unsupported caller edges were detected by this lexical scan. A missing observed caller "
                "is not proof of absence."
                if not unresolved_handlers
                else (
                    "Some URL or method edges could not be resolved, even if the same fetch also has known URLs. "
                    "Routes without known callers have unknown browser usage, not proof of disuse."
                )
            ),
        },
        "totals": {
            "fetch_call_sites": len(calls),
            "resolved_urls": url_count,
            "exact_matches": exact,
            "dynamic_matches": dynamic,
            "dynamic_method_matches": dynamic_method,
            "forwarded_call_sites": by_source.get("forwarded", 0),
            "unresolved_call_sites": sum(1 for call in calls if not call["urls"] or call["unresolved_edges"]),
            "unresolved_caller_edges": sum(len(call["unresolved_edges"]) for call in calls),
            "unresolved_method_edges": sum(
                1 for call in calls for edge in call["unresolved_edges"] if edge.get("kind") == "unresolved_method"
            ),
            "findings": len(findings),
            "routes": len(routes),
            "routes_with_resolved_or_possible_caller": used_routes,
            "routes_with_certain_caller": certain_routes,
            "routes_only_possible_caller": len(possible_only),
            "routes_no_caller_observed": unobserved_routes,
            "routes_unknown_frontend_caller": unknown_routes,
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
            "URL forwarding, assignments and conditional branches are followed at most four resolution "
            "steps; helper chains are published as `forwarded_via`. Unsupported, cyclic or deeper paths are recorded "
            "as unresolved edges even if another caller resolves.",
            "Only a recognizable top-level literal RequestInit method is read; variable options, computed "
            "properties, spreads and unsupported forms are unresolved. Their path-compatible routes for "
            "all methods are possible candidates, not certain calls.",
            "Line numbers are published in two frames: `script_line` is relative to the inline script, "
            "`document_line` to static/index.html. Reading a script line as a document line is off by "
            "the markup above the script.",
            "This lexical scan is not a complete JavaScript parser. No resolved caller does not prove "
            "a route is unused; the CLI, demo tour, tests or unrecognized browser paths may call it.",
            "When any caller edge is unresolved, routes without known callers have unknown browser usage "
            "rather than a false no-caller assertion.",
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
        "It is the `UIUX-P0-03` artifact: lexically located `fetch` calls, their enclosing",
        "functions and structural route candidates, derived from the inline script and the",
        "route table, not from handler names.",
        "",
        f"* Pinned source commit: `{mapping['pinned_source_commit']}`.",
        f"* Frontend digest: `{mapping['frontend_sha256'][:16]}…` · `app.py` digest: `{mapping['app_sha256'][:16]}…`.",
        f"* {totals['fetch_call_sites']} call sites resolve to {totals['resolved_urls']} URLs — "
        f"exact: {totals['exact_matches']}, dynamic URL: {totals['dynamic_matches']}, "
        f"unknown method: {totals['dynamic_method_matches']}, "
        f"forwarded through a helper: {totals['forwarded_call_sites']}, "
        f"findings: {totals['findings']}, unresolved sites: {totals['unresolved_call_sites']}, "
        f"unresolved edges: {totals['unresolved_caller_edges']} "
        f"({totals['unresolved_method_edges']} method).",
        f"* {totals['routes']} routes exist; {totals['routes_with_resolved_or_possible_caller']} have a "
        f"resolved or possible caller ({totals['routes_with_certain_caller']} unique structural matches, "
        f"{totals['routes_only_possible_caller']} only possible candidates); "
        f"{totals['routes_no_caller_observed']} have none observed and "
        f"{totals['routes_unknown_frontend_caller']} are unknown due to unresolved edges.",
        "",
        "## What this page is, and is not",
        "",
        "A matched call is a request the browser **can construct**, not a route that works: nothing",
        "here was executed, and authorization, request bodies and response handling are not checked.",
        "A URL segment chosen at runtime is reported as a *dynamic* match with every candidate route,",
        "not a certain runtime call. Only top-level literal request methods are read; dynamic options",
        "leave the method unknown and retain every path-compatible method as a possible candidate.",
        "Helper chains appear in `Via`; unresolved URL or method edges remain findings even when another",
        "caller resolves. This is a lexical scan, not a complete JavaScript parser.",
        "A missing observed caller is not proof of absence; the CLI, demo tour or tests may also call a route.",
        "",
        "In JSON, `callers` are unique structural matches and `possible_callers` are ambiguous candidates;",
        "neither proves execution. `caller_certainty` is `certain`, `possible`, `none` or `unknown`,",
        "with `used_by_frontend` respectively true, true, false or null. When any edge is unresolved,",
        "routes without known callers are `unknown`, not `none`. In schema 2, the old",
        "`routes_used_by_frontend` total is `routes_with_resolved_or_possible_caller`;",
        "`routes_without_frontend_caller` is replaced by `routes_no_caller_observed` and",
        "`routes_unknown_frontend_caller`. Consumers must handle nullable `used_by_frontend` and",
        "nullable call `method`; `dynamic_method` resolutions contain candidates across methods.",
        "The existing `unresolved_caller_edges` total includes method edges; the subset is also",
        "reported as `unresolved_method_edges`.",
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
        method = call["method"] or "unknown"
        if not call["urls"]:
            lines.append(
                f"| `{call['handler']}` | {call['script_line']} | {call['document_line']} | {method} "
                f"| {call['resolved_from']} | — | `{_cell(call['url_argument'])}` | unresolved | — |"
            )
            continue
        for item in call["urls"]:
            candidates = item["candidates"]
            if item["kind"] == "dynamic_method":
                route = " or ".join(f"{candidate['method']} {candidate['path']}" for candidate in candidates)
            else:
                route = " or ".join(candidate["path"] for candidate in candidates) if len(candidates) > 1 else (
                    item.get("route") or "—"
                )
            via = " → ".join(f"`{name}`" for name in item["forwarded_via"]) or "—"
            lines.append(
                f"| `{call['handler']}` | {call['script_line']} | {call['document_line']} | {method} "
                f"| {call['resolved_from']} | {via} | `{_cell(item['url'])}` | {item['kind']} | `{_cell(route)}` |"
            )

    lines += ["", "## Findings", ""]
    if not mapping["findings"]:
        lines.append("No unmatched URL, method mismatch or unresolved edge was detected in this scan.")
    else:
        lines += ["| Kind | Handler | Document line | Method | URL or options | Detail |", "|---|---|---|---|---|---|"]
        for finding in mapping["findings"]:
            lines.append(
                f"| {finding['kind']} | `{finding['handler']}` | {_cell(finding.get('document_line'))} "
                f"| {finding['method'] or 'unknown'} | `{_cell(finding['url'])}` | {_cell(finding['detail'])} |"
            )

    possible = [route for route in mapping["routes"] if route["caller_certainty"] == "possible"]
    lines += ["", "## Routes with only possible browser callers", ""]
    if not possible:
        lines.append("No routes depend solely on an ambiguous candidate in this scan.")
    else:
        lines.append(
            f"{len(possible)} route(s) have only possible callers in this scan. The actual URL and method "
            "at runtime determine which candidate route is reached, if the call executes."
        )
        lines += ["", "| Method | Route | Possible callers |", "|---|---|---|"]
        for route in possible:
            names = ", ".join(f"`{name}`" for name in route["possible_callers"])
            lines.append(f"| {route['method']} | `{route['path']}` | {names} |")

    unobserved = [route for route in mapping["routes"] if route["used_by_frontend"] is not True]
    lines += ["", "## Routes without a resolved browser caller", ""]
    if not resolution["complete"]:
        lines.append(
            f"**Unknown.** {len(resolution['unresolved_call_sites'])} fetch site(s) have unresolved "
            "edges: " + ", ".join(f"`{name}`" for name in resolution["unresolved_call_sites"]) + "."
        )
    if not unobserved:
        lines.append("Every route has a resolved or possible browser caller in this scan.")
    else:
        lines.append(
            f"{len(unobserved)} of {mapping['totals']['routes']} routes have no resolved browser caller "
            "in this scan. That is not proof of absence or dead code; routes marked unknown may be "
            "reached by an unresolved edge."
        )
        lines += ["", "| Method | Route | Caller status |", "|---|---|---|"]
        for route in unobserved:
            lines.append(f"| {route['method']} | `{route['path']}` | {route['caller_certainty']} |")

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
                f"{totals['dynamic_matches']} dynamic URL and {totals['dynamic_method_matches']} unknown-method matches, "
                f"{totals['findings']} findings, "
                f"{totals['routes_no_caller_observed']} routes without an observed caller, "
                f"{totals['routes_unknown_frontend_caller']} unknown"
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
