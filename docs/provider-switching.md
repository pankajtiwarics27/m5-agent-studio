# Provider switching without workflow rewrite

AS-054 defines `switch_provider()` in
[`provider_switching.py`](../provider_switching.py). It compares normalized
request semantics—streaming, tools, structured output, and reasoning—with the
target provider's declared capabilities.

Supported switches preserve the request semantics exactly. Unsupported target
features are returned explicitly in `ProviderSwitchDecision`; the contract does
not silently drop tools, disable streaming, rewrite prompts, or make a
billable/network request.

## Evidence

`tests/test_provider_switching.py` covers fully compatible switches, explicit
unsupported features, semantic preservation, and invalid input rejection.
