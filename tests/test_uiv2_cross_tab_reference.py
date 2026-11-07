"""UIV2-26 contract, fail-closed approval, and static-DOM unit coverage."""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uiux_v2_cross_tab_reference.py"
WALK = ROOT / "scripts" / "uiux_v2_cross_tab_reference_walk.mjs"
spec = importlib.util.spec_from_file_location("uiv2_cross_tab_reference", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)

# A valid, opaque PNG is sufficient for candidate hash/path validation; browser rendering is
# tested by the actual walk, not by fabricating visual evidence here.
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class StudioDOM(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {key: value or "" for key, value in attrs}))

    def find_all(self, *, element_id: str | None = None, class_name: str | None = None) -> list[dict[str, str]]:
        rows = []
        for _, attrs in self.tags:
            if element_id is not None and attrs.get("id") != element_id:
                continue
            if class_name is not None and class_name not in attrs.get("class", "").split():
                continue
            rows.append(attrs)
        return rows


def candidate_fixture(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "candidate"
    root.mkdir()
    captures = []
    for key in gate.matrix_keys():
        theme, viewport, pane = key.split("/")
        images = {}
        for kind in ("actual", "reference", "comparison"):
            relative = Path(theme) / viewport / pane / f"{kind}.png"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PIXEL)
            images[kind] = {"file": relative.as_posix(), "sha256": gate.sha256(path), "bytes": len(PIXEL)}
        captures.append({
            "key": key,
            "signature": {"stable": key},
            "reference_signature": {"stable": key},
            "differences": [{"id": f"D{index}"} for index in range(1, 4)],
            "pixel_difference": {"mean_absolute_rgb_0_255": 1.0, "changed_pixel_ratio_0_1": 0.1},
            "images": images,
        })
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    payload = {
        "schema_version": 1,
        "document_type": "m5_uiv2_cross_tab_candidate",
        "story": "UIV2-26",
        "mode": "candidate",
        "complete": True,
        "matrix": {"keys": gate.matrix_keys()},
        "contract_sha256": gate.contract_fingerprint(json.loads(gate.JSON_PATH.read_text(encoding="utf-8"))),
        "wireframe_sha256": gate.sha256(gate.WIREFRAME_PATH),
        "git": {"head": head, "dirty": False},
        "checks": {"total": 48, "passed": 48, "failed": 0},
        "failures": [],
        "captures": captures,
    }
    path = root / "candidate-manifest.json"
    path.write_text(gate.dumps(payload), encoding="utf-8")
    return path, payload


def test_contract_is_complete_clean_and_reproducible() -> None:
    payload = gate.build()
    assert payload["findings"] == []
    assert payload["matrix"]["keys"] == gate.matrix_keys()
    assert len(payload["matrix"]["keys"]) == 48
    assert payload["matrix"]["total_pngs_per_run"] == 144
    assert payload["totals"]["difference_annotations"] == 18
    assert len(payload["approved_reference"]["committed_fixtures"]) == 36
    assert {row["id"] for row in payload["panes"]} == {"start", "harness", "hall", "council", "debate", "chat"}
    for pane in payload["panes"]:
        assert set(pane["state_support"]) == {"empty", "populated", "long-content", "loading", "error", "recovery"}
        assert len(pane["differences"]) == 3
        assert all(row["story"] for row in pane["differences"])
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_contract_fingerprint_is_stable_across_approval_and_app_revision() -> None:
    payload = gate.build()
    original = gate.contract_fingerprint(payload)
    changed_status = copy.deepcopy(payload)
    changed_status["approval"] = {"status": "approved", "human_approval": {"reference": "user:…"}}
    changed_status["source"]["static_sha256"] = "f" * 64
    changed_status["source"]["walk_sha256"] = "e" * 64
    assert gate.contract_fingerprint(changed_status) == original
    changed_rules = copy.deepcopy(payload)
    changed_rules["layout_contract"]["header_max_height"] = 63
    assert gate.contract_fingerprint(changed_rules) != original
    changed_reference = copy.deepcopy(payload)
    changed_reference["approved_reference"]["wireframe_sha256"] = "0" * 64
    assert gate.contract_fingerprint(changed_reference) != original


def test_static_dom_has_the_six_panes_and_pane_specific_structure() -> None:
    html = gate.STATIC_PATH.read_text(encoding="utf-8")
    dom = StudioDOM()
    dom.feed(html)
    assert len(dom.find_all(class_name="mode-tab")) == 6
    assert len(dom.find_all(class_name="pane")) == 6
    for pane in gate.PANES.values():
        for selector in (*pane["required"], *pane["order"]):
            if selector.startswith("#"):
                assert len(dom.find_all(element_id=selector[1:])) == 1, selector
    assert len(dom.find_all(class_name="harness-stage")) == 3
    assert len(dom.find_all(class_name="step-card")) == 5  # All stages + exactly four stages.
    assert len(dom.find_all(class_name="pipeline-overview-button")) == 1
    for ordered in (
        ("startLaunchCard", "startNextBlocker", "startDiagnostics"),
        ("harnessStages", "harnessPromptInput", "harnessRunBtn"),
        ("hallStream", "hallVerdict", "hallHumanInput"),
        ("councilPrompt", "pipelineStageStrip", "pipelineWorkspace"),
        ("debatePrompt", "debateCriteriaDetails", "debateGrid"),
        ("chatMessages", "chatInput", "chatSendBtn"),
    ):
        positions = [html.index(f'id="{element_id}"') for element_id in ordered]
        assert positions == sorted(positions), ordered


def test_candidate_validation_checks_all_144_image_digests(tmp_path: Path) -> None:
    path, payload = candidate_fixture(tmp_path)
    assert gate.validate_candidate(payload, path) == []
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate-candidate", str(path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    tampered = path.parent / payload["captures"][0]["images"]["comparison"]["file"]
    tampered.write_bytes(b"not the reviewed image")
    errors = gate.validate_candidate(payload, path)
    assert any("comparison image missing or digest mismatch" in error for error in errors)


def test_partial_failed_or_foreign_contract_candidates_are_rejected(tmp_path: Path) -> None:
    path, payload = candidate_fixture(tmp_path)
    partial = copy.deepcopy(payload)
    partial["complete"] = False
    assert "partial matrices cannot be approved or checked" in gate.validate_candidate(partial, path)
    failed = copy.deepcopy(payload)
    failed["checks"]["passed"] = 47
    failed["checks"]["failed"] = 1
    assert any("failed or unexecuted" in error for error in gate.validate_candidate(failed, path))
    foreign = copy.deepcopy(payload)
    foreign["contract_sha256"] = "0" * 64
    assert any("different contract" in error for error in gate.validate_candidate(foreign, path))
    missing = copy.deepcopy(payload)
    missing["captures"] = missing["captures"][:-1]
    assert any("exactly 48" in error for error in gate.validate_candidate(missing, path))


def test_only_explicit_human_decision_can_bind_an_approved_baseline(tmp_path: Path) -> None:
    path, payload = candidate_fixture(tmp_path)
    output = tmp_path / "approval.json"
    with pytest.raises(ValueError, match="explicit human approval"):
        gate.approve_candidate(path, "agent:the screenshots look fine", output_path=output)
    assert not output.exists()
    dirty = copy.deepcopy(payload)
    dirty["git"]["dirty"] = True
    path.write_text(gate.dumps(dirty), encoding="utf-8")
    with pytest.raises(ValueError, match="clean tracked worktree"):
        gate.approve_candidate(path, "user:test conversation explicitly approves structure", output_path=output)
    assert not output.exists()
    path.write_text(gate.dumps(payload), encoding="utf-8")
    approval = gate.approve_candidate(
        path, "user:test conversation explicitly approves structure", output_path=output,
    )
    assert output.exists()
    assert approval["verification"] == {"checks_total": 48, "checks_passed": 48, "failures": 0, "captures": 48}
    assert approval["candidate"]["manifest_sha256"] == gate.sha256(path)
    assert len(approval["captures"]) == 48
    assert gate.validate_approval(approval) == []


def test_approved_schema_rejects_missing_signatures_or_annotations(tmp_path: Path) -> None:
    path, _ = candidate_fixture(tmp_path)
    approval = gate.approve_candidate(
        path, "user:test conversation explicitly approves structure", output_path=tmp_path / "approval.json",
    )
    damaged = copy.deepcopy(approval)
    damaged["captures"][0]["signature"] = None
    assert any("signature missing" in error for error in gate.validate_approval(damaged))
    damaged = copy.deepcopy(approval)
    damaged["captures"] = damaged["captures"][:-1]
    assert any("exactly 48" in error for error in gate.validate_approval(damaged))
    damaged = copy.deepcopy(approval)
    damaged["human_approval"]["reference"] = "agent:self-approved"
    assert any("explicit user:" in error for error in gate.validate_approval(damaged))


def test_browser_check_fails_closed_before_chrome_without_human_approval(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "product").mkdir()
    (root / "docs/media/uiv2-v2/reference").mkdir(parents=True)
    shutil.copy2(WALK, root / "scripts" / WALK.name)
    shutil.copy2(gate.JSON_PATH, root / "product" / gate.JSON_PATH.name)
    shutil.copy2(gate.WIREFRAME_PATH, root / "docs/media/uiv2-v2/reference/wireframe.html")
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, str(root / "scripts" / WALK.name), "--check", "--out", str(root / "out")],
        cwd=root, capture_output=True, text=True, check=False, timeout=20,
        env={"PATH": "/usr/bin:/bin"},  # no Chrome or project dependencies are available
    )
    assert result.returncode != 0
    assert "fails closed: no human-approved live structural baseline" in result.stderr
    assert not (root / "out").exists()


def test_browser_gate_measures_pixels_without_a_tolerance_verdict() -> None:
    source = WALK.read_text(encoding="utf-8")
    assert "mean_absolute_rgb_0_255" in source
    assert "changed_pixel_ratio_0_1" in source
    assert "informational only; structural checks and human review decide" in source
    assert not re.search(r"changed_pixel_ratio_0_1\s*(?:<|>|<=|>=)", source)
    assert 'livePage.keyboard.press("Enter")' in source
    assert "approved.live-structural-signature" in source
    assert "approved.reference-structural-signature" in source
    assert "candidate" in source and "inferred: false" in source
    assert 'detached: process.platform !== "win32"' in source
    assert 'process.kill(-child.pid, name)' in source


def test_nested_structural_signature_mutation_is_detected() -> None:
    module = WALK.as_uri()
    program = f"""
      const gate = await import({json.dumps(module)});
      const approved = {{regions: {{pane: {{x: 200, width: 920}}}}, counts: {{stages: 4}}}};
      if (gate.diffValues(approved, approved).length !== 0) throw new Error('identical signatures drifted');
      const moved = structuredClone(approved); moved.regions.pane.x = 199;
      const wrongCount = structuredClone(approved); wrongCount.counts.stages = 3;
      if (!gate.diffValues(approved, moved).some((row) => row.includes('regions.pane.x'))) throw new Error('position drift passed');
      if (!gate.diffValues(approved, wrongCount).some((row) => row.includes('counts.stages'))) throw new Error('count drift passed');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program], cwd=ROOT, capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_new_png_is_a_git_input() -> None:
    changed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert not [name for name in changed if name.lower().endswith((".png", ".jpg", ".jpeg"))]
