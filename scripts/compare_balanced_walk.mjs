#!/usr/bin/env node
/**
 * Check that Compare is a question first, with the answers in matching columns (UIV2-16).
 *
 *   node scripts/compare_balanced_walk.mjs --screenshots docs/media/compare-balanced
 *
 * The pane stacked a four-row criteria table above the question, so at 1440x900 the fold held a
 * table and the thing being compared was under it; the answers landed in whatever columns
 * `auto-fit` happened to make, so two replies could be different widths and heights; a single
 * model looked exactly like a comparison whose second column failed to fill; and the criteria
 * that the run settles sat in the same table as the ones only the reader can judge.
 *
 * What it proves:
 *
 *  1. the question and the model picks are the composer, and the composer comes before anything
 *     that explains the answers;
 *  2. the question is fully above the fold at 1640x900, 1440x900, 1280x800 and 390x844 - the
 *     criteria panel is folded, so it cannot push the question off the screen;
 *  3. the folded summary states how many criteria each group holds, so folding it hides nothing
 *     a reader needs;
 *  4. opening it shows three groups - settled by the run, not settled here, yours to judge - with
 *     their own rails and colour, drawn apart rather than interleaved;
 *  5. the P4-04 behaviour survives: a criterion the reader adds opens the panel, focuses the
 *     input, is listed as theirs to judge, and can be removed;
 *  6. one model is stated as a single answer, not as a comparison, and it is drawn full width;
 *  7. two models are two columns of the same width and the same height, side by side;
 *  8. a long reply scrolls inside its own card instead of stretching the row;
 *  9. at 390x844 the columns stack, and nothing overflows sideways;
 * 10. the summary is reachable from the keyboard and toggles when pressed;
 * 11. the pane raises no console error, and posts only the debate stream.
 *
 * Usage:
 *
 *   node scripts/compare_balanced_walk.mjs                       # starts its own fixture
 *   node scripts/compare_balanced_walk.mjs --base-url URL --token TOKEN
 *   node scripts/compare_balanced_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *
 * The fixture is `scripts/council_readiness_seed.py --resident-model ...`: one model loaded, one
 * resident, so two panels can answer the same question through the stand-in runtime.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";

const REQUEST =
  "Compare SQLite WAL vs embedded Postgres for a local telemetry store: which would you pick and why?";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
  viewport: "1440x900",
  theme: "dark",
};

function parseArgs(argv) {
  const args = { ...DEFAULTS, json: false, captureOnly: false };
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
      case "--screenshots": args.screenshots = next(); break;
      case "--viewport": args.viewport = next(); break;
      case "--theme": args.theme = next(); break;
      case "--json": args.json = true; break;
      case "--capture-only": args.captureOnly = true; break;
      case "--help":
      case "-h":
        console.log("usage: node scripts/compare_balanced_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--json] [--capture-only]");
        process.exit(0);
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

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
  ];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (!found) {
    throw new Error("no Chromium found: set STUDIO_SCREENSHOT_CHROME to one");
  }
  return found;
}

async function loadPuppeteer() {
  const attempts = [
    process.env.STUDIO_PUPPETEER_CORE,
    "puppeteer-core",
    "puppeteer",
    resolve("tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js"),
  ].filter(Boolean);
  const failures = [];
  for (const attempt of attempts) {
    try {
      const module = await import(attempt);
      return module.default || module;
    } catch (error) {
      failures.push(`${attempt}: ${error.message}`);
    }
  }
  throw new Error(`puppeteer-core is not importable. Install it or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
}

async function waitForHttp(url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.status < 500) return true;
    } catch (error) { /* not up yet */ }
    await sleep(150);
  }
  return false;
}

async function startFixture() {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", "Qwen3.6-35B-REAP-19B", "--port", "0"],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)), 90000);
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
  return { child, ready };
}

function readPane(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const box = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return { top: Math.round(rect.top), left: Math.round(rect.left), width: Math.round(rect.width), height: Math.round(rect.height), bottom: Math.round(rect.bottom) };
    };
    const prompt = document.getElementById("debatePrompt");
    const composer = document.getElementById("compareComposer");
    const details = document.getElementById("debateCriteriaDetails");
    const panel = document.getElementById("debateCriteria");
    const grid = document.getElementById("debateGrid");
    const note = document.getElementById("debateColumnsNote");
    const groups = [...document.querySelectorAll("#debateCriteria .debate-criteria-group")].map((group) => ({
      kind: group.dataset.kind || "",
      head: clean(group.querySelector(".debate-criteria-group-head") ? group.querySelector(".debate-criteria-group-head").textContent : ""),
      rows: [...group.querySelectorAll("tbody tr")].map((row) => clean(row.textContent)),
      rail: getComputedStyle(group).borderLeftColor,
      width: getComputedStyle(group).borderLeftWidth,
    }));
    const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => ({
      status: card.dataset.status || "",
      model: clean(card.querySelector(".stage-output-head strong") ? card.querySelector(".stage-output-head strong").textContent : ""),
      box: box(card),
      body: box(card.querySelector(".stage-output-body")),
      bodyScrolls: Boolean(card.querySelector(".stage-output-body"))
        && card.querySelector(".stage-output-body").scrollHeight > card.querySelector(".stage-output-body").clientHeight + 1,
      maxBody: card.querySelector(".stage-output-body") ? getComputedStyle(card.querySelector(".stage-output-body")).maxHeight : "",
    }));
    return {
      paneOpen: Boolean(document.querySelector("#pane-debate.active, #pane-debate[aria-hidden='false']")),
      innerHeight: window.innerHeight,
      innerWidth: window.innerWidth,
      overflowX: document.documentElement.scrollWidth - window.innerWidth,
      prompt: box(prompt),
      composer: box(composer),
      detailsOpen: Boolean(details && details.open),
      panelBox: box(panel),
      summaryText: clean(document.querySelector(".debate-criteria-summary") ? document.querySelector(".debate-criteria-summary").textContent : ""),
      counts: clean(document.getElementById("debateCriteriaCounts") ? document.getElementById("debateCriteriaCounts").textContent : ""),
      groups,
      chosen: clean(document.getElementById("debateModelsChosen") ? document.getElementById("debateModelsChosen").textContent : ""),
      hint: clean(document.getElementById("debateRunHint") ? document.getElementById("debateRunHint").textContent : ""),
      startDisabled: Boolean(document.getElementById("debateRunBtn") && document.getElementById("debateRunBtn").disabled),
      checked: [...document.querySelectorAll(".debate-m-chk:checked")].map((box2) => box2.value),
      options: [...document.querySelectorAll(".debate-m-chk")].map((box2) => box2.value),
      gridCount: grid ? grid.dataset.count || "" : "",
      cols: grid ? getComputedStyle(grid).gridTemplateColumns : "",
      styleCols: grid ? grid.style.getPropertyValue("--debate-cols") : "",
      cards,
      noteHidden: note ? note.hidden : true,
      noteText: clean(note ? note.textContent : ""),
      summary: clean(document.getElementById("debateSummary") ? document.getElementById("debateSummary").textContent : ""),
      inspector: clean(document.getElementById("monitorRail") ? document.getElementById("monitorRail").textContent : ""),
    };
  });
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const record = (step, passed, detail) => {
    const ok = Boolean(passed) || args.captureOnly;
    results.push({ step, passed: Boolean(passed), detail: detail || "", status: ok ? "pass" : "fail" });
    if (!args.json) console.log(`${ok ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  const [width, height] = args.viewport.split("x").map(Number);
  const shots = [];
  const shoot = async (page, name, focus, block = "start") => {
    if (!args.screenshots) return "";
    mkdirSync(args.screenshots, { recursive: true });
    const path = resolve(args.screenshots, `${name}.png`);
    await page.evaluate((selector, align) => {
      const host = selector ? document.querySelector(selector) : null;
      if (host && host.scrollIntoView) host.scrollIntoView({ block: align, inline: "nearest" });
    }, focus || "#compareComposer", block);
    await sleep(args.settleMs * 2);
    await page.screenshot({ path });
    shots.push(path);
    return path;
  };

  const fixture = args.baseUrl ? null : await startFixture();
  const baseUrl = args.baseUrl || fixture.ready.base_url;
  const token = args.token || (fixture && fixture.ready.token) || "";
  const origin = new URL(baseUrl).origin;
  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" ").filter(Boolean),
  });
  const pageErrors = [];
  const posts = [];
  const posts2 = [];
  try {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error" && !/status of 40[34]/.test(message.text())) pageErrors.push(message.text()); });
    page.on("request", (request) => { if (request.method() === "POST") posts.push(new URL(request.url()).pathname); });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await page.evaluateOnNewDocument((key, value, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, value);
      localStorage.setItem("m5_theme", theme);
    }, args.tokenKey, token, args.theme, origin);
    await page.goto(origin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 10);
    await page.evaluate(() => switchTab("debate"));
    await sleep(args.settleMs * 8);
    const read = () => readPane(page);

    // ── 1-2: the question is the first thing, and it is above the fold ─────────
    const first = await read();
    record(
      "the question and the picks are the composer, and the composer comes first",
      Boolean(first.composer && first.prompt)
        && first.composer.top < (await page.evaluate(() => {
          const details = document.getElementById("debateCriteriaDetails");
          return details ? Math.round(details.getBoundingClientRect().top) : 0;
        }))
        && (await page.evaluate(() => {
          const pane = document.getElementById("pane-debate");
          const composer = document.getElementById("compareComposer");
          const firstRegion = pane ? pane.querySelector(".scroll-body > *") : null;
          return Boolean(firstRegion && composer && firstRegion.contains(composer));
        })),
      `composer top=${first.composer ? first.composer.top : "missing"} · prompt top=${first.prompt ? first.prompt.top : "missing"}`,
    );
    record(
      "the question is fully above the fold",
      Boolean(first.prompt) && first.prompt.bottom <= first.innerHeight,
      `prompt bottom=${first.prompt ? first.prompt.bottom : "?"} of ${first.innerHeight}`,
    );

    // ── 3: the criteria are folded, and the fold states what it hides ──────────
    record(
      "the criteria panel is folded, so it cannot push the question down",
      first.detailsOpen === false
        && (!first.panelBox || first.panelBox.height === 0)
        && Boolean(first.summaryText && first.summaryText.length < 160),
      `open=${first.detailsOpen} panelHeight=${first.panelBox ? first.panelBox.height : "none"} summary="${first.summaryText.slice(0, 90)}"`,
    );
    record(
      "the folded summary counts what each group holds",
      /3 settled by the run/.test(first.counts) && /1 not settled here/.test(first.counts) && /2 yours to judge/.test(first.counts),
      first.counts,
    );
    await shoot(page, `compare-collapsed-${args.viewport}-${args.theme}`);

    // ── 4: opening it draws the three groups apart ─────────────────────────────
    await page.evaluate(() => { document.getElementById("debateCriteriaDetails").open = true; });
    await sleep(args.settleMs * 2);
    const opened = await read();
    const kinds = opened.groups.map((group) => group.kind);
    record(
      "the criteria open into three named groups, in the order the run reads them",
      kinds.join(",") === "measured,not-stated,judged"
        && opened.groups.map((group) => group.rows.length).join(",") === "3,1,2",
      `${kinds.join(",")} · rows ${opened.groups.map((group) => group.rows.length).join(",")}`,
    );
    record(
      "what the run settles and what the reader judges are drawn apart",
      new Set(opened.groups.map((group) => group.rail)).size === 3
        && opened.groups[0].head.startsWith("Settled by the run")
        && opened.groups[2].head.startsWith("Yours to judge")
        && opened.groups[0].rows.every((row) => /after each panel reports/.test(row))
        && opened.groups[2].rows.every((row) => /yours to judge/i.test(row)),
      opened.groups.map((group) => `${group.kind}:${group.rail}`).join(" · "),
    );

    // ── 5: the P4-04 behaviour survives the fold ───────────────────────────────
    await page.evaluate(() => { document.getElementById("debateCriteriaDetails").open = false; });
    await sleep(args.settleMs);
    const added = await page.evaluate(async () => {
      const details = document.getElementById("debateCriteriaDetails");
      details.open = true;
      const input = document.getElementById("debateCriterionInput");
      input.value = "Runs on my machine";
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await new Promise((done) => setTimeout(done, 200));
      const active = document.activeElement;
      return {
        open: details.open,
        rows: [...document.querySelectorAll("#debateCriteria .debate-criteria-group[data-kind='judged'] tbody tr")].length,
        listed: [...document.querySelectorAll("#debateCriteria tbody tr")].some((row) => /Runs on my machine/.test(row.textContent) && /yours to judge/i.test(row.textContent)),
        focused: Boolean(active && active.id === "debateCriterionInput"),
        counts: (document.getElementById("debateCriteriaCounts") || {}).textContent || "",
      };
    });
    record(
      "a criterion the reader adds opens the panel, stays focused, and is counted",
      added.open && added.listed && added.focused && added.rows === 3 && /3 yours to judge/.test(added.counts),
      `${added.rows} judged rows · focused=${added.focused} · counts="${String(added.counts).trim()}"`,
    );
    const afterAdd = await read();
    record(
      "the inspector's criteria counts follow the panel that changed",
      /Criteria\s*7(?!\d)/.test(afterAdd.inspector) && /Yours to judge\s*3(?!\d)/.test(afterAdd.inspector),
      afterAdd.inspector.slice(0, 120),
    );
    const removed = await page.evaluate(async () => {
      const row = [...document.querySelectorAll("#debateCriteria tbody tr")].find((candidate) => /Runs on my machine/.test(candidate.textContent));
      const button = row ? row.querySelector("button") : null;
      if (button) button.click();
      await new Promise((done) => setTimeout(done, 200));
      return (document.getElementById("debateCriteriaCounts") || {}).textContent || "";
    });
    record(
      "and removing it puts the count back",
      /2 yours to judge/.test(removed),
      String(removed).trim(),
    );
    const afterRemove = await read();
    record(
      "and the inspector is back to six criteria, two of them the reader's",
      /Criteria\s*6(?!\d)/.test(afterRemove.inspector) && /Yours to judge\s*2(?!\d)/.test(afterRemove.inspector),
      afterRemove.inspector.slice(0, 120),
    );
    await shoot(page, `compare-criteria-open-${args.viewport}-${args.theme}`, "#debateCriteriaDetails");

    // ── 6: the keyboard can open and close the summary ─────────────────────────
    const keyboard = await page.evaluate(async () => {
      const details = document.getElementById("debateCriteriaDetails");
      details.open = false;
      const summary = details.querySelector("summary");
      summary.focus();
      const focused = document.activeElement === summary;
      summary.click();
      await new Promise((done) => setTimeout(done, 120));
      const openedByClick = details.open;
      details.open = false;
      summary.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
      await new Promise((done) => setTimeout(done, 120));
      return { focused, openedByClick, nativeEnter: details.open };
    });
    record(
      "the summary is reachable from the keyboard and toggles when pressed",
      keyboard.focused && keyboard.openedByClick,
      `focused=${keyboard.focused} press opened=${keyboard.openedByClick} (Enter is the element's own behaviour: ${keyboard.nativeEnter})`,
    );
    await page.evaluate(() => { document.getElementById("debateCriteriaDetails").open = false; });

    // ── 7: one model is stated as a single answer ──────────────────────────────
    const single = await page.evaluate(async () => {
      const boxes = [...document.querySelectorAll(".debate-m-chk")];
      // The one-model state is a local model, so the capture matches the two-model one.
      const first = boxes.find((box) => !box.value.includes("::") && !box.value.startsWith("combo/")) || boxes[0];
      boxes.forEach((box) => { box.checked = box === first; });
      document.getElementById("debatePrompt").value = document.getElementById("debatePrompt").value.trim();
      updateDebateRunControl();
      await new Promise((done) => setTimeout(done, 200));
      const note = document.getElementById("debateColumnsNote");
      return {
        chosen: (document.getElementById("debateModelsChosen") || {}).textContent || "",
        count: document.getElementById("debateGrid").dataset.count,
        noteHidden: note.hidden,
        note: (note.textContent || "").trim(),
        cols: document.getElementById("debateGrid").style.getPropertyValue("--debate-cols"),
      };
    });
    record(
      "one model is stated as a single answer, not as a comparison",
      /single answer/.test(single.note) && single.noteHidden === false && single.count === "1" && single.cols === "1",
      `note="${single.note.slice(0, 90)}" count=${single.count} cols=${single.cols}`,
    );
    await shoot(page, `compare-one-model-${args.viewport}-${args.theme}`, "#debateColumnsNote", "center");

    // ── 8-9: two models, one run, matching columns ─────────────────────────────
    // The pair has to be two models the fixture really serves. Falling back to a cloud row would
    // photograph a failed transport and call it a comparison - which is what this walk first did.
    const locals = await page.evaluate(() => [...document.querySelectorAll(".debate-m-chk")]
      .map((box) => box.value)
      .filter((value) => !value.includes("::") && !value.startsWith("combo/")));
    record(
      "the fixture serves two local models, so a comparison is two real answers",
      locals.length >= 2,
      locals.length ? locals.join(" + ") : "no local model in the picker",
    );
    const picked = await page.evaluate(async () => {
      const boxes = [...document.querySelectorAll(".debate-m-chk")];
      const locals = boxes.filter((box) => !box.value.includes("::") && !box.value.startsWith("combo/"));
      const pair = locals.slice(0, 2);
      boxes.forEach((box) => { box.checked = pair.includes(box); });
      if (document.getElementById("d-synth")) {
        document.getElementById("d-synth").checked = false;
        syncDebateArbiter();
      }
      updateDebateRunControl();
      await new Promise((done) => setTimeout(done, 200));
      return {
        values: pair.map((box) => box.value),
        chosen: (document.getElementById("debateModelsChosen") || {}).textContent || "",
        hint: (document.getElementById("debateRunHint") || {}).textContent || "",
        disabled: document.getElementById("debateRunBtn").disabled,
        cols: document.getElementById("debateGrid").style.getPropertyValue("--debate-cols"),
      };
    });
    record(
      "two models are stated as a comparison, and Start is ready",
      picked.values.length === 2 && /2 models selected/.test(picked.chosen) && picked.disabled === false && picked.cols === "2",
      `${picked.values.join(" + ")} · "${String(picked.hint).trim()}"`,
    );
    await page.evaluate(() => { runDebate(); });
    const started = Date.now();
    let done = null;
    while (Date.now() - started < 90000) {
      await sleep(500);
      done = await page.evaluate(() => {
        const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")];
        const statuses = cards.map((card) => card.dataset.status || "");
        return {
          count: cards.length,
          statuses,
          summary: (document.getElementById("debateSummary") || {}).textContent || "",
          settled: cards.length >= 2 && statuses.every((status) => !["queued", "running"].includes(status)),
        };
      });
      if (done.count >= 2 && done.settled) break;
    }
    const ran = await read();
    // A comparison that failed is not the state this story is about: the capture has to show two
    // answers, so the walk refuses to pass on two failed panels.
    record(
      "both panels answered, so the capture is a comparison and not a failure",
      ran.cards.length === 2
        && ran.cards.every((card) => card.status === "answered" || card.status === "done")
        && /2 answered/.test(ran.summary),
      `${ran.cards.map((card) => `${card.model.slice(0, 24)}:${card.status}`).join(" · ")} · summary "${ran.summary}"`,
    );
    // Two answers in a row are the same width and the same height; where the viewport cannot hold
    // two columns they stack at the same width instead, and that is the point of this check there.
    const twoUp = ran.innerWidth >= 900;
    const sameWidth = ran.cards.length === 2 && Math.abs(ran.cards[0].box.width - ran.cards[1].box.width) <= 1;
    const sameRow = ran.cards.length === 2 && ran.cards[0].box.top === ran.cards[1].box.top
      && ran.cards[0].box.left !== ran.cards[1].box.left;
    const sameHeight = ran.cards.length === 2 && Math.abs(ran.cards[0].box.height - ran.cards[1].box.height) <= 1;
    const stacked = ran.cards.length === 2 && ran.cards[0].box.left === ran.cards[1].box.left
      && ran.cards[0].box.top !== ran.cards[1].box.top;
    record(
      twoUp
        ? "two answers land in two columns of the same width, top and height"
        : "two answers stack at one width when the viewport cannot hold two columns",
      sameWidth && (twoUp ? sameHeight && sameRow : stacked),
      `${ran.cards.map((card) => `${card.box.width}x${card.box.height}@${card.box.left},${card.box.top}`).join(" · ")} (viewport ${ran.innerWidth})`,
    );
    record(
      "a long reply scrolls inside its own card instead of stretching the row",
      ran.cards.every((card) => card.maxBody && card.maxBody !== "none" && card.body && card.body.height <= card.box.height),
      ran.cards.map((card) => `${card.model.slice(0, 20)} body=${card.body ? card.body.height : "?"} card=${card.box.height} max=${card.maxBody}`).join(" · "),
    );
    record(
      "the one-model note is gone once there is something to compare",
      ran.noteHidden === true && ran.cards.length === 2 && /panels?/.test(ran.summary),
      `noteHidden=${ran.noteHidden} summary="${ran.summary.slice(0, 70)}"`,
    );
    await shoot(page, `compare-two-models-${args.viewport}-${args.theme}`, "#debateSummary");

    // ── 10: narrow columns stack and nothing overflows ─────────────────────────
    await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 4);
    const narrow = await read();
    record(
      "at 390x844 the answers stack in one column and nothing overflows sideways",
      narrow.overflowX <= 0
        && (narrow.cards.length < 2 || narrow.cards[0].box.left === narrow.cards[1].box.left)
        && narrow.prompt && narrow.prompt.bottom <= narrow.innerHeight,
      `overflowX=${narrow.overflowX} cards=${narrow.cards.map((card) => `${card.box.width}x${card.box.top}`).join(",")}`,
    );
    // The narrow state is 390x844 whatever the run's viewport is, so it is named for what it shows.
    await shoot(page, `compare-narrow-390x844-${args.theme}`, "#debateSummary");
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 2);

    // ── 11: nothing else was posted, and nothing threw ────────────────────────
    const unexpected = posts2.length ? posts2 : posts;
    record(
      "the pane posts only the debate stream",
      [...new Set(unexpected)].every((path) => path === "/api/debate/stream"),
      [...new Set(unexpected)].join(", ") || "no POST",
    );
    record("no uncaught error is raised by any of it", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  } finally {
    await browser.close();
    if (fixture) fixture.child.kill("SIGTERM");
  }

  const passed = results.filter((result) => result.passed).length;
  if (args.json) {
    console.log(JSON.stringify({ results, passed, total: results.length, screenshots: shots }, null, 2));
  } else {
    console.log(`\n${passed}/${results.length} checks passed · ${shots.length} screenshots${args.captureOnly ? " (capture-only: failures not counted)" : ""}`);
  }
  process.exitCode = results.every((result) => result.passed) || args.captureOnly ? 0 : 1;
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(`compare_balanced_walk: ${error.message}`);
  process.exitCode = 1;
});
