#!/usr/bin/env node
/**
 * UIV2-25: drive and photograph the selected-stage Pipeline workspace.
 *
 * Starts scripts/pipeline_recovery_seed.py and Chromium itself. Every state comes from the
 * server's real stream and the app's existing buffers/handlers; the walk never paints output.
 *
 * node scripts/pipeline_workspace_walk.mjs --out docs/media/pipeline-workspace-uiv2-25/after
 * Options: --only 1440x900,390x844 --theme light --json --toast-settle-ms 0
 * --capture-only is for a pre-change comparison: it records no checks and is not verification.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const VIEWPORTS = [
  { id: "1640x900", width: 1640, height: 900 },
  { id: "1440x900", width: 1440, height: 900 },
  { id: "1280x800", width: 1280, height: 800 },
  { id: "390x844", width: 390, height: 844 },
];
const THEMES = ["light", "dark"];
const STATES = ["empty", "loading", "populated", "long-content", "error", "recovery"];
const LONG_TASK = "Design a durable queue with an intentionally detailed task: preserve lease fencing, explain recovery after a worker dies holding a lease, and include the backpressure, replay and audit decisions in the final answer.";
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function parseArgs(argv) {
  const args = { out: "", only: "", theme: "", json: false, captureOnly: false, settle: 140, toastSettle: 5200, timeout: 90000 };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      const value = argv[index + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      index += 1;
      return value;
    };
    switch (arg) {
      case "--out": args.out = next(); break;
      case "--only": args.only = next(); break;
      case "--theme": args.theme = next(); break;
      case "--settle-ms": args.settle = Number(next()); break;
      case "--toast-settle-ms": args.toastSettle = Number(next()); break;
      case "--timeout-ms": args.timeout = Number(next()); break;
      case "--json": args.json = true; break;
      case "--capture-only": args.captureOnly = true; break;
      case "--help":
      case "-h": console.log("node scripts/pipeline_workspace_walk.mjs --out DIR [--only 1440x900] [--theme light] [--json]"); process.exit(0); break;
      default: throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!args.out) throw new Error("--out is required: captures are evidence, not an optional claim");
  if (args.theme && !THEMES.includes(args.theme)) throw new Error("--theme must be light or dark");
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
  throw new Error("No Chromium found. Set STUDIO_SCREENSHOT_CHROME; this walk refuses to fake a capture.");
}

async function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { const response = await fetch(url); if (response.status < 500) return true; } catch (_error) { /* starting */ }
    await sleep(150);
  }
  return false;
}

async function startFixture(args) {
  const child = spawn(process.env.STUDIO_PYTHON || "python3", [resolve("scripts/pipeline_recovery_seed.py"), "--port", "0"], { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] });
  let buffer = "", stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not start.\n${stderr}`)), args.timeout);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try { resolveReady(JSON.parse(line)); } catch (error) { rejectReady(error); }
    });
    child.on("exit", (code) => { clearTimeout(timer); rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`)); });
  });
  if (!await waitForHttp(`${ready.base_url}/`, args.timeout)) {
    child.kill("SIGTERM");
    throw new Error(`The seeded app never answered at ${ready.base_url}.\n${stderr}`);
  }
  return { child, ready };
}

async function openPipeline(page, args, baseUrl, token, theme, viewport, reload = false) {
  if (!reload) {
    await page.setViewport({ width: viewport.width, height: viewport.height, deviceScaleFactor: 1 });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.evaluateOnNewDocument((key, value, themeName) => {
      try { window.sessionStorage.setItem(key, value); window.localStorage.setItem("m5_theme", themeName); } catch (_error) { /* checked by app */ }
    }, "m5_api_token", token, theme);
    await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeout });
  } else await page.reload({ waitUntil: "domcontentloaded", timeout: args.timeout });
  await sleep(args.settle * 7);
  await page.evaluate(() => switchTab("council"));
  await sleep(args.settle * 5);
}

async function pickModel(page, selectId, value) {
  return page.evaluate((id, model) => {
    const select = document.getElementById(id);
    if (![...select.options].some((option) => option.value === model)) {
      const option = document.createElement("option"); option.value = model; option.textContent = model; select.appendChild(option);
    }
    select.value = model;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return select.value === model;
  }, selectId, value);
}

async function setTask(page, task) {
  await page.evaluate((value) => {
    const input = document.getElementById("councilPrompt"); input.value = value; input.dispatchEvent(new Event("input", { bubbles: true }));
  }, task);
}

async function waitForEnd(page, args, reason) {
  try {
    await page.waitForFunction(() => {
      const run = window.M5PipelineRecovery.state();
      return document.getElementById("councilRunBtn").disabled === false && Boolean(run && run.endReason);
    }, { timeout: args.timeout, polling: 100 });
  } catch (error) {
    const debug = await page.evaluate(() => ({ run: window.M5PipelineRecovery.state(), events: window.M5Pipeline.workspace.events() }));
    throw new Error(`${error.message}; terminal wait debug ${JSON.stringify(debug)}`);
  }
  const actual = await page.evaluate(() => window.M5PipelineRecovery.state()?.endReason || "");
  if (reason && actual !== reason) {
    const debug = await page.evaluate(() => ({ run: window.M5PipelineRecovery.state(), events: window.M5Pipeline.workspace.events() }));
    throw new Error(`Expected terminal reason ${reason}, got ${actual}; ${JSON.stringify(debug)}`);
  }
  await sleep(args.settle * 6);
}

async function readWorkspace(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const byId = (id) => document.getElementById(id);
    const box = (node) => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { top: Math.round(rect.top), bottom: Math.round(rect.bottom), width: Math.round(rect.width), height: Math.round(rect.height) };
    };
    const cards = {};
    for (const stage of [1, 2, 3, 4]) {
      const card = byId(`stage-dom-${stage}`);
      cards[stage] = {
        present: Boolean(card), visible: Boolean(card) && card.style.display !== "none",
        recovered: card ? card.dataset.recovered === "true" : false,
        text: card ? clean(card.textContent).slice(0, 500) : "",
        tail: card ? clean(card.textContent).slice(-500) : "",
      };
    }
    const recovery = byId("pipelineRecovery");
    const recoveryBox = recovery && recovery.querySelector(".presentation");
    const empty = byId("pipelineStageEmpty");
    const workspace = byId("pipelineWorkspace");
    const selected = window.M5Pipeline.workspace.selected();
    const outputBody = document.querySelector(`#stage-dom-${selected} .stage-output-body`);
    let checkpoint = null;
    try { checkpoint = JSON.parse(localStorage.getItem("m5_pipeline_checkpoint") || "null"); } catch (_error) { checkpoint = { error: true }; }
    return {
      stageButtons: [...document.querySelectorAll("#pane-council .stepper > .step-card")].filter((node) => node.id !== "step-card-0").map((node) => node.id),
      overview: Boolean(byId("step-card-0")),
      controls: ["councilPrompt", "councilRunBtn", "councilStopBtn", "c-arch", "c-coder", "c-critic", "c-judge", "c-unload", "c-reasoning"].map((id) => ({ id, present: Boolean(byId(id)) })),
      setup: { open: byId("pipelineSetup").open, summary: clean(byId("pipelineSetupSummary").textContent) },
      selected,
      focus: document.activeElement ? document.activeElement.id || document.activeElement.tagName : "",
      workspace: { state: workspace.dataset.state, stage: Number(workspace.dataset.stage), title: clean(byId("pipelineWorkspaceTitle").textContent), subtitle: clean(byId("pipelineWorkspaceSubtitle").textContent), status: clean(byId("pipelineWorkspaceState").textContent), box: box(workspace), text: clean(workspace.textContent) },
      output: {
        meta: clean(byId("pipelineOutputMeta").textContent), emptyHidden: empty.hidden, emptyState: empty.dataset.state || "", emptyText: clean(empty.textContent),
        body: outputBody ? { top: Math.round(outputBody.getBoundingClientRect().top), scrollHeight: outputBody.scrollHeight, clientHeight: outputBody.clientHeight, scrollTop: outputBody.scrollTop } : null,
      },
      cards,
      facts: { model: clean(byId("pipelineSelectedModel").textContent), input: clean(byId("pipelineSelectedInput").textContent), artifact: clean(byId("pipelineSelectedArtifact").textContent) },
      events: window.M5Pipeline.workspace.events(),
      activity: [...byId("pipelineActivityLog").children].map((row) => clean(row.textContent)),
      run: window.M5Pipeline.artifactTotals().run,
      totals: window.M5Pipeline.artifactTotals().seats,
      artifacts: { hidden: byId("councilArtifacts").hidden, text: clean(byId("councilArtifacts").textContent) },
      recovery: {
        hidden: recovery.hidden, state: recovery.dataset.state || "", title: recoveryBox ? clean(recoveryBox.querySelector(".presentation-title")?.textContent) : "", text: clean(recovery.textContent),
        actions: recoveryBox ? [...recoveryBox.querySelectorAll(".presentation-actions button")].map((button) => ({ id: button.dataset.actionId || "", label: clean(button.textContent), disabled: button.disabled })) : [],
      },
      checkpoint: checkpoint ? { reason: checkpoint.reason || "", stages: (checkpoint.stages || []).length, task: checkpoint.task || "" } : null,
      prompt: byId("councilPrompt").value,
      runDisabled: byId("councilRunBtn").disabled,
      stopVisible: getComputedStyle(byId("councilStopBtn")).display !== "none",
      totalStatus: clean(byId("councilTotalStatus").textContent),
      inspector: { text: clean(byId("insp-panel-stage").textContent), overview: clean(document.querySelector("#insp-panel-stage .insp-item .d")?.textContent) },
      recoveryAfterWorkspace: Boolean(workspace.compareDocumentPosition(recovery) & Node.DOCUMENT_POSITION_FOLLOWING),
      docOverflow: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
      timelineOverflow: Math.max(0, document.querySelector("#pane-council .stepper").scrollWidth - document.querySelector("#pane-council .stepper").clientWidth),
      timelineBox: box(document.querySelector("#pane-council .stepper")),
    };
  });
}

async function shoot(page, args, run, state) {
  console.log(`${run.theme}/${run.viewport}: capturing ${state}`);
  if ((state === "error" || state === "recovery") && args.toastSettle) await sleep(args.toastSettle);
  await page.evaluate((state, narrow) => {
    const scroller = document.getElementById("councilScroll"); if (scroller) scroller.scrollTop = 0;
    window.scrollTo(0, 0);
    document.querySelectorAll(".stage-output-body").forEach((node) => { node.scrollTop = 0; });
    // A narrow capture must show the surface that proves its state, not only the composer and
    // timeline above it. Desktop keeps the task-to-output hierarchy in one frame.
    const target = narrow && (state === "populated" || state === "long-content") ? "pipelineWorkspace"
      : narrow && (state === "error" || state === "recovery") ? "pipelineRecovery" : "";
    if (target) document.getElementById(target)?.scrollIntoView({ block: "start" });
  }, state, Number(run.viewport.split("x")[0]) < 800);
  await sleep(args.settle * 2);
  const directory = resolve(args.out, run.theme); mkdirSync(directory, { recursive: true });
  const name = `${state}-${run.viewport}.png`;
  const path = resolve(directory, name);
  await page.screenshot({ path, fullPage: false });
  const bytes = readFileSync(path);
  run.shots.push({ state, theme: run.theme, viewport: run.viewport, path: `${run.theme}/${name}`, bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
}

async function runCaptureOnly(browser, fixture, args, theme, viewport) {
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  const run = { theme, viewport: viewport.id, checks: [], shots: [], mode: "capture-only" };
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  try {
    await openPipeline(page, args, fixture.ready.base_url, fixture.ready.token, theme, viewport);
    await shoot(page, args, run, "empty");
    for (const id of ["c-arch", "c-coder", "c-critic", "c-judge"]) await pickModel(page, id, "combo/hybrid-resilient");
    await pickModel(page, "c-arch", fixture.ready.slow_model);
    await setTask(page, "Write a small queue and show the loading state while its first seat streams.");
    await page.evaluate(() => document.getElementById("councilRunBtn").click());
    await page.waitForFunction(() => document.getElementById("councilRunBtn").disabled && Boolean(window.M5PipelineRecovery.state()), { timeout: args.timeout, polling: 50 });
    await sleep(args.settle);
    await shoot(page, args, run, "loading");
    await waitForEnd(page, args, "done");
    await page.evaluate(() => selectStageFilter(2));
    await shoot(page, args, run, "populated");

    await pickModel(page, "c-judge", fixture.ready.long_model);
    await setTask(page, LONG_TASK);
    await page.evaluate(() => document.getElementById("councilRunBtn").click());
    await waitForEnd(page, args, "done");
    await page.evaluate(() => selectStageFilter(4));
    await shoot(page, args, run, "long-content");

    await pickModel(page, "c-critic", fixture.ready.failing_model);
    await setTask(page, "Retry this exact queue task after the Critic's provider fails.");
    await page.evaluate(() => document.getElementById("councilRunBtn").click());
    await waitForEnd(page, args, "failed");
    await shoot(page, args, run, "error");

    await openPipeline(page, args, fixture.ready.base_url, fixture.ready.token, theme, viewport, true);
    await page.waitForFunction(() => document.querySelector("#pipelineRecovery button[data-action-id=\"restore\"]"), { timeout: args.timeout, polling: 100 });
    await page.evaluate(() => document.querySelector("#pipelineRecovery button[data-action-id=\"restore\"]").click());
    await page.waitForFunction(() => window.M5PipelineRecovery.state()?.endReason === "recovered", { timeout: args.timeout, polling: 100 });
    await sleep(args.settle * 5);
    await page.evaluate(() => selectStageFilter(3));
    await shoot(page, args, run, "recovery");
    if (errors.length) throw new Error(`pre-change page errors: ${errors.join(" | ")}`);
  } finally { await context.close(); }
  return { run, failures: [] };
}

async function runOne(browser, fixture, args, theme, viewport) {
  if (args.captureOnly) return runCaptureOnly(browser, fixture, args, theme, viewport);
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  const run = { theme, viewport: viewport.id, checks: [], shots: [] };
  const errors = [], posts = [], failures = [];
  const record = (step, passed, detail = "") => {
    const check = { step, passed: Boolean(passed), detail: String(detail || ""), status: passed ? "pass" : "fail" };
    run.checks.push(check);
    if (!passed) failures.push(`${theme}/${viewport.id}: ${step}${detail ? ` — ${detail}` : ""}`);
  };
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("request", (request) => {
    try {
      const url = new URL(request.url());
      if (url.origin === new URL(fixture.ready.base_url).origin && request.method() === "POST") posts.push({ path: url.pathname, body: request.postData() || "" });
    } catch (_error) { /* not a URL this walk reads */ }
  });

  try {
    await openPipeline(page, args, fixture.ready.base_url, fixture.ready.token, theme, viewport);
    let pane = await readWorkspace(page);
    record("the timeline contains exactly the server's four stages", pane.stageButtons.join(",") === "step-card-1,step-card-2,step-card-3,step-card-4", pane.stageButtons.join(","));
    record("All stages and every original run/setup control survive", pane.overview && pane.controls.every((control) => control.present));
    record("setup is progressively disclosed with its state in the summary", !pane.setup.open && /4 voice selectors/.test(pane.setup.summary) && /auto-unload/.test(pane.setup.summary), pane.setup.summary);
    record("the no-run workspace is meaningful rather than an empty expanse", pane.workspace.box && pane.workspace.box.height >= 180 && !pane.output.emptyHidden && /starts with the Architect/.test(pane.output.emptyText), `${pane.workspace.box && pane.workspace.box.height}px`);
    record("the workspace immediately follows the timeline", pane.workspace.box && pane.timelineBox && pane.workspace.box.top >= pane.timelineBox.bottom - 1);
    record("no-run logs do not invent events", pane.events.length === 0 && /No run events yet/.test(pane.activity.join(" ")));

    await page.focus("#step-card-0"); await page.keyboard.press("ArrowRight");
    let keyboard = await readWorkspace(page);
    record("ArrowRight selects and focuses the Architect", keyboard.selected === 1 && keyboard.focus === "step-card-1", `${keyboard.selected} · ${keyboard.focus}`);
    await page.keyboard.press("ArrowRight"); await page.keyboard.press("End"); keyboard = await readWorkspace(page);
    record("Arrow and End keys reach the fourth real stage", keyboard.selected === 4 && keyboard.focus === "step-card-4");
    await page.keyboard.press("Home"); await page.keyboard.press("ArrowRight"); keyboard = await readWorkspace(page);
    record("Home returns to the preserved run overview", keyboard.selected === 1 && keyboard.focus === "step-card-1" && /Architect/.test(keyboard.workspace.title));
    record("a selected idle stage states its input and absent artifact honestly", /No output artifact yet/.test(keyboard.facts.artifact) && /task/i.test(keyboard.facts.input));
    await page.focus("#pipelineSetup > summary"); await page.keyboard.press("Enter");
    const setupOpen = await readWorkspace(page);
    record("Enter opens the setup disclosure and keeps its controls reachable", setupOpen.setup.open && setupOpen.focus === "pipelineSetupSummary");
    await page.keyboard.press("Enter"); pane = await readWorkspace(page);
    record("the stage strip and page do not scroll sideways", pane.docOverflow === 0 && pane.timelineOverflow === 0, `document=${pane.docOverflow}, timeline=${pane.timelineOverflow}`);
    await shoot(page, args, run, "empty");

    // Establish all four seats for every isolated viewport. A prior custom run can change the
    // app's next-picker defaults; only the behaviour under test may differ between runs.
    const baselineSeats = [];
    for (const id of ["c-arch", "c-coder", "c-critic", "c-judge"]) baselineSeats.push(await pickModel(page, id, "combo/hybrid-resilient"));
    record("each viewport explicitly starts from four known-safe voices", baselineSeats.every(Boolean));
    record("the slow fixture model is selected through the real picker", await pickModel(page, "c-arch", fixture.ready.slow_model));
    await setTask(page, "Write a small queue and show the loading state while its first seat streams.");
    await page.focus("#councilRunBtn"); await page.keyboard.press("Enter");
    await page.waitForFunction(() => window.M5Pipeline.workspace.events().some((event) => event.kind === "start" && event.stage === 1), { timeout: args.timeout, polling: 50 });
    await sleep(args.settle);
    const loading = await readWorkspace(page);
    record("Enter starts the existing council stream", loading.runDisabled && loading.stopVisible && loading.run && !loading.run.endReason);
    record("loading follows an observed stage_start", loading.events.some((event) => event.kind === "start" && event.stage === 1 && event.detail.includes(fixture.ready.slow_model)));
    record("the selected workspace and log show the active stage", loading.workspace.state === "loading" && loading.workspace.stage === 1 && loading.activity.some((row) => /Stage started/.test(row)));
    record("loading carries no fabricated percent or finished stage", !loading.run.endReason && !/\b\d+%/.test(loading.workspace.text) && !loading.cards[2].visible);
    await shoot(page, args, run, "loading");

    await page.focus("#stage-dom-1 .stage-output-head .btn");
    const focusBeforeStream = await page.evaluate(() => document.activeElement === document.querySelector("#stage-dom-1 .stage-output-head .btn"));
    await waitForEnd(page, args, "done");
    const focusAfterStream = await page.evaluate(() => ({ held: document.activeElement === document.querySelector("#stage-dom-1 .stage-output-head .btn"), active: document.activeElement ? document.activeElement.tagName : "none", selected: window.M5Pipeline.workspace.selected() }));
    record("streaming redraws retain focus on the stage's Copy MD control", focusBeforeStream && focusAfterStream.held, JSON.stringify(focusAfterStream));
    await page.evaluate(() => selectStageFilter(0)); pane = await readWorkspace(page);
    record("all four real stages complete from server events", pane.totals.every((seat) => seat.chars > 0) && pane.run.endReason === "done" && /4 of 4 stages finished/.test(pane.artifacts.text));
    record("All stages still shows all four output cards", [1, 2, 3, 4].every((stage) => pane.cards[stage].visible));
    record("normal close does not create a drop event", !pane.events.some((event) => event.kind === "dropped"));
    await page.evaluate(() => selectStageFilter(2)); pane = await readWorkspace(page);
    record("selected-stage detail shows one output and its actual artifact", pane.selected === 2 && pane.cards[2].visible && [1, 3, 4].every((stage) => !pane.cards[stage].visible) && pane.facts.artifact.startsWith(`${pane.totals[1].chars} chars`));
    record("completed activity and saved outcome are visible", pane.events.some((event) => event.kind === "done" && event.stage === 2) && pane.events.some((event) => event.kind === "saved") && !pane.artifacts.hidden);
    record("completed overview reports its terminal state", pane.totalStatus.toLowerCase() === "completed", pane.totalStatus);
    record("the Inspector mirrors the same terminal stage states", pane.inspector.overview.startsWith("completed") && !/(pending|active)/.test(pane.inspector.overview) && !/(Running|Streaming|Queued)/.test(pane.inspector.text), JSON.stringify(pane.inspector));
    record("routine saved-run confirmation follows selected output", pane.recoveryAfterWorkspace);
    record("selected output begins in the desktop viewport", viewport.width < 800 || (pane.output.body && pane.output.body.top < viewport.height - 40), JSON.stringify(pane.output.body));
    record("populated output does not overflow the page", pane.docOverflow === 0 && pane.timelineOverflow === 0);
    await shoot(page, args, run, "populated");

    record("the long fixture model is selected through the real picker", await pickModel(page, "c-judge", fixture.ready.long_model));
    await setTask(page, LONG_TASK);
    await page.evaluate(() => document.getElementById("councilRunBtn").click());
    await waitForEnd(page, args, "done");
    await page.evaluate(() => selectStageFilter(4)); pane = await readWorkspace(page);
    record("the long answer is real streamed output", pane.totals[3].chars >= fixture.ready.long_reply_chars && pane.cards[4].tail.includes("Check 12"), `${pane.totals[3].chars} chars`);
    record("the long task and Judge output stay with the selected stage", pane.prompt === LONG_TASK && pane.selected === 4 && /Judge/.test(pane.workspace.title) && pane.output.body && pane.output.body.scrollHeight > pane.output.body.clientHeight);
    record("long content scrolls inside its reader, not sideways", pane.docOverflow === 0 && pane.timelineOverflow === 0);
    const stability = await page.evaluate(() => {
      const scroller = document.getElementById("councilScroll");
      const body = document.querySelector("#stage-dom-4 .stage-output-body");
      const copy = document.querySelector("#stage-dom-4 .stage-output-head .btn");
      scroller.scrollTop = Math.min(180, scroller.scrollHeight - scroller.clientHeight);
      body.scrollTop = Math.min(120, body.scrollHeight - body.clientHeight);
      copy.focus({ preventScroll: true });
      const before = { page: scroller.scrollTop, output: body.scrollTop };
      window.M5Pipeline.render();
      return { before, after: { page: scroller.scrollTop, output: body.scrollTop }, focused: document.activeElement === copy };
    });
    record("a same-state redraw preserves scroll position and Copy MD focus", stability.focused && JSON.stringify(stability.before) === JSON.stringify(stability.after), JSON.stringify(stability));
    await shoot(page, args, run, "long-content");

    await pickModel(page, "c-critic", fixture.ready.failing_model);
    await setTask(page, "Retry this exact queue task after the Critic's provider fails.");
    const postsBeforeFailure = posts.length;
    await page.evaluate(() => document.getElementById("councilRunBtn").click());
    await waitForEnd(page, args, "failed"); pane = await readWorkspace(page);
    record("a provider failure selects the failed Critic workspace", pane.selected === 3 && pane.workspace.state === "error" && pane.workspace.status === "failed");
    record("failure, retry and the kept checkpoint are visible", !pane.recovery.hidden && pane.recovery.state === "failed" && pane.recovery.actions.some((action) => action.id === "retry" && !action.disabled) && pane.checkpoint && pane.checkpoint.reason === "failed");
    record("the error log contains the provider's own message", pane.events.some((event) => event.kind === "error" && event.stage === 3 && event.detail.includes(fixture.ready.failure_message)));
    record("the failed run never reports a saved success", pane.run.savedRunId === 0 && pane.run.endReason === "failed" && /nothing about this run is on record/.test(pane.recovery.text));
    record("failed overview and recovery placement match the terminal state", pane.totalStatus === "Failed" && !pane.recoveryAfterWorkspace, pane.totalStatus);
    const failurePost = JSON.parse(posts[postsBeforeFailure].body);
    record("failure used the unchanged four-seat API payload", posts[postsBeforeFailure].path === "/api/council/stream" && ["architect_model", "coder_model", "critic_model", "judge_model", "task", "auto_unload", "reasoning_mode"].every((key) => Object.hasOwn(failurePost, key)));
    await page.focus("#pipelineRecovery button[data-action-id=\"retry\"]");
    record("Retry is reachable by keyboard without hiding the failure", await page.evaluate(() => document.activeElement && document.activeElement.dataset.actionId === "retry") && !pane.recovery.hidden);
    await shoot(page, args, run, "error");

    await openPipeline(page, args, fixture.ready.base_url, fixture.ready.token, theme, viewport, true); pane = await readWorkspace(page);
    record("reload offers the checkpoint without drawing it as a run", pane.recovery.state === "checkpoint" && !pane.run && [1, 2, 3, 4].every((stage) => !pane.cards[stage].present));
    const postsBeforeRestore = posts.length;
    await page.focus("#pipelineRecovery button[data-action-id=\"restore\"]"); await page.keyboard.press("Enter");
    await page.waitForFunction(() => window.M5PipelineRecovery.state() && window.M5PipelineRecovery.state().endReason === "recovered", { timeout: args.timeout, polling: 100 });
    await sleep(args.settle * 5);
    await page.evaluate(() => selectStageFilter(3)); pane = await readWorkspace(page);
    record("Enter restores the actual checkpoint", pane.run.endReason === "recovered" && pane.cards[3].recovered && pane.workspace.state === "recovery");
    record("restoration starts no new backend stage or stream", posts.length === postsBeforeRestore);
    record("the recovered stage names its source and keeps the original task", /browser/.test(pane.recovery.text) && /not the server's record/.test(pane.recovery.text) && pane.prompt === "Retry this exact queue task after the Critic's provider fails.");
    record("recovery is logged and focus lands in the restored workspace", pane.events.some((event) => event.kind === "recovered") && pane.focus === "pipelineWorkspaceTitle", `${pane.focus}`);
    record("checkpoint recovery stays before the restored workspace", !pane.recoveryAfterWorkspace);
    record("recovery remains responsive without horizontal overflow", pane.docOverflow === 0 && pane.timelineOverflow === 0);
    await shoot(page, args, run, "recovery");

    record("only the existing council stream was posted", posts.every((post) => post.path === "/api/council/stream"));
    record("no page error occurred in any state", errors.length === 0, errors.join(" | "));
  } finally { await context.close(); }
  return { run, failures };
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const fixture = await startFixture(args);
  const browser = await puppeteer.launch({ executablePath: resolveChrome(), headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"] });
  const only = args.only ? args.only.split(",").map((id) => id.trim()) : VIEWPORTS.map((item) => item.id);
  const themes = args.theme ? [args.theme] : THEMES;
  const runs = [], failures = [];
  try {
    for (const theme of themes) {
      for (const viewport of VIEWPORTS) {
        if (!only.includes(viewport.id)) continue;
        const result = await runOne(browser, fixture, args, theme, viewport);
        runs.push(result.run); failures.push(...result.failures);
        console.log(`${theme}/${viewport.id}: ${result.run.checks.filter((check) => check.passed).length}/${result.run.checks.length} checks, ${result.run.shots.length} captures`);
        result.failures.forEach((failure) => console.error(`FAIL ${failure}`));
      }
    }
  } finally { await browser.close(); fixture.child.kill("SIGTERM"); }
  const checks = runs.flatMap((item) => item.checks), shots = runs.flatMap((item) => item.shots);
  const manifest = { story: "UIV2-25", mode: args.captureOnly ? "capture-only-pre-change" : "verification", producer: "scripts/pipeline_workspace_walk.mjs", fixture: "scripts/pipeline_recovery_seed.py", states: STATES, viewports: only, themes, captured_at: new Date().toISOString(), checks: { passed: checks.filter((check) => check.passed).length, total: checks.length }, captures: shots, runs, failures };
  mkdirSync(resolve(args.out), { recursive: true });
  writeFileSync(resolve(args.out, "capture-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`${checks.length - failures.length}/${checks.length} checks; ${shots.length} captures; manifest ${resolve(args.out, "capture-manifest.json")}`);
  if (args.json) console.log(JSON.stringify({ checks: manifest.checks, captures: shots.length, failures }, null, 2));
  if (failures.length) throw new Error(`${failures.length} check(s) failed:\n${failures.join("\n")}`);
  const expected = runs.length * STATES.length;
  if (shots.length !== expected) throw new Error(`capture count ${shots.length} does not equal ${expected}; no state may be claimed without its screenshot`);
}

run(parseArgs(process.argv.slice(2))).catch((error) => { console.error(`pipeline_workspace_walk: ${error.message}`); process.exit(1); });
