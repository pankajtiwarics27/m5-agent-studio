"""AP-008: the existing Harness must not hide stale state or overwrite a draft."""
from pathlib import Path
import subprocess

import workflow_checkpoints


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


def test_evidence_modal_clears_and_fences_overlapping_session_reads():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  let harnessEvidencePayload = null;')
    end = html.index('  function harnessEvidenceMetaText(', start)
    script = r'''
const assert=require('node:assert/strict');
let activeHarnessId='one', nodes={}, rendered=[];
let document={getElementById(id){return nodes[id] ||= {textContent:'old secret'}}};
function harnessCachedEvents(){return [];} function harnessVerdictTone(){return '';}
function renderHarnessEvidence(value){rendered.push(value);} function openOverlay(){} function closeOverlay(){invalidateHarnessEvidence();}
function showToast(){} let fetch;
''' + html[start:end] + r'''
(async()=>{
  let releaseOld;
  fetch=url=>String(url).endsWith('/task') ? new Promise(resolve=>{releaseOld=resolve}) : Promise.resolve({ok:true,json:async()=>({status:'old'})});
  const old=refreshHarnessEvidence();
  assert.equal(nodes.harnessEvidenceChange.textContent,''); assert.match(nodes.harnessEvidenceMeta.textContent,/reading/);
  fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/task')?{task:'new'}:{diff:'new'}});
  await refreshHarnessEvidence();
  releaseOld({ok:true,json:async()=>({task:'old'})}); await old;
  assert.equal(rendered.length,1); assert.equal(rendered[0].task.task,'new');
  let releaseA;
  fetch=()=>new Promise(resolve=>{releaseA=resolve});
  const a=refreshHarnessEvidence(); activeHarnessId='two'; invalidateHarnessEvidence(); activeHarnessId='one'; invalidateHarnessEvidence();
  releaseA({ok:true,json:async()=>({task:'stale'})}); await a;
  assert.equal(rendered.length,1); assert.equal(nodes.harnessEvidenceChange.textContent,'');
  fetch=()=>new Promise(resolve=>{releaseA=resolve});
  const closing=refreshHarnessEvidence(); closeHarnessEvidenceModal();
  releaseA({ok:true,json:async()=>({task:'closed'})}); await closing;
  assert.equal(rendered.length,1); assert.equal(nodes.harnessEvidenceMeta.textContent,'');
  fetch=async()=>({ok:false,status:403}); await refreshHarnessEvidence();
  assert.equal(rendered.length,2); assert.equal(rendered[1].task,null); assert.equal(rendered[1].diff,null);
  assert.ok(rendered[1].errors.every(x=>x.includes('403')));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_checkpoint_evidence_references_are_session_scoped_inert_and_bounded():
    html = HTML.read_text(encoding="utf-8")
    start = html.index('  function harnessCheckpointReferencesHtml(')
    end = html.index('  function harnessEvidenceArtifactsHtml(', start)
    script = r'''
const assert = require('node:assert/strict');
function escapeHtml(s){return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
''' + html[start:end] + r'''
const ref={kind:'artifact',ref_id:'<img src=x onerror=evil()>',sha256:'abcdef',summary:'<script>evil()</script>'};
const task={latest_checkpoint:{task_id:'one',evidence_refs:[ref]}};
const html=harnessCheckpointReferencesHtml(task,'one');
assert.match(html,/&lt;img/); assert.ok(!html.includes('<img')); assert.ok(!html.includes('<script>'));
assert.match(html,/recorded sha256/); assert.match(html,/not artifact versions/); assert.match(html,/not been rechecked/);
assert.match(harnessCheckpointReferencesHtml(task,'two'),/identity could not be confirmed/);
assert.ok(!harnessCheckpointReferencesHtml(task,'two').includes('abcdef'));
assert.match(harnessCheckpointReferencesHtml({...task,checkpoint_integrity_error:'corrupt'},'one'),/integrity check/);
assert.match(harnessCheckpointReferencesHtml({latest_checkpoint:null},'one'),/No latest checkpoint/);
assert.match(harnessCheckpointReferencesHtml({latest_checkpoint:{task_id:'one',evidence_refs:[]}},'one'),/no evidence references/);
assert.match(harnessCheckpointReferencesHtml({latest_checkpoint:{task_id:'one',evidence_refs:[{kind:'x'}]}},'one'),/malformed/);
assert.match(harnessCheckpointReferencesHtml({latest_checkpoint:{task_id:'one',evidence_refs:Array(25).fill(ref)}},'one'),/Showing 20 of 25/);
assert.ok(!harnessCheckpointReferencesHtml({latest_checkpoint:{task_id:'one',evidence_refs:[{...ref,ref_id:'x'.repeat(1000)}]}},'one').includes('x'.repeat(257)));
'''
    result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, cwd=ROOT, check=False)
    assert result.returncode == 0, result.stderr


def test_session_artifact_versions_are_derived_from_the_checkpoint_not_global_lineage():
    import app as studio

    refs = [
        workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/a.py", sha256="a" * 64, summary="12B"),
        workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="docs/b.md", sha256="b" * 64),
        workflow_checkpoints.EvidenceRef(kind="action", ref_id="act-1", sha256="c" * 64),
        workflow_checkpoints.EvidenceRef(kind="git_diff", ref_id="HEAD", sha256="d" * 64),
        workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="undigested"),
        workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="malformed", sha256="not-a-digest"),
    ]
    versions = studio._session_artifact_versions(refs, session_id="sess-1")

    # Only artifact references carrying a real digest are versionable.
    assert [v["artifact_id"] for v in versions] == ["docs/b.md", "src/a.py"]
    for version in versions:
        assert version["session_id"] == "sess-1"
        assert len(version["identity_sha256"]) == 64
        # The checkpoint records a digest and nothing else about provenance. The
        # record must say so rather than borrowing confidence from axes it lacks.
        assert version["identity_basis"] == "content_only"
        assert version["identity_axes_recorded"] == ["content_sha256"]
        assert "producer" in version["identity_axes_absent"]
        assert "inputs" in version["identity_axes_absent"]
        # The honest fields: a recorded pointer is not a re-read file, and this is
        # not the process-wide lineage or a workspace snapshot.
        assert version["rechecked_against_current_content"] is False
        assert version["is_global_lineage"] is False
        assert version["is_workspace_snapshot"] is False


def test_session_artifact_versions_are_session_scoped_and_deterministic():
    import app as studio

    ref = workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/a.py", sha256="a" * 64)
    first = studio._session_artifact_versions([ref], session_id="sess-1")
    second = studio._session_artifact_versions([ref], session_id="sess-1")
    other = studio._session_artifact_versions([ref], session_id="sess-2")

    assert first == second
    # The identity is scoped to recorded content only, so the same bytes in two
    # sessions carry the same identity. The session is reported alongside it rather
    # than folded in, because a session was never an input to the artifact.
    assert first[0]["identity_sha256"] == other[0]["identity_sha256"]
    assert first[0]["session_id"] != other[0]["session_id"]
    assert first[0]["is_global_lineage"] is False
    assert first[0]["is_workspace_snapshot"] is False


def test_identity_is_not_inflated_with_unrecorded_provenance():
    import app as studio

    ref = workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/a.py", sha256="a" * 64)
    version = studio._session_artifact_versions([ref], session_id="sess-1")[0]

    # If the reference path had been passed as a producer, the same content from a
    # different path would look like a different version. It is the same content.
    other_path = workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/copy-of-a.py", sha256="a" * 64)
    other = studio._session_artifact_versions([other_path], session_id="sess-1")[0]
    assert version["identity_sha256"] == other["identity_sha256"]
    assert version["artifact_id"] != other["artifact_id"]


def test_identity_ignores_the_reference_path_without_hiding_it():
    import app as studio

    # The same content recorded under two names is one version, two references.
    a = workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="a.py", sha256="e" * 64)
    b = workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="b.py", sha256="e" * 64)
    versions = studio._session_artifact_versions([b, a], session_id="s")
    assert [v["artifact_id"] for v in versions] == ["a.py", "b.py"]
    assert versions[0]["identity_sha256"] == versions[1]["identity_sha256"]


def test_session_with_no_versionable_references_yields_nothing_not_a_guess():
    import app as studio

    refs = [workflow_checkpoints.EvidenceRef(kind="action", ref_id="act-1", sha256="c" * 64)]
    assert studio._session_artifact_versions(refs, session_id="sess-1") == []
    assert studio._session_artifact_versions([], session_id="sess-1") == []


AUTH_TOKEN = "a" * 40


def _harness_client(tmp_path, monkeypatch):
    import app as studio
    from fastapi.testclient import TestClient

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store)
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "ap008.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    studio.init_db()
    studio._reset_process_supervisor(None)
    return studio, TestClient(
        studio.app,
        base_url="http://127.0.0.1:8787",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )


def _seed_checkpointed_task(studio, evidence_refs):
    session = studio.create_harness_session_db(
        title="AP-008 artifact versions",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        use_worktree=False,
        policy="standard",
        lead_model="",
        critic_model="",
    )
    sid = session["id"]
    sm = studio.get_task_state_machine()
    sm.ensure_root_task(
        sid,
        session_id=sid,
        title="AP-008 artifact versions",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        policy="standard",
    )
    sm.checkpoints.save_checkpoint(
        sid,
        step=1,
        payload={"note": "artifact written"},
        evidence_refs=evidence_refs,
        require_active_epoch=False,
    )
    return sid


def test_both_surfaces_carry_session_artifact_versions(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _seed_checkpointed_task(
        studio,
        [
            workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/a.py", sha256="a" * 64, summary="12B"),
            workflow_checkpoints.EvidenceRef(kind="action", ref_id="act-1", sha256="c" * 64),
            workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="undigested"),
        ],
    )

    for path in (f"/api/harness/sessions/{sid}/task", f"/api/harness/sessions/{sid}/state"):
        body = client.get(path).json()
        versions = body["session_artifact_versions"]
        # The integrated route, not a private helper: one artifact, and the
        # reference without a digest is dropped rather than given a version.
        assert [v["artifact_id"] for v in versions] == ["src/a.py"], path
        assert versions[0]["session_id"] == sid
        assert versions[0]["rechecked_against_current_content"] is False
        assert versions[0]["is_global_lineage"] is False
        assert versions[0]["is_workspace_snapshot"] is False

    # The pane still keeps the global lineage in its own row; this slice does not
    # touch the markup, because every source-bound browser capture pins index.html's
    # digest and re-rendering it would invalidate evidence that cannot be re-made here.
    html = HTML.read_text(encoding="utf-8")
    assert "Global lineage (not session-specific)" in html


def test_an_integrity_failure_suppresses_versions_instead_of_trusting_them(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _seed_checkpointed_task(
        studio,
        [workflow_checkpoints.EvidenceRef(kind="artifact", ref_id="src/a.py", sha256="a" * 64)],
    )
    assert client.get(f"/api/harness/sessions/{sid}/task").json()["session_artifact_versions"]

    # Corrupt the stored payload so the digest check fails for real: the runtime
    # itself must refuse the checkpoint, not a stubbed error string.
    def _corrupt(conn):
        conn.execute(
            "UPDATE runtime_task_checkpoints SET payload_json = ? WHERE task_id = ?",
            ('{"note":"tampered"}', sid),
        )
        conn.commit()

    studio.runtime_persistence.run_write_transaction(studio.STORAGE, studio.DB_PATH, _corrupt)

    body = client.get(f"/api/harness/sessions/{sid}/task").json()
    assert "integrity" in body["checkpoint_integrity_error"].lower()
    # A checkpoint that failed its own integrity check cannot define a version.
    assert body["session_artifact_versions"] == []
