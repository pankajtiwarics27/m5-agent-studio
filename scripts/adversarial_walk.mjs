#!/usr/bin/env node
/**
 * Attack the Studio's writes and check that each refusal is drawn as a refusal (UIUX-P6-02).
 *
 *   node scripts/adversarial_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *   node scripts/adversarial_walk.mjs --json
 *
 * A refusal is the server's answer. The panes used to draw whatever their success path drew,
 * because the handler read the response *body* and never its *status*:
 *
 *   * an apply the server refused with `Cannot apply worktree changes while session policy is
 *     read-only 'plan'.` printed **"Applied!"** in the ok tone;
 *   * a compaction on a session that no longer exists printed "Compaction checked.";
 *   * a delete that was refused cleared the pane and re-read a list that still held the row;
 *   * a Council message that was refused had already been cleared out of the composer, and a
 *     session created from an error body became `activeHallId = undefined` with the text gone;
 *   * a refused MCP or OpenContext write answered with `if (r.ok) { … }` and nothing else - no
 *     row changed and no word said, which reads exactly like a click that missed.
 *
 * The walk attacks the running app. Refusals it can make the server produce for real, it does:
 *
 *   * **403** - a session whose policy is read-only `plan` refuses every worktree apply;
 *   * **404** - a session deleted underneath the pane is gone when the pane writes to it;
 *   * **401** - a browser whose token is wrong is refused by the security boundary;
 *   * **429** - the rate window fills and the boundary answers with `Retry-After`;
 *   * **401 on a stream** - a refused `/api/chat/stream` before it opens.
 *
 * Three refusals the browser cannot provoke on its own are *injected* at the network layer,
 * through CDP request interception, with the server's own `detail` text copied from `app.py`:
 * a cross-origin 403 (a page cannot forge `Origin`), a 413 (the page cannot exceed the body
 * limit without sending one) and a 503 (the demo app always has a token configured). Every
 * check states which it is, so the record never claims a real refusal it did not get.
 *
 * What it proves, in order:
 *
 *   1. the harness pane opens the session the server holds;
 *   2. a read-only session's refused apply is drawn where the button is, with the server's own
 *      reason, what was kept, and a way forward - and nothing says "Applied";
 *   3. an injected 503 on the same route is drawn as an error, not as a success;
 *   4. an injected 507 says the disk is full and keeps the diff;
 *   5. an injected 413 on compaction says the request is over the size limit;
 *   6. a compact on a session the server no longer has says 404 and keeps the reader's place;
 *   7. an injected cross-origin 403 on the Council's spawn keeps the modal open with the text;
 *   8. a refused Council message puts the reader's text back in the composer;
 *   9. a wrong token is reported as 401 with the boundary's own detail and a way to re-enter it;
 *  10. re-entering the right token and retrying makes the same write land (the pane recovers);
 *  11. a refused chat stream shows the server's reason, not `HTTP 401`;
 *  12. a full rate window is reported with the server's Retry-After as a countdown, and the
 *      Retry control says why it cannot work yet;
 *  13. (with --screenshots) the same refused apply at 390x740, in the theme that was asked for;
 *  14. no refusal drew a second presentation into the same host;
 *  15. none of the attacks raised a page error.
 *
 * Every check names whether its refusal was `real` or `injected`. `--screenshots DIR` also writes
 * `adversarial-<theme>-{apply-refusal,injected-503,rate-limit,narrow-apply-refusal}.png`; the
 * committed copies in docs/media/adversarial were taken by this walk on this tree.
 *
 * Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).
 * See docs/uiux-2-0-adversarial.md.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  baseUrl: "",
  token: "",
  timeoutMs: 120000,
  settleMs: 250,
  screenshots: "",
  theme: "dark",
  json: false,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS };
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
      case "--theme": args.theme = next(); break;
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
    "Attack the Studio's writes and check that every refusal is drawn as a refusal.",
    "",
    "  node scripts/adversarial_walk.mjs [--base-url URL --token TOKEN] [--json]",
    "  node scripts/adversarial_walk.mjs [--screenshots DIR] [--theme dark|light]",
    "",
    "Without --base-url the walk starts its own scripts/demo_tour.py --serve fixture.",
    "Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).",
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

/** One throwaway app, the same fixture the other walks use. */
async function startFixture() {
  const child = spawn(process.env.STUDIO_PYTHON || "python3", [resolve("scripts/demo_tour.py"), "--serve", "--port", "0"], {
    cwd: process.cwd(),
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The demo did not report itself in time.\n${stderr}`)), 90000);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        resolveReady(JSON.parse(line));
      } catch (error) {
        rejectReady(new Error(`The demo printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The demo exited with ${code}.\n${stderr}`));
    });
  });
  if (!(await waitForHttp(ready.url, 60000))) throw new Error(`The demo never answered at ${ready.url}.`);
  return { child, baseUrl: ready.url, token: ready.token };
}

/** The server's own words, copied from `app.py` so an injected refusal is not invented. */
const SERVER_DETAIL = {
  crossOrigin: "Cross-origin state-changing request rejected.",
  tooLarge: "Request body exceeds the configured size limit.",
  authUnavailable: "Authentication is unavailable; configure a token and restart Studio.",
  rateLimited: "Rate limit exceeded for expensive workflow requests.",
  authRequired: "Authentication required. Send `Authorization: Bearer <STUDIO_API_TOKEN>`.",
};

async function main(args) {
  const puppeteer = await loadPuppeteer();
  const chrome = resolveChrome();
  const results = [];
  const record = (title, passed, detail = "") => {
    results.push({ title, passed: Boolean(passed), detail });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${title}${detail ? ` — ${detail}` : ""}`);
  };

  let fixture = null;
  let baseUrl = args.baseUrl;
  let token = args.token;
  if (!baseUrl) {
    fixture = await startFixture();
    baseUrl = fixture.baseUrl;
    token = fixture.token;
  }
  const authHeaders = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const api = async (method, path, body) => {
    const response = await fetch(baseUrl + path, {
      method,
      headers: authHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    return { status: response.status, ok: response.ok, payload };
  };

  const pageErrors = [];
  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1440,900"],
  });

  const newPage = async (tokenForPage = token, viewport = { width: 1440, height: 900, deviceScaleFactor: 1 }) => {
    const page = await browser.newPage();
    await page.setViewport(viewport);
    await page.evaluateOnNewDocument((value, theme) => {
      sessionStorage.setItem("m5_api_token", value);
      // A headless browser has no human to type a token into the auth prompt, and a
      // prompt that never resolves would hang the walk instead of testing the pane.
      window.prompt = () => null;
      // Both themes are captured, because a refusal that is legible in one and not the
      // other is a story that only half shipped.
      localStorage.setItem("m5_theme", theme);
    }, tokenForPage, args.theme);
    page.on("pageerror", (error) => pageErrors.push(String(error && error.message ? error.message : error)));
    await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await page.waitForFunction(() => typeof window.selectHarnessSession === "function", { timeout: 30000 });
    return page;
  };

  const captures = [];
  const capture = async (page, name) => {
    if (!args.screenshots) return null;
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `adversarial-${args.theme}-${name}.png`);
    await page.screenshot({ path });
    captures.push(path);
    return path;
  };

  /** The presentation inside a host, as the page drew it. */
  const readPresentation = (page, hostId) => page.evaluate((id) => {
    const host = document.getElementById(id);
    // The dialog a refusal is drawn into has to stay open: a refusal behind a closed
    // dialog is the same as no refusal.
    const dialog = host && host.closest(".modal-backdrop");
    const stillOpen = dialog ? dialog.classList.contains("open") : true;
    if (!host) return { present: false, reason: `#${id} is not in the document` };
    const box = host.querySelector(".presentation");
    if (!box) return { present: false, reason: `#${id} holds no presentation` };
    const text = (selector) => {
      const node = box.querySelector(selector);
      return node ? (node.textContent || "").replace(/\s+/g, " ").trim() : "";
    };
    return {
      present: true,
      stillOpen,
      kind: box.dataset.kind || "",
      title: text(".presentation-title"),
      cause: text(".presentation-cause"),
      context: text(".presentation-context"),
      why: text(".presentation-why"),
      actions: [...box.querySelectorAll(".presentation-actions button")].map((button) => ({
        label: (button.textContent || "").trim(),
        disabled: button.disabled === true,
        reason: button.getAttribute("aria-describedby") ? text(".presentation-why") : (button.title || ""),
      })),
    };
  }, hostId);

  const toasts = (page) => page.evaluate(() => [...document.querySelectorAll("#toastStack .toast")]
    .map((node) => ({ tone: [...node.classList].find((name) => ["ok", "warn", "err", "info"].includes(name)) || "", text: (node.textContent || "").replace(/\s+/g, " ").trim() })));

  const clearToasts = (page) => page.evaluate(() => { const stack = document.getElementById("toastStack"); if (stack) stack.innerHTML = ""; });

  const countPresentations = (page, hostId) => page.evaluate((id) => {
    const host = document.getElementById(id);
    return host ? host.querySelectorAll(".presentation").length : -1;
  }, hostId);

  /** Run `action` with one API response replaced, then put the network back. */
  const withInjectedRefusal = async (page, match, status, body, action) => {
    const entry = { status, body };
    await page.setRequestInterception(true);
    const handler = (request) => {
      const url = request.url();
      if (request.method() === match.method && match.test(url)) {
        request.respond({ status: entry.status, contentType: "application/json", body: JSON.stringify(entry.body) });
        return;
      }
      request.continue();
    };
    page.on("request", handler);
    try {
      await action();
    } finally {
      page.off("request", handler);
      await page.setRequestInterception(false);
    }
  };

  try {
    const page = await newPage();
    const workspace = mkdtempSync(join(tmpdir(), "m5-adversarial-ws-"));
    const created = await api("POST", "/api/harness/sessions", {
      title: "adversarial walk",
      workspace_dir: workspace,
      use_worktree: false,
      policy: "plan",
    });
    const sessionId = created.payload && created.payload.session && created.payload.session.id;
    record("the server created a read-only session for the attacks", created.status === 200 && Boolean(sessionId), `POST /api/harness/sessions → ${created.status}`);

    await page.evaluate((id) => window.selectHarnessSession(id), sessionId);
    await sleep(args.settleMs);
    const opened = await page.evaluate((id) => ({
      label: (document.getElementById("harnessActiveLabel") || {}).textContent || "",
      policy: (document.getElementById("harnessPolicy") || {}).value || "",
      matches: (document.getElementById("harnessActiveLabel") || {}).textContent.includes(id),
    }), sessionId);
    record(
      "the harness pane opened the session the server holds, with the policy it holds",
      opened.matches && opened.policy === "plan",
      `label: ${opened.label.slice(0, 90)} | policy: ${opened.policy}`,
    );

    // ── 2. real 403: a read-only session refuses every worktree apply ──────────
    await clearToasts(page);
    // The Apply control lives in the diff dialog, and the demo fixture cannot create a
    // worktree session (`use_worktree: true` 500s on it), so the control that dialog shows
    // only to worktree sessions is revealed here. The click, the request and the refusal are
    // the app's own; what is staged is the dialog state a worktree session would have.
    await page.evaluate(() => window.openHarnessDiffModal());
    await sleep(args.settleMs);
    await page.evaluate(() => {
      const button = document.getElementById("applyWorktreeBtn");
      if (button) { button.style.display = "inline-flex"; button.click(); }
    });
    await sleep(args.settleMs * 2);
    const applyRefusal = await readPresentation(page, "harnessApplyRefusal");
    const applyToasts = await toasts(page);
    const claimedApplied = applyToasts.some((toast) => toast.tone === "ok" && /Applied/i.test(toast.text));
    record(
      "a read-only session's refused apply is drawn with the server's own reason, not as \"Applied!\"",
      applyRefusal.present
        && applyRefusal.kind === "error"
        && /403/.test(applyRefusal.title)
        && /Cannot apply worktree changes while session policy is read-only 'plan'/.test(applyRefusal.cause)
        && applyRefusal.context.length > 0
        && applyRefusal.actions.some((action) => /retry/i.test(action.label))
        && applyRefusal.stillOpen
        && !claimedApplied,
      `${applyRefusal.title || "no presentation"} | cause: ${(applyRefusal.cause || "").slice(0, 90)} | dialog open: ${applyRefusal.stillOpen} | toasts: ${applyToasts.map((toast) => `${toast.tone}:${toast.text}`).join(" · ") || "none"}`,
    );
    await capture(page, "apply-refusal");

    // ── 3. injected 503 on the same route ─────────────────────────────────────
    await page.evaluate(() => window.M5Presentations.clear("harnessApplyRefusal"));
    await clearToasts(page);
    await withInjectedRefusal(
      page,
      { method: "POST", test: (url) => /\/apply$/.test(url) },
      503,
      { detail: SERVER_DETAIL.authUnavailable },
      async () => {
        await page.evaluate(() => { const button = document.getElementById("applyWorktreeBtn"); if (button) button.click(); });
        await sleep(args.settleMs * 2);
      },
    );
    const unavailable = await readPresentation(page, "harnessApplyRefusal");
    record(
      "an injected 503 is drawn as an error with the server's own detail",
      unavailable.present
        && (unavailable.kind === "error" || unavailable.kind === "recovery")
        && /503/.test(unavailable.title)
        && unavailable.cause.includes(SERVER_DETAIL.authUnavailable)
        && unavailable.actions.length > 0,
      `${unavailable.title || "no presentation"} | ${(unavailable.cause || "").slice(0, 90)}`,
    );
    await capture(page, "injected-503");

    // ── 4. injected 507: disk full is a recovery state, and the diff stays ─────
    await page.evaluate(() => window.M5Presentations.clear("harnessApplyRefusal"));
    await withInjectedRefusal(
      page,
      { method: "POST", test: (url) => /\/apply$/.test(url) },
      507,
      { detail: "No space left on device while snapshotting the workspace." },
      async () => {
        await page.evaluate(() => { const button = document.getElementById("applyWorktreeBtn"); if (button) button.click(); });
        await sleep(args.settleMs * 2);
      },
    );
    const diskFull = await readPresentation(page, "harnessApplyRefusal");
    record(
      "an injected 507 says the server has no room to write and is drawn as a recovery",
      diskFull.present && diskFull.kind === "recovery" && /507/.test(diskFull.title) && /No space left/.test(diskFull.cause) && diskFull.context.length > 0,
      `${diskFull.title || "no presentation"} | ${(diskFull.cause || "").slice(0, 80)}`,
    );

    await page.evaluate(() => window.closeHarnessDiffModal());
    await sleep(args.settleMs);

    // ── 5. injected 413 on compaction ────────────────────────────────────────
    await clearToasts(page);
    await withInjectedRefusal(
      page,
      { method: "POST", test: (url) => /\/compact$/.test(url) },
      413,
      { detail: SERVER_DETAIL.tooLarge },
      async () => {
        await page.evaluate(() => window.compactActiveHarness());
        await sleep(args.settleMs * 2);
      },
    );
    const tooLarge = await readPresentation(page, "harnessRefusalState");
    const compactToasts = await toasts(page);
    record(
      "an injected 413 on compaction is drawn as over the size limit, not as \"Compaction checked\"",
      tooLarge.present
        && /413/.test(tooLarge.title)
        && tooLarge.cause.includes(SERVER_DETAIL.tooLarge)
        && !compactToasts.some((toast) => toast.tone === "ok" && /Compaction/i.test(toast.text)),
      `${tooLarge.title || "no presentation"} | toasts: ${compactToasts.map((toast) => `${toast.tone}:${toast.text}`).join(" · ") || "none"}`,
    );

    // ── 6. real 404: a session the server no longer has ──────────────────────
    const doomed = await api("POST", "/api/harness/sessions", { title: "deleted underneath", workspace_dir: workspace, use_worktree: false });
    const doomedId = doomed.payload && doomed.payload.session && doomed.payload.session.id;
    await page.evaluate((id) => window.selectHarnessSession(id), doomedId);
    await sleep(args.settleMs);
    const removed = await api("DELETE", `/api/harness/sessions/${encodeURIComponent(doomedId)}`);
    await clearToasts(page);
    await page.evaluate(() => window.compactActiveHarness());
    await sleep(args.settleMs * 2);
    const gone = await readPresentation(page, "harnessRefusalState");
    record(
      "a compact on a session the server no longer has says 404 and keeps the reader's place",
      removed.ok && gone.present && /404/.test(gone.title) && /not found/i.test(gone.cause) && gone.context.length > 0,
      `DELETE → ${removed.status} | ${gone.title || "no presentation"} | ${(gone.cause || "").slice(0, 90)}`,
    );

    // ── 7. injected cross-origin 403 on the Council spawn ────────────────────
    await page.evaluate(() => { window.switchTab("hall"); });
    await sleep(args.settleMs);
    const spawnText = "Audit the retry policy for the ingest queue.";
    await page.evaluate(() => { if (typeof window.openSpawnModal === "function") window.openSpawnModal(); });
    await sleep(args.settleMs);
    await withInjectedRefusal(
      page,
      { method: "POST", test: (url) => /\/api\/hall\/sessions$/.test(url) },
      403,
      { detail: SERVER_DETAIL.crossOrigin },
      async () => {
        await page.evaluate((text) => {
          const input = document.getElementById("spawnRequestInput");
          if (input) input.value = text;
          window.confirmSpawnSession(false);
        }, spawnText);
        await sleep(args.settleMs * 2);
      },
    );
    const spawnRefusal = await readPresentation(page, "hallStream");
    const spawnKept = await page.evaluate(() => ({
      modalOpen: !!(document.getElementById("spawnModal") || {}).classList
        && document.getElementById("spawnModal").classList.contains("open"),
      text: (document.getElementById("spawnRequestInput") || {}).value || "",
    }));
    record(
      "an injected cross-origin 403 on the Council spawn keeps the modal open with the reader's text",
      spawnRefusal.present && /403/.test(spawnRefusal.title) && spawnRefusal.cause.includes(SERVER_DETAIL.crossOrigin)
        && spawnKept.modalOpen === true && spawnKept.text.trim().length > 0,
      `${spawnRefusal.title || "no presentation"} | modal open: ${spawnKept.modalOpen} | text kept: ${spawnKept.text.slice(0, 40)}`,
    );

    // ── 8. a refused Council message puts the text back in the composer ──────
    await page.evaluate((text) => {
      const input = document.getElementById("hallHumanInput");
      if (input) input.value = text;
    }, "One more reading, please.");
    await withInjectedRefusal(
      page,
      { method: "POST", test: (url) => /\/message$/.test(url) },
      403,
      { detail: SERVER_DETAIL.crossOrigin },
      async () => {
        await page.evaluate(() => window.postHumanHallMessage(false));
        await sleep(args.settleMs * 2);
      },
    );
    const messageKept = await page.evaluate(() => (document.getElementById("hallHumanInput") || {}).value || "");
    const messageRefusal = await readPresentation(page, "hallStream");
    record(
      "a refused Council message puts the reader's text back in the composer",
      messageKept.includes("One more reading") && messageRefusal.present && /403/.test(messageRefusal.title),
      `composer: ${messageKept.slice(0, 40)} | ${messageRefusal.title || "no presentation"}`,
    );

    // ── 9. real 401 from the security boundary, and ──────────────────────────
    const badPage = await newPage();
    await badPage.evaluate((id) => window.selectHarnessSession(id), sessionId);
    await sleep(args.settleMs);
    await badPage.evaluate(() => sessionStorage.setItem("m5_api_token", "wrong-token-for-the-adversarial-walk"));
    await badPage.evaluate(() => window.compactActiveHarness());
    await sleep(args.settleMs * 2);
    const unauthorised = await readPresentation(badPage, "harnessRefusalState");
    record(
      "a wrong token is reported as 401 with the boundary's own detail and a way to re-enter it",
      unauthorised.present
        && /401/.test(unauthorised.title)
        && unauthorised.cause.includes(SERVER_DETAIL.authRequired)
        && unauthorised.actions.some((action) => /token/i.test(action.label))
        && unauthorised.context.length > 0,
      `${unauthorised.title || "no presentation"} | ${(unauthorised.cause || "").slice(0, 90)}`,
    );

    // ── 10. …re-entering the right token makes the same write land ───────────
    await badPage.evaluate((value) => sessionStorage.setItem("m5_api_token", value), token);
    await badPage.evaluate(() => window.compactActiveHarness());
    await sleep(args.settleMs * 3);
    const afterRecovery = await badPage.evaluate(() => ({
      refusal: !!document.querySelector("#harnessRefusalState .presentation"),
      langToast: [...document.querySelectorAll("#toastStack .toast")].map((node) => (node.textContent || "").trim()),
    }));
    record(
      "with the right token the same write lands and the refusal is gone",
      afterRecovery.refusal === false
        && afterRecovery.langToast.some((text) => /Compaction|compact/i.test(text)),
      `presentation left: ${afterRecovery.refusal} | toasts: ${afterRecovery.langToast.join(" · ") || "none"}`,
    );
    await badPage.close();

    // ── 11. a refused chat stream shows the server's reason ──────────────────
    const streamPage = await newPage();
    await streamPage.evaluate(() => window.switchTab("chat"));
    await sleep(args.settleMs);
    await streamPage.evaluate(() => sessionStorage.setItem("m5_api_token", "wrong-token-for-the-chat-stream"));
    await streamPage.evaluate(() => {
      const input = document.getElementById("chatInput");
      if (input) input.value = "Does a refused stream say why?";
      const button = document.getElementById("chatSendBtn");
      if (button) button.click();
      else if (typeof window.sendChat === "function") window.sendChat();
    });
    await sleep(args.settleMs * 3);
    const streamRefusal = await readPresentation(streamPage, "chatState");
    record(
      "a refused chat stream shows the server's reason, not \"HTTP 401\"",
      streamRefusal.present
        && streamRefusal.cause.includes(SERVER_DETAIL.authRequired)
        && !/^HTTP \d+$/.test(streamRefusal.cause.trim()),
      `${streamRefusal.title || "no presentation"} | ${(streamRefusal.cause || "").slice(0, 100)}`,
    );
    await streamPage.close();

    // ── 12. a full rate window states its Retry-After ────────────────────────
    // The boundary counts every POST on the expensive routes, so a cheap body that answers
    // 400 still fills the window - and doing it here, last, keeps the 60 s it costs away
    // from the writes above.
    let limited = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const response = await fetch(`${baseUrl}/api/chat/stream`, {
        method: "POST",
        headers: authHeaders,
        body: "{}",
      });
      if (response.status === 429) {
        limited = { status: response.status, retryAfter: response.headers.get("retry-after"), detail: (await response.json()).detail };
        break;
      }
      await response.text();
    }
    await page.evaluate(() => window.switchTab("chat"));
    await clearToasts(page);
    await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      if (input) input.value = "Does the rate window say when it clears?";
      const button = document.getElementById("chatSendBtn");
      if (button) button.click();
      else if (typeof window.sendChat === "function") window.sendChat();
    });
    await sleep(args.settleMs * 3);
    const rateRefusal = await readPresentation(page, "chatState");
    const retryAction = rateRefusal.actions ? rateRefusal.actions[0] || {} : {};
    record(
      "a full rate window is reported with the server's Retry-After as a countdown",
      Boolean(limited)
        && limited.status === 429
        && Number(limited.retryAfter) > 0
        && rateRefusal.present
        && /429/.test(rateRefusal.cause + rateRefusal.title)
        && retryAction.disabled === true
        && /Retry in \d+s/.test(retryAction.label || ""),
      limited
        ? `boundary: 429 in ${limited.retryAfter}s | pane: ${rateRefusal.title || "no presentation"} | ${(rateRefusal.cause || "").slice(0, 80)} | first action: ${retryAction.label}${retryAction.disabled ? " (disabled)" : ""}`
        : "the rate window never filled",
    );
    await capture(page, "rate-limit");

    // ── captures: the same refusal at 390px, on the theme that was asked for ──
    if (args.screenshots) {
      const narrow = await newPage(token, { width: 390, height: 740, deviceScaleFactor: 1 });
      await narrow.evaluate((id) => window.selectHarnessSession(id), sessionId);
      await sleep(args.settleMs);
      await narrow.evaluate(() => window.openHarnessDiffModal());
      await sleep(args.settleMs);
      await narrow.evaluate(() => {
        const button = document.getElementById("applyWorktreeBtn");
        if (button) { button.style.display = "inline-flex"; button.click(); }
      });
      await sleep(args.settleMs * 2);
      await capture(narrow, "narrow-apply-refusal");
      const drawnTheme = await narrow.evaluate(() => (document.body.dataset.theme || ""));
      const narrowRefusal = await readPresentation(narrow, "harnessApplyRefusal");
      record(
        "the captures show the refusal in the theme that was asked for, at 390px",
        drawnTheme === args.theme && narrowRefusal.present && narrowRefusal.actions.length > 0,
        `asked ${args.theme}, page reports ${drawnTheme} | narrow: ${narrowRefusal.title || "no presentation"}`,
      );
      await narrow.close();
    }

    // ── 13/14. one presentation per host, and no page error ──────────────────
    const duplicates = [];
    for (const host of ["harnessRefusalState", "harnessApplyRefusal", "hallStream", "chatState"]) {
      const count = await countPresentations(page, host);
      if (count > 1) duplicates.push(`${host}: ${count}`);
    }
    record("no refusal drew a second presentation into the same host", duplicates.length === 0, duplicates.join(" · ") || "one box in every host");

    record("none of the attacks raised a page error", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    await page.close();
  } finally {
    await browser.close();
    if (fixture) {
      try { process.kill(-fixture.child.pid, "SIGTERM"); } catch (_) { /* already gone */ }
    }
  }

  const failed = results.filter((entry) => !entry.passed);
  if (args.json) {
    console.log(JSON.stringify({ checks: results, passed: results.length - failed.length, failed: failed.length, injected: ["cross-origin 403", "413", "503", "507"], theme: args.theme, screenshots: captures.length }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  }
  return failed.length === 0 ? 0 : 1;
}

main(parseArgs(process.argv.slice(2)))
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`adversarial walk: ${error.message}`);
    process.exit(1);
  });
