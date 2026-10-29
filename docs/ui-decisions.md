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
