# Keyboard walk safety follow-up

Issue [#130](https://github.com/pankajtiwarics27/m5-agent-studio/issues/130)
tracks a synthetic cross-origin token disclosure in the keyboard walker merged by
[#128](https://github.com/pankajtiwarics27/m5-agent-studio/pull/128). This
follow-up is scoped to the walker and its tests, not the navigation story or the
historical screenshots. Do not run the unpatched walker with a real token.

The walker accepts only loopback demo origins, seeds the token only in the expected
top-level document, checks the API's authenticated and unauthenticated responses,
and rejects document redirects and unexpected main-frame navigation. Mandatory CI
browser regressions exercise a cross-origin iframe, redirects, authentication
failures and the real isolated demo's keyboard walk. All test tokens are synthetic.

A loopback service can still be malicious or compromised: these checks cannot
establish server identity or prove a token is throwaway. Same-origin code can access
its own session storage; the command-line token may also be visible to other local
processes. Use only throwaway demo tokens and an isolated demo. Independent review
is required before treating the issue as resolved.
