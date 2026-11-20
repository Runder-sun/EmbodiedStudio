import test from 'node:test';
import assert from 'node:assert/strict';
import { CodexBridge } from '../src/codex.mjs';

// Real child process and line framing, without login, network or model calls.
const fakeServer = String.raw`
import { createInterface } from 'node:readline';
const mode = process.argv[1];
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
const lines = createInterface({ input: process.stdin });
lines.on('line', line => {
  const message = JSON.parse(line);
  send({ method: 'test/received', params: message });
  if (!message.method) return;
  if (message.method === 'initialized') return;
  if (mode === 'startup-timeout') return;
  if (message.method === 'initialize') return send({ id: message.id, result: { userAgent: 'fake' } });
  if (message.method === 'skills/extraRoots/set') return send({ id: message.id, result: {} });
  if (message.method === 'skills/list') return send({ id: message.id, result: { data: [{ cwd: message.params.cwds[0], errors: [],
    skills: mode === 'missing-skill' ? [] : [{ name: 'scene-generation', path: process.env.TEST_SKILL_PATH, enabled: true }] }] } });
  if (message.method === 'mcpServerStatus/list') return send({ id: message.id, result: { data: [{ name: 'sceneagent',
    runtimeStatus: mode === 'failed-mcp' ? 'failed' : 'connected', toolsError: null,
    tools: { inspect: { name: 'project_inspect' }, observe: { name: 'scene_observe' } } }] } });
  if (message.method === 'thread/read') return send({ id: message.id, result: { thread: { id: message.params.threadId,
    turns: [{ id: 'turn-1', status: 'completed' }] } } });
  if (message.method === 'thread/goal/set' || message.method === 'thread/goal/get') return send({ id: message.id,
    result: { goal: { threadId: message.params.threadId, objective: message.params.objective || '生成实验室', status: 'active' } } });
  if (mode === 'exit') return process.exit(7);
  if (mode === 'timeout') return;
  if (mode === 'error') return send({ id: message.id, error: { code: -32602, message: 'Invalid input', data: { field: 'threadId' } } });
  const result = message.method === 'turn/start' ? { turn: { id: 'turn-1', status: 'inProgress' } }
    : message.method === 'turn/steer' ? { turnId: message.params.expectedTurnId }
    : message.method === 'turn/interrupt' ? {}
    : { thread: { id: message.params.threadId || 'thread-1' } };
  send({ id: message.id, result });
  if (mode === 'resolved') {
    send({ id: 'stale-1', method: 'item/commandExecution/requestApproval', params: { threadId: 'thread-1' } });
    send({ method: 'serverRequest/resolved', params: { requestId: 'stale-1', threadId: 'thread-1' } });
  }
  if (mode === 'requests') {
    send({ id: 'approve-1', method: 'item/commandExecution/requestApproval', params: { command: 'ssh simulation-host', threadId: 'thread-1' } });
    send({ id: 99, method: 'item/tool/requestUserInput', params: { questions: [] } });
    send({ method: 'item/agentMessage/delta', params: { delta: '你好' } });
    send({ method: 'turn/completed', params: { turn: { id: 'turn-1', status: 'completed' } } });
  }
});
`;

function makeBridge(mode = 'normal', options = {}) {
  const events = [];
  const bridge = new CodexBridge({ cwd: process.cwd(),
    command: [process.execPath, '--input-type=module', '-e', fakeServer, mode],
    timeoutMs: 2_000, onEvent: event => events.push(event), ...options });
  return { bridge, events };
}

test('the final native send guard runs after skill and MCP preparation and proves an input was not sent', async t => {
  const skillPath = `${process.cwd()}/skills/scene-generation/SKILL.md`;
  const { bridge, events } = makeBridge('normal', { skillPath,
    env: { TEST_SKILL_PATH: skillPath }, mcp: { requiredTools: ['project_inspect'] } });
  t.after(() => bridge.close());
  const error = Object.assign(new Error('SYSTEM_VERIFICATION_ONLY budget expired'), { budgetExpired: true });
  let guarded = false;
  await assert.rejects(bridge.turn('thread-1', 'must not start', { beforeSend() {
    guarded = true;
    assert.ok(events.some(event => event.method === 'test/received' && event.params.method === 'skills/list'));
    assert.ok(events.some(event => event.method === 'test/received' && event.params.method === 'mcpServerStatus/list'));
    throw error;
  } }), value => value === error && value.nativeInputNotSent === true && value.budgetExpired === true);
  assert.equal(guarded, true);
  assert.equal(events.some(event => event.method === 'test/received' && event.params.method === 'turn/start'), false);
});

test('the final steering guard runs after startup and preserves a proven unsent rejection', async t => {
  const { bridge, events } = makeBridge();
  t.after(() => bridge.close());
  const error = Object.assign(new Error('SYSTEM_VERIFICATION_ONLY stop before steering'), { budgetExpired:true });
  await assert.rejects(bridge.steer('thread-1','turn-1','must not steer',{beforeSend(){
    assert.equal(bridge.status().ready,true);throw error;
  }}),value=>value===error && value.nativeInputNotSent===true);
  assert.equal(events.some(event=>event.method==='test/received' && event.params.method==='turn/steer'),false);
});

async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail('Event did not arrive');
}

test('handshake, thread lifecycle and steering preserve Codex defaults and user approvals', async t => {
  const { bridge, events } = makeBridge();
  t.after(() => bridge.close());
  const [first, second] = await Promise.all([bridge.start(), bridge.start()]);
  assert.deepEqual(first, { userAgent: 'fake' });
  assert.deepEqual(second, first);
  assert.equal(bridge.status().ready, true);
  assert.deepEqual(await bridge.startThread({ instructions: '生成场景' }), { id: 'thread-1' });
  assert.deepEqual(await bridge.resumeThread('stored-thread'), { id: 'stored-thread' });
  assert.equal((await bridge.turn('thread-1', '创建厨房')).id, 'turn-1');
  assert.deepEqual(await bridge.steer('thread-1', 'turn-1', '增加抽屉'), { turnId: 'turn-1' });
  assert.deepEqual(await bridge.interrupt('thread-1', 'turn-1'), {});
  const received = events.filter(event => event.method === 'test/received').map(event => event.params);
  assert.equal(received.filter(message => message.method === 'initialize').length, 1);
  assert.equal(received[1].method, 'initialized');
  for (const method of ['thread/start', 'thread/resume', 'turn/start']) {
    const params = received.find(message => message.method === method).params;
    assert.equal(params.approvalPolicy, 'on-request');
    assert.equal(params.approvalsReviewer, 'user');
    assert.equal('model' in params, false);
    assert.equal('modelProvider' in params, false);
  }
  const startParams = received.find(message => message.method === 'thread/start').params;
  assert.equal(startParams.sandbox, 'workspace-write');
  assert.equal(startParams.developerInstructions, '生成场景');
  assert.equal('baseInstructions' in startParams, false);
  const turnParams = received.find(message => message.method === 'turn/start').params;
  assert.deepEqual(turnParams.input, [{ type: 'text', text: '创建厨房' }]);
  assert.deepEqual(turnParams.sandboxPolicy, { type: 'workspaceWrite', writableRoots: [process.cwd()], networkAccess: false });
  assert.equal(received.find(message => message.method === 'turn/steer').params.expectedTurnId, 'turn-1');
});

test('server requests wait for explicit UI responses; raw notifications are preserved', async t => {
  const requests = [];
  const { bridge, events } = makeBridge('requests', { onRequest: request => requests.push(request) });
  t.after(() => bridge.close());
  await bridge.startThread();
  await until(() => requests.length === 2 && events.some(event => event.method === 'turn/completed'));
  assert.equal(bridge.status().pendingRequests, 2);
  assert.equal(events.filter(event => event.method === 'test/received').some(event => !event.params.method), false);
  assert.equal(events.find(event => event.method === 'item/agentMessage/delta').params.delta, '你好');
  await bridge.respond('approve-1', { decision: 'decline' });
  await bridge.respond(99, { answers: {} });
  await until(() => events.filter(event => event.method === 'test/received' && !event.params.method).length === 2);
  const responses = events.filter(event => event.method === 'test/received' && !event.params.method).map(event => event.params);
  assert.deepEqual(responses[0], { id: 'approve-1', result: { decision: 'decline' } });
  assert.deepEqual(responses[1], { id: 99, result: { answers: {} } });
  assert.equal(bridge.status().pendingRequests, 0);
  await assert.rejects(bridge.respond('approve-1', {}), /no longer pending/);
});

test('server request is surfaced as an event when no request callback is supplied', async t => {
  const { bridge, events } = makeBridge('requests');
  t.after(() => bridge.close());
  await bridge.startThread();
  await until(() => events.filter(event => event.method === 'bridge/request').length === 2);
  assert.equal(events.find(event => event.method === 'bridge/request').params.id, 'approve-1');
});

test('resolved server requests cannot be answered after cancellation', async t => {
  const { bridge, events } = makeBridge('resolved');
  t.after(() => bridge.close());
  await bridge.startThread();
  await until(() => events.some(event => event.method === 'serverRequest/resolved'));
  assert.equal(bridge.status().pendingRequests, 0);
  await assert.rejects(bridge.respond('stale-1', { decision: 'accept' }), /no longer pending/);
});

test('RPC errors retain error code and data and clear pending requests', async t => {
  const { bridge } = makeBridge('error');
  t.after(() => bridge.close());
  await assert.rejects(bridge.startThread(), error => error.code === -32602 && error.data.field === 'threadId');
  assert.equal(bridge.status().pending, 0);
});

test('request timeouts reject and clear timers without fabricating a result', async t => {
  const { bridge } = makeBridge('timeout');
  t.after(() => bridge.close());
  await bridge.start();
  bridge.timeoutMs = 150;
  await assert.rejects(bridge.startThread(), /thread\/start timed out/);
  assert.equal(bridge.status().pending, 0);
});

test('startup timeout terminates the process and clears its pending request', async () => {
  const { bridge } = makeBridge('startup-timeout', { timeoutMs: 150 });
  await assert.rejects(bridge.start(), /initialize timed out/);
  assert.equal(bridge.status().ready, false);
  assert.equal(bridge.status().pid, null);
  assert.equal(bridge.status().pending, 0);
});

test('unexpected exit rejects all pending requests and reports the exit', async () => {
  const { bridge } = makeBridge('exit');
  await bridge.start();
  await assert.rejects(bridge.startThread(), /Codex exited \(7\)/);
  assert.equal(bridge.status().ready, false);
  assert.equal(bridge.status().pending, 0);
});

test('close rejects outstanding work and stops the child', async () => {
  const { bridge } = makeBridge('timeout');
  await bridge.start();
  const pending = bridge.startThread();
  const rejected = assert.rejects(pending, /connection closed|exited/);
  await bridge.close();
  await rejected;
  assert.equal(bridge.status().pid, null);
  assert.equal(bridge.status().pending, 0);
});

test('missing executable rejects initialization and cleans up', async () => {
  const { bridge } = makeBridge('normal', { command: ['/does/not/exist'] });
  await assert.rejects(bridge.start(), /ENOENT/);
  assert.equal(bridge.status().ready, false);
  assert.equal(bridge.status().pid, null);
  assert.equal(bridge.status().pending, 0);
});

test('skill discovery and connected MCP inventory precede an explicitly skilled native turn', async t => {
  const skillPath = `${process.cwd()}/skills/scene-generation/SKILL.md`;
  const { bridge, events } = makeBridge('normal', { skillsRoot: `${process.cwd()}/skills`, skillPath,
    env: { TEST_SKILL_PATH: skillPath },
    mcp: { command: process.execPath, args: ['src/mcp.mjs'], env: { SCENEAGENT_URL: 'http://127.0.0.1:4567' },
      requiredTools: ['project_inspect', 'scene_observe'] } });
  t.after(() => bridge.close());
  const thread = await bridge.startThread();
  await bridge.turn(thread.id, '生成带工作台的实验室');
  const received = events.filter(event => event.method === 'test/received').map(event => event.params);
  assert.deepEqual(received.find(message => message.method === 'skills/extraRoots/set').params,
    { extraRoots: [`${process.cwd()}/skills`] });
  const inventory = received.find(message => message.method === 'mcpServerStatus/list');
  assert.equal(inventory.params.threadId, thread.id);
  assert.equal(inventory.params.serverName, 'sceneagent');
  const turn = received.find(message => message.method === 'turn/start');
  assert.ok(received.indexOf(inventory) < received.indexOf(turn));
  assert.deepEqual(turn.params.input, [{ type: 'text', text: '生成带工作台的实验室' },
    { type: 'skill', name: 'scene-generation', path: skillPath }]);
  assert.deepEqual(events.find(event => event.method === 'transport/sent' && event.params.message.method === 'turn/start').params.message, turn);
  assert.equal(bridge.status().skillLoaded, true);
  assert.ok(bridge.child.spawnargs.includes('mcp_servers.sceneagent.required=true'));
  assert.ok(bridge.child.spawnargs.includes('mcp_servers.sceneagent.env.SCENEAGENT_URL="http://127.0.0.1:4567"'));
});

test('missing skill and failed MCP stop before any model turn', async t => {
  for (const mode of ['missing-skill', 'failed-mcp']) {
    const skillPath = `${process.cwd()}/skills/scene-generation/SKILL.md`;
    const { bridge, events } = makeBridge(mode, { skillsRoot: `${process.cwd()}/skills`, skillPath,
      env: { TEST_SKILL_PATH: skillPath }, mcp: { command: 'node', args: [], requiredTools: ['project_inspect'] } });
    t.after(() => bridge.close());
    await assert.rejects(bridge.startThread(), mode === 'missing-skill' ? /scene-generation.*not available/ : /sceneagent.*failed/);
    assert.equal(events.some(event => event.method === 'test/received' && event.params.method === 'turn/start'), false);
  }
});

test('native thread read and goal methods support recovery without inventing a new conversation', async t => {
  const { bridge, events } = makeBridge();
  t.after(() => bridge.close());
  assert.deepEqual(await bridge.readThread('original-thread'), { id: 'original-thread', turns: [{ id: 'turn-1', status: 'completed' }] });
  assert.equal((await bridge.setGoal('original-thread', '生成实验室')).objective, '生成实验室');
  assert.equal((await bridge.getGoal('original-thread')).status, 'active');
  const sent = events.filter(event => event.method === 'transport/sent').map(event => event.params.message);
  assert.equal(sent.some(message => message.method === 'thread/start'), false);
  const goal = sent.find(message => message.method === 'thread/goal/set').params;
  assert.deepEqual(goal, { threadId: 'original-thread', objective: '生成实验室', origin: 'user' });
  assert.equal('tokenBudget' in goal, false);
});


test('explicit native model is retained across thread start, resume and every new turn', async t => {
  const { bridge, events } = makeBridge('normal', { model: 'gpt-6-sol' });
  t.after(() => bridge.close());
  await bridge.startThread();
  await bridge.resumeThread('original-thread');
  await bridge.turn('original-thread', '继续同一场景');
  await bridge.turn('original-thread', '继续验收');
  const received = events.filter(event => event.method === 'test/received').map(event => event.params);
  const requests = received.filter(message => ['thread/start', 'thread/resume', 'turn/start'].includes(message.method));
  assert.equal(requests.length, 4);
  for (const request of requests) {
    assert.equal(request.params.model, 'gpt-6-sol');
    assert.equal(request.params.approvalPolicy, 'on-request');
    assert.equal(request.params.approvalsReviewer, 'user');
    assert.equal('modelProvider' in request.params, false);
    assert.equal('effort' in request.params, false);
  }
});

test('native pagination forwards exact read-only parameters and exclusive item anchors', async t => {
  const { bridge, events } = makeBridge();
  t.after(() => bridge.close());
  const turns = await bridge.listTurns('review-child', { cursor: 'opaque-turn-page', limit: 4, sortDirection: 'desc', itemsView: 'notLoaded' });
  assert.ok(turns);
  const anchor = { type: 'item', itemId: 'completed-evidence' };
  const items = await bridge.listItems('review-child', { turnId: 'latest', cursor: anchor, limit: 3, sortDirection: 'asc' });
  assert.ok(items);
  const sent = events.filter(event => event.method === 'transport/sent').map(event => event.params.message);
  assert.deepEqual(sent.find(row => row.method === 'thread/turns/list').params, { threadId: 'review-child',
    cursor: 'opaque-turn-page', limit: 4, sortDirection: 'desc', itemsView: 'notLoaded' });
  assert.deepEqual(sent.find(row => row.method === 'thread/items/list').params, { threadId: 'review-child',
    turnId: 'latest', cursor: anchor, limit: 3, sortDirection: 'asc' });
  assert.equal(sent.some(row => ['thread/start', 'thread/resume', 'turn/start'].includes(row.method)), false);
});
