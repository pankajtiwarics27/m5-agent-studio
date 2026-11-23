# AP-030 — Optional guest-desktop capability

**Status:** delivered and merged (PR #353, `618537f`; story recorded `done` by
its completion record); **offline and advisory only, and the adapter ships
disabled.** It **launches no virtual machine, desktop session,
service or provider**, drives nothing on the host, and wires no production
caller. It is **not activated** and **not qualified**.

The objective is the constraint: *enable GUI-only tasks only when simpler APIs
and browser workers are insufficient.* The failure mode this module exists to
prevent is an optional desktop adapter that quietly becomes the default — so
every gate below defaults to "no", and the one thing it will not do is make an
unqualified adapter look usable.

## Desktop control is justified per task, not per project (AC1)

`DesktopJustification` requires recorded attempts against the simpler
capabilities — `http_api`, `mcp_tool`, `browser_worker`, `cli` — and each failed
attempt must say *why that failure makes desktop control necessary*. Two shapes
are refused outright:

- a capability claimed insufficient that was **never attempted**;
- a capability that **was attempted and was sufficient**, because that defeats
  the justification rather than supporting it.

"This project is a design tool" is not a reason. A named task with recorded
failures is.

## A platform is qualified only by observation (AC2)

`PlatformProfile` must state every resource dimension (vcpus, memory, disk,
display width/height) and carries a boolean `observed` — a claim phrased as a
string is refused. `QualificationRecord` has three honest states:

| State | Meaning | Consequences |
| --- | --- | --- |
| `not_measured` | nothing was run | refuses to carry a profile, checks, verifier or timestamp |
| `observed_unqualified` | numbers exist, checks failed | must name the failing check |
| `qualified` | observed, every check passed, named verifier | the only admitting state |

A `qualified` record cannot carry failed checks, cannot omit a verifier, and
evidence for one profile **does not transfer** to another. That last check is
why `evaluate_platform_qualification` compares profile identities rather than
trusting the record in isolation.

## Host surfaces stay outside guest authority (AC3, AC4)

Six host surfaces — filesystem, accounts, clipboard, desktop input, screenshots,
network — are denied by default. A grant requires an explicit scope; there is no
bundle deal, so granting screenshots says nothing about the clipboard. Host
credentials are never shared with a guest: `credentials_shared=True` is refused,
as is any grant asking for an unredacted or writable clipboard/screenshot
surface.

Shared folders are confined to `~/M5_AP_Workspace`, refuse secret filenames
(from `sandbox_boundary.SECRET_NAMES`), refuse `.git` and traversal, and may not
overlap each other. Guest networking defaults to `none`, supports only a
credential-free allowlist, and never bridges the host network. Screenshots land
under the workspace root, are redacted through `history_privacy.redact_text`
before storage, and carry their own size cap.

## A guest failure never falls back to the host (AC5)

`GuestFailure` has no settable `host_fallback_available` — the property is
hard-coded false, and any `host_actions_taken > 0` raises
`HostFallbackRefusedError`. An uncontained failure is refused as an incident
rather than reported as a result. The `next_step` it publishes is "report the
failure to the operator; do not retry on the host".

## Maturity is published, and the adapter stays disabled (AC6)

`GuestDesktopAdapter` has **no executor** — there is no method to run a task,
because this story implements no driver. `enabled` is derived, never asserted:

```
enabled = is_qualified and maturity == "qualified" and enabled_flag
```

An unqualified adapter cannot be enabled, cannot claim `qualified` maturity, and
must publish its limitations. `default_disabled_adapter()` is what this story
ships: present, described, `not_qualified`, and honest about why — **no guest
desktop was launched or observed in this environment**, so no platform is
qualified and no GUI-only task is demonstrated to need desktop control.

`BakeOffReport` compares drivers per use case; an empty report reports
`measured: false` and recommends nothing rather than picking a winner from no
runs. Where it does recommend, it ranks completed drivers by fewest host
interventions, then latency.

## What this story does not decide

- **It does not implement a driver.** There is no VM launch, no display
  server, no input injection. `sandbox_boundary`, `process_supervision` and
  `mcp_client` remain the execution seams.
- **It does not qualify any platform.** Qualification needs an observed guest
  and a named verifier; this environment has neither.
- **It does not measure a bake-off.** Every `BakeOffEntry` is caller-supplied.
- **It does not prove a GUI-only task needs desktop control.** That requires
  running the alternatives and recording them.

## Reviewability

`render_capability` is deterministic sorted-key JSON with a trailing newline, a
`desktop_rules` block stating every boundary (no desktop without a task
justification, no platform admitted without observation, no implicit host
surfaces, no shared host credentials, no unredacted capture, no host fallback, no
adapter enabled without qualification) and a three-line `does_not_prove` block.

## Verification

```sh
python -m pytest -q tests/test_guest_desktop.py
```

81 tests, fully offline. Every platform fact, grant and bake-off entry is
synthetic; none is presented as a measurement of a real guest. The adapter's
`not_qualified` state is a statement about **this environment**, not a test
result about a platform that was never booted.
