# MCP tools: what a call can do, and what it needs before it runs

Story **UIV2-23** — MCP tool discovery, approval and test-call experience.

The hub's fourth tab is the tool browser. One row per tool the server actually returned,
each stating the four things that decide whether a model may call it — and the playground
below it is a test call that has to be told, in words, what it is about to do.

## Four different numbers

| counter | meaning |
| --- | --- |
| configured | servers the operator added |
| discovered | tools the servers actually returned |
| available | tools a model may call right now |
| denied | tools the allowlist refuses |

## Four risks, and the only thing that makes a tool a read

| risk | label | why |
| --- | --- | --- |
| `read` | Read-only | the server set annotations.readOnlyHint |
| `write` | Write | nothing said otherwise: the fail-closed default |
| `write_idempotent` | Write · repeatable | it carries an idempotency key |
| `destructive` | Destructive | the server set annotations.destructiveHint |

A tool the server says nothing about is a **write**. The order is fixed: `read`, then
`destructive`, then `write_idempotent`, then `write`.

## Refusals the page can explain

| kind | the page says |
| --- | --- |
| `server_denied` | the server is switched off |
| `tool_denied` | the tool is outside the allowlist |
| `unknown_tool` | the server never returned that tool |
| `stdio_gate` | the local-command gate is closed |
| `approval_required` | the harness policy needs a human approval |
| `paused_idempotency` | an earlier write is unresolved |

## The test call

`Test call` on a row lands on the Connected tab with that server and tool selected and the
arguments pre-filled from the tool's schema. Before anything is posted the arguments are
validated against that schema — not JSON, not an object, or missing a required field each get
a sentence in an alert and a disabled button. The risk is stated in the same words the Tools
tab uses, and anything that is not a read needs a consent naming the tool and the risk; the
request carries `confirm_write` only when that box is ticked, and the server refuses the call
with `428 confirmation_required` if it is not. A `403` is drawn as a denial with the kind
named and the way out given. The inspector then states the outcome, the latency, the
character count, the authorization (grant source, policy, whether the write was confirmed and
whether a model would have been asked), the output — truncated with a real `Show all` — and
the raw response behind a disclosure that starts closed.

## Checks

47 checks, 0 finding(s).

| check | result | detail |
| --- | --- | --- |
| the hub's tab strip has a fourth tab — Tools — with a count of what it holds | pass | tabs: Catalog, Connected, Tools, Activity |
| the Tools panel is a labelled tabpanel with a summary, filters, a list and a real empty state | pass | summary in a live region, a labelled filter group, one list and one empty state |
| switching to Tools is a real tab change: one panel shown, the roving tab stop moved | pass | switchMcpTab() whitelists tools exactly like the other three |
| the tab and its count are rendered from the same state the rows are | pass | renderMcpHub() calls renderMcpTools(), which sets the tab count from the rows |
| configured, discovered, available and denied are four numbers, each named | pass | configured: servers the operator added · discovered: tools the servers actually returned · available: tools a model may call right now · denied: tools the allowlist refuses |
| a server with no tools yet is counted separately, not hidden among the zeros | pass | `N configured without tools yet` appears only when a server has returned nothing |
| an empty Tools tab names the next action instead of showing a blank panel | pass | the empty state says what to do, not that there is nothing |
| the filter chips count what they would show, and one is pressed | pass | All / Read-only / Writes / Denied, each with a live count and aria-pressed |
| search reaches the tool list, and a search with no hit says so and offers to clear itself | pass | the hub's one search box narrows the rows, and the empty state clears it |
| a tool is read-only only when the server says so, and a silent tool is a write | pass | read → destructive → write_idempotent → write, with `write` as the fall-through |
| the four risks have labels and a consequence in words | pass | Read-only because the server set annotations.readOnlyHint · Write because nothing said otherwise: the fail-closed default · Write · repeatable because it carries an idempotency key · Destructive because the server set annotations.destructiveHint |
| each row states the risk, the state, what a call does, what happens when a model asks, and its arguments | pass | five labelled statements per row; the row carries its risk and state as data attributes too |
| the row answers the policy question in the policy's own terms | pass | one sentence per policy, plus one for a denied tool |
| the approval policy is explained in words, per server policy | pass | three sentences, one per policy, and the Connected row prints one for each server |
| a denied row is drawn as denied and its test call is disabled | pass | dashed row, Denied pill, no Test call |
| Allow and Deny on a row are the allowlist itself, not a decoration | pass | the row calls the same endpoint the drawer's checkbox does |
| Test call opens that exact tool in the playground, on the server that owns it | pass | openMcpToolTest() selects the tab, the server and the tool, then pre-fills its arguments |
| the arguments are validated against the tool's own schema before anything is posted | pass | invalid JSON, a non-object and a missing required argument each get a sentence |
| the refusal is announced, and the button stays disabled while it stands | pass | role=alert, cleared as soon as the arguments could be accepted; the button follows |
| the risk is stated before the call, in the same words as the Tools tab | pass | a pill and a sentence, plus the reason it will be refused when there already is one |
| anything that is not a read needs a consent that names the tool and cannot be pre-ticked | pass | hidden for a read, named for a write, and cleared whenever the subject changes |
| the request carries confirm_write only because the box was ticked | pass | confirm_write is added to the body only when the consent is given |
| a 428 is drawn as a refusal that says nothing ran, not as a result | pass | the confirmation_required body becomes its own state with the next step |
| a 403 is drawn as a denial, with the kind named and the way out given | pass | server_denied → the server is switched off · tool_denied → the tool is outside the allowlist · unknown_tool → the server never returned that tool · stdio_gate → the local-command gate is closed · approval_required → the harness policy needs a human approval · paused_idempotency → an earlier write is unresolved |
| the inspector states the outcome, the cost and what authorized the call | pass | outcome · latency · characters · grant source · policy · confirmation · whether a model would be asked |
| long output is truncated with a count, and Show all restores every character | pass | head and tail with the hidden count in between; Show all prints the rest |
| the raw response is offered behind a disclosure that starts closed | pass | `Raw response`, shut; the inspector fills it and opens nothing |
| the server classifies a tool from its annotations, and a silent tool is a write | pass | read → destructive → write_idempotent → write, in the same order as the page |
| every tool the API returns states its own risk and whether it is allowed | pass | get_mcp_server() adds read_only, destructive, requires_approval_override, risk and denied per tool |
| a refusal is named before the call is even risked | pass | disabled server, unknown tool, allowlisted-out tool, closed stdio gate — in that order |
| the executor's own refusals can be classified without matching words on the page | pass | server_denied · tool_denied · unknown_tool · stdio_gate · approval_required · paused_idempotency — and a fallback word for anything unrecognised |
| the call route answers each refusal with its own status and a structured body | pass | 404 unknown server · 403 with denial.kind · 428 confirmation_required · 409 duplicate side effect |
| a call that runs says what authorized it, instead of implying it | pass | surface, policy, grant source, approval, confirmation, risk and whether a model would be asked |
| the confirmation is required before the executor, not after it | pass | the 428 is returned before any execution is attempted |
| the six MCP routes are still there | pass | /api/mcp/servers · /api/mcp/servers · /api/mcp/import-json · /api/mcp/servers/{alias} · /api/mcp/servers/{alias}/sync · /api/mcp/servers/{alias}/call |
| the stdio launch gate is untouched | pass | a local command still needs the operator's own opt-in, whatever the page says |
| the auto approval policy still means what it meant | pass | auto runs without asking unless the tool asks for an override; this story did not tighten it |
| action admission and the workspace panes are still in place | pass | the admission gate the harness goes through, and all six panes |
| no React rewrite and no new dependency | pass | the frontend is still the same single document |
| every control the old playground had is still on the page | pass | mcpPlayServer, mcpPlayTool, mcpPlayArgs, mcpPlayBtn, mcpPlayOutput, configuredMcpServersList, mcpToolsCountBadge, activeMcpPills, mcpImportJsonInput, mcpSaveBtn |
| a browser walk drives the tool browser and the test call on a screen | pass | mcp_tools_walk.mjs: 57 recorded checks |
| the walk proves a write cannot be posted without consent, and the server refuses it if it is | pass | the page's gate and the server's 428 are both asserted |
| the walk proves Deny is the allowlist and that Allow recovers the same call | pass | Deny → 403 tool_denied on the server → Allow → the same call succeeds |
| the walk covers every state the ledger asks for, and can photograph the old dialog | pass | empty · list · filters · search · confirm · error · loading · result · long · denied · recovery · failure · keyboard |
| captures exist for every state, viewport and theme | pass | 120 after PNG · missing: none |
| the before set is the same instrument against the dialog this story replaced | pass | 112 before PNG across 8 viewport/theme runs |
| the roadmap records this story as done, with its commit and its evidence | pass | status=done commit=648af14 visual=6 acceptance=6 |

## Captures

`docs/media/mcp-tools-uiv2-23/` holds `empty`, `list`, `filter`, `search`, `search-empty`, `consent`, `error`, `loading`, `result`, `long`, `denied`, `refused`, `recovery`, `failure`, `keyboard` at four viewports (1640×900, 1440×900, 1280×800, 390×844) in light and dark,
beside `before-uiv2-23/`: the same instrument run in `--capture-only` against the dialog this
story replaced (three tabs, the tool list in a collapsed drawer, a playground with no risk,
no consent and a one-line output box).

## Regenerate

```
node scripts/mcp_tools_walk.mjs --screenshots docs/media/mcp-tools-uiv2-23
python3 scripts/uiux_mcp_tools_inventory.py
python3 scripts/uiux_mcp_tools_inventory.py --check
```
