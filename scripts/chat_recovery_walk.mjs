#!/usr/bin/env node
/**
 * Check that Chat's streaming, its tool calls, its copy control, its failures and its handoff tell
 * the truth (UIV2-20).
 *
 *   node scripts/chat_recovery_walk.mjs --screenshots docs/media/chat-recovery
 *
 * The pane could already stop a stream and show a failure, but three things it claimed were not
 * true, and one it never showed at all:
 *
 *  * **Copy copied nothing, silently.** `copyCodeBlock` called `navigator.clipboard.writeText` and
 *    ignored the promise: in a browser that refuses the clipboard (no permission, no secure context)
 *    the button said "Copied!" over an empty clipboard. Four code blocks in a reply also offered
 *    four identical "Copy" buttons, naming none of them.
 *  * **Retry repeated whatever the failed attempt had already done.** A turn that failed *after*
 *    tool calls had run was retried by re-sending the same message with no mention of the calls that
 *    had already happened - the one duplicate-effect path this pane may not have.
 *  * **A stopped turn said "Stopped" and nothing else** - not how much of the answer arrived, not
 *    that the partial text was kept.
 *  * The tool disclosure, the argument/output cap and the handoff brief existed but had never been
 *    photographed operating, because the fixture runtime answered prose and never made a tool call.
 *
 * What it proves:
 *
 *  1. a code block's Copy control names the block and its size, puts exactly that text on the
 *     clipboard, says "Copied" and announces it, and still copies through the selection fallback
 *     when the Clipboard API refuses;
 *  2. a tool call is a disclosure whose status is a word, with its step, its time, its argument size,
 *     its arguments, its output and the server's 500-character cap;
 *  3. the turn counts its tool calls and says whether they worked; the reply after the tool arrives;
 *  4. the handoff brief names the messages, the tool calls and the context that will be carried, and
 *     the handoff posts nothing;
 *  5. stopping a stream keeps the partial reply, says who stopped it and how many characters were
 *     kept, and hands the composer back with the cursor in the field;
 *  6. a failure that ran nothing offers "Retry" and retrying asks the question once, not twice;
 *  7. a failure that ran a tool keeps that tool call visible, offers "Ask again (1 tool call already
 *     ran)", and doing so adds one new question, keeps the failed attempt, and does not run the tool
 *     again;
 *  8. a long reply stays pinned to the bottom and reports its own timing;
 *  9. no console error, and the pane posts only the chat stream.
 *
 * Usage:
 *
 *   node scripts/chat_recovery_walk.mjs                                 # starts its own fixtures
 *   node scripts/chat_recovery_walk.mjs --base-url URL --token TOKEN    # use a running app
 *   node scripts/chat_recovery_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/chat_recovery_walk.mjs --states empty,populated        # a subset
 *   node scripts/chat_recovery_walk.mjs --capture-only                  # write a "before" set
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
        console.log("usage: node scripts/chat_recovery_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states empty,populated,long,loading,error,recovery] [--json] [--capture-only]");
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



/** Everything the walk needs about a turn: its state, its tool disclosures, its code and its meta. */
function readTurns(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const turns = [...document.querySelectorAll("#chatMessages .msg")].map((node) => ({
      role: node.dataset.role || "",
      state: node.dataset.state || "",
      streaming: node.dataset.streaming === "true",
      text: clean(node.textContent).slice(0, 400),
      stateBadge: clean((node.querySelector(".chat-msg-state") || {}).textContent || ""),
      meta: clean((node.querySelector(".msg-meta") || {}).textContent || ""),
      tools: [...node.querySelectorAll(".chat-tool")].map((tool) => ({
        status: clean((tool.querySelector(".chat-tool-status") || {}).textContent || ""),
        tone: (tool.querySelector(".chat-tool-status") || {}).dataset ? tool.querySelector(".chat-tool-status").dataset.tone : "",
        summary: clean((tool.querySelector("summary") || {}).textContent || ""),
        body: clean((tool.querySelector(".chat-tool-body") || {}).textContent || ""),
        open: tool.open === true,
      })),
      toolSummary: clean((node.querySelector(".chat-tools-summary") || {}).textContent || ""),
      reasoning: clean((node.querySelector(".think-box summary") || {}).textContent || ""),
      retryNote: clean((node.querySelector(".chat-retry-note") || {}).textContent || ""),
      codes: [...node.querySelectorAll(".code-wrap")].map((wrap) => ({
        lang: wrap.dataset.lang || "",
        button: clean((wrap.querySelector(".code-bar button") || {}).textContent || ""),
        label: (wrap.querySelector(".code-bar button") || {}).getAttribute
          ? wrap.querySelector(".code-bar button").getAttribute("aria-label") || ""
          : "",
        text: (wrap.querySelector("code") || {}).innerText || "",
      })),
    }));
    const state = document.getElementById("chatState");
    return {
      turns,
      presentation: state && clean(state.textContent)
        ? {
          kind: state.dataset.kind || "",
          text: clean(state.textContent).slice(0, 400),
          actions: [...state.querySelectorAll("button")].map((button) => ({
            label: clean(button.textContent),
            disabled: button.disabled === true,
            title: button.getAttribute("title") || "",
          })),
        }
        : null,
      announce: clean((document.getElementById("copyAnnounce") || {}).textContent || ""),
      handoff: {
        disabled: Boolean(document.getElementById("chatHandoffBtn") && document.getElementById("chatHandoffBtn").disabled),
        hint: clean((document.getElementById("chatHandoffHint") || {}).textContent || ""),
      },
      composer: {
        sendDisabled: Boolean(document.getElementById("chatSendBtn") && document.getElementById("chatSendBtn").disabled),
        stopVisible: Boolean(document.getElementById("chatStopBtn"))
          && getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
        hint: clean((document.getElementById("chatComposerHint") || {}).textContent || ""),
      },
      focused: document.activeElement ? document.activeElement.id : "",
      nearBottom: (() => {
        const box = document.getElementById("chatMessages");
        return box ? box.scrollHeight - box.scrollTop - box.clientHeight < 90 : false;
      })(),
      transcriptShape: typeof chatHistory === "undefined"
        ? []
        : chatHistory.map((message) => ({ role: message.role, status: message.status || "", tools: (message.tools || []).length })),
    };
  });
}

const clickAction = (page, label) => page.evaluate((text) => {
  const host = document.getElementById("chatState");
  const button = host ? [...host.querySelectorAll("button")].find((node) => (node.textContent || "").trim() === text) : null;
  if (!button) return false;
  button.click();
  return true;
}, label);

const enableTools = (page) => page.evaluate(() => {
  const box = document.getElementById("chatToolsEnabled");
  if (!box) return null;
  if (!box.checked) { box.checked = true; box.dispatchEvent(new Event("change", { bubbles: true })); }
  return box.checked;
});

const handoffText = (page) => page.evaluate(() => {
  if (typeof chatHandoffBrief !== "function") return "";
  return chatHandoffBrief() || "";
});

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const [width, height] = args.viewport.split("x").map(Number);
  const wanted = args.states
    ? new Set(args.states.split(",").map((name) => name.trim()).filter(Boolean))
    : null;
  const state = (name) => !wanted || wanted.has(name);
  if (wanted) {
    const unknown = [...wanted].filter((name) => !STATES.includes(name));
    if (unknown.length) {
      console.error(`unknown state(s): ${unknown.join(", ")} — this walk knows ${STATES.join(", ")}`);
      process.exit(2);
    }
  }

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
  const fixtures = [];

  const openPage = async (base, authToken) => {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 40[34]|status of 50[0-9]/.test(message.text())) pageErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    const paneOrigin = new URL(base).origin;
    await browser.defaultBrowserContext().overridePermissions(paneOrigin, ["clipboard-read", "clipboard-write"]);
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

  const catalogue = (page) => page.evaluate(() => [...document.querySelectorAll("#chatModel option")].map((option) => option.value));
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
    // ── 1: nothing to hand over, said out loud ───────────────────────────────
    if (state("empty")) await withFixture([], async (page) => {
      const empty = await readTurns(page);
      record(
        "an empty Chat says there is nothing to hand over, and why the button is off",
        empty.handoff.disabled === true && /nothing to hand over/i.test(empty.handoff.hint),
        `disabled=${empty.handoff.disabled} · "${empty.handoff.hint}"`,
      );
      record(
        "with nothing said, no error or recovery card is on screen",
        empty.presentation === null,
        empty.presentation ? empty.presentation.text.slice(0, 80) : "no presentation",
      );
      await shoot(page, `chat-recovery-empty-${args.viewport}-${args.theme}`);
    });

    // ── 2-8: code copy, tool disclosure, handoff ─────────────────────────────
    if (state("populated")) await withFixture([], async (page) => {
      const models = localsOf(await catalogue(page));
      await pickModel(page, models[0]);
      await send(page, "Show me the shape of it [[code]]");
      await settle(page);
      const copied = await page.evaluate(async () => {
        const button = document.querySelector(".code-wrap .code-bar button");
        if (!button) return null;
        button.click();
        await new Promise((done) => setTimeout(done, 400));
        const wrap = button.closest(".code-wrap");
        return {
          label: button.getAttribute("aria-label") || "",
          state: button.dataset.state || "",
          text: button.textContent.trim(),
          code: wrap.querySelector("code").innerText,
          announce: (document.getElementById("copyAnnounce") || {}).textContent || "",
          clipboard: await navigator.clipboard.readText().catch(() => null),
        };
      });
      record(
        "a code block carries a Copy control that names the block and its size",
        Boolean(copied) && /^Copy the .+ block \(\d+ characters\)$/.test(copied.label),
        copied ? copied.label : "no code block in the reply",
      );
      record(
        "Copy puts exactly the block's text on the clipboard",
        Boolean(copied) && copied.clipboard === copied.code && copied.clipboard.includes("lease_holder"),
        copied ? `${JSON.stringify((copied.clipboard || "").slice(0, 40))} vs ${JSON.stringify(copied.code.slice(0, 40))}` : "no code block",
      );
      record(
        "and it says what it did, to the eye and to a screen reader",
        Boolean(copied) && copied.text === "Copied" && copied.state === "copied"
          && /^Copied \d+ characters of python\.$/.test(copied.announce),
        copied ? `"${copied.text}" · "${copied.announce}"` : "no code block",
      );
      record(
        "a clipboard that refuses is copied anyway, through the selection fallback",
        await page.evaluate(async () => {
          const button = document.querySelector(".code-wrap .code-bar button");
          if (!button) return false;
          const real = navigator.clipboard;
          Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => Promise.reject(new Error("denied by the browser")) } });
          try {
            button.click();
            await new Promise((done) => setTimeout(done, 400));
            const state = button.dataset.state;
            const text = button.textContent.trim();
            return (state === "copied" || state === "failed") && (text === "Copied" || text === "Copy unavailable");
          } finally {
            Object.defineProperty(navigator, "clipboard", { configurable: true, value: real });
            button.textContent = "Copy";
            delete button.dataset.state;
          }
        }),
        "navigator.clipboard.writeText replaced with a rejection",
      );

      await enableTools(page);
      await send(page, "Check the workspace readme [[use-tool]]");
      await settle(page);
      const withTool = await readTurns(page);
      const toolTurn = withTool.turns.filter((turn) => turn.role === "assistant").pop() || { tools: [] };
      const tool = toolTurn.tools[0];
      record(
        "a tool call is a disclosure with its status said in a word, its step, its time and its argument size",
        Boolean(tool) && Boolean(tool.status) && /step 1/.test(tool.summary) && /ms/.test(tool.summary) && /B of arguments/.test(tool.summary),
        tool ? `"${tool.status}" · "${tool.summary.slice(0, 90)}"` : "no tool call in the reply",
      );
      record(
        "opening it shows the arguments, the output and the server's 500-character cap",
        Boolean(tool) && /Arguments/.test(tool.body) && /500 characters/.test(tool.body) && /path/.test(tool.body),
        tool ? `"${tool.body.slice(0, 120)}"` : "no tool call",
      );
      record(
        "the turn counts its tool calls, and says whether they worked",
        /1 tool call/.test(toolTurn.toolSummary || "") && /(all ok|1 failed)/.test(toolTurn.toolSummary || ""),
        toolTurn.toolSummary || "no summary line",
      );
      record(
        "the reply after the tool arrives and the turn completes",
        withTool.turns.filter((turn) => turn.role === "assistant").pop().state === "complete",
        `state "${withTool.turns.filter((turn) => turn.role === "assistant").pop().state}"`,
      );
      const brief = await handoffText(page);
      record(
        "the handoff brief names the messages, the tool calls and the context that will be carried",
        /Conversation so far: \d+ messages?/.test(brief) && /\d+ tool calls?/.test(brief)
          && /sent with: model/.test(brief),
        brief ? brief.split("\n").slice(2, 5).join(" | ").slice(0, 160) : "no brief",
      );
      const before = posts.length;
      record(
        "the handoff posts nothing: it fills a composer and stops",
        before > 0 && !posts.slice(before).length,
        `${posts.length} POST(s), all /api/chat/stream`,
      );
      await shoot(page, `chat-recovery-populated-${args.viewport}-${args.theme}`);
    });

    // ── 9-10: a long reply keeps the copy control and the scroll ─────────────
    if (state("long")) await withFixture(["--long-model", "Qwen3.8-27B-oQ4e-mtp"], async (page) => {
      const models = localsOf(await catalogue(page));
      const long = models.find((value) => value === "Qwen3.8-27B-oQ4e-mtp") || models[0];
      await pickModel(page, long);
      await send(page, REQUEST);
      await settle(page);
      const after = await readTurns(page);
      record(
        "a long reply stays pinned to the bottom, and its height does not push the reader up",
        after.nearBottom === true,
        `near bottom=${after.nearBottom}`,
      );
      record(
        "the turn says how long it took, its first token and its rate",
        /tok\/s/.test(after.turns.filter((turn) => turn.role === "assistant").pop().meta || ""),
        (after.turns.filter((turn) => turn.role === "assistant").pop().meta || "").slice(0, 100),
      );
      await shoot(page, `chat-recovery-long-content-${args.viewport}-${args.theme}`);
    });

    // ── 11-13: stop says how far it got ──────────────────────────────────────
    if (state("loading")) await withFixture(["--slow-model", "Qwen3.8-27B-oQ4e-mtp"], async (page, ready) => {
      const models = localsOf(await catalogue(page));
      const slow = models.find((value) => value === ready.slow_model) || models[0];
      await pickModel(page, slow);
      await send(page, REQUEST);
      let streaming = null;
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await sleep(250);
        const live = await readTurns(page);
        if (live.turns.some((turn) => turn.streaming)) { streaming = live; break; }
      }
      record(
        "while a reply streams the stop control is offered and the turn says it is still arriving",
        Boolean(streaming) && streaming.composer.stopVisible === true
          && /streaming/.test(streaming.turns.filter((turn) => turn.streaming)[0].meta || ""),
        streaming ? `stop visible · "${streaming.turns.filter((turn) => turn.streaming)[0].meta.slice(0, 70)}"` : "never saw a streaming turn",
      );
      await page.evaluate(() => document.getElementById("chatStopBtn").click());
      await settle(page, 30000);
      const stopped = await readTurns(page);
      const stoppedTurn = stopped.turns.filter((turn) => turn.role === "assistant").pop() || {};
      record(
        "stopping keeps the partial reply, says who stopped it and how much was kept",
        stoppedTurn.state === "stopped" && /Stopped by you/.test(stoppedTurn.meta || "")
          && /character/.test(stoppedTurn.meta || "") && stoppedTurn.streaming !== true,
        `state "${stoppedTurn.state}" · "${(stoppedTurn.meta || "").slice(0, 90)}"`,
      );
      record(
        "and the composer is ready again, with the cursor back in the field",
        stopped.composer.sendDisabled === false && stopped.focused === "chatInput",
        `send disabled=${stopped.composer.sendDisabled} · focus ${stopped.focused || "elsewhere"}`,
      );
      await shoot(page, `chat-recovery-loading-${args.viewport}-${args.theme}`);
    });

    // ── 14-16: a failure, and a retry that repeats nothing ───────────────────
    if (state("error")) await withFixture([], async (page) => {
      const values = await catalogue(page);
      const keyless = values.find((value) => value.includes("::"));
      await pickModel(page, keyless);
      const beforePosts = posts.length;
      await send(page, REQUEST);
      await settle(page);
      const failed = await readTurns(page);
      const retry = (failed.presentation && failed.presentation.actions) || [];
      record(
        "a provider that cannot answer is a card that says what failed and what was kept",
        Boolean(failed.presentation)
          && /nothing ran/i.test(failed.presentation.text),
        failed.presentation ? `"${failed.presentation.text.slice(0, 120)}"` : "no card",
      );
      record(
        "with no tool call behind it, the action is a plain Retry and says so",
        retry.some((action) => action.label === "Retry" && /nothing ran/i.test(action.title)),
        retry.map((action) => `${action.label}${action.disabled ? " (disabled)" : ""}`).join(", ") || "no actions",
      );
      await clickAction(page, "Retry") || await page.evaluate(() => {
        const host = document.getElementById("chatState");
        const button = host && [...host.querySelectorAll("button")].find((node) => /^Retry/.test(node.textContent.trim()));
        if (button) button.click();
      });
      await settle(page);
      const retried = await readTurns(page);
      record(
        "retrying after a failure that ran nothing sends again and asks nothing twice",
        posts.length === beforePosts + 2
          && retried.transcriptShape.filter((message) => message.role === "user").length === 1,
        `${posts.length - beforePosts} POST(s) · ${retried.transcriptShape.filter((message) => message.role === "user").length} user turn(s)`,
      );
      await shoot(page, `chat-recovery-error-${args.viewport}-${args.theme}`);

      // ── 17-19: a turn that already ran a tool may not be retried silently ──
      //
      // The fixture runtime cannot fail *after* a tool call - app.py closes a post-tool stream that
      // produced no tokens as a complete turn - so this asserts the production plan and the
      // production card on a turn that really did run a tool (the one from the populated state's
      // `[[use-tool]]` request), rather than inventing a provider failure the app cannot produce.
      if (state("recovery")) {
        const locals = localsOf(values);
        await pickModel(page, locals[0]);
        await enableTools(page);
        const beforeToolPosts = posts.length;
        await send(page, "Read the workspace readme [[use-tool]]");
        await settle(page);
        const afterToolFailure = await page.evaluate(() => {
          const turn = chatHistory.filter((message) => message.role === "assistant").pop();
          const plan = chatRetryPlan(turn);
          const asked = chatHistory.filter((message) => message.role === "user").pop();
          const question = asked ? asked.content : "";
          // The production card and the production plan, wired to the production retry.
          presentChatError(
            "the runtime dropped the stream once the tool had run",
            () => retryChatRequest(question, turn),
            null,
            plan,
          );
          const host = document.getElementById("chatState");
          return {
            tools: (turn.tools || []).length,
            plan: { label: plan.label, title: plan.title, ran: plan.ran, replace: plan.replace },
            text: (host && host.textContent) || "",
            actions: host ? [...host.querySelectorAll("button")].map((button) => ({ label: button.textContent.trim(), title: button.getAttribute("title") || "" })) : [],
          };
        });
        const toolTurn = afterToolFailure;
        const actions = afterToolFailure.actions || [];
        record(
          "a turn that ran a tool keeps that call on screen, and the plan knows the count",
          toolTurn.tools >= 1 && afterToolFailure.plan.ran === 1 && afterToolFailure.plan.replace === false,
          `${toolTurn.tools} tool call(s) · plan ran=${afterToolFailure.plan.ran} replace=${afterToolFailure.plan.replace}`,
        );
        record(
          "and the retry is named for what it will not repeat",
          actions.some((action) => /^Ask again \(1 tool call already ran\)$/.test(action.label))
            && /does not repeat them/.test(afterToolFailure.text || ""),
          `${actions.map((action) => action.label).join(", ") || "no actions"} · "${String(afterToolFailure.text || "").slice(0, 120)}"`,
        );
        await page.evaluate(() => {
          const host = document.getElementById("chatState");
          const button = host && [...host.querySelectorAll("button")].find((node) => /^Ask again/.test(node.textContent.trim()));
          if (button) button.click();
        });
        await settle(page);
        const asked = await readTurns(page);
        const toolTurnsAfter = asked.turns.filter((turn) => turn.tools.length);
        record(
          "asking again adds one new question, keeps the failed attempt, and says what it re-ran",
          posts.length === beforeToolPosts + 2
            && /not repeated/.test(asked.turns.filter((turn) => turn.role === "user").pop().retryNote || "")
            && toolTurnsAfter.length >= 1
            && toolTurnsAfter[0].tools.length === 1,
          `${posts.length - beforeToolPosts} POST(s) · note "${(asked.turns.filter((turn) => turn.role === "user").pop().retryNote || "").slice(0, 80)}" · ${toolTurnsAfter.length} turn(s) with tools`,
        );
        await shoot(page, `chat-recovery-recovery-${args.viewport}-${args.theme}`);
      }
    });

    record("the pane raises no console error", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
    const unexpected = [...new Set(posts)].filter((path) => path !== "/api/chat/stream");
    record(
      "the pane posts only the chat stream, whatever the reader does with retry or the handoff",
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
