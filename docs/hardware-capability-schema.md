# Hardware capability schema

AS-015 defines the normalized `HardwareProfile` contract in
[`hardware_capabilities.py`](../hardware_capabilities.py). It is an immutable,
JSON-compatible data boundary for later profilers, model registries, and
resource admission logic.

## Schema fields

- `operating_system` and `architecture` identify the host platform without
  restricting supported values.
- `accelerators` contains generic kind/vendor/name records with optional memory
  and capability labels. No chip, vendor, or accelerator name is required by
  the schema.
- `physical_memory_bytes`, `available_memory_bytes`, and `memory_pressure`
  represent normalized memory state; unknown values are allowed explicitly.
- `installed_runtimes` records runtime names, versions, and capabilities.
- `permissions` records observed permission labels without granting any
  permission; authorization remains an application policy boundary.
- `schema_version` is currently `1` and rejects unsupported versions.

The profile and nested records are frozen dataclasses. Construction validates
non-negative byte counts, memory bounds, supported pressure values, non-empty
identifiers, and deterministic collection normalization. Platform-specific
discovery is deliberately outside this story and will be supplied by AS-016.

## Evidence

`tests/test_hardware_capabilities.py` covers generic architectures and
accelerators, deterministic round trips, immutability, and invalid or unsafe
values. The tests use no host probe, provider, model, network, or hardware
dependency.
