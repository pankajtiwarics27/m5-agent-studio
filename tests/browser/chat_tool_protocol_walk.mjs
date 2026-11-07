/** Real Chat controls/context helpers, isolated from startup and all network. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const module = await import(pathToFileURL(resolve(process.env.STUDIO_PUPPETEER_CORE)).href);
const source = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
const html = source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
const extract = name => {
  const start = source.indexOf(`  function ${name}(`);
  assert(start >= 0, `missing helper ${name}`);
  return source.slice(start, source.indexOf('\n  function ', start + 1));
};
const browser = await (module.default || module).launch({
  executablePath: process.env.STUDIO_SCREENSHOT_CHROME, headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
});
let passed = 0;
try {
  const page = await browser.newPage();
  const network = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setRequestInterception(true);
  page.on('request', request => { network.push(request.url()); request.abort(); });
  await page.setContent(html, { waitUntil: 'domcontentloaded' });
  await page.addScriptTag({ content: `
    const selectedSkills = new Set();
    ${['chatToolsOn', 'chatPersonaLabel', 'chatContextSnapshot', 'chatContextLine'].map(extract).join('\n')}
    window.capture = () => chatContextSnapshot('fixture-model', 2, '/fixture workspace');
    window.contextLine = value => chatContextLine(value);
  ` });
  await page.evaluate(() => {
    document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-chat'));
    document.getElementById('chatModel').innerHTML = '<option>Fixture model</option>';
    document.getElementById('chatPersona').innerHTML = '<option>Fixture persona</option>';
    document.getElementById('chatPersonaDescription').textContent = 'A concise technical assistant with clear explanations.';
  });
  for (const theme of ['light', 'dark']) for (const density of ['comfortable', 'compact']) for (const width of [390, 900, 1280, 1440]) {
    await page.setViewport({ width, height: 900 });
    await page.evaluate(({theme, density}) => { document.body.dataset.theme = theme; document.body.dataset.density = density; }, {theme, density});
    const layout = await page.evaluate(() => {
      const selector = document.getElementById('chatToolProtocol');
      const rect = selector.getBoundingClientRect();
      const pane = document.getElementById('pane-chat');
      return { width: rect.width, height: rect.height, left: rect.left, right: rect.right,
        paneLeft: pane.getBoundingClientRect().left, paneRight: pane.getBoundingClientRect().right,
        headerHeight: document.querySelector(".chat-header-bar").getBoundingClientRect().height,
        labels: selector.labels.length, hidden: getComputedStyle(selector).visibility === 'hidden' };
    });
    assert(layout.width >= 96 && layout.height >= 30 && !layout.hidden, JSON.stringify({ width, layout }));
    assert(layout.left >= layout.paneLeft - 1 && layout.right <= layout.paneRight + 1, JSON.stringify({ width, layout }));
    assert.equal(layout.labels, 1);
    if (width === 390) assert(layout.headerHeight <= 320, JSON.stringify(layout));
    passed++;
  }
  await page.select('#chatToolProtocol', 'native');
  await page.focus('#chatToolProtocol');
  await page.keyboard.press('ArrowDown');
  const captured = await page.evaluate(() => window.capture());
  assert.equal(captured.tool_protocol, 'tagged');
  assert.equal(captured.tools, true);
  passed++;
  await page.select('#chatToolProtocol', 'native');
  assert((await page.evaluate(value => window.contextLine(value), captured)).includes('tool protocol tagged'));
  passed++;
  await page.click('#chatToolsEnabled');
  assert.equal((await page.evaluate(() => window.capture())).tools, false);
  passed++;
  assert.equal(network.length, 0, `unexpected network: ${network}`);
  assert.deepEqual(errors, []);
  passed++;
  console.log(JSON.stringify({ passed, network: network.length, errors }));
} finally {
  await browser.close();
}
