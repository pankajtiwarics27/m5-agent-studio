"""Contract tests for the extracted OpenContext memory-store boundary.

`context_store` receives its storage handle and database path explicitly, so
these tests run against a temporary database through the real SQLiteStorage
implementation plus a recording fake that proves the injection seam. Route
tests exercise the HTTP surface end to end over an isolated app instance.
Note: `init_db` seeds exactly one pinned default document.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
import context_store
from context_store import sanitize_fts5_query


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection | Any:
    db = tmp_path / "context.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    studio.init_db()
    storage = studio.SQLiteStorage()
    # Deterministic start: remove the seeded default doc.
    context_store.delete_doc(storage, db, context_store.list_docs(storage, db)[0]["id"])
    return db


def seed(store: Any, **kwargs: Any) -> dict[str, Any]:
    return context_store.upsert_doc(studio.SQLiteStorage(), store, **kwargs)


class RecordingStorage:
    """Fake storage double: proves the boundary can be mocked without SQLite."""

    def __init__(self) -> None:
        self.connect_calls: list[Path] = []

    def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
        self.connect_calls.append(database)
        return sqlite3.connect(database, timeout=timeout)


# ---------------------------------------------------------------------------
# CRUD contract
# ---------------------------------------------------------------------------


def test_upsert_and_get_roundtrip_defaults(store: Any) -> None:
    doc = seed(store, title="Decision log", content="We chose SQLite FTS5 for search.")
    assert doc["title"] == "Decision log"
    assert doc["folder"] == "decisions"
    assert doc["pinned"] is False
    assert doc["uri"].startswith("oc://doc/oc-")
    assert doc["description"] == "We chose SQLite FTS5 for search."[:240]
    fetched = context_store.get_doc(studio.SQLiteStorage(), store, doc["uri"])
    assert fetched["id"] == doc["id"]
    with pytest.raises(KeyError, match="not found"):
        context_store.get_doc(studio.SQLiteStorage(), store, "oc-does-not-exist")


def test_upsert_sanitizes_folder_truncates_title_and_accepts_doc_id(store: Any) -> None:
    doc = seed(store, title="x" * 200, content="body", folder="My Folder!/Weekly", doc_id="oc-custom01", pinned=True)
    assert doc["title"] == "x" * 140
    assert doc["folder"] == "my-folder-/weekly"  # `/` is preserved for nested folders
    assert doc["id"] == "oc-custom01"
    assert doc["pinned"] is True
    with pytest.raises(ValueError, match="title"):
        seed(store, title="   ", content="body")


def test_update_reindexes_fts(store: Any) -> None:
    doc = seed(store, title="Runbook", content="deployment uses blue-green releases")
    updated = seed(store, title="Runbook", content="deployment now uses canary analysis", doc_id=doc["id"])
    assert updated["id"] == doc["id"]
    hits = context_store.search_docs(studio.SQLiteStorage(), store, "canary")
    assert [h["id"] for h in hits] == [doc["id"]]
    assert context_store.search_docs(studio.SQLiteStorage(), store, "blue-green") == []


def test_delete_removes_doc_and_index_row(store: Any) -> None:
    doc = seed(store, title="Temp", content="scratch note to delete")
    context_store.delete_doc(studio.SQLiteStorage(), store, doc["id"])
    assert context_store.list_docs(studio.SQLiteStorage(), store) == []
    with studio.SQLiteStorage().connect(store) as conn:
        assert conn.execute("SELECT COUNT(*) FROM opencontext_fts WHERE doc_id = ?", (doc["id"],)).fetchone()[0] == 0


def test_list_orders_pinned_first_then_recency(store: Any) -> None:
    a = seed(store, title="A", content="alpha body text")
    time.sleep(0.01)
    b = seed(store, title="B", content="beta body text")
    context_store.upsert_doc(studio.SQLiteStorage(), store, title="A", content="alpha body text", doc_id=a["id"], pinned=True)
    ids = [d["id"] for d in context_store.list_docs(studio.SQLiteStorage(), store)]
    assert ids == [a["id"], b["id"]]
    folder_hits = context_store.list_docs(studio.SQLiteStorage(), store, folder="notes")
    assert folder_hits == []


# ---------------------------------------------------------------------------
# Search + sanitization
# ---------------------------------------------------------------------------


def test_sanitize_fts5_query_shapes_match_expression() -> None:
    assert sanitize_fts5_query('deploy "the" roll-out!') == '"deploy" OR "the" OR "roll" OR "out"'
    assert sanitize_fts5_query("!!! ...") == ""
    assert sanitize_fts5_query("") == ""
    many = " ".join(f"w{i}" for i in range(20))
    assert len(sanitize_fts5_query(many).split(" OR ")) == 14


def test_search_ranks_by_bm25_and_falls_back_when_empty(store: Any) -> None:
    seed(store, title="Sparse", content="deployment mention once")
    seed(store, title="Dense", content="deployment deployment deployment runbook")
    hits = context_store.search_docs(studio.SQLiteStorage(), store, "deployment")
    assert [h["title"] for h in hits] == ["Dense", "Sparse"]
    fallback = context_store.search_docs(studio.SQLiteStorage(), store, "   ")
    assert len(fallback) == 2


# ---------------------------------------------------------------------------
# Vault lint
# ---------------------------------------------------------------------------


def test_lint_reports_dangling_orphan_stale_and_thin(store: Any) -> None:
    good = seed(store, title="Good", content="x" * 40, pinned=True)
    linker = seed(store, title="Linker", content=f"see oc://doc/{good['id']} and oc://doc/oc-missing00 " + "y" * 40, pinned=True)
    stale = seed(store, title="Stale", content="z" * 40)
    thin = seed(store, title="Thin", content="tiny")
    with studio.SQLiteStorage().connect(store) as conn:
        conn.execute("UPDATE opencontext_docs SET updated_at = ? WHERE id = ?", (time.time() - 40 * 86400, stale["id"]))
        conn.commit()
    report = context_store.lint_vault(studio.SQLiteStorage(), store)
    assert report["total_docs"] == 4
    assert report["pinned_docs"] == 2
    assert report["dangling_links"] == [{"from": linker["uri"], "missing_target": "oc://doc/oc-missing00"}]
    assert report["stale_docs"] == [stale["uri"]]
    assert thin["uri"] in report["thin_docs"]
    assert sorted(report["orphan_docs"]) == sorted([stale["uri"], thin["uri"]])
    assert report["health_score"] == 100 - 15 - 10 - min(20, 2 * 4)


def test_storage_seam_accepts_a_fake_double(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "fake.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    studio.init_db()
    fake = RecordingStorage()
    docs = context_store.list_docs(fake, db)
    assert fake.connect_calls == [db]
    assert len(docs) == 1  # the seeded default doc


# ---------------------------------------------------------------------------
# HTTP route integration (isolated app + auth)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(store: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "h" * 40)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", set())
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    headers = {"Authorization": "Bearer " + "h" * 40}
    with TestClient(studio.app) as c:
        c.headers.update(headers)
        yield c


def test_opencontext_routes_crud_lifecycle(client: TestClient) -> None:
    before = client.get("/api/opencontext/docs").json()["docs"]
    assert len(before) == 1  # lifespan re-seeds the default doc (fixture emptied the vault)
    seed_doc = before[0]
    created = client.post("/api/opencontext/docs", json={"title": "API note", "content": "created over HTTP", "pinned": True}).json()
    doc = created["doc"]
    assert doc["title"] == "API note" and doc["pinned"] is True
    assert len(created["docs"]) == 2

    patched = client.patch(f"/api/opencontext/docs/{doc['id']}", json={"content": "patched over HTTP"}).json()
    assert patched["doc"]["content"] == "patched over HTTP"
    assert patched["doc"]["title"] == "API note"

    found = client.get("/api/opencontext/search", params={"q": "patched"}).json()["docs"]
    assert [d["id"] for d in found] == [doc["id"]]

    deleted = client.delete(f"/api/opencontext/docs/{doc['id']}").json()
    assert deleted["status"] == "deleted"
    assert [d["id"] for d in deleted["docs"]] == [seed_doc["id"]]
    assert client.get("/api/opencontext/docs").json()["docs"] != []


def test_opencontext_route_validation_and_missing_doc(client: TestClient) -> None:
    assert client.post("/api/opencontext/docs", json={"title": "", "content": "x"}).status_code == 400
    assert client.patch("/api/opencontext/docs/oc-nope", json={"title": "t"}).status_code == 404


def test_opencontext_lint_route(client: TestClient) -> None:
    client.post("/api/opencontext/docs", json={"title": "Pinned", "content": "x" * 40, "pinned": True})
    report = client.get("/api/opencontext/lint").json()
    assert report["total_docs"] == 2
    assert report["health_score"] > 0
    assert set(report) == {"total_docs", "pinned_docs", "health_score", "dangling_links", "orphan_docs", "stale_docs", "thin_docs"}


def test_pinned_prompt_builder_uses_store(store: Path) -> None:
    seed(store, title="Guide", content="step by step guidance", pinned=True, folder="howto")
    prompt = studio.build_pinned_opencontext_prompt()
    assert "OpenContext Persistent Memory" in prompt
    assert "[Guide](oc://doc/" in prompt and "`howto`" in prompt
    context_store.delete_doc(studio.SQLiteStorage(), store, context_store.list_docs(studio.SQLiteStorage(), store)[0]["id"])
    assert studio.build_pinned_opencontext_prompt() == ""
