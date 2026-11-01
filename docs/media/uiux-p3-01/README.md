# UIUX-P3-01 Harness settings evidence

These captures compare the Harness before and after the P3-01 run-settings
hierarchy change. Every PNG was produced by `scripts/capture_screenshots.mjs`
from a disposable Git worktree with its own empty `.harnesses` store, using the
repository's seeded demo, a generated throwaway bearer token, loopback-only HTTP,
Chromium 153, and `puppeteer-core` 25.16.0. The token is not recorded here. The
demo makes no provider/model calls; no real workspace, model, or credential was
used. The browser checks and captures do not attest to provider-backed execution.

The before set is from the claim commit `e98fe34` before application changes. The
after set is from that same baseline with only the P3-01 frontend change applied.
The full six-pane screenshot set was also regenerated through the same pipeline;
`docs/media/screenshots/harness.png` is the after desktop capture, with its
capture index and manifest entry updated in the same PR.

| Evidence | Viewport | Theme | Captured at (UTC) | SHA-256 |
|---|---:|---|---|---|
| `harness-before-desktop.png` | 1440×900 @2 | dark | 2026-11-01 04:31:42 | `ccd88a5dad5480679b3e3befcb704412af901bc7637c648aeb722c5357b12d43` |
| `harness-before-900x800.png` | 900×800 @2 | dark | 2026-11-01 04:31:52 | `d7a9403f1dcd58af8298cab0d7673ef4c0831057b3ca52a9041af0d8fdb6738e` |
| `harness-after-desktop.png` | 1440×900 @2 | dark | 2026-11-01 04:39:13 | `bb6dea0fe6cbd85f6aace9ac5341525df0f58a8369d817aec9bd97a5f83a16ef` |
| `harness-after-900x800.png` | 900×800 @2 | dark | 2026-11-01 04:38:07 | `2f7d016cb9eb06ca962185274272dd656e9a359cf78cbd8719e0136b7a42964c` |
| `harness-after-light.png` | 1440×900 @2 | light | 2026-11-01 04:38:07 | `95b85a76fee7d3112704dc4d12ac0e0d1c7c15300288ee6f95b57a85dc94a07a` |

The demo shows the expected no-session state. It proves the default values are
visible while `Run settings` is collapsed; `tests/browser/accessibility_walk.mjs`
exercises opening, changing and closing the native disclosure with the keyboard,
verifies the focus indicator and checks that the visible summary follows the
controls. The demo screenshots do not represent an active task, approval, stream,
diff or recovery state; those backend flows remain unchanged and are covered by
their existing tests.
