import test from 'node:test';
import assert from 'node:assert/strict';
import { validateOnlineAssetTarget } from '../src/remote.mjs';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { BlockList, isIP } from 'node:net';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import vm from 'node:vm';

test('public model targets keep the system TUN exception narrow and reject private addresses on every hop', () => {
  assert.equal(validateOnlineAssetTarget('https://raw.githubusercontent.com/x/model.glb', ['198.18.0.131']).address, '198.18.0.131');
  assert.equal(validateOnlineAssetTarget('https://models.example.com/model.glb', ['93.184.216.34']).address, '93.184.216.34');
  for (const address of ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254', '0.0.0.0']) {
    assert.throws(() => validateOnlineAssetTarget('https://raw.githubusercontent.com/model.glb', [address]), /public|公网/);
  }
  assert.throws(() => validateOnlineAssetTarget('https://untrusted.example.com/model.glb', ['198.18.0.131']), /public|公网/);
  for (const url of ['http://example.com/model.glb', 'https://user:pass@example.com/model.glb', 'https://example.com:444/model.glb', 'https://127.0.0.1/model.glb']) {
    assert.throws(() => validateOnlineAssetTarget(url, ['93.184.216.34']));
  }
});

function glb(document = { asset: { version: '2.0' }, meshes: [{}] }) {
  const encoded = Buffer.from(JSON.stringify(document));
  const content = Buffer.concat([encoded, Buffer.alloc((-encoded.length % 4 + 4) % 4, ' ')]);
  const header = Buffer.alloc(20);
  [0x46546c67, 2, content.length + 20, content.length, 0x4e4f534a].forEach((value, index) => header.writeUInt32LE(value, index * 4));
  return Buffer.concat([header, content]);
}

async function downloadFixture(replies, addresses = () => ['93.184.216.34'], options = {}) {
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const commands = [], hosts = [], children = [];
  const context = { BlockList, isIP, URL, Buffer, createHash, setTimeout, clearTimeout, ...options.timers,
    lookup: async host => { hosts.push(host); return options.lookup ? options.lookup(host) : addresses(host).map(address => ({ address })); },
    spawn: (program, args) => {
      commands.push({ program, args });
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
      child.stdio = [null, child.stdout, child.stderr, new EventEmitter()];
      child.signals = [];
      child.kill = signal => { child.signals.push(signal); if (!options.ignoreKill) queueMicrotask(() => child.emit('close', 7)); };
      children.push(child);
      if (options.hold) return child;
      const reply = replies.shift();
      queueMicrotask(() => {
        for (const block of reply.headerChunks || [reply.rawHeaders ?? `HTTP/1.1 ${reply.status || 200} response\r\n${reply.headers || ''}\r\n`]) child.stdio[3].emit('data', Buffer.from(block));
        child.stdout.emit('data', reply.bytes || glb());
        child.emit('close', 0);
      });
      return child;
    } };
  vm.runInNewContext(source.slice(source.indexOf('const privateNetworks'), source.indexOf('export async function stageOnlineAsset('))
    .replaceAll('export ', '') + '\nthis.download=downloadOnlineAsset;', context);
  return { download: context.download, commands, hosts, children };
}

test('online import uses the final HTTP response and never inherits an informational redirect', async () => {
  for (const prefix of ['HTTP/1.1 103 Early Hints\r\nLocation: https://private.example.com/\r\n\r\n',
    'HTTP/1.1 100 Continue\r\n\r\nHTTP/2 103\r\n\r\n']) {
    const f = await downloadFixture([{ rawHeaders: prefix+'HTTP/2 200\r\nContent-Type: model/gltf-binary\r\n\r\n' }]);
    const result = await f.download('https://models.example.com/model.glb');
    assert.equal(result.retrieval.httpStatus, 200);
    assert.equal(result.retrieval.contentType, 'model/gltf-binary');
    assert.equal(f.commands.length, 1);
  }
  const f = await downloadFixture([{ rawHeaders:'HTTP/1.1 103 Early Hints\r\n\r\n' }]);
  await assert.rejects(f.download('https://models.example.com/model.glb'), /最终|HTTP/);
});

test('online cancellation during DNS returns without starting curl or using a late DNS answer', { timeout:1000 }, async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await downloadFixture([], undefined, { lookup: () => gate });
  const controller = new AbortController();
  const pending = f.download('https://models.example.com/model.glb', { signal: controller.signal });
  controller.abort(new Error('cancelled during DNS'));
  await assert.rejects(pending, /cancelled during DNS/);
  release([{ address:'93.184.216.34' }]); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.commands.length, 0);
});

test('informational headers cannot replace the final redirect or content type across chunks', async () => {
  const f = await downloadFixture([{ headerChunks:['HTTP/1.1 103 Early Hints\r\nLocation: https://safe.example.com/\r\n\r',
    '\nHTTP/2 302\r\nLocation: https://private.example.com/model.glb\r\n\r\n'] }],
  host => host === 'private.example.com' ? ['127.0.0.1'] : ['93.184.216.34']);
  await assert.rejects(f.download('https://models.example.com/model.glb'), /公网/);
  assert.equal(f.commands.length,1);
  for (const header of ['HTTP/1.1 101 Switching Protocols\r\n\r\n','HTTP/2 404\r\n\r\n']) {
    const invalid=await downloadFixture([{rawHeaders:header}]);
    await assert.rejects(invalid.download('https://models.example.com/model.glb'), /HTTP/);
  }
});

test('online cancellation bounds curl cleanup even when termination signals are ignored', async () => {
  const timers = [];
  const f = await downloadFixture([], undefined, { hold:true, ignoreKill:true, timers:{
    setTimeout(fn,ms) { const timer={ fn,ms,cleared:false };timers.push(timer);return timer; },
    clearTimeout(timer) { if (timer) timer.cleared=true; } } });
  const controller = new AbortController();
  const pending = f.download('https://models.example.com/model.glb', { signal:controller.signal });
  await new Promise(resolve => setImmediate(resolve));
  controller.abort(new Error('first download cancellation'));
  assert.deepEqual(f.children[0].signals, ['SIGTERM']);
  timers.find(timer => !timer.cleared && timer.ms === 1000).fn();
  assert.deepEqual(f.children[0].signals, ['SIGTERM','SIGKILL']);
  timers.filter(timer => !timer.cleared && timer.ms === 1000).at(-1).fn();
  await assert.rejects(pending, error => error.exitUnconfirmed === true && /first download cancellation/.test(error.message));
  assert.ok(timers.every(timer => timer.cleared));
});

test('the single Mac URL importer rechecks redirected hosts and uses only checked curl targets', async () => {
  const { download, commands, hosts } = await downloadFixture([
    { status: 302, headers: 'Location: https://private.example.com/model.glb\r\n' },
  ], host => host === 'private.example.com' ? ['127.0.0.1'] : ['93.184.216.34']);
  await assert.rejects(download('https://models.example.com/model.glb'), /公网/);
  assert.deepEqual(hosts, ['models.example.com', 'private.example.com']);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].program, '/usr/bin/curl');
  assert.ok(commands[0].args.includes('--resolve'));
  assert.ok(commands[0].args.includes('--max-filesize'));
  assert.equal(commands[0].args.includes('-L'), false);
});

test('the single Mac URL importer rejects external GLB dependencies and oversized actual bytes', async () => {
  for (const bytes of [glb({ asset: { version: '2.0' }, meshes: [{}], images: [{ uri: 'https://example.com/texture.png' }] }),
    Buffer.alloc(25 * 1024 * 1024 + 1)]) {
    const { download } = await downloadFixture([{ bytes }]);
    await assert.rejects(download('https://models.example.com/model.glb'), /外部依赖|25 MiB/);
  }
});

test('staging through the single JSON transport preserves original binary bytes and receipt digests', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-staging-transport-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await readFile(new URL('../src/remote.mjs', import.meta.url), 'utf8');
  const bytes = Buffer.concat([glb(), Buffer.from([0, 255, 128, 1])]);
  const retrieval = { url: 'https://models.example.com/model.glb', finalUrl: 'https://models.example.com/model.glb',
    sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length, httpStatus: 200 };
  let calls = 0, transferred;
  const context = { Buffer, createHash, setTimeout, clearTimeout, quote: value => value,
    config: { remotePython: 'python3', remoteRoot: root }, onlineAssetUrl: () => {},
    downloadOnlineAsset: async () => ({ bytes, retrieval }), ssh: command => {
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); child.stdin = new EventEmitter();
      const cacheLookup = calls++ === 0;
      child.stdin.end = input => queueMicrotask(() => {
        if (cacheLookup) { child.stdout.emit('data', 'null'); child.emit('close', 0); return; }
        transferred = input;
        const result = spawnSync('python3', ['-B', '-c', command.split(' -c ')[1]], { input, encoding: 'utf8' });
        child.stdout.emit('data', result.stdout); child.stderr.emit('data', result.stderr); child.emit('close', result.status);
      });
      return child;
    } };
  const helper = source.slice(source.indexOf('function singleJsonRemote('), source.indexOf('const privateNetworks'));
  const stage = source.slice(source.indexOf('export async function stageOnlineAsset('), source.indexOf('async function runtimePacket('));
  vm.runInNewContext(helper + stage.replace('export ', '') + '\nthis.stage=stageOnlineAsset;', context);
  const reference = await context.stage(retrieval.url);
  assert.equal(reference.digest, retrieval.sha256);
  assert.deepEqual(transferred.subarray(transferred.indexOf(10) + 1), bytes);
  assert.deepEqual(await readFile(path.join(root, 'assets/imports', reference.digest, 'source.glb')), bytes);
  const receipt = await readFile(path.join(root, 'assets/imports', reference.digest, `retrieval-${reference.retrievalDigest}.json`));
  assert.equal(createHash('sha256').update(receipt).digest('hex'), reference.retrievalDigest);
  assert.deepEqual(JSON.parse(receipt), retrieval);
});
