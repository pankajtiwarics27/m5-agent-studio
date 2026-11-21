# AP-012 — Run disposable capability experiments with receipts

**Status:** in review. It is a fail-closed experiment lab: **not activated**,
**not wired into any production caller**, **not executing anything by itself**,
and **not a production guarantee**. The lab decides whether a trial may run and
what must be recorded; a caller-supplied `TrialRunner` does the executing. Under
the current repository state the lab **refuses every trial**, because the host
inventory in `product/M5_Autonomous_Production_Operating_Envelope.json` is
`not_measured`. That is the intended behaviour, not a defect.

## Story and owner scope

AP-012 (Phase P2) delivers the story themed "permit broad safe experimentation
only inside a qualified, explicitly granted lab": a disposable environment with a
lifecycle, pinned install and artifact receipts, and quarantine/revoke/cleanup
paths. Its dependencies AP-007, AP-010 and AP-011 are done; its decision
dependencies U02, U04 and U06 are resolved. Owner approval is recorded in the AP
ledger's `authorization_record`, together with the `qualification_decision` this
session was delegated to make.

The story's defining constraint is its subject: the **qualified, explicitly
granted lab**. Nothing in the module grants capability, promotes a trial result
into policy, or substitutes the development sandbox for the owner's host.

### The qualification decision, and why the gate is closed

U02 (the owner's 2026-11-17 statement about the host) is `resolved` as an *owner
statement*. The operating envelope records it as **explicitly not a measurement**,
and `measured_inventory.status` is `not_measured`. AC5 says a trial is blocked
when qualification is *unavailable*, not merely when it is negative, so reading
that statement as qualification would be a fabrication.

Two shortcuts were considered and deliberately rejected:

1. Treating U02's statement as qualification — it is not a measurement, and the
   envelope says so in as many words.
2. A per-trial waiver for unmeasured hosts — it would let an unmeasured host run
   exactly the experiments AC5 forbids, which makes the criterion vacuous.

So the lab is built, granted, tested and reviewable, and its admission gate
refuses. `QualificationState` has three values — `not_measured`,
`observed_unqualified`, `qualified` — and only `qualified` admits a trial. A
`qualified` host must name both a qualification record *and* a host fingerprint;
`not_measured` forbids observation fields, so a placeholder cannot masquerade as
a measurement. When an operator runs the collection recipe from
`docs/autonomous-production-operating-envelope.md` and records a `qualified`
inventory, the gate opens with **no code change** — that is the whole design.

## What the module decides

- **Grants precede work (AC1).** `admit` requires `allow_install` **and**
  `allow_code_execution`; an expired grant admits nothing; all booleans default
  to `False`. A network grant is valid only if it names its hosts, and named
  hosts without a network grant are rejected as contradictory. A trial is never
  admitted on the strength of an experiment id alone.
- **Untrusted candidates get no host reach (AC2).** `TrialSpec` refuses to
  construct with host mounts, privileged runtime sockets, production credentials,
  or credential-shaped environment keys. Mounts are checked against both the
  sensitive roots and their sub-paths, so `/etc/ssl/private` fails as surely as
  `/etc`. Containment is a construction-time refusal, not a runtime hope.
- **Uninspected surface blocks the trial (AC3).** `inspect_candidate` walks
  install hooks, custom loaders and binaries and returns findings; every item not
  present in the inspected set is a **blocking** finding, and a missing or
  malformed digest is a blocking provenance finding. `admit_with_inspection`
  performs the inspection and admits only when nothing blocks.
- **Every trial leaves a receipt (AC4).** `TrialReceipt` records exact versions,
  commands, effects, checks, cost, the findings that admitted it, and the cleanup
  outcome. `completed` requires `cleanup.succeeded`; a trial that ran but failed
  to clean up is `failed`; a crashing runner is a `failed` receipt carrying the
  error, not an unhandled exception. The lab removes its own scratch directory
  and records that it did.
- **Failure and unavailable qualification both block (AC5).** Refusals are
  receipts with `admission: refused` and a reason, not exceptions — a refusal is
  a decision that must be auditable. There is no host fallback: no parameter on
  any type offers one, and a test asserts the absence of the usual names.
- **Success is task-local unless an owner says otherwise (AC6).** A `completed`
  trial is not a promotion: `Lab.promotions` stays empty and every receipt
  carries `grants_global_capability: false`. `record_promotion` accepts only a
  completed trial and requires `owner_approved`, and the record it produces is
  scoped `task-local`.

## Cleanup, quarantine and revoke

Quarantine writes a marker that cites the receipt digest, so a quarantined trial
is traceable back to what it did; quarantining a trial with no receipt is
refused. `render_receipts` emits sorted-key JSON with a trailing newline and a
`lab_grants` block that is `false` on every entry, so two runs over the same
work produce byte-identical output and each receipt has a stable digest.
Credential-shaped fields are redacted on the way out, which covers whatever a
caller stuffed into a mapping rather than only the fields this module populates.

## What this story does not decide

- **It does not execute, install or download anything.** Execution arrives
  through the injected `TrialRunner` protocol; `run` and `cleanup` are the
  caller's code. Constructing a `Lab` without a runner and asking it to run is a
  configuration error, and says so.
- **It does not qualify the host, and it does not claim the host is qualified.**
  Qualification is an operator act with a record and a fingerprint. No sandbox
  result is substituted for it.
- **It does not prove isolation.** The module enforces the *specification* of
  containment — it refuses forbidden mounts, sockets and credentials — but a
  spec that passes is not evidence that the runner honoured it. That is a
  `does_not_prove` line on every receipt.
- **It does not promote, activate or wire a caller.** Promotions need owner
  approval and stay task-local; `runtime_activation_authorized` remains `false`;
  no production path imports this module.

## Verification

```sh
python -m pytest -q tests/test_autonomous_production_lab.py
python autonomous_production_decisions.py
```

The suite is offline: every trial in it uses an injected fake runner that
installs nothing and starts no process. 58 tests cover AC1–AC6 one-to-one, the
gate's refusal states, containment refusals, receipt integrity, quarantine and
promotion. No installed-runtime qualification was run and no claim is made about
the M5 host; ADR-0040–0045 remain `not-reviewed`.
