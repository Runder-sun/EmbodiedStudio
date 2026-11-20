import { spawn, spawnSync } from 'node:child_process';
import { readFileSync, realpathSync, createReadStream } from 'node:fs';
import { mkdir, readFile, writeFile, rename, rm, lstat, statfs, open, readdir, cp } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { ROOT, configHash, normalizeConfig, readMotionDependencies, resolveCodexCommand, validId, validSshHost, quote } from './config.mjs';

const fail = (message, status = 400, code) => Object.assign(new Error(message), { status, ...(code ? {code} : {}) });
const canonical = value => Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
const sameComputeHost = (a,b) => a.runtimeMode===b.runtimeMode&&(a.runtimeMode!=='ssh'||a.remoteHost===b.remoteHost);
const sameTarget = (a,b) => ['runtimeMode','remoteHost','remoteRoot'].every(key => a[key] === b[key]);
const inside = (root, file, paths=path) => { const relative=paths.relative(root,file);return relative===''||!relative.startsWith('..'+paths.sep)&&relative!=='..'&&!paths.isAbsolute(relative); };
async function jsonFile(file, value) {
  await mkdir(path.dirname(file),{recursive:true});
  const temporary=file+'.tmp-'+randomUUID();
  await writeFile(temporary,JSON.stringify(value,null,2)+'\n',{flag:'wx',mode:0o600});
  await rename(temporary,file);
}
async function readJson(file) { try { return JSON.parse(await readFile(file,'utf8')); } catch(error) { if(error.code==='ENOENT')return null;throw error; } }

export async function processIdentity(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return undefined;
  if (process.platform === 'linux') {
    try {
      const fields=(await readFile('/proc/'+pid+'/stat','utf8')).split(') ').at(-1).split(' ');
      if (fields[0]==='Z') return null;
      return 'linux:'+String(await readFile('/proc/sys/kernel/random/boot_id','utf8')).trim()+':'+fields[19];
    } catch(error) { return error.code==='ENOENT' ? null : undefined; }
  }
  if (process.platform === 'win32') {
    const script=`$p=Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}'; if($p){$p | Select-Object CreationDate,ExecutablePath | ConvertTo-Json -Compress}`;
    const result=spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',script],{encoding:'utf8',timeout:5000,windowsHide:true});
    if(result.error || result.status!==0)return undefined;
    if(!result.stdout.trim())return null;
    try { const value=JSON.parse(result.stdout);return value.CreationDate&&value.ExecutablePath?'windows:'+value.CreationDate+':'+value.ExecutablePath:undefined; } catch { return undefined; }
  }
  const result=spawnSync('ps',['-p',String(pid),'-o','lstart=','-o','comm='],{encoding:'utf8',timeout:5000});
  if(result.status===1&&!result.stdout.trim())return null;
  return result.error||result.status!==0||!result.stdout.trim()?undefined:process.platform+':'+result.stdout.trim();
}

export async function acquireWorkspaceOwnership({workspaceRoot,configFile,port,readIdentity=processIdentity}) {
  const root=path.resolve(workspaceRoot),state=path.join(root,'.state'),ownerFile=path.join(state,'owner.json'),guard=path.join(state,'owner.guard');
  await mkdir(state,{recursive:true});
  const ownStart=await readIdentity(process.pid);
  if(!ownStart)throw fail('无法确认当前进程的启动身份；工作区保持只读。',409,'owner_unknown');
  const instanceId=randomUUID();
  const withGuard=async action=>{
    try { await mkdir(guard); } catch(error) { if(error.code==='EEXIST')throw fail('工作区所有权正在处理或尚未确认，不能抢占。',409,'owner_unknown');throw error; }
    const guardId=randomUUID();
    await writeFile(path.join(guard,'claim.json'),JSON.stringify({pid:process.pid,processStart:ownStart,instanceId:guardId}),{flag:'wx',mode:0o600});
    try { return await action(); }
    finally {
      const current=await readJson(path.join(guard,'claim.json')).catch(()=>null);
      if(current?.instanceId===guardId)await rm(guard,{recursive:true});
    }
  };
  const owner={pid:process.pid,processStart:ownStart,instanceId,workspaceRoot:root,configFile:path.resolve(configFile),port};
  await withGuard(async()=>{
    let previous;
    try { previous=await readJson(ownerFile); } catch { throw fail('工作区所有权记录损坏；不能推测无人使用。',409,'owner_unknown'); }
    if(previous){
      if(!Number.isSafeInteger(previous.pid)||!previous.processStart||!previous.instanceId||previous.workspaceRoot!==root)throw fail('工作区所有权未确认。',409,'owner_unknown');
      const identity=await readIdentity(previous.pid);
      if(identity===undefined)throw fail('无法确认旧进程是否仍在；工作区保持只读。',409,'owner_unknown');
      if(identity===previous.processStart)throw fail('已有 Studio 或安装进程拥有此工作区；HTTP不可达也不能抢占。',409,'workspace_owned');
      const again=await readJson(ownerFile);
      if(again?.instanceId!==previous.instanceId)throw fail('工作区所有权已变化，请重新检查。',409,'owner_unknown');
      await rename(ownerFile,path.join(state,'owner-stale-'+configHash(previous.instanceId).slice(0,16)+'.json'));
    }
    await jsonFile(ownerFile,owner);
  });
  let released=false;
  return { ...owner, ownerFile,
    async assertOwned(){const current=await readJson(ownerFile);if(released||current?.instanceId!==instanceId||current?.processStart!==ownStart)throw fail('当前进程不再拥有工作区。',409,'owner_unknown');},
    async release(){
      if(released)return;
      try { await withGuard(async()=>{const current=await readJson(ownerFile);if(current?.instanceId===instanceId&&current.processStart===ownStart)await rm(ownerFile);}); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      released=true;
    }
  };
}

export async function bindWorkspaceTarget(owner,target,{confirmExisting=false}={}) {
  await owner.assertOwned();
  const file=path.join(owner.workspaceRoot,'.state','workspace-target.json');
  const binding={runtimeMode:target.runtimeMode,remoteHost:target.remoteHost,remoteRoot:target.remoteRoot};
  const previous=await readJson(file);
  if(previous){if(!sameTarget(previous,binding))throw fail('此工作区已属于另一计算主机或数据根，请使用独立工作区。',409,'target_bound');return previous;}
  let entries=[];try{entries=await readdir(path.join(owner.workspaceRoot,'projects'));}catch(error){if(error.code!=='ENOENT')throw error;}
  if(entries.length&&!confirmExisting)throw fail('旧工作区计算目标尚未确认，请先在设置中明确启用当前配置。',409,'target_unconfirmed');
  await owner.assertOwned();await jsonFile(file,binding);return binding;
}

export function tunnelArguments({host,localPort,remotePort}) {
  if(!validSshHost(host)||![localPort,remotePort].every(value=>Number.isInteger(value)&&value>0&&value<=65535))throw fail('SSH主机别名或转发端口无效。');
  return ['-N','-T','-a','-o','ForwardAgent=no','-o','ExitOnForwardFailure=yes','-o','StrictHostKeyChecking=yes','-o','ServerAliveInterval=15',
    '-L',`127.0.0.1:${localPort}:127.0.0.1:${remotePort}`,host];
}

export function runCommand(executable,args,{cwd,env,signal,onOutput,input,timeoutMs=30000,maxBytes=262144}={}) {
  return new Promise((resolve,reject)=>{
    const child=spawn(executable,args,{cwd,env:{...process.env,...env},stdio:[input===undefined?'ignore':'pipe','pipe','pipe'],shell:false,windowsHide:true,detached:process.platform!=='win32'});
    let stdout='',stderr='',settled=false,stopReason,killTimer,cleanupTimer;
    const cleanup=()=>{clearTimeout(timer);clearTimeout(killTimer);clearTimeout(cleanupTimer);signal?.removeEventListener('abort',abort);};
    const groupAlive=()=>{if(process.platform==='win32')return null;try{process.kill(-child.pid,0);return true;}catch(error){return error.code==='ESRCH'?false:null;}};
    const finish=(code,exitSignal,exitConfirmed)=>{if(settled)return;settled=true;cleanup();if(!exitConfirmed){child.stdout.destroy();child.stderr.destroy();child.unref();}
      resolve({code,signal:exitSignal,stdout,stderr,...(stopReason?{error:stopReason.message}:{}),exitConfirmed});};
    const kill=hard=>{
      if(!child.pid)return;
      if(process.platform==='win32'){
        const killer=spawn('taskkill.exe',['/PID',String(child.pid),'/T',...(hard?['/F']:[])],{stdio:'ignore',shell:false,windowsHide:true});killer.on('error',()=>{});
      }else try{process.kill(-child.pid,hard?'SIGKILL':'SIGTERM');}catch(error){if(error.code!=='ESRCH')stderr+=(stderr?'\n':'')+error.message;}
    };
    const stop=reason=>{if(settled||stopReason)return;stopReason=reason;kill(false);killTimer=setTimeout(()=>{kill(true);cleanupTimer=setTimeout(()=>finish(null,null,false),1500);},1500);};
    const abort=()=>stop(signal.reason||new Error('已请求取消安装。'));
    const timer=setTimeout(()=>stop(new Error('命令执行超时。')),timeoutMs);
    const output=(kind,chunk)=>{const value=chunk.toString();onOutput?.(kind,value);if(kind==='stdout')stdout=(stdout+value).slice(-maxBytes);else stderr=(stderr+value).slice(-maxBytes);};
    child.stdout.on('data',chunk=>output('stdout',chunk));child.stderr.on('data',chunk=>output('stderr',chunk));
    child.once('error',error=>{if(settled)return;settled=true;cleanup();if(!child.pid)error.localProcessNotStarted=true;reject(error);});
    child.once('close',(code,exitSignal)=>{
      const alive=groupAlive();
      if(alive===false)return finish(code,exitSignal,true);
      if(process.platform==='win32')return finish(code,exitSignal,!stopReason);
      if(!stopReason)stopReason=new Error('命令留下运行中的子进程，正在清理本次进程组。');
      clearTimeout(killTimer);kill(true);
      const check=()=>{const current=groupAlive();if(current===false)finish(code,exitSignal,true);else cleanupTimer=setTimeout(()=>finish(code,exitSignal,false),500);};
      cleanupTimer=setTimeout(check,50);
    });
    if(input!==undefined){child.stdin.on('error',error=>stop(error));child.stdin.end(input);}
    signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();
  });
}

function npmCommand(){
  const candidates=[process.env.npm_execpath,path.join(path.dirname(process.execPath),'node_modules/npm/bin/npm-cli.js'),path.join(path.dirname(process.execPath),'../lib/node_modules/npm/bin/npm-cli.js')];
  for(const directory of [path.dirname(process.execPath),...(process.env.PATH||'').split(path.delimiter)])for(const name of process.platform==='win32'?['npm.cmd','npm.exe']:['npm']){
    try{const binary=realpathSync(path.join(directory,name));candidates.push(binary,path.join(path.dirname(binary),'node_modules/npm/bin/npm-cli.js'));}catch{}
  }
  const cli=candidates.find(file=>{try{return path.basename(file)==='npm-cli.js'&&JSON.parse(readFileSync(path.join(path.dirname(file),'../package.json'),'utf8')).name==='npm';}catch{return false;}});
  if(!cli)throw fail('尚未找到安装好的 npm-cli.js，请先安装官方 Node/npm。',422);
  return [process.execPath,cli];
}

const metadataProbe=`import sys,json,os,shutil,platform,subprocess,importlib.metadata,pathlib
q=json.loads(sys.argv[1]);p=pathlib.Path(q['root'])
while not p.exists() and p!=p.parent:p=p.parent
packages={}
for name in ('isaacsim','torch','numpy','nvidia-curobo','trimesh','yourdfpy'):
 try:packages[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError:packages[name]=None
gpus=[];gpuError=None
try:
 r=subprocess.run(['nvidia-smi','--query-gpu=index,uuid,name,memory.total,memory.free,driver_version','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10,check=True)
 for row in r.stdout.splitlines():
  a=[x.strip() for x in row.split(',')];gpus.append(dict(index=int(a[0]),uuid=a[1],name=a[2],memoryTotalMiB=int(a[3]),memoryFreeMiB=int(a[4]),driverVersion=a[5]))
except Exception as e:gpuError=type(e).__name__+': '+str(e)
libraries=[]
for base in (pathlib.Path(q['root']),pathlib.Path(q.get('legacyRoot') or q['root'])):
 catalog=base/'assets'/'collections'/'catalog.json'
 if catalog.is_file():
  try:
   if catalog.stat().st_size>32*1024*1024:raise ValueError('catalog too large for metadata probe')
   doc=json.loads(catalog.read_text());libraries.append(dict(path=str(catalog),schema=doc.get('schema'),collections=len(doc.get('collections',[])),assets=len(doc.get('assets',[])),registeredSource=True,qualityVerified=False))
  except Exception as e:libraries.append(dict(path=str(catalog),error=str(e)))
print(json.dumps(dict(platform=sys.platform,arch=platform.machine(),python=dict(executable=sys.executable,version=platform.python_version()),packages=packages,libraries=libraries,gpus=gpus,gpuError=gpuError,gpuLockRootSuggestion=str(pathlib.Path(os.environ.get('XDG_CACHE_HOME',str(pathlib.Path.home()/'.cache')))/'sceneagent'/'gpu-locks'),isolation=dict(bwrap=bool(shutil.which('bwrap'))),disk=dict(path=str(p),freeBytes=shutil.disk_usage(p).free,writeAccess='reported_access' if os.access(p,os.W_OK) else 'not_reported'))))`;

function sshArgs(host,command){if(!validSshHost(host))throw fail('SSH主机别名无效。');return ['-T','-a','-o','ForwardAgent=no','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12',host,command];}
async function sshResult(host,command,run,options){
  let result;try{result=await run('ssh',sshArgs(host,command),options);}catch(error){if(!error.localProcessNotStarted)error.remoteExitUnconfirmed=true;throw error;}
  if(result.code===255||result.code===null||result.error||result.exitConfirmed===false)throw Object.assign(new Error(result.stderr||result.error||'SSH连接终态未知，远端安装进程退出未确认。'),{remoteExitUnconfirmed:true});
  return result;
}

async function pythonCommand(target,script,args,run,options){
  if(!target.remotePython)throw fail('尚未选择执行解释器。',422,'python_missing');
  const bootstrap="import sys,json;p=json.load(sys.stdin);sys.argv=['sceneagent-setup',*p['args']];exec(compile(p['source'],'<sceneagent-setup>','exec'))";
  const opts={...options,input:JSON.stringify({source:script,args})};
  return target.runtimeMode==='ssh'?sshResult(target.remoteHost,[quote(target.remotePython),'-B','-c',quote(bootstrap)].join(' '),run,opts)
    :run(target.remotePython,['-B','-c',bootstrap],opts);
}

const safeOptions=new Set(['destination','prefix','version','basePython','packageIds','catalogFile','robotPackage','peakMiB']);
function validateOptions(value={}){if(!value||typeof value!=='object'||Array.isArray(value))throw fail('安装选项必须是对象。');for(const key of Object.keys(value))if(!safeOptions.has(key))throw fail('不支持安装选项：'+key);return value;}

export async function createSetup({configuration,owner,stateRoot=owner?.workspaceRoot,resourceBusy=()=>[],run=runCommand,recipes,registerAssets}={}) {
  if(!configuration||!owner)throw new Error('设置模块需要配置和工作区所有权。');
  registerAssets ??= async ({options,target,signal}) => (await import('./remote.mjs')).registerAssetCatalog(options,{target,signal});
  const effective=configuration.value,root=owner.workspaceRoot,directory=path.join(stateRoot,'.state','setup'),draftFile=path.join(directory,'config-draft.json');
  recipes ??= JSON.parse(await readFile(path.join(ROOT,'setup','recipes.json'),'utf8'));
  recipes=await Promise.all(recipes.map(async recipe=>{
    if(!recipe.manifest)return {...recipe,available:recipe.available!==false};
    const manifest=JSON.parse(await readFile(path.join(ROOT,'setup','asset-manifests',recipe.manifest),'utf8'));
    return {...recipe,available:true,version:manifest.revision,packages:manifest.packages.map(({files,asset,assets,...row})=>row)};
  }));
  const operations=new Map(),claims=new Map(),openingInstallations=new Map();let closed=false,lastProbe=null,configurationEdit=Promise.resolve();
  try{for(const name of await readdir(directory)){if(!validId(name))continue;const receipt=await readJson(path.join(directory,name,'receipt.json'));if(receipt&&['running','cancel_requested','unknown'].includes(receipt.status))claims.set('unresolved:'+name,{key:'unresolved:'+name,effect:receipt.effect,host:receipt.target?.host,runtimeMode:receipt.target?.runtimeMode,destinations:receipt.destinations,unresolved:true});}}catch(error){if(error.code!=='ENOENT')throw error;}
  const draft=async()=>{
    const saved=await readJson(draftFile);
    return saved?.hash===configuration.effectiveHash && saved.baseFileHash!==configuration.fileHash ? null : saved;
  };
  const inspect=async()=>{
    let raw=null;try{raw=await readFile(configuration.file,'utf8');}catch(error){if(error.code!=='ENOENT')throw error;}
    const editing=await draft(),fileHash=raw===null?null:configHash(raw);
    return {schema:'sceneagent-setup-v1',ownership:{status:'owned',pid:owner.pid,instanceId:owner.instanceId},
      configuration:{configured:configuration.configured,file:configuration.file,fileHash,effectiveHash:configuration.effectiveHash,effective,
        draftHash:editing?.hash??null,draft:editing?.value??null,editHash:editing?.hash??fileHash,diagnostics:configuration.diagnostics,
        restartRequired:fileHash!==configuration.fileHash},
      platform:{platform:process.platform,arch:process.arch},busy:{resources:[...claims.values()]},
      recipes:recipes.map(({commands,...recipe})=>recipe),lastProbe,
      installations:(await listReceipts()).slice(-20)};
  };
  async function listReceipts(){let names;try{names=await readdir(directory);}catch(error){if(error.code==='ENOENT')return [];throw error;}
    const rows=[];for(const name of names)if(validId(name)){const active=operations.has(name)||openingInstallations.has(name);const row=await readJson(path.join(directory,name,'receipt.json'));if(row)rows.push({requestId:row.requestId,status:row.status==='running'&&!active&&!operations.has(name)&&!openingInstallations.has(name)?'interrupted':row.status,effect:row.effect,startedAt:row.startedAt});}return rows;}
  async function selectedTarget(input){
    if(input.configuration==='draft'){const editing=await draft();if(!editing||editing.hash!==input.targetHash)throw fail('配置草稿已变化。',409,'config_changed');return normalizeConfig(editing.value,{file:configuration.file});}
    if(input.targetHash!==configuration.effectiveHash)throw fail('执行配置版本不匹配。',409,'config_changed');
    return effective;
  }
  const probe=async(input={})=>{
    if(!['workspace','compute','ssh'].includes(input.target||'workspace'))throw fail('检测目标无效。');
    for(const key of Object.keys(input))if(!['target','settings','ssh'].includes(key))throw fail('不支持检测参数：'+key);
    const target=input.settings?normalizeConfig(input.settings,{file:configuration.file}):effective;
    const result={schema:'sceneagent-setup-probe-v1',scope:'read-only',checkedAt:new Date().toISOString(),target:{runtimeMode:target.runtimeMode,host:target.remoteHost},
      workspace:{platform:process.platform,arch:process.arch,node:{status:'available',version:process.version}},compute:{runtimeVerified:false},recipes:recipes.map(({commands,...recipe})=>recipe)};
    try{const command=npmCommand();result.workspace.npm={status:'available',version:JSON.parse(readFileSync(path.join(path.dirname(command[1]),'../package.json'),'utf8')).version};}catch(error){result.workspace.npm={status:'unavailable',error:error.message};}
    if(input.target==='ssh'){
      const host=input.ssh?.host||target.remoteHost;
      const reply=await run('ssh',sshArgs(host,'uname -s'),{timeoutMs:15000});
      result.ssh={host,connected:reply.code===0,platform:reply.stdout.trim(),error:reply.code===0?null:reply.stderr||reply.error};
    }else if(input.target==='compute'){
      try{
        const reply=await pythonCommand(target,metadataProbe,[JSON.stringify({root:target.remoteRoot||root,legacyRoot:target.legacyRoot})],run,{timeoutMs:20000});
        if(reply.code!==0)throw new Error(reply.stderr||reply.error||'解释器检测未成功。');
        const measured=JSON.parse(reply.stdout);
        result.compute={...measured,runtimeVerified:false,formalExecution:{supportedPlatform:measured.platform==='linux',runtimeVerified:false,
          prerequisitesSatisfied:false,hardwareVerification:'not_executed',metadataPrerequisitesSatisfied:measured.platform==='linux'&&measured.python?.version?.startsWith('3.12.')&&measured.packages?.isaacsim==='6.0.0.1'&&measured.isolation?.bwrap===true&&measured.gpus?.some(gpu=>/RTX/i.test(gpu.name))===true,
          limitations:['检测元数据不代表SDK、机器人或任务已验证。']},
          gpuLockRootSuggestion:{path:target.gpuLockRoot||measured.gpuLockRootSuggestion,created:false,scope:'同主机且使用同一目录的协作实例；不控制其他用户程序。'}};
      }catch(error){result.compute={runtimeVerified:false,error:error.message,formalExecution:{supportedPlatform:false,runtimeVerified:false,prerequisitesSatisfied:false}};}
    }else{
      try{const [exe,...args]=resolveCodexCommand(target);const version=await run(exe,[...args,'--version'],{timeoutMs:10000});const auth=version.code===0?await run(exe,[...args,'login','status'],{timeoutMs:10000}):null;
        result.workspace.codex={status:version.code===0?'available':'unavailable',version:version.stdout.trim(),loggedIn:auth?.code===0,credentialContentsReturned:false};
      }catch(error){result.workspace.codex={status:'unavailable',error:error.message,loggedIn:false};}
    }
    lastProbe=result;return result;
  };
  const claim=(key,effect,target)=>{if(claims.has(key))throw fail('此安装目标正在处理。',409,'setup_busy');if(!['prepare','gpu_verify'].includes(effect)){const busy=resourceBusy(effect,target);if(busy.length)throw fail('受影响资源仍在使用：'+busy.join('、'),409,'setup_busy');}claims.set(key,{key,effect,target:configHash(target)});};
  function saveConfig(input){const work=configurationEdit.then(()=>writeConfig(input));configurationEdit=work.catch(()=>{});return work;}
  async function writeConfig(input){
    await owner.assertOwned();
    if(input.action==='save'){
      const current=await inspect();if(input.baseHash!==current.configuration.editHash)throw fail('配置编辑版本已变化。',409,'config_changed');
      let value={...input.value};
      if(value.gpuIndices!==undefined){if(!Array.isArray(value.gpuIndices)||new Set(value.gpuIndices).size!==value.gpuIndices.length||value.gpuIndices.some(index=>!Number.isInteger(index)||index<0))throw fail('请选择互不重复的显卡编号。');
        const indices=value.gpuIndices;delete value.gpuIndices;
        const devices=indices.length?(await probe({target:'compute',settings:value})).compute.gpus:[];
        value.gpuDevices=indices.map(index=>{const gpu=devices?.find(gpu=>gpu.index===index);if(!gpu)throw fail('检测未确认所选显卡：'+index,422);return {index:gpu.index,uuid:gpu.uuid};});}
      value=normalizeConfig(value,{file:configuration.file});const motion=value.motionDependenciesFile?readMotionDependencies(value.motionDependenciesFile):null;if(motion&&motion.executable!==value.remotePython)throw fail('运动依赖清单与执行解释器不一致。');const hash=motion?configHash({configuration:value,motionDependenciesSha256:motion.sha256}):configHash(value);
      await owner.assertOwned();if((await inspect()).configuration.editHash!==input.baseHash)throw fail('配置编辑版本已变化。',409,'config_changed');
      await jsonFile(draftFile,{hash,value,motionDependenciesSha256:motion?.sha256??null,baseFileHash:current.configuration.fileHash,savedAt:new Date().toISOString()});return {saved:true,activated:false,draftHash:hash,effectiveHash:configuration.effectiveHash};
    }
    if(input.action!=='activate')throw fail('配置操作必须为 save 或 activate。');
    const editing=await draft();if(!editing||editing.hash!==input.draftHash)throw fail('配置草稿版本已变化。',409,'config_changed');
    const value=normalizeConfig(editing.value,{file:configuration.file}),key='activation';
    if(!value.remoteRoot||!value.remotePython||value.runtimeMode==='ssh'&&!value.remoteHost)
      throw fail('请填写计算主机、数据目录和 Python 解释器。',422);
    if(value.motionDependenciesFile&&readMotionDependencies(value.motionDependenciesFile).sha256!==editing.motionDependenciesSha256)throw fail('运动依赖清单在草稿保存后变化，请重新保存并核对。',409,'config_changed');
    if([...claims.values()].some(row=>row.runtimeMode===value.runtimeMode&&row.host===value.remoteHost&&row.destinations?.some(destination=>inside(destination,value.remotePython||'',value.runtimeMode==='ssh'?path.posix:path))))throw fail('所选环境仍有退出未确认的安装记录，请先核实原安装进程。',409,'setup_unknown');
    claim(key,'activate',effective);
    try{
      if(!sameTarget(value,effective)&&value.workspaceRoot===effective.workspaceRoot){let entries=[];try{entries=await readdir(path.join(root,'projects'));}catch(error){if(error.code!=='ENOENT')throw error;}if(entries.length)throw fail('已有工程不能切换计算主机或数据根，请选择新工作区。',409,'target_bound');}
      const busy=resourceBusy('activate',effective);if(busy.length)throw fail('受影响资源尚未空闲。',409,'setup_busy');
      await owner.assertOwned();if((await inspect()).configuration.fileHash!==editing.baseFileHash)throw fail('活动配置文件已被其他操作更改。',409,'config_changed');if((await draft())?.hash!==input.draftHash)throw fail('配置草稿版本已变化。',409,'config_changed');
      const targetOwner=value.workspaceRoot===owner.workspaceRoot?owner:await acquireWorkspaceOwnership({workspaceRoot:value.workspaceRoot,configFile:configuration.file,port:value.port});
      try{await bindWorkspaceTarget(targetOwner,value,{confirmExisting:sameTarget(value,effective)});await owner.assertOwned();if(resourceBusy('activate',effective).length)throw fail('受影响资源重新开始使用。',409,'setup_busy');await jsonFile(configuration.file,value);}finally{if(targetOwner!==owner)await targetOwner.release();}
      return {saved:true,activated:true,draftHash:editing.hash,fileHash:configHash(JSON.stringify(value,null,2)+'\n'),effectiveHash:configuration.effectiveHash,restartRequired:true,gpuDevices:value.gpuDevices};
    }finally{claims.delete(key);}
  }
  async function installation(id,cursor=0){
    if(!validId(id)||!Number.isInteger(cursor)||cursor<0)throw fail('安装标识或游标无效。');
    const wasActive=operations.has(id)||openingInstallations.has(id);
    const receipt=await readJson(path.join(directory,id,'receipt.json'));if(!receipt)throw fail('安装记录不存在。',404);
    if(['running','cancel_requested'].includes(receipt.status)&&!wasActive&&!operations.has(id)&&!openingInstallations.has(id))receipt.status='interrupted';
    const events=[];let buffer='';
    try{
      reading:for await(const chunk of createReadStream(path.join(directory,id,'events.jsonl'),{encoding:'utf8',highWaterMark:16384})){
        buffer+=chunk;let end;
        while((end=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(line){const row=JSON.parse(line);if(row.sequence>cursor)events.push(row);}if(events.length>=100)break reading;}
        if(Buffer.byteLength(buffer)>65536)throw fail('安装事件单行超过预算。',422);
      }
    }catch(error){if(error.code!=='ENOENT')throw error;}
    return {...receipt,events,nextCursor:events.at(-1)?.sequence??cursor,moreEvents:events.length===100};
  }

  async function install(input){
    if(!input||!validId(input.requestId))throw fail('安装请求标识无效。');
    const hash=configHash(canonical(input)),pending=openingInstallations.get(input.requestId);
    if(pending){if(pending.hash!==hash)throw fail('同一安装标识不能更改输入。',409);return pending.work;}
    const work=startInstall(input);openingInstallations.set(input.requestId,{hash,work});
    try{return await work;}finally{openingInstallations.delete(input.requestId);}
  }
  async function startInstall(input){
    if(closed)throw fail('设置服务已关闭。',409);await owner.assertOwned();
    for(const key of Object.keys(input))if(!['requestId','configuration','targetHash','items'].includes(key))throw fail('不支持安装请求字段：'+key);
    if(!['effective','draft'].includes(input.configuration||'effective'))throw fail('安装配置选择无效。');
    if(!validId(input.requestId))throw fail('安装请求标识无效。');
    const requestHash=configHash(canonical(input)),previous=await readJson(path.join(directory,input.requestId,'request.json'));
    if(previous){if(previous.hash!==requestHash)throw fail('同一安装标识不能更改输入。',409);return installation(input.requestId);}
    if(!Array.isArray(input.items)||!input.items.length)throw fail('请选择至少一项安装。');
    const target=await selectedTarget(input),selected=input.items.map(item=>{
      for(const key of Object.keys(item))if(!['recipeId','options','acceptedLicenses'].includes(key))throw fail('不支持安装项字段：'+key);
      if(item.acceptedLicenses!==undefined&&(!Array.isArray(item.acceptedLicenses)||item.acceptedLicenses.some(value=>typeof value!=='string')))throw fail('许可接受项须为明确的许可编号数组。');
      const recipe=recipes.find(recipe=>recipe.id===item.recipeId);if(!recipe)throw fail('不支持安装项：'+item.recipeId);
      if(recipe.platforms&&!recipe.platforms.includes(recipe.host==='studio'?process.platform:target.runtimeMode==='local'?process.platform:'linux'))throw fail('当前平台不支持此安装项。',422);
      const options=validateOptions(item.options);for(const key of Object.keys(options))if(recipe.options&&!recipe.options.some(row=>row.key===key))throw fail('此安装项不支持选项：'+key);for(const option of recipe.options||[])if(option.required&&options[option.key]===undefined)throw fail('缺少安装选项：'+option.key);
      if(recipe.effect==='gpu_verify'&&input.configuration==='draft')throw fail('GPU安装/验证只使用已启用计算配置的设备锁；新目标请在独立实例明确启用后执行。',422);
      if(recipe.available===false)throw fail(recipe.reason||'此操作尚无可验证安装来源。',422);
      for(const license of recipe.licenseIds||[])if(!item.acceptedLicenses?.includes(license))throw fail('请先阅读并选择接受所需许可：'+license,422,'license_required');
      const paths=recipe.host==='compute'&&target.runtimeMode==='ssh'?path.posix:path;
      for(const option of recipe.options||[]){const value=options[option.key];if(value===undefined)continue;if(option.type==='packages'){if(!Array.isArray(value)||!value.length||value.some(id=>typeof id!=='string')||new Set(value).size!==value.length)throw fail('请选择互不重复的有限资产包。');}else if(typeof value!=='string'||!value||option.type==='path'&&!paths.isAbsolute(value))throw fail('安装路径或版本选项无效：'+option.key);}
      for(const license of recipe.packages?.filter(row=>options.packageIds?.includes(row.id)).flatMap(row=>row.licenseIds||[])||[])if(!item.acceptedLicenses?.includes(license))throw fail('请先阅读并选择接受所选资产许可：'+license,422,'license_required');
      const destination=options.destination;if(typeof destination!=='string'||!destination||!paths.isAbsolute(destination))throw fail('请选择全新的绝对安装路径。');
      const base=recipe.host==='studio'?target.workspaceRoot:target.remoteRoot;if(!base||!inside(base,destination,paths))throw fail('安装路径必须位于所选工作区或配置数据根。');
      const local=recipe.host==='studio'||target.runtimeMode==='local',protectsPython=recipe.host==='studio'?effective.runtimeMode==='local':sameComputeHost(target,effective);
      if(recipe.effect==='prepare'&&(protectsPython&&inside(destination,effective.remotePython||'',paths)||local&&inside(destination,ROOT,path)))throw fail('不能覆盖正在使用的环境或工作台。',409);
      if(recipe.action==='assets'&&!inside(paths.join(target.remoteRoot,'assets','staging'),destination,paths))throw fail('资产准备目录须位于数据根的 assets/staging 下。');
      if(options.prefix&&(!paths.isAbsolute(options.prefix)||!inside(target.remoteRoot,options.prefix,paths)||sameComputeHost(target,effective)&&inside(options.prefix,effective.remotePython||'',paths)))throw fail('请选择数据根下尚未启用的独立运行环境前缀。',409);
      if(options.version&&!/^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/.test(options.version))throw fail('请选择明确的版本号。');
      if(options.peakMiB!==undefined&&(!/^\d+$/.test(options.peakMiB)||!Number.isSafeInteger(Number(options.peakMiB))||Number(options.peakMiB)<=0))
        throw fail('验证显存峰值必须是正整数 MiB。');
      return {recipe,options,item,destination,paths,local};
    });
    for(let i=0;i<selected.length;i++)for(let j=0;j<i;j++)if(selected[i].local===selected[j].local&&(inside(selected[i].destination,selected[j].destination,selected[i].paths)||inside(selected[j].destination,selected[i].destination,selected[i].paths)))throw fail('同一请求的安装目录不能重叠。');
    const effect=selected.some(row=>row.recipe.effect==='publish')?'publish':selected.some(row=>row.recipe.effect==='gpu_verify')?'gpu_verify':'prepare';
    if(effect==='publish'&&(input.configuration==='draft'||!configuration.configured||!sameTarget(target,effective)))
      throw fail('请先启用并重启，使该计算目标生效，再登记资产。',409);
    const destinations=selected.flatMap(row=>[row.destination,...(row.options.prefix?[row.options.prefix]:[])]);for(const existing of claims.values())if(existing.host===target.remoteHost&&existing.runtimeMode===target.runtimeMode&&existing.destinations?.some(a=>destinations.some(b=>inside(a,b,target.runtimeMode==='ssh'?path.posix:path)||inside(b,a,target.runtimeMode==='ssh'?path.posix:path))))throw fail('此安装目录与进行中操作重叠。',409,'setup_busy');
    const key=selected.some(row=>row.recipe.action==='register')?'catalog:'+configHash([target.runtimeMode,target.remoteHost,target.remoteRoot]):configHash([target.runtimeMode,target.remoteHost,destinations.sort()]);if(key.startsWith('catalog:')&&[...claims.values()].some(row=>row.effect==='publish'&&(row.key===key||row.unresolved)))throw fail('此计算目标的资产目录正在发布或退出尚未确认。',409,'setup_busy');claim(key,effect,target);Object.assign(claims.get(key),{host:target.remoteHost,runtimeMode:target.runtimeMode,destinations});
    const place=path.join(directory,input.requestId),controller=new AbortController();
    const receipt={requestId:input.requestId,status:'running',effect,targetHash:configHash(target),target:{runtimeMode:target.runtimeMode,host:target.remoteHost,root:target.remoteRoot},resourceKey:key,destinations,startedAt:new Date().toISOString(),registered:false,items:[],receiptRef:'.state/setup/'+input.requestId+'/receipt.json'};
    let sequence=0,events=Promise.resolve();const event=(type,message)=>{const row={sequence:++sequence,type,message:String(message).slice(0,8192),time:new Date().toISOString()};events=events.then(()=>open(path.join(place,'events.jsonl'),'a')).then(async file=>{try{await file.appendFile(JSON.stringify(row)+'\n');}finally{await file.close();}});};
    try{await jsonFile(path.join(place,'request.json'),{hash:requestHash,input});await jsonFile(path.join(place,'receipt.json'),receipt);}catch(error){claims.delete(key);throw error;}
    const work=(async()=>{
      try{
        for(const row of selected){
          if(controller.signal.aborted)throw controller.signal.reason;
          if(!['prepare','gpu_verify'].includes(row.recipe.effect)&&resourceBusy(row.recipe.effect,target).length)throw fail('受影响资源重新开始使用，未执行。',409);
          receipt.currentItem=row.recipe.id;await jsonFile(path.join(place,'receipt.json'),receipt);
          const result=await executeRecipe(row,target,{signal:controller.signal,event,run,registerAssets,root:target.workspaceRoot,receiptDirectory:place});
          receipt.items.push({recipeId:row.recipe.id,...result});if(result.registered)receipt.registered=true;
          await jsonFile(path.join(place,'receipt.json'),receipt);
        }
        receipt.status='completed';
      }catch(error){if(target.runtimeMode==='ssh'&&controller.signal.aborted)error.exitUnconfirmed=true;receipt.status=!error.localProcessNotStarted&&(error.exitUnconfirmed||error.remoteExitUnconfirmed)?'unknown':controller.signal.aborted?'cancelled':'failed';receipt.error=error.message;event('error',error.message);}
      finally{await events;receipt.finishedAt=new Date().toISOString();await jsonFile(path.join(place,'receipt.json'),receipt);if(receipt.status!=='unknown')claims.delete(key);operations.delete(input.requestId);}
    })();operations.set(input.requestId,{controller,work,receipt});
    return {...receipt,statusUrl:'/api/setup/install/'+input.requestId};
  }
  async function cancel(id){const operation=operations.get(id);if(operation){operation.receipt.status='cancel_requested';operation.controller.abort(new Error('用户已取消安装。'));await operation.work;}return installation(id);}
  return {inspect,probe,saveConfig,install,installation,cancel,
    assertAvailable(resource){if([...claims.values()].some(claim=>!['prepare','gpu_verify'].includes(claim.effect)&&(resource==='generation'||resource==='catalog')))throw fail('受影响资源正在设置，请稍后重试。',409,'setup_busy');},
    async close(){closed=true;await Promise.allSettled([...openingInstallations.values()].map(row=>row.work));await configurationEdit;for(const row of operations.values())row.controller.abort(new Error('设置服务关闭。'));await Promise.all([...operations.values()].map(row=>row.work));}
  };
}

async function executeRecipe({recipe,options,destination},target,{signal,event,run,registerAssets,root,receiptDirectory}){
  if(recipe.commands?.some(command=>command.executable==='$npm'))npmCommand();
  const bootstrapTarget=options.basePython?{...target,remotePython:options.basePython}:target;
  if(recipe.host==='compute'){
    const preflight=`import sys,json,shutil,pathlib
q=json.loads(sys.argv[1]);p=pathlib.Path(q['destination'])
while not p.exists() and p!=p.parent:p=p.parent
if sys.platform not in q['platforms']:raise RuntimeError('selected recipe does not support actual compute OS '+sys.platform)
if q['python312'] and sys.version_info[:2]!=(3,12):raise RuntimeError('selected Isaac6 recipe requires Python3.12')
if shutil.disk_usage(p).free<q['diskBytes']:raise RuntimeError('insufficient disk for selected recipe')
print(json.dumps({'platform':sys.platform,'python':sys.version,'freeBytes':shutil.disk_usage(p).free}))`;
    const checked=await pythonCommand(bootstrapTarget,preflight,[JSON.stringify({destination,platforms:recipe.platforms||['linux','darwin','win32'],python312:recipe.id==='isaac6-runtime',diskBytes:recipe.diskBytes||0})],run,{signal,timeoutMs:30000,onOutput:event});
    if(checked.code!==0)throw new Error(checked.stderr||checked.error||'安装先决条件未通过。');
  }
  if(recipe.effect==='prepare'){
    if(target.runtimeMode==='local'||recipe.host==='studio'){
      const absolute=path.resolve(destination);let current=recipe.host==='studio'?root:target.remoteRoot;
      for(const part of path.relative(current,absolute).split(path.sep).filter(Boolean)){current=path.join(current,part);try{if((await lstat(current)).isSymbolicLink())throw fail('安装目录不能经过软链接。',409);}catch(error){if(error.code!=='ENOENT')throw error;}}
      await mkdir(path.dirname(destination),{recursive:true});
      if(recipe.diskBytes){const disk=await statfs(path.dirname(destination));if(disk.bavail*disk.bsize<recipe.diskBytes)throw fail('安装磁盘空间不足。',422);}
      try{await mkdir(destination);}catch(error){if(error.code==='EEXIST')throw fail('安装目的目录已存在，不覆盖已有内容。',409);throw error;}
    }else{
      const prepare=`import pathlib,sys,json,shutil
p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True)
if p.resolve()!=p: raise ValueError('prefix contains symlink')
if shutil.disk_usage(p.parent).free<int(sys.argv[2]): raise ValueError('insufficient disk')
p.mkdir()
print(json.dumps({'created':str(p)}))`;
      const created=await pythonCommand(bootstrapTarget,prepare,[destination,String(recipe.diskBytes||0)],run,{signal,timeoutMs:30000,onOutput:event});
      if(created.code!==0)throw new Error(created.stderr||created.error||'安装目的目录未能独占创建。');
    }
  }
  if(recipe.action==='dependencies'){
    for(const file of ['package.json','package-lock.json','README.md','README.zh-CN.md','studio.config.example.json','SceneAgent.command','LICENSE','THIRD_PARTY.md','requirements-motion.txt','docs/compute.md','docs/assets.md']){
      const source=path.join(ROOT,file);
      if (!(await lstat(source).catch(error=>{if(error.code!=='ENOENT')throw error;}))?.isFile()) continue;
      await mkdir(path.dirname(path.join(destination,file)),{recursive:true});
      await cp(source,path.join(destination,file),{errorOnExist:true,force:false});
    }
    for(const folder of ['src','runtime','web','skills','examples','setup'])await cp(path.join(ROOT,folder),path.join(destination,folder),{recursive:true,errorOnExist:true,force:false,filter:async file=>!(await lstat(file)).isSymbolicLink()&&!['__pycache__','.DS_Store','motion-dependencies.json'].includes(path.basename(file))});
    await mkdir(path.join(destination,'tests'));await cp(path.join(ROOT,'tests/probe_motion_backend.py'),path.join(destination,'tests/probe_motion_backend.py'));
  }
  if(recipe.action==='gpu')return gpuAction(recipe,options,destination,target,{signal,event,run,receiptDirectory});
  if(recipe.action==='assets')return stageAssets(recipe,options,destination,target,{signal,event,run});
  if(recipe.action==='register'){
    if(!registerAssets)throw fail('资产登记需要当前工作台的CPU索引接线。',422);
    return registerAssets({recipe,options,destination,target,signal,event});
  }
  const commands=recipe.commands||[];const results=[];
  let python=options.basePython||target.remotePython;
  for(const command of commands){
    const values={destination,version:options.version,python,newPython:(recipe.host==='compute'&&target.runtimeMode==='ssh'?path.posix:path).join(destination,process.platform==='win32'&&recipe.host==='studio'?'Scripts':'bin',process.platform==='win32'&&recipe.host==='studio'?'python.exe':'python')};
    const replace=value=>String(value).replace(/\{(destination|version|python|newPython)\}/g,(_,key)=>{if(!values[key])throw fail('缺少安装选项：'+key);return values[key];});
    if(options.version&&!/^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/.test(options.version))throw fail('请选择明确的版本号。');
    let executable=replace(command.executable),args=(command.args||[]).map(replace);
    if(executable==='$node')executable=process.execPath;
    if(executable==='$npm'){const command=npmCommand();executable=command[0];args=[command[1],...args];}
    event('command',JSON.stringify({executable,args}));
    const result=recipe.host==='compute'&&target.runtimeMode==='ssh'?await sshResult(target.remoteHost,[quote(executable),...args.map(quote)].join(' '),run,{signal,timeoutMs:3600000,onOutput:event})
      :await run(executable,args,{signal,timeoutMs:3600000,onOutput:event});
    results.push({executable,args,code:result.code,exitConfirmed:result.exitConfirmed!==false});
    if(result.exitConfirmed===false)throw Object.assign(new Error(result.stderr||result.error||'安装进程退出未确认。'),{exitUnconfirmed:true});
    if(signal.aborted)throw signal.reason;
    if(result.error||result.code!==0)throw new Error(result.stderr||result.error||'安装命令未成功退出。');
  }
  const actual={};
  if(recipe.id==='codex-cli'){
    const command=resolveCodexCommand({codexScript:path.join(destination,'node_modules/@openai/codex/bin/codex.js')});
    const version=await run(command[0],[...command.slice(1),'--version'],{signal,timeoutMs:15000,onOutput:event});
    if(version.code!==0)throw new Error(version.stderr||'已安装Codex入口未通过版本检查。');actual.version=version.stdout.trim();actual.command=command;actual.package=JSON.parse(await readFile(path.join(destination,'node_modules/@openai/codex/package.json'),'utf8'));actual.packageLockHash=configHash(await readFile(path.join(destination,'package-lock.json'),'utf8'));
  }
  if(recipe.id==='workspace-dependencies')actual.packageLockHash=configHash(await readFile(path.join(destination,'package-lock.json'),'utf8'));
  if(recipe.id==='isaac6-runtime'){
    const python=path.posix.join(destination,'bin','python');
    const measured=await pythonCommand({...target,remotePython:python},metadataProbe,[JSON.stringify({root:target.remoteRoot})],run,{signal,timeoutMs:30000,onOutput:event});
    if(measured.code!==0)throw new Error(measured.stderr||'新环境元数据读取失败。');actual.metadata=JSON.parse(measured.stdout);
    if(actual.metadata.packages?.isaacsim!=='6.0.0.1'||!actual.metadata.python?.version?.startsWith('3.12.'))throw new Error('已安装环境版本与匹配配置不同。');
  }
  return {status:'completed',destination,commands:results,actual,registered:false,runtimeVerified:false};
}

async function stageAssets(recipe,options,destination,target,{signal,event,run}){
  const manifest=JSON.parse(await readFile(path.join(ROOT,'setup','asset-manifests',recipe.manifest),'utf8'));
  const ids=options.packageIds;if(!Array.isArray(ids)||!ids.length)throw fail('请选择有限资产包；不会默认下载整个库。');
  const selected=ids.map(id=>{const row=manifest.packages.find(row=>row.id===id);if(!row)throw fail('资产包不属于固定清单：'+id);return row;});
  const files=selected.flatMap(row=>row.files);if(!files.length||files.some(row=>!Number.isSafeInteger(row.size)||row.size<0||!row.sha256&&!row.gitBlob))throw fail('资产清单缺少有限大小或摘要，未下载。',422);
  const prefix=target.runtimeMode==='ssh'?path.posix.relative(path.posix.join(target.remoteRoot,'assets'),destination):path.relative(path.join(target.remoteRoot,'assets'),destination).split(path.sep).join('/');
  const assets=selected.flatMap(row=>row.asset?[row.asset]:row.assets||[]).map(asset=>({...asset,packageRoot:path.posix.join(prefix,asset.packageRoot)}));
  const request={destination,repository:manifest.repository,revision:manifest.revision,files,catalog:{schema:'sceneagent-asset-collections-v1',collections:[manifest.collection],assets}};
  const script=await readFile(path.join(ROOT,'setup','stage-assets.py'),'utf8');
  event('download',JSON.stringify({source:manifest.repository,revision:manifest.revision,files:files.length,bytes:files.reduce((sum,row)=>sum+row.size,0)}));
  const result=await pythonCommand(target,script,[JSON.stringify(request)],run,{signal,timeoutMs:3600000,onOutput:event});
  if(result.code!==0)throw new Error(result.stderr||result.error||'资产准备失败，保留已下载前缀。');
  return {status:'completed',destination,source:manifest.repository,revision:manifest.revision,
    catalogFile:(target.runtimeMode==='ssh'?path.posix:path).join(destination,'staged-catalog.json'),
    bytes:files.reduce((sum,row)=>sum+row.size,0),registered:false,runtimeVerified:false};
}

async function gpuAction(recipe,options,destination,target,{signal,event,run,receiptDirectory}) {
  const device=target.gpuDevices[0];if(!device)throw fail('请先检测并选择验证显卡。',422);
  const python=path.posix.join(options.prefix,'bin','python');
  const runtimeFiles={};for(const name of ['jobs.py','records.py','scene.py','gpu_reservation.py'])runtimeFiles[name]=(await readFile(path.join(ROOT,'runtime',name))).toString('base64');
  const commands=recipe.id==='motion-backend'?[[python,'-m','pip','install','--no-deps','--no-build-isolation','git+https://github.com/NVlabs/curobo.git@ebb71702f3f70e767f40fd8e050674af0288abe8']]:[];
  const probeSource=recipe.id==='verify-runtime'?await readFile(path.join(ROOT,'tests/probe_motion_backend.py'),'utf8'):null;
  if(probeSource){
    if(!options.robotPackage||!path.posix.isAbsolute(options.robotPackage)||!inside(target.remoteRoot,options.robotPackage,path.posix))throw fail('原生验证需要数据根内已有的真实冻结机器人包。',422);
    for(const phase of ['numerical','native'])commands.push([python,path.posix.join(destination,'probe_motion_backend.py'),'--phase',phase,'--output',path.posix.join(destination,phase+'-receipt.json'),'--gpu-index',String(device.index),'--gpu-uuid',device.uuid,'--robot-package',options.robotPackage]);
  }
  const request={destination,prefix:options.prefix,root:target.remoteRoot,runtimeFiles,commands,device,verify:recipe.id==='verify-runtime',
    peakMiB:options.peakMiB?Number(options.peakMiB):target.remoteGpuPeakMiB,probeSource};
  const script=await readFile(path.join(ROOT,'setup/gpu-action.py'),'utf8');
  const result=await pythonCommand(target,script,[JSON.stringify(request)],run,{signal,timeoutMs:3600000,onOutput:event});
  if(result.exitConfirmed===false||target.runtimeMode==='ssh'&&signal.aborted||result.stderr?.includes('GPU_SETUP_EXIT_UNCONFIRMED'))
    throw Object.assign(new Error('GPU设置子进程退出未确认；保留原目录和显存预留记录。'),{exitUnconfirmed:true});
  if(signal.aborted)throw signal.reason;
  if(result.code!==0)throw new Error(result.stderr||result.error||'显式GPU设置未成功。');
  let motionDependenciesFile,motionDependenciesSha256;
  if(recipe.id==='verify-runtime'){
    const collect=`import pathlib,sys,json,hashlib
f=pathlib.Path(sys.argv[1])/'motion-dependencies.json'
if not f.is_file() or f.is_symlink() or f.stat().st_size>8*1024*1024:raise ValueError('verified manifest missing or oversized')
b=f.read_bytes();print(json.dumps({'text':b.decode('utf8'),'sha256':hashlib.sha256(b).hexdigest()}))`;
    const saved=await pythonCommand(target,collect,[destination],run,{signal,timeoutMs:30000,maxBytes:16*1024*1024});
    if(saved.code!==0)throw new Error(saved.stderr||'新运行环境的实际依赖清单未能取回。');
    const manifest=JSON.parse(saved.stdout);if(configHash(manifest.text)!==manifest.sha256)throw new Error('依赖清单传输摘要不一致。');
    motionDependenciesFile=path.join(receiptDirectory,'motion-dependencies.json');await writeFile(motionDependenciesFile,manifest.text,{flag:'wx',mode:0o600});
    const selected=readMotionDependencies(motionDependenciesFile);if(selected.executable!==python)throw new Error('验证后清单属于其他解释器。');motionDependenciesSha256=selected.sha256;
  }
  return {status:'completed',destination,device,...(motionDependenciesFile?{motionDependenciesFile,motionDependenciesSha256}:{}),runtimeVerified:recipe.id==='verify-runtime',scope:'system runtime checks only; no task-success claim'};
}
