#!/usr/bin/env node
/**
 * Measure what a stream costs the pane it is streaming into (UIUX-P5-04).
 *
 *   node scripts/stream_perf_walk.mjs --screenshots docs/media/stream-perf
 *
 * Two defects, both invisible on a short reply on a fast machine and both paid by a reader watching
 * a long one:
 *
 * * the chat transcript re-rendered **every message** for **every token** - on a 60-message
 *   conversation that was 12 ms of main-thread work per token, so a 400-token answer cost about five
 *   seconds of stutter on top of the wait;
 * * the harness stream rebuilt **every card** for **every event** and kept every event for ever - 3.5
 *   ms per event at 800 events and climbing, with an unbounded DOM behind it.
 *
 * What this walk proves, on a real streamed reply and on synthetic bursts:
 *
 *   1. a reply still arrives, still reads correctly, and still ends in `complete` (the fix is not a
 *      reason for a transcript to lose a word);
 *   2. a token costs the message being written, not the conversation around it: the cost at 60
 *      messages is within 2x the cost at 6;
 *   3. a burst of tokens is coalesced into one render per frame, measured by counting the DOM
 *      rewrites the burst actually caused;
 *   4. a live event appends one card instead of rebuilding the window, and never draws a card twice;
 *   5. the stream holds a bounded number of cards however long the log is, and says how many it is
 *      holding back;
 *   6. the cache behind it is bounded too, while the cursor still names the next event to fetch;
 *   7. a reader who is at the bottom follows the stream, and a reader who scrolled up is left alone
 *      and told a frame landed;
 *   8. no page error.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 240,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
};

/** The budget a token may cost on a 60-message transcript, in milliseconds. */
const TOKEN_BUDGET_MS = 3;
/** The budget a live event may cost when it only adds a card. */
const EVENT_BUDGET_MS = 2;
/** The number of cards the harness stream may keep on screen. */
const RENDER_WINDOW = 300;
/** The number of events the client cache may keep. */
const CACHE_LIMIT = 5000;

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false };
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
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
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
    "Measure the cost of a streamed reply and a streamed event log.",
    "",
    "  node scripts/stream_perf_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/stream_perf_walk.mjs --base-url URL --token TOKEN",
    "",
    "Without --base-url the walk starts the Compare fixture itself. Needs Chrome/Chromium",
    "(STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE). See",
    "docs/uiux-2-0-stream-performance.md.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
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
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This script refuses to report a walk it did not perform.",
  );
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (_error) {
      // not up yet
    }
    await sleep(150);
  }
  return false;
}

async function startFixture() {
  // The local model is paced by the fixture. A real runtime streams at its own speed and a frame
  // taken mid-reply would be luck; a paced one makes "this is what streaming looks like" a fact the
  // walk can stand behind. Nothing else in the walk depends on the pace.
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--port", "0", "--slow-model", "Qwen3.8-27B-oQ4e-mtp"],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)),
      120000,
    );
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        ready(JSON.parse(line));
      } catch (_error) {
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
    });
    function ready(value) { resolveReady(value); }
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { ...ready, child };
}

async function shoot(page, args, name) {
  if (!args.screenshots) return;
  mkdirSync(args.screenshots, { recursive: true });
  await sleep(args.settleMs);
  await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
}

/** Select the seeded local model and turn the tool loop off: a plain reply is the thing measured. */
const PICK_LOCAL_MODEL = () => {
  const select = document.getElementById("chatModel");
  if (!select) return "";
  const local = [...select.options].find((option) => /local|omlx|m5/i.test(`${option.value} ${option.textContent}`));
  if (local) {
    select.value = local.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const tools = document.getElementById("chatToolsEnabled");
  if (tools && tools.checked) {
    tools.checked = false;
    tools.dispatchEvent(new Event("change", { bubbles: true }));
  }
  return local ? local.value : "";
};

/** Time a burst of token renders against a transcript of `count` messages. */
const TIME_TOKENS = (count, tokens) => {
  const build = (n) => Array.from({ length: n }, (_, i) => ({
    role: i % 2 ? "assistant" : "user",
    content: `Paragraph ${i} `.repeat(40),
    model: i % 2 ? "m5-local" : undefined,
    status: i === n - 1 ? "streaming" : (i % 2 ? "complete" : undefined),
    createdAt: Date.now() - (n - i) * 1000,
    usage: i % 2 && i !== n - 1 ? { total_tokens: 120 } : undefined,
  }));
  const saved = chatHistory.slice();
  chatHistory = build(count);
  renderChatMessages();
  const started = performance.now();
  for (let i = 0; i < tokens; i += 1) {
    chatHistory[chatHistory.length - 1].content += " word";
    renderChatMessages();
  }
  const msPerToken = (performance.now() - started) / tokens;
  const nodes = document.getElementById("chatMessages").querySelectorAll("*").length;
  chatHistory = saved;
  renderChatMessages();
  return { msPerToken, nodes };
};

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture();
      args.baseUrl = fixture.base_url;
      args.token = args.token || fixture.token;
    }

    const browser = await puppeteer.launch({
      executablePath: resolveChrome(),
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"],
    });
    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(`console: ${message.text()}`);
      });
      await page.evaluateOnNewDocument((key, token) => {
        try {
          window.sessionStorage.setItem(key, token);
          localStorage.setItem("m5_theme", "dark");
        } catch (_error) {}
      }, args.tokenKey, args.token);
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 8);

      // ── 1. a real streamed reply, still correct ───────────────────────────
      await page.evaluate(() => switchTab("chat"));
      await sleep(args.settleMs * 2);
      const model = await page.evaluate(PICK_LOCAL_MODEL);
      await sleep(args.settleMs * 2);
      await page.evaluate(() => {
        const input = document.getElementById("chatInput");
        input.value = "In two sentences, say why a browser should not re-render a whole transcript for every token.";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await page.click("#chatSendBtn");
      let midStreamSeen = false;
      let midStreamChars = 0;
      let finalState = "none";
      const startedAt = Date.now();
      for (let i = 0; i < 500; i += 1) {
        await sleep(60);
        const snapshot = await page.evaluate(() => {
          const node = document.querySelector("#chatMessages .msg.assistant");
          return node ? { state: node.dataset.state, text: (node.textContent || "").trim() } : null;
        });
        // Capture the first frame with words on it: a screenshot taken after the last token is a
        // picture of the finished reply, whatever it is called.
        if (snapshot && snapshot.state === "streaming" && !midStreamSeen && snapshot.text.length >= 4) {
          midStreamSeen = true;
          midStreamChars = snapshot.text.length;
          await shoot(page, args, "stream-chat-midstream");
        }
        if (snapshot && snapshot.state !== "streaming") {
          finalState = snapshot.state;
          break;
        }
        if (Date.now() - startedAt > 40000) break;
      }
      const transcript = await page.evaluate(() => [...document.querySelectorAll("#chatMessages .msg")].map((node) => ({
        role: node.dataset.role,
        state: node.dataset.state,
        chars: (node.querySelector(".msg-meta") ? node.textContent.replace(node.querySelector(".msg-meta").textContent, "") : node.textContent || "").trim().length,
      })));
      await shoot(page, args, "stream-chat-complete");
      const assistant = transcript.find((entry) => entry.role === "assistant");
      record(
        "a streamed reply arrives, reads, and ends in complete",
        finalState === "complete" && transcript.length === 2 && assistant && assistant.chars > 5,
        `model=${model || "default"} state=${finalState} messages=${transcript.length} assistant chars=${assistant ? assistant.chars : 0}`,
      );
      const capturedPartial = Boolean(assistant) && midStreamSeen && midStreamChars < assistant.chars;
      record(
        "the reply was captured while it was still arriving, not after it landed",
        capturedPartial,
        midStreamSeen
          ? `captured at ${midStreamChars} chars, finished at ${assistant ? assistant.chars : 0}`
          : "no streaming frame observed",
      );
      if (args.screenshots) {
        const digest = (name) => createHash("sha256").update(readFileSync(resolve(args.screenshots, `${name}.png`))).digest("hex");
        const differs = digest("stream-chat-midstream") !== digest("stream-chat-complete");
        record(
          "the two chat captures are different pictures",
          differs,
          differs ? "mid-stream and complete differ" : "both captures are the same frame",
        );
      }

      // ── 2. a token costs the message, not the conversation ────────────────
      const small = await page.evaluate(TIME_TOKENS, 6, 120);
      const large = await page.evaluate(TIME_TOKENS, 60, 120);
      record(
        `a token costs at most ${TOKEN_BUDGET_MS}ms on a 60-message transcript`,
        large.msPerToken <= TOKEN_BUDGET_MS,
        `${large.msPerToken.toFixed(2)}ms/token at 60 messages`,
      );
      record(
        "and does not grow with the transcript: 60 messages cost at most 2x what 6 do",
        large.msPerToken <= small.msPerToken * 2 + 0.25,
        `${small.msPerToken.toFixed(2)}ms at 6 messages vs ${large.msPerToken.toFixed(2)}ms at 60`,
      );

      // ── 3. a burst is coalesced into one render per frame ─────────────────
      const coalesced = await page.evaluate(async () => {
        const box = document.getElementById("chatMessages");
        const saved = chatHistory.slice();
        chatHistory = Array.from({ length: 20 }, (_, i) => ({
          role: i % 2 ? "assistant" : "user",
          content: `Turn ${i}`,
          status: i === 19 ? "streaming" : undefined,
          createdAt: Date.now(),
        }));
        renderChatMessages();
        let rewrites = 0;
        const observer = new MutationObserver((records) => { rewrites += records.length; });
        observer.observe(box, { childList: true, subtree: true });
        for (let i = 0; i < 60; i += 1) {
          chatHistory[chatHistory.length - 1].content += " word";
          scheduleChatRender();
        }
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        observer.disconnect();
        chatHistory = saved;
        renderChatMessages();
        return { rewrites, scheduled: 60 };
      });
      record(
        "60 tokens scheduled in one frame cause one render, not sixty",
        coalesced.rewrites <= 4,
        `${coalesced.scheduled} schedules → ${coalesced.rewrites} DOM rewrites`,
      );

      // ── 4. a live event appends one card ──────────────────────────────────
      const harness = await page.evaluate(() => {
        const event = (seq) => ({
          seq,
          type: seq % 9 === 0 ? "turn/end" : "tool/call",
          ts: Math.floor(Date.now() / 1000) - (6000 - seq),
          payload: { name: "grep_search", detail: "x".repeat(120), ok: true },
        });
        const savedEntries = Array.from(harnessEventCache.entries());
        const savedCursor = harnessCursor;
        harnessResetEventCache();
        harnessRememberEvents(Array.from({ length: 200 }, (_, i) => event(i + 1)));
        renderStoredHarnessEvents(harnessCachedEvents());
        const cardsBefore = document.getElementById("harnessStream").querySelectorAll(".h-event").length;
        const started = performance.now();
        const arrivals = 200;
        for (let i = 0; i < arrivals; i += 1) {
          harnessRememberEvents([event(201 + i)]);
          renderStoredHarnessEvents(harnessCachedEvents());
        }
        const msPerEvent = (performance.now() - started) / arrivals;
        const stream = document.getElementById("harnessStream");
        const cards = [...stream.querySelectorAll(".h-event")].map((node) => Number(node.dataset.seq));
        const duplicates = cards.length - new Set(cards).size;
        const inOrder = cards.every((seq, index) => index === 0 || seq > cards[index - 1]);
        const scrolledToBottom = Math.abs(stream.scrollHeight - stream.scrollTop - stream.clientHeight) < 4;
        harnessEventCache = new Map(savedEntries);
        harnessCursor = savedCursor;
        return { msPerEvent, cardsBefore, cardsAfter: cards.length, duplicates, inOrder, scrolledToBottom };
      });
      const expectedCards = Math.min(harness.cardsBefore + 200, RENDER_WINDOW);
      record(
        `a live event appends one card, at most ${EVENT_BUDGET_MS}ms`,
        harness.msPerEvent <= EVENT_BUDGET_MS && harness.cardsAfter === expectedCards && harness.duplicates === 0 && harness.inOrder,
        `${harness.msPerEvent.toFixed(2)}ms/event, ${harness.cardsBefore} → ${harness.cardsAfter} cards (expected ${expectedCards}), duplicates=${harness.duplicates}, in order=${harness.inOrder}`,
      );
      record(
        "a reader who is at the bottom follows the stream",
        harness.scrolledToBottom,
        `at bottom after 200 arrivals: ${harness.scrolledToBottom}`,
      );

      // ── 5/6. the window and the cache are bounded ─────────────────────────
      const bounded = await page.evaluate(() => {
        const event = (seq) => ({
          seq,
          type: "tool/call",
          ts: Math.floor(Date.now() / 1000) - (8000 - seq),
          payload: { name: "grep_search", detail: "x".repeat(80), ok: true },
        });
        const savedEntries = Array.from(harnessEventCache.entries());
        const savedCursor = harnessCursor;
        const savedActive = activeHarnessId;
        harnessResetEventCache();
        harnessRememberEvents(Array.from({ length: 8000 }, (_, i) => event(i + 1)));
        renderStoredHarnessEvents(harnessCachedEvents());
        // The timeline is the pane's own statement of what it holds and where its cursor is; it
        // only draws for a session, so it is asked with one rather than with a bare cache.
        activeHarnessId = savedActive || "walk-fixture";
        renderHarnessTimeline();
        const stream = document.getElementById("harnessStream");
        const note = document.getElementById("harnessWindowNote");
        const timelineCount = document.getElementById("harnessTimelineCount");
        const timelineState = document.getElementById("harnessTimelineState");
        const result = {
          cached: harnessCachedEvents().length,
          cards: stream.querySelectorAll(".h-event").length,
          nodes: stream.querySelectorAll("*").length,
          note: note ? note.textContent.trim() : "",
          timeline: timelineCount ? timelineCount.textContent.trim() : "",
          state: timelineState ? timelineState.textContent.trim() : "",
          cursor: harnessCursor,
        };
        harnessEventCache = new Map(savedEntries);
        harnessCursor = savedCursor;
        activeHarnessId = savedActive;
        return result;
      });
      record(
        `the stream holds at most ${RENDER_WINDOW} cards however long the log is`,
        bounded.cards <= RENDER_WINDOW && bounded.nodes <= RENDER_WINDOW * 8,
        `${bounded.cards} cards / ${bounded.nodes} nodes for an 8000-event log`,
      );
      record(
        "it says how many it is holding back, and names the window and the cursor it holds",
        /newest 300 of \d+/.test(bounded.note) && /5000 events/.test(bounded.timeline) && /cursor 8000/.test(bounded.state),
        `note "${bounded.note}" · timeline "${bounded.timeline}" · state "${bounded.state}"`,
      );
      record(
        `the cache behind it is bounded at ${CACHE_LIMIT} events, and the cursor still advances`,
        bounded.cached <= CACHE_LIMIT && bounded.cached > 0 && bounded.cursor === 8000,
        `${bounded.cached} cached, cursor ${bounded.cursor}`,
      );
      await page.evaluate(() => switchTab("harness"));
      await sleep(args.settleMs * 2);
      await shoot(page, args, "stream-harness-window");

      // ── 6b. a full window keeps its frame budget ──────────────────────────
      // The window is full here, so every arrival also pushes a card off the top: the case a long
      // live run spends all its time in, and the one a "we bounded the DOM" story must not pay for
      // with a rebuild per event.
      const steady = await page.evaluate(async () => {
        const event = (seq) => ({
          seq,
          type: "tool/call",
          ts: Math.floor(Date.now() / 1000) - (9000 - seq),
          payload: { name: "grep_search", detail: "x".repeat(120), ok: true },
        });
        const savedEntries = Array.from(harnessEventCache.entries());
        const savedCursor = harnessCursor;
        harnessResetEventCache();
        harnessRememberEvents(Array.from({ length: 5000 }, (_, i) => event(i + 1)));
        renderStoredHarnessEvents(harnessCachedEvents());
        const original = window.renderStoredHarnessEvents;
        let total = 0;
        let calls = 0;
        window.renderStoredHarnessEvents = function (...callArgs) {
          const started = performance.now();
          try {
            return original.apply(this, callArgs);
          } finally {
            total += performance.now() - started;
            calls += 1;
          }
        };
        const frames = 20;
        let seq = 5001;
        for (let frame = 0; frame < frames; frame += 1) {
          for (let k = 0; k < 10; k += 1) {
            harnessRememberEvents([event(seq)]);
            seq += 1;
            scheduleHarnessStreamRender();
          }
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        }
        window.renderStoredHarnessEvents = original;
        const stream = document.getElementById("harnessStream");
        const result = {
          msPerFrame: total / frames,
          calls,
          frames,
          cards: stream.querySelectorAll(".h-event").length,
          nodes: stream.querySelectorAll("*").length,
        };
        harnessEventCache = new Map(savedEntries);
        harnessCursor = savedCursor;
        return result;
      });
      record(
        "a full window absorbs 10 events a frame inside one frame budget",
        steady.msPerFrame <= 4 && steady.calls <= steady.frames + 2 && steady.cards === RENDER_WINDOW,
        `${steady.msPerFrame.toFixed(2)}ms/frame over ${steady.calls} renders for ${steady.frames} frames of 10 events, ${steady.cards} cards`,
      );

      // ── 7. a reader who scrolled up is left alone ─────────────────────────
      // The window is full, so the arriving card also pushes one off the top: the case where a
      // follower's scroll has to be maintained rather than dropped or jumped to the bottom.
      const reader = await page.evaluate(() => {
        const event = (seq) => ({ seq, type: "tool/call", ts: Math.floor(Date.now() / 1000) - (1000 - seq), payload: { name: "grep_search", detail: "y".repeat(120), ok: true } });
        const savedEntries = Array.from(harnessEventCache.entries());
        const savedCursor = harnessCursor;
        harnessResetEventCache();
        harnessRememberEvents(Array.from({ length: 400 }, (_, i) => event(i + 1)));
        renderStoredHarnessEvents(harnessCachedEvents());
        const stream = document.getElementById("harnessStream");
        stream.scrollTop = 240;
        const before = stream.scrollTop;
        const head = stream.querySelector(".h-event");
        const leavingHeight = head ? head.getBoundingClientRect().height : 0;
        harnessRememberEvents([event(401)]);
        renderStoredHarnessEvents(harnessCachedEvents());
        const after = stream.scrollTop;
        const atBottom = Math.abs(stream.scrollHeight - stream.scrollTop - stream.clientHeight) < 4;
        const landed = document.getElementById("harnessTimelineCount");
        const fresh = landed ? landed.dataset.fresh === "true" : false;
        harnessEventCache = new Map(savedEntries);
        harnessCursor = savedCursor;
        return { before, after, leavingHeight, atBottom, fresh };
      });
      // Two ways to fail: pull the reader to the bottom, or leave the numbers where they were while
      // the words slide up. Neither is allowed; the height of the card that left is the adjustment.
      const drift = Math.abs(reader.before - reader.leavingHeight - reader.after);
      record(
        "a reader who scrolled up keeps the words they were reading, and is told a frame landed",
        drift <= 2 && !reader.atBottom && reader.fresh,
        `scrollTop ${reader.before} → ${reader.after} while a ${reader.leavingHeight.toFixed(1)}px card left (drift ${drift.toFixed(1)}px), at bottom=${reader.atBottom}, fresh badge=${reader.fresh}`,
      );

      // ── 8. narrow, and no page error ──────────────────────────────────────
      await page.setViewport({ width: 360, height: 740, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 4);
      const narrow = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - window.innerWidth,
      }));
      await page.evaluate(() => switchTab("harness"));
      await sleep(args.settleMs * 3);
      await shoot(page, args, "stream-harness-window-narrow");
      record("the bounded stream fits a 360px phone without a sideways scroll", narrow.overflow <= 0, `overflow=${narrow.overflow}`);
      record("no page error occurred", errors.length === 0, errors.slice(0, 3).join(" | "));

      const failed = results.filter((entry) => !entry.passed);
      if (args.json) {
        console.log(JSON.stringify({ checks: results, passed: results.length - failed.length, total: results.length }, null, 2));
      } else {
        console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
      }
      if (failed.length) process.exitCode = 1;
    } finally {
      await browser.close();
    }
  } finally {
    if (fixture && fixture.child) fixture.child.kill("SIGTERM");
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(`stream performance walk: ${error.message}`);
  process.exit(1);
});
