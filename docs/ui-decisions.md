# UI decisions

## Harness and shared shell

- The audit identified Harness as the highest-density pane. Lead model, execution policy, run, abort and the permission badge stay visible. Critic, workspace, worktree and session actions (diff, compact, permissions, evidence) are grouped in native `details` under “Task settings”, and Discipline, reasoning, and step limit in a nested `details` under “Run settings”; their DOM nodes, defaults, values and payload logic are unchanged. *Corrected 2026-11-08 (UIV2-P7-04): this line previously said critic, workspace, diff and compact “stay visible”, which the shipped markup has not done since the disclosures landed — `tests/test_task_layout.py::test_primary_and_safety_controls_are_not_inside_disclosures` asserts both are inside `harnessTaskSettings`. The prose was behind the markup, not the other way round.* Disclosure state is stored in sessionStorage only and storage failures fall back to native behavior. No validation or critical status is hidden.
- Density changes only shell/Harness spacing, not data or execution settings; the explicit preference is stored locally. “Compact” here means layout density, distinct from the existing context compaction command. The control exposes its current state and `aria-pressed`. *Retired 2026-11-09 (UIV2-P7-05): the owner removed the Density control, having found no use for it. The control and `toggleDensity()` are gone, and the boot path no longer reads `m5_ui_density` — a stored “compact” from an earlier session would otherwise leave a reader in spacing with no control to undo it, so the pane is now always comfortable. The `body[data-density="compact"]` token block remains in the stylesheet because `scripts/uiux_tokens.py` requires it to re-point exactly its own five tokens; it is unreachable from the UI, not absent.*
- New session is secondary to Run Turn, avoiding two primary fills in Harness. The information banner now uses `--bg-elev` rather than a fixed cyan tint. The Skills/MCP strip labels drop fixed light-only inline colors and use existing secondary text. This is a targeted migration, not a claim that the screen or app is fully migrated.
- Added `--primary-text` (`#ffffff`) in the existing token layer, inherited by both themes. It replaces the hardcoded white on primary buttons; against the existing `--accent` (`#5969d8` in both themes) it measures 4.73:1. No new palette was introduced.
- The existing telemetry toggle and mobile panel are retained. They already initialize the accessible state for narrow viewports. Its resize behavior and remaining color/contrast debt are deferred rather than risk changing monitor behavior without browser coverage.

## Council workspace

- Council role model selectors remain open initially because they determine the models used for a round; users may collapse them after reviewing the choices. The less frequent “Ask one model” controls are disclosed separately. Reasoning affects both individual and full rounds, so it remains visible, as do Stop, round, conclusion and message controls. Each disclosure remembers its state for the browser session without changing model choices.
- Removed decorative hardcoded colors from role labels and the empty-state heading and conclusion heading, using existing text tokens. New-session buttons are secondary to “Send and run round”. No token additions or execution behavior changes. Role selectors reflow to two columns at the existing 880px breakpoint.

## Compare feature dependency

- The existing Arbiter Verdict checkbox already controls `include_synthesis` in the request. When unchecked, the arbiter model selector is hidden and replaced with an explanation; re-enabling reveals the same selector and value. The request payload and checkbox default are unchanged. This does not create or enable any feature.

## Pipeline controls

- Stage filters and prompt presets were clickable non-keyboard elements; they are now native buttons. Filter selection has a textual selected indicator and synchronized `aria-pressed`; live stage status remains visible. Model and reasoning labels are explicitly associated with their controls and the task has an accessible name. Preset hover now uses existing neutral tokens instead of fixed white/cyan. Filtering and preset logic remain unchanged.

## Phone workspace overflow

- Headless Chromium at 320 and 390px showed that the stacked Harness and Council workspaces clipped their composers below the viewport. At the existing 600px breakpoint, their main areas now scroll, streams have a usable minimum height, and composer inputs wrap onto their own row. The integrations strip is height-limited and scrollable rather than consuming most of the screen. All controls remain in the DOM; no settings or execution behavior changed. This makes the composer reachable by scrolling, not permanently pinned.
- At 1280px with the session sidebar and telemetry open, the Harness prompt was only about 64px wide. Composer controls now wrap before the prompt becomes that narrow; the integrations strip can receive keyboard focus for scrolling.

## UIV2-P7-07 — the header is one row (2026-11-09)

*Owner decision: the workspace toolbar joins the header, and Theme + Telemetry become settings.*

The global header was two stacked rows — `.topbar` (56px) plus `.workspace-toolbar` (48px) —
so 104px of every screen went to chrome before any task content. The toolbar is now a child of
`<header>` and the header is one 56px row, reclaiming 48px on every pane.

*What moved, and what did not.* Theme and Telemetry left the header for a Settings overlay
(`#settingsModal`). Both keep their ids, their handlers and their aria state, so every existing
reader still resolves: `applyTheme` still writes `#themeToggleBtn`/`#themeToggleLabel`, and the
telemetry trigger still points at `#telemetryPopover` via `aria-controls`. Search stayed inline —
it is an accelerator, not a preference, and Claude keeps it in the header too. The telemetry
trigger closes Settings before opening its popover so two layers never stack their dismissal rules.

*What was deliberately preserved.* The workspace caption is still a childless text node
(`Workspace: <path>`) with `title === path`; three browser checks assert `children === 0`, so the
mock's breadcrumb chip is still not adopted. The narrow-width contracts are unchanged in
substance: `.topbar` still scrolls horizontally at ≤390px, and the folded toolbar is forced to
`flex-wrap: nowrap` there so it cannot wrap inside a header that is `overflow-y: hidden`. The
Inspector repair (#monitorToggleBtn at a 195px effective viewport) now relies on that contained
horizontal scroll rather than on the toolbar wrapping — the same remedy `#hallClearSessionBtn`
already uses, and the record in `uiv2_accessibility_adversarial.py` says so.

*Consequence the owner must act on.* The header's geometry changed, so the 48 annotated
cross-tab captures are stale. The recorded approval in `M5_2_0_UIUX_V2_Cross_Tab_Approval.json`
still cites the PR 258 review of the previous geometry; it was **not** re-approved here, and the
browser walk that produces the new candidate could not run in this environment. Re-capture and
re-approve before this is treated as visually signed off.
