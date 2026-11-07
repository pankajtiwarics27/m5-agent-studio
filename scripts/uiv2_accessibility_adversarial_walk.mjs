#!/usr/bin/env node
/**
 * UIV2-28: accessibility, zoom, nested-scroll and reduced-motion adversarial gate.
 *
 * The default run starts an isolated generated-token fixture and proves:
 *   - rendered text contrast and accessible names on all six panes in light/dark;
 *   - real Tab traversal with no pane-level focus loop;
 *   - every control remains reachable at 200% at 1640x900, 1440x900, 1280x800
 *     and 390x844 in both themes (half CSS viewport, device scale factor 2);
 *   - reduced-motion durations are effectively zero;
 *   - a route-created long Council conclusion opens a modal whose focus is
 *     contained and whose Escape path restores both focus and exact scroll offsets.
 *
 * This is a browser regression, not a screen-reader certification. Computed
 * solid backgrounds are measured with WCAG relative luminance; an unmeasurable
 * image/gradient background fails instead of being silently ignored. Disabled
 * controls and descendants of closed native disclosures are not measured as
 * visible content. No provider request, user workspace, or real credential is
 * used. Screenshots and capture-manifest.json are local evidence, never required
 * release inputs.
 */
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const PANES = ["start", "harness", "hall", "council", "debate", "chat"];
const VIEWPORTS = [
  { label: "1640x900", width: 1640, height: 900 },
  { label: "1440x900", width: 1440, height: 900 },
  { label: "1280x800", width: 1280, height: 800 },
  { label: "390x844", width: 390, height: 844 },
];
const THEMES = ["light", "dark"];
const ZOOM_FACTOR = 2;
const TEXT_FLOOR = 4.5;
const LARGE_TEXT_FLOOR = 3;
const TARGET_MIN = 24;
const LONG_REQUEST = Array.from({ length: 16 }, (_, index) =>
  `Transcript paragraph ${index + 1}: inspect the request, preserve the session and leave an independently scrollable record.`,
).join("\n\n");
const LONG_CONCLUSION = Array.from({ length: 24 }, (_, index) =>
  `Decision paragraph ${index + 1}: preserve every voice, state the evidence and dissent, ` +
  "and keep the conclusion readable without moving the transcript or the page.",
).join("\n\n") + "\n\nFinal marker: uiv2-28-conclusion-end.";

function usage() {
  return [
    "Run the UIV2-28 real-browser accessibility adversarial gate.",
    "  node scripts/uiv2_accessibility_adversarial_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/uiv2_accessibility_adversarial_walk.mjs --base-url URL --token TOKEN [--json]",
    "Without --base-url an isolated council_readiness_seed fixture is started.",
  ].join("\n");
}

function parseArgs(argv) {
  const args = { baseUrl: "", token: "", screenshots: "", json: false, settleMs: 90, timeoutMs: 90000 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[++i];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      return value;
    };
    if (arg === "--base-url") args.baseUrl = next();
    else if (arg === "--token") args.token = next();
    else if (arg === "--screenshots") args.screenshots = next();
    else if (arg === "--settle-ms") args.settleMs = Number(next());
    else if (arg === "--timeout-ms") args.timeoutMs = Number(next());
    else if (arg === "--json") args.json = true;
    else if (arg === "--help" || arg === "-h") { console.log(usage()); process.exit(0); }
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (!Number.isFinite(args.settleMs) || args.settleMs < 0) throw new Error("--settle-ms must be non-negative");
  if (!Number.isSafeInteger(args.timeoutMs) || args.timeoutMs <= 0) throw new Error("--timeout-ms must be positive");
  return args;
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const errors = [];
  for (const candidate of candidates) {
    try {
      const module = await import(pathToFileURL(resolve(candidate)).href);
      return module.default || module;
    } catch (error) { errors.push(error.message); }
  }
  throw new Error(`puppeteer-core is unavailable: ${errors.join("; ")}`);
}

function chromePath() {
  const explicit = process.env.STUDIO_SCREENSHOT_CHROME;
  if (explicit && existsSync(explicit)) return explicit;
  for (const candidate of ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("set STUDIO_SCREENSHOT_CHROME to a Chromium executable");
}

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try { if ((await fetch(url)).status < 500) return; } catch { /* not ready */ }
    await sleep(150);
  }
  throw new Error(`fixture did not answer ${url}`);
}

async function startFixture(args) {
  const child = spawn(process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--port", "0"],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, reject) => {
    const timer = setTimeout(() => reject(new Error(`fixture startup timed out: ${stderr}`)), args.timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
      const line = stdout.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try { resolveReady(JSON.parse(line)); } catch { reject(new Error(`invalid fixture metadata: ${line}`)); }
    });
    child.on("exit", (code) => { clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${stderr}`)); });
  });
  await waitForHttp(`${ready.base_url}/`, args.timeoutMs);
  return { child, baseUrl: ready.base_url, token: ready.token };
}

function loopbackUrl(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("only a credential-free loopback root URL is accepted");
  }
  return url;
}

/** This function is serialized into Chromium. Keep every helper inside it. */
const AUDIT = () => {
  const hiddenByClosedDetails = (el) => {
    let node = el.parentElement;
    while (node) {
      if (node.tagName === "DETAILS" && !node.open) {
        const summary = [...node.children].find((child) => child.tagName === "SUMMARY");
        if (!summary || !summary.contains(el)) return true;
      }
      node = node.parentElement;
    }
    return false;
  };
  const visible = (el) => {
    if (!(el instanceof Element) || el.closest("[hidden],[inert],[aria-hidden='true']") || hiddenByClosedDetails(el)) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) > 0;
  };
  const nameOf = (el) => {
    const aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    if (ids.length) return ids.map((id) => document.getElementById(id)?.textContent || "").join(" ").trim();
    const label = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
    if (label?.textContent.trim()) return label.textContent.trim();
    if (el.closest("label")?.textContent.trim()) return el.closest("label").textContent.trim();
    return (el.textContent || el.getAttribute("title") || el.getAttribute("placeholder") || "").replace(/\s+/g, " ").trim();
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(), id: el.id || "",
    cls: (typeof el.className === "string" ? el.className : "").slice(0, 50),
    text: nameOf(el).slice(0, 90),
  });
  const selector = "a[href],button,input:not([type=hidden]),select,textarea,summary,[role='button'],[tabindex]";
  const focusable = (root) => [...root.querySelectorAll(selector)].filter((el) =>
    visible(el) && !el.matches(":disabled") && el.getAttribute("aria-disabled") !== "true" &&
    el.tabIndex >= 0 && !el.closest("[aria-hidden='true']"));
  const rgba = (value) => {
    const parts = (value.match(/[\d.]+/g) || []).map(Number);
    // color-mix() is serialized by Chromium as CSS Color 4
    // `color(srgb 0..1 0..1 0..1 / alpha)`, not rgb(0..255,...).
    if (/^color\(srgb\s/.test(value)) {
      return [0, 1, 2].map((index) => (parts[index] || 0) * 255).concat(parts.length > 3 ? parts[3] : 1);
    }
    return [parts[0] || 0, parts[1] || 0, parts[2] || 0, parts.length > 3 ? parts[3] : 1];
  };
  const over = (top, bottom) => {
    const alpha = top[3] + bottom[3] * (1 - top[3]);
    if (!alpha) return [0, 0, 0, 0];
    return [0, 1, 2].map((i) => (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / alpha).concat(alpha);
  };
  const luminance = (color) => {
    const channels = color.slice(0, 3).map((value) => {
      const srgb = value / 255;
      return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const ratioOf = (first, second) => {
    const a = luminance(first); const b = luminance(second);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const backgroundOf = (el) => {
    let accumulated = [0, 0, 0, 0];
    let node = el;
    let nonSolid = false;
    while (node) {
      const style = getComputedStyle(node);
      if (style.backgroundImage !== "none") nonSolid = true;
      accumulated = over(rgba(style.backgroundColor), accumulated);
      if (accumulated[3] >= 0.999) break;
      node = node.parentElement;
    }
    return { color: over(accumulated, [255, 255, 255, 1]), nonSolid };
  };
  const contrast = [];
  for (const el of document.querySelectorAll(".topbar *, .pane.active *")) {
    if (!visible(el) || el.matches(":disabled") || el.closest(":disabled") ||
        ![...el.childNodes].some((node) => node.nodeType === 3 && node.textContent.trim())) continue;
    const style = getComputedStyle(el);
    const background = backgroundOf(el);
    const foreground = over(rgba(style.color), background.color);
    const ratio = ratioOf(foreground, background.color);
    const size = Number.parseFloat(style.fontSize);
    const weight = Number.parseInt(style.fontWeight, 10) || 400;
    const floor = size >= 24 || (size >= 18.66 && weight >= 700) ? 3 : 4.5;
    contrast.push({
      ...describe(el), foreground: style.color,
      background: background.color.slice(0, 3).map((value) => Math.round(value * 100) / 100),
      ratio: Math.round(ratio * 100) / 100, floor, nonSolid: background.nonSolid,
      passed: !background.nonSolid && ratio >= floor,
    });
  }
  const controls = focusable(document);
  const scroller = (el, axis) => {
    let node = el.parentElement;
    const horizontal = axis === "x";
    while (node) {
      const style = getComputedStyle(node);
      const overflow = horizontal ? style.overflowX : style.overflowY;
      const scrollSize = horizontal ? node.scrollWidth : node.scrollHeight;
      const clientSize = horizontal ? node.clientWidth : node.clientHeight;
      if (["auto", "scroll"].includes(overflow) && scrollSize > clientSize + 1) return true;
      node = node.parentElement;
    }
    return false;
  };
  const clipped = [];
  const small = [];
  for (const el of controls) {
    const rect = el.getBoundingClientRect();
    const label = el.closest("label");
    const labelRect = label?.getBoundingClientRect();
    const hit = labelRect && labelRect.width > rect.width ? labelRect : rect;
    if (hit.width < 24 || hit.height < 24) small.push({ ...describe(el), width: Math.round(hit.width), height: Math.round(hit.height) });
    const outsideX = rect.left < -2 || rect.right > innerWidth + 2;
    const outsideY = rect.top < -2 || rect.bottom > innerHeight + 2;
    if ((outsideX && !scroller(el, "x")) || (outsideY && !scroller(el, "y"))) {
      const chain = [];
      let ancestor = el.parentElement;
      for (let depth = 0; ancestor && depth < 7; depth += 1, ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        chain.push({ tag: ancestor.tagName.toLowerCase(), id: ancestor.id || "",
          cls: (typeof ancestor.className === "string" ? ancestor.className : "").slice(0, 40),
          overflowX: style.overflowX, overflowY: style.overflowY,
          scrollWidth: ancestor.scrollWidth, clientWidth: ancestor.clientWidth,
          scrollHeight: ancestor.scrollHeight, clientHeight: ancestor.clientHeight });
      }
      clipped.push({ ...describe(el), left: Math.round(rect.left), right: Math.round(rect.right), top: Math.round(rect.top), bottom: Math.round(rect.bottom), chain });
    }
  }
  const unnamed = controls.filter((el) => !nameOf(el)).map(describe);
  const positive = [...document.querySelectorAll(".pane.active [tabindex]")].filter((el) => Number(el.getAttribute("tabindex")) > 0).map(describe);
  const motion = [];
  for (const el of document.querySelectorAll(".topbar, .topbar *, .pane.active, .pane.active *")) {
    if (!visible(el)) continue;
    const style = getComputedStyle(el);
    const durations = (value) => value.split(",").map((entry) => {
      const number = Number.parseFloat(entry);
      return entry.includes("ms") ? number : number * 1000;
    });
    const animation = style.animationName !== "none" && Math.max(...durations(style.animationDuration)) > 0.011;
    const transition = Math.max(...durations(style.transitionDuration)) > 0.011;
    const smooth = ["auto", "scroll"].includes(style.overflowY) && style.scrollBehavior === "smooth";
    if (animation || transition || smooth) motion.push({ ...describe(el), animation, transition, smooth });
  }
  return {
    contrast,
    unnamed,
    positive,
    clipped,
    small,
    motion,
    overflow: document.documentElement.scrollWidth - innerWidth,
    tabbable: focusable(document.getElementById(document.querySelector(".pane.active")?.id || ""))?.length || 0,
  };
};

async function newPage(browser, args, theme, viewport, scale = 1) {
  const page = await browser.newPage();
  await page.setViewport({ width: viewport.width, height: viewport.height, deviceScaleFactor: scale });
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.evaluateOnNewDocument((token, themeName) => {
    sessionStorage.setItem("m5_api_token", token);
    localStorage.setItem("m5_theme", themeName);
  }, args.token, theme);
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    try {
      const target = new URL(request.url());
      if (target.origin !== new URL(args.baseUrl).origin && !["data:", "blob:"].includes(target.protocol)) {
        errors.push(`outbound request blocked: ${target.origin}`);
        void request.abort();
        return;
      }
      void request.continue();
    } catch { void request.abort(); }
  });
  const response = await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  if (!response || response.status() !== 200) throw new Error(`document status ${response?.status()}`);
  await sleep(args.settleMs * 8);
  return { page, errors };
}

async function selectPane(page, args, pane) {
  await page.evaluate((name) => switchTab(name), pane);
  if (pane === "council") {
    // Overview mounts four stage cards as their responses arrive. Select the
    // Architect through its real control so the keyboard audit has one stable
    // stage rather than measuring nodes that the overview is still replacing.
    await page.evaluate(() => document.getElementById("step-card-1").click());
    await page.waitForFunction(() =>
      document.getElementById("pipelineWorkspace")?.dataset.stage === "1" &&
      (document.getElementById("stage-dom-1") || !document.getElementById("pipelineStageEmpty")?.hidden),
      { timeout: 15000 });
  }
  await sleep(args.settleMs * (pane === "council" ? 4 : 1));
}

async function trapWalk(page, args) {
  const initial = await page.evaluate(() => {
    const root = document.querySelector(".pane.active");
    const hiddenByClosedDetails = (el) => {
      let node = el.parentElement;
      while (node) {
        if (node.tagName === "DETAILS" && !node.open) {
          const summary = [...node.children].find((child) => child.tagName === "SUMMARY");
          if (!summary || !summary.contains(el)) return true;
        }
        node = node.parentElement;
      }
      return false;
    };
    const controls = [...root.querySelectorAll("a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex='-1'])")]
      .filter((el) => {
        const rect = el.getBoundingClientRect(); const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden" &&
          !el.closest("[hidden],[inert],[aria-hidden='true']") && !hiddenByClosedDetails(el);
      });
    controls.forEach((el, index) => { el.dataset.uiv228Tab = String(index); });
    if (controls[0]) controls[0].focus();
    return { count: controls.length, root: root.id };
  });
  if (!initial.count) return { passed: false, detail: "no tabbable controls" };
  const seen = new Set();
  let repeated = "";
  let left = false;
  let dynamic = 0;
  // Scrolling and asynchronous session state can reveal a control after the
  // initial ledger (for example Chat's Jump to latest). Give that control a
  // stable key and keep walking; a newly visible stop is not itself a trap.
  for (let index = 0; index < initial.count + 24; index += 1) {
    await page.keyboard.press("Tab");
    const current = await page.evaluate((rootId) => {
      const active = document.activeElement;
      return { inside: document.getElementById(rootId).contains(active), key: active?.dataset.uiv228Tab || "", id: active?.id || active?.tagName || "" };
    }, initial.root);
    if (!current.inside) { left = true; break; }
    const key = current.key || `dynamic:${current.id}`;
    if (!current.key) dynamic += 1;
    if (seen.has(key)) { repeated = key; break; }
    seen.add(key);
  }
  await page.evaluate(() => document.querySelectorAll("[data-uiv228-tab]").forEach((el) => delete el.dataset.uiv228Tab));
  const finalTabbable = (await page.evaluate(AUDIT)).tabbable;
  // A live stage may replace controls while it renders. Require every stop in
  // the stable case; if the DOM changed, use the smaller exit ledger rather
  // than demanding focus from removed nodes. Leaving the pane and having no
  // repeated focus key remain mandatory in both cases.
  const required = Math.min(Math.max(initial.count - 1, 0), Math.max(finalTabbable - 1, 0));
  return { passed: left && !repeated && required > 0 && seen.size >= required,
    detail: `stops=${initial.count} visited=${seen.size} required=${required} final=${finalTabbable} dynamic=${dynamic} left=${left} repeated=${repeated || "none"}` };
}

async function createLongSession(page) {
  return page.evaluate(async ({ request, conclusion }) => {
    await fetch("/api/hall/sessions", { method: "DELETE" });
    const createdResponse = await fetch("/api/hall/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request, title: "UIV2-28 long conclusion" }),
    });
    const created = await createdResponse.json();
    if (!createdResponse.ok) throw new Error(`create ${createdResponse.status}`);
    const closed = await fetch(`/api/hall/sessions/${encodeURIComponent(created.id)}/close`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conclusion, author: "Lead Arbiter" }),
    });
    if (!closed.ok) throw new Error(`close ${closed.status}`);
    await selectHallSession(created.id);
    return created.id;
  }, { request: LONG_REQUEST, conclusion: LONG_CONCLUSION });
}

async function main(args) {
  let fixture = null;
  if (!args.baseUrl) {
    fixture = await startFixture(args);
    args.baseUrl = fixture.baseUrl;
    args.token = fixture.token;
  }
  const url = loopbackUrl(args.baseUrl);
  args.baseUrl = url.href;
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(args.token)) throw new Error("a synthetic token is required");
  const anonymous = await fetch(new URL("/api/workspace/directory", url), { redirect: "manual" });
  const authenticated = await fetch(new URL("/api/workspace/directory", url), {
    headers: { Authorization: `Bearer ${args.token}` }, redirect: "manual",
  });
  if (anonymous.status !== 401 || authenticated.status !== 200) throw new Error("fixture authentication boundary failed");
  const puppeteer = await loadPuppeteer();
  const browser = await puppeteer.launch({
    executablePath: chromePath(), headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean)],
  });
  const checks = [];
  const captures = [];
  const record = (group, state, step, passed, detail = "") => {
    checks.push({ group, ...state, step, passed: Boolean(passed), detail });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${group} ${Object.values(state).join("/")}: ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const shoot = async (page, group, state, pane) => {
    if (!args.screenshots) return;
    const path = resolve(args.screenshots, `${group}-${Object.values(state).join("-")}${state.pane ? "" : `-${pane}`}.png`);
    await mkdir(args.screenshots, { recursive: true });
    await page.screenshot({ path });
    captures.push({ group, ...state, pane, path, sha256: createHash("sha256").update(await readFile(path)).digest("hex") });
  };
  const summarizeContrast = (audit) => {
    const failed = audit.contrast.filter((entry) => !entry.passed);
    const lowest = audit.contrast.length ? Math.min(...audit.contrast.map((entry) => entry.ratio)) : 0;
    return { failed, detail: `${audit.contrast.length} pairs, lowest=${lowest.toFixed(2)}:1, failures=${failed.length}` };
  };
  try {
    // Normal-size rendered contrast, semantics, reduced motion and Tab traversal.
    for (const theme of THEMES) {
      const viewport = VIEWPORTS[1];
      const { page, errors } = await newPage(browser, args, theme, viewport);
      try {
        for (const pane of PANES) {
          await selectPane(page, args, pane);
          const audit = await page.evaluate(AUDIT);
          const state = { viewport: viewport.label, theme, pane };
          const contrast = summarizeContrast(audit);
          record("rendered", state, "all visible text meets its WCAG contrast floor with no unmeasured background", contrast.failed.length === 0 && audit.contrast.length > 0,
            contrast.failed.length ? `${contrast.detail}; ${JSON.stringify(contrast.failed.slice(0, 3))}` : contrast.detail);
          record("rendered", state, "all tabbable controls have accessible names and no positive tabindex", audit.unnamed.length === 0 && audit.positive.length === 0,
            `${audit.tabbable} pane stops; unnamed=${JSON.stringify(audit.unnamed.slice(0, 3))}; positive=${audit.positive.length}`);
          record("rendered", state, "reduced motion disables non-essential animation, transition and smooth scrolling", audit.motion.length === 0, JSON.stringify(audit.motion.slice(0, 3)));
          const trap = await trapWalk(page, args);
          record("keyboard", state, "Tab traverses the pane and leaves it without a focus loop", trap.passed, trap.detail);
          await shoot(page, "rendered", state, pane);
        }
        record("browser", { viewport: viewport.label, theme }, "no page or outbound-request error occurred", errors.length === 0, errors.slice(0, 3).join(" | "));
      } finally { await page.close(); }
    }

    // Real browser-style 200%: half the CSS viewport and twice the device scale.
    for (const viewport of VIEWPORTS) {
      const effective = { width: Math.round(viewport.width / ZOOM_FACTOR), height: Math.round(viewport.height / ZOOM_FACTOR) };
      for (const theme of THEMES) {
        const { page, errors } = await newPage(browser, args, theme, effective, ZOOM_FACTOR);
        try {
          for (const pane of PANES) {
            await selectPane(page, args, pane);
            const audit = await page.evaluate(AUDIT);
            const state = { viewport: viewport.label, theme, pane };
            record("zoom-200", state, "200% keeps every control reachable and at least 24 CSS px without page overflow",
              audit.clipped.length === 0 && audit.small.length === 0 && audit.overflow <= 1,
              `overflow=${audit.overflow}px clipped=${JSON.stringify(audit.clipped.slice(0, 3))} small=${JSON.stringify(audit.small.slice(0, 3))}`);
            await shoot(page, "zoom-200", state, pane);
          }
          record("browser", { viewport: viewport.label, theme }, "no page or outbound-request error occurred at 200%", errors.length === 0, errors.slice(0, 3).join(" | "));
        } finally { await page.close(); }
      }
    }

    // Long conclusion and nested scroll recovery at desktop and phone 200%.
    for (const viewport of [VIEWPORTS[2], VIEWPORTS[3]]) {
      const theme = viewport.width === 390 ? "light" : "dark";
      const effective = { width: Math.round(viewport.width / 2), height: Math.round(viewport.height / 2) };
      const { page, errors } = await newPage(browser, args, theme, effective, 2);
      try {
        await selectPane(page, args, "hall");
        const sessionId = await createLongSession(page);
        await sleep(args.settleMs * 3);
        const state = { viewport: viewport.label, theme };
        const before = await page.evaluate(() => {
          const stream = document.getElementById("hallStream");
          const outer = stream.scrollHeight > stream.clientHeight ? stream : document.querySelector("#pane-hall .hall-main");
          const conclusion = document.getElementById("hallConclusionBody");
          outer.scrollTop = Math.min(96, Math.max(0, outer.scrollHeight - outer.clientHeight));
          conclusion.scrollTop = Math.min(72, Math.max(0, conclusion.scrollHeight - conclusion.clientHeight));
          window.scrollTo(0, Math.min(32, Math.max(0, document.documentElement.scrollHeight - innerHeight)));
          const expand = document.querySelector('[data-conclusion-action="expand"]');
          expand.focus({ preventScroll: true });
          return { streamHost: outer.id || "hall-main", stream: outer.scrollTop,
            conclusion: conclusion.scrollTop, page: scrollY,
            streamRange: outer.scrollHeight - outer.clientHeight,
            conclusionRange: conclusion.scrollHeight - conclusion.clientHeight };
        });
        record("long-recovery", state, "transcript and pinned conclusion hold independent nonzero nested scroll offsets",
          before.stream > 0 && before.conclusion > 0, JSON.stringify(before));
        await page.keyboard.press("Enter");
        await sleep(args.settleMs * 2);
        const open = await page.evaluate(() => ({
          open: document.getElementById("hallConclusionModal").classList.contains("open"),
          full: document.getElementById("hallConclusionModalBody").textContent.includes("uiv2-28-conclusion-end"),
          focus: document.getElementById("hallConclusionModal").contains(document.activeElement),
        }));
        record("long-recovery", state, "the full route-created conclusion opens at 200% with focus inside", Boolean(sessionId) && open.open && open.full && open.focus, JSON.stringify(open));
        let contained = true;
        for (let index = 0; index < 8; index += 1) {
          await page.keyboard.press("Tab");
          contained = contained && await page.evaluate(() => document.getElementById("hallConclusionModal").contains(document.activeElement));
        }
        record("long-recovery", state, "Tab stays inside the intentional long-content modal", contained);
        const modalEnd = await page.evaluate((streamHost) => {
          const body = document.getElementById("hallConclusionModalBody");
          const outer = streamHost === "hallStream" ? document.getElementById("hallStream") : document.querySelector("#pane-hall .hall-main");
          body.scrollTop = body.scrollHeight;
          return { scrollTop: body.scrollTop, range: body.scrollHeight - body.clientHeight,
            atEnd: body.scrollTop + body.clientHeight >= body.scrollHeight - 2,
            marker: body.textContent.includes("uiv2-28-conclusion-end"),
            page: scrollY, stream: outer.scrollTop,
            conclusion: document.getElementById("hallConclusionBody").scrollTop };
        }, before.streamHost);
        record("long-recovery", state, "the long dialog scrolls to its final marker without moving the underlying regions",
          modalEnd.range > 0 && modalEnd.scrollTop > 0 && modalEnd.atEnd && modalEnd.marker &&
          modalEnd.page === before.page && modalEnd.stream === before.stream && modalEnd.conclusion === before.conclusion,
          JSON.stringify(modalEnd));
        await shoot(page, "long-recovery", state, "conclusion-modal");
        await page.keyboard.press("Escape");
        await sleep(args.settleMs * 2);
        const restored = await page.evaluate((streamHost) => {
          const outer = streamHost === "hallStream" ? document.getElementById("hallStream") : document.querySelector("#pane-hall .hall-main");
          return {
            open: document.getElementById("hallConclusionModal").classList.contains("open"),
            action: document.activeElement?.dataset.conclusionAction || "",
            stream: outer.scrollTop,
            conclusion: document.getElementById("hallConclusionBody").scrollTop, page: scrollY,
          };
        }, before.streamHost);
        record("long-recovery", state, "Escape restores the Expand trigger", !restored.open && restored.action === "expand", JSON.stringify(restored));
        record("long-recovery", state, "Escape restores transcript, conclusion and page scroll exactly",
          restored.stream === before.stream && restored.conclusion === before.conclusion && restored.page === before.page,
          `before=${JSON.stringify(before)} after=${JSON.stringify(restored)}`);
        record("browser", state, "no page or outbound-request error occurred during long recovery", errors.length === 0, errors.slice(0, 3).join(" | "));
      } finally { await page.close(); }
    }
  } finally {
    await browser.close();
    if (fixture?.child) fixture.child.kill("SIGTERM");
  }
  const failed = checks.filter((check) => !check.passed);
  const sourceSha256 = {};
  for (const path of ["static/index.html", "scripts/uiv2_accessibility_adversarial_walk.mjs"]) {
    sourceSha256[path] = createHash("sha256").update(await readFile(resolve(path))).digest("hex");
  }
  const manifest = {
    story: "UIV2-28",
    git: { head: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
      dirty: Boolean(execFileSync("git", ["status", "--porcelain"], { encoding: "utf8" }).trim()) },
    source_sha256: sourceSha256,
    generated_at: new Date().toISOString(),
    method: { contrast: "WCAG 2 relative luminance on computed solid backgrounds", zoom: "half CSS viewport with deviceScaleFactor=2", reducedMotion: "prefers-reduced-motion: reduce" },
    totals: { checks: checks.length, passed: checks.length - failed.length, failed: failed.length, captures: captures.length },
    checks, captures,
  };
  if (args.screenshots) {
    await mkdir(args.screenshots, { recursive: true });
    await writeFile(resolve(args.screenshots, "capture-manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
  }
  if (args.json) console.log(JSON.stringify(manifest, null, 2));
  else console.log(`\n${manifest.totals.passed}/${checks.length} checks · ${captures.length} screenshots`);
  return failed.length ? 1 : 0;
}

main(parseArgs(process.argv.slice(2))).then((code) => { process.exitCode = code; })
  .catch((error) => { console.error(error.stack || error.message); process.exitCode = 1; });
