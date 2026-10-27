"""Durable idempotency ledger for non-idempotent workflow side effects."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EffectStatus = Literal["claimed", "completed", "failed"]
_STATUSES = frozenset({"claimed", "completed", "failed"})


class SideEffectError(ValueError):
    """Raised when a side-effect ledger request is malformed."""


class DuplicateSideEffect(RuntimeError):
    """Raised when an operation key already represents a non-retryable effect."""


@dataclass(frozen=True, slots=True)
class EffectRecord:
    operation_key: str
    effect_name: str
    request_hash: str
    status: EffectStatus
    result_hash: str | None
    attempts: int
    updated_at: str


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SideEffectError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


def _status(value: str) -> EffectStatus:
    if value not in _STATUSES:
        raise SideEffectError(f"unknown side-effect status: {value}")
    return value  # type: ignore[return-value]


class SideEffectLedger:
    """Claim operation keys atomically before execution and finalize exactly once."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS side_effects (
                    operation_key TEXT PRIMARY KEY,
                    effect_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_hash TEXT,
                    attempts INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (status IN ('claimed', 'completed', 'failed')),
                    CHECK (attempts > 0)
                )
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> EffectRecord | None:
        if row is None:
            return None
        _status(row["status"])
        return EffectRecord(row["operation_key"], row["effect_name"], row["request_hash"], row["status"], row["result_hash"], row["attempts"], row["updated_at"])

    def get(self, operation_key: str) -> EffectRecord | None:
        key = _text(operation_key, "operation_key")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return self._record(connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone())

    def claim(self, operation_key: str, effect_name: str, request_hash: str) -> EffectRecord:
        key = _text(operation_key, "operation_key")
        name = _text(effect_name, "effect_name")
        digest = _text(request_hash, "request_hash")
        with sqlite3.connect(self.database, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
            existing = self._record(row)
            if existing is not None:
                if existing.request_hash != digest or existing.effect_name != name:
                    raise DuplicateSideEffect("operation key conflicts with a different request.")
                if existing.status != "failed":
                    raise DuplicateSideEffect("operation key already has a claimed or completed side effect.")
                connection.execute(
                    "UPDATE side_effects SET status = 'claimed', result_hash = NULL, attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE operation_key = ?",
                    (key,),
                )
            else:
                connection.execute(
                    "INSERT INTO side_effects(operation_key, effect_name, request_hash, status, attempts) VALUES (?, ?, ?, 'claimed', 1)",
                    (key, name, digest),
                )
            updated = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result

    def complete(self, operation_key: str, result_hash: str) -> EffectRecord:
        return self._finish(operation_key, "completed", result_hash)

    def fail(self, operation_key: str, result_hash: str = "failure") -> EffectRecord:
        return self._finish(operation_key, "failed", result_hash)

    def _finish(self, operation_key: str, status: EffectStatus, result_hash: str) -> EffectRecord:
        key = _text(operation_key, "operation_key")
        digest = _text(result_hash, "result_hash")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
            existing = self._record(row)
            if existing is None or existing.status != "claimed":
                raise DuplicateSideEffect("only a claimed side effect can be finalized.")
            connection.execute(
                "UPDATE side_effects SET status = ?, result_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE operation_key = ?",
                (status, digest, key),
            )
            updated = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result
