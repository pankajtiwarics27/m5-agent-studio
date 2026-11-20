#!/usr/bin/env node
/**
 * AP-010 governed browser worker bridge (real pinned Chromium).
 *
 * This is the *driver* side of browser_worker.py: the Python contract decides
 * what is allowed, this script performs exactly the admitted work and reports
 * facts back. It never widens a policy and never retries around a refusal.
 *
 * Prerequisites (same pinned tooling as the existing browser regressions, no
 * external CDN involved):
 *   npm ci --prefix tests/browser --ignore-scripts
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chromium
 *   STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/puppeteer/puppeteer-core.js
 *
 * Usage:
 *   node scripts/browser_worker_bridge.mjs --plan plan.json --out result.json
 *
 * The plan is a JSON object:
 *   {"tasks": [{"name": "s", "steps": [
 *       {"kind": "navigate", "url": "http://127.0.0.1:PORT/"},
 *       {"kind": "click", "selector": "#go"},
 *       {"kind": "type", "selector": "#q", "text": "hello"},
 *       {"kind": "screenshot"}]}]}
 *
 * The result records, per task: navigation URLs actually visited, popups that
 * were refused, whether the profile directory was isolated, and per-step
 * outcomes. Any policy error fails that step only; the script exits non-zero if
 * a step could not be executed at all, so a caller cannot mistake a failed run
 * for a successful one.
 */
import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function fail(message) {
  process.stderr.write(`browser_worker_bridge: ${message}\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const out = { plan: null, out: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--plan") out.plan = argv[++index];
    else if (arg === "--out") out.out = argv[++index];
    else fail(`unknown argument ${arg}`);
  }
  if (!out.plan) fail("--plan is required");
  return out;
}

const args = parseArgs(process.argv.slice(2));
const chrome = process.env.STUDIO_SCREENSHOT_CHROME;
const puppeteerCore = process.env.STUDIO_PUPPETEER_CORE;
if (!chrome) fail("STUDIO_SCREENSHOT_CHROME is not set");
if (!puppeteerCore) fail("STUDIO_PUPPETEER_CORE is not set");
if (!existsSync(chrome)) fail(`chrome binary not found: ${chrome}`);
if (!existsSync(puppeteerCore)) fail(`puppeteer-core not found: ${puppeteerCore}`);

const puppeteer = (await import(pathToFileURL(puppeteerCore).href)).default;

const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "")
  .split(/\s+/)
  .filter(Boolean);

const plan = JSON.parse(await readFile(args.plan, "utf8"));
const tasks = Array.isArray(plan.tasks) ? plan.tasks : [];
if (tasks.length === 0) fail("plan has no tasks");

// A dedicated profile per run, in the OS temp dir the caller controls. It is
// never a real user profile and it is removed before the process exits.
const profileDir = await mkdtemp(join(tmpdir(), "ap010-profile-"));

let browser = null;
const results = [];

const hardFailures = [];

try {
  browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: [
      ...extraArgs,
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      // No extension, no sync, no default browser surfaces.
      "--disable-extensions",
      "--no-first-run",
      "--no-default-browser-check",
      `--user-data-dir=${profileDir}`,
    ],
    env: process.env,
  });

  for (const task of tasks) {
    const page = await browser.newPage();
    const visited = [];
    const popups = [];
    const failures = [];
    let executed = 0;
    let screenshots = 0;
    const shotBytes = [];

    // Popups are recorded and closed. The close is tracked as a promise so the
    // report is only built once every refused surface is actually gone — a
    // snapshot taken mid-close would claim a window that still exists.
    const popupClosures = [];
    page.on("popup", (popup) => {
      popups.push({ url: "<pending>", refused: true });
      const slot = popups.length - 1;
      popupClosures.push(
        (async () => {
          try {
            popups[slot].url = safeUrl(popup.url());
          } catch {
            popups[slot].url = "<unavailable>";
          }
          try {
            await popup.close();
          } catch {
            /* already gone */
          }
        })()
      );
    });

    page.on("dialog", async (dialog) => {
      // A page must not block the worker behind a modal it controls.
      await dialog.dismiss().catch(() => {});
    });

    for (const step of task.steps || []) {
      try {
        if (step.kind === "navigate") {
          const response = await page.goto(step.url, {
            waitUntil: "domcontentloaded",
            timeout: 20000,
          });
          visited.push({ url: page.url(), status: response ? response.status() : null });
          executed += 1;
        } else if (step.kind === "click") {
          await page.click(step.selector);
          executed += 1;
        } else if (step.kind === "type") {
          await page.type(step.selector, step.text || "");
          executed += 1;
        } else if (step.kind === "screenshot") {
          const shot = await page.screenshot({ encoding: "base64" });
          screenshots += 1;
          executed += 1;
          shotBytes.push(Buffer.from(shot, "base64").length);
        } else if (step.kind === "wait") {
          await new Promise((r) => setTimeout(r, Number(step.ms) || 0));
          executed += 1;
        } else {
          failures.push(`unsupported step kind ${step.kind}`);
        }
      } catch (error) {
        failures.push(`${step.kind}: ${String(error && error.message ? error.message : error)}`);
      }
    }

    // A popup event is dispatched asynchronously, so a snapshot taken straight
    // after the click can miss the window entirely. Wait a short grace period,
    // then let every refused popup finish closing before reporting.
    await new Promise((r) => setTimeout(r, 300));
    if (popupClosures.length) {
      await Promise.allSettled(popupClosures);
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const remaining = (await browser.pages())
          .map((p) => safeUrl(p.url()))
          .filter((url) => url !== "about:blank");
        if (remaining.length <= 1) break;
        await new Promise((r) => setTimeout(r, 50));
      }
    }

    const title = await page.title().catch(() => "");
    results.push({
      task: task.name,
      title,
      visited,
      popups,
      failures,
      executed,
      screenshots,
      screenshotBytes: shotBytes,
      // Report the surfaces that actually survived, so a caller can prove a
      // popup did not become a live window. The initial about:blank tab that
      // puppeteer opens with the browser is not an agent surface.
      openSurfaces: (await browser.pages())
        .map((p) => safeUrl(p.url()))
        .filter((url) => url !== "about:blank"),
    });

    await page.close().catch(() => {});
  }
} catch (error) {
  hardFailures.push(String(error && error.stack ? error.stack : error));
} finally {
  if (browser) {
    await browser.close().catch(() => {});
  }
  await rm(profileDir, { recursive: true, force: true }).catch(() => {});
}

const payload = {
  ok: hardFailures.length === 0 && results.every((r) => r.failures.length === 0),
  hardFailures,
  profileIsolated: !existsSync(profileDir),
  chromium: chrome,
  results,
};

if (args.out) {
  await mkdir(dirname(resolve(args.out)), { recursive: true });
  await writeFile(args.out, `${JSON.stringify(payload, null, 2)}\n`);
}
process.stdout.write(`${JSON.stringify(payload)}\n`);

function safeUrl(raw) {
  try {
    return new URL(raw).href;
  } catch {
    return String(raw);
  }
}

process.exit(payload.ok ? 0 : 1);
