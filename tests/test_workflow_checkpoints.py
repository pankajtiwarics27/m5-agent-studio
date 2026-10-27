from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workflow_checkpoints import (
    CheckpointError,
    ResumeRejected,
    WorkflowCheckpointStore,
)


def test_checkpoint_interrupt_and_resume_survive_restart_with_audit(tmp_path: Path) -> None:
    database = tmp_path / "checkpoints.db"
    store = WorkflowCheckpointStore(database)
    saved = store.save("workflow-1", 3, {"model": "fixture", "output": ["safe"]})
    interrupted = store.mark_interrupted("workflow-1")
    assert interrupted.state == "interrupted"

    restarted = WorkflowCheckpointStore(database)
    resumed = restarted.resume("workflow-1")
    assert resumed.step == saved.step
    assert resumed.payload == saved.payload
    assert resumed.state == "resumed"
    assert [entry.event for entry in restarted.audit("workflow-1")] == [
        "checkpoint_saved",
        "workflow_interrupted",
        "workflow_resumed",
    ]


def test_tampered_payload_cannot_resume(tmp_path: Path) -> None:
    database = tmp_path / "checkpoints.db"
    store = WorkflowCheckpointStore(database)
    store.save("workflow-1", 1, {"result": "original"})
    store.mark_interrupted("workflow-1")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE workflow_checkpoints SET payload_json = ? WHERE workflow_id = ?", ('{"result":"tampered"}', "workflow-1"))
    with pytest.raises(ResumeRejected, match="integrity"):
        store.resume("workflow-1")


def test_only_interrupted_checkpoints_resume_and_invalid_input_fails(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore(tmp_path / "checkpoints.db")
    with pytest.raises(CheckpointError, match="workflow_id"):
        store.save("", 0, {})
    store.save("workflow-1", 0, {})
    with pytest.raises(ResumeRejected, match="only interrupted"):
        store.resume("workflow-1")
    with pytest.raises(CheckpointError, match="unknown checkpoint state"):
        store.save("workflow-2", 0, {}, state="unknown")  # type: ignore[arg-type]
