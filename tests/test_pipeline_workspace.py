"""Mutation/API/contract checks for the selected-stage Pipeline workspace (UIV2-25).

Every inventory rule is broken in copied source. The API tests use the real application and
security middleware with isolated state; the provider failure travels through the actual SSE
route and must not save a run. Browser behavior and the 48 captures are produced separately by
scripts/pipeline_workspace_walk.mjs, never inferred from these source checks.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import app as studio  # noqa: E402

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "pipeline_workspace_walk.mjs"
SEED = PROJECT_ROOT / "scripts" / "pipeline_recovery_seed.py"
TOKEN = "pipeline-workspace-test-token-0123456789ab"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _load_inventory():
    spec = importlib.util.spec_from_file_location(
        "uiux_pipeline_workspace_inventory",
        PROJECT_ROOT / "scripts" / "uiux_pipeline_workspace_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_inventory()


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {"html": FRONTEND.read_text(encoding="utf-8"), "app": APP.read_text(encoding="utf-8"), "walk": WALK.read_text(encoding="utf-8"), "seed": SEED.read_text(encoding="utf-8")}


@pytest.fixture(scope="module")
def payload(sources: dict[str, str]) -> dict:
    return inventory.build_inventory(**sources)


def _checks(document: dict) -> set[str]:
    return {finding["check"] for finding in document["findings"]}


def test_inventory_matches_generated_artifacts(payload: dict):
    result = subprocess.run(
        [sys.executable, "scripts/uiux_pipeline_workspace_inventory.py", "--check"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["status"] == "clean" and payload["check_count"] == 28
    assert json.loads((PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Workspace.json").read_text(encoding="utf-8"))["story"] == "UIV2-25"
    assert (PROJECT_ROOT / "docs" / "uiux-2-5-pipeline-workspace.md").exists()


def test_the_four_stage_contract_and_full_visual_matrix_are_explicit(payload: dict):
    assert payload["stage_model"]["stages"] == [1, 2, 3, 4]
    assert payload["stage_model"]["overview"] == 0
    assert payload["stage_model"]["route"] == "/api/council/stream"
    assert payload["visual_matrix"] == {
        "states": ["empty", "loading", "populated", "long-content", "error", "recovery"],
        "viewports": ["1640x900", "1440x900", "1280x800", "390x844"],
        "themes": ["light", "dark"],
        "required_captures": 48,
    }


# rule, source, old, new — one mutation for every inventory rule.
MUTATIONS = [
    ("pane-anchor", "html", '<h2 class="pane-title">Pipeline</h2>', '<h2 class="pane-title">Other</h2>'),
    ("four-stage-timeline", "html", 'id="step-card-4"', 'id="step-card-5"'),
    ("original-controls", "html", 'id="councilRunBtn"', 'id="runButton"'),
    ("task-first", "html", "Run timeline", "Timeline"),
    ("setup-disclosure", "html", '<details id="pipelineSetup"', '<details id="pipelineSetup" open'),
    ("stage-keyboard", "html", 'document.getElementById(`step-card-${next}`)?.focus({ preventScroll: true });', 'document.getElementById(`step-card-${next}`)?.focus();'),
    ("stage-keyboard", "html", 'getElementById("pipelineStageStrip").addEventListener("keydown"', 'getElementById("pipelineStageStrip").addEventListener("click"'),
    ("roving-selection", "html", "button.tabIndex = i === next ? 0 : -1;", "button.dataset.tab = i === next ? \"0\" : \"-1\";"),
    ("selected-workspace", "html", 'id="pipelineActivityLog"', 'id="activityLog"'),
    ("meaningful-empty", "html", "The run starts with the Architect", "Nothing is here"),
    ("workspace-state-model", "html", 'state = "recovery"', 'state = "empty"'),
    ("observed-activity", "html", 'recordPipelineEvent("error"', 'recordSyntheticEvent("error"'),
    ("bounded-activity", "html", "PIPELINE_EVENT_LIMIT = 80", "PIPELINE_EVENT_LIMIT = 8"),
    ("stable-output", "html", "if (stageContentSignature[k] !== contentSignature)", "if (true)"),
    ("artifact-handoff-preserved", "html", "renderDeliberationEvidence();", "void 0;"),
    ("api-payload-preserved", "html", "architect_model:", "architectModel:"),
    ("failure-selection", "html", "selectStageFilter(councilRun.failedStage)", "selectStageFilter(0)"),
    ("truthful-terminal-state", "html", "if (allDone) return;", "if (false) return;"),
    ("recovery-priority", "html", 'workspace.insertAdjacentElement("afterend", host)', 'workspace.insertAdjacentElement("beforebegin", host)'),
    ("checkpoint-recovery", "html", 'if (restored) document.getElementById("pipelineWorkspaceTitle")?.focus({ preventScroll: true });', 'if (restored) document.getElementById("pipelineWorkspaceTitle")?.focus();'),
    ("evidence-disclosure", "html", '<details id="pipelineEvidenceDetails" class="pipeline-evidence-details" hidden>', '<details id="pipelineEvidenceDetails" class="pipeline-evidence-details">'),
    ("contract-progressive-disclosure", "html", 'class="stage-evidence"', 'class="stage-contract-now"'),
    ("long-output-reader", "walk", "scrollHeight > pane.output.body.clientHeight", "scrollHeight >= 0"),
    ("responsive-workspace", "html", "@media (min-width: 800px) and (max-height: 820px)", "@media (min-width: 800px) and (max-height: 0px)"),
    ("inspector-readable", "html", "/recovered|partial/i.test(meta)", "/never-match/i.test(meta)"),
    ("inspector-readable", "html", "    syncPipelineInspector();\n    return true;", "    return true;"),
    ("walk-matrix", "walk", 'const THEMES = ["light", "dark"];', 'const THEMES = ["light"];'),
    ("walk-real-fixture", "walk", "puppeteer.launch", "puppeteer.connect"),
    ("seed-real-stream", "seed", "async for item in original", "async for item in []"),
    ("backend-stage-contract", "app", 'WorkflowStage(4, "judge"', 'WorkflowStage(5, "judge"'),
]


@pytest.mark.parametrize(("rule", "source", "old", "new"), MUTATIONS, ids=[row[0] for row in MUTATIONS])
def test_every_inventory_rule_fails_when_its_source_is_broken(sources, rule, source, old, new):
    changed = dict(sources)
    assert old in changed[source], f"mutation anchor vanished for {rule}"
    changed[source] = changed[source].replace(old, new)
    assert changed[source] != sources[source], "the mutation did not change the copied source"
    broken = inventory.build_inventory(**changed)
    assert rule in _checks(broken), f"{rule} passed after its guarantee was removed"


def test_the_walk_reads_but_never_paints_stage_buffers():
    source = WALK.read_text(encoding="utf-8")
    assert "stageBuffers" not in source
    assert ".innerHTML =" not in source
    assert "pipeline_recovery_seed.py" in source and "puppeteer.launch" in source


def test_the_long_fixture_has_real_untruncated_tail_content():
    namespace = {"__file__": str(SEED), "__name__": "pipeline_recovery_seed_test"}
    source = SEED.read_text(encoding="utf-8")
    # Importing is side-effect free: its app starts only under __main__.
    exec(compile(source, str(SEED), "exec"), namespace)
    assert namespace["LONG_REPLY"].count("### Check ") == 12
    assert namespace["LONG_REPLY"].endswith("The final response ends with Check 12 and the complete audit trail.")
    assert namespace["LONG_MODEL"] == "judge-long-content-demo"
    assert namespace["SLOW_MODEL"] == "architect-loading-demo"


class MemoryKeyring:
    def __init__(self) -> None:
        self.values = {}

    def set_password(self, service, item, value):
        self.values[(service, item)] = value

    def get_password(self, service, item):
        return self.values.get((service, item))

    def delete_password(self, service, item):
        self.values.pop((service, item), None)


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    keyring = MemoryKeyring()
    monkeypatch.setattr(studio.keyring, "set_password", keyring.set_password)
    monkeypatch.setattr(studio.keyring, "get_password", keyring.get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", keyring.delete_password)
    db = tmp_path / "state" / "studio.db"
    db.parent.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    studio.init_db()
    return db


@pytest.fixture
def client(isolated_state: Path):
    with TestClient(studio.app) as test_client:
        yield test_client


def council_payload() -> dict:
    return {
        "task": "Return a queue specification.",
        "architect_model": "test-architect",
        "coder_model": "test-builder",
        "critic_model": "test-critic",
        "judge_model": "test-judge",
        "auto_unload": True,
        "reasoning_mode": "off",
        "skills": [],
    }


def test_the_existing_stream_route_requires_authorization(client: TestClient):
    response = client.post("/api/council/stream", json=council_payload())
    assert response.status_code == 401
    assert "Authentication required" in response.text


def test_the_existing_stream_route_still_rejects_cross_origin_posts(client: TestClient):
    response = client.post("/api/council/stream", json=council_payload(), headers={**AUTH, "Origin": "https://attacker.invalid"})
    assert response.status_code == 403


def test_a_real_provider_error_is_sse_and_never_saved(client: TestClient, isolated_state: Path, monkeypatch: pytest.MonkeyPatch):
    async def failing_provider(*_args, **_kwargs):
        if False:  # async generator, with the exception raised on first iteration
            yield {}
        raise RuntimeError("UIV2-25 provider exploded")

    monkeypatch.setattr(studio, "stream_any_model", failing_provider)
    response = client.post("/api/council/stream", json=council_payload(), headers=AUTH)
    assert response.status_code == 200
    assert "event: stage_start" in response.text
    assert "event: error" in response.text
    assert "UIV2-25 provider exploded" in response.text
    assert "event: council_done" not in response.text
    with sqlite3.connect(isolated_state) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'council'").fetchone()[0] == 0
