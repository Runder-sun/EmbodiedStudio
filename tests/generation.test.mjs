import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, writeFile, symlink } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { Store } from '../src/store.mjs';
import { GenerationHost } from '../src/generation.mjs';

async function fixture(t, { timeBudgetMs, ...options } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'sceneagent-generation-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const store = await new Store({ root }).load();
  const project = await store.create({ name: '实验室', prompt: '生成完整实验室，包含工作台和工具柜，并保留三个产物。' });
  store.beginGeneration(project.id, { timeBudgetMs });
  project.threadId = 'original-thread';
  project.activeTurnId = 'initial-turn';
  const calls = [];
  const codex = { readThread: async () => ({ id: project.threadId, status: { type: 'idle' }, turns: [] }) };
  const host = new GenerationHost({ store, codex, publish: () => {}, hasApprovals: () => false,
    continueTurn: async (current, input, context) => {
      const persisted = JSON.parse(await (await import('node:fs/promises')).readFile(store.stateFile, 'utf8'));
      assert.equal(persisted[0].generation.pendingContinuation.id, context.eventId);
      assert.equal(persisted[0].generation.pendingContinuation.status, 'pending');
      calls.push({ input, context });
      return { id: `continued-${calls.length}`, status: 'inProgress' };
    }, ...options });
  return { root, store, project, host, calls, codex };
}

// Isolated host fixtures exercise raw recording acceptance, not generated products.
function recordedVideo(project, overrides = {}) {
  return { id: 'recorded-rollout', kind: 'rollout', generationId: project.generation.id, status: 'completed',
    revision: 'recorded-revision', payload: { task: { purpose: 'demonstration' } },
    result: { projectId: project.id, revision: 'recorded-revision', episodes: [{ projectId: project.id,
      revision: 'recorded-revision', taskId: 'recorded-task', episode: 'recorded-episode',
      executionKind: 'actuator_physics_native', score: { success: true, executionStatus: 'completed' },
      artifacts: { video: `projects/${project.id}/episodes/recorded-episode/videos/front.mp4` }, ...overrides }] } };
}

function recordOnlyVideos(project, taskIds = ['recorded-task']) {
  Object.assign(project.generation, { videoOnly: true, requiredOutputs: ['demonstrations'],
    recordOnlyVideos: true, recordOnlyTaskIds: taskIds });
}

test('explicit raw video collection accepts one same-generation historical robot success without review or new work', async t => {
  const { root, project, host, store, calls, codex } = await fixture(t);
  recordOnlyVideos(project);
  const recorded = recordedVideo(project);
  project.jobs.push(recorded, { id: 'unrelated-running-work', generationId: project.generation.id, status: 'running' });
  project.latestBuild = { revision: 'newer-revision' };
  const jobs = structuredClone(project.jobs);
  codex.readThread = async () => { throw new Error('raw collection must not query or restart native work'); };
  store.sourceSnapshot = async () => { throw new Error('raw collection must not read generated sources'); };
  host.readNativeWork = async () => { throw new Error('raw collection must not read independent reviewer work'); };
  host.stopWork = async () => { throw new Error('raw collection must not stop running jobs'); };
  await host.recover(project);
  assert.equal(project.generation.status, 'complete');
  assert.equal(calls.length, 0);
  assert.deepEqual(project.jobs, jobs);
  assert.equal(project.activeTurnId, 'initial-turn');
  assert.deepEqual(project.delivery.videos, [{ jobId: recorded.id, projectId: project.id, revision: 'recorded-revision',
    taskId: 'recorded-task', episode: 'recorded-episode',
    videoRef: recorded.result.episodes[0].artifacts.video, executionKind: 'actuator_physics_native',
    purpose: 'demonstration', reportedSuccess: true }]);
  assert.equal(project.delivery.schema, 'sceneagent-recorded-videos-v1');
  assert.equal(project.delivery.accepted, true);
  assert.equal(project.delivery.assessedBy, 'user_acceptance_without_review');
  assert.equal(project.delivery.reviewSkipped, true);
  assert.equal(project.delivery.checksSkipped, true);
  assert.equal(project.delivery.generationId, project.generation.id);
  assert.equal(project.delivery.requirementVersion, project.generation.requirementVersion);
  assert.equal(project.delivery.requestHash, project.generation.requestHash);
  assert.deepEqual(project.delivery.requiredOutputs, ['demonstrations']);
  assert.deepEqual(project.delivery.recordOnlyTaskIds, ['recorded-task']);
  for (const key of ['passed', 'internalReview', 'consumerSchema', 'consumerArtifactRef', 'products']) assert.equal(Object.hasOwn(project.delivery, key), false);
  const loaded = await new Store({ root }).load();
  assert.deepEqual(loaded.get(project.id).delivery, project.delivery);
  assert.equal((await store.trace(project.id)).at(-1).type, 'generation_complete');
  await host.recover(project);
  assert.equal(calls.length, 0);
});

test('raw collection excludes probes, incomplete execution, missing videos and foreign recorded identities', async t => {
  const cases = [
    ['probe purpose', {}, job => { job.payload.task.purpose = 'probe'; }],
    ['conflicting probe purpose', { purpose: 'probe' }],
    ['non-actuated native probe', { executionKind: 'native_physics' }],
    ['failed result', { score: { success: false, executionStatus: 'completed' } }],
    ['partial execution', { score: { success: true, executionStatus: 'truncated' } }],
    ['success without video', { artifacts: {} }],
    ['foreign video', { artifacts: { video: 'projects/other/episodes/recorded-episode/video.mp4' } }],
    ['foreign episode', { episode: 'another-episode' }],
    ['foreign project', { projectId: 'another-project' }],
    ['wrong task', { taskId: 'another-task' }],
    ['wrong generation', {}, job => { job.generationId = 'another-generation'; }],
    ['unfinished job', {}, job => { job.status = 'running'; }],
    ['non-rollout job', {}, job => { job.kind = 'check'; }],
    ['local cache only', {}, job => { job.result.episodes = []; }],
  ];
  for (const [name, overrides, change] of cases) await t.test(name, async t => {
    const { project, host, calls, codex } = await fixture(t);
    recordOnlyVideos(project);
    const job = recordedVideo(project, overrides);
    project.episodes = structuredClone(job.result.episodes);
    change?.(job);
    project.jobs.push(job);
    codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
    await host.recover(project);
    assert.notEqual(project.generation.status, 'complete');
    assert.equal(project.delivery?.accepted, undefined);
    assert.equal(calls.length, 0);
  });
});

test('raw video collection covers every locked task and accepts a camera video artifact', async t => {
  const { project, host, codex } = await fixture(t);
  recordOnlyVideos(project, ['recorded-task', 'second-task']);
  const first = recordedVideo(project);
  project.jobs.push(first);
  codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
  await host.recover(project);
  assert.notEqual(project.generation.status, 'complete');
  const second = recordedVideo(project, { taskId: 'second-task', episode: 'second-episode',
    artifacts: { 'video:front': `projects/${project.id}/episodes/second-episode/videos/front.mp4` } });
  second.id = 'second-rollout';
  project.jobs.push(second);
  await host.jobCompleted(project, second);
  assert.equal(project.generation.status, 'complete');
  assert.deepEqual(project.delivery.videos.map(row => row.taskId), ['recorded-task', 'second-task']);
  assert.equal(project.delivery.videos[1].videoRef, second.result.episodes[0].artifacts['video:front']);
});

test('raw receipts cannot bypass the default completion path or a changed collection scope', async t => {
  for (const change of ['default', 'no video-only flag', 'full outputs', 'empty tasks', 'changed tasks']) await t.test(change, async t => {
    const { project, host, codex } = await fixture(t);
    recordOnlyVideos(project);
    project.jobs.push(recordedVideo(project));
    await host.recover(project);
    assert.equal(project.generation.status, 'complete');
    project.generation.status = 'active';
    if (change === 'default') delete project.generation.recordOnlyVideos;
    if (change === 'no video-only flag') delete project.generation.videoOnly;
    if (change === 'full outputs') project.generation.requiredOutputs = ['generation', 'demonstrations', 'evaluation'];
    if (change === 'empty tasks') project.generation.recordOnlyTaskIds = [];
    if (change === 'changed tasks') project.generation.recordOnlyTaskIds = ['new-task'];
    codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
    await host.recover(project);
    assert.notEqual(project.generation.status, 'complete');
  });
});

test('successful raw recordings alone do not complete a default generation', async t => {
  const { project, host, codex } = await fixture(t);
  project.jobs.push(recordedVideo(project));
  codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
  await host.recover(project);
  assert.notEqual(project.generation.status, 'complete');
  assert.equal(project.delivery, undefined);
});

test('incomplete raw collection continues the original robot request without source inspection or delivery review', async t => {
  const { project, host, store, calls } = await fixture(t);
  recordOnlyVideos(project);
  project.jobs.push(recordedVideo(project, { artifacts: {} }));
  let sourceReads = 0;
  store.sourceSnapshot = async () => { sourceReads++; throw new Error('raw recording mode excludes source inspection'); };
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(calls.length, 1);
  assert.equal(sourceReads, 0);
  assert.match(calls[0].input, /锁定任务 \["recorded-task"\]/);
  assert.match(calls[0].input, /跳过交付检查和独立审核/);
  assert.match(calls[0].input, /不生成训练数据/);
  assert.match(calls[0].input, /已有成功记录直接收录，不重跑/);
  assert.doesNotMatch(calls[0].input, /按本次交付范围完成必要生成/);
});

test('a raw receipt missing its locked task binding is not accepted', async t => {
  const { project, host } = await fixture(t);
  recordOnlyVideos(project);
  project.delivery = { accepted: true, recordOnlyVideos: true, generationId: project.generation.id,
    requirementVersion: project.generation.requirementVersion, requestHash: project.generation.requestHash,
    requiredOutputs: ['demonstrations'] };
  assert.equal(host._accepted(project), false);
});

test('premature native completion preserves the prompt and continues until a verified delivery', async t => {
  const { project, host, calls } = await fixture(t);
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].input.includes(project.prompt));
  assert.equal(project.activeTurnId, 'continued-1');
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.continuationCount, 1);
  assert.equal(project.generation.pendingContinuation.status, 'sent');
  project.messages.push({ role: 'assistant', content: '三个产品全部完成。' });
  await writeFile(path.join(host.store.directory(project.id), 'scene.py'), '# real next source\n');
  await host.turnCompleted(project, { id: 'continued-1', status: 'completed' });
  assert.equal(project.generation.status, 'active');
  assert.equal(calls.length, 2);
  project.delivery = { accepted: true, generationId: project.generation.id,
    requirementVersion: project.generation.requirementVersion, requestHash: project.generation.requestHash };
  await host.turnCompleted(project, { id: 'continued-2', status: 'completed' });
  assert.equal(project.generation.status, 'complete');
  assert.equal(calls.length, 2);
});

test('pending remote work waits; one terminal job wakes the original thread once', async t => {
  const { project, host, calls } = await fixture(t);
  const job = { id: 'job-1', generationId: project.generation.id, status: 'running' };
  project.jobs.push(job);
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(project.generation.status, 'waiting_jobs');
  assert.equal(calls.length, 0);
  job.status = 'completed'; job.result = { revision: 'rev-1', score: { success: false } };
  await Promise.all([host.jobCompleted(project, job), host.jobCompleted(project, job)]);
  assert.equal(calls.length, 1);
  assert.match(calls[0].input, /rev-1/);
  assert.match(calls[0].input, /false/);
  assert.equal(project.generation.status, 'active');
});

test('a terminal overloaded root continues once after its durable job completes', async t => {
  const { createNativeWorkReader } = await import('../src/server.mjs');
  const { project, host, codex, calls } = await fixture(t);
  const failed = { id: 'initial-turn', status: 'failed', items: [],
    error: { codexErrorInfo: 'serverOverloaded', message: 'Selected model is at capacity.' } };
  codex.readThread = async (id, { includeTurns }) => ({ id, status: { type: 'systemError' },
    turns: includeTurns ? [failed] : [] });
  codex.listTurns = async () => ({ data: [failed], nextCursor: null });
  host.readNativeWork = createNativeWorkReader({ codex });
  const job = { id: 'overload-inflight', generationId: project.generation.id, status: 'running' };
  project.jobs.push(job);
  await host.turnCompleted(project, failed);
  assert.equal(calls.length, 0);
  job.status = 'completed';
  await Promise.all([host.jobCompleted(project, job), host.jobCompleted(project, job)]);
  assert.equal(calls.length, 1);
  assert.equal(project.generation.status, 'active');
});

test('recovery matches an ambiguously sent continuation to the native user input instead of resending', async t => {
  const { project, store, root, host, calls, codex } = await fixture(t);
  project.activeTurnId = null;
  project.generation.pendingContinuation = { id: 'wake-1', input: '[续接事件 wake-1] 原要求', status: 'pending', dispatchAttemptedAt: new Date().toISOString() };
  project.generation.continuationCount = 1;
  await store.save();
  const reloaded = await new Store({ root }).load();
  const recovered = reloaded.get(project.id);
  codex.readThread = async () => ({ id: project.threadId, status: { type: 'active' }, turns: [{ id: 'actually-sent', status: 'inProgress',
    items: [{ type: 'userMessage', content: [{ type: 'text', text: project.generation.pendingContinuation.input }] }] }] });
  const restarted = new GenerationHost({ store: reloaded, codex, continueTurn: async () => { calls.push({}); }, publish: () => {} });
  await restarted.recover(recovered);
  assert.equal(calls.length, 0);
  assert.equal(recovered.activeTurnId, 'actually-sent');
  assert.equal(recovered.generation.pendingContinuation.status, 'sent');
  assert.equal(recovered.generation.continuationCount, 1);
});

test('an unconfirmed ambiguous send stays recoverable rather than blindly repeating', async t => {
  const { project, host, calls } = await fixture(t);
  project.activeTurnId = null;
  project.generation.pendingContinuation = { id: 'uncertain', input: '[续接事件 uncertain]', status: 'pending', dispatchAttemptedAt: new Date().toISOString() };
  await host.recover(project);
  assert.equal(calls.length, 0);
  assert.equal(project.generation.status, 'recovering');
  assert.match(project.generation.recoveryIssue, /确认/);
});

test('a native active turn and approval wait prevent a second root turn', async t => {
  const { project, host, calls, codex } = await fixture(t);
  codex.readThread = async () => ({ id: project.threadId, status: { type: 'active' }, turns: [{ id: 'native-next', status: 'inProgress', items: [] }] });
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(calls.length, 0);
  assert.equal(project.activeTurnId, 'native-next');
  host.hasApprovals = () => true;
  await host.recover(project);
  assert.equal(project.generation.status, 'waiting_user');
  assert.equal(calls.length, 0);
});

test('old requirement receipts cannot complete a changed request', async t => {
  const { project, store, host, calls } = await fixture(t);
  project.delivery = { accepted: true, generationId: project.generation.id,
    requirementVersion: 1, requestHash: project.generation.requestHash };
  await store.addRequirement(project.id, '还必须包含独立储藏室。');
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(project.generation.status, 'active');
  assert.equal(calls.length, 1);
  assert.match(calls[0].input, /独立储藏室/);
  assert.match(calls[0].input, /完整实验室/);
});

test('continuation counts do not stop a generation while an explicit elapsed time budget still does', async t => {
  const counted = await fixture(t, { timeBudgetMs: 60 * 60_000 });
  counted.project.generation.continuationCount = 24;
  await counted.host.turnCompleted(counted.project, { id: 'initial-turn', status: 'completed' });
  assert.equal(counted.calls.length, 1);
  assert.equal(counted.project.generation.continuationCount, 25);
  assert.equal(counted.project.generation.status, 'active');
  const elapsed = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => Date.now() + 61 * 60_000 });
  await elapsed.host.turnCompleted(elapsed.project, { id: 'initial-turn', status: 'completed' });
  assert.equal(elapsed.calls.length, 0);
  assert.match(elapsed.project.generation.terminalReason, /时间/);
});

test('an unlimited time budget survives recovery beyond an hour and remains cancellable', async t => {
  const stops = [];
  const { root, store, project, host, codex, calls } = await fixture(t, {
    now: () => Date.now() + 3 * 60 * 60_000,
    stopWork: async () => stops.push('stop'),
  });
  project.generation.timeBudgetMs = null;
  codex.readThread = async () => ({ status: { type: 'active' },
    turns: [{ id: 'initial-turn', status: 'inProgress' }] });
  await store.save();
  const recoveredStore = await new Store({ root }).load();
  const recovered = recoveredStore.get(project.id);
  const restarted = new GenerationHost({ store: recoveredStore, codex, now: host.now,
    continueTurn: async () => calls.push('unexpected continuation'), stopWork: host.stopWork });
  await restarted.recover(recovered);
  assert.equal(recovered.generation.status, 'active');
  assert.equal(recovered.generation.timeBudgetMs, null);
  assert.equal(stops.length, 0);
  assert.equal(calls.length, 0);
  codex.readThread = async () => ({ status: { type: 'idle' }, turns: [] });
  await restarted.cancel(recovered);
  assert.equal(recovered.generation.status, 'cancelled');
  assert.equal(stops.length, 1);
});

test('a missing legacy time budget remains unlimited across recovery without being backfilled', async t => {
  const { root, store, project, host, codex, calls } = await fixture(t, { now: () => Date.now() + 3 * 60 * 60_000 });
  delete project.generation.timeBudgetMs;
  project.generation.continuationCount = 50;
  await store.save();
  const loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
  const restarted = new GenerationHost({ store: loaded, codex, now: host.now,
    continueTurn: async () => { calls.push({}); return { id: 'legacy-continued', status: 'inProgress' }; } });
  await restarted.recover(recovered);
  assert.equal(recovered.generation.status, 'active');
  assert.equal(Object.hasOwn(recovered.generation, 'timeBudgetMs'), false);
  assert.equal(recovered.generation.continuationCount, 51);
  assert.equal(calls.length, 1);
});

test('the synchronous budget assertion shares expiry rules without mutating generation state', async t => {
  for (const timeBudgetMs of [100, null, undefined]) await t.test(String(timeBudgetMs), async t => {
    let current = 0;
    const { project, host } = await fixture(t, { timeBudgetMs, now: () => current });
    project.generation.startedAt = new Date(0).toISOString();
    if (timeBudgetMs === undefined) delete project.generation.timeBudgetMs;
    const before = structuredClone(project.generation);
    assert.equal(host.assertBudget(project.generation), undefined);
    current = 100;
    if (timeBudgetMs === 100) assert.throws(() => host.assertBudget(project.generation), error =>
      error.status === 409 && error.nativeInputNotSent === true && error.budgetExpired === true && /生成时间预算已达到/.test(error.message));
    else assert.equal(host.assertBudget(project.generation), undefined);
    assert.deepEqual(project.generation, before);
  });
});

test('the shared budget assertion excludes recorded approval and system waiting time', async t => {
  let current = 139;
  const { project, host } = await fixture(t, { timeBudgetMs: 100, now: () => current });
  Object.assign(project.generation, { startedAt: new Date(0).toISOString(), approvalWaitMs: 30, systemWaitMs: 10 });
  assert.equal(host.assertBudget(project.generation), undefined);
  current = 140;
  assert.throws(() => host.assertBudget(project.generation), { status: 409, nativeInputNotSent: true, budgetExpired: true });
});

test('opening an initial input defers automatic continuation without blocking cancellation or budgets', async t => {
  let opening = true;
  const { project, host, calls } = await fixture(t, { hasOpeningInput: () => opening });
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(calls.length, 0);
  assert.equal(project.generation.continuationCount, 0);
  opening = false;
  await host.recover(project);
  assert.equal(calls.length, 1);

  const cancelled = await fixture(t, { hasOpeningInput: () => true });
  await cancelled.host.cancel(cancelled.project);
  assert.equal(cancelled.project.generation.status, 'cancelled');
  assert.equal(cancelled.calls.length, 0);

  const elapsed = await fixture(t, { timeBudgetMs: 60 * 60_000, hasOpeningInput: () => true, now: () => Date.now() + 61 * 60_000 });
  await elapsed.host.recover(elapsed.project);
  assert.equal(elapsed.project.generation.status, 'failed');
  assert.match(elapsed.project.generation.terminalReason, /60 分钟/);
});

test('cancellation requires all remote terminals and late results cannot wake it', async t => {
  const { project, host, calls } = await fixture(t);
  const job = { id: 'job-1', generationId: project.generation.id, status: 'running' };
  project.jobs.push(job);
  await host.cancel(project);
  assert.equal(project.generation.status, 'cancel_requested');
  job.status = 'cancel_requested';
  await host.jobCompleted(project, job);
  assert.equal(project.generation.status, 'cancel_requested');
  job.status = 'cancelled';
  await host.jobCompleted(project, job);
  assert.equal(project.generation.status, 'cancelled');
  await host.jobCompleted(project, { ...job, status: 'completed', result: { revision: 'late' } });
  assert.equal(project.generation.status, 'cancelled');
  assert.equal(calls.length, 0);
});

test('forty minutes waiting for approval do not consume the sixty minute task budget', async t => {
  const started = Date.now(); let current = started, pending = false;
  const { project, host, calls, codex } = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => current, hasApprovals: () => pending });
  codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
  project.generation.startedAt = new Date(started).toISOString();
  current = started + 20 * 60_000; pending = true;
  await host.recover(project);
  current = started + 60 * 60_000; pending = false;
  await host.recover(project);
  assert.equal(project.generation.status, 'active');
  current = started + 70 * 60_000;
  await host.recover(project);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.approvalWaitMs, 40 * 60_000);
  assert.equal(project.generation.approvalWaitingSince, null);
  assert.equal(project.generation.startedAt, new Date(started).toISOString());
  assert.equal(calls.length, 0);
});

test('sixty minutes of task time excluding approvals still stops at the original limit', async t => {
  const started = Date.now(); let current = started;
  const { project, host, calls } = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => current });
  project.generation.startedAt = new Date(started).toISOString();
  project.generation.approvalWaitMs = 40 * 60_000;
  current = started + 100 * 60_000;
  await host.recover(project);
  assert.equal(project.generation.status, 'failed');
  assert.match(project.generation.terminalReason, /60 分钟/);
  assert.equal(project.generation.startedAt, new Date(started).toISOString());
  assert.equal(calls.length, 0);
});

test('persisted approval waiting survives recovery and is not subtracted twice', async t => {
  const started = Date.now(); let current = started, pending = true;
  const { root, project, store, host, codex, calls } = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => current, hasApprovals: () => pending });
  project.generation.startedAt = new Date(started).toISOString();
  current = started + 10 * 60_000;
  await host.recover(project);
  const reloaded = await new Store({ root }).load(), recovered = reloaded.get(project.id);
  codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'initial-turn', status: 'inProgress' }] });
  const restarted = new GenerationHost({ store: reloaded, codex, now: () => current, hasApprovals: () => pending,
    publish: () => {}, continueTurn: async () => { calls.push({}); } });
  current = started + 40 * 60_000;
  await restarted.recover(recovered);
  await restarted.recover(recovered);
  current = started + 50 * 60_000; pending = false;
  await restarted.recover(recovered);
  current = started + 51 * 60_000;
  await restarted.recover(recovered);
  assert.equal(recovered.generation.approvalWaitMs, 40 * 60_000);
  assert.equal(recovered.generation.approvalWaitingSince, null);
  assert.equal(recovered.generation.status, 'active');
  assert.equal(calls.length, 0);
});

test('approval event boundaries account overlapping waits without a reconciliation tick or duplicate close', async t => {
  const started = Date.now(); let current = started, pending = false;
  const { project, host } = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => current, hasApprovals: () => pending });
  project.generation.startedAt = new Date(started).toISOString();
  current = started + 20 * 60_000; pending = true;
  await host.approvalsChanged(project);
  current = started + 30 * 60_000;
  await host.approvalsChanged(project);
  current = started + 50 * 60_000;
  await host.approvalsChanged(project);
  current = started + 60 * 60_000; pending = false;
  await host.approvalsChanged(project);
  current = started + 61 * 60_000;
  await host.approvalsChanged(project);
  assert.equal(project.generation.approvalWaitMs, 40 * 60_000);
  assert.equal(project.generation.approvalWaitingSince, null);
  current = started + 70 * 60_000;
  await host.recover(project);
  assert.equal(project.generation.status, 'active');
});

test('an incomplete internal-review receipt continues the native generator instead of finishing', async t => {
  const { project, host, calls } = await fixture(t);
  project.delivery = { accepted: false, status: 'incomplete', generationId: project.generation.id,
    requirementVersion: project.generation.requirementVersion, requestHash: project.generation.requestHash,
    missing: ['当前内部独立审核来源缺失'] };
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.notEqual(project.generation.status, 'complete'); assert.equal(calls.length, 1);
  assert.match(calls[0].input, /内部独立审核来源缺失/);
});

test('cancellation keeps the native turn pending until its stop is confirmed', async t => {
  const stops = [];
  const { project, host, codex, calls } = await fixture(t, { stopWork: async (_project, state) => stops.push(state) });
  let running = true;
  codex.readThread = async () => ({ status: { type: running ? 'active' : 'idle' },
    turns: running ? [{ id: 'native-running', status: 'inProgress' }] : [] });
  await host.cancel(project);
  assert.equal(project.generation.status, 'cancel_requested');
  assert.equal(stops[0].turnId, 'native-running');
  assert.equal(project.generation.completedAt, undefined);
  running = false;
  await host.recover(project);
  assert.equal(project.generation.status, 'cancelled');
  assert.equal(project.activeTurnId, null);
  assert.equal(calls.length, 0);
});

test('a failed native stop query preserves cancellation across recovery and restart', async t => {
  const { project, host, codex, store, root, calls } = await fixture(t);
  codex.readThread = async () => { throw new Error('native connection unavailable'); };
  await host.cancel(project);
  assert.equal(project.generation.status, 'recovering');
  const cancellation = project.generation.cancelRequestedAt;
  assert.ok(cancellation);
  await store.save();
  const loaded = await new Store({ root }).load();
  const recovered = loaded.get(project.id);
  const restarted = new GenerationHost({ store: loaded, codex,
    continueTurn: async () => calls.push({}), stopWork: async () => {} });
  await restarted.recover(recovered);
  assert.equal(recovered.generation.cancelRequestedAt, cancellation);
  assert.equal(recovered.generation.status, 'recovering');
  codex.readThread = async () => ({ status: { type: 'idle' }, turns: [] });
  await restarted.recover(recovered);
  assert.equal(recovered.generation.status, 'cancelled');
  assert.equal(calls.length, 0);
});

test('an elapsed budget stops actual work before publishing a failed terminal', async t => {
  const stops = [];
  const { project, host, codex } = await fixture(t, {
    timeBudgetMs: 60 * 60_000,
    now: () => Date.now() + 61 * 60_000,
    stopWork: async (_project, state) => stops.push(state),
  });
  const job = { id: 'still-running', generationId: project.generation.id, status: 'running' };
  project.jobs.push(job);
  let running = true;
  codex.readThread = async () => ({ status: { type: running ? 'active' : 'idle' },
    turns: running ? [{ id: 'budget-running', status: 'inProgress' }] : [] });
  await host.recover(project);
  assert.equal(project.generation.status, 'cancel_requested');
  assert.equal(stops[0].turnId, 'budget-running');
  assert.equal(project.generation.completedAt, undefined);
  running = false;
  await host.recover(project);
  assert.equal(project.generation.status, 'cancel_requested');
  job.status = 'cancelled';
  await host.recover(project);
  assert.equal(project.generation.status, 'failed');
  assert.match(project.generation.terminalReason, /60 分钟/);
});

test('a finite budget expiring during source reads, native work checks or dispatch saves prevents a new turn', async t => {
  for (const phase of ['source success', 'source failure', 'native work', 'dispatch save']) await t.test(phase, async t => {
    let current = 0; const stops = [];
    const { project, host, store, calls } = await fixture(t, { timeBudgetMs: 100, now: () => current,
      stopWork: async (_project, state) => stops.push(state) });
    project.generation.startedAt = new Date(0).toISOString();
    const snapshot = store.sourceSnapshot.bind(store), save = store.save.bind(store);
    store.sourceSnapshot = async (...args) => {
      if (phase.startsWith('source')) current = 101;
      if (phase === 'source failure') throw new Error('source unavailable after budget expiry');
      return snapshot(...args);
    };
    host.readNativeWork = async () => {
      if (phase === 'native work') current = 101;
      return { ready: true, completed: [] };
    };
    store.save = async () => {
      if (phase === 'dispatch save' && project.generation.pendingContinuation?.dispatchAttemptedAt) current = 101;
      return save();
    };
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    assert.equal(calls.length, 0);
    assert.equal(project.generation.status, 'failed');
    assert.match(project.generation.terminalReason, /生成时间预算已达到/);
    assert.equal(project.generation.waitingSystem, undefined);
    assert.equal(project.generation.pendingContinuation, null);
    assert.equal(stops.length, 1);
    assert.equal((await store.trace(project.id)).some(event => event.type === 'continuation_sent'), false);
  });
});

test('null and missing budgets still permit diagnostic turns after asynchronous reads and saves', async t => {
  for (const timeBudgetMs of [null, undefined]) for (const phase of ['source success', 'source failure', 'dispatch save']) await t.test(String(timeBudgetMs)+' '+phase, async t => {
    let current = 0;
    const { project, host, store, calls } = await fixture(t, { timeBudgetMs, now: () => current,
      stopWork: async () => assert.fail('an unlimited generation must not be stopped') });
    if (timeBudgetMs === undefined) delete project.generation.timeBudgetMs;
    project.generation.startedAt = new Date(0).toISOString();
    const snapshot = store.sourceSnapshot.bind(store), save = store.save.bind(store);
    store.sourceSnapshot = async (...args) => {
      if (phase.startsWith('source')) current = 101;
      if (phase === 'source failure') throw new Error('source unavailable without a total budget');
      return snapshot(...args);
    };
    store.save = async () => {
      if (phase === 'dispatch save' && project.generation.pendingContinuation?.dispatchAttemptedAt) current = 101;
      return save();
    };
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    assert.equal(calls.length, 1); assert.equal(project.generation.status, 'active');
    assert.equal(project.generation.waitingSystem, undefined);
    assert.equal(Object.hasOwn(project.generation, 'timeBudgetMs'), timeBudgetMs !== undefined);
  });
});

test('an explicit wait or cancellation during a dispatch save keeps precedence over an elapsed budget', async t => {
  for (const action of ['wait', 'cancel']) await t.test(action, async t => {
    let current = 0, started, release;
    const saving = new Promise(resolve => { started = resolve; }), reply = new Promise(resolve => { release = resolve; });
    const { project, host, store, calls } = await fixture(t, { timeBudgetMs: 100, now: () => current });
    project.generation.startedAt = new Date(0).toISOString();
    const save = store.save.bind(store); let blocked = false;
    store.save = async () => {
      if (!blocked && project.generation.pendingContinuation?.dispatchAttemptedAt) { blocked = true; started(); await reply; }
      return save();
    };
    const running = host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    await saving; current = 101;
    const request = action === 'wait' ? host.waitSystem(project, { projectId: project.id, generationId: project.generation.id,
      requestId: 'wait-at-expired-dispatch', reason: 'actual external blocker', evidence: ['actual error'] }) : host.cancel(project);
    release(); await Promise.all([running, request]);
    assert.equal(calls.length, 0);
    assert.equal(project.generation.status, action === 'wait' ? 'waiting_system' : 'cancelled');
    if (action === 'wait') assert.equal(project.generation.waitingSystem.requestId, 'wait-at-expired-dispatch');
    else assert.doesNotMatch(project.generation.terminalReason, /生成时间预算/);
  });
});

test('budget expiry preserves an earlier uncertain send until the native stop is confirmed', async t => {
  let current = 0, attempts = 0;
  const { project, host, codex } = await fixture(t, { timeBudgetMs: 100, now: () => current,
    continueTurn: async () => { attempts++; throw new Error('original dispatch response unavailable'); } });
  project.generation.startedAt = new Date(0).toISOString();
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  const pending = structuredClone(project.generation.pendingContinuation);
  current = 101;
  codex.readThread = async () => { throw new Error('native stop is not yet confirmed'); };
  await host.recover(project);
  assert.equal(attempts, 1); assert.equal(project.generation.status, 'recovering');
  assert.equal(project.generation.pendingContinuation.id, pending.id);
  assert.equal(project.generation.pendingContinuation.dispatchAttemptedAt, pending.dispatchAttemptedAt);
  assert.equal(project.generation.completedAt, undefined);
  codex.readThread = async () => ({ status: { type: 'idle' }, turns: [] });
  await host.recover(project);
  assert.equal(attempts, 1); assert.equal(project.generation.status, 'failed');
  assert.equal(project.generation.pendingContinuation, null);
});

test('the final native send callback rechecks the same budget after asynchronous preparation', async t => {
  for (const timeBudgetMs of [100, null, undefined]) await t.test(String(timeBudgetMs), async t => {
    let current = 0, preparations = 0, sends = 0, receipt, eventId;
    const { project, host, store } = await fixture(t, { timeBudgetMs, now: () => current,
      continueTurn: async (active, _input, context) => {
        preparations++; eventId = context.eventId;
        assert.equal(active.generation.pendingContinuation.id, eventId);
        try {
          context.beforeSend();
          await Promise.resolve(); current = 101;
          context.beforeSend();
        } catch (error) { receipt = error; throw error; }
        sends++;
        return { id: 'after-preparation', status: 'inProgress' };
      } });
    project.generation.startedAt = new Date(0).toISOString();
    if (timeBudgetMs === undefined) delete project.generation.timeBudgetMs;
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    assert.equal(preparations, 1);
    assert.equal(eventId, (await store.trace(project.id)).find(event => event.type === 'continuation_planned').continuationId);
    assert.equal(sends, timeBudgetMs === 100 ? 0 : 1);
    assert.equal(project.generation.status, timeBudgetMs === 100 ? 'failed' : 'active');
    if (timeBudgetMs === 100) {
      assert.equal(receipt.nativeInputNotSent, true); assert.equal(receipt.budgetExpired, true);
      assert.equal(receipt.status, 409);
      assert.equal(project.generation.terminalReason, receipt.message);
      assert.equal(project.generation.pendingContinuation, null);
    } else assert.equal(receipt, undefined);
  });
});

test('late budget errors need affirmative unsent evidence before stopping the generation', async t => {
  for (const unsent of [false, true]) await t.test(String(unsent), async t => {
    let current = 0;
    const { project, host } = await fixture(t, { timeBudgetMs: 100, now: () => current, continueTurn: async () => {
      current = 101;
      const error = Object.assign(new Error('total budget expired before native RPC'), { budgetExpired: true });
      if (unsent) error.nativeInputNotSent = true;
      throw error;
    } });
    project.generation.startedAt = new Date(0).toISOString();
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    assert.equal(project.generation.status, unsent ? 'failed' : 'recovering');
    if (unsent) assert.equal(project.generation.pendingContinuation, null);
    else assert.ok(project.generation.pendingContinuation.dispatchAttemptedAt);
  });
});

test('late preparation guards are checked before an expired budget can stop the generation', async t => {
  for (const guard of ['approval', 'opening input', 'job', 'active turn', 'accepted', 'generation', 'pending']) await t.test(guard, async t => {
    let current = 0, approval = false, opening = false, sends = 0, receipt, replacement;
    const { project, host } = await fixture(t, { timeBudgetMs: 100, now: () => current,
      hasApprovals: () => approval, hasOpeningInput: () => opening,
      continueTurn: async (active, _input, context) => {
        await Promise.resolve(); current = 101;
        if (guard === 'approval') approval = true;
        if (guard === 'opening input') opening = true;
        if (guard === 'job') active.jobs.push({ id: 'late-job', generationId: active.generation.id, status: 'running' });
        if (guard === 'active turn') active.activeTurnId = 'another-native-turn';
        if (guard === 'accepted') active.delivery = { accepted: true, generationId: active.generation.id,
          requirementVersion: active.generation.requirementVersion, requestHash: active.generation.requestHash };
        if (guard === 'generation') active.generation = { ...active.generation, id: 'replacement', status: 'draft' };
        if (guard === 'pending') active.generation.pendingContinuation = { id: 'replacement-pending', status: 'pending' };
        if (guard === 'generation' || guard === 'pending') replacement = structuredClone(active.generation);
        try { context.beforeSend(); } catch (error) { receipt = error; throw error; }
        sends++;
        return { id: 'must-not-send', status: 'inProgress' };
      } });
    project.generation.startedAt = new Date(0).toISOString();
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    assert.equal(sends, 0); assert.equal(receipt.nativeInputNotSent, true);
    assert.notEqual(receipt.budgetExpired, true);
    if (replacement) assert.deepEqual(project.generation, replacement);
    else {
      assert.equal(project.generation.cancelRequestedAt, undefined);
      assert.equal(project.generation.pendingContinuation.dispatchAttemptedAt, undefined);
    }
  });
});

test('a late explicit wait or cancellation keeps precedence over the final send budget callback', async t => {
  for (const action of ['wait', 'cancel']) await t.test(action, async t => {
    let current = 0, sends = 0; const requests = [];
    const { project, host } = await fixture(t, { timeBudgetMs: 100, now: () => current,
      continueTurn: async (active, _input, context) => {
        await Promise.resolve(); current = 101;
        requests.push(action === 'wait' ? host.waitSystem(active, { projectId: active.id, generationId: active.generation.id,
          requestId: 'late-reported-wait', reason: 'actual external blocker', evidence: ['actual error'] }) : host.cancel(active));
        context.beforeSend(); sends++;
        return { id: 'must-not-send', status: 'inProgress' };
      } });
    project.generation.startedAt = new Date(0).toISOString();
    await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
    await Promise.all(requests);
    assert.equal(sends, 0);
    assert.equal(project.generation.status, action === 'wait' ? 'waiting_system' : 'cancelled');
    if (action === 'wait') assert.equal(project.generation.waitingSystem.requestId, 'late-reported-wait');
    else assert.doesNotMatch(project.generation.terminalReason, /生成时间预算/);
  });
});

test('an automatic turn without observable persisted work receives one diagnostic continuation', async t => {
  const { project, host, calls, codex } = await fixture(t);
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  await host.turnCompleted(project, { id: 'continued-1', status: 'completed' });
  codex.readThread = async () => ({ status: { type: 'active' }, turns: [{ id: 'continued-2', status: 'inProgress' }] });
  for (let index = 0; index < 4; index++) await host.recover(project);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.waitingSystem, undefined);
  assert.equal(project.activeTurnId, 'continued-2');
  assert.equal(calls.length, 2);
  assert.match(calls[1].input, /源码、需求、实际作业及已完成原生协作指针均未变化/);
  assert.ok(calls[1].input.indexOf('均未变化') < calls[1].input.indexOf('完整持久要求'));
});

test('the first stop outcome stays locked when a later user cancellation arrives', async t => {
  const { project, host, codex } = await fixture(t, { timeBudgetMs: 60 * 60_000, now: () => Date.now() + 61 * 60_000 });
  let running = true;
  codex.readThread = async () => ({ status: { type: running ? 'active' : 'idle' },
    turns: running ? [{ id: 'budget-running', status: 'inProgress' }] : [] });
  await host.recover(project);
  const reason = project.generation.terminalReason;
  await host.cancel(project);
  assert.equal(project.generation.stopStatus, 'failed');
  assert.equal(project.generation.terminalReason, reason);
  running = false;
  await host.recover(project);
  assert.equal(project.generation.status, 'failed');
});

test('cancellation is visible while native reconciliation is still awaiting a response', async t => {
  const { project, host, codex, calls } = await fixture(t);
  let reading, respond;
  const started = new Promise(resolve => { reading = resolve; });
  const reply = new Promise(resolve => { respond = resolve; });
  codex.readThread = async () => { reading(); return reply; };
  project.activeTurnId = null;
  const reconciliation = host.recover(project);
  await started;
  const cancelled = host.cancel(project);
  assert.ok(project.generation.cancelRequestedAt);
  respond({ status: { type: 'idle' }, turns: [] });
  await Promise.all([reconciliation, cancelled]);
  assert.equal(project.generation.status, 'cancelled');
  assert.equal(calls.length, 0);
});

test('active budget polling reads metadata and recovers a missed completion without repeated full history', async t => {
  const { project, host, codex, calls } = await fixture(t);
  let status = 'active'; const reads = [];
  codex.readThread = async (_id, { includeTurns }) => { reads.push(includeTurns); return { status: { type: status }, turns: [] }; };
  await host.recover(project);
  assert.deepEqual(reads, [false]); assert.equal(calls.length, 0);
  status = 'idle';
  await host.recover(project);
  assert.deepEqual(reads, [false, false, true]); assert.equal(calls.length, 1);
});

test('unknown active metadata never sends a continuation and stop checks bypass metadata polling', async t => {
  const { project, host, codex, calls } = await fixture(t);
  const reads = [];
  codex.readThread = async (_id, { includeTurns }) => { reads.push(includeTurns); return { status: { type: 'unknown' } }; };
  await host.recover(project);
  assert.equal(project.generation.status, 'recovering'); assert.equal(calls.length, 0);
  await host.cancel(project);
  assert.equal(reads.at(-1), true); assert.equal(calls.length, 0);
  assert.ok(project.generation.cancelRequestedAt);
});

async function stoppedFixture(t, options = {}) {
  const state = await fixture(t, options), { project, store } = state;
  project.activeTurnId = null;
  Object.assign(project.generation, { status: 'failed', timeBudgetMs: null, continuationCount: 24,
    completedAt: '2026-10-18T15:39:02.127Z', cancelRequestedAt: '2026-10-18T15:39:02.100Z',
    stopStatus: 'failed', terminalReason: '已达到 24 次自动续接预算。' });
  project.jobs.push({ id: 'old-failed-consumer', generationId: project.generation.id,
    status: 'failed', error: 'original consumer failure', result: { artifactRef: 'original-failure.json' } });
  await store.record(project.id, { type: 'generation_stopped', reason: project.generation.terminalReason });
  await store.save();
  return state;
}

test('explicit resume retains generation, prompt, failures and cumulative count while continuing the original thread once', async t => {
  const { project, host, calls, store } = await stoppedFixture(t);
  const before = structuredClone(project), generation = project.generation;
  const results = await Promise.all([host.resume(project, { generationId: generation.id }),
    host.resume(project, { generationId: generation.id })]);
  assert.equal(results[0], generation); assert.equal(results[1], generation);
  assert.equal(project.generation, generation);
  assert.equal(project.generation.id, before.generation.id);
  assert.equal(project.threadId, before.threadId);
  assert.equal(project.prompt, before.prompt);
  assert.deepEqual(project.requirements, before.requirements);
  assert.deepEqual(project.jobs, before.jobs);
  assert.deepEqual(project.messages, before.messages);
  for (const field of ['startedAt', 'timeBudgetMs', 'requirementVersion', 'requestHash', 'requiredOutputs']) {
    assert.deepEqual(generation[field], before.generation[field]);
  }
  assert.equal(generation.status, 'active');
  assert.equal(generation.continuationCount, 25);
  assert.equal(calls.length, 1);
  assert.ok(calls[0].input.includes(project.prompt));
  assert.match(calls[0].input, /original consumer failure/);
  assert.equal(project.activeTurnId, 'continued-1');
  for (const field of ['cancelRequestedAt', 'stopStatus', 'terminalReason', 'completedAt', 'recoveryIssue']) {
    assert.equal(generation[field], undefined);
  }
  const trace = await store.trace(project.id), resumes = trace.filter(event => event.type === 'generation_resumed');
  assert.equal(trace.filter(event => event.type === 'generation_stopped').length, 1);
  assert.equal(resumes.length, 1);
  assert.equal(resumes[0].generationId, generation.id);
  for (const field of ['status', 'completedAt', 'cancelRequestedAt', 'stopStatus', 'terminalReason', 'continuationCount']) {
    assert.deepEqual(resumes[0].previous[field], before.generation[field]);
  }
  assert.ok(trace.indexOf(resumes[0]) < trace.findIndex(event => event.type === 'continuation_planned'));
  assert.equal(trace.some(event => ['user_message', 'requirement_added'].includes(event.type)), false);
});

test('an unlimited generation continues past twenty-four turns without a hidden continuation cap', async t => {
  const { project, host, calls } = await fixture(t);
  project.generation.timeBudgetMs = null;
  project.generation.continuationCount = 24;
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  assert.equal(project.generation.status, 'active'); assert.equal(calls.length, 1);
  assert.equal(project.generation.continuationCount, 25);
  await writeFile(path.join(host.store.directory(project.id), 'expert.py'), '# actual change\n');
  await host.turnCompleted(project, { id: 'continued-1', status: 'completed' });
  assert.equal(project.generation.continuationCount, 26); assert.equal(calls.length, 2);
});

test('explicit resume rejects mismatched identity and unsafe states without changing the prior terminal', async t => {
  const guards = [
    ['wrong generation', state => {}, 'stale-generation'],
    ['missing generation', state => {}, undefined],
    ['already complete', state => { state.project.generation.status = 'complete'; }],
    ['draft', state => { state.project.generation.status = 'draft'; }],
    ['approval', state => { state.host.hasApprovals = () => true; }],
    ['opening input', state => { state.host.hasOpeningInput = () => true; }],
    ['unconfirmed continuation', state => { state.project.generation.pendingContinuation = { id: 'unconfirmed-send', dispatchAttemptedAt: '2026-10-18T15:39:00Z' }; }],
    ['project turn still present', state => { state.project.activeTurnId = 'not-confirmed-idle'; }],
    ['current remote job', state => { state.project.jobs.push({ id: 'running-current', generationId: state.project.generation.id, status: 'running' }); }],
    ['other generation remote job', state => { state.project.jobs.push({ id: 'running-old', generationId: 'other-generation', status: 'unknown' }); }],
    ['native active', state => { state.codex.readThread = async () => ({ status: { type: 'active' }, turns: [] }); }],
    ['native turn in progress', state => { state.codex.readThread = async () => ({ status: { type: 'idle' }, turns: [{ id: 'running-native', status: 'inProgress' }] }); }],
    ['native unknown', state => { state.codex.readThread = async () => ({ status: { type: 'unknown' }, turns: [] }); }],
    ['different native thread', state => { state.codex.readThread = async () => ({ id: 'different-thread', status: { type: 'idle' }, turns: [] }); }],
    ['native thread absent', state => { state.project.threadId = null; }],
  ];
  for (const [name, configure, requested] of guards) await t.test(name, async t => {
    const state = await stoppedFixture(t); configure(state);
    const before = structuredClone(state.project);
    const generationId = name === 'missing generation' ? undefined : requested || state.project.generation.id;
    await assert.rejects(() => state.host.resume(state.project, { generationId }), { status: 409 });
    assert.deepEqual(state.project, before);
    assert.equal(state.calls.length, 0);
  });
});

test('a native read failure leaves the original failed generation untouched', async t => {
  const state = await stoppedFixture(t);
  const before = structuredClone(state.project);
  state.codex.readThread = async () => { throw new Error('native connection unavailable'); };
  await assert.rejects(() => state.host.resume(state.project, { generationId: state.project.generation.id }), { status: 409 });
  assert.deepEqual(state.project, before);
  assert.equal(state.calls.length, 0);
});

test('resume attaches only a complete same-id unloaded thread, then rechecks native idle and existing guards', async t => {
  const cases = [
    ['idle after attach', {}],
    ['missing unloaded turns', { before: { id: 'original-thread', status: { type: 'notLoaded' } }, rejected: true, attaches: 0 }],
    ['running unloaded turn', { before: { id: 'original-thread', status: { type: 'notLoaded' }, turns: [{ id: 'running', status: 'inProgress' }] }, rejected: true, attaches: 0 }],
    ['unknown before attach', { before: { id: 'original-thread', status: { type: 'unknown' }, turns: [] }, rejected: true, attaches: 0 }],
    ['active before attach', { before: { id: 'original-thread', status: { type: 'active' }, turns: [] }, rejected: true, attaches: 0 }],
    ['wrong unloaded thread', { before: { id: 'wrong-thread', status: { type: 'notLoaded' }, turns: [] }, rejected: true, attaches: 0 }],
    ['still unloaded', { after: { id: 'original-thread', status: { type: 'notLoaded' }, turns: [] }, rejected: true }],
    ['active after attach', { after: { id: 'original-thread', status: { type: 'active' }, turns: [] }, rejected: true }],
    ['unknown after attach', { after: { id: 'original-thread', status: { type: 'unknown' }, turns: [] }, rejected: true }],
    ['wrong loaded thread', { after: { id: 'wrong-thread', status: { type: 'idle' }, turns: [] }, rejected: true }],
    ['missing loaded identity', { after: { status: { type: 'idle' }, turns: [] }, rejected: true }],
    ['missing loaded turns', { after: { id: 'original-thread', status: { type: 'idle' } }, rejected: true }],
    ['running after attach', { after: { id: 'original-thread', status: { type: 'idle' }, turns: [{ id: 'running', status: 'inProgress' }] }, rejected: true }],
    ['attach failure', { attachError: true, rejected: true }],
    ...['approval', 'job', 'generation', 'thread'].flatMap(change => ['attach', 'read'].map(at => [change+' during '+at, { change, at, rejected: true }])),
  ];
  for (const [name, options] of cases) await t.test(name, async t => {
    const state = await stoppedFixture(t), { project, host, codex, calls } = state;
    const before = structuredClone(project), generationId = project.generation.id;
    let attached = 0, reads = 0, approval = false, changed;
    host.hasApprovals = () => approval;
    const change = at => {
      if (options.at !== at) return;
      if (options.change === 'approval') approval = true;
      if (options.change === 'job') project.jobs.push({ id: 'new-job', status: 'running' });
      if (options.change === 'generation') project.generation = { ...project.generation, id: 'replacement' };
      if (options.change === 'thread') project.threadId = 'replacement-thread';
      changed = structuredClone(project);
    };
    codex.readThread = async () => {
      reads++;
      if (reads === 1) return options.before ?? { id: 'original-thread', status: { type: 'notLoaded' }, turns: [] };
      change('read');
      return options.after ?? { id: 'original-thread', status: { type: 'idle' }, turns: [] };
    };
    host.ensureThread = async (current, expectedThreadId) => {
      attached++;
      assert.equal(current, project); assert.equal(expectedThreadId, before.threadId);
      change('attach');
      if (options.attachError) throw new Error('native attach unavailable');
    };
    if (options.rejected) {
      await assert.rejects(() => host.resume(project, { generationId }), { status: 409 });
      assert.deepEqual(project, changed ?? before); assert.equal(calls.length, 0);
    } else {
      await host.resume(project, { generationId });
      assert.equal(project.generation.status, 'active'); assert.equal(calls.length, 1);
      assert.equal(project.threadId, before.threadId); assert.equal(project.generation.id, generationId);
    }
    assert.equal(attached, options.attaches ?? 1);
  });
});

test('resume rechecks approval state after waiting for native idle confirmation', async t => {
  const state = await stoppedFixture(t);
  let started, respond, pending = false;
  const reading = new Promise(resolve => { started = resolve; });
  const reply = new Promise(resolve => { respond = resolve; });
  state.host.hasApprovals = () => pending;
  state.codex.readThread = async () => { started(); return reply; };
  const before = structuredClone(state.project);
  const resuming = state.host.resume(state.project, { generationId: state.project.generation.id });
  await reading;
  pending = true;
  respond({ status: { type: 'idle' }, turns: [] });
  await assert.rejects(resuming, { status: 409 });
  assert.deepEqual(state.project, before); assert.equal(state.calls.length, 0);
});

test('resume rejects identity changes during its native preflight without altering the replacement state', async t => {
  for (const field of ['generation', 'thread']) await t.test(field, async t => {
    const state = await stoppedFixture(t);
    let started, respond;
    const reading = new Promise(resolve => { started = resolve; });
    const reply = new Promise(resolve => { respond = resolve; });
    state.codex.readThread = async () => { started(); return reply; };
    const generationId = state.project.generation.id;
    const resuming = state.host.resume(state.project, { generationId });
    await reading;
    if (field === 'generation') state.project.generation = { ...state.project.generation, id: 'new-generation' };
    else state.project.threadId = 'new-thread';
    const changed = structuredClone(state.project);
    respond({ status: { type: 'idle' }, turns: [] });
    await assert.rejects(resuming, { status: 409 });
    assert.deepEqual(state.project, changed); assert.equal(state.calls.length, 0);
  });
});

test('resume rechecks identity after its terminal audit is appended', async t => {
  const state = await stoppedFixture(t);
  let started, respond;
  const writing = new Promise(resolve => { started = resolve; });
  const reply = new Promise(resolve => { respond = resolve; });
  const record = state.store.record.bind(state.store);
  state.store.record = async (id, event) => {
    const row = await record(id, event);
    if (event.type === 'generation_resumed') { started(); await reply; }
    return row;
  };
  const resuming = state.host.resume(state.project, { generationId: state.project.generation.id });
  await writing;
  state.project.generation = { ...state.project.generation, id: 'replacement-generation' };
  const changed = structuredClone(state.project);
  respond();
  await assert.rejects(resuming, { status: 409 });
  assert.deepEqual(state.project, changed); assert.equal(state.calls.length, 0);
});

test('a cancelled generation resumes explicitly while finite budgets retain their original limits', async t => {
  const cancelled = await stoppedFixture(t);
  cancelled.project.generation.status = 'cancelled'; cancelled.project.generation.stopStatus = 'cancelled';
  await cancelled.host.resume(cancelled.project, { generationId: cancelled.project.generation.id });
  assert.equal(cancelled.project.generation.status, 'active'); assert.equal(cancelled.calls.length, 1);
  for (const [name, options, timeBudgetMs, count] of [
    ['finite with old continuation count', {}, 60 * 60_000, 24],
    ['missing with old continuation count', {}, undefined, 24],
    ['elapsed time', { now: () => Date.now() + 61 * 60_000 }, 60 * 60_000, 0],
  ]) await t.test(name, async t => {
    const state = await stoppedFixture(t, options);
    Object.assign(state.project.generation, { timeBudgetMs, continuationCount: count });
    const startedAt = state.project.generation.startedAt;
    await state.host.resume(state.project, { generationId: state.project.generation.id });
    const elapsed = name === 'elapsed time';
    assert.equal(state.project.generation.status, elapsed ? 'failed' : 'active');
    assert.equal(state.calls.length, elapsed ? 0 : 1);
    if (elapsed) assert.match(state.project.generation.terminalReason, /时间/);
    assert.equal(state.project.generation.startedAt, startedAt);
    assert.equal(state.project.generation.timeBudgetMs, timeBudgetMs);
    assert.equal(state.project.generation.continuationCount, count + (elapsed ? 0 : 1));
  });
});

const waitArgs = project => ({ projectId: project.id, generationId: project.generation.id, requestId: 'wait-1',
  reason: '消费者工具返回系统错误', evidence: ['job-1: actual TimeoutError'], jobIds: ['job-1'] });

test('reported wait latches during a live turn and survives late jobs, recovery and duplicate requests', async t => {
  const { project, host, calls, store, root, codex } = await fixture(t);
  const job = { id: 'job-1', generationId: project.generation.id, status: 'running' }; project.jobs.push(job);
  const args = waitArgs(project);
  await Promise.all([host.waitSystem(project, args), host.waitSystem(project, args)]);
  assert.equal(project.activeTurnId, 'initial-turn');
  assert.equal(project.generation.status, 'waiting_system');
  await host.turnCompleted(project, { id: 'initial-turn', status: 'completed' });
  job.status = 'completed'; await host.jobCompleted(project, job); await host.recover(project);
  assert.equal(calls.length, 0);
  assert.equal((await store.trace(project.id)).filter(row => row.type === 'generation_waiting_system').length, 1);
  const loaded = await new Store({ root }).load(), recovered = loaded.get(project.id);
  assert.equal(recovered.generation.status, 'waiting_system');
  const restarted = new GenerationHost({ store: loaded, codex, continueTurn: async () => calls.push({}) });
  await restarted.recover(recovered);
  assert.equal(recovered.generation.waitingSystem.requestId, 'wait-1'); assert.equal(calls.length, 0);
});

test('wait validates identity and job ownership and cancellation or accepted delivery wins', async t => {
  for (const change of [args => { args.generationId = 'old'; }, args => { args.projectId = 'other'; },
    args => { args.jobIds = ['unknown']; }, args => { args.evidence = []; }]) {
    const { project, host } = await fixture(t); project.jobs.push({ id: 'job-1', generationId: project.generation.id, status: 'completed' });
    const args = waitArgs(project); change(args); await assert.rejects(host.waitSystem(project, args));
    assert.equal(project.generation.waitingSystem, undefined);
  }
  const cancelled = await fixture(t); cancelled.project.jobs.push({ id:'job-1', generationId:cancelled.project.generation.id,status:'completed' });
  await cancelled.host.waitSystem(cancelled.project, waitArgs(cancelled.project)); await cancelled.host.cancel(cancelled.project);
  assert.equal(cancelled.project.generation.status, 'cancelled'); assert.equal(cancelled.calls.length, 0);
  const delivered = await fixture(t); delivered.project.jobs.push({ id:'job-1', generationId:delivered.project.generation.id,status:'completed' });
  await delivered.host.waitSystem(delivered.project, waitArgs(delivered.project));
  delivered.project.delivery={accepted:true,generationId:delivered.project.generation.id,requirementVersion:1,requestHash:delivered.project.generation.requestHash};
  await delivered.host.recover(delivered.project); assert.equal(delivered.project.generation.status,'complete');
});

test('explicit resume settles system wait once and stale wait retries cannot relatch it', async t => {
  const started = Date.now(); let current = started;
  const state = await fixture(t, { now: () => current }), { project, host, calls } = state;
  project.jobs.push({ id:'job-1', generationId:project.generation.id,status:'completed' });
  project.generation.startedAt = new Date(started).toISOString();
  current += 20*60_000; await host.waitSystem(project, waitArgs(project));
  await host.turnCompleted(project, {id:'initial-turn',status:'completed'});
  current += 80*60_000; await host.resume(project,{generationId:project.generation.id});
  assert.equal(project.generation.systemWaitMs,80*60_000); assert.equal(project.generation.startedAt,new Date(started).toISOString());
  assert.equal(project.generation.waitingSystem,undefined); assert.equal(calls.length,1);
  await host.waitSystem(project,waitArgs(project)); assert.equal(project.generation.waitingSystem,undefined); assert.equal(calls.length,1);
  assert.equal((await state.store.trace(project.id)).filter(row=>row.type==='generation_resumed')[0].previous.waitingSystem.requestId,'wait-1');
});

test('overlapping approval and system waits do not subtract the same time twice', async t => {
  const started=Date.now(); let current=started,pending=false;
  const {project,host}=await fixture(t,{now:()=>current,hasApprovals:()=>pending});
  project.jobs.push({id:'job-1',generationId:project.generation.id,status:'completed'});
  project.generation.startedAt=new Date(started).toISOString();
  current+=10*60_000; pending=true; await host.approvalsChanged(project);
  current+=10*60_000; await host.waitSystem(project,waitArgs(project));
  current+=20*60_000; await host.approvalsChanged(project);
  current+=20*60_000; pending=false; await host.approvalsChanged(project);
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  await host.resume(project,{generationId:project.generation.id});
  assert.equal(project.generation.approvalWaitMs,10*60_000); assert.equal(project.generation.systemWaitMs,40*60_000);
});

test('fresh source, requirements and real job changes each permit another unlimited continuation', async t => {
  const {project,host,store,calls}=await fixture(t); project.generation.timeBudgetMs=null;
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  for(let n=1;n<=28;n++) {
    if(n%3===0) await store.addRequirement(project.id,'persistent requirement '+n);
    else if(n%3===1) await writeFile(path.join(store.directory(project.id),'expert.py'),'# source '+n+'\n');
    else project.jobs.push({id:'real-'+n,generationId:project.generation.id,status:'completed'});
    await host.turnCompleted(project,{id:'continued-'+n,status:'completed'});
  }
  assert.equal(calls.length,29); assert.equal(project.generation.status,'active');
});

test('snapshot failure sends its actual error and unknown current hash to the same producer', async t => {
  const {project,host,store,calls}=await fixture(t);
  project.sourceHash='previous-known-hash';
  store.sourceSnapshot=async()=>{throw new Error('source cannot be read');};
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(project.generation.waitingSystem,undefined);assert.equal(calls.length,1);
  assert.equal(project.generation.pendingContinuation.progress.sourceHash,null);
  assert.equal(project.generation.pendingContinuation.progress.sourceUnavailable,'source cannot be read');
  assert.match(calls[0].input,/source cannot be read/);assert.match(calls[0].input,/"sourceHash":null/);
  assert.ok(calls[0].input.indexOf('source cannot be read')<calls[0].input.indexOf('完整持久要求'));
  assert.equal(project.sourceHash,'previous-known-hash');
  for(let n=0;n<4;n++) await host.recover(project);
  assert.equal(calls.length,1);assert.equal(project.generation.waitingSystem,undefined);
});

test('a project source symlink can be diagnosed and repaired without entering system wait', async t => {
  const {project,host,store,calls}=await fixture(t),directory=store.directory(project.id);
  const before=await store.sourceSnapshot(project.id),link=path.join(directory,'helper.py');
  await symlink('scene.py',link);
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(calls.length,1);assert.equal(project.generation.waitingSystem,undefined);
  assert.equal(project.generation.pendingContinuation.progress.sourceHash,null);
  assert.match(calls[0].input,/工程源文件不能通过软链接引用未锁定内容/);
  await rm(link);
  await host.turnCompleted(project,{id:'continued-1',status:'completed'});
  assert.equal(calls.length,2);assert.equal(project.generation.pendingContinuation.progress.sourceHash,before.sourceHash);
  assert.equal(project.generation.pendingContinuation.progress.sourceUnavailable,undefined);
  assert.equal(project.generation.waitingSystem,undefined);
});

test('source failure still rechecks child work, job admission and generation identity before dispatch', async t => {
  for(const change of ['child','job','generation','approval','wait','cancel'])await t.test(change,async t=>{
    const {project,host,store,calls}=await fixture(t);let ready=true,approval=false;const waits=[];
    host.readNativeWork=async()=>({ready,completed:[],reason:'child still active'});
    host.hasApprovals=()=>approval;
    store.sourceSnapshot=async()=>{
      if(change==='child')ready=false;
      if(change==='job')project.jobs.push({id:'new-job',generationId:project.generation.id,status:'running'});
      if(change==='generation')project.generation={...project.generation,id:'replacement',status:'draft'};
      if(change==='approval')approval=true;
      if(change==='wait')waits.push(host.waitSystem(project,{projectId:project.id,generationId:project.generation.id,requestId:'reported-during-read',reason:'actual external blocker',evidence:['actual error']}));
      if(change==='cancel')waits.push(host.cancel(project));
      throw new Error('source read failed after boundary change');
    };
    await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
    await Promise.all(waits);
    assert.equal(calls.length,0);
    assert.equal(project.generation.waitingSystem?.kind,change==='wait'?'reported':undefined);
    if(change==='generation')assert.equal(project.generation.status,'draft');
    if(change==='cancel')assert.equal(project.generation.status,'cancelled');
  });
});

test('the latest timeout stays before truncated history and does not retry or end the generation', async t => {
  const {project,host,calls,codex}=await fixture(t);
  project.jobs.push({id:'old-large-result',generationId:project.generation.id,status:'completed',result:{padding:'x'.repeat(12000)}});
  const failed={id:'latest-timeout',generationId:project.generation.id,status:'failed',workerTimeoutSeconds:1800,
    stage:'required_files',error:'TimeoutError: workerTimeoutSeconds=1800; last worker phase: required_files'};
  project.jobs.push(failed);
  const jobs=structuredClone(project.jobs),generationId=project.generation.id;
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(calls.length,1);assert.equal(project.generation.id,generationId);
  assert.equal(project.generation.status,'active');assert.equal(project.generation.waitingSystem,undefined);
  assert.match(calls[0].input,/"workerTimeoutSeconds":1800/);assert.match(calls[0].input,/required_files/);
  assert.ok(calls[0].input.indexOf('latest-timeout')<calls[0].input.indexOf('完整持久要求'));
  assert.match(calls[0].input,/摘要已截断/);assert.deepEqual(project.jobs,jobs);
  codex.readThread=async()=>({status:{type:'active'},turns:[{id:'continued-1',status:'inProgress'}]});
  await host.jobCompleted(project,failed);await host.jobCompleted(project,failed);await host.recover(project);
  assert.equal(calls.length,1);assert.deepEqual(project.jobs,jobs);
});

test('active or unknown native child work defers no-progress and a new completion counts as progress', async t => {
  const {project,host,calls}=await fixture(t); let ready=true,turn='child-turn-1';
  host.readNativeWork=async()=>({ready,completed:[['review',turn,'completed']],reason:'child active or unknown'});
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'}); assert.equal(calls.length,1);
  ready=false; await host.turnCompleted(project,{id:'continued-1',status:'completed'});
  assert.equal(project.generation.waitingSystem,undefined); assert.equal(calls.length,1);
  await host.recover(project); assert.equal(calls.length,1);
  ready=true;turn='child-turn-2';await host.recover(project);
  assert.equal(project.generation.status,'active');assert.equal(calls.length,2);
});

test('a wait arriving during continuation dispatch keeps the actual send identity without a second dispatch', async t => {
  let started,reply; const sending=new Promise(r=>started=r),response=new Promise(r=>reply=r);
  const {project,host,calls}=await fixture(t,{continueTurn:async()=>{started();return response;}});
  project.jobs.push({id:'job-1',generationId:project.generation.id,status:'completed'});
  const running=host.turnCompleted(project,{id:'initial-turn',status:'completed'});await sending;
  const waiting=host.waitSystem(project,waitArgs(project));
  assert.equal(project.generation.waitingSystem.requestId,'wait-1');
  reply({id:'sent-before-wait',status:'inProgress'});await Promise.all([running,waiting]);
  assert.equal(project.generation.pendingContinuation.turnId,'sent-before-wait');assert.equal(project.generation.status,'waiting_system');
  assert.equal(calls.length,0);
});

test('recovery of an already completed continuation sends its no-progress diagnostic once', async t => {
  const {project,host,codex,calls}=await fixture(t);
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  project.activeTurnId=null;
  codex.readThread=async()=>({status:{type:'idle'},turns:[{id:'continued-1',status:'completed',items:[]}]});
  await host.recover(project);assert.equal(project.generation.waitingSystem,undefined);assert.equal(calls.length,2);
  assert.match(calls[1].input,/均未变化/);
  codex.readThread=async()=>({status:{type:'active'},turns:[{id:'continued-2',status:'inProgress'}]});
  await host.recover(project);assert.equal(calls.length,2);
});

test('all prior wait IDs stay idempotent across multiple resumes and changed payload is rejected', async t => {
  const {project,host}=await fixture(t);project.activeTurnId=null;
  const args={projectId:project.id,generationId:project.generation.id,requestId:'first',reason:'first failure',evidence:['HTTP 500']};
  await host.waitSystem(project,args);await host.resume(project,{generationId:project.generation.id});
  await host.turnCompleted(project,{id:'continued-1',status:'completed'});
  assert.equal(project.generation.waitingSystem,undefined);
  project.activeTurnId=null;project.generation.pendingContinuation=null;
  await host.waitSystem(project,{...args,requestId:'second',reason:'second failure'});
  await host.resume(project,{generationId:project.generation.id});
  await host.waitSystem(project,args);assert.equal(project.generation.waitingSystem,undefined);
  await assert.rejects(host.waitSystem(project,{...args,reason:'different input'}),{status:409});
});

test('async progress checks cannot override a replacement generation or accepted delivery', async t => {
  for(const action of ['replace','accept'])await t.test(action,async t=>{
    const {project,host,store,calls}=await fixture(t);let started,reply;
    const reading=new Promise(r=>started=r),response=new Promise(r=>reply=r);
    const snapshot=store.sourceSnapshot.bind(store);store.sourceSnapshot=async(...args)=>{const value=await snapshot(...args);started();await response;return value;};
    const running=host.turnCompleted(project,{id:'initial-turn',status:'completed'});await reading;
    if(action==='replace') project.generation={...project.generation,id:'replacement',status:'draft'};
    else project.delivery={accepted:true,generationId:project.generation.id,requirementVersion:project.generation.requirementVersion,requestHash:project.generation.requestHash};
    reply();await running;assert.equal(calls.length,0);assert.equal(project.generation.status,action==='replace'?'draft':'complete');
  });
});

test('a child starting during a source read defers automatic dispatch', async t => {
  const {project,host,store,calls}=await fixture(t);let ready=true;
  host.readNativeWork=async()=>({ready,completed:[],reason:'new child work active'});
  const snapshot=store.sourceSnapshot.bind(store);store.sourceSnapshot=async(...args)=>{const value=await snapshot(...args);ready=false;return value;};
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(calls.length,0);assert.equal(project.generation.waitingSystem,undefined);
});

test('a job admitted during a progress snapshot blocks another root dispatch', async t => {
  const {project,host,store,calls}=await fixture(t);const snapshot=store.sourceSnapshot.bind(store);
  store.sourceSnapshot=async(...args)=>{const value=await snapshot(...args);project.jobs.push({id:'new-live-job',generationId:project.generation.id,status:'running'});return value;};
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(calls.length,0);assert.equal(project.generation.status,'waiting_jobs');
});

test('missed automatic completion clears its stale active pointer before progress checks', async t => {
  const {project,host,codex,calls}=await fixture(t);
  await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
  assert.equal(project.activeTurnId,'continued-1');
  codex.readThread=async()=>({status:{type:'idle'},turns:[{id:'continued-1',status:'completed',items:[]}]});
  await host.recover(project);assert.equal(project.activeTurnId,'continued-2');assert.equal(project.generation.waitingSystem,undefined);assert.equal(calls.length,2);
  assert.match(calls[1].input,/均未变化/);
});

test('wait before the actual send withholds a persisted dispatch intent without inventing uncertainty', async t => {
  const {project,host,store,calls}=await fixture(t);let started,reply;
  const writing=new Promise(r=>started=r),response=new Promise(r=>reply=r),save=store.save.bind(store);let blocked=false;
  store.save=async()=>{if(!blocked&&project.generation.pendingContinuation?.dispatchAttemptedAt){blocked=true;started();await response;}return save();};
  const running=host.turnCompleted(project,{id:'initial-turn',status:'completed'});await writing;
  const waiting=host.waitSystem(project,{projectId:project.id,generationId:project.generation.id,requestId:'before-send',reason:'new fault',evidence:['actual error']});
  reply();await Promise.all([running,waiting]);assert.equal(calls.length,0);assert.equal(project.generation.pendingContinuation,null);
  await host.resume(project,{generationId:project.generation.id});assert.equal(calls.length,1);
});

test('wait persistence rejects an in-place generation ID change at the audit boundary', async t => {
  const {project,host,store}=await fixture(t);const record=store.record.bind(store);
  store.record=async(id,event)=>{const row=await record(id,event);if(event.type==='generation_waiting_system')project.generation.id='changed-in-place';return row;};
  await assert.rejects(host.waitSystem(project,{projectId:project.id,generationId:project.generation.id,requestId:'bound',reason:'fault',evidence:['actual error']}),{status:409});
});

test('only an explicit unsent boundary clears an attempted continuation, never HTTP status alone', async t => {
  for(const unsent of [false,true])await t.test(String(unsent),async t=>{
    const {project,host}=await fixture(t,{continueTurn:async()=>{const error=Object.assign(new Error('same status different boundary'),{status:409});if(unsent)error.nativeInputNotSent=true;throw error;}});
    await host.turnCompleted(project,{id:'initial-turn',status:'completed'});
    assert.equal(project.generation.status,'recovering');
    assert.equal(!!project.generation.pendingContinuation.dispatchAttemptedAt,!unsent);
    assert.equal((await host.store.trace(project.id)).at(-1).type,unsent?'continuation_not_sent':'continuation_uncertain');
  });
});

test('an old dispatch rejection cannot alter a replacement pending continuation', async t => {
  let started,reject;const sending=new Promise(r=>started=r),response=new Promise((_,r)=>reject=r);
  const {project,host}=await fixture(t,{continueTurn:async()=>{started();return response;}});
  const running=host.turnCompleted(project,{id:'initial-turn',status:'completed'});await sending;
  project.generation.pendingContinuation={id:'replacement',status:'pending',dispatchAttemptedAt:'new-intent-is-not-the-old-send'};
  const replacement=structuredClone(project.generation);
  reject(Object.assign(new Error('old preflight response'),{nativeInputNotSent:true}));await running;
  assert.deepEqual(project.generation,replacement);
});

test('an accepted environment receipt cannot finish a new robot-only generation with a different output scope', async t => {
  const f = await fixture(t);
  const { project, host } = f;
  project.generation.requiredOutputs = ['demonstrations'];
  project.generation.environmentRefs = [{ revision: 'existing_layout' }];
  project.delivery = { accepted: true, generationId: project.generation.id,
    requirementVersion: project.generation.requirementVersion, requestHash: project.generation.requestHash,
    requiredOutputs: ['generation', 'evaluation'], environmentRefs: [{ revision: 'existing_layout' }] };
  await host.turnCompleted(project, { id: 'wrong-output-receipt', status: 'completed' });
  assert.notEqual(project.generation.status, 'complete');
});

test('automatic recovery reattaches the same terminal unloaded root before continuing once', async t => {
  const { project, host, codex, calls } = await fixture(t);
  project.activeTurnId = null;
  project.generation.status = 'recovering';
  project.generation.completedTurnIds = ['initial-turn'];
  project.generation.pendingContinuation = { id: 'old-wake', turnId: 'initial-turn', status: 'completed' };
  let attached = 0;
  codex.readThread = async () => ({ id: project.threadId, status: { type: attached ? 'idle' : 'notLoaded' },
    turns: [{ id: 'initial-turn', status: 'failed', items: [] }] });
  host.ensureThread = async (current, expectedId) => { assert.equal(current, project); assert.equal(expectedId, 'original-thread'); attached++; };
  host.readNativeWork = async (current, native) => ({ ready: native.status.type === 'idle', completed: [], reason: 'root not idle' });
  const original = { id: project.generation.id, requestHash: project.generation.requestHash, prompt: project.prompt };
  await host.recover(project);
  assert.equal(attached, 1);
  assert.equal(calls.length, 1);
  assert.equal(project.generation.status, 'active');
  assert.equal(project.generation.id, original.id);
  assert.equal(project.generation.requestHash, original.requestHash);
  assert.equal(project.prompt, original.prompt);
  assert.equal(project.generation.timeBudgetMs, null);
  assert.deepEqual(project.generation.completedTurnIds, ['initial-turn']);
  await Promise.all([host.recover(project), host.recover(project)]);
  assert.equal(attached, 1);
  assert.equal(calls.length, 1);
});

test('automatic unloaded-root recovery preserves incomplete sources and asynchronous guards', async t => {
  const terminal = { id: 'original-thread', status: { type: 'notLoaded' }, turns: [{ id: 'initial-turn', status: 'failed', items: [] }] };
  const idle = { ...terminal, status: { type: 'idle' } };
  const cases = [
    ['no complete headers', { before: { id: 'original-thread', status: { type: 'notLoaded' } }, attaches: 0 }],
    ['initial root identity missing', { before: { status: { type: 'notLoaded' }, turns: terminal.turns }, attaches: 0 }],
    ['wrong initial root', { before: { ...terminal, id: 'different-thread' }, attaches: 0 }],
    ['known root missing', { before: { ...terminal, turns: [] }, attaches: 0 }],
    ['running root', { before: { ...terminal, turns: [{ id: 'initial-turn', status: 'inProgress' }] }, attaches: 0 }],
    ['unknown root status', { before: { ...terminal, turns: [{ id: 'initial-turn', status: 'unknown' }] }, attaches: 0 }],
    ['attach failure', { attachError: true }],
    ['still unloaded', { after: terminal }],
    ['wrong attached root', { after: { ...idle, id: 'different-thread' } }],
    ['attached root identity missing', { after: { status: { type: 'idle' }, turns: terminal.turns } }],
    ['attached root active', { after: { ...idle, status: { type: 'active' } } }],
    ['attached root unknown', { after: { ...idle, status: { type: 'unknown' } } }],
    ['attached header missing', { after: { ...idle, turns: [] } }],
    ['attached turn running', { after: { ...idle, turns: [{ id: 'initial-turn', status: 'inProgress' }] } }],
    ['read failure', { readError: true }],
    ['child not ready', { childUnknown: true }],
    ...['generation', 'id', 'thread', 'hash', 'version', 'approval', 'opening', 'job', 'cancel', 'waiting']
      .flatMap(change => [...['attach', 'read'].map(at => [change + ' during ' + at, { change, at }]),
        [change + ' during read failure', { change, at: 'read', readError: true }]])
  ];
  for (const [name, options] of cases) await t.test(name, async t => {
    const { project, host, codex, calls } = await fixture(t);
    project.activeTurnId = null; project.generation.status = 'recovering';
    project.generation.completedTurnIds = ['initial-turn'];
    project.generation.pendingContinuation = { id: 'old-wake', turnId: 'initial-turn', status: 'completed' };
    let attached = 0, reads = 0, approval = false, opening = false;
    host.hasApprovals = () => approval; host.hasOpeningInput = () => opening;
    const mutate = at => {
      if (options.at !== at) return;
      if (options.change === 'generation') project.generation = { ...project.generation, id: 'replacement' };
      if (options.change === 'id') project.generation.id = 'replacement';
      if (options.change === 'thread') project.threadId = 'replacement-thread';
      if (options.change === 'hash') project.generation.requestHash = 'replacement-hash';
      if (options.change === 'version') project.generation.requirementVersion++;
      if (options.change === 'approval') approval = true;
      if (options.change === 'opening') opening = true;
      if (options.change === 'job') project.jobs.push({ id: 'new-job', generationId: project.generation.id, status: 'running' });
      if (options.change === 'cancel') project.generation.cancelRequestedAt = new Date().toISOString();
      if (options.change === 'waiting') project.generation.waitingSystem = { requestId: 'wait-now', reason: 'fixture block', since: new Date().toISOString() };
    };
    codex.readThread = async () => {
      if (++reads === 1) return options.before ?? terminal;
      mutate('read');
      if (options.readError) throw new Error('fixture native read failed');
      return options.after ?? idle;
    };
    host.ensureThread = async () => { attached++; mutate('attach'); if (options.attachError) throw new Error('fixture attach failed'); };
    host.readNativeWork = async (current, native) => ({ ready: !options.childUnknown && native.id === 'original-thread'
      && native.status.type === 'idle' && native.turns?.some(turn => turn.id === 'initial-turn' && turn.status === 'failed'),
      completed: [], reason: 'fixture source not ready' });
    await host.recover(project);
    assert.equal(calls.length, 0);
    assert.equal(attached, options.attaches ?? 1);
    if (options.change === 'hash') assert.equal(project.generation.requestHash, 'replacement-hash');
    if (options.change === 'version') assert.equal(project.generation.requirementVersion, 2);
  });
});
