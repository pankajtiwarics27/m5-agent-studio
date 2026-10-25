# M5 Agent Studio — Interface Design System

**Status:** Active visual system for `static/index.html`.
**Purpose:** A calm, high-clarity developer workbench. This file is the source of truth for visual tokens and interaction styling; it does not redefine application behavior or backend contracts.

## Product principles

1. **Task first:** make the current workflow, status, and next action obvious before exposing implementation detail.
2. **Neutral by default:** use surfaces and typography for hierarchy. Reserve color for focus, selection, and meaningful status.
3. **Consistent shell, distinct workflows:** navigation, headers, controls, and states behave consistently across Harness, Council, Pipeline, Compare, and Chat.
4. **Progressive disclosure, not feature removal:** specialist options may be grouped, but every existing action remains reachable by visible navigation or the command palette.
5. **Local-first reliability:** core layout and typography work without a network connection or external font/CDN request.
6. **Safety is part of design:** approvals, credential handling, diff review, and worktree application stay explicit and inspectable.

## Design foundations

### Themes and semantic color

Provide a readable neutral dark theme and a soft neutral light theme. Default dark is charcoal, not OLED black. Persist explicit theme choice in local storage. Never apply accent colors as glow, gradient, or category decoration.

| Semantic token | Dark | Light | Usage |
|---|---|---|---|
| Canvas | `#151719` | `#F7F7F5` | App background |
| Raised surface | `#1B1E21` | `#FFFFFF` | Header, sidebar, controls |
| Card surface | `#202428` | `#FFFFFF` | Meaningful grouped content |
| Hover surface | `#292E33` | `#EFF0ED` | Hover and selected neutral surface |
| Code surface | `#111315` | `#F1F2EF` | Code/log panels |
| Border | `#34393E` | `#D9DCD8` | Dividers and control edges |
| Strong border | `#525A62` | `#B7BCB7` | Emphasis and separators |
| Primary text | `#E9EAEC` | `#202326` | Body and headings |
| Secondary text | `#B0B5BA` | `#555C62` | Explanatory copy |
| Muted text | `#969DA4` | `#687077` | Metadata (minimum 12px) |
| Accent | `#5969D8` | `#5969D8` | Primary action, selected marker, focus |

Status semantic colors: muted green for success, amber for warning/pending approval, red for destructive/error, and neutral gray for idle/unknown. Every status also has a text label or accessible name; hue alone must not convey state. Validate text contrast to WCAG 2.2 AA (4.5:1 normal text, 3:1 large text) and test UI boundaries/focus rings in both themes before release. The token values are implementation starting points, not a substitute for measured contrast checks.

CSS should reference semantic custom properties, not introduce new one-off hex values. Color-mix may be used for subtle state backgrounds only with a tested solid fallback where required by supported browser versions.

### Typography

- UI/body stack: `Inter, "IBM Plex Sans", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif`.
- Code/data stack: `"JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace`.
- No required external font fetch. If custom fonts are added, vendor or package locally and validate licensing, coverage, and offline startup.
- Body 14px default, 1.45–1.6 line-height; form fields 13–14px; secondary metadata at least 12px; section titles 18–20px; page titles 22–26px.
- Use 400/500/600 weights. Use monospace only for source, paths, identifiers, and diagnostic values. Use tabular figures for counters.
- Sentence case for form labels and section names. Avoid tiny all-caps labels and bold-everywhere styling.

### Spacing, sizing, and layout

- 4px spacing grid; common values: 4, 8, 12, 16, 24, 32, 40px.
- Desktop content gutters 20–24px; tablet 16px; mobile 12–16px.
- Inputs/buttons 36–40px high; touch targets at least 44px where practical. Compact secondary controls must remain comfortably operable.
- Use one surface per meaningful group. Prefer whitespace and alignment over nested borders/cards.
- Shared application shell: fixed global header, persistent mode navigation on wide screens, central workspace, optional monitor/inspector rail. At narrow widths, retain navigation and rail access via the horizontal mode row and reachable monitor drawer.
- Reflow at content-driven breakpoints; test 1280, 1024, 768, 390, and 320 CSS px and 200% zoom.

## Component rules

### Navigation and palette

- All five workflows stay visible and named: Harness, Council, Pipeline, Compare, Chat.
- Active state uses a stable background/marker and text; hover and keyboard focus are separate states.
- Keep Alt+1…Alt+5 and Cmd/Ctrl+K. Palette supports mouse and keyboard, groups navigation/actions/tools, exposes shortcuts, and returns focus to its trigger.

### Buttons and fields

- One primary action in a local context; neutral secondary; quiet tertiary; explicit destructive action.
- Do not use module-specific button hues. Green/amber/red are reserved for genuinely meaningful statuses and decisions.
- Fields have persistent labels, concise help, adjacent validation, visible focus, and distinguishable disabled/loading/error states. Secret values stay masked.
- Icon-only controls need an accessible name and tooltip on both pointer hover and keyboard focus.

### Cards, status, logs, and output

- Cards use a restrained surface, consistent 7–9px radius, simple border, no decorative glow, and no hover lift/scale.
- Status tokens cover idle, queued, running, awaiting approval, success, failed, cancelled, disconnected, and stale. Pair status icon and text with color.
- Preserve code/log whitespace, copy affordances, file/model labels, and horizontal scrolling. Do not reduce code type to fit a pane.
- Keep loading, empty, error, and complete states explicit. A toast is never the only notice for a consequential error or pending approval.

### Dialogs, drawers, and focus

- Modal dialogs are for short interruptions/confirmations; drawers are for contextual inspection; long configuration remains a roomy, structured surface.
- Dialogs/drawers require title/description, contained scrolling, visible close control, focus management, Escape handling, and focus return.
- Do not hide a required control on small screens. Reflow or provide a persistent drawer/sheet entry point.

### Icons and motion

- Use one bundled outline SVG icon family, 16px standard/18px navigation, consistent 1.75–2px stroke. Do not use emoji as interface icons.
- Short interaction transitions: 120–180ms ease-out; meaningful panel transitions at most 240ms. No animated dashboard choreography, pulsing glows, bounce, or parallax.
- Streaming must preserve scroll intent and not cause repeated layout jumps. Honor `prefers-reduced-motion` for all motion.

## Behavioral compatibility rules

- Styling work does not change routes, API calls, provider/MCP security, approval policy, persistence, local hosting, or model behavior.
- Keep existing pane IDs, selectors, handlers, keyboard shortcuts, event-stream behavior, and status announcements during visual migration.
- If a later component extraction needs a DOM/handler change, maintain an explicit compatibility map and regression test every affected action in the same change.
- Relocated tools must have a visible route and a palette entry; no feature may become palette-only.

## Release checklist

- [ ] Both themes work offline, are legible, and persist preference.
- [ ] Contrast and focus reviewed on normal, hover, selected, disabled, warning, error, and modal states.
- [ ] 5 modes and every tool/configuration action remain discoverable and operable.
- [ ] Keyboard, screen-reader labels/live status, reduced motion, 200% zoom, and narrow-width access pass.
- [ ] No gradient/glow decoration, tiny default labels, or unexplained acronym-heavy screen titles remain.
- [ ] Frontend syntax, deterministic standalone build, repository tests, route matrix, and manual workflow regression suite pass.
