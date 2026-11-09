#!/usr/bin/env node
/**
 * UIV2-29: measured streaming, bounded memory/DOM and layout-stability evidence.
 *
 *   node scripts/uiv2_performance_safety_walk.mjs --screenshots /tmp/uiv2-29-performance
 *
 * The two chat turns travel through the real bearer-authenticated app and paced local SSE fixture.
 * Harness bursts then exercise the production cache, renderer and DOM with 30,000 synthetic events;
 * they never execute a tool. CDP heap/node readings are taken after explicit garbage collection.
 * No assertion is inferred from a screenshot and no external request is allowed.
 */
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { cpus, platform, arch } from "node:os";
import { resolve, relative } from "node:path";
import { pathToFileURL } from "node:url";

const STORY = "UIV2-29";
const SLOW_MODEL = "Qwen3.8-27B-oQ4e-mtp";
const FAST_MODEL = "Gemma-4-21B-REAP-4bit";
const RENDER_WINDOW = 300;
const CACHE_LIMIT = 5000;
const FRAME_BUDGET_MS = 4;
const HEAP_PLATEAU_BYTES = 8 * 1024 * 1024;
const HEAP_BASELINE_BYTES = 16 * 1024 * 1024;
const DOM_PLATEAU_NODES = 64;
const LAYOUT_SHIFT_BUDGET = 0.01;
const MIN_FAST_SSE_PER_SECOND = 50;
const VIEWPORTS = [
  { label: "1640x900", width: 1640, height: 900 },
  { label: "1440x900", width: 1440, height: 900 },
  { label: "1280x800", width: 1280, height: 800 },
  { label: "390x844", width: 390, height: 844, isMobile: true, hasTouch: true },
];
const THEMES = ["light", "dark"];
const EXPECTED_CAPTURES = 11;

function parseArgs(argv) {
  const args = { screenshots: "", baseUrl: "", token: "", tokenKey: "m5_api_token", settleMs: 100, timeoutMs: 60000, json: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      i += 1;
      return value;
    };
    if (arg === "--screenshots") args.screenshots = next();
    else if (arg === "--base-url") args.baseUrl = next();
    else if (arg === "--token") args.token = next();
    else if (arg === "--settle-ms") args.settleMs = Number(next());
    else if (arg === "--timeout-ms") args.timeoutMs = Number(next());
    else if (arg === "--json") args.json = true;
    else if (arg === "--help" || arg === "-h") { console.log("node scripts/uiv2_performance_safety_walk.mjs --screenshots DIR [--json]"); process.exit(0); }
    else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!args.screenshots) throw new Error("--screenshots is required: this walk writes source-bound capture evidence");
  return args;
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const target = candidate.startsWith("/") || candidate.startsWith(".") ? pathToFileURL(resolve(candidate)).href : candidate;
      const module = await import(target);
      return module.default || module;
    } catch (error) { failures.push(`${candidate}: ${error.message}`); }
  }
  throw new Error(`puppeteer-core is not importable.\n${failures.join("\n")}`);
}

function resolveChrome() {
  const explicit = process.env.STUDIO_SCREENSHOT_CHROME;
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`STUDIO_SCREENSHOT_CHROME does not exist: ${explicit}`);
    return explicit;
  }
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("No Chrome/Chromium found; set STUDIO_SCREENSHOT_CHROME");
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const digest = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const sourceHashes = () => ({
  "static/index.html": digest(resolve("static/index.html")),
  "scripts/uiv2_performance_safety_walk.mjs": digest(resolve("scripts/uiv2_performance_safety_walk.mjs")),
});

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try { const response = await fetch(url); if (response.status < 500) return true; } catch (_error) {}
    await sleep(150);
  }
  return false;
}

async function startFixture() {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--port", "0", "--slow-model", SLOW_MODEL],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`fixture startup timed out\n${stderr}`)), 120000);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try { resolveReady(JSON.parse(line)); } catch (_error) { rejectReady(new Error(`unreadable fixture line: ${line}`)); }
    });
    child.on("exit", (code) => { clearTimeout(timer); rejectReady(new Error(`fixture exited ${code}\n${stderr}`)); });
  });
  if (!await waitForHttp(`${ready.base_url}/`, 60000)) throw new Error(`fixture did not answer\n${stderr}`);
  return { ...ready, child };
}

function gitState() {
  const run = (args) => execFileSync("git", args, { encoding: "utf8" }).trim();
  return { head: run(["rev-parse", "HEAD"]), dirty: Boolean(run(["status", "--porcelain"])) };
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const captures = [];
  const metrics = { chat: {}, harness: {}, layout: [] };
  const record = (group, step, passed, detail, extra = {}) => {
    const row = { group, step, passed: Boolean(passed), detail: typeof detail === "string" ? detail : JSON.stringify(detail), ...extra };
    results.push(row);
    if (!args.json) console.log(`${row.passed ? "pass" : "FAIL"}  ${group}: ${step} — ${row.detail}`);
  };
  const shoot = async (page, name, metadata) => {
    const path = resolve(args.screenshots, `${name}.png`);
    mkdirSync(resolve(path, ".."), { recursive: true });
    await sleep(args.settleMs);
    await page.screenshot({ path });
    captures.push({ path: relative(resolve(args.screenshots), path), sha256: digest(path), ...metadata });
  };

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture();
      args.baseUrl = fixture.base_url;
      args.token = args.token || fixture.token;
    }
    const origin = new URL(args.baseUrl).origin;
    const browser = await puppeteer.launch({
      executablePath: resolveChrome(), headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb", "--enable-precise-memory-info"],
    });
    try {
      const browserVersion = await browser.version();
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      const errors = [];
      const outbound = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("console", (message) => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
      await page.setRequestInterception(true);
      page.on("request", (request) => {
        const target = new URL(request.url());
        if (target.origin !== origin) {
          outbound.push(target.origin);
          request.abort().catch(() => {});
        } else request.continue().catch(() => {});
      });
      await page.evaluateOnNewDocument((key, token) => {
        try { sessionStorage.setItem(key, token); localStorage.setItem("m5_theme", "dark"); } catch (_error) {}
      }, args.tokenKey, args.token);

      const authBody = JSON.stringify({});
      const anonymous = await fetch(`${args.baseUrl}/api/harness/approve`, { method: "POST", headers: { "content-type": "application/json" }, body: authBody });
      const authenticated = await fetch(`${args.baseUrl}/api/harness/approve`, { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${args.token}` }, body: authBody });
      record("browser", "anonymous approval execution is refused and an authenticated unknown approval cannot execute", anonymous.status === 401 && authenticated.status === 404, `anonymous=${anonymous.status} authenticated-unknown=${authenticated.status}`);

      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 8);
      await page.evaluate(() => switchTab("chat"));
      await sleep(args.settleMs * 2);

      const pickModel = async (wanted) => page.evaluate((model, other) => {
        const select = document.getElementById("chatModel");
        const options = [...select.options];
        const local = options.find((option) => option.value === model)
          || options.find((option) => option.value !== other && /local|omlx|m5|gemma/i.test(`${option.value} ${option.textContent}`));
        if (!local) return "";
        select.value = local.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        const tools = document.getElementById("chatToolsEnabled");
        if (tools && tools.checked) { tools.checked = false; tools.dispatchEvent(new Event("change", { bubbles: true })); }
        return local.value;
      }, wanted, wanted === SLOW_MODEL ? FAST_MODEL : SLOW_MODEL);
      const send = async (text) => {
        await page.evaluate((value) => {
          const input = document.getElementById("chatInput");
          input.value = value;
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }, text);
        await page.click("#chatSendBtn");
      };
      const lastAssistant = () => page.evaluate(() => {
        const msg = chatHistory[chatHistory.length - 1];
        const nodes = [...document.querySelectorAll("#chatMessages .msg.assistant")];
        const node = nodes[nodes.length - 1];
        // Markdown fences and emphasis are syntax, not visible reply text. Compare the
        // complete rendered reply, not a raw tail containing a closing ``` fence.
        const expected = document.createElement("div");
        expected.innerHTML = renderMarkdown(String(msg && msg.content || ""));
        const expectedBody = expected.querySelector(".md");
        const actualBody = node && node.querySelector(":scope > .md");
        const completeReplyMatches = Boolean(expectedBody && actualBody
          && expectedBody.innerHTML === actualBody.innerHTML);
        return msg && msg.role === "assistant" ? {
          completeReplyMatches,
          status: msg.status || (node && node.dataset.state), content: String(msg.content || ""),
          usage: msg.usage || null, dom: node ? String(node.textContent || "") : "", messages: chatHistory.length,
        } : { status: "missing", content: "", usage: null, dom: "", messages: chatHistory.length };
      });
      const waitComplete = async (timeout = 45000, midstream = false) => {
        const started = Date.now();
        let partial = 0;
        while (Date.now() - started < timeout) {
          await sleep(50);
          const state = await lastAssistant();
          if (midstream && state.status === "streaming" && !partial && state.content.trim().length >= 8) {
            partial = state.content.trim().length;
            await shoot(page, "chat-midstream", { group: "sse", viewport: "1440x900", theme: "dark", pane: "chat", state: "loading" });
          }
          if (state.status && state.status !== "streaming" && state.status !== "missing") return { state, partial };
        }
        return { state: await lastAssistant(), partial };
      };

      const slowModel = await pickModel(SLOW_MODEL);
      await sleep(args.settleMs * 3);
      await send("In two sentences, explain bounded streaming without hiding reader progress.");
      const slow = await waitComplete(45000, true);
      const slowTail = slow.state.content.trim().split(/\s+/).slice(-8).join(" ");
      record("sse", "a real paced SSE reply keeps its complete text and reaches complete", slow.state.status === "complete" && slow.state.content.length > 80 && slow.state.completeReplyMatches, `model=${slowModel} chars=${slow.state.content.length} messages=${slow.state.messages} full_reply_matches=${slow.state.completeReplyMatches} tail=${slowTail}`);
      await shoot(page, "chat-complete", { group: "sse", viewport: "1440x900", theme: "dark", pane: "chat", state: "populated" });
      const chatDigests = captures.slice(0, 2).map((capture) => capture.sha256);
      record("sse", "the loading capture is a real partial reply and differs from completion", slow.partial > 0 && slow.partial < slow.state.content.length && captures.length === 2 && chatDigests[0] !== chatDigests[1], `partial=${slow.partial} complete=${slow.state.content.length} distinct_pngs=${chatDigests[0] !== chatDigests[1]}`);
      metrics.chat.slow = { model: slowModel, partial_chars: slow.partial, complete_chars: slow.state.content.length };

      const fastModel = await pickModel(FAST_MODEL);
      await sleep(args.settleMs * 3);
      await page.evaluate(() => {
        window.__uiv29RenderCount = 0;
        window.__uiv29ScheduleCount = 0;
        window.__uiv29OriginalRender = window.renderChatMessages;
        window.__uiv29OriginalSchedule = window.scheduleChatRender;
        window.renderChatMessages = function (...callArgs) { window.__uiv29RenderCount += 1; return window.__uiv29OriginalRender.apply(this, callArgs); };
        window.scheduleChatRender = function (...callArgs) { window.__uiv29ScheduleCount += 1; return window.__uiv29OriginalSchedule.apply(this, callArgs); };
        window.__uiv29FastStarted = performance.now();
      });
      await send("Give a detailed five-sentence answer about stable high-frequency token rendering and reader trust.");
      const fast = await waitComplete(15000, false);
      const fastTiming = await page.evaluate(() => {
        const out = { elapsed_ms: performance.now() - window.__uiv29FastStarted, renders: window.__uiv29RenderCount, schedules: window.__uiv29ScheduleCount };
        window.renderChatMessages = window.__uiv29OriginalRender;
        window.scheduleChatRender = window.__uiv29OriginalSchedule;
        return out;
      });
      const modelTokens = Number(fast.state.usage && (fast.state.usage.completion_tokens || fast.state.usage.completionTokens)) || fast.state.content.trim().split(/\s+/).length;
      const ssePerSecond = fastTiming.schedules / Math.max(fastTiming.elapsed_ms / 1000, 0.001);
      const fastTail = fast.state.content.trim().split(/\s+/).slice(-8).join(" ");
      record("sse", "high-frequency SSE tokens are coalesced without losing the final reply", fast.state.status === "complete" && fastTiming.schedules >= 20 && fastTiming.renders > 0 && fastTiming.renders < fastTiming.schedules && fast.state.completeReplyMatches, `schedules=${fastTiming.schedules} renders=${fastTiming.renders} model_tokens=${modelTokens} chars=${fast.state.content.length} full_reply_matches=${fast.state.completeReplyMatches}`);
      record("sse", "the measured fast fixture actually exceeds fifty scheduled SSE updates per second", ssePerSecond >= MIN_FAST_SSE_PER_SECOND && fastTiming.schedules >= 20, `rate=${ssePerSecond.toFixed(1)}/s elapsed=${fastTiming.elapsed_ms.toFixed(1)}ms schedules=${fastTiming.schedules}`);
      await shoot(page, "chat-high-frequency-complete", { group: "sse", viewport: "1440x900", theme: "dark", pane: "chat", state: "populated" });
      metrics.chat.fast = { model: fastModel, ...fastTiming, model_tokens: modelTokens, sse_per_second: Number(ssePerSecond.toFixed(3)), complete_chars: fast.state.content.length };

      await page.evaluate(() => switchTab("harness"));
      await sleep(args.settleMs * 3);
      const snapshotSemantics = await page.evaluate(() => {
        const event = (seq) => ({ seq, type: "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: "snapshot", ok: true } });
        harnessResetEventCache();
        harnessRememberEvents([event(1), event(2), event(3)]);
        const first = harnessCachedEvents();
        const stableRead = first === harnessCachedEvents();
        harnessRememberEvents([event(4)]);
        const stableAppend = first === harnessCachedEvents() && first.map((ev) => ev.seq).join(",") === "1,2,3,4";
        harnessResetEventCache();
        harnessRememberEvents([event(10), event(20)]);
        const beforeOutOfOrder = harnessCachedEvents();
        harnessRememberEvents([event(15), event(15), { seq: "bad" }, null]);
        const after = harnessCachedEvents();
        const outOfOrder = beforeOutOfOrder !== after && after.map((ev) => ev.seq).join(",") === "10,15,20";
        harnessResetEventCache();
        harnessRememberEvents(Array.from({ length: 5000 }, (_, i) => event(i + 1)));
        const full = harnessCachedEvents();
        harnessRememberEvents([event(5001)]);
        const pruned = full === harnessCachedEvents() && full.length === 5000 && full[0].seq === 2 && full[full.length - 1].seq === 5001;
        harnessResetEventCache();
        const snapshotCleared = harnessCachedSnapshot === null;
        const reset = snapshotCleared && harnessCachedEvents().length === 0 && harnessCursor === 0 && harnessEventOrder.length === 0;
        return { stableRead, stableAppend, outOfOrder, pruned, reset };
      });
      record("dom-memory", "ordered snapshots are incremental while duplicates, invalid and out-of-order records retain merge semantics", Object.values(snapshotSemantics).every(Boolean), snapshotSemantics);

      const cdp = await page.createCDPSession();
      await cdp.send("Performance.enable");
      const memoryMetrics = async () => {
        await cdp.send("HeapProfiler.collectGarbage");
        await sleep(60);
        const response = await cdp.send("Performance.getMetrics");
        const values = Object.fromEntries(response.metrics.map((row) => [row.name, row.value]));
        return { Nodes: values.Nodes, Documents: values.Documents, JSEventListeners: values.JSEventListeners, JSHeapUsedSize: values.JSHeapUsedSize, JSHeapTotalSize: values.JSHeapTotalSize, LayoutCount: values.LayoutCount, RecalcStyleCount: values.RecalcStyleCount };
      };
      const seedHarness = async (start, count) => page.evaluate(({ first, total }) => {
        const event = (seq) => ({ seq, type: seq % 11 === 0 ? "turn/end" : "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: `bounded ${seq} ${"x".repeat(100)}`, ok: true } });
        harnessRememberEvents(Array.from({ length: total }, (_, i) => event(first + i)));
        renderStoredHarnessEvents(harnessCachedEvents());
        const stream = document.getElementById("harnessStream");
        stream.scrollTop = stream.scrollHeight;
      }, { first: start, total: count });
      await page.evaluate(() => harnessResetEventCache());
      await seedHarness(1, 6000);
      const memory0 = await memoryMetrics();
      const stressCycle = async (first) => page.evaluate(async ({ firstSeq }) => {
        const event = (seq) => ({ seq, type: seq % 11 === 0 ? "turn/end" : "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: `bounded ${seq} ${"x".repeat(100)}`, ok: true } });
        const started = performance.now();
        let seq = firstSeq;
        for (let frame = 0; frame < 120; frame += 1) {
          const batch = Array.from({ length: 100 }, () => event(seq++));
          harnessRememberEvents(batch);
          scheduleHarnessStreamRender();
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        }
        const stream = document.getElementById("harnessStream");
        return { elapsed_ms: performance.now() - started, cursor: harnessCursor, cached: harnessCachedEvents().length, cards: stream.querySelectorAll(".h-event").length, nodes: stream.querySelectorAll("*").length, last: seq - 1 };
      }, { firstSeq: first });
      const cycle1 = await stressCycle(6001);
      const memory1 = await memoryMetrics();
      const cycle2 = await stressCycle(18001);
      const memory2 = await memoryMetrics();
      metrics.harness.memory = { baseline: memory0, after_cycle_1: memory1, after_cycle_2: memory2, cycle_1: cycle1, cycle_2: cycle2, collection: "CDP HeapProfiler.collectGarbage before every Performance.getMetrics sample" };
      record("dom-memory", "30,000 events leave a 5,000-event cache and an advancing authoritative cursor", cycle2.cached === CACHE_LIMIT && cycle2.cursor === 30000 && cycle2.last === 30000, `cached=${cycle2.cached} cursor=${cycle2.cursor} last=${cycle2.last}`);
      record("dom-memory", "the rendered trajectory remains a 300-card bounded DOM after both 12,000-event cycles", cycle1.cards === RENDER_WINDOW && cycle2.cards === RENDER_WINDOW && cycle2.nodes <= RENDER_WINDOW * 8, `cycle1=${cycle1.cards}/${cycle1.nodes} cycle2=${cycle2.cards}/${cycle2.nodes} (cards/nodes)`);
      const nodeDelta = memory2.Nodes - memory1.Nodes;
      record("dom-memory", "post-GC document nodes plateau instead of growing with the event count", nodeDelta <= DOM_PLATEAU_NODES && memory2.Nodes <= memory0.Nodes + DOM_PLATEAU_NODES * 2, `baseline=${memory0.Nodes} cycle1=${memory1.Nodes} cycle2=${memory2.Nodes} delta=${nodeDelta}`);
      const heapCycleDelta = memory2.JSHeapUsedSize - memory1.JSHeapUsedSize;
      const heapBaselineDelta = memory2.JSHeapUsedSize - memory0.JSHeapUsedSize;
      record("dom-memory", "post-GC JS heap remains within the declared repeated-cycle plateau budgets", heapCycleDelta <= HEAP_PLATEAU_BYTES && heapBaselineDelta <= HEAP_BASELINE_BYTES, `baseline=${memory0.JSHeapUsedSize} cycle1=${memory1.JSHeapUsedSize} cycle2=${memory2.JSHeapUsedSize} cycle_delta=${heapCycleDelta} baseline_delta=${heapBaselineDelta}`);

      const reader = await page.evaluate(() => {
        const stream = document.getElementById("harnessStream");
        const reasoningSeq = harnessCursor + 1;
        harnessRememberEvents([{ seq: reasoningSeq, type: "assistant/message", ts: 1700000000 + reasoningSeq, step: 1, model: "uiv2-29", reasoning: "Keep this disclosure open while the window slides.", content: "Reader preservation probe." }]);
        renderStoredHarnessEvents(harnessCachedEvents());
        stream.scrollTop = 480;
        const before = stream.scrollTop;
        const head = stream.querySelector(".h-event");
        const leavingHeight = head ? head.getBoundingClientRect().height : 0;
        const details = [...stream.querySelectorAll(".h-event details")];
        const opened = details[Math.min(4, details.length - 1)];
        if (opened) opened.open = true;
        const openedCard = opened && opened.closest(".h-event");
        const seq = Number(harnessCursor) + 1;
        harnessRememberEvents([{ seq, type: "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: "reader", ok: true } }]);
        renderStoredHarnessEvents(harnessCachedEvents());
        const stillOpen = openedCard && openedCard.isConnected && opened.open;
        return { before, after: stream.scrollTop, leavingHeight, details: details.length, stillOpen: Boolean(stillOpen), atBottom: Math.abs(stream.scrollHeight - stream.scrollTop - stream.clientHeight) < 4 };
      });
      const readerDrift = Math.abs(reader.before - reader.leavingHeight - reader.after);
      record("dom-memory", "a full-window append preserves the reader offset and an open retained disclosure", readerDrift <= 2 && !reader.atBottom && reader.details > 0 && reader.stillOpen, { ...reader, drift: readerDrift });

      const steady = await page.evaluate(async () => {
        const original = window.renderStoredHarnessEvents;
        const samples = [];
        window.renderStoredHarnessEvents = function (...callArgs) {
          const started = performance.now();
          try { return original.apply(this, callArgs); } finally { samples.push(performance.now() - started); }
        };
        let seq = harnessCursor + 1;
        for (let frame = 0; frame < 20; frame += 1) {
          for (let k = 0; k < 10; k += 1) {
            harnessRememberEvents([{ seq, type: "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: "frame", ok: true } }]);
            seq += 1;
          }
          scheduleHarnessStreamRender();
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        }
        window.renderStoredHarnessEvents = original;
        const sorted = samples.slice().sort((a, b) => a - b);
        return { calls: samples.length, mean_ms: samples.reduce((a, b) => a + b, 0) / samples.length, p95_ms: sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)], max_ms: sorted[sorted.length - 1], cards: document.querySelectorAll("#harnessStream .h-event").length };
      });
      record("dom-memory", "ten-event frames retain the existing four-millisecond full-window budget", steady.calls === 20 && steady.mean_ms <= FRAME_BUDGET_MS && steady.cards === RENDER_WINDOW, steady);
      metrics.harness.steady = steady;

      for (const viewport of VIEWPORTS) {
        for (const theme of THEMES) {
          // Keep the same document/session alive: toggling mobile emulation can force a Chromium
          // reload. The 390px CSS viewport is the responsive boundary under test; touch recovery is
          // covered by the preservation walks rather than by destroying this populated stress state.
          await page.setViewport({ width: viewport.width, height: viewport.height, deviceScaleFactor: 1 });
          await page.evaluate((selected) => { localStorage.setItem("m5_theme", selected); applyTheme(selected); switchTab("harness"); }, theme);
          await sleep(args.settleMs * 4);
          const layout = await page.evaluate(async () => {
            const rect = (selector) => {
              const node = document.querySelector(selector);
              if (!node) return null;
              const box = node.getBoundingClientRect();
              return { x: box.x, y: box.y, width: box.width, height: box.height };
            };
            const geometry = () => ({ topbar: rect(".topbar"), stream: rect("#harnessStream"), composer: rect("#harnessPromptInput") });
            const before = geometry();
            const shifts = [];
            const streamNode = document.getElementById("harnessStream");
            const streamRect = streamNode.getBoundingClientRect();
            const insideRect = (box) => box && box.x >= streamRect.x - 1 && box.y >= streamRect.y - 1
              && box.x + box.width <= streamRect.right + 1 && box.y + box.height <= streamRect.bottom + 1;
            const observer = new PerformanceObserver((list) => {
              for (const entry of list.getEntries()) {
                if (entry.hadRecentInput) continue;
                const sources = [...entry.sources];
                // Streaming intentionally moves cards inside their own scroller. A shell shift is
                // different: attribute every source, including detached nodes by their old/new
                // rectangles, and fail closed when an entry has no attributable source.
                const external = !sources.length || sources.some((source) => {
                  if (source.node) return !streamNode.contains(source.node);
                  return !(insideRect(source.previousRect) && insideRect(source.currentRect));
                });
                shifts.push({ value: entry.value, external });
              }
            });
            observer.observe({ type: "layout-shift", buffered: false });
            let seq = harnessCursor + 1;
            for (let frame = 0; frame < 10; frame += 1) {
              for (let k = 0; k < 10; k += 1) {
                harnessRememberEvents([{ seq, type: "tool/call", ts: 1700000000 + seq, payload: { name: "grep_search", detail: "layout", ok: true } }]);
                seq += 1;
              }
              scheduleHarnessStreamRender();
              await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
            }
            observer.disconnect();
            const totalCls = shifts.reduce((total, shift) => total + shift.value, 0);
            const externalCls = shifts.filter((shift) => shift.external).reduce((total, shift) => total + shift.value, 0);
            return { before, after: geometry(), cls: totalCls, externalCls, internalCls: totalCls - externalCls, shifts: shifts.length, overflow: document.documentElement.scrollWidth - window.innerWidth, cards: document.querySelectorAll("#harnessStream .h-event").length, cursor: harnessCursor };
          });
          const deltas = [];
          for (const region of ["topbar", "stream", "composer"]) {
            for (const field of ["x", "y", "width", "height"]) {
              const a = layout.before[region] && layout.before[region][field];
              const b = layout.after[region] && layout.after[region][field];
              deltas.push(Math.abs(Number(a) - Number(b)));
            }
          }
          const stable = deltas.every((delta) => Number.isFinite(delta) && delta <= 0.5);
          const passed = stable && layout.overflow <= 0 && layout.externalCls <= LAYOUT_SHIFT_BUDGET && layout.cards === RENDER_WINDOW;
          const detail = { cls_total: Number(layout.cls.toFixed(5)), cls_internal_stream: Number(layout.internalCls.toFixed(5)), cls_external_shell: Number(layout.externalCls.toFixed(5)), shifts: layout.shifts, overflow: layout.overflow, max_geometry_delta: Math.max(...deltas), cards: layout.cards, cursor: layout.cursor };
          record("layout", `${viewport.label} ${theme}: streaming shifts neither shell geometry nor the page`, passed, detail, { viewport: viewport.label, theme, pane: "harness" });
          metrics.layout.push({ viewport: viewport.label, theme, ...detail });
          await shoot(page, `layout/${theme}/${viewport.label}-harness`, { group: "layout", viewport: viewport.label, theme, pane: "harness", state: "long-content" });
        }
      }
      record("browser", "no page error or outbound request occurred", errors.length === 0 && outbound.length === 0, `errors=${errors.slice(0, 3).join(" | ") || "none"} outbound=${[...new Set(outbound)].join(",") || "none"}`);

      const environment = {
        node: process.version, platform: platform(), arch: arch(), cpu_count: cpus().length,
        cpu_model: cpus()[0] ? cpus()[0].model : "unknown", chrome_version: browserVersion,
        chrome_headless: true, python: execFileSync(process.env.STUDIO_PYTHON || "python3", ["--version"], { encoding: "utf8" }).trim(),
        user_agent: await page.evaluate(() => navigator.userAgent), fixture: "council_readiness_seed.py; real loopback app and SSE provider",
        memory_method: "CDP Performance.getMetrics after HeapProfiler.collectGarbage", layout_method: "PerformanceObserver layout-shift plus shell getBoundingClientRect before/after",
      };
      const failed = results.filter((row) => !row.passed);
      const manifest = {
        story: STORY, git: gitState(), source_sha256: sourceHashes(), generated_at: new Date().toISOString(),
        environment,
        method: { slow_sse: "0.35s/word paced real provider", fast_sse: "0.004s/word real provider; production schedule/render wrappers", harness_stress: "two 12,000-event cycles at 100 events/frame plus 10-event timing frames", external_requests: "blocked except the isolated loopback fixture" },
        thresholds: { render_window: RENDER_WINDOW, cache_limit: CACHE_LIMIT, frame_budget_ms: FRAME_BUDGET_MS, heap_plateau_bytes: HEAP_PLATEAU_BYTES, heap_baseline_bytes: HEAP_BASELINE_BYTES, dom_plateau_nodes: DOM_PLATEAU_NODES, layout_shift_budget: LAYOUT_SHIFT_BUDGET, min_fast_sse_per_second: MIN_FAST_SSE_PER_SECOND },
        totals: { checks: results.length, passed: results.length - failed.length, failed: failed.length, captures: captures.length },
        metrics, checks: results, captures,
      };
      mkdirSync(args.screenshots, { recursive: true });
      writeFileSync(resolve(args.screenshots, "capture-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
      if (captures.length !== EXPECTED_CAPTURES) throw new Error(`capture accounting drifted: ${captures.length} != ${EXPECTED_CAPTURES}`);
      console.log(JSON.stringify({ checks: manifest.totals, manifest: resolve(args.screenshots, "capture-manifest.json") }, null, 2));
      if (failed.length) process.exitCode = 1;
    } finally { await browser.close(); }
  } finally { if (fixture && fixture.child) fixture.child.kill("SIGTERM"); }
}

run(parseArgs(process.argv.slice(2))).catch((error) => { console.error(`UIV2-29 browser gate: ${error.stack || error.message}`); process.exit(1); });
