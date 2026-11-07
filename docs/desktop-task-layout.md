# Task-first desktop layout

Owner-requested follow-up to PR #222, using the supplied Claude Desktop screenshot
as a layout reference, not as branding or a feature specification.

## Problem and acceptance

The Harness currently has four competing columns (navigation, sessions, task,
inspector), a persistent integrations band, and separate setup, run-plan and
composer blocks. At ordinary laptop heights the task prompt and output lose most
of their usable space. The same integration counts are repeated in the inspector.

This change is a focused shell/Harness usability fix, not completion of another
UIV2 roadmap story. UIV2-26's parallel visual-reference work is separate.

- One left navigation/session surface on desktop; sessions remain reachable on mobile.
- One central task composer with the selected lead model, policy, workflow, attachment
  and primary Run action. Stop/Abort and approval/recovery controls stay reachable.
- Empty state leads with a short greeting, not a catalogue of internal frameworks.
- Project configuration, critic, advanced settings and complete run details disclose
  on demand. The current project and execution policy remain visible before Run.
- Integrations are one closed-by-default, keyboard-operable tools disclosure, not a
  permanent pill/count band. Existing tools, identifiers and handlers are preserved.
- Inspector is closed by default, opens explicitly, and has an explicit Close action.
  Approval/refusal/recovery state remains in the task view, independent of the inspector.
- No authentication, endpoint, consent, run-payload, default-policy or storage changes.
- Both themes and densities, empty/active/blocked states, keyboard access and narrow
  viewports must be tested with disposable synthetic state, not a user's workspace.

## Verification

Regenerate the standalone bundle and UIUX inventories; exercise the actual DOM/CSS
and real task controls in an isolated demo browser, add structural regression tests,
and run the release gate, lint, syntax and full test suite. Report evidence and
limitations rather than treating an attractive empty state as feature preservation.

## Implemented hierarchy

The annotated problem areas are addressed as follows:

- Skills, MCP and integration counts (1, 8, 9, 10, 11): one tools disclosure, closed
  until requested. Its original toggle pills, summary, menus and unload action remain.
- Inspector and the unused right column (2, 14): closed initially at every width,
  explicit open/close, existing approval/change/verification/resource views retained.
- Progress and empty content (3, 4): progress is shown for a real task; the empty state
  has one short greeting and optional examples instead of an internal-framework list.
- Input, configuration preview and actions (5, 6, 7): one composer. Model, execution
  policy, workflow and project summary remain visible. Project path, critic, advanced
  settings and full run decisions are in keyboard-operable disclosures. Existing
  session grants remain accessible outside the disclosures when a session is selected.
- Two left columns (12, 13): navigation and Harness sessions share the same desktop
  column. Below 1280px, Sessions opens the same session list as a dismissible panel;
  Escape closes it and returns focus. No duplicate model or project inputs are added.

The full run-plan renderer, actual values, handlers and Run guards are unchanged.
A long trajectory has a shrinking, independently scrolling viewport so it cannot
push Abort below the screen. Settings panels are viewport-bounded rather than
positioned above a composer where they could be clipped.

The browser regression uses the isolated demo, blocks non-loopback requests,
checks both themes and densities at phone/tablet/laptop/desktop sizes, tests the
real settings/inspector/session handlers, and holds a Run request in the browser
before it can reach a model to exercise the real Abort path safely. It also injects
a long synthetic transcript to check that Abort stays on screen. Screenshots are
synthetic previews, not owner acceptance or a new sign-off of the visual gate.
