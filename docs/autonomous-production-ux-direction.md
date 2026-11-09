# Autonomous Production — integrated UX direction

Revision 1.2 • 2026-11-08 • Owner agreed to record this direction. Concept references, not implemented-screen acceptance or permission to remove existing features.

## Selected direction

A calm, Claude-like, conversation-first workspace inside M5 Agent Studio. Warm neutral surfaces, restrained accent colours, readable typography and progressive disclosure. Extend the current application, not a second studio or orchestration plane. Mockup navigation labels are illustrative; reconcile the actual current routes and owning UI program before implementation.

## Visual references

- [Project workspace](designs/agent-studio-workspace.png)
- [Focused artifact review](designs/agent-studio-artifact-review.png)

These AI-generated images communicate hierarchy and workflow, not pixel-perfect requirements. Their wording, states and approval badges are not runtime evidence. Use this written specification when a detail in an image conflicts with authority or accessibility requirements.

## Everyday project workspace — AP-008

- Preserve existing navigation and feature/control routes. Show recent projects without forcing existing conversations into a new data model.
- Conversation is the central interaction: brief, clarification, progress summaries, requests and ordinary-language guidance.
- An adjacent artifact panel shows the current version, preview and relevant files/sources. Support websites, documents, designs and media without assuming a video-only product.
- Show concise meaningful milestones, current work, blockers and needs-input states. Expand activity, agents, sources and resource details on demand; do not expose a permanent wall of logs or pretend activity.
- Preserve pause/stop/cancellation and drafts. Display outcomes from durable application state and recover after reconnect.
- Early direction checks are contextual. Direction approval must not imply final acceptance, use-as-reference, publishing or spending authority.

## Focused artifact review — AP-018, AP-019, AP-020

- Expand an artifact within the same project, preserving conversation position, version and draft feedback when returning.
- Allow natural-language feedback associated with a selected element, text range, scene or timestamp where supported. Provide a plain-text fallback; never make precision selection mandatory.
- Record project, exact artifact version, selection/time context and feedback. Flag stale selections after revisions rather than silently applying them to a different version.
- Show relevant open checks and their evidence. Blocking checks must be explicit; do not label agent self-review as owner acceptance.
- Keep direction approval, final artifact acceptance, scoped benchmark/reference adoption and publish/deploy permission distinct. No preselected approval or fabricated acceptance.

## Capabilities and model integration

Model management remains part of Studio, including its existing sequential load/unload intent. Tools/capabilities surfaces expose installed integrations, grants and status, not a competing runtime. A project may surface a contextual request explaining the quality gap, proposed service, data to be shared, estimated cost, limits and alternative. Agents may act without repeated prompts inside an existing valid grant. New providers, data scopes or increased budgets require the applicable decision.

Local inference remains the default, including local reasoning and evaluation. Architecture must support future explicitly authorised cloud/media services, application APIs, CLIs and MCP adapters. Discovery is not activation; MCP is neither a quality guarantee nor a security boundary. No silent fallback. External reasoning-model use remains a separate unresolved policy decision.

## Integration and verification gates

1. AP-001 maps owning UI stories, active changes and all existing routes before implementation.
2. AP-008 implements the integrated project surface; AP-018 handles early samples; AP-019 handles scoped feedback; AP-020 handles versioned references. Extend existing persistence, authority and events rather than duplicating them.
3. Write a blast-radius map covering conversation, preview isolation, models/tools, Settings, streaming/reconnect, cancel and existing tabs. Capture baseline and regression evidence.
4. Validate keyboard access, focus restoration, screen-reader labels, contrast, light/dark themes, 200% zoom, narrow widths and stacked conversation/preview behaviour. No clipping or overlap.
5. Exercise loading, empty, failure, offline/service-unavailable, stale-version, denied-permission and reconnect states. Controls must reflect real state, not mock-only receipts.
6. Untrusted previews must not gain Studio credentials, privileged origin or network access. Permission and approval actions require backend enforcement.
7. Submit actual rendered implementation for owner visual review. Agreement with these concept images is not approval of later screenshots or release acceptance.

## Open design work

Exact existing-navigation placement, project/conversation relationships, responsive breakpoints, selection mechanics per artifact type, component tokens and final copy require source inspection and an interactive prototype. Do not treat these remaining design details as a reason to invent authority or erase existing functionality.
