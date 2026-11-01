#!/usr/bin/env python3
"""Inventory the Chat transcript and its composer (UIUX-P2-04).

    python3 scripts/uiux_chat_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_chat_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_chat_inventory.py --json     # print the inventory

Two questions, one pane. **Who is speaking?** The transcript named nobody until this
story: an answer's model was printed in a footer *under* the answer, a question carried
no label at all, and the pane scrolled itself to the bottom on every token, so reading
back through a long reply was a tug of war with the stream. **What will Send do?** The
composer promised "Enter to send" - true - and then sent whatever `#chatModel` held,
including an empty string, leaving the server to explain.

What the pane does now, and what this artifact checks:

* every turn carries its role and its number in a header (`CHAT_ROLES` is the only
  place a role gets words, and a role without a row fails);
* the transcript follows the stream only when the reader was already at the bottom, and
  otherwise offers a way back down - the old unconditional `scrollTop = scrollHeight`
  fails the walk, not the review;
* one predicate decides whether the composer can send (`chatSendReason()`), and both
  the disabled button and the sentence under it read it: a disabled control that
  explains itself differently from its own hint is two answers to one question;
* the hint is a live region written only when its *reason* changes, so a per-token
  re-render does not read the same sentence aloud forever;
* the tools sentence describes what the route actually does (read-only under policy
  `plan`, falling back to the configured project root) rather than inventing a refusal;
* Clear is undoable, because it sits one stray click away from a long conversation that
  the server cannot give back;
* Enter sends and Shift+Enter does not, in one handler, and the placeholder says so.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat.md"

TONES = ("ok", "warn", "danger", "info", "busy", "neutral")
BODIES = (
    "renderChatMessages",
    "updateChatComposer",
    "chatComposerState",
    "clearChat",
    "sendChat",
    "sendChatRequest",
    "jumpToLatestChatTurn",
)


def function_body(html: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", html)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(html)):
        char = html[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = html.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(html)):
        char = html[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : offset + 1]
    return html[start:]


def role_table(html: str) -> dict[str, dict[str, str]]:
    """``CHAT_ROLES``: the one place a role gets a label and a tone."""
    block = re.search(r"const CHAT_ROLES = \{(.*?)\n  \};", html, re.S)
    if not block:
        return {}
    rows = {}
    for role, body in re.findall(r"(\w+):\s*\{([^}]*)\}", block.group(1)):
        label = re.search(r'label:\s*"([^"]*)"', body)
        tone = re.search(r'tone:\s*"([a-z]+)"', body)
        rows[role] = {
            "label": label.group(1) if label else "",
            "tone": tone.group(1) if tone else "",
        }
    return rows


def chat_contract(app_source: str) -> dict:
    """What ``POST /api/chat/stream`` reads, and the policy it runs the turn under."""
    route = '@app.post("/api/chat/stream")' in app_source
    # Python has no braces to balance: the handler runs to the next decorator.
    start = app_source.find("async def api_chat_stream")
    body = ""
    if start >= 0:
        end = app_source.find("\n@app.", start)
        body = app_source[start : end if end > start else start + 6000]
    fields = sorted(
        {
            field
            for field in (
                "model",
                "messages",
                "reasoning_mode",
                "auto_unload",
                "tools_enabled",
                "workspace_dir",
                "skills",
                "temperature",
                "max_tokens",
            )
            if re.search(rf'body\.get\("{field}"', body)
        }
    )
    policy = re.search(r'"policy":\s*"(\w+)"', body)
    fallback = re.search(r'body\.get\("workspace_dir"\)\s*or\s*str\((\w+)\)', body)
    return {
        "route": "/api/chat/stream" if route else None,
        "fields": fields,
        # The route defaults these, and the client has never sent them: a field with a
        # default is not a contract the pane has to satisfy.
        "optional_fields": sorted(
            field for field in fields if field in {"temperature", "max_tokens"}
        ),
        "policy": policy.group(1) if policy else None,
        "workspace_fallback": fallback.group(1) if fallback else None,
    }


def read_frontend(html: str) -> dict:
    render = function_body(html, "renderChatMessages")
    hint = function_body(html, "updateChatComposer")
    state = function_body(html, "chatComposerState")
    reason = function_body(html, "chatSendReason")
    clearer = function_body(html, "clearChat")
    sender = function_body(html, "sendChat")
    request = function_body(html, "sendChatRequest")
    bar = function_body(html, "updateChatTranscriptBar")
    jump = function_body(html, "jumpToLatestChatTurn")
    helpers = "\n".join(
        function_body(html, name)
        for name in (
            "chatRuntimeOffline",
            "chatModelIsLocal",
            "chatToolsOn",
            "chatToolDirectory",
            "chatModelId",
        )
    )
    key_match = re.search(
        r'getElementById\("chatInput"\)\.addEventListener\("keydown", (.*)', html
    )
    chat_key = key_match.group(1) if key_match else ""
    render_all = "\n".join(
        [render, hint, state, reason, clearer, sender, request, bar, jump, helpers]
    )
    composer_text = "\n".join([state, helpers])
    return {
        "functions": {name: bool(function_body(html, name)) for name in BODIES},
        "roles": {
            "table": role_table(html),
            "emitted": sorted(set(re.findall(r'data-role="(\w+)"', render))),
            "labelled": "chat-turn-role" in render and render.count("${head}") >= 2,
            "numbered": "chat-turn-index" in render,
            "model_in_header": 'm.role === "assistant" && m.model' in render,
        },
        "composer": {
            "one_predicate": bool(reason)
            and "chatSendReason()" in hint
            and "chatSendReason()" in sender,
            "button_reads_it": "send.disabled = Boolean(chatSendReason())" in hint,
            "no_second_rule": len(re.findall(r"\.disabled\s*=", render_all))
            <= 2,  # Send and Clear
            # The guard, not just the variable: the hint must skip the write when the
            # reason and the model are unchanged.
            "hint_written_on_change": "signature !== chatHintSignature" in hint,
            # The blocked reasons are plain returns from chatSendReason(); the rest are
            # object literals. Both are answers the composer can give.
            "reasons": sorted(
                set(re.findall(r'reason:\s*"([a-z-]+)"', state))
                | set(re.findall(r'return "([a-z-]+)"', reason))
            ),
            "tones": sorted(set(re.findall(r'tone:\s*"([a-z]+)"', state))),
            "tools_says_readonly": "read-only" in state,
            "tools_names_a_directory": "configured default project directory" in state,
            "offline_reads_telemetry": "lastTelemetry.omlx_online" in composer_text,
            "local_only_for_offline": "chatModelIsLocal(model)" in composer_text,
            "live_region": 'role="status"' in html and 'id="chatComposerHint"' in html,
        },
        "transcript": {
            "pinned_before_render": "const pinned = chatNearBottom(box);" in render,
            "scroll_guarded": "if (pinned) box.scrollTop = box.scrollHeight;" in render,
            "no_unconditional_scroll": not re.search(
                r"(?<!if \(pinned\) )box\.scrollTop = box\.scrollHeight", render
            ),
            "slack_constant": "CHAT_PIN_SLACK" in html,
            "jump_control": 'id="chatJumpLatest"' in html
            and "jumpToLatestChatTurn()" in html,
            "jump_hidden_when_pinned": "jump.hidden = chatNearBottom(box)" in bar,
            "log_role": 'role="log"' in html,
            "live_off": 'aria-live="off"' in html,
            "aria_busy": 'setAttribute("aria-busy"' in bar,
            "streaming_marked": 'data-streaming="true"' in render,
            "empty_says_what_appears": "replies and reasoning appear here" in render,
        },
        "clear": {
            "undoable": 'kind: "recovery"' in clearer and "Undo" in clearer,
            "keeps_a_copy": "const kept = chatHistory.slice();" in clearer,
            "restores": "chatHistory = kept.slice()" in clearer,
            "noop_when_empty": "if (!chatHistory.length)" in clearer,
            "disabled_when_empty": "clear.disabled = !chatHistory.length" in hint,
        },
        "keyboard": {
            "enter_sends": 'e.key === "Enter"' in chat_key,
            "shift_is_a_newline": "!e.shiftKey" in chat_key
            and "preventDefault" in chat_key,
            "one_handler": len(re.findall(r'id="chatInput"', html)) == 1
            and len(
                re.findall(
                    r'getElementById\("chatInput"\)\.addEventListener\("keydown"', html
                )
            )
            == 1,
            "no_markup_handler": 'onkeydown="' not in html,
            # The placeholder and the "ready" sentence both promise the key; the handler
            # above is what has to keep the promise.
            "placeholder_matches": 'placeholder="Message your selected model… (Enter sends, Shift+Enter adds a line)"'
            in html
            and "Enter sends, Shift+Enter adds a line" in state,
            "described_by_hint": 'aria-describedby="chatComposerHint chatKeyboardHint"' in html
            and 'id="chatKeyboardHint"' in html,
        },
        "stream": {
            "busy_flag": "chatBusy = true;" in request and "chatBusy = false;" in html,
            "busy_cleared_in_finally": "() => { chatBusy = false; renderChatMessages();"
            in request,
            "stop_clears_busy": "stopActiveStream();"
            in function_body(html, "stopChatStream")
            and "() => { chatBusy = false; renderChatMessages();" in request,
            "payload_fields": sorted(
                {
                    field
                    for field in (
                        "model",
                        "messages",
                        "reasoning_mode",
                        "auto_unload",
                        "tools_enabled",
                        "workspace_dir",
                        "skills",
                    )
                    if re.search(rf"^\s*{field}[,:]", request, re.M)
                }
            ),
        },
    }


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = chat_contract(APP.read_text(encoding="utf-8"))
    frontend = read_frontend(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    for name, present in frontend["functions"].items():
        if not present:
            fail("function-missing", f"{name}() is gone")

    # 1. Every turn has words, from one table.
    roles = frontend["roles"]
    if not roles["table"]:
        fail("roles-missing", "CHAT_ROLES is gone, so nothing names who is speaking")
    for role, row in roles["table"].items():
        if not row["label"]:
            fail("role-unlabelled", f"role {role!r} has no label")
        if row["tone"] not in TONES:
            fail(
                "tone-unknown",
                f"role {role!r} uses tone {row['tone']!r}, which is not in the status vocabulary",
            )
    for role in roles["emitted"]:
        if role not in roles["table"]:
            fail(
                "role-unlisted",
                f"the transcript emits role {role!r}, which CHAT_ROLES does not name",
            )
    for role in ("user", "assistant"):
        if role not in roles["emitted"]:
            fail("role-missing", f"no turn is drawn with role {role!r}")
    if not roles["labelled"]:
        fail("turn-unlabelled", "a turn is drawn without a role label")
    if not roles["numbered"]:
        fail(
            "turn-unnumbered", "turns carry no number, so a reader cannot refer to one"
        )
    if not roles["model_in_header"]:
        fail(
            "model-below-the-fold", "the answering model is not named above its answer"
        )

    # 2. One predicate decides, and both the button and the sentence read it.
    composer = frontend["composer"]
    if not composer["one_predicate"]:
        fail(
            "second-rule",
            "the composer's disabled state and its sentence do not come from chatSendReason()",
        )
    if not composer["button_reads_it"]:
        fail("button-differs", "the Send button does not read the predicate")
    if not composer["no_second_rule"]:
        fail("second-rule", "something other than the composer assigns .disabled")
    for reason in composer["reasons"]:
        if reason not in {
            "streaming",
            "no-model",
            "tools-readonly",
            "runtime-offline",
            "ready",
        }:
            fail(
                "reason-unlisted",
                f"the composer can report reason {reason!r}, which no rule describes",
            )
    for tone in composer["tones"]:
        if tone not in TONES:
            fail("tone-unknown", f"the composer hint uses tone {tone!r}")
    if not composer["hint_written_on_change"]:
        fail(
            "live-region-chatter",
            "the hint is rewritten on every render, so a live region repeats itself per token",
        )
    if not composer["live_region"]:
        fail(
            "no-live-region",
            "the composer has no live region, so a screen reader is not told why Send is off",
        )

    # 3. The tools sentence describes the route, not a guess.
    if not composer["tools_says_readonly"]:
        fail("tool-claim", "the tools sentence does not say the tools are read-only")
    if server["policy"] and server["policy"] != "plan":
        fail(
            "tool-claim",
            f"/api/chat/stream now runs policy {server['policy']!r}, so 'read-only' is a stale claim",
        )
    if not composer["tools_names_a_directory"]:
        fail(
            "tool-claim",
            "the tools sentence does not say which directory would be read",
        )
    if server["workspace_fallback"] and not composer["tools_names_a_directory"]:
        fail(
            "tool-claim",
            "the route falls back to a default directory and the pane does not say so",
        )

    # 4. The runtime verdict is read, not guessed, and only for a local model.
    if not composer["offline_reads_telemetry"]:
        fail(
            "runtime-guess",
            "the composer predicts a runtime failure without reading the telemetry verdict",
        )
    if not composer["local_only_for_offline"]:
        fail(
            "runtime-guess",
            "the composer would warn about the local runtime for a cloud model",
        )

    # 5. The transcript follows the reader, not the stream.
    transcript = frontend["transcript"]
    for flag, description in (
        (
            "pinned_before_render",
            "the renderer does not remember whether the reader was at the bottom",
        ),
        (
            "scroll_guarded",
            "the renderer does not scroll only when the reader was at the bottom",
        ),
        (
            "no_unconditional_scroll",
            "an unconditional scrollTop assignment is back: it drags the reader to the bottom on every token",
        ),
        ("slack_constant", "there is no tolerance for 'close enough to the bottom'"),
        ("jump_control", "there is no way back to the newest turn"),
        (
            "jump_hidden_when_pinned",
            "the jump control is not hidden when the reader is already at the bottom",
        ),
        ("log_role", "the transcript is not a log for assistive technology"),
        (
            "live_off",
            "the transcript is a live region, so every token would be read aloud",
        ),
        ("aria_busy", "the transcript does not say it is streaming"),
        ("streaming_marked", "the streaming turn is not marked"),
        (
            "empty_says_what_appears",
            "an empty transcript does not say what will appear",
        ),
    ):
        if not transcript.get(flag):
            fail("transcript", description)

    # 6. Clear is recoverable.
    clear = frontend["clear"]
    for flag, description in (
        ("undoable", "Clear is a one-way door"),
        ("keeps_a_copy", "Clear does not keep the turns it removes"),
        ("restores", "Undo does not restore the turns"),
        ("noop_when_empty", "Clear does nothing useful on an empty transcript"),
        ("disabled_when_empty", "Clear is enabled with nothing to clear"),
    ):
        if not clear.get(flag):
            fail("clear", description)

    # 7. Keyboard: Enter sends, Shift+Enter does not, in one handler.
    keyboard = frontend["keyboard"]
    for flag, description in (
        ("enter_sends", "Enter no longer sends"),
        ("shift_is_a_newline", "Shift+Enter is not distinguished from Enter"),
        ("one_handler", "the composer's key handling is no longer in one place"),
        (
            "no_markup_handler",
            "an inline onkeydown is back, so the key can be sent from two places",
        ),
        ("placeholder_matches", "the placeholder and the key handler disagree"),
        (
            "described_by_hint",
            "the composer is not described by the sentence that explains it",
        ),
    ):
        if not keyboard.get(flag):
            fail("keyboard", description)

    # 8. The stream owns the busy state, and every way out clears it.
    stream = frontend["stream"]
    for flag, description in (
        ("busy_flag", "nothing records that a reply is in flight"),
        (
            "busy_cleared_in_finally",
            "the busy state is not cleared when the stream ends",
        ),
        ("stop_clears_busy", "Stop leaves the composer disabled"),
    ):
        if not stream.get(flag):
            fail("busy", description)
    missing = [
        field
        for field in server["fields"]
        if field not in server["optional_fields"]
        and field not in stream["payload_fields"]
    ]
    if missing:
        fail(
            "payload-drift",
            f"/api/chat/stream reads {missing}, which the client no longer sends",
        )

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P2-04",
        "statement": (
            "Chat's transcript names who is speaking and follows the stream only when the reader was already at the "
            "bottom; its composer says what Send will do, disables the button on the same predicate that writes the "
            "sentence, and keeps Clear undoable."
        ),
        "source": {
            "frontend": "static/index.html",
            "server": "app.py",
            "routes": [server["route"]] if server["route"] else [],
        },
        "server": server,
        "roles": frontend["roles"]["table"],
        "reasons": frontend["composer"]["reasons"],
        "tones_used": frontend["composer"]["tones"],
        "frontend": frontend,
        "totals": {
            "roles": len(frontend["roles"]["table"]),
            "reasons": len(frontend["composer"]["reasons"]),
            "tones": len(frontend["composer"]["tones"]),
            "server_fields": len(server["fields"]),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the walk drives the transcript by writing chatHistory in the page, because the demo has no model runtime: "
            "it proves the rendering, the scroll rule, the composer predicate and the Clear undo, and it proves the "
            "failure surface by sending to a runtime that is not there - it does not prove a model's answer",
            "the read-only claim is checked against the policy literal in app.py, not by attempting a write tool call",
            "the pin tolerance (80px) is a judgement about a hand, not a measurement; it is recorded here so a later "
            "story can change it in one place",
        ],
    }


def render_doc(payload: dict) -> str:
    server = payload["server"]
    roles = payload["roles"]
    role_rows = "\n".join(
        f"| `{role}` | {row['label']} | `{row['tone']}` |"
        for role, row in roles.items()
    )
    reason_rows = "\n".join(
        f"| `{reason}` | {description} |"
        for reason, description in (
            ("streaming", "a reply is in flight; Stop keeps what already arrived"),
            ("no-model", "`#chatModel` holds nothing usable, so Send is disabled"),
            (
                "runtime-offline",
                "a local model is selected and the telemetry verdict says the runtime is not answering",
            ),
            (
                "tools-readonly",
                "tools are on: the route reads the workspace and cannot write to it",
            ),
            ("ready", "Send will send, and the sentence says what Enter does"),
        )
    )
    lines = [
        "# Chat: the transcript and the composer",
        "",
        "Generated by `python3 scripts/uiux_chat_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## Who is speaking",
        "",
        "| Role | Label | Tone |",
        "|---|---|---|",
        role_rows,
        "",
        "`CHAT_ROLES` is the only place a role gets words. Each turn is drawn with `data-role`, a header "
        "carrying its label and its number, and - for an answer - the model that produced it, above the answer "
        "rather than in a footer under it.",
        "",
        'The transcript is `role="log"` with `aria-live="off"`: the pane re-renders on every token, so a live '
        "region here would read the whole conversation aloud, repeatedly. What *is* announced is the composer's "
        'reason, which changes only when the answer to "can I send?" changes.',
        "",
        "## Following the stream, not the reader",
        "",
        "Before this story the renderer ended with `box.scrollTop = box.scrollHeight`, so scrolling up during a "
        "reply dragged the reader back down on the next token. Now the pane asks `chatNearBottom()` *before* it "
        "re-renders and only follows when the reader was already there; otherwise it keeps the position and shows a "
        "**Jump to latest** control, which moves the scroll and the focus together.",
        "",
        f"The tolerance is {80}px: close enough to the bottom counts as reading the newest turn.",
        "",
        "## What Send will do",
        "",
        "| Reason | When |",
        "|---|---|",
        reason_rows,
        "",
        "`chatSendReason()` is the only predicate. The disabled button and the sentence under it both read it, so the "
        "composer cannot tell one story and do another. The sentence is a live region written only when its reason or "
        "the selected model changes.",
        "",
        f"`POST {server['route']}` reads `{', '.join(server['fields'])}` and runs the turn under policy "
        f"`{server['policy']}`, falling back to `{server['workspace_fallback']}` when no directory is sent — which is "
        "why the tools sentence says *read-only* and names the directory rather than claiming the tools will refuse.",
        "",
        "## Clear, and the way back",
        "",
        "Clear keeps the turns it removes and presents the recovery kind with **Undo**, because the transcript lives "
        "only in this page and the server cannot give it back. It is disabled when there is nothing to clear.",
        "",
        "## Checks and limits",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "`node scripts/chat_walk.mjs` drives the pane in a browser: it writes a transcript, holds the labels and the "
        "numbers against what it wrote, scrolls up mid-render and proves the pane does not follow, exercises the "
        "composer's reasons, clears and undoes, and sends to a runtime that is not there to photograph the failure "
        "surface.",
        "",
        "## Regenerating",
        "",
        "1. Change the pane in `static/index.html`, or a field in `POST /api/chat/stream`.",
        "2. `python3 scripts/uiux_chat_inventory.py`.",
        "3. `node scripts/chat_walk.mjs` against a running `scripts/demo_tour.py --serve`.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the artifacts drifted or a rule broke",
    )
    parser.add_argument("--json", action="store_true", help="print the inventory")
    args = parser.parse_args(argv)

    payload = build_inventory()
    text = dumps(payload)
    doc = render_doc(payload)

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        problems = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is stale")
        if problems:
            print("error: the chat inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_chat_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the Chat pane and its server disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"chat OK: {payload['totals']['roles']} roles, {payload['totals']['reasons']} send reasons, "
            f"{payload['totals']['server_fields']} route fields, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['roles']} roles, {payload['totals']['reasons']} reasons, "
        f"{len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
