from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from side_effect_ledger import DuplicateSideEffect, SideEffectError, SideEffectLedger


def test_claim_complete_and_duplicate_are_durable(tmp_path: Path) -> None:
    database = tmp_path / "effects.db"
    ledger = SideEffectLedger(database)
    claimed = ledger.claim("operation-1", "write-file", "request-hash")
    assert claimed.status == "claimed"
    completed = ledger.complete("operation-1", "result-hash")
    assert completed.status == "completed"
    assert SideEffectLedger(database).get("operation-1") == completed
    with pytest.raises(DuplicateSideEffect, match="already"):
        ledger.claim("operation-1", "write-file", "request-hash")


def test_conflicting_request_and_invalid_finalization_are_rejected(tmp_path: Path) -> None:
    ledger = SideEffectLedger(tmp_path / "effects.db")
    ledger.claim("operation-1", "send", "hash-a")
    with pytest.raises(DuplicateSideEffect, match="conflicts"):
        ledger.claim("operation-1", "send", "hash-b")
    with pytest.raises(DuplicateSideEffect, match="claimed"):
        ledger.complete("missing", "result")
    ledger.complete("operation-1", "result")
    with pytest.raises(DuplicateSideEffect, match="claimed"):
        ledger.fail("operation-1")


def test_failed_operation_may_be_reclaimed_once_and_concurrent_claims_do_not_duplicate(tmp_path: Path) -> None:
    ledger = SideEffectLedger(tmp_path / "effects.db")
    ledger.claim("retry", "job", "hash")
    ledger.fail("retry")
    retried = ledger.claim("retry", "job", "hash")
    assert retried.attempts == 2

    def attempt() -> bool:
        try:
            ledger.claim("concurrent", "job", "hash")
        except DuplicateSideEffect:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        successes = list(pool.map(lambda _item: attempt(), range(8)))
    assert sum(successes) == 1


def test_invalid_ledger_inputs_fail_closed(tmp_path: Path) -> None:
    ledger = SideEffectLedger(tmp_path / "effects.db")
    with pytest.raises(SideEffectError, match="operation_key"):
        ledger.claim("", "job", "hash")
    with pytest.raises(SideEffectError, match="unknown side-effect status"):
        ledger._record({"status": "unknown"})  # type: ignore[arg-type]
