from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import demo_tour as tour

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_tour.py"
TRANSCRIPT = ROOT / tour.TRANSCRIPT_PATH


def _run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
    )


# ---------------------------------------------------------------------------
# Declarative surface
# ---------------------------------------------------------------------------


def test_every_step_names_a_real_pane() -> None:
    panes = {pane.tab for pane in tour.PANES}

    assert tour.STEPS
    for step in tour.STEPS:
        assert step.pane in panes, step.step_id


def test_panes_match_the_ui_tabs() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import validate_screenshots
    finally:
        sys.path.pop(0)

    assert tuple(pane.tab for pane in tour.PANES) == validate_screenshots.ui_tabs()


def test_steps_only_use_endpoints_the_app_declares() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    for step in tour.STEPS:
        path = step.request.path.split("?", 1)[0]
        template = path.replace(f"/{tour.DEMO_COMBO_ALIAS}", "/{alias}")
        assert f'"{template}"' in source, f"{step.step_id} uses undeclared route {template}"


def test_step_ids_are_unique_and_ordered() -> None:
    ids = [step.step_id for step in tour.STEPS]

    assert len(ids) == len(set(ids))
    assert ids[0] == "preflight"
    assert ids[-1] == "cleanup"


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------


def _environment(tmp_path: Path) -> tour.DemoEnvironment:
    return tour.make_environment(tmp_path / "demo")


def test_environment_is_isolated_and_authenticated(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    env = environment.environ({})

    tour.assert_isolated(env, environment)
    assert env["STUDIO_EXECUTION_MODE"] == "disabled"
    assert env["STUDIO_ALLOW_HOST_EXECUTION"] == "0"
    assert env["STUDIO_AUTH_DISABLED"] == "0"
    assert env["STUDIO_API_TOKEN"] == ""
    assert Path(env["STUDIO_API_TOKEN_FILE"]) == environment.token_path
    assert Path(env["STUDIO_DB_PATH"]) == environment.db_path
    assert Path(env["STUDIO_WORKSPACE"]) == environment.workspace_dir
    assert environment.harness_dir.is_relative_to(environment.root)
    assert env["STUDIO_DB_PATH"].startswith(str(tmp_path))


@pytest.mark.parametrize(
    "override, message",
    [
        ({"STUDIO_EXECUTION_MODE": "host"}, "never execute code"),
        ({"STUDIO_ALLOW_HOST_EXECUTION": "1"}, "host execution"),
        ({"STUDIO_AUTH_DISABLED": "1"}, "authentication"),
        ({"STUDIO_API_TOKEN": "a" * 40}, "generate its own token"),
        ({"STUDIO_DB_PATH": "/tmp/real-user-data.db"}, "throwaway database"),
        ({"STUDIO_WORKSPACE": "/tmp/real-user-workspace"}, "throwaway workspace"),
        ({"STUDIO_API_TOKEN_FILE": "/tmp/real-user-token"}, "throwaway token"),
    ],
)
def test_the_demo_refuses_an_unsafe_environment(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    environment = _environment(tmp_path)
    env = {**environment.environ({}), **override}

    with pytest.raises(tour.DemoEnvironmentError, match=message):
        tour.assert_isolated(env, environment)


def test_token_must_be_generated_and_long_enough(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.token_path.write_text("short\n", encoding="utf-8")

    with pytest.raises(tour.DemoEnvironmentError, match="32-character"):
        environment.token()

    environment.token_path.unlink()
    with pytest.raises(tour.DemoEnvironmentError, match="never written"):
        environment.token()


# ---------------------------------------------------------------------------
# Running with a stub client
# ---------------------------------------------------------------------------


class StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


PAYLOADS: dict[str, Any] = {
    "GET /api/onboarding/preflight": {
        "preflight": {
            "steps": [
                {"id": "auth", "status": "ready"},
                {"id": "model-source", "status": "action-required"},
                {"id": "execution", "status": "optional"},
            ],
            "next_step": {"title": "Connect a model source"},
        }
    },
    "GET /api/providers": {"presets": {"a": {}, "b": {}}, "providers": []},
    "POST /api/combos": {"combo": {"alias": "demo-combo", "targets": ["model"]}},
    "POST /api/opencontext/docs": {"doc": {"title": "Local-first demo note", "folder": "demo"}},
    "GET /api/opencontext/search?q=local-first": {"docs": [{"id": "oc-1"}]},
    "GET /api/opencontext/lint": {"total_docs": 2, "health_score": 86, "orphan_docs": ["oc-1"], "thin_docs": []},
    "GET /api/mcp/servers": {"presets": {"x": {}}, "servers": []},
    "POST /api/hall/sessions": {"title": "t", "participants": ["Human Operative"]},
    "GET /api/history/export": {"tables": {"runs": []}, "excluded_tables": {"a": ["x", "y"]}, "redaction": {"enabled": True}},
    "DELETE /api/combos/demo-combo": {"status": "ok"},
}


class StubClient:
    def __init__(self, override: dict[str, Any] | None = None, status: int | None = None) -> None:
        self.calls: list[str] = []
        self.override = override or {}
        self.status = status

    def _respond(self, method: str, path: str) -> StubResponse:
        key = f"{method} {path}"
        self.calls.append(key)
        if key in self.override:
            value = self.override[key]
            if isinstance(value, int):
                return StubResponse(value, {"detail": "stub"})
            return StubResponse(200, value)
        return StubResponse(self.status or 200, PAYLOADS[key])

    def get(self, path: str, **_kwargs) -> StubResponse:
        return self._respond("GET", path)

    def post(self, path: str, **_kwargs) -> StubResponse:
        return self._respond("POST", path)

    def delete(self, path: str, **_kwargs) -> StubResponse:
        return self._respond("DELETE", path)


def test_run_tour_records_every_step_in_order() -> None:
    client = StubClient()
    outcomes = tour.run_tour(client, "t" * 32)

    assert [outcome.step_id for outcome in outcomes] == [step.step_id for step in tour.STEPS]
    assert client.calls == [f"{step.request.method} {step.request.path}" for step in tour.STEPS]
    assert all(outcome.status_code == 200 for outcome in outcomes)
    assert all(outcome.summary for outcome in outcomes)


def test_run_tour_raises_on_an_unexpected_status() -> None:
    client = StubClient(override={"POST /api/combos": 400})

    with pytest.raises(tour.DemoStepError, match="Step 'combo'"):
        tour.run_tour(client, "t" * 32)


def test_run_tour_reports_why_a_step_failed() -> None:
    client = StubClient(override={"GET /api/opencontext/lint": 500})

    with pytest.raises(tour.DemoStepError, match="returned 500"):
        tour.run_tour(client, "t" * 32)


def test_preflight_summary_counts_statuses() -> None:
    summary = tour._summarise_preflight(PAYLOADS["GET /api/onboarding/preflight"])

    assert "3 checklist steps" in summary
    assert "1 ready" in summary
    assert "1 optional" in summary
    assert "Connect a model source" in summary


def test_summaries_tolerate_unexpected_payloads() -> None:
    assert "unexpected payload" in tour._summarise_lint([])
    assert "unexpected payload" in tour._summarise_session("nope")
    assert "no documents" in tour._summarise_search({})
    assert "0 provider presets" in tour._summarise_providers({})


# ---------------------------------------------------------------------------
# Rendering and determinism
# ---------------------------------------------------------------------------


def _rendered() -> str:
    return tour.render_transcript(tour.run_tour(StubClient(), "t" * 32))


def test_transcript_lists_every_step_and_pane() -> None:
    rendered = _rendered()

    for step in tour.STEPS:
        assert step.title in rendered
    for pane in tour.PANES:
        assert f"`{pane.tab}`" in rendered
    assert rendered.endswith("\n")


def test_transcript_is_byte_stable() -> None:
    assert _rendered() == _rendered()


def test_scrub_removes_generated_ids_paths_and_timestamps() -> None:
    dirty = "doc oc-80b5e41f at /tmp/m5-demo-abc/data/studio.db updated 1793296972.21 and ch-a40b2743"

    scrubbed = tour.scrub(dirty)

    assert "oc-80b5e41f" not in scrubbed
    assert "ch-a40b2743" not in scrubbed
    assert "/tmp/m5-demo-abc" not in scrubbed
    assert "1793296972" not in scrubbed
    assert "<temporary-directory>" in scrubbed


def test_json_view_matches_the_steps() -> None:
    payload = json.loads(tour.transcript_json(tour.run_tour(StubClient(), "t" * 32)))

    assert [entry["step"] for entry in payload] == [step.step_id for step in tour.STEPS]
    assert all(entry["status"] == 200 for entry in payload)


# ---------------------------------------------------------------------------
# Committed transcript and CLI
# ---------------------------------------------------------------------------


def test_committed_transcript_matches_a_fresh_run() -> None:
    result = _run("--check")

    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout


def test_check_detects_drift(tmp_path: Path) -> None:
    drifted = TRANSCRIPT.read_text(encoding="utf-8").replace("Observed:", "Seen:", 1)
    target = tmp_path / "demo-transcript.md"
    target.write_text(drifted, encoding="utf-8")

    import importlib.util

    spec = importlib.util.spec_from_file_location("demo_tour_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._check(target, TRANSCRIPT.read_text(encoding="utf-8")) == 1


def test_check_fails_when_the_transcript_is_missing(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("demo_tour_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._check(tmp_path / "absent.md", "content") == 1


def test_cli_prints_the_same_transcript_that_is_committed() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    assert result.stdout == TRANSCRIPT.read_text(encoding="utf-8")


def test_cli_json_mode_is_machine_readable() -> None:
    result = _run("--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload) == len(tour.STEPS)
    assert payload[-1]["step"] == "cleanup"


def test_transcript_contains_no_secrets_or_absolute_paths() -> None:
    text = TRANSCRIPT.read_text(encoding="utf-8")

    assert "Bearer " not in text
    assert "/home/" not in text
    assert "/tmp/" not in text
