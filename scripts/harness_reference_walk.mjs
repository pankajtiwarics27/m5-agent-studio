#!/usr/bin/env node
/**
 * Check that an owner-approved reference is versioned, limited and never mistaken for permission,
 * in a browser (AP-020).
 *
 *   node scripts/harness_reference_walk.mjs --screenshots docs/media/harness-reference
 *
 * A reference pane is not a card a walk can draw: which artifact version the owner approved, in
 * whose words, whether that version was replaced, why a reference was offered for a task and what
 * it therefore does not settle all come from durable record. This walk starts
 * `scripts/harness_reference_seed.py`, which asks the real reference routes over HTTP for every
 * record it reads -- an approved reference with a separately approved second version, a revoked
 * reference, a record whose id and approval words are markup, and a reference belonging to
 * another project.
 *
 * What it proves:
 *
 *   1. the pane draws the owner's own words beside the exact artifact version they approved, and
 *      states that adopting a reference is not accepting or publishing anything;
 *   2. an approved second version is shown as a new version, and the earlier approval is still
 *      drawn exactly as it was given -- enhancement did not rewrite it;
 *   3. a revoked reference keeps its record and shows its status rather than disappearing;
 *   4. a reference approved for another project is not offered to this one;
 *   5. the pane names what a reference never settles: new code, new facts, security sign-off,
 *      screenshot approval, final acceptance and publish permission;
 *   6. words, ids and briefs that are markup are drawn as text: no element, no handler, no page
 *      error;
 *   7. a reference cannot be recorded without a typed approver and the owner's own words: the
 *      pane refuses and sends nothing rather than inventing an approver;
 *   8. history is append-only at the route: the earlier version is still readable after the
 *      enhancement, with its original approval;
 *   9. the pane is usable at 390x844 and at 200% zoom, is reachable by keyboard, and a reload
 *      restores the same records from the server;
 *  10. no page error occurs, and no route outside the reference routes is written to.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
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
  otherSessionId: "",
  artifactId: "hero.html",
  referenceId: "ref/website",
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
      case "--other-session": args.otherSessionId = next(); break;
      case "--artifact-id": args.artifactId = next(); break;
      case "--reference-id": args.referenceId = next(); break;
      case "--port": args.port = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--json": args.json = true; break;
      case "--help": case "-h": console.log(usage()); process.exit(0); break;
      default: throw new Error(`unknown argument ${arg}`);
    }
  }
  return args;
}

function usage() {
  return [
    "node scripts/harness_reference_walk.mjs [--screenshots DIR] [--base-url URL --token TOKEN]",
    "",
    "Without --base-url the walk starts scripts/harness_reference_seed.py and stages every record",
    "through the real routes before driving the shipped pane.",
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
  const child = spawn(python, [resolve(process.cwd(), "scripts", "harness_reference_seed.py"), "--port", String(args.port || 0)], {
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
const capturedShots = [];
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
    other: args.otherSessionId,
  };
  let artifactId = args.artifactId;
  let referenceId = args.referenceId;
  let hostileText = "";
  let approvalWords = "";
  let secondWords = "";
  if (!baseUrl) {
    seed = await startFixture(args);
    baseUrl = seed.ready.base_url;
    token = token || seed.ready.token;
    ids = {
      main: ids.main || seed.ready.main_session_id,
      stale: ids.stale || seed.ready.stale_session_id,
      hostile: ids.hostile || seed.ready.hostile_session_id,
      other: ids.other || seed.ready.other_session_id,
    };
    artifactId = seed.ready.artifact_id || artifactId;
    referenceId = seed.ready.reference_id || referenceId;
    hostileText = seed.ready.hostile_text || "";
    approvalWords = seed.ready.approval_words || "";
    secondWords = seed.ready.second_words || "";
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

    // The reference pane lives in the "Verification & artifacts" modal, opened the way a reader
    // opens it. Reading the served state directly would prove the route, not the pane.
    async function openReferencePane() {
      await page.evaluate(() => openHarnessEvidenceModal());
      await sleep(args.settleMs * 14);
    }

    async function paneText() {
      return page.evaluate(() => {
        const host = document.getElementById("harnessReferenceBlock");
        return host ? host.innerText : "";
      });
    }

    // Each capture is recorded as it is taken, so the set carries its own manifest: when it was
    // shot, at what viewport, and the checksum of every file. A review that cannot say what it
    // looked at is not a review.
    // A capture goes into a sub-folder per viewport and theme, which is the layout this tree
    // already uses. One folder carrying two viewports would make every checksum in it
    // uninterpretable: a reader cannot say which size a given file was shot at.
    async function capture(file, label, meta) {
      const folder = resolve(args.screenshots, meta.folder || "");
      mkdirSync(folder, { recursive: true });
      const target = join(folder, file);
      const buffer = await page.screenshot({ type: "png", fullPage: false });
      writeFileSync(target, buffer);
      const shot = {
        id: file.replace(/\.png$/, ""),
        file,
        folder: meta.folder || "",
        label,
        bytes: buffer.length,
        sha256: createHash("sha256").update(buffer).digest("hex"),
        viewport: { width: meta.width, height: meta.height, device_scale_factor: meta.device_scale_factor },
        theme: meta.theme,
      };
      capturedShots.push(shot);
      return shot;
    }

    async function paneVisible() {
      return page.evaluate(() => {
        const modal = document.getElementById("harnessEvidenceModal");
        const host = document.getElementById("harnessReferencePanel");
        if (!modal || !host) return false;
        const style = window.getComputedStyle(modal);
        return modal.classList.contains("open") && style.display !== "none" && style.visibility !== "hidden" && Boolean(host.innerText.trim());
      });
    }

    // ---- 1. the approved version, in the owner's own words ----
    await openSession(ids.main);
    await openReferencePane();
    check(await paneVisible(), "1. the reference pane opens on the project's approved references");
    const first = await paneText();
    check(first.includes(referenceId), "1. the recorded reference is drawn by its id", first.slice(0, 400));
    check(
      approvalWords && first.includes(approvalWords),
      "1. the owner's own approval words are drawn, not a paraphrase",
      first.slice(0, 400),
    );
    check(/owner:priya/.test(first), "1. the record names the person who approved it");
    check(/What a reference never settles/i.test(first), "1. the pane states what a reference never settles", first.slice(0, 400));
    check(!/permission to publish|approved to ship/i.test(first), "1. the pane never claims publish permission", first.slice(0, 400));

    // ---- 2 & 8. two versions, and the earlier approval was not rewritten ----
    check(first.includes("v2"), "2. the approved enhancement is drawn as a new version", first.slice(0, 500));
    check(
      secondWords && first.includes(secondWords),
      "2. the second version carries its own approval words",
      first.slice(0, 500),
    );
    check(
      /Replaced by .*v2/i.test(first.replace(/\s+/g, " ")),
      "2. the superseded version points forward instead of disappearing",
      first.slice(0, 600),
    );
    const history = await page.evaluate(async (id) => {
      const res = await fetch(`/api/reference-benchmarks/${encodeURIComponent(id)}`);
      return res.ok ? await res.json() : null;
    }, referenceId);
    const versions = history && Array.isArray(history.versions) ? history.versions : [];
    check(
      versions.length === 2 && versions[0].version === 1 && versions[1].version === 2,
      "8. the route keeps an append-only history, oldest first",
      JSON.stringify(versions.map((row) => row.version)),
    );
    check(
      versions.length === 2 && versions[0].approval && versions[0].approval.statement === approvalWords,
      "8. the first version's acceptance is exactly as it was recorded",
      JSON.stringify(versions[0] && versions[0].approval),
    );
    check(
      versions.length === 2 && versions[0].grants_publish_permission === false,
      "8. every stored version reports no publish permission",
      JSON.stringify(versions[0] || {}),
    );

    // ---- 3. a revoked reference keeps its record ----
    check(/revoked-banner/.test(first), "3. a revoked reference is still listed", first.slice(0, 700));
    check(/revoked/i.test(first), "3. its status is drawn as revoked", first.slice(0, 700));

    // ---- 4. another project's reference is not offered ----
    check(!/other-project/i.test(first), "4. a reference approved for another project is not offered here", first.slice(0, 700));

    // ---- 5. what a reference cannot settle, named ----
    for (const phrase of [
      /changed code works/i,
      /changed facts are true/i,
      /security sign off/i,
      /screenshot approval/i,
      /final acceptance/i,
      /publish permission/i,
    ]) {
      check(phrase.test(first), `5. the pane names "${phrase.source.replace(/\\/g, "")}" as unsettled`, first.slice(-400));
    }

    // ---- 6. markup in the record is text on screen ----
    const hostileVisible = await page.evaluate(() => {
      const host = document.getElementById("harnessReferenceBlock");
      if (!host) return null;
      return {
        injected: host.querySelectorAll("script, img, svg, a[href]").length,
        pwned: Boolean(window.pwned),
        text: host.innerText,
      };
    });
    check(Boolean(hostileVisible), "6. the reference pane is present for the hostile-text check");
    check(hostileVisible && hostileVisible.injected === 0, "6. no element from the record reached the DOM", JSON.stringify(hostileVisible && hostileVisible.injected));
    check(hostileVisible && hostileVisible.pwned === false, "6. an injected handler never ran");
    check(
      hostileVisible && hostileText && hostileVisible.text.includes(hostileText.slice(0, 24)),
      "6. the hostile text is drawn as text",
      (hostileVisible && hostileVisible.text || "").slice(0, 300),
    );

    // ---- 7. no invented approver: the pane refuses and sends nothing ----
    const before = posted.length;
    await page.evaluate(() => {
      const approver = document.getElementById("harnessReferenceApprover");
      const statement = document.getElementById("harnessReferenceStatement");
      if (approver) approver.value = "";
      if (statement) statement.value = "a statement typed without naming who approved it";
    });
    await page.evaluate(() => submitHarnessReference(null));
    await sleep(args.settleMs * 6);
    const refusalText = await page.evaluate(() => {
      const note = document.getElementById("harnessReferenceNote");
      return note ? note.innerText : "";
    });
    check(posted.length === before, "7. a reference was not recorded when no approver was named", posted.slice(before).join(", "));
    check(/type your name/i.test(refusalText), "7. the pane says why nothing was recorded", refusalText);
    const stillThere = await page.evaluate(() => Boolean(document.getElementById("harnessReferenceApprover")));
    check(stillThere, "7. the approver field is a real labelled input the owner fills in, not a stored identity");

    // ---- 9. keyboard, narrow viewport, zoom, reload ----
    const tabbed = await page.evaluate(() => {
      const fields = ["harnessReferenceApprover", "harnessReferenceStatement", "harnessReferenceSubmit"]
        .map((id) => document.getElementById(id))
        .filter(Boolean);
      if (fields.length !== 3) return false;
      fields[0].focus();
      return document.activeElement === fields[0] && fields.every((node) => node.tabIndex >= 0 || node.tagName === "BUTTON" || node.tagName === "TEXTAREA" || node.tagName === "INPUT");
    });
    check(tabbed, "9. the approver, the statement and the button are all reachable by keyboard");

    async function reopenAt(width, height, extra) {
      await page.setViewport({ width, height, ...extra });
      await sleep(args.settleMs * 8);
      await openSession(ids.main);
      await openReferencePane();
      return paneVisible();
    }
    check(await reopenAt(390, 844, { isMobile: true, hasTouch: true, deviceScaleFactor: 2 }), "9. the reference pane opens at 390x844");
    const mobile = await page.evaluate(() => {
      const modal = document.getElementById("harnessEvidenceModal");
      const host = document.getElementById("harnessReferenceBlock");
      const field = document.getElementById("harnessReferenceStatement");
      if (!modal || !host || !field) return null;
      const modalBox = modal.querySelector(".modal").getBoundingClientRect();
      const fieldBox = field.getBoundingClientRect();
      return {
        overflow: Math.max(0, Math.round(host.scrollWidth - host.clientWidth)),
        fieldVisible: fieldBox.width > 0 && fieldBox.height > 0,
        modalFits: modalBox.right <= window.innerWidth + 1 && modalBox.left >= -1,
        documentOverflow: Math.max(0, Math.round(document.documentElement.scrollWidth - window.innerWidth)),
      };
    });
    check(Boolean(mobile) && mobile.fieldVisible, "9. the approval form stays usable at 390x844", JSON.stringify(mobile));
    check(Boolean(mobile) && mobile.modalFits, "9. the modal stays inside a 390 px viewport", JSON.stringify(mobile));
    check(Boolean(mobile) && mobile.documentOverflow === 0, "9. the page does not scroll sideways at 390x844", JSON.stringify(mobile));
    if (args.screenshots) {
      await page.evaluate(() => {
        const host = document.getElementById("harnessReferenceBlock");
        if (host) host.scrollIntoView({ block: "center" });
      });
      await sleep(args.settleMs * 4);
      await capture("reference-mobile.png", "reference pane at 390x844", { folder: "390x844-light", width: 390, height: 844, device_scale_factor: 2, theme: "light" });
    }

    check(await reopenAt(720, 480, { deviceScaleFactor: 2 }), "9. the reference pane opens at 200% zoom");
    const zoomed = await page.evaluate(() => {
      const host = document.getElementById("harnessReferenceBlock");
      const field = document.getElementById("harnessReferenceStatement");
      if (!host || !field) return null;
      const box = field.getBoundingClientRect();
      return {
        overflow: Math.max(0, Math.round(host.scrollWidth - host.clientWidth)),
        fieldVisible: box.width > 0 && box.height > 0,
        documentOverflow: Math.max(0, Math.round(document.documentElement.scrollWidth - window.innerWidth)),
      };
    });
    check(Boolean(zoomed) && zoomed.fieldVisible, "9. the approval form remains reachable at 200% zoom", JSON.stringify(zoomed));
    check(Boolean(zoomed) && zoomed.documentOverflow === 0, "9. nothing overflows the page at 200% zoom", JSON.stringify(zoomed));

    await page.setViewport({ width: 1440, height: 960 });
    await sleep(args.settleMs * 4);
    if (args.screenshots) {
      await openSession(ids.main);
      await openReferencePane();
      // The pane sits below the evidence sections, so the capture scrolls it into view. A
      // screenshot of the top of the dialog would be a review of the sections above it.
      await page.evaluate(() => {
        const host = document.getElementById("harnessReferenceBlock");
        if (host) host.scrollIntoView({ block: "center" });
      });
      await sleep(args.settleMs * 4);
      await capture("reference-pane.png", "approved, superseded, revoked and hostile references", { folder: "1440x960-light", width: 1440, height: 960, device_scale_factor: 1, theme: "light" });
    }

    // A reload must rebuild the pane from the record, not from pane state.
    await page.reload({ waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 8);
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);
    await openSession(ids.main);
    await openReferencePane();
    const reloaded = await paneText();
    check(approvalWords && reloaded.includes(approvalWords), "9. a reload restores the same records from the server", reloaded.slice(0, 300));
    check(/What a reference never settles/i.test(reloaded), "9. the limit is still stated after a reload");

    // ---- 10. no page error, and nothing outside the reference routes was written ----
    const strayWrites = posted.filter((line) => !/\/api\/reference-benchmarks/.test(line));
    check(pageErrors.length === 0, "10. no page error occurred", pageErrors.join(" | ").slice(0, 300));
    check(strayWrites.length === 0, "10. every write went to a reference route", strayWrites.join(", ").slice(0, 300));
  } finally {
    await browser.close();
    if (seed && seed.child) seed.child.kill("SIGTERM");
  }

  if (args.screenshots && capturedShots.length) {
    const capturedAt = new Date().toISOString();
    for (const folder of new Set(capturedShots.map((shot) => shot.folder))) {
      const mine = capturedShots.filter((shot) => shot.folder === folder);
      const index = {
        schema: 1,
        captured_at: capturedAt,
        viewport: mine[0].viewport,
        variant: { theme: mine[0].theme, interaction: "reference pane in the Verification & artifacts dialog" },
        checks: { total: checks.length, passed: checks.filter((row) => row.ok).length },
        shots: mine,
      };
      writeFileSync(join(resolve(args.screenshots, folder), "index.json"), `${JSON.stringify(index, null, 2)}\n`, "utf-8");
    }
  }
  const passed = checks.filter((row) => row.ok).length;
  const payload = { story: "AP-020", checks: checks.length, passed, failed: checks.length - passed, results: checks };
  if (args.json) console.log(JSON.stringify(payload));
  if (!checks.length || passed !== checks.length) {
    console.error(`harness reference walk: ${passed}/${checks.length} checks passed`);
    process.exit(1);
  }
  console.log(`harness reference walk: ${passed}/${checks.length} checks passed`);
}

main().catch((error) => {
  console.error(`harness reference walk: ${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
