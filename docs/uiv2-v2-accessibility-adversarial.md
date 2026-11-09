# UIV2-28 — accessibility, scroll and responsive adversarial suite

Measured runtime accessibility and recovery evidence, not a screen-reader certification or a cosmetic-only completion.

## Gate and evidence policy

- Real Chromium input drives six panes in light and dark; no focus loop is accepted outside an intentional modal.
- Contrast is computed with WCAG relative luminance. A gradient/image or otherwise unmeasured background fails instead of being inferred from tokens.
- The 200% matrix uses half the CSS viewport and `deviceScaleFactor=2` at 1640×900, 1440×900, 1280×800 and 390×844, in both themes. Every control must remain reachable and at least 24 CSS px.
- Reduced motion is emulated with `prefers-reduced-motion: reduce`. Accessible-name checks support screen-reader semantics but are not a claim of screen-reader certification.
- The isolated fixture requires bearer auth, uses a synthetic token, blocks outbound requests and never contacts a provider or user workspace.
- PNGs and raw capture manifests are local evidence. The committed run record binds check results and capture SHA-256 digests to exact source digests; CI runs the browser behavior again.

## Imported browser run

**120/120 checks passed; 62 captures; 0 failures.**

Run Git head: `b6134c56aac555379685a300247021a3dcc913c2`; dirty worktree during evidence capture: `true`.

Local after evidence: `/tmp/uiv2-28-adversarial-after/capture-manifest.json` and its 62 PNGs. Original baselines are `/tmp/uiv2-28-before-a11y.log`, `/tmp/uiv2-28-before-responsive.log`, and `/tmp/uiv2-28-contrast-before.json`; these `/tmp` paths are review evidence, not release inputs.

The final long-recovery pass added four checks beyond the initial 116-check matrix: nonzero nested offsets and scrolling to the modal's final marker at each of two 200% recovery variants.

## Measured defects and repairs

| Control / region | Reproduced before | Repaired contract |
| --- | --- | --- |
| `dark brand chip` | 2.41:1 computed contrast | explicit var(--primary-text) foreground; every final rendered pair passes |
| `#harnessProjectSummary` | 113×18 CSS px | 24px minimum target height; responsive walk passes |
| `#hallClearSessionBtn` | right=430px at a 360px viewport | contained horizontal action scrolling; the control remains reachable |
| `#chatPersona` | 20×44 CSS px at 200% phone zoom | 140px group minimum wraps only when two columns cannot fit |
| `#monitorToggleBtn` | clipped at a 195px effective viewport | the header is one row and scrolls horizontally at narrow widths, so the toggle stays reachable at 200%; #hallClearSessionBtn uses the same contained-scroll remedy |
| `#startTaskInput` | 220px fixed minimum caused horizontal overflow | zero flex minimum with the desktop basis retained |
| `Start / Council / Chat recovery` | short-height content had no reachable scroll path | native Start-shell and Council/Chat main overflow recovery |

## Matrix accounting

| Group | Checks | Captures |
| --- | ---: | ---: |
| `rendered` | 36 | 12 |
| `keyboard` | 12 | 0 |
| `zoom-200` | 48 | 48 |
| `long-recovery` | 12 | 2 |
| `browser` | 12 | 0 |
| **Total** | **120** | **62** |

At desktop 200%, `hallStream` is the outer scroller. At phone 200%, `.hall-main` takes that role while the transcript grows with its content. The walk records the active host, starts with nonzero outer and pinned-conclusion offsets, scrolls the dialog to `uiv2-28-conclusion-end`, and requires Escape to restore the Expand trigger and every original offset exactly.

## Empty, populated, loading, error and recovery coverage

The adversarial walk supplies the common seeded state and route-created long/recovery state. The other real states retain their specialist producers from the approved cross-tab contract; the preservation suite runs them rather than painting states into screenshots.

| Pane | State | Producer |
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

## Visual approval and preservation

The UIV2-26 48-variant actual/reference/annotated matrix remains mandatory. Accessibility changes that alter the approved structural signature require a fresh candidate and explicit human approval; a passing accessibility walk cannot approve its own pixels.

The source-derived UIV2-02 control/API map (currently 273 controls and 121 mounted routes) and the UIV2-27 21-row preservation run remain mandatory. No backend route, payload, session/workspace rule, authorization, sandbox, action-ledger, lease-fencing or model-memory boundary is changed by this story.

## Reproduce

```bash
node scripts/uiv2_accessibility_adversarial_walk.mjs --screenshots /tmp/uiv2-28-adversarial-after
python3 scripts/uiv2_accessibility_adversarial.py --import-evidence /tmp/uiv2-28-adversarial-after/capture-manifest.json
python3 scripts/uiv2_accessibility_adversarial.py --check
node scripts/a11y_walk.mjs --screenshots /tmp/uiv2-28-final-a11y
node scripts/responsive_walk.mjs --screenshots /tmp/uiv2-28-final-responsive
node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out /tmp/uiv2-28-cross-tab-after --timeout-ms 60000
```

Browser environment and pinned Chromium setup are the same as `.github/workflows/ci.yml`. The browser-safety CI job runs this walk plus the legacy keyboard and responsive gates.

Contract findings: **0**.
