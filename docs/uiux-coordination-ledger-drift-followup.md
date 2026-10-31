# UI/UX coordination ledger drift follow-up

At `582baaf`, `python3 scripts/uiux_coordination.py --github --json` failed
closed before checking GitHub: the P0 and P1 phase summaries disagreed with
story-level counts. PR #132 has now merged `UIUX-P1-03`; this follow-up reconciles
only the derived phase summaries (P0 complete, P1 60% in progress) and documents
the associated security follow-up. It does not change any story tracking or make
a release or owner-approval decision. Recheck GitHub and the ledger after this
follow-up merges before claiming the next story.

The preservation package distinguishes the historical owner signature from
current inputs; the recorded decision must remain unchanged, and generated
current-input hashes must not be represented as a fresh owner approval. The
remaining safety and provenance holds require separate review.

## P1-03 security follow-up

PR #132 merged `UIUX-P1-03`, but its new `scripts/status_walk.mjs` seeds the
provided API token in every document frame and allows arbitrary base URLs and
navigation. A synthetic two-origin browser reproduction confirmed the foreign
iframe receives the synthetic token before the walk exits on a deliberately
non-Studio page. The finding is posted on the merged PR; no real token or checkout
store was used. Do not use that walker with a real token. Require the same
top-frame, loopback, authentication and navigation checks as the keyboard walker,
with mandatory browser regressions, before treating its browser evidence as safe.

This follow-up does not claim or change P1-03 tracking. The story is marked done
by #132, but that merge did not resolve the separately reported token exposure.
The next story must be selected using a fresh coordinator run and review of open
safety risks.
