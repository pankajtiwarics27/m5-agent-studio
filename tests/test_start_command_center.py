"""The Start command center's verdict, and the contradiction it exists to print (UIV2-24).

Start used to open with a six-row checklist. The story replaces it with the thing a
person actually lands for - open a task - followed by one sentence of verdict and at
most one blocker, with the checklist, health and workspace one disclosure below.

Two rules get the weight here, because a screenshot cannot hold them:

* a port answering is not a runtime that can run a model, so ``port_answered`` and
  ``model_count`` are separate observations and an empty model list is a
  contradiction, never a green word;
* the pane prints the server's verdict rather than deriving its own, so the sentence
  on screen and the checklist underneath cannot drift apart.

Every rule is also broken in a copy of the inputs or the markup, because a check that
passes because it stopped looking is the same defect it was written to catch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import app as studio  # noqa: E402
import onboarding_preflight as preflight  # noqa: E402
import start_command_center as command_center  # noqa: E402

FRONTEND = PROJECT_ROOT / "static" / "index.html"
TOKEN = "command-center-test-token-0123456789ab"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ── the module: observations in, one verdict out ─────────────────────────────


def _preflight_inputs(**overrides) -> preflight.PreflightInputs:
    base = dict(
        auth_ready=True,
        local_runtime_configured=True,
        providers=(preflight.ProviderState(alias="openai", enabled=True, has_credential=True, model_count=2),),
        execution_mode="sandbox",
        sandbox_runtime_available=True,
        workspace_writable=True,
        workspace_detail="Workspace directory is writable: /tmp/ws.",
    )
    base.update(overrides)
    return preflight.PreflightInputs(**base)  # type: ignore[arg-type]


def _report(**overrides) -> dict:
    return preflight.build_report(_preflight_inputs(**overrides)).to_dict()


def test_a_ready_preflight_without_a_probe_says_the_runtime_was_not_asked():
    readiness = command_center.build_readiness(
        _report(), runtime=command_center.RuntimeObservation(configured=True, url="http://127.0.0.1:8000")
    )

    assert readiness["verdict"] == "ready"
    assert readiness["basis"] == "derived"
    assert readiness["runtime"]["probe"] == "not-run"
    assert readiness["runtime"]["port_answered"] is None
    runtime_row = [check for check in readiness["checks"] if check["id"] == "runtime"][0]
    assert runtime_row["status"] == "unchecked"
    assert "not asked yet" in runtime_row["detail"].lower()


def test_a_port_that_answers_with_no_models_is_a_contradiction_not_a_ready():
    report = _report()
    readiness = command_center.build_readiness(
        report,
        runtime=command_center.RuntimeObservation(
            configured=True,
            url="http://127.0.0.1:8000",
            port_answered=True,
            model_count=0,
            probe="ran",
        ),
    )

    assert readiness["verdict"] == "contradiction"
    assert readiness["ready"] is False
    assert readiness["contradictions"][0]["id"] == "port-without-models"
    assert "zero models" in readiness["contradictions"][0]["detail"]
    assert readiness["next"]["action"] == "probe-local-models"
    assert readiness["runtime"]["model_count"] == 0
    assert readiness["runtime"]["basis"] == "measured"


def test_a_runtime_with_models_that_the_check_finds_unusable_is_reported_side_by_side():
    report = _report(providers=())
    readiness = command_center.build_readiness(
        report,
        runtime=command_center.RuntimeObservation(
            configured=True, url="http://127.0.0.1:8000", port_answered=True, model_count=3, probe="ran"
        ),
    )

    ids = [item["id"] for item in readiness["contradictions"]]
    assert "runtime-models-without-usable-selection" in ids
    assert readiness["verdict"] == "contradiction"


def test_checks_that_pass_while_the_runtime_stays_silent_are_printed_not_smoothed():
    readiness = command_center.build_readiness(
        _report(),
        runtime=command_center.RuntimeObservation(
            configured=True, url="http://127.0.0.1:8000", port_answered=False, model_count=None, probe="ran"
        ),
        workspace_dir="/tmp/ws",
    )

    assert readiness["contradictions"][0]["id"] == "checks-passed-without-runtime"
    assert readiness["verdict"] == "contradiction"
    assert readiness["runtime"]["basis"] == "measured"
    assert readiness["runtime"]["port_answered"] is False


def test_the_demo_health_payload_next_to_a_live_runtime_is_named():
    readiness = command_center.build_readiness(
        _report(),
        runtime=command_center.RuntimeObservation(
            configured=True, url="http://127.0.0.1:8000", port_answered=True, model_count=2, probe="ran"
        ),
        telemetry=command_center.TelemetryObservation(source="simulated_demo", omlx_online=False),
    )

    ids = [item["id"] for item in readiness["contradictions"]]
    assert "demo-health-with-live-runtime" in ids
    assert readiness["telemetry"]["simulated"] is True


def test_no_runtime_configured_is_optional_not_a_failure():
    readiness = command_center.build_readiness(
        _report(local_runtime_configured=False),
        runtime=command_center.RuntimeObservation(configured=False),
    )

    runtime_row = [check for check in readiness["checks"] if check["id"] == "runtime"][0]
    assert runtime_row["status"] == "optional"
    assert readiness["verdict"] == "ready"
    assert readiness["contradictions"] == []


def test_a_missing_workspace_is_the_next_action_and_names_the_step():
    ready = command_center.build_readiness(_report(workspace_writable=False, workspace_detail="missing"))
    assert ready["verdict"] == "needs-you"
    assert ready["next"]["id"] == "workspace"
    assert ready["next"]["action"] == "fix-workspace"
    assert ready["counts"]["total"] == ready["counts"]["total"]


def test_the_builder_performs_no_io():
    """A verdict that phones home on every render is a poll wearing a status pill."""
    source = (PROJECT_ROOT / "start_command_center.py").read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import socket", "urlopen", "requests.", "asyncio", "open("):
        assert forbidden not in source, f"start_command_center.py reaches for {forbidden}"


# ── the route ────────────────────────────────────────────────────────────────


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, item: str, value: str) -> None:
        self.values[(service, item)] = value

    def get_password(self, service: str, item: str) -> str | None:
        return self.values.get((service, item))

    def delete_password(self, service: str, item: str) -> None:
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
    return workspace


@pytest.fixture
def client(isolated_state: Path):
    # Entered as a context manager so the app's lifespan runs: the probe route reads
    # app.state.http, and without startup it would silently report "unavailable".
    with TestClient(studio.app) as test_client:
        yield test_client


def test_the_route_requires_the_token(client: TestClient):
    assert client.get("/api/start/command-center").status_code in (401, 403)


def test_the_default_read_contacts_nothing(client: TestClient):
    response = client.get("/api/start/command-center", headers=AUTH)
    assert response.status_code == 200
    readiness = response.json()["readiness"]
    assert readiness["runtime"]["probe"] == "not-run"
    assert readiness["basis"] == "derived"
    assert readiness["probe"] == "not-run"
    assert readiness["workspace"]["dir"] == str(studio.WORKSPACE_ROOT)


def test_a_probe_that_reports_no_models_is_a_contradiction(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    async def no_models(_client):
        return 0

    monkeypatch.setattr(studio, "_onboarding_probe_local_models", no_models)
    readiness = client.get("/api/start/command-center?probe=local", headers=AUTH).json()["readiness"]

    assert readiness["runtime"]["probe"] == "ran"
    assert readiness["runtime"]["port_answered"] is True
    assert readiness["runtime"]["model_count"] == 0
    assert readiness["verdict"] == "contradiction"


def test_a_probe_that_gets_no_answer_is_measured_and_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    async def unreachable(_client):
        return None

    monkeypatch.setattr(studio, "_onboarding_probe_local_models", unreachable)
    readiness = client.get("/api/start/command-center?probe=local", headers=AUTH).json()["readiness"]

    assert readiness["runtime"]["probe"] == "unavailable"
    assert readiness["runtime"]["port_answered"] is False
    assert readiness["basis"] == "derived"
    assert readiness["runtime"]["measured"] is False


def test_the_route_echoes_which_health_payload_the_pane_holds(client: TestClient):
    readiness = client.get("/api/start/command-center?telemetry=simulated_demo", headers=AUTH).json()["readiness"]
    assert readiness["telemetry"]["source"] == "simulated_demo"
    assert readiness["telemetry"]["simulated"] is True
    ignored = client.get("/api/start/command-center?telemetry=made-up", headers=AUTH).json()["readiness"]
    assert ignored["telemetry"]["source"] == "none"


def test_the_route_never_claims_ready_from_the_port_alone(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The acceptance criterion, as a test: readiness needs more than a live socket."""
    async def empty_runtime(_client):
        return 0

    monkeypatch.setattr(studio, "_onboarding_probe_local_models", empty_runtime)
    readiness = client.get("/api/start/command-center?probe=local", headers=AUTH).json()["readiness"]
    assert readiness["ready"] is False
    assert readiness["verdict"] != "ready"


# ── the pane ─────────────────────────────────────────────────────────────────


def _load_inventory():
    spec = importlib.util.spec_from_file_location(
        "uiux_start_command_center_inventory",
        PROJECT_ROOT / "scripts" / "uiux_start_command_center_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def inventory_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inventory = _load_inventory()
    html = FRONTEND.read_text(encoding="utf-8")

    def _make(transform):
        changed = transform(html)
        assert changed != html, "the transform did not change the markup, so the test proves nothing"
        path = tmp_path / "index.html"
        path.write_text(changed, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", path)
        return inventory.build_inventory()

    return _make


@pytest.fixture
def payload():
    return _load_inventory().build_inventory()


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_start_command_center_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_inventory_artifacts_exist(payload):
    json_path = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Start_Command_Center.json"
    doc_path = PROJECT_ROOT / "docs" / "uiux-2-4-start-command-center.md"
    assert json_path.exists() and doc_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["story"] == "UIV2-24"
    assert payload["findings"] == [], payload["findings"]


def test_the_composer_is_first_and_the_composer_is_primary(payload):
    assert payload["frontend"]["order"]["composer_first"] is True
    assert payload["frontend"]["composer"]["primary_button"] is True
    assert payload["frontend"]["composer"]["label_names_task"] is True


def test_the_verdict_and_the_blocker_are_before_the_details(payload):
    order = payload["frontend"]["order"]
    assert order["readiness_before_composer"] is True
    assert order["blocker_after_composer"] is True
    assert order["details_after_blocker"] is True


def test_the_checklist_health_and_workspace_are_disclosures(payload):
    details = payload["frontend"]["details"]
    assert details["checklist"] == "startDiagnostics"
    assert details["health"] == "startHealthDetails"
    assert details["workspace"] == "startWorkspaceDetails"
    assert details["all_closed_by_default"] is True
    assert details["summary_carries_state"] is True


def test_the_pane_prints_the_server_verdict(payload):
    verdict = payload["frontend"]["verdict"]
    assert verdict["reads_route"] == "/api/start/command-center"
    assert verdict["prints_headline"] is True
    assert verdict["prints_counts"] is True
    assert verdict["prints_basis"] is True
    assert verdict["recomputes"] is False


def test_the_pane_renders_the_contradictions_it_is_handed(payload):
    contradictions = payload["frontend"]["contradictions"]
    assert contradictions["container"] is True
    assert contradictions["from_payload"] is True
    assert contradictions["prints_evidence"] is True
    assert contradictions["alert_role"] is True


def test_the_blocker_action_reaches_the_control_it_names(payload):
    action = payload["frontend"]["blocker"]
    assert action["button"] is True
    assert action["opens_workspace"] is True
    assert action["delegates"] is True


def test_a_pane_that_hides_the_details_with_no_summary_is_caught(inventory_copy):
    def transform(text: str) -> str:
        needle = '<span id="onboardingSummary" class="status" data-tone="neutral" role="status">checking</span>'
        assert needle in text
        return text.replace(needle, "", 1)

    payload = inventory_copy(transform)
    assert "details-state-hidden" in _checks(payload)


def test_a_pane_that_puts_the_checklist_first_is_caught(inventory_copy):
    def transform(text: str) -> str:
        needle = '<p id="startReadinessLine"'
        assert needle in text
        # A composer that comes after the checklist: move the launch card to the end
        # of the pane, which is exactly the ordering the story replaces.
        start = text.index('<div class="m-card" id="startLaunchCard">')
        end = text.index("<!-- Work that already exists.", start)
        card = text[start:end]
        rest = text[:start] + text[end:]
        anchor = rest.index("        </details>\n      </div>\n    </section>")
        return rest[:anchor] + card + rest[anchor:]

    payload = inventory_copy(transform)
    assert "composer-not-first" in _checks(payload)


def test_a_pane_that_computes_its_own_verdict_is_caught(inventory_copy):
    def transform(text: str) -> str:
        needle = "const readiness = startReadiness;"
        assert needle in text
        return text.replace(
            needle,
            'const readiness = { ...startReadiness, verdict: startReadiness.counts.ready === startReadiness.counts.total ? "ready" : "needs-you" };',
            1,
        )

    payload = inventory_copy(transform)
    assert "verdict-recomputed" in _checks(payload)


def test_a_pane_that_drops_the_evidence_line_is_caught(inventory_copy):
    def transform(text: str) -> str:
        needle = 'basis.className = "start-readiness-basis";'
        assert needle in text
        return text.replace(needle, "basis.className = \"\";", 1)

    payload = inventory_copy(transform)
    assert "basis-missing" in _checks(payload)


def test_a_pane_that_shows_no_contradiction_evidence_is_caught(inventory_copy):
    def transform(text: str) -> str:
        needle = 'evidence.className = "start-contradiction-evidence";'
        assert needle in text
        return text.replace(needle, "evidence.className = \"\";", 1)

    payload = inventory_copy(transform)
    assert "contradiction-evidence-missing" in _checks(payload)
