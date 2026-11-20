import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import { TOOL_SCHEMAS } from './mcp.mjs';
import { contentHash, requiredOutputs, environmentRefs } from './store.mjs';
import { validId } from './config.mjs';

const active = new Set(['queued', 'running', 'unknown']);
const terminal = new Set(['completed', 'failed', 'cancelled']);
const operations = { assets_prepare: 'prepare', scene_build: 'build', scene_observe: 'observe',
  task_check: 'check', robot_run: 'rollout', project_deliver: 'consume' };
const fail = (message, status = 409) => Object.assign(new Error(message), { status });
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;

function validate(schema, value, label) {
  if (schema.type === 'object') {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw fail(`${label} 必须是对象`, 400);
    for (const key of schema.required || []) if (value[key] === undefined) throw fail(`缺少 ${key}`, 400);
    for (const [key, child] of Object.entries(value)) {
      const rule = schema.properties?.[key];
      if (!rule && schema.additionalProperties === false) throw fail(`不支持参数 ${key}`, 400);
      if (rule) validate(rule, child, key);
    }
  } else if (schema.type === 'array') {
    if (!Array.isArray(value) || value.length < (schema.minItems || 0) || value.length > (schema.maxItems ?? Infinity)) throw fail(`${label} 数组长度无效`, 400);
    value.forEach(child => validate(schema.items, child, label));
  } else if (schema.type === 'string') {
    if (typeof value !== 'string' || value.length < (schema.minLength || 0)) throw fail(`${label} 必须是有效文本`, 400);
  } else if (schema.type === 'boolean') {
    if (typeof value !== 'boolean') throw fail(`${label} 必须是布尔值`, 400);
  } else if (schema.type === 'number' || schema.type === 'integer') {
    if (typeof value !== 'number' || !Number.isFinite(value) || schema.type === 'integer' && !Number.isInteger(value)
      || value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity)
      || value <= (schema.exclusiveMinimum ?? -Infinity)) throw fail(`${label} 数值无效`, 400);
  }
  if (schema.enum && !schema.enum.includes(value)) throw fail(`${label} 取值无效`, 400);
}

function response(result) {
  const data = structuredClone(result);
  const images = [], seen = new Map();
  const strip = (value, bindImage = false) => {
    if (!value || typeof value !== 'object') return;
    if (value.image?.data && value.image.mimeType) {
      if (!seen.has(value.image.data)) { images.push(value.image); seen.set(value.image.data, images.length); }
      if (bindImage) value.imageContentIndex = seen.get(value.image.data);
      delete value.image;
    }
    for (const [key, child] of Object.entries(value)) {
      if (key === 'observations' && Array.isArray(child)) child.forEach(row => strip(row, true));
      else strip(child);
    }
  };
  strip(data);
  const content = [{ type: 'text', text: JSON.stringify(data) }];
  for (const image of images) content.push({ type: 'image', data: image.data, mimeType: image.mimeType });
  return { structuredContent: data, content };
}

function preparedSummary(result) {
  if (result?.kind !== 'native_files') return result;
  const fields = ['assetId', 'kind', 'name', 'description', 'entry', 'path', 'reportPath', 'digest',
    'artifactRef', 'license', 'checks', 'thumbnailRef', 'preview', 'image'];
  return { ...Object.fromEntries(fields.filter(key => result[key] !== undefined).map(key => [key, result[key]])),
    fileCount: result.nativeFiles?.length ?? 0, verification: 'declared_files_only' };
}

function inspectSummary(inspected, published) {
  const fields = ['schema', 'engine', 'physics', 'status', 'projectId', 'projectName', 'revision', 'seed',
    'episodeLimits', 'runtime', 'sourceDigests', 'tasks', 'nodes', 'validation', 'camera',
    'coordinateSpec', 'runtimeSnapshotRef', 'runtimeExecution', 'rendering', 'physicsSpec',
    'terminationMode', 'artifacts', 'limitations', 'worldContext', 'treeReport',
    'built', 'runtimeCompatible', 'capabilities', 'io'];
  const summary = Object.fromEntries(fields.filter(key => inspected[key] !== undefined).map(key => [key, inspected[key]]));
  if (!summary.capabilities && published?.capabilities) summary.capabilities = published.capabilities;
  if (inspected.compiled?.io && !summary.io) summary.io = inspected.compiled.io;
  if (inspected.compiled?.bindings) {
    const { nativeMeshLinkRelative, nativeMeshLocalBounds, ...bindings } = inspected.compiled.bindings;
    summary.compiled = { bindings };
  }
  const reference = summary.artifacts?.manifest, digest = published?.publicationArtifactDigests?.[reference];
  if (/^[a-f0-9]{64}$/.test(digest || '')) summary.publicationArtifactDigests = { [reference]: digest };
  return summary;
}

export function createTools({ store, remote, publish = () => {}, onJobTerminal = async () => {},
  onDelivery, onSystemWait, syncTrace, consumeGeneration, consumeProducedArtifacts, readInternalReview, stageOnlineAsset, sourceBusy = () => false }) {
  const reservations = new Map(), inFlight = new Map(), onlinePreparations = new Map();
  const terminalApplications = new Map();
  // Local abort handles only; persistent Job fields remain the state authority.
  let closed = false, closePromise;

  function releaseReservation(project, job) {
    if (reservations.get(project.id) === job.id) reservations.delete(project.id);
  }

  function scope(args, name) {
    if (!args.projectId || !args.generationId) throw fail('工具必须绑定项目与生成任务', 400);
    const project = store.get(args.projectId);
    if (project.generation?.id !== args.generationId) {
      const historical = operations[name] && args.requestId
        ? project.jobs.find(row => row.requestId === args.requestId && row.generationId === args.generationId)
        : name === 'job' && ['status', 'wait'].includes(args.action)
          ? project.jobs.find(row => row.id === args.jobId && row.generationId === args.generationId) : null;
      if (!historical || !terminal.has(historical.status)) throw fail('生成任务作用域不匹配');
    }
    return project;
  }

  const stopped = project => Boolean(project.generation?.cancelRequestedAt
    || ['cancel_requested', 'cancelled', 'failed'].includes(project.generation?.status));

  function admit(project, job, name = job?.name) {
    const generation = project.generation;
    if (closed || stopped(project)
      || generation?.status === 'complete' && !TOOL_SCHEMAS[name]?.annotations.readOnlyHint
      || job?.cancelRequested || job && terminal.has(job.status)) throw fail('生成任务已请求停止，不能提交新的计算。');
    if (job && (generation?.id !== job.generationId || generation.requirementVersion !== job.requirementVersion
      || generation.requestHash !== job.requestHash)) throw fail('需求版本已经变化，请重新检查。');
  }

  function manifest(project, revision) {
    const builds = project.jobs.filter(job => job.kind === 'build' && job.status === 'completed' && job.result?.revision === revision);
    const build = builds.at(-1);
    const result = build?.result || (project.latestBuild?.revision === revision ? project.latestBuild : null);
    if (!result || result.projectId && result.projectId !== project.id) throw fail('场景版本不属于当前项目或尚未构建');
    return { result, build };
  }

  function publicJob(project, job, events = []) {
    return { projectId: project.id, generationId: job.generationId, jobId: job.id, requestId: job.requestId,
      kind: job.kind, status: job.status, state: job.status, revision: job.result?.revision || job.revision,
      ...(job.workerTimeoutSeconds !== undefined ? { workerTimeoutSeconds: job.workerTimeoutSeconds } : {}),
      ...(job.rubricSeconds !== undefined ? { rubricSeconds: job.rubricSeconds } : {}),
      stage: job.stage, message: job.message,
      cursor: job.cursor || 0, events: job.kind === 'prepare' ? events.map(event => event.result?.kind === 'native_files'
        ? { ...event, result: preparedSummary(event.result) } : event) : events,
      result: job.kind === 'prepare' ? preparedSummary(job.result) ?? null : job.result ?? null, delivery: job.delivery || null,
      error: job.error || job.applicationError || job.transportError || null, cancellationPending: Boolean(job.cancelRequested) };
  }

  function artifactsFor(project, artifacts = {}) {
    project.artifacts ||= {};
    for (const [key, relative] of Object.entries(artifacts)) {
      if (typeof relative !== 'string' || !relative || relative.startsWith('/') || relative.split('/').some(part => !part || part === '.' || part === '..')) continue;
      project.artifacts[key] = `/api/projects/${project.id}/artifacts/${encodeURIComponent(path.posix.basename(relative))}?file=${encodeURIComponent(relative)}`;
    }
  }

  function producedSummary(project, job, result) {
    const pointer = job.producedArtifacts, root = `projects/${project.id}/deliveries/artifacts/${pointer.sha256}/`;
    const inside = value => typeof value === 'string' && value.startsWith(root)
      && value.split('/').every(part => part && part !== '.' && part !== '..');
    const binding = { projectId: project.id, generationId: job.generationId, requirementVersion: job.requirementVersion,
      requestHash: job.requestHash, sourceHash: job.sourceHash, revision: job.revision,
      ...(job.videoOnly ? { videoOnly: true } : {}),
      ...(job.environmentRefs ? { requiredOutputs: job.requiredOutputs, environmentRefs: job.environmentRefs } : {}) };
    const outputs = job.requiredOutputs || requiredOutputs();
    const episodes = job.reviewedEpisodes.map(({ episodeId, taskId, use, revision }) => ({ episodeId, taskId, use,
      ...(job.environmentRefs ? { revision } : {}) }));
    if (result?.schema !== 'sceneagent-produced-consumption-v1' || result.passed !== true
      || result.manifestRef !== pointer.manifestRef || result.sha256 !== pointer.sha256
      || contentHash(canonical(result.binding || {})) !== contentHash(canonical(binding))
      || contentHash(canonical(result.episodes || [])) !== contentHash(canonical(episodes))
      || result.frozenManifestRef !== root+'manifest.json' || !inside(result.artifactRef)
      || outputs.includes('evaluation') && (!inside(result.environmentRoot) || result.products?.environment?.root !== result.environmentRoot)
      || outputs.includes('generation') && !result.products?.generation || outputs.includes('demonstrations') && !result.products?.robot
      || !Array.isArray(result.files) || !result.files.length
      || result.files.some(row => !inside(row?.artifactRef) || !Number.isSafeInteger(row.bytes) || row.bytes < 0 || !/^[a-f0-9]{64}$/.test(row.sha256 || ''))
      || new Set(result.files.map(row => row.artifactRef)).size !== result.files.length) throw fail('生成器产物清单、来源或冻结文件消费未通过');
    return { manifestRef: result.manifestRef, sha256: result.sha256, frozenManifestRef: result.frozenManifestRef,
      environmentRoot: result.environmentRoot, binding: result.binding, episodes: result.episodes, products: result.products, files: result.files };
  }

  function deliveryGaps(project, job, consumer) {
    const gaps = [], generation = project.generation, outputs = job.requiredOutputs || requiredOutputs();
    const selected = Boolean(job.environmentRefs);
    const tasks = job.requiredTasks || (job.requiredTaskIds || []).map(taskId => ({ revision: job.revision, taskId }));
    const pair = row => `${row?.revision || job.revision}:${row?.taskId}`;
    if (stopped(project)) gaps.push('生成任务已停止，不能接受新交付');
    if (generation?.id !== job.generationId || generation.requirementVersion !== job.requirementVersion || generation.requestHash !== job.requestHash) gaps.push('需求版本已经变化，请按当前需求重新申请交付');
    if (project.sourceHash !== job.sourceHash) gaps.push('工程源码已经变化，交付必须重新申请');
    if (consumer?.schema !== 'sceneagent-product-consumption-v1' || consumer.passed !== true) gaps.push('消费者报告不是已通过的当前产物验收合同');
    if (consumer?.projectId !== project.id || consumer?.revision !== job.revision) gaps.push('消费者报告项目或场景版本不匹配');
    if (selected && (contentHash(consumer?.requiredOutputs ?? null) !== contentHash(outputs)
      || contentHash(canonical(consumer?.environmentRefs ?? null)) !== contentHash(canonical(job.environmentRefs)))) gaps.push('消费者交付范围或环境引用不匹配');
    if (outputs.includes('generation') && (job.generationConsumer?.passed !== true || !(job.generationConsumer.sampleCount > 0))) gaps.push('场景生成轨迹消费者未通过');
    if (job.internalReview?.schema !== 'sceneagent-internal-review-v2' || job.internalReview.decision !== 'pass') gaps.push('当前内部独立审核未通过');
    if (!tasks.length) gaps.push('交付必须包含非空的真实任务定义');
    const batches = consumer?.demonstrations?.batches;
    if (outputs.includes('demonstrations')) {
      const executed = Array.isArray(batches) && batches.length > 0 && batches.every(row =>
        Number.isSafeInteger(row?.controlCycles) && row.controlCycles > 0
        && Number.isSafeInteger(row.physicalSteps) && row.physicalSteps > 0 && row.uncommittedTail === false
        && ['execution_data', 'successful_demonstration'].includes(row.use)
        && typeof row.demonstrationEligible === 'boolean' && [true, false, null].includes(row.result?.success)
        && (!row.demonstrationEligible || row.result.success === true)
        && (row.use !== 'successful_demonstration' || row.demonstrationEligible && row.result.success === true)
        && job.reviewedEpisodes?.some(value => value.episodeId === row.episodeId && value.taskId === row.taskId
          && value.use === row.use && (!selected || value.revision === row.revision)));
      if (consumer?.demonstrations?.passed !== true || !executed || consumer.demonstrations.episodeCount !== batches.length) gaps.push('机器人执行数据加载、用途或成功声明未通过');
      if (!Array.isArray(batches) || batches.length !== job.reviewedEpisodes?.length
        || new Set(batches.map(row => row?.episodeId)).size !== batches.length) gaps.push('消费者执行集合及用途与本次固定审核集合不一致');
      for (const row of tasks) if (!Array.isArray(batches) || !batches.some(batch => pair(batch) === pair(row))) gaps.push(`任务 ${pair(row)} 缺少可消费执行数据`);
    }
    if (outputs.includes('evaluation')) {
      if (consumer?.reload?.passed !== true) gaps.push('冻结场景独立重载未通过');
      if (job.producedArtifacts && consumer?.reload?.environmentRoot !== job.producedArtifactsConsumer?.environmentRoot) gaps.push('环境消费者未使用本次冻结产物的环境根目录');
      if (consumer?.evaluation?.passed !== true) gaps.push('环境包与生成评分验证未通过');
      if (selected && (!Array.isArray(consumer?.evaluation?.tasks) || consumer.evaluation.tasks.length !== tasks.length
        || consumer.evaluation.tasks.some(row => !row?.revision || !tasks.some(task => pair(task) === pair(row)))
        || new Set(consumer.evaluation.tasks.map(pair)).size !== tasks.length)) gaps.push('环境验收任务的版本集合不匹配');
      const interfaces = consumer?.evaluation?.interfaces, isolation = consumer?.evaluation?.isolation;
      const reportRef = consumer?.artifactRef;
      const reportRoot = typeof reportRef === 'string' && reportRef.startsWith(`projects/${project.id}/consumer-reports/`)
        && reportRef.split('/').every(part => part && part !== '.' && part !== '..') ? path.posix.dirname(reportRef) : null;
      const runtimeRoots = new Map();
      if (consumer?.runtimeGroups !== undefined) {
        const groups = consumer.runtimeGroups;
        const valid = selected && reportRoot && Array.isArray(groups) && groups.length > 0 && groups.every(group => {
          if (!/^runtime\/[a-f0-9]{64}$/.test(group?.runtimeSnapshotRef || '') || !/^[a-f0-9]{64}$/.test(group.sha256 || '')
            || typeof group.runtimeExecution?.python !== 'string' || !Array.isArray(group.environmentRefs) || !group.environmentRefs.length) return false;
          const id = 'runtime_'+createHash('sha256').update(path.posix.basename(reportRoot)+'\n'+group.runtimeSnapshotRef+'\n'+group.runtimeExecution.python).digest('hex');
          const root = `projects/${project.id}/consumer-reports/${id}`;
          if (group.artifactRef !== root+'/report.json') return false;
          return group.environmentRefs.every(ref => {
            const expected = job.environmentRefs.find(value => value.revision === ref?.revision);
            if (!expected || runtimeRoots.has(ref.revision) || contentHash(canonical(ref)) !== contentHash(canonical(expected))) return false;
            const build = manifest(project, ref.revision).result;
            if (build.runtimeSnapshotRef !== group.runtimeSnapshotRef || build.runtime !== group.runtime
              || contentHash(canonical(build.runtimeExecution)) !== contentHash(canonical(group.runtimeExecution))) return false;
            runtimeRoots.set(ref.revision, root);
            return true;
          });
        }) && runtimeRoots.size === job.environmentRefs.length;
        if (!valid) gaps.push('冻结内核分组、原生报告范围或版本绑定不匹配');
      }
      const interfacesValid = Array.isArray(interfaces) && interfaces.length === tasks.length
        && new Set(interfaces.map(pair)).size === tasks.length && interfaces.every(row => {
          if (!row || selected && !row.revision) return false;
          if (!tasks.some(task => pair(task) === pair(row))) return false;
          const revision = row.revision || job.revision;
          const interfaceRoot = consumer?.runtimeGroups === undefined ? reportRoot : runtimeRoots.get(revision);
          const clock = manifest(project, revision).result.io?.clock, ticks = clock && clock.physicsRateHz / clock.controlRateHz;
          const source = selected ? job.validationEpisodes?.some(value => value.episodeId === row.sourceEpisodeId && value.revision === revision && value.taskId === row.taskId)
            : Array.isArray(batches) && batches.find(batch => batch?.episodeId === row.sourceEpisodeId && batch.taskId === row.taskId);
          const coverage = row.coverage;
          return tasks.some(task => pair(task) === pair(row)) && source && row.passed === true && row.scoreAvailable === true
            && Number.isSafeInteger(ticks) && ticks > 0 && ['open', 'reset', 'observe', 'record', 'score'].every(name => coverage?.[name] === 'checked')
            && ((row.controlCycles === 1 && row.physicalSteps === ticks && coverage.step === 'checked'
              && ['checked', 'not_exercised'].includes(coverage.endedStepRejection))
              || (!selected && row.controlCycles === 0 && row.physicalSteps === 0 && coverage.step === 'not_exercised'
                && coverage.endedStepRejection === 'checked' && source.controlCycles > 0 && source.physicalSteps > 0))
            && interfaceRoot && ['recordRef', 'episodeRef', 'sourceRef'].every(name => typeof row[name] === 'string'
              && row[name].startsWith(interfaceRoot+`/interfaces/${selected ? revision+'/' : ''}${row.taskId}/`)
              && row[name].split('/').every(part => part && part !== '.' && part !== '..'))
            && ['sourceDigest', 'sourceFactsDigest'].every(name => /^[a-f0-9]{64}$/.test(row[name] || ''));
        });
      if (!interfacesValid || isolation?.passed !== true || !isolation.checks || !Object.keys(isolation.checks).length
        || !Object.values(isolation.checks).every(value => value === true)) gaps.push('独立环境接口或隔离检查未通过');
      for (const row of tasks) if (!consumer?.evaluation?.tasks?.some(task => pair(task) === pair(row) && task.passed === true)) gaps.push(`任务 ${pair(row)} 缺少独立环境验收`);
    }
    return gaps;
  }

  function applyTerminal(project, job) {
    if (terminalApplications.has(job.id)) return terminalApplications.get(job.id);
    const pending = Promise.resolve().then(() => applyTerminalOnce(project, job)).finally(() => {
      if (terminalApplications.get(job.id) === pending) terminalApplications.delete(job.id);
    });
    terminalApplications.set(job.id, pending);
    return pending;
  }

  async function applyTerminalOnce(project, job) {
    if (job.cancelRequested) {
      delete job.cancelRequested;
      await store.save(); publish();
    }
    if (job.terminalApplied) {
      if (!job.terminalNotified) {
        await onJobTerminal(project, job);
        job.terminalNotified = true;
        await store.save();
      }
      return;
    }
    const result = job.result;
    if (job.status === 'completed' && result) {
      if (result.projectId && result.projectId !== project.id || job.revision && result.revision && job.revision !== result.revision) throw fail('远端作业返回了不同项目或版本');
      if (job.kind === 'build') {
        project.latestBuild = { ...result, sourceHash: job.sourceHash, requirementVersion: job.requirementVersion, requestHash: job.requestHash };
        project.artifacts = {};
        delete project.latestEpisode;
        artifactsFor(project, result.artifacts);
      } else if (job.kind === 'rollout') {
        project.latestEpisode = result;
        project.episodes ||= [];
        for (const episode of result.episodes || (result.episode ? [result] : [])) {
          if (!project.episodes.some(row => row.episode === episode.episode)) project.episodes.push(episode);
        }
        artifactsFor(project, result.artifacts || result.episodes?.[0]?.artifacts);
        if (result.media) {
          const primary = result.media.cameras?.find(row => row.cameraId === result.media.primaryCameraId);
          if (primary?.video) artifactsFor(project, { video: primary.video });
          else delete project.artifacts.video;
        }
      } else if (job.kind === 'check') project.latestCheck = result;
      else if (job.kind === 'prepare') project.latestPreparedAsset = result;
      else if (job.kind === 'consume') {
        // Re-read sources at the acceptance boundary; native file edits bypass HTTP.
        await store.sourceSnapshot(project.id, 'delivery_recheck');
        const missing = deliveryGaps(project, job, result);
        try {
          if (typeof readInternalReview !== 'function' || !job.reviewPointer || !job.internalReview?.checkpoint) throw fail('当前内部独立审核来源缺失');
          job.internalReview = await readInternalReview(project, job.reviewPointer, { job, checkpoint: job.internalReview.checkpoint, phase: 'terminal' });
          if (job.internalReview?.schema !== 'sceneagent-internal-review-v2' || job.internalReview.decision !== 'pass') throw fail('当前内部独立审核合同或结论无效');
          if (stopped(project)) throw fail('生成任务已停止，不能接受新交付');
          if (contentHash(job.internalReview.episodes) !== contentHash(job.reviewedEpisodes)) throw fail('固定审核示范集合变化');
          await store.sourceSnapshot(project.id, 'delivery_final_boundary');
          if (stopped(project)) throw fail('生成任务已停止，不能接受新交付');
          if (project.sourceHash !== job.sourceHash || project.generation?.id !== job.generationId
            || project.generation.requirementVersion !== job.requirementVersion || project.generation.requestHash !== job.requestHash) throw fail('源码或需求在最终边界变化');
          if (job.producedArtifacts) {
            if (typeof consumeProducedArtifacts !== 'function') throw fail('生成器产物消费者尚未接入');
            const produced = await consumeProducedArtifacts(project, job.producedArtifacts, { job, phase: 'terminal' });
            if (contentHash(canonical(producedSummary(project, job, produced))) !== contentHash(canonical(producedSummary(project, job, job.producedArtifactsConsumer)))) throw fail('生成器产物冻结清单在最终边界变化');
            job.producedArtifactsConsumer = produced;
            if (stopped(project)) throw fail('生成任务已停止，不能接受新交付');
            await store.sourceSnapshot(project.id, 'delivery_products_boundary');
            if (project.sourceHash !== job.sourceHash || project.generation?.id !== job.generationId
              || project.generation.requirementVersion !== job.requirementVersion || project.generation.requestHash !== job.requestHash) throw fail('产物核对期间源码或需求变化');
          }
        } catch (error) { missing.push(error.message); }
        const receipt = { generationId: job.generationId, requirementVersion: job.requirementVersion,
          requestHash: job.requestHash, projectId: project.id, revision: job.revision, requestId: job.requestId, sourceHash: job.sourceHash,
          accepted: !missing.length, status: missing.length ? 'incomplete' : 'accepted', missing,
          consumerSchema: result.schema,
          ...(job.environmentRefs ? { requiredOutputs: job.requiredOutputs, environmentRefs: job.environmentRefs } : {}),
          products: Object.fromEntries((job.requiredOutputs || requiredOutputs()).map(key => [key, {
            ...(key === 'generation' ? job.generationConsumer : result[key]),
            ...(job.producedArtifactsConsumer ? { exports: job.producedArtifactsConsumer.products[{ generation: 'generation', demonstrations: 'robot', evaluation: 'environment' }[key]] } : {}) }])),
          ...(job.producedArtifactsConsumer ? { producedArtifacts: job.producedArtifactsConsumer } : {}),
          reload: result.reload, evidence: job.evidence || [], internalReview: job.internalReview ? (({ checkpoint, episodes, ...receipt }) => receipt)(job.internalReview) : null,
          assessedBy: 'native_independent_review_with_consumer_evidence',
          semanticScope: 'native_review_receipt_provenance_binding_and_consumer_checks_not_formal_natural_language_proof', consumerArtifactRef: result.artifactRef };
        if (typeof onDelivery !== 'function') throw fail('缺少正式交付持久化入口，不能标记交付');
        job.delivery = await onDelivery(project, receipt) || receipt;
      }
    }
    await store.record(project.id, { type: 'operation_completed', operation: job.kind, jobId: job.id,
      status: job.status, result: job.result, error: job.error, delivery: job.delivery });
    job.terminalApplied = true;
    await store.save(); publish();
    await onJobTerminal(project, job);
    job.terminalNotified = true;
    await store.save();
  }

  async function refreshJob(project, job, { action = 'status', cursor = job.cursor || 0, waitMs = 0 } = {}) {
    if (job.generationId !== project.generation?.id) {
      if (terminal.has(job.status) && ['status', 'wait'].includes(action)) return publicJob(project, job);
      throw fail('作业属于不同生成任务');
    }
    if (action === 'wait' && waitMs > 0 && inFlight.has(job.id)) {
      let timer;
      try { await Promise.race([inFlight.get(job.id), new Promise(resolve => {
        timer = setTimeout(resolve, Math.min(30000, waitMs));
      })]); } finally { clearTimeout(timer); }
    }
    if (terminal.has(job.status)) {
      await applyTerminal(project, job);
      return publicJob(project, job);
    }
    if (job.pendingPreparation && reservations.get(project.id) === job.id) {
      if (action === 'cancel' && !terminal.has(job.status)) {
        job.cancelRequested = true;
        if (job.preparationPhase === 'remote_submit') { await store.save(); publish(); }
        else {
          job.status = 'cancelled';
          if (job.pendingPreparation === 'online_asset') job.error = '已取消：未提交后续远端计算；在线暂存进程是否退出以清理回执为准。';
          await applyTerminal(project, job);
          onlinePreparations.get(job.id)?.controller.abort(new Error('用户已取消在线准备；未提交后续远端计算。'));
          if (['trace_sync', 'online_stage'].includes(job.preparationPhase)) releaseReservation(project, job);
        }
      }
      return publicJob(project, job);
    }
    if (job.cancelRequested && job.payload) action = 'cancel';
    if (job.status === 'queued' && !job.payload) {
      if (reservations.has(project.id)) return publicJob(project, job);
      job.status = 'failed';
      job.error = '作业在提交前被中断，请使用新请求标识重新尝试。';
      delete job.pendingPreparation; delete job.preparationPhase;
      await applyTerminal(project, job);
      return publicJob(project, job);
    }
    if (inFlight.has(job.id)) {
      await inFlight.get(job.id);
      if (action !== 'cancel') return publicJob(project, job);
    }
    if (job.status === 'queued' && job.payload) {
      job.status = 'unknown';
      job.transportError = '远端提交意图已保存，接收结果尚未确认；仅查询原作业。';
      delete job.pendingPreparation; delete job.preparationPhase;
      await store.save(); publish();
    }
    const pending = (async () => {
      try {
        const update = await remote({ operation: 'job', projectId: project.id, generationId: job.generationId,
          jobId: job.id, action, cursor, waitSeconds: Math.min(30, Math.max(0, waitMs / 1000)) });
        if (update.jobId && update.jobId !== job.id) throw fail('远端作业身份不匹配');
        if (!active.has(update.state) && !terminal.has(update.state)) throw fail('远端作业状态无效');
        const events = update.events || [];
        for (const event of events) {
          if (event.sequence > (job.cursor || 0)) await store.record(project.id, { type: 'domain_event', jobId: job.id, ...event });
          if (event.type === 'progress') { job.stage = event.stage; job.message = event.message; }
        }
        job.cursor = Math.max(job.cursor || 0, update.cursor || 0);
        job.status = update.state;
        job.result = update.result;
        job.error = update.error || null;
        if (terminal.has(job.status)) delete job.cancelRequested;
        delete job.transportError;
        await store.save(); publish();
        if (terminal.has(job.status)) await applyTerminal(project, job);
        return publicJob(project, job, events);
      } catch (error) {
        job.transportError = error.message;
        await store.save(); publish();
        throw error;
      }
    })();
    inFlight.set(job.id, pending);
    try { return await pending; }
    finally { if (inFlight.get(job.id) === pending) inFlight.delete(job.id); }
  }

  async function submitRemote(project, job, task) {
    job.payload = { operation: 'submit', jobId: job.id, requestId: job.requestId, projectId: project.id,
      generationId: job.generationId, task };
    await store.record(project.id, { type: 'operation_started', operation: job.kind, jobId: job.id,
      requestId: job.requestId, sourceHash: job.sourceHash, revision: job.revision });
    await store.save(); publish();
    admit(project, job);
    job.preparationPhase = 'remote_submit';
    onlinePreparations.delete(job.id);
    try {
      const update = await remote(job.payload);
      if (update.jobId && update.jobId !== job.id || !active.has(update.state) && !terminal.has(update.state)) throw fail('远端返回了无效作业身份或状态');
      job.status = update.state;
      job.cursor = update.cursor || 0;
      job.result = update.result;
      job.error = update.error;
      delete job.transportError;
      for (const event of update.events || []) await store.record(project.id, { type: 'domain_event', jobId: job.id, ...event });
      await store.save(); publish();
    } catch (error) {
      // The remote may already own this request. Reconcile the same ID; never invent a retry job.
      job.status = 'unknown'; job.transportError = error.message;
      await store.save(); publish();
  }
    if (terminal.has(job.status)) await applyTerminal(project, job);
    return publicJob(project, job);
  }

  async function retrySubmission(project, job) {
    if (inFlight.has(job.id)) {
      await inFlight.get(job.id);
      return publicJob(project, job);
    }
    if (reservations.has(project.id)) return publicJob(project, job);
    admit(project, job);
    // This is an explicit retransmission, not a new attempt. Remote submit
    // serializes the same request identity and returns any existing job.
    const pending = Promise.resolve().then(async () => {
      await store.record(project.id, { type: 'operation_submission_retry', jobId: job.id,
        requestId: job.requestId, transportError: job.transportError });
      return submitRemote(project, job, job.payload.task);
    });
    inFlight.set(job.id, pending);
    try { return await pending; }
    finally {
      if (inFlight.get(job.id) === pending) inFlight.delete(job.id);
      delete job.pendingPreparation; delete job.preparationPhase;
      if (job.cancelRequested && active.has(job.status)) await refreshJob(project, job, { action: 'cancel' });
    }
  }

  async function submit(project, name, args, callEvent) {
    const requestId = args.requestId || 'r_' + randomUUID().replaceAll('-', '');
    if (!validId(requestId)) throw fail('requestId 格式无效', 400);
    const input = { ...args, requestId };
    const digest = contentHash(canonical({ name, args: input }));
    const existing = project.jobs.find(job => job.requestId === requestId && job.generationId === args.generationId);
    if (existing) {
      if (existing.inputHash !== digest) throw fail('同一 requestId 不能使用不同输入');
      return publicJob(project, existing);
    }
    if (name === 'robot_run' && args.rubricSeconds !== undefined
      && manifest(project, args.revision).result.capabilities?.adjustableRubricBudget !== true) throw fail('旧环境版本不支持显式评分预算；请显式构建兼容的新版本，原版本与记录保留。');
    admit(project, null, name);
    if (name === 'robot_run' && !requiredOutputs(project.generation.requiredOutputs).includes('demonstrations')
      && (args.behavior || args.robotSetup || args.robotSetupRef || args.controller?.type !== 'policy' || args.purpose !== 'verification')) {
      throw fail('本次交付范围为纯场景与评测环境，只允许 purpose=verification 的冻结验证案例，不执行专家或独立行为采集');
    }
    if (name === 'project_deliver' && project.generation.recordOnlyVideos) throw fail('原始录像模式不执行交付检查；真实任务成功后直接报告原始视频引用。', 400);
    if (name === 'project_deliver' && !args.review) throw fail('新的交付申请需要 review 原生内部独立审核指针', 400);
    if (name === 'project_deliver' && (project.generation.deliveryContract === 'producer-exports-v1' || args.producedArtifacts)) {
      const pointer = args.producedArtifacts;
      if (!pointer || !/^[a-f0-9]{64}$/.test(pointer.sha256 || '') || typeof pointer.manifestRef !== 'string'
        || !pointer.manifestRef.startsWith(`projects/${project.id}/delivery/`)
        || pointer.manifestRef.split('/').some(part => !part || part === '.' || part === '..')) throw fail('新的交付合同需要本工程 producedArtifacts 产物清单及 SHA256', 400);
    }
    if (reservations.has(project.id) || project.jobs.some(job => job.durable && active.has(job.status))) throw fail('这个项目已有远端作业正在执行');
    if (sourceBusy(project.id)) throw fail('工程源文件正在保存，请稍后重试');
    const job = { id: 'j_' + contentHash(`${project.id}:${args.generationId}:${requestId}`).slice(0, 24),
      requestId, inputHash: digest, name, kind: operations[name], durable: true, status: 'queued', cursor: 0,
      generationId: args.generationId, requirementVersion: project.generation.requirementVersion,
      requestHash: project.generation.requestHash, revision: args.revision, callEventId: callEvent.eventId,
      startedAt: new Date().toISOString(), terminalApplied: false, pendingPreparation: 'submission' };
    if (TOOL_SCHEMAS[name].inputSchema.properties.workerTimeoutSeconds) job.workerTimeoutSeconds = args.workerTimeoutSeconds ?? 900;
    if (args.rubricSeconds !== undefined) job.rubricSeconds = args.rubricSeconds;
    reservations.set(project.id, job.id);
    project.jobs.push(job);
    let preparing = false;
    try {
      let task = { ...input, operation: job.kind };
      delete task.projectId; delete task.generationId;
      if (name === 'scene_build' && requiredOutputs(project.generation.requiredOutputs).length === 1
        && (!project.generation.videoOnly || project.generation.environmentRefs)) throw fail('当前任务只读复用已选环境，不能重新构建场景');
      if (name === 'scene_build' || name === 'assets_prepare' && args.sourceFile) {
        const source = await store.sourceSnapshot(project.id, callEvent.eventId);
        admit(project, job);
        if (source.sourceHash !== args.expectedSourceHash) throw fail('源码摘要已经变化，请重新检查后构建');
        if (args.environmentSourceRefs && (new Set(args.environmentSourceRefs).size !== args.environmentSourceRefs.length || args.environmentSourceRefs.some(name => !Object.hasOwn(source.sourceFiles, name)))) throw fail('环境源引用必须是本工程快照中的唯一文件', 400);
        if (args.sourceFile && !Object.hasOwn(source.sourceFiles, args.sourceFile)) throw fail('资产生成源必须属于本工程的冻结Python快照', 400);
        if (args.baseRevision) manifest(project, args.baseRevision);
        job.sourceHash = source.sourceHash;
        task = { ...task, sources: source.sources, sourceFiles: source.sourceFiles,
          sourceHash: source.sourceHash, projectName: project.name };
      } else if (name === 'project_deliver') {
        const outputs = requiredOutputs(project.generation.requiredOutputs);
        if (project.generation.videoOnly) job.videoOnly = true;
        const requestedEnvironments = args.environmentRefs ?? project.generation.environmentRefs;
        const selected = outputs.length !== 3 || requestedEnvironments !== undefined;
        const refs = selected ? environmentRefs(requestedEnvironments ?? [{ revision: args.revision }]) : [{ revision: args.revision }];
        if (!refs.some(row => row.revision === args.revision)) throw fail('交付主版本不属于本次环境选择');
        if (project.generation.environmentRefs && contentHash(canonical(refs)) !== contentHash(canonical(project.generation.environmentRefs))) throw fail('环境引用与当前生成任务锁定选择不一致');
        if (selected) {
          job.requiredOutputs = outputs; job.environmentRefs = refs;
          task.requiredOutputs = outputs; task.environmentRefs = refs;
        }
        if (args.expectedRequirementVersion !== project.generation.requirementVersion || args.requestHash !== project.generation.requestHash) throw fail('交付需求版本已经变化');
        const source = await store.sourceSnapshot(project.id, 'delivery_start');
        admit(project, job);
        job.sourceHash = source.sourceHash;
        if (args.producedArtifacts) job.producedArtifacts = { ...args.producedArtifacts };
        const requiredTasks = [], validationEpisodes = [], validationSelections = [];
        for (const ref of refs) {
          const locked = manifest(project, ref.revision);
          if (selected && locked.result.capabilities?.stageSplit !== true) throw fail('该旧环境版本不支持独立阶段交付；请显式构建兼容的新版本，保留原包');
          const tasks = locked.result.tasks || [];
          const ids = ref.taskIds || tasks.map(row => row.id);
          if (!ids.length || ids.some(id => !tasks.some(row => row.id === id))) throw fail('交付任务不属于冻结环境或集合为空');
          if (outputs.includes('evaluation') && tasks.some(row => !ids.includes(row.id))) throw fail('场景与环境交付不能缩减冻结任务集合');
          requiredTasks.push(...ids.map(taskId => ({ revision: ref.revision, taskId })));
          if (outputs.includes('evaluation')) {
            for (const row of tasks) {
              const sourceRef = row.rubric?.sourceRef, sourceDigest = locked.result.sourceDigests?.[sourceRef];
              if (!sourceRef || typeof sourceDigest !== 'string' || !/^[a-f0-9]{64}$/.test(sourceDigest)) throw fail(`任务 ${row.id} 缺少冻结的生成评分程序`);
              const cases = row.evaluation?.cases;
              if (!Array.isArray(cases) || !cases.length || cases.some(value => typeof value.id !== 'string' || !value.id)
                || new Set(cases.map(value => value.id)).size !== cases.length) throw fail(`任务 ${row.id} 缺少适用的评分验证案例`);
            }
            if (selected) {
              const frozenInput = locked.build?.payload?.task?.sourceFiles;
              if (!frozenInput || contentHash(frozenInput) !== locked.build.sourceHash
                || !Object.keys(locked.result.sourceDigests || {}).length
                || Object.entries(locked.result.sourceDigests).some(([name, digest]) => typeof frozenInput[name] !== 'string' || contentHash(frozenInput[name]) !== digest)) throw fail('冻结环境源闭包与原始构建输入不匹配');
            } else if (!locked.build || locked.build.sourceHash !== source.sourceHash || locked.build.requirementVersion !== job.requirementVersion) throw fail('场景版本未对应当前源码或需求，请重新构建');
            const check = project.jobs.filter(row => row.kind === 'check' && row.status === 'completed' && row.result?.revision === ref.revision).at(-1)?.result
              || (project.latestCheck?.revision === ref.revision ? project.latestCheck : null);
            if (check?.passed !== true || !ids.every(id => check.tasks?.some(row => row.taskId === id && row.passed === true))) throw fail('当前版本的任务绑定与生成评分验证尚未通过');
            for (const row of check.tasks.filter(row => ids.includes(row.taskId))) for (const checked of row.cases || []) {
              validationEpisodes.push(checked.episodeId);
              validationSelections.push({ revision: ref.revision, taskId: row.taskId, episodeId: checked.episodeId });
            }
          }
        }
        job.requiredTaskIds = [...new Set(requiredTasks.map(row => row.taskId))];
        if (selected) job.requiredTasks = requiredTasks;
        task.validationEpisodes = [...new Set(validationEpisodes)];
        job.validationEpisodes = selected ? validationSelections : task.validationEpisodes;
        if (task.validationEpisodes.some(id => !validId(id))) throw fail('评分验证缺少实际执行回合');
        if (typeof syncTrace !== 'function' || outputs.includes('generation') && typeof consumeGeneration !== 'function') throw fail('生成轨迹消费者尚未接入');
        if (typeof readInternalReview !== 'function') throw fail('内部独立审核来源读取尚未接入');
        job.reviewPointer = args.review;
        job.internalReview = await readInternalReview(project, args.review, { job, phase: 'prepare' });
        admit(project, job);
        if (job.internalReview?.schema !== 'sceneagent-internal-review-v2' || job.internalReview.decision !== 'pass'
          || !job.internalReview.checkpoint || !Array.isArray(job.internalReview.episodes)
          || outputs.includes('demonstrations') && !job.internalReview.episodes.length) throw fail('内部独立审核未通过或执行集合缺失');
        job.reviewedEpisodes = job.internalReview.episodes;
        task.episodes = job.reviewedEpisodes.map(({ episodeId, taskId, use, revision }) => ({ episodeId, taskId, use,
          ...(selected ? { revision } : {}) }));
        if (task.episodes.some(row => !validId(row.episodeId) || !requiredTasks.some(task => task.taskId === row.taskId && (!selected || task.revision === row.revision))
            || !['execution_data', 'successful_demonstration'].includes(row.use))
          || new Set(task.episodes.map(row => row.episodeId)).size !== task.episodes.length
          || outputs.includes('demonstrations') && !requiredTasks.every(required => task.episodes.some(row => row.taskId === required.taskId && (!selected || row.revision === required.revision)))
          || !outputs.includes('demonstrations') && task.episodes.length) throw fail('内部审核执行重复、用途无效或任务覆盖缺失');
        delete task.review;
        delete task.producedArtifacts;
        job.evidence = args.evidence || [];
        job.pendingPreparation = 'generation_consumer';
        job.preparationPhase = 'trace_sync';
        await store.save(); publish();
        preparing = true;
        const preparation = (async () => {
          try {
            await syncTrace(project);
            admit(project, job);
            if (project.generation?.id !== job.generationId || project.generation.requirementVersion !== job.requirementVersion || project.generation.requestHash !== job.requestHash) throw fail('交付需求版本已经变化');
            if (outputs.includes('generation')) {
              job.preparationPhase = 'generation_consumer';
              job.generationConsumer = await consumeGeneration(project);
              admit(project, job);
              if (job.generationConsumer?.passed !== true || !(job.generationConsumer.sampleCount > 0)) throw fail('场景生成轨迹消费者未通过');
            }
            if (job.producedArtifacts) {
              if (typeof consumeProducedArtifacts !== 'function') throw fail('生成器产物消费者尚未接入');
              job.preparationPhase = 'produced_artifacts';
              const produced = await consumeProducedArtifacts(project, job.producedArtifacts, { job, phase: 'prepare' });
              admit(project, job);
              producedSummary(project, job, produced);
              job.producedArtifactsConsumer = produced;
              if (outputs.includes('evaluation')) task.environmentRoot = produced.environmentRoot;
            }
            job.preparationPhase = 'source_snapshot';
            const preparedSource = await store.sourceSnapshot(project.id, 'delivery_prepared');
            admit(project, job);
            if (preparedSource.sourceHash !== job.sourceHash) throw fail('工程源码已经变化，交付必须重新申请');
            if (project.generation?.id !== job.generationId || project.generation.requirementVersion !== job.requirementVersion || project.generation.requestHash !== job.requestHash) throw fail('交付需求版本已经变化');
            job.internalReview = await readInternalReview(project, job.reviewPointer, { job, checkpoint: job.internalReview.checkpoint, phase: 'prepared' });
            admit(project, job);
            if (job.internalReview?.schema !== 'sceneagent-internal-review-v2' || job.internalReview.decision !== 'pass') throw fail('当前内部独立审核合同或结论无效');
            if (contentHash(job.internalReview.episodes) !== contentHash(job.reviewedEpisodes)) throw fail('固定审核示范集合变化');
            await submitRemote(project, job, task);
          } catch (error) {
            if (terminal.has(job.status) && job.payload) job.applicationError = error.message;
            else if (!terminal.has(job.status)) { job.status = job.cancelRequested || project.generation?.cancelRequestedAt ? 'cancelled' : 'failed'; job.error = error.message; }
            await store.save(); publish();
            if (terminal.has(job.status)) await applyTerminal(project, job);
          } finally {
            delete job.pendingPreparation;
            delete job.preparationPhase;
            releaseReservation(project, job);
            if (inFlight.get(job.id) === preparation) inFlight.delete(job.id);
            await store.save(); publish();
            if (job.cancelRequested && active.has(job.status)) await refreshJob(project, job, { action: 'cancel' });
          }
        })();
        inFlight.set(job.id, preparation);
        void preparation.catch(() => {});
        return publicJob(project, job);
      } else if (args.revision) {
        const locked = manifest(project, args.revision);
        if (args.taskIds && !args.taskIds.every(id => locked.result.tasks?.some(task => task.id === id))) throw fail('任务标识不属于当前场景版本');
        if (args.episodeId && !project.episodes?.some(row => row.episode === args.episodeId && row.revision === args.revision)) throw fail('执行片段不属于当前场景版本');
        if (args.episodeIds && !args.episodeIds.every(id => project.episodes?.some(row => row.episode === id && row.revision === args.revision))) throw fail('验证回合不属于当前场景版本');
        if (name === 'scene_observe') {
          if (args.nodeId && (args.cameraId || args.episodeId)) throw fail('节点范围不能与冻结相机或执行片段混用', 400);
          if (args.frame !== undefined && !args.episodeId) throw fail('全局相机帧索引需要明确执行片段', 400);
          const episode = args.episodeId && project.episodes.find(row => row.episode === args.episodeId);
          const selected = episode ? locked.result.tasks?.find(row => row.id === episode.taskId) : locked.result.tasks?.[0];
          const published = episode && project.jobs.filter(row => row.kind === 'rollout' && row.status === 'completed')
            .flatMap(row => row.result?.episodes || (row.result?.episode ? [row.result] : []))
            .find(row => row.episode === episode.episode && row.revision === args.revision && row.taskId === episode.taskId);
          const installed = episode?.robotSetupRef || published?.robotSetupRef;
          if (installed && (!published?.robotSetupRef || !/^[a-f0-9]{64}$/.test(published.robotSetupSha256 || '')
            || !Array.isArray(published.recording?.cameras))) throw fail('执行片段缺少已发布的机器人安装与相机绑定');
          const recording = installed ? published.recording : selected?.recording;
          if (args.cameraId && !recording?.cameras?.some(row => row.cameraId === args.cameraId)) throw fail('相机不属于所选冻结任务或执行片段', 400);
          if (args.cameraIds && !args.cameraIds.every(id => selected?.recording?.cameras?.some(row => row.cameraId === id))) throw fail('批量相机不属于第一项冻结任务', 400);
        }
      }
      if (name === 'scene_observe' && args.episodeId) { task.episode = args.episodeId; delete task.episodeId; }
      if (name === 'robot_run') task.purpose ||= 'demonstration';
      if (name === 'robot_run') {
        const locked = manifest(project, args.revision).result;
        const selectedEnvironment = project.generation.environmentRefs?.find(row => row.revision === args.revision);
        if (project.generation.environmentRefs && (!selectedEnvironment || selectedEnvironment.taskIds && args.taskIds.some(id => !selectedEnvironment.taskIds.includes(id)))) throw fail('执行版本或任务不属于本次锁定的环境选择');
        if (args.repetitions !== undefined && args.seeds !== undefined) throw fail('repetitions不能与旧seeds重复参数混用', 400);
        if (args.robotSetup && args.robotSetupRef) throw fail('robotSetup不能与robotSetupRef同时使用', 400);
        if ((args.robotSetup || args.robotSetupRef) && locked.capabilities?.robotSetup !== true) throw fail('旧环境版本不支持独立机器人安装；请显式构建兼容新版本，原包保持');
        if (args.robotSetup && !args.behavior) throw fail('新机器人安装必须明确提供独立behavior行为源', 400);
        if (args.robotSetup && (new Set(args.robotSetup.robots.map(row => row.id)).size !== args.robotSetup.robots.length
          || new Set(args.robotSetup.resourceRefs).size !== args.robotSetup.resourceRefs.length)) throw fail('机器人安装标识和资源引用必须唯一，不能重复', 400);
        if ((args.behavior || args.repetitions !== undefined) && locked.capabilities?.independentBehavior !== true) throw fail('旧环境版本不支持独立行为或重复次数；请显式构建兼容新版本，原包保持');
        if (args.behavior) {
          if (args.controller) throw fail('独立行为不能与冻结controller选择混用', 400);
          const source = await store.sourceSnapshot(project.id, callEvent.eventId);
          admit(project, job);
          const names = [args.behavior.sourceRef, ...args.behavior.helperRefs];
          if (new Set(names).size !== names.length || names.some(name => !Object.hasOwn(source.sourceFiles, name))) throw fail('行为源闭包必须是本工程快照中的唯一文件', 400);
          task.behavior = { ...args.behavior, sourceFiles: Object.fromEntries(names.map(name => [name, source.sourceFiles[name]])),
            sourceDigests: Object.fromEntries(names.map(name => [name, contentHash(source.sourceFiles[name])])) };
          if (args.robotSetup) {
            const profiles = [...new Set(args.robotSetup.robots.map(row => row.profileRef))];
            if (profiles.some(name => !name.endsWith('.robot.json') || !Object.hasOwn(source.sourceFiles, name))) throw fail('机器人配置必须是本工程快照中的.robot.json文件', 400);
            task.robotSetup = { ...args.robotSetup, sourceFiles: Object.fromEntries(profiles.map(name => [name, source.sourceFiles[name]])),
              sourceDigests: Object.fromEntries(profiles.map(name => [name, contentHash(source.sourceFiles[name])])) };
          }
          job.sourceHash = source.sourceHash;
        }
        if (args.controller?.type === 'policy') {
          if (args.controller.sourceRef) throw fail('独立策略使用任务冻结的 policyRef');
          for (const id of args.taskIds) {
            const matches = locked.tasks?.find(row => row.id === id)?.evaluation?.cases?.filter(row => row.sourceRef === args.controller.policyRef
              && (!args.controller.caseId || row.id === args.controller.caseId)) || [];
            const policy = matches.length === 1 ? matches[0] : null;
            if (!policy?.sourceRef || !locked.sourceDigests?.[policy.sourceRef]) throw fail('请用 policyRef/caseId 明确选择当前任务的一个冻结验证案例');
            if (args.controller.digest !== locked.sourceDigests[policy.sourceRef]) throw fail('独立策略来源或摘要不匹配');
          }
          if (task.purpose !== 'verification') throw fail('验证程序必须使用 verification 用途');
        } else {
          if (args.controller?.caseId) throw fail('caseId 仅用于选择冻结验证案例，不能用于专家入口');
          if (args.controller?.policyRef || args.controller?.sourceRef && args.controller.sourceRef !== 'expert.py') throw fail('专家入口为本版本冻结的 expert.py');
          if (args.controller?.digest && args.controller.digest !== locked.sourceDigests?.['expert.py']) throw fail('专家程序摘要不匹配');
        }
      }
      if (name === 'assets_prepare' && args.url) {
        if (typeof stageOnlineAsset !== 'function') throw fail('在线资产下载入口尚未接入');
        job.pendingPreparation = 'online_asset'; job.preparationPhase = 'online_stage';
        job.stage = 'online_stage'; job.message = '正在核对缓存、下载并冻结在线资产';
        await store.save(); publish();
        preparing = true;
        const controller = new AbortController(), local = { project, job, controller };
        const preparation = Promise.resolve().then(async () => {
          try {
            admit(project, job);
            task.importRef = await stageOnlineAsset(args.url, { signal: controller.signal });
            if (closed && onlinePreparations.get(job.id) !== local) return;
            admit(project, job);
            delete job.stage; delete job.message;
            await submitRemote(project, job, task);
          } catch (error) {
            if (closed && onlinePreparations.get(job.id) !== local && job.preparationPhase !== 'remote_submit') return;
            if (terminal.has(job.status)) job.applicationError ??= error.message;
            else {
              job.status = job.cancelRequested || project.generation?.cancelRequestedAt ? 'cancelled' : 'failed';
              job.error = error.message;
              await applyTerminal(project, job);
            }
            await store.save(); publish();
          } finally {
            if (closed && onlinePreparations.get(job.id) !== local && job.preparationPhase !== 'remote_submit') return;
            delete job.pendingPreparation; delete job.preparationPhase;
            releaseReservation(project, job);
            if (inFlight.get(job.id) === preparation) inFlight.delete(job.id);
            await store.save(); publish();
            if (job.cancelRequested && active.has(job.status)) await refreshJob(project, job, { action: 'cancel' });
            if (onlinePreparations.get(job.id) === local) onlinePreparations.delete(job.id);
          }
        });
        local.promise = preparation;
        onlinePreparations.set(job.id, local); inFlight.set(job.id, preparation);
        void preparation.catch(() => {});
        return publicJob(project, job);
      }
      return await submitRemote(project, job, task);
    } catch (error) {
      if (terminal.has(job.status) && job.payload) job.applicationError = error.message;
      else if (!terminal.has(job.status)) { job.status = job.cancelRequested || project.generation?.cancelRequestedAt ? 'cancelled' : 'failed'; job.error = error.message; job.terminalApplied = true; job.terminalNotified = true; }
      await store.save(); publish();
      throw error;
    } finally {
      if (!preparing) {
        delete job.pendingPreparation; delete job.preparationPhase;
        releaseReservation(project, job);
        if (job.cancelRequested && active.has(job.status)) await refreshJob(project, job, { action: 'cancel' });
      }
    }
  }

  async function call(name, args = {}, { actor = 'unknown' } = {}) {
    const schema = TOOL_SCHEMAS[name];
    if (!schema) throw fail('场景工具不存在', 404);
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw fail('工具参数必须是对象', 400);
    const project = scope(args, name);
    const existing = operations[name] && args.requestId && project.jobs.find(job => job.requestId === args.requestId && job.generationId === args.generationId);
    if (existing && existing.inputHash !== contentHash(canonical({ name, args }))) throw fail('同一 requestId 不能使用不同输入');
    if (!existing) validate(schema.inputSchema, args, name);
    if (!existing && args.changedNodeIds !== undefined) throw fail('changedNodeIds 已退役，新请求请省略；构建和检查保持实际全量范围。', 400);
    if (!existing && name === 'scene_observe' && args.output === 'web_preview'
      && ['cameraIds', 'cameraId', 'nodeId', 'episodeId', 'frame'].some(key => args[key] !== undefined)) {
      throw fail('网页预览不能与相机、节点或执行片段参数混用。', 400);
    }
    if (!existing && name === 'scene_observe' && args.cameraIds !== undefined
      && (new Set(args.cameraIds).size !== args.cameraIds.length
        || ['cameraId', 'nodeId', 'episodeId', 'frame'].some(key => args[key] !== undefined))) {
      throw fail('批量相机必须互异，不能与单相机、节点或执行片段参数混用。', 400);
    }
    if (!existing && name === 'assets_prepare') {
      if (['assetId', 'url', 'sourceFile', 'resourceFile'].filter(key => args[key] !== undefined).length !== 1) throw fail('资产准备必须选择 assetId、url、sourceFile、resourceFile 中恰好一个', 400);
      if (!args.resourceFile && args.dependencyFiles !== undefined) throw fail('dependencyFiles 仅用于原生 resourceFile', 400);
      if (args.mdlSearchRoots !== undefined && (!args.resourceFile || new Set(args.mdlSearchRoots).size !== args.mdlSearchRoots.length
        || args.mdlSearchRoots.some(value => !path.posix.isAbsolute(value) || path.posix.normalize(value) !== value || value.includes('\\')))) {
        throw fail('mdlSearchRoots 仅用于原生 resourceFile 的互异、规范绝对目录。', 400);
      }
      if (args.sourceFile && !args.expectedSourceHash) throw fail('资产生成源需要 expectedSourceHash', 400);
      if (!args.sourceFile && (args.expectedSourceHash !== undefined || args.materialIds !== undefined)) throw fail('expectedSourceHash 和 materialIds 仅用于资产生成源', 400);
      if (args.url && (!args.sourcePage || !args.name)) throw fail('在线资产需要 sourcePage 和 name 来源信息', 400);
    }
    const event = await store.record(project.id, { type: 'tool_call', name, args, actor,
      requestId: args.requestId || null });
    try {
      let result;
      if (!existing && operations[name] && project.generation?.waitingSystem) throw fail('生成任务正在等待系统处理，修复并明确恢复后才能提交新的计算。');
      if (existing) result = existing.status === 'unknown' && existing.payload && !closed
        && !project.generation?.waitingSystem && !stopped(project) && !existing.cancelRequested
        ? await retrySubmission(project, existing) : publicJob(project, existing);
      else if (name === 'generation_wait_system') {
        if (!args.requestId.trim() || !args.reason.trim() || args.evidence.some(value => !value.trim())) throw fail('等待请求需要非空标识、原因和实际错误证据。', 400);
        if (args.jobIds?.some(id => !project.jobs.some(job => job.id === id && job.generationId === args.generationId))) throw fail('等待证据作业不属于当前生成任务。');
        if (typeof onSystemWait !== 'function') throw fail('系统等待入口尚未接通。', 503);
        result = await onSystemWait(project, args);
      } else if (name === 'job') {
        const job = project.jobs.find(row => row.id === args.jobId && row.generationId === args.generationId);
        if (!job) throw fail('作业不属于当前项目与生成任务');
        result = await refreshJob(project, job, { action: args.action, cursor: args.cursor, waitMs: args.waitMs ?? (args.action === 'wait' ? 30000 : 0) });
      } else if (name === 'assets_lookup') {
        result = await remote({ ...args, operation: 'assets', limit: args.limit || 8 });
      } else if (name === 'project_inspect') {
        const source = await store.sourceSnapshot(project.id, event.eventId);
        const revision = args.baseRevision || project.latestBuild?.revision;
        const published = revision ? manifest(project, revision).result : null;
        const inspected = await remote({ operation: 'inspect', projectId: project.id, generationId: args.generationId, revision, nodeId: args.nodeId });
        result = { ...inspectSummary(inspected, published), projectId: project.id, generationId: project.generation.id, originalRequest: project.prompt,
          requirements: project.requirements, requirementVersion: project.generation.requirementVersion,
          requestHash: project.generation.requestHash, requiredOutputs: project.generation.requiredOutputs,
          environmentRefs: project.generation.environmentRefs,
          deliveryContract: project.generation.deliveryContract ?? null,
          ...(project.generation.videoOnly ? { videoOnly: true } : {}),
          ...(project.generation.recordOnlyVideos ? { recordOnlyVideos: true, recordOnlyTaskIds: project.generation.recordOnlyTaskIds } : {}),
          generationStatus: project.generation.status, waitingSystem: project.generation.waitingSystem ?? null,
          sourcePaths: ['scene', 'tasks', 'expert', 'rubrics'].map(key => path.join(store.directory(project.id), key + '.py')),
          sourceHash: source.sourceHash, currentSourceHash: source.sourceHash, snapshotRef: source.snapshotRef,
          recording: { localTrace: path.join(store.directory(project.id), 'trace.jsonl'),
            mirroredTrace: `projects/${project.id}/generation-trace.jsonl`,
            archiveDirectories: ['.trace/sources', '.trace/inputs'], archiveAccess: 'read_only_original_bytes',
            mirror: { syncedAt: project.traceSyncedAt ?? null, sha256: project.traceSyncedHash ?? null, error: project.traceSyncError ?? null },
            traceScope: 'complete_line_prefix; later events are not part of a frozen export' },
          jobs: project.jobs.map(job => ({ jobId: job.id, requestId: job.requestId, kind: job.kind, status: job.status,
            ...(job.workerTimeoutSeconds !== undefined ? { workerTimeoutSeconds: job.workerTimeoutSeconds } : {}),
            revision: job.result?.revision || job.revision, artifactRef: job.result?.artifactRef,
            success: job.result?.success ?? job.result?.score?.success, error: job.error || job.transportError || job.applicationError })),
          delivery: project.delivery || null };
      } else result = await submit(project, name, args, event);
      await store.record(project.id, { type: 'tool_result', name, callEventId: event.eventId, result });
      await store.save();
      return response(result);
    } catch (error) {
      await store.record(project.id, { type: 'tool_result', name, callEventId: event.eventId, error: error.message });
      await store.save();
      throw error;
    }
  }

  const timer = setInterval(() => {
    if (closed) return;
    for (const project of store.projects) for (const job of project.jobs) {
      if (job.durable && job.generationId === project.generation?.id && (active.has(job.status) || terminal.has(job.status) && (!job.terminalApplied || !job.terminalNotified)) && !inFlight.has(job.id) && !reservations.has(project.id)) {
        void refreshJob(project, job).catch(() => {});
      }
    }
  }, 1500);
  timer.unref();
  return { call, refreshJob, close() {
    if (closePromise) return closePromise;
    closed = true; clearInterval(timer);
    const local = [...onlinePreparations.values()];
    const deliveries = store.projects.flatMap(project => project.jobs
      .filter(job => job.preparationPhase === 'trace_sync').map(job => inFlight.get(job.id))).filter(Boolean);
    for (const item of local) item.controller.abort(new Error('工作台关闭，在线准备在远端计算提交前中断。'));
    closePromise = (async () => {
      await Promise.allSettled(deliveries);
      if (!local.length) return;
      let timeout;
      try {
        const settled = await Promise.race([Promise.allSettled(local.map(item => item.promise)).then(() => true),
          new Promise(resolve => { timeout = setTimeout(() => resolve(false), 3000); })]);
        if (!settled) for (const item of local) {
          if (onlinePreparations.get(item.job.id) !== item) continue;
          item.job.applicationError = [item.job.applicationError, '本地在线准备清理退出未确认；后续远端计算未提交。'].filter(Boolean).join('；');
          if (!terminal.has(item.job.status)) {
            item.job.status = 'failed'; item.job.error = '工作台关闭，在线准备在远端计算提交前中断。';
            await applyTerminal(item.project, item.job);
          }
          delete item.job.pendingPreparation; delete item.job.preparationPhase;
          releaseReservation(item.project, item.job);
          if (inFlight.get(item.job.id) === item.promise) inFlight.delete(item.job.id);
          onlinePreparations.delete(item.job.id);
        }
        await store.save(); publish();
      } finally { clearTimeout(timeout); }
    })();
    return closePromise;
  } };
}
