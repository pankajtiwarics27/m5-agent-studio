"""UIUX-P0-02: the control inventory must describe the markup it ships with.

The inventory exists so a later story can say "this control was reachable before
and after" instead of hoping. That only works if the artifact is complete and
current, so the checks here are about completeness and drift, not about taste:

* the totals must add up (panes + chrome == markup controls, and every pane's
  breakdown must sum to its own count);
* the six panes must be the six tabs the manifest pins, in the same order — the
  two artifacts describe one UI and cannot be allowed to disagree;
* controls that a session would look for are attributed to the pane they are
  actually in (`chatInput` in chat, `councilPrompt` in council, and the trap:
  the Council pane is `hall`, not `council`);
* `--check` must fail when a control set moves, proved against a temporary copy.

Script-rendered controls are counted separately on purpose. A test that folded
them into the markup totals would make the "complete" claim false in the other
direction, so the split is asserted too.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "product" / "M5_2_0_UIUX_Control_Inventory.json"
DOC = ROOT / "docs" / "uiux-2-0-control-inventory.md"
GENERATOR = ROOT / "scripts" / "uiux_control_inventory.py"
MANIFEST = ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
FRONTEND = ROOT / "static" / "index.html"
ALLOWED_TAGS = {"button", "input", "select", "textarea", "a", "summary"}


def _inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def _all_controls(inventory: dict) -> list[dict]:
    controls = [c for pane in inventory["panes"] for c in pane["controls"]]
    controls.extend(inventory["chrome"]["controls"])
    return controls


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_control_inventory", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_totals_add_up() -> None:
    inventory = _inventory()
    totals = inventory["totals"]
    assert inventory["document_type"] == "m5_uiux_control_inventory"
    assert inventory["story"] == "UIUX-P0-02"
    assert totals["panes"] == len(inventory["panes"]) == 6
    assert totals["pane_controls"] == sum(pane["control_count"] for pane in inventory["panes"])
    assert totals["chrome_controls"] == inventory["chrome"]["control_count"]
    assert totals["markup_controls"] == totals["pane_controls"] + totals["chrome_controls"]
    assert totals["script_rendered_controls"] == sum(totals["script_rendered_by_function"].values())

    for pane in inventory["panes"]:
        assert pane["control_count"] == len(pane["controls"])
        assert sum(pane["counts"].values()) == pane["control_count"], f"{pane['dom_id']}: counts drifted"
        assert pane["section_id"] == f"pane-{pane['dom_id']}"


def test_every_control_is_well_formed() -> None:
    for control in _all_controls(_inventory()):
        assert control["tag"] in ALLOWED_TAGS, f"unexpected element {control['tag']}"
        assert control["tag"] != "details", "a disclosure container is not a control"
        handler = next((value for key, value in control.items() if key.startswith("on")), None)
        if handler is not None:
            assert re.match(r"^[A-Za-z_$][\w$.]*\(", str(handler)), f"handler {handler!r} is not a call"


def test_the_panes_are_the_tabs_the_manifest_pins() -> None:
    inventory = _inventory()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert [pane["dom_id"] for pane in inventory["panes"]] == [
        tab["dom_id"] for tab in manifest["frontend"]["tabs"]
    ]
    assert inventory["frontend_sha256"] == manifest["frontend"]["sha256"]
    assert inventory["pinned_source_commit"] == manifest["pinned_source_commit"]
    ledger_pin = json.loads(LEDGER.read_text(encoding="utf-8"))["source_commit_sha"]
    assert inventory["pinned_source_commit"] == ledger_pin


def test_controls_are_attributed_to_the_pane_they_are_actually_in() -> None:
    """Including the trap this program keeps tripping over."""
    inventory = _inventory()
    where: dict[str, str] = {}
    for pane in inventory["panes"]:
        for control in pane["controls"]:
            if control.get("id"):
                where[control["id"]] = pane["dom_id"]
    for control in inventory["chrome"]["controls"]:
        if control.get("id"):
            where.setdefault(control["id"], "chrome")

    assert where["onboardingRunBtn"] == "start"
    assert where["chatInput"] == "chat"
    assert where["debatePrompt"] == "debate"
    assert where["councilPrompt"] == "council"
    # The Council pane is `hall`; `council` is the Pipeline pane.
    assert "hall" in {pane["dom_id"] for pane in inventory["panes"]}
    assert "council" in {pane["dom_id"] for pane in inventory["panes"]}


def test_script_rendered_controls_are_counted_separately() -> None:
    inventory = _inventory()
    totals = inventory["totals"]
    assert totals["script_rendered_controls"] > 0, "the inline script builds no controls at all?"
    for function in totals["script_rendered_by_function"]:
        assert function == "None" or re.fullmatch(r"[A-Za-z_$][\w$]*", function), function
    assert "not_verified" in inventory
    assert any("rendered" in note for note in inventory["not_verified"])


def test_dom_created_controls_are_counted_with_inline_templates() -> None:
    module = _load_generator()
    controls = module.parse_script_controls(
        '<script>function render(){ const row = document.createElement("button"); }</script>'
    )
    assert controls == [{"tag": "button", "rendered_in_function": "render"}]


def test_the_generated_page_covers_every_pane_and_is_marked_generated() -> None:
    doc = DOC.read_text(encoding="utf-8")
    inventory = _inventory()
    assert "never hand-edit this page" in doc
    for pane in inventory["panes"]:
        assert f"## Pane `{pane['dom_id']}`" in doc
    assert "## Shared chrome" in doc
    assert "## Controls built by script" in doc
    assert "## Not verified" in doc
    for note in inventory["not_verified"]:
        assert note[:60] in doc, "the page drops a limitation the JSON records"


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_check_mode_fails_when_a_control_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_generator()
    inventory = module.build_inventory()
    inventory["panes"][0]["controls"].append({"tag": "button", "id": "invented"})

    stale_json = tmp_path / "inventory.json"
    stale_doc = tmp_path / "inventory.md"
    stale_json.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    stale_doc.write_text(module.render_doc(inventory), encoding="utf-8")
    monkeypatch.setattr(module, "INVENTORY_PATH", stale_json)
    monkeypatch.setattr(module, "DOC_PATH", stale_doc)

    assert module.main(["--check", "--quiet"]) == 1

    inventory_json, inventory_doc = module._rendered()
    stale_json.write_text(inventory_json, encoding="utf-8")
    stale_doc.write_text(inventory_doc, encoding="utf-8")
    assert module.main(["--check", "--quiet"]) == 0


def test_the_frontend_is_still_the_file_that_was_inventoried() -> None:
    import hashlib

    digest = hashlib.sha256(FRONTEND.read_bytes()).hexdigest()
    assert _inventory()["frontend_sha256"] == digest, (
        "static/index.html changed; regenerate the control inventory in the same commit"
    )
