#!/usr/bin/env node
/**
 * Check that every figure the Compare pane prints is a figure the response carried (UIUX-P4-05).
 *
 *   node scripts/compare_provenance_walk.mjs --screenshots docs/media/compare-provenance
 *
 * The pane printed exactly one number and it was the arbiter's throughput, in a green badge, with
 * nothing saying where it came from — and a demo verdict carried 29.5 or 42.1 tok/s that no model
 * ever ran for. Latency, tokens and cost per model were absent, so a side-by-side comparison was a
 * side-by-side of prose.
 *
 * What it proves:
 *
 *   1. before a run the provenance block is not drawn, because nothing has been measured;
 *   2. every figure on screen equals the figure the server sent with that reply — the walk tees the
 *      `/api/debate/stream` response, parses the `panel_done` / `synthesis_done` / `debate_done`
 *      events, and compares them cell by cell with the table, so a figure this page computed
 *      (a rate from its own clock, a latency from the renderer) would not match;
 *   3. a panel that failed shows no figure and says why, rather than a zero;
 *   4. a comparison the reader stopped shows no figure for the panel that never reported;
 *   5. a reply the runtime cut still carries its measured figures;
 *   6. the arbiter's card states the same figures as the arbiter's row, and neither invents one;
 *   7. with no runtime behind the page, a demo reply is given no figure at all — not the demo
 *      path's 29.5 / 42.1 tok/s — and says why;
 *   8. cost is stated as not known, once, with the reason;
 *   9. the block fits a 480 px viewport and carries the same figures in the light theme;
 *  10. no console error, and the pane still posts only the comparison it already posted.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const TASK =
  "Compare SQLite WAL vs embedded Postgres for a local telemetry store: which would you pick and why?";

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
    "Check that the Compare pane's figures are the response's figures, not the page's.",
    "",
    "  node scripts/compare_provenance_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py three times: once plain,",
    "once with --slow-model (so a comparison can be stopped mid-panel) and once offline (so a demo",
    "reply is available). Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-compare-provenance.md.",
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

async function startFixture(args, { offline = false, slowModel = "", residentModel = "" } = {}) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [
      resolve("scripts/council_readiness_seed.py"),
      "--port", "0",
      ...(offline ? ["--offline"] : []),
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
  return ready;
}

/** The page mirrors `debateFigure` exactly; this is the same arithmetic, in the walk. */
function figure(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "—";
  const text = Number.isInteger(number) ? String(number) : String(Math.round(number * 100) / 100);
  return text + suffix;
}

function tokensCell(usage) {
  const tokens = figure(usage.completion_tokens);
  if (tokens === "—") return "—";
  const reasoning = figure(usage.reasoning_tokens);
  return tokens + " tokens" + (reasoning === "—" ? "" : ` (${reasoning} reasoning)`);
}

/** The provenance block, as the page drew it. */
async function readProvenance(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const host = document.getElementById("debateProvenance");
    const rows = host
      ? [...host.querySelectorAll("tbody tr")].map((row) => ({
        cells: [...row.querySelectorAll("td")].map((cell) => clean(cell.textContent)),
        note: clean(row.querySelector(".debate-provenance-note") ? row.querySelector(".debate-provenance-note").textContent : ""),
        spans: row.querySelectorAll("td").length,
      }))
      : [];
    const synthesis = document.getElementById("debateSynthesisArea");
    return {
      hidden: !host || host.hidden,
      text: clean(host ? host.textContent : ""),
      rows,
      basis: clean(host && host.querySelector(".debate-criteria-basis") ? host.querySelector(".debate-criteria-basis").textContent : ""),
      footer: clean(host && host.querySelector(".debate-results-basis") ? host.querySelector(".debate-results-basis").textContent : ""),
      headers: host ? [...host.querySelectorAll("thead th")].map((cell) => clean(cell.textContent)) : [],
      arbiterBadge: clean(synthesis && synthesis.querySelector(".badge") ? synthesis.querySelector(".badge").textContent : ""),
      arbiterTitle: clean(synthesis && synthesis.querySelector(".badge") ? synthesis.querySelector(".badge").getAttribute("title") : ""),
      cards: [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => ({
        status: card.dataset.status || "",
        model: clean(card.querySelector(".stage-output-head strong") ? card.querySelector(".stage-output-head strong").textContent : ""),
      })),
      runButtonDisabled: Boolean(document.getElementById("debateRunBtn") && document.getElementById("debateRunBtn").disabled),
      overflowing: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

/**
 * Install the tee before the page runs: `consumeSSE` reads a `fetch` response's body, so the walk
 * clones it and parses the frames the app also parses. What the page prints can then be compared
 * with what the server sent, which is the whole claim of this story.
 */
async function openCompare(page, args, baseUrl, token, { theme = "dark" } = {}) {
  await page.evaluateOnNewDocument((key, value, themeName) => {
    try {
      window.sessionStorage.setItem(key, value);
      localStorage.setItem("m5_theme", themeName);
    } catch (_error) {}
    window.__debateFrames = [];
    const original = window.fetch;
    window.fetch = async function (input, init) {
      const response = await original.apply(this, arguments);
      try {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        if (url.indexOf("/api/debate/stream") !== -1 && response.body) {
          const clone = response.clone();
          (async () => {
            try {
              const reader = clone.body.getReader();
              const decoder = new TextDecoder();
              let buffer = "";
              for (;;) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let index;
                while ((index = buffer.indexOf("\n\n")) !== -1) {
                  const frame = buffer.slice(0, index);
                  buffer = buffer.slice(index + 2);
                  let name = "message";
                  let data = "";
                  for (const line of frame.split("\n")) {
                    if (line.startsWith("event:")) name = line.slice(6).trim();
                    else if (line.startsWith("data:")) data += line.slice(5).trim();
                  }
                  if (!data) continue;
                  try {
                    window.__debateFrames.push({ event: name, data: JSON.parse(data) });
                  } catch (_error) {}
                }
              }
            } catch (_error) {
              // The page cancelled its own reader when the stream ended; the frames it did read stay.
            }
          })();
        }
      } catch (_error) {}
      return response;
    };
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

async function frames(page) {
  return page.evaluate(() => window.__debateFrames || []);
}

const firstFrame = (all, event) => all.find((entry) => entry.event === event) || null;
const framesOf = (all, event) => all.filter((entry) => entry.event === event);

/** Start the comparison and wait until nothing on the pane still claims to be running. */
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
    const state = await readProvenance(page);
    const busy = await page.evaluate(() => {
      const button = document.getElementById("debateRunBtn");
      return Boolean(button && button.disabled && document.getElementById("debateStopBtn").style.display !== "none");
    });
    if (!busy && state.cards.length && state.cards.every((card) => card.status !== "running")) return state;
  }
  return readProvenance(page);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };

  // The blocks as one picture: the results table and the provenance under it.
  const shootSpan = async (page, name, selectors) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.setViewport({ width: 1440, height: 1800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    try {
      const rect = await page.evaluate((list) => {
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
      if (!rect) return;
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

  const live = args.baseUrl ? { base_url: args.baseUrl, token: args.token } : await startFixture(args, {});
  args.baseUrl = live.base_url;
  args.token = args.token || live.token;
  const loaded = live.loaded_model;
  const keyless = live.keyless_model;
  // Two resident models, so two panels can answer and an arbiter verdict exists to measure.
  // The keyless alias fails by design, and a run that fails never reaches the arbiter.
  const pair = await startFixture(args, { residentModel: live.unloaded_fits_model });
  const slow = await startFixture(args, { slowModel: loaded });
  const offline = args.offlineBaseUrl
    ? { base_url: args.offlineBaseUrl, token: args.offlineToken }
    : await startFixture(args, { offline: true });
  args.offlineBaseUrl = offline.base_url;
  args.offlineToken = args.offlineToken || offline.token;

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox").split(" "),
  });
  const pageErrors = [];
  const posts = [];
  const watch = (page) => {
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
  };
  try {
    const page = await browser.newPage();
    watch(page);
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── nothing measured yet ───────────────────────────────────────────────
    await openCompare(page, args, args.baseUrl, args.token);
    const initial = await readProvenance(page);
    record(
      "before a run there is no performance block, because nothing has been measured",
      initial.hidden && initial.rows.length === 0,
      `hidden=${initial.hidden} rows=${initial.rows.length}`,
    );

    // ── a comparison where one reply reports and one fails ─────────────────
    await selectModels(page, [loaded, keyless]);
    await writePrompt(page, TASK);
    const mixed = await runCompare(page, args);
    const mixedFrames = await frames(page);
    const reported = firstFrame(mixedFrames, "panel_done");
    const done = firstFrame(mixedFrames, "debate_done");
    const answeredRow = mixed.rows.find((row) => row.spans === 6 && row.cells[0] === (reported ? reported.data.model : ""));
    const failedRow = mixed.rows.find((row) => row.spans === 6 && row.cells[1] === "—" && /groq|llama/.test(row.cells[0]));
    record(
      "every figure on screen is the figure the response carried for that reply",
      Boolean(reported)
        && Boolean(answeredRow)
        && answeredRow.cells[1] === figure(reported.data.usage.elapsed_s, " s")
        && answeredRow.cells[2] === figure(reported.data.usage.ttft, " s")
        && answeredRow.cells[3] === tokensCell(reported.data.usage)
        && answeredRow.cells[4] === figure(reported.data.usage.decode_tps, " tok/s")
        && answeredRow.cells[5] === "—"
        && unlikely(mixed.text, ["NaN", "undefined", "null"]),
      answeredRow ? `${answeredRow.cells.slice(1).join(" | ")} vs wire ${JSON.stringify(reported.data.usage)}` : "no answered row",
    );
    record(
      "a panel that failed is given no figure, and says why instead of showing a zero",
      Boolean(failedRow)
        && failedRow.cells.slice(1, 5).every((cell) => cell === "—")
        && mixed.rows.some((row) => row.note === "the provider failed before it reported any figure"),
      failedRow ? failedRow.cells.join(" | ") : "no failed row",
    );
    record(
      "the block names the fields it reads, and a run that failed claims no total of its own",
      /elapsed_s/.test(mixed.basis)
        && /ttft/.test(mixed.basis)
        && /completion_tokens/.test(mixed.basis)
        && /decode_tps/.test(mixed.basis)
        && /this page times nothing/.test(mixed.basis)
        && done === null
        && /Cost: not known\./.test(mixed.footer)
        && /No price is configured for any model/.test(mixed.footer)
        && /is not reported until the run ends/.test(mixed.footer),
      `${mixed.headers.join(" | ")} || ${mixed.footer}`,
    );
    await shootSpan(page, "compare-provenance", ["#debateResults", "#debateProvenance"]);

    // ── two panels that answered, and the arbiter's own figures ────────────
    const pairPage = await browser.newPage();
    watch(pairPage);
    await pairPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(pairPage, args, pair.base_url, pair.token);
    const pairModels = [pair.loaded_model, pair.resident_model];
    await selectModels(pairPage, pairModels);
    await writePrompt(pairPage, TASK);
    const both = await runCompare(pairPage, args);
    const pairFrames = await frames(pairPage);
    const synthesis = firstFrame(pairFrames, "synthesis_done");
    const pairDone = firstFrame(pairFrames, "debate_done");
    const arbiterRow = both.rows.find((row) => /· arbiter$/.test(row.cells[0]));
    const arbiterFigures = synthesis
      ? [figure(synthesis.data.usage.decode_tps, " tok/s"), figure(synthesis.data.usage.elapsed_s, " s"), figure(synthesis.data.usage.completion_tokens, " tokens")]
        .filter((part) => part !== "—")
        .join(" · ")
      : "";
    record(
      "the arbiter's card and the arbiter's row state the same figures, and neither invents one",
      pairModels.length === 2
        && both.cards.length === 2
        && both.cards.every((card) => card.status === "answered")
        && Boolean(synthesis)
        && Boolean(arbiterRow)
        && arbiterRow.cells[4] === figure(synthesis.data.usage.decode_tps, " tok/s")
        && arbiterRow.cells[3] === tokensCell(synthesis.data.usage)
        && both.arbiterBadge === arbiterFigures
        && /runtime reported it/.test(both.arbiterTitle)
        && !/Synthesizing\.\.\./.test(both.arbiterBadge),
      `badge: ${both.arbiterBadge} || row: ${arbiterRow ? arbiterRow.cells.slice(1).join(" | ") : "missing"}`,
    );
    record(
      "a run that finished reports its own elapsed time, and the figure is the server's",
      Boolean(pairDone)
        && new RegExp(`The run took ${figure(pairDone.data.total_elapsed_s, " s").replace(".", "\\.")}, as the server measured it`).test(both.footer)
        && /Cost: not known\./.test(both.footer),
      `${both.footer} || wire total_elapsed_s = ${pairDone ? pairDone.data.total_elapsed_s : "missing"}`,
    );
    await shootSpan(pairPage, "compare-provenance-arbiter", ["#debateSynthesisArea", "#debateProvenance"]);

    // ── a comparison the reader stopped ────────────────────────────────────
    const stopPage = await browser.newPage();
    watch(stopPage);
    await stopPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(stopPage, args, slow.base_url, slow.token);
    await selectModels(stopPage, [loaded]);
    await writePrompt(stopPage, TASK);
    const stopped = await runCompare(stopPage, args, { stopAfterMs: 900 });
    const stoppedFrames = await frames(stopPage);
    const stoppedRow = stopped.rows.find((row) => row.spans === 6);
    record(
      "a comparison the reader stopped reports no figure for the panel that never reported",
      stopped.cards.length === 1
        && stopped.cards[0].status === "stopped"
        && framesOf(stoppedFrames, "panel_done").length === 0
        && Boolean(stoppedRow)
        && stoppedRow.cells.slice(1, 5).every((cell) => cell === "—")
        && stopped.rows.some((row) => row.note === "you stopped the run before the runtime reported"),
      `${stopped.cards.map((card) => card.status).join(",")} || ${stoppedRow ? stoppedRow.cells.join(" | ") : "no row"}`,
    );

    // ── narrow and light ───────────────────────────────────────────────────
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readProvenance(page);
    record(
      "the table fits a 480 px viewport with all six columns still stated",
      narrow.overflowing <= 0 && narrow.headers.length === 6 && !narrow.hidden,
      `scrollWidth - innerWidth = ${narrow.overflowing}, headers = ${narrow.headers.join(",")}`,
    );
    if (args.screenshots) {
      mkdirSync(args.screenshots, { recursive: true });
      await page.screenshot({ path: resolve(args.screenshots, "compare-provenance-narrow.png") });
    }
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await page.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readProvenance(page);
    record(
      "the light theme carries the same figures, not a rounded or reformatted copy",
      light.rows.length === mixed.rows.length
        && JSON.stringify(light.rows) === JSON.stringify(mixed.rows)
        && light.footer === mixed.footer,
      light.rows.map((row) => row.cells.join(" ")).join(" || "),
    );
    await shootSpan(page, "compare-provenance-light", ["#debateProvenance"]);

    // ── the machine with no runtime behind it ──────────────────────────────
    const offlinePage = await browser.newPage();
    watch(offlinePage);
    await offlinePage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(offlinePage, args, args.offlineBaseUrl, args.offlineToken);
    const offlineModels = await offlinePage.evaluate(() => [...document.querySelectorAll(".debate-m-chk")].map((box) => box.value));
    await selectModels(offlinePage, offlineModels);
    await writePrompt(offlinePage, TASK);
    const demo = await runCompare(offlinePage, args);
    const demoFrames = await frames(offlinePage);
    const demoRows = demo.rows.filter((row) => row.spans === 6);
    record(
      "a demo reply is given no figure at all, and never the demo path's 29.5 or 42.1 tok/s",
      offlineModels.length > 0
        && demo.cards.some((card) => card.status === "demo")
        && demoRows.length === offlineModels.length
        && demoRows.every((row) => row.cells.slice(1, 6).every((cell) => cell === "—"))
        && demo.rows.some((row) => /demo text: the reply is the server's, so it carries no measured figure/.test(row.note))
        && !/tok\/s/.test(demo.text)
        && !/29\.5|42\.1/.test(demo.text)
        && framesOf(demoFrames, "panel_done").every((frame) => frame.data.usage && frame.data.usage.demo === true),
      `${demoRows.map((row) => row.cells.join(" ")).join(" || ")} || ${demo.rows.map((row) => row.note).filter(Boolean).join(" / ")}`,
    );
    await shootSpan(offlinePage, "compare-provenance-demo", ["#debateGrid", "#debateProvenance"]);

    record("no page error occurred", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    const unexpected = posts.filter((path) => path !== "/api/debate/stream");
    record(
      "the pane still posts only the comparison it already posted, and reads no price",
      unexpected.length === 0 && !posts.some((path) => /price|cost|billing/.test(path)),
      [...new Set(posts)].join(", "),
    );
  } finally {
    await browser.close();
    for (const started of [live, pair, slow, offline]) {
      if (started && started.child) started.child.kill("SIGTERM");
    }
  }

  const failed = results.filter((entry) => !entry.passed);
  if (args.json) {
    console.log(JSON.stringify({ checks: results, passed: results.length - failed.length, failed: failed.length }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  }
  return failed.length === 0 ? 0 : 1;
}

/** True when none of the given strings appears in `text`. */
function unlikely(text, needles) {
  return needles.every((needle) => !text.includes(needle));
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`compare provenance walk: ${error.message}`);
    process.exit(1);
  });
