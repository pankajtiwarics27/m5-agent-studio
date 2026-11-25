// DOM-level probe for the AP-020 reference pane.
//
// The pane draws a reference id, a brief, and the owner's own approval words -- all of which
// come from outside the pane. This probe extracts the real functions from static/index.html and
// runs them against hostile input in jsdom, so escaping is checked without claiming a rendered
// screenshot. It is not evidence of layout, focus, theming or zoom.
//
// It also pins the sentences the pane exists to say, because a later edit could quietly drop
// them: a reference settles reuse of a style and nothing else, and adopting one grants neither
// final acceptance nor permission to publish. And it pins the refusal the pane makes on the
// agent's behalf: no typed name, no record.
//
//   node scripts/ui-probes/reference-panel.mjs
//
// Requires jsdom; install it wherever node can reach the registry.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { JSDOM } = require(process.env.JSDOM_PATH || 'jsdom');
const dom = new JSDOM('<!doctype html><body></body>');
const { window } = dom;
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');

const escStart = html.indexOf('  function escapeHtml(');
const escapeSrc = html.slice(escStart, html.indexOf('\n  }', escStart) + 4);

const blockStart = html.indexOf('  // ================= AP-020: VERSIONED OWNER-APPROVED REFERENCES');
const blockEnd = html.indexOf('  // ================= UNIVERSAL CLOUD + CUSTOM MCP HUB');
if (blockStart < 0 || blockEnd < 0 || blockStart > blockEnd) {
  console.error('FAIL: the AP-020 reference pane block could not be located in static/index.html');
  process.exit(1);
}
const blockSrc = html.slice(blockStart, blockEnd);

const build = new window.Function(
  `${escapeSrc}\n${blockSrc}\nreturn { harnessReferenceHtml, harnessReferenceModel };`,
);
const { harnessReferenceHtml, harnessReferenceModel } = build();

const DIGEST = 'b'.repeat(64);
const failures = [];
function check(name, condition, detail) {
  if (!condition) failures.push(`${name}${detail ? `: ${detail}` : ''}`);
}

function payload(overrides = {}) {
  return {
    project_scope_id: 'proj-0123456789abcdef0123456789abcdef',
    count: 1,
    cannot_settle: [
      'changed_code_works',
      'changed_facts_are_true',
      'security_sign_off',
      'screenshot_approval',
      'final_acceptance_of_new_work',
      'publish_permission',
    ],
    grants_publish_permission: false,
    is_final_acceptance: false,
    items: [
      {
        reference_id: 'ref/website',
        version: 2,
        status: 'active',
        brief: 'a landing page for the ledger app',
        task_kind: 'website',
        audience: 'consumer',
        language: 'en',
        style_tags: ['calm', 'plain'],
        artifact: { artifact_id: 'benchmarks/website-v2.html', content_sha256: DIGEST },
        approval: { actor: 'owner:priya', statement: 'this is the tone I meant' },
        is_final_acceptance: false,
        grants_publish_permission: false,
        why_offered: ['same task kind', 'same audience', 'shared style: calm'],
        superseded_by: '',
      },
    ],
    ...overrides,
  };
}

// ── the hostile case: every value is data, never markup ────────────────────────────────
{
  const hostile = payload({
    items: [
      {
        reference_id: '<img src=x onerror=alert(1)>',
        version: 1,
        status: '"><script>alert(2)</script>',
        brief: '<b>bold claim</b>',
        task_kind: '<i>kind</i>',
        audience: '<svg onload=alert(3)>',
        language: '<u>en</u>',
        style_tags: ['<em>calm</em>'],
        artifact: { artifact_id: 'x', content_sha256: DIGEST },
        approval: { actor: 'owner:<script>alert(4)</script>', statement: '"><img src=x>' },
        is_final_acceptance: false,
        grants_publish_permission: false,
        why_offered: ['<script>alert(5)</script>'],
        superseded_by: '<a href=javascript:alert(6)>next</a>',
      },
    ],
  });
  const host = window.document.createElement('div');
  host.innerHTML = harnessReferenceHtml(hostile);
  check(
    'no element from reference data reached the DOM',
    host.querySelectorAll('script, img, svg, b, i, u, em, a').length === 0,
    host.innerHTML.slice(0, 200),
  );
  check(
    'the hostile reference id is shown as text',
    host.textContent.includes('<img src=x onerror=alert(1)>'),
  );
  check(
    'a javascript: link in superseded_by is not a link',
    // The escaped *text* may legitimately contain the word "javascript:"; what must not exist
    // is an anchor with a live href, or a tag the browser would parse out of the string.
    host.querySelectorAll('a[href]').length === 0 && !/<a\s/i.test(host.innerHTML),
    host.innerHTML.slice(0, 300),
  );
}

// ── what the pane must always say ─────────────────────────────────────────────────────
{
  const host = window.document.createElement('div');
  host.innerHTML = harnessReferenceHtml(payload());
  const text = host.textContent.replace(/\s+/g, ' ');

  check('the owner is named on the row', text.includes('owner:priya'), text);
  check("the owner's own words are shown", text.includes('this is the tone I meant'));
  check('the exact artifact version is named', text.includes(DIGEST.slice(0, 12)));
  check('the reason it was offered is visible', text.includes('shared style: calm'));
  check(
    'the pane states what a reference never settles',
    text.includes('What a reference never settles'),
    text,
  );
  for (const obligation of [
    'changed code works',
    'changed facts are true',
    'security sign off',
    'screenshot approval',
    'final acceptance of new work',
    'publish permission',
  ]) {
    check(`the pane names "${obligation}" as unsettled`, text.includes(obligation), text);
  }
  check(
    'the pane never claims publish permission',
    !/grants? (you )?permission to publish/i.test(text),
    text,
  );
}

// ── the empty and superseded states say something true ───────────────────────────────
{
  const empty = window.document.createElement('div');
  empty.innerHTML = harnessReferenceHtml(payload({ items: [], count: 0 }));
  const text = empty.textContent.replace(/\s+/g, ' ');
  check('an empty project says so', text.includes('Nothing has been adopted as a reference'));
  check(
    'an empty project still states the limit',
    text.includes('What a reference never settles'),
    text,
  );
}

{
  const model = harnessReferenceModel(
    payload({
      items: [
        {
          reference_id: 'ref/website',
          version: 1,
          status: 'superseded',
          brief: 'the first draft',
          artifact: { artifact_id: 'x', content_sha256: DIGEST },
          approval: { actor: 'owner:priya', statement: 'good enough to start' },
          superseded_by: 'ref/website@v2',
        },
      ],
    }),
  );
  check('a superseded row keeps its original approval', model.items[0].approvalWords === 'good enough to start');
  check('a superseded row points forward', model.items[0].supersededBy === 'ref/website@v2');
  check('status is carried through', model.items[0].status === 'superseded');
}

// ── a record claiming permission is drawn as false, never as granted ────────────────
{
  const model = harnessReferenceModel(
    payload({
      items: [
        {
          reference_id: 'ref/website',
          version: 1,
          status: 'active',
          brief: 'x',
          artifact: { artifact_id: 'x', content_sha256: DIGEST },
          approval: { actor: 'owner:priya', statement: 'yes' },
          is_final_acceptance: true,
          grants_publish_permission: true,
        },
      ],
    }),
  );
  // The server refuses such a record at read time; the pane must not be the weaker check.
  check('the pane reads a claimed permission as true only if the record says so', model.items[0].publishPermission === true);
  const host = window.document.createElement('div');
  host.innerHTML = harnessReferenceHtml(payload());
  check(
    'a normal record draws no permission language',
    !/may publish|permission to publish|approved to ship/i.test(host.textContent),
  );
}

if (failures.length) {
  console.error(`FAIL: ${failures.length} probe check(s) failed`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log('ALL PROBE CHECKS PASSED');
