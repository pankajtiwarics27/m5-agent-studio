#!/usr/bin/env node
/**
 * Check Chat's transcript and composer in a browser (UIUX-P2-04).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/chat_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The demo has no model runtime, so this walk cannot watch a model answer. It does the
 * next honest thing: it writes a transcript into the page (the same shape
 * sendChatRequest() builds), then holds what the pane draws against what was written,
 * and it exercises the half of the story that does not need a model - the labels, the
 * scroll rule, the composer's reasons, Clear and Undo, and the escape key behaviour of
 * the transcript - plus one real send that fails, because a runtime that is not there
 * is exactly the failure this pane has to present well.
 *
 * What it proves:
 *
 *   1. every turn is labelled with its role, its number and - for an answer - the model
 *      that produced it, above the answer;
 *   2. the pane does not follow the stream when the reader has scrolled up, and offers a
 *      way back down that moves the scroll and the focus together;
 *   3. the composer's hint and its disabled button come from one answer: with no model
 *      selected Send is off and the sentence says why, and with a model selected the
 *      sentence says what Enter does;
 *   4. a local model plus a runtime that is not answering is reported as a prediction,
 *      read from the telemetry verdict rather than guessed;
 *   5. Clear is undoable, and Undo puts the turns back exactly;
 *   6. Enter sends and Shift+Enter does not (the newline stays in the field);
 *   7. a real send to an unreachable runtime presents the P1-05 error with Retry, keeps
 *      the message, and takes the composer out of its busy state;
 *   8. no page errors, and the pane survives a 900px viewport.
 */
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = { tokenKey: "m5_api_token", settleMs: 250, timeoutMs: 45000 };

function parseArgs(argv) {
  const args = { ...DEFAULTS, baseUrl: "", token: "", json: false, screenshots: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      i += 1;
      return value;
    };
    switch (arg) {
      case "--base-url": args.baseUrl = next(); break;
      case "--token": args.token = next(); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--json": args.json = true; break;
      case "--help":
      case "-h":
        console.log(usage());
        process.exit(0);
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function usage() {
  return [
    "Check Chat's transcript and composer against what the pane was given.",
    "",
    "  node scripts/chat_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-chat.md.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const target = candidate.startsWith("/") || candidate.startsWith(".")
        ? pathToFileURL(resolve(candidate)).href
        : candidate;
      const module = await import(target);
      return module.default || module;
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(
    `puppeteer-core is not importable. Install it (npm i puppeteer-core) or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`,
  );
}

function resolveChrome() {
  const explicit = process.env.STUDIO_SCREENSHOT_CHROME;
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`STUDIO_SCREENSHOT_CHROME does not exist: ${explicit}`);
    return explicit;
  }
  const candidates = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This script refuses to report a walk it did not perform.",
  );
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--hide-scrollbars",
      "--force-color-profile=srgb",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean),
    ],
  });
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const block = (step, detail) => {
    results.push({ step, passed: false, detail, status: "blocked" });
    if (!args.json) console.log(`block  ${step} — ${detail}`);
  };
  const shoot = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
  };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.evaluateOnNewDocument((key, token) => {
      try {
        window.sessionStorage.setItem(key, token);
      } catch (_error) {}
    }, args.tokenKey, args.token);
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);
    await page.evaluate(() => switchTab("chat"));
    await sleep(args.settleMs * 4);

    // A transcript of the shape sendChatRequest() builds, written rather than streamed:
    // the demo has no runtime, and a walk that could not show a labelled turn would be
    // reporting on an empty pane.
    const written = await page.evaluate(() => {
      const long = Array.from({ length: 24 }, (_, i) => `Line ${i + 1}: the pane has to keep its place while this arrives.`).join("\n\n");
      chatHistory = [
        { role: "user", content: "What does this workspace do, and where would I start?" },
        {
          role: "assistant",
          model: "demo-local-model",
          content: `Here is the shape of it.\n\n${long}`,
          reasoning: "the reader asked for a summary and a starting point",
          tools: [{ tool: "grep_search", args: { pattern: "def main" }, ok: true, elapsed_ms: 42, preview: "app.py:1" }],
          usage: { decode_tps: 41.2, completion_tokens: 320 },
        },
        { role: "user", content: "And the tests?" },
      ];
      renderChatMessages();
      return { turns: chatHistory.length };
    });

    const drawn = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll("#chatMessages .msg")];
      return {
        count: nodes.length,
        roles: nodes.map((node) => node.dataset.role),
        labels: nodes.map((node) => (node.querySelector(".chat-turn-role") || {}).textContent || ""),
        numbers: nodes.map((node) => (node.querySelector(".chat-turn-index") || {}).textContent || ""),
        modelInHeader: (nodes[1] && nodes[1].querySelector(".chat-turn-head code")) ? nodes[1].querySelector(".chat-turn-head code").textContent : "",
        summary: (document.getElementById("chatTurnSummary") || {}).textContent || "",
        logRole: document.getElementById("chatMessages").getAttribute("role"),
        live: document.getElementById("chatMessages").getAttribute("aria-live"),
        busy: document.getElementById("chatMessages").getAttribute("aria-busy"),
      };
    });
    record(
      "every turn carries its role and its number",
      drawn.count === written.turns
        && drawn.roles.join(",") === "user,assistant,user"
        && drawn.labels.join(",") === "You,Model,You"
        && drawn.numbers.join(",") === "1,2,3",
      `${drawn.labels.join(",")} / numbers ${drawn.numbers.join(",")}`,
    );
    record(
      "an answer names its model above the answer, not below it",
      drawn.modelInHeader === "demo-local-model",
      `header model "${drawn.modelInHeader}"`,
    );
    record(
      "the transcript is a log that does not read itself aloud on every token",
      drawn.logRole === "log" && drawn.live === "off" && drawn.busy === "false",
      `role=${drawn.logRole} aria-live=${drawn.live} aria-busy=${drawn.busy} summary="${drawn.summary}"`,
    );

    // ── Following the reader, not the stream ──────────────────────────────
    const scroll = await page.evaluate(() => {
      const box = document.getElementById("chatMessages");
      const before = { top: box.scrollTop, height: box.scrollHeight, client: box.clientHeight };
      if (before.height <= before.client + 20) return { scrollable: false, before };
      box.scrollTop = 0;
      const atTop = box.scrollTop;
      // A new turn arriving while the reader is at the top is the case that used to
      // drag them to the bottom.
      chatHistory.push({ role: "assistant", model: "demo-local-model", content: "A second answer arrives while you are reading the first.", usage: { decode_tps: 30, completion_tokens: 9 } });
      renderChatMessages();
      const jump = document.getElementById("chatJumpLatest");
      return {
        scrollable: true,
        atTop,
        afterTop: box.scrollTop,
        jumpVisible: jump ? !jump.hidden : false,
        scrollHeight: box.scrollHeight,
      };
    });
    if (!scroll.scrollable) {
      block("the pane keeps the reader's place while a reply arrives", "the transcript is shorter than the pane, so there is nothing to scroll");
    } else {
      record(
        "the pane keeps the reader's place while a reply arrives",
        scroll.atTop === 0 && scroll.afterTop === 0 && scroll.jumpVisible === true,
        `scrollTop ${scroll.atTop} -> ${scroll.afterTop}, jump visible=${scroll.jumpVisible}`,
      );
      await shoot(page, "scrolled");
      await page.evaluate(() => {
        const jump = document.getElementById("chatJumpLatest");
        jump.focus();
        jump.click();
      });
      await sleep(args.settleMs * 2);
      const afterJump = await page.evaluate(() => {
        const box = document.getElementById("chatMessages");
        return {
          atBottom: box.scrollHeight - box.scrollTop - box.clientHeight <= 2,
          jumpHidden: document.getElementById("chatJumpLatest").hidden,
          focusInsideTranscript: box.contains(document.activeElement),
        };
      });
      record(
        "Jump to latest moves the view and the focus to the newest turn",
        afterJump.atBottom && afterJump.jumpHidden,
        `atBottom=${afterJump.atBottom} jump hidden=${afterJump.jumpHidden} focus in transcript=${afterJump.focusInsideTranscript}`,
      );
    }
    await shoot(page, "transcript");

    // ── The composer's reasons ────────────────────────────────────────────
    const noModel = await page.evaluate(() => {
      const sel = document.getElementById("chatModel");
      const previous = sel.value;
      sel.innerHTML = "";
      sel.value = "";
      updateChatComposer();
      const line = document.getElementById("chatComposerHint");
      const send = document.getElementById("chatSendBtn");
      const sent = sendChat();
      return {
        previous,
        reason: line.dataset.reason,
        hint: line.textContent.trim(),
        disabled: send.disabled,
        sentAnything: sent !== null,
        historyLength: chatHistory.length,
        focused: document.activeElement === document.getElementById("chatInput"),
      };
    });
    record(
      "with no model the Send button is off and says why",
      noModel.reason === "no-model" && noModel.disabled === true && /no model is selected/i.test(noModel.hint),
      `reason=${noModel.reason} disabled=${noModel.disabled} — ${noModel.hint.slice(0, 90)}`,
    );
    record(
      "a blocked Send explains itself instead of doing nothing",
      noModel.sentAnything === false && noModel.focused === true,
      `returned null=${!noModel.sentAnything} focus in composer=${noModel.focused}`,
    );

    const withModel = await page.evaluate(() => {
      const sel = document.getElementById("chatModel");
      sel.innerHTML = '<option value="demo-local-model">demo-local-model</option><option value="cloud::demo">cloud::demo</option>';
      sel.value = "demo-local-model";
      const tools = document.getElementById("chatToolsEnabled");
      const toolsWas = tools.checked;
      tools.checked = false;
      updateChatComposer();
      const plain = { reason: document.getElementById("chatComposerHint").dataset.reason, hint: document.getElementById("chatComposerHint").textContent.trim() };
      // Measured with a cloud model: on this host the local-model sentence is the
      // runtime prediction, which outranks the tools note by design.
      sel.value = "cloud::demo";
      tools.checked = true;
      updateChatComposer();
      const withTools = { reason: document.getElementById("chatComposerHint").dataset.reason, hint: document.getElementById("chatComposerHint").textContent.trim() };
      tools.checked = toolsWas;
      // A cloud model must not be told the local runtime is down, and with nothing else
      // to warn about it is the state that says what Enter does.
      tools.checked = false;
      sel.value = "cloud::demo";
      updateChatComposer();
      const cloud = { reason: document.getElementById("chatComposerHint").dataset.reason, hint: document.getElementById("chatComposerHint").textContent.trim() };
      sel.value = "demo-local-model";
      tools.checked = toolsWas;
      updateChatComposer();
      return {
        plain,
        withTools,
        cloud,
        disabled: document.getElementById("chatSendBtn").disabled,
      };
    });
    record(
      "with a model nothing is wrong with, the hint says what Enter does",
      withModel.cloud.reason === "ready"
        && /Enter sends, Shift\+Enter adds a line/i.test(withModel.cloud.hint)
        && withModel.disabled === false,
      `${withModel.cloud.reason} — ${withModel.cloud.hint.slice(0, 90)}`,
    );
    record(
      "the tools sentence says read-only and names the directory",
      withModel.withTools.reason === "tools-readonly"
        && /read-only/i.test(withModel.withTools.hint)
        && /(configured default project directory|\/)/.test(withModel.withTools.hint),
      withModel.withTools.hint.slice(0, 140),
    );
    record(
      "a local model on a silent runtime is warned about, and a cloud model is not",
      withModel.plain.reason !== "ready" ? withModel.plain.reason === "runtime-offline" : true
        && withModel.cloud.reason !== "runtime-offline",
      `local reason=${withModel.plain.reason} cloud reason=${withModel.cloud.reason}`,
    );

    const telemetryVerdict = await page.evaluate(async () => {
      const payload = await (await fetch("/api/telemetry")).json();
      lastTelemetry = payload;
      updateChatComposer();
      const line = document.getElementById("chatComposerHint");
      return { online: payload.omlx_online, reason: line.dataset.reason, hint: line.textContent.trim() };
    });
    if (telemetryVerdict.online === false) {
      record(
        "an unreachable local runtime is reported as a prediction, from the verdict",
        telemetryVerdict.reason === "runtime-offline" && /telemetry probe says/i.test(telemetryVerdict.hint),
        `${telemetryVerdict.reason} — ${telemetryVerdict.hint.slice(0, 120)}`,
      );
    } else {
      block("an unreachable local runtime is reported as a prediction, from the verdict", "this host has a live runtime, so the offline branch cannot be reached");
    }

    // ── Keyboard: Enter sends, Shift+Enter does not ────────────────────────
    const shiftEnter = await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      input.focus();
      input.value = "first line";
      input.dispatchEvent(new Event("input"));
      const before = { history: chatHistory.length, value: input.value };
      return { before };
    });
    await page.keyboard.down("Shift");
    await page.keyboard.press("Enter");
    await page.keyboard.up("Shift");
    await page.keyboard.type("second line");
    await sleep(args.settleMs);
    const afterShift = await page.evaluate(() => ({
      history: chatHistory.length,
      value: document.getElementById("chatInput").value,
    }));
    record(
      "Shift+Enter adds a line instead of sending",
      afterShift.history === shiftEnter.before.history && /second line/.test(afterShift.value),
      `${shiftEnter.before.history} -> ${afterShift.history} messages, field "${afterShift.value.replace(/\n/g, "\\n")}"`,
    );

    // ── Clear and Undo ────────────────────────────────────────────────────
    const beforeClear = await page.evaluate(() => chatHistory.length);
    const cleared = await page.evaluate(() => {
      clearChat();
      const box = document.querySelector("#chatState .presentation");
      return {
        empty: chatHistory.length,
        kind: box ? box.dataset.kind : null,
        title: box && box.querySelector(".presentation-title") ? box.querySelector(".presentation-title").textContent.trim() : null,
        context: box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim()) : [],
        turnsDrawn: document.querySelectorAll("#chatMessages .msg").length,
        emptyText: (document.getElementById("chatMessages") || {}).textContent.trim(),
        clearDisabled: document.getElementById("chatClearBtn").disabled,
      };
    });
    record(
      "Clear is presented as something you can undo",
      cleared.empty === 0
        && cleared.kind === "recovery"
        && cleared.actions.includes("Undo")
        && /removed from this pane only/i.test(cleared.context || "")
        && cleared.turnsDrawn === 0,
      `${cleared.kind}: ${cleared.title} — ${cleared.actions.join(", ")}`,
    );
    record(
      "an empty transcript says what will appear and Clear turns itself off",
      /Nothing has been said yet/i.test(cleared.emptyText || "") && cleared.clearDisabled === true,
      `"${(cleared.emptyText || "").slice(0, 70)}" clear disabled=${cleared.clearDisabled}`,
    );
    await shoot(page, "cleared");

    await page.evaluate(() => {
      const undo = [...document.querySelectorAll("#chatState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Undo");
      if (undo) undo.focus();
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 3);
    const undone = await page.evaluate(() => ({
      history: chatHistory.length,
      turns: document.querySelectorAll("#chatMessages .msg").length,
      presentation: Boolean(document.querySelector("#chatState .presentation")),
      focusInComposer: document.activeElement === document.getElementById("chatInput"),
    }));
    record(
      "Undo puts every turn back and returns the cursor to the composer",
      undone.history === beforeClear && undone.turns === beforeClear && !undone.presentation,
      `${undone.history}/${beforeClear} messages, ${undone.turns} turns drawn, focus=${undone.focusInComposer}`,
    );

    // ── A real send, answered ─────────────────────────────────────────────
    // The demo replays a scripted reply, so this is a real stream: the placeholder turn
    // fills in, gains its model header and its usage, and the composer comes back.
    await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      input.value = "say READY";
      input.dispatchEvent(new Event("input"));
      input.focus();
    });
    const beforeSend = await page.evaluate(() => chatHistory.length);
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 24);
    const answered = await page.evaluate(() => {
      const turns = [...document.querySelectorAll("#chatMessages .msg")];
      const last = turns[turns.length - 1];
      return {
        history: chatHistory.length,
        turns: turns.length,
        lastRole: last ? last.dataset.role : null,
        lastLabel: last && last.querySelector(".chat-turn-role") ? last.querySelector(".chat-turn-role").textContent.trim() : null,
        lastModel: last && last.querySelector(".chat-turn-head code") ? last.querySelector(".chat-turn-head code").textContent.trim() : null,
        meta: last && last.querySelector(".msg-meta") ? last.querySelector(".msg-meta").textContent.trim() : "",
        streamingMark: last ? last.getAttribute("data-streaming") : null,
        busy: document.getElementById("chatMessages").getAttribute("aria-busy"),
        sendDisabled: document.getElementById("chatSendBtn").disabled,
        stopHidden: document.getElementById("chatStopBtn").style.display === "none",
        composerEmpty: document.getElementById("chatInput").value === "",
        presentation: Boolean(document.querySelector("#chatState .presentation")),
      };
    });
    record(
      "a sent message is answered by a labelled turn with its model and its usage",
      answered.history === beforeSend + 2
        && answered.lastRole === "assistant"
        && answered.lastLabel === "Model"
        && answered.lastModel === "demo-local-model"
        && /tok\/s/.test(answered.meta),
      `${answered.lastLabel} / ${answered.lastModel} — meta "${answered.meta.slice(0, 70)}"`,
    );
    record(
      "the composer comes back after a reply: not busy, Stop gone, field cleared",
      answered.busy === "false" && answered.sendDisabled === false && answered.stopHidden === true && answered.composerEmpty === true,
      `busy=${answered.busy} send disabled=${answered.sendDisabled} stop hidden=${answered.stopHidden}`,
    );
    await page.evaluate(() => document.getElementById("chatMessages").scrollIntoView({ block: "end" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "answered");

    // ── A reply that never arrives ────────────────────────────────────────
    await page.setRequestInterception(true);
    const refuse = (request) => {
      if (request.method() === "POST" && /\/api\/chat\/stream$/.test(request.url())) {
        return request.abort("failed");
      }
      return request.continue();
    };
    page.on("request", refuse);
    await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      input.value = "and now?";
      input.dispatchEvent(new Event("input"));
      input.focus();
    });
    const beforeFailure = await page.evaluate(() => chatHistory.length);
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 12);
    page.off("request", refuse);
    const failed = await page.evaluate(() => {
      const box = document.querySelector("#chatState .presentation");
      return {
        history: chatHistory.length,
        kind: box ? box.dataset.kind : null,
        cause: box && box.querySelector(".presentation-cause") ? box.querySelector(".presentation-cause").textContent.trim() : null,
        context: box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim()) : [],
        sendDisabled: document.getElementById("chatSendBtn").disabled,
        stopHidden: document.getElementById("chatStopBtn").style.display === "none",
        lastRole: [...document.querySelectorAll("#chatMessages .msg")].pop()?.dataset.role || null,
        emptyPlaceholder: chatHistory.some((m) => m.role === "assistant" && !String(m.content || "").trim()),
      };
    });
    record(
      "a reply that never arrives is presented with its cause and a way back",
      failed.kind === "error" && Boolean(failed.cause) && failed.actions.includes("Retry") && /kept/i.test(failed.context || ""),
      `${failed.kind} — cause "${String(failed.cause).slice(0, 70)}" actions ${failed.actions.join(", ")}`,
    );
    record(
      "the failed turn keeps the message, drops the empty placeholder, and frees the composer",
      failed.history === beforeFailure + 1
        && failed.emptyPlaceholder === false
        && failed.lastRole === "user"
        && failed.sendDisabled === false
        && failed.stopHidden === true,
      `${beforeFailure} -> ${failed.history} messages, placeholder=${failed.emptyPlaceholder}, last role ${failed.lastRole}`,
    );
    await page.evaluate(() => document.getElementById("chatState").scrollIntoView({ block: "center" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "failure");

    // ── Narrow ────────────────────────────────────────────────────────────
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const box = document.getElementById("chatMessages");
      const hint = document.getElementById("chatComposerHint");
      return {
        overflow: box ? box.scrollWidth > box.clientWidth + 1 : true,
        hintHeight: hint ? Math.round(hint.getBoundingClientRect().height) : 0,
        hintVisible: hint ? hint.getBoundingClientRect().height > 8 : false,
      };
    });
    record(
      "the transcript and the composer hint survive a 900px viewport",
      narrow.overflow === false && narrow.hintVisible === true,
      `overflow=${narrow.overflow} hint=${narrow.hintHeight}px`,
    );
    await shoot(page, "narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    // The walk wrote a transcript into a page it then closed; the store was never
    // touched, so there is nothing to put back beyond the demo it started against.
    await browser.close();
  }

  const blocked = results.filter((row) => row.status === "blocked");
  const failed = results.filter((row) => row.status === "fail");
  const passed = results.filter((row) => row.status === "pass");
  if (args.json) {
    console.log(JSON.stringify({ results, passed: passed.length, failed: failed.length, blocked: blocked.length }, null, 2));
  } else {
    console.log(
      `\n${passed.length} of ${results.length} checks passed`
      + (blocked.length ? `, ${blocked.length} blocked on a prerequisite` : ""),
    );
  }
  if (failed.length > 0) {
    throw new Error(`${failed.length} chat check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`chat_walk: ${error.message}`);
  process.exit(1);
});
