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
python scripts/verify_baseline.py
git ls-files -z -- '*.py' | xargs -0 -r python -m compileall -q
python scripts/check_frontend_syntax.py
python -m pytest -q
python scripts/smoke_local.py
python scripts/smoke_local.py --standalone
python scripts/build_studio.py
python scripts/generate_route_matrix.py
python scripts/generate_subsystem_inventory.py
python scripts/generate_roadmap_status.py
git diff --exit-code
python scripts/validate_governance.py
```

This is the same sequence CI runs; a pull request is mergeable only when all three Python legs pass it.


The live container-runtime test is opt-in and is not required by the ordinary test suite. Do not make tests contact live model, cloud-provider, MCP, or other external services; use mocks, local fakes, temporary paths, and disposable loopback servers. Never run tests against a user's real workspace, credentials, or data.

## Project-specific contribution principles

- **Hardware and model neutrality:** Do not require a specific chip, memory size, operating system, local model, provider, or model-family ordering. Platform-specific capabilities must degrade explicitly and remain optional. Include tests for platform-independent behavior and any platform-specific branches.
- **Provider independence:** Keep integrations configurable and behind existing provider abstractions. Do not hard-code a provider as the only path or add a provider dependency without a reviewed need.
- **Privacy:** Do not add startup-time, background, or otherwise implicit outbound requests. Remote calls must be tied to a clear user action and configured integration, documented in the relevant user-facing and security documentation, and covered by tests that do not contact the service.
- **Secure execution:** Preserve deny-by-default behavior for code execution, MCP stdio, and skill code. Do not weaken URL validation, authentication, approval, sandboxing, audit, workspace, or secret-handling boundaries without a focused security review and regression tests.
- **Compatibility:** Preserve existing API, CLI, configuration, and database behavior unless a deliberate migration is documented and tested. Avoid adding runtime dependencies for documentation or governance tasks.
- **Evidence:** Tests should cover expected behavior, rejection paths, and failures. Report platform limitations honestly; do not turn skipped opt-in checks into claims of verified isolation.

## Commit messages: every commit must be documented

A commit is a message to future maintainers (human or AI). Each commit must let
a reader understand what changed and why without diffing the tree:

- **Subject** (first line): 8–100 characters, imperative mood, no trailing
  period. Prefer `AS-XXX: <what changed>` for roadmap story work (e.g.
  `AS-030: Add history retention and export controls`), or
  `type: <what changed>` with `feat|fix|docs|refactor|test|chore|ci` otherwise.
- **Body**: after a blank line, explain what changed, why, and the evidence —
  tests added, commands run and their results, tradeoffs, compatibility notes,
  and any blockers. At least a short paragraph; "misc fixes" or an empty body
  is not acceptable.
- `python scripts/check_commit_messages.py --base origin/main` enforces this on
  new commits and runs in CI. Rewrite unpushed work with
  `git rebase -i origin/main`; do not amend shared history.

## Branches and merges

- One roadmap story per branch, named `as-XXX-short-slug` (for example
  `as-030-history-retention`). Keep branches rebased on `origin/main` while a
  pull request is open.
- Pull requests merge with **rebase** to keep history linear; each merged
  commit therefore carries its own documented message.
- **Merged branches are deleted automatically** (repository setting
  `delete_branch_on_merge`). Do not reuse a merged branch name; start a fresh
  branch for the next story.

## Keeping the roadmap status honest

Product work is tracked in
[`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json);
the readable dashboard at `docs/product-roadmap-status.md` is generated from it
by `scripts/generate_roadmap_status.py` and verified in CI. When your pull
request lands a story (or its verification evidence):

1. Update that story's `status` (`planned` / `in_progress` / `completed` /
   `verified`), `status_evidence`, and `status_updated` in the JSON, plus
   `status_tracking.last_updated`.
2. Regenerate the dashboard and commit both files in the same pull request.
3. State the status change in the pull request description with pointers to the
   evidence (tests, checks, docs).

A status may only change in the pull request that contains the underlying
implementation or evidence.

## Architecture decisions

For a material, long-lived choice, follow [`docs/adr/README.md`](docs/adr/README.md) and start from [`docs/adr/0000-template.md`](docs/adr/0000-template.md). The ADR should record context, decision, alternatives, tradeoffs, and decision/review status; run `python scripts/validate_adrs.py`. Do not include secrets, private prompts, personal data, or unredacted vulnerability details in these documents.

## Pull request review

A pull request should explain the user-visible change, security/privacy implications, compatibility or migration considerations, and the exact checks run (including skipped checks and warnings). Update documentation and generated artifacts when their sources change. Maintainers may request changes, decline a contribution, or ask for a smaller scope. No CLA or DCO sign-off is currently required.

## License

By submitting a contribution, you represent that you have the right to submit it and agree that it may be distributed under the repository's [MIT License](LICENSE). You retain copyright in your contribution; this statement does not transfer ownership to the project maintainer.
