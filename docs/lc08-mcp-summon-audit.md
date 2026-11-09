# LC08 MCP stdio summon audit — dormant continuation

## Scope and coverage accounting

This follow-up to merged PR265/base `bbf34e4` audits `_mcp_summon_model`, its
`_execute_mcp_stdio_tool` admission composition and its stdio JSON-RPC result.
It is not a new MCP tool, canonical tool consumer, native producer, schema/route
change, lifecycle recovery policy or installed-runtime qualification. Legacy
remains the served default and no native backend is installed or enabled.

The [owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
still contains **29 owners / 54 static sites**. Two shared Council/Debate adapter
owners and one HTTP Hall owner retain their prior bounded assessments. This
increment assesses **one MCP stdio summon owner**, leaving **25 inventory-only
owners**. The two Council/Debate assembly closures remain separately named.
An assessed owner is bounded executable evidence, not blanket workflow support.

## Reproduced finding and minimal change

After correcting the fixture's cleanup exception expectation (the application
already translates cleanup refusal into HTTP503), the unchanged-source baseline
was **1 failed / 9 passed**. A malformed content packet without `text` caused a
consumer `KeyError` while the owned dispatch source remained open. Assertions ran
before event-loop finalization, which otherwise concealed the delayed close.
The earlier four wrong-exception expectations are fixture errors, not additional
runtime defects.

The helper now wraps its single owned `stream_any_model` source in `aclosing`.
There is no prompt, output-budget, reasoning, retry, model, role, save-order or
cleanup-policy change. Source close now completes before the HTTP client context
exits when the consumer rejects an event.

Unlike [HTTP Hall](lc08-hall-summon-audit.md), MCP's existing ordering is:

1. Consume and close the model source.
2. For a local oMLX model, require confirmed cleanup.
3. Exit the HTTP client context.
4. Append the text reply, then return the author-labelled response.

Pending/refused/uncertain and malformed `ok` cleanup replies must not append a
message or produce a successful MCP result. This audit preserves that ordering;
it does not transplant HTTP Hall's save/pin-before-cleanup semantics. The real
admission boundary still denies plan-mode dispatch before client/model effects,
records the admitted outcome, and propagates cleanup exceptions. The stdio
server converts those exceptions to JSON-RPC `isError: true`.

## Executable evidence

The [MCP tests](../tests/test_workflow_mcp_coordinated.py) contain **22 cases**:

- Exact long Unicode matter/chronicle, role system prompt and unchanged
  3,200-token/off/zero-temperature options at the coordinated seam. There is one
  invocation, with no compacting, replacement model or extra reasoning retry.
- Consumer-error source closure before client exit; task cancellation; partial
  backend failure; and no save/cleanup after failed consumption.
- Four unconfirmed/malformed cleanup statuses; source-close/cleanup/client-exit/
  append ordering; advertised and returned native-tool refusal by this text-only
  consumer, not tool execution or canonical native-call support.
- Legacy role fallback and reasoning-only behavior retained; real admission/audit
  composition in plan/standard modes and cleanup refusal; real stdio JSON-RPC
  success/error framing and the existing model/critic defaults.
- Six actual dormant warm executor/fit/coordinator/memory/bridge cases across
  Ollama and LM Studio: completion, pre-inference context refusal and cancellation
  in a blocked synthetic native transport. They compare complete application
  payloads and final counted/sent native bytes. Refusal performs no inference or
  transcript append. Cancellation closes the wire/client while preserving one
  active request, paused residency and a 600-byte synthetic memory charge.

The native transport, capabilities, observations, profiles and counts are
**synthetic test assumptions**, not installed facts. Ollama receives an explicit
fake approved 3,200/off/zero-temperature profile before warm admission. LM Studio
retains the explicit fake off capability. Positive cases count 1,000 tokens;
refusal cases count 7,000 plus unchanged 3,200 output reserve against the fake
8,192 context. These values exercise accounting; they do not prove actual
long-prompt tokenization/fit, physical memory bounds or native termination.
The warm-only executor does not supply cleanup commands; native fixtures use an
explicit non-oMLX alias and do not claim native cleanup support. Local cleanup
semantics are separately tested through the real dispatch command boundary.

The isolated standalone test now also exercises MCP full input/options, consumer
error closure and cleanup refusal with public sibling fallback blocked. In-memory
source mutants in isolated subprocesses are caught: remove closing (**1 failure**),
swallow cleanup exceptions (**6 failures**), truncate the assembled user input
(**1 failure**). Tracked runtime sources were not changed for these experiments.

## Preserved limitations and remaining work

The existing MCP helper assembles the matter and full message chronicle, not
HTTP Hall's skills/workspace-file blocks. This audit does not add those fields or
claim that they were sent. MCP's implicit model/critic defaults, unknown-role
fallback, reasoning-only empty-text behavior and lack of HTTP Hall's closed-room/
active-turn/abandonment lifecycle are unchanged. These differences require their
own contract/retention review; a bounded ownership fix must not silently change
session semantics or advertise parity with HTTP Hall.

No actor/default composition, cold multistage qualification, oMLX inference,
installed tokenizer/transport, already-absent/all/cache controls, LC09 restart or
retention migration, independent acceptance or hardware action is added. G1
remains partial; G2–G6 and LC09–LC11 remain open. Test8 remains paused/unpassed.

## Validation

The MCP suite passes **22 tests**. The nine-module MCP/Hall/workflow/native/
admission/Council/Pipeline suite passes **255 tests, 1 upstream warning in37.47s**.
Ruff, mypy75 and scope-map validation205modules/13areas pass. The complete
app-derived dependency refresh passes backend302tests/16isolation checks
(isolation remains unqualified/blocked), UI37/37checks/7gates/0findings and
restored V2 review7/7local/2200visual/15Compare/0findings. Source64files/6tabs/
121routes, preservation272controls, documentation263files/ADRs, frontend syntax,
pinned baseline, app/standalone startup/auth/origin smokes, screenshots6/6 and
demo checks pass. Final MCP/workflow rerun58passed in3.92s.
Full local regression and exact-head CI must be recorded only after execution;
a started process is not a pass. Historical approvals, imported browser evidence
and independent security-release decisions are not relabelled.
