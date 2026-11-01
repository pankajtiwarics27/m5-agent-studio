#!/usr/bin/env node
/** Browser-only accessibility regressions for the real isolated demo UI.
 * The Python test starts a disposable loopback demo and supplies a synthetic token.
 * Never run this against a user checkout or with a real credential.
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
  if (!['http:', 'https:'].includes(url.protocol) ||
      !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error("browser test accepts only a loopback demo origin");
  }
  return url;
}

async function puppeteerModule() {
  const candidate = process.env.STUDIO_PUPPETEER_CORE;
  if (!candidate) throw new Error("STUDIO_PUPPETEER_CORE is required");
  const module = await import(pathToFileURL(resolve(candidate)).href);
  return module.default || module;
}

// openOverlay() focuses in the frame after the class lands, because a display:none
// element cannot take focus. A walk that reads activeElement the instant .open appears
// is reading the focus that was there before: on a loaded runner the evaluation wins the
// race and the check fails for a reason that has nothing to do with the product.
async function waitForFocus(page, id, timeout = 5000) {
  try {
    await page.waitForFunction(
      (want) => document.activeElement && document.activeElement.id === want,
      { timeout },
      id,
    );
  } catch (_error) {
    // Nothing to say here: the check that follows reports what the focus actually was,
    // which is a better failure message than a timeout from the wait.
  }
}

function check(name, passed, detail = "") {
  if (!passed) throw new Error(`${name} failed${detail ? `: ${detail}` : ""}`);
  return { name, passed: true, detail };
}

async function main() {
  const { baseUrl, token } = argsOf(process.argv.slice(2));
  const url = checkedDemoUrl(baseUrl);
  if (!/^[A-Za-z0-9._~-]{32,}$/.test(token)) throw new Error("test token must be synthetic and disposable");
  const endpoint = new URL("/api/workspace/directory", url);
  const signal = AbortSignal.timeout(10000);
  const unauthenticated = await fetch(endpoint, { redirect: "manual", signal });
  if (unauthenticated.status !== 401) throw new Error("demo endpoint did not require authentication");
  const authenticated = await fetch(endpoint, {
    redirect: "manual", signal, headers: { Authorization: `Bearer ${token}` },
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
    let firstNavigation = true;
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("dialog", async (dialog) => {
      if (dialog.type() === "prompt") await dialog.accept(token);
      else await dialog.dismiss();
    });
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      try {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {
          const allowed = firstNavigation && request.url() === url.href && request.redirectChain().length === 0;
          firstNavigation = false;
          if (!allowed) { void request.abort(); return; }
        }
        void request.continue();
      } catch { void request.abort().catch(() => {}); }
    });
    // This test harness is origin-bound and top-document-only. Child frames never
    // receive the synthetic bearer token.
    await page.evaluateOnNewDocument((origin, value) => {
      try {
        if (window.top === window && window.location.origin === origin) {
          window.sessionStorage.setItem("m5_api_token", value);
        }
      } catch { /* a browser storage refusal must not fail open */ }
    }, url.origin, token);
    const response = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 30000 });
    if (!response || response.status() !== 200 || !/text\/html/i.test(response.headers()["content-type"] || "") || page.url() !== url.href) {
      throw new Error("isolated demo did not serve a direct successful HTML document");
    }
    await page.waitForSelector("#cmdPaletteModal .modal[role=dialog]", { timeout: 10000 });
    await new Promise((resolve) => setTimeout(resolve, 500));

    // Harness run settings stay collapsed by default, but their current values are
    // visible in the native disclosure summary and remain keyboard-operable.
    const initialRunSettings = await page.evaluate(() => {
      const details = document.getElementById("harnessAdvanced");
      const summary = details?.querySelector("summary");
      const valueSummary = summary?.querySelector("#harnessAdvancedSummary");
      return {
        collapsed: details ? !details.open : false,
        label: summary?.querySelector("span")?.textContent.trim() || "",
        values: valueSummary?.textContent.replace(/\s+/g, " ").trim() || "",
        changed: valueSummary?.dataset.changed || "",
        discipline: document.getElementById("harnessEfficiencyMode")?.value,
        reasoning: document.getElementById("harnessReasoning")?.value,
        steps: document.getElementById("harnessMaxSteps")?.value,
      };
    });
    results.push(check("Harness Run settings summarize current values while collapsed",
      initialRunSettings.collapsed && initialRunSettings.label === "Run settings" &&
      initialRunSettings.values.includes("Ponytail + Concise") && initialRunSettings.values.includes("Low") &&
      initialRunSettings.values.includes("10 steps") && initialRunSettings.changed === "false" &&
      initialRunSettings.discipline === "ponytail+concise" && initialRunSettings.reasoning === "low" &&
      initialRunSettings.steps === "10", JSON.stringify(initialRunSettings)));

    await page.focus("#harnessPolicy");
    await page.keyboard.press("Tab");
    const runSettingsFocus = await page.evaluate(() => {
      const summary = document.querySelector("#harnessAdvanced > summary");
      return {
        focused: document.activeElement === summary,
        visibleRing: summary ? getComputedStyle(summary).outlineStyle === "solid" &&
          Number.parseFloat(getComputedStyle(summary).outlineWidth) >= 2 : false,
      };
    });
    results.push(check("Tab moves from policy to Run settings with a visible focus ring",
      runSettingsFocus.focused && runSettingsFocus.visibleRing, JSON.stringify(runSettingsFocus)));

    await page.keyboard.press("Space");
    const openedRunSettings = await page.evaluate(() => ({
      open: document.getElementById("harnessAdvanced").open,
      focused: document.activeElement === document.querySelector("#harnessAdvanced > summary"),
      fieldsVisible: ["harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps"]
        .every((id) => document.getElementById(id).getClientRects().length > 0),
    }));
    results.push(check("Space opens Run settings and exposes all advanced controls",
      openedRunSettings.open && openedRunSettings.focused && openedRunSettings.fieldsVisible,
      JSON.stringify(openedRunSettings)));

    await page.select("#harnessReasoning", "medium");
    await page.select("#harnessMaxSteps", "16");
    const updatedRunSettings = await page.evaluate(() => ({
      values: document.getElementById("harnessAdvancedSummary").textContent,
      changed: document.getElementById("harnessAdvancedSummary").dataset.changed,
      reasoning: document.getElementById("harnessReasoning").value,
      steps: document.getElementById("harnessMaxSteps").value,
    }));
    results.push(check("Run settings summary follows changed controls without altering their values",
      updatedRunSettings.values.includes("Medium") && updatedRunSettings.values.includes("16 steps") &&
      updatedRunSettings.values.includes("changed") && updatedRunSettings.changed === "true" &&
      updatedRunSettings.reasoning === "medium" && updatedRunSettings.steps === "16",
      JSON.stringify(updatedRunSettings)));

    await page.focus("#harnessAdvanced summary");
    await page.keyboard.press("Space");
    await page.waitForFunction(() => !document.getElementById("harnessAdvanced").open);
    await page.waitForFunction(() => sessionStorage.getItem("m5_harness_advanced") === "closed");
    const closedRunSettings = await page.evaluate(() => ({
      collapsed: !document.getElementById("harnessAdvanced").open,
      focus: document.activeElement === document.querySelector("#harnessAdvanced > summary"),
      preference: sessionStorage.getItem("m5_harness_advanced"),
    }));
    results.push(check("Space closes Run settings and retains its existing disclosure preference",
      closedRunSettings.collapsed && closedRunSettings.focus && closedRunSettings.preference === "closed",
      JSON.stringify(closedRunSettings)));

    // Command palette: shortcut, combobox semantics, active option and focus loop.
    await page.focus("#themeToggleBtn");
    await page.keyboard.down("Control");
    await page.keyboard.press("k");
    await page.keyboard.up("Control");
    await page.waitForFunction(() => document.getElementById("cmdPaletteModal").classList.contains("open"));
    await waitForFocus(page, "cmdSearchInput");
    let palette = await page.evaluate(() => {
      const modal = document.querySelector("#cmdPaletteModal [role=dialog]");
      const input = document.getElementById("cmdSearchInput");
      return {
        open: document.getElementById("cmdPaletteModal").classList.contains("open"),
        modal: modal?.getAttribute("aria-modal"),
        focus: document.activeElement?.id,
        role: input?.getAttribute("role"),
        expanded: input?.getAttribute("aria-expanded"),
        controls: input?.getAttribute("aria-controls"),
        active: input?.getAttribute("aria-activedescendant"),
        focusOutline: input ? getComputedStyle(input).outlineStyle : null,
        focusOutlineWidth: input ? getComputedStyle(input).outlineWidth : null,
      };
    });
    results.push(check("Ctrl+K opens a modal command palette with an accessible combobox",
      palette.open && palette.modal === "true" && palette.focus === "cmdSearchInput" &&
      palette.role === "combobox" && palette.expanded === "true" && palette.controls === "cmdPaletteList" && Boolean(palette.active) &&
      palette.focusOutline === "solid" && Number.parseFloat(palette.focusOutlineWidth) >= 2,
      JSON.stringify(palette)));

    await page.keyboard.press("ArrowDown");
    const selected = await page.evaluate(() => {
      const input = document.getElementById("cmdSearchInput");
      const active = document.getElementById(input.getAttribute("aria-activedescendant"));
      return Boolean(active && active.getAttribute("aria-selected") === "true");
    });
    results.push(check("ArrowDown announces the selected command through aria-activedescendant", selected));
    await page.keyboard.down("Shift");
    await page.keyboard.press("Tab");
    await page.keyboard.up("Shift");
    const wrappedBack = await page.evaluate(() => {
      const dialog = document.querySelector("#cmdPaletteModal [role=dialog]");
      const focusable = [...dialog.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
        .filter((node) => node.getClientRects().length);
      return document.activeElement === focusable.at(-1);
    });
    results.push(check("Shift+Tab wraps focus within the palette", wrappedBack));
    await page.keyboard.press("Tab");
    const wrappedForward = await page.evaluate(() => document.activeElement?.id === "cmdSearchInput");
    results.push(check("Tab wraps focus to the first palette control", wrappedForward));
    await page.keyboard.press("Escape");
    const paletteClosed = await page.evaluate(() => ({
      closed: !document.getElementById("cmdPaletteModal").classList.contains("open"),
      focus: document.activeElement?.id,
      expanded: document.getElementById("cmdSearchInput").getAttribute("aria-expanded"),
    }));
    results.push(check("Escape closes the palette and restores its trigger",
      paletteClosed.closed && paletteClosed.focus === "themeToggleBtn" && paletteClosed.expanded === "false",
      JSON.stringify(paletteClosed)));

    // Nested overlays: close only the topmost dialog, restore into the parent,
    // then restore to the original trigger when the parent closes.
    await page.evaluate(() => {
      document.getElementById("themeToggleBtn").focus();
      window.openContextModal();
    });
    await page.waitForFunction(() => document.getElementById("contextModal").classList.contains("open"));
    await new Promise((resolve) => setTimeout(resolve, 100));
    await page.evaluate(() => window.openMcpModal());
    await page.waitForFunction(() => document.getElementById("contextModal").classList.contains("open") && document.getElementById("mcpModal").classList.contains("open"));
    await new Promise((resolve) => setTimeout(resolve, 100));
    const modalSemantics = await page.evaluate(() => [...document.querySelectorAll(".modal-backdrop.open [role=dialog]")]
      .every((dialog) => dialog.getAttribute("aria-modal") === "true" && dialog.getAttribute("aria-label")));
    results.push(check("every open overlay exposes a named modal dialog", modalSemantics));
    await page.keyboard.press("Escape");
    const topClosed = await page.evaluate(() => ({
      top: !document.getElementById("mcpModal").classList.contains("open"),
      parent: document.getElementById("contextModal").classList.contains("open"),
      focusInsideParent: document.getElementById("contextModal").contains(document.activeElement),
    }));
    results.push(check("Escape closes only the top overlay and restores focus to its parent",
      topClosed.top && topClosed.parent && topClosed.focusInsideParent, JSON.stringify(topClosed)));
    await page.keyboard.press("Escape");
    const parentClosed = await page.evaluate(() => ({
      closed: !document.getElementById("contextModal").classList.contains("open"),
      focus: document.activeElement?.id,
    }));
    results.push(check("closing the parent overlay restores the original focus",
      parentClosed.closed && parentClosed.focus === "themeToggleBtn", JSON.stringify(parentClosed)));

    await page.setViewport({ width: 420, height: 800, deviceScaleFactor: 1 });
    await page.evaluate(() => window.openContextModal());
    const narrowDialog = await page.evaluate(() => {
      const rect = document.querySelector("#contextModal [role=dialog]").getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: rect.width, viewport: innerWidth };
    });
    results.push(check("modal fits the 420px narrow viewport",
      narrowDialog.left >= 0 && narrowDialog.right <= narrowDialog.viewport && narrowDialog.width > 0,
      JSON.stringify(narrowDialog)));
    await page.keyboard.press("Escape");
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });

    // Toasts are named, dismissible, pause while focused and normalize error tone.
    await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
    await page.evaluate(() => window.showToast("Synthetic overlay test notice", "error"));
    const toastInfo = await page.evaluate(() => {
      const stack = document.getElementById("toastStack");
      const toast = stack.querySelector(".toast");
      const close = toast?.querySelector("button[aria-label]");
      return {
        region: stack.getAttribute("role"),
        live: stack.getAttribute("aria-live"),
        label: stack.getAttribute("aria-label"),
        tone: toast?.className,
        closeLabel: close?.getAttribute("aria-label"),
        reducedMotion: getComputedStyle(toast).animationName,
      };
    });
    results.push(check("toast is a named live region with a dismiss control and reduced motion",
      toastInfo.region === "region" && toastInfo.live === "polite" && toastInfo.label === "Notifications" &&
      toastInfo.tone.includes("err") && toastInfo.closeLabel === "Dismiss notification" &&
      toastInfo.reducedMotion === "none", JSON.stringify(toastInfo)));
    const dismissSelector = "#toastStack .toast button[aria-label='Dismiss notification']";
    await page.focus(dismissSelector);
    await new Promise((resolve) => setTimeout(resolve, 5200));
    const toastPersistsWhileFocused = await page.evaluate(() => Boolean(document.querySelector("#toastStack .toast")));
    results.push(check("toast timeout pauses while its dismiss control has focus", toastPersistsWhileFocused));
    await page.keyboard.press("Enter");
    const toastDismissed = await page.evaluate(() => !document.querySelector("#toastStack .toast"));
    results.push(check("toast can be dismissed from the keyboard", toastDismissed));

    await page.evaluate(() => {
      const sessionId = "synthetic-fabricated-session";
      const approvalId = "client-fabricated-approval";
      const event = { type: "approval/request", seq: 1, ts: Date.now() / 1000, step: 1,
        approval_id: approvalId, tool: "run_bash", args: { command: "echo must-not-run" } };
      const requests = [];
      const state = {
        oldActive: activeHarnessId,
        oldSnapshot: harnessSnapshot,
        oldRunActive: harnessRunActive,
        oldFetch: window.fetch,
        oldResolver: resolveHarnessApproval,
        requests,
        approvalPosts: 0,
        executionPosts: 0,
        sessionId,
        approvalId,
        root: document.createElement("div"),
      };
      activeHarnessId = sessionId;
      harnessRunActive = true;
      // The client cache lies: it claims this invented id is still pending.
      harnessSnapshot = { session_id: sessionId, session: { id: sessionId },
        approvals: { pending: [{ approval_id: approvalId, live: true }] } };
      state.root.id = "synthetic-approval-security-walk";
      state.root.innerHTML = renderStoredHarnessEventHtml(event, {}, new Set([approvalId]));
      document.body.appendChild(state.root);
      window.fetch = async (input, init = {}) => {
        const url = String(input);
        requests.push({ url, method: init.method || "GET" });
        if (url.endsWith(`/api/harness/sessions/${sessionId}/state`)) {
          return new Response(JSON.stringify({ session_id: sessionId, session: { id: sessionId },
            status: "waiting_approval", task_status: "running",
            approvals: { pending: [], expired: [{ approval_id: approvalId, tool: "run_bash", step: 1 }] },
            event_stream: { latest_seq: 1, retained_from_seq: 1 } }),
            { status: 200, headers: { "Content-Type": "application/json" } });
        }
        if (url.includes(`/api/harness/sessions/${sessionId}/events?`)) {
          return new Response(JSON.stringify({ events: [event], latest_seq: 1, next_cursor: 1,
            retained_from_seq: 1, has_more: false, snapshot_required: false }),
            { status: 200, headers: { "Content-Type": "application/json" } });
        }
        if (url === "/api/harness/approve") {
          state.approvalPosts += 1;
          return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
        }
        if (url.startsWith("/api/harness/run") || url.includes("/execute")) state.executionPosts += 1;
        return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
      };
      window.__approvalWalkState = state;
      window.__approvalWalkDone = false;
      resolveHarnessApproval = (...args) => {
        const pending = state.oldResolver(...args);
        Promise.resolve(pending).then(
          () => { window.__approvalWalkDone = true; },
          () => { window.__approvalWalkDone = true; },
        );
        return pending;
      };
    });
    const approvalButtonSelector = "#synthetic-approval-security-walk .h-approval-actions button";
    await page.focus(approvalButtonSelector);
    const approvalButtonWasKeyboardFocused = await page.evaluate((selector) => document.activeElement === document.querySelector(selector), approvalButtonSelector);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => window.__approvalWalkDone === true, { timeout: 10000 });
    const fabricatedApproval = await page.evaluate(() => {
      const state = window.__approvalWalkState;
      const originalCard = state.root.querySelector(`#apr-card-${state.approvalId}`);
      const refreshedCard = document.querySelector(`#harnessStream #apr-card-${state.approvalId}`);
      const result = {
        requests: state.requests,
        approvalPosts: state.approvalPosts,
        executionPosts: state.executionPosts,
        keyboardFocused: false,
        allButtonsStayDisabled: [...(originalCard?.querySelectorAll("button") || [])].every((button) => button.disabled),
        refreshedLive: refreshedCard?.dataset.live || "",
        refreshedButtons: refreshedCard?.querySelectorAll("button").length || 0,
        refreshedText: refreshedCard?.textContent.replace(/\s+/g, " ").trim() || "",
      };
      window.fetch = state.oldFetch;
      resolveHarnessApproval = state.oldResolver;
      state.root.remove();
      activeHarnessId = state.oldActive;
      harnessSnapshot = state.oldSnapshot;
      harnessRunActive = state.oldRunActive;
      delete window.__approvalWalkState;
      delete window.__approvalWalkDone;
      return result;
    });
    fabricatedApproval.keyboardFocused = approvalButtonWasKeyboardFocused;
    results.push(check("keyboard-activated fabricated approval cannot authorize execution without a live server pending slot",
      fabricatedApproval.keyboardFocused && fabricatedApproval.approvalPosts === 0 && fabricatedApproval.executionPosts === 0 &&
      fabricatedApproval.requests.some((request) => request.url.endsWith("/state")) &&
      !fabricatedApproval.requests.some((request) => request.url === "/api/harness/approve") &&
      fabricatedApproval.allButtonsStayDisabled && fabricatedApproval.refreshedLive === "false" &&
      fabricatedApproval.refreshedButtons === 0 && /never decided|nothing was granted/i.test(fabricatedApproval.refreshedText),
      JSON.stringify(fabricatedApproval)));
    const hallReadiness = await page.evaluate(async () => {
      const original = {
        telemetry: lastTelemetry,
        telemetryAt: lastTelemetryAt,
        signature: lastCatalogSignature,
        selectors: Object.fromEntries(["hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge", "hallModelSelect"]
          .map((id) => [id, document.getElementById(id).value])),
        role: document.getElementById("hallRoleSelect").value,
        mode: document.getElementById("hallSendModeSelect").value,
        input: document.getElementById("hallHumanInput").value,
        spawnInput: document.getElementById("spawnRequestInput").value,
        hallId: activeHallId,
        hallData: activeHallData,
        hallTitle: document.getElementById("hallTitle").textContent,
        hallStatus: document.getElementById("hallStatusBadge").textContent,
        seatSettingsOpen: document.getElementById("hallSeatsSettings").open,
        singleSettingsOpen: document.getElementById("hallSingleSettings").open,
      };
      if (!original.telemetry) throw new Error("Council readiness walk requires the isolated demo telemetry snapshot");
      const source = JSON.parse(JSON.stringify(original.telemetry));
      const localReady = { id: "M5-Qwen3.8-27B-Local", display_name: "M5-Qwen3.8-27B-Local", role_hint: "Architect", loaded: true, is_loading: false, size_gb: 16.6, pinned: false, is_default: true, model_type: "llm", mtp_enabled: false, oq_a8_enabled: false, ttl_seconds: 120, ttl_remaining_seconds: 120, active_requests: 0, live_tps: 0 };
      const localLoading = { ...localReady, id: "M5-Qwen3.6-Loading", display_name: "M5-Qwen3.6-Loading", loaded: false, is_loading: true };
      const localUnloaded = { ...localReady, loaded: false, is_loading: false, ttl_remaining_seconds: null };
      const cloud = { id: "demo-cloud::council-model", label: "[Cloud • demo-cloud] council-model", group: "Cloud • Demo", source: "cloud", provider: "demo-cloud", size_gb: 0 };
      const combo = { id: "combo/synthetic-review-route", label: "[Combo • Auto-Fallback] Synthetic review route", group: "Smart Fallback Combos", source: "combo", provider: "combo", size_gb: 0 };
      const localCatalog = (model) => ({ id: model.id, label: `[M5 Local • Loaded] ${model.id}`, group: "Local M5 (Loaded in RAM)", source: "local", provider: "omlx", size_gb: model.size_gb, loaded: model.loaded });
      const readyTelemetry = { ...source, models: [localReady, localLoading, localUnloaded], loaded_models: [localReady, localLoading], unloaded_models: [localUnloaded], catalog: [localCatalog(localReady), localCatalog(localLoading), cloud, combo] };
      const unloadedTelemetry = { ...readyTelemetry, models: [localUnloaded], loaded_models: [], unloaded_models: [localUnloaded], catalog: [cloud, combo] };
      const requests = [];
      const originalFetch = window.fetch;
      const originalParticipants = document.getElementById("hallParticipants").innerHTML;
      const originalParticipantsHidden = document.getElementById("hallParticipants").hidden;
      let readyState, emptyState, missingState, loadingState, unloadedState, staleState, participants;
      let emptyRoomEnabled = false;
      let noteOnlyEnabled = false;
      let noteRetained = false;
      try {
        renderTelemetry(readyTelemetry);
        document.getElementById("hallSeatArch").value = localReady.id;
        document.getElementById("hallSeatCoder").value = cloud.id;
        document.getElementById("hallSeatCritic").value = combo.id;
        document.getElementById("hallSeatJudge").value = localReady.id;
        document.getElementById("hallModelSelect").value = localReady.id;
        document.getElementById("hallRoleSelect").value = "architect";
        renderHallReadiness();
        readyState = {
          roles: [...document.querySelectorAll("#hallRoster .hall-seat-role")].map((node) => node.textContent.trim()),
          cards: [...document.querySelectorAll("#hallRoster .hall-seat-card")].map((node) => node.innerText.replace(/\s+/g, " ").trim()),
          fullRoundEnabled: !document.getElementById("hallFullRoundBtn").disabled,
          singleEnabled: !document.getElementById("hallSummonBtn").disabled,
          spawnConveneEnabled: !document.getElementById("spawnConveneBtn").disabled,
          reachabilityDisclosure: document.getElementById("hallReadinessStatus").textContent.includes("not reachability-tested"),
          memoryDisclosure: document.querySelector("#hallSeatsSettings .hall-resource-note")?.textContent || "",
        };

        document.getElementById("hallSeatCoder").value = "";
        renderHallReadiness();
        emptyState = {
          status: document.querySelector("#hallRoster .hall-seat-card:nth-child(2) .hall-seat-state")?.textContent,
          roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
        };
        const missingOption = document.createElement("option");
        missingOption.value = "demo-cloud::removed-model";
        missingOption.textContent = "[Unavailable] demo-cloud::removed-model";
        missingOption.dataset.source = "cloud";
        missingOption.dataset.provider = "demo-cloud";
        document.getElementById("hallSeatCoder").appendChild(missingOption);
        document.getElementById("hallSeatCoder").value = missingOption.value;
        renderHallReadiness();
        missingState = {
          status: document.querySelector("#hallRoster .hall-seat-card:nth-child(2) .hall-seat-state")?.textContent,
          roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
        };
        document.getElementById("hallSeatCoder").removeChild(missingOption);
        document.getElementById("hallSeatCoder").value = cloud.id;
        renderHallReadiness();

        document.getElementById("hallSeatCritic").value = localLoading.id;
        renderHallReadiness();
        loadingState = {
          status: document.querySelector("#hallRoster .hall-seat-card:nth-child(3) .hall-seat-state")?.textContent,
          roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
        };

        document.getElementById("hallSeatCritic").value = combo.id;
        renderHallReadiness();
        renderTelemetry(unloadedTelemetry);
        unloadedState = {
          architectModel: document.getElementById("hallSeatArch").value,
          architectStatus: document.querySelector("#hallRoster .hall-seat-card:nth-child(1) .hall-seat-state")?.textContent,
          judgeStatus: document.querySelector("#hallRoster .hall-seat-card:nth-child(4) .hall-seat-state")?.textContent,
          singleModel: document.getElementById("hallModelSelect").value,
          roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
          singleDisabled: document.getElementById("hallSummonBtn").disabled,
          spawnDisabled: document.getElementById("spawnConveneBtn").disabled,
        };

        lastTelemetryAt = Date.now() - HALL_TELEMETRY_MAX_AGE_MS - 1;
        renderHallReadiness();
        staleState = {
          status: document.querySelector("#hallRoster .hall-seat-card:nth-child(2) .hall-seat-state")?.textContent,
          roundDisabled: document.getElementById("hallFullRoundBtn").disabled,
        };
        document.getElementById("hallSendModeSelect").value = "post_only";
        renderHallReadiness();
        noteOnlyEnabled = !document.getElementById("hallSendBtn").disabled;
        emptyRoomEnabled = ![...document.querySelectorAll("#spawnModal button")]
          .find((button) => button.textContent.includes("Open Empty Room"))?.disabled;

        activeHallId = "synthetic-council-session";
        activeHallData = { title: "Synthetic Council", status: "active", participants: ["Architect (M5-Qwen3.8-27B-Local)", "Human Operative"], context_files: ["docs/brief.md"], messages: [], conclusion_text: "" };
        renderActiveHall();
        participants = {
          shown: !document.getElementById("hallParticipants").hidden,
          text: document.getElementById("hallParticipants").innerText.replace(/\s+/g, " ").trim(),
          seatRolesRemain: document.querySelectorAll("#hallRoster .hall-seat-card").length === 4 &&
            document.querySelector("#hallRoster").textContent.includes("Architect / Thesis"),
        };

        window.fetch = async (input, init = {}) => {
          requests.push({ url: String(input), method: init.method || "GET" });
          throw new Error("Council readiness guard should prevent this API call");
        };
        document.getElementById("hallHumanInput").value = "Keep this note until seats are ready.";
        await confirmSpawnSession(true).catch(() => {});
        await runHallFullRound(false).catch(() => {});
        await summonSingleAgent().catch(() => {});
        await postHumanHallMessage(true).catch(() => {});
        noteRetained = document.getElementById("hallHumanInput").value === "Keep this note until seats are ready.";
      } finally {
        window.fetch = originalFetch;
        activeHallId = original.hallId;
        activeHallData = original.hallData;
        lastCatalogSignature = "";
        if (original.telemetry) renderTelemetry(original.telemetry);
        for (const [id, value] of Object.entries(original.selectors)) {
          const select = document.getElementById(id);
          select.value = value && [...select.options].some((option) => option.value === value) ? value : "";
        }
        document.getElementById("hallRoleSelect").value = original.role;
        document.getElementById("hallSendModeSelect").value = original.mode;
        document.getElementById("hallHumanInput").value = original.input;
        document.getElementById("spawnRequestInput").value = original.spawnInput;
        document.getElementById("hallSeatsSettings").open = original.seatSettingsOpen;
        document.getElementById("hallSingleSettings").open = original.singleSettingsOpen;
        if (original.hallData) renderActiveHall();
        else {
          document.getElementById("hallParticipants").innerHTML = originalParticipants;
          document.getElementById("hallParticipants").hidden = originalParticipantsHidden;
          document.getElementById("hallTitle").textContent = original.hallTitle;
          document.getElementById("hallStatusBadge").textContent = original.hallStatus;
        }
        lastTelemetryAt = original.telemetryAt;
        renderHallReadiness();
        document.getElementById("toastStack").replaceChildren();
      }
      return { readyState, emptyState, missingState, loadingState, unloadedState, staleState, participants, emptyRoomEnabled, noteOnlyEnabled, noteRetained, requests };
    });
    results.push(check("Council roster shows four roles, exact model IDs, source and truthful readiness",
      hallReadiness.readyState.roles.length === 4 && hallReadiness.readyState.roles[0] === "1. Architect / Thesis" &&
      hallReadiness.readyState.roles[3] === "4. Lead Arbiter" &&
      hallReadiness.readyState.cards.join(" ").includes("M5-Qwen3.8-27B-Local") &&
      hallReadiness.readyState.cards.join(" ").includes("demo-cloud::council-model") &&
      hallReadiness.readyState.cards.join(" ").includes("combo/synthetic-review-route") &&
      hallReadiness.readyState.cards.join(" ").includes("Local · oMLX") &&
      hallReadiness.readyState.cards.join(" ").includes("Cloud · demo-cloud") &&
      hallReadiness.readyState.cards.join(" ").includes("Fallback combo") &&
      hallReadiness.readyState.fullRoundEnabled && hallReadiness.readyState.singleEnabled &&
      hallReadiness.readyState.spawnConveneEnabled && hallReadiness.readyState.reachabilityDisclosure &&
      /share this machine’s memory/i.test(hallReadiness.readyState.memoryDisclosure), JSON.stringify(hallReadiness.readyState)));
    results.push(check("Council preflight blocks empty, missing, loading, unloaded and stale assignments without silently replacing their IDs",
      hallReadiness.emptyState.status === "Choose a model" && hallReadiness.emptyState.roundDisabled &&
      hallReadiness.missingState.status === "Unavailable" && hallReadiness.missingState.roundDisabled &&
      hallReadiness.loadingState.status === "Loading" && hallReadiness.loadingState.roundDisabled &&
      hallReadiness.unloadedState.architectModel === "M5-Qwen3.8-27B-Local" &&
      hallReadiness.unloadedState.architectStatus === "Not loaded" && hallReadiness.unloadedState.judgeStatus === "Not loaded" &&
      hallReadiness.unloadedState.singleModel === "M5-Qwen3.8-27B-Local" &&
      hallReadiness.unloadedState.roundDisabled && hallReadiness.unloadedState.singleDisabled && hallReadiness.unloadedState.spawnDisabled &&
      hallReadiness.staleState.status === "Stale telemetry" && hallReadiness.staleState.roundDisabled, JSON.stringify(hallReadiness)));
    results.push(check("Council readiness guards preserve Open Empty Room, Post note only and actual session participants without invoking",
      hallReadiness.emptyRoomEnabled && hallReadiness.noteOnlyEnabled && hallReadiness.noteRetained &&
      hallReadiness.participants.shown && hallReadiness.participants.text.includes("Architect (M5-Qwen3.8-27B-Local)") &&
      hallReadiness.participants.text.includes("Human Operative") && hallReadiness.participants.text.includes("docs/brief.md") &&
      hallReadiness.participants.seatRolesRemain && hallReadiness.requests.length === 0, JSON.stringify(hallReadiness)));

    const narrowPage = await browser.newPage();
    const narrowErrors = [];
    try {
      await narrowPage.setViewport({ width: 420, height: 1200, deviceScaleFactor: 1 });
      narrowPage.on("pageerror", (error) => narrowErrors.push(String(error)));
      await narrowPage.evaluateOnNewDocument((origin, value) => {
        if (window.top === window && window.location.origin === origin) {
          window.sessionStorage.setItem("m5_api_token", value);
        }
      }, url.origin, token);
      const narrowResponse = await narrowPage.goto(url.href, { waitUntil: "domcontentloaded", timeout: 30000 });
      if (!narrowResponse || narrowResponse.status() !== 200 || narrowPage.url() !== url.href) {
        throw new Error("narrow Council demo did not serve a direct successful HTML document");
      }
      await narrowPage.waitForFunction(() => document.querySelectorAll("#hallRoster .hall-seat-card").length === 4, { timeout: 10000 });
      await narrowPage.evaluate(() => switchTab("hall"));
      const narrowHall = await narrowPage.evaluate(() => {
        const pane = document.getElementById("pane-hall");
        const roster = document.getElementById("hallRoster");
        const rect = roster.getBoundingClientRect();
        const cards = [...roster.querySelectorAll(".hall-seat-card")].map((card) => {
          const box = card.getBoundingClientRect();
          return { left: box.left, right: box.right, width: box.width, clientWidth: card.clientWidth, scrollWidth: card.scrollWidth };
        });
        return {
          viewport: innerWidth,
          visible: rect.width > 0 && rect.height > 0,
          columns: getComputedStyle(roster).gridTemplateColumns.split(" ").length,
          cardsFit: cards.length === 4 && cards.every((card) => card.width > 0 && card.left >= rect.left - 1 && card.right <= rect.right + 1),
          cardsNoOverflow: cards.every((card) => card.scrollWidth <= card.clientWidth + 1),
          paneNoOverflow: pane.scrollWidth <= pane.clientWidth + 1,
        };
      });
      results.push(check("Council four-seat roster remains readable in a fresh 420px viewport",
        narrowHall.viewport === 420 && narrowHall.visible && narrowHall.columns === 2 &&
        narrowHall.cardsFit && narrowHall.cardsNoOverflow && narrowHall.paneNoOverflow && narrowErrors.length === 0,
        JSON.stringify({ ...narrowHall, errors: narrowErrors })));
    } finally {
      await narrowPage.close();
    }
    results.push(check("no browser page errors during the overlay and Council readiness walks", errors.length === 0, errors.join("; ")));
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ results, passed: results.length, failed: 0 }, null, 2));
}

main().catch((error) => {
  console.error(`accessibility_walk: ${error.message}`);
  process.exit(1);
});
