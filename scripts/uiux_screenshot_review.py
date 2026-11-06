#!/usr/bin/env python3
"""Review every committed capture and check that each one still means what it claims (UIUX-P6-03).

    python3 scripts/uiux_screenshot_review.py            # regenerate JSON + docs
    python3 scripts/uiux_screenshot_review.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_screenshot_review.py --json     # print the review

The program committed **228 PNGs across 27 capture sets** and nothing looked at them together.
Each story photographed its own surface with its own walk, wrote a README beside it, and moved on;
no pass asked whether the set the README shows is still the pane this tree draws, whether the
`dark` in a filename is still dark, or whether a capture belongs to the story it is filed under.

This is that pass, and its first finding was the one that mattered: the six images the README
shows — the published onboarding set — were taken on **2026-11-01**, before twenty-two UI stories.
They were missing the P5-03 criteria table, the P5-04 "Ready: 1 model" strip and the P2 status
components, and the Compare capture carried a horizontal overflow the later stories fixed. The
story re-captured them from this tree with the repository's own `scripts/capture_screenshots.mjs`,
and photographed the same six panes again in the dark theme, which the published set never had.

What the review actually checks, per set:

* **it has a producer.** A README that names a script that exists, or a curated entry that does.
  A set nobody can re-take is a picture, not evidence.
* **the index agrees with the disk.** Where `index.json` exists it names a sha256, a byte count, a
  viewport and a theme; each file is hashed and measured, and a mismatch is a finding.
* **the theme in the name is the theme in the pixels.** Mean luminance is measured with
  ImageMagick's `identify`; a `-dark-` capture that is 96% white is a saving mistake, and the
  check refuses it. When `identify` is absent the review says so per set instead of passing it.
* **the producer still works.** A set whose walk is a known failure in the preservation map is
  evidence from a walk that does not pass today, and the review says which sets those are.
* **every set has an authored review.** `REVIEWS` below is the human reading; `--check` fails when
  a set on disk has no entry, so a capture cannot ship unreviewed — the same rule the refusal map
  and the preservation matrix use.

The rule the artifact states: **a picture is evidence only while someone can say what it shows,
which command made it, and that the pane still looks like that.**
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEDIA = PROJECT_ROOT / "docs" / "media"
PUBLISHED = MEDIA / "screenshots"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Screenshot_Review.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-screenshot-review.md"
PRESERVATION = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Preservation_E2E.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The six panes. A pane with no capture anywhere is a finding: the program cannot claim it
#: reviewed a surface it never photographed.
PANES = ("start", "harness", "hall", "pipeline", "compare", "chat")

#: Panes are named differently in filenames and in the UI (`hall` is the Council pane, `council`
#: is the Pipeline, `debate` is Compare). The mapping is the ledger's `tab_registry`, and the
#: review reads captures by what they photograph, not by what they are called.
PANE_ALIASES = {
    "start": "start",
    "harness": "harness",
    "hall": "hall",
    "council": "hall",
    "pipeline": "pipeline",
    "compare": "compare",
    "debate": "compare",
    "chat": "chat",
}

#: A capture is `narrow` below this CSS width and `desktop` at or above the wide threshold.
NARROW_MAX = 600
DESKTOP_MIN = 800

#: Sets the README does not describe, with the script that produced them. Verified to exist and
#: grepped to write the directory it is filed under; a wrong entry fails the run.
CURATED_PRODUCERS = {
    "screenshots": "scripts/capture_screenshots.mjs",
    "adversarial": "scripts/adversarial_walk.mjs",
    "keyboard-a11y": "scripts/a11y_walk.mjs",
    "responsive": "scripts/responsive_walk.mjs",
    "stream-perf": "scripts/stream_perf_walk.mjs",
    "uiux-p5-05": "scripts/uiux_interaction_polish_walk.mjs",
    "uiux-p6-03": "scripts/capture_screenshots.mjs",
}

#: The author's reading of each set. `note` is what a reviewer concluded, `eye` names the files
#: they actually opened, and `re_taken` says whether this story re-shot the set on this tree.
#: `--check` fails when a set on disk has no entry here.
REVIEWS: dict[str, dict[str, object]] = {
    "screenshots": {
        "story": "UIUX-P0-01 (published set, re-shot by UIUX-P6-03, re-shot again by UIV2-04, re-shot again by UIV2-05)",
        "note": (
            "Re-shot on this tree: the set the README shows was twenty-two stories old and showed a "
            "Compare pane without the P5-03 criteria table or the P5-04 readiness strip, plus a "
            "horizontal overflow the later stories fixed. Re-captured with "
            "`scripts/capture_screenshots.mjs`, `validate_screenshots.py` -> 6 of 6, and all six "
            "opened: every pane draws its own empty state with a cause and a next step, nothing is "
            "clipped at 1440x900, and the inspector fits. Re-shot once more for UIV2-04 (approved V2 "
            "palette in the m5-redesign-system token block): same six panes, same viewport and OS-default "
            "theme, only the colour tokens changed; opened again and the indigo accent, canvas, surface "
            "and border roles read as the approved north-star values in both themes (the light/dark "
            "after-sets live under uiv2-v2/after-uiv2-04). Re-shot once more for UIV2-05 (responsive shared "
            "shell: header pinned to one <=64px row at every width, tablist moved out of the header to a "
            "200px rail at >=1280, a 64px icon rail at 800-1279 and a scrollable row under the header at "
            "<800, inspector a 320px column at >=1440 and a drawer below, skills/MCP strip collapsed to one "
            "row); same six panes, opened again - all six tabs reachable, no horizontal overflow, and the "
            "before/after sets plus measured shell metrics live under uiv2-v2/before-uiv2-05 and "
            "uiv2-v2/after-uiv2-05)."
        ),
        "eye": [
            "start.png",
            "harness.png",
            "hall.png",
            "pipeline.png",
            "compare.png",
            "chat.png",
        ],
        "re_taken": True,
        "theme": "light (the OS default the manifest records; no dark variant exists)",
    },
    "uiux-p6-03": {
        "story": "UIUX-P6-03",
        "note": (
            "This story's own before/after: `before/` is the published set as it stood at cee9159 "
            "(extracted with `git show`, checksums recomputed, capture time taken from the index "
            "that set recorded for itself), `after-dark/` is the same six panes photographed again "
            "on this tree with `--emulate-theme dark`, which the published set never had."
        ),
        "eye": ["after-dark/compare.png", "before/harness.png", "before/compare.png"],
        "re_taken": True,
        "theme": "light (before) and dark (after-dark)",
    },
    "adversarial": {
        "story": "UIUX-P6-02",
        "note": (
            "Refusal states rather than panes: the 403 a read-only session's apply earns, drawn "
            "inside the diff dialog that stays open, the injected 503, the rate-limited Chat send, "
            "and the same refused apply at 390x740 — in both themes, plus the pre-story `Applied!` "
            "toast for contrast. Every frame names the state it is in; none of them can be mistaken "
            "for a success."
        ),
        "eye": [
            "adversarial-dark-apply-refusal.png",
            "adversarial-light-narrow-apply-refusal.png",
            "adversarial-dark-before-apply-applied.png",
        ],
        "re_taken": True,
        "theme": "dark and light",
    },
    "polish": {
        "story": "UIUX-P5-05",
        "note": (
            "Hover, press and focus-ring parity across the 51 control kinds, in both themes and at "
            "360x740. Opened the dark at-rest capture: a loaded runtime in the inspector with its "
            "Unload action ringed, the Chat empty state centred, nothing clipped at 1440x900. The "
            "hover and press counts are the walk's, not this frame's."
        ),
        "eye": ["polish-dark-hover-primary.png"],
        "re_taken": False,
        "theme": "dark and light",
    },
    "harness-recovery": {
        "story": "UIUX-P3-05",
        "note": (
            "The seven recovery states of a Harness run: each carries the server's reason, what was "
            "kept and the action that moves it, and none of them reuses the ok tone."
        ),
        "eye": ["paused-panel.png"],
        "re_taken": False,
        "theme": "dark",
    },
    "responsive": {
        "story": "UIUX-P1-05",
        "note": (
            "The three breakpoints with the rail collapsed and the inspector stacked; no horizontal "
            "scrollbar in any of them."
        ),
        "eye": ["responsive-360-start.png"],
        "re_taken": False,
        "theme": "dark",
    },
    "chat": {
        "story": "UIUX-P2-05 / P2-06",
        "note": (
            "The composer's stop, failure and handoff states, including the restored draft after a "
            "refused send; the transcript stays readable while the refusal is drawn above it."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "compare-continuity": {
        "story": "UIUX-P4-06",
        "note": "The record bar keeps a finished comparison readable while the reader starts another.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "compare-criteria": {
        "story": "UIUX-P4-04",
        "note": (
            "The criteria table names what settles each criterion and says `you` where the pane "
            "cannot decide — the same table the refreshed published Compare capture now shows."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "compare-provenance": {
        "story": "UIUX-P4-05",
        "note": "Each reply carries the model, locality and cost it was produced with.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "council-deliberation": {
        "story": "UIUX-P4-02",
        "note": "The deliberation stages are shown in order, with the seat that is speaking marked.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "council-participants": {
        "story": "UIUX-P4-01",
        "note": (
            "Four pre-round seats with exact model ids, locality and readiness, and the shared-memory "
            "disclosure; a seat that is not ready says why instead of looking idle."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "council-verdict": {
        "story": "UIUX-P4-03",
        "note": "The verdict card states the winner, the tally and the seats that dissented.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "harness": {
        "story": "UIUX-P3-01",
        "note": "Task creation, workspace and run settings with the diff, approval and abort controls still reachable.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "harness-evidence": {
        "story": "UIUX-P3-04",
        "note": (
            "The verification-and-artifacts surface, re-taken from the fixed build: `rejected-card` "
            "and `rejected-record` show the rejected completion (#169, closed) with the code that "
            "decided it, the exit code and the failing test classified as introduced."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "harness-permissions": {
        "story": "UIUX-P3-03",
        "note": "The four approval states, including the session-scoped refusal the server enforces.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "harness-timeline": {
        "story": "UIUX-P3-02",
        "note": (
            "The durable log and the cursor discipline, re-taken from the fixed build: `pruned` shows "
            "the strip saying 9 events held over the retained floor seq 5-13 (#170, closed), and "
            "`replayed` the window a resync draws after the whole log is re-read."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "keyboard-a11y": {
        "story": "UIUX-P1-06",
        "note": "Tab order and focus rings on the shell's own controls; the ring is the same 2px in both themes.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "pipeline-artifacts": {
        "story": "UIUX-P3-06",
        "note": "The artifact list distinguishes what the run produced from what it only named.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "pipeline-recovery": {
        "story": "UIUX-P3-07",
        "note": "The pipeline's recovery states, each with a way forward rather than a retry that would fail the same way.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "presentations": {
        "story": "UIUX-P1-03",
        "note": (
            "The one status component in its four kinds, so a recovery is never drawn as a success. "
            "The error capture says `Could not read the preflight`, gives `Failed to fetch` as the cause, "
            "says what was kept, and offers Re-check and Providers — none of which reads as a success."
        ),
        "eye": ["start-error.png"],
        "re_taken": False,
        "theme": "dark",
    },
    "start": {
        "story": "UIUX-P2-01 / P2-02 / P2-03",
        "note": "The preflight checklist, the one marked next step, and the recent-work list with its honest `not run` rows.",
        "eye": [],
        "re_taken": False,
        "theme": "dark and light",
    },
    "status": {
        "story": "UIUX-P1-03",
        "note": "Before/after of the status component replacing the per-pane banners.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "stream-perf": {
        "story": "UIUX-P5-04",
        "note": "The streaming states with the throughput and readiness strip the Compare pane now carries.",
        "eye": [],
        "re_taken": False,
        "theme": "dark",
    },
    "uiux-p5-05": {
        "story": "UIUX-P5-05",
        "note": (
            "Twelve 6-pane sets (dark, light, narrow, and the focused variants, before and after) with "
            "an index.json each. They are checked by their own validator, "
            "`scripts/validate_uiux_interaction_polish_screenshots.py`, which knows the 390x844 variants "
            "the published validator does not; this review checks the same files' checksums, sizes, "
            "viewports and themes from the indexes rather than duplicating that script."
        ),
        "eye": [],
        "re_taken": False,
        "theme": "dark, light, narrow, focused",
    },
    "terminology": {
        "story": "UIUX-P5-01",
        "note": "The same noun for the same thing across the six panes; the captures are the evidence for the copy change.",
        "eye": [],
        "re_taken": False,
        "theme": "dark and light",
    },
    "tokens": {
        "story": "UIUX-P1-01",
        "note": "The token set photographed at compact density, light at 900px, and the narrow rail.",
        "eye": [],
        "re_taken": False,
        "theme": "dark and light",
    },
    "uiv2-v2": {
        "story": "UIV2-01 (baseline), UIV2-03 (reference fixtures), UIV2-04/UIV2-05/UIV2-06/UIV2-11/UIV2-12/UIV2-13/UIV2-14 (after-sets)",
        "note": (
            "Two V2-program sets. `baseline/` is the running app at pinned HEAD 9a584a3, captured "
            "from a disposable git-archive copy via demo_tour.py --serve with scripts/capture_screenshots.mjs "
            "(six tabs x 1640x900/1440x900/1280x800/390x844 in light and dark) plus the MCP hub overlay "
            "opened through the real header select. Opened and read: the 1440x900-light start, harness, "
            "hall and compare panes (checklist-heavy Start, dense Harness composer, four council role "
            "cards, criteria-table Compare, persistent right telemetry rail in all of them), the "
            "390x844-light chat pane (mobile single column, three-option persona picker), and the "
            "1440x900-light MCP overlay (one 860px dialog holding presets, form, servers and test call). "
            "`reference/` is the encoded approved three-region reference rendered by "
            "scripts/uiux_v2_reference_capture.mjs from wireframe.html; opened: harness at 1440x900-light "
            "(four regions with annotated widths, telemetry as header popover) and harness at 390x844-light "
            "(single column, annotations hidden). The baseline set is current-state evidence; the "
            "reference set is the target approved by the user at the UIV2-03 gate. `after-uiv2-04/` is "
            "the running app re-shot at the UIV2-04 commit after the approved palette landed: six panes "
            "at 1440x900 light, 1440x900 dark and 390x844 light plus the MCP overlay at 1440x900 light, "
            "opened and read against `baseline/` — the indigo accent (#4F5FE8 light / #93A0FF dark), "
            "canvas, surface and border roles now match the approved tokens while layout is unchanged. "
            "`before-uiv2-05/` and `after-uiv2-05/` bracket the UIV2-05 shared-shell change: opened the "
            "390x844-dark pair (header was 108px with the tabs wrapped inside it, now one 56px row with a "
            "tab row under it) and the 1280x800-dark pair plus `after-uiv2-05/1280x800-dark/harness-drawer-open.png` "
            "(inspector was a permanent 320px column, now a 320px drawer over a 1080px central pane); the "
            "measured numbers are in `uiv2-05-shell-metrics-before.json` / `uiv2-05-shell-metrics-after.json`. "
            "`before-uiv2-06/` and `after-uiv2-06/` bracket the UIV2-06 inspector change: opened the "
            "1440x900-dark harness pair (permanent telemetry rail -> contextual inspector with "
            "Approvals/Changes/Verification/Resources tabs) and `after-uiv2-06/1440x900-light/harness-telemetry-popover.png` "
            "(header Telemetry popover holding all four telemetry cards incl. the model load/unload "
            "actions), plus the per-pane inspector tabs in the 1440x900-dark set (Council "
            "People/Evidence, Pipeline Stage/Artifacts, Compare Evaluation, Chat hidden by default). "
            "`before-uiv2-11/` and `after-uiv2-11/` bracket the UIV2-11 session-navigation change "
            "(served from disposable git-archive copies at a723078 and a6b578a, four seeded sessions, one "
            "flipped to running): opened the 1440x900-dark pair - before has no filter box and the running "
            "row's delete is an enabled one-click button, after has the named Filter sessions box, "
            "shown-of-total counts, the disabled (aria-disabled) delete on the running row, and the armed "
            "Confirm delete state with its irreversible-scope note - plus the walk state shots "
            "(filter-active, running-protected, armed-delete-scope, sessions-mobile at 390x844). "
            "`before-uiv2-12/` and `after-uiv2-12/` bracket the UIV2-12 pinned-conclusion scroll-lock fix "
            "(served from a disposable git-archive copy at b01704f vs the branch, one seeded closed Council "
            "session carrying a 17,430-char conclusion): opened the 1440x900-dark pair - before shows the "
            "conclusion flooding the whole pane as one unscrollable block with the transcript and composer "
            "pushed out of view, after shows it as a capped sticky card at the top of the transcript with "
            "Expand/Unpin/close controls while the transcript and composer stay visible - plus the walk "
            "state shots (pinned-long-conclusion, unpinned, resized, expanded-modal at 1440x900-dark; "
            "mobile-long-conclusion at 390x844-dark). "
            "`before-uiv2-13/` and `after-uiv2-13/` bracket the UIV2-13 clear-session change "
            "(served from a disposable git-archive copy at 65f6e94 vs the branch, seeded Council "
            "sessions): opened the 1440x900-dark pair - before has no clear control anywhere (sidebar "
            "rows carry no delete button, the composer row has only Run full round / Finish and pin "
            "summary, and DELETE /api/hall/sessions answers 405), after adds the armed per-row delete, "
            "the sidebar Clear all, and the composer Clear session button matching the wireframe's "
            "hall composer row - plus the walk state shots (armed-delete with its role=alert scope "
            "note, armed-clear-all, cleared-empty, populated-with-clear-controls at 1440x900-dark). "
            "`before-uiv2-14/` and `after-uiv2-14/` bracket the UIV2-14 session-list/roles/compact-conclusion "
            "change (served from a disposable git-archive copy of origin/main vs the branch, one seeded closed "
            "Council session with architect + critic replies and a 9k-char conclusion): opened the "
            "1440x900-dark pair - before shows rows with only '3 voices • 3 msgs' (no participant roles) and a "
            "24vh conclusion body flooding the pane, after shows wrapped role chips on each row "
            "(Human / Staff Architect / Adversarial Critic), a compact 96px pinned summary, the height-capped "
            "verdict block, and the composer visible again - plus the walk state shots "
            "(compact-conclusion-with-roles and full-conclusion-modal at 1440x900-dark; mobile-roles-compact "
            "at 390x844-dark)."
        ),
        "eye": [
            "baseline/1440x900-light/start.png",
            "baseline/1440x900-light/harness.png",
            "baseline/1440x900-light/hall.png",
            "baseline/1440x900-light/compare.png",
            "baseline/390x844-light/chat.png",
            "baseline/overlay/1440x900-light/mcp-hub.png",
            "reference/1440x900-light/harness.png",
            "reference/390x844-light/harness.png",
            "after-uiv2-05/390x844-dark/harness.png",
            "after-uiv2-05/1280x800-dark/harness-drawer-open.png",
            "after-uiv2-06/1440x900-dark/harness.png",
            "after-uiv2-06/1440x900-light/harness-telemetry-popover.png",
            "before-uiv2-11/1440x900-dark/harness.png",
            "after-uiv2-11/1440x900-dark/harness.png",
            "after-uiv2-11/1440x900-dark-states/filter-active.png",
            "after-uiv2-11/1440x900-dark-states/running-protected.png",
            "after-uiv2-11/1440x900-dark-states/armed-delete-scope.png",
            "after-uiv2-11/390x844-dark-states/sessions-mobile.png",
            "before-uiv2-12/1440x900-dark/hall.png",
            "after-uiv2-12/1440x900-dark/hall.png",
            "after-uiv2-12/1440x900-dark-states/pinned-long-conclusion.png",
            "after-uiv2-12/1440x900-dark-states/unpinned.png",
            "after-uiv2-12/1440x900-dark-states/resized.png",
            "after-uiv2-12/1440x900-dark-states/expanded-modal.png",
            "after-uiv2-12/390x844-dark-states/mobile-long-conclusion.png",
            "before-uiv2-13/1440x900-dark/hall.png",
            "after-uiv2-13/1440x900-dark/hall.png",
            "after-uiv2-13/1440x900-dark-states/armed-delete.png",
            "after-uiv2-13/1440x900-dark-states/armed-clear-all.png",
            "after-uiv2-13/1440x900-dark-states/cleared-empty.png",
            "after-uiv2-13/1440x900-dark-states/populated-with-clear-controls.png",
            "before-uiv2-14/1440x900-dark/hall.png",
            "after-uiv2-14/1440x900-dark/hall.png",
            "after-uiv2-14/1440x900-dark-states/compact-conclusion-with-roles.png",
            "after-uiv2-14/1440x900-dark-states/full-conclusion-modal.png",
            "after-uiv2-14/390x844-dark-states/mobile-roles-compact.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "uiv2-v15-states": {
        "story": "UIV2-15",
        "note": (
            "The five Council states this story makes say something, 5 states x 4 viewports x "
            "light/dark, shot by `scripts/council_state_walk.mjs` against an authenticated demo app "
            "after it passed 19/19 on each of the eight runs; each shot scrolls to the element that "
            "reports the state rather than to whatever the pane happened to show. Opened and read: "
            "the 1440x900-dark lost-turn capture (notice naming Seat 1 · Staff Architect, the model "
            "and '4 characters ... none of them reached the transcript', with Ask this seat again / "
            "Read the session / Check readiness, and the partial voice card under a 'Stream ended · "
            "not saved' badge rather than a Streaming badge), the 1440x900-dark still-answering "
            "capture ('still answering', not a failure, with Read it now / Stop watching, on a live "
            "session row), the 1440x900-light reopened-session capture ('This session is open "
            "again', the pinned conclusion kept, in the light palette), and the 390x844-light "
            "closed-session capture (the 409 notice stacked in the single mobile column with Write "
            "a note to reopen / Read the conclusion / Start a new session). Every one names a state "
            "and a next action; none leaves a permanent 'Streaming...' card."
        ),
        "eye": [
            "lost-turn-1440x900-dark.png",
            "still-answering-1440x900-dark.png",
            "reopened-session-1440x900-light.png",
            "closed-session-390x844-light.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "compare-balanced": {
        "story": "UIV2-16",
        "note": (
            "The Compare pane's question-first composition: 4 states x 4 viewports x light/dark "
            "plus the 390x844 stack, written by `scripts/compare_balanced_walk.mjs` against "
            "`council_readiness_seed.py` with two local models that answer, after 17/17 checks on "
            "each of the eight runs, and beside it `before-uiv2-16/`, the same pane from "
            "origin/main at 5c9a6e3 captured with `capture_screenshots.mjs --panes debate`. Opened "
            "and read: the 1440x900-light pair (before: the fold holds the four-row criteria table "
            "and the question is under it; after: the question and the two picks fill the fold and "
            "the criteria are one folded line reading '3 settled by the run · 1 not settled here · "
            "2 yours to judge'), the 1440x900-dark two-model capture (two 434x229 answers at the "
            "same width and height, the results table under them, Result columns 2 in the "
            "inspector), the 1440x900-dark criteria-open capture (three groups on their own "
            "emerald/amber/cyan rails), and the 390x844-light one-model capture (the single-answer "
            "note above one full-width card, nothing overflowing)."
        ),
        "eye": [
            "before-uiv2-16/compare-1440x900-light.png",
            "compare-collapsed-1440x900-light.png",
            "compare-two-models-1440x900-dark.png",
            "compare-criteria-open-1440x900-dark.png",
            "compare-one-model-390x844-light.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "compare-evidence": {
        "story": "UIV2-17",
        "note": (
            "The Compare pane's results, verdict and evidence rail: nine shots on each of four "
            "viewports x light/dark - seven states, the 390x844 narrow shot and the rail on its "
            "own - written by `scripts/compare_evidence_walk.mjs` against fixtures it starts "
            "itself (default, `--slow-model`, `--long-model`, and the catalogue's keyless provider "
            "row for the failure), after 25/25 checks on each of the eight runs, beside "
            "`before-uiv2-17/`, the same two states served from origin/main at faff1e9 captured by "
            "an earlier copy of the walk in `--capture-only`. Opened and read: the 1440x900-dark "
            "populated capture (two 434px answers, one row each in both tables, the rail reading "
            "`Panels 2 of 2 answered` and one evidence line per reply), the 1440x900-dark verdict "
            "capture (arbiter named with its catalogue row, `The replies above are its evidence`, "
            "and its own 41.2 tok/s badge), the 1440x900-dark error capture (a dash where the size "
            "would be and `the provider failed before it reported any figure` where a figure would "
            "be), the 390x844-light narrow capture (both tables stacked with their labels and no "
            "sideways scroll), and the before-uiv2-17 1440x900-light populated capture (`0 ch` for "
            "the failed row, a verdict with no provenance, and a rail that points at the pane)."
        ),
        "eye": [
            "evidence-populated-1440x900-dark.png",
            "evidence-verdict-1440x900-dark.png",
            "evidence-error-1440x900-dark.png",
            "evidence-narrow-390x844-light.png",
            "before-uiv2-17/1440x900-light/evidence-populated-1440x900-light.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "mcp-hub": {
        "story": "UIV2-21",
        "note": (
            "The MCP hub: its catalog, its connected-server dashboard and its activity log - "
            "eleven shots on each of four viewports x light/dark (catalog, search, connected, a "
            "server that could not be reached, a stored token, activity, a slow call's loading "
            "state, a long output, a failed call, the health list and the keyboard state), written "
            "by `scripts/mcp_hub_walk.mjs` against a fixture it starts itself and a real stdio MCP "
            "server it connects through `scripts/mcp_hub_fixture_server.py`, after 40/40 checks on "
            "each desktop run and 39/39 at 390x844, beside `before-uiv2-21/`, the same eleven "
            "states served from origin/main at 16320ab and captured by this same walk in "
            "`--capture-only` (8/39 there: the tabs, the cards, the search and the counts do not "
            "exist yet, which is what the before set is). Opened and read: the 1440x900-dark "
            "catalog capture (three columns of named cards, each with Remote-HTTP, its trust word, "
            "its key requirement and its policy, and the tab strip above them), the 1440x900-dark "
            "activity capture (two servers carrying their stored sync note in rose, one local "
            "server reading `4 of 4 tools on`, and four test calls with their latency and outcome), "
            "the 390x844-light connected capture (full-bleed, one field per row, the tab strip still "
            "one line), and the before-uiv2-21 1440x900-dark catalog capture (chips that name no "
            "transport, the whole form open, and the playground clipped below the fold)."
        ),
        "eye": [
            "mcp-hub-catalog-1440x900-dark.png",
            "mcp-hub-activity-health-1440x900-dark.png",
            "mcp-hub-connected-error-1440x900-light.png",
            "mcp-hub-catalog-390x844-light.png",
            "before-uiv2-21/mcp-hub-catalog-1440x900-dark.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "mcp-wizard-uiv2-22": {
        "story": "UIV2-22",
        "note": (
            "The MCP guided connect wizard: thirteen shots on each of four viewports x light/dark "
            "(an empty step 1, the credentials step with a password-field token, the policy step "
            "with its consequence in words, the review of the seven things that will be saved, the "
            "consent row naming a local command, a save held in flight reading 'Saving and "
            "testing...', the green 'Connected - 4 tools discovered.' confirmation, the server list "
            "after it, a failed connection test that keeps the server's own note, the same server "
            "recovered in place, a stored token kept through a blank field, the keyboard path and a "
            "315-character target at phone width), written by `scripts/mcp_wizard_walk.mjs` against "
            "a fixture it starts itself and the real stdio MCP server it connects to through step 4, "
            "after 38/38 checks on all eight runs, beside `before-uiv2-22/`, the same thirteen states "
            "served from b06ab96 and captured by this same walk in `--capture-only` (12/38 there: "
            "there are no steps, no consent and no result where the button is, which is what the "
            "before set is). Opened and read: the 1440x900-dark step-1 capture (four rail items with "
            "the first marked, 'Step 1 of 4 - Server' announced, Back disabled, Continue the only way "
            "on), the 1440x900-dark review capture (seven labelled rows including 'Credential: A new "
            "token will be stored, and only its mask shown', the advanced disclosure shut and out of "
            "the way), the 1440x900-dark consent capture (the exact command inside the consent "
            "sentence, the note that the screen cannot grant STUDIO_ALLOW_MCP_STDIO, and Continue "
            "refusing in amber), the 1440x900-dark success capture (the green result line, the "
            "namespace the tools arrive under, and the server row masked as Token sk-u...4b71), the "
            "390x844-dark review capture (values wrapping in place, no sideways scroll), the "
            "1440x900-light review capture (the same rows in the light theme, the focus ring on the "
            "step heading reading as a ring and not as a field), and the before-uiv2-22 1440x900-dark "
            "credentials capture (one form with alias, name, transport, policy, URL, auth header, "
            "token and raw headers JSON on screen at once above a 'Save & Sync Tools' button)."
        ),
        "eye": [
            "mcp-wizard-empty-1440x900-dark.png",
            "mcp-wizard-review-1440x900-dark.png",
            "mcp-wizard-consent-1440x900-dark.png",
            "mcp-wizard-success-1440x900-dark.png",
            "mcp-wizard-review-1440x900-light.png",
            "mcp-wizard-review-390x844-dark.png",
            "before-uiv2-22/mcp-wizard-credentials-1440x900-dark.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "start-command-center-uiv2-24": {
        "story": "UIV2-24",
        "note": (
            "The Start command center: seven states - empty, populated, long content, loading, error, "
            "recovery and a runtime port that answers with no models - on each of four viewports "
            "(1640x900, 1440x900, 1280x800, 390x844) x light/dark, written by "
            "`scripts/start_command_center_walk.mjs` against a fixture it starts itself (a stand-in "
            "runtime it can empty, plus `scripts/council_readiness_seed.py --runtime-url`), after 31/31 "
            "checks on each of the eight runs; beside `before-uiv2-24/`, the same pane served from "
            "ff14655 and captured with `scripts/capture_screenshots.mjs --panes start` (light and dark "
            "at 1440x900, light at 390x844). Opened and read: the 390x844-light populated capture (the "
            "composer card fills the first screen: verdict pill, 'Ready to run - 6 of 6 checks ready - "
            "runtime answered, 2 models', the input, the primary button, and 'Nothing is in the way' "
            "with its way to the evidence - and no checklist above it), the 1280x800-dark long-content "
            "capture (a 149-character draft held in one line above the fold, long session titles "
            "clipped at one line with an ellipsis, no sideways scroll), the 1440x900-light error "
            "capture (pill `failed`, a sentence that keeps no stale verdict, and the retry sitting in "
            "the blocker slot above the fold rather than 800px down a closed disclosure), the "
            "1440x900-light contradiction capture (`Checks disagree`, 'runtime answered, 0 models', "
            "and the port-without-models row printing `port_answered=True - model_count=0` with the "
            "action that settles it), and the before-uiv2-24 1440x900 capture (GUIDED FIRST RUN first, "
            "six checklist rows under it, and the task composer below the fold - the pane this story "
            "replaces)."
        ),
        "eye": [
            "after/light/populated-390x844.png",
            "after/dark/long-content-1280x800.png",
            "after/light/error-1440x900.png",
            "after/light/contradiction-1440x900.png",
            "before-uiv2-24/start.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "mcp-tools-uiv2-23": {
        "story": "UIV2-23",
        "note": (
            "The MCP tool browser and its test call: fifteen shots on each of four viewports x "
            "light/dark (an empty Tools tab that names the next action, four rows each carrying a "
            "risk pill, its allowlist state, what a call does, what happens when a model asks and "
            "the arguments it takes, the filter chips with their counts, search narrowed to one "
            "tool, a search with no hit offering to clear itself, a write selected with its consent "
            "row and a disabled Test Call, arguments that are not JSON with the alert naming them, a "
            "call held in flight reading 'Calling...', the result inspector with its authorization "
            "line and a shut Raw response, a 3625-character output truncated with 'Show all', a "
            "Denied row with no test call, a switched-off server refused with the switch to move, "
            "the same call recovered, a tool that reports failure, and the keyboard path), written "
            "by `scripts/mcp_tools_walk.mjs` against a fixture it starts itself and the real stdio "
            "MCP server it syncs through the guide, after 56/56 checks on the six desktop runs and "
            "55/55 on the two phone runs, beside `before-uiv2-23/`, the same states served from "
            "57ef410 and captured by this same walk in `--capture-only`. Opened and read: the "
            "1640x900-dark list capture (four rows, the risk words Read-only / Destructive / Write, "
            "the three labelled statements each, and the counters reading 1 configured - 4 "
            "discovered - 4 available - 0 denied), the 390x844-light consent capture (one column, "
            "the consent naming slow_echo and the write it is, Test Call greyed until it is ticked), "
            "the 1440x900-dark result capture (the Write pill, 1660 ms, 59 characters, 'Authorized as "
            "operator_one_call under standard - you confirmed this write', and the Raw response "
            "disclosure shut), the 1440x900-dark long capture (the truncation line with its hidden "
            "count and 'Show all 3631 characters'), and the before-uiv2-23 1640x900-dark list "
            "capture (three tabs, the tool list still a collapsed drawer on the Connected row, and "
            "a one-line output box with no risk and no consent)."
        ),
        "eye": [
            "mcp-tools-list-1640x900-dark.png",
            "mcp-tools-consent-390x844-light.png",
            "mcp-tools-result-1440x900-dark.png",
            "mcp-tools-long-1440x900-dark.png",
            "mcp-tools-denied-1440x900-light.png",
            "before-uiv2-23/mcp-tools-list-1640x900-dark.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
    "chat-recovery": {
        "story": "UIV2-20",
        "note": (
            "Chat's streaming, tool disclosure, copy and recovery: six shots on each of four "
            "viewports x light/dark - empty, populated, long-content, loading, error and recovery - "
            "written by `scripts/chat_recovery_walk.mjs` against fixtures it starts itself, after "
            "25/25 checks on each of the eight runs, beside `before-uiv2-20/`, the empty and "
            "populated states served from origin/main at 2b0c35f and captured by this same walk in "
            "`--capture-only` (7/14 there). Opened and read: the 1440x900-dark populated capture (a "
            "fenced code block with its labelled Copy control, one tool disclosure open under its "
            "Failed status word with the arguments and the 500-character cap, and the turn's "
            "'1 tool call - 1 failed - 1 ms' line), the 1440x900-dark loading capture (a stopped "
            "turn reading 'Stopped by you - N characters kept in this pane'), and the "
            "before-uiv2-20 1440x900-dark populated capture (the same reply with a bare Copy button "
            "and no tool call, because the old fixture could not make one)."
        ),
        "eye": [
            "chat-recovery-populated-1440x900-dark.png",
            "chat-recovery-loading-1440x900-dark.png",
            "chat-recovery-error-1440x900-light.png",
            "chat-recovery-empty-390x844-light.png",
            "before-uiv2-20/1440x900-dark/chat-recovery-populated-1440x900-dark.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1024-1440 and narrow 390",
    },
    "chat-persona": {
        "story": "UIV2-19",
        "note": (
            "Chat's persona picker: six shots on each of four viewports x light/dark - empty, "
            "populated, long-content, loading, error and recovery - written by "
            "`scripts/chat_persona_walk.mjs` against fixtures it starts itself, after 21/21 checks "
            "on each of the eight runs, beside `before-uiv2-19/`, the empty and populated states "
            "served from origin/main at 3bd4890 and captured by this same walk in `--capture-only` "
            "(3/17 checks there: the registry, the filter and the description line are the things "
            "the story adds, so their absence is what the before set is). Opened and read: the "
            "1440x900-dark empty capture (the picker with its filter beside it and one clamped "
            "description line under both, inside the same one-row control bar UIV2-18 measured), "
            "the 390x844-light empty capture (the same three parts at phone width, the bar still "
            "three rows), and the before-uiv2-19 1440x900-dark empty capture (the select alone, "
            "its value a paragraph of prose, nothing describing it)."
        ),
        "eye": [
            "chat-persona-empty-1440x900-dark.png",
            "chat-persona-populated-1440x900-dark.png",
            "chat-persona-loading-390x844-light.png",
            "chat-persona-error-1440x900-light.png",
            "before-uiv2-19/1440x900-dark/chat-persona-empty-1440x900-dark.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1024-1440 and narrow 390",
    },
    "chat-layout": {
        "story": "UIV2-18",
        "note": (
            "Chat's conversation column: six shots on each of four viewports x light/dark - empty, "
            "populated, long-content, loading, error and recovery - written by "
            "`scripts/chat_layout_walk.mjs` against fixtures it starts itself (default, "
            "`--slow-model` for the reply in progress, `--long-model` for the reply long enough to "
            "scroll, and the catalogue's keyless provider row for the failure), after 26/26 checks "
            "on each of the eight runs, beside `before-uiv2-18/`, the empty and populated states "
            "served from origin/main at 21c1eef captured by this walk in `--capture-only`. Opened "
            "and read: the 1440x900-dark populated capture (an 820px column centred in the 1,240px "
            "pane, the answer at the column's width, the question aligned to its right edge, the "
            "composer under both), the 1440x900-dark long-content capture (a code block with its own "
            "scrollbar inside its bubble and an unbroken token that wraps instead of widening the "
            "pane), the 390x844-light populated capture (a three-row control bar and the same column "
            "at phone width), and the before-uiv2-18 1640x900-light populated capture (an 870px "
            "answer in a 1,300px row, a full-width composer, and the second control row)."
        ),
        "eye": [
            "chat-populated-1440x900-dark.png",
            "chat-long-content-1440x900-dark.png",
            "chat-empty-390x844-light.png",
            "chat-loading-1440x900-light.png",
            "before-uiv2-18/1640x900-light/chat-populated-1640x900-light.png",
        ],
        "re_taken": True,
        "theme": "light and dark, desktop 1280-1640 and narrow 390",
    },
}


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the IHDR chunk, without decoding the image."""
    header = path.read_bytes()[:24]
    if not header.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mean_luminance(paths: list[Path]) -> dict[str, float] | None:
    """Mean pixel value per image, one `identify` call, or None when ImageMagick is absent."""
    if not paths:
        return {}
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, repository files only
            ["identify", "-format", "%i %[fx:mean]\n", *[str(p) for p in paths]],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values: dict[str, float] = {}
    for line in result.stdout.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return values or None


def device_scale_for(png: Path, width: int) -> int:
    """The device pixel ratio a capture was taken at.

    An `index.json` beside the file records the CSS viewport it was captured with, so the ratio is
    that viewport divided into the pixels. Without an index the ratio is inferred from the width:
    every walk in this repository captures at either 1x (1440px, 880px element crops) or 2x
    (2880px), and the gap between them is not close.
    """
    for folder in (png.parent, png.parent.parent):
        index_path = folder / "index.json"
        if not index_path.exists():
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        viewport = index.get("viewport") or {}
        recorded = viewport.get("device_scale_factor")
        if recorded and int(viewport.get("width") or 0) * int(recorded) == width:
            return int(recorded)
    return 2 if width >= 2000 else 1


def css_width_for(png: Path, width: int) -> int:
    return width // device_scale_for(png, width)


def theme_of(name: str) -> str | None:
    lowered = name.lower()
    if "light" in lowered:
        return "light"
    if "dark" in lowered:
        return "dark"
    return None


def theme_is_consistent(theme: str, luminance: float) -> bool:
    """A dark capture is mostly dark; a light one is mostly light. The bands are wide on purpose.

    Measured across the program: dark captures sit between 0.09 and 0.17, light ones between 0.52
    and 0.97. The thresholds split that gap rather than fitting any one set.
    """
    return luminance < 0.35 if theme == "dark" else luminance > 0.45


def readme_for(media_root: Path, name: str) -> Path:
    return media_root / name / "README.md"


def producer_for(media_root: Path, name: str) -> tuple[str | None, str]:
    """The script that produced a set, and where that answer came from."""
    readme = readme_for(media_root, name)
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        found = re.findall(r"(scripts/[\w./-]+\.(?:mjs|py))", text)
        for candidate in found:
            if (PROJECT_ROOT / candidate).exists() and "seed" not in candidate:
                return candidate, "README"
        for candidate in found:
            if (PROJECT_ROOT / candidate).exists():
                return candidate, "README (seed script)"
    if name in CURATED_PRODUCERS:
        return CURATED_PRODUCERS[name], "curated"
    return None, "unknown"


def stories_in(media_root: Path, name: str) -> list[str]:
    readme = readme_for(media_root, name)
    if not readme.exists():
        return []
    return sorted(set(re.findall(r"UIUX-P\d-\d\d", readme.read_text(encoding="utf-8"))))


def known_failing_walks() -> set[str]:
    if not PRESERVATION.exists():
        return set()
    payload = json.loads(PRESERVATION.read_text(encoding="utf-8"))
    failing: set[str] = set()
    for row in payload.get("rows", []):
        if row.get("status") != "known":
            continue
        for prover in row.get("provers", []):
            if prover.get("kind") == "walk":
                failing.add(str(prover.get("script", "")).split("/")[-1])
    return failing


def capture_sets(media_root: Path) -> dict[str, dict[str, object]]:
    """Each `docs/media/<name>` directory that holds at least one PNG."""
    sets: dict[str, dict[str, object]] = {}
    for png in sorted(media_root.rglob("*.png")):
        relative = png.relative_to(media_root)
        name = relative.parts[0]
        bucket = sets.setdefault(name, {"files": []})
        bucket["files"].append(png)
    return sets


def review_set(name: str, files: list[Path], media_root: Path, luminances: dict[str, float] | None, failing: set[str]) -> dict[str, object]:
    producer, provenance = producer_for(media_root, name)
    rows = []
    for png in files:
        width, height = png_size(png)
        rows.append(
            {
                "file": png.relative_to(media_root).as_posix(),
                "name": png.name,
                "bytes": png.stat().st_size,
                "sha256": sha256(png),
                "width": width,
                "height": height,
                "css_width": css_width_for(png, width),
                "theme": theme_of(png.relative_to(media_root).as_posix()),
                "theme_measured": (
                    None
                    if luminances is None
                    else ("dark" if luminances.get(str(png), 1.0) < 0.35 else "light" if luminances.get(str(png), 0.0) > 0.45 else None)
                ),
                "luminance": None if luminances is None else round(luminances.get(str(png), 0.0), 4),
            }
        )
    indexes = []
    for folder in sorted({(media_root / str(entry["file"])).parent for entry in rows} | {media_root / name}):
        index_path = folder / "index.json"
        if index_path.exists():
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            loaded["_folder"] = folder.relative_to(media_root).as_posix()
            indexes.append(loaded)
    index = indexes[0] if indexes else None
    return {
        "set": name,
        "directory": f"docs/media/{name}",
        "story": (REVIEWS.get(name, {}).get("story") or " / ".join(stories_in(media_root, name)) or None),
        "producer": producer,
        "producer_from": provenance,
        "index": None if index is None else {
            "captured_at": index.get("captured_at"),
            "viewport": index.get("viewport"),
            "theme": (index.get("variant") or {}).get("theme"),
            "folders": [entry["_folder"] for entry in indexes],
        },
        "reviewed": name in REVIEWS,
        "note": REVIEWS.get(name, {}).get("note"),
        "eye": list(REVIEWS.get(name, {}).get("eye") or []),
        "re_taken": bool(REVIEWS.get(name, {}).get("re_taken")),
        "theme_claim": REVIEWS.get(name, {}).get("theme"),
        "known_failing_producer": bool(producer and producer.split("/")[-1] in failing),
        "files": rows,
    }


def check_set(row: dict[str, object], media_root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    name = str(row["set"])
    notes = REVIEWS.get(name, {})

    if not row["reviewed"]:
        findings.append({"check": "unreviewed-set", "set": name, "detail": "no review entry; a capture that nobody read is not evidence"})
    else:
        eye = notes.get("eye") or []
        known = {str(f["file"]).split("/", 1)[-1] for f in row["files"]}  # type: ignore[index]
        for opened in eye:
            if str(opened) not in known:
                findings.append(
                    {
                        "check": "eye-names-a-missing-file",
                        "set": name,
                        "detail": f"the review says `{opened}` was opened, and the set does not hold it",
                    }
                )
    if not row["producer"]:
        findings.append({"check": "no-producer", "set": name, "detail": "neither a README nor a curated entry names the script that made these"})
    elif not (PROJECT_ROOT / str(row["producer"])).exists():
        findings.append({"check": "missing-producer", "set": name, "detail": f"`{row['producer']}` does not exist"})

    if row["index"] is None:
        observations.append(
            {
                "check": "no-index",
                "set": name,
                "detail": "no index.json, so the set records neither when it was captured nor the checksum of what it holds",
            }
        )

    measured = any(f["luminance"] is not None for f in row["files"])  # type: ignore[index]
    if not measured:
        observations.append(
            {
                "check": "luminance-not-measured",
                "set": name,
                "detail": "ImageMagick `identify` is not installed here, so the theme claim of these files was not measured",
            }
        )

    for entry in row["files"]:  # type: ignore[index]
        theme = entry["theme"]
        lum = entry["luminance"]
        if theme and lum is not None and not theme_is_consistent(theme, lum):
            findings.append(
                {
                    "check": "theme-contradicted",
                    "set": name,
                    "detail": f"{entry['file']} is named `{theme}` but measures {lum} mean luminance",
                }
            )
        if entry["width"] < 64 or entry["height"] < 32:
            findings.append(
                {
                    "check": "degenerate-capture",
                    "set": name,
                    "detail": f"{entry['file']} is {entry['width']}x{entry['height']}, which is not a capture of anything",
                }
            )

    folders = {(media_root / str(entry["file"])).parent for entry in row["files"]}  # type: ignore[index]
    folders.add(media_root / name)
    for folder_path in sorted(folders):
        index_path = folder_path / "index.json"
        if not index_path.exists():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        folder = index_path.parent
        listed = {str(shot["file"]): shot for shot in index.get("shots", [])}
        on_disk = {
            Path(str(entry["file"])).name: entry
            for entry in row["files"]  # type: ignore[index]
            if (media_root / str(entry["file"])).parent == folder
        }  # type: ignore[index]
        for filename, shot in listed.items():
            entry = on_disk.get(filename)
            if entry is None:
                findings.append({"check": "index-drift", "set": name, "detail": f"index.json lists `{filename}`, which is not on disk"})
                continue
            if shot.get("sha256") != entry["sha256"]:
                findings.append(
                    {
                        "check": "index-drift",
                        "set": name,
                        "detail": f"{filename}: index sha256 {str(shot.get('sha256'))[:12]} != disk {str(entry['sha256'])[:12]}",
                    }
                )
            if shot.get("bytes") not in (None, entry["bytes"]):
                findings.append(
                    {
                        "check": "index-drift",
                        "set": name,
                        "detail": f"{filename}: index says {shot.get('bytes')} bytes, disk has {entry['bytes']}",
                    }
                )
        for filename in on_disk:
            if filename not in listed:
                findings.append(
                    {
                        "check": "unindexed-capture",
                        "set": name,
                        "detail": f"{folder.relative_to(media_root).as_posix()}/{filename} is not in that folder's index.json",
                    }
                )
        viewport = index.get("viewport") or {}
        scale = int(viewport.get("device_scale_factor") or 1)
        expected = (int(viewport.get("width") or 0) * scale, int(viewport.get("height") or 0) * scale)
        if expected[0]:
            for entry in on_disk.values():
                if (entry["width"], entry["height"]) != expected:
                    findings.append(
                        {
                            "check": "viewport-mismatch",
                            "set": name,
                            "detail": f"{entry['file']} is {entry['width']}x{entry['height']}, the index says {expected[0]}x{expected[1]}",
                        }
                    )
    return findings, observations


def coverage(sets: list[dict[str, object]]) -> dict[str, object]:
    """Which pane has been photographed, in which theme, at which width.

    A capture is attributed to a pane by the token in its own filename (including the aliases the
    ledger records: `council` is the Hall, `debate` is Compare). A state shot that names no pane —
    `paused-panel.png`, `failed-run.png` — is counted under its set, not the grid, and the count of
    those is reported rather than spread over panes it may not belong to.
    """
    grid: dict[str, dict[str, int]] = {
        pane: {"sets": 0, "files": 0, "dark": 0, "light": 0, "narrow": 0, "desktop": 0} for pane in PANES
    }
    uncounted = 0
    for row in sets:
        touched: set[str] = set()
        for entry in row["files"]:  # type: ignore[index]
            tokens = {
                PANE_ALIASES[token]
                for token in PANE_ALIASES
                if re.search(rf"(^|[-_./]){token}([-_./]|$)", str(entry["file"]).lower())
            }
            if not tokens:
                uncounted += 1
                continue
            theme = entry["theme"]
            for pane in tokens:
                touched.add(pane)
                grid[pane]["files"] += 1
                if theme in ("dark", "light"):
                    grid[pane][theme] += 1
                if entry["css_width"] and entry["css_width"] <= NARROW_MAX:
                    grid[pane]["narrow"] += 1
                if entry["css_width"] and entry["css_width"] >= DESKTOP_MIN:
                    grid[pane]["desktop"] += 1
        for pane in touched:
            grid[pane]["sets"] += 1
    gaps = [f"{pane}: no capture at all" for pane in PANES if not grid[pane]["files"]]
    return {"grid": grid, "uncounted_files": uncounted, "gaps": gaps}


def published_files(media_root: Path, published: list[dict[str, object]]) -> list[dict[str, object]]:
    """The manifest's six shots, matched to the captures on disk, in manifest order."""
    if not published:
        return []
    manifest_path = media_root / "screenshots" / "manifest.json"
    if not manifest_path.exists():
        return published[0]["files"]  # type: ignore[return-value]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_path = {str(entry["file"]): entry for entry in published[0]["files"]}  # type: ignore[index]
    ordered = []
    for shot in manifest.get("shots", []):
        entry = by_path.get(f"screenshots/{shot['file']}")
        if entry is not None:
            ordered.append(entry)
    return ordered


def build(media_root: Path = MEDIA, *, measure: bool = True) -> dict[str, object]:
    """The whole review.

    `measure=False` skips the `identify` pass. The checks that need pixels say so in their own
    names, and a run without them records `luminance_measured: false` rather than a pass.
    """
    sets_raw = capture_sets(media_root)
    all_files = [png for entry in sets_raw.values() for png in entry["files"]]
    luminances = mean_luminance(all_files) if measure else None
    failing = known_failing_walks()

    rows = [review_set(name, entry["files"], media_root, luminances, failing) for name, entry in sorted(sets_raw.items())]
    findings: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    for row in rows:
        set_findings, set_observations = check_set(row, media_root)
        findings.extend(set_findings)
        observations.extend(set_observations)

    cover = coverage(rows)
    for gap in cover["gaps"]:
        findings.append({"check": "pane-never-captured", "set": "-", "detail": gap})

    published = [row for row in rows if row["set"] == "screenshots"]
    file_count = sum(len(row["files"]) for row in rows)
    return {
        "schema_version": "1.0.0",
        "document_type": "m5_uiux_screenshot_review",
        "program": "M5 2.0 UI/UX transformation",
        "story": "UIUX-P6-03",
        "generated_by": "scripts/uiux_screenshot_review.py",
        "regenerate_with": "python3 scripts/uiux_screenshot_review.py",
        "check_with": "python3 scripts/uiux_screenshot_review.py --check",
        "statement": (
            "Every committed capture, the script that made it, what a reviewer concluded about it, and the "
            "checks that keep the claim honest: the index agrees with the disk, the theme in the filename "
            "is the theme in the pixels, the producer still exists, and a set that was re-shot in this "
            "story says so. A set nobody reviewed is a finding."
        ),
        "luminance_measured": luminances is not None,
        "totals": {
            "sets": len(rows),
            "files": file_count,
            "reviewed_sets": sum(1 for row in rows if row["reviewed"]),
            "sets_eyed": sum(1 for row in rows if row["eye"]),
            "files_eyed": sum(len(row["eye"]) for row in rows),
            "re_taken_sets": sum(1 for row in rows if row["re_taken"]),
            "published_sets": len(published),
            "findings": len(findings),
            "observations": len(observations),
        },
        "coverage": cover,
        "published": {
            "directory": "docs/media/screenshots",
            "manifest": "docs/media/screenshots/manifest.json",
            "validator": "scripts/validate_screenshots.py",
            "re_taken_in_this_story": bool(published and published[0]["re_taken"]),
            # Only the six files the manifest names, in manifest order: the set also holds a
            # narrow variant in `p4-01-narrow-dark/`, which is not part of the published set and
            # is counted under the set rather than listed as if the README showed it.
            "files": published_files(media_root, published),
        },
        "sets": rows,
        "findings": findings,
        "observations": observations,
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    coverage_data = payload["coverage"]
    lines = [
        "# The screenshot review — every committed capture, and what it still means",
        "",
        "Generated by [`scripts/uiux_screenshot_review.py`](../scripts/uiux_screenshot_review.py) for",
        "`UIUX-P6-03`; the checked artifact is",
        "[`product/M5_2_0_UIUX_Screenshot_Review.json`](../product/M5_2_0_UIUX_Screenshot_Review.json).",
        "Do not hand-edit.",
        "",
        "The program committed screenshots for every story and never looked at them together. This is",
        "that pass: **" + str(totals["files"]) + " captures across " + str(totals["sets"]) + " sets**, each traced to the script",
        "that made it, checked against the index beside it, measured for the theme it claims, and",
        "reviewed by a person who wrote down what it shows.",
        "",
        "## What the review changed",
        "",
        "The six images the README shows were taken on **2026-11-01**, before twenty-two UI stories.",
        "They were missing the P5-03 criteria table, the P5-04 readiness strip and the P2 status",
        "components, and the Compare capture carried a horizontal overflow the later stories fixed.",
        "This story re-captured them from this tree with the repository's own",
        "[`scripts/capture_screenshots.mjs`](../scripts/capture_screenshots.mjs) and photographed the",
        "same six panes again in the dark theme, which the published set never had. `before/` and",
        "`after-dark/` in `docs/media/uiux-p6-03` keep the replaced set and the dark set, with an",
        "`index.json` each; the before images were extracted with `git show` from the commit that held",
        "the old set, and their checksums are recomputed rather than copied.",
        "",
        "## The published set",
        "",
        "| Pane | File | Size | Mean luminance | Theme |",
        "|---|---|---|---|---|",
    ]
    for entry in payload["published"]["files"]:  # type: ignore[index]
        lines.append(
            f"| {entry['name'].replace('.png', '')} | `{entry['file']}` | {entry['width']}x{entry['height']} "
            f"| {entry['luminance'] if entry['luminance'] is not None else 'not measured'} | {entry['theme'] or 'OS default (light)'} |"
        )
    lines += [
        "",
        "`python3 scripts/validate_screenshots.py` validates this set independently, against the",
        "manifest and the UI's own tab list; this review does not replace it.",
        "",
        "## Every set",
        "",
        "| Set | Story | Producer | Files | Index | Theme | Re-shot here | Reviewed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in payload["sets"]:  # type: ignore[index]
        index = "yes" if row["index"] else "no"
        reviewed = "eyed " + str(len(row["eye"])) if row["eye"] else ("reviewed" if row["reviewed"] else "**no**")
        lines.append(
            f"| `{row['directory']}` | {row['story'] or '—'} | "
            f"{('`' + str(row['producer']) + '`') if row['producer'] else '**none**'} | {len(row['files'])} | {index} "
            f"| {row['theme_claim'] or '—'} | {'yes' if row['re_taken'] else 'no'} | {reviewed} |"
        )
    lines += [
        "",
        "## What a reviewer concluded",
        "",
    ]
    for row in payload["sets"]:  # type: ignore[index]
        if not row["note"]:
            continue
        lines.append(f"* **`{row['directory']}`** — {row['note']}")
    lines += [
        "",
        "## Coverage",
        "",
        "| Pane | sets | captures | dark | light | narrow (<=600px) | desktop (>=800px) |",
        "|---|---|---|---|---|---|---|",
    ]
    for pane, counts in coverage_data["grid"].items():  # type: ignore[union-attr]
        lines.append(
            f"| {pane} | {counts['sets']} | {counts['files']} | {counts['dark']} | {counts['light']} "
            f"| {counts['narrow']} | {counts['desktop']} |"
        )
    lines += [
        "",
        f"{coverage_data['uncounted_files']} captures are state shots rather than pane shots (a dialog,",
        "a refusal, a token sheet) and are counted under their set rather than the grid.",
        "",
        "## What the checks enforce",
        "",
        "* **a producer that exists** — a README naming a script, or a curated entry; a set nobody can",
        "  re-take is a picture, not evidence;",
        "* **the index agrees with the disk** — sha256, byte count, viewport and file set;",
        "* **the theme in the name is the theme in the pixels** — mean luminance measured with",
        "  ImageMagick; dark captures sit at 0.09–0.17 and light at 0.52–0.97, and the thresholds split",
        "  that gap instead of fitting one set;",
        "* **every set has a review** — `--check` fails when a set on disk has no entry in `REVIEWS`, so a",
        "  capture cannot ship unreviewed;",
        "* **the eye-list is real** — a review that names a file the set does not hold is a finding.",
        "",
        "## What this does not claim",
        "",
        "* it does not replace `scripts/validate_screenshots.py` for the published set, or",
        "  `scripts/validate_uiux_interaction_polish_screenshots.py` for the P5-05 grid; those check their",
        "  own sets and this review says so;",
        "* it does not re-run the seventeen walks that produced the other sets — the review re-shot the",
        "  published set and this story's own captures, and every other set is reviewed as it stands with",
        "  the command that would re-take it named in the table;",
        "* it does not judge taste. It checks that a capture is traceable, current and honest about its",
        "  own theme and size; whether the pane is *good* is the owner's call, and the ledger records that.",
        "",
    ]
    if payload["observations"]:
        lines += ["## Observations (recorded, not failures)", ""]
        lines += [f"* **`{row['set']}`** — {row['detail']}" for row in payload["observations"]]  # type: ignore[index]
        lines.append("")
    if payload["findings"]:
        lines += ["## Findings", ""]
        lines += [f"* **{row['check']}** — `{row['set']}`: {row['detail']}" for row in payload["findings"]]  # type: ignore[index]
        lines.append("")
    return "\n".join(lines)


def without_pixels(payload: dict[str, object]) -> dict[str, object]:
    """The review with everything only a machine that can read pixels could reproduce removed.

    Used by `--check` and by the artifact test when ImageMagick is not installed: the structure,
    the producers, the indexes and the coverage are compared, and the pixel pass says it did not
    run rather than passing itself.
    """
    clone = json.loads(json.dumps(payload))
    clone.pop("luminance_measured", None)
    clone["observations"] = [row for row in clone["observations"] if row["check"] != "luminance-not-measured"]
    clone["findings"] = [row for row in clone["findings"] if row["check"] != "theme-contradicted"]
    clone["totals"]["findings"] = len(clone["findings"])
    clone["totals"]["observations"] = len(clone["observations"])
    for row in clone["sets"]:
        for entry in row["files"]:
            entry.pop("luminance", None)
            entry.pop("theme_measured", None)
    for entry in clone["published"]["files"]:
        entry.pop("luminance", None)
        entry.pop("theme_measured", None)
    return clone


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the review instead of writing it")
    parser.add_argument("--media", type=Path, default=MEDIA, help="media root (default: docs/media)")
    args = parser.parse_args(argv)

    payload = build(args.media)
    text = dumps(payload)
    doc = render_doc(payload)
    totals = payload["totals"]

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        drifted = []
        committed = json.loads(JSON_PATH.read_text(encoding="utf-8")) if JSON_PATH.exists() else None
        same_json = committed is not None and committed == payload
        same_doc = DOC_PATH.exists() and DOC_PATH.read_text(encoding="utf-8") == doc
        if committed is not None and not payload["luminance_measured"] and not (same_json and same_doc):
            # No `identify` here: the committed artifact carries measurements this machine cannot take,
            # so compare everything except those and check that the committed page is the committed
            # artifact's own rendering. Say so rather than passing silently.
            same_json = without_pixels(committed) == without_pixels(payload)
            same_doc = DOC_PATH.exists() and DOC_PATH.read_text(encoding="utf-8") == render_doc(committed)
            if same_json and same_doc:
                print(
                    "note: ImageMagick `identify` is not installed here, so the pixel checks were not re-run; "
                    "the rest of the review matches the committed artifact, and the committed page matches it.",
                    file=sys.stderr,
                )
        if not same_json:
            drifted.append(str(JSON_PATH.relative_to(PROJECT_ROOT)))
        if not same_doc:
            drifted.append(str(DOC_PATH.relative_to(PROJECT_ROOT)))
        if drifted or payload["findings"]:
            if drifted:
                print("error: the screenshot review has drifted from the tree.", file=sys.stderr)
                print("       Regenerate it (the change is part of the story, not a fix-up):", file=sys.stderr)
                print("           python3 scripts/uiux_screenshot_review.py", file=sys.stderr)
                for path in drifted:
                    print(f"       changed: {path}", file=sys.stderr)
            for finding in payload["findings"]:
                print(f"finding: {finding['check']} — {finding['set']}: {finding['detail']}", file=sys.stderr)
            return 1
        print(
            f"screenshot review OK: {totals['files']} captures in {totals['sets']} sets, "
            f"{totals['reviewed_sets']} reviewed ({totals['files_eyed']} files opened), "
            f"{totals['re_taken_sets']} re-shot, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{totals['files']} captures in {totals['sets']} sets, {totals['reviewed_sets']} reviewed, "
        f"{totals['findings']} findings"
    )
    return 0 if not payload["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
