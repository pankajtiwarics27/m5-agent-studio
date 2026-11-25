# AP-031 — progressive configuration, onboarding and operations

A capable system feels untrustworthy for three reasons, and none of them is a missing feature: it uses
six words for the same shrug, its readiness screen costs money to look at, and the settings that matter
are hidden in the settings. This module is the value a surface renders, not the surface itself — the
routes and markup are a separate, reviewed change.

## 1. Six distinct states, derived from evidence

`CAPABILITY_STATES` is a closed set — `qualified`, `tested`, `configured`, `experimental`, `blocked`,
`unavailable` — and `status_for()` collapses one observation into exactly one of them, worst-first:

`blocked` > `unavailable` > `experimental` > `configured` > `tested` > `qualified`

The order is the point: a capability can be configured *and* blocked, and reporting it as configured
because the setting exists is the shop-window version of the truth. The constructor refuses the two
upgrades that make status displays lie:

- `qualified` without `tested` — a qualified capability is one that was tested **here**;
- `tested` without `configured` — nothing was configured to test;
- both `blocked` and `unavailable` — they are different answers ("policy forbids it" versus "nothing
  provides it").

Absence of an observation is never upgraded: a capability nobody ran is `configured` at best.

## 2. Readiness never bills anyone to look ready

`ReadinessCheck` records its `basis` — `measured`, `declared` or `unmeasured` — and whether it
`spends_money` or `uses_network`. A check that would spend or contact a network is refused unless an
opt-in was recorded **before** it ran. Two further refusals keep the field honest: `measured` with no
result, and a result on an `unmeasured` basis (a result requires having looked).

`ready` means every row was measured **and** passed. Anything else reports `partly-verified` with the
sentence `"N of M checks were measured; K are unverified, so this is not a clean bill of health."`
Averaging an unverified row into a green badge is exactly what this prevents.

`build_readiness_from_preflight()` projects the existing `onboarding_preflight` snapshot without
inventing anything: its rows are `declared`, because that module reports configuration, and the one
thing it can genuinely observe — a runtime probe that ran — is the only `measured` row.

## 3. Grants are inspectable where they are revocable

`GrantView` names who granted it, what it allows, until when, how many uses remain, and either a
revoke route or **why there is none**. An authority control with no off switch must say so; a settings
screen that displays one as ordinary is the failure.

The reader's own data is the same shape: `PreferenceStore` can `correct`, `export` (with a stable
digest) and `delete` preferences and references, `delete` takes exactly one target — a delete that
guesses is how the wrong preference disappears — and the available actions are reported rather than
assumed.

## 4. Simple by default, nothing removed

`ProgressiveDisclosure` groups settings by `simple`/`advanced`/`expert`. The report states
`hidden_by_default_count` and `settings_removed: 0` in the open, and `unreachable()` returns any
setting no level reveals — a test asserts it stays empty, so "simple by default" cannot quietly become
"capable things deleted".

## 5. Help describes the build, and acceptance is not authority

`build_help()` refuses a topic describing a capability the inventory does not report, and refuses one
calling a capability better than it is: a topic may be honest about a **worse** state than the
inventory, never a better one. Help topics are generated alongside the capability list, so a help page
cannot promise something the build lacks.

`grants_external_authority` is fixed to `False` and constructing it `True` raises: accepting a draft
and being allowed to publish it are different decisions, and no help topic may merge them.
`acceptance_is_not_authority: true` appears in the report itself.

## What this story does not claim

- **No route serves this yet and no markup renders it.** The module returns values; wiring it into
  `static/index.html` and the app routes is a separate, reviewed surface change with its own UI/UX
  gates. Nothing here changes what a user currently sees.
- **No provider, runtime or gateway was probed.** Every readiness row in the tests is a fixture.
- **Existing features were not renumbered or moved.** The disclosure levels describe a proposed
  grouping; the real inventory of controls is still `uiux-2-0-control-inventory.md`.
- **Qualification is not performed here.** `qualified=True` is a claim the caller must already have
  measured elsewhere.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_operations_surface.py` | 61 passed |
| mypy (101 modules), `ruff check .` | clean |
| shard/scopes registries | 20 passed |
