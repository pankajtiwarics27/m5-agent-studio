"""Local history privacy: export, retention, and best-effort secret redaction.

AS-030 contracts. These helpers are pure functions over an open SQLite
connection so the server, the maintenance script, and tests share one
implementation. Design boundaries:

- Export covers conversation, telemetry, harness, context-vault, and
  OpenContext tables. It never includes credential or configuration tables
  (``cloud_providers``, ``mcp_servers``, ``model_combos``), the tool audit
  trail (``tool_audit_events``), or migration bookkeeping.
- Redaction is pattern-based defense-in-depth applied on export. It catches
  common credential shapes; it cannot guarantee detection of arbitrary
  secrets embedded in free text. Keep raw databases protected regardless.
- Retention prunes conversation/telemetry/context artifacts older than the
  configured age. It deliberately does not touch OpenContext documents (a
  persistent knowledge vault with its own delete API), the tool audit trail
  (a security record), or schema bookkeeping.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"


class HistoryPrivacyError(RuntimeError):
    """Raised when export or retention is invoked with invalid parameters."""


# Common credential shapes: provider API keys, tokens in headers, key=value
# assignments, and JSON credential fields. Best-effort by design.
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),
    re.compile(
        r"(?<![A-Za-z0-9])(?:api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|auth[_-]?token"
        r"|session[_-]?token|token|secret|password|passwd|authorization|credentials?|private[_-]?key)"
        r"\"?\s*[:=]\s*\"?[A-Za-z0-9._\-/+=]{8,}\"?",
        re.IGNORECASE,
    ),
)


def redact_text(value: str) -> tuple[str, int]:
    """Return the redacted text and how many replacements were made."""
    if not value:
        return value, 0
    total = 0
    result = value
    for pattern in _REDACTION_PATTERNS:
        result, count = pattern.subn(REDACTED, result)
        total += count
    return result, total


def redact_json(value: Any) -> tuple[Any, int]:
    """Recursively redact every string in a JSON-like structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        total = 0
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_key, key_count = redact_text(key) if isinstance(key, str) else (key, 0)
            new_item, item_count = redact_json(item)
            out[new_key] = new_item
            total += key_count + item_count
        return out, total
    if isinstance(value, (list, tuple)):
        total = 0
        items: list[Any] = []
        for item in value:
            new_item, count = redact_json(item)
            items.append(new_item)
            total += count
        return (items if isinstance(value, list) else tuple(items)), total
    return value, 0


@dataclass(frozen=True)
class RetentionTable:
    """A history table pruned by its own timestamp column."""

    table: str
    timestamp_column: str
    label: str


# Conversation, telemetry, and context-vault artifacts with per-row clocks.
# OpenContext documents, tool_audit_events, and schema bookkeeping are
# intentionally absent (see module docstring).
RETENTION_TABLES: tuple[RetentionTable, ...] = (
    RetentionTable("runs", "created_at", "saved runs"),
    RetentionTable("perf_samples", "created_at", "performance samples"),
    RetentionTable("hall_sessions", "created_at", "Council Hall sessions"),
    RetentionTable("ctx_session_metrics", "updated_at", "context-vault metrics"),
    RetentionTable("ctx_vault_chunks", "created_at", "context-vault chunks"),
)

EXPORT_TABLES: tuple[str, ...] = (
    "runs",
    "perf_samples",
    "hall_sessions",
    "hall_messages",
    "harness_sessions",
    "ctx_vault_chunks",
    "ctx_session_metrics",
    "opencontext_docs",
)

# Never exported: credentials/configuration and security/integrity trails.
EXCLUDED_TABLES: tuple[str, ...] = (
    "cloud_providers",
    "model_combos",
    "mcp_servers",
    "tool_audit_events",
    "schema_migrations",
)


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def export_history(conn: sqlite3.Connection, *, redact: bool = True) -> dict[str, Any]:
    """Bundle local history tables into a JSON-serializable dict.

    Row timestamps are converted to ISO-8601 UTC for portability. With
    ``redact`` enabled (the default), every string field passes the
    best-effort credential scrubber and the total replacement count is
    reported.
    """
    import datetime as _dt

    present = _existing_tables(conn)
    tables_out: dict[str, list[dict[str, Any]]] = {}
    replacements = 0
    for table in EXPORT_TABLES:
        if table not in present:
            continue
        cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table allowlist
        columns = [str(d[0]) for d in cursor.description or []]
        rows: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            record: dict[str, Any] = {}
            for column, value in zip(columns, row):
                if column.endswith("_at") and isinstance(value, (int, float)):
                    value = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc).isoformat()
                if redact and isinstance(value, str):
                    value, count = redact_text(value)
                    replacements += count
                record[column] = value
            rows.append(record)
        tables_out[table] = rows
    try:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, IndexError, TypeError, ValueError):
        schema_version = 0
    return {
        "format": "m5-agent-studio history export",
        "schema_version": schema_version,
        "redaction": {"enabled": bool(redact), "replacements": replacements, "marker": REDACTED if redact else None},
        "excluded_tables": {
            "credentials_and_configuration": ["cloud_providers", "model_combos", "mcp_servers"],
            "security_and_integrity_trails": ["tool_audit_events", "schema_migrations"],
        },
        "tables": tables_out,
    }


def apply_retention(conn: sqlite3.Connection, *, max_age_days: int, now: float) -> dict[str, int]:
    """Delete history artifacts older than ``max_age_days``; return per-table counts.

    Council Hall messages and context-vault chunks are pruned with their
    owning sessions/metrics so no orphaned conversation content survives.
    OpenContext documents, the tool audit trail, and schema bookkeeping are
    never pruned here.
    """
    if isinstance(max_age_days, bool) or not isinstance(max_age_days, int) or max_age_days < 1:
        raise HistoryPrivacyError("max_age_days must be an integer >= 1 (0 means retention is disabled).")
    if now <= 0:
        raise HistoryPrivacyError("now must be a positive epoch timestamp.")
    cutoff = now - max_age_days * 86400.0
    present = _existing_tables(conn)
    deleted: dict[str, int] = {}

    def _prune(table: str, column: str, label: str) -> int:
        if table not in present:
            return 0
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {column} < ?", (cutoff,))  # noqa: S608 - fixed identifiers
        return int(cursor.rowcount or 0)

    for spec in RETENTION_TABLES:
        count = _prune(spec.table, spec.timestamp_column, spec.label)
        if count:
            deleted[spec.table] = count
    # Children of pruned owners (Hall messages belong to sessions; vault
    # chunks belong to a context session).
    if "hall_messages" in present and "hall_sessions" in present:
        cursor = conn.execute(
            "DELETE FROM hall_messages WHERE session_id NOT IN (SELECT id FROM hall_sessions)"
        )
        if cursor.rowcount:
            deleted["hall_messages"] = int(cursor.rowcount)
    if "ctx_vault_chunks" in present and "ctx_session_metrics" in present:
        cursor = conn.execute(
            "DELETE FROM ctx_vault_chunks WHERE session_id NOT IN (SELECT session_id FROM ctx_session_metrics)"
        )
        if cursor.rowcount:
            deleted["ctx_vault_chunks"] = deleted.get("ctx_vault_chunks", 0) + int(cursor.rowcount)
    conn.commit()
    return deleted
