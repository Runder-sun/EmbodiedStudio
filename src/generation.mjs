import { randomUUID } from 'node:crypto';
import { contentHash, requiredOutputs } from './store.mjs';

const terminal = new Set(['complete', 'failed', 'cancelled']);
const jobTerminal = new Set(['completed', 'failed', 'cancelled', 'interrupted']);

export class GenerationHost {
  constructor({ store, codex, continueTurn, ensureThread = null, stopWork = async () => {}, publish = () => {}, hasApprovals = () => false, hasOpeningInput = () => false, readNativeWork = async () => ({ ready: true, completed: [] }), now = Date.now }) {
    Object.assign(this, { store, codex, continueTurn, ensureThread, stopWork, publish, hasApprovals, hasOpeningInput, readNativeWork, now });
    this.queues = new Map();
  }

  _serial(project, action) {
    const next = (this.queues.get(project.id) ?? Promise.resolve()).catch(() => {}).then(action);
    this.queues.set(project.id, next);
    return next;
  }

  _jobs(project) {
    return project.jobs.filter(job => job.generationId === project.generation.id);
  }

  async _save(project, event) {
    if (event) await this.store.record(project.id, event);
    await this.store.save();
    this.publish(project);
    return project.generation;
  }

  turnCompleted(project, turn) {
    return this._serial(project, async () => {
      const generation = project.generation;
      if (!generation || terminal.has(generation.status)) return generation;
      if (project.activeTurnId && project.activeTurnId !== turn.id) return generation;
      generation.completedTurnIds ??= [];
      if (generation.completedTurnIds.includes(turn.id)) return generation;
      generation.completedTurnIds.push(turn.id);
      project.activeTurnId = null;
      if (generation.waitingSystem && generation.pendingContinuation?.turnId === turn.id) generation.pendingContinuation = null;
      return this._reconcile(project);
    });
  }

  jobCompleted(project, job) {
    return this._serial(project, async () => {
      const generation = project.generation;
      if (!generation || terminal.has(generation.status)) return generation;
      if (job.generationId && job.generationId !== generation.id) return generation;
      if (!jobTerminal.has(job.status)) return this._reconcile(project);
      generation.observedJobTerminals ??= [];
      if (generation.observedJobTerminals.includes(job.id)) return generation;
      generation.observedJobTerminals.push(job.id);
      return this._reconcile(project);
    });
  }

  recover(project) {
    return this._serial(project, () => this._reconcile(project));
  }

  waitSystem(project, args = {}) {
    const generation = project.generation;
    const conflict = message => Object.assign(new Error(message), { status: 409 });
    if (!generation || args.projectId !== project.id || args.generationId !== generation.id) return Promise.reject(conflict('等待请求不属于当前生成任务。'));
    if (![args.requestId, args.reason].every(value => typeof value === 'string' && value.trim())
      || !Array.isArray(args.evidence) || !args.evidence.length || !args.evidence.every(value => typeof value === 'string' && value.trim())
      || args.jobIds !== undefined && (!Array.isArray(args.jobIds) || !args.jobIds.every(id => this._jobs(project).some(job => job.id === id)))) {
      return Promise.reject(Object.assign(new Error('等待请求须有请求标识、原因、非空证据及属于本任务的作业。'), { status: 400 }));
    }
    const hash = contentHash({ reason: args.reason, evidence: args.evidence, jobIds: args.jobIds ?? [] });
    const hashes = generation.lastWaitReceipt?.requestHashes;
    const prior = hashes && Object.hasOwn(hashes, args.requestId) ? hashes[args.requestId] : null;
    if (prior) return prior === hash ? this._serial(project, () => {
      if (project.generation !== generation || generation.id !== args.generationId) throw conflict('等待请求对应的生成任务已变化。');
      return generation;
    }) : Promise.reject(conflict('同一等待请求标识不能更改原因或证据。'));
    if (generation.cancelRequestedAt || terminal.has(generation.status)) return Promise.resolve(generation);
    if (generation.waitingSystem) return Promise.reject(conflict('当前任务已有等待声明，请保留其原始请求标识。'));
    // Latch before joining the serial queue so an in-flight dispatch sees the request.
    this._requestWait(project, { requestId: args.requestId, kind: 'reported', reason: args.reason,
      evidence: [...args.evidence], ...(args.jobIds ? { jobIds: [...args.jobIds] } : {}) });
    generation.lastWaitReceipt = { requestId: args.requestId,
      requestHashes: { ...generation.lastWaitReceipt?.requestHashes, [args.requestId]: hash } };
    return this._serial(project, async () => {
      if (project.generation !== generation || generation.id !== args.generationId) throw conflict('等待保存前生成任务已变化。');
      await this._save(project, { type: 'generation_waiting_system', waitingSystem: generation.waitingSystem });
      if (project.generation !== generation || generation.id !== args.generationId) throw conflict('等待保存时生成任务已变化。');
      return generation.cancelRequestedAt || (project.delivery?.accepted === true || project.delivery?.status === 'accepted') ? this._reconcile(project) : generation;
    });
  }

  _requestWait(project, wait) {
    const generation = project.generation;
    if (generation.waitingSystem || generation.cancelRequestedAt || terminal.has(generation.status)) return;
    const at = this.now();
    this._accountApprovalWait(project, false, at);
    generation.waitingSystem = { ...wait, since: new Date(at).toISOString() };
    generation.systemWaitMs ??= 0;
    generation.status = 'waiting_system';
  }

  _settleSystemWait(generation) {
    if (!generation.waitingSystem) return;
    generation.systemWaitMs = (generation.systemWaitMs ?? 0) + Math.max(0, this.now() - Date.parse(generation.waitingSystem.since));
    if (generation.lastWaitReceipt) generation.lastWaitReceipt.resolvedAt = new Date(this.now()).toISOString();
    delete generation.waitingSystem;
  }

  resume(project, { generationId } = {}) {
    return this._serial(project, async () => {
      const generation = project.generation, threadId = project.threadId;
      const conflict = message => Object.assign(new Error(message), { status: 409 });
      if (!generation || !generationId || generation.id !== generationId) throw conflict('恢复的生成任务标识与当前任务不匹配。');
      if (generation.status === 'active' && !generation.cancelRequestedAt && !generation.waitingSystem) return generation;
      const waiting = generation.waitingSystem;
      const previous = Object.fromEntries(['status', 'completedAt', 'terminalReason', 'cancelRequestedAt',
        'stopStatus', 'recoveryIssue', 'continuationCount', 'waitingSystem', 'systemWaitMs'].filter(key => generation[key] !== undefined).map(key => [key, structuredClone(generation[key])]));
      const ready = () => {
        if (project.generation !== generation || generation.id !== generationId || project.threadId !== threadId
          || !threadId || !(waiting ? generation.waitingSystem === waiting && generation.status === 'waiting_system'
            : ['failed', 'cancelled'].includes(generation.status) && generation.status === previous.status)) {
          throw conflict('仅可恢复同一原生会话中已失败、已取消或等待系统处理的当前生成任务。');
        }
        if (project.activeTurnId || generation.pendingContinuation || this.hasApprovals(project) || this.hasOpeningInput(project)
          || project.jobs.some(job => !jobTerminal.has(job.status))) throw conflict('仍有原生轮次、作业、审批、输入或未确认的续接，不能恢复。');
      };
      ready();
      let native;
      try { native = await this.codex.readThread(threadId, { includeTurns: true }); }
      catch (error) { throw conflict(`无法确认原生会话空闲，原状态保持不变：${error.message}`); }
      ready();
      if (native?.id === threadId && native.status?.type === 'notLoaded' && Array.isArray(native.turns)
        && !native.turns.some(turn => turn.status === 'inProgress') && this.ensureThread) {
        try { await this.ensureThread(project, threadId); }
        catch (error) { throw conflict(`无法重新附着原生会话，原状态保持不变：${error.message}`); }
        ready();
        try { native = await this.codex.readThread(threadId, { includeTurns: true }); }
        catch (error) { throw conflict(`无法确认原生会话空闲，原状态保持不变：${error.message}`); }
        ready();
        if (native?.id !== threadId || !Array.isArray(native.turns)) throw conflict('重新附着后的原生会话标识或轮次未完整返回，不能恢复。');
      }
      if (native?.id && native.id !== threadId || native?.status?.type !== 'idle'
        || native.turns?.some(turn => turn.status === 'inProgress')) throw conflict('原生会话未确认空闲，不能恢复。');
      if (waiting) {
        let work;
        try { work = await this.readNativeWork(project, native); }
        catch (error) { throw conflict(`无法确认原生子任务空闲：${error.message}`); }
        ready();
        if (work?.ready !== true) throw conflict('原生子任务仍在运行或来源状态未确认，不能恢复。');
      }
      await this.store.record(project.id, { type: 'generation_resumed', generationId, threadId, previous });
      ready();
      this._settleSystemWait(generation);
      for (const key of ['completedAt', 'terminalReason', 'cancelRequestedAt', 'stopStatus', 'recoveryIssue']) delete generation[key];
      generation.status = 'active';
      await this._save(project);
      return this._reconcile(project);
    });
  }

  _accountApprovalWait(project, waiting = this.hasApprovals(project), at = this.now()) {
    const generation = project.generation;
    generation.approvalWaitMs ??= 0;
    if (waiting && !generation.waitingSystem) generation.approvalWaitingSince ??= new Date(at).toISOString();
    else if (generation.approvalWaitingSince != null) {
      generation.approvalWaitMs += Math.max(0, at - Date.parse(generation.approvalWaitingSince));
      generation.approvalWaitingSince = null;
    } else generation.approvalWaitingSince = null;
  }

  approvalsChanged(project, at = this.now()) {
    const waiting = this.hasApprovals(project);
    return this._serial(project, () => {
      if (!project.generation || terminal.has(project.generation.status)) return project.generation;
      this._accountApprovalWait(project, waiting, at);
      return this._save(project);
    });
  }

  cancel(project) {
    if (!project.generation || terminal.has(project.generation.status)) return Promise.resolve(project.generation);
    this._requestStop(project, 'cancelled', '用户已取消，原生会话与远端作业终态已确认。');
    return this._serial(project, () => this._reconcile(project));
  }

  _requestStop(project, status, reason) {
    const generation = project.generation;
    if (!generation.cancelRequestedAt) {
      generation.cancelRequestedAt = new Date(this.now()).toISOString();
      generation.stopStatus = status;
      generation.terminalReason = reason;
    }
    generation.stopStatus ??= 'cancelled';
    generation.status = 'cancel_requested';
  }

  async _reconcileStop(project) {
    const generation = project.generation;
    generation.stopStatus ??= 'cancelled';
    await this._save(project);
    const native = await this._native(project);
    const active = native?.turns?.findLast(turn => turn.status === 'inProgress');
    const turnId = active?.id ?? (native === undefined || native?.status?.type === 'active' ? project.activeTurnId : null);
    try {
      await this.stopWork(project, { turnId });
    } catch (error) {
      generation.status = 'recovering';
      generation.recoveryIssue = `停止尚未确认：${error.message}`;
      return this._save(project);
    }
    if (native === undefined) return generation;
    if (native !== null && !['active', 'idle'].includes(native.status?.type)) {
      generation.status = 'recovering';
      generation.recoveryIssue = '原生会话状态未知，尚未确认停止。';
      return this._save(project);
    }
    const running = active || native?.status?.type === 'active';
    project.activeTurnId = running ? turnId ?? null : null;
    if (running || this._jobs(project).some(job => !jobTerminal.has(job.status))) {
      generation.status = 'cancel_requested';
      return this._save(project);
    }
    this._settleSystemWait(generation);
    generation.status = generation.stopStatus;
    generation.completedAt = new Date(this.now()).toISOString();
    generation.pendingContinuation = null;
    delete generation.recoveryIssue;
    return this._save(project, { type: 'generation_stopped', reason: generation.terminalReason });
  }

  async _native(project, includeTurns = true) {
    const generation = project.generation, threadId = project.threadId;
    if (!threadId) return null;
    try {
      return await this.codex.readThread(threadId, { includeTurns });
    } catch (error) {
      if (project.generation !== generation || project.threadId !== threadId) return undefined;
      generation.status = generation.waitingSystem ? 'waiting_system' : 'recovering';
      generation.recoveryIssue = `无法查询原生会话，尚未确认是否可续接：${error.message}`;
      await this._save(project);
      return undefined;
    }
  }

  async _waiting(project, native) {
    const generation = project.generation, pending = generation.pendingContinuation, threadId = project.threadId;
    generation.status = 'waiting_system';
    if (pending && !pending.dispatchAttemptedAt) generation.pendingContinuation = null;
    if (project.activeTurnId || pending?.dispatchAttemptedAt) {
      native ??= await this._native(project);
      if (project.generation !== generation || project.threadId !== threadId) return project.generation;
      if (generation.cancelRequestedAt) return this._reconcileStop(project);
      if (native !== undefined) {
        const active = native?.turns?.findLast(turn => turn.status === 'inProgress');
        if (active) project.activeTurnId = active.id;
        else if (native?.status?.type === 'idle') project.activeTurnId = null;
        const matched = native?.turns?.find(turn => turn.id === pending?.turnId || pending && turn.items?.some(item => item.type === 'userMessage'
          && item.content?.some(input => input.type === 'text' && input.text.includes(`[续接事件 ${pending.id}]`))));
        if (matched && matched.status !== 'inProgress') generation.pendingContinuation = null;
      }
    }
    generation.status = 'waiting_system';
    return this._save(project);
  }

  async _progress(project, native) {
    const generation = project.generation, threadId = project.threadId;
    if (native?.status?.type === 'notLoaded' && this.ensureThread) {
      const id = generation.id, requirementVersion = generation.requirementVersion, requestHash = generation.requestHash;
      const known = new Set([...(generation.completedTurnIds ?? []), generation.pendingContinuation?.turnId].filter(Boolean));
      const current = () => project.generation === generation && generation.id === id && project.threadId === threadId
        && generation.requirementVersion === requirementVersion && generation.requestHash === requestHash
        && !terminal.has(generation.status) && !this._accepted(project) && !generation.cancelRequestedAt
        && !generation.waitingSystem && !project.activeTurnId && !this.hasApprovals(project) && !this.hasOpeningInput(project)
        && this._jobs(project).every(job => jobTerminal.has(job.status));
      const complete = value => value?.id === threadId && known.size > 0 && Array.isArray(value.turns)
        && value.turns.every(turn => typeof turn.id === 'string' && turn.id && ['completed', 'failed', 'interrupted'].includes(turn.status))
        && [...known].every(turnId => value.turns.some(turn => turn.id === turnId));
      if (current() && complete(native)) {
        try {
          await this.ensureThread(project, threadId);
          if (!current()) return null;
          const attached = await this.codex.readThread(threadId, { includeTurns: true });
          if (!current()) return null;
          if (attached?.status?.type !== 'idle' || !complete(attached)) throw new Error('重新附着后的原生会话未返回完整空闲来源。');
          native = attached;
        } catch (error) {
          if (!current()) return null;
          generation.status = 'recovering';
          generation.recoveryIssue = `原生会话重新附着尚未确认：${error.message}`;
          await this._save(project);
          return null;
        }
      }
    }
    let work;
    const ready = async () => {
      try { work = await this.readNativeWork(project, native); }
      catch (error) { work = { ready: false, reason: `原生协作状态查询失败：${error.message}` }; }
      if (project.generation !== generation || project.threadId !== threadId || generation.cancelRequestedAt || generation.waitingSystem || project.activeTurnId) return false;
      if (work?.ready === true) return true;
      generation.status = 'recovering';
      generation.recoveryIssue = work?.reason || '原生子任务状态尚未确认，等待对账。';
      await this._save(project);
      return false;
    };
    if (!await ready()) return null;
    let source, sourceUnavailable;
    if (!this._recordOnlyVideos(generation)) {
      try { source = await this.store.sourceSnapshot(project.id, 'continuation_progress'); }
      catch (error) {
        if (project.generation !== generation || generation.cancelRequestedAt || generation.waitingSystem) return null;
        sourceUnavailable = error.message;
      }
    }
    if (!await ready() || this.hasOpeningInput(project)) return null;
    return { sourceHash: source?.sourceHash ?? null, ...(sourceUnavailable !== undefined ? { sourceUnavailable } : {}),
      requirementVersion: generation.requirementVersion, requestHash: generation.requestHash,
      jobs: this._jobs(project).map(job => [job.id, job.status]).sort((a, b) => a[0].localeCompare(b[0])), nativeWork: work.completed ?? [] };
  }

  _accepted(project) {
    const generation = project.generation, delivery = project.delivery;
    return (delivery?.accepted === true || delivery?.status === 'accepted') && delivery.generationId === generation.id
      && delivery.requirementVersion === generation.requirementVersion && delivery.requestHash === generation.requestHash
      && contentHash(delivery.requiredOutputs ?? requiredOutputs()) === contentHash(requiredOutputs(generation.requiredOutputs))
      && (!delivery.recordOnlyVideos || this._recordOnlyVideos(generation)
        && Array.isArray(delivery.recordOnlyTaskIds)
        && contentHash(delivery.recordOnlyTaskIds) === contentHash(generation.recordOnlyTaskIds))
      && (!generation.environmentRefs || contentHash(delivery.environmentRefs) === contentHash(generation.environmentRefs));
  }

  _recordOnlyVideos(generation) {
    return generation.recordOnlyVideos === true && generation.videoOnly === true
      && generation.requiredOutputs?.length === 1 && generation.requiredOutputs[0] === 'demonstrations'
      && Array.isArray(generation.recordOnlyTaskIds) && generation.recordOnlyTaskIds.length > 0
      && generation.recordOnlyTaskIds.every(id => typeof id === 'string' && id.trim())
      && new Set(generation.recordOnlyTaskIds).size === generation.recordOnlyTaskIds.length;
  }

  _recordedVideoReceipt(project) {
    const generation = project.generation;
    if (!this._recordOnlyVideos(generation)) return null;
    const selected = new Map();
    for (const job of this._jobs(project).filter(job => job.kind === 'rollout' && job.status === 'completed')) {
      if (job.result?.projectId && job.result.projectId !== project.id) continue;
      for (const episode of job.result?.episodes ?? (job.result?.episode ? [job.result] : [])) {
        const purpose = episode.purpose ?? job.payload?.task?.purpose;
        if (purpose !== 'demonstration' || job.payload?.task?.purpose && job.payload.task.purpose !== purpose
          || episode.score?.success !== true || episode.score.executionStatus !== 'completed'
          || episode.executionKind !== 'actuator_physics_native' || episode.projectId !== project.id
          || typeof episode.revision !== 'string' || !episode.revision || job.revision && job.revision !== episode.revision
          || typeof episode.episode !== 'string' || !episode.episode
          || !generation.recordOnlyTaskIds.includes(episode.taskId) || selected.has(episode.taskId)) continue;
        const scope = `projects/${project.id}/episodes/${episode.episode}/`;
        const videoRef = Object.entries(episode.artifacts ?? {}).find(([key, ref]) => (key === 'video' || key.startsWith('video:'))
          && typeof ref === 'string' && ref.startsWith(scope) && ref.split('/').every(part => part && part !== '.' && part !== '..'))?.[1];
        if (videoRef) selected.set(episode.taskId, { jobId: job.id, projectId: episode.projectId, revision: episode.revision,
          taskId: episode.taskId, episode: episode.episode, videoRef, executionKind: episode.executionKind,
          purpose, reportedSuccess: true });
      }
    }
    if (selected.size !== generation.recordOnlyTaskIds.length) return null;
    return { schema: 'sceneagent-recorded-videos-v1', deliveryId: 'rv_' + randomUUID().replaceAll('-', '').slice(0, 16),
      projectId: project.id, generationId: generation.id, requirementVersion: generation.requirementVersion,
      requestHash: generation.requestHash, requiredOutputs: [...generation.requiredOutputs],
      ...(generation.environmentRefs ? { environmentRefs: structuredClone(generation.environmentRefs) } : {}),
      accepted: true, status: 'accepted', assessedBy: 'user_acceptance_without_review',
      reviewSkipped: true, checksSkipped: true, recordOnlyVideos: true, recordOnlyTaskIds: [...generation.recordOnlyTaskIds],
      videos: generation.recordOnlyTaskIds.map(id => selected.get(id)) };
  }

  _budgetReason(generation) {
    const timeBudgetMs = generation.timeBudgetMs ?? null;
    return timeBudgetMs !== null && this.now() - Date.parse(generation.startedAt) - (generation.approvalWaitMs ?? 0) - (generation.systemWaitMs ?? 0) >= timeBudgetMs
      ? `生成时间预算已达到 ${timeBudgetMs / 60_000} 分钟。` : null;
  }

  assertBudget(generation) {
    const reason = this._budgetReason(generation);
    if (reason) throw Object.assign(new Error(reason), { status: 409, nativeInputNotSent: true, budgetExpired: true });
  }

  async _reconcile(project) {
    const generation = project.generation, threadId = project.threadId;
    if (!generation || terminal.has(generation.status)) return generation;
    const jobs = this._jobs(project), pendingJobs = jobs.filter(job => !jobTerminal.has(job.status));
    if (generation.status === 'cancel_requested' && !generation.cancelRequestedAt) this._requestStop(project, 'cancelled', '用户已取消，原生会话与远端作业终态已确认。');
    if (generation.cancelRequestedAt) return this._reconcileStop(project);
    if (!this._accepted(project)) {
      const recorded = this._recordedVideoReceipt(project);
      if (recorded) project.delivery = recorded;
    }
    const delivery = project.delivery;
    if (this._accepted(project)) {
      this._settleSystemWait(generation);
      generation.status = 'complete';
      generation.completedAt = new Date(this.now()).toISOString();
      generation.pendingContinuation = null;
      return this._save(project, { type: 'generation_complete', deliveryId: delivery.deliveryId ?? null });
    }
    if (generation.waitingSystem) return this._waiting(project);
    this._accountApprovalWait(project);
    if (this.hasApprovals(project)) {
      generation.status = 'waiting_user';
      return this._save(project);
    }
    const budgetReason = this._budgetReason(generation);
    if (budgetReason) return this._stop(project, budgetReason);
    if (this.hasOpeningInput(project)) return generation;
    if (pendingJobs.length) {
      generation.status = project.activeTurnId ? 'active' : 'waiting_jobs';
      return this._save(project);
    }
    if (generation.status === 'active' && project.activeTurnId && !generation.pendingContinuation) {
      const metadata = await this._native(project, false);
      if (project.generation !== generation || project.threadId !== threadId) return project.generation;
      if (generation.cancelRequestedAt) return this._reconcileStop(project);
      if (generation.waitingSystem) return this._waiting(project, metadata);
      if (metadata === undefined) return generation;
      if (metadata?.status?.type === 'active') return generation;
      if (metadata?.status?.type !== 'idle') {
        generation.status = 'recovering';
        generation.recoveryIssue = '原生会话状态未知，尚未确认是否可续接。';
        return this._save(project);
      }
      project.activeTurnId = null;
    }
    const native = await this._native(project);
    if (project.generation !== generation || project.threadId !== threadId) return project.generation;
    if (generation.cancelRequestedAt) return this._reconcileStop(project);
    if (generation.waitingSystem) return this._waiting(project, native);
    if (native === undefined) return generation;
    const turns = native?.turns ?? [];
    const active = turns.findLast(turn => turn.status === 'inProgress');
    const pending = generation.pendingContinuation;
    if (pending) {
      const matched = turns.find(turn => turn.id === pending.turnId || turn.items?.some(item => item.type === 'userMessage' &&
        item.content?.some(input => input.type === 'text' && input.text.includes(`[续接事件 ${pending.id}]`))))
        ?? (generation.completedTurnIds?.includes(pending.turnId) ? { id: pending.turnId, status: 'completed' } : null);
      if (matched) {
        pending.status = 'sent'; pending.turnId = matched.id;
        if (matched.status === 'inProgress') {
          project.activeTurnId = matched.id;
          generation.status = 'active';
          delete generation.recoveryIssue;
          return this._save(project);
        }
        pending.status = 'completed';
        if (project.activeTurnId === matched.id) project.activeTurnId = null;
        generation.completedTurnIds ??= [];
        if (!generation.completedTurnIds.includes(matched.id)) generation.completedTurnIds.push(matched.id);
      } else if (pending.dispatchAttemptedAt && !active && native?.status?.type !== 'active') {
        generation.status = 'recovering';
        generation.recoveryIssue = '续接发送结果尚未确认；原生历史未找到对应输入，保留待对账记录并暂停重发。';
        return this._save(project);
      }
    }
    if (active || native?.status?.type === 'active' || project.activeTurnId) {
      if (active) project.activeTurnId = active.id;
      generation.status = 'active';
      return this._save(project);
    }
    if (generation.pendingContinuation && generation.pendingContinuation.status !== 'completed') return this._dispatch(project);
    const progress = await this._progress(project, native);
    if (project.generation !== generation || project.threadId !== threadId) return project.generation;
    if (generation.cancelRequestedAt) return this._reconcileStop(project);
    if (generation.waitingSystem) return this._waiting(project, native);
    if (this._accepted(project) || this._jobs(project).some(job => !jobTerminal.has(job.status))) return this._reconcile(project);
    if (!progress || this.hasApprovals(project) || this.hasOpeningInput(project)) return generation;
    const unchanged = pending?.status === 'completed' && pending.progress?.sourceHash != null && progress.sourceHash != null
      && contentHash(pending.progress) === contentHash(progress);
    if (pending?.status === 'completed') generation.pendingContinuation = null;
    const id = 'c_' + randomUUID().replaceAll('-', '').slice(0, 16);
    const requirements = project.requirements?.map(item => `需求版本 ${item.version}：${item.text}`).join('\n') ?? project.prompt;
    const summaries = jobs.filter(job => jobTerminal.has(job.status)).map(job => ({ jobId: job.id, status: job.status,
      workerTimeoutSeconds: job.workerTimeoutSeconds ?? job.payload?.task?.workerTimeoutSeconds ?? null,
      result: job.result ?? null, error: job.error ?? null }));
    const failed = jobs.findLast(job => job.status === 'failed');
    const failure = failed ? { jobId: failed.id, status: failed.status,
      workerTimeoutSeconds: failed.workerTimeoutSeconds ?? failed.payload?.task?.workerTimeoutSeconds ?? null,
      stage: failed.stage ?? null, error: failed.error ?? null } : null;
    const resultText = JSON.stringify(summaries);
    const input = `[续接事件 ${id}]\n继续同一生成要求，不创建新工程。生成任务 ${generation.id}，当前需求版本 ${generation.requirementVersion}。\n` +
      `当前源观察：${JSON.stringify({ sourceHash: progress.sourceHash, ...(progress.sourceUnavailable !== undefined ? { sourceUnavailable: progress.sourceUnavailable } : {}) })}\n` +
      (unchanged ? '续接诊断：源码、需求、实际作业及已完成原生协作指针均未变化；这是观察结果，请自主判断下一步。\n' : '') +
      `最后失败作业：${JSON.stringify(failure)}\n` +
      '普通超时、源码读取异常或无持久变化本身不终止生成。请读取实际证据，在项目权限和公开执行参数内自主诊断、修正；有依据的新尝试可选择 workerTimeoutSeconds 并使用新 requestId，传输重试保留原标识，不盲目原样重跑。只有确认无法在这些权限内解决的重大阻碍，才用 generation_wait_system 提交实际证据及不能自行解决的原因。\n' +
      `完整持久要求：\n${requirements}\n` +
      `实际作业回执：${resultText.slice(0, 10_000)}${resultText.length > 10_000 ? '（摘要已截断；请用 job/project_inspect 读取完整结果。）' : ''}\n` +
      (this._recordOnlyVideos(generation)
        ? `本次仅收录原始视频，锁定任务 ${JSON.stringify(generation.recordOnlyTaskIds)} 尚未全部有完整成功的真实机器人视频。已有成功记录直接收录，不重跑；跳过交付检查和独立审核，不生成训练数据，不申请 project_deliver。请自主继续缺少视频任务的真实机器人执行；宿主按原始已完成 rollout 记录收录。`
        : `交付仍未通过：${JSON.stringify(delivery?.missing ?? delivery?.issues ?? generation.requiredOutputs)}。` +
          '请读取当前工程和具体缺项，按本次交付范围完成必要生成、观察、实际验证与对应消费者，再申请 project_deliver；本次轮次结束或计算完成不代表交付完成。');
    generation.pendingContinuation = { id, input, status: 'pending', createdAt: new Date(this.now()).toISOString(),
      requirementVersion: generation.requirementVersion, requestHash: generation.requestHash, progress };
    generation.continuationCount++;
    await this._save(project, { type: 'continuation_planned', continuationId: id, input });
    return this._dispatch(project);
  }

  async _dispatch(project) {
    if (project.generation.cancelRequestedAt) return this._reconcileStop(project);
    if (project.generation.waitingSystem) return this._waiting(project);
    const generation = project.generation, pending = generation.pendingContinuation;
    pending.dispatchAttemptedAt = new Date(this.now()).toISOString();
    await this._save(project);
    if (project.generation !== generation || generation.pendingContinuation !== pending) return project.generation;
    if (generation.cancelRequestedAt) return this._reconcileStop(project);
    if (generation.waitingSystem) {
      delete pending.dispatchAttemptedAt;
      return this._waiting(project);
    }
    if (this._accepted(project) || this._jobs(project).some(job => !jobTerminal.has(job.status))) {
      delete pending.dispatchAttemptedAt;
      return this._reconcile(project);
    }
    if (project.activeTurnId || this.hasApprovals(project) || this.hasOpeningInput(project)) {
      delete pending.dispatchAttemptedAt;
      return this._save(project);
    }
    const budgetReason = this._budgetReason(generation);
    if (budgetReason) {
      delete pending.dispatchAttemptedAt;
      return this._stop(project, budgetReason);
    }
    const beforeSend = () => {
      let reason;
      if (project.generation !== generation || generation.pendingContinuation !== pending) reason = '续接所属的生成任务或待发送记录已经变化。';
      else if (generation.cancelRequestedAt) reason = '生成任务正在停止，不能启动新轮次。';
      else if (generation.waitingSystem) reason = '生成任务正在等待系统处理，不能启动新轮次。';
      else if (this._accepted(project)) reason = '生成任务已经交付，不能启动新的续接。';
      else if (project.activeTurnId || this._jobs(project).some(job => !jobTerminal.has(job.status))
        || this.hasApprovals(project) || this.hasOpeningInput(project)) reason = '仍有原生轮次、作业、审批或输入，不能发送新的续接。';
      if (reason) throw Object.assign(new Error(reason), { nativeInputNotSent: true });
      this.assertBudget(generation);
    };
    try {
      const turn = await this.continueTurn(project, pending.input, { eventId: pending.id, beforeSend });
      if (project.generation !== generation || generation.pendingContinuation !== pending) return project.generation;
      if (!turn?.id) throw new Error('原生续接未返回轮次标识');
      pending.status = 'sent'; pending.turnId = turn.id;
      project.activeTurnId = turn.status === 'inProgress' ? turn.id : null;
      generation.status = generation.cancelRequestedAt ? 'cancel_requested' : generation.waitingSystem ? 'waiting_system' : 'active';
      delete generation.recoveryIssue;
      return this._save(project, { type: 'continuation_sent', continuationId: pending.id, turnId: turn.id });
    } catch (error) {
      if (project.generation !== generation || generation.pendingContinuation !== pending) return project.generation;
      if (error.nativeInputNotSent === true) {
        delete pending.dispatchAttemptedAt;
        if (generation.cancelRequestedAt) return this._reconcileStop(project);
        if (generation.waitingSystem) return this._waiting(project);
        if (error.budgetExpired === true) return this._stop(project, error.message);
        generation.status = 'recovering';
        generation.recoveryIssue = `续接准备失败，原生输入尚未发送：${error.message}`;
        return this._save(project, { type: 'continuation_not_sent', continuationId: pending.id, error: error.message });
      }
      generation.status = generation.waitingSystem ? 'waiting_system' : 'recovering';
      generation.recoveryIssue = `续接响应不确定，须先查询原生会话：${error.message}`;
      return this._save(project, { type: 'continuation_uncertain', continuationId: pending.id, error: error.message });
    }
  }

  async _stop(project, reason) {
    this._requestStop(project, 'failed', reason);
    return this._reconcileStop(project);
  }
}
