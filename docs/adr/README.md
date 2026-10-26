# Architecture Decision Records

This directory records durable architecture decisions that affect implementation, compatibility, security, privacy, operations, or long-term maintenance. ADRs are Markdown files so decisions remain reviewable with the code and require no external service or tool dependency.

## When to write an ADR

Create or update an ADR when a change materially affects one or more of these areas:

- Public API, CLI, configuration, persistence, schema, or migration compatibility.
- Trust boundaries, authorization, secure execution, secret handling, or data retention.
- Outbound network behavior, privacy, model/provider/MCP independence, or user consent.
- Supported platforms, hardware assumptions, runtime/build dependencies, or deployment model.
- A cross-cutting design with meaningful alternatives or long-lived tradeoffs.

Routine implementation details and reversible local refactors do not need an ADR. If a proposal changes an accepted decision, write a new ADR that supersedes the old one rather than rewriting the decision history. Link code, tests, issues, and the source-linked [subsystem inventory](../subsystem-dependency-inventory.md) where useful. Keep evidence factual and avoid publishing credentials, private prompts, personal data, or unredacted security details.

## Format and identifiers

1. Copy [`0000-template.md`](0000-template.md) to the next available `NNNN-short-title.md` filename. Use a four-digit, never-reused identifier; `0000` is reserved for the template.
2. Keep each ADR focused on one decision. Include the required sections: **Context**, **Decision**, **Alternatives**, **Tradeoffs**, and **Review Status**.
3. Keep the YAML-style front matter fields (`id`, `title`, `status`, `date`, `review_status`, `reviewed_by`, and `reviewed_on`) synchronized with the body. Dates use ISO `YYYY-MM-DD`.
4. Decision status is one of `proposed`, `accepted`, `rejected`, or `superseded`. Review status is separately one of `not-reviewed`, `reviewed`, or `re-review-required`. A decision can be accepted while its review status remains `not-reviewed`; do not imply independent review that did not occur.
5. For a reviewed ADR, record the reviewer or review role and review date. If later changes invalidate the review, set `re-review-required` and explain why. Link a superseding ADR when applicable.
6. Update the status and review metadata in a pull request with the decision or review. Do not silently replace a decision's rationale after acceptance; add a superseding record when the decision changes.

## Validation

Run the validator and tests locally:

```bash
python scripts/validate_adrs.py
python -m pytest -q tests/test_adr_process.py
```

CI runs the validator and the full test suite. The validator is a standard-library document check; it does not import the application, contact providers, or execute tools. It checks required fields and sections, allowed status values, date syntax, unique identifiers, and that each recorded section contains substantive text.
