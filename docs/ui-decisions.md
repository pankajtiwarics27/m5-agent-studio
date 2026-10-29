# UI decisions

## Harness and shared shell

- The audit identified Harness as the highest-density pane. Lead model, critic, execution policy, workspace, run, abort, diff, and compact stay visible. Discipline, reasoning, and step limit are grouped in native `details` under “Advanced settings”; their DOM nodes, defaults, values and payload logic are unchanged. Disclosure state is stored in sessionStorage only and storage failures fall back to native behavior. No validation or critical status is hidden.
- Density changes only shell/Harness spacing, not data or execution settings; the explicit preference is stored locally. “Compact” here means layout density, distinct from the existing context compaction command. The control exposes its current state and `aria-pressed`.
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
