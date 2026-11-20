import http from 'node:http';
import path from 'node:path';
import { createReadStream, constants } from 'node:fs';
import { readFile, stat, readdir, mkdir, writeFile, open, lstat } from 'node:fs/promises';
import { randomUUID, createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { pipeline } from 'node:stream/promises';
import { isDeepStrictEqual } from 'node:util';
import { ROOT, config, configuration as startupConfiguration, loadConfiguration, resolveCodexCommand } from './config.mjs';
import { Store, contentHash, requiredOutputs } from './store.mjs';
import { createTools } from './tools.mjs';
import { TOOL_SCHEMAS } from './mcp.mjs';
import { GenerationHost } from './generation.mjs';
import { CodexBridge } from './codex.mjs';
import * as remoteFiles from './remote.mjs';
import { acquireWorkspaceOwnership, bindWorkspaceTarget, createSetup } from './setup.mjs';

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.py': 'text/plain; charset=utf-8', '.glb': 'model/gltf-binary', '.mp4': 'video/mp4', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.jsonl': 'application/x-ndjson' };

export function parseRange(header, size) {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match || (!match[1] && !match[2])) return false;
  let start, end;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!suffix) return false;
    start = Math.max(0, size - suffix); end = size - 1;
  } else {
    start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  }
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || start > end) return false;
  return { start, end, length: end - start + 1 };
}

async function body(request) {
  if (!request.headers['content-type']?.startsWith('application/json')) throw Object.assign(new Error('请求必须使用 JSON'), { status: 415 });
  let bytes = 0; const chunks = [];
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > 2000000) throw Object.assign(new Error('请求内容过大'), { status: 413 });
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString() || '{}'); }
  catch { throw Object.assign(new Error('JSON 格式无效'), { status: 400 }); }
}

function json(response, data, status = 200) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(data));
}

const pick = (value, keys) => value ? Object.fromEntries(keys.filter(key => value[key] !== undefined).map(key => [key, value[key]])) : null;
const short = value => typeof value === 'string' ? value.slice(0, 2048) : value;
function resultSummary(value) {
  if (!value) return null;
  const result = pick(value, ['projectId', 'revision', 'episode', 'episodeId', 'taskId', 'success', 'passed', 'terminated', 'truncated', 'reward', 'scoreStatus', 'executionStatus', 'nodes', 'controlCycles', 'episodeCount', 'sampleCount', 'agentTrainingSampleCount', 'diagnosticSampleCount', 'editSampleCount', 'toolSampleCount', 'artifactRef', 'rendering', 'capabilities']);
  if (value.score) result.score = pick(value.score, ['success', 'terminated', 'reward', 'details', 'scoreStatus', 'executionStatus', 'rubric', 'taskId']);
  if (value.artifacts) result.artifacts = value.artifacts;
  if (Array.isArray(value.tasks)) result.tasks = value.tasks.map(row => pick(row, ['id', 'taskId', 'name', 'revision', 'passed']));
  if (value.exports) result.exports = value.exports;
  if (value.media) result.media = { primaryCameraId: value.media.primaryCameraId,
    cameras: (value.media.cameras || []).map(row => ({ ...pick(row, ['cameraId', 'required', 'status', 'video', 'frameCount', 'firstTime', 'lastTime']),
      ...(row.reason ? { reason: short(row.reason) } : {}) })) };
  return result;
}
function publicJob(job) {
  return { ...pick(job, ['id', 'kind', 'status', 'stage', 'progress', 'requestId', 'generationId', 'revision', 'startedAt', 'endedAt', 'workerTimeoutSeconds']),
    message: short(job.message), error: short(job.error || job.applicationError || job.transportError
      || (job.durable && !job.generationId && ['queued', 'running', 'unknown'].includes(job.status) ? '旧作业归属未知，仍占用远端工作范围，需核对原作业。' : '')), result: resultSummary(job.result),
    ...(job.delivery ? { delivery: pick(job.delivery, ['accepted', 'status', 'revision', 'generationId', 'requiredOutputs', 'environmentRefs', 'artifactRef']) } : {}) };
}
function publicEvent(event) {
  if (event.type === 'codex') {
    if (!['turn/started', 'turn/completed', 'error', 'item/started', 'item/completed'].includes(event.method)) return null;
    const params = event.params || {};
    return { ...pick(event, ['type', 'method', 'time', 'eventId', 'child']), params: {
      threadId: params.threadId, turnId: params.turnId,
      turn: pick(params.turn, ['id', 'status']), error: params.error ? { message: short(params.error.message) } : undefined,
      item: params.item ? { ...pick(params.item, ['id', 'type', 'status', 'tool', 'name']), command: short(params.item.command) } : undefined } };
  }
  return { ...pick(event, ['type', 'time', 'eventId', 'operation', 'name', 'jobId', 'stage']), message: short(event.message), error: short(event.error) };
}
export function publicProject(project) {
  const environments = new Map(project.jobs.filter(row => row.kind === 'build' && row.status === 'completed'
    && row.result?.revision && (!row.result.projectId || row.result.projectId === project.id)).map(row => [row.result.revision, row.result]));
  if (project.latestBuild?.revision && !environments.has(project.latestBuild.revision)) environments.set(project.latestBuild.revision, project.latestBuild);
  const environmentRevisions = [...environments.values()].map(value => {
    const delivered = project.jobs.findLast(row => row.delivery?.accepted === true && row.delivery.products?.evaluation?.passed === true
      && (row.delivery.environmentRefs?.some(ref => ref.revision === value.revision) || row.delivery.revision === value.revision))?.delivery;
    const split = delivered?.environmentRefs?.find(ref => ref.revision === value.revision)?.split;
    return { ...pick(value, ['revision', 'seed', 'capabilities']), tasks: (value.tasks || []).map(row => pick(row, ['id', 'name'])),
      ...(split ? { split } : {}),
      ...(delivered ? { delivery: pick(delivered, ['artifactRef', 'generationId', 'accepted']) } : {}) };
  });
  const artifactUrl = relative => `/api/projects/${project.id}/artifacts/${encodeURIComponent(path.posix.basename(relative))}?file=${encodeURIComponent(relative)}`;
  const videoUrl = episode => artifactUrl(episode.artifacts.video);
  const archivedEpisode = project.episodes?.findLast(episode => project.latestBuild?.revision
    && episode.revision === project.latestBuild.revision && episode.demonstrationEligible === true && episode.score?.success === true
    && typeof episode.artifacts?.video === 'string' && episode.artifacts.video.startsWith(`projects/${project.id}/episodes/`)
    && (!project.artifacts?.video || project.artifacts.video === videoUrl(episode)));
  const displayEpisode = project.latestEpisode?.media ? project.latestEpisode
    : !project.artifacts?.video && archivedEpisode ? archivedEpisode : project.latestEpisode || archivedEpisode;
  let artifacts = !project.artifacts?.video && archivedEpisode && !project.latestEpisode?.media
    ? { ...project.artifacts, video: videoUrl(archivedEpisode) } : project.artifacts;
  const previewJobs = project.jobs.filter(job => project.latestBuild?.revision && job.kind === 'observe' && job.revision === project.latestBuild.revision
    && job.payload?.task?.output === 'web_preview');
  const preview = previewJobs.findLast(job => {
    const result = job.result, build = project.latestBuild, source = result?.source;
    const reference = result?.artifacts?.preview;
    const prefix = `projects/${project.id}/previews/${build.revision}/${result?.converterRuntime}/${job.requestId}/`;
    return job.status === 'completed' && result?.schema === 'sceneagent-web-preview-v1'
      && result.checkedScope === 'web_preview' && result.projectId === project.id && result.revision === build.revision
      && typeof source?.manifestRef === 'string' && source.manifestRef === build.artifacts?.manifest
      && source.manifestSha256 === build.publicationArtifactDigests?.[source.manifestRef]
      && /^[a-f0-9]{64}$/.test(source.manifestSha256 || '')
      && source.usdRef === path.posix.join(path.posix.dirname(source.manifestRef), build.compiled?.usd || '')
      && source.usdSha256 === build.compiled?.frozenUsdSha256
      && /^[a-f0-9]{64}$/.test(result.digests?.preview || '')
      && typeof reference === 'string' && reference === prefix + 'preview.glb'
      && reference.split('/').every(part => part && part !== '.' && part !== '..')
      && result.artifactRef === prefix + 'receipt.json';
  });
  if (preview) artifacts = { ...artifacts, preview: artifactUrl(preview.result.artifacts.preview) };
  const lastPreview = previewJobs.at(-1);
  const webPreview = lastPreview ? { jobId: lastPreview.id, revision: lastPreview.revision,
    status: lastPreview.status, ...(lastPreview.error ? { error: short(lastPreview.error) } : {}) } : undefined;
  if (displayEpisode?.media) {
    const primary = displayEpisode.media.cameras.find(row => row.cameraId === displayEpisode.media.primaryCameraId);
    artifacts = { ...artifacts };
    delete artifacts.video;
    if (typeof primary?.video === 'string' && primary.video.startsWith(`projects/${project.id}/episodes/${displayEpisode.episode}/`)) artifacts.video = artifactUrl(primary.video);
  }
  const generation = project.generation;
  const currentReceiptHash = project.delivery?.generationId === generation?.id
    ? project.delivery?.sourceHash || project.delivery?.internalReview?.sourceHash : null;
  const buildWorkspaceHash = project.jobs.findLast(row => row.kind === 'build' && row.status === 'completed'
    && row.result?.revision === project.latestBuild?.revision)?.sourceHash || project.latestBuild?.sourceHash;
  const boundWorkspaceHash = currentReceiptHash || (generation?.requiredOutputs?.length === 1 ? null : buildWorkspaceHash);
  const edited = project.sourceHash && boundWorkspaceHash && project.sourceHash !== boundWorkspaceHash;
  const deliveryCurrent = !edited && !generation?.cancelRequestedAt && !['cancel_requested', 'cancelled', 'failed'].includes(generation?.status) && project.delivery?.generationId === generation?.id
    && (!generation?.recordOnlyVideos || project.delivery?.recordOnlyVideos === true)
    && project.delivery?.requirementVersion === generation?.requirementVersion && project.delivery?.requestHash === generation?.requestHash;
  const delivery = project.delivery ? { ...pick(project.delivery, ['accepted', 'status', 'revision', 'generationId', 'requirementVersion', 'deliveryId', 'artifactRef', 'consumerArtifactRef', 'requiredOutputs', 'environmentRefs', 'schema', 'assessedBy', 'reviewSkipped', 'checksSkipped', 'recordOnlyVideos', 'recordOnlyTaskIds', 'videos']),
    ...(project.delivery.producedArtifacts ? { producedArtifacts: pick(project.delivery.producedArtifacts, ['schema', 'passed', 'manifestRef', 'sha256', 'frozenManifestRef', 'environmentRoot', 'artifactRef', 'products']) } : {}),
    ...(!deliveryCurrent && project.delivery.accepted ? { accepted: false, status: 'incomplete' } : {}),
    missing: project.delivery.missing?.slice(0, 20).map(short),
    products: Object.fromEntries(Object.entries(project.delivery.products || {}).map(([key, value]) => [key, resultSummary(value)])) } : null;
  return { ...pick(project, ['id', 'name', 'status', 'threadId', 'activeTurnId', 'createdAt', 'sourceHash', 'traceSyncedAt']),
    status: generation && !['complete', 'failed', 'cancelled'].includes(generation.status) ? generation.status
      : project.activeTurnId ? 'thinking' : edited ? 'edited' : generation?.status
        ?? (project.threadId || project.latestBuild ? 'idle' : project.status || 'draft'),
    prompt: short(project.prompt),
    generation: pick(project.generation, ['id', 'status', 'requirementVersion', 'requestHash', 'requiredOutputs', 'environmentRefs', 'deliveryContract', 'videoOnly', 'recordOnlyVideos', 'recordOnlyTaskIds', 'startedAt', 'timeBudgetMs', 'completedAt', 'terminalReason', 'waitingSystem', 'systemWaitMs']),
    delivery, artifacts, environmentRevisions, ...(webPreview ? { webPreview } : {}),
    latestBuild: resultSummary(project.latestBuild), latestEpisode: resultSummary(displayEpisode),
    jobs: project.jobs.slice(-30).map(publicJob),
    messages: project.messages.slice(-30).map(message => ({ ...pick(message, ['id', 'role', 'status']), content: String(message.content || '').slice(0, 8192) })),
    events: project.events.map(publicEvent).filter(Boolean).slice(-80) };
}

// A slow browser may reconnect to the latest snapshot; the full trace remains on disk.
export function writeSse(response, packet) {
  if (response.destroyed || response.writableEnded) return false;
  if (response.writableLength + Buffer.byteLength(packet) > 1024 * 1024) { response.destroy(); return false; }
  response.write(packet);
  return true;
}

// Reads one native final receipt; the generator's reviewer owns all quality decisions.
export function createInternalReviewReader({ store, codex, publishSnapshot }) {
  const require = (condition, message) => { if (!condition) throw new Error('内部审核：' + message); };
  const normalization = 'native final receipt; no reviewer-work replay';
  return async (project, pointer, { job, checkpoint, phase = 'prepare' } = {}) => {
    const binding = { projectId: project.id, generationId: job.generationId, requirementVersion: job.requirementVersion,
      requestHash: job.requestHash, sourceHash: job.sourceHash, revision: job.revision,
      ...(job.videoOnly ? { videoOnly: true } : {}),
      ...(job.producedArtifacts ? { producedArtifacts: { ...job.producedArtifacts } } : {}),
      ...(job.environmentRefs ? { requiredOutputs: job.requiredOutputs, environmentRefs: job.environmentRefs } : {}) };
    const checkBinding = () => require(project.generation?.id === binding.generationId
      && project.generation.requirementVersion === binding.requirementVersion && project.generation.requestHash === binding.requestHash
      && project.sourceHash === binding.sourceHash && isDeepStrictEqual(job.producedArtifacts, binding.producedArtifacts)
      && (project.generation.videoOnly === true) === (job.videoOnly === true)
      && (!job.environmentRefs || isDeepStrictEqual(requiredOutputs(project.generation.requiredOutputs), binding.requiredOutputs)
        && (!project.generation.environmentRefs || isDeepStrictEqual(project.generation.environmentRefs, binding.environmentRefs))), '源码或需求已经变化，或产物清单不一致');
    checkBinding();
    require(pointer?.threadId && pointer.turnId && pointer.itemId && project.threadId && pointer.threadId !== project.threadId, '需要真实独立子会话指针');
    const readHeader = async () => {
      const metadata = await codex.readThread(pointer.threadId, { includeTurns: false });
      const spawn = metadata?.source?.subAgent?.thread_spawn;
      require(metadata?.id === pointer.threadId && metadata.parentThreadId === project.threadId
        && spawn?.parent_thread_id === project.threadId && spawn.agent_path?.split('/').at(-1) === 'scene_quality_review', '审核会话父来源或专职角色不一致');
      require(['idle', 'notLoaded'].includes(metadata.status?.type), '审核会话仍在运行或状态不可用');
      const turns = await codex.listTurns(pointer.threadId, { sortDirection: 'desc', itemsView: 'notLoaded', limit: 1 });
      require(Array.isArray(turns?.data) && turns.data.length === 1 && Object.hasOwn(turns, 'nextCursor')
        && (turns.nextCursor === null || typeof turns.nextCursor === 'string'), '最新原生轮次响应缺失');
      const turn = turns.data[0];
      require(turn.id === pointer.turnId && turn.status === 'completed', '指针不是当前最新已完成审核轮次');
      return { metadata, turn };
    };
    const { metadata, turn } = await readHeader();
    const items = await codex.listItems(pointer.threadId, { turnId: turn.id, sortDirection: 'desc', limit: 1 });
    require(Array.isArray(items?.data) && items.data.length === 1 && Object.hasOwn(items, 'nextCursor')
      && (items.nextCursor === null || typeof items.nextCursor === 'string'), '最终原生消息响应缺失');
    const row = items.data[0], message = row.item;
    require(row.turnId === turn.id && message?.id === pointer.itemId && message.type === 'agentMessage'
      && [null, 'final_answer'].includes(message.phase ?? null), '指针不是该轮最后完整最终消息');
    let report;
    try { report = JSON.parse(message.text); } catch { throw new Error('内部审核：最终结论必须是单一完整 JSON'); }
    require(report?.schema === 'sceneagent-internal-review-v2' && report.decision === 'pass', '结论不是当前 v2 通过回执');
    for (const [key, value] of Object.entries(binding)) require(isDeepStrictEqual(report[key], value), '审核结论 ' + key + ' 绑定过期');
    if (project.generation.deliveryContract === 'producer-exports-v1') require(job.producedArtifacts, '生成器产物清单绑定缺失');
    const selected = report.coverage?.episodes;
    const hasDemonstrations = (job.requiredOutputs || requiredOutputs()).includes('demonstrations');
    const scoped = Boolean(job.environmentRefs);
    require(!Object.hasOwn(report.coverage || {}, 'episodeIds') && Array.isArray(selected)
      && (hasDemonstrations ? selected.length > 0 : selected.length === 0)
      && selected.every(value => value && Object.keys(value).length === (scoped ? 3 : 2)
        && typeof value.episodeId === 'string' && value.episodeId
        && (!scoped || job.environmentRefs.some(ref => ref.revision === value.revision))
        && ['execution_data', 'successful_demonstration'].includes(value.use))
      && new Set(selected.map(value => value.episodeId)).size === selected.length, '执行选择或用途缺失、无效或重复');
    const requiredTasks = job.requiredTasks || (job.requiredTaskIds || []).map(taskId => ({ revision: binding.revision, taskId }));
    require(requiredTasks.length > 0, '冻结任务集合缺失');
    const episodes = selected.map(({ episodeId, use, revision: selectedRevision }) => {
      const revision = scoped ? selectedRevision : binding.revision;
      const published = project.jobs.filter(value => value.kind === 'rollout' && value.status === 'completed'
        && value.generationId === binding.generationId && value.revision === revision
        && value.result?.projectId === project.id && value.result.revision === revision)
        .flatMap(value => value.result.episodes || []).filter(value => value.episode === episodeId);
      require(published.length === 1, '执行记录不是当前已发布运行');
      const episode = published[0];
      require(episode.projectId === project.id && episode.revision === revision
        && requiredTasks.some(task => task.revision === revision && task.taskId === episode.taskId)
        && project.episodes?.some(value => value.episode === episodeId && value.projectId === project.id
          && value.revision === revision && value.taskId === episode.taskId), '执行记录项目、版本或任务来源不一致');
      return { episodeId, taskId: episode.taskId, use, revision };
    });
    if (hasDemonstrations) for (const row of requiredTasks) require(episodes.some(value => value.revision === row.revision && value.taskId === row.taskId), '任务缺少选定执行数据：' + row.revision + ':' + row.taskId);
    const messageHash = contentHash(message);
    if (checkpoint) {
      require(/^\.trace\/inputs\/[a-f0-9]{64}\.json$/.test(checkpoint.snapshotRef)
        && /^[a-f0-9]{64}$/.test(checkpoint.snapshotHash), '回执快照路径或摘要无效');
      const bytes = await readFile(path.join(store.directory(project.id), checkpoint.snapshotRef));
      require(createHash('sha256').update(bytes).digest('hex') === checkpoint.snapshotHash, '回执快照摘要变化');
      const previous = JSON.parse(bytes);
      require(previous.schema === 'sceneagent-native-review-source-v1' && previous.normalization === normalization, '旧过程快照不能作为当前最终回执');
      require(contentHash(previous.binding) === contentHash(binding) && contentHash(previous.pointer) === contentHash(pointer), '回执快照属于旧绑定或指针');
      require(contentHash(previous.episodes) === contentHash(episodes), '固定交付执行集合或用途变化');
      require(previous.native?.messageHash === messageHash, '原生最终消息在交付期间变化');
    }
    await store.sourceSnapshot(project.id, 'internal_review_' + phase); checkBinding();
    const scope = 'native final receipt provenance and current binding; quality belongs to native reviewer; product technical checks belong to consumers';
    const snapshot = { schema: 'sceneagent-native-review-source-v1', normalization, binding, pointer: { ...pointer }, phase,
      native: { metadata: { id: metadata.id, parentThreadId: metadata.parentThreadId, source: metadata.source, status: metadata.status },
        turn: { id: turn.id, status: turn.status }, messageId: message.id, messageHash, report }, episodes, scope };
    const serialized = JSON.stringify(snapshot, null, 2), snapshotHash = contentHash(serialized), snapshotRef = `.trace/inputs/${snapshotHash}.json`;
    await mkdir(path.join(store.directory(project.id), '.trace/inputs'), { recursive: true });
    await writeFile(path.join(store.directory(project.id), snapshotRef), serialized);
    await store.record(project.id, { type: 'input_snapshot', kind: 'internal_review_source', snapshotRef, inputHash: snapshotHash,
      normalization, phase, coverage: scope });
    if (phase !== 'prepare') {
      require(typeof publishSnapshot === 'function', '终态来源归档发布入口缺失');
      await publishSnapshot(project.id, snapshotRef, snapshot, { immutable: true });
    }
    await readHeader(); checkBinding();
    return { schema: 'sceneagent-internal-review-v2', threadId: pointer.threadId, turnId: pointer.turnId, itemId: pointer.itemId,
      decision: 'pass', ...binding, episodes, snapshotRef, snapshotHash, scope, checkpoint: { snapshotRef, snapshotHash } };
  };
}

// Follow only native structural delegation sources; never interpret reviewer content or quality.
export function createNativeWorkReader({ codex, now = Date.now }) {
  return async (project, native, { purpose } = {}) => {
    const maintenance = purpose === 'context-maintenance';
    const generation = project.generation, generationId = generation.id, threadId = project.threadId, waiting = generation.waitingSystem, cancellation = generation.cancelRequestedAt;
    const require = (condition, message) => { if (!condition) throw new Error(message); };
    const current = () => require(project.generation === generation && generation.id === generationId && project.threadId === threadId
      && generation.waitingSystem === waiting && generation.cancelRequestedAt === cancellation, '原生工作查询期间生成身份已变化。');
    const pages = async (method, id, options) => {
      const rows = []; let cursor = null; const cursors = new Set();
      do {
        require(!cursors.has(JSON.stringify(cursor)), '原生分页游标重复。'); cursors.add(JSON.stringify(cursor));
        const page = await codex[method](id, { ...options, cursor }); current();
        require(Array.isArray(page?.data) && Object.hasOwn(page, 'nextCursor')
          && (page.nextCursor === null || typeof page.nextCursor === 'string'), '原生分页不完整。');
        rows.push(...page.data); cursor = page.nextCursor;
      } while (cursor !== null);
      return rows;
    };
    const items = async (id, turn, embedded) => {
      if (embedded && Array.isArray(turn.items)) return turn.items;
      const rows = await pages('listItems', id, { turnId: turn.id, limit: 50, sortDirection: 'asc' });
      require(rows.every(row => row.turnId === turn.id && typeof row.item?.type === 'string'), '原生条目来源不完整。');
      return rows.map(row => row.item);
    };
    const ended = turn => ['completed', 'failed', 'interrupted'].includes(turn.status)
      && (Number.isFinite(turn.completedAt) && turn.completedAt > 0
        || typeof turn.completedAt === 'string' && Number.isFinite(Date.parse(turn.completedAt)))
      && (turn.status !== 'failed' || turn.error && typeof turn.error === 'object');
    try {
      current();
      if (!threadId && native === null && !(generation.completedTurnIds?.length || project.activeTurnId || generation.pendingContinuation?.turnId)) {
        require(!maintenance, '上下文维护需要已有原生根会话。');
        return { ready: true, completed: [] };
      }
      const latestRoot = native?.turns?.at(-1);
      const rateLimited = latestRoot?.status === 'failed'
        && latestRoot.error?.codexErrorInfo?.responseTooManyFailedAttempts?.httpStatusCode === 429;
      const disconnected = latestRoot?.status === 'failed' && latestRoot.error?.codexErrorInfo === 'other'
        && latestRoot.error.message === 'stream disconnected before completion: error sending request';
      const retryDelayed = rateLimited || disconnected;
      const overloaded = native?.status?.type === 'systemError' && latestRoot?.status === 'failed'
        && (latestRoot.error?.codexErrorInfo === 'serverOverloaded' || retryDelayed || maintenance)
        && native.turns.every(turn => ['completed', 'failed', 'interrupted'].includes(turn.status));
      require(native && (!native.id || native.id === threadId) && (native.status?.type === 'idle' || overloaded)
        && Array.isArray(native.turns), '原生根轮次来源未完整返回。');
      require(!retryDelayed || native.turns.every(turn => ['completed', 'failed', 'interrupted'].includes(turn.status)), '原生根历史仍有未结束轮次。');
      const scope = new Set([...(generation.completedTurnIds ?? []), project.activeTurnId,
        generation.pendingContinuation?.turnId].filter(Boolean));
      const required = [...scope], roots = native.turns;
      if (maintenance) {
        require(native.id === threadId && roots.length && roots.every(turn => turn.id && ended(turn)), '上下文维护的原生根结束记录不完整。');
        for (const turn of roots) scope.add(turn.id);
      }
      for (let changed = true; changed;) {
        changed = false;
        for (const turn of roots) if (!scope.has(turn.id) && scope.has(turn.rootTurnId)) { scope.add(turn.id); changed = true; }
      }
      require(required.every(id => roots.some(turn => turn.id === id)), '本生成任务已知原生根轮次缺失，等待对账。');
      const queue = [{ id: threadId, parent: null, ancestors: [], turns: roots.filter(turn => scope.has(turn.id)) }];
      const discovered = new Map([[threadId, queue[0]]]), completed = [], communications = [];
      for (let index = 0; index < queue.length; index++) {
        const node = queue[index]; let turns = node.turns, latest, failedChild = false;
        if (node.parent) {
          const metadata = await codex.readThread(node.id, { includeTurns: false }); current();
          require(metadata?.id === node.id && metadata.parentThreadId === node.parent, '原生子任务父来源不匹配。');
          require(['idle', 'notLoaded', 'systemError'].includes(metadata.status?.type), '原生子任务仍在运行或状态未知。');
          const headers = await pages('listTurns', node.id, { limit: 50, sortDirection: 'asc', itemsView: 'notLoaded' });
          require(headers.length && headers.every(turn => turn.id && typeof turn.status === 'string'), '原生子任务轮次头缺失。');
          require(!maintenance || headers.every(ended), '上下文维护的原生子任务结束记录不完整。');
          latest = headers.at(-1); turns = [];
          failedChild = metadata.status?.type === 'systemError' && latest.status === 'failed'
            && headers.every(turn => ['completed', 'failed', 'interrupted'].includes(turn.status));
          require(['idle', 'notLoaded'].includes(metadata.status?.type) || failedChild, '原生子任务错误状态尚未确认终止。');
          // A current spawn anchors null-root headers. Reused threads need an explicit current cause.
          const scoped = new Set();
          if (maintenance) for (const turn of headers) scoped.add(turn.id);
          for (let changed = true; changed;) {
            changed = false;
            for (const turn of headers) if (!scoped.has(turn.id)
              && (scope.has(turn.rootTurnId) || scoped.has(turn.rootTurnId) || node.spawned && turn.rootTurnId == null)) {
              scoped.add(turn.id); changed = true;
            }
          }
          require(scoped.has(latest.id), '原生子任务最新轮次不属于本生成任务来源。');
          turns = headers.filter(turn => scoped.has(turn.id));
          require(turns.every(turn => ['completed', 'failed', 'interrupted'].includes(turn.status)), '原生子任务含未完成或未知轮次。');
          for (const turn of turns) scope.add(turn.id);
        }
        for (const turn of turns) for (const item of await items(node.id, turn, !node.parent)) {
          let refs = [], spawned = false;
          if (item.type === 'subAgentActivity' && item.kind === 'started') {
            require(typeof item.agentThreadId === 'string' && item.agentThreadId, '原生子任务启动引用缺失。');
            refs = [item.agentThreadId]; spawned = true;
          }
          if (item.type === 'collabAgentToolCall') {
            require(['completed', 'failed'].includes(item.status), '原生协作请求尚未完成。');
            if (item.status === 'failed' && !item.receiverThreadIds?.length) continue;
            require(Array.isArray(item.receiverThreadIds) && item.receiverThreadIds.every(id => typeof id === 'string' && id), '原生协作收件来源尚未确认。');
            refs = item.receiverThreadIds; spawned = item.tool === 'spawnAgent';
            if (!spawned) { communications.push(...refs); continue; }
          }
          for (const id of refs) {
            require(id !== node.id && !node.ancestors.includes(id), '原生委派来源存在循环。');
            const previous = discovered.get(id);
            require(!previous || previous.parent === node.id, '原生委派父来源冲突。');
            if (previous) previous.spawned ||= spawned;
            else {
              const child = { id, parent: node.id, ancestors: [...node.ancestors, node.id], spawned };
              discovered.set(id, child); queue.push(child);
            }
          }
        }
        if (node.parent) {
          const after = await codex.readThread(node.id, { includeTurns: false }); current();
          require(after?.id === node.id && after.parentThreadId === node.parent
            && (['idle', 'notLoaded'].includes(after.status?.type) || failedChild && after.status?.type === 'systemError'), '原生子任务在查询期间重新启动或来源改变。');
          const tail = await codex.listTurns(node.id, { limit: 1, sortDirection: 'desc', itemsView: 'notLoaded' }); current();
          require(tail?.data?.length === 1 && tail.data[0].id === latest.id && tail.data[0].status === latest.status, '原生子任务最新轮次在查询期间变化。');
          require(!maintenance || Object.hasOwn(tail, 'nextCursor') && (tail.nextCursor === null || typeof tail.nextCursor === 'string')
            && tail.data[0].completedAt === latest.completedAt && isDeepStrictEqual(tail.data[0].error, latest.error), '上下文维护的子任务错误或结束时间变化。');
          completed.push([node.id, latest.id, latest.status]);
        }
      }
      require(communications.every(id => discovered.has(id)), '原生通信收件者缺少本生成任务的真实委派来源。');
      const after = await codex.readThread(threadId, { includeTurns: false }); current();
      require((maintenance ? after?.id === threadId : !after?.id || after.id === threadId) && (after?.status?.type === 'idle'
        || overloaded && after?.status?.type === 'systemError'), '原生根轮次在协作查询期间重新启动。');
      if (overloaded || retryDelayed || maintenance) {
        const tail = await codex.listTurns(threadId, { limit: 1, sortDirection: 'desc', itemsView: 'notLoaded' }); current();
        require(tail?.data?.length === 1 && tail.data[0].id === latestRoot.id && tail.data[0].status === (maintenance ? latestRoot.status : 'failed')
          && isDeepStrictEqual(tail.data[0].error, latestRoot.error)
          && tail.data[0].completedAt === latestRoot.completedAt, '原生根轮次在繁忙错误恢复期间变化。');
        require(!maintenance || Object.hasOwn(tail, 'nextCursor') && (tail.nextCursor === null || typeof tail.nextCursor === 'string'), '上下文维护的根尾部分页不完整。');
      }
      if (retryDelayed && !maintenance) {
        const completion = project.events?.findLast(event => event.type === 'codex' && event.method === 'turn/completed'
          && event.child === false && event.generationId === generationId && event.params?.threadId === threadId
          && event.params.turn?.id === latestRoot.id && event.params.turn.status === 'failed'
          && isDeepStrictEqual(event.params.turn.error, latestRoot.error));
        const failureAt = Number.isFinite(latestRoot.completedAt) ? latestRoot.completedAt * 1000
          : Date.parse(latestRoot.completedAt ?? completion?.time);
        const reason = rateLimited ? '429 原生请求限流' : '原生网络传输断开';
        require(Number.isFinite(failureAt), reason + '失败时间尚未确认，保留原会话等待真实完成记录。');
        require(now() - failureAt >= 60000, reason + '退避尚未满 60 秒，保留原会话等待。');
      }
      return { ready: true, completed: completed.sort((a, b) => a[0].localeCompare(b[0])) };
    } catch (error) { return { ready: false, completed: [], reason: `原生工作尚未确认：${error.message}` }; }
  };
}

export async function createStudio({ store: suppliedStore, remote = remoteFiles.executeRemote, bridge, port: suppliedPort,
  configuration: suppliedConfiguration, configFile, setupOptions,
  traceSync = remoteFiles.syncTrace, generationConsumer, deliveryWriter = remoteFiles.writeRemoteProjectFile,
  onlineAssetStage = remoteFiles.stageOnlineAsset, producedArtifactsConsumer = remoteFiles.consumeProducedArtifacts } = {}) {
  let configuration = suppliedConfiguration || (configFile ? loadConfiguration({ file: configFile }) : startupConfiguration);
  const injectedWorkspace = suppliedStore && !suppliedConfiguration && !configFile;
  let configured = Boolean(configuration.configured || injectedWorkspace);
  const port = suppliedPort ?? configuration.value.port;
  let store = configured && suppliedStore || new Store({ root: configured ? configuration.value.workspaceRoot : configuration.setupRoot });
  let owner = await acquireWorkspaceOwnership({ workspaceRoot: store.root, configFile: configuration.file, port });
  try {
    if (configured) {
      try { await bindWorkspaceTarget(owner, configuration.value, { confirmExisting:true }); }
      catch (error) {
        if (error.code !== 'target_bound') throw error;
        await owner.release();
        configured = false;
        configuration = { ...configuration, configured:false, effectiveHash:null, diagnostics:[...configuration.diagnostics, error.message] };
        store = new Store({ root:configuration.setupRoot });
        owner = await acquireWorkspaceOwnership({ workspaceRoot:store.root, configFile:configuration.file, port });
      }
      if (configured) await store.load();
    }
  const clients = new Set(), approvals = new Map(), resumed = new Set(), completedTurns = new Set();
  const openingChats = new Set(), openingJobs = new Set(), savingSources = new Set(), background = new Set();
  const mirrorQueues = new Map(), threadProjects = new Map();
  const traceController = new AbortController();
  let eventQueue = Promise.resolve(), remoteStatus = null, recoveryTimer, saveTimer, publishTimer, closing = false, closePromise;
  let host, tools;
  const snapshot = () => ({ config: { configured, runtimeMode:configuration.value.runtimeMode,
    remoteHost: configuration.value.remoteHost, remoteRoot: configuration.value.remoteRoot, port },
    codex: codex.status(), remote: remoteStatus, projects: store.projects.map(publicProject), approvals: [...approvals.values()] });
  const emit = event => {
    if (!clients.size) return;
    if (event.kind === 'job') event = { kind: 'job', projectId: event.projectId, job: publicJob(event.job) };
    if (event.kind === 'codex') {
      const filtered = publicEvent({ type: 'codex', ...event });
      if (!filtered) return;
      event = { kind: 'codex', projectId: event.projectId, ...filtered };
    }
    const packet = `data: ${JSON.stringify(event)}\n\n`;
    for (const response of clients) if (!writeSse(response, packet)) clients.delete(response);
  };
  const publish = () => {
    if (closing || publishTimer) return;
    publishTimer = setTimeout(() => { publishTimer = null; emit({ kind: 'state', state: snapshot() }); }, 150);
  };
  const saveSoon = () => {
    if (closing || saveTimer) return;
    saveTimer = setTimeout(() => { saveTimer = null; track(store.save()); }, 200);
  };
  const track = promise => {
    const task = promise.catch(error => emit({ kind: 'error', error: error.message })).finally(() => background.delete(task));
    background.add(task);
    return task;
  };
  const projectFor = params => {
    const nested = params?.message?.params || {};
    const id = params?.threadId || params?.thread?.id || nested.threadId;
    const cwd = params?.cwd || params?.thread?.cwd || nested.cwd;
    const known = threadProjects.get(id);
    return store.projects.find(project => (!cwd || store.directory(project.id) === cwd) && (id
      ? project.threadId === id || known?.projectId === project.id && known.rootThreadId === project.threadId
        && known.generation === project.generation
      : cwd && store.directory(project.id) === cwd));
  };
  const nativeIdentities = () => new Map(store.projects.map(project => [project, { threadId: project.threadId, generation: project.generation }]));
  const currentNativeIdentity = (project, identities) => {
    const identity = identities.get(project);
    return identity && store.projects.includes(project) && project.threadId === identity.threadId && project.generation === identity.generation;
  };
  const nativeProjectFor = async (params, { read = true, parent = null, owner = null, identities = nativeIdentities() } = {}) => {
    const id = params?.threadId || params?.thread?.id || params?.message?.params?.threadId;
    const known = projectFor(params);
    if (known) return currentNativeIdentity(known, identities) && (!owner || known === owner)
      && (!parent || threadProjects.get(id)?.parentThreadId === parent) ? known : null;
    if (!id || !read) return null;
    const chain = [], seen = new Set();
    let current = id;
    try {
      while (current && !seen.has(current)) {
        seen.add(current);
        const project = projectFor({ threadId: current });
        if (project) {
          const identity = identities.get(project);
          if (!identity || !store.projects.includes(project) || project.threadId !== identity.threadId || project.generation !== identity.generation
            || owner && project !== owner || !chain.every(thread => thread.cwd === store.directory(project.id))) return null;
          for (const thread of chain) threadProjects.set(thread.id, { projectId: project.id, rootThreadId: identity.threadId,
            generation: identity.generation, parentThreadId: thread.parentThreadId });
          return project;
        }
        const supplied = current === id ? params?.thread : null;
        const thread = supplied?.source && supplied.cwd && supplied.parentThreadId
          ? supplied : await codex.readThread(current, { includeTurns: false });
        const spawn = thread?.source?.subAgent?.thread_spawn;
        if (thread?.id !== current || !thread.cwd || !thread.parentThreadId || spawn?.parent_thread_id !== thread.parentThreadId
          || current === id && parent && thread.parentThreadId !== parent) return null;
        const candidate = store.projects.find(project => store.directory(project.id) === thread.cwd), identity = identities.get(candidate);
        if (!identity || candidate.threadId !== identity.threadId || candidate.generation !== identity.generation) return null;
        chain.push(thread); current = thread.parentThreadId;
      }
    } catch {}
    return null;
  };

  function registerArtifacts(project, value) {
    project.artifactRefs ||= [];
    if (typeof value === 'string' && value.startsWith(`projects/${project.id}/`)) {
      try {
        remoteFiles.validateArtifactPath(value);
        if (!project.artifactRefs.includes(value)) project.artifactRefs.push(value);
      } catch {}
    } else if (value && typeof value === 'object') Object.values(value).forEach(child => registerArtifacts(project, child));
  }
  for (const project of store.projects) { registerArtifacts(project, project.jobs); registerArtifacts(project, project.delivery); }

  function mirrorTrace(project) {
    if (traceController.signal.aborted) return Promise.reject(traceController.signal.reason);
    let queue = mirrorQueues.get(project.id);
    if (!queue) { queue = { tail: Promise.resolve(), queued: null, hashes: new Map(), files: new Map() }; mirrorQueues.set(project.id, queue); }
    if (queue.blocked) return Promise.reject(queue.blocked);
    if (queue.queued) return queue.queued;
    const pending = queue.tail.catch(() => {}).then(async () => {
      queue.queued = null;
      if (traceController.signal.aborted) throw traceController.signal.reason;
      if (queue.blocked) throw queue.blocked;
      await store.flush(project.id);
      const directory = store.directory(project.id), archive = [], hashes = {}, files = new Map();
      const stamp = info => [info.dev, info.ino, info.size, info.mtimeNs, info.ctimeNs].join(':');
      const describe = async (name, file, completeLines = false) => {
        const source = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try {
          const info = await source.stat({ bigint: true }), size = Number(info.size), identity = { dev: Number(info.dev), ino: Number(info.ino) };
          if (!info.isFile() || !Number.isSafeInteger(size) || !Number.isSafeInteger(identity.dev) || !Number.isSafeInteger(identity.ino)) throw new Error('轨迹存档必须为普通文件');
          const current = stamp(info), cached = !completeLines && queue.files.get(name);
          if (cached?.stamp === current && queue.hashes.get(name) === cached.file.sha256) {
            files.set(name, cached);
            return cached.file;
          }
          let bytes = size;
          if (completeLines) {
            bytes = 0;
            for (let end = size; end > 0;) {
              const start = Math.max(0, end - 65536), block = Buffer.allocUnsafe(end - start);
              const read = await source.read(block, 0, block.length, start);
              if (read.bytesRead !== block.length) throw new Error('轨迹固定范围在读取时被截断');
              const newline = block.lastIndexOf(10);
              if (newline >= 0) { bytes = start + newline + 1; break; }
              end = start;
            }
          }
          const digest = createHash('sha256'); let read = 0;
          if (bytes) for await (const chunk of source.createReadStream({ start: 0, end: bytes - 1, autoClose: false })) {
            read += chunk.length; digest.update(chunk);
          }
          const after = await source.stat({ bigint: true });
          if (read !== bytes || after.size < BigInt(bytes) || !completeLines && stamp(after) !== current) throw new Error('轨迹存档固定范围已变化');
          const described = { name, path: file, bytes, sha256: digest.digest('hex'), identity };
          if (!completeLines) files.set(name, { stamp: current, file: described });
          return described;
        } finally { await source.close(); }
      };
      const collect = async relative => {
        let entries;
        try {
          if ((await lstat(path.join(directory, relative))).isSymbolicLink()) throw new Error('轨迹存档不能引用软链接');
          entries = await readdir(path.join(directory, relative), { withFileTypes: true });
        }
        catch (error) { if (error.code === 'ENOENT') return; throw error; }
        for (const entry of entries) {
          const name = path.posix.join(relative, entry.name);
          if (entry.isSymbolicLink()) throw new Error('轨迹存档不能引用软链接');
          if (entry.isDirectory()) await collect(name);
          else if (entry.isFile()) {
            const file = await describe(name, path.join(directory, name));
            if (queue.hashes.get(name) !== file.sha256) { archive.push(file); hashes[name] = file.sha256; }
          }
        }
      };
      try {
        const trace = await describe('generation-trace.jsonl', path.join(directory, 'trace.jsonl'), true);
        await collect('.trace');
        const hash = trace.sha256;
        if (!archive.length && queue.hashes.get(trace.name) === hash) {
          queue.files = files;
          return { hash, receipt: queue.receipt, unchanged: true };
        }
        const receipt = await traceSync(project.id, trace, archive, { signal:traceController.signal });
        if (receipt?.hashes?.['generation-trace.jsonl'] !== hash
          || Object.entries(hashes).some(([name, value]) => receipt.hashes[name] !== value)) throw new Error('轨迹同步摘要不匹配');
        for (const [name, value] of Object.entries(hashes)) queue.hashes.set(name, value);
        queue.hashes.set(trace.name, hash); queue.files = files; queue.receipt = receipt;
        project.traceSyncedHash = hash; project.traceSyncedAt = new Date().toISOString();
        delete project.traceSyncError;
        await store.save(); publish();
        return { hash, receipt };
      } catch (error) {
        queue.hashes.clear();
        queue.files.clear();
        if (error.exitUnconfirmed) queue.blocked = error;
        project.traceSyncError = error.message;
        await store.save(); publish(); throw error;
      }
    });
    queue.tail = queue.queued = pending;
    return pending;
  }

  const onCodexEvent = event => {
    const approvalResolvedAt = event.method === 'serverRequest/resolved' ? (host?.now() ?? Date.now()) : undefined;
    const identities = nativeIdentities();
    if (event.method === 'turn/completed') completedTurns.add(event.params.turn.id);
    eventQueue = eventQueue.then(async () => {
      const params = event.params || {};
      const resolvedApproval = event.method === 'serverRequest/resolved' ? approvals.get(String(params.requestId)) : null;
      let project = await nativeProjectFor(params, { identities, read: !event.method.startsWith('transport/') && !event.method.startsWith('bridge/') })
        || (resolvedApproval?.projectId ? store.get(resolvedApproval.projectId) : null);
      const threadId = params.threadId || params.thread?.id || params.message?.params?.threadId;
      const activity = params.item;
      if (project && activity?.type === 'subAgentActivity' && ['started', 'interacted', 'completed'].includes(activity.kind)) {
        await nativeProjectFor({ threadId: activity.agentThreadId }, { parent: threadId, owner: project, identities });
      }
      if (project && !currentNativeIdentity(project, identities)) project = null;
      const child = !!(project?.threadId && threadId && threadId !== project.threadId);
      if (event.method === 'serverRequest/resolved') {
        approvals.delete(String(params.requestId));
        if (project) track(host.approvalsChanged(project, approvalResolvedAt));
      }
      if (event.method === 'bridge/status' && !params.ready) {
        approvals.clear(); resumed.clear();
        for (const item of store.projects) {
          if (item.activeTurnId) item.activeTurnId = null;
          if (item.generation && !['complete', 'cancelled', 'failed'].includes(item.generation.status)) item.generation.status = item.generation.waitingSystem ? 'waiting_system' : 'recovering';
        }
      }
      if (project) {
        await store.record(project.id, { type: 'codex', method: event.method, params, child });
        if (!child && event.method === 'item/agentMessage/delta') {
          let message = project.messages.find(item => item.id === params.itemId);
          if (!message) { message = { id: params.itemId, role: 'assistant', content: '', status: 'streaming' }; project.messages.push(message); }
          message.content += params.delta || '';
        }
        if (!child && event.method === 'item/completed' && params.item?.type === 'agentMessage') {
          let message = project.messages.find(item => item.id === params.item.id);
          if (!message) { message = { id: params.item.id, role: 'assistant' }; project.messages.push(message); }
          message.content = params.item.text || message.content || ''; message.status = 'completed';
        }
        if (!child && event.method === 'turn/started') project.activeTurnId = params.turn.id;
        if (event.method === 'item/completed' && ['fileChange', 'commandExecution'].includes(params.item?.type)) await store.sourceSnapshot(project.id, params.item.id);
        if (!child && event.method === 'turn/completed') {
          for (const [id, approval] of approvals) if (approval.projectId === project.id && (!approval.params?.turnId || approval.params.turnId === params.turn.id)) approvals.delete(id);
          track(host.approvalsChanged(project));
          project.activeTurnId = null;
          try { await store.sourceSnapshot(project.id, params.turn.id); }
          catch (error) {
            await store.record(project.id, { type: 'source_snapshot_failed', turnId: params.turn.id, error: error.message });
            emit({ kind: 'error', error: error.message });
          }
          if (params.turn.error) project.messages.push({ role: 'system', content: params.turn.error.message || JSON.stringify(params.turn.error) });
          if (!closing) {
            track(mirrorTrace(project));
            // Host may send native messages. Never await it inside the native event queue.
            track(host.turnCompleted(project, params.turn));
          }
        }
      }
      if (['turn/started', 'turn/completed', 'bridge/status', 'serverRequest/resolved', 'item/completed', 'error'].includes(event.method)) await store.save();
      else saveSoon();
      emit({ kind: 'codex', projectId: project?.id, child, ...event }); publish();
    }).catch(error => emit({ kind: 'error', error: error.message }));
    return eventQueue;
  };
  const onRequest = request => {
    const identities = nativeIdentities();
    eventQueue = eventQueue.then(async () => {
      const project = await nativeProjectFor(request.params, { identities });
      const record = { projectId: project?.id, id: request.id, method: request.method, params: request.params };
      approvals.set(String(request.id), record);
      if (project) await store.record(project.id, { type: 'approval_requested', ...request });
      if (project && host) track(host.approvalsChanged(project));
      emit({ kind: 'approval', ...record }); publish();
    }).catch(error => emit({ kind: 'error', error: error.message }));
    return eventQueue;
  };
  const codex = !configured ? { status:()=>({ ready:false, setupRequired:true }), async close(){} }
    : bridge || new CodexBridge({ cwd: ROOT, onEvent: onCodexEvent, onRequest, networkAccess: true, model: configuration.value.codexModel,
    command: [...resolveCodexCommand(configuration.value), 'app-server', '--listen', 'stdio://', '-c', 'web_search="live"'],
    skillsRoot: path.join(ROOT, 'skills'), skillPath: path.join(ROOT, 'skills/scene-generation/SKILL.md'),
    mcp: { command: process.execPath, args: [path.join(ROOT, 'src/mcp.mjs')],
      env: { SCENEAGENT_URL: `http://127.0.0.1:${port}` }, requiredTools: Object.keys(TOOL_SCHEMAS) } });
  if (configured && bridge) { bridge.onEvent = onCodexEvent; bridge.onRequest = onRequest; }

  function instructions(project) {
    return `你是 SceneAgent 场景工程师，和用户用通俗中文交流，保留完整原生文件与工具能力。\n` +
      `工程目录：${store.directory(project.id)}；内核目录：${ROOT}。原始要求在 request.md。\n` +
      `生成时显式使用 scene-generation 技能，按需读其 references/api.md；仅在本次 requiredOutputs 包含 demonstrations 时自主试验并产出可独立重跑的冻结闭环行为程序。完整项目与生成任务标识以当前输入为准。\n` +
      (project.generation?.recordOnlyVideos ? recordingInstruction(project)
        : '源程序保留在工作台主机，资产与执行数据在仿真主机。你自主召集直接子代理 scene_quality_review 专职独立审核；它不兼任作者、不再委派，按技能独立审核，你自主修正并复审当前版本。交付提供 review 原生最终指针，宿主只核回执来源、当前版本绑定和固定执行集合，本次选定产物的消费者检查技术完整性；具体质量由内部审核者判断。\n') +
      (!project.generation?.recordOnlyVideos && project.generation?.deliveryContract === 'producer-exports-v1' ? `本次采用 producer-exports-v1 交付合同：你及子代理可写本工程 delivery/ 和仿真主机的 ${config.remoteRoot}/projects/${project.id}/delivery/；在其中自主编写、运行和修复交付程序与隔离依赖，按 references/delivery.md 输出并实际读取本次选定产物，内部审核回执绑定 producedArtifacts 清单。物理源不得导入 delivery/，修格式无需重建场景。\n` : '') +
      (!project.generation?.recordOnlyVideos && project.generation?.videoOnly ? '本次 videoOnly=true：机器人产品只交完整原始执行视频及来源映射，不生成 LeRobot/HDF5 或训练数据。按 references/delivery.md 的仅视频合同交付；真实执行、成功证据和独立内审仍保留。\n' : '') +
      `你及所有子代理可写当前生成工程的源程序和必要辅助文件，以及仿真主机当前数据根 ${config.remoteRoot}/assets/imports/${project.id}/ 或 ${config.remoteRoot}/assets/imports/robots/${project.id}/ 下的独立新目录。已有库文件、其他工程与旧仓库保持只读；不执行模型附带脚本。Studio 内核目录中的 src、runtime、tests、skills、配置、宿主状态与服务均只读。普通超时由你读取原作业阶段、performance和实际预算，诊断后调整项目或workerTimeoutSeconds并决定新的有意尝试；不要原样盲目重跑，不降低原任务或必要记录要求。只有确认重大阻碍无法在项目与公开执行权限内解决时，用generation_wait_system说明实际证据及为何现有处理手段不足，收到回执后结束当前轮次，由系统开发代理处理；不得为修改系统源码、配置、技能或服务自行修复、申请提权或重启服务。`;
  }
  function recordingInstruction(project) {
    return `本次用户显式 recordOnlyVideos=true、videoOnly=true、recordOnlyTaskIds=${JSON.stringify(project.generation.recordOnlyTaskIds)}：此要求优先于历史 producer-exports-v1、内审和交付要求，跳过所有视频的独立审核和交付检查；不召集 scene_quality_review，不制作 producedArtifacts，不调用 project_deliver，不导出训练数据。每个锁定任务保留一条真实机器人从原初态完整完成原任务的成功视频，已有真实成功记录直接复用，完成后只报告原始视频引用、taskId、revision、episodeId。原场景、物理、任务完成条件及全部失败、中断、源和执行记录保持。\n`;
  }
  async function archiveInput(project, value) {
    const serialized = JSON.stringify(value), inputHash = contentHash(serialized);
    const snapshotRef = `.trace/inputs/${inputHash}.json`;
    await mkdir(path.join(store.directory(project.id), '.trace/inputs'), { recursive: true });
    await writeFile(path.join(store.directory(project.id), snapshotRef), serialized);
    await store.record(project.id, { type: 'input_snapshot', kind: value.kind, snapshotRef, inputHash,
      coverage: 'candidate_snapshot_actual_submission_is_transport_sent' });
    return snapshotRef;
  }
  async function ensureThread(project, expectedThreadId = null) {
    const generation = project.generation;
    const current = () => {
      if (expectedThreadId && (project.threadId !== expectedThreadId || project.generation !== generation)) {
        throw Object.assign(new Error('重新附着时生成任务或原生会话已变化。'), { status: 409 });
      }
    };
    current();
    await codex.start();
    current();
    if (!project.threadId) {
      const text = instructions(project);
      project.threadInstructionsRef = await archiveInput(project, { kind: 'thread_instructions', text });
      const thread = await codex.startThread({ cwd: store.directory(project.id), instructions: text });
      project.threadId = thread.id; resumed.add(thread.id);
    } else if (expectedThreadId || !resumed.has(project.threadId)) {
      const thread = await codex.resumeThread(project.threadId, { cwd: store.directory(project.id) });
      current();
      if (expectedThreadId && thread?.id !== expectedThreadId) throw Object.assign(new Error('重新附着返回的原生会话标识不匹配。'), { status: 409 });
      resumed.add(project.threadId);
    }
    await store.save();
    current();
  }
  async function nativeTurn(project, input, { mode = 'generate', eventId, beforeSend } = {}) {
    let nativeInputInvoked = false, unsentGuardError;
    try {
      const generation = project.generation;
      const ready = () => {
        try {
          if (project.generation !== generation || generation?.waitingSystem) throw Object.assign(new Error('生成任务正在等待系统处理，请显式恢复后再启动原生轮次。'), { status: 409 });
          if (mode === 'generate' && generation?.cancelRequestedAt) throw Object.assign(new Error('生成任务正在停止，不能启动新轮次。'), { status: 409 });
          beforeSend?.();
          if (mode === 'generate' && !beforeSend) host.assertBudget(generation);
        } catch (error) { unsentGuardError = error; throw error; }
      };
      ready();
      await ensureThread(project);
      ready();
      let sourceObservation;
      try { await store.sourceSnapshot(project.id, eventId || 'before_native_input'); }
      catch (error) {
        if (mode !== 'generate') throw error;
        sourceObservation = { sourceHash: null, sourceUnavailable: error.message };
      }
      ready();
      const diagnostic = sourceObservation ? `准备本轮时源码快照读取失败，当前源码版本未知：${JSON.stringify(sourceObservation)}。请在现有项目权限内检查；本记录不构成系统故障判定。\n` : '';
      const scopeInstruction = mode === 'generate' && !requiredOutputs(project.generation.requiredOutputs).includes('demonstrations')
        ? '本次为纯场景模式：不选择或加载机械臂/机器人，不编写或运行专家/独立行为采集，机器人接入留待后续轨迹阶段。必要的家具关节、被动物理和评分验证可使用冻结 case 验证入口及空动作 env.step；不以机器人成功任务作为环境交付前置。\n' : '';
      const text = mode === 'generate' ? `项目 projectId=${project.id}；生成任务 generationId=${project.generation.id}；需求版本=${project.generation.requirementVersion}。\n本次交付范围 requiredOutputs=${JSON.stringify(project.generation.requiredOutputs)}；只读冻结环境 environmentRefs=${JSON.stringify(project.generation.environmentRefs ?? [])}。\n${project.generation.recordOnlyVideos ? recordingInstruction(project) : project.generation.videoOnly ? 'videoOnly=true：仅交完整执行视频与来源映射，按交付文档绑定 videoOnly，不导出训练格式。\n' : ''}${scopeInstruction}${diagnostic}${input}` : input;
      const skillPath = path.join(ROOT, 'skills/scene-generation/SKILL.md');
      const skillText = mode === 'generate' ? await readFile(skillPath, 'utf8') : null;
      const inputRef = await archiveInput(project, { kind: 'native_turn_input', text, mode,
        ...(sourceObservation ? { sourceObservation } : {}),
        instructionsRef: project.threadInstructionsRef || null,
        skillReference: skillText === null ? null : { path: skillPath, text: skillText, sha256: contentHash(skillText), coverage: 'submitted_reference' },
        apiReferenceCoverage: 'not_claimed_loaded' });
      await store.record(project.id, { type: 'native_input', input: text, mode, continuationId: eventId || null,
        threadId: project.threadId, cwd: store.directory(project.id), inputRef });
      ready();
      nativeInputInvoked = true;
      const turn = project.activeTurnId ? await codex.steer(project.threadId, project.activeTurnId, text, { beforeSend: ready })
        : await codex.turn(project.threadId, text, { cwd: store.directory(project.id), skill: mode === 'generate', beforeSend: ready });
      await eventQueue;
      if (turn?.id && !completedTurns.has(turn.id)) project.activeTurnId = turn.id;
      await store.save(); publish();
      return turn?.id && completedTurns.has(turn.id) ? { ...turn, status: 'completed' } : turn;
    } catch (error) {
      if (!nativeInputInvoked || error === unsentGuardError) {
        if (!(error instanceof Error)) error = new Error(String(error));
        error.nativeInputNotSent = true;
      } else if (error && typeof error === 'object') error.nativeInputNotSent = false;
      if (!beforeSend && error?.nativeInputNotSent === true && error.budgetExpired === true) await host.recover(project);
      throw error;
    }
  }
  const stopWork = async (project, { turnId } = {}) => {
    const operations = [];
    if (project.threadId && turnId) operations.push(Promise.resolve().then(() => codex.interrupt(project.threadId, turnId)));
    for (const job of project.jobs) if (job.durable && job.generationId === project.generation?.id
      && !['completed', 'failed', 'cancelled', 'interrupted'].includes(job.status)) {
      operations.push(Promise.resolve().then(() => tools.refreshJob(project, job, { action: 'cancel' })));
    }
    const stopped = await Promise.allSettled(operations);
    const failed = stopped.filter(row => row.status === 'rejected');
    if (failed.length) throw new Error(failed.map(row => row.reason.message || String(row.reason)).join('；'));
  };
  host = new GenerationHost({ store, codex, stopWork, publish, ensureThread, readNativeWork: createNativeWorkReader({ codex }), hasApprovals: project => [...approvals.values()].some(item => item.projectId === project.id),
    hasOpeningInput: project => openingChats.has(project.id) || openingJobs.has(project.id) || savingSources.has(project.id),
    continueTurn: (project, input, options) => nativeTurn(project, input, options) });
  tools = createTools({ store, remote, publish, onSystemWait: (project, args) => host.waitSystem(project, args), sourceBusy: id => savingSources.has(id),
    stageOnlineAsset: onlineAssetStage,
    syncTrace: mirrorTrace,
    consumeProducedArtifacts: producedArtifactsConsumer,
    readInternalReview: createInternalReviewReader({ store, codex, publishSnapshot: deliveryWriter }),
    consumeGeneration: async project => {
      const consume = generationConsumer || (async current => remoteFiles.consumeRemoteGeneration(current.id, current.generation.id,
        await readFile(path.join(ROOT, 'examples/read_generation.py'), 'utf8')));
      try {
        const report = await consume(project);
        if (report.passed !== true) mirrorQueues.get(project.id)?.hashes.clear();
        registerArtifacts(project, report); await store.save(); return report;
      } catch (error) { mirrorQueues.get(project.id)?.hashes.clear(); throw error; }
    },
    onDelivery: async (project, receipt) => {
      const superseded = () => project.generation?.recordOnlyVideos === true && receipt.recordOnlyVideos !== true;
      const current = () => {
        const generation = project.generation;
        return !superseded() && (!receipt.accepted || (!generation?.cancelRequestedAt && !['cancel_requested', 'cancelled', 'failed'].includes(generation?.status)
          && receipt.generationId === generation?.id && receipt.requirementVersion === generation.requirementVersion
          && receipt.requestHash === generation.requestHash));
      };
      const deliveryId = 'd_' + contentHash(`${project.id}:${receipt.requestId}`).slice(0, 24);
      const data = { ...receipt, deliveryId, artifactRef: `projects/${project.id}/deliveries/${deliveryId}/delivery.json` };
      let written = false;
      const aborted = async () => {
        if (project.delivery === data) { delete project.delivery; delete project.artifacts.delivery; }
        const diagnostic = { ...receipt, accepted: false, status: 'incomplete', publicationAborted: true,
          missing: [...receipt.missing, superseded() ? '用户已切换原始录像模式，旧正式消费回执只保留历史，不覆盖当前录像结果' : '任务已停止或需求已变化，回执未发布为当前交付'],
          candidateArtifactRef: written ? data.artifactRef : null };
        await store.record(project.id, { type: 'delivery_publication_aborted', receipt: diagnostic });
        await store.save(); publish();
        return diagnostic;
      };
      if (superseded()) {
        await deliveryWriter(project.id, `deliveries/${deliveryId}/delivery.json`, data, { immutable: true });
        written = true;
        return aborted();
      }
      if (!current()) return aborted();
      await deliveryWriter(project.id, `deliveries/${deliveryId}/delivery.json`, data, { immutable: true });
      written = true;
      if (!current()) return aborted();
      await deliveryWriter(project.id, 'delivery.json', { deliveryId, reference: data.artifactRef,
        generationId: data.generationId, revision: data.revision });
      if (!current()) return aborted();
      project.delivery = data;
      project.artifacts.delivery = `/api/projects/${project.id}/artifacts/delivery.json?file=${encodeURIComponent(data.artifactRef)}`;
      registerArtifacts(project, data); await store.record(project.id, { type: 'delivery_receipt', receipt: data });
      if (!current()) return aborted();
      await store.save(); publish();
      return current() ? data : aborted();
    },
    onJobTerminal: async (project, job) => {
      job.endedAt ||= new Date().toISOString(); registerArtifacts(project, job.result); registerArtifacts(project, job.delivery);
      await store.save(); emit({ kind: 'job', projectId: project.id, job }); publish();
      if (!closing) { track(mirrorTrace(project)); track(host.jobCompleted(project, job)); }
    } });

  async function chat(project, message, mode = 'generate', { timeBudgetMs, requiredOutputs, environmentRefs, videoOnly, recordOnlyVideos, recordOnlyTaskIds } = {}) {
    if (typeof message !== 'string' || !message.trim() || message.length > 50000 || !['generate', 'discussion'].includes(mode)) throw Object.assign(new Error('请输入有效消息与对话方式'), { status: 400 });
    if (mode !== 'generate' && (timeBudgetMs !== undefined || requiredOutputs !== undefined || environmentRefs !== undefined || videoOnly !== undefined || recordOnlyVideos !== undefined || recordOnlyTaskIds !== undefined)) throw Object.assign(new Error('生成选项只用于启动生成任务。'), { status: 400 });
    if (openingChats.has(project.id) || savingSources.has(project.id)) throw Object.assign(new Error('工程正在处理另一条操作，请稍后发送'), { status: 409 });
    openingChats.add(project.id);
    try {
      if (mode === 'generate') {
        const existed = !!project.generation;
        store.beginGeneration(project.id, { timeBudgetMs, requiredOutputs, environmentRefs, videoOnly, recordOnlyVideos, recordOnlyTaskIds });
        if (existed || message.trim() !== project.prompt.trim()) await store.addRequirement(project.id, message.trim());
      }
      project.messages.push({ id: randomUUID(), role: 'user', content: message.trim() });
      await store.record(project.id, { type: 'user_message', message: message.trim(), mode,
        ...(mode === 'generate' ? { timeBudgetMs: project.generation.timeBudgetMs } : {}) });
      await store.save(); publish();
      if (project.generation?.waitingSystem) return { saved: true, status: 'waiting_system' };
      return await nativeTurn(project, message.trim(), { mode });
    } finally { openingChats.delete(project.id); }
  }

  async function debugJob(project, operation, input = {}) {
    if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some(key => key !== 'workerTimeoutSeconds' && (operation !== 'rollout' || key !== 'taskIds'))
      || input.taskIds !== undefined && (!Array.isArray(input.taskIds) || !input.taskIds.length
        || input.taskIds.some(id => typeof id !== 'string' || !id))
      || input.workerTimeoutSeconds !== undefined && (typeof input.workerTimeoutSeconds !== 'number'
        || !Number.isFinite(input.workerTimeoutSeconds) || input.workerTimeoutSeconds <= 0)) {
      throw Object.assign(new Error('构建/执行参数无效：任务选择须为非空标识列表，workerTimeoutSeconds须为正有限秒数。'), { status:400 });
    }
    if (openingJobs.has(project.id) || savingSources.has(project.id)) throw Object.assign(new Error('工程正在处理另一个操作，请稍后构建'), { status: 409 });
    openingJobs.add(project.id);
    try {
      let taskIds;
      if (operation === 'rollout') {
        if (!project.latestBuild?.revision) throw Object.assign(new Error('请先构建场景'), { status: 409 });
        taskIds = input.taskIds === undefined ? (project.latestBuild.tasks || []).map(task => task.id) : input.taskIds;
        if (!taskIds.length || !taskIds.every(id => project.latestBuild.tasks?.some(task => task.id === id))) {
          throw Object.assign(new Error('任务标识不属于当前场景版本'), { status: 409 });
        }
      }
      store.beginGeneration(project.id);
      const scope = { projectId: project.id, generationId: project.generation.id, requestId: 'r_' + randomUUID().replaceAll('-', ''),
        ...(input.workerTimeoutSeconds !== undefined ? { workerTimeoutSeconds: input.workerTimeoutSeconds } : {}) };
      if (operation === 'build') {
        const source = await store.sourceSnapshot(project.id, 'debug_build');
        return (await tools.call('scene_build', { ...scope, expectedSourceHash: source.sourceHash }, { actor: 'manual' })).structuredContent;
      }
      return (await tools.call('robot_run', { ...scope, revision: project.latestBuild.revision, taskIds }, { actor: 'manual' })).structuredContent;
    } finally { openingJobs.delete(project.id); }
  }

  async function serveArtifact(request, response, project, filename, url) {
    const relative = url.searchParams.get('file');
    remoteFiles.validateArtifactPath(relative);
    if (!relative.startsWith(`projects/${project.id}/`) || !project.artifactRefs?.includes(relative) || path.posix.basename(relative) !== decodeURIComponent(filename)) throw Object.assign(new Error('产物不存在或不属于这个项目'), { status: 404 });
    const info = await remoteFiles.statRemoteFile(relative);
    const range = parseRange(request.headers.range, info.size);
    if (range === false) { response.writeHead(416, { 'Content-Range': `bytes */${info.size}` }); response.end(); return; }
    const headers = { 'Content-Type': mime[path.extname(relative)] || 'application/octet-stream', 'Accept-Ranges': 'bytes',
      'Content-Length': range?.length ?? info.size, 'Cache-Control': 'private, max-age=3600' };
    if (range) headers['Content-Range'] = `bytes ${range.start}-${range.end}/${info.size}`;
    response.writeHead(range ? 206 : 200, headers);
    const child = remoteFiles.openRemoteFile(relative, range ? { start: range.start, length: range.length } : {});
    child.stdin.end(); response.on('close', () => child.kill()); child.stderr.on('data', () => {});
    child.on('error', error => response.destroy(error));
    child.on('close', code => { if (code !== 0 && !response.writableEnded) response.destroy(new Error('远端文件流中断')); });
    await pipeline(child.stdout, response);
  }

  let setupReaders = 0;
  const resourceBusy = () => {
    const active = store.projects.filter(project => project.activeTurnId || openingChats.has(project.id)
      || openingJobs.has(project.id) || savingSources.has(project.id)
      || project.jobs.some(job => ['queued','running','unknown'].includes(job.status))
      || project.generation && !['complete','cancelled','failed','draft'].includes(project.generation.status));
    return [...(setupReaders ? ['当前请求'] : []), ...active.map(project => project.id),
      ...[...approvals.keys()].map(id => 'approval:' + id)];
  };
  const setup = await createSetup({ configuration, owner, stateRoot:configuration.setupRoot, resourceBusy, ...setupOptions });

  const server = http.createServer(async (request, response) => {
    let usesResources = false;
    try {
      const hostname = request.headers.host || '';
      const hostPort = Number(hostname.split(':').at(-1));
      if (!/^(?:127\.0\.0\.1|localhost):(?:0|[1-9]\d{0,4})$/.test(hostname) || hostPort > 65535 || hostPort === 0 && port !== 0)
        throw Object.assign(new Error('仅接受本机访问'), { status: 403 });
      const url = new URL(request.url, `http://${hostname}`);
      if (request.headers.origin && request.headers.origin !== `http://${hostname}`) throw Object.assign(new Error('跨来源访问已拒绝'), { status: 403 });
      const parts = url.pathname.split('/').filter(Boolean);
      if (url.pathname === '/api/setup' && request.method === 'GET') return json(response, await setup.inspect());
      if (url.pathname === '/api/setup/probe' && request.method === 'POST') return json(response, await setup.probe(await body(request)));
      if (url.pathname === '/api/setup/config' && request.method === 'PUT') return json(response, await setup.saveConfig(await body(request)));
      if (url.pathname === '/api/setup/install' && request.method === 'POST') return json(response, await setup.install(await body(request)), 202);
      if (parts[0] === 'api' && parts[1] === 'setup' && parts[2] === 'install' && parts[3]) {
        if (parts.length === 4 && request.method === 'GET') return json(response, await setup.installation(parts[3], Number(url.searchParams.get('cursor') ?? 0)));
        if (parts.length === 5 && parts[4] === 'cancel' && request.method === 'POST') {
          await body(request); return json(response, await setup.cancel(parts[3]));
        }
      }
      if (!configured && parts[0] === 'api' && !['state','events','setup'].includes(parts[1]))
        throw Object.assign(new Error('请保存并启用设置，然后重启工作台。'), { status:503, code:'setup_required' });
      if (url.pathname === '/api/assets' || url.pathname === '/api/health' ||
          parts[0] === 'api' && request.method !== 'GET' && !['interrupt','cancel','job'].includes(parts[3] || parts[2])) {
        setup.assertAvailable('generation');
        usesResources = true; setupReaders++;
      }
      if (url.pathname === '/api/events' && request.method === 'GET') {
        response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
        if (!writeSse(response, `data: ${JSON.stringify({ kind: 'state', state: snapshot() })}\n\n`)) return;
        for (const approval of approvals.values()) if (!writeSse(response, `data: ${JSON.stringify({ kind: 'approval', ...approval })}\n\n`)) return;
        clients.add(response);
        const heartbeat = setInterval(() => { if (!writeSse(response, ': keepalive\n\n')) clients.delete(response); }, 15000);
        request.on('close', () => { clients.delete(response); clearInterval(heartbeat); }); return;
      }
      if (url.pathname === '/api/state' && request.method === 'GET') return json(response, snapshot());
      if (url.pathname === '/api/health' && request.method === 'GET') { remoteStatus = await remote({ operation: 'health' }, { timeoutMs: 30000 }); publish(); return json(response, remoteStatus); }
      if (url.pathname === '/api/assets' && request.method === 'GET') return json(response, await remote({ operation: 'assets', query: url.searchParams.get('q') || '' }, { timeoutMs: 40000 }));
      if (parts[0] === 'api' && parts[1] === 'tools' && parts.length === 3 && request.method === 'POST') {
        const actor = request.headers['x-sceneagent-origin'] === 'codex-mcp' ? 'codex-mcp' : 'manual';
        return json(response, await tools.call(parts[2], await body(request), { actor }));
      }
      if (url.pathname === '/api/projects' && request.method === 'POST') { const project = await store.create(await body(request)); publish(); return json(response, project, 201); }
      if (parts[0] === 'api' && parts[1] === 'approvals' && request.method === 'POST') {
        const approval = approvals.get(parts[2]);
        if (!approval) throw Object.assign(new Error('审批已失效'), { status: 404 });
        const { decision, answers } = await body(request);
        if (!['accept', 'decline'].includes(decision)) throw Object.assign(new Error('审批结果无效'), { status: 400 });
        let result;
        if (['item/commandExecution/requestApproval', 'item/fileChange/requestApproval'].includes(approval.method)) result = { decision };
        else if (approval.method === 'item/tool/requestUserInput') result = { answers: answers || {} };
        else if (approval.method === 'item/permissions/requestApproval') result = { permissions: {}, scope: 'turn' };
        else throw Object.assign(new Error('这个原生请求需要在 Codex 中处理'), { status: 422 });
        if (approval.projectId) await store.record(approval.projectId, { type: 'approval_response', requestId: approval.id, result });
        await codex.respond(approval.id, result); approvals.delete(parts[2]);
        if (approval.projectId) await host.approvalsChanged(store.get(approval.projectId));
        publish(); return json(response, { resolved: true });
      }
      if (parts[0] === 'api' && parts[1] === 'projects' && parts[2]) {
        const project = store.get(parts[2]);
        if (parts.length === 3 && request.method === 'GET') return json(response, publicProject(project));
        if (parts[3] === 'sources') {
          if (request.method === 'GET') return json(response, await store.sources(project.id));
          if (request.method === 'PUT') {
            const sources = await body(request);
            if (savingSources.has(project.id) || openingJobs.has(project.id) || openingChats.has(project.id)
              || project.jobs.some(job => job.durable && ['queued', 'running', 'unknown'].includes(job.status))) throw Object.assign(new Error('工程正在处理另一个操作，请稍后保存'), { status: 409 });
            savingSources.add(project.id);
            try { await store.updateSources(project.id, sources); } finally { savingSources.delete(project.id); }
            publish(); return json(response, { saved: true });
          }
        }
        if (parts[3] === 'chat' && request.method === 'POST') { const input = await body(request); return json(response, await chat(project, input.message, input.mode || 'generate', { timeBudgetMs: input.timeBudgetMs, requiredOutputs: input.requiredOutputs, environmentRefs: input.environmentRefs, videoOnly: input.videoOnly, recordOnlyVideos: input.recordOnlyVideos, recordOnlyTaskIds: input.recordOnlyTaskIds })); }
        if (parts[3] === 'resume' && request.method === 'POST') {
          const { generationId } = await body(request);
          return json(response, await host.resume(project, { generationId }));
        }
        if (parts[3] === 'interrupt' && request.method === 'POST') {
          if (project.generation) await host.cancel(project);
          else await stopWork(project, { turnId: project.activeTurnId });
          return json(response, { interrupted: true });
        }
        if (['build', 'rollout'].includes(parts[3]) && request.method === 'POST') return json(response, await debugJob(project, parts[3], await body(request)), 202);
        if (parts[3] === 'artifacts' && parts[4] && request.method === 'GET') return await serveArtifact(request, response, project, parts[4], url);
      }
      if (request.method === 'GET' && !url.pathname.startsWith('/api/')) {
        let relative = decodeURIComponent(url.pathname);
        const base = relative.startsWith('/vendor/three/') ? path.join(ROOT, 'node_modules/three') : path.join(ROOT, 'web');
        relative = relative.startsWith('/vendor/three/') ? relative.slice('/vendor/three/'.length) : relative === '/' ? 'index.html' : relative.slice(1);
        const file = path.resolve(base, relative);
        if (!file.startsWith(base + path.sep)) throw Object.assign(new Error('文件路径无效'), { status: 403 });
        const info = await stat(file);
        if (!info.isFile()) throw Object.assign(new Error('页面不存在'), { status: 404 });
        response.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Content-Length': info.size, 'Cache-Control': 'no-cache' });
        return await pipeline(createReadStream(file), response);
      }
      throw Object.assign(new Error('接口不存在'), { status: 404 });
    } catch (error) {
      if (response.headersSent) response.destroy(error);
      else json(response, { error: error.message, ...(error.status && error.code ? { code:error.code } : {}) }, error.status || (error.code === 'ENOENT' ? 404 : 500));
    } finally { if (usesResources) setupReaders--; }
  });
  return { server, store, codex, tools, host:configured ? host : null, setup,
    async start() {
      try { await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); }); }
      catch (error) { await setup.close(); await owner.release(); throw error; }
      const address = `http://127.0.0.1:${server.address().port}`;
      if (!configured) return address;
      if (codex.mcp) codex.mcp.env.SCENEAGENT_URL = address;
      track(codex.start().then(publish));
      const recover = () => { for (const project of store.projects) if (project.generation && ['active', 'cancel_requested', 'recovering', 'waiting_jobs', 'waiting_user', 'waiting_system'].includes(project.generation.status)) track(host.recover(project)); };
      recover(); recoveryTimer = setInterval(recover, 15000); recoveryTimer.unref();
      return address;
    },
    close() {
      if (closePromise) return closePromise;
      closePromise = (async () => {
        closing = true; clearInterval(recoveryTimer); clearTimeout(saveTimer); clearTimeout(publishTimer);
        traceController.abort(new Error('工作台关闭，轨迹传输已中断。'));
        const preparationStopped = Promise.all([tools.close(), setup.close()]);
        const stopped = new Promise(resolve => server.close(resolve));
        for (const response of clients) response.end(); server.closeAllConnections();
        await preparationStopped; await codex.close(); await eventQueue;
        await Promise.allSettled([...mirrorQueues.values()].map(queue => queue.tail));
        for (const project of store.projects) {
          project.activeTurnId = null;
          if (project.generation && !['complete', 'failed', 'cancelled'].includes(project.generation.status)) project.generation.status = project.generation.waitingSystem ? 'waiting_system' : 'recovering';
        }
        if (configured) await store.save();
        while (background.size) await Promise.all([...background]);
        if (configured) await store.save();
        await stopped;
      })().finally(() => owner.release());
      return closePromise;
    }
  };
  } catch (error) { await owner.release(); throw error; }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const studio = await createStudio();
  console.log('SceneAgent 工作台：' + await studio.start());
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => studio.close().finally(() => process.exit(0)));
}
