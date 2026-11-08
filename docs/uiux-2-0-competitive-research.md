# Competitive research record — Claude desktop vs. M5 Agent Studio

**Story:** proposed `P7-01` (closes the ledger's `competitive_research.status = "research_pending"`)
**Recorded:** 2026-11-08
**Author of the record:** agent session, from owner-supplied screenshots
**Decision authority:** none. This record informs a proposal; it approves nothing.

---

## 1. Evidence basis, and what it is not

This is a **screenshot-bounded** record. The ledger's instruction is: *"Verify each
contemporary claim against primary docs/screenshots; record date and URL. No
unsupported competitor feature claims."*

| Field | Value |
|---|---|
| Product observed | Claude desktop application |
| Source | 3 screenshots supplied by the repository owner on 2026-11-08 |
| Public URL for the observed build | **none recorded** — the owner's local install; no released build number was visible in the captures |
| Surfaces actually seen | home / composer, a session view under an error state, Settings → Claude Code |
| Surfaces **not** seen | Projects, artifacts, scheduled tasks, connectors, any multi-model or comparison flow |
| Date of capture | 2026-11-08 (screenshots in the owner's possession, not captured for this record) |

**Consequence, stated plainly:** three surfaces cannot characterise a product.
Everything below is limited to what those three screens show. No claim is made
about Claude's permission model, its provider support, its offline behaviour, or
any feature not visible in the captures. This record is therefore **sufficient to
direct typography, hierarchy, composer and empty-state decisions**, and
**insufficient** to support any statement about how a competitor works
internally.

---

## 2. Screenshot inventory

| File (owner workspace) | Bytes | Subject |
|---|---:|---|
| `uploads/image-2.png` | 200,952 | Home: sidebar, single composer, "You're here!" empty state |
| `uploads/image-3.png` | 231,895 | Session view under a gateway error: banner, same composer, session list |
| `uploads/image-4.png` | 402,812 | Settings: grouped destinations, search, live code-appearance previews |

---

## 3. Patterns observed, with the M5 contrast

### 3.1 One composer, identical in every context
The composer is the same component in the home and session captures: attach on the
left, a two-way segmented control, the model chip, submit at the right. Only the
placeholder changes.

**M5 today:** five different composer shapes across six panes — Start (inline input
+ "Open in Harness"), Harness (inline input + "Run", mid-page), Council (message box
+ a separate "Send & run 4-LLM round"), Pipeline (top textarea + "Run pipeline"),
Compare (top textarea + "Start Compare"), Chat (bottom bar + "Send"). Council
carries two submit paths at once.

### 3.2 Sidebar is content-first
The sidebar is dominated by the user's own recent work, labelled by intent. Chrome
occupies one nav row.

**M5 today:** the 200px rail is navigation only; the session list appears inside
Harness alone, and its entries are auto-titles that repeat (`Session greeting` ×3,
`hi` ×3), so the list offers no recall cue.

### 3.3 Empty state is one headline, one sentence, one input
No cards, no status chips, no second paragraph — including in the error state,
where the same restraint is kept.

**M5 today:** Start states the same readiness truth four times (status pill, verdict
line, blocker card, and the `READINESS DETAILS` disclosure plus its button).
Compare states the model count twice.

### 3.4 One problem, one banner, two actions
The error surface names the failure, gives one plain-language sentence on the likely
cause, offers `Details` for depth, and exactly two actions.

**M5 today:** the equivalent facts are frequently spread across a chip, a sentence
and a card.

### 3.5 Long identity lives in a chip, not the layout
The model appears as one truncated footer chip. The full 60-character name is never
repeated across the screen.

**M5 today:** the full local filename is among the most repeated strings in the
product — header, Harness select, Chat select, Council seats, Compare's list — and
becomes the loudest element on several screens purely because it is longest.

### 3.6 Settings is a real surface
Twelve destinations, grouped, with a search field, and live side-by-side previews
for appearance rather than abstract toggles.

**M5 today:** `Telemetry`, `Density`, `Inspector` and `Tools & integrations` sit in
the primary chrome of every screen at equal weight to the product's main action.

### 3.7 Rhythm: whitespace over rules
Grouping is carried by proximity and one surface, not by nested bordered boxes.

**M5 today:** border-on-everything, which contradicts this repository's own design
system rule: *"Use one surface per meaningful group. Prefer whitespace and alignment
over nested borders/cards."*

---

## 4. Patterns that must NOT be adopted

Recorded because a competitor record that only lists borrowings invites the wrong
change. Each is grounded in this repository's own rules.

1. **Do not reduce workflow visibility.** Claude's navigation is deliberately sparse.
   M5's design system requires all five workflows stay *visible and named*, and the
   ledger's `feature_preservation_matrix` gates it. Six tabs are a constraint.
2. **Do not adopt the voice.** "You're here!" is warm; M5 is an evidence-first
   developer tool whose states explicitly must not *"fabricate runtime state"*
   (`tab_analysis.*.states`). Borrow the brevity, keep the honesty: shorten and
   relocate causes behind disclosure; do not delete them.
3. **Do not compress safety into a summary.** Approvals, diff review, secret masking
   and deny-by-default stay explicit and inspectable (non-negotiable principle 3;
   design system: *"Safety is part of design"*). Claude's visible surface does not
   carry a permission model, so there is nothing here to copy.
4. **Do not flatten structural panes.** A single linear composer assumes one job.
   Council's four seats and Pipeline's four stages are the substance of those panes
   and must stay legible before a run.

---

## 5. What this record licenses

- Typography and whitespace decisions, especially one hero line per pane.
- Composer consolidation (five shapes → one), preserving every input id and handler.
- Empty-state consolidation (one headline, one sentence, one primary action).
- Moving developer chrome into Settings.
- Display-level model aliasing, with `option.value` untouched.

## 6. What this record does not license

- Any claim about competitor internals or unreleased capability.
- Hiding, renaming, or removing any workflow, control, or safety surface.
- Any backend or API change.
- Treating the three captured surfaces as a complete competitive picture.

## 7. Known gaps to close in a later record

- No public URL / build number for the observed Claude build.
- Only 3 of the product's surfaces seen; Projects and artifacts unseen.
- No equivalent capture of M5's own panes attached to this record; the pane-level
  audit that pairs with this document lives outside the repository and must be
  added through the normal screenshot-review process before it is used as evidence.
- `docs/uiux-2-0-baseline-evidence.md` still records 0/6 accepted M5 baseline
  screenshots; this record does not change that count.
