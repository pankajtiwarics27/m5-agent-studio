# Accessible overlays, notifications and shortcuts

This P1-04 increment keeps the existing modal surfaces and command actions. It
adds one focus manager around the existing overlays instead of replacing their
workflows.

## Overlay behavior

- Every modal dialog has an accessible name and `aria-modal="true"` while open.
- Opening a modal remembers the prior focus and moves focus into the dialog.
- Tab and Shift+Tab stay within the topmost open dialog. Escape closes only that
dialog; focus returns to its opener or to the still-open parent dialog.
- Every existing open/close handler now uses the shared focus manager. Existing
backdrop clicks and explicit Close buttons remain available.

## Command palette and shortcuts

The command search is a combobox controlling the existing actions list. It exposes
its expanded state, active option and selected option to assistive technology.
Arrow keys change the active option, Enter activates it, and Ctrl/Cmd+K opens or
closes the palette. The existing Alt+1 through Alt+6 tab shortcuts remain sourced
from the tab labels; they do not intercept Alt+N while the user is editing or a
modal is open.

## Notifications

Toasts appear in a named polite live region, use text nodes for message content,
normalize the existing `error` tone alias, and provide a labeled keyboard-operable
dismiss button. The auto-dismiss timer pauses while a toast is hovered or focused.
The stack keeps at most five unpinned notifications, and toast motion is disabled
when the system requests reduced motion.

## Verification and limits

The mandatory browser safety suite now exercises the actual isolated demo. Its
accessibility walk checks command-palette shortcut and combobox state, active
option, focus wrapping/restoration, nested overlay Escape behavior, named dialog
semantics, notification dismissal/focus pause/reduced motion, and page errors.
It uses only the synthetic token generated for a disposable demo and does not use
`scripts/status_walk.mjs` (which has a separate open token-exposure follow-up).

No API, route, approval or durable-state behavior changes. No canonical screenshot
IDs were modified: this story changes interaction semantics rather than a pane
layout, and the historical screenshot provenance hold #122 remains open. Browser
assertions are not a screen-reader test; an assistive-technology pass remains
unperformed.
