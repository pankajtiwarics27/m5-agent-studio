# Sandbox and filesystem boundary hardening

AS-028 defines the isolated boundary primitives in
[`sandbox_boundary.py`](../sandbox_boundary.py). `confined_path()` rejects
traversal, sibling-prefix escapes, and symlink targets outside the workspace.
`validate_workspace()` rejects symlink/non-directory roots, home/filesystem-root
mounts, known secret-bearing filenames, and over-budget trees.

`sandbox_command()` constructs Docker/Podman arguments with no network, a
read-only container root, dropped capabilities, no-new-privileges, CPU/memory/
PID limits, and only the validated workspace mounted read-write. The module
does not execute the command and does not claim kernel-level isolation.

## Evidence

`tests/test_sandbox_boundary.py` covers traversal and symlink escapes, unsafe
roots and secret files, entry budgets, both supported runtimes, and required
container isolation flags.
