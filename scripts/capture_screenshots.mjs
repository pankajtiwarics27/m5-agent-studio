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

const TABS = new Set(["start", "harness", "hall", "council", "debate", "chat"]);

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
  if (manifest.schema !== 1) throw new Error("manifest.schema must be 1");
  const seen = new Set();
  const files = new Set();
  for (const shot of manifest.shots) {
    for (const field of ["id", "tab", "title", "file"]) {
      if (typeof shot[field] !== "string" || shot[field].trim() === "") {
        throw new Error(`every shot needs a non-empty ${field}`);
      }
    }
    if (!TABS.has(shot.tab)) throw new Error(`unknown Studio tab: ${shot.tab}`);
    if (!shot.file.endsWith(".png")) throw new Error(`${shot.file} must be a .png`);
    if (shot.file.startsWith(".") || shot.file.includes("/") || shot.file.includes("\\")) {
      throw new Error(`${shot.file} must be a plain file name`);
    }
    if (seen.has(shot.id)) throw new Error(`duplicate shot id: ${shot.id}`);
    if (files.has(shot.file)) throw new Error(`duplicate shot file: ${shot.file}`);
    seen.add(shot.id);
    files.add(shot.file);
  }
  const viewport = manifest.viewport || {};
  for (const field of ["width", "height"]) {
    if (!Number.isInteger(viewport[field]) || viewport[field] <= 0) {
      throw new Error(`manifest.viewport.${field} must be a positive integer`);
    }
  }
  const scale = viewport.device_scale_factor ?? 1;
  if (typeof scale !== "number" || !Number.isFinite(scale) || scale <= 0) {
    throw new Error("manifest.viewport.device_scale_factor must be positive and finite");
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
    baseUrl: args.baseUrl ? demoUrl(args.baseUrl).href : "<start scripts/demo_tour.py --serve>",
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

function demoUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("--base-url must be a loopback demo URL"); }
  if (!['http:', 'https:'].includes(url.protocol) ||
      !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error("--base-url must be an HTTP(S) loopback origin without a path, credentials, query, or fragment");
  }
  return url;
}

async function verifyAuth(url, token, timeoutMs) {
  const endpoint = new URL('/api/workspace/directory', url);
  const options = { redirect: 'manual', signal: AbortSignal.timeout(timeoutMs) };
  // A disabled auth boundary and a redirect are not acceptable evidence.
  const without = await fetch(endpoint, options);
  if (without.status !== 401) throw new Error("demo API did not require authentication");
  const withToken = await fetch(endpoint, {
    redirect: 'manual', signal: AbortSignal.timeout(timeoutMs),
    headers: { Authorization: `Bearer ${token}` },
  });
  if (withToken.status !== 200 || !(withToken.headers.get('content-type') || '').includes('application/json')) {
    throw new Error("demo API did not accept the supplied token");
  }
  let data;
  try { data = await withToken.json(); } catch { throw new Error("demo API returned invalid JSON"); }
  if (!data || typeof data.workspace_dir !== 'string' || !Array.isArray(data.recent_dirs)) {
    throw new Error("demo API returned an unexpected workspace response");
  }
}

async function capture(args, manifest) {
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(args.token)) throw new Error("--token must be a valid throwaway demo token");
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs <= 0) throw new Error("--timeout-ms must be positive");
  const url = demoUrl(args.baseUrl);
  const shots = selectedShots(args, manifest);
  const planViewport = plan(args, manifest).viewport;
  if ((args.panes || args.emulateTheme || args.emulateDensity || args.viewport) &&
      resolve(args.out) === resolve(DEFAULTS.out)) {
    throw new Error("variant captures require a separate --out directory");
  }
  await verifyAuth(url, args.token, args.timeoutMs);
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
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: args.emulateTheme }]);
    }
    let pageErrors = 0;
    let unexpectedNavigation = false;
    page.on("pageerror", () => { pageErrors += 1; });
    page.on("dialog", (dialog) => { pageErrors += 1; void dialog.dismiss(); });
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (request.isNavigationRequest() && request.frame() === page.mainFrame() &&
          (request.url() !== url.href || request.redirectChain().length > 0)) {
        unexpectedNavigation = true;
        void request.abort();
      } else {
        void request.continue();
      }
    });
    // The shell fetches on load, so seed early, but never seed a child frame or
    // any other origin. A redirect is also blocked before it is requested.
    await page.evaluateOnNewDocument(
      (key, token, origin) => {
        if (window.top === window && location.origin === origin) {
          window.sessionStorage.setItem(key, token);
        }
      },
      args.tokenKey,
      args.token,
      url.origin,
    );
    if (args.emulateTheme || args.emulateDensity) {
      await page.evaluateOnNewDocument((theme, density, origin) => {
        if (window.top !== window || location.origin !== origin) return;
        if (theme) window.localStorage.setItem("m5_theme", theme);
        if (density) window.localStorage.setItem("m5_ui_density", density);
      }, args.emulateTheme, args.emulateDensity, url.origin);
    }
    const response = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    if (!response || response.status() !== 200 || response.request().redirectChain().length ||
        !(response.headers()['content-type'] || '').includes('text/html')) {
      throw new Error("demo document was not a direct successful HTML response");
    }
    const browserAuth = await page.evaluate(async () => {
      const response = await fetch('/api/workspace/directory', { redirect: 'error', cache: 'no-store' });
      if (response.status !== 200) return false;
      const data = await response.json();
      return typeof data.workspace_dir === 'string' && Array.isArray(data.recent_dirs);
    });
    if (!browserAuth) throw new Error("browser could not authenticate to the demo API");
    // The shell holds streaming connections open, so networkidle never arrives.
    await new Promise((done) => setTimeout(done, args.settleMs));
    for (const shot of shots) {
      const found = await page.evaluate((tab) => {
        const buttons = [...document.querySelectorAll('.mode-tab')].filter((b) => b.dataset.tab === tab);
        if (buttons.length !== 1) return false;
        buttons[0].click();
        return true;
      }, shot.tab);
      if (!found) throw new Error(`missing or ambiguous tab for ${shot.id}`);
      await new Promise((done) => setTimeout(done, args.settleMs));
      const valid = await page.evaluate((tab) => {
        const buttons = [...document.querySelectorAll('.mode-tab.active')];
        const panes = [...document.querySelectorAll('.pane.active')];
        const pane = document.getElementById(`pane-${tab}`);
        if (buttons.length !== 1 || buttons[0].dataset.tab !== tab ||
            buttons[0].getAttribute('aria-current') !== 'page' || panes.length !== 1 || panes[0] !== pane) return false;
        for (let node = pane; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) <= 0) return false;
        }
        const rect = pane.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0 &&
          rect.left < innerWidth && rect.top < innerHeight;
      }, shot.tab);
      if (!valid) throw new Error(`tab did not display the expected pane for ${shot.id}`);
      const variantApplied = await page.evaluate((theme, density) =>
        (!theme || document.body.dataset.theme === theme) &&
        (!density || document.body.dataset.density === density), args.emulateTheme, args.emulateDensity);
      if (!variantApplied) throw new Error(`requested theme or density was not applied for ${shot.id}`);
      if (unexpectedNavigation || page.url() !== url.href || pageErrors) {
        throw new Error("unexpected navigation, dialog, or page error during capture");
      }
      const png = await page.screenshot({ type: "png" });
      if (!png || png.length === 0) throw new Error(`empty screenshot for ${shot.id}`);
      captured.push({ shot, png });
    }
    if (unexpectedNavigation || page.url() !== url.href || pageErrors) {
      throw new Error("unexpected navigation, dialog, or page error during capture");
    }
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
