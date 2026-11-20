import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, rename, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createStudio, publicProject } from '../src/server.mjs';
import { Store, contentHash } from '../src/store.mjs';
import { fixture as nativeReviewFixture } from './fixtures/review-source.mjs';

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  const deadline = Date.now() + 2000;
  while (!predicate()) { if (Date.now() >= deadline) assert.fail('expected asynchronous event did not arrive'); await wait(5); }
}

async function fixture(t, { bridge = {}, sources, updateSources, remote, traceSync, producedArtifactsConsumer } = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), 'sceneagent-integration-'));
  const store = await new Store({ root: directory }).load();
  let project = await store.create({ name: '测试场景', prompt: '' });
  const oldDirectory = store.directory(project.id);
  project.id = 'p_test';
  await rename(oldDirectory, store.directory(project.id));
  await store.save();
  if (sources) {
    const original = store.sourceSnapshot.bind(store);
    store.sourceSnapshot = async (...args) => { await sources(); return original(...args); };
  }
  if (updateSources) store.updateSources = updateSources;
  const remoteJobs = new Map(), remoteCalls = [], controls = [];
  const durableRemote = async (payload, options = {}) => {
    remoteCalls.push(payload);
    if (payload.operation === 'submit') {
      const state = { jobId: payload.jobId, state: 'running', cursor: 1, events: [{ sequence: 1, type: 'submitted' }], result: null };
      remoteJobs.set(payload.jobId, state);
      const controller = new AbortController(); controls.push(controller);
      Promise.resolve().then(() => remote ? remote({ ...payload.task, projectId: project.id }, { signal: controller.signal })
        : { revision: 'revision-test', tasks: [{ id: 'push' }], artifacts: {} }).then(result => {
          Object.assign(state, { state: 'completed', result: { projectId: project.id, tasks: [{ id: 'push' }], ...result }, cursor: 2 });
          state.events.push({ sequence: 2, type: 'result', result: state.result });
        }, error => { state.state = controller.signal.aborted ? 'cancelled' : 'failed'; state.error = error.message; });
      return { ...state };
    }
    if (payload.operation === 'job') {
      const state = remoteJobs.get(payload.jobId);
      if (payload.action === 'cancel') { state.state = 'cancelled'; }
      return { ...state, events: state.events.filter(row => row.sequence > (payload.cursor || 0)) };
    }
    if (payload.operation === 'inspect') return { projectId: project.id, built: false, worldContext: null };
    if (remote) return remote(payload, options);
    return payload.operation === 'health' ? { ready: true } : { items: [] };
  };
  const nativeTurns = new Map();
  const codex = {
    async start() {}, status: () => ({ ready: true }), async close() {},
    async startThread() { return { id: 'thread-test' }; }, async resumeThread() {},
    async readThread() {
      const turns = [...nativeTurns.values()];
      if (project.activeTurnId && !nativeTurns.has(project.activeTurnId)) turns.push({ id: project.activeTurnId, status: 'inProgress', items: [] });
      return { id: 'thread-test', status: { type: turns.some(turn => turn.status === 'inProgress') ? 'active' : 'idle' }, turns };
    },
    async turn(threadId) {
      this.onEvent({ method: 'turn/started', params: { threadId, turn: { id: 'turn-test', status: 'inProgress' } } });
      return { id: 'turn-test', status: 'inProgress' };
    }, async steer() { return { turnId: project.activeTurnId }; }, async interrupt() {}, async respond() {}, ...bridge,
  };
  const studio = await createStudio({ store, bridge: codex, port: 0, remote: durableRemote, producedArtifactsConsumer,
    traceSync: traceSync || (async (_id, trace, archive) => ({ saved: true,
      hashes: Object.fromEntries([...archive, trace].map(file => [file.name, file.sha256])) })),
    generationConsumer: async () => ({ passed: true, sampleCount: 1 }), deliveryWriter: async () => ({ saved: true }) });
  const onEvent = codex.onEvent;
  codex.onEvent = event => {
    if (event.params?.threadId === 'thread-test' && ['turn/started', 'turn/completed'].includes(event.method)) {
      const turn = event.params.turn;
      nativeTurns.set(turn.id, { ...nativeTurns.get(turn.id), items: [], ...turn });
    }
    onEvent(event);
  };
  project = studio.store.get('p_test');
  await studio.start();
  const base = `http://127.0.0.1:${studio.server.address().port}`;
  t.after(async () => { studio.server.closeAllConnections(); await studio.close(); controls.forEach(controller => controller.abort()); await rm(directory, { recursive: true, force: true }); });
  const request = (route, data, method = data === undefined ? 'GET' : 'POST', headers = {}) => new Promise((resolve, reject) => {
    const outgoing = http.request(base + route, {
      method,
      headers: { host: '127.0.0.1:0', ...(data === undefined ? {} : { 'content-type': 'application/json' }), ...headers },
    }, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, json: async () => JSON.parse(body) }));
    });
    outgoing.on('error', reject);
    outgoing.setTimeout(2_000, () => outgoing.destroy(new Error('HTTP request timed out')));
    outgoing.end(data === undefined ? undefined : JSON.stringify(data));
  });
  return { studio, project, codex, request, base, remoteCalls, remoteJobs, controls };
}

test('a source snapshot error reaches the real native continuation as an explicit unknown observation', async t => {
  let failed = false;
  const turns = [];
  const { studio, project, request } = await fixture(t, { sources: async () => {
    if (failed) throw new Error('SYSTEM_VERIFICATION_ONLY source temporarily unavailable');
  }, bridge: { async turn(_id, text) {
    turns.push(text); return { id:'diagnostic-'+turns.length, status:'inProgress' };
  }, async readThread() { return {id:'thread-test',status:{type:'idle'},turns:turns.map((_text,index)=>({id:'diagnostic-'+(index+1),status:'completed',items:[]}))}; } } });
  assert.equal((await request('/api/projects/p_test/chat', { message:'SYSTEM_VERIFICATION_ONLY preserve request', mode:'generate' })).status,200);
  failed = true;
  await studio.host.turnCompleted(project,{id:project.activeTurnId,status:'completed'});
  assert.equal(turns.length,2,'the producer must receive the diagnostic through nativeTurn, not only a stub');
  assert.match(turns[1],/source temporarily unavailable/);
  assert.match(turns[1],/sourceHash\\?":null|源码版本未知/);
  assert.equal(project.generation.waitingSystem,undefined);
  assert.equal(project.generation.status,'active');
});

test('an explicit budget expiring inside native preflight cannot send a late turn', async t => {
  let preparing = false, snapshots = 0, current = Date.now();
  const turns = [];
  const { studio, project, request } = await fixture(t,{sources:async()=>{
    if(preparing && ++snapshots===2) current+=101;
  },bridge:{async turn(_id,text){turns.push(text);return {id:'budget-'+turns.length,status:'inProgress'};},
    async readThread(){return {id:'thread-test',status:{type:'idle'},turns:turns.map((_text,index)=>({id:'budget-'+(index+1),status:'completed',items:[]}))};}}});
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY',mode:'generate',timeBudgetMs:100})).status,200);
  project.generation.startedAt=new Date(current).toISOString();studio.host.now=()=>current;
  preparing=true;
  await studio.host.turnCompleted(project,{id:project.activeTurnId,status:'completed'});
  assert.equal(turns.length,1,'source observation inside nativeTurn consumed the remaining user budget');
  assert.equal(project.generation.status,'failed');
  assert.match(project.generation.terminalReason,/时间预算/);
  assert.equal(project.activeTurnId,null);
  assert.equal(project.generation.pendingContinuation,null);
});

test('a failed source snapshot on the native completed event does not swallow completion', async t => {
  let failed = false;
  const turns = [];
  const { project, request, codex } = await fixture(t, { sources:async()=>{
    if(failed) throw new Error('SYSTEM_VERIFICATION_ONLY completion source error');
  },bridge:{async turn(_id,text){
    turns.push(text); const turn={id:'completed-source-'+turns.length,status:'inProgress'};
    this.onEvent({method:'turn/started',params:{threadId:'thread-test',turn}});return turn;
  }}});
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY',mode:'generate'})).status,200);
  failed=true;
  codex.onEvent({method:'turn/completed',params:{threadId:'thread-test',turn:{id:'completed-source-1',status:'completed'}}});
  await until(()=>turns.length===2);
  assert.match(turns[1],/completion source error/);
  assert.equal(project.generation.waitingSystem,undefined);
});

test('a user supplied finite budget expiring in the first input preflight prevents the native send', async t => {
  let current=Date.now(), turns=0;
  const { studio, project, request }=await fixture(t,{sources:async()=>{current+=10000;},
    bridge:{async turn(){turns++;return {id:'must-not-send',status:'inProgress'};}}});
  current=Date.now();studio.host.now=()=>current;
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY',mode:'generate',timeBudgetMs:100})).status,409);
  assert.equal(turns,0);
  assert.equal(project.generation.status,'failed');
  assert.match(project.generation.terminalReason,/时间预算/);
  assert.equal(project.activeTurnId,null);
});

test('the final bridge guard preserves a proven unsent budget rejection after native preflight', async t => {
  let current=Date.now(), preparing=false;
  const turns=[];
  const {studio,project,request}=await fixture(t,{bridge:{async turn(_id,text,options){
    if(preparing){current+=10000;options.beforeSend();}
    turns.push(text);return {id:'final-guard-'+turns.length,status:'inProgress'};
  },async readThread(){return {id:'thread-test',status:{type:'idle'},turns:turns.map((_text,index)=>({id:'final-guard-'+(index+1),status:'completed',items:[]}))};}}});
  current=Date.now();studio.host.now=()=>current;
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY',mode:'generate',timeBudgetMs:100})).status,200);
  project.generation.startedAt=new Date(current).toISOString();preparing=true;
  await studio.host.turnCompleted(project,{id:project.activeTurnId,status:'completed'});
  assert.equal(turns.length,1);
  assert.equal(project.generation.status,'failed');
  assert.equal(project.generation.pendingContinuation,null);
});

test('steering rechecks an explicit budget at its final native send boundary', async t => {
  let current=Date.now(), sent=0;
  const {studio,project,request}=await fixture(t,{bridge:{async steer(_thread,_turn,_text,options){
    await Promise.resolve();current+=100001;options?.beforeSend();sent++;return {turnId:'turn-test'};
  }}});
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY',mode:'generate',timeBudgetMs:100000})).status,200);
  current=Date.now();project.generation.startedAt=new Date(current).toISOString();studio.host.now=()=>current;
  assert.equal((await request('/api/projects/p_test/chat',{message:'SYSTEM_VERIFICATION_ONLY followup',mode:'generate'})).status,409);
  assert.equal(sent,0);
  assert.equal(project.generation.stopStatus,'failed');
  assert.match(project.generation.terminalReason,/时间预算/);
});

test('an evidenced system wait blocks automatic and chat dispatch until explicit same-generation recovery', async t => {
  const turns = [], steers = [];
  const { studio, project, codex, request } = await fixture(t, { bridge: {
    async turn(threadId, input) {
      const turn = { id: `turn-${turns.length + 1}`, status: 'inProgress' };
      turns.push({ threadId, input, turn });
      this.onEvent({ method: 'turn/started', params: { threadId, turn } });
      return turn;
    },
    async steer(...args) { steers.push(args); return { turnId: project.activeTurnId }; },
  } });
  assert.equal((await request('/api/projects/p_test/chat', { message: '系统夹具，保留完整三产物。', mode: 'generate', timeBudgetMs: null })).status, 200);
  const generationId = project.generation.id;
  const blocked = await request('/api/tools/generation_wait_system', { projectId: project.id, generationId,
    requestId: 'blocked-runner', reason: '执行器出现系统错误。', evidence: ['SYSTEM_VERIFICATION_ONLY timeout receipt'] });
  assert.equal(blocked.status, 200);
  assert.equal(project.generation.status, 'waiting_system');
  assert.equal((await request('/api/projects/p_test/resume', { generationId })).status, 409);
  codex.onEvent({ method: 'turn/completed', params: { threadId: project.threadId, turn: { id: 'turn-1', status: 'completed' } } });
  await until(() => project.activeTurnId === null);
  for (let index = 0; index < 4; index++) await studio.host.recover(project);
  assert.equal(turns.length, 1);
  assert.equal((await request('/api/projects/p_test/chat', { message: '补充要求必须保存。', mode: 'generate' })).status, 200);
  assert.equal((await request('/api/projects/p_test/chat', { message: '状态讨论也不能重启生成。', mode: 'discussion' })).status, 200);
  assert.equal(turns.length, 1);
  assert.equal(steers.length, 0);
  assert.match(project.requirements.at(-1).text, /补充要求/);
  const exposed = await (await request('/api/projects/p_test')).json();
  assert.equal(exposed.generation.waitingSystem.reason, '执行器出现系统错误。');
  assert.equal(exposed.status, 'waiting_system');
  assert.equal((await request('/api/projects/p_test/resume', { generationId: 'stale' })).status, 409);
  assert.equal((await request('/api/projects/p_test/resume', { generationId })).status, 200);
  assert.equal(project.generation.id, generationId);
  assert.equal(project.generation.timeBudgetMs, null);
  assert.equal(turns.length, 2);
  const late = await request('/api/tools/generation_wait_system', { projectId: project.id, generationId,
    requestId: 'blocked-runner', reason: '执行器出现系统错误。', evidence: ['SYSTEM_VERIFICATION_ONLY timeout receipt'] });
  assert.equal(late.status, 200);
  assert.equal(project.generation.waitingSystem, undefined);
});

test('system waiting during native preflight retains no falsely uncertain unsent continuation', async t => {
  let hold = false, entered, release, turns = 0;
  const opened = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const { project, request, codex } = await fixture(t, { bridge: {
    async start() { if (hold) { entered(); await gate; } },
    async turn(threadId) { const turn = { id: `preflight-${++turns}`, status: 'inProgress' };
      this.onEvent({ method: 'turn/started', params: { threadId, turn } }); return turn; },
  } });
  assert.equal((await request('/api/projects/p_test/chat', { message: 'SYSTEM_VERIFICATION_ONLY', mode: 'generate', timeBudgetMs: null })).status, 200);
  hold = true;
  codex.onEvent({ method: 'turn/completed', params: { threadId: project.threadId, turn: { id: 'preflight-1', status: 'completed' } } });
  await opened;
  const scope = { projectId: project.id, generationId: project.generation.id };
  const reporting = request('/api/tools/generation_wait_system', { ...scope, requestId: 'preflight-wait',
    reason: '系统等待发生在原生输入准备期间。', evidence: ['SYSTEM_VERIFICATION_ONLY preflight gate'] });
  await until(() => !!project.generation.waitingSystem);
  release();
  assert.equal((await reporting).status, 200);
  assert.equal(turns, 1, 'no second native input was sent');
  assert.equal(project.generation.pendingContinuation, null, 'a proven unsent input must not require transport reconciliation');
  hold = false;
  assert.equal((await request('/api/projects/p_test/resume', { generationId: scope.generationId })).status, 200);
  assert.equal(turns, 2);
});

test('a fast completed Codex turn stays completed when the turn/start response arrives later', async t => {
  const { project, request } = await fixture(t, { bridge: {
    async turn(threadId) {
      this.onEvent({ method: 'turn/started', params: { threadId, turn: { id: 'turn-fast', status: 'inProgress' } } });
      this.onEvent({ method: 'turn/completed', params: { threadId, turn: { id: 'turn-fast', status: 'completed' } } });
      await wait(30);
      return { id: 'turn-fast', status: 'inProgress' };
    },
  } });
  const response = await request('/api/projects/p_test/chat', { message: '检查场景', mode: 'discussion' });
  assert.equal(response.status, 200);
  assert.equal(project.activeTurnId, null);
  assert.equal(publicProject(project).status, 'idle');
});

test('native resolved events without a thread ID close approval timing before later reconciliation', async t => {
  const { studio, project, codex } = await fixture(t);
  const started = Date.now(); let current = started;
  studio.store.beginGeneration(project.id);
  project.generation.startedAt = new Date(started).toISOString();
  project.threadId = 'thread-test'; project.activeTurnId = 'turn-test';
  studio.host.now = () => current;
  current = started + 20 * 60_000;
  codex.onRequest({ id: 0, method: 'item/commandExecution/requestApproval', params: { threadId: 'thread-test', turnId: 'turn-test' } });
  await wait(30);
  current = started + 60 * 60_000;
  codex.onEvent({ method: 'serverRequest/resolved', params: { requestId: 0 } });
  await wait(30);
  assert.equal(project.generation.approvalWaitMs, 40 * 60_000);
  assert.equal(project.generation.approvalWaitingSince, null);
  current = started + 65 * 60_000;
  codex.onEvent({ method: 'serverRequest/resolved', params: { requestId: 0 } });
  await wait(30);
  assert.equal(project.generation.approvalWaitMs, 40 * 60_000);
  current = started + 70 * 60_000;
  await studio.host.recover(project);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.approvalWaitMs, 40 * 60_000);
});

test('native child-agent events belong to the project trace without changing root chat or turn state', async t => {
  const { studio, project, codex } = await fixture(t);
  project.threadId = 'thread-test';
  project.activeTurnId = 'root-turn';
  project.status = 'thinking';
  project.messages.push({ id: 'root-message', role: 'assistant', content: '正在安排场景构建' });
  const originalMessages = structuredClone(project.messages);
  codex.onEvent({ method: 'thread/started', params: { thread: {
    id: 'thread-child', parentThreadId: 'thread-test', sessionId: 'thread-test', cwd: studio.store.directory(project.id),
    source: { subAgent: { thread_spawn: { parent_thread_id: 'thread-test', agent_path: '/root/fixture' } } },
  } } });
  codex.onEvent({ method: 'turn/started', params: { threadId: 'thread-child', turn: { id: 'child-turn', status: 'inProgress' } } });
  codex.onEvent({ method: 'item/started', params: { threadId: 'thread-child', turnId: 'child-turn', item: {
    id: 'child-command', type: 'commandExecution', command: 'python scene.py', status: 'inProgress',
  } } });
  codex.onEvent({ method: 'item/agentMessage/delta', params: {
    threadId: 'thread-child', turnId: 'child-turn', itemId: 'child-message', delta: '子智能体检查结果',
  } });
  codex.onEvent({ method: 'item/completed', params: { threadId: 'thread-child', turnId: 'child-turn', item: {
    id: 'child-message', type: 'agentMessage', text: '子智能体检查结果',
  } } });
  codex.onEvent({ method: 'turn/completed', params: { threadId: 'thread-child', turn: { id: 'child-turn', status: 'completed' } } });
  await until(() => project.events.some(event => event.method === 'turn/completed' && event.params.threadId === 'thread-child'));
  assert.ok(project.events.some(event => event.method === 'item/started'
    && event.params.threadId === 'thread-child' && event.params.item.id === 'child-command'));
  assert.ok(project.events.some(event => event.method === 'turn/completed' && event.params.threadId === 'thread-child'));
  assert.equal(project.activeTurnId, 'root-turn');
  assert.equal(publicProject(project).status, 'thinking');
  assert.deepEqual(project.messages, originalMessages);
});

test('simultaneous builds reserve the project before waiting for source files', async t => {
  let releaseSources, sourceCalls = 0;
  const sourceGate = new Promise(resolve => { releaseSources = resolve; });
  const { project, request } = await fixture(t, {
    sources: async () => { sourceCalls++; await sourceGate; return { scene: '', tasks: '', expert: '' }; },
    remote: async (_payload, { signal }) => new Promise((resolve, reject) => {
      if (signal.aborted) reject(new Error('cancelled'));
      else signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true });
    }),
  });
  const first = request('/api/projects/p_test/build', {});
  const second = request('/api/projects/p_test/build', {});
  await wait(30);
  releaseSources();
  const responses = await Promise.all([first, second]);
  assert.deepEqual(responses.map(response => response.status).sort(), [202, 409]);
  assert.equal(sourceCalls, 2);
  assert.equal(project.jobs.length, 1);
});

test('a completed domain job preserves an active root Codex turn and its status', async t => {
  let finishBuild;
  const buildGate = new Promise(resolve => { finishBuild = resolve; });
  const { studio, project, request } = await fixture(t, {
    remote: async () => { await buildGate; return { revision: 'revision-test', artifacts: {} }; },
  });
  project.threadId = 'thread-test';
  project.activeTurnId = 'root-turn';
  project.status = 'thinking';
  const response = await request('/api/projects/p_test/build', {});
  assert.equal(response.status, 202);
  finishBuild();
  await wait(5);
  await studio.tools.refreshJob(project, project.jobs[0]);
  assert.equal(project.jobs[0].status, 'completed');
  assert.equal(project.activeTurnId, 'root-turn');
  assert.ok(['thinking', 'active'].includes(publicProject(project).status));
});

test('close reclaims an active HTTP socket without waiting for its health response', async t => {
  let finishHealth, healthStarted = false;
  const healthGate = new Promise(resolve => { finishHealth = resolve; });
  const { studio, request } = await fixture(t, {
    remote: async () => { healthStarted = true; await healthGate; return { ok: true }; },
  });
  const health = request('/api/health').then(response => ({ response }), error => ({ error }));
  for (let i = 0; i < 100 && !healthStarted; i++) await wait(5);
  assert.ok(healthStarted);
  const closing = studio.close();
  const closedPromptly = await Promise.race([closing.then(() => true), wait(300).then(() => false)]);
  finishHealth();
  const outcome = await health;
  await closing;
  assert.equal(closedPromptly, true, 'close must finish while the HTTP health handler is still waiting');
  assert.ok(outcome.error, 'the pending client socket should be closed rather than receiving a late response');
});

test('a build cannot read source files while a source save is in progress', async t => {
  let releaseSave, sourceReads = 0, saveStarted = false;
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const { request } = await fixture(t, {
    updateSources: async () => { saveStarted = true; await saveGate; },
    sources: async () => { sourceReads++; return { scene: '', tasks: '', expert: '' }; },
  });
  const saving = request('/api/projects/p_test/sources', { scene: 'new', tasks: 'new', expert: 'new' }, 'PUT');
  for (let i = 0; i < 100 && !saveStarted; i++) await wait(5);
  assert.ok(saveStarted);
  const building = await request('/api/projects/p_test/build', {});
  releaseSave();
  assert.equal((await saving).status, 200);
  assert.equal(building.status, 409);
  assert.equal(sourceReads, 0);
});

test('completion and bridge disconnect remove approval cards that cannot be answered', async t => {
  const { project, codex, request } = await fixture(t);
  project.threadId = 'thread-test';
  project.activeTurnId = 'turn-test';
  codex.onRequest({ id: 'approval-test', method: 'item/commandExecution/requestApproval',
    params: { threadId: 'thread-test', turnId: 'turn-test', itemId: 'item-test', command: 'echo test' } });
  let state = await (await request('/api/state')).json();
  assert.equal(state.approvals.length, 1);
  codex.onEvent({ method: 'turn/completed', params: { threadId: 'thread-test', turn: { id: 'turn-test', status: 'interrupted' } } });
  await wait(20);
  state = await (await request('/api/state')).json();
  assert.equal(state.approvals.length, 0);
  codex.onRequest({ id: 'disconnect-test', method: 'item/commandExecution/requestApproval', params: { threadId: 'thread-test', turnId: 'next-turn' } });
  codex.onEvent({ method: 'bridge/status', params: { ready: false, error: 'Codex exited (1)' } });
  await wait(20);
  state = await (await request('/api/state')).json();
  assert.equal(state.approvals.length, 0);
});

test('an invalid recorded artifact returns an HTTP error without crashing the server', async t => {
  const { project, request } = await fixture(t);
  const relative = 'projects/p_test/../outside.glb';
  project.jobs.push({ status: 'completed', result: { artifacts: { preview: relative } } });
  const response = await request('/api/projects/p_test/artifacts/preview.glb?file=' + encodeURIComponent(relative));
  assert.ok(response.status >= 400);
  assert.ok((await response.json()).error);
  assert.equal((await request('/api/state')).status, 200);
});

test('SSE sends the initial snapshot and subsequent Codex events until the client disconnects', async t => {
  const { project, codex, base } = await fixture(t);
  project.threadId = 'thread-test';
  let received = '';
  let response;
  const outgoing = http.get(base + '/api/events', { headers: { host: '127.0.0.1:0' } }, incoming => {
    response = incoming;
    incoming.setEncoding('utf8');
    incoming.on('data', chunk => { received += chunk; });
  });
  t.after(() => { response?.destroy(); outgoing.destroy(); });
  for (let i = 0; i < 100 && !received.includes('"kind":"state"'); i++) await wait(5);
  assert.ok(received.includes('"kind":"state"'));
  codex.onEvent({ method: 'item/agentMessage/delta', params: { threadId: 'thread-test', itemId: 'message-test', delta: '场景已开始' } });
  for (let i = 0; i < 100 && !received.includes('场景已开始'); i++) await wait(5);
  assert.ok(received.includes('场景已开始'));
  response.destroy(); outgoing.destroy();
});

test('discussion leaves generation absent while generation and later steering preserve request versions', async t => {
  const { project, request } = await fixture(t);
  assert.equal((await request('/api/projects/p_test/chat', { message: '讨论架构', mode: 'discussion' })).status, 200);
  assert.equal(project.generation, undefined);
  assert.equal((await request('/api/projects/p_test/chat', { message: '生成工作台', mode: 'generate' })).status, 200);
  const generationId = project.generation.id, version = project.generation.requirementVersion;
  assert.equal((await request('/api/projects/p_test/chat', { message: '桌子使用蓝色', mode: 'generate' })).status, 200);
  assert.equal(project.generation.id, generationId);
  assert.equal(project.generation.requirementVersion, version + 1);
  assert.ok(project.requirements.some(row => row.text === '生成工作台'));
  assert.ok(project.requirements.some(row => row.text === '桌子使用蓝色'));
  assert.equal((await request('/api/projects/p_test/chat', { message: '进展如何', mode: 'discussion' })).status, 200);
  assert.equal(project.generation.requirementVersion, version + 1);
});

test('one prompt can start an unlimited generation and later chat cannot replace its budget', async t => {
  const { project, request, studio } = await fixture(t);
  assert.equal((await request('/api/projects/p_test/chat', {
    message: '完整厨房任务', mode: 'generate', timeBudgetMs: null,
  })).status, 200);
  assert.equal(project.generation.timeBudgetMs, null);
  assert.equal(publicProject(project).generation.timeBudgetMs, null);
  const saved = JSON.parse(await readFile(studio.store.stateFile, 'utf8'));
  assert.equal(saved[0].generation.timeBudgetMs, null);
  const version = project.generation.requirementVersion;
  assert.equal((await request('/api/projects/p_test/chat', {
    message: '不得悄悄加上时限', mode: 'generate', timeBudgetMs: 60 * 60_000,
  })).status, 409);
  assert.equal(project.generation.timeBudgetMs, null);
  assert.equal(project.generation.requirementVersion, version);
  assert.equal((await request('/api/projects/p_test/chat', { message: '保留当前要求', mode: 'generate' })).status, 200);
  assert.equal(project.generation.timeBudgetMs, null);
});

test('invalid generation budgets and discussion budget options do not start native work', async t => {
  const { project, request } = await fixture(t);
  for (const [mode, timeBudgetMs] of [['generate', 0], ['generate', 'unlimited'], ['discussion', null]]) {
    assert.equal((await request('/api/projects/p_test/chat', { message: '系统验证', mode, timeBudgetMs })).status, 400);
    assert.equal(project.generation, undefined);
    assert.equal(project.threadId, null);
    assert.equal(project.messages.length, 0);
  }
});

for (const phase of ['thread', 'turn']) test(`recovery while the initial ${phase} opens does not send another input`, async t => {
  let entered, release;
  const opening = new Promise(resolve => { entered = resolve; });
  const delayed = new Promise(resolve => { release = resolve; });
  let threadCalls = 0, turnCalls = 0, steerCalls = 0;
  const { project, request, studio } = await fixture(t, { bridge: {
    async startThread() {
      threadCalls++;
      if (phase === 'thread') { entered(); await delayed; }
      return { id: 'thread-test' };
    },
    async turn(threadId) {
      turnCalls++;
      if (phase === 'turn') { entered(); await delayed; }
      this.onEvent({ method: 'turn/started', params: { threadId, turn: { id: 'turn-test', status: 'inProgress' } } });
      return { id: 'turn-test', status: 'inProgress' };
    },
    async steer() { steerCalls++; return { turnId: 'turn-test' }; },
  } });
  const submitted = request('/api/projects/p_test/chat', { message: '一次完整需求', mode: 'generate', timeBudgetMs: null });
  await opening;
  const recovery = studio.host.recover(project);
  try {
    await wait(30);
    assert.equal(project.generation.continuationCount, 0);
    assert.equal(threadCalls, 1);
    assert.equal(steerCalls, 0);
  } finally {
    release();
    await Promise.allSettled([submitted, recovery]);
  }
  assert.equal((await submitted).status, 200);
  assert.equal(turnCalls, 1);
  assert.equal(project.events.filter(row => row.type === 'native_input').length, 1);
  assert.equal(project.generation.pendingContinuation, null);
});

test('a failed initial thread opening leaves recovery able to start native work', async t => {
  let starts = 0;
  const { project, request, studio } = await fixture(t, { bridge: {
    async startThread() {
      if (++starts === 1) throw new Error('initial native startup failed');
      return { id: 'thread-test' };
    },
  } });
  assert.equal((await request('/api/projects/p_test/chat', { message: '保留原需求', mode: 'generate', timeBudgetMs: null })).status, 500);
  await studio.host.recover(project);
  assert.equal(starts, 2);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.activeTurnId, 'turn-test');
});

test('HTTP and debug routes enter the same tool core and closing preserves durable remote work', async t => {
  const { studio, project, request, remoteCalls, controls } = await fixture(t, {
    remote: async (_payload, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('fixture cleanup')), { once: true });
    }),
  });
  const debug = await request('/api/projects/p_test/build', {});
  assert.equal(debug.status, 202);
  const job = (await debug.json()).jobId;
  const inspected = await request('/api/tools/project_inspect', { projectId: project.id, generationId: project.generation.id });
  assert.equal(inspected.status, 200);
  const data = (await inspected.json()).structuredContent;
  assert.ok(data.jobs.some(row => row.jobId === job));
  assert.ok(project.events.some(row => row.type === 'tool_call' && row.name === 'scene_build'));
  assert.equal(remoteCalls.filter(row => row.operation === 'submit').length, 1);
  await studio.close();
  assert.equal(controls[0].signal.aborted, false);
  assert.equal(project.generation.status, 'recovering');
  assert.equal(project.jobs[0].status, 'running');
});

test('public instructions and submitted skill references can be recovered from frozen input snapshots', async t => {
  let started;
  const { project, request, studio } = await fixture(t, { bridge: {
    async startThread(options) { started = options; return { id: 'thread-test' }; },
  } });
  assert.equal((await request('/api/projects/p_test/chat', { message: '生成一个工作台', mode: 'generate' })).status, 200);
  const inputs = project.events.filter(row => row.type === 'input_snapshot');
  assert.ok(inputs.some(row => row.kind === 'thread_instructions'));
  const submitted = inputs.find(row => row.kind === 'native_turn_input');
  assert.ok(submitted);
  const { readFile } = await import('node:fs/promises');
  const archived = await readFile(path.join(studio.store.directory(project.id), submitted.snapshotRef), 'utf8');
  assert.equal(contentHash(archived), submitted.inputHash);
  const input = JSON.parse(archived);
  assert.ok(input.text.includes('生成一个工作台'));
  assert.equal(input.skillReference.coverage, 'submitted_reference');
  assert.equal(contentHash(input.skillReference.text), input.skillReference.sha256);
  assert.ok(input.skillReference.path.endsWith('SKILL.md'));
  assert.equal(input.apiReferenceCoverage, 'not_claimed_loaded');
  const instructions = JSON.parse(await readFile(path.join(studio.store.directory(project.id), input.instructionsRef), 'utf8')).text;
  assert.equal(started.cwd, studio.store.directory(project.id));
  assert.equal(started.instructions, instructions);
  for (const text of [started.instructions, input.skillReference.text]) {
    assert.ok(text.includes('当前生成工程的源程序和必要辅助文件'));
    assert.ok(text.includes('assets/imports/'));
    assert.ok(text.includes('src、runtime、tests、skills'));
    assert.ok(text.includes('宿主状态') && text.includes('服务均只读'));
    assert.ok(text.includes('generation_wait_system'));
    assert.ok(text.includes('由系统开发代理处理'));
    assert.ok(text.includes('不得为修改系统源码、配置、技能或服务自行修复、申请提权或重启服务'));
  }
  assert.ok(started.instructions.includes(`/assets/imports/${project.id}/`));
  assert.ok(started.instructions.includes(`/assets/imports/robots/${project.id}/`));
  assert.ok(started.instructions.includes(`/projects/${project.id}/delivery/`));
  assert.ok(started.instructions.includes('producer-exports-v1'));
});

test('tool actor comes from transport metadata and cannot be supplied in model arguments', async t => {
  const { project, request, studio } = await fixture(t);
  studio.store.beginGeneration(project.id);
  const scope = { projectId: project.id, generationId: project.generation.id };
  assert.equal((await request('/api/tools/assets_lookup', scope)).status, 200);
  assert.equal(project.events.findLast(row => row.type === 'tool_call').actor, 'manual');
  assert.equal((await request('/api/tools/assets_lookup', scope, 'POST', { 'x-sceneagent-origin': 'codex-mcp' })).status, 200);
  assert.equal(project.events.findLast(row => row.type === 'tool_call').actor, 'codex-mcp');
  assert.equal((await request('/api/tools/assets_lookup', { ...scope, actor: 'codex-mcp' })).status, 400);
});

test('UI snapshots exclude executable payloads, encoded pixels and raw transport while retaining authoritative data', async t => {
  const { project, request } = await fixture(t);
  const large = 'private-payload-marker' + 'x'.repeat(200000);
  project.jobs.push({ id: 'j_large', kind: 'build', status: 'completed', payload: { sourceFiles: { 'scene.py': large } },
    result: { revision: 'rev_large', nodes: 403, image: { data: large, mimeType: 'image/png' }, artifacts: {} } });
  project.events.push({ type: 'codex', method: 'transport/sent', params: { message: { large } } });
  project.delivery = { accepted: true, artifactRef: 'projects/p_test/deliveries/d_1/delivery.json',
    consumerArtifactRef: 'projects/p_test/consumer-reports/robot/report.json',
    products: { generation: { passed: true, artifactRef: 'projects/p_test/consumer-reports/generation.json',
      agentTrainingSampleCount: 12, diagnosticSampleCount: 3, samples: [large] } } };
  const state = await (await request('/api/state')).json();
  const text = JSON.stringify(state);
  assert.ok(text.length < 20000);
  assert.equal(text.includes('private-payload-marker'), false);
  assert.equal(state.projects[0].delivery.consumerArtifactRef, project.delivery.consumerArtifactRef);
  assert.equal(state.projects[0].delivery.products.generation.agentTrainingSampleCount, 12);
  assert.equal(state.projects[0].delivery.products.generation.artifactRef, project.delivery.products.generation.artifactRef);
  assert.equal(project.jobs[0].payload.sourceFiles['scene.py'], large);
  assert.equal(project.events[0].params.message.large, large);
  assert.equal((await (await request('/api/projects/p_test')).json()).jobs[0].payload, undefined);
});

test('ordinary native deltas are traced completely while state persistence is coalesced', async t => {
  const { project, studio, codex } = await fixture(t);
  project.threadId = 'thread-test';
  const original = studio.store.save.bind(studio.store);
  let saves = 0;
  studio.store.save = () => { saves++; return original(); };
  for (let index = 0; index < 20; index++) codex.onEvent({ method: 'item/agentMessage/delta', params: {
    threadId: 'thread-test', itemId: 'stream', delta: String(index) + ',',
  } });
  await wait(400);
  assert.equal(project.events.filter(row => row.method === 'item/agentMessage/delta').length, 20);
  assert.equal(project.messages[0].content, Array.from({ length: 20 }, (_, index) => String(index) + ',').join(''));
  assert.ok(saves <= 3, `expected batched persistence, observed ${saves}`);
});

test('a final native reply preserves the completed generation status', async t => {
  const { project, studio, codex, request } = await fixture(t);
  studio.store.beginGeneration(project.id);
  project.generation.status = 'complete';
  project.status = 'complete';
  project.threadId = 'thread-test';
  project.activeTurnId = 'turn-final';
  codex.onEvent({ method: 'turn/completed', params: { threadId: 'thread-test', turn: { id: 'turn-final', status: 'completed' } } });
  await wait(40);
  assert.equal(publicProject(project).status, 'complete');
  assert.equal((await (await request('/api/projects/p_test')).json()).status, 'complete');
});

test('same frozen revision retains an archived qualified demonstration video after preview metadata is cleared', async t => {
  const { project, request } = await fixture(t);
  project.latestBuild = { revision: 'rev_same' };
  project.artifacts = { preview: '/current-preview.glb' };
  const episode = { episode: 'ep_kept', revision: 'rev_same', demonstrationEligible: true, score: { success: true },
    artifacts: { video: 'projects/p_test/episodes/ep_kept/video.mp4' } };
  project.episodes = [episode];
  delete project.latestEpisode;
  const result = await (await request('/api/projects/p_test')).json();
  assert.equal(result.artifacts.video, '/api/projects/p_test/artifacts/video.mp4?file=projects%2Fp_test%2Fepisodes%2Fep_kept%2Fvideo.mp4');
  assert.equal(result.latestEpisode.episode, 'ep_kept');
  assert.equal(project.artifacts.video, undefined);
  assert.equal(project.latestEpisode, undefined);
  assert.deepEqual(project.episodes, [episode]);
});

test('archived videos from another revision or nonqualified demonstrations stay absent', async t => {
  const { project, request } = await fixture(t);
  project.latestBuild = { revision: 'rev_current' }; project.artifacts = {};
  project.episodes = [
    { episode: 'old', revision: 'rev_old', demonstrationEligible: true, score: { success: true }, artifacts: { video: 'projects/p_test/episodes/old/video.mp4' } },
    { episode: 'probe', revision: 'rev_current', demonstrationEligible: false, score: { success: true }, artifacts: { video: 'projects/p_test/episodes/probe/video.mp4' } },
    { episode: 'failed', revision: 'rev_current', demonstrationEligible: true, score: { success: false }, artifacts: { video: 'projects/p_test/episodes/failed/video.mp4' } },
  ];
  const result = await (await request('/api/projects/p_test')).json();
  assert.equal(result.artifacts.video, undefined); assert.equal(result.latestEpisode, null);
});

test('an existing video and caption keep their current pairing instead of using a different archive', async t => {
  const { project, request } = await fixture(t);
  project.latestBuild = { revision: 'rev_current' };
  project.artifacts = { video: '/already-visible-video.mp4' };
  project.latestEpisode = { episode: 'ep_visible', revision: 'rev_current' };
  project.episodes = [{ episode: 'archived', revision: 'rev_current', demonstrationEligible: true, score: { success: true },
    artifacts: { video: 'projects/p_test/episodes/archived/video.mp4' } }];
  const result = await (await request('/api/projects/p_test')).json();
  assert.equal(result.artifacts.video, '/already-visible-video.mp4'); assert.equal(result.latestEpisode.episode, 'ep_visible');
});

test('native subAgentActivity associates a child even without thread/started and child completion cannot end the root', async t => {
  const { project, codex, studio } = await fixture(t);
  project.threadId = 'thread-test'; project.activeTurnId = 'root-turn'; project.status = 'thinking';
  const read = codex.readThread.bind(codex);
  codex.readThread = async (id, options) => id === 'actual-child' ? { id, parentThreadId: project.threadId,
    cwd: studio.store.directory(project.id), source: { subAgent: { thread_spawn: { parent_thread_id: project.threadId,
      agent_path: '/root/scene_quality_review' } } }, status: { type: 'idle' } } : read(id, options);
  codex.onEvent({ method: 'item/completed', params: { threadId: 'thread-test', turnId: 'root-turn', item: {
    type: 'subAgentActivity', id: 'started', kind: 'started', agentThreadId: 'actual-child', agentPath: '/root/scene_quality_review',
  } } });
  codex.onEvent({ method: 'turn/completed', params: { threadId: 'actual-child', turn: { id: 'child-turn', status: 'completed' } } });
  await wait(30);
  assert.ok(project.events.some(row => row.child && row.method === 'turn/completed' && row.params.threadId === 'actual-child'));
  assert.equal(project.activeTurnId, 'root-turn'); assert.equal(publicProject(project).status, 'thinking');
});

for (const mode of ['legacy', 'producer', 'shutdown']) test('HTTP delivery receipt and pending trace lifecycle: ' + mode, async t => {
  const producer = mode !== 'legacy';
  const native = await nativeReviewFixture(t);
  let releaseTrace, traceSignal, traceCalls = 0;
  const traceGate = new Promise(resolve => { releaseTrace = resolve; });
  let consumer; const phases = [];
  const f = await fixture(t, { bridge: native.codex, remote: async payload => {
    assert.equal(payload.operation, 'consume');
    assert.deepEqual(payload.episodes, [{ episodeId: 'ep', taskId: 'task', use: 'successful_demonstration' }]);
    assert.deepEqual(payload.validationEpisodes, ['validation_ep']);
    assert.equal(Boolean(payload.environmentRoot), producer);
    return consumer;
  }, traceSync: mode === 'shutdown' ? async (_id, trace, archive, { signal } = {}) => {
    traceCalls++; traceSignal = signal;
    await traceGate;
    if (signal?.aborted) throw signal.reason;
    return { saved:true, hashes:Object.fromEntries([...archive, trace].map(file => [file.name, file.sha256])) };
  } : undefined, producedArtifactsConsumer: producer ? async (target, pointer, { job, phase }) => {
    phases.push(phase);
    const root = `projects/${target.id}/deliveries/artifacts/${pointer.sha256}/`;
    return { schema: 'sceneagent-produced-consumption-v1', passed: true, ...pointer,
      binding: Object.fromEntries(['projectId', 'generationId', 'requirementVersion', 'requestHash', 'sourceHash', 'revision']
        .map(key => [key, key === 'projectId' ? target.id : job[key]])),
      episodes: job.reviewedEpisodes.map(({ episodeId, taskId, use }) => ({ episodeId, taskId, use })),
      frozenManifestRef: root+'manifest.json', environmentRoot: root+'environment', artifactRef: root+'consumption.json',
      products: { generation: { atif: root+'generation/atif.json' }, robot: { hdf5: root+'robot/data.hdf5' }, environment: { root: root+'environment' } },
      files: [{ artifactRef: root+'robot/data.hdf5', bytes: 16, sha256: 'b'.repeat(64) }] };
  } : undefined });
  const { project, studio } = f;
  studio.store.beginGeneration(project.id); project.threadId = 'root';
  if (!producer) delete project.generation.deliveryContract;
  const producedArtifacts = producer ? { manifestRef: `projects/${project.id}/delivery/run/manifest.json`, sha256: 'c'.repeat(64) } : undefined;
  if (producer) native.report.producedArtifacts = producedArtifacts;
  const source = await studio.store.sourceSnapshot(project.id);
  const binding = { projectId: project.id, generationId: project.generation.id, requirementVersion: 1,
    requestHash: project.generation.requestHash, sourceHash: source.sourceHash, revision: 'rev' };
  Object.assign(native.report, binding); native.final();
  const build = { projectId: project.id, revision: 'rev', io: { clock: { physicsRateHz: 200, controlRateHz: 50 } },
    sourceDigests: { 'rubrics.py': contentHash(source.sourceFiles['rubrics.py']) },
    tasks: [{ id: 'task', rubric: { sourceRef: 'rubrics.py', helperRefs: [] }, evaluation: {
      cases: [{ id: 'case_a', sourceRef: 'expert.py', helperRefs: [], expected: { success: false } }] } }] };
  project.latestBuild = build;
  project.latestCheck = { revision: 'rev', passed: true, tasks: [{ taskId: 'task', passed: true,
    cases: [{ id: 'case_a', episodeId: 'validation_ep', passed: true }] }] };
  const episode = { projectId: project.id, revision: 'rev', episode: 'ep', taskId: 'task' };
  project.episodes = [episode];
  project.jobs.push({ id: 'build', kind: 'build', status: 'completed', ...binding, result: build },
    { id: 'run', kind: 'rollout', status: 'completed', ...binding,
      result: { projectId: project.id, revision: 'rev', episodes: [episode] } });
  const reportRoot = `projects/${project.id}/consumer-reports/http_delivery/`, interfaceRoot = reportRoot+'interfaces/task/';
  // Transport substitutes exercise HTTP/receipt wiring; actual record verification has separate CPU tests.
  consumer = { schema: 'sceneagent-product-consumption-v1', projectId: project.id, revision: 'rev', passed: true,
    artifactRef: reportRoot+'report.json', reload: { passed: true,
      ...(producer ? { environmentRoot: `projects/${project.id}/deliveries/artifacts/${producedArtifacts.sha256}/environment` } : {}) },
    demonstrations: { passed: true, episodeCount: 1, batches: [{ episodeId: 'ep', taskId: 'task',
      use: 'successful_demonstration', demonstrationEligible: true, result: { success: true },
      controlCycles: 20, physicalSteps: 80, uncommittedTail: false }] },
    evaluation: { passed: true, tasks: [{ taskId: 'task', passed: true }], isolation: { passed: true, checks: { networkBlocked: true } },
      interfaces: [{ taskId: 'task', sourceEpisodeId: 'ep', passed: true, scoreAvailable: true, controlCycles: 1, physicalSteps: 4,
        recordRef: interfaceRoot+'records.json', episodeRef: interfaceRoot+'episode.json', sourceRef: interfaceRoot+'probe.py',
        sourceDigest: 'a'.repeat(64), sourceFactsDigest: 'b'.repeat(64), coverage: { open: 'checked', reset: 'checked', observe: 'checked',
          step: 'checked', record: 'checked', score: 'checked', endedStepRejection: 'not_exercised' } }] } };
  const scope = { projectId: project.id, generationId: project.generation.id };
  const submitted = await f.request('/api/tools/project_deliver', { ...scope, revision: 'rev', expectedRequirementVersion: 1,
    requestHash: project.generation.requestHash, requestId: 'http_delivery', review: native.pointer,
    ...(producer ? { producedArtifacts } : {}) });
  assert.equal(submitted.status, 200);
  const pending = (await submitted.json()).structuredContent;
  if (mode === 'shutdown') {
    await until(() => traceCalls === 1);
    let closed = false;
    const closing = studio.close().then(() => { closed = true; });
    try {
      await wait(30);
      assert.equal(traceSignal?.aborted, true, 'shutdown must abort a tool-owned trace transfer');
      assert.equal(closed, false, 'shutdown must confirm the pending transfer exit first');
    } finally { releaseTrace(); await closing; }
    const job = project.jobs.find(row => row.id === pending.jobId);
    assert.equal(job.status, 'failed'); assert.equal(job.pendingPreparation, undefined);
    assert.equal(f.remoteCalls.some(row => row.operation === 'submit'), false, 'no remote worker may follow cancelled trace preparation');
    const saved = JSON.parse(await readFile(path.join(studio.store.root, '.state/projects.json'), 'utf8'));
    const diskJob = saved.find(row => row.id === project.id).jobs.find(row => row.id === job.id);
    assert.equal(diskJob.status, 'failed'); assert.equal(diskJob.pendingPreparation, undefined);
    assert.equal(traceCalls, 1);
    return;
  }
  const completed = await f.request('/api/tools/job', { ...scope, jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(completed.status, 200);
  const delivered = (await completed.json()).structuredContent.delivery;
  assert.equal(delivered.accepted, true);
  assert.deepEqual(Object.keys(delivered.products), ['generation', 'demonstrations', 'evaluation']);
  assert.equal(project.delivery.deliveryId, delivered.deliveryId);
  assert.deepEqual(project.events.filter(row => row.kind === 'internal_review_source').map(row => row.phase), ['prepare', 'prepared', 'terminal']);
  assert.equal(native.calls.filter(row => row.method === 'items').length, 3);
  assert.ok(native.calls.every(row => row.id === native.pointer.threadId));
  assert.deepEqual(phases, producer ? ['prepare', 'terminal'] : []);
  if (producer) assert.equal(delivered.producedArtifacts.sha256, producedArtifacts.sha256);
});

test('trace mirroring coalesces queued requests and only resends changed archive attachments', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const calls = [];
  const { studio, project, codex } = await fixture(t, { traceSync: async (_id, trace, archive) => {
    const content = (await readFile(trace.path)).subarray(0, trace.bytes).toString();
    calls.push({ content, archive, trace });
    if (calls.length === 1) { entered(); await gate; }
    return { saved: true, hashes: Object.fromEntries([...archive, trace].map(file => [file.name, file.sha256])) };
  } });
  project.threadId = 'thread-test';
  let lastSnapshot = false;
  const sourceSnapshot = studio.store.sourceSnapshot.bind(studio.store);
  studio.store.sourceSnapshot = async (...args) => {
    const result = await sourceSnapshot(...args);
    if (args[1] === 'sync-queued-3') lastSnapshot = true;
    return result;
  };
  const event = id => codex.onEvent({ method: 'turn/completed', params: { threadId: 'thread-test', turn: { id, status: 'completed' } } });
  event('sync-first'); await started;
  try {
    for (let i = 0; i < 4; i++) event('sync-queued-' + i);
    await until(() => lastSnapshot);
  } finally { release(); }
  await until(() => calls.length >= 2 && project.traceSyncedHash === contentHash(calls.at(-1).content));
  await wait(30);
  assert.equal(calls.length, 2);
  const sentFirst = new Set(calls[0].archive.map(file => file.name));
  assert.ok(sentFirst.size > 0);
  assert.ok(calls[1].archive.every(file => !sentFirst.has(file.name)), 'acknowledged unchanged attachments must not be resent');
  assert.ok(calls[1].content.includes('sync-queued-3'));
  await studio.store.flush(project.id);
});

test('HTTP new collection locks selected outputs and old layouts before the native turn without submitting a new build', async t => {
  const seen = [];
  const f = await fixture(t, { bridge: { async turn(_id, input) { seen.push(input); return { id: 'collection-turn', status: 'inProgress' }; } } });
  const { project, studio, request } = f;
  studio.store.beginGeneration(project.id); project.generation.status = 'complete';
  const oldId = project.generation.id;
  const original = { accepted: true, generationId: oldId, products: { evaluation: { passed: true } } };
  project.delivery = original;
  project.jobs.push({ id: 'previous-environment', kind: 'consume', status: 'completed', delivery: original });
  project.latestBuild = { projectId: project.id, revision: 'layout_a', capabilities: { stageSplit: true }, tasks: [{ id: 'task' }] };
  const environments = [{ revision: 'layout_a', split: 'train', taskIds: ['task'] }];
  const response = await request('/api/projects/p_test/chat', { message: '只采集已有环境的数据', mode: 'generate',
    requiredOutputs: ['demonstrations'], environmentRefs: environments });
  assert.equal(response.status, 200);
  assert.notEqual(project.generation.id, oldId);
  assert.deepEqual(project.generation.requiredOutputs, ['demonstrations']);
  assert.deepEqual(project.generation.environmentRefs, environments);
  assert.match(seen[0], /requiredOutputs=\["demonstrations"\]/);
  assert.match(seen[0], /layout_a/);
  assert.equal(original.accepted, true);
  assert.equal(f.remoteCalls.filter(row => row.operation === 'submit').length, 0);
  const change = await request('/api/projects/p_test/chat', { message: '错误改范围', mode: 'generate', requiredOutputs: ['generation', 'evaluation'] });
  assert.equal(change.status, 409);
});

test('HTTP video-only generation freezes a new environment without requiring training exports', async t => {
  const seen = [];
  const f = await fixture(t, { bridge: { async turn(_id, input) { seen.push(input); return { id: 'video-turn', status: 'inProgress' }; } } });
  const started = await f.request('/api/projects/p_test/chat', { message: 'SYSTEM_VERIFICATION_ONLY 完整视频',
    mode: 'generate', requiredOutputs: ['demonstrations'], videoOnly: true });
  assert.equal(started.status, 200);
  assert.equal(f.project.generation.videoOnly, true);
  assert.match(seen[0], /videoOnly=true/);
  const source = await f.studio.store.sourceSnapshot(f.project.id);
  const build = await f.request('/api/tools/scene_build', { projectId: f.project.id, generationId: f.project.generation.id,
    requestId: 'video-environment', expectedSourceHash: source.sourceHash });
  assert.equal(build.status, 200);
  const shown = await f.request('/api/projects/p_test', undefined, 'GET');
  assert.equal((await shown.json()).generation.videoOnly, true);
});
