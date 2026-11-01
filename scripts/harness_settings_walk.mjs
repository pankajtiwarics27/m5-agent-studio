#!/usr/bin/env node
/**
 * Check the Harness's task creation and settings hierarchy in a browser (UIUX-P3-01).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/harness_settings_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The demo has no model runtime, so this walk cannot let a task finish. It does the next
 * honest thing: it reads the plan the composer prints, changes each control it claims to
 * read and re-reads it, reaches the failure by pressing Run with no task, and hands a real
 * conversation from Chat to the Harness to reach the banner that answers it.
 *
 * What it proves:
 *
 *   1. the plan is printed with its four decisions and no row is empty;
 *   2. every control the run payload sends is read by the plan - changing the workflow
 *      mode, the policy, the worktree box, a model and the directory each move the row
 *      that claims them;
 *   3. the collapsed advanced group states its contents while closed, and says in words
 *      that a setting changed when one does;
 *   4. Run is off with no task and the sentence beside it says why; typing a task turns it
 *      on and the sentence changes; the mode the board owns needs no task;
 *   5. pressing Run with no task writes nothing and puts the cursor back in the composer;
 *   6. a conversation handed over from Chat raises the banner with its counts, the banner
 *      says when this run departs from it, and it goes when the composer is emptied;
 *   7. + New session clears the banner;
 *   8. the controls are named for assistive technology and the advanced group opens from
 *      the keyboard;
 *   9. the plan survives a 900px viewport and no page error occurs.
 */
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = { tokenKey: "m5_api_token", settleMs: 250, timeoutMs: 45000 };

function parseArgs(argv) {
  const args = { ...DEFAULTS, baseUrl: "", token: "", json: false, screenshots: "" };
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
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--json": args.json = true; break;
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
    "Check the Harness's plan, Run control and carried brief against a live pane.",
    "",
    "  node scripts/harness_settings_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-harness-settings.md.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
    resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
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
    `puppeteer-core is not importable. Install it (npm i puppeteer-core) or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`,
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
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This script refuses to report a walk it did not perform.",
  );
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function readPlan(page) {
  return page.evaluate(() => {
    const rows = {};
    for (const row of document.querySelectorAll("#harnessRunPlanBody .harness-plan-row")) {
      rows[row.dataset.plan] = {
        value: row.querySelector(".harness-plan-value").textContent.trim(),
        changed: row.dataset.changed,
      };
    }
    const hint = document.getElementById("harnessRunHint");
    const button = document.getElementById("harnessRunBtn");
    return {
      rows,
      order: [...document.querySelectorAll("#harnessRunPlanBody .harness-plan-row")].map((row) => row.dataset.plan),
      advancedSummary: document.getElementById("harnessAdvancedSummary").textContent.trim(),
      advancedChanged: document.getElementById("harnessAdvancedSummary").dataset.changed,
      advancedOpen: document.getElementById("harnessAdvanced").open,
      hint: hint.textContent.trim(),
      hintReason: hint.dataset.reason,
      runDisabled: button.disabled,
      runLabel: button.textContent.trim(),
      groupLabel: document.getElementById("harnessRunPlan").getAttribute("aria-label"),
    };
  });
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--hide-scrollbars",
      "--force-color-profile=srgb",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean),
    ],
  });
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const shoot = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
  };
  // The composer states are about the composer: a clipped capture of it is legible at the
  // size a reader actually reads, and it still shows the real element rather than a mock-up.
  const shootComposer = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    const box = await page.evaluate(() => {
      const node = document.querySelector(".hall-composer");
      if (!node) return null;
      node.scrollIntoView({ block: "end" });
      const rect = node.getBoundingClientRect();
      return { x: Math.max(0, rect.x), y: Math.max(0, rect.y), width: rect.width, height: rect.height };
    });
    if (!box) return shoot(page, name);
    await page.screenshot({
      path: resolve(args.screenshots, `${name}.png`),
      clip: { x: box.x, y: box.y, width: box.width, height: box.height },
    });
  };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.evaluateOnNewDocument((key, token) => {
      try {
        window.sessionStorage.setItem(key, token);
      } catch (_error) {}
    }, args.tokenKey, args.token);
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);

    // ── The plan, as it stands ────────────────────────────────────────────
    const initial = await readPlan(page);
    record(
      "the plan prints its four decisions, in order, and none of them is empty",
      initial.order.join(",") === "directory,policy,models,workflow"
        && Object.values(initial.rows).every((row) => row.value.length > 0)
        && initial.groupLabel === "What Run will do",
      initial.order.map((key) => `${key}=${initial.rows[key].value.slice(0, 42)}`).join(" | "),
    );
    record(
      "the directory row names the project directory and the isolation mode",
      initial.rows.directory.value.length > 0
        && /· (direct|isolated git worktree)$/.test(initial.rows.directory.value),
      initial.rows.directory.value,
    );
    record(
      "the policy row is the option's own label, with no second description",
      initial.rows.policy.value.length > 0 && !initial.rows.policy.value.includes(" - "),
      initial.rows.policy.value,
    );
    record(
      "the models row names the lead and the critic",
      /critic (same|none|.+)/.test(initial.rows.models.value),
      initial.rows.models.value,
    );
    record(
      "the workflow row carries the three settings the collapsed group holds",
      initial.rows.workflow.value.split(" · ").length >= 4
        && initial.advancedSummary.length > 0
        && initial.rows.workflow.value.includes(initial.advancedSummary),
      `workflow="${initial.rows.workflow.value}" summary="${initial.advancedSummary}"`,
    );
    await shoot(page, "plan");

    // ── Each control the payload sends moves the row that claims it ────────
    const changedByControl = await page.evaluate(() => {
      const change = (id, value) => {
        const node = document.getElementById(id);
        node.value = value;
        node.dispatchEvent(new Event("change"));
        const row = (key) => document.querySelector(`#harnessRunPlanBody [data-plan="${key}"] .harness-plan-value`).textContent.trim();
        return row;
      };
      const out = {};
      out.policyBefore = document.querySelector('[data-plan="policy"] .harness-plan-value').textContent.trim();
      change("harnessPolicy", "plan");
      out.policy = document.querySelector('[data-plan="policy"] .harness-plan-value').textContent.trim();
      out.worktree = (() => {
        const box = document.getElementById("harnessUseWorktree");
        box.checked = true;
        box.dispatchEvent(new Event("change"));
        return document.querySelector('[data-plan="directory"] .harness-plan-value').textContent.trim();
      })();
      out.mode = (() => {
        const select = document.getElementById("harnessWorkflowMode");
        select.value = "gsd";
        select.dispatchEvent(new Event("change"));
        return {
          row: document.querySelector('[data-plan="workflow"] .harness-plan-value').textContent.trim(),
          label: document.getElementById("harnessRunBtn").textContent.trim(),
          hint: document.getElementById("harnessRunHint").dataset.reason,
        };
      })();
      out.steps = (() => {
        const select = document.getElementById("harnessMaxSteps");
        select.value = "24";
        select.dispatchEvent(new Event("change"));
        return {
          row: document.querySelector('[data-plan="workflow"] .harness-plan-value').textContent.trim(),
          changed: document.querySelector('[data-plan="workflow"]').dataset.changed,
          summary: document.getElementById("harnessAdvancedSummary").textContent.trim(),
          summaryChanged: document.getElementById("harnessAdvancedSummary").dataset.changed,
          closed: document.getElementById("harnessAdvanced").open,
        };
      })();
      out.directory = (() => {
        const input = document.getElementById("harnessWorkspaceInput");
        input.value = "/tmp/p3-01-walk-project";
        input.dispatchEvent(new Event("change"));
        return document.querySelector('[data-plan="directory"] .harness-plan-value').textContent.trim();
      })();
      return out;
    });
    record(
      "changing the policy moves the policy row",
      changedByControl.policyBefore !== changedByControl.policy
        && /Plan & Audit/.test(changedByControl.policy),
      `${changedByControl.policyBefore} -> ${changedByControl.policy}`,
    );
    record(
      "checking the worktree box says so in the directory row",
      /isolated git worktree$/.test(changedByControl.worktree),
      changedByControl.worktree,
    );
    record(
      "changing the workflow mode moves the row and the Run button together",
      /GSD 3-Wave/.test(changedByControl.mode.row) && changedByControl.mode.label === "Run 3-Wave",
      `row="${changedByControl.mode.row.slice(0, 60)}" button="${changedByControl.mode.label}"`,
    );
    record(
      "changing a setting inside the collapsed group marks it changed in words, while closed",
      changedByControl.steps.changed === "true"
        && /24 steps/.test(changedByControl.steps.row)
        && /changed/.test(changedByControl.steps.row)
        && changedByControl.steps.summaryChanged === "true"
        && /changed/.test(changedByControl.steps.summary)
        && changedByControl.steps.closed === false,
      `row="${changedByControl.steps.row.slice(0, 70)}" summary="${changedByControl.steps.summary}"`,
    );
    record(
      "changing the directory moves the directory row",
      changedByControl.directory.startsWith("/tmp/p3-01-walk-project"),
      changedByControl.directory,
    );
    const modelsRow = await page.evaluate(() => {
      const select = document.getElementById("harnessCriticModel");
      const other = [...select.options]
        .map((option) => option.value)
        .find((value) => value && value !== "__custom__" && value !== select.value);
      if (!other) return { skipped: true };
      select.value = other;
      select.dispatchEvent(new Event("change"));
      return { row: document.querySelector('[data-plan="models"] .harness-plan-value').textContent.trim() };
    });
    record(
      "changing the critic moves the models row",
      modelsRow.skipped === true || (/critic/.test(modelsRow.row) && !/critic same/.test(modelsRow.row)),
      modelsRow.skipped ? "only one model in the catalog, so the row cannot differ" : modelsRow.row,
    );

    // ── Run with no task is a sentence, not silence ───────────────────────
    await page.evaluate(() => {
      for (const id of ["harnessWorkflowMode", "harnessPolicy", "harnessMaxSteps", "harnessWorkspaceInput"]) {
        const node = document.getElementById(id);
        node.value = id === "harnessMaxSteps" ? "10" : id === "harnessWorkflowMode" ? "turn" : id === "harnessPolicy" ? "standard" : node.value;
        node.dispatchEvent(new Event("change"));
      }
      const box = document.getElementById("harnessUseWorktree");
      box.checked = false;
      box.dispatchEvent(new Event("change"));
      const composer = document.getElementById("harnessPromptInput");
      composer.value = "";
      composer.dispatchEvent(new Event("input"));
    });
    await sleep(args.settleMs);
    const empty = await readPlan(page);
    const emptyPress = await page.evaluate(() => {
      const composer = document.getElementById("harnessPromptInput");
      const before = { turns: document.querySelectorAll("#harnessStream .h-turn-user").length };
      const returned = executeHarnessSelectedWorkflow();
      return {
        returned,
        focused: document.activeElement === composer,
        turns: document.querySelectorAll("#harnessStream .h-turn-user").length,
        before: before.turns,
        disabled: document.getElementById("harnessRunBtn").disabled,
      };
    });
    record(
      "with no task Run is off and the sentence beside it says why",
      empty.runDisabled === true
        && empty.hintReason === "no-task"
        && /describe the task/i.test(empty.hint)
        && /describe the task/i.test(await page.evaluate(() => document.getElementById("harnessPromptInput").getAttribute("aria-describedby") ? document.getElementById("harnessRunHint").textContent : "")),
      `${empty.hintReason}: ${empty.hint}`,
    );
    record(
      "pressing Run with no task starts nothing and puts the cursor in the composer",
      emptyPress.returned === null && emptyPress.turns === emptyPress.before && emptyPress.focused === true,
      `returned=${emptyPress.returned} turns=${emptyPress.turns} focused=${emptyPress.focused}`,
    );
    await shootComposer(page, "no-task");

    const typed = await page.evaluate(() => {
      const composer = document.getElementById("harnessPromptInput");
      composer.value = "Add a /health endpoint and a test for it";
      composer.dispatchEvent(new Event("input"));
      return {
        disabled: document.getElementById("harnessRunBtn").disabled,
        reason: document.getElementById("harnessRunHint").dataset.reason,
        hint: document.getElementById("harnessRunHint").textContent.trim(),
      };
    });
    record(
      "typing a task turns Run on and the sentence changes",
      typed.disabled === false && typed.reason === "ready" && /one task/i.test(typed.hint),
      `${typed.reason}: ${typed.hint}`,
    );

    const board = await page.evaluate(() => {
      const select = document.getElementById("harnessWorkflowMode");
      select.value = "sdlc_modal";
      select.dispatchEvent(new Event("change"));
      const composer = document.getElementById("harnessPromptInput");
      composer.value = "";
      composer.dispatchEvent(new Event("input"));
      return {
        disabled: document.getElementById("harnessRunBtn").disabled,
        reason: document.getElementById("harnessRunHint").dataset.reason,
        hint: document.getElementById("harnessRunHint").textContent.trim(),
        label: document.getElementById("harnessRunBtn").textContent.trim(),
      };
    });
    record(
      "the board mode needs no task, and says so",
      board.disabled === false && board.reason === "board" && /No task is needed/i.test(board.hint),
      `${board.label}: ${board.hint}`,
    );
    await page.evaluate(() => {
      const select = document.getElementById("harnessWorkflowMode");
      select.value = "turn";
      select.dispatchEvent(new Event("change"));
    });

    // ── The brief from Chat ───────────────────────────────────────────────
    const handed = await page.evaluate(() => {
      switchTab("chat");
      const model = document.getElementById("chatModel");
      if (model) {
        model.innerHTML = '<option value="demo-local-model">demo-local-model</option>';
        model.value = "demo-local-model";
      }
      chatHistory = [
        { role: "user", content: "Which tool failed, and then hand this to the Harness." },
        {
          role: "assistant",
          model: "demo-local-model",
          content: "read_file on src/missing.py was refused by the workspace guard.",
          tools: [
            { step: 1, tool: "grep_search", args: { query: "health" }, ok: true, elapsed_ms: 41, preview: "src/app.py:1: health" },
            { step: 2, tool: "read_file", args: { path: "src/missing.py" }, ok: false, elapsed_ms: 7, preview: "Error: file not found" },
          ],
          context: {
            model: "demo-local-model",
            persona: "Principal Engineer",
            reasoning: "off",
            tools: true,
            skills: 0,
            messages: 2,
            workspace_effective: "/tmp/chat-read-project",
          },
        },
      ];
      renderChatMessages();
      handOffChatToHarness();
      const host = document.getElementById("harnessIncomingBrief");
      return {
        hidden: host.hidden,
        match: host.dataset.match,
        text: document.getElementById("harnessIncomingBriefText").textContent.trim(),
        tab: (document.querySelector('.mode-tab[aria-selected="true"]') || {}).dataset.tab,
        reason: document.getElementById("harnessRunHint").dataset.reason,
      };
    });
    await sleep(args.settleMs * 2);
    record(
      "a conversation handed over from Chat raises the banner with its counts",
      handed.hidden === false
        && handed.tab === "harness"
        && /2 messages, 2 tool calls \(1 failed\)/.test(handed.text)
        && /Nothing starts until you press Run/.test(handed.text),
      handed.text.slice(0, 150),
    );
    record(
      "the banner says where this run departs from the conversation",
      handed.match === "mismatch"
        && /changes \w+ setting/.test(handed.text)
        && /demo-local-model ->/.test(handed.text)
        && /\/tmp\/chat-read-project ->/.test(handed.text),
      handed.match,
    );
    await shootComposer(page, "brief-from-chat");

    const aligned = await page.evaluate(() => {
      const model = document.getElementById("harnessLeadModel");
      const wanted = "demo-local-model";
      if (![...model.options].some((option) => option.value === wanted)) {
        const option = document.createElement("option");
        option.value = wanted;
        option.textContent = wanted;
        model.appendChild(option);
      }
      model.value = wanted;
      model.dispatchEvent(new Event("change"));
      const box = document.getElementById("harnessWorkspaceInput");
      box.value = "/tmp/chat-read-project";
      box.dispatchEvent(new Event("change"));
      const host = document.getElementById("harnessIncomingBrief");
      return {
        match: host.dataset.match,
        text: document.getElementById("harnessIncomingBriefText").textContent.trim(),
      };
    });
    record(
      "when the run matches the brief the banner says that instead",
      aligned.match === "match" && /same model and project directory/.test(aligned.text),
      aligned.text.slice(0, 150),
    );
    await shootComposer(page, "brief-aligned");

    const fitted = await page.evaluate(() => {
      const box = document.getElementById("harnessPromptInput");
      const line = parseFloat(getComputedStyle(box).lineHeight) || 18;
      const brief = { scroll: box.scrollHeight, client: box.clientHeight };
      box.value = "";
      box.dispatchEvent(new Event("input"));
      const rest = { client: box.clientHeight };
      return { brief, rest, resize: getComputedStyle(box).resize, line };
    });
    record(
      "a carried brief is shown in full, and the box rests again once the task goes",
      fitted.brief.scroll <= fitted.brief.client + 2
        && fitted.brief.client > fitted.rest.client
        && fitted.rest.client <= fitted.line * 3 + 2
        && fitted.resize === "none",
      `brief ${fitted.brief.client}px of ${fitted.brief.scroll}px, rest ${fitted.rest.client}px (line ${fitted.line}px), resize=${fitted.resize}`,
    );

    const cleared = await page.evaluate(() => {
      const composer = document.getElementById("harnessPromptInput");
      composer.value = "";
      composer.dispatchEvent(new Event("input"));
      const afterEmpty = document.getElementById("harnessIncomingBrief").hidden;
      handOffChatToHarness();
      const afterHandoff = document.getElementById("harnessIncomingBrief").hidden;
      startFreshHarnessDraft();
      return {
        afterEmpty,
        afterHandoff,
        afterFresh: document.getElementById("harnessIncomingBrief").hidden,
        reason: document.getElementById("harnessRunHint").dataset.reason,
      };
    });
    record(
      "the banner goes when the task goes: emptied, handed again, or a new session",
      cleared.afterEmpty === true && cleared.afterHandoff === false && cleared.afterFresh === true,
      `emptied=${cleared.afterEmpty} re-handed=${cleared.afterHandoff} new session=${cleared.afterFresh}`,
    );

    // ── Keyboard and labels ───────────────────────────────────────────────
    const keyboard = await page.evaluate(() => {
      const named = (id) => {
        const node = document.getElementById(id);
        if (!node) return { id, named: false };
        const label = node.id ? document.querySelector(`label[for="${node.id}"]`) : null;
        const aria = node.getAttribute("aria-label");
        const described = node.getAttribute("aria-describedby");
        const describedText = described ? (document.getElementById(described) || {}).textContent || "" : "";
        return { id, named: Boolean(label || aria), described: Boolean(described && describedText.trim()) };
      };
      const ids = ["harnessLeadModel", "harnessCriticModel", "harnessPolicy", "harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps", "harnessWorkflowMode"];
      const summary = document.getElementById("harnessAdvanced").querySelector("summary");
      summary.focus();
      const focusable = document.activeElement === summary;
      return { controls: ids.map(named), composer: named("harnessPromptInput"), focusable };
    });
    record(
      "every control the plan reads is named for assistive technology",
      keyboard.controls.every((row) => row.named) && keyboard.composer.named && keyboard.composer.described,
      [
        ...keyboard.controls.filter((row) => !row.named).map((row) => row.id),
        ...(keyboard.composer.named ? [] : ["harnessPromptInput: unnamed"]),
        ...(keyboard.composer.described ? [] : ["harnessPromptInput: hints not described"]),
      ].join(", ") || "all named and described",
    );
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 2);
    const opened = await page.evaluate(() => ({
      open: document.getElementById("harnessAdvanced").open,
      summary: document.getElementById("harnessAdvancedSummary").textContent.trim(),
    }));
    const closedAgain = await page.evaluate(() => {
      const summary = document.getElementById("harnessAdvanced").querySelector("summary");
      summary.focus();
      return document.activeElement === summary;
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 2);
    const afterClose = await page.evaluate(() => document.getElementById("harnessAdvanced").open);
    record(
      "the collapsed group opens and closes from the keyboard and keeps its summary",
      keyboard.focusable === true && opened.open === true && opened.summary.length > 0
        && closedAgain === true && afterClose === false,
      `focusable=${keyboard.focusable} opened=${opened.open} closed=${!afterClose}`,
    );

    // ── Narrow ────────────────────────────────────────────────────────────
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const composer = document.querySelector(".hall-composer");
      const plan = document.getElementById("harnessRunPlan");
      const hint = document.getElementById("harnessRunHint");
      const button = document.getElementById("harnessRunBtn");
      const box = plan.getBoundingClientRect();
      return {
        overflow: composer ? composer.scrollWidth > composer.clientWidth + 1 : true,
        planVisible: box.height > 10 && box.width > 100,
        hintVisible: hint.getBoundingClientRect().width > 60,
        buttonsVisible: button.getBoundingClientRect().width > 40,
        rows: document.querySelectorAll("#harnessRunPlanBody .harness-plan-row").length,
      };
    });
    record(
      "the plan survives a 900px viewport without overflowing the composer",
      narrow.overflow === false && narrow.planVisible && narrow.hintVisible && narrow.buttonsVisible
        && narrow.rows === 4,
      `overflow=${narrow.overflow} plan ${narrow.planVisible} hint ${narrow.hintVisible} rows=${narrow.rows}`,
    );
    await shoot(page, "plan-narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    // The walk changed only composer state in a page it then closed: no session was
    // created, no task ran, and the project directory it typed was never saved.
    await browser.close();
  }

  const blocked = results.filter((row) => row.status === "blocked");
  const failed = results.filter((row) => row.status === "fail");
  const passed = results.filter((row) => row.status === "pass");
  if (args.json) {
    console.log(JSON.stringify({ results, passed: passed.length, failed: failed.length, blocked: blocked.length }, null, 2));
  } else {
    console.log(
      `\n${passed.length} of ${results.length} checks passed`
      + (blocked.length ? `, ${blocked.length} blocked on a prerequisite` : ""),
    );
  }
  if (failed.length > 0) {
    throw new Error(`${failed.length} harness settings check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`harness_settings_walk: ${error.message}`);
  process.exit(1);
});
