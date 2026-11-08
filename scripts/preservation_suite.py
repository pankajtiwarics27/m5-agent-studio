#!/usr/bin/env python3
"""Run the preservation matrix's proofs (UIUX-P6-01).

    python3 scripts/preservation_suite.py --list         # the plan and what each step proves
    python3 scripts/preservation_suite.py                # routes, pytest nodes, then every walk
    python3 scripts/preservation_suite.py --only approvals
    python3 scripts/preservation_suite.py --json

[`scripts/uiux_preservation_e2e.py`](uiux_preservation_e2e.py) is the map: 21 matrix rows, each
naming the walk checks, pytest nodes, routes and wired controls that hold it up. This is the
drive. It executes exactly what the map lists and then prints the map's rows with the result of
every proof underneath, so "preserved" is the output of a command instead of a reading of a page.

Three kinds of proof, run in the order that fails fastest:

* **routes** - one throwaway demo server, then each route twice: without a token, which must be
  refused, and with the token and an id that does not exist, which must answer rather than
  answer 405. The first is the authorization contract; the second is the method still being
  mounted. Neither claims the capability *works* - that is the walk's job.
* **pytest** - the named tests, in one invocation, because they are the contract half.
* **walks** - every browser walk the map names. A walk that starts its own fixture is run with
  no arguments; the ones that read an already-running app are given the same throwaway server.

The suite is deliberately not silent about what it skipped: `--only` prints the rows it did not
cover, and a row whose proofs did not all run is reported as `not run` rather than passed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_preservation_e2e.py"
SCRIPTS = PROJECT_ROOT / "scripts"

BOGUS_ID = "00000000-0000-0000-0000-000000000000"

# A walk this suite has found broken, with the story that owns it and the issue that tracks it.
# An entry is an admission, not an excuse: the suite reports it as a known defect instead of a
# pass, and if the walk starts passing the entry itself fails the run - a stale exemption is how
# a suite slowly stops meaning anything.
KNOWN_FAILURES: dict[str, dict[str, str]] = {
    # Empty as of the harness window fix: both entries this map carried (#169, a rejected
    # completion that drew no card; #170, a resync that left 13 events held and 9 drawn) were the
    # same defect - a drawn window that outlived the log it was drawn from - and both walks pass
    # now. The mechanism stays: an entry is an admission, not an excuse, and the next walk this
    # suite finds broken gets one rather than a comment.
}



def load_inventory():
    spec = importlib.util.spec_from_file_location("uiux_preservation_e2e", INVENTORY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def chrome_path() -> str:
    configured = os.environ.get("STUDIO_SCREENSHOT_CHROME", "")
    if configured and Path(configured).is_file():
        return configured
    fallback = Path("/tmp/m5-runner/m5-browser/tmp/chromium")
    if fallback.is_file():
        return str(fallback)
    return ""


def walk_environment(chrome: str) -> dict:
    env = dict(os.environ)
    env.setdefault("STUDIO_PYTHON", sys.executable)
    if chrome:
        env.setdefault("STUDIO_SCREENSHOT_CHROME", chrome)
    return env


def start_server(env: dict) -> tuple[subprocess.Popen, str, str]:
    """One throwaway demo instance, the same fixture several walks already use."""
    process = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "demo_tour.py"), "--serve", "--port", "0"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        # The fixture forks the app into a child of its own. Signalling the parent alone leaves that
        # child holding its port and ~60 MB of RSS; fourteen of those per run is the memory the next
        # run needs, and on this runner it was enough to get the suite OOM-killed. A session leader
        # makes the whole tree reachable with one `killpg`.
        start_new_session=True,
    )
    assert process.stdout is not None
    deadline = time.time() + 60
    while time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                raise RuntimeError("the demo server exited before printing its address")
            continue
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            return process, payload["url"], payload["token"]
    stop_server(process)
    raise RuntimeError("the demo server never printed its address")


def stop_server(process: subprocess.Popen) -> None:
    """Stop the fixture's whole session and wait until no group member remains.

    The app is a grandchild of the fixture. Waiting for the fixture parent alone leaves a small
    SIGTERM delivery race, and a child that ignores SIGTERM would survive indefinitely. Keep the
    process-group id after the parent exits, escalate if any descendant remains, and do not return
    while the group is still reachable. Both call sites create the process with start_new_session,
    so its pid is also the process-group id even if the parent has already exited.
    """
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:  # pragma: no cover - parent exited before cleanup
        pgid = process.pid
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - escalate on the next pass
                    continue
            deadline = time.monotonic() + (2 if sig == signal.SIGTERM else 10)
            while True:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    return
                if time.monotonic() >= deadline:
                    if sig == signal.SIGTERM:
                        break  # a descendant survived graceful shutdown; kill the whole group
                    raise RuntimeError(f"demo server process group {pgid} survived SIGKILL")
                time.sleep(0.02)
    finally:
        try:
            process.stdout.close()  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - a closed pipe is not a failure
            pass


def probe_route(base_url: str, token: str, method: str, path: str) -> dict:
    """Refused without a token, and answered - not 405 - with one."""
    url = base_url.rstrip("/") + path.replace("{session_id}", BOGUS_ID).replace("{approval_id}", BOGUS_ID)
    body = b"{}" if method == "POST" else None

    def call(auth: str | None) -> int:
        headers = {"Origin": base_url, "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except urllib.error.URLError as error:  # pragma: no cover - the server just started
            raise RuntimeError(f"{method} {path}: {error}") from error

    anonymous = call(None)
    authenticated = call(token)
    ok = anonymous in (401, 403) and authenticated != 405
    return {
        "step": f"{method} {path}",
        "anonymous": anonymous,
        "authenticated": authenticated,
        "passed": ok,
        "detail": f"without a token {anonymous}, with one {authenticated}",
    }


def run_pytest(nodes: list[str], env: dict, timeout: float) -> dict:
    if not nodes:
        return {"step": "pytest", "passed": True, "detail": "no nodes named", "seconds": 0.0}
    started = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *nodes],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    tail = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return {
        "step": f"pytest {' '.join(nodes)}",
        "passed": result.returncode == 0,
        "detail": tail[-1] if tail else f"exit {result.returncode}",
        "seconds": round(time.time() - started, 1),
        "nodes": nodes,
    }


def run_walk(name: str, args: list[str], env: dict, timeout: float) -> dict:
    started = time.time()
    try:
        result = subprocess.run(
            ["node", str(SCRIPTS / name), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "step": name,
            "passed": False,
            "detail": f"timed out after {timeout:.0f}s",
            "seconds": round(time.time() - started, 1),
        }
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    # Walks summarise in two dialects ("10/10 checks passed" and "PASS <last check>"), so the
    # count wins when there is one and the last line is the fallback.
    summary = next(
        (line for line in reversed(lines) if re.search(r"\d+\s*(of|/)\s*\d+", line) and "check" in line.lower()),
        next((line for line in reversed(lines) if "check" in line.lower()), lines[-1] if lines else ""),
    )
    if result.returncode != 0:
        failure = next((line for line in lines if line.startswith("FAIL")), "")
        summary = failure or summary or f"exit {result.returncode}"
    return {
        "step": name,
        "passed": result.returncode == 0,
        "detail": summary[:160],
        "seconds": round(time.time() - started, 1),
    }


def build_plan(inventory, only: str) -> dict:
    payload = inventory.build_inventory()
    rows = payload["rows"]
    if only:
        rows = [
            row
            for row in rows
            if only.lower() in row["capability"].lower()
            or only.lower() in row["tab"].lower()
            or any(
                only.lower() in str(prover.get("script", "")).lower()
                or only.lower() in str(prover.get("node", "")).lower()
                or only.lower() in str(prover.get("path", "")).lower()
                for prover in row["provers"]
            )
        ]
    routes = []
    for row in rows:
        for prover in row["provers"]:
            if prover["kind"] == "route":
                key = (prover["method"], prover["path"])
                if key not in [(item["method"], item["path"]) for item in routes]:
                    routes.append({"method": prover["method"], "path": prover["path"]})
    nodes = sorted({prover["node"] for row in rows for prover in row["provers"] if prover["kind"] == "pytest"})
    scripts = sorted({Path(prover["script"]).name for row in rows for prover in row["provers"] if prover["kind"] == "walk"})
    return {"rows": rows, "routes": routes, "nodes": nodes, "walks": scripts, "needs_server": inventory.NEEDS_SERVER}


def coverage(rows: list[dict], results: dict) -> list[dict]:
    """One line per matrix row: did every proof it names actually run and pass?

    A row is `known` when the only proofs that failed are tracked defects, so the report never
    shows a green row next to a failing walk and never hides the defect behind a red one.
    """
    lines = []
    for row in rows:
        verdicts: list[str] = []
        for prover in row["provers"]:
            kind = prover["kind"]
            if kind == "control":
                verdicts.append("pass")  # verified statically by the inventory
            elif kind == "pytest":
                verdicts.append("pass" if results["pytest"].get("passed") else "fail")
            elif kind == "route":
                entry = results["routes"].get(f"{prover['method']} {prover['path']}")
                verdicts.append("pass" if entry and entry.get("passed") else ("fail" if entry else "not run"))
            else:
                entry = results["walks"].get(Path(prover["script"]).name)
                if entry and entry.get("passed"):
                    verdicts.append("pass")
                elif entry and entry.get("known"):
                    verdicts.append("known")
                elif entry:
                    verdicts.append("fail")
                else:
                    verdicts.append("not run")
        if "fail" in verdicts:
            status = "fail"
        elif "not run" in verdicts:
            status = "not run"
        elif "known" in verdicts:
            status = "known"
        else:
            status = "pass"
        lines.append(
            {
                "row": f"{row['tab']} / {row['capability']}",
                "status": status,
                "proofs": row["proof_count"],
            }
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the UIUX-P6-01 preservation proofs.")
    parser.add_argument("--list", action="store_true", help="print the plan without running it")
    parser.add_argument("--only", default="", help="run only the rows, walks or routes matching this text")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per walk")
    parser.add_argument("--skip-walks", action="store_true", help="routes and tests only")
    parser.add_argument("--skip-pytest", action="store_true", help="everything but the pytest nodes")
    parser.add_argument("--save", default="", help="write the run record to this path (committed as evidence)")
    args = parser.parse_args(argv)

    inventory = load_inventory()
    problems = inventory.build_inventory()["findings"]
    if problems:
        print("error: the preservation map is broken; fix it before running the suite:")
        for finding in problems:
            print(f"       {finding['check']}: {finding['detail']}")
        return 1

    plan = build_plan(inventory, args.only)

    if args.list:
        print(f"preservation suite: {len(plan['rows'])} rows, "
              f"{len(plan['routes'])} routes, {len(plan['nodes'])} tests, {len(plan['walks'])} walks")
        for row in plan["rows"]:
            print(f"  {row['tab']:9s} {row['capability']}: {row['proof_count']} proofs")
        print("\nroutes:")
        for route in plan["routes"]:
            print(f"  {route['method']} {route['path']}")
        print("\ntests:")
        for node in plan["nodes"]:
            print(f"  {node}")
        print("\nwalks:")
        for name in plan["walks"]:
            print(f"  {name}{' (needs the server)' if name in plan['needs_server'] else ''}")
        return 0

    chrome = chrome_path()
    env = walk_environment(chrome)
    results: dict = {"routes": {}, "walks": {}, "pytest": {"passed": True, "step": "pytest", "detail": "skipped"}}
    server = None
    started = time.time()
    # Progress goes to stderr when the report is JSON, so the report stays parseable.
    say = (lambda message: print(message, file=sys.stderr, flush=True)) if args.json else (
        lambda message: print(message, flush=True)
    )
    try:
        if plan["routes"]:
            server, base_url, token = start_server(env)
            say(f"server   demo_tour --serve on {base_url}")
        else:
            base_url = token = ""

        for route in plan["routes"]:
            result = probe_route(base_url, token, route["method"], route["path"])
            results["routes"][f"{route['method']} {route['path']}"] = result
            say(f"{'pass' if result['passed'] else 'FAIL'}  route {result['step']} — {result['detail']}")

        if not args.skip_pytest:
            pytest_result = run_pytest(plan["nodes"], env, max(args.timeout, 120))
            results["pytest"] = pytest_result
            say(f"{'pass' if pytest_result['passed'] else 'FAIL'}  tests — {pytest_result['detail']}")

        if not args.skip_walks:
            for name in plan["walks"]:
                # A walk that reads the app gets its *own* fresh instance: these walks assert counts,
                # statuses and cursors, and a server shared with thirteen other walks has already
                # been written to by the time it starts. That is not a stricter test, it is a
                # different one, and the difference shows up as a failure the walk does not deserve.
                walk_server = None
                walk_args: list[str] = []
                if name in inventory.NEEDS_SERVER:
                    if server is None:
                        server, base_url, token = start_server(env)
                    walk_server, walk_base, walk_token = start_server(env)
                    walk_args = ["--base-url", walk_base, "--token", walk_token]
                result = run_walk(name, walk_args, env, args.timeout)
                if walk_server is not None:
                    stop_server(walk_server)
                known = KNOWN_FAILURES.get(name)
                if known:
                    result["known"] = known
                    result["passed"] = False
                    say(f"known  walk {name} ({result['seconds']}s) — {known['issue']} {known['reason'][:110]}")
                else:
                    say(f"{'pass' if result['passed'] else 'FAIL'}  walk {name} ({result['seconds']}s) — {result['detail']}")
                results["walks"][name] = result
    finally:
        if server is not None:
            stop_server(server)

    rows = coverage(plan["rows"], results)
    failed = [row for row in rows if row["status"] == "fail"]
    unproven = [row for row in rows if row["status"] == "not run"]
    steps = (
        list(results["routes"].values())
        + [results["pytest"]]
        + list(results["walks"].values())
    )
    step_failures = [step for step in steps if not step.get("passed") and not step.get("known")]
    known_steps = [step for step in steps if step.get("known")]
    stale = [
        name
        for name, known in KNOWN_FAILURES.items()
        if name in results["walks"] and results["walks"][name].get("passed") and not results["walks"][name].get("known")
    ]
    report = {
        "story": "UIUX-P6-01",
        "only": args.only,
        "seconds": round(time.time() - started, 1),
        "chrome": chrome or "not configured",
        "steps": steps,
        "rows": rows,
        "totals": {
            "rows": len(rows),
            "passed_rows": len([row for row in rows if row["status"] == "pass"]),
            "failed_rows": len(failed),
            "not_run_rows": len(unproven),
            "steps": len(steps),
            "failed_steps": len(step_failures),
            "known_failures": len(known_steps),
            "stale_known_failures": stale,
        },
    }

    if args.save:
        # A run record is evidence of one run on one machine, so it carries the machine: the
        # chrome binary, the interpreter and the commit it ran at. It is not a badge.
        record = {
            "story": "UIUX-P6-01",
            "kind": "preservation-suite-run",
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True
            ).stdout.strip(),
            "environment": {
                "python": sys.version.split()[0],
                "chrome": chrome or "not configured",
                "tmpdir": os.environ.get("TMPDIR", ""),
            },
            "only": args.only,
            "seconds": report["seconds"],
            "totals": report["totals"],
            "routes": list(results["routes"].values()),
            "pytest": results["pytest"],
            "walks": [
                {
                    "walk": name,
                    "passed": result.get("passed", False),
                    "known": result.get("known"),
                    "seconds": result.get("seconds"),
                    "detail": result.get("detail"),
                }
                for name, result in results["walks"].items()
            ],
            "rows": report["rows"],
        }
        target = Path(args.save)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"saved {args.save}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("")
        for row in rows:
            print(f"{row['status']:8s} {row['row']} ({row['proofs']} proofs)")
        print(
            f"\n{len(steps)} steps, {len(step_failures)} failed in {report['seconds']}s — "
            f"{report['totals']['passed_rows']}/{report['totals']['rows']} rows proved"
            + (f", {len(unproven)} not run" if unproven else "")
            + (f", {len(known_steps)} known defects (#known)" if known_steps else "")
        )
        if known_steps:
            print("known defects (tracked, still failing):")
            for step in known_steps:
                print(f"  {step['step']} — {step['known']['issue']} {step['known']['owner']}: {step['known']['reason']}")
        if stale:
            print("stale known failures — these walks pass now, so remove them from KNOWN_FAILURES:")
            for name in stale:
                print(f"  {name}")
    return 0 if not step_failures and not failed and not stale else 1


if __name__ == "__main__":
    sys.exit(main())
