# The navigation shell (`UIUX-P1-02`)

The shell is one page with six panes behind a rail. This page records the contract
the rail now keeps, the defect that tracing it turned up, and how the keyboard half
is checked — because "it works with a keyboard" is the one acceptance criterion a
reviewer cannot confirm from a diff.

## The contract

| Element | Contract |
|---|---|
| `.mode-tabs` | `role="tablist"`, `aria-label`, `aria-orientation` that follows the layout (vertical above 1100px, horizontal below) |
| `.mode-tab` | `role="tab"`, `id="tab-<pane>"`, `aria-controls="pane-<pane>"`, `aria-selected`, `data-shortcut`, roving `tabindex` |
| `section.pane` | `role="tabpanel"`, `aria-labelledby="tab-<pane>"` |
| `data-tab` / pane ids | unchanged — the contract every handler, test and screenshot already used |

The rail used `aria-current="page"` and no tab semantics, which told assistive
technology that these were six buttons in a menu rather than six views of one
application. It also left all six tabs in the tab order, so a keyboard user paid six
presses to cross a rail that already had arrow keys.

## Keyboard map

| Key | Effect |
|---|---|
| `Tab` | one stop in the rail: the selected tab |
| `ArrowDown` / `ArrowRight` | next tab, wrapping at the end |
| `ArrowUp` / `ArrowLeft` | previous tab, wrapping at the start |
| `Home` / `End` | first / last tab |
| `Alt+1` … `Alt+6` | jump to a tab from anywhere |
| `⌘K` / `Ctrl+K` | command palette (unchanged) |

Activation follows focus. In a six-item rail, requiring `Enter` after an arrow is a
second keystroke that buys nothing, and the pane switch is instant and local.

Both arrow axes work regardless of orientation, because the rail is vertical on a
desktop and a horizontal strip on a narrow window, and a keyboard user should not
have to know which breakpoint they are on.

## The defect this story found

`Alt+6` was advertised on the Start tab's title and in the ledger's `tab_registry`,
but the key handler carried its own map of keys 1–5. Start — the first-run pane, the
one a new user needs — was the only unreachable-by-shortcut tab, and every document
said otherwise.

The handler now reads `data-shortcut` off the tabs, so the shortcut a user reads and
the shortcut that runs are the same attribute, and `tests/test_navigation_shell.py`
checks the rail, the titles and the ledger registry against each other.

## Checking it in a browser

`scripts/keyboard_walk.mjs` drives real key events through a real browser and asserts
22 things: tablist role, exactly one selected and one tabbable tab, `Tab` reaching
that tab and leaving the rail on the next press, both arrow axes, `Home`/`End`,
wrapping, a visible focus ring, all six `Alt+N` shortcuts, and no page errors. It
fails loudly if no browser is available rather than reporting a pass it did not earn.

```bash
python3 scripts/demo_tour.py --serve
STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
STUDIO_PUPPETEER_CORE=node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
  node scripts/keyboard_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
```

Result recorded for this story: **22 of 22 checks passed**.

## What this page does not claim

* **No routes, handlers or pane identities changed.** The route map is unchanged at
  53 call sites and 0 findings; the control inventory is unchanged at 170 markup
  controls and 30 rendered by script.
* **The rendering did not change.** With the stylesheet and markup swapped between
  captures on one running instance, all six panes are byte-identical before and
  after, so the committed baseline screenshots were neither re-shot nor edited.
* **Screen-reader support is asserted, not heard.** The attributes are the standard
  tablist pattern and the walk proves the focus behaviour, but nobody listened to a
  screen reader in this environment. Listening is part of the P5 accessibility gate,
  not a claim made here.
* **Narrow layouts were exercised by the walk's 1440px viewport only.** The
  orientation attribute follows a media query, and that transition is read from the
  code, not driven.
