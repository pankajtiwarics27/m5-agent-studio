from __future__ import annotations

from pathlib import Path

import pytest

from workflow_state import (
    PersistedWorkflowState,
    WorkflowStateError,
    WorkflowTransitionRejected,
)


def test_legal_transitions_persist_across_authority_restart(tmp_path: Path) -> None:
    database = tmp_path / "workflow.db"
    first = PersistedWorkflowState(database)
    created = first.create("run-1")
    running = first.transition("run-1", "running", expected_version=created.version)
    paused = first.transition("run-1", "paused", expected_version=running.version)

    restarted = PersistedWorkflowState(database)
    recovered = restarted.get("run-1")
    assert recovered == paused
    assert restarted.transition("run-1", "running", expected_version=paused.version).version == 3


def test_illegal_and_stale_transitions_are_rejected_without_mutation(tmp_path: Path) -> None:
    store = PersistedWorkflowState(tmp_path / "workflow.db")
    created = store.create("run-1")
    with pytest.raises(WorkflowTransitionRejected, match="not allowed"):
        store.transition("run-1", "completed")
    assert store.get("run-1") == created

    running = store.transition("run-1", "running")
    with pytest.raises(WorkflowTransitionRejected, match="version changed"):
        store.transition("run-1", "completed", expected_version=created.version)
    assert store.get("run-1") == running


def test_terminal_states_cannot_transition_and_duplicate_workflows_fail(tmp_path: Path) -> None:
    store = PersistedWorkflowState(tmp_path / "workflow.db")
    store.create("run-1")
    store.transition("run-1", "running")
    store.transition("run-1", "completed")
    with pytest.raises(WorkflowTransitionRejected, match="not allowed"):
        store.transition("run-1", "running")
    with pytest.raises(WorkflowStateError, match="already exists"):
        store.create("run-1")


def test_invalid_workflow_inputs_fail_closed(tmp_path: Path) -> None:
    store = PersistedWorkflowState(tmp_path / "workflow.db")
    with pytest.raises(WorkflowStateError, match="workflow_id"):
        store.create("")
    with pytest.raises(WorkflowStateError, match="unknown workflow state"):
        store.create("run-1", state="unknown")  # type: ignore[arg-type]
    with pytest.raises(WorkflowStateError, match="does not exist"):
        store.transition("missing", "running")
