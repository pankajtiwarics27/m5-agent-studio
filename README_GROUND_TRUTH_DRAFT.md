# M5 Agent Studio

> **Ground-truth README draft — repository verification required.** This document is intentionally conservative. It must replace the repository README only after the autonomous agent inspects the current code, verifies the claims below, and preserves any working setup instructions.

## Current state

M5 Agent Studio is an existing local-first coding-agent project. A prior codebase review reported version **v9.0.1**, a FastAPI application, an Agent Harness, model-routing modules, SQLite persistence, and a vanilla JavaScript UI. **These are reported findings, not independently verified by this document.** Verify them in the current checkout before presenting them as confirmed functionality.

## Verified functionality

**Not yet established from this repository in this review.** The implementation agent must populate this section only with code-path evidence and passing tests. Do not equate the presence of a module with an integrated capability.

## Installation and running

**Not yet verified.** Preserve and test the repository's existing commands; document the actual prerequisites, commands, and outcomes here. Do not invent startup instructions.

## Tests and measurements

**No current repository test execution or hardware measurements were performed for this draft.** Record real pytest, ruff, mypy, dependency-audit results, environment, and exit codes when available. Peak 24 GB Apple Silicon memory and swap behavior are not established.

## Security and limitations

The previous code review reported potentially unverified or bypassable shell isolation, including direct skill subprocess paths and a host-execution environment flag. These findings require validation in the live checkout. Until required isolation guarantees have been demonstrated, affected autonomous execution must fail closed. A Git worktree is not an operating-system sandbox. Do not claim that external actions are exactly-once or that all subprocess descendants can always be terminated.

## Implementation status

The authoritative story-by-story implementation state is maintained in [`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json), once installed into the repository and reconciled with the prior roadmap. All roadmap stories start as `not_started` until verified. Planned capabilities are not current product capabilities.

## README maintenance rule

After every completed story, update this README using only observed and tested behavior. Document regressions and limitations promptly. Include actual commands and outcomes; never promote an intended feature, passing mock, or untested sandbox claim into a production guarantee.
