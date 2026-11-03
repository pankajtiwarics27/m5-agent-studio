#!/usr/bin/env node
/**
 * Capture the UIV2-03 reference wireframe fixtures (annotated three-region
 * Harness reference) at the roadmap's viewports and themes.
 *
 * Prerequisites, same as scripts/capture_screenshots.mjs and deliberately not
 * vendored: a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and
 * puppeteer-core (STUDIO_PUPPETEER_CORE). The wireframe is a static local file,
 * so no server or token is involved and nothing leaves the machine.
 *
 * Usage:
 *   STUDIO_SCREENSHOT_CHROME=/tmp/chromium \
 *   STUDIO_PUPPETEER_CORE=/scratch/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js \
 *   STUDIO_SCREENSHOT_CHROME_ARGS="--no-zygote --single-process --disable-gpu" \
 *     node scripts/uiux_v2_reference_capture.mjs \
 *       [--wireframe docs/media/uiv2-v2/reference/wireframe.html] \
 *       [--out docs/media/uiv2-v2/reference] [--sets 1640x900-light,1440x900-light,...]
 *
 * --sets is a comma list of WxH-theme; default covers the roadmap viewports:
 * 1640x900 and 1440x900 in light and dark, 1280x800 and 390x844 in light.
 * Each set directory gets one PNG per pane (harness, start, hall, council,
 * debate, chat) plus an index.json with sha256/bytes/viewport. A failed pane
 * aborts the whole set: partial fixture sets are never written.
 */
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const PANES = ["harness", "start", "hall", "council", "debate", "chat"];
// File names follow the published screenshot convention (pane aliases the review
// grid uses): the `council` tab is the Pipeline pane, `debate` is Compare.
const FILE_FOR = { council: "pipeline", debate: "compare" };
const DEFAULT_SETS = [
  "1640x900-light", "1640x900-dark",
  "1440x900-light", "1440x900-dark",
  "1280x800-light", "390x844-light",
];

function parseArgs(argv) {
  const args = { wireframe: "docs/media/uiv2-v2/reference/wireframe.html", out: "docs/media/uiv2-v2/reference", sets: DEFAULT_SETS, settleMs: 250 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => { const v = argv[i + 1]; if (v === undefined) throw new Error(`${arg} needs a value`); i += 1; return v; };
    if (arg === "--wireframe") args.wireframe = next();
    else if (arg === "--out") args.out = next();
    else if (arg === "--sets") args.sets = next().split(",").map((s) => s.trim()).filter(Boolean);
    else if (arg === "--settle-ms") args.settleMs = Number(next());
    else throw new Error(`unknown argument: ${arg}`);
  }
  return args;
}

async function loadPuppeteer() {
  const candidates = [process.env.STUDIO_PUPPETEER_CORE, "puppeteer-core", join(process.cwd(), "node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js")].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const target = candidate.startsWith("/") || candidate.startsWith(".") ? pathToFileURL(resolve(candidate)).href : candidate;
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
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME. This script refuses to fabricate images.");
}

function parseSet(spec) {
  const match = /^(\d{2,4})x(\d{2,4})-(light|dark)$/.exec(spec);
  if (!match) throw new Error(`set must look like 1440x900-light, got ${JSON.stringify(spec)}`);
  return { width: Number(match[1]), height: Number(match[2]), theme: match[3], name: spec };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const wireframe = resolve(args.wireframe);
  if (!existsSync(wireframe)) throw new Error(`wireframe not found: ${wireframe}`);
  const url = pathToFileURL(wireframe).href;
  const puppeteer = await loadPuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean)],
  });
  const report = [];
  try {
    const page = await browser.newPage();
    let pageErrors = 0;
    page.on("pageerror", () => { pageErrors += 1; });
    for (const set of args.sets.map(parseSet)) {
      const captured = [];
      await page.setViewport({ width: set.width, height: set.height, deviceScaleFactor: 1 });
      for (const pane of PANES) {
        const target = `${url}?pane=${pane}&theme=${set.theme}`;
        const response = await page.goto(target, { waitUntil: "domcontentloaded" });
        if (!response || response.status() !== 0 && response.status() !== 200) throw new Error(`wireframe did not load for ${pane}`);
        await new Promise((done) => setTimeout(done, args.settleMs));
        const applied = await page.evaluate((theme) => document.documentElement.dataset.theme === theme, set.theme);
        if (!applied) throw new Error(`theme ${set.theme} not applied for ${pane}`);
        const body = await page.evaluate((p) => {
          const el = document.getElementById("paneBody");
          return Boolean(el && el.innerHTML.trim()) && document.querySelectorAll("#nav .item").length === 6 && p;
        }, pane);
        if (!body) throw new Error(`pane ${pane} did not render`);
        if (pageErrors) throw new Error("page error during wireframe capture");
        const png = await page.screenshot({ type: "png" });
        if (!png || png.length === 0) throw new Error(`empty screenshot for ${set.name}/${pane}`);
        captured.push({ pane, png });
      }
      const dir = join(resolve(args.out), set.name);
      await mkdir(dir, { recursive: true });
      const index = {
        schema: 1,
        fixture_of: "docs/media/uiv2-v2/reference/wireframe.html",
        captured_with: "scripts/uiux_v2_reference_capture.mjs",
        viewport: { width: set.width, height: set.height, device_scale_factor: 1 },
        variant: { theme: set.theme, density: null },
        shots: [],
      };
      for (const { pane, png } of captured) {
        const file = `${pane}.png`;
        await writeFile(join(dir, file), png);
        index.shots.push({ id: `ref-${pane}`, pane, file, bytes: png.length, sha256: createHash("sha256").update(png).digest("hex") });
      }
      await writeFile(join(dir, "index.json"), `${JSON.stringify(index, null, 2)}\n`);
      report.push({ set: set.name, dir, shots: index.shots.length });
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ captured: report }, null, 2));
}

main().catch((error) => { console.error(`uiux_v2_reference_capture: ${error.message}`); process.exit(1); });
