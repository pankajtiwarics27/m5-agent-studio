# UIV2-26 — cross-tab visual regression reference gate

The actual app and the explicitly approved UIV2-03 wireframe are captured at the same six-tab viewport/theme matrix. Pixel differences are measured and annotated, never hidden by a blanket tolerance. Independent structural rules must pass, and future --check runs also require a live structural baseline approved by a human. New PNGs are local evidence, not Git inputs.

## Matrix and evidence policy

- Six tabs: Start, Harness, Council, Pipeline, Compare and Chat.
- Four viewports: 1640×900, 1440×900, 1280×800 and 390×844; light and dark separately.
- Every variant produces `actual.png`, `reference.png` and an annotated `comparison.png`: **48 of each, 144 PNGs total**.
- PNGs and candidate manifests are Git-ignored local evidence. The committed approval stores structural signatures and SHA-256 digests only.
- Pixel RGB delta and changed-pixel ratio are reported for review. There is deliberately **no pixel-tolerance pass** because the approved target is structural, not a pixel clone.

## Independent layout contract

- Header ≤64px; desktop nav 192–224px; visible desktop inspector 272–320px; task region ≥640px where the desktop reference requires it.
- Exactly one selected tab and visible pane; all six tabs remain reachable; no document horizontal overflow; telemetry starts as a closed popover.
- Pane-specific selectors, DOM order and counts are asserted in the running app, not inferred from source inspection.

## Authored differences shown on every comparison

| Pane | ID | Difference | Reference intent | Live, feature-preserving behavior |
| --- | --- | --- | --- | --- |
| Start | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Start | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Start | D3 | Command-center evidence follows the action | One next action and one collapsed status line. | The task and one blocker lead; recent work and measured diagnostics remain below them in native disclosures. |
| Harness | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Harness | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Harness | D3 | A unified task composer leads the empty state | A populated Plan → Execute → Verify cockpit with timeline and composer. | A short greeting and one composer keep lead model, policy, workflow and Run visible. The three stages return for a real active task; settings and run details disclose on demand. Sessions share the desktop navigation column and use a dismissible narrow-screen panel. |
| Council | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Council | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Council | D3 | Readiness precedes deliberation | Sessions, transcript, pinned conclusion and Clear session. | The same session and transcript regions also retain role readiness and model evidence before the composer; no fabricated conclusion is shown. |
| Pipeline | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Pipeline | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Pipeline | D3 | The real four-stage output replaces the placeholder | A stage-focused canvas with artifacts. | Task, All stages plus exactly four stages, selected output, observed activity and factual artifacts fill the canvas; empty states stay empty. |
| Compare | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Compare | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Compare | D3 | Model and criteria controls stay explicit | Question first, balanced results, evidence in the inspector. | The question leads; model choices and a closed criteria disclosure follow before the result region, preserving the actual run contract. |
| Chat | D1 | On-demand tools and integrations | The conceptual wireframe leaves the integrations row out. | One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow. |
| Chat | D2 | Preserved global actions and explicit Inspector | Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent. | Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action. |
| Chat | D3 | Conversation controls remain above the focused column | A focused single conversation with every registered persona available. | Model, registry-driven persona, reasoning, explicit tool protocol and read-only tool controls remain above the focused transcript and composer; the generic inspector stays collapsed for Chat, and the demo shows its real registry size. |

## Existing state and interaction support

The cross-tab matrix is the common empty/seeded state. Populated, long-content, loading, error and recovery remain real product states exercised by their existing walks; no state is painted into a screenshot.

| Pane | State | Existing producer |
| --- | --- | --- |
| Start | empty | `scripts/start_command_center_walk.mjs` |
| Start | populated | `scripts/start_command_center_walk.mjs` |
| Start | long-content | `scripts/start_command_center_walk.mjs` |
| Start | loading | `scripts/start_command_center_walk.mjs` |
| Start | error | `scripts/start_command_center_walk.mjs` |
| Start | recovery | `scripts/start_command_center_walk.mjs` |
| Harness | empty | `scripts/harness_settings_walk.mjs` |
| Harness | populated | `scripts/harness_timeline_walk.mjs` |
| Harness | long-content | `scripts/harness_evidence_walk.mjs` |
| Harness | loading | `scripts/harness_timeline_walk.mjs` |
| Harness | error | `scripts/harness_recovery_walk.mjs` |
| Harness | recovery | `scripts/harness_recovery_walk.mjs` |
| Council | empty | `scripts/council_state_walk.mjs` |
| Council | populated | `scripts/council_deliberation_walk.mjs` |
| Council | long-content | `scripts/council_deliberation_walk.mjs` |
| Council | loading | `scripts/council_state_walk.mjs` |
| Council | error | `scripts/council_state_walk.mjs` |
| Council | recovery | `scripts/council_state_walk.mjs` |
| Pipeline | empty | `scripts/pipeline_workspace_walk.mjs` |
| Pipeline | populated | `scripts/pipeline_workspace_walk.mjs` |
| Pipeline | long-content | `scripts/pipeline_workspace_walk.mjs` |
| Pipeline | loading | `scripts/pipeline_workspace_walk.mjs` |
| Pipeline | error | `scripts/pipeline_workspace_walk.mjs` |
| Pipeline | recovery | `scripts/pipeline_workspace_walk.mjs` |
| Compare | empty | `scripts/compare_balanced_walk.mjs` |
| Compare | populated | `scripts/compare_evidence_walk.mjs` |
| Compare | long-content | `scripts/compare_provenance_walk.mjs` |
| Compare | loading | `scripts/compare_evidence_walk.mjs` |
| Compare | error | `scripts/compare_history_walk.mjs` |
| Compare | recovery | `scripts/compare_continuity_walk.mjs` |
| Chat | empty | `scripts/chat_layout_walk.mjs` |
| Chat | populated | `scripts/chat_layout_walk.mjs` |
| Chat | long-content | `scripts/chat_layout_walk.mjs` |
| Chat | loading | `scripts/chat_layout_walk.mjs` |
| Chat | error | `scripts/chat_layout_walk.mjs` |
| Chat | recovery | `scripts/chat_layout_walk.mjs` |

## Human structural-fidelity approval

**Approved.** `user:Arena review 2026-11-09 "approved the re-captured phone Hall baseline; re-capture the two 390x844 hall captures"` at `2026-11-09T17:22:37.569589+00:00`.

- Candidate: `/home/runner/work/_temp/candidate/candidate-manifest.json`
- Candidate SHA-256: `e69a16a6a2b1c5711a77c1798b956ef67f418a46f9b835af96c6a99a6a778bea`
- HEAD: `99fada2a21e4a2e9d8b59dcd8ead09e467adad76`

Future `--check` runs compare all 48 live structural signatures with this approval and still rerun every independent reference/layout assertion.

## Reproduce

```bash
node scripts/uiux_v2_cross_tab_reference_walk.mjs --candidate --out docs/media/cross-tab-reference-uiv2-26/candidate
python3 scripts/uiux_v2_cross_tab_reference.py --validate-candidate docs/media/cross-tab-reference-uiv2-26/candidate/candidate-manifest.json
# After approval exists:
node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out docs/media/cross-tab-reference-uiv2-26/check
python3 scripts/uiux_v2_cross_tab_reference.py --check
```

The walk starts and stops its own isolated `scripts/demo_tour.py --serve` instance, requires bearer authentication, blocks unexpected navigation, and fails on browser page errors or partial captures.

Contract findings: **0**.
