#!/usr/bin/env node
/**
 * Check that Compare shows its evidence, and says what it does not know (UIV2-17).
 *
 *   node scripts/compare_evidence_walk.mjs --screenshots docs/media/compare-evidence
 *
 * The pane draws two tables and a rail, and three things were wrong with them. A panel that never
 * answered was given a size of `0 ch` - a zero standing in for a silence, in a pane whose own
 * provenance table refuses exactly that. The arbiter card named a model, and said nothing about
 * where the verdict came from, what it judged, or what its own figures were. And the Inspector -
 * which the approved target calls "evidence in the inspector" - counted criteria and then told the
 * reader that "scores and the arbiter verdict render in the Compare pane".
 *
 * What it proves:
 *
 *  1. nothing is measured before a run: both tables are hidden and the inspector says so, rather
 *     than showing zeroes;
 *  2. two answers produce one row per reply in both tables, and the two answer bodies render at
 *     one type scale - same size, leading and family - because side by side is a claim about
 *     comparability;
 *  3. a reply with no text shows `—`, never `0 ch`;
 *  4. a provider failure is attributed to the panel it happened to, and the panel that never
 *     started says so; the reason sits with the row, and no figure is invented for either;
 *  5. the arbiter card states which model judged, that it judged this question, and that the
 *     replies above are its evidence - and the same model appears in the table, marked `arbiter`;
 *  6. the inspector repeats the pane's figures row for row, so it cannot quietly disagree with it,
 *     and where a reply has no figures the inspector gives the reason instead of a zero;
 *  7. while a panel is streaming, its figures are described as arriving with the reply;
 *  8. a stopped run is attributed, explained in both places, and leaves Start ready to run again;
 *  9. at 390x844 both tables fit and nothing overflows sideways;
 * 10. the pane raises no console error and posts only the debate stream.
 *
 * Usage:
 *
 *   node scripts/compare_evidence_walk.mjs                        # starts its own fixtures
 *   node scripts/compare_evidence_walk.mjs --base-url URL --token TOKEN   # slow: no stopped state
 *   node scripts/compare_evidence_walk.mjs --screenshots DIR --viewport 1440x900 --theme dark
 *   node scripts/compare_evidence_walk.mjs --capture-only          # write the "before" set
 *
 * Without `--base-url` it starts `scripts/council_readiness_seed.py` twice: once with two loaded
 * local models, and once with one of them slow, which is what makes a stopped run photo-able.
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
  states: "",
};

/**
 * The groups `--states` can select. The verdict, the rail and the 390x844 frame are shot inside
 * `populated`; `narrow` and `inspector` are not states of their own, so naming them here would be
 * a promise the walk cannot keep.
 */
const STATES = ["empty", "populated", "error", "stopped", "long"];

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
      case "--states": args.states = next(); break;
      case "--help":
      case "-h":
        console.log("usage: node scripts/compare_evidence_walk.mjs [--base-url URL --token TOKEN] [--screenshots DIR] [--viewport WxH] [--theme light|dark] [--states populated,error] [--json] [--capture-only]");
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
  if (!found) throw new Error("no Chromium found: set STUDIO_SCREENSHOT_CHROME to one");
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

async function startFixture(extraArgs = []) {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--resident-model", "Qwen3.6-35B-REAP-19B", "--port", "0", ...extraArgs],
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

/** Everything the walk needs about the results, the provenance table and the rail. */
function readPane(page) {
  return page.evaluate(() => {
    const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
    const style = (element) => {
      if (!element) return null;
      const computed = getComputedStyle(element);
      return {
        fontFamily: computed.fontFamily,
        fontSize: computed.fontSize,
        lineHeight: computed.lineHeight,
        marginTop: computed.marginTop,
        marginBottom: computed.marginBottom,
      };
    };
    const table = (host) => {
      if (!host) return { hidden: true, rows: [], heads: [], text: "" };
      const rows = [...host.querySelectorAll("tbody tr")].map((row) => ({
        cells: [...row.querySelectorAll("th, td")].map((cell) => clean(cell.textContent)),
        mono: [...row.querySelectorAll("td.mono")].map((cell) => clean(cell.textContent)),
        note: Boolean(row.querySelector("td.debate-provenance-note")),
      }));
      return {
        hidden: host.hidden,
        heads: [...host.querySelectorAll("thead th")].map((cell) => clean(cell.textContent)),
        rows,
        text: clean(host.textContent),
        overflowX: host.scrollWidth - host.clientWidth,
      };
    };
    const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")].map((card) => {
      const body = card.querySelector(".stage-output-body");
      const first = body ? body.querySelector("p, li, pre, div") : null;
      return {
        status: card.dataset.status || "",
        model: clean(card.querySelector(".stage-output-head strong") ? card.querySelector(".stage-output-head strong").textContent : ""),
        text: clean(body ? body.textContent : ""),
        bodyStyle: style(body),
        firstStyle: style(first),
        scrolls: Boolean(body) && body.scrollHeight > body.clientHeight + 1,
        height: card.getBoundingClientRect().height,
      };
    });
    const verdict = document.querySelector("#debateSynthesisArea [data-role='verdict']");
    const inspector = document.getElementById("monitorRail");
    const results = document.getElementById("debateResults");
    const provenance = document.getElementById("debateProvenance");
    return {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      overflowX: document.documentElement.scrollWidth - window.innerWidth,
      results: table(results),
      provenance: table(provenance),
      cards,
      verdict: verdict
        ? {
          head: clean(verdict.querySelector(".stage-output-head") ? verdict.querySelector(".stage-output-head").textContent : ""),
          provenance: clean(verdict.querySelector("[data-verdict-provenance]") ? verdict.querySelector("[data-verdict-provenance]").textContent : ""),
          body: clean(verdict.querySelector(".stage-output-body") ? verdict.querySelector(".stage-output-body").textContent : ""),
          badge: clean(verdict.querySelector(".badge") ? verdict.querySelector(".badge").textContent : ""),
        }
        : null,
      synthesis: clean(document.getElementById("debateSynthesisArea") ? document.getElementById("debateSynthesisArea").textContent : ""),
      inspector: clean(inspector ? inspector.textContent : ""),
      inspectorItems: inspector
        ? [...inspector.querySelectorAll(".insp-item, .insp-empty, .insp-kv")].map((item) => clean(item.textContent))
        : [],
      summary: clean(document.getElementById("debateSummary") ? document.getElementById("debateSummary").textContent : ""),
      startDisabled: Boolean(document.getElementById("debateRunBtn") && document.getElementById("debateRunBtn").disabled),
      checked: [...document.querySelectorAll(".debate-m-chk:checked")].map((box) => box.value),
      options: [...document.querySelectorAll(".debate-m-chk")].map((box) => box.value),
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
    }, focus || "#debateSummary", block);
    await sleep(args.settleMs * 2);
    await page.screenshot({ path });
    shots.push(path);
    return path;
  };

  const browser = await puppeteer.launch({
    executablePath: resolveChrome(),
    args: (process.env.STUDIO_SCREENSHOT_CHROME_ARGS || "--no-zygote --disable-gpu --no-sandbox --disable-dev-shm-usage").split(" ").filter(Boolean),
  });
  const pageErrors = [];
  const posts = [];
  const fixtures = [];

  const openPage = async (base, authToken) => {
    const page = await browser.newPage();
    page.on("pageerror", (error) => pageErrors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error" && !/status of 40[34]/.test(message.text())) pageErrors.push(message.text()); });
    page.on("request", (request) => { if (request.method() === "POST") posts.push(new URL(request.url()).pathname); });
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    const paneOrigin = new URL(base).origin;
    await page.evaluateOnNewDocument((token, key, theme, own) => {
      if (window.top === window && location.origin === own) window.sessionStorage.setItem(key, token);
      localStorage.setItem("m5_theme", theme);
    }, authToken, args.tokenKey, args.theme, paneOrigin);
    await page.goto(paneOrigin, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
    await sleep(args.settleMs * 10);
    await page.evaluate(() => switchTab("debate"));
    await page.waitForFunction(() => document.querySelectorAll(".debate-m-chk").length > 0, { timeout: 30000 }).catch(() => {});
    await sleep(args.settleMs * 4);
    return page;
  };

  /** Start a fixture (unless one was given), open the pane on it, and clean both up after. */
  const withFixture = async (extraArgs, work) => {
    const fixture = args.baseUrl ? null : await startFixture(extraArgs);
    if (fixture) fixtures.push(fixture);
    const base = args.baseUrl || fixture.ready.base_url;
    const token = args.token || (fixture && fixture.ready.token) || "";
    const page = await openPage(base, token);
    try {
      return await work(page, fixture ? fixture.ready : { base_url: base, token });
    } finally {
      await page.close().catch(() => {});
      if (fixture) {
        fixture.child.kill();
        fixtures.splice(fixtures.indexOf(fixture), 1);
      }
    }
  };

  const pick = (page, wanted) => page.evaluate(async (choice) => {
    const boxes = [...document.querySelectorAll(".debate-m-chk")];
    boxes.forEach((box) => { box.checked = choice.models.includes(box.value); });
    document.getElementById("debatePrompt").value = choice.promptText;
    updateDebateRunControl();
    await new Promise((done) => setTimeout(done, 200));
    return [...document.querySelectorAll(".debate-m-chk:checked")].map((box) => box.value);
  }, wanted);

  /** Wait until the run is over - every panel settled and the arbiter's own stream closed. */
  const settle = async (page, limitMs = 120000) => {
    const started = Date.now();
    while (Date.now() - started < limitMs) {
      await sleep(400);
      const done = await page.evaluate(() => {
        const cards = [...document.querySelectorAll("#debateGrid .stage-output-card")];
        const start = document.getElementById("debateRunBtn");
        const streaming = [...document.querySelectorAll("#debateGrid .stage-output-card")]
          .some((card) => ["queued", "running"].includes(card.dataset.status));
        const synth = (document.getElementById("debateSynthesisArea") || {}).textContent || "";
        const arbiterStreaming = /synthesizing/i.test(synth);
        return cards.length > 0 && !streaming && !arbiterStreaming && Boolean(start) && start.disabled === false;
      });
      if (done) return true;
    }
    return false;
  };

  const localsOf = (snapshot) => snapshot.options.filter((value) => !value.includes("::") && !value.startsWith("combo/"));
  const wanted = args.states ? new Set(args.states.split(",").map((name) => name.trim()).filter(Boolean)) : null;
  // A misspelt state used to select no group at all: the run then reported the two housekeeping
  // checks as "1/2 passed", which reads like a broken pane rather than a broken command.
  if (wanted) {
    const unknown = [...wanted].filter((name) => !STATES.includes(name));
    if (unknown.length) {
      console.error(`unknown state(s): ${unknown.join(", ")} — this walk knows ${STATES.join(", ")}`);
      process.exit(2);
    }
  }
  const state = (name) => !wanted || wanted.has(name);

  try {
    // ── 1-2: nothing is measured before a run ─────────────────────────────────
    if (state("empty")) await withFixture([], async (page) => {
      const empty = await readPane(page);
      record(
        "before a run the pane shows no table and invents no figure",
        empty.results.hidden && empty.provenance.hidden && !/\b0 s\b|\b0 tok\/s\b/.test(empty.inspector),
        `results hidden=${empty.results.hidden} provenance hidden=${empty.provenance.hidden}`,
      );
      record(
        "and the inspector says there is no run rather than counting nothing",
        /no comparison|not run|nothing has been compared/i.test(empty.inspector),
        empty.inspector.slice(0, 140),
      );
      await shoot(page, `evidence-empty-${args.viewport}-${args.theme}`);
    });

    // ── 3-6: two answers, one type scale, one row each, and a verdict with provenance ──
    let pair = [];
    if (state("populated")) await withFixture([], async (page) => {
      const before = await readPane(page);
      pair = localsOf(before).slice(0, 2);
      record(
        "the fixture serves two local models, so a comparison is two real answers",
        pair.length >= 2,
        pair.length ? pair.join(" + ") : "no local model in the picker",
      );
      await pick(page, { models: pair, promptText: REQUEST });
      const judged = await page.evaluate(async (judgeModel) => {
        const synth = document.getElementById("d-synth");
        synth.checked = true;
        const judge = document.getElementById("d-judge");
        if (judge && judgeModel) judge.value = judgeModel;
        syncDebateArbiter();
        updateDebateRunControl();
        await new Promise((done) => setTimeout(done, 200));
        return { judge: judge ? judge.value : "", synth: synth.checked };
      }, pair[1] || pair[0]);
      record(
        "an arbiter can be chosen, so the verdict has somewhere to come from",
        judged.synth && Boolean(judged.judge),
        `synth=${judged.synth} judge=${judged.judge}`,
      );
      await page.evaluate(() => runDebate());
      await settle(page);
      const ran = await readPane(page);
      record(
        "each reply is one row in the results and one row in the provenance table",
        ran.cards.length === 2 && ran.results.rows.length === 2
          && ran.provenance.rows.filter((row) => !row.note).length >= 2,
        `cards ${ran.cards.length} · results rows ${ran.results.rows.length} · provenance rows ${ran.provenance.rows.length}`,
      );
      record(
        "the two answers are drawn at one type scale, since side by side is a claim about comparability",
        ran.cards.length === 2
          && JSON.stringify(ran.cards[0].bodyStyle) === JSON.stringify(ran.cards[1].bodyStyle)
          && JSON.stringify(ran.cards[0].firstStyle) === JSON.stringify(ran.cards[1].firstStyle),
        `${JSON.stringify(ran.cards[0] ? ran.cards[0].bodyStyle : null)} vs ${JSON.stringify(ran.cards[1] ? ran.cards[1].bodyStyle : null)}`,
      );
      record(
        "a reply with text has a size, and the size is the reply's own length",
        ran.cards.length === 2 && ran.results.rows.every((row, index) => {
          const size = Number(String(row.cells[2] || "").replace(" ch", ""));
          return /^\d+ ch$/.test(row.cells[2]) && size >= ran.cards[index].text.length;
        }),
        ran.results.rows.map((row) => row.cells.join(" | ")).join("  ·  "),
      );
      await shoot(page, `evidence-populated-${args.viewport}-${args.theme}`, "#debateResults");

      const verdict = ran.verdict;
      record(
        "the verdict names the model that judged, that it judged this question, and what its evidence was",
        Boolean(verdict)
          && /arbiter/i.test(verdict.head)
          && /judged this question/i.test(verdict.provenance)
          && /evidence/i.test(verdict.provenance),
        verdict ? `head="${verdict.head}" provenance="${verdict.provenance}"` : "no verdict card",
      );
      record(
        "and the arbiter is one row in the table, marked as the arbiter",
        ran.provenance.rows.some((row) => row.cells.some((cell) => /arbiter/i.test(cell))),
        ran.provenance.rows.map((row) => row.cells.join(" | ")).join("  ·  "),
      );
      record(
        "the verdict carries the figures the runtime reported for it, or says none came",
        Boolean(verdict) && (verdict.badge.length > 0) && !/^\s*$/.test(verdict.badge),
        verdict ? `badge="${verdict.badge}"` : "no verdict card",
      );
      await shoot(page, `evidence-verdict-${args.viewport}-${args.theme}`, "#debateSynthesisArea");

      const paneFigures = ran.provenance.rows
        .filter((row) => !row.note)
        .map((row) => row.cells.slice(1, 5).filter((cell) => cell && cell !== "—"));
      record(
        "the inspector carries the evidence, one line per reply",
        paneFigures.length >= 2 && paneFigures.every((figures) => figures.some((figure) => ran.inspector.includes(figure))),
        `figures ${JSON.stringify(paneFigures)} · rail="${ran.inspector.slice(0, 160)}"`,
      );
      record(
        "and says where the figures came from, and that cost is not known",
        /usage/i.test(ran.inspector) && /cost/i.test(ran.inspector) && /not known|no price/i.test(ran.inspector),
        ran.inspector.slice(0, 200),
      );

      // ── narrow: both tables fit, and the rail stacks ──
      await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 4);
      const narrow = await readPane(page);
      record(
        "at 390x844 both tables fit the pane and nothing overflows sideways",
        narrow.overflowX === 0 && narrow.results.overflowX <= 0 && narrow.provenance.overflowX <= 0,
        `page ${narrow.overflowX} · results ${narrow.results.overflowX} · provenance ${narrow.provenance.overflowX}`,
      );
      await shoot(page, `evidence-narrow-390x844-${args.theme}`, "#debateResults");
      await page.setViewport({ width, height, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 2);
      await shoot(page, `evidence-inspector-${args.viewport}-${args.theme}`, "#monitorRail");
    });

    // ── 7-8: a failure is attributed and never given a zero ───────────────────
    if (state("error")) await withFixture([], async (page) => {
      const options = await readPane(page);
      // The catalogue carries one provider row saved without a key: it fails the moment it is
      // called, so a failure can be photographed without a network and without inventing one.
      const keyless = options.options.find((value) => value.includes("::"));
      const survivor = localsOf(options)[0];
      record(
        "a provider that will fail can be picked, so a failure can be shown honestly",
        Boolean(keyless) && Boolean(survivor),
        `keyless=${keyless || "missing"} survivor=${survivor || "missing"}`,
      );
      await pick(page, { models: [survivor, keyless].filter(Boolean), promptText: REQUEST });
      await page.evaluate(() => { document.getElementById("d-synth").checked = false; syncDebateArbiter(); });
      await page.evaluate(() => runDebate());
      await settle(page);
      const failed = await readPane(page);
      const failedRow = failed.results.rows.find((row) => /failed/.test(row.cells[1] || ""));
      const otherRow = failed.results.rows.find((row) => row !== failedRow);
      record(
        "a failure is attributed to the row of the panel it happened to, and the other panel's outcome is stated",
        Boolean(failedRow) && failedRow.cells[0] === keyless
          && Boolean(otherRow) && /answered|never ran|not started/.test(otherRow.cells[1] || "")
          && /failed/.test(failed.summary) && /answered|never ran|not started/.test(failed.summary),
        `${failed.summary} · row="${failedRow ? failedRow.cells.join(" | ") : "missing"}"`,
      );
      record(
        "a panel with no reply shows a dash, never 0 ch",
        failed.results.rows.length > 0
          && failed.results.rows.some((row) => row.cells[2] === "—")
          && failed.results.rows.every((row) => row.cells[2] === "—" || /^\d+ ch$/.test(row.cells[2])),
        failed.results.rows.map((row) => row.cells.join(" | ")).join("  ·  "),
      );
      record(
        "and its figures are refused with a reason rather than a zero",
        failed.provenance.rows.some((row) => row.note && /failed|never started|no usage/i.test(row.cells.join(" ")))
          && !/\b0 s\b|\b0 tokens\b|\b0 tok\/s\b/.test(failed.provenance.text),
        failed.provenance.text.slice(0, 200),
      );
      record(
        "the inspector names the same reason the table gives",
        /failed/i.test(failed.inspector) && /provider transport/i.test(failed.inspector),
        failed.inspector.slice(0, 200),
      );
      await shoot(page, `evidence-error-${args.viewport}-${args.theme}`, "#debateResults");
    });

    // ── 9-10: streaming, then stopped, then ready again ───────────────────────
    if (state("stopped")) await withFixture(["--slow-model", "Qwen3.8-27B-oQ4e-mtp"], async (page, ready) => {
      const state = await readPane(page);
      const slowModel = localsOf(state).find((value) => value === ready.slow_model) || localsOf(state)[0];
      const secondModel = localsOf(state).find((value) => value !== slowModel);
      record(
        "the slow fixture serves two models, so one can be stopped while the other waits",
        Boolean(slowModel) && Boolean(secondModel),
        `${slowModel || "?"} + ${secondModel || "?"}`,
      );
      await pick(page, { models: [slowModel, secondModel].filter(Boolean), promptText: REQUEST });
      await page.evaluate(() => { document.getElementById("d-synth").checked = false; syncDebateArbiter(); });
      await page.evaluate(() => runDebate());
      await sleep(args.settleMs * 12);
      const loading = await readPane(page);
      record(
        "while a panel is streaming, its figures are described as arriving with the reply",
        /still streaming|arrives with the reply|has not started|never started/i.test(loading.provenance.text),
        loading.provenance.text.slice(0, 200),
      );
      await shoot(page, `evidence-loading-${args.viewport}-${args.theme}`, "#debateProvenance");
      await page.evaluate(() => { const stop = document.getElementById("debateStopBtn"); if (stop && !stop.disabled) stop.click(); });
      await settle(page, 40000);
      const stopped = await readPane(page);
      record(
        "a stopped run is attributed, explained in both places, and leaves Start ready",
        /stopped/i.test(stopped.summary) && /stopped/i.test(stopped.provenance.text)
          && /stopped/i.test(stopped.inspector) && stopped.startDisabled === false,
        `summary="${stopped.summary}" rail="${stopped.inspector.slice(0, 120)}" start disabled=${stopped.startDisabled}`,
      );
      await shoot(page, `evidence-stopped-${args.viewport}-${args.theme}`, "#debateResults");
    });

    // ── 11: a reply long enough to need scrolling ─────────────────────────────
    if (state("long")) await withFixture(["--long-model", "Qwen3.8-27B-oQ4e-mtp"], async (page, ready) => {
      const options = await readPane(page);
      const longModel = options.options.find((value) => value === ready.long_model);
      record(
        "the fixture can answer at length, so a long reply is a state and not a claim",
        Boolean(longModel),
        `long=${longModel || "missing"}`,
      );
      await pick(page, { models: [longModel].filter(Boolean), promptText: REQUEST });
      await page.evaluate(() => { document.getElementById("d-synth").checked = false; syncDebateArbiter(); });
      await page.evaluate(() => runDebate());
      await settle(page);
      const longRead = await readPane(page);
      const longCard = longRead.cards[0];
      record(
        "a long reply scrolls inside its card, and the table reports its whole size",
        Boolean(longCard) && longCard.scrolls
          && /^\d{3,} ch$/.test(longRead.results.rows[0] ? longRead.results.rows[0].cells[2] : "")
          && longRead.results.overflowX <= 0,
        longCard ? `scrolls=${longCard.scrolls} height=${Math.round(longCard.height)} size=${longRead.results.rows[0] ? longRead.results.rows[0].cells[2] : "?"}` : "no card",
      );
      await shoot(page, `evidence-long-content-${args.viewport}-${args.theme}`, "#debateGrid");
    });

    record(
      "the pane posts only the debate stream",
      posts.length > 0 && posts.every((path) => path === "/api/debate/stream"),
      posts.join(" · ") || "no POST",
    );
    record(
      "no uncaught error is raised by any of it",
      pageErrors.length === 0,
      pageErrors.slice(0, 3).join(" · "),
    );
  } finally {
    await browser.close();
    fixtures.forEach((fixture) => fixture.child.kill());
  }

  const passed = results.filter((result) => result.passed).length;
  if (args.json) {
    console.log(JSON.stringify({ viewport: args.viewport, theme: args.theme, passed, total: results.length, screenshots: shots, results }, null, 2));
  } else {
    console.log(`\n${passed}/${results.length} checks passed · ${shots.length} screenshots`);
  }
  return passed === results.length ? 0 : 1;
}

run(parseArgs(process.argv.slice(2)))
  .then((code) => { process.exitCode = code; })
  .catch((error) => {
    console.error(String(error && error.stack ? error.stack : error));
    process.exitCode = 1;
  });
