// DOM-level probe for the fact-row renderer with an optional inert note line.
//
// Why this exists: AP-008 needs one more row in the verification facts list, but the
// Chromium walks that produce the source-bound UIV2 captures cannot run in every
// environment. This probe extracts the real functions from static/index.html and runs
// them against hostile input in jsdom, so the renderer's escaping can be checked
// without claiming a rendered screenshot. It is not evidence of layout, focus,
// theming or zoom -- only that data becomes text and never markup.
//
//   node scripts/ui-probes/ev-facts-note.mjs [--proposed]
//
// Requires jsdom; install it wherever node can reach the registry.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const require = createRequire(import.meta.url);
// Resolve jsdom from wherever it is installed, without vendoring it into the repo.
const { JSDOM } = require(process.env.JSDOM_PATH || 'jsdom');
const dom = new JSDOM('<!doctype html><body></body>');
const { window } = dom;
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');
const escStart = html.indexOf('  function escapeHtml(');
const escapeSrc = html.slice(escStart, html.indexOf('\n  }', escStart) + 4);
const PROPOSED = process.argv.includes('--proposed');
const proposed = PROPOSED
  ? fs.readFileSync(path.join(ROOT, 'scripts', 'ui-probes', 'ev-facts-note.proposed.js'), 'utf8')
  : null;
const source = proposed ?? html.slice(html.indexOf('  function harnessEvidenceFactListHtml('), html.indexOf('  function harnessEvidenceCoverageHtml('));
const fn = new window.Function(escapeSrc + '\n' + source + '\nreturn harnessEvidenceFactListHtml;')();

const cases = [
  [{ label: 'Session artifact versions', value: '2 derived', note: '<script>alert(1)</script>' }],
  [{ label: 'a', value: 'b', note: '<img src=x onerror=globalThis.pwned=1>' }],
  [{ label: 'a', value: 'b', note: 'plain note' }],
  [{ label: 'a', value: 'b' }],
  [{ label: '<svg/onload=alert(1)>', value: '<iframe src=javascript:1>', note: '<style>*{}</style>' }],
];
let failures = 0;
for (const c of cases) {
  const host = window.document.createElement('div');
  host.innerHTML = fn(c);
  for (const bad of ['script', 'img', 'svg', 'iframe', 'style', 'onerror', 'onload']) {
    if (host.querySelector(bad)) { console.log('FAIL: element injected:', bad); failures++; }
  }
  if (host.querySelectorAll('dt').length !== c.length) { console.log('FAIL: row count'); failures++; }
  if (PROPOSED && c[0].note) {
    const note = host.querySelector('p.ev-note');
    if (!note || note.textContent !== c[0].note) { console.log('FAIL: note text not verbatim/escaped'); failures++; }
  }
  if (PROPOSED && !c[0].note && host.querySelector('p.ev-note')) { console.log('FAIL: empty note emitted'); failures++; }
  if (!PROPOSED && host.querySelector('p.ev-note')) { console.log('FAIL: shipped renderer emitted a note element'); failures++; }
  // no handlers survive as attributes
  for (const el of host.querySelectorAll('*')) for (const a of el.attributes) {
    if (/^on/i.test(a.name)) { console.log('FAIL: handler attribute survived:', a.name); failures++; }
  }
  console.log('ok', JSON.stringify(fn(c)).slice(0, 70));
}
console.log(failures === 0 ? 'PROPOSED PATCH: ALL PROBE CHECKS PASSED' : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
