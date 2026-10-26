# macOS hardware and memory profiler

AS-016 adds `MacHardwareProfiler` in
[`macos_hardware_profiler.py`](../macos_hardware_profiler.py). It adapts
standard macOS probes into the generic AS-015 `HardwareProfile`; it does not
select models or hard-code a chip, vendor, or memory tier.

## Probes and failure behavior

- `sysctl -n hw.memsize` supplies physical memory when available.
- `memory_pressure -Q` supplies free-memory percentage or pressure text.
- `vm_stat` supplies a page-based available-memory fallback.
- `system_profiler SPDisplaysDataType -json` supplies generic display
  accelerator records.

Each probe has a short timeout and is isolated behind an injectable command
runner. A missing command, malformed response, or non-zero exit produces an
explicit unknown value or empty accelerator list; it does not invent a
measurement or prevent the rest of the profile from being returned. Non-macOS
hosts return a portable profile without attempting macOS commands.

## Evidence

`tests/test_macos_hardware_profiler.py` uses deterministic fake command output
to verify memory pressure, available-memory fallback, accelerator parsing,
probe failure handling, and non-macOS behavior without requiring macOS,
hardware, or external services.
