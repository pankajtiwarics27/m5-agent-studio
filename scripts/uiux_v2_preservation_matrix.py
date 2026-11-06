#!/usr/bin/env python3
"""Build the UIV2-02 control-to-handler-to-API-to-test preservation matrix.

    python3 scripts/uiux_v2_preservation_matrix.py          # regenerate both artifacts
    python3 scripts/uiux_v2_preservation_matrix.py --check  # fail if either drifted
    python3 scripts/uiux_v2_preservation_matrix.py --json   # print the JSON, write nothing

The matrix is the machine-readable half of story UIV2-02 ("Map feature
preservation and persona/MCP/session contracts"): every interactive control in
``static/index.html`` attributed to its pane, its DOM handler, the API routes
that handler calls (resolved with the same machinery as
``scripts/uiux_route_map.py``), whether those routes exist in ``app.py``, and
which test files mention each route. The three named contracts — chat persona
registry, Council session deletion, MCP credential handling — are classified
existing / partial / absent / unknown with source evidence, and hard assertions
make the generator fail if the code drifts from what the matrix claims, so
``--check`` is a real gate and not a timestamp comparison.

No wall-clock field is written, so ``--check`` compares stable content only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import uiux_control_inventory as uci  # noqa: E402
import uiux_route_map as urm  # noqa: E402

FRONTEND = ROOT / "static" / "index.html"
APP = ROOT / "app.py"
TESTS = ROOT / "tests"
MATRIX_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Preservation_Matrix.json"
DOC_PATH = ROOT / "docs" / "uiv2-v2-preservation-matrix.md"

SCHEMA_VERSION = 1
STORY = "UIV2-02"
HANDLER_RE = re.compile(r"^([A-Za-z_$][\w$]*)\s*\(")
# The picker carries aria-describedby since UIV2-19 (it is described by the line under it), so the
# selector is matched by its id wherever the id sits in the tag.
PERSONA_SELECT_RE = re.compile(
    r'<select\b[^>]*\bid="chatPersona"[^>]*>(.*?)</select>', re.S
)
OPTION_RE = re.compile(r"<option\b[^>]*>(.*?)</option>", re.S)
# `\bPERSONAS\b` never matched `CHAT_PERSONAS`: "_" is a word character, so there is no boundary
# inside the identifier. The registry is named outright now that UIV2-19 shipped it.
REGISTRY_RE = re.compile(r"\bCHAT_PERSONAS\b\s*=")


class MatrixError(RuntimeError):
    """The sources no longer support the claims the matrix makes."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


LEDGER = ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"


def _pin() -> str:
    """The source pin lives in the V2 ledger's source_commit_sha, not in git HEAD:
    CI checks out the PR head, so reading HEAD would make every --check drift."""
    pin = str(json.loads(LEDGER.read_text(encoding="utf-8")).get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise MatrixError(
            f"{LEDGER.name}: source_commit_sha is {pin!r}, not a full 40-char sha; re-pin the ledger first"
        )
    return pin


def _handler_of(control: dict[str, object]) -> str | None:
    for attr in ("onclick", "onchange", "oninput", "onsubmit"):
        raw = control.get(attr)
        if raw:
            match = HANDLER_RE.match(str(raw).strip())
            if match:
                return match.group(1)
    return None


def _calls_by_handler(route_map: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for call in route_map["calls"]:  # type: ignore[index]
        handler = str(call["handler"])
        urls = [
            {
                "method": str(url.get("method") or call.get("method") or ""),
                "route": url.get("route"),
                "kind": url.get("kind"),
            }
            for url in call.get("urls", [])  # type: ignore[union-attr]
        ]
        grouped.setdefault(handler, []).extend(urls)
    return grouped


def _route_exists(routes: list[dict[str, str]], method: str, route: object) -> bool:
    if not isinstance(route, str) or not route:
        return False
    return any(r["method"] == method.upper() and r["path"] == route for r in routes)


def _tests_for_route(route: object, test_index: dict[str, list[str]]) -> list[str]:
    if not isinstance(route, str) or not route:
        return []
    literal = route.split("{")[0].rstrip("/") or "/"
    return sorted(test_index.get(literal, []))


def _build_test_index() -> dict[str, list[str]]:
    """Map each API route literal prefix to the test files that mention it."""
    routes = urm.read_routes(APP.read_text(encoding="utf-8"))
    literals = sorted({r["path"].split("{")[0].rstrip("/") or "/" for r in routes})
    index: dict[str, list[str]] = {literal: [] for literal in literals}
    for test_file in sorted(TESTS.glob("test_*.py")):
        try:
            text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for literal in literals:
            if literal in text:
                index[literal].append(test_file.name)
    return index


def _persona_contract(html: str, script: str) -> dict[str, object]:
    """UIV2-19 re-classified this: the registry the matrix used to refuse now exists.

    The picker was three `<option>` tags whose *value* was a paragraph of prose, so the markup was
    the contract and nothing could describe, remember or fall back from a persona. `CHAT_PERSONAS`
    is the authority now and `#chatPersona` is an empty select the registry fills, which this
    contract proves: every registry entry reaches the picker, the picker ships no option of its own,
    and the request sends the registry's prompt rather than the select's value.
    """
    select = PERSONA_SELECT_RE.search(html)
    if not select:
        raise MatrixError("select#chatPersona not found in static/index.html")
    options = [re.sub(r"\s+", " ", o).strip() for o in OPTION_RE.findall(select.group(1))]
    if options:
        raise MatrixError(
            f"select#chatPersona ships {len(options)} hard-coded <option>(s) again; "
            "the picker must be drawn from CHAT_PERSONAS"
        )
    registry = REGISTRY_RE.search(script)
    if not registry:
        raise MatrixError("CHAT_PERSONAS registry disappeared; the picker would have nothing to draw")
    start = script.index("const CHAT_PERSONAS = [")
    end = script.index("];", start)
    table = script[start:end]
    ids = re.findall(r'id:\s*"([a-z0-9-]+)"', table)
    prompts = re.findall(r'prompt:\s*"((?:[^"\\]|\\.)*)"', table)
    labels = list(dict.fromkeys(re.findall(r'label:\s*"((?:[^"\\]|\\.)*)"', table)))
    if len(ids) < 3 or len(set(ids)) != len(ids):
        raise MatrixError(f"CHAT_PERSONAS must hold at least 3 uniquely identified personas, parsed {ids}")
    if not labels or len(labels) != len(ids) or len(prompts) != len(ids):
        raise MatrixError("every persona needs an id, a label and a prompt")
    if "CHAT_PERSONA_DEFAULT" not in script:
        raise MatrixError("CHAT_PERSONAS names no default persona")
    if "renderChatPersonaPicker" not in script:
        raise MatrixError("no renderer draws the picker from CHAT_PERSONAS")
    if "chatPersonaSelected" not in script:
        raise MatrixError("the request does not read the persona through the registry")
    line = html[: select.start()].count("\n") + 1
    return {
        "question": "Is there a chat persona registry behind the picker?",
        "classification": "present_registry__picker_rendered_from_it",
        "picker": {
            "status": "rendered_from_registry",
            "evidence": (
                f"select#chatPersona at static/index.html:{line} ships no <option>; "
                "renderChatPersonaPicker() fills it from CHAT_PERSONAS on load and on every filter"
            ),
            "option_source": "CHAT_PERSONAS",
        },
        "registry": {
            "status": "present",
            "evidence": f"const CHAT_PERSONAS in the inline script with {len(ids)} persona(s): {', '.join(ids)}",
            "personas": [
                {"id": pid, "label": label}
                for pid, label in zip(ids, labels)
            ],
            "default": "CHAT_PERSONA_DEFAULT",
            "sent_as": "chatPersonaSelected().prompt",
        },
    }


def _council_contract(routes: list[dict[str, str]]) -> dict[str, object]:
    """UIV2-13 re-classified this: the DELETE the matrix used to refuse now exists.

    The matrix used to raise on any `DELETE /api/hall*`, because a session could only be *closed*:
    the transcripts accumulated and there was no way to remove one. The hall now clears every
    session (keeping any that is mid-summon) and deletes one, both as true deletes, and the pane
    reaches them. The re-classification is the point of the guard - it forced this paragraph to be
    written instead of letting the contract quietly describe something that is no longer true.
    """
    hall = sorted(
        {f"{r['method']} {r['path']}" for r in routes if r["path"].startswith("/api/hall")}
    )
    deletes = sorted(
        f"{r['method']} {r['path']}"
        for r in routes
        if r["method"] == "DELETE" and r["path"].startswith("/api/hall")
    )
    if "DELETE /api/hall/sessions" not in deletes or "DELETE /api/hall/sessions/{session_id}" not in deletes:
        raise MatrixError(
            f"the Council's clear/delete routes changed shape: {deletes or 'none'}"
        )
    return {
        "question": "Can a Council (hall) session be deleted or cleared through the API?",
        "classification": "full__lifecycle_with_delete",
        "routes": hall,
        "delete": {
            "status": "existing",
            "evidence": (
                "DELETE /api/hall/sessions clears every session except those running a summon "
                "(clear_hall_sessions_db(exclude=running)); DELETE /api/hall/sessions/{id} removes "
                "one and answers 409 while its summon is in flight. Both are true deletes "
                "(archived: false) and touch no workspace file, because a hall session owns none."
            ),
            "routes": deletes,
        },
        "close": {
            "status": "existing",
            "evidence": "POST /api/hall/sessions/{session_id}/close still exists (app.py)",
        },
    }


def _mcp_contract(
    html: str, app_text: str, routes: list[dict[str, str]]
) -> dict[str, object]:
    mcp = sorted(
        {f"{r['method']} {r['path']}" for r in routes if r["path"].startswith("/api/mcp")}
    )
    list_fn = re.search(
        r'@app\.get\("/api/mcp/servers"\)\s*\nasync def api_list_mcp_servers.*?\n(?=@|\Z)',
        app_text,
        re.S,
    )
    if not list_fn or "include_secret=False" not in list_fn.group(0):
        raise MatrixError("GET /api/mcp/servers no longer masks secrets")
    if "masked_token" not in app_text or "_keyring_read" not in app_text:
        raise MatrixError("MCP token masking/keyring helpers disappeared from app.py")
    token_tag = re.search(r"<input\b[^>]*id=\"mcpToken\"[^>]*>", html)
    if not token_tag or 'type="password"' not in token_tag.group(0):
        raise MatrixError("the MCP token field is no longer a password input")
    if not any(r["method"] == "DELETE" and r["path"] == "/api/mcp/servers/{alias}" for r in routes):
        raise MatrixError("DELETE /api/mcp/servers/{alias} disappeared")
    return {
        "question": "How are MCP credentials stored and exposed?",
        "classification": "existing__masked",
        "routes": mcp,
        "storage": {
            "status": "existing",
            "evidence": "mcp_servers.auth_token column in the SQLite store, resolved through _keyring_read (keyring-backed)",
        },
        "api_surface": {
            "status": "existing",
            "evidence": "GET /api/mcp/servers answers with list_mcp_servers(include_secret=False); rows expose only has_token and masked_token (_mask_secret), never auth_token",
        },
        "ui_field": {
            "status": "existing",
            "evidence": 'input#mcpToken is type="password" with placeholder "Optional for free servers"; blank keeps the stored value',
        },
    }


def build_matrix() -> tuple[dict[str, object], str]:
    html = FRONTEND.read_text(encoding="utf-8")
    app_text = APP.read_text(encoding="utf-8")
    script, _ = urm.inline_script(html)
    routes = urm.read_routes(app_text)
    if not routes:
        raise MatrixError("no @app.<method> routes parsed from app.py")
    route_map = urm.build_map()
    calls_by_handler = _calls_by_handler(route_map)
    test_index = _build_test_index()

    panes, chrome, section_ids = uci.parse_markup(html)
    if len(section_ids) != 6:
        raise MatrixError(f"expected six pane sections, parsed {section_ids}")

    controls: list[dict[str, object]] = []
    status_counts: dict[str, int] = {}

    def _row(pane: str, control: dict[str, object]) -> None:
        handler = _handler_of(control)
        calls = calls_by_handler.get(handler, []) if handler else []
        if not handler:
            status = "no-handler"
        elif not calls:
            status = "no-api"
        elif all(
            _route_exists(routes, str(c["method"]), c["route"]) for c in calls
        ) and all(c["kind"] in ("exact", "dynamic") for c in calls):
            status = "existing"
        elif any(c["kind"] in ("no_route", "method_mismatch") for c in calls):
            status = "absent-route"
        else:
            status = "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        controls.append(
            {
                "pane": pane,
                "id": control.get("id"),
                "tag": control.get("tag"),
                "text": control.get("text"),
                "handler": handler,
                "calls": calls,
                "api_status": status,
                "tests": sorted(
                    {t for c in calls for t in _tests_for_route(c["route"], test_index)}
                ),
            }
        )

    for dom_id in section_ids:
        for control in panes.get(dom_id, []):
            _row(dom_id, control)
    for control in chrome:
        _row("chrome", control)

    contracts = {
        "chat_persona_registry": _persona_contract(html, script),
        "council_session_deletion": _council_contract(routes),
        "mcp_credential_handling": _mcp_contract(html, app_text, routes),
    }

    matrix: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_v2_preservation_matrix",
        "program": "M5-UIUX-V2-VISUAL-CONFORMANCE",
        "story": STORY,
        "generator": "scripts/uiux_v2_preservation_matrix.py",
        "regenerate_with": "python3 scripts/uiux_v2_preservation_matrix.py",
        "check_with": "python3 scripts/uiux_v2_preservation_matrix.py --check",
        "statement": (
            "Control -> DOM handler -> API route -> test coverage for every interactive control in "
            "static/index.html, plus the three UIV2-02 contract investigations. api_status is "
            "existing when every call the handler makes resolves to a mounted @app route, "
            "absent-route when a call matches no mounted route, unknown when the resolver left an "
            "edge open, no-api for handlers that never fetch, and no-handler for controls without "
            "an inline handler. The contracts distinguish existing / partial / absent / unknown "
            "and cite file evidence; the generator hard-fails if the sources drift from the "
            "classification, so --check is a gate."
        ),
        "pinned_source_commit": _pin(),
        "frontend_sha256": _sha256(FRONTEND),
        "app_sha256": _sha256(APP),
        "totals": {
            "controls": len(controls),
            "by_pane": {
                dom_id: sum(1 for c in controls if c["pane"] == dom_id) for dom_id in section_ids
            },
            "chrome_controls": sum(1 for c in controls if c["pane"] == "chrome"),
            "api_status_counts": dict(sorted(status_counts.items())),
            "mounted_routes": len(routes),
            "fetch_call_sites": route_map["totals"]["fetch_call_sites"],  # type: ignore[index]
        },
        "controls": controls,
        "contracts": contracts,
        "not_verified": [
            "This is a static trace: a handler that exists is not proof its route is reachable in "
            "every state, and script-rendered controls from template strings are not re-walked here.",
            "tests lists files whose source mentions the route literal; it proves a test mentions "
            "the route, not that every branch of the handler is covered.",
            "46 mounted routes have no observed frontend caller (see the route map); absence of an "
            "observed caller is not absence of a caller.",
        ],
    }
    return matrix, _render_doc(matrix)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def _render_doc(matrix: dict[str, object]) -> str:
    totals = matrix["totals"]  # type: ignore[index]
    contracts = matrix["contracts"]  # type: ignore[index]
    lines = [
        "# M5 2.0 UI/UX V2 — preservation matrix (UIV2-02)",
        "",
        "Generated by `scripts/uiux_v2_preservation_matrix.py` from `static/index.html` and "
        "`app.py` at the pinned commit; the machine-readable matrix is "
        "[`product/M5_2_0_UIUX_V2_Preservation_Matrix.json`](../product/M5_2_0_UIUX_V2_Preservation_Matrix.json). "
        "`python3 scripts/uiux_v2_preservation_matrix.py --check` fails CI on drift.",
        "",
        f"**{totals['controls']} controls** traced across the six panes and the shell chrome.",
        "",
        "| api_status | controls | meaning |",
        "|---|---|---|",
    ]
    meanings = {
        "existing": "every call the handler makes resolves to a mounted route",
        "no-api": "handler performs no fetch (client-side state only)",
        "no-handler": "control without an inline handler",
        "absent-route": "a call matches no mounted route — investigate before shipping",
        "unknown": "the resolver left an edge open",
    }
    for status, count in totals["api_status_counts"].items():  # type: ignore[union-attr]
        lines.append(f"| `{status}` | {count} | {meanings.get(status, '')} |")
    lines += [
        "",
        "## Contract investigations",
        "",
    ]
    for key, contract in contracts.items():  # type: ignore[union-attr]
        c = contract  # type: ignore[assignment]
        lines.append(f"### `{key}` — classification `{c['classification']}`")
        lines.append("")
        lines.append(f"**{c['question']}**")
        lines.append("")
        for name, part in c.items():  # type: ignore[union-attr]
            if name in ("question", "classification") or not isinstance(part, dict):
                continue
            evidence = part.get("evidence", "")
            lines.append(f"- **{name}**: `{part.get('status')}` — {evidence}")
        if key == "council_session_deletion":
            lines.append("- routes: " + ", ".join(f"`{r}`" for r in c["routes"]))  # type: ignore[union-attr]
        if key == "mcp_credential_handling":
            lines.append("- routes: " + ", ".join(f"`{r}`" for r in c["routes"]))  # type: ignore[union-attr]
        lines.append("")
    lines += [
        "## Limits",
        "",
        "A static trace, not a rendered walk: see the `not_verified` list in the JSON. Baseline "
        "screenshots and the source pin for this program are in "
        "[uiv2-v2-baseline-evidence.md](uiv2-v2-baseline-evidence.md).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if either artifact drifted")
    parser.add_argument("--json", action="store_true", help="print the matrix and write nothing")
    args = parser.parse_args(argv)

    try:
        matrix, page = build_matrix()
    except (MatrixError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(matrix, indent=2) + "\n"
    if args.json:
        sys.stdout.write(payload)
        return 0

    if args.check:
        drifted = []
        for path, expected in ((MATRIX_PATH, payload), (DOC_PATH, page)):
            if not path.is_file():
                print(f"error: {path.name} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(path.name)
        if drifted:
            print(
                "error: the V2 preservation matrix has drifted: " + ", ".join(drifted)
                + "\n       Regenerate it: python3 scripts/uiux_v2_preservation_matrix.py",
                file=sys.stderr,
            )
            return 1
        totals = matrix["totals"]  # type: ignore[index]
        print(
            "V2 preservation matrix is up to date: "
            f"{totals['controls']} controls, {totals['mounted_routes']} mounted routes, "
            f"api_status={totals['api_status_counts']}"
        )
        return 0

    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATRIX_PATH.write_text(payload, encoding="utf-8")
    DOC_PATH.write_text(page, encoding="utf-8")
    totals = matrix["totals"]  # type: ignore[index]
    print(
        f"Wrote {MATRIX_PATH.name} and {DOC_PATH.name}: {totals['controls']} controls, "
        f"api_status={totals['api_status_counts']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
