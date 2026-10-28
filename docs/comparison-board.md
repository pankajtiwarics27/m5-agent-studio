# Comparison board view-model

AS-061 defines [`comparison_board.py`](../comparison_board.py): the view-model
layer the comparison and benchmark surface renders from. One row per
candidate model, four columns - quality, latency, memory, cost - and the
acceptance criteria's honesty requirement implemented as first-class cell
states: every cell is exactly one of `measured`, `missing`, `stale`,
`incomparable` or `excluded`, each with a bounded human-readable reason.

## Composition, not policy

`build_board(candidates, *, comparison=None, performance=None, costs=None,
staleness=None)` composes the artifacts owned by the earlier stories instead
of re-deriving them:

- **Quality** comes from an AS-056 `ComparisonResult`: ranked entries supply
  rank, overall, per-dimension medians and the score digest; excluded models
  surface with their exact exclusion reasons and no rank. A ranked score
  recorded under the same model id but a different digest is not silently
  reused - the cell flips to `stale` ("different build") and the row loses
  its rank, mirroring AS-056's refusal to compare incomparable identities.
- **Latency and memory** come from AS-060 vault entries supplied as
  `(ProfileKey, ProfileRecord)` pairs: the board re-checks that each profile
  agrees with its own key (a mismatch is rejected outright, mirroring the
  vault's read-time guarantee) and that the profile digest matches the
  candidate's. Warm and cold aggregates pass through per-metric with median,
  spread and sample count, alongside `profile_version`, `inputs_digest` and
  the profile's failure rate - a profile with zero successful samples of a
  metric renders as `missing`, not as zeros.
- **Cost** is deliberately not computed: no upstream module owns price data,
  so the board accepts caller-supplied `CostObservation` records (amount per
  million tokens, ISO-style currency, bounded source note) and marks every
  model without one `missing`. Fabricating or implying a price would violate
  the story's own honesty clause.
- **Staleness** is the AS-057 ledger's verdict: `superseded` and
  `invalidated` flip every measured cell on that row to `stale` while
  keeping the (faded) numbers available, so the UI can offer history without
  presenting it as current. Informational verdicts (`missing`, `observed`,
  `unknown`) are recorded on the row but do not change cell states.

## Comparability anchor

Benchmark numbers may only be shown side by side when they were measured
under one context. The board takes the **first profiled candidate's**
key triple - hardware digest, `os_id`, methodology digest - as the anchor
(model digest, quantization and runtime version are what the board is
comparing, so they differ by design) and marks any later profile whose
anchor component differs `incomparable`, naming the component, instead of
mixing machines or methodologies into one column.

## Determinism

The board has no wall-clock, no file access and no randomness: the same
inputs always produce the same canonical `to_mapping()` and digest, so a
screenshot, an audit record and a cache key can all reference the exact
board a user saw. Board-level disclosures carry AS-056's
environment-difference report and any scored models outside the candidate
list. Notes are whitespace-collapsed and bounded to 300 characters at the
cell level, so arbitrarily long upstream text can never balloon the render.

Validation is exhaustive and loud: non-`ModelDescriptor` candidates, empty
or duplicate identities, mappings referencing non-candidates, malformed
performance pairs, unknown verdicts and foreign cost types all raise
`ComparisonBoardError` or `TypeError` before any render is attempted.

## Evidence

`tests/test_comparison_board.py` (13 tests) covers a fully populated board
with exact per-column values, all-missing rows, stale-flip retention of
numbers, informational verdicts, per-build staleness for both scores and
profiles, hardware/methodology/os incomparability, zero-count failure-only
profiles, partial measurement (latency measured, memory missing),
excluded-model reasons with rank suppression, environment and
outside-candidate disclosures, every validation refusal, and digest
determinism/sensitivity.
