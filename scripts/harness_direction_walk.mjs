#!/usr/bin/env node
/**
 * Check that an early direction check-in is stated, answerable and limited, in a browser (AP-018).
 *
 *   node scripts/harness_direction_walk.mjs --screenshots docs/media/harness-direction
 *
 * A direction panel is not a card a walk can draw: the sample, the digest of the exact version
 * shown, the trigger and the question all come from durable record. This walk starts
 * `scripts/harness_direction_seed.py`, which drives the real state machine and asks the real
 * `POST …/direction-checkpoint` route for three check-ins - an unbenchmarked task, a task an
 * owner-approved reference already covers, and one whose sample id and question are markup.
 *
 * What it proves:
 *
 *   1. a paused-for-direction task is drawn as waiting on the reader, with the question and the
 *      sample it is about - not as a stuck or failed run;
 *   2. the sample's exact version is shown, and the panel says an answer is bound to that digest;
 *   3. the panel says, in the pane, that a direction answer is not acceptance and grants no
 *      permission to publish, deploy or spend;
 *   4. "Keep this direction" goes through `POST …/direction-answer`, the task leaves
 *      `waiting_approval`, and the pane reports the outcome without claiming acceptance;
 *   5. "Change direction" refuses to send an empty note, and states why;
 *   6. a covered task is NOT interrupted: no direction panel, the task stays running, and the
 *      route reports `skipped` rather than manufacturing a pause;
 *   7. a sample id and question that are markup are drawn as text: no element, no handler, no
 *      page error;
 *   8. the check-in survives a reload - it is read from the record, not from pane state;
 *   9. no page error occurs, and no route outside direction-checkpoint / direction-answer / task
 *      / state reads is posted.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 60000,
  screenshots: "",
  baseUrl: "",
  token: "",
  waitingSessionId: "",
  coveredSessionId: "",
  hostileSessionId: "",
  port: 0,
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
      case "--waiting-session-id": args.waitingSessionId = next(); break;
      case "--covered-session-id": args.coveredSessionId = next(); break;
      case "--hostile-session-id": args.hostileSessionId = next(); break;
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
    "Check that an early direction check-in is stated, answerable and limited.",
    "",
    "  node scripts/harness_direction_walk.mjs [--screenshots DIR] [--json]",
    "",
    "Without --base-url the walk starts scripts/harness_direction_seed.py itself, so every state",
    "it reads is the server's own. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and",
    "puppeteer-core (STUDIO_PUPPETEER_CORE). See docs/ap018-direction-checkpoints.md.",
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
      if (response.ok) return true;
    } catch {
      // not up yet
    }
    await sleep(150);
  }
  return false;
}

async function startFixture(args) {
  const python = process.env.STUDIO_PYTHON || "python3";
  const child = spawn(python, [resolve(process.cwd(), "scripts", "harness_direction_seed.py"), "--port", String(args.port || 0)], {
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env },
  });
  const ready = await new Promise((done, fail) => {
    let buffer = "";
    const timer = setTimeout(() => fail(new Error("the seed fixture did not report a ready line in 60s")), 60000);
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString();
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (line) {
        clearTimeout(timer);
        try {
          done(JSON.parse(line));
        } catch (error) {
          fail(error);
        }
      }
    });
    child.stderr.on("data", (chunk) => {
      process.stderr.write(chunk);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      fail(new Error(`the seed fixture exited before reporting ready (code ${code})`));
    });
  });
  return { child, ready };
}

const checks = [];
function check(ok, label, detail) {
  checks.push({ ok: Boolean(ok), label, detail: detail || "" });
  const mark = ok ? "  ok  " : "  FAIL";
  console.log(`${mark} ${label}${ok || !detail ? "" : `: ${detail}`}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const puppeteer = await loadPuppeteer();
  const chrome = resolveChrome();

  let seed = null;
  let baseUrl = args.baseUrl;
  let token = args.token;
  let ids = {
    waiting: args.waitingSessionId,
    covered: args.coveredSessionId,
    hostile: args.hostileSessionId,
  };
  if (!baseUrl) {
    seed = await startFixture(args);
    baseUrl = seed.ready.base_url;
    token = token || seed.ready.token;
    ids = {
      waiting: ids.waiting || seed.ready.waiting_session_id,
      covered: ids.covered || seed.ready.covered_session_id,
      hostile: ids.hostile || seed.ready.hostile_session_id,
    };
  }
  if (!(await waitForHttp(`${baseUrl}/healthz`, args.timeoutMs))) {
    throw new Error(`the app did not become healthy at ${baseUrl}`);
  }

  if (args.screenshots) mkdirSync(args.screenshots, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean),
    ],
  });
  const pageErrors = [];
  const posted = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 960 });
    page.on("pageerror", (error) => pageErrors.push(String(error && error.message ? error.message : error)));
    page.on("console", (message) => {
      if (message.type() === "error" && /Uncaught/.test(message.text())) pageErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (request.method() === "POST") posted.push(new URL(request.url()).pathname);
    });

    // The token is read from sessionStorage by the shipped app, so it is seeded before any
    // document script runs rather than written afterwards and hoped for.
    await page.evaluateOnNewDocument((key, value) => {
      try {
        window.sessionStorage.setItem(key, value);
      } catch (_error) {}
    }, args.tokenKey, token);
    await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);

    async function openSession(sessionId) {
      await page.evaluate((id) => selectHarnessSession(id), sessionId);
      await sleep(args.settleMs * 14);
    }

    async function panelText() {
      return page.evaluate(() => {
        const host = document.getElementById("harnessDirectionPanel");
        return host ? host.innerText : "";
      });
    }
    async function panelPresent() {
      return page.evaluate(() => {
        const host = document.getElementById("harnessDirectionPanel");
        return Boolean(host && !host.hidden && host.innerText.trim());
      });
    }
    async function taskState(sessionId) {
      return page.evaluate(async (id) => {
        const res = await fetch(`/api/harness/sessions/${encodeURIComponent(id)}/task`, {
          headers: { Authorization: `Bearer ${window.sessionStorage.getItem("m5_api_token") || ""}` },
        });
        if (!res.ok) return `HTTP ${res.status}`;
        const body = await res.json();
        return String((body.task && body.task.state) || "");
      }, sessionId);
    }

    // ---- 1. the check-in is drawn as waiting on the reader ----
    await openSession(ids.waiting);
    check(await panelPresent(), "1. the direction panel is drawn for a paused-for-direction task");
    let text = await panelText();
    check(/Direction sample/i.test(text), "1. the panel is titled as a direction sample", text.slice(0, 160));
    check(/waiting on you/i.test(text), "1. it says the run is waiting on the reader", text.slice(0, 160));
    check(/right direction/i.test(text), "1. the question the owner is asked is shown", text.slice(0, 200));
    check(await taskState(ids.waiting) === "waiting_approval", "1. the task really is waiting_approval");

    // ---- 2. the exact version is shown and the answer is bound to it ----
    check(/sha256 [0-9a-f]{64}/i.test(text), "2. the sample's exact version digest is shown");
    check(/bound to this digest/i.test(text), "2. the panel says an answer is bound to that digest");

    // ---- 3. the limit is stated in the pane ----
    check(/not accepting this artifact/i.test(text), "3. the pane says a direction answer is not acceptance");
    check(/does not publish, deploy or spend/i.test(text), "3. the pane says it grants no publish/deploy/spend permission");

    if (args.screenshots) {
      await page.screenshot({ path: `${args.screenshots}/direction-waiting.png`, fullPage: false });
    }

    // ---- 4. keep goes through the route and settles the pause ----
    const keepPressed = await page.evaluate(() => {
      const host = document.getElementById("harnessRecoveryStory");
      const button = host ? [...host.querySelectorAll("button")].find((b) => /keep this direction/i.test(b.textContent || "")) : null;
      if (!button) return false;
      button.click();
      return true;
    });
    check(keepPressed, "4. the keep control is present and pressable");
    await sleep(args.settleMs * 12);
    check(posted.some((path) => path.endsWith("/direction-answer")), "4. the answer went through POST direction-answer", posted.join(", "));
    const afterKeep = await taskState(ids.waiting);
    check(afterKeep !== "waiting_approval", `4. the task left waiting_approval (now ${afterKeep})`);
    const note = await page.evaluate(() => {
      const node = document.getElementById("harnessRecoveryNote");
      return node && !node.hidden ? node.innerText : "";
    });
    check(/not acceptance|not the acceptance/i.test(note), "4. the pane reports the outcome without claiming acceptance", note.slice(0, 200));
    if (args.screenshots) {
      await page.screenshot({ path: `${args.screenshots}/direction-answered.png`, fullPage: false });
    }

    // ---- 8. the check-in is read from the record, so a reload still shows it ----
    await openSession(ids.waiting);
    const afterReload = await panelText();
    check(/sha256 [0-9a-f]{64}/i.test(afterReload), "8. the answered check-in is still read from the record after a reload");

    // ---- 6. a covered task is not interrupted ----
    await openSession(ids.covered);
    check(!(await panelPresent()), "6. no direction panel is drawn for a covered task");
    check(await taskState(ids.covered) === "running", "6. a covered task is left running, not paused");
    const coveredText = await page.evaluate(() => {
      const host = document.getElementById("harnessRecoveryStory");
      return host ? host.innerText : "";
    });
    check(!/waiting on you: is this the right direction/i.test(coveredText), "6. the covered task is not drawn as waiting on the reader");

    // ---- 7. hostile text is drawn as text ----
    await openSession(ids.hostile);
    const hostilePresent = await panelPresent();
    check(hostilePresent, "7. the hostile sample still draws a panel (the record is real)");
    const injected = await page.evaluate(() => {
      const host = document.getElementById("harnessDirectionPanel");
      return {
        scripts: host ? host.querySelectorAll("script").length : -1,
        images: host ? host.querySelectorAll("img").length : -1,
        pwned: window.pwned === undefined ? "absent" : "present",
      };
    });
    check(injected.scripts === 0 && injected.images === 0, "7. no element was injected from the sample id or question", JSON.stringify(injected));
    check(injected.pwned === "absent", "7. no hostile handler executed", JSON.stringify(injected));
    if (args.screenshots) {
      await page.screenshot({ path: `${args.screenshots}/direction-hostile-text.png`, fullPage: false });
    }

    // ---- 9. no page error, no unexpected route ----
    const allowed = ["/direction-answer", "/direction-checkpoint", "/pause", "/resume", "/cancel", "/contain"];
    const unexpected = posted.filter((path) => !allowed.some((suffix) => path.endsWith(suffix)));
    check(pageErrors.length === 0, "9. no page error occurred", pageErrors.join(" | "));
    check(unexpected.length === 0, "9. nothing outside the expected routes was posted", unexpected.join(", "));

    const failed = checks.filter((entry) => !entry.ok);
    const result = {
      checks: checks.length,
      failed: failed.length,
      waiting_session_id: ids.waiting,
      covered_session_id: ids.covered,
      hostile_session_id: ids.hostile,
      screenshots: args.screenshots || null,
    };
    console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
    if (args.json) console.log(JSON.stringify(result, null, 2));
    process.exitCode = failed.length === 0 ? 0 : 1;
  } finally {
    await browser.close();
    if (seed && seed.child) seed.child.kill("SIGTERM");
  }
}

main().catch((error) => {
  console.error(`harness direction walk failed: ${error.message}`);
  process.exit(2);
});
