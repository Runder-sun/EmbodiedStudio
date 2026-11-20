import { mkdir, readFile, writeFile, rename, open, readdir } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import path from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { ROOT, validId } from './config.mjs';

export const contentHash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');

export function requiredOutputs(value = ['generation', 'demonstrations', 'evaluation']) {
  const allowed = ['generation', 'demonstrations', 'evaluation'];
  if (!Array.isArray(value) || new Set(value).size !== value.length
    || ![allowed, ['generation', 'evaluation'], ['demonstrations']].some(row => row.length === value.length && row.every(key => value.includes(key)))) {
    throw Object.assign(new Error('交付范围只能为完整三项、场景与环境、机器人数据。'), { status: 400 });
  }
  return allowed.filter(key => value.includes(key));
}

export function environmentRefs(value) {
  if (!Array.isArray(value) || !value.length || value.some(row => !row || typeof row !== 'object' || Array.isArray(row)
    || Object.keys(row).some(key => !['revision', 'split', 'taskIds'].includes(key)) || !validId(row.revision)
    || row.split !== undefined && !['train', 'validation', 'test'].includes(row.split)
    || row.taskIds !== undefined && (!Array.isArray(row.taskIds) || !row.taskIds.length || row.taskIds.some(id => !validId(id))
      || new Set(row.taskIds).size !== row.taskIds.length)) || new Set(value.map(row => row.revision)).size !== value.length) {
    throw Object.assign(new Error('环境引用须为同工程的唯一版本；同一布局只能有一个拆分标签，任务选择须非空且不重复。'), { status: 400 });
  }
  return value.map(row => ({ revision: row.revision, ...(row.split !== undefined ? { split: row.split } : {}),
    ...(row.taskIds !== undefined ? { taskIds: [...row.taskIds].sort() } : {}) })).sort((a, b) => a.revision.localeCompare(b.revision));
}

export class Store {
  projects = [];
  records = new Map();
  saveQueue = Promise.resolve();

  constructor({ root = ROOT } = {}) {
    this.root = root;
    this.stateDir = path.join(root, '.state');
    this.stateFile = path.join(this.stateDir, 'projects.json');
  }

  async load() {
    await mkdir(this.stateDir, { recursive: true });
    try {
      this.projects = JSON.parse(await readFile(this.stateFile, 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    for (const project of this.projects) {
      project.activeTurnId = null;
      project.eventSeq ??= 0;
      const unconfirmed = new Set();
      const advance = row => {
        if (typeof row.eventId !== 'string' || !row.eventId.startsWith(`${project.id}:`)) return;
        const sequence = row.eventId.slice(project.id.length + 1);
        if (/^[1-9]\d*$/.test(sequence) && Number.isSafeInteger(Number(sequence))) {
          project.eventSeq = Math.max(project.eventSeq, Number(sequence));
          return true;
        }
      };
      for (const row of project.events) if (advance(row)) unconfirmed.add(row.eventId);
      const confirm = row => { advance(row); unconfirmed.delete(row.eventId); };
      const input = createReadStream(path.join(this.directory(project.id), 'trace.jsonl'));
      let previous, lastByte = 10;
      input.on('data', chunk => { lastByte = chunk.at(-1); });
      const lines = createInterface({ input, crlfDelay: Infinity });
      try {
        for await (const line of lines) {
          if (previous) confirm(JSON.parse(previous));
          previous = line;
        }
        if (previous) {
          try { confirm(JSON.parse(previous)); }
          catch (error) {
            const position = /at position (\d+)/.exec(error.message);
            if (lastByte === 10 || !(error.message === 'Unexpected end of JSON input' || position && Number(position[1]) === previous.length)) throw error;
            const tailHash = contentHash(previous);
            if (project.traceRecovery?.tailHash !== tailHash || project.traceRecovery.reservedEventId !== `${project.id}:${project.eventSeq}`) {
              project.eventSeq++; // Reserve the interrupted row's identity without rewriting its bytes.
              project.traceRecovery = { tailHash, reservedEventId: `${project.id}:${project.eventSeq}` };
            }
            this._holdTrace(project, ['trace.jsonl 末尾没有完整 JSON 行；原文件保留，须核对修复后重新加载。', error.message]);
          }
        }
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      } finally {
        lines.close();
        input.destroy();
      }
      if (unconfirmed.size) this._holdTrace(project, ['projects.json 中已保存的事件尚未在 trace.jsonl 确认；不从最近事件盲目回填历史。',
        `未确认事件：${[...unconfirmed].join('、')}`]);
      if (project.generation && !['complete', 'failed', 'cancelled', 'draft'].includes(project.generation.status)) project.generation.status = project.generation.waitingSystem ? 'waiting_system' : 'recovering';
      for (const job of project.jobs) {
        if (job.durable && !job.generationId && job.payload?.projectId === project.id
          && job.payload.jobId === job.id && validId(job.payload.generationId)) job.generationId = job.payload.generationId;
        if (!job.durable && (job.status === 'running' || job.status === 'queued')) {
          job.status = 'interrupted';
          job.error = '本地服务已重启，请检查远端结果后重新运行。';
        }
      }
    }
    return this;
  }

  get(id) {
    if (!validId(id)) throw Object.assign(new Error('项目标识无效'), { status: 400 });
    const project = this.projects.find(item => item.id === id);
    if (!project) throw Object.assign(new Error('项目不存在'), { status: 404 });
    return project;
  }

  async create({ name, prompt = '' }) {
    if (typeof name !== 'string' || !name.trim() || name.length > 120) {
      throw Object.assign(new Error('请输入不超过 120 字的项目名称'), { status: 400 });
    }
    const project = {
      id: 'p_' + randomUUID().replaceAll('-', '').slice(0,16),
      name: name.trim(), prompt: String(prompt), status: 'draft',
      createdAt: new Date().toISOString(), threadId: null, activeTurnId: null,
      jobs: [], messages: [], events: [], eventSeq: 0, artifacts: {},
      requirements: [{ version: 1, text: String(prompt), time: new Date().toISOString() }],
    };
    const dir = this.directory(project.id);
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, 'request.md'), project.prompt || '请在网页中描述希望生成的场景与任务。');
    await writeFile(path.join(dir, 'scene.py'), '"""从用户要求生成场景；World 内核由运行器提供。"""\ndef build(world):\n    pass\n');
    await writeFile(path.join(dir, 'tasks.py'), '"""绑定新场景的代表任务。"""\ndef build(world):\n    return []\n');
    await writeFile(path.join(dir, 'expert.py'), '"""正式控制只经受控环境接口。"""\ndef run(env, task):\n    raise NotImplementedError("尚未生成专家程序")\n');
    await writeFile(path.join(dir, 'rubrics.py'), '"""按原始任务生成评分；运行时只提供真实事实。"""\ndef evaluate(task, facts, state):\n    raise NotImplementedError("尚未生成任务评分")\n');
    this.projects.push(project);
    await this.save();
    return project;
  }

  directory(id) {
    if (!validId(id)) throw new Error('项目标识无效');
    return path.join(this.root, 'projects', id);
  }

  beginGeneration(id, { timeBudgetMs, requiredOutputs: requestedOutputs, environmentRefs: requestedEnvironments, videoOnly, recordOnlyVideos, recordOnlyTaskIds } = {}) {
    const project = this.get(id);
    const outputs = requestedOutputs === undefined ? undefined : requiredOutputs(requestedOutputs);
    const environments = requestedEnvironments === undefined ? undefined : environmentRefs(requestedEnvironments);
    const selectedOutputs = outputs ?? (project.generation && !['complete', 'cancelled', 'failed'].includes(project.generation.status) ? project.generation.requiredOutputs : undefined) ?? requiredOutputs();
    if (videoOnly !== undefined && typeof videoOnly !== 'boolean' || videoOnly && !selectedOutputs.includes('demonstrations')) throw Object.assign(new Error('仅视频选项须为布尔值，且本次包含机器人执行。'), { status: 400 });
    const current = project.generation && !['complete', 'cancelled', 'failed'].includes(project.generation.status) ? project.generation : null;
    const recording = recordOnlyVideos ?? (current?.recordOnlyVideos === true);
    if (recordOnlyVideos !== undefined && typeof recordOnlyVideos !== 'boolean'
      || recording && (!(videoOnly ?? current?.videoOnly) || selectedOutputs.length !== 1 || selectedOutputs[0] !== 'demonstrations')
      || recordOnlyTaskIds !== undefined && (!recording || !Array.isArray(recordOnlyTaskIds) || !recordOnlyTaskIds.length
        || recordOnlyTaskIds.some(id => !validId(id)) || new Set(recordOnlyTaskIds).size !== recordOnlyTaskIds.length)) {
      throw Object.assign(new Error('原始录像须显式启用仅视频机器人范围，并提供非空、唯一的真实任务标识。'), { status: 400 });
    }
    const recordingTasks = recordOnlyTaskIds === undefined ? current?.recordOnlyTaskIds : [...recordOnlyTaskIds].sort();
    if (current?.recordOnlyVideos && (recordOnlyVideos === false
      || recordOnlyTaskIds !== undefined && contentHash(recordingTasks) !== contentHash(current.recordOnlyTaskIds))) {
      throw Object.assign(new Error('当前生成任务的原始录像模式和任务范围已锁定。'), { status: 409 });
    }
    if (recording) {
      const selectedEnvironments = environments ?? current?.environmentRefs;
      const builds = [...project.jobs.filter(row => row.kind === 'build' && row.status === 'completed').map(row => row.result), project.latestBuild]
        .filter(row => row && (!row.projectId || row.projectId === project.id) && Array.isArray(row.tasks)
          && (!selectedEnvironments || selectedEnvironments.some(ref => ref.revision === row.revision)));
      if (!recordingTasks?.length || builds.length && recordingTasks.some(id => !builds.some(row => row.tasks.some(task => task.id === id)
        && (!selectedEnvironments || selectedEnvironments.some(ref => ref.revision === row.revision && (!ref.taskIds || ref.taskIds.includes(id))))))) {
        throw Object.assign(new Error('原始录像任务必须属于本工程所选真实环境；尚未构建时须先显式锁定任务标识。'), { status: 400 });
      }
    }
    if (environments?.some(row => row.taskIds) && selectedOutputs.includes('evaluation')) throw Object.assign(new Error('场景与环境交付必须覆盖冻结环境的全部任务。'), { status: 400 });
    if (timeBudgetMs !== undefined && timeBudgetMs !== null && (!Number.isSafeInteger(timeBudgetMs) || timeBudgetMs <= 0)) {
      throw Object.assign(new Error('生成总时限须为正整数毫秒或 null（不限总时长）。'), { status: 400 });
    }
    if (project.generation && !['complete', 'cancelled', 'failed'].includes(project.generation.status)) {
      const currentBudget = project.generation.timeBudgetMs ?? null;
      if (timeBudgetMs !== undefined && timeBudgetMs !== currentBudget) {
        throw Object.assign(new Error('当前生成任务的总时限已锁定，不能通过后续消息更改。'), { status: 409 });
      }
      if (outputs && contentHash(outputs) !== contentHash(requiredOutputs(project.generation.requiredOutputs))
        || environments && contentHash(environments) !== contentHash(project.generation.environmentRefs ?? null)
        || videoOnly !== undefined && videoOnly !== (project.generation.videoOnly === true)) {
        throw Object.assign(new Error('当前生成任务的交付范围和环境引用已锁定。'), { status: 409 });
      }
      if (recording) Object.assign(project.generation, { recordOnlyVideos: true, recordOnlyTaskIds: recordingTasks });
      return project.generation;
    }
    if ((outputs ?? requiredOutputs()).length === 1 && !environments && !videoOnly) throw Object.assign(new Error('机器人数据采集须明确选择已有冻结环境。'), { status: 400 });
    if ((outputs ?? requiredOutputs()).length === 1 && environments) for (const ref of environments) {
      const frozen = project.jobs.filter(row => row.kind === 'build' && row.status === 'completed' && row.result?.revision === ref.revision).at(-1)?.result
        || (project.latestBuild?.revision === ref.revision ? project.latestBuild : null);
      if (!frozen || frozen.projectId && frozen.projectId !== project.id) throw Object.assign(new Error('所选环境版本不属于当前工程或尚未构建。'), { status: 409 });
      if (frozen.capabilities?.stageSplit !== true) throw Object.assign(new Error('所选旧环境不支持独立采集；请先显式构建兼容的新版本，原包保留。'), { status: 409 });
      if (ref.taskIds?.some(id => !frozen.tasks?.some(row => row.id === id))) throw Object.assign(new Error('所选采集任务不属于冻结环境。'), { status: 400 });
    }
    project.requirements ??= [{ version: 1, text: project.prompt || '', time: new Date().toISOString() }];
    project.generation = { id: 'g_' + randomUUID().replaceAll('-', '').slice(0,16), status: 'active',
      requirementVersion: project.requirements.length, requestHash: contentHash(project.requirements.map(({text}) => text)),
      requiredOutputs: outputs ?? requiredOutputs(), ...(environments ? { environmentRefs: environments } : {}), startedAt: new Date().toISOString(),
      deliveryContract: 'producer-exports-v1',
      ...(videoOnly ? { videoOnly: true } : {}),
      ...(recording ? { recordOnlyVideos: true, recordOnlyTaskIds: recordingTasks } : {}),
      timeBudgetMs: timeBudgetMs ?? null,
      continuationCount: 0, pendingContinuation: null };
    project.generation.approvalWaitMs = 0;
    project.generation.approvalWaitingSince = null;
    const issue = this.records.get(id)?.issue;
    if (issue) this._holdTrace(project, issue.evidence);
    delete project.delivery;
    return project.generation;
  }

  async addRequirement(id, text) {
    const project = this.get(id);
    const generation = this.beginGeneration(id);
    if (generation.cancelRequestedAt && !['complete', 'failed', 'cancelled'].includes(generation.status)) {
      throw Object.assign(new Error('任务正在停止，请等待原生会话与远端作业确认后继续。'), { status: 409 });
    }
    project.requirements.push({ version: project.requirements.length + 1, text, time: new Date().toISOString() });
    generation.requirementVersion = project.requirements.length;
    generation.requestHash = contentHash(project.requirements.map(({text}) => text));
    generation.status = generation.waitingSystem ? 'waiting_system' : 'active';
    await writeFile(path.join(this.directory(id), 'request.md'), project.requirements.map(({version,text}) => `需求版本 ${version}\n${text}`).join('\n\n'));
    await this.record(id, { type: 'requirement_added', text });
    await this.save();
    return generation;
  }

  async sources(id) {
    const dir = this.directory(this.get(id).id);
    const sources = {};
    for (const key of ['scene', 'tasks', 'expert', 'rubrics']) {
      try { sources[key] = await readFile(path.join(dir, key + '.py'), 'utf8'); }
      catch (error) { if (key !== 'rubrics' || error.code !== 'ENOENT') throw error; sources[key] = ''; }
    }
    return sources;
  }

  async sourceSnapshot(id, cause = null) {
    const project = this.get(id), directory = this.directory(id), sourceFiles = {};
    const collect = async relative => {
      for (const entry of (await readdir(path.join(directory, relative), { withFileTypes: true })).sort((a,b) => a.name.localeCompare(b.name))) {
        const name = path.posix.join(relative, entry.name);
        if (entry.name.startsWith('.') || entry.name === '__pycache__') continue;
        if (!relative && entry.name === 'delivery' && project.generation?.deliveryContract === 'producer-exports-v1') continue;
        if (entry.isSymbolicLink()) throw new Error('工程源文件不能通过软链接引用未锁定内容');
        if (entry.isDirectory()) await collect(name);
        else if (entry.isFile() && (name.endsWith('.py') || name.endsWith('.robot.json'))) sourceFiles[name] = await readFile(path.join(directory, name), 'utf8');
      }
    };
    await collect('');
    const sourceHash = contentHash(sourceFiles), snapshotRef = `.trace/sources/${sourceHash}.json`;
    await mkdir(path.join(directory, '.trace', 'sources'), { recursive: true });
    const snapshot = { sourceHash, sourceFiles, sources: Object.fromEntries(['scene','tasks','expert','rubrics'].map(key => [key, sourceFiles[key+'.py'] ?? ''])), snapshotRef };
    await writeFile(path.join(directory, snapshotRef), JSON.stringify({ sourceHash, sourceFiles }));
    if (project.sourceHash !== sourceHash) {
      await this.record(id, { type: 'source_snapshot', sourceHash, snapshotRef,
        previousSourceHash: project.sourceHash ?? null, cause, files: Object.keys(sourceFiles) });
      project.sourceHash = sourceHash;
    }
    return snapshot;
  }

  async updateSources(id, sources) {
    const project = this.get(id);
    if (project.activeTurnId || project.jobs.some(job => job.status === 'running')) {
      throw Object.assign(new Error('请等待当前生成或运行完成，再保存代码。'), { status: 409 });
    }
    for (const key of ['scene', 'tasks', 'expert', 'rubrics']) {
      if (typeof sources[key] !== 'string' || sources[key].length > 500000) {
        throw Object.assign(new Error('场景、任务、行为和评分程序必须是文本，每份不超过 500 KB'), { status: 400 });
      }
    }
    for (const key of ['scene', 'tasks', 'expert', 'rubrics']) await writeFile(path.join(this.directory(id), key + '.py'), sources[key]);
    await this.sourceSnapshot(id, 'manual_edit');
    if (!project.generation) project.status = 'edited';
    await this.save();
  }

  record(id, event) {
    const project = this.get(id);
    const queue = this._recordQueue(id);
    if (queue.issue) return Promise.reject(queue.issue);
    const row = { ...event, time: new Date().toISOString(), eventId: `${id}:${project.eventSeq = (project.eventSeq || 0) + 1}`,
      generationId: project.generation?.id ?? null, requirementVersion: project.generation?.requirementVersion ?? 1 };
    project.events.push(row);
    if (project.events.length > 200) project.events.shift();
    queue.pending.push({ text: JSON.stringify(row) + '\n', attempt: null });
    return this.flush(id).then(() => row);
  }

  _recordQueue(id) {
    let queue = this.records.get(id);
    if (!queue) { queue = { tail: Promise.resolve(), pending: [], issue: null }; this.records.set(id, queue); }
    return queue;
  }

  _holdTrace(project, evidence) {
    const queue = this._recordQueue(project.id);
    queue.issue ??= Object.assign(new Error('工程轨迹含未确认记录，保留原文件与状态证据并等待核对修复后重新加载。'), { status: 409, evidence });
    const generation = project.generation;
    if (generation) {
      if (!generation.waitingSystem) {
        const at = Date.now();
        if (generation.approvalWaitingSince != null) {
          generation.approvalWaitMs = (generation.approvalWaitMs ?? 0) + Math.max(0, at - Date.parse(generation.approvalWaitingSince));
          generation.approvalWaitingSince = null;
        }
        generation.waitingSystem = { requestId: `trace_${project.id}_${project.eventSeq}`, kind: 'trace_incomplete',
          reason: queue.issue.message, evidence: [...queue.issue.evidence], since: new Date(at).toISOString() };
        generation.systemWaitMs ??= 0;
      }
      if (!generation.cancelRequestedAt) generation.status = 'waiting_system';
    }
    return queue.issue;
  }

  flush(id) {
    const queue = this.records.get(id);
    if (!queue) return Promise.resolve();
    if (queue.issue) return Promise.reject(queue.issue);
    if (!queue.pending.length) return queue.tail;
    queue.tail = queue.tail.catch(() => {}).then(async () => {
      while (queue.pending.length) {
        if (queue.issue) throw queue.issue;
        const entry = queue.pending[0], bytes = Buffer.from(entry.text), source = await open(path.join(this.directory(id), 'trace.jsonl'), 'a+');
        try {
          const info = await source.stat();
          let confirmed = false;
          if (entry.attempt) {
            // A failed close may follow a complete write; confirm its original byte range before retrying.
            const { offset, dev, ino } = entry.attempt;
            if (info.dev !== dev || info.ino !== ino || info.size < offset || info.size > offset + bytes.length) {
              throw this._holdTrace(this.get(id), ['失败追加的文件身份或固定范围已变化，不能确认是否已经写入。']);
            }
            if (info.size > offset) {
              const actual = Buffer.alloc(bytes.length); let read = 0;
              while (read < actual.length) {
                const result = await source.read(actual, read, actual.length - read, offset + read);
                if (!result.bytesRead) break;
                read += result.bytesRead;
              }
              if (read !== bytes.length || !actual.equals(bytes)) {
                throw this._holdTrace(this.get(id), ['失败追加留下未确认的尾部字节；不追加下一条记录。']);
              }
              confirmed = true;
            }
          }
          if (!confirmed) {
            if (info.size) {
              const last = Buffer.alloc(1);
              const result = await source.read(last, 0, 1, info.size - 1);
              if (result.bytesRead !== 1 || last[0] !== 10) throw this._holdTrace(this.get(id), ['trace.jsonl 末尾缺少完整行分隔；原字节保留。']);
            }
            entry.attempt = { offset: info.size, dev: info.dev, ino: info.ino };
            await source.appendFile(entry.text, 'utf8');
          }
        } catch (error) {
          if (entry.attempt && !queue.issue) {
            const after = await source.stat().catch(() => null), { offset } = entry.attempt;
            if (after && after.size > offset && after.size !== offset + bytes.length) this._holdTrace(this.get(id), ['追加失败留下部分尾行，原始字节保留。', error.message]);
          }
          throw error;
        } finally { await source.close(); }
        queue.pending.shift();
      }
    });
    return queue.tail;
  }

  async trace(id) {
    await this.flush(id);
    try { return (await readFile(path.join(this.directory(id), 'trace.jsonl'), 'utf8')).split('\n').filter(Boolean).map(JSON.parse); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  }

  save() {
    if (this.pendingSave) return this.pendingSave;
    const pending = this.saveQueue.catch(() => {}).then(async () => {
      try {
        await mkdir(this.stateDir, { recursive: true });
        this.pendingSave = null; // Later callers need a write after this serialization boundary.
        const data = JSON.stringify(this.projects, null, 2);
        await writeFile(this.stateFile + '.tmp', data);
        await rename(this.stateFile + '.tmp', this.stateFile);
      } finally { if (this.pendingSave === pending) this.pendingSave = null; }
    });
    this.saveQueue = this.pendingSave = pending;
    return pending;
  }
}
