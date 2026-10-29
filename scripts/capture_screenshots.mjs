#!/usr/bin/env node
/**
 * Capture the onboarding screenshots from a running demo instance (AS-045).
 *
 * Prerequisites, deliberately not vendored into this repository:
 *   - a Chrome/Chromium binary (set STUDIO_SCREENSHOT_CHROME, or have one on PATH)
 *   - puppeteer-core (npm i puppeteer-core, or point STUDIO_PUPPETEER_CORE at
 *     node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js)
 *
 * Usage:
 *   python3 scripts/demo_tour.py --serve            # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Flags:
 *   --manifest PATH   screenshot manifest (default docs/media/screenshots/manifest.json)
 *   --out DIR         output directory (default docs/media/screenshots)
 *   --dry-run         print the capture plan and exit; needs no browser
 *
 * The script writes the PNGs plus docs/media/screenshots/index.json, which
 * records sha256, size, viewport, and capture time. It refuses to write partial
 * sets: if any pane fails, nothing is written and the exit status is non-zero.
 */
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  manifest: "docs/media/screenshots/manifest.json",
  out: "docs/media/screenshots",
  tokenKey: "m5_api_token",
  settleMs: 1200,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS, dryRun: false, baseUrl: "", token: "", timeoutMs: 15000 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      i += 1;
      return value;
    };
    switch (arg) {
      case "--manifest": args.manifest = next(); break;
      case "--out": args.out = next(); break;
      case "--base-url": args.baseUrl = next(); break;
      case "--token": args.token = next(); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--dry-run": args.dryRun = true; break;
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
    "Capture Studio onboarding screenshots from a seeded demo instance.",
    "",
    "  node scripts/capture_screenshots.mjs --base-url URL --token TOKEN [--out DIR]",
    "  node scripts/capture_screenshots.mjs --dry-run",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/demo.md.",
  ].join("\n");
}

function validateManifest(manifest) {
  if (!manifest || typeof manifest !== "object") throw new Error("manifest must be an object");
  if (!Array.isArray(manifest.shots) || manifest.shots.length === 0) {
    throw new Error("manifest.shots must be a non-empty array");
  }
  const seen = new Set();
  for (const shot of manifest.shots) {
    for (const field of ["id", "tab", "title", "file"]) {
      if (typeof shot[field] !== "string" || shot[field].trim() === "") {
        throw new Error(`every shot needs a non-empty ${field}`);
      }
    }
    if (!shot.file.endsWith(".png")) throw new Error(`${shot.file} must be a .png`);
    if (shot.file.includes("/") || shot.file.includes("\\")) {
      throw new Error(`${shot.file} must be a plain file name`);
    }
    if (seen.has(shot.id)) throw new Error(`duplicate shot id: ${shot.id}`);
    seen.add(shot.id);
  }
  const viewport = manifest.viewport || {};
  for (const field of ["width", "height"]) {
    if (!Number.isInteger(viewport[field]) || viewport[field] <= 0) {
      throw new Error(`manifest.viewport.${field} must be a positive integer`);
    }
  }
  return manifest;
}

function plan(args, manifest) {
  return {
    baseUrl: args.baseUrl || "<start scripts/demo_tour.py --serve>",
    out: resolve(args.out),
    viewport: manifest.viewport,
    shots: manifest.shots.map((shot) => ({
      id: shot.id,
      tab: shot.tab,
      file: join(resolve(args.out), shot.file),
    })),
  };
}

async function loadPuppeteer() {
  const candidates = [process.env.STUDIO_PUPPETEER_CORE, "puppeteer-core", join(process.cwd(), "node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js")]
    .filter(Boolean);
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
    `puppeteer-core is not importable. Install it (npm i puppeteer-core) or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`
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
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one, or install Chrome. " +
      "This script refuses to fabricate images."
  );
}

async function capture(args, manifest) {
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  const puppeteer = await loadPuppeteer();
  const executablePath = resolveChrome();
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"],
  });
  const captured = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({
      width: manifest.viewport.width,
      height: manifest.viewport.height,
      deviceScaleFactor: manifest.viewport.device_scale_factor || 1,
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await page.evaluate((key, token) => window.sessionStorage.setItem(key, token), args.tokenKey, args.token);
    await page.reload({ waitUntil: "networkidle2", timeout: args.timeoutMs });
    for (const shot of manifest.shots) {
      await page.evaluate((tab) => {
        if (typeof window.switchTab === "function") window.switchTab(tab);
        else document.querySelector(`.mode-tab[data-tab="${tab}"]`)?.click();
      }, shot.tab);
      await new Promise((done) => setTimeout(done, args.settleMs));
      const png = await page.screenshot({ type: "png" });
      if (!png || png.length === 0) throw new Error(`empty screenshot for ${shot.id}`);
      captured.push({ shot, png });
    }
    if (errors.length > 0) throw new Error(`the page reported errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
  }
  await mkdir(resolve(args.out), { recursive: true });
  const index = { schema: 1, captured_at: new Date().toISOString(), viewport: manifest.viewport, shots: [] };
  for (const { shot, png } of captured) {
    await writeFile(join(resolve(args.out), shot.file), png);
    index.shots.push({
      id: shot.id,
      tab: shot.tab,
      file: shot.file,
      bytes: png.length,
      sha256: createHash("sha256").update(png).digest("hex"),
    });
  }
  await writeFile(join(resolve(args.out), "index.json"), `${JSON.stringify(index, null, 2)}\n`, "utf-8");
  console.log(JSON.stringify({ captured: index.shots.map((shot) => shot.file), out: resolve(args.out) }, null, 2));
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const raw = await readFile(resolve(args.manifest), "utf-8");
  const manifest = validateManifest(JSON.parse(raw));
  if (args.dryRun) {
    console.log(JSON.stringify(plan(args, manifest), null, 2));
    return;
  }
  await capture(args, manifest);
}

main().catch((error) => {
  console.error(`capture_screenshots: ${error.message}`);
  process.exit(1);
});

export { validateManifest, parseArgs, plan };
