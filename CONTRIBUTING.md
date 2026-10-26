# Contributing

Thanks for helping improve M5 Agent Studio. This is a local-first developer application with optional model providers, MCP connections, and tool execution; contributions should preserve its privacy and secure-by-default behavior.

## Before opening an issue or pull request

- Read the [Code of Conduct](CODE_OF_CONDUCT.md) and [Security policy](SECURITY.md).
- Search existing issues and pull requests. Use the repository's issue templates for bugs and feature proposals.
- Keep changes focused. Describe the problem, proposed behavior, compatibility impact, and tests in your pull request.
- Do not include API tokens, provider keys, private prompts, personal data, or unredacted logs in issues, commits, fixtures, or screenshots. Follow `SECURITY.md` for suspected vulnerabilities; do not report them in a public issue.

## Development setup and checks

The project supports Python 3.11 or newer. CI currently exercises Python 3.11, 3.12, and 3.13 on Linux. Use an isolated virtual environment and install the existing development extra:

```bash
python -m venv .venv
# Activate it: source .venv/bin/activate (POSIX) or .venv\Scripts\activate (Windows)
python -m pip install -e '.[dev]'
python -m pytest -q
python -m compileall -q app.py workflow_orchestration.py scripts tests
python scripts/check_frontend_syntax.py
python scripts/smoke_local.py
python scripts/smoke_local.py --standalone
python scripts/validate_governance.py
```

For generated-file changes, also run the relevant generator and verify reproducibility:

```bash
python scripts/build_studio.py
python scripts/generate_route_matrix.py
git diff --check
```

The live container-runtime test is opt-in and is not required by the ordinary test suite. Do not make tests contact live model, cloud-provider, MCP, or other external services; use mocks, local fakes, temporary paths, and disposable loopback servers. Never run tests against a user's real workspace, credentials, or data.

## Project-specific contribution principles

- **Hardware and model neutrality:** Do not require a specific chip, memory size, operating system, local model, provider, or model-family ordering. Platform-specific capabilities must degrade explicitly and remain optional. Include tests for platform-independent behavior and any platform-specific branches.
- **Provider independence:** Keep integrations configurable and behind existing provider abstractions. Do not hard-code a provider as the only path or add a provider dependency without a reviewed need.
- **Privacy:** Do not add startup-time, background, or otherwise implicit outbound requests. Remote calls must be tied to a clear user action and configured integration, documented in the relevant user-facing and security documentation, and covered by tests that do not contact the service.
- **Secure execution:** Preserve deny-by-default behavior for code execution, MCP stdio, and skill code. Do not weaken URL validation, authentication, approval, sandboxing, audit, workspace, or secret-handling boundaries without a focused security review and regression tests.
- **Compatibility:** Preserve existing API, CLI, configuration, and database behavior unless a deliberate migration is documented and tested. Avoid adding runtime dependencies for documentation or governance tasks.
- **Evidence:** Tests should cover expected behavior, rejection paths, and failures. Report platform limitations honestly; do not turn skipped opt-in checks into claims of verified isolation.

## Architecture decisions

For a material, long-lived choice, follow [`docs/adr/README.md`](docs/adr/README.md) and start from [`docs/adr/0000-template.md`](docs/adr/0000-template.md). The ADR should record context, decision, alternatives, tradeoffs, and decision/review status; run `python scripts/validate_adrs.py`. Do not include secrets, private prompts, personal data, or unredacted vulnerability details in these documents.

## Pull request review

A pull request should explain the user-visible change, security/privacy implications, compatibility or migration considerations, and the exact checks run (including skipped checks and warnings). Update documentation and generated artifacts when their sources change. Maintainers may request changes, decline a contribution, or ask for a smaller scope. No CLA or DCO sign-off is currently required.

## License

By submitting a contribution, you represent that you have the right to submit it and agree that it may be distributed under the repository's [MIT License](LICENSE). You retain copyright in your contribution; this statement does not transfer ownership to the project maintainer.
