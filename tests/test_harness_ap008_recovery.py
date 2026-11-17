"""AP-008: the existing Harness must not hide stale state or overwrite a draft."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "static/index.html"


def test_snapshot_failure_and_recovery_draft_behavior():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  async function harnessResyncFromSnapshot(reason) {')
    end = html.index('  async function closeHarnessEventStream()', start)
    recovery_start = html.index('  async function harnessRecoveryAct(id) {')
    recovery_end = html.index('  function harnessLastPrompt()', recovery_start)
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessId = 'one', harnessLastResyncAt = 0, harnessSnapshot = {old:true}, harnessSnapshotReadSerial = 0;
let harnessLogTelemetry = {resyncs:0}, harnessRecoveryPrepared = {sessionId:'one'};
let harnessRecoveryPayload = {sessionId:'one'}, harnessRecoveryArmedCancel = 0;
let shown = [], calls = 0, input = {value:'draft to keep', focus(){this.focused = true;}};
let document = {getElementById(id){return id === 'harnessPromptInput' ? input : null;}};
let fetch = async () => ({ok:false,status:503});
function presentHarnessSnapshotUnavailable(reason) { shown.push(reason); }
function clearHarnessSnapshotUnavailable() { shown.push('cleared'); }
function updateHarnessHeaderBanner() {} function applyHarnessSnapshotState() {}
async function loadHarnessEvents() { return {gap:false}; }
function renderStoredHarnessEvents() {} function harnessCachedEvents() {return []}
function scheduleHarnessEventStreamRetry() {} async function refreshHarnessRecovery() {}
function showToast() {} function harnessRecoveryStory() {return {actions:[]};}
function harnessRecoverySay(text) {shown.push(text)} function harnessLastPrompt(){return 'earlier prompt'}
function fitHarnessComposer() {} function runHarnessTurn(){calls++;}
''' + html[start:end] + html[recovery_start:recovery_end] + r'''
(async () => {
  await harnessResyncFromSnapshot('test');
  assert.ok(shown.some(x => /503/.test(x)), 'failure must say snapshot is unavailable');
  assert.deepEqual(harnessSnapshot, {old:true});
  fetch = async () => ({ok:true,json:async()=>({session:{id:'one'},task_status:'paused'})});
  await harnessResyncFromSnapshot('test');
  assert.equal(harnessSnapshot.task_status, 'paused');
  assert.ok(shown.includes('cleared'), 'successful resync clears the stale warning');
  fetch = async () => { throw new Error('offline'); };
  await harnessResyncFromSnapshot('offline');
  assert.ok(shown.some(x => /offline/.test(x)));
  let resolveOld;
  fetch = () => new Promise(resolve => { resolveOld = resolve; });
  const oldRead = harnessResyncFromSnapshot('old');
  fetch = async () => ({ok:true,json:async()=>({task_status:'new'})});
  await harnessResyncFromSnapshot('new');
  resolveOld({ok:true,json:async()=>({task_status:'old'})});
  await oldRead;
  assert.equal(harnessSnapshot.task_status, 'new', 'older response cannot replace newer state');
  await harnessRecoveryAct('continue');
  assert.equal(input.value, 'draft to keep');
  assert.equal(calls, 0, 'must not start a run while a different draft exists');
  assert.equal(input.focused, true);
  input.value = '';
  await harnessRecoveryAct('continue');
  assert.equal(input.value, 'earlier prompt');
  assert.equal(calls, 1);
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_failed_session_does_not_mark_verification_done():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  function updateHarnessHeaderBanner(sess) {')
    end = html.index('  async function selectHarnessSession(id)', start)
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessSession = null;
let harnessStageState = {plan:'done', execute:'done', verify:'done'};
let nodes = {};
let document = {getElementById(id) {return nodes[id] ||= {textContent:'', value:''}}};
let unifiedCatalog = [];
function harnessStatusBadge() {return {cls:'warn', label:'failed'}}
function setStatus() {} function updateHarnessCtxBadge() {} function updateHarnessRunControl() {}
function renderHarnessStages() {}
''' + html[start:end] + r'''
updateHarnessHeaderBanner({id:'s', status:'failed'});
assert.notEqual(harnessStageState.verify, 'done');
assert.equal(harnessStageState.execute, 'blocked');
updateHarnessHeaderBanner({id:'s', status:'completed'});
assert.equal(harnessStageState.verify, 'done');
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr
