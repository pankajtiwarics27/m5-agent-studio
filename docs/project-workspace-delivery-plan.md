# Project workspace and artifact review delivery plan

This is follow-up implementation planning for the owner's Claude-like direction in
[the integrated UX specification](autonomous-production-ux-direction.md). The supplied
images are hierarchy references, not pixel-perfect designs or approval of a release.
The screenshots show empty and unconfigured states; they do not establish which active
session capabilities exist or work. Website is the first pilot; media is not excluded.

## Delivery order

1. **State and route audit.** Map the existing six panes, session lifecycle, conversation,
   task controls, artifact identity, preview and review APIs to their owners. Record
   which project/session relationships and artifact operations are actually available;
   do not substitute a global artifact or workspace snapshot for a session-owned version.
   Capture disposable baseline states and a blast-radius map before changing the shell.
2. **Conversation-first project workspace.** Extend the existing Harness/session surface:
   conversation and a current-artifact area side by side, stacked at narrow widths.
   Keep navigation, model selection, permissions, stop, drafts and recovery available;
   progressively disclose secondary activity and configuration. Show unavailable, loading,
   failure and stale states rather than invented progress or artifacts.
3. **Real artifact preview.** Connect only session-scoped, versioned data with an existing
   authority. Support the website pilot first, with an inert or appropriately isolated
   preview and files/sources where supported. Do not introduce privileged-origin access,
   credentials or network access to untrusted content. Keep absence explicit.
4. **Focused review.** Preserve project, version, conversation position and feedback draft
   across entry and return. Associate feedback with supported selection context, with a
   text-only fallback and explicit stale-version handling. Show actual checks and blockers.
   Keep direction, final acceptance, reference use and publish permission separate.
5. **Visual refinement and qualification.** Apply a quiet warm-neutral visual system and
   readable hierarchy across Start, Harness, Compare and Chat without removing controls.
   Test keyboard/focus, light and dark themes, narrow layouts and 200% zoom, plus offline,
   reconnect, denied and failed states. Submit rendered states for owner visual review.

Each stage should ship in small, reversible slices using existing ownership, state and
security boundaries. No new approval, grant, external call or runtime activation follows
from this plan. The existing AP acceptance-outstanding and release gates remain in force;
passing a structural test or viewing a concept does not qualify a pilot or approve a UI.

## Initial slice

The Compare layout currently uses the greater of selected models and existing result
panels for the result grid and single-answer note. Selecting models for a future run can
therefore make an existing single answer stop identifying itself as a single answer, or
change the number of columns for existing results. The first slice derives that layout
from existing panels when present and uses selections only before results exist. It does
not change dispatch, model selections, persistence, verdicts or permissions. The separate
empty-state card in the supplied screenshot requires further reproduction; this change
is not represented as a fix for that screenshot's full state.

Verification for this slice: run the renderer with no results and with one/two result
panels while changing the selection; mutation-check the previous maximum-of-both rule;
run neighboring Compare tests and frontend syntax; regenerate and check source-bound
artifacts. Browser visual, full CI, independent security and owner review remain separate
and are not implied by these focused tests.
