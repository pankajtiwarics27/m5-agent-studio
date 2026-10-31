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
