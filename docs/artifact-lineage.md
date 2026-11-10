# Artifact lineage, provenance, retention and versioned identity (`M5-P5-02`, AP-006)

Design records:
[`docs/adr/0019-artifact-lineage-provenance-and-selective-invalidation.md`](adr/0019-artifact-lineage-provenance-and-selective-invalidation.md)
(provenance, invalidation, retention) and
[`docs/adr/0040-artifact-identity-completeness-and-confined-references.md`](adr/0040-artifact-identity-completeness-and-confined-references.md)
(AP-006: versioned identity, completeness, confined references, export/delete).

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
| Versioned identity: bytes + producer + configuration + input versions (`identity_sha256`) | implemented, tested (AP-006) |
| Declared required checks; `complete` only when the newest outcome of each passed | implemented, tested (AP-006) |
| Input-version change invalidates only its consumers and their derivatives | implemented, tested (AP-006) |
| Confined file references refuse traversal, absolute escape, symlinked components, directories, digest mismatch | implemented, tested (AP-006) |
| Preview refused for unknown, invalidated, untrusted, secret and changed content | implemented, tested (AP-006) |
| Scoped export (confidentiality gate, recorded history) and deletion tombstones that keep reference history | implemented, tested (AP-006) |
| Populated `schema_version` 1 store read as-is and rewritten as 2 | implemented, tested (AP-006) |
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

## Artifact versions and identities (AP-006)

A content digest answers *what the bytes are*. It cannot answer *which version this is*: the
same bytes can be produced by two configurations from two different input versions. A
provenance entry therefore also carries the producer, its version, a digest of its
configuration, and the input versions it consumed:

```python
entry = registry.record_artifact(
    digest,
    source_kind="pipeline", source_ref="run-14", trust="trusted",
    producer="workflow:site-builder", producer_version="1.4.0",
    config_sha256=config_digest,
    inputs={"src/app.py": app_version, "src/theme.css": theme_version},
    required_checks=("render", "lint"),
)
entry.identity_sha256   # recomputable, and stored beside the entry
```

`identity_sha256` is the SHA-256 of canonical JSON over the content digest plus those
fields, so a configuration or input change is a *different version* even when the bytes
coincide. `registry.record_file_artifact(root, "site/index.html", ...)` does the same for a
file, with the digest computed from confined bytes instead of accepted from the caller.

## Completeness: partial output cannot look accepted

```python
registry.artifact_status(digest).state      # partial until every declared check passed
registry.require_complete(digest)           # raises ArtifactIncompleteError instead
registry.record_check(digest, "render", outcome="passed", detail="11 pages")
```

`artifact_status` returns `complete` only when the artifact declares required checks and the
**newest** recorded outcome of every one of them is `passed`. It returns `partial` with
`missing_checks`/`failed_checks` otherwise, `partial` with `checks_not_declared` when no
check was ever declared, `invalidated` after invalidation, and `unknown` for unrecorded
content. Check records are append-only, so re-running a check after a fix clears the
failure while the failure stays in `checks_for` — and a check recorded *after* invalidation
cannot make an invalidated artifact complete again.

## Selective invalidation by input version

```python
result = registry.invalidate_for_inputs({"src/app.py": new_app_version}, reason="source edited")
result.invalidated        # the artifacts that consumed the old version, plus their derivatives
```

Only entries whose recorded version for a changed input differs are affected; artifacts
that never consumed it — or that already recorded the new version — keep working, and
nothing is deleted. The existing `invalidate(digest, ...)` cascade is reused, so derivatives
are invalidated transitively and their evidence goes stale.

## Confined references and safe previews

```python
confined = artifact_lineage.confine_reference(workspace_root, "site/index.html")
verdict  = registry.check_preview(digest, workspace_root, "site/index.html")
verdict.allowed, verdict.code, verdict.complete, verdict.reference.relative_path
```

`confine_reference` walks the reference one component at a time with `lstat`, so `..`, an
absolute path outside the root, a symlinked file *or* a symlinked directory component, a
directory, and a digest mismatch are all refused rather than resolved. `check_preview`
requires the artifact to be known, current (not invalidated), not from an untrusted source,
not secret unless `allow_secret=True`, permitted by the deny-wins operation verdict, and
still hashing to the recorded identity. A preview of a partial artifact is allowed only
with `complete=False`, so a UI can label it instead of implying the work is finished. A
preview is a *read* of someone else's bytes; it is not an execution permission.

## Export and deletion

```python
export = registry.export_artifact(digest, requested_by="task-7")   # refuses secret content
deletion = registry.delete_artifact(digest, reason="owner withdrew the draft", confirmed=True)
registry.tombstones()[digest]["preserved_references"]
```

`export_artifact` refuses invalidated artifacts, refuses secret content without an explicit
allowance, marks a partial export `complete=False`, and appends the envelope to the export
history. `delete_artifact` needs explicit confirmation and a reason, and refuses an artifact
that is pinned, that valid evidence still rests on, or that a current artifact was derived
from. What it leaves behind is a tombstone: identity, reason, timestamp and the scoped
reference history (source kind, task id, trust, confidentiality, declared checks) — never
the content, and never the free-form `source_ref`, which can carry a path or a URL. The
check history survives too, so an audit can still say what was claimed about the artifact.

## Schema v2 migration

The sidecar is `schema_version` 2 since AP-006. A populated v1 document is read as-is — every
v2 addition is optional and defaults to empty — and the next atomic store write stamps 2
with every blob entry, evidence record and pin preserved; there is no rewrite pass, no
backup file and no reset. A v1 binary refuses a v2 store loudly instead of silently dropping
the identity fields, so a downgrade is a deliberate act: re-apply the newer module, or
restore a snapshot taken before the upgrade. A store whose version is newer than this build
supports is refused.

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
  substitutes for the isolation boundary, which is qualified on the target host only
  ([record](m5-2-0-sandbox-isolation-evidence.md)); an environment without its own run
  is unqualified.
- Trust labels are recorded by the caller. A call site that mislabels its own output
  records a wrong history faithfully; the module cannot audit its own callers.
- The generation gate covers the verification window only. A configuration change between
  completion and later use of the evidence is not detected by this path.
- Retention is a plan/apply API, not a background job; nothing schedules it today.
- Check outcomes are last-writer-wins per check name, and the producer/configuration
  digests are recorded as the caller states them: the module binds them into the identity
  but cannot audit a caller that describes its own producer inaccurately.
- Confinement is a per-call check, not a reservation: the bytes can change between the
  digest check and the read that follows. The digest re-check narrows that window and turns
  a changed file into a refusal.
- Export, check and tombstone history is append-only and not globally collected; retention
  still collects only artifact blobs, so that history needs its own policy if usage grows.
- AP-006 adds no endpoint and no control: `app.py` continues to expose the read-only
  `artifact_lineage` summary, and the mission UI wiring remains AP-008 scope.
