#!/usr/bin/env node
/**
 * Check Chat's handoff to the Harness in a browser (UIUX-P2-06).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/chat_handoff_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The demo has no model runtime, so this walk cannot make a model produce the
 * conversation it hands over. It does the next honest thing: it writes a transcript of
 * the shape sendChatRequest() builds - a task, a reply with its recorded context, ten
 * tool calls of which one failed - and then presses the real button against the real
 * Harness composer.
 *
 * What it proves:
 *
 *   1. with nothing to hand over, the button is off and the sentence says why, and
 *      pressing it anyway carries nothing and moves the cursor back to the composer;
 *   2. with a conversation, the sentence counts what it will carry - messages, replies,
 *      tool calls, failures - and the button is on;
 *   3. pressing it posts nothing: every request the page makes afterwards is recorded
 *      and none of them goes to an API route;
 *   4. the Harness composer receives a brief built from the turns - the task, the
 *      counts, the recorded context line, each call with its status word, step, time and
 *      output, the bound it applied and the server's cap - and says nothing has run;
 *   5. the brief lists at most eight calls and says how many it left out;
 *   6. the reader lands in the Harness with the cursor in the composer;
 *   7. the Chat pane leaves a receipt with the counts, the kept-state line and
 *      Back to Chat / Open Harness;
 *   8. Back to Chat returns the cursor to the composer with the transcript unchanged,
 *      and the sentence then says this conversation is already in the composer;
 *   9. a handoff that cannot happen - the composer removed from the page - presents an
 *      error with its cause and a Retry, keeps the transcript, and Retry works once the
 *      composer is back;
 *  10. the button is keyboard-operable, the sentence survives a 900px viewport, and no
 *      page error occurs.
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
    "Check Chat's handoff to the Harness against the composer it fills.",
    "",
    "  node scripts/chat_handoff_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-chat-handoff.md.",
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
    await page.evaluate(() => {
      const sel = document.getElementById("chatModel");
      sel.innerHTML = '<option value="demo-local-model">demo-local-model</option>';
      sel.value = "demo-local-model";
      updateChatComposer();
    });

    // ── Nothing to hand over ──────────────────────────────────────────────
    const empty = await page.evaluate(() => {
      chatHistory = [];
      renderChatMessages();
      const button = document.getElementById("chatHandoffBtn");
      const hint = document.getElementById("chatHandoffHint");
      const before = document.getElementById("harnessPromptInput").value;
      const carried = handOffChatToHarness();
      return {
        disabled: button.disabled,
        reason: hint.dataset.reason,
        sentence: hint.textContent.trim(),
        live: hint.getAttribute("aria-live"),
        role: hint.getAttribute("role"),
        describedBy: button.getAttribute("aria-describedby"),
        carried,
        harnessBefore: before,
        harnessAfter: document.getElementById("harnessPromptInput").value,
        focusInComposer: document.activeElement === document.getElementById("chatInput"),
      };
    });
    record(
      "with nothing to hand over the button is off and the sentence says why",
      empty.disabled === true
        && empty.reason === "nothing"
        && /send a message/i.test(empty.sentence)
        && empty.live === "polite"
        && empty.role === "status"
        && empty.describedBy === "chatHandoffHint",
      `reason=${empty.reason} disabled=${empty.disabled} — ${empty.sentence.slice(0, 80)}`,
    );
    record(
      "pressing it anyway carries nothing and keeps the cursor in the composer",
      empty.carried === null && empty.harnessAfter === empty.harnessBefore && empty.focusInComposer === true,
      `returned=${empty.carried} harness empty=${empty.harnessAfter === ""} focus in composer=${empty.focusInComposer}`,
    );

    // ── A conversation worth handing over ─────────────────────────────────
    const written = await page.evaluate(() => {
      const okCall = (index) => ({
        step: 1,
        tool: index === 0 ? "grep_search" : `read_file_${index}`,
        args: { path: `src/file_${index}.py` },
        ok: true,
        elapsed_ms: 40 + index,
        preview: `src/file_${index}.py:1: found the entry point`,
      });
      const calls = Array.from({ length: 9 }, (_, index) => okCall(index));
      calls.push({
        step: 2,
        tool: "read_file",
        args: { path: "src/missing.py" },
        ok: false,
        elapsed_ms: 7,
        preview: `Error: file not found: src/missing.py - ${"x".repeat(280)}`,
      });
      const startedAt = performance.now() - 2500;
      chatHistory = [
        { role: "user", content: "Which tools ran, and which one failed? Then hand this to the Harness." },
        {
          role: "assistant",
          model: "demo-local-model",
          content: "Ten tools ran; read_file on src/missing.py was refused by the workspace guard.",
          reasoning: "the reader asked which tool failed",
          tools: calls,
          context: chatContextSnapshot("demo-local-model", 2, ""),
          timing: { startedAt, firstTokenAt: startedAt + 400, endedAt: startedAt + 1800 },
          usage: { decode_tps: 41.2, completion_tokens: 320 },
        },
      ];
      renderChatMessages();
      const button = document.getElementById("chatHandoffBtn");
      const hint = document.getElementById("chatHandoffHint");
      const stats = chatHandoffStats();
      return {
        disabled: button.disabled,
        reason: hint.dataset.reason,
        sentence: hint.textContent.trim(),
        stats,
        expectedContext: chatContextLine(stats.context),
      };
    });
    record(
      "with a conversation the sentence counts what it will carry and the button is on",
      written.disabled === false
        && written.reason === "ready"
        && written.sentence.includes("2 messages")
        && written.sentence.includes("1 reply")
        && written.sentence.includes("10 tool calls")
        && written.sentence.includes("1 failed")
        && /nothing runs/i.test(written.sentence),
      `${written.reason}: ${written.sentence.slice(0, 120)}`,
    );

    // ── The carry, and the proof that it posts nothing ────────────────────
    const requests = [];
    const watch = (request) => requests.push({ url: request.url(), method: request.method() });
    page.on("request", watch);
    const carried = await page.evaluate(() => {
      const brief = handOffChatToHarness();
      const composer = document.getElementById("harnessPromptInput");
      const tab = document.querySelector('.mode-tab[aria-selected="true"]');
      const receipt = document.querySelector("#chatState .presentation");
      return {
        brief,
        composerValue: composer.value,
        composerFocused: document.activeElement === composer,
        tab: tab ? tab.dataset.tab : null,
        receiptKind: receipt ? receipt.dataset.kind : null,
        receiptTitle: receipt ? receipt.querySelector(".presentation-title").textContent.trim() : "",
        receiptCause: receipt ? receipt.querySelector(".presentation-cause").textContent.trim() : "",
        receiptContext: receipt ? receipt.querySelector(".presentation-context").textContent.trim() : "",
        receiptActions: receipt
          ? [...receipt.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim())
          : [],
        historyLength: chatHistory.length,
        sentenceReason: document.getElementById("chatHandoffHint").dataset.reason,
        sentence: document.getElementById("chatHandoffHint").textContent.trim(),
      };
    });
    await sleep(args.settleMs * 2);
    page.off("request", watch);
    const writes = requests.filter((row) => row.method !== "GET");
    const reads = requests.filter((row) => /\/api\//.test(row.url) && !/\/api\/(telemetry|harness\/sessions)/.test(row.url));
    record(
      "the carry writes nothing: no POST, and no read the two panes do not already make",
      writes.length === 0 && reads.length === 0,
      writes.length || reads.length
        ? [...writes, ...reads].map((row) => `${row.method} ${row.url}`).join(", ")
        : `${requests.length} request(s): the telemetry poll and the Harness's own session list, both GET`,
    );
    record(
      "the reader lands in the Harness with the cursor in the composer it filled",
      carried.tab === "harness" && carried.composerFocused === true && carried.composerValue === carried.brief,
      `tab=${carried.tab} focused=${carried.composerFocused} ${carried.composerValue.length} chars carried`,
    );

    const brief = carried.composerValue;
    record(
      "the brief is the task, the counts, and the record of what the tools did",
      brief.startsWith("Which tools ran, and which one failed?")
        && brief.includes("nothing has run")
        && brief.includes("Conversation so far: 2 messages (1 reply), 10 tool calls, 1 failed")
        && /FAILED read_file - step 2, 7 ms/.test(brief)
        && /OK read_file_4 - step 1, 44 ms/.test(brief)
        && !brief.includes("grep_search"),
      brief.split("\n").slice(0, 6).join(" / ").slice(0, 200),
    );
    record(
      "the brief carries the context the reply was sent with, not a fresh guess",
      written.expectedContext.length > 0 && brief.includes(written.expectedContext),
      written.expectedContext.slice(0, 120),
    );
    const listed = (brief.match(/^- (OK|FAILED) /gm) || []).length;
    record(
      "the brief lists at most eight calls and says how many it left out and clipped",
      listed === 8
        && brief.includes("grep_search") === false
        && brief.includes("2 earlier call(s) are in the Chat transcript")
        && brief.includes("clipped to 200 characters")
        && brief.includes("500-character cap"),
      `${listed} call(s) listed; bounds stated=${/earlier call\(s\)/.test(brief)}`,
    );
    record(
      "the receipt counts the carry, says the transcript is kept, and offers both ways on",
      carried.receiptKind === "recovery"
        && carried.receiptTitle === "Handed to the Harness"
        && /2 messages and 10 tool calls/.test(carried.receiptCause)
        && /this transcript is kept/i.test(carried.receiptContext)
        && /nothing has run/i.test(carried.receiptContext)
        && carried.receiptActions.join(",") === "Back to Chat,Open Harness",
      `${carried.receiptKind}: ${carried.receiptTitle} — ${carried.receiptActions.join(", ")}`,
    );
    record(
      "the sentence then says this conversation is already in the composer",
      carried.sentenceReason === "handed" && /Already in the Harness composer/.test(carried.sentence),
      carried.sentence.slice(0, 110),
    );
    await page.evaluate(() => {
      // Show the head of the brief rather than the tail of a two-line box.
      const box = document.getElementById("harnessPromptInput");
      box.rows = 8;
      box.scrollTop = 0;
      box.scrollIntoView({ block: "center" });
    });
    await sleep(args.settleMs * 2);
    await shoot(page, "handed-to-harness");

    // ── Back to Chat, by keyboard ─────────────────────────────────────────
    // A reader returns to Chat with the rail (the receipt lives in the pane they came
    // from), then presses Back to Chat, which is reachable there.
    await page.evaluate(() => {
      const tab = document.querySelector('.mode-tab[data-tab="chat"]');
      tab.focus();
      tab.click();
    });
    await sleep(args.settleMs * 2);
    const back = await page.evaluate(() => {
      const button = [...document.querySelectorAll("#chatState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Back to Chat");
      if (!button) return { found: false };
      button.focus();
      button.click();
      return {
        found: true,
        tab: (document.querySelector('.mode-tab[aria-selected="true"]') || {}).dataset.tab,
        receipt: Boolean(document.querySelector("#chatState .presentation")),
        focusInComposer: document.activeElement === document.getElementById("chatInput"),
        historyLength: chatHistory.length,
        harnessKept: document.getElementById("harnessPromptInput").value.length,
        turnsDrawn: document.querySelectorAll("#chatMessages .msg").length,
      };
    });
    record(
      "Back to Chat returns the cursor to the composer with the transcript unchanged",
      back.found === true
        && back.tab === "chat"
        && back.receipt === false
        && back.focusInComposer === true
        && back.historyLength === 2
        && back.turnsDrawn === 2
        && back.harnessKept === brief.length,
      `tab=${back.tab} receipt gone=${!back.receipt} focus=${back.focusInComposer} transcript=${back.historyLength}`,
    );
    await shoot(page, "back-to-chat");

    // ── A handoff that cannot happen ──────────────────────────────────────
    const failure = await page.evaluate(() => {
      const composer = document.getElementById("harnessPromptInput");
      const parent = composer.parentNode;
      composer.remove();
      const cleared = handOffChatToHarness();
      const receipt = document.querySelector("#chatState .presentation");
      const result = {
        returned: cleared,
        composerGone: document.getElementById("harnessPromptInput") === null,
        kind: receipt ? receipt.dataset.kind : null,
        cause: receipt ? receipt.querySelector(".presentation-cause").textContent.trim() : "",
        context: receipt ? receipt.querySelector(".presentation-context").textContent.trim() : "",
        actions: receipt
          ? [...receipt.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim())
          : [],
        historyLength: chatHistory.length,
        tab: (document.querySelector('.mode-tab[aria-selected="true"]') || {}).dataset.tab,
        sentenceReasons: document.getElementById("chatHandoffHint").dataset.reason,
      };
      // Put the Harness back exactly where it was, then let Retry prove the recovery.
      parent.appendChild(composer);
      return result;
    });
    record(
      "a handoff with no composer to hand to says so and keeps the conversation",
      failure.returned === null
        && failure.kind === "error"
        && /Harness composer was not found/.test(failure.cause)
        && /untouched/.test(failure.context)
        && failure.actions.join(",") === "Retry,Dismiss"
        && failure.historyLength === 2
        && failure.tab === "chat"
        && failure.sentenceReasons !== "handed",
      `${failure.kind} — ${failure.cause} (${failure.historyLength} messages kept, sentence says ${failure.sentenceReasons})`,
    );
    await shoot(page, "handoff-failed");

    await page.evaluate(() => {
      const retry = [...document.querySelectorAll("#chatState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Retry");
      if (retry) retry.focus();
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 3);
    const retried = await page.evaluate(() => ({
      composerLength: (document.getElementById("harnessPromptInput") || {}).value.length,
      kind: (document.querySelector("#chatState .presentation") || {}).dataset
        ? document.querySelector("#chatState .presentation").dataset.kind
        : null,
      tab: (document.querySelector('.mode-tab[aria-selected="true"]') || {}).dataset.tab,
    }));
    record(
      "Retry performs the handoff once the composer is back",
      retried.composerLength > 0 && retried.kind === "recovery" && retried.tab === "harness",
      `${retried.composerLength} chars, ${retried.kind}, tab=${retried.tab}`,
    );

    // ── Keyboard operation of the button itself ───────────────────────────
    const keyboard = await page.evaluate(() => {
      switchTab("chat");
      document.getElementById("harnessPromptInput").value = "";
      chatHistory = [{ role: "user", content: "Hand this one over with the keyboard." }];
      renderChatMessages();
      const button = document.getElementById("chatHandoffBtn");
      button.focus();
      return { focused: document.activeElement === button, disabled: button.disabled };
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 3);
    const keyboardResult = await page.evaluate(() => ({
      composer: document.getElementById("harnessPromptInput").value,
      tab: (document.querySelector('.mode-tab[aria-selected="true"]') || {}).dataset.tab,
      focusInComposer: document.activeElement === document.getElementById("harnessPromptInput"),
    }));
    record(
      "the button is keyboard-operable and does the same thing from the keyboard",
      keyboard.focused === true
        && keyboard.disabled === false
        && /Hand this one over with the keyboard\./.test(keyboardResult.composer)
        && keyboardResult.tab === "harness"
        && keyboardResult.focusInComposer === true,
      `focused=${keyboard.focused} tab=${keyboardResult.tab} composer has the task=${/keyboard/.test(keyboardResult.composer)}`,
    );

    // ── Narrow ────────────────────────────────────────────────────────────
    await page.evaluate(() => {
      switchTab("chat");
      renderChatMessages();
    });
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const bar = document.querySelector(".chat-transcript-bar");
      const hint = document.getElementById("chatHandoffHint");
      const button = document.getElementById("chatHandoffBtn");
      const box = hint.getBoundingClientRect();
      return {
        overflow: bar ? bar.scrollWidth > bar.clientWidth + 1 : true,
        hintVisible: box.height > 8 && box.width > 40,
        buttonVisible: button.getBoundingClientRect().width > 40,
        oneLine: box.height < 30,
      };
    });
    record(
      "the sentence and the button survive a 900px viewport on one line",
      narrow.overflow === false && narrow.hintVisible && narrow.buttonVisible && narrow.oneLine,
      `overflow=${narrow.overflow} hint ${narrow.hintVisible} button ${narrow.buttonVisible} one line=${narrow.oneLine}`,
    );
    await shoot(page, "handoff-narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    // The walk wrote a transcript into a page it then closed; nothing was created on the
    // server, which is the point of the story.
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
    throw new Error(`${failed.length} handoff check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`chat_handoff_walk: ${error.message}`);
  process.exit(1);
});
