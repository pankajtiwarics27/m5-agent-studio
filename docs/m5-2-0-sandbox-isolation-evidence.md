# M5 2.0 sandbox isolation evidence (`M5-P3-01`)

Operator-run record of the sandbox backend qualification suite
(`scripts/qualify_environment.py --sandbox`, wrapped by
`scripts/qualify_environment.sh`) on the target machine. This is the run that
qualified container isolation for `M5-P3-01`; the earlier, failed record stays
where it was written and is quoted, not erased, in
[§4](#4-the-earlier-failing-run-and-why-it-is-not-erased).

Nothing below is inferred. Every number is reproduced from the run's console
output, pasted by the operator on 2026-10-31. The suite also wrote
`dist/qualification/qualification-20261031T091931Z.json` and `.md`; `dist/` is
git-ignored, so those files are not committed and this page is the record that
travels with the repository.

## 1. Record identity

| Field | Value |
|---|---|
| Date | 2026-10-31 |
| Machine | `Darwin arm64` (Apple M5), 24.0 GiB RAM, 10 CPUs |
| Runtime | `docker` at `/usr/local/bin/docker` — Docker version 29.5.3, build `d1c06ef` |
| Image | `python:3.12-slim`, content id `sha256:aa943ce41142`, digest `python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159` |
| Bound config | 18 flags — `--pull=never`, `--network=none`, `--pids-limit=128`, `--memory=2g`, `--cpus=2`, `--ipc=private`, plus the rest of the hardening set; 1 bind mount; `privileged=False` |
| Command | `./scripts/qualify_environment.sh` |
| Verdict | `M5-P3-01: qualified` — 12 of 12 checks `pass` |

The console output of this invocation does not print the repository revision.
The suite writes `git_revision` into its JSON record when run from a git
checkout (`docs/m5-2-0-hardware-qualification-evidence.md` shows that field for
the earlier run), and the environment binding above is what the suite compares
before reusing a record: runtime version, image content id and digest, and the
product's hardening flags.

## 2. Checks, verbatim

| Check | Result |
|---|---|
| `p3.runtime.available` | pass — container runtime CLI and daemon reachable (Docker 29.5.3, build `d1c06ef`, info exit 0) |
| `p3.image.local` | pass — image `python:3.12-slim` present, id `sha256:aa943ce41142` |
| `p3.environment.bind` | pass — evidence bound to runtime, image content id, digest and 18 product config flags; 1 bind mount; `privileged=False` |
| `p3.positive.execute` | pass — exit 0; `/workspace` lists `[qualify-link]`; workspace write landed on the host |
| `p3.adversarial.escape_matrix` | pass — exit 0; `leaked=none`; outside paths `ENOENT`, traversal `ENOENT`, read-only root `EROFS`/`EACCES`, workspace symlink target `ABSENT` |
| `p3.adversarial.credentials` | pass — exit 0; `HOME=/tmp`; host credential stores visible: none |
| `p3.adversarial.memory_limit` | pass — exit **137**; `memory.max=536870912`, `memory.swap.max=536870912`, host `SwapTotal=1048572 kB` |
| `p3.adversarial.descendants` | pass — exit 0; surviving containers 0 |
| `p3.product.argv` | pass — every hardening flag present (`missing=none`); 1 bind mount; `privileged=False` |
| `p3.product.argv_parity` | pass — the suite probes exactly the argv the product builds (17 flags match) |
| `p3.product.no_host_fallback` | pass — with a missing runtime, both the simulated and the real configuration deny with `PermissionError`; host write simulated `False`, real `False` |
| `p3.product.isolation_smoke` | pass — `scripts/isolation_smoke.py --live` exit 0; **`dependent_execution: released under the observed container boundary`** |

The last row is the first time in this repository's record that
`isolation_smoke.py` released dependent execution against a live container
boundary rather than failing closed.

## 3. The memory-limit check, read exactly

The probe asks for a 3 GiB allocation inside a container limited to 512 MiB and
records what the container's own cgroup files say, before and after:

```
p3.adversarial.memory_limit: exit 137
  memory.max=536870912  memory.swap.max=536870912  meminfo SwapTotal=1048572 kB
```

Exit 137 is the kernel's OOM kill, i.e. the allocation was **refused**. The two
cgroup values explain where the ceiling came from: the probe passes `--memory
512m` and no `--memory-swap`, so the engine applies its default swap allowance
(half of the memory limit here), giving `memory.swap.max` = 512 MiB and a total
ceiling of 1 GiB — well below the 3 GiB requested. The counters the check now
records are the ones this repository added for exactly this purpose, so a future
failure names its mechanism instead of saying only `exit 0`.

## 4. The earlier failing run, and why it is not erased

| When | What the suite recorded |
|---|---|
| 2026-10-30 | The run that first failed this check: a 3 GiB allocation completed (`exit 0`) under `--memory 512m`; no counters were captured, so the mechanism was unknown. |
| 2026-10-31 | The run above: same check, refused (`exit 137`), counters captured. |

The 2026-10-30 record stands as written. What changed on the host is not
measured by either run: the probe's counters were absent then and present now,
and the swap allowance on this host is bounded *now*. The two candidates the
[finding page](m5-2-0-sandbox-memory-limit-finding.md) listed — a swap allowance
that let the allocation escape, or a cgroup the kernel did not charge — are both
compatible with `exit 0` and no counters, so this record does not retroactively
name the earlier cause. It closes the finding because the guarantee now has a
passing observation on the target machine, which is what `M5-P3-01` requires,
not because the earlier observation was explained.

Nothing was re-run until green: the operator ran the suite once, on a later day,
and both outcomes are recorded.

## 5. The residual this run measures

The probe's numbers also describe the **shipped** sandbox, because
`p3.product.argv_parity` proves the probe's argv is the product's argv:

* the product passes `--memory=2g` and no `--memory-swap`, the same shape as the
  probe's `--memory 512m`;
* the engine turns that into a swap allowance of half the memory limit, so the
  sandbox's total ceiling is roughly RAM + swap (**~4 GiB** for the shipped `2g`),
  not the 2 GiB the flag name suggests.

So the guarantee that this record supports is: **the sandbox refuses allocations
past its memory plus the engine's swap allowance**, and the limit is enforced by
the kernel. It is not "the sandbox is confined to 2 GiB". Pinning
`--memory-swap` to the same value as `--memory` would make the total equal to the
stated limit; that is a product hardening change with its own tests and its own
operator re-run, and it is deliberately **not** made here on the strength of a
single observation of an engine default.

## 6. Acceptance criteria

1. **Each claimed guarantee has positive and adversarial evidence** — met: the
   positive path (`p3.positive.execute`) is paired with adversarial escape,
   credential, descendant and resource-limit checks, all in one container run.
2. **Unqualified guarantees remain unavailable** — met: `p3.product.no_host_fallback`
   shows denial for a missing runtime in both the simulated and the real
   configuration, and `STUDIO_EXECUTION_MODE` still defaults to `disabled`.
3. **No silent host fallback** — met: the same check records
   `host_write simulated=False real=False`, and `p3.product.argv_parity` proves
   the qualified argv is the one the product constructs.

## 7. What this record does not establish

* It does not qualify any other machine, engine, runtime version or image: the
  binding is to Docker Desktop 29.5.3 on this Apple M5 with image digest
  `python@sha256:aa943ce41142…`.
* It does not claim swap is *unavailable* to a sandboxed process — §5 measures
  the allowance instead.
* It does not qualify the hardware suite: in the same invocation `M5-P4-01`
  was `blocked` because the optional `--pressure-gb` / `--model-command` phases
  were not requested. The measured hardware record is
  [`docs/m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md).
* The CI environment's `scripts/isolation_smoke.py` summary still reads
  `isolation_status: unqualified` — CI has no container runtime, and that is a
  statement about CI, not about the target host.

## 8. Reproduce

```bash
./scripts/qualify_environment.sh                      # both suites
python3 scripts/qualify_environment.py --sandbox --runtime docker   # sandbox only
```

Exit codes are the contract: `0` qualified, `1` unqualified (an observed
violation), `2` blocked (a prerequisite could not be met). A `blocked` result is
not a security finding and must not be recorded as one.
