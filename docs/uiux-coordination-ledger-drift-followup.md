# UI/UX coordination ledger drift follow-up

At `582baaf`, `python3 scripts/uiux_coordination.py --github --json` fails
closed before checking GitHub: the P0 and P1 phase percentages disagree with
story-level counts. This follow-up reconciles derived phase summaries and adds
regressions. It does not claim `UIUX-P1-03`, change any story tracking, or make
a release or owner-approval decision. Recheck GitHub and the ledger after the
follow-up merges before claiming a story.

The preservation package distinguishes the historical owner signature from
current inputs; the recorded decision must remain unchanged, and generated
current-input hashes must not be represented as a fresh owner approval. The
remaining safety and provenance holds require separate review.

## Concurrent P1-03 review finding

Open PR #132 claims `UIUX-P1-03`. Its new `scripts/status_walk.mjs` seeds the
provided API token in every document frame and allows arbitrary base URLs and
navigation. A synthetic two-origin browser reproduction confirmed the foreign
iframe receives the synthetic token before the walk exits on a deliberately
non-Studio page. The finding is posted on #132; no real token or checkout store
was used. Do not use that walker with a real token. Require the same top-frame,
loopback, authentication and navigation checks as the keyboard walker, with
mandatory browser regressions, before treating its browser evidence as safe.

This is not an ownership claim or implementation of P1-03. The branch's owner
must coordinate the overlapping ledger updates and address the security finding;
no subsequent story is ready to claim until P1-03 is resolved and merged.
