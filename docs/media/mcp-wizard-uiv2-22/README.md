# MCP guided connect wizard — captures

UIV2-22 turned the MCP editor into a guide. The hub from UIV2-21 is unchanged above it; what
changed is what happens when you open the editor: four questions, one screen at a time, and nothing
saved or tested until the last one is answered.

## After (`*.png`, 104 files)

Thirteen states × four viewports × two themes, written by `scripts/mcp_wizard_walk.mjs` against a
fixture app it starts itself and the real stdio MCP server in `scripts/mcp_hub_fixture_server.py`,
which the guide connects to through its own step 4. Each of the eight runs scored **38/38** checks —
including the two at 390×844, because the guide has no desktop-only measurement.
Viewports: `1640x900`, `1440x900`, `1280x800`, `390x844`. Themes: `light`, `dark`.

| state | what it shows |
| --- | --- |
| `empty` | step 1 of 4 on a fresh server: the rail marks where you are, `Step 1 of 4 · Server` is announced, Back is disabled and Continue is the only way on |
| `credentials` | step 2 with a remote HTTP server: URL, auth header and a password-field token, with the sentence that says Studio will talk to the server over the URL |
| `policy` | step 3: the approval policy select with its consequence written next to it, and no consent row, because a remote server needs none |
| `review` | step 4: the seven things that will be saved — including `Credential: A new token will be stored, and only its mask shown` — and `Save & test connection` |
| `consent` | step 3 for a local command: the consent row names the exact command, the note says the screen cannot grant `STUDIO_ALLOW_MCP_STDIO=1`, and Continue refuses with a reason in amber |
| `loading` | the save held in flight: `Saving and testing…` on a disabled button and a `testing` result line, so the test is visible while it runs |
| `success` | the connection test came back: `Connected — 4 tools discovered.`, the namespace the tools arrive under, and a way to the server list |
| `populated` | the server list after the guide finished, with the new server, its four tools and its policy |
| `error` | a save whose test failed: `Saved, but the connection test failed — …` with the server's own refusal, and the row that keeps the sync note |
| `recovery` | the same server fixed in place — transport changed to stdio, consented, saved again — green, and still one row, not two |
| `keyed` | unchanged-token semantics: a stored token edited with a blank field, shown only as `Token sk-u••••4b71` before and after |
| `keyboard` | the whole path from the keyboard: Tab reaches Continue, Enter advances, focus lands on the step heading, Back returns with the alias intact |
| `long` | a 315-character command in the review at phone width, wrapping instead of pushing the dialog sideways |

## Before (`before-uiv2-22/`, 104 files)

The same walk in `--capture-only`, pointed at the merge commit this story starts from (`b06ab96`) —
thirteen states at the same four viewports and two themes. There the editor is one form: alias,
display name, transport, approval policy, URL, auth header, token, raw headers JSON and Save & Sync
Tools, all on screen at once, with the environment JSON below the fold. No step refuses to advance
because there are no steps; no consent is asked before a local command; no result appears where the
button is. The walk scores **12/38** on each of the eight runs, which is what the before set is for.

## Regenerate

```bash
node scripts/mcp_wizard_walk.mjs --viewport 1440x900 --theme dark \
  --screenshots docs/media/mcp-wizard-uiv2-22

# the before set — from a worktree at the commit this story starts from
git worktree add /tmp/before-uiv2-22 b06ab96
cd /tmp/before-uiv2-22 && node <this repo>/scripts/mcp_wizard_walk.mjs --capture-only \
  --viewport 1440x900 --theme dark \
  --screenshots <this repo>/docs/media/mcp-wizard-uiv2-22/before-uiv2-22
```

The walk starts its own fixture app with `STUDIO_ALLOW_MCP_STDIO=1` and a memory keyring in the
fixture process only; it does not change the app, and the app still refuses to launch a command
unless an operator set that variable first.
