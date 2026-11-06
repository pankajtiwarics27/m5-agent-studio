# Chat persona registry

Story **UIV2-19** — Chat persona registry audit and complete supported picker.

The picker used to be three `<option>` tags whose value was the system prompt, so nothing
could describe a persona, remember a choice, or recover from one being deleted. `CHAT_PERSONAS`
is the authority now, and `select#chatPersona` ships no options of its own.

## The registry

`CHAT_PERSONAS` in `static/index.html` — default `principal-engineer`, remembered
under `localStorage['m5_chat_persona']`.

| id | label | description | prompt |
| --- | --- | --- | --- |
| `principal-engineer` | Principal Engineer & UI Architect | Production-ready code with the reasoning behind it: named trade-offs, accessibility, no filler. | You are a concise Principal Software Engineer & UI Architect. Write clean, accessible, production-ready code. |
| `bug-ux-auditor` | Bug & UX Auditor | Reads what is on screen adversarially: bugs, race conditions, UX anti-patterns and security issues. | You are an adversarial code reviewer. Focus strictly on bugs, race conditions, UX anti-patterns, and security issues. |
| `code-only` | Code Only (Zero Fluff) | Answers in code blocks, with as little prose as the question allows. | Output code blocks only with minimal prose. |

## The picker

* `select#chatPersona` ships **0** options of
  its own; `renderChatPersonaPicker()` fills it from the registry on load and on every filter.
* `input#chatPersonaSearch` narrows by label or description, keeps the selected persona
  in the list, says so when it matches nothing else, clears on Escape and chooses on Enter.
* `#chatPersonaDescription` is one clamped line (the whole sentence is its title
  and its text) that says what the selected persona does.
* A stored id that is not in the table falls back to the default persona and says why.
* The request sends `chatPersonaSelected().prompt`; the id never reaches the wire.

## Checks

34 checks, 0 finding(s).

| check | result | detail |
| --- | --- | --- |
| the registry exists and holds at least the three personas the pane shipped | pass | 3 persona(s): principal-engineer, bug-ux-auditor, code-only |
| every persona has an id, a label, a description and a prompt | pass | principal-engineer: ; bug-ux-auditor: ; code-only:  |
| ids are stable slugs, not prose | pass | principal-engineer, bug-ux-auditor, code-only |
| ids, labels and prompts are all distinct | pass | 3 unique id(s), 3 unique label(s), 3 unique prompt(s) |
| every label fits a picker row | pass | longest 33 chars (limit 40) |
| every description explains the persona in one line | pass | 68-99 chars (band 30-140) |
| every prompt is a system prompt the pane can send | pass | 43-117 chars |
| the default persona is one the registry names | pass | CHAT_PERSONA_DEFAULT = principal-engineer |
| the pane remembers the selection under its own key | pass | CHAT_PERSONA_KEY = m5_chat_persona |
| the markup ships no persona options of its own | pass | 0 <option> in select#chatPersona |
| the picker is filled from the registry, with nothing hard-coded beside it | pass | renderChatPersonaPicker() filters CHAT_PERSONAS and writes persona.label |
| the picker escapes what it writes into the markup | pass | both the id and the label go through escapeHtml |
| every registry entry reaches the picker, and the count says how many | pass | the label shows (N) / N of M while filtering |
| the filter is labelled, and controls the picker it filters | pass | input#chatPersonaSearch with aria-label="Filter personas" and aria-controls |
| the filter reads labels and descriptions, never prompts | pass | chatPersonaMatches() looks at label and description only |
| the description line is announced, and the picker points at it | pass | select#chatPersona aria-describedby=chatPersonaDescription, whose own tag is aria-live=polite |
| the description says what the selected persona does, not what it is called | pass | updateChatPersonaDescription() writes persona.description, and names the label only when the filter matched nothing else |
| the description is one clamped line, with the whole sentence in its title | pass | nowrap + ellipsis in CSS, and the full text on the element's title |
| Enter chooses the first persona the filter matched | pass | the Enter handler resolves through chatPersonaMatches, not through options[0] |
| Escape clears the filter rather than leaving an empty list | pass | Escape empties the filter and redraws every persona |
| the filter never filters out the persona that is about to be sent | pass | the selected persona is kept in the list whatever the filter says |
| the request sends the selected persona's prompt | pass | sendChatRequest() builds the system message from chatPersonaSelected().prompt |
| nothing sends the select's value any more | pass | no read of #chatPersona.value outside the picker helpers |
| the context line still names the persona by its label | pass | chatPersonaLabel() reads the selected option's text |
| a stored id resolves through the registry | pass | chatPersonaById() searches the table and falls back to the default id |
| a persona cannot be synthesised from an unknown id | pass | chatPersonaById() returns a registry entry or nothing; it never builds one |
| a deleted persona is detected, not silently replaced | pass | the fallback note fires only when the stored id is absent from the table |
| the choice is written to storage when it is made | pass | selectChatPersona() stores the id, guarded against blocked storage |
| the picker is drawn before the pane is used | pass | boot draws the picker, then wires the filter and the change handler |
| the filter is the one documented exception to the control floor, and stays tappable | pass | min-height 32px (control floor 44px, target floor 24px), declared on the id selector |
| the picker and its filter share one row at every width, with no breakpoint of their own | pass | a two-column grid whose select shrinks by min-width, not a stacking breakpoint |
| the browser walk exists and asserts the prompt on the wire | pass | chat_persona_walk.mjs: 21 recorded checks |
| the walk covers the six states the ledger asks for | pass | STATES = empty, populated, long, loading, error, recovery |
| the state captures are present for every viewport and theme | pass | 48 PNG in docs/media/chat-persona |

## Regenerate

```
python3 scripts/uiux_chat_persona_inventory.py
python3 scripts/uiux_chat_persona_inventory.py --check
```
