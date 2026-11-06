#!/usr/bin/env node
/**
 * Check that Chat's persona picker is drawn from the registry, and that what it shows is what it
 * sends (UIV2-19).
 *
 *   node scripts/chat_persona_walk.mjs --screenshots docs/media/chat-persona
 *
 * The pane shipped three personas as `<option value="...">` text in the markup. The picker *was* the
 * contract, which meant nothing could describe a persona, nothing survived a reload, the value the
 * request carried was a 130-character paragraph nobody could read back, and a persona deleted from
 * the markup would silently become whichever option happened to be first. `CHAT_PERSONAS` is now the
 * authority; this walk holds the screen and the wire against it.
 *
 * What it proves:
 *
 *  1. every persona in the registry is in the picker under its own label, and nothing is offered
 *     that the registry does not name;
 *  2. the pane opens on the default persona and says, in one clamped line, what that persona does;
 *  3. the filter narrows by label or description without ever dropping the selected persona, says
 *     so when it matches nothing else, clears on Escape, and chooses on Enter;
 *  4. a choice is remembered across a reload, and a stored id that no longer exists falls back to
 *     the default persona with the reason under the picker - never to an invented one;
 *  5. the request carries the selected persona's *prompt*, and switching persona changes the next
 *     request's system message;
 *  6. the turn's context line names the persona the answer was sent with;
 *  7. the picker survives a long reply, a stream in progress, and a provider that cannot answer,
 *     and the same persona answers again afterwards;
 *  8. the picker still fits the control bar's one row above 1200px and the phone layout below it;
 *  9. no console error, and the pane posts only the chat stream.
 *
 * Usage:
 *
 *   node scripts/chat_persona_walk.mjs                                  # starts its own fixtures
 *   node scripts/chat_persona_walk.mjs --base-url URL --token TOKEN     # use a running app
 *   node scripts/chat_persona_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/chat_persona_walk.mjs --states empty,populated         # a subset
 *   node scripts/chat_persona_walk.mjs --capture-only                   # write a "before" set
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
        console.log("usage: node scripts/chat_persona_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states empty,populated,long,loading,error,recovery] [--json] [--capture-only]");
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


/** Everything the walk needs about the picker, the request it builds, and the pane around it. */
function readPicker(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const select = document.getElementById("chatPersona");
    const description = document.getElementById("chatPersonaDescription");
    const search = document.getElementById("chatPersonaSearch");
    const count = document.getElementById("chatPersonaCount");
    const header = document.querySelector(".chat-header-bar");
    const rect = (node) => (node ? node.getBoundingClientRect() : null);
    const descriptionRect = rect(description);
    const searchRect = rect(search);
    const headerRows = (() => {
      if (!header) return 0;
      const boxes = [...header.children].map((child) => child.getBoundingClientRect())
        .filter((box) => box.width > 0).sort((a, b) => a.top - b.top);
      let rows = 0;
      let reached = -Infinity;
      for (const box of boxes) {
        if (box.top >= reached - 1) { rows += 1; reached = box.bottom; } else { reached = Math.max(reached, box.bottom); }
      }
      return rows;
    })();
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      pageOverflowX: document.documentElement.scrollWidth - window.innerWidth,
      options: select ? [...select.options].map((option) => ({
        id: option.value,
        label: clean(option.textContent),
        disabled: option.disabled,
        selected: option.selected,
      })) : [],
      selected: select ? select.value : "",
      selectEnabled: select ? !select.disabled : false,
      search: search ? {
        present: true,
        value: search.value,
        label: search.getAttribute("aria-label") || "",
        controls: search.getAttribute("aria-controls") || "",
        height: searchRect ? Math.round(searchRect.height) : 0,
      } : { present: false },
      description: description ? {
        text: clean(description.textContent),
        title: description.getAttribute("title") || "",
        state: description.dataset.state || "",
        height: descriptionRect ? Math.round(descriptionRect.height) : 0,
        clamped: description ? description.scrollWidth > description.clientWidth + 1 : false,
      } : null,
      count: clean(count ? count.textContent : ""),
      header: header ? { rows: headerRows, height: Math.round(header.getBoundingClientRect().height) } : null,
      registry: typeof CHAT_PERSONAS === "undefined" ? null : CHAT_PERSONAS.map((persona) => ({
        id: persona.id,
        label: persona.label,
        description: persona.description,
        prompt: persona.prompt,
      })),
      defaultPersona: typeof CHAT_PERSONA_DEFAULT === "undefined" ? "" : CHAT_PERSONA_DEFAULT,
      stored: (() => { try { return localStorage.getItem("m5_chat_persona"); } catch (error) { return null; } })(),
      // The *last* context line: each answer records the context it was sent with, and the claim
      // here is about the persona the most recent answer went out under.
      context: (() => {
        const lines = [...document.querySelectorAll("#chatMessages .chat-context")];
        return clean(lines.length ? lines[lines.length - 1].textContent : "");
      })(),
      turns: typeof chatHistory === "undefined" ? [] : chatHistory.map((message) => ({ role: message.role, state: message.status || "" })),
      streaming: Boolean(document.querySelector('#chatMessages .msg[data-streaming="true"]')),
      stopVisible: Boolean(document.getElementById("chatStopBtn"))
        && getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
      sendDisabled: Boolean(document.getElementById("chatSendBtn") && document.getElementById("chatSendBtn").disabled),
    };
  });
}

/** Type into the filter the way a reader does, so the pane's own listener runs. */
const filter = (page, value) => page.evaluate((text) => {
  const search = document.getElementById("chatPersonaSearch");
  // A build without a registry has no filter: `--capture-only` runs against origin/main, where this
  // walk is photographing the picker it replaced.
  if (!search) return null;
  search.value = text;
  search.dispatchEvent(new Event("input", { bubbles: true }));
  return search.value;
}, value);

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
  const shoot = async (page, name) => {
    if (!args.screenshots) return "";
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
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
  const bodies = [];
  const fixtures = [];

  const openPage = async (base, authToken) => {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 40[34]/.test(message.text())) pageErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (request.method() !== "POST") return;
      const path = new URL(request.url()).pathname;
      posts.push(path);
      if (path === "/api/chat/stream") bodies.push(request.postData() || "");
    });
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
      const select = document.getElementById("chatPersona");
      return Boolean(select) && select.options.length > 0;
    }, { timeout: 30000 }).catch(() => {});
    await sleep(args.settleMs * 4);
    return page;
  };

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

  const catalogue = (page) => page.evaluate(() => [...document.querySelectorAll("#chatModel option")]
    .map((option) => option.value));
  const localsOf = (list) => list.filter((value) => value && !value.includes("::") && !value.startsWith("combo/") && value !== "__custom__");
  const pickModel = (page, model) => page.evaluate((choice) => {
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
    // ── 1-8: the picker is the registry, and the filter is a filter ───────────
    if (state("empty")) await withFixture([], async (page) => {
      const opened = await readPicker(page);
      record(
        "every persona the pane ships is in the picker, under its own label",
        Array.isArray(opened.registry) && opened.registry.length >= 3
          && opened.registry.every((persona) => opened.options.some((option) => option.id === persona.id && option.label === persona.label)),
        `${opened.options.length} option(s): ${opened.options.map((option) => `${option.id}=${option.label}`).join(" · ")}`,
      );
      record(
        "and nothing is offered that the registry does not name",
        opened.options.every((option) => option.disabled || (opened.registry || []).some((persona) => persona.id === option.id))
          && opened.count.includes(String(opened.registry.length)),
        `count "${opened.count}" · ids ${opened.options.map((option) => option.id || "(disabled)").join(",")}`,
      );
      record(
        "the pane opens on the default persona, and says what it does",
        opened.selected === opened.defaultPersona
          && Boolean(opened.description) && opened.description.text.length > 30,
        `${opened.selected} · "${opened.description ? opened.description.text.slice(0, 90) : ""}"`,
      );
      record(
        "the description is one clamped line, with the whole sentence in its title",
        Boolean(opened.description) && opened.description.height <= 20
          && opened.description.title === opened.description.text,
        `${opened.description ? `${opened.description.height}px` : "?"} · title ${opened.description ? opened.description.title.length : 0} chars`,
      );
      record(
        "the filter narrows the list by label or description without dropping the selection",
        await (async () => {
          await filter(page, "audit");
          const filteredPane = await readPicker(page);
          await filter(page, "");
          return filteredPane.options.some((option) => option.id === "bug-ux-auditor")
            && !filteredPane.options.some((option) => option.id === "code-only")
            && filteredPane.selected === opened.selected;
        })(),
        "typed \"audit\"",
      );
      record(
        "a filter that matches nothing keeps the selected persona and says so",
        await (async () => {
          await filter(page, "zzzz");
          const none = await readPicker(page);
          await filter(page, "");
          return none.options.some((option) => option.id === opened.selected)
            && none.options.some((option) => option.disabled && /no other persona matches/i.test(option.label))
            && Boolean(none.description) && none.description.state === "empty";
        })(),
        "typed \"zzzz\"",
      );
      record(
        "Escape clears the filter and puts every persona back",
        await (async () => {
          await filter(page, "code");
          const typed = await readPicker(page);
          await page.evaluate(() => {
            const search = document.getElementById("chatPersonaSearch");
            if (!search) return;
            search.focus();
            search.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
          });
          const cleared = await readPicker(page);
          return typed.options.length < (opened.registry || []).length && cleared.options.length === (opened.registry || []).length;
        })(),
        "typed \"code\", pressed Escape",
      );
      record(
        "Enter chooses the first match and moves focus to the picker",
        await (async () => {
          await filter(page, "code only");
          await page.evaluate(() => {
            const search = document.getElementById("chatPersonaSearch");
            if (search) search.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
          });
          const chosen = await readPicker(page);
          const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
          await filter(page, "");
          return chosen.selected === "code-only" && focused === "chatPersona";
        })(),
        "typed \"code only\", pressed Enter",
      );
      record(
        "a choice is remembered across a reload, and a stored id that no longer exists falls back",
        await (async () => {
          await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("bug-ux-auditor") : null));
          await page.reload({ waitUntil: "domcontentloaded" });
          await sleep(args.settleMs * 10);
          await page.evaluate(() => switchTab("chat"));
          await sleep(args.settleMs * 4);
          const restored = await readPicker(page);
          await page.evaluate(() => localStorage.setItem("m5_chat_persona", "a-persona-that-was-deleted"));
          await page.reload({ waitUntil: "domcontentloaded" });
          await sleep(args.settleMs * 10);
          await page.evaluate(() => switchTab("chat"));
          await sleep(args.settleMs * 4);
          const fellBack = await readPicker(page);
          return restored.selected === "bug-ux-auditor"
            && fellBack.selected === fellBack.defaultPersona
            && Boolean(fellBack.description) && /no longer available/i.test(fellBack.description.text);
        })(),
        "selected the auditor, reloaded; then a deleted id",
      );
      await page.evaluate(() => { try { localStorage.removeItem("m5_chat_persona"); } catch (error) { /* storage may be blocked */ } });
      await page.reload({ waitUntil: "domcontentloaded" });
      await sleep(args.settleMs * 10);
      await page.evaluate(() => switchTab("chat"));
      await sleep(args.settleMs * 4);
      const finalPane = await readPicker(page);
      record(
        "the picker still fits the control bar's one row",
        Boolean(finalPane.header) && (finalPane.innerWidth >= 1200
          ? finalPane.header.rows === 1 && finalPane.header.height <= 110
          : finalPane.header.rows <= 3 && finalPane.header.height <= 250),
        `${finalPane.header ? `${finalPane.header.rows} row(s), ${finalPane.header.height}px` : "?"} at ${finalPane.innerWidth}px`,
      );
      record(
        "at 390x844 the picker and its filter still fit, with nothing overflowing",
        await (async () => {
          await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
          await sleep(args.settleMs * 6);
          const narrow = await readPicker(page);
          await page.setViewport({ width, height, deviceScaleFactor: 1 });
          await sleep(args.settleMs * 4);
          return narrow.pageOverflowX <= 0 && narrow.options.length >= 3
            && Boolean(narrow.search.present) && narrow.header.rows <= 4;
        })(),
        "390x844",
      );
      await shoot(page, `chat-persona-empty-${args.viewport}-${args.theme}`);
    });

    // ── 9-11: what is shown is what is sent ──────────────────────────────────
    if (state("populated")) await withFixture([], async (page) => {
      const models = localsOf(await catalogue(page));
      const local = models[0];
      await pickModel(page, local);
      await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("principal-engineer") : null));
      await send(page, REQUEST);
      await settle(page);
      const first = JSON.parse(bodies[0] || "{}");
      const sentSystem = Array.isArray(first.messages) && first.messages[0] ? first.messages[0].content : "";
      const principal = ((await readPicker(page)).registry || []).find((persona) => persona.id === "principal-engineer");
      record(
        "the request carries the selected persona's prompt, not its id",
        Boolean(principal) && sentSystem === principal.prompt,
        `system message ${JSON.stringify(String(sentSystem).slice(0, 70))}`,
      );
      await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("code-only") : null));
      await send(page, FOLLOW_UP);
      await settle(page);
      const second = JSON.parse(bodies[1] || "{}");
      const nextSystem = Array.isArray(second.messages) && second.messages[0] ? second.messages[0].content : "";
      const codeOnly = ((await readPicker(page)).registry || []).find((persona) => persona.id === "code-only");
      record(
        "switching persona changes the next request's system message",
        Boolean(codeOnly) && nextSystem === codeOnly.prompt && nextSystem !== sentSystem,
        `system message ${JSON.stringify(String(nextSystem).slice(0, 70))}`,
      );
      const answered = await readPicker(page);
      record(
        "the reply arrives, and the turn's context line names the persona it was sent",
        answered.turns.some((turn) => turn.role === "assistant")
          && !answered.turns.some((turn) => turn.state === "error")
          && /Code Only/i.test(answered.context),
        `${answered.turns.length} turn(s) · context "${answered.context.slice(0, 90)}"`,
      );
      record(
        "the picker keeps its selection and its description after two sends",
        answered.selected === "code-only" && Boolean(answered.description) && answered.description.text.length > 30,
        `${answered.selected} · "${answered.description ? answered.description.text.slice(0, 70) : ""}"`,
      );
      await shoot(page, `chat-persona-populated-${args.viewport}-${args.theme}`);
    });

    // ── 12: a long reply does not disturb the picker ─────────────────────────
    if (state("long")) await withFixture(["--long-model", "Qwen3.8-27B-oQ4e-mtp"], async (page) => {
      const models = localsOf(await catalogue(page));
      const local = models.find((value) => value === "Qwen3.8-27B-oQ4e-mtp") || models[0];
      await pickModel(page, local);
      await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("bug-ux-auditor") : null));
      await send(page, REQUEST);
      await settle(page);
      const pane = await readPicker(page);
      record(
        "a long reply scrolls the transcript, not the persona picker",
        pane.selected === "bug-ux-auditor" && pane.options.length >= 3
          && Boolean(pane.header) && (pane.innerWidth < 1200 || pane.header.rows === 1),
        `${pane.selected} · ${pane.options.length} option(s) · ${pane.header ? pane.header.rows : "?"} row(s)`,
      );
      await shoot(page, `chat-persona-long-content-${args.viewport}-${args.theme}`);
    });

    // ── 13: mid-stream the picker is still usable ────────────────────────────
    if (state("loading")) await withFixture(["--slow-model", "Qwen3.8-27B-oQ4e-mtp"], async (page, ready) => {
      const models = localsOf(await catalogue(page));
      const slow = models.find((value) => value === ready.slow_model) || models[0];
      await pickModel(page, slow);
      await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("principal-engineer") : null));
      await send(page, REQUEST);
      let streaming = null;
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await sleep(250);
        const pane = await readPicker(page);
        if (pane.streaming) { streaming = pane; break; }
      }
      record(
        "while a reply streams, the picker still says which persona is answering",
        Boolean(streaming) && streaming.selectEnabled && streaming.selected === "principal-engineer"
          && Boolean(streaming.description) && streaming.description.text.length > 30,
        streaming ? `${streaming.selected} · select enabled ${streaming.selectEnabled}` : "never saw a streaming turn",
      );
      await shoot(page, `chat-persona-loading-${args.viewport}-${args.theme}`);
      await page.evaluate(async () => {
        document.getElementById("chatStopBtn").click();
        await new Promise((done) => setTimeout(done, 1200));
      });
    });

    // ── 14-15: a failure does not invent or lose a persona ───────────────────
    if (state("error")) await withFixture([], async (page) => {
      const values = await catalogue(page);
      const keyless = values.find((value) => value.includes("::"));
      await pickModel(page, keyless);
      await page.evaluate(() => (typeof selectChatPersona === "function" ? selectChatPersona("code-only") : null));
      await send(page, REQUEST);
      await settle(page);
      const failed = await readPicker(page);
      record(
        "a provider that cannot answer leaves the choice exactly where it was",
        failed.selected === "code-only" && failed.options.length >= 3
          && Boolean(failed.description) && failed.description.text.length > 30 && failed.stored === "code-only",
        `${failed.selected} · stored ${failed.stored} · ${failed.options.length} option(s)`,
      );
      await shoot(page, `chat-persona-error-${args.viewport}-${args.theme}`);

      if (state("recovery")) {
        const models = localsOf(values);
        await pickModel(page, models[0]);
        await send(page, FOLLOW_UP);
        await settle(page);
        const recovered = await readPicker(page);
        const lastSystem = (() => {
          const last = JSON.parse(bodies[bodies.length - 1] || "{}");
          return Array.isArray(last.messages) && last.messages[0] ? last.messages[0].content : "";
        })();
        record(
          "after the failure the same persona answers, with the same prompt on the wire",
          recovered.selected === "code-only"
            && recovered.turns.filter((turn) => turn.state === "complete").length >= 1
            && lastSystem === ((recovered.registry || []).find((persona) => persona.id === "code-only") || {}).prompt,
          `${recovered.selected} · system ${JSON.stringify(String(lastSystem).slice(0, 50))}`,
        );
        await shoot(page, `chat-persona-recovery-${args.viewport}-${args.theme}`);
      }
    });

    record("the pane raises no console error", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
    const unexpected = [...new Set(posts)].filter((path) => path !== "/api/chat/stream");
    record(
      "the pane posts only the chat stream",
      unexpected.length === 0,
      [...new Set(posts)].join(" · ") || "no POST",
    );
  } finally {
    for (const fixture of fixtures.slice()) fixture.child.kill();
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
