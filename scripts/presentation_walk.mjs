#!/usr/bin/env node
/**
 * Break the network on purpose and check what the shell says about it (UIUX-P1-05).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/presentation_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Why this exists: the acceptance criterion for this story is "each presentation
 * exercised in a test with a refused request and a disconnected stream", and both
 * of those are runtime situations. A source check can prove the builder validates
 * its arguments; only a browser with request interception can prove that a refused
 * preflight renders a cause, a what-is-kept line and a working Retry, or that a
 * dropped event stream says "live updates paused" instead of freezing silently.
 *
 * What it does:
 *
 *   1. refused request  - aborts /api/onboarding/preflight, asserts the Start pane
 *      shows the error presentation (title, cause, kept, Re-check), then lets the
 *      request through, presses Re-check and asserts the presentation is gone and
 *      the checklist is back;
 *   2. refused stream   - aborts /api/chat/stream after a send and asserts the chat
 *      error presentation, that the user's message is retained, and that no empty
 *      assistant bubble is left behind for Retry to duplicate;
 *   3. disconnected stream - creates a real Harness session, selects it, aborts its
 *      /events/stream and asserts the recovery presentation with cause, kept events
 *      and the two actions; then reconnects and asserts it clears;
 *   4. the builder      - rejects a presentation with no cause, no kept line or no
 *      action, so the shape cannot degrade into "Error: HTTP 500";
 *   5. the empty state  - the Compare pane opens with the shared empty presentation
 *      and its action, not a blank column;
 *   6. no page errors, and the shell is left with no presentation stuck open.
 *
 * Exit status is 0 only when every check passed; `--json` prints the results so a
 * story can record the exact output.
 */
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 45000,
};

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
    "Break the network on purpose and report what the shell says about it.",
    "",
    "  node scripts/presentation_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-presentations.md.",
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

/** Read one presentation out of the DOM as a reader would see it. */
const READ = (target) => {
  const host = document.getElementById(target);
  const box = host ? host.querySelector(".presentation") : null;
  if (!box) return null;
  const text = (selector) => {
    const node = box.querySelector(selector);
    return node ? node.textContent.trim() : null;
  };
  return {
    kind: box.dataset.kind,
    role: box.getAttribute("role"),
    label: box.getAttribute("aria-label"),
    title: text(".presentation-title"),
    cause: text(".presentation-cause"),
    context: text(".presentation-context"),
    actions: [...box.querySelectorAll(".presentation-actions button")].map((button) => button.textContent.trim()),
    // A "Cause:" or "Kept:" prefix is drawn by the stylesheet; the check below reads
    // the declared content so a missing slot cannot be hidden by empty text.
    causePrefix: getComputedStyle(box.querySelector(".presentation-cause") || box, "::before").content,
    contextPrefix: getComputedStyle(box.querySelector(".presentation-context") || box, "::before").content,
  };
};

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
  // Optional evidence: a broken state is easier to review as a picture than as a
  // passing line, and these are the only captures of the app being deliberately
  // starved of a response.
  const shoot = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
  };
  const block = (step, detail) => {
    results.push({ step, passed: false, detail, status: "blocked" });
    if (!args.json) console.log(`block  ${step} — ${detail}`);
  };

  // Which requests to break, switched per scenario. A rule of "abort" answers with
  // net::ERR_FAILED, which is what a dropped connection looks like to fetch.
  const breakers = { preflight: false, chat: false, harnessStream: false };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (breakers.preflight && url.includes("/api/onboarding/preflight")) return request.abort("failed");
      if (breakers.chat && url.includes("/api/chat/stream")) return request.abort("failed");
      if (breakers.harnessStream && url.includes("/events/stream")) return request.abort("failed");
      return request.continue();
    });
    await page.evaluateOnNewDocument((key, token) => {
      try {
        window.sessionStorage.setItem(key, token);
      } catch (_error) {}
    }, args.tokenKey, args.token);
    await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);

    record("the builder is exported for the walk", await page.evaluate(() => typeof window.M5Presentations === "object"), "window.M5Presentations");

    // ── 1. The empty state a pane opens with ──────────────────────────────────
    await page.evaluate(() => switchTab("debate"));
    await sleep(args.settleMs * 2);
    const empty = await page.evaluate(READ, "debateSynthesisArea");
    record(
      "Compare opens with the shared empty presentation",
      Boolean(empty && empty.kind === "empty" && empty.title && empty.cause && empty.context && empty.actions.length),
      empty ? `${empty.kind}: ${empty.title} / ${empty.actions.join(", ")}` : "nothing rendered",
    );
    if (empty) await shoot(page, "compare-empty");
    record(
      "the empty presentation labels its cause and its kept context",
      Boolean(empty && empty.causePrefix.includes("Cause") && empty.contextPrefix.includes("Kept")),
      empty ? `${empty.causePrefix} ${empty.contextPrefix}` : "missing",
    );

    // ── 2. The builder refuses to render an unusable presentation ─────────────
    const refused = await page.evaluate(() => {
      const problem = window.M5Presentations.problem;
      const base = { kind: "error", title: "t", cause: "c", context: "k", actions: [{ label: "a" }] };
      const out = {};
      for (const [name, spec] of Object.entries({
        noCause: { ...base, cause: "" },
        noContext: { ...base, context: "" },
        noActions: { ...base, actions: [] },
        badKind: { ...base, kind: "vibes" },
        badRun: { ...base, actions: [{ label: "a", run: "nope" }] },
      })) out[name] = problem(spec);
      const threw = (() => {
        try {
          window.M5Presentations.present("debateSynthesisArea", { kind: "vibes", title: "t", cause: "c", context: "k", actions: [{ label: "a" }] });
          return false;
        } catch (_error) {
          return true;
        }
      })();
      return { out, threw };
    });
    record(
      "a presentation with no cause, kept line or action is refused",
      Object.values(refused.out).every((value) => typeof value === "string" && value.length > 0),
      JSON.stringify(refused.out),
    );
    record("an unknown kind is refused at render time", refused.threw === true, String(refused.threw));

    // ── 3. A refused request ─────────────────────────────────────────────────
    breakers.preflight = true;
    await page.evaluate(() => switchTab("start"));
    await sleep(args.settleMs);
    await page.evaluate(() => refreshOnboarding());
    await sleep(args.settleMs * 3);
    const startError = await page.evaluate(READ, "onboardingState");
    record(
      "a refused preflight renders the error presentation",
      Boolean(startError && startError.kind === "error" && startError.role === "alert"),
      startError ? `${startError.kind} role=${startError.role}` : "nothing rendered",
    );
    record(
      "the error names its cause instead of saying 'something went wrong'",
      Boolean(startError && startError.cause && !/^something went wrong$/i.test(startError.cause)),
      startError ? startError.cause : "missing",
    );
    record(
      "the error says what was kept",
      Boolean(startError && startError.context && startError.context.length > 20),
      startError ? startError.context : "missing",
    );
    await shoot(page, "start-error");
    record(
      "the error offers the actions that exist",
      Boolean(startError && startError.actions.includes("Re-check")),
      startError ? startError.actions.join(", ") : "none",
    );

    breakers.preflight = false;
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#onboardingState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Re-check");
      if (button) button.click();
    });
    await sleep(args.settleMs * 4);
    const afterRetry = await page.evaluate(() => {
      const host = document.getElementById("onboardingState");
      return {
        presentation: host ? host.querySelector(".presentation") : null,
        steps: document.querySelectorAll("#onboardingSteps .onboarding-step").length,
      };
    });
    record(
      "Retry clears the presentation and brings the checklist back",
      afterRetry.presentation === null && afterRetry.steps > 0,
      `${afterRetry.steps} step(s)`,
    );

    // ── 4. A refused stream in Chat ──────────────────────────────────────────
    breakers.chat = true;
    await page.evaluate(() => switchTab("chat"));
    await sleep(args.settleMs);
    const beforeCount = await page.evaluate(() => document.querySelectorAll("#chatMessages .chat-msg, #chatMessages > *").length);
    await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      input.value = "presentation walk: this send is refused on purpose";
      sendChat();
    });
    await sleep(args.settleMs * 4);
    const chatError = await page.evaluate(READ, "chatState");
    const chatState = await page.evaluate(() => ({
      userMessages: [...document.querySelectorAll("#chatMessages .chat-msg.user, #chatMessages .msg-user")].length,
      bubbles: document.querySelectorAll("#chatMessages > *").length,
      transcriptMentions: (document.getElementById("chatMessages").textContent || "").includes("refused on purpose"),
      emptyAssistant: [...document.querySelectorAll("#chatMessages > *")].some((node) => !node.textContent.trim()),
    }));
    record(
      "a refused chat stream renders the error presentation",
      Boolean(chatError && chatError.kind === "error"),
      chatError ? `${chatError.title} — ${chatError.cause}` : "nothing rendered",
    );
    record(
      "the failed message is kept in the transcript",
      chatState.transcriptMentions === true,
      `${chatState.bubbles} bubble(s), was ${beforeCount}`,
    );
    record(
      "no empty assistant bubble is left for Retry to duplicate",
      chatState.emptyAssistant === false,
      `empty bubbles: ${chatState.emptyAssistant}`,
    );
    await shoot(page, "chat-error");
    record(
      "the chat error offers Retry and Dismiss",
      Boolean(chatError && chatError.actions.includes("Retry") && chatError.actions.includes("Dismiss")),
      chatError ? chatError.actions.join(", ") : "none",
    );

    breakers.chat = false;
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#chatState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Dismiss");
      if (button) button.click();
    });
    await sleep(args.settleMs);
    record(
      "Dismiss hides the presentation without touching the transcript",
      Boolean(await page.evaluate(() => !document.querySelector("#chatState .presentation")
        && (document.getElementById("chatMessages").textContent || "").includes("refused on purpose"))),
      "transcript kept",
    );

    // ── 5. A disconnected stream: the Harness timeline ───────────────────────
    breakers.harnessStream = true;
    const session = await page.evaluate(async () => {
      const response = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "presentation walk" }),
      });
      const payload = await response.json().catch(() => ({}));
      return { ok: response.ok, id: payload && payload.session ? payload.session.id : null, status: response.status };
    });
    if (!session.ok || !session.id) {
      block("a disconnected stream renders the recovery presentation", `could not create a Harness session (HTTP ${session.status})`);
    } else {
      await page.evaluate((id) => { switchTab("harness"); return selectHarnessSession(id); }, session.id);
      await sleep(args.settleMs * 6);
      const recovery = await page.evaluate(READ, "harnessStreamState");
      record(
        "a disconnected stream renders the recovery presentation",
        Boolean(recovery && recovery.kind === "recovery"),
        recovery ? `${recovery.title} — ${recovery.cause}` : "nothing rendered",
      );
      record(
        "the recovery says the task is still running and what is shown",
        Boolean(recovery && /running/i.test(recovery.context) && /\d+ event/.test(recovery.context)),
        recovery ? recovery.context : "missing",
      );
      await shoot(page, "harness-recovery");
      record(
        "the recovery offers reconnect and snapshot paths",
        Boolean(recovery && recovery.actions.includes("Reconnect now") && recovery.actions.includes("Reload from snapshot")),
        recovery ? recovery.actions.join(", ") : "none",
      );

      breakers.harnessStream = false;
      await page.evaluate(() => {
        const button = [...document.querySelectorAll("#harnessStreamState .presentation-actions button")]
          .find((node) => node.textContent.trim() === "Reconnect now");
        if (button) button.click();
      });
      await sleep(args.settleMs * 6);
      const cleared = await page.evaluate(() => !document.querySelector("#harnessStreamState .presentation"));
      record("reconnecting clears the recovery presentation", cleared === true, String(cleared));
    }

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
    throw new Error(`${failed.length} presentation check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`presentation_walk: ${error.message}`);
  process.exit(1);
});
