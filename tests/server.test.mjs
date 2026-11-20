import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRange, publicProject, createStudio } from '../src/server.mjs';
import { validId, quote } from '../src/config.mjs';
import { validateArtifactPath } from '../src/remote.mjs';
import { Store } from '../src/store.mjs';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';

// SYSTEM_VERIFICATION_ONLY: native routing fixtures never start a service or model.
async function nativeRoutingFixture(t, { read } = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-native-routing-'));
  const store = await new Store({ root }).load();
  const original = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY native routing', prompt: 'isolated public events' });
  const other = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY foreign routing', prompt: 'isolated foreign events' });
  original.threadId = 'native-root'; other.threadId = 'foreign-root';
  store.beginGeneration(original.id); store.beginGeneration(other.id);
  await store.save();
  const metadata = new Map(), reads = [], responses = [];
  const bridge = { async start() {}, async close() {}, status: () => ({ ready: true }),
    async readThread(id, options) { reads.push(id); assert.equal(options.includeTurns, false);
      return read ? read(id, { metadata, bridge }) : metadata.get(id); },
    async respond(...args) { responses.push(args); } };
  const studio = await createStudio({ store, bridge, traceSync: async () => {} });
  const project = store.get(original.id), foreign = store.get(other.id);
  project.activeTurnId = 'root-active';
  const child = (id, parent = project.threadId, cwd = store.directory(project.id)) => ({ id, parentThreadId: parent, cwd,
    source: { subAgent: { thread_spawn: { parent_thread_id: parent, agent_path: '/root/fixture' } } }, status: { type: 'idle' } });
  metadata.set('native-child', child('native-child'));
  metadata.set('foreign-child', child('foreign-child', foreign.threadId, store.directory(foreign.id)));
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  const event = async value => {
    const pending = bridge.onEvent(value);
    if (pending) await pending; else await new Promise(resolve => setTimeout(resolve, 20));
    await store.flush(project.id); await store.flush(foreign.id);
  };
  const completed = (id = 'native-child') => ({ method: 'item/completed', params: { threadId: id, turnId: 'child-turn',
    item: { id: 'child-message', type: 'agentMessage', text: 'SYSTEM_VERIFICATION_ONLY public output' } } });
  return { studio, store, project, foreign, bridge, metadata, reads, responses, child, event, completed };
}

test('cold native child events recover their true project without another started notification', async t => {
  const f = await nativeRoutingFixture(t);
  f.project.generation.waitingSystem = { requestId: 'preserved-wait', reason: 'fixture' };
  f.project.generation.cancelRequestedAt = '2026-10-28T00:00:00Z';
  const before = structuredClone({ generation: f.project.generation, activeTurnId: f.project.activeTurnId, messages: f.project.messages });
  await f.event({ method: 'item/completed', params: { threadId: f.project.threadId, turnId: 'root-active',
    item: { id: 'reused', type: 'subAgentActivity', kind: 'interacted', agentThreadId: 'native-child' } } });
  await f.event({ method: 'turn/started', params: { threadId: 'native-child', turn: { id: 'child-turn', status: 'inProgress' } } });
  await f.event(f.completed());
  await f.event({ method: 'turn/completed', params: { threadId: 'native-child', turn: { id: 'child-turn', status: 'completed' } } });
  const rows = (await f.store.trace(f.project.id)).filter(row => row.params?.threadId === 'native-child');
  assert.equal(rows.length, 3); assert.ok(rows.every(row => row.child === true));
  assert.deepEqual({ generation: f.project.generation, activeTurnId: f.project.activeTurnId, messages: f.project.messages }, before);
});

test('a cold child approval as the first native request is scoped without automatically approving it', async t => {
  const f = await nativeRoutingFixture(t);
  await f.bridge.onRequest({ id: 'cold-approval', method: 'item/commandExecution/requestApproval',
    params: { threadId: 'native-child', turnId: 'child-turn', command: 'fixture' } });
  await f.store.flush(f.project.id);
  const row = f.project.events.find(row => row.type === 'approval_requested');
  assert.ok(row); assert.equal(row.params.threadId, 'native-child');
  assert.equal(f.studio.host.hasApprovals(f.project), true); assert.equal(f.studio.host.hasApprovals(f.foreign), false);
  assert.deepEqual(f.responses, []); assert.equal(f.project.activeTurnId, 'root-active');
});

test('started claims cannot assign a foreign child by recipient or project cwd', async t => {
  const f = await nativeRoutingFixture(t);
  await f.event({ method: 'item/completed', params: { threadId: f.project.threadId, turnId: 'root-active',
    item: { id: 'false-start', type: 'subAgentActivity', kind: 'started', agentThreadId: 'foreign-child' } } });
  await f.event(f.completed('foreign-child'));
  assert.equal(f.project.events.filter(row => row.params?.threadId === 'foreign-child').length, 0);
  assert.equal(f.foreign.events.filter(row => row.params?.threadId === 'foreign-child').length, 1);
  await f.event({ method: 'thread/started', params: { thread: { id: 'bad-cwd-child', parentThreadId: 'foreign-root',
    cwd: f.store.directory(f.project.id), source: { subAgent: { thread_spawn: { parent_thread_id: 'foreign-root' } } } } } });
  await f.event(f.completed('bad-cwd-child'));
  assert.equal(f.project.events.filter(row => row.params?.threadId === 'bad-cwd-child').length, 0);
});

test('native routing follows a real ancestor chain and rejects conflicting sources', async t => {
  const f = await nativeRoutingFixture(t);
  f.metadata.set('grandchild', f.child('grandchild', 'native-child'));
  await f.event(f.completed('grandchild'));
  assert.equal(f.project.events.filter(row => row.params?.threadId === 'grandchild' && row.child).length, 1);
  for (const kind of ['source-parent', 'cwd', 'id', 'cycle']) {
    const metadata = f.child('invalid-' + kind);
    if (kind === 'source-parent') metadata.source.subAgent.thread_spawn.parent_thread_id = 'different-root';
    if (kind === 'cwd') metadata.cwd = f.store.directory(f.foreign.id);
    if (kind === 'id') metadata.id = 'another-id';
    if (kind === 'cycle') metadata.parentThreadId = metadata.source.subAgent.thread_spawn.parent_thread_id = metadata.id;
    f.metadata.set('invalid-' + kind, metadata); await f.event(f.completed('invalid-' + kind));
    assert.equal(f.project.events.filter(row => row.params?.threadId === 'invalid-' + kind).length, 0);
  }
});

test('native provenance reads cannot recursively probe transport events or commit after root identity changes', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; }), pending = new Promise(resolve => { entered = resolve; });
  const f = await nativeRoutingFixture(t, { read: async (id, { metadata, bridge }) => {
    bridge.onEvent({ method: 'transport/sent', params: { message: { method: 'thread/read', params: { threadId: id } } } });
    entered(); await gate; return metadata.get(id);
  } });
  const task = f.bridge.onEvent(f.completed());
  await Promise.race([pending, new Promise(resolve => setTimeout(resolve, 30))]);
  f.project.threadId = 'replacement-root'; release(); await task; await f.store.flush(f.project.id);
  assert.equal(f.project.events.filter(row => row.params?.threadId === 'native-child').length, 0);
  assert.deepEqual(f.reads, ['native-child']);
});

test('queued native events and approvals cannot acquire a replacement generation identity', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; }), pending = new Promise(resolve => { entered = resolve; });
  const f = await nativeRoutingFixture(t, { read: async (id, { metadata }) => { entered(); await gate; return metadata.get(id); } });
  const blocked = f.bridge.onEvent(f.completed()); await pending;
  const queued = f.bridge.onEvent({ method: 'item/completed', params: { threadId: f.project.threadId, turnId: 'old-root-turn',
    item: { id: 'old-root-message', type: 'agentMessage', text: 'old generation' } } });
  const approval = f.bridge.onRequest({ id: 'old-generation-approval', method: 'item/commandExecution/requestApproval',
    params: { threadId: f.project.threadId, turnId: 'old-root-turn' } });
  f.project.generation = { ...f.project.generation, id: 'replacement-generation' };
  release(); await Promise.all([blocked, queued, approval]); await f.store.flush(f.project.id);
  assert.equal(f.project.events.filter(row => row.method === 'item/completed' || row.type === 'approval_requested').length, 0);
  assert.equal(f.studio.host.hasApprovals(f.project), false);
});

test('the HTTP rollout shortcut preserves explicit task selection and rejects invalid selections before execution', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-task-selection-'));
  const store = await new Store({root}).load(), submitted=[];
  const studio = await createStudio({store,port:0,
    bridge:{async start(){},async close(){},status:()=>({ready:true})},
    remote:async request => { submitted.push(request);return {jobId:request.jobId,state:'queued',cursor:0}; } });
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const address = await studio.start();
  const run = async body => {
    const p = await store.create({name:'SYSTEM_VERIFICATION_ONLY tasks',prompt:'isolated task selection'});
    p.latestBuild={projectId:p.id,revision:'rev_selection',tasks:[{id:'task_a'},{id:'task_b'}],sourceDigests:{'expert.py':'a'.repeat(64)}};
    await store.save();
    const response = await fetch(`${address}/api/projects/${p.id}/rollout`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    return {response,value:await response.json()};
  };
  assert.equal((await run({taskIds:['task_b']})).response.status,202);
  assert.deepEqual(submitted.at(-1).task.taskIds,['task_b']);
  assert.equal((await run({})).response.status,202);
  assert.deepEqual(submitted.at(-1).task.taskIds,['task_a','task_b']);
  const budgeted = await run({taskIds:['task_b'],workerTimeoutSeconds:1800.5});
  assert.equal(budgeted.response.status,202);
  assert.equal(submitted.at(-1).task.workerTimeoutSeconds,1800.5);
  assert.equal(budgeted.value.workerTimeoutSeconds,1800.5);
  for (const body of [{taskIds:[]},{taskIds:null},{taskIds:'task_b'},{taskIds:['unknown']},null]) {
    const count=submitted.length,result=await run(body);
    assert.ok([400,409].includes(result.response.status),JSON.stringify(result.value));
    assert.equal(submitted.length,count,'invalid input must not run all tasks');
  }
});

test('invalid manual worker budgets preserve the existing generation and delivery before dispatch', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'manual-budget-preserve-'));
  const store=await new Store({root}).load(), submitted=[];
  const studio=await createStudio({store,port:0,bridge:{async start(){},async close(){},status:()=>({ready:true})},
    remote:async request=>{submitted.push(request);return {jobId:request.jobId,state:'queued',cursor:0};}});
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const address=await studio.start();
  const project=await store.create({name:'SYSTEM_VERIFICATION_ONLY budget',prompt:'preserve receipt'});
  store.beginGeneration(project.id);project.generation.status='complete';
  project.delivery={accepted:true,generationId:project.generation.id};
  const before=structuredClone({generation:project.generation,delivery:project.delivery,jobs:project.jobs});
  for(const operation of ['build','rollout']) for(const workerTimeoutSeconds of [null,false,true,'1800',0,-1]) {
    const response=await fetch(`${address}/api/projects/${project.id}/${operation}`,{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({workerTimeoutSeconds})});
    assert.equal(response.status,400);
    assert.deepEqual({generation:project.generation,delivery:project.delivery,jobs:project.jobs},before);
  }
  assert.equal(submitted.length,0);
});

test('invalid rollout selections preserve the completed generation and accepted delivery', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-task-selection-preserve-'));
  const store = await new Store({root}).load(), submitted=[];
  const studio = await createStudio({store,port:0,
    bridge:{async start(){},async close(){},status:()=>({ready:true})},
    remote:async request => { submitted.push(request);return {jobId:request.jobId,state:'queued',cursor:0}; } });
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const address = await studio.start();
  for (const kind of ['unknown_task','no_build','no_tasks']) {
    const p = await store.create({name:'SYSTEM_VERIFICATION_ONLY completed task',prompt:'preserve accepted delivery'});
    store.beginGeneration(p.id);
    p.generation.status='complete';
    p.delivery={accepted:true,generationId:p.generation.id,marker:'preserve'};
    if (kind!=='no_build') p.latestBuild={projectId:p.id,revision:'rev_selection',tasks:kind==='no_tasks'?[]:[{id:'task_a'}]};
    const before=structuredClone({generation:p.generation,delivery:p.delivery,jobs:p.jobs});
    await store.save();
    const response=await fetch(`${address}/api/projects/${p.id}/rollout`,{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify(kind==='unknown_task'?{taskIds:['unknown']}:{})});
    const value=await response.json();
    assert.ok([400,409].includes(response.status),JSON.stringify(value));
    assert.deepEqual({generation:p.generation,delivery:p.delivery,jobs:p.jobs},before,kind);
  }
  assert.equal(submitted.length,0);
});

test('each generation turn receives its current scene-only scope without an unconditional robot behavior assignment', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-scene-only-input-'));
  const store = await new Store({ root }).load(), starts = [], turns = [];
  const project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY scene scope', prompt: '只生成场景与评测环境' });
  const studio = await createStudio({ store, port: 0, traceSync: async () => {},
    bridge: { async start() {}, async close() {}, status: () => ({ ready: true }),
      async startThread(options) { starts.push(options); return { id: 'scope-thread' }; },
      async turn(id, input) { turns.push({ id, input }); return { id: 'scope-turn', status: 'inProgress' }; },
      async steer(id, turnId, input) { turns.push({ id, input }); return { id: turnId, status: 'inProgress' }; } } });
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  const address = await studio.start();
  for (const message of [project.prompt, '继续本次纯场景任务']) {
    const response = await fetch(`${address}/api/projects/${project.id}/chat`, { method: 'POST',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ message, requiredOutputs: ['generation', 'evaluation'] }) });
    assert.equal(response.status, 200, await response.text());
    assert.match(turns.at(-1).input, /不选择或加载机械臂\/机器人/);
    assert.match(turns.at(-1).input, /不编写或运行专家/);
    assert.match(turns.at(-1).input, /家具关节.*物理.*评分/);
  }
  assert.equal(starts.length, 1);
  assert.doesNotMatch(starts[0].instructions, /；自主试验并产出可独立重跑的冻结闭环行为程序。/);
  assert.match(starts[0].instructions, /仅在.*demonstrations.*行为/);
});

test('raw recording options reach the native input and public generation without old delivery instructions', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-raw-video-input-'));
  const store = await new Store({ root }).load(), starts = [], turns = [];
  const project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY raw recording', prompt: '仅录制原始成功视频' });
  const studio = await createStudio({ store, port: 0, traceSync: async () => {},
    bridge: { async start() {}, async close() {}, status: () => ({ ready: true }),
      async startThread(options) { starts.push(options); return { id: 'record-thread' }; },
      async turn(id, input) { turns.push({ id, input }); return { id: 'record-turn', status: 'inProgress' }; },
      async steer(id, turnId, input) { turns.push({ id, input }); return { id: turnId, status: 'inProgress' }; } } });
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  const address = await studio.start();
  const response = await fetch(`${address}/api/projects/${project.id}/chat`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ message: project.prompt,
      requiredOutputs: ['demonstrations'], videoOnly: true, recordOnlyVideos: true, recordOnlyTaskIds: ['native_task'] }) });
  assert.equal(response.status, 200, await response.text());
  const current = store.get(project.id);
  assert.equal(current.generation.recordOnlyVideos, true);
  assert.deepEqual(publicProject(current).generation.recordOnlyTaskIds, ['native_task']);
  for (const input of [starts[0].instructions, turns[0].input]) {
    assert.match(input, /recordOnlyVideos=true/);
    assert.match(input, /recordOnlyTaskIds=\["native_task"\]/);
    assert.match(input, /跳过.*独立审核.*交付检查/);
    assert.doesNotMatch(input, /独立内审仍保留|按交付文档绑定 videoOnly|自主召集直接子代理/);
  }
  const generationId = current.generation.id;
  const followup = await fetch(`${address}/api/projects/${project.id}/chat`, { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ message: '继续原任务' }) });
  assert.equal(followup.status, 200, await followup.text());
  assert.equal(current.generation.id, generationId);
  assert.match(turns.at(-1).input, /recordOnlyVideos=true/);
});

test('public raw recording receipt exposes actual video references and skipped checks', () => {
  const project = { id: 'p_record_public', jobs: [], messages: [], events: [], artifacts: {},
    generation: { id: 'g_record_public', status: 'complete', requirementVersion: 1, requestHash: 'r',
      recordOnlyVideos: true, recordOnlyTaskIds: ['native_task'], requiredOutputs: ['demonstrations'] },
    delivery: { schema: 'sceneagent-recorded-videos-v1', accepted: true, status: 'accepted',
      generationId: 'g_record_public', requirementVersion: 1, requestHash: 'r', recordOnlyVideos: true,
      assessedBy: 'user_acceptance_without_review', reviewSkipped: true, checksSkipped: true,
      videos: [{ taskId: 'native_task', videoRef: 'projects/p_record_public/episodes/ep/videos/front.mp4' }] } };
  const shown = publicProject(project).delivery;
  assert.equal(shown.accepted, true);
  assert.equal(shown.reviewSkipped, true);
  assert.equal(shown.checksSkipped, true);
  assert.deepEqual(shown.videos, project.delivery.videos);
  delete project.delivery.recordOnlyVideos;
  assert.equal(publicProject(project).delivery.accepted, false);
  assert.equal(project.delivery.accepted, true);
});

test('old formal consumption receipts stay historical when raw recording becomes current', async t => {
  const { readFile } = await import('node:fs/promises');
  const { default: vm } = await import('node:vm');
  const { contentHash } = await import('../src/store.mjs');
  const source = await readFile(new URL('../src/server.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('    onDelivery: async (project, receipt) => {');
  const body = source.slice(start, source.indexOf('    onJobTerminal:', start)).trim().replace(/^onDelivery: /, '').replace(/,$/, '');
  for (const boundary of ['before', 'immutable', 'pointer']) for (const accepted of [true, false]) await t.test(boundary + ' ' + accepted, async () => {
    const original = { recordOnlyVideos: true, accepted: true, marker: 'SYSTEM_VERIFICATION_ONLY raw receipt' };
    const project = { id: 'p_recording', artifacts: {},
      generation: { id: 'g_recording', status: 'active', requirementVersion: 2, requestHash: 'r' } };
    const switchMode = () => { project.generation.recordOnlyVideos = true; project.delivery = original; project.artifacts.delivery = 'raw-video-reference'; };
    if (boundary === 'before') switchMode();
    const writes = [], events = [];
    const publish = vm.runInNewContext('(' + body + ')', { contentHash, registerArtifacts() {}, publish() {},
      store: { record: async (_id, event) => events.push(event), save: async () => {} },
      deliveryWriter: async (_id, ref, value, options) => {
        writes.push({ ref, value: structuredClone(value), options });
        if (boundary === 'immutable' && options?.immutable || boundary === 'pointer' && ref === 'delivery.json') switchMode();
      } });
    const receipt = { accepted, missing: [], generationId: 'g_recording', requirementVersion: 2,
      requestHash: 'r', requestId: 'old_consumer', revision: 'rev' };
    const result = await publish(project, receipt);
    assert.equal(result.accepted, false);
    assert.equal(result.publicationAborted, true);
    assert.equal(project.delivery, original);
    assert.equal(project.artifacts.delivery, 'raw-video-reference');
    assert.equal(writes.length, boundary === 'pointer' ? 2 : 1);
    assert.equal(writes[0].options.immutable, true);
    assert.equal(writes[0].value.accepted, accepted);
    assert.match(result.missing.join(' '), /原始录像/);
    assert.equal(events.at(-1).type, 'delivery_publication_aborted');
  });
});

test('视频支持完整、指定和末尾区间，拒绝不可满足区间', () => {
  assert.equal(parseRange(undefined, 100), null);
  assert.deepEqual(parseRange('bytes=0-9', 100), { start: 0, end: 9, length: 10 });
  assert.deepEqual(parseRange('bytes=90-', 100), { start: 90, end: 99, length: 10 });
  assert.deepEqual(parseRange('bytes=-20', 100), { start: 80, end: 99, length: 20 });
  for (const invalid of ['bytes=100-', 'bytes=9-2', 'bytes=-0', 'bytes=0-1,4-5', 'bytes=-', 'x']) assert.equal(parseRange(invalid, 100), false);
});

test('项目和产物路径拒绝越界输入', () => {
  assert.ok(validId('p_abc123'));
  for (const invalid of ['../asset', 'a/b', '', null]) assert.equal(validId(invalid), false);
  assert.equal(validateArtifactPath('projects/p_a/revisions/123/preview.glb'), 'projects/p_a/revisions/123/preview.glb');
  for (const invalid of ['/etc/passwd', 'projects/../secret', 'projects//a', 'projects/./a']) assert.throws(() => validateArtifactPath(invalid));
});

test('SSH 参数按字面值引用，保留换行、美元符和单引号', () => {
  assert.equal(quote("a'b\n$HOME"), "'a'\\''b\n$HOME'");
});

test('an SSE client that does not drain is disconnected before its queue grows without bound', async () => {
  const { writeSse } = await import('../src/server.mjs');
  const response = { writableLength: 0, destroyed: false,
    write(packet) { this.writableLength += Buffer.byteLength(packet); return false; },
    destroy() { this.destroyed = true; } };
  for (let index = 0; index < 200; index++) writeSse(response, 'x'.repeat(16384));
  assert.equal(response.destroyed, true);
  assert.ok(response.writableLength <= 1024 * 1024);
});

test('public state derives native discussion, generation and edited delivery without stale mirrors', () => {
  const project = { id: 'p_state', status: 'failed', jobs: [], messages: [], events: [], artifacts: {}, threadId: 'thread' };
  assert.equal(publicProject(project).status, 'idle');
  project.activeTurnId = 'discussion';
  assert.equal(publicProject(project).status, 'thinking');
  project.generation = { id: 'g_state', status: 'waiting_jobs', requirementVersion: 1, requestHash: 'request' };
  assert.equal(publicProject(project).status, 'waiting_jobs');
  project.activeTurnId = null;
  project.generation.status = 'complete';
  project.delivery = { accepted: true, status: 'accepted', generationId: 'g_state', requirementVersion: 1, requestHash: 'request' };
  project.latestBuild = { sourceHash: 'built' };
  project.sourceHash = 'edited';
  const displayed = publicProject(project);
  assert.equal(displayed.status, 'edited');
  assert.equal(displayed.delivery.accepted, false);
  assert.equal(project.delivery.accepted, true, 'the immutable historical receipt is preserved');
});

test('public delivery exposes format entrypoints without broadcasting the full file inventory', () => {
  const project = { id: 'p_exports', jobs: [], messages: [], events: [], artifacts: {},
    generation: { id: 'g_exports', status: 'complete', requirementVersion: 1, requestHash: 'request', deliveryContract: 'producer-exports-v1' },
    delivery: { accepted: true, status: 'accepted', generationId: 'g_exports', requirementVersion: 1, requestHash: 'request',
      producedArtifacts: { frozenManifestRef: 'projects/p_exports/deliveries/artifacts/hash/manifest.json', products: { generation: { atif: 'atif.json' } }, files: [{ artifactRef: 'large-inventory-entry' }] },
      products: { generation: { sampleCount: 3, exports: { atif: 'atif.json' } } } } };
  const shown = publicProject(project);
  assert.equal(shown.generation.deliveryContract, 'producer-exports-v1');
  assert.deepEqual(shown.delivery.products.generation.exports, { atif: 'atif.json' });
  assert.equal(shown.delivery.producedArtifacts.frozenManifestRef, project.delivery.producedArtifacts.frozenManifestRef);
  assert.equal(shown.delivery.producedArtifacts.files, undefined);
  assert.equal(project.delivery.producedArtifacts.files.length, 1);
});

test('a built native scene exposes an unavailable web preview without claiming a preview artifact', () => {
  const rendering = { webPreview: { available: false, reason: 'unsupported preview material' } };
  const shown = publicProject({ id: 'p_preview', jobs: [], messages: [], events: [], artifacts: {},
    latestBuild: { revision: 'native_revision', rendering } });
  assert.equal(shown.latestBuild.revision, 'native_revision');
  assert.deepEqual(shown.latestBuild.rendering, rendering);
  assert.equal(shown.artifacts.preview, undefined);
});

test('a native interruption error does not skip cancellation of other current jobs', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-stop-all-'));
  const store = await new Store({ root }).load();
  const original = await store.create({ name: '停止范围', prompt: '系统验证' });
  store.beginGeneration(original.id);
  original.threadId = 'native-stop';
  for (const id of ['first', 'second']) original.jobs.push({ id, generationId: original.generation.id,
    durable: true, status: 'running', kind: 'observe', payload: { operation: 'submit' } });
  original.jobs.push({ id: 'older', generationId: 'older-generation', durable: true, status: 'running' });
  await store.save();
  const attempts = [];
  const studio = await createStudio({ store, port: 0,
    bridge: { status: () => ({ ready: true }), async close() {},
      async readThread() { return { status: { type: 'active' }, turns: [{ id: 'actual-active', status: 'inProgress' }] }; },
      async interrupt(_thread, turn) { attempts.push(turn); throw new Error('native stop unavailable'); } },
    remote: async request => {
      attempts.push(request.jobId);
      if (request.jobId === 'first') throw new Error('first stop unavailable');
      return { state: 'cancelled', jobId: request.jobId, cursor: 0, events: [] };
    },
    traceSync: async () => { throw new Error('unused test trace sync'); } });
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  const project = store.get(original.id);
  await studio.host.cancel(project);
  assert.deepEqual(new Set(attempts), new Set(['actual-active', 'first', 'second']));
  assert.equal(project.generation.status, 'recovering');
  assert.ok(project.generation.cancelRequestedAt);
  assert.equal(project.jobs[1].status, 'cancelled');
});

test('the existing recovery interval applies elapsed budgets to an active native turn', { timeout: 2000 }, async t => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-active-budget-'));
  const store = await new Store({ root }).load();
  const original = await store.create({ name: '活跃预算', prompt: '系统验证' });
  let running = true, interrupted;
  const stopped = new Promise(resolve => { interrupted = resolve; });
  const studio = await createStudio({ store, port: 0,
    bridge: { async start() {}, status: () => ({ ready: true }), async close() {},
      async readThread() { return { status: { type: running ? 'active' : 'idle' },
        turns: running ? [{ id: 'budget-active-turn', status: 'inProgress' }] : [] }; },
      async interrupt() { running = false; interrupted(); } } });
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  await studio.start();
  const project = store.get(original.id);
  store.beginGeneration(project.id, { timeBudgetMs: 60 * 60_000 });
  project.threadId = 'budget-thread';
  project.activeTurnId = 'budget-active-turn';
  project.generation.startedAt = new Date(Date.now() - 61 * 60_000).toISOString();
  t.mock.timers.tick(15000);
  await stopped;
  await studio.host.recover(project);
  assert.equal(project.generation.status, 'failed');
  assert.match(project.generation.terminalReason, /60 分钟/);
  assert.equal(project.activeTurnId, null);
});

test('explicit HTTP resume keeps the failed generation, native thread and original single prompt', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-resume-'));
  const store = await new Store({ root }).load();
  const original = await store.create({ name: '恢复旧任务', prompt: '系统验证：保留原需求与三产物。' });
  store.beginGeneration(original.id, { timeBudgetMs: null });
  Object.assign(original.generation, { status: 'failed', continuationCount: 24, stopStatus: 'failed',
    terminalReason: '已达到 24 次自动续接预算。', completedAt: new Date().toISOString(), cancelRequestedAt: new Date().toISOString() });
  original.threadId = 'original-native-thread';
  const generationId = original.generation.id, requestHash = original.generation.requestHash;
  const requirements = structuredClone(original.requirements);
  const requestText = await readFile(path.join(store.directory(original.id), 'request.md'), 'utf8');
  await store.save();
  const turns = [], threads = [];
  const studio = await createStudio({ store, port: 0,
    bridge: { async start() {}, status: () => ({ ready: true }), async close() {},
      async readThread(id) { return { id, status: { type: turns.length ? 'active' : 'idle' }, turns: [] }; },
      async resumeThread(id) { threads.push(id); return { id }; },
      async turn(id, input) { turns.push({ id, input }); return { id: 'resumed-turn', status: 'inProgress' }; } } });
  t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
  const address = await studio.start();
  const resume = id => fetch(address+'/api/projects/'+original.id+'/resume', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ generationId: id }) });
  assert.equal((await resume('g_stale')).status, 409);
  assert.equal(store.get(original.id).generation.status, 'failed');
  const response = await resume(generationId);
  assert.equal(response.status, 200, await response.text());
  assert.equal((await resume(generationId)).status, 200);
  const project = store.get(original.id);
  assert.equal(project.generation.id, generationId);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.requestHash, requestHash);
  assert.equal(project.generation.requirementVersion, 1);
  assert.equal(project.generation.timeBudgetMs, null);
  assert.equal(project.generation.continuationCount, 25);
  assert.deepEqual(project.requirements, requirements);
  assert.equal(await readFile(path.join(store.directory(original.id), 'request.md'), 'utf8'), requestText);
  assert.equal(project.events.filter(event => event.type === 'user_message' || event.type === 'requirement_added').length, 0);
  assert.equal(project.events.filter(event => event.type === 'generation_resumed').length, 1);
  assert.deepEqual(threads, ['original-native-thread']);
  assert.equal(turns.length, 1);
  assert.equal(turns[0].id, 'original-native-thread');
  assert.match(turns[0].input, /系统验证：保留原需求与三产物/);
});

test('HTTP resume reattaches the same unloaded native session without a launcher or additional user input', async t => {
  for (const scenario of ['same thread', 'wrong attached id', 'generation changed before attach', 'thread changed before attach',
    'generation changed during attach', 'thread changed during attach']) await t.test(scenario, async t => {
    const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-cold-resume-'));
    const store = await new Store({ root }).load();
    const original = await store.create({ name: '冷启动恢复', prompt: 'SYSTEM_VERIFICATION_ONLY: same original prompt' });
    store.beginGeneration(original.id, { timeBudgetMs: null });
    original.threadId = 'stored-thread';
    Object.assign(original.generation, { status: 'waiting_system', waitingSystem: { requestId: 'original-wait', since: new Date().toISOString() } });
    await store.save();
    let loaded = false, active = false, starts = 0;
    const attaches = [], turns = [], nativeTurns = [];
    const studio = await createStudio({ store, port: 0, traceSync: async () => {},
      bridge: { async start() {
          if (++starts !== 2) return;
          const current = store.get(original.id);
          if (scenario === 'generation changed before attach') current.generation = { ...current.generation, id: 'replacement-generation' };
          if (scenario === 'thread changed before attach') current.threadId = 'replacement-thread';
        }, status: () => ({ ready: true }), async close() {},
        async readThread(id) { return { id, status: { type: loaded ? active ? 'active' : 'idle' : 'notLoaded' }, turns: nativeTurns }; },
        async resumeThread(id, options) {
          attaches.push({ id, cwd: options.cwd }); loaded = true;
          const current = store.get(original.id);
          if (scenario === 'generation changed during attach') current.generation = { ...current.generation, id: 'replacement-generation' };
          if (scenario === 'thread changed during attach') current.threadId = 'replacement-thread';
          return { id: scenario === 'wrong attached id' ? 'other-thread' : id };
        },
        async turn(id, input) { turns.push({ id, input }); active = true; return { id: 'continued-'+turns.length, status: 'inProgress' }; },
        async steer() { assert.fail('cold resume must not steer'); } } });
    t.after(async () => { await studio.close(); await rm(root, { recursive: true, force: true }); });
    const project = store.get(original.id), before = structuredClone(project);
    const address = await studio.start();
    const response = await fetch(address+'/api/projects/'+project.id+'/resume', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ generationId: before.generation.id }) });
    assert.equal(response.status, scenario === 'same thread' ? 200 : 409, await response.text());
    assert.deepEqual(attaches, scenario.includes('before attach') ? [] : [{ id: before.threadId, cwd: store.directory(project.id) }]);
    assert.equal(project.prompt, before.prompt); assert.deepEqual(project.requirements, before.requirements);
    assert.equal(project.events.filter(event => ['user_message', 'requirement_added'].includes(event.type)).length, 0);
    if (scenario === 'same thread') {
      assert.equal(turns.length, 1); assert.equal(turns[0].id, before.threadId);
      assert.match(turns[0].input, /SYSTEM_VERIFICATION_ONLY: same original prompt/);
      assert.equal(project.generation.status, 'active');
      assert.equal(project.events.filter(event => event.type === 'generation_resumed').length, 1);
      await t.test('a previously attached thread that becomes notLoaded is attached again before resuming', async () => {
        await studio.host.waitSystem(project, { projectId: project.id, generationId: before.generation.id,
          requestId: 'wait-after-first-turn', reason: 'SYSTEM_VERIFICATION_ONLY: wait again', evidence: ['fake native turn completed'] });
        const completed = { id: 'continued-1', status: 'completed', items: [] };
        nativeTurns.push(completed); active = false;
        await studio.host.turnCompleted(project, completed);
        loaded = false;
        const again = await fetch(address+'/api/projects/'+project.id+'/resume', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ generationId: before.generation.id }) });
        assert.equal(again.status, 200, await again.text());
        assert.deepEqual(attaches, Array(2).fill({ id: before.threadId, cwd: store.directory(project.id) }));
        assert.equal(turns.length, 2); assert.equal(turns[1].id, before.threadId);
        assert.equal(project.generation.id, before.generation.id); assert.equal(project.threadId, before.threadId);
        assert.equal(project.generation.status, 'active');
        assert.equal(project.events.filter(event => ['user_message', 'requirement_added'].includes(event.type)).length, 0);
      });
    } else {
      assert.deepEqual(turns, []);
      assert.deepEqual(project.generation.waitingSystem, before.generation.waitingSystem);
      assert.equal(project.generation.status, 'waiting_system');
      assert.equal(project.events.filter(event => event.type === 'generation_resumed').length, 0);
    }
  });
});

test('cancellation while publishing a receipt ends with a diagnostic and never republishes the current delivery', async t => {
  const { readFile } = await import('node:fs/promises');
  const { default: vm } = await import('node:vm');
  const { contentHash } = await import('../src/store.mjs');
  const source = await readFile(new URL('../src/server.mjs', import.meta.url), 'utf8');
  const start = source.indexOf('    onDelivery: async (project, receipt) => {');
  const body = source.slice(start, source.indexOf('    onJobTerminal:', start)).trim().replace(/^onDelivery: /, '').replace(/,$/, '');
  for (const boundary of ['immutable', 'pointer']) await t.test(boundary, async () => {
    const project = { id: 'p_publication', artifacts: {}, generation: { id: 'g_publication', status: 'active', requirementVersion: 1, requestHash: 'r' } };
    const writes = [], events = [];
    const context = { contentHash, registerArtifacts() {}, publish() {},
      store: { record: async (_id, event) => events.push(event), save: async () => {} },
      deliveryWriter: async (_id, ref, value, options) => {
        writes.push({ ref, value: structuredClone(value) });
        if (boundary === 'immutable' && options?.immutable || boundary === 'pointer' && ref === 'delivery.json') project.generation.cancelRequestedAt = new Date().toISOString();
      } };
    const publish = vm.runInNewContext('(' + body + ')', context);
    const receipt = { accepted: true, missing: [], generationId: 'g_publication', requirementVersion: 1, requestHash: 'r', requestId: 'delivery', revision: 'rev' };
    const result = await publish(project, receipt);
    assert.equal(result.accepted, false); assert.equal(result.publicationAborted, true);
    assert.equal(project.delivery, undefined); assert.equal(writes[0].value.accepted, true);
    assert.ok(result.candidateArtifactRef); assert.equal(events.at(-1).type, 'delivery_publication_aborted');
    assert.equal(publicProject({ ...project, jobs: [], messages: [], events: [] }).delivery, null);
  });
});


test('public media describes the selected episode without frame tables or pixels', () => {
  const episode={episode:'ep',revision:'rev',media:{primaryCameraId:'front',cameras:[{cameraId:'front',required:true,status:'available',
    video:'projects/p_test/episodes/ep/videos/front.mp4',frameCount:3,firstTime:0,lastTime:.08,frames:[{image:'private-pixels'}]}]},
    artifacts:{video:'projects/p_test/episodes/ep/videos/front.mp4',annotations:'projects/p_test/episodes/ep/annotations.json'}};
  const displayed=publicProject({id:'p_test',jobs:[],messages:[],events:[],artifacts:{video:'/stale-other-episode.mp4'},latestBuild:{revision:'rev'},latestEpisode:episode});
  assert.equal(displayed.latestEpisode.media.primaryCameraId,'front');
  assert.equal(displayed.latestEpisode.media.cameras[0].cameraId,'front');
  assert.equal(JSON.stringify(displayed).includes('private-pixels'),false);
  assert.equal(displayed.latestEpisode.artifacts.annotations,episode.artifacts.annotations);
  assert.match(displayed.artifacts.video,/ep%2Fvideos%2Ffront\.mp4/);
});

test('waiting HTTP messages retain requirements and conversation but never dispatch or steer', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'sceneagent-wait-chat-'));const store=await new Store({root}).load();
  const original=await store.create({name:'等待中的工程',prompt:'三产物完整要求'});store.beginGeneration(original.id,{timeBudgetMs:null});
  original.threadId='native-root';await store.save();const sends=[];
  const studio=await createStudio({store,port:0,bridge:{async start(){},status:()=>({ready:true}),async close(){},
    async readThread(id){return{id,status:{type:'idle'},turns:[]};},async resumeThread(){sends.push('resume');},
    async turn(){sends.push('turn');},async steer(){sends.push('steer');}}});
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const project=store.get(original.id);
  await studio.host.waitSystem(project,{projectId:project.id,generationId:project.generation.id,requestId:'wait',reason:'tool error',evidence:['HTTP 500']});
  const address=await studio.start();
  for(const mode of ['generate','discussion']){
    const response=await fetch(address+'/api/projects/'+project.id+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,message:'保留补充 '+mode})});
    assert.equal(response.status,200,await response.text());
  }
  assert.deepEqual(sends,[]);assert.equal(project.generation.status,'waiting_system');
  assert.equal(project.requirements.at(-1).text,'保留补充 generate');assert.equal(project.messages.at(-1).content,'保留补充 discussion');
  const shown=publicProject(project);assert.equal(shown.generation.waitingSystem.requestId,'wait');assert.equal(shown.generation.systemWaitMs,0);
  await studio.codex.onEvent({method:'bridge/status',params:{ready:false}});
  await studio.close();assert.equal(project.generation.status,'waiting_system');
});

test('overload recovery rejects running work, other errors and a changed root tail', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  for (const scenario of ['other error', 'running turn', 'active root', 'changed tail', 'active child']) await t.test(scenario, async () => {
    const failed = { id: 'r1', status: 'failed', error: { codexErrorInfo: 'serverOverloaded' }, items: [] };
    if (scenario === 'other error') failed.error.codexErrorInfo = 'unauthorized';
    if (scenario === 'running turn') failed.status = 'inProgress';
    if (scenario === 'active child') failed.items.push({ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' });
    const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } };
    const reader = createNativeWorkReader({ codex: {
      async readThread(id) { return { id, parentThreadId: id === 'child' ? 'root' : undefined,
        status: { type: id === 'child' || scenario === 'active root' ? 'active' : 'systemError' } }; },
      async listTurns() { return { data: [{ ...failed, id: scenario === 'changed tail' ? 'r2' : 'r1' }], nextCursor: null }; }
    } });
    const result = await reader(project, { id: 'root', status: { type: 'systemError' }, turns: [failed] });
    assert.equal(result.ready, false);
  });
});

test('a terminal 429 root reconciles failed children but waits a full minute before continuation', async () => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const failure = { responseTooManyFailedAttempts: { httpStatusCode: 429 } };
  const failed = { id: 'r1', status: 'failed', startedAt: 1794991317, completedAt: 1794991471,
    error: { codexErrorInfo: failure }, items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' }] };
  const child = { id: 'c1', rootTurnId: 'r1', status: 'failed', error: { codexErrorInfo: failure } };
  const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } };
  const native = { id: 'root', status: { type: 'systemError' }, turns: [failed] };
  let at = failed.completedAt * 1000; const reads = [];
  const reader = createNativeWorkReader({ now: () => at, codex: {
    async readThread(id) { reads.push(id); return { id, parentThreadId: id === 'child' ? 'root' : null, status: { type: 'systemError' } }; },
    async listTurns(id) { return { data: [id === 'child' ? child : failed], nextCursor: null }; },
    async listItems() { return { data: [], nextCursor: null }; }
  } });
  for (const elapsed of [0, 15000, 30000, 59999]) {
    at = failed.completedAt * 1000 + elapsed; reads.length = 0;
    const result = await reader(project, native);
    assert.equal(result.ready, false); assert.match(result.reason, /429.*退避/);
    assert.equal(reads.filter(id => id === 'child').length, 2, 'terminal child provenance is reconciled while waiting');
  }
  at = failed.completedAt * 1000 + 60000;
  assert.deepEqual(await reader(project, native), { ready: true, completed: [['child', 'c1', 'failed']] });
});

test('429 recovery keeps root identity, terminal state, error and failure clock checks', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  for (const scenario of ['404', 'other root error', 'running root', 'active root', 'changed tail', 'changed error', 'changed clock', 'missing clock']) await t.test(scenario, async () => {
    const failed = { id: 'r1', status: 'failed', completedAt: 1794991471,
      error: { codexErrorInfo: { responseTooManyFailedAttempts: { httpStatusCode: 429 } } }, items: [] };
    if (scenario === '404') failed.error.codexErrorInfo.responseTooManyFailedAttempts.httpStatusCode = 404;
    if (scenario === 'other root error') failed.error.codexErrorInfo = { httpConnectionFailed: { httpStatusCode: 429 } };
    if (scenario === 'running root') failed.status = 'inProgress';
    if (scenario === 'missing clock') delete failed.completedAt;
    const tail = structuredClone(failed);
    if (scenario === 'changed tail') tail.id = 'r2';
    if (scenario === 'changed error') tail.error.codexErrorInfo.responseTooManyFailedAttempts.httpStatusCode = 404;
    if (scenario === 'changed clock') tail.completedAt++;
    const reader = createNativeWorkReader({ now: () => 1794991471 * 1000 + 60000, codex: {
      async readThread(id) { return { id, status: { type: scenario === 'active root' ? 'active' : 'systemError' } }; },
      async listTurns() { return { data: [tail], nextCursor: null }; }
    } });
    const result = await reader({ threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } },
      { id: 'root', status: { type: 'systemError' }, turns: [failed] });
    assert.equal(result.ready, false);
  });
});

test('native 429 backoff uses the real completion event when the turn clock is absent', async () => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const failed = { id: 'r1', status: 'failed', error: { codexErrorInfo: { responseTooManyFailedAttempts: { httpStatusCode: 429 } } }, items: [] };
  const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] }, events: [
    { type: 'codex', method: 'turn/completed', child: false, generationId: 'g', time: '2026-11-18T08:44:31.926Z',
      params: { threadId: 'root', turn: failed } }
  ] };
  let at = Date.parse(project.events[0].time) + 59999;
  const reader = createNativeWorkReader({ now: () => at, codex: {
    async readThread(id) { return { id, status: { type: 'systemError' } }; },
    async listTurns() { return { data: [failed], nextCursor: null }; }
  } });
  const native = { id: 'root', status: { type: 'systemError' }, turns: [failed] };
  assert.equal((await reader(project, native)).ready, false);
  at++;
  assert.equal((await reader(project, native)).ready, true);
});

test('the exact terminal request-send disconnection reconciles children and keeps the same minute backoff', async () => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const failed = { id: 'r1', status: 'failed', completedAt: 1794991471,
    error: { codexErrorInfo: 'other', message: 'stream disconnected before completion: error sending request' },
    items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' }] };
  const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } };
  const native = { id: 'root', status: { type: 'systemError' }, turns: [failed] };
  let at = failed.completedAt * 1000 + 59999; const reads = [];
  const reader = createNativeWorkReader({ now: () => at, codex: {
    async readThread(id) { reads.push(id); return { id, parentThreadId: id === 'child' ? 'root' : null,
      status: { type: id === 'child' ? 'idle' : 'systemError' } }; },
    async listTurns(id) { return { data: [id === 'child' ? { id: 'c1', rootTurnId: 'r1', status: 'completed' } : failed], nextCursor: null }; },
    async listItems() { return { data: [], nextCursor: null }; }
  } });
  const waiting = await reader(project, native);
  assert.equal(waiting.ready, false); assert.match(waiting.reason, /网络.*退避/);
  assert.equal(reads.filter(id => id === 'child').length, 2);
  at++;
  assert.deepEqual(await reader(project, native), { ready: true, completed: [['child', 'c1', 'completed']] });
});

test('request-send recovery rejects content refusal, other messages and a changed root error', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const requestError = 'stream disconnected before completion: error sending request';
  const contentRefusal = 'stream disconnected before completion: Image processing blocked due to content policy violation.';
  for (const scenario of ['content refusal', 'other message', 'changed message', 'active child', 'running root']) await t.test(scenario, async () => {
    const failed = { id: 'r1', status: 'failed', completedAt: 1794991471, items: [],
      error: { codexErrorInfo: 'other', message: requestError } };
    if (scenario === 'content refusal') failed.error.message = contentRefusal;
    if (scenario === 'other message') failed.error.message += '.';
    if (scenario === 'running root') failed.status = 'inProgress';
    if (scenario === 'active child') failed.items.push({ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' });
    const tail = structuredClone(failed);
    if (scenario === 'changed message') tail.error.message = contentRefusal;
    const reader = createNativeWorkReader({ now: () => failed.completedAt * 1000 + 60000, codex: {
      async readThread(id) { return { id, parentThreadId: id === 'child' ? 'root' : null,
        status: { type: id === 'child' ? 'active' : 'systemError' } }; },
      async listTurns() { return { data: [tail], nextCursor: null }; }
    } });
    assert.equal((await reader({ threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } },
      { id: 'root', status: { type: 'systemError' }, turns: [failed] })).ready, false);
  });
});

test('the existing generation host resumes a rate-limited root once after backoff with its original identity', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const { GenerationHost } = await import('../src/generation.mjs');
  const root = await mkdtemp(path.join(tmpdir(), 'sceneagent-rate-limit-recovery-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load();
  const project = await store.create({ name: 'SYSTEM_VERIFICATION_ONLY 429 recovery', prompt: 'original task' });
  store.beginGeneration(project.id); project.threadId = 'root'; project.activeTurnId = 'r1';
  const generation = project.generation;
  const failed = { id: 'r1', status: 'failed', completedAt: 1794991471, items: [],
    error: { codexErrorInfo: { responseTooManyFailedAttempts: { httpStatusCode: 429 } } } };
  let at = failed.completedAt * 1000, sent = false; const calls = [];
  const codex = {
    async readThread(id, { includeTurns }) { return { id, status: { type: sent ? 'active' : 'systemError' },
      turns: includeTurns ? [failed, ...(sent ? [{ id: 'r2', status: 'inProgress', items: [] }] : [])] : [] }; },
    async listTurns() { return { data: [failed], nextCursor: null }; }
  };
  const host = new GenerationHost({ store, codex, now: () => at, readNativeWork: createNativeWorkReader({ codex, now: () => at }),
    continueTurn: async (current, input, context) => { context.beforeSend(); calls.push([current, input]); sent = true; return { id: 'r2', status: 'inProgress' }; } });
  await host.turnCompleted(project, failed);
  for (const elapsed of [15000, 30000, 59999]) { at = failed.completedAt * 1000 + elapsed; await host.recover(project); }
  assert.equal(calls.length, 0); assert.equal(generation.continuationCount, 0);
  at = failed.completedAt * 1000 + 60000;
  await Promise.all([host.recover(project), host.recover(project)]);
  assert.equal(calls.length, 1); assert.equal(calls[0][0], project); assert.match(calls[0][1], /original task/);
  assert.equal(project.threadId, 'root'); assert.equal(project.generation, generation); assert.equal(project.activeTurnId, 'r2');
});

test('system-wait recovery accepts a failed child only after all its native turns are confirmed terminal', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  for (const scenario of ['failed child', 'no system wait', 'completed tail', 'running older turn', 'reactivated child', 'changed tail']) await t.test(scenario, async () => {
    const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'],
      ...(scenario === 'no system wait' ? {} : { waitingSystem: { requestId: 'transport-repair' } }) } };
    const native = { id: 'root', status: { type: 'idle' }, turns: [{ id: 'r1', status: 'completed',
      items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' }] }] };
    const failed = { id: 'c1', rootTurnId: 'r1', status: scenario === 'completed tail' ? 'completed' : 'failed',
      error: { codexErrorInfo: 'other', message: 'invalid_encrypted_content' } };
    let reads = 0;
    const reader = createNativeWorkReader({ codex: {
      async readThread(id) { return { id, parentThreadId: id === 'child' ? 'root' : null,
        status: { type: id === 'root' ? 'idle' : scenario === 'reactivated child' && ++reads === 2 ? 'active' : 'systemError' } }; },
      async listTurns(id, options) { return { data: options.limit === 1 ? [{ ...failed, id: scenario === 'changed tail' ? 'c2' : 'c1' }]
        : [...(scenario === 'running older turn' ? [{ id: 'c0', rootTurnId: 'r1', status: 'inProgress' }] : []), failed], nextCursor: null }; },
      async listItems() { return { data: [], nextCursor: null }; }
    } });
    const before = structuredClone(failed), result = await reader(project, native);
    assert.equal(result.ready, ['failed child', 'no system wait'].includes(scenario), result.reason);
    assert.deepEqual(failed, before);
    if (result.ready) assert.deepEqual(result.completed, [['child', 'c1', 'failed']]);
  });
});

test('native work reader scopes child references to current generation and reads only status headers', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g_current',completedTurnIds:['current']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'old',status:'completed',items:[{id:'oldspawn',type:'subAgentActivity',kind:'started',agentThreadId:'old-child'}]},
    {id:'current',status:'completed',items:[{id:'spawn',type:'subAgentActivity',kind:'started',agentThreadId:'child'}]}]};
  let status='active',last='child-1';const reads=[];
  const reader=createNativeWorkReader({codex:{
    async readThread(id,options){reads.push([id,options]);return{id,parentThreadId:'root',status:{type:status}};},
    async listTurns(id,options){assert.equal(id,'child');assert.ok([1,50].includes(options.limit));assert.equal(options.itemsView,'notLoaded');return{data:[{id:last,status:'completed'}],nextCursor:null};},
    async listItems(){return{data:[],nextCursor:null};}}});
  assert.equal((await reader(project,native)).ready,false);
  status='idle';assert.deepEqual((await reader(project,native)).completed,[['child','child-1','completed']]);
  last='child-2';assert.deepEqual((await reader(project,native)).completed,[['child','child-2','completed']]);
  assert.ok(reads.every(([id,options])=>['child','root'].includes(id)&&options.includeTurns===false));
});

test('native work reader treats unknown namespace, unfinished communication and foreign child as unresolved', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['current']}};
  for(const item of [
    {id:'pending',type:'collabAgentToolCall',tool:'sendInput',status:'inProgress',receiverThreadIds:['child']},
    {id:'unknown',type:'collabAgentToolCall',tool:'spawnAgent',status:'completed'},
    {id:'foreign',type:'subAgentActivity',kind:'started',agentThreadId:'child'}]){
    const reader=createNativeWorkReader({codex:{async readThread(){return{id:'child',parentThreadId:'other',status:{type:'idle'}};}}});
    const result=await reader(project,{id:'root',status:{type:'idle'},turns:[{id:'current',status:'completed',items:[item]}]});
    assert.equal(result.ready,false);assert.match(result.reason,/原生/);
  }
});

test('unloaded child history must be terminal and scoped before native work can continue', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['r1']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'r1',status:'completed',items:[{type:'subAgentActivity',kind:'started',agentThreadId:'child'}]}]};
  let childState='notLoaded',turnState='completed',rootState='idle',tail='c1',completePage=true,reads=0,reactivate=false;
  const reader=createNativeWorkReader({codex:{
    async readThread(id){return{id,parentThreadId:id==='child'?'root':null,status:{type:id==='root'?rootState:reactivate&&++reads===2?'active':childState}};},
    async listTurns(id,options){const data=[{id:options.limit===1?tail:'c1',rootTurnId:'r1',status:turnState}];return completePage?{data,nextCursor:null}:{data};},
    async listItems(){return{data:[],nextCursor:null};}}});
  for(const state of ['completed','failed','interrupted']){
    turnState=state;const result=await reader(project,native);
    assert.equal(result.ready,true,result.reason);assert.deepEqual(result.completed,[['child','c1',state]]);
  }
  turnState='inProgress';assert.equal((await reader(project,native)).ready,false);turnState='completed';
  for(const state of ['active','systemError','unknown',undefined]){childState=state;assert.equal((await reader(project,native)).ready,false);}
  childState='notLoaded';rootState='notLoaded';assert.equal((await reader(project,native)).ready,false);rootState='idle';
  assert.equal((await reader(project,{...native,status:{type:'notLoaded'}})).ready,false);
  completePage=false;assert.equal((await reader(project,native)).ready,false);completePage=true;
  tail='new-turn';assert.equal((await reader(project,native)).ready,false);tail='c1';
  reactivate=true;reads=0;assert.equal((await reader(project,native)).ready,false);
});

test('native work reader permits retry after a confirmed failure before any root thread existed', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const reader=createNativeWorkReader({codex:{readThread:async()=>assert.fail('no native thread exists')}});
  assert.deepEqual(await reader({threadId:null,generation:{id:'g',completedTurnIds:[]}},null),{ready:true,completed:[]});
  assert.equal((await reader({threadId:null,generation:{id:'g',completedTurnIds:['unreconciled']}},null)).ready,false);
});

test('native work follows a scoped child delegation and waits for an active grandchild', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['r1']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'r1',status:'completed',items:[{type:'subAgentActivity',kind:'started',agentThreadId:'child'}]}]};
  const queried=[];let grandStatus='active';
  const reader=createNativeWorkReader({codex:{
    async readThread(id){queried.push(id);return{id,parentThreadId:id==='child'?'root':id==='grand'?'child':null,status:{type:id==='grand'?grandStatus:'idle'}};},
    async listTurns(id){return{data:[{id:id==='child'?'c1':'g1',rootTurnId:'r1',status:'completed'}],nextCursor:null};},
    async listItems(id,{turnId}){return{data:id==='child'?[{turnId,item:{type:'subAgentActivity',kind:'started',agentThreadId:'grand'}}]:[],nextCursor:null};}}});
  assert.equal((await reader(project,native)).ready,false);assert.ok(queried.includes('grand'));
  grandStatus='idle';assert.deepEqual((await reader(project,native)).completed,[['child','c1','completed'],['grand','g1','completed']]);
});

test('native work retains older scoped delegation sources without scanning unrelated turns', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['r1']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'r1',items:[{type:'subAgentActivity',kind:'started',agentThreadId:'child'}]}]};
  const loaded=[];
  const reader=createNativeWorkReader({codex:{
    async readThread(id){return{id,parentThreadId:id==='child'?'root':'child',status:{type:id==='grand'?'active':'idle'}};},
    async listTurns(id,options){const rows=[{id:'old-unrelated',rootTurnId:'old-root',status:'completed'},{id:'c1',rootTurnId:'r1',status:'completed'},{id:'c2',rootTurnId:'c1',status:'completed'}];return{data:options.limit===1?[rows.at(-1)]:rows,nextCursor:null};},
    async listItems(id,{turnId,cursor}){loaded.push(turnId);assert.notEqual(turnId,'old-unrelated');return{data:turnId==='c1'?[{turnId,item:{type:'subAgentActivity',kind:'started',agentThreadId:'grand'}}]:[],nextCursor:null};}}});
  assert.equal((await reader(project,native)).ready,false);assert.ok(loaded.includes('c1'));
});

test('recursive native references reject cycles, parent conflicts and incomplete item pages', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['r1']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'r1',items:[{type:'subAgentActivity',kind:'started',agentThreadId:'child'}]}]};
  for(const kind of ['cycle','foreign-parent','incomplete']){
    const reader=createNativeWorkReader({codex:{
      async readThread(id){return{id,parentThreadId:id==='child'?'root':'wrong-parent',status:{type:'idle'}};},
      async listTurns(){return{data:[{id:'c1',rootTurnId:'r1',status:'completed'}],nextCursor:null};},
      async listItems(id,{turnId}){const data=[{turnId,item:{type:'subAgentActivity',kind:'started',agentThreadId:kind==='cycle'?'root':'grand'}}];return kind==='incomplete'?{data}:{data,nextCursor:null};}}});
    assert.equal((await reader(project,native)).ready,false,kind);
  }
});

test('completed native communication to a proven root, ancestor or peer is not a new delegation', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const project={threadId:'root',generation:{id:'g',completedTurnIds:['r1']}};
  const native={id:'root',status:{type:'idle'},turns:[{id:'r1',items:['child','peer'].map(agentThreadId=>({type:'subAgentActivity',kind:'started',agentThreadId}))}]};
  let activePeer=false;
  const reader=createNativeWorkReader({codex:{
    async readThread(id){return{id,parentThreadId:id==='grand'?'child':id==='root'?null:'root',status:{type:id==='peer'&&activePeer?'active':'idle'}};},
    async listTurns(id){return{data:[{id:id+'-turn',rootTurnId:'r1',status:'completed'}],nextCursor:null};},
    async listItems(id,{turnId}){
      const data=id==='child'?[{type:'subAgentActivity',kind:'started',agentThreadId:'grand'},
        {type:'collabAgentToolCall',tool:'sendInput',status:'completed',receiverThreadIds:['root','peer']}]
        :id==='grand'?[{type:'collabAgentToolCall',tool:'sendInput',status:'completed',receiverThreadIds:['child','root','peer']}]:[];
      return{data:data.map(item=>({turnId,item})),nextCursor:null};}}});
  const result=await reader(project,native);assert.equal(result.ready,true,result.reason);
  assert.deepEqual(result.completed.map(row=>row[0]),['child','grand','peer']);
  activePeer=true;assert.equal((await reader(project,native)).ready,false);
});

test('communication cannot promote an unknown or older-generation receiver into a current child', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');
  const reader=createNativeWorkReader({codex:{async readThread(id){return{id,status:{type:'idle'}};}}});
  const result=await reader({threadId:'root',generation:{id:'g',completedTurnIds:['r1']}},
    {id:'root',status:{type:'idle'},turns:[{id:'old',items:[{type:'subAgentActivity',kind:'started',agentThreadId:'old-child'}]},
      {id:'r1',items:[{type:'collabAgentToolCall',tool:'sendInput',status:'completed',receiverThreadIds:['old-child']}]}]});
  assert.equal(result.ready,false);assert.match(result.reason,/来源/);
});

test('a failed collaboration still checks an actual declared receiver before concluding idle', async () => {
  const {createNativeWorkReader}=await import('../src/server.mjs');let childRead=false;
  const reader=createNativeWorkReader({codex:{async readThread(id){if(id==='child')childRead=true;return{id,parentThreadId:'root',status:{type:id==='child'?'active':'idle'}};}}});
  const result=await reader({threadId:'root',generation:{id:'g',completedTurnIds:['r1']}},
    {id:'root',status:{type:'idle'},turns:[{id:'r1',items:[{type:'collabAgentToolCall',tool:'spawnAgent',status:'failed',receiverThreadIds:['child']}]}]});
  assert.equal(result.ready,false);assert.equal(childRead,true);
});

test('an invoked native RPC cannot claim it was never sent through its own error property', async t => {
  const root=await mkdtemp(path.join(tmpdir(),'sceneagent-sent-error-'));const store=await new Store({root}).load();
  const project=await store.create({name:'已调用输入边界',prompt:'SYSTEM_VERIFICATION_ONLY'});store.beginGeneration(project.id,{timeBudgetMs:null});
  project.threadId='root';await store.save();let invoked=0;
  const rpcError=Object.assign(new Error('RPC rejected after invocation'),{status:409,nativeInputNotSent:true});
  const studio=await createStudio({store,bridge:{async start(){},async close(){},status:()=>({ready:true}),
    async readThread(id){return{id,status:{type:'idle'},turns:[]};},async resumeThread(){},
    async turn(){invoked++;throw rpcError;}}});
  t.after(async()=>{await studio.close();await rm(root,{recursive:true,force:true});});
  const current=store.get(project.id);await studio.host.recover(current);
  assert.equal(invoked,1);assert.equal(rpcError.nativeInputNotSent,false);
  assert.ok(current.generation.pendingContinuation.dispatchAttemptedAt);
  assert.equal(current.events.at(-1).type,'continuation_uncertain');
});

test('public environment choices retain older immutable layouts beyond the recent-job window without a second registry', () => {
  const jobs = [{ id: 'layout-job', kind: 'build', status: 'completed', result: { projectId: 'p_choices', revision: 'layout_old',
    capabilities: { stageSplit: true }, tasks: [{ id: 'task', rubric: { private: 'not for choice UI' } }] } },
    ...Array.from({ length: 35 }, (_, index) => ({ id: 'diagnostic-'+index, kind: 'observe', status: 'completed' }))];
  const project = { id: 'p_choices', jobs, messages: [], events: [], artifacts: {} };
  const result = publicProject(project);
  assert.equal(result.jobs.some(row => row.id === 'layout-job'), false);
  assert.deepEqual(result.environmentRevisions, [{ revision: 'layout_old', capabilities: { stageSplit: true }, tasks: [{ id: 'task' }] }]);
  assert.equal(project.environmentRevisions, undefined);
});

test('accepted robot-only delivery stays current after changing behavior while the frozen environment source stays old', () => {
  const project = { id: 'p_collection', jobs: [], messages: [], events: [], artifacts: {}, sourceHash: 'new-behavior-workspace',
    generation: { id: 'new-collection', status: 'complete', requirementVersion: 2, requestHash: 'collection-request', requiredOutputs: ['demonstrations'] },
    latestBuild: { projectId: 'p_collection', revision: 'old-environment', sourceHash: 'old-environment-workspace', tasks: [] },
    delivery: { accepted: true, status: 'accepted', generationId: 'new-collection', requirementVersion: 2,
      requestHash: 'collection-request', sourceHash: 'new-behavior-workspace', products: { demonstrations: { passed: true } } } };
  const result = publicProject(project);
  assert.equal(result.delivery.accepted, true);
  assert.equal(result.status, 'complete');
  project.sourceHash = 'edited-after-acceptance';
  assert.equal(publicProject(project).delivery.accepted, false);
});

test('public layout splits come from accepted environment delivery and cannot be overwritten by later collection labels', () => {
  const environmentRefs = ['train', 'validation', 'test'].map(split => ({ revision: 'layout_'+split, split }));
  const originalDelivery = { accepted: true, artifactRef: 'projects/p_split/deliveries/environment/delivery.json',
    generationId: 'environment-generation', environmentRefs, products: { evaluation: { passed: true } } };
  const collectionDelivery = { accepted: true, artifactRef: 'projects/p_split/deliveries/collection/delivery.json',
    generationId: 'collection-generation', environmentRefs: [{ revision: 'layout_test', split: 'train' }],
    products: { demonstrations: { passed: true } } };
  const project = { id: 'p_split', messages: [], events: [], artifacts: {}, delivery: collectionDelivery,
    jobs: [...environmentRefs.map(ref => ({ kind: 'build', status: 'completed', result: { projectId: 'p_split',
      revision: ref.revision, capabilities: { stageSplit: true }, tasks: [{ id: 'task' }] } })),
      { kind: 'consume', status: 'completed', delivery: originalDelivery },
      { kind: 'consume', status: 'completed', delivery: { ...originalDelivery, accepted: false,
        environmentRefs: [{ revision: 'layout_test', split: 'train' }] } },
      { kind: 'consume', status: 'completed', delivery: collectionDelivery }] };
  const before = JSON.stringify(project);
  const choices = publicProject(project).environmentRevisions;
  assert.deepEqual(choices.map(({ revision, split }) => ({ revision, split })), environmentRefs);
  assert.ok(choices.every(row => row.delivery.artifactRef === originalDelivery.artifactRef));
  assert.equal(JSON.stringify(project), before, 'deriving labels must not change environment or collection receipts');
});

test('context maintenance proves terminal native work without permitting permanent-error continuation', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  for (const message of ['stream disconnected before completion: Image processing blocked due to content policy violation.', 'invalid_encrypted_content']) await t.test(message, async () => {
    const error = { codexErrorInfo: 'other', message };
    const root = { id: 'r1', status: 'failed', completedAt: 1794991471, error,
      items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' }] };
    const child = { id: 'c1', rootTurnId: 'r1', status: 'failed', completedAt: 1794991482, error };
    const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } };
    const native = { id: 'root', status: { type: 'systemError' }, turns: [root] };
    const before = JSON.stringify([project, native]);
    const reader = createNativeWorkReader({ codex: {
      async readThread(id) { return { id, parentThreadId: id === 'child' ? 'root' : null, status: { type: 'systemError' } }; },
      async listTurns(id) { return { data: [id === 'child' ? child : root], nextCursor: null }; },
      async listItems() { return { data: [], nextCursor: null }; }
    } });
    assert.equal((await reader(project, native)).ready, false);
    assert.deepEqual(await reader(project, native, { purpose: 'context-maintenance' }), { ready: true, completed: [['child', 'c1', 'failed']] });
    assert.equal((await reader(project, native)).ready, false);
    assert.equal(JSON.stringify([project, native]), before, 'maintenance must not alter errors, native history or generation identity');
  });
});

test('context maintenance refuses active work, incomplete timestamps/pages and changed native tails', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  for (const scenario of ['active root', 'older active root', 'active child', 'active child turn', 'older active child', 'root tail id',
    'root tail error', 'root tail time', 'child tail id', 'child tail error', 'child tail time', 'missing root time', 'missing child time', 'incomplete pages', 'foreign child']) await t.test(scenario, async () => {
    const error = { codexErrorInfo: 'other', message: 'invalid_encrypted_content' };
    const root = { id: 'r1', status: 'failed', completedAt: 1794991471, error,
      items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'child' }] };
    const child = { id: 'c1', rootTurnId: 'r1', status: 'failed', completedAt: 1794991482, error };
    if (scenario === 'active child turn') child.status = 'inProgress';
    if (scenario === 'missing root time') delete root.completedAt;
    if (scenario === 'missing child time') delete child.completedAt;
    const changed = (turn, prefix) => {
      const value = structuredClone(turn);
      if (scenario === prefix+' tail id') value.id += '-changed';
      if (scenario === prefix+' tail error') value.error.message = 'different error';
      if (scenario === prefix+' tail time') value.completedAt++;
      return value;
    };
    const reader = createNativeWorkReader({ codex: {
      async readThread(id) { return { id, parentThreadId: id === 'child' ? scenario === 'foreign child' ? 'foreign' : 'root' : null,
        status: { type: scenario === 'active root' && id === 'root' || scenario === 'active child' && id === 'child' ? 'active' : 'systemError' } }; },
      async listTurns(id, options) {
        const data = options.limit === 1 ? [changed(id === 'root' ? root : child, id)]
          : [...(scenario === 'older active child' ? [{ id: 'c0', rootTurnId: 'r1', status: 'inProgress' }] : []), child];
        return scenario === 'incomplete pages' ? { data } : { data, nextCursor: null };
      },
      async listItems() { return { data: [], nextCursor: null }; }
    } });
    const native = { id: 'root', status: { type: 'systemError' }, turns: [
      ...(scenario === 'older active root' ? [{ id: 'r0', status: 'inProgress' }] : []), root] };
    assert.equal((await reader({ threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } }, native,
      { purpose: 'context-maintenance' })).ready, false);
  });
});

test('context maintenance checks older root delegations and does not inherit retry backoff', async () => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const root = { id: 'r1', status: 'failed', completedAt: 1794991471, items: [],
    error: { codexErrorInfo: { responseTooManyFailedAttempts: { httpStatusCode: 429 } } } };
  const project = { threadId: 'root', generation: { id: 'g', completedTurnIds: ['r1'] } };
  const native = { id: 'root', status: { type: 'systemError' }, turns: [root] };
  const reader = createNativeWorkReader({ now: () => root.completedAt*1000, codex: {
    async readThread(id) { return { id, parentThreadId: id === 'old-child' ? 'root' : null,
      status: { type: id === 'old-child' ? 'active' : 'systemError' } }; },
    async listTurns() { return { data: [root], nextCursor: null }; }
  } });
  assert.equal((await reader(project, native)).ready, false);
  assert.equal((await reader(project, native, { purpose: 'context-maintenance' })).ready, true);
  native.turns.unshift({ id: 'older-generation', status: 'completed', completedAt: 1794991000, error: null,
    items: [{ type: 'subAgentActivity', kind: 'started', agentThreadId: 'old-child' }] });
  assert.equal((await reader(project, native, { purpose: 'context-maintenance' })).ready, false);
});
