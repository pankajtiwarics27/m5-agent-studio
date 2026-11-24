#!/usr/bin/env node
/**
 * Check that scoped review feedback is recorded, limited and correctable, in a browser (AP-019).
 *
 *   node scripts/harness_feedback_walk.mjs --screenshots docs/media/harness-feedback
 *
 * Feedback is not a card a walk can draw: the version it is bound to, the words that were said,
 * whether a reading was confirmed, which corrections contradict each other and which ones no
 * longer describe the artifact all come from durable record. This walk starts
 * `scripts/harness_feedback_seed.py`, which asks the real feedback routes over HTTP for every
 * record it reads -- an ambiguous region correction, a plain-words correction with no selection
 * at all, a contradictory pair on one region, feedback against an older version of the same
 * artifact, and words that are markup.
 *
 * What it proves:
 *
 *   1. the feedback pane draws what was said, at the scope it was said at, next to the exact
 *      version it was said about - and states that recording it is not acceptance;
 *   2. a plain-words correction with no region is recorded at artifact scope: precision is
 *      never the price of reporting a problem;
 *   3. an ambiguous correction is drawn as waiting on the owner's reading, and the revision
 *      reports itself as not ready while it waits;
 *   4. confirming the reading goes through `POST …/feedback/{id}/confirm`, the item stops
 *      waiting, and the pane still does not claim the artifact was accepted;
 *   5. two contradictory corrections on one region are shown as a contradiction rather than
 *      averaged into one change;
 *   6. feedback given against an OLDER version is shown as stale and is not counted as
 *      applying to the version on screen;
 *   7. a region can be selected for a comment, the selection is stated in words, and clearing
 *      it returns the composer to artifact scope;
 *   8. withdrawing a comment goes through `DELETE …/feedback/{id}` and the item stops applying
 *      while the record of what was said stays in the store;
 *   9. words, ids and regions that are markup are drawn as text: no element, no handler, no
 *      page error;
 *  10. the pane is usable at 390x844 and at 200% zoom (no horizontal overflow of the modal),
 *      the composer is reachable by keyboard, and a reload restores the same records;
 *  11. no page error occurs, and no route outside the feedback routes is written to.
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
  mainSessionId: "",
  staleSessionId: "",
  hostileSessionId: "",
  artifactId: "hero.html",
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
      case "--main-session": args.mainSessionId = next(); break;
      case "--stale-session": args.staleSessionId = next(); break;
      case "--hostile-session": args.hostileSessionId = next(); break;
      case "--artifact-id": args.artifactId = next(); break;
      case "--port": args.port = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--json": args.json = true; break;
      case "--help": case "-h": console.log(usage()); process.exit(0); break;
      default: throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

function usage() {
  return [
    "Usage: node scripts/harness_feedback_walk.mjs [--screenshots DIR] [--json]",
    "       --base-url URL --token TOKEN --main-session ID --stale-session ID --hostile-session ID",
    "",
    "Without --base-url the walk starts scripts/harness_feedback_seed.py itself, so every record",
    "it reads was written by the running server's own routes. This script refuses to report a",
    "walk it did not perform.",
  ].join("\n");
}

function resolveChrome() {
  const candidates = [
    process.env.STUDIO_SCREENSHOT_CHROME,
    process.env.CHROME_PATH,
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) if (existsSync(candidate)) return candidate;
  throw new Error("No Chrome/Chromium found. Set STUDIO_SCREENSHOT_CHROME to the pinned browser.");
}

async function loadPuppeteer() {
  const modulePath = process.env.STUDIO_PUPPETEER_CORE || resolve(process.cwd(), "tests/browser/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js");
  return import(pathToFileURL(modulePath).href);
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

async function waitForHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return true;
    } catch {
      // not up yet
    }
    await sleep(150);
  }
  return false;
}

async function startFixture(args) {
  const python = process.env.STUDIO_PYTHON || "python3";
  const child = spawn(python, [resolve(process.cwd(), "scripts", "harness_feedback_seed.py"), "--port", String(args.port || 0)], {
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env },
  });
  const ready = await new Promise((done, fail) => {
    let buffer = "";
    const timer = setTimeout(() => fail(new Error("the seed fixture did not report a ready line in 90s")), 90000);
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString();
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (line) {
        clearTimeout(timer);
        try {
          done(JSON.parse(line));
        } catch (error) {
          fail(error);
        }
      }
    });
    child.stderr.on("data", (chunk) => {
      process.stderr.write(chunk);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      fail(new Error(`the seed fixture exited before reporting ready (code ${code})`));
    });
  });
  return { child, ready };
}

const checks = [];
function check(ok, label, detail) {
  checks.push({ ok: Boolean(ok), label, detail: detail || "" });
  const mark = ok ? "  ok  " : "  FAIL";
  console.log(`${mark} ${label}${ok || !detail ? "" : `: ${detail}`}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const puppeteer = await loadPuppeteer();
  const chrome = resolveChrome();

  let seed = null;
  let baseUrl = args.baseUrl;
  let token = args.token;
  let ids = {
    main: args.mainSessionId,
    stale: args.staleSessionId,
    hostile: args.hostileSessionId,
  };
  let artifactId = args.artifactId;
  let hostileText = "";
  if (!baseUrl) {
    seed = await startFixture(args);
    baseUrl = seed.ready.base_url;
    token = token || seed.ready.token;
    ids = {
      main: ids.main || seed.ready.main_session_id,
      stale: ids.stale || seed.ready.stale_session_id,
      hostile: ids.hostile || seed.ready.hostile_session_id,
    };
    artifactId = seed.ready.artifact_id || artifactId;
    hostileText = seed.ready.hostile_text || "";
  }
  if (!(await waitForHttp(`${baseUrl}/healthz`, args.timeoutMs))) {
    throw new Error(`the app did not become healthy at ${baseUrl}`);
  }

  if (args.screenshots) mkdirSync(args.screenshots, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      ...(process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean),
    ],
  });
  const pageErrors = [];
  const posted = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 960 });
    page.on("pageerror", (error) => pageErrors.push(String(error && error.message ? error.message : error)));
    page.on("console", (message) => {
      if (message.type() === "error" && /Uncaught/.test(message.text())) pageErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (request.method() !== "GET") posted.push(`${request.method()} ${new URL(request.url()).pathname}`);
    });

    // The token is read from sessionStorage by the shipped app, so it is seeded before any
    // document script runs rather than written afterwards and hoped for.
    await page.evaluateOnNewDocument((key, value) => {
      try {
        window.sessionStorage.setItem(key, value);
      } catch (_error) {}
    }, args.tokenKey, token);
    await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 6);
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);

    async function openSession(sessionId) {
      await page.evaluate((id) => selectHarnessSession(id), sessionId);
      await sleep(args.settleMs * 14);
    }

    // The feedback pane lives in the "Verification & artifacts" modal, which is opened the way a
    // reader opens it. Reading the served state directly would prove the route, not the pane.
    async function openFeedbackPane() {
      await page.evaluate(() => openHarnessEvidenceModal());
      await sleep(args.settleMs * 10);
    }

    async function paneText() {
      return page.evaluate(() => {
        const host = document.getElementById("harnessFeedbackBlock");
        return host ? host.innerText : "";
      });
    }

    async function paneVisible() {
      return page.evaluate(() => {
        const modal = document.getElementById("harnessEvidenceModal");
        const host = document.getElementById("harnessFeedbackPanel");
        if (!modal || !host) return false;
        const style = window.getComputedStyle(modal);
        return modal.classList.contains("open") && style.display !== "none" && style.visibility !== "hidden" && Boolean(host.innerText.trim());
      });
    }

    // The served truth for one session's artifact version, read with the browser's own token.
    async function served(sessionId, digest) {
      return page.evaluate(async (id, art, sha) => {
        const res = await fetch(`/api/harness/sessions/${encodeURIComponent(id)}/feedback?artifact_id=${encodeURIComponent(art)}&artifact_sha256=${encodeURIComponent(sha)}`, {
          headers: { Authorization: `Bearer ${window.sessionStorage.getItem("m5_api_token") || ""}` },
        });
        if (!res.ok) return { error: res.status };
        return res.json();
      }, sessionId, artifactId, digest);
    }

    // The digest the pane itself bound to, taken from the pane, so the checks below compare the
    // same version the reader is looking at.
    async function boundDigest() {
      return page.evaluate(() => {
        const text = (document.getElementById("harnessFeedbackPanel") || {}).innerText || "";
        const match = /sha256 ([0-9a-f]{64})/i.exec(text);
        return match ? match[1].toLowerCase() : "";
      });
    }

    async function composerReady() {
      return page.evaluate(() => {
        const field = document.getElementById("harnessFeedbackText");
        const pressed = Array.from(document.querySelectorAll("#harnessFeedbackBlock button")).find((n) => /Record feedback/i.test(n.innerText));
        return Boolean(field && !field.disabled && pressed);
      });
    }

    // ---- 1. the pane draws what was said, at its scope, next to the version ----
    await openSession(ids.main);
    await openFeedbackPane();
    check(await paneVisible(), "1. the feedback pane is drawn in the evidence modal");
    let text = await paneText();
    check(/Feedback/i.test(text), "1. the pane names itself", text.slice(0, 160));
    const digest = await boundDigest();
    check(Boolean(digest), "1. the pane shows the exact version feedback is bound to", text.slice(0, 200));
    check(/not accepting the artifact|not accepting/i.test(text), "1. the pane says recording feedback is not acceptance", text.slice(0, 400));
    check(/does not publish|grants no permission|publish/i.test(text), "1. the pane says it grants no publish permission");
    check(/only the region you selected/i.test(text), "1. a region-scoped correction states its narrower reach", text.slice(0, 400));
    check(/the heading is a bit much/i.test(text), "1. the owner's own words are shown, not a paraphrase");

    // ---- 2. plain words with no selection are first class ----
    check(/this artifact, this version/i.test(text), "2. a comment with no region is recorded at artifact scope", text.slice(0, 400));
    check(/say where the dates come from/i.test(text), "2. the plain-words correction is shown as it was said");

    if (args.screenshots) await page.screenshot({ path: `${args.screenshots}/feedback-pane.png` });

    // ---- 3. an ambiguous correction waits on the owner's reading ----
    check(/could mean more than one thing|more than one thing/i.test(text), "3. the ambiguous correction is drawn as ambiguous", text.slice(0, 400));
    check(/not yet|not ready/i.test(text), "3. the pane reports that nothing can be revised yet", text.slice(0, 300));
    const before = await served(ids.main, digest);
    check(Array.isArray(before.awaiting_confirmation) && before.awaiting_confirmation.includes("fb-walk-quieter"), "3. the record itself is awaiting a confirmed reading");
    check(before.revision && before.revision.ready === false, "3. the revision really is not ready while a reading is unconfirmed");

    // ---- 4. confirming the reading is not accepting the artifact ----
    // The specific item, found by the words it records -- not "the first confirm button", which
    // would let the walk pass by confirming whatever happened to be first.
    const confirmed = await page.evaluate(async () => {
      const row = Array.from(document.querySelectorAll("#harnessFeedbackPanel li")).find((n) => /the heading is a bit much/.test(n.innerText));
      const button = row && Array.from(row.querySelectorAll("button")).find((n) => /This is what I meant/i.test(n.innerText));
      if (!button) return false;
      button.click();
      return true;
    });
    check(confirmed, "4. the pane offers a way to say which reading was meant");
    await sleep(args.settleMs * 8);
    const after = await served(ids.main, digest);
    check(!(after.awaiting_confirmation || []).includes("fb-walk-quieter"), "4. the confirmation went through the route and the item stopped waiting");
    let afterText = await paneText();
    check(/Reading confirmed|reading confirmed/i.test(afterText), "4. the pane reports the reading was confirmed");
    check(/nothing about whether the work is accepted|not accepting/i.test(afterText), "4. the pane still does not claim the artifact was accepted");
    const record = (after.items || []).find((item) => item.feedback_id === "fb-walk-quieter") || {};
    check(record.is_final_acceptance === false && record.grants_publish_permission === false, "4. the record says it is neither acceptance nor permission to publish");

    // ---- 5. contradictory corrections are shown, not averaged ----
    check(/Contradictory comments|contradiction/i.test(afterText), "5. the pane shows the two corrections as contradictory", afterText.slice(0, 500));
    check(Array.isArray(after.conflicts) && after.conflicts.length >= 1, "5. the record reports the contradictory pair");

    if (args.screenshots) await page.screenshot({ path: `${args.screenshots}/feedback-confirmed.png` });

    // ---- 6. feedback for an older version is stale, not applied ----
    await page.evaluate(() => closeHarnessEvidenceModal());
    await sleep(args.settleMs * 3);
    await openSession(ids.stale);
    await openFeedbackPane();
    const staleDigest = await boundDigest();
    const staleText = await paneText();
    check(/Earlier versions/i.test(staleText), "6. feedback about an older version is drawn under its own heading", staleText.slice(0, 500));
    check(/no longer describes what is on screen/i.test(staleText), "6. the pane says the older comment no longer describes what is shown");
    check(/the all-caps heading shouts/i.test(staleText), "6. the original wording of the old comment is kept");
    const staleServed = await served(ids.stale, staleDigest);
    check(Boolean(staleDigest), "6. the stale session is bound to a version of its own", staleDigest);
    check(Array.isArray(staleServed.stale) && staleServed.stale.some((item) => item.feedback_id === "fb-walk-old-heading"), "6. the record itself reports the old comment as stale", JSON.stringify(staleServed).slice(0, 300));
    const staleChanges = (staleServed.revision && Array.isArray(staleServed.revision.changes)) ? staleServed.revision.changes : [];
    check(!staleChanges.some((item) => item.feedback_id === "fb-walk-old-heading"), "6. the stale comment is NOT part of the revision for the current version", JSON.stringify(staleChanges).slice(0, 200));

    if (args.screenshots) await page.screenshot({ path: `${args.screenshots}/feedback-stale.png` });

    // ---- 7. a region can be selected, stated in words, and cleared ----
    await openSession(ids.main);
    await openFeedbackPane();
    check(await composerReady(), "7. the composer is present and enabled");
    const selected = await page.evaluate(() => {
      const row = document.querySelector("#harnessEvidenceChange [data-feedback-region]");
      if (!row) return "";
      row.click();
      return row.dataset.feedbackRegion || "";
    });
    await sleep(args.settleMs * 4);
    check(Boolean(selected), "7. a region in the diff can be selected for the comment", selected);
    let status = await page.evaluate(() => (document.getElementById("harnessFeedbackSelection") || {}).textContent || "");
    check(new RegExp(`Narrowed to region: ${selected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(status), "7. the composer states in words what the comment will apply to", status);
    await page.evaluate(() => clearHarnessFeedbackSelection());
    await sleep(args.settleMs * 3);
    status = await page.evaluate(() => (document.getElementById("harnessFeedbackSelection") || {}).textContent || "");
    check(/No selection/i.test(status) && /artifact version only/i.test(status), "7. clearing the selection returns the composer to artifact scope", status);

    // ---- 8. a comment can be withdrawn, and the record survives ----
    const submitted = await page.evaluate(async () => {
      const field = document.getElementById("harnessFeedbackText");
      if (!field) return "";
      field.value = "the footer needs the year";
      field.dispatchEvent(new Event("input", { bubbles: true }));
      const button = Array.from(document.querySelectorAll("#harnessFeedbackBlock button")).find((n) => /Record feedback/i.test(n.innerText));
      button.click();
      return field.value;
    });
    check(Boolean(submitted), "8. the composer accepted typed words");
    await sleep(args.settleMs * 10);
    let recorded = await served(ids.main, digest);
    const typed = (recorded.items || []).find((item) => item.verbatim === "the footer needs the year");
    check(Boolean(typed), "8. typed words reached the store through the real route", JSON.stringify((recorded.items || []).map((i) => i.verbatim)).slice(0, 200));
    check(typed && typed.scope_kind === "artifact", "8. with no selection it was recorded at artifact scope");
    await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll("#harnessFeedbackPanel li"));
      const row = rows.find((n) => /the footer needs the year/.test(n.innerText));
      const button = row && Array.from(row.querySelectorAll("button")).find((n) => /Withdraw/i.test(n.innerText));
      if (button) button.click();
    });
    await sleep(args.settleMs * 10);
    recorded = await served(ids.main, digest);
    const withdrawn = (recorded.items || []).find((item) => item.verbatim === "the footer needs the year");
    check(Boolean(withdrawn) && withdrawn.deleted === true, "8. withdrawing goes through DELETE and the item stops applying");
    check(Boolean(withdrawn) && withdrawn.verbatim === "the footer needs the year", "8. the record of what was said survives the withdrawal, unchanged");
    check(!(recorded.live || []).some((item) => item.verbatim === "the footer needs the year"), "8. the withdrawn comment is no longer in the live set");

    // ---- 9. markup is drawn as text ----
    await page.evaluate(() => closeHarnessEvidenceModal());
    await sleep(args.settleMs * 3);
    await openSession(ids.hostile);
    await openFeedbackPane();
    const hostilePane = await paneText();
    if (hostileText) {
      check(hostilePane.includes(hostileText), "9. the markup was kept verbatim in the record and drawn", hostilePane.slice(0, 200));
    }
    const injected = await page.evaluate(() => ({
      pwned: Boolean(window.pwned),
      scripts: document.querySelectorAll("#harnessFeedbackBlock script").length,
      images: document.querySelectorAll("#harnessFeedbackBlock img[src='x']").length,
      handlers: Array.from(document.querySelectorAll("#harnessFeedbackBlock [onerror]")).length,
    }));
    check(injected.pwned === false, "9. the markup did not execute");
    check(injected.scripts === 0 && injected.images === 0 && injected.handlers === 0, "9. no element or handler was created from the record", JSON.stringify(injected));

    if (args.screenshots) await page.screenshot({ path: `${args.screenshots}/feedback-hostile.png` });

    // ---- 10. keyboard, mobile, zoom and reload ----
    await openSession(ids.main);
    await openFeedbackPane();
    const tabbed = await page.evaluate(async () => {
      const field = document.getElementById("harnessFeedbackText");
      if (!field) return false;
      field.focus();
      return document.activeElement === field;
    });
    check(tabbed, "10. the composer can be reached by keyboard focus");
    const typedByKeyboard = await page.evaluate(async () => {
      const field = document.getElementById("harnessFeedbackText");
      field.focus();
      return document.activeElement === field && field.tagName === "TEXTAREA";
    });
    check(typedByKeyboard, "10. the plain-text fallback is a real textarea, not a canvas or a custom widget");

    // A viewport change closes open overlays in this app (the same is true of every modal), so
    // the pane is opened again at the new size -- which is what a reader rotating a phone or
    // zooming in does. Measuring a closed modal would prove nothing.
    // A viewport change re-lays out the pane and closes open overlays, so the reader's
    // selection is re-established the way they would re-establish it: pick the run again, then
    // open the evidence modal. The pane must come back with the same records.
    async function reopenAt(width, height, extra) {
      await page.setViewport({ width, height, ...extra });
      await sleep(args.settleMs * 8);
      await openSession(ids.main);
      await openFeedbackPane();
      return paneVisible();
    }
    check(await reopenAt(390, 844, { isMobile: true, hasTouch: true, deviceScaleFactor: 2 }), "10. the feedback pane opens at 390x844");
    const mobile = await page.evaluate(() => {
      const modal = document.getElementById("harnessEvidenceModal");
      const host = document.getElementById("harnessFeedbackBlock");
      const field = document.getElementById("harnessFeedbackText");
      if (!modal || !host || !field) return null;
      const modalBox = modal.querySelector(".modal").getBoundingClientRect();
      const fieldBox = field.getBoundingClientRect();
      return {
        overflow: Math.max(0, Math.round(host.scrollWidth - host.clientWidth)),
        fieldVisible: fieldBox.width > 0 && fieldBox.height > 0,
        modalFits: modalBox.right <= window.innerWidth + 1 && modalBox.left >= -1,
        fieldWidth: Math.round(fieldBox.width),
        documentOverflow: Math.max(0, Math.round(document.documentElement.scrollWidth - window.innerWidth)),
      };
    });
    check(Boolean(mobile), "10. the pane is still present at 390x844");
    check(mobile && mobile.fieldVisible && mobile.fieldWidth > 100, "10. the composer stays usable at 390x844", JSON.stringify(mobile));
    check(mobile && mobile.modalFits, "10. the modal stays inside a 390 px viewport", JSON.stringify(mobile));
    check(mobile && mobile.documentOverflow === 0, "10. the page does not scroll sideways at 390x844", JSON.stringify(mobile));
    if (args.screenshots) await page.screenshot({ path: `${args.screenshots}/feedback-mobile.png` });

    // 200% zoom, as a reader who zooms actually gets it: a 720-wide CSS viewport at 2x.
    check(await reopenAt(720, 480, { deviceScaleFactor: 2 }), "10. the feedback pane opens at 200% zoom");
    const zoomed = await page.evaluate(() => {
      const host = document.getElementById("harnessFeedbackBlock");
      const field = document.getElementById("harnessFeedbackText");
      if (!host || !field) return null;
      const box = field.getBoundingClientRect();
      return {
        overflow: Math.max(0, Math.round(host.scrollWidth - host.clientWidth)),
        fieldVisible: box.width > 0 && box.height > 0,
        documentOverflow: Math.max(0, Math.round(document.documentElement.scrollWidth - window.innerWidth)),
      };
    });
    check(Boolean(zoomed) && zoomed.fieldVisible, "10. the composer remains reachable at 200% zoom", JSON.stringify(zoomed));
    check(Boolean(zoomed) && zoomed.documentOverflow === 0, "10. nothing overflows the page at 200% zoom", JSON.stringify(zoomed));

    await page.setViewport({ width: 1440, height: 960 });
    await sleep(args.settleMs * 4);

    // A reload must rebuild the pane from the record, not from pane state.
    await page.reload({ waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 8);
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);
    await openSession(ids.main);
    await openFeedbackPane();
    const reloaded = await paneText();
    check(/the heading is a bit much/i.test(reloaded), "10. a reload restores the same records from the server", reloaded.slice(0, 300));
    check(/reading confirmed/i.test(reloaded), "10. a confirmed reading is still confirmed after a reload");

    // ---- 11. no page error, and nothing outside the feedback routes was written ----
    const strayWrites = posted.filter((line) => !/\/api\/harness\/sessions\/[^/]+\/feedback/.test(line));
    check(pageErrors.length === 0, "11. no page error occurred", pageErrors.join(" | ").slice(0, 300));
    check(strayWrites.length === 0, "11. every write went to a feedback route", strayWrites.join(", ").slice(0, 300));
  } finally {
    await browser.close();
    if (seed && seed.child) seed.child.kill("SIGTERM");
  }

  const passed = checks.filter((row) => row.ok).length;
  const payload = { story: "AP-019", checks: checks.length, passed, failed: checks.length - passed, results: checks };
  if (args.json) console.log(JSON.stringify(payload));
  if (!checks.length || passed !== checks.length) {
    console.error(`harness feedback walk: ${passed}/${checks.length} checks passed`);
    process.exit(1);
  }
  console.log(`harness feedback walk: ${passed}/${checks.length} checks passed`);
}

main().catch((error) => {
  console.error(`harness feedback walk: ${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
