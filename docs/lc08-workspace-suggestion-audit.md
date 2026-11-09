# LC08 workspace suggestion audit — dormant continuation

## Scope and coverage accounting

This follow-up to merged PR266/base `76cf461` audits the single
`api_workspace_picker_suggest` owner, not folder creation, workspace switching,
other workflows or installed native engines. No route/schema, capability grant,
backend installation, activation, retention/recovery policy or acceptance change
is introduced. Legacy remains the default.

The [owner matrix](../tests/fixtures/runtime_dispatch/workflow_coverage.json)
retains **29 owners / 54 static sites**: two shared Council/Debate adapter owners,
one HTTP Hall owner, one MCP owner and now one workspace-suggestion owner have
bounded assessments; **24 owners remain inventory-only**. Council/Debate assembly
closures remain separately named. This accounting is not a permission or native
qualification oracle.

## Reproduced finding and minimal change

The unchanged-source baseline produced **1 failed / 15 passed**. Overlong model
output was correctly rejected, but the route raised from the consuming loop
before its owned dispatch source closed. The assertion runs before loop shutdown
or generator finalization, so delayed cleanup cannot disguise the leak.

The route now explicitly closes that source within the existing timeout scope.
No prompt, response field, consent check, timeout duration, output-size guard,
model, output budget, reasoning option or error status/message changes. There is
no new retry, fallback, cleanup command or physical-release claim.

Existing contracts are preserved:

- Exact `confirmed: true`, an explicitly named model and a nonempty description
  of at most1,000 stripped characters are required before dispatch. Longer input
  is refused, not silently compacted. Existing outer whitespace normalization is
  retained and tested; accepted Unicode description text is otherwise complete.
- The fixed folder-name-only system instruction and description are the entire
  request: no workspace paths, listings, file contents or credentials are added.
  Original options remain128output tokens, reasoning off and temperature zero.
- This text-only consumer clears inherited native tool schemas for the request
  and restores the prior context on success, rejection, timeout or cancellation.
  Unexpected native structural calls are refused, not flattened or executed.
- Content above4,096characters is rejected. Reasoning-only output, invalid JSON,
  unsafe names, paths and extra action fields do not become folder authority.
- A successful response is still a suggestion with `created: false` and
  `workspace_changed: false`. There is no model-driven filesystem action,
  workspace switch, transcript write or automatic model cleanup.

## Executable evidence and limits

[Picker tests](../tests/test_workflow_picker_coordinated.py) add **22 cases**:
full maximum-length Unicode input/options and incoming tool-context restoration;
seven invalid consent/input cases; overlong output with prompt source closure;
three reasoning/unsafe-output refusals; unexpected native tools; private partial
provider failure with no retry; real task cancellation and a short synthetic
expiry of the unchanged45second timeout; plus six dormant native cases.

The six native cases exercise actual warm executor, fit admission, coordinator,
memory accounting and Ollama/LM Studio bridge code with **synthetic** transport,
capabilities, profiles, observations and counts. They check complete application
payloads and final counted/sent native bytes, valid suggestion parsing, pre-send
context refusal and blocked-transport cancellation. Native fixtures supply JSON
suggestion packets rather than generic ACK text. Ollama's fake approved profile
explicitly matches128/off/zero-temperature before warm admission; LM Studio's off
capability remains an explicit fake installed assumption.

Fake positive counts are1,000; refusal counts are8,100plus128output reserve
against the fixture's8,192context. They test fit arithmetic, not actual installed
tokenization or capacity for this Unicode input. Refusal sends no inference.
Cancellation closes the transport while retaining one active request, paused
residency and the600byte synthetic charge; it does not prove native termination
or physical release. No native backend is composed into the served application.

Tests trap any call to the folder-creation helper and any model-control command;
existing [host-path tests](../tests/test_workspace_paths.py) also cover filesystem
noncreation, authenticated/origin/rate-limit boundaries and unsafe model text.
The isolated standalone test now includes full suggestion input/context, early
output refusal/closure and structural-output refusal, with public siblings
blocked. Source mutants compiled only in isolated subprocess memory are caught:
remove closing (**1 failure**), truncate input (**1 failure**), claim creation
(**3 failures**). Tracked runtime files were not mutated for these experiments.

## Validation and remaining work

The new suite passes22cases. The ten-module picker/workflow/native/host-path/
Council/Pipeline suite passes **327 tests, 1 upstream warning in30.91s**;
three-module picker/workflow/host-path regression passes118tests/1warning.
Ruff, mypy75 and scope-map206modules/13areas pass. The complete app-derived
artifact refresh passes backend302tests/16isolation checks (unqualified/blocked),
UI37/37checks/7gates/0findings and restored V2review7/7local/2200visual/15Compare/
0findings. Source64files/6tabs/121routes, preservation272controls, docs264files/
ADRs, baseline, frontend syntax, app/standalone startup/auth/origin smokes,
screenshots6/6 and demo checks pass. Full local regression at `2f7bd77` completed
**5,138 passed, 32 skipped, 1 upstream warning in976.21s**. Its PR exact-head
workflow run42579603416 passed Python3.11/3.12/3.13 and Browser safety (all four
checks green); the report is updated in the follow-up documentation commit. No
activation or acceptance advancement is implied. Historical approvals, imported
browser evidence and independent security-release decisions are not relabelled.

This is not canonical tool support, folder-creation authorization, all-workflow
integration, cold multistage qualification, oMLX inference qualification, LC09
recovery, hardware acceptance or independent review. G1remains partial; G2–G6 and
LC09–LC11 remain open. Test8remains paused/unpassed.
