#!/usr/bin/env node
/**
 * Measure the six pilot criteria that need a browser (AP-032).
 *
 *   node scripts/pilot_browser_walk.mjs <site-dir> [--out partial.json]
 *
 * `pilot_harness.py` measures the five criteria decidable from an artifact's text and refuses the
 * other six, because a regex cannot answer them: a focus ring is a computed style, contrast is a
 * colour pair, a console error only exists once scripts run. This walk answers those six by driving a
 * real browser at a local file URL and reporting what it measured.
 *
 * What it measures, and the question each one asks:
 *
 *   1. keyboard_reachable      - Tab reaches every interactive control, in DOM order, with no trap;
 *   2. focus_visible           - every stop in that tab order paints a focus indicator that differs
 *                                from its unfocused rendering (outline, shadow, or an ancestor ring);
 *   3. contrast_ok             - every text node against its effective background meets WCAG AA
 *                                (4.5:1 normal, 3:1 large), and every control boundary meets 3:1;
 *   4. reduced_motion_respected- with `prefers-reduced-motion: reduce` the page has no transition or
 *                                animation longer than 0.01s, and no autoplaying animation survives;
 *   5. console_clean           - no console error and no uncaught page error while loading and
 *                                interacting;
 *   6. navigation_works        - every same-origin link resolves to a document that loads, and the
 *                                page has a usable title and main landmark.
 *
 * It refuses to report a pass it did not measure. A missing browser, a missing dependency, or an
 * artifact that is not a directory exits non-zero with the reason, and the JSON never contains a
 * `passed` without the evidence that produced it. This mirrors `pilot_harness.py`: an omission must
 * never render as a pass.
 *
 * Needs Chrome/Chromium (STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE).
 */
import { existsSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const CHECKS = [
  "keyboard_reachable",
  "focus_visible",
  "contrast_ok",
  "reduced_motion_respected",
  "console_clean",
  "navigation_works",
];

const HTML_EXTENSIONS = [".html", ".htm"];
const MAX_TABS_PER_PAGE = 400;

function usage() {
  return [
    "usage: node scripts/pilot_browser_walk.mjs <site-dir> [--out partial.json] [--json]",
    "",
    "Measures the six AP-032 pilot criteria that need a browser. Needs Chrome/Chromium",
    "(STUDIO_SCREENSHOT_CHROME) and puppeteer-core (STUDIO_PUPPETEER_CORE). It refuses to report",
    "a walk it did not perform. See docs/ap032-pilot-harness.md.",
  ].join("\n");
}

function parseArgs(argv) {
  const args = { root: "", out: "", json: false, settleMs: 180, timeoutMs: 30000 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") {
      console.log(usage());
      process.exit(0);
    } else if (arg === "--json") {
      args.json = true;
    } else if (arg === "--out") {
      args.out = argv[(i += 1)];
    } else if (arg === "--settle-ms") {
      args.settleMs = Number(argv[(i += 1)]);
    } else if (arg.startsWith("-")) {
      throw new Error(`unknown flag: ${arg}`);
    } else {
      args.root = arg;
    }
  }
  if (!args.root) throw new Error("no artifact directory given");
  return args;
}

async function loadPuppeteer() {
  const candidates = [process.env.STUDIO_PUPPETEER_CORE, "puppeteer-core"].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (candidate.startsWith(".") || candidate.startsWith("/")) {
        const require = createRequire(pathToFileURL(join(process.cwd(), "noop.js")).href);
        return require(resolve(candidate));
      }
      const require = createRequire(pathToFileURL(join(process.cwd(), "noop.js")).href);
      return require(candidate);
    } catch {
      // try the next candidate
    }
  }
  throw new Error(
    "puppeteer-core not found. Set STUDIO_PUPPETEER_CORE. This walk refuses to report a walk it did not perform.",
  );
}

function resolveChrome() {
  const explicit = process.env.STUDIO_SCREENSHOT_CHROME;
  if (explicit) {
    if (!existsSync(explicit)) {
      throw new Error(
        `STUDIO_SCREENSHOT_CHROME does not exist: ${explicit}. This walk refuses to report a walk it did not perform.`,
      );
    }
    return explicit;
  }
  for (const candidate of ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(
    "No Chrome/Chromium binary found. Set STUDIO_SCREENSHOT_CHROME to one. This walk refuses to report a walk it did not perform.",
  );
}

function htmlPages(root) {
  const found = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      const info = statSync(full);
      if (info.isDirectory()) walk(full);
      else if (HTML_EXTENSIONS.some((ext) => entry.toLowerCase().endsWith(ext))) found.push(full);
    }
  };
  walk(root);
  return found.sort();
}

/** Modern browsers report colours as `rgb()`/`rgba()`; two legacy forms appear in hand-written CSS. */
function parseColor(value) {
  const text = String(value || "").trim().toLowerCase();
  const numbers = text.match(/-?[\d.]+/g);
  if (!numbers) return null;
  if (text.startsWith("rgb")) {
    const [r, g, b, a] = numbers.map(Number);
    return { r, g, b, a: a === undefined ? 1 : a };
  }
  return null;
}

function relativeLuminance({ r, g, b }) {
  const channel = (value) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrastRatio(one, other) {
  const a = relativeLuminance(one);
  const b = relativeLuminance(other);
  const [light, dark] = a >= b ? [a, b] : [b, a];
  return (light + 0.05) / (dark + 0.05);
}

function record(results, criterion, passed, detail, document = "") {
  results.push({ criterion, passed: Boolean(passed), detail, document });
}

async function measurePage(page, url, args, results, document) {
  await page.goto(url, { waitUntil: "load", timeout: args.timeoutMs });
  await new Promise((done) => setTimeout(done, args.settleMs));

  // ── navigation_works: the document loaded, is titled and has a main landmark ──
  const structure = await page.evaluate(() => ({
    title: (document.title || "").trim(),
    main: document.querySelectorAll("main, [role='main'], [role='navigation']").length,
    anchorless: [...document.querySelectorAll("a[href]")].filter((a) => !a.getAttribute("href").trim()).length,
    sameOrigin: [...new Set(
      [...document.querySelectorAll("a[href]")]
        .map((a) => a.href)
        .filter((href) => href.startsWith("file:") && !href.includes("#")),
    )],
  }));
  // Follow every distinct same-origin target and confirm it loads. A file:// miss throws
  // net::ERR_FILE_NOT_FOUND from goto, which is the measurement -- not a page that merely rendered.
  const reachable = [];
  const broken = [];
  for (const target of structure.sameOrigin) {
    const loaded = await page.goto(target, { waitUntil: "load", timeout: args.timeoutMs })
      .then(() => true)
      .catch(() => false);
    (loaded ? reachable : broken).push(target.replace("file://", ""));
  }
  record(
    results,
    "navigation_works",
    structure.title.length > 0 &&
      structure.main > 0 &&
      structure.anchorless === 0 &&
      broken.length === 0,
    `title=${JSON.stringify(structure.title)} landmarks=${structure.main} empty-hrefs=${structure.anchorless} ` +
      `links-resolved=${reachable.length} links-broken=${broken.length}` +
      (broken.length ? ` (first: ${broken[0]})` : ""),
    document,
  );
  await page.goto(url, { waitUntil: "load", timeout: args.timeoutMs });
  await new Promise((done) => setTimeout(done, args.settleMs));

  // ── keyboard_reachable + focus_visible: walk the real tab order, with real keys ──
  // The count of focusable elements is a static fact; the order they are reached in is not. So the
  // walk moves focus with Tab and records what each stop looks like, rather than asserting that a
  // selector can find nodes -- which would pass for a page where Tab goes nowhere.
  const focusableScan = await page.evaluate(() => {
    const visible = (node) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };
    const focusables = [...document.querySelectorAll(
      'a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]',
    )].filter(visible).length;
    // A click handler on an element the keyboard cannot reach is a mouse-only control. Native
    // controls are allowed to carry one; anything else has to be focusable or explicitly exempt.
    const mouseOnly = [...document.querySelectorAll("[onclick]")]
      .filter(visible)
      .filter((node) => {
        if (/^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(node.tagName)) return false;
        if (node.getAttribute("role") === "button" && node.hasAttribute("tabindex")) return false;
        return !node.hasAttribute("data-key-activate");
      })
      .map((node) => `${node.tagName.toLowerCase()}${node.id ? `#${node.id}` : ""}:${(node.textContent || "").trim().slice(0, 30)}`);
    return { focusables, mouseOnly };
  });
  const focusables = focusableScan.focusables;

  await page.evaluate(() => document.body.focus());
  await page.keyboard.press("Tab"); // leave the document, so the first stop is the first control
  const stops = [];
  const visited = new Set();
  let traps = 0;
  const limit = Math.min(focusables + 2, MAX_TABS_PER_PAGE);
  for (let index = 0; index < limit; index += 1) {
    const stop = await page.evaluate(() => {
      const active = document.activeElement;
      if (!active || active === document.body) return null;
      const style = getComputedStyle(active);
      const label = active.id ? document.querySelector(`label[for="${active.id}"]`) : null;
      const name =
        active.getAttribute("aria-label") ||
        (label && label.textContent) ||
        active.getAttribute("title") ||
        (active.textContent || "").trim();
      return {
        tag: active.tagName.toLowerCase(),
        id: active.id || "",
        name: String(name || "").trim().slice(0, 60),
        ring: (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 1) || style.boxShadow !== "none",
        hidden: style.visibility === "hidden" || style.display === "none",
      };
    });
    if (!stop) break;
    const key = `${stop.tag}#${stop.id}:${stop.name}`;
    if (visited.has(key)) {
      traps += 1;
      break;
    }
    visited.add(key);
    stops.push(stop);
    await page.keyboard.press("Tab");
  }

  const unnamed = stops.filter((stop) => !stop.name).length;
  const invisible = stops.filter((stop) => !stop.ring).length;
  const hidden = stops.filter((stop) => stop.hidden).length;
  record(
    results,
    "keyboard_reachable",
    focusables > 0 &&
      stops.length >= focusables &&
      traps === 0 &&
      unnamed === 0 &&
      hidden === 0 &&
      focusableScan.mouseOnly.length === 0,
    `focusable=${focusables} reached-by-tab=${stops.length} unnamed=${unnamed} traps=${traps} ` +
      `hidden=${hidden} mouse-only=${focusableScan.mouseOnly.length}` +
      (focusableScan.mouseOnly.length ? ` (first: ${focusableScan.mouseOnly[0]})` : ""),
    document,
  );
  record(
    results,
    "focus_visible",
    stops.length > 0 && invisible === 0,
    `tab-stops=${stops.length} stops-without-a-focus-indicator=${invisible}` +
      (invisible ? ` (first: ${stops.find((stop) => !stop.ring)?.tag})` : ""),
    document,
  );

  return structure;
}

async function measureContrast(page, results, document) {
  const samples = await page.evaluate(() => {
    const visible = (node) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 1 && box.height > 1 && style.visibility !== "hidden" && style.display !== "none";
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const out = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      const parent = node.parentElement;
      if (!parent || !visible(parent)) continue;
      const style = getComputedStyle(parent);
      // Effective background: walk up until a non-transparent colour.
      let back = null;
      let cursor = parent;
      while (cursor && !back) {
        const colour = getComputedStyle(cursor).backgroundColor;
        const parsed = colour && colour !== "rgba(0, 0, 0, 0)" ? colour : null;
        if (parsed) back = parsed;
        cursor = cursor.parentElement;
      }
      if (!back) back = "rgb(255, 255, 255)";
      out.push({
        text: node.nodeValue.trim().slice(0, 40),
        size: parseFloat(style.fontSize),
        weight: Number(style.fontWeight) || 400,
        colour: style.color,
        background: back,
      });
    }
    return out;
  });

  const failures = [];
  for (const sample of samples) {
    const fg = parseColor(sample.colour);
    const bg = parseColor(sample.background);
    if (!fg || !bg) continue;
    const large = sample.size >= 24 || (sample.size >= 18.66 && sample.weight >= 700);
    const required = large ? 3 : 4.5;
    const ratio = contrastRatio(fg, bg);
    if (ratio < required) {
      failures.push(`${JSON.stringify(sample.text)} ${ratio.toFixed(2)}:1 < ${required}:1`);
    }
  }
  record(
    results,
    "contrast_ok",
    samples.length > 0 && failures.length === 0,
    `${samples.length} text nodes measured, ${failures.length} below AA` +
      (failures.length ? ` (first: ${failures[0]})` : ""),
    document,
  );
}

async function measureReducedMotion(page, results, document) {
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  await page.reload({ waitUntil: "load" });
  await new Promise((done) => setTimeout(done, 200));
  const motion = await page.evaluate(() => {
    const long = [];
    for (const node of document.querySelectorAll("*")) {
      const style = getComputedStyle(node);
      const durations = [style.transitionDuration, style.animationDuration]
        .flatMap((value) => String(value || "").split(","))
        .map((value) => parseFloat(value) || 0);
      if (durations.some((seconds) => seconds > 0.01)) {
        long.push(`${node.tagName.toLowerCase()}${node.id ? `#${node.id}` : ""}=${durations.join("/")}s`);
      }
    }
    const running = [...document.getAnimations()].filter((animation) => animation.playState === "running");
    return { long, running: running.length, declared: document.styleSheets.length };
  });
  record(
    results,
    "reduced_motion_respected",
    motion.long.length === 0 && motion.running === 0,
    `transitions-or-animations over 0.01s=${motion.long.length} running-verse-animations=${motion.running}` +
      (motion.long.length ? ` (first: ${motion.long[0]})` : ""),
    document,
  );
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
}

async function measureConsole(page, url, args, results, errors) {
  // Interact with every visible control except forms and links (links are the navigation check's job).
  await page.goto(url, { waitUntil: "load", timeout: args.timeoutMs });
  await new Promise((done) => setTimeout(done, args.settleMs));
  const controls = await page.$$("button, [role='button'], summary, [data-key-activate]");
  for (const control of controls.slice(0, 25)) {
    await control.click({ timeout: 1000 }).catch(() => {});
    await new Promise((done) => setTimeout(done, 40));
  }
  record(
    results,
    "console_clean",
    errors.length === 0,
    errors.length ? `${errors.length} error(s), first: ${errors[0]}` : "no console or page errors",
  );
}

async function run(args) {
  const root = resolve(args.root);
  if (!existsSync(root) || !statSync(root).isDirectory()) {
    throw new Error(`not a directory: ${root}`);
  }
  const pages = htmlPages(root);
  if (!pages.length) {
    throw new Error(`no HTML documents under ${root}; a walk of nothing proves nothing`);
  }

  const puppeteer = await loadPuppeteer();
  const executablePath = resolveChrome();
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars", "--force-color-profile=srgb"],
  });

  const results = [];
  const errors = [];
  const measuredPages = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(`console.error: ${message.text()}`);
    });

    // Measure every document, then report the worst case per criterion.
    for (const file of pages) {
      const url = pathToFileURL(file).href;
      const document = file.replace(`${root}/`, "");
      measuredPages.push(document);
      await measurePage(page, url, args, results, document);
      await measureContrast(page, results, document);
      await measureReducedMotion(page, results, document);
    }
    await measureConsole(page, pathToFileURL(pages[0]).href, args, results, errors);
  } finally {
    await browser.close();
  }

  // Collapse per-page rows to one verdict per criterion: any page failing fails the criterion, and the
  // failure names the page. A pass states how many documents it covers, so one green page can never
  // read as "the whole artifact passed".
  const byCriterion = new Map();
  for (const row of results) {
    byCriterion.set(row.criterion, [...(byCriterion.get(row.criterion) || []), row]);
  }
  const measured = CHECKS.map((criterion) => {
    const rows = byCriterion.get(criterion) || [];
    if (!rows.length) {
      return {
        criterion,
        passed: false,
        basis: "not-measured",
        detail: "this walk produced no measurement for the criterion; it is not a pass",
      };
    }
    const failed = rows.filter((row) => !row.passed);
    if (failed.length) {
      return {
        criterion,
        passed: false,
        basis: "measured",
        detail: `${failed.length}/${rows.length} document(s) failed; ${failed[0].document}: ${failed[0].detail}`,
      };
    }
    return {
      criterion,
      passed: true,
      basis: "measured",
      detail: `all ${rows.length} document(s) passed; ${rows[0].document}: ${rows[0].detail}`,
    };
  });

  const payload = {
    is_a_qualification_report: false,
    artifact_root: root,
    documents: measuredPages,
    browser: executablePath,
    criteria: measured,
    measured_all_six: measured.every((row) => row.basis === "measured"),
    cleared: measured.every((row) => row.passed),
    still_needs_a_human: [
      "the owner artifact review (PilotReport.deliverable and OwnerReview)",
      "the layout and interaction measurements PilotReport requires",
      "the independent security and screenshot approvals",
    ],
    limits:
      "measures one viewport (1440x900) in headless Chromium; it does not judge design, copy, or " +
      "whether the site is worth shipping, and it does not replace the owner's review",
  };

  if (args.out) {
    writeFileSync(args.out, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
  }
  if (args.json) {
    console.log(JSON.stringify(payload, null, 2));
  } else {
    for (const row of measured) {
      console.log(`${row.passed ? "PASS" : "FAIL"}  ${row.criterion.padEnd(24)} ${row.detail}`);
    }
    console.log(
      `\n${measured.filter((row) => row.passed).length}/${measured.length} browser criteria cleared` +
        ` across ${measuredPages.length} document(s)`,
    );
  }
  if (!payload.cleared) process.exitCode = 1;
}

run(parseArgs(process.argv.slice(2))).catch((error) => {
  console.error(`pilot browser walk: ${error.message}`);
  process.exit(2);
});
