/** Exercise the task-first layout against an isolated, synthetic demo only. */
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const args = process.argv.slice(2);
const base = new URL(args[args.indexOf('--base-url') + 1]);
const token = args[args.indexOf('--token') + 1];
assert(['127.0.0.1', 'localhost'].includes(base.hostname) && base.protocol === 'http:');
assert(token && token.length >= 32);
const module = await import(pathToFileURL(resolve(process.env.STUDIO_PUPPETEER_CORE)).href);
const browser = await (module.default || module).launch({ executablePath: process.env.STUDIO_SCREENSHOT_CHROME,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
const checks = [];
function check(name, pass, detail) { assert(pass, `${name}: ${JSON.stringify(detail)}`); checks.push(name); }
try {
  const page = await browser.newPage();
  const errors = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setRequestInterception(true);
  page.on('request', request => {
    if (new URL(request.url()).origin !== base.origin) { external.push(request.url()); return request.abort(); }
    if (new URL(request.url()).pathname === '/api/harness/run') return; // Hold before the server; Abort is exercised without a model call.
    return request.continue();
  });
  await page.evaluateOnNewDocument(token => sessionStorage.setItem('m5_api_token', token), token);
  await page.setViewport({ width: 1568, height: 900 });
  await page.goto(base.href, { waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelector('#harnessLeadModel').options.length > 0);
  await page.evaluate(() => startFreshHarnessDraft());
  for (const theme of ['light', 'dark']) {
    for (const compact of [false, true]) {
      for (const width of [390, 820, 1280, 1568]) {
        const height = width === 390 ? 844 : width === 1280 ? 720 : 900;
        await page.setViewport({ width, height });
        await page.evaluate(({ theme, compact }) => { applyTheme(theme); applyDensity(compact); }, { theme, compact });
        const layout = await page.evaluate(() => {
          const rect = el => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height }; };
          const ids = ['harnessPromptInput', 'harnessRunBtn', 'harnessLeadModel', 'harnessPolicy', 'harnessWorkflowMode', 'harnessProjectSummary'];
          return { boxes: ids.map(id => ({ id, ...rect(document.getElementById(id)) })),
            pageOverflow: document.documentElement.scrollWidth - innerWidth,
            inspector: getComputedStyle(document.getElementById('monitorRail')).display,
            tools: document.getElementById('studioTools').open,
            settings: document.getElementById('harnessTaskSettings').open,
            disabled: document.getElementById('harnessRunBtn').disabled,
            side: rect(document.getElementById('harnessSessionsPanel')),
            nav: rect(document.querySelector('.mode-tabs')) };
        });
        check(`composer ${theme}/${compact}/${width}`, layout.boxes.every(b => b.width > 0 && b.left >= -1 && b.right <= width + 1 && b.top >= 0 && b.bottom <= height), layout);
        check(`secondary chrome ${theme}/${compact}/${width}`, layout.inspector === 'none' && !layout.tools && !layout.settings && layout.disabled && layout.pageOverflow <= 1, layout);
        if (width >= 1280) check(`one left surface ${width}`, Math.abs(layout.side.left - layout.nav.left) < 1 && Math.abs(layout.side.width - layout.nav.width) < 1 && layout.side.top >= layout.nav.bottom - 1, layout);
      }
    }
  }
  // Real handlers and live controls: no run request or provider call is needed.
  await page.setViewport({ width: 1280, height: 800 });
  await page.evaluate(() => { applyTheme('light'); applyDensity(false); });
  await page.click('#harnessProjectSummary');
  check('project opens settings', await page.$eval('#harnessTaskSettings', el => el.open));
  const settings = await page.$eval('.harness-settings-body', el => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom, right: b.right }; });
  check('settings fit viewport', settings.top >= 0 && settings.bottom <= 800 && settings.right <= 1280, settings);
  await page.select('#harnessCriticModel', await page.$eval('#harnessLeadModel', el => el.value));
  await page.$eval('#harnessTaskSettings', el => { el.open = false; });
  await page.type('#harnessPromptInput', 'Explain the local project without executing commands.');
  await page.select('#harnessPolicy', 'plan');
  check('policy and plan stay in sync', await page.$eval('[data-plan="policy"]', el => el.textContent.includes('Read-Only')));
  await page.click('.harness-run-review > summary');
  check('complete run decisions retained', await page.$$eval('#harnessRunPlanBody .harness-plan-row', els => els.length === 6));
  await page.$eval('.harness-run-review', el => { el.open = false; });
  await page.click('#monitorToggleBtn');
  check('inspector opens explicitly', await page.$eval('#monitorToggleBtn', el => el.getAttribute('aria-expanded') === 'true'));
  await page.click('[aria-label="Close inspector"]');
  check('inspector close restores toggle state', await page.$eval('#monitorToggleBtn', el => el.getAttribute('aria-expanded') === 'false'));
  await page.click('#studioTools > summary');
  check('integrations are reachable', await page.$eval('#activeSkillsPills', el => el.getBoundingClientRect().width > 0));
  await page.click('#studioTools > summary');
  await page.setViewport({ width: 390, height: 844 });
  await page.click('#harnessSessionsToggle');
  check('mobile sessions reachable', await page.$eval('#harnessSessionFilter', el => el === document.activeElement));
  await page.keyboard.press('Escape');
  check('mobile sessions escape and return focus', await page.$eval('#harnessSessionsToggle', el => el === document.activeElement && el.getAttribute('aria-expanded') === 'false'));
  // A live task must still have its transcript, progress, permission and abort surfaces.
  await page.evaluate(() => {
    document.getElementById('harnessStream').innerHTML = '<div class="h-turn-user">Synthetic in-progress task</div>'.repeat(80);
  });
  await page.click('#harnessRunBtn');
  await page.waitForFunction(() => getComputedStyle(document.getElementById('harnessAbortBtn')).display !== 'none');
  await page.$eval('#harnessStream', el => { el.innerHTML = '<div class="h-turn-user">Synthetic streamed response</div>'.repeat(80); });
  check('active task keeps abort', await page.$eval('#harnessAbortBtn', el => getComputedStyle(el).display !== 'none'));
  check('abort stays in view with a long transcript', await page.$eval('#harnessAbortBtn', el => { const b = el.getBoundingClientRect(); return b.top >= 0 && b.bottom <= innerHeight; }));
  check('active task keeps stages', await page.$eval('#harnessStages', el => getComputedStyle(el).display !== 'none'));
  check('active task retains permissions', await page.$eval('#harnessPermissionBadge', el => !!el));
  await page.click('#harnessAbortBtn');
  await page.waitForFunction(() => getComputedStyle(document.getElementById('harnessAbortBtn')).display === 'none');
  await page.evaluate(() => startFreshHarnessDraft());
  if (process.env.STUDIO_TASK_SCREENSHOT_DIR) {
    await page.select('#harnessPolicy', 'standard');
    mkdirSync(process.env.STUDIO_TASK_SCREENSHOT_DIR, { recursive: true });
    for (const [name, width] of [['desktop', 1568], ['mobile', 390]]) {
      await page.setViewport({ width, height: 900 });
      await page.screenshot({ path: resolve(process.env.STUDIO_TASK_SCREENSHOT_DIR, `${name}.png`) });
    }
  }
  check('no browser boot failures', errors.length === 0, errors);
  check('no external requests', external.length === 0, external);
  console.log(JSON.stringify({ passed: checks.length, checks }));
} finally { await browser.close(); }
