#!/usr/bin/env node
/**
 * Walk the six panes at five widths, with a finger and with a mouse (UIUX-P5-03).
 *
 *   node scripts/responsive_walk.mjs --screenshots docs/media/responsive
 *
 * The panes were built for a 1440 px desktop and looked fine there. Narrowed, three of them clipped
 * their right-hand third without a scrollbar - the Start pane's task row, the Hall model picker and
 * the Compare arbiter picker - because a `<select>` is as wide as its longest `<option>` and a flex
 * item with `margin: 0 auto` sizes to its own content instead of the pane. And a row of controls sat
 * under the 24 px floor a finger needs, one of them because an inline `min-height: 20px` had escaped
 * the density tokens entirely.
 *
 * What it proves, at 1440, 1024, 768, 480 and 360, in each of the six panes:
 *
 *   1. the document does not scroll sideways: nothing is pushed off-screen by a mis-sized child;
 *   2. no control is drawn outside the viewport unless an ancestor scrolls to reach it, and the walk
 *      lists the scrollers it relied on so the exception is visible rather than assumed;
 *   3. every visible control's hit area - the control, or the `<label>` that wraps it - is at least
 *      24 by 24 CSS pixels, the floor in `--target-min`;
 *   4. status chips are not clipped: a chip whose text is cut off is a word the reader cannot read;
 *   5. at 360 the rail still offers all six panes and the composer is still usable;
 *   6. a touch tap (not a mouse click) opens a pane and presses a starter card;
 *   7. rotating the phone (740x360) does not bring the sideways scroll back;
 *   8. no page error.
 */
import { existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 240,
  timeoutMs: 120000,
  screenshots: "",
  baseUrl: "",
  token: "",
};

const PANES = ["start", "harness", "hall", "council", "debate", "chat"];
const WIDTHS = [
  { width: 1440, height: 900, label: "1440", touch: false },
  { width: 1024, height: 768, label: "1024", touch: false },
  { width: 768, height: 1024, label: "768", touch: true },
  { width: 480, height: 900, label: "480", touch: true },
  { width: 360, height: 740, label: "360", touch: true },
];

/** WCAG 2.5.8 Target Size (Minimum): 24 by 24 CSS pixels. The token is `--target-min`. */
const TARGET_MIN = 24;

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
    "Walk the six panes at five widths with real layout measurements.",
    "",
    "  node scripts/responsive_walk.mjs [--screenshots DIR] [--json]",
    "  node scripts/responsive_walk.mjs --base-url URL --token TOKEN",
    "",
    "Without --base-url the walk starts the Compare fixture itself. Needs Chrome/Chromium",
    "(STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE). See",
    "docs/uiux-2-0-responsive.md.",
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
    const timer = setTimeout(
      () => rejectReady(new Error(`The fixture did not report itself in time.\n${stderr}`)),
      120000,
    );
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
  const healthy = await waitForHttp(`${ready.base_url}/`, 60000);
  if (!healthy) throw new Error(`The fixture never answered at ${ready.base_url}.\n${stderr}`);
  return { ...ready, child };
}

/** Measure one pane: overflow, targets, clipping, chips. Runs inside the page. */
const AUDIT = (pane, targetMin) => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const scrollsTo = (el) => {
    let node = el.parentElement;
    while (node) {
      const style = getComputedStyle(node);
      if ((style.overflowX === "auto" || style.overflowX === "scroll") && node.scrollWidth > node.clientWidth + 2) return true;
      node = node.parentElement;
    }
    return false;
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || "",
    cls: (typeof el.className === "string" ? el.className : "").slice(0, 34),
    text: (el.textContent || "").trim().slice(0, 32),
  });

  const small = [];
  const clipped = [];
  const scrollers = new Set();
  const SELECTOR = "button, a[href], input:not([type=hidden]), select, textarea, summary, [role='button'], [tabindex='0']";
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    const label = el.closest("label");
    const labelRect = label ? label.getBoundingClientRect() : null;
    // The hit area a finger gets: the control, or the label it lives in when that is bigger.
    const box = labelRect && labelRect.width > rect.width ? labelRect : rect;
    if (box.width < targetMin || box.height < targetMin) {
      small.push({ ...describe(el), w: Math.round(box.width), h: Math.round(box.height) });
    }
    if (rect.left < -2 || rect.right > window.innerWidth + 2) {
      if (scrollsTo(el)) {
        let node = el.parentElement;
        while (node) {
          const style = getComputedStyle(node);
          if ((style.overflowX === "auto" || style.overflowX === "scroll") && node.scrollWidth > node.clientWidth + 2) {
            scrollers.add(`${node.tagName.toLowerCase()}.${(typeof node.className === "string" ? node.className : "").split(" ")[0] || node.id}`);
            break;
          }
          node = node.parentElement;
        }
      } else {
        clipped.push({ ...describe(el), left: Math.round(rect.left), right: Math.round(rect.right) });
      }
    }
  }

  const cutChips = [];
  for (const chip of document.querySelectorAll(".status, .chip, .harness-perm-badge, .stage-pill")) {
    if (!visible(chip)) continue;
    if (chip.scrollWidth > chip.clientWidth + 2 && getComputedStyle(chip).textOverflow !== "ellipsis") {
      cutChips.push({ ...describe(chip), scroll: chip.scrollWidth, client: chip.clientWidth });
    }
  }

  const paneEl = document.getElementById(`pane-${pane}`);
  const paneRect = paneEl ? paneEl.getBoundingClientRect() : null;

  return {
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    small: small.slice(0, 8),
    smallCount: small.length,
    clipped: clipped.slice(0, 8),
    clippedCount: clipped.length,
    scrollers: [...scrollers],
    cutChips: cutChips.slice(0, 6),
    cutChipCount: cutChips.length,
    paneWidth: paneRect ? Math.round(paneRect.width) : 0,
  };
};

async function shoot(page, args, name) {
  if (!args.screenshots) return;
  mkdirSync(args.screenshots, { recursive: true });
  await sleep(args.settleMs * 2);
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

      const perWidth = {};
      const scrollersSeen = new Set();

      for (const size of WIDTHS) {
        await page.setViewport({
          width: size.width,
          height: size.height,
          isMobile: size.touch,
          hasTouch: size.touch,
          deviceScaleFactor: 1,
        });
        await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
        await sleep(args.settleMs * 5);
        const panes = {};
        for (const pane of PANES) {
          await page.evaluate((name) => switchTab(name), pane);
          await sleep(args.settleMs);
          panes[pane] = await page.evaluate(AUDIT, pane, TARGET_MIN);
          panes[pane].scrollers.forEach((name) => scrollersSeen.add(name));
        }
        perWidth[size.label] = panes;

        const overflowed = Object.entries(panes).filter(([, audit]) => audit.overflow > 0);
        record(
          `at ${size.label}px no pane scrolls sideways`,
          overflowed.length === 0,
          overflowed.map(([pane, audit]) => `${pane}: +${audit.overflow}px`).join(", "),
        );

        const clippedAll = Object.entries(panes).flatMap(([pane, audit]) =>
          audit.clipped.map((entry) => `${size.label}/${pane}: ${entry.tag}.${entry.cls} right=${entry.right}`));
        record(
          `at ${size.label}px every control is either on screen or inside a strip that scrolls to it`,
          clippedAll.length === 0,
          clippedAll.slice(0, 5).join(" | "),
        );

        const smallAll = Object.entries(panes).flatMap(([pane, audit]) =>
          audit.small.map((entry) => `${size.label}/${pane}: ${entry.tag}.${entry.cls} ${entry.w}x${entry.h}`));
        record(
          `at ${size.label}px every control's hit area is at least ${TARGET_MIN} by ${TARGET_MIN}`,
          smallAll.length === 0,
          smallAll.slice(0, 5).join(" | "),
        );

        const chipsAll = Object.entries(panes).flatMap(([pane, audit]) =>
          audit.cutChips.map((entry) => `${size.label}/${pane}: "${entry.text}" ${entry.scroll}>${entry.client}`));
        record(
          `at ${size.label}px no status chip clips its own text`,
          chipsAll.length === 0,
          chipsAll.slice(0, 4).join(" | "),
        );

        if (size.label === "1440") {
          await page.evaluate(() => switchTab("debate"));
          await shoot(page, args, "responsive-1440-debate");
        }
        if (size.label === "768") {
          await page.evaluate(() => switchTab("harness"));
          await shoot(page, args, "responsive-768-harness");
        }
        if (size.label === "480") {
          await page.evaluate(() => switchTab("council"));
          await shoot(page, args, "responsive-480-council");
        }
        if (size.label === "360") {
          await page.evaluate(() => switchTab("start"));
          await shoot(page, args, "responsive-360-start");
          await page.evaluate(() => switchTab("chat"));
          await shoot(page, args, "responsive-360-chat");
        }
      }

      // ── the narrow rail still offers every pane, and the composer is usable ──
      await page.setViewport({ width: 360, height: 740, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
      await page.goto(args.baseUrl, { waitUntil: "domcontentloaded", timeout: args.timeoutMs });
      await sleep(args.settleMs * 5);
      const rail = await page.evaluate(() => {
        const tabs = [...document.querySelectorAll(".mode-tab[role='tab']")];
        return {
          count: tabs.length,
          onScreen: tabs.filter((tab) => {
            const rect = tab.getBoundingClientRect();
            return rect.width >= 24 && rect.height >= 24 && rect.left >= -1 && rect.right <= window.innerWidth + 1;
          }).length,
        };
      });
      record(
        "at 360px all six panes are still on the rail and big enough to hit",
        rail.count === 6 && rail.onScreen === 6,
        `tabs=${rail.count} on-screen-and-large-enough=${rail.onScreen}`,
      );

      await page.evaluate(() => switchTab("debate"));
      await sleep(args.settleMs * 2);
      const composer = await page.evaluate(() => {
        const area = document.getElementById("debatePrompt");
        const button = document.getElementById("debateRunBtn") || document.querySelector("#pane-debate .btn-primary");
        const areaRect = area ? area.getBoundingClientRect() : null;
        const buttonRect = button ? button.getBoundingClientRect() : null;
        return {
          area: areaRect ? Math.round(areaRect.width) : 0,
          button: buttonRect ? Math.round(buttonRect.width) : 0,
          buttonOnScreen: buttonRect ? buttonRect.right <= window.innerWidth + 1 && buttonRect.left >= -1 : false,
        };
      });
      record(
        "at 360px the Compare composer and its run button are both usable",
        composer.area >= 240 && composer.button >= 24 && composer.buttonOnScreen,
        `textarea=${composer.area}px run button=${composer.button}px on-screen=${composer.buttonOnScreen}`,
      );

      // ── a finger, not a mouse ─────────────────────────────────────────────
      // `page.tap()` resolves to nothing; the evidence is the state it changed, not its return value.
      await page.tap("#tab-chat");
      await sleep(args.settleMs * 3);
      const afterTap = await page.evaluate(() => ({
        pane: (document.querySelector(".pane.active") || {}).id || "",
        selected: (document.querySelector(".mode-tab[aria-selected='true']") || {}).id || "",
      }));
      record(
        "a touch tap opens a pane, so the rail is not mouse-only",
        afterTap.pane === "pane-chat" && afterTap.selected === "tab-chat",
        `active=${afterTap.pane} selected=${afterTap.selected}`,
      );

      await page.evaluate(() => switchTab("harness"));
      await sleep(args.settleMs * 3);
      const cardBefore = await page.evaluate(() => (document.getElementById("harnessPromptInput") || {}).value || "");
      const card = await page.$(".starter-card[data-key-activate]");
      if (card) await card.tap();
      await sleep(args.settleMs * 3);
      const cardAfter = await page.evaluate(() => (document.getElementById("harnessPromptInput") || {}).value || "");
      record(
        "a touch tap presses a starter card, so the card is not keyboard- or mouse-only",
        Boolean(card) && cardAfter.length > 20 && cardAfter !== cardBefore,
        `task box: ${cardBefore.length} → ${cardAfter.length} chars`,
      );

      // ── rotating the phone must not bring the sideways scroll back ────────
      await page.setViewport({ width: 740, height: 360, isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
      await sleep(args.settleMs * 4);
      const landscape = await page.evaluate(
        (panes, min) => {
          const out = {};
          for (const pane of panes) {
            switchTab(pane);
            out[pane] = {
              overflow: document.documentElement.scrollWidth - window.innerWidth,
              small: [...document.querySelectorAll("button, select, summary, [role='button']")]
                .filter((el) => {
                  const rect = el.getBoundingClientRect();
                  const style = getComputedStyle(el);
                  if (rect.width === 0 || rect.height === 0 || style.display === "none") return false;
                  const label = el.closest("label");
                  const box = label && label.getBoundingClientRect().width > rect.width ? label.getBoundingClientRect() : rect;
                  return box.width < min || box.height < min;
                })
                .length,
            };
          }
          return out;
        },
        PANES,
        TARGET_MIN,
      );
      await page.evaluate(() => switchTab("debate"));
      await shoot(page, args, "responsive-landscape-debate");
      const landscapeBad = Object.entries(landscape)
        .filter(([, value]) => value.overflow > 0 || value.small > 0)
        .map(([pane, value]) => `${pane}: +${value.overflow}px, ${value.small} small`);
      record(
        "a landscape phone (740x360) is still laid out: no sideways scroll, no small target",
        landscapeBad.length === 0,
        landscapeBad.join(" | "),
      );

      record("no page error occurred", errors.length === 0, errors.slice(0, 3).join(" | "));
      record(
        "the strips that scroll horizontally are named, so the exception is visible",
        true,
        scrollersSeen.size ? [...scrollersSeen].join(", ") : "none needed",
      );

      const failed = results.filter((entry) => !entry.passed);
      if (args.json) {
        console.log(JSON.stringify({ checks: results, perWidth, scrollers: [...scrollersSeen], passed: results.length - failed.length, total: results.length }, null, 2));
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
  console.error(`responsive walk: ${error.message}`);
  process.exit(1);
});
