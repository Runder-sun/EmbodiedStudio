import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { EventEmitter, getEventListeners } from 'node:events';

test('CPU queries and check submission do not need GPU configuration or forward caller device fields', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-cpu-packet-'));
  t.after(() => rm(root, { recursive:true, force:true }));
  await mkdir(path.join(root,'runtime'));
  await writeFile(path.join(root,'runtime/worker.py'),'# SYSTEM_VERIFICATION_ONLY\n');
  const code = await readFile(new URL('../src/remote.mjs', import.meta.url),'utf8');
  const context = { ROOT:root, config:{ remoteRoot:'/data',legacyRoot:'/legacy',remoteGpu:null },
    readdir,readFile,path,createHash };
  vm.runInNewContext(code.slice(code.indexOf('async function runtimePacket('),code.indexOf('export async function executeRemote('))+'\nthis.packet=runtimePacket;',context);
  for (const operation of ['health','assets','inspect','job','check','read','list','write','submit']) {
    const packet = await context.packet({ operation,device:{ index:42 },task:{ operation:'check',device:{index:43} } });
    assert.equal(Object.hasOwn(packet.payload,'device'),false,operation);
    assert.equal(Object.hasOwn(packet.payload.task,'device'),false,operation);
    assert.equal(packet.payload.root,'/data');
  }
  for (const operation of ['build','rollout','consume','evaluate','prepare','observe','unknown']) {
    await assert.rejects(context.packet({operation}),/显卡/);
    await assert.rejects(context.packet({operation:'submit',task:{operation}}),/显卡/);
  }
});

async function sshFixture(remoteHost = 'fixture-user@first.example') {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  let invoked;
  const context = { config: { remoteHost, remoteRoot: '/fixture', remotePython: '/fixture/bin/python' }, spawn: (program, args, options) => {
    invoked = { program, args: Array.from(args), options }; return invoked;
  } };
  const start = source.indexOf('function ssh(command)'), end = source.indexOf('function singleJsonRemote(', start);
  vm.runInNewContext(source.slice(start, end) + '\nthis.ssh=ssh;', context);
  context.ssh('exact original remote command');
  return invoked;
}

test('SSH reuses a host connection for at most 60 idle seconds without changing the command or streams', async () => {
  const invoked = await sshFixture();
  assert.equal(invoked.program, 'ssh');
  for (const option of ['ControlMaster=auto', 'ControlPersist=60', 'ControlPath=~/.ssh/sceneagent-studio-%C',
    'BatchMode=yes', 'ConnectTimeout=12', 'ServerAliveInterval=15']) assert.ok(invoked.args.includes(option), option);
  assert.ok(invoked.args.includes('-C'));
  assert.deepEqual(invoked.args.slice(-2), ['fixture-user@first.example', 'exact original remote command']);
  assert.deepEqual(Array.from(invoked.options.stdio), ['pipe', 'pipe', 'pipe']);
});

test('the native SSH control socket identity separates remote users, hosts and ports', async () => {
  const identities = [];
  for (const host of ['ssh://alice@first.example:2222', 'ssh://bob@first.example:2222',
    'ssh://alice@second.example:2222', 'ssh://alice@first.example:2223']) {
    const invoked = await sshFixture(host);
    const result = spawnSync('ssh', ['-G', '-F', '/dev/null', ...invoked.args.slice(0, -1)], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    const socket = result.stdout.match(/^controlpath (.+)$/m)?.[1];
    assert.ok(socket?.includes('sceneagent-studio-'), 'an expanded per-connection socket is required');
    identities.push(socket);
  }
  assert.equal(new Set(identities).size, identities.length);
});

test('every reused runtime request still rejects changed bytes in an existing immutable snapshot', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-runtime-byte-check-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('const bootstrap ='), end = source.indexOf('\nfunction ssh(', start);
  const context = {};
  vm.runInNewContext(source.slice(start, end) + '\nthis.bootstrap=bootstrap;', context);
  const bytes = Buffer.from('print("SYSTEM_VERIFICATION_ONLY immutable worker")\n');
  const packet = { files: { 'worker.py': bytes.toString('base64') },
    codeHash: createHash('sha256').update(bytes).digest('hex'), payload: { root } };
  const invoke = () => spawnSync('python3', ['-B', '-c', context.bootstrap], { input: JSON.stringify(packet), encoding: 'utf8' });
  assert.equal(invoke().status, 0);
  const worker = path.join(root, 'runtime', packet.codeHash, 'runtime', 'worker.py');
  await writeFile(worker, 'raise AssertionError("changed worker must never execute")\n');
  const result = invoke();
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /runtime snapshot mismatch/);
  assert.doesNotMatch(result.stderr, /AssertionError/);
});

test('runtime references execute only an intact cached snapshot and report missing code before any operation', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-runtime-reference-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const context = {};
  vm.runInNewContext(source.slice(source.indexOf('const bootstrap ='), source.indexOf('\nfunction ssh('))+'\nthis.bootstrap=bootstrap;',context);
  const bytes = Buffer.from('import json\nprint(json.dumps({"type":"result","result":{"checked":True}}))\n');
  const hash = createHash('sha256').update('worker.py').update(bytes).digest('hex');
  const full = { files: {'worker.py':bytes.toString('base64')}, codeHash:hash, payload:{root} };
  const reference = { codeHash:hash, fileNames:['worker.py'], payload:{root} };
  const invoke = value => spawnSync('python3',['-B','-c',context.bootstrap],{input:JSON.stringify(value),encoding:'utf8'});
  const cold = invoke(reference);
  assert.equal(cold.status,75,cold.stderr);
  assert.deepEqual(JSON.parse(cold.stdout),{type:'runtime_missing',codeHash:hash});
  assert.equal(invoke(full).status,0);
  const warm = invoke(reference);
  assert.equal(warm.status,0,warm.stderr);
  assert.deepEqual(JSON.parse(warm.stdout).result,{checked:true});
  const worker=path.join(root,'runtime',hash,'runtime/worker.py');
  await writeFile(worker,'raise AssertionError("tampered code must not execute")\n');
  const changed=invoke(reference);
  assert.notEqual(changed.status,0);
  assert.notEqual(changed.status,75,'corrupt code is not a missing snapshot');
  assert.match(changed.stderr,/runtime snapshot mismatch/);
  assert.doesNotMatch(changed.stderr,/AssertionError/);
  await rm(worker);
  assert.equal(invoke(reference).status,75);
});

async function referenceTransport(t, replies, options={}) {
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8');
  const packet={files:{'worker.py':Buffer.from('# SYSTEM_VERIFICATION_ONLY\n').toString('base64')},codeHash:'a'.repeat(64),
    payload:{operation:'submit',jobId:'j_reference',requestId:'r_reference',projectId:'p_reference',generationId:'g_reference'}};
  const sent=[],events=[];
  const context={runtimePacket:async()=>packet,quote:value=>value,bootstrap:'SYSTEM_VERIFICATION_ONLY',config:{remotePython:'unused'},setTimeout,clearTimeout,
    ssh:()=>{
      const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();
      child.stdout.setEncoding=()=>{};child.kill=()=>{};
      child.stdin.end=value=>{sent.push(JSON.parse(value));queueMicrotask(()=>replies[sent.length-1](child,packet));};
      return child;
    }};
  vm.runInNewContext(source.slice(source.indexOf('export async function executeRemote('),source.indexOf('export function validateArtifactPath(')).replace('export ', '')+'\nthis.execute=executeRemote;',context);
  return {sent,packet,events,reply:context.execute(packet.payload,{...options,onEvent:event=>events.push(event)})};
}

test('runtime transport sends a small reference and uploads once only after an explicit pre-execution cache miss', async t => {
  const f=await referenceTransport(t,[
    (child,p)=>{child.stdout.emit('data',JSON.stringify({type:'runtime_missing',codeHash:p.codeHash})+'\n');child.emit('close',75);},
    child=>{child.stdout.emit('data','{"type":"result","result":{"state":"queued"}}\n');child.emit('close',0);}
  ]);
  assert.equal((await f.reply).state,'queued');
  assert.equal(f.sent.length,2);
  assert.equal(f.sent[0].files,undefined);
  assert.deepEqual(f.sent[0].fileNames,['worker.py']);
  assert.deepEqual(f.sent[1],f.packet);
  assert.deepEqual(f.events.map(row=>row.type),['result']);
});

for (const reason of ['unknown_exit','wrong_hash','prior_progress','abort']) test('runtime transport does not resubmit on '+reason, async t => {
  const controller=new AbortController();
  const f=await referenceTransport(t,[(child,p)=>{
    if(reason==='prior_progress')child.stdout.emit('data','{"type":"progress","stage":"started"}\n');
    if(reason!=='unknown_exit')child.stdout.emit('data',JSON.stringify({type:'runtime_missing',codeHash:reason==='wrong_hash'?'b'.repeat(64):p.codeHash})+'\n');
    if(reason==='abort')controller.abort();
    child.emit('close',reason==='unknown_exit'?7:75);
  }],{signal:controller.signal});
  await assert.rejects(f.reply);
  assert.equal(f.sent.length,1);
});

test('runtime packet locks dependency manifest and one host assigned device including nested jobs', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-runtime-packet-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, 'runtime'));
  await writeFile(path.join(root, 'runtime', 'worker.py'), '# isolated fixture\n');
  await writeFile(path.join(root, 'runtime', 'motion-dependencies.json'), '{"fixture":1}');
  await writeFile(path.join(root, 'runtime', 'other.json'), 'excluded');
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('async function runtimePacket('), end = source.indexOf('export async function executeRemote(', start);
  const device = { index:3, uuid:'GPU-30000000-0000-0000-0000-000000000000' };
  const config = { remoteRoot:'/data', legacyRoot:'/legacy', remoteGpu:device };
  const context = { readdir, readFile, path, createHash, ROOT:root, config };
  vm.runInNewContext(source.slice(start, end)+'\nthis.packet=runtimePacket;', context);
  const packet = await context.packet({ operation:'submit', device:{ index:0 }, task:{ operation:'rollout', device:{ index:1 } } });
  assert.deepEqual(JSON.parse(JSON.stringify(packet.payload.device)), device);
  assert.deepEqual(JSON.parse(JSON.stringify(packet.payload.task.device)), device);
  assert.equal(packet.payload.root, '/data');
  assert.deepEqual(Object.keys(packet.files), ['motion-dependencies.json','worker.py']);
  const before = packet.codeHash;
  await writeFile(path.join(root, 'runtime', 'motion-dependencies.json'), '{"fixture":2}');
  assert.notEqual((await context.packet({ operation:'health' })).codeHash, before);
  config.remoteGpuPeakMiB = 16000;
  await assert.rejects(context.packet({ operation:'build' }), /卡 0、1、2/);
  const sharedDevice = { index:0, uuid:'GPU-10000000-0000-0000-0000-000000000000' };
  const shared = await context.packet({ operation:'submit', gpuPeakMiB:1,
    task:{ operation:'rollout', gpuPeakMiB:2 } }, sharedDevice, 7000);
  assert.equal(shared.payload.gpuPeakMiB, 7000);
  assert.equal(shared.payload.task.gpuPeakMiB, 7000);
  assert.equal((await context.packet({ operation:'health', gpuPeakMiB:1 })).payload.gpuPeakMiB, undefined);
  config.remoteGpu = null;
  await assert.rejects(context.packet({ operation:'submit' }), /显卡/);
});

async function executionFixture(t, { signal, endError, runtimePacket } = {}) {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const child = new EventEmitter(), timers = [], signals = [], events = [];
  child.stdin = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin.end = value => { if (endError) throw endError; child.input = JSON.parse(value); };
  child.stdout.setEncoding = () => {};
  for (const stream of [child.stdin, child.stdout, child.stderr]) stream.destroy = () => {};
  child.kill = value => { signals.push(value); return true; };
  let spawned = 0;
  const context = { runtimePacket: runtimePacket || (async payload => ({ payload })), ssh: () => { spawned++; return child; }, quote: value => value,
    bootstrap: 'SYSTEM_VERIFICATION_ONLY transport fixture', config: { remotePython: 'python3' },
    setTimeout: (fn, ms) => { const timer = { fn, ms, cleared: false }; timers.push(timer); return timer; },
    clearTimeout: timer => { if (timer) timer.cleared = true; } };
  const start = source.indexOf('export async function executeRemote('), end = source.indexOf('export function validateArtifactPath(', start);
  vm.runInNewContext(source.slice(start, end).replace('export ', '')+'\nthis.execute=executeRemote;', context);
  const state = { settled: false };
  const reply = context.execute({ operation: 'assets', query: 'oil bottle' },
    { timeoutMs: 1234, signal, onEvent: event => events.push(event) }).then(value => {
    state.settled = true; state.value = value;
  }, error => { state.settled = true; state.error = error; });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  t.after(() => { child.emit('close', 7); });
  return { child, timers, signals, events, state, reply, flush, spawnCount: () => spawned };
}

test('runtime execution resolves actual successful output only after SSH closes normally', async t => {
  const controller = new AbortController();
  const f = await executionFixture(t, { signal: controller.signal });
  assert.equal(getEventListeners(controller.signal, 'abort').length, 1);
  f.child.stdout.emit('data', '{"type":"progress","stage":"assets"}\n{"type":"result","result":{"items":[]}}\n');
  await f.flush();
  assert.equal(f.state.settled, false);
  f.child.emit('close', 0); await f.reply;
  assert.equal(f.state.error, undefined);
  assert.deepEqual(JSON.parse(JSON.stringify(f.state.value)), { items: [] });
  assert.equal(f.events.length, 2);
  assert.deepEqual(f.signals, []);
  assert.ok(f.timers.every(timer => timer.cleared));
  assert.equal(getEventListeners(controller.signal, 'abort').length, 0);
  controller.abort();
  assert.deepEqual(f.signals, []);
});

for (const reason of ['timeout', 'cancel']) test('runtime execution bounds local SSH cleanup after '+reason+' when signals are ignored', async t => {
  const controller = new AbortController();
  const f = await executionFixture(t, { signal: controller.signal });
  for (const stream of [f.child.stdin, f.child.stdout, f.child.stderr]) {
    stream.destroy = () => stream.emit('error', new Error('destroy emitted a later cleanup error'));
  }
  if (reason === 'timeout') f.timers[0].fn();
  else controller.abort();
  await f.flush();
  assert.deepEqual(f.signals, ['SIGTERM']);
  assert.equal(f.state.settled, false, 'local process exit must be confirmed or explicitly reported unconfirmed');
  const escalation = f.timers[1];
  assert.ok(escalation, 'SIGTERM without close must schedule bounded SIGKILL escalation');
  assert.ok(escalation.ms > 0 && escalation.ms <= 1000);
  escalation.fn(); await f.flush();
  assert.deepEqual(f.signals, ['SIGTERM', 'SIGKILL']);
  const cleanup = f.timers[2];
  assert.ok(cleanup, 'SIGKILL without close must have a final local exit-confirmation bound');
  assert.ok(cleanup.ms > 0 && cleanup.ms <= 1000);
  cleanup.fn(); await f.reply;
  assert.equal(f.state.error?.exitUnconfirmed, true);
  assert.match(f.state.error.message, reason === 'timeout' ? /超时/ : /取消/);
  assert.match(f.state.error.message, /SSH.*退出未确认/);
  assert.match(f.state.error.message, /远端.*(?:未确认|未知|重新查询)/);
  assert.doesNotMatch(f.state.error.message, /执行已中断|作业已取消/);
  assert.ok(f.timers.every(timer => timer.cleared));
  assert.equal(getEventListeners(controller.signal, 'abort').length, 0);
  const original = f.state.error.message;
  f.child.stdout.emit('data', '{"type":"result","result":{"late":true}}\n');
  f.child.emit('close', 0);
  f.child.emit('error', new Error('late process error'));
  assert.equal(f.state.error.message, original);
  assert.equal(f.state.value, undefined);
});

test('runtime execution preserves the first local failure while cleaning up and rejects late successful output', async t => {
  const f = await executionFixture(t);
  f.child.emit('error', new Error('first actual SSH failure'));
  await f.flush();
  assert.deepEqual(f.signals, ['SIGTERM']);
  f.child.emit('error', new Error('later cleanup failure'));
  f.child.stdout.emit('data', '{"type":"result","result":{"items":["late output"]}}\n');
  f.child.emit('close', 0); await f.reply;
  assert.match(f.state.error?.message || '', /first actual SSH failure/);
  assert.doesNotMatch(f.state.error.message, /later cleanup failure/);
  assert.equal(f.state.value, undefined);
  assert.ok(f.timers.every(timer => timer.cleared));
});

test('runtime execution preserves timeout over a late remote error or result even if SSH later exits zero', async t => {
  const f = await executionFixture(t);
  f.timers[0].fn();
  f.child.stdout.emit('data', '{"type":"error","error":"later remote error"}\n{"type":"result","result":{"items":[]}}\n');
  f.child.emit('close', 0); await f.reply;
  assert.match(f.state.error?.message || '', /超时/);
  assert.doesNotMatch(f.state.error.message, /later remote error|执行已中断|作业已取消/);
  assert.equal(f.state.value, undefined);
});

for (const phase of ['SIGTERM', 'SIGKILL']) test('runtime execution confirms local exit during '+phase+' cleanup without claiming remote cancellation', async t => {
  const f = await executionFixture(t);
  f.timers[0].fn();
  if (phase === 'SIGKILL') {
    assert.ok(f.timers[1], 'SIGKILL escalation must be scheduled');
    f.timers[1].fn();
  }
  f.child.emit('close', null); await f.reply;
  assert.match(f.state.error?.message || '', /超时.*SSH.*退出已确认/);
  assert.match(f.state.error.message, /远端.*(?:未确认|未知|重新查询)/);
  assert.doesNotMatch(f.state.error.message, /执行已中断|作业已取消/);
  assert.equal(f.state.error.exitUnconfirmed, undefined);
  assert.deepEqual(f.signals, phase === 'SIGTERM' ? ['SIGTERM'] : ['SIGTERM', 'SIGKILL']);
  assert.ok(f.timers.every(timer => timer.cleared));
});

test('a pre-aborted runtime request never starts SSH or writes a runtime packet', async t => {
  const controller = new AbortController(); controller.abort();
  const f = await executionFixture(t, { signal: controller.signal });
  assert.equal(f.spawnCount(), 0);
  await f.reply;
  assert.equal(f.child.input, undefined);
  assert.match(f.state.error?.message || '', /取消/);
  assert.deepEqual(f.signals, []);
  assert.deepEqual(f.timers, []);
});

test('cancellation while preparing a runtime packet prevents a later SSH submission', async t => {
  const controller = new AbortController();
  let prepared;
  const packet = new Promise(resolve => { prepared = resolve; });
  const f = await executionFixture(t, { signal: controller.signal, runtimePacket: () => packet });
  controller.abort(); prepared({ payload: { operation: 'assets' } });
  await f.flush();
  assert.equal(f.spawnCount(), 0);
  await f.reply;
  assert.match(f.state.error?.message || '', /取消/);
  assert.equal(f.child.input, undefined);
});

for (const stream of ['child', 'stdin', 'stdout', 'stderr', 'stdin.end']) test('runtime execution cleans up '+stream+' errors and absorbs later stream failures', async t => {
  const primary = new Error('first '+stream+' failure');
  const f = await executionFixture(t, stream === 'stdin.end' ? { endError: primary } : {});
  if (stream !== 'stdin.end') (stream === 'child' ? f.child : f.child[stream]).emit('error', primary);
  await f.flush();
  assert.deepEqual(f.signals, ['SIGTERM']);
  f.child.emit('close', 7); await f.reply;
  assert.match(f.state.error?.message || '', new RegExp('first '+stream.replace('.', '\\.')+' failure'));
  for (const target of [f.child, f.child.stdin, f.child.stdout, f.child.stderr]) {
    assert.doesNotThrow(() => target.emit('error', new Error('late ignored cleanup error')));
  }
  assert.equal(f.state.value, undefined);
  assert.ok(f.timers.every(timer => timer.cleared));
});

test('runtime execution kills an actual local child that ignores SIGTERM without using a network connection', { timeout: 4000 }, async t => {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const controller = new AbortController(), signals = [];
  let child;
  const context = { runtimePacket: async payload => ({ payload }), quote: value => value,
    bootstrap: 'SYSTEM_VERIFICATION_ONLY no SSH or remote command', config: { remotePython: 'unused' },
    setTimeout, clearTimeout, ssh: () => {
      child = spawn(process.execPath, ['-e', `process.on('SIGTERM',()=>{});
process.stdout.write('{"type":"progress","stage":"local_child_ready"}\\n');
setInterval(()=>{},1000);`], { stdio: ['pipe', 'pipe', 'pipe'] });
      const kill = child.kill.bind(child);
      child.kill = signal => { signals.push(signal); return kill(signal); };
      return child;
    } };
  t.after(() => { if (child && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  const start = source.indexOf('export async function executeRemote('), end = source.indexOf('export function validateArtifactPath(', start);
  vm.runInNewContext(source.slice(start, end).replace('export ', '')+'\nthis.execute=executeRemote;', context);
  const reply = context.execute({ operation: 'assets' }, { signal: controller.signal, timeoutMs: 5000,
    onEvent: event => { if (event.stage === 'local_child_ready') controller.abort(); } });
  await assert.rejects(reply, error => {
    assert.match(error.message, /取消.*SSH.*退出已确认/);
    assert.equal(error.exitUnconfirmed, undefined);
    return true;
  });
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
  assert.equal(child.signalCode, 'SIGKILL');
});

test('trusted execution-device option routes jobs without mutating defaults or trusting payload device', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'sceneagent-device-option-'));
  t.after(()=>rm(root,{recursive:true,force:true}));await mkdir(path.join(root,'runtime'));
  await writeFile(path.join(root,'runtime/worker.py'),'# SYSTEM_VERIFICATION_ONLY\n');
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8');
  const original={index:0,uuid:'GPU-00000000-0000-0000-0000-000000000000'};
  const selected={index:2,uuid:'GPU-22222222-2222-2222-2222-222222222222'};
  const config={remoteRoot:'/data',legacyRoot:'/legacy',remoteGpu:original};
  const context={ROOT:root,config,readdir,readFile,path,createHash};
  vm.runInNewContext(source.slice(source.indexOf('async function runtimePacket('),source.indexOf('export async function executeRemote('))+'\nthis.packet=runtimePacket;',context);
  const payload={operation:'submit',device:{index:99},task:{operation:'rollout',device:{index:98}}};
  const packet=await context.packet(payload,selected);
  assert.deepEqual(JSON.parse(JSON.stringify(packet.payload.device)),selected);
  assert.deepEqual(JSON.parse(JSON.stringify(packet.payload.task.device)),selected);
  assert.equal(config.remoteGpu,original);
  assert.deepEqual(JSON.parse(JSON.stringify((await context.packet(payload)).payload.device)),original);
  assert.equal(Object.hasOwn((await context.packet({operation:'health'},selected)).payload,'device'),false);
  await assert.rejects(context.packet(payload,{index:2,uuid:'invalid'}),/显卡/);
});

test('GPU three admission requires explicit host permission and a memory peak without trusting payload flags', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'sceneagent-gpu-three-option-'));
  t.after(()=>rm(root,{recursive:true,force:true}));await mkdir(path.join(root,'runtime'));
  await writeFile(path.join(root,'runtime/worker.py'),'# SYSTEM_VERIFICATION_ONLY\n');
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8');
  const original={index:0,uuid:'GPU-00000000-0000-0000-0000-000000000000'};
  const selected={index:3,uuid:'GPU-30000000-0000-0000-0000-000000000000'};
  const config={remoteRoot:'/data',legacyRoot:'/legacy',remoteGpu:original};
  const context={ROOT:root,config,readdir,readFile,path,createHash};
  vm.runInNewContext(source.slice(source.indexOf('async function runtimePacket('),source.indexOf('export async function executeRemote('))+'\nthis.packet=runtimePacket;',context);
  const payload={operation:'submit',allowGpu3:true,task:{operation:'rollout',allowGpu3:true}};
  const ordinary=await context.packet(payload,original,7000);
  assert.equal(Object.hasOwn(ordinary.payload,'allowGpu3'),false);
  assert.equal(Object.hasOwn(ordinary.payload.task,'allowGpu3'),false);
  await assert.rejects(context.packet(payload,selected,7000),/卡 0、1、2/);
  await assert.rejects(context.packet(payload,selected,7000,1),/授权/);
  await assert.rejects(context.packet(payload,selected,undefined,true),/峰值/);
  config.remoteGpu=selected;config.remoteGpuPeakMiB=7000;
  await assert.rejects(context.packet(payload,undefined,7000,true),/显式/);
  config.remoteGpu=original;delete config.remoteGpuPeakMiB;
  const authorized=await context.packet(payload,selected,7000,true);
  assert.equal(authorized.payload.allowGpu3,true);
  assert.equal(authorized.payload.task.allowGpu3,true);
  assert.equal(authorized.payload.gpuPeakMiB,7000);
  assert.deepEqual(JSON.parse(JSON.stringify(authorized.payload.task.device)),selected);
  assert.equal(config.remoteGpu,original);
  const cpu=await context.packet({operation:'health',allowGpu3:true},selected,7000,true);
  assert.equal(Object.hasOwn(cpu.payload,'allowGpu3'),false);
});

test('runtime execution passes the trusted GPU three option to packet preparation', async () => {
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8');
  const selected={index:3,uuid:'GPU-30000000-0000-0000-0000-000000000000'};
  const stop=new Error('SYSTEM_VERIFICATION_ONLY stop before SSH');
  let argumentsSeen;
  const context={runtimePacket:async(...args)=>{argumentsSeen=args;throw stop;}};
  vm.runInNewContext(source.slice(source.indexOf('export async function executeRemote('),source.indexOf('export function validateArtifactPath(')).replace('export ','')+'\nthis.execute=executeRemote;',context);
  const payload={operation:'rollout'};
  await assert.rejects(context.execute(payload,{executionDevice:selected,executionGpuPeakMiB:7000,executionAllowGpu3:true}),error=>error===stop);
  assert.equal(argumentsSeen[0],payload);
  assert.equal(argumentsSeen[1],selected);
  assert.equal(argumentsSeen[2],7000);
  assert.equal(argumentsSeen[3],true);
});
