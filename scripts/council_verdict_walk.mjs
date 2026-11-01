#!/usr/bin/env node
/**
 * Check that the Council states consensus, disagreement and the decision (UIUX-P4-03).
 *
 *   node scripts/council_verdict_walk.mjs --screenshots docs/media/council-verdict
 *
 * The Hall could show a transcript and, if a seat had been asked for its verdict, a banner whose
 * only claim was "Pinned Council Conclusion (author)". Three things were unsaid:
 *
 *   * what the decision has to do with the four replies above it - it is one seat's reply, and
 *     nothing said whether the other seats' words reached it or were left behind;
 *   * whether four seats produced four readings, or one reading repeated. Two seats pointed at the
 *     same model say the same thing, and the offline demo path makes every seat say the same
 *     thing; a results pane that counted replies would call that consensus;
 *   * that a session ending with nothing pinned has settled nothing.
 *
 * The walk starts one fixture, `scripts/council_readiness_seed.py`. Its stand-in runtime now
 * answers each catalogue model with a different paragraph, so the seats can be told apart - and
 * two seats on one model still produce identical words, which is the failure the pane must name.
 *
 * What it proves:
 *
 *   1. a session with four replies and nothing pinned draws the verdict area and says so;
 *   2. the area counts voices, not replies: two seats on one model are drawn as one voice, and the
 *      row says which seat it repeats;
 *   3. the shared-wording measure is the fraction of a reply's 4-word sequences found in the text
 *      it is compared with - self 100 %, a quoted fragment 100 %, an unrelated sentence 0 %;
 *   4. pinning the Lead Arbiter's verdict states where the decision came from, word for word;
 *   5. the pinned seat's row is the decision, and every other reply is measured against it;
 *   6. replies the decision does not carry are named as dissent, and stay in the transcript;
 *   7. the conclusion banner still draws the pinned text (the surface that existed keeps working);
 *   8. the session's status reads CLOSED once a decision is pinned;
 *   9. Copy verdict writes the same table as markdown, and says it copied;
 *  10. a redundant redraw leaves the focus on that copy control;
 *  11. the block fits a 480 px viewport and renders in the light theme;
 *  12. an empty session draws no verdict at all;
 *  13. with no runtime every seat is the same demo text: one voice, four replies, and the pane
 *      says that instead of presenting four identical replies as a council that agrees;
 *  14. the pane raises no console error and posts no route it did not post before.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const TASK = "Design a durable job queue for 500 rps with at-least-once delivery.";

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
    "Check that the Council states agreement, dissent and the decision it reached.",
    "",
    "  node scripts/council_verdict_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py twice (once answering,",
    "once offline). Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-council-verdict.md.",
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

async function startFixture(args, { offline }) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--port", "0", ...(offline ? ["--offline"] : [])],
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

/** The verdict area, as the page drew it. */
async function readVerdict(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const box = document.getElementById("hallVerdict");
    const banner = document.getElementById("hallConclusionBanner");
    const rows = box
      ? [...box.querySelectorAll("tbody tr")].map((row) => {
        const reading = row.querySelector(".verdict-reading");
        return {
          cells: [...row.querySelectorAll("td")].map((cell) => clean(cell.textContent)),
          reading: reading ? reading.dataset.reading || "" : "",
          readingText: reading ? clean(reading.textContent) : "",
        };
      })
      : [];
    const summary = window.M5CouncilVerdict ? window.M5CouncilVerdict.summary() : null;
    return {
      hidden: !box || box.hidden,
      state: box ? box.dataset.state || "" : "",
      head: clean(box && box.querySelector(".verdict-head strong") ? box.querySelector(".verdict-head strong").textContent : ""),
      summaryLine: clean(box && box.querySelector(".verdict-head span") ? box.querySelector(".verdict-head span").textContent : ""),
      headers: box ? [...box.querySelectorAll("thead th")].map((cell) => clean(cell.textContent)) : [],
      rows,
      provenance: clean(box && box.querySelector(".verdict-provenance") ? box.querySelector(".verdict-provenance").textContent : ""),
      space: box ? [...box.querySelectorAll(".verdict-basis")].map((node) => clean(node.textContent)) : [],
      copyLabel: clean(box && box.querySelector(".verdict-head .btn") ? box.querySelector(".verdict-head .btn").textContent : ""),
      focusOnCopy: Boolean(document.activeElement && document.activeElement.getAttribute
        && document.activeElement.getAttribute("onclick") === "copyHallVerdict(this)"),
      bannerVisible: Boolean(banner && banner.style.display !== "none"),
      bannerText: clean(banner ? banner.textContent : ""),
      status: clean(document.getElementById("hallStatusBadge") ? document.getElementById("hallStatusBadge").textContent : ""),
      voices: summary ? summary.voices : null,
      replies: summary ? summary.replies.length : null,
      overflowing: document.documentElement.scrollWidth - window.innerWidth,
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

async function openHall(page, args, baseUrl, token, { theme = "dark" } = {}) {
  await page.evaluateOnNewDocument((key, value, themeName) => {
    try {
      window.sessionStorage.setItem(key, value);
      localStorage.setItem("m5_theme", themeName);
    } catch (_error) {}
    // A headless window is not focused and the real clipboard refuses writes from it. What this
    // walk checks is the pane's own code, so the clipboard is stubbed and read back.
    try {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text) => { window.__m5CopiedText = String(text); } },
      });
    } catch (_error) {}
  }, args.tokenKey, token, theme);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 8);
  await page.evaluate(() => switchTab("hall"));
  await sleep(args.settleMs * 6);
}

/** A session created the way an operator creates one: the composer, posting a note only. */
async function startSession(page, args, task) {
  await page.evaluate((text) => {
    document.getElementById("hallHumanInput").value = text;
    document.getElementById("hallSendModeSelect").value = "post_only";
    submitHallComposer();
  }, task);
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await sleep(args.settleMs);
    const state = await page.evaluate(() => ({
      id: typeof activeHallId === "undefined" ? "" : activeHallId,
      title: (document.getElementById("hallTitle") || {}).textContent || "",
    }));
    if (state.id && state.title.trim()) return state.id;
  }
  throw new Error("the composer never created a hall session");
}

/** Exercise the already-mounted server path when a browser preflight would block it. */
async function summonThroughExistingRoute(page, sessionId, model, roleKey) {
  const result = await page.evaluate(async ({ id, selectedModel, role }) => {
    const response = await fetch(`/api/hall/sessions/${encodeURIComponent(id)}/summon`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: selectedModel, role_key: role, reasoning_mode: "off", auto_unload: true, skills: [] }),
    });
    const body = await response.text();
    return { status: response.status, body };
  }, { id: sessionId, selectedModel: model, role: roleKey });
  if (result.status !== 200) throw new Error(`the existing summon route returned ${result.status}: ${result.body}`);
}

/** Run the four seats and wait until the session holds one reply per seat. */
async function runRound(page, args) {
  await page.evaluate(() => document.getElementById("hallFullRoundBtn").click());
  for (let attempt = 0; attempt < 300; attempt += 1) {
    await sleep(args.settleMs);
    const running = await page.evaluate(() => {
      const button = document.getElementById("hallFullRoundBtn");
      return Boolean(button && button.disabled);
    });
    const verdict = await readVerdict(page);
    if (!running && verdict.replies === 4) return verdict;
  }
  return readVerdict(page);
}

/** Ask the Lead Arbiter for its verdict, which the server pins as the session's conclusion. */
async function conclude(page, args) {
  // The readiness refresh rewrites the seat pickers while a round runs, so the value the button
  // will read is set again here - the walk asks the picker's own seat for its verdict, as an
  // operator who looked at the picker would. A round unloads local models after each turn, so
  // refresh and Load now for the Arbiter rather than asking an unready single seat to dispatch.
  await page.evaluate(async () => refreshCouncilReadiness());
  const chosen = await page.evaluate(() => document.getElementById("hallSeatJudge").value);
  if (!chosen) throw new Error("the Arbiter's picker is empty");
  let readiness = await page.evaluate(() => ({
    state: document.getElementById("hallSeatJudgeState").dataset.state,
    action: document.getElementById("hallSeatJudgeState").querySelector("button")?.dataset.action || "",
  }));
  if (readiness.action === "load") {
    await page.evaluate(() => document.getElementById("hallSeatJudgeStateAction")?.click());
  }
  for (let attempt = 0; attempt < 100 && !["ready", "configured"].includes(readiness.state); attempt += 1) {
    await sleep(args.settleMs);
    readiness = await page.evaluate(() => ({
      state: document.getElementById("hallSeatJudgeState").dataset.state,
      action: document.getElementById("hallSeatJudgeState").querySelector("button")?.dataset.action || "",
    }));
  }
  if (!["ready", "configured"].includes(readiness.state)) {
    throw new Error(`the Arbiter remained unavailable after readiness refresh: ${readiness.state}`);
  }
  await page.evaluate(() => document.getElementById("hallConcludeBtn").click());
  for (let attempt = 0; attempt < 200; attempt += 1) {
    await sleep(args.settleMs);
    const verdict = await readVerdict(page);
    if (!verdict.hidden && verdict.summaryLine.includes("decision pinned") && verdict.status === "CLOSED") return verdict;
  }
  return readVerdict(page);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  // The Hall pane scrolls inside a body sized from the viewport, so a crop taken at 1440x1000 shows
  // the top of a block and cuts the rest. The viewport is grown first, then the element is captured.
  const TALL_VIEWPORT = 1800;
  const shoot = async (page, name, selector) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    await page.setViewport({ width: 1440, height: TALL_VIEWPORT, deviceScaleFactor: 1 });
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
      fixture = await startFixture(args, { offline: false });
      args.baseUrl = fixture.ready.base_url;
      args.token = args.token || fixture.ready.token;
    }
    if (!args.offlineBaseUrl) {
      offlineFixture = await startFixture(args, { offline: true });
      args.offlineBaseUrl = offlineFixture.ready.base_url;
      args.offlineToken = args.offlineToken || offlineFixture.ready.token;
    }
    const ready = (fixture && fixture.ready) || {};
    const loaded = ready.loaded_model;
    const fits = ready.unloaded_fits_model;

    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── an empty session draws no verdict ──────────────────────────────────
    await openHall(page, args, args.baseUrl, args.token);
    await startSession(page, args, TASK);
    const empty = await readVerdict(page);
    record(
      "a session with no reply draws no verdict at all",
      empty.hidden && empty.replies === 0 && !empty.bannerVisible,
      `hidden=${empty.hidden} replies=${empty.replies}`,
    );

    // ── four replies, two models, nothing pinned ───────────────────────────
    // Seats 1 and 4 share a model, seats 2 and 3 share the other: the room really does say two
    // things in four replies, which is what the pane has to be able to say.
    await pickModel(page, "hallSeatArch", loaded);
    await pickModel(page, "hallSeatCoder", fits);
    await pickModel(page, "hallSeatCritic", fits);
    await pickModel(page, "hallSeatJudge", loaded);
    await sleep(args.settleMs * 2);
    await page.evaluate(() => document.getElementById("hallSeatCoderStateAction")?.click());
    let readyRows = null;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      await sleep(args.settleMs);
      readyRows = await page.evaluate(() => ({
        coder: document.getElementById("hallSeatCoderState").dataset.state,
        critic: document.getElementById("hallSeatCriticState").dataset.state,
        disabled: document.getElementById("hallFullRoundBtn").disabled,
      }));
      if (readyRows.coder === "ready" && readyRows.critic === "ready" && !readyRows.disabled) break;
    }
    record(
      "a fitting local model is loaded before the readiness-guarded round",
      Boolean(readyRows && readyRows.coder === "ready" && readyRows.critic === "ready" && !readyRows.disabled),
      JSON.stringify(readyRows),
    );
    const unpinned = await runRound(page, args);
    record(
      "four replies and nothing pinned: the verdict area says no decision has been reached",
      !unpinned.hidden
        && unpinned.head === "Verdict"
        && unpinned.summaryLine === "4 replies · 2 voices · no decision pinned"
        && unpinned.headers.join(" | ") === "Seat | Model | Size | Shared wording with another seat | Reading"
        && /nothing has been settled/.test(unpinned.provenance)
        && unpinned.rows.length === 4,
      `${unpinned.summaryLine} | ${unpinned.provenance}`,
    );
    record(
      "the pane counts voices, not replies, and names the seat each repeated answer repeats",
      unpinned.rows.filter((row) => row.reading === "same").length === 4
        && /^the same words as Seat [14]$/.test(unpinned.rows[0].readingText)
        && /^the same words as Seat [23]$/.test(unpinned.rows[1].readingText)
        && /Seat 1 and Seat 4/.test(unpinned.space.join(" "))
        && /Seat 2 and Seat 3/.test(unpinned.space.join(" "))
        && /Repeated wording:/.test(unpinned.space.join(" "))
        && /would call this consensus; it is the same answer, repeated/.test(unpinned.space.join(" ")),
      unpinned.rows.map((row) => `${row.cells[0]}: ${row.readingText}`).join(" || "),
    );
    await shoot(page, "council-verdict-unpinned", "#hallVerdict");

    // ── the measure is what it says it is ──────────────────────────────────
    const arithmetic = await page.evaluate(() => {
      const measure = window.M5CouncilVerdict;
      const text = "the log keeps every message and the consumer tracks its own offset";
      return {
        self: measure.containedIn(text, text),
        inside: measure.containedIn("consumer tracks its own offset", text),
        apart: measure.containedIn("quarantine the poison message with a reason", text),
        nothing: measure.containedIn("", text),
        shortWords: measure.shingles("two words").size,
        shingles: measure.shingles(text).size,
        words: text.split(" ").length,
      };
    });
    record(
      "shared wording is the fraction of a reply's 4-word sequences found in the text it is compared with",
      arithmetic.self === 1
        && arithmetic.inside === 1
        && arithmetic.apart === 0
        && arithmetic.nothing === 0
        && arithmetic.shortWords === 2
        && arithmetic.shingles === arithmetic.words - 3,
      `self ${arithmetic.self} · fragment ${arithmetic.inside} · unrelated ${arithmetic.apart} · shingles ${arithmetic.shingles} of ${arithmetic.words} words`,
    );

    // ── the Arbiter's verdict, pinned ──────────────────────────────────────
    await pickModel(page, "hallSeatJudge", loaded);
    await sleep(args.settleMs * 2);
    const pinned = await conclude(page, args);
    // The Arbiter's verdict is a reply like any other, so asking for it appends a fifth message:
    // the seat that answered in the round has now answered twice, and the table has to say which
    // of the two is the decision.
    record(
      "pinning the Lead Arbiter's verdict states where the decision came from, word for word",
      !pinned.hidden
        && pinned.state !== "single"
        && /^5 replies · 2 voices · decision pinned from Seat 4 · Lead Arbiter \(reply 2\)'s reply, word for word at \d/.test(pinned.summaryLine)
        && /one seat's reply as written; it is not a synthesis/.test(pinned.provenance)
        && pinned.headers.join(" | ") === "Seat | Model | Size | Shared wording with the decision | Reading",
      `${pinned.summaryLine} | ${pinned.provenance}`,
    );
    record(
      "the pinned seat's row is the decision and every other reply is measured against it",
      pinned.rows.length === 5
        && pinned.rows[4].reading === "decision"
        && pinned.rows[4].readingText === "is the decision"
        && pinned.rows[4].cells[3] === "—"
        && /^Seat 4 · Lead Arbiter \(reply 2\)$/.test(pinned.rows[4].cells[0])
        && /^Seat 4 · Lead Arbiter \(reply 1\)$/.test(pinned.rows[3].cells[0])
        && pinned.rows[0].reading === "mostly"
        && pinned.rows[0].readingText === "mostly in the decision"
        && /^100 %$/.test(pinned.rows[0].cells[3])
        && pinned.rows.slice(1, 3).every((row) => row.reading === "dissent" && row.cells[3] === "0 %"),
      pinned.rows.map((row) => `${row.cells[0]}: ${row.cells[3]} → ${row.readingText}`).join(" || "),
    );
    record(
      "replies the decision does not carry are named as dissent and left in the transcript",
      /^Dissent: 2 of 5 replies are not carried into the decision - Seat 2 · Fast Coder \/ Analyst \(0 %\), Seat 3 · Adversarial Critic \(0 %\)\. Each stays in the transcript above, as written\./.test(pinned.space.join(" "))
        && pinned.bannerVisible
        && pinned.bannerText.includes("Pinned Council Conclusion (")
        && pinned.bannerText.length > 80,
      `${pinned.space.join(" ")} | banner ${pinned.bannerVisible} ${pinned.bannerText.slice(0, 60)}`,
    );
    record(
      "the session's status reads CLOSED once a decision is pinned",
      pinned.status === "CLOSED",
      `badge: ${pinned.status}`,
    );
    // The capture is the whole Hall pane, and the seat pickers are part of it: a readiness refresh
    // during the round rewrote them back to their fallbacks, so they are set to the models the
    // replies actually came from before the shot - otherwise the picture would show pickers naming
    // models the table above says did not answer.
    await pickModel(page, "hallSeatArch", loaded);
    await pickModel(page, "hallSeatCoder", fits);
    await pickModel(page, "hallSeatCritic", fits);
    await pickModel(page, "hallSeatJudge", loaded);
    await sleep(args.settleMs * 3);
    await shoot(page, "council-verdict-pinned", "#hallConclusionBanner");
    await shoot(page, "council-verdict-decision", "#hallVerdict");

    // ── copy ───────────────────────────────────────────────────────────────
    const markdown = await page.evaluate(() => window.M5CouncilVerdict.markdown());
    const copied = await page.evaluate(async () => {
      const button = document.querySelector("#hallVerdict .verdict-head .btn");
      if (!button) return "";
      button.focus();
      button.click();
      await new Promise((done) => setTimeout(done, 300));
      return JSON.stringify({ label: button.textContent, copied: window.__m5CopiedText || "" });
    });
    const copyResult = JSON.parse(copied || "{}");
    record(
      "Copy verdict writes the same table as markdown and says it copied",
      markdown.includes("| Seat | Model | Size | Shared wording with the decision | Reading |")
        && markdown.includes("Decision: pinned from Seat 4 · Lead Arbiter (reply 2)'s reply")
        && markdown.includes("| Seat 4 · Lead Arbiter (reply 2) |")
        && /Dissent: 2 of 5 replies/.test(markdown)
        && markdown.includes("It measures text, not intent.")
        && copyResult.label === "Copied!"
        && copyResult.copied === markdown,
      `button reads ${copyResult.label}, clipboard ${(copyResult.copied || "").length} chars`,
    );

    // ── a redundant redraw keeps the focus ─────────────────────────────────
    // The copy control says "Copied!" for 1.2 s; let it go back before reading it again.
    await sleep(1500);
    const focusHeld = await page.evaluate(async () => {
      const button = document.querySelector("#hallVerdict .verdict-head .btn");
      if (!button) return false;
      button.focus();
      renderActiveHall();
      renderActiveHall();
      await new Promise((done) => setTimeout(done, 200));
      return document.activeElement === button;
    });
    const afterRedraw = await readVerdict(page);
    record(
      "a redraw of the same session leaves the focus on the copy control",
      focusHeld && afterRedraw.copyLabel === "Copy verdict" && afterRedraw.focusOnCopy,
      `focus stayed: ${focusHeld}`,
    );

    // ── narrow and light ───────────────────────────────────────────────────
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readVerdict(page);
    record(
      "the verdict area fits a 480 px viewport",
      narrow.overflowing <= 0 && narrow.rows.length === 5 && !narrow.hidden,
      `scrollWidth - innerWidth = ${narrow.overflowing}`,
    );
    if (args.screenshots) {
      mkdirSync(args.screenshots, { recursive: true });
      await page.screenshot({ path: resolve(args.screenshots, "council-verdict-narrow.png") });
    }
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await page.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readVerdict(page);
    record(
      "the verdict area renders in the light theme with the same readings",
      light.rows.map((row) => row.reading).join(",") === pinned.rows.map((row) => row.reading).join(","),
      light.rows.map((row) => `${row.reading}`).join(" "),
    );
    await shoot(page, "council-verdict-light", "#hallVerdict");

    // ── the machine with no runtime behind it ──────────────────────────────
    const offlinePage = await browser.newPage();
    offlinePage.on("pageerror", (error) => pageErrors.push(String(error)));
    offlinePage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await offlinePage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openHall(offlinePage, args, args.offlineBaseUrl, args.offlineToken);
    const offlineSessionId = await startSession(offlinePage, args, TASK);
    await pickModel(offlinePage, "hallSeatArch", loaded);
    await pickModel(offlinePage, "hallSeatCoder", fits);
    await pickModel(offlinePage, "hallSeatCritic", loaded);
    await pickModel(offlinePage, "hallSeatJudge", fits);
    const offlineBlocked = await offlinePage.evaluate(() => document.getElementById("hallFullRoundBtn").disabled);
    record(
      "the offline Hall still blocks its local assignments before an invocation",
      offlineBlocked,
      `round disabled=${offlineBlocked}`,
    );
    // P4-01 deliberately blocks a user-initiated offline round. To exercise P4-03's rendering of
    // the server's existing demo transcript (including the runtime-dropping-after-preflight edge),
    // this isolated fixture calls the same mounted summon route directly after asserting the UI is
    // blocked. P4-01 separately asserts that the blocked UI sends no such request.
    for (const [role, model] of [["architect", loaded], ["coder", fits], ["critic", loaded], ["judge", fits]]) {
      await summonThroughExistingRoute(offlinePage, offlineSessionId, model, role);
    }
    await offlinePage.evaluate(async () => selectHallSession(activeHallId));
    await sleep(args.settleMs * 4);
    const demo = await readVerdict(offlinePage);
    record(
      "with no runtime the four demo replies are one voice, and the pane says so instead of consensus",
      !demo.hidden
        && demo.summaryLine === "4 replies · 1 voice · no decision pinned"
        && demo.rows.length === 4
        && demo.rows.every((row) => row.reading === "same")
        && demo.rows.every((row) => /demo text/.test(row.cells[2]))
        && /One voice, not 4: Seat 1, Seat 2, Seat 3 and Seat 4 \(\d+ %\)/.test(demo.space.join(" "))
        && /would call this consensus; it is the same answer, repeated/.test(demo.space.join(" ")),
      `${demo.summaryLine} || ${demo.rows.map((row) => `${row.cells[0]}: ${row.readingText}`).join(" · ")}`,
    );
    await shoot(offlinePage, "council-verdict-offline", "#pane-hall");

    record("no page error occurred", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    const unexpected = posts.filter((path) => !/^\/api\/hall\/sessions(\/|$)/.test(path)
      && !/^\/api\/models\/[^/]+\/load$/.test(path));
    record(
      "the pane uses only existing Hall/session routes and the existing local model-load route",
      unexpected.length === 0,
      [...new Set(posts)].join(", "),
    );
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
    console.error(`council verdict walk: ${error.message}`);
    process.exit(1);
  });
