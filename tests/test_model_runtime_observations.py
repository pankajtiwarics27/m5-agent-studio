"""LC-01: facts are immutable and unknown is never proof of release/authority."""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from local_runtime_lifecycle import (
    CapabilityEvidence,
    ModelInstanceObservation,
    ObservationStamp,
    RuntimeIdentity,
    RuntimeInventory,
)

RUNTIME = RuntimeIdentity("configured-runtime", "custom-engine")
STAMP = ObservationStamp(100.0, "observer-boot-a")
QUERY = dict(now=105.0, clock_id="observer-boot-a", max_age=10.0)


def inventory(**kwargs) -> RuntimeInventory:
    return RuntimeInventory(RUNTIME, reachability="reachable", completeness="complete", stamp=STAMP, **kwargs)


@pytest.mark.parametrize("backend", ["omlx", "ollama", "lmstudio", "future-engine"])
def test_wire_and_backend_never_imply_location_domain_or_egress(backend: str) -> None:
    identity = RuntimeIdentity("loopback-binding", backend, wire_protocol="cloud")
    assert identity.location == identity.egress == identity.ownership_mode == "unknown"
    assert identity.memory_domain_id is None
    assert identity.declared_domain_relation(identity) == "unknown"


def test_declared_aliases_share_domain_without_collapsing_runtime_ids() -> None:
    first = replace(RUNTIME, runtime_id="alias-one", memory_domain_id="host-memory")
    second = replace(first, runtime_id="alias-two")
    assert first != second
    assert first.declared_domain_relation(second) == "shared"
    assert first.declared_domain_relation(replace(second, memory_domain_id="other")) == "distinct"
    assert first.declared_domain_relation(RUNTIME) == "unknown"


@pytest.mark.parametrize("bad", ["", " ", " leading", "trailing ", "private\nbody", "a\x00b", "x" * 513, True])
def test_identity_errors_are_bounded_and_do_not_echo_values(bad: object) -> None:
    with pytest.raises(ValueError, match="invalid runtime_id") as error:
        RuntimeIdentity(bad, "ollama")
    assert str(error.value) == "invalid runtime_id"


@pytest.mark.parametrize("changes", [{"location": "localhost"}, {"egress": True}, {"ownership_mode": "exclusive"}])
def test_ambiguous_identity_values_are_not_coerced(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(RUNTIME, **changes)


def test_immutable_records_reject_mutable_collections() -> None:
    with pytest.raises(FrozenInstanceError):
        RUNTIME.backend = "changed"
    with pytest.raises(ValueError, match="instances"):
        inventory(instances=[])
    with pytest.raises(ValueError, match="capabilities"):
        inventory(capabilities=[])
    with pytest.raises(ValueError, match="evidence_refs"):
        CapabilityEvidence("count", evidence_refs=[])


@pytest.mark.parametrize("changes, expected", [
    ({}, "fresh"), ({"now": 110.0}, "fresh"), ({"now": 110.1}, "stale"),
    ({"now": 99.0}, "unknown"), ({"clock_id": "observer-boot-b"}, "unknown"),
    ({"clock_id": None}, "unknown"),
])
def test_freshness_is_explicit_clock_scoped(changes: dict, expected: str) -> None:
    assert STAMP.freshness(**{**QUERY, **changes}) == expected


def test_missing_stamp_fields_are_unknown() -> None:
    for stamp in (ObservationStamp(), ObservationStamp(100.0), ObservationStamp(clock_id="observer-boot-a")):
        assert stamp.freshness(**QUERY) == "unknown"


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "100", 10**400])
def test_invalid_times_cannot_become_fresh(bad: object) -> None:
    with pytest.raises(ValueError):
        ObservationStamp(bad, "clock")
    with pytest.raises(ValueError):
        STAMP.freshness(**{**QUERY, "now": bad})


@pytest.mark.parametrize("bad", [0, -1, True, float("nan")])
def test_invalid_freshness_windows_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        STAMP.freshness(**{**QUERY, "max_age": bad})


@pytest.mark.parametrize("basis", ["source", "offline", "installed"])
@pytest.mark.parametrize("state", ["supported", "unsupported"])
def test_only_exact_installed_scope_can_classify_recorded_capability(basis: str, state: str) -> None:
    evidence = CapabilityEvidence("exact-count", state, basis, runtime_id="configured-runtime",
                                  runtime_version="version-a", profile_fingerprint="profile-a", evidence_refs=("evidence-id",))
    result = evidence.state_for_installed_scope(runtime_id="configured-runtime", runtime_version="version-a", profile_fingerprint="profile-a")
    assert result == (state if basis == "installed" else "unknown")
    for version, profile in (("version-b", "profile-a"), ("version-a", "profile-b"), (None, "profile-a"), ("version-a", None)):
        assert evidence.state_for_installed_scope(runtime_id="configured-runtime", runtime_version=version, profile_fingerprint=profile) == "unknown"


def test_installed_evidence_cannot_transfer_between_runtime_bindings() -> None:
    cap = CapabilityEvidence("count", "supported", "installed", runtime_id="configured-runtime",
                             runtime_version="v", profile_fingerprint="p", evidence_refs=("ref",))
    assert cap.state_for_installed_scope(runtime_id="different-runtime", runtime_version="v", profile_fingerprint="p") == "unknown"
    assert cap.state_for_installed_scope(runtime_id=None, runtime_version="v", profile_fingerprint="p") == "unknown"
    with pytest.raises(ValueError, match="different runtime"):
        RuntimeInventory(RuntimeIdentity("other-runtime", "custom-engine"), capabilities=(cap,))
    with pytest.raises(ValueError, match="runtime identity"):
        replace(cap, runtime_id=None)


def test_capabilities_default_unknown_and_cannot_be_claimed_without_evidence() -> None:
    evidence = CapabilityEvidence("load")
    assert evidence.state == "unknown"
    assert evidence.state_for_installed_scope(runtime_id="configured-runtime", runtime_version="v", profile_fingerprint="p") == "unknown"
    with pytest.raises(ValueError, match="scoped evidence"):
        replace(evidence, state="supported")
    with pytest.raises(ValueError, match="scoped evidence"):
        replace(evidence, state="unsupported", basis="source", evidence_refs=("ref",))


def test_model_file_maximum_instance_context_and_memory_are_separate() -> None:
    model = ModelInstanceObservation("artifact", instance_id="loaded-instance", state="loaded",
                                     architectural_context_tokens=262144, effective_context_tokens=4096,
                                     artifact_size_bytes=12345)
    assert model.effective_context_tokens == 4096
    assert model.resident_memory_bytes is None
    assert model.memory_basis == "unknown"
    assert ModelInstanceObservation("artifact", architectural_context_tokens=262144).effective_context_tokens is None
    assert inventory(instances=(model,)).residency_at(**QUERY) == "occupied"


@pytest.mark.parametrize("field", ["architectural_context_tokens", "effective_context_tokens", "artifact_size_bytes", "resident_memory_bytes", "active_requests"])
@pytest.mark.parametrize("bad", [True, -1, "4096", 4096.0])
def test_malformed_counts_are_not_coerced(field: str, bad: object) -> None:
    with pytest.raises(ValueError):
        ModelInstanceObservation("artifact", **{field: bad})


def test_memory_bytes_require_provenance_but_are_not_peak_admission() -> None:
    with pytest.raises(ValueError, match="provenance"):
        ModelInstanceObservation("artifact", resident_memory_bytes=100)
    model = ModelInstanceObservation("artifact", resident_memory_bytes=100, memory_basis="runtime-reported")
    assert inventory(instances=(model,)).residency_at(**QUERY) == "occupied"


@pytest.mark.parametrize("state", ["loaded", "loading", "unloading"])
def test_any_potentially_allocating_instance_holds_domain(state: str) -> None:
    model = ModelInstanceObservation("artifact", state=state)
    assert inventory(instances=(model,)).residency_at(**QUERY) == "occupied"


def test_unknown_partial_unreachable_and_stale_are_never_empty() -> None:
    assert RuntimeInventory(RUNTIME).residency_at(**QUERY) == "unknown"
    base = inventory()
    assert base.residency_at(**QUERY) == "empty_observed"
    assert replace(base, completeness="partial").residency_at(**QUERY) == "unknown"
    assert replace(base, reachability="unreachable").residency_at(**QUERY) == "unknown"
    assert base.residency_at(**{**QUERY, "now": 111.0}) == "unknown"
    assert inventory(instances=(ModelInstanceObservation("artifact"),)).residency_at(**QUERY) == "unknown"
    occupied = replace(base, completeness="partial", instances=(ModelInstanceObservation("artifact", state="loading"),))
    assert occupied.residency_at(**QUERY) == "occupied"


def test_absent_with_active_work_is_not_empty() -> None:
    model = ModelInstanceObservation("artifact", state="absent", active_requests=1)
    assert inventory(instances=(model,)).residency_at(**QUERY) == "occupied"


def test_distinct_loaded_instances_of_same_artifact_are_not_deduplicated() -> None:
    first = ModelInstanceObservation("artifact", instance_id="one", state="loaded")
    second = replace(first, instance_id="two")
    assert len(inventory(instances=(first, second)).instances) == 2
    with pytest.raises(ValueError, match="duplicate instance"):
        inventory(instances=(first, replace(first, artifact_id="different-alias")))


def test_duplicate_capability_scope_is_rejected_not_last_writer_wins() -> None:
    cap = CapabilityEvidence("load")
    with pytest.raises(ValueError, match="duplicate capability"):
        inventory(capabilities=(cap, cap))
    assert len(inventory(capabilities=(cap, replace(cap, profile_fingerprint="other"))).capabilities) == 2


def test_contract_methods_have_no_io_clock_or_environment_calls() -> None:
    tree = ast.parse(Path("local_runtime_lifecycle.py").read_text())
    names = {"RuntimeIdentity", "ObservationStamp", "CapabilityEvidence", "ModelInstanceObservation", "RuntimeInventory"}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in names:
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                if isinstance(call.func, ast.Attribute):
                    assert call.func.attr not in {"getenv", "get", "time", "monotonic", "request", "load", "unload", "open", "read_text"}
