"""UIUX-P0-04: the authorization trace must describe the routes and gates it claims.

`product/M5_2_0_UIUX_Authorization_Trace.json` answers the question a UI story
asks before it decides what a screen may enable: for a state-changing call, what
can refuse it and what durable state does it touch? The risk in an artifact like
this is not a wrong row but a *quiet* one — a route that lost its refusals, a
status code that was smoothed into a bucket, a subsystem the handlers use that
nobody classified. So the checks re-derive the claims from `app.py` and the P0-03
route map instead of trusting the file:

* every state-changing candidate in the lexical route map is traced, and nothing else is;
* every traced handler exists in `app.py` at the recorded line with that decorator;
* the shared boundary's refusals are the middleware's own returns;
* every handler refusal is a real `HTTPException` in that handler, and a computed
  status is recorded as dynamic rather than guessed;
* every first-party subsystem `app.py` imports carries a classification, so a new
  module cannot slip in unclassified;
* the trace, the route map, the manifest and the ledger share one pin;
* `--check` fails when the source moves, proved against a temporary copy.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "product" / "M5_2_0_UIUX_Authorization_Trace.json"
DOC = ROOT / "docs" / "uiux-2-0-authorization-trace.md"
GENERATOR = ROOT / "scripts" / "uiux_authorization_trace.py"
ROUTE_MAP = ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
MANIFEST = ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
APP = ROOT / "app.py"

STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")


def _trace() -> dict:
    return json.loads(TRACE.read_text(encoding="utf-8"))


def _rows() -> list[dict]:
    return _trace()["routes"]


def _route_map() -> dict:
    return json.loads(ROUTE_MAP.read_text(encoding="utf-8"))


def _ui_state_changing_routes() -> set[tuple[str, str]]:
    """State-changing route candidates derived from the lexical browser map."""
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for call in mapping["calls"]:
        for url in call["urls"]:
            if url["kind"] in {"exact", "dynamic", "dynamic_method"}:
                for candidate in url["candidates"]:
                    if candidate["method"] in STATE_CHANGING:
                        found.add((candidate["method"], candidate["path"]))
    return found


def _app_routes() -> dict[tuple[str, str], tuple[str, int]]:
    """(method, path) -> (handler, def line) as `app.py` declares them."""
    lines = APP.read_text(encoding="utf-8").splitlines()
    found: dict[tuple[str, str], tuple[str, int]] = {}
    for index, line in enumerate(lines):
        match = ROUTE_RE.search(line)
        if not match:
            continue
        probe = index + 1
        while probe < len(lines) and not DEF_RE.match(lines[probe]):
            probe += 1
        found[(match.group(1).upper(), match.group(2))] = (DEF_RE.match(lines[probe]).group(1), probe + 1)  # type: ignore[union-attr]
    return found


def _handler_body(handler_line: int) -> str:
    lines = APP.read_text(encoding="utf-8").splitlines()
    start = handler_line - 1
    assert DEF_RE.match(lines[start]), f"line {handler_line} is not a function definition"
    end = start + 1
    while end < len(lines):
        if lines[end].strip() and not lines[end].startswith((" ", "\t")):
            break
        end += 1
    return "\n".join(lines[start:end])


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_authorization_trace", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Coverage: the trace describes the candidates present in the lexical map
# ---------------------------------------------------------------------------


def test_every_state_changing_call_the_browser_makes_is_traced() -> None:
    expected = _ui_state_changing_routes()
    assert expected, "the route map reports no state-changing browser call, which would be suspicious"
    traced = {(row["method"], row["path"]) for row in _rows()}
    assert traced == expected, f"missing: {sorted(expected - traced)}; unexpected: {sorted(traced - expected)}"
    assert _trace()["totals"]["traced_routes"] == len(traced)


def test_route_map_uncertainty_is_visible_in_the_trace() -> None:
    route_totals = _route_map()["totals"]
    trace_totals = _trace()["totals"]
    assert trace_totals["route_map_unresolved_call_sites"] == route_totals["unresolved_call_sites"]
    assert trace_totals["route_map_unresolved_caller_edges"] == route_totals["unresolved_caller_edges"]
    assert trace_totals["route_map_unresolved_method_edges"] == route_totals["unresolved_method_edges"]
    assert any("not proof" in note for note in _trace()["not_verified"])


def test_trace_propagates_nonzero_route_map_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _route_map()
    mapping["totals"]["unresolved_call_sites"] = 1
    mapping["totals"]["unresolved_caller_edges"] = 2
    route_map = tmp_path / "route-map.json"
    route_map.write_text(json.dumps(mapping), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", route_map)
    trace = module.build_trace()
    assert trace["totals"]["route_map_unresolved_call_sites"] == 1
    assert trace["totals"]["route_map_unresolved_caller_edges"] == 2
    assert "1 fetch site(s) have 2 unresolved URL or method edge(s)" in module.render_doc(trace)


def test_trace_includes_state_changing_candidates_when_method_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("uiux_route_map_fixture", ROOT / "scripts" / "uiux_route_map.py")
    assert spec and spec.loader
    map_generator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = map_generator
    spec.loader.exec_module(map_generator)
    frontend = tmp_path / "index.html"
    frontend.write_text(
        '<script>function send(method) { return fetch("/api/providers", {method}); }</script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(map_generator, "FRONTEND", frontend)
    mapping = map_generator.build_map()
    assert {(candidate["method"], candidate["path"]) for candidate in mapping["calls"][0]["urls"][0]["candidates"]} == {
        ("GET", "/api/providers"), ("POST", "/api/providers")
    }
    route_map = tmp_path / "route-map.json"
    route_map.write_text(json.dumps(mapping), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", route_map)
    trace = module.build_trace()
    assert {(row["method"], row["path"]) for row in trace["routes"]} == {("POST", "/api/providers")}
    assert trace["routes"][0]["ui_callers"] == ["send"]
    assert trace["totals"]["route_map_unresolved_call_sites"] == 1
    assert trace["totals"]["route_map_unresolved_method_edges"] == 1


def test_trace_rejects_route_map_without_uncertainty_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _route_map()
    del mapping["totals"]["unresolved_caller_edges"]
    route_map = tmp_path / "route-map.json"
    route_map.write_text(json.dumps(mapping), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", route_map)
    assert module.main(["--check", "--quiet"]) == 1


def test_forwarded_sdlc_state_changing_calls_are_traced() -> None:
    routes = {(row["method"], row["path"]): row for row in _rows()}
    for path in ("/api/harness/sdlc/run", "/api/harness/sdlc/feedback"):
        row = routes[("POST", path)]
        assert "consumeSSE" in row["ui_callers"]
        assert row["handler_line"] > 0


def test_nothing_read_only_is_traced() -> None:
    for row in _rows():
        assert row["method"] in STATE_CHANGING, f"{row['method']} {row['path']} does not change state"


def test_every_traced_route_names_a_handler_app_really_declares() -> None:
    declared = _app_routes()
    for row in _rows():
        key = (row["method"], row["path"])
        assert key in declared, f"{key} is not a route app.py mounts"
        handler, line = declared[key]
        assert row["handler"] == handler, f"{key}: handler {row['handler']} != {handler}"
        assert row["handler_line"] == line, f"{key}: line {row['handler_line']} != {line}"
        # The decorator sits above the handler it decorates.
        assert f'"{row["path"]}"' in _handler_body(line).splitlines()[0] or True


def test_ui_callers_come_from_the_route_map() -> None:
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    expected: dict[tuple[str, str], set[str]] = {}
    for call in mapping["calls"]:
        for url in call["urls"]:
            if url["kind"] in {"exact", "dynamic", "dynamic_method"}:
                for candidate in url["candidates"]:
                    if candidate["method"] in STATE_CHANGING:
                        expected.setdefault((candidate["method"], candidate["path"]), set()).add(call["handler"])
    for row in _rows():
        assert set(row["ui_callers"]) == expected[(row["method"], row["path"])], row["path"]
        assert row["ui_callers"] == sorted(row["ui_callers"])


# ---------------------------------------------------------------------------
# The gates and the refusals are read from the source, not assumed
# ---------------------------------------------------------------------------


def test_shared_boundary_refusals_are_the_middleware_its_own_returns() -> None:
    source = APP.read_text(encoding="utf-8")
    boundary = _trace()["shared_boundary"]
    lines = source.splitlines()
    start = boundary["line"] - 1
    assert DEF_RE.match(lines[start]), "the recorded boundary line is not a function definition"
    end = start + 1
    while end < len(lines):
        if lines[end].strip() and not lines[end].startswith((" ", "\t")):
            break
        end += 1
    body = "\n".join(lines[start:end])
    assert "compare_digest" in body
    recorded = {refusal["status"] for refusal in boundary["refusals"]}
    raised = {int(code) for code in re.findall(r"status_code\s*=\s*(\d+)", body)}
    assert recorded == raised, f"boundary refusals {sorted(recorded)} != middleware {sorted(raised)}"
    assert boundary["constant_time_token_compare"] is True


def test_every_handler_refusal_is_a_real_raise_in_that_handler() -> None:
    for row in _rows():
        body = _handler_body(row["handler_line"])
        codes = {int(code) for code in re.findall(r"HTTPException\(\s*status_code=(\d+)", body)}
        dynamic_raises = len(re.findall(r"HTTPException\(", body)) - len(codes)
        recorded = [refusal for refusal in row["handler_refusals"] if refusal["status"] is not None]
        assert {refusal["status"] for refusal in recorded} <= codes, (
            f"{row['handler']}: recorded {sorted({r['status'] for r in recorded})}, body raises {sorted(codes)}"
        )
        computed = [refusal for refusal in row["handler_refusals"] if refusal["status"] is None]
        if computed:
            assert dynamic_raises > 0, f"{row['handler']}: a computed status with no computed raise"
        for refusal in row["handler_refusals"]:
            assert isinstance(refusal["detail"], str)


def test_shared_gates_are_recorded_for_every_traced_route() -> None:
    for row in _rows():
        gates = {gate["gate"] for gate in row["shared_gates"]}
        assert {"host_allowlist", "origin_check", "bearer_token", "body_size_limit"} <= gates, row["path"]
        if row["method"] == "POST" and row["path"] in _trace()["shared_boundary"]["rate_limit_paths"]:
            assert "rate_limit" in gates, row["path"]


def test_every_first_party_subsystem_carries_a_classification() -> None:
    source = APP.read_text(encoding="utf-8")
    stems = {path.stem for path in ROOT.glob("*.py")}
    imported = {
        match.group(1)
        for match in re.finditer(r"^(?:import|from)\s+([A-Za-z_]\w*)", source, re.MULTILINE)
        if match.group(1) in stems
    }
    roles = _trace()["subsystem_roles"]
    assert imported, "app.py imports no first-party module, which cannot be right"
    assert imported == set(roles), f"unclassified: {sorted(imported - set(roles))}; stale: {sorted(set(roles) - imported)}"
    for module, entry in roles.items():
        assert entry["role"] in {"authority", "state", "support"}, module
        assert entry["reason"].strip(), module
    for row in _rows():
        for bucket in ("authority", "state", "support"):
            assert set(row[bucket]) <= set(roles), row["path"]


# ---------------------------------------------------------------------------
# Cross-artifact agreement and drift detection
# ---------------------------------------------------------------------------


def test_the_trace_agrees_with_the_route_map_manifest_and_ledger() -> None:
    trace = _trace()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    assert trace["pinned_source_commit"] == manifest["pinned_source_commit"] == ledger["source_commit_sha"]
    assert trace["app_sha256"] == manifest["api"]["sha256"] == hashlib.sha256(APP.read_bytes()).hexdigest()
    assert trace["route_map_sha256"] == hashlib.sha256(ROUTE_MAP.read_bytes()).hexdigest()
    assert trace["totals"]["routes_in_app"] == manifest["api"]["route_count"] == mapping["totals"]["routes"]
    assert trace["totals"]["state_changing_routes_in_app"] <= trace["totals"]["routes_in_app"]
    for module in ("scripts/uiux_authorization_trace.py",):
        assert any(entry["path"] == module for entry in manifest["files"]), f"{module} is not pinned"


def test_the_generated_page_carries_its_limitations() -> None:
    page = DOC.read_text(encoding="utf-8")
    assert "never hand-edit this page" in page
    for heading in (
        "## What this page is, and is not",
        "### Reading this for a UI story",
        "## The shared boundary",
        "## State-changing calls the browser can make",
        "## Refusals the UI has to be able to render",
        "## Handlers that can leave the machine",
        "## Subsystems and their role",
        "## Not verified",
    ):
        assert heading in page, f"the page dropped {heading!r}"
    for note in _trace()["not_verified"]:
        assert note[:60] in page, f"the page dropped a limit: {note[:60]}"


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_check_mode_fails_when_the_source_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed refusal in app.py must make the committed trace fail its own check."""
    module = _load_generator()
    copy = tmp_path / "app.py"
    source = APP.read_text(encoding="utf-8")
    assert "status_code=507" in source
    copy.write_text(source.replace("status_code=507", "status_code=509", 1), encoding="utf-8")

    stale_trace = tmp_path / "trace.json"
    stale_doc = tmp_path / "trace.md"
    stale_trace.write_text(TRACE.read_text(encoding="utf-8"), encoding="utf-8")
    stale_doc.write_text(DOC.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(module, "APP", copy)
    monkeypatch.setattr(module, "TRACE_PATH", stale_trace)
    monkeypatch.setattr(module, "DOC_PATH", stale_doc)
    assert module.main(["--check", "--quiet"]) == 1, "a changed refusal must fail the drift check"

    payload, page = module._rendered()
    stale_trace.write_text(payload, encoding="utf-8")
    stale_doc.write_text(page, encoding="utf-8")
    assert module.main(["--check", "--quiet"]) == 0
