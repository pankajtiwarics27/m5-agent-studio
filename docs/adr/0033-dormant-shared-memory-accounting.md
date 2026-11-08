---
id: ADR-0033
title: dormant-shared-memory-accounting
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0033: Shared model/action reservation accounting

## Context

LC-02 serializes residency but does not prove memory fit. Existing model policy
and execution-profile action pools can each fit independently while their sum
exceeds a shared-memory machine. Process-local lease release also cannot prove
native or subprocess allocations ended after a disconnect or process crash.

## Decision

Propose dormant shared reservations in LC-02's owner-private journal/lock,
not another independently sized pool. Schema v2 adds memory policy/reservation
tables for one declared physical domain. All current held bounds survive crashes.
Initial v2 creation is atomic; prior private v1 is preserved and refused. No
populated upgrade/backup implementation exists. Reviewed migration/rollback is
required before activation against v1 state; deleting it is not a migration.
Studio's active schema v3 and public configuration remain unchanged.

Reserve exact model/profile component upper bounds and explicitly qualified
action bounds tied to the existing footprint digest. Footprint defaults alone
cannot qualify actual tool/process peak memory. Keep slot/barrier/policy/grant
checks separate and mandatory. Preserve warm model bounds across turns, retain
unknown effects, and require terminal physical release rather than finally/cancel
release. Reserve action growth before allocating; never implicitly resize context.

Compare total commitments to an explicit shared limit, and only unmaterialized
commitments plus new demand/safety reserve to usable physical headroom. Credit
already measured allocations only with complete, non-overlapping same-domain
attribution. Unknown capacity/pressure/provenance fails closed. Persist unsafe
observation pauses, fence sample revisions/epochs/clocks, and never replay adopted
work. All supplied evidence is trusted consumer attestation, not self-authenticating
native proof or execution permission.

## Alternatives

- Independent model and action pools: can spend the same physical RAM twice.
- Add all reservations to physical used bytes: counts already allocated tracked
  memory twice and incorrectly rejects otherwise fitting work.
- Subtract all reserved peaks from physical used bytes: grants credit for work
  not yet materialized and can admit dangerous future growth.
- Use artifact size / process RSS sums as peak/physical attribution: omits KV,
  workspace, helpers, load transients, or counts shared pages incorrectly.
- Release on cancellation/timeout/process exit: ignores late native loads and
  surviving subprocesses; epochs cannot cancel physical allocations.
- Wire served paths immediately: would activate unqualified collectors, bounds,
  native control and incomplete request-fit/handoff paths.

## Tradeoffs

Conservative load-transient retention and strict unknown-bound blocking can
reduce admitted work. No model quality, context or native execution settings are
changed to obtain a fit. A declared bound is not enforcement: actual collectors,
installed profiles, tool/process limits, topology and external-client assumptions
remain qualification gates. One owner namespace does not control arbitrary
clients/UIDs/roots. Only fake/process tests run; no installed-engine safety is
certified. The internal extension reuses LC-02 private transaction helpers and
binding validation; this is not a public transport/protocol contract.

## Review Status

Proposed and not reviewed. Development authorization and passing offline/CI tests
do not accept this decision, establish independent security signoff, or resume
hardware test8. See the [accounting contract](../shared-memory-admission.md) for
precise evidence semantics, storage limits and activation gates.
