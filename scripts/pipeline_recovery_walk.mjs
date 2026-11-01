#!/usr/bin/env node
/**
 * Check that a failed pipeline is a state with a way back, and that the way back is honest
 * (UIUX-P3-07).
 *
 *   node scripts/pipeline_recovery_walk.mjs --screenshots docs/media/pipeline-recovery
 *
 * A run that failed used to be a toast: `Council error: HTTP 503`, four seconds, then nothing.
 * The seat it died on was not named, the output that had survived was not named, and there was
 * no record of it anywhere - `save_run()` writes a run only after the fourth seat returns. The
 * only way to try again was to retype the task, which may not have been the task that failed.
 *
 * This walk starts `scripts/pipeline_recovery_seed.py`, which wraps the server's own
 * `stream_any_model` so one seat dies after six tokens and another refuses before a token. The
 * failure is the provider's; the `error` event, the absence of `council_done` and the absence of
 * a saved run are the server's answers.
 *
 * What it proves:
 *
 *   1. a failed run draws a presentation, not a toast: the seat, the provider's own message, and
 *      the output that survived;
 *   2. it says what is on record, which is nothing - the route saves a run only after seat four;
 *   3. the salvage is written to this browser's checkpoint automatically, and says it is not the
 *      server's record;
 *   4. after a reload the checkpoint is still there and is offered, not silently restored;
 *   5. restoring draws the kept seats, marks them recovered, and re-runs nothing;
 *   6. Retry posts the payload that failed, not the controls as they are now (the form is edited
 *      before the press to prove it), and says that all four seats run again;
 *   7. a retry of a failing payload fails the same way, names attempt 2, and keeps the copy;
 *   8. a successful run clears the salvage, and states the run is on record with the id the
 *      server returned;
 *   9. a saved run is readable back through GET /api/history/export, and the view says so;
 *  10. a run that fails before the first seat is drawn as such, with nothing to salvage;
 *  11. Forget removes the copy, and the action is disabled with its reason afterwards;
 *  12. the dropped-stream state is drawn from the same renderer (driven by hand: a loopback
 *      stream cannot be dropped deterministically, and this walk does not pretend otherwise);
 *  13. Retry is reachable and operable from the keyboard;
 *  14. no page error occurs, and the pane posts nothing but the council stream it posted before.
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
  failingModel: "",
  failingFastModel: "",
  failureMessage: "",
  port: 0,
};

const PROMPT =
  "Write an async token-bucket rate limiter in Python that is safe under concurrent asyncio tasks, with graceful cancellation.";
const OTHER_PROMPT = "This is the edited task, and it must not be the one a retry posts.";

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
      case "--failing-model": args.failingModel = next(); break;
      case "--failing-fast-model": args.failingFastModel = next(); break;
      case "--failure-message": args.failureMessage = next(); break;
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
    "Check that a failed pipeline is stated and recoverable.",
    "",
    "  node scripts/pipeline_recovery_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts the seed fixture itself, so the failure it reads is the",
    "server's. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-pipeline-recovery.md.",
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
    [resolve("scripts/pipeline_recovery_seed.py"), "--port", String(args.port)],
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

/** The recovery block, the run line and the seats, read out of the DOM that rendered them. */
async function readRecovery(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const host = document.getElementById("pipelineRecovery");
    const box = host ? host.querySelector(".presentation") : null;
    const actions = box
      ? [...box.querySelectorAll(".presentation-actions .btn")].map((button) => ({
          id: button.dataset.actionId || "",
          label: clean(button.textContent),
          disabled: Boolean(button.disabled),
          reason: button.getAttribute("title") || "",
        }))
      : [];
    const artifacts = document.getElementById("councilArtifacts");
    let checkpoint = null;
    try {
      const raw = localStorage.getItem("m5_pipeline_checkpoint");
      checkpoint = raw ? JSON.parse(raw) : null;
    } catch (_error) {
      checkpoint = { error: "unreadable" };
    }
    const cards = {};
    for (const stage of [1, 2, 3, 4]) {
      const card = document.getElementById(`stage-dom-${stage}`);
      cards[stage] = {
        mounted: Boolean(card),
        recovered: card ? card.dataset.recovered === "true" : false,
        chars: (document.getElementById(`st-chars-${stage}`) || {}).textContent || "",
        state: clean((document.getElementById(`st-state-${stage}`) || {}).textContent),
      };
    }
    const state = window.M5PipelineRecovery.state();
    return {
      visible: Boolean(host) && !host.hidden,
      state: host ? host.dataset.state || "" : "",
      kind: box ? box.dataset.kind : "",
      role: box ? box.getAttribute("role") : "",
      title: clean((box && box.querySelector(".presentation-title")?.textContent) || ""),
      cause: clean((box && box.querySelector(".presentation-cause")?.textContent) || "").replace(/^Cause:\s*/, ""),
      context: clean((box && box.querySelector(".presentation-context")?.textContent) || "").replace(/^Kept:\s*/, ""),
      actions,
      why: clean((box && box.querySelector(".presentation-why")?.textContent) || ""),
      runLine: {
        state: artifacts ? artifacts.dataset.state || "" : "",
        text: clean(artifacts ? artifacts.textContent : ""),
        hidden: artifacts ? artifacts.hidden : true,
      },
      cards,
      checkpoint: checkpoint
        ? {
            reason: checkpoint.reason,
            failedStage: checkpoint.failedStage,
            error: checkpoint.error,
            task: checkpoint.task,
            payload: checkpoint.payload,
            stages: (checkpoint.stages || []).map((stage) => ({
              stage: stage.stage,
              chars: (stage.content || "").length,
              usage: Boolean(stage.usage),
            })),
            truncated: Boolean(checkpoint.truncation && checkpoint.truncation.truncated),
          }
        : null,
      run: state
        ? {
            attempt: state.attempt,
            endReason: state.endReason,
            failedStage: state.failedStage,
            savedRunId: state.savedRunId,
            recoveredFrom: state.recoveredFrom || "",
            payload: state.payload || null,
            error: state.error,
          }
        : null,
      promptValue: document.getElementById("councilPrompt").value,
      focusId: document.activeElement ? document.activeElement.id : "",
      focusInBlock: Boolean(host) && host.contains(document.activeElement),
    };
  });
}

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

async function waitForRunEnd(page, timeoutMs, label) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const done = await page.evaluate(() => document.getElementById("councilRunBtn").disabled === false);
    if (done) return true;
    await sleep(120);
  }
  throw new Error(`${label}: the run never settled`);
}

async function pressAction(page, actionId) {
  return page.evaluate((id) => {
    const button = document.querySelector(`#pipelineRecovery .presentation-actions .btn[data-action-id="${id}"]`);
    if (!button) return false;
    button.click();
    return true;
  }, actionId);
}

async function openPipeline(page, args) {
  await page.evaluateOnNewDocument((key, token) => {
    try {
      window.sessionStorage.setItem(key, token);
    } catch (_error) {}
  }, args.tokenKey, args.token);
  await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 6);
  await page.evaluate(() => {
    try {
      localStorage.removeItem("m5_pipeline_checkpoint");
    } catch (_error) {}
  });
  await page.evaluate(() => switchTab("council"));
  await sleep(args.settleMs * 4);
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
      args.failingModel = args.failingModel || fixture.ready.failing_model;
      args.failingFastModel = args.failingFastModel || fixture.ready.failing_fast_model;
      args.failureMessage = args.failureMessage || fixture.ready.failure_message;
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
      const gets = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("request", (request) => {
        try {
          const url = new URL(request.url());
          if (url.origin !== new URL(args.baseUrl).origin) return;
          if (request.method() === "POST") posts.push({ path: url.pathname, body: request.postData() || "" });
          if (request.method() === "GET" && url.pathname.startsWith("/api/")) gets.push(url.pathname);
        } catch (_error) {
          // not a URL worth checking
        }
      });
      await openPipeline(page, args);

      // ── A: a seat dies mid-stream ────────────────────────────────────────────
      const healthyCritic = await page.evaluate(() => document.getElementById("c-critic").value);
      await pickModel(page, "c-critic", args.failingModel);
      await page.evaluate((prompt) => { document.getElementById("councilPrompt").value = prompt; }, PROMPT);
      await page.evaluate(() => document.getElementById("councilRunBtn").click());
      await waitForRunEnd(page, args.timeoutMs, "the failing run");
      await sleep(args.settleMs * 8);
      const failed = await readRecovery(page);
      await shoot(page, "failed-run", "#pipelineRecovery");
      await shoot(page, "failed-run-seats", ".stepper");

      record(
        "a failed run draws a presentation, not a toast",
        failed.visible === true && failed.kind === "error" && failed.role === "alert" && /^The run failed at Critic \(seat 3\)$/.test(failed.title),
        `visible=${failed.visible} kind=${failed.kind} role=${failed.role} title="${failed.title}"`,
      );
      record(
        "the presentation carries the provider's own message and the seat it died on",
        failed.cause.includes(args.failureMessage) && failed.run.failedStage === 3,
        `cause="${failed.cause}" failedStage=${failed.run.failedStage}`,
      );
      record(
        "it names the output that survived",
        /characters from Architect, Builder/.test(failed.context) && failed.checkpoint.stages[0].chars > 0 && failed.checkpoint.stages[1].chars > 0,
        `context="${failed.context}"`,
      );
      record(
        "it says what is on record, which is nothing",
        /nothing about this run is on record/.test(failed.context) && failed.run.savedRunId === 0,
        `context="${failed.context}" savedRunId=${failed.run.savedRunId}`,
      );
      record(
        "the salvage is kept in this browser automatically, and says it is not the server's record",
        failed.checkpoint !== null
          && failed.checkpoint.reason === "failed"
          && failed.checkpoint.failedStage === 3
          && /kept a copy of what survived in this browser/.test(failed.context)
          && /not the server's record/.test(failed.context),
        `checkpoint=${JSON.stringify(failed.checkpoint && failed.checkpoint.reason)} context="${failed.context}"`,
      );
      record(
        "the card of the seat that died says it stopped",
        failed.cards[3].state === "Stopped" && failed.cards[1].state === "Done" && failed.cards[4].state === "Queued",
        JSON.stringify({ 1: failed.cards[1].state, 3: failed.cards[3].state, 4: failed.cards[4].state }),
      );
      record(
        "the actions are a retry and a way to forget the copy",
        failed.actions.map((action) => action.id).join(",") === "retry,forget" && failed.actions[1].disabled === false,
        JSON.stringify(failed.actions),
      );

      // ── B: the copy survives a reload, and is offered rather than restored ──
      await page.reload({ waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 8);
      await page.evaluate(() => switchTab("council"));
      await sleep(args.settleMs * 4);
      const reloaded = await readRecovery(page);
      await shoot(page, "checkpoint-after-reload", "#pipelineRecovery");
      record(
        "after a reload the kept run is still there and is offered, not drawn",
        reloaded.visible === true
          && reloaded.state === "checkpoint"
          && /^A run of this task was kept in this browser$/.test(reloaded.title)
          && Object.values(reloaded.cards).every((card) => card.mounted === false),
        `state=${reloaded.state} title="${reloaded.title}" cards=${
          JSON.stringify(Object.values(reloaded.cards).map((card) => card.mounted))
        }`,
      );
      record(
        "the offered copy names where it was kept and what restoring means",
        /this browser's storage/.test(reloaded.context) && /does not re-run anything/.test(reloaded.context),
        `context="${reloaded.context}"`,
      );

      const restored = await pressAction(page, "restore");
      await sleep(args.settleMs * 6);
      const afterRestore = await readRecovery(page);
      await sleep(args.settleMs * 16);
      await shoot(page, "restored-from-checkpoint", "#councilStagesContainer");
      // The seat that died had streamed six tokens before it failed, so the salvage carries a
      // partial third seat - and the fourth seat, which never started, is not drawn at all.
      record(
        "restoring draws every kept seat, including the partial one, and marks them recovered",
        restored === true
          && afterRestore.cards[1].mounted === true
          && afterRestore.cards[1].recovered === true
          && afterRestore.cards[1].chars === `${failed.checkpoint.stages[0].chars} ch`
          && afterRestore.cards[2].mounted === true
          && afterRestore.cards[3].mounted === true
          && afterRestore.cards[3].chars === `${failed.checkpoint.stages[2].chars} ch`
          && afterRestore.cards[4].mounted === false,
        JSON.stringify([1, 2, 3, 4].map((stage) => afterRestore.cards[stage])),
      );
      record(
        "the restored view says where it came from, and that nothing was re-run",
        afterRestore.runLine.state === "recovered"
          && /Read back from this browser's checkpoint/.test(afterRestore.runLine.text)
          && /nothing was re-run/.test(afterRestore.runLine.text),
        `state=${afterRestore.runLine.state} text="${afterRestore.runLine.text}"`,
      );
      record(
        "restoring puts the task back in the composer",
        afterRestore.promptValue === PROMPT,
        `prompt="${afterRestore.promptValue.slice(0, 60)}…"`,
      );

      // ── C: retry posts what failed, not the edited form ─────────────────────
      await pickModel(page, "c-coder", "a-model-that-was-not-in-the-failed-payload");
      await page.evaluate((prompt) => { document.getElementById("councilPrompt").value = prompt; }, OTHER_PROMPT);
      const postsBefore = posts.length;
      const pressed = await pressAction(page, "retry");
      await waitForRunEnd(page, args.timeoutMs, "the retried run");
      await sleep(args.settleMs * 8);
      const retried = await readRecovery(page);
      const retryPost = posts.slice(postsBefore).find((entry) => entry.path === "/api/council/stream");
      let retryBody = null;
      try {
        retryBody = retryPost ? JSON.parse(retryPost.body) : null;
      } catch (_error) {
        retryBody = null;
      }
      record(
        "Retry posts the payload that failed, not the controls as they are now",
        pressed === true
          && retryBody !== null
          && retryBody.task === PROMPT
          && retryBody.coder_model !== "a-model-that-was-not-in-the-failed-payload"
          && retryBody.coder_model === failed.run.payload.coder_model,
        `posted task="${(retryBody && retryBody.task || "").slice(0, 40)}…" coder=${retryBody && retryBody.coder_model}`,
      );
      record(
        "a retry of a failing payload fails the same way and says which attempt it is",
        retried.run.endReason === "failed"
          && retried.run.attempt === 2
          && /Attempt 2 of this task: retry re-runs all four seats/.test(retried.runLine.text),
        `endReason=${retried.run.endReason} attempt=${retried.run.attempt}`,
      );
      record(
        "the retry keeps a fresh copy of the salvage",
        retried.checkpoint !== null && retried.checkpoint.failedStage === 3,
        JSON.stringify(retried.checkpoint && retried.checkpoint.failedStage),
      );

      // ── D: a run that succeeds clears the salvage and is on record ─────────
      const beforeGood = posts.length;
      await pickModel(page, "c-critic", healthyCritic);
      await page.evaluate(() => document.getElementById("councilRunBtn").click());
      await waitForRunEnd(page, args.timeoutMs, "the healthy run");
      await sleep(args.settleMs * 8);
      const good = await readRecovery(page);
      await shoot(page, "run-on-record", "#pipelineRecovery");
      record(
        "a successful run clears the salvage",
        posts.length > beforeGood && good.checkpoint === null,
        `checkpoint=${good.checkpoint === null ? "null" : JSON.stringify(good.checkpoint.reason)}`,
      );
      record(
        "a saved run says it is on record, with the id the server returned",
        good.state === "recorded"
          && good.run.savedRunId > 0
          && new RegExp(`on record as #${good.run.savedRunId}`).test(good.title),
        `state=${good.state} title="${good.title}" savedRunId=${good.run.savedRunId}`,
      );

      // ── E: the server's own record, read back through the export ────────────
      const getsBefore = gets.filter((path) => path === "/api/history/export").length;
      const reloadedSaved = await pressAction(page, "reload-saved");
      await sleep(args.settleMs * 12);
      const fromServer = await readRecovery(page);
      await shoot(page, "reloaded-from-export", "#councilStagesContainer");
      const exportReads = gets.filter((path) => path === "/api/history/export").length;
      record(
        "a saved run is readable back through GET /api/history/export",
        reloadedSaved === true && exportReads > getsBefore,
        `pressed=${reloadedSaved} exportReads=${exportReads}`,
      );
      record(
        "the reloaded view says the record is the server's, and that nothing was re-run",
        fromServer.runLine.state === "recovered"
          && new RegExp(`Read back from the server's export of run #${good.run.savedRunId}`).test(fromServer.runLine.text),
        `state=${fromServer.runLine.state} text="${fromServer.runLine.text}"`,
      );
      record(
        "the seats the export carried are the seats that ran",
        [1, 2, 3, 4].every((stage) => fromServer.cards[stage].mounted === true && fromServer.cards[stage].recovered === true),
        JSON.stringify([1, 2, 3, 4].map((stage) => fromServer.cards[stage].mounted)),
      );

      // ── F: a seat that never starts ─────────────────────────────────────────
      await pickModel(page, "c-arch", args.failingFastModel);
      await page.evaluate((prompt) => { document.getElementById("councilPrompt").value = prompt; }, PROMPT);
      await page.evaluate(() => document.getElementById("councilRunBtn").click());
      await waitForRunEnd(page, args.timeoutMs, "the fast-failing run");
      await sleep(args.settleMs * 8);
      const fastFail = await readRecovery(page);
      record(
        "a run that fails before the first token names the seat that was asked, with nothing to salvage",
        fastFail.state === "failed"
          && /^The run failed at Architect \(seat 1\)$/.test(fastFail.title)
          && /no seat produced any output/.test(fastFail.context)
          && fastFail.cards[1].state === "Stopped",
        `state=${fastFail.state} title="${fastFail.title}" context="${fastFail.context}"`,
      );

      // ── G: Forget, and the disabled action that explains itself ─────────────
      const forgot = await pressAction(page, "forget");
      await sleep(args.settleMs * 4);
      const afterForget = await readRecovery(page);
      const forgetAction = afterForget.actions.find((action) => action.id === "forget");
      record(
        "Forget removes the copy, and the action is then disabled with its reason",
        forgot === true
          && afterForget.checkpoint === null
          && Boolean(forgetAction)
          && forgetAction.disabled === true
          && /no copy was kept/.test(forgetAction.reason),
        `checkpoint=${afterForget.checkpoint === null ? "null" : "present"} action=${JSON.stringify(forgetAction)}`,
      );

      // ── H: the dropped-stream state, through the real renderer ─────────────
      await page.evaluate(() => {
        const state = window.M5PipelineRecovery.state();
        // Driven by hand on purpose: a loopback stream cannot be dropped deterministically, and
        // this walk will not claim a drop it did not cause. Everything below this line is the
        // renderer the real stream ends in.
        state.endReason = "dropped";
        state.failedStage = 2;
        state.error = "";
        window.M5PipelineRecovery.render();
      });
      await sleep(args.settleMs * 3);
      const dropped = await readRecovery(page);
      record(
        "a stream that ends without a result is drawn as dropped, in the same renderer",
        dropped.state === "dropped"
          && dropped.kind === "error"
          && dropped.role === "alert"
          && /^The stream ended without a result$/.test(dropped.title)
          && /without the run's final event - the connection dropped or the server stopped/.test(dropped.cause),
        `state=${dropped.state} title="${dropped.title}" cause="${dropped.cause}"`,
      );

      // ── I: the keyboard ────────────────────────────────────────────────────
      const keyboard = await page.evaluate(() => {
        window.M5PipelineRecovery.render();
        const button = document.querySelector('#pipelineRecovery .presentation-actions .btn[data-action-id="retry"]');
        if (!button) return { found: false };
        button.focus();
        return { found: true, focusId: document.activeElement === button ? "action" : document.activeElement.id };
      });
      record("the retry action takes focus", keyboard.found === true && keyboard.focusId === "action", JSON.stringify(keyboard));

      // ── J: nothing broke ───────────────────────────────────────────────────
      const unexpectedPosts = posts.filter((entry) => entry.path !== "/api/council/stream").map((entry) => entry.path);
      const unexpectedGets = gets.filter((path) => path !== "/api/history/export" && path !== "/api/models" && path !== "/api/telemetry" && path !== "/api/providers" && path !== "/api/harness/sessions" && path !== "/api/start/recent" && path !== "/api/start/health" && path !== "/api/skills" && path !== "/api/mcp/servers" && path !== "/api/hall/sessions" && path !== "/api/opencontext/docs" && path !== "/api/onboarding" && path !== "/api/onboarding/preflight");
      record(
        "the pane posted only the council stream it posted before",
        unexpectedPosts.length === 0,
        `unexpected posts: ${unexpectedPosts.join(", ")}`,
      );
      record("no page error occurred", errors.length === 0, errors.join(" | "));
      if (unexpectedGets.length) {
        record("the pane read only routes it already read", false, `unexpected reads: ${[...new Set(unexpectedGets)].join(", ")}`);
      } else {
        record("the pane read only routes it already read", true, "");
      }
    } finally {
      await browser.close();
    }
  } finally {
    if (fixture && fixture.child) fixture.child.kill("SIGTERM");
  }

  const failedSteps = results.filter((entry) => !entry.passed);
  const payload = {
    story: "UIUX-P3-07",
    passed: results.length - failedSteps.length,
    total: results.length,
    status: failedSteps.length === 0 ? "pass" : "fail",
    results,
  };
  if (args.json) console.log(JSON.stringify(payload, null, 2));
  else console.log(`\n${payload.passed}/${payload.total} checks passed`);
  return failedSteps.length === 0 ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
