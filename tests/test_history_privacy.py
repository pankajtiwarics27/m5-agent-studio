"""AS-030: History privacy retention, export, and secret redaction.

Covers the acceptance outcome: export/delete/retention and secret redaction
tested. The redaction scrubber is documented best-effort defense-in-depth;
these tests pin the covered credential shapes, the export boundary
(credential/config and audit tables never exported), retention deletion
semantics, and the authenticated API behavior.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
from history_privacy import REDACTED, HistoryPrivacyError, _prune_harness_files, apply_retention, export_history, redact_json, redact_text
from scripts.db_maintenance import main as maintenance_main


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "history-privacy.db"
    keyring_values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(studio.keyring, "set_password", lambda s, i, v: keyring_values.__setitem__((s, i), v))
    monkeypatch.setattr(studio.keyring, "get_password", lambda s, i: keyring_values.get((s, i)))
    monkeypatch.setattr(studio.keyring, "delete_password", lambda s, i: keyring_values.pop((s, i), None))
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "STUDIO_HISTORY_RETENTION_DAYS", 0)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "h" * 40)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", set())
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    studio.init_db()
    return tmp_path


# ---------------------------------------------------------------------------
# Redaction (best-effort by design; every claim here is pattern-pinned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("secret", "label"),
    [
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456", "anthropic key"),
        ("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", "openai-style key"),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", "github token"),
        ("xoxb-123456789-abcdef", "slack token"),
        ("AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ123456789", "google key"),
        ("AKIAABCDEFGHIJKLMNOP", "aws access key id"),
    ],
)
def test_common_credential_shapes_are_redacted(secret: str, label: str) -> None:
    text, count = redact_text(f"my credential is {secret} please keep the rest")
    assert REDACTED in text
    assert secret not in text
    assert count == 1
    assert "please keep the rest" in text


def test_bearer_and_key_value_assignments_are_redacted() -> None:
    text, count = redact_text("Authorization: Bearer abcdefghijklmnop\nAPI_TOKEN=abcdefghijklmnop\napi_key: \"abcdefgh\"\n")
    assert "abcdefghijklmnop" not in text
    assert count >= 3
    assert REDACTED in text


def test_ordinary_prose_and_short_values_are_not_redacted() -> None:
    text, count = redact_text(
        "taskmaster ran sk fewer than tokens; TOKEN=short; token=12345678 is borderline; "
        "the tokenizer splits words and skunks are animals"
    )
    assert text.startswith("taskmaster ran")
    assert "tokenizer splits words" in text  # 'token' inside a word is untouched
    assert count == 1  # only `token=12345678` matches the key=value rule


def test_redact_json_walks_nested_structures_and_counts() -> None:
    payload = {
        "messages": [{"role": "user", "content": "key sk-abcdefghijklmnop1234"}],
        "headers": {"Authorization": "Bearer abcdefghijklmnop"},
        "note": "clean",
        "count": 5,
    }
    out, total = redact_json(payload)
    dumped = json.dumps(out)
    assert "sk-abcdefghijklmnop1234" not in dumped
    assert "abcdefghijklmnop" not in dumped
    assert out["note"] == "clean" and out["count"] == 5
    assert total == 2


# ---------------------------------------------------------------------------
# Export boundary
# ---------------------------------------------------------------------------


def _seed_history(conn: sqlite3.Connection, *, created_at: float) -> None:
    secret = "sk-abc123secretkeyvalue"
    conn.execute(
        "INSERT INTO runs (created_at, mode, title, payload_json) VALUES (?, ?, ?, ?)",
        (created_at, "chat", "leaky run", json.dumps({"answer": f"token {secret}"})),
    )
    conn.execute(
        "INSERT INTO hall_sessions (id, title, request_text, created_by, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
        ("hall-old", "old hall", f"request with {secret}", "tester", created_at, created_at),
    )
    conn.execute(
        "INSERT INTO hall_messages (session_id, author, role_key, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("hall-old", "tester", "user", f"message with {secret}", created_at),
    )
    conn.execute(
        "INSERT INTO opencontext_docs (id, folder, title, description, content, pinned, updated_at) "
        "VALUES ('doc-1', 'architecture', 'arch', '', 'vault note', 0, ?)",
        (created_at,),
    )
    conn.execute(
        "INSERT INTO ctx_vault_chunks (session_id, doc_id, source_tool, chunk_idx, content, created_at) "
        "VALUES ('sess-1', 'ctx-1', 'run_bash', 0, ?, ?)",
        (f"output containing {secret}", created_at),
    )
    conn.execute(
        "INSERT INTO ctx_vault_fts (session_id, doc_id, source_tool, content) "
        "VALUES ('sess-1', 'ctx-1', 'run_bash', ?)",
        (f"output containing {secret}",),
    )
    conn.execute(
        "INSERT INTO ctx_session_metrics (session_id, raw_bytes, sent_bytes, compactions, updated_at) "
        "VALUES ('sess-1', 10, 5, 1, ?)",
        (created_at,),
    )
    conn.commit()


def test_export_covers_history_tables_and_applies_redaction(isolated_app: Path) -> None:
    db = isolated_app / "history-privacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        _seed_history(conn, created_at=time.time() - 3600)
        bundle = export_history(conn)
    assert bundle["format"] == "m5-agent-studio history export"
    assert bundle["schema_version"] == 2
    assert bundle["redaction"]["enabled"] is True
    assert bundle["redaction"]["replacements"] >= 4  # one secret per seeded text field
    tables = bundle["tables"]
    for table in ("runs", "hall_sessions", "hall_messages", "ctx_vault_chunks", "ctx_session_metrics", "opencontext_docs"):
        assert table in tables and tables[table], table
    raw = json.dumps(bundle)
    assert "sk-abc123secretkeyvalue" not in raw
    assert REDACTED in raw
    # Timestamps are ISO-8601 UTC strings, not raw epoch floats.
    assert isinstance(tables["runs"][0]["created_at"], str) and tables["runs"][0]["created_at"].endswith("+00:00")
    # Credential/configuration and audit tables are excluded by construction.
    for excluded in ("cloud_providers", "mcp_servers", "model_combos", "tool_audit_events", "schema_migrations"):
        assert excluded not in tables
        assert excluded not in json.dumps(bundle["tables"])
    assert "tool_audit_events" in bundle["excluded_tables"]["security_and_integrity_trails"]


def test_export_can_be_requested_without_redaction(isolated_app: Path) -> None:
    db = isolated_app / "history-privacy.db"
    with sqlite3.connect(db) as conn:
        _seed_history(conn, created_at=time.time() - 60)
        bundle = export_history(conn, redact=False)
    assert bundle["redaction"]["enabled"] is False
    assert bundle["redaction"]["replacements"] == 0
    assert "sk-abc123secretkeyvalue" in json.dumps(bundle["tables"])


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_prunes_old_rows_and_keeps_recent(isolated_app: Path) -> None:
    db = isolated_app / "history-privacy.db"
    now = time.time()
    harness_store = isolated_app / "harness-sessions"
    harness_store.mkdir()
    old_session = "hns-1234abcd"
    recent_session = "hns-5678abcd"
    active_session = "hns-0badcafe"
    for session_id in (old_session, recent_session, active_session):
        (harness_store / f"{session_id}.jsonl").write_text("raw transcript", encoding="utf-8")
        (harness_store / f"{session_id}.messages.json").write_text(json.dumps(["raw prompt"]), encoding="utf-8")
    old_mtime = now - 90 * 86400
    for session_id in (old_session, active_session):
        os.utime(harness_store / f"{session_id}.jsonl", (old_mtime, old_mtime))
        os.utime(harness_store / f"{session_id}.messages.json", (old_mtime, old_mtime))
    with sqlite3.connect(db) as conn:
        _seed_history(conn, created_at=now - 90 * 86400)
        conn.execute(
            "INSERT INTO runs (created_at, mode, title, payload_json) VALUES (?, 'chat', 'recent', '{}')",
            (now - 86400,),
        )
        session_columns = "id, title, workspace_dir, worktree_dir, branch_name, use_worktree, policy, lead_model, critic_model, status, step_count, created_at, updated_at"
        conn.execute(
            f"INSERT INTO harness_sessions ({session_columns}) VALUES (?, 'old', '', '', '', 0, 'standard', 'local', 'critic', 'idle', 0, ?, ?)",
            (old_session, now - 90 * 86400, now - 90 * 86400),
        )
        conn.execute(
            f"INSERT INTO harness_sessions ({session_columns}) VALUES (?, 'recent', '', '', '', 0, 'standard', 'local', 'critic', 'idle', 0, ?, ?)",
            (recent_session, now - 86400, now - 86400),
        )
        conn.execute(
            f"INSERT INTO harness_sessions ({session_columns}) VALUES (?, 'active', '', '', '', 0, 'standard', 'local', 'critic', 'running', 0, ?, ?)",
            (active_session, now - 90 * 86400, now - 90 * 86400),
        )
        deleted = apply_retention(conn, max_age_days=30, now=now, harness_store_dir=harness_store)
        remaining_runs = conn.execute("SELECT title FROM runs ORDER BY id").fetchall()
        remaining_hall = conn.execute("SELECT COUNT(*) FROM hall_sessions").fetchone()[0]
        remaining_msgs = conn.execute("SELECT COUNT(*) FROM hall_messages").fetchone()[0]
        remaining_chunks = conn.execute("SELECT COUNT(*) FROM ctx_vault_chunks").fetchone()[0]
        remaining_metrics = conn.execute("SELECT COUNT(*) FROM ctx_session_metrics").fetchone()[0]
        remaining_harness = {row[0] for row in conn.execute("SELECT id FROM harness_sessions")}
        remaining_fts = conn.execute("SELECT COUNT(*) FROM ctx_vault_fts WHERE ctx_vault_fts MATCH 'secretkeyvalue'").fetchone()[0]
        remaining_fts_all = conn.execute("SELECT COUNT(*) FROM ctx_vault_fts").fetchone()[0]
        survivor_ids = {row[0] for row in conn.execute("SELECT id FROM opencontext_docs").fetchall()}
    assert deleted["runs"] == 1
    assert deleted["hall_sessions"] == 1 and deleted["hall_messages"] == 1
    assert deleted["ctx_vault_chunks"] == 1 and deleted["ctx_session_metrics"] == 1
    assert deleted["harness_sessions"] == 1 and deleted["harness_session_files"] == 2
    assert deleted["ctx_vault_fts"] == 1
    assert [row[0] for row in remaining_runs] == ["recent"]
    assert remaining_hall == 0 and remaining_msgs == 0
    assert remaining_chunks == 0 and remaining_metrics == 0
    assert remaining_fts == 0 and remaining_fts_all == 0
    assert "No FTS5 chunks matched" in studio.search_ctx_vault("sess-1", "secretkeyvalue")
    assert remaining_harness == {recent_session, active_session}
    assert not (harness_store / f"{old_session}.jsonl").exists()
    assert not (harness_store / f"{old_session}.messages.json").exists()
    assert (harness_store / f"{recent_session}.jsonl").exists()
    assert (harness_store / f"{recent_session}.messages.json").exists()
    assert (harness_store / f"{active_session}.jsonl").exists()
    assert (harness_store / f"{active_session}.messages.json").exists()
    # OpenContext is a persistent vault: retention never deletes knowledge docs.
    assert "doc-1" in survivor_ids


def test_harness_retention_reports_unlink_failures_and_preserves_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "sessions"
    store.mkdir()
    session_id = "hns-cafe1234"
    transcript = store / f"{session_id}.jsonl"
    transcript.write_text("sensitive transcript", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == transcript:
            raise PermissionError("simulated permission error")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    removed, failures = _prune_harness_files(
        store, [session_id], cutoff=time.time() + 1, protected_ids=set()
    )
    assert removed == 0 and failures == 1
    assert transcript.read_text(encoding="utf-8") == "sensitive transcript"


def test_retention_validates_arguments(isolated_app: Path) -> None:
    db = isolated_app / "history-privacy.db"
    with sqlite3.connect(db) as conn:
        with pytest.raises(HistoryPrivacyError, match="integer >= 1"):
            apply_retention(conn, max_age_days=0, now=time.time())
        with pytest.raises(HistoryPrivacyError, match="integer >= 1"):
            apply_retention(conn, max_age_days=-5, now=time.time())
        with pytest.raises(HistoryPrivacyError, match="positive epoch"):
            apply_retention(conn, max_age_days=30, now=0)


def test_init_db_enforces_configured_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "startup-retention.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "STUDIO_HISTORY_RETENTION_DAYS", 30)
    old = time.time() - 120 * 86400
    with sqlite3.connect(db) as conn:
        studio.init_db()
        conn.execute(
            "INSERT INTO runs (created_at, mode, title, payload_json) VALUES (?, 'chat', 'ancient', '{}')",
            (old,),
        )
        conn.commit()
    studio.init_db()  # startup pass applies retention
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert "History retention pruned" in capsys.readouterr().err


def test_init_db_leaves_data_intact_when_retention_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "startup-no-retention.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    monkeypatch.setattr(studio, "STUDIO_HISTORY_RETENTION_DAYS", 0)
    with sqlite3.connect(db) as conn:
        studio.init_db()
        conn.execute(
            "INSERT INTO runs (created_at, mode, title, payload_json) VALUES (?, 'chat', 'kept', '{}')",
            (time.time() - 400 * 86400,),
        )
        conn.commit()
    studio.init_db()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Authenticated API surface
# ---------------------------------------------------------------------------


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + "h" * 40}


def test_history_export_requires_authentication(isolated_app: Path) -> None:
    with TestClient(studio.app) as client:
        assert client.get("/api/history/export").status_code == 401
        response = client.get("/api/history/export", headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers.get("content-disposition", "")
    bundle = response.json()
    assert bundle["format"] == "m5-agent-studio history export"
    assert "runs" in bundle["tables"]


def test_protected_api_fails_closed_when_lifespan_did_not_load_token(
    isolated_app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    # Do not enter TestClient as a context manager: this deliberately skips ASGI lifespan.
    response = TestClient(studio.app).get("/api/providers")
    assert response.status_code == 503
    assert "Authentication is unavailable" in response.json()["detail"]


def test_history_export_redacts_secrets_in_payload(isolated_app: Path) -> None:
    secret = "sk-abc123secretkeyvalue"
    with sqlite3.connect(isolated_app / "history-privacy.db") as conn:
        _seed_history(conn, created_at=time.time() - 60)
    with TestClient(studio.app) as client:
        response = client.get("/api/history/export", headers=_auth())
    assert response.status_code == 200
    assert secret not in response.text
    assert REDACTED in response.text


def test_history_retention_run_endpoint(isolated_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    db = isolated_app / "history-privacy.db"
    store = isolated_app / "harness-store"
    store.mkdir()
    session_id = "hns-a1b2c3d4"
    for suffix in (".jsonl", ".messages.json"):
        path = store / f"{session_id}{suffix}"
        path.write_text("old transcript", encoding="utf-8")
        os.utime(path, (now - 90 * 86400, now - 90 * 86400))
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store)
    with sqlite3.connect(db) as conn:
        _seed_history(conn, created_at=now - 90 * 86400)
        conn.execute(
            "INSERT INTO harness_sessions "
            "(id, title, workspace_dir, worktree_dir, branch_name, use_worktree, policy, lead_model, critic_model, status, step_count, created_at, updated_at) "
            "VALUES (?, 'old', '', '', '', 0, 'standard', 'local', 'critic', 'idle', 0, ?, ?)",
            (session_id, now - 90 * 86400, now - 90 * 86400),
        )
    with TestClient(studio.app) as client:
        headers = {**_auth(), "Origin": "http://testserver"}
        disabled = client.post("/api/history/retention/run", headers=headers)
        assert disabled.status_code == 400
        assert disabled.json()["status"] == "disabled"
        monkeypatch.setattr(studio, "STUDIO_HISTORY_RETENTION_DAYS", 30)
        ok = client.post("/api/history/retention/run", headers=headers)
    assert ok.status_code == 200
    payload: dict[str, Any] = ok.json()
    assert payload["status"] == "ok" and payload["max_age_days"] == 30
    assert payload["deleted_total"] >= 5
    assert payload["deleted"]["harness_sessions"] == 1
    assert payload["deleted"]["harness_session_files"] == 2
    assert not (store / f"{session_id}.jsonl").exists()
    assert not (store / f"{session_id}.messages.json").exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM harness_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM opencontext_docs WHERE id='doc-1'").fetchone()[0] == 1


def test_history_retention_run_rejects_cross_origin(isolated_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_HISTORY_RETENTION_DAYS", 30)
    with TestClient(studio.app) as client:
        response = client.post(
            "/api/history/retention/run",
            headers={**_auth(), "Origin": "https://evil.example"},
        )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Operator CLI (scripts/db_maintenance.py)
# ---------------------------------------------------------------------------


def test_maintenance_cli_retention_and_export(isolated_app: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = isolated_app / "history-privacy.db"
    with sqlite3.connect(db) as conn:
        _seed_history(conn, created_at=time.time() - 90 * 86400)
    assert maintenance_main(["retention", "--database", str(db), "--days", "30"]) == 0
    assert "deleted" in capsys.readouterr().out
    out_file = tmp_path / "exports" / "history.json"  # tmp_path is the fixture workspace
    assert maintenance_main(["export", "--database", str(db), "--output", str(out_file)]) == 0
    assert out_file.exists()
    bundle = json.loads(out_file.read_text(encoding="utf-8"))
    assert bundle["format"] == "m5-agent-studio history export"
    assert "sk-abc123secretkeyvalue" not in out_file.read_text(encoding="utf-8")
    import os
    import stat

    assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600
    assert maintenance_main(["retention", "--database", str(db), "--days", "0"]) == 2
