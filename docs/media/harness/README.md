# What the Harness will do with the task you are about to run

Captures of the Harness pane taken by
`node scripts/harness_settings_walk.mjs --screenshots docs/media/harness` against
`python3 scripts/demo_tour.py --serve`.

This host has no model runtime, so nothing here is a finished run: the walk reads the plan
the composer prints, changes each control the plan claims to read, presses Run with no task
to reach the refusal, and hands a conversation from Chat to the Harness to reach the banner
that answers it. The conversation in the two `brief-*` captures is written by the walk
through the same `chatHistory` shape `sendChatRequest()` builds, because a real one would
need a model to answer. Everything else is the pane reacting to its own controls and to the
server it was pointed at.

| File | What it shows |
|---|---|
| `plan.png` | The Harness at 1440×900: `RUN WILL` above Run, then `DIRECTORY /tmp/m5-demo-…/workspace · direct`, `POLICY Standard (Ask before Bash)`, `MODELS [Combo • Auto-Fallback] Local M5 Multi-Model Fallback (2 targets) · critic same`, `WORKFLOW Autonomous Turn (Step Loop) · Ponytail + Concise · Low · 10 steps` — the same three values the collapsed **Run settings** group now carries in its summary, and the sentence beside Run saying a task is needed. |
| `no-task.png` | The composer with an empty box: Run off, and the amber sentence beside it saying `Describe the task in the box above, then Run. Nothing runs until there is one.` Pressing Run here starts nothing and returns the cursor to the box. |
| `brief-from-chat.png` | A conversation handed over from Chat: the **From Chat** banner counting `2 messages, 2 tool calls (1 failed)` and naming both settings this run changes — the model `demo-local-model -> combo/hybrid-resilient` and the directory `/tmp/chat-read-project -> /tmp/p3-01-walk-project` — with the whole carried brief visible in the box that grew to hold it, and the plan below it. |
| `brief-aligned.png` | The same handoff after choosing `demo-local-model` and typing `/tmp/chat-read-project`: the banner flips to `This run uses the same model and project directory it was read with.` and the plan and the brief now agree. |
| `plan-narrow.png` | The same surface at 900px: the plan wraps inside the composer with no horizontal overflow, the hint and Run still reachable. |

The directory the walk types into **Project Directory** is never saved: the walk writes the
input and dispatches a change so the plan follows it, and never presses **Set / Create**, so
no workspace setting was written for a capture.

Regenerate all five with the command above. See
`docs/uiux-2-0-harness-settings.md` for the rules behind them.
