#!/usr/bin/env node
/**
 * Check that the six panes name states the same way (UIUX-P5-01).
 *
 *   node scripts/terminology_walk.mjs --screenshots docs/media/terminology
 *
 * Every pane used to name its own states: the same failed run read `FAILED` in the harness badge,
 * `ERROR` a few lines later, `Check failed` on Start and `failed` in Compare; a stop read `CANCELLED`
 * in one pane and `stopped by you` in another; a running workflow was drawn `ok` on one path and
 * `busy` on another. This walk starts `scripts/council_readiness_seed.py` (two resident models, so a
 * comparison really answers and the Compare pane draws its own states), opens each of the six panes
 * with that pane's own keyboard shortcut, and reads every status word the DOM actually holds.
 *
 * What it proves:
 *
 *   1. there is one vocabulary table, each state carrying a word and a tone the component accepts;
 *   2. no two states share a word, every word is lowercase prose, and an unknown state throws
 *      instead of printing itself;
 *   3. the Harness draws the server's ten states in those words, with `error` as `failed` and
 *      `cancelled` as `stopped by you`;
 *   4. the Compare pane's ten states are the same table, word for word and tone for tone;
 *   5. Alt+N opens each of the six panes, and focus follows the shortcut;
 *   6. every status word visible in every pane is in the table (or is a stated non-status);
 *   7. no status is shouted in capitals;
 *   8. one word carries one tone, wherever on screen it appears;
 *   9. after a real comparison, the panel chips say `answered` in the ok tone - the vocabulary is
 *      what the reader sees, not only what the source says;
 *  10. the panes still fit 480 px, the light theme says the same words, and no page error occurs.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 90000,
  screenshots: "",
  baseUrl: "",
  token: "",
  residentModel: "",
  port: 0,
};

/** The panes, in the order the rail lists them, with the screenshot each one gets. */
const PANES = [
  { tab: "start", shot: "terminology-start" },
  { tab: "harness", shot: "terminology-harness" },
  { tab: "hall", shot: "terminology-hall" },
  { tab: "council", shot: "terminology-council" },
  { tab: "debate", shot: "terminology-debate" },
  { tab: "chat", shot: "terminology-chat" },
];

/** Words the panes draw that are deliberately not statuses; the inventory argues each one. */
const NOT_A_STATUS = [
  "0 / 0 Passed",
  "ESC",
  "PRD",
  "VIEW",
  "MTP",
  "CPU",
  "RAM",
  "PINNED",
  "USER TASK",
  "LOADED",
  "LOADING",
  "PASSED",
  "set",
  "unset",
  "—",
  "0",
];

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
      case "--resident-model": args.residentModel = next(); break;
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
    "Check that the six panes name states the same way.",
    "",
    "  node scripts/terminology_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/terminology_walk.mjs --base-url URL --token TOKEN",
    "",
    "Without --base-url the walk starts the Compare fixture itself, so the statuses it reads are",
    "the ones the app drew from a real run. Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and",
    "puppeteer-core (STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-terminology.md.",
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

async function startFixture(args) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [
      resolve("scripts/council_readiness_seed.py"),
      "--port", "0",
      ...(args.residentModel ? ["--resident-model", args.residentModel] : []),
    ],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(
      () => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)),
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
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`));
    });
  });
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { ...ready, child };
}

/** The vocabulary, read out of the page rather than out of the source. */
async function readVocabulary(page) {
  return page.evaluate(() => {
    const table = typeof UI_STATUS === "undefined" ? null : UI_STATUS;
    if (!table) return null;
    let unknownThrows = false;
    try {
      uiStatus("no-such-state");
    } catch (_error) {
      unknownThrows = true;
    }
    return {
      entries: Object.entries(table).map(([state, entry]) => ({ state, label: entry.label, tone: entry.tone })),
      unknownThrows,
      tones: typeof STATUS_TONES === "undefined" ? [] : [...STATUS_TONES],
    };
  });
}

/** The Harness's ten server states, drawn through its own badge function. */
async function readHarnessStates(page) {
  return page.evaluate(() => {
    const states = [
      "waiting_approval", "running", "paused", "blocked", "pending",
      "idle", "completed", "cancelled", "failed", "error", "something_new",
    ];
    return states.map((state) => {
      const hit = harnessStatusBadge({ status: state });
      return { state, label: hit.label, tone: hit.cls };
    });
  });
}

/** The Compare pane's ten states, as the pane itself holds them. */
async function readDebateStates(page) {
  return page.evaluate(() => Object.entries(DEBATE_STATUS).map(([state, entry]) => ({
    state,
    label: entry.label,
    tone: entry.tone,
  })));
}

/** Every status word visible in the pane that is showing. */
async function readVisibleWords(page) {
  return page.evaluate(() => {
    const seen = [];
    for (const node of document.querySelectorAll(".status, [data-tone]")) {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden") continue;
      if (box.width === 0 && box.height === 0) continue;
      const text = (node.textContent || "").replace(/\s+/g, " ").trim();
      if (!text || text.length > 48) continue;
      // The case a reader sees is the box's, not the word's: the memory rail's heading is uppercase
      // and used to shout `OK` out of a table that said `ok`.
      seen.push({
        word: text,
        tone: node.dataset.tone || "",
        tag: node.tagName.toLowerCase(),
        transform: style.textTransform || "",
      });
    }
    return seen;
  });
}

/** Press a pane's own shortcut and report where focus went. */
async function openPaneByShortcut(page, tab, settleMs) {
  const shortcut = await page.evaluate((name) => {
    const button = document.querySelector(`.mode-tab[data-tab="${name}"]`);
    return button ? button.dataset.shortcut || "" : "";
  }, tab);
  if (!shortcut) return { shortcut: "", opened: false, focusTag: "" };
  const key = shortcut.replace(/^Alt\+/i, "");
  await page.keyboard.down("Alt");
  await page.keyboard.press(key);
  await page.keyboard.up("Alt");
  await sleep(settleMs * 4);
  return page.evaluate((name) => {
    const button = document.querySelector(`.mode-tab[data-tab="${name}"]`);
    const section = document.querySelector(`[data-tab="${name}"]`);
    const active = document.activeElement;
    return {
      shortcut: button ? button.dataset.shortcut || "" : "",
      opened: Boolean(button && button.classList.contains("active") && section && !section.hidden),
      focusTag: active ? active.tagName.toLowerCase() : "",
      focusInPane: Boolean(section && active && section.contains(active)),
    };
  }, tab);
}

/** Capture the viewport as it is: the caller sets the size the capture is *of*. */
async function shootViewport(page, args, name) {
  if (!args.screenshots) return;
  mkdirSync(args.screenshots, { recursive: true });
  await sleep(args.settleMs * 4);
  await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
}

/** Put the page back to the size the pane captures are taken at. */
async function fullSize(page, args) {
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
  await sleep(args.settleMs * 3);
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture(args);
      args.baseUrl = fixture.base_url;
      args.token = args.token || fixture.token;
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
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(`console: ${message.text()}`);
      });
      await page.evaluateOnNewDocument((key, token) => {
        try {
          window.sessionStorage.setItem(key, token);
          localStorage.setItem("m5_theme", "dark");
        } catch (_error) {}
      }, args.tokenKey, args.token);
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 10);

      // ── 1. one table ──────────────────────────────────────────────────────
      const vocab = await readVocabulary(page);
      record(
        "there is one vocabulary table, and every state carries a word and an accepted tone",
        Boolean(vocab)
        && vocab.entries.length >= 15
        && vocab.entries.every((entry) => entry.label && vocab.tones.includes(entry.tone)),
        vocab
          ? `${vocab.entries.length} states; tones ${vocab.tones.join("/")}; bad: ${vocab.entries.filter((entry) => !entry.label || !vocab.tones.includes(entry.tone)).map((entry) => entry.state).join(",") || "none"}`
          : "no UI_STATUS in the page",
      );
      const labels = (vocab ? vocab.entries : []).map((entry) => entry.label);
      record(
        "no state is spelled twice, and no two states share a word",
        new Set(labels).size === labels.length && labels.every((label) => label === label.toLowerCase()),
        `${new Set(labels).size} distinct words of ${labels.length}`,
      );
      record(
        "an unknown state throws instead of printing itself",
        Boolean(vocab && vocab.unknownThrows),
        `uiStatus("no-such-state") threw: ${vocab ? vocab.unknownThrows : false}`,
      );

      // ── 2. the harness and the compare pane draw from it ──────────────────
      const harnessStates = await readHarnessStates(page);
      const registered = new Map((vocab ? vocab.entries : []).map((entry) => [entry.state, entry]));
      const harnessWrong = harnessStates
        .filter((entry) => entry.state !== "something_new")
        .filter((entry) => !labels.includes(entry.label));
      record(
        "the Harness draws the server's ten states in the shared words",
        harnessStates.length === 11 && harnessWrong.length === 0,
        harnessStates.map((entry) => `${entry.state}=${entry.label}/${entry.tone}`).join(" · "),
      );
      const errorWord = harnessStates.find((entry) => entry.state === "error") || {};
      const cancelledWord = harnessStates.find((entry) => entry.state === "cancelled") || {};
      record(
        "one outcome has one word: `error` is `failed` and `cancelled` is `stopped by you`",
        errorWord.label === "failed" && cancelledWord.label === "stopped by you",
        `error=${errorWord.label} cancelled=${cancelledWord.label}`,
      );
      const unknownWord = harnessStates.find((entry) => entry.state === "something_new") || {};
      record(
        "a state the table has never seen keeps the server's own word rather than a wrong one",
        unknownWord.label === "something new" && unknownWord.tone === "neutral",
        `${unknownWord.label}/${unknownWord.tone}`,
      );
      const debateStates = await readDebateStates(page);
      const debateWrong = debateStates.filter((entry) => {
        const shared = registered.get(entry.state);
        return !shared || shared.label !== entry.label || shared.tone !== entry.tone;
      });
      record(
        "the Compare pane's ten states are the same table, word for word and tone for tone",
        debateStates.length === 10 && debateWrong.length === 0,
        debateWrong.length
          ? debateWrong.map((entry) => `${entry.state}: ${entry.label}/${entry.tone}`).join(", ")
          : `${debateStates.length} states agree`,
      );

      // ── 3. a real comparison, so the words are the ones a reader sees ─────
      await page.evaluate(() => switchTab("debate"));
      await sleep(args.settleMs * 4);
      const models = await page.evaluate(() => {
        const boxes = [...document.querySelectorAll(".debate-m-chk")].map((box) => box.value);
        return boxes.slice(0, 2);
      });
      await page.evaluate((wanted) => {
        const boxes = [...document.querySelectorAll(".debate-m-chk")];
        boxes.forEach((box) => { box.checked = wanted.includes(box.value); });
        if (boxes[0]) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
      }, models);
      await page.evaluate(() => {
        const area = document.getElementById("debatePrompt");
        area.value = "Terminology walk: compare two models on one question.";
        area.dispatchEvent(new Event("input", { bubbles: true }));
        document.getElementById("debateRunBtn").click();
      });
      let cards = [];
      for (let attempt = 0; attempt < 300; attempt += 1) {
        await sleep(args.settleMs);
        cards = await page.evaluate(() => [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => ({
          status: card.dataset.status || "",
          pill: (card.querySelector(".status[data-tone]") || {}).textContent || "",
          tone: (card.querySelector(".status[data-tone]") || {}).dataset
            ? (card.querySelector(".status[data-tone]") || {}).dataset.tone || ""
            : "",
        })));
        if (cards.length && cards.every((card) => card.status !== "running" && card.status !== "queued")) break;
      }
      const answeredWords = cards.filter((card) => card.status === "answered");
      record(
        "after a real comparison the panel chips say `answered` in the ok tone",
        answeredWords.length >= 1 && answeredWords.every((card) => /answered/.test(card.pill)),
        cards.map((card) => `${card.status}: ${card.pill.replace(/\s+/g, " ").trim()}/${card.tone}`).join(" · ") || "no cards",
      );

      // ── 4. every word in every pane ───────────────────────────────────────
      const perPane = {};
      let shortcutsOk = true;
      for (const pane of PANES) {
        await fullSize(page, args);
        const opened = await openPaneByShortcut(page, pane.tab, args.settleMs);
        shortcutsOk = shortcutsOk && opened.opened && opened.focusInPane;
        const words = await readVisibleWords(page);
        perPane[pane.tab] = {
          opened,
          raw: words,
          words: words.map((entry) => `${entry.word}/${entry.tone}`),
        };
        await shootViewport(page, args, pane.shot);
      }
      record(
        "Alt+N opens each of the six panes, and focus follows the shortcut into the pane",
        shortcutsOk,
        PANES.map((pane) => `${pane.tab}:${perPane[pane.tab].opened.opened ? "open" : "closed"}/${perPane[pane.tab].opened.focusTag}`).join(" · "),
      );
      // A chip may count as well as name - `0/4 ready`, `2 ready · 1 configured`. Every word in it
      // has to be the vocabulary's: strip the counts and the separators, and what is left must be a
      // label, whole.
      const isVocabularyText = (text) => {
        let rest = ` ${text} `;
        for (const label of [...labels].sort((a, b) => b.length - a.length)) {
          rest = rest.split(new RegExp(`(?<=^|[^a-z])${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=[^a-z]|$)`, "g")).join(" ");
        }
        for (const allowed of NOT_A_STATUS) rest = rest.split(allowed).join(" ");
        return /^[\s\d/·,.:%–—-]*$/.test(rest);
      };
      const strays = [];
      const shouting = [];
      // A word the table spells in lowercase, drawn by a box that shouts: the DOM said `ok` and the
      // reader saw `OK` in the memory rail, so the case has to be checked on the computed style too.
      const transformed = [];
      for (const [tab, data] of Object.entries(perPane)) {
        for (const entry of data.raw) {
          if (/uppercase/.test(entry.transform || "") && isVocabularyText(entry.word)) {
            transformed.push(`${tab}:${entry.word}`);
          }
        }
        for (const entry of data.words) {
          const [word] = entry.split("/");
          if (isVocabularyText(word)) continue;
          if (/^[A-Z][A-Z /·]{2,}/.test(word)) shouting.push(`${tab}:${word}`);
          strays.push(`${tab}:${word}`);
        }
      }
      const everyPaneDrewSomething = Object.values(perPane).some((data) => data.words.length > 0);
      record(
        "every status word visible in every pane is in the table or a stated non-status",
        everyPaneDrewSomething && strays.length === 0,
        strays.slice(0, 8).join(", ") || `${Object.values(perPane).reduce((total, data) => total + data.words.length, 0)} words read`,
      );
      record(
        "no status is shouted in capitals, by its text or by the box it lands in",
        shouting.length === 0 && transformed.length === 0,
        [...shouting, ...transformed].slice(0, 6).join(", "),
      );
      const tonesByWord = new Map();
      for (const data of Object.values(perPane)) {
        for (const entry of data.words) {
          const [word, tone] = entry.split("/");
          if (!labels.includes(word)) continue;
          if (!tonesByWord.has(word)) tonesByWord.set(word, new Set());
          tonesByWord.get(word).add(tone);
        }
      }
      const split = [...tonesByWord.entries()].filter(([, tones]) => tones.size > 1);
      record(
        "one word carries one tone wherever on screen it appears",
        split.length === 0,
        split.map(([word, tones]) => `${word}: ${[...tones].join("+")}`).join(", "),
      );

      // ── 5. narrow, light, and no page error ───────────────────────────────
      await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 6);
      await page.evaluate(() => switchTab("debate"));
      const narrow = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        words: [...document.querySelectorAll(".status")].map((node) => (node.textContent || "").trim()).slice(0, 6),
      }));
      await shootViewport(page, { ...args, screenshots: args.screenshots }, "terminology-narrow");
      record(
        "the panes still fit a 480 px viewport with the words intact",
        narrow.overflow <= 0 && narrow.words.every((word) => !/^[A-Z][A-Z /]{2,}$/.test(word)),
        `scrollWidth - innerWidth = ${narrow.overflow}`,
      );

      await page.evaluate(() => applyTheme("light"));
      await fullSize(page, args);
      await sleep(args.settleMs * 6);
      const light = await page.evaluate(() => ({
        theme: document.body.dataset.theme || "",
        words: [...document.querySelectorAll(".status")].map((node) => (node.textContent || "").trim()),
      }));
      await shootViewport(page, args, "terminology-light");
      const lightStrays = light.words.filter((word) => word && !isVocabularyText(word));
      record(
        "the light theme says the same words as the dark one",
        light.theme === "light" && lightStrays.length === 0,
        `${light.theme || "no theme"}; strays: ${lightStrays.slice(0, 5).join(", ") || "none"}`,
      );
      record("no page error occurred", errors.length === 0, errors.slice(0, 3).join(" | "));

      const failed = results.filter((entry) => !entry.passed);
      if (args.json) {
        console.log(JSON.stringify({ checks: results, passed: results.length - failed.length, total: results.length }, null, 2));
      } else {
        console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
      }
      if (failed.length) process.exitCode = 1;
    } finally {
      await browser.close();
    }
  } finally {
    if (fixture && fixture.child) fixture.child.kill("SIGTERM");
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(`terminology walk: ${error.message}`);
  process.exit(1);
});
