# Environment qualification suite

`M5-P3-01` (sandbox backend qualification) and `M5-P4-01` (measured model
qualification and RAM admission) are the two M5 2.0 stories whose acceptance
criteria cannot be satisfied from CI. One needs a live container runtime; the
other needs a 24 GB Apple Silicon Mac under real model load. In the Linux
evidence environment that produced the rest of this program both are **blocked**,
and they stay blocked until somebody runs the suite on the machine that is
supposed to hold the guarantee.

`scripts/qualify_environment.py` is that run. It is a single script covering both
stories, and `scripts/qualify_environment.sh` is a thin wrapper that names the
outputs. Neither one is executed by the server, and neither one can mark a story
complete — they produce evidence, and a human (or the agent, reading the record)
decides.

## What it is not

- It never invents a result. A check that cannot run is recorded `blocked` or
  `skip` **with the reason**, and a suite with any unexecuted required check is
  `blocked`, never `qualified`.
- It never pulls an image, installs a package, or reaches the network.
- It never touches your project files. The sandbox probes run in a temporary
  workspace under the system temp directory; the only writes are the artifact
  paths you name.
- It never allocates memory unless you pass `--pressure-gb`, and it never runs a
  model unless you pass `--model-command`.

## Running it

On the Docker/Podman host:

```sh
./scripts/qualify_environment.sh                       # both suites
./scripts/qualify_environment.sh --hardware            # M5-P4-01 only
./scripts/qualify_environment.sh --sandbox             # M5-P3-01 only
```

Or call the script directly:

```sh
python3 scripts/qualify_environment.py --all \
  --json dist/qualification.json --markdown dist/qualification.md
```

The sandbox suite needs a local image and refuses to fetch one:

```sh
docker pull python:3.12-slim      # or any image with python3 on PATH
python3 scripts/qualify_environment.py --sandbox --image python:3.12-slim
```

The hardware suite takes the two optional measurements:

```sh
python3 scripts/qualify_environment.py --hardware \
  --pressure-gb 12 \
  --model-command "your-probe-command" --model-runs 3 \
  --profile-out dist/model-profile.jsonl
```

Useful flags: `--runtime docker|podman` (default `auto`; an explicitly requested
runtime that is not installed blocks the run instead of quietly qualifying a
different backend), `--timeout` (per check, default 180 s), `--quiet`, `--json`,
`--markdown`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Every required check in every requested suite ran and passed. |
| `1` | A required check failed: the guarantee does not hold as configured. |
| `2` | Required checks could not run (no runtime and/or not the target hardware). |

`2` is the normal, honest answer on a machine that is not the target
environment. On the Linux development container used for this repository it is
the expected result.

### Server startup

Qualification is an operator action, not a boot step: allocating memory and
starting containers on every server start would be hostile to a serving machine,
and a startup-time failure would look like an application fault. The script is
safe to call from an entrypoint if you really want it — it is idempotent, writes
only to `--json`/`--markdown`, and returns a meaningful exit code:

```sh
# Docker Compose
command: sh -c "python3 scripts/qualify_environment.py --all --json /evidence/q.json; exec python3 app.py"

# systemd
ExecStartPre=/opt/m5-studio/.venv/bin/python scripts/qualify_environment.py --sandbox --json /var/lib/m5/evidence/q.json
```

Note that `ExecStartPre` refusal (exit `1`/`2`) stops the service. Use `-` as the
`ExecStartPre` prefix if you want the record without the gate.

## The sandbox suite (M5-P3-01)

| Check | What it observes |
|---|---|
| `p3.runtime.available` | The runtime CLI answers `--version` and its daemon answers `info`. |
| `p3.image.local` | The configured image is already present (`image inspect`); nothing is pulled. |
| `p3.positive.execute` | Positive compatibility: code runs and its write lands in the mounted workspace. |
| `p3.adversarial.escape_matrix` | Inside the container: a host-only canary file via direct path, `/workspace/..` traversal, and `/proc/1/root`, read-only rootfs write attempts (`/` and `/etc/hostname`), a workspace symlink pointing outside, direct and host-loopback TCP egress, an inherited host environment sentinel, and a raw socket are all refused. |
| `p3.adversarial.memory_limit` | A 3 GiB allocation dies under `--memory 512m`. |
| `p3.adversarial.descendants` | A background `sleep` descendant does not survive the container (`docker ps --filter name=`). |
| `p3.product.argv` | The **product's** `_sandbox_command` still carries `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pull=never`, the pids/memory/cpu limits, exactly one bind mount, and no `--privileged`. |
| `p3.product.no_host_fallback` | With the runtime name pointed at a nonexistent binary the product denies; with the real configuration no marker file appears on the host. |
| `p3.product.isolation_smoke` | `scripts/isolation_smoke.py --live` exits 0. |

The suite's own container flags are asserted against the product's argv by
`tests/test_qualify_environment.py::test_container_argv_matches_the_product_hardening_flags`,
so the suite cannot quietly qualify a sandbox the product does not build. A leak
in the escape matrix fails the suite; there is no interpretation step that can
turn it into a pass.

## The hardware suite (M5-P4-01)

| Check | What it observes |
|---|---|
| `p4.machine.fingerprint` | Chip, OS build, core count, total RAM and swap, bound into the record with the repository revision. `blocked` off-target (the story AC is 24 GB Apple Silicon). |
| `p4.single_heavy_model` | `resource_admission` admits one request at 80 % of detected RAM, refuses the second, and admits again after release. |
| `p4.admission_wired` | Informational: whether `app.py` actually consults `resource_admission`. Reported, never assumed — a module-tested primitive is not a serving-path guarantee. |
| `p4.priors_not_verified` | Routing confidence with no measured profile is `unknown` with the note `no comparable benchmark profile`. |
| `p4.pressure` | With `--pressure-gb`: touches that much anonymous memory while sampling swap; records before/peak/after and the growth. |
| `p4.model_load` | With `--model-command`: runs the command N times, records each exit code, wall time and reported metrics, plus swap growth. |

### The model probe contract

`--model-command` is a shell command that performs one measured model call. If it
prints a single JSON line, these fields are used (all optional except the
identity ones); anything missing stays `None` rather than becoming a guess:

```json
{"model_id": "…", "model_digest": "sha256:…", "runtime": "omlx", "context_length": 8192,
 "temperature": 0.0, "first_token_ms": 412, "prompt_tokens": 512,
 "output_tokens": 128, "peak_memory_bytes": 16106127360}
```

With `--profile-out` the record is written as a `model_calibration` profile that
loads through `model_calibration.load_profile`. An impossible measurement (a
first token after the end of the run) is recorded as a failed sample instead of
contributing a fabricated throughput.

## Reading the record

The JSON record carries `schema_version`, `generated_at_utc`, `invocation`,
`fingerprint` (including `git_revision`), the detected `sandbox` runtime/image,
every `check` with its status, detail, duration and evidence, per-story
`verdicts`, and `claims` that mirror those verdicts verbatim. The Markdown file
is the same content for a human reader.

Verdicts mean exactly this:

- `qualified` — every required check ran and passed on this machine.
- `unqualified` — a required check failed. The guarantee does not hold.
- `blocked` — a required check could not run. Nothing is claimed.

## Landing the evidence

1. Run the suite on the target machine and keep both artifacts.
2. Attach or commit the record (they are small JSON/Markdown files; keep them out
   of the source tree unless you want them archived — `dist/` is ignored).
3. Update `product/M5_2_0_Autonomous_Roadmap.json`: `M5-P3-01` can move out of
   `blocked` only when the sandbox suite is `qualified`, and `M5-P4-01` only when
   the hardware suite is `qualified` **and** `p4.admission_wired` is true — the
   acceptance criterion "second simultaneous heavy load rejected under policy"
   is about the serving path, not only the primitive.
4. If a check fails, the honest outcome is `unqualified` plus a fix, not a
   re-run until it passes.

## Tests

`tests/test_qualify_environment.py` (46 tests) pins the properties above without a
container runtime: verdict semantics, required-versus-optional checks, argv
parity with the product, the blocked path when no runtime exists, the failure
path when a leak is observed, host-execution detection, the record and Markdown
shape, the exit codes, the model-probe contract, and an end-to-end run in this
environment that must exit `2` and print both stories as `blocked`.
