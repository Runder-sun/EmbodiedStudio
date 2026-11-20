import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { Store, contentHash } from '../src/store.mjs';
import { createTools } from '../src/tools.mjs';

async function fixture(t, overrides = {}) {
  const { inputPrompt = '生成一个工作台，机器人推方块', producerContract = false, ...toolOverrides } = overrides;
  const root = await mkdtemp(path.join(os.tmpdir(), 'scene-tools-'));
  const store = await new Store({ root }).load();
  const project = await store.create({ name: '场景', prompt: inputPrompt });
  store.beginGeneration(project.id);
  if (!producerContract) delete project.generation.deliveryContract; // Historical contract fixtures stay historical.
  const jobs = new Map(), requests = [], receipts = [];
  const remote = async payload => {
    requests.push(payload);
    if (payload.operation === 'submit') {
      if (!jobs.has(payload.jobId)) jobs.set(payload.jobId, { state: 'running', jobId: payload.jobId,
        events: [{ sequence: 1, type: 'submitted' }], cursor: 1, result: null });
      return jobs.get(payload.jobId);
    }
    if (payload.operation === 'job') {
      const job = jobs.get(payload.jobId);
      if (!job) throw new Error('job not found');
      if (payload.action === 'cancel') job.state = 'cancelled';
      return { ...job, events: job.events.filter(row => row.sequence > (payload.cursor || 0)) };
    }
    if (payload.operation === 'inspect') return { projectId: payload.projectId, revision: payload.revision, worldContext: { subtree: [] } };
    if (payload.operation === 'assets') return { items: [] };
    throw new Error('unexpected remote operation ' + payload.operation);
  };
  const reviewPointer = { threadId: 'native-review-child', turnId: 'review-turn', itemId: 'review-final' };
  // Transport-level source substitute; native provenance is exercised separately with real protocol shapes.
  const readInternalReview = async (target, pointer, { job }) => {
    assert.deepEqual(pointer, reviewPointer);
    return { ...pointer, schema: 'sceneagent-internal-review-v2', decision: 'pass', projectId: target.id, generationId: job.generationId,
      requirementVersion: job.requirementVersion, requestHash: job.requestHash, sourceHash: job.sourceHash, revision: job.revision,
      episodes: [{ episodeId: 'ep_a', taskId: 'push', revision: job.revision, use: 'successful_demonstration' }],
      snapshotRef: '.trace/inputs/' + 'a'.repeat(64) + '.json', snapshotHash: 'a'.repeat(64),
      checkpoint: { snapshotRef: '.trace/inputs/' + 'a'.repeat(64) + '.json', snapshotHash: 'a'.repeat(64) },
      ...(job.producedArtifacts ? { producedArtifacts: { ...job.producedArtifacts } } : {}) };
  };
  const tools = createTools({ store, remote, readInternalReview, stageOnlineAsset: async () => ({ digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) }),
    consumeGeneration: async () => ({ passed: true, sampleCount: 2 }),
    syncTrace: async () => {}, onDelivery: async (target, receipt) => { receipts.push(receipt); target.delivery = receipt; }, ...toolOverrides });
  t.after(async () => { await tools.close(); await rm(root, { recursive: true, force: true }); });
  const scope = { projectId: project.id, generationId: project.generation.id };
  const call = async (name, args = {}) => (await tools.call(name, { ...scope, ...(name === 'project_deliver' ? { review: reviewPointer } : {}), ...args })).structuredContent;
  const finish = (jobId, result, state = 'completed') => {
    const job = jobs.get(jobId);
    job.state = state; job.result = result; job.events.push({ sequence: 2, type: 'result', result }); job.cursor = 2;
  };
  return { store, project, tools, scope, call, finish, jobs, requests, receipts, remote, reviewPointer, readInternalReview };
}

async function ready(t, overrides) {
  const f = await fixture(t, overrides);
  await writeFile(path.join(f.store.directory(f.project.id), 'case.py'), '# isolated transport validation source\n');
  const source = await f.store.sourceSnapshot(f.project.id);
  const build = await f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'build_a' });
  f.finish(build.jobId, { projectId: f.project.id, revision: 'rev_a',
    sourceDigests: Object.fromEntries(Object.entries(source.sourceFiles).map(([name, text]) => [name, contentHash(text)])),
    io: { clock: { physicsRateHz: 200, controlRateHz: 50 } },
    tasks: [{ id: 'push', rubric: { sourceRef: 'rubrics.py', helperRefs: [] },
      evaluation: { cases: [{ id: 'case_a', sourceRef: 'case.py', helperRefs: [], expected: { success: false } }] } }],
    artifacts: { scene: `projects/${f.project.id}/revisions/rev_a/scene.json` } });
  await f.call('job', { jobId: build.jobId, action: 'status' });
  const check = await f.call('task_check', { revision: 'rev_a', requestId: 'check_a' });
  f.finish(check.jobId, { revision: 'rev_a', passed: true, tasks: [{ taskId: 'push', passed: true,
    rubric: { sourceRef: 'rubrics.py', sourceDigest: contentHash(source.sourceFiles['rubrics.py']) },
    cases: [{ id: 'case_a', episodeId: 'validation_ep', passed: true, result: { success: false } }] }] });
  await f.call('job', { jobId: check.jobId, action: 'status' });
  const run = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'run_a' });
  f.finish(run.jobId, { projectId: f.project.id, revision: 'rev_a', episodes: [{ episode: 'ep_a', taskId: 'push',
    revision: 'rev_a', demonstrationEligible: true, score: { success: true } }] });
  await f.call('job', { jobId: run.jobId, action: 'status' });
  f.deliver = () => f.call('project_deliver', { revision: 'rev_a', requestId: 'deliver_a',
    expectedRequirementVersion: f.project.generation.requirementVersion, requestHash: f.project.generation.requestHash });
  const interfaceRoot = `projects/${f.project.id}/consumer-reports/deliver_a/interfaces/push/`;
  f.consumer = { schema: 'sceneagent-product-consumption-v1', passed: true, projectId: f.project.id, revision: 'rev_a',
    artifactRef: `projects/${f.project.id}/consumer-reports/deliver_a/report.json`, reload: { passed: true },
    demonstrations: { passed: true, episodeCount: 1, batches: [{ taskId: 'push', episodeId: 'ep_a',
      use: 'successful_demonstration', demonstrationEligible: true, result: { success: true }, controlCycles: 20, physicalSteps: 80, uncommittedTail: false }] },
    evaluation: { passed: true, tasks: [{ taskId: 'push', passed: true }], isolation: { passed: true, checks: { networkBlocked: true } }, interfaces: [{ taskId: 'push', sourceEpisodeId: 'ep_a',
      passed: true, controlCycles: 1, physicalSteps: 4, scoreAvailable: true,
      recordRef: interfaceRoot+'records.json', episodeRef: interfaceRoot+'episode.json', sourceRef: interfaceRoot+'probe.py',
      sourceDigest: 'a'.repeat(64), sourceFactsDigest: 'b'.repeat(64),
      coverage: { open: 'checked', reset: 'checked', observe: 'checked', step: 'checked',
        record: 'checked', score: 'checked', endedStepRejection: 'not_exercised' } }] } };
  return f;
}

test('project inspection exposes raw recording scope and skips formal delivery submission', async t => {
  const f = await fixture(t);
  Object.assign(f.project.generation, { requiredOutputs: ['demonstrations'], videoOnly: true,
    recordOnlyVideos: true, recordOnlyTaskIds: ['native_task'] });
  const inspected = await f.call('project_inspect');
  assert.equal(inspected.recordOnlyVideos, true);
  assert.deepEqual(inspected.recordOnlyTaskIds, ['native_task']);
  await assert.rejects(f.tools.call('project_deliver', { ...f.scope, revision: 'unused_revision', requestId: 'unused_delivery',
    expectedRequirementVersion: f.project.generation.requirementVersion, requestHash: f.project.generation.requestHash,
  }), /原始录像.*不.*交付检查/);
  assert.equal(f.requests.some(row => row.operation === 'submit'), false);
});

for (const name of ['assets_prepare', 'scene_build', 'scene_observe', 'robot_run', 'project_deliver']) {
  test(name + ' preserves an explicit whole-worker budget in the Job and remote request', async t => {
    const f = await ready(t);
    const source = await f.store.sourceSnapshot(f.project.id);
    const args = { requestId: 'budget_'+name, workerTimeoutSeconds: 100000,
      ...(name === 'assets_prepare' ? { assetId: 'fixture:item' } : {}),
      ...(name === 'scene_build' ? { expectedSourceHash: source.sourceHash } : {}),
      ...(['scene_observe','robot_run','project_deliver'].includes(name) ? { revision: 'rev_a' } : {}),
      ...(name === 'robot_run' ? { taskIds: ['push'] } : {}),
      ...(name === 'project_deliver' ? { expectedRequirementVersion: f.project.generation.requirementVersion,
        requestHash: f.project.generation.requestHash } : {}) };
    const response = await f.call(name, args);
    if (name === 'project_deliver') await f.call('job', { jobId: response.jobId, action: 'wait', waitMs: 1000 });
    const job = f.project.jobs.find(row => row.id === response.jobId);
    assert.equal(job.workerTimeoutSeconds, 100000);
    assert.equal(response.workerTimeoutSeconds, 100000);
    assert.equal(f.requests.findLast(row => row.operation === 'submit').task.workerTimeoutSeconds, 100000);
    await assert.rejects(f.call(name, { ...args, workerTimeoutSeconds: 200000 }), /requestId.*不同输入|requestId.*不能/);
    assert.equal((await f.call(name, args)).jobId, job.id);
  });
}

test('default worker budget stays out of original arguments and old exact retries', async t => {
  const f = await ready(t);
  const args = { revision: 'rev_a', taskIds: ['push'], requestId: 'budget_default' };
  const response = await f.call('robot_run', args);
  const job = f.project.jobs.find(row => row.id === response.jobId);
  assert.equal(job.workerTimeoutSeconds, 900);
  assert.equal(Object.hasOwn(job.payload.task, 'workerTimeoutSeconds'), false);
  const hash = job.inputHash, payload = structuredClone(job.payload);
  delete job.workerTimeoutSeconds; // Preserved historical Job with no recorded budget.
  const retried = await f.call('robot_run', args);
  assert.equal(retried.workerTimeoutSeconds, undefined);
  assert.equal(job.inputHash, hash);
  assert.deepEqual(job.payload, payload);
  assert.equal(Object.hasOwn(job, 'workerTimeoutSeconds'), false);
});

test('invalid budgets and CPU-only budget arguments fail without new work or a generation wait', async t => {
  const f = await ready(t);
  const count = f.project.jobs.length;
  for (const workerTimeoutSeconds of [null, false, true, '1800', 0, -1, NaN, Infinity]) {
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'invalid_budget', workerTimeoutSeconds }), /workerTimeoutSeconds/);
  }
  await assert.rejects(f.call('task_check', { revision: 'rev_a', requestId: 'cpu_budget', workerTimeoutSeconds: 10 }), /workerTimeoutSeconds/);
  assert.equal(f.project.jobs.length, count);
  assert.equal(f.project.generation.status, 'active');
  assert.equal(f.project.generation.waitingSystem, undefined);
});

test('explicit rubric budget is version-gated, preserved and bound to exact retries', async t => {
  const f = await ready(t);
  const args = { revision: 'rev_a', taskIds: ['push'], requestId: 'rubric_budget', rubricSeconds: 180 };
  await assert.rejects(f.call('robot_run', args), /旧.*评分预算|评分预算.*旧/);
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = { adjustableRubricBudget: true };
  const response = await f.call('robot_run', args);
  const job = f.project.jobs.find(row => row.id === response.jobId);
  assert.equal(response.rubricSeconds, 180);
  assert.equal(job.rubricSeconds, 180);
  assert.equal(job.payload.task.rubricSeconds, 180);
  assert.equal((await f.call('robot_run', args)).jobId, job.id);
  await assert.rejects(f.call('robot_run', { ...args, rubricSeconds: 360 }), /requestId.*不同输入|requestId.*不能/);
});

test('invalid rubric budgets create no work and omitted budgets stay omitted', async t => {
  const f = await ready(t), count = f.project.jobs.length;
  for (const rubricSeconds of [null, true, '180', 0, -1, NaN, Infinity]) {
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'bad_rubric_budget', rubricSeconds }), /rubricSeconds/);
  }
  assert.equal(f.project.jobs.length, count);
  const response = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'omitted_rubric_budget' });
  const job = f.project.jobs.find(row => row.id === response.jobId);
  assert.equal(Object.hasOwn(job.payload.task, 'rubricSeconds'), false);
  assert.equal(Object.hasOwn(job, 'rubricSeconds'), false);
});

async function produced(t, overrides = {}) {
  const phases = [];
  const consumer = async (project, pointer, { job, phase }) => {
    phases.push(phase);
    const root = `projects/${project.id}/deliveries/artifacts/${pointer.sha256}/`;
    return { schema: 'sceneagent-produced-consumption-v1', passed: true, ...pointer,
      binding: { ...Object.fromEntries(['projectId', 'generationId', 'requirementVersion', 'requestHash', 'sourceHash', 'revision']
        .map(key => [key, key === 'projectId' ? project.id : job[key]])),
        ...(job.environmentRefs ? { requiredOutputs: job.requiredOutputs, environmentRefs: job.environmentRefs } : {}) },
      episodes: job.reviewedEpisodes.map(({ episodeId, taskId, use, revision }) => ({ episodeId, taskId, use, ...(job.environmentRefs ? { revision } : {}) })),
      frozenManifestRef: root + 'manifest.json', environmentRoot: root + 'environment', artifactRef: root + 'consumption.json',
      products: { generation: { atif: root + 'generation/atif.json' }, robot: { hdf5: root + 'robot/data.hdf5' }, environment: { root: root + 'environment' } },
      files: [{ artifactRef: root + 'robot/data.hdf5', bytes: 16, sha256: 'b'.repeat(64) }] };
  };
  const f = await ready(t, { producerContract: true, consumeProducedArtifacts: consumer, ...overrides });
  f.producedArtifacts = { manifestRef: `projects/${f.project.id}/delivery/run/manifest.json`, sha256: 'c'.repeat(64) };
  f.consumer.reload.environmentRoot = `projects/${f.project.id}/deliveries/artifacts/${f.producedArtifacts.sha256}/environment`;
  f.producedConsumer = consumer;
  f.phases = phases;
  f.deliver = (extra = {}) => f.call('project_deliver', { revision: 'rev_a', requestId: 'deliver_a',
    expectedRequirementVersion: f.project.generation.requirementVersion, requestHash: f.project.generation.requestHash,
    producedArtifacts: f.producedArtifacts, ...extra });
  return f;
}

test('new delivery contract requires producer manifest without changing legacy delivery', async t => {
  const f = await ready(t, { producerContract: true });
  await assert.rejects(f.deliver(), /producedArtifacts|产物清单/);
  assert.equal(f.project.jobs.filter(row => row.kind === 'consume').length, 0);
});

test('producer export contract freezes before native consumption and binds final files without rebuilding physics', async t => {
  const f = await produced(t), originalSource = f.project.sourceHash;
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.deepEqual(f.phases, ['prepare']);
  assert.equal(f.requests.findLast(row => row.operation === 'submit').task.environmentRoot,
    `projects/${f.project.id}/deliveries/artifacts/${f.producedArtifacts.sha256}/environment`);
  f.finish(pending.jobId, f.consumer);
  const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.deepEqual(f.phases, ['prepare', 'terminal']);
  assert.equal(done.delivery.accepted, true);
  assert.equal(done.delivery.producedArtifacts.sha256, f.producedArtifacts.sha256);
  assert.equal(done.delivery.products.demonstrations.exports.hdf5,
    `projects/${f.project.id}/deliveries/artifacts/${f.producedArtifacts.sha256}/robot/data.hdf5`);
  assert.equal(f.project.sourceHash, originalSource);
  assert.equal(f.project.jobs.filter(row => row.kind === 'build').length, 1);
  const retried = await f.deliver();
  assert.equal(retried.jobId, pending.jobId);
  assert.deepEqual(f.phases, ['prepare', 'terminal']);
});

for (const change of ['manifest', 'binding', 'episodes', 'files', 'environment', 'passed']) {
  test('producer delivery rejects changed or invalid ' + change + ' at final publication', async t => {
    let f;
    f = await produced(t, { consumeProducedArtifacts: async (...args) => {
      const result = await f.producedConsumer(...args);
      if (args[2].phase === 'terminal') {
        if (change === 'manifest') result.sha256 = 'd'.repeat(64);
        if (change === 'binding') result.binding.sourceHash = 'other';
        if (change === 'episodes') result.episodes[0].use = 'execution_data';
        if (change === 'files') result.files[0].sha256 = 'd'.repeat(64);
        if (change === 'environment') result.environmentRoot = 'projects/foreign/environment';
        if (change === 'passed') result.passed = false;
      }
      return result;
    } });
    const pending = await f.deliver();
    await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
    f.finish(pending.jobId, f.consumer);
    const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
    assert.equal(done.delivery.accepted, false);
    assert.match(done.delivery.missing.join(' '), /产物|清单|来源|冻结/);
  });
}

test('producer CPU failure is returned as the existing failed delivery job with no new GPU submission', async t => {
  const f = await produced(t, { consumeProducedArtifacts: async () => { throw new Error('official reader rejected HDF5'); } });
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(done.status, 'failed');
  assert.match(done.error, /official reader rejected HDF5/);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});

test('a legacy environment consumer cannot accept new exports by silently loading the original scene', async t => {
  const f = await produced(t);
  delete f.consumer.reload.environmentRoot;
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.finish(pending.jobId, f.consumer);
  const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(done.delivery.accepted, false);
  assert.match(done.delivery.missing.join(' '), /环境根目录/);
});

for (const change of ['stop', 'requirement', 'source']) test('producer preparation cannot submit after ' + change + ' changes', async t => {
  let f;
  f = await produced(t, { consumeProducedArtifacts: async (...args) => {
    const result = await f.producedConsumer(...args);
    if (change === 'stop') f.project.generation.cancelRequestedAt = new Date().toISOString();
    if (change === 'requirement') f.project.generation.requirementVersion++;
    if (change === 'source') await writeFile(path.join(f.store.directory(f.project.id), 'scene.py'), '# changed physical source\n');
    return result;
  } });
  const pending = await f.deliver();
  const done = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.ok(['failed', 'cancelled'].includes(done.status));
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});

test('export failure is repaired through a new manifest request without rerunning scene or demonstration', async t => {
  let f, rejected = false;
  f = await produced(t, { consumeProducedArtifacts: async (...args) => {
    if (!rejected) { rejected = true; throw new Error('producer must repair format'); }
    return f.producedConsumer(...args);
  } });
  const failed = await f.deliver();
  assert.equal((await f.call('job', { jobId: failed.jobId, action: 'wait', waitMs: 1000 })).status, 'failed');
  const pending = await f.deliver({ requestId: 'deliver_repaired', producedArtifacts: { ...f.producedArtifacts, sha256: 'e'.repeat(64) } });
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const repairedConsumer = structuredClone(f.consumer);
  repairedConsumer.reload.environmentRoot = `projects/${f.project.id}/deliveries/artifacts/${'e'.repeat(64)}/environment`;
  const oldRoot = `projects/${f.project.id}/consumer-reports/deliver_a/`;
  const newRoot = `projects/${f.project.id}/consumer-reports/deliver_repaired/`;
  repairedConsumer.artifactRef = repairedConsumer.artifactRef.replace(oldRoot, newRoot);
  for (const row of repairedConsumer.evaluation.interfaces) for (const key of ['recordRef', 'episodeRef', 'sourceRef']) row[key] = row[key].replace(oldRoot, newRoot);
  f.finish(pending.jobId, repairedConsumer);
  assert.equal((await f.call('job', { jobId: pending.jobId, action: 'status' })).delivery.accepted, true);
  assert.equal(f.project.jobs.filter(row => row.kind === 'build').length, 1);
  assert.equal(f.project.jobs.filter(row => row.kind === 'rollout').length, 1);
  assert.equal(f.project.jobs.find(row => row.id === failed.jobId).status, 'failed');
});

test('batched frozen-camera observation preserves the selection and returns each image through the existing job', async t => {
  const f=await ready(t);
  const task=f.project.jobs.find(row=>row.kind==='build').result.tasks[0];
  task.recording={primaryCameraId:'front',cameras:[{cameraId:'front'},{cameraId:'side'}]};
  const pending=await f.call('scene_observe',{revision:'rev_a',cameraIds:['front','side'],requestId:'batch_views'});
  assert.deepEqual(f.requests.at(-1).task.cameraIds,['front','side']);
  f.finish(pending.jobId,{revision:'rev_a',cameraIds:['front','side'],observations:[
    {cameraId:'front',image:{mimeType:'image/png',data:'Zmlyc3Q='}},
    {cameraId:'side',image:{mimeType:'image/png',data:'c2Vjb25k'}}]});
  const reply=await f.tools.call('job',{...f.scope,jobId:pending.jobId,action:'status'});
  assert.equal(reply.content.filter(row=>row.type==='image').length,2);
  assert.deepEqual(reply.structuredContent.result.observations.map(row=>row.cameraId),['front','side']);
  assert.ok(reply.structuredContent.result.observations.every(row=>row.image===undefined));
  const retried=await f.call('scene_observe',{revision:'rev_a',cameraIds:['front','side'],requestId:'batch_views'});
  assert.equal(retried.jobId,pending.jobId);
  assert.equal(f.requests.filter(row=>row.operation==='submit'&&row.task.operation==='observe').length,1);
});

test('batch image references bind every camera even when several cameras return identical pixels', async t => {
  const f=await ready(t),ids=['front','side','back'];
  f.project.jobs.find(row=>row.kind==='build').result.tasks[0].recording={primaryCameraId:'front',cameras:ids.map(cameraId=>({cameraId}))};
  const pending=await f.call('scene_observe',{revision:'rev_a',cameraIds:ids,requestId:'same_pixels'});
  const pixels=['c2FtZQ==','c2FtZQ==','ZGlmZmVyZW50'];
  f.finish(pending.jobId,{revision:'rev_a',cameraIds:ids,observations:ids.map((cameraId,i)=>({cameraId,image:{mimeType:'image/png',data:pixels[i]}}))});
  const reply=await f.tools.call('job',{...f.scope,jobId:pending.jobId,action:'status'});
  assert.equal(reply.content.filter(row=>row.type==='image').length,2);
  for(const [i,row] of reply.structuredContent.result.observations.entries()) {
    assert.ok(Number.isInteger(row.imageContentIndex)&&row.imageContentIndex>=1);
    assert.equal(reply.content[row.imageContentIndex].type,'image');
    assert.equal(reply.content[row.imageContentIndex].data,pixels[i]);
  }
  assert.deepEqual(reply.structuredContent.result.observations.map(row=>row.imageContentIndex),[1,1,2]);
});

test('batched observation rejects mixed, duplicate, oversized and foreign camera selections before computation', async t => {
  const f=await ready(t);
  f.project.jobs.find(row=>row.kind==='build').result.tasks[0].recording={primaryCameraId:'front',cameras:[{cameraId:'front'},{cameraId:'side'}]};
  const inputs=[{cameraIds:[]},{cameraIds:['front','front']},{cameraIds:['front','side','a','b','c']},
    {cameraIds:['unknown']},{cameraIds:['front'],cameraId:'front'},{cameraIds:['front'],nodeId:'room'},
    {cameraIds:['front'],episodeId:'ep_a'},{cameraIds:['front'],frame:0}];
  for(let i=0;i<inputs.length;i++) await assert.rejects(f.call('scene_observe',{revision:'rev_a',requestId:'invalid_batch_'+i,...inputs[i]}));
  assert.equal(f.requests.some(row=>row.operation==='submit'&&row.task.operation==='observe'),false);
});

test('web preview observation accepts a completed generation without changing its delivery or source', async t => {
  const f=await ready(t);
  f.project.generation.status='complete';f.project.delivery={accepted:true,marker:'SYSTEM_VERIFICATION_ONLY immutable receipt'};
  const before=structuredClone({generation:f.project.generation,delivery:f.project.delivery,latestBuild:f.project.latestBuild,sourceHash:f.project.sourceHash});
  const job=await f.call('scene_observe',{revision:'rev_a',output:'web_preview',requestId:'preview_repair'});
  assert.equal(job.kind,'observe');
  assert.equal(f.requests.at(-1).task.output,'web_preview');
  assert.deepEqual({generation:f.project.generation,delivery:f.project.delivery,latestBuild:f.project.latestBuild,sourceHash:f.project.sourceHash},before);
  assert.equal((await f.call('scene_observe',{revision:'rev_a',output:'web_preview',requestId:'preview_repair'})).jobId,job.jobId);
});

test('web preview selection rejects camera, node and episode options before submitting compute', async t => {
  const f=await ready(t);
  for(const [index,choice] of [{cameraId:'front'},{cameraIds:['front']},{nodeId:'room'},{episodeId:'ep_a'},{frame:0}].entries()) {
    await assert.rejects(f.call('scene_observe',{revision:'rev_a',output:'web_preview',requestId:'mixed_preview_'+index,...choice}),/网页预览/);
  }
  assert.equal(f.requests.some(row=>row.operation==='submit'&&row.task.operation==='observe'),false);
});

for (const consumerPassed of [true, false]) test('minimal native review receipt preserves all delivery boundaries with consumer pass ' + consumerPassed, async t => {
  const phases = [];
  const f = await ready(t, { readInternalReview: async (project, pointer, options) => {
    phases.push(options.phase);
    const review = await f.readInternalReview(project, pointer, options);
    return { ...review, episodes: review.episodes.map(({ episodeId, taskId, use }) => ({ episodeId, taskId, use })) };
  } });
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.deepEqual(phases, ['prepare', 'prepared']);
  assert.deepEqual(f.requests.find(row => row.operation === 'submit' && row.task.operation === 'consume').task.episodes,
    [{ episodeId: 'ep_a', taskId: 'push', use: 'successful_demonstration' }]);
  f.consumer.passed = consumerPassed;
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.deepEqual(phases, ['prepare', 'prepared', 'terminal']);
  const receipt = f.receipts.at(-1);
  assert.equal(receipt.accepted, consumerPassed);
  assert.equal(receipt.semanticScope, 'native_review_receipt_provenance_binding_and_consumer_checks_not_formal_natural_language_proof');
  assert.equal(receipt.products.demonstrations.batches[0].result.success, true);
  if (!consumerPassed) assert.match(receipt.missing.join(' '), /消费者报告/);
});

test('current consumption contract accepts reviewed execution data without a second physical success', async t => {
  const f = await ready(t, { inputPrompt: 'SYSTEM_VERIFICATION_ONLY 收集实际失败的机器人数据，不要求任务成功。', readInternalReview: async (...args) => {
    const review = await f.readInternalReview(...args);
    return { ...review, episodes: review.episodes.map(row => ({ ...row, use: 'execution_data' })) };
  } });
  const batch = f.consumer.demonstrations.batches[0];
  Object.assign(batch, { use: 'execution_data', demonstrationEligible: false, result: { success: false } });
  Object.assign(f.project.episodes[0], { demonstrationEligible: false, score: { success: false } });
  const pending = await f.deliver(); await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, true);
  assert.equal(f.receipts.at(-1).products.demonstrations.batches[0].result.success, false);
  assert.deepEqual(f.requests.find(row => row.operation === 'submit' && row.task.operation === 'consume').task.episodes,
    [{ episodeId: 'ep_a', taskId: 'push', use: 'execution_data' }]);
});

for (const variant of ['legacy schema', 'success false', 'success unknown', 'use downgrade', 'use missing', 'unmeasured step', 'foreign probe', 'missing interface']) {
  test('current consumption contract rejects '+variant, async t => {
    const f = await ready(t), pending = await f.deliver();
    await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
    const batch = f.consumer.demonstrations.batches[0], probe = f.consumer.evaluation.interfaces[0];
    f.consumer.demonstrations.replay = [{ episodeId: 'ep_a', passed: true }];
    if (variant === 'legacy schema') { delete f.consumer.schema; f.consumer.demonstrations.replay = [{ episodeId: 'ep_a', passed: true }]; }
    if (variant === 'success false') { batch.result.success = false; batch.demonstrationEligible = false; }
    if (variant === 'success unknown') batch.result.success = null;
    if (variant === 'use downgrade') batch.use = 'execution_data';
    if (variant === 'use missing') delete batch.use;
    if (variant === 'unmeasured step') probe.coverage.step = 'not_exercised';
    if (variant === 'foreign probe') probe.sourceEpisodeId = 'unreviewed';
    if (variant === 'missing interface') f.consumer.evaluation.interfaces = [];
    f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
    assert.equal(f.receipts.at(-1).accepted, false);
  });
}

test('an initially ended diagnostic keeps zero fresh steps and uses the explicit source execution', async t => {
  const f = await ready(t), pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const probe = f.consumer.evaluation.interfaces[0];
  Object.assign(probe, { controlCycles: 0, physicalSteps: 0 });
  Object.assign(probe.coverage, { step: 'not_exercised', endedStepRejection: 'checked' });
  f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, true);
  assert.equal(f.receipts.at(-1).products.evaluation.interfaces[0].coverage.step, 'not_exercised');
});

for (const variant of ['wrong ticks', 'zero without rejection', 'foreign record', 'bad source digest', 'score unavailable', 'overall incomplete', 'null probe', 'null batch']) {
  test('interface evidence rejects '+variant, async t => {
    const f = await ready(t), pending = await f.deliver();
    await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
    const probe = f.consumer.evaluation.interfaces[0];
    if (variant === 'wrong ticks') probe.physicalSteps = 5;
    if (variant === 'zero without rejection') { Object.assign(probe, { controlCycles: 0, physicalSteps: 0 }); probe.coverage.step = 'not_exercised'; }
    if (variant === 'foreign record') probe.recordRef = 'projects/foreign/consumer-reports/other/records.json';
    if (variant === 'bad source digest') probe.sourceFactsDigest = 'not-a-digest';
    if (variant === 'score unavailable') probe.scoreAvailable = false;
    if (variant === 'overall incomplete') f.consumer.passed = false;
    if (variant === 'null probe') f.consumer.evaluation.interfaces = [null];
    if (variant === 'null batch') f.consumer.demonstrations.batches = [null];
    f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
    assert.equal(f.receipts.at(-1).accepted, false);
  });
}

for (const phase of ['prepare', 'prepared', 'terminal']) test('current delivery rejects a legacy review at '+phase, async t => {
  const f = await ready(t, { readInternalReview: async (project, pointer, options) => {
    const review = await f.readInternalReview(project, pointer, options);
    return options.phase === phase ? { ...review, schema: 'sceneagent-internal-review-v1' } : review;
  } });
  if (phase === 'prepare') { await assert.rejects(f.deliver(), /审核/); return; }
  const pending = await f.deliver(); await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  if (phase === 'prepared') { assert.equal(f.jobs.has(pending.jobId), false); return; }
  f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
});

test('a scoped system wait is persisted by the host without creating remote work', async t => {
  const reports = [];
  const f = await fixture(t, { onSystemWait: async (project, input) => {
    reports.push(input);
    project.generation.waitingSystem = { ...input, kind: 'reported', since: new Date().toISOString() };
    project.generation.status = 'waiting_system';
    return project.generation;
  } });
  const input = { requestId: 'wait_kernel', reason: '执行器超时，需要检查系统。', evidence: ['job failed with a worker timeout'] };
  const result = await f.call('generation_wait_system', input);
  assert.equal(result.status, 'waiting_system');
  assert.equal(reports.length, 1);
  assert.equal(reports[0].generationId, f.scope.generationId);
  assert.deepEqual(reports[0].evidence, input.evidence);
  assert.equal(f.project.jobs.length, 0);
  assert.equal(f.requests.length, 0);
});

test('system wait rejects empty evidence and foreign job references before changing state', async t => {
  let reports = 0;
  const f = await fixture(t, { onSystemWait: async () => { reports++; } });
  for (const extra of [{ reason: ' ' }, { evidence: [] }, { evidence: [' '] }, { jobIds: ['foreign'] }]) {
    await assert.rejects(f.call('generation_wait_system', {
      requestId: 'wait_kernel', reason: '系统阻碍', evidence: ['actual failure'], ...extra,
    }));
  }
  await assert.rejects(f.tools.call('generation_wait_system', { ...f.scope, generationId: 'other',
    requestId: 'wait_other', reason: '系统阻碍', evidence: ['actual failure'] }));
  assert.equal(reports, 0);
  assert.equal(f.project.generation.waitingSystem, undefined);
});

test('system waiting rejects new computation but preserves existing receipts, inspection and job cancellation', async t => {
  const f = await fixture(t);
  const source = await f.store.sourceSnapshot(f.project.id);
  const buildArgs = { expectedSourceHash: source.sourceHash, requestId: 'already_admitted' };
  const started = await f.call('scene_build', buildArgs);
  f.project.generation.waitingSystem = { requestId: 'blocked', reason: '系统阻碍', evidence: ['failed tool'], since: new Date().toISOString() };
  f.project.generation.status = 'waiting_system';
  for (const [name, args] of [
    ['scene_build', { ...buildArgs, requestId: 'new_build' }],
    ['assets_prepare', { requestId: 'new_prepare', assetId: 'asset_a' }],
    ['scene_observe', { revision: 'rev_a', requestId: 'new_observe' }],
    ['task_check', { revision: 'rev_a', requestId: 'new_check' }],
    ['robot_run', { revision: 'rev_a', requestId: 'new_run', taskIds: ['task_a'] }],
  ]) await assert.rejects(f.call(name, args), /等待/);
  assert.equal(f.project.jobs.length, 1);
  assert.equal((await f.call('scene_build', buildArgs)).jobId, started.jobId);
  const inspected = await f.call('project_inspect');
  assert.equal(inspected.generationStatus, 'waiting_system');
  assert.equal(inspected.waitingSystem.reason, '系统阻碍');
  assert.equal((await f.call('job', { jobId: started.jobId, action: 'status' })).status, 'running');
  assert.equal((await f.call('job', { jobId: started.jobId, action: 'cancel' })).status, 'cancelled');
});

test('entering system wait does not abort a computation already admitted during source preparation', async t => {
  const f = await fixture(t);
  const source = await f.store.sourceSnapshot(f.project.id);
  const original = f.store.sourceSnapshot.bind(f.store);
  let entered, release;
  const opened = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  f.store.sourceSnapshot = async (...args) => { entered(); await gate; return original(...args); };
  const starting = f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'preparing' });
  await opened;
  f.project.generation.waitingSystem = { requestId: 'blocked', reason: '系统阻碍', evidence: ['failed tool'], since: new Date().toISOString() };
  f.project.generation.status = 'waiting_system';
  release();
  const result = await starting;
  assert.equal(result.status, 'running');
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
});

for (const variant of ['missing rubric', 'invalid rubric digest', 'missing cases', 'duplicate cases', 'missing case id', 'failed task check']) test('delivery rejects incomplete generated grading before consumption: ' + variant, async t => {
  const f = await ready(t);
  const build = f.project.jobs.find(row => row.kind === 'build').result, task = build.tasks[0];
  if (variant === 'missing rubric') delete task.rubric;
  if (variant === 'invalid rubric digest') build.sourceDigests['rubrics.py'] = 'invalid';
  if (variant === 'missing cases') task.evaluation.cases = [];
  if (variant === 'duplicate cases') task.evaluation.cases.push({ ...task.evaluation.cases[0] });
  if (variant === 'missing case id') delete task.evaluation.cases[0].id;
  if (variant === 'failed task check') f.project.latestCheck.passed = false;
  await assert.rejects(f.deliver(), /评分|验证/);
  assert.equal(f.requests.some(row => row.operation === 'submit' && row.task?.operation === 'consume'), false);
});

test('project inspect keeps complete task/world/control data and the published manifest reference without duplicate inventories', async t => {
  const f = await ready(t); f.tools.close();
  const reference = `projects/${f.project.id}/revisions/rev_a/manifest.json`, sha256 = 'a'.repeat(64);
  const built = f.project.jobs.find(row => row.kind === 'build').result;
  built.artifacts.manifest = reference;
  built.publicationArtifactDigests = { [reference]: sha256 };
  const inspected = { ...built, worldContext: { nodeId: null, subtree: [{ id: 'world' }, { id: 'item' }] }, nodes: 2,
    io: { schema: 'sceneagent-native-io-v1', clock: { physicsRateHz: 200, controlRateHz: 50 }, ports: {}, streams: {} },
    capabilities: { robotId: 'franka', baseFrame: '/World/franka/base', tcpFrame: '/World/franka/tcp',
      actionSpec: { shape: [8] }, unsupportedCapabilities: ['mobile_base_control'] },
    compiled: { bindings: { robot: '/World/franka', tcp: '/World/franka/tcp', joints: ['native-joint'], drives: { mode: 'position' },
      nativeMeshLocalBounds: 'x'.repeat(20000) }, firstLoadValidation: 'x'.repeat(170000) },
    assets: { inventory: 'x'.repeat(50000) }, runtimeVersions: { motion: { files: 'x'.repeat(200000) } } };
  inspected.manifest = { ...inspected };
  const tools = createTools({ store: f.store, remote: async () => inspected }); t.after(() => tools.close());
  const result = await tools.call('project_inspect', f.scope), data = result.structuredContent;
  assert.deepEqual(data.requirements, f.project.requirements);
  assert.equal(data.originalRequest, f.project.prompt);
  for (const key of ['worldContext', 'tasks', 'sourceDigests', 'capabilities', 'io']) assert.deepEqual(data[key], inspected[key]);
  for (const key of ['robot', 'tcp', 'joints', 'drives']) assert.deepEqual(data.compiled.bindings[key], inspected.compiled.bindings[key]);
  assert.equal(data.artifacts.manifest, reference);
  assert.equal(data.publicationArtifactDigests[reference], sha256);
  assert.equal(Object.hasOwn(data, 'manifest'), false);
  assert.equal(Object.hasOwn(data, 'assets'), false);
  assert.equal(Object.hasOwn(data, 'runtimeVersions'), false);
  assert.equal(Object.hasOwn(data.compiled, 'firstLoadValidation'), false);
  assert.ok(Buffer.byteLength(JSON.stringify(result)) < 48000);
  assert.equal(result.content[0].text, JSON.stringify(data));
  delete built.publicationArtifactDigests;
  const missing = (await tools.call('project_inspect', f.scope)).structuredContent;
  assert.equal(missing.publicationArtifactDigests?.[reference], undefined, 'no invented or rehashed manifest identity');
});

test('project inspection discovers existing trace archives without extra source snapshots or records', async t => {
  const f = await fixture(t);
  await f.store.sourceSnapshot(f.project.id);
  let snapshots = 0;
  const sourceSnapshot = f.store.sourceSnapshot.bind(f.store);
  f.store.sourceSnapshot = async (...args) => { snapshots++; return sourceSnapshot(...args); };
  const before = (await f.store.trace(f.project.id)).length;
  const result = await f.call('project_inspect');
  assert.deepEqual(result.recording, {
    localTrace: path.join(f.store.directory(f.project.id), 'trace.jsonl'),
    mirroredTrace: `projects/${f.project.id}/generation-trace.jsonl`,
    archiveDirectories: ['.trace/sources', '.trace/inputs'],
    archiveAccess: 'read_only_original_bytes',
    mirror: { syncedAt: null, sha256: null, error: null },
    traceScope: 'complete_line_prefix; later events are not part of a frozen export',
  });
  assert.equal(snapshots, 1, 'keep the pre-existing project_inspect sourceSnapshot only');
  assert.equal((await f.store.trace(f.project.id)).length - before, 2, 'keep the existing tool call/result records only');
});

test('tools reject different session/job/revision scope before remote effects', async t => {
  const f = await fixture(t);
  await assert.rejects(f.tools.call('assets_lookup', { ...f.scope, generationId: 'wrong' }), /生成|scope|generation/);
  await assert.rejects(f.call('robot_run', { revision: 'foreign', taskIds: ['push'], requestId: 'run_x' }), /版本|revision/);
  await assert.rejects(f.call('job', { jobId: 'foreign', action: 'status' }), /作业|job/);
  assert.equal(f.requests.length, 0);
});

test('asset preparation accepts one online source and retains its provenance in the durable request', async t => {
  const f = await fixture(t);
  const result = await f.call('assets_prepare', { requestId: 'online_asset',
    url: 'https://example.com/chair.glb', sourcePage: 'https://example.com/chair',
    name: 'wood chair', license: 'CC0; declaration on source page' });
  assert.equal(result.status, 'queued');
  assert.equal((await f.call('job', { jobId: result.jobId, action: 'wait', waitMs: 1000 })).status, 'running');
  const submitted = f.requests.find(row => row.operation === 'submit');
  assert.equal(submitted.task.url, 'https://example.com/chair.glb');
  assert.equal(submitted.task.sourcePage, 'https://example.com/chair');
  assert.equal(submitted.task.importRef.digest, 'a'.repeat(64));
  assert.equal(submitted.projectId, f.project.id);
});

test('online source is staged once, persisted before remote submission, and a local download failure does not become an ambiguous remote job', async t => {
  let downloads = 0;
  const f = await fixture(t, { stageOnlineAsset: async () => { downloads++; return { digest: 'c'.repeat(64), retrievalDigest: 'd'.repeat(64) }; } });
  const args = { requestId: 'online_once', url: 'https://example.com/chair.glb', sourcePage: 'https://example.com/chair', name: 'chair' };
  const started = await f.call('assets_prepare', args); await f.call('assets_prepare', args);
  await f.call('job', { jobId: started.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(downloads, 1);
  assert.equal(f.project.jobs[0].payload.task.importRef.digest, 'c'.repeat(64));
  const failed = await fixture(t, { stageOnlineAsset: async () => { throw Error('public download failed'); } });
  const pending = await failed.call('assets_prepare', args);
  const failure = await failed.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(failure.status, 'failed'); assert.match(failure.error, /public download failed/);
  assert.equal(failed.requests.length, 0);
  assert.equal(failed.project.jobs[0].status, 'failed');
  assert.equal((await failed.call('assets_prepare', args)).jobId, pending.jobId);
});

test('restart before online submit terminates the unsent attempt without failing a live local reservation', async t => {
  let ready;
  const started = new Promise(resolve => { ready = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async (_url, { signal }) => {
    ready(); return new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
  } });
  void f.call('assets_prepare', { requestId: 'local_stage', url: 'https://example.com/model.glb',
    sourcePage: 'https://example.com/model', name: 'chair' });
  await started;
  const live = f.project.jobs[0];
  assert.equal((await f.call('job', { jobId: live.id, action: 'status' })).status, 'queued');
  await f.store.save();
  const store = await new Store({ root: f.store.root }).load();
  const project = store.get(f.project.id), recovered = project.jobs[0];
  let notifications = 0;
  const tools = createTools({ store, remote: f.remote, onJobTerminal: async () => { notifications++; } });
  t.after(() => tools.close());
  const result = await tools.refreshJob(project, recovered);
  assert.equal(result.status, 'failed');
  assert.match(result.error, /提交前.*中断/);
  assert.equal(notifications, 1);
  assert.equal(f.requests.length, 0);
});

test('asset source selection and generation snapshots are checked before any remote effect', async t => {
  const f = await fixture(t);
  await assert.rejects(f.call('assets_prepare', { requestId: 'none' }), /恰好一个/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'two', assetId: 'chair',
    url: 'https://example.com/chair.glb' }), /恰好一个/);
  await writeFile(path.join(f.store.directory(f.project.id), 'asset.py'), 'def build_asset(materials):\n    return None\n');
  const snapshot = await f.store.sourceSnapshot(f.project.id);
  await assert.rejects(f.call('assets_prepare', { requestId: 'missing_hash', sourceFile: 'asset.py' }), /expectedSourceHash/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'stale', sourceFile: 'asset.py', expectedSourceHash: 'old' }), /摘要/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'outside', sourceFile: '../asset.py', expectedSourceHash: snapshot.sourceHash }), /快照/);
  assert.equal(f.requests.length, 0);
  const prepared = await f.call('assets_prepare', { requestId: 'generate', sourceFile: 'asset.py',
    expectedSourceHash: snapshot.sourceHash, materialIds: ['material:WoodFloor052'] });
  assert.equal(prepared.status, 'running');
  const payload = f.requests.find(row => row.operation === 'submit').task;
  assert.equal(payload.sourceHash, snapshot.sourceHash);
  assert.equal(payload.sourceFiles['asset.py'], 'def build_asset(materials):\n    return None\n');
  assert.deepEqual(payload.materialIds, ['material:WoodFloor052']);
});

test('native resources use the existing durable asset tool and scene build carries references only', async t => {
  const f = await fixture(t);
  const args = { requestId: 'native_resource', resourceFile: '/sdk/models/arm.urdf',
    dependencyFiles: ['/sdk/models/mesh.obj', '/sdk/models/controller.yaml'],
    mdlSearchRoots: ['/sdk/models/mdl'], name: 'fixture native package' };
  const job = await f.call('assets_prepare', args);
  assert.equal(job.status, 'running');
  assert.equal(f.requests.at(-1).task.resourceFile, args.resourceFile);
  assert.deepEqual(f.requests.at(-1).task.dependencyFiles, args.dependencyFiles);
  assert.deepEqual(f.requests.at(-1).task.mdlSearchRoots, args.mdlSearchRoots);
  assert.equal(f.requests.at(-1).task.sourceFiles, undefined);
  f.finish(job.jobId, { kind: 'native_files', reportPath: '/data/assets/prepared/digest/report.json',
    assetId: 'imported:digest', entry: 'files/arm.urdf', path: '/data/assets/prepared/digest/files/arm.urdf',
    nativeFiles: ['files/arm.urdf', 'files/mesh.obj'], preparedFiles: ['report.json'],
    thumbnailRef: 'projects/fixture/asset-previews/fixture/thumbnail.png',
    preview: { kind: 'publisher_render', systemRendered: false, sha256: 'a'.repeat(64) },
    image: { data: 'cGl4ZWxz', mimeType: 'image/png' },
    inputs: { dependencies: 'x'.repeat(200000) }, frozenDigests: { largeInventory: 'x'.repeat(200000) } });
  const completed = await f.call('job', { jobId: job.jobId, action: 'status' });
  assert.equal(completed.result.fileCount, 2);
  assert.equal(completed.result.reportPath, '/data/assets/prepared/digest/report.json');
  assert.equal(completed.result.inputs, undefined);
  assert.equal(completed.result.frozenDigests, undefined);
  assert.equal(completed.result.preview.kind, 'publisher_render');
  assert.equal(completed.result.preview.systemRendered, false);
  const imageResult = await f.tools.call('job', { ...f.scope, jobId: job.jobId, action: 'status' });
  assert.deepEqual(imageResult.content.find(row => row.type === 'image'),
    { type: 'image', data: 'cGl4ZWxz', mimeType: 'image/png' });
  assert.ok(Buffer.byteLength(JSON.stringify(completed)) < 4000);
  const snapshot = await f.store.sourceSnapshot(f.project.id);
  const resourceRefs = ['/data/assets/prepared/digest/report.json'];
  await f.call('scene_build', { requestId: 'native_build', expectedSourceHash: snapshot.sourceHash, resourceRefs });
  assert.deepEqual(f.requests.at(-1).task.resourceRefs, resourceRefs);
  await assert.rejects(f.call('assets_prepare', { ...args, requestId: 'mixed_native', assetId: 'old' }), /恰好一个/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'orphan_native_deps', assetId: 'old', dependencyFiles: ['/sdk/other'] }), /dependencyFiles/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'orphan_mdl_roots', assetId: 'old', mdlSearchRoots: ['/sdk/mdl'] }), /mdlSearchRoots/);
  await assert.rejects(f.call('assets_prepare', { ...args, requestId: 'relative_mdl_roots', mdlSearchRoots: ['mdl'] }), /mdlSearchRoots/);
  await assert.rejects(f.call('assets_prepare', { ...args, requestId: 'duplicate_mdl_roots', mdlSearchRoots: ['/sdk/mdl', '/sdk/mdl'] }), /mdlSearchRoots|重复/);
});

test('asset lookup carries actual matching filters and rejects invalid boolean values', async t => {
  const f = await fixture(t);
  const result = await f.call('assets_lookup', { query: '木质办公椅', category: 'chair',
    kind: 'model', textured: true, size: [.7, .7, 1] });
  assert.equal(result.unverifiedFilters?.includes('size') ?? false, false);
  const request = f.requests.at(-1);
  assert.equal(request.textured, true);
  assert.equal(request.category, 'chair');
  assert.deepEqual(request.size, [.7, .7, 1]);
  await assert.rejects(f.call('assets_lookup', { textured: 'yes' }), /布尔/);
});

test('asset lookup forwards an explicit library tier without selecting an asset for the generator', async t => {
  const f = await fixture(t);
  for (const libraryTier of ['preferred', 'broad', 'all']) {
    await f.call('assets_lookup', { query: 'bowl', libraryTier });
    assert.equal(f.requests.at(-1).libraryTier, libraryTier);
  }
  await assert.rejects(f.call('assets_lookup', { libraryTier: 'certified' }), /取值/);
});

test('asset lookup forwards collection and explicit joint filters and rejects invalid values before dispatch', async t => {
  const f = await fixture(t);
  for (const requiresJoints of [true, false]) {
    await f.call('assets_lookup', { query: 'cabinet', libraryTier: 'preferred',
      collection: 'future-preferred-source', requiresJoints });
    assert.equal(f.requests.at(-1).collection, 'future-preferred-source');
    assert.equal(f.requests.at(-1).requiresJoints, requiresJoints);
  }
  const before = f.requests.length;
  await assert.rejects(f.call('assets_lookup', { requiresJoints: 'yes' }), /布尔/);
  await assert.rejects(f.call('assets_lookup', { collection: 3 }), /有效文本/);
  await assert.rejects(f.call('assets_lookup', { collection: '' }), /有效文本/);
  assert.equal(f.requests.length, before);
});

test('durable write requests reserve before source awaits and deduplicate exact retries', async t => {
  const f = await fixture(t);
  const snapshot = await f.store.sourceSnapshot(f.project.id);
  const first = await f.call('scene_build', { expectedSourceHash: snapshot.sourceHash, requestId: 'same' });
  const repeat = await f.call('scene_build', { requestId: 'same', expectedSourceHash: snapshot.sourceHash });
  assert.equal(repeat.jobId, first.jobId);
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
  await assert.rejects(f.call('scene_build', { requestId: 'same', expectedSourceHash: snapshot.sourceHash, seed: 1 }), /输入|request/);
  await assert.rejects(f.call('scene_build', { requestId: 'other', expectedSourceHash: snapshot.sourceHash }), /作业|正在|busy/);
  const trace = f.project.events.filter(row => row.type === 'tool_call');
  assert.ok(trace.length);
  assert.ok(f.project.events.some(row => row.type === 'tool_result' && row.callEventId === trace[0].eventId));
});

test('a completed physical job with failed score never becomes accepted delivery', async t => {
  const f = await ready(t);
  const run = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'failed_run' });
  f.finish(run.jobId, { revision: 'rev_a', success: false, episodes: [{ episode: 'ep_bad', taskId: 'push',
    revision: 'rev_a', demonstrationEligible: false, score: { success: false } }] });
  const result = await f.call('job', { jobId: run.jobId, action: 'status' });
  assert.equal(result.status, 'completed');
  assert.equal(result.result.success, false);
  assert.notEqual(f.project.generation.status, 'complete');
  assert.equal(f.project.delivery, undefined);
});

test('scene observation wait forwards actual image pixels as MCP image content', async t => {
  const f = await ready(t);
  const observation = await f.call('scene_observe', { revision: 'rev_a', requestId: 'observe_a' });
  f.finish(observation.jobId, { revision: 'rev_a', artifactRef: 'observations/a.png', image: { data: 'cGl4ZWxz', mimeType: 'image/png' } });
  const response = await f.tools.call('job', { ...f.scope, jobId: observation.jobId, action: 'wait', waitMs: 0 });
  assert.deepEqual(response.content.find(row => row.type === 'image'), { type: 'image', data: 'cGl4ZWxz', mimeType: 'image/png' });
  assert.equal(response.structuredContent.result.image, undefined);
});

test('delivery requires all actual consumers and task coverage before persistent acceptance', async t => {
  const f = await ready(t);
  const pending = await f.deliver();
  assert.equal(pending.status, 'queued');
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(f.receipts.length, 0);
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts[0].accepted, true);
  assert.equal(f.receipts[0].products.generation.sampleCount, 2);
  assert.equal(Object.hasOwn(f.receipts[0].products.demonstrations, 'controlCycles'), false);
  assert.equal(f.receipts[0].products.demonstrations.batches[0].controlCycles, 20);
  assert.equal(f.receipts[0].revision, 'rev_a');
  assert.equal(f.receipts[0].requirementVersion, 1);
});

for (const cycles of [undefined, 0, -1, 1.5, '20', true, Number.MAX_SAFE_INTEGER + 1]) test('delivery rejects a nonpositive or invalid batch control count: ' + String(cycles), async t => {
  const f = await ready(t), pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.consumer.demonstrations.controlCycles = 20;
  f.consumer.demonstrations.batches[0].controlCycles = cycles;
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /机器人执行数据/);
});

for (const variant of ['empty', 'missing task']) test('one valid demonstration cannot hide another reviewed batch with ' + variant, async t => {
  const f = await ready(t, { readInternalReview: async (...args) => {
    const review = await f.readInternalReview(...args);
    return { ...review, episodes: [...review.episodes, { episodeId: 'ep_b', taskId: 'push', revision: 'rev_a', use: 'successful_demonstration' }] };
  } });
  f.project.episodes.push({ ...f.project.episodes[0], episode: 'ep_b' });
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.consumer.demonstrations.episodeCount = 2;
  f.consumer.demonstrations.controlCycles = 40;
  f.consumer.demonstrations.batches.push({ ...f.consumer.demonstrations.batches[0], episodeId: 'ep_b' });
  if (variant === 'empty') f.consumer.demonstrations.batches[1].controlCycles = 0;
  else delete f.consumer.demonstrations.batches[1].taskId;
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /机器人执行数据/);
});

test('delivery rejects an episode count that differs from actual demonstration batches', async t => {
  const f = await ready(t), pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.consumer.demonstrations.episodeCount = 2;
  f.consumer.demonstrations.controlCycles = 20;
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /机器人执行数据/);
});

test('missing consumer coverage and a late older requirement never accept a receipt', async t => {
  const f = await ready(t);
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.consumer.evaluation.tasks = [];
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts[0].accepted, false);
  assert.ok(f.receipts[0].missing.length);
  const second = await f.call('project_deliver', { revision: 'rev_a', requestId: 'deliver_b',
    expectedRequirementVersion: 1, requestHash: f.project.generation.requestHash });
  await f.call('job', { jobId: second.jobId, action: 'wait', waitMs: 1000 });
  await f.store.addRequirement(f.project.id, '再增加一张桌子');
  f.consumer.evaluation.tasks = [{ taskId: 'push', passed: true }];
  f.finish(second.jobId, f.consumer);
  await f.call('job', { jobId: second.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /需求|版本/);
});

test('source reservation covers the await and prevents a concurrent remote submission', async t => {
  const f = await fixture(t);
  const source = await f.store.sourceSnapshot(f.project.id);
  const original = f.store.sourceSnapshot.bind(f.store);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  f.store.sourceSnapshot = async (...args) => { entered(); await gate; return original(...args); };
  const first = f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'reserved_a' });
  await started;
  await assert.rejects(f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'reserved_b' }), /作业|正在/);
  assert.equal(f.requests.length, 0);
  release();
  const result = await first;
  f.finish(result.jobId, { projectId: f.project.id, revision: 'rev_a', tasks: [{ id: 'push' }] });
  await f.call('job', { jobId: result.jobId, action: 'status' });
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
});

test('ambiguous remote submission survives restart without submitting another job', async t => {
  const f = await fixture(t);
  f.tools.close();
  const tools = createTools({ store: f.store, remote: async payload => {
    const reply = await f.remote(payload);
    if (payload.operation === 'submit') throw new Error('lost SSH response');
    return reply;
  } });
  t.after(() => tools.close());
  const source = await f.store.sourceSnapshot(f.project.id);
  const args = { ...f.scope, requestId: 'uncertain', expectedSourceHash: source.sourceHash };
  const first = (await tools.call('scene_build', args)).structuredContent;
  assert.equal(first.status, 'unknown');
  f.finish(first.jobId, { projectId: f.project.id, revision: 'rev_a', tasks: [{ id: 'push' }] });
  tools.close();
  const restored = await new Store({ root: f.store.root }).load();
  const recovered = createTools({ store: restored, remote: f.remote });
  t.after(() => recovered.close());
  const reply = (await recovered.call('job', { ...f.scope, jobId: first.jobId, action: 'status' })).structuredContent;
  assert.equal(reply.status, 'completed');
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
});

test('missing receipt persistence never accepts consumer booleans in memory', async t => {
  const f = await ready(t, { onDelivery: undefined });
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.finish(pending.jobId, f.consumer);
  await assert.rejects(f.call('job', { jobId: pending.jobId, action: 'status' }), /持久化|交付/);
  assert.equal(f.project.jobs.at(-1).status, 'completed');
  assert.equal(f.project.delivery, undefined);
});


test('delivery returns its durable pending job while generation consumption is still preparing', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  let consumes = 0;
  const f = await ready(t, { consumeGeneration: async () => { consumes++; entered(); await gate; return { passed: true, sampleCount: 2 }; } });
  t.after(() => release());
  const reply = f.deliver(); await started;
  const pending = await Promise.race([reply, new Promise(resolve => setTimeout(() => resolve(null), 1000))]);
  assert.ok(pending, 'preparing generation consumption must not hold the tool HTTP response');
  assert.equal(pending.status, 'queued');
  assert.equal(f.project.jobs.at(-1).id, pending.jobId);
  const persisted = await new Store({ root: f.store.root }).load();
  assert.equal(persisted.get(f.project.id).jobs.at(-1).id, pending.jobId);
  assert.equal(persisted.get(f.project.id).jobs.at(-1).status, 'queued');
  assert.equal((await f.deliver()).jobId, pending.jobId);
  assert.equal(consumes, 1);
  await assert.rejects(f.call('scene_observe', { revision: 'rev_a', requestId: 'competing' }), /作业|正在/);
  assert.equal((await f.call('job', { jobId: pending.jobId, action: 'status' })).status, 'queued');
  assert.equal((await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 5 })).status, 'queued');
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
  release();
  const submitted = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(submitted.status, 'running');
  assert.equal(consumes, 1);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 1);
  assert.deepEqual(f.project.jobs.at(-1).payload.task.episodes, [{ episodeId: 'ep_a', taskId: 'push', use: 'successful_demonstration' }]);
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts[0].accepted, true);
});

test('a failed asynchronous generation consumer never submits physical consumers or accepts delivery', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); await gate; return { passed: false, sampleCount: 2 }; } });
  t.after(() => release());
  const reply = f.deliver(); await started;
  const pending = await Promise.race([reply, new Promise(resolve => setTimeout(() => resolve(null), 1000))]);
  assert.ok(pending, 'failed later work must have a persistent pending identity first');
  release();
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.status, 'failed');
  assert.match(result.error, /生成轨迹消费者未通过/);
  assert.equal(f.receipts.length, 0);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});

test('restart during unsent generation preparation preserves the interrupted job and never submits it again', async t => {
  let entered; const started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); return new Promise(() => {}); } });
  const reply = f.deliver(); await started;
  const pending = await Promise.race([reply, new Promise(resolve => setTimeout(() => resolve(null), 1000))]);
  assert.ok(pending, 'persist before returning a pending generation consumer');
  const store = await new Store({ root: f.store.root }).load();
  const project = store.get(f.project.id), job = project.jobs.at(-1);
  const tools = createTools({ store, remote: f.remote }); t.after(() => tools.close());
  const recovered = await tools.refreshJob(project, job);
  assert.equal(recovered.jobId, pending.jobId);
  assert.equal(recovered.status, 'failed');
  assert.match(recovered.error, /提交前.*中断/);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});

test('generation identity changes during preparation prevent a stale remote delivery submission', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); await gate; return { passed: true, sampleCount: 2 }; } });
  t.after(() => release());
  const reply = f.deliver(); await started;
  const pending = await Promise.race([reply, new Promise(resolve => setTimeout(() => resolve(null), 1000))]);
  assert.ok(pending);
  await f.store.addRequirement(f.project.id, '保留原要求，再核对新需求');
  release();
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.status, 'failed');
  assert.match(result.error, /需求|生成/);
  assert.equal(f.receipts.length, 0);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});


test('source edits during preparation stop the stale delivery before physical consumer submission', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); await gate; return { passed: true, sampleCount: 2 }; } });
  t.after(() => release());
  const pending = await f.deliver(); await started;
  await writeFile(path.join(f.store.directory(f.project.id), 'scene.py'), 'native changed source during preparation');
  release();
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.status, 'failed'); assert.match(result.error, /源码/);
  assert.equal(f.receipts.length, 0);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});


test('cancelling an unsent preparation cannot later submit physical consumers or rewrite cancellation as failure', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); await gate; return { passed: true, sampleCount: 2 }; } });
  t.after(() => release());
  const pending = await f.deliver(); await started;
  const cancelled = await f.call('job', { jobId: pending.jobId, action: 'cancel' });
  assert.equal(cancelled.status, 'cancelled');
  release(); await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(f.project.jobs.at(-1).status, 'cancelled');
  assert.equal(f.receipts.length, 0);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
  assert.equal(f.project.events.filter(row => row.type === 'operation_completed' && row.jobId === pending.jobId).length, 1);
});

test('requirements changing during the final source read cannot submit a stale prepared delivery', async t => {
  const f = await ready(t);
  const original = f.store.sourceSnapshot.bind(f.store);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  f.store.sourceSnapshot = async (id, cause) => { if (cause === 'delivery_prepared') { entered(); await gate; } return original(id, cause); };
  t.after(() => release());
  const pending = await f.deliver(); await started;
  await f.store.addRequirement(f.project.id, '保留原要求，追加一次核对');
  release();
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.status, 'failed'); assert.match(result.error, /需求/);
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 0);
});

test('cancellation during a hanging remote submit is retained on the same durable job and forwarded after submission', async t => {
  const f = await ready(t); f.tools.close();
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const tools = createTools({ store: f.store, syncTrace: async () => {}, readInternalReview: f.readInternalReview,
    consumeGeneration: async () => ({ passed: true, sampleCount: 2 }),
    remote: async payload => { if (payload.operation === 'submit') { entered(); await gate; } return f.remote(payload); } });
  t.after(() => { release(); tools.close(); });
  const call = async (name, args) => (await tools.call(name, { ...f.scope, ...(name === 'project_deliver' ? { review: f.reviewPointer } : {}), ...args })).structuredContent;
  const pending = await call('project_deliver', { revision: 'rev_a', requestId: 'cancel_submit',
    expectedRequirementVersion: 1, requestHash: f.project.generation.requestHash });
  await started;
  const cancel = await call('job', { jobId: pending.jobId, action: 'cancel' });
  assert.equal(cancel.cancellationPending, true);
  assert.notEqual(cancel.status, 'cancelled', 'remote cancellation requires actual confirmation');
  assert.equal(f.project.jobs.at(-1).cancelRequested, true);
  const persisted = await new Store({ root: f.store.root }).load();
  assert.equal(persisted.get(f.project.id).jobs.at(-1).cancelRequested, true);
  release();
  const result = await call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.jobId, pending.jobId); assert.equal(result.status, 'cancelled');
  assert.equal(f.requests.filter(row => row.operation === 'submit' && row.task.operation === 'consume').length, 1);
  assert.equal(f.requests.filter(row => row.operation === 'job' && row.action === 'cancel').length, 1);
});


for (const outcome of ['success', 'failure']) test(`cancelled unsent sync releases its own lease; late ${outcome} cannot release a newer source lease`, async t => {
  let releaseSync, rejectSync, enteredSync;
  const syncGate = new Promise((resolve, reject) => { releaseSync = resolve; rejectSync = reject; });
  const syncStarted = new Promise(resolve => { enteredSync = resolve; });
  let consumes = 0;
  const f = await ready(t, { syncTrace: async () => { enteredSync(); await syncGate; },
    consumeGeneration: async () => { consumes++; return { passed: true, sampleCount: 2 }; } });
  t.after(() => releaseSync());
  const old = await f.deliver(); await syncStarted;
  assert.equal((await f.call('job', { jobId: old.jobId, action: 'cancel' })).status, 'cancelled');
  const source = await f.store.sourceSnapshot(f.project.id);
  const original = f.store.sourceSnapshot.bind(f.store);
  let releaseSource, enteredSource;
  const sourceGate = new Promise(resolve => { releaseSource = resolve; });
  const sourceStarted = new Promise(resolve => { enteredSource = resolve; });
  f.store.sourceSnapshot = async (...args) => { enteredSource(); await sourceGate; return original(...args); };
  t.after(() => releaseSource());
  const next = f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'next_build' });
  next.catch(() => {});
  await Promise.race([sourceStarted, next]);
  if (outcome === 'success') releaseSync(); else rejectSync(new Error('late sync transport failure'));
  for (let n = 0; n < 100 && f.project.jobs.find(j => j.id === old.jobId).pendingPreparation; n++) await new Promise(resolve => setTimeout(resolve, 1));
  assert.equal(f.project.jobs.find(j => j.id === old.jobId).pendingPreparation, undefined);
  assert.equal(f.project.jobs.find(j => j.id === old.jobId).status, 'cancelled');
  assert.equal(consumes, 0);
  assert.equal(f.requests.filter(r => r.operation === 'submit' && r.task.operation === 'consume').length, 0);
  assert.equal(f.project.events.filter(e => e.type === 'operation_completed' && e.jobId === old.jobId).length, 1);
  await assert.rejects(f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'third_build' }), /作业|正在/);
  releaseSource(); const build = await next;
  assert.equal(build.status, 'running');
  assert.equal(f.requests.filter(r => r.operation === 'submit' && r.task.operation === 'build').length, 2);
});

test('cancellation during final source snapshot keeps the lease until the mutating source read settles', async t => {
  const f = await ready(t), original = f.store.sourceSnapshot.bind(f.store);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  f.store.sourceSnapshot = async (id, cause) => { if (cause === 'delivery_prepared') { entered(); await gate; } return original(id, cause); };
  t.after(() => release());
  const old = await f.deliver(); await started;
  await f.call('job', { jobId: old.jobId, action: 'cancel' });
  const source = await original(f.project.id);
  await assert.rejects(f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'while_old_read' }), /作业|正在/);
  release();
  for (let n = 0; n < 100 && f.project.jobs.find(j => j.id === old.jobId).pendingPreparation; n++) await new Promise(resolve => setTimeout(resolve, 1));
  assert.equal(f.project.jobs.find(j => j.id === old.jobId).pendingPreparation, undefined);
  assert.equal(f.project.jobs.find(j => j.id === old.jobId).status, 'cancelled');
  assert.equal(f.requests.filter(r => r.operation === 'submit' && r.task.operation === 'consume').length, 0);
  assert.equal((await f.call('scene_build', { expectedSourceHash: source.sourceHash, requestId: 'after_old_read' })).status, 'running');
});

// Regression: consumers alone cannot attest native independent scene review.
test('new delivery without a native independent review cannot be accepted', async t => {
  const f = await ready(t);
  let pending;
  try { pending = (await f.tools.call('project_deliver', { ...f.scope, revision: 'rev_a', requestId: 'no_review',
    expectedRequirementVersion: 1, requestHash: f.project.generation.requestHash })).structuredContent; } catch (error) {
    assert.match(error.message, /review|审核/); return;
  }
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  if (f.jobs.has(pending.jobId)) {
    f.finish(pending.jobId, f.consumer);
    await f.call('job', { jobId: pending.jobId, action: 'status' });
  }
  assert.notEqual(f.receipts.at(-1)?.accepted, true);
});

for (const variant of ['missing', 'duplicate', 'extra']) test('delivery rejects ' + variant + ' consumer episode set', async t => {
  const f = await ready(t), pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  if (variant === 'missing') f.consumer.demonstrations.batches = [];
  if (variant === 'duplicate') f.consumer.demonstrations.batches.push({ ...f.consumer.demonstrations.batches[0] });
  if (variant === 'extra') f.consumer.demonstrations.batches.push({ ...f.consumer.demonstrations.batches[0], episodeId: 'unreviewed' });
  f.finish(pending.jobId, f.consumer);
  await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /集合/);
});

test('adding an unreviewed same-revision episode during consumption never expands the submitted fixed set', async t => {
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; }), started = new Promise(resolve => { entered = resolve; });
  const f = await ready(t, { consumeGeneration: async () => { entered(); await gate; return { passed: true, sampleCount: 2 }; } });
  t.after(() => release());
  const pending = await f.deliver(); await started;
  f.project.episodes.push({ ...f.project.episodes[0], episode: 'ep_later' });
  release(); await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.deepEqual(f.project.jobs.at(-1).payload.task.episodes, [{ episodeId: 'ep_a', taskId: 'push', use: 'successful_demonstration' }]);
});

for (const phase of ['prepared', 'terminal']) test('fresh native read failure at ' + phase + ' never falls back to the prepared pass', async t => {
  const f = await ready(t, { readInternalReview: async (project, pointer, options) => {
    if (options.phase === phase) throw Error('native latest read failed');
    return f.readInternalReview(project, pointer, options);
  } });
  const pending = await f.deliver();
  const submitted = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  if (phase === 'terminal') {
    f.finish(pending.jobId, f.consumer); await f.call('job', { jobId: pending.jobId, action: 'status' });
    assert.equal(f.receipts.at(-1).accepted, false);
    assert.match(f.receipts.at(-1).missing.join(' '), /native latest read failed/);
  } else { assert.equal(submitted.status, 'failed'); assert.equal(f.receipts.length, 0); }
});

test('a historical completed delivery query preserves the original receipt without inventing internal review', async t => {
  const f = await ready(t);
  const receipt = { accepted: true, assessedBy: 'historical_consumer_scope' };
  const historical = { id: 'historical', durable: true, generationId: f.project.generation.id, status: 'completed', terminalApplied: true,
    terminalNotified: true, delivery: receipt };
  f.project.jobs.push(historical);
  assert.deepEqual((await f.call('job', { jobId: historical.id, action: 'status' })).delivery, receipt);
  assert.equal(historical.delivery.internalReview, undefined);
});

test('exact legacy project_deliver retry returns its immutable completed receipt without inventing review', async t => {
  const f = await ready(t);
  const args = { ...f.scope, revision: 'rev_a', requestId: 'legacy_delivery', expectedRequirementVersion: 1,
    requestHash: f.project.generation.requestHash };
  const canonicalArgs = Object.fromEntries(Object.keys(args).sort().map(key => [key, args[key]]));
  const inputHash = (await import('../src/store.mjs')).contentHash({ args: canonicalArgs, name: 'project_deliver' });
  const receipt = { accepted: true, assessedBy: 'historical_consumer_scope' };
  f.project.jobs.push({ id: 'legacy_delivery_job', name: 'project_deliver', requestId: args.requestId, inputHash,
    generationId: f.project.generation.id, status: 'completed', terminalApplied: true, terminalNotified: true, delivery: receipt });
  const result = (await f.tools.call('project_deliver', args)).structuredContent;
  assert.deepEqual(result.delivery, receipt);
  assert.equal(result.delivery.internalReview, undefined);
  await assert.rejects(f.tools.call('project_deliver', { ...args, requestId: 'new_request_without_review' }), /review|审核/);
});

test('stopped generations reject new computation but preserve exact historical receipts', async t => {
  for (const status of ['cancel_requested', 'cancelled', 'failed', 'recovering', 'complete']) {
    await t.test(status, async t => {
      const f = await fixture(t);
      const args = { requestId: 'before_stop', assetId: 'asset-1' };
      const prior = await f.call('assets_prepare', args);
      f.project.generation.status = status;
      if (status === 'recovering') f.project.generation.cancelRequestedAt = new Date().toISOString();
      assert.equal((await f.call('assets_prepare', args)).jobId, prior.jobId);
      await assert.rejects(f.call('assets_prepare', { ...args, requestId: 'after_stop' }), /停止|取消|终止/);
      assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
    });
  }
});

test('cancelling during online staging never submits or rewrites cancellation on late failure', async t => {
  for (const lateFailure of [false, true]) {
    await t.test(String(lateFailure), async t => {
      let entered, release;
      const started = new Promise(resolve => { entered = resolve; });
      const gate = new Promise(resolve => { release = resolve; });
      const f = await fixture(t, { stageOnlineAsset: async () => {
        entered(); await gate;
        if (lateFailure) throw new Error('late download failure');
        return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
      } });
      const pending = f.call('assets_prepare', { requestId: 'staging_cancel', url: 'https://example.com/chair.glb',
        sourcePage: 'https://example.com/chair', name: 'chair' });
      const settled = pending.catch(error => error);
      await started;
      const job = f.project.jobs[0];
      const cancelled = await f.call('job', { jobId: job.id, action: 'cancel' });
      release(); await settled;
      await f.call('job', { jobId: job.id, action: 'wait', waitMs: 1000 });
      assert.equal(cancelled.status, 'cancelled');
      assert.equal(job.status, 'cancelled');
      assert.equal(f.requests.filter(row => row.operation === 'submit').length, 0);
    });
  }
});

test('cancellation while persisting a prepared payload is checked immediately before transport', async t => {
  for (const phase of ['record', 'save']) {
    await t.test(phase, async t => {
      const f = await fixture(t);
      const original = f.store[phase].bind(f.store);
      let injected = false;
      f.store[phase] = async (...args) => {
        const result = await original(...args);
        if (!injected && f.project.jobs[0]?.payload && (phase === 'save' || args[1]?.type === 'operation_started')) {
          injected = true;
          f.project.generation.status = 'cancel_requested';
          f.project.generation.cancelRequestedAt = new Date().toISOString();
        }
        return result;
      };
      await assert.rejects(f.call('assets_prepare', { assetId: 'asset-1', requestId: 'persist_cancel' }), /停止|取消|终止/);
      assert.equal(injected, true);
      assert.equal(f.requests.filter(row => row.operation === 'submit').length, 0);
      assert.notEqual(f.project.jobs[0].status, 'unknown');
    });
  }
});

test('unimplemented public options are rejected rather than advertised or silently ignored', async t => {
  const f = await fixture(t);
  await assert.rejects(f.call('assets_lookup', { query: 'chair', capabilities: ['grasp'] }), /不支持参数/);
  await assert.rejects(f.call('assets_prepare', { requestId: 'dead_mode', assetId: 'a', mode: 'rigid' }), /不支持参数/);
  assert.equal(f.requests.length, 0);
});

test('retired fields in an exact historical request remain readable before new schema validation', async t => {
  const { contentHash } = await import('../src/store.mjs');
  const f = await fixture(t);
  const args = { ...f.scope, requestId: 'old_mode', assetId: 'a', mode: 'rigid' };
  const canonical = value => value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  f.project.jobs.push({ id: 'old_job', requestId: args.requestId, generationId: args.generationId,
    inputHash: contentHash(canonical({ name: 'assets_prepare', args })), name: 'assets_prepare',
    kind: 'prepare', status: 'completed', terminalApplied: true, terminalNotified: true });
  assert.equal((await f.call('assets_prepare', args)).jobId, 'old_job');
  await assert.rejects(f.call('assets_prepare', { ...args, mode: 'articulated' }), /输入|request/);
  await assert.rejects(f.call('robot_run', args), /输入|request/);
  assert.equal(f.requests.length, 0);
});


test('validation controllers select an explicit frozen case and digest without a kernel fallback', async t => {
  const f = await ready(t);
  const build = f.project.jobs.find(row => row.kind === 'build').result;
  build.tasks[0].evaluation = { cases: [
    { id: 'reach', sourceRef: 'policies/reach.py', helperRefs: [], expected: { success: true } },
    { id: 'miss', sourceRef: 'policies/miss.py', helperRefs: [], expected: { success: false } }] };
  build.sourceDigests = { 'policies/reach.py': 'a'.repeat(64), 'policies/miss.py': 'b'.repeat(64) };
  for (const controller of [{ type: 'policy', policyRef: 'expert.py' },
    { type: 'policy', policyRef: 'kernel_frozen_feedback' }, { type: 'policy', digest: 'wrong' }]) {
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'],
      requestId: 'bad_policy_' + f.project.jobs.length, controller }), /来源|摘要|policyRef/);
  }
  await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'case_wrong_purpose',
    controller: { type: 'policy', policyRef: 'policies/reach.py', digest: 'a'.repeat(64) } }), /verification/);
  const result = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'frozen_policy', purpose: 'verification',
    controller: { type: 'policy', policyRef: 'policies/reach.py', digest: 'a'.repeat(64) } });
  assert.equal(result.status, 'running');
  assert.equal(f.requests.at(-1).task.controller.policyRef, 'policies/reach.py');
});

for (const boundary of ['review', 'snapshot']) test('cancellation during terminal ' + boundary + ' prevents accepted delivery', async t => {
  const f = await ready(t, { readInternalReview: async (project, pointer, options) => {
    const result = await f.readInternalReview(project, pointer, options);
    if (boundary === 'review' && options.phase === 'terminal') project.generation.cancelRequestedAt = new Date().toISOString();
    return result;
  } });
  const snapshot = f.store.sourceSnapshot.bind(f.store);
  f.store.sourceSnapshot = async (id, cause) => {
    const result = await snapshot(id, cause);
    if (boundary === 'snapshot' && cause === 'delivery_final_boundary') f.project.generation.cancelRequestedAt = new Date().toISOString();
    return result;
  };
  const job = await f.deliver();
  await f.call('job', { jobId: job.jobId, action: 'wait', waitMs: 1000 });
  f.finish(job.jobId, f.consumer);
  await f.call('job', { jobId: job.jobId, action: 'status' });
  assert.equal(f.receipts.at(-1).accepted, false);
  assert.match(f.receipts.at(-1).missing.join(' '), /停止/);
});

test('an aborted receipt publication is applied once and repeated refresh never rewrites its immutable candidate', async t => {
  let publications = 0;
  const f = await ready(t, { onDelivery: async (_project, receipt) => {
    publications++;
    return { ...receipt, accepted: false, status: 'incomplete', publicationAborted: true,
      missing: ['取消发生于不可变回执写出后'], candidateArtifactRef: 'historical-candidate.json' };
  } });
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.finish(pending.jobId, f.consumer);
  for (let i = 0; i < 3; i++) await f.call('job', { jobId: pending.jobId, action: 'status' });
  const job = f.project.jobs.at(-1);
  assert.equal(publications, 1);
  assert.equal(job.delivery.accepted, false); assert.equal(job.delivery.publicationAborted, true);
  assert.equal(job.terminalApplied, true); assert.equal(job.terminalNotified, true);
});


test('observation camera scope is checked before submitting a remote operation', async t => {
  const f=await ready(t);
  f.project.jobs.find(row=>row.kind==='build').result.tasks[0].recording={primaryCameraId:'front',cameras:[{cameraId:'front',required:true}]};
  f.project.episodes[0].recording={primaryCameraId:'front',cameras:[{cameraId:'front',required:true}]};
  await assert.rejects(f.call('scene_observe',{revision:'rev_a',cameraId:'missing',requestId:'missing_camera'}),/相机/);
  await assert.rejects(f.call('scene_observe',{revision:'rev_a',cameraId:'front',nodeId:'room',requestId:'mixed_scope'}),/相机|范围/);
  assert.equal(f.requests.some(row=>row.task?.operation==='observe'),false);
  const observed=await f.call('scene_observe',{revision:'rev_a',episodeId:'ep_a',cameraId:'front',frame:0,requestId:'front_frame'});
  assert.equal(observed.kind,'observe');
  assert.equal(f.requests.at(-1).task.cameraId,'front');
  assert.equal(f.requests.at(-1).task.episode,'ep_a');
});

test('task checking preserves single and plural episode parameters while observation alone renames the single field', async t => {
  for (const episodes of [{ episodeId: 'ep_a' }, { episodeIds: ['ep_a'] }, { episodeId: 'ep_a', episodeIds: ['ep_a'] }]) {
    const f = await ready(t);
    await f.call('task_check', { revision: 'rev_a', requestId: 'check_alias', ...episodes });
    const task = f.requests.at(-1).task;
    for (const [key, value] of Object.entries(episodes)) assert.deepEqual(task[key], value);
    assert.equal(Object.hasOwn(task, 'episode'), false);
  }
});

test('shared validation sources select a frozen case explicitly without weakening per-task provenance', async t => {
  const f = await ready(t), build = f.project.jobs.find(row => row.kind === 'build').result;
  build.tasks[0].evaluation.cases = ['first', 'second'].map(id => ({ id, sourceRef: 'case.py', helperRefs: [], expected: { success: false } }));
  const input = { revision: 'rev_a', taskIds: ['push'], purpose: 'verification',
    controller: { type: 'policy', policyRef: 'case.py', digest: build.sourceDigests['case.py'] } };
  await assert.rejects(f.call('robot_run', { ...input, requestId: 'ambiguous_case' }), /caseId|明确/);
  for (const controller of [{ ...input.controller, caseId: 'missing' }, { ...input.controller, caseId: 'first', digest: 'wrong' },
    { type: 'expert', caseId: 'first' }]) {
    await assert.rejects(f.call('robot_run', { ...input, requestId: 'bad_case_' + f.project.jobs.length, controller }), /caseId|明确|摘要|专家/);
  }
  const selected = await f.call('robot_run', { ...input, requestId: 'chosen_case', controller: { ...input.controller, caseId: 'second' } });
  assert.equal(selected.status, 'running');
  assert.equal(f.requests.at(-1).task.controller.caseId, 'second');
});

test('retired changedNodeIds rejects new requests after exact original input and tool lookup', async t => {
  for (const name of ['scene_build', 'task_check']) {
    const f = await fixture(t);
    const args = { ...f.scope, requestId: 'historical_hints', changedNodeIds: ['old-node'],
      ...(name === 'scene_build' ? { expectedSourceHash: 'old-hash' } : { revision: 'old-revision' }) };
    const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    f.project.jobs.push({ id: 'old_job', requestId: args.requestId, generationId: f.scope.generationId,
      inputHash: contentHash(canonical({ name, args })), name, status: 'completed', terminalApplied: true, terminalNotified: true });
    assert.equal((await f.tools.call(name, args)).structuredContent.jobId, 'old_job');
    await assert.rejects(f.tools.call(name, { ...args, changedNodeIds: ['changed'] }), /输入/);
    await assert.rejects(f.tools.call(name, { ...args, requestId: 'fresh' }), /changedNodeIds.*退役|退役.*changedNodeIds/);
    assert.equal(f.requests.length, 0);
  }
});

test('online preparation returns a persisted job before staging and retries never stage twice', async t => {
  let entered, release, stages = 0, signal;
  const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async (_url, options) => {
    stages++; signal = options?.signal; entered(); await gate;
    return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
  } });
  t.after(release);
  const args = { requestId: 'async_online', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' };
  const calling = f.call('assets_prepare', args); await started;
  const first = await Promise.race([calling, new Promise(resolve => setTimeout(() => resolve(null), 50))]);
  const persisted = JSON.parse(await (await import('node:fs/promises')).readFile(f.store.stateFile, 'utf8'))[0].jobs[0];
  release(); await calling;
  assert.ok(first, 'the admitted job must return before online IO settles');
  assert.equal(first.status, 'queued'); assert.equal(first.jobId, persisted.id);
  assert.equal(persisted.preparationPhase, 'online_stage'); assert.ok(signal instanceof AbortSignal);
  await f.call('job', { jobId: first.jobId, action: 'wait', waitMs: 1000 });
  assert.equal((await f.call('assets_prepare', args)).jobId, first.jobId); assert.equal(stages, 1);
  await assert.rejects(f.call('assets_prepare', { ...args, url: 'https://example.com/other.glb' }), /输入/);
});

test('online cancellation aborts local staging but confirms only that no compute is submitted', async t => {
  let entered, release, signal;
  const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async (_url, options) => {
    signal = options?.signal; entered(); await gate;
    throw Object.assign(new Error('staging SSH exit not confirmed'), { exitUnconfirmed: true });
  } });
  t.after(release);
  const calling = f.call('assets_prepare', { requestId: 'cancel_online', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' }).catch(error => error);
  await started;
  const job = f.project.jobs[0];
  const cancelled = await f.call('job', { jobId: job.id, action: 'cancel' });
  release(); await calling;
  await f.call('job', { jobId: job.id, action: 'wait', waitMs: 1000 });
  assert.equal(signal?.aborted, true); assert.equal(cancelled.status, 'cancelled');
  assert.match(cancelled.error, /未提交.*计算/);
  assert.equal(job.status, 'cancelled'); assert.match(job.applicationError, /SSH exit not confirmed/);
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 0);
});

test('restart with a persisted online submission intent queries only the original job and marks uncertainty explicitly', async t => {
  const f = await fixture(t);
  const job = { id: 'j_intent', requestId: 'intent', generationId: f.scope.generationId, kind: 'prepare', durable: true,
    status: 'queued', pendingPreparation: 'online_asset', preparationPhase: 'online_stage',
    payload: { operation: 'submit', task: { operation: 'prepare', importRef: { digest: 'a'.repeat(64) } } } };
  f.project.jobs.push(job);
  await assert.rejects(f.tools.refreshJob(f.project, job), /job not found/);
  assert.equal(job.status, 'unknown'); assert.match(job.transportError, /job not found/);
  assert.deepEqual(f.requests.map(row => [row.operation, row.jobId]), [['job', 'j_intent']]);
});

test('tools close aborts only local preparations and bounds an uncooperative staging callback', { timeout: 6000 }, async t => {
  let entered, release, signal;
  const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async (_url, options) => {
    signal = options?.signal; entered(); await gate;
    return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
  } });
  t.after(release);
  const calling = f.call('assets_prepare', { requestId: 'closing_online', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' }).catch(error => error);
  await started;
  const closing = f.tools.close(); assert.ok(closing instanceof Promise);
  await closing;
  const job = f.project.jobs[0];
  assert.equal(signal?.aborted, true); assert.equal(job.status, 'failed');
  assert.match(job.error, /关闭|中断/); assert.match(job.applicationError, /未确认/);
  release(); await calling;
  await f.call('job', { jobId: job.id, action: 'wait', waitMs: 1000 });
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 0);
});

test('online submit cancellation stays pending and tools close never waits for the remote job', async t => {
  let submitted, release, signal;
  const sending = new Promise(resolve => { submitted = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async (_url, options) => {
    signal = options.signal; return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
  }, remote: async payload => {
    if (payload.operation === 'submit') { submitted(); await gate; }
    return f.remote(payload);
  } });
  t.after(release);
  const pending = await f.call('assets_prepare', { requestId: 'submit_cancel', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
  await sending;
  const cancel = await f.call('job', { jobId: pending.jobId, action: 'cancel' });
  assert.equal(cancel.status, 'queued'); assert.equal(cancel.cancellationPending, true); assert.equal(signal.aborted, false);
  const closed = await Promise.race([f.tools.close().then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 50))]);
  assert.equal(closed, true, 'a dispatched remote computation must not be part of local cleanup');
  release();
  const ended = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(ended.status, 'cancelled');
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
  assert.equal(f.requests.filter(row => row.operation === 'job' && row.action === 'cancel').length, 1);
});

test('changed requirements during online staging fail the original attempt without submitting stale work', async t => {
  let entered, release;
  const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async () => {
    entered(); await gate; return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
  } });
  t.after(release);
  const pending = await f.call('assets_prepare', { requestId: 'stale_online', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
  await started; await f.store.addRequirement(f.project.id, 'SYSTEM_VERIFICATION_ONLY changed requirement'); release();
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(result.status, 'failed'); assert.match(result.error, /需求/);
  assert.equal(f.requests.filter(row => row.operation === 'submit').length, 0);
});

test('online cancellation late cleanup cannot release a newer source reservation', async t => {
  let entered, release;
  const started = new Promise(resolve => { entered = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { stageOnlineAsset: async () => {
    entered(); await gate; return { digest: 'a'.repeat(64), retrievalDigest: 'b'.repeat(64) };
  } });
  t.after(release);
  const old = await f.call('assets_prepare', { requestId: 'old_online', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
  await started; await f.call('job', { jobId: old.jobId, action: 'cancel' });
  const snapshot = f.store.sourceSnapshot.bind(f.store), source = await snapshot(f.project.id);
  let reading, allowRead;
  const blocked = new Promise(resolve => { reading = resolve; }), readGate = new Promise(resolve => { allowRead = resolve; });
  t.after(allowRead);
  f.store.sourceSnapshot = async (...args) => { reading(); await readGate; return snapshot(...args); };
  const next = f.call('scene_build', { requestId: 'new_build', expectedSourceHash: source.sourceHash });
  await blocked; release(); await f.call('job', { jobId: old.jobId, action: 'wait', waitMs: 1000 });
  await assert.rejects(f.call('assets_prepare', { requestId: 'blocked_third', assetId: 'other' }), /正在执行/);
  allowRead(); await next;
});

test('a case selector must match every selected frozen task', async t => {
  const f = await ready(t), build = f.project.jobs.find(row => row.kind === 'build').result;
  const task = build.tasks[0];
  build.tasks.push({ ...task, id: 'other', evaluation: { cases: [{ ...task.evaluation.cases[0], id: 'other_case' }] } });
  const before = f.requests.length;
  await assert.rejects(f.call('robot_run', { revision: 'rev_a', requestId: 'batch_case', taskIds: ['push', 'other'], purpose: 'verification',
    controller: { type: 'policy', policyRef: 'case.py', caseId: 'case_a', digest: build.sourceDigests['case.py'] } }), /caseId|明确/);
  assert.equal(f.requests.length, before);
});

test('online cancellation before a saved payload is sent never queries an imaginary remote job', async t => {
  const f = await fixture(t);
  const save = f.store.save.bind(f.store);
  let cancelled = false;
  f.store.save = async () => {
    await save();
    const job = f.project.jobs[0];
    if (!cancelled && job?.payload && job.preparationPhase === 'online_stage') {
      cancelled = true;
      await f.tools.refreshJob(f.project, job, { action: 'cancel' });
    }
  };
  const pending = await f.call('assets_prepare', { requestId: 'cancel_saved_intent', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
  const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.equal(cancelled, true); assert.equal(result.status, 'cancelled');
  assert.equal(f.requests.length, 0);
  assert.equal((await f.call('job', { jobId: pending.jobId, action: 'status' })).status, 'cancelled');
  assert.equal(f.requests.length, 0);
});

test('online cancel and concurrent status or wait apply one terminal receipt and notification', async t => {
  for (const action of ['status', 'wait']) await t.test(action, async t => {
    let staged, releaseRecord, writing, notifications = 0, terminalWrites = 0;
    const staging = new Promise(resolve => { staged = resolve; });
    const blocked = new Promise(resolve => { writing = resolve; }), recordGate = new Promise(resolve => { releaseRecord = resolve; });
    const f = await fixture(t, { stageOnlineAsset: async (_url, { signal }) => {
      staged(); return new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
    }, onJobTerminal: async () => { notifications++; } });
    t.after(releaseRecord);
    const pending = await f.call('assets_prepare', { requestId: 'terminal_race', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
    await staging;
    const record = f.store.record.bind(f.store);
    f.store.record = async (id, event) => {
      if (event.type === 'operation_completed') {
        terminalWrites++;
        if (terminalWrites === 1) { writing(); await recordGate; }
      }
      return record(id, event);
    };
    const job = f.project.jobs[0];
    const cancel = f.tools.refreshJob(f.project, job, { action: 'cancel' }); await blocked;
    const query = f.tools.refreshJob(f.project, job, { action, waitMs: 0 });
    releaseRecord();
    const replies = await Promise.all([cancel, query]);
    assert.ok(replies.every(row => row.status === 'cancelled' && row.cancellationPending === false));
    assert.equal(terminalWrites, 1); assert.equal(notifications, 1);
    assert.equal(f.project.events.filter(row => row.type === 'operation_completed' && row.jobId === pending.jobId).length, 1);
  });
});

test('failed terminal notification is shared concurrently and retries only its unfinished stage', async t => {
  let notifying, release, notifications = 0;
  const entered = new Promise(resolve => { notifying = resolve; }), gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { onJobTerminal: async () => {
    notifications++;
    if (notifications === 1) { notifying(); await gate; throw new Error('notification fixture failed'); }
  } });
  t.after(release);
  const job = { id: 'j_notify_retry', generationId: f.scope.generationId, kind: 'prepare', status: 'failed', error: 'fixture failure' };
  f.project.jobs.push(job);
  const first = f.tools.refreshJob(f.project, job).catch(error => error); await entered;
  const concurrent = f.tools.refreshJob(f.project, job, { action: 'wait', waitMs: 0 }).catch(error => error);
  release();
  const errors = await Promise.all([first, concurrent]);
  assert.ok(errors.every(error => error instanceof Error && /notification fixture failed/.test(error.message)));
  assert.equal(notifications, 1); assert.equal(job.terminalApplied, true); assert.notEqual(job.terminalNotified, true);
  await Promise.all([f.tools.refreshJob(f.project, job), f.tools.refreshJob(f.project, job)]);
  assert.equal(notifications, 2); assert.equal(job.terminalNotified, true);
  assert.equal(f.project.events.filter(row => row.type === 'operation_completed' && row.jobId === job.id).length, 1);
});

test('terminal application for a different job does not wait on another job receipt', async t => {
  const f = await fixture(t);
  const jobs = ['blocked', 'independent'].map(id => ({ id: 'j_' + id, generationId: f.scope.generationId, kind: 'prepare', status: 'failed' }));
  f.project.jobs.push(...jobs);
  let writing, release;
  const entered = new Promise(resolve => { writing = resolve; }), gate = new Promise(resolve => { release = resolve; });
  t.after(release);
  const record = f.store.record.bind(f.store);
  f.store.record = async (id, event) => {
    if (event.type === 'operation_completed' && event.jobId === jobs[0].id) { writing(); await gate; }
    return record(id, event);
  };
  const first = f.tools.refreshJob(f.project, jobs[0]); await entered;
  const second = f.tools.refreshJob(f.project, jobs[1]);
  const independent = await Promise.race([second, new Promise(resolve => setTimeout(() => resolve(null), 100))]);
  release(); await Promise.all([first, second]);
  assert.equal(independent?.jobId, jobs[1].id);
});

test('a terminal submit receipt settles cancellation without rewriting its actual remote outcome', async t => {
  for (const state of ['completed', 'failed', 'cancelled', 'running', 'unknown']) await t.test(state, async t => {
    let sending, release;
    const entered = new Promise(resolve => { sending = resolve; }), gate = new Promise(resolve => { release = resolve; });
    const f = await fixture(t, { remote: async payload => {
      if (payload.operation === 'submit') { sending(); await gate; }
      f.requests.push(payload);
      return { jobId: payload.jobId, state, events: [], cursor: 0,
        result: state === 'completed' ? { projectId: f.project.id, fixture: true } : null,
        error: state === 'failed' ? 'remote fixture failed' : null };
    } });
    t.after(release);
    const pending = await f.call('assets_prepare', { requestId: 'terminal_cancel', url: 'https://example.com/model.glb', sourcePage: 'https://example.com/model', name: 'fixture' });
    await entered;
    assert.equal((await f.call('job', { jobId: pending.jobId, action: 'cancel' })).cancellationPending, true);
    release();
    const result = await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
    const isTerminal = ['completed', 'failed', 'cancelled'].includes(state);
    assert.equal(result.status, state); assert.equal(result.cancellationPending, !isTerminal);
    const persisted = JSON.parse(await (await import('node:fs/promises')).readFile(f.store.stateFile, 'utf8'))[0].jobs[0];
    assert.equal(Boolean(persisted.cancelRequested), !isTerminal);
    assert.equal(f.requests.filter(row => row.operation === 'submit').length, 1);
  });
});

test('an already applied terminal receipt clears a stale cancellation flag without another receipt', async t => {
  let notifications = 0;
  const f = await fixture(t, { onJobTerminal: async () => { notifications++; } });
  const job = { id: 'j_old_terminal', generationId: f.scope.generationId, kind: 'prepare', status: 'completed',
    terminalApplied: true, terminalNotified: true, cancelRequested: true };
  f.project.jobs.push(job);
  assert.equal((await f.tools.refreshJob(f.project, job)).cancellationPending, false);
  assert.equal(notifications, 0); assert.equal(f.project.events.filter(row => row.type === 'operation_completed').length, 0);
  const persisted = JSON.parse(await (await import('node:fs/promises')).readFile(f.store.stateFile, 'utf8'))[0].jobs[0];
  assert.equal(Boolean(persisted.cancelRequested), false);
});

test('explicit environment sources are forwarded without requiring an expert source selection', async t => {
  const f = await ready(t);
  const source = await f.store.sourceSnapshot(f.project.id);
  await f.call('scene_build', { requestId: 'scene_closure', expectedSourceHash: source.sourceHash,
    environmentSourceRefs: ['scene.py', 'tasks.py', 'rubrics.py', 'case.py'] });
  assert.deepEqual(f.requests.at(-1).task.environmentSourceRefs, ['scene.py', 'tasks.py', 'rubrics.py', 'case.py']);
});

test('independent behavior freezes only its declared current files while keeping the environment revision', async t => {
  const f = await ready(t);
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = { independentBehavior: true, stageSplit: true };
  await writeFile(path.join(f.store.directory(f.project.id), 'control_helper.py'), 'VALUE = 9\n');
  await writeFile(path.join(f.store.directory(f.project.id), 'unrelated.py'), 'PRIVATE = 4\n');
  const run = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'independent_behavior',
    repetitions: 2, behavior: { sourceRef: 'expert.py', helperRefs: ['control_helper.py'] } });
  assert.equal(run.revision, 'rev_a');
  const payload = f.requests.at(-1).task;
  assert.equal(payload.repetitions, 2);
  assert.deepEqual(Object.keys(payload.behavior.sourceFiles).sort(), ['control_helper.py', 'expert.py']);
  assert.equal(payload.behavior.sourceFiles['control_helper.py'], 'VALUE = 9\n');
  assert.equal(payload.behavior.sourceDigests['control_helper.py'], contentHash('VALUE = 9\n'));
  assert.equal(f.project.jobs.filter(row => row.kind === 'build').length, 1);
});

test('scene-only work rejects new expert and independent behavior runs before any remote submission', async t => {
  for (const selection of [{}, { controller: { type: 'expert' } }, { purpose: 'verification' }, { purpose: 'probe' },
    { behavior: { sourceRef: 'expert.py', helperRefs: [] } },
    { purpose: 'verification', controller: { type: 'policy', policyRef: 'case.py', digest: 'a'.repeat(64) },
      behavior: { sourceRef: 'expert.py', helperRefs: [] } }]) await t.test(JSON.stringify(selection), async t => {
    const f = await ready(t);
    f.project.generation.requiredOutputs = ['generation', 'evaluation'];
    f.project.jobs.find(row => row.kind === 'build').result.capabilities = { stageSplit: true, independentBehavior: true };
    const count = f.requests.length, jobs = f.project.jobs.length;
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'unrequested_expert', ...selection }), /本次.*范围|纯场景/);
    assert.equal(f.requests.length, count);
    assert.equal(f.project.jobs.length, jobs);
  });
});

test('scene-only work runs the selected frozen validation case without requiring an expert success', async t => {
  const f = await ready(t);
  f.project.generation.requiredOutputs = ['generation', 'evaluation'];
  const locked = f.project.jobs.find(row => row.kind === 'build').result;
  const run = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'environment_case', purpose: 'verification',
    controller: { type: 'policy', policyRef: 'case.py', digest: locked.sourceDigests['case.py'] } });
  assert.equal(f.requests.at(-1).task.controller.type, 'policy');
  assert.equal(f.requests.at(-1).task.purpose, 'verification');
  f.finish(run.jobId, { projectId: f.project.id, revision: 'rev_a', episodes: [{ episode: 'case_only', taskId: 'push',
    revision: 'rev_a', controllerKind: 'case', purpose: 'verification', demonstrationEligible: false, score: { success: false } }] });
  assert.equal((await f.call('job', { jobId: run.jobId, action: 'status' })).status, 'completed');
  assert.equal(f.project.episodes.at(-1).demonstrationEligible, false);
  const count = f.requests.length;
  assert.equal((await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'run_a' })).status, 'completed');
  assert.equal(f.requests.length, count, 'an exact historical expert receipt must remain readable');
});

test('robot-only collection cannot rebuild its selected immutable environment', async t => {
  const f = await ready(t);
  f.project.latestBuild.capabilities = { stageSplit: true, independentBehavior: true };
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = f.project.latestBuild.capabilities;
  const locked = structuredClone(f.project.latestBuild);
  f.project.generation.status = 'complete';
  f.store.beginGeneration(f.project.id, { requiredOutputs: ['demonstrations'], environmentRefs: [{ revision: 'rev_a' }] });
  f.scope.generationId = f.project.generation.id;
  const source = await f.store.sourceSnapshot(f.project.id), count = f.requests.length;
  await assert.rejects(f.call('scene_build', { requestId: 'collection_rebuild', expectedSourceHash: source.sourceHash }), /只读.*环境|不能重新构建/);
  assert.equal(f.requests.length, count);
  assert.deepEqual(f.project.latestBuild, locked);
});

test('scene and environment delivery accepts no demonstration products or selected demonstration episodes', async t => {
  const f = await produced(t, { readInternalReview: async (...args) => {
    const value = await f.readInternalReview(...args); return { ...value, episodes: [] };
  }, consumeGeneration: async () => ({ passed: true, sampleCount: 2 }), consumeProducedArtifacts: async (...args) => {
    const result = await f.producedConsumer(...args); delete result.products.robot; return result;
  } });
  f.project.generation.requiredOutputs = ['generation', 'evaluation'];
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = { stageSplit: true };
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const consumer = structuredClone(f.consumer);
  consumer.requiredOutputs = ['generation', 'evaluation'];
  consumer.environmentRefs = [{ revision: 'rev_a' }];
  delete consumer.demonstrations;
  consumer.evaluation.tasks[0].revision = 'rev_a';
  const row = consumer.evaluation.interfaces[0]; row.revision = 'rev_a'; row.sourceEpisodeId = 'validation_ep';
  for (const key of ['recordRef', 'episodeRef', 'sourceRef']) row[key] = row[key].replace('/interfaces/push/', '/interfaces/rev_a/push/');
  f.finish(pending.jobId, consumer);
  const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(done.delivery.accepted, true, done.delivery.missing.join('; '));
  assert.deepEqual(Object.keys(done.delivery.products), ['generation', 'evaluation']);
  assert.deepEqual(f.requests.findLast(row => row.operation === 'submit').task.episodes, []);
});

async function sceneDeliveryTasks(t, taskSets) {
  let f;
  f = await produced(t, { readInternalReview: async (...args) => {
    const value = await f.readInternalReview(...args); return { ...value, episodes: [] };
  }, consumeProducedArtifacts: async (...args) => {
    const result = await f.producedConsumer(...args); delete result.products.robot; return result;
  } });
  f.project.generation.requiredOutputs = ['generation', 'evaluation'];
  const originalBuild = structuredClone(f.project.jobs.find(row => row.kind === 'build'));
  const originalCheck = structuredClone(f.project.jobs.find(row => row.kind === 'check'));
  for (const [index, ids] of taskSets.entries()) {
    const revision = 'rev_' + String.fromCharCode(97 + index);
    const build = index ? structuredClone(originalBuild) : f.project.jobs.find(row => row.kind === 'build');
    const check = index ? structuredClone(originalCheck) : f.project.jobs.find(row => row.kind === 'check');
    build.result.revision = revision; build.result.capabilities = { stageSplit: true };
    build.result.tasks = ids.map(id => ({ ...structuredClone(originalBuild.result.tasks[0]), id }));
    check.result.revision = revision;
    check.result.tasks = ids.map(id => ({ ...structuredClone(originalCheck.result.tasks[0]), taskId: id,
      cases: [{ ...originalCheck.result.tasks[0].cases[0], episodeId: 'validation_' + revision + '_' + id }] }));
    if (index) { build.id = 'build_' + index; check.id = 'check_' + index; f.project.jobs.push(build, check); }
  }
  return f;
}

test('delivery preserves frozen-runtime group scope and accepts only its exact native interface roots', async t => {
  for (const corruption of [null, 'foreign root', 'wrong runtime', 'missing revision', 'duplicate revision', 'wrong digest']) {
    await t.test(String(corruption), async t => {
      const f = await sceneDeliveryTasks(t, [['push']]);
      const snapshot = 'runtime/'+'a'.repeat(64), execution = { python:'/frozen/bin/python',prefix:'/frozen' };
      Object.assign(f.project.jobs.find(row => row.kind === 'build').result,
        { runtimeSnapshotRef:snapshot,runtimeExecution:execution,runtime:'frozen-key' });
      const refs = [{ revision:'rev_a',taskIds:['push'] }];
      const pending = await f.deliver({ environmentRefs:refs });
      await f.call('job', { jobId:pending.jobId,action:'wait',waitMs:1000 });
      const consumer = structuredClone(f.consumer);
      Object.assign(consumer,{requiredOutputs:['generation','evaluation'],environmentRefs:refs});
      delete consumer.demonstrations;
      consumer.evaluation.tasks[0].revision = 'rev_a';
      const { createHash } = await import('node:crypto');
      const group = 'runtime_'+createHash('sha256').update('deliver_a\n'+snapshot+'\n'+execution.python).digest('hex');
      const prefix = `projects/${f.project.id}/consumer-reports/`;
      consumer.runtimeGroups = [{ runtimeSnapshotRef:snapshot,runtimeExecution:execution,runtime:'frozen-key',
        environmentRefs:refs,artifactRef:prefix+group+'/report.json',sha256:'b'.repeat(64) }];
      const row=consumer.evaluation.interfaces[0];row.revision='rev_a';row.sourceEpisodeId='validation_rev_a_push';
      for (const key of ['recordRef','episodeRef','sourceRef']) row[key]=row[key]
        .replace('/deliver_a/interfaces/push/','/'+group+'/interfaces/rev_a/push/');
      if (corruption==='foreign root') row.recordRef=prefix+'foreign/interfaces/rev_a/push/records.json';
      if (corruption==='wrong runtime') consumer.runtimeGroups[0].runtimeSnapshotRef='runtime/'+'c'.repeat(64);
      if (corruption==='missing revision') consumer.runtimeGroups[0].environmentRefs=[];
      if (corruption==='duplicate revision') consumer.runtimeGroups.push(structuredClone(consumer.runtimeGroups[0]));
      if (corruption==='wrong digest') consumer.runtimeGroups[0].sha256='invalid';
      f.finish(pending.jobId,consumer);
      const done=await f.call('job',{jobId:pending.jobId,action:'status'});
      assert.equal(done.delivery.accepted,corruption===null,done.delivery.missing.join('; '));
    });
  }
});

test('selected delivery admits different frozen source versions using their original build inputs', async t => {
  const f = await sceneDeliveryTasks(t, [['push'], ['push']]);
  const build = f.project.jobs.find(row => row.kind === 'build' && row.result.revision === 'rev_b');
  build.payload.task.sourceFiles['tasks.py'] += '\n# isolated later camera recording configuration\n';
  build.sourceHash = build.payload.task.sourceHash = contentHash(build.payload.task.sourceFiles);
  build.result.sourceDigests['tasks.py'] = contentHash(build.payload.task.sourceFiles['tasks.py']);
  const refs = [{ revision: 'rev_a' }, { revision: 'rev_b' }];
  const pending = await f.deliver({ environmentRefs: refs });
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  assert.deepEqual(f.requests.findLast(row => row.operation === 'submit').task.environmentRefs, refs);
  assert.deepEqual(f.phases, ['prepare']);
});

test('selected delivery rejects missing or altered original frozen source evidence', async t => {
  for (const corruption of ['missing_input', 'changed_input', 'changed_digest', 'missing_digest', 'missing_build']) {
    const f = await sceneDeliveryTasks(t, [['push']]);
    const build = f.project.jobs.find(row => row.kind === 'build');
    if (corruption === 'missing_input') delete build.payload.task.sourceFiles;
    if (corruption === 'changed_input') build.payload.task.sourceFiles['tasks.py'] += '\n# corrupted\n';
    if (corruption === 'changed_digest') build.result.sourceDigests['tasks.py'] = '0'.repeat(64);
    if (corruption === 'missing_digest') build.result.sourceDigests = {};
    if (corruption === 'missing_build') {
      f.project.latestBuild = build.result;
      f.project.jobs = f.project.jobs.filter(row => row !== build);
    }
    await assert.rejects(f.deliver({ environmentRefs: [{ revision: 'rev_a' }] }), /冻结环境源|尚未构建|缺少冻结/);
    assert.deepEqual(f.phases, []);
  }
});

test('scene delivery accepts an explicit full task set through final consumption without rewriting its binding', async t => {
  const f = await sceneDeliveryTasks(t, [['push']]);
  const refs = [{ revision: 'rev_a', taskIds: ['push'] }];
  const pending = await f.deliver({ environmentRefs: refs });
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const submitted = f.requests.findLast(row => row.operation === 'submit').task;
  assert.deepEqual(submitted.environmentRefs, refs);
  assert.deepEqual(submitted.validationEpisodes, ['validation_rev_a_push']);
  const consumer = structuredClone(f.consumer);
  consumer.requiredOutputs = ['generation', 'evaluation']; consumer.environmentRefs = refs;
  delete consumer.demonstrations;
  consumer.evaluation.tasks[0].revision = 'rev_a';
  const row = consumer.evaluation.interfaces[0]; row.revision = 'rev_a'; row.sourceEpisodeId = 'validation_rev_a_push';
  for (const key of ['recordRef', 'episodeRef', 'sourceRef']) row[key] = row[key].replace('/interfaces/push/', '/interfaces/rev_a/push/');
  f.finish(pending.jobId, consumer);
  const done = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(done.delivery.accepted, true, done.delivery.missing.join('; '));
  assert.deepEqual(f.project.jobs.find(row => row.id === pending.jobId).environmentRefs, refs);
});

test('scene delivery accepts each revision complete set with explicit or omitted task IDs', async t => {
  for (const explicit of [false, true]) await t.test(String(explicit), async t => {
    const f = await sceneDeliveryTasks(t, [['push', 'place'], ['other']]);
    const refs = [{ revision: 'rev_a', ...(explicit ? { taskIds: ['place', 'push'] } : {}) },
      { revision: 'rev_b', ...(explicit ? { taskIds: ['other'] } : {}) }];
    const pending = await f.deliver({ environmentRefs: refs });
    await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
    const submitted = f.requests.findLast(row => row.operation === 'submit').task;
    assert.deepEqual(submitted.environmentRefs, refs);
    assert.deepEqual(new Set(submitted.validationEpisodes), new Set(['validation_rev_a_push', 'validation_rev_a_place', 'validation_rev_b_other']));
    assert.deepEqual(new Set(f.project.jobs.find(row => row.id === pending.jobId).requiredTaskIds), new Set(['push', 'place', 'other']));
  });
});

test('scene delivery rejects actual task reductions and invalid task IDs before remote consumption', async t => {
  for (const selection of [['push'], ['push', 'foreign'], ['push', 'push']]) await t.test(selection.join(','), async t => {
    const f = await sceneDeliveryTasks(t, [['push', 'place']]);
    const count = f.requests.length;
    await assert.rejects(f.deliver({ environmentRefs: [{ revision: 'rev_a', taskIds: selection }] }), /缩减|不属于|唯一|引用/);
    assert.equal(f.requests.length, count);
  });
});

test('scene delivery checks task completeness separately for every selected revision', async t => {
  const f = await sceneDeliveryTasks(t, [['push'], ['push', 'place']]);
  const count = f.requests.length;
  await assert.rejects(f.deliver({ environmentRefs: [{ revision: 'rev_a', taskIds: ['push'] },
    { revision: 'rev_b', taskIds: ['push'] }] }), /缩减/);
  assert.equal(f.requests.length, count);
});

test('robot-only collection reuses two old layouts and explicitly selected tasks without environment or generation consumption', async t => {
  let f;
  f = await produced(t, { consumeGeneration: async () => { throw new Error('unrequested generation consumer called'); },
    consumeProducedArtifacts: async (...args) => {
      const result = await f.producedConsumer(...args);
      delete result.environmentRoot; delete result.products.generation; delete result.products.environment;
      return result;
    },
    readInternalReview: async (...args) => {
      const value = await f.readInternalReview(...args);
      return { ...value, episodes: ['a', 'b'].map(id => ({ episodeId: 'collected_'+id, taskId: 'push', revision: 'rev_'+id, use: 'execution_data' })) };
    } });
  const build = f.project.jobs.find(row => row.kind === 'build');
  build.result.capabilities = { stageSplit: true, independentBehavior: true };
  build.result.tasks.push({ id: 'not_selected' });
  const second = structuredClone(build); second.id = 'build_b'; second.result.revision = 'rev_b';
  f.project.jobs.push(second);
  const oldGeneration = f.project.generation; oldGeneration.status = 'complete';
  const receipt = { accepted: true, generationId: oldGeneration.id, products: { evaluation: { passed: true } } };
  f.project.jobs.push({ id: 'accepted_environment', kind: 'consume', status: 'completed', delivery: receipt });
  const refs = [{ revision: 'rev_a', split: 'train', taskIds: ['push'] }, { revision: 'rev_b', split: 'test', taskIds: ['push'] }];
  f.store.beginGeneration(f.project.id, { requiredOutputs: ['demonstrations'], environmentRefs: refs });
  await f.store.addRequirement(f.project.id, '仅采集已选环境与任务，保留原环境');
  f.scope.generationId = f.project.generation.id;
  await writeFile(path.join(f.store.directory(f.project.id), 'expert.py'), '# new collector behavior\n');
  for (const suffix of ['a', 'b']) {
    const run = await f.call('robot_run', { requestId: 'collect_'+suffix, revision: 'rev_'+suffix, taskIds: ['push'],
      behavior: { sourceRef: 'expert.py', helperRefs: [] }, repetitions: 1 });
    f.finish(run.jobId, { projectId: f.project.id, revision: 'rev_'+suffix, episodes: [
      { episode: 'collected_'+suffix, taskId: 'push', projectId: f.project.id, revision: 'rev_'+suffix }] });
    await f.call('job', { jobId: run.jobId, action: 'status' });
  }
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  const consumer = { schema: 'sceneagent-product-consumption-v1', passed: true, projectId: f.project.id, revision: 'rev_a',
    requiredOutputs: ['demonstrations'], environmentRefs: refs,
    artifactRef: `projects/${f.project.id}/consumer-reports/deliver_a/report.json`, demonstrations: { passed: true, episodeCount: 2,
      batches: ['a', 'b'].map(id => ({ ...f.consumer.demonstrations.batches[0], episodeId: 'collected_'+id,
        revision: 'rev_'+id, use: 'execution_data' })) } };
  f.finish(pending.jobId, consumer);
  const result = await f.call('job', { jobId: pending.jobId, action: 'status' });
  assert.equal(result.delivery.accepted, true, result.delivery.missing.join('; '));
  assert.deepEqual(Object.keys(result.delivery.products), ['demonstrations']);
  assert.equal(receipt.accepted, true);
  assert.equal(f.project.jobs.filter(row => row.kind === 'build').length, 2);
  assert.deepEqual(f.requests.findLast(row => row.operation === 'submit').task.episodes.map(row => row.revision), ['rev_a', 'rev_b']);
  await assert.rejects(f.call('robot_run', { requestId: 'outside_subset', revision: 'rev_a', taskIds: ['not_selected'] }), /选择|任务/);
  await assert.rejects(f.call('project_deliver', { requestId: 'wrong_split', revision: 'rev_a',
    expectedRequirementVersion: f.project.generation.requirementVersion, requestHash: f.project.generation.requestHash,
    environmentRefs: [{ ...refs[0], split: 'test' }, refs[1]], producedArtifacts: f.producedArtifacts }), /锁定|选择/);
});

test('a new collection can read the original exact completed environment receipt without reopening old work', async t => {
  const f = await ready(t);
  const pending = await f.deliver();
  await f.call('job', { jobId: pending.jobId, action: 'wait', waitMs: 1000 });
  f.finish(pending.jobId, f.consumer);
  const old = await f.call('job', { jobId: pending.jobId, action: 'status' });
  f.project.generation.status = 'complete';
  const originalScope = { ...f.scope };
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = { stageSplit: true };
  f.store.beginGeneration(f.project.id, { requiredOutputs: ['demonstrations'], environmentRefs: [{ revision: 'rev_a' }] });
  const replay = await f.call('project_deliver', { revision: 'rev_a', requestId: 'deliver_a', expectedRequirementVersion: 1,
    requestHash: f.project.generation.requestHash });
  assert.deepEqual(replay.delivery, old.delivery);
  const historical = (await f.tools.call('job', { ...originalScope, jobId: pending.jobId, action: 'status' })).structuredContent;
  assert.deepEqual(historical.delivery, old.delivery);
  await assert.rejects(f.call('robot_run', { revision: 'rev_a', requestId: 'stale_scope_new_work', taskIds: ['push'] }), /作用域|任务/);
});

test('robot-only collection freezes selected profiles and behavior once without changing its base environment', async t => {
  const f = await ready(t), build = f.project.jobs.find(row => row.kind === 'build');
  build.result.capabilities = { stageSplit: true, independentBehavior: true, robotSetup: true };
  f.project.latestBuild.capabilities = build.result.capabilities;
  const base = structuredClone(f.project.latestBuild), accepted = { accepted: true };
  f.project.jobs.push({ id: 'accepted_environment', kind: 'consume', status: 'completed', delivery: accepted });
  f.project.generation.status = 'complete';
  f.store.beginGeneration(f.project.id, { requiredOutputs: ['demonstrations'], environmentRefs: [{ revision: 'rev_a' }] });
  f.scope.generationId = f.project.generation.id;
  const directory = f.store.directory(f.project.id);
  const profile = JSON.stringify({ schema: 'sceneagent-robot-profile-v1', model: { resourceRef: 'model' }, ports: {}, streams: {} });
  await writeFile(path.join(directory, 'arm.robot.json'), profile);
  await writeFile(path.join(directory, 'unused.robot.json'), '{}');
  await writeFile(path.join(directory, 'control_helper.py'), 'VALUE = 9\n');
  let snapshots = 0;
  const sourceSnapshot = f.store.sourceSnapshot.bind(f.store);
  f.store.sourceSnapshot = (...args) => { snapshots++; return sourceSnapshot(...args); };
  const robotSetup = { robots: [{ id: 'left', profileRef: 'arm.robot.json' },
    { id: 'right', profileRef: 'arm.robot.json', frame: 'table', at: [1, 0, 0], orientation: [1, 0, 0, 0] }],
    resourceRefs: ['assets/prepared/arm/report.json'], recording: { push: { cameras: [], primaryCameraId: null } } };
  const args = { revision: 'rev_a', taskIds: ['push'], requestId: 'installed_collect', robotSetup,
    behavior: { sourceRef: 'expert.py', helperRefs: ['control_helper.py'] } };
  const run = await f.call('robot_run', args), payload = structuredClone(f.requests.at(-1).task);
  assert.equal(snapshots, 1);
  assert.equal(run.revision, 'rev_a');
  assert.deepEqual(payload.robotSetup, { ...robotSetup, sourceFiles: { 'arm.robot.json': profile },
    sourceDigests: { 'arm.robot.json': contentHash(profile) } });
  assert.deepEqual(Object.keys(payload.behavior.sourceFiles).sort(), ['control_helper.py', 'expert.py']);
  assert.deepEqual(f.project.latestBuild, base);
  assert.equal(accepted.accepted, true);
  assert.equal(f.project.jobs.filter(row => row.kind === 'build').length, 1);
  await writeFile(path.join(directory, 'arm.robot.json'), '{}');
  await writeFile(path.join(directory, 'expert.py'), '# changed after submission\n');
  const sent = f.requests.length;
  assert.equal((await f.call('robot_run', args)).jobId, run.jobId);
  assert.equal(f.requests.length, sent);
  assert.equal(snapshots, 1);
  assert.deepEqual(f.project.jobs.at(-1).payload.task, payload);
});

test('robot setup guards reject unsupported or ambiguous new installation requests before remote submission', async t => {
  const setup = { robots: [{ id: 'robot', profileRef: 'arm.robot.json' }], resourceRefs: [] };
  const behavior = { sourceRef: 'expert.py', helperRefs: [] };
  const cases = [
    { selection: { robotSetup: setup }, error: /behavior|行为/ },
    { selection: { robotSetup: setup, robotSetupRef: 'frozen', behavior }, error: /混用|互斥|同时/ },
    { selection: { robotSetup: { ...setup, robots: [...setup.robots, ...setup.robots] }, behavior }, error: /重复|唯一/ },
    { selection: { robotSetup: { ...setup, resourceRefs: ['same', 'same'] }, behavior }, error: /重复|唯一/ },
    { selection: { robotSetup: { ...setup, robots: [{ id: 'robot', profileRef: 'expert.py' }] }, behavior }, error: /robot\.json|配置/ },
    { selection: { robotSetup: setup, behavior }, missing: true, error: /工程|配置/ },
    { selection: { robotSetup: setup, behavior }, legacy: true, error: /旧环境|不支持/ },
    { selection: { robotSetupRef: 'frozen' }, legacy: true, error: /旧环境|不支持/ },
  ];
  for (const [index, value] of cases.entries()) await t.test(String(index), async t => {
    const f = await ready(t), count = f.requests.length;
    f.project.jobs.find(row => row.kind === 'build').result.capabilities = { independentBehavior: true, robotSetup: !value.legacy };
    if (!value.missing) await writeFile(path.join(f.store.directory(f.project.id), 'arm.robot.json'), '{}');
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'invalid_setup', ...value.selection }), value.error);
    assert.equal(f.requests.length, count);
  });
});

test('scene-only validation cannot install or reuse a robot setup', async t => {
  for (const selection of [{ robotSetupRef: 'frozen' },
    { robotSetup: { robots: [{ id: 'robot', profileRef: 'arm.robot.json' }], resourceRefs: [] } }]) await t.test(JSON.stringify(selection), async t => {
    const f = await ready(t), build = f.project.jobs.find(row => row.kind === 'build').result;
    f.project.generation.requiredOutputs = ['generation', 'evaluation'];
    build.capabilities = { stageSplit: true, independentBehavior: true, robotSetup: true };
    const count = f.requests.length;
    await assert.rejects(f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'scene_only_setup', ...selection,
      purpose: 'verification', controller: { type: 'policy', policyRef: 'case.py', digest: build.sourceDigests['case.py'] } }), /本次.*范围|纯场景/);
    assert.equal(f.requests.length, count);
  });
});

test('reusing a frozen robot setup keeps the reference and does not snapshot source without a new behavior', async t => {
  const f = await ready(t);
  f.project.jobs.find(row => row.kind === 'build').result.capabilities = { robotSetup: true };
  f.store.sourceSnapshot = () => { throw new Error('reuse must not freeze current profiles'); };
  const robotSetupRef = `projects/${f.project.id}/robot-setups/hash/manifest.json`;
  const run = await f.call('robot_run', { revision: 'rev_a', taskIds: ['push'], requestId: 'reuse_installation', robotSetupRef });
  assert.equal(run.revision, 'rev_a');
  assert.equal(f.requests.at(-1).task.robotSetupRef, robotSetupRef);
  assert.equal(Object.hasOwn(f.requests.at(-1).task, 'robotSetup'), false);
});

test('episode camera permission comes from that published installation episode recording', async t => {
  const f = await ready(t), build = f.project.jobs.find(row => row.kind === 'build').result;
  build.tasks[0].recording = { primaryCameraId: 'front', cameras: [{ cameraId: 'front' }] };
  const robotSetupRef = `projects/${f.project.id}/robot-setups/a/manifest.json`;
  const recording = { primaryCameraId: 'front', cameras: [{ cameraId: 'front' }, { cameraId: 'wrist' }] };
  const source = f.project.jobs.find(row => row.kind === 'rollout').result.episodes[0];
  Object.assign(source, { robotSetupRef, robotSetupSha256: 'a'.repeat(64), recording });
  f.project.episodes[0] = { ...structuredClone(source), recording: { cameras: [{ cameraId: 'forged' }] } };
  f.project.jobs.push({ kind: 'rollout', status: 'completed', result: { episodes: [{ ...source, episode: 'other_episode',
    recording: { cameras: [{ cameraId: 'other' }] } }] } });
  const before = f.requests.length;
  for (const cameraId of ['forged', 'other']) await assert.rejects(f.call('scene_observe', {
    revision: 'rev_a', episodeId: 'ep_a', cameraId, requestId: 'foreign_'+cameraId }), /相机/);
  await assert.rejects(f.call('scene_observe', { revision: 'rev_a', cameraId: 'wrist', requestId: 'base_wrist' }), /相机/);
  assert.equal(f.requests.length, before);
  await f.call('scene_observe', { revision: 'rev_a', episodeId: 'ep_a', cameraId: 'wrist', requestId: 'installed_wrist' });
  assert.equal(f.requests.at(-1).task.cameraId, 'wrist');
  assert.equal(f.requests.at(-1).task.episode, 'ep_a');
});

test('an installation episode cache alone cannot authorize an unpublished camera binding', async t => {
  const f = await ready(t);
  f.project.episodes.push({ episode: 'unpublished', revision: 'rev_a', taskId: 'push',
    robotSetupRef: 'projects/p/robot-setups/fake/manifest.json', robotSetupSha256: 'a'.repeat(64),
    recording: { cameras: [{ cameraId: 'wrist' }] } });
  const count = f.requests.length;
  await assert.rejects(f.call('scene_observe', { revision: 'rev_a', episodeId: 'unpublished', cameraId: 'wrist', requestId: 'forged_installation' }), /发布|安装|相机/);
  assert.equal(f.requests.length, count);
});

for (const accepted of [false, true]) test(`explicit identical retry reconciles an unknown submission (accepted=${accepted})`, async t => {
  const f = await fixture(t); await f.tools.close();
  const submissions = [];
  const tools = createTools({ store: f.store, remote: async payload => {
    if (payload.operation === 'submit') {
      submissions.push(structuredClone(payload));
      if (submissions.length === 1) {
        if (accepted) await f.remote(payload);
        throw new Error('SSH response unavailable');
      }
    }
    return f.remote(payload);
  } });
  t.after(() => tools.close());
  const args = { ...f.scope, requestId: 'same_transport_request', assetId: 'fixture_asset' };
  const first = (await tools.call('assets_prepare', args)).structuredContent;
  assert.equal(first.status, 'unknown');
  assert.equal(f.jobs.size, accepted ? 1 : 0);
  const again = (await tools.call('assets_prepare', args)).structuredContent;
  assert.equal(again.status, 'running');
  assert.equal(again.jobId, first.jobId); assert.equal(again.error, null);
  assert.equal(f.project.jobs.length, 1); assert.equal(f.jobs.size, 1);
  assert.equal(submissions.length, 2); assert.deepEqual(submissions[1], submissions[0]);
  assert.equal(f.project.jobs[0].preparationPhase, undefined);
  await assert.rejects(tools.call('assets_prepare', { ...args, assetId: 'changed_asset' }), /输入/);
  assert.equal(submissions.length, 2);
});

test('concurrent identical retries share one retransmission', { timeout: 5000 }, async t => {
  const f = await fixture(t); await f.tools.close();
  let calls = 0, release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const tools = createTools({ store: f.store, remote: async payload => {
    if (payload.operation === 'submit') {
      if (++calls === 1) throw new Error('SSH handshake timeout');
      entered(); await gate;
    }
    return f.remote(payload);
  } });
  t.after(() => { release(); return tools.close(); });
  const args = { ...f.scope, requestId: 'concurrent_transport_retry', assetId: 'fixture_asset' };
  const first = (await tools.call('assets_prepare', args)).structuredContent;
  const a = tools.call('assets_prepare', args); await started;
  const b = tools.call('assets_prepare', args); release();
  const results = await Promise.all([a, b]);
  assert.equal(calls, 2); assert.equal(f.jobs.size, 1);
  assert.ok(results.every(row => row.structuredContent.jobId === first.jobId && row.structuredContent.status === 'running'));
});

for (const boundary of ['waiting_system', 'cancelled', 'job_cancelled', 'requirements_changed']) test(`unknown retry respects ${boundary}`, async t => {
  const f = await fixture(t); await f.tools.close(); let calls = 0;
  const tools = createTools({ store: f.store, remote: async () => { calls++; throw new Error('SSH handshake timeout'); } });
  t.after(() => tools.close());
  const args = { ...f.scope, requestId: 'guarded_transport_retry', assetId: 'fixture_asset' };
  await tools.call('assets_prepare', args);
  if (boundary === 'waiting_system') f.project.generation.waitingSystem = { reason: 'system investigation' };
  if (boundary === 'cancelled') f.project.generation.cancelRequestedAt = new Date().toISOString();
  if (boundary === 'job_cancelled') f.project.jobs[0].cancelRequested = true;
  if (boundary === 'requirements_changed') {
    f.project.generation.requirementVersion++;
    await assert.rejects(tools.call('assets_prepare', args), /需求版本/);
  } else assert.equal((await tools.call('assets_prepare', args)).structuredContent.status, 'unknown');
  assert.equal(calls, 1);
});
