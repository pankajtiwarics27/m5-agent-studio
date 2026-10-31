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

Add representative cases and thermal sampling to the hardware run:

```sh
python3 scripts/qualify_environment.py --hardware \
  --pressure-gb 16 --case-runs 3 \
  --coding-cases cases.json --model-command "your-probe-command"
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

Useful flags: `--dry-run` (print every command the suite would run and exit
without touching anything), `--runtime docker|podman` (default `auto`; an
explicitly requested runtime that is not installed blocks the run instead of
quietly qualifying a different backend), `--timeout` (per check, default 180 s),
`--quiet`, `--json`, `--markdown`.

Review a run before making it:

```sh
./scripts/qualify_environment.sh --all --dry-run --pressure-gb 16
```

### Prerequisites, and why they are `blocked` rather than `unqualified`

| Situation | Verdict | Why |
|---|---|---|
| No runtime CLI on `PATH`, or the daemon is down | `blocked` | Nothing was observed. |
| The image is not local | `blocked` (exit `2`) | The suite never pulls; pull it yourself and re-run. |
| The container cannot see the shared workspace | `blocked` | A macOS file-sharing or SELinux labelling problem is a setup gap, not an isolation leak. |
| A probe observed a leak | `unqualified` (exit `1`) | A guarantee actually failed. |

`unqualified` is reserved for *observed* violations. A misconfigured host must not
be reported as a security failure, and it must not be reported as a pass either —
`blocked` is the honest third answer.

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

### Which story task each check answers (M5-P3-01)

`M5-P3-01`'s implementation tasks, mapped to the checks that produce evidence for
them. Run `--runtime docker` **and** `--runtime podman` if you intend to claim
both backends: one run qualifies one runtime.

| Task | Checks |
|---|---|
| Exercise live container/native candidates on supported macOS | `p3.runtime.available`, `p3.image.local`, `p3.positive.execute` |
| Test path traversal, symlink, network bypass, credentials, descendants, resource limits | `p3.adversarial.escape_matrix` (traversal, symlink, network, raw socket), `p3.adversarial.credentials`, `p3.adversarial.descendants`, `p3.adversarial.memory_limit` |
| Run positive compatibility tests | `p3.positive.execute` |
| Bind evidence to OS/runtime/config fingerprint | `p3.environment.bind` (runtime version, image id and digest, hardening flags) plus the record's `fingerprint` (OS build, CPU, git revision) |

| Check | What it observes |
|---|---|
| `p3.runtime.available` | The runtime CLI answers `--version` and its daemon answers `info`. |
| `p3.image.local` | The configured image is already present (`image inspect`); nothing is pulled. Absent → `blocked`, with the exact `pull` command. |
| `p3.environment.bind` | Binds the record to **what** it measured: the runtime's version line, the image's content id and repo digest (a tag can be re-pulled to a different digest), and the product's hardening flags with their limit values, read from `app._sandbox_command`. Unreadable identity is `blocked`, not a failed guarantee — but a `qualified` record cannot be produced without it. |
| `p3.positive.execute` | Positive compatibility: the container lists `/workspace`, code runs, and its write lands on the host. If the mount is missing/empty or unwritable the check is `blocked` (setup), not failed. |
| `p3.adversarial.escape_matrix` | Inside the container: a host-only canary file via direct path, `/workspace/..` traversal, and `/proc/1/root`, read-only rootfs write attempts (`/` and `/etc/hostname`), a workspace symlink pointing outside, direct and host-loopback TCP egress, an inherited host environment sentinel, and a raw socket are all refused. |
| `p3.adversarial.credentials` | Inside the container `HOME` is `/tmp` and `~/.aws/credentials`, `~/.ssh/*`, `~/.netrc`, `~/.docker/config.json`, `~/.kube/config` and the gcloud ADC file are all absent. A host credential store visible in the sandbox fails the check. |
| `p3.adversarial.memory_limit` | A 3 GiB allocation dies under `--memory 512m`. |
| `p3.adversarial.descendants` | A background `sleep` descendant does not survive the container (`docker ps --filter name=`). |
| `p3.product.argv` | The **product's** `_sandbox_command` still carries `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pull=never`, the pids/memory/cpu limits, exactly one bind mount, and no `--privileged`. |
| `p3.product.argv_parity` | The argv the suite probes with is compared, **at run time on this machine**, against `app._sandbox_command`: same flag set, exactly one bind mount. If the product changes and the suite is not updated, this fails instead of quietly qualifying stale flags. |
| `p3.product.no_host_fallback` | With the runtime name pointed at a nonexistent binary the product denies; with the real configuration no marker file appears on the host. |
| `p3.product.isolation_smoke` | `scripts/isolation_smoke.py --live` exits 0. |

The suite's own container flags are asserted against the product's argv twice:
statically in CI
(`tests/test_qualify_environment.py::test_suite_argv_matches_the_product_argv`)
and dynamically in `p3.product.argv_parity` on the machine that runs the
qualification. The suite carries the product's full hardening set —
`--network=none`, `--read-only`, `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, `--pull=never`,
`--env HOME=/tmp TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1`, `--pids-limit`,
`--memory`, `--cpus`, `--ipc=private`, `--ulimit nofile`, a 256m `noexec` `/tmp`
tmpfs, one bind mount, `--entrypoint /bin/sh` and the same `exec python3 -c`
shell form — so it cannot quietly qualify a sandbox the product does not build.
A leak in the escape matrix fails the suite; there is no interpretation step that
can turn it into a pass.

## The hardware suite (M5-P4-01)

| Check | What it observes |
|---|---|
| `p4.machine.fingerprint` | Chip, OS build, core count, total RAM and swap, bound into the record with the repository revision. `blocked` off-target (the story AC is 24 GB Apple Silicon). |
| `p4.single_heavy_model` | `resource_admission` admits one request at 80 % of detected RAM, refuses the second, and admits again after release. |
| `p4.admission_wired` | Drives the real `app.stream_any_model` funnel: the first stream is admitted, a second is refused while the slot is held, and a stream succeeds again after release. A module-tested primitive is not a serving-path guarantee, so this is observed rather than assumed. |
| `p4.priors_not_verified` | Routing confidence with no measured profile is `unknown` with the note `no comparable benchmark profile`. |
| `p4.pressure` | With `--pressure-gb`: touches that much anonymous memory while sampling swap; records before/peak/after and the growth. |
| `p4.model_load` | With `--model-command`: runs the command N times, records each exit code, wall time and reported metrics, plus swap growth. |
| `p4.coding_cases` | With `--coding-cases` + `--model-command`: runs every representative case, per category, and records per-case completion, success rate, wall-time median and spread, plus an uncertainty summary. Optional check — a case that never completes is reported, not hidden. |
| `p4.thermal` | Sampled during every load phase. macOS reads thermal pressure from `pmset -g therm` (no root needed; `powermetrics` is never invoked because it needs root); Linux reads `/sys/class/thermal/thermal_zone*/temp`. Records stress min/max and whether throttling was observed; `blocked` when the platform offers no readable source. Optional check. |

### Which story task each check answers (M5-P4-01)

`M5-P4-01`'s implementation tasks, mapped to the checks that produce evidence for
them, so a gap is visible instead of assumed:

| Task | Checks |
|---|---|
| Build representative per-category coding cases | `p4.coding_cases` (case set supplied by the operator) |
| Record completed case evidence and uncertainty | `p4.coding_cases` (`success_rate`, `wall_s_median`, `wall_s_spread_ratio`, `uncertainty`) |
| Measure load/prefill/swap/thermal behavior | `p4.model_load` (load/prefill/tokens), `p4.pressure` (swap), `p4.thermal` (heat/throttling) |
| Implement one-heavy-model admission and optional sequential switching | `p4.single_heavy_model`, `p4.admission_wired` (switching is `M5-P4-02`) |

### Coding cases

A case file is either a bare list or an object with an optional `categories`
declaration (cases may not use an undeclared category):

```json
{
  "categories": ["bugfix", "refactor", "tests"],
  "cases": [
    {"id": "bugfix-1", "category": "bugfix", "prompt": "Fix the off-by-one in the paginator."},
    {"id": "refactor-1", "category": "refactor", "prompt": "Extract the retry loop into a helper."},
    {"id": "tests-1", "category": "tests", "prompt": "Add a regression test for the cache eviction bug."}
  ]
}
```

Each case runs `--model-command` once per `--case-runs`, with
`STUDIO_QUALIFY_CASE_ID`, `STUDIO_QUALIFY_CASE_CATEGORY` and
`STUDIO_QUALIFY_CASE_PROMPT` in the environment — so the probe decides how to
talk to the model, and the suite only measures. Duplicate ids, a missing id or
category, or an undeclared category abort the run with exit `2` and no record,
because a malformed case set is an input error rather than a verdict.

`--case-runs 1` (the default) is recorded as a limitation: with one run per case
the record cannot separate model variance from case difficulty, and
`uncertainty.single_run_cases` says how many cases are in that position.

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
`fingerprint` (including `git_revision`), the detected `sandbox` backend down to
the runtime version, image content id, repo digest and hardening configuration,
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
   is about the serving path, not only the primitive. Both halves now have code:
   the serving path refuses a second heavy local model
   ([`model-admission.md`](model-admission.md), ADR-0011), and only the measured
   24 GB evidence is still missing.
4. If a check fails, the honest outcome is `unqualified` plus a fix, not a
   re-run until it passes.

## Tests

`tests/test_qualify_environment.py` (94 tests) pins the properties above without a
container runtime: verdict semantics, required-versus-optional checks, argv
parity with the product, the blocked path when no runtime, image or usable
workspace mount is missing, the failure path when a leak is observed,
host-execution detection, the evidence binding (version, image id/digest,
hardening flags, and the refusal to invent any of them), thermal sampling and
coding-case parsing, the record and Markdown shape, the exit codes, the
model-probe contract, `--dry-run` executing nothing, and an end-to-end run in
this environment that must exit `2` and print both stories as `blocked`.
