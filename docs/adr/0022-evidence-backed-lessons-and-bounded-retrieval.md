---
id: ADR-0022
title: evidence-backed-lessons-and-bounded-retrieval
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0022: Evidence-backed project lessons, staleness invalidation, and bounded decision-boundary retrieval

## Context

`M5-P6-01` (`dependencies: M5-P5-03`) is the first owner-authorized optional Phase `P6`
story in the M5 2.0 roadmap. Across repeated coding tasks inside a single repository,
agents frequently rediscover non-obvious repository conventions (domain sentinels,
rounding policies, module invariants) only after failing a first verification pass and
burning extra tool calls and context tokens on replanning. At the same time, naive
cross-session "memory" introduces four concrete failure modes documented in the
M5 2.0 anti-patterns (`AS-033`, `AS-037`, `B6`, `B10`):

1. **Unverified hallucinated advice.** Storing model-generated takeaways from failed or
   unverified turns turns transient mistakes into persistent prompt pollution.
2. **Cross-project privacy leakage.** A global lesson pool shared across repositories
   can leak private file paths, business invariants, or credentials from one local
   checkout into another.
3. **Silent staleness.** Guidance that was true at one revision becomes actively harmful
   when the anchored source files change, are deleted, or are replaced by symlinks, or
   when security generations or artifact lineage entries are invalidated.
4. **Unbounded prompt growth.** Injecting lessons on every inner step or without a hard
   token budget degrades local M5 latency and KV-cache efficiency, violating the Phase
   `P6` rule that optional subsystems must earn activation through measured improvement
   over baseline.

## Decision

We introduce `evidence_lessons.py` (stdlib-only, `_el_`/`_EL_` private namespace,
inlined into `studio.py` and covered by `tests/test_durable_state_contract.py`), wire
it into the harness turn boundary and `/api/harness/lessons*` routes in `app.py`, and
gate automatic retrieval behind measured A/B coding evaluation:

1. **Provenance, secret, and file-anchor promotion gates (`promote_lesson`).**
   - Candidate lessons are accepted only when `LessonProvenance` proves `verified=True`,
     carries a valid 64-hex `verification_spec_sha256` with `passed_checks >= 1`, uses
     an allowlisted trust tier (`verified_task` or `trusted`) and non-secret
     confidentiality (`internal` or `public`), and validates any optional
     `lineage_evidence_id` against `artifact_lineage.ArtifactRegistry`.
   - Promotion scans `title`, `guidance`, and `tags` against secret token patterns
     (`sk-...`, `ghp_...`, bearer headers, API key assignments, PEM private keys, and
     caller-supplied secret values) and Unix absolute host paths, rejecting violations
     with `LessonPromotionRejectedError`.
   - Every lesson must anchor to at least one workspace-confined regular file opened
     with `O_NOFOLLOW`, recording its exact SHA-256 digest (`anchored_files`) and
     capping individual lesson size to `MAX_SINGLE_LESSON_TOKENS` (`110`).
2. **Canonical per-project scope isolation (`derive_project_scope_id`).**
   - Every lesson is partitioned under an opaque `proj-...` digest of the canonical
     owning repository root (resolving harness worktrees under
     `.harnesses/worktrees/...` back to their parent workspace root while isolating
     distinct repositories). Retrieval, listing, invalidation, and purging operate
     exclusively within a single caller-specified project scope; cross-project reads are
     structurally absent.
3. **Automatic staleness and regression invalidation (`revalidate_project_lessons`).**
   - Before listing or retrieving lessons, `EvidenceLessonStore` re-hashes each
     anchored file with `O_NOFOLLOW` confinement, checks `current_security_generation`
     against `provenance.security_generation`, and checks optional
     `ArtifactRegistry` freshness. Any file modification, deletion, symlink swap,
     security-generation advance, or lineage invalidation transitions the lesson to
     `stale` so it is never injected.
   - `record_retrieval_outcome` tracks post-retrieval task verification verdicts and
     automatically invalidates any lesson that correlates with
     `MAX_CONSECUTIVE_VERIFICATION_FAILURES` (`2`) consecutive failed verifications.
4. **Decision-boundary retrieval, hard token ceilings, and optional disablement.**
   - Retrieval is restricted to explicit decision boundaries (`turn_plan_start`,
     `verification_replan`, `handoff_switch`) and never runs on intermediate tool steps.
   - Rendered prompts are framed as untrusted repository hints requiring workspace
     verification and capped at `DEFAULT_LESSON_TOKEN_CEILING` (`160` tokens, hard max
     `ABSOLUTE_MAX_LESSON_TOKEN_CEILING = 256`, max `3` lessons).
   - Automatic retrieval fails closed (`ab_gate_open=False`) until
     `run_lessons_ab_evaluation_async` (`POST /api/harness/lessons/evaluate`) records
     `measured_improvement_over_baseline == True` with zero cross-project leaks and zero
     stale injections, and operators can disable the subsystem at any time via
     `STUDIO_ENABLE_EVIDENCE_LESSONS=0`, `POST /api/harness/lessons/config`
     (`enabled=false`), or per-turn `enable_lessons=false`.

## Alternatives

1. **Global cross-repository vector or FTS memory store.**
   Rejected: sharing retrieved context across distinct local repositories risks
   cross-project privacy leakage and adds unmeasured embedding/index overhead when a
   canonical per-project scope and anchored-file keyword/path scoring suffice.
2. **Promoting lessons automatically from unverified model summaries.**
   Rejected: without an authoritative `TaskVerificationSpec` digest and passing test
   verdict, transient model rationalizations could poison future turns.
3. **Injecting lessons on every step of the harness tool loop.**
   Rejected: repeating lesson blocks mid-loop inflates prompt token usage without
   improving planning; restricting injection to `turn_plan_start`,
   `verification_replan`, and `handoff_switch` preserves hard token ceilings.

## Tradeoffs

- **Cold-start fail-closed gate.** Because `ab_gate_open` defaults to `false` until
  measured A/B evaluation passes (or an operator explicitly configures the gate), fresh
  stores do not inject lessons silently without benchmark evidence.
- **Strict file-hash invalidation.** Formatting or refactoring an anchored file
  invalidates lessons tied to its exact SHA-256 until a subsequent verified task
  re-promotes updated guidance. Preferring false-stale eviction over stale-advice
  injection preserves coding correctness.

## Review Status

Accepted architecture decision (`status: accepted`); independent human review is **not reviewed** (`review_status: not-reviewed`). Code and test verification are covered by [`evidence_lessons.py`](../../evidence_lessons.py), [`app.py`](../../app.py), [`tests/test_durable_state_contract.py`](../../tests/test_durable_state_contract.py), and [`tests/test_evidence_lessons.py`](../../tests/test_evidence_lessons.py).
