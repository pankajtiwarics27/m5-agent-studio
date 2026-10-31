# Keyboard walk safety follow-up

Issue [#130](https://github.com/pankajtiwarics27/m5-agent-studio/issues/130)
tracks a synthetic cross-origin token disclosure in the keyboard walker merged by
[#128](https://github.com/pankajtiwarics27/m5-agent-studio/pull/128). This
follow-up is scoped to the walker and its tests, not the navigation story or the
historical screenshots. Do not run the unpatched walker with a real token.

The intended safeguards are loopback demo origins, early token seeding only in the
expected top-level document, rejection of redirects and unexpected navigation,
authentication checks, and mandatory browser regressions with disposable inputs.
A loopback service can still be malicious or compromised: these checks cannot
establish server identity. Use only throwaway demo tokens and an isolated demo.
Independent review is required before treating the issue as resolved.
