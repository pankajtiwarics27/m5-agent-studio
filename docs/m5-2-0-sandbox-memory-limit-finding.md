# Sandbox memory-limit finding (M5-P3-01, resolved 2026-10-31)

A qualification finding about the container memory limit the product ships,
**closed by a later operator run on the same machine**. It records what one run
observed on real hardware, the three mechanisms that could explain it, what each
would mean for the product, and the evidence that settled it. The original
analysis is kept unchanged below, because the run that closed the finding did
not explain the earlier observation — it produced a passing one.

| Field | Value |
|---|---|
| Status | **resolved 2026-10-31** — the check now passes on the target host (`exit 137`, counters captured); see [Resolution](#resolution-2026-10-31) |
| Story | `M5-P3-01` (Sandbox backend qualification suite) |
| First observed | 2026-10-31, operator run on Apple M5, macOS 26.6.1 |
| Engine | Docker Desktop 29.5.3, engine 29.5.3 build `d1c06ef`, context `desktop-linux`, `CgroupVersion=2` / driver `cgroupfs`, overlayfs, VM memory 8,321,515,520 B |
| Repository revision | `d0e47f0` (`main` at the time of the run) |
| Failing check | `p3.adversarial.memory_limit` — "a 3 GiB allocation is refused under `--memory 512m`" |
| Recorded detail (then) | `exit 0` |
| Artifact (operator's machine, `dist/` is ignored) | `qualification-20261031T074459Z.json` / `.md` |

## Resolution (2026-10-31)

The operator re-ran the suite on the same Apple M5 with Docker Desktop 29.5.3.
`p3.adversarial.memory_limit` **passed**: exit **137**, with
`memory.max=536870912`, `memory.swap.max=536870912` and host
`SwapTotal=1048572 kB` captured by the two-phase probe. The probe container
(`--memory 512m`, no `--memory-swap`) therefore had a bounded swap allowance of
512 MiB, so its total ceiling was 1 GiB and the 3 GiB allocation was refused by
the kernel. All twelve sandbox checks passed and `M5-P3-01` is `qualified`; the
full record is
[`docs/m5-2-0-sandbox-isolation-evidence.md`](m5-2-0-sandbox-isolation-evidence.md).

What that does and does not settle:

* **Settled:** on this host, engine and image the limit is enforced, and the
  counters needed to read a future failure are now recorded.
* **Not settled:** the cause of the 2026-10-30 `exit 0`. That record captured no
  counters, so candidates A and B both remain compatible with it. No re-run was
  performed until it turned green — a later run produced a different observation.
* **Newly measured:** the engine's default swap allowance (half the memory
  limit) applies to the shipped `--memory=2g` too, so the sandbox's ceiling is
  RAM + swap (~4 GiB), not 2 GiB. Pinning `--memory-swap` is a follow-up
  hardening change, not something this finding closes.

`dependent_execution` is no longer blocked by this finding: the qualification
run's `p3.product.isolation_smoke` released it under the observed container
boundary.

## 1. What was observed

The `--sandbox --runtime docker` suite passed 11 of its 12 checks on that
machine — runtime availability, local image, evidence binding, positive
execution, the escape matrix, credentials, descendants, product argv and argv
parity, no-host-fallback, and the isolation smoke test — and failed exactly one:

```
[FAIL   ] p3.adversarial.memory_limit: a 3 GiB allocation is refused under --memory 512m — exit 0
```

Two hand-run controls by the operator, same machine, same engine:

```console
$ docker info --format 'mem={{.MemTotal}} limit={{.MemoryLimit}} cgroup={{.CgroupVersion}}/{{.CgroupDriver}}'
mem=8321515520 limit=true cgroup=2/cgroupfs

$ docker run --rm --memory=512m --entrypoint /bin/sh python:3.12-slim -c 'cat /sys/fs/cgroup/memory.max'
536870912

$ docker run --rm --memory=512m python:3.12-slim python3 -c 'b=bytearray(3*1024**3); print("allocated", len(b))'
allocated 3221225472
```

So the request reaches the engine, the container's own cgroup namespace reports a
512 MiB cap, and a 3 GiB allocation still completes with exit status 0.

This is **not** an artifact of an untouched allocation. The suite's probe uses
`bytearray`, which faults every page as it zeroes the buffer, and since the
2026-10-31 follow-up it also writes one byte per 4 KiB page explicitly and reports
the resulting RSS. A local control on Linux shows the distinction directly: a
1 GiB `bytearray` raises RSS from 8 MiB to 1,032 MiB, while an untouched 1 GiB
`mmap` leaves RSS at 8 MiB.

## 2. What it might be — three candidates, all still open

| # | Mechanism | What would prove it | What it would mean |
|---|---|---|---|
| A | **Swap escape.** An anonymous page that would exceed `memory.max` is reclaimed into the Docker Desktop VM's swapfile; the process keeps allocating and never hits the OOM killer. `--memory` caps RAM, and neither the product nor the suite passes `--memory-swap`. | The probe's new pre-allocation report shows `memory.max=536870912` with `memory.swap.max=max`; the post-allocation report shows `memory.swap.current` of roughly allocation minus 512 MiB while `memory.current` stays near the cap; `docker inspect` shows `HostConfig.MemorySwap = 0` (unset); `docker info` shows `SwapLimit=true`. | The sandbox limit is a **RAM cap, not a total cap**: runaway sandboxed code can still consume the VM's swap and degrade everything else on the machine. Fixable by pinning `--memory-swap` to the same value as `--memory`, which is a product hardening change, not a suite change. |
| B | **Written but not enforced.** `memory.max` exists in the container's cgroup but pages are charged to a different (sibling or ancestor) cgroup, so the controller never sees the overrun — a placement or driver problem, of the kind seen where a cgroupfs driver puts containers in a subtree that does not own the limit. | `memory.current` stays far below 512 MiB while 3 GiB is resident, `memory.events` shows `max 0` (the limit was never hit), `docker stats` reports usage above the stated limit, and the post-allocation RSS disagrees with `memory.current`. | The cap is **nominal on this engine**: any memory guarantee the sandbox claims is unavailable there, and dependent execution must stay blocked regardless of other checks passing. |
| C | **Limit enforced, process never resident.** The interpreter allocated virtual address space without committing pages. | Ruled out for the suite's probe by the page-write loop and the RSS control above; would only return as an explanation if a run reports `rss_bytes` far below `allocated_bytes` alongside `memory.current` below the cap. | Nothing — but the report now makes it checkable instead of assumed. |

The three are distinguishable from **one** re-run with the current suite: the
probe reports `memory.max`, `memory.swap.max`, `memory.current`, `memory.peak`,
`memory.swap.current`, `memory.events` and RSS both before and after the
allocation, and the pre-allocation line is flushed so an OOM kill cannot remove
it.

## 3. Product consequence, whichever candidate holds

The product builds its container with `--memory=2g` and no `--memory-swap`
(`sandbox_boundary.build_container_invocation`, mirrored by the suite's
`container_argv` and pinned by `p3.product.argv_parity`). The failing probe runs
with `512m` deliberately, to be *stricter* than the product, but the flag set is
otherwise the same — so the mechanism, once known, applies to the shipped sandbox
on this engine.

- If **A**: the shipped `2g` behaves as "2 GiB of RAM plus swap headroom", which
  is not what the hardening flags claim to bound.
- If **B**: the shipped `2g` bounds nothing on this engine.

Neither conclusion is drawn here. Because isolation is unqualified,
`dependent_execution` remains blocked (fail closed) and the release stays blocked;
no `--memory-swap` change is made on speculation, because a hardening change made
before the mechanism is known would obscure which change fixed what.

## 4. What this finding blocks

- `M5-P3-01`'s acceptance criterion "each claimed guarantee has positive and
  adversarial evidence" — the resource-limit guarantee has adversarial evidence,
  and it is adverse.
- `M5-P5-03`'s criterion "no known critical unauthorized execution paths" cannot
  be evaluated as clean while a boundary control is unqualified; the release gate
  must fail closed on this record rather than on a green test suite.

## 5. What would close it

In order, on a host with the same engine:

```bash
# 1. The suite's own record, which now carries the counters for both outcomes.
PYTHON=.venv/bin/python ./scripts/qualify_environment.sh --sandbox --runtime docker

# 2. Engine-side accounting facts for the record.
docker info --format 'swapLimit={{.SwapLimit}} memoryLimit={{.MemoryLimit}} cgroup={{.CgroupVersion}}/{{.CgroupDriver}}'

# 3. A control that removes swap from the equation entirely.
docker run --rm --memory=512m --memory-swap=512m python:3.12-slim \
  python3 -c 'b=bytearray(3*1024**3); print("allocated", len(b))'; echo "exit=$?"
```

Decision rule:

| Observation | Conclusion |
|---|---|
| Step 1 shows `memory.swap.max=max` and `memory.swap.current` in the gigabytes | **A** — product hardening (`--memory-swap` parity), then re-qualify |
| Step 1 shows `memory.current` near zero, `memory.peak` low and `memory.events` without a `max` hit | **B** — engine/placement record; the cap is unavailable on that engine and the qualification stays failed |
| Step 3 is refused (137 or `MemoryError`) while step 1 is not | **A**, with the swap allowance as the only difference |
| Step 3 also succeeds | **B** — a swap cap did not bind either, so the controller is not enforcing on that engine |
| Step 1 passes | The finding is closed by evidence; `M5-P3-01` moves out of `blocked` only after the whole suite is `qualified` |

## 6. What is not claimed

- No claim that the sandbox is safe or unsafe in general: eleven other checks
  passed, and this record says nothing about them.
- No claim of an engine vulnerability. The observed behaviour is consistent with
  documented cgroup v2 swap accounting and with misplacement; neither is a CVE.
- No claim about other engines, other Docker Desktop versions, Linux cgroup v1, or
  rootless Podman. One machine, one engine, one date.
- No waiver. The check remains required, the story remains `blocked`, and a
  re-run is evidence, not a retry-until-green.
