import test from 'node:test';
import http from 'node:http';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { loadConfiguration, quote } from '../src/config.mjs';
import { acquireWorkspaceOwnership, createSetup, tunnelArguments } from '../src/setup.mjs';

async function fixture(t, options={}) {
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-setup-'));
  const file=path.join(root,'config.json');
  await writeFile(file,JSON.stringify({port:8766,workspaceRoot:path.join(root,'workspace'),runtimeMode:'local',remoteRoot:path.join(root,'data'),
    remotePython:process.execPath,gpuDevices:[],codexBinary:'codex'}));
  const configuration=loadConfiguration({file});
  const owner=await acquireWorkspaceOwnership({workspaceRoot:configuration.value.workspaceRoot,configFile:file,port:8766});
  const setup=await createSetup({configuration,owner,...options});
  t.after(async()=>{await setup.close();await owner.release();await rm(root,{recursive:true,force:true});});
  return {root,file,configuration,owner,setup};
}

test('a live workspace owner cannot be stolen by another config or an offline CLI', async t => {
  const f=await fixture(t);
  await assert.rejects(acquireWorkspaceOwnership({workspaceRoot:f.configuration.value.workspaceRoot,configFile:path.join(f.root,'other.json'),port:9999}),{status:409});
  assert.equal((await f.setup.inspect()).ownership.status,'owned');
});

test('draft edits do not change effective target or stop existing work', async t => {
  let active=true;
  const f=await fixture(t,{resourceBusy:()=>active?['SYSTEM_VERIFICATION_ONLY generation']:[]});
  const before=await f.setup.inspect(),raw=await readFile(f.file,'utf8');
  const value={...before.configuration.effective,codexModel:'chosen-model'};
  const saved=await f.setup.saveConfig({action:'save',baseHash:before.configuration.editHash,value});
  assert.ok(saved.draftHash);assert.equal(await readFile(f.file,'utf8'),raw);
  assert.equal((await f.setup.inspect()).configuration.effectiveHash,before.configuration.effectiveHash);
  await assert.rejects(f.setup.saveConfig({action:'activate',draftHash:saved.draftHash}),{status:409});
  active=false;const applied=await f.setup.saveConfig({action:'activate',draftHash:saved.draftHash});
  assert.equal(applied.restartRequired,true);assert.equal(f.configuration.value.codexModel,undefined);
});

test('pure probing uses fixed commands and never creates or deploys target directories', async t => {
  const calls=[];
  const f=await fixture(t,{run:async(executable,args)=>{calls.push({executable,args});return {code:0,stdout:'{}',stderr:''};}});
  const target=path.join(f.root,'data');
  const before=await f.setup.inspect();
  const probe=await f.setup.probe({target:'compute'});
  assert.equal(probe.scope,'read-only');assert.equal(probe.compute.runtimeVerified,false);
  await assert.rejects(stat(target),{code:'ENOENT'});
  assert.equal((await f.setup.inspect()).configuration.fileHash,before.configuration.fileHash);
  assert.ok(calls.every(row=>!row.args.includes('install')&&!row.args.includes('app-server')));
});

test('unused no-GPU preparation can run with a generation, is real, and never publishes an active catalog', async t => {
  const calls=[];
  const f=await fixture(t,{resourceBusy:()=>['active generation'],recipes:[{id:'system-fixture',effect:'prepare',host:'studio',licenseIds:[],
    commands:[{executable:process.execPath,args:['-e','process.stdout.write("SYSTEM_VERIFICATION_ONLY installed")']}]}],
    run:async(executable,args)=>{calls.push({executable,args});return {code:0,stdout:'SYSTEM_VERIFICATION_ONLY installed',stderr:''};}});
  const hash=(await f.setup.inspect()).configuration.effectiveHash;
  const request={requestId:'install_fixture',configuration:'effective',targetHash:hash,
    items:[{recipeId:'system-fixture',options:{destination:path.join(f.configuration.value.workspaceRoot,'.tools','new-prefix')},acceptedLicenses:[]}]};
  const accepted=await f.setup.install(request);assert.equal(accepted.status,'running');
  let result;
  for(let n=0;n<30;n++){result=await f.setup.installation(request.requestId);if(result.status!=='running')break;await new Promise(r=>setTimeout(r,5));}
  assert.equal(result.status,'completed');assert.equal(calls.length,1);
  assert.equal((await f.setup.install(request)).status,'completed');assert.equal(calls.length,1);
  await assert.rejects(f.setup.install({...request,items:[]}),{status:409});
  assert.equal(result.registered,false);
});

test('unselected recipes and raw shell/URL options cannot execute', async t => {
  let calls=0;const f=await fixture(t,{run:async()=>{calls++;return {code:0,stdout:'',stderr:''};}});
  const hash=(await f.setup.inspect()).configuration.effectiveHash;
  await assert.rejects(f.setup.install({requestId:'bad',targetHash:hash,items:[{recipeId:'arbitrary',options:{command:'rm anything'}}]}),{status:400});
  assert.equal(calls,0);
});

test('SSH tunnel argv overrides agent forwarding and never binds publicly', () => {
  const args=tunnelArguments({host:'fixture-host',localPort:8766,remotePort:8765});
  assert.ok(args.includes('-a'));assert.ok(args.includes('ForwardAgent=no'));assert.ok(args.includes('StrictHostKeyChecking=yes'));
  assert.ok(args.includes('127.0.0.1:8766:127.0.0.1:8765'));assert.ok(args.includes('-N'));
  assert.throws(()=>tunnelArguments({host:'-oProxyCommand=bad',localPort:8766,remotePort:8765}),{status:400});
});

test('setup-only startup skips an injected old Store, native producer, runtime and recovery', async t=>{
  const {createStudio}=await import('../src/server.mjs');
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-setup-only-')),file=path.join(root,'missing.json');
  const configuration=loadConfiguration({file});let loads=0,saves=0,native=0,remote=0;
  const oldStore={root:path.join(root,'old-workspace'),projects:[{id:'p_old'}],async load(){loads++;},async save(){saves++;}};
  const studio=await createStudio({configuration,store:oldStore,port:0,bridge:{async start(){native++;},status(){native++;},async close(){native++;}},
    remote:async()=>{remote++;throw Error('SDK must not start');},setupOptions:{run:async()=>({code:1,stdout:'',stderr:'SYSTEM_VERIFICATION_ONLY unavailable'})}});
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const address=await studio.start();
  const state=await (await fetch(address+'/api/state')).json();assert.equal(state.config.configured,false);assert.deepEqual(state.projects,[]);assert.equal(studio.host,null);
  assert.equal((await fetch(address+'/api/setup')).status,200);
  assert.equal((await fetch(address+'/api/setup/probe',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({target:'workspace'})})).status,200);
  const blocked=await fetch(address+'/api/projects',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});assert.equal(blocked.status,503);assert.equal((await blocked.json()).code,'setup_required');
  assert.deepEqual({loads,saves,native,remote},{loads:0,saves:0,native:0,remote:0});await assert.rejects(stat(oldStore.root),{code:'ENOENT'});
});

test('a localhost forwarded port is accepted only with its exact Origin',async t=>{
  const {createStudio}=await import('../src/server.mjs');
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-forward-origin-'));
  const studio=await createStudio({configuration:loadConfiguration({file:path.join(root,'missing.json')}),port:0});
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});const address=await studio.start();
  const status=headers=>new Promise((resolve,reject)=>{const req=http.get(address+'/api/setup',{headers},res=>{res.resume();resolve(res.statusCode);});req.on('error',reject);});
  assert.equal(await status({Host:'localhost:23456',Origin:'http://localhost:23456'}),200);
  assert.equal(await status({Host:'localhost:23456',Origin:'http://localhost:23457'}),403);
  assert.equal(await status({Host:'public.example:23456'}),403);
});

test('a real isolated installer runs selected argv and keeps partial failures and cancelled process groups',async t=>{
  const {runCommand}=await import('../src/setup.mjs');
  const child=await runCommand(process.execPath,['-e','process.stdout.write(process.argv[1])','SYSTEM_VERIFICATION_ONLY 中文 "quoted"'],{timeoutMs:1000});
  assert.equal(child.code,0);assert.equal(child.stdout,'SYSTEM_VERIFICATION_ONLY 中文 "quoted"');assert.equal(child.exitConfirmed,true);
  const controller=new AbortController();let started=false;
  const pending=runCommand(process.execPath,['-e',`process.on('SIGTERM',()=>{});console.log('owned child started');setInterval(()=>{},1000)`],{signal:controller.signal,timeoutMs:5000,onOutput(){started=true;}});
  while(!started)await new Promise(resolve=>setTimeout(resolve,5));controller.abort(new Error('explicit cancellation'));
  const result=await pending;assert.equal(result.exitConfirmed,process.platform!=='win32');assert.match(result.error,/explicit cancellation/);
});

test('a changed external config file cannot be overwritten by activation of an old draft',async t=>{
  const f=await fixture(t);const before=await f.setup.inspect();
  const draft=await f.setup.saveConfig({action:'save',baseHash:before.configuration.editHash,value:before.configuration.effective});
  await writeFile(f.file,'{"SYSTEM_VERIFICATION_ONLY":"external change"}');
  await assert.rejects(f.setup.saveConfig({action:'activate',draftHash:draft.draftHash}),{status:409});
  assert.equal(await readFile(f.file,'utf8'),'{"SYSTEM_VERIFICATION_ONLY":"external change"}');
});

test('only an explicitly selected asset license/package can become a download request',async t=>{
  const f=await fixture(t,{run:async()=>{throw Error('no download selected');}});
  const state=await f.setup.inspect(),recipe=state.recipes.find(row=>row.id==='install-artvip');assert.ok(recipe.packages.length);
  assert.equal(recipe.commands,undefined);assert.equal(recipe.packages[0].files,undefined);
  const request={requestId:'asset_license',targetHash:state.configuration.effectiveHash,items:[{recipeId:recipe.id,options:{destination:path.join(f.configuration.value.remoteRoot,'assets/staging/new'),packageIds:[recipe.packages[0].id]},acceptedLicenses:[]}]};
  await assert.rejects(f.setup.install(request),{status:422});await assert.rejects(stat(request.items[0].options.destination),{code:'ENOENT'});
});

test('finite asset acquisition verifies actual bytes and rejects archive traversal, links and expansion',async t=>{
  const {spawnSync}=await import('node:child_process');
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-finite-assets-'));t.after(()=>rm(root,{recursive:true,force:true}));
  const helper=fileURLToPath(new URL('../setup/stage-assets.py',import.meta.url));
  const script=`import importlib.util,io,json,pathlib,hashlib,tempfile,zipfile,stat,sys
spec=importlib.util.spec_from_file_location('stage',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=pathlib.Path(sys.argv[2]).resolve();content=b'SYSTEM_VERIFICATION_ONLY finite bytes'
m.urllib.request.urlopen=lambda *a,**kw:io.BytesIO(content)
q={'destination':str(root/'safe'),'repository':'fixture/official','revision':'f'*40,'files':[{'path':'object/model.usda','size':len(content),'sha256':hashlib.sha256(content).hexdigest()}]}
assert m.main(q)['staged'];assert (root/'safe/object/model.usda').read_bytes()==content
bad={**q,'destination':str(root/'bad-digest'),'files':[{**q['files'][0],'sha256':'0'*64}]}
try:m.main(bad);raise AssertionError('digest mismatch accepted')
except ValueError:pass
for kind in ['traversal','symlink','expanded','duplicate']:
 archive=root/(kind+'.zip')
 with zipfile.ZipFile(archive,'w') as z:
  info=zipfile.ZipInfo('../outside' if kind=='traversal' else 'file');info.external_attr=((stat.S_IFLNK|0o777) if kind=='symlink' else (stat.S_IFREG|0o600))<<16;z.writestr(info,b'1234')
  if kind=='duplicate':z.writestr('file',b'5678')
 try:m.extract(archive,root/kind,{'expandedBytes':1 if kind=='expanded' else 16,'entries':10});raise AssertionError(kind+' accepted')
 except ValueError:pass
print(json.dumps({'verified':True,'network':False,'SDK':False}))`;
  const result=spawnSync('python3',['-B','-c',script,helper,root],{encoding:'utf8'});assert.equal(result.status,0,result.stderr);assert.match(result.stdout,/"verified": true/);
});

test('explicit existing catalogs use the real CPU parser/index and preserve the previous catalog on failure',async t=>{
  const {registerAssetCatalog}=await import('../src/remote.mjs');
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-register-cpu-'));t.after(()=>rm(root,{recursive:true,force:true}));
  const entry=path.join(root,'assets/staging/SYSTEM_VERIFICATION_ONLY/model.usda');await mkdir(path.dirname(entry),{recursive:true});await writeFile(entry,'#usda 1.0\ndef Xform "Fixture" {}\n');
  const value={schema:'sceneagent-asset-collections-v1',collections:[{id:'fixture',revision:'v1',libraryTier:'preferred'}],assets:[{assetId:'collection:fixture:v1:model',collection:'fixture',sourceRoot:'current_assets',packageRoot:'staging/SYSTEM_VERIFICATION_ONLY',entry:'model.usda',dependencyFiles:[],format:'native_files',kind:'model',name:'SYSTEM_VERIFICATION_ONLY fixture',category:'chair'}]};
  const catalog=path.join(root,'candidate-catalog.json');await writeFile(catalog,JSON.stringify(value));const target={runtimeMode:'local',remotePython:'python3',remoteRoot:root,legacyRoot:null};
  const receipt=await registerAssetCatalog({catalogFile:catalog},{target});assert.equal(receipt.registered,true);assert.equal(receipt.assetCount,1);assert.equal(receipt.runtimeVerified,false);
  const active=path.join(root,'assets/collections/catalog.json'),before=await readFile(active,'utf8');
  await writeFile(catalog,JSON.stringify({...value,assets:[{...value.assets[0],assetId:'collection:fixture:v1:missing',entry:'missing.usd'}]}));
  await assert.rejects(registerAssetCatalog({catalogFile:catalog},{target}),/missing|Missing|dependencies/);assert.equal(await readFile(active,'utf8'),before);
});

test('concurrent exact install requests launch once and changed input cannot race the durable intent',async t=>{
  let calls=0;const f=await fixture(t,{recipes:[{id:'fixture-exact',host:'studio',effect:'prepare',licenseIds:[],commands:[{executable:process.execPath,args:['-e',''] }]}],run:async()=>{calls++;return {code:0,stdout:'',stderr:''};}});
  const before=await f.setup.inspect(),input={requestId:'same_intent',targetHash:before.configuration.effectiveHash,items:[{recipeId:'fixture-exact',options:{destination:path.join(f.owner.workspaceRoot,'new-prefix')}}]};
  const replies=await Promise.all([f.setup.install(input),f.setup.install({...input}),f.setup.install({items:input.items,targetHash:input.targetHash,requestId:input.requestId})]);assert.equal(replies.length,3);
  await assert.rejects(f.setup.install({...input,items:[{...input.items[0],options:{destination:path.join(f.owner.workspaceRoot,'other-prefix')}}]}),{status:409});
  for(let n=0;n<50;n++){if((await f.setup.installation(input.requestId)).status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}
  assert.equal(calls,1);
});

test('unknown SSH installer exit stays unknown and protects its prefix across setup restarts',async t=>{
  const f=await fixture(t);await f.setup.close();
  const value={...f.configuration.value,runtimeMode:'ssh',remoteHost:'SYSTEM_VERIFICATION_ONLY-ssh-host'};
  await writeFile(f.file,JSON.stringify(value));const configuration=loadConfiguration({file:f.file});let calls=0;
  const setup=await createSetup({configuration,owner:f.owner,recipes:[{id:'fixture-remote',host:'compute',effect:'prepare',licenseIds:[],commands:[{executable:'/usr/bin/true',args:[]}]}],run:async()=>({code:++calls===3?255:0,stdout:'{}',stderr:calls===3?'SYSTEM_VERIFICATION_ONLY SSH connection lost':'',exitConfirmed:true})});
  const before=await setup.inspect(),destination=path.posix.join(value.remoteRoot,'envs/partial-prefix'),input={requestId:'unknown_exit',targetHash:before.configuration.effectiveHash,items:[{recipeId:'fixture-remote',options:{destination}}]};
  await setup.install(input);let receipt;for(let n=0;n<50;n++){receipt=await setup.installation(input.requestId);if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}
  assert.equal(receipt.status,'unknown');assert.equal((await setup.cancel(input.requestId)).status,'unknown');await setup.close();
  const restarted=await createSetup({configuration,owner:f.owner});
  try{const saved=await restarted.saveConfig({action:'save',baseHash:before.configuration.editHash,value:{...before.configuration.effective,remotePython:path.posix.join(destination,'bin/python')}});await assert.rejects(restarted.saveConfig({action:'activate',draftHash:saved.draftHash}),{status:409});assert.ok((await restarted.inspect()).busy.resources.some(row=>row.unresolved&&row.host===value.remoteHost&&row.destinations.includes(destination)));}
  finally{await restarted.close();}
});

test('ownership with an unreadable original process identity remains read-only and release never removes another nonce',async t=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-owner-unknown-'));t.after(()=>rm(root,{recursive:true,force:true}));const workspaceRoot=path.join(root,'workspace'),configFile=path.join(root,'config.json');
  const owner=await acquireWorkspaceOwnership({workspaceRoot,configFile,port:23456});
  const previous=JSON.parse(await readFile(owner.ownerFile,'utf8'));await writeFile(owner.ownerFile,JSON.stringify({...previous,pid:999999,instanceId:'foreign-nonce'}));
  await assert.rejects(acquireWorkspaceOwnership({workspaceRoot,configFile,port:23457,readIdentity:async pid=>pid===process.pid?'SYSTEM_VERIFICATION_ONLY-own-start':undefined}),{status:409});
  await owner.release();assert.equal(JSON.parse(await readFile(owner.ownerFile,'utf8')).instanceId,'foreign-nonce');
});

test('new-prefix manifests require complete same-environment numerical/native evidence and actual source hashes',async t=>{
  const {spawnSync}=await import('node:child_process');const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-motion-evidence-'));t.after(()=>rm(root,{recursive:true,force:true}));
  const script=`import importlib.util,json,hashlib,pathlib,copy,sys
spec=importlib.util.spec_from_file_location('action',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=pathlib.Path(sys.argv[2]).resolve();prefix=root/'SYSTEM_VERIFICATION_ONLY-prefix';site=prefix/'lib/python3.12/site-packages';directory=root/'receipts';directory.mkdir()
modules={}
for name in sorted(m.REQUIRED):
 file=site/(name.replace('.','/')+('.so' if name.startswith('curobo.curobolib.') else '/__init__.py'));file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(('SYSTEM_VERIFICATION_ONLY '+name).encode());st=file.stat();modules[name]=dict(path=str(file),bytes=st.st_size,device=st.st_dev,inode=st.st_ino,mtimeNs=st.st_mtime_ns,ctimeNs=st.st_ctime_ns,sha256=m.digest(file))
url=site/'nvidia_curobo-fixture.dist-info/direct_url.json';url.parent.mkdir();url.write_text(json.dumps({'vcs_info':{'commit_id':m.COMMIT}}))
identity=dict(prefix=str(prefix),executable=str(prefix/'bin/python'),python='3.12 SYSTEM_VERIFICATION_ONLY',SOABI='fixture',distributions={'isaacsim':'6.0.0.1','torch':'2.11.0+cu128','numpy':'2.3.1','warp-lang':None},torchABI={'fixture':True},torchCUDA='12.8',modules=modules,mappedNativeLibraries={row['path']:row for name,row in modules.items() if name.startswith('curobo.curobolib.')},actualProcessGpuDevices=[{'uuid':'GPU-fixture'}],contentHashesComputed=True)
q=dict(prefix=str(prefix),device={'index':0,'uuid':'GPU-fixture'},probeSource='# SYSTEM_VERIFICATION_ONLY fixture source')
reports={}
for phase in ('numerical','native'):
 names=m.NUMERICAL|({'isaac_same_process_startup','native_franka_actual_q_fk_and_tcp_calibration'} if phase=='native' else set())
 reports[phase]=dict(status='passed',phase=phase,upstreamCommit=m.COMMIT,probeSourceSha256=hashlib.sha256(q['probeSource'].encode()).hexdigest(),assignedGpu={'physicalIndex':0,'uuid':'GPU-fixture'},checks=[{'name':name,'status':'passed'} for name in names],identity=copy.deepcopy(identity),identityCapturedBeforeSdkShutdown=phase=='native')
def write(values):
 for phase,report in values.items():(directory/(phase+'-receipt.json')).write_text(json.dumps(report))
write(reports);valid=m.build_manifest(q,directory);assert valid['verification']['actualLoadedIdentity']['prefix']==str(prefix);assert len(valid['sourceFiles'])>=3
for issue in ('missing_module','prefix','commit','device','partial_check','phase_change','binary_hash'):
 bad=copy.deepcopy(reports)
 if issue=='missing_module':bad['native']['identity']['modules'].pop('warp')
 if issue=='prefix':bad['native']['identity']['prefix']='/wrong-prefix'
 if issue=='commit':bad['native']['upstreamCommit']='bad'
 if issue=='device':bad['native']['identity']['actualProcessGpuDevices']=[{'uuid':'other'}]
 if issue=='partial_check':bad['numerical']['checks'].pop()
 if issue=='phase_change':bad['native']['identity']['SOABI']='changed'
 if issue=='binary_hash':next(iter(bad['native']['identity']['mappedNativeLibraries'].values()))['sha256']='0'*64
 write(bad)
 try:m.build_manifest(q,directory);raise AssertionError(issue+' accepted')
 except (ValueError,KeyError):pass
write(reports);url.write_text(json.dumps({'vcs_info':{'commit_id':'wrong-actual-installed-commit'}}))
try:m.build_manifest(q,directory);raise AssertionError('wrong installed commit accepted')
except ValueError:pass
print(json.dumps({'CPUFixtureOnly':True,'SDKExecuted':False,'rejections':8}))`;
  const result=spawnSync('python3',['-B','-c',script,fileURLToPath(new URL('../setup/gpu-action.py',import.meta.url)),root],{encoding:'utf8'});assert.equal(result.status,0,result.stderr);assert.match(result.stdout,/"rejections": 8/);
});

test('workspace preparation produces a complete new launchable source tree and invokes only the isolated npm prefix',async t=>{
  const calls=[];const f=await fixture(t,{run:async(executable,args)=>{calls.push({executable,args});return {code:0,stdout:'SYSTEM_VERIFICATION_ONLY npm fixture',stderr:''};}});
  const state=await f.setup.inspect(),destination=path.join(f.owner.workspaceRoot,'new-studio');
  await f.setup.install({requestId:'complete_deployment',targetHash:state.configuration.effectiveHash,items:[{recipeId:'workspace-dependencies',options:{destination},acceptedLicenses:[]}]});
  let receipt;for(let n=0;n<400;n++){receipt=await f.setup.installation('complete_deployment');if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}
  assert.equal(receipt.status,'completed',receipt.error);assert.equal(calls.length,1);assert.equal(calls[0].executable,process.execPath);assert.ok(calls[0].args.includes('ci'));assert.equal(calls[0].args[calls[0].args.indexOf('--prefix')+1],destination);
  for(const file of ['src/server.mjs','runtime/jobs.py','web/index.html','skills/scene-generation/SKILL.md','setup/recipes.json','SceneAgent.command','package-lock.json','tests/probe_motion_backend.py'])assert.ok((await stat(path.join(destination,file))).isFile(),file);
  for(const file of ['studio.config.json','.state','projects','node_modules'])await assert.rejects(stat(path.join(destination,file)),{code:'ENOENT'});
});

test('SSH transport loss keeps every side-effecting recipe unknown while an actual remote command failure stays failed',async t=>{
  const {configHash}=await import('../src/config.mjs');const f=await fixture(t);await f.setup.close();
  const value=Object.freeze({...f.configuration.value,runtimeMode:'ssh',remoteHost:'SYSTEM_VERIFICATION_ONLY-host',gpuDevices:[{index:0,uuid:'GPU-00000000-0000-0000-0000-000000000001'}]});
  const configuration={...f.configuration,value,effectiveHash:configHash(value)};
  for(const kind of ['command','assets','gpu'])for(const code of [255,1,'timeout']){
    let count=0;const recipe=kind==='command'?{id:'fixture-ssh',host:'compute',effect:'prepare',licenseIds:[],commands:[{executable:'/usr/bin/true',args:[]}]}:null;
    const setup=await createSetup({configuration,owner:f.owner,...(recipe?{recipes:[recipe]}:{}),run:async()=>{count++;const failedAt=kind==='gpu'?2:3;return {code:count===failedAt&&code!=='timeout'?code:0,...(count===failedAt&&code==='timeout'?{error:'SYSTEM_VERIFICATION_ONLY local SSH timeout'}:{}),stdout:'{}',stderr:count===failedAt?'SYSTEM_VERIFICATION_ONLY '+(code===255?'connection lost':'remote command failed'):'',exitConfirmed:true};}});
    try{
      const state=await setup.inspect(),id='ssh_'+kind+'_'+code,selected=kind==='assets'?state.recipes.find(row=>row.id==='install-artvip'):kind==='gpu'?state.recipes.find(row=>row.id==='motion-backend'):recipe;
      const destination=path.join(value.remoteRoot,'assets/staging',id),options={destination};let acceptedLicenses=selected.licenseIds||[];
      if(kind==='assets'){const pack=selected.packages[0];options.packageIds=[pack.id];acceptedLicenses=[...acceptedLicenses,...pack.licenseIds];}
      if(kind==='gpu'){options.prefix=path.join(value.remoteRoot,'envs',id);options.peakMiB='1024';}
      await setup.install({requestId:id,targetHash:state.configuration.effectiveHash,items:[{recipeId:selected.id,options,acceptedLicenses}]});let receipt;
      for(let n=0;n<100;n++){receipt=await setup.installation(id);if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}
      assert.equal(receipt.status,code===1?'failed':'unknown',kind+' '+code+' '+receipt.error);assert.equal((await setup.cancel(id)).status,receipt.status);
    }finally{await setup.close();}
  }
});

test('a draft Isaac prefix bootstraps metadata and directory creation with explicit basePython before the new interpreter exists',async t=>{
  const calls=[];const f=await fixture(t,{run:async(executable,args)=>{calls.push({executable,args});const command=args.at(-1);if(calls.length<=2&&command.includes('new-sdk/bin/python'))return {code:1,stdout:'',stderr:'SYSTEM_VERIFICATION_ONLY target interpreter does not exist'};return {code:0,stdout:JSON.stringify({python:{version:'3.12.3'},packages:{isaacsim:'6.0.0.1'}}),stderr:'',exitConfirmed:true};}});
  const before=await f.setup.inspect(),destination=path.join(f.configuration.value.remoteRoot,'envs/new-sdk'),basePython='/SYSTEM_VERIFICATION_ONLY/base/bin/python3.12';
  const draft=await f.setup.saveConfig({action:'save',baseHash:before.configuration.editHash,value:{...before.configuration.effective,runtimeMode:'ssh',remoteHost:'fixture-host',remotePython:destination+'/bin/python'}});
  await f.setup.install({requestId:'bootstrap_new_prefix',configuration:'draft',targetHash:draft.draftHash,items:[{recipeId:'isaac6-runtime',options:{destination,basePython},acceptedLicenses:['omniverse-eula:6.0.0.1']}]});let receipt;
  for(let n=0;n<100;n++){receipt=await f.setup.installation('bootstrap_new_prefix');if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}
  assert.equal(receipt.status,'completed',receipt.error);assert.ok(calls.slice(0,2).every(row=>row.executable==='ssh'&&row.args.at(-1).includes(basePython)));assert.equal(f.configuration.value.remotePython,process.execPath);
});

test('a proven local spawn failure cannot masquerade as an unknown remote installer',async t=>{
  const {runCommand}=await import('../src/setup.mjs');await assert.rejects(runCommand('sceneagent-SYSTEM_VERIFICATION_ONLY-command-that-does-not-exist',[]),error=>error.localProcessNotStarted===true);
  const {configHash}=await import('../src/config.mjs'),f=await fixture(t);await f.setup.close();const value={...f.configuration.value,runtimeMode:'ssh',remoteHost:'fixture-host'},configuration={...f.configuration,value,effectiveHash:configHash(value)};
  const setup=await createSetup({configuration,owner:f.owner,recipes:[{id:'fixture-nospawn',host:'compute',effect:'prepare',licenseIds:[],commands:[{executable:'/usr/bin/true',args:[]}]}],run:async()=>{throw Object.assign(new Error('SYSTEM_VERIFICATION_ONLY local executable missing'),{localProcessNotStarted:true});}});
  try{await setup.install({requestId:'known_nospawn',targetHash:configuration.effectiveHash,items:[{recipeId:'fixture-nospawn',options:{destination:path.join(value.remoteRoot,'prefix')}}]});let receipt;for(let n=0;n<50;n++){receipt=await setup.installation('known_nospawn');if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}assert.equal(receipt.status,'failed');}
  finally{await setup.close();}
});

test('catalog publication claims its real target even when receipt destinations differ',async t=>{
  let active=0,maximum=0,release;const gate=new Promise(resolve=>{release=resolve;});
  const f=await fixture(t,{recipes:[{id:'register-fixture',host:'compute',effect:'publish',action:'register',licenseIds:[]}],run:async()=>({code:0,stdout:'{}',stderr:''}),registerAssets:async()=>{active++;maximum=Math.max(maximum,active);await gate;active--;return {registered:true};}});
  const hash=(await f.setup.inspect()).configuration.effectiveHash,input=id=>({requestId:'catalog_'+id,targetHash:hash,items:[{recipeId:'register-fixture',options:{destination:path.join(f.configuration.value.remoteRoot,'receipts',id)}}]});
  let rejected=false;try{await f.setup.install(input('a'));for(let n=0;n<50&&!active;n++)await new Promise(resolve=>setTimeout(resolve,5));try{await f.setup.install(input('b'));}catch(error){rejected=error.status===409;}if(!rejected)for(let n=0;n<50&&maximum<2;n++)await new Promise(resolve=>setTimeout(resolve,5));}
  finally{release();}
  assert.equal(rejected,true);assert.equal(maximum,1);
});

test('real CPU publishers serialize read merge index and rollback across processes on one catalog',async t=>{
  const vm=await import('node:vm'),{spawn}=await import('node:child_process');
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8'),ROOT=fileURLToPath(new URL('..',import.meta.url));
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-catalog-process-'));t.after(()=>rm(root,{recursive:true,force:true}));
  for(const locked of [false,true]){
    const data=path.join(root,locked?'cooperating-lock':'SYSTEM_VERIFICATION_ONLY-disabled-control');await mkdir(data);
    const inputs=[];for(const id of ['a','b']){const packageRoot='staging/fixture-'+id,entry=path.join(data,'assets',packageRoot,'model.usda');await mkdir(path.dirname(entry),{recursive:true});await writeFile(entry,'#usda 1.0\ndef Xform "SYSTEM_VERIFICATION_ONLY" {}\n');
      const catalogFile=path.join(data,id+'.json');await writeFile(catalogFile,JSON.stringify({schema:'sceneagent-asset-collections-v1',collections:[{id:'fixture',revision:'v1',libraryTier:'preferred'}],assets:[{assetId:'collection:fixture:'+id,collection:'fixture',sourceRoot:'current_assets',packageRoot,entry:'model.usda',dependencyFiles:[],format:'native_files',kind:'model',name:'SYSTEM_VERIFICATION_ONLY '+id}]}));inputs.push(catalogFile);}
    const target={runtimeMode:'local',remoteRoot:data,remotePython:'python3',legacyRoot:null};
    const context={ROOT,config:target,Buffer,readFile,path,quote,setTimeout,clearTimeout,
      connectCompute:(_target,command)=>spawn('sh',['-c',command],{stdio:['pipe','pipe','pipe']})};
    let publisher=source.slice(source.indexOf('export async function registerAssetCatalog('),source.indexOf('const bootstrap =')).replaceAll('export ','')
      .replace("old=dest.read_bytes() if dest.exists() else None","old=dest.read_bytes() if dest.exists() else None;__import__('time').sleep(.25)");
    if(!locked)publisher=publisher.replace('fcntl.flock(lock,fcntl.LOCK_EX)','pass # SYSTEM_VERIFICATION_ONLY disabled negative control').replace('fcntl.flock(lock,fcntl.LOCK_UN)','pass');
    vm.runInNewContext(source.slice(source.indexOf('function singleJsonRemote('),source.indexOf('const privateNetworks'))+publisher+'\nthis.publish=registerAssetCatalog;',context);
    await Promise.all(inputs.map(catalogFile=>context.publish({catalogFile},{target})));
    const catalog=JSON.parse(await readFile(path.join(data,'assets/collections/catalog.json'),'utf8'));
    assert.equal(catalog.assets.length,locked?2:1,locked?'both actual publications must survive':'negative control must reproduce the lost merge');
  }
});

test('a Windows workbench sends POSIX new-interpreter argv to the SSH compute host',async()=>{
  const vm=await import('node:vm'),source=await readFile(new URL('../src/setup.mjs',import.meta.url),'utf8');const start=source.indexOf('async function executeRecipe('),end=source.indexOf('async function stageAssets(',start),commands=[];
  const context={path:path.win32,process:{platform:'win32',execPath:'C:\\Node\\node.exe'},metadataProbe:'SYSTEM_VERIFICATION_ONLY metadata',pythonCommand:async()=>({code:0,stdout:'{}'}),quote:value=>"'"+value+"'",sshResult:async(host,command)=>{commands.push({host,command});return {code:0,stdout:'',stderr:'',exitConfirmed:true};}};
  vm.runInNewContext(source.slice(start,end)+'\nthis.execute=executeRecipe;',context);
  await context.execute({recipe:{id:'fixture-windows-ssh',host:'compute',effect:'prepare',commands:[{executable:'{newPython}',args:['-m','pip','install','SYSTEM_VERIFICATION_ONLY']} ]},options:{},destination:'/srv/studio-data/env-new'},{runtimeMode:'ssh',remoteHost:'fixture-linux',remoteRoot:'/srv/studio-data',remotePython:'/usr/bin/python3.12'},{signal:new AbortController().signal,event(){},root:'C:\\Studio\\workspace'});
  assert.equal(commands.length,1);assert.equal(commands[0].host,'fixture-linux');assert.ok(commands[0].command.startsWith("'/srv/studio-data/env-new/bin/python' "),commands[0].command);
});

test('path containment explicitly keeps Windows local semantics and case-sensitive POSIX compute semantics',async()=>{
  const vm=await import('node:vm'),source=await readFile(new URL('../src/setup.mjs',import.meta.url),'utf8');const context={path:path.win32};const start=source.indexOf('const inside ='),end=source.indexOf('\nasync function jsonFile',start);vm.runInNewContext(source.slice(start,end)+'\nthis.inside=inside;',context);
  assert.equal(context.inside('/data/Root','/data/root/local'),true,'local Windows paths are case-insensitive');
  assert.equal(context.inside('/data/Root','/data/root/remote',path.posix),false);
  assert.equal(context.inside('/data/Root','/data/Root/folder\\..\\..\\literal',path.posix),true,'Linux backslashes are literal filename characters');
  assert.equal(context.inside('/data/Root','/data/RootSibling',path.posix),false);
});

test('preparing a new target protects only files on its actual host while preserving same-host protection',async t=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'sceneagent-host-scope-')),file=path.join(root,'config.json');
  await writeFile(file,JSON.stringify({workspaceRoot:path.join(root,'workspace'),runtimeMode:'ssh',remoteHost:'fixture-host-a',remoteRoot:'/SYSTEM_VERIFICATION_ONLY/data',remotePython:'/SYSTEM_VERIFICATION_ONLY/data/env/bin/python',gpuDevices:[]}));
  const configuration=loadConfiguration({file}),owner=await acquireWorkspaceOwnership({workspaceRoot:configuration.value.workspaceRoot,configFile:file,port:8766});
  const setup=await createSetup({configuration,owner,recipes:[{id:'fixture-compute',host:'compute',effect:'prepare',licenseIds:[],commands:[]},{id:'fixture-studio',host:'studio',effect:'prepare',licenseIds:[],commands:[]}],run:async()=>({code:0,stdout:'{}',stderr:'',exitConfirmed:true})});
  t.after(async()=>{await setup.close();await owner.release();await rm(root,{recursive:true,force:true});});
  const state=await setup.inspect(),draft=await setup.saveConfig({action:'save',baseHash:state.configuration.editHash,value:{...state.configuration.effective,remoteHost:'fixture-host-b'}});
  const input={requestId:'different_host_same_path',configuration:'draft',targetHash:draft.draftHash,items:[{recipeId:'fixture-compute',options:{destination:'/SYSTEM_VERIFICATION_ONLY/data/env'}}]};
  await setup.install(input);let receipt;for(let n=0;n<100;n++){receipt=await setup.installation(input.requestId);if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}assert.equal(receipt.status,'completed',receipt.error);
  await assert.rejects(setup.install({...input,requestId:'same_host_same_path',configuration:'effective',targetHash:configuration.effectiveHash}),{status:409});
  const local=path.join(owner.workspaceRoot,'new-local-prefix'),remoteSameString=Object.freeze({...configuration.value,remotePython:path.join(local,'bin/python')});
  const localSetup=await createSetup({configuration:{...configuration,value:remoteSameString,effectiveHash:'SYSTEM_VERIFICATION_ONLY-local-scope'},owner,recipes:[{id:'fixture-studio',host:'studio',effect:'prepare',licenseIds:[],commands:[]}]});
  try{await localSetup.install({requestId:'studio_vs_remote',targetHash:'SYSTEM_VERIFICATION_ONLY-local-scope',items:[{recipeId:'fixture-studio',options:{destination:local}}]});for(let n=0;n<100;n++){receipt=await localSetup.installation('studio_vs_remote');if(receipt.status!=='running')break;await new Promise(resolve=>setTimeout(resolve,5));}assert.equal(receipt.status,'completed',receipt.error);}finally{await localSetup.close();}
});
