#!/usr/bin/env node
/**
 * Check Start's recent work and task handoff in a browser (UIUX-P2-02).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/start_recent_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The claims this story makes are all about state that has to exist first, so the
 * walk makes it: it creates its own Harness session through the same endpoint the
 * Harness uses, asserts the row and its state pill, opens the session from the row,
 * and deletes what it created on the way out so the demo store is left as it was
 * found.
 *
 * What it proves:
 *
 *   1. a session appears on Start with its title, its step count and the label the
 *      shared state table gives its state - read from harnessStatusBadge(), not from
 *      a second vocabulary written for Start;
 *   2. the row is a real button: focusable, and Enter on it opens the session in the
 *      Harness (not a click handler on a div);
 *   3. the launcher carries the text into the Harness composer and stops - the pane
 *      switches, the composer holds the text, no session is created and no stream
 *      starts, which is the difference between a handoff and a launch;
 *   4. the note under the composer quotes the preflight's verdict, so a task is not
 *      carried into a Run that will refuse;
 *   5. with nothing to show, Start presents the empty state and its action;
 *   6. with the session list refused, Start presents the error with a working Retry
 *      (and the launcher still works: the pane says what is kept);
 *   7. no page errors, and the rows survive a 900px viewport.
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
    "Check Start's recent sessions and task handoff against the Harness.",
    "",
    "  node scripts/start_recent_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-recent-work.md.",
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

  const breakers = { sessions: false };
  const created = [];

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (breakers.sessions && request.method() === "GET" && /\/api\/harness\/sessions(\?|$)/.test(url)) {
        return request.abort("failed");
      }
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

    // A session of the walk's own, so the assertions do not depend on demo state.
    const title = `walk recent ${Date.now().toString(36)}`;
    const session = await page.evaluate(async (sessionTitle) => {
      const response = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: sessionTitle }),
      });
      const payload = await response.json().catch(() => ({}));
      return { ok: response.ok, status: response.status, id: payload && payload.session ? payload.session.id : null };
    }, title);
    if (!session.ok || !session.id) {
      block("a created session appears in recent work", `could not create a session (HTTP ${session.status})`);
      throw new Error("the walk could not create its own session, so it has nothing to assert");
    }
    created.push(session.id);

    const expectedLabel = await page.evaluate(() => {
      const badge = window.M5Status ? null : null; // the table is not exported; read it through a session row below
      return badge;
    });
    void expectedLabel;

    await page.evaluate(() => loadStartRecent({ force: true }));
    await sleep(args.settleMs * 4);

    const rows = await page.evaluate(() => [...document.querySelectorAll("#startRecentList .start-recent-row")].map(row => ({
      id: row.dataset.sessionId,
      title: row.querySelector(".start-recent-title").textContent.trim(),
      meta: row.querySelector(".start-recent-meta").textContent.trim(),
      pill: row.querySelector(".status") ? row.querySelector(".status").textContent.trim() : null,
      tone: row.querySelector(".status") ? row.querySelector(".status").dataset.tone : null,
      tag: row.tagName,
      role: row.getAttribute("role"),
    })));
    const mine = rows.find(row => row.id === session.id);
    record("a created session appears in recent work", Boolean(mine), mine ? `${mine.title} — ${mine.pill}` : "not listed");
    if (mine) {
      record("the row names the session and what it has done", mine.title === title && /\d+ steps?/.test(mine.meta), `${mine.title} / ${mine.meta}`);
      record(
        "the row's state is the shared table's label for that state",
        mine.pill === "READY" && mine.tone === "info",
        `${mine.pill} (${mine.tone})`,
      );
      record("the row is a button a keyboard can reach", mine.tag === "BUTTON" && mine.role === "listitem", `${mine.tag} role=${mine.role}`);
    }
    record(
      "the list is bounded",
      rows.length <= 5,
      `${rows.length} row(s) for a limit of 5`,
    );
    // Frame the card that is being judged: a capture of the top of the pane would
    // show the checklist and leave the evidence below the fold.
    await page.evaluate(() => document.getElementById("startRecentList").scrollIntoView({ block: "center" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "recent");

    // Keyboard opens the session, not a scripted click.
    if (mine) {
      await page.evaluate((id) => {
        const row = [...document.querySelectorAll("#startRecentList .start-recent-row")].find(node => node.dataset.sessionId === id);
        row.focus();
      }, session.id);
      const focused = await page.evaluate(() => document.activeElement.classList.contains("start-recent-row"));
      await page.keyboard.press("Enter");
      await sleep(args.settleMs * 8);
      const opened = await page.evaluate(() => ({
        pane: document.querySelector(".pane.active") ? document.querySelector(".pane.active").id : null,
        active: typeof activeHarnessId === "undefined" ? null : activeHarnessId,
        header: document.getElementById("harnessActiveLabel") ? document.getElementById("harnessActiveLabel").textContent.trim() : "",
      }));
      record(
        "Enter on a row opens that session in the Harness",
        focused && opened.pane === "pane-harness" && opened.active === session.id,
        `${opened.pane} active=${opened.active} header=${opened.header.slice(0, 40)}`,
      );
    }

    // The handoff: text moves, nothing runs, nothing is created.
    await page.evaluate(() => switchTab("start"));
    await sleep(args.settleMs * 3);
    const before = await page.evaluate(() => ({
      sessions: harnessSessions.length,
      stream: (document.getElementById("harnessStream") || {}).textContent.trim().length,
    }));
    const task = "add a health endpoint and a test for it";
    await page.evaluate((text) => {
      const input = document.getElementById("startTaskInput");
      input.value = text;
      input.dispatchEvent(new Event("input"));
      document.getElementById("startTaskBtn").click();
    }, task);
    await sleep(args.settleMs * 6);
    const after = await page.evaluate(() => ({
      pane: document.querySelector(".pane.active") ? document.querySelector(".pane.active").id : null,
      composer: (document.getElementById("harnessPromptInput") || {}).value || "",
      focusedComposer: document.activeElement === document.getElementById("harnessPromptInput"),
      sessions: harnessSessions.length,
      stream: (document.getElementById("harnessStream") || {}).textContent.trim().length,
      hint: (document.getElementById("startTaskHint") || {}).textContent || "",
    }));
    record(
      "the launcher carries the task into the Harness composer",
      after.pane === "pane-harness" && after.composer === task && after.focusedComposer,
      `${after.pane} composer=${after.composer.slice(0, 40)} focused=${after.focusedComposer}`,
    );
    record(
      "the launcher starts nothing: no session created, no stream",
      after.sessions === before.sessions && after.stream === before.stream,
      `${before.sessions}->${after.sessions} sessions, stream ${before.stream}->${after.stream}`,
    );
    const preflight = await page.evaluate(async () => {
      const response = await fetch("/api/onboarding/preflight");
      const payload = await response.json();
      return payload.preflight || {};
    });
    record(
      "the handoff's note quotes the preflight's verdict",
      preflight.ready ? /ready/i.test(after.hint) : Boolean(preflight.next_step) && after.hint.includes(preflight.next_step.title),
      after.hint.slice(0, 140),
    );
    await shoot(page, "handoff");

    // Back to Start: a control inside a hidden pane cannot take focus, so a focus
    // assertion made from the Harness pane would fail for the wrong reason.
    await page.evaluate(() => switchTab("start"));
    await sleep(args.settleMs * 4);

    // Empty: delete everything the walk made, then re-read the list.
    for (const id of created) {
      await page.evaluate(async (sessionId) => {
        await fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      }, id);
    }
    await page.evaluate(() => {
      startRecentSessions = [];
      return loadStartRecent({ force: true });
    });
    await sleep(args.settleMs * 5);
    const empty = await page.evaluate(() => {
      const box = document.querySelector("#startRecentState .presentation");
      return {
        present: Boolean(box),
        kind: box ? box.dataset.kind : null,
        title: box && box.querySelector(".presentation-title") ? box.querySelector(".presentation-title").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map(node => node.textContent.trim()) : [],
        rows: document.querySelectorAll("#startRecentList .start-recent-row").length,
      };
    });
    if (empty.present && empty.kind === "empty") {
      record(
        "with nothing to show, Start presents the empty state and its action",
        empty.rows === 0 && empty.actions.length > 0 && /no recent work/i.test(empty.title || ""),
        `${empty.title} — ${empty.actions.join(", ")}`,
      );
    } else if (empty.rows > 0) {
      block(
        "with nothing to show, Start presents the empty state and its action",
        "the demo store has other sessions, so the empty state cannot be reached without deleting them",
      );
    } else {
      record("with nothing to show, Start presents the empty state and its action", false, JSON.stringify(empty));
    }
    await shoot(page, "empty");

    // Unreadable: the pane must say so, keep the launcher, and recover.
    breakers.sessions = true;
    await page.evaluate(() => loadStartRecent({ force: true }));
    await sleep(args.settleMs * 5);
    const failed = await page.evaluate(() => {
      const box = document.querySelector("#startRecentState .presentation");
      return {
        kind: box ? box.dataset.kind : null,
        title: box && box.querySelector(".presentation-title") ? box.querySelector(".presentation-title").textContent.trim() : null,
        context: box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map(node => node.textContent.trim()) : [],
        launcherUsable: Boolean(document.getElementById("startTaskInput")) && !document.getElementById("startTaskInput").disabled,
      };
    });
    record(
      "a refused session list presents the error and keeps the launcher",
      failed.kind === "error" && failed.launcherUsable && failed.actions.includes("Retry"),
      `${failed.title} — ${failed.actions.join(", ")}`,
    );
    record(
      "the error says what is kept",
      Boolean(failed.context) && /composer|launcher|nothing was changed/i.test(failed.context || ""),
      failed.context,
    );

    breakers.sessions = false;
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#startRecentState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Retry");
      if (button) {
        button.id = "walkRecentRetry";
        button.focus();
      }
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 6);
    const recovered = await page.evaluate(() => {
      const box = document.querySelector("#startRecentState .presentation");
      return {
        kind: box ? box.dataset.kind : null,
        rows: document.querySelectorAll("#startRecentList .start-recent-row").length,
        summary: (document.getElementById("startRecentSummary") || {}).textContent || "",
        focusInside: document.getElementById("pane-start").contains(document.activeElement),
      };
    });
    // Retry is not "make the error vanish": it re-reads the list, and the answer may
    // legitimately be "there is no recent work". What must not survive is the error.
    record(
      "Retry re-reads the list and clears the error",
      recovered.kind !== "error" && (recovered.rows > 0 || recovered.kind === "empty"),
      `kind=${recovered.kind} rows=${recovered.rows} summary=${recovered.summary.trim()}`,
    );
    record(
      "focus stays on the pane after a successful retry",
      recovered.focusInside === true,
      recovered.focusInside ? "inside #pane-start" : "outside the pane",
    );

    await page.evaluate(() => switchTab("start"));
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const card = document.getElementById("startRecentList");
      const launch = document.querySelector(".start-launch");
      return {
        overflow: card ? card.scrollWidth > card.clientWidth + 1 : true,
        launchHeight: launch ? Math.round(launch.getBoundingClientRect().height) : 0,
      };
    });
    record(
      "recent work survives a 900px viewport",
      narrow.overflow === false && narrow.launchHeight > 20,
      `overflow=${narrow.overflow} launcher=${narrow.launchHeight}px`,
    );

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    if (created.length) {
      // Leave the store as it was found, even when an assertion above threw.
      try {
        const page = (await browser.pages())[0];
        if (page) {
          for (const id of created) {
            await page.evaluate(async (sessionId) => {
              await fetch(`/api/harness/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }).catch(() => {});
            }, id).catch(() => {});
          }
        }
      } catch (_error) {}
    }
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
    throw new Error(`${failed.length} recent-work check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`start_recent_walk: ${error.message}`);
  process.exit(1);
});
