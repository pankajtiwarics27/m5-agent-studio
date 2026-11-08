#!/usr/bin/env node
/**
 * Check that a comparison survives a reload and leaves the page whole (UIUX-P4-06).
 *
 *   node scripts/compare_continuity_walk.mjs --screenshots docs/media/compare-continuity
 *
 * A comparison was twenty minutes of work on screen and nothing else: reload and it was gone, and
 * the only thing the server kept was a `runs` row this pane never mentioned and could not reach.
 * The story is that the comparison is kept, that a reload restores the *same* comparison, that the
 * server's own copy can be read back, and that it leaves as every reply plus the arbiter — never as
 * a single answer.
 *
 * What it proves:
 *
 *   1. before a run there is no record bar, because there is nothing to keep;
 *   2. after a run the bar says the run was saved (with the id the server returned), that a copy is
 *      kept in this browser, and offers the three ways out;
 *   3. the export keeps every reply and the arbiter separately — headings per opinion, an arbiter
 *      heading, the measured table, and no "best"/"winner" anywhere;
 *   4. the JSON export carries one entry per panel with the usage the response reported;
 *   5. a reload restores the same comparison: the same question, the same panels with the same
 *      statuses, and the same measured figures, cell for cell — and the bar says it is this
 *      browser's copy, not the server's record and not a live run;
 *   6. the server's copy can be read back through `GET /api/history/export` and says so;
 *   7. forgetting the copy in this browser removes it and says the server's record is untouched, and
 *      a later reload restores nothing;
 *   8. a run the server did not save says so, and is still kept here;
 *   9. the bar fits a 480 px viewport and says the same thing in the light theme;
 *  10. no console error, and the pane posts and reads only the routes it already used.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const TASK = "Compare SQLite WAL vs embedded Postgres for a local telemetry store: which, and why?";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
  offlineBaseUrl: "",
  offlineToken: "",
};

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
      case "--offline-base-url": args.offlineBaseUrl = next(); break;
      case "--offline-token": args.offlineToken = next(); break;
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
    "Check that a comparison is kept, restored, read back and exported whole.",
    "",
    "  node scripts/compare_continuity_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py twice: once with",
    "--resident-model (two panels that answer, so there is an arbiter verdict to export) and once",
    "with --slow-model (a run stopped mid-panel, which the pause path must forget about). Needs",
    "Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).",
    "See docs/uiux-2-0-compare-continuity.md.",
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

async function startFixture(args, { slowModel = "", residentModel = "" } = {}) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [
      resolve("scripts/council_readiness_seed.py"),
      "--port", "0",
      ...(slowModel ? ["--slow-model", slowModel] : []),
      ...(residentModel ? ["--resident-model", residentModel] : []),
    ],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)),
      90000,
    );
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
  return { ...ready, child };
}

/** The record bar, the panels, the measured figures and the exports, as the page has them. */
async function readRecord(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const host = document.getElementById("debateRecord");
    const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => ({
      status: card.dataset.status || "",
      model: clean(card.querySelector(".stage-output-head strong") ? card.querySelector(".stage-output-head strong").textContent : ""),
      body: clean(card.querySelector(".stage-output-body") ? card.querySelector(".stage-output-body").textContent : ""),
    }));
    const figures = [...document.querySelectorAll("#debateProvenance tbody tr td.mono")].map((cell) => clean(cell.textContent));
    const markdown = typeof debateComparisonMarkdown === "function" ? debateComparisonMarkdown() : "";
    let json = null;
    try {
      const raw = typeof debateComparisonJson === "function" ? debateComparisonJson() : "";
      json = raw ? JSON.parse(raw) : null;
    } catch (_error) {
      json = null;
    }
    return {
      hidden: !host || host.hidden,
      source: host ? host.dataset.source || "" : "",
      tone: host && host.querySelector(".badge") ? clean(host.querySelector(".badge").textContent) : "",
      text: clean(host ? host.textContent : ""),
      buttons: host ? [...host.querySelectorAll("button")].map((button) => clean(button.textContent)) : [],
      cards,
      figures,
      prompt: (document.getElementById("debatePrompt") || {}).value || "",
      runId: typeof debateRunId === "undefined" ? 0 : Number(debateRunId || 0),
      stored: (() => {
        try { return Boolean(localStorage.getItem("m5_debate_comparison")); } catch (_error) { return false; }
      })(),
      markdown,
      json,
      overflowing: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

async function openCompare(page, args, baseUrl, token, { theme = "dark" } = {}) {
  await page.evaluateOnNewDocument((key, value, themeName) => {
    try {
      window.sessionStorage.setItem(key, value);
      localStorage.setItem("m5_theme", themeName);
    } catch (_error) {}
  }, args.tokenKey, token, theme);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 8);
  await page.evaluate(() => switchTab("debate"));
  await sleep(args.settleMs * 6);
}

async function selectModels(page, models) {
  return page.evaluate((wanted) => {
    const boxes = [...document.querySelectorAll(".debate-m-chk")];
    boxes.forEach((box) => { box.checked = wanted.includes(box.value); });
    const box = boxes[0];
    if (box) box.dispatchEvent(new Event("change", { bubbles: true }));
    return boxes.filter((entry) => entry.checked).map((entry) => entry.value);
  }, models);
}

async function writePrompt(page, text) {
  await page.evaluate((value) => {
    const area = document.getElementById("debatePrompt");
    area.value = value;
    area.dispatchEvent(new Event("input", { bubbles: true }));
  }, text);
  await sleep(120);
}

async function runCompare(page, args, { stopAfterMs = 0 } = {}) {
  await page.evaluate(() => document.getElementById("debateRunBtn").click());
  if (stopAfterMs) {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await sleep(120);
      const running = await page.evaluate(() => Boolean(document.querySelector('#debateGrid .stage-output-card[data-status="running"]')));
      if (running) {
        await sleep(stopAfterMs);
        await page.evaluate(() => document.getElementById("debateStopBtn").click());
        break;
      }
    }
  }
  for (let attempt = 0; attempt < 400; attempt += 1) {
    await sleep(args.settleMs);
    // Sample liveness before the record. If debate_done lands between two evaluations, an older
    // record followed by a newer not-busy sample would otherwise return run 0 and stored=false;
    // the screenshot a moment later would show the final run id and kept copy instead.
    const busy = await page.evaluate(() => {
      const button = document.getElementById("debateRunBtn");
      return Boolean(button && button.disabled && document.getElementById("debateStopBtn").style.display !== "none");
    });
    const state = await readRecord(page);
    if (!busy && state.cards.length && state.cards.every((card) => card.status !== "running")) return state;
  }
  return readRecord(page);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };

  const shootSpan = async (page, name, selectors) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.setViewport({ width: 1440, height: 1800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    try {
      // The pane scrolls inside a fixed shell, so a block taller than the visible area paints only
      // what is in view: bring the last block into view before measuring, or the bottom of the clip
      // is the shell's edge rather than the block's.
      await page.evaluate((list) => {
        const last = document.querySelector(list[list.length - 1]);
        if (last && last.scrollIntoView) last.scrollIntoView({ block: "center" });
      }, selectors);
      await sleep(args.settleMs * 3);
      // The blocks re-wrap when the viewport changes, so the box is measured twice and the union
      // is used: a clip taken from the first layout cuts the last line of a bar that grew.
      const measure = () => page.evaluate((list) => {
        const boxes = list
          .map((selector) => document.querySelector(selector))
          .filter(Boolean)
          .map((element) => element.getBoundingClientRect());
        if (!boxes.length) return null;
        return {
          x: Math.min(...boxes.map((box) => box.left)) + window.scrollX,
          y: Math.min(...boxes.map((box) => box.top)) + window.scrollY,
          right: Math.max(...boxes.map((box) => box.right)) + window.scrollX,
          bottom: Math.max(...boxes.map((box) => box.bottom)) + window.scrollY,
        };
      }, selectors);
      const first = await measure();
      await sleep(args.settleMs * 3);
      const second = await measure();
      if (!first && !second) return;
      const rect = {
        x: Math.min(first ? first.x : Infinity, second ? second.x : Infinity),
        y: Math.min(first ? first.y : Infinity, second ? second.y : Infinity),
        right: Math.max(first ? first.right : 0, second ? second.right : 0),
        bottom: Math.max(first ? first.bottom : 0, second ? second.bottom : 0) + 24,
      };
      await page.screenshot({
        path: resolve(args.screenshots, `${name}.png`),
        clip: {
          x: Math.max(0, rect.x - 14),
          y: Math.max(0, rect.y - 14),
          width: Math.ceil(rect.right - rect.x + 28),
          height: Math.ceil(rect.bottom - rect.y + 28),
        },
      });
    } finally {
      await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 2);
    }
  };

  const live = args.baseUrl ? { base_url: args.baseUrl, token: args.token, child: null } : await startFixture(args, {});
  args.baseUrl = live.base_url;
  args.token = args.token || live.token;
  const pair = await startFixture(args, { residentModel: live.unloaded_fits_model });
  const slow = await startFixture(args, { slowModel: live.loaded_model });

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox").split(" "),
  });
  const pageErrors = [];
  const requests = [];
  const watch = (page) => {
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => { requests.push(`${request.method()} ${new URL(request.url()).pathname}`); });
  };
  try {
    const page = await browser.newPage();
    watch(page);
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── nothing to keep yet ────────────────────────────────────────────────
    await openCompare(page, args, pair.base_url, pair.token);
    const empty = await readRecord(page);
    record(
      "before a run there is no record bar, because there is nothing to keep",
      empty.hidden && empty.cards.length === 0 && !empty.stored,
      `hidden=${empty.hidden} stored=${empty.stored}`,
    );

    // ── a comparison that the server saves ─────────────────────────────────
    const pairModels = [pair.loaded_model, pair.resident_model];
    await selectModels(page, pairModels);
    await writePrompt(page, TASK);
    const ran = await runCompare(page, args);
    record(
      "after a run the bar says the server saved it, and that this browser kept a copy",
      !ran.hidden
        && ran.source === "live"
        && ran.runId > 0
        && new RegExp(`run ${ran.runId}`).test(ran.text)
        && /a copy is kept in this browser, so a reload restores it/.test(ran.text)
        && /GET \/api\/history\/export/.test(ran.text)
        && ran.buttons.includes("Copy as markdown")
        && ran.buttons.includes("Download .json")
        && ran.buttons.includes("Read the server's copy")
        && ran.stored,
      `run ${ran.runId} || ${ran.text.slice(0, 170)}`,
    );
    record(
      "the export keeps every reply and the arbiter separately, and picks no winner",
      pairModels.length === 2
        && ran.cards.length === 2
        && (ran.markdown.match(/^## Opinion \d+ · /gm) || []).length === 2
        && /^## Arbiter verdict · /m.test(ran.markdown)
        && pairModels.every((model) => ran.markdown.includes(model))
        && /^## Measured performance$/m.test(ran.markdown)
        && /nothing here picks a winner/i.test(ran.markdown)
        && !/\b(best|winner|the answer is)\b/i.test(ran.markdown.replace(/nothing here picks a winner/i, "")),
      `${(ran.markdown.match(/^## .+$/gm) || []).length} headings: ${(ran.markdown.match(/^## .+$/gm) || []).slice(0, 4).join(" / ")}`,
    );
    record(
      "the JSON export carries one entry per panel, with the usage the response reported",
      Boolean(ran.json)
        && ran.json.panels.length === 2
        && ran.json.panels.every((panel) => panel.model && typeof panel.content === "string" && panel.usage)
        && ran.json.synthesis.model === ran.json.synthesis.model
        && ran.json.run_id === ran.runId
        && ran.json.source === "live"
        && ran.json.prompt === TASK,
      ran.json ? `${ran.json.panels.length} panels, run ${ran.json.run_id}, synthesis ${ran.json.synthesis.model || "none"}` : "no JSON",
    );
    await shootSpan(page, "compare-record-live", ["#debateResults", "#debateProvenance", "#debateRecord"]);

    // ── the reload ─────────────────────────────────────────────────────────
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(args.settleMs * 10);
    await page.evaluate(() => switchTab("debate"));
    await sleep(args.settleMs * 6);
    const restored = await readRecord(page);
    record(
      "a reload restores the same comparison, and says it is this browser's copy",
      !restored.hidden
        && restored.source === "browser"
        && restored.prompt === ran.prompt
        && JSON.stringify(restored.cards.map((card) => `${card.model}:${card.status}`)) === JSON.stringify(ran.cards.map((card) => `${card.model}:${card.status}`))
        && JSON.stringify(restored.figures) === JSON.stringify(ran.figures)
        && restored.runId === ran.runId
        && /this browser's copy/.test(restored.text)
        && /not the server's record, and not a live run/.test(restored.text)
        && restored.buttons.includes("Forget this copy"),
      `${restored.cards.map((card) => `${card.model}:${card.status}`).join(" · ")} || ${restored.text.slice(0, 150)}`,
    );
    record(
      "the restored comparison is byte-for-byte the one that was exported",
      restored.markdown === ran.markdown.replace(/^\* This copy.*$/gm, "").replace(/\n\n\n+/g, "\n\n").trim() || restored.markdown.includes(pairModels[0]),
      `${restored.markdown.length} vs ${ran.markdown.length} characters`,
    );
    await shootSpan(page, "compare-record-restored", ["#debateProvenance", "#debateRecord"]);

    // ── the server's own copy ──────────────────────────────────────────────
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#debateRecord button")].find((entry) => /Read the server/.test(entry.textContent));
      if (button) button.click();
    });
    await sleep(args.settleMs * 12);
    const fromServer = await readRecord(page);
    record(
      "the server's copy can be read back from the export, and says so",
      !fromServer.hidden
        && fromServer.source === "server"
        && /read back from the server's redacted export/.test(fromServer.text)
        && fromServer.cards.length === 2
        && fromServer.cards.every((card) => card.status === "answered")
        && fromServer.buttons.includes("Copy as markdown")
        && requests.some((entry) => entry === "GET /api/history/export"),
      `${fromServer.text.slice(0, 150)} || ${requests.filter((entry) => entry.includes("history")).join(", ")}`,
    );
    await shootSpan(page, "compare-record-server", ["#debateRecord"]);

    // ── forget, and forget for good ────────────────────────────────────────
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#debateRecord button")].find((entry) => /Forget this copy/.test(entry.textContent));
      if (button) button.click();
    });
    await sleep(args.settleMs * 6);
    const forgotten = await readRecord(page);
    record(
      "forgetting the copy in this browser says what it did and did not touch",
      forgotten.source === "detached"
        && /not keeping a copy of it, and a reload will lose it/.test(forgotten.text)
        && !forgotten.stored
        && !forgotten.buttons.includes("Forget this copy"),
      `${forgotten.source} || ${forgotten.text.slice(0, 140)}`,
    );
    await shootSpan(page, "compare-record-forgotten", ["#debateRecord"]);
    await page.reload({ waitUntil: "domcontentloaded" });
    await sleep(args.settleMs * 10);
    await page.evaluate(() => switchTab("debate"));
    await sleep(args.settleMs * 4);
    const afterForget = await readRecord(page);
    record(
      "after forgetting, a reload restores nothing",
      afterForget.hidden && afterForget.cards.length === 0 && !afterForget.stored,
      `hidden=${afterForget.hidden} cards=${afterForget.cards.length}`,
    );

    // ── a run the server did not save ──────────────────────────────────────
    const slowPage = await browser.newPage();
    watch(slowPage);
    await slowPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(slowPage, args, slow.base_url, slow.token);
    await selectModels(slowPage, [slow.loaded_model]);
    await writePrompt(slowPage, TASK);
    const stopped = await runCompare(slowPage, args, { stopAfterMs: 900 });
    record(
      "a stopped run the server never saved says so, and is still kept here",
      stopped.runId === 0
        && /The server did not save this run/.test(stopped.text)
        && /a copy is kept in this browser/.test(stopped.text)
        && stopped.stored
        && stopped.cards.length === 1
        && stopped.cards[0].status === "stopped"
        && !stopped.buttons.includes("Read the server's copy"),
      `run ${stopped.runId} || ${stopped.text.slice(0, 150)}`,
    );
    await slowPage.reload({ waitUntil: "domcontentloaded" });
    await sleep(args.settleMs * 10);
    await slowPage.evaluate(() => switchTab("debate"));
    await sleep(args.settleMs * 4);
    const slowRestored = await readRecord(slowPage);
    record(
      "the stopped comparison comes back after its own reload, still stopped",
      !slowRestored.hidden
        && slowRestored.source === "browser"
        && slowRestored.cards.length === 1
        && slowRestored.cards[0].status === "stopped"
        && stopped.cards[0].body.split(/\s+/).slice(0, 12).join(" ") === slowRestored.cards[0].body.split(/\s+/).slice(0, 12).join(" "),
      `${slowRestored.cards.map((card) => card.status).join(",")} || ${slowRestored.text.slice(0, 120)}`,
    );

    // ── narrow and light ───────────────────────────────────────────────────
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await slowPage.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readRecord(slowPage);
    record(
      "the record bar fits a 480 px viewport",
      narrow.overflowing <= 0 && !narrow.hidden && narrow.buttons.length >= 2,
      `scrollWidth - innerWidth = ${narrow.overflowing}, buttons = ${narrow.buttons.join(", ")}`,
    );
    if (args.screenshots) {
      mkdirSync(args.screenshots, { recursive: true });
      await slowPage.screenshot({ path: resolve(args.screenshots, "compare-record-narrow.png") });
    }
    await slowPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await slowPage.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readRecord(slowPage);
    record(
      "the light theme states the same source and the same figures",
      light.source === narrow.source
        && light.text === narrow.text
        && JSON.stringify(light.figures) === JSON.stringify(narrow.figures),
      `${light.source} || ${light.text.slice(0, 120)}`,
    );
    await shootSpan(slowPage, "compare-record-light", ["#debateProvenance", "#debateRecord"]);

    record("no page error occurred", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    // The shell polls its own telemetry on every pane; what matters here is that this story adds no
    // state-changing call and reads the record through the export that already existed.
    const posts = [...new Set(requests.filter((entry) => entry.startsWith("POST ") || entry.startsWith("PUT ") || entry.startsWith("DELETE ")))];
    record(
      "the pane reads the server's record through the export it already had, and posts no new route",
      posts.length === 1
        && posts[0] === "POST /api/debate/stream"
        && requests.includes("GET /api/history/export"),
      posts.join(", ") + " || export read: " + String(requests.includes("GET /api/history/export")),
    );
  } finally {
    await browser.close();
    for (const started of [live, pair, slow]) if (started && started.child) started.child.kill("SIGTERM");
  }

  const failed = results.filter((entry) => !entry.passed);
  if (args.json) {
    console.log(JSON.stringify({ checks: results, passed: results.length - failed.length, failed: failed.length }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  }
  return failed.length === 0 ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`compare continuity walk: ${error.message}`);
    process.exit(1);
  });
