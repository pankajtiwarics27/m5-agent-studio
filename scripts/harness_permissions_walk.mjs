#!/usr/bin/env node
/**
 * Check the Harness's approval safety and permission UX in a browser (UIUX-P3-03).
 *
 *   node scripts/harness_permissions_walk.mjs --screenshots docs/media/harness-permissions
 *
 * No route mints a pending approval, so this walk starts its own fixture: the same isolated
 * demo instance the other walks use, with two live gates and one expired offer created by the
 * app's own `_create_pending_tool_approval()`. That is what a turn calls at the boundary, and
 * the demo has no model runtime, so nothing here is a card the walk drew itself.
 *
 * What it proves:
 *
 *   1. an approval arrives as one card that states the tool, the exact arguments, the
 *      capability and the resource the decision records, the policy asking, the one-use
 *      limit and the minute it expires;
 *   2. the card counts down, and says what approving does - and what refusing does instead;
 *   3. the decision is posted with the session that owns the approval;
 *   4. refusing records a refusal and no permission;
 *   5. approving records one permission, and the badge says so;
 *   6. the permissions surface lists what is held, with what it covers and who issued it;
 *   7. revoking takes two steps, names the denial rule it creates, and then the surface
 *      shows the rule and the grant's history;
 *   8. an offer nobody answered reads as expired, with what to do instead;
 *   9. no page error occurs during the walk.
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
  sessionId: "",
  approveApprovalId: "",
  rejectApprovalId: "",
  expiredApprovalId: "",
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
      case "--session-id": args.sessionId = next(); break;
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
    "Check the Harness's approval card and its permissions surface.",
    "",
    "  node scripts/harness_permissions_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/harness_permissions_walk.mjs --base-url URL --token TOKEN --session-id ID \\",
    "       (against a server started by scripts/harness_permissions_seed.py)",
    "",
    "Without --base-url the walk starts the seed fixture itself, so the approvals it reads are",
    "the server's own. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-harness-permissions.md.",
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
    [resolve("scripts/harness_permissions_seed.py"), "--port", String(args.port)],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The seed fixture did not report a session in time.\n${stderr}`)),
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

async function readCard(page, approvalId) {
  return page.evaluate((id) => {
    const card = document.getElementById(`apr-card-${id}`);
    if (!card) return null;
    const facts = {};
    card.querySelectorAll(".h-approval-facts dt").forEach((dt) => {
      const dd = dt.nextElementSibling;
      facts[dt.textContent.trim()] = dd ? dd.textContent.trim() : "";
    });
    return {
      live: card.dataset.live,
      text: card.textContent.replace(/\s+/g, " ").trim(),
      head: (card.querySelector(".h-approval-head") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
      args: (card.querySelector(".h-approval-args") || {}).textContent || "",
      consequence: (card.querySelector(".h-approval-consequence") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
      countdown: document.querySelector(`#apr-card-${id} .h-approval-countdown`)?.textContent?.trim() || "",
      facts,
      buttons: [...card.querySelectorAll("button")].map((b) => b.textContent.replace(/\s+/g, " ").trim()),
      policy: (document.getElementById("harnessPolicy") || {}).value || "",
      policyLabel: (() => {
        const select = document.getElementById("harnessPolicy");
        if (!select) return "";
        const option = [...select.options].find((o) => o.value === select.value);
        return option ? option.textContent.trim() : "";
      })(),
    };
  }, approvalId);
}

async function readSurface(page) {
  return page.evaluate(() => {
    const host = document.getElementById("harnessGrantsModal");
    const badge = document.getElementById("harnessPermissionBadge");
    const text = (id) => (document.getElementById(id)?.textContent || "").replace(/\s+/g, " ").trim();
    return {
      open: Boolean(host) && getComputedStyle(host).display !== "none",
      badge: {
        text: badge ? badge.textContent.trim() : "",
        count: badge ? badge.dataset.count : "",
        drawn: Boolean(badge) && badge.getBoundingClientRect().height > 0,
      },
      meta: text("harnessGrantsMeta"),
      summary: text("harnessGrantsSummary"),
      active: [...document.querySelectorAll("#harnessGrantsActive .perm-card")].map((card) => ({
        id: card.dataset.grantId || "",
        status: card.dataset.status || "",
        text: card.textContent.replace(/\s+/g, " ").trim(),
      })),
      activeEmpty: text("harnessGrantsActive"),
      rules: [...document.querySelectorAll("#harnessGrantsRules .perm-card")].map((card) => card.textContent.replace(/\s+/g, " ").trim()),
      history: [...document.querySelectorAll("#harnessGrantsHistory .perm-card")].map((card) => card.textContent.replace(/\s+/g, " ").trim()),
      approvals: [...document.querySelectorAll("#harnessGrantsApprovals .perm-card")].map((card) => card.textContent.replace(/\s+/g, " ").trim()),
      confirm: document.querySelector("#harnessGrantsActive .perm-confirm")?.textContent.replace(/\s+/g, " ").trim() || "",
    };
  });
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
      args.sessionId = args.sessionId || fixture.ready.session_id;
      args.approveApprovalId = fixture.ready.approve_approval_id;
      args.rejectApprovalId = fixture.ready.reject_approval_id;
      args.expiredApprovalId = fixture.ready.expired_approval_id;
    }
    if (!args.sessionId || !args.approveApprovalId || !args.rejectApprovalId) {
      throw new Error(
        "--base-url mode needs --session-id, and the approvals seeded in that session",
      );
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
      const approvalPosts = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("request", (request) => {
        if (request.method() === "POST" && request.url().endsWith("/api/harness/approve")) {
          try {
            approvalPosts.push(JSON.parse(request.postData() || "{}"));
          } catch (_error) {
            approvalPosts.push({ unreadable: true });
          }
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
      await page.evaluate((id) => selectHarnessSession(id), args.sessionId);
      await sleep(args.settleMs * 12);

      // ── The card states the permission before the click ──────────────────
      const card = await readCard(page, args.approveApprovalId);
      record(
        "an approval arrives as one card that names the tool and shows the arguments",
        Boolean(card)
          && card.live === "true"
          && /Approval required: run_bash/.test(card.head)
          && /pytest -q tests\/test_store\.py/.test(card.args),
        card ? `head="${card.head}"` : "no card",
      );
      record(
        "the card names the policy that is asking, and it is the policy in force",
        Boolean(card) && card.head.includes(card.policyLabel) && card.policy === "standard"
          && !card.head.includes("Standard Policy"),
        card ? `policy=${card.policy} label="${card.policyLabel}" head="${card.head}"` : "",
      );
      record(
        "the card states the capability, the resource and the one-use limit the approval records",
        Boolean(card)
          && /code_execution/.test(card.facts.Capability || "")
          && /worktree/.test(card.facts.Resource || "")
          && /one use/.test(card.facts.Limit || "")
          && /10 minutes/.test(card.facts.Limit || ""),
        card ? JSON.stringify(card.facts) : "",
      );
      record(
        "the card says what approving does, what it does not do, and that the turn is waiting",
        Boolean(card)
          && /records a permission for this exact call/.test(card.consequence)
          && /It is not a standing allow for run_bash/.test(card.consequence)
          && /the turn waits/.test(card.consequence)
          && /Rejecting refuses this call/.test(card.consequence),
        card ? card.consequence.slice(0, 160) : "",
      );
      record(
        "the card counts down to the minute the offer expires",
        Boolean(card) && /^expires in (\d+ min|\d+ s)$/.test(card.countdown),
        card ? card.countdown : "",
      );
      record(
        "the card offers exactly the two decisions, in words that say what they do",
        Boolean(card)
          && card.buttons.some((b) => /Approve & run once/.test(b))
          && card.buttons.some((b) => b === "Reject"),
        card ? JSON.stringify(card.buttons) : "",
      );
      await shoot(page, "approval-card");

      // ── An offer nobody answered reads as expired ────────────────────────
      const expired = await readCard(page, args.expiredApprovalId);
      record(
        "an offer nobody answered is drawn as expired, with what to do instead",
        Boolean(expired)
          && expired.live === "false"
          && /Expired — re-run the turn/.test(expired.text)
          && /Re-run the turn to reach the gate again/.test(expired.consequence)
          && !(expired.buttons || []).some((b) => /Approve/.test(b)),
        expired ? expired.consequence.slice(0, 140) : "no expired card",
      );
      await page.evaluate((id) => {
        const card = document.getElementById(`apr-card-${id}`);
        if (card) card.scrollIntoView({ block: "center" });
      }, args.expiredApprovalId);
      await sleep(args.settleMs * 2);
      await shoot(page, "expired");

      // ── Before any decision: nothing is held, and what is waiting is listed ─
      const before = await readSurface(page);
      record(
        "a session that holds nothing says so in the badge",
        before.badge.count === "0" && /No permissions/.test(before.badge.text) && before.badge.drawn === true,
        `badge="${before.badge.text}" count=${before.badge.count} drawn=${before.badge.drawn}`,
      );
      await page.evaluate(() => openHarnessGrants());
      await sleep(args.settleMs * 8);
      const waiting = await readSurface(page);
      record(
        "the surface lists the approvals still waiting and the offer that expired",
        waiting.open === true
          && waiting.active.length === 0
          && waiting.approvals.filter((row) => /waiting for you/.test(row)).length === 2
          && waiting.approvals.filter((row) => /expired/.test(row)).length === 1,
        JSON.stringify(waiting.approvals.map((row) => row.slice(0, 48))),
      );
      record(
        "the surface says which policy and revision govern what it shows",
        /Standard/.test(waiting.meta) && /revision 1/.test(waiting.meta),
        waiting.meta,
      );
      await page.evaluate(() => closeHarnessGrants());
      await sleep(args.settleMs * 4);

      await page.evaluate((id) => {
        const card = document.getElementById(`apr-card-${id}`);
        const buttons = [...card.querySelectorAll("button")];
        const reject = buttons.find((b) => b.textContent.trim() === "Reject");
        reject.click();
      }, args.rejectApprovalId);
      await sleep(args.settleMs * 12);
      const refused = await readCard(page, args.rejectApprovalId);
      const afterRefusal = await readSurface(page);
      record(
        "refusing redraws the card as a refusal that says nothing was granted",
        Boolean(refused)
          && /Rejected/.test(refused.text)
          && /You refused this call, so it did not run and no permission was recorded\./.test(refused.consequence),
        refused ? refused.consequence : "no card",
      );
      record(
        "a refusal records no permission",
        afterRefusal.badge.count === "0" && /No permissions/.test(afterRefusal.badge.text),
        `badge="${afterRefusal.badge.text}"`,
      );
      const refusalPost = approvalPosts.find((post) => post.approval_id === args.rejectApprovalId);
      record(
        "the decision is posted with the session that owns the approval",
        Boolean(refusalPost) && refusalPost.session_id === args.sessionId && refusalPost.decision === "reject",
        refusalPost ? JSON.stringify(refusalPost) : "no approval request captured",
      );

      // ── Approving records one permission ─────────────────────────────────
      await page.evaluate((id) => {
        const card = document.getElementById(`apr-card-${id}`);
        const buttons = [...card.querySelectorAll("button")];
        buttons.find((b) => /Approve/.test(b.textContent)).click();
      }, args.approveApprovalId);
      await sleep(args.settleMs * 16);
      const approved = await readCard(page, args.approveApprovalId);
      record(
        "approving redraws the card as a decision that says the permission was spent",
        Boolean(approved)
          && approved.live === "false"
          && /Approved/.test(approved.text)
          && /one-use permission that has now been used/.test(approved.consequence),
        approved ? approved.consequence : "no card",
      );
      const afterApprove = await readSurface(page);
      record(
        "the badge the pane shows is the permission the approval recorded",
        afterApprove.badge.count === "1" && /1 permission/.test(afterApprove.badge.text),
        `badge="${afterApprove.badge.text}" count=${afterApprove.badge.count}`,
      );

      // ── The surface lists what is held ───────────────────────────────────
      await page.evaluate(() => openHarnessGrants());
      await sleep(args.settleMs * 8);
      const surface = await readSurface(page);
      const held = surface.active[0] || { text: "" };
      record(
        "the permissions surface opens from the badge and lists the grant the approval recorded",
        surface.open === true
          && surface.active.length === 1
          && held.status === "active"
          && held.text.includes(args.approveApprovalId),
        `open=${surface.open} rows=${surface.active.length} text="${held.text.slice(0, 120)}"`,
      );
      record(
        "the row states what the permission covers, how long it lasts and who issued it",
        /code_execution/.test(held.text)
          && /workspace/.test(held.text)
          && /one use/.test(held.text)
          && /operator_approval/.test(held.text),
        held.text.slice(0, 200),
      );
      record(
        "the surface still lists the offer that expired once a permission is held",
        surface.approvals.some((row) => /expired/.test(row)),
        JSON.stringify(surface.approvals.map((row) => row.slice(0, 48))),
      );
      await shoot(page, "permissions-open");

      // ── Revoking takes two steps and names the rule ──────────────────────
      await page.evaluate(() => {
        const card = document.querySelector("#harnessGrantsActive .perm-card");
        const button = [...card.querySelectorAll("button")].find((b) => /Revoke/.test(b.textContent));
        button.click();
      });
      await sleep(args.settleMs * 4);
      const confirming = await readSurface(page);
      record(
        "revoking asks first, in a sentence that names the denial rule and its reach",
        /a revocation rule refuses/.test(confirming.confirm)
          && /code_execution/.test(confirming.confirm)
          && /Neither can be undone from here/.test(confirming.confirm)
          && /stopped too/.test(confirming.confirm),
        confirming.confirm.slice(0, 220),
      );
      record(
        "the confirmation can be dismissed without revoking",
        /Keep it/.test(confirming.confirm) && confirming.active.length === 1,
        `rows=${confirming.active.length}`,
      );
      await page.evaluate(() => {
        const card = document.querySelector("#harnessGrantsActive .perm-card");
        [...card.querySelectorAll("button")].find((b) => /Revoke and block/.test(b.textContent)).click();
      });
      await sleep(args.settleMs * 20);
      const revoked = await readSurface(page);
      record(
        "revoking empties the active list and records the rule that refuses the capability",
        revoked.active.length === 0
          && revoked.rules.some((row) => /code_execution/.test(row))
          && revoked.history.some((row) => /revoked/.test(row)),
        `active=${revoked.active.length} rules=${revoked.rules.length} history=${revoked.history.length}`,
      );
      record(
        "the badge drops back to no permissions once the permission is revoked",
        revoked.badge.count === "0" && /No permissions/.test(revoked.badge.text),
        `badge="${revoked.badge.text}" count=${revoked.badge.count}`,
      );
      await shoot(page, "revoked");
      await page.evaluate(() => closeHarnessGrants());
      await sleep(args.settleMs * 2);

      // ── The trajectory records both decisions ────────────────────────────
      const trajectory = await page.evaluate(() => ({
        types: [...document.querySelectorAll("#harnessStream .h-event")].map((card) => card.dataset.type),
        revokedEvents: [...document.querySelectorAll("#harnessStream .h-recorded-type")]
          .map((node) => node.textContent.trim())
          .filter((text) => /security\/grant_revoked/.test(text)),
      }));
      record(
        "the trajectory draws the decisions and the revocation the server appended",
        trajectory.types.includes("approval/request")
          && trajectory.types.includes("approval/decision")
          && trajectory.revokedEvents.length > 0,
        `types=${JSON.stringify([...new Set(trajectory.types)])} revoked=${trajectory.revokedEvents.length}`,
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
