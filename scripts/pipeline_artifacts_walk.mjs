#!/usr/bin/env node
/**
 * Check that Pipeline stages state their handoffs and artifacts (UIUX-P3-06), and that
 * failure, stop and explicit full-run retry behave honestly (UIUX-P3-07).
 *
 *   node scripts/pipeline_artifacts_walk.mjs --screenshots docs/media/pipeline-artifacts
 *
 * The four-seat pipeline is a chain - the architect writes a spec, the builder is handed it, the
 * critic is handed both, the judge is handed all three - and the pane drew four cards carrying a
 * state and a tok/s. Nothing on the screen said who fed whom, a seat that produced nothing looked
 * like a seat that had just started, and a run that was stopped looked like a run that was still
 * going.
 *
 * This walk starts `scripts/pipeline_artifacts_seed.py`, which serves the demo app and stages two
 * arrivals the canned demo reply cannot produce: a critic that returns usage and no content, and
 * an architect slow enough to stop mid-stream. Everything else - the events, the sizes, the saved
 * run id - is the server's.
 *
 * What it proves:
 *
 *   1. every stage card carries a row naming what that seat reads and which seat it feeds;
 *   2. the upstream size is the output the seats before it actually streamed, not the task;
 *   3. the first seat's row counts the task alone, and the last seat says it ends the run;
 *   4. every stage card carries the artifact it produced - characters, tokens, tok/s;
 *   5. a seat that produced nothing is drawn as empty, and the run line says the next seat was
 *      handed an empty section;
 *   6. the run line states how many seats finished, the total output, the handoffs that carried
 *      content, and the run id the server saved;
 *   7. the page says the sizes are this pane's stream, not the prompt the server assembles;
 *   8. the collapsed step card carries its seat's character count, through a stage filter;
 *   9. Run pipeline is reachable and operable from the keyboard;
 *  10. a run stopped mid-stream is drawn as stopped, is not given a run id, and says it was not
 *      saved;
 *  11. no page error occurs, and the pane posts only the council stream it already posted;
 *  12. SSE errors render Failed, while an explicit abort remains Stopped;
 *  13. partial output stays visible until retry is deliberately activated;
 *  14. the native Retry button is keyboard-operable and warns that all four stages restart;
 *  15. retry reuses the exact captured request even if form controls were edited afterwards;
 *  16. the replay begins at Architect, completes all four stages and does not claim a checkpoint;
 *  17. a stream closing without a terminal event is a failure, not a successful or stopped run.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 90000,
  screenshots: "",
  baseUrl: "",
  token: "",
  emptyModel: "",
  slowModel: "",
  port: 0,
};

const PROMPT =
  "Write an async token-bucket rate limiter in Python that is safe under concurrent asyncio tasks, with graceful cancellation.";

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
      case "--empty-model": args.emptyModel = next(); break;
      case "--slow-model": args.slowModel = next(); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--port": args.port = Number(next()); break;
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
    "Check that a pipeline run states its dependencies and artifacts.",
    "",
    "  node scripts/pipeline_artifacts_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts the seed fixture itself, so every number it reads is the",
    "server's. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-pipeline-artifacts.md.",
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

async function startFixture(args) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/pipeline_artifacts_seed.py"), "--port", String(args.port)],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The seed fixture did not report itself in time.\n${stderr}`)),
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
        rejectReady(new Error(`The seed fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The seed fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The seed fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { child, ready };
}

/** Everything the pane says about the pipeline, read out of the DOM it actually rendered. */
async function readPipeline(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const rows = {};
    for (const stage of [1, 2, 3, 4]) {
      const card = document.getElementById(`stage-dom-${stage}`);
      const handoff = document.getElementById(`stage-handoff-${stage}`);
      const artifact = document.getElementById(`stage-artifact-${stage}`);
      rows[stage] = {
        mounted: Boolean(card) && card.style.display !== "none",
        handoff: handoff ? clean(handoff.textContent) : null,
        upstream: handoff ? Number(handoff.dataset.upstream) : null,
        taskChars: handoff ? Number(handoff.dataset.taskChars) : null,
        artifact: artifact ? clean(artifact.textContent) : null,
        empty: artifact ? artifact.dataset.empty : null,
        chars: artifact ? Number(artifact.dataset.chars) : null,
        stepChars: clean((document.getElementById(`st-chars-${stage}`) || {}).textContent),
      };
    }
    const host = document.getElementById("councilArtifacts");
    const totals = window.M5Pipeline.artifactTotals();
    const stepState = {};
    for (const stage of [1, 2, 3, 4]) {
      stepState[stage] = clean((document.getElementById(`st-state-${stage}`) || {}).textContent);
    }
    return {
      rows,
      partialOutput: clean((document.getElementById("stage-md-1") || {}).textContent),
      stepState,
      summary: {
        hidden: host.hidden,
        state: host.dataset.state || "",
        text: clean(host.textContent),
        totalChars: Number(host.dataset.totalChars || -1),
        handoffs: Number(host.dataset.handoffs || -1),
        role: host.getAttribute("role") || "",
        live: host.getAttribute("aria-live") || "",
      },
      run: totals.run,
      seats: totals.seats,
      chain: window.M5Pipeline.seats.map((seat) => ({ stage: seat.stage, key: seat.key, reads: seat.reads, short: seat.short })),
      runButtonDisabled: document.getElementById("councilRunBtn").disabled,
      stopVisible: getComputedStyle(document.getElementById("councilStopBtn")).display !== "none",
      retryVisible: getComputedStyle(document.getElementById("councilRetryBtn")).display !== "none",
      retryDisabled: document.getElementById("councilRetryBtn").disabled,
      retryNotice: clean(document.getElementById("councilRetryNotice").textContent),
      totalStatus: clean(document.getElementById("councilTotalStatus").textContent),
      focusId: document.activeElement ? document.activeElement.id : "",
    };
  });
}

/** Pick a model in the dropdown the pane reads, adding the demo's staged one if it is absent. */
async function pickModel(page, selectId, value) {
  return page.evaluate((id, model) => {
    const select = document.getElementById(id);
    if (![...select.options].some((option) => option.value === model)) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      select.appendChild(option);
    }
    select.value = model;
    return select.value === model;
  }, selectId, value);
}

async function pollUntil(page, predicate, timeoutMs, label) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeoutMs) {
    last = await readPipeline(page);
    try {
      if (await page.evaluate(predicate, last === null)) return last;
    } catch (error) {
      throw new Error(`${label}: ${error.message}`);
    }
    await sleep(120);
  }
  return last;
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  // A capture of the stage stack has to be as tall as the stack: the pane scrolls inside its
  // own body, so an element shot taller than the viewport is cut off and padded in black.
  const shoot = async (page, name, selector) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    if (selector) {
      const element = await page.$(selector);
      if (element) {
        const box = await element.boundingBox();
        const height = Math.min(Math.max(1000, Math.ceil((box ? box.height : 0) + 120)), 4200);
        await page.setViewport({ width: 1440, height, deviceScaleFactor: 1 });
        await sleep(args.settleMs * 3);
        await element.screenshot({ path });
        await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
        return;
      }
    }
    await page.screenshot({ path, fullPage: false });
  };

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture(args);
      args.baseUrl = fixture.ready.base_url;
      args.token = args.token || fixture.ready.token;
      args.emptyModel = args.emptyModel || fixture.ready.empty_model;
      args.slowModel = args.slowModel || fixture.ready.slow_model;
    }

    const browser = await puppeteer.launch({
      executablePath: resolveChrome(),
      headless: true,
      args: [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean),
      ],
    });
    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
      const errors = [];
      const posts = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("request", (request) => {
        if (request.method() !== "POST") return;
        try {
          const url = new URL(request.url());
          if (url.origin === new URL(args.baseUrl).origin) posts.push(url.pathname);
        } catch (_error) {
          // not a URL worth checking
        }
      });
      await page.evaluateOnNewDocument((key, token) => {
        try {
          window.sessionStorage.setItem(key, token);
        } catch (_error) {}
      }, args.tokenKey, args.token);
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 6);
      await page.evaluate(() => switchTab("council"));
      await sleep(args.settleMs * 4);

      // ── A: a full run, with one seat that produces nothing ──────────────────
      const pickedEmpty = await pickModel(page, "c-critic", args.emptyModel);
      await page.evaluate((prompt) => { document.getElementById("councilPrompt").value = prompt; }, PROMPT);
      const before = await readPipeline(page);
      record(
        "a pane with no run shows no run line",
        before.summary.hidden === true && before.rows[1].handoff === null,
        `hidden=${before.summary.hidden}`,
      );

      await page.evaluate(() => document.getElementById("councilRunBtn").click());
      const finished = await pollUntil(
        page,
        () => document.getElementById("councilRunBtn").disabled === false && Boolean(window.M5Pipeline.artifactTotals().run),
        args.timeoutMs,
        "the full run never finished",
      );
      await sleep(args.settleMs * 8);
      const full = await readPipeline(page);
      await shoot(page, "pipeline-run-line", "#councilArtifacts");
      await shoot(page, "pipeline-all-stages", "#councilStagesContainer");

      const seats = full.seats || [];
      const charsOf = (stage) => (seats.find((seat) => seat.stage === stage) || { chars: -1 }).chars;
      const upstreamFor = (stage) => {
        const seat = (full.chain || []).find((entry) => entry.stage === stage);
        if (!seat) return -1;
        return seat.reads.reduce((total, key) => {
          const source = (full.chain || []).find((entry) => entry.key === key);
          return total + (source ? charsOf(source.stage) : 0);
        }, 0);
      };

      record(
        "every stage card carries a row naming what that seat reads",
        [1, 2, 3, 4].every((stage) => (full.rows[stage].handoff || "").includes("reads")),
        JSON.stringify([1, 2, 3, 4].map((stage) => full.rows[stage].handoff)),
      );
      record(
        "the row names every seat it was handed",
        /Architect/.test(full.rows[2].handoff)
          && /Architect/.test(full.rows[3].handoff) && /Builder/.test(full.rows[3].handoff)
          && /Architect/.test(full.rows[4].handoff) && /Builder/.test(full.rows[4].handoff) && /Critic/.test(full.rows[4].handoff),
        `2="${full.rows[2].handoff}" 3="${full.rows[3].handoff}" 4="${full.rows[4].handoff}"`,
      );
      record(
        "the upstream size is the output those seats actually streamed",
        [2, 3, 4].every((stage) => full.rows[stage].upstream === upstreamFor(stage) && full.rows[stage].upstream > 0),
        JSON.stringify([2, 3, 4].map((stage) => ({ stage, drawn: full.rows[stage].upstream, streamed: upstreamFor(stage) }))),
      );
      record(
        "the first seat counts the task alone, and the last seat ends the run",
        full.rows[1].upstream === 0 && full.rows[1].taskChars === PROMPT.length && /ends the run/.test(full.rows[4].handoff),
        `taskChars=${full.rows[1].taskChars} prompt=${PROMPT.length} 4="${full.rows[4].handoff}"`,
      );
      record(
        "every stage card carries the artifact it produced",
        [1, 2, 4].every((stage) => /chars/.test(full.rows[stage].artifact || "") && /tokens/.test(full.rows[stage].artifact || "") && full.rows[stage].chars === charsOf(stage) && full.rows[stage].chars > 0),
        JSON.stringify([1, 2, 4].map((stage) => ({ stage, artifact: full.rows[stage].artifact }))),
      );
      record(
        "a seat that produced nothing is drawn as empty, not as a quiet one",
        pickedEmpty === true
          && full.rows[3].empty === "true"
          && /produced nothing/.test(full.rows[3].artifact || "")
          && charsOf(3) === 0
          && full.rows[2].empty === "false",
        `3="${full.rows[3].artifact}" empty=${full.rows[3].empty}`,
      );
      record(
        "the run line says the next seat was handed an empty section",
        /Critic finished with no output/.test(full.summary.text),
        full.summary.text,
      );
      // A handoff carried content when the seat that read upstream produced something. The
      // empty critic read two seats and handed on nothing, so it is not counted.
      const carriedForward = (full.chain || []).filter(
        (seat) => seat.reads.length > 0 && charsOf(seat.stage) > 0,
      ).length;
      record(
        "the run line states the seats, the output and the handoffs that carried content",
        /4 of 4 stages finished/.test(full.summary.text)
          && full.summary.totalChars === [1, 2, 3, 4].reduce((total, stage) => total + charsOf(stage), 0)
          && full.summary.handoffs === carriedForward
          && full.summary.state === "ok",
        `state=${full.summary.state} chars=${full.summary.totalChars} handoffs=${full.summary.handoffs} text="${full.summary.text}"`,
      );
      record(
        "a saved run is named by the id the server returned",
        full.run && full.run.savedRunId > 0
          ? new RegExp(`saved as #${full.run.savedRunId}`).test(full.summary.text)
          : /did not report a saved run id/.test(full.summary.text),
        `savedRunId=${full.run ? full.run.savedRunId : "?"} text="${full.summary.text}"`,
      );
      record(
        "the page says the sizes are this pane's stream, not the prompt around it",
        /not the prompt the server assembles/.test(full.summary.text),
        full.summary.text,
      );
      record(
        "the run line is a live region a reader is told about",
        full.summary.role === "status" && full.summary.live === "polite",
        `role=${full.summary.role} live=${full.summary.live}`,
      );

      // ── B: the collapsed cards, through a filter ────────────────────────────
      await page.evaluate(() => selectStageFilter(3));
      await sleep(args.settleMs * 4);
      const filtered = await readPipeline(page);
      await shoot(page, "pipeline-empty-seat", "#councilStagesContainer");
      await shoot(page, "pipeline-step-chars", ".stepper");
      record(
        "the collapsed step card carries its seat's character count",
        [1, 2, 3, 4].every((stage) => filtered.rows[stage].stepChars === `${charsOf(stage)} ch`),
        JSON.stringify([1, 2, 3, 4].map((stage) => filtered.rows[stage].stepChars)),
      );
      record(
        "a stage filter still hides the other cards",
        filtered.rows[3].mounted === true && [1, 2, 4].every((stage) => filtered.rows[stage].mounted === false),
        JSON.stringify([1, 2, 3, 4].map((stage) => filtered.rows[stage].mounted)),
      );
      await page.evaluate(() => selectStageFilter(0));
      await sleep(args.settleMs * 2);

      // ── C: Run pipeline from the keyboard, then stop it mid-stream ──────────
      await pickModel(page, "c-arch", args.slowModel);
      const focused = await page.evaluate(() => {
        const button = document.getElementById("councilRunBtn");
        button.focus();
        return document.activeElement === button;
      });
      record("Run pipeline takes focus", focused === true, `focus=${await page.evaluate(() => document.activeElement.id)}`);
      await page.keyboard.press("Enter");
      const streaming = await pollUntil(
        page,
        () => document.getElementById("councilRunBtn").disabled === true,
        20000,
        "Enter never started a run",
      );
      record(
        "Enter on the focused button starts a run",
        streaming && streaming.runButtonDisabled === true && streaming.stopVisible === true,
        `disabled=${streaming ? streaming.runButtonDisabled : "?"} stop=${streaming ? streaming.stopVisible : "?"}`,
      );
      const partial = await pollUntil(
        page,
        () => window.M5Pipeline.artifactTotals().seats[0].chars > 0,
        30000,
        "the first seat never streamed",
      );
      await shoot(page, "pipeline-streaming", "#councilStagesContainer");
      const stopPressed = await page.evaluate(() => {
        const button = document.getElementById("councilStopBtn");
        if (getComputedStyle(button).display === "none") return false;
        button.click();
        return true;
      });
      const stopped = await pollUntil(
        page,
        () => document.getElementById("councilRunBtn").disabled === false,
        args.timeoutMs,
        "the stopped run never settled",
      );
      await sleep(args.settleMs * 8);
      const afterStop = await readPipeline(page);
      await page.evaluate(() => { document.getElementById("councilScroll").scrollTop = 0; });
      await shoot(page, "pipeline-stopped-run", "#councilScroll");

      record(
        "Stop was pressed while the first seat was still streaming",
        stopPressed === true && partial !== null && (partial.seats[0] || {}).chars > 0,
        `pressed=${stopPressed} streamed=${partial ? partial.seats[0].chars : "?"}`,
      );
      record(
        "a run stopped mid-stream is drawn as stopped",
        afterStop.summary.state === "partial" && /^Stopped after \d of 4 stages/.test(afterStop.summary.text),
        `state=${afterStop.summary.state} text="${afterStop.summary.text}"`,
      );
      record(
        "a stopped run is not given a run id, and says it was not saved",
        afterStop.run.savedRunId === 0
          && /not saved/.test(afterStop.summary.text)
          && !/saved as #/.test(afterStop.summary.text),
        `savedRunId=${afterStop.run.savedRunId} text="${afterStop.summary.text}"`,
      );
      record(
        "no seat is left saying it is streaming after the run has ended",
        [2, 3, 4].every((stage) => afterStop.stepState[stage] === "Queued" || afterStop.stepState[stage] === "Idle")
          && [1, 2, 3, 4].every((stage) => afterStop.stepState[stage] !== "Streaming"),
        JSON.stringify(afterStop.stepState),
      );
      record(
        "a deliberate Stop remains distinct and offers an explicit retry",
        afterStop.summary.state === "partial" && afterStop.totalStatus === "Stopped"
          && afterStop.retryVisible === true && /all four stages/i.test(afterStop.retryNotice),
        `state=${afterStop.summary.state} status=${afterStop.totalStatus} retry=${afterStop.retryVisible}`,
      );

      // ── D: fail then replay the exact request from Architect ─────────────────
      const retryBodies = [];
      const eventFrame = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
      const usage = { decode_tps: 1, ttft: 0.1, completion_tokens: 2, reasoning_tokens: 0 };
      const successFrames = [1, 2, 3, 4].map((stage) =>
        eventFrame("stage_start", { stage, model: "retry-fixture" })
        + eventFrame("token", { stage, kind: "content", text: `replay output ${stage}` })
        + eventFrame("stage_done", { stage, usage }),
      ).join("") + eventFrame("council_done", { run_id: 0, total_elapsed_s: 1.2 });
      let interceptionError = "";
      await page.setRequestInterception(true);
      page.on("request", async (request) => {
        let pathname = "";
        try { pathname = new URL(request.url()).pathname; } catch (_error) {}
        if (request.method() !== "POST" || pathname !== "/api/council/stream") {
          try { await request.continue(); } catch (error) { interceptionError = String(error); }
          return;
        }
        let body = {};
        try { body = JSON.parse(request.postData() || "{}"); } catch (error) { interceptionError = String(error); }
        retryBodies.push(body);
        const stream = retryBodies.length === 1
          ? eventFrame("stage_start", { stage: 1, model: body.architect_model || "retry-fixture" })
            + eventFrame("token", { stage: 1, kind: "content", text: "partial architect output before retry" })
            + eventFrame("error", { message: "fixture stage failure" })
          : retryBodies.length === 2
            ? successFrames
            : eventFrame("stage_start", { stage: 1, model: body.architect_model || "retry-fixture" })
              + eventFrame("token", { stage: 1, kind: "content", text: "partial output before close" });
        try {
          await request.respond({ status: 200, contentType: "text/event-stream", body: stream });
        } catch (error) {
          interceptionError = String(error);
        }
      });
      const capturedTask = "Retry must replay the exact captured four-stage request.";
      await page.evaluate((prompt) => { document.getElementById("councilPrompt").value = prompt; }, capturedTask);
      await pickModel(page, "c-arch", args.slowModel);
      await page.evaluate(() => document.getElementById("councilRunBtn").click());
      const failed = await pollUntil(
        page,
        () => !document.getElementById("councilRunBtn").disabled
          && window.M5Pipeline.artifactTotals().run.outcome === "failed"
          && getComputedStyle(document.getElementById("councilRetryBtn")).display !== "none",
        args.timeoutMs,
        "the injected SSE error never became a failed Pipeline run",
      );
      await sleep(args.settleMs * 3);
      const afterFailure = await readPipeline(page);
      await page.evaluate(() => { document.getElementById("councilScroll").scrollTop = 0; });
      await shoot(page, "pipeline-failed-run", "#councilScroll");
      record(
        "an SSE error is Failed rather than Stopped and retains its reason",
        afterFailure.summary.state === "failed" && afterFailure.totalStatus === "Failed"
          && /Failed after 0 of 4 stages/.test(afterFailure.summary.text)
          && /fixture stage failure/.test(afterFailure.summary.text),
        `state=${afterFailure.summary.state} text="${afterFailure.summary.text}"`,
      );
      record(
        "partial output remains visible before an explicit retry",
        Boolean(failed) && afterFailure.seats[0].chars > 0
          && /partial architect output before retry/.test(afterFailure.partialOutput),
        `architect chars=${afterFailure.seats[0].chars}`,
      );
      record(
        "retry copy explains full replay and replacement of partial output",
        afterFailure.retryVisible === true && afterFailure.retryDisabled === false
          && /starts again at Architect/.test(afterFailure.retryNotice)
          && /reruns all four stages/.test(afterFailure.retryNotice)
          && /replaces any partial output/.test(afterFailure.retryNotice),
        afterFailure.retryNotice,
      );
      await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
      const narrowRetry = await page.evaluate(() => {
        const button = document.getElementById("councilRetryBtn");
        const notice = document.getElementById("councilRetryNotice");
        button.scrollIntoView({ block: "center" });
        button.focus();
        const buttonRect = button.getBoundingClientRect();
        const noticeRect = notice.getBoundingClientRect();
        return {
          focused: document.activeElement === button,
          buttonFits: buttonRect.width > 0 && buttonRect.left >= 0 && buttonRect.right <= innerWidth,
          noticeFits: noticeRect.width > 0 && noticeRect.left >= 0 && noticeRect.right <= innerWidth,
        };
      });
      record(
        "retry action and disclosure fit and remain focusable at a narrow viewport",
        narrowRetry.focused && narrowRetry.buttonFits && narrowRetry.noticeFits,
        JSON.stringify(narrowRetry),
      );

      await page.evaluate(({ task, model }) => {
        document.getElementById("councilPrompt").value = task;
        const select = document.getElementById("c-arch");
        if (![...select.options].some((option) => option.value === model)) {
          const option = document.createElement("option"); option.value = model; option.textContent = model; select.appendChild(option);
        }
        select.value = model;
        document.getElementById("councilRetryBtn").focus();
      }, { task: "This edited task must not replace the retry payload.", model: "edited-after-failure" });
      const retryFocused = await page.evaluate(() => document.activeElement.id === "councilRetryBtn");
      await page.keyboard.press("Enter");
      const replayed = await pollUntil(
        page,
        () => !document.getElementById("councilRunBtn").disabled
          && window.M5Pipeline.artifactTotals().run.outcome === "done",
        args.timeoutMs,
        "the full-run retry never completed",
      );
      await sleep(args.settleMs * 3);
      const afterRetry = await readPipeline(page);
      await page.evaluate(() => { document.getElementById("councilScroll").scrollTop = 0; });
      await shoot(page, "pipeline-replayed-run", "#councilScroll");
      const exactReplay = retryBodies.length >= 2
        && JSON.stringify(retryBodies[0]) === JSON.stringify(retryBodies[1])
        && retryBodies[1].task === capturedTask
        && retryBodies[1].architect_model === args.slowModel;
      record(
        "Retry is keyboard-operable and reuses the exact captured request despite later form edits",
        retryFocused && exactReplay && retryBodies.length === 2,
        `focused=${retryFocused} requests=${retryBodies.length} samePayload=${exactReplay}`,
      );
      record(
        "the replay starts at Architect, reruns all four stages and clears the prior partial output only after activation",
        Boolean(replayed) && afterRetry.summary.state === "ok" && afterRetry.totalStatus === "Done"
          && afterRetry.stepState[1] === "Done" && afterRetry.stepState[4] === "Done"
          && !/partial architect output before retry/.test(afterRetry.partialOutput)
          && afterRetry.retryVisible === false,
        `state=${afterRetry.summary.state} stages=${JSON.stringify(afterRetry.stepState)}`,
      );

      await page.evaluate(() => { document.getElementById("councilPrompt").value = "A close without terminal event must be failed."; document.getElementById("councilRunBtn").click(); });
      const closedEarly = await pollUntil(
        page,
        () => !document.getElementById("councilRunBtn").disabled
          && window.M5Pipeline.artifactTotals().run.outcome === "failed",
        args.timeoutMs,
        "a stream closed without a terminal event but did not fail",
      );
      const afterClose = await readPipeline(page);
      record(
        "a premature stream close is Failed and keeps retry available",
        Boolean(closedEarly) && afterClose.summary.state === "failed"
          && /ended before reporting completion/.test(afterClose.summary.text)
          && afterClose.retryVisible === true,
        `state=${afterClose.summary.state} text="${afterClose.summary.text}"`,
      );
      await page.setRequestInterception(false);
      record("request interception completed without harness errors", !interceptionError, interceptionError);

      // ── E: nothing broke ────────────────────────────────────────────────────
      const unexpected = posts.filter((path) => path !== "/api/council/stream");
      record(
        "the pane posted only the council stream it already posted",
        unexpected.length === 0,
        `unexpected posts: ${unexpected.join(", ")}`,
      );
      record("no page error occurred", errors.length === 0, errors.join(" | "));
    } finally {
      await browser.close();
    }
  } finally {
    if (fixture && fixture.child) fixture.child.kill("SIGTERM");
  }

  const failed = results.filter((entry) => !entry.passed);
  const payload = {
    story: "UIUX-P3-07",
    related_story: "UIUX-P3-06",
    passed: results.length - failed.length,
    total: results.length,
    status: failed.length === 0 ? "pass" : "fail",
    results,
  };
  if (args.json) console.log(JSON.stringify(payload, null, 2));
  else console.log(`\n${payload.passed}/${payload.total} checks passed`);
  return failed.length === 0 ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
