"""One durable-state contract, checked against every store that implements it.

Every subsystem that keeps its own JSON state file under `$HARNESS_STORE_DIR` is
listed here: containment (`process_supervision`), scoped grants (`scoped_grants`),
MCP external effects (`mcp_client`), security incidents (`sandbox_boundary`) and
artifact lineage (`artifact_lineage`). Each has a hand-written secure-write path,
so each can drift.

That is not hypothetical. The containment store fsynced its temp file but not the
containing directory, so the rename publishing the manifest was not durable — a
crash could roll `state.json` back behind a fence or a hold the caller had been
told was written. The other two stores did it correctly, which is precisely why a
per-store unit test would not have caught it and a shared conformance test would.

Every case here is asserted once per store, so adding a fourth durable store means
adding one entry to `STORE_CASES` and inheriting the whole contract.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Callable

import pytest

import artifact_lineage
import mcp_client
import process_supervision
import sandbox_boundary
import scoped_grants


def _write_containment(root: Path) -> None:
    process_supervision.ContainmentStore(store_dir=root).fence_origin(
        reason="contract", session_id="sess", task_id="task", epoch=1
    )


def _write_grants(root: Path) -> None:
    store = scoped_grants.ScopedGrantStore(root)
    store.ensure_session_policy("sess", "standard")
    # `ensure_session_policy` is idempotent; issuing always writes, so the state
    # file — and its fsync path — is exercised on every call.
    store.issue_grant(
        session_id="sess",
        capabilities=["workspace_read"],
        resource_scopes=["global:*"],
        ttl_seconds=60,
    )


def _write_mcp_effects(root: Path) -> None:
    store = mcp_client.McpExternalEffectStore(root)
    semantics = mcp_client.classify_mcp_tool_semantics(
        {"alias": "srv", "transport": "http"}, {"name": "create"}
    )
    store.stage_dispatch(
        action_id="act-contract",
        session_id="sess",
        task_id=None,
        epoch=0,
        semantics=semantics,
        arguments={"order": "A-1"},
        bound_idempotency_key="act-contract",
    )


def _write_security_incidents(root: Path) -> None:
    store = sandbox_boundary.SecurityIncidentStore(root)
    store.record_incident(
        reason="contract_incident",
        failed_guarantees=("container_network_isolation",),
        failed_dependencies=("dep:runtime:docker",),
        affected_backends=("docker",),
    )


def _write_artifacts(root: Path) -> None:
    registry = artifact_lineage.ArtifactRegistry(root)
    registry.record_artifact(
        "f" * 64,
        source_kind="baseline",
        source_ref="contract",
        trust="trusted",
        size=12,
    )


WriteFn = Callable[[Path], None]
ErrorCls = type[BaseException]

# (id, writer, state path relative to the store root, the store's integrity error)
STORE_CASES: list[tuple[str, WriteFn, Path, ErrorCls]] = [
    (
        "containment",
        _write_containment,
        Path("containment") / "state.json",
        process_supervision.ContainmentIntegrityError,
    ),
    (
        "grants",
        _write_grants,
        Path("grants") / "state.json",
        scoped_grants.GrantStoreIntegrityError,
    ),
    (
        "mcp_effects",
        _write_mcp_effects,
        Path("mcp_effects") / "state.json",
        mcp_client.McpEffectStoreIntegrityError,
    ),
    (
        "security_incidents",
        _write_security_incidents,
        Path("security_incidents") / "state.json",
        sandbox_boundary.SecurityIncidentIntegrityError,
    ),
    (
        "artifacts",
        _write_artifacts,
        Path("artifacts") / "state.json",
        artifact_lineage.ArtifactLineageIntegrityError,
    ),
]
_CASES = [pytest.param(w, rel, err, id=name) for name, w, rel, err in STORE_CASES]


def test_every_module_with_a_durable_state_file_is_covered() -> None:
    """Every `state.json` store must be added to STORE_CASES, not just written.

    Discovered from source rather than hard-coded, so a new store fails this test
    instead of silently skipping the contract. `studio.py` is the generated bundle
    and is skipped; it is rebuilt from these modules by definition.
    """
    repo_root = Path(__file__).resolve().parents[1]
    stores_in_source = {
        path.stem
        for path in repo_root.glob("*.py")
        if path.stem not in {"studio"}
        and "state.json" in path.read_text(encoding="utf-8")
    }
    covered_modules = {
        "containment": "process_supervision",
        "grants": "scoped_grants",
        "mcp_effects": "mcp_client",
        "security_incidents": "sandbox_boundary",
        "artifacts": "artifact_lineage",
    }
    covered = {
        covered_modules[relative.parts[0]] for _name, _write, relative, _err in STORE_CASES
    }
    assert stores_in_source == covered, (
        f"modules with a durable state file: {sorted(stores_in_source)}; "
        f"covered by this contract: {sorted(covered)}"
    )


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_state_file_and_directory_are_owner_only(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    assert issubclass(error, Exception)
    write(tmp_path)
    state_path = tmp_path / relative
    assert state_path.exists(), state_path
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_write_fsyncs_the_temp_file_and_the_containing_directory(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert relative.parts[-1] == "state.json"
    """Contents *and* the rename that publishes them have to reach the disk.

    Syncing only the file leaves the rename in the page cache: a crash can roll the
    manifest back to its previous version even though the caller was told the write
    succeeded.
    """
    write(tmp_path)
    counters = {"file": 0, "dir": 0}
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        try:
            is_dir = bool(os.fstat(fd).st_mode & 0o040000)
        except OSError:
            is_dir = False
        counters["dir" if is_dir else "file"] += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy)
    write(tmp_path)
    monkeypatch.undo()

    assert counters["file"] >= 1, "the temp file must be fsynced before the rename"
    assert counters["dir"] >= 1, "the containing directory must be fsynced after the rename"


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_a_symlinked_state_file_fails_closed(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    write(tmp_path)
    state_path = tmp_path / relative
    real = tmp_path / "elsewhere.json"
    state_path.rename(real)
    state_path.symlink_to(real)
    with pytest.raises(error):
        write(tmp_path)


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_a_symlinked_state_directory_fails_closed(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / relative.parent).symlink_to(outside, target_is_directory=True)
    with pytest.raises(error):
        write(root)


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_a_corrupt_state_file_fails_closed(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    """Refusing beats silently starting from an empty state and losing every record."""
    write(tmp_path)
    (tmp_path / relative).write_text("{not json", encoding="utf-8")
    with pytest.raises(error):
        write(tmp_path)


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_a_future_schema_version_fails_closed(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    """An older Studio must not downgrade a newer manifest."""
    write(tmp_path)
    (tmp_path / relative).write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
    with pytest.raises(error):
        write(tmp_path)


@pytest.mark.parametrize(("write", "relative", "error"), _CASES)
def test_no_temporary_files_are_left_behind(
    tmp_path: Path, write: WriteFn, relative: Path, error: ErrorCls
) -> None:
    assert issubclass(error, Exception)
    write(tmp_path)
    leftovers = sorted(
        path.name for path in (tmp_path / relative).parent.iterdir() if path.name != "state.json"
    )
    assert leftovers == []
