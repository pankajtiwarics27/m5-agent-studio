---
id: ADR-0028
title: confirmed-studio-host-path-selection
status: accepted
date: 2026-11-07
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0028: Confirmed Studio-host path selection

## Context

Issue #228 requests workspace/system-path selection, confirmed folder creation
and optional model suggestions. Browser directory upload cannot produce a trusted
absolute server path, especially for remote/container hosts. This requires a
separately visible filesystem/runtime boundary, not API expansion hidden in the
Chat protocol UI fix or an agent execution permission.

## Decision

Introduce typed `workspace_paths.py` and three authenticated operator routes:
bounded metadata-only browsing, exact-boolean confirmed single-level mkdir and
exact-boolean confirmed description-only model name suggestion. Document their
[request, configuration, privacy and race contracts](../host-path-picker.md).
Preserve shared bearer/sessionStorage, Host/Origin, rate/body limits, audit and
execution-policy boundaries. Record audit intent before mkdir. No agent tool,
MCP execution or implicit inference/network operation is added.

Use approved home/workspace/recent/operator roots with component-by-component
no-follow directory capabilities; omit hidden/known-secret names, spoofing/control
characters, symlinks and special files. Unsupported platforms fail closed.
Roots configure operator navigation, not a new filesystem sandbox for other APIs.
An opened directory is a capability to that directory even if concurrently renamed;
selection paths can become stale and do not bypass subsequent validation.

Reuse the existing accessible overlay controller. Keep navigation, editable
suggestion, confirmed creation and explicit application separate. Applying a
workspace disables implicit directory creation. MCP insertion changes only an
executable, one argv token or one named environment property in an unsaved draft.
Manual entry remains available, including venv executable symlinks omitted by the
conservative picker. Generated standalone packaging must expose the same boundary.

## Alternatives

- Browser upload/directory selection was rejected as host-path authority: it
  supplies browser file handles/relative paths, not a remote server directory.
- Unrestricted filesystem traversal or following child symlinks was rejected:
  selection needs bounded metadata and a clear fail-closed path policy.
- Automatic model suggestions or creation/application were rejected: inference
  may be billable/outbound and suggested text is untrusted, not user authority.
- Replacing a whole MCP args/environment field was rejected: selecting a path
  must preserve other arguments, quoting and environment settings.
- A safe-fd-unavailable resolve-then-open fallback was rejected because it would
  silently reintroduce a symlink race. Manual entry retains existing behavior.

## Tradeoffs

Home/recent roots permit useful operator navigation but are not tenant isolation;
known-name filtering cannot identify every secret. Bounded listings may omit
matches, and common venv executable symlinks need manual entry. Explicit mkdir
cannot be undone by Cancel after submission. Descriptor-relative operations stay
with the opened directory during renames, so display strings can become stale.
These limits are explicit rather than hidden by unsafe fallback or auto-retry.

## Review Status

Accepted as the implementation decision for issue #228. Not reviewed; independent
review is pending.
Offline module/API, race and browser tests provide author evidence only. Required
multi-version/browser CI and final owner end-to-end acceptance remain separate;
no independent security signature or AS-031 verification is asserted.
