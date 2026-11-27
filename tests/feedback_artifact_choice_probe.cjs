// Exercise the shipped chooser, target validation and feedback reads with reordered replies.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('  let harnessFeedbackPayload = null;');
const end = html.indexOf('  function renderHarnessEvidence(payload)', start);
assert.ok(start >= 0 && end > start);
const code = html.slice(start, end);
const scenario = `
let activeHarnessId = 'session-a', harnessEvidencePayload = null;
const a='a'.repeat(64), b='b'.repeat(64), identity='c'.repeat(64);
const version = (id, digest, sid='session-a') => ({artifact_id:id, content_sha256:digest, identity_sha256:identity, session_id:sid, contributes_a_verified_version:true, rechecked_against_current_content:false, is_global_lineage:false, is_workspace_snapshot:false});
const ref = (id, digest) => ({kind:'artifact', ref_id:id, sha256:digest});
const task = {checkpoint_integrity_error:null, latest_checkpoint:{task_id:'session-a', evidence_refs:[ref('one',a),ref('two',b)]}, session_artifact_versions:[version('one',a),version('two',b)]};
const options=[];
const select={disabled:true, appendChild(x){options.push(x)}, set innerHTML(x){options.length=0}, get innerHTML(){return ''}};
const status={textContent:''}, field={value:''}, host={innerHTML:''};
const nodes={harnessFeedbackArtifact:select,harnessFeedbackArtifactStatus:status,harnessFeedbackText:field,harnessFeedbackPanel:host};
const document={getElementById(id){return nodes[id]||null}, createElement(tag){assert.equal(tag,'option');return {value:'',textContent:''}}, querySelectorAll(){return []}};
const calls=[], refs=[];
function fetch(url, opts){return new Promise((resolve,reject)=>calls.push({url,opts,resolve,reject}));}
function harnessFeedbackDigest(x){return /^[0-9a-f]{64}$/.test(String(x||''))?x:'';}
function harnessFeedbackHtml(data){return 'data:'+data.name;}
function escapeHtml(x){return String(x);}
function loadHarnessReferences(s,t){refs.push({s,t});}
function invalidateHarnessReference(){refs.push('invalid');}
${code}
const watchdog=setTimeout(()=>{console.error('artifact chooser probe timed out');process.exitCode=1},3000);
(async()=>{
 harnessEvidencePayload={sessionId:'session-a',task};
 assert.equal(renderHarnessFeedbackChoices(harnessEvidencePayload).artifactId,'one');
 assert.equal(select.disabled,false); assert.equal(options.length,2);
 assert.match(options[1].textContent,/two.*bbbbbbbbbbbb/);
 chooseHarnessFeedbackArtifact('0');
 assert.match(calls[0].url,/artifact_id=one/); assert.equal(refs[0].t.artifactId,'one');
 field.value='draft one';
 chooseHarnessFeedbackArtifact('1');
 assert.equal(field.value,''); assert.match(calls[1].url,/artifact_id=two/); assert.equal(refs[1].t.artifactId,'two');
 calls[1].resolve({ok:true,json:async()=>({name:'two'})}); await new Promise(resolve=>setImmediate(resolve));
 calls[0].resolve({ok:true,json:async()=>({name:'old one'})}); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(harnessFeedbackPayload.artifactId,'two'); assert.match(host.innerHTML,/data:two/);
 field.value='draft two'; chooseHarnessFeedbackArtifact('0'); assert.equal(field.value,'draft one');
 calls[2].resolve({ok:true,json:async()=>({name:'one'})}); await new Promise(resolve=>setImmediate(resolve));
 chooseHarnessFeedbackArtifact('1'); assert.equal(field.value,'draft two');
 calls[3].resolve({ok:true,json:async()=>({name:'two again'})}); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(renderHarnessFeedbackChoices(harnessEvidencePayload).artifactId,'two'); assert.equal(select.value,'1');
 const removedTask={...task,latest_checkpoint:{...task.latest_checkpoint,evidence_refs:[ref('one',a)]},session_artifact_versions:[version('one',a)]};
 assert.equal(renderHarnessFeedbackChoices({sessionId:'session-a',task:removedTask}),null);
 assert.equal(select.disabled,false); assert.equal(select.value,''); assert.match(status.textContent,/previously selected version is no longer available/);
 field.value='sending two'; const pending=submitHarnessFeedback(null); const posted=calls[4];
 assert.equal(JSON.parse(posted.opts.body).artifact_id,'two');
 chooseHarnessFeedbackArtifact('0'); assert.equal(field.value,'draft one');
 calls[5].resolve({ok:true,json:async()=>({name:'one new'})}); await new Promise(resolve=>setImmediate(resolve));
 posted.resolve({ok:true,json:async()=>({name:'late write'})}); await pending;
 assert.equal(harnessFeedbackPayload.artifactId,'one'); assert.equal(field.value,'draft one');
 const before=calls.length; chooseHarnessFeedbackArtifact('1e0'); assert.equal(calls.length,before); assert.equal(harnessFeedbackPayload,null); assert.match(status.textContent,/unavailable/); assert.equal(refs.at(-1),'invalid');
 activeHarnessId='session-b'; const refsBefore=refs.length; chooseHarnessFeedbackArtifact('0'); assert.equal(calls.length,before); assert.equal(harnessFeedbackPayload,null); assert.equal(refs.at(-1),'invalid'); assert.equal(refs.length,refsBefore+1);
 assert.equal(renderHarnessFeedbackChoices(harnessEvidencePayload),null); assert.equal(select.disabled,true); assert.equal(options.length,0);
 activeHarnessId='session-a';
 assert.equal(harnessFeedbackTargets({...task, checkpoint_integrity_error:''},'session-a').length,2);
 assert.equal(harnessFeedbackTargets({...task, checkpoint_integrity_error:undefined},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task, checkpoint_integrity_error:'broken'},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task, latest_checkpoint:{...task.latest_checkpoint,task_id:'foreign'}},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task, latest_checkpoint:{...task.latest_checkpoint,evidence_refs:[ref('one',a)]}},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task,session_artifact_versions:[version('one',a)]},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task,latest_checkpoint:{...task.latest_checkpoint,evidence_refs:[null]}},'session-a'),null);
 const singleTask={...task,latest_checkpoint:{...task.latest_checkpoint,evidence_refs:[ref('one',a)]},session_artifact_versions:[version('one',a)]};
 assert.equal(harnessFeedbackTargets({...singleTask,latest_checkpoint:{...singleTask.latest_checkpoint,evidence_refs:[ref('one',a),{kind:'metric',ref_id:'x'}]}},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...singleTask,session_artifact_versions:[version('one',a,'foreign')]},'session-a'),null);
 for (const [flag, bad] of [['contributes_a_verified_version',false], ['rechecked_against_current_content',true], ['is_global_lineage',true], ['is_workspace_snapshot',true]]) {
  assert.equal(harnessFeedbackTargets({...singleTask,session_artifact_versions:[{...version('one',a),[flag]:bad}]},'session-a'),null);
 }
 assert.equal(harnessFeedbackTargets({...singleTask,session_artifact_versions:[{...version('one',a),identity_sha256:'bad'}]},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...singleTask,session_artifact_versions:[{...version('one',a),content_sha256:'bad'}]},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...singleTask,session_artifact_versions:[version('one',a),version('one',a)]},'session-a'),null);
 assert.equal(harnessFeedbackTargets({...task,latest_checkpoint:null},'session-a'),null);
 assert.deepEqual(harnessFeedbackTargets({checkpoint_integrity_error:null,latest_checkpoint:null,session_artifact_versions:[]},'session-a'),[]);
 harnessFeedbackPreferredTargets.delete('session-a');
 harnessEvidencePayload={sessionId:'session-a',task:singleTask}; renderHarnessFeedbackChoices(harnessEvidencePayload);
 assert.equal(select.disabled,true); assert.equal(options.length,1); assert.match(status.textContent,/One recorded artifact/);
 const hostile='<img src=x onerror=alert(1)>';
 const hostileTask={...singleTask,latest_checkpoint:{...singleTask.latest_checkpoint,evidence_refs:[ref(hostile,a)]},session_artifact_versions:[version(hostile,a)]};
 harnessEvidencePayload={sessionId:'session-a',task:hostileTask}; renderHarnessFeedbackChoices(harnessEvidencePayload);
 assert.match(options[0].textContent,/^<img src=x/);
 const more=Array.from({length:21},(_,i)=>version('name'+i,a));
 const longTask={checkpoint_integrity_error:null,latest_checkpoint:{task_id:'session-a',evidence_refs:more.map(v=>ref(v.artifact_id,a))},session_artifact_versions:more};
 harnessEvidencePayload={sessionId:'session-a',task:longTask}; renderHarnessFeedbackChoices(harnessEvidencePayload);
 assert.equal(options.length,20); assert.match(status.textContent,/Showing 20 of 21/);
 chooseHarnessFeedbackArtifact('20'); assert.equal(calls.length,before); assert.match(status.textContent,/unavailable/);
 clearTimeout(watchdog);console.log('feedback artifact chooser probe passed');
})().catch(e=>{clearTimeout(watchdog);console.error(e);process.exitCode=1});
`;
vm.runInNewContext(scenario,{assert,console,process,setTimeout,clearTimeout,setImmediate},{filename:'feedback-artifact-choice.js'});
