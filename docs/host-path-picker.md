# Studio-host path picker

Implemented for [issue #228](https://github.com/pankajtiwarics27/m5-agent-studio/issues/228)
as an explicit filesystem/runtime capability plus its UI, separate from the Chat
protocol fix. Architecture: [ADR-0028](adr/0028-confirmed-studio-host-path-selection.md).

## Operator workflow

**Browse Studio host…** opens the same accessible popup from Start, Harness, SDLC and
CodeGraph workspace fields. It navigates the **Studio host** filesystem. A remote
server/container is not the browser's computer; browser directory uploads do not
provide trusted host absolute paths. Type a path manually if browsing is unavailable.

On **Start**, scroll the main content past **Readiness details** and **System
health**, then expand **Project workspace** and press **Browse Studio host…**. This does not
require a connected model source. The Start content owns native scrolling inside
the fixed workspace; opening disclosures must not clip the controls below them.
Keyboard users can Tab to the native workspace summary, press Space to expand it,
then Tab through the path field to Browse. Cancelling returns focus to the opener.

Choose an approved/recent location, use Up, filter names or enter a known host
folder and Go. **Cancel**, Close, backdrop and Escape discard the selection;
browsing never creates directories or changes the workspace. **Choose folder**
updates the existing shared workspace setting with `create_if_missing:false`.
The manual Set / Create workflow is unchanged. Listings never read file contents.

**New folder** is a different action: edit a single name, review its full parent
and destination, check confirmation, then **Create folder**. Changing the parent
or name resets confirmation. Creation does not change the workspace; review the
new folder and separately press Choose folder. Cancel cannot undo a creation already
confirmed/submitted. A late creation reply does not replace newer navigation.

MCP's executable Browse replaces only the executable draft. Script/file-path
Browse **appends one quoted argv token**, retaining other arguments. Environment
path Browse requires a variable name and merges just that key into an existing
JSON object; invalid JSON/arrays are not overwritten. Environment paths can serve
working-directory conventions required by an individual server; this does not
invent a generic MCP `cwd` field. No MCP save, launch, sync or tool execution occurs.
Hidden entries and symlinks are intentionally excluded, including common
`.venv/bin/python` symlinks: enter those paths manually when appropriate, rather
than silently weakening the no-follow policy.

**Ask a model for a name** is optional and explicit. Pick a cached catalog model
and enter the description to send; it may use a cloud/billable provider. Merely
opening the popup/details, browsing, or creating a folder does not request a model.
Only the description and a fixed name-only system instruction are sent—not paths,
listings, files, environment or conversation history. Anything you put in the
free-text description is of course included. The returned name is untrusted:
**Use suggested name** copies an editable draft, not a creation/application grant.
Then review/edit, confirm Create and separately Choose. No tools are advertised or run.

## Authenticated API boundary

These are operator APIs in the existing single-operator bearer/Host/Origin
boundary, not registered agent tools or a new execution grant. Existing browser
sessionStorage authentication is unchanged. Both POSTs share the existing
expensive-request limiter and maximum body size; bearer authentication also
protects GET. A supplied cross-origin Origin on POST is rejected by the shared
middleware. Disabling execution or plan-mode tool restrictions does not turn
explicit operator folder management into an agent filesystem tool.

| Method and route | Request | Response |
|---|---|---|
| `GET /api/workspace/picker` | Query: optional `path` (default saved/current workspace, else approved root), `kind=directory` or `file`, `query` (case-insensitive substring, at most 100 characters). | `200`: `path`, `parent` or null, `entries:[{name,path,kind}]`, `roots`, `truncated`, `filesystem:"studio_host"`. Directory mode lists folders; file mode lists folders for navigation and regular files for selection. |
| `POST /api/workspace/picker/folders` | JSON object: `parent` absolute approved host folder, `name` single portable basename, `confirmed:true` (exact boolean). | `201`: `path`, `workspace_changed:false`. One non-recursive `mkdir` with mode `0700`, subject to umask. Existing names are never overwritten. |
| `POST /api/workspace/picker/suggest` | JSON object: `model` (1–512 characters), `description` (1–1000 trimmed characters), `confirmed:true` (exact boolean). | `200`: `name`, `created:false`, `workspace_changed:false`. Strict single-key JSON name response, 128 output-token request, 45-second timeout and 4096-character content limit. Reasoning is not a name or executable input. |

Invalid JSON, confirmation, kind, search, basename, root, hidden/secret/symlink,
missing/inaccessible folder or unsupported secure-fd support yields `400`.
Provider failures/timeouts yield a safe `502`; shared middleware retains
`401`/`403`/`413`/`429` behavior, including Retry-After. Creation records a
privacy-preserving `workspace_create_folder` operator audit start **before** mkdir
and a finish; audit-start failure prevents creation. It does not create a workflow,
agent grant or task action. A failed finish/response may follow a successful mkdir:
refresh before retrying. Browse/cancel do not create preferences or persist a path.
The locations resolver reads the existing preferences file solely for approved
workspace locations, never user-selected file contents.

## Roots, privacy and race policy

Defaults are the host home directory, current workspace, saved default workspace
and up to 20 saved recent locations, plus operator additions. These are an
**operator navigation allowlist, not a process sandbox or a restriction on the
existing manual workspace API**. `STUDIO_PATH_PICKER_ROOTS` adds (does not replace)
roots: a strict JSON array of at most 32 absolute directory strings, with existing
configuration-style `~` expansion. Missing means `[]`; explicit empty/non-JSON,
relative, traversal, NUL or non-string values fail startup configuration validation.
Parsing never creates/checks directories. When browsing, missing roots are skipped
and existing roots are canonicalized/deduplicated; a configured root symlink is
resolved as operator configuration, never followed during child navigation.
For example: `STUDIO_PATH_PICKER_ROOTS='["/usr/bin", "/opt/project tools"]'`.

Each request opens every absolute path component with POSIX directory descriptors
and `O_NOFOLLOW`; traversal/backslash paths are rejected. All symlink children,
non-regular files and hidden components are omitted/refused (except `.venv` for
virtual-environment directory navigation). Known credential/key/secret patterns,
Google default-credential filename, control/format/surrogate characters are omitted.
This is not a universal secret-name classifier: an ordinarily named file may be
sensitive, but contents are never read/sent. Search scans at most 2000 entries,
returns at most 200 matches and signals truncation; it is not a recursive search
or a promise to find every match in a large directory. Folder names additionally
reject leading/trailing spaces, trailing dots, platform-reserved names, separators
and reserved filename metacharacters, and permit at most 240 UTF-8 bytes.

Descriptor-relative operations resist component/symlink swaps before and after
open. If another process renames an already-open directory, creation stays in
that original directory, not its replacement; returned display paths can become
stale. Selection is a reviewed path string, not a lasting inode/permission grant;
later workspace/MCP use retains its existing validation and authorization.
Unsupported fd/no-follow platforms fail closed with manual-entry fallback, not
an unsafe path-resolution fallback.

## Author evidence and remaining acceptance

`tests/test_workspace_paths.py` covers positive/negative API, consent, privacy,
roots, bounds, configuration, audit failure, symlink swaps and secure-fd fallback.
Its standalone test copies only generated `studio.py` and reruns the same boundary
suite with the inlined module alias; no sibling source is supplied.
`tests/browser/path_picker_walk.mjs`, required through the existing browser CI
suite, exercises actual markup, picker helpers and shared overlay/keyboard
controller with synthetic replies: both themes/densities, 320–1440px widths,
focus wrapping/return, Escape/cancel, error recovery, draft argv/environment,
explicit model/create/use separation and stale replies. It blocks all network.

This is author test evidence, **not** independent security review or human visual
acceptance. Python 3.11/3.12/3.13 and browser CI plus final owner end-to-end review
remain required before merge. No live model/provider/MCP request is needed or
claimed, and AS-031/signature/roadmap acceptance fields are not changed.

### 2026-11-07 implementation checkpoint

- `pytest tests/test_workspace_paths.py tests/test_overlays.py`: 77 passed
  (60 picker cases including isolated standalone parity; 17 overlay cases).
- Dedicated picker browser walk: 25 checks, zero network requests/page errors.
  Integrated six-pane capture/keyboard/overlay test plus picker browser test:
  2 passed; the integrated overlay walk now checks all 13 dialogs (98 passed
  assertions, 3 prerequisite-blocked existing session-specific dialogs).
- Ruff, mypy (62 source files), frontend syntax, standalone smoke, ADR/docs
  validation passed. Generated inventories rebuilt: 37/37 artifact checks,
  7/7 phase gates, zero findings; historical approval remains unbound.
- Full-suite and complete browser reruns are pending at this checkpoint; these
  counts do not assert completed multi-version CI or human/security acceptance.

### Regression follow-up

The complete browser job passed: **36 passed**, with the required-browser/no-skips
plugin enabled. The first full offline run found **3249 passed, 33 skipped,
6 failed**: two post-roadmap inventories had not been regenerated, another legacy
modal assertion still expected 12 dialogs, two documentation-command tests repeated
the stale Start inventory failure, and the shallow clone lacked the pinned Git
baseline. Regenerated the Start/Pipeline inventories, updated the modal assertion
without weakening its accessibility checks, and fetched full Git ancestry (no
baseline or acceptance record changed). All **94 tests** across those affected
modules, including both nested documentation commands, now pass. The 37/37,
7/7 generated release gate still passes. Final full-suite rerun remains pending.

### Final verification and current-main integration

Before the concurrent main update, the full suite with browser dependencies
configured passed: **3287 passed, 1 skipped, 1 deprecation warning** in 1033.95s.
The skip is `test_live_sandbox.py`, which requires explicit live-container opt-in.
All four PR CI jobs at `0c53033` passed (Python 3.11/3.12/3.13 and browser).
Both modular/standalone startup-auth smokes passed. Supply-chain build and verify
passed secret/dependency scans and artifact provenance verification; the artifact
remains unsigned and is **not** declared release-ready.

While those ran, main advanced to `660b504` through PR #223. Integrated that main
commit on the same picker branch, preserving its portable mobile Chat CSS, SQLite
retry fix, robust transformation samples and approved cross-tab gate. All merge
conflicts were generated artifacts and were resolved by their generators, not
hand-editing. Regenerated the cross-tab **contract source digest only**; the
human approval file is unchanged from main, and no new approval was recorded.
Post-integration evidence: **185 focused tests**, **36 required browser tests**,
and the existing approved cross-tab check: **48 captures, 2200/2200 checks**.
Ruff, mypy62 and frontend syntax remain clean. The full pre-integration count is
not represented as a post-integration run; updated multi-version CI is required
on the integrated head before merge. No PR merge has been authorized/performed.

## Start scrolling regression — issue #236

Owner manual test4 could not reach Project workspace because Start clipped its
content below Readiness details. An offline Chromium regression failed before the
patch: at390×568 the content shell was1263px tall, extended below the viewport,
and had visible rather than scrollable overflow. The fix makes that shell a
shrinking flex child with `min-height:0` and native `overflow-y:auto`; it does not
change global pane overflow, the fixed header/navigation, or any API/consent path.

The real-markup/controller path-picker browser walk now includes16 additional
Start reachability cases: light/dark, comfortable/compact,390×568,900×700,
1440×810 and1572×865, with five synthetic recent tasks and a model-source blocker.
Real wheel scrolling reaches/opens Project workspace; the real Browse button
opens the picker, directory navigation then Cancel preserves the original path
and restores focus. Tab/Space/Enter/Escape repeat reachability without a pointer.
No workspace update, folder creation, model request or external request occurs.
This closes a test gap: the previous walk focused an offscreen field and invoked
the picker programmatically, which could pass despite unreachable controls.
The complete picker walk passes41 cases with0 network requests and0 page errors.
Full multi-version/browser CI and the owner's same test4 retry remain pending;
this is author regression evidence, not a new human/security approval.

Author checkpoint: the mandatory browser suite passes36 tests with no skips;
workspace-path/Start/Pipeline regressions pass124 tests. Ruff, mypy62, frontend
syntax and docs checks pass. Regenerated all37 normal UI/UX artifact sources,
the separate Pipeline/Start inventories and preservation second pass; the
aggregate reports37/37 checks,7/7 gates,0findings. The inherited cross-tab approval
and security review records are unchanged.

Full-CI correction on PR #237: the Python jobs found five failures missed by the
initial focused run (the browser job passed). Two responsive negative tests
matched the entire old CSS prefix, so their mutations stopped changing the source
after scrolling declarations were added. They now target only the shell's width
while preserving its independent scroll rules; their mutation/finding assertions
are unchanged. Three cross-tab contract tests failed because the separate
`scripts/uiux_v2_cross_tab_reference.py` generator, outside the37-source release
gate, had not been run. Regenerating it changes only the reference artifact's
static source SHA; it neither approves a candidate nor alters the inherited
approval, structural rules or fingerprint. All five CI-reported failures now
pass locally, as do Ruff/mypy62/docs checks. Required full CI must rerun on the
corrected head before merge; owner test4 acceptance is still pending.

A second CI correction addresses scoped push collection, not the scrolling fix:
full PR Python3.11/3.12 passed while the scoped push order imported the security
CLI wrapper instead of the runtime module. `tests/test_screenshots.py` prepended
`scripts/` globally to `sys.path`; unlike full collection, this selection had not
yet cached the runtime security module. A fresh screenshot→security collection
reproduced the missing evaluator error. Importing the screenshot validator via
its `scripts` namespace removes the global path mutation. A clean-child regression
fails before this change and now checks both an unchanged import path and the
runtime module's exact file/evaluator. Screenshot/security/scoped-selector tests
pass61 cases in that order; Ruff/mypy62/map/sign-off checks pass. Security/runtime
code, approvals and CI selections are unchanged. Both push and PR checks must
succeed on the corrected head; no release or owner test4 acceptance is inferred.

Integration checkpoint: main advanced through PR #233 while this fix's CI ran.
Merged that accepted source into this branch and resolved generated-file conflicts
by rebuilding standalone, all37 UI/UX sources, the separate Pipeline/Start/V2
preservation/cross-tab contracts and the preservation second pass. Aggregate:
37/37 checks,7/7 gates,0findings. On the merged source, screenshot→security→scope
regressions again pass61 and the actual picker browser walk passes41 (no requests
or page errors); Ruff/mypy62/frontend syntax pass. Before integration the broader
40-module scoped group passed809 tests with30 browser skips (browser checks are
a separate mandatory job, not counted as passes here). Inherited approval records
are identical to main. Complete CI on the integrated head is still required.


## Selection-first UX — issue #238

Owner manual test4 passed, including cancellation and explicit creation, but the
custom browser was confusing compared with the expected Finder flow. This UI-only
follow-up makes the scope and commit action explicit without introducing a native
OS bridge or a new filesystem/provider capability.

- Workspace and executable entry buttons say **Browse Studio host…**. The chooser
  identifies itself as Studio's web picker, not Finder or an upload dialog. Native
  browser file/directory dialogs return browser-side files or handles, not a trusted
  Studio-host absolute path; no guessed path or upload is substituted here. A
  native desktop bridge would be a separate, explicitly authorized capability.
- Existing-folder/file selection is primary. Navigate, inspect **Selected**, then
  press **Choose folder** or **Choose file**. Contextual help says whether this
  updates the shared workspace, replaces a draft field, appends a quoted argument,
  or changes only a named environment value. MCP drafts still require Save.
- A fixed footer keeps status/errors, selection and **Cancel / Choose** available
  while the body scrolls. Long paths have a bounded scrollable preview rather
  than pushing the actions offscreen. No synthetic Finder chrome is implied.
- **New folder… (optional)** is a secondary disclosure. It states before creation
  that creation is a separate write, does not apply the selection, and cannot be
  undone by Cancel. Confirmation, audit and separate application are unchanged.
  Optional model naming stays nested, is collapsed each time the picker opens,
  and still requires an explicit request with the existing data/consent boundary.
- Roots, hidden/secret exclusions, no-follow rules, authentication, sessionStorage,
  manual Set / Create and all backend routes/contracts are unchanged.

The real-markup browser walk adds22 UX cases to the previous41: host-visible
entry labels, context-specific commit help and reset, and expanded creation/model
forms with long selections across both themes/densities and four viewport sizes.
The existing delegated Enter navigation/filtering still makes one read per keypress without applying or creating. The new label regression failed before patch. All63 picker cases now pass with
zero external requests/page errors. Full multi-version/browser CI and
owner review of the new UI are still pending; no approval record is self-signed.
Owner functional testing proceeds independently with test5 (Start-to-Harness draft
handoff without pressing Run), not a test of uninstalled picker changes.

Author verification for issue #238:161 workspace/Start/Pipeline/responsive/contract
regressions and36 mandatory browser tests pass (no browser skips), including the
63-case actual picker walk. Cross-tab structural check passes48 captures and
2200/2200 assertions against the unchanged inherited approval. Regenerated all37
release-gate sources plus the separate Pipeline/Start/V2 preservation/cross-tab
contracts and preservation second pass:37/37 checks,7/7 gates,0findings. Ruff,
mypy62, docs and frontend syntax pass. Synthetic light desktop/dark mobile picker
previews are author evidence only, outside Git, not new signed visual approval.


## Shared workspace header — issue #240

Owner manual test6 showed the selected folder in settings and the composer, but
the shared toolbar still printed the literal “Workspace”. That span was static;
the screenshot alone did not establish a saved-workspace or reload failure.

The desktop header now displays **Workspace: full host path**, with the same
`plan.dir` that the existing composer/run plan reads. It is a display of the
current draft selection, not another saved preference, a persistence guarantee,
or a statement that an active task's execution directory has changed. Existing
workspace validation/save/reload and authority boundaries are unchanged. Empty
selection says **Workspace: not selected** rather than retaining a stale path.
Long paths ellipsize inside the available toolbar width, while full text remains
in the DOM and `title`. Paths are inserted only with `textContent`/`title`, not
HTML. Existing sub800px header-caption hiding remains; the project summary and
settings still provide directory access on compact screens.

The actual task-workspace browser walk failed before patch because its loaded
caption was only “Workspace”. The added checks cover default hydration, draft
changes with duplicate leaf names, root/empty values, long hostile path text,
reload back to the server's saved default, and all theme/density/viewport
combinations without toolbar overflow. Display updates make no write/inference
POST; the test does not save fake paths or claim draft edits persist. Manual
test6 remains pending the installed header fix and owner reload verification.
Full multi-version/browser CI and independent approval gates remain separate.

Header author checkpoint:66 task-layout/workspace-path regressions and36 mandatory
browser tests pass, no browser skips. The added header checks exercise the real
page/controller, including startup/reload and safe draft changes; the pre-patch
failure reported exactly the static “Workspace” caption. Ruff/mypy62/frontend/
docs and both modular/standalone startup-auth smokes pass. Rebuilt standalone and
all37 UI/UX sources plus separate Pipeline/Start/V2 preservation/cross-tab contracts
and preservation second pass:37/37 checks,7/7 gates,0findings. Broader source and
cross-tab visual checks are running separately; full CI and owner test6 remain
pending. No independent approval record or backend behavior changed.


### PR #241 CI timing regression

The Python3.12 PR run42410229249 failed in the checkout-isolation child for
`test_http_endpoints_and_standalone_studio_bundle_parity`. The expanded traceback
showed a legitimate negative A/B outcome: the CI-report scenario measured
40.608ms baseline,100.5828ms qualified latency, and50.6023ms transformation
overhead against its50.0ms allowance. The runtime correctly kept the gate shut;
the HTTP success-path test incorrectly assumed real runner timing always qualified.
Three trials had not removed that assumption, so retries are not the remedy.

The test-only correction separates responsibilities:

- The HTTP success-path test substitutes explicitly synthetic measurements at
  `run_builtin_transformation_ab_suite`, computed through the real comparison
  policy. Real HTTP handlers, report recording/gating, execution, model-authority
  rejection, operator promotion, redaction and revocation remain under test.
  These fixture measurements are not performance evidence or release acceptance.
- The existing real-clock built-in suite still executes all three scenarios and
  all three paired trials. It checks completion lift, token savings, zero
  unauthorized relaxations, per-scenario overhead enforcement and the actual
  persisted gate decision, rather than demanding that every runner qualify.
- Additional modular and standalone HTTP tests check49.9999ms, exactly50.0ms,
  and the observed50.6023ms cost. A non-persisting preview leaves the prior gate
  and report unchanged. Persisting the over-budget report closes an already-open
  gate, survives a fresh registry read and rejects execution with
  `ab_benefit_gate_closed`.

No production source, threshold, ratio, trial count, authority check or approval
record changes in this CI correction; no retry loop or skip is added. Local
verification:15 trusted-transformation tests plus the exact checkout-isolation
case pass (16 total); Ruff, mypy62 and frontend syntax pass. Full multi-version
CI remains required before merge; manual owner test6 is still pending.


### Merge integration with accessibility updates

The owner authorized merging PR #241 after the original eight checks passed.
GitHub then refused the merge because PR #242 had advanced main to06067b6 and
conflicted with generated inventories/evidence. Main was merged into the existing
workspace-header branch without rebasing or force-pushing. Canonical HTML merged
cleanly, retaining the selected-path caption and all new accessibility fixes;
standalone output and the conflicting inventories were regenerated through their
owning scripts, not resolved by choosing stale generated contents.

Fresh combined-source checks:204 focused regressions (including all13 checkout
store-isolation cases and the accessibility contract),36 mandatory browser tests
with no skips,120/120 accessibility checks with62 captures,48 approved visual
variants with2200/2200 checks,15/15 keyboard and27/27 responsive checks. All37
release artifact checks and7 gates pass with0 findings. Ruff/mypy62, docs235,
frontend syntax, scope-map171 and both startup/auth smokes pass. The latest
accessibility run was imported through its validated evidence importer; its
source digests bind the combined HTML. Existing human visual approval and the
independent security sign-off are preserved byte-for-byte from main; no new
human acceptance is asserted. The additional old-head local full-suite run was
stopped before changing its shared working tree; it is not claimed as complete.
Fresh exact-head multi-version/browser CI is required after this integration.
Owner manual test6 remains pending; merge authorization does not complete it.
