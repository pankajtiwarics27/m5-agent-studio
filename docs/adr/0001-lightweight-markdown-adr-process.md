---
id: ADR-0001
title: lightweight-markdown-adr-process
status: accepted
date: 2026-10-26
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0001: Adopt a lightweight Markdown ADR process

## Context

Before this record, the repository had no ADR directory, template, or durable review-status convention. Important choices affect API and storage compatibility, local versus remote integrations, privacy, and secure execution, so future maintainers need a reviewable history of context, alternatives, and tradeoffs. The project already uses Markdown documentation and source-linked inventory files; no dedicated ADR service or runtime integration is present.

## Decision

Adopt numbered, version-controlled Markdown ADRs in `docs/adr/`, using `0000-template.md` and the metadata and section requirements in `docs/adr/README.md`. Each ADR records decision status independently from review status. Accepted decisions that later change are superseded by a new ADR rather than silently rewritten. This process is accepted as the AS-005 repository documentation change; this record does not select or alter an application architecture.

## Alternatives

- Continue without ADRs: no process overhead, but architectural context and rejected options remain scattered across code, issues, and general documentation.
- Use issue or pull-request discussions as the only decision record: discussions are already part of review, but are less durable and harder to discover as a decision catalog.
- Adopt an external ADR tool or hosted decision service: it could offer workflow features, but would introduce an unnecessary tool or service dependency for a Markdown documentation need.

## Tradeoffs

The Markdown process is portable, reviewable in ordinary pull requests, and adds no runtime or development dependency. The template and validator add a small amount of maintenance and do not provide automated approval, enforce decision quality, or substitute for human review. A status field can become stale unless reviewers update it when decisions are superseded or require re-review. ADRs must remain factual and must not include sensitive prompts, credentials, or personal data.

## Review Status

Not reviewed. AS-005 introduces this process at the user's request; no separate maintainer review of this ADR is claimed. A maintainer may record a later review by updating the front matter and this section together.
