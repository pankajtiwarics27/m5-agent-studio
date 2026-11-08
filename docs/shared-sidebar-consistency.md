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

The existing human-approved visual record and independent security sign-off are
not edited by this patch. The intentional Harness geometry change requires a
fresh clean-head48-variant candidate and explicit owner visual approval before
its structural baseline may be refreshed. Candidate checks are author evidence,
not independent acceptance or permission to merge a new PR.


Author checkpoint:223 focused source regressions pass, including checkout-store
isolation, accessibility contract and token registration. Final-source mandatory
browser suite passes36 tests with no skips, plus120/120 source-bound accessibility
checks/62 captures. Keyboard15/15 and responsive27/27 walks pass. Generated release
artifact validation records37/37 checks,7/7 gates,0 findings. Standalone was rebuilt;
Ruff, mypy62, frontend syntax, docs236, scope-map171, baseline validation and both
startup/auth smokes pass. Two shell metrics are registered in the token inventory;
348px is only the initial navigation-height default before actual measurement.
These checks are not visual-baseline approval, full multi-version CI, merge
permission or independent security sign-off. The clean-head visual candidate is
the next review checkpoint; the existing approval JSON remains untouched.
