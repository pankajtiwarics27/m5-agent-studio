# Browser regression CI follow-up

The capture hardening in [#124](https://github.com/pankajtiwarics27/m5-agent-studio/pull/124)
added real-browser tests, but the ordinary CI matrix skips them unless browser
paths are configured. A synthetic review of [#128](https://github.com/pankajtiwarics27/m5-agent-studio/pull/128)
showed why that matters: its proposed tab semantics are incompatible with the
current capture checks, while its new keyboard walker also sends the token to a
cross-origin frame. Neither finding proves any committed image is incorrect.

The dedicated CI job installs pinned Chromium and puppeteer-core from
`tests/browser/package-lock.json`, extracts the packaged Chromium libraries, and
runs the existing screenshot browser tests using synthetic loopback services and
a demo in disposable source. It sets `M5_REQUIRE_BROWSER_TESTS=1` and uses the
`browser_ci_no_skips` pytest plugin: missing tooling or any skip fails the job.
Ordinary local runs remain opt-in. The lock pins test tooling, not the product's
runtime dependencies; browser releases and their advisories still need maintenance.

This follow-up does not claim the navigation story, review its keyboard walker,
close the screenshot provenance hold [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122),
or modify another session's ledger. CI success does not establish the provenance
of existing images or protect browser tools not covered by these tests.
