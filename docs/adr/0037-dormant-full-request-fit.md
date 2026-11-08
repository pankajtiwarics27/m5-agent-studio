---
id: ADR-0037
title: dormant-full-request-fit
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0037: Dormant immutable full-request counting and fit authority

## Context

The paused test8 request exceeded allocated context (7,326/4,096). Character
trimming, a messages-only count or a model's metadata maximum cannot establish
fit of tools, security, history, templates and media with output/reasoning reserve.
Tokenization can itself load a model; timeout does not prove physical release.
Installed matching counter/rendering parity has not been established for any of
the three local runtimes. Dormant bridges need an explicit dispatch refusal until
full-request evidence exists, without rejecting long streams on a preflight TTL.

## Decision

Propose pure preparation of immutable **final provider bytes** and an injected,
default-deny post-residency service. Bind bytes/digest, lease/owner/generation,
actual approved context, tokenizer/renderer/native profile and output accounting.
Count only under an already-held warm model/memory lease with installed-scoped,
non-loading bounded counter qualification and fresh before/after evidence.
Commit a durable no-replay count marker before counter I/O; uncertain effects
retain holds. Issue short-lived bounded in-memory fit tickets and require a
separate exact-byte fit callback at dormant Ollama/LM Studio inference send.
Do not recheck the preflight ticket TTL at stream completion or replace existing
native/policy/memory/terminal gates. Preserve essential input and refuse overflow.

Do not install counters/producers, expose routes/settings, activate adapters or
wire served workflows here. Workflow-origin fixtures prove a reusable API only;
LC-08 owns complete app/canonical/standalone consumption and removal of legacy
bypasses. LC-09 owns recovery orchestration; no automatic count replay or memory
settlement is authorized. Existing schema-2 journal events need no migration.

## Alternatives

- Character estimates/tail trimming, lazy tools, automatic context enlargement:
  do not preserve the full request or approved memory/quality profile.
- Tokenizer route before admission: may trigger hidden model load/overlap.
- Count messages only or add template estimates twice: not full-render parity.
- Trust installed SDK/native API names: not executable/version/profile evidence.
- Use runtime overflow rejection or post-generation usage as proof: too late for
  strict readiness, though strict native input controls remain defense-in-depth.
- Activate partially wired app routes: leaves bypasses and misstates readiness.

## Tradeoffs

Conservative refusal is intentional while production qualification is absent.
The preparer cannot prove that upstream assembly did not already omit content;
final all-path consumers need separate inclusion evidence. Positive fake counter
receipts are trusted test assumptions, not proof of any installed tokenizer.
Strict JSON/wire/media support is deliberately narrower than generic provider
APIs. Count uncertainty may retain a request/memory hold until explicit recovery.
Ephemeral tickets disappear on restart while durable count markers prevent replay.
No new dependency, runtime configuration, schema or native engine change is made.
Cooperating process locks do not isolate arbitrary GUI/root/same-UID clients.

## Review Status

Proposed, not reviewed. Author-run tests, CI or merging dormant infrastructure do
not constitute architecture/security acceptance, installed qualification, native
activation or permission to resume test8. ADR-0031 through ADR-0036 and independent
security-signoff records are unchanged. Exact source limits and validation are in
the [LC-07 contract](../request-fit-admission.md).
