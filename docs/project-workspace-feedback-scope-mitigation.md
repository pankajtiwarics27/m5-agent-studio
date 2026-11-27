# Feedback scope: interim file-selection refusal

Issue [413](https://github.com/pankajtiwarics27/m5-agent-studio/issues/413) exposed that a
current-worktree diff path could be sent as a region of the first checkpoint artifact, even
when no relationship had been established. This bounded mitigation makes diff rows inert,
refuses the legacy selection handler, and sends no selection from this composer. It names
the first eligible bound artifact and digest, states that other artifacts cannot yet be
chosen, and discloses that recorded content was not rechecked against current files.
Plain-text feedback remains available at artifact-version scope. The previously exposed file
selection is temporarily unavailable rather than being silently recast as an element.
Existing scoped records still display; the backend API and its selection types, stored
records, authority, confirmations, reference adoption and publishing behavior are unchanged.

The real browser fixture now contains a bound artifact and a second unrelated changed file.
The feedback walk passed 60/60 checks, including inert rows, binding and limitation text,
the legacy handler refusal, and a persisted artifact-scoped submission even when a stale
selection was deliberately injected into page state. Six targeted mutations (missing second
file, interactive rows, missing bound identity, false freshness, legacy-handler selection,
and sending stale selection) were caught. The mandatory browser wrapper passed all six
walk tests; an obsolete expected-count mutation failed. The focused feedback, reference,
recovery and presentation suite passed 132 tests. The keyboard inventory now pins seven
remaining non-native controls because the misleading diff-row control is inert; its 34
regressions passed, and a mutation allowing a missing control was caught. The broad
accessibility, performance and cross-tab walks passed 120/120, 21/21 and 2200/2200;
backend safety passed 309 tests and 16 isolation checks, with isolation unqualified. The
historical UIUX gate passed 37/37 artifact checks and 7/7 phase gates, while its old
preservation approval remains unbound. These results do not establish server-side checkpoint
membership or authorization, current-file integrity, owner visual acceptance, or
website-pilot qualification.

This is a containment slice, not completion of focused selection: choosing another artifact,
verifiable region selection and the route's caller-supplied binding remain open. Issue 413
stays open. Drafts remain page-memory only, and an in-flight submission can still succeed
after navigation; the existing warning and fencing remain in place.
