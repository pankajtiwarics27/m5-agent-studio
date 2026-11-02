#!/usr/bin/env node
/**
 * Browser walk for browsing old Compare runs and downloading the full existing history export.
 *
 * This page uses the isolated demo shell, but stubs GET /api/history/export in the browser.
 * It never streams a model request or writes server history.
 */
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const args = { baseUrl: "", token: "", tokenKey: "m5_api_token", timeoutMs: 30000 };
for (let i = 2; i < process.argv.length; i += 1) {
  const arg = process.argv[i];
  if (arg === "--base-url") args.baseUrl = process.argv[++i] || "";
  else if (arg === "--token") args.token = process.argv[++i] || "";
  else if (arg === "--json") args.json = true;
  else throw new Error(`unknown argument: ${arg}`);
}
if (!args.baseUrl || !args.token) throw new Error("--base-url and --token are required");

async function loadPuppeteer() {
  const candidates = [
    process.env.STUDIO_PUPPETEER_CORE || "",
    resolve("tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
    "puppeteer-core",
  ].filter(Boolean);
  const failures = [];
  for (const candidate of candidates) {
    try {
      const target = candidate.startsWith("/") || candidate.startsWith(".") ? pathToFileURL(resolve(candidate)).href : candidate;
      const module = await import(target);
      return module.default || module;
    } catch (error) {
      failures.push(`${candidate}: ${error.message}`);
    }
  }
  throw new Error(`puppeteer-core is not importable. ${failures.join("\n")}`);
}

function resolveChrome() {
  const chrome = process.env.STUDIO_SCREENSHOT_CHROME;
  if (!chrome) throw new Error("STUDIO_SCREENSHOT_CHROME is required");
  return chrome;
}

const puppeteer = await loadPuppeteer();
const browser = await puppeteer.launch({
  executablePath: resolveChrome(),
  args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --single-process --disable-gpu --no-sandbox").split(" "),
});
const errors = [];
const requests = [];
const report = [];
const wait = (ms = 250) => new Promise((resolveWait) => setTimeout(resolveWait, ms));
const check = (name, passed, details = "") => report.push({ name, passed: Boolean(passed), details });

const history = {
  tables: {
    runs: [
      {
        id: 104,
        mode: "debate",
        created_at: "2026-10-31T08:30:00Z",
        payload_json: {
          prompt: "Compare a queue store; <img src=x onerror=alert(1)>",
          opinions: [
            { model: "History Model A", content: "Opinion A stays separate.", usage: { eval_count: 12, total_duration: 1000000000 } },
            { model: "History Model B", content: "Opinion B remains distinct.", usage: { eval_count: 14, total_duration: 1500000000 } },
          ],
          synthesis: { model: "History Arbiter", content: "The synthesis is separately displayed.", usage: { eval_count: 8 } },
          total_elapsed_s: 2.5,
        },
      },
      {
        id: 103,
        mode: "chat",
        created_at: "2026-10-31T08:20:00Z",
        payload_json: { prompt: "A different mode must not appear in Compare history." },
      },
      {
        id: 102,
        mode: "debate",
        created_at: "2026-10-30T08:30:00Z",
        payload_json: {
          prompt: "An older Compare run",
          opinions: [{ model: "Older Model", content: "Older opinion.", usage: {} }],
          synthesis: { model: "Older Arbiter", content: "Older synthesis.", usage: {} },
          total_elapsed_s: 1.2,
        },
      },
    ],
    providers: [{ name: "export-wide-table", credential_value: "best-effort-redacted-on-the-server" }],
    audit: [{ id: "synthetic-audit-row" }],
  },
};

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("request", (request) => requests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  await page.evaluateOnNewDocument((key, token, data) => {
    sessionStorage.setItem(key, token);
    window.__historyFixture = data;
    window.__historyStatus = 200;
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, init = {}) => {
      const target = new URL(typeof input === "string" ? input : input.url, window.location.href);
      const method = String(init.method || (typeof input === "object" && input.method) || "GET").toUpperCase();
      if (target.pathname === "/api/history/export" && method === "GET") {
        window.__historyReadCount = Number(window.__historyReadCount || 0) + 1;
        return Promise.resolve(new Response(
          JSON.stringify(window.__historyFixture),
          { status: window.__historyStatus, headers: { "content-type": "application/json" } },
        ));
      }
      return originalFetch(input, init);
    };
    window.__downloadedHistory = null;
    URL.createObjectURL = (blob) => { window.__historyDownloadBlob = blob; return "blob:history-export"; };
    URL.revokeObjectURL = () => {};
    HTMLAnchorElement.prototype.click = function click() {
      window.__downloadedHistory = { filename: this.download, href: this.href };
    };
  }, args.tokenKey, args.token, history);
  await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
  await wait(1600);
  await page.evaluate(() => switchTab("debate"));
  await wait(400);

  const initial = await page.evaluate(() => ({
    visible: Boolean(document.getElementById("debateHistoryPanel"))
      && !document.getElementById("debateHistoryPanel").hidden,
    noRecordYet: document.getElementById("debateRecord").hidden,
    selectedDisabled: document.getElementById("debateHistoryLoadBtn").disabled,
  }));
  check("history navigation is available before a new run", initial.visible && initial.noRecordYet && initial.selectedDisabled, JSON.stringify(initial));

  await page.click("#debateHistoryFindBtn");
  await page.waitForFunction(() => /Found 2 saved Compare runs/.test(document.getElementById("debateHistoryStatus").textContent), { timeout: 5000 });
  const listed = await page.evaluate(() => ({
    options: [...document.querySelectorAll("#debateHistorySelect option")].map((option) => ({ value: option.value, text: option.textContent })),
    status: document.getElementById("debateHistoryStatus").textContent,
    injectedImageCount: document.querySelectorAll("#debateHistorySelect img").length,
    reads: Number(window.__historyReadCount || 0),
  }));
  check("Find lists saved Compare runs only, in recent-first order, without parsing prompt markup", listed.options.length === 3
    && listed.options[1].value === "104" && listed.options[2].value === "102"
    && listed.options.every((option) => option.value !== "103")
    && listed.injectedImageCount === 0 && listed.reads === 1,
  JSON.stringify(listed));

  await page.select("#debateHistorySelect", "104");
  await page.click("#debateHistoryLoadBtn");
  await page.waitForFunction(() => document.getElementById("debateRecord").dataset.source === "server"
    && /run 104/.test(document.getElementById("debateRecord").textContent), { timeout: 5000 });
  const restored = await page.evaluate(() => ({
    source: document.getElementById("debateRecord").dataset.source,
    recordText: document.getElementById("debateRecord").textContent,
    historyStatus: document.getElementById("debateHistoryStatus").textContent,
    prompt: document.getElementById("debatePrompt").value,
    opinions: [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => card.textContent),
    synthesis: document.getElementById("debateSynthesisArea").textContent,
    streamPosts: window.performance.getEntriesByType("resource").filter((entry) => new URL(entry.name).pathname === "/api/debate/stream").length,
    requestCount: Number(window.__historyReadCount || 0),
  }));
  check("Revisit restores separate opinions and synthesis without rerunning or changing the server copy",
    restored.source === "server" && restored.prompt.includes("Compare a queue store")
      && restored.opinions.length === 2 && restored.opinions[0].includes("Opinion A stays separate")
      && restored.opinions[1].includes("Opinion B remains distinct")
      && restored.synthesis.includes("The synthesis is separately displayed")
      && restored.historyStatus.includes("custom criteria are not stored")
      && restored.requestCount === 1 && restored.streamPosts === 0,
    JSON.stringify(restored));

  await page.click("#debateHistoryExportBtn");
  await page.waitForFunction(() => /Started downloading the full history export/.test(document.getElementById("debateHistoryStatus").textContent), { timeout: 5000 });
  const downloaded = await page.evaluate(async () => ({
    filename: window.__downloadedHistory && window.__downloadedHistory.filename,
    json: window.__historyDownloadBlob ? await window.__historyDownloadBlob.text() : "",
    reads: Number(window.__historyReadCount || 0),
  }));
  let downloadJson = null;
  try { downloadJson = JSON.parse(downloaded.json); } catch (_error) {}
  check("full export downloads all existing history tables rather than only one Compare answer",
    downloaded.filename === "studio-history-export.json" && downloadJson
      && Array.isArray(downloadJson.tables.runs) && Array.isArray(downloadJson.tables.providers)
      && Array.isArray(downloadJson.tables.audit) && downloadJson.tables.runs.length === 3
      && downloaded.reads === 2,
    `filename=${downloaded.filename}, tables=${downloadJson ? Object.keys(downloadJson.tables).join(",") : "invalid"}`);

  await page.evaluate(() => { window.__historyStatus = 503; });
  await page.click("#debateHistoryFindBtn");
  await page.waitForFunction(() => /HTTP 503/.test(document.getElementById("debateHistoryStatus").textContent), { timeout: 5000 });
  const failure = await page.evaluate(() => ({
    status: document.getElementById("debateHistoryStatus").textContent,
    tone: document.getElementById("debateHistoryStatus").dataset.tone,
    record: document.getElementById("debateRecord").dataset.source,
    opinionCount: document.querySelectorAll("#debateGrid .stage-output-card").length,
  }));
  check("history read errors are announced without replacing the current comparison", failure.tone === "error"
    && /HTTP 503/.test(failure.status) && failure.record === "server" && failure.opinionCount === 2,
    JSON.stringify(failure));

  await page.evaluate(() => { window.__historyStatus = 200; });
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
  await page.focus("#debateHistoryFindBtn");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => /Found 2 saved Compare runs/.test(document.getElementById("debateHistoryStatus").textContent), { timeout: 5000 });
  const mobile = await page.evaluate(() => ({
    active: document.activeElement && document.activeElement.id,
    panelWidth: document.getElementById("debateHistoryPanel").scrollWidth,
    clientWidth: document.getElementById("debateHistoryPanel").clientWidth,
    viewport: window.innerWidth,
  }));
  check("history controls remain keyboard-operable and fit a narrow viewport", mobile.active === "debateHistoryFindBtn"
    && mobile.panelWidth <= mobile.clientWidth + 1 && mobile.clientWidth <= mobile.viewport,
    JSON.stringify(mobile));

  check("no model or state-changing request was sent", !requests.some((item) => /^(POST|PUT|DELETE) \/api\/(history|debate)/.test(item)), requests.filter((item) => /^(POST|PUT|DELETE) \/api\//.test(item)).join(", "));
  check("no browser console or page errors", errors.length === 0, errors.slice(0, 4).join(" | "));
} finally {
  await browser.close();
}

const failed = report.filter((entry) => !entry.passed);
if (args.json) console.log(JSON.stringify({ checks: report, passed: report.length - failed.length, failed: failed.length }, null, 2));
else console.log(`${report.length - failed.length}/${report.length} checks passed`);
if (failed.length) process.exitCode = 1;
