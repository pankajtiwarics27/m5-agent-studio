# Quickstart

Get from a clean checkout to a first completed task. Everything here is local:
no account, no cloud key, and no network call happens until you make one.

Related: [`onboarding-preflight.md`](onboarding-preflight.md) (the checks behind
the Start tab), [`../BASELINE.md`](../BASELINE.md) (reproducible setup),
[`configuration-contracts.md`](configuration-contracts.md) (every setting).

## Install

Python `>=3.11` is the only hard requirement. Pick one of two shapes.

**Standalone single file** — recommended on macOS, no virtualenv needed for the
code itself, but the runtime dependencies still come from pip:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install 'fastapi>=0.115,<1' 'uvicorn>=0.32,<1' 'httpx>=0.28,<0.29' \
  'httpcore>=1.0,<2' 'keyring>=25,<27'
python3 studio.py
```

`studio.py` is generated from `app.py` + `static/index.html`; rebuild it with
`python scripts/build_studio.py` after changing sources. To install it as a
versioned release instead of running it in place, see
[`packaging-rollback.md`](packaging-rollback.md).

**Development checkout** — split sources, editable install:

```bash
git clone https://github.com/pankajtiwarics27/m5-agent-studio.git
cd m5-agent-studio
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]'
python3 app.py
```

The server binds `127.0.0.1:3000` by default. Open <http://127.0.0.1:3000>.

## First run

On first startup Studio creates a bearer token at
`~/.config/m5-agent-studio/api-token` — file `0600`, directory `0700`, honouring
`XDG_CONFIG_HOME` — and prints the URL. Startup refuses a token file or directory
that is a symlink or owned by another user. The
browser prompts for that token once and keeps it in `sessionStorage`.

Then open the **Start** tab. It reads local configuration only and lists six
ordered steps — authentication, model source, model availability, workspace,
execution mode, first task — with exactly one highlighted next action. Green
means you can proceed; amber means one click is needed; red means something is
blocked (for example a missing token).

Nothing in the preflight contacts the network. The single exception is the
**Check local models** button, which asks your configured local runtime for its
model list because that is the only way to know which local models exist.

## Connect a model

Choose one. Either is enough to finish the first task.

**Local runtime** — start oMLX / Ollama / llama.cpp on its loopback port and set
the base URL if it is not the default:

```bash
export OMLX_BASE_URL=http://127.0.0.1:8000   # default
```

Return to **Start** and press **Check local models**.

**Cloud provider** — open **Providers**, add an alias, base URL, and API key.
The key is stored in the OS keychain, not in SQLite. Then press **Fetch models**
so Studio knows what it can call. A provider that is disabled, has no
credential, or lists no models does not count as ready — that is deliberate.

Routing *combos* (for example the seeded `hybrid-resilient`) are presets over
other targets. They never count as available models.

## First task

Press **Run the first task** on the **Start** tab. It opens Chat with a guided
prompt and sends exactly one request to the model you selected. A reply means the
whole path — auth, provider routing, streaming — works, and you are done with
setup.

From there:

- **Harness** runs multi-step agent work in an isolated Git worktree.
- **Council**, **Pipeline**, and **Compare** run multi-model workflows.
- `http://127.0.0.1:3000/v1` is an OpenAI-compatible gateway for Cursor, Cline,
  or OpenCode.

Code execution is **disabled by default**. To let the agent run shell/Python,
enable the container sandbox (`STUDIO_EXECUTION_MODE=sandbox`, needs Docker or
Podman) rather than host mode; see [`../SECURITY.md`](../SECURITY.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 Authentication required` on `/api/*` | No `Authorization: Bearer` header, or a stale token in the browser | Reload the page and re-enter the token printed at startup; compare with `~/.config/m5-agent-studio/api-token` |
| `503 Authentication is unavailable` | The process has no token (for example a skipped ASGI lifespan) | Restart Studio; protected routes fail closed by design |
| Start tab says *Connect a model source* | No enabled provider with a credential, and no local runtime models found | Add a provider with a key, or start the local runtime and press **Check local models** |
| Start tab says *Fetch the model list* | Provider saved, but no models listed | Press **Fetch models** in Providers |
| Start tab says *The local runtime did not answer* | `OMLX_BASE_URL` unreachable | Start the runtime, confirm the port, press **Check local models** |
| `Address already in use` | Port 3000 busy | `STUDIO_PORT=3001 python3 app.py` |
| Keyring errors at startup | No OS keychain available in this session (SSH, container) | Startup fails closed rather than storing plaintext; unlock the keychain or run with a desktop session |
| Browser blocks a cross-origin call | Host/Origin allowlists | Set `STUDIO_ALLOWED_HOSTS` / `STUDIO_ALLOWED_ORIGINS`; see [`configuration-contracts.md`](configuration-contracts.md) |

## Evidence

- `python scripts/smoke_local.py` and `python scripts/smoke_local.py --standalone`
  boot both shapes on loopback and check health, token generation, protected API,
  Origin rejection, and database isolation. The standalone run copies the
  artifact to an isolated directory, so a missing inlined module fails here.
- `tests/test_onboarding_preflight.py` (29 tests) covers the Start-tab contract,
  including the rule that combos and uncredentialed providers never look ready.
- The commands here were run in this repository on Linux / Python 3.11.2 as part
  of the AS-041 change; a real-browser walkthrough on macOS is part of the
  maintainer's final end-to-end verification.
