# MCP hub: a catalog you can search, a dashboard you can read

Story **UIV2-21** — MCP hub: searchable catalog and connected-server dashboard.

| section | what it answers |
| --- | --- |
| Catalog | what could I connect? each card states remote HTTP / local stdio, trust, key
| | requirement, approval policy and the endpoint or command it would use |
| Connected | what is connected? the editor is shut until a card or Edit opens it; rows show
| | transport, policy, tools and any stored sync note; the playground runs one live call |
| Activity | what happened? per-server health with its error text, and every test call with
| | its outcome and latency |

Search and the filter chips work across all three sections; the configuration form is never
on screen while you browse the catalog.

## Checks

41 checks, 0 finding(s).

| check | result | detail |
| --- | --- | --- |
| the dialog is a hub: one tablist with three tabs | pass | tabs: Catalog, Connected, Activity |
| each tab controls its own panel, and the panels are tabpanels | pass | panels: Catalog, Connected, Activity · aria-controls and aria-labelledby wired both ways |
| the landing tab is the catalog, not the configuration form | pass | Catalog is selected and Connected starts hidden |
| switching a tab shows exactly one panel and moves the roving tabindex | pass | switchMcpTab() hides every other panel, sets aria-selected and a single tab stop |
| the tab strip answers the arrow keys, Home and End | pass | one delegated listener bound when the hub opens; no inline key handler in the markup |
| the dialog is a labelled modal | pass | role=dialog, aria-modal, labelled by the hub title |
| the hub has one search box that names what it searches | pass | type=search, labelled, re-renders the hub on input |
| the catalog filters count what they would show | pass | each chip is a toggle with a count and aria-pressed |
| search narrows the cards and hides the rows that do not match | pass | applyMcpHubSearch() hides every row whose haystack misses the term |
| a search with no hit says so and offers to clear itself | pass | the empty state names the term and offers a Clear search action |
| a preset is a card, not a chip with a name | pass | mcpCatalogCardHtml() renders a named card with its target |
| every card states its transport, trust, key requirement and policy | pass | remote/local · trust · key requirement · approval policy on every card |
| the transport says remote HTTP or local stdio in words | pass | a pill, not a colour or an icon alone |
| the key requirement names the variable or says no key is needed | pass | either `No key needed` or the exact environment variable |
| trust is derived from the stored preset, never invented | pass | mcpPresetTrust() reads requires_token and approval_policy only |
| the card says whether it is connected and what its button will do | pass | state line plus a Use preset / Review action |
| cards show the endpoint or the command they would run | pass | remote cards print the URL, stdio cards print command and arguments |
| the configuration form is a shut disclosure, not the first thing shown | pass | the editor is a closed <details> with a summary that says what it is |
| a card opens the editor for you and lands on the Connected tab | pass | selectMcpPreset() and editMcpServer() both open the editor on Connected |
| the editor describes what is actually in it, not the card that filled it | pass | syncMcpEditorState() reads the alias and picks preset / editing / new |
| connected rows state transport, policy and their sync note | pass | each row carries its search key, its tools and the error it really got |
| the connected list explains itself when nothing is connected | pass | the empty state points at the Catalog tab |
| every control the old dialog had is still on the hub | pass | 20 controls:  |
| all three approval policies survive, on the form and on the rows | pass | auto / inherit / always_ask kept in the select and in the per-server row |
| Activity reports every server's health, not just the happy ones | pass | state pill, policy, tool counts, token mask and the stored sync note per server |
| a test call is recorded with its outcome, its latency and a capped detail | pass | both the success and the failure path record, and the detail is capped at 160 characters |
| the activity log is bounded and newest first | pass | unshift with a 12-entry cap |
| the tab counts are computed from the state, not typed in | pass | catalog / connected / activity counts read the live arrays |
| no renderer interpolates a secret into the page | pass | renderers: only masked values |
| the only credential the hub renders is the server's mask | pass | masked_token in the connected row and in Activity |
| saving empties the token field | pass | the typed secret does not stay in the DOM after a save |
| the served payload is what masks the secret, not the page | pass | app.py sends masked_token; the hub never sees the raw value |
| the six MCP routes are unchanged and none were added | pass | 6 routes, no additions |
| spawning a stdio command is still gated on the operator's opt-in | pass | STUDIO_ALLOW_MCP_STDIO is still required before a command is launched |
| the workspace still has all six tabs | pass | start, harness, hall, council, debate, chat |
| the top strip still shows the MCP pill row and its tool count | pass | the strip is outside the dialog and untouched |
| a browser walk asserts the hub on a screen | pass | mcp_hub_walk.mjs: 40 recorded checks |
| the walk covers every state the ledger asks for | pass | STATES = catalog, search, connected, secrets, activity, loading, long, error |
| the walk connects a real stdio server rather than inventing one | pass | the fixture speaks the protocol; the fixture app opts into stdio launch explicitly |
| the walk refuses to photograph the hub with a fake secret | pass | the secret is typed into the form and then searched for in the document |
| captures exist for every viewport and theme, with a before set | pass | 88 after PNG, 88 before PNG |

## States

`catalog`, `search`, `connected`, `secrets`, `activity`, `loading`, `long`, `error`.

## Regenerate

```
python3 scripts/uiux_mcp_hub_inventory.py
python3 scripts/uiux_mcp_hub_inventory.py --check
```
