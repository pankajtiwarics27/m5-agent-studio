# Persisted workflow state machine

AS-032 defines `PersistedWorkflowState` in
[`workflow_state.py`](../workflow_state.py). Workflow records are stored in
SQLite with a monotonic version and validated status transition graph:

`pending → running → paused/completed/failed/cancelled`, with paused and failed
states able to return to running or be cancelled. Terminal completed and
cancelled states cannot transition further.

Transitions use `BEGIN IMMEDIATE`, validate the current row and optional
expected version, then increment the version atomically. A new authority over
the same database recovers the current state, so process restart does not lose
workflow state. The module does not execute providers or tools.

## Evidence

`tests/test_workflow_state.py` covers creation, legal and illegal transitions,
version compare-and-set, restart recovery, duplicate creation, and terminal
state behavior.
