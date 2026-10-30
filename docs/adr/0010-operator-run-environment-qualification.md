---
id: ADR-0010
title: operator-run-environment-qualification
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0010: Environment qualification is operator-run, recorded, and fail-closed

## Context

`M5-P3-01` (sandbox backend qualification) and `M5-P4-01` (measured model
qualification and RAM admission) both need evidence that the evidence environment
cannot produce: a live Docker/Podman runtime for the first, and a 24 GB Apple
Silicon Mac under real model load for the second. `M5-P0-04` had already
recorded the consequence for the sandbox — `isolation_status: unqualified`,
`dependent_execution: blocked (fail closed)`.

Three ways to handle a blocked story exist, and two of them are unsound:

1. mark the story done because the code "should" isolate and the artefacts "should"
   fit in 24 GB;
2. relax the criterion to whatever CI can see, and describe the gap in prose;
3. keep the criterion, and ship a reproducible way for the owner to produce the
   missing evidence on the machine that is supposed to hold the guarantee.

Option 1 fabricates evidence. Option 2 quietly changes the requirement. This
repository's program rules already forbid both, so the remaining question is how
option 3 should look.

## Decision

Environment qualification is a **separate, operator-run artefact** delivered as
`scripts/qualify_environment.py` (library + CLI) and
`scripts/qualify_environment.sh` (output-naming wrapper).

- **One script, two suites.** `--sandbox` covers `M5-P3-01`, `--hardware` covers
  `M5-P4-01`, `--all` is the default. One run produces one record.
- **Every check records a status, never a boolean hope.** `pass`, `fail`, `skip`,
  `blocked` — with a detail string. `skip`/`blocked` on a *required* check makes
  the suite `blocked`; a `fail` makes it `unqualified`; only an all-passing run of
  every required check is `qualified`.
- **The suite qualifies the product's sandbox, not its own.** The probes execute
  through `app._execute_limited` where the application is importable, and a test
  asserts the suite's container flags match `app._sandbox_command`'s hardening
  flags. A drift between suite and product fails CI, not production.
- **Exit codes are part of the interface.** `0` qualified, `1` unqualified, `2`
  could not run. `2` is the honest result on this Linux evidence container and is
  expected there.
- **The record is bound to an identity.** Repository revision, OS build, chip,
  cores, total memory, detected runtime, image, invocation and UTC timestamp are
  written into the JSON; the Markdown rendering carries the same content.
- **Nothing touches the user's project or the network.** Probes run in a
  temporary workspace, in a `--network=none --read-only --pull=never` container;
  `--pressure-gb` and `--model-command` are the only flags that load the machine.
- **The suite is not wired into server startup.** Qualification is an operator
  action. A boot-time memory pressure test would be hostile to a serving machine,
  and a boot-time isolation failure would masquerade as an application fault. The
  documented `ExecStartPre`/Compose hooks exist for operators who want the record
  on every boot; they are opt-in and their failure semantics are spelled out.
- **A blocked story stays blocked.** Tooling existing is not evidence. `M5-P3-01`
  and `M5-P4-01` remain `blocked` in the ledger until a `qualified` record exists,
  and `M5-P4-01` additionally requires `app.py` to consult `resource_admission`
  for its "second simultaneous heavy load rejected under policy" criterion.

## Alternatives

- **Run the suite inside the test suite with a runtime skip.** Rejected: a skipped
  test is not evidence, and CI has no runtime at all — the record would prove
  nothing while looking green.
- **Mock the container runtime and call it qualification.** Rejected: mocks
  validate the mock. The existing mocked checks (`p3.product.argv`,
  `p3.product.no_host_fallback`) are explicitly labelled product-path checks, and
  the suite still reports the container probes as `skip`/`blocked` beside them.
- **Auto-run qualification on every server start.** Rejected for the reasons
  above; the wrapper and the documented hooks give operators the option without
  making it the default.
- **Have the agent write the measurement numbers.** Rejected: this is exactly the
  fabrication the program rules prohibit. The suite prints what it observed and
  nothing else.
- **Fold this into `scripts/isolation_smoke.py`.** Rejected: that script is the
  `M5-P0-04` evidence tool with its own contract and tests. The new suite runs it
  as one aggregated check (`p3.product.isolation_smoke`) instead of duplicating
  or mutating it.

## Tradeoffs

- The two stories stay open across sessions until the owner runs the suite; the
  ledger's `blocked` state is honest but not "progress".
- The suite duplicates a little of `isolation_smoke.py`'s argv knowledge. The
  parity test caps that risk, but the duplication is real.
- Some checks are advisory by construction: `p4.admission_wired` text-inspects
  `app.py` for the admission import. It is marked non-required precisely because
  a source scan is weaker than a behavioural probe, and it is reported rather than
  hidden.
- `p4.machine.fingerprint` encodes the story's 24 GB Apple Silicon target as a
  gate. A qualification run on 16 GB or on Linux reports `blocked` even when all
  the other measurements pass — by design, since the criterion names the machine.
- Probe timeouts mean a heavily loaded machine can report `unqualified` for a
  reason that is not a real regression; the record shows each check's duration so
  a human can tell the difference.

## Review Status

Not reviewed. This record accompanies the operator-run qualification tooling for
`M5-P3-01`/`M5-P4-01` and records the decision to keep both stories blocked until
a qualified record exists. It has not been reviewed by anyone other than the
authoring agent.
