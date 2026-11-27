# Project workspace: first adjacent layout slice

This slice implements the first visible part of the [delivery plan](project-workspace-delivery-plan.md):
when a Harness session is visible, put the existing workspace review beside the trajectory on a
wide screen and stack it on narrower screens. The panel still shows only the existing inert Git
diff, recovery snapshots, declared file requirements and recorded progress. It links to the
existing verification/artifact review; it does not pretend that a Git diff is an active preview,
that a checkpoint is a file copy, or that an artifact is approved. The untrusted-preview isolation
boundary remains unchanged: no iframe, file route, network request or provider action is added.

The same disclosure, handlers and session/request fences are reused; the session composer,
Run/Abort, recovery, log, other panes and approval modal remain available. The new layout is a
reversible presentation change in `static/index.html` and the generated standalone bundle.
The disclosure is opened after the frontend handlers are initialized and reads existing
session-scoped routes, as it did when opened manually. It remains keyboard-collapsible;
when vertical space is constrained, the containing Harness surface can scroll. No
authentication, permission, persistence or runtime policy is changed.

Verification: exercise real Harness sessions in a disposable browser at wide, narrow and 200%
zoom, including opening and closing the disclosure, switching sessions, long trajectory, keyboard
focus and Run/Abort. Check source-bound generators and the existing cross-tab visual gate; report
structural or owner-review failures rather than updating approval on the agent's authority. The
mock's active preview and integrated version-aware review are later work, not supplied by this slice.

## Working-tree evidence and limits

- The disposable Harness recovery walk passed 38 checks, including wide adjacency, narrow
  stacking at 320 and 390 pixels, inert hostile text, keyboard disclosure, and 200% zoom.
  Deliberately stacking the wide layout, forcing a narrow row, and collapsing the panel's
  height each caused the relevant browser check to fail; the restored walk passed.
- The task-first workspace browser test passed with a long synthetic transcript and Abort
  visible. Making the disclosure initially open in markup reproduced an early toggle error;
  deferring its opening until initialization restored the passing browser test.
- The cross-tab structural walk passed its 48 captures, but those captures do not establish
  owner acceptance of the new active-session layout. Accessibility and performance walks
  cover their own fixture states; neither is a review of the website pilot or this design.
- A real active website preview, durable review drafts, final layout review and full CI remain
  outstanding. Screenshots of the disposable fixture are not owner approval.
