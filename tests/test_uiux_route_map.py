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
    assert totals["unresolved_call_sites"] == sum(1 for call in calls if not call["urls"])
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

    # A helper that takes the URL as a parameter is resolved from its call sites.
    streamer = by_handler["consumeSSE"]
    assert streamer["resolved_from"] == "callers"
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
    for heading in ("## Call sites by handler", "## Findings", "## Routes with no browser caller", "## Not verified"):
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
