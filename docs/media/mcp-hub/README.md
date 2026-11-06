# MCP hub — captures

UIV2-21 turned one long configuration dialog into a hub you can look things up in: a searchable
catalog of presets, a connected-server dashboard and an activity log.

## After (`*.png`, 88 files)

Eleven states × four viewports × two themes, written by `scripts/mcp_hub_walk.mjs` against a fixture
app it starts itself and a real stdio MCP server (`scripts/mcp_hub_fixture_server.py`) that the app
connects to, syncs and calls. Each of the six desktop runs scored **40/40** checks; the two 390×844
runs scored **39/39** (the desktop-only first-screen measurement is not taken at phone width).
Viewports: `1640x900`, `1440x900`, `1280x800`, `390x844`. Themes: `light`, `dark`.

| state | what it shows |
| --- | --- |
| `catalog` | three columns of preset cards; each states Remote·HTTP or Local·stdio, its trust level, its key requirement and its policy, plus the endpoint or command it would use |
| `search` | the catalog narrowed to the cards matching a typed term |
| `connected` | the server list with a live local stdio server, its four synced tools and its policy |
| `connected-error` | a server that could not be reached, holding the sync note it really got |
| `secrets` | a stored token rendered only as its mask — the typed secret appears nowhere in the document |
| `activity` | the health list and the test calls in this session |
| `loading` | a slow call: `Calling…` on a disabled button and `Executing MCP tool call…` in the output |
| `long` | a 4,000-character result scrolling inside its own output box |
| `activity-failed` | a call that returned `isError`, recorded as failed with its message |
| `activity-health` | every configured server, healthy or not, with its state pill and its error |
| `error` | the keyboard state: arrow keys and End moving selection, one panel on screen |

## Before (`before-uiv2-21/`, 88 files)

The same walk, in `--capture-only`, pointed at `origin/main` at `16320ab` — every state at the same
four viewports and two themes. There the presets are two rows of chips that name no transport, no
key requirement and no policy; the add/edit form, the importer, the empty server list and a clipped
playground are all stacked in one scroll; there is no search, no filter and no activity. The walk
scores **8/39** on each of the eight runs, which is what the before set is for.

## Regenerate

```bash
node scripts/mcp_hub_walk.mjs --viewport 1440x900 --theme dark \
  --screenshots docs/media/mcp-hub
```

`python3 scripts/uiux_mcp_hub_inventory.py` inventories what these images are evidence for.
