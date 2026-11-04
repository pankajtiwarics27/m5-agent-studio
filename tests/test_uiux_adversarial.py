"""Adversarial refusal tests: the server's contract, and that the shell draws it (UIUX-P6-02).

Three things are proved here, in that order:

* **the server really refuses** — a read-only session's worktree apply is a 403 with its own
  words, a write to a session the server has lost is a 404, and a boundary with no token
  answers 503 instead of letting a request through. The rest of the boundary's contract
  (401, cross-origin 403, 413, 429 with `Retry-After`, approval replay) already had owners in
  `tests/test_security_and_quality.py` and `tests/test_remote_protections.py`; the inventory
  points at those nodes rather than restating them, so there is one proof per claim.

* **the shell draws that refusal** — one reader (`refusalReader`), one table of words
  (`REFUSAL_WORDS`), one shape (`refuseWrite` through the shared presentation builder) and one
  guarded write (`guardedWrite`). The rules that keep it honest are asserted against the source:
  the cause is the server's `detail`, never an invented one; a 429's Retry is disabled for the
  server's own `Retry-After`; a success clears a stale refusal; no write can fall through to its
  success path on a refusal.

* **the inventory and the shell agree** — every mutating `fetch` is guarded or states how it
  answers a refusal, every refusal the inventory lists names a prover that exists, and the
  artifacts on disk are the generator's own output.

The last test is the one that matters: it breaks a copy of the shell by restoring the old
"read the body, never the status" apply, and requires the inventory to *fail* on it. A check
nobody has seen fail is a check that may be decoration.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "static" / "index.html"
INVENTORY = ROOT / "scripts" / "uiux_adversarial_inventory.py"
ARTIFACT = ROOT / "product" / "M5_2_0_UIUX_Adversarial.json"
DOC = ROOT / "docs" / "uiux-2-0-adversarial.md"
WALK = ROOT / "scripts" / "adversarial_walk.mjs"
TOKEN = "t" * 40


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _function(name: str, html: str | None = None) -> str:
    """The source of one top-level function, by balanced-brace scan."""
    source = html if html is not None else _html()
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", source)
    assert match, f"{name} is no longer in static/index.html"
    brace = source.index("{", match.end())
    depth = 0
    index = brace
    while index < len(source):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start():index + 1]
        elif char in "\"'`":
            quote = char
            index += 1
            while index < len(source) and source[index] != quote:
                if source[index] == "\\":
                    index += 1
                index += 1
        index += 1
    raise AssertionError(f"{name} is not closed")


def _load_inventory() -> Any:
    spec = importlib.util.spec_from_file_location("uiux_adversarial_inventory", INVENTORY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write the source checkout's harness store or database."""
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "harness-sessions")
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    studio.init_db()


# ── the server's side of three refusals nobody else owned ─────────────────────


def test_a_read_only_session_refuses_its_worktree_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 403 the walk meets for real, in the boundary's own words."""
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    session = studio.create_harness_session_db(
        title="plan only",
        workspace_dir=str(tmp_path / "ws"),
        use_worktree=False,
        policy="plan",
        lead_model="local",
        critic_model="critic",
    )
    with TestClient(studio.app) as client:
        response = client.post(f"/api/harness/sessions/{session['id']}/apply")
    assert response.status_code == 403
    assert response.json()["detail"] == "Cannot apply worktree changes while session policy is read-only 'plan'."


def test_a_write_to_a_session_the_server_no_longer_has_is_a_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the pane meets when a session is deleted underneath it."""
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    session = studio.create_harness_session_db(
        title="doomed",
        workspace_dir=str(tmp_path / "ws"),
        use_worktree=False,
        policy="standard",
        lead_model="local",
        critic_model="critic",
    )
    with TestClient(studio.app) as client:
        assert client.delete(f"/api/harness/sessions/{session['id']}").status_code == 200
        compact = client.post(f"/api/harness/sessions/{session['id']}/compact")
        apply = client.post(f"/api/harness/sessions/{session['id']}/apply")
    for response in (compact, apply):
        assert response.status_code == 404, response.text
        assert "not found" in response.json()["detail"].lower()
    assert session["id"] in compact.json()["detail"]


def test_a_delete_of_a_running_session_is_refused_and_keeps_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UIV2-11: an active running session is protected - the DELETE is a 409 that names the
    state and says what to do first, and the session, its record and its worktree survive."""
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    session = studio.create_harness_session_db(
        title="live-run",
        workspace_dir=str(tmp_path / "ws"),
        use_worktree=False,
        policy="standard",
        lead_model="local",
        critic_model="critic",
    )
    sid = session["id"]
    studio._update_harness_session_state(sid, status="running")
    with TestClient(studio.app) as client:
        refused = client.delete(f"/api/harness/sessions/{sid}")
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "running" in detail and "protected" in detail
        assert "pause or cancel" in detail
        # The refusal removed nothing: the session is still listed and still readable.
        listed = client.get("/api/harness/sessions")
        assert listed.status_code == 200
        assert any(s["id"] == sid and s["status"] == "running" for s in listed.json()["sessions"])
        detail_get = client.get(f"/api/harness/sessions/{sid}")
        assert detail_get.status_code == 200
    # Once the run is over the session can be deleted after all.
    studio._update_harness_session_state(sid, status="completed")
    with TestClient(studio.app) as client:
        assert client.delete(f"/api/harness/sessions/{sid}").status_code == 200
        assert all(s["id"] != sid for s in client.get("/api/harness/sessions").json()["sessions"])


def test_an_unconfigured_token_refuses_every_protected_route_with_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no token there is nothing to authenticate against, and the boundary says so."""
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    with TestClient(studio.app) as client:
        # Startup mints a loopback token when none is configured, so the boundary can only be
        # seen without one by taking it away after the app has started.
        monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
        responses = {
            "GET /api/providers": client.get("/api/providers"),
            "POST /api/harness/sessions": client.post("/api/harness/sessions", json={}),
        }
    for label, response in responses.items():
        assert response.status_code == 503, label
        assert response.json()["detail"] == "Authentication is unavailable; configure a token and restart Studio."


# ── the shell's side of every refusal ────────────────────────────────────────


def test_the_shell_reads_the_status_before_the_body() -> None:
    guarded = _function("guardedWrite")
    assert "if (response.ok) {" in guarded
    # The body is only read after the status has been answered for.
    assert guarded.index("if (response.ok)") < guarded.index("refusalReader(response)")
    # A refusal returns null, so `if (!write) return;` is the only way past it.
    assert "return refuseWrite(refusal, spec);" in guarded
    assert "return null;" in _function("refuseWrite")


def test_the_cause_is_the_servers_detail_and_never_an_invented_one() -> None:
    reader = _function("refusalReader")
    assert 'typeof body.detail === "string"' in reader
    assert 'typeof body.message === "string"' in reader
    # A body that is not JSON, or has no detail, leaves the cause empty - the renderer then
    # names the status and says the server gave no reason, instead of guessing one.
    assert "detail: (detail || \"\").trim()," in reader
    writer = _function("refuseWrite")
    assert "the server answered ${refusal.status} without a reason of its own" in writer
    assert "cause," in writer


def test_a_rate_limited_retry_states_the_servers_own_wait() -> None:
    writer = _function("refuseWrite")
    counter = _function("armRetryCountdown")
    assert 'response.headers.get("retry-after")' in _function("refusalReader")
    assert "Retry in ${seconds}s" in writer
    assert "disabled: true" in writer
    # The control becomes the retry it claims to be only when the window has passed.
    assert "button.disabled = false;" in counter
    assert "button.textContent = \"Retry\";" in counter
    chat_error = _function("presentChatError")
    assert "Retry in ${wait}s" in chat_error
    assert 'armRetryCountdown("chatState", wait * 1000);' in chat_error


def test_a_later_success_clears_the_refusal_it_contradicts() -> None:
    guarded = _function("guardedWrite")
    assert "if (spec.host) clearPresentation(spec.host);" in guarded
    assert guarded.index("if (spec.host) clearPresentation(spec.host);") < guarded.index("return response;")


def test_no_write_can_report_success_on_a_refusal() -> None:
    """The four handlers that used to draw their success path over a refusal."""
    apply_body = _function("applyHarnessWorktree")
    assert "guardedWrite({" in apply_body
    assert apply_body.index("if (!write) return;") < apply_body.index("showToast(")
    # The refusal is answered inside the dialog the button is in, not on the pane behind it.
    assert 'host: "harnessApplyRefusal"' in apply_body

    compact = _function("compactActiveHarness")
    assert "guardedWrite({" in compact
    # The first toast in the handler is its "pick a session" refusal; the guard has to come
    # before the *success* one, which is the one that used to say "Compaction checked".
    assert compact.index("if (!write) return;") < compact.index('showToast(d.message')

    delete = _function("deleteHarnessSession")
    assert "guardedWrite({" in delete
    assert delete.index("if (!write) return;") < delete.index("startFreshHarnessDraft()")

    spawn = _function("confirmSpawnSession")
    assert spawn.index("if (!write) return;") < spawn.index("closeSpawnModal()")


def test_a_refused_write_gives_the_readers_text_back() -> None:
    """The Council composer is cleared only after the server has taken the message."""
    message = _function("postHumanHallMessage")
    assert message.count("if (!write) { inp.value = txt; return; }") == 2
    assert message.index("if (!write) { inp.value = txt; return; }") < message.index("inp.value = \"\";")


def test_a_refused_stream_shows_the_reason_not_the_status_code() -> None:
    sse = _function("consumeSSE")
    assert "const refusal = await refusalReader(resp);" in sse
    assert "const error = new Error(refusal.detail || `HTTP ${refusal.status}`);" in sse
    assert "error.retryAfterMs = refusal.retryAfterMs;" in sse
    # The handlers keep the refusal, so the pane can state the wait as well as the cause.
    assert "handlers.error({ message: e.message, status: e.status || 0, retryAfterMs: e.retryAfterMs || 0 });" in sse


# ── the inventory, its provers and its artifact ──────────────────────────────


def test_every_refusal_names_a_prover_that_exists() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["refusals"], "the inventory carries no refusals, which cannot be true"
    walk = WALK.read_text(encoding="utf-8").replace('\\"', '"')
    for row in payload["refusals"]:
        proof = row["proof"]
        if proof["kind"] == "walk":
            assert proof["check"] in walk, f"{row['status']} names a walk check that is gone"
        else:
            path, _, name = proof["node"].partition("::")
            assert (ROOT / path).is_file(), f"{row['status']} names {path}, which does not exist"
            assert f"def {name}(" in (ROOT / path).read_text(encoding="utf-8"), f"{row['status']} names a test that is gone"
        assert row["contract"].strip(), f"{row['status']} says nothing about how it is drawn"
        assert row["kept"].strip(), f"{row['status']} never says what was kept"


def test_every_status_the_boundary_can_emit_has_words() -> None:
    html = _html()
    table = dict(re.findall(r"(\d{3}):\s*\{\s*title:\s*\"([^\"]+)\"\s*\}", html))
    for status in ("400", "401", "403", "404", "409", "413", "429", "503", "507"):
        assert status in table, f"REFUSAL_WORDS has no words for {status}"
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert {str(row["status"]) for row in payload["refusals"]} == set(table), (
        "the inventory and the shell's table disagree about which refusals exist"
    )


def test_the_write_inventory_has_no_silent_write() -> None:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert payload["findings"] == []
    kinds = {row["kind"] for row in payload["writes"]}
    assert kinds <= {"guarded", "reports"}, kinds
    assert payload["totals"]["guarded_writes"] >= 20
    for row in payload["writes"]:
        if row["kind"] == "reports":
            assert row["note"], f"{row['function']} reports a refusal with no line saying how"


def test_the_committed_artifacts_are_the_generators_output() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, local script only
        [sys.executable, str(INVENTORY), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert DOC.is_file() and "Generated by" in DOC.read_text(encoding="utf-8")


def test_the_check_fails_on_the_shell_this_story_replaced(tmp_path: Path) -> None:
    """Restore the old apply - read the body, never the status - and the inventory must fail."""
    module = _load_inventory()
    html = _html()
    old_apply = """  async function applyHarnessWorktree() {
    if (!activeHarnessId) return;
    const r = await fetch(`/api/harness/sessions/${encodeURIComponent(activeHarnessId)}/apply`, { method: "POST" });
    const d = await r.json();
    showToast(d.message || "Applied!", "ok");
    closeHarnessDiffModal();
    selectHarnessSession(activeHarnessId);
  }"""
    start = _function("applyHarnessWorktree")
    broken = html.replace(start, old_apply, 1)
    assert broken != html

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "FRONTEND", _write(tmp_path / "index.html", broken))
        payload = module.build_inventory()
    finally:
        monkeypatch.undo()
    checks = {finding["check"] for finding in payload["findings"]}
    assert "silent-write" in checks, payload["findings"]
    silent = [finding for finding in payload["findings"] if finding["check"] == "silent-write"]
    assert any("applyHarnessWorktree" in finding["detail"] for finding in silent), silent


def test_a_refusal_drawn_into_a_host_that_does_not_exist_is_a_finding(tmp_path: Path) -> None:
    module = _load_inventory()
    html = _html().replace('id="harnessRefusalState"', 'id="harnessRefusalStateGone"', 1)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "FRONTEND", _write(tmp_path / "index.html", html))
        payload = module.build_inventory()
    finally:
        monkeypatch.undo()
    assert any(finding["check"] == "missing-host" for finding in payload["findings"]), payload["findings"]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path
