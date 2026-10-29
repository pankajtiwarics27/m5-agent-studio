from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import release_manager as rm

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"


def _artifact(directory: Path, payload: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / rm.ARTIFACT_NAME
    target.write_text(payload, encoding="utf-8")
    return target


def _manifest_for(artifact: Path, version: str) -> rm.ReleaseManifest:
    return rm.build_manifest(artifact, version, created_at="2026-10-29T00:00:00Z")


@pytest.fixture
def layout(tmp_path: Path) -> rm.InstallLayout:
    return rm.InstallLayout(tmp_path / "install")


@pytest.fixture
def releases(tmp_path: Path):
    """Return a factory producing (artifact, manifest) pairs with distinct content."""

    def make(version: str, payload: str | None = None) -> tuple[Path, rm.ReleaseManifest]:
        directory = tmp_path / "dist" / version
        artifact = _artifact(directory, payload if payload is not None else f"# studio {version}\n")
        return artifact, _manifest_for(artifact, version)

    return make


# ---------------------------------------------------------------------------
# Core install / update / rollback behaviour
# ---------------------------------------------------------------------------


def test_install_activates_a_verified_release(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")

    result = rm.install(layout, artifact, manifest)

    assert result == {
        "action": "install",
        "version": "9.0.1",
        "sha256": manifest.sha256,
        "path": str(layout.version_dir("9.0.1")),
        "activated": True,
        "previous": None,
    }
    assert layout.current_artifact().read_text(encoding="utf-8") == "# studio 9.0.1\n"
    stored = rm.ReleaseManifest.load(layout.manifest_path("9.0.1"))
    assert stored.sha256 == manifest.sha256
    assert rm.verify(layout) == []


def test_install_without_activation_leaves_no_active_release(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")

    result = rm.install(layout, artifact, manifest, activate=False)

    assert result["activated"] is False
    assert not layout.current_pointer.exists()
    assert rm.installed_versions(layout) == ["9.0.1"]


def test_install_rejects_a_digest_mismatch_before_touching_the_pointer(
    layout: rm.InstallLayout, releases
) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)
    other_artifact, _ = releases("9.0.2", "# different bytes\n")
    stale_manifest = rm.ReleaseManifest(
        version="9.0.2",
        sha256=manifest.sha256,
        size_bytes=manifest.size_bytes,
        created_at="2026-10-29T00:00:00Z",
    )

    with pytest.raises(rm.ReleaseError, match="size mismatch|digest mismatch"):
        rm.install(layout, other_artifact, stale_manifest)

    assert rm.read_pointer(layout.current_pointer) == rm.Pointer("9.0.1", manifest.sha256)
    assert rm.installed_versions(layout) == ["9.0.1"]


def test_install_rejects_a_mislabelled_artifact(layout: rm.InstallLayout, tmp_path: Path) -> None:
    directory = tmp_path / "dist" / "9.0.1"
    directory.mkdir(parents=True)
    artifact = directory / "studio-copy.py"
    artifact.write_text("# studio\n", encoding="utf-8")

    with pytest.raises(rm.ReleaseError, match="must be named studio.py"):
        rm.install(layout, artifact, _manifest_for(artifact, "9.0.1"))


def test_update_records_the_previous_release_for_rollback(layout: rm.InstallLayout, releases) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)

    result = rm.update(layout, second, second_manifest)

    assert result["action"] == "update"
    assert result["previous"] == "9.0.1"
    assert rm.read_pointer(layout.current_pointer) == rm.Pointer("9.0.2", second_manifest.sha256)
    assert rm.read_pointer(layout.previous_pointer) == rm.Pointer("9.0.1", first_manifest.sha256)
    assert rm.verify(layout) == []


def test_update_refuses_an_unrequested_downgrade(layout: rm.InstallLayout, releases) -> None:
    second, second_manifest = releases("9.0.2")
    older, older_manifest = releases("9.0.1")
    rm.install(layout, second, second_manifest)

    with pytest.raises(rm.ReleaseError, match="Refusing to downgrade"):
        rm.update(layout, older, older_manifest)

    assert rm.read_pointer(layout.current_pointer).version == "9.0.2"
    assert not layout.version_dir("9.0.1").exists()


def test_update_can_downgrade_when_explicitly_allowed(layout: rm.InstallLayout, releases) -> None:
    second, second_manifest = releases("9.0.2")
    older, older_manifest = releases("9.0.1")
    rm.install(layout, second, second_manifest)

    result = rm.update(layout, older, older_manifest, allow_downgrade=True)

    assert result["version"] == "9.0.1"
    assert result["previous"] == "9.0.2"


def test_rollback_swaps_the_pointers_and_is_repeatable(layout: rm.InstallLayout, releases) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)
    rm.update(layout, second, second_manifest)

    rolled_back = rm.rollback(layout)

    assert rolled_back["action"] == "rollback"
    assert rolled_back["version"] == "9.0.1"
    assert rolled_back["from"] == "9.0.2"
    assert layout.current_artifact().read_text(encoding="utf-8") == "# studio 9.0.1\n"
    assert rm.read_pointer(layout.previous_pointer) == rm.Pointer("9.0.2", second_manifest.sha256)

    forward = rm.rollback(layout)
    assert forward["version"] == "9.0.2"
    assert rm.verify(layout) == []


def test_rollback_without_a_previous_release_fails_clearly(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)

    with pytest.raises(rm.ReleaseError, match="No previous release"):
        rm.rollback(layout)


def test_rollback_without_any_install_fails_clearly(layout: rm.InstallLayout) -> None:
    with pytest.raises(rm.ReleaseError, match="No active release"):
        rm.rollback(layout)


def test_rollback_refuses_a_corrupted_target_and_keeps_the_active_release(
    layout: rm.InstallLayout, releases
) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)
    rm.update(layout, second, second_manifest)
    layout.artifact_path("9.0.1").write_text("# studio 9.0.1\n# tampered\n", encoding="utf-8")

    with pytest.raises(rm.ReleaseError, match="size mismatch|digest mismatch"):
        rm.rollback(layout)

    assert rm.read_pointer(layout.current_pointer).version == "9.0.2"
    assert layout.current_artifact().read_text(encoding="utf-8") == "# studio 9.0.2\n"


def test_rollback_refuses_when_the_recorded_digest_contradicts_the_manifest(
    layout: rm.InstallLayout, releases
) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)
    rm.update(layout, second, second_manifest)
    rm._write_json_atomic(layout.previous_pointer, rm.Pointer("9.0.1", "0" * 64).to_dict())

    with pytest.raises(rm.ReleaseError, match="does not match its manifest"):
        rm.rollback(layout)

    assert rm.read_pointer(layout.current_pointer).version == "9.0.2"


def test_reinstalling_the_same_version_keeps_the_rollback_target(layout: rm.InstallLayout, releases) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)
    rm.update(layout, second, second_manifest)

    rebuilt, rebuilt_manifest = releases("9.0.2", "# studio 9.0.2 rebuilt\n")
    rm.install(layout, rebuilt, rebuilt_manifest)

    assert rm.read_pointer(layout.previous_pointer).version == "9.0.1"
    assert layout.current_artifact().read_text(encoding="utf-8") == "# studio 9.0.2 rebuilt\n"
    assert rm.verify(layout) == []


# ---------------------------------------------------------------------------
# Integrity, layout hygiene, and validation
# ---------------------------------------------------------------------------


def test_verify_reports_tampering_of_the_active_artifact(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)
    layout.current_artifact().write_text("# studio 9.0.1\n# injected\n", encoding="utf-8")

    problems = rm.verify(layout)

    assert problems and "current 9.0.1" in problems[0]
    assert rm.status(layout)["healthy"] is False


def test_verify_reports_a_missing_artifact(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)
    layout.artifact_path("9.0.1").unlink()

    problems = rm.verify(layout)

    assert problems and "Artifact missing" in problems[0]


def test_verify_reports_a_corrupt_pointer(layout: rm.InstallLayout, releases) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)
    layout.current_pointer.write_text("{not json", encoding="utf-8")

    problems = rm.verify(layout)

    assert problems and "current pointer" in problems[0]
    assert rm.status(layout)["current"] is None


def test_verify_reports_an_uninstalled_root(layout: rm.InstallLayout) -> None:
    assert rm.verify(layout) == ["current pointer: no release is active"]
    assert rm.status(layout)["versions"] == []


def test_install_leaves_no_staging_directories_behind(layout: rm.InstallLayout, releases) -> None:
    first, first_manifest = releases("9.0.1")
    second, second_manifest = releases("9.0.2")
    rm.install(layout, first, first_manifest)
    rm.update(layout, second, second_manifest)

    assert list(rm.staged_leftovers(layout)) == []


def test_failed_install_leaves_no_staging_directories_behind(
    layout: rm.InstallLayout, releases, tmp_path: Path
) -> None:
    artifact, manifest = releases("9.0.1")
    rm.install(layout, artifact, manifest)
    other, _ = releases("9.0.2", "# other\n")
    bad_manifest = rm.ReleaseManifest(
        version="9.0.2", sha256="a" * 64, size_bytes=other.stat().st_size, created_at="2026-10-29T00:00:00Z"
    )

    with pytest.raises(rm.ReleaseError):
        rm.install(layout, other, bad_manifest)

    assert list(rm.staged_leftovers(layout)) == []
    assert rm.installed_versions(layout) == ["9.0.1"]


def test_installed_versions_are_sorted_numerically(layout: rm.InstallLayout, releases) -> None:
    for version in ("9.0.2", "9.0.10", "9.0.1"):
        artifact, manifest = releases(version)
        rm.install(layout, artifact, manifest, activate=False)

    assert rm.installed_versions(layout) == ["9.0.1", "9.0.2", "9.0.10"]


def test_prune_keeps_the_active_and_rollback_targets(layout: rm.InstallLayout, releases) -> None:
    for version in ("9.0.1", "9.0.2", "9.0.3"):
        artifact, manifest = releases(version)
        if version == "9.0.1":
            rm.install(layout, artifact, manifest)
        else:
            rm.update(layout, artifact, manifest)

    result = rm.prune(layout, keep=2)

    assert result["removed"] == ["9.0.1"]
    assert rm.installed_versions(layout) == ["9.0.2", "9.0.3"]
    assert rm.verify(layout) == []
    assert layout.current_artifact().read_text(encoding="utf-8") == "# studio 9.0.3\n"


def test_prune_rejects_a_nonpositive_keep(layout: rm.InstallLayout) -> None:
    with pytest.raises(rm.ReleaseError, match="keep must be at least 1"):
        rm.prune(layout, keep=0)


@pytest.mark.parametrize("version", ["1.2", "1.2.3.4", "../9.0.1", "9.0.1/../..", "9.0.1 ", "", "v9.0.1", "9.0.1\n"])
def test_version_validation_rejects_traversal_and_noise(version: str) -> None:
    with pytest.raises(rm.ReleaseError, match="MAJOR.MINOR.PATCH"):
        rm.validate_version(version)


def test_version_validation_accepts_semantic_versions() -> None:
    assert rm.validate_version("9.0.1") == "9.0.1"
    assert rm.version_tuple("10.20.30") == (10, 20, 30)


def test_manifest_rejects_an_invalid_digest_or_negative_size() -> None:
    with pytest.raises(rm.ReleaseError, match="sha256"):
        rm.ReleaseManifest(version="9.0.1", sha256="NOTHEX", size_bytes=1, created_at="2026-10-29T00:00:00Z")
    with pytest.raises(rm.ReleaseError, match="size_bytes"):
        rm.ReleaseManifest(version="9.0.1", sha256="a" * 64, size_bytes=-1, created_at="2026-10-29T00:00:00Z")


def test_manifest_round_trip_preserves_provenance(tmp_path: Path) -> None:
    path = tmp_path / rm.MANIFEST_NAME
    manifest = rm.ReleaseManifest(
        version="9.0.1",
        sha256="b" * 64,
        size_bytes=42,
        created_at="2026-10-29T00:00:00Z",
        python_requires=">=3.12",
        source_commit="c" * 40,
    )
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    loaded = rm.ReleaseManifest.load(path)

    assert loaded == manifest


def test_manifest_load_reports_missing_fields_and_bad_json(tmp_path: Path) -> None:
    path = tmp_path / rm.MANIFEST_NAME
    path.write_text(json.dumps({"version": "9.0.1"}), encoding="utf-8")
    with pytest.raises(rm.ReleaseError, match="missing required field"):
        rm.ReleaseManifest.load(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(rm.ReleaseError, match="not valid JSON"):
        rm.ReleaseManifest.load(path)
    with pytest.raises(rm.ReleaseError, match="Cannot read manifest"):
        rm.ReleaseManifest.load(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# CLI smoke tests: package -> install -> update -> rollback -> verify
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False, cwd=ROOT
    )


def test_cli_end_to_end_install_update_rollback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    root = tmp_path / "root"
    first = tmp_path / "src-9.0.1" / rm.ARTIFACT_NAME
    first.parent.mkdir(parents=True)
    first.write_text("# studio 9.0.1\n", encoding="utf-8")

    packaged = _run("package", "--version", "9.0.1", "--artifact", str(first), "--output", str(dist))
    assert packaged.returncode == 0, packaged.stderr
    manifest = json.loads(packaged.stdout)
    assert manifest["version"] == "9.0.1" and len(manifest["sha256"]) == 64

    installed = _run("install", "--version", "9.0.1", "--root", str(root), "--from-dir", str(dist))
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["activated"] is True

    second = tmp_path / "src-9.0.2" / rm.ARTIFACT_NAME
    second.parent.mkdir(parents=True)
    second.write_text("# studio 9.0.2\n", encoding="utf-8")
    assert _run("package", "--version", "9.0.2", "--artifact", str(second), "--output", str(dist)).returncode == 0

    updated = _run("update", "--version", "9.0.2", "--root", str(root), "--from-dir", str(dist))
    assert updated.returncode == 0, updated.stderr
    assert json.loads(updated.stdout)["previous"] == "9.0.1"

    status = _run("status", "--root", str(root))
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["current"] == "9.0.2"

    rolled = _run("rollback", "--root", str(root))
    assert rolled.returncode == 0, rolled.stderr
    assert json.loads(rolled.stdout)["version"] == "9.0.1"
    assert (root / "versions" / "9.0.1" / rm.ARTIFACT_NAME).read_text(encoding="utf-8") == "# studio 9.0.1\n"

    verified = _run("verify", "--root", str(root))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["healthy"] is True


def test_cli_reports_failures_with_a_nonzero_exit(tmp_path: Path) -> None:
    root = tmp_path / "root"

    rollback = _run("rollback", "--root", str(root))
    assert rollback.returncode == 1
    assert "No active release" in rollback.stderr

    status = _run("status", "--root", str(root))
    assert status.returncode == 1
    assert json.loads(status.stdout)["healthy"] is False

    install = _run("install", "--version", "9.0.1", "--root", str(root), "--from-dir", str(tmp_path / "empty"))
    assert install.returncode == 1
    assert "release:" in install.stderr


def test_cli_package_verifies_the_artifact_it_writes(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    source = tmp_path / "src" / rm.ARTIFACT_NAME
    source.parent.mkdir(parents=True)
    source.write_text("# studio\n", encoding="utf-8")

    assert _run("package", "--version", "9.0.1", "--artifact", str(source), "--output", str(dist)).returncode == 0

    packaged = dist / "9.0.1" / rm.ARTIFACT_NAME
    manifest = rm.ReleaseManifest.load(dist / "9.0.1" / rm.MANIFEST_NAME)
    assert packaged.read_bytes() == source.read_bytes()
    assert rm.sha256_file(packaged) == manifest.sha256
    assert manifest.size_bytes == packaged.stat().st_size


# ---------------------------------------------------------------------------
# Standalone artifact packaging: the build must inline every local module
# ---------------------------------------------------------------------------


def _local_module_imports(source: str, local_modules: set[str]) -> set[str]:
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in local_modules:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in local_modules and node.level == 0:
            found.add(node.module or "")
    return found


def test_every_local_module_import_is_inlined_for_the_standalone_build() -> None:
    """Guard the artifact: a sibling import the builder forgets only fails off-checkout.

    `onboarding_preflight` was added to app.py without being inlined, and the
    standalone smoke test still passed because it ran from the repo root where the
    sibling was importable. This test compares app.py's local imports with the
    builder's inlining list so the same gap cannot reappear.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_studio import INLINED_MODULES  # noqa: PLC0415

    local_modules = {path.stem for path in ROOT.glob("*.py")} - {"app", "studio", "setup"}
    app_imports = _local_module_imports((ROOT / "app.py").read_text(encoding="utf-8"), local_modules)
    # history_privacy and workflow_orchestration are inlined with dedicated blocks.
    specially_inlined = {"history_privacy", "workflow_orchestration"}

    missing = app_imports - set(INLINED_MODULES) - specially_inlined

    assert missing == set(), f"app.py imports local modules the standalone build does not inline: {sorted(missing)}"


def test_generated_standalone_has_no_local_module_imports() -> None:
    standalone = ROOT / rm.ARTIFACT_NAME
    if not standalone.is_file():
        pytest.skip("studio.py has not been generated in this checkout")

    local_modules = {path.stem for path in ROOT.glob("*.py")} - {"app", "studio", "setup"}
    imports = _local_module_imports(standalone.read_text(encoding="utf-8"), local_modules)

    assert imports == set(), f"standalone artifact still imports local modules: {sorted(imports)}"
