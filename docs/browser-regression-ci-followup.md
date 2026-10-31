# Browser regression CI follow-up

The capture hardening in [#124](https://github.com/pankajtiwarics27/m5-agent-studio/pull/124)
added real-browser tests, but the ordinary CI matrix skips them unless browser
paths are configured. A synthetic review of [#128](https://github.com/pankajtiwarics27/m5-agent-studio/pull/128)
showed why that matters: an early integration of its proposed tab semantics
was incompatible with the capture checks. Its updated head passed the existing
browser capture regressions before merge. The separately reviewed keyboard walker
still disclosed a synthetic token to a cross-origin frame; see
[#130](https://github.com/pankajtiwarics27/m5-agent-studio/issues/130). Neither
finding proves any committed image is incorrect.

The dedicated CI job installs pinned Chromium and puppeteer-core from
`tests/browser/package-lock.json`, extracts the packaged Chromium libraries, and
runs the existing screenshot browser tests using synthetic loopback services and
a demo in disposable source. It sets `M5_REQUIRE_BROWSER_TESTS=1` and uses the
`browser_ci_no_skips` pytest plugin: missing tooling or any skip fails the job.
Ordinary local runs remain opt-in. The lock pins test tooling, not the product's
runtime dependencies; browser releases and their advisories still need maintenance.

This follow-up does not claim the navigation story, fix or test its keyboard walker,
close the screenshot provenance hold [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122),
or modify another session's ledger. CI success does not establish the provenance
of existing images or protect browser tools not covered by these tests.
