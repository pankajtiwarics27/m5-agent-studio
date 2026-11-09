# AP-001 — Program reconciliation and production gap map

**Story:** AP-001 • **State:** in progress • **Claimed:** 2026-11-09T14:34:22Z  
**Main base:** `f97d02ef6a23c213eac80986d47fff13c3ee8df9` • **Starting branch head:** `15f754270f7e02a79cf3c6810424803f7d7918cd`

## Authority and claim

The repository owner instructed this session on 2026-11-09, after merging PR
[#273](https://github.com/pankajtiwarics27/m5-agent-studio/pull/273), to start
AP-001. That instruction authorizes the AP-001 reconciliation and program
registration work only. It does not activate a runtime path, authorize a later
AP story, approve an external service, or settle decisions U01–U06.

The session is claimed by `arena/73573f93-m5-agent-studio`. Arena fixes that
branch name for the session, so this AP story continues on the same branch after
the documentation-import PR rather than creating or switching to a differently
named branch. The draft pull request is the remote coordination claim; the JSON
record alone is not treated as atomic coordination.

## Pre-edit blast-radius record

- **Objective and gap:** establish current source truth for AP requirements,
  classify existing capability as reuse, extend, missing, dormant or blocked,
  preserve owning-program boundaries, and register an AP tracking/claim
  convention. Named modules and unit tests do not by themselves prove live
  application integration.
- **Options considered:** (1) create a new control plane, rejected because the
  repository already owns authority, workflow, artifact, routing and lifecycle
  contracts; (2) mark similarly named capabilities complete, rejected because
  AP requires consumer evidence; (3) produce a documentation-only delta map and
  registration record, selected as the smallest AP-001-compliant change.
- **Expected files:** this reconciliation record, `AGENTS.md`,
  `docs/README.md`, and AP-001 tracking in
  `product/M5_Autonomous_Production_Roadmap.json`. Source modules, APIs,
  payloads, UI controls, persistence schemas and generated runtime artifacts
  are outside this story.
- **Pinned tests and generated artifacts:** no pin is to be weakened. Applicable
  documentation, governance, ledger-invariant, commit-message and generated
  artifact drift checks will be executed. `studio.py` and generated UI/runtime
  inventories remain generator-owned and should not change.
- **Authority, data and resources:** no new egress, credential access, private
  data, side effect, provider, model load, spending or concurrent residency is
  introduced. Existing auth, grants, isolation and sequential lifecycle intent
  remain binding.
- **Compatibility and recovery:** documentation and proposed-ledger metadata are
  additive. There is no data migration or feature flag. Rollback is removal of
  the AP-001 registration/reconciliation changes; existing program histories
  and product behavior remain unchanged.
- **Evidence limits:** static callers and offline tests establish contract and
  wiring evidence only. They do not establish installed runtime/sandbox
  qualification, physical model release, visual owner acceptance, independent
  security acceptance, or real media quality.

The source-grounded matrices, ownership findings, registration convention,
acceptance mapping and final handoff will be completed in this AP-001 change
before it is submitted for review.
