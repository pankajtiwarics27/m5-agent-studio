#!/usr/bin/env node
/**
 * Check that a stopped Harness run is stated and actionable in a browser (UIUX-P3-05).
 *
 *   node scripts/harness_recovery_walk.mjs --screenshots docs/media/harness-recovery
 *
 * A run that stopped used to be one line in the log. `POST …/pause`, `…/cancel` and `…/resume`
 * existed and the pane could reach none of them, and a worker that died left the task saying
 * `running` with nothing said about it. This walk starts `scripts/harness_recovery_seed.py`,
 * which drives the real state machine into four stopped states - paused, blocked, cancelled and
 * "was running with no worker behind it" - and then presses the pane's own controls against the
 * real routes.
 *
 * What it proves:
 *
 *   1. a paused task draws its state, its cause, what survived and the two actions that exist;
 *   2. Resume goes through `POST …/resume`, and the surface says what the runtime answered -
 *      a prepared resume with the step it continues from, not a claim that work restarted;
 *   3. the prepared resume is what makes the next `/run` a continuation (`resume: true`);
 *   4. cancel takes two presses, says what it contained and how many actions it left unresolved,
 *      and a cancelled task is drawn as un-resumable rather than as a button that would fail;
 *   5. a task the runtime recovered at startup says so, and names the checkpoint it kept;
 *   6. a blocked task states the refusal the server would give, disables the action, and the
 *      server's own 409 agrees with it;
 *   7. a reason that is markup is drawn as text: no element, no handler, no page error;
 *   8. focus survives an action - the pressed button is re-drawn or the surface takes focus;
 *   9. no page error occurs, and no route outside pause/cancel/resume/abort is posted.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 60000,
  screenshots: "",
  baseUrl: "",
  token: "",
  pausedSessionId: "",
  blockedSessionId: "",
  orphanSessionId: "",
  cancelledSessionId: "",
  injectedSessionId: "",
  port: 0,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false };
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
      case "--paused-session-id": args.pausedSessionId = next(); break;
      case "--blocked-session-id": args.blockedSessionId = next(); break;
      case "--orphan-session-id": args.orphanSessionId = next(); break;
      case "--cancelled-session-id": args.cancelledSessionId = next(); break;
      case "--injected-session-id": args.injectedSessionId = next(); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--port": args.port = Number(next()); break;
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
    "Check that a stopped Harness run is stated and actionable.",
    "",
    "  node scripts/harness_recovery_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/harness_recovery_walk.mjs --base-url URL --token TOKEN \\",
    "       --paused-session-id ID --blocked-session-id ID --orphan-session-id ID",
    "",
    "Without --base-url the walk starts the seed fixture itself, so every state it reads is the",
    "server's own. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-harness-recovery.md.",
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
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This script refuses to report a walk it did not perform.",
  );
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (_error) {
      // not up yet
    }
    await sleep(150);
  }
  return false;
}

/** Start the seed fixture and read the one JSON line it prints before serving. */
async function startFixture(args) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/harness_recovery_seed.py"), "--port", String(args.port)],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The seed fixture did not report its sessions in time.\n${stderr}`)),
      90000,
    );
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        resolveReady(JSON.parse(line));
      } catch (error) {
        rejectReady(new Error(`The seed fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The seed fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The seed fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { child, ready };
}

/** Everything the panel says, read out of the DOM it actually rendered. */
async function readPanel(page) {
  return page.evaluate(() => {
    const host = document.getElementById("harnessRecoveryPanel");
    const story = document.getElementById("harnessRecoveryStory");
    const visible = Boolean(host) && !host.hidden && getComputedStyle(host).display !== "none";
    const facts = {};
    document.querySelectorAll("#harnessRecoveryFacts dt").forEach((dt) => {
      const dd = dt.nextElementSibling;
      facts[dt.textContent.trim()] = dd ? dd.textContent.trim() : "";
    });
    const note = document.getElementById("harnessRecoveryNote");
    return {
      visible,
      kind: story.querySelector(".presentation")?.dataset.kind || "",
      title: (story.querySelector(".presentation-title")?.textContent || "").trim(),
      cause: (story.querySelector(".presentation-cause")?.textContent || "").replace(/^Cause:\s*/, "").trim(),
      kept: (story.querySelector(".presentation-context")?.textContent || "").replace(/^Kept:\s*/, "").trim(),
      why: (story.querySelector(".presentation-why")?.textContent || "").trim(),
      actions: [...story.querySelectorAll(".presentation-actions .btn")].map((button) => ({
        id: button.dataset.actionId || "",
        label: button.textContent.replace(/\s+/g, " ").trim(),
        disabled: Boolean(button.disabled),
        title: button.title || "",
        aria: button.getAttribute("aria-label") || "",
      })),
      facts,
      note: (note?.textContent || "").replace(/\s+/g, " ").trim(),
      noteTone: note?.dataset.tone || "",
      text: host ? host.textContent.replace(/\s+/g, " ").trim() : "",
      images: host ? host.querySelectorAll("img").length : -1,
      focusInside: Boolean(host) && host.contains(document.activeElement),
      focusTag: document.activeElement ? document.activeElement.tagName.toLowerCase() : "",
      cards: [...document.querySelectorAll("#harnessStream .hrec-card")].map((card) => ({
        tone: card.dataset.tone || "",
        title: (card.querySelector(".hrec-title")?.textContent || "").replace(/\s+/g, " ").trim(),
        text: card.textContent.replace(/\s+/g, " ").trim(),
      })),
    };
  });
}

async function pressAction(page, actionId) {
  return page.evaluate((id) => {
    const button = document.querySelector(`#harnessRecoveryStory .presentation-actions .btn[data-action-id="${id}"]`);
    if (!button) return false;
    button.click();
    return true;
  }, actionId);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
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

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture(args);
      args.baseUrl = fixture.ready.base_url;
      args.token = args.token || fixture.ready.token;
      args.pausedSessionId = args.pausedSessionId || fixture.ready.paused_session_id;
      args.blockedSessionId = args.blockedSessionId || fixture.ready.blocked_session_id;
      args.orphanSessionId = args.orphanSessionId || fixture.ready.orphan_session_id;
      args.cancelledSessionId = args.cancelledSessionId || fixture.ready.cancelled_session_id;
      args.injectedSessionId = args.injectedSessionId || fixture.ready.injected_session_id;
    }
    for (const [label, value] of [
      ["--paused-session-id", args.pausedSessionId],
      ["--blocked-session-id", args.blockedSessionId],
      ["--orphan-session-id", args.orphanSessionId],
      ["--injected-session-id", args.injectedSessionId],
    ]) {
      if (!value) throw new Error(`--base-url mode needs ${label}`);
    }

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
    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      const errors = [];
      const posts = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("request", (request) => {
        if (request.method() !== "POST") return;
        try {
          const url = new URL(request.url());
          if (url.origin === new URL(args.baseUrl).origin) posts.push(url.pathname);
        } catch (_error) {
          // not a URL worth checking
        }
      });
      await page.evaluateOnNewDocument((key, token) => {
        try {
          window.sessionStorage.setItem(key, token);
        } catch (_error) {}
      }, args.tokenKey, args.token);
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 6);
      await page.evaluate(() => switchTab("harness"));
      await sleep(args.settleMs * 4);

      // ── A paused task says what happened, what survived, and what it can do ──
      await page.evaluate((id) => selectHarnessSession(id), args.pausedSessionId);
      await sleep(args.settleMs * 14);
      const paused = await readPanel(page);
      record(
        "a paused task draws its own surface, not a line in the log",
        paused.visible === true && paused.title === "Paused at a safe boundary",
        `visible=${paused.visible} title="${paused.title}"`,
      );
      record(
        "the surface states the cause in the runtime's reason and the checkpoint it kept",
        /the operator asked the run to stop/.test(paused.cause)
          && /operator_pause_requested/.test(paused.cause)
          && /step 3/.test(paused.facts["Latest checkpoint"] || "")
          && /paused/.test(paused.facts.Task || ""),
        `${paused.cause} | ckpt="${paused.facts["Latest checkpoint"]}"`,
      );
      record(
        "the surface offers resume and cancel, and resume is available",
        paused.actions.length === 2
          && paused.actions[0].id === "resume" && paused.actions[0].disabled === false
          && paused.actions[1].id === "cancel",
        JSON.stringify(paused.actions.map((action) => `${action.id}:${action.disabled ? "off" : "on"}`)),
      );
      record(
        "the facts list the recovery memory, the budget and the containment store",
        /retries/.test(paused.facts["Recovery budget"] || "")
          && /recoveries/.test(paused.facts["Task budget"] || "")
          && /barrier/.test(paused.facts.Containment || "") === true,
        `${paused.facts["Recovery budget"]} · ${paused.facts.Containment}`,
      );
      await shoot(page, "paused-panel");

      // ── Resume is a real call, and the surface reports what came back ───────
      await page.evaluate(() => {
        const button = document.querySelector('#harnessRecoveryStory .presentation-actions .btn[data-action-id="resume"]');
        button.focus();
      });
      await page.keyboard.press("Enter");
      await sleep(args.settleMs * 14);
      const prepared = await readPanel(page);
      record(
        "Resume goes through the route and the surface says what the runtime answered",
        /^Resume prepared — the next turn continues from step 4/.test(prepared.title)
          && /Nothing has run yet/.test(prepared.note),
        `title="${prepared.title}" note="${prepared.note}"`,
      );
      record(
        "the log records the prepared resume, with the step it continues from",
        prepared.cards.some((card) => /Resume prepared — the next turn starts at step 4/.test(card.title)
          && /Nothing has run/.test(card.text)),
        JSON.stringify(prepared.cards.map((card) => card.title)),
      );
      record(
        "the pressed action keeps focus somewhere real",
        prepared.focusInside === true || prepared.focusTag === "button",
        `focusInside=${prepared.focusInside} tag=${prepared.focusTag}`,
      );
      await shoot(page, "resume-prepared");

      // ── Cancel takes two presses and says what it did ───────────────────────
      await pressAction(page, "cancel");
      await sleep(args.settleMs * 4);
      const armed = await readPanel(page);
      record(
        "cancel asks once before it fences anything",
        armed.actions.some((action) => /^Confirm: cancel/.test(action.label))
          && /Pressing cancel again/.test(armed.note)
          && armed.facts.Task.includes("paused"),
        `note="${armed.note}"`,
      );
      await shoot(page, "cancel-armed");
      await pressAction(page, "cancel");
      await sleep(args.settleMs * 16);
      const cancelled = await readPanel(page);
      record(
        "the second press cancels, and the surface reports what it contained and left unresolved",
        /^Cancelled\./.test(cancelled.note) && /contained/.test(cancelled.note) && /unresolved/.test(cancelled.note),
        cancelled.note,
      );
      record(
        "a cancelled task is drawn as un-resumable, with the way forward and no dead button",
        cancelled.visible === true
          && /^Cancelled$/.test(cancelled.title)
          && cancelled.actions.length === 1
          && cancelled.actions[0].id === "fresh"
          && /cannot be resumed/.test(cancelled.kept),
        `title="${cancelled.title}" actions=${JSON.stringify(cancelled.actions.map((action) => action.id))}`,
      );
      record(
        "the cancelled event is a card that states why and what it fenced",
        cancelled.cards.some((card) => card.tone === "danger"
          && /were contained/.test(card.text)
          && /No consequential action was left unresolved/.test(card.text)),
        JSON.stringify(cancelled.cards.map((card) => card.tone)),
      );
      await shoot(page, "cancelled-panel");
      const cancelledResume = await page.evaluate(async (id) => {
        const response = await fetch(`/api/harness/sessions/${encodeURIComponent(id)}/resume`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        return { status: response.status, body: await response.text() };
      }, args.pausedSessionId);
      record(
        "the server refuses the resume the surface said was impossible",
        cancelledResume.status === 409 && /cannot be resumed/i.test(cancelledResume.body),
        `${cancelledResume.status} ${cancelledResume.body.slice(0, 120)}`,
      );

      // ── A run the runtime recovered at startup says so ─────────────────────
      await page.evaluate((id) => selectHarnessSession(id), args.orphanSessionId);
      await sleep(args.settleMs * 14);
      const recovered = await readPanel(page);
      record(
        "a run that died is drawn from the state the runtime recovered it to, and names the reason",
        recovered.visible === true
          && recovered.title === "Paused at a safe boundary"
          && /no live worker/.test(recovered.cause)
          && /recovered_incomplete_transition/.test(recovered.cause),
        `title="${recovered.title}" cause="${recovered.cause}"`,
      );
      record(
        "the checkpoint a crash recovery kept is the one named, not a claim that nothing was lost",
        /step 2/.test(recovered.facts["Latest checkpoint"] || "")
          && /survive|survived|is where a resume/.test(recovered.kept),
        `kept="${recovered.kept}"`,
      );
      await shoot(page, "crash-recovered");
      await pressAction(page, "resume");
      await sleep(args.settleMs * 14);
      const recoveredResume = await readPanel(page);
      record(
        "the recovered run resumes from the checkpoint the crash left",
        /^Resume prepared — the next turn continues from step 3/.test(recoveredResume.title),
        recoveredResume.title,
      );
      await sleep(args.settleMs * 4);
      // ── A prepared resume is what makes the next run a continuation ────────
      // `consumeSSE` is the one call that starts a turn; it is replaced for a single
      // request so the body the pane would send can be read without running a model.
      const continueBody = await page.evaluate(() => {
        const original = window.consumeSSE;
        let captured = null;
        window.consumeSSE = (url, payload) => { captured = { url, payload }; return Promise.resolve(); };
        try {
          document.getElementById("harnessPromptInput").value = "add the missing test";
          window.runHarnessTurn();
        } finally {
          window.consumeSSE = original;
          // `runHarnessTurn` commits to a run before it posts; the probe has to undo that,
          // or the pane keeps saying "a run is in flight" for a turn that was never sent.
          window.setHarnessRunActive(false);
          document.getElementById("harnessAbortBtn").style.display = "none";
          document.getElementById("harnessRunBtn").disabled = false;
        }
        return captured
          ? { url: captured.url, resume: captured.payload.resume, prompt: captured.payload.prompt }
          : null;
      });
      record(
        "a run started after a prepared resume asks the server to continue the checkpoint",
        Boolean(continueBody)
          && continueBody.url === "/api/harness/run"
          && continueBody.resume === true
          && continueBody.prompt === "add the missing test",
        JSON.stringify(continueBody),
      );

      // ── A blocked task states the refusal instead of failing it at a press ──
      await page.evaluate((id) => selectHarnessSession(id), args.blockedSessionId);
      await sleep(args.settleMs * 14);
      const blocked = await readPanel(page);
      const blockedResume = blocked.actions.find((action) => action.id === "resume");
      record(
        "a blocked task is a story with the blocked cause, not an editable form",
        blocked.visible === true
          && /^Blocked on effects the runtime cannot account for$/.test(blocked.title)
          && /cannot account for/.test(blocked.cause),
        `title="${blocked.title}"`,
      );
      record(
        "resume is drawn as unavailable, with the reason in text and in the accessible name",
        Boolean(blockedResume) && blockedResume.disabled === true
          && /not available/i.test(blockedResume.aria)
          && /blocked|unresolved/i.test(blocked.why),
        `why="${blocked.why}"`,
      );
      record(
        "the unresolved action the barrier holds is listed by id",
        Object.keys(blocked.facts).includes("Unresolved")
          && /^act-seed-/.test(blocked.facts.Unresolved || ""),
        `unresolved="${blocked.facts.Unresolved}"`,
      );
      await shoot(page, "blocked-panel");
      const blockedServer = await page.evaluate(async (id) => {
        const response = await fetch(`/api/harness/sessions/${encodeURIComponent(id)}/resume`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        return { status: response.status, body: await response.text() };
      }, args.blockedSessionId);
      record(
        "the server's refusal agrees with the reason the surface stated",
        blockedServer.status === 409 && /unresolved side effects or integrity violations/i.test(blockedServer.body),
        `${blockedServer.status} ${blockedServer.body.slice(0, 140)}`,
      );

      // ── A reason that is markup is drawn as text ───────────────────────────
      await page.evaluate((id) => selectHarnessSession(id), args.injectedSessionId);
      await sleep(args.settleMs * 14);
      const hostile = await readPanel(page);
      const xss = await page.evaluate(() => ({
        fired: typeof window.__recoveryXss !== "undefined",
        imgs: document.querySelectorAll("#harnessRecoveryPanel img, #harnessStream .hrec-card img").length,
        text: document.getElementById("harnessRecoveryPanel").textContent,
      }));
      record(
        "a reason that is markup is drawn as text: no element, no handler",
        hostile.visible === true
          && xss.fired === false
          && xss.imgs === 0
          && /<img src=x onerror=/.test(xss.text),
        `fired=${xss.fired} imgs=${xss.imgs}`,
      );
      await shoot(page, "hostile-reason");

      // ── The pane never invents a route for any of this ────────────────────
      const postedPaths = [...new Set(posts.filter((path) => path.startsWith("/api/")))];
      const allowed = postedPaths.every((path) => /^\/api\/harness\/(abort\/[^/]+|sessions\/[^/]+\/(pause|cancel|resume))$/.test(path));
      record(
        "every request the surface made is one of the routes that already existed",
        allowed && postedPaths.length > 0,
        postedPaths.join(", "),
      );

      record(
        "no page error occurred during the walk",
        errors.length === 0,
        errors.length ? errors.slice(0, 2).join(" | ") : "",
      );
    } finally {
      await browser.close();
    }

    const failed = results.filter((result) => !result.passed);
    if (args.json) {
      console.log(JSON.stringify({ steps: results, passed: results.length - failed.length, failed: failed.length }, null, 2));
    } else {
      console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
    }
    process.exitCode = failed.length ? 1 : 0;
  } finally {
    if (fixture) fixture.child.kill("SIGTERM");
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exitCode = 1;
});
