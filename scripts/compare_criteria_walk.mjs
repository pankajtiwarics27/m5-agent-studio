#!/usr/bin/env node
/**
 * Check that the Compare pane states its criteria and each model's fate (UIUX-P4-04).
 *
 *   node scripts/compare_criteria_walk.mjs --screenshots docs/media/compare-criteria
 *
 * The pane offered checkboxes, a question and a wall of cards. Nothing said what the comparison
 * was being judged on, a panel that failed raised a toast and left its card saying "Streaming", a
 * comparison the reader stopped left the same word on screen, the panels behind a failure kept
 * saying "Waiting" as if their turn were still coming, and no row said which models answered.
 *
 * What it proves:
 *
 *   1. the criteria are stated before anything runs: what settles each one, and whether this pane
 *      can say it - cost is listed as unsettleable (no price is configured) and the reader's own
 *      criteria are listed as theirs to judge;
 *   2. a criterion the reader adds appears, survives a redraw and can be removed;
 *   3. Start is disabled with the reason it is disabled, and does nothing silently;
 *   4. before a run there is no results table and the run line says nothing has been compared;
 *   5. two panels, one answering and one failing: the run line counts both, the failed card quotes
 *      the provider instead of pretending to be slow, and the results table reads the survivor
 *      against the criteria;
 *   6. every panel failing is drawn as such, with no answered row;
 *   7. a comparison the reader stops says `stopped by you`, keeps what the panel had written, and
 *      leaves nothing saying "running";
 *   8. a panel the runtime cut says `cut at the length limit`, and does not claim it finished;
 *   9. with no runtime behind it, the local panel says `demo text` and the results table still
 *      refuses to score anything;
 *  10. the criteria and the results fit a 480 px viewport and render in the light theme;
 *  11. the pane raises no console error and posts only the debate stream.
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
    "Check that the Compare pane states its criteria and what each model's panel did.",
    "",
    "  node scripts/compare_criteria_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py four times: once plain,",
    "once with --slow-model (so a comparison can be stopped), once with --cap-model (so a panel is",
    "cut at the length limit) and once offline. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and",
    "puppeteer-core (STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-compare-criteria.md.",
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

async function startFixture(args, { offline = false, capModel = "", slowModel = "" } = {}) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [
      resolve("scripts/council_readiness_seed.py"),
      "--port", "0",
      ...(offline ? ["--offline"] : []),
      ...(capModel ? ["--cap-model", capModel] : []),
      ...(slowModel ? ["--slow-model", slowModel] : []),
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
  return { child, ready };
}

/** The criteria strip, the run line, the cards and the results table, as the page drew them. */
async function readCompare(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const criteriaHost = document.getElementById("debateCriteria");
    const criteria = criteriaHost
      ? [...criteriaHost.querySelectorAll("tbody tr")].map((row) => ({
        cells: [...row.querySelectorAll("td")].map((cell) => clean(cell.textContent)),
        answer: clean(row.querySelector("td:last-child .criterion-yes") ? "yes" : "no"),
        yes: Boolean(row.querySelector(".criterion-yes")),
        no: Boolean(row.querySelector(".criterion-no")),
      }))
      : [];
    const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => ({
      status: card.dataset.status || "",
      model: clean(card.querySelector(".stage-output-head strong") ? card.querySelector(".stage-output-head strong").textContent : ""),
      pill: clean(card.querySelector(".stage-output-head .status") ? card.querySelector(".stage-output-head .status").textContent : ""),
      tone: card.querySelector(".stage-output-head .status") ? card.querySelector(".stage-output-head .status").dataset.tone || "" : "",
      note: clean(card.querySelector(".debate-panel-note") ? card.querySelector(".debate-panel-note").textContent : ""),
      body: clean(card.querySelector(".stage-output-body") ? card.querySelector(".stage-output-body").textContent : ""),
    }));
    const resultsHost = document.getElementById("debateResults");
    const rows = resultsHost
      ? [...resultsHost.querySelectorAll("tbody tr")].map((row) => [...row.querySelectorAll("td")].map((cell) => clean(cell.textContent)))
      : [];
    const summary = document.getElementById("debateSummary");
    const hint = document.getElementById("debateRunHint");
    const button = document.getElementById("debateRunBtn");
    return {
      criteria,
      criteriaText: clean(criteriaHost ? criteriaHost.textContent : ""),
      judgedCount: criteria.filter((row) => row.cells[1] === "you").length,
      cards,
      summary: clean(summary ? summary.textContent : ""),
      summaryState: summary ? summary.dataset.state || "" : "",
      resultsHidden: !resultsHost || resultsHost.hidden,
      resultsRows: rows,
      resultsText: clean(resultsHost ? resultsHost.textContent : ""),
      hint: clean(hint ? hint.textContent : ""),
      startDisabled: Boolean(button && button.disabled),
      runningPills: cards.filter((card) => card.status === "running" || /running|Streaming|Waiting|Queued/i.test(card.pill)).length,
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

/** Tick exactly the models named, and nothing else. */
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
    const state = await readCompare(page);
    const busy = await page.evaluate(() => {
      const button = document.getElementById("debateRunBtn");
      return Boolean(button && button.disabled && document.getElementById("debateStopBtn").style.display !== "none");
    });
    if (!busy && state.cards.length && state.runningPills === 0) return state;
  }
  return readCompare(page);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const shoot = async (page, name, selector) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    await page.setViewport({ width: 1440, height: 1800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    try {
      if (selector) {
        const element = await page.$(selector);
        if (element) {
          await element.screenshot({ path });
          return path;
        }
      }
      await page.screenshot({ path, fullPage: false });
      return path;
    } finally {
      await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 2);
    }
  };

  // One picture of the run as a reader meets it: the run line, the panels and the results table.
  // A whole-pane crop at a tall viewport wastes half the frame on the pane's empty floor, and the
  // model checkboxes above it are rebuilt whenever telemetry refreshes, so the clip starts where
  // the comparison does. The clip comes from the live layout in page coordinates.
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

  const RUN_SPAN = ["#debateSummary", "#debateGrid", "#debateResults"];

  const fixtures = [];
  const start = async (options) => {
    const started = await startFixture(args, options);
    fixtures.push(started);
    return started.ready;
  };

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox").split(" "),
  });
  const pageErrors = [];
  const posts = [];
  try {
    const live = args.baseUrl ? { base_url: args.baseUrl, token: args.token } : await start({});
    args.baseUrl = live.base_url;
    args.token = args.token || live.token;
    const loaded = live.loaded_model;
    const keyless = live.keyless_model;
    const slow = await start({ slowModel: loaded });
    const capped = await start({ capModel: loaded });
    const offline = args.offlineBaseUrl
      ? { base_url: args.offlineBaseUrl, token: args.offlineToken }
      : await start({ offline: true });
    args.offlineBaseUrl = offline.base_url;
    args.offlineToken = args.offlineToken || offline.token;

    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── the criteria, stated before anything runs ──────────────────────────
    await openCompare(page, args, args.baseUrl, args.token);
    const initial = await readCompare(page);
    record(
      "the criteria are stated before the run, with what settles each one and whether this pane can say it",
      initial.criteria.length === 6
        && initial.criteria.slice(0, 3).every((row) => row.yes && /after each panel reports/.test(row.cells[2]))
        && initial.criteria.slice(0, 3).map((row) => row.cells[0]).join(" | ") === "Answered at all | Finished rather than cut | Size"
        && initial.criteria[3].cells[0] === "Cost"
        && initial.criteria[3].no
        && /no price is configured for any model/.test(initial.criteria[3].cells[2])
        && initial.judgedCount === 2
        && /yours to judge/.test(initial.criteriaText),
      initial.criteria.map((row) => `${row.cells[0]} → ${row.cells[2]}`).join(" || "),
    );
    record(
      "nothing has been compared yet: no results table, and the run line says so",
      initial.resultsHidden && initial.cards.length === 0 && /^Nothing has been compared yet/.test(initial.summary),
      `hidden=${initial.resultsHidden} summary=${initial.summary}`,
    );

    // ── the reader's own criteria ──────────────────────────────────────────
    const added = await page.evaluate(async () => {
      const input = document.getElementById("debateCriterionInput");
      input.value = "Runs on my machine";
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await new Promise((done) => setTimeout(done, 150));
      const rows = [...document.querySelectorAll("#debateCriteria tbody tr")].map((row) => row.textContent.replace(/\s+/g, " ").trim());
      // The strip is re-rendered when a criterion is added, so the focused node is the *new* input:
      // what the check is about is that focus is still in the add control, not on one node identity.
      const active = document.activeElement;
      return { rows, focused: Boolean(active && active.id === "debateCriterionInput"), value: active ? active.value : "missing" };
    });
    record(
      "a criterion the reader adds is listed as theirs to judge, and Enter adds it",
      added.rows.some((row) => /Runs on my machine/.test(row) && /yours to judge/.test(row))
        && added.focused
        && added.value === "",
      `${added.rows.length} criteria rows, input focused: ${added.focused}`,
    );
    const removed = await page.evaluate(async () => {
      const rows = [...document.querySelectorAll("#debateCriteria tbody tr")];
      const target = rows.find((row) => /Runs on my machine/.test(row.textContent));
      const button = target ? target.querySelector("button") : null;
      if (button) button.click();
      await new Promise((done) => setTimeout(done, 150));
      return [...document.querySelectorAll("#debateCriteria tbody tr")].length;
    });
    record(
      "a criterion the reader removes is gone",
      removed === 6,
      `${removed} criteria rows after removing the added one`,
    );

    // ── Start says what it will do, and cannot silently do nothing ─────────
    await selectModels(page, [loaded]);
    await writePrompt(page, "");
    const noPrompt = await readCompare(page);
    await selectModels(page, []);
    await writePrompt(page, TASK);
    const noModels = await readCompare(page);
    await selectModels(page, [loaded]);
    const ready = await readCompare(page);
    record(
      "Start is disabled with its reason, and enabled only when a question and a model are both there",
      noPrompt.startDisabled && /^Write the question to compare on\.$/.test(noPrompt.hint)
        && noModels.startDisabled && /^Select at least one model to compare\.$/.test(noModels.hint)
        && !ready.startDisabled && /^Ready: 1 model, one question\.$/.test(ready.hint),
      `no prompt: ${noPrompt.hint} / no model: ${noModels.hint} / both: ${ready.hint}`,
    );

    // ── a comparison where one panel fails ─────────────────────────────────
    await selectModels(page, [loaded, keyless]);
    await writePrompt(page, TASK);
    const partial = await runCompare(page, args);
    record(
      "two panels, one answering and one failing: the run line counts both and neither is drawn as slow",
      partial.cards.length === 2
        && partial.cards[0].status === "answered"
        && partial.cards[0].pill === "answered"
        && partial.cards[0].tone === "ok"
        && partial.cards[0].body.length > 120
        && partial.cards[1].status === "failed"
        && partial.cards[1].tone === "danger"
        && /did not answer/.test(partial.cards[1].note)
        && /provider transport failed/i.test(partial.cards[1].note)
        && /Nothing was saved for it\./.test(partial.cards[1].note)
        && partial.summary === "2 panels · 1 answered · 1 failed"
        && partial.summaryState === "mixed"
        && partial.runningPills === 0,
      `${partial.summary} || ${partial.cards.map((card) => `${card.model}: ${card.pill} — ${card.note}`).join(" || ")}`,
    );
    record(
      "the results table reads the run against the criteria, and refuses the two it cannot settle",
      !partial.resultsHidden
        && partial.resultsRows.length === 2
        && partial.resultsRows[0][1] === "answered"
        && partial.resultsRows[0][2].startsWith(String(partial.resultsRows[0][2].split(" ")[0]))
        && partial.resultsRows[0][3] === "answered, finished, size"
        && partial.resultsRows[1][1] === "failed"
        && partial.resultsRows[1][3] === "—"
        && /Not settled here: cost \(no price is configured for any model\)/.test(partial.resultsText)
        && /Correctness, Fits my constraints \(yours to judge\)/.test(partial.resultsText)
        && /not which reply is better/.test(partial.resultsText),
      partial.resultsRows.map((row) => `${row[0]}: ${row[1]} / ${row[3]}`).join(" || "),
    );
    await shootSpan(page, "compare-partial-failure", RUN_SPAN);
    await shoot(page, "compare-criteria", "#debateCriteria");

    // ── every panel failing ────────────────────────────────────────────────
    await selectModels(page, [keyless]);
    await writePrompt(page, TASK);
    const allFailed = await runCompare(page, args);
    record(
      "when every panel fails the run line says so, and no row claims an answer",
      allFailed.cards.length === 1
        && allFailed.cards.every((card) => card.status === "failed")
        && allFailed.summary === "1 panel · 1 failed"
        && allFailed.summaryState === "bad"
        && allFailed.resultsRows.length === 1
        && allFailed.resultsRows[0][3] === "—",
      `${allFailed.summary} (${allFailed.summaryState})`,
    );

    // ── a panel the runtime cut ────────────────────────────────────────────
    const cappedPage = await browser.newPage();
    cappedPage.on("pageerror", (error) => pageErrors.push(String(error)));
    cappedPage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await cappedPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(cappedPage, args, capped.base_url, capped.token);
    await selectModels(cappedPage, [loaded]);
    await writePrompt(cappedPage, TASK);
    const cut = await runCompare(cappedPage, args);
    record(
      "a panel the runtime cut says it was cut, and does not claim it finished",
      cut.cards.length === 1
        && cut.cards[0].status === "cut"
        && cut.cards[0].pill === "cut at the length limit"
        && cut.resultsRows[0][3] === "answered, size"
        && cut.summary === "1 panel · 1 cut at the length limit",
      `${cut.summary} || ${cut.resultsRows.map((row) => row[3]).join(",")}`,
    );

    // ── a comparison the reader stops ──────────────────────────────────────
    const stopPage = await browser.newPage();
    stopPage.on("pageerror", (error) => pageErrors.push(String(error)));
    stopPage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await stopPage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(stopPage, args, slow.base_url, slow.token);
    await selectModels(stopPage, [loaded]);
    await writePrompt(stopPage, TASK);
    const stopped = await runCompare(stopPage, args, { stopAfterMs: 900 });
    record(
      "a comparison the reader stops says so, keeps what the panel wrote, and leaves nothing running",
      stopped.cards.length === 1
        && stopped.cards[0].status === "stopped"
        && stopped.cards[0].pill === "stopped by you"
        && /You stopped the comparison while this panel was running/.test(stopped.cards[0].note)
        && stopped.cards[0].body.length > 60
        && stopped.summary === "1 panel · 1 stopped by you"
        && stopped.runningPills === 0
        && stopped.resultsRows[0][3] === "answered, size",
      `${stopped.summary} || ${stopped.cards[0].note.slice(0, 90)} || ${stopped.resultsRows.map((row) => row[3]).join(",")}`,
    );
    await shootSpan(stopPage, "compare-stopped", RUN_SPAN);

    // ── narrow and light ───────────────────────────────────────────────────
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readCompare(page);
    record(
      "the criteria and the results fit a 480 px viewport",
      narrow.overflowing <= 0 && narrow.criteria.length === 6 && narrow.resultsRows.length === 1,
      `scrollWidth - innerWidth = ${narrow.overflowing}`,
    );
    if (args.screenshots) {
      mkdirSync(args.screenshots, { recursive: true });
      await page.screenshot({ path: resolve(args.screenshots, "compare-criteria-narrow.png") });
    }
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await page.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readCompare(page);
    record(
      "the same statuses render in the light theme",
      light.cards.map((card) => card.status).join(",") === allFailed.cards.map((card) => card.status).join(",")
        && light.summary === allFailed.summary,
      light.cards.map((card) => card.pill).join(" "),
    );
    await shootSpan(page, "compare-light", ["#debateCriteria", "#debateSummary", "#debateGrid", "#debateResults"]);

    // ── the machine with no runtime behind it ──────────────────────────────
    const offlinePage = await browser.newPage();
    offlinePage.on("pageerror", (error) => pageErrors.push(String(error)));
    offlinePage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await offlinePage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCompare(offlinePage, args, args.offlineBaseUrl, args.offlineToken);
    // With no runtime behind it the catalogue is whatever the server still lists: select all of it
    // rather than naming models the fixture may not have.
    const offlineModels = await offlinePage.evaluate(() => [...document.querySelectorAll(".debate-m-chk")].map((box) => box.value));
    await selectModels(offlinePage, offlineModels);
    await writePrompt(offlinePage, TASK);
    const demo = await runCompare(offlinePage, args);
    record(
      "with no runtime a panel says demo text, and a demo reply is credited with a size and nothing else",
      offlineModels.length > 0
        && demo.cards.length === offlineModels.length
        && demo.cards.every((card) => card.status === "demo" || card.status === "failed")
        && demo.cards.some((card) => card.status === "demo" && card.pill === "demo text" && card.tone === "warn")
        && demo.resultsRows.filter((row) => row[1] === "demo text").every((row) => row[3] === "size")
        && /Not settled here: cost \(no price is configured for any model\)/.test(demo.resultsText)
        && /not which reply is better/.test(demo.resultsText),
      `${demo.summary} || ${demo.cards.map((card) => `${card.model}: ${card.pill}`).join(" · ")} || ${demo.resultsRows.map((row) => row[3]).join(",")}`,
    );
    await shootSpan(offlinePage, "compare-offline", RUN_SPAN);

    record("no page error occurred", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    const unexpected = posts.filter((path) => path !== "/api/debate/stream");
    record(
      "the pane posts only the comparison it already posted",
      unexpected.length === 0,
      [...new Set(posts)].join(", "),
    );
  } finally {
    await browser.close();
    for (const started of fixtures) started.child.kill("SIGTERM");
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
    console.error(`compare criteria walk: ${error.message}`);
    process.exit(1);
  });
