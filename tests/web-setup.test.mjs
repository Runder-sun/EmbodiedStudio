import test from 'node:test';
import assert from 'node:assert/strict';

const ids = ['open-settings','setup-close','setup-content','setup-recipe','setup-install-form',
  'setup-tunnel-host','setup-tunnel-port','setup-tunnel-remote-port','setup-tunnel-command',
  'setup-dialog','setup-form','setup-error','setup-summary','setup-config-status','setup-runtime-mode',
  'setup-workspace-root','setup-data-root','setup-python','setup-ssh-host','setup-codex-binary','setup-codex-script',
  'setup-codex-model','setup-gpu-lock-root','setup-port','setup-gpus','setup-save','setup-activate','setup-target',
  'setup-motion-dependencies','setup-recipes','setup-installs','setup-probe-result','setup-refresh','setup-install'];

function documentFixture() {
  function element(tag = 'div') {
    const attributes = {}, listeners = new Map(), classes = new Set();
    let value = '', content = '';
    const e = { tagName:tag.toUpperCase(), id:'', children:[], dataset:{}, hidden:false, disabled:false,
      checked:false, selected:false, multiple:false, open:false, required:false, type:'', style:{},
      get className(){return [...classes].join(' ');}, set className(v){classes.clear();String(v).split(/\s+/).filter(Boolean).forEach(v=>classes.add(v));},
      classList:{add(...v){v.forEach(v=>classes.add(v));},remove(...v){v.forEach(v=>classes.delete(v));},
        toggle(v,force){const next=force??!classes.has(v);next?classes.add(v):classes.delete(v);return next;},contains(v){return classes.has(v);}},
      get value(){return this.tagName==='SELECT' ? this.options.find(o=>o.selected)?.value ?? value : value;},
      set value(v){value=String(v??'');if(this.tagName==='SELECT')this.options.forEach(o=>o.selected=o.value===value);},
      get textContent(){return content+this.children.map(child=>typeof child==='string'?child:child.textContent??'').join('');},
      set textContent(v){content=String(v??'');this.children=[];},
      get options(){return this.children.filter(child=>child.tagName==='OPTION');},
      get selectedOptions(){return this.options.filter(o=>o.selected);},
      append(...children){for(const child of children){if(typeof child==='object')child.parentElement=this;this.children.push(child);}},
      appendChild(child){this.append(child);return child;},
      replaceChildren(...children){content='';this.children=[];this.append(...children);},
      remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(child=>child!==this);},
      setAttribute(name,v){attributes[name]=String(v);if(name==='id')this.id=String(v);if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);},
      getAttribute(name){if(name==='id')return this.id||null;if(name.startsWith('data-'))return this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null;return attributes[name]??null;},
      removeAttribute(name){delete attributes[name];},
      addEventListener(name,handler){const rows=listeners.get(name)||[];rows.push(handler);listeners.set(name,rows);},
      querySelectorAll(selector){return descendants(this).filter(row=>matches(row,selector));},
      querySelector(selector){return this.querySelectorAll(selector)[0]??null;},
      closest(selector){let current=this;while(current){if(matches(current,selector))return current;current=current.parentElement;}return null;},
      showModal(){this.open=true;}, close(){this.open=false;}, focus(){}, checkValidity(){return true;}, reportValidity(){return true;},
      async fire(name){if(name==='click'&&this.disabled)return;const event={target:this,currentTarget:this,preventDefault(){}};
        if(this['on'+name])await this['on'+name](event);for(const handler of listeners.get(name)||[])await handler(event);},
    };
    return e;
  }
  function descendants(e){return e.children.flatMap(child=>typeof child==='object'?[child,...descendants(child)]:[]);}
  function matches(e,selector){
    return selector.split(',').some(part=>{
      part=part.trim().split(/\s+/).at(-1);
      const checked=part.includes(':checked');part=part.replace(':checked','');if(checked&&!e.checked&&!e.selected)return false;
      const id=part.match(/#([\w-]+)/);if(id&&e.id!==id[1])return false;
      const tag=part.match(/^[a-z]+/i);if(tag&&e.tagName!==tag[0].toUpperCase())return false;
      for(const match of part.matchAll(/\.([\w-]+)/g))if(!e.classList.contains(match[1]))return false;
      for(const match of part.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)){
        const value=e.getAttribute(match[1]);if(value===null||match[2]!==undefined&&value!==match[2])return false;
      }
      return true;
    });
  }
  const root=element('body');
  for(const id of ids){const e=element(id==='setup-dialog'?'dialog':id.endsWith('-form')?'form':
    ['setup-runtime-mode','setup-target','setup-recipe'].includes(id)?'select':'div');e.id=id;root.append(e);}
  for(const [id,values] of [['setup-target',['effective','draft']],['setup-runtime-mode',['local','ssh']]]){
    const select=root.querySelector('#'+id);for(const value of values){const option=element('option');option.value=value;select.append(option);}select.value=values[0];
  }
  for(const target of ['workspace','compute','ssh']){const e=element('button');e.dataset.setupProbe=target;root.append(e);}
  return {documentElement:{},body:root,defaultView:{confirm(){return true;}},createElement:element,createTextNode:text=>String(text),
    getElementById:id=>root.querySelector('#'+id),querySelectorAll:s=>root.querySelectorAll(s),querySelector:s=>root.querySelector(s)};
}

const effective = {port:8765,workspaceRoot:'/workspace',runtimeMode:'ssh',remoteHost:'test-server',
  remoteRoot:'/data',remotePython:'/data/env/bin/python',legacyRoot:'/old-read-only',codexBinary:'codex',
  codexModel:'test-model',gpuLockRoot:'/data/shared-gpu-locks',gpuDevices:[{index:3,uuid:'GPU-server-existing'}]};
const recipes = [
  {id:'workspace-dependencies',effect:'prepare',host:'studio',available:true,source:'package-lock.json',licenseIds:[],
    options:[{key:'destination',type:'path',required:true}]},
  {id:'test-runtime',effect:'prepare',host:'compute',available:true,source:'https://example.invalid/runtime',licenseIds:['runtime-license'],
    options:[{key:'destination',type:'path',required:true}]},
  {id:'test-assets',effect:'prepare',action:'assets',host:'compute',available:true,source:'https://example.invalid/assets',licenseIds:[],
    options:[{key:'destination',type:'path',required:true},{key:'packageIds',type:'packages',required:true}],
    packages:[{id:'bowl',name:'Bowl',bytes:123,licenseIds:['bowl-license']},{id:'table',name:'Table',bytes:456,licenseIds:['table-license']}]},
];
function snapshot(){return {configuration:{configured:true,file:'/workspace/studio.config.json',fileHash:'file-v1',editHash:'edit-v1',
  effectiveHash:'effective-v1',effective:structuredClone(effective),draftHash:null,draft:null,diagnostics:[]},
  platform:{platform:'darwin',arch:'arm64'},busy:{resources:[]},recipes:structuredClone(recipes),installations:[],lastProbe:null};}

async function page({state=snapshot(),fail,before}={}){
  const {createSetupView}=await import('../web/setup.js');
  const document=documentFixture(),requests=[],notifications=[];let activations=0;
  const api=async(path,options={})=>{
    const request={path,method:options.method||'GET',body:structuredClone(options.body)};requests.push(request);
    await before?.(request);
    const error=fail?.(request);if(error)throw error;
    if(path==='/api/setup')return structuredClone(state);
    if(path==='/api/setup/probe'){
      if(options.body.settings?.gpuIndices!==undefined)throw new Error('不支持配置字段：gpuIndices');
      return {scope:'read-only',checkedAt:'2026-10-25T00:00:00Z',
      workspace:{platform:'darwin',codex:{status:'available',loggedIn:true}},
      compute:{platform:'linux',runtimeVerified:false,gpus:[{index:7,uuid:'GPU-measured-seven',name:'RTX Test',memoryFreeMiB:9000}],
        formalExecution:{supportedPlatform:true,prerequisitesSatisfied:true,runtimeVerified:false}}};
    }
    if(path==='/api/setup/config'&&options.body.action==='save'){
      state.configuration.draft=structuredClone(options.body.value);state.configuration.draftHash='draft-v2';state.configuration.editHash='draft-v2';
      return {saved:true,activated:false,draftHash:'draft-v2',effectiveHash:'effective-v1'};
    }
    if(path==='/api/setup/config'&&options.body.action==='activate')return {saved:true,activated:true,restartRequired:true,draftHash:'draft-v2'};
    if(path==='/api/setup/install')return {requestId:options.body.requestId,status:'completed',registered:false,runtimeVerified:false};
    if(path.startsWith('/api/setup/install/'))return {requestId:path.split('/').at(-1),status:'completed',events:[],nextCursor:0};
    throw new Error('Unexpected request: '+path);
  };
  const t=(key,values={})=>String(key).replace(/\{(\w+)\}/g,(m,key)=>Object.hasOwn(values,key)?String(values[key]):m);
  const view=createSetupView({document,api,t,i18n:{apply(){},get locale(){return 'zh-CN';}},
    toast:(...args)=>notifications.push(args),onActivated:()=>activations++});
  return {view,document,requests,state,notifications,get activations(){return activations;},
    get:id=>document.getElementById(id),
    async click(id){const button=document.getElementById(id);if(button.disabled)return;await button.fire('click');
      if(id==='setup-install')await document.getElementById('setup-install-form').fire('submit');},
    async chooseRecipe(id){const control=document.getElementById('setup-recipe');assert.ok(control,'an explicit recipe selector is shown');control.value=id;await control.fire('change');},
    async save(){await document.getElementById('setup-form').fire('submit');},
    async probe(target){const button=document.querySelector('[data-setup-probe="'+target+'"]');await button.fire('click');},
    async accept(id){const input=document.querySelectorAll('input[data-setup-license]').find(row=>row.value===id);assert.ok(input,'license has an explicit unchecked control: '+id);assert.equal(input.checked,false);input.checked=true;await input.fire('change');},
  };
}
const mutations=p=>p.requests.filter(row=>row.method!=='GET');

test('setup initialization is inert and opening only reads setup metadata',async t=>{
  const p=await page();t.after(()=>p.view.close());
  p.view.applyState({config:{configured:true}});
  assert.deepEqual(p.requests,[]);await p.view.open();
  assert.deepEqual(p.requests.map(row=>[row.path,row.method]),[['/api/setup','GET']]);
  assert.equal(p.get('setup-dialog').open,true);assert.deepEqual(mutations(p),[]);
});

test('a legacy server without a setup endpoint leaves a visible local error instead of rejecting open',async t=>{
  const p=await page({fail:row=>row.path==='/api/setup'?new Error('404 setup unavailable'):null});t.after(()=>p.view.close());
  await assert.doesNotReject(()=>p.view.open());
  assert.match(p.get('setup-error').textContent,/404 setup unavailable/);
  assert.equal(p.get('setup-dialog').open,true);assert.deepEqual(mutations(p),[]);
});

test('workspace and compute checks never install, activate or claim verified robot execution',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();
  await p.probe('workspace');await p.probe('compute');
  assert.deepEqual(mutations(p).map(row=>row.path),['/api/setup/probe','/api/setup/probe']);
  assert.deepEqual(mutations(p).map(row=>row.body.target),['workspace','compute']);
  assert.match(p.get('setup-probe-result').textContent,/2026-10-25|runtimeVerified|尚未|未验证|元数据/);
  assert.doesNotMatch(p.get('setup-probe-result').textContent,/正式仿真已通过|机器人任务已验证|所有平台.*(?:可用|成功)/);
});

test('saving an edited draft preserves prior fields and never activates the current workspace',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();
  p.get('setup-workspace-root').value='/workspace-next';p.get('setup-data-root').value='/data-next';await p.save();
  const rows=mutations(p);assert.equal(rows.length,1);assert.equal(rows[0].path,'/api/setup/config');
  assert.equal(rows[0].body.action,'save');assert.equal(rows[0].body.baseHash,'edit-v1');
  assert.equal(rows[0].body.value.workspaceRoot,'/workspace-next');assert.equal(rows[0].body.value.remoteRoot,'/data-next');
  assert.equal(rows[0].body.value.legacyRoot,'/old-read-only');assert.equal(p.activations,0);
  assert.equal(p.state.configuration.effectiveHash,'effective-v1');
});

test('activation is a separate explicit action bound to the latest saved draft hash',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.save();
  assert.equal(mutations(p).filter(row=>row.body?.action==='activate').length,0);
  await p.click('setup-activate');
  const rows=mutations(p).filter(row=>row.body?.action==='activate');assert.equal(rows.length,1);
  assert.deepEqual(rows[0].body,{action:'activate',draftHash:'draft-v2'});
  assert.equal(p.activations,1);
});

test('a stale draft conflict is reported without retrying or silently activating effective configuration',async t=>{
  const p=await page({fail:row=>row.path==='/api/setup/config'?Object.assign(new Error('configuration changed 409'),{status:409}):null});
  t.after(()=>p.view.close());await p.view.open();await p.save();
  assert.equal(mutations(p).length,1);assert.match(p.get('setup-error').textContent,/configuration changed 409/);
  assert.equal(p.activations,0);
});

test('an install requires an explicit recipe, required destination and unaccepted license',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.click('setup-install');
  assert.equal(mutations(p).length,0);
  await p.chooseRecipe('test-runtime');await p.click('setup-install');assert.equal(mutations(p).length,0);
  p.get('setup-option-destination').value='/data/new-runtime';await p.click('setup-install');assert.equal(mutations(p).length,0);
  await p.accept('runtime-license');await p.click('setup-install');
  const row=mutations(p).find(row=>row.path==='/api/setup/install');assert.ok(row);
  assert.equal(row.body.configuration,'effective');assert.equal(row.body.targetHash,'effective-v1');
  assert.equal(row.body.items.length,1);assert.deepEqual(row.body.items[0],{
    recipeId:'test-runtime',options:{destination:'/data/new-runtime'},acceptedLicenses:['runtime-license']});
  assert.equal(typeof row.body.requestId,'string');assert.ok(row.body.requestId.length>0);
});

test('asset installation includes only explicitly selected packages and their accepted licenses',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.chooseRecipe('test-assets');
  p.get('setup-option-destination').value='/data/assets/staging/bowl';
  const packages=p.get('setup-option-packageIds');assert.ok(packages.multiple);
  assert.equal(packages.selectedOptions.length,0,'no whole-library or first-package default selection');
  await p.click('setup-install');assert.equal(mutations(p).length,0);
  for(const option of packages.options)option.selected=option.value==='bowl';await packages.fire('change');
  await p.accept('bowl-license');await p.click('setup-install');
  const row=mutations(p).find(row=>row.path==='/api/setup/install');assert.ok(row);
  assert.deepEqual(row.body.items,[{recipeId:'test-assets',options:{destination:'/data/assets/staging/bowl',packageIds:['bowl']},acceptedLicenses:['bowl-license']}]);
});

test('draft installation uses the returned draft hash without altering live configuration',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.save();
  p.get('setup-target').value='draft';await p.get('setup-target').fire('change');await p.chooseRecipe('workspace-dependencies');
  p.get('setup-option-destination').value='/workspace/unused-packages';await p.click('setup-install');
  const row=mutations(p).find(row=>row.path==='/api/setup/install');assert.ok(row);
  assert.equal(row.body.configuration,'draft');assert.equal(row.body.targetHash,'draft-v2');assert.equal(p.activations,0);
});

test('GPU selection is drawn from measured devices and sends selected indices for server UUID confirmation',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.probe('compute');
  assert.match(p.get('setup-gpus').textContent,/RTX Test|GPU-measured-seven/);
  const controls=p.get('setup-gpus').querySelectorAll('input');
  const selected=controls.find(row=>String(row.value)==='7'||row.dataset.gpuIndex==='7'||row.dataset.setupGpu==='7');
  assert.ok(selected,'the measured index has a selectable control');selected.checked=true;await selected.fire('change');await p.save();
  const row=mutations(p).find(row=>row.path==='/api/setup/config');assert.deepEqual(row.body.value.gpuIndices,[7]);
  assert.ok(!(row.body.value.gpuDevices||[]).some(gpu=>gpu.uuid==='GPU-measured-seven'),'the browser does not author its own verified UUID mapping');
});

test('switching UI language preserves unsaved configuration and never runs installation',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();
  p.get('setup-data-root').value='/data/未保存';p.get('setup-codex-model').value='user-selected-model';
  await p.view.languageChanged();
  assert.equal(p.get('setup-data-root').value,'/data/未保存');assert.equal(p.get('setup-codex-model').value,'user-selected-model');
  assert.deepEqual(mutations(p),[]);
});

test('checking metadata again after selecting GPUs does not send save-only fields to the probe API',async t=>{
  const p=await page();t.after(()=>p.view.close());await p.view.open();await p.probe('compute');
  const selected=p.get('setup-gpus').querySelectorAll('input').find(row=>row.value==='7');
  selected.checked=true;await selected.fire('change');await p.probe('compute');
  const probes=mutations(p).filter(row=>row.path==='/api/setup/probe');assert.equal(probes.length,2);
  assert.ok(!Object.hasOwn(probes[1].body.settings,'gpuIndices'),'probe consumes a configuration, not the save command payload');
  assert.equal(p.get('setup-error').textContent,'');
  assert.equal(p.get('setup-gpus').querySelectorAll('input').find(row=>row.value==='7').checked,true,'a metadata refresh preserves the user GPU selection');
  await p.save();assert.deepEqual(mutations(p).find(row=>row.path==='/api/setup/config').body.value.gpuIndices,[7]);
});

test('an unconfirmed remote installer receipt stays unconfirmed rather than appearing cancelled or rolled back',async t=>{
  const state=snapshot();state.installations=[{requestId:'install-unknown',status:'unknown',effect:'prepare',startedAt:'2026-10-25T00:00:00Z'}];
  const p=await page({state});t.after(()=>p.view.close());await p.view.open();
  assert.match(p.get('setup-installs').textContent,/unknown|未确认|无法确认/);
  assert.doesNotMatch(p.get('setup-installs').querySelector('summary').textContent,/已取消|已回滚|清理完成/);
  assert.deepEqual(mutations(p),[]);
});


test('a delayed draft save preserves newer form edits and advances the next save hash',async t=>{
  let release, observed;const started=new Promise(resolve=>observed=resolve);
  const p=await page({before:async row=>{if(row.path==='/api/setup/config'&&row.body.action==='save'&&!release){observed();await new Promise(resolve=>release=resolve);}}});
  t.after(()=>p.view.close());await p.view.open();
  p.get('setup-codex-model').value='submitted-model';await p.get('setup-codex-model').fire('input');
  const saving=p.save();await started;
  p.get('setup-codex-model').value='newer-unsaved-model';await p.get('setup-codex-model').fire('input');
  release();await saving;
  assert.equal(p.get('setup-codex-model').value,'newer-unsaved-model');
  await p.save();const rows=mutations(p).filter(row=>row.body?.action==='save');
  assert.equal(rows[1].body.value.codexModel,'newer-unsaved-model');assert.equal(rows[1].body.baseHash,'draft-v2');
});


test('post-save refresh cannot overwrite edits made after PUT completes',async t=>{
 let saved=false,release,observed;const started=new Promise(resolve=>observed=resolve);
 const p=await page({before:async row=>{
  if(row.path==='/api/setup/config'&&row.body.action==='save')saved=true;
  else if(row.path==='/api/setup'&&saved&&!release){observed();await new Promise(resolve=>release=resolve);}
 }});t.after(()=>p.view.close());await p.view.open();
 p.get('setup-codex-model').value='submitted';await p.get('setup-codex-model').fire('input');
 const saving=p.save();await started;
 p.get('setup-codex-model').value='newer-after-put';await p.get('setup-codex-model').fire('input');
 release();await saving;assert.equal(p.get('setup-codex-model').value,'newer-after-put');
});
test('GPU choice made while metadata refresh is pending survives the reply',async t=>{
 let probes=0,release,observed;const started=new Promise(resolve=>observed=resolve);
 const p=await page({before:async row=>{if(row.path==='/api/setup/probe'&&++probes===2){observed();await new Promise(resolve=>release=resolve);}}});
 t.after(()=>p.view.close());await p.view.open();await p.probe('compute');
 const probing=p.probe('compute');await started;
 const selected=p.get('setup-gpus').querySelectorAll('input').find(row=>row.value==='7');
 selected.checked=true;await selected.fire('change');release();await probing;
 assert.equal(p.get('setup-gpus').querySelectorAll('input').find(row=>row.value==='7').checked,true);
});

test('a late compute reply for an edited host is not shown as the current detection',async t=>{
 let release,observed;const started=new Promise(resolve=>observed=resolve);
 const p=await page({before:async row=>{if(row.path==='/api/setup/probe'){observed();await new Promise(resolve=>release=resolve);}}});
 t.after(()=>p.view.close());await p.view.open();const probing=p.probe('compute');await started;
 p.get('setup-ssh-host').value='different-server';await p.get('setup-ssh-host').fire('input');
 release();await probing;assert.equal(p.get('setup-probe-result').textContent,'');
 assert.equal(p.get('setup-gpus').querySelectorAll('input').length,0);
});

test('a receipt left by a lost host reports an unconfirmed status rather than claiming remote installation stopped',async t=>{
 const state=snapshot();state.installations=[{requestId:'lost_host',status:'interrupted'}];
 const p=await page({state});t.after(()=>p.view.close());await p.view.open();
 assert.match(p.get('setup-installs').textContent,/状态待确认/);
 assert.doesNotMatch(p.get('setup-installs').textContent,/安装已中断/);
});
