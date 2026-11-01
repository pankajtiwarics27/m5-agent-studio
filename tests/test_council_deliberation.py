"""Council deliberation stages and evidence in the Council pane (UIUX-P4-02).

A council stage card drew a state, a tok/s and a character count, and never said the ceiling the
count was measured against - so a seat that spent its whole token budget read exactly like one
that answered in three hundred tokens. The reasoning control offered three modes for "the run"
while the route gave them to seats 1 and 4 only. The Critic is deliberately given no skill
playbook, and no surface said so. And the handoff row counted `content` alone while the
orchestrator hands the next seat `content or reasoning`, so a seat that answered in thinking only
read as `0 chars`, exactly like a seat that answered nothing.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, drive the orchestrator itself for
the contract it publishes, and check the fixture is able to cut a reply and answer in reasoning
only - because a state a fixture cannot produce is a state no screenshot can show.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
ORCHESTRATION = PROJECT_ROOT / "workflow_orchestration.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "council_deliberation_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_council_deliberation_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Deliberation.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-deliberation.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "council-deliberation"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"

CAPTURES = (
    "council-deliberation-evidence.png",
    "council-deliberation-capped.png",
    "council-deliberation-narrow.png",
    "council-deliberation-light.png",
    "council-deliberation-offline.png",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_council_deliberation_inventory", INVENTORY)
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

    def _make(html_transform=None, app_transform=None, orchestration_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        orchestration = ORCHESTRATION.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
            app = changed
        if orchestration_transform:
            changed = orchestration_transform(orchestration)
            assert changed != orchestration, "the transform did not change the orchestrator, so the test proves nothing"
            orchestration = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        orchestration_path = tmp_path / "workflow_orchestration.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        orchestration_path.write_text(orchestration, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        monkeypatch.setattr(inventory, "ORCHESTRATION", orchestration_path)
        return inventory.build_inventory()

    return _make


def _checks(broken: dict) -> set[str]:
    return {finding["check"] for finding in broken["findings"]}


# ── the artifact itself ──────────────────────────────────────────────────────


def test_the_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 24
    assert payload["findings"] == []


def test_the_seats_carry_the_contract_the_route_gives_them(payload: dict):
    seats = {seat["seat"]: seat for seat in payload["seats"]}
    assert [seats[1]["route_key"], seats[2]["route_key"], seats[3]["route_key"], seats[4]["route_key"]] == [
        "architect",
        "coder",
        "critic",
        "judge",
    ]
    # The Critic is written without the skill playbook and without the caller's reasoning mode,
    # and the artifact records that rather than smoothing it into "all seats equal".
    assert seats[3]["skills"] is False
    assert all(seats[seat]["skills"] for seat in (1, 2, 4))
    assert seats[1]["reasoning_mode"] and seats[4]["reasoning_mode"]
    assert not seats[2]["reasoning_mode"] and not seats[3]["reasoning_mode"]


def test_the_pane_holds_no_copy_of_a_budget(payload: dict):
    assert payload["contract"]["budgets_in_the_frontend"] == []
    assert payload["contract"]["published_on"] == "stage_start"


def test_the_verdicts_are_the_six_the_artifact_lists(payload: dict):
    assert payload["verdicts"] == ["done", "capped", "demo", "no-output", "streaming", "not-run"]
    assert payload["evidence_table"]["columns"] == [
        "Seat",
        "Model",
        "Allowed",
        "Given",
        "Produced",
        "Carried",
        "Verdict",
    ]


# ── rules that must keep looking ────────────────────────────────────────────


def test_a_stream_that_stops_publishing_the_contract_is_a_finding(sources):
    broken = sources(orchestration_transform=lambda source: source.replace('"max_tokens": stage.max_tokens,', "", 1))
    assert "the orchestrator publishes the contract a stage runs under" in _checks(broken)


def test_a_critic_that_gains_the_playbook_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            'AGENT_ROLES["critic"]["system"], lambda c:',
            'AGENT_ROLES["critic"]["system"] + skill_block, lambda c:',
            1,
        )
    )
    assert "the council route hands the skills decision to the stream, seat by seat" in _checks(broken)


def test_a_reasoning_mode_that_reaches_every_seat_is_a_finding(sources):
    broken = sources(
        app_transform=lambda server: server.replace(
            'lambda c: f"Matter / Task:\\n{task}\\n\\nSeat 1 (Architect / Strategist) Output:\\n{c[\'architect\']}", 4096, meta={"skills": uses_skills}),',
            'lambda c: f"Matter / Task:\\n{task}\\n\\nSeat 1 (Architect / Strategist) Output:\\n{c[\'architect\']}", 4096, reasoning_mode, meta={"skills": uses_skills}),',
            1,
        )
    )
    assert "the reasoning mode reaches the two seats the control promises" in _checks(broken)


def test_a_pane_that_hard_codes_a_budget_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<span class="contract-chip" data-chip="budget">budget ',
            '<span class="contract-chip" data-chip="budget">budget 2400 ',
            1,
        )
    )
    assert "the pane keeps the contract from the event, and holds no copy of a budget" in _checks(broken)


def test_a_contract_row_that_appears_before_the_seat_starts_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'if (!view.contract) {',
            'if (false) {',
            1,
        )
    )
    assert "an absent contract is drawn as absent, not as a default" in _checks(broken)


def test_a_handoff_that_counts_content_alone_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'return { chars: reasoning.length, via: "reasoning" };',
            'return { chars: 0, via: "reasoning" };',
            1,
        )
    )
    assert "what a seat carries forward is its output or its reasoning" in _checks(broken)


def test_a_cut_reply_drawn_as_an_answer_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('cut at the length limit', 'answered', 1),
    )
    assert "six verdicts, and a cut reply is not drawn as an answer" in _checks(broken)


def test_a_demo_row_given_a_figure_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("demo text, no tok/s", "demo text", 1),
    )
    assert "a demo reply is drawn as demo text, and is given no figure it did not earn" in _checks(broken)


def test_an_unescaped_value_in_the_table_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<td>${escapeHtml(row.seat)}</td>',
            "<td>${row.seat}</td>",
            1,
        )
    )
    assert "every value the table draws is escaped" in _checks(broken)


def test_a_table_redrawn_on_every_token_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "host.dataset.signature !== signature || host.hidden",
            "true",
            1,
        )
    )
    assert "the table is not rewritten while nothing in it has changed" in _checks(broken)


def test_a_redraw_that_drops_the_focus_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("if (replacement) replacement.focus();", "", 1),
    )
    assert "a redraw under the copy control keeps the focus on it" in _checks(broken)


def test_an_invented_route_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "const markdown = deliberationEvidenceMarkdown();",
            'const markdown = deliberationEvidenceMarkdown(); fetch("/api/deliberation/export");',
            1,
        )
    )
    assert "the pane posts nothing new: the evidence is read from the stream it already had" in _checks(broken)


# ── the server side, driven directly ────────────────────────────────────────


async def _events_for(stage):
    from workflow_orchestration import WorkflowOrchestrator

    async def stream(model, messages, temperature, max_tokens, reasoning_mode):
        assert max_tokens == stage.max_tokens
        yield {"type": "content", "text": "ok"}
        yield {"type": "usage", "completion_tokens": 1, "decode_tps": 1.0, "finish_reason": "stop"}

    async def unload(model):
        return None

    orchestrator = WorkflowOrchestrator(
        stream_model=stream,
        record_usage=lambda workflow, model, usage: None,
        is_local_model=lambda model: False,
        unload_model=unload,
    )
    collected = []
    async for event in orchestrator.run_stages([stage], workflow="council", auto_unload=False):
        collected.append(event)
    return collected


def test_the_orchestrator_publishes_the_contract_a_stage_runs_under():
    from workflow_orchestration import WorkflowStage

    stage = WorkflowStage(1, "architect", "1. Architect", "a-model", "sys", lambda _c: "task", 2400, "low", meta={"skills": True})
    events = asyncio.run(_events_for(stage))
    start = next(event for event in events if event.kind == "stage_start")
    assert start.data["max_tokens"] == 2400
    assert start.data["reasoning_mode"] == "low"
    assert start.data["skills"] is True
    assert start.data["model"] == "a-model"


def test_a_stage_without_a_contract_still_publishes_its_budget_and_defaults_reasoning_off():
    from workflow_orchestration import WorkflowStage

    stage = WorkflowStage(2, "coder", "2. Builder", "a-model", "sys", lambda _c: "task", 4096)
    events = asyncio.run(_events_for(stage))
    start = next(event for event in events if event.kind == "stage_start")
    assert start.data["max_tokens"] == 4096
    assert start.data["reasoning_mode"] == "off"
    assert "skills" not in start.data


def test_the_council_route_states_the_critic_without_skills():
    source = APP.read_text(encoding="utf-8")
    start = source.find("async def api_council_stream")
    body = source[start : source.find('@app.post("/api/debate/stream")', start)]
    critic_line = next(line for line in body.splitlines() if 'WorkflowStage(3, "critic"' in line)
    assert "skill_block" not in critic_line
    assert 'meta={"skills": False}' in critic_line


def test_the_fixture_can_cut_a_reply_and_answer_in_reasoning_only():
    source = SEED.read_text(encoding="utf-8")
    assert '"finish_reason": "length" if capped else "stop"' in source
    assert '"reasoning_content": word + " "' in source
    assert "--cap-model" in source and "--reasoning-model" in source


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    for token in ("--cap-model", "--reasoning-model", "M5Deliberation.markdown()", "docs/media/council-deliberation"):
        assert token in source, token
    assert "readStages" in source and "readEvidence" in source


# ── the evidence this story ships ───────────────────────────────────────────


def test_the_captures_exist_and_are_the_widths_the_readme_claims():
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    for name in CAPTURES:
        path = MEDIA / name
        assert path.exists(), name
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width = struct.unpack(">I", data[16:20])[0]
        if "narrow" in name:
            assert width == 480, name
        elif name.endswith("evidence.png"):
            assert width == 1440, name
        else:
            # An element capture of the pane at a 1440 px viewport, not a hand-made crop.
            assert 400 < width <= 1440, name
        assert name in readme, name
    assert "nothing here was drawn by hand" in readme


def test_the_doc_states_the_verdicts_the_pane_draws():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for verdict in ("capped", "demo text", "no output", "streaming"):
        assert verdict in doc, verdict
    assert "every ceiling on screen arrived on `stage_start`" in doc
    assert "council_deliberation_walk.mjs" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P4-02")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert any("commit" in entry.lower() or "tree" in entry.lower() for entry in tracking["evidence"])
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P4-02"
