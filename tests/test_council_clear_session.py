"""Council clear session / clear-all with safety (UIV2-13).

The Council pane had no way to clear a session: the sidebar listed sessions with no
delete control and the composer had no Clear session button, and the server mounted no
delete route at all (`docs/uiv2-v2-reference.md` line 60 names "no clear control
(verified absent)" as the defect; the wireframe's hall composer row carries
"Run full round" + "Clear session").

UIV2-13 adds `DELETE /api/hall/sessions/{id}` and `DELETE /api/hall/sessions` (clear-all)
plus armed two-press UI controls. The gates that matter here:

* every `/api/` route is behind the bearer-token middleware, so the new deletes are
  authorized by construction - and a request without the token must get 401;
* a session with a summon in flight is "running": 409 on single delete, skipped by
  clear-all (and reported in `skipped_running`);
* delete is a true delete (`archived: false`), keyed on one id - deleting one session
  never deletes others - and it touches only the two hall tables, never workspace files;
* the delete is durable: a fresh app instance over the same DB file still sees the
  session gone (restart persistence);
* the UI arms before it fires (two presses) and the armed note says what is gone for
  good, that there is no archive, and that workspace files are never touched.

The mutation tests break each rule in a copy of the source so a rule that stopped looking
fails here rather than in production.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_council_verdict_inventory.py"

import app as studio  # noqa: E402
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import uiux_council_verdict_inventory as inventory  # noqa: E402

TOKEN = "test-token-" + "k" * 40


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, auth: bool = True) -> TestClient:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "hall-clear.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN if auth else "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", not auth)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    studio._HALL_RUNNING_SUMMONS.clear()
    return TestClient(studio.app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _make_sessions(client: TestClient, n: int = 3) -> list[str]:
    ids = []
    for i in range(n):
        r = client.post("/api/hall/sessions", headers=_headers(), json={"request": f"matter {i}", "title": f"Session {i}"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


# ── API integration and error handling ────────────────────────────────────────


def test_delete_requires_the_bearer_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth=True)
    with client:
        ids = _make_sessions(client, 1)
        assert client.delete(f"/api/hall/sessions/{ids[0]}").status_code == 401
        assert client.delete("/api/hall/sessions").status_code == 401
        # the session is untouched by the refused writes
        assert len(client.get("/api/hall/sessions", headers=_headers()).json()["sessions"]) == 1


def test_delete_one_session_keeps_the_others(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth=False)
    with client:
        ids = _make_sessions(client, 3)
        client.post(f"/api/hall/sessions/{ids[0]}/message", json={"content": "a note"})
        r = client.delete(f"/api/hall/sessions/{ids[0]}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "deleted" and body["id"] == ids[0]
        assert body["archived"] is False, "delete must say it is not an archive"
        assert client.get(f"/api/hall/sessions/{ids[0]}").status_code == 404
        remaining = {s["id"] for s in client.get("/api/hall/sessions").json()["sessions"]}
        assert remaining == set(ids[1:]), "deleting one session must not delete others"


def test_delete_unknown_session_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth=False)
    with client:
        assert client.delete("/api/hall/sessions/ch-unknown").status_code == 404


def test_running_session_is_guarded_409_and_clear_all_skips_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, auth=False)
    with client:
        ids = _make_sessions(client, 3)
        monkeypatch.setattr(studio, "_HALL_RUNNING_SUMMONS", {ids[1]})
        r = client.delete(f"/api/hall/sessions/{ids[1]}")
        assert r.status_code == 409, r.text
        assert "running" in r.json()["detail"]
        r = client.delete("/api/hall/sessions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] == 2
        assert body["skipped_running"] == [ids[1]]
        assert body["archived"] is False
        remaining = [s["id"] for s in client.get("/api/hall/sessions").json()["sessions"]]
        assert remaining == [ids[1]], "clear-all must skip the running session"
        monkeypatch.setattr(studio, "_HALL_RUNNING_SUMMONS", set())
        assert client.delete("/api/hall/sessions").json()["deleted"] == 1
        assert client.get("/api/hall/sessions").json()["sessions"] == []


def test_delete_persists_across_a_restart(tmp_path, monkeypatch):
    """A new app instance over the same DB file is the restart; the delete must hold."""
    client = _client(tmp_path, monkeypatch, auth=False)
    with client:
        ids = _make_sessions(client, 2)
        client.delete(f"/api/hall/sessions/{ids[0]}")
        client.delete("/api/hall/sessions")
        assert client.get("/api/hall/sessions").json()["sessions"] == []
    again = _client(tmp_path, monkeypatch, auth=False)
    with again:
        assert again.get("/api/hall/sessions").json()["sessions"] == [], "the delete must survive a restart"
        assert again.get(f"/api/hall/sessions/{ids[0]}").status_code == 404


def test_hall_tables_are_the_only_ones_the_delete_touches(tmp_path, monkeypatch):
    """The delete SQL must name the hall tables only - workspace data lives elsewhere."""
    src = APP.read_text(encoding="utf-8")
    helper = src[src.find("def delete_hall_session_db") :]
    helper = helper[: helper.find("\ndef ")]
    assert "DELETE FROM hall_messages WHERE session_id = ?" in helper
    assert "DELETE FROM hall_sessions WHERE id = ?" in helper
    forbidden = [line for line in helper.splitlines() if "DELETE FROM" in line and "hall_" not in line]
    assert forbidden == [], f"the delete touches non-hall tables: {forbidden}"


# ── inventory + mutation tests over the sources ───────────────────────────────


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


def test_the_inventory_is_clean_and_covers_uiv2_13():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = inventory.build_inventory()
    assert payload["status"] == "clean", payload["findings"]
    names = [c["check"] for c in payload["checks"]]
    for name in (
        "the server mounts an authorized delete for one hall session",
        "a running summon guards the session from deletion",
        "clear-all deletes every session except running ones and never touches workspace files",
        "the sidebar arms a per-session delete before it fires",
        "the composer carries a Clear session control for the active session",
    ):
        assert name in names, f"missing check: {name}"


def test_a_delete_route_without_the_running_guard_is_caught(sources):
    payload = sources(
        app_transform=lambda t: t.replace("_HALL_RUNNING_SUMMONS.add(session_id)", "pass", 1)
    )
    assert "a running summon guards the session from deletion" in _checks(payload)


def test_a_clear_all_that_deletes_running_sessions_is_caught(sources):
    payload = sources(
        app_transform=lambda t: t.replace("clear_hall_sessions_db(exclude=running)", "clear_hall_sessions_db(exclude=set())", 1)
    )
    assert "clear-all deletes every session except running ones and never touches workspace files" in _checks(payload)


def test_a_delete_that_swallows_the_archive_flag_is_caught(sources):
    payload = sources(
        app_transform=lambda t: t.replace('"archived": False', '"archived": True', 2)
    )
    assert "delete says it is a true delete, not an archive" in _checks(payload)


def test_a_one_press_delete_button_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace('onclick="event.stopPropagation(); armHallDelete(', 'onclick="event.stopPropagation(); deleteHallSession(', 1)
    )
    assert "the sidebar arms a per-session delete before it fires" in _checks(payload)


def test_a_scope_note_that_hides_the_workspace_guarantee_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace("Files in your workspace are never touched.", "", 2)
    )
    assert "the armed confirm states the scope: irreversible, no archive, workspace never touched" in _checks(payload)


def test_a_missing_composer_clear_control_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace('id="hallClearSessionBtn"', 'id="hallClearSessionBtnX"', 1)
    )
    assert "the composer carries a Clear session control for the active session" in _checks(payload)
