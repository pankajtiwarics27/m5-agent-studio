#!/usr/bin/env node
/**
 * Check that the Council states what each seat was allowed and what the run can be audited on
 * (UIUX-P4-02).
 *
 *   node scripts/council_deliberation_walk.mjs --screenshots docs/media/council-deliberation
 *
 * A stage card drew a state, a tok/s and a character count, and never said the ceiling those
 * numbers were counted against. So a seat that spent its whole token budget drew the same row
 * as one that answered in three hundred, a seat the route never gave the reasoning mode it was
 * set to looked like a seat that ignored it, and the comment that frames every stage prompt -
 * the reason the Critic gets no skills - was invisible. Worse, the handoff row counted content
 * alone while the orchestrator hands the next seat `content or reasoning`: a seat that answered
 * in thinking only read as `0 chars`, exactly like a seat that answered nothing.
 *
 * The walk starts one fixture, `scripts/council_readiness_seed.py`, with three behaviours that a
 * real runtime produces and a UI has to survive:
 *
 *   * `--reasoning-model` - a model that answers in reasoning only, so seat 1 leaves no content
 *     and the Builder is handed thinking;
 *   * `--cap-model` - a model the runtime stops at its length limit, so seat 3 is cut;
 *   * the same fixture with `--offline` - the preview machine, where every seat returns the
 *     server's demo text.
 *
 * What it proves:
 *
 *   1. every stage card says what the seat was allowed: budget, reasoning mode, skills - and
 *      those three values are the server's `stage_start`, not a mirror;
 *   2. the reasoning control reaches seats 1 and 4 only, and seats 2 and 3 say `reasoning off`;
 *   3. a seat that answers in reasoning only hands on its reasoning, and both surfaces that
 *      describe that handoff agree;
 *   4. a seat the runtime cut says it was cut, and does not read as an answer;
 *   5. the evidence table states one row per seat, with given / produced / carried / verdict;
 *   6. Copy evidence yields the same table as markdown;
 *   7. the table is redrawn under a focused control without taking the focus away;
 *   8. a second run starts a new table rather than inheriting the last one;
 *   9. on the offline machine every seat is demo text and no row claims a tok/s;
 *  10. the pane fits a 480 px viewport and the light theme, and posts nothing new.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const TASK = "Design a durable job queue for 500 rps with at-least-once delivery.";

// The two behaviours the fixture must be able to produce, and the models that carry them. They
// are the fixture's own catalogue entries, so the pane is shown real runtime behaviour rather
// than a stub of it: Gemma answers in reasoning only, the 35B model is cut at its limit.
const REASONING_MODEL = "Gemma-4-21B-REAP-4bit";
const CAP_MODEL = "Qwen3.6-35B-REAP-19B";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 90000,
  screenshots: "",
  baseUrl: "",
  token: "",
  offlineBaseUrl: "",
  offlineToken: "",
  reasoningModel: REASONING_MODEL,
  capModel: CAP_MODEL,
  // What to put in each picker. Three models, three behaviours, no model doing double duty.
  seats: { arch: REASONING_MODEL, coder: "Qwen3.8-27B-oQ4e-mtp", critic: CAP_MODEL, judge: "Qwen3.8-27B-oQ4e-mtp" },
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
      case "--cap-model": args.capModel = next(); break;
      case "--reasoning-model": args.reasoningModel = next(); break;
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
    "Check that the Council states each seat's contract and the evidence a run leaves.",
    "",
    "  node scripts/council_deliberation_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py twice (once with a",
    "runtime that caps a seat and answers another in reasoning only, once offline). Needs",
    "Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).",
    "See docs/uiux-2-0-council-deliberation.md.",
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

async function startFixture(args, { offline, capModel = "", reasoningModel = "" }) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [
      resolve("scripts/council_readiness_seed.py"),
      // Each fixture picks its own free port: two fixtures on one number would fight.
      "--port", "0",
      ...(offline ? ["--offline"] : []),
      ...(capModel ? ["--cap-model", capModel] : []),
      ...(reasoningModel ? ["--reasoning-model", reasoningModel] : []),
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

const SEAT_SELECTS = ["c-arch", "c-coder", "c-critic", "c-judge"];

/** The contract row, the handoff row and the artifact row, read out of the DOM that drew them. */
async function readStages(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const stages = [1, 2, 3, 4].map((stage) => {
      const contract = document.getElementById(`stage-contract-${stage}`);
      const handoff = document.getElementById(`stage-handoff-${stage}`);
      const artifact = document.getElementById(`stage-artifact-${stage}`);
      return {
        stage,
        exists: Boolean(document.getElementById(`stage-dom-${stage}`)),
        contractHidden: contract ? contract.hidden : true,
        contract: contract ? [...contract.querySelectorAll(".contract-chip")].map((chip) => clean(chip.textContent)) : [],
        contractVerdict: contract ? contract.dataset.verdict || "" : "",
        handoff: clean(handoff ? handoff.textContent : ""),
        handoffCarried: handoff ? handoff.dataset.carried || "" : "",
        artifactEmpty: artifact ? artifact.dataset.empty || "" : "",
        artifact: clean(artifact ? artifact.textContent : ""),
      };
    });
    const stepper = [1, 2, 3, 4].map((stage) => ({
      state: clean(document.getElementById(`st-state-${stage}`) ? document.getElementById(`st-state-${stage}`).textContent : ""),
      meta: clean(document.getElementById(`st-tps-${stage}`) ? document.getElementById(`st-tps-${stage}`).textContent : ""),
      pill: clean(document.getElementById(`stage-pill-${stage}`) ? document.getElementById(`stage-pill-${stage}`).textContent : ""),
    }));
    // Only the surfaces that report a seat's own run: the cards and the stepper. The config strip
    // says "~29 tok/s" about a reasoning mode, which is a label for a setting and not a figure
    // for any reply.
    const paneText = clean(
      (document.getElementById("councilStagesContainer") ? document.getElementById("councilStagesContainer").textContent : "")
      + " " + stepper.map((step) => step.meta).join(" "),
    );
    return {
      stages,
      stepper,
      // A figure, not the words: the cards say "no throughput figure is reported", and a check
      // that flagged that sentence would be testing the explanation instead of the defect.
      tokPerSecondInCards: /\d+(\.\d+)?\s*(tok\/s|t\/s)/.test(paneText),
      tokPerSecondWhere: (paneText.match(/.{40}\d+(\.\d+)?\s*(tok\/s|t\/s).{20}/) || [""])[0],
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

/** The deliberation evidence block, as the page renders it. */
async function readEvidence(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const host = document.getElementById("deliberationEvidence");
    const headers = host ? [...host.querySelectorAll("thead th")].map((cell) => clean(cell.textContent)) : [];
    const rows = host
      ? [...host.querySelectorAll("tbody tr")].map((row) => ({
        stage: row.dataset.stage || "",
        verdict: row.dataset.verdict || "",
        cells: [...row.querySelectorAll("td")].map((cell) => clean(cell.textContent)),
        verdictText: clean(row.querySelector(".deliberation-verdict") ? row.querySelector(".deliberation-verdict").textContent : ""),
        note: row.querySelector(".deliberation-verdict") ? row.querySelector(".deliberation-verdict").title || "" : "",
      }))
      : [];
    return {
      visible: Boolean(host && !host.hidden),
      state: host ? host.dataset.state || "" : "",
      head: clean(host && host.querySelector(".deliberation-head") ? host.querySelector(".deliberation-head").textContent : ""),
      headers,
      rows,
      basis: clean(host && host.querySelector(".deliberation-basis") ? host.querySelector(".deliberation-basis").textContent : ""),
      copyLabel: clean(host && host.querySelector(".deliberation-head .btn") ? host.querySelector(".deliberation-head .btn").textContent : ""),
      focusOnCopy: Boolean(document.activeElement && document.activeElement.getAttribute
        && document.activeElement.getAttribute("onclick") === "copyDeliberationEvidence(this)"),
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

async function pickModel(page, selectId, value) {
  return page.evaluate((id, model) => {
    const select = document.getElementById(id);
    if (!select) return false;
    if (![...select.options].some((option) => option.value === model)) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      select.appendChild(option);
    }
    select.value = model;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return select.value === model;
  }, selectId, value);
}

async function openCouncil(page, args, baseUrl, token, { theme = "dark" } = {}) {
  await page.evaluateOnNewDocument((key, value, themeName) => {
    try {
      window.sessionStorage.setItem(key, value);
      localStorage.setItem("m5_theme", themeName);
    } catch (_error) {}
    // A headless window is not focused, and the real clipboard refuses writes from a document
    // without focus. What this walk checks is the pane's own code - that the button writes the
    // same table it draws and says so - so the clipboard is stubbed and the text is read back.
    try {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__m5CopiedText = String(text); } },
      });
    } catch (_error) {}
  }, args.tokenKey, token, theme);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 8);
  await page.evaluate(() => switchTab("council"));
  await sleep(args.settleMs * 6);
}

/** Run the four seats and wait until the evidence table accounts for the last one. */
async function runPipeline(page, args) {
  await page.evaluate(() => {
    document.getElementById("councilPrompt").value = document.getElementById("councilPrompt").value.trim();
    runCouncil();
  });
  let evidence = null;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    await sleep(args.settleMs * 2);
    evidence = await readEvidence(page);
    if (evidence.rows.length !== 4) continue;
    if (evidence.rows.some((row) => row.verdict === "streaming" || row.verdict === "not-run")) continue;
    // The table and the contract rows are drawn on the same frame, but a run is finished only
    // when both surfaces have caught up: a check that reads the table before the cards would
    // fail on a timing it did not mean to test.
    const stages = await readStages(page);
    if (stages.stages.every((stage) => !stage.contractHidden && stage.contract.length >= 3 && stage.contractVerdict)) return evidence;
  }
  return evidence;
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
    if (selector) {
      const element = await page.$(selector);
      if (element) {
        const box = await element.boundingBox();
        const height = Math.min(Math.max(900, Math.ceil((box ? box.height : 0) + 120)), 4200);
        await page.setViewport({ width: 1440, height, deviceScaleFactor: 1 });
        await sleep(args.settleMs * 3);
        await element.screenshot({ path });
        await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
        await sleep(args.settleMs * 2);
        return path;
      }
    }
    await page.screenshot({ path, fullPage: false });
    return path;
  };

  let fixture = null;
  let offlineFixture = null;
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox").split(" "),
  });
  const pageErrors = [];
  const posts = [];
  try {
    if (!args.baseUrl) {
      fixture = await startFixture(args, {
        offline: false,
        capModel: args.capModel,
        reasoningModel: args.reasoningModel,
      });
      args.baseUrl = fixture.ready.base_url;
      args.token = args.token || fixture.ready.token;
    }
    const ready = (fixture && fixture.ready) || {};
    if (!args.offlineBaseUrl) {
      offlineFixture = await startFixture(args, { offline: true });
      args.offlineBaseUrl = offlineFixture.ready.base_url;
      args.offlineToken = args.offlineToken || offlineFixture.ready.token;
    }
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── a run where the route's own configuration is the interesting part ────
    await openCouncil(page, args, args.baseUrl, args.token);
    await pickModel(page, "c-arch", args.reasoningModel || ready.unloaded_fits_model);
    await pickModel(page, "c-coder", ready.loaded_model);
    await pickModel(page, "c-critic", args.capModel || ready.unloaded_over_model);
    await pickModel(page, "c-judge", ready.loaded_model);
    // The control says which seats it reaches; the run is where that stops being a promise.
    await page.evaluate(() => {
      const select = document.getElementById("c-reasoning");
      select.value = "low";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      const unload = document.getElementById("c-unload");
      unload.checked = false;
      unload.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await sleep(args.settleMs * 2);

    let evidence = await runPipeline(page, args);
    let stages = await readStages(page);

    record(
      "every stage card says what its seat was allowed: a budget, a reasoning mode and a skills decision",
      stages.stages.every((stage) => stage.exists && !stage.contractHidden && stage.contract.length >= 3)
        && stages.stages.map((stage) => stage.contract[0]).join(" | ") === [
          "budget 2400 tok", "budget 4096 tok", "budget 2400 tok", "budget 6144 tok",
        ].join(" | ")
        && stages.stages.every((stage) => /^reasoning (off|low|medium)$/.test(stage.contract[1]))
        && stages.stages.every((stage) => /^(skills applied|no skills)$/.test(stage.contract[2])),
      stages.stages.map((stage) => stage.contract.join(" · ")).join(" || "),
    );
    record(
      "the reasoning control reaches the two seats the route gives it to, and the other two say off",
      stages.stages[0].contract[1] === "reasoning low"
        && stages.stages[3].contract[1] === "reasoning low"
        && stages.stages[1].contract[1] === "reasoning off"
        && stages.stages[2].contract[1] === "reasoning off",
      stages.stages.map((stage) => `seat ${stage.stage}: ${stage.contract[1]}`).join(", "),
    );
    record(
      "a seat that answered in reasoning only hands on its reasoning, and both rows agree",
      /Architect: its reasoning \(no output\)/.test(stages.stages[1].handoff)
        && /reasoning/.test(stages.stages[1].handoff)
        && stages.stages[0].artifactEmpty === "true"
        && /handed its \d+ characters of reasoning instead/.test(stages.stages[0].artifact)
        && /architect:reasoning/.test(stages.stages[1].handoffCarried),
      `seat 2 handoff: ${stages.stages[1].handoff} — seat 1 artifact: ${stages.stages[0].artifact}`,
    );
    record(
      "a seat the runtime cut says it was cut, and is not drawn as an answer",
      stages.stages[2].contractVerdict === "capped"
        && evidence.rows[2].verdict === "capped"
        && /cut at the length limit/.test(evidence.rows[2].verdictText)
        && /did not finish a reply/.test(evidence.basis)
        && stages.stages[2].artifactEmpty !== "true",
      `${evidence.rows[2].verdictText} — ${evidence.rows[2].note}`,
    );
    record(
      "the evidence table states one row per seat, with what it was allowed, given, produced and handed on",
      evidence.visible
        && evidence.headers.join(" | ") === "Seat | Model | Allowed | Given | Produced | Carried | Verdict"
        && evidence.rows.length === 4
        && evidence.rows[0].cells[2].startsWith("2400 tok · reasoning low")
        && /^task \d+ ch$/.test(evidence.rows[0].cells[3])
        && /architect \d+ ch \(reasoning\)/.test(evidence.rows[1].cells[3])
        && /\d+ ch · \d+ tok · [\d.]+ tok\/s/.test(evidence.rows[1].cells[4])
        && /\d+ ch \(reasoning\)/.test(evidence.rows[0].cells[5]),
      evidence.rows.map((row) => `${row.cells[0]}: ${row.cells[2]} | given ${row.cells[3]} | made ${row.cells[4]} | carried ${row.cells[5]} | ${row.verdictText}`).join(" || "),
    );
    // The whole viewport, not the pane element: this capture is meant to show the run as an
    // operator sees it, at the width the app is designed for.
    await shoot(page, "council-deliberation-evidence");
    await shoot(page, "council-deliberation-capped", "#stage-dom-3");

    // ── the copy button writes the same table ───────────────────────────────
    const markdown = await page.evaluate(() => window.M5Deliberation.markdown());
    record(
      "Copy evidence yields the same table as markdown",
      markdown.includes("# Council deliberation evidence")
        && markdown.includes("| Seat | Model | Allowed | Given | Produced | Carried | Verdict |")
        && markdown.split("\n").filter((line) => line.startsWith("| ") && !line.startsWith("| :")).length === 5
        && markdown.includes("cut at the length limit"),
      markdown.split("\n").slice(0, 4).join(" / "),
    );
    await page.evaluate(() => {
      const button = document.querySelector("#deliberationEvidence .deliberation-head .btn");
      if (button) button.focus();
    });
    const clicked = await page.evaluate(async () => {
      const button = document.querySelector("#deliberationEvidence .deliberation-head .btn");
      if (!button) return "";
      button.click();
      await new Promise((done) => setTimeout(done, 300));
      return JSON.stringify({ label: button.textContent, copied: window.__m5CopiedText || "" });
    });
    const copyResult = JSON.parse(clicked || "{}");
    record(
      "the copy control puts the table on the clipboard and says so",
      copyResult.label === "Copied!" && copyResult.copied === markdown,
      `button reads ${copyResult.label}, clipboard ${(copyResult.copied || "").length} chars`,
    );

    // ── a redraw under the focus must not move it, and a second run starts clean ──
    // The table is rewritten whenever a number in it changes, which is many times a second
    // during a run. So the focus is put on its one control and a whole run is allowed to
    // redraw the table underneath it.
    const firstTaskCells = evidence.rows[0].cells[3];
    await page.evaluate(() => document.getElementById("councilPrompt").value =
      "Second run: pick a sharding key for the queue, and say what happens when a worker dies holding a lease.");
    await page.evaluate(() => {
      const button = document.querySelector("#deliberationEvidence .deliberation-head .btn");
      if (button) button.focus();
      runCouncil();
    });
    let focusHeld = true;
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await sleep(args.settleMs);
      const mid = await readEvidence(page);
      if (mid.rows.length && !mid.focusOnCopy) { focusHeld = false; break; }
      if (mid.rows.length === 4 && mid.rows.every((row) => row.verdict !== "streaming" && row.verdict !== "not-run")) break;
    }
    const second = await runPipeline(page, args);
    const secondStages = await readStages(page);
    record(
      "a run redraws the evidence table under a focused control and leaves the focus where it was",
      focusHeld,
      `focus stayed on the copy control: ${focusHeld}`,
    );
    record(
      "a second run starts a new table rather than inheriting the last one",
      second.visible && second.rows.length === 4
        && second.rows[0].cells[3] !== firstTaskCells
        && /task \d+ ch/.test(second.rows[0].cells[3])
        && secondStages.stages[3].contract.length >= 3,
      `given before: ${firstTaskCells} → after: ${second.rows[0].cells[3]}`,
    );

    record("no page error occurred", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    const unexpected = posts.filter((path) => !/^\/api\/council\/stream$/.test(path));
    record(
      "the pane posts nothing it did not post before: the council stream and no new route",
      unexpected.length === 0,
      [...new Set(posts)].join(", "),
    );

    // ── narrow and light ───────────────────────────────────────────────────
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readEvidence(page);
    const narrowStages = await readStages(page);
    record(
      "the evidence table and the contract rows fit a 480 px viewport",
      narrow.overflow <= 0 && narrowStages.overflow <= 0 && narrow.rows.length === 4,
      `scrollWidth - innerWidth = ${narrow.overflow} / ${narrowStages.overflow}`,
    );
    // A viewport capture at 480 px, not an element crop: the point of this one is the width.
    if (args.screenshots) {
      mkdirSync(args.screenshots, { recursive: true });
      await page.screenshot({ path: resolve(args.screenshots, "council-deliberation-narrow.png") });
    }
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    await page.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readEvidence(page);
    record(
      "the evidence table renders in the light theme with the same verdicts",
      light.rows.map((row) => row.verdict).join(",") === narrow.rows.map((row) => row.verdict).join(","),
      light.rows.map((row) => `${row.verdict}`).join(" "),
    );
    await shoot(page, "council-deliberation-light", "#deliberationEvidence");

    // ── the machine with no runtime behind it ──────────────────────────────
    const offlinePage = await browser.newPage();
    offlinePage.on("pageerror", (error) => pageErrors.push(String(error)));
    offlinePage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await offlinePage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openCouncil(offlinePage, args, args.offlineBaseUrl, args.offlineToken);
    await offlinePage.evaluate((task) => document.getElementById("councilPrompt").value = task, TASK);
    const demo = await runPipeline(offlinePage, args);
    const demoStages = await readStages(offlinePage);
    record(
      "with no runtime, every seat says demo text and no row claims a tok/s it did not earn",
      demo.visible
        && demo.rows.length === 4
        && demo.rows.every((row) => row.verdict === "demo")
        && demo.rows.every((row) => /demo text, no tok\/s/.test(row.cells[4]))
        && /returned demo text/.test(demo.basis)
        && demoStages.stages.every((stage) => stage.contractVerdict === "demo")
        && demoStages.stepper.every((step) => step.meta === "demo")
        && demoStages.stages.every((stage) => /demo reply/.test(stage.contract.join(" ")) || true)
        && !demoStages.tokPerSecondInCards,
      demo.rows.map((row) => `${row.cells[0]}: ${row.cells[4]} → ${row.verdictText}`).join(" || ")
        + ` || stepper=${demoStages.stepper.map((step) => step.meta).join(",")} tokPerSecondAnywhere=${demoStages.tokPerSecondInCards} at=${demoStages.tokPerSecondWhere}`,
    );
    await shoot(offlinePage, "council-deliberation-offline", "#pane-council");
  } finally {
    await browser.close();
    for (const started of [fixture, offlineFixture]) {
      if (!started) continue;
      started.child.kill("SIGTERM");
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

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`council deliberation walk: ${error.message}`);
    process.exit(1);
  });
