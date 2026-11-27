// Drive the actual reference handlers with reordered read and write completions.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('  let harnessReferencePayload = null;');
const end = html.indexOf('  // ================= UNIVERSAL CLOUD', start);
const invStart = html.indexOf('  function invalidateHarnessEvidence() {');
const invEnd = html.indexOf('  function harnessLatestVerdict()', invStart);
assert.ok(start >= 0 && end > start && invStart >= 0 && invEnd > invStart);
const scenario = `
const a = {sessionId:'A', artifactId:'hero', digest:'a'};
const a2 = {sessionId:'A', artifactId:'hero', digest:'b'};
const b = {sessionId:'B', artifactId:'hero', digest:'c'};
let activeHarnessId = 'A', harnessFeedbackPayload = a;
let harnessEvidenceReadSerial = 0, harnessEvidencePayload = null;
function harnessFeedbackKey(t) {return t && t.sessionId && t.artifactId && t.digest ? JSON.stringify([t.sessionId,t.artifactId,t.digest]) : '';}
function invalidateHarnessFeedback() { harnessFeedbackPayload = null; }
const nodes = {harnessReferenceApprover:{value:''}, harnessReferenceStatement:{value:''}, harnessReferencePanel:{innerHTML:''}, harnessReferenceNote:{textContent:'',dataset:{}}};
const document = {getElementById(id) {return nodes[id] || null;}};
function escapeHtml(s) {return String(s);}
const calls = [];
function fetch(url, opts) {return new Promise((resolve,reject) => calls.push({url,opts,resolve,reject}));}
function ok(scope) {return {ok:true,json:async()=>({project_scope_id:scope,items:[],cannot_settle:[]})};}
${html.slice(start, end)}
${html.slice(invStart, invEnd)}
const watchdog = setTimeout(() => {console.error('reference probe did not complete'); process.exitCode=1;}, 3000);
(async () => {
 const old = loadHarnessReferences('A',a);
 assert.match(nodes.harnessReferencePanel.innerHTML,/Reading references/);
 nodes.harnessReferenceApprover.value='Owner A'; nodes.harnessReferenceStatement.value='statement A';
 const next = loadHarnessReferences('A',a2); harnessFeedbackPayload = a2;
 assert.equal(nodes.harnessReferenceApprover.value,''); assert.equal(nodes.harnessReferenceStatement.value,'');
 calls[1].resolve(ok('scope-two')); await next;
 calls[0].resolve(ok('scope-old')); await old;
 assert.equal(harnessReferencePayload.scope,'scope-two');
 assert.equal(harnessReferenceIsCurrent(harnessReferenceGeneration,'A',a),false);
 nodes.harnessReferenceApprover.value='Owner two'; nodes.harnessReferenceStatement.value='statement two';
 activeHarnessId='B'; invalidateHarnessEvidence(); harnessFeedbackPayload=b;
 assert.equal(harnessReferencePayload,null);
 assert.equal(nodes.harnessReferenceStatement.value,'');
 const oldB=loadHarnessReferences('B',b);
 activeHarnessId='A'; invalidateHarnessEvidence(); harnessFeedbackPayload=a;
 const back=loadHarnessReferences('A',a);
 assert.equal(nodes.harnessReferenceApprover.value,'Owner A'); assert.equal(nodes.harnessReferenceStatement.value,'statement A');
 calls[2].reject(new Error('stale failure')); await oldB;
 assert.equal(harnessReferencePayload,null); assert.match(nodes.harnessReferencePanel.innerHTML,/Reading references/);
 calls[3].resolve(ok('scope-A')); await back;
 assert.equal(harnessReferencePayload.scope,'scope-A');
 // Cannot combine a project scope from A with another artifact target.
 harnessFeedbackPayload=a2; const n=calls.length; await submitHarnessReference(null); assert.equal(calls.length,n);
 harnessFeedbackPayload=a;
 const savedKey = harnessReferencePayload.targetKey;
 harnessReferencePayload.targetKey = harnessFeedbackKey(a2);
 await submitHarnessReference(null); assert.equal(calls.length,n);
 harnessReferencePayload.targetKey = savedKey;
 const approval=submitHarnessReference(null); const post=calls[calls.length-1];
 assert.equal(JSON.parse(post.opts.body).project_scope_id,'scope-A');
 invalidateHarnessEvidence(); harnessFeedbackPayload=a2;
 const newer=loadHarnessReferences('A',a2); calls[calls.length-1].resolve(ok('scope-two')); await newer;
 post.resolve(ok('saved')); await approval;
 assert.equal(harnessReferencePayload.scope,'scope-two');
 assert.equal(nodes.harnessReferenceStatement.value,'statement two');
 const returnA=loadHarnessReferences('A',a); harnessFeedbackPayload=a;
 calls[calls.length-1].resolve(ok('scope-A')); await returnA;
 assert.match(nodes.harnessReferencePanel.innerHTML,/may have been recorded/);
 assert.equal(nodes.harnessReferenceStatement.value,'statement A');
 const failed=submitHarnessReference(null); const failing=calls[calls.length-1];
 invalidateHarnessEvidence(); harnessFeedbackPayload=a2;
 const after=loadHarnessReferences('A',a2); calls[calls.length-1].resolve(ok('scope-two')); await after;
 failing.reject(new Error('stale write failure')); await failed;
 assert.doesNotMatch(nodes.harnessReferencePanel.innerHTML,/stale write failure/);
 assert.equal(harnessReferencePayload.scope,'scope-two');
 // A successful current write must not erase text edited while waiting.
 harnessFeedbackPayload=a2;
 const current=submitHarnessReference(null); const c=calls[calls.length-1];
 nodes.harnessReferenceStatement.value='new edit'; c.resolve(ok('saved'));
 await new Promise(resolve=>setImmediate(resolve));
 calls[calls.length-1].resolve(ok('scope-two')); await current;
 assert.equal(nodes.harnessReferenceStatement.value,'new edit');
 assert.match(nodes.harnessReferencePanel.innerHTML,/Recorded as a reference/);
 const closing=loadHarnessReferences('A',a2); invalidateHarnessEvidence(); harnessFeedbackPayload=a2;
 const reopened=loadHarnessReferences('A',a2);
 calls[calls.length-1].resolve(ok('reopened')); await reopened;
 calls[calls.length-2].resolve(ok('closed')); await closing;
 assert.equal(harnessReferencePayload.scope,'reopened');
 clearTimeout(watchdog); console.log('reference fencing probe passed');
})().catch(e=>{clearTimeout(watchdog);console.error(e);process.exitCode=1});
`;
vm.runInNewContext(scenario, {assert, console, process, setImmediate, setTimeout, clearTimeout}, {filename:'reference-handlers-probe.js'});
