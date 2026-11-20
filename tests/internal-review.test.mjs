import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createInternalReviewReader } from '../src/server.mjs';
import { fixture, digest } from './fixtures/review-source.mjs';

test('minimal current native pass needs no root history or review-work evidence', async t => {
  const f = await fixture(t);
  const result = await f.reader(f.project, f.pointer, { job: f.job });
  assert.equal(result.decision, 'pass');
  assert.deepEqual(result.episodes, [{ episodeId: 'ep', taskId: 'task', use: 'successful_demonstration', revision: 'rev' }]);
  assert.deepEqual(f.calls.map(row => [row.method, row.id]), [['read', 'review'], ['turns', 'review'], ['items', 'review'], ['read', 'review'], ['turns', 'review']]);
  assert.equal(f.calls[1].options.limit, 1); assert.equal(f.calls[1].options.sortDirection, 'desc');
  assert.equal(f.calls[2].options.limit, 1); assert.equal(f.calls[2].options.sortDirection, 'desc');
});

test('producer receipt binds the frozen export manifest without inspecting reviewer work', async t => {
  const f = await fixture(t);
  f.project.generation.deliveryContract = 'producer-exports-v1';
  f.job.producedArtifacts = { manifestRef: `projects/${f.project.id}/delivery/run/manifest.json`, sha256: 'd'.repeat(64) };
  f.report.producedArtifacts = { ...f.job.producedArtifacts }; f.final();
  const first = await f.reader(f.project, f.pointer, { job: f.job });
  assert.deepEqual(first.producedArtifacts, f.job.producedArtifacts);
  f.job.producedArtifacts.sha256 = 'e'.repeat(64);
  f.report.producedArtifacts = { ...f.job.producedArtifacts }; f.final();
  await assert.rejects(f.reader(f.project, f.pointer, { job: f.job, checkpoint: first.checkpoint, phase: 'terminal' }), /绑定|清单/);
});

for (const missing of [true, false]) test('producer receipt rejects ' + (missing ? 'missing' : 'stale') + ' manifest binding', async t => {
  const f = await fixture(t);
  f.project.generation.deliveryContract = 'producer-exports-v1';
  f.job.producedArtifacts = { manifestRef: `projects/${f.project.id}/delivery/run/manifest.json`, sha256: 'd'.repeat(64) };
  if (!missing) f.report.producedArtifacts = { ...f.job.producedArtifacts, sha256: 'e'.repeat(64) };
  f.final();
  await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /清单|producedArtifacts/);
});

test('completed native receipt ignores truncated tool history without completion notifications', async t => {
  const f = await fixture(t);
  f.childItems.unshift({ id: 'truncated_image', type: 'mcpToolCall', result: { content: [
    { type: 'text', text: '{"content":[…999999 chars truncated…]}' }], structuredContent: null } });
  f.childTurns.unshift({ id: 'old_unread_turn', status: 'completed', rootTurnId: 'old_root' });
  f.histories.set('review/old_unread_turn', [{ type: 'fileChange', status: 'completed', id: 'old_write' }]);
  assert.equal((await f.reader(f.project, f.pointer, { job: f.job })).decision, 'pass');
  assert.equal(f.calls.filter(row => row.method === 'items').length, 1);
  assert.equal(f.calls.find(row => row.method === 'items').options.turnId, 'review_turn');
});

test('detailed v2 review-work fields remain opaque and do not impose host quality rules', async t => {
  const f = await fixture(t);
  Object.assign(f.report, { requirements: 'reviewer work', issues: [{ id: 'historical' }], resolvedIssues: null });
  Object.assign(f.report.coverage, { nodeIds: [], views: 'reviewer work', rubrics: [], evidenceItemIds: [] }); f.final();
  const result = await f.reader(f.project, f.pointer, { job: f.job });
  const snapshot = JSON.parse(await readFile(path.join(f.store.directory(f.project.id), result.snapshotRef)));
  assert.deepEqual(snapshot.native.report, f.report);
});

for (const state of ['idle', 'notLoaded']) for (const phase of [null, 'final_answer', undefined]) {
  test('completed receipt accepts native state/phase ' + state + '/' + phase, async t => {
    const f = await fixture(t); f.metadata.get('review').status.type = state; f.childItems[0].phase = phase;
    assert.equal((await f.reader(f.project, f.pointer, { job: f.job })).decision, 'pass');
  });
}

test('prepare, prepared and terminal freshly read the same small receipt and publish no media', async t => {
  const f = await fixture(t); let checkpoint;
  for (const phase of ['prepare', 'prepared', 'terminal']) {
    const result = await f.reader(f.project, f.pointer, { job: f.job, checkpoint, phase }); checkpoint = result.checkpoint;
    const bytes = await readFile(path.join(f.store.directory(f.project.id), result.snapshotRef));
    assert.equal(digest(bytes), result.snapshotHash);
    const snapshot = JSON.parse(bytes);
    assert.equal(snapshot.phase, phase); assert.ok(bytes.length < 5000);
    for (const key of ['roots', 'children', 'rootHeaders', 'previousSource', 'inputOccurrences']) assert.equal(Object.hasOwn(snapshot, key), false);
  }
  assert.equal(f.calls.length, 15); assert.equal(f.project.jobs.length, 1);
  assert.equal(f.publications.length, 2); assert.ok(f.publications.every(row => row[1].startsWith('.trace/inputs/')));
});

const invalid = {
  'root self review': f => { f.pointer.threadId = 'root'; },
  'missing pointer item': f => { delete f.pointer.itemId; },
  'wrong pointer turn': f => { f.pointer.turnId = 'other'; },
  'wrong pointer item': f => { f.pointer.itemId = 'other'; },
  'wrong metadata identity': f => { f.metadata.get('review').id = 'other'; },
  'wrong metadata parent': f => { f.metadata.get('review').parentThreadId = 'other'; },
  'missing native spawn': f => { delete f.metadata.get('review').source; },
  'wrong native parent': f => { f.metadata.get('review').source.subAgent.thread_spawn.parent_thread_id = 'other'; },
  'wrong native role': f => { f.metadata.get('review').source.subAgent.thread_spawn.agent_path = '/root/author'; },
  'active child': f => { f.metadata.get('review').status.type = 'active'; },
  'system error child': f => { f.metadata.get('review').status.type = 'systemError'; },
  'unknown child status': f => { delete f.metadata.get('review').status; },
  'failed latest turn': f => { f.childTurns[0].status = 'failed'; },
  'interrupted latest turn': f => { f.childTurns[0].status = 'interrupted'; },
  'running latest turn': f => { f.childTurns[0].status = 'inProgress'; },
  'no latest turn': f => { f.childTurns.length = 0; },
  'ordinary commentary': f => { f.childItems[0].phase = 'commentary'; },
  'nonmessage final item': f => { f.childItems[0].type = 'reasoning'; },
  'truncated final JSON': f => { f.childItems[0].text = '{"schema":'; },
  'old schema': f => { f.report.schema = 'sceneagent-internal-review-v1'; f.final(); },
  'needs changes': f => { f.report.decision = 'needs_changes'; f.final(); },
  'missing episode selection': f => { delete f.report.coverage.episodes; f.final(); },
  'empty episode selection': f => { f.report.coverage.episodes = []; f.final(); },
  'duplicate episode selection': f => { f.report.coverage.episodes.push({ episodeId: 'ep', use: 'execution_data' }); f.final(); },
  'legacy episode list': f => { f.report.coverage.episodeIds = ['ep']; f.final(); },
  'unknown use': f => { f.report.coverage.episodes[0].use = 'success'; f.final(); },
  'extra selection field': f => { f.report.coverage.episodes[0].success = true; f.final(); },
  'unknown episode': f => { f.report.coverage.episodes[0].episodeId = 'other'; f.final(); },
  'foreign generation rollout': f => { f.run.generationId = 'other'; },
  'foreign rollout project': f => { f.run.result.projectId = 'other'; },
  'foreign rollout revision': f => { f.run.revision = 'other'; },
  'foreign rollout result revision': f => { f.run.result.revision = 'other'; },
  'unfinished rollout': f => { f.run.status = 'running'; },
  'nonrollout job': f => { f.run.kind = 'evaluate'; },
  'foreign published episode project': f => { f.run.result.episodes[0].projectId = 'other'; },
  'foreign published episode revision': f => { f.run.result.episodes[0].revision = 'other'; },
  'foreign published episode task': f => { f.run.result.episodes[0].taskId = 'other'; },
  'unregistered episode': f => { f.project.episodes = []; },
  'conflicting registered task': f => { f.project.episodes[0].taskId = 'other'; },
  'duplicate publication identity': f => { f.project.jobs.push(structuredClone(f.run)); },
  'missing required task data': f => { f.job.requiredTaskIds.push('other'); },
};
for (const [name, mutate] of Object.entries(invalid)) test('native final receipt rejects ' + name, async t => {
  const f = await fixture(t); mutate(f); await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /内部审核/);
});

for (const key of ['projectId', 'generationId', 'requirementVersion', 'requestHash', 'sourceHash', 'revision']) {
  test('native final receipt requires current binding ' + key, async t => {
    const f = await fixture(t); f.report[key] = key === 'requirementVersion' ? 2 : 'other'; f.final();
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /绑定过期/);
  });
}

for (const phase of ['prepared', 'terminal']) for (const state of ['failed', 'interrupted', 'inProgress', 'completed']) {
  test('old pass cannot hide newer selected-reviewer turn ' + state + ' at ' + phase, async t => {
    const f = await fixture(t), first = await f.reader(f.project, f.pointer, { job: f.job });
    f.childTurns.push({ id: 'newer', status: state });
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job, checkpoint: first.checkpoint, phase }), /最新已完成/);
  });
}

for (const phase of ['prepare', 'prepared', 'terminal']) for (const change of ['source file', 'generation', 'requirement', 'request']) {
  test('binding drift during native read rejects ' + change + ' at ' + phase, async t => {
    const f = await fixture(t), first = phase === 'prepare' ? null : await f.reader(f.project, f.pointer, { job: f.job });
    const original = f.codex.listItems;
    f.codex.listItems = async (...args) => {
      const result = await original(...args);
      if (change === 'source file') await writeFile(path.join(f.store.directory(f.project.id), 'scene.py'), 'changed source');
      if (change === 'generation') f.project.generation.id = 'other';
      if (change === 'requirement') f.project.generation.requirementVersion++;
      if (change === 'request') f.project.generation.requestHash = 'other';
      return result;
    };
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job, checkpoint: first?.checkpoint, phase }), /源码或需求/);
  });
}

for (const method of ['readThread', 'listTurns', 'listItems']) for (const phase of ['prepare', 'prepared', 'terminal']) {
  test('native ' + method + ' failure has no checkpoint fallback at ' + phase, async t => {
    const f = await fixture(t), first = phase === 'prepare' ? null : await f.reader(f.project, f.pointer, { job: f.job });
    f.codex[method] = async () => { throw Error('native read unavailable'); };
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job, checkpoint: first?.checkpoint, phase }), /native read unavailable/);
  });
}

for (const method of ['listTurns', 'listItems']) for (const malformed of ['missing cursor', 'invalid cursor', 'duplicate row', 'wrong turn']) {
  test('minimal native response validates ' + method + '/' + malformed, async t => {
    const f = await fixture(t), original = f.codex[method];
    f.codex[method] = async (...args) => {
      const page = await original(...args);
      if (malformed === 'missing cursor') delete page.nextCursor;
      if (malformed === 'invalid cursor') page.nextCursor = {};
      if (malformed === 'duplicate row') page.data.push(page.data[0]);
      if (malformed === 'wrong turn') {
        if (method === 'listTurns') page.data[0] = { ...page.data[0], id: 'other' };
        else page.data[0].turnId = 'other';
      }
      return page;
    };
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /内部审核/);
  });
}

test('execution_data use preserves the selected actual outcome for downstream consumers', async t => {
  const f = await fixture(t); f.report.coverage.episodes[0].use = 'execution_data'; f.final();
  f.run.result.episodes[0].score = { result: { success: null }, executionStatus: 'truncated' };
  const result = await f.reader(f.project, f.pointer, { job: f.job });
  assert.equal(result.episodes[0].use, 'execution_data');
  assert.equal(f.run.result.episodes[0].score.result.success, null);
});

test('late same-version episodes cannot enter the fixed selection', async t => {
  const f = await fixture(t), first = await f.reader(f.project, f.pointer, { job: f.job });
  const episode = { ...f.project.episodes[0], episode: 'later' };
  f.project.episodes.push(episode); f.run.result.episodes.push(structuredClone(episode));
  const result = await f.reader(f.project, f.pointer, { job: f.job, checkpoint: first.checkpoint, phase: 'terminal' });
  assert.deepEqual(result.episodes.map(row => row.episodeId), ['ep']);
});

for (const change of ['path', 'hash', 'bytes', 'legacy process snapshot', 'binding', 'pointer', 'selection', 'final message']) {
  test('checkpoint rejects ' + change, async t => {
    const f = await fixture(t), first = await f.reader(f.project, f.pointer, { job: f.job });
    let checkpoint = first.checkpoint;
    const file = path.join(f.store.directory(f.project.id), checkpoint.snapshotRef);
    if (change === 'path') checkpoint = { ...checkpoint, snapshotRef: '../outside.json' };
    else if (change === 'hash') checkpoint = { ...checkpoint, snapshotHash: '0'.repeat(64) };
    else if (change === 'bytes') await writeFile(file, '{}');
    else if (change === 'selection') { f.report.coverage.episodes[0].use = 'execution_data'; f.final(); }
    else if (change === 'final message') { f.report.note = 'changed immutable native item'; f.final(); }
    else {
      const snapshot = JSON.parse(await readFile(file));
      if (change === 'legacy process snapshot') { snapshot.normalization = 'native paginated records; image content addressed; not raw unchanged messages'; snapshot.roots = {}; }
      if (change === 'binding') snapshot.binding.generationId = 'other';
      if (change === 'pointer') snapshot.pointer.itemId = 'other';
      const text = JSON.stringify(snapshot), hash = digest(text), ref = `.trace/inputs/${hash}.json`;
      await writeFile(path.join(f.store.directory(f.project.id), ref), text); checkpoint = { snapshotRef: ref, snapshotHash: hash };
    }
    await assert.rejects(f.reader(f.project, f.pointer, { job: f.job, checkpoint, phase: 'terminal' }), /快照|绑定|指针|固定交付|最终消息/);
  });
}

test('publishing a prepared receipt must succeed before it can return', async t => {
  const f = await fixture(t);
  const reader = createInternalReviewReader({ store: f.store, codex: f.codex, publishSnapshot: async () => { throw Error('publication unavailable'); } });
  await assert.rejects(reader(f.project, f.pointer, { job: f.job, phase: 'prepared' }), /publication unavailable/);
});

for (const boundary of ['sourceSnapshot', 'publishSnapshot']) {
  test('selected reviewer restarting during ' + boundary + ' prevents a stale pass', async t => {
    const f = await fixture(t);
    const restart = () => {
      f.childTurns.push({ id: 'new_review', status: boundary === 'sourceSnapshot' ? 'inProgress' : 'completed' });
      if (boundary === 'sourceSnapshot') f.metadata.get('review').status.type = 'active';
    };
    let reader = f.reader;
    if (boundary === 'sourceSnapshot') {
      const original = f.store.sourceSnapshot.bind(f.store);
      f.store.sourceSnapshot = async (...args) => { const result = await original(...args); restart(); return result; };
    } else reader = createInternalReviewReader({ store: f.store, codex: f.codex, publishSnapshot: async () => { restart(); } });
    await assert.rejects(reader(f.project, f.pointer, { job: f.job, phase: boundary === 'sourceSnapshot' ? 'prepare' : 'terminal' }), /审核会话|最新已完成/);
  });
}

test('scene-only native review binds selected outputs and accepts an empty demonstration selection', async t => {
  const f = await fixture(t);
  const requiredOutputs = ['generation', 'evaluation'], environmentRefs = [{ revision: 'rev' }];
  Object.assign(f.project.generation, { requiredOutputs });
  Object.assign(f.job, { requiredOutputs, environmentRefs, requiredTasks: [{ revision: 'rev', taskId: 'task' }] });
  Object.assign(f.report, { requiredOutputs, environmentRefs, coverage: { episodes: [] } }); f.final();
  const accepted = await f.reader(f.project, f.pointer, { job: f.job });
  assert.deepEqual(accepted.episodes, []);
  f.report.requiredOutputs = ['demonstrations']; f.final();
  await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /requiredOutputs|绑定/);
});

test('native collection selection covers the same task ID separately for each real environment revision', async t => {
  const f = await fixture(t);
  const requiredOutputs = ['demonstrations'], environmentRefs = [{ revision: 'rev' }, { revision: 'rev_b', split: 'test' }];
  Object.assign(f.project.generation, { requiredOutputs, environmentRefs });
  Object.assign(f.job, { requiredOutputs, environmentRefs, requiredTasks: environmentRefs.map(row => ({ revision: row.revision, taskId: 'task' })) });
  const next = { ...f.project.episodes[0], episode: 'ep_b', revision: 'rev_b' };
  f.project.episodes.push(next);
  f.project.jobs.push({ ...f.run, id: 'run_b', revision: 'rev_b', result: { projectId: f.project.id, revision: 'rev_b', episodes: [next] } });
  Object.assign(f.report, { requiredOutputs, environmentRefs, coverage: { episodes: [
    { episodeId: 'ep', revision: 'rev', use: 'successful_demonstration' },
    { episodeId: 'ep_b', revision: 'rev_b', use: 'execution_data' }] } }); f.final();
  assert.deepEqual((await f.reader(f.project, f.pointer, { job: f.job })).episodes.map(row => row.revision), ['rev', 'rev_b']);
  f.report.coverage.episodes.pop(); f.final();
  await assert.rejects(f.reader(f.project, f.pointer, { job: f.job }), /rev_b|执行数据/);
});
