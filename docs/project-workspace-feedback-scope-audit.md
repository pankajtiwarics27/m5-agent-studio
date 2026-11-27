# Feedback selection scope: reproduced boundary gap

The existing feedback modal binds its composer to the first usable artifact version in the
session task response. It lists multiple versions but does not offer a way to choose which
one the composer targets. The current Git diff is a separate worktree observation; it is
not a frozen copy of that artifact and does not establish that each changed file belongs
to it. The feedback heading currently gives the digest, but does not clearly name the
bound artifact in the same binding row.

In a disposable fixture, a checkpoint recorded `hero.html`, while the current Git diff
contained both that file and `z-other.txt`. A real browser opened the evidence modal and
clicked the latter row. The composer reported `Narrowed to region: z-other.txt` while
still bound to `hero.html`. The probe did **not** submit that unrelated-file selection.
A separate selected submission for `hero.html` reached the real route and was stored as
an element selection, although the UI called it a region. The browser probe intentionally
failed both assertions, reporting 59/61 checks; it is a reproduction, not a passing
qualification report.

The client keeps `locatorKind` and `artifactSha256`, while the route reads `locator_kind`
and `artifact_sha256`; missing values fall back to `element` and the feedback digest.
The backend's supported locator kinds do not include `region`. Thus neither a current diff
path nor that fallback proves a narrower region in the recorded artifact. The route accepts
caller-supplied artifact identifiers and digests; this audit does not establish a server-side
binding to checkpoint membership or current-file integrity. Selection and feedback do not
constitute acceptance or permission to publish.

Tracking: [issue 413](https://github.com/pankajtiwarics27/m5-agent-studio/issues/413).
An [interim mitigation](project-workspace-feedback-scope-mitigation.md) now refuses
worktree-file selection in the composer; the wider issue remains open. Do not paper over
this by assigning an unsupported kind or treating an unrelated file as an element of the
first artifact. A follow-up needs explicit artifact/version selection,
a defensible mapping to supported locators or an honest refusal with plain-text fallback,
and mutation-tested coverage of the actual persisted selection, multi-file switches,
stale responses and drafts. Any server authority or governance change requires separate
review. Until then, the existing selected-file UI is not qualified as precise feedback.
