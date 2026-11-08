"""Council state consistency and streaming recovery (UIV2-15).

Three drifts met in the Council pane and none of them was a drawing problem.

* A catalogue poll rebuilt every model picker whenever the catalogue's ids changed, and any
  picker whose id was not in the new read fell through to the catalogue default. Only the four
  seat pickers kept their id, and a role change in the single-seat card overwrote the picked
  model with the substring search's favourite for the new role. The next dispatch then spent a
  real inference on a model nobody picked.
* A stream could end with no ``summon_done`` and no ``error`` frame (a sleeping laptop, a proxy
  that reaps an idle socket, a reload). ``consumeSSE()`` reports that as ``handlers.closed()``,
  and the summon passed none: the "Connecting..." card stayed on screen for the life of the
  page, and nothing said the reply had not been saved.
* A session's own state was ignored: a closed session accepted a summon and answered over its
  pinned conclusion, the summon route substituted ``Qwen3.8-27B-oQ4e-mtp`` for an unnamed model
  and Seat 1 for an unnamed role, and ``selectHallSession()`` returned on ``!r.ok`` without a
  word, leaving the previous transcript on screen under the new title.

Gates covered here:

* the three refusals (400 unnamed model, 400 unknown seat, 409 closed session) and the reopen a
  Human message performs, which keeps the pinned conclusion;
* the two turn facts a session read now carries, and that a dropped stream is recorded as a turn
  whose reply was never saved while a completed turn clears that record;
* the pane rules the inventory reads (a pick survives a catalogue refresh, a dropped stream is
  reconciled, a closed session cannot be dispatched into);
* mutation tests that break each rule in a copy of the source, so a rule that stopped looking
  fails here rather than in production.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_council_state_inventory.py"

import app as studio  # noqa: E402
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import uiux_council_state_inventory as inventory  # noqa: E402

TOKEN = "test-token-" + "s" * 40
LOCAL_MODEL = "Qwen3.8-27B-oQ4e-mtp"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "hall-state.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    studio._HALL_RUNNING_SUMMONS.clear()
    studio._HALL_ACTIVE_TURNS.clear()
    studio._HALL_ABANDONED_TURNS.clear()
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    return TestClient(studio.app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _session(client: TestClient) -> str:
    response = client.post("/api/hall/sessions", headers=_headers(), json={"request": "matter", "title": "State"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _summon(client: TestClient, sid: str, **payload) -> object:
    body = {"model": LOCAL_MODEL, "role_key": "architect"} | payload
    return client.post(f"/api/hall/sessions/{sid}/summon", headers=_headers(), json=body)


def _drop_stream(client: TestClient, sid: str, **payload) -> None:
    """Open a summon stream, read two frames, and close the connection under it."""
    body = {"model": LOCAL_MODEL, "role_key": "architect"} | payload
    with client.stream("POST", f"/api/hall/sessions/{sid}/summon", headers=_headers(), json=body) as stream:
        assert stream.status_code == 200, stream.read()
        seen = 0
        for _line in stream.iter_lines():
            seen += 1
            if seen >= 4:
                break
    assert seen >= 4, "the stream produced no frames to drop"


# ── the three refusals ───────────────────────────────────────────────────────


def test_a_summon_that_names_no_model_is_refused(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        response = client.post(f"/api/hall/sessions/{sid}/summon", headers=_headers(), json={"role_key": "architect"})
        assert response.status_code == 400
        assert "will not pick one" in response.json()["detail"]
        # Nothing was answered for a model nobody named.
        assert len(client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()["messages"]) == 1


def test_a_summon_that_names_no_known_seat_is_refused(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        response = client.post(
            f"/api/hall/sessions/{sid}/summon", headers=_headers(), json={"model": LOCAL_MODEL, "role_key": "nope"}
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "not a Council seat" in detail and "architect" in detail


def test_a_closed_session_refuses_a_summon_and_names_the_way_back(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        studio.append_hall_message(sid, "Adversarial Critic (m)", "critic", "objection", model="m")
        client.post(f"/api/hall/sessions/{sid}/close", headers=_headers(), json={"conclusion": "the verdict", "author": "Lead Arbiter"})
        response = _summon(client, sid, role_key="judge")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "is closed" in detail and "Send a Human message to reopen the session" in detail
        # The pinned verdict is untouched by the refusal.
        one = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        assert one["conclusion_text"] == "the verdict"
        assert len(one["messages"]) == 2


def test_a_human_message_reopens_a_closed_session_and_keeps_the_conclusion(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        client.post(f"/api/hall/sessions/{sid}/close", headers=_headers(), json={"conclusion": "the verdict", "author": "Lead Arbiter"})
        assert client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()["status"] == "closed"
        client.post(
            f"/api/hall/sessions/{sid}/message",
            headers=_headers(),
            json={"author": "Human Operative", "role_key": "human", "content": "one more round"},
        )
        reopened = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        assert reopened["status"] == "active"
        assert reopened["conclusion_text"] == "the verdict", "the reopen must not clear the pinned verdict"
        # The session is dispatchable again, which is the point of the reopen.
        assert _summon(client, sid).status_code == 200


# ── the two turn facts ──────────────────────────────────────────────────────


def test_successful_terminal_frames_do_not_enter_the_dropped_stream_path():
    """A saved Council reply ends with summon_done; its normal EOF is not a lost turn."""
    source = FRONTEND.read_text(encoding="utf-8")
    start = source.index("const SSE_TERMINAL_EVENTS")
    end = source.index("async function consumeSSE", start)
    terminal = source[start:end]
    for event in ("done", "council_done", "debate_done", "summon_done", "error"):
        assert f'"{event}"' in terminal
    consume_end = source.index("// ================= AUTO 4-STAGE", end)
    consume = source[end:consume_end]
    assert "if (SSE_TERMINAL_EVENTS.has(ev)) receivedTerminalEvent = true;" in consume
    assert consume.index("receivedTerminalEvent = true") < consume.index("await handlers[ev]")


def test_a_session_read_carries_the_turn_facts_and_they_are_null_when_idle(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        one = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        assert one["active_turn"] is None and one["abandoned_turn"] is None
        assert set(studio.hall_turn_state(sid)) == {"active_turn", "abandoned_turn"}


def test_a_dropped_stream_is_recorded_as_a_turn_whose_reply_was_never_saved(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        _drop_stream(client, sid)
        deadline = time.monotonic() + 10
        one = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        while one["abandoned_turn"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
            one = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        lost = one["abandoned_turn"]
        assert lost is not None, "a stream that ended unsaved left no record for the next read"
        assert lost["role_key"] == "architect" and lost["model"] == LOCAL_MODEL
        assert lost["reason"], "the record must say why the reply is not there"
        assert one["active_turn"] is None, "the turn is not running after its stream ended"
        assert len(one["messages"]) == 1, "nothing was written for the seat whose stream died"


def test_a_completed_turn_clears_the_loss_it_answers(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        _drop_stream(client, sid)
        assert client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()["abandoned_turn"] is not None
        assert _summon(client, sid).status_code == 200
        done = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        assert done["abandoned_turn"] is None, "a stale 'not saved' label must not outlive the retry"
        assert len(done["messages"]) == 2


def test_deleting_a_session_drops_its_turn_records(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        sid = _session(client)
        _drop_stream(client, sid)
        assert studio._HALL_ABANDONED_TURNS.get(sid) is not None
        assert client.delete(f"/api/hall/sessions/{sid}", headers=_headers()).status_code == 200
        assert studio._HALL_ABANDONED_TURNS.get(sid) is None
        assert studio._HALL_ACTIVE_TURNS.get(sid) is None


# ── the inventory and its mutation tests ────────────────────────────────────


@pytest.fixture()
def sources(tmp_path, monkeypatch):
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


def _checks(broken: dict) -> set[str]:
    return {finding["check"] for finding in broken["findings"]}


def test_the_inventory_is_clean_and_covers_uiv2_15():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = inventory.build_inventory()
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] >= 30
    names = [c["check"] for c in payload["checks"]]
    for name in (
        "every picker keeps the id it holds, not only the four Council seats",
        "a catalogue poll does not rebuild the Council pickers while a dispatch holds them",
        "choosing a role does not silently replace the picked model",
        "a summon that names no model is refused instead of defaulted",
        "a summon that names no known seat is refused instead of becoming Seat 1",
        "a summon into a closed session is refused, with the way back named",
        "both facts travel with the session read the pane already makes",
        "the dropped-stream handler reconciles against the server",
        "a lost turn names the seat, the model and the discarded characters",
        "the closed guard runs before readiness, at both dispatch entry points",
    ):
        assert name in names, f"missing check: {name}"


def test_a_picker_that_falls_back_to_the_default_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(
            "      if (held) sel.value = prevVal;\n      else if (prevVal === \"__custom__\") sel.value = \"__custom__\";\n      else if (defVal) sel.value = defVal;",
            "      if (unavailable) sel.value = prevVal;\n      else if (prevVal === \"__custom__\") sel.value = \"__custom__\";\n      else if (defVal) sel.value = defVal;",
            1,
        )
    )
    assert "every picker keeps the id it holds, not only the four Council seats" in _checks(payload)


def test_a_picker_rebuilt_mid_round_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(
            "if (COUNCIL_PICKER_IDS.includes(elId) && (councilRoundInProgress || councilSummonInProgress)) continue;",
            "if (false) continue;",
            1,
        )
    )
    assert "a catalogue poll does not rebuild the Council pickers while a dispatch holds them" in _checks(payload)


def test_a_role_change_that_swaps_the_model_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(
            "if (current && current !== \"__custom__\" && unifiedCatalog.some(item => item.id === current)) return;",
            "if (false) return;",
            1,
        )
    )
    assert "choosing a role does not silently replace the picked model" in _checks(payload)


def test_a_stream_that_ends_silently_is_caught(sources):
    payload = sources(html_transform=lambda t: t.replace("async closed() {", "async closed_x() {", 1))
    assert "the dropped-stream handler reconciles against the server" in _checks(payload)


def test_a_session_read_that_skips_the_turn_facts_is_caught(sources):
    payload = sources(app_transform=lambda t: t.replace("sess.update(hall_turn_state(session_id))", "pass", 1))
    assert "both facts travel with the session read the pane already makes" in _checks(payload)


def test_a_reinstated_model_default_is_caught(sources):
    def reinstate_summon_default(source: str) -> str:
        import ast

        # Target this handler, not the first unrelated model assignment in the
        # application (the coordinated native gateway now precedes Council).
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_hall_summon")
        lines = source.splitlines(keepends=True)
        start, end = node.lineno - 1, node.end_lineno
        handler = "".join(lines[start:end])
        original = '    model = body.get("model")\n'
        assert handler.count(original) == 1
        mutated = handler.replace(original, '    model = body.get("model") or "Qwen3.8-27B-oQ4e-mtp"\n', 1)
        result = "".join(lines[:start]) + mutated + "".join(lines[end:])
        assert inventory.python_function(result, "_coordinated_gateway_chat") == inventory.python_function(source, "_coordinated_gateway_chat")
        return result

    payload = sources(app_transform=reinstate_summon_default)
    assert "a summon that names no model is refused instead of defaulted" in _checks(payload)


def test_a_removed_closed_refusal_is_caught(sources):
    payload = sources(app_transform=lambda t: t.replace('    if sess.get("status") == "closed":', '    if False:', 1))
    assert "a summon into a closed session is refused, with the way back named" in _checks(payload)


def test_silent_session_selection_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(
            "if (!read.ok) {",
            "if (!read.ok) return false;\n    if (false) {",
            1,
        )
    )
    assert "a failed session read is stated instead of skipped" in _checks(payload)


def test_a_closed_session_that_can_still_be_dispatched_into_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(
            "round.disabled = invocationBusy || dispatchBlocked || !allSeatsCanInvoke;",
            "round.disabled = invocationBusy || !allSeatsCanInvoke;",
            1,
        )
    )
    assert "a closed or vanished session cannot be dispatched into, and the reason is on the control" in _checks(payload)


def test_the_doc_names_the_two_turn_facts_and_the_three_refusals():
    doc = (PROJECT_ROOT / "docs" / "uiux-2-0-council-state.md").read_text(encoding="utf-8")
    for needle in ("active_turn", "abandoned_turn", "400", "409", "reopens", "not saved"):
        assert needle in doc, f"{needle} is missing from the inventory doc"
