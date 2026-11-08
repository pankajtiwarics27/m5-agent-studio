/** Exercise the task-first layout against an isolated, synthetic demo only. */
import assert from 'node:assert/strict';
import { waitForHarnessSidebarLayout } from './sidebar_layout_ready.mjs';
import { checkSidebarLayoutReadiness } from './sidebar_layout_ready_checks.mjs';
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
  await checkSidebarLayoutReadiness(browser, check);
  const page = await browser.newPage();
  const errors = [], external = [], mcpWrites = [], posts = [];
  const mcpFixture = { alias: 'ui-proof-server', name: 'Synthetic UI proof', category: 'local',
    transport: 'stdio', command: 'synthetic-not-executed', args: [], enabled: true,
    active_tool_count: 7, tools: [], approval_policy: 'always_ask' };
  let fixtureMcpActive = false, refuseMcp = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.setRequestInterception(true);
  page.on('request', request => {
    if (new URL(request.url()).origin !== base.origin) { external.push(request.url()); return request.abort(); }
    const path = new URL(request.url()).pathname;
    if (request.method()==='POST') posts.push(path);
    if (fixtureMcpActive && path === '/api/mcp/servers/ui-proof-server' && request.method() === 'PATCH') {
      const payload = JSON.parse(request.postData()); mcpWrites.push(payload);
      if (refuseMcp) return request.respond({status: 403, contentType: 'application/json', body: JSON.stringify({detail: 'Synthetic permission refusal: server state kept'})});
      mcpFixture.enabled = payload.enabled;
      return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify({servers: [mcpFixture]})});
    }
    if (fixtureMcpActive && path === '/api/mcp/servers') return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify({servers: [mcpFixture], presets: {}})});
    if (path === '/api/harness/run') return; // Hold before the server; Abort is exercised without a model call.
    return request.continue();
  });
  await page.evaluateOnNewDocument(token => sessionStorage.setItem('m5_api_token', token), token);
  await page.setViewport({ width: 1568, height: 900 });
  await page.goto(base.href, { waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelector('#harnessLeadModel').options.length > 0);
  const savedWorkspace = await page.$eval('#harnessWorkspaceInput', n=>n.value.trim());
  const caption = () => page.$eval('.workspace-caption', n=>({text:n.textContent,title:n.title,children:n.children.length}));
  check('header hydrates from workspace', (await caption()).text===`Workspace: ${savedWorkspace}`,await caption());
  const beforePosts = posts.length;
  const longWorkspace = '/host/'+'nested-'.repeat(60)+'<img src=x onerror=alert(1)>';
  for (const path of ['/host/alpha/Developer','/host/beta/Developer','/',longWorkspace]) {
    await page.evaluate(path=>syncProjectDirInputs(path),path);
    const label = await caption();
    check(`header follows draft ${path.slice(0,30)}`,label.text===`Workspace: ${path}` && label.title===path && label.children===0,label);
  }
  await page.$eval('#harnessWorkspaceInput',n=>{n.value='';n.dispatchEvent(new Event('change'));});
  check('empty header does not retain a stale path',(await caption()).text==='Workspace: not selected',await caption());
  check('header rendering makes no write/inference request',posts.length===beforePosts,posts);
  // Draft display is not a save: reload must hydrate the actual saved default again.
  await page.reload({waitUntil:'networkidle0'});
  await page.waitForFunction(()=>document.querySelector('#harnessLeadModel').options.length>0);
  await page.waitForFunction(()=>document.getElementById('harnessWorkspaceInput').value.trim().length>0);
  check('header rehydrates after reload', (await caption()).text===`Workspace: ${savedWorkspace}` && await page.$eval('#harnessWorkspaceInput',n=>n.value.trim())===savedWorkspace, await caption());
  // Shared navigation must not jump when switching into/out of Harness.
  const navigationPosts = posts.length;
  for (const theme of ['light', 'dark']) for (const compact of [false, true]) {
    for (const width of [390, 799, 800, 820, 1279, 1280, 1568]) {
      await page.setViewport({width, height: 900});
      await page.evaluate(({theme, compact}) => { applyTheme(theme); applyDensity(compact); }, {theme, compact});
      const navigation = async tab => {
        await page.evaluate(tab => switchTab(tab), tab);
        // Keep navigation snapshots frame-aligned, then require the real measured
        // result for the desktop Harness session seam as well.
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await waitForHarnessSidebarLayout(page);
        return page.evaluate(() => {
          const rail = document.querySelector('.mode-tabs');
          const rect = n => { const b = n.getBoundingClientRect(); return [b.x,b.y,b.width,b.height].map(v=>Math.round(v*100)/100); };
          const css = getComputedStyle(rail);
          return {width: rect(rail)[2], padding: css.padding, gap: css.gap,
            mainLeft: rect(document.getElementById('mainLayout'))[0] + parseFloat(getComputedStyle(document.getElementById('mainLayout')).paddingLeft),
            buttons: [...rail.querySelectorAll('.mode-tab')].map(n=>({box:rect(n), justify:getComputedStyle(n).justifyContent, label:getComputedStyle(n.querySelector('span')).display}))};
        });
      };
      const reference = await navigation('start');
      for (const tab of ['harness', 'hall', 'council', 'debate', 'chat']) {
        const actual = await navigation(tab);
        check(`shared navigation ${tab}/${theme}/${compact}/${width}`,JSON.stringify(actual)===JSON.stringify(reference),{reference, actual});
        if (tab==='harness' && width>=1280) {
          const seam = await page.evaluate(() => {
            const nav=document.querySelector('.mode-tabs').getBoundingClientRect();
            const session=document.getElementById('harnessSessionsPanel').getBoundingClientRect();
            const last=document.querySelector('.mode-tab:last-child').getBoundingClientRect();
            return {navBottom:nav.bottom,sessionTop:session.top,lastBottom:last.bottom};
          });
          check(`sessions below full navigation ${theme}/${compact}/${width}`,Math.abs(seam.navBottom-seam.sessionTop)<1 && seam.lastBottom<=seam.navBottom,seam);
        }
      }
    }
  }
  check('tab consistency makes no write/inference request',posts.length===navigationPosts,posts);
  await page.evaluate(() => switchTab('harness'));
  await page.evaluate(path=>syncProjectDirInputs(path),longWorkspace);
  await page.evaluate(() => startFreshHarnessDraft());
  for (const theme of ['light', 'dark']) {
    for (const compact of [false, true]) {
      for (const width of [390, 820, 1280, 1568]) {
        const height = width === 390 ? 844 : width === 1280 ? 720 : 900;
        await page.setViewport({ width, height });
        await page.evaluate(({ theme, compact }) => { applyTheme(theme); applyDensity(compact); }, { theme, compact });
        // ResizeObserver delivers asynchronously, before paint but not necessarily
        // before this next protocol evaluation after a breakpoint/density change.
        await waitForHarnessSidebarLayout(page);
        const layout = await page.evaluate(() => {
          const rect = el => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height }; };
          const ids = ['harnessPromptInput', 'harnessRunBtn', 'harnessLeadModel', 'harnessPolicy', 'harnessWorkflowMode', 'harnessProjectSummary'];
          return { boxes: ids.map(id => ({ id, ...rect(document.getElementById(id)) })),
            pageOverflow: document.documentElement.scrollWidth - innerWidth,
            inspector: getComputedStyle(document.getElementById('monitorRail')).display,
            tools: document.getElementById('studioTools').open,
            settings: document.getElementById('harnessTaskSettings').open,
            disabled: document.getElementById('harnessRunBtn').disabled,
            controls: ['harnessLeadModel', 'harnessPolicy', 'harnessAttachBtn', 'harnessWorkflowMode', 'harnessRunBtn'].map(id => ({id, ...rect(document.getElementById(id))})),
            actionRow: rect(document.querySelector('.task-composer-actions')),
            settingsControl: rect(document.querySelector('#harnessTaskSettings > summary')),
            side: rect(document.getElementById('harnessSessionsPanel')),
            nav: rect(document.querySelector('.mode-tabs')), caption: rect(document.querySelector('.workspace-caption')),
            captionDisplay:getComputedStyle(document.querySelector('.workspace-caption')).display, toolsBox:rect(document.getElementById('studioTools')) };
        });
        check(`composer ${theme}/${compact}/${width}`, layout.boxes.every(b => b.width > 0 && b.left >= -1 && b.right <= width + 1 && b.top >= 0 && b.bottom <= height), layout);
        check(`header fits ${theme}/${compact}/${width}`, width<800 ? layout.captionDisplay==='none' : layout.caption.width>0 && layout.caption.left>=0 && layout.caption.right<=layout.toolsBox.left && layout.pageOverflow<=1, layout);
        check(`secondary chrome ${theme}/${compact}/${width}`, layout.inspector === 'none' && !layout.tools && !layout.settings && layout.disabled && layout.pageOverflow <= 1, layout);
        check(`uniform controls ${theme}/${compact}/${width}`, [...layout.controls, layout.settingsControl].every(b => Math.abs(b.height - (compact ? 32 : 44)) < 1), layout);
        check(`readable workflow ${theme}/${compact}/${width}`, layout.controls.find(b => b.id === 'harnessWorkflowMode').width >= 80, layout);
        check(`single action row ${theme}/${compact}/${width}`, layout.controls.filter(b => ['harnessAttachBtn','harnessWorkflowMode','harnessRunBtn'].includes(b.id)).every(b => Math.abs(b.top - layout.actionRow.top) < 1) && Math.abs(layout.settingsControl.top - layout.actionRow.top) < 1, layout);
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
  await page.click('#integrationSkillsTab');
  check('integrations are reachable', await page.$eval('#activeSkillsPills', el => el.getBoundingClientRect().width > 0));
  // Exercise the actual guarded PATCH handler; intercept it before any server write.
  fixtureMcpActive = true;
  await page.evaluate(server => { mcpServers = [server]; setIntegrationKind('mcp'); renderMcpUI(); }, mcpFixture);
  await page.focus('.integration-row'); await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.integration-row').getAttribute('aria-pressed') === 'false');
  check('real MCP toggle sends only enabled state', mcpWrites.length === 1 && Object.keys(mcpWrites[0]).join() === 'enabled' && mcpWrites[0].enabled === false, mcpWrites);
  check('disabled MCP does not contribute tools', await page.$eval('#mcpToolsCountBadge', el => el.textContent === '0'));
  refuseMcp = true;
  await page.focus('.integration-row'); await page.keyboard.press('Space');
  await page.waitForFunction(() => document.getElementById('toastStack').textContent.includes('Synthetic permission refusal'));
  await page.waitForFunction(() => document.querySelector('.integration-row').getAttribute('aria-pressed') === 'false');
  check('refused toggle retains state and policy', await page.evaluate(() => !mcpServers[0].enabled && mcpServers[0].approval_policy === 'always_ask'));
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
    await page.$eval('#toastStack', el => el.replaceChildren());
    mkdirSync(process.env.STUDIO_TASK_SCREENSHOT_DIR, { recursive: true });
    for (const [name, width] of [['desktop', 1568], ['mobile', 390]]) {
      await page.setViewport({ width, height: 900 });
      await page.screenshot({ path: resolve(process.env.STUDIO_TASK_SCREENSHOT_DIR, `${name}.png`) });
    }
    await page.evaluate(() => {
      mcpServers = Array.from({length: 130}, (_, i) => ({alias: `configured-server-${i}`, name: `Synthetic server ${i}`, category: 'local', transport: 'stdio', args: [], tools: [], enabled: i < 3, active_tool_count: i === 0 ? 45 : 5, approval_policy: 'always_ask'}));
      installedSkills = Array.from({length: 130}, (_, i) => ({id: `skill-${i}`, name: `Synthetic skill ${i}`, description: 'Synthetic preview'}));
      selectedSkills = new Set(['skill-0', 'skill-1']); renderSkillsUI(); renderMcpUI();
      document.getElementById('studioTools').open = true;
    });
    for (const [name, width, theme] of [['integrations-light',1568,'light'], ['integrations-dark',1568,'dark'], ['integrations-mobile',390,'light']]) {
      await page.setViewport({width, height: 900});
      await page.evaluate(async theme => { applyTheme(theme); await new Promise(resolve => setTimeout(resolve, 250)); }, theme);
      await page.screenshot({path: resolve(process.env.STUDIO_TASK_SCREENSHOT_DIR, `${name}.png`)});
    }
  }
  check('no browser boot failures', errors.length === 0, errors);
  check('no external requests', external.length === 0, external);
  console.log(JSON.stringify({ passed: checks.length, checks }));
} finally { await browser.close(); }
