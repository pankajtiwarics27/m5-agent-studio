#!/usr/bin/env node
/**
 * Check that connecting an MCP server is a guided, consented, tested path (UIV2-22).
 *
 *   node scripts/mcp_wizard_walk.mjs --screenshots docs/media/mcp-wizard
 *
 * UIV2-21 made the MCP surface a hub with a catalog, a connected dashboard and an activity log.
 * The editor behind it was still one form: alias, name, transport, URL, auth header, token,
 * headers JSON, command, arguments, environment JSON and a policy select, all at once, with a
 * single "Save & Sync Tools" button that either worked or left a red line behind. Nothing asked
 * whether you had thought about running a local command; nothing told you what the chosen policy
 * would cost you; nothing said whether the server had answered before you committed to it.
 *
 * What it proves:
 *
 *  1. the editor is a four-step guide - name it, say how it is reached, decide what a call costs,
 *     then review and connect - with one step on screen, a rail that says where you are and a
 *     progress line a screen reader hears;
 *  2. a step that is not satisfied refuses to advance, says why in words, and puts the cursor in
 *     the field that needs it;
 *  3. the credentials step shows only the fields the chosen transport needs, keeps the token in a
 *     password field with autocomplete off, and never pre-fills it;
 *  4. the typed token is never rendered anywhere: not in the review, not in a confirmation, not in
 *     the document, not in the console, and not in the activity log;
 *  5. a stored token is shown only as the mask the server sends, and saving with the field blank
 *     keeps it (unchanged-token semantics);
 *  6. raw headers and environment JSON live behind one closed advanced disclosure, off the guided
 *     path;
 *  7. the policy step explains what each policy means, and a local command requires an explicit,
 *     named consent before the guide will advance - a consent that the page states it cannot turn
 *     into permission, because the server's own `STUDIO_ALLOW_MCP_STDIO` gate still decides;
 *  8. the last step reviews what will be saved, then saves and tests the connection in one
 *     request: a testing state while it runs, a visible result when it lands, a confirmation with
 *     the tool count, and a failure that keeps the server but shows the server's own note;
 *  9. a failed connection can be fixed in place - changing the transport and saving again turns
 *     the result green without adding a second server;
 * 10. the whole path works from the keyboard alone, with focus landing on each step's heading;
 * 11. no console error, and the hub posts only MCP endpoints.
 *
 * Usage:
 *
 *   node scripts/mcp_wizard_walk.mjs                                 # starts its own fixture
 *   node scripts/mcp_wizard_walk.mjs --base-url URL --token TOKEN    # use a running app
 *   node scripts/mcp_wizard_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/mcp_wizard_walk.mjs --states empty,review           # a subset
 *   node scripts/mcp_wizard_walk.mjs --capture-only                  # write a "before" set
 */


import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const FIXTURE_SERVER = resolve("scripts/mcp_hub_fixture_server.py");
/** A token that must never reach the DOM, the console or a review line. */
const SECRET = "sk-uiv2-22-do-not-render-4b71";
/** Nothing listens here; HTTPS because the app refuses plain http:// to anything but an opted-in private host. */
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

const STATES = ["empty", "credentials", "policy", "review", "loading", "success", "error", "recovery", "keyboard", "long"];

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
        console.log("usage: node scripts/mcp_wizard_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states empty,credentials,policy,review,loading,success,error,recovery,keyboard,long] [--json] [--capture-only]");
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
 * The fixture app runs with the two operator opt-ins this story needs, and no app change:
 * `STUDIO_ALLOW_MCP_STDIO=1` because the app refuses to spawn a command otherwise, and a memory
 * keyring backend because a headless container has no OS credential store (the same stand-in the
 * app's own tests use). The consent the wizard asks for is *additional* to those, never instead.
 */
function writeFixtureSupport() {
  const dir = mkdtempSync(join(tmpdir(), "mcp-wizard-fixture-"));
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

/** What the guide is claiming, read back from the DOM. */
function readWizard(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const el = (id) => document.getElementById(id);
    const steps = [...document.querySelectorAll("#mcpWizardSteps [data-wizard-step]")].map((item) => ({
      step: Number(item.dataset.wizardStep),
      label: clean(item.textContent),
      current: item.getAttribute("aria-current") === "step",
      done: item.classList.contains("done"),
    }));
    const panels = [1, 2, 3, 4].map((step) => ({
      step,
      hidden: Boolean(el(`mcpStep${step}`) && el(`mcpStep${step}`).hidden),
      heading: clean((el(`mcpStep${step}`) && el(`mcpStep${step}`).querySelector("h4") || {}).textContent || ""),
    }));
    const review = [...document.querySelectorAll("#mcpReviewList dt")].map((term) => ({
      term: clean(term.textContent),
      value: clean((term.nextElementSibling || {}).textContent || ""),
    }));
    const result = el("mcpWizardResult");
    return {
      open: Boolean(el("mcpEditor") && el("mcpEditor").open),
      step: typeof mcpWizardStep === "number" ? mcpWizardStep : null,
      progress: clean((el("mcpWizardProgress") || {}).textContent || ""),
      progressRole: (el("mcpWizardProgress") || {}).getAttribute
        ? el("mcpWizardProgress").getAttribute("role") || ""
        : "",
      steps,
      panels,
      visiblePanels: panels.filter((panel) => !panel.hidden).map((panel) => panel.step),
      hint: clean((el("mcpWizardHint") || {}).textContent || ""),
      hintRole: (el("mcpWizardHint") || {}).getAttribute ? el("mcpWizardHint").getAttribute("role") || "" : "",
      backDisabled: Boolean(el("mcpWizardBack") && el("mcpWizardBack").disabled),
      nextHidden: Boolean(el("mcpWizardNext") && el("mcpWizardNext").hidden),
      review,
      result: {
        hidden: Boolean(result && result.hidden),
        state: (result && result.dataset.state) || "",
        text: clean((result || {}).textContent || ""),
        role: result ? result.getAttribute("role") || "" : "",
      },
      confirm: {
        hidden: Boolean(el("mcpWizardConfirm") && el("mcpWizardConfirm").hidden),
        text: clean((el("mcpWizardConfirm") || {}).textContent || ""),
      },
      save: {
        label: clean((el("mcpSaveBtn") || {}).textContent || ""),
        disabled: Boolean(el("mcpSaveBtn") && el("mcpSaveBtn").disabled),
      },
      token: {
        type: (el("mcpToken") || {}).type || "",
        autocomplete: (el("mcpToken") || {}).getAttribute ? el("mcpToken").getAttribute("autocomplete") || "" : "",
        value: (el("mcpToken") || {}).value || "",
      },
      advanced: {
        present: Boolean(el("mcpAdvancedJson")),
        open: Boolean(el("mcpAdvancedJson") && el("mcpAdvancedJson").open),
        summary: clean((el("mcpAdvancedJson") && el("mcpAdvancedJson").querySelector("summary") || {}).textContent || ""),
        onGuidedPath: (() => {
          const headers = el("mcpHeadersJson");
          if (!headers) return false;
          return !headers.closest("details#mcpAdvancedJson");
        })(),
      },
      transport: {
        value: (el("mcpTransport") || {}).value || "",
        httpVisible: Boolean(el("mcpHttpFields") && el("mcpHttpFields").offsetParent !== null),
        stdioVisible: Boolean(el("mcpStdioFields") && el("mcpStdioFields").offsetParent !== null),
        effect: clean((el("mcpTransportEffect") || {}).textContent || ""),
      },
      policy: {
        value: (el("mcpApprovalPolicy") || {}).value || "",
        effect: clean((el("mcpPolicyEffect") || {}).textContent || ""),
        options: el("mcpApprovalPolicy") ? [...el("mcpApprovalPolicy").options].map((option) => option.value) : [],
      },
      consent: {
        rowVisible: Boolean(el("mcpConsentRow") && el("mcpConsentRow").offsetParent !== null),
        checked: Boolean(el("mcpStdioConsent") && el("mcpStdioConsent").checked),
        command: clean((el("mcpConsentCommand") || {}).textContent || ""),
        note: clean((el("mcpConsentNote") || {}).textContent || ""),
        noteVisible: Boolean(el("mcpConsentNote") && el("mcpConsentNote").offsetParent !== null),
      },
      fields: ["mcpAlias", "mcpName", "mcpTransport", "mcpApprovalPolicy", "mcpUrl", "mcpAuthHeader", "mcpToken",
        "mcpHeadersJson", "mcpCommand", "mcpArgs", "mcpEnvJson", "mcpPresetNote", "mcpSaveBtn", "mcpImportJsonInput",
        "configuredMcpServersList", "mcpPlayServer", "mcpPlayTool", "mcpPlayArgs", "mcpPlayBtn", "mcpPlayOutput"]
        .filter((id) => !el(id)),
      focused: document.activeElement ? (document.activeElement.id || document.activeElement.dataset.wizardStep || "") : "",
      secretInDom: document.documentElement.innerHTML.includes("sk-uiv2-22-do-not-render-4b71"),
      rows: [...document.querySelectorAll("#configuredMcpServersList [data-mcp-server]")].map((row) => ({
        alias: row.dataset.mcpServer || "",
        text: clean(row.textContent).slice(0, 300),
      })),
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      activeTab: (() => {
        const tab = document.querySelector("#mcpModal [data-mcp-tab][aria-selected='true']");
        return tab ? tab.dataset.mcpTab : "";
      })(),
    };
  });
}

/**
 * Hold the save request in flight so the "testing" state can be seen and photographed rather
 * than raced: the browser's own request is paused until `release()` is called, which is exactly
 * the window a slow server would create.
 */
async function holdSave(page) {
  await page.setRequestInterception(true);
  const held = { request: null };
  const handler = (request) => {
    if (!held.request && request.method() === "POST" && new URL(request.url()).pathname === "/api/mcp/servers") {
      held.request = request;
      return;
    }
    request.continue().catch(() => {});
  };
  page.on("request", handler);
  return {
    held,
    release: async () => {
      page.off("request", handler);
      if (held.request) await held.request.continue().catch(() => {});
      await page.setRequestInterception(false).catch(() => {});
    },
  };
}

const fill = (page, id, value) => page.evaluate((fieldId, val) => {
  const field = document.getElementById(fieldId);
  if (!field) return false;
  field.value = val;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  field.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}, id, value);

const click = (page, id) => page.evaluate((nodeId) => {
  const node = document.getElementById(nodeId);
  if (!node) return false;
  node.click();
  return true;
}, id);

const tick = (page, id, checked = true) => page.evaluate((nodeId, state) => {
  const node = document.getElementById(nodeId);
  if (!node) return false;
  node.checked = state;
  node.dispatchEvent(new Event("change", { bubbles: true }));
  return node.checked;
}, id, checked);

const openGuide = async (page) => {
  await page.evaluate(() => {
    if (typeof openMcpModal === "function") openMcpModal();
    if (typeof openMcpEditor === "function") openMcpEditor();
  });
  await page.waitForFunction(
    () => Boolean(document.getElementById("mcpModal") && document.getElementById("mcpModal").classList.contains("open"))
      && Boolean(document.getElementById("mcpEditor")),
    { timeout: 30000 },
  );
  await sleep(300);
};

const waitForLanding = async (page, predicate, timeoutMs = 120000) => {
  await page.waitForFunction(predicate, { timeout: timeoutMs });
  await sleep(250);
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
  const wanted = args.states ? new Set(args.states.split(",").map((name) => name.trim()).filter(Boolean)) : null;
  const state = (name) => !wanted || wanted.has(name);
  if (wanted) {
    const unknown = [...wanted].filter((name) => !STATES.includes(name));
    if (unknown.length) {
      console.error(`unknown state(s): ${unknown.join(", ")} — this walk knows ${STATES.join(", ")}`);
      process.exit(2);
    }
  }

  /** In a capture-only run the tree under test may predate the wizard, so nothing is awaited. */
  const land = async (page, predicate) => {
    if (args.captureOnly) {
      await sleep(1200);
      return;
    }
    await waitForLanding(page, predicate);
  };

  /** One review row's value, or "" when the tree under test has no such row. */
  const row = (list, term) => {
    const hit = (list || []).find((entry) => entry.term === term);
    return hit ? hit.value : "";
  };

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
  const consoleText = [];
  const secretConsole = [];
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
      const text = message.text();
      consoleText.push(text);
      if (text.includes("sk-uiv2-22")) secretConsole.push(text);
      if (message.type() === "error" && !/status of 40[34]|status of 50[0-9]|ERR_CONNECTION_REFUSED|Failed to load resource/.test(text)) {
        pageErrors.push(text);
      }
    });
    page.on("request", (request) => {
      if (request.method() === "POST" || request.method() === "DELETE") {
        posts.push({ path: new URL(request.url()).pathname, body: request.postData() || "" });
      }
    });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await page.evaluateOnNewDocument((sessionToken, key, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, sessionToken);
      localStorage.setItem("m5_theme", theme);
    }, token, args.tokenKey, args.theme, origin);
    await page.goto(origin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 8);

    // ── 1: the guide exists, starts at step 1, and refuses an empty step ─────
    if (state("empty")) {
      await openGuide(page);
      const opened = await readWizard(page);
      record(
        "the editor is a four-step guide, on the Connected tab, starting at step 1",
        opened.open === true
          && opened.activeTab === "connected"
          && opened.step === 1
          && opened.steps.length === 4
          && opened.steps.map((step) => step.step).join(",") === "1,2,3,4",
        `tab=${opened.activeTab} step=${opened.step} rail=${opened.steps.map((step) => step.label).join(" · ")}`,
      );
      record(
        "one step is on screen, and the rail says which",
        opened.visiblePanels.join(",") === "1"
          && opened.steps.filter((step) => step.current).length === 1
          && opened.steps.find((step) => step.current).step === 1
          && /^Step 1 of 4 · /.test(opened.progress)
          && opened.progressRole === "status",
        `visible=${opened.visiblePanels.join(",")} current=${(opened.steps.find((step) => step.current) || {}).step} · "${opened.progress}"`,
      );
      await click(page, "mcpWizardNext");
      await sleep(200);
      const refused = await readWizard(page);
      record(
        "an empty guide refuses to advance, says why, and puts the cursor in the field",
        refused.step === 1 && /alias/i.test(refused.hint) && refused.focused === "mcpAlias",
        `step=${refused.step} hint="${refused.hint}" focused=${refused.focused}`,
      );
      await fill(page, "mcpAlias", "not a legal alias!");
      await click(page, "mcpWizardNext");
      await sleep(200);
      const badAlias = await readWizard(page);
      record(
        "an alias the harness could not name is refused with its own reason",
        badAlias.step === 1 && /letters, digits, hyphens and underscores/.test(badAlias.hint),
        `"${badAlias.hint}"`,
      );
      await fill(page, "mcpAlias", "wizard-notes");
      await sleep(150);
      const cleared = await readWizard(page);
      record(
        "the complaint clears as soon as the field satisfies it",
        cleared.hint === "",
        `hint "${cleared.hint}"`,
      );
      await shoot(page, `mcp-wizard-empty-${args.viewport}-${args.theme}`);
    }

    // ── 2: transport and credentials ────────────────────────────────────────
    if (state("credentials")) {
      await fill(page, "mcpAlias", "wizard-notes");
      await click(page, "mcpWizardNext");
      await sleep(250);
      const step2 = await readWizard(page);
      record(
        "step 2 asks how the server is reached, and shows only that transport's fields",
        step2.step === 2
          && step2.visiblePanels.join(",") === "2"
          && step2.transport.value === "http"
          && step2.transport.httpVisible === true
          && step2.transport.stdioVisible === false,
        `step=${step2.step} transport=${step2.transport.value} http=${step2.transport.httpVisible} stdio=${step2.transport.stdioVisible}`,
      );
      await fill(page, "mcpUrl", "mcp.context7.com/mcp");
      await click(page, "mcpWizardNext");
      await sleep(200);
      const badUrl = await readWizard(page);
      record(
        "a URL without a scheme is refused before anything is saved",
        badUrl.step === 2 && /must start with https/.test(badUrl.hint),
        `"${badUrl.hint}"`,
      );
      await fill(page, "mcpUrl", "https://mcp.example.com/mcp");
      await fill(page, "mcpToken", SECRET);
      await sleep(200);
      const withSecret = await readWizard(page);
      record(
        "the token lives in a password field that is never pre-filled and never auto-completed",
        withSecret.token.type === "password"
          && withSecret.token.autocomplete === "off"
          && withSecret.token.value === SECRET
          && withSecret.secretInDom === false,
        `type=${withSecret.token.type} autocomplete=${withSecret.token.autocomplete} in DOM=${withSecret.secretInDom}`,
      );
      record(
        "raw headers and environment JSON sit behind one closed advanced disclosure",
        withSecret.advanced.present === true
          && withSecret.advanced.open === false
          && withSecret.advanced.onGuidedPath === false
          && /raw headers/i.test(withSecret.advanced.summary),
        `open=${withSecret.advanced.open} on guided path=${withSecret.advanced.onGuidedPath} · "${withSecret.advanced.summary}"`,
      );
      await shoot(page, `mcp-wizard-credentials-${args.viewport}-${args.theme}`);
    }

    // ── 3: policy, and consent for a local command ──────────────────────────
    if (state("policy")) {
      await click(page, "mcpWizardNext");
      await sleep(250);
      const step3 = await readWizard(page);
      const effects = await page.evaluate(() => {
        const select = document.getElementById("mcpApprovalPolicy");
        const out = [];
        for (const value of ["auto", "inherit", "always_ask"]) {
          select.value = value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          const effect = document.getElementById("mcpPolicyEffect");
          out.push({ value, text: ((effect && effect.textContent) || "").trim() });
        }
        select.value = "inherit";
        select.dispatchEvent(new Event("change", { bubbles: true }));
        return out;
      });
      record(
        "the policy step explains what each policy costs, in words",
        step3.step === 3
          && step3.policy.options.join(",") === "auto,inherit,always_ask"
          && effects.every((entry) => entry.text.length > 30)
          && new Set(effects.map((entry) => entry.text)).size === 3,
        effects.map((entry) => `${entry.value}: ${entry.text.slice(0, 40)}…`).join(" | "),
      );
      record(
        "a remote server needs no consent, and says so",
        step3.consent.rowVisible === false && step3.consent.noteVisible === false,
        `consent row visible=${step3.consent.rowVisible}`,
      );
      await shoot(page, `mcp-wizard-policy-${args.viewport}-${args.theme}`);
    }

    // ── 4: the review, and a typed secret that is never echoed ──────────────
    if (state("review")) {
      await click(page, "mcpWizardNext");
      await sleep(250);
      const review = await readWizard(page);
      const terms = review.review.map((entry) => entry.term);
      record(
        "the last step reviews alias, transport, target, credential, policy and consent",
        review.step === 4
          && terms.join(",") === "Alias,Display name,Transport,Target,Credential,Approval policy,Local command consent",
        terms.join(" · "),
      );
      record(
        "a typed token is described, never echoed, and appears nowhere in the page",
        row(review.review, "Credential") === "A new token will be stored, and only its mask shown"
          && review.secretInDom === false
          && !JSON.stringify({ rows: review.review, result: review.result, confirm: review.confirm }).includes("sk-uiv2-22"),
        `${row(review.review, "Credential")} · in DOM=${review.secretInDom}`,
      );
      record(
        "the review says the connection will be tested, where the button is",
        /connection test/i.test(review.save.label + (await page.evaluate(() => document.getElementById("mcpPresetNote").textContent || "")))
          || /asks it for its tools/i.test(await page.evaluate(() => (document.querySelector("#mcpStep4 p.mcp-hub-hint") || {}).textContent || "")),
        `button "${review.save.label}"`,
      );
      await shoot(page, `mcp-wizard-review-${args.viewport}-${args.theme}`);
    }

    // ── 5: the server list gets a server, once, with a visible test result ──
    if (state("loading")) {
      await page.evaluate(() => { const editor = document.getElementById("mcpEditor"); if (editor) editor.open = true; });
      await fill(page, "mcpTransport", "stdio");
      await fill(page, "mcpCommand", process.env.STUDIO_PYTHON || "python3");
      await fill(page, "mcpArgs", FIXTURE_SERVER);
      await page.evaluate(() => { if (typeof mcpWizardGo === "function") mcpWizardGo(3, { focus: false }); });
      await sleep(250);
      const consentStep = await readWizard(page);
      record(
        "a local command demands an explicit consent, naming the command",
        consentStep.consent.rowVisible === true
          && consentStep.consent.checked === false
          && consentStep.consent.command.includes(FIXTURE_SERVER.split("/").pop())
          && /STUDIO_ALLOW_MCP_STDIO/.test(consentStep.consent.note),
        `row=${consentStep.consent.rowVisible} checked=${consentStep.consent.checked} · "${consentStep.consent.command.slice(0, 80)}"`,
      );
      await click(page, "mcpWizardNext");
      await sleep(200);
      const blocked = await readWizard(page);
      record(
        "the guide will not advance past an unticked consent",
        blocked.step === 3 && /consent/i.test(blocked.hint) && blocked.focused === "mcpStdioConsent",
        `step=${blocked.step} hint="${blocked.hint}" focused=${blocked.focused}`,
      );
      record(
        "the consent says it cannot grant the server's own permission",
        /cannot grant that permission/.test(blocked.consent.note) && /does not weaken the sandbox/.test(blocked.consent.note),
        `"${blocked.consent.note.slice(0, 120)}…"`,
      );
      await shoot(page, `mcp-wizard-consent-${args.viewport}-${args.theme}`);
      await tick(page, "mcpStdioConsent", true);
      await click(page, "mcpWizardNext");
      await sleep(250);
      const beforeSave = await readWizard(page);
      record(
        "with consent given, the review records it and the transport",
        beforeSave.step === 4
          && row(beforeSave.review, "Local command consent") === "Given"
          && /stdio/i.test(row(beforeSave.review, "Transport")),
        `${row(beforeSave.review, "Local command consent")} · ${row(beforeSave.review, "Transport")}`,
      );
      const inflight = await holdSave(page);
      await click(page, "mcpSaveBtn");
      await sleep(700);
      const testing = await readWizard(page);
      record(
        "saving shows a testing state instead of a frozen button",
        testing.save.disabled === true
          && /Saving and testing/.test(testing.save.label)
          && testing.result.state === "testing"
          && testing.result.role === "status"
          && testing.result.hidden === false,
        `button "${testing.save.label}" disabled=${testing.save.disabled} · result ${testing.result.state} · "${testing.result.text.slice(0, 60)}"`,
      );
      await shoot(page, `mcp-wizard-loading-${args.viewport}-${args.theme}`);
      await inflight.release();
      await land(page, () => {
        const box = document.getElementById("mcpWizardResult");
        return Boolean(box) && !box.hidden && box.dataset.state !== "testing";
      });
      const landed = await readWizard(page);
      record(
        "the connection test reports what it found, and the confirmation names the tool namespace",
        landed.result.state === "ok"
          && /Connected — \d+ tools? discovered/.test(landed.result.text)
          && /mcp__wizard-notes__/.test(landed.confirm.text)
          && landed.confirm.hidden === false,
        `"${landed.result.text}" · "${landed.confirm.text.slice(0, 90)}"`,
      );
      record(
        "the button comes back, and the secret stayed out of the page and the console",
        landed.save.disabled === false
          && landed.secretInDom === false
          && secretConsole.length === 0
          && !JSON.stringify(consoleText).includes("sk-uiv2-22"),
        `disabled=${landed.save.disabled} · secret in DOM=${landed.secretInDom} · in console: ${secretConsole.length}`,
      );
      await shoot(page, `mcp-wizard-success-${args.viewport}-${args.theme}`);
      await click(page, "mcpWizardDoneBtn");
      await sleep(300);
      const afterDone = await readWizard(page);
      record(
        "the confirmation lands on the server list, with the server in it",
        afterDone.open === false
          && afterDone.rows.some((row) => row.alias === "wizard-notes" && /4 of 4 tools on|Discovered Tools/.test(row.text)),
        `editor open=${afterDone.open} · rows ${afterDone.rows.map((row) => row.alias).join(", ")}`,
      );
      record(
        "every control the old editor had is still there",
        afterDone.fields.length === 0,
        afterDone.fields.length ? `missing: ${afterDone.fields.join(", ")}` : "20 of 20 present",
      );
      await shoot(page, `mcp-wizard-populated-${args.viewport}-${args.theme}`);
    }

    // ── 6: a connection test that fails, and the recovery from it ───────────
    if (state("error")) {
      await openGuide(page);
      await fill(page, "mcpAlias", "wizard-dead");
      await click(page, "mcpWizardNext");
      await sleep(200);
      await fill(page, "mcpTransport", "http");
      await fill(page, "mcpAuthHeader", "none");
      await fill(page, "mcpUrl", DEAD_URL);
      await fill(page, "mcpToken", "");
      await click(page, "mcpWizardNext");
      await sleep(200);
      await click(page, "mcpWizardNext");
      await sleep(200);
      await click(page, "mcpWizardSubmitGuard");
      await click(page, "mcpSaveBtn");
      await land(page, () => {
        const box = document.getElementById("mcpWizardResult");
        return Boolean(box) && !box.hidden && box.dataset.state !== "testing";
      });
      const failed = await readWizard(page);
      record(
        "a failed connection test stays on the step and shows the server's own note",
        failed.result.state === "failed" && /connection test failed/i.test(failed.result.text) && failed.result.text.length > 60,
        `"${failed.result.text.slice(0, 140)}"`,
      );
      record(
        "the server is still saved, and the list says what it could not do",
        failed.rows.some((row) => row.alias === "wizard-dead" && /Sync note/.test(row.text)),
        failed.rows.map((row) => row.alias).join(", ") || "no rows",
      );
      await shoot(page, `mcp-wizard-error-${args.viewport}-${args.theme}`);

      // recovery: fix the transport in place and connect again
      await click(page, "mcpWizardBack");
      await click(page, "mcpWizardBack");
      await sleep(250);
      await fill(page, "mcpTransport", "stdio");
      await fill(page, "mcpCommand", process.env.STUDIO_PYTHON || "python3");
      await fill(page, "mcpArgs", FIXTURE_SERVER);
      await click(page, "mcpWizardNext");
      await sleep(250);
      await tick(page, "mcpStdioConsent", true);
      await click(page, "mcpWizardNext");
      await sleep(250);
      const beforeRetry = await readWizard(page);
      await click(page, "mcpSaveBtn");
      await land(page, () => {
        const box = document.getElementById("mcpWizardResult");
        return Boolean(box) && !box.hidden && box.dataset.state === "ok";
      });
      const recovered = await readWizard(page);
      record(
        "fixing the transport and connecting again turns the result green",
        recovered.result.state === "ok" && /Connected — \d+ tools? discovered/.test(recovered.result.text),
        `was ${beforeRetry.result.state} · "${recovered.result.text}"`,
      );
      record(
        "and it updates that server instead of adding a second one",
        recovered.rows.filter((row) => row.alias === "wizard-dead").length === 1,
        `${recovered.rows.filter((row) => row.alias === "wizard-dead").length} row(s) for wizard-dead`,
      );
      await shoot(page, `mcp-wizard-recovery-${args.viewport}-${args.theme}`);
      await click(page, "mcpWizardDoneBtn");
      await sleep(200);
    }

    // ── 7: unchanged-token semantics, on a stored secret ────────────────────
    if (state("keyboard")) {
      await openGuide(page);
      await fill(page, "mcpAlias", "wizard-keyed");
      await click(page, "mcpWizardNext");
      await sleep(200);
      await fill(page, "mcpTransport", "http");
      await fill(page, "mcpAuthHeader", "Authorization");
      await fill(page, "mcpUrl", DEAD_URL);
      await fill(page, "mcpToken", SECRET);
      await click(page, "mcpWizardNext");
      await sleep(200);
      await click(page, "mcpWizardNext");
      await sleep(200);
      await click(page, "mcpSaveBtn");
      await land(page, () => {
        const box = document.getElementById("mcpWizardResult");
        return Boolean(box) && !box.hidden && box.dataset.state !== "testing";
      });
      const stored = await readWizard(page);
      const keyedRow = stored.rows.find((row) => row.alias === "wizard-keyed");
      record(
        "a stored token is shown back only as its mask",
        Boolean(keyedRow) && /Token \S*•{2,}\S*/.test(keyedRow.text) && stored.secretInDom === false,
        keyedRow ? (keyedRow.text.match(/Token \S+/) || ["no mask"])[0] : "no row",
      );
      await page.evaluate(() => { const editor = document.getElementById("mcpEditor"); if (editor) editor.open = true; });
      await page.evaluate(() => { if (typeof editMcpServer === "function") editMcpServer("wizard-keyed"); });
      await sleep(350);
      const editing = await readWizard(page);
      record(
        "editing a server starts the guide at step 1 with the token field empty",
        editing.step === 1 && editing.token.value === "",
        `step=${editing.step} token "${editing.token.value}"`,
      );
      await click(page, "mcpWizardTab4");
      await sleep(250);
      const keptReview = await readWizard(page);
      record(
        "the review says the stored token is kept when the field is blank",
        /stored token is kept \(/i.test(row(keptReview.review, "Credential")),
        row(keptReview.review, "Credential"),
      );
      await click(page, "mcpSaveBtn");
      await land(page, () => {
        const box = document.getElementById("mcpWizardResult");
        return Boolean(box) && !box.hidden && box.dataset.state !== "testing";
      });
      const kept = await readWizard(page);
      record(
        "saving with the field blank preserves the stored secret's mask",
        Boolean(kept.rows.find((row) => row.alias === "wizard-keyed" && /Token \S*•{2,}/.test(row.text))),
        (kept.rows.find((row) => row.alias === "wizard-keyed") || {}).text ? "mask kept" : "mask lost",
      );
      await shoot(page, `mcp-wizard-keyed-${args.viewport}-${args.theme}`);

      // ── keyboard: the whole path, from the rail to the last step ──
      await page.evaluate(() => {
        const editor = document.getElementById("mcpEditor");
        if (editor) editor.open = true;
        if (typeof mcpWizardGo === "function") mcpWizardGo(1, { focus: false });
        const alias = document.getElementById("mcpAlias");
        alias.value = "wizard-keys";
        alias.dispatchEvent(new Event("input", { bubbles: true }));
        alias.focus();
      });
      await page.keyboard.press("Tab");
      const focusedAfterTab = await page.evaluate(() => document.activeElement.id || "");
      await page.evaluate(() => { const next = document.getElementById("mcpWizardNext"); if (next) next.focus(); });
      await page.keyboard.press("Enter");
      await sleep(250);
      const afterEnter = await readWizard(page);
      record(
        "the guide is operable from the keyboard: Tab reaches the controls, Enter advances, focus lands on the heading",
        afterEnter.step === 2 && afterEnter.focused === "mcpWizardHeading2",
        `tab -> ${focusedAfterTab} · step=${afterEnter.step} focused=${afterEnter.focused}`,
      );
      await page.keyboard.down("Shift");
      await page.keyboard.press("Tab");
      await page.keyboard.up("Shift");
      const shiftTab = await page.evaluate(() => document.activeElement.id || "");
      await page.evaluate(() => { const back = document.getElementById("mcpWizardBack"); if (back) back.focus(); });
      await page.keyboard.press("Enter");
      await sleep(250);
      const backAgain = await readWizard(page);
      record(
        "and Back returns without losing what was typed",
        backAgain.step === 1 && shiftTab.length > 0
          && (await page.evaluate(() => document.getElementById("mcpAlias").value)) === "wizard-keys",
        `step=${backAgain.step} alias kept=${(await page.evaluate(() => document.getElementById("mcpAlias").value))}`,
      );
      await shoot(page, `mcp-wizard-keyboard-${args.viewport}-${args.theme}`);
    }

    if (state("long")) {
      await page.evaluate(() => {
        const editor = document.getElementById("mcpEditor");
        if (editor) editor.open = true;
        if (typeof mcpWizardGo === "function") mcpWizardGo(2, { focus: false });
      });
      await fill(page, "mcpTransport", "stdio");
      await fill(page, "mcpCommand", "/usr/local/bin/a-very-long-command-name-that-keeps-going-and-going-" + "x".repeat(60));
      await fill(page, "mcpArgs", "--flag " + "argument-".repeat(20));
      await page.evaluate(() => { if (typeof mcpWizardGo === "function") mcpWizardGo(4, { focus: false }); });
      await sleep(300);
      const longTarget = await readWizard(page);
      record(
        "a long command wraps inside the review instead of pushing the dialog sideways",
        longTarget.horizontalOverflow === false
          && row(longTarget.review, "Target").length > 120,
        `overflow=${longTarget.horizontalOverflow} · target ${row(longTarget.review, "Target").length} chars`,
      );
      await shoot(page, `mcp-wizard-long-${args.viewport}-${args.theme}`);
    }

    record("the guide raises no console error", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
    record(
      "no console line carries the token",
      secretConsole.length === 0,
      secretConsole.slice(0, 1).join(" | ") || `${consoleText.length} console line(s), none with the secret`,
    );
    const unexpected = [...new Set(posts.map((entry) => entry.path))].filter((path) => !path.startsWith("/api/mcp/"));
    record(
      "the guide posts only to MCP endpoints, and only the token field carries the secret",
      unexpected.length === 0,
      [...new Set(posts.map((entry) => entry.path))].join(" · ") || "no POST",
    );
    record(
      "the secret is never sent anywhere but the save request's own body",
      posts.every((entry) => entry.path === "/api/mcp/servers" || !entry.body.includes("sk-uiv2-22")),
      `${posts.filter((entry) => entry.body.includes("sk-uiv2-22")).map((entry) => entry.path).join(", ") || "no other body carries it"}`,
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
