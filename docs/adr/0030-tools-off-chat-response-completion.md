---
id: ADR-0030
title: tools-off-chat-response-completion
status: accepted
date: 2026-11-07
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0030: Truthful tools-off Chat completion

## Context

Owner manual test2 reproduced a fresh tools-off conversation in which the model
returned a bare namespaced Gemma tool marker, not an answer. No tool execution
occurred, but the turn was labelled Complete. Omitting native schemas and active
tool instructions preserved execution safety without explicitly telling the
model that tool requests were unavailable or rejecting unusable final output.

## Decision

Add explicit tools-off system guidance. Keep schemas absent, tool dispatch
unreachable and the single-inference tools-off budget unchanged. Do not promise
that a particular model/template will obey instructions.

For tools-off requests only, reject empty/whitespace user-facing output and bare
tool-call-shaped output through the existing SSE `step_clean` then `error` path,
without emitting `done`. Reasoning-only output is not an answer, regardless of
whether its private text contains a marker. Explain that tools were disabled and
no tool calls were executed, rather than falsely implying successful completion.

Recognize supported and unsupported bare call shapes for diagnostics only; do
not expand executable syntax, inspect reasoning as authority, authorize unknown
names, retry, switch provider or dispatch anything. Ordinary text answers and
prose explaining quoted syntax remain valid. Native deltas sent despite absent
schemas remain ignored; a resulting empty answer receives the same failure.
No HTTP route, configuration, authentication/sessionStorage, execution policy,
model/provider-specific behavior or persistent schema is changed.

## Alternatives

- Continuing to show Complete was rejected: a tool request/empty reply is not a
  completed answer, even though the execution boundary correctly refused it.
- Enabling or parsing reasoning into executable calls was rejected: it would
  violate the user's explicit tools-off setting and promote planning to authority.
- Retrying with tools enabled or switching providers was rejected: there is no
  authorization for additional execution/inference or a different provider.
- Rejecting every mention of tool syntax was rejected: explanatory prose and
  ordinary quoted examples should still be possible with tools disabled.

## Tradeoffs

A bare tool-shaped literal without explanatory prose is intentionally treated
as an unusable tools-off answer, even if a user requested that literal. This is
a terminal-state diagnostic, not intent detection or a comprehensive parser for
all conceivable model text. Previously Complete empty/call-only replies now end
in the existing error event; normal answers and tools-on execution are unchanged.
A model can still produce incorrect prose, so mocks do not replace live user QA.

## Review Status

Not reviewed; independent review remains pending. Author tests reproduce the
reported forms in fragmented content/reasoning, verify native/tagged guidance,
mock actual native wire handling, forbid dispatch and replay standalone behavior.
CI and the owner's controlled same-model test2 retry are separate. No human
visual/security acceptance, signed approval or AS-031 verification is asserted.
