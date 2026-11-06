#!/usr/bin/env node
/**
 * Check that the MCP surface is a hub you can look things up in, not one long configuration
 * scroll (UIV2-21).
 *
 *   node scripts/mcp_hub_walk.mjs --screenshots docs/media/mcp-hub
 *
 * The dialog could already connect a remote server, a local command and a pasted JSON config.
 * What it could not do was let anyone *find* one: presets, the whole add/edit form, the importer,
 * every configured server with its tool list, and a playground all lived in a single column, so
 * the first thing on screen was configuration and the last thing was the tools you had connected.
 * A preset was a chip that said only its name - not whether it was local or remote, not whether it
 * needed a key, not what approval policy it would run under.
 *
 * What it proves:
 *
 *  1. the dialog is a hub: a three-tab tablist (Catalog / Connected / Activity) with counts, and
 *     only one section on screen at a time - the configuration form is *not* visible while you
 *     browse the catalog, and at desktop sizes the catalog needs no dialog scroll at all;
 *  2. every preset is a card that states its transport (remote HTTP / local stdio), its trust level,
 *     whether it needs a key, and its approval policy, plus the target it would connect to;
 *  3. search narrows the catalog to matching cards, a filter chip counts what it would show, and a
 *     search with no hit says so and offers to clear itself;
 *  4. "Use preset" prefills the editor, opens it, and lands on the Connected tab;
 *  5. a server that cannot be reached is still added, with the sync note it really got, and the
 *     Connected row and the Activity row both say so - through a reload;
 *  6. a live local stdio server syncs its real tools (the walk's own fixture MCP server);
 *  7. a stored token is never rendered: only the masked badge, and the secret string appears
 *     nowhere in the document;
 *  8. a test call is recorded in Activity with its latency and outcome, a failing call is recorded
 *     as failed with its message, a slow call shows a real loading state, and long output scrolls
 *     inside the output box;
 *  9. tab counts match the state, arrow keys move between tabs with aria-selected following;
 * 10. the tab strip stays on screen and the panel does not overflow horizontally at this viewport;
 * 11. no console error, and the hub posts only MCP endpoints.
 *
 * Usage:
 *
 *   node scripts/mcp_hub_walk.mjs                                    # starts its own fixture
 *   node scripts/mcp_hub_walk.mjs --base-url URL --token TOKEN       # use a running app
 *   node scripts/mcp_hub_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/mcp_hub_walk.mjs --states catalog,connected         # a subset
 *   node scripts/mcp_hub_walk.mjs --capture-only                     # write a "before" set
 */


import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const FIXTURE_SERVER = resolve("scripts/mcp_hub_fixture_server.py");
/** A token that must never reach the DOM, only its mask. */
const SECRET = "sk-uiv2-21-do-not-render-9f3c";
/** A port nothing listens on, so a "remote" server fails fast and for a real reason.
 *  HTTPS because the app refuses plain HTTP to anything but an opted-in private network. */
const DEAD_URL = "https://127.0.0.1:9/mcp";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
  viewport: "1440x900",
  theme: "dark",
  states: "",
};

const STATES = ["catalog", "connected", "secrets", "activity", "loading", "long", "error"];

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false, captureOnly: false };
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
      case "--screenshots": args.screenshots = next(); break;
      case "--viewport": args.viewport = next(); break;
      case "--theme": args.theme = next(); break;
      case "--json": args.json = true; break;
      case "--capture-only": args.captureOnly = true; break;
      case "--states": args.states = next(); break;
      case "--help":
      case "-h":
        console.log("usage: node scripts/mcp_hub_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states catalog,connected,secrets,activity,loading,long,error] [--json] [--capture-only]");
        process.exit(0);
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

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
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) throw new Error("no Chromium found: set STUDIO_SCREENSHOT_CHROME to one");
  return found;
}

async function loadPuppeteer() {
  const attempts = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    "puppeteer",
    resolve("tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const failures = [];
  for (const attempt of attempts) {
    try {
      const module = await import(attempt);
      return module.default || module;
    } catch (error) {
      failures.push(`${attempt}: ${error.message}`);
    }
  }
  throw new Error(`puppeteer-core is not importable. Install it or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (error) { /* not up yet */ }
    await sleep(150);
  }
  return false;
}

/**
 * The fixture app runs with two operator opt-ins the story needs, and no app change:
 *
 *  * `STUDIO_ALLOW_MCP_STDIO=1`, because the app refuses to spawn a stdio MCP command unless the
 *    operator has said so - the walk connects its own fixture MCP server, nothing else;
 *  * a memory keyring backend (through a `sitecustomize` on the fixture's `PYTHONPATH`), because a
 *    headless container has no OS credential store, and the app correctly refuses to write a
 *    secret into SQLite instead. This is the same stand-in the app's own tests use.
 */
function writeFixtureSupport() {
  const dir = mkdtempSync(join(tmpdir(), "mcp-hub-fixture-"));
  writeFileSync(join(dir, "sitecustomize.py"), [
    "import keyring",
    "_MEMORY = {}",
    "def _set(service, item, value):",
    "    _MEMORY[(service, item)] = value",
    "def _get(service, item):",
    "    return _MEMORY.get((service, item))",
    "def _delete(service, item):",
    "    _MEMORY.pop((service, item), None)",
    "keyring.set_password = _set",
    "keyring.get_password = _get",
    "keyring.delete_password = _delete",
    "",
  ].join("\n"));
  return dir;
}

async function startFixture(extraArgs = []) {
  const support = writeFixtureSupport();
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", "Qwen3.6-35B-REAP-19B", "--port", "0", ...extraArgs],
    {
      cwd: process.cwd(),
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        STUDIO_ALLOW_MCP_STDIO: "1",
        PYTHONPATH: [support, process.env.PYTHONPATH].filter(Boolean).join(":"),
      },
    },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)), 90000);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        resolveReady(JSON.parse(line));
      } catch (error) {
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { child, ready };
}

/** Everything the hub is claiming, read back from the DOM. */
function readHub(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const tabs = [...document.querySelectorAll("#mcpModal [data-mcp-tab]")].map((tab) => ({
      id: tab.dataset.mcpTab,
      label: clean(tab.textContent),
      selected: tab.getAttribute("aria-selected") === "true",
      tabIndex: tab.tabIndex,
      active: tab.classList.contains("active"),
      visible: tab.offsetParent !== null,
    }));
    const panels = [...document.querySelectorAll("#mcpModal .mcp-hub-panel")].map((panel) => ({
      id: panel.id,
      hidden: panel.hidden === true,
      role: panel.getAttribute("role") || "",
      labelledBy: panel.getAttribute("aria-labelledby") || "",
    }));
    const cards = [...document.querySelectorAll("#mcpCatalogGrid .mcp-card")].map((card) => ({
      alias: card.dataset.mcpPreset || "",
      name: clean((card.querySelector(".mcp-card-name") || {}).textContent || ""),
      pills: [...card.querySelectorAll(".mcp-pill")].map((pill) => clean(pill.textContent)),
      notes: clean((card.querySelector(".mcp-card-notes") || {}).textContent || ""),
      target: clean((card.querySelector(".mcp-card-target") || {}).textContent || ""),
      state: clean((card.querySelector(".mcp-card-state") || {}).textContent || ""),
      action: clean((card.querySelector("button") || {}).textContent || ""),
      visible: card.offsetParent !== null,
    }));
    const chips = [...document.querySelectorAll("#mcpCatalogFilters .chip")].map((chip) => ({
      label: clean(chip.textContent),
      pressed: chip.getAttribute("aria-pressed") === "true",
    }));
    const connected = [...document.querySelectorAll("#configuredMcpServersList [data-mcp-server]")].map((row) => ({
      alias: row.dataset.mcpServer || "",
      text: clean(row.textContent).slice(0, 500),
      tools: row.querySelectorAll("details input[type=checkbox]").length,
      hasSyncNote: /Sync note:/.test(row.textContent || ""),
    }));
    const activity = [...document.querySelectorAll("#mcpActivityList .mcp-activity-row")].map((row) => ({
      title: clean((row.querySelector(".mcp-activity-title") || {}).textContent || ""),
      meta: clean((row.querySelector(".mcp-activity-meta") || {}).textContent || ""),
      err: clean((row.querySelector(".mcp-activity-err, .mcp-activity-detail") || {}).textContent || ""),
      state: clean((row.querySelector(".mcp-pill") || {}).textContent || ""),
    }));
    const calls = [...document.querySelectorAll("#mcpActivityCalls .mcp-activity-row")].map((row) => ({
      title: clean((row.querySelector(".mcp-activity-title") || {}).textContent || ""),
      meta: clean((row.querySelector(".mcp-activity-meta") || {}).textContent || ""),
      err: clean((row.querySelector(".mcp-activity-err, .mcp-activity-detail") || {}).textContent || ""),
      state: clean((row.querySelector(".mcp-pill") || {}).textContent || ""),
    }));
    const body = document.querySelector("#mcpModal .mcp-hub-body");
    const dialog = document.querySelector("#mcpModal .modal");
    const tabbar = document.querySelector("#mcpModal .mcp-hub-tabbar");
    const editor = document.getElementById("mcpEditor");
    const output = document.getElementById("mcpPlayOutput");
    return {
      open: document.getElementById("mcpModal").classList.contains("open"),
      dialog: {
        role: (dialog || {}).getAttribute ? dialog.getAttribute("role") : "",
        modal: (dialog || {}).getAttribute ? dialog.getAttribute("aria-modal") : "",
        label: (dialog || {}).getAttribute ? (dialog.getAttribute("aria-labelledby") || "") : "",
        width: dialog ? Math.round(dialog.getBoundingClientRect().width) : 0,
        height: dialog ? Math.round(dialog.getBoundingClientRect().height) : 0,
      },
      summary: clean((document.getElementById("mcpHubSummary") || {}).textContent || ""),
      searchLabel: (document.getElementById("mcpHubSearch") || {}).getAttribute
        ? document.getElementById("mcpHubSearch").getAttribute("aria-label") || ""
        : "",
      tabs,
      panels,
      cards,
      chips,
      connected,
      activity,
      calls,
      editorOpen: Boolean(editor && editor.open),
      editorState: clean((document.getElementById("mcpEditorState") || {}).textContent || ""),
      formVisible: Boolean(document.getElementById("mcpAlias"))
        && document.getElementById("mcpAlias").offsetParent !== null,
      counts: {
        catalog: clean((document.getElementById("mcpTabCatalogCount") || {}).textContent || ""),
        connected: clean((document.getElementById("mcpTabConnectedCount") || {}).textContent || ""),
        activity: clean((document.getElementById("mcpTabActivityCount") || {}).textContent || ""),
      },
      empty: {
        catalogHidden: Boolean(document.getElementById("mcpCatalogEmpty") && document.getElementById("mcpCatalogEmpty").hidden),
        catalogText: clean((document.getElementById("mcpCatalogEmpty") || {}).textContent || ""),
        activityHidden: Boolean(document.getElementById("mcpActivityEmpty") && document.getElementById("mcpActivityEmpty").hidden),
      },
      playground: {
        output: clean((output || {}).textContent || "").slice(0, 300),
        outputVisible: Boolean(output) && output.offsetParent !== null && getComputedStyle(output).display !== "none",
        outputScrolls: Boolean(output) && output.scrollHeight > output.clientHeight + 8,
        buttonLabel: clean((document.getElementById("mcpPlayBtn") || {}).textContent || ""),
        buttonDisabled: Boolean(document.getElementById("mcpPlayBtn") && document.getElementById("mcpPlayBtn").disabled),
        servers: [...document.querySelectorAll("#mcpPlayServer option")].map((option) => option.value),
        tools: [...document.querySelectorAll("#mcpPlayTool option")].map((option) => option.value),
      },
      body: body ? {
        scrollHeight: body.scrollHeight,
        clientHeight: body.clientHeight,
        scrollTop: body.scrollTop,
        overflows: body.scrollHeight > body.clientHeight + 4,
      } : null,
      tabbar: tabbar ? {
        top: Math.round(tabbar.getBoundingClientRect().top),
        bottom: Math.round(tabbar.getBoundingClientRect().bottom),
        inViewport: tabbar.getBoundingClientRect().top >= 0
          && tabbar.getBoundingClientRect().bottom <= window.innerHeight,
      } : null,
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      focused: document.activeElement ? (document.activeElement.dataset.mcpTab || document.activeElement.id || "") : "",
      secretInDom: document.documentElement.innerHTML.includes("sk-uiv2-21-do-not-render-9f3c"),
      innerWidth: window.innerWidth,
    };
  });
}

const clickText = (page, selector, text) => page.evaluate((sel, wanted) => {
  const node = [...document.querySelectorAll(sel)].find((item) => (item.textContent || "").trim().startsWith(wanted));
  if (!node) return false;
  node.click();
  return true;
}, selector, text);

const fill = (page, id, value) => page.evaluate((fieldId, val) => {
  const field = document.getElementById(fieldId);
  if (!field) return false;
  field.value = val;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}, id, value);

const switchTab = (page, tab) => page.evaluate(
  (name) => (typeof switchMcpTab === "function" ? switchMcpTab(name) : null),
  tab,
);

const openHub = async (page) => {
  await page.evaluate(() => openMcpModal());
  await page.waitForFunction(
    () => document.getElementById("mcpModal").classList.contains("open"),
    { timeout: 30000 },
  );
  // A "before" capture runs against the pre-hub dialog, which has no catalog grid; wait for cards
  // only when the hub is actually there, so the same instrument can photograph both.
  await page.waitForFunction(
    () => !document.getElementById("mcpCatalogGrid")
      || document.querySelectorAll("#mcpCatalogGrid .mcp-card").length > 0,
    { timeout: 30000 },
  ).catch(() => {});
  await sleep(400);
};

/** Add a server through the real form, and wait for its row. */
const addServer = async (page, config) => {
  await switchTab(page, "connected");
  await page.evaluate(() => { const editor = document.getElementById("mcpEditor"); if (editor) editor.open = true; });
  await fill(page, "mcpAlias", config.alias);
  await fill(page, "mcpName", config.name || config.alias);
  await fill(page, "mcpTransport", config.transport || "http");
  await fill(page, "mcpApprovalPolicy", config.policy || "inherit");
  if ((config.transport || "http") === "http") {
    await fill(page, "mcpAuthHeader", config.authHeader || "none");
    await fill(page, "mcpUrl", config.url || "");
    await fill(page, "mcpToken", config.token || "");
  } else {
    await fill(page, "mcpCommand", config.command || "");
    await fill(page, "mcpArgs", config.args || "");
    await fill(page, "mcpEnvJson", "");
  }
  await clickText(page, "#mcpSaveBtn", "Save");
  await page.waitForFunction(
    (alias) => {
      // The pre-hub dialog has no data-mcp-server attribute, so a "before" capture matches the row
      // by its text instead; the hub rows carry both.
      const list = document.getElementById("configuredMcpServersList");
      const row = document.querySelector(`#configuredMcpServersList [data-mcp-server="${alias}"]`);
      // Pre-hub rows print the display name only; the alias lives in their onclick handlers.
      const byText = Boolean(list) && (list.innerHTML || "").includes(alias);
      return Boolean(row || byText)
        && document.getElementById("mcpSaveBtn").textContent.trim() === "Save & Sync Tools";
    },
    { timeout: 120000 },
    config.alias,
  );
  await sleep(250);
};

const runTool = async (page, alias, tool, args) => {
  await switchTab(page, "connected");
  await page.evaluate((serverAlias, toolName, rawArgs) => {
    const server = document.getElementById("mcpPlayServer");
    server.value = serverAlias;
    server.dispatchEvent(new Event("change", { bubbles: true }));
    const picker = document.getElementById("mcpPlayTool");
    picker.value = toolName;
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    document.getElementById("mcpPlayArgs").value = rawArgs;
  }, alias, tool, args || "{}");
  await page.evaluate(() => document.getElementById("mcpPlayBtn").click());
};

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const [width, height] = args.viewport.split("x").map(Number);
  const wide = width >= 1280;
  const wanted = args.states
    ? new Set(args.states.split(",").map((name) => name.trim()).filter(Boolean))
    : null;
  const state = (name) => !wanted || wanted.has(name);
  if (wanted) {
    const unknown = [...wanted].filter((name) => !STATES.includes(name));
    if (unknown.length) {
      console.error(`unknown state(s): ${unknown.join(", ")} — this walk knows ${STATES.join(", ")}`);
      process.exit(2);
    }
  }

  const shots = [];
  const shoot = async (page, name) => {
    if (!args.screenshots) return "";
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    await sleep(args.settleMs * 2);
    await page.screenshot({ path });
    shots.push(path);
    return path;
  };

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" ").filter(Boolean),
  });
  const pageErrors = [];
  const posts = [];
  const fixtures = [];

  try {
    const fixture = args.baseUrl ? null : await startFixture([]);
    if (fixture) fixtures.push(fixture);
    const base = args.baseUrl || fixture.ready.base_url;
    const token = args.token || (fixture && fixture.ready.token) || "";
    const origin = new URL(base).origin;

    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 40[34]|status of 50[0-9]|ERR_CONNECTION_REFUSED|Failed to load resource/.test(message.text())) {
        pageErrors.push(message.text());
      }
    });
    page.on("request", (request) => {
      if (request.method() === "POST" || request.method() === "DELETE") posts.push(new URL(request.url()).pathname);
    });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await page.evaluateOnNewDocument((sessionToken, key, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, sessionToken);
      localStorage.setItem("m5_theme", theme);
    }, token, args.tokenKey, args.theme, origin);
    await page.goto(origin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 8);

    // ── 1: the hub is three sections, and the catalog is what you land on ────
    if (state("catalog")) {
      await openHub(page);
      const hub = await readHub(page);
      record(
        "the MCP dialog is a hub: a three-tab tablist, only one section shown",
        hub.tabs.length === 3
          && hub.tabs.map((tab) => tab.id).join(",") === "catalog,connected,activity"
          && hub.tabs.filter((tab) => tab.selected).length === 1
          && (hub.tabs.find((tab) => tab.selected) || {}).id === "catalog"
          && hub.panels.filter((panel) => !panel.hidden).length === 1
          && (hub.panels.find((panel) => !panel.hidden) || {}).id === "mcpPanelCatalog",
        `${hub.tabs.map((tab) => `${tab.label}${tab.selected ? "*" : ""}`).join(" · ")} · ${hub.panels.filter((panel) => !panel.hidden).length} panel(s) shown`,
      );
      record(
        "the dialog is a labelled modal, and the search box says what it searches",
        hub.dialog.role === "dialog" && hub.dialog.modal === "true" && hub.dialog.label === "mcpHubTitle"
          && /presets, connected servers and tools/i.test(hub.searchLabel),
        `role=${hub.dialog.role} aria-modal=${hub.dialog.modal} labelledby=${hub.dialog.label} · "${hub.searchLabel}"`,
      );
      record(
        "browsing the catalog does not put the configuration form on screen",
        hub.formVisible === false,
        `form visible: ${hub.formVisible}`,
      );
      record(
        "every preset card states transport, trust, key requirement and policy",
        hub.cards.length >= 8 && hub.cards.every((card) => card.pills.length >= 3
          && card.pills.some((pill) => /^(Remote · HTTP|Local · stdio)$/.test(pill))
          && card.pills.some((pill) => /^(Trusted — auto-run|Gated — asks first|Inherits harness policy|Needs a key)$/.test(pill))
          && card.pills.some((pill) => /^(No key needed|Key: .+)$/.test(pill))
          && card.pills.some((pill) => /^Policy · /.test(pill))),
        `${hub.cards.length} card(s) · first: ${hub.cards[0] ? hub.cards[0].pills.join(" | ") : "none"}`,
      );
      record(
        "and each names the endpoint or command it would connect to",
        hub.cards.length > 0 && hub.cards.every((card) => card.target.length > 0),
        hub.cards[0] ? hub.cards[0].target : "no card",
      );
      record(
        "filter chips count what they would show",
        hub.chips.length === 5
          && hub.chips[0].pressed === true
          && hub.chips.every((chip) => /\d+$/.test(chip.label)),
        hub.chips.map((chip) => chip.label).join(" · "),
      );
      record(
        "the tab bar stays on screen without scrolling the dialog",
        hub.tabbar !== null && hub.tabbar.inViewport === true,
        hub.tabbar ? `tab bar ${hub.tabbar.top}–${hub.tabbar.bottom}px of ${height}` : "no tab bar",
      );
      if (wide) {
        const firstScreen = await page.evaluate(() => {
          const host = document.querySelector("#mcpModal .mcp-hub-body");
          if (!host) return { cards: 0, fullyVisible: 0 };
          const box = host.getBoundingClientRect();
          const cards = [...document.querySelectorAll("#mcpCatalogGrid .mcp-card")];
          const fully = cards.filter((card) => {
            const rect = card.getBoundingClientRect();
            return rect.top >= box.top && rect.bottom <= Math.min(box.bottom, window.innerHeight);
          });
          return { cards: cards.length, fullyVisible: fully.length };
        });
        record(
          "at desktop sizes the first screen is a row of cards, not a configuration form",
          firstScreen.fullyVisible >= 6 && hub.formVisible === false,
          `${firstScreen.fullyVisible} of ${firstScreen.cards} cards fully visible without scrolling · form on screen: ${hub.formVisible}`,
        );
      }
      record(
        "the panel does not overflow horizontally",
        hub.horizontalOverflow === false,
        `document ${hub.innerWidth}px wide, overflow: ${hub.horizontalOverflow}`,
      );
      await shoot(page, `mcp-hub-catalog-${args.viewport}-${args.theme}`);
    }

    // ── 2: search and filter ────────────────────────────────────────────────
    if (state("catalog")) {
      await fill(page, "mcpHubSearch", "exa");
      await sleep(200);
      const searched = await readHub(page);
      record(
        "search narrows the catalog to matching cards",
        searched.cards.length > 0
          && searched.cards.length < 12
          && searched.cards.every((card) => /exa/i.test(`${card.name} ${card.target} ${card.notes}`)),
        `${searched.cards.length} card(s): ${searched.cards.map((card) => card.alias).join(", ")}`,
      );
      await shoot(page, `mcp-hub-search-${args.viewport}-${args.theme}`);
      await fill(page, "mcpHubSearch", "zzz-no-such-server");
      await sleep(200);
      const none = await readHub(page);
      record(
        "a search with no hit says so and offers to clear itself",
        none.cards.length === 0 && none.empty.catalogHidden === false && /No preset matches/i.test(none.empty.catalogText),
        `"${none.empty.catalogText.slice(0, 80)}"`,
      );
      await page.evaluate(() => { if (typeof resetMcpHubSearch === "function") resetMcpHubSearch(); });
      await sleep(150);
      const reset = await readHub(page);
      record(
        "clearing the search puts the whole catalog back",
        reset.cards.length >= 8,
        `${reset.cards.length} card(s)`,
      );
      await clickText(page, "#mcpCatalogFilters .chip", "Local");
      await sleep(150);
      const local = await readHub(page);
      record(
        "the Local filter shows only local templates",
        local.cards.length > 0
          && local.cards.every((card) => card.pills.some((pill) => pill === "Local · stdio")),
        `${local.cards.length} local card(s): ${local.cards.map((card) => card.alias).join(", ")}`,
      );
      await page.evaluate(() => { if (typeof setMcpCatalogFilter === "function") setMcpCatalogFilter("all"); });
      await sleep(150);
    }

    // ── 3: a card prefills the editor and lands on Connected ────────────────
    if (state("connected")) {
      // nothing has been opened yet: Connected must land on the list, not a wall of fields
      await switchTab(page, "connected");
      await sleep(250);
      const landed = await page.evaluate(() => {
        const list = document.getElementById("configuredMcpServersList");
        const rect = list.getBoundingClientRect();
        return {
          editorOpen: Boolean(document.getElementById("mcpEditor") && document.getElementById("mcpEditor").open),
          listTop: Math.round(rect.top),
          listInFirstScreen: Boolean(list.offsetParent) && rect.top < window.innerHeight,
          fieldsInFirstScreen: (() => {
            const field = document.getElementById("mcpAlias");
            if (!field) return false;
            if (field.closest("details:not([open])")) return false;
            const rect = field.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight;
          })(),
          viewport: window.innerHeight,
        };
      });
      record(
        "the Connected tab opens on the server list, with the editor shut",
        landed.editorOpen === false && landed.listInFirstScreen === true && landed.fieldsInFirstScreen === false,
        `editorOpen=${landed.editorOpen} list at ${landed.listTop}px, form fields on screen: ${landed.fieldsInFirstScreen}`,
      );

      await switchTab(page, "catalog");
      await page.evaluate(() => {
        const button = [...document.querySelectorAll("#mcpCatalogGrid .mcp-card button")]
          .find((node) => (node.textContent || "").trim() === "Use preset");
        if (button) button.click();
      });
      await sleep(300);
      const afterUse = await readHub(page);
      const alias = await page.evaluate(() => document.getElementById("mcpAlias").value);
      record(
        "\"Use preset\" prefills the editor and opens it on the Connected tab",
        (afterUse.tabs.find((tab) => tab.selected) || {}).id === "connected"
          && afterUse.editorOpen === true
          && alias.length > 0,
        `tab=${(afterUse.tabs.find((tab) => tab.selected) || {}).id} editorOpen=${afterUse.editorOpen} alias=${alias}`,
      );
      record(
        "and the editor says where the values came from",
        /Prefilled from the .+ card/i.test(afterUse.editorState),
        `"${afterUse.editorState.slice(0, 100)}"`,
      );
      await fill(page, "mcpAlias", "hand-typed-server");
      await sleep(150);
      const retitled = await readHub(page);
      record(
        "typing your own alias retitles the editor, instead of still claiming a preset",
        /New server/i.test(retitled.editorState) && !/Prefilled from/i.test(retitled.editorState),
        `"${retitled.editorState.slice(0, 100)}"`,
      );

      // a remote server that cannot be reached: a real sync note, in both tabs
      await addServer(page, { alias: "dead-remote", name: "Dead remote", transport: "http", url: DEAD_URL, policy: "always_ask" });
      const errored = await readHub(page);
      const deadRow = errored.connected.find((row) => row.alias === "dead-remote");
      record(
        "a server that cannot be reached is added, with the sync note it really got",
        Boolean(deadRow) && deadRow.hasSyncNote === true,
        deadRow ? deadRow.text.slice(0, 160) : "no row",
      );
      record(
        "its Connected row states transport, policy and tool count",
        Boolean(deadRow) && /HTTP/.test(deadRow.text) && /Always Ask|Always ask/i.test(deadRow.text) && /0 tools synced|0 tools/i.test(deadRow.text),
        deadRow ? deadRow.text.slice(0, 200) : "no row",
      );

      // refresh, and the note is still there - the error is the stored truth, not a toast
      await page.evaluate(() => loadMcpServers());
      await sleep(600);
      const reloaded = await readHub(page);
      record(
        "the error survives a reload, because it is stored rather than announced",
        reloaded.connected.some((row) => row.alias === "dead-remote" && row.hasSyncNote),
        `${reloaded.connected.length} row(s) after reload`,
      );
      const health = reloaded.activity.find((row) => /dead-remote/.test(row.title));
      record(
        "Activity reports the same failure for that server",
        Boolean(health) && /Sync note/.test(`${health.err} ${health.meta}`),
        health ? `${health.state} · ${health.err.slice(0, 100)}` : "no health row",
      );
      await shoot(page, `mcp-hub-connected-error-${args.viewport}-${args.theme}`);

      // a live local stdio server, syncing real tools
      await addServer(page, {
        alias: "local-notes",
        name: "Local notes fixture",
        transport: "stdio",
        command: process.env.STUDIO_PYTHON || "python3",
        args: FIXTURE_SERVER,
        policy: "auto",
      });
      const synced = await readHub(page);
      const goodRow = synced.connected.find((row) => row.alias === "local-notes");
      record(
        "a local stdio server syncs its real tools",
        Boolean(goodRow) && /Discovered Tools \(4 \/ 4 active/.test(goodRow.text) && !goodRow.hasSyncNote && goodRow.tools === 4,
        goodRow ? `${goodRow.text.slice(0, 160)} · ${goodRow.tools} tool checkbox(es)` : "no row",
      );
      const offered = await page.evaluate(() => {
        if (typeof switchMcpTab === "function") switchMcpTab("connected");
        const server = document.getElementById("mcpPlayServer");
        server.value = "local-notes";
        server.dispatchEvent(new Event("change", { bubbles: true }));
        return {
          servers: [...server.options].map((option) => option.value),
          tools: [...document.getElementById("mcpPlayTool").options].map((option) => option.value),
        };
      });
      record(
        "the playground offers that server and its tools",
        offered.servers.includes("local-notes") && offered.tools.length >= 4,
        `servers: ${offered.servers.join(", ")} · tools: ${offered.tools.join(", ")}`,
      );
      await shoot(page, `mcp-hub-connected-${args.viewport}-${args.theme}`);
    }

    // ── 4: no exposed secrets ───────────────────────────────────────────────
    if (state("secrets")) {
      await addServer(page, {
        alias: "keyed-remote",
        name: "Keyed remote",
        transport: "http",
        url: DEAD_URL,
        authHeader: "Authorization",
        token: SECRET,
        policy: "always_ask",
      });
      const keyed = await readHub(page);
      const row = keyed.connected.find((item) => item.alias === "keyed-remote");
      record(
        "a stored token is rendered only as its mask",
        Boolean(row) && /Token \S*•{2,}\S*/.test(row.text) && keyed.secretInDom === false,
        row ? `${(row.text.match(/Token [^\s]+/) || ["no badge"])[0]} · secret in DOM: ${keyed.secretInDom}` : "no row",
      );
      record(
        "the token field is emptied after saving",
        await page.evaluate(() => document.getElementById("mcpToken").value === ""),
        `field value: ${JSON.stringify(await page.evaluate(() => document.getElementById("mcpToken").value))}`,
      );
      const keyedHealth = keyed.activity.find((item) => /keyed-remote/.test(item.title));
      record(
        "Activity shows the mask, never the secret",
        Boolean(keyedHealth) && /Token \S*•{2,}/.test(keyedHealth.meta) && !/do-not-render/.test(keyedHealth.meta),
        keyedHealth ? keyedHealth.meta : "no health row",
      );
      await shoot(page, `mcp-hub-secrets-${args.viewport}-${args.theme}`);
    }

    // ── 5: a real test call, a real failure, a real loading state ───────────
    if (state("activity")) {
      await runTool(page, "local-notes", "echo_note", '{"note":"uiv2-21"}');
      await page.waitForFunction(
        () => (document.getElementById("mcpPlayOutput").textContent || "").includes("echo:"),
        { timeout: 60000 },
      );
      await sleep(300);
      const called = await readHub(page);
      const okCall = called.calls.find((call) => /echo_note/.test(call.title));
      record(
        "a test call is recorded in Activity with its outcome and latency",
        Boolean(okCall) && okCall.state === "ok" && /\d+ ms/.test(okCall.meta),
        okCall ? `${okCall.state} · ${okCall.meta}` : "no call row",
      );
      record(
        "and the playground still prints the call result",
        called.playground.outputVisible === true && /echo: uiv2-21/.test(called.playground.output),
        called.playground.output.slice(0, 100),
      );
      await shoot(page, `mcp-hub-activity-${args.viewport}-${args.theme}`);

      await runTool(page, "local-notes", "fail_always", "{}");
      await page.waitForFunction(
        () => /isError|fail_always|the fixture tool was asked to fail/.test(document.getElementById("mcpPlayOutput").textContent || "")
          || document.getElementById("mcpPlayBtn").textContent.trim() === "Test Call",
        { timeout: 60000 },
      );
      await sleep(400);
      const failed = await readHub(page);
      const badCall = failed.calls.find((call) => /fail_always/.test(call.title));
      record(
        "a failing tool call is recorded as failed, with what it said",
        Boolean(badCall) && badCall.state === "failed" && badCall.err.length > 0,
        badCall ? `${badCall.state} · "${badCall.err.slice(0, 80)}"` : `no call row · output: ${failed.playground.output.slice(0, 80)}`,
      );
      record(
        "tab counts follow the state",
        failed.counts.catalog === String(failed.cards.length >= 0 ? await page.evaluate(() => Object.keys(mcpPresets).length) : 0)
          && failed.counts.connected === String(failed.connected.length)
          && Number(failed.counts.activity) === failed.activity.length + failed.calls.length,
        `catalog=${failed.counts.catalog} connected=${failed.counts.connected} activity=${failed.counts.activity} (${failed.activity.length}+${failed.calls.length})`,
      );
      await shoot(page, `mcp-hub-activity-failed-${args.viewport}-${args.theme}`);
    }

    if (state("loading")) {
      await switchTab(page, "connected");
      await runTool(page, "local-notes", "slow_echo", '{"note":"slow"}');
      await sleep(250);
      const loading = await readHub(page);
      record(
        "a slow call shows a real loading state instead of a frozen button",
        loading.playground.buttonDisabled === true
          && loading.playground.buttonLabel === "Calling..."
          && /Executing MCP tool call/.test(loading.playground.output),
        `button "${loading.playground.buttonLabel}" disabled=${loading.playground.buttonDisabled} · output "${loading.playground.output.slice(0, 60)}"`,
      );
      if (state("activity")) {
        await switchTab(page, "activity");
        await sleep(150);
        await shoot(page, `mcp-hub-loading-activity-${args.viewport}-${args.theme}`);
        await switchTab(page, "connected");
      }
      await shoot(page, `mcp-hub-loading-${args.viewport}-${args.theme}`);
      await page.waitForFunction(
        () => document.getElementById("mcpPlayBtn").textContent.trim() === "Test Call"
          && (document.getElementById("mcpPlayOutput").textContent || "").includes("slow echo: slow"),
        { timeout: 60000 },
      );
      await sleep(250);
      const loaded = await readHub(page);
      record(
        "and it lands as a completed call",
        loaded.playground.buttonDisabled === false
          && loaded.calls.some((call) => /slow_echo/.test(call.title) && call.state === "ok"),
        loaded.calls.filter((call) => /slow_echo/.test(call.title)).map((call) => `${call.state} ${call.meta}`).join(" · ") || "no slow_echo row",
      );
    }

    if (state("long")) {
      await runTool(page, "local-notes", "big_output", "{}");
      await page.waitForFunction(
        () => document.getElementById("mcpPlayBtn").textContent.trim() === "Test Call"
          && (document.getElementById("mcpPlayOutput").textContent || "").includes("line 100"),
        { timeout: 60000 },
      );
      await sleep(250);
      const long = await readHub(page);
      record(
        "long output scrolls inside its own box instead of stretching the dialog",
        long.playground.outputScrolls === true && long.playground.output.length <= 300,
        `scrolls=${long.playground.outputScrolls} · body ${long.body ? `${long.body.scrollHeight}px in ${long.body.clientHeight}px` : "n/a"}`,
      );
      const longCall = long.calls.find((call) => /big_output/.test(call.title));
      record(
        "and its Activity row stays a one-line summary",
        Boolean(longCall) && longCall.err.length <= 200,
        longCall ? `detail ${longCall.err.length} chars` : "no call row",
      );
      await switchTab(page, "activity");
      await sleep(200);
      await shoot(page, `mcp-hub-long-${args.viewport}-${args.theme}`);
      await switchTab(page, "connected");
    }

    // ── 6: keyboard, and the strip at this viewport ─────────────────────────
    if (state("error")) {
      await switchTab(page, "catalog");
      await page.evaluate(() => { const tab = document.getElementById("mcpTabCatalog"); if (tab) tab.focus(); });
      await page.keyboard.press("ArrowRight");
      await sleep(200);
      const moved = await readHub(page);
      record(
        "arrow keys move between the tabs and aria-selected follows",
        (moved.tabs.find((tab) => tab.selected) || {}).id === "connected"
          && moved.tabs.find((tab) => tab.id === "connected").tabIndex === 0
          && moved.tabs.find((tab) => tab.id === "catalog").tabIndex === -1
          && moved.focused === "connected",
        `selected=${(moved.tabs.find((tab) => tab.selected) || {}).id} focused=${moved.focused} tabindex=${moved.tabs.map((tab) => `${tab.id}:${tab.tabIndex}`).join(",")}`,
      );
      await page.keyboard.press("End");
      await sleep(200);
      const last = await readHub(page);
      record(
        "End reaches the last tab and only one section is ever shown",
        (last.tabs.find((tab) => tab.selected) || {}).id === "activity"
          && last.panels.filter((panel) => !panel.hidden).length === 1,
        `selected=${(last.tabs.find((tab) => tab.selected) || {}).id} · ${last.panels.filter((panel) => !panel.hidden).length} panel(s)`,
      );
      record(
        "Activity lists every configured server, healthy or not",
        last.activity.length >= 3
          && last.activity.some((row) => /dead-remote/.test(row.title) && row.err.length > 0)
          && last.activity.some((row) => /local-notes/.test(row.title) && row.state === "On"),
        `${last.activity.length} health row(s): ${last.activity.map((row) => `${row.title.split(" ")[0]}:${row.state}`).join(", ")}`,
      );
      await shoot(page, `mcp-hub-activity-health-${args.viewport}-${args.theme}`);
      const strip = await page.evaluate(() => {
        const tabs = document.querySelector("#mcpModal .mcp-hub-tabbar");
        const box = tabs ? tabs.getBoundingClientRect() : null;
        return {
          visible: Boolean(box) && box.top >= 0 && box.bottom <= window.innerHeight,
          fits: Boolean(box) && box.width <= window.innerWidth,
          scrollsSideways: tabs ? tabs.scrollWidth > tabs.clientWidth + 2 : null,
        };
      });
      record(
        "the tab strip stays on screen and inside the viewport at this size",
        strip.visible === true && strip.fits === true,
        `visible=${strip.visible} fits=${strip.fits} sideways=${strip.scrollsSideways}`,
      );
    }

    record("the hub raises no console error", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
    const unexpected = [...new Set(posts)].filter((path) => !path.startsWith("/api/mcp/"));
    record(
      "the hub only posts to MCP endpoints",
      unexpected.length === 0,
      [...new Set(posts)].join(" · ") || "no POST",
    );
    await page.close().catch(() => {});
  } finally {
    for (const fixture of fixtures.slice()) fixture.child.kill();
    await browser.close().catch(() => {});
  }

  const passed = results.filter((result) => result.passed).length;
  if (args.json) {
    console.log(JSON.stringify({ viewport: args.viewport, theme: args.theme, passed, total: results.length, screenshots: shots, results }, null, 2));
  } else {
    console.log(`\n${passed}/${results.length} checks passed · ${shots.length} screenshots`);
  }
  return results.every((result) => result.passed) ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  });
