#!/usr/bin/env node
/**
 * Prove that keyboard focus and pointer hover paint the same shared action
 * surface on all six Studio panes, while retaining a visible focus ring.
 *
 * This walk only reads the isolated demo API and captures the six pane states.
 * It never creates sessions, changes settings, or calls a model.
 *
 * Usage:
 *   node scripts/uiux_interaction_polish_walk.mjs --base-url http://127.0.0.1:PORT \
 *     --token THROWAWAY_TOKEN --screenshots-dir docs/media/uiux-p5-05/after-focus
 *   Add --allow-unmatched only for the before-change baseline; its JSON index
 *   records the measured mismatch and the command still exits successfully.
 */
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const TABS = ["start", "harness", "hall", "council", "debate", "chat"];
const DEFAULTS = { width: 1440, height: 900, scale: 2 };

function argsFrom(argv) {
  const args = { baseUrl: "", token: "", out: "", theme: "dark", viewport: { ...DEFAULTS }, allowUnmatched: false };
  for (let i = 0; i < argv.length; i += 1) {
    const take = () => {
      const value = argv[++i];
      if (!value) throw new Error(`${argv[i - 1]} requires a value`);
      return value;
    };
    switch (argv[i]) {
      case "--base-url": args.baseUrl = take(); break;
      case "--token": args.token = take(); break;
      case "--screenshots-dir": args.out = take(); break;
      case "--theme": args.theme = take(); break;
      case "--viewport": {
        const match = /^(\d{3,4})x(\d{3,4})$/.exec(take());
        if (!match) throw new Error("--viewport must be WIDTHxHEIGHT");
        args.viewport = { width: Number(match[1]), height: Number(match[2]), scale: 1 };
        break;
      }
      case "--allow-unmatched": args.allowUnmatched = true; break;
      case "--help":
      case "-h":
        console.log("Usage: uiux_interaction_polish_walk.mjs --base-url LOOPBACK_URL --token THROWAWAY_TOKEN --screenshots-dir DIR [--theme dark|light] [--viewport 1440x900] [--allow-unmatched]");
        process.exit(0);
        break;
      default: throw new Error(`unknown argument: ${argv[i]}`);
    }
  }
  if (!args.baseUrl || !args.token || !args.out) throw new Error("--base-url, --token and --screenshots-dir are required");
  const url = new URL(args.baseUrl);
  if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("--base-url must be a loopback HTTP(S) origin without a path or credentials");
  }
  if (!["dark", "light"].includes(args.theme)) throw new Error("--theme must be dark or light");
  return { ...args, url };
}

async function loadPuppeteer() {
  const candidates = [process.env.STUDIO_PUPPETEER_CORE, "puppeteer-core", "node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js"].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const target = candidate.startsWith("/") || candidate.startsWith(".") ? pathToFileURL(resolve(candidate)).href : candidate;
      const module = await import(target);
      return module.default || module;
    } catch { /* Try the next explicitly configured or local package. */ }
  }
  throw new Error("puppeteer-core is not importable; install it or set STUDIO_PUPPETEER_CORE");
}

function resolveChrome() {
  const candidate = process.env.STUDIO_SCREENSHOT_CHROME;
  if (!candidate) throw new Error("STUDIO_SCREENSHOT_CHROME must name the Chromium binary");
  return candidate;
}

async function main() {
  const args = argsFrom(process.argv.slice(2));
  const endpoint = new URL("/api/workspace/directory", args.url);
  const unauth = await fetch(endpoint, { redirect: "manual" });
  if (unauth.status !== 401) throw new Error("demo API did not require authentication");
  const auth = await fetch(endpoint, { headers: { Authorization: `Bearer ${args.token}` }, redirect: "manual" });
  if (auth.status !== 200) throw new Error("demo API did not accept the supplied throwaway token");
  const puppeteer = await loadPuppeteer();
  const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean);
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb", ...extraArgs],
  });
  const captured = [];
  const issues = [];
  let pageErrors = 0;
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: args.viewport.width, height: args.viewport.height, deviceScaleFactor: args.viewport.scale });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: args.theme }]);
    page.on("pageerror", () => { pageErrors += 1; });
    await page.evaluateOnNewDocument((token, theme) => {
      sessionStorage.setItem("m5_api_token", token);
      localStorage.setItem("m5_theme", theme);
    }, args.token, args.theme);
    const response = await page.goto(args.url.href, { waitUntil: "domcontentloaded", timeout: 15000 });
    if (!response || response.status() !== 200) throw new Error("demo page did not load directly");
    await new Promise((done) => setTimeout(done, 900));

    for (const tab of TABS) {
      const selected = await page.evaluate((name) => {
        const button = [...document.querySelectorAll(".mode-tab")].find((item) => item.dataset.tab === name);
        if (!button) return false;
        button.click();
        const pane = document.getElementById(`pane-${name}`);
        return button.classList.contains("active") && pane?.classList.contains("active");
      }, tab);
      if (!selected) throw new Error(`pane ${tab} could not be selected`);

      await page.evaluate(() => {
        const button = document.querySelector("#themeToggleBtn");
        if (!(button instanceof HTMLButtonElement)) throw new Error("shared theme action is missing");
        button.blur();
      });
      await page.hover("#themeToggleBtn");
      await new Promise((done) => setTimeout(done, 220));
      const hovered = await page.evaluate(() => {
        const style = getComputedStyle(document.querySelector("#themeToggleBtn"));
        return { background: style.backgroundColor, border: style.borderColor };
      });

      await page.mouse.move(0, 0);
      await page.evaluate(() => {
        const activeTab = document.querySelector(".mode-tab.active");
        if (!(activeTab instanceof HTMLButtonElement)) throw new Error("active navigation tab is missing");
        activeTab.focus();
      });
      for (let i = 0; i < 3; i += 1) await page.keyboard.press("Tab");
      await new Promise((done) => setTimeout(done, 220));
      const focused = await page.evaluate(() => {
        const button = document.activeElement;
        if (!(button instanceof HTMLButtonElement) || button.id !== "themeToggleBtn") return null;
        const style = getComputedStyle(button);
        return {
          background: style.backgroundColor,
          border: style.borderColor,
          focusVisible: button.matches(":focus-visible"),
          outlineColor: style.outlineColor,
          outlineWidth: style.outlineWidth,
        };
      });
      if (!focused) throw new Error(`keyboard path did not reach the shared theme action from ${tab}`);
      if (!focused.focusVisible || focused.outlineWidth !== "2px" || focused.outlineColor === "rgba(0, 0, 0, 0)") {
        issues.push(`${tab}: keyboard focus ring is missing or not visible`);
      }
      if (focused.background !== hovered.background || focused.border !== hovered.border) {
        issues.push(`${tab}: hover (${hovered.background}; ${hovered.border}) differs from focus (${focused.background}; ${focused.border})`);
      }
      const png = await page.screenshot({ type: "png", fullPage: false });
      captured.push({ id: tab, tab, file: `${tab}.png`, png, hover: hovered, focus: focused });
    }
    if (pageErrors) issues.push(`${pageErrors} uncaught page error(s)`);
  } finally {
    await browser.close();
  }

  await mkdir(resolve(args.out), { recursive: true });
  const index = {
    schema: 1,
    captured_at: new Date().toISOString(),
    viewport: { width: args.viewport.width, height: args.viewport.height, device_scale_factor: args.viewport.scale },
    variant: { theme: args.theme, interaction: "keyboard focus on shared theme action" },
    checks: { panes: TABS.length, mismatches: issues.length, status: issues.length ? "mismatch" : "pass" },
    issues,
    shots: [],
  };
  for (const shot of captured) {
    await writeFile(join(resolve(args.out), shot.file), shot.png);
    index.shots.push({
      id: shot.id,
      tab: shot.tab,
      file: shot.file,
      bytes: shot.png.length,
      sha256: createHash("sha256").update(shot.png).digest("hex"),
      hover: shot.hover,
      focus: shot.focus,
    });
  }
  await writeFile(join(resolve(args.out), "index.json"), `${JSON.stringify(index, null, 2)}\n`, "utf-8");
  console.log(JSON.stringify({ out: resolve(args.out), checks: index.checks, issues, panes: index.shots.map(({ id, hover, focus }) => ({ id, hover, focus })) }, null, 2));
  if (issues.length && !args.allowUnmatched) process.exitCode = 1;
}

main().catch((error) => {
  console.error(`uiux_interaction_polish_walk: ${error.message}`);
  process.exit(1);
});
