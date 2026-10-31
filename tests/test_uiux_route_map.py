"""UIUX-P0-03: the route map must describe the calls and the routes it claims.

`product/M5_2_0_UIUX_Route_Map.json` answers a question the repository could not
answer before: which frontend call hits which server route. The risk in an artifact
like this is not a wrong row, it is a *missing* one — a call that quietly failed to
resolve, or a route that quietly lost its caller. So the checks here are about
coverage and cross-agreement, not about the shape of the table:

* every `fetch(` in the inline script appears as a call site (nothing dropped);
* every resolved URL is classified exactly once, and every `exact` route exists in
  `app.py` for the method the call uses;
* a URL whose segment is chosen at runtime is a *dynamic* match with candidates,
  never a silent exact match;
* the map, the source manifest and the ledger share one pin and one frontend digest;
* `--check` fails when a call moves, proved against a temporary copy.
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
MAP = ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
DOC = ROOT / "docs" / "uiux-2-0-route-map.md"
GENERATOR = ROOT / "scripts" / "uiux_route_map.py"
MANIFEST = ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
FRONTEND = ROOT / "static" / "index.html"
APP = ROOT / "app.py"
ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')


def _map() -> dict:
    return json.loads(MAP.read_text(encoding="utf-8"))


def _calls() -> list[dict]:
    return _map()["calls"]


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_route_map", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_fetch_call_site_is_recorded() -> None:
    script = max(re.findall(r"<script\b[^>]*>(.*?)</script>", FRONTEND.read_text(encoding="utf-8"), re.DOTALL), key=len)
    expected = len(re.findall(r"\bfetch\s*\(", script))
    assert expected > 0
    assert len(_calls()) == expected, "a fetch call in the inline script is missing from the map"


def test_every_resolved_url_is_classified_exactly_once() -> None:
    calls = _calls()
    resolved = sum(len(call["urls"]) for call in calls)
    kinds: dict[str, int] = {}
    for call in calls:
        for url in call["urls"]:
            kinds[url["kind"]] = kinds.get(url["kind"], 0) + 1
    totals = _map()["totals"]
    assert totals["resolved_urls"] == resolved
    assert totals["exact_matches"] == kinds.get("exact", 0)
    assert totals["dynamic_matches"] == kinds.get("dynamic", 0)
    assert sum(kinds.values()) == resolved
    assert totals["unresolved_call_sites"] == sum(1 for call in calls if not call["urls"] or call["unresolved_edges"])
    assert totals["unresolved_caller_edges"] == sum(len(call["unresolved_edges"]) for call in calls)
    assert set(kinds) <= {"exact", "dynamic", "method_mismatch", "no_route"}


def test_every_exact_match_names_a_route_that_exists_for_that_method() -> None:
    routes = {(m.group(1).upper(), m.group(2)) for m in ROUTE_RE.finditer(APP.read_text(encoding="utf-8"))}
    for call in _calls():
        for url in call["urls"]:
            if url["kind"] == "exact":
                assert (call["method"], url["route"]) in routes, f"{call['handler']}: {call['method']} {url['route']}"
            if url["kind"] == "dynamic":
                assert url["candidates"], f"{call['handler']}: dynamic match with no candidates"
                for candidate in url["candidates"]:
                    assert (candidate["method"], candidate["path"]) in routes
                    assert candidate["method"] == call["method"]


def test_a_runtime_chosen_segment_is_never_an_exact_match() -> None:
    dynamic = [call for call in _calls() if any(u["kind"] == "dynamic" for u in call["urls"])]
    assert dynamic, "the map claims no runtime-chosen segment, which would be suspicious for this UI"
    for call in dynamic:
        for url in call["urls"]:
            if url["kind"] == "dynamic":
                assert len(url["candidates"]) >= 1
                assert "{param}" in url["url"]


def test_known_calls_map_to_known_routes() -> None:
    by_handler = {call["handler"]: call for call in _calls()}

    onboarding = by_handler["refreshOnboarding"]
    assert onboarding["resolved_from"] == "assignment"
    assert {u["route"] for u in onboarding["urls"]} == {"/api/onboarding/preflight"}

    # A helper that takes the URL as a parameter is resolved from its call sites,
    # including the call sites of helpers that forward their own parameter into
    # it, so the value is reported as forwarded rather than as a direct caller.
    streamer = by_handler["consumeSSE"]
    assert streamer["resolved_from"] == "forwarded"
    streamed = {u["route"] for u in streamer["urls"]}
    assert {"/api/chat/stream", "/api/council/stream", "/api/debate/stream"} <= streamed

    # `${act}` picks one of two literal segments, so both routes stay candidates.
    action = by_handler["modelAction"]
    (url,) = action["urls"]
    assert url["kind"] == "dynamic"
    assert {c["path"] for c in url["candidates"]} == {
        "/api/models/{model_id:path}/load",
        "/api/models/{model_id:path}/unload",
    }


def test_a_helper_that_forwards_its_parameter_keeps_the_route() -> None:
    """Issue #110: `_streamSdlcEndpoint(url)` handed its URL to `consumeSSE(url)`.

    Reading only the fetch helper's direct call sites lost both SDLC routes and
    published them as unused. The forwarding chain must be followed, and it must
    name the UI entry point that supplied the literal.
    """
    streamer = {call["handler"]: call for call in _calls()}["consumeSSE"]
    by_url = {item["url"]: item for item in streamer["urls"]}

    run = by_url["/api/harness/sdlc/run"]
    feedback = by_url["/api/harness/sdlc/feedback"]
    assert run["route"] == "/api/harness/sdlc/run"
    assert feedback["route"] == "/api/harness/sdlc/feedback"
    assert run["forwarded_via"][:2] == ["consumeSSE", "_streamSdlcEndpoint"]
    assert "startSdlcRun" in run["forwarded_via"]
    assert "submitSdlcFeedback" in feedback["forwarded_via"]

    routes = {(route["method"], route["path"]): route for route in _map()["routes"]}
    for path in ("/api/harness/sdlc/run", "/api/harness/sdlc/feedback"):
        row = routes[("POST", path)]
        assert row["callers"] == ["consumeSSE"], f"{path} lost the caller that fetches it"
        assert row["used_by_frontend"] is True


def test_a_runtime_chosen_segment_flags_every_candidate_as_possible() -> None:
    """Issue #110: `${act}` chose between load and unload, but only load got a caller.

    A dynamic match cannot prove which candidate runs, so the caller is recorded
    as possible on *all* of them, and a route is never reported unused while a
    candidate branch can reach it.
    """
    routes = {(route["method"], route["path"]): route for route in _map()["routes"]}
    load = routes[("POST", "/api/models/{model_id:path}/load")]
    unload = routes[("POST", "/api/models/{model_id:path}/unload")]

    for row in (load, unload):
        assert row["used_by_frontend"] is True, f"{row['path']} is reported unused"
        assert "modelAction" in row["possible_callers"]
        assert "modelAction" not in row["callers"], "a runtime-chosen segment is not proof of a route"

    assert {route["caller_certainty"] for route in (load, unload)} == {"possible"}
    for route in _map()["routes"]:
        assert not set(route["callers"]) & set(route["possible_callers"])
        assert route["used_by_frontend"] == bool(route["callers"] or route["possible_callers"])


def test_line_numbers_are_published_in_both_frames() -> None:
    """Issue #110: a script-relative line read as a document line is off by the markup."""
    document = FRONTEND.read_text(encoding="utf-8").splitlines()
    for call in _calls():
        assert isinstance(call["script_line"], int)
        assert isinstance(call["document_line"], int)
        # The script starts below the head, so its lines are always earlier.
        assert call["document_line"] > call["script_line"]
        line = document[call["document_line"] - 1]
        assert "fetch" in line or "fetch(" in line, f"{call['handler']}: {call['document_line']} is not a fetch"

    frames = _map()["line_frames"]
    assert set(frames) == {"script_line", "document_line"}


def test_an_unresolved_forward_is_reported() -> None:
    """Issue #110: an unresolved hand-off must not be published as proof of disuse."""
    module = _load_generator()
    synthetic = """
      async function sink(url) { await fetch(url, { method: "POST" }); }
      async function holder() { const box = { url: "/api/things" }; await sink(box.url); }
    """
    functions = module._read_functions(synthetic)
    at = synthetic.index("fetch(")
    scope = module._scope_at(synthetic, functions, at)
    pairs, how, unresolved = module._resolve_expression(
        synthetic, functions, "url", scope, at
    )
    assert pairs == []
    assert how == "unresolved"
    assert unresolved

    # With a literal at the call site the same shape resolves, so the test above
    # fails for the reason it names and not because the walker never works.
    resolvable = """
      async function sink(url) { await fetch(url, { method: "POST" }); }
      async function holder() { await sink("/api/things"); }
    """
    functions = module._read_functions(resolvable)
    at = resolvable.index("fetch(")
    scope = module._scope_at(resolvable, functions, at)
    pairs, how, unresolved = module._resolve_expression(resolvable, functions, "url", scope, at)
    assert [url for url, _chain in pairs] == ["/api/things"]
    assert how == "callers"
    assert not unresolved

    resolution = _map()["caller_resolution"]
    assert resolution["complete"] is True
    assert resolution["unresolved_call_sites"] == []
    assert "not proof of absence" in resolution["statement"]


def _fixture_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
    routes: str = '@app.post("/api/known")\n@app.post("/api/maybe")\n',
) -> dict:
    module = _load_generator()
    html = tmp_path / "index.html"
    app = tmp_path / "app.py"
    html.write_text("<!doctype html>\n<script>\n" + script + "\n</script>\n", encoding="utf-8")
    app.write_text(routes, encoding="utf-8")
    monkeypatch.setattr(module, "FRONTEND", html)
    monkeypatch.setattr(module, "APP", app)
    return module.build_map()


def test_multiple_structural_candidates_are_all_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(
        tmp_path, monkeypatch,
        'function send() { return fetch("/api/known", {method: "POST"}); }',
        '@app.post("/api/{identifier}")\n@app.post("/api/known")\n',
    )
    assert len(mapping["calls"][0]["urls"][0]["candidates"]) == 2
    for route in mapping["routes"]:
        assert route["possible_callers"] == ["send"]
        assert route["callers"] == []
        assert route["caller_certainty"] == "possible"


def test_partially_resolved_forwarding_keeps_unknown_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(url) { return fetch(url, {method: "POST"}); }
function relay(other, url) { sink(url); }
function known() { relay(callWithComma(1, 2), "/api/known"); }
function unknown(input) { relay("token", input); }
""")
    (call,) = mapping["calls"]
    assert {url["route"] for url in call["urls"]} == {"/api/known"}
    assert call["unresolved_edges"], "one literal must not hide an unresolved caller"
    assert mapping["totals"]["unresolved_call_sites"] == 1
    assert mapping["totals"]["unresolved_caller_edges"] >= 1
    assert mapping["caller_resolution"]["complete"] is False
    assert any(finding["kind"] == "unresolved_caller" for finding in mapping["findings"])
    routes = {route["path"]: route for route in mapping["routes"]}
    assert routes["/api/known"]["used_by_frontend"] is True
    assert routes["/api/maybe"]["used_by_frontend"] is None
    assert routes["/api/maybe"]["caller_certainty"] == "unknown"
    assert mapping["totals"]["routes_unknown_frontend_caller"] == 1


def test_unknown_only_never_asserts_unused_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(url) { return fetch(url, {method: "POST"}); }
function unknown(input) { sink(input); }
""")
    assert not mapping["caller_resolution"]["complete"]
    assert all(route["used_by_frontend"] is None for route in mapping["routes"])
    module = _load_generator()
    page = module.render_doc(mapping)
    assert "routes are not called by the inline script" not in page
    assert "unknown" in page.lower()


def test_same_url_keeps_each_distinct_helper_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(url) { return fetch(url, {method: "POST"}); }
function first() { sink("/api/known"); }
function second() { sink("/api/known"); }
""")
    urls = mapping["calls"][0]["urls"]
    assert {tuple(url["forwarded_via"]) for url in urls} == {("sink", "first"), ("sink", "second")}


def test_literal_inside_an_unknown_expression_is_not_a_resolved_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(url) { return fetch(url, {method: "POST"}); }
function unknown() { sink(rewrite("/api/known")); }
""")
    (call,) = mapping["calls"]
    assert call["urls"] == []
    assert call["unresolved_edges"]
    assert all(route["used_by_frontend"] is None for route in mapping["routes"])


def test_known_conditional_branch_does_not_hide_an_unknown_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(condition, custom) {
  const url = condition ? "/api/known" : custom;
  return fetch(url, {method: "POST"});
}
""")
    (call,) = mapping["calls"]
    assert {url["route"] for url in call["urls"]} == {"/api/known"}
    assert call["unresolved_edges"]
    assert next(row for row in mapping["routes"] if row["path"] == "/api/maybe")["caller_certainty"] == "unknown"


def test_reassigned_url_does_not_hide_an_unresolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(condition, custom) {
  let url = "/api/known";
  if (condition) url = custom;
  return fetch(url, {method: "POST"});
}
""")
    (call,) = mapping["calls"]
    assert call["unresolved_edges"]
    assert next(row for row in mapping["routes"] if row["path"] == "/api/maybe")["caller_certainty"] == "unknown"


def test_cyclic_forwarding_is_bounded_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = _fixture_map(tmp_path, monkeypatch, """
function sink(url) { return fetch(url, {method: "POST"}); }
function first(url) { second(url); }
function second(url) { first(url); sink(url); }
function known() { first("/api/known"); }
""")
    (call,) = mapping["calls"]
    assert "/api/known" in {url["route"] for url in call["urls"]}
    assert any("cycle" in edge["detail"].lower() for edge in call["unresolved_edges"])


def test_long_forwarding_chain_fails_closed_at_the_depth_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = 'function sink(url) { return fetch(url, {method: "POST"}); }\n'
    script += 'function f0(url) { sink(url); }\n'
    script += ''.join(f'function f{i}(url) {{ f{i - 1}(url); }}\n' for i in range(1, 8))
    script += 'function known() { f7("/api/known"); }\n'
    mapping = _fixture_map(tmp_path, monkeypatch, script)
    (call,) = mapping["calls"]
    assert any("depth limit" in edge["detail"] for edge in call["unresolved_edges"])
    assert all(route["used_by_frontend"] is None for route in mapping["routes"])


def test_the_map_agrees_with_the_manifest_and_the_ledger() -> None:
    mapping = _map()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert mapping["frontend_sha256"] == manifest["frontend"]["sha256"]
    assert mapping["app_sha256"] == manifest["api"]["sha256"]
    assert mapping["totals"]["routes"] == manifest["api"]["route_count"]
    assert mapping["pinned_source_commit"] == manifest["pinned_source_commit"]
    assert mapping["pinned_source_commit"] == json.loads(LEDGER.read_text(encoding="utf-8"))["source_commit_sha"]
    assert mapping["frontend_sha256"] == hashlib.sha256(FRONTEND.read_bytes()).hexdigest()


def test_the_generated_page_carries_its_limitations() -> None:
    page = DOC.read_text(encoding="utf-8")
    assert "never hand-edit this page" in page
    for heading in (
        "## Call sites by handler", "## Findings", "## Routes without a resolved browser caller", "## Not verified"
    ):
        assert heading in page
    for note in _map()["not_verified"]:
        assert note[:60] in page


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_check_mode_fails_when_a_call_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_generator()
    mapping = module.build_map()
    mapping["calls"][0]["urls"] = []

    stale_json = tmp_path / "map.json"
    stale_doc = tmp_path / "map.md"
    stale_json.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    stale_doc.write_text(module.render_doc(mapping), encoding="utf-8")
    monkeypatch.setattr(module, "MAP_PATH", stale_json)
    monkeypatch.setattr(module, "DOC_PATH", stale_doc)

    assert module.main(["--check", "--quiet"]) == 1

    payload, page = module._rendered()
    stale_json.write_text(payload, encoding="utf-8")
    stale_doc.write_text(page, encoding="utf-8")
    assert module.main(["--check", "--quiet"]) == 0
