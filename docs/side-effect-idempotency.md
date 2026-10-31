# Side-effect reconciliation and idempotency

AS-034 defines `SideEffectLedger` in
[`side_effect_ledger.py`](../side_effect_ledger.py). Callers claim an
operation key and request hash before performing a non-idempotent action, then
record completion or failure.

The SQLite primary key and `BEGIN IMMEDIATE` make claims atomic. Repeated or
conflicting keys are rejected; only an explicitly failed record may be claimed
again. Completion cannot be silently duplicated or overwritten. The ledger
stores hashes and status metadata, not side-effect payloads or credentials.

## Evidence

`tests/test_side_effect_ledger.py` covers atomic duplicate claims, conflicting
request rejection, completion/finalization rules, explicit failed-operation
retry, and persistence across a new ledger instance. For remote MCP connector
idempotency semantics, external-effect reconciliation, and uncertain-write
session pauses (`M5-P3-05`), see [`mcp-idempotency.md`](mcp-idempotency.md) and
[`ADR-0015`](adr/0015-mcp-idempotency-and-external-effect-reconciliation.md).
