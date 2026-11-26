# AP-032 drill witness — what was actually exercised

`release_qualification.DrillRecord` is a claim. `release_drill_witness.py` is what makes it true: it
performs the rollback, migration and restore drills against a throwaway database and returns records
whose digests were computed from the real bytes. Stdlib only; no network, no clock, no writes outside
its own temporary directory.

This page records the drills **as run on 2026-11-10** against this repository's own
`scripts/db_maintenance.py` and `runtime_persistence.py`. It is evidence of drills, not of a release.

## What was run

```
$ python release_drill_witness.py
rollback   exercised=True  v4 -> restore pre-migration backup -> v1 with the original rows -> v4 again;
                           rollback is reversible, not one-way
migration  exercised=True  schema v1 -> v4; 5 pre-existing rows kept every original column value;
                           13 tables gained columns, including runs: [created_at, mode, title, payload_json]
restore    exercised=True  1f18fe30b77b==1f18fe30b77b  backup, mutate, restore, re-read: identical logical
                           content. File digests differ (0966a8ec4412 vs 67dfd4d02777), which is expected
                           for SQLite and is why the comparison is logical.
```

`tests/test_release_drill_witness.py` (12 tests) runs all three for real — nothing is stubbed, because a
stubbed drill proves nothing about the claim it backs.

## The measurement that changed the design

A SQLite backup restored through the online backup API is **logically identical but not
byte-identical**. Measured on this repository's own `backup_database`/`restore_database` round-trip:

| comparison | result |
|---|---|
| `sha256` of the file bytes | **differs** |
| logical digest over tables, ordered rows and `user_version` | **identical** |

`DrillRecord` originally compared `restored_digest == source_digest` without saying what those digests
were taken over. That is ambiguous in a way that matters: a worker measuring file digests would record
a **failed** restore for a correct restore and block the release; one measuring logical content and
reporting it as a byte match would overclaim.

`DrillRecord` now requires a `comparison` of `byte-identical` or `logical-content`, and refuses a
restore record that names neither:

> "a restore drill must name what its digests are taken over … without it the reader cannot tell a real
> mismatch from the expected difference between two SQLite files holding the same rows"

The witness records `logical-content` and states the differing file digests in its own detail, so the
record cannot be read as a byte match.

## A drill that caught a flaw in the witness itself

The first run failed with *"migration changed pre-existing rows; that is data loss"*. It was wrong: the
repository's real v1→v4 migration is **additive** — it adds `created_at`, `mode`, `title` and
`payload_json` to a legacy `runs` table. Comparing whole rows flags a legitimate migration as data
loss.

The witness now snapshots the columns that existed *before* migrating and compares only those, so it
asserts the thing that matters — *the old values survived* — and reports the added columns separately
rather than hiding them. Recorded here because the first measurement being wrong is the reason the
second one is trustworthy.

## What this does not claim

- **No operator data was touched.** Every drill runs under a private temporary directory, and
  `_check_scratch` refuses any path outside it. One test asserts that refusal.
- **This is not the release's rollback plan.** The drills exercise the *mechanism* (`db_maintenance` and
  `runtime_persistence`) on a fixture holding two small tables. They do not prove that a production
  history restore completes inside an operator's maintenance window, and they say nothing about
  install/lifecycle rollback.
- **The drills do not run in CI.** They create and mutate real SQLite files, so they are run on demand
  and their output recorded here; `test_release_drill_witness.py` does run them, but in a temporary
  directory and without asserting timings.
- **Restore still requires the app to be stopped.** `restore_database` refuses when WAL/SHM sidecars
  exist; the drill does not attempt a live restore and neither should an operator.

## Why the drills being done does not unblock the release

`DrillRecord`s are one row of the qualification pack. The pack still blocks on the things this agent
cannot do: running the website pilot at a real head with an artifact review, and obtaining independent
security and screenshot approvals. Recording these drills moves the release from *"three drills never
run"* to *"three drills run and measured, two approvals and one pilot remaining"* — a shorter list, not
an empty one.
