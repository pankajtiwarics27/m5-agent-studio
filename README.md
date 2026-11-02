# M5 Agent Studio

A local-first AI development studio for working with language models and
building, testing, and reviewing agent-assisted code changes.

[![CI](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml)

**New here?** Start with the [Quickstart](docs/quickstart.md). For the full
 documentation index, see [docs/README.md](docs/README.md).

## What it does

- Chat with configured local or cloud model providers.
- Run agent-assisted development tasks in a Git worktree, with reviewable
  changes and verification evidence.
- Use multi-model Council, Pipeline, and Compare workflows.
- Connect supported tools and MCP servers, and use the OpenAI-compatible `/v1`
  API.

The application is designed for local use. Model and external-service requests
occur only through integrations you configure and actions you initiate.

## Quick start

Requirements: Python 3.11 or newer and Git.

```bash
git clone https://github.com/pankajtiwarics27/m5-agent-studio.git
cd m5-agent-studio
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python app.py
```

Open <http://127.0.0.1:3000>. On first startup, Studio creates a local bearer
token and asks for it in the browser. Follow the [Quickstart](docs/quickstart.md)
to connect a model and run your first task.

To preview the seeded, provider-free demo after installing dependencies:

```bash
python scripts/demo_tour.py
```

## Safety and data

Studio binds to loopback by default and requires bearer authentication for its
API. Code execution, MCP stdio, and skill-script execution are disabled unless
explicitly enabled. Treat prompts, history, workspace files, and tool output as
sensitive local data; sending content to a configured remote provider or MCP
server shares it with that service.

Do not expose Studio to an untrusted network or enable host execution unless you
understand the risks. See [SECURITY.md](SECURITY.md) for deployment guidance,
unsafe opt-ins, and limitations, and [docs/privacy.md](docs/privacy.md) for data
handling and retention.

## Development

Install the development dependencies as shown above, then run:

```bash
ruff check .
mypy
python -m pytest -q
python scripts/check_frontend_syntax.py
python scripts/validate_docs.py
```

The canonical application sources are `app.py` and `static/index.html`.
`studio.py` is generated from them; rebuild it after source changes with:

```bash
python scripts/build_studio.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full verification workflow, and
[AGENTS.md](AGENTS.md) for repository-specific coding and agent instructions.

## Documentation

- [Quickstart](docs/quickstart.md) — installation, first run, provider setup,
  and troubleshooting
- [Architecture](docs/architecture.md) — components, request flow, storage, and
  extension points
- [Configuration](docs/configuration-contracts.md) — environment variables and
  settings contracts
- [Supported hardware](docs/supported-hardware.md) — platform and capability
  guidance
- [Privacy](docs/privacy.md) — local data, external requests, secrets, and
  retention
- [Documentation index](docs/README.md) — user guides, subsystem contracts, and
  project evidence

## License

MIT. See [LICENSE](LICENSE).
