"""Pause, cancel, resume and crash recovery in the Harness pane (UIUX-P3-05).

A run that stopped used to be one line in the log: `Paused at step 4 — operator_pause_requested`.
The four routes built for the moment a run stops - `POST …/pause`, `…/cancel`, `…/resume`,
`…/contain` - were reachable from the API and from nowhere a person could see; the pane's only
control was Abort, which ends the view rather than the work; and a worker that died left the task
saying `running`, which the pane drew as a running task.

The inventory is the artifact. These tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check that the fixture the
browser walk uses is the state machine rather than a hand-written record.
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
SEED = PROJECT_ROOT / "scripts" / "harness_recovery_seed.py"
WALK = PROJECT_ROOT / "scripts" / "harness_recovery_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_harness_recovery_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-recovery.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "harness-recovery"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_harness_recovery_inventory", INVENTORY)
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
        [sys.executable, "scripts/uiux_harness_recovery_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Pause, cancel, resume, and the morning after (UIUX-P3-05)")
    assert "Do not edit by hand" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P3-05"
    assert data["findings"] == []
    assert data["totals"]["checks"] == len(data["checks"])
    assert data["totals"]["actions"] == 6
    # The surface posts to four routes that all predate this story; it added none.
    assert data["routes"] == [
        "POST /api/harness/sessions/{session_id}/pause",
        "POST /api/harness/sessions/{session_id}/cancel",
        "POST /api/harness/sessions/{session_id}/resume",
        "POST /api/harness/abort/{session_id}",
        "GET /api/harness/sessions/{session_id}/state",
        "GET /api/harness/sessions/{session_id}/task",
    ]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


def test_a_second_recovery_renderer_is_refused(sources):
    broken = sources(html_transform=lambda html: html.replace(
        'if (ev.type === "turn/paused" || ev.type === "turn/cancelled" || ev.type === "turn/resume_prepared") {',
        'if (ev.type === "turn/paused") return "Paused at step 4";\n'
        '    if (ev.type === "turn/paused" || ev.type === "turn/cancelled" || ev.type === "turn/resume_prepared") {',
        1,
    ))
    assert "the three recovery events have exactly one renderer" in _checks(broken), (
        "a second renderer for one of the three events must be refused"
    )


# ── a stopped run is not a running one ──────────────────────────────────────


def test_a_task_this_pane_is_not_running_is_not_drawn_as_live(sources):
    broken = sources(html_transform=lambda html: html.replace(
        "const following = Boolean(harnessRunActive);",
        "const following = Boolean(harnessRunActive) || Boolean(harnessEventStreamAbort);",
        1,
    ))
    assert "a running task this pane is not running is not drawn as a live one" in _checks(broken), (
        "an open log stream must not be read as a live worker"
    )


def test_a_refused_action_must_be_disabled_before_it_reaches_the_network(sources):
    broken = sources(html_transform=lambda html: html.replace(
        'if (action && action.disabled) {\n      harnessRecoverySay(`Not available: ${action.reason}`, "warn");\n      return;\n    }',
        "",
        1,
    ))
    assert "a refused action is refused before any request is made" in _checks(broken)


def test_a_disabled_action_without_a_reason_is_refused(sources):
    def drop_reason(html: str) -> str:
        return html.replace(
            'button.setAttribute("aria-describedby", "presentationWhyIsForTheseActions");',
            "",
            1,
        )

    broken = sources(html_transform=drop_reason)
    assert "a refusal is drawn as a disabled button with its reason, never as a hidden one" in _checks(broken)


def test_cancel_must_keep_its_two_press_confirmation(sources):
    broken = sources(html_transform=lambda html: html.replace(
        'if (id !== "cancel") harnessRecoveryArmedCancel = 0;',
        "harnessRecoveryArmedCancel = 0;",
        1,
    ))
    assert "cancel takes two presses and any other action disarms it" in _checks(broken)


def test_a_cancelled_task_must_not_offer_a_resume(sources):
    broken = sources(html_transform=lambda html: html.replace(
        '{ id: "fresh", label: HARNESS_RECOVERY_ACTION_LABELS.fresh',
        '{ id: "resume", label: HARNESS_RECOVERY_ACTION_LABELS.resume, disabled: true },\n        { id: "fresh", label: HARNESS_RECOVERY_ACTION_LABELS.fresh',
        1,
    ))
    assert "a cancelled task offers no resume at all" in _checks(broken)


def test_a_prepared_resume_must_be_consumed_by_the_run_that_uses_it(sources):
    broken = sources(html_transform=lambda html: html.replace(
        "if (resumingFromCheckpoint) harnessRecoveryPrepared = null;",
        "",
        1,
    ))
    assert "the preparation is consumed by the request that uses it" in _checks(broken)


def test_the_run_must_carry_the_resume_flag(sources):
    broken = sources(html_transform=lambda html: html.replace(
        "      resume: resumingFromCheckpoint,\n",
        "",
        1,
    ))
    assert "a prepared resume is what makes the next run a continuation" in _checks(broken)


# ── what reaches the page is escaped ────────────────────────────────────────


def test_an_unescaped_value_from_the_record_is_refused(sources):
    broken = sources(html_transform=lambda html: html.replace(
        '<span class="hrec-title">Cancelled</span>',
        '<span class="hrec-title">${ev.reason}</span>',
        1,
    ))
    assert "no value from the record reaches the page unescaped" in _checks(broken)


def test_the_escaping_scanner_reads_what_it_claims_to():
    assert inventory.unescaped_interpolations("${escapeHtml(ev.reason)}") == []
    assert inventory.unescaped_interpolations("${String(ev.step)}") == []
    assert inventory.unescaped_interpolations("${atHtml}") == []
    assert inventory.unescaped_interpolations("${ev.reason}") == ["ev.reason"]
    # A nested literal is judged by its own expressions, not as one blob.
    assert inventory.unescaped_interpolations("${ok ? `${escapeHtml(a)}` : `${b}`}") == ["b"]


# ── the server contracts this story had to preserve ─────────────────────────


def test_every_preserved_route_still_exists(payload):
    names = (
        "api_pause_harness_session",
        "api_cancel_harness_session",
        "api_resume_harness_session",
        "api_contain_harness_session",
        "api_abort_harness",
        "api_get_harness_task_state",
        "api_get_harness_session_state",
    )
    source = APP.read_text(encoding="utf-8")
    for name in names:
        assert f"def {name}(" in source, f"{name} is gone, and the pane posts to it"


def test_a_recovery_route_the_server_does_not_mount_is_refused(sources):
    broken = sources(html_transform=lambda html: html.replace(
        'await harnessRecoveryPost("resume", {})',
        'await fetch("/api/harness/sessions/x/recovery")',
        1,
    ))
    assert "every route the pane calls is mounted by the server" in _checks(broken)


def test_a_pause_that_stops_claiming_to_be_deferred_is_refused(sources):
    broken = sources(app_transform=lambda app: app.replace(
        '"deferred_to_safe_boundary": outcome.deferred_to_safe_boundary,',
        "",
        1,
    ))
    assert "pause still defers to a safe boundary and says which it is" in _checks(broken)


def test_a_cancel_that_stops_reporting_what_it_fenced_is_refused(sources):
    broken = sources(app_transform=lambda app: app.replace(
        '"unresolved_action_ids": list(outcome.unresolved_action_ids),',
        "",
    ))
    assert "cancel still fences, revokes, contains and reports what it left unresolved" in _checks(broken)


def test_the_surface_is_not_drawn_without_a_cause(sources):
    broken = sources(html_transform=lambda html: html.replace(
        'cause: `it stopped between steps:',
        'note: `it stopped between steps:',
        1,
    ))
    assert "no story can be drawn without a title, a cause, a kept line and an action" in _checks(broken)


# ── the fixture is the product's own state machine ──────────────────────────


def test_the_seed_drives_the_real_state_machine():
    source = SEED.read_text(encoding="utf-8")
    for token in (
        "sm.ensure_root_task(",
        "sm.transition_task(",
        "sm.checkpoints.save_checkpoint(",
        "sm.request_pause(",
        "sm.cancel_task(",
        "supervisor.store.persist_barrier_hold(",
        "studio._append_harness_event(",
        "demo_tour.make_environment(",
    ):
        assert token in source, f"the fixture does not use {token}"
    assert "assert not pause_outcome.deferred_to_safe_boundary" in source, (
        "a deferred pause would leave the fixture claiming a state the record does not have"
    )
    assert "recovered_incomplete_transition" in source, (
        "the crash fixture must name the reason the runtime's own startup pass writes"
    )


def test_the_walk_says_what_it_checks_and_refuses_to_fake_it():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    assert "harness_recovery_seed.py" in source
    assert "--screenshots" in source
    assert "no page error occurred during the walk" in source
    for phrase in (
        "a paused task draws its own surface",
        "the surface states the cause in the runtime's reason and the checkpoint it kept",
        "Resume goes through the route and the surface says what the runtime answered",
        "cancel asks once before it fences anything",
        "a cancelled task is drawn as un-resumable",
        "a run that died is drawn from the state the runtime recovered it to",
        "resume is drawn as unavailable, with the reason in text and in the accessible name",
        "a reason that is markup is drawn as text: no element, no handler",
        "every request the surface made is one of the routes that already existed",
        "a run started after a prepared resume asks the server to continue the checkpoint",
    ):
        assert phrase in source, f"the walk dropped its check: {phrase}"


def test_the_media_directory_says_what_produced_it():
    readme = MEDIA / "README.md"
    assert readme.exists(), "the captures have no README saying how they were produced"
    text = readme.read_text(encoding="utf-8")
    assert "harness_recovery_walk.mjs" in text
    assert "harness_recovery_seed.py" in text
    assert "Do not edit these images" in text
    for name in (
        "paused-panel",
        "resume-prepared",
        "cancel-armed",
        "cancelled-panel",
        "crash-recovered",
        "blocked-panel",
        "hostile-reason",
    ):
        assert (MEDIA / f"{name}.png").exists(), f"{name}.png is missing from the captures"
        assert f"`{name}.png`" in text, f"{name}.png is not described in the README"


# ── the presentation component can say "not here" ───────────────────────────


def test_the_presentation_component_states_a_refusal_instead_of_hiding_it():
    html = FRONTEND.read_text(encoding="utf-8")
    body = inventory.function_body(html, "presentInto")
    assert "button.disabled = true" in body
    assert 'aria-disabled", "true"' in body
    assert "presentation-why" in html
    # An action that is available is untouched: the component still draws one primary.
    assert 'index === 0 ? "btn btn-xs btn-primary" : "btn btn-xs"' in body
    assert 'if (action.tone === "danger") button.classList.add("btn-danger");' in body
