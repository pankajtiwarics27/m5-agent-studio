#!/usr/bin/env node
/**
 * Capture the onboarding screenshots from a running demo instance (AS-045).
 *
 * Prerequisites, deliberately not vendored into this repository:
 *   - a Chrome/Chromium binary (set STUDIO_SCREENSHOT_CHROME, or have one on PATH)
 *   - puppeteer-core (npm i puppeteer-core, or point STUDIO_PUPPETEER_CORE at
 *     node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js)
 *   - optionally STUDIO_SCREENSHOT_CHROME_ARGS for extra Chrome flags, which is
 *     how a constrained container gets a working headless browser: the npm
 *     package @sparticuz/chromium ships a Chromium build whose network service
 *     needs "--no-zygote --single-process --disable-gpu" here. See
 *     docs/uiux-2-0-baseline-evidence.md for the exact invocation that produced
 *     the committed images.
 *
 * Usage:
 *   python3 scripts/demo_tour.py --serve            # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Flags:
 *   --manifest PATH   screenshot manifest (default docs/media/screenshots/manifest.json)
 *   --out DIR         output directory (default docs/media/screenshots)
 *   --panes LIST      capture only these panes, comma separated (default: all)
 *   --emulate-theme T seed localStorage m5_theme = T before the first document, so
 *                     the shell boots in that theme instead of the OS preference
 *   --emulate-density D set body[data-density] after load; the stylesheet reads it
 *   --viewport WxH    override the manifest viewport's CSS pixels (scale is kept)
 *   --dry-run         print the capture plan and exit; needs no browser
 *
 * The variant flags exist so a theme or a density is photographed rather than
 * described. They change nothing about the manifest: a variant set is written to
 * its own --out directory with its own index.json, and the baseline set stays the
 * one scripts/validate_screenshots.py checks.
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
      case "--panes": args.panes = next().split(",").map((name) => name.trim()).filter(Boolean); break;
      case "--emulate-theme": args.emulateTheme = next(); break;
      case "--emulate-density": args.emulateDensity = next(); break;
      case "--viewport": args.viewport = next(); break;
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
    "  node scripts/capture_screenshots.mjs --base-url URL --token TOKEN --panes start,chat \\",
    "      --emulate-theme light --emulate-density compact --viewport 900x800 --out DIR",
    "  node scripts/capture_screenshots.mjs --dry-run",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/demo.md.",
  ].join("\n");
}

const THEMES = ["dark", "light"];
const DENSITIES = ["comfortable", "compact"];

function viewportOverride(value) {
  const match = /^(\d{2,4})x(\d{2,4})$/.exec(String(value || "").trim());
  if (!match) throw new Error(`--viewport must look like 900x800, got ${JSON.stringify(value)}`);
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (width < 320 || height < 320) {
    throw new Error(`--viewport ${value} is smaller than the 320px floor a phone-class render needs`);
  }
  return { width, height };
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

function selectedShots(args, manifest) {
  if (args.emulateTheme && !THEMES.includes(args.emulateTheme)) {
    throw new Error(`--emulate-theme must be one of ${THEMES.join(", ")}, got ${args.emulateTheme}`);
  }
  if (args.emulateDensity && !DENSITIES.includes(args.emulateDensity)) {
    throw new Error(`--emulate-density must be one of ${DENSITIES.join(", ")}, got ${args.emulateDensity}`);
  }
  if (!args.panes || args.panes.length === 0) return manifest.shots;
  const known = new Set(manifest.shots.map((shot) => shot.id));
  const unknown = args.panes.filter((name) => !known.has(name));
  if (unknown.length > 0) {
    throw new Error(
      `--panes names ${unknown.join(", ")}, which the manifest does not plan; it plans ${[...known].join(", ")}`,
    );
  }
  // Keep manifest order so a subset is a subset of the same capture, not a reordered one.
  return manifest.shots.filter((shot) => args.panes.includes(shot.id));
}

function plan(args, manifest) {
  const shots = selectedShots(args, manifest);
  const override = args.viewport ? viewportOverride(args.viewport) : null;
  return {
    baseUrl: args.baseUrl || "<start scripts/demo_tour.py --serve>",
    out: resolve(args.out),
    viewport: override
      ? { ...manifest.viewport, width: override.width, height: override.height }
      : manifest.viewport,
    variant: {
      theme: args.emulateTheme || null,
      density: args.emulateDensity || null,
    },
    shots: shots.map((shot) => ({
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
  const shots = selectedShots(args, manifest);
  const planViewport = plan(args, manifest).viewport;
  const puppeteer = await loadPuppeteer();
  const executablePath = resolveChrome();
  const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "")
    .split(/\s+/)
    .filter(Boolean);
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--hide-scrollbars",
      "--force-color-profile=srgb",
      ...extraArgs,
    ],
  });
  const captured = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({
      width: planViewport.width,
      height: planViewport.height,
      deviceScaleFactor: planViewport.device_scale_factor || 1,
    });
    if (args.emulateTheme) {
      // The shell picks its theme at boot from localStorage, falling back to the OS
      // preference. Seeding the preference before the first document is the only way
      // to photograph a theme the machine does not use.
      await page.emulateMediaFeatures([
        { name: "prefers-color-scheme", value: args.emulateTheme },
      ]);
    }
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    // Seed the token before the first document exists. The shell starts fetching
    // as it loads; unauthenticated it answers 401 and retries without backoff,
    // which can starve the renderer, so "load, then store the token, then reload"
    // can hang instead of producing an image.
    await page.evaluateOnNewDocument(
      (key, token) => {
        try {
          window.sessionStorage.setItem(key, token);
        } catch (_error) {
          // A document with no storage is a capture problem, not a silent pass:
          // the first authenticated request below will fail loudly instead.
        }
      },
      args.tokenKey,
      args.token,
    );
    if (args.emulateTheme || args.emulateDensity) {
      // Seed the shell's own persisted preferences rather than poking the DOM after
      // load: the app restores theme and density from localStorage on boot, so this
      // is the same path a returning user takes, and a re-render cannot undo it.
      await page.evaluateOnNewDocument(
        (theme, density) => {
          try {
            if (theme) window.localStorage.setItem("m5_theme", theme);
            if (density) window.localStorage.setItem("m5_ui_density", density);
          } catch (_error) {
            // Same reasoning as the token: a document without storage fails loudly later.
          }
        },
        args.emulateTheme,
        args.emulateDensity,
      );
    }
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    // The shell holds streaming connections open for its whole lifetime, so
    // networkidle never arrives. Settle for the same window used between panes.
    await new Promise((done) => setTimeout(done, args.settleMs));
    for (const shot of shots) {
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
  const index = {
    schema: 1,
    captured_at: new Date().toISOString(),
    viewport: planViewport,
    variant: { theme: args.emulateTheme || null, density: args.emulateDensity || null },
    shots: [],
  };
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

export { validateManifest, parseArgs, plan, selectedShots, viewportOverride };
