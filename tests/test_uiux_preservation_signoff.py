"""UIUX-P0-06: the sign-off package must be an honest snapshot an owner can approve.

The package exists because an agent may prepare an owner's decision but may not make
it. So the checks here are about the two ways such a package goes wrong:

* it drifts from the artifacts it summarises, so the owner approves a tree that no
  longer exists — every count, digest and pin is re-derived from disk;
* it flatters itself, claiming approval, coverage or certainty it does not have —
  the status comes from the ledger, the pane-to-route walk publishes its depth and
  its exclusions, and the page carries its own limits.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIGNOFF = ROOT / "product" / "M5_2_0_UIUX_Preservation_Signoff.json"
DOC = ROOT / "docs" / "uiux-2-0-p0-signoff.md"
GENERATOR = ROOT / "scripts" / "uiux_preservation_signoff.py"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
INVENTORY = ROOT / "product" / "M5_2_0_UIUX_Control_Inventory.json"
ROUTE_MAP = ROOT / "product" / "M5_2_0_UIUX_Route_Map.json"
TRACE = ROOT / "product" / "M5_2_0_UIUX_Authorization_Trace.json"
MANIFEST = ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"


def _package() -> dict:
    return json.loads(SIGNOFF.read_text(encoding="utf-8"))


def _ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_preservation_signoff", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_pane_in_the_registry_appears_once_with_its_capabilities() -> None:
    package = _package()
    ledger = _ledger()
    registry = ledger["tab_registry"]
    matrix = ledger["feature_preservation_matrix"]

    assert [pane["dom_id"] for pane in package["panes"]] == [entry["dom_id"] for entry in registry]
    carried = sum(len(pane["preserved_capabilities"]) for pane in package["panes"])
    assert carried == len(matrix), "a preservation row was dropped or duplicated"

    for pane, entry in zip(package["panes"], registry):
        assert pane["label"] == entry["label"]
        assert pane["keyboard_shortcut"] == entry["keyboard_shortcut"]
        expected = [row["existing"] for row in matrix if row["tab"] == entry["label"]]
        assert [row["capability"] for row in pane["preserved_capabilities"]] == expected
        for row in pane["preserved_capabilities"]:
            assert row["must_preserve"].strip()


def test_pane_control_counts_match_the_inventory() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    by_id = {pane["dom_id"]: pane for pane in inventory["panes"]}
    totals = _package()["totals"]
    assert totals["markup_controls"] == inventory["totals"]["markup_controls"]
    assert totals["chrome_controls"] == inventory["totals"]["chrome_controls"]
    assert totals["pane_controls"] == inventory["totals"]["pane_controls"]
    assert totals["script_rendered_controls"] == inventory["totals"]["script_rendered_controls"]
    for pane in _package()["panes"]:
        source = by_id[pane["dom_id"]]
        assert pane["control_count"] == source["control_count"]
        assert len(pane["controls"]) == source["control_count"]
        assert [control["id"] for control in pane["controls"]] == [c.get("id") for c in source["controls"]]


def test_signoff_keeps_ambiguous_and_unknown_method_candidates() -> None:
    module = _load_generator()
    calls = [
        {
            "handler": "ambiguous", "method": "GET",
            "urls": [{"kind": "exact", "route": "/api/known", "candidates": [
                {"method": "GET", "path": "/api/known"},
                {"method": "GET", "path": "/api/{item}"},
            ]}],
        },
        {
            "handler": "dynamic_method", "method": None,
            "urls": [{"kind": "dynamic_method", "route": None, "candidates": [
                {"method": "GET", "path": "/api/known"},
                {"method": "POST", "path": "/api/known"},
            ]}],
        },
    ]
    trace = {("POST", "/api/known"): {"shared_gates": [{"gate": "bearer_token"}]}}
    routes = module._routes_by_handler(calls, trace)
    assert {(r["path"], r["certainty"]) for r in routes["ambiguous"]} == {
        ("/api/known", "possible"), ("/api/{item}", "possible")
    }
    assert {(r["method"], r["path"], r["certainty"]) for r in routes["dynamic_method"]} == {
        ("GET", "/api/known", "possible"), ("POST", "/api/known", "possible")
    }
    post = next(r for r in routes["dynamic_method"] if r["method"] == "POST")
    assert post["state_changing"] is True
    assert post["gates"] == ["bearer_token"]


def test_shared_helper_retains_distinct_paths_within_the_walk_depth() -> None:
    module = _load_generator()
    routes_by_handler = {"fetcher": [
        {"method": "GET", "path": "/api/one", "certainty": "certain", "state_changing": False,
         "gates": [], "entry_point": "one"},
        {"method": "POST", "path": "/api/two", "certainty": "certain", "state_changing": True,
         "gates": ["bearer_token"], "entry_point": "two"},
    ]}
    callees = {"start": ["one", "two"], "one": ["fetcher"], "two": ["fetcher"]}
    routes, _excluded = module._pane_routes({"start"}, routes_by_handler, callees)
    assert {(route["method"], route["path"]) for route in routes} == {
        ("GET", "/api/one"), ("POST", "/api/two")
    }


def test_signoff_fails_closed_when_a_state_changing_candidate_is_not_traced() -> None:
    module = _load_generator()
    calls = [{"handler": "send", "method": None, "urls": [{
        "kind": "dynamic_method", "route": None,
        "candidates": [{"method": "POST", "path": "/api/known"}],
    }]}]
    with pytest.raises(module.SignoffError, match="trace"):
        module._routes_by_handler(calls, {})


def test_signoff_propagates_route_map_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    mapping["caller_resolution"]["complete"] = False
    mapping["totals"]["unresolved_call_sites"] = 1
    mapping["totals"]["unresolved_caller_edges"] = 2
    mapping["totals"]["unresolved_method_edges"] = 1
    mapping["totals"]["routes_unknown_frontend_caller"] = 58
    mapping["totals"]["routes_no_caller_observed"] = 0
    route_map = tmp_path / "map.json"
    route_map.write_text(json.dumps(mapping), encoding="utf-8")
    trace = json.loads(TRACE.read_text(encoding="utf-8"))
    trace["route_map_sha256"] = hashlib.sha256(route_map.read_bytes()).hexdigest()
    trace["totals"]["route_map_unresolved_call_sites"] = 1
    trace["totals"]["route_map_unresolved_caller_edges"] = 2
    trace["totals"]["route_map_unresolved_method_edges"] = 1
    trace_file = tmp_path / "trace.json"
    trace_file.write_text(json.dumps(trace), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", route_map)
    monkeypatch.setattr(module, "TRACE", trace_file)
    package = module.build_signoff()
    # The historical signature is distinct from binding to the changed route map.
    record = (_ledger().get("coordination") or {}).get("preservation_signoff") or {}
    assert package["signature"]["signed"] is bool(record.get("approved"))
    assert package["approval_binding"]["status"] in {"unsigned", "unbound", "matched", "drifted"}
    assert package["totals"]["routes_with_resolved_or_possible_caller"] == 57
    assert package["totals"]["routes_unknown_frontend_caller"] == 58
    assert package["totals"]["route_map_unresolved_method_edges"] == 1
    page = module.render_doc(package)
    assert "2 unresolved URL or method edge(s)" in page
    assert "not proof" in page


def test_signoff_uses_schema_two_candidate_totals() -> None:
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    package = _package()
    assert package["schema_version"] == "3.0.0"
    assert package["totals"]["routes_with_resolved_or_possible_caller"] == (
        mapping["totals"]["routes_with_resolved_or_possible_caller"]
    )
    assert package["totals"]["routes_unknown_frontend_caller"] == mapping["totals"]["routes_unknown_frontend_caller"]
    assert "routes_used_by_frontend" not in package["totals"]


def test_signoff_rejects_route_maps_without_uncertainty_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    del mapping["totals"]["unresolved_method_edges"]
    path = tmp_path / "map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", path)
    with pytest.raises(module.SignoffError, match="uncertainty"):
        module.build_signoff()


def test_signoff_rejects_an_authorization_trace_for_another_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    mapping["statement"] += " changed"
    path = tmp_path / "map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "ROUTE_MAP", path)
    with pytest.raises(module.SignoffError, match="does not match"):
        module.build_signoff()


def test_the_package_reports_the_ledger_signature_state() -> None:
    """The page reports the ledger; it never carries a signature of its own.

    This used to assert that the package *was* unsigned, which turned the owner's
    signature into a test failure and would have invited someone to "fix" the test
    by deleting the assertion. The invariant is the agreement between the record and
    what is published, and it holds in both directions: an unsigned tree may not
    render an approval, and a signed tree may not render "no signature", quote a
    different signer, or drop the limits the signature does not remove.
    """
    package = _package()
    ledger = _ledger()
    record = (ledger.get("coordination") or {}).get("preservation_signoff") or {}
    page = DOC.read_text(encoding="utf-8")
    approved = bool(record.get("approved"))

    assert package["signature"]["signed"] is approved
    binding = package["approval_binding"]
    statuses = {"unsigned": "unsigned", "unbound": "historical-approval-unbound",
                "matched": "approval-inputs-match", "drifted": "historical-approval-input-drift"}
    assert package["status"] == statuses[binding["status"]]
    assert f"| Status | **{package['status']}** |" in page
    assert (binding["status"] != "unsigned") is approved
    assert "Prepared by an agent" in page, "the package must keep saying who prepared it"

    if approved:
        for field in ("approved_by", "approved_at", "reference"):
            assert record.get(field), f"an approval without {field} is not a signature"
            assert package["signature"][field] == record[field], f"the page disagrees with the ledger on {field}"
        assert "Signed in the ledger" in package["signature"]["statement"]
        assert "No signature" not in package["signature"]["statement"]
        # A signature approves the matrix and the rollback path; it does not upgrade
        # the scan's coverage, and the page has to keep saying so.
        assert "not proof" in page
    else:
        assert package["signature"]["approved_by"] is None
        assert package["signature"]["approved_at"] is None
        assert "No signature" in package["signature"]["statement"]


def test_a_partial_signature_is_refused_and_a_complete_one_is_reported() -> None:
    module = _load_generator()

    assert module._approval({}) == {
        "approved": False,
        "approved_by": None,
        "approved_at": None,
        "reference": None,
    }

    partial = {"coordination": {"preservation_signoff": {"approved": True, "approved_by": "owner"}}}
    with pytest.raises(module.SignoffError):
        module._approval(partial)

    complete = {
        "coordination": {
            "preservation_signoff": {
                "approved": True,
                "approved_by": "owner",
                "approved_at": "2026-11-01",
                "reference": "conversation 2026-11-01",
            }
        }
    }
    approved = module._approval(complete)
    assert approved["approved"] is True
    assert approved["approved_by"] == "owner"


def test_every_attributed_route_exists_and_names_its_walk() -> None:
    package = _package()
    mapping = json.loads(ROUTE_MAP.read_text(encoding="utf-8"))
    trace = json.loads(TRACE.read_text(encoding="utf-8"))
    mounted = {(route["method"], route["path"]) for route in mapping["routes"]}
    traced = {(row["method"], row["path"]): row for row in trace["routes"]}

    for pane in package["panes"]:
        handlers = set(pane["declared_handlers"])
        for route in pane["routes_reached"]:
            assert (route["method"], route["path"]) in mounted
            assert route["certainty"] in {"certain", "possible"}
            assert route["via"][0] in handlers, f"{route['path']} was not reached from this pane's controls"
            assert route["state_changing"] is bool((route["method"], route["path"]) in traced)
            if route["state_changing"]:
                assert route["gates"], f"{route['path']} is state-changing but names no gate"
            else:
                assert route["gates"] == []


def test_a_shared_helper_is_not_blamed_for_another_panes_route() -> None:
    """`consumeSSE` fetches whatever URL its caller gave it.

    Both Chat and Pipeline reach it; neither may inherit the other's stream. The
    package keeps a route only when the walk passes the function that supplied the
    literal, and counts what it excluded rather than silently dropping it.
    """
    panes = {pane["label"]: pane for pane in _package()["panes"]}

    chat = {route["path"] for route in panes["Chat"]["routes_reached"]}
    pipeline = {route["path"] for route in panes["Pipeline"]["routes_reached"]}
    compare = {route["path"] for route in panes["Compare"]["routes_reached"]}

    assert "/api/chat/stream" in chat
    assert "/api/council/stream" in pipeline
    assert "/api/debate/stream" in compare
    assert "/api/council/stream" not in chat
    assert "/api/chat/stream" not in pipeline
    assert "/api/debate/stream" not in chat

    for pane in panes.values():
        assert pane["routes_excluded_by_entry_point"] >= 0
        for route in pane["routes_reached"]:
            assert route["path"] not in {"/api/hall/sessions/{session_id}/summon"} or pane["label"] == "Council"

    totals = _package()["totals"]
    assert totals["route_walk_depth"] == 2
    assert totals["pane_routes_reached"] == sum(len(p["routes_reached"]) for p in panes.values())


def test_the_package_is_frozen_to_the_tree_it_describes() -> None:
    package = _package()
    ledger = _ledger()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert package["pinned_source_commit"] == ledger["source_commit_sha"]
    assert package["pinned_source_commit"] == manifest["pinned_source_commit"]
    assert package["inputs"]["frontend_sha256"] == manifest["frontend"]["sha256"]
    for key, path in (
        ("ledger_sha256", LEDGER),
        ("control_inventory_sha256", INVENTORY),
        ("route_map_sha256", ROUTE_MAP),
        ("authorization_trace_sha256", TRACE),
        ("source_manifest_sha256", MANIFEST),
    ):
        assert package["inputs"][key] == hashlib.sha256(path.read_bytes()).hexdigest(), key


def test_the_rollback_path_is_runnable_and_says_what_it_leaves_alone() -> None:
    package = _package()
    rollback = package["rollback"]
    assert "merge commit" in rollback["strategy"]
    joined = " ".join(rollback["steps"])
    assert "git revert -m 1" in joined
    assert rollback["not_touched"] and any("migration" in item for item in rollback["not_touched"])

    for command in rollback["verify_after_revert"]:
        if command.startswith("python3 "):
            script = command.split()[1]
            assert (ROOT / script).is_file(), f"rollback verification names a missing script: {script}"
        assert command.split()[0] in {".venv/bin/python", "python3"}

    page = DOC.read_text(encoding="utf-8")
    assert "## The rollback path" in page
    for step in rollback["steps"]:
        assert step[:40] in page


def test_the_page_carries_its_own_limits() -> None:
    page = DOC.read_text(encoding="utf-8")
    for heading in (
        "## What the owner is being asked to approve",
        "## The preservation surface, pane by pane",
        "## Shared chrome",
        "## The rollback path",
        "## Approval record",
        "## Not verified",
    ):
        assert heading in page
    for item in _package()["not_verified"]:
        assert item[:60] in page


def test_the_check_mode_passes_and_fails_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    module = _load_generator()
    package = module.build_signoff()
    package["totals"]["markup_controls"] = 0

    stale_json = tmp_path / "signoff.json"
    stale_doc = tmp_path / "signoff.md"
    stale_json.write_text(json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    stale_doc.write_text(module.render_doc(package), encoding="utf-8")
    monkeypatch.setattr(module, "SIGNOFF_PATH", stale_json)
    monkeypatch.setattr(module, "DOC_PATH", stale_doc)

    assert module.main(["--check", "--quiet"]) == 1
    payload, page = module._rendered()
    stale_json.write_text(payload, encoding="utf-8")
    stale_doc.write_text(page, encoding="utf-8")
    assert module.main(["--check", "--quiet"]) == 0


def test_historical_approval_without_a_snapshot_is_not_current_input_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_generator()
    ledger = _ledger()
    ledger["coordination"]["preservation_signoff"] = {
        "approved": True, "approved_by": "synthetic-owner", "approved_at": "2026-01-01", "reference": "synthetic-test",
    }
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(module, "LEDGER", path)
    package = module.build_signoff()
    assert package["signature"]["signed"] is True
    assert package["status"] == "historical-approval-unbound"
    binding = package["approval_binding"]
    assert binding["status"] == "unbound"
    assert binding["current_inputs_match_approval"] is None
    assert binding["approved_inputs"] is None
    assert binding["changed_inputs"] is None
    page = module.render_doc(package)
    assert "does not establish approval of current inputs" in page


def test_explicit_snapshot_distinguishes_matching_and_changed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic signatures only: regenerating current hashes must not reapprove drift."""
    module = _load_generator()
    ledger = _ledger()
    ledger_path = tmp_path / "ledger.json"
    index_path = tmp_path / "index.json"
    index_path.write_bytes((ROOT / "docs/media/screenshots/index.json").read_bytes())
    monkeypatch.setattr(module, "LEDGER", ledger_path)
    monkeypatch.setattr(module, "SCREENSHOT_INDEX", index_path)
    record = ledger["coordination"]["preservation_signoff"]
    record.update({"approved_by": "synthetic-owner", "approved_at": "2026-01-01", "reference": "synthetic-test"})
    current = module._current_binding_inputs(ledger)
    record["binding_schema"] = "m5-uiux-inputs-v1"
    record["binding_reference"] = "synthetic approval of this digest set"
    record["approved_inputs"] = current
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    package = module.build_signoff()
    assert package["status"] == "approval-inputs-match"
    assert package["approval_binding"]["current_inputs_match_approval"] is True
    assert package["approval_binding"]["changed_inputs"] == []
    assert package["approval_binding"]["approved_inputs"] == current

    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["synthetic-note"] = "changed after owner approval"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    drifted = module.build_signoff()
    assert drifted["status"] == "historical-approval-input-drift"
    assert drifted["approval_binding"]["current_inputs_match_approval"] is False
    assert drifted["approval_binding"]["changed_inputs"] == ["screenshot_index_sha256"]
    assert drifted["approval_binding"]["approved_inputs"] == current
    assert "current inputs differ" in module.render_doc(drifted)
    # --check measures generated-file freshness, not owner authorization.
    output = tmp_path / "signoff.json"
    page = tmp_path / "signoff.md"
    monkeypatch.setattr(module, "SIGNOFF_PATH", output)
    monkeypatch.setattr(module, "DOC_PATH", page)
    payload, rendered = module._rendered()
    output.write_text(payload, encoding="utf-8")
    page.write_text(rendered, encoding="utf-8")
    assert module.main(["--check", "--quiet"]) == 0
    assert json.loads(output.read_text())["status"] == "historical-approval-input-drift"


def test_signature_fields_do_not_create_a_ledger_hash_cycle_but_content_changes_do() -> None:
    module = _load_generator()
    ledger = _ledger()
    ledger["coordination"]["preservation_signoff"] = {"approved": True, "reference": "synthetic"}
    before = module._current_binding_inputs(ledger)["ledger_content_sha256"]
    ledger["coordination"]["preservation_signoff"]["approved_inputs"] = {"synthetic": "record changes are excluded"}
    assert module._current_binding_inputs(ledger)["ledger_content_sha256"] == before
    ledger["feature_preservation_matrix"][0]["must_preserve"] += " synthetic change"
    assert module._current_binding_inputs(ledger)["ledger_content_sha256"] != before


@pytest.mark.parametrize("invalid", ["missing", "missing-reference", "empty-reference", "partial", "unknown-key", "bad-hash", "bad-schema", "unsigned"])
def test_invalid_explicit_bindings_fail_closed(invalid: str) -> None:
    module = _load_generator()
    ledger = _ledger()
    record = ledger["coordination"]["preservation_signoff"]
    current = module._current_binding_inputs(ledger)
    record["binding_schema"] = "m5-uiux-inputs-v1"
    record["binding_reference"] = "synthetic approval of this digest set"
    record["approved_inputs"] = dict(current)
    if invalid == "missing":
        del record["approved_inputs"]
    elif invalid == "missing-reference":
        del record["binding_reference"]
    elif invalid == "empty-reference":
        record["binding_reference"] = "  "
    elif invalid == "partial":
        del record["approved_inputs"]["route_map_sha256"]
    elif invalid == "unknown-key":
        record["approved_inputs"]["extra"] = "a" * 64
    elif invalid == "bad-hash":
        record["approved_inputs"]["source_manifest_sha256"] = "bad"
    elif invalid == "bad-schema":
        record["binding_schema"] = "v999"
    else:
        record["approved"] = False
    with pytest.raises(module.SignoffError):
        module._approval_binding(ledger, current)


def test_unsigned_record_never_reports_current_input_approval() -> None:
    module = _load_generator()
    ledger = _ledger()
    ledger["coordination"].pop("preservation_signoff")
    binding = module._approval_binding(ledger, module._current_binding_inputs(ledger))
    assert binding["status"] == "unsigned"
    assert binding["current_inputs_match_approval"] is None
    assert binding["approved_inputs"] is None


@pytest.mark.parametrize("invalid", ["false", 1, None])
def test_non_boolean_approval_is_not_treated_as_an_owner_decision(invalid: object) -> None:
    module = _load_generator()
    record = {"approved": invalid, "approved_by": "synthetic", "approved_at": "2026-01-01", "reference": "test"}
    with pytest.raises(module.SignoffError, match="boolean"):
        module._approval({"coordination": {"preservation_signoff": record}})


def test_binding_hashes_actual_frontend_and_optional_screenshot_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_generator()
    frontend = tmp_path / "index.html"
    frontend.write_bytes((ROOT / "static/index.html").read_bytes())
    monkeypatch.setattr(module, "FRONTEND", frontend)
    index = tmp_path / "screenshots.json"
    monkeypatch.setattr(module, "SCREENSHOT_INDEX", index)
    ledger = _ledger()
    before = module._current_binding_inputs(ledger)
    assert before["screenshot_index_sha256"] is None
    frontend.write_bytes(frontend.read_bytes() + b"\n<!-- synthetic drift -->\n")
    index.write_text('{"shots":[]}', encoding="utf-8")
    after = module._current_binding_inputs(ledger)
    assert before["frontend_sha256"] != after["frontend_sha256"]
    assert after["screenshot_index_sha256"] == hashlib.sha256(index.read_bytes()).hexdigest()
    assert before["source_manifest_sha256"] == after["source_manifest_sha256"]
