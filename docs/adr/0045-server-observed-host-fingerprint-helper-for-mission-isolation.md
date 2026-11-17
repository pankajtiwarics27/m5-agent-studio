---
id: ADR-0045
title: server-observed-host-fingerprint-helper-for-mission-isolation
status: accepted
date: 2026-11-17
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0045: Server-observed host fingerprint helper for mission isolation

## Context

[ADR-0044](0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
made a server-observed host part of the proof for consequential actions of
mission-owned work. It records that no observer existed, so
`IsolationEvidence.from_admission` could only receive `observed_host` from a
test fixture; even a recorded acceptance would not make a dispatch pass the
isolation gate. Its proposed observer was left for the activation wiring.

The owner authorized finishing AP-007 on 2026-11-17 (issue #322), after
recording U06 and accepting AP-007-F2+F3 as the refusal foundation. The AP
ledger claim `AP-007-R1` scopes this remaining slice to the observer seam. It
does not authorize a production caller, activation, or installed-runtime
qualification. The 2026-10-31 M5-P3-01 record remains the only recorded machine
evidence under the owner's explicit waiver of a fresh qualification run (PR
#324).

The qualification suite already defines the five host values and the canonical
digest through `scripts/qualify_environment.py` and
`sandbox_boundary.qualification_host_fingerprint`. The observer must use the
same values and digest recipe or the comparison with the recorded target host
will refuse. It must also fail closed when any reading is unavailable, avoid
hardware-unique identifiers, and remain safe to inline in standalone
`studio.py`.

## Decision

Add `sandbox_boundary.observe_host_fingerprint() -> str`, a call-time-only
observer that reads the same five values as the qualification suite and passes
them to the existing `qualification_host_fingerprint` function:

| Value | Read as |
|---|---|
| `os` | `platform.system()` |
| `machine` | `platform.machine()` |
| `cpu_model` | On Darwin, the `machdep.cpu.brand_string` sysctl value, falling back to `hw.model`; otherwise, the `model name:` line of `/proc/cpuinfo` |
| `cpu_count` | `os.cpu_count()` |
| `total_memory_bytes` | On Darwin, the `hw.memsize` sysctl value; otherwise, `MemTotal` from `/proc/meminfo`, converted from KiB to bytes |

The result is `sha256:` plus 64 lowercase hexadecimal digits, formed by the
unchanged canonical-JSON digest recipe. If any value is missing, malformed or
unreadable, or observation fails for another ordinary exception, the function
returns the empty string. The isolation gate already refuses an empty
`observed_host` with “the host of this dispatch was not observed”; the observer
must not invent a value or allow an unreadable host through.

The readers are private `_sb_` helpers. On Darwin, one shell-free `sysctl`
invocation requests the three named OIDs and has a bounded timeout; on
non-Darwin systems, the CPU and memory readers each read one small `/proc` file.
Reads occur only when the observer is called, not at module import or at rest.
Nothing beyond the five existing values is collected; two machines of the same
model and memory size continue to share a fingerprint. The observer mirrors the
qualification suite instead of importing it, preserving module layering and
compatibility with the standalone studio bundle.

This slice supplies the missing function only. It does not call the observer
from a production dispatch path; a separately authorized server composition
would call it at dispatch time and pass the result as `observed_host` to
`IsolationEvidence.from_admission`. There is no recorded `IsolationAcceptance`,
no production dispatch proof, and no runtime activation. The existing gate
therefore continues to refuse consequential actions of mission-owned work.
No route, endpoint, UI control, table, column, schema version or budget key is
added. Browser-worker and native-lifecycle work remains refused. Installed
runtime qualification is **NOT RUN**.

## Alternatives

- Import `scripts.qualify_environment`: rejected because it violates the
  inlined module's layering and the qualification suite imports higher-level
  application modules. The sibling must remain usable in `studio.py`.
- Use `platform.uname`, a partial platform-specific description, or another
  fingerprint recipe: rejected because the gate compares with the five-value
  canonical digest already recorded by qualification reports; a different
  recipe would fail to match legitimate evidence.
- Collect a serial number, UUID or other unique hardware identifier: rejected.
  ADR-0044 intentionally uses a minimal machine description, and two identical
  machine types remain indistinguishable.
- Observe at import time or return a guessed/default fingerprint on failure:
  rejected because host reads must be call-time only and any incomplete
  observation must make the mission isolation gate refuse.
- Run a separate subprocess for each Darwin value: rejected because one bounded
  shell-free sysctl invocation can read the same three OIDs. Non-Darwin systems
  use `/proc`, as does the qualification suite.
- Add a mission dispatch caller, acceptance record or activation here: rejected
  as outside AP-007-R1's bounded seam. That wiring would add a live caller and
  execution evidence that this host cannot qualify; the owner has not
  authorized runtime activation in this change.

## Tradeoffs

The helper now makes the server's current host description available using the
same digest as the qualification suite. Tests pin equality against the suite's
five-field description on this host, prove each field contributes to the
fingerprint, and cover missing, malformed and unreadable inputs. Tests also
exercise the Darwin-specific reader and timeout behavior with fakes. This does
not constitute a macOS run: the authoring environment is Linux x86_64, and
installed-runtime qualification remains unrun.

The fingerprint is not authenticated and does not distinguish identical
machine types. Those are existing properties of the proof model, not properties
this observer can repair. The function's existence is not evidence that any
production path calls it. Until a separately authorized composition supplies a
server-observed fingerprint and a recorded acceptance is available, mission
isolation remains refused. Rollback is a source revert; no state is migrated.

## Review Status

Not reviewed. No independent security, release or owner approval is implied by
this implementation decision.
