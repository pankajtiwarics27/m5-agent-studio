#!/usr/bin/env node
/**
 * Check that Chat reads as a conversation, not as a full-width ribbon (UIV2-18).
 *
 *   node scripts/chat_layout_walk.mjs --screenshots docs/media/chat-layout
 *
 * The approved target asks for a readable column, compact model/persona controls, a composer that
 * stays visible, and no dead whitespace. Chat had none of it: at 1640px the transcript used the
 * whole 1,300px of workspace, so an answer line ran far past the point where an eye can track it
 * and the rest of the row was empty. The three pickers and two checkboxes wrapped onto a second
 * row, pushing the first word of the conversation down the screen. Nothing was *clipped* - a bubble
 * that wide simply never had to break - so the clipping rules had never been exercised either.
 *
 * What it proves:
 *
 *  1. the conversation is one column of 720-900px, centred in the pane, and the composer shares
 *     that column's left edge rather than the pane's;
 *  2. Chat reserves no telemetry rail: the rail is gone and the grid has one track, so the pane is
 *     the width the reader paid for;
 *  3. the model, persona and reasoning controls fit on one row above 1200px;
 *  4. the empty state is one short sentence, and it is the same sentence the markup ships;
 *  5. a real reply arrives, is attributed to the model that was asked, and never leaves the column;
 *  6. Enter sends and Shift+Enter adds a line (the composer keeps the keyboard contract it had);
 *  7. a long reply scrolls inside the transcript, and a long code block or an unbroken token
 *     scrolls inside its bubble - the page itself never scrolls sideways;
 *  8. the composer is on screen at the start, mid-stream, at the end of the longest reply, and after
 *     a failure - and stopping a stream hands it back;
 *  9. a provider that cannot answer is reported in its own turn, and the pane recovers in the same
 *     column when a model that can answer is asked instead;
 * 10. at 390x844 nothing overflows sideways and the same column rules hold at phone width;
 * 11. no console error, and the pane posts only the chat stream.
 *
 * Usage:
 *
 *   node scripts/chat_layout_walk.mjs                                   # starts its own fixtures
 *   node scripts/chat_layout_walk.mjs --base-url URL --token TOKEN      # use a running app
 *   node scripts/chat_layout_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/chat_layout_walk.mjs --states empty,populated          # a subset
 *   node scripts/chat_layout_walk.mjs --capture-only                    # write a "before" set
 *
 * Without `--base-url` it starts `scripts/council_readiness_seed.py` three times: once with two
 * loaded local models, once with one of them slow enough to photograph mid-stream, and once with a
 * model whose reply is long enough to scroll.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

const REQUEST = "How does a durable queue keep its ordering when one consumer restarts?";
const FOLLOW_UP = "And what breaks first under load?";
/** Wide enough to need a scrollbar inside its own bubble, in any theme. */
const LONG_TOKEN = "queue-" + "1234567890".repeat(30);

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
  viewport: "1440x900",
  theme: "dark",
  states: "",
};

/** The groups `--states` can select. */
const STATES = ["empty", "populated", "long", "loading", "error", "recovery"];

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false, captureOnly: false };
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
      case "--screenshots": args.screenshots = next(); break;
      case "--viewport": args.viewport = next(); break;
      case "--theme": args.theme = next(); break;
      case "--json": args.json = true; break;
      case "--capture-only": args.captureOnly = true; break;
      case "--states": args.states = next(); break;
      case "--help":
      case "-h":
        console.log("usage: node scripts/chat_layout_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states empty,populated,long,loading,error,recovery] [--json] [--capture-only]");
        process.exit(0);
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

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
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) throw new Error("no Chromium found: set STUDIO_SCREENSHOT_CHROME to one");
  return found;
}

async function loadPuppeteer() {
  const attempts = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    "puppeteer",
    resolve("tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const failures = [];
  for (const attempt of attempts) {
    try {
      const module = await import(attempt);
      return module.default || module;
    } catch (error) {
      failures.push(`${attempt}: ${error.message}`);
    }
  }
  throw new Error(`puppeteer-core is not importable. Install it or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (error) { /* not up yet */ }
    await sleep(150);
  }
  return false;
}

async function startFixture(extraArgs = []) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", "Qwen3.6-35B-REAP-19B", "--port", "0", ...extraArgs],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)), 90000);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        resolveReady(JSON.parse(line));
      } catch (error) {
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { child, ready };
}

/**
 * Everything the walk needs to know about the pane's geometry, read from the rendered page: the
 * column the conversation sits in, where the composer is, what each bubble measures, and whether
 * anything - page, transcript, code block - is wider than the thing that holds it.
 */
function readPane(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const box = document.getElementById("chatMessages");
    const composer = document.querySelector(".chat-composer");
    const header = document.querySelector(".chat-header-bar");
    const input = document.getElementById("chatInput");
    const rail = document.getElementById("monitorRail");
    const layout = document.getElementById("mainLayout");
    const empty = box ? box.querySelector(".chat-empty-state") : null;
    const rectOf = (element) => (element ? element.getBoundingClientRect() : null);
    const padOf = (element) => {
      if (!element) return { left: 0, right: 0 };
      const computed = getComputedStyle(element);
      return { left: parseFloat(computed.paddingLeft), right: parseFloat(computed.paddingRight) };
    };
    const boxRect = rectOf(box);
    const boxPad = padOf(box);
    const composerRect = rectOf(composer);
    const composerPad = padOf(composer);
    const inputRect = rectOf(input);
    const nodes = [...(box ? box.querySelectorAll(".msg") : [])].map((node) => {
      const rect = node.getBoundingClientRect();
      return {
        role: node.dataset.role || "",
        state: node.dataset.state || "",
        streaming: node.dataset.streaming === "true",
        left: Math.round(rect.left),
        right: Math.round(rect.right),
        width: Math.round(rect.width),
        text: clean(node.textContent).slice(0, 160),
        meta: clean((node.querySelector(".msg-meta") || {}).textContent || ""),
        preCount: node.querySelectorAll("pre").length,
      };
    });
    const pres = [...(box ? box.querySelectorAll(".msg pre") : [])].map((pre) => ({
      overflowX: getComputedStyle(pre).overflowX,
      scrollWidth: pre.scrollWidth,
      clientWidth: pre.clientWidth,
      right: Math.round(pre.getBoundingClientRect().right),
      left: Math.round(pre.getBoundingClientRect().left),
    }));
    // How many rows the controls occupy. Counting distinct tops would call a centred row of
    // different-height controls "four rows", so this groups children by the vertical band they
    // occupy: a child starts a new row only when it begins below everything already placed.
    const headerRows = (() => {
      if (!header) return 0;
      const boxes = [...header.children]
        .map((child) => child.getBoundingClientRect())
        .filter((box) => box.width > 0)
        .sort((a, b) => a.top - b.top);
      let rows = 0;
      let reached = -Infinity;
      for (const box of boxes) {
        if (box.top >= reached - 1) {
          rows += 1;
          reached = box.bottom;
        } else {
          reached = Math.max(reached, box.bottom);
        }
      }
      return rows;
    })();
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      pageOverflowX: document.documentElement.scrollWidth - window.innerWidth,
      column: {
        left: Math.round(boxRect.left + boxPad.left),
        right: Math.round(boxRect.right - boxPad.right),
        width: Math.round(boxRect.width - boxPad.left - boxPad.right),
        padLeft: boxPad.left,
        padRight: boxPad.right,
      },
      box: boxRect ? {
        top: Math.round(boxRect.top),
        bottom: Math.round(boxRect.bottom),
        scrollHeight: box.scrollHeight,
        clientHeight: box.clientHeight,
        overflowX: box.scrollWidth - box.clientWidth,
      } : null,
      composer: composerRect ? {
        top: Math.round(composerRect.top),
        bottom: Math.round(composerRect.bottom),
        left: Math.round(composerRect.left + composerPad.left),
        position: getComputedStyle(composer).position,
      } : null,
      input: inputRect ? {
        top: Math.round(inputRect.top),
        bottom: Math.round(inputRect.bottom),
        left: Math.round(inputRect.left),
        width: Math.round(inputRect.width),
        value: input.value,
        visible: inputRect.top >= 0 && inputRect.bottom <= window.innerHeight + 1 && inputRect.height > 0,
      } : null,
      header: header ? {
        rows: headerRows,
        height: Math.round(header.getBoundingClientRect().height),
        controls: [...header.querySelectorAll("select, input[type=checkbox], button")].map((node) => node.id).filter(Boolean),
      } : null,
      presentation: (() => {
        const host = document.getElementById("chatState");
        if (!host || !String(host.textContent || "").trim()) return null;
        return {
          text: clean(host.textContent).slice(0, 300),
          actions: [...host.querySelectorAll("button")].map((button) => clean(button.textContent)),
        };
      })(),
      history: {
        turns: typeof chatHistory === "undefined" ? -1 : chatHistory.length,
        roles: typeof chatHistory === "undefined" ? [] : chatHistory.map((message) => `${message.role}${message.status ? `:${message.status}` : ""}`),
      },
      rail: rail ? { display: getComputedStyle(rail).display, width: Math.round(rail.getBoundingClientRect().width) } : null,
      grid: layout ? getComputedStyle(layout).gridTemplateColumns : "",
      workspaceWidth: Math.round((document.querySelector(".workspace") || { getBoundingClientRect: () => ({ width: 0 }) }).getBoundingClientRect().width),
      sendDisabled: Boolean(document.getElementById("chatSendBtn") && document.getElementById("chatSendBtn").disabled),
      stopVisible: Boolean(document.getElementById("chatStopBtn")) && getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
      streaming: nodes.some((node) => node.streaming),
      empty: empty ? { text: clean(empty.textContent), length: clean(empty.textContent).length } : null,
      summary: clean((document.getElementById("chatTurnSummary") || {}).textContent || ""),
      messages: nodes,
      pres,
    };
  });
}

/** A transcript written into the page, in the shape `sendChatRequest()` builds. */
const seedTranscript = (page, messages) => page.evaluate((rows) => {
  chatHistory = rows.map((row, index) => ({
    role: row.role,
    model: row.model || "",
    content: row.content,
    reasoning: "",
    tools: [],
    usage: row.role === "assistant" ? { decode_tps: 41.2, completion_tokens: content_words(row.content) } : null,
    status: row.role === "assistant" ? "complete" : "",
    createdAt: Date.now() + index,
    context: null,
    timing: null,
  }));
  renderChatMessages();
  return chatHistory.length;
  // The count is the number of words the reply carries: the same figure the pane's own meta line
  // reads, so the seeded turn is not a different shape from a streamed one.
  function content_words(text) {
    return String(text || "").split(/\s+/).filter(Boolean).length;
  }
}, messages);

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const [width, height] = args.viewport.split("x").map(Number);
  if (args.states) {
    const unknown = args.states.split(",").map((name) => name.trim()).filter(Boolean)
      .filter((name) => !STATES.includes(name));
    if (unknown.length) {
      console.error(`unknown state(s): ${unknown.join(", ")} — this walk knows ${STATES.join(", ")}`);
      process.exit(2);
    }
  }
  const wanted = args.states ? new Set(args.states.split(",").map((name) => name.trim()).filter(Boolean)) : null;
  const state = (name) => !wanted || wanted.has(name);

  const shots = [];
  const shoot = async (page, name, focus) => {
    if (!args.screenshots) return "";
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    if (focus) {
      await page.evaluate((selector) => {
        const host = document.querySelector(selector);
        if (host && host.scrollIntoView) host.scrollIntoView({ block: "start", inline: "nearest" });
      }, focus);
    }
    await sleep(args.settleMs * 2);
    await page.screenshot({ path });
    shots.push(path);
    return path;
  };

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" ").filter(Boolean),
  });
  const pageErrors = [];
  const posts = [];
  const fixtures = [];

  const openPage = async (base, authToken) => {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 40[34]/.test(message.text())) pageErrors.push(message.text());
    });
    page.on("request", (request) => { if (request.method() === "POST") posts.push(new URL(request.url()).pathname); });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    const paneOrigin = new URL(base).origin;
    await page.evaluateOnNewDocument((token, key, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, token);
      localStorage.setItem("m5_theme", theme);
    }, authToken, args.tokenKey, args.theme, paneOrigin);
    await page.goto(paneOrigin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 10);
    await page.evaluate(() => switchTab("chat"));
    await page.waitForFunction(() => {
      const select = document.getElementById("chatModel");
      return Boolean(select) && select.options.length > 0;
    }, { timeout: 30000 }).catch(() => {});
    await sleep(args.settleMs * 4);
    return page;
  };

  /** Start a fixture (unless one was given), open the pane on it, and clean both up after. */
  const withFixture = async (extraArgs, work) => {
    const fixture = args.baseUrl ? null : await startFixture(extraArgs);
    if (fixture) fixtures.push(fixture);
    const base = args.baseUrl || fixture.ready.base_url;
    const token = args.token || (fixture && fixture.ready.token) || "";
    const page = await openPage(base, token);
    try {
      return await work(page, fixture ? fixture.ready : { base_url: base, token });
    } finally {
      await page.close().catch(() => {});
      if (fixture) {
        fixture.child.kill();
        fixtures.splice(fixtures.indexOf(fixture), 1);
      }
    }
  };

  const options = (page) => page.evaluate(() => [...document.querySelectorAll("#chatModel option")]
    .map((option) => ({ value: option.value, label: (option.textContent || "").trim() })));
  const localsOf = (list) => list.map((option) => option.value).filter((value) => value && !value.includes("::") && !value.startsWith("combo/") && value !== "__custom__");

  const pick = (page, model) => page.evaluate((choice) => {
    const select = document.getElementById("chatModel");
    select.value = choice;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return select.value;
  }, model);

  const send = (page, text) => page.evaluate((message) => {
    const input = document.getElementById("chatInput");
    input.value = message;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("chatSendBtn").click();
    return chatHistory.length;
  }, text);

  /** Wait until nothing is streaming and the composer is idle again. */
  const settle = async (page, limitMs = 90000) => {
    const started = Date.now();
    while (Date.now() - started < limitMs) {
      await sleep(300);
      const done = await page.evaluate(() => !chatBusy
        && !document.querySelector('#chatMessages .msg[data-streaming="true"]')
        && getComputedStyle(document.getElementById("chatStopBtn")).display === "none");
      if (done) return true;
    }
    return false;
  };

  try {
    // ── 1-5: nothing has been said, and the pane is a column ──────────────────
    if (state("empty")) await withFixture([], async (page) => {
      const empty = await readPane(page);
      const wide = empty.innerWidth >= 1440;
      record(
        "the conversation is one readable column, centred in the pane",
        Math.abs(empty.column.padLeft - empty.column.padRight) <= 2
          && empty.column.width <= 900
          && (!wide || empty.column.width >= 720),
        `${empty.column.width}px column in ${empty.innerWidth}px, padding ${empty.column.padLeft}/${empty.column.padRight}`,
      );
      record(
        "the composer sits on that column's left edge, not the pane's",
        Boolean(empty.composer) && Math.abs(empty.composer.left - empty.column.left) <= 1,
        `composer ${empty.composer ? empty.composer.left : "?"} vs column ${empty.column.left}`,
      );
      record(
        "the composer is on screen and the empty state is one short sentence",
        Boolean(empty.input && empty.input.visible) && Boolean(empty.empty) && empty.empty.length <= 150,
        `input ${empty.input ? `${empty.input.top}-${empty.input.bottom} of ${empty.innerHeight}` : "missing"} · empty "${empty.empty ? empty.empty.text.slice(0, 90) : ""}"`,
      );
      record(
        "Chat keeps no telemetry rail, so the pane is the width the reader has",
        Boolean(empty.rail) && (empty.rail.display === "none" || empty.rail.width === 0)
          && !/320px/.test(empty.grid),
        `rail ${empty.rail ? `${empty.rail.display} ${empty.rail.width}px` : "missing"} · grid ${empty.grid}`,
      );
      record(
        "the model, persona and reasoning controls are one compact row",
        Boolean(empty.header)
          && (empty.innerWidth >= 1200
            ? empty.header.rows === 1 && empty.header.height <= 84
            : empty.header.rows <= 3 && empty.header.height <= 220),
        `${empty.header ? empty.header.rows : "?"} row(s), ${empty.header ? empty.header.height : "?"}px tall · ${empty.header ? empty.header.controls.join(",") : ""}`,
      );
      await shoot(page, `chat-empty-${args.viewport}-${args.theme}`);

      // ── 11: the same rules at phone width ──────────────────────────────────
      await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 6);
      const narrow = await readPane(page);
      record(
        "at 390x844 nothing overflows sideways and the composer is still on screen",
        narrow.pageOverflowX <= 0 && Boolean(narrow.input && narrow.input.visible) && narrow.column.width <= 390,
        `page ${narrow.pageOverflowX} · column ${narrow.column.width} · input ${narrow.input ? narrow.input.bottom : "?"}/${narrow.innerHeight}`,
      );
      await page.setViewport({ width, height, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 4);
    });

    // ── 6-8: a real conversation ─────────────────────────────────────────────
    if (state("populated")) await withFixture([], async (page) => {
      const catalogue = await options(page);
      const local = localsOf(catalogue)[0];
      record(
        "a local model is in the catalogue, so a reply can be photographed",
        Boolean(local),
        `${catalogue.length} option(s) · picked ${local || "none"}`,
      );
      await pick(page, local);
      await send(page, REQUEST);
      const finished = await settle(page);
      const answered = await readPane(page);
      const reply = answered.messages.filter((message) => message.role === "assistant").pop();
      record(
        "a real reply arrives, attributed to the model that was asked",
        finished && Boolean(reply) && reply.state === "complete" && reply.meta.includes(local),
        reply ? `${reply.state} · "${reply.meta}"` : "no assistant turn",
      );
      record(
        "no bubble leaves the column, and the page never scrolls sideways",
        answered.pageOverflowX <= 0
          && answered.messages.every((message) => message.right <= answered.column.right + 1 && message.width <= answered.column.width + 1),
        `page ${answered.pageOverflowX} · widest ${Math.max(...answered.messages.map((message) => message.width), 0)} of ${answered.column.width}`,
      );

      // Enter sends, Shift+Enter does not: the contract the composer's own hint promises.
      const keyboard = await page.evaluate(async (text) => {
        const input = document.getElementById("chatInput");
        input.focus();
        const before = chatHistory.length;
        input.value = text;
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true }));
        const afterShift = { value: input.value, history: chatHistory.length };
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        await new Promise((done) => setTimeout(done, 200));
        return { before, afterShift, afterEnter: chatHistory.length, focused: document.activeElement === input || document.activeElement === document.body };
      }, FOLLOW_UP);
      record(
        "Enter sends and Shift+Enter adds a line",
        keyboard.afterShift.history === keyboard.before
          && keyboard.afterShift.value === FOLLOW_UP
          && keyboard.afterEnter > keyboard.before,
        `shift kept ${keyboard.afterShift.history} turns and the newline · enter → ${keyboard.afterEnter}`,
      );
      await settle(page);
      const scrolled = await readPane(page);
      record(
        "the transcript scrolls inside the pane, and the composer is still on screen",
        Boolean(scrolled.box) && scrolled.box.scrollHeight >= scrolled.box.clientHeight
          && Boolean(scrolled.input && scrolled.input.visible),
        `transcript ${scrolled.box ? `${scrolled.box.scrollHeight}/${scrolled.box.clientHeight}` : "?"} · input ${scrolled.input ? scrolled.input.bottom : "?"}/${scrolled.innerHeight}`,
      );
      await shoot(page, `chat-populated-${args.viewport}-${args.theme}`);
    });

    // ── 9-10: long content, and code that has to scroll ──────────────────────
    if (state("long")) await withFixture(["--long-model", "Qwen3.8-27B-oQ4e-mtp"], async (page) => {
      const catalogue = await options(page);
      const local = localsOf(catalogue).find((value) => value === "Qwen3.8-27B-oQ4e-mtp") || localsOf(catalogue)[0];
      await pick(page, local);
      await send(page, REQUEST);
      await settle(page);
      const long = await readPane(page);
      record(
        "a long reply scrolls inside the transcript, not the page",
        Boolean(long.box) && long.box.scrollHeight > long.box.clientHeight + 40 && long.pageOverflowX <= 0,
        `transcript ${long.box ? `${long.box.scrollHeight}/${long.box.clientHeight}` : "?"} · page ${long.pageOverflowX}`,
      );

      // A code block wide enough to need its own scrollbar, written through the pane's own renderer.
      const seeded = await seedTranscript(page, [
        { role: "user", content: "Show me the retry loop." },
        { role: "assistant", model: local, content: "Here it is:\n\n```js\n" + LONG_TOKEN + "\n```\n\nThat token is one identifier." },
        { role: "user", content: "And the unbroken token on its own?" },
        { role: "assistant", model: local, content: LONG_TOKEN },
      ]);
      record("a code block can be written into the pane to measure it", seeded === 4, `${seeded} turns`);
      const coded = await readPane(page);
      const pre = coded.pres[0];
      record(
        "a long code block scrolls inside its bubble, never off the pane",
        Boolean(pre) && pre.overflowX === "auto" && pre.scrollWidth > pre.clientWidth
          && pre.right <= coded.column.right + 1 && coded.pageOverflowX <= 0,
        pre ? `pre ${pre.scrollWidth}/${pre.clientWidth} · ${pre.overflowX} · right ${pre.right} of ${coded.column.right}` : "no pre",
      );
      record(
        "an unbroken token does not widen its bubble",
        coded.pageOverflowX <= 0 && coded.messages.every((message) => message.width <= coded.column.width + 1),
        `page ${coded.pageOverflowX} · widest ${Math.max(...coded.messages.map((message) => message.width), 0)} of ${coded.column.width}`,
      );
      record(
        "the composer is still on screen at the end of the longest transcript",
        Boolean(coded.input && coded.input.visible) && Boolean(coded.composer && coded.composer.bottom <= coded.innerHeight + 1),
        `input ${coded.input ? coded.input.top : "?"}-${coded.input ? coded.input.bottom : "?"} of ${coded.innerHeight} · composer ${coded.composer ? coded.composer.position : "?"}`,
      );
      await shoot(page, `chat-long-content-${args.viewport}-${args.theme}`);
    });

    // ── 12-14: a reply in progress ───────────────────────────────────────────
    if (state("loading")) await withFixture(["--slow-model", "Qwen3.8-27B-oQ4e-mtp"], async (page, ready) => {
      const catalogue = await options(page);
      const slow = localsOf(catalogue).find((value) => value === ready.slow_model) || localsOf(catalogue)[0];
      await pick(page, slow);
      await send(page, REQUEST);
      let streaming = null;
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await sleep(250);
        const pane = await readPane(page);
        if (pane.streaming) { streaming = pane; break; }
      }
      record(
        "a reply in progress is drawn as in progress, with Stop offered",
        Boolean(streaming) && streaming.stopVisible,
        streaming ? `${streaming.messages.filter((message) => message.streaming).length} streaming · stop ${streaming.stopVisible}` : "never saw a streaming turn",
      );
      record(
        "the composer stays on screen while the reply streams",
        Boolean(streaming && streaming.input && streaming.input.visible),
        streaming && streaming.input ? `input ${streaming.input.top}-${streaming.input.bottom} of ${streaming.innerHeight}` : "no measurement",
      );
      await shoot(page, `chat-loading-${args.viewport}-${args.theme}`);
      const stopped = await page.evaluate(async () => {
        document.getElementById("chatStopBtn").click();
        await new Promise((done) => setTimeout(done, 1200));
        const last = [...document.querySelectorAll('#chatMessages .msg[data-role="assistant"]')].pop();
        return {
          state: last ? last.dataset.state : "",
          text: (last ? last.textContent : "").replace(/\s+/g, " ").trim().slice(0, 120),
          sendDisabled: document.getElementById("chatSendBtn").disabled,
          stopVisible: getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
        };
      });
      record(
        "stopping keeps the turn it had and hands the composer back",
        stopped.state === "stopped" && stopped.sendDisabled === false && stopped.stopVisible === false,
        `${stopped.state} · send disabled ${stopped.sendDisabled} · "${stopped.text}"`,
      );
    });

    // ── 15-16: a provider that cannot answer ─────────────────────────────────
    if (state("error")) await withFixture([], async (page) => {
      const catalogue = await options(page);
      const keyless = catalogue.map((option) => option.value).find((value) => value.includes("::"));
      const before = await readPane(page);
      record(
        "a provider that will fail can be picked, so a failure can be shown honestly",
        Boolean(keyless),
        keyless || "no keyless provider row",
      );
      await pick(page, keyless);
      await send(page, REQUEST);
      await settle(page);
      const failed = await readPane(page);
      const last = failed.messages.filter((message) => message.role === "assistant").pop();
      // The pane keeps a failed *bubble* only when something arrived before the failure; a provider
      // that refuses before its first token leaves no half-empty answer behind, and the failure is
      // reported above the composer with the message still in the transcript.
      record(
        "a provider that cannot answer is reported above the composer, with a retry",
        Boolean(failed.presentation)
          && /reply stopped|refused|rate window|signed in/i.test(failed.presentation.text)
          && failed.presentation.actions.some((action) => /retry/i.test(action)),
        failed.presentation ? `"${failed.presentation.text.slice(0, 120)}" · ${failed.presentation.actions.join(",")}` : "no presentation",
      );
      record(
        "the message the reader wrote is kept, and no empty answer bubble is left",
        failed.history.roles.filter((role) => role.startsWith("user")).length >= 1
          && failed.history.roles.every((role) => !role.startsWith("assistant"))
          && Boolean(last) === false && !failed.streaming,
        `turns ${failed.history.roles.join(" ")}${last ? ` · last ${last.state}` : ""}`,
      );
      record(
        "the failure leaves the column and the composer intact",
        failed.pageOverflowX <= 0
          && Math.abs(failed.column.width - before.column.width) <= 1
          && failed.sendDisabled === false
          && Boolean(failed.input && failed.input.visible),
        `column ${failed.column.width} of ${before.column.width} · send disabled ${failed.sendDisabled} · page ${failed.pageOverflowX}`,
      );
      await shoot(page, `chat-error-${args.viewport}-${args.theme}`);

      // ── 17: the same pane, answering again ────────────────────────────────
      if (state("recovery")) {
        const local = localsOf(catalogue)[0];
        await pick(page, local);
        await send(page, FOLLOW_UP);
        const finished = await settle(page);
        const recovered = await readPane(page);
        const reply = recovered.messages.filter((message) => message.role === "assistant").pop();
        record(
          "the same pane answers again after a failure, in the same column",
          finished && Boolean(reply) && reply.state === "complete"
            && Math.abs(recovered.column.width - before.column.width) <= 1,
          reply ? `${reply.state} · column ${recovered.column.width} of ${before.column.width}` : "no reply after the failure",
        );
        await shoot(page, `chat-recovery-${args.viewport}-${args.theme}`);
      }
    });

    // ── 18-19: nothing else happened ─────────────────────────────────────────
    record(
      "the pane raises no console error",
      pageErrors.length === 0,
      pageErrors.slice(0, 2).join(" | "),
    );
    const unexpected = [...new Set(posts)].filter((path) => path !== "/api/chat/stream");
    // A subset run that only photographs the empty pane never sends, so it cannot prove the call
    // site; a run that does send has to post exactly one route.
    const sends = ["populated", "long", "loading", "error", "recovery"];
    const sendsHere = !wanted || sends.some((name) => wanted.has(name));
    record(
      "the pane posts only the chat stream",
      unexpected.length === 0 && (!sendsHere || posts.length > 0),
      [...new Set(posts)].join(" · ") || (sendsHere ? "no POST" : "no send in this subset"),
    );
  } finally {
    for (const fixture of fixtures.slice()) {
      fixture.child.kill();
    }
    await browser.close().catch(() => {});
  }

  const passed = results.filter((result) => result.passed).length;
  if (args.json) {
    console.log(JSON.stringify({ viewport: args.viewport, theme: args.theme, passed, total: results.length, screenshots: shots, results }, null, 2));
  } else {
    console.log(`\n${passed}/${results.length} checks passed · ${shots.length} screenshots`);
  }
  return results.every((result) => result.passed) ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  });
