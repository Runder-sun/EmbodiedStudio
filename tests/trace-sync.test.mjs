import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { open, mkdtemp, mkdir, readFile, writeFile, rm, readdir, symlink, lstat, stat, utimes } from 'node:fs/promises';
import { constants, createReadStream } from 'node:fs';
import { Readable, PassThrough } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawn, spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import { fileURLToPath } from 'node:url';
import { quote, validId } from '../src/config.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
async function descriptor(name, file) {
  const bytes = await readFile(file), info = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try { const stat = await info.stat(); return { name, path: file, bytes: bytes.length, sha256: hash(bytes), identity: { dev: stat.dev, ino: stat.ino } }; }
  finally { await info.close(); }
}
async function localTransport(root, options = {}) {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const helper = source.slice(source.indexOf('function singleJsonRemote('), source.indexOf('const privateNetworks'));
  const writer = source.slice(source.indexOf('async function writeProjectFiles('), source.indexOf('export async function consumeRemoteGeneration(')).replaceAll('export ', '');
  const commands = [];
  const context = { Buffer, Uint8Array, Readable, pipeline, open, constants, createReadStream, createHash, validId, quote, path,
    validateArtifactPath: value => { if (!value || value.startsWith('/') || value.split('/').some(part => ['', '.', '..'].includes(part))) throw Error('invalid path'); },
    config: { remoteRoot: root, remotePython: 'python3' }, setTimeout: options.setTimeout || setTimeout, clearTimeout: options.clearTimeout || clearTimeout,
    ssh: command => { commands.push(command); return options.ssh ? options.ssh(command) : spawn('/bin/sh', ['-c', command], { stdio: ['pipe', 'pipe', 'pipe'] }); } };
  vm.runInNewContext(helper + writer + '\nthis.sync=syncTrace;this.run=singleJsonRemote;', context);
  return { sync: context.sync, run: context.run, commands };
}
async function isolated(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-trace-sync-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const local = path.join(root, 'local'), remote = path.join(root, 'remote');
  await mkdir(local); await mkdir(remote);
  return { root, local, remote };
}

async function mirrorFor(directory, traceSync, overrides = {}) {
  const source = await readFile(new URL('../src/server.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('  function mirrorTrace(project)'), end = source.indexOf('\n  const onCodexEvent', start);
  const context = { open, constants, Buffer, readdir, readFile, lstat, path, createHash, contentHash: hash,
    mirrorQueues: new Map(), traceController: new AbortController(), publish() {}, store: { async flush() {}, async save() {}, directory: () => directory }, traceSync, ...overrides };
  vm.runInNewContext(source.slice(start, end) + '\nthis.mirror=mirrorTrace;', context);
  return context.mirror;
}

test('unchanged mirrors skip publication and reuse attachments only while their opened identity and timestamps match', async t => {
  const { local } = await isolated(t), calls = [];
  await mkdir(path.join(local, '.trace/inputs'), { recursive: true });
  const trace = path.join(local, 'trace.jsonl'), attachment = path.join(local, '.trace/inputs/a.json');
  await writeFile(trace, '{}\n'); await writeFile(attachment, 'first');
  let hashedBytes = 0;
  const mirror = await mirrorFor(local, async (id, trace, archive) => {
    calls.push({ trace, archive });
    return { hashes: Object.fromEntries([...archive, trace].map(row => [row.name, row.sha256])) };
  }, { createHash: algorithm => {
    const digest = createHash(algorithm), update = digest.update.bind(digest);
    digest.update = bytes => { hashedBytes += bytes.length; return update(bytes); }; return digest;
  } });
  const project = { id: 'p_test' };
  await mirror(project);
  const before = hashedBytes;
  const unchanged = await mirror(project);
  assert.equal(calls.length, 1, 'an unchanged trace and attachments need no second publication');
  assert.equal(hashedBytes - before, 3, 'only the trace prefix is rehashed');
  assert.equal(unchanged.hash, project.traceSyncedHash);
  const original = await stat(attachment);
  await writeFile(attachment, 'other'); await utimes(attachment, original.atime, original.mtime);
  await mirror(project);
  assert.equal(calls.length, 2, 'a restored mtime does not hide a ctime change');
  assert.equal(calls[1].archive.length, 1);
  assert.equal(calls[1].archive[0].sha256, hash('other'));
  await writeFile(trace, '{"next":1}\n', { flag: 'a' });
  const afterAttachment = hashedBytes;
  await mirror(project);
  assert.equal(calls.length, 3);
  assert.equal(calls[2].archive.length, 0);
  assert.equal(hashedBytes - afterAttachment, Buffer.byteLength('{}\n{"next":1}\n'));
});

test('attachment cache invalidates when an inode is replaced or the path becomes a symlink', async t => {
  const { local } = await isolated(t), calls = [];
  await mkdir(path.join(local, '.trace')); await writeFile(path.join(local, 'trace.jsonl'), '{}\n');
  const attachment = path.join(local, '.trace/input.json'); await writeFile(attachment, 'first');
  const mirror = await mirrorFor(local, async (id, trace, archive) => {
    calls.push(archive);
    return { hashes: Object.fromEntries([...archive, trace].map(row => [row.name, row.sha256])) };
  });
  const project = { id: 'p_test' }; await mirror(project);
  const original = await stat(attachment);
  await rm(attachment); await writeFile(attachment, 'other'); await utimes(attachment, original.atime, original.mtime);
  await mirror(project); assert.equal(calls[1][0].sha256, hash('other'));
  await rm(attachment); await symlink(path.join(local, 'trace.jsonl'), attachment);
  await assert.rejects(mirror(project), /软链接/);
  await rm(attachment); await writeFile(attachment, 'other');
  await mirror(project);
  assert.equal(calls.at(-1).length, 1, 'a failed scan invalidates the prior verification cache');
});

test('mirroring pins a complete-line prefix before collecting attachments during append', async t => {
  const { local, remote } = await isolated(t), transport = await localTransport(remote);
  await mkdir(path.join(local, '.trace/inputs'), { recursive: true });
  await writeFile(path.join(local, '.trace/inputs/a.json'), '{"input":"first"}');
  const first = '{"ref":".trace/inputs/a.json","text":"完整行"}\n';
  await writeFile(path.join(local, 'trace.jsonl'), first + '{"text":"未完');
  const captures = [];
  const mirror = await mirrorFor(local, async (id, trace, archive) => {
    captures.push({ trace, archive });
    if (captures.length === 1) {
      await writeFile(path.join(local, '.trace/inputs/b.json'), '{"input":"second"}');
      await writeFile(path.join(local, 'trace.jsonl'), '成"}\n{"ref":".trace/inputs/b.json"}\n', { flag:'a' });
    }
    return transport.sync(id, trace, archive);
  });
  const project = { id:'p_test' };
  await mirror(project);
  assert.equal((await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'))).toString(), first);
  assert.equal(captures[0].trace.bytes, Buffer.byteLength(first));
  assert.equal(captures[0].archive.length, 1);
  await mirror(project);
  assert.deepEqual(Array.from(captures[1].archive, row=>row.name), ['.trace/inputs/b.json']);
  assert.equal((await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'))).toString(), (await readFile(path.join(local, 'trace.jsonl'))).toString());
});

test('mirroring rejects symlink attachments and blocks later sync after an unconfirmed exit', async t => {
  const { local } = await isolated(t); await writeFile(path.join(local, 'trace.jsonl'), '{}\n');
  await mkdir(path.join(local, '.trace')); await symlink(path.join(local, 'trace.jsonl'), path.join(local, '.trace/link'));
  let calls=0;
  let mirror = await mirrorFor(local, async () => { calls++; });
  await assert.rejects(mirror({id:'p_test'}), /软链接/); assert.equal(calls,0);
  await rm(path.join(local, '.trace/link'));
  await rm(path.join(local, '.trace'),{recursive:true});
  await mkdir(path.join(local,'outside')); await writeFile(path.join(local,'outside/input.json'),'{}');
  await symlink(path.join(local,'outside'),path.join(local,'.trace'));
  await assert.rejects(mirror({id:'p_test'}), /软链接/); assert.equal(calls,0);
  await rm(path.join(local,'.trace')); await mkdir(path.join(local,'.trace'));
  mirror = await mirrorFor(local, async () => { calls++; throw Object.assign(new Error('SSH进程退出未确认'),{exitUnconfirmed:true}); });
  const project={id:'p_test'};
  await assert.rejects(mirror(project), /未确认/);
  await assert.rejects(mirror(project), /未确认/);
  assert.equal(calls,1,'an unconfirmed SSH process must prevent a following sync');
});

test('studio lifetime cancellation prevents queued and later mirrors from opening another transport', async t => {
  const { local } = await isolated(t); await writeFile(path.join(local, 'trace.jsonl'), '{}\n');
  const traceController = new AbortController();
  let calls = 0, entered, release;
  const started = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const mirror = await mirrorFor(local, async (_id, _trace, _archive, { signal }) => {
    calls++; entered(); await gate;
    if (signal.aborted) throw signal.reason;
  }, { traceController });
  const first = mirror({ id:'p_test' }); first.catch(() => {}); await started;
  const queued = mirror({ id:'p_test' }); queued.catch(() => {});
  traceController.abort(new Error('fixture studio closed')); release();
  await assert.rejects(first, /studio closed/); await assert.rejects(queued, /studio closed/);
  await assert.rejects(mirror({ id:'p_test' }), /studio closed/);
  assert.equal(calls, 1);
});

test('a remote hash failure removes its temporary file before any rename', async t => {
  const { local, remote } = await isolated(t), { sync, commands } = await localTransport(remote);
  const trace = path.join(local,'trace.jsonl'); await writeFile(trace,'first\n');
  const selected=await descriptor('generation-trace.jsonl',trace); await sync('p_test',selected,[]);
  const header=JSON.stringify({root:remote,projectId:'p_test',name:selected.name,bytes:6,sha256:selected.sha256,immutable:false});
  const child=spawnSync('/bin/sh',['-c',commands.at(-1)],{input:header+'\nother\n',encoding:'utf8'});
  assert.notEqual(child.status,0); assert.match(child.stderr,/hash differs/);
  const published=path.join(remote,'projects/p_test');
  assert.equal((await readFile(path.join(published,selected.name))).toString(),'first\n');
  assert.deepEqual(await readdir(published),['generation-trace.jsonl']);
});

test('source truncation, missing files, symlinks and a replaced FIFO are explicit read failures', { timeout:3000 }, async t => {
  const { local, remote } = await isolated(t), { sync } = await localTransport(remote);
  const trace=path.join(local,'trace.jsonl'); await writeFile(trace,'original\n');
  const selected=await descriptor('generation-trace.jsonl',trace);
  await writeFile(trace,'x'); await assert.rejects(sync('p_test',selected,[]),/固定范围/);
  await rm(trace); await assert.rejects(sync('p_test',selected,[]),/ENOENT/);
  await writeFile(path.join(local,'outside'),'original\n'); await symlink(path.join(local,'outside'),trace);
  await assert.rejects(sync('p_test',selected,[]),/ELOOP|symbolic|links/i);
  await rm(trace); assert.equal(spawnSync('mkfifo',[trace]).status,0);
  await assert.rejects(sync('p_test',selected,[]),/身份/);
});

test('streaming transport destroys its source and stdio on early zero exit or timeout', async () => {
  const source=await readFile(new URL('../src/remote.mjs',import.meta.url),'utf8');
  const helper=source.slice(source.indexOf('function singleJsonRemote('),source.indexOf('const privateNetworks'));
  for (const mode of ['early','timeout']) {
    const child=new EventEmitter(); child.stdin=new PassThrough();child.stdout=new PassThrough();child.stderr=new PassThrough();
    child.kill=()=>setImmediate(()=>child.emit('close',143));
    const timers=[];
    const context={Buffer,pipeline,ssh:()=>child,setTimeout:(fn,ms)=>{timers.push({fn,ms});return fn;},clearTimeout(){}};
    vm.runInNewContext(helper+'\nthis.run=singleJsonRemote;',context);
    const input=new Readable({read(){this.push(Buffer.alloc(65536));}});
    const pending=context.run('unused',input,{timeoutMs:100,maxBytes:1000,label:'fixture stream'});
    if(mode==='early') {child.stdout.write('{"saved":true}');child.emit('close',0);} else timers[0].fn();
    await assert.rejects(pending,/尚未完成|超时/);
    assert.equal(input.destroyed,true); assert.equal(child.stdin.destroyed,true);
    assert.equal(child.stdout.destroyed,true); assert.equal(child.stderr.destroyed,true);
  }
});

async function progressFixture(t, totalDeadline = false, signal) {
  const { local, remote } = await isolated(t);
  const trace = path.join(local, 'trace.jsonl'); await writeFile(trace, '1234');
  const selected = await descriptor('generation-trace.jsonl', trace);
  const child = new EventEmitter(); child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.stdin.resume(); child.kill = () => queueMicrotask(() => child.emit('close', 143));
  let now = 0; const timers = new Set();
  const clock = { setTimeout(fn, ms) { const timer = { fn, at: now + ms }; timers.add(timer); return timer; },
    clearTimeout(timer) { timers.delete(timer); } };
  const { sync, run } = await localTransport(remote, { ssh: command => command.includes("print(json.dumps(result))")
    ? spawn('/bin/sh', ['-c', command], { stdio:['pipe','pipe','pipe'] }) : child, ...clock });
  const reply = totalDeadline ? run('unused', Readable.from(['1234']), { timeoutMs:300000, maxBytes:65536, label:'fixture total' })
    : sync('p_test', selected, [], { signal }); reply.catch(() => {});
  await new Promise(resolve => child.stdin.once('finish', resolve));
  await new Promise(resolve => setImmediate(resolve));
  return { child, reply, selected, async advance(ms) {
    now += ms;
    for (const timer of [...timers]) if (timer.at <= now && timers.delete(timer)) timer.fn();
    await new Promise(resolve => setImmediate(resolve));
  }, finish() {
    child.stdout.write(JSON.stringify({ saved:true, hashes:{ [selected.name]:selected.sha256 } })); child.emit('close', 0);
  } };
}

test('trace transfer accepts actual progress beyond the former total deadline, including split and coalesced frames', async t => {
  const f = await progressFixture(t);
  await f.advance(200000); f.child.stderr.write('TRACE_SYNC_PROGRESS 1\nTRACE_SYNC_PRO');
  await f.advance(200000); f.child.stderr.write('GRESS 2\nTRACE_SYNC_PROGRESS 3\n');
  await f.advance(200000); f.child.stderr.write('TRACE_SYNC_PROGRESS 4\n');
  f.finish(); assert.equal((await f.reply).saved, true);
});

test('trace transfer stops on no progress and after final bytes without remote exit', async t => {
  for (const frame of ['', 'TRACE_SYNC_PROGRESS 4\n']) {
    const f = await progressFixture(t);
    await f.advance(100000); if (frame) f.child.stderr.write(frame);
    await f.advance(300000);
    await assert.rejects(f.reply, /超时/);
    assert.equal(f.child.stdin.destroyed, true);
  }
});

test('invalid or duplicate progress and an oversized line cannot prolong trace transfer', async t => {
  for (const frame of ['TRACE_SYNC_PROGRESS 0\n', 'TRACE_SYNC_PROGRESS 1\n', 'TRACE_SYNC_PROGRESS 5\n',
    'TRACE_SYNC_PROGRESS -1\n', 'TRACE_SYNC_PROGRESS 2.0\n', 'TRACE_SYNC_PROGRESS 2e0\n',
    'TRACE_SYNC_PROGRESS 02\n', 'ordinary stderr\n', 'x'.repeat(10000) + 'TRACE_SYNC_PROGRESS 2\n']) {
    const f = await progressFixture(t);
    await f.advance(100000); f.child.stderr.write('TRACE_SYNC_PROGRESS 1\n');
    await f.advance(200000); f.child.stderr.write(frame);
    await f.advance(100000);
    await assert.rejects(f.reply, /超时/, frame.slice(-70));
  }
});

test('backward progress and total-deadline callers retain their original deadline', async t => {
  for (const totalDeadline of [false, true]) {
    const f = await progressFixture(t, totalDeadline);
    await f.advance(100000); f.child.stderr.write('TRACE_SYNC_PROGRESS 3\n');
    await f.advance(150000); f.child.stderr.write('TRACE_SYNC_PROGRESS 2\n');
    await f.advance(150000);
    await assert.rejects(f.reply, /超时/);
  }
});

test('trace lifetime cancellation destroys streams and confirms the SSH exit', async t => {
  const controller = new AbortController(), f = await progressFixture(t, false, controller.signal);
  controller.abort(new Error('fixture trace cancelled'));
  await assert.rejects(f.reply, /fixture trace cancelled/);
  assert.equal(f.child.stdin.destroyed, true); assert.equal(f.child.stdout.destroyed, true); assert.equal(f.child.stderr.destroyed, true);
});

test('trace sync streams fixed UTF-8 bytes and attachments before atomic publication', async t => {
  const { local, remote } = await isolated(t), { sync } = await localTransport(remote);
  const trace = path.join(local, 'trace.jsonl'), attachment = path.join(local, 'input.json');
  await writeFile(trace, '{"text":"完整原始要求"}\n'); await writeFile(attachment, Buffer.from([0, 255, 9]));
  const selected = await descriptor('generation-trace.jsonl', trace);
  const archive = await descriptor('.trace/inputs/input.json', attachment);
  await writeFile(trace, '{"future":"不能进入固定快照"}\n', { flag: 'a' });
  const result = await sync('p_test', selected, [archive]);
  assert.equal(result.saved, true);
  assert.equal(result.hashes['generation-trace.jsonl'], selected.sha256);
  assert.equal(result.hashes[archive.name], archive.sha256);
  assert.equal((await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'))).toString(), '{"text":"完整原始要求"}\n');
  assert.deepEqual(await readFile(path.join(remote, 'projects/p_test', archive.name)), Buffer.from([0, 255, 9]));
});

test('a changed source cannot replace the published trace and leaves no temporary file', async t => {
  const { local, remote } = await isolated(t), { sync } = await localTransport(remote);
  const trace = path.join(local, 'trace.jsonl'); await writeFile(trace, 'first\n');
  await sync('p_test', await descriptor('generation-trace.jsonl', trace), []);
  const selected = await descriptor('generation-trace.jsonl', trace);
  await writeFile(trace, 'other\n');
  await assert.rejects(sync('p_test', selected, []), /hash|摘要|differs|transfer/);
  assert.equal((await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'))).toString(), 'first\n');
  assert.deepEqual(await readdir(path.join(remote, 'projects/p_test')), ['generation-trace.jsonl']);
});

test('an attachment failure cannot publish a newer trace; empty files remain valid', async t => {
  const { local, remote } = await isolated(t), { sync, commands } = await localTransport(remote);
  const trace = path.join(local, 'trace.jsonl'), asset = path.join(local, 'empty');
  await writeFile(trace, 'old\n'); await writeFile(asset, '');
  await sync('p_test', await descriptor('generation-trace.jsonl', trace), []);
  await writeFile(trace, 'new\n');
  const attachment = await descriptor('.trace/media/empty', asset);
  const invalid = { ...attachment, sha256: 'f'.repeat(64) };
  const before = commands.length;
  await assert.rejects(sync('p_test', await descriptor('generation-trace.jsonl', trace), [invalid]), /hash|摘要|differs|transfer/);
  assert.equal(commands.length, before + 1, 'a failed first attachment must prevent trace submission');
  assert.equal((await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'))).toString(), 'old\n');
  const done = await sync('p_test', await descriptor('generation-trace.jsonl', trace), [attachment]);
  assert.equal(done.hashes[attachment.name], hash(Buffer.alloc(0)));
});

test('actual mirror and stream writer sync a 192MiB UTF-8 trace and 96MiB attachment under a 96MiB heap', { timeout:60000 }, async t => {
  const { root, local, remote } = await isolated(t);
  await mkdir(path.join(local,'.trace/media'),{recursive:true});
  const trace=path.join(local,'trace.jsonl'), asset=path.join(local,'.trace/media/large.bin');
  const line=JSON.stringify({type:'isolated_system_verification',text:'完整UTF8记录'.repeat(100)})+'\n';
  const chunk=Buffer.from(line.repeat(64));
  const writer=await open(trace,'w');let traceBytes=0;
  try {while(traceBytes<192*1024*1024){await writer.write(chunk);traceBytes+=chunk.length;}} finally {await writer.close();}
  const attachment=await open(asset,'w'), block=Buffer.alloc(65536,17);
  try {for(let size=0;size<96*1024*1024;size+=block.length) await attachment.write(block);} finally {await attachment.close();}
  const repository=process.env.SCENEAGENT_TRACE_SOURCE_ROOT || fileURLToPath(new URL('..',import.meta.url));
  const script=`import {readFile,readdir,open,lstat} from 'node:fs/promises';
import {constants} from 'node:fs';import {createHash} from 'node:crypto';
import {Readable} from 'node:stream';import {pipeline} from 'node:stream/promises';
import {spawn} from 'node:child_process';import path from 'node:path';import vm from 'node:vm';
const [repository,local,remote]=process.argv.slice(2);
const remoteSource=await readFile(path.join(repository,'src/remote.mjs'),'utf8');
const helper=remoteSource.slice(remoteSource.indexOf('function singleJsonRemote('),remoteSource.indexOf('const privateNetworks'));
const writer=remoteSource.slice(remoteSource.indexOf('async function writeProjectFiles('),remoteSource.indexOf('export async function consumeRemoteGeneration(')).replaceAll('export ','');
const quote=value=>"'"+String(value).replaceAll("'","'\\\\''")+"'";
const transport={Buffer,Uint8Array,Readable,pipeline,open,constants,createHash,path,setTimeout,clearTimeout,
 config:{remoteRoot:remote,remotePython:'python3'},validId:()=>true,validateArtifactPath:()=>{},quote,
 ssh:command=>spawn('/bin/sh',['-c',command],{stdio:['pipe','pipe','pipe']})};
vm.runInNewContext(helper+writer+'\\nthis.sync=syncTrace;',transport);
const serverSource=await readFile(path.join(repository,'src/server.mjs'),'utf8');
const start=serverSource.indexOf('  function mirrorTrace(project)'),end=serverSource.indexOf('\\n  const onCodexEvent',start);
const context={Buffer,open,constants,readdir,readFile,lstat,path,createHash,contentHash:value=>createHash('sha256').update(value).digest('hex'),
 mirrorQueues:new Map(),traceController:new AbortController(),publish(){},store:{async flush(){},async save(){},directory:()=>local},traceSync:transport.sync};
vm.runInNewContext(serverSource.slice(start,end)+'\\nthis.mirror=mirrorTrace;',context);
const project={id:'p_test'};await context.mirror(project);
console.log(JSON.stringify({hash:project.traceSyncedHash,heapUsed:process.memoryUsage().heapUsed,maxRSSKiB:process.resourceUsage().maxRSS}));`;
  const code=path.join(root,'small-heap.mjs');await writeFile(code,script);
  const child=spawnSync(process.execPath,['--max-old-space-size=96',code,repository,local,remote],{encoding:'utf8',timeout:55000,maxBuffer:65536});
  assert.equal(child.status,0,JSON.stringify({signal:child.signal,error:child.error?.message,stderr:child.stderr?.slice(-5000)}));
  const receipt=JSON.parse(child.stdout);
  const saved=await open(path.join(remote,'projects/p_test/generation-trace.jsonl'));
  try {assert.equal((await saved.stat()).size,traceBytes);} finally {await saved.close();}
  const savedAsset=path.join(remote,'projects/p_test/.trace/media/large.bin');
  assert.equal((await open(savedAsset).then(async file=>{try{return await file.stat();}finally{await file.close();}})).size,96*1024*1024);
  assert.ok(receipt.heapUsed<96*1024*1024);
  t.diagnostic(JSON.stringify({traceBytes,attachmentBytes:96*1024*1024,...receipt}));
});

test('trace append transfers only a verified remote prefix suffix and replaces it atomically', async t => {
  const { local, remote } = await isolated(t);
  let transferred = 0;
  const transport = await localTransport(remote, { ssh: command => {
    const child = spawn('/bin/sh', ['-c', command], { stdio: ['pipe', 'pipe', 'pipe'] });
    const write = child.stdin.write.bind(child.stdin);
    child.stdin.write = (data, ...args) => { transferred += Buffer.byteLength(data); return write(data, ...args); };
    return child;
  } });
  const trace = path.join(local, 'trace.jsonl'), first = '{"record":"preserved"}\n'.repeat(10000);
  await writeFile(trace, first);
  await transport.sync('p_test', await descriptor('generation-trace.jsonl', trace));
  await writeFile(trace, '{"new":true}\n', { flag: 'a' });
  transferred = 0;
  const selected = await descriptor('generation-trace.jsonl', trace);
  const receipt = await transport.sync('p_test', selected);
  assert.equal(receipt.hashes[selected.name], selected.sha256);
  assert.equal(await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'), 'utf8'), first+'{"new":true}\n');
  assert.ok(transferred < 4096, 'an unchanged 220KB prefix must not be uploaded again: '+transferred);
  await writeFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'), 'unrelated\n');
  transferred = 0;
  await transport.sync('p_test', selected);
  assert.ok(transferred > first.length, 'a different prefix requires full transfer');
  assert.equal(await readFile(path.join(remote, 'projects/p_test/generation-trace.jsonl'), 'utf8'), first+'{"new":true}\n');
});

test('trace prefix replacement after verification cannot publish the appended trace', async t => {
  const { local, remote } = await isolated(t), { sync, commands } = await localTransport(remote);
  const trace = path.join(local, 'trace.jsonl'); await writeFile(trace, 'first\n');
  const before = await descriptor('generation-trace.jsonl', trace); await sync('p_test', before);
  const target = path.join(remote, 'projects/p_test/generation-trace.jsonl');
  const header = JSON.stringify({ root:remote, projectId:'p_test', name:before.name,
    bytes:11, sha256:hash('first\nnext\n'), immutable:false, prefixBytes:6, prefixSha256:before.sha256 });
  for (const altered of ['other\n', 'short']) {
    await writeFile(target, altered);
    const result = spawnSync('/bin/sh', ['-c', commands.at(-1)], { input:header+'\nnext\n', encoding:'utf8' });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /trace prefix/);
    assert.equal(await readFile(target, 'utf8'), altered);
    assert.deepEqual(await readdir(path.dirname(target)), ['generation-trace.jsonl']);
  }
});
