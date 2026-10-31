# Screenshot capture safety follow-up

This branch investigates [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122),
independently of story ownership and the preservation-signoff record. The current
capture tool can accept a non-Studio page as a full capture and seed the demo
bearer token into cross-origin frames. A passing manifest or checksum confirms
file consistency, not rendered pane identity or authentication. These findings
do not establish that any existing screenshot is fake.

The capture tool now checks a loopback demo's authentication boundary and
browser-authenticated API response, rejects main-document redirects and unexpected
navigation, seeds the token only in the expected main frame, and verifies each tab
and visible active pane before writing captures. Browser regressions cover the real
isolated demo, synthetic invalid pages and cross-origin frames. The tests are
opt-in when a Chromium binary and puppeteer-core are available.

These checks do not authenticate the identity of an untrusted local service or
prove historical image provenance. Publication may be partial if filesystem writes
fail; the validator checks file consistency, not pane identity. Continue using
disposable source and a throwaway demo environment. Existing captures remain
historical until separately reviewed; this work does not claim or sign off a
UI/UX story. Regeneration updates the current manifest hash reported in the
preservation package, not the owner's historical signature. That signature must
not be construed as approval of these new input hashes; [#126](https://github.com/pankajtiwarics27/m5-agent-studio/issues/126)
tracks the missing binding between the decision and its approved inputs.
