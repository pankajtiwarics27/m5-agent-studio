#!/usr/bin/env node
/**
 * UIV2-26: authenticated live/reference/annotated-comparison matrix.
 *
 * Candidate (never an approval):
 *   node scripts/uiux_v2_cross_tab_reference_walk.mjs --candidate --out docs/media/cross-tab-reference-uiv2-26/candidate
 * Approved structural drift check (fails closed without human approval):
 *   node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out docs/media/cross-tab-reference-uiv2-26/check
 *
 * The demo is started and stopped by this process. Pixel differences are measurements, not a
 * tolerance-based verdict. All new PNGs and candidate/check manifests are local-only evidence.
 */
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { resolve, dirname, join, relative } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash } from "node:crypto";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CONTRACT_PATH = join(ROOT, "product/M5_2_0_UIUX_V2_Cross_Tab_Reference.json");
const APPROVAL_PATH = join(ROOT, "product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json");
const WIREFRAME_PATH = join(ROOT, "docs/media/uiv2-v2/reference/wireframe.html");
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const jsonBytes = (value) => Buffer.from(`${JSON.stringify(value, null, 2)}\n`);

export function contractFingerprint(contract) {
  // Approval status and app/walk source hashes are intentionally excluded: a future app
  // revision must be checked against the old baseline. Semantic rule/reference edits cannot.
  return digest(jsonBytes({
    schema_version: contract.schema_version,
    story: contract.story,
    matrix: contract.matrix,
    layout_contract: contract.layout_contract,
    panes: contract.panes,
    approved_reference: contract.approved_reference,
  }));
}

function usage() {
  return [
    "UIV2-26 cross-tab live/reference structural gate", "",
    "  --candidate --out DIR   capture a candidate; never establishes approval",
    "  --check --out DIR       compare with the separately human-approved baseline",
    "  --only 1440x900         development subset (cannot be approved or checked)",
    "  --theme light|dark      development subset",
    "  --panes start,chat      development subset",
    "  --timeout-ms 15000      navigation/API timeout",
  ].join("\n");
}

function parseArgs(argv) {
  const args = { mode: "", out: "", only: "", theme: "", panes: [], timeoutMs: 15000 };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      const value = argv[index + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      index += 1;
      return value;
    };
    if (arg === "--candidate" || arg === "--check") {
      if (args.mode) throw new Error("choose exactly one of --candidate or --check");
      args.mode = arg === "--check" ? "approved-check" : "candidate";
    } else if (arg === "--out") args.out = next();
    else if (arg === "--only") args.only = next();
    else if (arg === "--theme") args.theme = next();
    else if (arg === "--panes") args.panes = next().split(",").filter(Boolean);
    else if (arg === "--timeout-ms") args.timeoutMs = Number(next());
    else if (arg === "--help" || arg === "-h") { console.log(usage()); process.exit(0); }
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (!args.mode || !args.out) throw new Error("--candidate or --check, and --out DIR, are required");
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 1000) throw new Error("invalid --timeout-ms");
  if (args.mode === "approved-check" && (args.only || args.theme || args.panes.length)) {
    throw new Error("--check always requires the complete 48-capture matrix");
  }
  return args;
}

function findChrome() {
  const candidates = [process.env.STUDIO_SCREENSHOT_CHROME, process.env.CHROME_PATH, "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].filter(Boolean);
  for (const candidate of candidates) if (existsSync(candidate)) return candidate;
  throw new Error("No Chrome/Chromium found. Set STUDIO_SCREENSHOT_CHROME to the pinned browser.");
}

async function loadPuppeteer() {
  const modulePath = process.env.STUDIO_PUPPETEER_CORE || join(ROOT, "tests/browser/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js");
  return import(pathToFileURL(modulePath).href);
}

function gitState() {
  const options = { cwd: ROOT, encoding: "utf8" };
  return {
    head: execFileSync("git", ["rev-parse", "HEAD"], options).trim(),
    dirty: execFileSync("git", ["status", "--porcelain", "--untracked-files=no"], options).trim().length > 0,
  };
}

function startFixture(timeoutMs) {
  const demoRoot = mkdtempSync(join(tmpdir(), "m5-uiv2-26-demo-"));
  const child = spawn(process.env.STUDIO_PYTHON || "python3", [
    "scripts/demo_tour.py", "--serve", "--port", "0", "--root", demoRoot,
  ], {
    cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
  });
  let output = "";
  let url = "";
  child.stdout.on("data", (chunk) => {
    output += chunk.toString();
    const direct = output.match(/http:\/\/127\.0\.0\.1:\d+\/\?token=[A-Za-z0-9_-]+/);
    const jsonUrl = output.match(/"url":\s*"(http:\/\/127\.0\.0\.1:\d+)"/);
    const jsonToken = output.match(/"token":\s*"([A-Za-z0-9_-]+)"/);
    if (direct) url = direct[0];
    else if (jsonUrl && jsonToken) url = `${jsonUrl[1]}/?token=${jsonToken[1]}`;
  });
  child.stderr.on("data", (chunk) => { output += chunk.toString(); });
  const ready = (async () => {
    const deadline = Date.now() + timeoutMs;
    while (!url && Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error(`demo exited before ready: ${output}`);
      await sleep(50);
    }
    if (!url) throw new Error(`demo readiness timed out: ${output}`);
    return url;
  })();
  return { child, ready, root: demoRoot, output: () => output };
}

async function stopDemo(child) {
  const signal = (name) => {
    try {
      if (process.platform !== "win32") process.kill(-child.pid, name);
      else if (child.exitCode === null) child.kill(name);
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  };
  signal("SIGTERM");
  for (let index = 0; index < 50 && child.exitCode === null; index += 1) await sleep(100);
  if (process.platform !== "win32" || child.exitCode === null) signal("SIGKILL");
}

async function waitForDemo(url, timeoutMs) {
  const origin = new URL(url).origin;
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${origin}/api/status`, { redirect: "error" });
      const payload = await response.json();
      if (response.status === 401 && /auth/i.test(JSON.stringify(payload))) return;
      lastError = `${response.status}: ${JSON.stringify(payload)}`;
    } catch (error) { lastError = String(error); }
    await sleep(100);
  }
  throw new Error(`demo API did not become ready: ${lastError}`);
}

function selectedMatrix(contract, args) {
  const keys = [];
  for (const theme of contract.matrix.themes) {
    if (args.theme && args.theme !== theme) continue;
    for (const viewport of contract.matrix.viewports) {
      if (args.only && args.only !== viewport) continue;
      for (const pane of contract.matrix.panes) {
        if (args.panes.length && !args.panes.includes(pane)) continue;
        keys.push({ theme, viewport, pane });
      }
    }
  }
  if (!keys.length) throw new Error("the selected matrix is empty");
  const complete = keys.length === contract.matrix.keys.length && !args.theme && !args.only && !args.panes.length;
  if (args.mode === "approved-check" && !complete) throw new Error("approved checks cannot be partial");
  return { keys, complete };
}

export function diffValues(expected, actual, path = "", differences = []) {
  if (differences.length >= 20) return differences;
  if (JSON.stringify(expected) === JSON.stringify(actual)) return differences;
  if (!expected || !actual || typeof expected !== "object" || typeof actual !== "object" || Array.isArray(expected) || Array.isArray(actual)) {
    differences.push(`${path}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    return differences;
  }
  for (const key of new Set([...Object.keys(expected), ...Object.keys(actual)])) {
    diffValues(expected[key], actual[key], path ? `${path}.${key}` : key, differences);
    if (differences.length >= 20) break;
  }
  return differences;
}

async function collectLive(page, pane, theme, viewport) {
  return page.evaluate((paneContract, requestedTheme, viewportName) => {
    const checks = [];
    const check = (id, passed, detail) => checks.push({ id: `live.${id}`, passed: Boolean(passed), detail: String(detail) });
    const round = (number) => Math.round(number * 100) / 100;
    const rect = (selector) => {
      const element = typeof selector === "string" ? document.querySelector(selector) : selector;
      if (!element) return null;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return {
        x: round(box.x), y: round(box.y), width: round(box.width), height: round(box.height),
        display: style.display, visibility: style.visibility,
      };
    };
    const visible = (selector) => {
      const element = document.querySelector(selector);
      if (!element) return false;
      const box = element.getBoundingClientRect();
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility !== "visible" || Number(style.opacity) <= 0) return false;
      }
      return box.width > 0 && box.height > 0;
    };
    const [width, height] = viewportName.split("x").map(Number);
    const activeTabs = [...document.querySelectorAll(".mode-tab.active")];
    const activePanes = [...document.querySelectorAll(".pane.active")];
    const bodyStyle = getComputedStyle(document.body);
    const rootStyle = getComputedStyle(document.documentElement);
    const header = rect(".topbar");
    const nav = rect(".mode-tabs");
    const inspector = rect("#monitorRail");
    const activePane = rect(`#pane-${paneContract.tab}`);
    const session = rect(paneContract.tab === "harness" ? ".harness-sidebar" : paneContract.tab === "hall" ? ".hall-sidebar" : "#does-not-exist");
    const expected = requestedTheme === "light"
      ? { canvas: "rgb(248, 249, 251)", surface: "rgb(255, 255, 255)", text: "rgb(27, 36, 48)", accent: "#4F5FE8" }
      : { canvas: "rgb(17, 19, 24)", surface: "rgb(26, 29, 36)", text: "rgb(243, 245, 247)", accent: "#93A0FF" };
    check("theme", document.body.dataset.theme === requestedTheme, document.body.dataset.theme);
    check("canvas-color", bodyStyle.backgroundColor === expected.canvas, `${bodyStyle.backgroundColor} != ${expected.canvas}`);
    check("text-color", bodyStyle.color === expected.text, `${bodyStyle.color} != ${expected.text}`);
    check("accent-color", bodyStyle.getPropertyValue("--accent").trim().toUpperCase() === expected.accent, bodyStyle.getPropertyValue("--accent"));
    check("one-active-tab", activeTabs.length === 1 && activeTabs[0]?.dataset.tab === paneContract.tab, activeTabs.map((node) => node.dataset.tab).join(","));
    check("one-active-pane", activePanes.length === 1 && activePanes[0]?.id === `pane-${paneContract.tab}`, activePanes.map((node) => node.id).join(","));
    check("six-tabs", document.querySelectorAll(".mode-tab[data-tab]").length === 6, document.querySelectorAll(".mode-tab[data-tab]").length);
    check("keyboard-selected", activeTabs[0]?.getAttribute("aria-selected") === "true", activeTabs[0]?.getAttribute("aria-selected"));
    check("no-horizontal-overflow", document.documentElement.scrollWidth <= innerWidth, `${document.documentElement.scrollWidth} > ${innerWidth}`);
    check("header-max-64", header && header.height > 0 && header.height <= 64, JSON.stringify(header));
    check("telemetry-closed", getComputedStyle(document.querySelector("#telemetryPopover")).display === "none" && document.querySelector("#telemetryPopoverBtn").getAttribute("aria-expanded") === "false", "default closed popover");
    const tools = document.querySelector("#studioTools");
    const monitorToggle = document.querySelector("#monitorToggleBtn");
    check("tools-closed-by-default", tools && !tools.open && tools.querySelector(":scope > summary"), "closed details with a keyboard-operable summary");
    check("inspector-toggle-collapsed", monitorToggle?.getAttribute("aria-expanded") === "false", monitorToggle?.getAttribute("aria-expanded"));
    check("inspector-closed-by-default", inspector && inspector.display === "none", JSON.stringify(inspector));
    if (width >= 1280) {
      const maxNavWidth = paneContract.tab === "harness" ? 240 : 224;
      check("desktop-nav-width", nav && nav.width >= 192 && nav.width <= maxNavWidth, JSON.stringify(nav));
    } else {
      check("mobile-tab-row", nav && nav.height > 0 && nav.height <= 64 && nav.y <= 64, JSON.stringify(nav));
    }
    if (width >= 1440) {
      check("task-min-640", activePane && activePane.width >= 640, JSON.stringify(activePane));
    } else {
      check("task-has-width", activePane && activePane.width > 0, JSON.stringify(activePane));
    }
    const required = {};
    for (const selector of paneContract.required) {
      required[selector] = rect(selector);
      check(`required:${selector}`, visible(selector), JSON.stringify(required[selector]));
    }
    const order = {};
    for (let index = 0; index < paneContract.order.length; index += 1) {
      const selector = paneContract.order[index];
      const element = document.querySelector(selector);
      order[selector] = element ? element.compareDocumentPosition(document.body) : null;
      check(`order-exists:${selector}`, Boolean(element), selector);
      if (index > 0 && element) {
        const previous = document.querySelector(paneContract.order[index - 1]);
        const relation = previous.compareDocumentPosition(element);
        check(`order:${paneContract.order[index - 1]}→${selector}`, Boolean(relation & Node.DOCUMENT_POSITION_FOLLOWING), relation);
      }
    }
    const counts = {};
    for (const [selector, expectedCount] of Object.entries(paneContract.counts)) {
      counts[selector] = document.querySelectorAll(selector).length;
      check(`count:${selector}`, counts[selector] === expectedCount, `${counts[selector]} != ${expectedCount}`);
    }
    let personaValues = null;
    let chatControls = null;
    if (paneContract.tab === "harness") {
      const stages = document.querySelector("#harnessStages");
      const settings = document.querySelector("#harnessTaskSettings");
      const sessionsToggle = document.querySelector("#harnessSessionsToggle");
      check("harness-empty-stages-collapsed", stages && getComputedStyle(stages).display === "none" && stages.querySelectorAll(".harness-stage").length === 3, JSON.stringify(rect(stages)));
      check("harness-settings-closed", settings && !settings.open, "advanced project and critic settings start closed");
      check("harness-empty-run-disabled", document.querySelector("#harnessRunBtn").disabled, "empty task cannot start a run");
      if (width >= 1280) {
        check("harness-shared-left-surface", session && nav && session.display !== "none" && Math.abs(session.x - nav.x) < 1 && Math.abs(session.width - nav.width) < 1 && session.y >= nav.y + nav.height - 1, JSON.stringify({ nav, session }));
      } else {
        check("harness-sessions-panel-closed", session && session.display === "none" && sessionsToggle?.getAttribute("aria-expanded") === "false", JSON.stringify({ session, expanded: sessionsToggle?.getAttribute("aria-expanded") }));
      }
    }
    if (paneContract.tab === "chat") {
      const values = [...document.querySelectorAll("#chatPersona option")].map((option) => option.value);
      const registry = [...CHAT_PERSONAS].map((persona) => persona.id);
      personaValues = values;
      check("chat-persona-registry", JSON.stringify(values) === JSON.stringify(registry) && values.length > 0, `${values.length} options / ${registry.length} registry`);
      check("chat-persona-count", document.querySelector("#chatPersonaCount").textContent.includes(String(registry.length)), document.querySelector("#chatPersonaCount").textContent);
      const selectors = [
        'label[for="chatModel"]', "#chatModel", 'label[for="chatPersona"]', "#chatPersona",
        "#chatPersonaSearch", "#chatPersonaDescription", 'label[for="chatReasoning"]', "#chatReasoning",
      ];
      chatControls = Object.fromEntries(selectors.map((selector) => [selector, rect(selector)]));
      const overlaps = [];
      for (let left = 0; left < selectors.length; left += 1) {
        for (let right = left + 1; right < selectors.length; right += 1) {
          const a = chatControls[selectors[left]];
          const b = chatControls[selectors[right]];
          if (!a || !b || a.display === "none" || b.display === "none") continue;
          const overlapWidth = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
          const overlapHeight = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
          if (overlapWidth > 0.5 && overlapHeight > 0.5) overlaps.push(`${selectors[left]} × ${selectors[right]}`);
        }
      }
      check("chat-controls-no-overlap", overlaps.length === 0, overlaps.join(", ") || "labels, selects, filter and description do not intersect");
      check("chat-persona-select-usable", chatControls["#chatPersona"].width >= 44, chatControls["#chatPersona"].width);
      check("chat-reasoning-select-usable", chatControls["#chatReasoning"].width >= 44, chatControls["#chatReasoning"].width);
    }
    if (paneContract.tab === "debate") {
      check("compare-two-models", document.querySelectorAll("#debateModelChecks input[type=checkbox]:checked").length >= 2, document.querySelectorAll("#debateModelChecks input[type=checkbox]:checked").length);
    }
    if (paneContract.tab === "start") {
      const task = document.querySelector("#startTaskBtn").getBoundingClientRect();
      check("start-primary-in-viewport", task.top >= 0 && task.bottom <= innerHeight && task.left >= 0 && task.right <= innerWidth, JSON.stringify(rect("#startTaskBtn")));
    }
    const tabs = [...document.querySelectorAll(".mode-tab[data-tab]")].map((element) => ({ id: element.dataset.tab, rect: rect(element) }));
    const signature = {
      viewport: viewportName, viewport_css: { width: innerWidth, height: innerHeight }, theme: document.body.dataset.theme,
      active_tab: activeTabs[0]?.dataset.tab, active_pane: activePanes[0]?.id,
      palette: { canvas: bodyStyle.backgroundColor, text: bodyStyle.color, accent: bodyStyle.getPropertyValue("--accent").trim().toUpperCase() },
      typography: { body: bodyStyle.fontFamily, size: bodyStyle.fontSize },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      regions: {
        header, nav, integrations: rect(".skills-strip"), tools: rect("#studioTools"), workspace: rect(".workspace"),
        pane: activePane, inspector, session,
      },
      secondary_state: {
        tools_open: tools.open, inspector_expanded: monitorToggle.getAttribute("aria-expanded"),
        harness_sessions_expanded: paneContract.tab === "harness" ? document.querySelector("#harnessSessionsToggle").getAttribute("aria-expanded") : null,
        harness_settings_open: paneContract.tab === "harness" ? document.querySelector("#harnessTaskSettings").open : null,
      },
      tabs, required, counts, order, persona_values: personaValues, chat_controls: chatControls,
    };
    return { checks, signature };
  }, pane, theme, viewport);
}

async function collectReference(page, pane, theme, viewport) {
  return page.evaluate((paneId, requestedTheme, viewportName) => {
    const checks = [];
    const check = (id, passed, detail) => checks.push({ id: `reference.${id}`, passed: Boolean(passed), detail: String(detail) });
    const round = (number) => Math.round(number * 100) / 100;
    const rect = (selector) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return { x: round(box.x), y: round(box.y), width: round(box.width), height: round(box.height), display: style.display, visibility: style.visibility };
    };
    const [width] = viewportName.split("x").map(Number);
    const bodyStyle = getComputedStyle(document.body);
    const rootStyle = getComputedStyle(document.documentElement);
    const expected = requestedTheme === "light"
      ? { canvas: "rgb(248, 249, 251)", text: "rgb(27, 36, 48)", accent: "#4F5FE8" }
      : { canvas: "rgb(17, 19, 24)", text: "rgb(243, 245, 247)", accent: "#93A0FF" };
    const header = rect("header");
    const nav = rect("#nav");
    const main = rect("#main");
    const inspector = rect("#inspector");
    const sessions = rect("#sessions");
    check("theme", document.documentElement.dataset.theme === requestedTheme, document.documentElement.dataset.theme);
    check("canvas-color", bodyStyle.backgroundColor === expected.canvas, bodyStyle.backgroundColor);
    check("text-color", bodyStyle.color === expected.text, bodyStyle.color);
    check("accent-color", rootStyle.getPropertyValue("--accent").trim().toUpperCase() === expected.accent, rootStyle.getPropertyValue("--accent"));
    check("no-horizontal-overflow", document.documentElement.scrollWidth <= innerWidth, `${document.documentElement.scrollWidth} > ${innerWidth}`);
    check("header-max-64", header && header.height > 0 && header.height <= 64, JSON.stringify(header));
    check("six-tabs", document.querySelectorAll("#nav .item").length === 6, document.querySelectorAll("#nav .item").length);
    check("one-active-tab", document.querySelectorAll("#nav .item.active").length === 1, document.querySelectorAll("#nav .item.active").length);
    check("telemetry-closed", getComputedStyle(document.querySelector("#telemetryPopover")).display === "none", "default closed popover");
    check("pane-present", document.querySelector("#paneBody").children.length > 0, document.querySelector("#paneBody").children.length);
    if (width >= 1280) check("desktop-nav-width", nav && nav.width >= 192 && nav.width <= 224, JSON.stringify(nav));
    else check("mobile-tab-row", nav && nav.height > 0 && nav.height <= 64, JSON.stringify(nav));
    if (width >= 1440) {
      check("desktop-inspector", inspector && inspector.display !== "none" && inspector.width >= 272 && inspector.width <= 320, JSON.stringify(inspector));
      check("task-min-640", main && main.width >= 640, JSON.stringify(main));
      check("sessions-visible", sessions && sessions.display !== "none" && sessions.width >= 200, JSON.stringify(sessions));
    } else {
      check("inspector-hidden", inspector && inspector.display === "none", JSON.stringify(inspector));
      check("task-has-width", main && main.width > 0, JSON.stringify(main));
      if (width >= 1280) check("sessions-visible", sessions && sessions.display !== "none" && sessions.width >= 200, JSON.stringify(sessions));
      else check("sessions-hidden", sessions && sessions.display === "none", JSON.stringify(sessions));
    }
    if (paneId === "harness") check("harness-three-stages", document.querySelectorAll("#paneBody .stage").length === 3, document.querySelectorAll("#paneBody .stage").length);
    if (paneId === "council") check("pipeline-four-stages", document.querySelectorAll("#paneBody .stage").length === 4, document.querySelectorAll("#paneBody .stage").length);
    if (paneId === "debate") check("compare-question-first", document.querySelector("#paneBody .composer").compareDocumentPosition(document.querySelector("#paneBody .card")) & Node.DOCUMENT_POSITION_FOLLOWING, "question before results");
    if (paneId === "chat") check("chat-persona-intent", document.querySelector("#paneBody").textContent.includes("persona picker lists every registered persona"), "registered persona intent");
    const signature = {
      viewport: viewportName, viewport_css: { width: innerWidth, height: innerHeight }, theme: document.documentElement.dataset.theme,
      active_pane: paneId,
      palette: { canvas: bodyStyle.backgroundColor, text: bodyStyle.color, accent: rootStyle.getPropertyValue("--accent").trim().toUpperCase() },
      typography: { body: bodyStyle.fontFamily, size: bodyStyle.fontSize },
      document: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      regions: { header, nav, main, sessions, inspector, pane_body: rect("#paneBody") },
      stages: document.querySelectorAll("#paneBody .stage").length,
    };
    return { checks, signature };
  }, pane, theme, viewport);
}

async function renderComparison(page, actualBytes, referenceBytes, width, height, entry) {
  const data = await page.evaluate(async ({ actual, reference, width: w, height: h, row }) => {
    const load = (source) => new Promise((resolveImage, reject) => {
      const image = new Image();
      image.onload = () => resolveImage(image);
      image.onerror = reject;
      image.src = source;
    });
    const [actualImage, referenceImage] = await Promise.all([load(actual), load(reference)]);
    const actualCanvas = document.createElement("canvas");
    const referenceCanvas = document.createElement("canvas");
    actualCanvas.width = referenceCanvas.width = w;
    actualCanvas.height = referenceCanvas.height = h;
    const actualContext = actualCanvas.getContext("2d", { willReadFrequently: true });
    const referenceContext = referenceCanvas.getContext("2d", { willReadFrequently: true });
    actualContext.drawImage(actualImage, 0, 0, w, h);
    referenceContext.drawImage(referenceImage, 0, 0, w, h);
    const a = actualContext.getImageData(0, 0, w, h).data;
    const b = referenceContext.getImageData(0, 0, w, h).data;
    let sum = 0;
    let changed = 0;
    for (let index = 0; index < a.length; index += 4) {
      const dr = Math.abs(a[index] - b[index]);
      const dg = Math.abs(a[index + 1] - b[index + 1]);
      const db = Math.abs(a[index + 2] - b[index + 2]);
      sum += dr + dg + db;
      if (Math.max(dr, dg, db) > 24) changed += 1;
    }
    const gap = 16;
    const top = 46;
    const bottom = 26 + row.differences.length * 42;
    const canvas = document.createElement("canvas");
    canvas.width = w * 2 + gap;
    canvas.height = h + top + bottom;
    const context = canvas.getContext("2d");
    context.fillStyle = "#f4f6fa";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#172033";
    context.font = "600 15px sans-serif";
    const wideLabels = w >= 700;
    context.fillText(`${wideLabels ? "APPROVED REFERENCE" : "REFERENCE"} · ${row.pane} · ${row.theme} · ${row.viewport}`, 12, 29);
    context.fillText(wideLabels
      ? "ACTUAL APP · independent structure checks + measured delta (no pixel tolerance)"
      : "ACTUAL · live DOM + measured Δ", w + gap + 12, 29);
    context.drawImage(referenceCanvas, 0, top);
    context.drawImage(actualCanvas, w + gap, top);
    const colors = ["#d13c8f", "#8a4bd1", "#d06a16"];
    row.differences.forEach((difference, index) => {
      const region = row.signature.regions[difference.anchor] || row.signature.regions.pane;
      if (!region || region.display === "none") return;
      const color = colors[index % colors.length];
      context.strokeStyle = color;
      context.lineWidth = 3;
      context.strokeRect(w + gap + region.x + 2, top + region.y + 2, Math.max(4, region.width - 4), Math.max(4, region.height - 4));
      context.fillStyle = color;
      context.fillRect(w + gap + region.x + 3, top + region.y + 3 + index * 20, 32, 18);
      context.fillStyle = "white";
      context.font = "bold 11px monospace";
      context.fillText(difference.id, w + gap + region.x + 9, top + region.y + 16 + index * 20);
    });
    let y = h + top + 22;
    context.font = "12px sans-serif";
    row.differences.forEach((difference, index) => {
      context.fillStyle = colors[index % colors.length];
      context.font = "bold 12px monospace";
      context.fillText(difference.id, 12, y);
      context.fillStyle = "#172033";
      context.font = "bold 12px sans-serif";
      context.fillText(difference.title, 46, y);
      context.font = "12px sans-serif";
      context.fillStyle = "#455168";
      context.fillText(difference.live, 46, y + 17, canvas.width - 58);
      y += 42;
    });
    return {
      png: canvas.toDataURL("image/png"),
      metrics: {
        mean_absolute_rgb_0_255: Math.round((sum / (w * h * 3)) * 10000) / 10000,
        changed_pixel_ratio_0_1: Math.round((changed / (w * h)) * 1000000) / 1000000,
        changed_channel_threshold: 24,
        policy: "informational only; structural checks and human review decide, never a blanket pixel tolerance",
      },
    };
  }, {
    actual: `data:image/png;base64,${Buffer.from(actualBytes).toString("base64")}`,
    reference: `data:image/png;base64,${Buffer.from(referenceBytes).toString("base64")}`,
    width, height, row: entry,
  });
  return { png: Buffer.from(data.png.split(",")[1], "base64"), metrics: data.metrics };
}

async function atomicWrite(path, bytes) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, bytes);
  await rename(temporary, path);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!existsSync(CONTRACT_PATH)) throw new Error("generate the cross-tab contract before running the walk");
  const contract = JSON.parse(await readFile(CONTRACT_PATH, "utf8"));
  if (contract.findings.length) throw new Error(`contract has ${contract.findings.length} findings`);
  const wireframe = await readFile(WIREFRAME_PATH, "utf8");
  const contractSha = contractFingerprint(contract);
  const wireframeSha = digest(Buffer.from(wireframe));
  const selection = selectedMatrix(contract, args);
  let approval = null;
  let approvedByKey = new Map();
  if (args.mode === "approved-check") {
    if (!existsSync(APPROVAL_PATH)) throw new Error("--check fails closed: no human-approved live structural baseline exists");
    approval = JSON.parse(await readFile(APPROVAL_PATH, "utf8"));
    if (approval.contract_sha256 !== contractSha || approval.wireframe_sha256 !== wireframeSha) throw new Error("approved baseline does not match the current contract or wireframe");
    if (approval.human_approval?.decision !== "approved" || !approval.human_approval?.reference?.startsWith("user:")) throw new Error("approved baseline lacks an explicit user approval reference");
    if (!Array.isArray(approval.captures) || approval.captures.length !== 48) throw new Error("approved baseline must contain 48 captures");
    approvedByKey = new Map(approval.captures.map((row) => [row.key, row]));
  }
  const out = resolve(ROOT, args.out);
  const manifestPath = join(out, args.mode === "candidate" ? "candidate-manifest.json" : "check-manifest.json");
  await rm(manifestPath, { force: true });
  const chrome = findChrome();
  const puppeteer = await loadPuppeteer();
  const demo = startFixture(args.timeoutMs);
  let browser;
  const failures = [];
  const captures = [];
  try {
    const demoUrl = await demo.ready;
    await waitForDemo(demoUrl, args.timeoutMs);
    const url = new URL(demoUrl);
    const token = url.searchParams.get("token");
    const base = `${url.origin}/`;
    const authorization = await fetch(`${base}api/workspace/directory`, {
      headers: { Authorization: `Bearer ${token}` }, redirect: "error", cache: "no-store",
    });
    if (authorization.status !== 200) throw new Error(`Bearer API authentication failed: ${authorization.status}`);
    const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean);
    browser = await puppeteer.launch({
      executablePath: chrome, headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", ...extraArgs],
    });
    const comparisonPage = await browser.newPage();
    await comparisonPage.setContent("<!doctype html><title>comparison renderer</title>");
    const referencePage = await browser.newPage();
    let currentTheme = "";
    let livePage;
    let pageErrors = [];
    let unexpectedNavigation = false;
    for (const item of selection.keys) {
      const key = `${item.theme}/${item.viewport}/${item.pane}`;
      const pane = contract.panes.find((row) => row.id === item.pane);
      if (!pane) throw new Error(`unknown pane ${item.pane}`);
      if (item.theme !== currentTheme) {
        if (livePage) await livePage.close();
        currentTheme = item.theme;
        livePage = await browser.newPage();
        livePage.on("pageerror", (error) => pageErrors.push(String(error)));
        livePage.on("dialog", async (dialog) => { unexpectedNavigation = true; await dialog.dismiss(); });
        livePage.on("framenavigated", (frame) => {
          if (frame === livePage.mainFrame() && frame.url() !== base && frame.url() !== demoUrl) unexpectedNavigation = true;
        });
        await livePage.setRequestInterception(true);
        livePage.on("request", (request) => {
          const requestUrl = request.url();
          if (requestUrl.startsWith(url.origin) || requestUrl.startsWith("data:") || requestUrl.startsWith("blob:")) void request.continue();
          else void request.abort("blockedbyclient");
        });
        await livePage.evaluateOnNewDocument((requestedTheme, origin, authToken) => {
          if (window.top !== window || location.origin !== origin) return;
          window.localStorage.setItem("m5_theme", requestedTheme);
          window.sessionStorage.setItem("m5_api_token", authToken);
          window.sessionStorage.setItem("m5_demo_auth_session", "1");
        }, item.theme, url.origin, token);
      }
      const [width, height] = item.viewport.split("x").map(Number);
      await livePage.setViewport({ width, height, deviceScaleFactor: 1 });
      const response = await livePage.goto(base, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      if (!response || response.status() !== 200 || response.request().redirectChain().length) throw new Error(`${key}: live navigation was not a direct 200`);
      await livePage.waitForFunction(() => document.querySelectorAll(".mode-tab[data-tab]").length === 6 && Boolean(window.switchTab), { timeout: args.timeoutMs });
      await livePage.evaluate((tab) => document.querySelector(`.mode-tab[data-tab="${tab}"]`).focus(), pane.tab);
      await livePage.keyboard.press("Enter");
      await sleep(1200);
      if (pane.tab === "hall") {
        // The seeded demo transitions from an unread/0-of-4 readiness summary to its factual
        // demo verdict after telemetry + provider reads. Capturing between those states moves
        // the transcript when the sentence wraps; wait for the one defined matrix state.
        await livePage.waitForFunction(
          () => document.querySelector("#councilReadinessSummary")?.textContent.includes("would receive demo text"),
          { timeout: args.timeoutMs },
        );
      }
      await livePage.evaluate(() => document.activeElement?.blur());
      await referencePage.setViewport({ width, height, deviceScaleFactor: 1 });
      await referencePage.setContent(wireframe, { waitUntil: "load" });
      await referencePage.evaluate((requestedTheme, paneId, names) => {
        document.documentElement.dataset.theme = requestedTheme;
        document.getElementById("paneBody").innerHTML = PANES[paneId] || PANES.harness;
        document.querySelectorAll("#nav .item").forEach((element, index) => element.classList.toggle("active", names[index] === paneId));
      }, item.theme, pane.tab, ["harness", "start", "hall", "council", "debate", "chat"]);
      await sleep(80);
      const live = await collectLive(livePage, pane, item.theme, item.viewport);
      const referenceResult = await collectReference(referencePage, pane.tab, item.theme, item.viewport);
      const checks = [...live.checks, ...referenceResult.checks];
      if (unexpectedNavigation || pageErrors.length || livePage.url() !== base) {
        checks.push({ id: "live.browser-safety", passed: false, detail: JSON.stringify({ unexpectedNavigation, pageErrors, url: livePage.url() }) });
      } else {
        checks.push({ id: "live.browser-safety", passed: true, detail: "direct document; no page error, dialog or unexpected navigation" });
      }
      const entry = {
        key, pane: item.pane, tab: pane.tab, theme: item.theme, viewport: item.viewport,
        signature: live.signature, reference_signature: referenceResult.signature,
        differences: pane.differences, checks,
      };
      if (args.mode === "approved-check") {
        const approved = approvedByKey.get(key);
        const liveDifferences = approved ? diffValues(approved.signature, live.signature) : ["capture missing from approval"];
        const referenceDifferences = approved ? diffValues(approved.reference_signature, referenceResult.signature) : ["capture missing from approval"];
        checks.push({ id: "approved.live-structural-signature", passed: liveDifferences.length === 0, detail: liveDifferences.join(" | ") || "exact match" });
        checks.push({ id: "approved.reference-structural-signature", passed: referenceDifferences.length === 0, detail: referenceDifferences.join(" | ") || "exact match" });
      }
      const actualBytes = await livePage.screenshot({ type: "png" });
      const referenceBytes = await referencePage.screenshot({ type: "png" });
      const comparison = await renderComparison(comparisonPage, actualBytes, referenceBytes, width, height, entry);
      entry.pixel_difference = comparison.metrics;
      entry.images = {};
      const imageDirectory = join(out, item.theme, item.viewport, item.pane);
      for (const [kind, bytes, file] of [
        ["actual", actualBytes, "actual.png"], ["reference", referenceBytes, "reference.png"], ["comparison", comparison.png, "comparison.png"],
      ]) {
        const path = join(imageDirectory, file);
        await atomicWrite(path, bytes);
        entry.images[kind] = { file: relative(out, path), sha256: digest(bytes), bytes: bytes.length };
      }
      const failed = checks.filter((check) => !check.passed);
      captures.push(entry);
      failures.push(...failed.map((check) => ({ key, ...check })));
      console.log(`${failed.length ? "FAIL" : "PASS"} ${key}: ${checks.length - failed.length}/${checks.length}; mean RGB Δ=${comparison.metrics.mean_absolute_rgb_0_255}; changed=${comparison.metrics.changed_pixel_ratio_0_1}`);
    }
    const checkTotal = captures.reduce((sum, row) => sum + row.checks.length, 0);
    const manifest = {
      schema_version: 1, document_type: "m5_uiv2_cross_tab_candidate", story: "UIV2-26", mode: args.mode,
      complete: selection.complete, generated_at: new Date().toISOString(), contract_sha256: contractSha,
      wireframe_sha256: wireframeSha, browser: { requested_build: "153.0.8010.0", executable: chrome, version: await browser.version() },
      demo: { script: "scripts/demo_tour.py --serve", isolated: true, base_path: "/", authentication: "session token + Bearer API", external_provider_calls: false },
      matrix: { ...contract.matrix, keys: captures.map((row) => row.key) },
      git: gitState(), approval: args.mode === "candidate" ? { status: "pending", inferred: false } : { status: "approved-check", reference: approval.human_approval.reference, candidate: approval.candidate },
      checks: { total: checkTotal, passed: checkTotal - failures.length, failed: failures.length },
      failures, captures,
    };
    await atomicWrite(manifestPath, jsonBytes(manifest));
    console.log(`${failures.length ? "FAIL" : "PASS"} ${manifestPath}: ${captures.length} captures, ${checkTotal - failures.length}/${checkTotal} checks`);
    if (failures.length) process.exitCode = 1;
  } catch (error) {
    console.error(error.stack || error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    await stopDemo(demo.child);
    await rm(demo.root, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
}
