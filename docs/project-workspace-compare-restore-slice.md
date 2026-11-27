# Compare: do not replace restored results with an empty state

On page load, Compare restores a browser-kept run before the shell's final initialization.
The shell then unconditionally drew “No comparison yet” into the same host as the
arbiter verdict. A real browser reproduced both a restored verdict replaced by that card
and a restored single stopped panel shown alongside it. The boot-time empty presentation
now appears only when there are neither restored panels nor a verdict. It does not change
storage, dispatch, results, provider selection or approval.

The real continuity walk now checks a fresh empty pane, a saved two-panel verdict after
reload, and a saved single stopped panel after reload. It passed 18/18 checks. Reintroducing
the unconditional call made both restored-state checks fail; removing the empty-state call
made the fresh-state check fail. The restored walk passed. The mandatory browser wrapper
now runs this walk and checks its 18/18 summary; a stale count mutation failed, and the
restored six-test wrapper passed. The browser fixture uses real routes and a local
stand-in runtime; this is not owner visual acceptance or a pilot release
qualification. The verdict comparison checks rendered verdict body and model; provenance
copy may legitimately differ on restoration.

The longer boot code also exposed a separate inventory limitation: its fixed 400-character
window falsely reported both startup presentations missing. The inventory now examines the
bounded boot section and fails closed when its anchors are absent. Tests caught mutations
that restored the fixed window or silently accepted a missing end anchor. This remains a
static presence check, not proof that a call executes; the browser walk supplies behavioral
coverage. The focused Compare suite passed 110 tests and the presentation suite 24 tests.
The broader accessibility, performance, and cross-tab walks passed 120/120, 21/21 and
2200/2200 checks respectively. Backend safety passed 309 tests and 16 isolation checks;
isolation remains unqualified.
