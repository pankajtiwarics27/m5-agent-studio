#!/usr/bin/env node
/**
 * Draw the status and evidence components in a real browser and measure the result
 * (UIUX-P1-03).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/status_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Why this exists: `scripts/uiux_status_components.py` proves the declarations agree
 * and every state has a tone and a word. It cannot prove a chip looks like anything -
 * a rule can name a token and still be overridden by a later layer, which is exactly
 * how `.badge.green` and `.badge.red` came to paint the same colour. So this drives a
 * browser and checks what a reader would see:
 *
 *   - every tone paints a dot that is visible and the colour of the token it names;
 *   - every state in the harness table renders label first, and its tone matches;
 *   - the dot is non-zero and the label is text, in both themes, at 1440px and 420px;
 *   - a live region is silent when the same state is written twice and speaks once
 *     when the state changes - the "announce the transition, not the render" rule;
 *   - an evidence block shows its basis, refuses an unknown one, and its copy
 *     affordance carries the basis with the claim;
 *   - no page errors during the walk.
 *
 * It draws into a detached probe element, so it does not disturb the shell's own
 * state: nothing here writes to a surface the user can see.
 *
 * Exit status is 0 only when every check passed; `--json` prints the results so a
 * story can record the exact output.
 */
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 250,
  timeoutMs: 45000,
};

function parseArgs(argv) {
  const args = { ...DEFAULTS, baseUrl: "", token: "", json: false };
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
    "Draw the status and evidence components in a browser and report what rendered.",
    "",
    "  node scripts/status_walk.mjs --base-url URL --token TOKEN [--json]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-status-and-evidence.md.",
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

/** The in-page probe: draw a chip or a note, measure it, remove it. */
const PROBE = () => {
  window.__statusProbe = (function probeStatus() {
    const host = document.createElement("div");
    host.id = "__status-probe";
    host.style.cssText = "position:fixed;left:-9999px;top:0;display:flex;gap:8px;flex-direction:column;";
    document.body.appendChild(host);

    function measure(node) {
      const style = getComputedStyle(node);
      const dot = node.tagName === "SPAN" ? getComputedStyle(node, "::before") : null;
      const rect = node.getBoundingClientRect();
      return {
        tone: node.dataset.tone || null,
        text: node.textContent.trim(),
        color: style.color,
        borderColor: style.borderTopColor,
        dotBackground: dot ? dot.backgroundColor : null,
        dotWidth: dot ? dot.width : null,
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
    }

    return {
      tone(tone, text) {
        const node = document.createElement("span");
        node.className = "status";
        node.dataset.tone = tone;
        node.textContent = text;
        host.appendChild(node);
        const out = measure(node);
        node.remove();
        return out;
      },
      state(tone, text) {
        // Exactly what setStatus() writes, through the same function.
        const node = document.createElement("span");
        node.className = "status";
        node.dataset.tone = tone;
        node.textContent = text;
        host.appendChild(node);
        window.M5Status.set(node, tone, text);
        const out = measure(node);
        node.remove();
        return out;
      },
      async transition(tone, text, nextTone, nextText) {
        const node = document.createElement("span");
        node.className = "status";
        node.setAttribute("role", "status");
        node.dataset.tone = tone;
        node.textContent = text;
        document.body.appendChild(node);
        // A screen reader speaks the content that changed, not the attributes that
        // carried it: one setStatus() change touches class, data-tone and the text
        // node, and counting all of them would call a single announcement four.
        const seen = [];
        const observer = new MutationObserver((records) => {
          for (const record of records) {
            seen.push(record.type === "attributes" ? "paint" : "speak");
          }
        });
        const spoken = () => seen.filter((kind) => kind === "speak").length;
        const painted = () => seen.filter((kind) => kind === "paint").length;
        observer.observe(node, { childList: true, characterData: true, subtree: true, attributes: true });
        window.M5Status.set(node, tone, text);          // the same state again
        await new Promise((done) => setTimeout(done, 0));  // MutationObserver batches
        const afterRepeat = spoken();
        window.M5Status.set(node, nextTone, nextText);  // a real transition
        await new Promise((done) => setTimeout(done, 0));
        const afterChange = spoken();
        const paintAfterChange = painted();
        // Same words, new tone - a colour that changes without repeating itself.
        window.M5Status.set(node, tone, nextText);
        await new Promise((done) => setTimeout(done, 0));
        const afterToneOnly = spoken() - afterChange;
        const paintAfterToneOnly = painted() - paintAfterChange;
        observer.disconnect();
        const out = measure(node);
        node.remove();
        return {
          afterRepeat,
          afterChange,
          afterToneOnly,
          paintAfterToneOnly,
          tone: out.tone,
          text: out.text,
        };
      },
      evidence(basis, claim) {
        const node = document.createElement("div");
        host.appendChild(node);
        window.M5Status.evidence(node, basis, claim, { copyText: "claim" });
        const label = node.querySelector(".evidence-basis");
        const copy = node.querySelector(".evidence-copy");
        const style = getComputedStyle(node);
        const out = {
          basis: node.dataset.basis,
          label: label ? label.textContent.trim() : null,
          claim: node.textContent.replace(label ? label.textContent : "", "").replace(copy ? copy.textContent : "", "").trim(),
          copyLabel: copy ? copy.textContent.trim() : null,
          borderLeft: style.borderLeftColor,
        };
        node.remove();
        return out;
      },
      refuses(basis) {
        const node = document.createElement("div");
        try {
          window.M5Status.evidence(node, basis, "x");
          return false;
        } catch (error) {
          return true;
        }
      },
      toneList() {
        return window.M5Status.tones.slice();
      },
      basisList() {
        return window.M5Status.bases.slice();
      },
      token(name) {
        // body, not documentElement: body is where data-theme lives, so body is the
        // element a theme re-points.
        return getComputedStyle(document.body).getPropertyValue(name).trim();
      },
      states() {
        // The table the shell uses, read from the running page rather than the source.
        const source = [...document.querySelectorAll("script")]
          .map((node) => node.textContent || "")
          .join("\n");
        const start = source.indexOf("const map = {");
        const body = source.slice(start, source.indexOf("};", start));
        return [...body.matchAll(/(\w+):\s*\["(\w+)",\s*"([^"]+)"\]/g)].map((hit) => ({
          state: hit[1],
          tone: hit[2],
          label: hit[3],
        }));
      },
      hexToRgb(value) {
        const match = value.trim().match(/^#([0-9a-f]{6})$/i);
        if (!match) return null;
        const int = parseInt(match[1], 16);
        return `rgb(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255})`;
      },
    };
  })();
  return true;
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
    results.push({ step, passed, detail });
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
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
    await sleep(args.settleMs * 4);
    await page.evaluate(PROBE);

    const exported = await page.evaluate(() => typeof window.M5Status === "object" && window.M5Status !== null);
    record("the component is exported for other writers", exported, "window.M5Status");

    const tones = await page.evaluate(() => window.__statusProbe.toneList());
    record("six tones are declared", tones.length === 6, tones.join(", "));

    // Every tone paints the token it names, in both themes.
    for (const theme of ["dark", "light"]) {
      await page.evaluate((value) => {
        if (value === "light") document.body.dataset.theme = "light";
        else delete document.body.dataset.theme;
      }, theme);
      await sleep(args.settleMs / 2);
      const resolved = await page.evaluate(() => ({
        emerald: window.__statusProbe.token("--emerald"),
        amber: window.__statusProbe.token("--amber"),
        rose: window.__statusProbe.token("--rose"),
        cyan: window.__statusProbe.token("--cyan"),
        textDim: window.__statusProbe.token("--text-dim"),
        accent: window.__statusProbe.token("--accent"),
        borderHi: window.__statusProbe.token("--border-hi"),
        text: window.__statusProbe.token("--text"),
      }));
      const expected = {
        ok: resolved.emerald,
        warn: resolved.amber,
        danger: resolved.rose,
        info: resolved.cyan,
        busy: resolved.accent,
        // neutral is the one tone whose dot and hairline name different tokens: a
        // border-hi dot at 1.7:1 would not be a cue, so the dot uses text-dim.
        neutral: resolved.textDim,
      };
      const expectedHairline = { ...expected, neutral: resolved.borderHi };
      for (const tone of tones) {
        const drawn = await page.evaluate(
          ([value, label]) => window.__statusProbe.tone(value, label),
          [tone, `SAMPLE ${tone.toUpperCase()}`],
        );
        const wantDot = await page.evaluate((value) => window.__statusProbe.hexToRgb(value), expected[tone]);
        record(
          `${theme}/${tone}: the dot paints ${expected[tone]}`,
          drawn.dotBackground === wantDot,
          `${drawn.dotBackground} vs ${wantDot}`,
        );
        record(
          `${theme}/${tone}: the dot has size and the chip has a box`,
          drawn.dotWidth === "6px" && drawn.width > 20 && drawn.height > 10,
          `dot ${drawn.dotWidth}, chip ${drawn.width}x${drawn.height}`,
        );
        record(
          `${theme}/${tone}: the label is --text, not the tone`,
          drawn.color === (await page.evaluate((value) => window.__statusProbe.hexToRgb(value), resolved.text)),
          drawn.color,
        );
        const wantHairline = await page.evaluate((value) => window.__statusProbe.hexToRgb(value), expectedHairline[tone]);
        record(
          `${theme}/${tone}: the hairline paints ${expectedHairline[tone]}`,
          drawn.borderColor === wantHairline,
          drawn.borderColor,
        );
      }
    }
    await page.evaluate(() => { delete document.body.dataset.theme; });

    // The state table, drawn through the same writer setStatus() uses.
    const states = await page.evaluate(() => window.__statusProbe.states());
    record("the state table has ten states", states.length === 10, states.map((row) => row.state).join(", "));
    for (const row of states) {
      const drawn = await page.evaluate(
        ([tone, label]) => window.__statusProbe.state(tone, label),
        [row.tone, row.label],
      );
      record(
        `state ${row.state} renders ${row.label} with tone ${row.tone}`,
        drawn.text === row.label && drawn.tone === row.tone && drawn.dotWidth === "6px",
        `${drawn.tone}/${drawn.text}`,
      );
    }

    // Announce the transition, not the render.
    const transition = await page.evaluate(() =>
      window.__statusProbe.transition("ok", "COMPLETED", "danger", "FAILED"));
    record(
      "writing the same state twice is silent",
      transition.afterRepeat === 0,
      `${transition.afterRepeat} mutation(s)`,
    );
    record(
      "changing the state speaks once",
      transition.afterChange === 1 && transition.tone === "ok" && transition.text === "FAILED",
      `${transition.afterChange} announcement(s) → ${transition.tone}/${transition.text}`,
    );
    record(
      "a tone-only change repaints without speaking",
      transition.afterToneOnly === 0 && transition.paintAfterToneOnly > 0,
      `${transition.afterToneOnly} announcement(s), ${transition.paintAfterToneOnly} paint(s)`,
    );

    // Evidence: basis present, unknown basis refused, copy carries the basis.
    const evidence = await page.evaluate(() => window.__statusProbe.evidence("measured", "3 of 6 steps ready"));
    record(
      "an evidence block shows its basis and claim",
      evidence.basis === "measured" && evidence.label === "Basis: measured" && evidence.claim.includes("3 of 6 steps ready"),
      `${evidence.label} / ${evidence.claim}`,
    );
    record(
      "an evidence block offers to copy with its basis",
      evidence.copyLabel === "Copy with basis",
      evidence.copyLabel,
    );
    const refused = await page.evaluate(() => window.__statusProbe.refuses("feels right"));
    record("an unknown basis is refused", refused, "M5Status.evidence('feels right') threw");

    // The converted surfaces are on screen and are the component.
    for (const id of ["harnessActiveStatusBadge", "hallStatusBadge", "dockerStateBadge", "onboardingSummary", "sdlcProgressBadge"]) {
      const drawn = await page.evaluate((ident) => {
        const node = document.getElementById(ident);
        if (!node) return null;
        const dot = getComputedStyle(node, "::before");
        return {
          cls: node.className,
          tone: node.dataset.tone,
          dot: dot.width,
          live: node.getAttribute("role") === "status",
          height: Math.round(node.getBoundingClientRect().height),
          visible: node.offsetParent !== null,
        };
      }, id);
      record(
        `#${id} renders the component`,
        Boolean(
          drawn
          && drawn.cls.includes("status")
          && drawn.tone
          && drawn.dot === "6px"
          && (!drawn.visible || drawn.height > 10)
        ),
        drawn ? `${drawn.tone}, dot ${drawn.dot}${drawn.visible ? `, ${drawn.height}px` : ", in a hidden pane"}` : "missing",
      );
    }

    // Narrow viewport: the chip still fits its box rather than collapsing to the dot.
    await page.setViewport({ width: 420, height: 880, deviceScaleFactor: 1 });
    await sleep(args.settleMs);
    const narrow = await page.evaluate(() => window.__statusProbe.tone("danger", "WAITING APPROVAL"));
    record(
      "the chip keeps its label at 420px",
      narrow.width > 80 && narrow.dotWidth === "6px",
      `${narrow.width}x${narrow.height}`,
    );
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
    await browser.close();
  }

  const failed = results.filter((row) => !row.passed);
  if (args.json) {
    console.log(JSON.stringify({ results, passed: results.length - failed.length, failed: failed.length }, null, 2));
  } else {
    console.log(`\n${results.length - failed.length} of ${results.length} checks passed`);
  }
  if (failed.length > 0) {
    throw new Error(`${failed.length} status check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`status_walk: ${error.message}`);
  process.exit(1);
});
