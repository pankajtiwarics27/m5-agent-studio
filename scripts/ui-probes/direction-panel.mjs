// DOM-level probe for the AP-018 direction panel.
//
// Why this exists: the direction panel draws a sample id, a question and a rationale that all
// originate outside the pane, and the Chromium walks that produce source-bound UIV2 captures
// cannot run in every environment. This probe extracts the real functions from
// static/index.html and runs them against hostile input in jsdom, so escaping is checked
// without claiming a rendered screenshot. It is not evidence of layout, focus, theming or
// zoom -- only that data becomes text and never markup.
//
// It also pins the two sentences the panel exists to say: approval is not acceptance, and it
// grants no permission to publish. If a later edit drops them, this fails.
//
//   node scripts/ui-probes/direction-panel.mjs
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
// harnessEvidenceFactListHtml lives outside the AP-018 block and is used by the panel.
const factsStart = html.indexOf('  function harnessEvidenceFactListHtml(');
const factsSrc = html.slice(factsStart, html.indexOf('  function harnessEvidenceCoverageHtml('));
const directionStart = html.indexOf('  const HARNESS_DIRECTION_TRIGGERS');
const directionEnd = html.indexOf('  function harnessRecoveryFactsHtml(');
if (directionStart < 0 || directionEnd < 0 || directionStart > directionEnd) {
  console.error('FAIL: the direction panel block could not be located in static/index.html');
  process.exit(1);
}
const directionSrc = html.slice(directionStart, directionEnd);

const build = new window.Function(
  `${escapeSrc}\n${factsSrc}\n${directionSrc}\nreturn { harnessDirectionHtml, harnessDirectionModel };`,
);
const { harnessDirectionHtml, harnessDirectionModel } = build();

const DIGEST = 'a'.repeat(64);

function record(overrides = {}) {
  return {
    schema_version: 1,
    task_id: 'hns-1',
    task_kind: 'website',
    trigger: 'no_benchmark',
    rationale: 'no owner-approved reference covers website',
    sample: { artifact_id: 'samples/hero.html', content_sha256: DIGEST, technical_checks: { renders: true } },
    feedback_prompt: 'is this the right direction for website?',
    feedback_points: [],
    benchmark_reference_id: '',
    benchmark_reference_sha256: '',
    escalated_uncertainties: [],
    pauses_production: true,
    direction_approval_is_final_acceptance: false,
    direction_approval_grants_publish_permission: false,
    ...overrides,
  };
}

function task(direction, state = 'waiting_approval', taken = 1) {
  return { task: { state }, direction_checkpoint: direction, direction_checkpoints_taken: taken };
}

const hostile = '<script>globalThis.pwned=1</script><img src=x onerror="globalThis.pwned=1">';

const cases = [
  {
    name: 'a normal check-in names the sample, its version and the limit',
    task: task(record()),
    want: ['samples/hero.html', DIGEST, 'not accepting this artifact', 'does not publish, deploy or spend'],
  },
  {
    name: 'hostile text in every string field stays text',
    task: task(
      record({
        task_kind: hostile,
        rationale: hostile,
        feedback_prompt: hostile,
        feedback_points: [hostile],
        sample: { artifact_id: hostile, content_sha256: DIGEST, technical_checks: { [hostile]: true } },
        escalated_uncertainties: [hostile],
        benchmark_reference_id: hostile,
      }),
    ),
    want: ['&lt;script&gt;'],
    forbid: ['<script>', '<img'],
    pwned: true,
  },
  {
    name: 'an escalation says what the reference did not measure',
    task: task(record({ trigger: 'uncertainty_exceeds_coverage', escalated_uncertainties: ['new_format'] })),
    want: ['outside what it measured', 'new_format'],
  },
  {
    name: 'a sample with no usable digest is drawn as unidentifiable, not guessed',
    task: task(record({ sample: { artifact_id: 'samples/x.html', content_sha256: 'nope', technical_checks: {} } })),
    want: ['no digest recorded', 'cannot be identified'],
    forbid: ['sha256 nope'],
  },
  {
    name: 'no checkpoint record means no panel, rather than an empty question',
    task: task(null),
    want: [],
    empty: true,
  },
  {
    name: 'the model exposes the version the answer must be bound to',
    task: task(record()),
    model: m => m.digest === DIGEST && m.waiting === true,
  },
];

let failures = 0;
function check(ok, label, detail) {
  if (ok) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}${detail ? `: ${detail}` : ''}`);
  }
}

for (const testCase of cases) {
  console.log(testCase.name);
  const markup = harnessDirectionHtml(testCase.task);
  if (testCase.empty) {
    check(markup === '', 'nothing is drawn');
  } else {
    check(markup.length > 0, 'a panel is drawn');
  }
  for (const want of testCase.want || []) {
    check(markup.includes(want), `contains ${JSON.stringify(want)}`);
  }
  for (const forbidden of testCase.forbid || []) {
    check(!markup.includes(forbidden), `does not contain ${JSON.stringify(forbidden)}`);
  }
  if (testCase.pwned) {
    const host = window.document.createElement('div');
    host.innerHTML = markup;
    window.document.body.appendChild(host);
    check(window.pwned === undefined, 'no hostile handler executed');
    check(host.querySelector('script') === null && host.querySelector('img') === null, 'no element was injected from data');
    host.remove();
  }
  if (testCase.model) {
    const model = harnessDirectionModel(testCase.task);
    check(!!model && testCase.model(model), 'the model carries the bindable facts');
  }
}

// The two sentences are the point of the story, so they are asserted directly.
const prose = harnessDirectionHtml(task(record()));
for (const sentence of ['not accepting this artifact', 'does not publish, deploy or spend', 'separate decision']) {
  check(prose.includes(sentence), `states: ${sentence}`);
}

console.log(failures === 0 ? 'ALL PROBE CHECKS PASSED' : `${failures} PROBE CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
