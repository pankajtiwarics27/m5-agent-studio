#!/usr/bin/env node
/**
 * UIUX-P2-01 browser regressions for Start readiness and re-check recovery.
 * Runs only against the disposable loopback demo with a synthetic token.
 */
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

function argsOf(argv) {
  const out = { baseUrl: "", token: "" };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--base-url") out.baseUrl = argv[++i] || "";
    else if (argv[i] === "--token") out.token = argv[++i] || "";
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  return out;
}

function checkedDemoUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("test demo URL is invalid"); }
  if (!["http:", "https:"].includes(url.protocol) ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("onboarding walk accepts only a loopback demo origin");
  }
  return url;
}

async function puppeteerModule() {
  const candidate = process.env.STUDIO_PUPPETEER_CORE;
  if (!candidate) throw new Error("STUDIO_PUPPETEER_CORE is required");
  const loaded = await import(pathToFileURL(resolve(candidate)).href);
  return loaded.default || loaded;
}

function record(results, name, passed, detail = "") {
  results.push({ name, passed: Boolean(passed), detail });
}

async function main() {
  const { baseUrl, token } = argsOf(process.argv.slice(2));
  const url = checkedDemoUrl(baseUrl);
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(token)) throw new Error("test token must be synthetic and disposable");
  const endpoint = new URL("/api/workspace/directory", url);
  const unauthenticated = await fetch(endpoint, { redirect: "manual", signal: AbortSignal.timeout(10000) });
  if (unauthenticated.status !== 401) throw new Error("demo endpoint did not require authentication");
  const authenticated = await fetch(endpoint, {
    redirect: "manual", signal: AbortSignal.timeout(10000), headers: { Authorization: `Bearer ${token}` },
  });
  if (authenticated.status !== 200) throw new Error("demo endpoint rejected the synthetic test token");

  const puppeteer = await puppeteerModule();
  const chrome = process.env.STUDIO_SCREENSHOT_CHROME;
  if (!chrome || !existsSync(chrome)) throw new Error("STUDIO_SCREENSHOT_CHROME is required");
  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const results = [];
  try {
    const page = await browser.newPage();
    const errors = [];
    const chatStreamRequests = [];
    let firstNavigation = true;
    await page.setRequestInterception(true);
    page.on("pageerror", error => errors.push(String(error)));
    page.on("request", request => {
      try {
        if (request.url().includes("/api/chat/stream")) chatStreamRequests.push(request.url());
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
          const allowed = firstNavigation && request.url() === url.href && request.redirectChain().length === 0;
          firstNavigation = false;
          if (!allowed) { void request.abort(); return; }
        }
        void request.continue();
      } catch { void request.abort().catch(() => {}); }
    });
    await page.evaluateOnNewDocument((origin, value) => {
      try {
        if (window.top === window && window.location.origin === origin) {
          window.sessionStorage.setItem("m5_api_token", value);
        }
      } catch { /* storage refusal must not fail open */ }
    }, url.origin, token);
    const response = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (!response || response.status() !== 200 || !/text\/html/i.test(response.headers()["content-type"] || "") || page.url() !== url.href) {
      throw new Error("isolated demo did not serve direct successful HTML");
    }
    await page.waitForSelector("#onboardingSteps", { timeout: 10000 });
    await page.waitForFunction(() => {
      const list = document.getElementById("onboardingSteps");
      return list?.children.length === 6 && list.getAttribute("aria-busy") !== "true";
    }, { timeout: 15000 });

    await page.evaluate(() => window.switchTab("start"));
    await page.waitForFunction(() => document.getElementById("pane-start").classList.contains("active") &&
      document.getElementById("onboardingSteps").getAttribute("aria-busy") !== "true" &&
      !document.getElementById("onboardingCheckBtn").disabled, { timeout: 15000 });
    const initial = await page.evaluate(() => {
      const list = document.getElementById("onboardingSteps");
      const summary = document.getElementById("onboardingSummary");
      const rows = [...list.querySelectorAll('[role="listitem"]')];
      return {
        summaryRole: summary.getAttribute("role"),
        summary: summary.textContent.trim(),
        listRole: list.getAttribute("role"),
        listLabel: list.getAttribute("aria-label"),
        rows: rows.length,
        statuses: rows.map(row => row.querySelector(".onboarding-step-state")?.textContent.trim() || ""),
        actionCounts: rows.map(row => row.querySelectorAll("button").length),
      };
    });
    record(results, "Start exposes a live readiness summary and labeled checklist", initial.summaryRole === "status" &&
      initial.listRole === "list" && initial.listLabel === "Readiness steps" && initial.rows === 6 &&
      initial.statuses.every(Boolean), JSON.stringify(initial));
    record(results, "Each checklist step offers no more than one direct action",
      initial.actionCounts.every(count => count <= 1), JSON.stringify(initial.actionCounts));

    const before = await page.evaluate(() => document.getElementById("onboardingSteps").textContent);
    await page.evaluate(() => {
      window.__nativeOnboardingFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const requestUrl = new URL(typeof input === "string" ? input : input.url, window.location.href);
        if (requestUrl.pathname === "/api/onboarding/preflight") {
          return new Promise((_resolve, reject) => { window.__rejectOnboardingCheck = reject; });
        }
        return window.__nativeOnboardingFetch(input, init);
      };
    });
    await page.focus("#onboardingCheckBtn");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => {
      const list = document.getElementById("onboardingSteps");
      return document.getElementById("onboardingSummary").textContent.includes("Checking") &&
        list.getAttribute("aria-busy") === "true" && document.getElementById("onboardingCheckBtn").disabled;
    }, { timeout: 5000 });
    const loading = await page.evaluate(() => ({
      retained: document.getElementById("onboardingSteps").textContent,
      rows: document.getElementById("onboardingSteps").children.length,
      ariaBusy: document.getElementById("onboardingSteps").getAttribute("aria-busy"),
      checkDisabled: document.getElementById("onboardingCheckBtn").disabled,
      focusedId: document.activeElement.id,
      summaryTabIndex: document.getElementById("onboardingSummary").tabIndex,
    }));
    record(results, "Re-check announces its busy state and retains the last checklist",
      loading.rows === 6 && loading.retained === before && loading.ariaBusy === "true" && loading.checkDisabled,
      JSON.stringify({ rows: loading.rows, ariaBusy: loading.ariaBusy, checkDisabled: loading.checkDisabled }));
    record(results, "Keyboard re-check keeps focus on the live status while its control is disabled",
      loading.focusedId === "onboardingSummary" && loading.summaryTabIndex === -1,
      JSON.stringify({ focusedId: loading.focusedId, summaryTabIndex: loading.summaryTabIndex }));

    await page.evaluate(() => window.__rejectOnboardingCheck(new Error("synthetic preflight refusal")));
    await page.waitForFunction(() => document.getElementById("onboardingSummary").textContent.startsWith("Check failed at ") &&
      document.querySelector("#onboardingState [role=alert]")?.textContent.includes("synthetic preflight refusal"), { timeout: 5000 });
    const failed = await page.evaluate(() => {
      const list = document.getElementById("onboardingSteps");
      const alert = document.querySelector("#onboardingState [role=alert]");
      return {
        summary: document.getElementById("onboardingSummary").textContent.trim(),
        busy: list.getAttribute("aria-busy"),
        retained: list.textContent,
        alert: alert?.textContent || "",
        checkDisabled: document.getElementById("onboardingCheckBtn").disabled,
      };
    });
    record(results, "A failed re-check names the time and cause, preserves the previous result, and unlocks retry",
      failed.summary.startsWith("Check failed at ") && failed.busy === null && failed.retained === before &&
      failed.alert.includes("synthetic preflight refusal") && failed.alert.includes("last successful check") && !failed.checkDisabled,
      JSON.stringify({ summary: failed.summary, busy: failed.busy, rowsRetained: failed.retained === before,
        causeShown: failed.alert.includes("synthetic preflight refusal"), retryEnabled: !failed.checkDisabled }));

    await page.evaluate(() => { window.fetch = window.__nativeOnboardingFetch; });
    await page.focus("#onboardingCheckBtn");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.getElementById("onboardingSteps").getAttribute("aria-busy") !== "true" &&
      document.getElementById("onboardingState").textContent.trim() === "", { timeout: 15000 });
    const recovered = await page.evaluate(() => ({
      summaryRole: document.getElementById("onboardingSummary").getAttribute("role"),
      summary: document.getElementById("onboardingSummary").textContent.trim(),
      rows: document.getElementById("onboardingSteps").children.length,
      busy: document.getElementById("onboardingSteps").getAttribute("aria-busy"),
    }));
    record(results, "A successful re-check clears the error and restores a current summary",
      recovered.summaryRole === "status" && recovered.rows === 6 && recovered.busy === null &&
      recovered.summary.includes("of 6 steps ready"), JSON.stringify(recovered));

    await page.evaluate(() => window.startGuidedFirstTask());
    const prepared = await page.evaluate(() => ({
      tab: document.querySelector(".mode-tab.active")?.dataset.tab,
      prompt: document.getElementById("chatInput")?.value,
      note: document.getElementById("onboardingFirstTask")?.textContent,
    }));
    record(results, "The first-task action prepares Chat for review without sending a model request",
      prepared.tab === "chat" && Boolean(prepared.prompt) && prepared.note.includes("Review it and press Send") && chatStreamRequests.length === 0,
      JSON.stringify({ tab: prepared.tab, promptPrepared: Boolean(prepared.prompt), note: prepared.note,
        chatStreamRequests: chatStreamRequests.length }));
    record(results, "No browser page errors during the Start onboarding walk", errors.length === 0, errors.join("; "));
  } finally {
    await browser.close();
  }
  const passed = results.filter(result => result.passed).length;
  const failed = results.length - passed;
  console.log(JSON.stringify({ results, passed, failed }, null, 2));
  if (failed) process.exitCode = 1;
}

main().catch(error => {
  console.error(`onboarding_walk: ${error.message}`);
  process.exit(1);
});
