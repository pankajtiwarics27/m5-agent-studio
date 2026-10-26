# Cloud consent and fallback policy

AS-021 defines the provider-neutral fallback policy in
[`routing_policy.py`](../routing_policy.py). Local candidates are eligible by
default; network and billable candidates require both an operator policy and
per-request explicit consent identified by `consent_id`.

`evaluate_fallback()` only evaluates an ordered candidate list. It does not
make a network request, incur a charge, retry a provider, or silently switch a
model. If no candidate is allowed, the result contains explicit exclusions and
`require_selection()` raises `FallbackDenied`.

## Evidence

`tests/test_routing_policy.py` covers local-first defaults, policy denial,
missing consent, combined network/billing approval, provider allowlists,
malformed consent, and no-selection failure behavior.
