#!/usr/bin/env node
/**
 * Check that the Council names its participants and says what each seat can actually reach
 * (UIUX-P4-01).
 *
 *   node scripts/council_readiness_walk.mjs --screenshots docs/media/council-readiness
 *
 * The pane had four pickers and no answers. It could not say what the Architect seat was for,
 * it could not say whether the model in a picker was resident, loadable, reachable, or
 * missing a key - and on a machine whose local runtime was down, every local seat was answered
 * by `_stream_omlx_completion`'s canned demo text while the transcript drew a tok/s badge for
 * it. A seat that cannot run failed into a four-second toast, and its card went on saying
 * "Connecting...".
 *
 * This walk starts the fixture twice:
 *
 *   * `scripts/council_readiness_seed.py` - a stand-in oMLX admin API on loopback, one alias
 *     saved with no key, so the real `/api/telemetry` and `/api/providers` report a resident
 *     model, an over-budget model, a recoverable one and a keyless provider;
 *   * the same fixture with `--offline` - no runtime and no alias, which is the preview
 *     machine every screenshot in this repository was taken on.
 *
 * What it proves:
 *
 *   1. four seats, each named by role and each saying what the seat is for;
 *   2. exact selected model ids, locality/source, and readiness before invocation;
 *   3. resident, over-budget, loading, keyless, not-listed, stale and offline states from the
 *      server's own reads;
 *   4. configured cloud/fallback routes are not described as reachability-tested;
 *   5. full-round, spawn-and-convene and unready single-seat actions are blocked before writes;
 *   6. Open Empty Room and Post note only remain available while seats are blocked;
 *   7. a ready single seat still dispatches with its role tag and server usage;
 *   8. Check again re-reads the two endpoints and the basis line ages;
 *   9. the readiness rows survive a narrow viewport and the light theme without overflowing;
 *  10. no page error occurs, and the pane uses only the existing session, message, summon and
 *      model-load routes.
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
  offlineBaseUrl: "",
  offlineToken: "",
  port: 0,
};

const REQUEST = "Design a durable job queue for 500 rps with at-least-once delivery.";

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
    "Check that the Council names its participants and states what each seat can reach.",
    "",
    "  node scripts/council_readiness_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/council_readiness_seed.py twice (with and",
    "without a runtime), so the states it reads are the server's. Needs Chrome/Chromium",
    "(STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).",
    "See docs/uiux-2-0-council-participants.md.",
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
    [
      resolve("scripts/council_readiness_seed.py"),
      // Each fixture picks its own free port: two fixtures on one number would fight.
      "--port", "0",
      ...(offline ? ["--offline"] : []),
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

const SEAT_ROWS = ["hallSeatArchState", "hallSeatCoderState", "hallSeatCriticState", "hallSeatJudgeState"];
const SEAT_SELECTS = ["hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge"];

/** The readiness block, read out of the DOM that drew it. */
async function readReadiness(page) {
  return page.evaluate((rowIds, selectIds) => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const rows = rowIds.map((id, index) => {
      const element = document.getElementById(id);
      const action = element ? element.querySelector("button") : null;
      const select = document.getElementById(selectIds[index]);
      return {
        id,
        exists: Boolean(element),
        state: element ? element.dataset.state || "" : "",
        text: clean(element ? element.textContent : ""),
        pill: clean(element && element.querySelector(".status") ? element.querySelector(".status").textContent : ""),
        tone: element && element.querySelector(".status") ? element.querySelector(".status").dataset.tone || "" : "",
        action: action
          ? { label: clean(action.textContent), kind: action.dataset.action || "", id: action.id, disabled: Boolean(action.disabled) }
          : null,
        model: select ? select.value : "",
        modelLabel: clean(element && element.querySelector(".seat-model-id") ? element.querySelector(".seat-model-id").textContent : ""),
        source: clean(element && element.querySelector(".seat-source") ? element.querySelector(".seat-source").textContent : ""),
      };
    });
    const summary = document.getElementById("councilReadinessSummary");
    const count = document.getElementById("councilReadinessCount");
    const basis = document.getElementById("councilReadinessBasis");
    const seats = document.getElementById("hallSeatsSettings");
    return {
      rows,
      summary: clean(summary ? summary.textContent : ""),
      count: clean(count ? count.textContent : ""),
      countTone: count ? count.dataset.tone || "" : "",
      basis: clean(basis ? basis.textContent : ""),
      basisHasNote: Boolean(basis && basis.classList.contains("evidence")),
      seatsOpen: seats ? seats.open : false,
      roundDisabled: Boolean(document.getElementById("hallFullRoundBtn")?.disabled),
      spawnConveneDisabled: Boolean(document.getElementById("spawnConveneBtn")?.disabled),
      singleDisabled: Boolean(document.getElementById("hallSummonBtn")?.disabled),
      singleState: document.getElementById("hallSingleState")?.dataset.state || "",
      singleText: clean(document.getElementById("hallSingleState")?.textContent || ""),
      sendDisabled: Boolean(document.getElementById("hallSendBtn")?.disabled),
      resourceDisclosure: clean(document.querySelector(".council-resource-note")?.textContent || ""),
      purposes: [...document.querySelectorAll("#hallSeatsSettings .seat-purpose")].map((node) => clean(node.textContent)),
      labels: [...document.querySelectorAll("#hallSeatsSettings .field-lbl")].map((node) => clean(node.textContent)),
      focusId: document.activeElement ? document.activeElement.id : "",
      overflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  }, SEAT_ROWS, SEAT_SELECTS);
}

/** One summon's failure block, and the cards around it. */
async function readSummonState(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const host = document.getElementById("hallSummonState");
    const box = host ? host.querySelector(".presentation") : null;
    const cards = [...document.querySelectorAll("#hallStream .voice-card")].map((card) => {
      const head = card.querySelector(".voice-head");
      const status = card.querySelector(".status");
      return {
        head: clean(head ? head.textContent : ""),
        role: clean(card.querySelector(".role-tag") ? card.querySelector(".role-tag").textContent : ""),
        demo: Boolean(status && /Demo reply/.test(status.textContent)),
        tokPerSecond: /tok\/s/.test(clean(head ? head.textContent : "")),
        connecting: /Connecting\.\.\.|Streaming\.\.\./.test(clean(head ? head.textContent : "")),
      };
    });
    return {
      visible: Boolean(box),
      kind: box ? box.dataset.kind : "",
      title: clean(box && box.querySelector(".presentation-title") ? box.querySelector(".presentation-title").textContent : ""),
      cause: clean(box && box.querySelector(".presentation-cause") ? box.querySelector(".presentation-cause").textContent : ""),
      context: clean(box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent : ""),
      actions: box ? [...box.querySelectorAll(".presentation-actions .btn")].map((button) => clean(button.textContent)) : [],
      cards,
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
  }, args.tokenKey, token, theme);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await sleep(args.settleMs * 8);
  await page.evaluate(() => switchTab("hall"));
  await sleep(args.settleMs * 6);
}

async function createSession(page, title) {
  return page.evaluate(async (label, request) => {
    const response = await fetch("/api/hall/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, title: label }),
    });
    const created = await response.json();
    await selectHallSession(created.id);
    return created.id;
  }, title, REQUEST);
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
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    // ── the machine with a runtime behind it ────────────────────────────────
    await openHall(page, args, args.baseUrl, args.token);
    await createSession(page, "Readiness walk");
    await pickModel(page, "hallSeatArch", ready.loaded_model);
    await pickModel(page, "hallSeatCoder", ready.unloaded_over_model);
    await pickModel(page, "hallSeatCritic", ready.keyless_model);
    await pickModel(page, "hallSeatJudge", ready.unloaded_fits_model);
    await sleep(args.settleMs * 4);
    let block = await readReadiness(page);

    record(
      "the four seats are named by role, and each says what the seat is for",
      block.labels.length === 4
        && block.labels[0].startsWith("1. Architect") && block.labels[3].startsWith("4. Lead Arbiter")
        && block.purposes.length === 4 && block.purposes.every((purpose) => purpose.length > 15),
      `${block.labels.join(" | ")}`,
    );
    record(
      "a resident local model is READY and names the memory it holds",
      block.rows[0].state === "ready" && /Resident, \d+(\.\d+)? GB/.test(block.rows[0].text) && block.rows[0].action === null,
      `${block.rows[0].pill} — ${block.rows[0].text}`,
    );
    record(
      "a listed model that does not fit the ceiling says so in gigabytes",
      block.rows[1].state === "needs-memory"
        && /Needs 12\.05 GB/.test(block.rows[1].text)
        && /4\.20 GB of the 20\.80 GB ceiling is free/.test(block.rows[1].text)
        && block.rows[1].action.kind === "load",
      `${block.rows[1].pill} — ${block.rows[1].text}`,
    );
    record(
      "an alias saved without an API key is NEEDS KEY, and offers the providers panel",
      block.rows[2].state === "needs-key"
        && /no API key/.test(block.rows[2].text)
        && block.rows[2].action.kind === "providers",
      `${block.rows[2].pill} — ${block.rows[2].text}`,
    );
    record(
      "a listed model that fits is NEEDS LOAD, and offers to load it",
      block.rows[3].state === "needs-load" && /loads on the first token/.test(block.rows[3].text)
        && block.rows[3].action.kind === "load",
      `${block.rows[3].pill} — ${block.rows[3].text}`,
    );
    record(
      "the summary counts ready assignments, names both reads, and states that an incomplete round is blocked",
      block.count === "1/4 READY"
        && /1 of 4 assignments are ready or configured/.test(block.summary)
        && /invocation is blocked/.test(block.summary)
        && block.basisHasNote
        && /GET \/api\/telemetry and GET \/api\/providers/.test(block.basis)
        && /not from the picker/.test(block.basis),
      `${block.count} — ${block.summary} — ${block.basis}`,
    );
    record(
      "each Council row shows its exact selected model and locality/source before invocation",
      block.rows.every((row) => row.model && row.modelLabel === row.model && (row.model.includes("::") ? row.source.startsWith("Cloud ·") : row.source === "Local · oMLX")),
      block.rows.map((row) => `${row.modelLabel} / ${row.source}`).join(" | "),
    );
    record(
      "shared-memory and per-turn unload policy are disclosed without claiming cloud reachability",
      /share this machine’s memory/i.test(block.resourceDisclosure)
        && /requests unload after each local turn/i.test(block.resourceDisclosure)
        && /does not confirm provider reachability/i.test(block.resourceDisclosure),
      block.resourceDisclosure,
    );
    const loadingFixture = await page.evaluate((modelId) => {
      const previous = { telemetry: lastTelemetry, readAt: councilTelemetryReadAt };
      const telemetry = JSON.parse(JSON.stringify(lastTelemetry));
      const model = (telemetry.models || []).find((entry) => entry.id === modelId);
      if (!model) return { state: "missing-fixture-model", disabled: false };
      model.loaded = false;
      model.is_loading = true;
      lastTelemetry = telemetry;
      councilTelemetryReadAt = Date.now();
      renderCouncilReadiness();
      const state = document.getElementById("hallSeatArchState").dataset.state;
      const disabled = document.getElementById("hallFullRoundBtn").disabled;
      lastTelemetry = previous.telemetry;
      councilTelemetryReadAt = previous.readAt;
      renderCouncilReadiness();
      return { state, disabled };
    }, ready.loaded_model);
    record(
      "a local assignment that is loading blocks a round before invocation",
      loadingFixture.state === "loading" && loadingFixture.disabled,
      JSON.stringify(loadingFixture),
    );

    const staleFixture = await page.evaluate(() => {
      const previous = councilTelemetryReadAt;
      councilTelemetryReadAt = Date.now() - COUNCIL_READ_MAX_AGE_MS - 1;
      renderCouncilReadiness();
      const states = ["hallSeatArchState", "hallSeatCoderState", "hallSeatCriticState", "hallSeatJudgeState"].map((id) => document.getElementById(id).dataset.state);
      const disabled = document.getElementById("hallFullRoundBtn").disabled;
      councilTelemetryReadAt = previous;
      renderCouncilReadiness();
      return { states, disabled };
    });
    record(
      "stale telemetry blocks all four seat assignments until a fresh read",
      staleFixture.states.length === 4 && staleFixture.states.every((state) => state === "stale") && staleFixture.disabled,
      JSON.stringify(staleFixture),
    );

    const emptySingle = await page.evaluate(() => {
      const select = document.getElementById("hallModelSelect");
      const previous = select.value;
      select.value = "";
      renderCouncilReadiness();
      const state = document.getElementById("hallSingleState").dataset.state;
      const disabled = document.getElementById("hallSummonBtn").disabled;
      select.value = previous;
      renderCouncilReadiness();
      return { state, disabled };
    });
    record(
      "an empty single-seat model cannot be summoned",
      emptySingle.state === "no-model" && emptySingle.disabled,
      JSON.stringify(emptySingle),
    );

    const configuredCloud = await page.evaluate((alias) => {
      const provider = configuredProviders.find((entry) => entry.alias === alias);
      if (!provider) return { state: "missing-fixture-provider", text: "" };
      const previous = provider.has_key;
      provider.has_key = true;
      councilProvidersReadAt = Date.now();
      renderCouncilReadiness();
      const row = document.getElementById("hallSeatCriticState");
      const value = { state: row.dataset.state, pill: row.querySelector(".status")?.textContent || "", text: row.textContent };
      provider.has_key = previous;
      renderCouncilReadiness();
      return value;
    }, ready.keyless_alias);
    record(
      "a keyed cloud alias is labelled CONFIGURED without claiming it is reachable",
      configuredCloud.state === "configured" && /CONFIGURED/.test(configuredCloud.pill)
        && /reachability has not been tested/.test(configuredCloud.text),
      JSON.stringify(configuredCloud),
    );

    record(
      "unready assignments disable full-round and spawn-and-convene actions",
      block.roundDisabled && block.spawnConveneDisabled,
      `round=${block.roundDisabled} spawn-convene=${block.spawnConveneDisabled}`,
    );
    await shoot(page, "council-readiness-mixed", "#pane-hall");

    const beforeBlocked = posts.length;
    await page.evaluate(() => runHallFullRound());
    await page.evaluate(() => confirmSpawnSession(true));
    await sleep(args.settleMs * 3);
    const blockedActions = posts.slice(beforeBlocked);
    record(
      "full-round and spawn-and-convene are rejected before any summon or new session request",
      !blockedActions.some((path) => /\/summon$/.test(path) || path === "/api/hall/sessions"),
      blockedActions.join(", "),
    );

    // the load action really loads
    await page.click("#hallSeatJudgeStateAction");
    let loaded = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await sleep(args.settleMs * 2);
      block = await readReadiness(page);
      if (block.rows[3].state === "ready") { loaded = block; break; }
    }
    record(
      "Load now really loads: the runtime reports the model resident and the row turns READY",
      Boolean(loaded) && /Resident, 3\.80 GB/.test(loaded.rows[3].text),
      loaded ? `${loaded.rows[3].pill} — ${loaded.rows[3].text}` : "the row never became ready",
    );
    if (loaded) await shoot(page, "council-seat-loaded", "#pane-hall");

    // a poll must not throw the keyboard user out of the row they are on
    const actionIds = await page.evaluate(() => [...document.querySelectorAll(".seat-readiness button")].map((button) => button.id));
    await page.evaluate(() => {
      const target = document.querySelector(".seat-readiness button");
      if (target) target.focus();
    });
    await sleep(3000);
    const stable = await page.evaluate(() => document.activeElement ? document.activeElement.id : "");
    record(
      "a telemetry poll leaves the focus on the action it was on",
      actionIds.length > 0 && stable === actionIds[0],
      `actions=${actionIds.join(",")} focus after two polls: ${stable}`,
    );

    // the keyless alias opens the providers panel
    await page.click("#hallSeatCriticStateAction");
    await sleep(args.settleMs * 4);
    const modalOpen = await page.evaluate(() => {
      const modal = document.getElementById("providersModal");
      if (!modal) return false;
      const style = window.getComputedStyle(modal);
      return style.display !== "none" && style.visibility !== "hidden";
    });
    record("the NEEDS KEY action opens the providers panel", modalOpen, "clicked Open providers");
    await page.evaluate(() => { try { closeProvidersModal(); } catch (_error) {} });
    await sleep(args.settleMs * 2);

    // a model the runtime does not list
    await pickModel(page, "hallSeatJudge", "a-model-the-runtime-does-not-list");
    await sleep(args.settleMs * 4);
    block = await readReadiness(page);
    await page.click("#hallSeatJudgeStateAction");
    await sleep(args.settleMs * 2);
    const focused = await page.evaluate(() => document.activeElement ? document.activeElement.id : "");
    record(
      "a model the runtime does not list is NOT LISTED, and its action moves focus to the picker",
      block.rows[3].state === "unknown-model" && /does not list/.test(block.rows[3].text) && focused === "hallSeatJudge",
      `${block.rows[3].pill} — focus=${focused}`,
    );

    // A missing seat disables single invocation and remains visible by its original id.
    await pickModel(page, "hallModelSelect", "a-model-the-runtime-does-not-list");
    await sleep(args.settleMs * 3);
    block = await readReadiness(page);
    const beforeSingleBlock = posts.length;
    await page.evaluate(() => summonSingleAgent());
    await sleep(args.settleMs * 4);
    const singleBlockedPosts = posts.slice(beforeSingleBlock);
    record(
      "single-seat invocation is disabled and blocked before a summon when its selected model is unavailable",
      block.singleDisabled
        && block.rows.length === 4
        && !singleBlockedPosts.some((path) => /\/summon$/.test(path))
        && /a-model-the-runtime-does-not-list/.test(await page.evaluate(() => document.getElementById("hallSingleState").textContent)),
      `disabled=${block.singleDisabled} posts=${singleBlockedPosts.join(", ")}`,
    );

    // Open Empty Room remains available even while the four-seat plan is blocked.
    await page.evaluate(() => openSpawnModal());
    await sleep(args.settleMs * 2);
    const beforeEmptyRoom = posts.length;
    await page.evaluate(() => confirmSpawnSession(false));
    await sleep(args.settleMs * 4);
    const emptyRoomPosts = posts.slice(beforeEmptyRoom);
    record(
      "Open Empty Room still creates a session without invoking a seat",
      emptyRoomPosts.includes("/api/hall/sessions") && !emptyRoomPosts.some((path) => /\/summon$/.test(path)),
      emptyRoomPosts.join(", "),
    );

    // Note-only remains enabled while round actions are unavailable.
    await page.evaluate(() => {
      document.getElementById("hallSendModeSelect").value = "post_only";
      document.getElementById("hallSendModeSelect").dispatchEvent(new Event("change", { bubbles: true }));
      document.getElementById("hallHumanInput").value = "Keep this note while Council seats are unavailable.";
    });
    block = await readReadiness(page);
    const beforeNoteOnly = posts.length;
    await page.evaluate(() => submitHallComposer());
    await sleep(args.settleMs * 4);
    const notePosts = posts.slice(beforeNoteOnly);
    const noteState = await page.evaluate(() => ({
      enabled: !document.getElementById("hallSendBtn").disabled,
      mode: document.getElementById("hallSendModeSelect").value,
      text: activeHallData && activeHallData.messages.length ? activeHallData.messages[activeHallData.messages.length - 1].content : "",
    }));
    record(
      "Post note only remains enabled and saves the note without summoning",
      !block.sendDisabled && noteState.enabled && noteState.mode === "post_only"
        && noteState.text === "Keep this note while Council seats are unavailable."
        && notePosts.some((path) => /\/message$/.test(path))
        && !notePosts.some((path) => /\/summon$/.test(path)),
      `${noteState.text} — ${notePosts.join(", ")}`,
    );

    // A ready single seat remains invokable and keeps the server's seat tag and usage.
    await pickModel(page, "hallModelSelect", ready.loaded_model);
    await sleep(args.settleMs * 3);
    const beforeReadySingle = posts.length;
    await page.evaluate(() => summonSingleAgent());
    let singleReply = null;
    for (let attempt = 0; attempt < 80; attempt += 1) {
      await sleep(args.settleMs * 2);
      singleReply = await readSummonState(page);
      if (singleReply.cards.some((card) => card.role)) break;
    }
    record(
      "a ready single seat still dispatches one model with its role tag and genuine usage",
      singleReply.cards.some((card) => card.role === "Seat 1 · Staff Architect")
        && singleReply.cards.filter((card) => card.role).every((card) => !card.demo && card.tokPerSecond)
        && posts.slice(beforeReadySingle).some((path) => /\/summon$/.test(path)),
      singleReply.cards.map((card) => `${card.role}/${card.demo}/${card.tokPerSecond}`).join(" | "),
    );

    // the basis ages, and Check again re-reads it
    const before = (await readReadiness(page)).basis;
    await sleep(2200);
    const aged = (await readReadiness(page)).basis;
    await page.evaluate(() => document.getElementById("councilReadinessRefresh").focus());
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 6);
    const after = await readReadiness(page);
    record(
      "Check again re-reads both endpoints from the keyboard, and the basis line says how old the reads are",
      aged !== before
        && /telemetry \d+ s old/.test(after.basis)
        && after.focusId === "councilReadinessRefresh",
      `${before} -> ${after.basis}`,
    );
    record(
      "the readiness rows fit the pane at 1440 px",
      after.overflow <= 0,
      `scrollWidth - innerWidth = ${after.overflow}`,
    );

    // narrow and light: the same rows, no overflow
    await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 6);
    const narrow = await readReadiness(page);
    record(
      "the rows survive a 480 px viewport without overflowing it",
      narrow.overflow <= 0 && narrow.rows.every((row) => row.exists && row.text.length > 0),
      `scrollWidth - innerWidth = ${narrow.overflow}`,
    );
    await shoot(page, "council-readiness-narrow", "#pane-hall");
    await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });

    await page.evaluate(() => applyTheme("light"));
    await sleep(args.settleMs * 4);
    const light = await readReadiness(page);
    record(
      "the rows render in the light theme with the same states",
      light.rows.map((row) => row.state).join(",") === narrow.rows.map((row) => row.state).join(","),
      light.rows.map((row) => `${row.state}/${row.tone}`).join(" "),
    );
    await shoot(page, "council-readiness-light", "#pane-hall");

    // ── the machine with no runtime behind it ───────────────────────────────
    const offlinePage = await browser.newPage();
    offlinePage.on("pageerror", (error) => pageErrors.push(String(error)));
    offlinePage.on("console", (message) => { if (message.type() === "error") pageErrors.push(message.text()); });
    await offlinePage.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
    await openHall(offlinePage, args, args.offlineBaseUrl, args.offlineToken);
    await createSession(offlinePage, "Offline readiness guard walk");
    for (const selectId of [...SEAT_SELECTS, "hallModelSelect"]) {
      await pickModel(offlinePage, selectId, ready.loaded_model);
    }
    await sleep(args.settleMs * 4);
    const selects = await offlinePage.evaluate((ids) => ids.map((id) => document.getElementById(id).value), SEAT_SELECTS);
    const demo = await readReadiness(offlinePage);
    record(
      "with no runtime, every local seat is DEMO TEXT and says the reply will not be the model's",
      demo.rows.every((row) => row.state === "demo")
        && demo.rows.every((row) => /demo text/.test(row.text))
        && demo.count === "0/4 READY"
        && /would receive demo text; invocation is blocked/.test(demo.summary),
      `${demo.count} — ${demo.rows[0].text}`,
    );
    await pickModel(offlinePage, "hallSeatArch", "combo/hybrid-resilient");
    await sleep(args.settleMs * 3);
    const comboOffline = await readReadiness(offlinePage);
    record(
      "a configured combo that only targets offline local models is demo text, not a green pill",
      comboOffline.rows[0].state === "demo"
        && /demo text/.test(comboOffline.rows[0].text)
        && comboOffline.rows[0].source === "Fallback combo · hybrid-resilient",
      `${comboOffline.rows[0].modelLabel} — ${comboOffline.rows[0].pill} — ${comboOffline.rows[0].text}`,
    );
    await pickModel(offlinePage, "hallSeatArch", ready.loaded_model);
    await sleep(args.settleMs * 3);
    await shoot(offlinePage, "council-readiness-offline", "#pane-hall");

    const beforeOfflineBlock = posts.length;
    await offlinePage.evaluate(() => runHallFullRound());
    await offlinePage.evaluate(() => confirmSpawnSession(true));
    await sleep(args.settleMs * 3);
    const offlineBlockedPosts = posts.slice(beforeOfflineBlock);
    const offlineAfter = await readReadiness(offlinePage);
    record(
      "offline local seats disable round, spawn-and-convene and single-seat invocation without sending",
      offlineAfter.rows.every((row) => row.state === "demo")
        && offlineAfter.roundDisabled && offlineAfter.spawnConveneDisabled && offlineAfter.singleDisabled
        && !offlineBlockedPosts.some((path) => path === "/api/hall/sessions" || /\/summon$/.test(path)),
      `${offlineAfter.count} — ${offlineBlockedPosts.join(", ")}`,
    );

    record(
      "no page error occurred on either machine",
      pageErrors.length === 0,
      pageErrors.slice(0, 3).join(" | "),
    );
    const unexpected = posts.filter((path) => path !== "/api/hall/sessions"
      && !/^\/api\/hall\/sessions\/[^/]+\/(?:summon|message)$/.test(path)
      && !/^\/api\/models\/[^/]+\/load$/.test(path));
    record(
      "the pane uses only existing session, message, summon and model-load routes",
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
    console.error(`council readiness walk: ${error.message}`);
    process.exit(1);
  });
