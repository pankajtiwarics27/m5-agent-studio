from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as studio
import onboarding_preflight as preflight
from onboarding_preflight import PreflightInputs, ProviderState, build_report

AUTH = {"Authorization": "Bearer test-token"}


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
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    monkeypatch.setattr(studio.keyring, "set_password", backend.set_password)
    monkeypatch.setattr(studio.keyring, "get_password", backend.get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring
) -> Path:
    db = tmp_path / "state" / "studio.db"
    db.parent.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "test-token")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    studio.init_db()
    return workspace


def _steps(report: preflight.PreflightReport) -> dict[str, preflight.Step]:
    return {step.id: step for step in report.steps}


def _ready_inputs(**overrides: object) -> PreflightInputs:
    base = {
        "auth_ready": True,
        "local_runtime_configured": True,
        "providers": (ProviderState(alias="openai", enabled=True, has_credential=True, model_count=3),),
        "combo_count": 1,
        "execution_mode": "disabled",
        "sandbox_runtime_available": False,
        "workspace_writable": True,
        "workspace_detail": "Workspace directory is writable.",
    }
    base.update(overrides)
    return PreflightInputs(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure preflight contract
# ---------------------------------------------------------------------------


def test_fresh_install_reports_a_single_actionable_next_step() -> None:
    report = build_report(
        PreflightInputs(
            auth_ready=True,
            local_runtime_configured=True,
            workspace_writable=True,
        )
    )

    assert not report.ready
    assert [step.id for step in report.steps] == list(preflight.STEP_ORDER)
    next_step = report.next_step
    assert next_step is not None and next_step.id == "model-source"
    assert next_step.status == "action-required"
    assert next_step.action == "open-providers"
    # Execution is intentionally optional for a first chat turn.
    assert _steps(report)["execution"].status == "optional"


def test_fully_configured_install_is_ready_and_names_the_first_task() -> None:
    report = build_report(_ready_inputs())

    assert report.ready
    assert report.next_step is None
    assert report.blocked is False
    payload = report.to_dict()
    assert payload["first_task"]["prompt"] == preflight.FIRST_TASK_PROMPT
    assert payload["summary"] == f"{len(preflight.STEP_ORDER)} of {len(preflight.STEP_ORDER)} steps ready"


def test_step_order_and_payload_are_deterministic_and_json_round_trippable() -> None:
    first = build_report(_ready_inputs()).to_dict()
    second = build_report(_ready_inputs()).to_dict()

    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert [step["id"] for step in first["steps"]] == list(preflight.STEP_ORDER)


def test_provider_without_credential_does_not_count_as_a_model_source() -> None:
    report = build_report(
        _ready_inputs(
            providers=(ProviderState(alias="anthropic", enabled=True, has_credential=False, model_count=5),),
            combo_count=0,
        )
    )
    steps = _steps(report)

    assert steps["model-source"].status == "action-required"
    assert "missing a credential: anthropic" in steps["model-source"].detail
    assert steps["model-available"].status == "action-required"
    assert not report.ready


def test_disabled_provider_does_not_count_as_a_model_source() -> None:
    report = build_report(
        _ready_inputs(
            providers=(ProviderState(alias="ollama_cloud", enabled=False, has_credential=True, model_count=4),),
            combo_count=0,
        )
    )
    steps = _steps(report)

    assert steps["model-source"].status == "action-required"
    assert "disabled: ollama_cloud" in steps["model-source"].detail
    assert report.ready is False


def test_configured_provider_without_models_asks_for_a_fetch_not_a_pass() -> None:
    report = build_report(
        _ready_inputs(
            providers=(ProviderState(alias="openai", enabled=True, has_credential=True, model_count=0),),
            combo_count=0,
        )
    )
    steps = _steps(report)

    assert steps["model-source"].status == "ready"
    assert steps["model-available"].status == "action-required"
    assert steps["model-available"].action == "open-providers"
    assert steps["first-task"].status == "action-required"


def test_combos_alone_can_satisfy_model_availability() -> None:
    report = build_report(
        _ready_inputs(
            providers=(ProviderState(alias="openai", enabled=True, has_credential=True, model_count=0),),
            combo_count=1,
        )
    )

    assert _steps(report)["model-available"].status == "ready"


def test_missing_workspace_blocks_with_a_workspace_action() -> None:
    report = build_report(_ready_inputs(workspace_writable=False, workspace_detail="Workspace directory is missing: /x."))

    step = _steps(report)["workspace"]
    assert step.status == "action-required"
    assert step.action == "fix-workspace"
    assert "missing" in step.detail
    assert report.ready is False


def test_missing_token_is_blocked_rather_than_merely_actionable() -> None:
    report = build_report(_ready_inputs(auth_ready=False))

    assert _steps(report)["auth"].status == "blocked"
    assert report.blocked is True
    assert report.next_step is not None and report.next_step.id == "auth"


@pytest.mark.parametrize(
    ("mode", "runtime", "expected"),
    [
        ("disabled", False, "optional"),
        ("host", False, "optional"),
        ("sandbox", True, "ready"),
        ("sandbox", False, "action-required"),
    ],
)
def test_execution_step_degrades_by_capability(mode: str, runtime: bool, expected: str) -> None:
    report = build_report(
        _ready_inputs(execution_mode=mode, sandbox_runtime_available=runtime)  # type: ignore[arg-type]
    )
    assert _steps(report)["execution"].status == expected


def test_sandbox_without_runtime_still_lets_a_chat_first_task_through() -> None:
    report = build_report(_ready_inputs(execution_mode="sandbox", sandbox_runtime_available=False))

    assert _steps(report)["first-task"].status == "ready"
    assert report.ready is False  # the missing runtime is surfaced, not hidden


def test_host_mode_is_flagged_as_unsandboxed() -> None:
    report = build_report(_ready_inputs(execution_mode="host"))

    detail = _steps(report)["execution"].detail.lower()
    assert "unsandboxed" in detail or "no sandbox" in detail


def test_report_never_carries_credentials_or_aliases_beyond_names() -> None:
    secret = "sk-live-0123456789abcdef"
    inputs = _ready_inputs(
        providers=(ProviderState(alias="openai", enabled=True, has_credential=True, model_count=2),)
    )
    report = build_report(inputs)
    serialized = json.dumps(report.to_dict()).lower()

    for leak in (secret.lower(), "api_key", "apikey", "password", "keyring:", "sk-"):
        assert leak not in serialized
    assert "openai" in serialized
    # Provider state is credential-free by construction: only a boolean survives.
    assert set(ProviderState.__dataclass_fields__) == {"alias", "enabled", "has_credential", "model_count"}


def test_local_runtime_hint_appears_only_when_nothing_is_discovered() -> None:
    empty = build_report(PreflightInputs(auth_ready=True, local_runtime_configured=True, workspace_writable=True))
    configured = build_report(_ready_inputs())

    assert any("local runtime" in note.lower() for note in empty.notes)
    assert not any("start the runtime" in note.lower() for note in configured.notes)


# ---------------------------------------------------------------------------
# App adapter and route
# ---------------------------------------------------------------------------


def test_adapter_reads_only_local_state_and_defaults_to_not_ready(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preflight must not perform network calls")

    monkeypatch.setattr(studio, "_validate_outbound_url", explode)
    monkeypatch.setattr(studio.httpx.AsyncClient, "get", explode)

    inputs = studio.onboarding_preflight_inputs()

    assert inputs.auth_ready is True
    assert inputs.workspace_writable is True
    assert inputs.providers == ()
    report = build_report(inputs)
    assert report.next_step is not None and report.next_step.id == "model-source"


def test_adapter_reflects_configured_provider_and_models(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "STUDIO_ALLOW_PRIVATE_URLS", True)
    studio.upsert_cloud_provider(
        alias="local_echo",
        name="Local echo",
        wire="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-test-value",
        models=["echo-small", "echo-large"],
        enabled=True,
    )

    inputs = studio.onboarding_preflight_inputs()
    report = build_report(inputs)

    assert [(p.alias, p.has_credential, p.model_count) for p in inputs.providers] == [("local_echo", True, 2)]
    assert _steps(report)["model-available"].status == "ready"
    assert report.ready is True
    serialized = json.dumps(report.to_dict())
    assert "sk-test-value" not in serialized


def test_adapter_survives_a_broken_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(studio, "list_cloud_providers", broken)
    monkeypatch.setattr(studio, "list_model_combos", broken)

    inputs = studio.onboarding_preflight_inputs()

    assert inputs.providers == ()
    assert inputs.combo_count == 0
    assert build_report(inputs).ready is False


def test_preflight_route_requires_authentication(isolated_state: Path) -> None:
    client = TestClient(studio.app)

    assert client.get("/api/onboarding/preflight").status_code == 401


def test_preflight_route_returns_the_plan(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_ALLOW_PRIVATE_URLS", True)
    studio.upsert_cloud_provider(
        alias="local_echo",
        name="Local echo",
        wire="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-test-value",
        models=["echo-small"],
        enabled=True,
    )
    client = TestClient(studio.app)

    response = client.get("/api/onboarding/preflight", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    plan = body["preflight"]
    assert plan["ready"] is True
    assert plan["next_step"] is None
    assert [step["id"] for step in plan["steps"]] == list(preflight.STEP_ORDER)
    assert plan["first_task"]["prompt"]
    assert "sk-test-value" not in response.text


def test_preflight_route_reports_the_blocking_step_for_a_fresh_install(isolated_state: Path) -> None:
    client = TestClient(studio.app)

    plan = client.get("/api/onboarding/preflight", headers=AUTH).json()["preflight"]

    assert plan["ready"] is False
    assert plan["next_step"]["id"] == "model-source"
    assert plan["next_step"]["action"] == "open-providers"
