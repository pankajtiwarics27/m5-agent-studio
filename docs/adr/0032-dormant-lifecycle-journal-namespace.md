---
id: ADR-0032
title: dormant-lifecycle-journal-namespace
status: proposed
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0032: Owner-scoped private lifecycle journal

## Context

LC-02 needs one durable authority across sessions/workspaces, but the served
application's SQLite database can be selected independently. A coordinator tied
to each workspace database would create multiple independent residency guards.
Existing process-local admission is insufficient for crash/late-operation state.
ADR-0031 proposes the authority boundary; its decision/review status is unchanged.

## Decision

Propose an explicitly opened, owner-private machine namespace with fixed lock
and SQLite journal names. Use one POSIX nonblocking process lock and a durable
owner epoch for supported cooperating processes. All future app instances must
resolve the same owner namespace; neither different directories nor arbitrary
external clients are magically coordinated. Unsupported locking fails closed.

The initial LC-02 proposal uses application-tagged private schema v1 for residency, queued requests,
request leases, operation intents and metadata-only sequenced journal entries.
Reuse `runtime_persistence` short synchronous transactions and failure discipline,
not a second ad hoc connection lifetime. Keep Studio's active schema v3 unchanged.
New private DDL commits atomically. Existing unknown/future/corrupt/incomplete
journals fail closed without recreation or downgrade; later populated-schema
upgrades need reviewed backup/migration. Store references, not prompts or secrets.

Every registration/restart requires reconciliation before new work. Persist an
intent before exposing it; never auto-replay an unresolved physical action.
Request completion does not clear residency. Scope evidence to owner/generation/
operation/phase/clock, but do not interpret this logical scoping as physical
cancellation, memory headroom or authority to control a native runtime.

This is dormant implementation for fake-only verification. No startup/config/API
surface, active database migration or native control is enabled by the proposal.
See the [kernel contract](../model-lifecycle-coordinator.md) for the precise trust
boundary, default queue/freshness policy, storage behavior and release gates.

## Alternatives

- Add tables to every selected application database: risks independent machine
  authorities unless a separate machine namespace is added anyway. Revisit at
  integration if the application establishes a single canonical owner database.
- Use only SQLite transactions as process ownership: serializes writes but does
  not establish one long-lived effect runner while I/O occurs outside transactions.
- Use only a filesystem lock: loses queue, residency, uncertain effects and epochs
  after process death; releasing a lock is not proof native memory was released.
- Automatically repair/recreate unknown state: could erase a residency hold or
  cause a late load to collide with a newly admitted target.
- Hold a write transaction during runtime I/O: violates current persistence
  discipline and risks lock contention/deadlock while awaiting network/human work.

## Tradeoffs

A separate explicit namespace avoids changing active app storage before native
integration and gives a small auditable failure boundary. It adds another local
metadata file, owner-path/permission requirements and retention/migration work.
Private directories/locks are not an isolation boundary against a malicious owner
or all native clients. Only Linux fake/process tests have run; native macOS engine
qualification and non-POSIX support are not established.

Positive evidence fields are trusted-consumer attestations, not authenticated
native facts. Native qualification/admission and server-only provenance must be
implemented before consuming intents. Arbitrary independent namespaces cannot
be advertised as a machine-wide singleton. These limits block activation, not
justify weakening permissions or adding a force-clear checkbox.

## Review Status

Not reviewed. Status is proposed, not accepted. Owner authorization to develop
LC-02 does not constitute architectural acceptance or independent security
signoff. No historical roadmap, signed-review, native-runtime or hardware-test
acceptance state is changed. Test8 remains paused.

## LC-03 follow-up proposal

[Proposed ADR-0033](0033-dormant-shared-memory-accounting.md) extends new dormant
journals to v2 for model/action memory reservations. Existing v1 journals are
preserved and refused; no populated migration or architectural acceptance is
implied. The original proposal and its not-reviewed status remain unchanged.
