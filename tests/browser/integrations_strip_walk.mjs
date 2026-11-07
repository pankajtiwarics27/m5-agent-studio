/** Offline regression: real CSS/picker/skill renderer, synthetic large collections.
 * No Studio store, secrets, live MCP endpoints or outbound requests are used.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const module = await import(pathToFileURL(resolve(process.env.STUDIO_PUPPETEER_CORE)).href);
const source = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
const html = source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
const skills = source.slice(source.indexOf('  function toggleSkill(id)'), source.indexOf('  function openSkillsModal()'));
const mcp = source.slice(source.indexOf('  function renderMcpUI()'), source.indexOf('  function populateMcpPlayTools()'));
const escapeHtml = source.match(/  function escapeHtml\(s\) [^\n]+/)[0];
const browser = await (module.default || module).launch({ executablePath: process.env.STUDIO_SCREENSHOT_CHROME,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
let checks = 0;
try {
  const page = await browser.newPage();
  const errors = [], network = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setRequestInterception(true);
  page.on('request', request => { network.push(request.url()); request.abort(); });
  await page.setContent(html, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => { document.getElementById('studioTools').open = true; });
  await page.addScriptTag({ content: `
    let installedSkills = [], mcpServers = [], selectedSkills = new Set();
    const UI_ICONS = { check: '✓' };
    const mcpPolicyPromise = () => 'Synthetic local fixture';
    const renderMcpHub = () => {}, populateMcpPlayTools = () => {};
    window.storageWrites = []; window.serverWrites = [];
    Object.defineProperty(window, 'localStorage', {value: {setItem: (key, value) => storageWrites.push({key, value})}});
    ${escapeHtml}\n${skills}\n${mcp}
    async function toggleMcpServerEnabled(alias, enabled) {
      serverWrites.push({alias, enabled});
      if (!window.refuseMcp) mcpServers.find(s => s.alias === alias).enabled = enabled;
      renderMcpUI();
    }
    window.fixture = (count) => {
      installedSkills = Array.from({length: count}, (_, i) => ({
        id: 'skill-' + i, name: i === 1 ? '<img src=x onerror=alert(1)>' : 'Synthetic skill ' + i + (i === 2 ? 'longname'.repeat(20) : ''),
        description: 'Synthetic skill', has_design_engine: i === 0
      }));
      selectedSkills = new Set(['skill-0', 'uninstalled-stale-id']);
      mcpServers = Array.from({length: count}, (_, i) => ({
        alias: 'mcp-' + i, name: 'Synthetic MCP', category: 'local', transport: 'stdio', command: 'synthetic', args: [],
        enabled: i === 0, active_tool_count: i === 0 ? 55 : 3, tools: [], approval_policy: 'always_ask'
      }));
      integrationPage = 0; document.getElementById('integrationSearch').value = '';
      renderSkillsUI(); renderMcpUI();
    };
  ` });
  for (const theme of ['light', 'dark']) for (const compact of [false, true]) {
    for (const width of [390, 600, 820, 1024, 1280, 1440, 1920]) {
      await page.setViewport({ width, height: 900 });
      for (const count of [0, 3, 130]) {
        await page.evaluate(({theme, compact, count}) => {
          document.body.dataset.theme = theme; document.body.dataset.density = compact ? 'compact' : 'comfortable';
          window.fixture(count); setIntegrationKind('mcp');
        }, {theme, compact, count});
        const result = await page.evaluate(() => {
          const panel = document.querySelector('.skills-strip'), list = document.querySelector('.integration-results');
          const b = panel.getBoundingClientRect();
          const row = panel.querySelector('.integration-row.active');
          const luminance = text => {
            const c = text.match(/[\d.]+/g).slice(0,3).map(n => { const v = Number(n)/255; return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4; });
            return c[0]*.2126+c[1]*.7152+c[2]*.0722;
          };
          const contrast = row ? (() => { const s = getComputedStyle(row), a = luminance(s.color), b = luminance(s.backgroundColor); return (Math.max(a,b)+.05)/(Math.min(a,b)+.05); })() : 21;
          return { overflow: panel.scrollWidth-panel.clientWidth, listOverflow: list.scrollWidth-list.clientWidth,
            inViewport: b.left >= -1 && b.right <= innerWidth+1 && b.top >= 0 && b.bottom <= innerHeight,
            height: b.height, contrast, rows: panel.querySelectorAll('.integration-row').length,
            servers: document.getElementById('mcpServersCountBadge').textContent,
            enabled: document.getElementById('mcpEnabledCountBadge').textContent,
            tools: document.getElementById('mcpToolsCountBadge').textContent,
            skills: document.getElementById('skillsCountBadge').textContent };
        });
        assert(result.overflow <= 1 && result.listOverflow <= 1 && result.inViewport && result.height <= 542,
          JSON.stringify({theme, compact, width, count, result}));
        assert(result.contrast >= 4.5, JSON.stringify({theme, result}));
        assert.equal(result.rows, Math.min(count, 6));
        assert.equal(result.servers, String(count));
        assert.equal(result.enabled, count ? '1' : '0');
        assert.equal(result.tools, count ? '55' : '0');
        assert.equal(result.skills, `${count ? 1 : 0}/${count}`);
        await page.click('#integrationSkillsTab');
        assert.equal(await page.$$eval('#activeSkillsPills .integration-row', els => els.length), Math.min(count, 6));
        assert.equal(await page.$$eval('#activeMcpPills .integration-row', els => els.length), 0);
        assert.equal(await page.$$eval('#activeSkillsPills img', els => els.length), 0);
        // Keyboard can still reveal the final management action in a short viewport.
        await page.focus('.skills-strip .btn-danger');
        const visible = await page.$eval('.skills-strip .btn-danger', el => {
          const b = el.getBoundingClientRect(), panel = el.closest('.skills-strip').getBoundingClientRect();
          return b.top >= panel.top - 1 && b.bottom <= panel.bottom + 1;
        });
        assert(visible, `unreachable management at ${width}px`);
        checks++;
      }
    }
  }
  // Page through every item, including the final partial page, without duplicating/missing rows.
  for (const [kind, prefix] of [['mcp', 'mcp-'], ['skills', 'skill-']]) {
    await page.evaluate(kind => setIntegrationKind(kind), kind);
    const ids = new Set();
    for (;;) {
      for (const id of await page.$$eval('.integration-row', els => els.map(el => el.dataset.integrationId))) ids.add(id);
      if (await page.$eval('#integrationNext', el => el.disabled)) break;
      await page.click('#integrationNext');
    }
    assert.equal(ids.size, 130);
    assert(ids.has(prefix+'129'));
    assert(await page.$eval('#integrationPageStatus', el => el.textContent.startsWith('127–130')));
    assert(await page.$eval('#integrationPageStatus', el => el === document.activeElement));
  }
  // A data refresh that shrinks a last page clamps back to a real, reachable page.
  await page.evaluate(() => { installedSkills = installedSkills.slice(0, 1); renderSkillsUI(); });
  assert(await page.$eval('#integrationPageStatus', el => el.textContent.startsWith('1–1 of 1')));
  await page.evaluate(() => fixture(130));
  // Filtering is local and preserves selections; toggles delegate to the existing handlers.
  await page.evaluate(() => setIntegrationKind('mcp'));
  await page.type('#integrationSearch', 'mcp-129');
  assert.equal(await page.$$eval('.integration-row', els => els.length), 1);
  assert(await page.$eval('#integrationPageStatus', el => el.textContent.startsWith('1–1')));
  assert.equal(await page.evaluate(() => serverWrites.length), 0);
  await page.focus('.integration-row'); await page.keyboard.press('Space');
  await page.waitForFunction(() => document.querySelector('.integration-row').getAttribute('aria-pressed') === 'true');
  assert.deepEqual(await page.evaluate(() => serverWrites), [{alias: 'mcp-129', enabled: true}]);
  assert(await page.$eval('.integration-row', el => el === document.activeElement));
  await page.evaluate(() => { window.refuseMcp = true; });
  await page.keyboard.press('Enter');
  assert.equal(await page.$eval('.integration-row', el => el.getAttribute('aria-pressed')), 'true');
  assert(await page.evaluate(() => mcpServers.every(s => s.approval_policy === 'always_ask')));
  await page.click('#integrationSkillsTab');
  await page.$eval('#integrationSearch', el => { el.value = 'skill-129'; el.dispatchEvent(new Event('input')); });
  await page.focus('.integration-row'); await page.keyboard.press('Enter');
  assert(await page.evaluate(() => selectedSkills.has('skill-129')));
  assert(await page.$eval('.integration-row', el => el === document.activeElement));
  assert(await page.evaluate(() => storageWrites.some(w => w.key === 'm5_active_skills' && JSON.parse(w.value).includes('skill-129'))));
  // Quoted/untrusted identifiers are data attributes, not JavaScript interpolation.
  await page.evaluate(() => {
    installedSkills = [{id: "quoted'skill", name: '<svg onload=alert(1)>', description: ''}];
    document.getElementById('integrationSearch').value = ''; renderSkillsUI();
  });
  await page.click('.integration-row');
  assert(await page.evaluate(() => selectedSkills.has("quoted'skill")));
  assert.equal(await page.$$eval('.integration-row svg', els => els.length), 0);
  await page.type('#integrationSearch', 'not-found');
  assert.equal(await page.$$eval('.integration-row', els => els.length), 0);
  assert(await page.$eval('#activeSkillsPills', el => el.textContent.includes('No matching')));
  assert(await page.$eval('#integrationPrev', el => el.disabled));
  assert(await page.$eval('#integrationNext', el => el.disabled));
  await page.keyboard.press('Escape');
  assert(await page.$eval('#studioTools', el => el.open));
  assert.equal(await page.$eval('#integrationSearch', el => el.value), '');
  await page.keyboard.press('Escape');
  assert(await page.$eval('#studioTools', el => !el.open && el.querySelector('summary') === document.activeElement));
  assert.deepEqual(errors, []); assert.deepEqual(network, []);
  console.log(JSON.stringify({passed: checks, widths: 7, themes: 2, densities: 2,
    states: ['empty', 'three', '130'], interactions: 'search, all pages, keyboard toggles, refusal, persistence, focus, escape, escaping', network: 'blocked'}));
} finally { await browser.close(); }
