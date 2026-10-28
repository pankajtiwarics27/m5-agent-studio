"""OpenContext personal-memory store: CRUD, FTS5 search, and vault linting.

AS-012 boundary extraction. The OpenContext vault (`oc://doc/<id>`) is the
agent's persistent memory layer: a SQLite table mirrored into an FTS5 index.
This module owns that boundary and nothing else — every function receives its
storage handle and database path explicitly, so tests can run against a
temporary database (or a fake storage double) without touching application
state. Prompt assembly (`build_pinned_opencontext_prompt`) stays in the
application: it is presentation, not storage.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Protocol


class StorageLike(Protocol):
    """Minimal storage seam (structurally compatible with app.SQLiteStorage)."""

    def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection: ...


def doc_uri(doc_id: str) -> str:
    return f"oc://doc/{doc_id}"


def sanitize_fts5_query(raw_query: str) -> str:
    """Reduce free text to a safe OR-of-quoted-tokens FTS5 MATCH expression."""
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", raw_query or "")
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:14])


def _row_to_doc(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["pinned"] = bool(d.get("pinned", 0))
    d["uri"] = doc_uri(str(d["id"]))
    return d


def list_docs(storage: StorageLike, db_path: Path, folder: str = "") -> list[dict[str, Any]]:
    with storage.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if folder:
            rows = conn.execute(
                "SELECT * FROM opencontext_docs WHERE folder = ? ORDER BY pinned DESC, updated_at DESC",
                (folder,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM opencontext_docs ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
    return [_row_to_doc(r) for r in rows]


def get_doc(storage: StorageLike, db_path: Path, doc_id_or_uri: str) -> dict[str, Any]:
    clean_id = (doc_id_or_uri or "").strip().replace("oc://doc/", "").strip("/")
    with storage.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM opencontext_docs WHERE id = ?", (clean_id,)).fetchone()
    if not row:
        raise KeyError(f"OpenContext doc `{clean_id}` not found.")
    return _row_to_doc(row)


def upsert_doc(
    storage: StorageLike,
    db_path: Path,
    title: str,
    content: str,
    folder: str = "decisions",
    description: str = "",
    pinned: bool = False,
    doc_id: str = "",
) -> dict[str, Any]:
    clean_title = (title or "").strip()[:140]
    if not clean_title:
        raise ValueError("Document `title` is required.")
    clean_folder = re.sub(r"[^a-z0-9_/-]+", "-", (folder or "decisions").strip().lower()).strip("-") or "decisions"
    did = (doc_id or "").strip().replace("oc://doc/", "") or f"oc-{uuid.uuid4().hex[:8]}"
    desc = (description or content.splitlines()[0] if content else "").strip()[:240]
    now = time.time()
    with storage.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO opencontext_docs (id, folder, title, description, content, pinned, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              folder=excluded.folder, title=excluded.title, description=excluded.description,
              content=excluded.content, pinned=excluded.pinned, updated_at=excluded.updated_at
            """,
            (did, clean_folder, clean_title, desc, content, 1 if pinned else 0, now),
        )
        conn.execute("DELETE FROM opencontext_fts WHERE doc_id = ?", (did,))
        conn.execute(
            "INSERT INTO opencontext_fts (doc_id, folder, title, description, content) VALUES (?, ?, ?, ?, ?)",
            (did, clean_folder, clean_title, desc, content),
        )
        conn.commit()
    return get_doc(storage, db_path, did)


def delete_doc(storage: StorageLike, db_path: Path, doc_id: str) -> None:
    clean_id = (doc_id or "").strip().replace("oc://doc/", "")
    with storage.connect(db_path) as conn:
        conn.execute("DELETE FROM opencontext_docs WHERE id = ?", (clean_id,))
        conn.execute("DELETE FROM opencontext_fts WHERE doc_id = ?", (clean_id,))
        conn.commit()


def search_docs(storage: StorageLike, db_path: Path, query: str, limit: int = 8) -> list[dict[str, Any]]:
    fts_q = sanitize_fts5_query(query)
    if not fts_q:
        return list_docs(storage, db_path)[:limit]
    with storage.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT d.*, bm25(opencontext_fts) AS rank_score
            FROM opencontext_fts
            JOIN opencontext_docs d ON d.id = opencontext_fts.doc_id
            WHERE opencontext_fts MATCH ?
            ORDER BY rank_score ASC
            LIMIT ?
            """,
            (fts_q, limit),
        ).fetchall()
    return [_row_to_doc(r) for r in rows]


def lint_vault(storage: StorageLike, db_path: Path) -> dict[str, Any]:
    """
    Deterministic Knowledge Hygiene (`lint`):
      Checks `oc://doc/<id>` citation resolution, orphan unlinked notes, stale notes (>30d), and empty bodies.
    """
    docs = list_docs(storage, db_path)
    valid_ids = {d["id"] for d in docs}
    inbound_refs: dict[str, int] = {d["id"]: 0 for d in docs}
    dangling_links: list[dict[str, str]] = []
    stale_docs: list[str] = []
    thin_docs: list[str] = []
    now = time.time()

    link_rx = re.compile(r"oc://doc/([A-Za-z0-9_-]+)")
    for d in docs:
        body = d.get("content") or ""
        if len(body.strip()) < 30:
            thin_docs.append(d["uri"])
        if now - float(d.get("updated_at") or now) > 30 * 86400:
            stale_docs.append(d["uri"])
        for ref_id in link_rx.findall(body):
            if ref_id not in valid_ids:
                dangling_links.append({"from": d["uri"], "missing_target": f"oc://doc/{ref_id}"})
            elif ref_id != d["id"]:
                inbound_refs[ref_id] = inbound_refs.get(ref_id, 0) + 1

    orphans = [d["uri"] for d in docs if not d["pinned"] and inbound_refs.get(d["id"], 0) == 0 and len(docs) > 1]
    score = max(0, 100 - len(dangling_links) * 15 - len(thin_docs) * 10 - min(20, len(orphans) * 4))
    return {
        "total_docs": len(docs),
        "pinned_docs": sum(1 for d in docs if d["pinned"]),
        "health_score": score,
        "dangling_links": dangling_links,
        "orphan_docs": orphans,
        "stale_docs": stale_docs,
        "thin_docs": thin_docs,
    }
