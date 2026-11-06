# Chat streaming, tools, handoff and recovery

Story **UIV2-20** — Chat streaming, tools, handoff and recovery UX.

| capability | what it says |
| --- | --- |
| copy | a labelled Copy per code block, awaited, with a selection fallback and one shared
| | `role=status` announcement |
| tools | a disclosure per call: status in a word, step, time, argument bytes, arguments,
| | output and the server's 500-character cap |
| streaming | measured elapsed time, first token and size; Stop keeps the partial reply and
| | says who stopped it and how much was kept |
| retry | nothing ran → Retry; a tool ran → Ask again (N tool calls already ran), a new
| | question with the failed attempt kept and the note on the question |
| handoff | a brief naming the messages, the tool calls and the context; it posts nothing |

## Checks

29 checks, 0 finding(s).

| check | result | detail |
| --- | --- | --- |
| a code block's copy control names the block and its size | pass | aria-label carries the language and the character count; the wrap carries data-lang |
| copying awaits the clipboard and reports what happened | pass | copyCodeBlock() awaits, sets data-state and says Copy unavailable when neither path works |
| a refused clipboard is copied through a selection fallback | pass | writeClipboard() falls back to a selected textarea before it reports failure |
| the code-block copy never touches the clipboard directly | pass | copyCodeBlock() goes through writeClipboard(), which awaits; the sheet's other 11 call sites belong to other panes and are unchanged by this story |
| a copy is announced, not just painted | pass | copyAnnounce() fills one shared role=status region and copyCodeBlock() calls it |
| a tool call is a disclosure, not a line of text | pass | renderChatTool() returns a <details> with a <summary> |
| its status is a word, not only a colour | pass | the summary prints the status label |
| it states the step, the time and the size of what it was given | pass | step, elapsed milliseconds and argument bytes in the summary |
| opening it shows the arguments, the output and the server's cap | pass | the output label names the 500-character cap |
| the turn counts its tool calls and says whether they worked | pass | chatToolSummary() counts the calls, the failures and the total time |
| the tools are rendered into the turn, above the reply | pass | every tool in `message.tools` is drawn as a disclosure |
| a streaming turn reports elapsed time, first token and size | pass | chatStreamingMeta() measures rather than guesses |
| stopping records how much of the answer had arrived | pass | the stopped handler freezes the character count before it renders |
| a stopped turn says who stopped it and how much was kept | pass | the meta line names the reader and the characters kept |
| stopping hands the composer back with the cursor in the field | pass | the stop handler focuses the composer when the reader stopped it |
| a retry's meaning is decided by what the failed attempt did | pass | chatRetryPlan() reads the failed turn's tool count |
| a turn that ran nothing is a plain retry that replaces the placeholder | pass | no tool calls: Retry, replace |
| a turn that ran tools is asked again, not retried silently | pass | tool calls: Ask again (N tool calls already ran), keep the failed turn |
| the failure card carries the plan and says what will not be repeated | pass | presentChatError() prints the plan's sentence and label |
| retrying keeps the failed turn when it did something | pass | retryChatRequest() splices only when the failed turn is a placeholder |
| the question asked again says so on the question | pass | the user turn carries retryOf, and the transcript renders it |
| the handoff brief names the messages and the tool calls it carries | pass | chatHandoffBrief() counts the conversation and lists the calls |
| the brief carries the context the last reply was sent with | pass | the same context line the transcript shows |
| the handoff explains why it is off, or what it will do | pass | chatHandoffReason() covers nothing, streaming, handed and ready |
| the handoff posts nothing and starts nothing | pass | the handoff writes a composer's text, never a request |
| the browser walk exists and asserts the copy, the tool disclosure and the retry plan | pass | chat_recovery_walk.mjs: 25 recorded checks, clipboard read back, fallback forced, tool disclosure and retry plan asserted |
| the walk covers the six states the ledger asks for | pass | STATES = empty, populated, long, loading, error, recovery |
| the walk reads the clipboard rather than trusting the button | pass | the copied text is compared to the block's own text |
| the state captures are present for every viewport and theme, with a before set | pass | 48 after PNG, 16 before PNG |

## States

`empty`, `populated`, `long-content`, `loading`, `error`, `recovery`.

## Regenerate

```
python3 scripts/uiux_chat_recovery_inventory.py
python3 scripts/uiux_chat_recovery_inventory.py --check
```
