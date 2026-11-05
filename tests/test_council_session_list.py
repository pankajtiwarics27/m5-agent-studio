"""Council session list, participant roles and the compact pinned summary (UIV2-14).

The Council sidebar listed sessions as title + status + "N voices • M msgs" with no
participant roles, and the pinned conclusion card showed its full markdown at 24vh,
so a long conclusion crowded the pane (the transcript kept >=640px at desktop, but the
banner itself was tall). UIV2-14 makes the list say who sat in the round and makes the
pinned conclusion a compact summary with the full text one Expand away.

Gates covered here:

* the session list API carries each session's participant ``roles`` (read-only enrichment
  of the existing GET; state-changing routes and auth are untouched);
* each sidebar row renders its role chips, escaped, in a container that wraps;
* the pinned conclusion body defaults to a compact height (96px) and stays independently
  scrollable and resizable (clamp floor lowered to match), with the full conclusion in the
  true expand modal;
* the mutation tests break each rule in a copy of the source so a rule that stopped looking
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


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "hall-list.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    return TestClient(studio.app)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ── API: the list carries participant roles ──────────────────────────────────


def test_session_list_carries_participant_roles(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/hall/sessions").status_code == 401
        r = client.post("/api/hall/sessions", headers=_headers(), json={"request": "matter", "title": "Roles matter"})
        sid = r.json()["id"]
        studio.append_hall_message(sid, "Staff Architect (m)", "architect", "thesis text", model="m")
        studio.append_hall_message(sid, "Adversarial Critic (m)", "critic", "objection text", model="m")
        r = client.get("/api/hall/sessions", headers=_headers())
        assert r.status_code == 200
        (row,) = [s for s in r.json()["sessions"] if s["id"] == sid]
        assert sorted(row["roles"]) == ["architect", "critic", "human"], row
        assert row["participant_count"] >= 1
        # the single-session read is unchanged in shape (roles are list-only metadata)
        one = client.get(f"/api/hall/sessions/{sid}", headers=_headers()).json()
        assert one["id"] == sid and len(one["messages"]) == 3


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


def test_the_inventory_is_clean_and_covers_uiv2_14():
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
        "the session list carries participant roles from the server",
        "each session row renders its participant role chips",
        "role chips wrap instead of overflowing the row",
        "the pinned conclusion shows a compact summary by default, full text one Expand away",
        "the verdict block is height-capped and scrolls as a sibling of the transcript, not nested in it",
    ):
        assert name in names, f"missing check: {name}"


def test_a_list_without_roles_is_caught(sources):
    payload = sources(app_transform=lambda t: t.replace("GROUP_CONCAT(DISTINCT m.role_key)", "NULL", 1))
    assert "the session list carries participant roles from the server" in _checks(payload)


def test_a_row_without_role_chips_is_caught(sources):
    payload = sources(html_transform=lambda t: t.replace('class="chronicle-roles"', 'class="chronicle-roles-x"', 1))
    assert "each session row renders its participant role chips" in _checks(payload)


def test_unwrapped_chips_are_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(".chronicle-roles { display: flex; flex-wrap: wrap;", ".chronicle-roles { display: flex;", 1)
    )
    assert "role chips wrap instead of overflowing the row" in _checks(payload)


def test_a_tall_default_conclusion_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace(".conclusion-body { max-height: 96px;", ".conclusion-body { max-height: 24vh;", 1)
    )
    assert "the pinned conclusion shows a compact summary by default, full text one Expand away" in _checks(payload)


def test_an_uncapped_verdict_is_caught(sources):
    payload = sources(
        html_transform=lambda t: t.replace("max-height: 24vh; overflow-y: auto; overscroll-behavior: contain; }", "}", 1)
    )
    assert "the verdict block is height-capped and scrolls as a sibling of the transcript, not nested in it" in _checks(payload)
