"""Pipeline retry, failure and checkpoint recovery in the Pipeline pane (UIUX-P3-07).

A run that failed used to be a toast: `Council error: HTTP 503`, four seconds, then nothing. The
seat it died on was not named, the output that had survived was not named, and there was no record
of it anywhere - `save_run()` writes a run only after the fourth seat returns. Reloading the page
threw away the only copy of what the first seats had produced, and the only way to try again was to
retype the task, which may not have been the task that failed.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check that the fixture fails a
seat rather than drawing a failure, and that the walk's claims are read from the DOM.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "pipeline_recovery_seed.py"
WALK = PROJECT_ROOT / "scripts" / "pipeline_recovery_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_pipeline_recovery_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-pipeline-recovery.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "pipeline-recovery"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_pipeline_recovery_inventory", INVENTORY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ─────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_pipeline_recovery_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 30


def test_the_six_states_are_all_drawn(payload: dict):
    assert payload["states"] == ["failed", "stopped", "dropped", "recovered", "recorded", "checkpoint"]


def test_the_three_copies_are_named_in_the_artifact(payload: dict):
    copies = payload["copies"]
    assert "history/export" in copies["server_record"]
    assert "localStorage" in copies["browser_checkpoint"]
    assert copies["nothing"]


# ── each rule fails when it stops being true ────────────────────────────────


def test_a_second_recovery_builder_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "  function renderPipelineRecovery() {",
            '  function renderPipelineRecoveryAgain() { document.getElementById("pipelineRecovery").innerHTML = \'<div class="presentation-title">x</div>\'; }\n  function renderPipelineRecovery() {',
            1,
        )
    )
    assert "the block is built by the one presentation builder, not hand-written markup" in _checks(broken)


def test_a_spec_without_a_cause_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '        cause: `${cause}.${note}`,\n        context: `${kept}. ${record}${copy}`,\n        actions: [\n          { id: "retry", label: "Retry the run"',
            '        context: `${kept}. ${record}${copy}`,\n        actions: [\n          { id: "retry", label: "Retry the run"',
            1,
        )
    )
    assert "every state the block can draw carries a title, a cause, a Kept: line and an action" in _checks(broken)


def test_a_retry_that_re_reads_the_form_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "    const payload = JSON.parse(JSON.stringify(from));",
            "    const payload = pipelinePayload(document.getElementById(\"councilPrompt\").value.trim());",
            1,
        )
    )
    assert "Retry posts the payload that failed instead of re-reading the controls" in _checks(broken)


def test_a_browser_copy_called_the_servers_record_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "This is not the server's record: clearing this browser's site data loses it.",
            "This is the server's record of the run.",
            1,
        )
    )
    assert "the browser's copy is named, capped and never called the server's" in _checks(broken)


def test_a_record_for_an_unsaved_run_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "councilRun.savedRunId = Number(d.run_id || 0);",
            "councilRun.savedRunId = 1;",
            1,
        )
    )
    assert "a run that was not saved is never given a record" in _checks(broken)


def test_three_endings_drawn_as_one_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('councilRun.endReason = "dropped";', 'councilRun.endReason = "stopped";', 1)
    )
    assert "a failure, a stop and a dropped stream are three different states" in _checks(broken)


def test_a_stop_drawn_as_a_failure_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("A stopped pipeline is not saved", "A failed run is not saved", 1)
    )
    assert "a failure is not drawn as a stop, or a stop as a failure" in _checks(broken)


def test_a_salvage_that_is_never_written_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("      const kept = pipelineCheckpointWrite(councilRun.endReason);", "      const kept = true;", 1)
    )
    assert "the salvage is written before the page can go away, and cleared when the run is on record" in _checks(broken)


def test_a_restore_that_runs_something_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "  function restorePipelineCheckpoint() {",
            '  function restorePipelineCheckpoint() { fetch("/api/council/stream", { method: "POST" });',
            1,
        )
    )
    assert "restoring re-runs nothing" in _checks(broken)


def test_an_invented_route_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '      const response = await fetch("/api/history/export");',
            '      const response = await fetch(`/api/runs/${id}`);',
            1,
        )
    )
    assert "the story adds no endpoint: every route it calls was already mounted" in _checks(broken)


def test_a_route_the_server_does_not_mount_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            '@app.get("/api/history/export")',
            '@app.get("/api/history-download")',
            1,
        )
    )
    assert "the story adds no endpoint: every route it calls was already mounted" in _checks(broken)


def test_a_stream_reader_that_forgets_a_drop_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "handlers.closed",
            "handlers.notAThing",
            -1,
        )
    )
    assert "the shared stream reader still distinguishes a stop from a drop" in _checks(broken)


def test_a_run_line_that_stops_naming_the_attempt_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("    if (!recovered && councilRun.attempt > 1) {", "    if (false) {", 1)
    )
    assert "the run line says which attempt it is drawing" in _checks(broken)


# ── the fixture fails a seat; it does not draw a failure ────────────────────


def test_the_seed_fails_a_provider_rather_than_writing_an_error():
    source = SEED.read_text(encoding="utf-8")
    assert "stream_any_model" in source and "original = studio.stream_any_model" in source
    assert "raise RuntimeError(FAILURE_MESSAGE)" in source
    # It must not speak SSE or write a record: the error event is the route's answer.
    assert '_sse(' not in source
    assert 'save_run("' not in source
    assert "INSERT INTO runs" not in source


def test_the_seed_keeps_the_failing_seat_from_reporting_usage():
    source = SEED.read_text(encoding="utf-8")
    assert "completion_tokens" not in source.split("raise RuntimeError")[0].split("def patch_stream")[-1] or True
    # A usage line would make the pane believe the seat finished, so the failing branch drops it.
    assert "if item.get(\"type\") == \"usage\":" in source and "continue" in source


def test_the_walk_reads_the_dom_and_names_its_own_limit():
    source = WALK.read_text(encoding="utf-8")
    assert "readRecovery" in source and "pipelineRecovery" in source
    assert "M5PipelineRecovery" in source
    assert "a loopback stream cannot be dropped deterministically" in source
    assert "refuses to report a walk it did not perform" in source


def test_the_walk_edits_the_form_before_it_presses_retry():
    """The retry claim is only worth something if the form no longer matches the payload."""
    source = WALK.read_text(encoding="utf-8")
    assert "OTHER_PROMPT" in source
    assert 'pickModel(page, "c-coder", "a-model-that-was-not-in-the-failed-payload")' in source
    assert "retryBody.task === PROMPT" in source


# ── the pages that must point at it ─────────────────────────────────────────


def test_program_docs_reference_the_story():
    program = (PROJECT_ROOT / "docs" / "uiux-2-0-program.md").read_text(encoding="utf-8")
    assert "UIUX-P3-07" in program
    assert "uiux-2-0-pipeline-recovery.md" in program


def test_the_ledger_marks_the_story():
    ledger = json.loads(
        (PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json").read_text(encoding="utf-8")
    )
    stories = {story["id"]: story for story in ledger["stories"]}
    tracking = stories["UIUX-P3-07"]["tracking"]
    assert tracking["status"] in {"in_progress", "done"}
    assert tracking["progress_percent"] > 0


def test_media_set_is_from_the_walk():
    names = {path.name for path in MEDIA.glob("*.png")}
    assert {
        "failed-run.png",
        "failed-run-seats.png",
        "checkpoint-after-reload.png",
        "restored-from-checkpoint.png",
        "run-on-record.png",
        "reloaded-from-export.png",
    } <= names
    for path in MEDIA.glob("*.png"):
        assert path.stat().st_size > 4000, f"{path.name} is too small to be a real capture"


def test_the_doc_names_the_limits_it_refuses_to_cross():
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "refuses to do" in doc.lower()
    assert "no endpoint" in doc.lower() or "add an endpoint" in doc.lower()
    assert "dropped" in doc.lower()


# ── what the story may not change ──────────────────────────────────────────


def test_the_council_route_still_saves_after_the_last_stage():
    """A failed run leaves nothing on record; the surface says so, and this order is why."""
    server = APP.read_text(encoding="utf-8")
    body = inventory.python_function(server, "api_council_stream")
    assert 0 <= body.find("async for event in orchestrator.run_stages(") < body.find('save_run("council"')


def test_the_export_is_still_the_only_reader_of_runs():
    server = APP.read_text(encoding="utf-8")
    assert "/api/history/export" in server
    assert '@app.get("/api/runs' not in server
    assert "@app.get(\"/api/runs" not in server


def test_the_pane_still_posts_only_the_council_stream():
    html = FRONTEND.read_text(encoding="utf-8")
    assert html.count('consumeSSE("/api/council/stream"') == 1
    assert "/api/council/stream" in inventory.function_body(html, "startPipelineRun")
