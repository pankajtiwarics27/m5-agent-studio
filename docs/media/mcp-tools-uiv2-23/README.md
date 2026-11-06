# MCP tool browser and test call — captures

UIV2-23 gave the MCP hub a fourth tab: the tools a server actually returned, each with the risk of
calling it, whether the allowlist allows it, and what its approval policy promises. The playground
below it stopped being a one-click runner and became a test call that states the risk, validates the
arguments against the tool's schema, asks for a consent naming the tool, and reports what authorized
the call.

## After (`*.png`, 120 files)

Fifteen states × four viewports × two themes, written by `scripts/mcp_tools_walk.mjs` against a
fixture app it starts itself and the real stdio MCP server in `scripts/mcp_hub_fixture_server.py`,
which it syncs through the UIV2-22 guide. Each of the six desktop runs scored **56/56** checks; the
two phone runs scored **55/55**, because the first-screen measurement is desktop-only.
Viewports: `1640x900`, `1440x900`, `1280x800`, `390x844`. Themes: `light`, `dark`.

| state | what it shows |
| --- | --- |
| `empty` | the Tools tab with nothing connected: four honest zeroes and an empty state that names the next action |
| `list` | four rows — `echo_note` and `big_output` read-only, `slow_echo` a write, `fail_always` destructive — each with its allowlist state, what a call does, what happens when a model asks and the arguments it takes |
| `filter` | the `Writes` chip: the write and the destructive call, the read-only tools gone, the chip counts still visible |
| `search` | the hub's one search box narrowed to `slow_echo` |
| `search-empty` | a search with no hit: `No tool matches …` and a Clear search button |
| `consent` | `Test call` on a write: the risk pill with its sentence, a consent naming `slow_echo`, and Test Call disabled until it is ticked |
| `error` | arguments that are not JSON: the alert naming the position, and the button disabled |
| `loading` | the call held in flight: `Calling...` on a disabled button and a `loading` result box |
| `result` | the inspector: the Write pill, `1660 ms`, `59 characters`, `Authorized as operator_one_call under standard — you confirmed this write`, the output, and the Raw response disclosure shut |
| `long` | a 3625-character output truncated with `[1025 characters hidden — Show all]` and `Show all 3625 characters` |
| `denied` | `slow_echo` denied from its own row: a dashed row, no Test call, the counters at 1/4/3/1 and the `Denied 1` chip |
| `refused` | the server switched off: the risk line says so before the call, and the call is drawn as a denial with the switch to move |
| `recovery` | the server switched back on: the same call succeeds without a reload |
| `failure` | `fail_always` reporting its own failure, and Activity recording it as failed |
| `keyboard` | ArrowRight from Connected to Tools, focus on the chosen tab, and a row's Test call pressed with Enter |

## Before (`before-uiv2-23/`, 112 files)

The same walk in `--capture-only`, pointed at the merge commit this story starts from (`57ef410`) —
the same states at the same four viewports and two themes. There the hub has three tabs, the
discovered tools are a collapsed drawer on the Connected row (a checkbox, a name, a description), and
the playground is a server select, a tool select, a one-line argument box and a `Test Call` button
with no risk, no consent and a raw output box. The walk records the states this build cannot reach and
photographs the surface that is there, which is what the before set is for.

## Regenerate

```bash
node scripts/mcp_tools_walk.mjs --viewport 1440x900 --theme dark \
  --screenshots docs/media/mcp-tools-uiv2-23

# the before set — from a worktree at the commit this story starts from
git worktree add /tmp/before-uiv2-23 57ef410
cd /tmp/before-uiv2-23 && node <this repo>/scripts/mcp_tools_walk.mjs --capture-only \
  --viewport 1440x900 --theme dark \
  --screenshots <this repo>/docs/media/mcp-tools-uiv2-23/before-uiv2-23
```

The walk starts its own fixture app with `STUDIO_ALLOW_MCP_STDIO=1` and a memory keyring in the
fixture process only; it does not change the app, and the app still refuses to launch a command
unless an operator set that variable first.
