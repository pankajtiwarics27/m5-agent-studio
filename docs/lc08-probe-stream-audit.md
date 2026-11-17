# LC08 `execute_conservative_model_switch_async._run_probe_stream` audit — bounded continuation, the probe's delegated source

## Scope and coverage accounting

This increment audits the delegated source consumed inside
`app.execute_conservative_model_switch_async._run_probe_stream` — the
target-model semantic-confirmation probe of the conservative model switch. It
is the last non-SSE owner in the
[static owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
whose recorded seam is `stream_any_model`, and it is the one owner of the
remaining four that actually consumes a delegated stream. The other three
(`api_run_harness_turn.event_gen`, `execute_child_delegation_async`,
`execute_conservative_model_switch_async`) consume only `_unload_*` seams, which
perform HTTP acknowledgment rather than return a stream, so there is no
delegated iterator in them to close.

The probe helper read its source through a bare `async for`:

```python
async def _run_probe_stream(http_c: httpx.AsyncClient) -> None:
    async for item in stream_any_model(...):
        ...probe_chunks.append(item["text"])
```

## Result: this owner has no consumer-close defect, and this increment does not claim one

The prior five audits each proved a real gap: an outer SSE frame that stopped
reading left the delegated source suspended until interpreter-shutdown
finalization. **This owner has no such path, and I am not going to manufacture
one.** Measured on the unchanged tree, against the real
`execute_conservative_model_switch_async`, with a synthetic source that records
its own `finally`:

| Abandonment path | Source closed before the caller resumed? |
|---|---|
| Probe source raises mid-iteration (switch fails closed into safe pause) | **Yes** — `closed == calls` |
| `Task.cancel()` while suspended on the probe's next item | **Yes** — `closed == calls` |
| Normal completion | **Yes** |

### Why this owner differs, structurally

The defect the five SSE audits fixed needs a **generator boundary** between the
consumer and the source. An SSE shell is itself an async generator that `yield`s
from inside its loop; it therefore suspends *at the `yield`* with the delegated
source still held, and a consumer that goes away leaves both frames suspended.
Abandoning that outer generator does not close the inner
one — measured here, and it stays open even after `gc.collect()`:

| Shape | Abandonment | Source closed? |
|---|---|---|
| Outer **async generator** with `yield` in the loop body (SSE shell) | consumer dropped, then `gc.collect()` | **No** |
| Plain **coroutine** awaiting `__anext__` (this probe) | `Task.cancel()` | **Yes** |

`_run_probe_stream` is a coroutine, not an async generator, and it has no
`yield` in its loop body. There is no frame to suspend at, so no caller can
abandon it mid-item: the switch call returns or raises. CPython then unwinds the
`async for` frame, and the source's `finally` runs. **The gap is structurally
unreachable for this owner**, which is why the same `bounded_*_stream_close`
assessment would be a misreport.

### What the tests are, and are not

They are **regression pins, not red/green proofs**, and this audit says so
plainly because it was measured: all five pass on the unmodified tree, and they
also pass when the helper is deliberately rewritten into the **defect shape**
(manual `__anext__` iteration that abandons the source on `BaseException`). That
is the honest result — a consumer-close test cannot detect a defect that this
shape cannot express. What they do pin is the behaviour that would actually hurt
if it regressed: a probe failure or cancel still finalizes the source, an
inaccurate transcript is still refused rather than accepted as confirmation, and
the explicit-attestation branch still never enters the probe path.

The criterion this audit leaves behind for the remaining owners: a consumer-close
gap needs a `yield` boundary. The three owners still marked
`inventory_only_in_this_audit` have no delegated stream at all (their seams are
`_unload_*` acknowledgments), so none of them can host this defect either.

What *was* genuinely unproven is that the behaviour above holds and stays held.
This increment pins it with executable evidence rather than a parity edit.

## What this increment changes

- **No production behaviour change.** `app.py` is not edited. A `contextlib.aclosing`
  wrap here would be a no-op — it would duplicate finalization that already
  happens — and adding it would let the matrix claim a "close" assessment this
  owner earns for a different reason.
- **One test module** pinning the measured behaviour, including the
  negative controls that make the *other* claims falsifiable (refusal on an
  inaccurate transcript, branch selection on explicit attestation). As stated
  above, the source-finalization cases are regression pins: they do not fail on
  a defect-shaped rewrite of this helper, and this audit does not present them
  as red/green evidence.
- The matrix row moves to a distinct assessment, `bounded_probe_stream_finalized`,
  whose meaning is deliberately not the `bounded_*_stream_close` family used by
  the five SSE audits. Conflating them would misreport this owner as having had
  the SSE defect.

## Executable evidence and limits

[`test_workflow_model_switch_probe_cancel.py`](../tests/test_workflow_model_switch_probe_cancel.py)
drives the real `execute_conservative_model_switch_async` through its
high-risk semantic-confirmation branch with in-memory collaborators (isolated
harness store, temporary DB and workspace, a stubbed heavy-model admission, and
synthetic `stream_any_model` sources that record their own closure). It touches
no database, network, subprocess or real model. It checks:

1. **Probe source failure** mid-iteration closes the source before the switch
   returns, and the switch still fails closed into the safe pause with
   `handoff_semantic_confirmation_failed` — the partial probe transcript is not
   accepted as confirmation.
2. **Cancellation** while suspended on the probe's next item closes the source.
3. **A truncated probe transcript does not confirm.** A source that yields fewer
   or different fields than the authoritative bundle is rejected, so closing the
   source can never be confused with a verified handoff.
4. **Normal completion** drains and closes the source and the probe mode is
   recorded as `target_model_probe` — the positive control.
5. **The helper is reached only on the high-risk branch.** With
   `semantic_confirmation` supplied explicitly, the probe is not dispatched at
   all (explicit attestation path), which pins that this audit's claim is about
   the probe path and not about switching in general.

Limits: this does not establish provider, native-engine or installed-runtime
probe behaviour; it does not test the semantic quality of a confirmation; and it
says nothing about physical model unload. It is not activation, qualification,
release acceptance, an ADR-0038 status change or an LC09–11 advance.

## Recording

The matrix marks
`execute_conservative_model_switch_async._run_probe_stream`
`bounded_probe_stream_finalized` with this test file as evidence, and records in
the same row that the assessment is **not** a consumer-close fix. **3 of 29
owners remain `inventory_only_in_this_audit`** after this increment
(`api_run_harness_turn.event_gen`, which is PR #323's, and the two
`execute_*` owners whose only seams are `_unload_*` acknowledgments). The
[entry-point inventory](../docs/runtime-dispatch-entry-points.md) remains a
generated static index regenerated by `scripts/runtime_dispatch_inventory.py`;
it reports line numbers, not coverage claims. No production composition, mode
switch, default registration, activation, hardware instruction,
schema/migration, or any human, owner, independent-security, visual or release
approval is claimed or implied.
