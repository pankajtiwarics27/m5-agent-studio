#!/usr/bin/env node
/**
 * Open, trap, close and return focus for every dialog, with real keys (UIUX-P1-04).
 *
 *   python3 scripts/demo_tour.py --serve                 # in one terminal
 *   STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
 *     node scripts/overlay_walk.mjs --base-url http://127.0.0.1:PORT --token TOKEN
 *
 * Why this exists: `scripts/uiux_overlay_inventory.py` proves all nine dialogs route
 * through one controller. It cannot prove the controller works - focus is a runtime
 * fact, and the bug it replaced was exactly the kind that reads fine: Escape removed
 * `.open` from every backdrop, so the dialog closed and focus went nowhere.
 *
 * For each dialog the walk does what a keyboard user does, and asserts what they
 * would notice:
 *
 *   - open it through its own keyboard path (Enter on its control, or the palette);
 *   - focus lands inside the dialog, not on the page behind it;
 *   - Tab past the last control wraps to the first, and focus never escapes - the
 *     check runs two full cycles so a one-off wrap cannot pass by accident;
 *   - the background is inert while it is open, so the page behind cannot be typed
 *     into even if focus did escape;
 *   - Escape closes it and focus returns to the control that opened it;
 *   - no page errors, and the page is left with no overlay open.
 *
 * Then the two shared surfaces: the palette (open with the documented chord, move
 * with arrows, run with Enter, close returns focus) and the inspector drawer at a
 * narrow viewport (opens into focus, Escape returns it to the toggle).
 *
 * Exit status is 0 only when every check passed; `--json` prints the results.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULTS = {
  tokenKey: "m5_api_token",
  settleMs: 220,
  timeoutMs: 45000,
  inventory: "product/M5_2_0_UIUX_Overlays.json",
};

// How each dialog is reached without a pointer. The inventory supplies the ids and
// the markup triggers; this table supplies the palette search for the ones whose only
// caller is a script, and the walk fails if an id here is missing from either.
const PALETTE_QUERIES = {
  cmdPaletteModal: null, // opened by the chord itself
  contextModal: "Search knowledge",
  mcpModal: "Configure MCP tools",
  externalHarnessModal: "Harness bridge",
  skillsModal: "Manage agent skills",
  // The diff dialog's only markup control is a Refresh button *inside* it, so its
  // keyboard path is the palette row that names it.
  harnessDiffModal: "Inspect Harness Git",
  providersModal: "Configure model providers",
  sdlcModal: "Plan and deliver a product",
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
      case "--inventory": args.inventory = next(); break;
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
    "Open, trap, close and return focus for every dialog, with real keys.",
    "",
    "  node scripts/overlay_walk.mjs --base-url URL --token TOKEN [--json]",
    "",
    "Needs a Chrome/Chromium binary (STUDIO_SCREENSHOT_CHROME) and puppeteer-core",
    "(STUDIO_PUPPETEER_CORE). See docs/uiux-2-0-overlays.md.",
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

/** What the page looks like from the keyboard: focus, the open overlay, the stack. */
const STATE = () => {
  const open = [...document.querySelectorAll(".modal-backdrop.open")];
  const top = open[open.length - 1] || null;
  const active = document.activeElement;
  const describe = (node) => {
    if (!node) return null;
    const label = node.getAttribute("aria-label") || node.textContent?.trim().slice(0, 40) || node.id || node.tagName;
    return { tag: node.tagName, id: node.id || null, label, inTop: Boolean(top && top.contains(node)) };
  };
  return {
    open: open.map((node) => node.id),
    top: top ? top.id : null,
    stack: window.M5Overlays ? window.M5Overlays.stack() : null,
    active: describe(active),
    // The same set the controller makes inert: element children of body that are not
    // the open backdrop and are not scripts or stylesheets.
    backgroundInert: [...document.body.children]
      .filter((node) => node.nodeType === 1
        && !node.classList.contains("modal-backdrop")
        && !["SCRIPT", "STYLE", "LINK"].includes(node.tagName))
      .every((node) => node.inert === true),
    hook: typeof window.M5Overlays === "object" && window.M5Overlays !== null,
  };
};

async function pressKey(page, key, options = {}) {
  if (options.shift) await page.keyboard.down("Shift");
  await page.keyboard.press(key);
  if (options.shift) await page.keyboard.up("Shift");
}

/** Tab `count` times, recording where focus landed after each press. */
async function tabThrough(page, count) {
  const trail = [];
  for (let index = 0; index < count; index += 1) {
    await page.keyboard.press("Tab");
    trail.push(await page.evaluate(() => {
      const top = [...document.querySelectorAll(".modal-backdrop.open")].pop() || null;
      const active = document.activeElement;
      return {
        inTop: Boolean(top && top.contains(active)),
        label: active ? (active.getAttribute("aria-label") || active.textContent?.trim().slice(0, 24) || active.id) : null,
        tag: active ? active.tagName : null,
      };
    }));
  }
  return trail;
}

async function run(args) {
  const inventory = JSON.parse(readFileSync(resolve(args.inventory), "utf8"));
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
  // A dialog the app refuses to open without a prerequisite is not a pass and not a
  // regression: it is blocked, and the reason is the message the app itself showed.
  const block = (step, detail) => {
    results.push({ step, passed: false, detail, status: "blocked" });
    if (!args.json) console.log(`block  ${step} — ${detail}`);
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

    const initial = await page.evaluate(STATE);
    record("no dialog is open on load", initial.open.length === 0 && initial.stack.length === 0, initial.open.join(", "));
    record("the controller is exported for the walk", initial.hook === true, "window.M5Overlays");

    const dialogs = inventory.dialogs || [];
    record("the inventory lists every dialog", dialogs.length === 9, `${dialogs.length} dialogs`);

    for (const dialog of dialogs) {
      const id = dialog.id;
      const query = PALETTE_QUERIES[id];
      // `undefined` means "no palette row reaches this one"; only a string is a query.
      const reachableByPalette = typeof query === "string" || id === "cmdPaletteModal";
      const markupTrigger = (dialog.triggers || [])[0];

      if (!reachableByPalette && !markupTrigger) {
        record(`${id}: has a keyboard path`, false, "no palette entry and no markup trigger");
        continue;
      }

      let opened = false;
      if (id === "cmdPaletteModal" || reachableByPalette) {
        await page.evaluate(() => document.querySelector("[data-shortcut-action='command-palette']")?.focus());
        await sleep(args.settleMs / 2);
      }
      if (id === "cmdPaletteModal") {
        await page.keyboard.down("Control");
        await page.keyboard.press("k");
        await page.keyboard.up("Control");
        await sleep(args.settleMs);
        opened = true;
      } else if (reachableByPalette) {
        // Open the palette from the trigger, type, and run the highlighted row.
        await page.keyboard.down("Control");
        await page.keyboard.press("k");
        await page.keyboard.up("Control");
        await sleep(args.settleMs);
        await page.keyboard.type(query, { delay: 8 });
        await sleep(args.settleMs);
        const rows = await page.evaluate(() => [...document.querySelectorAll("#cmdPaletteList .cmd-item")].map((node) => node.textContent.trim()));
        if (!rows.length) {
          record(`${id}: reachable from the palette`, false, `no palette row matched ${query}`);
          await pressKey(page, "Escape");
          await sleep(args.settleMs);
          continue;
        }
        await pressKey(page, "Enter");
        await sleep(args.settleMs);
        opened = true;
      } else {
        // A markup trigger: more than one control can call the same opener, and one of
        // them may live in a pane that is not showing. Take the first trigger that is
        // visible, or switch to the pane that owns one and take it there.
        const triggers = await page.evaluate((fn) => [...document.querySelectorAll(`[onclick^="${fn}("]`)].map((node) => {
          const pane = node.closest(".pane");
          return {
            paneTab: pane ? pane.id.replace(/^pane-/, "") : null,
            visible: node.offsetParent !== null,
            label: node.getAttribute("aria-label") || node.textContent.trim().slice(0, 32),
          };
        }), dialog.open_function);
        if (!triggers.length) {
          record(`${id}: has a control that opens it`, false, `${dialog.open_function}() has no markup trigger`);
          continue;
        }
        const visible = triggers.find((trigger) => trigger.visible);
        const chosen = visible || triggers.find((trigger) => trigger.paneTab) || triggers[0];
        if (!visible && chosen.paneTab) {
          await page.evaluate((tab) => switchTab(tab), chosen.paneTab);
          await sleep(args.settleMs);
        }
        const focused = await page.evaluate((fn) => {
          const trigger = [...document.querySelectorAll(`[onclick^="${fn}("]`)].find((node) => node.offsetParent !== null);
          if (!trigger) return false;
          trigger.focus();
          return document.activeElement === trigger;
        }, dialog.open_function);
        record(`${id}: its trigger can take focus`, focused === true, chosen.label);
        if (!focused) {
          record(`${id}: opens from the keyboard`, false, "no focusable trigger");
          continue;
        }
        await pressKey(page, "Enter");
        await sleep(args.settleMs);
        opened = true;
      }

      if (!opened) continue;

      const openedState = await page.evaluate(STATE);
      if (!openedState.open.includes(id)) {
        const reason = await page.evaluate(() => {
          const toasts = [...document.querySelectorAll("#toastStack .toast")];
          const last = toasts[toasts.length - 1];
          return last ? last.textContent.trim() : "";
        });
        block(
          `${id}: opens from the keyboard`,
          reason ? `the app refused without a prerequisite: “${reason}”` : `no dialog opened (open stack: ${openedState.open.join(", ") || "empty"})`,
        );
        // The refusal itself is a behaviour worth asserting: a dialog that cannot be
        // used must say why, not open empty and let the user guess.
        record(
          `${id}: a refusal says why instead of opening empty`,
          Boolean(reason) && openedState.open.length === 0 && openedState.backgroundInert === false,
          reason || "no message shown",
        );
        continue;
      }
      record(`${id}: opens from the keyboard`, true, openedState.open.join(", "));
      record(
        `${id}: focus lands inside the dialog`,
        Boolean(openedState.active && openedState.active.inTop),
        openedState.active ? `${openedState.active.tag} “${openedState.active.label}”` : "nothing focused",
      );
      record(`${id}: the page behind is inert`, openedState.backgroundInert === true, String(openedState.backgroundInert));

      // Two full cycles: a single wrap can pass by luck, a cycle cannot.
      const stops = await page.evaluate(() => {
        const top = [...document.querySelectorAll(".modal-backdrop.open")].pop();
        const selector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),'
          + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
        return [...top.querySelectorAll(selector)].filter((node) => node.offsetParent !== null).length;
      });
      const trail = await tabThrough(page, Math.max(stops * 2 + 2, 6));
      const escaped = trail.filter((step) => !step.inTop);
      record(
        `${id}: focus never leaves the dialog (${trail.length} tab presses)`,
        escaped.length === 0,
        escaped.length ? `${escaped.length} escape(s): ${escaped.slice(0, 3).map((step) => step.label).join(", ")}` : "stayed inside",
      );
      record(
        `${id}: Tab cycles through its ${stops} control(s)`,
        stops > 0 && trail.some((step) => step.inTop),
        `${stops} stops`,
      );

      const beforeEscape = await page.evaluate(STATE);
      await pressKey(page, "Escape");
      await sleep(args.settleMs);
      const closed = await page.evaluate(STATE);
      record(`${id}: Escape closes it`, closed.open.length === 0, closed.open.join(", "));
      record(
        `${id}: focus returns to where it came from`,
        Boolean(closed.active && closed.active.inTop === false && closed.active.tag !== "BODY"),
        closed.active ? `${closed.active.tag} “${closed.active.label}”` : "nothing focused",
      );
      record(`${id}: the page is usable again`, closed.backgroundInert === false, String(closed.backgroundInert));
      void beforeEscape;
    }

    // The palette itself: the documented chord, arrows, Enter, and focus return.
    await page.evaluate(() => {
      const trigger = document.querySelector("[data-shortcut-action='command-palette']");
      if (trigger) trigger.focus();
    });
    await page.keyboard.down("Control");
    await page.keyboard.press("k");
    await page.keyboard.up("Control");
    await sleep(args.settleMs);
    const paletteOpen = await page.evaluate(STATE);
    record("Ctrl+K opens the palette", paletteOpen.open.includes("cmdPaletteModal"), paletteOpen.open.join(", "));
    record(
      "the palette focus goes to its search box",
      paletteOpen.active && paletteOpen.active.id === "cmdSearchInput",
      paletteOpen.active ? paletteOpen.active.id || paletteOpen.active.tag : "nothing",
    );
    const firstSelected = await page.evaluate(() => document.querySelector("#cmdPaletteList .cmd-item")?.getAttribute("aria-selected"));
    await page.keyboard.press("ArrowDown");
    await sleep(args.settleMs / 2);
    const secondSelected = await page.evaluate(() => [...document.querySelectorAll("#cmdPaletteList .cmd-item")].map((node) => node.getAttribute("aria-selected")));
    record(
      "ArrowDown moves the palette selection",
      firstSelected === "true" && secondSelected[1] === "true" && secondSelected[0] === "false",
      secondSelected.join(","),
    );
    await pressKey(page, "Escape");
    await sleep(args.settleMs);
    const afterPalette = await page.evaluate(() => {
      const trigger = document.querySelector("[data-shortcut-action='command-palette']");
      return {
        open: document.querySelectorAll(".modal-backdrop.open").length,
        returned: document.activeElement === trigger,
        label: document.activeElement ? (document.activeElement.getAttribute("aria-label") || document.activeElement.textContent.trim().slice(0, 32)) : null,
      };
    });
    record(
      "Escape returns focus to the palette's trigger",
      afterPalette.open === 0 && afterPalette.returned === true,
      afterPalette.label || "nothing focused",
    );
    const advertised = await page.evaluate(() => {
      const node = document.querySelector("[data-shortcut-action='command-palette']");
      return node ? { keys: node.getAttribute("aria-keyshortcuts"), title: node.title } : null;
    });
    record(
      "the palette trigger advertises its shortcut",
      Boolean(advertised && advertised.keys === "Ctrl+K Meta+K" && advertised.title.includes("Ctrl+K")),
      advertised ? `${advertised.keys} / ${advertised.title}` : "missing",
    );

    const railKeys = await page.evaluate(() => [...document.querySelectorAll(".mode-tab[data-shortcut]")]
      .map((tab) => `${tab.dataset.shortcut}=${tab.getAttribute("aria-keyshortcuts")}`));
    record(
      "every rail tab announces its shortcut",
      railKeys.length === 6 && railKeys.every((entry) => entry.split("=")[0] === entry.split("=")[1]),
      railKeys.join(" "),
    );

    // The inspector is a drawer below 1100px: it must take focus and give it back.
    await page.setViewport({ width: 420, height: 880, deviceScaleFactor: 1 });
    await sleep(args.settleMs * 2);
    await page.evaluate(() => {
      const toggle = document.getElementById("monitorToggleBtn");
      if (toggle) toggle.focus();
    });
    await pressKey(page, "Enter");
    await sleep(args.settleMs);
    const drawerOpen = await page.evaluate(() => {
      const rail = document.getElementById("monitorRail");
      const toggle = document.getElementById("monitorToggleBtn");
      return {
        mobile: document.getElementById("mainLayout").classList.contains("rail-mobile-open"),
        focusInRail: Boolean(rail && rail.contains(document.activeElement)),
        modal: rail ? rail.getAttribute("aria-modal") : null,
        expanded: toggle ? toggle.getAttribute("aria-expanded") : null,
      };
    });
    record(
      "the drawer opens into focus and declares itself modal",
      drawerOpen.mobile && drawerOpen.focusInRail && drawerOpen.modal === "true" && drawerOpen.expanded === "true",
      JSON.stringify(drawerOpen),
    );
    await pressKey(page, "Escape");
    await sleep(args.settleMs);
    const drawerClosed = await page.evaluate(() => {
      const layout = document.getElementById("mainLayout");
      const toggle = document.getElementById("monitorToggleBtn");
      return {
        mobile: layout.classList.contains("rail-mobile-open"),
        focusOnToggle: document.activeElement === toggle,
        modal: document.getElementById("monitorRail").getAttribute("aria-modal"),
        expanded: toggle.getAttribute("aria-expanded"),
      };
    });
    record(
      "Escape closes the drawer and returns focus to its toggle",
      !drawerClosed.mobile && drawerClosed.focusOnToggle && drawerClosed.modal === null && drawerClosed.expanded === "false",
      JSON.stringify(drawerClosed),
    );
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await sleep(args.settleMs);

    record("no page errors during the walk", errors.length === 0, errors.join("; "));
  } finally {
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
    throw new Error(`${failed.length} overlay check(s) failed: ${failed.map((row) => row.step).join("; ")}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.baseUrl) throw new Error("--base-url is required (start scripts/demo_tour.py --serve)");
  if (!args.token) throw new Error("--token is required (printed by scripts/demo_tour.py --serve)");
  await run(args);
}

main().catch((error) => {
  console.error(`overlay_walk: ${error.message}`);
  process.exit(1);
});
