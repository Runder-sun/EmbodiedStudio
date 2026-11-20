import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir, rm, appendFile, open, rename, readdir } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import { randomUUID, createHash } from 'node:crypto';
import vm from 'node:vm';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Store } from '../src/store.mjs';
import { ROOT, validId } from '../src/config.mjs';
import { GenerationHost } from '../src/generation.mjs';

async function isolated(t) {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-store-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = new Store({ root });
  // Existing Store can reproduce the template bug without touching live state.
  store.directory = id => path.join(root, 'projects', id);
  store.save = async () => {};
  return { store, root };
}

test('new projects start without an unrelated physical scene', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '建筑', prompt: '生成两层建筑' });
  const source = await readFile(path.join(store.directory(project.id), 'scene.py'), 'utf8');
  const result = spawnSync('python3', ['-c',
    'import sys,json;sys.path.insert(0,sys.argv[1]);from runtime.scene import World;w=World();s={};exec(sys.argv[2],s);s["build"](w);print(len(w.nodes))', ROOT, source], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(Number(result.stdout.trim()), 0, 'a building request must not silently inherit the push scene');
});

test('source snapshots include helper files and survive later edits', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '房间', prompt: '生成房间' });
  const directory = store.directory(project.id);
  await writeFile(path.join(directory, 'layout.py'), 'ROOM_COUNT = 3\n');
  const first = await store.sourceSnapshot(project.id);
  await writeFile(path.join(directory, 'layout.py'), 'ROOM_COUNT = 4\n');
  const second = await store.sourceSnapshot(project.id);
  assert.notEqual(first.sourceHash, second.sourceHash);
  assert.equal(first.sourceFiles['layout.py'], 'ROOM_COUNT = 3\n');
  const restored = JSON.parse(await readFile(path.join(directory, first.snapshotRef), 'utf8'));
  assert.deepEqual(restored.sourceFiles, first.sourceFiles);
});

test('new delivery programs do not change physical sources while nested physical helpers remain frozen', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '自主交付', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  store.beginGeneration(project.id, { timeBudgetMs: null });
  assert.equal(project.generation.deliveryContract, 'producer-exports-v1');
  const directory = store.directory(project.id);
  const first = await store.sourceSnapshot(project.id);
  await mkdir(path.join(directory, 'delivery'));
  await writeFile(path.join(directory, 'delivery', 'export.py'), '# producer-owned export\n');
  const exported = await store.sourceSnapshot(project.id);
  assert.equal(exported.sourceHash, first.sourceHash);
  assert.equal(exported.sourceFiles['delivery/export.py'], undefined);
  await mkdir(path.join(directory, 'helpers', 'delivery'), { recursive: true });
  await writeFile(path.join(directory, 'helpers', 'delivery', 'physics.py'), '# physical helper\n');
  const changed = await store.sourceSnapshot(project.id);
  assert.notEqual(changed.sourceHash, first.sourceHash);
  assert.equal(changed.sourceFiles['helpers/delivery/physics.py'], '# physical helper\n');
});

test('legacy generations keep delivery directories in their historical physical source layout', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '原合同', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  store.beginGeneration(project.id);
  delete project.generation.deliveryContract;
  const first = await store.sourceSnapshot(project.id);
  await mkdir(path.join(store.directory(project.id), 'delivery'));
  await writeFile(path.join(store.directory(project.id), 'delivery', 'helper.py'), '# legacy frozen helper\n');
  const changed = await store.sourceSnapshot(project.id);
  assert.notEqual(changed.sourceHash, first.sourceHash);
  assert.equal(changed.sourceFiles['delivery/helper.py'], '# legacy frozen helper\n');
  assert.equal(store.beginGeneration(project.id).deliveryContract, undefined);
});

test('public robot profiles are frozen with source while ordinary JSON remains data', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '配置接入', prompt: '按机器人配置加载' });
  const directory = store.directory(project.id);
  await mkdir(path.join(directory, 'robots'));
  const file = path.join(directory, 'robots', 'arm.robot.json');
  await writeFile(file, '{"schema":"sceneagent-robot-profile-v1"}\n');
  await writeFile(path.join(directory, 'diagnostic.json'), '{"private":true}\n');
  const first = await store.sourceSnapshot(project.id);
  assert.equal(first.sourceFiles['robots/arm.robot.json'], '{"schema":"sceneagent-robot-profile-v1"}\n');
  assert.equal(first.sourceFiles['diagnostic.json'], undefined);
  assert.ok(Object.keys(first.sourceFiles).every(name => !name.startsWith('.trace/')));
  await writeFile(file, '{"schema":"sceneagent-robot-profile-v1","ports":{}}\n');
  const second = await store.sourceSnapshot(project.id);
  assert.notEqual(first.sourceHash, second.sourceHash);
  const restored = JSON.parse(await readFile(path.join(directory, first.snapshotRef), 'utf8'));
  assert.deepEqual(restored.sourceFiles, first.sourceFiles);
});

test('generated scoring is a fourth source and changes the frozen input', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '生成评分', prompt: '按本任务生成行为与评分' });
  const sources = await store.sources(project.id);
  assert.equal(typeof sources.rubrics, 'string');
  const result = spawnSync('python3', ['-c',
    'import sys;s={};exec(sys.argv[1],s);s["evaluate"]({}, {}, None)', sources.rubrics], { encoding: 'utf8' });
  assert.notEqual(result.status, 0, 'an ungenerated rubric must not award a score');
  const first = await store.sourceSnapshot(project.id);
  const rubric = 'def evaluate(task, facts, state):\n    return {"state": state, "result": {"success": False, "terminated": False, "reward": None, "details": {}}}\n';
  await store.updateSources(project.id, { ...sources, rubrics: rubric });
  const second = await store.sourceSnapshot(project.id);
  assert.equal((await store.sources(project.id)).rubrics, rubric);
  assert.equal(second.sourceFiles['rubrics.py'], rubric);
  assert.notEqual(first.sourceHash, second.sourceHash);
  assert.equal(first.sourceFiles['rubrics.py'], sources.rubrics);
});

test('reading a historical project does not insert a generated scoring source', async t => {
  const { store } = await isolated(t);
  const project = { id: 'p_historical', jobs: [], events: [] };
  store.projects.push(project);
  const directory = store.directory(project.id);
  await mkdir(directory, { recursive: true });
  for (const key of ['scene', 'tasks', 'expert']) await writeFile(path.join(directory, key + '.py'), '# historical\n');
  const sources = await store.sources(project.id);
  assert.equal(sources.rubrics, '');
  await assert.rejects(readFile(path.join(directory, 'rubrics.py')), { code: 'ENOENT' });
});

test('request updates retain original intent and invalidate older delivery versions', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '楼', prompt: '两层，六个房间' });
  store.beginGeneration(project.id);
  const first = structuredClone(project.generation);
  await store.addRequirement(project.id, '保留楼梯和屋顶');
  assert.equal(project.prompt, '两层，六个房间');
  assert.equal(project.generation.requirementVersion, first.requirementVersion + 1);
  assert.notEqual(project.generation.requestHash, first.requestHash);
  assert.match(await readFile(path.join(store.directory(project.id), 'request.md'), 'utf8'), /两层，六个房间/);
  assert.match(await readFile(path.join(store.directory(project.id), 'request.md'), 'utf8'), /保留楼梯和屋顶/);
});

test('new generations omit progress guesses and cannot be reactivated while stopping', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '停止中的工程', prompt: '保留要求' });
  const generation = store.beginGeneration(project.id);
  assert.equal(Object.hasOwn(generation, 'noProgressCount'), false);
  assert.equal(Object.hasOwn(generation, 'lastSignature'), false);
  generation.status = 'recovering';
  generation.cancelRequestedAt = new Date().toISOString();
  const previous = project.requirements.length;
  await assert.rejects(store.addRequirement(project.id, '不能绕过停止'), /正在停止/);
  assert.equal(project.requirements.length, previous);
  assert.equal(generation.status, 'recovering');
});

test('an explicit unlimited generation budget survives requirements and restart', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-unlimited-generation-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load();
  const project = await store.create({ name: '不限总时长', prompt: '系统验证' });
  const generation = store.beginGeneration(project.id, { timeBudgetMs: null });
  assert.equal(generation.timeBudgetMs, null);
  await store.addRequirement(project.id, '保留全部任务要求');
  const recovered = (await new Store({ root }).load()).get(project.id);
  assert.equal(recovered.generation.id, generation.id);
  assert.equal(recovered.generation.timeBudgetMs, null);
  assert.throws(() => store.beginGeneration(project.id, { timeBudgetMs: 60 * 60_000 }), { status: 409 });
  assert.equal(project.generation.timeBudgetMs, null);
});

test('generation budgets reject invalid input before creating a generation', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '预算输入', prompt: '系统验证' });
  for (const timeBudgetMs of [0, -1, 1.5, 'unlimited', false, {}, Infinity, NaN]) {
    assert.throws(() => store.beginGeneration(project.id, { timeBudgetMs }), { status: 400 });
    assert.equal(project.generation, undefined);
  }
  assert.equal(store.beginGeneration(project.id).timeBudgetMs, null);
});

test('legacy missing budgets stay unlimited without rewriting saved numeric budgets', async t => {
  for (const timeBudgetMs of [undefined, null, 60 * 60_000]) await t.test(String(timeBudgetMs), async t => {
    const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-legacy-budget-'));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = await new Store({ root }).load();
    const project = await store.create({ name: '旧预算', prompt: '系统验证' });
    const generation = store.beginGeneration(project.id);
    if (timeBudgetMs === undefined) delete generation.timeBudgetMs;
    else generation.timeBudgetMs = timeBudgetMs;
    await store.save();
    const before = await readFile(store.stateFile, 'utf8');
    const loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
    assert.equal(await readFile(store.stateFile, 'utf8'), before);
    assert.equal(recovered.generation.timeBudgetMs, timeBudgetMs);
    assert.equal(loaded.beginGeneration(project.id, { timeBudgetMs: timeBudgetMs ?? null }), recovered.generation);
    assert.equal(Object.hasOwn(recovered.generation, 'timeBudgetMs'), timeBudgetMs !== undefined);
    assert.throws(() => loaded.beginGeneration(project.id, { timeBudgetMs: timeBudgetMs === 60 * 60_000 ? null : 60 * 60_000 }), { status: 409 });
    assert.equal(recovered.generation.timeBudgetMs, timeBudgetMs);
  });
});

test('restart preserves durable job identities for reconciliation', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-restart-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, '.state'));
  await writeFile(path.join(root, '.state', 'projects.json'), JSON.stringify([{
    id: 'p_restart', activeTurnId: 'turn_old', jobs: [{ id: 'j_durable', durable: true, status: 'running' }],
    generation: { id: 'g_restart', status: 'active', requirementVersion: 1 }, events: [], messages: [],
  }]));
  const store = await new Store({ root }).load();
  assert.equal(store.get('p_restart').jobs[0].status, 'running');
  assert.equal(store.get('p_restart').jobs[0].id, 'j_durable');
  assert.equal(store.get('p_restart').generation.status, 'recovering');
});

test('restart allocates fresh identities when trace appends outlive the state save', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-trace-restart-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const original = await new Store({ root }).load();
  const project = await original.create({ name: '三房间', prompt: '保留完整场景和机器人示范' });
  original.beginGeneration(project.id);
  await original.save();
  const progress = { type: 'domain_event', jobId: 'j_durable', sequence: 2,
    event: { type: 'progress', message: '机器人完成' } };
  const first = await original.record(project.id, progress);
  const second = await original.record(project.id, { ...progress, sequence: 3 });
  const tracePath = path.join(original.directory(project.id), 'trace.jsonl');
  const traceBefore = await readFile(tracePath, 'utf8');
  const stateBefore = await readFile(original.stateFile, 'utf8');
  assert.equal(JSON.parse(stateBefore)[0].eventSeq, 0, 'simulate exit after append and before save');
  const recovered = await new Store({ root }).load();
  assert.equal(await readFile(tracePath, 'utf8'), traceBefore, 'loading must not rewrite recorded history');
  assert.equal(await readFile(original.stateFile, 'utf8'), stateBefore, 'loading only reconciles the in-memory sequence');
  const third = await recovered.record(project.id, progress);
  const fourth = await recovered.record(project.id, { ...progress, sequence: 3 });
  assert.equal(third.eventId, `${project.id}:3`);
  assert.equal(fourth.eventId, `${project.id}:4`);
  assert.equal(recovered.get(project.id).generation.id, project.generation.id);
  assert.equal(recovered.get(project.id).generation.requirementVersion, project.generation.requirementVersion);
  const rows = await recovered.trace(project.id);
  assert.deepEqual(rows.slice(0, 2), [first, second]);
  assert.equal(new Set(rows.map(row => row.eventId)).size, rows.length);
});

test('concurrent state saves stay within a small heap and persist the final change', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-save-memory-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const script = String.raw`
    import { Store } from ${JSON.stringify(new URL('../src/store.mjs', import.meta.url).href)};
    import { readFile } from 'node:fs/promises';
    const store = new Store({ root: process.argv[1] });
    const payload = '场景状态'.repeat(262144);
    store.projects = [{ sequence: 0, payload }];
    const pending = [];
    for (let index = 0; index < 200; index++) {
      store.projects[0].sequence = index;
      pending.push(store.save());
    }
    await new Promise(resolve => setImmediate(resolve));
    store.projects[0].sequence = 200;
    pending.push(store.save());
    await Promise.all(pending);
    const restored = JSON.parse(await readFile(store.stateFile, 'utf8'));
    if (restored[0].sequence !== 200 || restored[0].payload !== payload) throw new Error('Latest state was not fully persisted');
    global.gc();
    console.log(JSON.stringify({ sequence: restored[0].sequence, payloadLength: restored[0].payload.length,
      heapBytes: process.memoryUsage().heapUsed }));
  `;
  const result = spawnSync(process.execPath, ['--max-old-space-size=64', '--expose-gc', '--input-type=module', '-e', script, root],
    { cwd: root, encoding: 'utf8', timeout: 30_000 });
  assert.equal(result.status, 0, `Small-heap save process failed (${result.signal ?? result.status}): ${result.stderr}`);
  const report = JSON.parse(result.stdout.trim());
  assert.equal(report.sequence, 200);
  assert.equal(report.payloadLength, 1048576);
  assert.ok(report.heapBytes < 64 * 1024 * 1024);
});

test('recovery restores a missing job generation only from its matching persisted payload', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-job-scope-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const original = await new Store({ root }).load();
  const project = await original.create({ name: '归属验证', prompt: '系统测试' });
  original.beginGeneration(project.id);
  project.jobs = ['matching', 'different', 'unknown'].map(id => ({ id, durable: true, status: 'running' }));
  project.jobs[0].payload = { projectId: project.id, jobId: 'matching', generationId: project.generation.id };
  project.jobs[1].payload = { projectId: 'another_project', jobId: 'different', generationId: project.generation.id };
  await original.save();
  const recovered = (await new Store({ root }).load()).get(project.id);
  assert.equal(recovered.jobs[0].generationId, project.generation.id);
  assert.equal(recovered.jobs[1].generationId, undefined);
  assert.equal(recovered.jobs[2].generationId, undefined);
  assert.equal(recovered.jobs[2].status, 'running');
});


test('persisted system wait survives reload and appended requirements without unlocking', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'sceneagent-system-wait-'));t.after(()=>rm(root,{recursive:true,force:true}));
  const store=await new Store({root}).load(),project=await store.create({name:'等待',prompt:'完整原要求'});
  const generation=store.beginGeneration(project.id,{timeBudgetMs:null});
  generation.waitingSystem={requestId:'wait',kind:'reported',reason:'tool blocked',evidence:['actual error'],since:new Date().toISOString()};
  generation.status='waiting_system';generation.systemWaitMs=123;await store.save();
  const loaded=await new Store({root}).load(),recovered=loaded.get(project.id);await loaded.addRequirement(project.id,'补充要求仍保存');
  assert.equal(recovered.generation.status,'waiting_system');assert.deepEqual(recovered.generation.waitingSystem,generation.waitingSystem);
  assert.equal(recovered.generation.systemWaitMs,123);assert.equal(recovered.generation.id,generation.id);
  assert.equal(recovered.requirements.at(-1).text,'补充要求仍保存');
});

async function instrumentedStore(overrides = {}) {
  const source = (await readFile(new URL('../src/store.mjs', import.meta.url), 'utf8'))
    .replace(/^import .+;\n/gm, '').replaceAll('export ', '');
  const context = { mkdir, readFile, writeFile, rename, appendFile, readdir, open, createReadStream, createInterface,
    path, randomUUID, createHash, ROOT, validId, Buffer, ...overrides };
  vm.runInNewContext(source + '\nthis.TestStore=Store;', context);
  return context.TestStore;
}

test('a recovered append failure retries the original records in order without losing their identities', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-append-retry-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 追加恢复' });
  const trace = path.join(store.directory(project.id), 'trace.jsonl');
  await mkdir(trace);
  await assert.rejects(store.record(project.id, { type: 'first' }));
  await assert.rejects(store.record(project.id, { type: 'second' }));
  await rm(trace, { recursive: true });
  await store.flush(project.id);
  const third = await store.record(project.id, { type: 'third' }), rows = await store.trace(project.id);
  assert.deepEqual(rows.map(row => row.type), ['first', 'second', 'third']);
  assert.deepEqual(rows.map(row => row.eventId), [1, 2, 3].map(index => `${project.id}:${index}`));
  assert.equal(rows[2].eventId, third.eventId);
});

test('a saved failed append is held across reload instead of skipping its row and dispatching again', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-pending-append-restart-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 待写行重启' });
  store.beginGeneration(project.id); project.threadId = 'isolated-original';
  const trace = path.join(store.directory(project.id), 'trace.jsonl');
  await mkdir(trace);
  await assert.rejects(store.record(project.id, { type: 'first_failed_row' }));
  await store.save();
  await rm(trace, { recursive: true }); await writeFile(trace, '');
  const stateBefore = await readFile(store.stateFile), traceBefore = await readFile(trace);
  const loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
  assert.deepEqual(await readFile(store.stateFile), stateBefore, 'load must preserve the original state evidence');
  assert.equal(recovered.generation.status, 'waiting_system');
  assert.equal(recovered.generation.waitingSystem.kind, 'trace_incomplete');
  assert.ok(recovered.generation.waitingSystem.evidence.some(text => text.includes(`${project.id}:1`)));
  let sends = 0;
  const host = new GenerationHost({ store: loaded, codex: { readThread: async () => ({ id: recovered.threadId, status: { type: 'idle' }, turns: [] }) },
    continueTurn: async () => { sends++; } });
  await host.recover(recovered);
  await assert.rejects(loaded.record(recovered.id, { type: 'second_after_reload' }));
  await assert.rejects(host.resume(recovered, { generationId: recovered.generation.id }));
  assert.equal(sends, 0); assert.equal(recovered.eventSeq, 1);
  assert.deepEqual(recovered.events.map(row => row.type), ['first_failed_row']);
  assert.deepEqual(await readFile(trace), traceBefore);
  const afterLoad = JSON.parse(stateBefore);
  const afterRecover = JSON.parse(await readFile(store.stateFile));
  assert.deepEqual(afterRecover[0].events, afterLoad[0].events, 'the persisted original row remains intact');
});

test('an uncertain completed append is confirmed before retry rather than appended twice', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-append-confirm-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let failClose = true, writes = 0;
  const TestStore = await instrumentedStore({ open: async (...args) => {
    const file = await open(...args);
    if (String(args[0]).endsWith('trace.jsonl')) {
      const close = file.close.bind(file), append = file.appendFile.bind(file);
      file.appendFile = async (...values) => { writes++; return append(...values); };
      file.close = async () => { await close(); if (failClose) { failClose = false; throw new Error('isolated uncertain close'); } };
    }
    return file;
  } });
  const store = await new TestStore({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 未知追加' });
  await assert.rejects(store.record(project.id, { type: 'first' }), /uncertain close/);
  await store.record(project.id, { type: 'second' });
  const rows = await store.trace(project.id);
  assert.deepEqual(Array.from(rows, row => row.type), ['first', 'second']);
  assert.equal(writes, 2, 'the confirmed first range must never be appended a second time');
});

test('reload confirms an uncertain completed append already in the trace without duplicating or blocking it', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-confirmed-append-restart-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let failClose = true;
  const TestStore = await instrumentedStore({ open: async (...args) => {
    const file = await open(...args);
    if (String(args[0]).endsWith('trace.jsonl')) {
      const close = file.close.bind(file);
      file.close = async () => { await close(); if (failClose) { failClose = false; throw new Error('isolated uncertain close'); } };
    }
    return file;
  } });
  const store = await new TestStore({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 已写行重启' });
  store.beginGeneration(project.id);
  await assert.rejects(store.record(project.id, { type: 'complete_before_failed_close' }), /uncertain close/);
  await store.save();
  const loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
  assert.equal(recovered.generation.waitingSystem, undefined);
  await loaded.record(project.id, { type: 'next_after_reload' });
  const rows = await loaded.trace(project.id);
  assert.deepEqual(rows.map(row => row.type), ['complete_before_failed_close', 'next_after_reload']);
  assert.deepEqual(rows.map(row => row.eventId), [1, 2].map(index => `${project.id}:${index}`));
});

test('an append that leaves partial bytes preserves them and waits before any further record', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-append-partial-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let partial = true;
  const TestStore = await instrumentedStore({ open: async (...args) => {
    const file = await open(...args);
    if (String(args[0]).endsWith('trace.jsonl')) {
      const append = file.appendFile.bind(file);
      file.appendFile = async (text, ...values) => {
        if (partial) { partial = false; await append(text.slice(0, 20), ...values); throw new Error('isolated short append'); }
        return append(text, ...values);
      };
    }
    return file;
  } });
  const store = await new TestStore({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 半条追加' });
  store.beginGeneration(project.id);
  await assert.rejects(store.record(project.id, { type: 'first' }));
  const trace = path.join(store.directory(project.id), 'trace.jsonl'), before = await readFile(trace);
  await assert.rejects(store.record(project.id, { type: 'second' }));
  await assert.rejects(store.flush(project.id));
  assert.deepEqual(await readFile(trace), before);
  assert.equal(project.generation.status, 'waiting_system');
  assert.equal(project.generation.waitingSystem.kind, 'trace_incomplete');
});

test('an incomplete final trace row isolates its project without rewriting evidence or dispatching', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-trace-tail-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 尾行恢复' });
  store.beginGeneration(project.id); project.threadId = 'isolated-original';
  await store.record(project.id, { type: 'first' });
  const other = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 另一工程' });
  await store.save();
  const trace = path.join(store.directory(project.id), 'trace.jsonl');
  await appendFile(trace, `{"eventId":"${project.id}:2","type":`);
  const before = await readFile(trace), loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
  assert.equal(loaded.get(other.id).name, other.name);
  assert.deepEqual(await readFile(trace), before);
  assert.equal(recovered.generation.status, 'waiting_system');
  assert.equal(recovered.generation.waitingSystem.kind, 'trace_incomplete');
  let sends = 0;
  const host = new GenerationHost({ store: loaded, codex: { readThread: async () => ({ id: recovered.threadId, status: { type: 'idle' }, turns: [] }) },
    continueTurn: async () => { sends++; } });
  await host.recover(recovered);
  await assert.rejects(host.resume(recovered, { generationId: recovered.generation.id }));
  await assert.rejects(loaded.record(recovered.id, { type: 'must_not_append' }));
  assert.equal(recovered.eventSeq, 2, 'the interrupted tail reserves the next identity without appending it');
  assert.equal(sends, 0);
  assert.deepEqual(await readFile(trace), before);
  await loaded.record(other.id, { type: 'unaffected' });
});

test('complete malformed trace rows are still rejected, including a malformed final value without a newline', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-trace-bad-row-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 坏行' });
  const trace = path.join(store.directory(project.id), 'trace.jsonl');
  for (const text of ['{bad}\n', '{bad}', '{"type":\n{}\n']) {
    await writeFile(trace, text);
    await assert.rejects(new Store({ root }).load(), SyntaxError);
    assert.equal(await readFile(trace, 'utf8'), text);
  }
});

test('only incomplete terminal JSON prefixes are isolated and a draft inherits the same generation wait', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-trace-prefix-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 草稿尾片段' });
  const trace = path.join(store.directory(project.id), 'trace.jsonl');
  for (const tail of ['{"type":', '{"type":"unfinished', '{"type":"first"']) {
    await writeFile(trace, tail);
    const loaded = await new Store({ root }).load();
    const generation = loaded.beginGeneration(project.id);
    assert.equal(generation.status, 'waiting_system');
    assert.equal(generation.waitingSystem.kind, 'trace_incomplete');
    await assert.rejects(loaded.record(project.id, { type: 'blocked' }));
    assert.equal(await readFile(trace, 'utf8'), tail);
  }
  await writeFile(trace, '{"type":"valid_final"}');
  const loaded = await new Store({ root }).load(), generation = loaded.beginGeneration(project.id);
  await assert.rejects(loaded.record(project.id, { type: 'cannot_join_the_prior_json' }));
  assert.equal(generation.status, 'waiting_system');
  assert.equal(generation.waitingSystem.kind, 'trace_incomplete');
  assert.equal(await readFile(trace, 'utf8'), '{"type":"valid_final"}');
});

test('reloading the same incomplete tail reuses its reserved identity for active generations and drafts', async t => {
  for (const active of [false, true]) await t.test(active ? 'active generation' : 'draft', async t => {
    const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-tail-reservation-'));
    t.after(() => rm(root, { recursive: true, force: true }));
    const store = await new Store({ root }).load(), project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 尾片段占位' });
    if (active) store.beginGeneration(project.id);
    await store.record(project.id, { type: 'first' }); await store.save();
    const trace = path.join(store.directory(project.id), 'trace.jsonl');
    await appendFile(trace, '{"type":');
    const before = await readFile(trace), first = await new Store({ root }).load();
    const reserved = first.get(project.id).eventSeq, requestId = first.get(project.id).generation?.waitingSystem?.requestId;
    await first.save();
    const second = await new Store({ root }).load(), recovered = second.get(project.id);
    assert.equal(recovered.eventSeq, reserved);
    assert.equal(recovered.generation?.waitingSystem?.requestId, requestId);
    assert.deepEqual(await readFile(trace), before);
    const generation = second.beginGeneration(project.id);
    assert.equal(generation.waitingSystem.kind, 'trace_incomplete');
    await assert.rejects(second.record(project.id, { type: 'still_blocked' }));
    assert.equal(recovered.eventSeq, reserved);
  });
});

test('queued state saves merge only before serialization and retain each caller persistence barrier', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-save-barriers-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let writes = 0, started, release;
  const firstStarted = new Promise(resolve => { started = resolve; }), blocked = new Promise(resolve => { release = resolve; });
  const TestStore = await instrumentedStore({ writeFile: async (...args) => {
    writes++;
    if (writes === 1) { started(); await blocked; }
    await writeFile(...args);
  } });
  const store = new TestStore({ root }); store.projects = [{ sequence: 0 }];
  const burst = [];
  for (let index = 0; index < 100; index++) { store.projects[0].sequence = index; burst.push(store.save()); }
  await firstStarted;
  store.projects[0].sequence = 100;
  let laterSettled = false;
  const later = store.save().then(() => { laterSettled = true; });
  assert.equal(laterSettled, false);
  release(); await Promise.all(burst);
  const atBarrier = JSON.parse(await readFile(store.stateFile, 'utf8'))[0].sequence;
  assert.ok(atBarrier >= 99);
  await later;
  assert.equal(JSON.parse(await readFile(store.stateFile, 'utf8'))[0].sequence, 100);
  assert.equal(writes, 2, 'the synchronous burst writes once; the post-serialization update needs another write');
});

test('a failed merged save rejects every waiting caller and a later save can persist the latest state', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-save-retry-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  let fail = true;
  const TestStore = await instrumentedStore({ writeFile: async (...args) => {
    if (fail) throw new Error('isolated state write failure');
    return writeFile(...args);
  } });
  const store = new TestStore({ root }); store.projects = [{ sequence: 1 }];
  const first = store.save(), second = store.save();
  const errors = await Promise.allSettled([first, second]);
  assert.ok(errors.every(row => row.status === 'rejected' && /state write failure/.test(row.reason.message)));
  fail = false; store.projects[0].sequence = 2;
  await store.save();
  assert.equal(JSON.parse(await readFile(store.stateFile, 'utf8'))[0].sequence, 2);
});

test('selected output scope locks while active and a new collection keeps the accepted environment receipt', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '阶段拆分隔离验证', prompt: '保留完整场景要求' });
  const scene = store.beginGeneration(project.id, { requiredOutputs: ['generation', 'evaluation'] });
  assert.deepEqual(scene.requiredOutputs, ['generation', 'evaluation']);
  assert.throws(() => store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'] }), { status: 409 });
  const receipt = { accepted: true, generationId: scene.id, products: { evaluation: { passed: true } } };
  const historical = { id: 'environment_delivery', kind: 'consume', status: 'completed', delivery: receipt };
  project.jobs.push(historical); project.delivery = receipt; scene.status = 'complete';
  project.latestBuild = { projectId: project.id, revision: 'layout_a', capabilities: { stageSplit: true }, tasks: [{ id: 'task' }] };
  const collection = store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'],
    environmentRefs: [{ revision: 'layout_a', split: 'train', taskIds: ['task'] }] });
  assert.notEqual(collection.id, scene.id);
  assert.deepEqual(collection.requiredOutputs, ['demonstrations']);
  assert.deepEqual(collection.environmentRefs, [{ revision: 'layout_a', split: 'train', taskIds: ['task'] }]);
  assert.equal(historical.delivery, receipt);
  assert.equal(receipt.accepted, true);
  assert.equal(project.prompt, '保留完整场景要求');
  assert.throws(() => store.beginGeneration(project.id, { environmentRefs: [{ revision: 'layout_a', split: 'test' }] }), { status: 409 });
});

test('scope rejects unsupported combinations and conflicting layout split labels', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '输入边界隔离验证', prompt: '任务' });
  for (const requiredOutputs of [[], ['evaluation'], ['generation'], ['generation', 'generation', 'evaluation']]) {
    assert.throws(() => store.beginGeneration(project.id, { requiredOutputs }), { status: 400 });
  }
  assert.throws(() => store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'],
    environmentRefs: [{ revision: 'layout_a', split: 'train' }, { revision: 'layout_a', split: 'test' }] }), { status: 400 });
});

test('collection refuses missing or unsupported old environment inputs before creating a new generation', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '旧包能力隔离验证', prompt: '原始任务' });
  assert.throws(() => store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'], environmentRefs: [{ revision: 'absent' }] }), /环境|版本/);
  assert.equal(project.generation, undefined);
  project.latestBuild = { projectId: project.id, revision: 'old', tasks: [{ id: 'task' }] };
  assert.throws(() => store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'], environmentRefs: [{ revision: 'old' }] }), /旧|兼容|支持/);
  assert.equal(project.generation, undefined);
});

test('video-only collection locks the requested format and preserves normal training exports', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '完整视频', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  project.latestBuild = { projectId: project.id, revision: 'layout', capabilities: { stageSplit: true }, tasks: [{ id: 'task' }] };
  const generation = store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'],
    environmentRefs: [{ revision: 'layout' }], videoOnly: true });
  assert.equal(generation.videoOnly, true);
  assert.throws(() => store.beginGeneration(project.id, { videoOnly: false }), { status: 409 });
  assert.equal(store.beginGeneration(project.id), generation);
  generation.status = 'complete';
  assert.equal(store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'],
    environmentRefs: [{ revision: 'layout' }] }).videoOnly, undefined);
  const scene = await store.create({ name: '纯场景', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  assert.throws(() => store.beginGeneration(scene.id, { requiredOutputs: ['generation', 'evaluation'], videoOnly: true }), { status: 400 });
  const fresh = await store.create({ name: '新环境完整视频', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  assert.equal(store.beginGeneration(fresh.id, { requiredOutputs: ['demonstrations'], videoOnly: true }).videoOnly, true);
});

test('explicit raw video recording keeps the generation and locks real task scope', async t => {
  const { store } = await isolated(t);
  const project = await store.create({ name: '原始录像', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  project.latestBuild = { projectId: project.id, revision: 'layout', tasks: [{ id: 'native_task' }] };
  const generation = store.beginGeneration(project.id, { requiredOutputs: ['demonstrations'], videoOnly: true });
  project.delivery = { accepted: true, marker: 'historical receipt' };
  project.episodes = [{ episode: 'failed_attempt', score: { success: false } }];
  const before = structuredClone({ delivery: project.delivery, episodes: project.episodes, latestBuild: project.latestBuild });
  assert.equal(store.beginGeneration(project.id, { recordOnlyVideos: true, recordOnlyTaskIds: ['native_task'] }), generation);
  assert.equal(generation.recordOnlyVideos, true);
  assert.deepEqual(generation.recordOnlyTaskIds, ['native_task']);
  assert.deepEqual({ delivery: project.delivery, episodes: project.episodes, latestBuild: project.latestBuild }, before);
  assert.equal(store.beginGeneration(project.id), generation);
  assert.throws(() => store.beginGeneration(project.id, { recordOnlyVideos: false }), { status: 409 });
  assert.throws(() => store.beginGeneration(project.id, { recordOnlyTaskIds: ['other_task'] }), { status: 409 });
  const scoped = await store.create({ name: '既有环境任务选择', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  scoped.latestBuild = { projectId: scoped.id, revision: 'layout', capabilities: { stageSplit: true },
    tasks: [{ id: 'native_task' }, { id: 'other_task' }] };
  store.beginGeneration(scoped.id, { requiredOutputs: ['demonstrations'], videoOnly: true,
    environmentRefs: [{ revision: 'layout', taskIds: ['native_task'] }] });
  assert.throws(() => store.beginGeneration(scoped.id, { recordOnlyVideos: true, recordOnlyTaskIds: ['other_task'] }), { status: 400 });
  assert.equal(scoped.generation.recordOnlyVideos, undefined);
});

test('raw video recording requires explicit video-only scope and unique known task IDs', async t => {
  const { store } = await isolated(t);
  for (const options of [{ recordOnlyVideos: 'true' }, { recordOnlyVideos: true },
    { recordOnlyVideos: true, requiredOutputs: ['demonstrations'], videoOnly: true },
    { recordOnlyVideos: true, requiredOutputs: ['demonstrations'], videoOnly: true, recordOnlyTaskIds: [] },
    { recordOnlyVideos: true, requiredOutputs: ['demonstrations'], videoOnly: true, recordOnlyTaskIds: ['native_task', 'native_task'] },
    { recordOnlyVideos: true, requiredOutputs: ['demonstrations'], videoOnly: true, recordOnlyTaskIds: ['foreign_task'] },
    { recordOnlyTaskIds: ['native_task'] }]) {
    const project = await store.create({ name: '录像参数检查', prompt: 'SYSTEM_VERIFICATION_ONLY' });
    project.latestBuild = { projectId: project.id, revision: 'layout', tasks: [{ id: 'native_task' }] };
    assert.throws(() => store.beginGeneration(project.id, options), { status: 400 });
    assert.equal(project.generation, undefined);
  }
  const fresh = await store.create({ name: '尚未构建录像', prompt: 'SYSTEM_VERIFICATION_ONLY' });
  assert.deepEqual(store.beginGeneration(fresh.id, { requiredOutputs: ['demonstrations'], videoOnly: true,
    recordOnlyVideos: true, recordOnlyTaskIds: ['native_task'] }).recordOnlyTaskIds, ['native_task']);
});
