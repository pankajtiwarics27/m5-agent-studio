# UIV2-29 — performance and backend safety regression gate

Measured streaming, DOM/heap, layout and backend safety evidence; unprobed container isolation remains unqualified and blocked.

## Fail-closed evidence policy

The contract does not create its own evidence. A green result requires a source-bound real-Chromium run, an executed backend pytest suite, and the isolation smoke test. Stale, partial, failed or threshold-weakened records are rejected.

PNG captures remain local review evidence; the committed browser record carries their SHA-256 digests and exact frontend/walk digests. CI reruns both the legacy stream walk and this gate.

## Measured browser run

**21/21 checks passed; 11 captures; 0 failures.**

Environment: `linux` / `x64`, 2 CPUs, Node `v22.22.3`, `Python 3.11.2`, `HeadlessChrome/153.0.8010.0` headless Chromium.

| Metric | Actual | Gate |
| --- | ---: | ---: |
| Fast real SSE schedules | 58 in 334.9ms (173.2/s) | ≥20 and ≥50/s |
| Coalesced chat renders | 18 | fewer than 58 schedules, complete text retained |
| Full-window ten-event frame | mean 1.83ms; nearest-rank p95 2.40ms; max 2.60ms | mean ≤4ms |
| Events / cache / cards | 30,000 / 5,000 / 300 | 5,000 / 300 ceilings |
| Post-GC document nodes | 8544 → 8544 → 8544 | repeated-cycle growth ≤64 |
| Post-GC JS heap | 3.24 MiB → 3.20 MiB → 3.20 MiB | cycle growth ≤8 MiB; baseline growth ≤16 MiB |

Memory readings use CDP `Performance.getMetrics` immediately after `HeapProfiler.collectGarbage`. They are repeated-cycle plateau checks, not a claim that a browser never allocates during a frame.

### Layout stability by viewport and theme

Streaming intentionally moves cards inside the trajectory scroller. The gate reports that internal movement separately and requires zero meaningful shift **outside** it, stable topbar/stream/composer rectangles, no document overflow, and 300 cards. Detached attribution nodes are classified by their previous/current rectangles; unattributable entries count as external and fail closed.

| Viewport | Theme | Total shift | Internal stream | External shell | Geometry delta | Overflow |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1640x900 | light | 0.18827 | 0.18827 | 0.00000 | 0.00px | 0px |
| 1640x900 | dark | 0.29613 | 0.29613 | 0.00000 | 0.00px | 0px |
| 1440x900 | light | 0.26887 | 0.26887 | 0.00000 | 0.00px | 0px |
| 1440x900 | dark | 0.38410 | 0.38410 | 0.00000 | 0.00px | 0px |
| 1280x800 | light | 0.23537 | 0.23537 | 0.00000 | 0.00px | 0px |
| 1280x800 | dark | 0.39229 | 0.39229 | 0.00000 | 0.00px | 0px |
| 390x844 | light | 0.94757 | 0.94757 | 0.00000 | 0.00px | 0px |
| 390x844 | dark | 1.35367 | 1.35367 | 0.00000 | 0.00px | 0px |

## Executed backend and isolation run

**305 tests passed across 13 owning modules; 16 isolation checks passed; 0 failures.**

Environment: Python `3.11.2`, `Linux-6.1.158+-x86_64-with-glibc2.36`, 2 CPUs. No external service, provider, user workspace or live container probe is used.

Container runtime available: **no**. Isolation status: **unqualified**; dependent execution: **blocked (fail closed)**. This is intentional fail-closed behavior, not an isolation qualification.

| Acceptance area | Executed owners |
| --- | --- |
| high frequency sse | `scripts/uiv2_performance_safety_walk.mjs`, `scripts/stream_perf_walk.mjs` |
| bounded dom and memory | `scripts/uiv2_performance_safety_walk.mjs`, `tests/test_stream_perf.py` |
| permissions and authorization | `tests/test_harness_permissions.py`, `tests/test_scoped_grants.py`, `tests/test_durable_intent_ledger.py` |
| crash and stream recovery | `tests/test_recovery_guard.py`, `tests/test_process_supervision.py`, `tests/test_harness_recovery.py`, `tests/test_chat_recovery.py`, `tests/test_pipeline_recovery.py` |
| action ledger and lease fencing | `tests/test_side_effect_ledger.py`, `tests/test_durable_intent_ledger.py`, `tests/test_worker_leases_and_fencing.py` |
| blocked isolation | `tests/test_isolation_smoke.py`, `scripts/isolation_smoke.py` |

## Measured rendering repair

- **Before:** isolated current-Chromium baseline: 4.67ms for a ten-event frame at a full 300-card/5000-event window; 14/15 legacy checks.
- **Implementation:** ordered snapshot append/prune, stable read-only snapshots, cached-snapshot render guard, no redundant note/count writes, disclosure scans only on full rebuilds.
- **Preserved:** general out-of-order/dedupe merge, approval-decision scan, cursor semantics, 300-card window, 5000-event cache, reader offset and open disclosures.

The pre-existing 4ms budget was not relaxed. The general merge still handles out-of-order, duplicate and invalid input, and the approval-decision scan was not cached away.

## Visual and preservation contract

The approved UIV2-26 cross-tab structural gate remains binding at 1640×900, 1440×900, 1280×800 and 390×844 in light and dark. This story's eleven actual captures add loading, populated and 30,000-event long-content evidence; they do not self-approve a structural delta. The UIV2-27 preservation run and UIV2-28 accessibility suites remain mandatory.

## Reproduce

```bash
python3 scripts/uiv2_performance_safety.py --run-backend
node scripts/stream_perf_walk.mjs --screenshots /tmp/uiv2-29-stream
node scripts/uiv2_performance_safety_walk.mjs --screenshots /tmp/uiv2-29-performance
python3 scripts/uiv2_performance_safety.py --import-evidence /tmp/uiv2-29-performance/capture-manifest.json
python3 scripts/uiv2_performance_safety.py --check
node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out /tmp/uiv2-29-cross-tab
```

Contract findings: **0**.
