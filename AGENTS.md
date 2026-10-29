# AGENTS.md — Working on this repository with an AI coding agent

This repository is designed so that an LLM agent (or a human) can clone it and
start productive work immediately, with no context beyond what is in the repo.
This file is the entry point: it tells you what the project is, where the work
plan lives, how to verify changes, and the rules every change must follow.

Read this file top to bottom before writing code.

---

## 1. What this project is

M5 Agentic Studio is an open-source, **local-first** AI developer platform and
agent harness: a FastAPI backend (`app.py` + 22 contract modules), a vanilla
HTML/CSS/JS UI (`static/index.html`), and a generated standalone executable
(`studio.py`). It runs local models (oMLX/MLX, Ollama, llama.cpp) and cloud
providers behind one OpenAI-compatible gateway, with multi-agent workflows
(Harness, Council, Debate, GSD, SDLC), an MCP hub, and a SQLite/FTS5 context
engine. macOS (Apple Silicon) is the first target platform, but nothing may be
hard-coded to a chip, RAM size, model, or vendor.

## 2. Non-negotiable principles

Violating any of these is a rejected change, regardless of tests passing:

1. **Local-first privacy.** No startup-time, background, or implicit outbound
   requests. Cloud/network/billable actions require explicit user
   authorization (policy + consent), fail closed, and are tested.
2. **Hardware / model / provider independence.** No chip, RAM, model-name, or
   vendor hard-coding. Platform-specific capability must degrade explicitly.
3. **Secure defaults.** Execution, MCP stdio, and skill code are deny-by-default.
   Never weaken authentication, origin checks, rate limits, URL/IP pinning,
   approval scoping, sandboxing, audit, or secret handling without a focused
   security review plus positive and negative tests.
4. **Evidence over claims.** Never state that tests were run if they were not.
   Skipped or environment-blocked checks are reported as skipped.

## 3. Where the work plan lives

| Artifact | Role |
|---|---|
| [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json) | **Source of truth for the product plan and per-story status** — 63 stories (`AS-001`…`AS-063`) across 7 milestones, each with priority, dependencies, acceptance criteria, an implementation prompt, and a `status` field (`planned` / `in_progress` / `completed` / `verified`) with `status_evidence`. |
| [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md) | Human-readable dashboard generated from the JSON. **Generated file — never hand-edit.** |
| [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) | Security-remediation depth beyond story status (what is validated, what remains). |
| [`docs/security-review-signoff.md`](docs/security-review-signoff.md) | The AS-031 independent-review record and release gate. Only an independent human reviewer signs it; `scripts/validate_security_signoff.py` fails CI if the record and the roadmap story disagree (a deferral requires `completed`, `verified` requires a signed accepted review). |
| [`docs/adr/`](docs/adr/README.md) | Architecture decision records (the living decision log). |
| [`BASELINE.md`](BASELINE.md) + `BASELINE_COMMIT` | The pinned source baseline and the reproducibility contract. |
| [`README.md`](README.md) | Product overview, quick start, full configuration/API reference, and critical invariants. |

**How to pick work:** choose the highest-priority (`P0` before `P1` before `P2`)
`planned` story whose dependencies are all `completed` or `verified`, respecting
milestone order. One story per branch, one branch per pull request.

**Never self-certify a review gate.** Human verification of this repository is
performed **once, at the end, as final end-to-end testing** after all stories are
complete (maintainer decision 2026-10-29). AS-031 is therefore `completed`, not
`verified`. An agent may prepare evidence, remediate findings, and fix tooling,
but may not fill the reviewer severity, independence, or decision fields, may not
change the record to `signed`, and may not mark AS-031 `verified` on author-only
evidence. `verified` is reachable only through a signed `accepted` review.

## 4. Session start protocol

The roadmap's implementation playbook (in the JSON under
`implementation_playbook`) is the expected agent loop. In short:

1. Inspect the current repository state; do **not** assume the roadmap matches
   the code. Check the story's `status` and `status_evidence`, and grep for
   existing behavior before writing anything.
2. Summarize: current state, gap to the story's acceptance criteria, risks,
   dependencies, and a minimal implementation plan.
3. Implement the smallest maintainable change for **exactly one story**: typed
   contracts, backward compatibility or a documented migration, positive +
   negative + failure tests, docs, and regenerated artifacts.
4. Run the full verification sequence (below), report actual commands and
   results, then open a pull request.
5. Ask questions only when a blocking ambiguity exists; otherwise decide and
   document the decision in the pull request description.

## 5. Environment setup and verification

Python `>=3.11`. No model, provider, MCP, or cloud endpoint is needed for any
check. From a clean checkout:

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e '.[dev]'

python scripts/verify_baseline.py                       # pinned Git ancestry (needs full history: git fetch --unshallow)
ruff check .                                            # lint (generated studio.py excluded)
mypy                                                    # type-check the typed contract modules
git ls-files -z -- '*.py' | xargs -0 -r python -m compileall -q
python scripts/check_frontend_syntax.py                 # needs Node.js
python -m pytest -q
python scripts/smoke_local.py && python scripts/smoke_local.py --standalone
python scripts/build_studio.py                          # regenerate artifacts, then prove no drift:
python scripts/generate_route_matrix.py
python scripts/generate_subsystem_inventory.py
python scripts/generate_roadmap_status.py
git diff --exit-code                                    # any output = stale generated file
python scripts/validate_governance.py
python scripts/validate_security_signoff.py              # AS-031 record/release-gate consistency
python scripts/validate_adrs.py                         # only if you touched docs/adr/
python scripts/check_commit_messages.py --base origin/main   # only new commits are checked
```

CI (`.github/workflows/ci.yml`) runs this same sequence on Python 3.11, 3.12,
and 3.13. A pull request is mergeable only when all three legs are green.

## 6. Branch, commit, and merge rules

- **Branches:** one story per branch, named `as-XXX-short-slug` (e.g.
  `as-030-history-retention`). Dependabot and CI-platform branches keep their
  own conventions.
- **Every commit must be documented** — a future reader must understand it
  without diffing the tree:
  - Subject: 8–100 chars, imperative, no trailing period. Prefer
    `AS-XXX: <what changed>` for story work, or `type: <what changed>`
    (`feat|fix|docs|refactor|test|chore|ci`) otherwise.
  - Body: blank line after the subject, then what changed, why, and the
    evidence (tests added, commands run, tradeoffs, blockers).
  - `scripts/check_commit_messages.py --base origin/main` enforces this on new
    commits; the CI run will fail otherwise. Example:

    ```
    AS-030: Add history retention and export controls

    Adds opt-in retention windows and authenticated export for local
    conversation history, per AS-030 acceptance criteria. ...
    Evidence: pytest -q (N passed), smoke_local.py x2, artifacts regenerated.
    ```
- **Merges:** rebase-merge (linear history). Branches are **deleted
  automatically on merge** (repository setting `delete_branch_on_merge`);
  do not reuse or re-push a merged branch name.

## 7. Landing a story: the status contract

When a pull request completes a story (or advances its verification):

1. Update the story's `status`, `status_evidence`, and `status_updated` in
   `product/Adaptive_Runtime_Product_Roadmap_v2.json`, plus
   `status_tracking.last_updated`. Use the documented vocabulary:
   - `verified` — implemented, merged CI green, dedicated automated tests on main;
   - `completed` — implemented and merged CI green, with a named validation
     aspect still outstanding (state exactly what and why);
   - `in_progress` — partially satisfied (state what exists and what remains);
   - `planned` — untouched.
2. Regenerate the dashboard (`python scripts/generate_roadmap_status.py`) and
   commit both files in the same pull request — CI fails if they drift.
3. Update any docs the story owns (`docs/*.md`, `README.md`,
   `IMPLEMENTATION_STATUS.md`) in the same change.

Status changes without the underlying implementation or evidence in the same
PR are rejected.

## 8. Generated artifacts — regenerate, never hand-edit

| File | Generator |
|---|---|
| `studio.py` | `scripts/build_studio.py` (embeds `static/index.html`) |
| `docs/route-security-matrix.md` | `scripts/generate_route_matrix.py` |
| `docs/subsystem-dependency-inventory.md` | `scripts/generate_subsystem_inventory.py` |
| `docs/product-roadmap-status.md` | `scripts/generate_roadmap_status.py` |

`app.py` and `static/index.html` are the canonical sources. CI regenerates all
four artifacts and fails on any drift, byte-for-byte.

## 9. Hard guardrails

- Never modify or reword `BASELINE_COMMIT`, and never break its ancestry.
- Never add a runtime dependency, public API, or config surface silently —
  document it, test it, and record a decision in `docs/adr/` when the choice is
  long-lived (the roadmap's `architecture_decision_records` list shows the
  planned ADR series).
- Tests must never contact live model, cloud, MCP, or network endpoints; use
  fakes, temporary paths, and disposable loopback servers. The container
  runtime test is opt-in and stays skipped in normal CI.
- `GET /healthz` is the only unauthenticated route; everything under `/api/*`
  and `/v1/*` requires the bearer token. Keep it that way.
- Preserve the runtime invariants in [`README.md` § Critical invariants](README.md#8-critical-invariants--hard-won-gotchas)
  (oMLX unload semantics, MCP `Accept` headers, tool-call dialects, secret-read
  guards, and the rest).
- Keep generated artifacts, `.harnesses/`, databases, tokens, and credentials
  out of commits (`.gitignore` covers them; keep it that way).
