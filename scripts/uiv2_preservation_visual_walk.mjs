#!/usr/bin/env node
/**
 * UIV2-27: exercise a real long Council conclusion at the approved visual matrix.
 *
 *   node scripts/uiv2_preservation_visual_walk.mjs --base-url URL --token TOKEN --out DIR
 *
 * Eight runs (four viewports x light/dark) use the existing session/close routes and real DOM.
 * Each run checks independent conclusion scrolling, the disclosure that restores transcript room,
 * sticky positioning where the transcript is the scroll container, keyboard resize, the shared
 * expand overlay and Escape focus restoration, narrow composer reachability, and page overflow.
 * PNGs and capture-manifest.json are local evidence; no application state is mocked or painted.
 */
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const VIEWPORTS = ["1640x900", "1440x900", "1280x800", "390x844"];
const THEMES = ["light", "dark"];
const LONG_REQUEST = Array.from({ length: 90 }, (_, index) =>
  `Transcript paragraph ${index + 1}: the complete archived discussion stays readable below the decision.`,
).join(" ");
const LONG_CONCLUSION = Array.from({ length: 80 }, (_, index) =>
  `Decision paragraph ${index + 1}: retain provenance, dissent, keyboard access, and recovery conditions.`,
).join(" ") + " Final decision marker: conclusion-scroll-end.";
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function parseArgs(argv) {
  const args = { baseUrl: "", token: "", out: "docs/media/uiv2-27-preservation/council-long", settleMs: 150 };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      if (argv[index + 1] === undefined) throw new Error(`${arg} requires a value`);
      return argv[++index];
    };
    if (arg === "--base-url") args.baseUrl = next();
    else if (arg === "--token") args.token = next();
    else if (arg === "--out") args.out = next();
    else if (arg === "--settle-ms") args.settleMs = Number(next());
    else if (arg === "--help" || arg === "-h") {
      console.log("node scripts/uiv2_preservation_visual_walk.mjs --base-url URL --token TOKEN [--out DIR]");
      process.exit(0);
    } else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!args.baseUrl || !args.token) throw new Error("--base-url and --token are required; use an authenticated throwaway demo app");
  return args;
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const module = await import(pathToFileURL(resolve(candidate)).href);
      return module.default || module;
    } catch (_) { /* try the next explicit installation */ }
  }
  throw new Error("puppeteer-core is not importable; set STUDIO_PUPPETEER_CORE");
}

function chrome() {
  const configured = process.env.STUDIO_SCREENSHOT_CHROME || "";
  if (configured && existsSync(configured)) return configured;
  throw new Error("STUDIO_SCREENSHOT_CHROME must name the pinned Chromium executable");
}

async function openHall(page, args, viewport, theme) {
  const [width, height] = viewport.split("x").map(Number);
  await page.setViewport({ width, height, deviceScaleFactor: 1 });
  await page.evaluateOnNewDocument((token, themeName) => {
    sessionStorage.setItem("m5_api_token", token);
    localStorage.setItem("m5_theme", themeName);
  }, args.token, theme);
  await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await sleep(args.settleMs * 8);
  await page.evaluate(() => switchTab("hall"));
  await sleep(args.settleMs * 6);
  await page.evaluate(async () => {
    await fetch("/api/hall/sessions", { method: "DELETE" });
    await refreshHallSessions();
  });
}

async function createLongSession(page) {
  return page.evaluate(async ({ requestText, conclusionText }) => {
    const createdResponse = await fetch("/api/hall/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: requestText, title: "UIV2-27 long pinned conclusion" }),
    });
    const created = await createdResponse.json();
    if (!createdResponse.ok) throw new Error(`create failed (${createdResponse.status})`);
    const closeResponse = await fetch(`/api/hall/sessions/${encodeURIComponent(created.id)}/close`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conclusion: conclusionText, author: "Lead Arbiter" }),
    });
    if (!closeResponse.ok) throw new Error(`close failed (${closeResponse.status})`);
    await selectHallSession(created.id);
    return created.id;
  }, { requestText: LONG_REQUEST, conclusionText: LONG_CONCLUSION });
}

async function main(args) {
  const puppeteer = await loadPuppeteer();
  const browser = await puppeteer.launch({
    executablePath: chrome(), headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean)],
  });
  const out = resolve(args.out);
  await mkdir(out, { recursive: true });
  const checks = [];
  const captures = [];
  const record = (viewport, theme, step, passed, detail = "") => {
    checks.push({ viewport, theme, step, passed, detail });
    console.log(`${passed ? "pass" : "FAIL"}  ${viewport} ${theme}: ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const shoot = async (page, viewport, theme, name) => {
    const path = resolve(out, `${name}-${viewport}-${theme}.png`);
    await page.screenshot({ path, fullPage: false });
    const digest = createHash("sha256").update(await readFile(path)).digest("hex");
    captures.push({ viewport, theme, state: name, path, sha256: digest });
  };

  try {
    for (const viewport of VIEWPORTS) {
      const width = Number(viewport.split("x")[0]);
      for (const theme of THEMES) {
        const page = await browser.newPage();
        const errors = [];
        page.on("pageerror", (error) => errors.push(String(error)));
        try {
          await openHall(page, args, viewport, theme);
          const sessionId = await createLongSession(page);
          await sleep(args.settleMs * 3);
          const initial = await page.evaluate(() => ({
            status: document.getElementById("hallStatusBadge").textContent.trim(),
            scrollable: document.getElementById("hallConclusionBody").scrollHeight
              > document.getElementById("hallConclusionBody").clientHeight + 20,
            overflow: document.documentElement.scrollWidth - innerWidth,
          }));
          record(viewport, theme, "the route-created long conclusion is closed, scrollable, and overflow-free",
            Boolean(sessionId) && /closed/i.test(initial.status) && initial.scrollable && initial.overflow <= 0,
            `status=${initial.status} overflow=${initial.overflow}px`);

          await page.evaluate(() => document.querySelector("#hallSeatsSettings > summary").focus());
          await page.keyboard.press("Enter");
          await sleep(args.settleMs * 3);
          const room = await page.evaluate(() => ({
            open: document.getElementById("hallSeatsSettings").open,
            streamHeight: document.getElementById("hallStream").clientHeight,
            bannerHeight: document.getElementById("hallConclusionBanner").getBoundingClientRect().height,
            streamScrollable: document.getElementById("hallStream").scrollHeight
              > document.getElementById("hallStream").clientHeight + 20,
          }));
          const desktopSticky = width >= 1280;
          record(viewport, theme, "the roster disclosure closes from the keyboard and preserves reading room",
            !room.open && (!desktopSticky || (room.streamScrollable && room.streamHeight >= room.bannerHeight + 30)),
            `stream=${Math.round(room.streamHeight)}px banner=${Math.round(room.bannerHeight)}px`);

          const before = await page.evaluate(() => {
            const body = document.getElementById("hallConclusionBody");
            const stream = document.getElementById("hallStream");
            body.scrollTop = 0;
            body.focus({ preventScroll: true });
            return { body: body.scrollTop, stream: stream.scrollTop, page: scrollY, focused: document.activeElement === body };
          });
          await page.keyboard.press("PageDown");
          await sleep(args.settleMs * 2);
          const after = await page.evaluate(() => ({
            body: document.getElementById("hallConclusionBody").scrollTop,
            stream: document.getElementById("hallStream").scrollTop,
            page: scrollY,
            focused: document.activeElement === document.getElementById("hallConclusionBody"),
          }));
          record(viewport, theme, "PageDown scrolls only the focused conclusion, not the transcript or page",
            before.focused && after.focused && after.body > before.body
              && after.stream === before.stream && after.page === before.page,
            `body=${Math.round(after.body)}px stream=${Math.round(after.stream)}px page=${Math.round(after.page)}px`);

          if (desktopSticky) {
            const sticky = await page.evaluate(() => {
              const stream = document.getElementById("hallStream");
              const banner = document.getElementById("hallConclusionBanner");
              stream.scrollTop = stream.scrollHeight;
              const streamRect = stream.getBoundingClientRect();
              const bannerRect = banner.getBoundingClientRect();
              return { top: stream.scrollTop, position: getComputedStyle(banner).position,
                delta: bannerRect.top - streamRect.top };
            });
            record(viewport, theme, "the pinned card stays at the transcript top at maximum scroll",
              sticky.top > 0 && sticky.position === "sticky" && sticky.delta >= -1 && sticky.delta <= 40,
              `scroll=${Math.round(sticky.top)}px delta=${Math.round(sticky.delta)}px`);
          } else {
            const narrow = await page.evaluate(() => {
              const banner = document.getElementById("hallConclusionBanner");
              banner.scrollIntoView({ block: "start" });
              const rect = banner.getBoundingClientRect();
              return { visible: rect.top >= -2 && rect.bottom <= innerHeight + 2,
                overflow: document.documentElement.scrollWidth - innerWidth };
            });
            record(viewport, theme, "the long pinned card is reachable without narrow-page overflow",
              narrow.visible && narrow.overflow <= 0, `visible=${narrow.visible} overflow=${narrow.overflow}px`);
          }
          await shoot(page, viewport, theme, "long-content");

          const resizeBefore = await page.evaluate(() => {
            const separator = document.getElementById("hallConclusionResize");
            separator.focus({ preventScroll: true });
            return parseFloat(getComputedStyle(document.getElementById("hallConclusionBody")).maxHeight);
          });
          await page.keyboard.press("ArrowDown");
          await sleep(args.settleMs);
          const resizeAfter = await page.evaluate(() => ({
            height: parseFloat(getComputedStyle(document.getElementById("hallConclusionBody")).maxHeight),
            focused: document.activeElement === document.getElementById("hallConclusionResize"),
          }));
          record(viewport, theme, "the separator resizes from the keyboard and retains focus",
            resizeAfter.focused && resizeAfter.height > resizeBefore,
            `${Math.round(resizeBefore)}px -> ${Math.round(resizeAfter.height)}px`);

          await page.evaluate(() => document.querySelector('[data-conclusion-action="expand"]').focus());
          await page.keyboard.press("Enter");
          await sleep(args.settleMs * 2);
          const expanded = await page.evaluate(() => ({
            open: document.getElementById("hallConclusionModal").classList.contains("open"),
            full: document.getElementById("hallConclusionModalBody").textContent.includes("conclusion-scroll-end"),
            focus: document.getElementById("hallConclusionModal").contains(document.activeElement),
            overflow: document.documentElement.scrollWidth - innerWidth,
          }));
          record(viewport, theme, "Expand presents the complete conclusion in the shared overlay with focus inside",
            expanded.open && expanded.full && expanded.focus && expanded.overflow <= 0,
            `open=${expanded.open} full=${expanded.full} overflow=${expanded.overflow}px`);
          await shoot(page, viewport, theme, "conclusion-modal");
          await page.keyboard.press("Escape");
          await sleep(args.settleMs * 2);
          const restored = await page.evaluate(() => ({
            open: document.getElementById("hallConclusionModal").classList.contains("open"),
            action: document.activeElement && document.activeElement.dataset
              ? document.activeElement.dataset.conclusionAction || "" : "",
          }));
          record(viewport, theme, "Escape closes the overlay and restores the Expand trigger",
            !restored.open && restored.action === "expand", `returned=${restored.action}`);

          const reachable = await page.evaluate(() => {
            const composer = document.getElementById("hallHumanInput");
            composer.scrollIntoView({ block: "center" });
            const rect = composer.getBoundingClientRect();
            return { visible: rect.top >= -2 && rect.bottom <= innerHeight + 2,
              overflow: document.documentElement.scrollWidth - innerWidth };
          });
          record(viewport, theme, "the composer remains reachable after long-content and modal recovery",
            reachable.visible && reachable.overflow <= 0,
            `visible=${reachable.visible} overflow=${reachable.overflow}px`);
          record(viewport, theme, "no uncaught page error occurred", errors.length === 0, errors.slice(0, 2).join(" | "));
        } finally {
          await page.close();
        }
      }
    }
  } finally {
    await browser.close();
  }

  const failed = checks.filter((check) => !check.passed);
  const sourceSha256 = {};
  for (const path of ["static/index.html", "scripts/uiv2_preservation_visual_walk.mjs"]) {
    sourceSha256[path] = createHash("sha256").update(await readFile(resolve(path))).digest("hex");
  }
  const manifest = {
    story: "UIV2-27",
    git: {
      head: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
      dirty: Boolean(execFileSync("git", ["status", "--porcelain"], { encoding: "utf8" }).trim()),
    },
    source_sha256: sourceSha256,
    generated_at: new Date().toISOString(),
    totals: { checks: checks.length, passed: checks.length - failed.length, failed: failed.length,
      captures: captures.length, variants: VIEWPORTS.length * THEMES.length },
    checks, captures,
  };
  await writeFile(resolve(out, "capture-manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log(`${manifest.totals.passed}/${checks.length} checks · ${captures.length} screenshots · ${manifest.totals.variants} variants`);
  return failed.length || captures.length !== VIEWPORTS.length * THEMES.length * 2 ? 1 : 0;
}

main(parseArgs(process.argv.slice(2))).then((code) => { process.exitCode = code; })
  .catch((error) => { console.error(error.stack || error.message); process.exitCode = 1; });
