#!/usr/bin/env node
/** UI walk for Compare tab-local reload recovery, server-history revisit and export controls.
 * Run only against a disposable loopback demo with a synthetic token; history is mocked at the
 * browser boundary so this walk never writes or mutates the demo database.
 */
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function argsOf(argv) {
  const out = { baseUrl: "", token: "", screenshotsDir: "" };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--base-url") out.baseUrl = argv[++i] || "";
    else if (argv[i] === "--token") out.token = argv[++i] || "";
    else if (argv[i] === "--screenshots-dir") out.screenshotsDir = argv[++i] || "";
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  return out;
}

function checkedUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("demo URL is invalid"); }
  if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("walk accepts only a loopback demo origin");
  }
  return url;
}

function check(name, passed, detail = "") {
  if (!passed) throw new Error(`${name} failed${detail ? `: ${detail}` : ""}`);
  return { name, passed: true, detail };
}

async function main() {
  const { baseUrl, token, screenshotsDir: requestedScreenshotsDir } = argsOf(process.argv.slice(2));
  const url = checkedUrl(baseUrl);
  const screenshotsDir = requestedScreenshotsDir ? resolve(requestedScreenshotsDir) : "";
  if (screenshotsDir) await mkdir(screenshotsDir, { recursive: true });
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(token)) throw new Error("token must be synthetic and disposable");
  const authEndpoint = new URL("/api/workspace/directory", url);
  const unauth = await fetch(authEndpoint, { redirect: "manual", signal: AbortSignal.timeout(10000) });
  if (unauth.status !== 401) throw new Error("demo did not require authentication");
  const auth = await fetch(authEndpoint, { redirect: "manual", signal: AbortSignal.timeout(10000), headers: { Authorization: `Bearer ${token}` } });
  if (auth.status !== 200) throw new Error("demo rejected the synthetic token");

  const candidate = process.env.STUDIO_PUPPETEER_CORE;
  const chrome = process.env.STUDIO_SCREENSHOT_CHROME;
  if (!candidate || !existsSync(candidate)) throw new Error("STUDIO_PUPPETEER_CORE is required");
  if (!chrome || !existsSync(chrome)) throw new Error("STUDIO_SCREENSHOT_CHROME is required");
  const loaded = await import(pathToFileURL(resolve(candidate)).href);
  const puppeteer = loaded.default || loaded;
  const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean);
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", ...extraArgs] });
  const errors = [];
  const historyRequests = [];
  const streamRequests = [];
  const results = [];
  const screenshots = [];
  async function captureState(page, id, title) {
    if (!screenshotsDir) return;
    const file = `${id}.png`;
    const path = join(screenshotsDir, file);
    await page.evaluate(() => document.getElementById("debateSavedPanel")?.scrollIntoView({ block: "center" }));
    await new Promise(resolve => setTimeout(resolve, 120));
    await page.screenshot({ path, fullPage: false });
    const bytes = await readFile(path);
    screenshots.push({ id, title, file, bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex") });
  }
  let failNextHistoryRead = false;
  const record = {
    prompt: "Which option fits the local workload?",
    opinions: [
      { slot: 0, model: "fixture/alpha", content: "Alpha keeps the comparison as one distinct response.", usage: { completion_tokens: 9 } },
      { slot: 1, model: "fixture/beta", content: "Beta remains a separate panel after revisit.", usage: { completion_tokens: 8 } },
    ],
    synthesis: { model: "fixture/judge", content: "A synthesis is shown separately, not substituted for either panel.", usage: { completion_tokens: 7 } },
    total_elapsed_s: 1.25,
  };
  const bundle = {
    format: "m5-agent-studio history export",
    tables: { runs: [{ id: 9061, created_at: "2026-11-02T09:00:00+00:00", mode: "debate", title: "Fixture comparison", payload_json: JSON.stringify(record) }] },
  };

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1200, deviceScaleFactor: 1 });
    page.on("pageerror", error => errors.push(`pageerror: ${error}`));
    page.on("console", message => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
    await page.setRequestInterception(true);
    page.on("request", request => {
      const requestUrl = new URL(request.url());
      if (request.isNavigationRequest() && request.frame() === page.mainFrame() && requestUrl.origin !== url.origin) {
        void request.abort(); return;
      }
      if (requestUrl.pathname === "/api/history/export") {
        const authorization = request.headers().authorization || "";
        historyRequests.push({ method: request.method(), authorization });
        if (authorization !== `Bearer ${token}`) { void request.respond({ status: 401, contentType: "application/json", body: "{}" }); return; }
        if (failNextHistoryRead) {
          failNextHistoryRead = false;
          void request.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ format: "fixture unavailable" }) });
          return;
        }
        void request.respond({ status: 200, contentType: "application/json", headers: { "Content-Disposition": "attachment; filename=studio-history-export.json" }, body: JSON.stringify(bundle) });
        return;
      }
      if (requestUrl.pathname === "/api/debate/stream") streamRequests.push(request.method());
      void request.continue();
    });
    await page.evaluateOnNewDocument((key, value, origin) => {
      if (window.top === window && location.origin === origin) sessionStorage.setItem(key, value);
    }, "m5_api_token", token, url.origin);
    await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector("#debateRunBtn", { timeout: 20000 });
    await page.click("#tab-debate");

    const restored = {
      version: 1,
      saved_at: "2026-11-02T09:00:00Z",
      run_id: 9059,
      run_state: "completed",
      prompt: "Does reload preserve two independent answers?",
      selected_models: ["fixture/alpha", "fixture/beta"],
      include_synthesis: true,
      judge_model: "fixture/judge",
      judged_criteria: ["Correctness", "Fits my constraints"],
      panels: [
        { slot: 0, model: "fixture/alpha", content: "First saved panel: its complete answer survives reload.", usage: { completion_tokens: 5 }, status: "answered", error: "" },
        { slot: 1, model: "fixture/beta", content: "Second saved panel: it does not collapse into the first.", usage: { completion_tokens: 6 }, status: "answered", error: "" },
      ],
      synthesis: { model: "fixture/judge", content: "Separate saved synthesis.", usage: { completion_tokens: 4 } },
      total_elapsed_s: 3.5,
    };
    await page.evaluate(snapshot => sessionStorage.setItem("m5_compare_last_view_v1", JSON.stringify(snapshot)), restored);
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    try {
      await page.waitForFunction(() => document.querySelectorAll("#debateGrid .stage-output-card").length === 2, { timeout: 20000 });
    } catch (error) {
      const debug = await page.evaluate(() => ({
        grid: document.getElementById("debateGrid")?.innerText || "missing grid",
        status: document.getElementById("debateSavedStatus")?.innerText || "missing status",
        copy: sessionStorage.getItem("m5_compare_last_view_v1"),
        token: Boolean(sessionStorage.getItem("m5_api_token")),
        pane: document.querySelector(".mode-tab.active")?.id || "none",
      }));
      throw new Error(`session restore did not render two panels: ${error.message}; ${JSON.stringify({ debug, errors })}`);
    }
    await page.click("#tab-debate");
    const afterReload = await page.evaluate(() => ({
      prompt: document.getElementById("debatePrompt").value,
      panels: [...document.querySelectorAll("#debateGrid .stage-output-card .stage-output-body")].map(node => node.innerText),
      synthesis: document.querySelector("#debateSynthesisArea .stage-output-body")?.innerText || "",
      synthesisHtml: document.getElementById("debateSynthesisArea")?.innerHTML || "missing area",
      includeSynthesis: document.getElementById("d-synth")?.checked,
      state: document.getElementById("debateSavedStatus").innerText,
      saveCopy: Boolean(sessionStorage.getItem("m5_compare_last_view_v1")),
    }));
    results.push(check("Reload restores the prompt and two distinct Compare panels plus separate synthesis",
      afterReload.prompt === restored.prompt && afterReload.panels.length === 2 &&
      afterReload.panels[0].includes("First saved panel") && afterReload.panels[1].includes("Second saved panel") &&
      afterReload.synthesis.includes("Separate saved synthesis") && afterReload.saveCopy,
      JSON.stringify(afterReload)));
    results.push(check("Restored comparison explains its tab-local source", afterReload.state.includes("Restored from this browser tab"), afterReload.state));
    await captureState(page, "session-reload", "Two separate panels restored after reload");

    await page.focus("#debateFindSavedBtn");
    await page.keyboard.press("Tab");
    const focus = await page.evaluate(() => ({
      id: document.activeElement?.id,
      outline: getComputedStyle(document.activeElement).outlineStyle,
      width: Number.parseFloat(getComputedStyle(document.activeElement).outlineWidth) || 0,
    }));
    results.push(check("Keyboard focus moves from Find to the labelled saved-run select with a visible ring",
      focus.id === "debateSavedSelect" && (focus.outline === "solid" || focus.width >= 2), JSON.stringify(focus)));

    failNextHistoryRead = true;
    await page.click("#debateFindSavedBtn");
    await page.waitForFunction(() => document.getElementById("debateSavedStatus").innerText.includes("did not contain a runs table"), { timeout: 10000 });
    const failed = await page.evaluate(() => ({
      message: document.getElementById("debateSavedStatus").innerText,
      findDisabled: document.getElementById("debateFindSavedBtn").disabled,
    }));
    results.push(check("History failure is actionable and the user can retry", failed.message.includes("Nothing was run or changed") && !failed.findDisabled, JSON.stringify(failed)));

    await page.click("#debateFindSavedBtn");
    await page.waitForFunction(() => document.getElementById("debateSavedStatus").innerText.includes("Found 1 saved Compare run"), { timeout: 10000 });
    const optionText = await page.$eval("#debateSavedSelect option", option => option.textContent);
    await page.click("#debateLoadSavedBtn");
    await page.waitForFunction(() => document.getElementById("debateSavedStatus").innerText.includes("Loaded server record #9061"), { timeout: 10000 });
    const revisited = await page.evaluate(() => ({
      prompt: document.getElementById("debatePrompt").value,
      panels: [...document.querySelectorAll("#debateGrid .stage-output-card .stage-output-body")].map(node => node.innerText),
      synthesis: document.querySelector("#debateSynthesisArea .stage-output-body")?.innerText || "",
      source: document.getElementById("debateSavedStatus").innerText,
    }));
    results.push(check("Existing history export revisits the same run as separate panels, not one answer",
      optionText.includes("Fixture comparison") && revisited.prompt === record.prompt && revisited.panels.length === 2 &&
      revisited.panels[0].includes("Alpha keeps") && revisited.panels[1].includes("Beta remains") &&
      revisited.synthesis.includes("A synthesis is shown separately") && revisited.source.includes("not loaded") && revisited.source.includes("not recovered criteria"),
      JSON.stringify(revisited)));
    await captureState(page, "history-revisit", "Existing history record revisited as separate panels");

    await page.click("#debateExportHistoryBtn");
    await page.waitForFunction(() => document.getElementById("debateSavedStatus").innerText.includes("Started downloading the full history export"), { timeout: 10000 });
    results.push(check("Download requests the existing full export with the session bearer token",
      historyRequests.filter(item => item.method === "GET" && item.authorization === `Bearer ${token}`).length === 3,
      JSON.stringify(historyRequests.map(({ method, authorization }) => ({ method, authorized: authorization === `Bearer ${token}` })))));

    await page.click("#debateForgetSessionBtn");
    const forgotten = await page.evaluate(() => ({
      message: document.getElementById("debateSavedStatus").innerText,
      hasCopy: Boolean(sessionStorage.getItem("m5_compare_last_view_v1")),
      panelCount: document.querySelectorAll("#debateGrid .stage-output-card").length,
    }));
    results.push(check("Forget removes only the tab copy and leaves both visible panels in place",
      !forgotten.hasCopy && forgotten.panelCount === 2 && forgotten.message.includes("visible result") && forgotten.message.includes("unchanged"), JSON.stringify(forgotten)));
    results.push(check("This walk never starts a model run", streamRequests.length === 0, JSON.stringify(streamRequests)));

    if (errors.length) throw new Error(`browser errors during tested journeys: ${errors.join(" | ")}`);
    if (screenshotsDir) {
      await writeFile(join(screenshotsDir, "index.json"), JSON.stringify({
        schema: 1,
        captured_at: new Date().toISOString(),
        viewport: { width: 1440, height: 1200, full_page: false, device_scale_factor: 1 },
        source: "tests/browser/compare_saved_walk.mjs against scripts/demo_tour.py with mocked authenticated history export",
        shots: screenshots,
      }, null, 2) + "\n");
    }
    console.log(JSON.stringify({ status: "passed", checks: results, console_errors: errors.length, history_requests: historyRequests.length, screenshots: screenshots.map(shot => shot.file) }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
