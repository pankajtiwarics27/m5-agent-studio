#!/usr/bin/env node
/**
 * Photograph and check the Start command center in every state it can be in (UIV2-24).
 *
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/start_command_center_walk.mjs --out docs/media/start-command-center-uiv2-24
 *
 * The story's evidence has to show more than a tidy desktop shot: six states - empty,
 * populated, long content, loading, error and recovery - on four viewports (1640x900,
 * 1440x900, 1280x800, 390x844) in both themes, from the real app, plus a seventh state
 * the story is about: a runtime port that answers with no models behind it.
 *
 * So the walk starts its own fixture rather than shooting whatever happens to be
 * running:
 *
 *   * a stand-in runtime it owns, so `GET /v1/models` can be turned from two models to
 *     none at the exact moment the pane presses "Check the runtime" - that is the only
 *     way to photograph the contradiction the story forbids calling ready;
 *   * `scripts/council_readiness_seed.py --runtime-url`, which brings up a seeded app
 *     (token, provider, workspace, session) pointed at that runtime;
 *   * state the walk sets through the app's own APIs - sessions created and deleted,
 *     the workspace directory set - so nothing is poked into SQLite directly.
 *
 * Each run records checks as it goes. A run is not allowed to report a capture it did
 * not take: every state asserts something about the DOM before it shoots, and a failed
 * assertion fails the run.
 */
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { existsSync } from "node:fs";

const VIEWPORTS = [
  { id: "1640x900", width: 1640, height: 900 },
  { id: "1440x900", width: 1440, height: 900 },
  { id: "1280x800", width: 1280, height: 800 },
  { id: "390x844", width: 390, height: 844 },
];
const THEMES = ["light", "dark"];

const MODELS = ["Qwen3.6-35B-REAP-19B", "Gemma-4-21B-REAP-4bit"];

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function parseArgs(argv) {
  const args = { baseUrl: "", token: "", out: "", json: false, only: "", settle: 220, keep: false };
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
      case "--out": args.out = next(); break;
      case "--only": args.only = next(); break;
      case "--settle-ms": args.settle = Number(next()); break;
      case "--json": args.json = true; break;
      case "--keep": args.keep = true; break;
      case "--help":
      case "-h":
        console.log(usage());
        process.exit(0);
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!args.out) throw new Error("--out is required: this walk exists to write captures");
  return args;
}

function usage() {
  return [
    "Photograph and check the Start command center in its seven states.",
    "",
    "  node scripts/start_command_center_walk.mjs --out DIR [--json] [--only 1440x900]",
    "",
    "Starts its own app and its own stand-in runtime (see the header), so it needs no",
    "server already running. Needs Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE); STUDIO_PYTHON picks the interpreter for the fixture.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
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
  throw new Error(`puppeteer-core is not importable. Set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

function resolveChrome() {
  const explicit = process.env.STUDIO_SCREENSHOT_CHROME;
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`STUDIO_SCREENSHOT_CHROME does not exist: ${explicit}`);
    return explicit;
  }
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("No Chrome/Chromium found. Set STUDIO_SCREENSHOT_CHROME. This script refuses to fake a capture.");
}

/**
 * A stand-in runtime the walk controls.
 *
 * `empty()` answers `/v1/models` with an empty list while the socket stays up: the exact
 * shape the story says must never be read as ready. `restore()` puts the models back.
 */
async function startStubRuntime() {
  const state = { models: [...MODELS] };
  const server = createServer((req, res) => {
    const path = (req.url || "/").split("?")[0];
    const send = (payload, status = 200) => {
      const body = JSON.stringify(payload);
      res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
      res.end(body);
    };
    if (path === "/v1/models") {
      return send({ object: "list", data: state.models.map((id) => ({ id, object: "model" })) });
    }
    if (path === "/admin/api/models") {
      return send({
        models: state.models.map((id) => ({
          id,
          loaded: true,
          virtual: false,
          is_helper: false,
          is_hidden: false,
          settings: {},
          actual_size: 0,
          estimated_size: 0,
        })),
      });
    }
    if (path === "/admin/api/activity") return send({ models: [], model_memory_used: 0, model_memory_max: 0 });
    if (path === "/admin/api/stats") return send({ total_requests: 0, total_completion_tokens: 0, total_cached_tokens: 0, runtime_cache: {} });
    if (path === "/admin/api/global-settings") return send({ memory: { memory_guard_custom_ceiling_gb: 20.8, memory_guard_tier: "custom" } });
    if (path === "/v1/chat/completions") {
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.write(`data: ${JSON.stringify({ choices: [{ delta: { content: "READY" } }] })}\n\n`);
      res.write(`data: ${JSON.stringify({ usage: { completion_tokens: 1, prompt_tokens: 1 } })}\n\n`);
      return res.end("data: [DONE]\n\n");
    }
    return send({ detail: "not found" }, 404);
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const port = server.address().port;
  return {
    url: `http://127.0.0.1:${port}`,
    empty() { state.models = []; },
    restore() { state.models = [...MODELS]; },
    close() { server.close(); },
  };
}

function writeFixtureSupport() {
  const dir = mkdtempSync(join(tmpdir(), "start-command-center-fixture-"));
  writeFileSync(join(dir, "sitecustomize.py"), [
    "import keyring",
    "_MEMORY = {}",
    "def _set(service, item, value):",
    "    _MEMORY[(service, item)] = value",
    "def _get(service, item):",
    "    return _MEMORY.get((service, item))",
    "def _delete(service, item):",
    "    _MEMORY.pop((service, item), None)",
    "keyring.set_password = _set",
    "keyring.get_password = _get",
    "keyring.delete_password = _delete",
    "",
  ].join("\n"));
  return dir;
}

async function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (error) { /* not up yet */ }
    await sleep(150);
  }
  return false;
}

async function startFixture(runtimeUrl) {
  const support = writeFixtureSupport();
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", MODELS[0], "--port", "0", "--runtime-url", runtimeUrl],
    {
      cwd: process.cwd(),
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        // The pane's execution row reads the mode; the default (disabled) is what a
        // first run on a laptop without a container runtime sees.
        STUDIO_EXECUTION_MODE: "",
        PYTHONPATH: [support, process.env.PYTHONPATH].filter(Boolean).join(":"),
      },
    },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)), 120000);
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
  const healthy = await waitForHttp(`${ready.base_url}/`, 90000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { child, ready };
}

/** Everything the landing state of the pane is claiming, read from the DOM. */
function readPane(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const box = (node) => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return { top: Math.round(rect.top), bottom: Math.round(rect.bottom), left: Math.round(rect.left), width: Math.round(rect.width) };
    };
    const pane = document.getElementById("pane-start");
    const cards = [...pane.querySelectorAll(".m-card")];
    const details = ["startDiagnostics", "startHealthDetails", "startWorkspaceDetails"].map((id) => {
      const node = document.getElementById(id);
      return { id, open: node ? node.open : null, tag: node ? node.tagName : null };
    });
    const blocker = document.getElementById("startNextBlocker");
    return {
      viewport: { width: window.innerWidth, height: window.innerHeight },
      firstCard: cards[0] ? cards[0].id || cards[0].className : null,
      composer: box(document.getElementById("startTaskInput")),
      button: box(document.getElementById("startTaskBtn")),
      line: box(document.getElementById("startReadinessLine")),
      lineText: clean((document.getElementById("startReadinessLine") || {}).textContent),
      pill: clean((document.getElementById("startReadinessPill") || {}).textContent),
      blockerHidden: blocker ? blocker.hidden : null,
      blockerText: clean(blocker ? blocker.textContent : "").slice(0, 240),
      blockerActions: blocker ? [...blocker.querySelectorAll("button")].map((button) => clean(button.textContent)) : [],
      blockerBox: blocker ? box(blocker) : null,
      contradictions: [...document.querySelectorAll("#startContradictions .start-contradiction")].map((row) => ({
        id: row.dataset.contradiction || "",
        title: clean((row.querySelector(".start-contradiction-title") || {}).textContent),
        evidence: clean((row.querySelector(".start-contradiction-evidence") || {}).textContent),
        actions: [...row.querySelectorAll("button")].map((button) => clean(button.textContent)),
      })),
      details,
      recentRows: document.querySelectorAll("#startRecentList .start-recent-row").length,
      recentEmpty: Boolean(document.querySelector("#startRecentState .presentation")),
      presentations: [...document.querySelectorAll("#pane-start .presentation")].map((node) => node.dataset.kind || ""),
      docOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      overflowRows: [...document.querySelectorAll("#startRecentList .start-recent-title")].filter((node) => node.scrollWidth > node.clientWidth + 1).length,
      longTitleClipped: [...document.querySelectorAll("#startRecentList .start-recent-title")].every((node) => {
        const style = window.getComputedStyle(node);
        return style.textOverflow === "ellipsis" && style.overflow === "hidden";
      }),
      draft: (document.getElementById("startTaskInput") || {}).value || "",
      summaryStates: {
        checklist: clean((document.querySelector("#startDiagnostics #onboardingSummary") || {}).textContent),
        health: clean((document.querySelector("#startHealthDetails #startHealthSummary") || {}).textContent),
        workspace: clean((document.querySelector("#startWorkspaceDetails #startWorkspaceSummary") || {}).textContent),
      },
    };
  });
}

const LONG_TITLE = "Reconcile the harness lease table with the sandbox container registry after the residency change";
const LONG_WORKSPACE = "projects/uiux-2-4-a-deliberately-long-workspace-directory-name-for-wrapping";
const LONG_TASK = "Rewrite the memory-admission guard so a leaked lease cannot starve the resident model, keeping the ledger and lease fencing exactly as they are, then run the suite and report the deltas";

async function clearSessions(page) {
  await page.evaluate(async () => {
    const payload = await (await fetch("/api/harness/sessions")).json();
    for (const session of payload.sessions || []) {
      await fetch(`/api/harness/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    }
  });
}

async function seedSessions(page, titles) {
  return page.evaluate(async (rows) => {
    const created = [];
    for (const title of rows) {
      const response = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      created.push({ title, status: response.status });
    }
    return created;
  }, titles);
}

async function setWorkspace(page, dir) {
  return page.evaluate(async (target) => {
    const response = await fetch("/api/workspace/directory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_dir: target, create_if_missing: true }),
    });
    return { status: response.status, body: await response.json().catch(() => ({})) };
  }, dir);
}

async function openStart(page) {
  await page.evaluate(() => switchTab("start"));
  // Both lists are re-read on purpose: the pane shows sessions it does not own, so a
  // state the walk just changed is a state the pane has to be told about.
  await page.evaluate(() => loadStartRecent({ force: true }));
  await page.evaluate(() => refreshOnboarding(true));
  await sleep(500);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const runtime = await startStubRuntime();
  const fixture = args.baseUrl
    ? { ready: { base_url: args.baseUrl, token: args.token }, child: null }
    : await startFixture(runtime.url);
  const baseUrl = fixture.ready.base_url;
  const token = fixture.ready.token;
  const root = fixture.ready.root || mkdtempSync(join(tmpdir(), "start-command-center-root-"));

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"],
  });

  const runs = [];
  const failures = [];
  const only = args.only ? args.only.split(",").map((id) => id.trim()) : null;
  try {
    for (const theme of THEMES) {
      for (const viewport of VIEWPORTS) {
        if (only && !only.includes(viewport.id)) continue;
        const run = { theme, viewport: viewport.id, checks: [], shots: [] };
        const record = (step, passed, detail) => {
          run.checks.push({ step, passed: Boolean(passed), detail: detail || "", status: passed ? "pass" : "fail" });
          if (!passed) failures.push(`${theme}/${viewport.id}: ${step} — ${detail}`);
        };
        const page = await browser.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(String(error)));
        const slow = { delay: 0, abort: false };
        await page.setRequestInterception(true);
        page.on("request", async (request) => {
          if (request.url().includes("/api/start/command-center") && slow.abort) return request.abort("failed");
          if (request.url().includes("/api/start/command-center") && slow.delay) {
            await sleep(slow.delay);
          }
          return request.continue();
        });
        await page.setViewport({ width: viewport.width, height: viewport.height, deviceScaleFactor: 1 });
        await page.evaluateOnNewDocument((key, value, themeName) => {
          try {
            window.sessionStorage.setItem(key, value);
            window.localStorage.setItem("m5_theme", themeName);
          } catch (_error) { /* storage may be refused */ }
        }, "m5_api_token", token, theme);
        const shoot = async (state) => {
          const dir = resolve(args.out, theme);
          mkdirSync(dir, { recursive: true });
          const name = `${state}-${viewport.id}.png`;
          // Every scroller is reset, not just the window: the pane scrolls inside its
          // own shell, and a capture of "the landing state" taken 600px down is a
          // capture of a different state.
          await page.evaluate(() => {
            if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
            for (let node = document.getElementById("pane-start"); node; node = node.parentElement) {
              node.scrollTop = 0;
            }
          });
          await sleep(120);
          await page.screenshot({ path: join(dir, name) });
          run.shots.push(`${theme}/${name}`);
        };

        const goto = async () => {
          await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
          await sleep(args.settle * 4);
          await page.evaluate(() => switchTab("start"));
          await sleep(args.settle * 4);
        };

        // ---- 1. empty: nothing configured, no recent work ---------------------
        await goto();
        await clearSessions(page);
        await page.evaluate(async () => {
          const payload = await (await fetch("/api/providers")).json();
          for (const provider of payload.providers || []) {
            await fetch(`/api/providers/${encodeURIComponent(provider.alias)}`, { method: "DELETE" });
          }
        });
        await page.evaluate(() => loadStartRecent({ force: true }));
        await page.evaluate(() => refreshOnboarding());
        await sleep(args.settle * 5);
        let pane = await readPane(page);
        record("the composer is the first card", pane.firstCard === "startLaunchCard", String(pane.firstCard));
        record(
          "the composer is above the fold",
          Boolean(pane.composer && pane.button) && pane.composer.bottom <= pane.viewport.height && pane.button.bottom <= pane.viewport.height,
          `input ends ${pane.composer ? pane.composer.bottom : "?"} of ${pane.viewport.height}`,
        );
        record("the details start closed", pane.details.every((item) => item.tag === "DETAILS" && item.open === false), JSON.stringify(pane.details));
        record("the verdict line answers before the composer", Boolean(pane.line && pane.composer) && pane.line.top < pane.composer.top);
        record("an empty install says what is needed", /Needs you|Blocked|disagree/i.test(pane.lineText), pane.lineText);
        record("recent work presents its empty state", pane.recentEmpty === true, `rows=${pane.recentRows}`);
        record("no row scrolls sideways", pane.docOverflow === false, `docOverflow=${pane.docOverflow}`);
        await shoot("empty");

        // ---- 2. populated: sessions, a workspace, a probed runtime ------------
        await seedSessions(page, ["Audit the sandbox boundary rows", "Wire the recents picker", "Re-run the failure-injection suite"]);
        const workspace = await setWorkspace(page, join(root, "projects/uiux-2-4"));
        record("the workspace is set through the app's own saver", workspace.status === 200, `HTTP ${workspace.status}`);
        await openStart(page);
        await sleep(args.settle * 5);
        pane = await readPane(page);
        record("populated recent work is listed", pane.recentRows >= 3 && pane.recentEmpty === false, `rows=${pane.recentRows}`);
        record(
          "the verdict prints the basis as well as the verdict",
          /(runtime answered|runtime did not answer|runtime not asked)/.test(pane.lineText),
          pane.lineText,
        );
        record(
          "the checklist summary carries the same verdict the line prints",
          pane.summaryStates.checklist.toLowerCase() === pane.pill.toLowerCase(),
          `${pane.summaryStates.checklist} vs ${pane.pill}`,
        );
        record("the pane still has no open disclosure while acting", pane.details.every((item) => item.open === false));
        await shoot("populated");

        // ---- 3. long content ---------------------------------------------------
        await seedSessions(page, [LONG_TITLE, `  ${LONG_TITLE} (second pass)`]);
        await setWorkspace(page, join(root, LONG_WORKSPACE));
        await openStart(page);
        await page.evaluate((text) => {
          const input = document.getElementById("startTaskInput");
          input.value = text;
          input.dispatchEvent(new Event("input"));
        }, LONG_TASK);
        await sleep(args.settle * 5);
        pane = await readPane(page);
        record("long sessions are clipped rather than overflowing", pane.longTitleClipped === true, `clipped=${pane.longTitleClipped}`);
        record("long content does not scroll the pane sideways", pane.docOverflow === false, `docOverflow=${pane.docOverflow}`);
        record("the composer keeps the long draft", pane.draft === LONG_TASK, `${pane.draft.length} chars`);
        record(
          "the long draft stays above the fold with its button",
          Boolean(pane.composer && pane.button) && pane.button.bottom <= pane.viewport.height,
          `button ends ${pane.button ? pane.button.bottom : "?"}`,
        );
        await shoot("long-content");

        // ---- 4. loading: the read is in flight ---------------------------------
        slow.delay = 1400;
        await Promise.all([
          page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60000 }),
          sleep(args.settle * 2),
        ]);
        await page.evaluate(() => switchTab("start"));
        await sleep(320);
        const loading = await page.evaluate(() => ({
          pill: (document.getElementById("startReadinessPill") || {}).textContent || "",
          line: (document.getElementById("startReadinessLine") || {}).textContent || "",
        }));
        record("the loading state says it is reading", /reading/i.test(loading.line) || /checking/i.test(loading.pill), `${loading.pill} · ${loading.line}`);
        await shoot("loading");
        slow.delay = 0;
        await sleep(args.settle * 6);

        // ---- 5. error: the read is refused -------------------------------------
        slow.abort = true;
        await page.evaluate(() => refreshOnboarding());
        await sleep(args.settle * 6);
        const failed = await readPane(page);
        record("a refused read says so in the verdict line", /could not read/i.test(failed.lineText), failed.lineText.slice(0, 160));
        record("a refused read does not keep a stale verdict beside it", !/Ready to run|Needs you/i.test(failed.lineText), failed.lineText.slice(0, 120));
        record(
          "the retry is above the fold rather than inside a closed disclosure",
          failed.blockerHidden === false
            && failed.blockerActions.includes("Re-check")
            && Boolean(failed.blockerBox) && failed.blockerBox.bottom <= failed.viewport.height,
          `actions=${failed.blockerActions.join("/")} bottom=${failed.blockerBox ? failed.blockerBox.bottom : "?"}`,
        );
        await page.evaluate(() => {
          document.getElementById("startDiagnostics").open = true;
        });
        await sleep(args.settle * 2);
        const errorPane = await readPane(page);
        record("the error is presented where the checks live", errorPane.presentations.includes("error"), JSON.stringify(errorPane.presentations));
        await shoot("error");

        // ---- 6. recovery -------------------------------------------------------
        slow.abort = false;
        const pressed = await page.evaluate(() => {
          // Pressed from the blocker, at the top of the card: that is where a reader who
          // just saw a failure is looking, and it is the affordance the story moved there.
          const button = document.getElementById("startRetryReadBtn");
          if (!button) return false;
          button.focus();
          return document.activeElement === button;
        });
        if (pressed) {
          await page.keyboard.press("Enter");
        }
        await sleep(args.settle * 8);
        const recovered = await readPane(page);
        const focus = await page.evaluate(() => ({
          id: document.activeElement ? document.activeElement.id || document.activeElement.tagName : null,
          inPane: document.getElementById("pane-start").contains(document.activeElement),
          isBody: document.activeElement === document.body,
        }));
        record("recovery clears the error", recovered.presentations.includes("error") === false, JSON.stringify(recovered.presentations));
        record("recovery restores the verdict line", /Ready to run|Needs you|Blocked|disagree/i.test(recovered.lineText), recovered.lineText.slice(0, 140));
        record(
          "recovery leaves focus inside the pane rather than on <body>",
          focus.isBody === false && focus.inPane === true,
          String(focus.id),
        );
        record(
          "recovery puts focus where the restored verdict is printed",
          focus.id === "startReadinessLine",
          String(focus.id),
        );
        await page.evaluate(() => {
          document.getElementById("startDiagnostics").open = false;
        });
        await sleep(args.settle * 2);
        await shoot("recovery");

        // ---- 7. contradiction: the port answers, the runtime has no models -----
        runtime.empty();
        await openStart(page);
        await sleep(args.settle * 8);
        const contradicted = await readPane(page);
        record(
          "a port with no models is printed as a contradiction",
          contradicted.contradictions.length >= 1
            && contradicted.contradictions.some((row) => row.id === "port-without-models"),
          contradicted.contradictions.map((row) => row.id).join(","),
        );
        record("the contradiction carries its evidence", contradicted.contradictions.every((row) => row.evidence.length > 0), contradicted.contradictions.map((row) => row.evidence).join(" | "));
        record("the contradiction offers a way to act", contradicted.contradictions.every((row) => row.actions.length > 0), contradicted.contradictions.map((row) => row.actions.join("/")).join(" | "));
        record("the verdict is not ready beside a contradiction", !/^Ready/i.test(contradicted.pill), contradicted.pill);
        record("the pane does not scroll sideways with the contradiction", contradicted.docOverflow === false, `docOverflow=${contradicted.docOverflow}`);
        await shoot("contradiction");
        runtime.restore();

        record("no page errors during the run", errors.length === 0, errors.join("; "));
        await page.close();
        runs.push(run);
      }
    }
  } finally {
    await browser.close();
    runtime.close();
    if (fixture.child) fixture.child.kill("SIGTERM");
  }

  const checks = runs.flatMap((run) => run.checks);
  const shots = runs.flatMap((run) => run.shots);
  if (args.json) {
    console.log(JSON.stringify({ runs, checks: checks.length, passed: checks.filter((row) => row.passed).length, shots: shots.length, failures }, null, 2));
  } else {
    for (const run of runs) {
      const failed = run.checks.filter((row) => !row.passed);
      console.log(`${run.theme}/${run.viewport}: ${run.checks.length - failed.length}/${run.checks.length} checks, ${run.shots.length} captures`);
    }
    console.log(`\n${shots.length} captures across ${runs.length} runs`);
  }
  if (failures.length) throw new Error(`${failures.length} check(s) failed:\n${failures.join("\n")}`);
}

const args = parseArgs(process.argv.slice(2));
run(args).catch((error) => {
  console.error(`start_command_center_walk: ${error.message}`);
  process.exit(1);
});
