#!/usr/bin/env node
/**
 * Check what a Harness completion claim rests on, in a browser (UIUX-P3-04).
 *
 *   node scripts/harness_evidence_walk.mjs --screenshots docs/media/harness-evidence
 *
 * A verdict is not a card a walk can draw: `turn/verified` and `turn/verification_failed` carry
 * a `TaskVerificationReport`, and only `TaskStateMachine.verify_task_completion()` produces one.
 * The demo has no model runtime, so this walk starts `scripts/harness_evidence_seed.py`, which
 * builds two git workspaces and lets that verifier decide - once on a workspace whose test
 * passes, once on one whose test fails - then appends the reports with the writer a turn uses.
 *
 * What it proves:
 *
 *   1. a verdict is drawn as a card that states the contract digest, the exit code, the
 *      revision, the modified files and the patch - not as a one-line label;
 *   2. a rejected verdict is drawn as a rejection, with the failure code, the classification
 *      and the failing test ids it was decided on;
 *   3. the card opens the record: the contract the session set, the observation the runtime
 *      recorded, and the change the claim is attached to;
 *   4. the change is counted per file from the same diff the diff dialog draws;
 *   5. the artifacts a turn left behind - its checkpoint and the workspace revision a re-run
 *      would be tied to - are listed;
 *   6. a session with a contract and no verdict says so instead of implying a pass;
 *   7. the surface is reachable from the session actions and from the keyboard;
 *   8. no page error occurs during the walk.
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
  verifiedSessionId: "",
  rejectedSessionId: "",
  pendingSessionId: "",
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
      case "--verified-session-id": args.verifiedSessionId = next(); break;
      case "--rejected-session-id": args.rejectedSessionId = next(); break;
      case "--pending-session-id": args.pendingSessionId = next(); break;
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
    "Check what a Harness completion claim rests on.",
    "",
    "  node scripts/harness_evidence_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/harness_evidence_walk.mjs --base-url URL --token TOKEN \\",
    "       --verified-session-id ID --rejected-session-id ID --pending-session-id ID",
    "",
    "Without --base-url the walk starts the seed fixture itself, so the verdicts it reads are",
    "the server's own. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-harness-evidence.md.",
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
    [resolve("scripts/harness_evidence_seed.py"), "--port", String(args.port)],
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

async function readCard(page, tone) {
  return page.evaluate((wanted) => {
    const card = document.querySelector(`#harnessStream .h-verdict[data-verdict="${wanted}"]`);
    if (!card) return null;
    const facts = {};
    card.querySelectorAll(".ev-facts dt").forEach((dt) => {
      const dd = dt.nextElementSibling;
      facts[dt.textContent.trim()] = dd ? dd.textContent.trim() : "";
    });
    return {
      tone: card.dataset.verdict,
      text: card.textContent.replace(/\s+/g, " ").trim(),
      badge: (card.querySelector(".h-verdict-badge") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
      claim: (card.querySelector(".h-verdict-claim") || {}).textContent?.trim() || "",
      detail: (card.querySelector(".h-verdict-detail") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
      facts,
      failures: [...card.querySelectorAll(".ev-failure")].map((row) => ({
        kind: row.dataset.kind,
        text: row.textContent.replace(/\s+/g, " ").trim(),
      })),
      coverage: [...card.querySelectorAll(".ev-cover")].map((row) => ({
        covered: row.dataset.covered,
        text: row.textContent.replace(/\s+/g, " ").trim(),
      })),
      hasOutput: Boolean(card.querySelector(".h-verdict-output")),
      buttons: [...card.querySelectorAll("button")].map((button) => button.textContent.replace(/\s+/g, " ").trim()),
      oneLineLabel: /Completion verified \(/.test(card.textContent),
    };
  }, tone);
}

async function readSurface(page) {
  return page.evaluate(() => {
    const host = document.getElementById("harnessEvidenceModal");
    const text = (id) => (document.getElementById(id)?.textContent || "").replace(/\s+/g, " ").trim();
    return {
      open: Boolean(host) && getComputedStyle(host).display !== "none",
      meta: text("harnessEvidenceMeta"),
      verdict: text("harnessEvidenceVerdict"),
      spec: text("harnessEvidenceSpec"),
      observed: text("harnessEvidenceObserved"),
      change: text("harnessEvidenceChange"),
      artifacts: text("harnessEvidenceArtifacts"),
      limits: [...document.querySelectorAll("#harnessEvidenceLimits .ev-limits li")].map((row) => row.textContent.trim()),
      verdictFacts: (() => {
        const facts = {};
        document.querySelectorAll("#harnessEvidenceVerdict .ev-facts dt").forEach((dt) => {
          const dd = dt.nextElementSibling;
          facts[dt.textContent.trim()] = dd ? dd.textContent.trim() : "";
        });
        return facts;
      })(),
      files: [...document.querySelectorAll("#harnessEvidenceChange .ev-file")].map((row) => ({
        path: (row.querySelector(".ev-file-path") || {}).textContent || "",
        added: (row.querySelector(".ev-file-add") || {}).textContent || "",
        deleted: (row.querySelector(".ev-file-del") || {}).textContent || "",
      })),
      coverage: [...document.querySelectorAll("#harnessEvidenceObserved .ev-cover, #harnessEvidenceSpec .ev-cover")].map((row) => ({
        covered: row.dataset.covered,
        text: row.textContent.replace(/\s+/g, " ").trim(),
      })),
      verdictTone: document.querySelector("#harnessEvidenceVerdict .ev-verdict")?.dataset.verdict || "",
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
      args.verifiedSessionId = args.verifiedSessionId || fixture.ready.verified_session_id;
      args.rejectedSessionId = args.rejectedSessionId || fixture.ready.rejected_session_id;
      args.pendingSessionId = args.pendingSessionId || fixture.ready.pending_session_id;
    }
    if (!args.verifiedSessionId || !args.rejectedSessionId || !args.pendingSessionId) {
      throw new Error("--base-url mode needs the three seeded session ids");
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
      await page.evaluate((id) => selectHarnessSession(id), args.verifiedSessionId);
      await sleep(args.settleMs * 12);

      // ── A verdict is a card that states what it rests on ─────────────────
      const verified = await readCard(page, "verified");
      record(
        "a verified completion is drawn as a verdict card, not a one-line label",
        Boolean(verified) && verified.oneLineLabel === false && /Completion verified/.test(verified.badge),
        verified ? `badge="${verified.badge}"` : "no card",
      );
      record(
        "the card states the contract digest, the exit code and the classification",
        Boolean(verified)
          && /^spec [0-9a-f]{12}/.test(verified.facts.Contract || "")
          && /exit 0/.test(verified.facts.Command || "")
          && /passed/.test(verified.facts.Command || ""),
        verified ? JSON.stringify({ Contract: verified.facts.Contract, Command: verified.facts.Command }) : "",
      );
      record(
        "the card states the revision and the environment the command ran in",
        Boolean(verified)
          && /\b[0-9a-f]{10}\b/.test(verified.facts.Revision || "")
          && /python 3\./.test(verified.facts.Environment || ""),
        verified ? `${verified.facts.Revision} · ${verified.facts.Environment}` : "",
      );
      record(
        "the card names the modified files and the patch digest",
        Boolean(verified)
          && /src\/total\.py/.test(verified.facts.Files || "")
          && /sha256 [0-9a-f]{12}/.test(verified.facts.Patch || ""),
        verified ? `${verified.facts.Files} · ${verified.facts.Patch}` : "",
      );
      record(
        "the card shows the acceptance criterion the transcript covered",
        Boolean(verified)
          && verified.coverage.length === 1
          && verified.coverage[0].covered === "true"
          && /total\(\) sums a list/.test(verified.coverage[0].text),
        verified ? JSON.stringify(verified.coverage) : "",
      );
      record(
        "the card keeps the verification output behind a disclosure",
        Boolean(verified) && verified.hasOutput === true,
        verified ? `output=${verified.hasOutput}` : "",
      );
      record(
        "the card offers the record and the diff, and says it re-runs nothing",
        Boolean(verified)
          && verified.buttons.some((label) => /Verification & artifacts/.test(label))
          && verified.buttons.some((label) => /Inspect the diff/.test(label))
          && /re-runs nothing/.test(verified.text),
        verified ? verified.buttons.join(" | ") : "",
      );
      const oneLineGone = await page.evaluate(() => !/Completion verified \(/.test(document.getElementById("harnessStream").textContent));
      record("the old one-line verdict label is gone from the trajectory", oneLineGone === true, "");
      await shoot(page, "verdict-card");

      // ── The record behind it ─────────────────────────────────────────────
      await page.evaluate(() => {
        const card = document.querySelector('#harnessStream .h-verdict[data-verdict="verified"]');
        [...card.querySelectorAll("button")].find((button) => /Verification & artifacts/.test(button.textContent)).click();
      });
      await sleep(args.settleMs * 16);
      const surface = await readSurface(page);
      record("the card opens the verification surface", surface.open === true, `open=${surface.open}`);
      record(
        "the surface names the session, the verdict and the branch",
        /hns-/.test(surface.meta) && /Completion verified/.test(surface.meta) && /branch/.test(surface.meta),
        surface.meta.slice(0, 160),
      );
      record(
        "the verdict section states the exit code and the classification",
        surface.verdictFacts.Command === "exit 0 · passed" && /passed/.test(surface.observed),
        JSON.stringify(surface.verdictFacts),
      );
      record(
        "the contract section states the command, the required file and the rules",
        /Test command/.test(surface.spec)
          && /pytest -v tests/.test(surface.spec)
          && /src\/total\.py/.test(surface.spec)
          && /only a clean run verifies/.test(surface.spec),
        surface.spec.slice(0, 200),
      );
      record(
        "the contract section lists the acceptance criterion it declares",
        /total\(\) sums a list/.test(surface.spec),
        surface.spec.slice(0, 120),
      );
      record(
        "the observation section records the coverage, not the claim",
        /Acceptance coverage/.test(surface.observed) && surface.coverage.some((row) => row.covered === "true"),
        surface.observed.slice(0, 160),
      );
      const serverDiff = await page.evaluate(async (id) => {
        const response = await fetch(`/api/harness/sessions/${encodeURIComponent(id)}/diff`, {
          headers: { "X-API-Token": window.sessionStorage.getItem("m5_api_token") || "" },
        });
        return response.ok ? await response.json() : null;
      }, args.verifiedSessionId);
      const serverText = String(serverDiff?.diff ?? "");
      const expectedFiles = serverText.split("\n").filter((line) => line.startsWith("diff --git ")).length;
      record(
        "the change is counted per file from the same diff the server reports",
        surface.files.length > 0
          && surface.files.length === Math.max(expectedFiles, surface.files.length)
          && surface.files.some((file) => /src\/total\.py/.test(file.path) && file.added !== "" && file.deleted !== ""),
        `files=${surface.files.length} server=${expectedFiles}`,
      );
      record(
        "the change section says the diff is read now, not frozen at the verdict",
        /not a copy frozen at the verdict/.test(surface.change),
        surface.change.slice(-160),
      );
      record(
        "the artifacts section lists the checkpoint and the revision a re-run would be tied to",
        /Checkpoints/.test(surface.artifacts) && /seq 1/.test(surface.artifacts) && /Workspace now/.test(surface.artifacts),
        surface.artifacts.slice(0, 200),
      );
      record(
        "the surface states what it does not prove",
        surface.limits.length === 4 && surface.limits.some((line) => /does not re-run/.test(line)),
        `${surface.limits.length} limit(s)`,
      );
      await shoot(page, "verdict-record");

      // ── A rejection is drawn as one ──────────────────────────────────────
      await page.evaluate(() => closeHarnessEvidenceModal());
      await sleep(args.settleMs * 2);
      await page.evaluate((id) => selectHarnessSession(id), args.rejectedSessionId);
      await sleep(args.settleMs * 12);
      const rejected = await readCard(page, "rejected");
      record(
        "a rejected completion is drawn as a rejection with the code that decided it",
        Boolean(rejected)
          && /Completion rejected/.test(rejected.badge)
          && /verification_test_failed_introduced/.test(rejected.detail),
        rejected ? rejected.detail : "no card",
      );
      record(
        "the rejected card states the failed run's exit code and the failing test",
        Boolean(rejected)
          && /exit 1/.test(rejected.facts.Command || "")
          && /introduced/.test(rejected.facts.Command || "")
          && rejected.failures.some((row) => /test_total_sums_a_list/.test(row.text) && row.kind === "introduced"),
        rejected ? JSON.stringify({ command: rejected.facts.Command, failures: rejected.failures }) : "",
      );
      await shoot(page, "rejected-card");
      await page.evaluate(() => openHarnessEvidenceModal());
      await sleep(args.settleMs * 16);
      const rejectedSurface = await readSurface(page);
      record(
        "the rejected record says which criterion was not covered",
        /not covered/.test(rejectedSurface.observed) || /not covered/.test(rejectedSurface.spec),
        rejectedSurface.observed.slice(0, 160),
      );
      await shoot(page, "rejected-record");
      await page.evaluate(() => closeHarnessEvidenceModal());
      await sleep(args.settleMs * 2);

      // ── An absent verdict is drawn as an absence ─────────────────────────
      await page.evaluate((id) => selectHarnessSession(id), args.pendingSessionId);
      await sleep(args.settleMs * 12);
      record(
        "a session with no verdict draws no verdict card",
        await page.evaluate(() => document.querySelectorAll("#harnessStream .h-verdict").length === 0),
        "",
      );
      await page.evaluate(() => openHarnessEvidenceModal());
      await sleep(args.settleMs * 16);
      const pendingSurface = await readSurface(page);
      record(
        "the surface says no verdict is recorded, and still shows the contract",
        /No verdict is recorded/.test(pendingSurface.verdict)
          && /no verdict recorded/.test(pendingSurface.meta)
          && /Test command/.test(pendingSurface.spec),
        pendingSurface.verdict.slice(0, 120),
      );
      record(
        "an absent verdict is never drawn as a pass",
        pendingSurface.verdictTone === "" && !/Completion verified/.test(pendingSurface.verdict),
        `tone="${pendingSurface.verdictTone}"`,
      );
      await shoot(page, "no-verdict");
      await page.evaluate(() => closeHarnessEvidenceModal());
      await sleep(args.settleMs * 2);

      // ── Reachable from the session actions, and from the keyboard ────────
      await page.evaluate((id) => selectHarnessSession(id), args.verifiedSessionId);
      await sleep(args.settleMs * 10);
      await page.evaluate(() => {
        const select = document.getElementById("harnessSessionActionSelect");
        select.value = "evidence";
        select.dispatchEvent(new Event("change"));
      });
      await sleep(args.settleMs * 16);
      record(
        "the session actions open the same surface",
        (await readSurface(page)).open === true,
        "",
      );
      await page.evaluate(() => closeHarnessEvidenceModal());
      await sleep(args.settleMs * 2);
      await page.evaluate(() => {
        const card = document.querySelector('#harnessStream .h-verdict[data-verdict="verified"]');
        [...card.querySelectorAll("button")].find((button) => /Verification & artifacts/.test(button.textContent)).focus();
      });
      await page.keyboard.press("Enter");
      await sleep(args.settleMs * 16);
      const keyboard = await readSurface(page);
      const focusInside = await page.evaluate(() => {
        const dialog = document.querySelector('#harnessEvidenceModal [role="dialog"]');
        return Boolean(dialog) && dialog.contains(document.activeElement);
      });
      record(
        "the record opens from the keyboard, with focus inside the dialog",
        keyboard.open === true && focusInside === true,
        `open=${keyboard.open} focusInside=${focusInside}`,
      );
      await page.keyboard.press("Escape");
      await sleep(args.settleMs * 3);
      record(
        "Escape closes it and returns focus to the card's control",
        await page.evaluate(() => {
          const host = document.getElementById("harnessEvidenceModal");
          const closed = getComputedStyle(host).display === "none";
          const active = document.activeElement;
          return closed && Boolean(active) && /Verification & artifacts/.test(active.textContent || "");
        }),
        "",
      );

      // ── The diff dialog says which change it is ──────────────────────────
      await page.evaluate(() => openHarnessDiffModal());
      await sleep(args.settleMs * 14);
      const diffDialog = await page.evaluate(() => ({
        summary: (document.getElementById("harnessGitDiffSummary")?.textContent || "").trim(),
        note: (document.getElementById("harnessDiffModal")?.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      record(
        "the diff dialog counts the change and says it is not frozen at a verdict",
        /file\(s\) · \+/.test(diffDialog.summary) && /not frozen at any verdict/.test(diffDialog.note),
        diffDialog.summary,
      );
      await page.evaluate(() => closeHarnessDiffModal());
      await sleep(args.settleMs * 2);

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
