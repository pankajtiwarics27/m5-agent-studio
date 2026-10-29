# UI redesign summary

## Scope delivered

- `docs/ui-audit.md`: pre-change structure, palette, responsive and test audit.
- `docs/ui-decisions.md`: disclosure, density and token decisions.
- `static/index.html`: Harness advanced settings disclosure, compact/comfortable density preference, less competing primary emphasis, token-based primary text and targeted shell/banner color cleanup.
- `static/index.html`: Council role-model and single-model disclosures, labeled controls and targeted color cleanup.
- `static/index.html`: Compare arbiter model appears only when its verdict feature is enabled, preserving its selection.
- `static/index.html`: Pipeline filters and presets are keyboard-operable, selection is announced, and fields have accessible labels.
- `studio.py`: regenerated standalone artifact.
- `tests/test_ui_disclosure.py`: structural regression checks for essential controls and disclosure.

## Remaining work

This is a first increment, not a complete implementation of the requested app-wide redesign. Pipeline, Chat and dialogs still need screen-by-screen progressive disclosure and feature-adaptive review; Council, Compare and Harness need further review. Most legacy hardcoded colors and contrast failures remain; control borders, primary-action competition on other screens, and real-browser accessibility, keyboard, responsive, and light/dark verification require further work. No feature-dependent controls were changed, because changing their conditions without deeper behavior testing risks altering configuration. Monitor resize behavior still resets visibility. No new high-contrast theme was added.

## Checks

- Audit baseline: frontend syntax and standalone build passed; Ruff and mypy passed after installing dev dependencies. Initial pytest run: 708 passed, 1 skipped, 1 failed because the shallow checkout lacked the pinned baseline commit.
- After the increment: Ruff and mypy passed; frontend syntax and standalone build passed; pytest passed (710 passed, 1 skipped, one dependency deprecation warning) after fetching the missing Git history. The skipped live sandbox test requires explicit opt-in.
- Council increment: Ruff, mypy, frontend syntax and build passed; pytest passed (711 passed, 1 skipped).
- Compare increment: Ruff, mypy, frontend syntax and build passed; pytest passed (712 passed, 1 skipped).
- Pipeline increment: Ruff, mypy, frontend syntax and build passed; pytest passed (713 passed, 1 skipped).
- Invoking `node scripts/test_ui_auth_helper.js` without its required injected HTML input failed; the full pytest suite passed its existing auth coverage. No manual browser, responsive, keyboard, or visual theme verification was performed in this environment; contrast measurements are limited to the pairs recorded in the audit and decisions.
