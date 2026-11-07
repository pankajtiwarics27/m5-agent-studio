# Studio-host path picker

Implemented for [issue #228](https://github.com/pankajtiwarics27/m5-agent-studio/issues/228)
as an explicit filesystem/runtime capability plus its UI, separate from the Chat
protocol fix. Architecture: [ADR-0028](adr/0028-confirmed-studio-host-path-selection.md).

## Operator workflow

**Browse…** opens the same accessible popup from Start, Harness, SDLC and
CodeGraph workspace fields. It navigates the **Studio host** filesystem. A remote
server/container is not the browser's computer; browser directory uploads do not
provide trusted host absolute paths. Type a path manually if browsing is unavailable.

Choose an approved/recent location, use Up, filter names or enter a known host
folder and Go. **Cancel**, Close, backdrop and Escape discard the selection;
browsing never creates directories or changes the workspace. **Use this folder**
updates the existing shared workspace setting with `create_if_missing:false`.
The manual Set / Create workflow is unchanged. Listings never read file contents.

**New folder** is a different action: edit a single name, review its full parent
and destination, check confirmation, then **Create folder**. Changing the parent
or name resets confirmation. Creation does not change the workspace; review the
new folder and separately press Use. Cancel cannot undo a creation already
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
Then review/edit, confirm Create and separately Use. No tools are advertised or run.

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
