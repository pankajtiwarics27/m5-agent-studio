"""UIV2-28 targeted DOM, API and fail-closed accessibility contract tests.

The Node walk supplies real Chromium evidence. These tests keep its contract
reproducible and prove that deleting an assertion or importing stale/partial
results cannot leave a green inventory.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as studio

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uiv2_accessibility_adversarial.py"
WALK = ROOT / "scripts" / "uiv2_accessibility_adversarial_walk.mjs"
spec = importlib.util.spec_from_file_location("uiv2_accessibility_adversarial", SCRIPT)
assert spec and spec.loader
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)


class ControlDOM(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.classes: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        self.classes.update(values.get("class", "").split())


def run_payload() -> dict:
    return json.loads(contract.RUN_PATH.read_text(encoding="utf-8"))


def test_contract_is_clean_and_reproducible() -> None:
    payload = contract.build()
    assert payload["findings"] == []
    assert payload["matrix"]["expected_check_groups"] == {
        "rendered": 36, "keyboard": 12, "zoom-200": 48, "long-recovery": 12, "browser": 12,
    }
    assert payload["matrix"]["expected_checks"] == 120
    assert payload["matrix"]["expected_captures"] == 62
    assert payload["evidence"]["status"] == "passed"
    assert payload["evidence"]["totals"] == {"checks": 120, "passed": 120, "failed": 0, "captures": 62}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_matrix_has_every_viewport_theme_pane_and_recovery_row() -> None:
    matrix = contract.build()["matrix"]
    zoom = {(row["viewport"], row["theme"], row["pane"]) for row in matrix["zoom_rows"]}
    assert zoom == {
        (viewport, theme, pane)
        for viewport in contract.VIEWPORTS for theme in contract.THEMES for pane in contract.PANES
    }
    phone = next(row for row in matrix["zoom_rows"] if row["viewport"] == "390x844")
    assert phone["effective_css_viewport"] == "195x422"
    assert phone["device_scale_factor"] == 2
    assert matrix["long_recovery_rows"] == [
        {"viewport": "1280x800", "theme": "dark", "device_scale_factor": 2},
        {"viewport": "390x844", "theme": "light", "device_scale_factor": 2},
    ]


def test_every_impacted_control_still_exists_in_the_real_dom() -> None:
    dom = ControlDOM()
    source = contract.STATIC_PATH.read_text(encoding="utf-8")
    dom.feed(source)
    for selector in contract.IMPACTED_CONTROLS:
        if selector.startswith("#"):
            # The conclusion body is rendered after an API session is selected,
            # so its id lives in the inline DOM template rather than static HTML.
            assert selector[1:] in dom.ids or f'id="{selector[1:]}"' in source, selector
        elif selector.startswith("."):
            assert selector[1:] in dom.classes, selector
        else:
            raise AssertionError(f"unsupported selector in contract: {selector}")


@pytest.mark.parametrize("requirement", sorted(contract.REQUIRED_WALK))
def test_removing_each_browser_requirement_fails_closed(requirement: str) -> None:
    walk = WALK.read_text(encoding="utf-8")
    needle = contract.REQUIRED_WALK[requirement]
    assert needle in walk
    findings = contract.source_findings(
        contract.STATIC_PATH.read_text(encoding="utf-8"), walk.replace(needle, ""),
    )
    assert any(row["check"] == requirement for row in findings)


def test_removing_a_frontend_repair_or_ci_command_fails_closed() -> None:
    html = contract.STATIC_PATH.read_text(encoding="utf-8")
    needle = contract.REQUIRED_FRONTEND["summary-target"]
    findings = contract.source_findings(html.replace(needle, ""), WALK.read_text(encoding="utf-8"))
    assert any(row["check"] == "summary-target" for row in findings)
    workflow = contract.WORKFLOW_PATH.read_text(encoding="utf-8").replace(
        "node scripts/uiv2_accessibility_adversarial_walk.mjs", "", 1,
    )
    findings = contract.source_findings(html, WALK.read_text(encoding="utf-8"), workflow)
    assert any(row["check"] == "ci-adversarial" for row in findings)


def test_walk_requires_real_measurement_keyboard_and_source_bound_captures() -> None:
    source = WALK.read_text(encoding="utf-8")
    assert 'page.keyboard.press("Tab")' in source
    assert 'page.keyboard.press("Enter")' in source
    assert 'page.keyboard.press("Escape")' in source
    assert "getComputedStyle" in source
    assert "0.2126 * channels[0]" in source
    assert "background.nonSolid" in source
    assert 'fetch("/api/hall/sessions"' in source
    assert "source_sha256" in source and "capture-manifest.json" in source


def test_committed_run_is_source_bound_complete_and_has_hashed_captures() -> None:
    payload = run_payload()
    assert contract.validate_run(payload) == []
    assert payload["source_sha256"] == contract.run_source_hashes()
    identities = set()
    for capture in payload["captures"]:
        identity = (capture["group"], capture["viewport"], capture["theme"], capture["pane"])
        assert identity not in identities
        identities.add(identity)
        assert re.fullmatch(r"[0-9a-f]{64}", capture["sha256"])
        assert capture["path"].endswith(".png")


def test_long_recovery_has_nonzero_offsets_modal_end_and_exact_escape_restoration() -> None:
    payload = run_payload()
    for viewport, host in (("1280x800", "hallStream"), ("390x844", "hall-main")):
        rows = [c for c in payload["checks"] if c["group"] == "long-recovery" and c["viewport"] == viewport]
        assert len(rows) == 6 and all(c["passed"] for c in rows)
        nested = next(c for c in rows if c["step"].startswith("transcript and pinned"))
        detail = json.loads(nested["detail"])
        assert detail["streamHost"] == host
        assert detail["stream"] == 96 and detail["conclusion"] == 72
        modal = next(c for c in rows if c["step"].startswith("the long dialog scrolls"))
        modal_detail = json.loads(modal["detail"])
        assert modal_detail["atEnd"] and modal_detail["marker"] and modal_detail["scrollTop"] > 0
        assert modal_detail["stream"] == 96 and modal_detail["conclusion"] == 72
        escaped = next(c for c in rows if c["step"] == "Escape restores the Expand trigger")
        escaped_detail = json.loads(escaped["detail"])
        assert escaped_detail == {
            "open": False, "action": "expand", "stream": 96, "conclusion": 72, "page": 0,
        }


def test_failed_partial_or_stale_run_is_rejected() -> None:
    original = run_payload()
    failed = copy.deepcopy(original)
    failed["checks"][0]["passed"] = False
    assert contract.validate_run(failed)
    partial = copy.deepcopy(original)
    partial["checks"] = partial["checks"][:-1]
    assert contract.validate_run(partial)
    stale = copy.deepcopy(original)
    stale["source_sha256"]["static/index.html"] = "0" * 64
    assert "stale" in " ".join(contract.validate_run(stale))
    missing_zoom = copy.deepcopy(original)
    missing_zoom["checks"] = [c for c in missing_zoom["checks"] if c["group"] != "zoom-200"]
    assert contract.validate_run(missing_zoom)


def test_zero_nested_offsets_or_unmeasured_contrast_cannot_be_imported() -> None:
    zero = run_payload()
    nested = next(c for c in zero["checks"] if c["step"].startswith("transcript and pinned"))
    detail = json.loads(nested["detail"])
    detail["stream"] = 0
    nested["detail"] = json.dumps(detail)
    assert any("nonzero" in error for error in contract.validate_run(zero))
    unmeasured = run_payload()
    unmeasured["method"]["contrast"] = "inferred from design tokens"
    assert any("contrast method" in error for error in contract.validate_run(unmeasured))


def test_long_conclusion_uses_the_real_api_and_survives_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "harness")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    studio.init_db()
    conclusion = "\n\n".join(f"Decision paragraph {index}: preserve dissent and evidence." for index in range(24))
    conclusion += "\n\nFinal marker: uiv2-28-conclusion-end."
    with TestClient(studio.app) as client:
        created = client.post("/api/hall/sessions", json={
            "request": "UIV2-28 long conclusion accessibility recovery", "title": "UIV2-28 contract test",
        })
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]
        closed = client.post(f"/api/hall/sessions/{session_id}/close", json={
            "conclusion": conclusion, "author": "Lead Arbiter",
        })
        assert closed.status_code == 200, closed.text
        reread = client.get(f"/api/hall/sessions/{session_id}")
        assert reread.status_code == 200, reread.text
        assert reread.json()["conclusion_text"] == conclusion
        assert reread.json()["conclusion_text"].endswith("uiv2-28-conclusion-end.")


def test_committed_contract_names_actual_execution_not_screen_reader_certification() -> None:
    payload = json.loads(contract.JSON_PATH.read_text(encoding="utf-8"))
    policy = payload["execution_policy"]
    assert policy["contract_is_not_run_evidence"] is True
    assert policy["browser_ci_job"] == "Browser safety regressions"
    assert "not a claim" in policy["screen_reader_scope"]
    assert payload["thresholds"]["normal_text_contrast"] == 4.5
    assert payload["thresholds"]["large_text_contrast"] == 3.0
    assert payload["thresholds"]["target_css_px"] == 24
    workflow = contract.WORKFLOW_PATH.read_text(encoding="utf-8")
    for command in contract.REQUIRED_WORKFLOW.values():
        assert command in workflow
