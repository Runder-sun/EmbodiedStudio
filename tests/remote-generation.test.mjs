import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile, rm, open, symlink } from 'node:fs/promises';
import { constants } from 'node:fs';
import { Readable, PassThrough } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

async function fixture() {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('export async function consumeRemoteGeneration(');
  assert.ok(start >= 0);
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.end = value => { child.input = JSON.parse(value); };
  child.kill = () => { child.killed = true; queueMicrotask(() => child.emit('close', null)); };
  const timers = [], cleared = [];
  const context = { Buffer, ssh: () => child, quote: value => value, config: { remotePython: 'python3', remoteRoot: '/data' },
    validId: () => true, setTimeout: (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; },
    clearTimeout: timer => cleared.push(timer) };
  const helperAt = source.indexOf('function singleJsonRemote(');
  const helper = helperAt < 0 ? '' : source.slice(helperAt, source.indexOf('const privateNetworks'));
  vm.runInNewContext(helper + source.slice(start).replace('export ', '') + '\nthis.consume=consumeRemoteGeneration;', context);
  const reply = context.consume('p_test', 'g_test', 'actual frozen consumer source');
  return { child, timer: timers[0], timers, cleared, reply };
}

test('the actual generation transport accepts a valid reply after the observed 55.28 second workload', async () => {
  const { child, timer, reply } = await fixture();
  if (timer.ms <= 55281) timer.fn();
  else { child.stdout.emit('data', JSON.stringify({ passed: true, sampleCount: 168, agentTrainingSampleCount: 160,
    traceHash: 'trace', consumerDigest: 'source', artifactRef: 'projects/p_test/report.json' })); child.emit('close', 0); }
  const result = await reply;
  assert.equal(result.passed, true); assert.equal(result.sampleCount, 168);
  assert.equal(result.agentTrainingSampleCount, 160); assert.equal(child.killed, undefined);
  assert.equal(child.input.source, 'actual frozen consumer source');
});

test('the actual generation transport still terminates an unresponsive process at the existing long-job bound', async () => {
  const { child, timer, reply } = await fixture();
  assert.ok(timer.ms <= 300000);
  timer.fn();
  await assert.rejects(reply, /生成轨迹消费超时/);
  assert.equal(child.killed, true);
});

test('the actual generation transport preserves a failed child report', async () => {
  const { child, reply } = await fixture();
  child.stderr.emit('data', 'actual generation consumer failed'); child.emit('close', 7);
  await assert.rejects(reply, /actual generation consumer failed/);
});

test('single JSON transport retains the exit code or signal when SSH produces no diagnostic text', async () => {
  for (const [code, signal, expected] of [[255, null, /exitCode=255/], [null, 'SIGKILL', /signal=SIGKILL/]]) {
    const { child, reply } = await fixture();
    child.emit('close', code, signal);
    await assert.rejects(reply, expected);
  }
});

test('single JSON transport bounds a successful-looking oversized reply', async () => {
  const { child, reply } = await fixture();
  child.stdout.emit('data', JSON.stringify({ passed: true, padding: 'x'.repeat(8 * 1024 * 1024) }));
  child.emit('close', 0);
  await assert.rejects(reply, /输出|字节|预算/);
  assert.equal(child.killed, true);
});

test('single JSON transport waits for child exit after broken input and preserves the primary error', async () => {
  const { child, reply, cleared, timer } = await fixture();
  child.kill = () => { child.killed = true; };
  let settled = false;
  reply.catch(() => { settled = true; });
  child.stdin.emit('error', new Error('actual broken input'));
  await Promise.resolve();
  assert.equal(settled, false, 'failure returned before the created SSH child closed');
  assert.equal(child.killed, true);
  child.emit('close', 7);
  await assert.rejects(reply, /actual broken input/);
  assert.ok(cleared.includes(timer));
});

test('single JSON transport rejects malformed JSON only after a normal child exit', async () => {
  const { child, reply } = await fixture();
  child.stdout.emit('data', '{actual invalid JSON'); child.emit('close', 0);
  await assert.rejects(reply, /JSON/);
});

test('single JSON transport escalates termination and reports an unconfirmed exit within its cleanup bound', async () => {
  const { child, reply, timers } = await fixture();
  const signals = [];
  child.kill = signal => { signals.push(signal); };
  child.stdin.emit('error', new Error('primary input failure'));
  timers[1].fn(); timers[2].fn();
  await assert.rejects(reply, /primary input failure.*退出未确认/);
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
  assert.equal(timers[1].ms + timers[2].ms, 2000);
});

test('single JSON transport handles spawn and stream errors through the same exit cleanup', async () => {
  for (const stream of ['child', 'stdout', 'stderr']) {
    const { child, reply } = await fixture();
    (stream === 'child' ? child : child[stream]).emit('error', new Error('actual ' + stream + ' failure'));
    await assert.rejects(reply, new RegExp('actual ' + stream + ' failure'));
    assert.equal(child.killed, true);
  }
});


test('record sync keeps fixed original bytes and hashes through the compressed transport', async t => {
  const directory = await mkdtemp(path.join(tmpdir(), 'compressed-trace-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const sshCode = source.slice(source.indexOf('function ssh(command)'), source.indexOf('const privateNetworks'));
  const writerCode = source.slice(source.indexOf('async function writeProjectFiles('), source.indexOf('export async function consumeRemoteGeneration(')).replaceAll('export ', '');
  const sent = [];
  const context = { Buffer, validId: () => true, validateArtifactPath: () => {}, quote: value => value,
    open, constants, Readable, pipeline, createHash, path, setTimeout, clearTimeout,
    config: { remoteHost: 'fixture-host', remoteRoot: '/data', remotePython: 'python3' },
    spawn: (program, args, options) => {
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new PassThrough();
      const chunks = [];
      child.stdin.on('data', chunk => chunks.push(chunk));
      child.stdin.on('end', () => setImmediate(() => {
        const bytes = Buffer.concat(chunks), newline = bytes.indexOf(10);
        if (args.at(-1).includes('print(json.dumps(result))')) {
          child.stdout.emit('data', '{}'); child.emit('close', 0); return;
        }
        const payload = JSON.parse(bytes.subarray(0, newline));
        sent.push({ program, args, options, payload, bytes: bytes.subarray(newline + 1) });
        if (!args.includes('-C')) { child.stderr.emit('data', 'uncompressed transport did not complete'); child.emit('close', 7); return; }
        child.stdout.emit('data', JSON.stringify({ saved: true,
          hashes: { [payload.name]: createHash('sha256').update(bytes.subarray(newline + 1)).digest('hex') } }));
        child.emit('close', 0);
      }));
      return child;
    } };
  vm.runInNewContext(sshCode + writerCode + '\nthis.sync=syncTrace;', context);
  const values = [{ name:'.trace/inputs/input.json', value:'{"text":"完整原始要求"}' },
    { name:'generation-trace.jsonl', value:'原始公开记录\n' + '0123456789'.repeat(100000) }];
  const files=[];
  for (const [index, row] of values.entries()) {
    const file=path.join(directory, String(index)); await writeFile(file, row.value);
    const handle=await open(file); const stat=await handle.stat(); await handle.close();
    files.push({name:row.name,path:file,bytes:Buffer.byteLength(row.value),sha256:createHash('sha256').update(row.value).digest('hex'),identity:{dev:stat.dev,ino:stat.ino}});
  }
  const result = await context.sync('p_test', files[1], [files[0]]);
  assert.equal(result.saved, true); assert.equal(sent.length, 2);
  for (const [index, row] of sent.entries()) {
    assert.equal(row.bytes.toString(), values[index].value);
    assert.equal(row.payload.root, '/data'); assert.equal(row.payload.projectId, 'p_test');
    assert.equal(row.payload.immutable, index === 0);
    assert.equal(row.program, 'ssh'); assert.equal(row.args.at(-2), 'fixture-host');
    assert.deepEqual(Array.from(row.options.stdio), ['pipe', 'pipe', 'pipe']);
    assert.equal(result.hashes[row.payload.name], files[index].sha256);
  }
});

async function runPublication(consumerSource, rows) {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-consumer-transport-'));
  const project = path.join(root, 'projects', 'p_test');
  await mkdir(project, { recursive: true });
  await writeFile(path.join(project, 'generation-trace.jsonl'), rows.map(row => JSON.stringify(row)).join('\n')+'\n');
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('export async function consumeRemoteGeneration(');
  let command;
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter();
  child.stdin.end = value => { child.input = value; };
  const context = { ssh: value => { command = value; return child; }, quote: value => value,
    Buffer,
    config: { remotePython: 'python3', remoteRoot: root }, validId: () => true,
    setTimeout: () => 1, clearTimeout: () => {} };
  const helper = source.slice(source.indexOf('function singleJsonRemote('), source.indexOf('const privateNetworks'));
  vm.runInNewContext(helper + source.slice(start).replace('export ', '')+'\nthis.consume=consumeRemoteGeneration;', context);
  const reply = context.consume('p_test', 'g_test', consumerSource);
  const result = spawnSync('python3', ['-B', '-c', command.split(' -u -c ')[1]], { input: child.input, encoding: 'utf8' });
  child.stdout.emit('data', result.stdout); child.stderr.emit('data', result.stderr); child.emit('close', result.status);
  return { root, project, reply, exitCode: result.status };
}

test('publication freezes a v2 reference report and returns only small metadata without expanding event contents', async t => {
  const consumer = await readFile(new URL('../examples/read_generation.py', import.meta.url), 'utf8');
  const rows = [
    { type: 'user_message', eventId: 'p_test:1', generationId: 'g_test', message: 'native input '.repeat(20000) },
    { type: 'tool_call', eventId: 'p_test:2', generationId: 'g_test', actor: 'codex-mcp', name: 'job', args: { jobId: 'job-1' } },
    { type: 'tool_result', eventId: 'p_test:3', generationId: 'g_test', name: 'job', callEventId: 'p_test:2', result: { jobId: 'job-1' } },
    { type: 'operation_failed', eventId: 'p_test:4', generationId: 'g_test', jobId: 'job-1', error: 'full failure '.repeat(20000) },
  ];
  const { root, project, reply, exitCode } = await runPublication(consumer, rows);
  t.after(() => rm(root, { recursive: true, force: true }));
  const summary = await reply;
  assert.equal(exitCode, 0); assert.equal(summary.schema, 'sceneagent-generation-consumer-v2'); assert.equal(summary.version, 2);
  assert.equal(summary.projectId, 'p_test'); assert.equal(summary.generationId, 'g_test'); assert.equal(summary.passed, true);
  assert.equal(summary.samples, undefined); assert.equal(summary.publicInputs, undefined); assert.equal(summary.outcomes, undefined);
  assert.ok(JSON.stringify(summary).length < 3000);
  const report = JSON.parse(await readFile(path.join(root, summary.artifactRef), 'utf8'));
  assert.deepEqual(report.samples[0].outcomeRefs, ['p_test:4']);
  assert.deepEqual(report.samples[0].publicInputRefs, ['p_test:1']);
  assert.equal(report.samples[0].publicInputs, undefined); assert.equal(report.samples[0].outcomes, undefined);
  assert.equal(summary.consumerDigest, createHash('sha256').update(consumer).digest('hex'));
  const frozen = await readFile(path.join(root, summary.traceRef));
  assert.equal(createHash('sha256').update(frozen).digest('hex'), summary.traceHash);
  const identity = spawnSync('python3', ['-B', '-c',
    "import json,runpy,sys;s=runpy.run_path(sys.argv[1]);ref,digest,raw=s['freeze_trace'](sys.argv[2]);print(json.dumps([ref,digest]))",
    fileURLToPath(new URL('../examples/read_generation.py', import.meta.url)), project], { encoding: 'utf8' });
  assert.equal(identity.status, 0, identity.stderr);
  assert.deepEqual(JSON.parse(identity.stdout), [summary.traceRef.replace('projects/p_test/', ''), summary.traceHash]);
  await writeFile(path.join(project, 'generation-trace.jsonl'), 'new live trace');
  assert.deepEqual(await readFile(path.join(root, summary.traceRef)), frozen);
});

test('publication rejects a mismatched schema or scope instead of certifying it by overwriting its metadata', async t => {
  const rows = [{ type: 'user_message', eventId: 'p_test:1', generationId: 'g_test', message: 'bounded input' }];
  const original = await readFile(new URL('../examples/read_generation.py', import.meta.url), 'utf8');
  for (const fields of ["schema='wrong',version=2,projectId='p_test',generationId='g_test'",
    "schema='sceneagent-generation-consumer-v2',version=2,projectId='p_other',generationId='g_test'",
    "schema='sceneagent-generation-consumer-v2',version=2,projectId='p_test',generationId='g_other'"]) {
    const consumer = original + `\ndef consume(project,generation_id,trace_ref):\n    return dict(${fields},traceHash=hashlib.sha256((project/trace_ref).read_bytes()).hexdigest(),samples=[])\n`;
    const { root, reply, exitCode } = await runPublication(consumer, rows);
    t.after(() => rm(root, { recursive: true, force: true }));
    await assert.rejects(reply, /schema|scope/);
    assert.notEqual(exitCode, 0);
  }
});

test('publication independently rejects a shared freezer that returns an active alias or wrong byte digest', async t => {
  const rows = [{ type: 'user_message', eventId: 'p_test:1', generationId: 'g_test', message: 'original input' }];
  const original = await readFile(new URL('../examples/read_generation.py', import.meta.url), 'utf8');
  for (const returned of ["'generation-trace.jsonl',digest,raw", "ref,'0'*64,raw"]) {
    const consumer = original + `\noriginal_freeze=freeze_trace\ndef freeze_trace(project,trace_ref='generation-trace.jsonl'):\n    ref,digest,raw=original_freeze(project,trace_ref)\n    return ${returned}\n`;
    const { root, reply, exitCode } = await runPublication(consumer, rows);
    t.after(() => rm(root, { recursive: true, force: true }));
    await assert.rejects(reply, /scope|hash/);
    assert.notEqual(exitCode, 0);
  }
});

test('existing immutable project writer preserves binary archive bytes and the prior JSON format', async () => {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('async function writeProjectFiles(');
  const end = source.indexOf('export async function consumeRemoteGeneration(');
  const sent = [];
  const context = { Buffer, Uint8Array, validId: () => true, validateArtifactPath: () => {}, quote: value => value,
    setTimeout, clearTimeout,
    config: { remoteRoot: '/data', remotePython: 'python3' }, ssh: () => {
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter();
      child.stdin.end = value => { sent.push(JSON.parse(value)); queueMicrotask(() => {
        child.stdout.emit('data', JSON.stringify({ saved: true })); child.emit('close', 0);
      }); };
      return child;
    } };
  const helper = source.slice(source.indexOf('function singleJsonRemote('), source.indexOf('const privateNetworks'));
  vm.runInNewContext(helper + source.slice(start, end).replaceAll('export ', '')+'\nthis.write=writeRemoteProjectFile;', context);
  const image = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0, 255, 2]);
  await context.write('p_test', '.trace/media/image.png', image, { immutable: true });
  await context.write('p_test', '.trace/media/typed.png', new Uint8Array(image), { immutable: true });
  const metadata = { text: '原始审核内容', complete: true };
  await context.write('p_test', '.trace/review.json', metadata, { immutable: true });
  assert.deepEqual(Buffer.from(sent[0].files['.trace/media/image.png'], 'base64'), image);
  assert.deepEqual(Buffer.from(sent[1].files['.trace/media/typed.png'], 'base64'), image);
  assert.equal(Buffer.from(sent[2].files['.trace/review.json'], 'base64').toString(), JSON.stringify(metadata, null, 2));
  for (const payload of sent) { assert.equal(payload.immutable, true); assert.equal(payload.projectId, 'p_test'); }
});

test('actual Python scope check rejects symlink escape and preserves unscoped range reads', async t => {
  const reference = 'projects/p_test/episodes/ep/trajectory.json';
  const root = await mkdtemp(path.join(tmpdir(), 'review-artifact-scope-')); t.after(() => rm(root, { recursive: true, force: true }));
  const episode = path.join(root, 'projects/p_test/episodes/ep'); await mkdir(episode, { recursive: true });
  const own = path.join(episode, 'real.json'); await writeFile(own, '0123456789');
  const other = path.join(root, 'projects/p_other/episodes/ep'); await mkdir(other, { recursive: true }); await writeFile(path.join(other, 'trajectory.json'), 'other project');
  await symlink(own, path.join(episode, 'manifest.json')); await symlink(path.join(other, 'trajectory.json'), path.join(episode, 'trajectory.json'));
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const script = source.slice(source.indexOf('const fileScript = `') + 'const fileScript = `'.length, source.indexOf('`;', source.indexOf('const fileScript = `')));
  const run = payload => spawnSync('python3', ['-c', script, JSON.stringify({ root, op: 'read', ...payload })]);
  assert.equal(run({ path: reference, scope: 'projects/p_test/episodes/ep' }).status, 1);
  assert.equal(run({ path: reference.replace('trajectory', 'manifest'), scope: 'projects/p_test/episodes/ep' }).stdout.toString(), '0123456789');
  assert.equal(run({ path: 'projects/p_test/episodes/ep/real.json', start: 2, length: 3 }).stdout.toString(), '234');
  await symlink(other, path.join(root, 'projects/p_test/episodes/linked'));
  assert.equal(run({ path: 'projects/p_test/episodes/linked/trajectory.json', scope: 'projects/p_test/episodes/linked' }).status, 1);
  await symlink(path.join(root, 'projects/p_other'), path.join(root, 'projects/p_linked'));
  assert.equal(run({ path: 'projects/p_linked/episodes/ep/trajectory.json', scope: 'projects/p_linked/episodes/ep' }).status, 1);
  const otherEpisode = path.join(root, 'projects/p_test/episodes/other'); await mkdir(otherEpisode);
  await symlink(own, path.join(otherEpisode, 'trajectory.json'));
  assert.equal(run({ path: 'projects/p_test/episodes/other/trajectory.json', scope: 'projects/p_test/episodes/other' }).status, 1);
  const outsideRoot = await mkdtemp(path.join(tmpdir(), 'review-artifact-outside-')); t.after(() => rm(outsideRoot, { recursive: true, force: true }));
  const outside = path.join(outsideRoot, 'trajectory.json'); await writeFile(outside, 'outside data root');
  await symlink(outside, path.join(episode, 'outside'));
  assert.equal(run({ path: 'projects/p_test/episodes/ep/outside', scope: 'projects/p_test/episodes/ep' }).status, 1);
});



test('produced format transport binds selected scope and actual episode revisions while preserving the legacy wire', async () => {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('export async function consumeProducedArtifacts(');
  const end = source.indexOf('export async function consumeRemoteGeneration(', start);
  const sent = [], budgets = [];
  const context = { readFile, path, ROOT: fileURLToPath(new URL('..', import.meta.url)), validId: () => true,
    quote: value => value, config: { remoteRoot: '/data' },
    singleJsonRemote: async (command, input, options) => { sent.push(JSON.parse(input)); budgets.push(options.timeoutMs); return { passed: true }; } };
  vm.runInNewContext(source.slice(start, end).replace('export ', '')+'\nthis.consume=consumeProducedArtifacts;', context);
  const legacy = { generationId: 'g_test', requirementVersion: 1, requestHash: 'request', sourceHash: 'source', revision: 'rev_train',
    reviewedEpisodes: [{ episodeId: 'ep_train', taskId: 'task', use: 'execution_data', revision: 'rev_train' }] };
  await context.consume({ id: 'p_test' }, { sha256: 'a'.repeat(64) }, { job: legacy });
  assert.equal(sent[0].binding.requiredOutputs, undefined); assert.equal(sent[0].binding.environmentRefs, undefined);
  assert.deepEqual(sent[0].episodes, [{ episodeId: 'ep_train', taskId: 'task', use: 'execution_data' }]);
  assert.equal(budgets[0], 600000);
  const selected = { ...legacy, workerTimeoutSeconds:14400, requiredOutputs: ['demonstrations'],
    environmentRefs: [{ revision: 'rev_train', split: 'train' }, { revision: 'rev_test', split: 'test' }],
    reviewedEpisodes: [...legacy.reviewedEpisodes, { episodeId: 'ep_test', taskId: 'task', use: 'execution_data', revision: 'rev_test' }],
    producedArtifactsConsumer: { frozenManifestRef: 'projects/p_test/deliveries/artifacts/frozen/manifest.json' } };
  await context.consume({ id: 'p_test' }, { sha256: 'b'.repeat(64) }, { job: selected, phase: 'terminal' });
  assert.deepEqual(sent[1].binding.requiredOutputs, selected.requiredOutputs);
  assert.deepEqual(sent[1].binding.environmentRefs, selected.environmentRefs);
  assert.deepEqual(sent[1].episodes, selected.reviewedEpisodes);
  assert.equal(sent[1].frozenRef, selected.producedArtifactsConsumer.frozenManifestRef);
  assert.equal(budgets[1], 14400000, 'format consumer must honor the already validated job execution budget');
});

async function producedTransport() {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const helper = source.slice(source.indexOf('function singleJsonRemote('), source.indexOf('const privateNetworks'));
  const consumer = source.slice(source.indexOf('export async function consumeProducedArtifacts('), source.indexOf('export async function consumeRemoteGeneration('));
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter();
  let inputReady;
  const ready = new Promise(resolve => { inputReady = resolve; });
  child.stdin.end = () => inputReady();
  child.kill = () => { child.killed = true; queueMicrotask(() => child.emit('close', null)); };
  const context = { Buffer, readFile, path, ROOT: fileURLToPath(new URL('..', import.meta.url)), validId: () => true,
    quote: value => value, config: { remoteRoot: '/data' }, ssh: () => child,
    setTimeout: () => ({}), clearTimeout() {} };
  vm.runInNewContext(helper + consumer.replace('export ', '') + '\nthis.consume=consumeProducedArtifacts;', context);
  const reply = context.consume({ id: 'p_test' }, { sha256: 'a'.repeat(64) }, { job: {
    generationId: 'g_test', reviewedEpisodes: [] } });
  await ready;
  return { child, reply };
}

test('produced transport receives a complete large image-file manifest without truncating its digests', async () => {
  const { child, reply } = await producedTransport();
  const files = Array.from({ length: 63769 }, (_, i) => ({
    artifactRef: 'projects/p_test/deliveries/artifacts/' + 'a'.repeat(64) +
      '/robot/native-success/environment/projects/p_test/episodes/episode/frames/external_rgb/' + String(i).padStart(6, '0') + '.png',
    bytes: 100000, sha256: 'b'.repeat(64) }));
  const encoded = JSON.stringify({ passed: true, files });
  assert.ok(Buffer.byteLength(encoded) > 16 * 1024 * 1024);
  for (let i = 0; i < encoded.length; i += 1024 * 1024) child.stdout.emit('data', encoded.slice(i, i + 1024 * 1024));
  child.emit('close', 0);
  const result = await reply;
  assert.equal(result.files.length, files.length);
  assert.equal(result.files.at(-1).artifactRef, files.at(-1).artifactRef);
  assert.equal(result.files.at(-1).sha256, files.at(-1).sha256);
  assert.equal(child.killed, undefined);
});

test('produced transport still rejects a reply beyond the bounded manifest budget', async () => {
  const { child, reply } = await producedTransport();
  const rejected = assert.rejects(reply, /输出超过字节预算/);
  for (let i = 0; i < 65; i++) child.stdout.emit('data', Buffer.alloc(1024 * 1024, 32));
  child.emit('close', 0);
  await rejected;
  assert.equal(child.killed, true);
});
