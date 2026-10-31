# Browser regression CI follow-up

The capture hardening in [#124](https://github.com/pankajtiwarics27/m5-agent-studio/pull/124)
added real-browser tests, but the ordinary CI matrix skips them unless browser
paths are configured. A synthetic review of [#128](https://github.com/pankajtiwarics27/m5-agent-studio/pull/128)
showed why that matters: its proposed tab semantics are incompatible with the
current capture checks, while its new keyboard walker also sends the token to a
cross-origin frame. Neither finding proves any committed image is incorrect.

This follow-up will investigate a dedicated, reproducible CI job for the existing
browser regressions. It does not claim the navigation story, close the screenshot
provenance hold [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122),
or modify another session's ledger or worktree. Tests must use synthetic tokens,
loopback servers and disposable source; missing browser dependencies must fail CI,
not silently produce a green skipped run.
