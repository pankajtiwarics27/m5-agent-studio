/** Offline layout regression: real stylesheet, markup and pill renderers, synthetic state.
 * No Studio store, credentials, startup script or network requests are used.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const module = await import(pathToFileURL(resolve(process.env.STUDIO_PUPPETEER_CORE)).href);
const puppeteer = module.default || module;
const source = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
const html = source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
const skills = source.slice(source.indexOf('  function renderSkillsUI()'), source.indexOf('  function openSkillsModal()'));
const mcp = source.slice(source.indexOf('  function renderMcpUI()'), source.indexOf('  function populateMcpPlayTools()'));
const escapeHtml = source.match(/  function escapeHtml\(s\) [^\n]+/)[0];
const browser = await puppeteer.launch({
  executablePath: process.env.STUDIO_SCREENSHOT_CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
});
let checks = 0;
try {
  const page = await browser.newPage();
  await page.setRequestInterception(true);
  page.on('request', request => request.abort());
  await page.setContent(html, { waitUntil: 'domcontentloaded' });
  // Only dependencies outside the band are stubbed; both band renderers run unchanged.
  await page.addScriptTag({ content: `
    let installedSkills = [], mcpServers = [], selectedSkills = new Set();
    const UI_ICONS = { check: '✓' };
    const mcpPolicyPromise = () => 'Synthetic local fixture';
    const renderMcpHub = () => {}, populateMcpPlayTools = () => {};
    ${escapeHtml}\n${skills}\n${mcp}
    window.fixture = (count) => {
      installedSkills = Array.from({length: count}, (_, i) => ({
        id: 'skill-' + i, name: i === 0 ? 'apple-design' : i === 1 ? 'find-skills' : 'ui-ux-' + 'longname'.repeat(i === 2 ? 12 : 1) + i,
        description: 'Synthetic skill', has_design_engine: i === 0
      }));
      selectedSkills = new Set(['skill-0']);
      mcpServers = Array.from({length: count}, (_, i) => ({
        alias: 'synthetic-mcp-' + 'longname'.repeat(i === 1 ? 12 : 1) + i,
        name: 'Synthetic MCP', category: 'local', transport: 'stdio', command: 'synthetic', args: [],
        enabled: i === 0, active_tool_count: 3, tools: [], approval_policy: 'always_ask'
      }));
      renderSkillsUI(); renderMcpUI();
    };
  ` });
  for (const width of [390, 600, 820, 1024, 1280, 1440, 1920]) {
    await page.setViewport({ width, height: 900 });
    for (const count of [0, 3, 30]) {
      await page.evaluate(count => window.fixture(count), count);
      const result = await page.evaluate(() => {
        const strip = document.querySelector('.skills-strip');
        const groups = ['activeSkillsPills', 'activeMcpPills'].map(id => document.getElementById(id));
        const horizontal = [strip, ...groups].map(el => ({ id: el.id || 'strip',
          overflow: el.scrollWidth - el.clientWidth, wrap: getComputedStyle(el).flexWrap }));
        const band = strip.getBoundingClientRect();
        const pills = groups.flatMap(group => [...group.children]);
        const controls = [document.getElementById('integrationsQuickMenu'), document.getElementById('monitorToggleBtn'),
          strip.querySelector('.btn-danger')];
        return {
          horizontal,
          bounded: [...pills, ...controls].every(el => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.left >= band.left - 1 && box.right <= band.right + 1;
          }),
          capped: band.height <= innerHeight * 0.3 + 2,
          empty: groups.map(el => el.textContent),
        };
      });
      assert(result.horizontal.every(el => el.overflow <= 1 && el.wrap === 'wrap'), JSON.stringify({ width, count, result }));
      assert(result.bounded && result.capped, JSON.stringify({ width, count, result }));
      if (!count) assert(result.empty.every(text => text.includes('Tools & Integrations')));
      // Even with a capped long list, keyboard focus can reveal the final action.
      await page.focus('.skills-strip');
      await page.focus('.skills-strip .btn-danger');
      const visible = await page.$eval('.skills-strip .btn-danger', el => {
        const box = el.getBoundingClientRect(), band = el.closest('.skills-strip').getBoundingClientRect();
        return box.top >= band.top - 1 && box.bottom <= band.bottom + 1;
      });
      assert(visible, `unreachable action at ${width}px with ${count} integrations`);
      checks++;
    }
  }
  console.log(JSON.stringify({ passed: checks, widths: 7, states: ['empty', 'three', 'thirty'], network: 'blocked' }));
} finally {
  await browser.close();
}
