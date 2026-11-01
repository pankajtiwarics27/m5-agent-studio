#!/usr/bin/env node
/**
 * Exercise the real Chat pane against an isolated demo server without calling a model.
 *
 * Usage: node scripts/chat_composer_walk.mjs --base-url http://127.0.0.1:PORT --token THROWAWAY_TOKEN [--json]
 * Requires STUDIO_SCREENSHOT_CHROME and STUDIO_PUPPETEER_CORE (same browser as capture_screenshots.mjs).
 * The supplied token must belong to scripts/demo_tour.py --serve; never use a real credential.
 * This walk does not take screenshots. Visual evidence is captured only by capture_screenshots.mjs.
 */
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

function args(argv) {
  const out = { baseUrl: "", token: "", json: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--base-url") out.baseUrl = argv[++i] || "";
    else if (argv[i] === "--token") out.token = argv[++i] || "";
    else if (argv[i] === "--json") out.json = true;
    else if (argv[i] === "--help" || argv[i] === "-h") {
      console.log("Chat interaction regression walk; uses synthetic SSE data and makes no model call.");
      console.log("  node scripts/chat_composer_walk.mjs --base-url http://127.0.0.1:PORT --token THROWAWAY_TOKEN [--json]");
      process.exit(0);
    } else throw new Error(`Unknown argument: ${argv[i]}`);
  }
  if (!out.baseUrl || !out.token) throw new Error("--base-url and --token are required");
  const url = new URL(out.baseUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("base URL must be an HTTP loopback origin for an isolated demo");
  }
  return { ...out, url };
}

async function loadPuppeteer() {
  const candidates = [process.env.STUDIO_PUPPETEER_CORE, "puppeteer-core"];
  const failures = [];
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      const specifier = candidate.startsWith("/") ? pathToFileURL(candidate).href : candidate;
      return await import(specifier);
    } catch (error) { failures.push(`${candidate}: ${error.message}`); }
  }
  throw new Error(`puppeteer-core is not importable. Set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

const options = args(process.argv.slice(2));
const checks = [];
function check(name, condition, detail = "") {
  assert.ok(condition, `${name}${detail ? `: ${detail}` : ""}`);
  checks.push({ name, passed: true, detail });
}

const puppeteer = await loadPuppeteer();
const chrome = process.env.STUDIO_SCREENSHOT_CHROME;
if (!chrome) throw new Error("STUDIO_SCREENSHOT_CHROME is required");
const extraArgs = (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "").split(/\s+/).filter(Boolean);
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage", ...extraArgs] });
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
const pageErrors = [];
const externalRequests = [];
page.on("pageerror", error => pageErrors.push(error.message));
await page.setRequestInterception(true);
page.on("request", request => {
  const requestUrl = new URL(request.url());
  if (requestUrl.origin !== options.url.origin) {
    externalRequests.push(requestUrl.origin);
    void request.abort();
  } else void request.continue();
});
await page.evaluateOnNewDocument((token) => {
  sessionStorage.setItem("m5_api_token", token);
  window.__chatTestMode = "success";
  window.__chatRequests = [];
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (resource, init = {}) => {
    const path = new URL(typeof resource === "string" ? resource : resource.url, location.href).pathname;
    if (path !== "/api/chat/stream") return nativeFetch(resource, init);
    window.__chatRequests.push(JSON.parse(init.body || "{}"));
    const mode = window.__chatTestMode;
    const signal = init.signal;
    const encoder = new TextEncoder();
    const timers = [];
    let controller;
    const event = (name, data) => controller.enqueue(encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`));
    const stream = new ReadableStream({
      start(streamController) {
        controller = streamController;
        const later = (fn, delay) => timers.push(setTimeout(fn, delay));
        if (mode === "success") {
          later(() => event("token", { kind: "content", text: "A verified " }), 80);
          later(() => event("token", { kind: "content", text: "answer." }), 150);
          later(() => event("done", { usage: { completion_tokens: 24, decode_tps: 12.5 } }), 230);
          later(() => controller.close(), 250);
        } else if (mode === "partial-fail") {
          later(() => event("token", { kind: "content", text: "Partial answer retained." }), 50);
          later(() => event("error", { message: "synthetic provider refusal" }), 100);
          later(() => controller.close(), 120);
        } else if (mode === "slow-fail") {
          later(() => event("token", { kind: "content", text: "Partial cleared." }), 25);
          later(() => event("error", { message: "synthetic delayed close" }), 50);
          later(() => controller.close(), 500);
        } else if (mode === "hang") {
          later(() => event("token", { kind: "content", text: "Partial before stop." }), 50);
        } else if (mode === "eof") {
          later(() => event("token", { kind: "content", text: "Unfinalized response." }), 50);
          later(() => controller.close(), 100);
        }
        if (signal) signal.addEventListener("abort", () => {
          for (const timer of timers) clearTimeout(timer);
          try { controller.error(new DOMException("The operation was aborted.", "AbortError")); } catch (_) { /* already closed */ }
        }, { once: true });
      },
      cancel() { for (const timer of timers) clearTimeout(timer); },
    });
    return Promise.resolve(new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
  };
}, options.token);

try {
  const response = await page.goto(options.url.href, { waitUntil: "domcontentloaded", timeout: 15000 });
  assert.equal(response?.status(), 200, "demo document must load directly");
  await page.waitForSelector("#chatMessages .chat-empty-state");
  await page.evaluate(() => switchTab("chat"));

  const empty = await page.evaluate(() => ({
    title: document.querySelector("#chatMessages h2")?.textContent,
    inputLabel: Boolean(document.querySelector('label[for="chatInput"]')),
    modeLabels: ["chatModel", "chatPersona", "chatReasoning"].every(id => document.querySelector(`label[for="${id}"]`)),
    hint: document.getElementById("chatComposerHint")?.textContent || "",
  }));
  check("empty conversation and labelled response mode", empty.title === "No messages yet" && empty.inputLabel && empty.modeLabels, empty.title);
  check("keyboard guidance distinguishes Enter and Shift+Enter", /Enter to send/.test(empty.hint) && /Shift\+Enter/.test(empty.hint), empty.hint);

  await page.select("#chatReasoning", "low");
  await page.select("#chatPersona", "You are an adversarial code reviewer. Focus strictly on bugs, race conditions, UX anti-patterns, and security issues.");
  const input = await page.$("#chatInput");
  await input.focus();
  await page.keyboard.type("desktop keyboard send");
  await page.keyboard.down("Shift");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Shift");
  check("Shift+Enter inserts a newline", /\n$/.test(await input.evaluate(element => element.value)));
  await input.evaluate(element => { element.value = "desktop keyboard send"; });
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('#chatMessages [data-state="streaming"]'));
  const streaming = await page.evaluate(() => ({
    stopVisible: getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
    sendDisabled: document.getElementById("chatSendBtn").disabled,
    busy: document.getElementById("chatMessages").getAttribute("aria-busy"),
  }));
  check("streaming exposes Stop and prevents a duplicate send", streaming.stopVisible && streaming.sendDisabled && streaming.busy === "true");
  await page.waitForFunction(() => [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.dataset.state === "complete" && !document.getElementById("chatSendBtn").disabled);
  const success = await page.evaluate(() => {
    const assistant = [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1);
    const body = window.__chatRequests.at(-1);
    return {
      text: assistant?.textContent || "",
      time: assistant?.querySelector("time")?.getAttribute("datetime"),
      model: assistant?.querySelector(".chat-msg-model")?.textContent,
      metadata: assistant?.querySelector(".chat-msg-footer")?.textContent || "",
      request: body,
    };
  });
  check("success keeps role, model, time and token metadata", /A verified answer\./.test(success.text) && Boolean(success.time && success.model) && /24 tokens/.test(success.metadata) && /12\.5 tok\/s/.test(success.metadata));
  check("existing chat API payload keys and selected mode are preserved", success.request.reasoning_mode === "low" && success.request.messages[0].content.startsWith("You are an adversarial code reviewer.") && ["auto_unload", "messages", "model", "reasoning_mode", "skills", "tools_enabled", "workspace_dir"].every(key => Object.hasOwn(success.request, key)));

  await page.evaluate(() => { window.__chatTestMode = "hang"; });
  await input.evaluate(element => { element.value = "stop this reply"; });
  await page.keyboard.press("Enter");
  await new Promise(resolve => setTimeout(resolve, 250));
  await page.focus("#chatStopBtn");
  await page.click("#chatStopBtn");
  await page.waitForFunction(() => [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.dataset.state === "stopped" && !document.getElementById("chatSendBtn").disabled);
  const stopped = await page.evaluate(() => ({
    text: [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.textContent || "",
    focus: document.activeElement?.id,
    stopVisible: getComputedStyle(document.getElementById("chatStopBtn")).display !== "none",
    busy: document.getElementById("chatMessages").getAttribute("aria-busy"),
  }));
  check("stop retains partial text and restores the composer focus", /Partial before stop\./.test(stopped.text) && stopped.focus === "chatInput" && !stopped.stopVisible && stopped.busy === "false");

  await page.evaluate(() => { window.__chatTestMode = "partial-fail"; });
  await input.evaluate(element => { element.value = "retry without duplication"; });
  await input.focus();
  await page.keyboard.press("Enter");
  await page.waitForSelector('#chatState .presentation[role="alert"]');
  const failure = await page.evaluate(() => ({
    state: [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.dataset.state,
    text: [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.textContent || "",
    actions: [...document.querySelectorAll("#chatState .presentation-actions button")].map(button => button.textContent.trim()),
  }));
  check("partial failure is labelled and offers retry and dismiss", failure.state === "failed" && /Partial answer retained\./.test(failure.text) && failure.actions.join(",") === "Retry,Dismiss");

  await page.evaluate(() => { window.__chatTestMode = "success"; });
  await page.click("#chatState .presentation-actions button:first-child");
  await page.waitForFunction(() => window.__chatRequests.length >= 4 && [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.dataset.state === "complete" && !document.getElementById("chatSendBtn").disabled);
  const retry = await page.evaluate(() => {
    const prompt = "retry without duplication";
    const body = window.__chatRequests.at(-1);
    return {
      visibleUserTurns: [...document.querySelectorAll('#chatMessages [data-role="user"]')].filter(node => node.textContent.includes(prompt)).length,
      userCopiesInRequest: body.messages.filter(message => message.role === "user" && message.content === prompt).length,
    };
  });
  check("Retry reuses one user turn", retry.visibleUserTurns === 1 && retry.userCopiesInRequest === 1);

  await page.evaluate(() => { window.__chatTestMode = "eof"; });
  await input.evaluate(element => { element.value = "incomplete stream"; });
  await page.click("#chatSendBtn");
  await page.waitForSelector('#chatState .presentation[role="alert"]');
  check("unexpected stream close is reported as a recoverable failure", /stream closed/.test(await page.$eval("#chatState .presentation-cause", element => element.textContent)));

  await page.evaluate(() => { window.__chatTestMode = "slow-fail"; });
  await input.evaluate(element => { element.value = "clear while failure finalizes"; });
  await input.focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => [...document.querySelectorAll('#chatMessages [data-role="assistant"]')].at(-1)?.dataset.state === "failed");
  await page.click("#chatClearBtn");
  await page.waitForFunction(() => document.querySelector("#chatMessages .chat-empty-state") && !document.getElementById("chatSendBtn").disabled);
  await new Promise(resolve => setTimeout(resolve, 550));
  const afterClear = await page.evaluate(() => ({
    empty: Boolean(document.querySelector("#chatMessages .chat-empty-state")),
    alerts: document.querySelectorAll('#chatState [role="alert"]').length,
  }));
  check("Clear cancels a pending error presentation", afterClear.empty && afterClear.alerts === 0);

  await page.evaluate(() => clearChat());
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
  const mobile = await page.evaluate(() => {
    const ids = ["chatModel", "chatPersona", "chatReasoning", "chatToolsEnabled", "chatUnload", "chatClearBtn", "chatInput", "chatSendBtn"];
    const boxes = ids.map(id => document.getElementById(id).getBoundingClientRect());
    return { width: innerWidth, documentWidth: document.documentElement.scrollWidth,
      controlsFit: boxes.every(box => box.left >= -1 && box.right <= innerWidth + 1 && box.width > 0),
      sendVisible: boxes.at(-1).width > 0, inputVisible: boxes.at(-2).width > 0 };
  });
  check("390px viewport keeps composer and mode controls in view", mobile.documentWidth <= mobile.width + 1 && mobile.controlsFit && mobile.sendVisible && mobile.inputVisible, `${mobile.documentWidth}px document / ${mobile.width}px viewport`);
  check("no browser error or outbound request", pageErrors.length === 0 && externalRequests.length === 0, pageErrors.join("; ") || externalRequests.join(", "));
} finally {
  await browser.close();
}

const report = { passed: checks.length, failed: 0, checks };
console.log(options.json ? JSON.stringify(report, null, 2) : checks.map(item => `PASS ${item.name}${item.detail ? ` — ${item.detail}` : ""}`).join("\n"));
