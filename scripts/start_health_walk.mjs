#!/usr/bin/env node
/**
 * Check Start's system health and workspace selection in a browser (UIUX-P2-03).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/start_health_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The card's whole claim is "these numbers came from somewhere you can check", so the
 * walk checks them against the payload rather than against the card's own wording: it
 * reads /api/telemetry itself and holds every row against it, asserts the basis matches
 * what the host actually produced (a demo tour has no model runtime, so it produces the
 * simulated payload - the walk asserts the card says so instead of pretending), then
 * changes the workspace through the pane and proves the change reached the Harness's
 * field and the preflight.
 *
 * What it proves:
 *
 *   1. four rows, each with a label, a value from the payload and a tone from the shared
 *      vocabulary - nothing invented, nothing hard-coded;
 *   2. the memory, container and runtime rows agree with /api/telemetry the walk read;
 *   3. the basis is honest for the host it ran on: `measured` with a live payload,
 *      `unverified` plus the "Demo data" pill and a claim naming the rows that are still
 *      real when the payload is simulated;
 *   4. the workspace row shows the configured directory and its git state, and says
 *      whether the directory was chosen here or is the server's default;
 *   5. setting a directory updates the row, the wait - the Harness's own input - and the
 *      preflight, which is re-read rather than left to contradict the pane;
 *   6. restoring the original directory puts all three back;
 *   7. a refused write is presented with its cause and which directory is still in
 *      force, and Retry performs the write once the route answers;
 *   8. a refused telemetry read drops the card to `unavailable` - rows say "not measured
 *      yet" rather than keeping a payload the pane can no longer see - and the workspace
 *      row survives, because it never came from telemetry;
 *   9. no page errors, and the two cards survive a 900px viewport.
 */
import { existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = { tokenKey: "m5_api_token", settleMs: 250, timeoutMs: 45000, scratch: "" };
const TONES = new Set(["ok", "warn", "danger", "info", "busy", "neutral"]);

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
      case "--scratch-dir": args.scratch = next(); break;
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
    "Check Start's system health and workspace selection against the server's payloads.",
    "",
    "  node scripts/start_health_walk.mjs --base-url URL --token TOKEN [--json]",
    "       [--screenshots DIR] [--scratch-dir DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-start-health.md.",
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

  const breakers = { telemetry: false, workspace: false };
  const counters = { preflight: 0 };
  const scratchRoot = args.scratch || tmpdir();
  const scratch = `${scratchRoot}/m5-walk-workspace-${Date.now().toString(36)}`;
  let original = null;

  const readCard = (page) => page.evaluate(() => {
    const rows = [...document.querySelectorAll("#startHealthRows .start-health-row")].map((row) => {
      const pill = row.querySelector(".status");
      return {
        key: row.dataset.health,
        label: row.querySelector(".start-health-label").textContent.trim(),
        value: row.querySelector(".start-health-value").textContent.trim(),
        tone: pill ? pill.dataset.tone : null,
        pill: pill ? pill.textContent.trim() : null,
      };
    });
    const summary = document.getElementById("startHealthSummary");
    const evidence = document.getElementById("startHealthEvidence");
    const workspaceSummary = document.getElementById("startWorkspaceSummary");
    return {
      rows,
      summary: summary ? { text: summary.textContent.trim(), tone: summary.dataset.tone } : null,
      basis: evidence ? evidence.dataset.basis : null,
      claim: evidence ? evidence.textContent.trim() : "",
      workspace: {
        path: (document.querySelector(".start-workspace-path") || {}).textContent?.trim() || "",
        source: (document.querySelector(".start-workspace-source") || {}).textContent?.trim() || "",
        summary: workspaceSummary ? workspaceSummary.textContent.trim() : null,
        input: (document.getElementById("startWorkspaceInput") || {}).value || "",
        hint: (document.getElementById("startWorkspaceHint") || {}).textContent || "",
        recents: [...document.querySelectorAll("#startWorkspaceRecent option")].map((o) => o.value).filter(Boolean),
      },
    };
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();
      if (/\/(api)\/onboarding\/preflight/.test(url) && request.method() === "GET") counters.preflight += 1;
      if (breakers.telemetry && request.method() === "GET" && /\/api\/telemetry(\?|$)/.test(url)) {
        return request.abort("failed");
      }
      if (breakers.workspace && request.method() === "POST" && /\/api\/workspace\/directory$/.test(url)) {
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

    // What the server says, so the card can be held against it rather than believed.
    const server = await page.evaluate(async () => {
      const [telemetry, sessions, workspace] = await Promise.all([
        fetch("/api/telemetry").then((r) => r.json()),
        fetch("/api/harness/sessions").then((r) => r.json()),
        fetch("/api/workspace/directory").then((r) => r.json()),
      ]);
      return {
        telemetry,
        defaultWorkspace: sessions.default_workspace || "",
        gitInfo: sessions.git_info || {},
        workspaceDir: workspace.workspace_dir || "",
      };
    });
    original = server.workspaceDir || server.defaultWorkspace || null;

    // The card is rendered by the rail's own poll; force one read so the walk compares
    // the card against the payload it just read, not one from a poll cycle ago.
    await page.evaluate(() => refreshTelemetry());
    await sleep(args.settleMs * 4);
    const card = await readCard(page);
    const payload = server.telemetry;
    const simulated = payload.telemetry_source === "simulated_demo";

    record(
      "the card draws four labelled rows, each with a tone",
      card.rows.length === 4
        && card.rows.every((row) => row.label && row.value && TONES.has(row.tone)),
      card.rows.map((row) => `${row.label}=${row.value} [${row.tone}]`).join(" | ").slice(0, 200),
    );
    if (card.rows.length !== 4) {
      throw new Error("the health card did not draw its four rows, so the remaining numbers cannot be judged");
    }
    const rowFor = (key) => card.rows.find((row) => row.key === key) || {};

    const mem = payload.memory || {};
    const used = +((mem.omlx_gb || 0) + (mem.orbstack_gb || 0) + (mem.macos_apps_gb || 0)).toFixed(1);
    const total = +(mem.total_gb || 0);
    const memoryRow = rowFor("unified-memory");
    record(
      "the memory row is the payload's own arithmetic",
      memoryRow.value.includes(`${used} /`) && memoryRow.value.includes(`${total}`),
      `card "${memoryRow.value}" vs payload ${used} / ${total} GB`,
    );
    const containerCount = ((payload.docker || {}).containers || []).length;
    const containerRow = rowFor("containers");
    record(
      "the container row is the payload's container list",
      containerRow.value.includes(String(containerCount)),
      `card "${containerRow.value}" vs ${containerCount} in payload`,
    );
    const runtimeRow = rowFor("model-runtime");
    record(
      "the runtime row reports what the payload reported",
      payload.omlx_online ? /loopback/.test(runtimeRow.value) : /not reachable/.test(runtimeRow.value),
      `omlx_online=${payload.omlx_online} — "${runtimeRow.value}"`,
    );

    // The basis: the point of the card. Whichever branch the host produces, the card
    // must say which one it is and what that means.
    if (simulated) {
      record(
        "a simulated payload is labelled unverified, not rendered as this machine's",
        card.basis === "unverified" && /demo data/i.test(card.summary.text) && /still this machine/i.test(card.claim),
        `basis=${card.basis} pill="${card.summary.text}"`,
      );
    } else {
      record(
        "a live payload is labelled measured and says when it was read",
        card.basis === "measured" && /live/i.test(card.summary.text) && /\bat\b/.test(card.claim),
        `basis=${card.basis} pill="${card.summary.text}"`,
      );
    }
    record(
      "the claim names the route the numbers came from",
      /\/api\/telemetry/.test(card.claim) || simulated,
      card.claim.slice(0, 160),
    );

    record(
      "the workspace row shows the configured directory and its state",
      Boolean(original) && card.workspace.path === original
        && /server's configured default|chosen on this pane/.test(card.workspace.source),
      `${card.workspace.path} — ${card.workspace.source}`,
    );
    record(
      "the workspace pill does not claim more than it knows",
      card.workspace.summary === (server.gitInfo.is_git ? `git:${server.gitInfo.branch || "HEAD"}` : "set")
        || card.workspace.summary === "Not set",
      `pill="${card.workspace.summary}" git=${JSON.stringify(server.gitInfo)}`,
    );
    await page.evaluate(() => document.getElementById("startHealthRows").scrollIntoView({ block: "center" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "health");

    if (!original) {
      block("setting a directory updates the row, the Harness field and the preflight", "the demo reports no project directory to change");
      throw new Error("no configured workspace, so there is nothing to change or restore");
    }

    // ── The change, through the pane ────────────────────────────────────────
    const preflightBefore = counters.preflight;
    await page.evaluate((dir) => {
      const input = document.getElementById("startWorkspaceInput");
      input.focus();
      input.value = dir;
      input.dispatchEvent(new Event("input"));
      document.getElementById("startWorkspaceBtn").click();
    }, scratch);
    await sleep(args.settleMs * 12);
    const changed = await readCard(page);
    const after = await page.evaluate(async () => {
      const [sessions, preflight] = await Promise.all([
        fetch("/api/harness/sessions").then((r) => r.json()),
        fetch("/api/onboarding/preflight").then((r) => r.json()),
      ]);
      const steps = (preflight.preflight || {}).steps || [];
      return {
        defaultWorkspace: sessions.default_workspace || "",
        harnessInput: (document.getElementById("harnessWorkspaceInput") || {}).value || "",
        workspaceStep: steps.find((step) => step.id === "workspace") || null,
      };
    });
    record(
      "setting a directory updates the row, the Harness field and the preflight",
      changed.workspace.path === scratch
        && changed.workspace.summary === "set"
        && after.defaultWorkspace === scratch
        && after.harnessInput === scratch
        && counters.preflight > preflightBefore,
      `row=${changed.workspace.path} server=${after.defaultWorkspace} harness=${after.harnessInput} `
        + `preflight reads ${preflightBefore}->${counters.preflight}`,
    );
    record(
      "the change is followed by a fresh preflight that agrees with it",
      Boolean(after.workspaceStep) && ["ready", "optional"].includes(after.workspaceStep.status),
      after.workspaceStep ? `${after.workspaceStep.status}: ${after.workspaceStep.detail.slice(0, 90)}` : "no workspace step",
    );
    record(
      "the pane says what it did, instead of silently succeeding",
      /is the project directory now/i.test(changed.workspace.hint) && /preflight/i.test(changed.workspace.hint),
      changed.workspace.hint.slice(0, 140),
    );
    await shoot(page, "workspace");

    // ── The restore, through the pane ──────────────────────────────────────
    await page.evaluate((dir) => {
      const input = document.getElementById("startWorkspaceInput");
      input.value = dir;
      input.dispatchEvent(new Event("input"));
      document.getElementById("startWorkspaceBtn").click();
    }, original);
    await sleep(args.settleMs * 12);
    const restored = await page.evaluate(async (dir) => {
      const sessions = await fetch("/api/harness/sessions").then((r) => r.json());
      return {
        row: (document.querySelector(".start-workspace-path") || {}).textContent?.trim() || "",
        harnessInput: (document.getElementById("harnessWorkspaceInput") || {}).value || "",
        server: sessions.default_workspace || "",
        restored: sessions.default_workspace === dir,
      };
    }, original);
    record(
      "restoring the original directory puts the row, the Harness field and the server back",
      restored.row === original && restored.harnessInput === original && restored.server === original,
      `row=${restored.row} harness=${restored.harnessInput} server=${restored.server}`,
    );

    // ── A refused write ─────────────────────────────────────────────────────
    breakers.workspace = true;
    await page.evaluate((dir) => {
      const input = document.getElementById("startWorkspaceInput");
      input.value = dir;
      input.dispatchEvent(new Event("input"));
      document.getElementById("startWorkspaceBtn").click();
    }, scratch);
    await sleep(args.settleMs * 8);
    const refusal = await page.evaluate(() => {
      const box = document.querySelector("#startWorkspaceState .presentation");
      return {
        kind: box ? box.dataset.kind : null,
        title: box && box.querySelector(".presentation-title") ? box.querySelector(".presentation-title").textContent.trim() : null,
        context: box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim()) : [],
        row: (document.querySelector(".start-workspace-path") || {}).textContent?.trim() || "",
        server: null,
      };
    });
    const stillInForce = await page.evaluate(async () => (await fetch("/api/harness/sessions").then((r) => r.json())).default_workspace || "");
    record(
      "a refused write is presented with its cause and what is still in force",
      refusal.kind === "error"
        && /still the project directory/i.test(refusal.context || "")
        && (refusal.context || "").includes(original)
        && refusal.actions.includes("Retry")
        && refusal.row === original
        && stillInForce === original,
      `${refusal.title} — ${refusal.context}`,
    );
    await page.evaluate(() => document.getElementById("startWorkspaceState").scrollIntoView({ block: "center" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "failed");

    breakers.workspace = false;
    await page.evaluate(() => {
      const button = [...document.querySelectorAll("#startWorkspaceState .presentation-actions button")]
        .find((node) => node.textContent.trim() === "Retry");
      if (button) button.focus();
    });
    const retryFocused = await page.evaluate(() => document.activeElement.tagName === "BUTTON");
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 12);
    const retried = await page.evaluate(async (dir) => {
      const box = document.querySelector("#startWorkspaceState .presentation");
      const sessions = await fetch("/api/harness/sessions").then((r) => r.json());
      return {
        stillPresent: Boolean(box),
        row: (document.querySelector(".start-workspace-path") || {}).textContent?.trim() || "",
        server: sessions.default_workspace || "",
        changed: sessions.default_workspace === dir,
      };
    }, scratch);
    record(
      "Retry performs the write it could not perform before",
      retryFocused && retried.changed && retried.row === scratch && !retried.stillPresent,
      `focused=${retryFocused} row=${retried.row} server=${retried.server}`,
    );

    // Back to the original, so the demo is left as it was found.
    await page.evaluate((dir) => {
      const input = document.getElementById("startWorkspaceInput");
      input.value = dir;
      input.dispatchEvent(new Event("input"));
      document.getElementById("startWorkspaceBtn").click();
    }, original);
    await sleep(args.settleMs * 10);

    // ── A refused telemetry read ────────────────────────────────────────────
    breakers.telemetry = true;
    await page.evaluate(() => refreshTelemetry());
    await sleep(args.settleMs * 4);
    const offline = await readCard(page);
    const offlineState = await page.evaluate(() => {
      const box = document.querySelector("#startHealthState .presentation");
      return {
        kind: box ? box.dataset.kind : null,
        cause: box && box.querySelector(".presentation-cause") ? box.querySelector(".presentation-cause").textContent.trim() : null,
        context: box && box.querySelector(".presentation-context") ? box.querySelector(".presentation-context").textContent.trim() : null,
        actions: box ? [...box.querySelectorAll(".presentation-actions button")].map((node) => node.textContent.trim()) : [],
        footerHidden: (document.getElementById("startHealthActions") || {}).style?.display === "none",
      };
    });
    record(
      "the failure box owns the recovery, so the same buttons are not printed twice",
      offlineState.footerHidden === true,
      `footer hidden=${offlineState.footerHidden}`,
    );
    record(
      "the failed read offers a cause, a way back, and what it did not affect",
      offlineState.kind === "error"
        && /\/api\/telemetry/.test(offlineState.cause || "")
        && /workspace row is unaffected/i.test(offlineState.context || "")
        && offlineState.actions.includes("Re-check"),
      `${offlineState.cause} — ${offlineState.actions.join(", ")}`,
    );
    record(
      "a refused telemetry read drops the card to unavailable rather than keeping stale numbers",
      offline.basis === "unavailable"
        && /offline/i.test(offline.summary.text)
        && offline.rows.length === 4
        && offline.rows.filter((row) => /not measured yet/.test(row.value)).length === 3,
      `basis=${offline.basis} pill="${offline.summary.text}" rows=${offline.rows.map((r) => r.value).join(" | ").slice(0, 120)}`,
    );
    record(
      "the workspace row survives a telemetry failure, because it never came from telemetry",
      offline.workspace.path === original && /still this machine/i.test(offline.claim),
      `row=${offline.workspace.path} claim="${offline.claim.slice(0, 100)}"`,
    );
    await page.evaluate(() => document.getElementById("startHealthState").scrollIntoView({ block: "center" }));
    await sleep(args.settleMs * 2);
    await shoot(page, "offline");

    breakers.telemetry = false;
    await page.evaluate(() => refreshTelemetry());
    await sleep(args.settleMs * 6);
    const recovered = await readCard(page);
    recovered.state = await page.evaluate(() => {
      const box = document.querySelector("#startHealthState .presentation");
      return box ? box.dataset.kind : null;
    });
    recovered.footerVisible = await page.evaluate(
      () => (document.getElementById("startHealthActions") || {}).style?.display !== "none",
    );
    record(
      "the card recovers when telemetry answers again",
      recovered.basis === (simulated ? "unverified" : "measured")
        && recovered.rows.filter((row) => /not measured yet/.test(row.value)).length === 0
        && recovered.state === null
        && recovered.footerVisible === true,
      `basis=${recovered.basis} pill="${recovered.summary.text}"`,
    );

    // ── Narrow, and no errors ──────────────────────────────────────────────
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const health = document.getElementById("startHealthRows");
      const workspace = document.getElementById("startWorkspaceRow");
      return {
        healthOverflow: health ? health.scrollWidth > health.clientWidth + 1 : true,
        workspaceOverflow: workspace ? workspace.scrollWidth > workspace.clientWidth + 1 : true,
        rowHeight: health ? Math.round(health.getBoundingClientRect().height) : 0,
      };
    });
    record(
      "both cards survive a 900px viewport",
      narrow.healthOverflow === false && narrow.workspaceOverflow === false && narrow.rowHeight > 40,
      `health overflow=${narrow.healthOverflow} workspace overflow=${narrow.workspaceOverflow} height=${narrow.rowHeight}px`,
    );
    // Not "narrow.png": that capture belongs to the lead, and a second walk silently
    // overwriting it would make the P2-01 picture describe a pane it never showed.
    await shoot(page, "health-narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    // The workspace setting is the demo store's, but a walk that crashed after changing
    // it would still hand the next reader someone else's directory, so it is put back
    // directly rather than trusting the assertions above to have finished.
    try {
      const pages = await browser.pages();
      const page = pages[0];
      if (page && original) {
        await page.evaluate(async (dir) => {
          await fetch("/api/workspace/directory", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workspace_dir: dir }),
          }).catch(() => {});
        }, original).catch(() => {});
      }
    } catch (_error) {}
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
    throw new Error(`${failed.length} start-health check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`start_health_walk: ${error.message}`);
  process.exit(1);
});
