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
