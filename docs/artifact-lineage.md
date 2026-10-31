# Artifact lineage, provenance and retention (`M5-P5-02`)

Design record:
[`docs/adr/0019-artifact-lineage-provenance-and-selective-invalidation.md`](adr/0019-artifact-lineage-provenance-and-selective-invalidation.md).

Module: `artifact_lineage.py` (inlined into the generated `studio.py`). Store:
`$HARNESS_STORE_DIR/artifacts/state.json`, one of the durable JSON sidecar stores in
[`docs/storage-migrations.md`](storage-migrations.md) and covered by
`tests/test_durable_state_contract.py`.

## What is true in this build

| Capability | Status |
| --- | --- |
| Content deduplicated by SHA-256 | implemented, tested |
| One provenance entry per sighting; histories never merged | implemented, tested |
| Effective trust = weakest entry; confidentiality = strictest | implemented, tested |
| Operation checks are deny-wins across entries | implemented, tested |
| Unknown content is trusted by nothing | implemented, tested |
| Invalidation recorded (not deleted) and cascaded to derivatives | implemented, tested |
| Evidence refused when a source digest, a source's absence, its subject, or the security generation changed | implemented, tested |
| Retention keeps active-task artifacts, pins, valid evidence, secrets, grace window | implemented, tested |
| Frozen quarantine bytes protected from retention (pin **and** re-derivation) | implemented, tested |
| Completion refused when the security generation moved mid-verification | implemented, tested, wired in `app.py` |
| General trust calculus / proof of safety | **not claimed** (out of scope by design) |

## How trust answers are computed

Given content `C` seen from a trusted baseline and from an untrusted tool:

```
entries(C)             -> [provenance-from-baseline(trust=trusted),
                           provenance-from-tool(trust=untrusted)]
effective_trust(C)     -> "untrusted"        # the weakest sighting decides
check_operation(C, "prompt") -> denied if any entry denies it, or allows a narrower set
```

Recording `C` again from a trusted source does **not** change the first answer. That is
the whole point: deduplication is about bytes, trust is about history.

## Evidence freshness

```python
record = registry.record_evidence(
    kind="verification", subject="patch",
    source_files={"app.py": current_digest}, security_generation=4,
)
verdict = registry.accept_evidence(
    record.evidence_id, current_files={"app.py": new_digest}, security_generation=4,
)
# verdict.accepted is False, verdict.code == "source_changed", and the record itself is
# now `stale` — re-checking later cannot make it valid again.
```

`accept_evidence` refuses on `source_changed`, `source_missing`, `subject_invalidated`,
`security_generation_changed`, or a previous refusal (`already_stale`). A `None`
generation means *not checked*: the caller is told nothing about security stability rather
than being told it is stable.

## Retention

```python
plan = registry.plan_retention(
    retain_seconds=7 * 24 * 3600,
    active_task_ids=active_task_ids,
    protected_refs=workspace_quarantine.recovery_protection(HARNESS_STORE_DIR),
)
result = registry.apply_retention(plan)   # deletes exactly plan.evictable
```

Every survivor appears in `plan.protected` with a reason (`pinned by …`, `active task …
still references it`, `unreferenced …`). `apply_retention` cannot remove anything that is
not in `plan.evictable`, and a protected artifact is never in it.

## Where the live gate is

`workflow_state._verify_task_completion_impl` captures the security generation before
gathering evidence and again before returning a verdict:

```
security_configuration_changed_during_verification
```

`app.py` supplies the provider (`_current_security_generation`, read from
`SecurityIncidentStore.summary()["current_generation_seq"]`). Callers that pass no
provider are not gated, and the report says nothing about the generation rather than
implying stability.

The task-state route also returns a read-only `artifact_lineage` block (counts, revision,
pins) so an operator can see the registry without a new endpoint.

## Known limits

- The registry records lineage; it does not decide whether content is *safe*. Nothing here
  substitutes for the isolation boundary, which is unqualified in this environment.
- Trust labels are recorded by the caller. A call site that mislabels its own output
  records a wrong history faithfully; the module cannot audit its own callers.
- The generation gate covers the verification window only. A configuration change between
  completion and later use of the evidence is not detected by this path.
- Retention is a plan/apply API, not a background job; nothing schedules it today.
