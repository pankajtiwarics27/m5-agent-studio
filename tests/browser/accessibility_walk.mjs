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

    // Command palette: shortcut, combobox semantics, active option and focus loop.
    await page.focus("#themeToggleBtn");
    await page.keyboard.down("Control");
    await page.keyboard.press("k");
    await page.keyboard.up("Control");
    await page.waitForFunction(() => document.getElementById("cmdPaletteModal").classList.contains("open"));
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
      window.openMcpModal();
    });
    await page.waitForFunction(() => document.getElementById("contextModal").classList.contains("open") && document.getElementById("mcpModal").classList.contains("open"));
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
    results.push(check("no browser page errors during the overlay walk", errors.length === 0, errors.join("; ")));
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ results, passed: results.length, failed: 0 }, null, 2));
}

main().catch((error) => {
  console.error(`accessibility_walk: ${error.message}`);
  process.exit(1);
});
