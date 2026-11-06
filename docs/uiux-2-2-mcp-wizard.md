# Connecting an MCP server is a guide, not a form

Story **UIV2-22** — MCP guided connect wizard and advanced configuration.

The editor the hub opens asks four questions, one screen at a time. Nothing is saved, and no
connection is attempted, until the last of them is answered.

| step | question | what it holds |
| --- | --- | --- |
| 1 Server | what is it called? | alias and display name |
| 2 Transport & credentials | how is it reached? | remote HTTP (URL, auth header, token) or a local command (command, arguments), plus one closed disclosure for raw headers and environment JSON |
| 3 Policy & consent | what does a call cost? | approval policy with its consequence in words; for a local command, an explicit consent naming the exact command, and a note that the screen cannot grant the server's own `STUDIO_ALLOW_MCP_STDIO` permission |
| 4 Review & connect | is this what I want, and does it answer? | the seven things that will be saved, then `Save & test connection`, a testing state, and the result the server gave |

A step that is not satisfied does not advance: it says why in a live region, puts the cursor in
the field that needs it, and clears the sentence as soon as the field satisfies it. The review
never reads a typed token back — it says a new token will be stored, and a stored one is named
only by the mask the server already sent.

## Checks

34 checks, 0 finding(s).

| check | result | detail |
| --- | --- | --- |
| connecting a server is four named steps, not one form | pass | rail: 1 · 2 · 3 · 4 · panels: 1, 2, 3, 4 |
| the guide asks four questions in order: what, how, what it costs, and is it right | pass | 1 Server · 2 Transport and credentials · 3 Policy and consent · 4 Review and connect |
| one step is on screen at a time, and starting over goes back to the first | pass | mcpWizardGo() hides every other panel; opening the editor resets to step 1 |
| the rail says where you are, in a live region, and marks the step it is on | pass | aria-current=step on the current item, done on the ones behind, `Step N of 4 · label` in role=status |
| each step is a labelled group whose heading names the question | pass | role=group, aria-labelledby the step heading, heading focusable for the keyboard |
| no step can advance until it is satisfied, and the refusal is words, not a shake | pass | mcpWizardValidate() returns the sentence, mcpWizardNext() refuses to move while it does |
| a refusal puts the cursor in the field that needs it, and clears when satisfied | pass | mcpWizardProblem() names the reason, focuses the field, and mcpWizardRefresh() clears it |
| Back and Continue are real buttons, and Continue leaves the last step | pass | Back is disabled on step 1; Continue disappears on step 4, where Save & test connection is |
| step 2 shows only the fields the chosen transport needs | pass | toggleMcpTransportFields() shows one block and hides the other |
| the transport says what will happen, in words | pass | a sentence per transport, not a badge or a colour |
| the token is a password field that is never pre-filled or auto-completed | pass | type=password, autocomplete=off, and the editor never writes a stored secret into it |
| raw headers and environment JSON are one closed disclosure, off the guided path | pass | `Advanced: raw headers and environment JSON`, shut, holding both JSON fields |
| the policy step explains what each policy costs | pass | three sentences, one per policy, rendered next to the select |
| a local command needs consent that is given, not implied | pass | the consent row only exists for stdio and mcpWizardValidate(3) refuses until it is ticked |
| the consent names the command it is about | pass | mcpConsentCommand is re-rendered from the exact command and arguments |
| the consent says it cannot grant the operator's permission | pass | the note names the server-side opt-in and disclaims it, so the screen is not mistaken for a gate |
| the review answers what, how, what it costs and whether consent was given | pass | Alias · Display name · Transport · Target · Credential · Approval policy · Local command consent |
| the review never reads the typed token back out | pass | the presence of a typed token is a boolean; its characters are never interpolated |
| a stored token is kept when the field is left blank, and shown only as its mask | pass | unchanged-token semantics: blank means keep, and the mask the server sent is what is named |
| saving re-checks every earlier step, so a field edited after passing it cannot slip through | pass | the guard runs before the POST, not only on the way to it |
| saving says it is testing, then tests in the same request | pass | button label and disabled state restored in `finally`; the POST is the connection test |
| the result is visible where the button is, and says what was found or what failed | pass | ok / failed / testing carried as data-state on a live region inside the last step |
| the confirmation names the tool namespace and offers the server list | pass | a successful test ends with a way to the list, not a dead end |
| the typed secret leaves the field after a save | pass | the field is emptied on success, so the secret does not stay in the page |
| every control the old form had is still in the guide | pass | 20 controls: all present |
| all three approval policies survive inside the guide | pass | auto / inherit / always_ask in the policy select |
| the six MCP routes are unchanged and none were added | pass | 6 routes, no additions |
| consent on the screen does not replace the operator's stdio opt-in | pass | the server still refuses to spawn unless the operator set the variable; the page only records consent |
| the served payload is what masks a secret, not the page | pass | app.py masks on read and keeps a blank token as the stored one |
| a browser walk drives the guide on a screen | pass | mcp_wizard_walk.mjs: 38 recorded checks |
| the walk covers every state the ledger asks for | pass | empty · credentials · policy · review · loading · success · error · recovery · keyboard · long |
| the walk proves the secret never reaches the page, the console or another request | pass | the secret is typed into the form, then searched for in the DOM, the console and every request body |
| captures exist for every viewport and theme, with a before set | pass | 104 after PNG, 104 before PNG |
| the walk can photograph the old one-shot form too | pass | the same instrument runs against the pre-wizard editor to make the before set |

## States

`empty`, `populated`, `long-content`, `error`, `loading`, `recovery`.

`docs/media/mcp-wizard-uiv2-22/` holds the captures: `empty`, `credentials`, `policy`,
`review`, `loading`, `success`, `error`, `recovery`, `keyboard` and a `long` target, at four
viewports and in both themes, plus `before-uiv2-22/` for the same instrument run against the
one-shot form this story replaced.

## Regenerate

```
python3 scripts/uiux_mcp_wizard_inventory.py
python3 scripts/uiux_mcp_wizard_inventory.py --check
node scripts/mcp_wizard_walk.mjs --screenshots docs/media/mcp-wizard-uiv2-22
```
