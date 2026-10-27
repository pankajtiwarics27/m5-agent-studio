# Checkpoints and safe resume

AS-033 defines `WorkflowCheckpointStore` in
[`workflow_checkpoints.py`](../workflow_checkpoints.py). It persists a JSON
checkpoint payload, step number, state, and SHA-256 integrity hash in SQLite.

Interrupted workflows can be resumed only after the payload hash is verified;
active, completed, or missing checkpoints are rejected. Interruption, resume,
and checkpoint writes create durable audit entries. The checkpoint store uses
SQLite transactions and does not repeat provider or tool side effects.

## Evidence

`tests/test_workflow_checkpoints.py` covers checkpoint persistence, interrupted
state, restart-safe resume, integrity tampering rejection, invalid resume
states, and durable audit records.
