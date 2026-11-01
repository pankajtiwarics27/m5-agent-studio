"""What each pipeline stage was given and what it left behind (UIUX-P3-06).

The four-seat pipeline is a chain. The architect writes a spec, the builder is handed it, the
critic is handed both, the judge is handed all three - and that chain lived only in
`api_council_stream`'s input lambdas. The screen drew four cards with a state and a tok/s: a seat
that produced nothing looked like a seat that had just started, no card said who fed whom, and a
run that was stopped looked like a run that was still going.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check that the walk is wired
to the server's own stream rather than to a fixture that could agree with it about anything.
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
SEED = PROJECT_ROOT / "scripts" / "pipeline_artifacts_seed.py"
WALK = PROJECT_ROOT / "scripts" / "pipeline_artifacts_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_pipeline_artifacts_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Artifacts.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-pipeline-artifacts.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "pipeline-artifacts"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_pipeline_artifacts_inventory", INVENTORY)
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
        [sys.executable, "scripts/uiux_pipeline_artifacts_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 25


def test_inventory_artifact_and_doc_exist():
    stored = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert stored["story"] == "UIUX-P3-06"
    assert stored["status"] == "clean"
    assert DOC_PATH.exists() and "UIUX-P3-06" in DOC_PATH.read_text(encoding="utf-8")


def test_the_chain_is_the_servers_own(payload: dict):
    assert payload["chain"]["declared_in_pane"] == payload["chain"]["declared_in_server"]
    server = payload["chain"]["declared_in_server"]
    assert server[1] == []
    assert server[2] == ["architect"]
    assert server[3] == ["architect", "coder"]
    assert server[4] == ["architect", "coder", "critic"]


# ── each rule fails when it stops being true ────────────────────────────────


def test_a_chain_that_drifts_from_the_server_is_a_finding(sources):
    # The pane suddenly claims the builder reads the critic too.
    broken = sources(
        html_transform=lambda html: html.replace(
            'reads: ["architect"] }',
            'reads: ["architect", "critic"] }',
            1,
        )
    )
    assert "the pane's chain is the server's chain" in _checks(broken)


def test_a_stage_card_without_its_rows_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('<div id="stage-handoff-${s}" class="stage-handoff"></div>', "", 1)
    )
    assert "every stage card carries a handoff row and an artifact row" in _checks(broken)


def test_a_handoff_that_counts_the_task_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "const chars = upstream.reduce((total, entry) => total + entry.chars, 0);",
            "const chars = seat.reads.length ? (councilRun ? councilRun.taskChars : 0) : 0;",
            1,
        )
    )
    assert "the handoff row measures the upstream it was handed, not the task" in _checks(broken)


def test_an_empty_seat_drawn_as_a_quiet_one_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('host.dataset.empty = empty ? "true" : "false";', "", 1)
    )
    assert "an empty seat is drawn as empty, not as a quiet one" in _checks(broken)


def test_a_stopped_run_drawn_as_a_finished_one_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "councilRun.stopped = !allDone;",
            "councilRun.stopped = false;",
            1,
        )
    )
    assert "a stopped run is stated as stopped" in _checks(broken)


def test_an_unsaved_run_given_an_id_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "councilRun.savedRunId = Number(d.run_id || 0);",
            "councilRun.savedRunId = 1;",
            1,
        )
    )
    assert "a stopped run says it was not saved, and a saved one is named by the id the server gave" in _checks(broken)


def test_a_size_the_pane_cannot_see_claimed_as_one_it_can_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "not the prompt the server assembles around them",
            "including the prompt the server assembles around them",
            1,
        )
    )
    assert "the sizes are the pane's own stream, said so on the page" in _checks(broken)


def test_an_unescaped_value_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<span class="stage-edge">ENDS THE RUN</span>',
            '<span class="stage-edge">ENDS THE RUN</span>',
            1,
        ).replace(
            "${escapeHtml(handoff.feeds.short)}",
            "${handoff.feeds.short}",
            1,
        )
    )
    assert "every value the rows draw is escaped" in _checks(broken)


def test_a_second_stage_renderer_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "function selectStageFilter(n) {",
            "function paintStageMarkdown(k) { document.getElementById(`stage-md-${k}`).innerHTML = stageBuffers[k].c; }\n  function selectStageFilter(n) {",
            1,
        )
    )
    assert "there is one renderer for a stage's markdown" in _checks(broken)


def test_an_invented_endpoint_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "window.M5Pipeline = {",
            "async function loadRunArtifacts(id) { return fetch(`/api/runs/${id}`); }\n  window.M5Pipeline = {",
            1,
        )
    )
    assert "the story adds no endpoint" in _checks(broken)


def test_a_server_route_the_pane_invents_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            '@app.post("/api/council/stream")',
            '@app.get("/api/runs")\nasync def api_runs():\n    return {}\n\n\n@app.post("/api/council/stream")',
            1,
        )
    )
    assert "the story adds no endpoint" in _checks(broken)


def test_a_stream_that_loses_an_event_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace('_sse("unloading"', '_sse("unload_done"', 1)
        if '_sse("unloading"' in server
        else server.replace('"unloading"', '"unload_done"', 1)
    )
    assert "the council route speaks the same seven events" in _checks(broken)


def test_a_run_record_that_loses_a_key_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            '"total_elapsed_s": total_elapsed, ',
            "",
            1,
        )
    )
    assert "the run record still carries what it carried" in _checks(broken)


def test_a_critic_that_silently_gains_skills_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            'AGENT_ROLES["critic"]["system"], lambda c:',
            'AGENT_ROLES["critic"]["system"] + skill_block, lambda c:',
            1,
        )
    )
    # UIUX-P4-02 moved the rule from "never claim skills" to "claim them only from the stream":
    # a critic that gains the playbook under a stream that says it was not given one is the same
    # finding under the new name.
    assert "the per-stage inputs are claimed only from the stream that now carries them" in _checks(broken)


def test_a_rows_that_churn_on_every_token_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "if (stageRowSignature[stage] === signature) return;",
            "if (false) return;",
            1,
        )
    )
    assert "the rows are redrawn on the stream's frame, guarded against churn" in _checks(broken)


def test_a_stale_run_inherited_by_the_next_one_is_a_finding(sources):
    # Every place that starts a view from scratch resets the rows; removing all of them is what
    # "inheriting the last run" looks like.
    broken = sources(html_transform=lambda html: html.replace("\n    stageRowSignature = {};", "", -1))
    assert "a new run clears the rows instead of inheriting them" in _checks(broken)


# ── the fixture stages arrivals, not records ────────────────────────────────


def test_the_seed_wraps_the_servers_own_stream():
    source = SEED.read_text(encoding="utf-8")
    assert "stream_any_model" in source and "original = studio.stream_any_model" in source
    # It must not write a run record or an event itself: those are the server's answers.
    assert 'save_run("' not in source
    assert "INSERT INTO runs" not in source
    assert '_sse(' not in source


def test_the_seed_stages_an_empty_seat_and_a_slow_one():
    source = SEED.read_text(encoding="utf-8")
    assert "critic-empty-seat-demo" in source
    assert "architect-slow-seat-demo" in source
    assert '"completion_tokens": 0' in source


def test_the_walk_reads_the_dom_and_names_what_it_proves():
    source = WALK.read_text(encoding="utf-8")
    assert "readPipeline" in source and "stage-handoff-" in source and "stage-artifact-" in source
    assert "M5Pipeline.artifactTotals" in source
    # The walk refuses to run without a browser, rather than reporting a pass it did not earn.
    assert "refuses to report a walk it did not perform" in source
    assert "pipeline_artifacts_seed.py" in source


def test_the_walk_would_fail_if_the_rows_disappeared():
    """The walk's expectations are read from the pane, so a missing row is a failed step."""
    source = WALK.read_text(encoding="utf-8")
    assert 'includes("reads")' in source
    assert "upstreamFor(stage)" in source
    assert 'empty === "true"' in source
    assert 'state === "partial"' in source


# ── the pages that must point at it ─────────────────────────────────────────


def test_program_docs_reference_the_story():
    program = (PROJECT_ROOT / "docs" / "uiux-2-0-program.md").read_text(encoding="utf-8")
    assert "UIUX-P3-06" in program
    assert "uiux-2-0-pipeline-artifacts.md" in program or "pipeline-artifacts" in program


def test_the_ledger_marks_the_story():
    ledger = json.loads(
        (PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json").read_text(encoding="utf-8")
    )
    stories = {story["id"]: story for story in ledger["stories"]}
    tracking = stories["UIUX-P3-06"]["tracking"]
    assert tracking["status"] in {"in_progress", "done"}
    assert tracking["progress_percent"] > 0
    assert "pipeline stages" in stories["UIUX-P3-06"]["title"].lower()


# ── what the story may not touch ───────────────────────────────────────────


def test_the_council_route_still_saves_after_the_last_stage():
    """The pane says a stopped run is not saved; that is true because of this order."""
    server = APP.read_text(encoding="utf-8")
    body = inventory.python_function(server, "api_council_stream")
    loop = body.find("async for event in orchestrator.run_stages(")
    saved = body.find('save_run("council"')
    assert 0 <= loop < saved


def test_the_pane_still_posts_only_what_it_posted():
    """P3-07 moved the run start out of `runCouncil`; the rule travels with the code."""
    html = FRONTEND.read_text(encoding="utf-8")
    start = inventory.function_body(html, "startPipelineRun")
    assert "/api/council/stream" in start
    assert "fetch(" not in start
    assert "/api/council/stream" not in inventory.function_body(html, "runCouncil")


def test_the_media_set_is_from_the_walk():
    names = {path.name for path in MEDIA.glob("*.png")}
    assert {
        "pipeline-all-stages.png",
        "pipeline-run-line.png",
        "pipeline-empty-seat.png",
        "pipeline-step-chars.png",
        "pipeline-stopped-run.png",
    } <= names
    for path in MEDIA.glob("*.png"):
        assert path.stat().st_size > 4000, f"{path.name} is too small to be a real capture"


def test_the_doc_names_the_limits_it_refuses_to_cross():
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "refuses to claim" in doc.lower()
    assert "prompt" in doc.lower()
    assert "UIUX-P3-07" in doc
