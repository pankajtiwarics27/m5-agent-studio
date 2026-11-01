#!/usr/bin/env node
/**
 * Check the Harness's durable timeline and event replay in a browser (UIUX-P3-02).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/harness_timeline_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * The demo has no model runtime, so no turn can run. The events this walk reads are still
 * the real thing: the app writes `session/init` when a session is created and
 * `security/policy_revision` when its policy changes, and the walk creates both through the
 * API and then watches the pane, the log and the trajectory agree about them. Nothing here
 * is a hand-made event.
 *
 * What it proves:
 *
 *   1. the log strip states what the view holds (count, seq range, cursor) before the
 *      trajectory is read;
 *   2. the strip is a disclosure: closed it states the count, open it lists one row per
 *      event, oldest first, each row a button carrying its seq;
 *   3. every retained event draws in the trajectory - including `session/init` and
 *      `security/policy_revision`, which used to render as nothing - so the number of
 *      cards equals the number of held events;
 *   4. a row points at its card: the card is brought into view and marked;
 *   5. Jump to latest appears only when the reader is not at the newest event, and it is
 *      what returns them;
 *   6. an event that arrives on the stream grows the log, the trajectory and the strip
 *      together, without a reload;
 *   7. Replay re-reads the log from its first retained event and rebuilds the view from it
 *      with no duplicates, and the strip counts the replays;
 *   8. a frame that lands while the reader is scrolled up does not move them;
 *   9. the strip says when the log holds more than the view shows, when earlier events
 *      were pruned, and when the view came from a re-read rather than the stream;
 *  10. no page error occurs and the strip survives a 900px viewport.
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
    "Check the Harness's durable log strip, its replay and its jump-to-latest control.",
    "",
    "  node scripts/harness_timeline_walk.mjs --base-url URL --token TOKEN [--json] [--screenshots DIR]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-harness-timeline.md.",
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

async function readTimeline(page) {
  return page.evaluate(() => {
    const host = document.getElementById("harnessTimeline");
    const jump = document.getElementById("harnessTimelineJump");
    const toggle = document.getElementById("harnessTimelineToggle");
    const list = document.getElementById("harnessTimelineList");
    const stream = document.getElementById("harnessStream");
    const events = (typeof harnessCachedEvents === "function") ? harnessCachedEvents() : [];
    const cards = [...stream.querySelectorAll(".h-event")];
    return {
      hidden: host.hidden,
      mode: host.dataset.mode,
      count: document.getElementById("harnessTimelineCount").textContent.trim(),
      state: document.getElementById("harnessTimelineState").textContent.trim(),
      stateMode: document.getElementById("harnessTimelineState").dataset.mode,
      expanded: toggle.getAttribute("aria-expanded"),
      controls: toggle.getAttribute("aria-controls"),
      listHidden: list.hidden,
      rows: [...list.querySelectorAll("button.harness-timeline-row")].map((row) => ({
        seq: Number(row.dataset.seq),
        type: row.dataset.type,
        text: row.textContent.replace(/\s+/g, " ").trim(),
      })),
      jumpHidden: jump.hidden,
      jumpDrawn: jump.getBoundingClientRect().height > 0 && getComputedStyle(jump).display !== "none",
      cards: cards.map((card) => ({
        seq: Number(card.dataset.seq),
        type: card.dataset.type,
        text: card.textContent.replace(/\s+/g, " ").trim(),
      })),
      held: events.length,
      cursor: typeof harnessCursor === "number" ? harnessCursor : null,
      scrollTop: stream.scrollTop,
      scrollHeight: stream.scrollHeight,
      clientHeight: stream.clientHeight,
      atBottom: stream.scrollHeight - stream.scrollTop - stream.clientHeight <= 2,
    };
  });
}

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
  const shoot = async (page, name) => {
    if (!args.screenshots) return;
    mkdirSync(args.screenshots, { recursive: true });
    await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
  };
  const waitForCount = async (page, expected, timeoutMs = 8000) => {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const state = await readTimeline(page);
      if (state.held === expected && state.cards.length === expected) return state;
      await sleep(120);
    }
    return readTimeline(page);
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
    await page.evaluate(() => switchTab("harness"));
    await sleep(args.settleMs * 4);

    // ── A session whose log the app itself wrote ──────────────────────────
    const created = await page.evaluate(async () => {
      const response = await fetch("/api/harness/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "P3-02 walk", policy: "standard" }),
      });
      const body = await response.json();
      return { status: response.status, id: (body.session || {}).id || "" };
    });
    record(
      "the app creates a session, which is what writes the log this walk reads",
      created.status === 200 && /^hns-/.test(created.id),
      `id=${created.id}`,
    );
    await page.evaluate((id) => selectHarnessSession(id), created.id);
    await sleep(args.settleMs * 8);
    // The stream is closed for the first reading so the held sentence, not the live one,
    // is the thing under test; it is reopened below for the live test.
    await page.evaluate(async () => {
      await closeHarnessEventStream();
      harnessLogTelemetry.source = "log";
      harnessLogTelemetry.latest = Number(harnessLogTelemetry.latest || harnessCursor);
      renderHarnessTimeline();
      const toggle = document.getElementById("harnessTimelineToggle");
      if (toggle.getAttribute("aria-expanded") !== "true") toggleHarnessTimeline();
    });
    await sleep(args.settleMs * 2);

    const first = await readTimeline(page);
    record(
      "the log strip is present for a session and states the count, the seq range and the cursor",
      first.hidden === false
        && first.count === "1 event"
        && /^1 event held, seq 1-1 · cursor 1 of 1$/.test(first.state),
      `count="${first.count}" state="${first.state}"`,
    );
    record(
      "the strip is a disclosure that says what it lists, and it lists one row per event",
      first.expanded === "true"
        && first.controls === "harnessTimelineList"
        && first.listHidden === false
        && first.rows.length === 1
        && first.rows[0].seq === 1
        && first.rows[0].type === "session/init",
      `rows=${JSON.stringify(first.rows.map((row) => `${row.seq}:${row.type}`))}`,
    );
    record(
      "the row reads the event in words instead of its field names",
      /policy/.test(first.rows[0].text) && /lead/.test(first.rows[0].text),
      first.rows[0].text,
    );
    record(
      "every held event draws a card, so session/init is no longer a blank trajectory",
      first.cards.length === first.held
        && first.cards.length === 1
        && first.cards[0].type === "session/init"
        && /session\/init/.test(first.cards[0].text),
      `cards=${first.cards.length} held=${first.held} card="${first.cards[0] ? first.cards[0].text.slice(0, 70) : ""}"`,
    );
    await shoot(page, "log-open");

    // ── A row points at its card ───────────────────────────────────────────
    const pointed = await page.evaluate(() => {
      const row = document.querySelector('#harnessTimelineList button[data-seq="1"]');
      row.click();
      const card = document.querySelector('#harnessStream .h-event[data-seq="1"]');
      const target = card.getBoundingClientRect();
      const box = document.getElementById("harnessStream").getBoundingClientRect();
      return {
        highlight: card.dataset.highlight,
        focused: document.activeElement === card,
        inView: target.top >= box.top - 2 && target.bottom <= box.bottom + 2,
      };
    });
    record(
      "a log row brings its own card into view and marks it",
      pointed.highlight === "true" && pointed.focused === true && pointed.inView === true,
      `highlight=${pointed.highlight} focused=${pointed.focused} inView=${pointed.inView}`,
    );

    // ── The log grows on the stream, one policy change at a time ───────────
    const live = await page.evaluate(async () => {
      openHarnessEventStream();
      const policies = ["plan", "autonomous"];
      for (let i = 0; i < 12; i += 1) {
        await fetch(`/api/harness/sessions/${encodeURIComponent(activeHarnessId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy: policies[i % 2] }),
        });
      }
      return { streamed: harnessLogTelemetry.streamed };
    });
    const grown = await waitForCount(page, 13, 15000);
    record(
      "twelve policy changes on the API arrive in the view over the stream, with no reload",
      grown.held === 13 && grown.rows.length === 13 && grown.cards.length === 13
        && grown.cards.every((card, index) => card.seq === index + 1),
      `held=${grown.held} rows=${grown.rows.length} cards=${grown.cards.length}`,
    );
    record(
      "the strip names the live source while the stream is filling the view",
      grown.mode === "live" && /following the log from seq 13/.test(grown.state),
      `mode=${grown.mode} state="${grown.state}"`,
    );
    record(
      "the revision card says what the policy moved between, not that an event happened",
      /plan\s*->\s*autonomous/.test(grown.cards[12].text) && /revision 13/.test(grown.cards[12].text),
      grown.cards[12].text,
    );

    // ── Jump to latest, in a pane short enough to scroll ───────────────────
    await page.setViewport({ width: 1440, height: 560, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 3);
    const jumped = await page.evaluate(async () => {
      const stream = document.getElementById("harnessStream");
      const jump = document.getElementById("harnessTimelineJump");
      stream.scrollTop = stream.scrollHeight;
      stream.dispatchEvent(new Event("scroll"));
      const hiddenAtBottom = jump.hidden && jump.getBoundingClientRect().height === 0;
      stream.scrollTop = 0;
      stream.dispatchEvent(new Event("scroll"));
      const shownWhenUp = !jump.hidden && jump.getBoundingClientRect().height > 0;
      const scrollable = stream.scrollHeight > stream.clientHeight + 40;
      jump.click();
      await new Promise((done) => setTimeout(done, 60));
      return {
        scrollable,
        hiddenAtBottom,
        shownWhenUp,
        afterClickHidden: jump.hidden && jump.getBoundingClientRect().height === 0,
        atBottom: stream.scrollHeight - stream.scrollTop - stream.clientHeight <= 2,
      };
    });
    record(
      "Jump to latest is offered only when the reader is above the newest event",
      jumped.scrollable === true && jumped.hiddenAtBottom === true && jumped.shownWhenUp === true,
      `scrollable=${jumped.scrollable} hidden at bottom=${jumped.hiddenAtBottom} shown when up=${jumped.shownWhenUp}`,
    );
    record(
      "Jump to latest is what returns the reader, and then it removes itself",
      jumped.afterClickHidden === true && jumped.atBottom === true,
      `hidden after=${jumped.afterClickHidden} atBottom=${jumped.atBottom}`,
    );

    // ── Replay: rebuild from the record, twice, with no duplicates ─────────
    const replayed = await page.evaluate(async () => {
      const before = harnessCachedEvents().length;
      const firstPass = await replayHarnessLog();
      const once = { state: harnessLogState(), cards: document.querySelectorAll("#harnessStream .h-event").length };
      const secondPass = await replayHarnessLog();
      const twice = { state: harnessLogState(), cards: document.querySelectorAll("#harnessStream .h-event").length };
      return { before, firstPass, once, secondPass, twice };
    });
    record(
      "Replay re-reads the log from its first retained event and rebuilds the view from it",
      replayed.firstPass && replayed.firstPass.before === 13 && replayed.firstPass.after === 13
        && replayed.once.cards === 13 && replayed.once.state.text.includes("replayed 1 time"),
      `before=${replayed.before} after=${replayed.firstPass ? replayed.firstPass.after : "null"} cards=${replayed.once.cards} state="${replayed.once.state.text}"`,
    );
    record(
      "Replaying again does not duplicate an event and counts the replay",
      replayed.secondPass && replayed.twice.cards === 13 && replayed.twice.state.count === 13
        && replayed.twice.state.text.includes("replayed 2 times"),
      `cards=${replayed.twice.cards} count=${replayed.twice.state.count} state="${replayed.twice.state.text}"`,
    );

    // ── A frame that lands while the reader is up does not move them ───────
    const held = await page.evaluate(async () => {
      const stream = document.getElementById("harnessStream");
      stream.scrollTop = 0;
      stream.dispatchEvent(new Event("scroll"));
      const before = stream.scrollTop;
      await replayHarnessLog();
      const jump = document.getElementById("harnessTimelineJump");
      return { before, after: stream.scrollTop, jumpShown: !jump.hidden && jump.getBoundingClientRect().height > 0, scrollable: stream.scrollHeight > stream.clientHeight + 40 };
    });
    record(
      "a re-render does not move a reader who is scrolled up, and the strip offers the way back",
      held.scrollable === true && held.after === held.before && held.jumpShown === true,
      `scrollTop ${held.before} -> ${held.after} jump=${held.jumpShown}`,
    );
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 2);
    await page.evaluate(() => { const stream = document.getElementById("harnessStream"); stream.scrollTop = 0; stream.dispatchEvent(new Event("scroll")); });
    await sleep(args.settleMs);
    await shoot(page, "replayed");
    // A window small enough that the strip and the trajectory compete for room: the strip
    // keeps its own line, and the trajectory keeps enough of the pane to be read.
    await page.setViewport({ width: 1440, height: 560, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 2);
    const shortWindow = await page.evaluate(() => {
      const host = document.getElementById("harnessTimeline");
      const stream = document.getElementById("harnessStream");
      return {
        stripHeight: host.getBoundingClientRect().height,
        streamHeight: stream.getBoundingClientRect().height,
        stateVisible: document.getElementById("harnessTimelineState").getBoundingClientRect().height > 0,
        actionsVisible: document.getElementById("harnessTimelineReplay").getBoundingClientRect().height > 0,
      };
    });
    record(
      "in a window too short for both, the strip keeps its line and the trajectory keeps its room",
      shortWindow.stripHeight >= 24 && shortWindow.stateVisible === true
        && shortWindow.actionsVisible === true && shortWindow.streamHeight >= 90,
      `strip=${Math.round(shortWindow.stripHeight)}px stream=${Math.round(shortWindow.streamHeight)}px`,
    );
    await shoot(page, "short-window");
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 3);
    await page.evaluate(() => { const stream = document.getElementById("harnessStream"); stream.scrollTop = stream.scrollHeight; stream.dispatchEvent(new Event("scroll")); });

    // ── The strip is honest about what the view is not showing ─────────────
    const honest = await page.evaluate(() => {
      const out = {};
      const held = harnessCachedEvents();
      // (a) the log holds more than this view: a cache reset with no reload yet. Read the
      // sentence, then rebuild a view that is consistent before anything is drawn.
      harnessLogTelemetry.source = "log";
      harnessLogTelemetry.reason = "";
      harnessResetEventCache();
      out.ahead = harnessLogState();
      renderHarnessTimeline();
      out.aheadDom = document.getElementById("harnessTimelineState").textContent.trim();
      // (b) a log whose head was pruned by the server: the view holds seq 5..13 and says
      // how many events it can no longer show. This is the shape a pruned log has, built
      // from the events the server really returned.
      const retainedFloor = 5;
      harnessEventCache = new Map(held.filter((ev) => Number(ev.seq) >= retainedFloor).map((ev) => [Number(ev.seq), ev]));
      harnessCursor = held.length ? Number(held[held.length - 1].seq) : 0;
      const savedSnapshot = harnessSnapshot;
      harnessLogTelemetry.latest = 0;
      harnessLogTelemetry.retained = 0;
      harnessSnapshot = { ...(savedSnapshot || {}), event_stream: { latest_seq: harnessCursor, retained_from_seq: retainedFloor } };
      out.pruned = harnessLogState();
      renderStoredHarnessEvents(harnessCachedEvents());
      out.prunedDom = {
        mode: document.getElementById("harnessTimeline").dataset.mode,
        text: document.getElementById("harnessTimelineState").textContent.trim(),
        rows: [...document.querySelectorAll("#harnessTimelineList button.harness-timeline-row")].map((row) => Number(row.dataset.seq)),
        cards: [...document.querySelectorAll("#harnessStream .h-event")].map((card) => Number(card.dataset.seq)),
      };
      // (c) the same log when the reader could not resume it at all.
      harnessLogTelemetry.reason = "events_pruned";
      out.stalled = harnessLogState();
      renderHarnessTimeline();
      out.stalledDom = { mode: document.getElementById("harnessTimeline").dataset.mode, text: document.getElementById("harnessTimelineState").textContent.trim() };
      harnessLogTelemetry.reason = "";
      harnessSnapshot = savedSnapshot;
      return out;
    });
    record(
      "the strip says how many events this view holds of the number last reported",
      /0 events held/.test(honest.ahead.text)
        && /this view holds 0 of the 13 events last reported/.test(honest.ahead.text)
        && !/following the log/.test(honest.ahead.text),
      honest.aheadDom,
    );
    record(
      "a pruned log is shown from its retained floor, and the strip says how much is gone",
      honest.pruned.mode === "pruned"
        && /\b9 events held\b/.test(honest.pruned.text)
        && honest.pruned.first === 5 && honest.pruned.last === 13
        && /4 earlier events pruned, retained from seq 5/.test(honest.pruned.text)
        && honest.prunedDom.mode === "pruned"
        && honest.prunedDom.rows.join(",") === "5,6,7,8,9,10,11,12,13"
        && honest.prunedDom.cards.join(",") === "5,6,7,8,9,10,11,12,13",
      `${honest.prunedDom.text} mode=${honest.prunedDom.mode} rows=${JSON.stringify(honest.prunedDom.rows)} cards=${JSON.stringify(honest.prunedDom.cards)}`,
    );
    record(
      "when the log does not resume, the strip says so and names the reason",
      honest.stalled.mode === "pruned"
        && /^the log did not resume \(events_pruned\), so this view is the snapshot's/.test(honest.stalled.text)
        && honest.stalledDom.mode === honest.stalled.mode
        && honest.stalledDom.text === honest.stalled.text,
      honest.stalledDom.text,
    );
    await shoot(page, "pruned");

    // ── A resync rebuilds the whole log without duplicating it ─────────────
    const reread = await page.evaluate(async () => {
      await harnessResyncFromSnapshot("operator");
      return { state: harnessLogState(), cards: document.querySelectorAll("#harnessStream .h-event").length };
    });
    record(
      "a resync restores every event from the server without duplicating one",
      reread.state.count === 13 && reread.cards === 13 && reread.state.first === 1 && reread.state.last === 13,
      `count=${reread.state.count} cards=${reread.cards} seq ${reread.state.first}-${reread.state.last} state="${reread.state.text}"`,
    );
    await page.setViewport({ width: 900, height: 800, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 3);

    // ── The strip survives a narrow pane ───────────────────────────────────
    const narrow = await page.evaluate(() => {
      const host = document.getElementById("harnessTimeline");
      const bar = document.querySelector(".harness-timeline-bar");
      const state = document.getElementById("harnessTimelineState");
      const hostBox = host.getBoundingClientRect();
      const barBox = bar.getBoundingClientRect();
      return {
        fitsWidth: hostBox.width > 0 && barBox.width <= hostBox.width + 1,
        stateVisible: state.getBoundingClientRect().height > 0,
        stateText: state.textContent.trim().slice(0, 40),
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      };
    });
    record(
      "the strip survives a 900px pane without overflowing or hiding its sentence",
      narrow.fitsWidth === true && narrow.stateVisible === true && narrow.overflow <= 1,
      `fits=${narrow.fitsWidth} state=${narrow.stateVisible} overflow=${narrow.overflow}px`,
    );

    record(
      "no page error occurred during the walk",
      errors.length === 0,
      errors.length ? errors.slice(0, 2).join(" | ") : "",
    );

    const failed = results.filter((result) => !result.passed);
    if (args.json) {
      console.log(JSON.stringify({ steps: results, passed: results.length - failed.length, failed: failed.length }, null, 2));
    } else {
      console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
    }
    process.exitCode = failed.length ? 1 : 0;
  } finally {
    await browser.close();
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exitCode = 1;
});
