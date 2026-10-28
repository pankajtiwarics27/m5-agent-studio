# Context reassembly on model switch

AS-055 defines `reassemble_context()` and the descriptor bridge
`reassemble_for_target()` in [`context_reassembly.py`](../context_reassembly.py).
It rebuilds a workflow transcript so it fits the target model's context
window, with every exclusion recorded — the auditable truncation AS-052's
newest-fitting-messages trim leaves implicit at the policy level.

## Policy

- **Anchors are mandatory.** `system` and `developer` messages are never
  dropped; a transcript whose anchors alone exceed the budget fails with
  `ContextReassemblyError` naming the exact token counts, rather than
  silently corrupting instructions.
- **The newest message is mandatory.** The latest turn is never dropped, so a
  switch never strands the workflow without its current input.
- **Tiered drops, oldest first.** Large `tool`/`function` outputs drop before
  older assistant turns, which drop before other droppable turns; iteration
  stops the moment the rebuilt transcript fits, so drops are minimal.
- **Output room is reserved.** The budget is the target's `context_tokens`
  minus its declared `max_output_tokens` (or an explicit
  `reserve_output_tokens`), so the rebuilt prompt leaves room for the reply
  instead of consuming the entire window.
- **Estimation is injected.** The default token estimate is the same word
  count AS-052 uses — deliberately consistent, so a context that survived
  `switch_model()` reassembles with zero additional drops — and callers with a
  real tokenizer pass an `estimator`. Estimates are labeled as estimates; no
  tokenizer is required and none is implied.

## Audit

`ReassembledContext` reports the kept messages in chronological order with
their original indices and per-message token counts, the budget, the used
total, and a `dropped` ledger of `(index, role, tokens, reason)` records.
The ledger references positions and sizes only — message bodies are never
copied into the audit or `to_mapping()` output, matching the repository's
hash-only audit convention. Reassembly is a pure data function: no provider
call, model load, network access, or mid-message truncation (partial content
rewrites are out of scope by design and fail visibly instead).

## Evidence

`tests/test_context_reassembly.py` (15 tests) covers intact small contexts,
the empty transcript, reservation arithmetic at the exact fit and one token
over, tool-before-conversation drop tiers, oldest-first order within a tier,
`function`/unknown-role classification, the never-drop rules for the newest
message and anchors (audible failure with counts), injected-estimator
accounting in both directions, content-privacy of the audit mapping with its
exact shape, determinism and index ordering of repeated runs, the descriptor
bridge, every invalid-input rejection path, and AS-052 consistency: the
output of `switch_model()` reassembles against the same target with zero
additional drops.
