# AP-008: session verification-file disclosure

## Scope and authority

The session `/state` response already contains a durable verification contract (`verification_spec`) with `required_files` and `required_modified_files`. The Harness workspace review currently hides these session-specific details behind the separate evidence modal. Add a read-only, on-demand summary in the existing disclosure using that same snapshot. A declared file is a contract requirement, not proof of its existence, modification, artifact version, validation or approval. Artifact source references and artifact versions still have no integrated session-scoped route and must not be invented; the existing diff is current and inert, not frozen at a verdict.

Do not add a file read, route, state mutation, new authority, data migration, network call or active preview. Do not use the global artifact lineage summary as a session-owned artifact listing. Render paths as text only and bound displayed entries. Show missing contract, invalid/unavailable snapshot and stale status honestly. Session selection/reset and resync continue to use the existing session snapshot and fences. Other tabs, models, tools, Settings, approvals, recovery actions and composer are unchanged.

## Checks, risk and rollback

Tests cover populated, empty, malformed, stale, foreign-session, and hostile text; Puppeteer checks the real state route and rendered disclosure, keyboard access and narrow/zoom layouts. Run existing route/inventory generators, Harness regressions, visual/accessibility and source-bound release gates. Risks include presenting declared requirements as results or leaking a previous session's data; use explicit language and the existing active-session identity check. Rollback is to revert this slice and regenerate inventories; no persisted data changes.
