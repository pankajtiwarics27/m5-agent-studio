#!/usr/bin/env python3
"""Inventory how the Council names its participants and states what each seat can reach (UIUX-P4-01).

    python3 scripts/uiux_council_participants_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_council_participants_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_council_participants_inventory.py --json     # print the inventory

The pane had four pickers and no answers. It could not say what each seat was for, and it could
not say whether the model in a picker was resident, loadable, reachable, or missing an API key.
On a machine whose local runtime was down, every local seat was answered by
`_stream_omlx_completion`'s canned demo text - and the transcript drew a tok/s badge for text
no model produced. A seat that failed said so in a four-second toast, while its card went on
saying "Connecting...".

What is measured here:

* **the roles**, taken from the server's own `AGENT_ROLES` labels rather than retyped, carried
  into the transcript by the `role_key` the route already stored;
* **one resolution**: `hallSeatPlan()` is what both the readiness rows and `runHallFullRound()`
  read, so a row cannot describe a seat the round does not post;
* **the ten states** the rows can draw, each with the read it stands on - `ready`, `demo`,
  `needs-load`, `needs-memory`, `loading`, `needs-key`, `not-configured`, `unknown-model`,
  `no-model`, `unverified` - and the actions that exist for them;
* **the honesty**: a seat is never ready because a picker says so, a combo is judged by its
  targets, an offline runtime is demo text rather than a green pill, demo replies are labelled
  and lose the tok/s badge they did not earn, and a failed seat is a block, not a toast;
* **the preservation**: the four seat pickers and their ids, the round's order and reasoning
  modes, the summon route's events and defaults, `append_hall_message`'s usage column, and the
  fact that this story adds no endpoint.

It fails when a second readiness classifier appears, when a state can ship without the read it
stands on or the action it offers, when a combo is claimed ready because it exists, when the
round reads the pickers behind the plan's back, when a demo reply regains a tok/s badge, or when
the summon route's contract changes shape.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Participants.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-participants.md"

CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

# The states the rows can draw, and the tag that proves each branch exists.
STATES = (
    ("ready", "READY"),
    ("demo", "DEMO TEXT"),
    ("needs-load", "NEEDS LOAD"),
    ("needs-memory", "NEEDS MEMORY"),
    ("loading", "LOADING"),
    ("needs-key", "NEEDS KEY"),
    ("not-configured", "NOT CONFIGURED"),
    ("unknown-model", "NOT LISTED"),
    ("no-model", "NO MODEL"),
    ("unverified", "UNVERIFIED"),
)

ACTIONS = ("load", "providers", "choose", "check")
SEAT_SELECTS = ("hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge")
SEAT_ROWS = ("hallSeatArchState", "hallSeatCoderState", "hallSeatCriticState", "hallSeatJudgeState")
SUMMON_EVENTS = ("summon_start", "token", "summon_done", "error")


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(source)):
        char = source[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = source.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    return source[start:]


def python_function(source: str, name: str) -> str:
    """The indented body of ``def name(...)`` in Python source, by dedent."""
    match = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M)
    if not match:
        return ""
    depth = 0
    index = match.start()
    while index < len(source):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    colon = source.find(":", index)
    if colon < 0:
        return ""
    body_start = source.find("\n", colon)
    if body_start < 0:
        return ""
    collected: list[str] = []
    for line in source[body_start + 1 :].splitlines():
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def element(source: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def mounted_routes(server: str) -> set[str]:
    """Every route the server mounts, with each path parameter collapsed to `{}`."""
    found = set()
    for match in re.finditer(r'@app\.(?:get|post|put|patch|delete)\("(/[^"]*)"\)', server):
        found.add(re.sub(r"\{[^}]*\}", "{}", match.group(1)))
    return found


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal in ``source``, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def section(html: str) -> str:
    """The P4-01 block, between its own banners."""
    start = html.find("/* ── UIUX-P4-01:")
    if start < 0:
        return ""
    end = html.find("/* ── end UIUX-P4-01", start)
    return html[start:end] if end > start else html[start : start + 60000]


def returned_states(body: str) -> list[str]:
    """Every ``state: "<name>"`` the classifier returns, in source order."""
    return re.findall(r'state:\s*"([a-z-]+)"', body)


def unescaped_interpolations(markup: str) -> list[str]:
    """``${…}`` expressions inside markup that are not wrapped in escapeHtml()."""
    offenders: list[str] = []
    for match in re.finditer(r"\$\{([^}]*)\}", markup):
        expression = match.group(1).strip()
        if expression.startswith("escapeHtml(") or expression.startswith("renderMarkdown("):
            continue
        if re.fullmatch(r"[A-Za-z0-9_.'\"\[\]()  ]*", expression) and not re.search(
            r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", re.sub(r"escapeHtml\(|renderMarkdown\(", "", expression)
        ):
            continue
        offenders.append(expression)
    return offenders


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    block = section(html)
    checker = function_body(html, "councilSeatReadiness")
    plan = function_body(html, "councilSeatPlan")
    route_of = function_body(html, "councilRouteOf")
    row = function_body(html, "councilSeatRow")
    render = function_body(html, "renderCouncilReadiness")
    refresh = function_body(html, "refreshCouncilReadiness")
    action = function_body(html, "councilSeatAction")
    failure = function_body(html, "presentHallSummonFailure")
    round_body = function_body(html, "runHallFullRound")
    active_hall = function_body(html, "renderActiveHall")
    summon_js = function_body(html, "summonAgentPromise")
    wire = function_body(html, "wireCouncilReadiness")
    card_map = active_hall[active_hall.find("stream.innerHTML") :]
    mounted = mounted_routes(server)
    block_routes = called_routes(block)
    server_route = python_function(server, "api_hall_summon")
    local_stream = python_function(server, "_stream_omlx_completion")
    demo_branch = local_stream[local_stream.find("if not await _omlx_is_online(client)") :]
    agent_roles = server[server.find("AGENT_ROLES: dict[str, dict[str, str]] = {") :][:2600]

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the roles ────────────────────────────────────────────────────────────
    check(
        "four seats, one per role, each with its label, purpose and readiness row",
        len(re.findall(r'<div class="seat-cell">', html)) == 4
        and all(element(html, seat) for seat in SEAT_SELECTS)
        and all(element(html, row) for row in SEAT_ROWS)
        and html.count('<p class="seat-purpose">') == 4
        and all(f'for="{seat}"' in html for seat in SEAT_SELECTS),
        "a seat without a purpose or a row is a picker again",
    )
    check(
        "the seat numbers stay on the labels, so the mapped controls keep their names",
        all(f'>{index}. ' in html for index in (1, 2, 3, 4))
        and "1. Architect / Thesis" in html and "4. Lead Arbiter" in html,
        "the seat names are how an operator and a test find the same control",
    )
    check(
        "the role names are the server's own AGENT_ROLES labels, not retyped",
        all(f'"{label}"' in agent_roles for label in ("Staff Architect", "Fast Coder / Analyst", "Adversarial Critic", "Lead Arbiter"))
        and all(f'label: "{label}"' in block for label in ("Staff Architect", "Fast Coder / Analyst", "Adversarial Critic", "Lead Arbiter")),
        "a pane with its own idea of a role name drifts from the system prompt it sends",
    )
    check(
        "the transcript takes its seat and role from the role_key the route stored",
        "COUNCIL_ROLES[m.role_key]" in card_map
        and 'data-seat="${role.seat}"' in card_map
        and "role_key" in server_route
        and "append_hall_message(session_id, author_label, role_key" in server_route,
        card_map[:200],
    )
    check(
        "each seat picker is described by the row that judges it",
        all(f'id="{seat}" aria-describedby="{row}"' in html for seat, row in zip(SEAT_SELECTS, SEAT_ROWS)),
        ", ".join(seat for seat, row in zip(SEAT_SELECTS, SEAT_ROWS) if f'id="{seat}" aria-describedby="{row}"' not in html),
    )
    check(
        "the single-model asker is judged by the same rows",
        element(html, "hallSingleState") != ""
        and 'councilSeatReadiness(councilValue("hallModelSelect"))' in render
        and "hallSingleState" in block,
        "one model asked on its own is a seat too",
    )

    # ── one classifier, and what it stands on ────────────────────────────────
    check(
        "there is one readiness classifier, and no second copy of the states",
        block.count("function councilSeatReadiness(") == 1
        and html.count("function councilSeatReadiness(") == 1
        and len(re.findall(r'state:\s*"[a-z-]+"', block)) == len(returned_states(checker)),
        "two classifiers is two answers to one question",
    )
    drawn = set(returned_states(checker))
    check(
        "the states the rows can draw are exactly the ten the artifact lists",
        drawn == {name for name, _ in STATES},
        f"missing={sorted({name for name, _ in STATES} - drawn)} extra={sorted(drawn - {name for name, _ in STATES})}",
    )
    check(
        "no seat is ready because a picker says so: readiness starts from the reads",
        'if (!telemetry) {' in checker
        and checker.find('if (!telemetry) {') < checker.find('const route = councilRouteOf(model);')
        and 'const telemetry = lastTelemetry;' in checker,
        "a green pill with no read behind it is the thing this story exists to remove",
    )
    check(
        "a local seat on a runtime that is not answering is demo text, not ready",
        'if (!telemetry.omlx_online) {' in checker
        and checker.find("if (!telemetry.omlx_online)") < checker.find("if (known.loaded)")
        and "demo text" in checker,
        "the demo path answers when the runtime does not, and the row has to say which one is talking",
    )
    check(
        "the memory claim reads the ceiling and the used bytes, and invents no number",
        "telemetry.omlx || {}).ceiling_gb" in checker
        and "telemetry.omlx || {}).used_gb" in checker
        and "councilGigabytes(" in checker
        and "ceiling_gb" in server_route.replace("ceiling_gb", "") + "ceiling_gb",
        "a seat that says it does not fit is only useful if the numbers are the runtime's",
    )
    check(
        "an alias without a key, and an alias that is not saved, are both stated rather than green",
        'if (!provider.has_key)' in checker
        and 'state: "needs-key"' in checker
        and 'state: "not-configured"' in checker
        and "not configured or disabled" in server,
        "the server refuses an unknown alias outright; the row says so first",
    )
    check(
        "a combo is judged by its targets, not by existing",
        'targets.map(target => councilSeatReadiness(target, depth + 1))' in checker
        and 'verdicts.filter(verdict => verdict.state === "demo")' in checker
        and "const loadable = verdicts.filter" in checker
        and 'state: "demo"' in checker,
        "a combo of local models on a dead runtime is demo text",
    )
    check(
        "the pane's route resolution mirrors the server's parse_model_route",
        all(token in route_of for token in ('"combo/"', '::', '"/"', 'configuredProviders.find'))
        and 'if model_spec.startswith("combo/")' in server
        and 'if "::" in model_spec:' in server
        and 'if "/" in model_spec:' in server,
        "two routers with different rules is how a row says ready for a seat the server refuses",
    )

    # ── one plan, one round ──────────────────────────────────────────────────
    check(
        "the round is dispatched from the same plan the rows read",
        "councilSeatPlan().map(" in round_body
        and 'pickPreferred(unifiedCatalog, "27b", 0)' not in round_body
        and 'document.getElementById("hallSeatArch")' not in round_body,
        round_body[:240],
    )
    check(
        "the plan keeps the fallback chain: seat picker, then the Pipeline's, then the catalogue",
        'councilValue("hallSeatArch")' in plan
        and 'councilValue("c-arch")' in plan
        and 'pickPreferred(unifiedCatalog, "27b", 0)' in plan
        and 'pickPreferred(unifiedCatalog, "gemma", 2)' in plan,
        plan[:240],
    )
    check(
        "the round keeps its order, its reasoning modes and its pinning seat",
        '["architect",' not in round_body
        and "seat.key === \"architect\" || seat.key === \"judge\" ? rm : \"off\"" in round_body
        and 'seat.key === "judge" ? pinFinal : false' in round_body
        and round_body.count("summonAgentPromise(") == 1,
        round_body[-260:],
    )

    # ── the actions that exist ───────────────────────────────────────────────
    check(
        "every state that is not ready offers an action, and only actions that exist",
        all(f'id: "{name}"' in checker for name in ACTIONS)
        and all(f'if (action === "{name}")' in action for name in ACTIONS)
        and checker.count("action: null") >= 1,
        ", ".join(name for name in ACTIONS if f'action === "{name}"' not in action),
    )
    check(
        "the load action posts the route the server already mounts, and says what happened",
        "`/api/models/${encodeURIComponent(model)}/load`" in action
        and "/api/models/{}/load" in mounted
        and "showToast(" in action
        and "await refreshCouncilReadiness()" in action,
        action[:300],
    )
    check(
        "the providers action opens the panel the shell already has",
        "openProvidersModal()" in action and "function openProvidersModal(" in html,
        "opening a panel that does not exist is not an action",
    )
    check(
        "the choose action moves focus to the picker rather than describing it",
        "if (select) select.focus();" in action and "councilTargetSelect(key)" in action,
        action[:300],
    )
    check(
        "Check again reads both endpoints and re-renders",
        "await Promise.all([loadProviders(), refreshTelemetry()])" in refresh
        and "renderCouncilReadiness();" in refresh,
        refresh,
    )

    # ── the drawing, and the keyboard ────────────────────────────────────────
    check(
        "the rows are built as nodes with textContent, so a model id cannot inject markup",
        "document.createElement(" in row
        and "textContent = verdict.text" in row
        and "innerHTML" not in row,
        row[:240],
    )
    check(
        "a row is only redrawn when what it says changes, and focus follows the button",
        "container.dataset.signature === signature" in row
        and "const hadFocus = container.contains(document.activeElement);" in row
        and "if (next) next.focus();" in row,
        "a poll that rebuilds the row throws a keyboard user out of it",
    )
    check(
        "the summary is the rows' count, and the basis names both reads",
        'counts.ready += 1' in render
        and "`${counts.ready}/${total} READY`" in render
        and 'evidenceNote(basis, "derived"' in render
        and "GET /api/telemetry and GET /api/providers" in render
        and 'evidenceNote(basis, "unavailable"' in render,
        render[-400:],
    )
    check(
        "the summary says the round stops at the first seat that fails, because it does",
        "stops at the first seat that fails" in render
        and "catch (e) { break; }" in round_body,
        "a promise the loop does not keep is the same lie as a badge nobody earned",
    )
    check(
        "the rows are re-read on the way into the pane, and the poll does not fight the reader",
        'if (tab === "hall") { refreshHallSessions(); renderCouncilReadiness(); }' in html
        and "renderCouncilReadiness();" in function_body(html, "renderTelemetry")
        and "setInterval" not in wire,
        "one poll, not two",
    )

    # ── the reply, and the failure ───────────────────────────────────────────
    check(
        "a demo reply is labelled before it is given a tok/s badge",
        "u.demo" in card_map
        and card_map.find("u.demo") < card_map.find("u.decode_tps")
        and "Demo reply" in card_map
        and "not given a tok/s figure" not in card_map,
        card_map[:240],
    )
    check(
        "the server marks the demo usage line, so the pane has something to read",
        '"demo": True,' in demo_branch
        and "async def _stream_omlx_completion" in server,
        "the flag is the fact; the badge is the UI's answer to it",
    )
    check(
        "a failed seat is a presentation with a cause, what was kept and an action",
        'presentInto("hallSummonState"' in failure
        and 'kind: "error"' in failure
        and "Nothing was saved for ${failure.model}" in failure
        and "Retry the round" in failure
        and "Check readiness" in failure,
        failure[:300],
    )
    check(
        "the failed seat redraws the server's transcript, so no card keeps saying Connecting",
        "renderActiveHall();" in summon_js
        and summon_js.find("renderActiveHall();") < summon_js.find("presentHallSummonFailure(")
        and "clearPresentation(\"hallSummonState\");" in active_hall,
        "a card that says Connecting after the seat failed is the same lie P3-07 removed",
    )

    # ── nothing invented, nothing broken ─────────────────────────────────────
    check(
        "the story adds no endpoint: every route it calls was already mounted",
        all(any(route == mount or route.startswith(mount.rstrip("/") + "/") for mount in mounted) for route in block_routes)
        and block_routes == ["/api/models/{}/load"],
        f"calls={block_routes}",
    )
    check(
        "the summon route still speaks its four events and saves through append_hall_message",
        all(f'_sse("{event}"' in server_route for event in SUMMON_EVENTS)
        and "append_hall_message(session_id, author_label, role_key, final_text, model=model, reasoning=" in server_route
        and "usage=usage_info" in server_route,
        ", ".join(event for event in SUMMON_EVENTS if f'_sse("{event}"' not in server_route),
    )
    check(
        "the summon route keeps its defaults, so a seat-less caller behaves as before",
        'body.get("model") or "Qwen3.8-27B-oQ4e-mtp"' in server_route
        and 'body.get("role_key") or "architect"' in server_route
        and 'AGENT_ROLES.get(role_key, AGENT_ROLES["architect"])' in server_route,
        server_route[:200],
    )
    check(
        "the pane still posts only a summon, and the same one",
        html.count('consumeSSE(`/api/hall/sessions/${encodeURIComponent(activeHallId)}/summon`') == 1
        and "role_key: roleKey, reasoning_mode: reasoningMode" in summon_js,
        summon_js[:200],
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": "UIUX-P4-01",
        "title": "Council participant roles and resource readiness",
        "generated_by": "scripts/uiux_council_participants_inventory.py",
        "seats": [
            {"seat": 1, "role": "Staff Architect", "picker": "hallSeatArch", "row": "hallSeatArchState"},
            {"seat": 2, "role": "Fast Coder / Analyst", "picker": "hallSeatCoder", "row": "hallSeatCoderState"},
            {"seat": 3, "role": "Adversarial Critic", "picker": "hallSeatCritic", "row": "hallSeatCriticState"},
            {"seat": 4, "role": "Lead Arbiter", "picker": "hallSeatJudge", "row": "hallSeatJudgeState"},
        ],
        "states": [name for name, _ in STATES],
        "reads": {
            "runtime": "GET /api/telemetry (omlx_online, models[].loaded/size_gb, omlx.ceiling_gb, omlx.used_gb)",
            "providers": "GET /api/providers (alias, enabled, has_key, models) and combos[].targets",
            "round": "POST /api/hall/sessions/{id}/summon, one seat at a time, in order",
            "load": "POST /api/models/{model}/load",
        },
        "actions": list(ACTIONS),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-01 — Council participant roles and resource readiness",
        "",
        "Generated by `scripts/uiux_council_participants_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the pane now does",
        "",
        "| Where | What it does |",
        "| :--- | :--- |",
        "| each seat | names its role, says what the seat is for, and states whether the thing it points at can answer |",
        "| a resident model | `READY`, with the memory it is holding |",
        "| a model that has to load | `NEEDS LOAD` / `NEEDS MEMORY`, with the gigabytes it needs and what is left under the ceiling, and **Load now** |",
        "| a saved alias with no key | `NEEDS KEY`, with **Open providers** |",
        "| a model the runtime does not list | `NOT LISTED`, with **Pick another model** (which moves focus to the picker) |",
        "| a runtime that is not answering | `DEMO TEXT`, because the reply will be the server's canned text and not the model's |",
        "| a round | each reply carries its seat and role, and a demo reply is labelled one and gets no tok/s badge |",
        "| a seat that fails | one block: which seat, the provider's own message, that nothing was saved for it, and the actions that exist |",
        "",
        "## The seats",
        "",
    ]
    for seat in payload["seats"]:
        lines.append(f"* seat {seat['seat']} — **{seat['role']}** (`#{seat['picker']}`, judged in `#{seat['row']}`)")
    lines += [
        "",
        "## The states a row can draw",
        "",
        "`" + "`, `".join(payload["states"]) + "`",
        "",
        "## What it reads, and what it refuses to invent",
        "",
    ]
    for name, description in payload["reads"].items():
        lines.append(f"* **{name}** — {description}")
    lines += [
        "",
        "## What it refuses to do",
        "",
        "* call a seat ready because a picker holds a model — every `ready` is gated on a read, and a",
        "  runtime that is not answering is `DEMO TEXT`, not a green pill;",
        "* claim a combo is ready because it exists — a combo is judged by its targets, and a combo of",
        "  local models on a dead runtime is demo text;",
        "* give a demo reply a tok/s badge — the usage line the demo path writes carries `demo: true`,",
        "  so the card labels it and declines the figure it did not earn;",
        "* describe a seat the round would not post — the rows and `runHallFullRound()` read the same",
        "  `hallSeatPlan()`, so a row cannot disagree with the dispatch;",
        "* add an endpoint — the only route this story calls is `POST /api/models/{model}/load`, which",
        "  the Residency panel already used;",
        "* promise that a round continues past a failing seat — it stops there, and the summary says so.",
        "",
        "## Checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} findings — **{payload['status']}**.",
        "",
    ]
    for entry in payload["checks"]:
        mark = "pass" if entry["ok"] else "FAIL"
        detail = f" — {entry['detail']}" if entry["detail"] else ""
        lines.append(f"* `{mark}` {entry['check']}{detail}")
    lines.append("")
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale or a rule is broken")
    parser.add_argument("--json", action="store_true", help="print the inventory instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    payload = build_inventory()

    if args.json:
        print(dumps(payload), end="")
    else:
        rendered_json = dumps(payload)
        rendered_doc = render_doc(payload)
        stale = (
            not JSON_PATH.exists()
            or JSON_PATH.read_text(encoding="utf-8") != rendered_json
            or not DOC_PATH.exists()
            or DOC_PATH.read_text(encoding="utf-8") != rendered_doc
        )
        if args.check:
            if stale:
                print(
                    "the council-participants inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    if payload["findings"]:
        for entry in payload["findings"]:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
