"""Council participant roles and resource readiness in the Council pane (UIUX-P4-01).

The pane had four pickers and no answers. It could not say what a seat was for, and it could not
say whether the model in a picker was resident, loadable, reachable, or missing an API key. On a
machine whose local runtime was down, every local seat was answered by the server's canned demo
text while the transcript drew a tok/s badge for it, and a seat that failed said so in a
four-second toast while its card went on saying "Connecting...".

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, check the fixture stages a
runtime rather than editing the payload, and check that the walk's claims are read from the DOM.
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
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "council_readiness_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_council_participants_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Participants.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-participants.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "council-participants"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_council_participants_inventory", INVENTORY)
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
        [sys.executable, "scripts/uiux_council_participants_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 30


def test_the_ten_states_are_all_drawn(payload: dict):
    assert payload["states"] == [
        "ready",
        "demo",
        "needs-load",
        "needs-memory",
        "loading",
        "needs-key",
        "not-configured",
        "unknown-model",
        "no-model",
        "unverified",
    ]


def test_the_seats_and_their_reads_are_named_in_the_artifact(payload: dict):
    assert [seat["role"] for seat in payload["seats"]] == [
        "Staff Architect",
        "Fast Coder / Analyst",
        "Adversarial Critic",
        "Lead Arbiter",
    ]
    assert "api/telemetry" in payload["reads"]["runtime"]
    assert "api/providers" in payload["reads"]["providers"]
    assert "summon" in payload["reads"]["round"]


# ── each rule fails when it stops being true ────────────────────────────────


def test_a_second_classifier_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "  function councilSeatReadiness(model, depth = 0) {",
            '  function councilSeatReadinessAgain(model) { return { state: "ready", tone: "ok", tag: "READY", text: "x", action: null }; }\n'
            "  function councilSeatReadiness(model, depth = 0) {",
            1,
        )
    )
    assert "there is one readiness classifier, and no second copy of the states" in _checks(broken)


def test_a_state_that_stops_being_drawn_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('state: "needs-key"', 'state: "ready"', 1)
    )
    assert "the states the rows can draw are exactly the ten the artifact lists" in _checks(broken)


def test_a_ready_seat_with_no_read_behind_it_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("    const telemetry = lastTelemetry;\n    if (!telemetry) {", "    const telemetry = lastTelemetry;\n    if (false) {", 1)
    )
    assert "no seat is ready because a picker says so: readiness starts from the reads" in _checks(broken)


def test_an_offline_runtime_drawn_as_ready_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("    if (!telemetry.omlx_online) {", "    if (false) {", 1)
    )
    assert "a local seat on a runtime that is not answering is demo text, not ready" in _checks(broken)


def test_a_combo_that_is_ready_because_it_exists_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "      const verdicts = depth >= 2 ? [] : targets.map(target => councilSeatReadiness(target, depth + 1));",
            "      const verdicts = targets.map(target => ({ state: 'ready' }));",
            1,
        )
    )
    assert "a combo is judged by its targets, not by existing" in _checks(broken)


def test_a_round_that_reads_the_pickers_behind_the_plans_back_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "    const plan = councilSeatPlan().map(seat => [",
            '    const plan = [["architect", document.getElementById("hallSeatArch").value, rm, false]].map(seat => [',
            1,
        )
    )
    assert "the round is dispatched from the same plan the rows read" in _checks(broken)


def test_a_demo_reply_given_a_tok_s_badge_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '      const meta = u.demo\n        ? `<span class="status" data-tone="warn"',
            '      const meta = u.decode_tps ? `<span class="badge green">${u.decode_tps} tok/s</span>` : u.demo\n        ? `<span class="status" data-tone="warn"',
            1,
        )
    )
    assert "a demo reply is labelled before it is given a tok/s badge" in _checks(broken)


def test_a_server_that_stops_marking_the_demo_usage_is_a_finding(sources):
    broken = sources(app_transform=lambda server: server.replace('            "demo": True,\n', "", 1))
    assert "the server marks the demo usage line, so the pane has something to read" in _checks(broken)


def test_a_failed_seat_left_saying_connecting_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "          renderActiveHall();  // the server's transcript, so the \"Connecting...\" card goes\n", "", 1
        )
    )
    assert "the failed seat redraws the server's transcript, so no card keeps saying Connecting" in _checks(broken)


def test_an_invented_route_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "`/api/models/${encodeURIComponent(model)}/load`",
            "`/api/models/${encodeURIComponent(model)}/force-load`",
            1,
        )
    )
    assert "the story adds no endpoint: every route it calls was already mounted" in _checks(broken)


def test_a_role_name_the_server_does_not_use_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('label: "Adversarial Critic"', 'label: "Critic"', 1)
    )
    assert "the role names are the server's own AGENT_ROLES labels, not retyped" in _checks(broken)


def test_a_summary_that_promises_a_round_the_loop_does_not_run_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("      try { await summonAgentPromise(rk, mod, rmode, pinIt); } catch (e) { break; }", "      try { await summonAgentPromise(rk, mod, rmode, pinIt); } catch (e) { }", 1)
    )
    assert "the summary says the round stops at the first seat that fails, because it does" in _checks(broken)


def test_a_row_that_rebuilds_under_the_keyboard_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("    if (container.dataset.signature === signature) return;", "    if (false) return;", 1)
    )
    assert "a row is only redrawn when what it says changes, and focus follows the button" in _checks(broken)


def test_a_second_poll_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "    // No timer of its own: the telemetry poll re-renders these rows every two seconds and",
            '    window.setInterval(() => renderCouncilReadiness(), 5000);\n    // No timer of its own: the telemetry poll re-renders these rows every two seconds and',
            1,
        )
    )
    assert "the rows are re-read on the way into the pane, and the poll does not fight the reader" in _checks(broken)


def test_a_seat_without_a_purpose_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<p class="seat-purpose">Weighs clash and pins the verdict.</p>', "", 1
        )
    )
    assert "four seats, one per role, each with its label, purpose and readiness row" in _checks(broken)


# ── the fixture stages the runtime; it does not edit the payload ────────────


def test_the_seed_points_the_app_at_a_runtime_and_speaks_no_sse_itself():
    source = SEED.read_text(encoding="utf-8")
    assert 'os.environ["OMLX_BASE_URL"] = runtime_url' in source
    assert "if studio.OMLX_BASE != runtime_url:" in source
    # The app's own telemetry route must be the thing that reads the fixture.
    assert "_demo_telemetry_payload" not in source
    assert "append_hall_message" not in source


def test_the_stand_in_runtime_answers_where_the_app_reads():
    """The memory totals live inside `active_models` in oMLX's payload, not beside it."""
    source = SEED.read_text(encoding="utf-8")
    body = inventory.python_function(source, "_activity_payload")
    assert body.count('"active_models"') == 1
    assert body.find('"active_models"') < body.find("model_memory_used") < body.find("model_memory_max")
    assert body.count("memory_pressure") == 1
    for route in ("/v1/models", "/admin/api/models", "/admin/api/activity", "/admin/api/global-settings"):
        assert f'"{route}"' in source, route


def test_the_stand_in_runtime_can_load_a_model_and_answer_a_seat():
    source = SEED.read_text(encoding="utf-8")
    assert '"/admin/api/models/"' in source and 'self.state.load(model_id)' in source
    assert "def _stream_completion" in source and "data: [DONE]" in source
    assert "self.state.unload(model_id)" in source


def test_the_offline_mode_saves_no_provider():
    source = SEED.read_text(encoding="utf-8")
    assert "provider=not args.offline" in source
    seed = inventory.python_function(source, "seed")
    assert "if provider:" in seed and "upsert_cloud_provider(" in seed


# ── the walk reads the DOM, and names its own limit ────────────────────────


def test_the_walk_reads_the_dom_and_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "readReadiness" in source and "hallSeatArchState" in source
    assert "readSummonState" in source and "hallSummonState" in source
    assert "refuses to report a walk it did not perform" in source
    assert "council_readiness_seed.py" in source


def test_the_walk_drives_the_failure_it_reports():
    """The failing seat is the keyless alias, and the seats before it really answered."""
    source = WALK.read_text(encoding="utf-8")
    assert 'await pickModel(page, "hallSeatJudge", ready.keyless_model);' in source
    assert "summon.cards.filter((card) => card.role).length === 3" in source
    assert "!card.demo && card.tokPerSecond" in source


def test_the_walk_checks_the_demo_reply_against_the_tok_s_badge():
    source = WALK.read_text(encoding="utf-8")
    assert "is not given a tok/s badge" in source
    assert "card.demo && !card.tokPerSecond" in source


# ── the pages that must point at it ────────────────────────────────────────


def test_program_docs_reference_the_story():
    program = (PROJECT_ROOT / "docs" / "uiux-2-0-program.md").read_text(encoding="utf-8")
    assert "UIUX-P4-01" in program
    assert "uiux-2-0-council-participants.md" in program


def test_the_ledger_marks_the_story():
    ledger = json.loads(
        (PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json").read_text(encoding="utf-8")
    )
    stories = {story["id"]: story for story in ledger["stories"]}
    tracking = stories["UIUX-P4-01"]["tracking"]
    assert tracking["status"] in {"in_progress", "done"}
    assert tracking["progress_percent"] > 0


def test_media_set_is_from_the_walk():
    names = {path.name for path in MEDIA.glob("*.png")}
    assert {
        "council-readiness-mixed.png",
        "council-readiness-offline.png",
        "council-seat-loaded.png",
        "council-summon-failed.png",
        "council-demo-replies.png",
        "council-readiness-narrow.png",
        "council-readiness-light.png",
    } <= names
    for path in MEDIA.glob("*.png"):
        assert path.stat().st_size > 4000, f"{path.name} is too small to be a real capture"


def test_the_doc_names_the_limits_it_refuses_to_cross():
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "refuses to do" in doc.lower()
    assert "add an endpoint" in doc.lower()
    assert "demo" in doc.lower()


# ── what the story may not change ──────────────────────────────────────────


def test_the_summon_route_still_saves_after_the_stream():
    server = APP.read_text(encoding="utf-8")
    body = inventory.python_function(server, "api_hall_summon")
    assert 0 <= body.find("async for item in stream_any_model(") < body.find("append_hall_message(")
    assert body.find("append_hall_message(") < body.find('_sse("summon_done"')


def test_the_demo_branch_still_writes_the_same_text_and_only_gains_a_flag():
    server = APP.read_text(encoding="utf-8")
    body = inventory.python_function(server, "_stream_omlx_completion")
    demo = body[body.find("if not await _omlx_is_online(client)") :]
    assert '"type": "usage"' in demo
    assert '"demo": True,' in demo
    assert demo.count("demo_reply =") == 4  # three harness branches and the plain reply


def test_the_pane_still_posts_one_summon_and_keeps_its_four_events():
    html = FRONTEND.read_text(encoding="utf-8")
    assert html.count('consumeSSE(`/api/hall/sessions/${encodeURIComponent(activeHallId)}/summon`') == 1
    server = APP.read_text(encoding="utf-8")
    body = inventory.python_function(server, "api_hall_summon")
    for event in ("summon_start", "token", "summon_done", "error"):
        assert f'_sse("{event}"' in body, event


def test_the_seat_pickers_are_still_the_same_controls():
    html = FRONTEND.read_text(encoding="utf-8")
    seats = html[html.find('<details class="council-settings" id="hallSeatsSettings"') : html.find('id="hallSingleSettings"')]
    for select_id in ("hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge"):
        assert inventory.element(seats, select_id), select_id
        assert f'for="{select_id}"' in seats, select_id
    single = html[html.find('id="hallSingleSettings"') : html.find('id="hallStream"')]
    for select_id in ("hallRoleSelect", "hallModelSelect", "hallSummonBtn"):
        assert inventory.element(single, select_id), select_id
