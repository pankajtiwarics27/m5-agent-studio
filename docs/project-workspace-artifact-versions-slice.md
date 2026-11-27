# Adjacent workspace: recorded artifact versions

This slice displays the selected Harness session's `session_artifact_versions` from its
existing `/state` snapshot in the adjacent workspace panel. It adds no endpoint, file
read, active preview, network call, permission or approval. The version is derived from
an integrity-checked checkpoint reference; it is not necessarily the current file, and
missing provenance axes make the identity weaker. Workspace recovery snapshots and the
current inert Git diff remain separately named. The existing verification and artifact
modal remains the path to more detail and scoped feedback.

The panel must distinguish a missing or malformed response, a checkpoint integrity
failure, no derivable versions, and a successful bounded list. It must refuse foreign
sessions, malformed rows, a contradictory checkpoint, and unsupported identity claims;
show stale state as last known; render untrusted identifiers only as text; clear the
old session immediately on switching; and never imply file freshness or acceptance.

Verification: run the actual frontend renderer against valid, empty, malformed, corrupt,
stale, foreign-session, long and hostile records and mutation-test each critical refusal.
Exercise the real route and panel in a disposable browser, including narrow/zoom and
session switching. Re-run source-bound inventories and browser/layout/security gates.
No result is owner visual acceptance or a website-pilot qualification.

## Progress and remaining work

The renderer and state-transition probes passed; fifteen targeted renderer and transition
mutations were caught. A real browser compared the panel against two session checkpoint
routes, verified immediate clearing and switching back, and inspected panel geometry at
390 CSS pixels and a 720 CSS-pixel viewport at device scale factor two. Its complete
feedback walk passed 59/59. Removing the freshness warning, omitting the immediate reset,
using the same checkpoint in both sessions, and forcing the panel wider than the viewport
made their respective checks fail; the restored walk passed. The mandatory browser test
wrapper passed all five tests after updating its expected count; restoring the old count
made the feedback wrapper fail. The geometry checks do not establish human visual
acceptance or screen-reader usability.

The focused Python suite passed 59 tests. The general accessibility walk passed 120/120,
the performance walk 21/21, and the cross-tab structural walk 2200/2200. Backend safety
passed 309 tests and 16 isolation checks, but isolation remains unqualified. The
source-bound accessibility and performance contracts were refreshed and passed. After
regenerating drifted source inventories, the historical UIUX release gate passed 37/37
artifact checks and 7/7 phase gates. Its preservation approval is historical and unbound;
it is not current-head release qualification. Current changes are not yet covered by the
draft PR's earlier green CI. Owner review and release qualification remain outstanding.
