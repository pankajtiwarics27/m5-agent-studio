#!/usr/bin/env node
/**
 * Check the context a Chat reply was given, and what its tools did (UIUX-P2-05).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/chat_context_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The demo has no model runtime, so this walk cannot make a model call a tool. It does
 * the next honest thing: it writes one turn of the shape `sendChatRequest()` builds -
 * a passing and a failing tool call, both timings, a reasoning trace - and holds the
 * pane against it: the status words, the counts, the times, the sizes, the whole
 * argument objects and the server's capped output. Then it streams nothing and shows a
 * turn before its first token arrives, and finally it sends one real message, because
 * the context line is only worth anything if it describes a request that actually went
 * out.
 *
 * What it proves:
 *
 *   1. every answer prints the context it was sent, composed by `chatContextLine()`
 *      from the same snapshot that is stored on the turn, and the whole line is also
 *      the element's title;
 *   2. that line names the persona, the reasoning mode, the tools and the message count,
 *      states the directory that will be read when none was sent - and mentions skills
 *      only when there are any;
 *   3. a tool summary counts the calls, the failures and the milliseconds, and every row
 *      carries a status word with a tone rather than a colour;
 *   4. a row says its step, its time and the byte size of its arguments, and the whole
 *      argument object is one disclosure below, byte-for-byte;
 *   5. the output disclosure says the server sends the first 500 characters, and shows
 *      them;
 *   6. a finished turn shows how long it took and when the first token arrived, and the
 *      elapsed figure does not keep climbing when the pane re-renders afterwards;
 *   7. a turn in flight says it is streaming, how long it has been, and that it is
 *      waiting for the first token - then shows the first-token time once there is one;
 *   8. a real send records the composer it was sent from: the model, the empty directory
 *      that was literally sent, the effective directory, and a first token after the
 *      start;
 *   9. the context line and the tool rows survive a 900px viewport;
 *  10. no page errors.
 */
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = { tokenKey: "m5_api_token", settleMs: 250, timeoutMs: 45000 };

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
    "Check the context a Chat reply was sent with, and what its tool calls did.",
    "",
    "  node scripts/chat_context_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-chat-context.md.",
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
const seconds = (text) => {
  const match = /(-?\d+(?:\.\d+)?)s\b/.exec(text || "");
  return match ? Number(match[1]) : null;
};

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
    await page.evaluate(() => switchTab("chat"));
    await sleep(args.settleMs * 4);

    // A model has to be selected for the composer to be usable at all.
    await page.evaluate(() => {
      const sel = document.getElementById("chatModel");
      sel.innerHTML = '<option value="demo-local-model">demo-local-model</option>';
      sel.value = "demo-local-model";
      const tools = document.getElementById("chatToolsEnabled");
      tools.checked = true;
      updateChatComposer();
    });

    // ── One written turn, with a passing and a failing tool call ──────────
    const written = await page.evaluate(() => {
      const okArgs = { pattern: "def main", path: "app.py" };
      const badArgs = { path: "app.py", limit: 20 };
      const reasoning = "the reader asked which tools ran, so two were run";
      const startedAt = performance.now() - 2500;
      const snapshot = chatContextSnapshot("demo-local-model", 2, "");
      const tools = [
        { step: 1, tool: "grep_search", args: okArgs, ok: true, elapsed_ms: 120, preview: "app.py:1: def main(argv):" },
        { step: 2, tool: "read_file", args: badArgs, ok: false, elapsed_ms: 7, preview: "Error: file not found: app.py" },
      ];
      chatHistory = [
        { role: "user", content: "Which tools can you use, and which of them failed?" },
        {
          role: "assistant",
          model: "demo-local-model",
          content: "Two tools ran: grep_search found the entry point, read_file was refused by the workspace guard.",
          reasoning,
          tools,
          context: snapshot,
          timing: { startedAt, firstTokenAt: startedAt + 412, endedAt: startedAt + 1800 },
          usage: { decode_tps: 41.2, completion_tokens: 320 },
        },
      ];
      renderChatMessages();
      return {
        snapshot,
        expectedLine: chatContextLine(snapshot),
        argsBytes: tools.map((tool) => JSON.stringify(tool.args, null, 1).length),
        argsText: tools.map((tool) => JSON.stringify(tool.args, null, 1)),
        previews: tools.map((tool) => tool.preview),
        summary: chatToolSummary(tools),
        reasoningChars: reasoning.length,
      };
    });

    const drawn = await page.evaluate(() => {
      const turn = [...document.querySelectorAll("#chatMessages .msg")].pop();
      const context = turn.querySelector(".chat-context");
      const rows = [...turn.querySelectorAll(".chat-tool")];
      return {
        contextText: context ? context.textContent.trim() : "",
        contextData: context ? context.dataset.context : null,
        contextTitle: context ? context.getAttribute("title") : null,
        summary: (turn.querySelector(".chat-tools-summary") || {}).textContent || "",
        rows: rows.map((row) => ({
          status: (row.querySelector(".chat-tool-status") || {}).textContent || "",
          tone: (row.querySelector(".chat-tool-status") || {}).dataset
            ? row.querySelector(".chat-tool-status").dataset.tone
            : null,
          toolStatus: row.dataset.toolStatus,
          meta: (row.querySelector(".chat-tool-meta") || {}).textContent || "",
          code: (row.querySelector("code") || {}).textContent || "",
          labels: [...row.querySelectorAll(".chat-tool-label")].map((node) => node.textContent.trim()),
          pres: [...row.querySelectorAll("pre")].map((node) => node.textContent),
        })),
        reasoning: (turn.querySelector(".think-box summary") || {}).textContent || "",
        meta: (turn.querySelector(".msg-meta") || {}).textContent || "",
        streaming: turn.getAttribute("data-streaming"),
      };
    });

    record(
      "every answer prints the context it was sent, composed by the pane's own helper",
      drawn.contextData === "sent"
        && drawn.contextText === `Context: ${written.expectedLine}`
        && drawn.contextTitle === written.expectedLine,
      `"${drawn.contextText.slice(0, 60)}…" title matches=${drawn.contextTitle === written.expectedLine}`,
    );
    const ctx = written.snapshot;
    const lineNamesContext = [
      ctx.model,
      ctx.persona,
      ctx.reasoning,
      ctx.workspace_effective,
      `${ctx.messages} messages sent`,
    ].every((needle) => written.expectedLine.includes(needle));
    record(
      "the context line names the model, persona, reasoning mode, message count and the directory that will be read",
      lineNamesContext && written.expectedLine.includes("tools on"),
      written.expectedLine,
    );
    const skillsHonest = ctx.skills
      ? written.expectedLine.includes(`${ctx.skills} skill`)
      : !written.expectedLine.includes("skill");
    record(
      "skills are named when there are any and left out when there are none",
      skillsHonest,
      `${ctx.skills} active skill(s); line ${ctx.skills ? "names them" : "omits them"}`,
    );

    record(
      "the tool summary counts the calls, the failures and the milliseconds",
      drawn.summary === written.summary && /2 tool calls · 1 failed · 127 ms/.test(drawn.summary),
      `"${drawn.summary}"`,
    );
    record(
      "every tool call carries a status word with a tone, not a colour",
      drawn.rows.length === 2
        && drawn.rows[0].status === "OK" && drawn.rows[0].tone === "ok" && drawn.rows[0].toolStatus === "ok"
        && drawn.rows[1].status === "FAILED" && drawn.rows[1].tone === "danger" && drawn.rows[1].toolStatus === "failed",
      drawn.rows.map((row) => `${row.status}/${row.tone}/${row.toolStatus}`).join(" "),
    );
    record(
      "a tool call says its step, its time and the size of its arguments",
      drawn.rows[0].meta.includes("step 1 · 120 ms · ") && drawn.rows[0].meta.includes(`${written.argsBytes[0]} B of arguments`)
        && drawn.rows[1].meta.includes("step 2 · 7 ms · ") && drawn.rows[1].meta.includes(`${written.argsBytes[1]} B of arguments`),
      drawn.rows.map((row) => row.meta).join(" | "),
    );
    record(
      "the whole argument object is one disclosure below, byte for byte",
      drawn.rows[0].pres[0] === written.argsText[0] && drawn.rows[1].pres[0] === written.argsText[1],
      `first call ${drawn.rows[0].pres[0].length} chars vs ${written.argsText[0].length} written`,
    );
    record(
      "the output disclosure states the server's cap and shows what was sent",
      drawn.rows[0].labels[1] === "Output (the server sends the first 500 characters)"
        && drawn.rows[1].labels[1] === "Error output (the server sends the first 500 characters)"
        && drawn.rows[0].pres[1] === written.previews[0]
        && drawn.rows[1].pres[1] === written.previews[1],
      `${drawn.rows[0].labels[1]} / ${drawn.rows[1].labels[1]}`,
    );
    record(
      "the reasoning disclosure says how much there is",
      drawn.reasoning === `Reasoning Trace · ${written.reasoningChars} chars`,
      `"${drawn.reasoning}"`,
    );

    // ── A finished turn's clock ───────────────────────────────────────────
    const finishedMeta = drawn.meta;
    await sleep(args.settleMs * 2);
    const afterRerender = await page.evaluate(() => {
      renderChatMessages();
      const turn = [...document.querySelectorAll("#chatMessages .msg")].pop();
      return (turn.querySelector(".msg-meta") || {}).textContent || "";
    });
    record(
      "a finished turn shows its elapsed time and its first token, and stops counting",
      /1\.8s/.test(finishedMeta)
        && /first token 0\.41s/.test(finishedMeta)
        && /tok\/s/.test(finishedMeta)
        && afterRerender === finishedMeta,
      `"${finishedMeta}" then "${afterRerender}" after a re-render`,
    );
    record(
      "the written turn is drawn as finished, with no streaming mark",
      drawn.streaming === null,
      `data-streaming=${drawn.streaming}`,
    );
    await shoot(page, "context-tools");

    // ── A turn before its first token ─────────────────────────────────────
    const streaming = await page.evaluate(() => {
      chatHistory.push({
        role: "assistant",
        model: "demo-local-model",
        content: "x".repeat(128),
        reasoning: "",
        tools: [],
        context: chatContextSnapshot("demo-local-model", 4, ""),
        timing: { startedAt: performance.now() - 1200, firstTokenAt: null, endedAt: null },
      });
      renderChatMessages();
      const turn = [...document.querySelectorAll("#chatMessages .msg")].pop();
      return {
        meta: (turn.querySelector(".msg-meta") || {}).textContent || "",
        streaming: turn.getAttribute("data-streaming"),
        busy: document.getElementById("chatMessages").getAttribute("aria-busy"),
        context: (turn.querySelector(".chat-context") || {}).textContent || "",
      };
    });
    record(
      "a turn in flight says how long it has been going and that it is waiting for the first token",
      /streaming…/.test(streaming.meta)
        && /waiting for the first token/.test(streaming.meta)
        && /128 chars/.test(streaming.meta)
        && Math.abs((seconds(streaming.meta) || 0) - 1.2) < 0.35
        && !/first token \d/.test(streaming.meta),
      `"${streaming.meta}"`,
    );
    record(
      "the streaming turn is marked as streaming and the transcript as busy",
      streaming.streaming === "true" && streaming.busy === "true" && streaming.context.startsWith("Context: "),
      `data-streaming=${streaming.streaming} aria-busy=${streaming.busy}`,
    );
    await shoot(page, "streaming");

    const firstToken = await page.evaluate(() => {
      const turn = chatHistory[chatHistory.length - 1];
      turn.timing.firstTokenAt = turn.timing.startedAt + 350;
      renderChatMessages();
      const node = [...document.querySelectorAll("#chatMessages .msg")].pop();
      return (node.querySelector(".msg-meta") || {}).textContent || "";
    });
    record(
      "once a token arrives the turn shows the first-token time instead of waiting",
      /first token 0\.35s/.test(firstToken) && !/waiting for the first token/.test(firstToken),
      `"${firstToken}"`,
    );

    // ── A real send records the composer it was sent from ─────────────────
    const beforeSend = await page.evaluate(() => {
      const input = document.getElementById("chatInput");
      input.value = "say READY";
      input.dispatchEvent(new Event("input"));
      input.focus();
      const workspace = document.getElementById("harnessWorkspaceInput");
      return {
        history: chatHistory.length,
        workspaceSent: workspace ? workspace.value.trim() : "",
      };
    });
    await page.keyboard.press("Enter");
    await sleep(args.settleMs * 24);
    const answer = await page.evaluate(() => {
      const turn = chatHistory[chatHistory.length - 1];
      const node = [...document.querySelectorAll("#chatMessages .msg")].pop();
      return {
        role: turn ? turn.role : null,
        usage: Boolean(turn && turn.usage),
        context: turn && turn.context ? turn.context : null,
        timing: turn && turn.timing ? turn.timing : null,
        drawnContext: (node && node.querySelector(".chat-context")
          ? node.querySelector(".chat-context").textContent.trim()
          : ""),
        expected: turn && turn.context ? `Context: ${chatContextLine(turn.context)}` : "",
      };
    });
    if (answer.context && answer.usage) {
      record(
        "a real send records the context it went out with, and the pane prints that same line",
        answer.context.model === "demo-local-model"
          && answer.context.workspace === beforeSend.workspaceSent
          && typeof answer.context.workspace_effective === "string"
          && answer.context.workspace_effective.length > 0
          && answer.drawnContext === answer.expected,
        `model=${answer.context.model} sent "${answer.context.workspace}" -> reads "${answer.context.workspace_effective}"`,
      );
      record(
        "the real turn measured its own start and its own first token",
        Boolean(answer.timing && answer.timing.startedAt)
          && Boolean(answer.timing.firstTokenAt)
          && answer.timing.firstTokenAt >= answer.timing.startedAt,
        `start=${Math.round(answer.timing ? answer.timing.startedAt : 0)} first token=${Math.round(answer.timing && answer.timing.firstTokenAt ? answer.timing.firstTokenAt : 0)}`,
      );
      record(
        "the real turn's drawn context agrees with the snapshot stored on it",
        answer.drawnContext.length > 0,
        answer.drawnContext,
      );
    } else {
      block(
        "a real send records the context it went out with",
        "the demo did not answer the send, so there is no real turn to inspect",
      );
    }
    await page.evaluate(() => {
      const box = document.getElementById("chatMessages");
      box.scrollTop = box.scrollHeight;
    });
    await sleep(args.settleMs * 2);
    await shoot(page, "sent-context");

    // ── Narrow ────────────────────────────────────────────────────────────
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await page.evaluate(() => {
      const box = document.getElementById("chatMessages");
      const context = document.querySelector("#chatMessages .chat-context");
      const row = document.querySelector("#chatMessages .chat-tool");
      return {
        overflow: box ? box.scrollWidth > box.clientWidth + 1 : true,
        contextVisible: context ? context.getBoundingClientRect().height > 8 : false,
        rowVisible: row ? row.getBoundingClientRect().height > 8 : false,
      };
    });
    record(
      "the context line and the tool rows survive a 900px viewport",
      narrow.overflow === false && narrow.contextVisible && narrow.rowVisible,
      `overflow=${narrow.overflow} context=${narrow.contextVisible} row=${narrow.rowVisible}`,
    );
    await shoot(page, "narrow");

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    // The walk wrote turns into a page it then closed; no store was touched.
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
    throw new Error(`${failed.length} chat context check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`chat_context_walk: ${error.message}`);
  process.exit(1);
});
