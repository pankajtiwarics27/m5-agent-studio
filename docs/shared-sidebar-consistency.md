# Shared left navigation consistency — issue #243

The owner confirmed the workspace caption fix and then reported different left
navigation geometry in Harness versus Start/other panes. This is a separate
presentation fix; it does not start another manual QA test or infer acceptance
of unrelated readiness/model state shown in the screenshots.

## Scope

The common desktop navigation remains 200px wide. Harness now inherits that
width, navigation padding, row gap, button alignment and label visibility rather
than replacing them with a 240px/12px/4px variant. The shared width token also
drives the main-content offset and the existing Harness session list. Other
panes keep their existing navigation geometry. At intermediate/mobile widths,
all panes use the same icon rail or horizontal tab row.

Harness still has one New session control, filter and task list below navigation.
A DOM-only ResizeObserver positions the existing list from the actual navigation
height, replacing its fixed 320px offset. It follows density, fonts and resizing
without storing preferences, fetching data, or changing task/session handlers.
The narrow-screen Sessions drawer, keyboard tab handling and all execution,
authorization and workspace behavior remain unchanged.

## Regression and verification

The real-browser task workspace walk compares navigation width, padding, gap,
every tab rectangle/alignment/label visibility and main-content offset across
all six panes, both themes and both densities at390/799/800/820/1279/1280/1568px.
Desktop session lists must start at the actual navigation bottom without covering
the last tab. Switching panes must make no write/inference request.

Before the fix, the new browser check failed on Harness at390px: Start had24px
horizontal padding and1px gaps while Harness had12px padding and4px gaps. After
the fix the same browser regression passes, including desktop width consistency.
67 task-layout/workspace regressions, Ruff, mypy62 and frontend syntax also pass.
Broader regeneration/browser evidence and fresh full CI are recorded in the PR;
checks not completed are not claimed as passing.

The source-change commit preserved the existing human-approved visual record
and independent security sign-off. The intentional Harness geometry change
requires a fresh clean-head48-variant candidate and explicit owner visual approval
before its structural baseline may be refreshed. The subsequent owner decision
is recorded below; candidate checks alone are not independent acceptance or
permission to merge a new PR.


Author checkpoint:223 focused source regressions pass, including checkout-store
isolation, accessibility contract and token registration. Final-source mandatory
browser suite passes36 tests with no skips, plus120/120 source-bound accessibility
checks/62 captures. Keyboard15/15 and responsive27/27 walks pass. Generated release
artifact validation records37/37 checks,7/7 gates,0 findings. Standalone was rebuilt;
Ruff, mypy62, frontend syntax, docs236, scope-map171, baseline validation and both
startup/auth smokes pass. Two shell metrics are registered in the token inventory;
348px is only the initial navigation-height default before actual measurement.
These checks are not visual-baseline approval, full multi-version CI, merge
permission or independent security sign-off. At that checkpoint, clean-head
visual review was pending and the existing approval JSON remained untouched.


## Owner-approved visual baseline

The owner approved the sidebar layout and then explicitly confirmed the
replacement candidate through `sidebar-243-recaptured-approval=approve`. The
original local candidate had been cleared by the session reset; it was not
reconstructed or silently rebound. The replacement was recaptured from the
unchanged source commit `55dff2b7c73f2064c7a649ceb1edfa9a9052d5a7`, with a clean
tracked worktree at capture, all48 variants and2104/2104 independent checks.

Approved manifest SHA-256:
`7186910f9c46979e6320a6e2fa84f53e84067159f2b4d92ea5e03a6b59f884b8`.
The owning approval command recorded that explicit decision and regenerated the
cross-tab contract/page. The approved baseline now passes a fresh48-variant
live/reference run with2200/2200 checks, including exact live/reference structural
signature comparisons. No visual gate, tolerance or independent layout rule was
weakened. Post-approval source regressions pass38 tests; Ruff, mypy62, docs236,
source manifest and security-signoff validation pass. App/HTML/standalone code
has not changed since the reviewed source commit.

This is visual baseline approval only. Independent security sign-off remains
unchanged/deferred. New exact-head Python3.11/3.12/3.13 and browser CI remain
required; PR #244 remains unmerged and no merge authorization is inferred.


## CI resize-observer synchronization correction

The expanded traceback for PR run42447643574/job128632494378 establishes why
the task-workspace browser walk failed after visual approval. At1280px the
navigation occupied y=56..437 (381px high), but the session panel started at100px:
it still used the previous44px mobile navigation measurement. The composer loop
read geometry immediately after resizing/changing density, before the native
ResizeObserver delivered the new measurement. The production observer delivers
during rendering; the protocol evaluation is not itself a rendering barrier.

This correction is test-only. Before desktop Harness geometry is sampled, a
bounded requestAnimationFrame-polled condition waits for the CSS measurement to
match actual navigation height and the session panel to meet the navigation
bottom. The original alignment, equal-width and non-overlap assertions remain
unchanged. Missing/broken delivery still times out with measured DOM diagnostics;
no arbitrary sleep, blind CI rerun, relaxed tolerance, layout mutation, skip or
new production behavior is introduced. Existing navigation frame alignment is
retained, and narrow-screen drawers/other panes keep their independent checks.

A deterministic browser regression holds a real native observer callback. It
recreates exactly sideTop=100/navBottom=437, proves that the original non-overlap
assertion rejects those coordinates, verifies that readiness stays pending and
does not overwrite the44px measurement, then explicitly releases the callback.
The wait only completes after the genuine381px measurement puts the panel at437.
A permanently undelivered measurement must time out without altering geometry.
The fixture is serverless, makes no requests and tests both narrow breakpoints
and another pane. An author negative probe replacing the wait with an immediate
return was rejected deterministically; the real focused browser test passes.

HTML, standalone, backend, approved visual signatures and independent security
sign-off remain unchanged by this correction. The existing owner visual approval
is not reinterpreted or refreshed. Full browser/visual checks and new exact-head
multi-version CI are recorded in PR #244; no new merge permission is inferred.
