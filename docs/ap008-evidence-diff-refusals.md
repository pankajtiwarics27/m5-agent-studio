# AP-008: truthful evidence diff refusals

## Scope and risk

Harness verification evidence uses the existing session-scoped `/diff` route. That route can return a non-Git response or an `is_git: true` payload whose `diff` contains a Git error and whose empty status is not a clean workspace. The evidence modal currently interprets these as a clean worktree. It can also say no uncommitted change when Git status lists changes whose patch could not be included. This slice corrects only that presentation; it changes no API, privilege, persistence, grant, preview sandbox, run control, or other pane. Untrusted server text remains escaped. The current diff is never described as frozen at a verification verdict.

Puppeteer exposed a producer defect: `history_privacy.redact_text` returns `(text, count)`, but `_compute_git_diff` was passing the tuple as file content and returning a tuple instead of the intended redacted string. This prevented the existing preview from showing real diffs and made a security test accidentally check tuple membership instead of inspecting the text. Correct the existing producer to unpack the redacted text at both uses, retaining existing confinement, secret filtering, Git hardening, redaction and 50,000-character limit. This corrects a broken existing response shape and does not add an endpoint or authority; consumers expecting the accidental two-element JSON array could be affected. Validate existing consumers, tracked and untracked redaction, and malicious Git/config/symlink handling. Roll back producer and consumer together if needed.

Alternatives rejected: treating every empty diff as clean, hiding an error in a success state, normalizing the erroneous tuple in the client, or adding a new workspace read route. Existing `_compute_git_diff` is authoritative for what it returns, and its bounded/truncated output cannot prove absence of all changes. Session switching and loading remain handled by the existing evidence flow; this slice does not change it.

## Verification and rollback

Focused JavaScript-executing tests cover Git error, non-Git, malformed response, no patch despite dirty status, clean status, and hostile error text; existing Harness evidence and browser tests protect the modal and six-pane surface. Regenerate source-bound assets with their owning scripts and repeat Puppeteer regressions. Roll back the presentation and generated artifacts to revert; there is no data migration.
