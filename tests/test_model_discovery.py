from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from local_provider_adapters import LlamaCppAdapter, OllamaAdapter, LocalRuntimeSnapshot
from model_discovery import (
    InstallPlan,
    InstallRequest,
    ModelCatalog,
    ModelDiscoveryError,
    RuntimeAvailability,
    discover_models,
    observe_install,
    plan_install,
)
from model_registry import ModelDescriptor, ModelLimits, ModelManifestError, ModelProvenance, ModelRegistry


def _descriptor(
    model_id: str,
    *,
    digest: str = "sha256:aa",
    runtime: str = "ollama",
    context_tokens: int = 4096,
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=digest,
        source=runtime,
        revision=digest,
        runtime=runtime,
        quantization="Q4_K_M",
        limits=ModelLimits(context_tokens=context_tokens),
        capabilities=("chat", "streaming"),
        provenance=ModelProvenance(runtime, model_id, "unverified"),
    )


class _FakeSource:
    """Minimal DiscoverySource for aggregation-level tests."""

    def __init__(
        self,
        runtime_name: str,
        snapshot: LocalRuntimeSnapshot | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.runtime_name = runtime_name
        self._snapshot = snapshot
        self._error = error

    async def discover(self) -> LocalRuntimeSnapshot:
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot


def _transport_for(payload: Mapping[str, Any]) -> Any:
    async def transport(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        return 200, payload

    return transport


def _ollama_payload(*names: str) -> dict[str, Any]:
    return {
        "models": [
            {
                "name": name,
                "digest": f"sha256:{hashlib.sha256(name.encode()).hexdigest()[:12]}",
                "size": 1000,
                "details": {"families": ["chat", "streaming"], "context_length": 4096},
            }
            for name in names
        ]
    }


def _llama_payload(*ids: str) -> dict[str, Any]:
    return {"data": [{"id": model_id, "digest": f"sha256:{model_id}", "context_length": 2048, "capabilities": ["chat"]} for model_id in ids]}


def test_catalog_unions_runtimes_deterministically() -> None:
    ollama = OllamaAdapter(_transport_for(_ollama_payload("zephyr:7b", "alpha:latest")))
    llama = LlamaCppAdapter(_transport_for(_llama_payload("mid.gguf")))
    catalog = asyncio.run(discover_models([llama, ollama]))

    assert [entry.runtime for entry in catalog.runtimes] == ["llama.cpp", "ollama"]
    assert all(entry.available for entry in catalog.runtimes)
    assert catalog.model_ids() == ("alpha:latest", "mid.gguf", "zephyr:7b")
    assert [item.model_id for item in catalog.descriptors] == ["alpha:latest", "mid.gguf", "zephyr:7b"]
    assert catalog.conflicts == ()
    assert catalog.runtimes[1].model_count == 2
    resolved = catalog.registry().resolve("mid.gguf")
    assert resolved.runtime == "llama.cpp"
    mapping = catalog.to_mapping()
    assert set(mapping) == {"descriptors", "runtimes", "conflicts", "ambiguous_model_ids"}


def test_unavailable_runtime_is_reported_without_sinking_the_catalog() -> None:
    async def down(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        raise OSError("private upstream body")

    catalog = asyncio.run(
        discover_models([OllamaAdapter(down), LlamaCppAdapter(_transport_for(_llama_payload("solo.gguf")))])
    )
    ollama_entry = next(entry for entry in catalog.runtimes if entry.runtime == "ollama")
    assert ollama_entry.available is False
    assert ollama_entry.model_count == 0
    assert ollama_entry.error == "local runtime unavailable"
    assert "private upstream body" not in json.dumps(catalog.to_mapping())
    assert catalog.model_ids() == ("solo.gguf",)


def test_source_contract_failures_produce_safe_entries_without_leaks() -> None:
    leaking = _FakeSource("broken", error=ValueError("SECRET response body contents"))
    mismatched = _FakeSource("liar", snapshot=LocalRuntimeSnapshot("someone-else", True))
    catalog = asyncio.run(discover_models([leaking, mismatched]))

    by_runtime = {entry.runtime: entry for entry in catalog.runtimes}
    assert by_runtime["broken"].available is False
    assert by_runtime["broken"].error == "discovery failed"
    assert by_runtime["liar"].error == "discovery source contract violation"
    assert catalog.descriptors == ()
    assert "SECRET" not in json.dumps(catalog.to_mapping())


def test_fanout_input_validation() -> None:
    with pytest.raises(ModelDiscoveryError, match="at least one discovery source"):
        asyncio.run(discover_models([]))
    with pytest.raises(TypeError, match="sequence"):
        asyncio.run(discover_models("ollama"))  # type: ignore[arg-type]
    with pytest.raises(ModelDiscoveryError, match="unique runtime names"):
        asyncio.run(discover_models([_FakeSource("ollama"), _FakeSource("ollama")]))


def test_cancellation_propagates_through_the_fanout() -> None:
    cancelled = _FakeSource("ollama", error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(discover_models([cancelled]))

    class _Hang:
        runtime_name = "hanging"

        async def discover(self) -> LocalRuntimeSnapshot:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        task = asyncio.create_task(discover_models([_Hang()]))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_conflicting_duplicate_identities_deduplicate_and_flag() -> None:
    first = _FakeSource(
        "alpha-runtime",
        snapshot=LocalRuntimeSnapshot("alpha-runtime", True, (_descriptor("shared:1", context_tokens=2048),)),
    )
    second = _FakeSource(
        "beta-runtime",
        snapshot=LocalRuntimeSnapshot("beta-runtime", True, (_descriptor("shared:1", context_tokens=8192),)),
    )
    catalog = asyncio.run(discover_models([second, first]))

    assert len(catalog.descriptors) == 1
    assert catalog.descriptors[0].limits.context_tokens == 2048  # first runtime in name order wins
    assert catalog.conflicts == ("shared:1",)
    # The dedup invariant keeps the AS-017 registry bridge always constructible.
    assert isinstance(catalog.registry(), ModelRegistry)


def test_multi_digest_identity_is_ambiguous_not_conflicting() -> None:
    one = _FakeSource("one", snapshot=LocalRuntimeSnapshot("one", True, (_descriptor("dup:1", digest="sha256:aa", runtime="one"),)))
    two = _FakeSource("two", snapshot=LocalRuntimeSnapshot("two", True, (_descriptor("dup:1", digest="sha256:bb", runtime="two"),)))
    catalog = asyncio.run(discover_models([one, two]))

    assert catalog.conflicts == ()
    assert catalog.ambiguous_model_ids() == ("dup:1",)
    assert len(catalog.find("dup:1")) == 2
    with pytest.raises(ModelManifestError, match="ambiguous"):
        catalog.registry().resolve("dup:1")
    assert catalog.find("dup:1", runtime="two")[0].digest == "sha256:bb"


def test_install_requests_are_validated_fail_closed() -> None:
    with pytest.raises(ModelDiscoveryError, match="runtime must be one of: ollama"):
        InstallRequest("omlx", "some-model")
    with pytest.raises(ModelDiscoveryError, match="runtime must be one of"):
        InstallRequest("llama.cpp", "some-model")

    for bad in (
        "",
        "   ",
        "model; rm -rf /",
        "-evil",
        "--insecure-registry",
        "http://evil.example/model",
        "user/../../etc/passwd",
        "repo@sha256:deadbeef",
        "model\tname",
        "model|tee",
        "a" * 257,
        "one/two/three/four/five/six/seven",
        "user/model:tag:extra",
        "user/.hidden",
    ):
        with pytest.raises(ModelDiscoveryError):
            InstallRequest("ollama", bad)
    with pytest.raises(TypeError):
        InstallRequest("ollama", 123)  # type: ignore[arg-type]


def test_plan_install_produces_inert_argv() -> None:
    bare = plan_install(InstallRequest("ollama", "llama3.2"))
    assert bare.runtime == "ollama"
    assert bare.model_reference == "llama3.2:latest"
    assert bare.argv == ("ollama", "pull", "llama3.2:latest")

    tagged = plan_install(InstallRequest("ollama", "hf.co/user/repo:Q4_K_M"))
    assert tagged.model_reference == "hf.co/user/repo:Q4_K_M"
    assert len(tagged.argv) == 3 and all(" " not in item for item in tagged.argv)
    assert tagged.to_mapping()["argv"] == ["ollama", "pull", "hf.co/user/repo:Q4_K_M"]

    with pytest.raises(TypeError, match="InstallRequest"):
        plan_install("ollama:llama3.2")  # type: ignore[arg-type]


def test_observe_install_classifies_before_after_catalogs() -> None:
    plan = plan_install(InstallRequest("ollama", "newmodel"))
    empty = ModelCatalog((), (RuntimeAvailability("ollama", True, 0),))
    landed = ModelCatalog((_descriptor("newmodel:latest"),), (RuntimeAvailability("ollama", True, 1),))

    installed = observe_install(empty, landed, plan)
    assert installed.status == "installed"
    assert installed.descriptor is not None
    assert installed.descriptor.model_id == "newmodel:latest"

    repeated = observe_install(landed, landed, plan)
    assert repeated.status == "already_present"

    failed = observe_install(empty, empty, plan)
    assert failed.status == "not_installed"
    assert failed.descriptor is None
    assert failed.to_mapping()["descriptor"] is None

    with pytest.raises(TypeError, match="ModelCatalog"):
        observe_install(None, landed, plan)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="InstallPlan"):
        observe_install(empty, landed, "ollama pull newmodel")  # type: ignore[arg-type]


def test_model_installed_by_runtime_appears_without_code_changes(tmp_path: Path) -> None:
    """End-to-end acceptance proof: a real install command (a fake runtime CLI,
    run through subprocess with no shell) changes only runtime data, and
    re-discovery — identical code — reports the new model."""
    state_file = tmp_path / "ollama-state.json"
    state_file.write_text(
        json.dumps({"models": [{"name": "existing:latest", "digest": "sha256:ex", "details": {"families": ["chat", "streaming"]}}]}),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ollama").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"state = pathlib.Path({str(state_file)!r})\n"
        "payload = json.loads(state.read_text())\n"
        "args = sys.argv[1:]\n"
        "if args[:1] != ['pull'] or len(args) != 2:\n"
        "    print('usage: ollama pull NAME[:tag]', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "name = args[1]\n"
        "if any(m['name'] == name for m in payload['models']):\n"
        "    print(name + ' already pulled')\n"
        "    raise SystemExit(0)\n"
        "payload['models'].append({\n"
        "    'name': name,\n"
        "    'digest': 'sha256-' + name.replace(':', '-'),\n"
        "    'size': 4096,\n"
        "    'details': {'families': ['chat', 'streaming', 'tools'], 'context_length': 8192, 'quantization_level': 'Q4_K_M'},\n"
        "})\n"
        "state.write_text(json.dumps(payload))\n"
        "print('pulling ' + name + ': success')\n",
        encoding="utf-8",
    )
    (bin_dir / "ollama").chmod(0o755)
    child_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    async def transport(_method: str, path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        assert path == "/api/tags"
        return 200, json.loads(state_file.read_text())

    adapter = OllamaAdapter(transport)
    before = asyncio.run(discover_models([adapter]))
    assert before.model_ids() == ("existing:latest",)

    plan = plan_install(InstallRequest("ollama", "fresh-model"))
    completed = subprocess.run(
        list(plan.argv),
        env=child_env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "fresh-model:latest" in completed.stdout

    after = asyncio.run(discover_models([adapter]))
    observation = observe_install(before, after, plan)
    assert observation.status == "installed"
    assert observation.descriptor is not None
    assert observation.descriptor.model_id == "fresh-model:latest"
    assert "tools" in observation.descriptor.capabilities
    assert observation.descriptor.limits.context_tokens == 8192
    assert observation.descriptor.quantization == "Q4_K_M"

    # Same code path again: the runtime reports no change, so a re-run is an
    # idempotent "already present" — no catalog or registry code was touched.
    subprocess.run(list(plan.argv), env=child_env, capture_output=True, text=True, check=True, timeout=30)
    rediscovered = asyncio.run(discover_models([adapter]))
    assert observe_install(after, rediscovered, plan).status == "already_present"
    assert rediscovered.registry().resolve("fresh-model:latest").supports_tools is True

    # A failed install never fabricates a model record.
    broken_plan = InstallPlan("ollama", "phantom:latest", ("ollama", "bogus", "phantom:latest"))
    failed = subprocess.run(list(broken_plan.argv), env=child_env, capture_output=True, text=True, timeout=30)
    assert failed.returncode == 2
    after_failure = asyncio.run(discover_models([adapter]))
    assert observe_install(rediscovered, after_failure, broken_plan).status == "not_installed"


def test_find_requires_a_model_id() -> None:
    catalog = ModelCatalog((_descriptor("a:1"),), ())
    with pytest.raises(ModelDiscoveryError, match="model_id must not be empty"):
        catalog.find("   ")
