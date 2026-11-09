# LC08 Hall HTTP summon audit — dormant continuation

## Scope

This bounded follow-up to merged PR264/base `b6134c5` audits
`api_hall_summon.event_gen`, including full input assembly, the reasoning-only
retry, owned source lifetime and optional cleanup. It does not audit
`_mcp_summon_model`, other workflows, installed engines or LC09 retention/recovery.
Legacy remains the default; no backend is installed or enabled, no route/schema
changes, hardware action or acceptance advancement is introduced.

The [owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
still maps **29 owners / 54 static sites**. Two shared Council/Debate adapter owners
retain their earlier bounded assessment; this increment assesses **one Hall HTTP
owner**, leaving **26 inventory-only owners**. Council/Debate assembly closures
remain separately named. Static coverage does not establish runtime qualification.

## Reproduced findings and changes

The initial ten-case Hall suite against unchanged application code produced
**3 failed / 7 passed**. Coordinated reasoning-only output retried with `off` and
4,096 tokens instead of the requested reasoning mode and 4,600 tokens. Consumer
close and disconnect left the owned dispatch source open until later generator
finalization. These failures used a fake coordinated backend, not native hardware.

Both Hall-owned `stream_any_model` sources now use explicit `aclosing`. The
reasoning-only retry is permitted only outside coordinated mode. Legacy retains
its existing 4,600/requested-mode then 4,096/off sequence, with both attempts closed.
No assembly, normalization, output cap, fallback model or retry is added.

Hall persistence intentionally differs from whole-workflow run persistence:

1. Consume and close the reply source.
2. Save the completed Hall reply, then optionally pin the conclusion.
3. Perform optional local cleanup.
4. Emit `summon_done` only after successful cleanup.

Pending/refused/uncertain and malformed `ok` cleanup results still emit an error,
not success. They **do not undo a saved/pinned transcript or label it unsaved**.
Interrupted unsaved turns retain the existing abandonment record and running/active
facts clear in `finally`. Explicit error responses retain existing terminal-error
semantics. No LC09 session-retention policy is changed.

## Executable evidence and limits

[Hall tests](../tests/test_workflow_hall_coordinated.py) cover **21 cases**:
full long Unicode matter/workspace/proceeding log/skills and exact options;
coordinated reasoning-only no-retry; legacy retry and retry-source close;
consumer close/disconnect/task cancellation; partial backend failure; advertised
and returned native-tool refusal by this text-only consumer; save/pin/source-close
before confirmed cleanup; four unconfirmed/malformed cleanup results; and six
warm native completion/close/context-refusal cases across Ollama and LM Studio.

The six native cases use the real dormant executor, request-fit admission,
coordinator, memory ledger and bridge, but **synthetic** transport, profiles,
capabilities, observations and counters. The Ollama fixture explicitly supplies
an approved fake 4,600/off/zero-temperature profile before warm admission; it does
not change a live profile. LM Studio's off capability remains an explicit fake
installed assumption. Positive cases use a fake count of 1,000; refusal cases
use 7,000 against the fixture's 8,192 context plus unchanged 4,600 output reserve.
These numbers test accounting/control flow, **not actual tokenization or feasibility
of this long prompt on an installed model**. Assertions compare the full parsed
application payload, final counted/sent native bytes and exact invocation count.
Context refusal sends no inference and saves no transcript. Midstream close closes
the wire while conservatively retaining one active request, paused residency and
the 600-byte synthetic charge; it does not claim native completion or release.

The existing isolated standalone test now also exercises Hall retry denial, close
and cleanup refusal with public sibling imports blocked. Source-level negative
controls, compiled only in isolated subprocess memory, are caught: remove retry
denial (**1 failure**), remove owned closing (**5 failures**), or swallow cleanup
refusal (**4 failures**). Tracked sources were not mutated for those experiments.

Early native fixture attempts correctly rejected a mismatched fake profile,
context overflow or thinking packets inconsistent with `off`. Those fixture
repairs are not additional application defects or evidence of live qualification.

## Validation and remaining gaps

The Hall suite passes **21 tests**. The six-module Hall/workflow/native/Council-state/
Pipeline contract suite passes **187 tests, 1 upstream warning in 24.14s**. The
backend safety gate passes **302 tests / 16 isolation checks**; isolation remains
unqualified and blocked. UI artifact checks pass **37/37, 7/7 gates, 0 findings**,
and restored V2 review remains 0 findings with imported evidence unchanged.
Ruff, mypy (75 sources), documentation/ADR checks, frontend syntax, pinned baseline
and scope-map validation (204 modules / 13 areas) pass. Full regression and exact-head
CI results must be recorded after execution; a started process is not a pass.
The first full local run against `34b9698` caught an older Council-participants
contract that required the literal direct `async for` spelling. That incomplete
run was stopped at roughly23% and is not a full result. The contract now checks
the AST instead: both initial/retry sources must be closed scopes, both must end
before the direct transcript save, and the direct success yield must follow that
save. This strengthens ownership/save-order coverage rather than dropping the
protected behavior. The expanded seven-module focused suite passes
**223 tests, 1 upstream warning in32.97s**. Runtime and generated app artifacts
are unchanged by this test-only correction; a fresh full run and exact-head CI
remain pending.

The complete app-derived artifact dependency graph is refreshed, without changing
historical approval pins, imported browser evidence or security-release decisions.

G1 remains partial, G2–G6 and LC09–LC11 remain open. Test8 remains paused/unpassed.
No installed/cold multi-stage/oMLX inference qualification, full workflow integration,
production composition, hardware acceptance or independent review is claimed.
