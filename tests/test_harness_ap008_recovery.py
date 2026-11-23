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


def test_workspace_preview_is_inert_truthful_and_session_fenced():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  let harnessWorkspaceReviewSerial = 0;')
    end = html.index('  function applyHarnessSnapshotState(state)', start)
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessId = 'one';
let harnessSnapshot = {session_id:'one', task_status:'paused', milestones:[{state:'completed'},{state:'pending'}], approvals:{pending:[{}]}, budget:{steps_used:3}};
let nodes = Object.fromEntries(['harnessWorkspaceProgress','harnessWorkspaceDiffState','harnessWorkspaceDiffPreview','harnessWorkspaceReview'].map(id => [id,{textContent:'',hidden:false,open:false}]));
let document = {getElementById(id){return nodes[id]}, querySelector(){return null}};
let fetch = async () => ({ok:false,status:403});
''' + html[start:end] + r'''
(async () => {
  await refreshHarnessWorkspaceReview();
  assert.match(nodes.harnessWorkspaceDiffState.textContent, /HTTP 403/);
  assert.equal(nodes.harnessWorkspaceDiffPreview.hidden,true);
  assert.match(nodes.harnessWorkspaceProgress.textContent, /1\/2 complete/);
  assert.match(nodes.harnessWorkspaceProgress.textContent, /Steps used: 3/);
  fetch = async () => { throw new Error('offline'); };
  await refreshHarnessWorkspaceReview();
  assert.match(nodes.harnessWorkspaceDiffState.textContent, /offline/);
  const attack = '<img src=x onerror=alert(1)>\n<script>alert(1)</script>';
  fetch = async () => ({ok:true,json:async()=>({is_git:true,status:' M a',diff:attack})});
  await refreshHarnessWorkspaceReview();
  assert.equal(nodes.harnessWorkspaceDiffPreview.textContent,attack);
  assert.equal(nodes.harnessWorkspaceDiffPreview.innerHTML,undefined);
  assert.equal(nodes.harnessWorkspaceDiffPreview.hidden,false);
  fetch = async () => ({ok:true,json:async()=>({is_git:true,status:'',diff:'Git diff error: failed'})});
  await refreshHarnessWorkspaceReview();
  assert.match(nodes.harnessWorkspaceDiffState.textContent, /unavailable/);
  assert.equal(nodes.harnessWorkspaceDiffPreview.hidden,true);
  fetch = async () => ({ok:true,json:async()=>({is_git:true,status:'',diff:''})});
  await refreshHarnessWorkspaceReview();
  assert.match(nodes.harnessWorkspaceDiffState.textContent, /No workspace changes/);
  let release;
  fetch = () => new Promise(resolve => {release=resolve});
  const old = refreshHarnessWorkspaceReview();
  activeHarnessId = 'two'; resetHarnessWorkspaceReview();
  release({ok:true,json:async()=>({is_git:true,status:'old secret',diff:'old secret'})});
  await old;
  assert.doesNotMatch(nodes.harnessWorkspaceDiffState.textContent, /old secret/);
  assert.equal(nodes.harnessWorkspaceDiffPreview.textContent,'');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_submitted_draft_waits_for_server_acceptance_and_never_overwrites_new_text():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  function harnessClearSubmittedDraft(')
    end = html.index('  function runHarnessTurn()', start)
    runner_end = html.index('  document.getElementById("harnessPromptInput").addEventListener', end)
    runner = html[end:runner_end]
    assert 'const submittedDraft = input.value;' in runner
    assert 'harnessClearSubmittedDraft(input, submittedDraft);' in runner
    assert 'if (activeHarnessId !== submittedSessionId) return;' in runner
    assert runner.index('harnessClearSubmittedDraft(input, submittedDraft);') > runner.index('turn_start(d)')
    assert 'input.value = "";' not in runner[:runner.index('turn_start(d)')]
    assert 'request may have reached the server' in runner
    script = r'''
const assert = require('node:assert/strict'); let fits=0;
function fitHarnessComposer(){fits++;}
''' + html[start:end] + r'''
let input={value:'original'};
harnessClearSubmittedDraft(input,'original'); assert.equal(input.value,'');
input.value='new draft'; harnessClearSubmittedDraft(input,'original'); assert.equal(input.value,'new draft');
assert.equal(fits,1);
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr
def test_outcome_and_approval_reads_reject_superseded_or_foreign_snapshots():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    start = html.index("  async function refreshHarnessSnapshotAfterOutcome(sessionId) {")
    end = html.index("  async function closeHarnessEventStream()", start)
    approval_start = html.index("  async function confirmLiveHarnessApproval(sessionId, approvalId) {")
    approval_end = html.index("  function setHarnessApprovalCardStatus(", approval_start)
    fresh_start = html.index("  function startFreshHarnessDraft() {")
    selection_start = html.index("  async function selectHarnessSession(id) {")
    fresh = html[fresh_start:fresh_start + 400]
    selection = html[selection_start:selection_start + 700]
    assert "harnessSnapshotReadSerial += 1;" in fresh[:400]
    assert "harnessSnapshotReadSerial += 1;" in selection
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessId='one', harnessSnapshotReadSerial=0, harnessSnapshot={session_id:'one',task_status:'initial'};
let notices=[], rendered=[];
function clearHarnessSnapshotUnavailable(){notices.push('cleared');}
function presentHarnessSnapshotUnavailable(message){notices.push(message);}
function updateHarnessHeaderBanner(){}
function applyHarnessSnapshotState(s){rendered.push(s.task_status);}
function harnessCachedEvents(){return [];}
function renderStoredHarnessEvents(){}
let fetch;
''' + html[start:end] + html[approval_start:approval_end] + r'''
(async () => {
  let release;
  fetch=()=>new Promise(resolve=>{release=resolve});
  const old=refreshHarnessSnapshotAfterOutcome('one');
  fetch=async()=>({ok:true,json:async()=>({session_id:'one',task_status:'new'})});
  await refreshHarnessSnapshotAfterOutcome('one');
  release({ok:true,json:async()=>({session_id:'one',task_status:'old'})});
  await old;
  assert.equal(harnessSnapshot.task_status,'new');
  assert.deepEqual(rendered,['new']);
  fetch=async()=>({ok:true,json:async()=>({session_id:'other',task_status:'wrong'})});
  await refreshHarnessSnapshotAfterOutcome('one');
  assert.equal(harnessSnapshot.task_status,'new');
  assert.match(notices.at(-1),/unexpected session state/);
  fetch=async()=>({ok:false,status:503});
  await refreshHarnessSnapshotAfterOutcome('one');
  assert.match(notices.at(-1),/503/);
  fetch=()=>new Promise(resolve=>{release=resolve});
  const pending=confirmLiveHarnessApproval('one','a');
  harnessSnapshotReadSerial++;
  release({ok:true,json:async()=>({session_id:'one',approvals:{pending:[{approval_id:'a'}]}})});
  const result=await pending;
  assert.equal(result.ok,false); assert.equal(result.authoritative,false);
  assert.match(result.message,/no decision was sent/);
  assert.equal(harnessSnapshot.task_status,'new');
  fetch=async()=>({ok:true,json:async()=>({session_id:'one',approvals:{pending:[{approval_id:'a'}]}})});
  assert.equal((await confirmLiveHarnessApproval('one','a')).ok,true);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_workspace_snapshot_summary_is_inert_truthful_and_fenced():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  async function refreshHarnessWorkspaceSnapshots(')
    end = html.index('  async function refreshHarnessWorkspaceReview()', start)
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessId='one', harnessWorkspaceReviewSerial=1;
let node={textContent:''}; let document={getElementById(){return node}};
let fetch;
''' + html[start:end] + r'''
(async () => {
  const run = async data => {fetch=async()=>({ok:true,json:async()=>data}); await refreshHarnessWorkspaceSnapshots('one',1)};
  await run({session_id:'one',snapshots:[],latest_snapshot_id:null});
  assert.match(node.textContent,/No workspace snapshots/);
  const label='<img src=x onerror=alert(1)>';
  await run({session_id:'one',latest_snapshot_id:'s1',snapshots:[{snapshot_id:'s1',label}]});
  assert.match(node.textContent,/Latest: s1/); assert.ok(node.textContent.includes(label));
  assert.equal(node.innerHTML,undefined); assert.match(node.textContent,/not a live workspace version/);
  await run({session_id:'one',latest_snapshot_id:'missing',snapshots:[{snapshot_id:'s1'}]});
  assert.match(node.textContent,/unavailable.*missing/);
  await run({session_id:'two',snapshots:[]}); assert.match(node.textContent,/invalid session/);
  await run({session_id:'one',snapshots:null}); assert.match(node.textContent,/invalid session/);
  fetch=async()=>({ok:false,status:403}); await refreshHarnessWorkspaceSnapshots('one',1);
  assert.match(node.textContent,/HTTP 403/);
  fetch=async()=>{throw new Error('offline')}; await refreshHarnessWorkspaceSnapshots('one',1);
  assert.match(node.textContent,/offline/);
  let release; fetch=()=>new Promise(resolve=>{release=resolve});
  const old=refreshHarnessWorkspaceSnapshots('one',1); harnessWorkspaceReviewSerial=2;
  node.textContent='new session'; release({ok:true,json:async()=>({session_id:'one',latest_snapshot_id:'s1',snapshots:[{snapshot_id:'s1'}]})});
  await old; assert.equal(node.textContent,'new session');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_evidence_diff_never_reports_failure_or_unrendered_changes_as_clean():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  function harnessDiffFiles(')
    end = html.index('  function harnessEvidenceFactListHtml(', start)
    change_start = html.index('  function harnessEvidenceChangeHtml(')
    change_end = html.index('  function harnessEvidenceArtifactsHtml(', change_start)
    script = r'''
const assert = require('node:assert/strict');
function escapeHtml(v) {return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
''' + html[start:end] + html[change_start:change_end] + r'''
const failure = harnessEvidenceChangeHtml({is_git:true,status:'',diff:'Git diff error: <script>alert(1)</script>'});
assert.match(failure,/could not be read/); assert.doesNotMatch(failure,/Status<\/dt><dd class="mono">clean/);
assert.doesNotMatch(failure,/<script>/); assert.match(failure,/&lt;script&gt;/);
for (const data of [{is_git:false,status:'',diff:''}, {is_git:true,status:'',diff:null}, {}]) {
  const output=harnessEvidenceChangeHtml(data); assert.match(output,/unavailable or invalid/); assert.doesNotMatch(output,/reports no workspace changes/);
}
const dirty=harnessEvidenceChangeHtml({is_git:true,status:'?? new.bin',diff:''});
assert.match(dirty,/Git reports workspace changes/); assert.doesNotMatch(dirty,/reports no workspace changes/);
const clean=harnessEvidenceChangeHtml({is_git:true,status:'',diff:''}); assert.match(clean,/Git reports no workspace changes/);
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_real_git_diff_returns_redacted_text_for_tracked_and_untracked(tmp_path):
    import app

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "AP008 test")
    git("config", "user.email", "ap008@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "baseline")
    secret = "sk-ant-" + "A" * 24
    tracked.write_text(f"tracked {secret}\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text(f"new {secret}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"secret {secret}\n", encoding="utf-8")
    diff = app._compute_git_diff(tmp_path)
    assert diff["is_git"] is True
    assert isinstance(diff["diff"], str)
    assert "tracked.txt" in diff["diff"] and "new.txt" in diff["diff"]
    assert secret not in diff["diff"] and diff["diff"].count("[REDACTED]") >= 2
    assert ".env" not in diff["diff"]
    assert len(diff["diff"]) <= 50_000


def test_contract_file_disclosure_is_session_scoped_stale_aware_and_inert():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  function renderHarnessWorkspaceContractFiles() {')
    end = html.index('  function resetHarnessWorkspaceReview()', start)
    script = r'''
const assert = require('node:assert/strict');
let activeHarnessId='one', harnessSnapshot=null, stale=false;
const node={textContent:''};
let document={getElementById(){return node},querySelector(){return stale ? {} : null}};
''' + html[start:end] + r'''
renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/unavailable/);
harnessSnapshot={session_id:'two', verification_spec:{required_files:['other'],required_modified_files:[]}};
renderHarnessWorkspaceContractFiles(); assert.doesNotMatch(node.textContent,/other/);
harnessSnapshot={session_id:'one', verification_spec:null}; renderHarnessWorkspaceContractFiles();
assert.match(node.textContent,/no contract recorded/);
harnessSnapshot.verification_spec={required_files:['<img src=x onerror=alert(1)>'],required_modified_files:['src/a.py']};
renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/<img/); assert.equal(node.innerHTML,undefined);
assert.match(node.textContent,/not proof/); assert.match(node.textContent,/src\/a.py/);
stale=true; renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/may be stale/);
harnessSnapshot.verification_spec={required_files:[],required_modified_files:[]};
renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/none declared/);
harnessSnapshot.verification_spec={required_files:['ok'],required_modified_files:[42]};
renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/invalid contract/);
activeHarnessId=null; renderHarnessWorkspaceContractFiles(); assert.match(node.textContent,/Select a session/);
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr
