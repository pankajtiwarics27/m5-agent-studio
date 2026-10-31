# Screenshot capture safety follow-up

This branch investigates [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122),
independently of story ownership and the preservation-signoff record. The current
capture tool can accept a non-Studio page as a full capture and seed the demo
bearer token into cross-origin frames. A passing manifest or checksum confirms
file consistency, not rendered pane identity or authentication. These findings
do not establish that any existing screenshot is fake.

The planned remediation is to fail closed on unexpected navigation, invalid
authentication, missing tabs and invisible/mismatched panes, and to restrict
token setup to the intended main document. Exercise both success and failure
paths with synthetic tokens and browser-backed tests. Continue using disposable
source and a throwaway demo environment. Existing captures remain historical
until separately reviewed; this work does not claim or sign off a UI/UX story.
