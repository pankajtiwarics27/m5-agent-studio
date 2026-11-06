#!/usr/bin/env node
/**
 * Check that the MCP Tools tab answers the two questions a tool call raises (UIV2-23).
 *
 *   node scripts/mcp_tools_walk.mjs --screenshots docs/media/mcp-tools-uiv2-23/after
 *
 * UIV2-21 made the MCP surface a hub and UIV2-22 made connecting a server a guided path. What was
 * still missing was the other half of that decision: given the tools a server actually returned,
 * which of them can a model call, what does each one cost, and what happens when it is called?
 * The tool list lived inside a collapsed drawer on the Connected row as a checkbox and a
 * description, with no risk, no policy, and no way to try one without guessing the arguments.
 *
 * What it proves:
 *
 *  1. the hub has a fourth tab - Tools - that counts the discovered tools and shows one panel;
 *  2. with nothing connected it says so in words that name the next action, and it does not
 *     pretend a server exists;
 *  3. configured, discovered, available and denied are four different numbers, each on screen;
 *  4. every row states the risk of a call (read-only / write / repeatable write / destructive),
 *     whether the server currently allows it, what a call does, what happens when a model asks,
 *     and what arguments it takes;
 *  5. filter chips count what they would show and narrow the list; search narrows it too, and a
 *     search with no hit says so and offers to clear itself;
 *  6. a row's Deny toggle really is the allowlist: the row turns denied, its Test call is disabled,
 *     the counters move, and the server itself then refuses that tool with a structured denial;
 *  7. Allow puts it back, and the same call that was refused then succeeds - recovery, not a reload;
 *  8. "Test call" lands on the Connected tab with that server and tool selected, the arguments
 *     pre-filled from the tool's schema, and the risk stated before anything runs;
 *  9. invalid JSON, a non-object, and a missing required argument are each named in words, and the
 *     button stays disabled until the arguments could actually be accepted;
 * 10. a write tool cannot be tested by accident: the consent row appears, the button is disabled
 *     until it is ticked, and no request leaves the page while it is unticked;
 * 11. the call goes through the same authorization a model's call does: the request carries
 *     `confirm_write` only after consent, and the same tool called without it is refused by the
 *     server with a 428 that says nothing ran;
 * 12. the result inspector shows the outcome, the latency, the length, the authorization (grant
 *     source, policy, whether it was confirmed, whether a model would be asked) and the output -
 *     with the raw response behind a disclosure that starts closed;
 * 13. long output is truncated with a real "Show all" that restores every character, and it
 *     scrolls inside its own box instead of stretching the dialog;
 * 14. a tool that reports failure renders as a failure with its own message, and Activity records
 *     it as failed;
 * 15. a slow call shows a loading state instead of a frozen button, and lands as a completed call;
 * 16. the Tools tab is reachable and operable from the keyboard, arrow keys included;
 * 17. no console error, no secret in the document, and the page posts only MCP endpoints.
 *
 * Usage:
 *
 *   node scripts/mcp_tools_walk.mjs                                  # starts its own fixture
 *   node scripts/mcp_tools_walk.mjs --base-url URL --token TOKEN     # use a running app
 *   node scripts/mcp_tools_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/mcp_tools_walk.mjs --states empty,list,denied       # a subset
 *   node scripts/mcp_tools_walk.mjs --capture-only                   # write a "before" set
 */

import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const FIXTURE_SERVER = resolve("scripts/mcp_hub_fixture_server.py");

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

const STATES = [
  "empty",
  "list",
  "filters",
  "search",
  "confirm",
  "error",
  "loading",
  "result",
  "long",
  "denied",
  "recovery",
  "failure",
  "keyboard",
];

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
        console.log("usage: node scripts/mcp_tools_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states empty,list,filters,search,confirm,error,loading,result,long,denied,recovery,failure,keyboard] [--json] [--capture-only]");
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
  const dir = mkdtempSync(join(tmpdir(), "mcp-tools-fixture-"));
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

async function startFixture() {
  const support = writeFixtureSupport();
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", "Qwen3.6-35B-REAP-19B", "--port", "0"],
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

/** Everything this story claims, read back from the DOM. */
function readTools(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const text = (selector) => clean((document.querySelector(selector) || {}).textContent || "");
    const id = (name) => document.getElementById(name);
    const shown = (node) => Boolean(node) && node.offsetParent !== null && !node.hidden;
    const rows = [...document.querySelectorAll("#mcpToolList .mcp-tool-row")].map((row) => ({
      key: row.dataset.toolKey || "",
      risk: row.dataset.risk || "",
      state: row.dataset.state || "",
      pills: [...row.querySelectorAll(".mcp-pill")].map((pill) => clean(pill.textContent)),
      desc: clean((row.querySelector(".mcp-tool-desc") || {}).textContent || ""),
      consequences: [...row.querySelectorAll(".mcp-tool-consequence")].map((line) => clean(line.textContent)),
      schema: clean((row.querySelector(".mcp-tool-schema") || {}).textContent || ""),
      schemaHidden: row.querySelector(".mcp-tool-schema") ? row.querySelector(".mcp-tool-schema").hidden : null,
      testDisabled: (row.querySelector("button") || {}).disabled === true,
      allowLabel: clean((row.querySelectorAll("button")[1] || {}).textContent || ""),
      borderStyle: row ? getComputedStyle(row).borderStyle : "",
      visible: row.offsetParent !== null,
    }));
    const filters = [...document.querySelectorAll("#mcpToolFilters .chip")].map((chip) => ({
      label: clean(chip.textContent),
      pressed: chip.getAttribute("aria-pressed") === "true",
    }));
    const tabs = [...document.querySelectorAll("#mcpModal [data-mcp-tab]")].map((tab) => ({
      id: tab.dataset.mcpTab,
      label: clean(tab.textContent),
      selected: tab.getAttribute("aria-selected") === "true",
    }));
    const panels = [...document.querySelectorAll("#mcpModal .mcp-hub-panel")].map((panel) => ({
      id: panel.id,
      hidden: panel.hidden === true,
    }));
    const rawDisclosure = id("mcpPlayRaw");
    return {
      hasToolsTab: tabs.some((tab) => tab.id === "tools"),
      tabs,
      panels,
      activeTab: (tabs.find((tab) => tab.selected) || {}).id || "",
      counts: {
        catalog: clean((id("mcpTabCatalogCount") || {}).textContent || ""),
        connected: clean((id("mcpTabConnectedCount") || {}).textContent || ""),
        tools: clean((id("mcpTabToolsCount") || {}).textContent || ""),
        activity: clean((id("mcpTabActivityCount") || {}).textContent || ""),
      },
      summary: clean((id("mcpToolsSummary") || {}).textContent || ""),
      summaryNumbers: [...document.querySelectorAll("#mcpToolsSummary b")].map((node) => clean(node.textContent)),
      rows,
      filters,
      emptyHidden: id("mcpToolsEmpty") ? id("mcpToolsEmpty").hidden : null,
      emptyText: clean((id("mcpToolsEmpty") || {}).textContent || ""),
      listVisible: shown(id("mcpToolList")),
      play: {
        server: (id("mcpPlayServer") || {}).value || "",
        tool: (id("mcpPlayTool") || {}).value || "",
        tools: [...document.querySelectorAll("#mcpPlayTool option")].map((option) => option.value),
        args: (id("mcpPlayArgs") || {}).value || "",
        riskText: clean((id("mcpPlayRisk") || {}).textContent || ""),
        riskHidden: id("mcpPlayRisk") ? id("mcpPlayRisk").hidden : null,
        consentHidden: id("mcpPlayConsent") ? id("mcpPlayConsent").hidden : null,
        consentText: clean((id("mcpPlayConsentText") || {}).textContent || ""),
        consentChecked: Boolean(id("mcpPlayConsentBox") && id("mcpPlayConsentBox").checked),
        schema: clean((id("mcpPlaySchema") || {}).textContent || ""),
        argError: id("mcpPlayArgsError") && !id("mcpPlayArgsError").hidden
          ? clean(id("mcpPlayArgsError").textContent)
          : "",
        argErrorRole: (id("mcpPlayArgsError") || {}).getAttribute
          ? id("mcpPlayArgsError").getAttribute("role") || ""
          : "",
        button: clean((id("mcpPlayBtn") || {}).textContent || ""),
        buttonDisabled: Boolean(id("mcpPlayBtn") && id("mcpPlayBtn").disabled),
        resultHidden: id("mcpPlayResult") ? id("mcpPlayResult").hidden : null,
        state: (id("mcpPlayResult") || {}).dataset ? id("mcpPlayResult").dataset.state || "" : "",
        meta: clean((id("mcpPlayMeta") || {}).textContent || ""),
        auth: clean((id("mcpPlayAuth") || {}).textContent || ""),
        output: clean((id("mcpPlayOutput") || {}).textContent || ""),
        outputVisible: shown(id("mcpPlayOutput")),
        outputScrolls: Boolean(id("mcpPlayOutput")) && id("mcpPlayOutput").scrollHeight > id("mcpPlayOutput").clientHeight + 8,
        moreHidden: id("mcpPlayMore") ? id("mcpPlayMore").hidden : null,
        moreLabel: clean((id("mcpPlayMore") || {}).textContent || ""),
        rawHidden: id("mcpPlayRaw") ? id("mcpPlayRaw").hidden : null,
        rawOpen: Boolean(rawDisclosure && rawDisclosure.open),
        raw: clean((id("mcpPlayRawBody") || {}).textContent || "").slice(0, 4000),
      },
      calls: [...document.querySelectorAll("#mcpActivityCalls .mcp-activity-row")].map((row) => ({
        title: clean((row.querySelector(".mcp-activity-title") || {}).textContent || ""),
        state: clean((row.querySelector(".mcp-pill") || {}).textContent || ""),
        err: clean((row.querySelector(".mcp-activity-err, .mcp-activity-detail") || {}).textContent || ""),
      })),
      body: (() => {
        const host = document.querySelector("#mcpModal .mcp-hub-body");
        return host ? { scrollHeight: host.scrollHeight, clientHeight: host.clientHeight, overflows: host.scrollHeight > host.clientHeight + 4 } : null;
      })(),
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      panelOverflow: (() => {
        const panel = document.getElementById("mcpPanelTools");
        return Boolean(panel) && panel.scrollWidth > panel.clientWidth + 1;
      })(),
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
  await page.waitForFunction(
    () => !document.getElementById("mcpCatalogGrid")
      || document.querySelectorAll("#mcpCatalogGrid .mcp-card").length > 0,
    { timeout: 30000 },
  ).catch(() => {});
  await sleep(400);
};

/** Add the fixture server through the real guide, and wait for its row. */
const addServer = async (page, config) => {
  await switchTab(page, "connected");
  await page.evaluate(() => { const editor = document.getElementById("mcpEditor"); if (editor) editor.open = true; });
  await fill(page, "mcpAlias", config.alias);
  await fill(page, "mcpName", config.name || config.alias);
  await fill(page, "mcpTransport", config.transport || "stdio");
  await fill(page, "mcpApprovalPolicy", config.policy || "inherit");
  await fill(page, "mcpCommand", config.command || "");
  await fill(page, "mcpArgs", config.args || "");
  await fill(page, "mcpEnvJson", "");
  await page.evaluate(() => {
    if (typeof mcpWizardGo !== "function") return;
    const consent = document.getElementById("mcpStdioConsent");
    if (consent) {
      consent.checked = true;
      consent.dispatchEvent(new Event("change", { bubbles: true }));
    }
    mcpWizardGo(4, { focus: false });
  });
  await clickText(page, "#mcpSaveBtn", "Save");
  await page.waitForFunction(
    (alias) => {
      const row = document.querySelector(`#configuredMcpServersList [data-mcp-server="${alias}"]`);
      const list = document.getElementById("configuredMcpServersList");
      const byText = Boolean(list) && (list.innerHTML || "").includes(alias);
      return Boolean(row || byText) && document.getElementById("mcpSaveBtn").disabled === false;
    },
    { timeout: 120000 },
    config.alias,
  );
  await sleep(300);
};

/** Select a tool in the playground the way the row's Test call button does. */
const openTool = async (page, tool) => {
  await page.evaluate((name) => {
    if (typeof openMcpToolTest === "function") {
      openMcpToolTest("local-notes", name);
      return;
    }
    switchMcpTab("connected");
    const picker = document.getElementById("mcpPlayTool");
    picker.value = name;
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  }, tool);
  await sleep(300);
};

const setConsent = (page, checked) => page.evaluate((value) => {
  const box = document.getElementById("mcpPlayConsentBox");
  if (!box) return false;
  box.checked = value;
  box.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}, checked);

const clickTest = (page) => page.evaluate(() => {
  const button = document.getElementById("mcpPlayBtn");
  if (!button) return false;
  button.click();
  return true;
});

let idleTimeout = 60000;

const waitIdle = (page) => page.waitForFunction(
  () => {
    const result = document.getElementById("mcpPlayResult");
    const button = document.getElementById("mcpPlayBtn");
    return Boolean(result && button) && result.dataset.state !== "loading" && button.textContent.trim() === "Test Call";
  },
  { timeout: idleTimeout },
);

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  //: a build that has no such state should fail fast while a "before" set is being captured
  const waitMs = args.captureOnly ? 5000 : 60000;
  idleTimeout = waitMs;
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

  /** One story state. A build that predates this story has no such state: in `--capture-only`
   *  mode that is recorded and photographed as it stands, instead of ending the run. */
  let guardedPage = null;
  //: the file name each state's block writes, so a build that lacks the state still lands on it
  const GUARDED_SHOTS = { filters: "filter", confirm: "consent" };
  const guarded = async (name, run) => {
    try {
      await run();
    } catch (error) {
      if (!args.captureOnly) throw error;
      record(`${name} state`, false, `this build has no ${name} state (${error.message.split("\n")[0]})`);
      if (args.screenshots && guardedPage) {
        const shot = GUARDED_SHOTS[name] || name;
        const path = resolve(args.screenshots, `mcp-tools-${shot}-${args.viewport}-${args.theme}.png`);
        if (!existsSync(path)) await shoot(guardedPage, `mcp-tools-${shot}-${args.viewport}-${args.theme}`);
      }
    }
  };

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" ").filter(Boolean),
  });
  const pageErrors = [];
  const posts = [];
  const bodies = [];
  const fixtures = [];

  try {
    const fixture = args.baseUrl ? null : await startFixture();
    if (fixture) fixtures.push(fixture);
    const base = args.baseUrl || fixture.ready.base_url;
    const token = args.token || (fixture && fixture.ready.token) || "";
    const origin = new URL(base).origin;

    const page = await browser.newPage();
  guardedPage = page;
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error" && !/status of 40[34]|status of 42[0-9]|status of 50[0-9]|ERR_CONNECTION_REFUSED|Failed to load resource/.test(message.text())) {
        pageErrors.push(message.text());
      }
    });
    page.on("request", (request) => {
      if (request.method() === "POST" || request.method() === "DELETE") {
        posts.push(new URL(request.url()).pathname);
        if (request.postData()) bodies.push({ path: new URL(request.url()).pathname, body: request.postData() });
      }
    });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await page.evaluateOnNewDocument((sessionToken, key, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, sessionToken);
      localStorage.setItem("m5_theme", theme);
    }, token, args.tokenKey, args.theme, origin);
    await page.goto(origin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 8);

    // ── 1: with nothing connected, the Tools tab says so ────────────────────
    if (state("empty")) await guarded("empty", async () => {
      await openHub(page);
      await switchTab(page, "tools");
      await sleep(300);
      const empty = await readTools(page);
      record(
        "the hub has a fourth tab, and it is the one that is showing",
        empty.hasToolsTab === true
          && empty.tabs.length === 4
          && empty.tabs.map((tab) => tab.id).join(",") === "catalog,connected,tools,activity"
          && empty.activeTab === "tools"
          && empty.panels.filter((panel) => !panel.hidden).length === 1
          && (empty.panels.find((panel) => !panel.hidden) || {}).id === "mcpPanelTools",
        `${empty.tabs.map((tab) => `${tab.label}${tab.selected ? "*" : ""}`).join(" · ")}`,
      );
      record(
        "an empty Tools tab names the next action instead of showing a blank panel",
        empty.rows.length === 0
          && empty.emptyHidden === false
          && /Sync a server|Catalog/.test(empty.emptyText),
        `"${empty.emptyText.slice(0, 140)}"`,
      );
      record(
        "and its counters are honest zeroes, not placeholders",
        empty.summary === ""
          ? empty.tabs.length === 4
          : empty.summaryNumbers.join(",") === "0,0,0,0",
        empty.summary || "(no summary in the pre-Tools dialog)",
      );
      await shoot(page, `mcp-tools-empty-${args.viewport}-${args.theme}`);
    });

    // ── 2: a live server's real tools, each with its risk and its policy ─────
    await openHub(page);
    await addServer(page, {
      alias: "local-notes",
      name: "Fixture notes",
      transport: "stdio",
      command: process.env.STUDIO_PYTHON || "python3",
      args: FIXTURE_SERVER,
      policy: "inherit",
    });

    if (state("list")) await guarded("list", async () => {
      await switchTab(page, "tools");
      await sleep(300);
      const list = await readTools(page);
      record(
        "every discovered tool is one row, with the risk of a call and whether it is allowed",
        list.rows.length === 4 && list.rows.every((row) => row.pills.length === 2
          && /^(Read-only|Write|Write · repeatable|Destructive)$/.test(row.pills[0])
          && /^(Available|Denied)$/.test(row.pills[1])),
        `${list.rows.length} row(s) · ${list.rows.map((row) => `${row.key}:${row.pills.join("/")}`).join(" · ")}`,
      );
      record(
        "the read-only hint from the server decides \"Read-only\"; an unannotated tool is not called safe",
        list.rows.some((row) => /echo_note$/.test(row.key) && row.risk === "read")
          && list.rows.some((row) => /big_output$/.test(row.key) && row.risk === "read")
          && list.rows.some((row) => /slow_echo$/.test(row.key) && row.risk === "write")
          && list.rows.some((row) => /fail_always$/.test(row.key) && row.risk === "destructive"),
        list.rows.map((row) => `${row.key}=${row.risk}`).join(" · "),
      );
      record(
        "each row answers what a call does, what happens when a model asks, and what it takes",
        list.rows.every((row) => row.desc.length > 0
          && row.consequences.length === 3
          && /^What a call does /.test(row.consequences[0])
          && /^When a model asks /.test(row.consequences[1])
          && /^Arguments /.test(row.consequences[2])),
        list.rows[0] ? list.rows[0].consequences.join(" | ") : "no row",
      );
      record(
        "the approval policy is explained in the words of the policy, not a code",
        list.rows.some((row) => /Harness policy/.test(row.consequences[1]))
          && !list.rows.some((row) => /inherit|always_ask/i.test(row.consequences[1])),
        list.rows.find((row) => /slow_echo/.test(row.key))?.consequences[1] || "",
      );
      record(
        "configured, discovered, available and denied are four numbers on screen",
        /configured server/.test(list.summary)
          && /discovered tool/.test(list.summary)
          && /available to call/.test(list.summary)
          && /denied by the allowlist/.test(list.summary)
          && list.summaryNumbers.slice(0, 4).join(",") === `1,${list.rows.length},${list.rows.length},0`,
        list.summary,
      );
      record(
        "the tab count is the discovered total, and the toolbar still fits",
        list.counts.tools === String(list.rows.length)
          && list.horizontalOverflow === false
          && list.panelOverflow === false,
        `tools=${list.counts.tools} · document ${list.innerWidth}px · panel overflow ${list.panelOverflow}`,
      );
      await shoot(page, `mcp-tools-list-${args.viewport}-${args.theme}`);

      // the raw schema is a disclosure that starts closed
      await page.evaluate(() => {
        const button = [...document.querySelectorAll("#mcpToolList .mcp-tool-row button")]
          .find((node) => (node.textContent || "").trim() === "Raw schema");
        if (button) button.click();
      });
      await sleep(200);
      const schema = await readTools(page);
      record(
        "the raw input schema is behind a disclosure that starts closed and opens on demand",
        list.rows.every((row) => row.schemaHidden === true)
          && schema.rows.some((row) => row.schemaHidden === false && /"type"/.test(row.schema)),
        schema.rows.filter((row) => !row.schemaHidden).map((row) => `${row.key}: ${row.schema.slice(0, 60)}`).join(" | ") || "no schema opened",
      );
      if (wide) {
        record(
          "at desktop sizes a whole tool row - name, risk, policy, arguments and actions - is on the first screen",
          await page.evaluate(() => {
            const host = document.querySelector("#mcpModal .mcp-hub-body");
            const first = document.querySelector("#mcpToolList .mcp-tool-row");
            if (!host || !first) return false;
            const box = host.getBoundingClientRect();
            const rect = first.getBoundingClientRect();
            return rect.top >= box.top && rect.bottom <= Math.min(box.bottom, window.innerHeight);
          }),
          "first row inside the dialog's first screen",
        );
      }
    });

    // ── 3: filter chips count, and narrow ───────────────────────────────────
    if (state("filters")) await guarded("filters", async () => {
      await switchTab(page, "tools");
      await sleep(200);
      const all = await readTools(page);
      record(
        "the filter chips count what they would show, and All is pressed",
        all.filters.length === 4
          && all.filters[0].pressed === true
          && all.filters.map((chip) => chip.label.replace(/\s+/g, " ")).join("|") === `All ${all.rows.length}|Read-only 2|Writes 2|Denied 0`,
        all.filters.map((chip) => chip.label).join(" · "),
      );
      await clickText(page, "#mcpToolFilters .chip", "Read-only");
      await sleep(200);
      const reads = await readTools(page);
      record(
        "\"Read-only\" shows only the tools whose calls change nothing",
        reads.rows.length === 2 && reads.rows.every((row) => row.risk === "read"),
        reads.rows.map((row) => `${row.key}=${row.risk}`).join(" · "),
      );
      await clickText(page, "#mcpToolFilters .chip", "Writes");
      await sleep(200);
      const writes = await readTools(page);
      record(
        "\"Writes\" shows the writes and the destructive call, and nothing else",
        writes.rows.length === 2 && writes.rows.every((row) => row.risk !== "read"),
        writes.rows.map((row) => `${row.key}=${row.risk}`).join(" · "),
      );
      await shoot(page, `mcp-tools-filter-${args.viewport}-${args.theme}`);
      await clickText(page, "#mcpToolFilters .chip", "All");
      await sleep(200);
    });

    // ── 4: search narrows the tool list, and says when it finds nothing ─────
    if (state("search")) await guarded("search", async () => {
      await switchTab(page, "tools");
      await fill(page, "mcpHubSearch", "slow");
      await sleep(300);
      const found = await readTools(page);
      record(
        "the hub's search narrows the tool list too",
        found.rows.length === 1 && /slow_echo/.test(found.rows[0].key) && found.listVisible === true,
        `${found.rows.length} row(s): ${found.rows.map((row) => row.key).join(", ")}`,
      );
      await shoot(page, `mcp-tools-search-${args.viewport}-${args.theme}`);
      await fill(page, "mcpHubSearch", "zzz-no-such-tool");
      await sleep(300);
      const none = await readTools(page);
      record(
        "a search with no hit says so and offers to clear itself",
        none.rows.length === 0 && none.emptyHidden === false && /No tool matches/i.test(none.emptyText),
        `"${none.emptyText.slice(0, 120)}"`,
      );
      await shoot(page, `mcp-tools-search-empty-${args.viewport}-${args.theme}`);
      await clickText(page, "#mcpToolsEmpty button", "Clear search");
      await sleep(300);
      const cleared = await readTools(page);
      record(
        "clearing it puts every tool back",
        cleared.rows.length === 4,
        `${cleared.rows.length} row(s)`,
      );
    });

    // ── 5: a test call starts from the row, and states the risk first ───────
    if (state("confirm")) await guarded("confirm", async () => {
      await switchTab(page, "tools");
      await sleep(200);
      await openTool(page, "slow_echo");
      const picked = await readTools(page);
      record(
        "\"Test call\" lands on the Connected tab with that server and tool selected",
        picked.activeTab === "connected"
          && picked.play.tool === "slow_echo"
          && picked.play.server === "local-notes"
          && /note/.test(picked.play.args),
        `tab=${picked.activeTab} server=${picked.play.server} tool=${picked.play.tool} args=${picked.play.args}`,
      );
      record(
        "the arguments are pre-filled from the tool's own schema, and the schema is on screen",
        picked.play.args === '{"note":""}' && /note: string \(required\)/.test(picked.play.schema),
        `args=${picked.play.args} · schema "${picked.play.schema}"`,
      );
      record(
        "a write states its risk and asks for consent before it can run",
        picked.play.riskHidden === false
          && /^Write\b/.test(picked.play.riskText)
          && picked.play.consentHidden === false
          && /I understand this is a write and I want to run slow_echo once/.test(picked.play.consentText)
          && picked.play.buttonDisabled === true,
        `risk "${picked.play.riskText.slice(0, 70)}" · consent "${picked.play.consentText}" · button disabled ${picked.play.buttonDisabled}`,
      );
      record(
        "and nothing is sent while the box is unticked",
        await page.evaluate(() => {
          const before = performance.getEntriesByType("resource").length;
          const button = document.getElementById("mcpPlayBtn");
          button.click();
          return document.getElementById("mcpPlayResult").hidden !== false || before >= 0;
        }) && posts.filter((path) => /\/call$/.test(path)).length === 0,
        `${posts.filter((path) => /\/call$/.test(path)).length} call request(s) so far`,
      );
      await shoot(page, `mcp-tools-consent-${args.viewport}-${args.theme}`);
      await setConsent(page, true);
      await sleep(200);
      const consented = await readTools(page);
      record(
        "ticking the consent is what enables the button",
        consented.play.buttonDisabled === false && consented.play.consentChecked === true,
        `button disabled ${consented.play.buttonDisabled}`,
      );
      await setConsent(page, false);
      await sleep(150);
      const unticked = await readTools(page);
      record(
        "unticking it disables the button again, so consent is per call",
        unticked.play.buttonDisabled === true,
        `button disabled ${unticked.play.buttonDisabled}`,
      );
    });

    // ── 6: bad arguments are named, and the button does not pretend ─────────
    if (state("error")) await guarded("error", async () => {
      await openTool(page, "slow_echo");
      await fill(page, "mcpPlayArgs", '{"note": 3,,}');
      await sleep(200);
      const broken = await readTools(page);
      record(
        "arguments that are not JSON are named as such, in an alert, with the button disabled",
        /not valid JSON/.test(broken.play.argError)
          && broken.play.argErrorRole === "alert"
          && broken.play.buttonDisabled === true,
        `"${broken.play.argError.slice(0, 120)}" role=${broken.play.argErrorRole} disabled=${broken.play.buttonDisabled}`,
      );
      await shoot(page, `mcp-tools-error-${args.viewport}-${args.theme}`);
      await fill(page, "mcpPlayArgs", "[1, 2, 3]");
      await sleep(200);
      const notObject = await readTools(page);
      record(
        "a JSON array is refused too: arguments have to be one object",
        /one JSON object/.test(notObject.play.argError) && notObject.play.buttonDisabled === true,
        `"${notObject.play.argError.slice(0, 120)}"`,
      );
      await fill(page, "mcpPlayArgs", "{}");
      await sleep(200);
      const missing = await readTools(page);
      record(
        "a missing required argument is named before the call, not after it",
        /needs note/.test(missing.play.argError) && missing.play.buttonDisabled === true,
        `"${missing.play.argError.slice(0, 120)}"`,
      );
      await fill(page, "mcpPlayArgs", '{"note":"uiv2-23"}');
      await sleep(200);
      const fixed = await readTools(page);
      record(
        "and the error clears as soon as the arguments could be accepted",
        fixed.play.argError === "",
        `argError "${fixed.play.argError}"`,
      );
    });

    // ── 7: a slow call shows loading, then lands as a real result ───────────
    if (state("loading")) await guarded("loading", async () => {
      await openTool(page, "slow_echo");
      await fill(page, "mcpPlayArgs", '{"note":"uiv2-23 slow"}');
      await setConsent(page, true);
      await sleep(150);
      await clickTest(page);
      await sleep(250);
      const loading = await readTools(page);
      record(
        "a slow call shows a loading state instead of a frozen button",
        loading.play.button === "Calling..."
          && loading.play.buttonDisabled === true
          && loading.play.state === "loading",
        `button "${loading.play.button}" state=${loading.play.state}`,
      );
      await shoot(page, `mcp-tools-loading-${args.viewport}-${args.theme}`);
      await waitIdle(page);
      await sleep(300);
      const landed = await readTools(page);
      record(
        "and it lands as a completed call with its latency and length",
        landed.play.state === "ok"
          && /Call returned/.test(landed.play.meta)
          && /\d+ ms/.test(landed.play.meta)
          && /slow echo: uiv2-23 slow/.test(landed.play.output),
        `${landed.play.meta} · "${landed.play.output.slice(0, 80)}"`,
      );
    });

    // ── 8: the result inspector says who authorized the call ────────────────
    if (state("result")) await guarded("result", async () => {
      await openTool(page, "slow_echo");
      await fill(page, "mcpPlayArgs", '{"note":"uiv2-23 inspector"}');
      await setConsent(page, true);
      await sleep(150);
      await clickTest(page);
      await waitIdle(page);
      await sleep(300);
      const done = await readTools(page);
      record(
        "the request carried confirm_write only because the consent was ticked",
        bodies.some((entry) => /\/call$/.test(entry.path)
          && /"confirm_write":\s*true/.test(entry.body)
          && /slow_echo/.test(entry.body)),
        bodies.filter((entry) => /\/call$/.test(entry.path)).map((entry) => entry.body.slice(0, 90)).slice(-2).join(" | "),
      );
      record(
        "the same tool, called without that confirmation, is refused by the server - nothing runs",
        await page.evaluate(async () => {
          const response = await fetch("/api/mcp/servers/local-notes/call", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tool: "slow_echo", arguments: { note: "no confirmation" } }),
          });
          const body = await response.json();
          return response.status === 428 && Boolean(body.confirmation_required)
            && body.confirmation_required.risk !== "read";
        }),
        "POST without confirm_write → 428 confirmation_required",
      );
      record(
        "the inspector shows the outcome, the latency, the length and the risk",
        /^Write/.test(done.play.meta) && /Call returned/.test(done.play.meta)
          && /\d+ ms/.test(done.play.meta) && /characters/.test(done.play.meta),
        done.play.meta,
      );
      record(
        "the authorization line names the grant source, the policy and the confirmation",
        /operator_one_call/.test(done.play.auth)
          && /standard/.test(done.play.auth)
          && /you confirmed this write/.test(done.play.auth)
          && /would wait for your approval/.test(done.play.auth),
        done.play.auth,
      );
      record(
        "the raw response is offered, but starts closed",
        done.play.rawHidden === false && done.play.rawOpen === false && /"elapsed_ms"/.test(done.play.raw),
        `raw disclosure hidden=${done.play.rawHidden} open=${done.play.rawOpen} · ${done.play.raw.slice(0, 60)}`,
      );
      await page.evaluate(() => { document.getElementById("mcpPlayRaw").open = true; });
      await sleep(200);
      const opened = await readTools(page);
      record(
        "and the disclosure can be opened, to the same authorization",
        opened.play.rawOpen === true && /grant_source/.test(opened.play.raw),
        `open=${opened.play.rawOpen} · ${opened.play.raw.slice(0, 200)}`,
      );
      await shoot(page, `mcp-tools-result-${args.viewport}-${args.theme}`);
      record(
        "the call is recorded in Activity as ok",
        done.calls.some((call) => /slow_echo/.test(call.title) && call.state === "ok"),
        done.calls.filter((call) => /slow_echo/.test(call.title)).map((call) => call.state).join(" · ") || "no call row",
      );
    });

    // ── 9: long output is truncated honestly and scrolls in its own box ─────
    if (state("long")) await guarded("long", async () => {
      await openTool(page, "big_output");
      await fill(page, "mcpPlayArgs", "{}");
      await sleep(150);
      await clickTest(page);
      await waitIdle(page);
      await sleep(300);
      const long = await readTools(page);
      record(
        "a read-only tool needs no consent at all",
        long.play.consentHidden === true && long.play.riskText.startsWith("Read-only"),
        `consent hidden ${long.play.consentHidden} · "${long.play.riskText.slice(0, 60)}"`,
      );
      record(
        "long output is truncated with a real count, and says how much is hidden",
        long.play.moreHidden === false
          && /^Show all \d+ characters$/.test(long.play.moreLabel)
          && /characters hidden/.test(long.play.output)
          && /line 100/.test(long.play.output),
        `more "${long.play.moreLabel}" · ${long.play.output.length} chars on screen`,
      );
      record(
        "and it scrolls inside its own box instead of stretching the dialog",
        long.play.outputScrolls === true && long.horizontalOverflow === false,
        `output scrolls=${long.play.outputScrolls} · document ${long.innerWidth}px`,
      );
      await shoot(page, `mcp-tools-long-${args.viewport}-${args.theme}`);
      await clickText(page, "#mcpPlayMore", "Show all");
      await sleep(250);
      const all = await readTools(page);
      record(
        "\"Show all\" restores every character, and then stops offering to",
        !/characters hidden/.test(all.play.output) && all.play.moreHidden === true,
        `${all.play.output.length} chars · more hidden=${all.play.moreHidden}`,
      );
    });

    // ── 10: Deny is the allowlist, and the server honours it ────────────────
    if (state("denied")) await guarded("denied", async () => {
      await switchTab(page, "tools");
      await sleep(200);
      await page.evaluate(() => {
        const row = [...document.querySelectorAll("#mcpToolList .mcp-tool-row")]
          .find((node) => /slow_echo/.test(node.dataset.toolKey || ""));
        const button = row ? [...row.querySelectorAll("button")].find((node) => (node.textContent || "").trim() === "Deny") : null;
        if (button) button.click();
      });
      await page.waitForFunction(() => {
        const row = [...document.querySelectorAll("#mcpToolList .mcp-tool-row")]
          .find((node) => /slow_echo/.test(node.dataset.toolKey || ""));
        return Boolean(row) && row.dataset.state === "denied";
      }, { timeout: waitMs });
      await sleep(300);
      const denied = await readTools(page);
      const row = denied.rows.find((item) => /slow_echo/.test(item.key));
      record(
        "denying a tool turns its row into a denied row with no test call",
        Boolean(row) && row.state === "denied" && row.pills[1] === "Denied" && row.testDisabled === true,
        row ? `${row.pills.join("/")} · test disabled ${row.testDisabled}` : "no row",
      );
      record(
        "the counters and the Denied chip follow it",
        denied.summaryNumbers.slice(0, 4).join(",") === "1,4,3,1"
          && denied.filters.some((chip) => /^Denied 1$/.test(chip.label)),
        `${denied.summaryNumbers.join(",")} · ${denied.filters.map((chip) => chip.label).join(" · ")}`,
      );
      record(
        "the server itself refuses that tool now, with a structured denial",
        await page.evaluate(async () => {
          const response = await fetch("/api/mcp/servers/local-notes/call", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tool: "slow_echo", arguments: { note: "denied" }, confirm_write: true }),
          });
          const body = await response.json();
          return response.status === 403 && body.denial && body.denial.kind === "tool_denied";
        }),
        "POST after Deny → 403 tool_denied",
      );
      await shoot(page, `mcp-tools-denied-${args.viewport}-${args.theme}`);

      await page.evaluate(() => {
        const row = [...document.querySelectorAll("#mcpToolList .mcp-tool-row")]
          .find((node) => /slow_echo/.test(node.dataset.toolKey || ""));
        const button = row ? [...row.querySelectorAll("button")].find((node) => (node.textContent || "").trim() === "Allow") : null;
        if (button) button.click();
      });
      await page.waitForFunction(() => {
        const row = [...document.querySelectorAll("#mcpToolList .mcp-tool-row")]
          .find((node) => /slow_echo/.test(node.dataset.toolKey || ""));
        return Boolean(row) && row.dataset.state === "available";
      }, { timeout: waitMs });
      await sleep(300);
      const allowed = await readTools(page);
      record(
        "Allow puts it back, and the counters return with it",
        allowed.summaryNumbers.slice(0, 4).join(",") === "1,4,4,0"
          && allowed.rows.some((item) => /slow_echo/.test(item.key) && item.testDisabled === false),
        allowed.summaryNumbers.join(","),
      );
    });

    // ── 11: a refused server is a state you can recover from ────────────────
    if (state("recovery")) await guarded("recovery", async () => {
      await page.evaluate(() => toggleMcpServerEnabled("local-notes", false));
      await sleep(900);
      await openTool(page, "echo_note");
      await fill(page, "mcpPlayArgs", '{"note":"recovery"}');
      await sleep(200);
      const off = await readTools(page);
      record(
        "a switched-off server says so before the call, not only after it",
        /switched off, so this will be refused/.test(off.play.riskText),
        `"${off.play.riskText.slice(0, 100)}"`,
      );
      await clickTest(page);
      await waitIdle(page);
      await sleep(300);
      const refused = await readTools(page);
      record(
        "and the refusal is rendered as one: what was refused, and what to do about it",
        refused.play.state === "denied"
          && /switched off/.test(refused.play.meta)
          && /Turn the server on here/.test(refused.play.output)
          && /No authorization was recorded/.test(refused.play.auth),
        `${refused.play.meta} · "${refused.play.output.slice(0, 120)}"`,
      );
      await shoot(page, `mcp-tools-refused-${args.viewport}-${args.theme}`);
      await page.evaluate(() => toggleMcpServerEnabled("local-notes", true));
      await sleep(900);
      await openTool(page, "echo_note");
      await fill(page, "mcpPlayArgs", '{"note":"recovered"}');
      await sleep(150);
      await clickTest(page);
      await waitIdle(page);
      await sleep(300);
      const recovered = await readTools(page);
      record(
        "turning the server back on makes the same call succeed, without a reload",
        recovered.play.state === "ok" && /echo: recovered/.test(recovered.play.output),
        `${recovered.play.state} · "${recovered.play.output.slice(0, 80)}"`,
      );
      await shoot(page, `mcp-tools-recovery-${args.viewport}-${args.theme}`);
    });

    // ── 12: a failing tool is a failure, not a blank panel ─────────────────
    if (state("failure")) await guarded("failure", async () => {
      await openTool(page, "fail_always");
      await fill(page, "mcpPlayArgs", "{}");
      await setConsent(page, true);
      await sleep(150);
      await clickTest(page);
      await waitIdle(page);
      await sleep(300);
      const failed = await readTools(page);
      record(
        "a tool that reports failure renders as failed, with its own message",
        failed.play.state === "failed"
          && /reported a failure/.test(failed.play.meta)
          && /fail_always|fixture tool was asked to fail/.test(failed.play.output),
        `${failed.play.meta} · "${failed.play.output.slice(0, 100)}"`,
      );
      record(
        "and Activity records it as a failed call",
        failed.calls.some((call) => /fail_always/.test(call.title) && call.state === "failed"),
        failed.calls.filter((call) => /fail_always/.test(call.title)).map((call) => `${call.state} "${call.err.slice(0, 60)}"`).join(" · ") || "no call row",
      );
      await shoot(page, `mcp-tools-failure-${args.viewport}-${args.theme}`);
    });

    // ── 13: the Tools tab works from the keyboard ──────────────────────────
    if (state("keyboard")) await guarded("keyboard", async () => {
      await switchTab(page, "connected");
      await sleep(200);
      await page.evaluate(() => {
        const tab = document.querySelector('#mcpModal [data-mcp-tab="connected"]');
        if (tab) tab.focus();
      });
      await page.keyboard.press("ArrowRight");
      await sleep(250);
      const moved = await readTools(page);
      record(
        "arrow keys reach the Tools tab and select it",
        moved.activeTab === "tools"
          && moved.tabs.find((tab) => tab.id === "tools").selected === true
          && moved.panels.find((panel) => panel.id === "mcpPanelTools").hidden === false,
        `active=${moved.activeTab}`,
      );
      const reached = await page.evaluate(() => document.activeElement
        && (document.activeElement.dataset.mcpTab || document.activeElement.id) === "tools");
      record(
        "and focus stays on the tab that was chosen",
        reached === true,
        `focus=${await page.evaluate(() => (document.activeElement && (document.activeElement.dataset.mcpTab || document.activeElement.id)) || "none")}`,
      );
      const rowFocus = await page.evaluate(() => {
        const row = document.querySelector("#mcpToolList .mcp-tool-row");
        const button = row ? row.querySelector("button") : null;
        if (!button) return { found: false };
        button.focus();
        return { found: true, focused: document.activeElement === button, name: (button.textContent || "").trim() };
      });
      record(
        "a row's actions are real buttons a keyboard can focus and press",
        rowFocus.found === true && rowFocus.focused === true,
        rowFocus.found ? `focused "${rowFocus.name}"` : "no row button",
      );
      await page.keyboard.press("Enter");
      await sleep(400);
      const pressed = await readTools(page);
      record(
        "pressing one opens that tool's test call",
        pressed.activeTab === "connected" && pressed.play.tool.length > 0,
        `tab=${pressed.activeTab} tool=${pressed.play.tool}`,
      );
      await shoot(page, `mcp-tools-keyboard-${args.viewport}-${args.theme}`);
    });

    // ── closing checks ─────────────────────────────────────────────────────
    record(
      "no console error, and no secret rendered",
      pageErrors.length === 0
        && !(await page.evaluate(() => /sk-uiv2-\d+-do-not-render/.test(document.documentElement.innerHTML))),
      pageErrors.length ? pageErrors.slice(0, 3).join(" | ") : "clean",
    );
    record(
      "the page posts only MCP endpoints",
      posts.every((path) => path.startsWith("/api/mcp/")),
      [...new Set(posts)].join(", ") || "no posts",
    );
    record(
      "the dialog stays inside the viewport",
      await page.evaluate(() => {
        const dialog = document.querySelector("#mcpModal .modal");
        if (!dialog) return false;
        const rect = dialog.getBoundingClientRect();
        return rect.top >= -1 && rect.bottom <= window.innerHeight + 1
          && rect.left >= -1 && rect.right <= window.innerWidth + 1;
      }),
      `viewport ${width}x${height}`,
    );
  } finally {
    await browser.close();
    for (const fixture of fixtures) fixture.child.kill("SIGTERM");
  }

  const failed = results.filter((result) => result.status === "fail");
  if (args.json) {
    console.log(JSON.stringify({ passed: results.length - failed.length, total: results.length, results, shots }, null, 2));
  } else if (args.screenshots) {
    console.log(`\n${shots.length} screenshot(s)`);
  }
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length && !args.captureOnly) {
    console.log("failed:");
    for (const result of failed) console.log(`  - ${result.step}${result.detail ? ` — ${result.detail}` : ""}`);
    process.exit(1);
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
