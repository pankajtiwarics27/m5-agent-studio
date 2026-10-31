#!/usr/bin/env node
/**
 * Walk the Studio shell with a keyboard only and report what happened (UIUX-P1-02).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/keyboard_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Why this exists: "keyboard operation works" is the acceptance criterion every P1
 * story inherits, and it is the one a reviewer cannot check by reading a diff. The
 * script drives real key events through a real browser and asserts the things a
 * keyboard user depends on:
 *
 *   - the rail is a tablist with one selected tab and one tabbable tab;
 *   - Tab from the top of the document reaches the selected tab, not six tabs in a row;
 *   - ArrowDown/ArrowRight move forward and ArrowUp/ArrowLeft back, wrapping at the ends;
 *   - Home and End jump to the first and last tab;
 *   - activation follows focus, and the visible pane matches the selected tab;
 *   - every advertised Alt+N shortcut (including Alt+6, which used to be dead) works;
 *   - focus is visibly indicated on the focused tab (a focus ring, not just a class).
 *
 * It changes nothing about the page: no DOM patching, no JS evaluation that fakes an
 * event. If a browser is missing it fails loudly rather than reporting a pass, because
 * a skipped keyboard walk that claims success is worse than no walk at all.
 *
 * Exit status is 0 only when every step passed; `--json` prints the results instead of
 * the summary so a story can record the exact output.
 */
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 45000,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS, baseUrl: "", token: "", json: false };
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
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
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
    "Walk the Studio shell with a keyboard only and report what happened.",
    "",
    "  node scripts/keyboard_walk.mjs --base-url URL --token TOKEN [--json]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-navigation-shell.md.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js"),
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
  const candidates = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This script refuses to report a keyboard walk it did not perform.",
  );
}

function demoUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("--base-url must be a loopback demo URL"); }
  if (!["http:", "https:"].includes(url.protocol) ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("--base-url must be an HTTP(S) loopback origin without a path, credentials, query, or fragment");
  }
  return url;
}

async function verifyAuth(url, token, timeoutMs) {
  const endpoint = new URL("/api/workspace/directory", url);
  const options = { redirect: "manual", signal: AbortSignal.timeout(timeoutMs) };
  const without = await fetch(endpoint, options);
  if (without.status !== 401) throw new Error("demo API did not require authentication");
  const withToken = await fetch(endpoint, {
    redirect: "manual", signal: AbortSignal.timeout(timeoutMs),
    headers: { Authorization: `Bearer ${token}` },
  });
  if (withToken.status !== 200 || !(withToken.headers.get("content-type") || "").includes("application/json")) {
    throw new Error("demo API did not accept the supplied token");
  }
  let data;
  try { data = await withToken.json(); } catch { throw new Error("demo API returned invalid JSON"); }
  if (!data || typeof data.workspace_dir !== "string" || !Array.isArray(data.recent_dirs)) {
    throw new Error("demo API returned an unexpected workspace response");
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function state(page) {
  return page.evaluate(() => {
    const rail = document.querySelector(".mode-tabs");
    const tabs = [...document.querySelectorAll(".mode-tab")];
    const selected = tabs.filter((tab) => tab.getAttribute("aria-selected") === "true");
    const tabbable = tabs.filter((tab) => tab.tabIndex === 0);
    const activePane = document.querySelector(".pane.active");
    const focused = document.activeElement;
    const style = focused ? getComputedStyle(focused) : null;
    return {
      role: rail ? rail.getAttribute("role") : null,
      orientation: rail ? rail.getAttribute("aria-orientation") : null,
      tabs: tabs.map((tab) => tab.dataset.tab),
      selected: selected.map((tab) => tab.dataset.tab),
      tabbable: tabbable.map((tab) => tab.dataset.tab),
      focusedTab: focused && focused.classList.contains("mode-tab") ? focused.dataset.tab : null,
      focusedLabel: focused ? (focused.getAttribute("aria-label") || focused.textContent.trim().slice(0, 24)) : null,
      activePane: activePane ? activePane.id : null,
      focusRing: style ? `${style.outlineStyle} ${style.outlineWidth}` : null,
      shortcuts: tabs.map((tab) => ({ tab: tab.dataset.tab, shortcut: tab.dataset.shortcut || null })),
    };
  });
}

async function run(args) {
  const url = demoUrl(args.baseUrl);
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(args.token)) throw new Error("--token must be a valid throwaway demo token");
  if (!Number.isSafeInteger(args.timeoutMs) || args.timeoutMs <= 0) throw new Error("--timeout-ms must be a positive integer");
  if (!Number.isFinite(args.settleMs) || args.settleMs < 0) throw new Error("--settle-ms must be non-negative");
  await verifyAuth(url, args.token, args.timeoutMs);
  const puppeteer = await loadPuppeteer();
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
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    let errors = 0;
    let unexpectedNavigation = false;
    let initialRequest = true;
    page.on("pageerror", () => { errors += 1; });
    page.on("dialog", (dialog) => { errors += 1; void dialog.dismiss(); });
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame() && frame.url() !== url.href) unexpectedNavigation = true;
    });
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
        const allowed = initialRequest && request.url() === url.href && request.redirectChain().length === 0;
        initialRequest = false;
        if (!allowed) {
          unexpectedNavigation = true;
          void request.abort();
          return;
        }
      }
      void request.continue();
    });
    // New-document scripts run in every frame, so check both frame and origin.
    await page.evaluateOnNewDocument((key, token, origin) => {
      if (window.top === window && location.origin === origin) {
        window.sessionStorage.setItem(key, token);
      }
    }, args.tokenKey, args.token, url.origin);
    const response = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    if (!response || response.status() !== 200 || response.request().redirectChain().length ||
        !(response.headers()["content-type"] || "").includes("text/html")) {
      throw new Error("demo document was not a direct successful HTML response");
    }
    const browserAuth = await page.evaluate(async () => {
      const response = await fetch("/api/workspace/directory", { redirect: "error", cache: "no-store" });
      if (response.status !== 200) return false;
      const data = await response.json();
      return typeof data.workspace_dir === "string" && Array.isArray(data.recent_dirs);
    });
    if (!browserAuth) throw new Error("browser could not authenticate to the demo API");
    await sleep(args.settleMs * 4);
    if (unexpectedNavigation || page.url() !== url.href) {
      throw new Error("unexpected main-frame navigation during keyboard walk");
    }

    const initial = await state(page);
    record("the rail is a tablist", initial.role === "tablist", `role=${initial.role}`);
    record("exactly one tab is selected", initial.selected.length === 1, initial.selected.join(","));
    record("exactly one tab is tabbable", initial.tabbable.length === 1, initial.tabbable.join(","));
    record(
      "the selected tab is the tabbable one",
      initial.tabbable.length === 1 && initial.tabbable[0] === initial.selected[0],
      `${initial.selected[0]} vs ${initial.tabbable[0]}`,
    );
    record(
      "the selected tab owns the visible pane",
      initial.activePane === `pane-${initial.selected[0]}`,
      `${initial.selected[0]} → ${initial.activePane}`,
    );
    record(
      "every tab advertises a shortcut",
      initial.shortcuts.every((entry) => /^Alt\+[1-6]$/.test(entry.shortcut || "")),
      initial.shortcuts.map((entry) => `${entry.tab}:${entry.shortcut}`).join(" "),
    );

    // Tab into the rail from the top of the document. The number of stops before it is
    // a layout fact, not a contract, so the walk tabs until the rail has focus and only
    // fails if it never gets there — what must hold is *which* tab the rail exposes.
    await page.evaluate(() => document.body.focus());
    let tabStops = 0;
    let inRail = null;
    let focusRing = null;
    for (let i = 0; i < 16; i += 1) {
      await page.keyboard.press("Tab");
      tabStops += 1;
      const now = await state(page);
      if (now.focusedTab) {
        inRail = now;
        focusRing = now.focusRing;
        break;
      }
    }
    record("Tab reaches the rail", inRail !== null, `after ${tabStops} stops`);
    record(
      "the rail's single tab stop is the selected tab",
      Boolean(inRail) && inRail.focusedTab === initial.selected[0],
      inRail ? `focus=${inRail.focusedTab}` : "the rail was never focused",
    );
    record("the focused tab shows a focus ring", /solid \d/.test(focusRing || ""), focusRing || "none");

    // Roving tabindex means Tab does not walk the other five tabs: the next stop leaves
    // the rail entirely. Without this the arrow keys would be a convenience, not a need.
    if (inRail) {
      await page.keyboard.press("Tab");
      const afterNextTab = await state(page);
      record(
        "Tab leaves the rail instead of walking six tabs",
        afterNextTab.focusedTab === null,
        `next focus=${afterNextTab.focusedLabel}`,
      );
      // Put focus back on the selected tab for the arrow-key part of the walk.
      await page.keyboard.down("Shift");
      await page.keyboard.press("Tab");
      await page.keyboard.up("Shift");
    }

    // Arrow navigation: one step forward, one back, then the ends.
    await page.keyboard.press("ArrowDown");
    await sleep(args.settleMs);
    const afterDown = await state(page);
    const order = initial.tabs;
    const expectedNext = order[(order.indexOf(initial.selected[0]) + 1) % order.length];
    record(
      "ArrowDown moves to the next tab and activates it",
      afterDown.selected[0] === expectedNext && afterDown.activePane === `pane-${expectedNext}`,
      `${initial.selected[0]} → ${afterDown.selected[0]} (${afterDown.activePane})`,
    );

    await page.keyboard.press("ArrowUp");
    await sleep(args.settleMs);
    const afterUp = await state(page);
    record(
      "ArrowUp returns to the previous tab",
      afterUp.selected[0] === initial.selected[0],
      `${afterDown.selected[0]} → ${afterUp.selected[0]}`,
    );

    await page.keyboard.press("End");
    await sleep(args.settleMs);
    const afterEnd = await state(page);
    record("End jumps to the last tab", afterEnd.selected[0] === order[order.length - 1], afterEnd.selected[0]);

    await page.keyboard.press("Home");
    await sleep(args.settleMs);
    const afterHome = await state(page);
    record("Home jumps to the first tab", afterHome.selected[0] === order[0], afterHome.selected[0]);

    await page.keyboard.press("ArrowLeft");
    await sleep(args.settleMs);
    const afterWrap = await state(page);
    record(
      "the rail wraps at the end",
      afterWrap.selected[0] === order[order.length - 1],
      `first + ArrowLeft → ${afterWrap.selected[0]}`,
    );

    // The advertised shortcuts, including Alt+6 for Start, which the old handler
    // dropped because it hard-coded keys 1-5.
    for (const entry of initial.shortcuts) {
      await page.keyboard.down("Alt");
      await page.keyboard.press(entry.shortcut.replace("Alt+", ""));
      await page.keyboard.up("Alt");
      await sleep(args.settleMs);
      const after = await state(page);
      record(
        `${entry.shortcut} opens ${entry.tab}`,
        after.selected[0] === entry.tab && after.activePane === `pane-${entry.tab}`,
        after.selected[0],
      );
    }

    record("no page errors during the walk", errors === 0, `${errors} page error(s) or dialog(s)`);
    if (unexpectedNavigation || page.url() !== url.href) {
      throw new Error("unexpected main-frame navigation during keyboard walk");
    }
  } finally {
    await browser.close();
  }

  const failed = results.filter((row) => !row.passed);
  if (args.json) {
    console.log(JSON.stringify({ results, passed: results.length - failed.length, failed: failed.length }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length} of ${results.length} checks passed`);
  }
  if (failed.length > 0) {
    throw new Error(`${failed.length} keyboard check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`keyboard_walk: ${error.message}`);
  process.exit(1);
});
