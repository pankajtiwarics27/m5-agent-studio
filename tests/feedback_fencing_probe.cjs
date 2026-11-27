// Run the shipped feedback handlers with controllable delayed network responses.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('  let harnessFeedbackPayload = null;');
const end = html.indexOf('  function renderHarnessEvidence(payload)', start);
assert.ok(start >= 0 && end > start, 'feedback handlers must be present');
const code = html.slice(start, end);
const invalidationStart = html.indexOf('  function invalidateHarnessEvidence() {');
const invalidationEnd = html.indexOf('  function harnessLatestVerdict()', invalidationStart);
assert.ok(invalidationStart >= 0 && invalidationEnd > invalidationStart);
const invalidation = html.slice(invalidationStart, invalidationEnd);
const scenario = `
const a = {sessionId:'A', artifactId:'hero', digest:'a'};
const a2 = {sessionId:'A', artifactId:'hero', digest:'b'};
const b = {sessionId:'B', artifactId:'hero', digest:'c'};
const deferred = [];
function fetch(url, opts) { return new Promise((resolve, reject) => deferred.push({url, opts, resolve, reject})); }
function ok(data) { return {ok:true, json:async () => data}; }
function harnessFeedbackHtml(data) { return 'data:' + data.name; }
function escapeHtml(value) { return String(value); }
const field = {value:''}, host = {innerHTML:''};
const document = {getElementById(id) {return id === 'harnessFeedbackText' ? field : id === 'harnessFeedbackPanel' ? host : null}, querySelectorAll() {return []}};
let activeHarnessId = 'A', harnessEvidenceReadSerial = 0, harnessEvidencePayload = null;
function invalidateHarnessReference() {}
${code}
${invalidation}
const watchdog = setTimeout(() => {console.error('feedback probe did not complete'); process.exitCode=1;}, 3000);
(async () => {
  const first = loadHarnessFeedback(a);
  assert.match(host.innerHTML, /Reading feedback/);
  field.value = 'draft A';
  const second = loadHarnessFeedback(a2);
  assert.equal(field.value, '');
  deferred[1].resolve(ok({name:'new'})); await second;
  deferred[0].resolve(ok({name:'old'})); await first;
  assert.equal(harnessFeedbackPayload.digest, 'b');
  assert.equal(harnessFeedbackIsCurrent(harnessFeedbackGeneration, a), false);
  assert.match(host.innerHTML, /data:new/);
  field.value = 'draft A2';
  activeHarnessId = 'B'; invalidateHarnessFeedback();
  const third = loadHarnessFeedback(b);
  const fourthTarget = b;
  activeHarnessId = 'A'; invalidateHarnessFeedback();
  const fourth = loadHarnessFeedback(a);
  assert.equal(field.value, 'draft A');
  deferred[2].reject(new Error('old network error')); await third;
  assert.equal(harnessFeedbackPayload, null);
  assert.match(host.innerHTML, /Reading feedback/);
  deferred[3].resolve(ok({name:'A again'})); await fourth;
  assert.equal(harnessFeedbackPayload.digest, 'a');
  assert.equal(field.value, 'draft A');
  // Close/reopen of the same target must fence the old read too.
  const oldRead = loadHarnessFeedback(a); invalidateHarnessFeedback();
  const newRead = loadHarnessFeedback(a);
  deferred[5].resolve(ok({name:'reopened'})); await newRead;
  deferred[4].resolve(ok({name:'closed'})); await oldRead;
  assert.match(host.innerHTML, /data:reopened/);
  // Every mutation can finish after a version switch. It must not redraw or clear the new draft.
  for (const action of ['submit','confirm','withdraw']) {
    field.value = 'words for A';
    const promise = action === 'submit' ? submitHarnessFeedback(null) : action === 'confirm' ? confirmHarnessFeedback('f') : withdrawHarnessFeedback('f');
    const call = deferred[deferred.length - 1];
    invalidateHarnessFeedback();
    const load = loadHarnessFeedback(a2);
    deferred[deferred.length - 1].resolve(ok({name:'version two'})); await load;
    assert.equal(field.value, 'draft A2');
    call.resolve(ok({name:'stale write'})); await promise;
    assert.equal(harnessFeedbackPayload.digest, 'b');
    assert.equal(field.value, 'draft A2');
    assert.match(host.innerHTML, /data:version two/);
    const back = loadHarnessFeedback(a);
    deferred[deferred.length - 1].resolve(ok({name:'A'})); await back;
    if (action === 'submit') assert.match(host.innerHTML, /may have been recorded/);
  }
  for (const action of ['submit','confirm','withdraw']) {
    field.value = 'words for A';
    const promise = action === 'submit' ? submitHarnessFeedback(null) : action === 'confirm' ? confirmHarnessFeedback('f') : withdrawHarnessFeedback('f');
    const call = deferred[deferred.length - 1];
    invalidateHarnessFeedback();
    const load = loadHarnessFeedback(a2);
    deferred[deferred.length - 1].resolve(ok({name:'version two'})); await load;
    call.reject(new Error('stale write error')); await promise;
    assert.equal(harnessFeedbackPayload.digest, 'b');
    assert.doesNotMatch(host.innerHTML, /stale write error/);
    const back = loadHarnessFeedback(a);
    deferred[deferred.length - 1].resolve(ok({name:'A'})); await back;
  }
  field.value = 'old text';
  const send = submitHarnessFeedback(null); const call = deferred[deferred.length - 1];
  field.value = 'edited while sending'; call.resolve(ok({name:'sent'})); await send;
  assert.equal(field.value, 'edited while sending');
  const failure = loadHarnessFeedback(a);
  deferred[deferred.length - 1].reject(new Error('offline')); await failure;
  assert.match(host.innerHTML, /offline/);
  assert.equal(harnessFeedbackPayload.digest, 'a');
  assert.equal(field.value, 'edited while sending');
  invalidateHarnessEvidence();
  assert.equal(field.value, '');
  assert.equal(harnessFeedbackPayload, null);
  clearTimeout(watchdog); console.log('feedback fencing probe passed');
})().catch(error => { clearTimeout(watchdog); console.error(error); process.exitCode = 1; });
`;
vm.runInNewContext(scenario, {assert, console, process, setTimeout, clearTimeout}, {filename:'feedback-handlers-probe.js'});
