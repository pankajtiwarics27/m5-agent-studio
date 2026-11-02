#!/usr/bin/env node
/**
 * Measure how the app answers a pointer and a keyboard (UIUX-P5-05).
 *
 *   node scripts/polish_walk.mjs --screenshots docs/media/polish
 *
 * The panes were built pane by pane over many stories, and each one invented its own idea of what a
 * control looks like when you point at it: some buttons light up, some do not, some press, some sit
 * there, the primary action is the one with no reaction at all, and a quarter of the radii in the
 * stylesheet are numbers no token ever named.
 *
 * What this walk measures, on the running app, through the pointer and the keyboard:
 *
 *   1. which interactive *kinds* a reader can actually reach - a kind counts only when a point in the
 *      middle of it hit-tests to itself, so a control hidden in a collapsed section is not a defect;
 *   2. whether pointing at one changes anything a reader can see (colour, border, shadow, opacity,
 *      transform - recorded separately, so "only moved 1px" is not counted as feedback);
 *   3. whether pressing one changes anything;
 *   4. whether it draws the same focus ring as every other control when it takes focus from the
 *      keyboard;
 *   5. whether its cursor says it can be pressed;
 *   6. what a pointer gets when the machine asks for reduced motion: the affordance has to survive
 *      without the transition, because the transition is the part that is being turned off;
 *   7. how many distinct corner radii and durations the app actually computes, against the tokens it
 *      declares.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 220,
  timeoutMs: 120000,
  screenshots: "",
  theme: "dark",
  baseUrl: "",
  token: "",
};

const PANES = ["start", "harness", "hall", "council", "debate", "chat"];

/**
 * Kinds that legitimately do not answer a pointer, with the reason. A text field shows a caret and a
 * border; a selected tab is already the surface the two rules are about; a native select opens a
 * popup the page cannot style.
 */
const TICKING = new Set(["checkbox", "radio", "range", "file", "color", "date", "time"]);

const POINTER_EXEMPT = {
  "button.mode-tab.active": "a selected tab already has the surface the hover rule describes",
};

const PRESS_EXEMPT = {
  "label.chk-lbl": "a checkbox label answers with the tick it places, not with a press colour",
};

/**
 * A text field's answer to a press is a caret and a ring, which is the right answer and not a colour:
 * exempt from the press check, but *not* from the ring check, because a field without a ring is a
 * field a keyboard reader cannot find.
 */
const TYPEABLE = new Set(["", "text", "number", "password", "search", "url", "email", "tel"]);

/** The durations the stylesheet may use, in ms, outside the ones it declares for progress. */
const MOTION_BUDGET_MS = 220;

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
      case "--settle-ms": args.settleMs = Number(next()); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      case "--screenshots": args.screenshots = next(); break;
      case "--theme": args.theme = next(); break;
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
    "Measure the hover, press, focus, cursor and motion affordances of every reachable control.",
    "",
    "  node scripts/polish_walk.mjs [--screenshots DIR] [--theme dark|light] [--json]",
    "  node scripts/polish_walk.mjs --base-url URL --token TOKEN",
    "",
    "Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).",
    "See docs/uiux-2-0-polish.md.",
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
  throw new Error(`puppeteer-core is not importable. Install it or set STUDIO_PUPPETEER_CORE.\n${failures.join("\n")}`);
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

async function startFixture() {
  const child = spawn(
    process.env.STUDIO_PYTHON || "python3",
    [resolve("scripts/council_readiness_seed.py"), "--port", "0"],
    { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] },
  );
  let buffer = "";
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += String(chunk); });
  const ready = await new Promise((resolveReady, rejectReady) => {
    const timer = setTimeout(() => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)), 120000);
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const line = buffer.split("\n").find((row) => row.trim().startsWith("{"));
      if (!line) return;
      clearTimeout(timer);
      try {
        resolveReady(JSON.parse(line));
      } catch (_error) {
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      rejectReady(new Error(`The fixture exited with ${code}.\n${stderr}`));
    });
  });
  return { ...ready, child };
}

/**
 * The style facts that decide whether a control answered. Colour, border and shadow are a visible
 * answer; `transform` is recorded apart, because a control that only moves 1px has not told a
 * reduced-motion reader anything.
 */
const READ_KIND = (key) => {
  const el = document.querySelector(`[data-polish-kind="${key}"]`);
  if (!el) return null;
  const s = getComputedStyle(el);
  return {
    colour: `${s.backgroundColor}|${s.borderColor}|${s.color}|${s.boxShadow}|${s.opacity}|${s.backgroundImage}|${s.filter}`,
    transform: s.transform,
    cursor: s.cursor,
    transition: `${s.transitionDuration}|${s.transitionProperty}`,
    radius: s.borderTopLeftRadius,
    outline: `${s.outlineStyle}|${s.outlineWidth}|${s.outlineColor}`,
    boxShadow: s.boxShadow,
    hover: el.matches(":hover"),
    focus: el === document.activeElement,
  };
};

/** Enumerate one candidate per interactive kind that a pointer can actually reach in this pane. */
const COLLECT_KINDS = () => {
  // A checkbox is pressed by pressing its label, and the label is the target a finger actually hits,
  // so the label is collected as a control in its own right.
  const selector = "button, select, input:not([type=hidden]), textarea, summary, [role=button], a[href], [onclick], label:has(input)";
  // Marks from the previous pane are cleared first: a kind is a control in *this* pane, and a stale
  // mark would have this pane measuring the last one's controls.
  for (const stale of document.querySelectorAll("[data-polish-kind]")) stale.removeAttribute("data-polish-kind");
  const kinds = new Map();
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) continue;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    const style = getComputedStyle(el);
    if (style.visibility !== "visible" || style.display === "none" || style.pointerEvents === "none") continue;
    const cx = rect.x + rect.width / 2;
    const cy = rect.y + rect.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) continue;
    const under = document.elementFromPoint(cx, cy);
    if (!under || !(el === under || el.contains(under))) continue;
    const key = `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 2).join(".") || "(noclass)"}`;
    if (kinds.has(key)) continue;
    el.dataset.polishKind = key;
    kinds.set(key, { key, tag: el.tagName.toLowerCase(), type: (el.getAttribute("type") || "").toLowerCase() });
  }
  return [...kinds.values()];
};

/**
 * Force a pseudo-class on one kind and read what the stylesheet says about it.
 *
 * The pointer is not used. A walk that clicks every control to find out what a click looks like
 * *changes the app while measuring it* - a chip toggles, a starter card rewrites the composer, a
 * step card changes the selection - and every measurement after that is of a different pane. Forcing
 * `:hover`, `:active` and `:focus-visible` through the DevTools protocol asks the stylesheet the same
 * question with no side effects, which is also what makes the numbers repeatable.
 */
async function forcedStyle(client, page, key, pseudo, readKind) {
  const { root } = await client.send("DOM.getDocument", { depth: -1, pierce: false });
  const { nodeId } = await client.send("DOM.querySelector", {
    nodeId: root.nodeId,
    selector: `[data-polish-kind="${key}"]`,
  });
  if (!nodeId) return null;
  await client.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: pseudo });
  // The forced state reaches the style engine on the next paint, and a read taken before it lands
  // reports the resting style - which reads as "this control has no hover" and then credits the
  // *press* with the hover it finally saw.
  await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
  const state = await page.evaluate(readKind, key);
  await client.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: [] });
  return state;
}

/**
 * Some controls carry their ring on a box around them: the two composers turn the textarea's own
 * ring off and draw it on `.prompt-box` with `:focus-within`, which is a decision the keyboard story
 * made and this one keeps. A reader still sees a ring, so the question is "did anything the reader
 * can see change when this control took focus", not "does this element have an outline".
 */
const COMPOSED_RING = (key) => {
  const el = document.querySelector(`[data-polish-kind="${key}"]`);
  if (!el) return false;
  const read = (node) => {
    const s = getComputedStyle(node);
    return `${s.outlineStyle}|${s.outlineWidth}|${s.outlineColor}|${s.boxShadow}`;
  };
  const chain = [];
  for (let node = el.parentElement; node && chain.length < 3; node = node.parentElement) chain.push(node);
  // A control measured earlier may still hold focus, and a box that is already ringed cannot show a
  // ring being drawn: the comparison starts from nothing focused.
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  const before = chain.map(read);
  el.focus();
  const after = chain.map(read);
  el.blur();
  return after.some((value, index) => value !== before[index]);
};

async function measurePane(page, client, pane, args) {
  await page.evaluate((name) => switchTab(name), pane);
  await sleep(args.settleMs * 2);
  const collected = await page.evaluate(COLLECT_KINDS);
  const keys = collected.map((kind) => kind.key);
  const rows = [];
  for (const key of keys) {
    // The pane refreshes itself (telemetry, counts, statuses), so a coordinate read once at
    // collection time is a coordinate of a control that may have moved. Each kind is brought into
    // view and measured on its own, and a kind that cannot be reached this way is reported as such
    // rather than counted as a control with no hover.
    const point = await page.evaluate((wanted) => {
      const el = document.querySelector(`[data-polish-kind="${wanted}"]`);
      if (!el) return null;
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      const cx = r.x + r.width / 2;
      const cy = r.y + r.height / 2;
      const under = document.elementFromPoint(cx, cy);
      if (!under || !(el === under || el.contains(under))) return null;
      if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return null;
      return { x: cx, y: cy };
    }, key);
    const kind = collected.find((entry) => entry.key === key) || { tag: key.split(".")[0], type: "" };
    if (!point) {
      rows.push({ pane, key, tag: kind.tag, type: kind.type, reachable: false, hover: false, press: false, focusRing: false, cursor: "", transitionMs: 0, radius: "" });
      continue;
    }
    await sleep(60);
    const before = await page.evaluate(READ_KIND, key);
    if (!before) continue;
    await page.mouse.move(point.x, point.y);
    await sleep(170);
    const hovered = await page.evaluate(READ_KIND, key);
    await page.mouse.down();
    await sleep(90);
    const pressed = await page.evaluate(READ_KIND, key);
    await page.mouse.up();
    await page.mouse.move(2, 2);
    await sleep(50);
    const focused = await forcedStyle(client, page, key, ["focus", "focus-visible"], READ_KIND);
    const answered = Boolean(hovered && hovered.hover) && hovered.colour !== before.colour;
    const moved = Boolean(hovered) && (hovered.transform !== before.transform || hovered.filter !== before.filter);
    const pressAnswered = Boolean(pressed) && (pressed.colour !== (hovered ? hovered.colour : before.colour) || pressed.transform !== (hovered ? hovered.transform : before.transform));
    const ownRing = Boolean(focused) && (focused.outline !== before.outline || focused.boxShadow !== before.boxShadow);
    const ring = ownRing || (await page.evaluate(COMPOSED_RING, key));
    rows.push({
      pane,
      key,
      tag: kind.tag,
      type: kind.type,
      reachable: true,
      hover: answered,
      hoverTransformOnly: !answered && moved,
      press: pressAnswered,
      focusRing: ring,
      cursor: before.cursor,
      transitionMs: Number((before.transition.split("|")[0] || "0s").replace("s", "").split(",")[0]) * 1000,
      radius: before.radius,
    });
  }
  return rows;
}

async function run(args) {
  const puppeteer = await loadPuppeteer();
  const results = [];
  const failures = [];
  const record = (step, passed, detail) => {
    results.push({ step, passed, detail, status: passed ? "pass" : "fail" });
    if (!passed) failures.push(step);
    if (!args.json) console.log(`${passed ? "pass" : "FAIL"}  ${step}${detail ? ` — ${detail}` : ""}`);
  };
  // A checkbox or a radio is drawn by the platform and answers with its own tick, so a colour it
  // cannot change is not a defect. Everything else is measured.
  const ticks = (row) => TICKING.has(row.type);
  const typeable = (row) => row.tag === "textarea" || (row.tag === "input" && TYPEABLE.has(row.type));
  const exempt = (table, row) =>
    Boolean(table[row.key] || table[row.tag]) || (table === PRESS_EXEMPT && (ticks(row) || typeable(row)));

  let fixture = null;
  try {
    if (!args.baseUrl) {
      fixture = await startFixture();
      args.baseUrl = fixture.base_url;
      args.token = args.token || fixture.token;
    }
    const browser = await puppeteer.launch({
      executablePath: resolveChrome(),
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"],
    });
    try {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(`console: ${message.text()}`);
      });
      // Both themes are walked and captured: a polish story that only looks right on the dark
      // surface has not been checked at all, and the light palette re-points every colour.
      await page.evaluateOnNewDocument((key, token, theme) => {
        try {
          sessionStorage.setItem(key, token);
          localStorage.setItem("m5_theme", theme);
        } catch (_error) {}
      }, args.tokenKey, args.token, args.theme);
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 8);

      const client = await page.target().createCDPSession();
      await client.send("DOM.enable");
      await client.send("CSS.enable");
      // A walk that clicks every control to see what a click looks like changes the app while it is
      // measuring it. Clicks are stopped at the capture phase for the duration: `:active` still
      // applies with the button held down, and nothing the reader typed or chose is altered.
      await page.evaluate(() => {
        window.__polishBlock = (event) => {
          event.preventDefault();
          event.stopImmediatePropagation();
        };
        for (const type of ["click", "submit"]) window.addEventListener(type, window.__polishBlock, true);
      });
      const rows = [];
      for (const pane of PANES) rows.push(...(await measurePane(page, client, pane, args)));
      const kinds = rows.length;

      // ── 1. pointing at something changes something you can see ─────────────
      const silentHover = rows.filter((row) => !row.hover && !exempt(POINTER_EXEMPT, row) && !ticks(row));
      record(
        "every control a reader can point at answers the pointer",
        silentHover.length === 0,
        silentHover.map((row) => `${row.pane}:${row.key}`).slice(0, 8).join(", ") || `${kinds} kinds, all answer`,
      );
      const transformOnly = rows.filter((row) => row.hoverTransformOnly);
      record(
        "and answers with something other than movement, so reduced motion loses nothing",
        transformOnly.length === 0,
        transformOnly.map((row) => `${row.pane}:${row.key}`).slice(0, 6).join(", ") || "no kind relies on movement alone",
      );

      // ── 2. pressing one changes something ──────────────────────────────────
      const silentPress = rows.filter((row) => !row.press && !exempt(PRESS_EXEMPT, row));
      record(
        "pressing a control changes it, so the press is not a guess",
        silentPress.length === 0,
        silentPress.map((row) => `${row.pane}:${row.key}`).slice(0, 8).join(", ") || `${rows.filter((row) => row.press).length}/${kinds} kinds press`,
      );

      // ── 3. one focus ring for all of them ──────────────────────────────────
      const ringless = rows.filter((row) => !row.focusRing && !ticks(row));
      record(
        "every control that can take focus draws a ring",
        ringless.length === 0,
        ringless.map((row) => `${row.pane}:${row.key}`).slice(0, 8).join(", ") || "all focusable kinds ring",
      );

      // ── 4. the cursor says what the control does ───────────────────────────
      const noPointer = rows.filter((row) => !["input", "textarea", "select"].includes(row.tag) && row.cursor !== "pointer");
      record(
        "a control that can be pressed says so with the cursor",
        noPointer.length === 0,
        noPointer.map((row) => `${row.pane}:${row.key}=${row.cursor}`).slice(0, 8).join(", ") || "every pressable kind is a pointer",
      );

      // ── 5. one radius scale, one motion vocabulary ─────────────────────────
      const measured = rows.filter((row) => row.reachable);
      const radii = [...new Set(measured.map((row) => row.radius))].sort();
      const durations = [...new Set(measured.map((row) => row.transitionMs))].sort((a, b) => a - b);
      record(
        "the controls are drawn with the declared corner tokens, not with ad-hoc numbers",
        radii.every((value) => ["4px", "6px", "8px", "10px", "12px", "999px", "0px", "50%"].includes(value)),
        `computed radii: ${radii.join(", ")}`,
      );
      record(
        `no control animates for longer than ${MOTION_BUDGET_MS}ms`,
        durations.every((value) => value <= MOTION_BUDGET_MS),
        `computed durations (ms): ${durations.join(", ")}`,
      );

      // ── 6. reduced motion ──────────────────────────────────────────────────
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
      await sleep(args.settleMs);
      const reduced = await page.evaluate(() => {
        const out = [];
        for (const el of document.querySelectorAll(".btn, .chip, .mode-tab, .starter-card, summary, button")) {
          const s = getComputedStyle(el);
          const ms = Number((s.transitionDuration.split(",")[0] || "0s").replace("s", "")) * 1000;
          if (ms > 1) out.push(`${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 2).join(".")}=${ms}ms`);
        }
        return [...new Set(out)];
      });
      record(
        "a machine that asks for reduced motion is given no transitions",
        reduced.length === 0,
        reduced.slice(0, 6).join(", ") || "every transition is off",
      );
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
      await sleep(args.settleMs);

      // ── 7. the captures ────────────────────────────────────────────────────
      if (args.screenshots) {
        mkdirSync(args.screenshots, { recursive: true });
        await page.evaluate(() => switchTab("chat"));
        await sleep(args.settleMs * 2);
        const hoverShot = await page.evaluate(() => {
          const btn = document.getElementById("chatSendBtn");
          if (!btn) return null;
          const r = btn.getBoundingClientRect();
          return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        });
        if (hoverShot) {
          await page.mouse.move(hoverShot.x, hoverShot.y);
          await sleep(args.settleMs);
          await page.screenshot({ path: resolve(args.screenshots, `polish-${args.theme}-hover-primary.png`) });
        }
        await page.mouse.move(2, 2);
        await sleep(args.settleMs);
        await page.screenshot({ path: resolve(args.screenshots, `polish-${args.theme}-rest-chat.png`) });
        await page.evaluate(() => switchTab("start"));
        await sleep(args.settleMs * 2);
        await page.screenshot({ path: resolve(args.screenshots, `polish-${args.theme}-rest-start.png`) });
        await page.setViewport({ width: 360, height: 740, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
        await sleep(args.settleMs * 3);
        // A resize is not a tab change: without this the narrow capture says "start" in its name and
        // shows whichever pane the last interaction left on screen.
        await page.evaluate(() => switchTab("start"));
        await sleep(args.settleMs * 2);
        await page.screenshot({ path: resolve(args.screenshots, `polish-${args.theme}-narrow-start.png`) });
      }

      record("no page error occurred", errors.length === 0, errors.slice(0, 3).join(" | "));

      const theme = await page.evaluate(() => document.body.dataset.theme || "dark");
      record(
        "the captures were taken on the theme that was asked for",
        !args.screenshots || theme === args.theme,
        `asked ${args.theme}, page reports ${theme}`,
      );

      const summary = {
        theme,
        checks: results,
        kinds,
        hover: rows.filter((row) => row.hover).length,
        press: rows.filter((row) => row.press).length,
        rings: rows.filter((row) => row.focusRing).length,
        pointer: rows.filter((row) => row.cursor === "pointer").length,
        radii,
        durations,
        rows,
      };
      if (args.json) {
        console.log(JSON.stringify(summary, null, 2));
      } else {
        console.log(`\n${results.length - failures.length}/${results.length} checks passed — ${summary.hover}/${kinds} kinds answer the pointer, ${summary.press}/${kinds} press, ${summary.rings}/${kinds} ring`);
      }
      if (failures.length) process.exitCode = 1;
    } finally {
      await browser.close();
    }
  } finally {
    if (fixture && fixture.child) fixture.child.kill("SIGTERM");
  }
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(`polish walk: ${error.message}`);
  process.exit(1);
});
