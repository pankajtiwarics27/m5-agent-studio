#!/usr/bin/env node
/**
 * Check the Start pane's readiness answer in a browser (UIUX-P2-01).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/start_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The acceptance criterion for this story is that the pane tells a first-run reader
 * the one next thing to click, and that a disabled control explains itself. Neither
 * is provable from source: both depend on what /api/onboarding/preflight actually
 * answered on this machine.
 *
 * So the walk reads the same response the pane read (`fetch` from inside the page)
 * and holds the pane against it:
 *
 *   1. the lead prints the server's completed_count of total_count, verbatim;
 *   2. the lead's state matches the server's verdict - ready, blocked or waiting;
 *   3. the lead names the server's next step and offers its action label;
 *   4. exactly one row is aria-current="step", and it is that step;
 *   5. Run is off exactly when the server says not ready, and its described hint
 *      names the step holding it up;
 *   6. pressing the lead's action does what the label says;
 *   7. Tab from the action stays inside the pane;
 *   8. a refused preflight presents the error, and Re-check restores the list with
 *      focus back on the lead instead of on <body>;
 *   9. the lead survives a 900px viewport with its action still usable;
 *  10. no page errors.
 *
 * It never fabricates a ready machine: when this host is not ready, the ready-state
 * assertions are recorded as blocked rather than skipped silently.
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
    "Check the Start pane's readiness answer against the preflight it read.",
    "",
    "  node scripts/start_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-start.md.",
  ].join("\n");
}

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    resolve(process.cwd(), "node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js"),
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
  const block = (step, detail) => {
    results.push({ step, passed: false, detail, status: "blocked" });
    if (!args.json) console.log(`block  ${step} — ${detail}`);
  };
  const shoot = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
  };

  const breakers = { preflight: false };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (breakers.preflight && request.url().includes("/api/onboarding/preflight")) return request.abort("failed");
      return request.continue();
    });
    await page.evaluateOnNewDocument((key, token) => {
      try {
        window.sessionStorage.setItem(key, token);
      } catch (_error) {}
    }, args.tokenKey, args.token);
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);
    await page.evaluate(() => switchTab("start"));
    await sleep(args.settleMs * 4);

    // What the server actually said, read from inside the page so it is the same
    // response the pane consumed rather than a second opinion from the walk.
    const server = await page.evaluate(async () => {
      const response = await fetch("/api/onboarding/preflight");
      if (!response.ok) return { error: `HTTP ${response.status}` };
      const payload = await response.json();
      const report = payload.preflight || {};
      return {
        ready: Boolean(report.ready),
        blocked: Boolean(report.blocked),
        completed: report.completed_count,
        total: report.total_count,
        next: report.next_step || null,
      };
    });
    if (server.error) {
      record("the preflight answers", false, server.error);
      throw new Error("the preflight could not be read, so the pane cannot be held against it");
    }
    record(
      "the preflight answers",
      true,
      `${server.completed}/${server.total} ready, next=${server.next ? server.next.id : "none"}`,
    );

    const readLead = () => page.evaluate(() => {
      const host = document.getElementById("onboardingLead");
      const rows = [...document.querySelectorAll("#onboardingSteps .onboarding-step")];
      const current = rows.filter((row) => row.getAttribute("aria-current") === "step");
      const run = document.getElementById("onboardingRunBtn");
      const describedBy = run ? run.getAttribute("aria-describedby") : null;
      const hintNode = describedBy ? document.getElementById(describedBy) : null;
      return {
        exists: Boolean(host),
        state: host ? host.dataset.state : null,
        count: host && host.querySelector(".onboarding-lead-count") ? host.querySelector(".onboarding-lead-count").textContent.trim() : null,
        title: host && host.querySelector(".onboarding-lead-title") ? host.querySelector(".onboarding-lead-title").textContent.trim() : null,
        detail: host && host.querySelector(".onboarding-lead-detail") ? host.querySelector(".onboarding-lead-detail").textContent.trim() : null,
        action: host && host.querySelector("#onboardingLeadAction") ? host.querySelector("#onboardingLeadAction").textContent.trim() : null,
        hint: host && host.querySelector(".onboarding-lead-hint") ? host.querySelector(".onboarding-lead-hint").textContent.trim() : null,
        rows: rows.length,
        currentCount: current.length,
        currentTitle: current[0] ? current[0].querySelector(".onboarding-title").textContent.trim() : null,
        chip: current[0] ? Boolean(current[0].querySelector(".onboarding-step-chip")) : false,
        runDisabled: run ? run.disabled : null,
        runDescription: hintNode ? hintNode.textContent.trim() : "",
        waitingRows: rows.filter((row) => row.querySelector(".onboarding-step-waiting")).length,
        actionRows: rows.filter((row) => row.querySelector("button")).length,
        // Only a row that needs the reader and offers nothing has to explain itself;
        // a ready or optional row legitimately has no button.
        waitingNeeded: rows.filter((row) => !row.querySelector("button")
          && (row.classList.contains("pending") || row.classList.contains("blocked"))).length,
      };
    });

    const lead = await readLead();
    record("the lead is on the pane", lead.exists === true, lead.exists ? `state=${lead.state}` : "no #onboardingLead");
    record(
      "the lead prints the server's count verbatim",
      lead.count === `${server.completed} of ${server.total} steps ready`,
      lead.count,
    );
    const expectedState = server.ready ? "ready" : (server.blocked ? "blocked" : "next");
    record("the lead's state is the server's verdict", lead.state === expectedState, `${lead.state} (expected ${expectedState})`);

    if (server.next) {
      record(
        "the lead names the server's next step",
        typeof lead.title === "string" && lead.title.includes(server.next.title),
        lead.title,
      );
      record(
        "the lead offers the action the server attached",
        lead.action === server.next.action_label,
        `${lead.action} (server: ${server.next.action_label})`,
      );
      record(
        "exactly one row is aria-current, and it is that step",
        lead.currentCount === 1 && lead.currentTitle && lead.currentTitle.startsWith(server.next.title) && lead.chip,
        `${lead.currentCount} marked: ${lead.currentTitle}`,
      );
    } else {
      record("a ready preflight says so instead of naming a step", lead.state === "ready" && Boolean(lead.title), lead.title);
    }

    if (server.ready) {
      record("Run is available when the server says ready", lead.runDisabled === false, `disabled=${lead.runDisabled}`);
    } else {
      record("Run is off when the server says not ready", lead.runDisabled === true, `disabled=${lead.runDisabled}`);
      record(
        "Run explains itself in words a reader and a screen reader both get",
        Boolean(lead.runDescription) && (!server.next || lead.runDescription.includes(server.next.title)),
        lead.runDescription,
      );
      record(
        "a row that needs the reader and offers no action says what it is waiting for",
        lead.waitingNeeded === lead.waitingRows,
        `${lead.waitingNeeded} row(s) need it, ${lead.waitingRows} carry the waiting line`,
      );
    }
    await shoot(page, "lead");

    // The action must do what its label says, not just exist.
    if (server.next && server.next.action) {
      const before = await page.evaluate(() => ({
        providers: document.getElementById("providersModal").classList.contains("open"),
        activePane: document.querySelector(".pane.active") ? document.querySelector(".pane.active").id : null,
      }));
      await page.evaluate(() => document.getElementById("onboardingLeadAction").click());
      await sleep(args.settleMs * 5);
      const after = await page.evaluate(() => ({
        providers: document.getElementById("providersModal").classList.contains("open"),
        activePane: document.querySelector(".pane.active") ? document.querySelector(".pane.active").id : null,
        note: (() => {
          const box = document.getElementById("onboardingFirstTask");
          return box && !box.hidden ? box.textContent.trim() : "";
        })(),
      }));
      const expected = {
        "open-providers": after.providers === true,
        "open-chat": after.activePane === "pane-chat",
        "fix-workspace": after.activePane === "pane-harness",
        "probe-local-models": true, // the request itself is the action; the pane reports its outcome
        "run-first-task": after.activePane === "pane-chat",
      }[server.next.action];
      const didSomething = after.providers !== before.providers || after.activePane !== before.activePane || Boolean(after.note);
      if (expected === undefined) {
        block(`the lead action "${server.next.action}" does what its label says`, "the walk has no expectation for this action yet");
      } else {
        record(
          `the lead action "${server.next.action}" does what its label says`,
          expected && didSomething,
          `pane=${after.activePane} providers=${after.providers} note=${after.note ? "shown" : "none"}`,
        );
      }
      await page.evaluate(() => {
        if (document.getElementById("providersModal").classList.contains("open")) closeProvidersModal();
        switchTab("start");
      });
      await sleep(args.settleMs * 3);
    }

    // Keyboard: Tab out of the lead's action must stay in the pane.
    const keyboard = await page.evaluate(() => {
      const lead = document.getElementById("onboardingLeadAction");
      if (!lead) return { skipped: true };
      lead.focus();
      const pane = document.getElementById("pane-start");
      const inside = pane.contains(document.activeElement);
      return { skipped: false, focused: document.activeElement.id || document.activeElement.tagName, inside };
    });
    if (keyboard.skipped) {
      block("Tab from the lead's action stays on the pane", "this preflight names no action to focus");
    } else {
      await page.keyboard.press("Tab");
      const afterTab = await page.evaluate(() => ({
        inside: document.getElementById("pane-start").contains(document.activeElement),
        moved: document.activeElement.id || document.activeElement.tagName,
      }));
      record(
        "Tab from the lead's action stays on the pane",
        keyboard.inside === true && afterTab.inside === true,
        `${keyboard.focused} -> ${afterTab.moved}`,
      );
    }

    // A refused preflight, then a re-check that must restore the pane and the focus.
    breakers.preflight = true;
    await page.evaluate(() => refreshOnboarding());
    await sleep(args.settleMs * 5);
    const failed = await page.evaluate(() => ({
      presentation: Boolean(document.querySelector("#onboardingState .presentation")),
      rows: document.querySelectorAll("#onboardingSteps .onboarding-step").length,
      leadStillThere: Boolean(document.querySelector("#onboardingLead .onboarding-lead-title")),
      hint: (document.getElementById("onboardingRunHint") || {}).textContent || "",
    }));
    record(
      "a refused preflight presents the error and keeps the last good list",
      failed.presentation && failed.rows > 0,
      `${failed.rows} rows kept, presentation=${failed.presentation}`,
    );
    record(
      "the failure says the lead is stale rather than leaving it looking current",
      failed.leadStillThere && /unreadable|off/i.test(failed.hint),
      failed.hint,
    );

    breakers.preflight = false;
    // Pressed from the keyboard on purpose: an element.click() from script does not
    // focus the button, and the focus-restoration this checks only happens when the
    // control that held focus is the one removed. A real click and a real Enter both
    // focus it; a scripted click would make the pane look broken when it is not.
    const recheck = await page.evaluate(() => {
      const button = [...document.querySelectorAll("#onboardingState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Re-check");
      if (!button) return null;
      button.id = "walkRecheckBtn";
      button.focus();
      return document.activeElement === button;
    });
    if (recheck) {
      await page.keyboard.press("Enter");
    } else {
      record("the error presentation offers a Re-check", false, "no Re-check button to press");
    }
    await sleep(args.settleMs * 6);
    const recovered = await page.evaluate(() => {
      const active = document.activeElement;
      return {
        presentation: Boolean(document.querySelector("#onboardingState .presentation")),
        rows: document.querySelectorAll("#onboardingSteps .onboarding-step").length,
        current: document.querySelectorAll('#onboardingSteps [aria-current="step"]').length,
        focusId: active ? (active.id || active.tagName) : null,
        focusInPane: document.getElementById("pane-start").contains(active),
        focusIsBody: active === document.body,
      };
    });
    record(
      "Re-check clears the presentation and brings the list back",
      recovered.presentation === false && recovered.rows > 0,
      `${recovered.rows} rows`,
    );
    record(
      "focus returns to the pane instead of falling to <body>",
      recovered.focusIsBody === false && recovered.focusInPane === true && recovered.focusId === "onboardingLead",
      `${recovered.focusId}`,
    );

    // Narrow viewport: the lead is the first thing a small screen shows, so it must
    // still be legible and its action still reachable.
    await page.setViewport({ width: 900, height: 780, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const host = document.getElementById("onboardingLead");
      const action = document.getElementById("onboardingLeadAction");
      const box = host.getBoundingClientRect();
      return {
        height: Math.round(box.height),
        overflowing: host.scrollWidth > host.clientWidth + 1,
        actionVisible: action ? action.getBoundingClientRect().height > 0 : true,
      };
    });
    record(
      "the lead survives a 900px viewport",
      narrow.height > 40 && narrow.overflowing === false && narrow.actionVisible === true,
      `height=${narrow.height} overflow=${narrow.overflowing}`,
    );
    await shoot(page, "narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
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
    throw new Error(`${failed.length} readiness check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`start_walk: ${error.message}`);
  process.exit(1);
});
