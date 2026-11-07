#!/usr/bin/env node
/**
 * Walk the six panes with a keyboard and a screen reader's questions (UIUX-P5-02).
 *
 *   node scripts/a11y_walk.mjs --screenshots docs/media/keyboard-a11y
 *
 * The shell has had a keyboard contract since P1: the rail is a tablist, the arrow keys move it and
 * Alt+N opens a pane. What was never measured is everything after the rail. A control drawn as a
 * `div` with `onclick` cannot be reached by Tab at all; a textarea with `outline: none` takes focus
 * and shows nothing; a control with no name is announced as "button". This walk starts
 * `scripts/council_readiness_seed.py`, walks each pane's real tab order, and asks the three questions
 * a keyboard user and a screen reader ask of every stop.
 *
 * What it proves:
 *
 *   1. no control in any pane is mouse-only: every `[onclick]` is a native control or carries
 *      `role="button" tabindex="0" data-key-activate`, and the only exceptions are the modal
 *      backdrops, which Escape dismisses;
 *   2. every stop in the tab order has an accessible name (label, `aria-label`, `aria-labelledby`,
 *      text, or its own title);
 *   3. every stop shows where the focus is - its own ring, a shadow, or an ancestor's
 *      `:focus-within` ring - and nothing is focused that is hidden or zero-sized;
 *   4. Enter presses a starter card and Space presses a chip: the two keys a `div` control has to
 *      answer, through the one delegated handler rather than an inline attribute;
 *   5. the composer shows focus on the box, not only inside the textarea that turned its ring off;
 *   6. an overlay is a `role="dialog"` with `aria-modal`, takes focus, keeps Tab inside it, closes on
 *      Escape and gives focus back to the control that opened it;
 *   7. the rail and the panes agree: exactly one selected tab, one tabbable tab, and every pane is a
 *      `tabpanel` labelled by its own tab;
 *   8. no positive `tabindex`, no focusable inside `aria-hidden`, and no page error.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 220,
  timeoutMs: 90000,
  screenshots: "",
  baseUrl: "",
  token: "",
  port: 0,
};

const PANES = ["start", "harness", "hall", "council", "debate", "chat"];

/** A modal backdrop closes on a click outside; Escape is the keyboard path, and check 6 proves it. */
const DISMISS_ONLY = ["modal-backdrop"];

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
    "Walk the six panes with a keyboard and a screen reader's questions.",
    "",
    "  node scripts/a11y_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/a11y_walk.mjs --base-url URL --token TOKEN",
    "",
    "Without --base-url the walk starts the Compare fixture itself. Needs Chrome/Chromium",
    "(STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE). It refuses to report a",
    "walk it did not perform. See docs/uiux-2-0-keyboard-a11y.md.",
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
    [resolve("scripts/council_readiness_seed.py"), "--port", "0"],
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
        readyJson(JSON.parse(line));
      } catch (error) {
        rejectReady(new Error(`The fixture printed something unreadable: ${line}`));
      }
      function readyJson(value) { resolveReady(value); }
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

/** What the reader would be told about the element that has focus. */
const FOCUS_LEDGER = () => {
  const nameOf = (el) => {
    const aria = (el.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    const labelled = el.getAttribute("aria-labelledby");
    if (labelled) {
      const text = labelled
        .split(/\s+/)
        .map((id) => (document.getElementById(id) || {}).textContent || "")
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (text) return text;
    }
    const text = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (text) return text.slice(0, 80);
    const alt = el.querySelector ? el.querySelector("img[alt]") : null;
    if (alt && alt.alt.trim()) return alt.alt.trim();
    const title = (el.getAttribute("title") || "").trim();
    if (title) return title;
    const label = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
    if (label && label.textContent.trim()) return label.textContent.trim();
    const wrapped = el.closest ? el.closest("label") : null;
    if (wrapped && wrapped.textContent.trim()) return wrapped.textContent.replace(/\s+/g, " ").trim();
    const placeholder = (el.getAttribute("placeholder") || "").trim();
    if (placeholder) return placeholder;
    return "";
  };
  const ringOf = (el) => {
    // The ring may be the control's own or an ancestor's (`:focus-within` on the composer box).
    let node = el;
    for (let depth = 0; node && depth < 4; depth += 1, node = node.parentElement) {
      const style = getComputedStyle(node);
      const outlined = style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2;
      const shadowed = style.boxShadow && style.boxShadow !== "none";
      if (outlined || shadowed) return { ring: true, by: depth === 0 ? "self" : "ancestor", style: `${style.outlineStyle}/${style.outlineWidth}` };
    }
    return { ring: false, by: "", style: "" };
  };
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const box = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || "",
    cls: (typeof el.className === "string" ? el.className : "").slice(0, 40),
    name: nameOf(el),
    ring: ringOf(el),
    hidden: style.display === "none" || style.visibility === "hidden" || (box.width === 0 && box.height === 0),
    pane: (el.closest("[role='tabpanel']") || {}).id || "",
  };
};

async function shoot(page, args, name) {
  if (!args.screenshots) return;
  mkdirSync(args.screenshots, { recursive: true });
  await sleep(args.settleMs * 3);
  await page.screenshot({ path: resolve(args.screenshots, `${name}.png`) });
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

      // ── 1. no control is mouse-only ───────────────────────────────────────
      const mouseOnly = await page.evaluate((dismissOnly) => {
        return [...document.querySelectorAll("[onclick]")]
          .filter((el) => !/^(BUTTON|A|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(el.tagName))
          .filter((el) => el.getAttribute("role") !== "button" || !el.hasAttribute("tabindex") || !el.hasAttribute("data-key-activate"))
          .filter((el) => !dismissOnly.includes((typeof el.className === "string" ? el.className : "").split(" ")[0]))
          .map((el) => ({
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === "string" ? el.className : "").slice(0, 40),
            id: el.id,
            text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 48),
          }));
      }, DISMISS_ONLY);
      record(
        "no control is mouse-only: every click target is a native control or a reachable button",
        mouseOnly.length === 0,
        mouseOnly.slice(0, 6).map((entry) => `${entry.tag}.${entry.cls}: ${entry.text}`).join(" | "),
      );
      const dismissOnly = await page.evaluate((dismissOnly) => {
        return [...document.querySelectorAll("[onclick]")]
          .filter((el) => dismissOnly.includes((typeof el.className === "string" ? el.className : "").split(" ")[0]))
          .length;
      }, DISMISS_ONLY);
      record(
        "the only exceptions are the modal backdrops, which Escape dismisses",
        dismissOnly >= 1,
        `${dismissOnly} backdrops`,
      );

      // ── 2/3. every stop in each pane's tab order ──────────────────────────
      const perPane = {};
      let unnamed = [];
      let ringless = [];
      let hiddenStops = [];
      for (const pane of PANES) {
        await page.evaluate((name) => switchTab(name), pane);
        await sleep(args.settleMs * 3);
        await page.evaluate((name) => {
          const section = document.getElementById(`pane-${name}`);
          const first = section && section.querySelector("button, input, select, textarea, a[href], summary, [tabindex='0']");
          (first || section || document.body).focus();
        }, pane);
        const stops = [];
        for (let i = 0; i < 22; i += 1) {
          await page.keyboard.press("Tab");
          await sleep(30);
          const stop = await page.evaluate(FOCUS_LEDGER);
          if (!stop) break;
          stops.push(stop);
        }
        perPane[pane] = stops;
        for (const stop of stops) {
          if (!stop.name) unnamed.push(`${pane}:${stop.tag}#${stop.id}.${stop.cls}`);
          if (!stop.ring.ring) ringless.push(`${pane}:${stop.tag}#${stop.id}.${stop.cls}`);
          if (stop.hidden) hiddenStops.push(`${pane}:${stop.tag}#${stop.id}`);
        }
        await page.evaluate((name) => switchTab(name), pane);
      }
      const stopsRead = Object.values(perPane).reduce((total, stops) => total + stops.length, 0);
      record(
        "every stop in the tab order has a name a screen reader can read",
        stopsRead >= 60 && unnamed.length === 0,
        unnamed.slice(0, 6).join(", ") || `${stopsRead} stops, all named`,
      );
      record(
        "every stop shows where the focus is",
        ringless.length === 0,
        ringless.slice(0, 6).join(", ") || `${stopsRead} stops, all with a ring`,
      );
      record(
        "nothing hidden or zero-sized takes focus",
        hiddenStops.length === 0,
        hiddenStops.slice(0, 4).join(", "),
      );

      // ── 4. Enter and Space press the controls that are not native ─────────
      await page.evaluate(() => {
        switchTab("harness");
        startFreshHarnessDraft();
      });
      await sleep(args.settleMs * 3);
      // The examples are a real native disclosure. Open it from the keyboard and
      // use Tab to reach its first card; focusing a descendant of the closed
      // disclosure would report a defect in the test, not in the product.
      await page.focus(".harness-examples > summary");
      await page.keyboard.press("Space");
      await page.waitForFunction(() => document.querySelector(".harness-examples")?.open === true);
      await page.keyboard.press("Tab");
      const cardFocused = await page.evaluate(() => {
        const card = document.querySelector(".starter-card[data-key-activate]");
        return Boolean(card && document.activeElement === card);
      });
      await page.keyboard.press("Enter");
      await sleep(args.settleMs * 4);
      const cardActivated = await page.evaluate(() => ({
        task: (document.getElementById("harnessPromptInput") || {}).value || "",
        focused: document.activeElement ? (document.activeElement.getAttribute("role") || document.activeElement.tagName.toLowerCase()) : "",
      }));
      record(
        "Enter presses a starter card, and the card is reachable by Tab",
        cardFocused && cardActivated.task.trim().length > 20,
        `card=${cardFocused} task=${cardActivated.task.trim().slice(0, 40)}…`,
      );
      await shoot(page, args, "a11y-card-focus");

      // Provider presets live in the Providers dialog (not the MCP hub). Open
      // that actual dialog, then Space a reachable preset chip and verify the
      // provider alias field it owns. This is the same action as the pointer.
      await page.evaluate(() => openProvidersModal());
      await page.waitForSelector("#providersModal.open .chip[data-key-activate]", { timeout: 10000 });
      await sleep(args.settleMs * 3);
      const chipPressed = await page.evaluate(() => {
        const chip = document.querySelector('#providersModal .chip[data-key-activate]');
        if (!chip) return { found: false };
        chip.focus();
        const field = document.getElementById("provAlias");
        return {
          found: true,
          focused: document.activeElement === chip,
          role: chip.getAttribute("role"),
          tabindex: chip.getAttribute("tabindex"),
          before: field ? field.value : "",
          text: (chip.textContent || "").trim(),
        };
      });
      if (chipPressed.found) {
        await page.keyboard.press("Space");
        await sleep(args.settleMs * 4);
      }
      const chipState = await page.evaluate(() => {
        const chip = document.querySelector('#providersModal .chip[data-key-activate]');
        const field = document.getElementById("provAlias");
        return { role: chip ? chip.getAttribute("role") : "", tabindex: chip ? chip.getAttribute("tabindex") : "", after: field ? field.value : "" };
      });
      record(
        "Space presses a chip, and the chip says it is a button rather than a decoration",
        chipPressed.found && chipPressed.focused && chipState.role === "button" && chipState.tabindex === "0"
          && chipState.after.length > 0 && chipState.after !== chipPressed.before,
        `chip "${chipPressed.text}" → provAlias "${chipState.after}" (was "${chipPressed.before}")`,
      );
      await shoot(page, args, "a11y-chip-focus");
      await page.keyboard.press("Escape");
      await sleep(args.settleMs * 4);

      // ── 5. the composer shows focus on the box ────────────────────────────
      await page.evaluate(() => switchTab("debate"));
      await sleep(args.settleMs * 3);
      await page.focus("#debatePrompt");
      await sleep(args.settleMs * 2);
      const composer = await page.evaluate(() => {
        const area = document.getElementById("debatePrompt");
        const box = area ? area.closest(".prompt-box") : null;
        const style = box ? getComputedStyle(box) : null;
        return {
          focused: document.activeElement === area,
          boxOutline: style ? `${style.outlineStyle}/${style.outlineWidth}` : "",
          boxRing: Boolean(style) && style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2,
        };
      });
      await shoot(page, args, "a11y-composer-focus");
      record(
        "the composer shows the focus on the box, not only inside the textarea",
        composer.focused && composer.boxRing,
        `box outline ${composer.boxOutline}`,
      );

      // ── 6. an overlay takes focus, keeps it, and gives it back ────────────
      // Whatever holds focus when the dialog opens is what has to hold it again when the dialog
      // closes; the walk records it rather than assuming it is the composer.
      const opener = await page.evaluate(() => {
        const button = document.querySelector('[data-shortcut-action="command-palette"], #cmdPaletteBtn, .search-trigger');
        if (button) button.focus();
        else document.getElementById("debatePrompt").focus();
        const el = document.activeElement;
        if (el && el !== document.body) el.setAttribute("data-a11y-opener", "1");
        return { id: el ? el.id : "", tag: el ? el.tagName.toLowerCase() : "" };
      });
      await page.keyboard.down("Control");
      await page.keyboard.press("k");
      await page.keyboard.up("Control");
      await sleep(args.settleMs * 6);
      const overlayOpen = await page.evaluate(() => {
        const dialog = document.querySelector('#cmdPaletteModal [role="dialog"]');
        return {
          dialog: Boolean(dialog),
          modal: dialog ? dialog.getAttribute("aria-modal") : "",
          focusInside: Boolean(dialog && dialog.contains(document.activeElement)),
          focusTag: document.activeElement ? document.activeElement.tagName.toLowerCase() : "",
        };
      });
      await shoot(page, args, "a11y-overlay-focus");
      let trapped = true;
      for (let i = 0; i < 6; i += 1) {
        await page.keyboard.press("Tab");
        await sleep(40);
        const inside = await page.evaluate(() => {
          const dialog = document.querySelector('#cmdPaletteModal [role="dialog"]');
          return Boolean(dialog && dialog.contains(document.activeElement));
        });
        trapped = trapped && inside;
      }
      await page.keyboard.press("Escape");
      await sleep(args.settleMs * 5);
      const overlayClosed = await page.evaluate(() => {
        const dialog = document.querySelector('#cmdPaletteModal [role="dialog"]');
        const open = dialog ? Boolean(dialog.closest(".open")) : false;
        const el = document.activeElement;
        const returned = Boolean(el) && el.getAttribute("data-a11y-opener") === "1";
        if (el) el.removeAttribute("data-a11y-opener");
        return { open, returned, focus: el ? el.id || el.tagName.toLowerCase() : "" };
      });
      record(
        "an overlay is a modal dialog that takes focus and keeps Tab inside it",
        overlayOpen.dialog && overlayOpen.modal === "true" && overlayOpen.focusInside && trapped,
        `dialog=${overlayOpen.dialog} modal=${overlayOpen.modal} inside=${overlayOpen.focusInside} trapped=${trapped}`,
      );
      record(
        "Escape closes the overlay and gives focus back to where it was",
        !overlayClosed.open && overlayClosed.returned,
        `open=${overlayClosed.open} returned=${overlayClosed.returned} focus=${overlayClosed.focus || "none"} (opened from ${opener.tag}#${opener.id})`,
      );

      // ── 7. the rail and the panes agree ──────────────────────────────────
      await page.focus("#tab-debate");
      await page.keyboard.press("ArrowDown");
      await sleep(args.settleMs * 3);
      const rail = await page.evaluate(() => {
        const tabs = [...document.querySelectorAll(".mode-tab[role='tab']")];
        const panes = [...document.querySelectorAll("[role='tabpanel'][id^='pane-']")];
        const labelled = panes.filter((panel) => {
          const id = panel.getAttribute("aria-labelledby");
          const tab = id ? document.getElementById(id) : null;
          return Boolean(tab) && tab.getAttribute("aria-controls") === panel.id;
        });
        return {
          tabs: tabs.length,
          selected: tabs.filter((tab) => tab.getAttribute("aria-selected") === "true").length,
          tabbable: tabs.filter((tab) => tab.tabIndex === 0).length,
          panes: panes.length,
          labelled: labelled.length,
          positive: [...document.querySelectorAll("[tabindex]")].filter((el) => Number(el.getAttribute("tabindex")) > 0).length,
        };
      });
      record(
        "the rail keeps one selected tab, one tabbable tab, and every pane labelled by its own tab",
        rail.tabs === 6 && rail.selected === 1 && rail.tabbable === 1 && rail.panes === 6 && rail.labelled === 6,
        JSON.stringify(rail),
      );
      record(
        "no control is pushed to the front of the tab order with a positive tabindex",
        rail.positive === 0,
        `positive tabindex: ${rail.positive}`,
      );
      const hiddenFocusables = await page.evaluate(
        () => [...document.querySelectorAll('[aria-hidden="true"]')]
          .reduce((total, node) => total + node.querySelectorAll('a[href], button, input, select, textarea, [tabindex]').length, 0),
      );
      record(
        "nothing focusable hides inside an `aria-hidden` subtree",
        hiddenFocusables === 0,
        `${hiddenFocusables} focusable descendants of aria-hidden`,
      );

      // ── 8. narrow, and no page error ──────────────────────────────────────
      await page.evaluate(() => switchTab("debate"));
      await page.setViewport({ width: 480, height: 900, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 5);
      await page.evaluate(() => document.getElementById("debatePrompt").focus());
      await shoot(page, args, "a11y-narrow-focus");
      const narrow = await page.evaluate(() => {
        const box = document.getElementById("debatePrompt").closest(".prompt-box");
        const style = box ? getComputedStyle(box) : null;
        return {
          overflow: document.documentElement.scrollWidth - window.innerWidth,
          ring: Boolean(style) && style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2,
        };
      });
      record(
        "the focus ring survives the narrow layout",
        narrow.overflow <= 0 && narrow.ring,
        `overflow=${narrow.overflow} ring=${narrow.ring}`,
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
  console.error(`keyboard/a11y walk: ${error.message}`);
  process.exit(1);
});
