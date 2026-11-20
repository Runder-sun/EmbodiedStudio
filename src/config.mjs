import { readFileSync, existsSync, openSync, fstatSync, readSync, closeSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import path from 'node:path';

export const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const fail = message => Object.assign(new Error(message), { status: 400 });
export const configHash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');

export function selectedConfigFile({ argv = process.argv.slice(2), env = process.env } = {}) {
  const index = argv.indexOf('--config');
  if (index >= 0 && !argv[index + 1]) throw fail('--config 需要配置文件路径。');
  return path.resolve(index >= 0 ? argv[index + 1] : env.SCENEAGENT_CONFIG || path.join(ROOT, 'studio.config.json'));
}

export function validId(value) {
  return typeof value === 'string' && /^[a-zA-Z0-9_-]{1,80}$/.test(value);
}

export function quote(value) {
  return "'" + String(value).replaceAll("'", "'\\''") + "'";
}

export function validSshHost(value) {
  return typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.@:[\]-]{0,199}$/.test(value);
}

export function normalizeConfig(raw, { file = path.join(ROOT, 'studio.config.json') } = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw fail('配置必须是对象。');
  const allowed = new Set(['port','workspaceRoot','runtimeMode','remoteHost','remoteRoot','remotePython','legacyRoot','remoteGpu','remoteGpuPeakMiB','gpuDevices','gpuLockRoot','codexBinary','codexScript','codexModel','motionDependenciesFile']);
  for (const name of Object.keys(raw)) if (!allowed.has(name)) throw fail('不支持配置字段：' + name);
  const port = raw.port ?? 8765;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw fail('端口必须是1至65535的整数。');
  const runtimeMode = raw.runtimeMode || (raw.remoteHost ? 'ssh' : 'local');
  if (!['local','ssh'].includes(runtimeMode)) throw fail('执行位置必须是 local 或 ssh。');
  if (runtimeMode === 'ssh' && raw.remoteHost != null && !validSshHost(raw.remoteHost)) throw fail('请选择有效的 SSH 主机别名。');
  if (raw.workspaceRoot != null && (typeof raw.workspaceRoot !== 'string' || /[\r\n\0]/.test(raw.workspaceRoot)))
    throw fail('工作区路径无效。');
  const absolute = value => runtimeMode === 'ssh' ? path.posix.isAbsolute(value) : path.isAbsolute(value);
  for (const name of ['remoteRoot','remotePython','legacyRoot','gpuLockRoot']) {
    if (raw[name] != null && (typeof raw[name] !== 'string' || !absolute(raw[name]) || /[\r\n\0]/.test(raw[name]))) throw fail(name + ' 必须是执行主机上的绝对路径。');
  }
  if (raw.remoteGpuPeakMiB != null && (!Number.isSafeInteger(raw.remoteGpuPeakMiB) || raw.remoteGpuPeakMiB <= 0))
    throw fail('显存峰值必须是正整数 MiB。');
  const devices = raw.gpuDevices === undefined ? (raw.remoteGpu ? [raw.remoteGpu] : []) : raw.gpuDevices;
  if (!Array.isArray(devices)) throw fail('GPU 配置必须是数组。');
  const indices = new Set(), uuids = new Set();
  for (const device of devices) {
    if (!device || Object.keys(device).sort().join(',') !== 'index,uuid' || !Number.isInteger(device.index) || device.index < 0
      || !/^GPU-[a-f0-9-]{36}$/i.test(device.uuid || '') || indices.has(device.index) || uuids.has(device.uuid)) throw fail('GPU 编号、UUID或重复显卡配置无效。');
    indices.add(device.index); uuids.add(device.uuid);
  }
  for (const name of ['codexBinary','codexScript','codexModel','motionDependenciesFile']) if (raw[name] != null && (typeof raw[name] !== 'string' || !raw[name].trim() || /[\r\n\0]/.test(raw[name]))) throw fail(name + ' 必须是有效文本。');
  if(raw.motionDependenciesFile!=null&&!path.isAbsolute(raw.motionDependenciesFile))throw fail('motionDependenciesFile 必须是 Studio 主机上的绝对路径。');
  if (raw.codexScript && raw.codexBinary && raw.codexBinary !== 'codex') throw fail('Codex 原生程序与官方 JS 入口不能同时自定义。');
  const value = { port, workspaceRoot: path.resolve(path.dirname(file), raw.workspaceRoot || (path.resolve(file) === path.join(ROOT,'studio.config.json') ? ROOT : 'workspace')),
    runtimeMode, remoteHost: runtimeMode === 'ssh' ? raw.remoteHost ?? null : null, remoteRoot: raw.remoteRoot ?? null,
    remotePython: raw.remotePython ?? null, legacyRoot: raw.legacyRoot ?? null,
    gpuDevices: Object.freeze(devices.map(device => Object.freeze({ index:device.index,uuid:device.uuid }))),
    ...(raw.remoteGpuPeakMiB != null ? { remoteGpuPeakMiB:raw.remoteGpuPeakMiB } : {}),
    ...(raw.gpuLockRoot ? { gpuLockRoot:raw.gpuLockRoot } : {}),
    ...(raw.codexScript ? { codexScript:raw.codexScript } : { codexBinary:raw.codexBinary || 'codex' }),
    ...(raw.codexModel ? { codexModel:raw.codexModel } : {}),
    ...(raw.motionDependenciesFile?{motionDependenciesFile:raw.motionDependenciesFile}:{}) };
  return Object.freeze(value);
}

export function readMotionDependencies(file) {
  const fd=openSync(file,'r');let text;
  try{const info=fstatSync(fd);if(!info.isFile()||info.size>8*1024*1024)throw fail('运动依赖清单必须是8MiB内的普通文件。');const bytes=Buffer.alloc(info.size+1);let length=0,count;while(length<bytes.length&&(count=readSync(fd,bytes,length,bytes.length-length,null))>0)length+=count;if(length!==info.size)throw fail('运动依赖清单在读取时变化。');text=bytes.subarray(0,length).toString('utf8');}finally{closeSync(fd);}
  const document=JSON.parse(text),identity=document.verification?.actualLoadedIdentity;
  const within=value=>typeof value==='string'&&typeof identity?.prefix==='string'&&path.posix.isAbsolute(value)&&path.posix.isAbsolute(identity.prefix)&&!path.posix.relative(identity.prefix,value).startsWith('..')&&path.posix.normalize(value)===value;
  const fileIdentity=row=>row&&Number.isSafeInteger(row.bytes)&&row.bytes>=0&&/^[a-f0-9]{64}$/.test(row.sha256||'');
  const required=['curobo','warp','torch',...['geom','kinematics_fused','lbfgs_step','line_search','tensor_step'].map(name=>'curobo.curobolib.'+name+'_cu')];
  if(document.schema!=='sceneagent-motion-dependencies-v1'||document.backend?.name!=='cuRobo'||document.backend?.commit!=='ebb71702f3f70e767f40fd8e050674af0288abe8'
    ||document.verification?.assessment?.decision!=='PASS_WITH_EXPLICIT_DEVICE_SCOPE'||!identity?.prefix||!identity?.executable||!identity?.SOABI
    ||!within(identity.executable)||!required.every(name=>within(identity.modules?.[name]?.path)&&fileIdentity(identity.modules[name]))
    ||!required.filter(name=>name.startsWith('curobo.curobolib.')).every(name=>fileIdentity(identity.mappedNativeLibraries?.[identity.modules[name].path]))
    ||!document.sourceFiles||!Object.keys(document.sourceFiles).length||!Object.entries(document.sourceFiles).every(([file,row])=>within(file)&&fileIdentity(row)))throw fail('运动依赖清单缺少已验证的固定版本、路径或完整身份。');
  return Object.freeze({text,sha256:configHash(text),prefix:identity.prefix,executable:identity.executable});
}

export function loadConfiguration({ file = selectedConfigFile() } = {}) {
  file = path.resolve(file);
  let raw = null, value, motionDependencies=null, diagnostics = [];
  try { raw = readFileSync(file,'utf8'); value = normalizeConfig(JSON.parse(raw), { file });if(value.motionDependenciesFile){motionDependencies=readMotionDependencies(value.motionDependenciesFile);if(motionDependencies.executable!==value.remotePython)throw fail('运动依赖清单与所选执行解释器不一致。');} }
  catch (error) { value=undefined;diagnostics.push(error.code === 'ENOENT' ? '尚未创建配置文件。' : error.message); }
  const configured = Boolean(value?.remoteRoot && value?.remotePython && (value.runtimeMode === 'local' || value.remoteHost));
  if (!configured && !diagnostics.length) diagnostics.push('请先选择执行位置、数据目录和解释器。');
  const setupRoot = path.join(path.dirname(file), '.sceneagent-setup', configHash(file).slice(0,16));
  return Object.freeze({ file, fileHash:raw === null ? null : configHash(raw), configured, diagnostics,
    value:value || normalizeConfig({ runtimeMode:process.platform === 'linux' ? 'local' : 'ssh' }, { file }),
    setupRoot,motionDependencies,effectiveHash:configured?(motionDependencies?configHash({configuration:value,motionDependenciesSha256:motionDependencies.sha256}):configHash(value)):null });
}

function officialScript(script) {
  if (!existsSync(script) || path.basename(script) !== 'codex.js' || path.basename(path.dirname(script)) !== 'bin') return false;
  try { return JSON.parse(readFileSync(path.join(path.dirname(script),'..','package.json'),'utf8')).name === '@openai/codex'; }
  catch { return false; }
}

export function resolveCodexCommand(value, { platform = process.platform, node = process.execPath, env = process.env } = {}) {
  if (value.codexScript) {
    if (!officialScript(value.codexScript)) throw fail('请选择完整安装的官方 Codex JS 入口。');
    return [node,path.resolve(value.codexScript)];
  }
  let binary = value.codexBinary || 'codex';
  if (platform === 'win32' && !/[\\/]/.test(binary)) {
    const directories = (env.Path || env.PATH || '').split(platform === process.platform ? path.delimiter : ';');
    const names = /\.[a-z]+$/i.test(binary) ? [binary] : [binary+'.exe',binary+'.cmd',binary+'.bat'];
    binary = directories.flatMap(directory=>names.map(name=>path.join(directory,name))).find(existsSync);
    if (!binary) throw fail('尚未找到 Codex 原生程序或完整官方 JS 安装。');
  }
  if (platform === 'win32' && /\.(cmd|bat)$/i.test(binary)) {
    const roots = [path.join(path.dirname(binary),'node_modules','@openai','codex','bin','codex.js'),
      path.join(path.dirname(binary),'..','@openai','codex','bin','codex.js')];
    const script = roots.find(officialScript);
    if (!script) throw fail('Codex .cmd 入口需要完整官方 JS 安装或原生 codex.exe。');
    return [node,path.resolve(script)];
  }
  return [binary];
}

export const configuration = loadConfiguration();
export const config = { ...configuration.value, remoteGpu:configuration.value.gpuDevices[0],
  ...(configuration.motionDependencies ? { motionDependencies:configuration.motionDependencies } : {}) };
