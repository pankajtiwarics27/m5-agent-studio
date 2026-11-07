/** Real picker markup/controller with synthetic host responses and zero networking. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const module = await import(pathToFileURL(resolve(process.env.STUDIO_PUPPETEER_CORE)).href);
const source = readFileSync(new URL('../../static/index.html', import.meta.url), 'utf8');
const block = (start, end) => {
  const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, `${start} / ${end}`);
  return source.slice(from, to);
};
const browser = await (module.default || module).launch({executablePath: process.env.STUDIO_SCREENSHOT_CHROME, headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']});
let passed = 0;
try {
  const page = await browser.newPage(), network = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setRequestInterception(true);
  page.on('request', request => { network.push(request.url()); request.abort(); });
  await page.setContent(source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ''), {waitUntil:'domcontentloaded'});
  await page.addScriptTag({content: `
    ${block('  const overlayStack = []', '  let knownModels = []')}
    const unifiedCatalog = [{id:'fixture-model', label:'Offline fixture'}];
    const startWorkspace = {};
    function mcpWizardRefresh() {}
    const clearPresentation = () => {}, renderStartWorkspace = () => {}, renderStartHealth = () => {};
    const calls = [], holds = {}, releases = {};
    window.calls = calls; window.holds = holds;
    window.release = key => { holds[key] = false; releases[key]?.(); };
    window.fetch = async (url, init = {}) => {
      const parsed = new URL(url, 'https://studio.invalid');
      const route = parsed.pathname, path = parsed.searchParams.get('path') || '/host';
      calls.push({route, query:parsed.searchParams.get('query'), method:init.method || 'GET', body:init.body ? JSON.parse(init.body) : null});
      if (holds[route]) await new Promise(resolve => releases[route] = resolve);
      const entries = [{name:"project's folder",path:"/host/project's folder",kind:'directory'}];
      if (parsed.searchParams.get('kind') === 'file') entries.push({name:"runner's script.py", path:"/host/runner's script.py",kind:'file'});
      let data = {path, parent:path === '/host' ? null : '/host', roots:['/host','/recent'], entries, filesystem:'studio_host', truncated:false};
      if (route.endsWith('/folders')) {const body = JSON.parse(init.body); data = {path:body.parent + '/' + body.name, workspace_changed:false};}
      if (route.endsWith('/suggest')) data = {name:'draft-project', created:false,workspace_changed:false};
      if (path.includes('bad')) return {ok:false, json:async()=>({detail:'Fixture permission denied'})};
      return {ok:true,json:async()=>data};
    };
    async function saveWorkspaceDirectory(path, createIfMissing) {
      calls.push({route:'/api/workspace/directory', body:{dir:path, create_if_missing:createIfMissing}});
      document.getElementById('startWorkspaceInput').value = path;
      return {ok:true,dir:path,recent:[path],git:{}};
    }
    ${block('  function formatMcpArgs(', '\n  function ')}
    ${block('  let pathPickerState = null;', '  async function loadHarnessSessions(')}
    window.picker = {open:openPathPicker, load:loadPathPicker, apply:applyPathPicker, create:createPathPickerFolder, ask:askPathPickerModel, close:closePathPicker};
  `});
  const open = async (id='startWorkspaceInput', options={workspace:true}) => {
    await page.evaluate(({id, options}) => picker.open(id, options), {id,options});
    await page.waitForFunction(() => document.activeElement.id === 'pathPickerLocation');
  };
  const value = id => page.$eval(`#${id}`, node => node.value);
  const close = async () => page.evaluate(() => picker.close());
  await page.evaluate(() => { document.querySelectorAll('.pane').forEach(n=>n.classList.toggle('active',n.id==='pane-start'));  document.getElementById('startWorkspaceInput').value='/original'; });
  // Selection-first host UX must be explicit before opening any chooser.
  assert(await page.$$eval('[data-path-for]', nodes => nodes.every(n=>n.textContent.includes('Studio host'))));
  assert.equal(await page.$eval('#pathPickerApplyBtn',n=>n.getAttribute('aria-describedby')),'pathPickerSelectionHint');
  passed++;
  // Owner test4: Start must be scrollable BEFORE invoking the picker. The old walk
  // focused an offscreen field/programmatically opened the dialog, masking clipping.
  await page.evaluate(() => {
    const blocker = document.getElementById('startNextBlocker');
    blocker.hidden = false;
    blocker.innerHTML = '<h3>Connect a model source</h3><p>Offline fixture: local runtime not asked; no model request is needed to browse.</p>';
    document.getElementById('startRecentList').innerHTML = Array.from({length:5}, (_,i) =>
      `<button type="button" class="start-recent-row"><span class="start-recent-main"><span class="start-recent-title">Synthetic recent task ${i}</span><span class="start-recent-meta">0 steps · plan · offline fixture</span></span></button>`).join('');
  });
  const browse = '#startWorkspaceDetails [data-path-for="startWorkspaceInput"]';
  for (const theme of ['light','dark']) for (const density of ['comfortable','compact']) {
    for (const [width,height] of [[390,568],[900,700],[1440,810],[1572,865]]) {
      const label = `${theme}/${density}/${width}x${height}`;
      await page.setViewport({width,height});
      await page.evaluate(({theme,density}) => {
        document.body.dataset.theme=theme; document.body.dataset.density=density;
        document.querySelectorAll('#pane-start details').forEach(n=>n.open=false);
        document.querySelector('.onboarding-shell').scrollTop=0;
      }, {theme,density});
      const layout = await page.$eval('.onboarding-shell', n => ({
        height:n.clientHeight, content:n.scrollHeight, bottom:n.getBoundingClientRect().bottom,
        overflow:getComputedStyle(n).overflowY, top:n.getBoundingClientRect().top,
        x:n.getBoundingClientRect().left+n.clientWidth/2,
      }));
      assert(layout.overflow==='auto' && layout.content>layout.height && layout.height>0 && layout.bottom<=height+1,
        `Start scroll owner ${label}: ${JSON.stringify(layout)}`);
      // A real wheel must reveal the collapsed workspace; no scrollIntoView/helper.
      await page.mouse.move(layout.x, (layout.top+layout.bottom)/2);
      await page.mouse.wheel({deltaY:4000});
      await page.waitForFunction(()=>document.querySelector('.onboarding-shell').scrollTop>0, {timeout:1500});
      await page.click('#startWorkspaceDetails > summary');
      await page.mouse.wheel({deltaY:4000});
      await page.waitForFunction(selector=>{
        const r=document.querySelector(selector).getBoundingClientRect(); return r.top>=0 && r.bottom<=innerHeight;
      }, {timeout:1500}, browse);
      await page.click(browse);
      await page.waitForFunction(()=>document.activeElement.id==='pathPickerLocation');
      await page.click('.path-picker-entry');
      await page.waitForFunction(()=>document.getElementById('pathPickerLocation').value.includes("project's folder"));
      await page.click('.path-picker-actions button:first-child'); // real Cancel
      assert.equal(await value('startWorkspaceInput'),'/original');
      assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('data-path-for')),'startWorkspaceInput');
      // Keyboard-only return from the top: Tab reveals each native disclosure;
      // Space opens workspace, Tab reaches its input and Browse, Enter opens it.
      await page.evaluate(()=>document.querySelectorAll('#pane-start details').forEach(n=>n.open=false));
      await page.focus('#startTaskInput');
      for (let i=0;i<30;i++) {
        await page.keyboard.press('Tab');
        if (await page.evaluate(()=>document.activeElement.matches('#startWorkspaceDetails > summary'))) break;
      }
      assert(await page.evaluate(()=>document.activeElement.matches('#startWorkspaceDetails > summary')),`Keyboard summary ${label}`);
      await page.keyboard.press('Space'); await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('data-path-for')),'startWorkspaceInput');
      await page.keyboard.press('Enter');
      await page.waitForFunction(()=>document.activeElement.id==='pathPickerLocation');
      await page.keyboard.press('Escape');
      assert.equal(await value('startWorkspaceInput'),'/original');
      assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('data-path-for')),'startWorkspaceInput');
      assert.equal(await page.evaluate(()=>calls.filter(c=>c.method==='POST'||c.route.endsWith('/directory')).length),0);
      passed++;
    }
  }
  // Themes, densities, narrow/short and wide viewports; focus containment and return.
  for (const theme of ['light','dark']) for (const density of ['comfortable','compact']) for (const width of [320,390,900,1440]) {
    await page.setViewport({width,height:width===320?568:900});
    await page.evaluate(({theme,density}) => { document.body.dataset.theme=theme; document.body.dataset.density=density; }, {theme,density});
    await page.$eval('#startWorkspaceInput', n=>{for(let p=n.parentElement;p;p=p.parentElement) if(p.tagName==='DETAILS')p.open=true;});
    await page.focus('#startWorkspaceInput');
    await open();
    const layout = await page.$eval('.path-picker', node => {
      const r=node.getBoundingClientRect();
      return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight,overflow:node.scrollWidth>node.clientWidth+1};
    });
    assert(layout.left>=-1 && layout.right<=width+1 && layout.top>=-1 && layout.bottom<=layout.height+1 && !layout.overflow, JSON.stringify(layout));
    assert.match(await page.$eval('#pathPickerHostNote', n=>n.textContent), /Studio host.*not necessarily this browser/s);
    await page.focus('#pathPickerApplyBtn'); await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-label')), 'Close path picker');
    await page.keyboard.down('Shift'); await page.keyboard.press('Tab'); await page.keyboard.up('Shift');
    assert.equal(await page.evaluate(()=>document.activeElement.id), 'pathPickerApplyBtn');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>document.activeElement.id), 'startWorkspaceInput');
    assert.equal(await value('startWorkspaceInput'), '/original');
    passed++;
    // Expanded creation/model forms scroll in the body, not over Choose/Cancel.
    await open();
    assert.equal(await page.$eval('#pathPickerNewFolder', n=>n.open),false);
    assert.equal(await page.$eval('.path-picker-ai', n=>n.open),false);
    await page.$eval('#pathPickerNewFolder', n=>n.open=true);
    await page.$eval('.path-picker-ai', n=>n.open=true);
    await page.$eval('#pathPickerSelected', n=>n.textContent='/host/'+'long-folder/'.repeat(80));
    const footer = await page.$eval('.path-picker-footer', n=>{
      const r=n.getBoundingClientRect(), body=document.querySelector('.path-picker-body');
      return {top:r.top,bottom:r.bottom,overflow:n.scrollWidth>n.clientWidth+1,
        scroll:getComputedStyle(body).overflowY,content:body.scrollHeight,height:body.clientHeight};
    });
    assert(footer.top>=0 && footer.bottom<=layout.height+1 && !footer.overflow && footer.scroll==='auto' && footer.content>footer.height, JSON.stringify(footer));
    await page.focus('#pathPickerApplyBtn');
    const action = await page.$eval('#pathPickerApplyBtn', n=>{const r=n.getBoundingClientRect();return {top:r.top,bottom:r.bottom};});
    assert(action.top>=0 && action.bottom<=layout.height+1,JSON.stringify(action));
    await page.keyboard.press('Escape');
    passed++;
  }
  for (const [id,options,label,hint] of [
    ['startWorkspaceInput',{workspace:true},'Choose folder',/shared workspace/],
    ['mcpCommand',{kind:'file'},'Choose file',/draft/],
    ['mcpArgs',{kind:'file',append:true},'Choose file',/quoted/],
    ['mcpEnvJson',{env:true},'Choose folder',/named environment/],
  ]) {
    await open(id,options);
    assert.equal(await page.$eval('#pathPickerApplyBtn',n=>n.textContent),label);
    assert.match(await page.$eval('#pathPickerSelectionHint',n=>n.textContent),hint);
    assert.equal(await page.$eval('.path-picker-ai',n=>n.open),false);
    await close(); passed++;
  }
  await open();
  await page.focus('#pathPickerLocation'); await page.keyboard.down('Control');
  await page.keyboard.press('A'); await page.keyboard.up('Control');
  await page.keyboard.type('/recent');
  const beforeEnter = await page.evaluate(()=>calls.length);
  await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.getElementById('pathPickerSelected').textContent==='/recent');
  assert.equal(await page.evaluate(()=>calls.length),beforeEnter+1);
  await page.type('#pathPickerSearch','project'); await page.keyboard.press('Enter');
  await page.waitForFunction(()=>calls.at(-1).query==='project');
  assert.equal(await page.evaluate(()=>calls.length),beforeEnter+2);
  assert.equal(await value('startWorkspaceInput'),'/original');
  await close(); passed++;
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.method==='POST'||c.route.endsWith('/directory')).length),0);
  passed++;
  await open(); await page.evaluate(()=>picker.load('/bad'));
  assert.equal(await page.$eval('#pathPickerApplyBtn',n=>n.disabled),true);
  assert.equal(await page.$eval('#pathPickerSelected',n=>n.textContent),'Nothing selected.');
  assert.match(await page.$eval('#pathPickerStatus',n=>n.textContent),/permission denied/);
  await page.evaluate(()=>picker.load('')); await page.select('#pathPickerRoots','/recent');
  await page.waitForFunction(()=>document.getElementById('pathPickerLocation').value==='/recent');
  await page.click('#pathPickerUp'); await page.waitForFunction(()=>document.getElementById('pathPickerLocation').value==='/host');
  passed++;
  // Explicit Use is the only workspace update; the existing-only flag cannot mkdir.
  await page.evaluate(()=>picker.apply());
  assert.deepEqual(await page.evaluate(()=>calls.find(c=>c.route.endsWith('/directory')).body),{dir:'/host',create_if_missing:false});
  passed++;
  // All four workspace Browse controls are wired to the same helper.
  for (const id of ['startWorkspaceInput','harnessWorkspaceInput','sdlcWorkspaceInput','cgWorkspaceInput']) {
    assert(source.includes(`openPathPicker('${id}', {workspace:true})`),id);
  }
  passed++;
  // Existing executable and script arguments are draft edits; quoting preserves one token.
  await open('mcpArgs',{kind:'file',append:true});
  await page.evaluate(()=>document.getElementById('mcpArgs').value="--keep 'two words'");
  await page.click('#pathPickerEntries button:last-child'); await page.evaluate(()=>picker.apply());
  assert.equal(await value('mcpArgs'),`--keep 'two words' '/host/runner'"'"'s script.py'`);
  await open('mcpCommand',{kind:'file'}); await page.click('#pathPickerEntries button:last-child'); await page.evaluate(()=>picker.apply());
  assert.equal(await value('mcpCommand'),"/host/runner's script.py");
  passed++;
  await page.evaluate(()=>document.getElementById('mcpEnvJson').value=JSON.stringify({KEEP:'original'}));
  await open('mcpEnvJson',{env:true}); await page.evaluate(()=>picker.apply());
  assert.match(await page.$eval('#pathPickerStatus',n=>n.textContent),/variable name/);
  await page.$eval('#pathPickerEnvKey',n=>n.value='PROJECT_DIR'); await page.evaluate(()=>picker.apply());
  assert.deepEqual(JSON.parse(await value('mcpEnvJson')),{KEEP:'original',PROJECT_DIR:'/host'});
  await page.evaluate(()=>document.getElementById('mcpEnvJson').value='[]');
  await open('mcpEnvJson',{env:true}); await page.$eval('#pathPickerEnvKey',n=>n.value='PROJECT_DIR'); await page.evaluate(()=>picker.apply());
  assert.equal(await value('mcpEnvJson'),'[]'); assert.match(await page.$eval('#pathPickerStatus',n=>n.textContent),/JSON object/); await close();
  passed++;
  // Description-only AI request; suggestion does not prefill, create, or apply.
  await open(); await page.$eval('#pathPickerNewFolder',n=>n.open=true); await page.$eval('.path-picker-ai',n=>n.open=true);
  await page.select('#pathPickerModel','fixture-model');
  await page.$eval('#pathPickerDescription',n=>n.value='A simple offline project');
  await page.evaluate(()=>picker.ask());
  assert.deepEqual(await page.evaluate(()=>calls.find(c=>c.route.endsWith('/suggest')).body),{model:'fixture-model',description:'A simple offline project',confirmed:true});
  assert.equal(await value('pathPickerNewName'),'');
  await page.click('#pathPickerUseSuggestion');
  assert.equal(await value('pathPickerNewName'),'draft-project');
  assert.equal(await page.$eval('#pathPickerCreateBtn',n=>n.disabled),true);
  await page.click('#pathPickerCreateConfirm'); await page.$eval('#pathPickerNewName',n=>{n.value='edited-project';n.dispatchEvent(new Event('input'));});
  assert.equal(await page.$eval('#pathPickerCreateConfirm',n=>n.checked),false);
  assert.equal(await page.evaluate(()=>calls.some(c=>c.route.endsWith('/folders'))),false);
  await page.click('#pathPickerCreateConfirm'); await page.evaluate(()=>picker.create());
  assert.equal(await value('pathPickerLocation'),'/host/edited-project');
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.route.endsWith('/directory')).length),1);
  await close(); passed++;
  // A completed mkdir must not overwrite newer navigation.
  await open();
  await page.evaluate(()=>{document.getElementById('pathPickerNewName').value='confirmed';document.getElementById('pathPickerCreateConfirm').checked=true; holds['/api/workspace/picker/folders']=true; window.creating=picker.create();});
  await page.evaluate(()=>picker.load('/recent'));
  await page.evaluate(async()=>{release('/api/workspace/picker/folders');await window.creating;});
  assert.equal(await value('pathPickerLocation'),'/recent'); await close(); passed++;
  // Closed/stale responses cannot alter a new dialog or leave Ask disabled.
  await open();
  await page.evaluate(()=>{document.getElementById('pathPickerModel').value='fixture-model';document.getElementById('pathPickerDescription').value='Pending';holds['/api/workspace/picker/suggest']=true;window.asking=picker.ask();});
  await close(); await open();
  assert.equal(await page.$eval('#pathPickerAskModel',n=>n.disabled),false);
  await page.evaluate(async()=>{release('/api/workspace/picker/suggest');await window.asking;});
  assert.equal(await page.$eval('#pathPickerSuggestion',n=>n.textContent),'');
  await page.evaluate(()=>{holds['/api/workspace/picker']=true;window.loading=picker.load('/stale');});
  await close(); await page.evaluate(()=>release('/api/workspace/picker')); await open();
  assert.equal(await value('pathPickerLocation'),'/host'); await close(); passed++;
  assert.deepEqual(errors,[]); assert.deepEqual(network,[]);
  console.log(JSON.stringify({passed,network:network.length,errors}));
} finally { await browser.close(); }
