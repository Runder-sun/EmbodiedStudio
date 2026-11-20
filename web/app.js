const $ = (id) => document.getElementById(id);
const dom = Object.fromEntries([...document.querySelectorAll('[id]')].map((node) => [node.id, node]));
const i18n = window.SceneAgentI18n || { t: (text, values = {}) => text.replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match), language: 'zh', locale: 'zh-CN', apply() {} };
const t = (text, values) => i18n.t(text, values);
const analysis = window.SceneAgentAnalysis || {};
let state = { projects: [], config: {}, codex: {} };
let activeId = window.SceneAgentAnalysis?.decodeShare?.(window.location?.hash).project || readPreference('sceneagent.project') || '';
let renderedProjectId = '';
let selectedTab = 'progress';
let activeSource = 'scene';
let sources = { scene: '', tasks: '', expert: '', rubrics: '' };
let sourceProjectId = '';
let sourceDirty = false;
let sourceRequest = 0;
let toastTimer;
let refreshTimer;
let refreshRequest = 0;
let refreshPromise;
let refreshQueued = false;
let previewUrl = '';
let videoUrl = '';
let videoPlaybackRequest = 0;
let reportUrl = '';
let reportValue;
let viewer;
let setupView;
let viewerError = '';
let previewRequest = 0;
let renderedApprovalsKey = '';
let renderedChatRows = [];
let renderedChatEmptyKey = '';
let renderedJobKey = '';
let renderedEventsKey = '';
let renderedDeliveryKey = '';
let renderedDataKey = '';
let renderedDataReportValue;
let renderedReport;
let chatMode = 'generate';
let sceneUrl = '';
let sceneSnapshot;
let selectedNodeId = '';
let sceneRequest = 0;
let spatialMode = 'default';
let previousSpatialMode = 'default';
let welcomeDraftMode = false;
let welcomePresetOverride = '';
let lastWelcomeVisual = null;
let pendingShare = analysis.decodeShare?.(window.location?.hash) || null;
let replay = { url: '', track: null, request: 0, playing: false, time: 0, frame: 0, last: 0 };
let cameraRigs = [];
let cameraRigsUrl = '';
let cameraRigsRequest = 0;
let contextNodeId = '';
const expandedNodes = new Set();
const pendingActions = new Set();
const chatDrafts = new Map();
const sourceSaves = new Map();
const previewRecoveries = new Map();
const approvals = new Map();
const streams = new Map();
const statusNames = { active: '正在生成', waiting_jobs: '等待运行结果', waiting_user: '等待确认', waiting_system: '等待系统处理', recovering: '正在恢复', cancel_requested: '正在停止', complete: '已交付', draft: '待开始', edited: '代码已更新', idle: '已就绪', ready: '已就绪', thinking: '正在生成', build: '构建中', rollout: '执行中', planning: '正在构思', generating: '生成中', queued: '排队中', running: '运行中', starting: '准备中', building: '构建中', completed: '已完成', succeeded: '已完成', success: '已完成', failed: '执行失败', error: '出现错误', interrupted: '已停止', cancelled: '已取消', stopped: '已停止', waiting: '等待确认', waiting_for_jobs: '等待运行结果', delivering: '正在检查交付', needs_attention: '需要处理', budget_exhausted: '已达本次预算', available: '可用', unavailable: '不可用', policy_error: '策略程序出错', environment_error: '仿真环境出错', score_unavailable: '评分不可用', truncated: '运行已截断' };
const activeStatuses = new Set(['queued', 'running', 'unknown', 'starting', 'building', 'generating']);
const sourceNames = { scene: '场景程序', tasks: '任务程序', expert: '机器人行为程序', rubrics: '任务评分程序' };

function readPreference(key) { try { return localStorage.getItem(key); } catch { return null; } }
function savePreference(key, value) { try { localStorage.setItem(key, value); } catch {} }

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function label(element, key, values = {}) {
  element.setAttribute('data-i18n', key);
  element.setAttribute('data-i18n-values', JSON.stringify(values));
  element.textContent = t(key, values);
  return element;
}

function uiNode(tag, className, key, values) { return label(node(tag, className), key, values); }

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const contentType = response.headers.get('content-type') || '';
  const result = contentType.includes('json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(result?.error || result?.message || (typeof result === 'string' ? result : t('请求未完成（{status}）', { status: response.status })));
  return result;
}

function project() {
  return state.projects.find((entry) => entry.id === activeId);
}

function projectPath(suffix = '') {
  return `/api/projects/${encodeURIComponent(activeId)}${suffix}`;
}

function actionKey(kind, projectId = activeId) { return projectId + ':' + kind; }
function actionPending(kind, projectId = activeId) { return pendingActions.has(actionKey(kind, projectId)); }

function rememberDraft() {
  chatDrafts.set(activeId, { text: dom['chat-input'].value || '', mode: chatMode,
    outputScope: dom['output-scope'].value || 'complete', environmentRefs: selectedEnvironments() });
}

function selectedEnvironments() {
  return [...dom['collection-layouts'].children].filter(row => row.children[0]?.checked).map(row => ({
    revision: row.dataset.revision, ...(row.dataset.split ? { split: row.dataset.split } : {}) }));
}

function outputScope(outputs) {
  return outputs?.length === 1 ? 'robot' : outputs?.length === 2 ? 'scene' : 'complete';
}

function generationOptions(current, scope) {
  const generation = current?.generation;
  if (generation && !['complete', 'failed', 'cancelled'].includes(generation.status)) return {
    requiredOutputs: generation.requiredOutputs || ['generation', 'demonstrations', 'evaluation'],
    ...(generation.environmentRefs ? { environmentRefs: generation.environmentRefs } : {}) };
  const requiredOutputs = scope === 'scene' ? ['generation', 'evaluation']
    : scope === 'robot' ? ['demonstrations'] : ['generation', 'demonstrations', 'evaluation'];
  if (scope !== 'robot') return { requiredOutputs };
  const environmentRefs = selectedEnvironments();
  if (!current || !environmentRefs.length) throw new Error(t('请先选择支持独立采集的冻结布局。'));
  return { requiredOutputs, environmentRefs };
}

function renderGenerationScope(current, busy) {
  const locked = current?.generation && !['complete', 'failed', 'cancelled'].includes(current.generation.status);
  const draft = chatDrafts.get(activeId);
  const scope = locked ? outputScope(current.generation.requiredOutputs) : draft?.outputScope || 'complete';
  dom['output-scope'].value = scope;
  dom['output-scope'].disabled = Boolean(locked) || chatMode !== 'generate';
  const environments = (current?.environmentRevisions || []).filter(row => row.capabilities?.stageSplit === true);
  dom['output-robot-option'].disabled = !environments.length;
  dom['collect-data'].disabled = !environments.length || busy;
  dom['collect-data'].title = !environments.length ? t('当前工程还没有支持独立采集的冻结环境布局') : busy ? t('请等待当前生成或远端作业完成') : '';
  dom['collection-options'].hidden = scope !== 'robot' || chatMode !== 'generate';
  const refs = locked ? current.generation.environmentRefs : draft?.environmentRefs;
  const key = JSON.stringify([activeId, environments, refs, Boolean(locked), i18n.language]);
  if (dom['collection-layouts'].dataset.key === key) return;
  dom['collection-layouts'].dataset.key = key;
  dom['collection-layouts'].replaceChildren(...environments.map((entry, index) => {
    const row = node('label', 'layout-choice');
    row.dataset.revision = entry.revision;
    if (entry.split) row.dataset.split = entry.split;
    const input = node('input'); input.type = 'checkbox';
    input.checked = Array.isArray(refs) ? refs.some(ref => ref.revision === entry.revision)
      : !['validation', 'test'].includes(entry.split);
    input.disabled = Boolean(locked);
    input.onchange = rememberDraft;
    const split = entry.split ? t({ train: '训练', validation: '验证', test: '测试' }[entry.split]) : t('未分组');
    row.append(input, node('span', '', t('布局 {number} · {revision} · {split}', {
      number: index + 1, revision: entry.revision.slice(0, 10), split })));
    return row;
  }));
}

function selectChatMode(mode) {
  chatMode = mode;
  for (const entry of document.querySelectorAll('[data-mode]')) {
    const active = entry.dataset.mode === chatMode;
    entry.classList.toggle('active', active);
    entry.setAttribute('aria-pressed', String(active));
  }
  dom['chat-input'].placeholder = t(mode === 'generate' ? '输入场景或任务要求' : '输入问题或设计想法');
}

function selectProject(id) {
  if (id === activeId) return;
  rememberDraft();
  activeId = id;
  const draft = chatDrafts.get(id);
  dom['chat-input'].value = draft?.text || '';
  selectChatMode(draft?.mode || 'generate');
  dom['output-scope'].value = draft?.outputScope || 'complete';
  if (id) savePreference('sceneagent.project', id);
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  dom.toast.textContent = message;
  dom.toast.classList.toggle('error', error);
  dom.toast.hidden = false;
  toastTimer = setTimeout(() => { dom.toast.hidden = true; }, error ? 8500 : 4000);
}

function statusLabel(status) {
  return t(statusNames[status] || status || '待开始');
}

function setStatus(element, label, ready, error) {
  element.replaceChildren(node('span', 'status-dot'), uiNode('span', '', label));
  element.classList.toggle('is-ready', !!ready);
  element.classList.toggle('is-error', !!error);
}

function messageText(message) {
  const content = message.content ?? message.text ?? message.message ?? '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map((part) => part.text || part.content || '').join('\n');
  return typeof content === 'object' ? JSON.stringify(content, null, 2) : String(content);
}

function artifactUrl(value) {
  const url = typeof value === 'string' ? value : value?.url || value?.href || '';
  if (!url) return '';
  try {
    const parsed = new URL(url, window.location.origin);
    return parsed.origin === window.location.origin && ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
  } catch { return ''; }
}

function projectArtifactUrl(reference) {
  if (typeof reference !== 'string' || !reference.startsWith(`projects/${activeId}/`)
    || reference.split('/').some(part => !part || part === '.' || part === '..')) return '';
  return artifactUrl(`${projectPath()}/artifacts/${encodeURIComponent(reference.split('/').at(-1))}?file=${encodeURIComponent(reference)}`);
}

function applyState(next) {
  state = { ...next, projects: Array.isArray(next.projects) ? next.projects : [] };
  if (Array.isArray(next.approvals)) {
    approvals.clear();
    next.approvals.forEach((entry) => approvals.set(String(entry.id), entry));
  }
  for (const [projectId, stream] of streams) {
    const target = state.projects.find((entry) => entry.id === projectId);
    if (!target?.activeTurnId) { streams.delete(projectId); continue; }
    const message = target.messages?.find((entry) => entry.id === stream.itemId);
    if (message && messageText(message).length > stream.text.length) stream.text = messageText(message);
  }
  if (!state.projects.some((entry) => entry.id === activeId)) selectProject(state.projects[0]?.id || '');
  if (activeId) savePreference('sceneagent.project', activeId);
  render();
  setupView?.applyState(state);
}

function refreshState() {
  if (refreshPromise) { refreshQueued = true; return refreshPromise; }
  refreshPromise = (async () => {
    do {
      refreshQueued = false;
      const request = ++refreshRequest;
      try {
        const next = await api('/api/state');
        if (request === refreshRequest) applyState(next);
      } catch (error) { if (request === refreshRequest) toast(t('读取工作台失败：{error}', { error: error.message }), true); }
    } while (refreshQueued);
  })().finally(() => { refreshPromise = undefined; });
  return refreshPromise;
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshState, 180);
}

function render() {
  const current = project();
  const changed = renderedProjectId !== activeId;
  const select = dom['project-select'];
  const options = state.projects.length ? state.projects.map(entry => [entry.id, entry.name || t('未命名工程')]) : [['', t('还没有场景工程')]];
  if (select.options.length !== options.length || options.some(([id, label], index) => select.options[index]?.value !== id || select.options[index]?.textContent !== label)) {
    select.replaceChildren(...options.map(([id, label]) => {
      const option = node('option', '', label);
      option.value = id;
      return option;
    }));
  }
  if (select.value !== activeId) select.value = activeId;
  select.title = current?.name || t('选择场景工程');
  select.setAttribute('aria-label', current?.name ? t('当前工程：{name}', { name: current.name }) : t('选择场景工程'));
  const remoteHost = state.config?.runtimeMode === 'local' ? t('本机计算') : state.config?.remoteHost || t('未配置计算');
  dom['remote-info'].textContent = remoteHost;
  dom['remote-info'].title = state.config?.remoteRoot || '';
  setStatus(dom['codex-status'], state.codex?.ready ? 'Codex · 就绪' : 'Codex · 未就绪', state.codex?.ready, !state.codex?.ready);
  dom['codex-status'].title = state.codex?.error || '';
  const systemWait = current?.generation?.waitingSystem;
  dom['system-wait-detail'].hidden = !systemWait;
  dom['system-wait-reason'].textContent = systemWait?.reason || '';
  if (changed) dom['system-wait-detail'].open = false;
  dom['project-status'].hidden = !current;
  dom['project-status'].textContent = statusLabel(current?.status);
  dom['project-status'].classList.toggle('is-error', ['failed', 'error'].includes(current?.status));
  dom['project-status'].classList.toggle('is-waiting', !!systemWait);
  const generationBusy = ['active', 'waiting_jobs', 'waiting_user', 'waiting_system', 'recovering', 'cancel_requested'].includes(current?.generation?.status);
  const busy = !!current?.activeTurnId || generationBusy || actionPending('chat');
  renderGenerationScope(current, busy);
  const remoteBusy = current?.jobs?.some((job) => activeStatuses.has(job.status));
  dom['interrupt'].hidden = !busy && !remoteBusy;
  dom['interrupt'].disabled = actionPending('interrupt');
  dom['resume-generation'].hidden = !systemWait;
  dom['resume-generation'].disabled = !systemWait || !!current?.activeTurnId || remoteBusy || !state.codex?.ready || actionPending('resume');
  dom['send-message'].disabled = actionPending('chat') || !state.codex?.ready;
  dom['composer-hint'].textContent = !state.codex?.ready ? t('Codex 尚未就绪') : systemWait ? t('等待系统处理') : busy ? t('Codex 正在处理') : t('Enter 发送');
  dom['composer-hint'].title = systemWait ? t('补充内容只会保存；系统问题处理后，点击继续当前任务。') : t('Enter 发送 · Shift + Enter 换行');
  dom['create-project'].textContent = state.codex?.ready ? t('创建并开始') : t('创建工程');
  dom['edit-source'].disabled = !current;
  dom['build-scene'].disabled = !current || actionPending('build') || current.jobs?.some((job) => job.kind === 'build' && activeStatuses.has(job.status));
  dom['run-robot'].disabled = !current || !(current.latestBuild || current.artifacts?.preview) || actionPending('rollout') || current.jobs?.some((job) => job.kind === 'rollout' && activeStatuses.has(job.status));
  const revision = current?.latestBuild?.revision || current?.artifacts?.preview?.revision;
  dom['scene-version'].textContent = revision ? t('版本 {revision}', { revision: String(revision).slice(0, 10) }) : current?.artifacts?.preview ? t('已生成') : t('等待构建');
  renderChat(changed);
  renderApprovals();
  renderProgress();
  renderArtifacts(current, changed);
  renderDelivery(current);
  syncWelcomeStage(current);
  syncSpatialBlueprint(current, busy || remoteBusy);
  renderProjectOverview();
  renderedProjectId = activeId;
}

function renderProjectOverview() {
  const running = state.projects.filter(entry => entry.activeTurnId || ['active', 'recovering', 'waiting_jobs'].includes(entry.generation?.status)
    || entry.jobs?.some(job => activeStatuses.has(job.status)));
  dom['project-running-count'].textContent = t('{count} 进行中', { count: running.length });
  dom['project-cards'].replaceChildren(...state.projects.map(entry => {
    const card = node('button', 'project-card');
    card.type = 'button'; card.title = entry.name;
    card.setAttribute('aria-current', String(entry.id === activeId));
    const pending = [...approvals.values()].filter(value => value.projectId === entry.id).length;
    const devices = [...new Set((entry.jobs || []).filter(job => activeStatuses.has(job.status) && job.device).map(job => job.device.index))];
    const detail = [statusLabel(entry.status), pending ? t('{count} 待确认', { count: pending }) : '', devices.map(index => 'GPU ' + index).join(', ')].filter(Boolean).join(' · ');
    card.append(node('strong', '', entry.name), node('span', '', detail));
    card.onclick = () => { selectProject(entry.id); render(); };
    return card;
  }));
}

function renderChat(changed = false) {
  const current = project();
  const feed = dom['chat-feed'];
  if (changed) { renderedChatRows = []; renderedChatEmptyKey = ''; }
  const atEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 75;
  const scrollTop = feed.scrollTop;
  if (!current || !(current.messages?.length || streams.has(activeId))) {
    const key = JSON.stringify([activeId, current?.prompt]);
    if (key === renderedChatEmptyKey) return;
    renderedChatEmptyKey = key;
    renderedChatRows = [];
    feed.replaceChildren();
    const empty = node('div', 'conversation-empty');
    empty.append(node('p', '', current?.prompt || t('暂无消息')));
    feed.append(empty);
    return;
  }
  renderedChatEmptyKey = '';
  const messages = current.messages || [];
  const stream = streams.get(activeId);
  const rows = messages.map((message, index) => {
    const isStreaming = stream && (message.id === stream.itemId || message.itemId === stream.itemId);
    return { key: message.id || message.itemId || String(index), role: message.role,
      text: isStreaming ? stream.text : messageText(message), streaming: !!(isStreaming || (message.status === 'streaming' && current.activeTurnId)) };
  });
  if (stream && !messages.some((message) => message.id === stream.itemId || message.itemId === stream.itemId || (message.role === 'assistant' && messageText(message) === stream.text))) {
    rows.push({ key: stream.itemId, role: 'assistant', text: stream.text, streaming: true });
  }
  let contentChanged = false;
  for (const row of rows) {
    const previous = renderedChatRows.find(entry => entry.key === row.key && entry.role === row.role);
    row.element = previous?.element || chatMessage({ role: row.role, content: row.text }, row.streaming);
    if (previous && (previous.text !== row.text || previous.streaming !== row.streaming)) {
      contentChanged = true;
      const body = row.element.lastElementChild;
      if (previous.text !== row.text) body.textContent = row.text;
      body.className = `message-body${row.streaming ? ' message-streaming' : ''}`;
    }
  }
  const structureChanged = rows.length !== renderedChatRows.length || rows.some((row, index) => row.element !== renderedChatRows[index]?.element);
  if (!structureChanged && !contentChanged) return;
  if (structureChanged) {
    feed.replaceChildren(...rows.map(row => row.element));
  }
  renderedChatRows = rows;
  if (changed || atEnd) feed.scrollTop = feed.scrollHeight;
  else feed.scrollTop = scrollTop;
}

function chatMessage(message, streaming = false) {
  const role = ['user', 'assistant', 'system'].includes(message.role) ? message.role : 'system';
  const article = node('article', `chat-message ${role}`);
  const heading = node('div', 'message-heading');
  if (role !== 'user') heading.append(node('span', 'message-avatar', role === 'assistant' ? '✳' : '·'));
  heading.append(node('span', '', role === 'user' ? t('你') : role === 'assistant' ? 'Codex' : t('工程记录')));
  article.append(heading, node('div', `message-body${streaming ? ' message-streaming' : ''}`, messageText(message)));
  return article;
}

function approvalDetails(entry) {
  const params = entry.params || {};
  const command = params.command || params.commandLine || params.proposedExecCommand;
  const changes = params.changes || params.fileChanges;
  return command ? (Array.isArray(command) ? command.join(' ') : command) : changes ? JSON.stringify(changes, null, 2) : JSON.stringify(params, null, 2);
}

function renderApprovals() {
  const key = JSON.stringify([activeId, [...approvals.entries()].filter(([, entry]) => entry.projectId === activeId)]);
  if (key === renderedApprovalsKey) return;
  renderedApprovalsKey = key;
  dom.approvals.replaceChildren();
  for (const [id, entry] of approvals) {
    if (entry.projectId !== activeId) continue;
    const card = node('div', 'approval-card');
    if (entry.method === 'item/tool/requestUserInput') {
      const form = node('form', 'question-form');
      form.append(uiNode('h3', '', 'Codex 需要你的回答'));
      const fields = [];
      for (const question of entry.params?.questions || []) {
        const group = node('fieldset', 'question-field');
        group.append(node('legend', '', question.header || '补充信息'), node('p', '', question.question || '请填写你的回答'));
        let select;
        if (question.options?.length) {
          select = node('select');
          select.setAttribute('aria-label', question.question || question.header || '选择回答');
          const placeholder = uiNode('option', '', '请选择一个回答');
          placeholder.value = '';
          select.append(placeholder);
          for (const option of question.options) {
            const choice = node('option', '', `${option.label}${option.description ? ` · ${option.description}` : ''}`);
            choice.value = option.label;
            select.append(choice);
          }
          group.append(select);
        }
        const text = node('textarea');
        text.rows = 2;
        const placeholder = select ? '或填写自己的回答' : '填写你的回答';
        text.setAttribute('data-i18n-placeholder', placeholder);
        text.placeholder = t(placeholder);
        const values = { question: question.question || question.header || t('问题') };
        text.setAttribute('data-i18n-aria-label', '{question}：自由回答');
        text.setAttribute('data-i18n-values', JSON.stringify(values));
        text.setAttribute('aria-label', t('{question}：自由回答', values));
        group.append(text);
        fields.push({ id: question.id, select, text });
        form.append(group);
      }
      const errorText = node('p', 'form-error');
      errorText.hidden = true;
      const submit = uiNode('button', 'button small primary', '提交回答');
      submit.type = 'submit';
      const actions = node('div', 'approval-actions');
      actions.append(submit);
      form.append(errorText, actions);
      form.onsubmit = async (event) => {
        event.preventDefault();
        const answers = {};
        for (const field of fields) {
          const value = field.text.value.trim() || field.select?.value;
          if (!value) { label(errorText, '请回答每个问题后再提交。'); errorText.hidden = false; return; }
          answers[field.id] = { answers: [value] };
        }
        submit.disabled = true;
        try {
          await api(`/api/approvals/${encodeURIComponent(id)}`, { method: 'POST', body: { decision: 'accept', answers } });
          approvals.delete(id);
          renderApprovals();
        } catch (error) { errorText.textContent = error.message; errorText.hidden = false; submit.disabled = false; }
      };
      card.append(form);
      dom.approvals.append(card);
      continue;
    }
    const supported = /commandExecution\/requestApproval|fileChange\/requestApproval|execCommandApproval|applyPatchApproval/.test(entry.method);
    card.append(uiNode('h3', '', supported ? 'Codex 需要确认这个操作' : 'Codex 请求了尚未支持的交互'), node('div', '', entry.params?.reason || entry.params?.reasoning || entry.method), node('pre', '', approvalDetails(entry)));
    const actions = node('div', 'approval-actions');
    for (const [decision, label] of [['decline', '拒绝'], ['accept', '批准']]) {
      if (!supported && decision === 'accept') continue;
      const button = uiNode('button', `button small ${decision === 'accept' ? 'primary' : 'secondary'}`, label);
      button.onclick = async () => {
        actions.querySelectorAll('button').forEach((item) => { item.disabled = true; });
        try {
          await api(`/api/approvals/${encodeURIComponent(id)}`, { method: 'POST', body: { decision } });
          approvals.delete(id);
          renderApprovals();
        } catch (error) { toast(t('确认操作失败：{error}', { error: error.message }), true); actions.querySelectorAll('button').forEach((item) => { item.disabled = false; }); }
      };
      actions.append(button);
    }
    card.append(actions);
    dom.approvals.append(card);
  }
}

function formatTime(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString(i18n.locale, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—';
}

function displayEvent(entry) {
  const operation = entry.operation === 'rollout' ? t('机器人执行') : t('场景构建');
  const types = { user_message: t('需求已保存'), operation_started: t('开始{operation}', { operation }), operation_completed: t('{operation}完成', { operation }), operation_failed: t('{operation}失败', { operation }), approval_requested: t('等待操作确认'),
    generation_waiting_system: t('等待系统处理，已停止自动续接'), generation_resumed: t('已恢复原生成任务') };
  if (types[entry.type]) return { ...entry, message: types[entry.type], detail: entry.error || '' };
  const plain = analysis.eventLabel ? analysis.eventLabel(entry).split(' · ').map(part => t(part)).join(' · ') : '';
  if (entry.type === 'domain_event') return { ...entry, message: entry.message || plain || entry.stage || t('远端状态更新') };
  if (entry.type !== 'codex') return { ...entry, message: entry.message || entry.title || plain || entry.stage || entry.type || t('工程状态更新') };
  const { method, params = {} } = entry;
  if (method === 'turn/started') return { ...entry, message: t('Codex 开始生成') };
  if (method === 'turn/completed') return { ...entry, message: params.turn?.status === 'completed' ? t('Codex 已完成当前回复') : t('Codex 当前任务已结束') };
  if (method === 'error') {
    const message = params.error?.message || params.message || t('Codex 执行出现错误');
    return { ...entry, type: 'error', message: message.replace(/^Reconnecting\.\.\.\s*(\d+\/\d+)/, t('模型连接恢复中（$1）')) };
  }
  const item = params.item || {};
  const names = { commandExecution: t('执行命令'), fileChange: t('修改工程文件'), mcpToolCall: t('调用场景工具'), webSearch: t('检索资料') };
  if (['item/started', 'item/completed'].includes(method) && names[item.type]) return { ...entry, message: `Codex ${names[item.type]}${method === 'item/completed' ? t(' · 已结束') : ''}`, detail: item.command || item.tool || item.name || '' };
  return null;
}

function renderProgress() {
  const current = project();
  const job = current?.jobs?.at(-1);
  const jobKey = JSON.stringify([activeId, job]);
  if (jobKey !== renderedJobKey) {
    renderedJobKey = jobKey;
    dom['job-status'].hidden = !job;
    dom['job-status'].replaceChildren();
    if (job) {
      const line = node('div', 'job-status-line');
      const jobName = { build: t('场景构建'), rollout: t('机器人执行'), observe: t('场景观察'), check: t('任务检查') }[job.kind] || t('远端作业');
      const numericProgress = typeof job.progress === 'number' && Number.isFinite(job.progress);
      const progress = numericProgress ? Math.max(0, Math.min(100, job.progress <= 1 ? job.progress * 100 : job.progress)) : null;
      line.append(node('strong', '', t('最近作业：{name} · {status}', { name: jobName, status: statusLabel(job.status) })), node('span', '', progress === null ? '' : `${Math.round(progress)}%`));
      dom['job-status'].append(line);
      if (job.stage || job.message || job.error) dom['job-status'].append(node('div', `job-stage${job.error ? ' is-error' : ''}`, job.error || job.message || job.stage));
      if (progress !== null) {
        const track = node('div', 'progress-track');
        const fill = node('div', 'progress-fill');
        fill.style.width = `${progress}%`;
        track.setAttribute('role', 'progressbar');
        track.setAttribute('aria-valuenow', String(Math.round(progress)));
        track.setAttribute('aria-valuemin', '0');
        track.setAttribute('aria-valuemax', '100');
        track.setAttribute('aria-label', jobName);
        track.append(fill);
        dom['job-status'].append(track);
      }
    }
  }
  const eventsKey = JSON.stringify([activeId, current?.events]);
  if (eventsKey === renderedEventsKey) return;
  renderedEventsKey = eventsKey;
  const events = (current?.events || []).map(displayEvent).filter(Boolean);
  dom['event-count'].textContent = String(events.length);
  const list = dom['event-list'];
  const opened = new Map(renderedProjectId === activeId ? [...list.querySelectorAll('[data-event-key]')]
    .map(details => [details.dataset.eventKey, { open: details.open, scrollTop: details.querySelector('pre').scrollTop }]) : []);
  const detailScroll = [];
  const scrollTop = dom['panel-progress'].scrollTop;
  const atEnd = dom['panel-progress'].scrollHeight - dom['panel-progress'].scrollTop - dom['panel-progress'].clientHeight < 60;
  list.replaceChildren();
  if (!events.length) {
    const empty = node('div', 'result-empty');
    empty.append(node('h3', '', t('暂无生成记录')));
    list.append(empty);
  } else {
    for (const entry of events.slice(-150)) {
      const row = node('div', `event-row${entry.type === 'error' || entry.error ? ' error' : ''}`);
      const title = node('div', 'event-title', entry.message);
      if (entry.detail) {
        const text = typeof entry.detail === 'string' ? entry.detail : JSON.stringify(entry.detail);
        if (entry.type === 'codex') {
          const details = node('details', 'event-detail');
          details.dataset.eventKey = entry.eventId || JSON.stringify([entry.type, entry.method, entry.time || entry.timestamp, entry.params?.item?.id, entry.detail]);
          const previous = opened.get(details.dataset.eventKey);
          const pre = node('pre', '', text);
          details.open = previous?.open || false;
          if (previous) detailScroll.push([pre, previous.scrollTop]);
          details.append(node('summary', '', t('查看执行细节')), pre);
          title.append(details);
        } else title.append(node('div', 'event-detail', text));
      }
      row.append(node('span', 'event-time', formatTime(entry.time || entry.timestamp)), node('span', 'event-marker'), title);
      list.append(row);
    }
  }
  for (const [pre, offset] of detailScroll) pre.scrollTop = offset;
  if (atEnd) dom['panel-progress'].scrollTop = dom['panel-progress'].scrollHeight;
  else dom['panel-progress'].scrollTop = scrollTop;
}

function previewRecoveryKey(current) { return `${current.id}:${current.generation?.id}:${current.latestBuild?.revision}`; }

function canRecoverPreview(current) {
  return current?.generation?.id && current.latestBuild?.revision && !current.artifacts?.preview
    && !current.activeTurnId && !current.jobs?.some(job => activeStatuses.has(job.status))
    && !['active', 'waiting_jobs', 'waiting_user', 'waiting_system', 'recovering', 'cancel_requested', 'cancelled', 'failed'].includes(current.generation.status);
}

async function recoverPreview(current, retry = false) {
  if (!canRecoverPreview(current) || activeStatuses.has(current.webPreview?.status)) return;
  const key = previewRecoveryKey(current);
  if (!retry && (previewRecoveries.has(key) || current.webPreview)) return;
  const attempt = { requestId: 'web_preview_' + crypto.randomUUID().replaceAll('-', ''), pending: true };
  previewRecoveries.set(key, attempt);
  try {
    await api('/api/tools/scene_observe', { method: 'POST', body: { projectId: current.id,
      generationId: current.generation.id, revision: current.latestBuild.revision,
      requestId: attempt.requestId, output: 'web_preview' } });
    await refreshState();
  } catch (error) { attempt.error = error.message; }
  finally {
    attempt.pending = false;
    if (activeId === current.id) renderArtifacts(project(), false);
  }
}

function renderArtifacts(current, changed) {
  const nextScene = artifactUrl(current?.artifacts?.scene);
  if (nextScene !== sceneUrl || changed) {
    sceneUrl = nextScene;
    if (changed) { selectedNodeId = ''; expandedNodes.clear(); }
    sceneSnapshot = undefined;
    renderSceneTree();
    fetchScene(nextScene);
  }
  const nextPreview = artifactUrl(current?.artifacts?.preview);
  if (nextPreview !== previewUrl || changed) {
    previewUrl = nextPreview;
    loadPreview(nextPreview);
  }
  if (!nextPreview && current?.latestBuild?.revision) {
    void recoverPreview(current);
    const attempt = previewRecoveries.get(previewRecoveryKey(current));
    const converting = activeStatuses.has(current.webPreview?.status) || attempt?.pending;
    const error = current.webPreview?.error || attempt?.error;
    dom['scene-empty'].hidden = true;
    dom['scene-loading'].hidden = !converting;
    dom['preview-status'].replaceChildren(node('span', 'status-dot'), uiNode('span', '', converting ? '正在生成网页预览' : '原生场景已构建 · 网页预览不可用'));
    dom['scene-error'].replaceChildren(node('span', '', error ? t('网页预览生成失败：{error}', { error })
      : t('网页预览转换暂不支持此场景，仿真和原生图像检查可继续。')));
    if (!converting && canRecoverPreview(current)) {
      const retry = uiNode('button', 'button secondary', '重新生成网页预览');
      retry.type = 'button'; retry.onclick = () => { void recoverPreview(current, true); renderArtifacts(current, false); };
      dom['scene-error'].append(retry);
    }
    dom['scene-error'].hidden = !!converting;
  }
  renderVideo(current, changed);
  const nextTransitions = artifactUrl(current?.artifacts?.transitions);
  if (nextTransitions !== replay.url || changed) {
    stopReplay();
    clearReplayOverlay();
    replay = { ...replay, url: nextTransitions, track: null, error: '', loading: false, time: 0, request: replay.request + 1 };
    renderReplay();
    if (nextTransitions && selectedTab === 'replay') ensureReplayLoaded();
  }
  const nextTasks = artifactUrl(current?.artifacts?.tasks);
  if (nextTasks !== cameraRigsUrl || changed) {
    cameraRigsUrl = nextTasks;
    cameraRigs = [];
    clearCameraRigs();
    if (dom['tool-cameras']) {
      dom['tool-cameras'].disabled = true;
      dom['tool-cameras'].setAttribute('aria-pressed', 'false');
      dom['tool-cameras'].title = nextTasks ? t('正在读取冻结相机…') : t('当前工程没有冻结的任务相机');
    }
    if (nextTasks) fetchTasksCameras(nextTasks);
  }
  const nextReport = artifactUrl(current?.artifacts?.report);
  if (nextReport !== reportUrl) {
    reportUrl = nextReport;
    reportValue = undefined;
    renderedReport = undefined;
    if (nextReport) fetchReport(nextReport);
  }
  renderData();
}

function videoEpisodes(current) {
  const rows = new Map();
  const add = episode => {
    const id = episode?.episode || episode?.episodeId;
    if (typeof id !== 'string' || rows.has(id) || !episode.media?.cameras?.some(camera => typeof camera.video === 'string'
      && camera.status !== 'unavailable' && camera.video.startsWith(`projects/${current.id}/episodes/${id}/`))) return;
    rows.set(id, episode);
  };
  add(current?.latestEpisode);
  for (const job of [...(current?.jobs || [])].reverse()) {
    if (job.kind === 'rollout' && job.status === 'completed' && job.result?.projectId === current.id) add(job.result);
  }
  return [...rows.values()];
}

function selectedVideoEpisode(current) {
  const rows = videoEpisodes(current);
  return rows.find(episode => (episode.episode || episode.episodeId) === dom['video-episode'].value) || rows[0] || current?.latestEpisode;
}

function renderVideo(current, changed = false) {
  if (changed) closeVideoPreview();
  const video = dom['robot-video'], select = dom['video-camera'];
  const runs = dom['video-episode'], episodes = videoEpisodes(current);
  const previousRun = !changed && runs.dataset.projectId === current?.id ? runs.value : '';
  const runOptionsKey = JSON.stringify([current?.id, i18n.language, episodes.map(episode => [episode.episode || episode.episodeId, episode.revision])]);
  if (runs.dataset.optionsKey !== runOptionsKey) {
    const latest = node('option', '', t('最新可用记录')); latest.value = '';
    runs.replaceChildren(latest, ...episodes.map(episode => {
      const id = episode.episode || episode.episodeId;
      const option = node('option', '', t('回合 {episode} · 版本 {revision}', { episode: id.slice(0, 8), revision: String(episode.revision || '—').slice(0, 10) }));
      option.value = id; return option;
    }));
    runs.dataset.optionsKey = runOptionsKey;
  }
  runs.dataset.projectId = current?.id || '';
  runs.value = episodes.some(episode => (episode.episode || episode.episodeId) === previousRun) ? previousRun : '';
  runs.hidden = !episodes.length;
  dom['video-episode-label'].hidden = !episodes.length;
  const episode = selectedVideoEpisode(current), episodeId = episode?.episode || episode?.episodeId;
  const media = episode?.media, cameras = media?.cameras || [];
  const sameEpisode = !changed && select.dataset.episodeId === episodeId;
  const previous = cameras.find(row => row.cameraId === select.dataset.cameraId);
  const selected = cameras.find(row => row.cameraId === (sameEpisode ? select.value : media?.primaryCameraId))
    || cameras.find(row => row.cameraId === media?.primaryCameraId);
  const selectionChanged = !sameEpisode || select.dataset.cameraId !== selected?.cameraId;
  const instant = sameEpisode && previous && Number.isFinite(previous.firstTime)
    ? previous.firstTime + video.currentTime : selected?.firstTime;
  const optionsKey = JSON.stringify([i18n.language, cameras.map(row => [row.cameraId, row.status])]);
  const optionsChanged = select.dataset.optionsKey !== optionsKey;
  if (optionsChanged) {
    select.replaceChildren(...cameras.map(row => {
      const option = node('option', '', row.cameraId + (row.status === 'unavailable' ? t('（无记录）') : row.status === 'partial' ? t('（部分记录）') : ''));
      option.value = row.cameraId;
      return option;
    }));
    select.dataset.optionsKey = optionsKey;
  }
  select.value = selected?.cameraId || '';
  select.dataset.episodeId = episodeId || '';
  select.dataset.cameraId = selected?.cameraId || '';
  dom['video-views'].hidden = !cameras.length;
  const belongs = reference => current?.id && episodeId && typeof reference === 'string' && reference.startsWith(`projects/${current.id}/episodes/${episodeId}/`);
  const legacyUrl = artifactUrl(current?.artifacts?.video);
  const legacyReference = legacyUrl ? new URL(legacyUrl).searchParams.get('file') : '';
  const legacyVideo = belongs(legacyReference) ? projectArtifactUrl(legacyReference) : '';
  const nextVideo = cameras.length ? selected && belongs(selected.video) ? projectArtifactUrl(selected.video) : '' : legacyVideo;
  const unavailableInstant = selected && Number.isFinite(instant) && (instant < selected.firstTime || instant > selected.lastTime);
  let note = unavailableInstant ? t('切换时刻没有记录，当前显示已保存片段的开头。')
    : selected?.status === 'partial' ? t('此视角仅保存了部分记录。') : '';
  if (selected && !nextVideo) note = selected.status === 'unavailable' ? t('此视角没有可用记录。') : t('此视角未提供视频，请下载轨迹查看已记录图像。');
  if (selectionChanged || optionsChanged || nextVideo !== videoUrl) {
    dom['video-availability'].textContent = note;
    dom['video-availability'].hidden = !note;
  }
  if (nextVideo !== videoUrl) {
    const resume = sameEpisode && !video.paused && selectedTab === 'video';
    const playbackRequest = ++videoPlaybackRequest;
    videoUrl = nextVideo;
    video.pause();
    video.preload = selectedTab === 'video' ? 'metadata' : 'none';
    if (nextVideo) video.src = nextVideo;
    else video.removeAttribute('src');
    video.onloadedmetadata = () => {
      if (videoUrl !== nextVideo || select.dataset.episodeId !== (episodeId || '')) return;
      if (selected && Number.isFinite(instant) && Number.isFinite(selected.firstTime)) {
        video.currentTime = unavailableInstant ? 0 : Math.max(0, instant - selected.firstTime);
        if (resume && !unavailableInstant && playbackRequest === videoPlaybackRequest && selectedTab === 'video') video.play().catch(() => {});
      }
    };
    video.load();
  }
  if (!nextVideo) closeVideoPreview();
  dom['expand-video'].hidden = !nextVideo;
  dom['expand-video'].disabled = !nextVideo;
  video.hidden = !nextVideo;
  dom['video-stage'].hidden = !nextVideo;
  dom['video-download'].hidden = !nextVideo;
  if (nextVideo) dom['video-download'].href = nextVideo;
  else dom['video-download'].removeAttribute('href');
  dom['video-empty'].hidden = !!nextVideo;
  dom['video-caption'].hidden = !nextVideo && !cameras.length;
  const historical = episode?.revision && current?.latestBuild?.revision && episode.revision !== current.latestBuild.revision;
  dom['video-caption'].textContent = episodeId ? t('真实执行记录 · {episodeId}', { episodeId })
    + (episode.revision ? ' · ' + t('版本 {revision}', { revision: episode.revision }) : '')
    + (historical ? ' · ' + t('历史版本') : '') : t('真实执行记录 · 由仿真生成');
  const annotations = episode?.artifacts?.annotations;
  dom['video-annotations'].hidden = !belongs(annotations);
  if (belongs(annotations)) dom['video-annotations'].href = projectArtifactUrl(annotations);
  else dom['video-annotations'].removeAttribute('href');
}

function pauseVideoPreview() {
  ++videoPlaybackRequest;
  dom['robot-video'].pause();
}

function openVideoPreview() {
  const dialog = dom['video-dialog'];
  if (!videoUrl || dom['robot-video'].hidden || dialog.open) return;
  dom['video-dialog-content'].append(dom['video-content']);
  dialog.showModal();
  dom['expand-video'].setAttribute('aria-expanded', 'true');
  dom['robot-video'].play().catch(() => {});
}

function closeVideoPreview() {
  if (!dom['video-dialog'].open) return;
  pauseVideoPreview();
  dom['video-dialog'].close();
}

function changeLanguage(language, redraw = true) {
  i18n.setLanguage?.(language);
  i18n.apply(document);
  setupView?.languageChanged();
  dom['ui-language'].value = i18n.language;
  selectChatMode(chatMode);
  if (!redraw) return;
  renderedChatRows = [];
  renderedChatEmptyKey = renderedJobKey = renderedEventsKey = renderedDeliveryKey = renderedDataKey = '';
  renderedReport = undefined;
  dom['video-camera'].dataset.optionsKey = '';
  render();
  renderSceneTree();
}

function initializeLayout() {
  const workspace = document.querySelector('.workspace'), divider = dom['workspace-divider'];
  const maximum = () => Math.max(260, Math.min(520, Math.floor(window.innerWidth * .48)));
  const setWidth = width => {
    const value = Math.max(260, Math.min(maximum(), width));
    workspace.style.setProperty('--chat-width', value + 'px');
    divider.setAttribute('aria-valuenow', String(Math.round(value)));
    divider.setAttribute('aria-valuemax', String(maximum()));
    return value;
  };
  const persist = () => savePreference('sceneagent.chat-width', divider.getAttribute('aria-valuenow'));
  const preferred = Number(readPreference('sceneagent.chat-width'));
  if (window.innerWidth > 760) setWidth(preferred > 0 ? preferred : window.innerWidth <= 1000 ? 280 : window.innerWidth >= 1500 ? 350 : 320);
  divider.onpointerdown = event => {
    divider.setPointerCapture(event.pointerId);
    workspace.classList.add('is-resizing');
    divider.onpointermove = move => setWidth(move.clientX - workspace.getBoundingClientRect().left);
  };
  divider.onlostpointercapture = () => { divider.onpointermove = null; workspace.classList.remove('is-resizing'); persist(); };
  divider.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const width = Number(divider.getAttribute('aria-valuenow'));
    setWidth(event.key === 'Home' ? 260 : event.key === 'End' ? maximum() : width + (event.key === 'ArrowLeft' ? -16 : 16));
    persist();
  };
  window.addEventListener('resize', () => { if (window.innerWidth > 760) setWidth(Number(divider.getAttribute('aria-valuenow')) || 320); });
}

async function fetchScene(url) {
  const request = ++sceneRequest;
  if (!url) return;
  try {
    const result = await api(url);
    if (request !== sceneRequest) return;
    if (!Array.isArray(result.nodes)) throw new Error(t('场景内容不完整'));
    sceneSnapshot = result;
    const nodes = new Map(result.nodes.map((entry) => [entry.id, entry]));
    for (const entry of result.nodes) {
      const owner = nodes.get(entry.owner ?? entry.parent);
      if (entry.kind === 'space' && (!owner || !(owner.owner ?? owner.parent))) expandedNodes.add(entry.id);
    }
    if (!nodes.has(selectedNodeId)) selectedNodeId = '';
    renderSceneTree();
    reframeContent();
    if (pendingShare) applyShare(pendingShare);
    focusSceneNode(selectedNodeId);
  } catch (error) {
    if (request !== sceneRequest) return;
    dom['tree-selection'].replaceChildren(node('p', 'is-error', t('场景层级读取失败：{error}', { error: error.message })));
  }
}

function sceneChildren() {
  const children = new Map([[null, []]]);
  for (const entry of sceneSnapshot?.nodes || []) children.set(entry.id, []);
  for (const entry of sceneSnapshot?.nodes || []) {
    const owner = entry.owner ?? entry.parent ?? null;
    (children.get(owner) || children.get(null)).push(entry);
  }
  return children;
}

function treeRow(entry, depth, descendants, expanded, filtered) {
  const hidden = viewer?.hiddenIds?.has(entry.id);
  const row = node('div', `tree-row${entry.id === selectedNodeId ? ' selected' : ''}${hidden ? ' is-hidden' : ''}`);
  row.style.setProperty('--tree-depth', String(filtered ? 0 : Math.min(depth, 8)));
  const expand = node('button', 'tree-expand', descendants.length && !filtered ? expanded ? '⌄' : '›' : '·');
  expand.type = 'button';
  expand.disabled = !descendants.length || filtered;
  expand.setAttribute('aria-label', `${expanded ? t('收起') : t('展开')} ${nodeTitle(entry)}`);
  if (descendants.length && !filtered) expand.setAttribute('aria-expanded', String(expanded));
  expand.onclick = () => { expanded ? expandedNodes.delete(entry.id) : expandedNodes.add(entry.id); renderSceneTree(); };
  const select = node('button', 'tree-node');
  select.type = 'button';
  select.title = [entry.id, entry.purpose].filter(Boolean).join(' · ');
  select.setAttribute('aria-current', String(entry.id === selectedNodeId));
  select.append(node('span', 'tree-node-title', nodeTitle(entry)));
  const kind = nodeKindText(entry);
  if (kind) select.append(node('span', 'tree-node-kind', kind));
  if (entry.purpose) select.append(node('span', 'tree-node-sub', entry.purpose));
  select.onclick = () => { selectedNodeId = entry.id; renderSceneTree(); flyCamera(() => focusSceneNode(entry.id)); };
  const eye = node('button', 'tree-eye', hidden ? '◌' : '◉');
  eye.type = 'button';
  eye.disabled = !viewer?.model;
  eye.setAttribute('aria-pressed', String(!hidden));
  eye.setAttribute('aria-label', `${hidden ? t('显示') : t('隐藏')} ${nodeTitle(entry)}`);
  eye.title = hidden ? t('显示') : t('隐藏');
  eye.onclick = () => setNodeHidden(entry.id, !hidden);
  row.append(expand, select, eye);
  return row;
}

function renderSceneTree() {
  const available = !!sceneSnapshot;
  dom['toggle-tree'].disabled = !available;
  dom['scene-tree'].hidden = !available || dom['toggle-tree'].getAttribute('aria-pressed') !== 'true';
  dom['tree-count'].textContent = available ? t('{count} 个节点', { count: sceneSnapshot.nodes.length }) : '';
  dom['tree-overview'].classList.toggle('active', !selectedNodeId);
  dom['tree-nodes'].replaceChildren();
  if (!available) return;
  const children = sceneChildren();
  const query = dom['tree-search']?.value || '', kind = dom['tree-kind']?.value || '';
  if (query.trim() || kind) {
    const matches = treeMatches(query, kind);
    for (const entry of matches.slice(0, 200)) dom['tree-nodes'].append(treeRow(entry, 0, children.get(entry.id) || [], false, true));
    dom['tree-nodes'].append(node('p', 'tree-filter-note', matches.length ? t('找到 {count} 个节点', { count: matches.length }) : t('没有匹配的节点')));
    renderSelection();
    return;
  }
  const stack = children.get(null).map((entry) => ({ entry, depth: 0 })).reverse();
  const visited = new Set();
  while (stack.length) {
    const { entry, depth } = stack.pop();
    if (visited.has(entry.id)) continue;
    visited.add(entry.id);
    const descendants = children.get(entry.id);
    const expanded = expandedNodes.has(entry.id);
    dom['tree-nodes'].append(treeRow(entry, depth, descendants, expanded, false));
    if (expanded) stack.push(...descendants.map((child) => ({ entry: child, depth: depth + 1 })).reverse());
  }
  renderSelection();
}

function renderSelection(note = '') {
  const entry = sceneSnapshot?.nodes.find((candidate) => candidate.id === selectedNodeId);
  const detail = dom['tree-selection'];
  detail.replaceChildren();
  if (!entry) { detail.append(node('p', '', t('选择一个范围，查看其中的场景与用途。'))); return; }
  detail.append(node('strong', '', nodeTitle(entry)));
  const facts = [nodeKindText(entry), Array.isArray(entry.size) ? t('尺寸 {size} 米', { size: entry.size.map(value => Number(value).toFixed(2)).join(' × ') }) : '',
    Number.isFinite(entry.mass) && entry.mass > 0 ? t('质量 {mass} 千克', { mass: entry.mass }) : ''].filter(Boolean);
  if (facts.length) detail.append(node('p', 'tree-facts', facts.join(' · ')));
  if (entry.purpose) detail.append(node('p', '', entry.purpose));
  const description = (value) => typeof value === 'string' ? value : value?.description || value?.text || value?.id || '';
  if (entry.requirements?.length) detail.append(node('p', 'tree-requirements', t('要求：{requirements}', { requirements: entry.requirements.map(description).filter(Boolean).join('；') })));
  if (Array.isArray(entry.completion) && entry.completion.length) detail.append(node('p', 'tree-requirements', t('完成条件：{conditions}', { conditions: entry.completion.map(description).filter(Boolean).join('；') })));
  if (note) detail.append(node('p', 'tree-focus-note', note));
  if (viewer?.cutawayIds?.length) detail.append(node('p', 'tree-focus-note', t('临时剖视：隐藏 {count} 个固定围护结构（{ids}）。回到全景恢复。', { count: viewer.cutawayIds.length, ids: viewer.cutawayIds.join(', ') })));
}

function renderDelivery(current) {
  const delivery = current?.delivery;
  const generation = current?.generation;
  const key = JSON.stringify([activeId, delivery, generation?.status, generation?.requiredOutputs, generation?.waitingSystem]);
  if (key === renderedDeliveryKey) return;
  renderedDeliveryKey = key;
  const accepted = delivery?.accepted === true;
  dom['delivery-status'].textContent = accepted ? t('本次产物已交付') : ['completed', 'complete'].includes(generation?.status) ? t('待交付验收') : generation ? statusLabel(generation.status) : t('等待交付');
  dom['delivery-status'].classList.toggle('is-error', ['failed', 'needs_attention'].includes(generation?.status));
  dom['delivery-products'].replaceChildren();
  if (generation?.waitingSystem) dom['delivery-products'].append(node('p', 'delivery-missing', t('等待处理：{reason}', { reason: generation.waitingSystem.reason })));
  for (const [key, label] of [['generation', t('场景生成轨迹')], ['demonstrations', t('场景与机器人示范')], ['evaluation', t('仿真评测环境')]]) {
    if (!(delivery?.requiredOutputs || generation?.requiredOutputs || ['generation', 'demonstrations', 'evaluation']).includes(key)) continue;
    const result = delivery?.products?.[key];
    const passed = result?.passed === true;
    const card = node('div', `delivery-product${passed ? ' passed' : ''}`);
    card.append(node('span', '', label), node('strong', '', passed ? t('消费验收通过') : accepted ? t('已交付') : t('待消费验收')));
    dom['delivery-products'].append(card);
  }
  if (delivery?.missing?.length) dom['delivery-products'].append(node('p', 'delivery-missing', t('待完成：{items}', { items: delivery.missing.map((item) => typeof item === 'string' ? item : item.message || item.id || '').join('；') })));
}

async function fetchReport(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(t('执行报告读取失败（{status}）', { status: response.status }));
    const result = await response.json();
    if (url !== reportUrl) return;
    reportValue = result;
    renderData();
  } catch (error) {
    if (url !== reportUrl) return;
    reportValue = { error: error.message };
    renderData();
  }
}

function renderData() {
  const current = project();
  const groupDefs = [
    ['scene', t('三维与场景'), [
      ['preview', t('三维场景'), 'GLB'],
      ['scene', t('场景快照'), 'JSON'],
      ['model', t('可执行场景'), 'USD'],
      ['manifest', t('构建清单'), 'JSON'],
    ]],
    ['robot', t('机器人示范与轨迹'), [
      ['video', t('机器人操作视频'), 'MP4'],
      ['report', t('执行报告'), 'JSON'],
      ['trajectory', t('轨迹记录'), 'JSON'],
      ['transitions', t('控制周期记录'), 'JSONL'],
      ['records', t('控制统计'), 'JSON'],
    ]],
  ];
  const groups = groupDefs.map(([id, title, items]) => [id, title, items.map(([key, label, format]) => [artifactUrl(current?.artifacts?.[key]), label, format])]);
  const episodeId = current?.latestEpisode?.episode || current?.latestEpisode?.episodeId;
  const episodeReference = reference => typeof reference === 'string' && reference.startsWith(`projects/${current.id}/episodes/${episodeId}/`);
  for (const camera of current?.latestEpisode?.media?.cameras || []) if (episodeReference(camera.video) && camera.cameraId !== current.latestEpisode.media.primaryCameraId) {
    groups[1][2].push([projectArtifactUrl(camera.video), t('机器人视频 · ') + camera.cameraId, 'MP4']);
  }
  const annotations = current?.latestEpisode?.artifacts?.annotations;
  if (episodeReference(annotations)) groups[1][2].push([projectArtifactUrl(annotations), t('阶段与事件说明（生成解释）'), 'JSON']);

  const deliveryItems = [
    [projectArtifactUrl(current?.delivery?.artifactRef), t('正式交付回执'), 'JSON'],
    [projectArtifactUrl(current?.delivery?.products?.generation?.artifactRef), t('生成轨迹消费报告'), 'JSON'],
    [projectArtifactUrl(current?.delivery?.consumerArtifactRef), t('示范与环境消费报告'), 'JSON'],
  ];
  const exported = current?.delivery?.producedArtifacts;
  deliveryItems.push([projectArtifactUrl(exported?.frozenManifestRef), t('产物文件清单'), 'JSON']);
  for (const [key, title] of [['rawTrace', '公开生成记录'], ['atif', 'ATIF生成轨迹'], ['training', '生成模型训练数据']]) {
    deliveryItems.push([projectArtifactUrl(exported?.products?.generation?.[key]), t(title), key === 'atif' ? 'JSON' : 'JSONL']);
  }
  for (const group of exported?.products?.robot || []) {
    deliveryItems.push([projectArtifactUrl(group.hdf5), t('机器人HDF5数据'), 'HDF5'],
      [projectArtifactUrl(group.mapping), t('机器人数据来源映射'), 'JSON'],
      [projectArtifactUrl(group.lerobot && group.lerobot + '/meta/info.json'), t('LeRobot数据说明'), 'JSON']);
  }
  deliveryItems.push([projectArtifactUrl(exported?.products?.environment?.readme), t('环境运行说明'), 'MD']);
  const priorEnvironments = new Set((current?.environmentRevisions || []).map(row => row.delivery?.artifactRef).filter(Boolean));
  for (const reference of priorEnvironments) if (reference !== current?.delivery?.artifactRef) {
    deliveryItems.push([projectArtifactUrl(reference), t('已交付环境回执'), 'JSON']);
  }
  const scoringSources = new Set(Object.values(current?.latestBuild?.artifacts?.rubrics || {}).flatMap(files => Object.values(files)));
  for (const reference of scoringSources) if (typeof reference === 'string') {
    deliveryItems.push([projectArtifactUrl(reference), t('评分源码 · ') + reference.split('/').at(-1), 'PY']);
  }
  groups.push(['delivery', t('交付回执与源码'), deliveryItems]);

  const availableGroups = groups.map(([id, title, items]) => [id, title, items.filter(([url]) => url)]).filter(([, , items]) => items.length);
  const runs = (current?.jobs || []).filter(job => job.kind === 'rollout' && (!job.result?.projectId || job.result.projectId === current.id))
    .map(job => ({ id: job.id, status: job.status, episode: job.result?.episode || job.result?.episodeId || '',
      revision: job.result?.revision || job.revision || '', taskId: job.result?.taskId || job.result?.score?.taskId || '',
      success: job.result?.success ?? job.result?.score?.success, score: job.result?.score?.score ?? job.result?.score?.reward,
      seconds: job.result?.score?.seconds ?? job.result?.seconds, error: job.error || job.result?.error || '' }));
  const revisions = current?.environmentRevisions || [];
  const key = JSON.stringify([activeId, availableGroups, reportUrl, runs, revisions]);
  if (key === renderedDataKey && renderedDataReportValue === reportValue) return;
  renderedDataKey = key;
  renderedDataReportValue = reportValue;
  const scrollTop = dom['panel-data'].scrollTop;
  const hasContent = availableGroups.length > 0 || runs.length > 0 || revisions.length > 0;
  dom['data-empty'].hidden = hasContent;
  dom['data-content'].hidden = !hasContent;
  dom['data-content'].replaceChildren();
  if (!hasContent) return;

  const appendReport = () => {
    if (!reportUrl) return;
    if (renderedReport?.url === reportUrl && renderedReport.value === reportValue) {
      dom['data-content'].append(renderedReport.node);
      return;
    }
    const report = node('div', 'report');
    renderedReport = { url: reportUrl, value: reportValue, node: report };
    report.append(node('h3', '', t('实际执行结果')));
    if (reportValue === undefined) report.append(node('div', 'report-label', t('正在读取执行报告…')));
    else if (reportValue.error) report.append(node('div', 'is-error', reportValue.error));
    else {
      const labels = { success: t('任务完成'), terminated: t('任务已终止'), truncated: t('运行被截断'), scoreStatus: t('评分是否可用'), executionStatus: t('执行结果类别'), score: t('评分'), reward: t('奖励'), steps: t('执行步数'), duration: t('执行时长'), durationSeconds: t('执行时长（秒）'), task: t('任务'), taskId: t('任务标识'), frames: t('记录帧数'), revision: t('场景版本'), episodeId: t('执行标识'), episode: t('执行标识'), status: t('执行状态'), passed: t('检查通过'), target: t('目标对象'), inGoal: t('进入目标范围'), stable: t('稳定保持'), holdSeconds: t('保持时长（秒）'), robotContact: t('实际接触物体'), contactSteps: t('接触步数'), physicsSteps: t('物理步数'), controlCycles: t('控制周期'), seconds: t('仿真时长（秒）'), execution: t('执行方式'), scoring: t('评分方式'), actual: t('实际位置'), goal: t('目标位置') };
      const rows = analysis.reportRows ? analysis.reportRows(reportValue) : Object.entries(reportValue)
        .filter(([, value]) => value !== null && ['string', 'number', 'boolean'].includes(typeof value))
        .map(([k, value]) => ({ key: k, kind: typeof value, value }));
      for (const entry of rows.slice(0, 24)) {
        const row = node('div', 'report-row');
        const display = entry.kind === 'boolean' ? (entry.value ? t('是') : t('否'))
          : entry.kind === 'number' ? entry.value.toLocaleString(i18n.locale, { maximumSignificantDigits: 6 })
            : ['executionStatus', 'scoreStatus', 'status'].includes(entry.key) ? statusLabel(entry.value) : String(entry.value);
        const valueNode = node('span', `report-value${entry.kind === 'boolean' ? (entry.value ? ' is-pass' : ' is-fail') : ''}`, display);
        row.append(node('span', 'report-label', labels[entry.key] || entry.key), valueNode);
        report.append(row);
      }
      const details = node('details');
      const pre = node('pre');
      const value = reportValue;
      let formatted = false;
      details.ontoggle = () => {
        if (!details.open || formatted) return;
        pre.textContent = JSON.stringify(value, null, 2);
        formatted = true;
      };
      details.append(node('summary', '', t('查看完整报告')), pre);
      report.append(details);
    }
    dom['data-content'].append(report);
  };

  appendReport();

  if (runs.length) {
    const section = node('div', 'report runs-section');
    section.append(node('h3', '', t('全部执行记录（含失败）')));
    const table = node('table', 'runs-table');
    const head = node('tr');
    for (const col of [t('回合'), t('版本'), t('任务'), t('状态'), t('结果'), t('时长')]) head.append(node('th', '', col));
    table.append(head);
    for (const run of runs.slice().reverse()) {
      const tr = node('tr', run.success === false || run.status === 'failed' ? 'is-fail-row' : '');
      const resultText = run.success === true ? t('成功') : run.success === false ? t('失败') : statusLabel(run.status);
      const scoreText = typeof run.score === 'number' ? ` (${run.score.toLocaleString(i18n.locale, { maximumSignificantDigits: 4 })})` : '';
      tr.append(
        node('td', '', run.episode ? run.episode.slice(0, 8) : run.id.slice(0, 8)),
        node('td', '', run.revision ? String(run.revision).slice(0, 8) : '—'),
        node('td', '', run.taskId || '—'),
        node('td', '', statusLabel(run.status)),
        node('td', '', `${resultText}${scoreText}${run.error ? ` · ${run.error}` : ''}`),
        node('td', '', typeof run.seconds === 'number' ? `${run.seconds.toFixed(2)}s` : '—'),
      );
      table.append(tr);
    }
    section.append(table);
    dom['data-content'].append(section);
  }

  if (revisions.length > 1) {
    const revSection = node('div', 'report runs-section');
    revSection.append(node('h3', '', t('环境版本与布局')));
    const pills = node('div', 'revision-list');
    for (const [idx, rev] of revisions.entries()) {
      const split = rev.split ? t({ train: '训练', validation: '验证', test: '测试' }[rev.split]) : t('未分组');
      const pill = node('span', 'revision-pill', `${t('布局')} ${idx + 1} · ${String(rev.revision).slice(0, 8)} · ${split}${rev.delivery?.accepted ? ' · ' + t('已交付') : ''}`);
      pills.append(pill);
    }
    revSection.append(pills);
    dom['data-content'].append(revSection);
  }

  for (const [, title, items] of availableGroups) {
    const groupBox = node('div', 'artifact-group');
    groupBox.append(node('h4', 'artifact-group-title', title));
    const list = node('div', 'artifact-links');
    for (const [url, label, format] of items) {
      const link = node('a', 'artifact-link', label);
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.append(node('span', '', `${format} ↗`));
      list.append(link);
    }
    groupBox.append(list);
    dom['data-content'].append(groupBox);
  }
  dom['panel-data'].scrollTop = scrollTop;
}

function addLiveEvent(projectId, entry) {
  const target = state.projects.find((item) => item.id === projectId);
  if (!target) return;
  target.events ||= [];
  target.events.push(entry);
  target.events = target.events.slice(-200);
  if (projectId === activeId) renderProgress();
}

function handleEvent(event) {
  if (event.kind === 'error') { toast(event.error || t('工作台出现错误'), true); return; }
  if (['state', 'approval', 'job', 'codex'].includes(event.kind)) refreshRequest++;
  if (event.kind === 'state') { clearTimeout(refreshTimer); applyState(event.state); return; }
  if (event.kind === 'approval') {
    approvals.set(String(event.id), event);
    renderApprovals();
    return;
  }
  if (event.kind === 'job') {
    const target = state.projects.find((entry) => entry.id === event.projectId);
    if (target) {
      target.jobs ||= [];
      const index = target.jobs.findIndex((job) => job.id === event.job.id);
      if (index < 0) target.jobs.push(event.job);
      else target.jobs[index] = event.job;
      render();
    }
    scheduleRefresh();
    return;
  }
  if (event.kind !== 'codex') return;
  const { method, params = {}, projectId } = event;
  if (event.child && (method === 'item/agentMessage/delta' || method.startsWith('turn/') || (method === 'item/completed' && params.item?.type === 'agentMessage'))) return;
  if (method === 'item/agentMessage/delta') {
    const stream = streams.get(projectId);
    const itemId = params.itemId || params.item?.id || 'assistant';
    if (stream?.itemId === itemId) stream.text += params.delta || '';
    else {
      const existing = state.projects.find((entry) => entry.id === projectId)?.messages?.find((entry) => entry.id === itemId);
      streams.set(projectId, { itemId, text: (existing ? messageText(existing) : '') + (params.delta || '') });
    }
    if (projectId === activeId) renderChat();
  } else if (method === 'item/started') {
    const item = params.item || {};
    const itemNames = { commandExecution: t('Codex 正在执行命令'), fileChange: t('Codex 正在修改工程文件'), mcpToolCall: t('Codex 正在调用场景工具'), webSearch: t('Codex 正在检索资料') };
    if (itemNames[item.type]) addLiveEvent(projectId, { type: 'codex', method, params, time: new Date().toISOString() });
  } else if (method === 'turn/completed' || method === 'turn/failed' || method === 'error') {
    streams.delete(projectId);
    if (method === 'error') addLiveEvent(projectId, { type: 'error', message: params.error?.message || params.message || t('Codex 执行出现错误'), time: new Date().toISOString() });
    scheduleRefresh();
  } else if (method === 'item/completed' || method === 'turn/started') scheduleRefresh();
}

function connectEvents() {
  const source = new EventSource('/api/events');
  source.onopen = () => setStatus(dom['connection-status'], '实时连接', true, false);
  source.onerror = () => setStatus(dom['connection-status'], '正在重连', false, true);
  source.onmessage = (event) => {
    try { handleEvent(JSON.parse(event.data)); }
    catch (error) { console.error(t('工作台事件读取失败'), error); }
  };
}

async function initializeViewer() {
  try {
    const [THREE, { OrbitControls }, { GLTFLoader }, { DRACOLoader }, { MeshoptDecoder }, { RoomEnvironment }] = await Promise.all([
      import('three'), import('three/addons/controls/OrbitControls.js'), import('three/addons/loaders/GLTFLoader.js'),
      import('three/addons/loaders/DRACOLoader.js'), import('three/addons/libs/meshopt_decoder.module.js'),
      import('three/addons/environments/RoomEnvironment.js'),
    ]);
    const draco = new DRACOLoader();
    draco.setDecoderPath('/vendor/three/examples/jsm/libs/draco/gltf/');
    const loader = new GLTFLoader();
    loader.setDRACOLoader(draco);
    loader.setMeshoptDecoder(MeshoptDecoder);
    const renderer = new THREE.WebGLRenderer({ canvas: dom['scene-canvas'], antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1;
    renderer.localClippingEnabled = true;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.autoUpdate = false;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#f5f3ec');
    scene.fog = new THREE.FogExp2('#f5f3ec', 0.028);
    const environment = new RoomEnvironment();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(environment, .04).texture;
    environment.dispose();
    pmrem.dispose();
    const camera = new THREE.PerspectiveCamera(40, 1, .01, 1000);
    camera.position.set(3.4, 2.35, 3.7);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = .06;
    controls.target.set(0, .55, 0);
    const ambient = new THREE.HemisphereLight(0xfff8ee, 0xc8bca8, .9);
    const key = new THREE.DirectionalLight(0xfff4e2, 2.3);
    key.position.set(4.5, 7.5, 4.2);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.radius = 3.5;
    key.shadow.bias = -.0002;
    const fill = new THREE.DirectionalLight(0xf5ead8, .65);
    fill.position.set(-4, 3, -3);
    scene.add(ambient, key, key.target, fill, fill.target);
    const grid = new THREE.GridHelper(10, 20, 0xd5cfc2, 0xe6e1d6);
    grid.visible = false;
    scene.add(grid);

    // Live Embodied Scene Atelier Stage — native-3D procedural sketch stage for start & fresh conversations.
    const ambientStage = new THREE.Group();
    const stageGrid = new THREE.GridHelper(22, 44, 0xd4cdc0, 0xe8e3d9);
    stageGrid.position.y = -0.004;
    stageGrid.material.transparent = true;
    stageGrid.material.opacity = 0.45;
    ambientStage.add(stageGrid);

    // Multi-tiered architectural travertine plinth.
    const floorPad = new THREE.Mesh(
      new THREE.CylinderGeometry(2.65, 2.75, 0.028, 72),
      new THREE.MeshStandardMaterial({ color: 0xebe6da, roughness: 0.9, metalness: 0.02 })
    );
    floorPad.position.y = -0.014;
    floorPad.receiveShadow = true;
    ambientStage.add(floorPad);

    const innerPad = new THREE.Mesh(
      new THREE.CylinderGeometry(1.78, 1.84, 0.024, 64),
      new THREE.MeshStandardMaterial({ color: 0xe3ddd0, roughness: 0.85, metalness: 0.03 })
    );
    innerPad.position.y = 0.008;
    innerPad.receiveShadow = true;
    ambientStage.add(innerPad);

    // Precision polar optical compass ring + radial degree ticks.
    const compassRing = new THREE.Mesh(
      new THREE.RingGeometry(1.86, 1.885, 80),
      new THREE.MeshBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.36, side: THREE.DoubleSide })
    );
    compassRing.rotation.x = -Math.PI / 2;
    compassRing.position.y = 0.005;
    ambientStage.add(compassRing);

    const tickPts = [];
    for (let i = 0; i < 36; i++) {
      const a = (i / 36) * Math.PI * 2;
      const major = i % 3 === 0;
      const r0 = major ? 1.89 : 1.91;
      const r1 = major ? 2.02 : 1.97;
      tickPts.push(new THREE.Vector3(Math.cos(a) * r0, 0.006, Math.sin(a) * r0), new THREE.Vector3(Math.cos(a) * r1, 0.006, Math.sin(a) * r1));
    }
    ambientStage.add(new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(tickPts),
      new THREE.LineBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.32 })
    ));

    // Subtle floating orbital calibration halo.
    const orbitHalo = new THREE.Mesh(
      new THREE.TorusGeometry(2.38, 0.0045, 8, 96),
      new THREE.MeshBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.22 })
    );
    orbitHalo.rotation.x = Math.PI / 2 - 0.14;
    orbitHalo.position.y = 0.72;
    ambientStage.add(orbitHalo);

    // Subtle origin coordinate axes (1m reference).
    const makeAxis = (to, color) => new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0.022, 0), new THREE.Vector3(...to)]),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.55 })
    );
    ambientStage.add(makeAxis([0.95, 0.022, 0], 0xc96442), makeAxis([0, 0.75, 0], 0x2e7d5b), makeAxis([0, 0.022, 0.95], 0x6a83a6));

    // Shared sculptural studio PBR materials.
    const matOak = new THREE.MeshStandardMaterial({ color: 0xd7c5aa, roughness: 0.72, metalness: 0.04 });
    const matLeg = new THREE.MeshStandardMaterial({ color: 0x45433f, roughness: 0.52, metalness: 0.28 });
    const matTerracotta = new THREE.MeshStandardMaterial({ color: 0xc96442, roughness: 0.45, metalness: 0.06 });
    const matSage = new THREE.MeshStandardMaterial({ color: 0x3b7d62, roughness: 0.62, metalness: 0.05 });
    const matCeramic = new THREE.MeshStandardMaterial({ color: 0xf4f1ea, roughness: 0.34, metalness: 0.12 });
    const matSlate = new THREE.MeshStandardMaterial({ color: 0x6f7c8a, roughness: 0.58, metalness: 0.16 });
    const addBox = (parent, w, h, d, x, y, z, mat) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
      m.position.set(x, y, z); m.castShadow = true; m.receiveShadow = true; parent.add(m); return m;
    };
    const addLegs = (parent, w, d, h) => {
      for (const sx of [-1, 1]) for (const sz of [-1, 1]) {
        const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.018, h, 14), matLeg);
        leg.position.set(sx * (w / 2 - 0.08), h / 2 + 0.02, sz * (d / 2 - 0.08));
        leg.castShadow = true; leg.receiveShadow = true; parent.add(leg);
      }
    };

    // Dynamic Spatial Blueprint Volume (scales live with prompt & preset context).
    const blueprintGroup = new THREE.Group();
    const volumeBox = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(1, 1, 1)),
      new THREE.LineBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.28 })
    );
    volumeBox.position.y = 0.5;
    const floorFootprint = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.045, side: THREE.DoubleSide })
    );
    floorFootprint.rotation.x = -Math.PI / 2;
    floorFootprint.position.y = 0.021;

    const matSurface = new THREE.MeshStandardMaterial({ color: 0xdfd6c5, roughness: 0.78, metalness: 0.03, transparent: true, opacity: 0.0 });
    const workSurface = new THREE.Mesh(new THREE.BoxGeometry(0.92, 0.02, 0.62), matSurface);
    workSurface.position.y = 0.36;

    // Robot base origin ring around the studio robot column.
    const robotRingPts = Array.from({ length: 49 }, (_, i) => {
      const a = (i / 48) * Math.PI * 2;
      return new THREE.Vector3(-0.78 + Math.cos(a) * 0.26, 0.024, Math.sin(a) * 0.26);
    });
    const robotOriginRing = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(robotRingPts),
      new THREE.LineBasicMaterial({ color: 0x2e7d5b, transparent: true, opacity: 0.85 })
    );

    const scanPlane = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.16, side: THREE.DoubleSide })
    );
    scanPlane.rotation.x = -Math.PI / 2;
    scanPlane.visible = false;

    blueprintGroup.add(volumeBox, floorFootprint, workSurface, scanPlane);
    blueprintGroup.scale.set(1.55, 1.05, 1.15);
    ambientStage.add(blueprintGroup, robotOriginRing);

    // Four morphable minimalist architectural vignettes.
    const vignettes = { kitchen: new THREE.Group(), desk: new THREE.Group(), living: new THREE.Group(), warehouse: new THREE.Group() };
    // 1. Kitchen island + ceramic coffee cup + sage tray
    addBox(vignettes.kitchen, 1.18, 0.055, 0.76, 0.14, 0.68, 0, matOak);
    addLegs(vignettes.kitchen, 1.18, 0.76, 0.66);
    vignettes.kitchen.children.slice(1).forEach(leg => { leg.position.x += 0.14; });
    addBox(vignettes.kitchen, 0.34, 0.02, 0.24, 0.38, 0.718, 0.08, matSage);
    const cup = new THREE.Mesh(new THREE.CylinderGeometry(0.044, 0.038, 0.095, 20), matTerracotta);
    cup.position.set(-0.08, 0.755, -0.05); cup.castShadow = true; vignettes.kitchen.add(cup);
    const cupHandle = new THREE.Mesh(new THREE.TorusGeometry(0.026, 0.007, 8, 18, Math.PI), matTerracotta);
    cupHandle.position.set(-0.035, 0.755, -0.05); cupHandle.rotation.z = -Math.PI / 2; vignettes.kitchen.add(cupHandle);
    const carafe = new THREE.Mesh(new THREE.CylinderGeometry(0.036, 0.055, 0.17, 20), matCeramic);
    carafe.position.set(0.08, 0.79, -0.18); carafe.castShadow = true; vignettes.kitchen.add(carafe);

    // 2. Quiet study desk + stacked books + organizer box
    addBox(vignettes.desk, 1.32, 0.045, 0.72, 0.16, 0.68, 0, matOak);
    addLegs(vignettes.desk, 1.32, 0.72, 0.66);
    vignettes.desk.children.slice(1).forEach(leg => { leg.position.x += 0.16; });
    addBox(vignettes.desk, 0.22, 0.03, 0.16, -0.06, 0.718, 0.04, matCeramic);
    addBox(vignettes.desk, 0.20, 0.028, 0.15, -0.04, 0.748, 0.03, matTerracotta);
    addBox(vignettes.desk, 0.18, 0.025, 0.14, -0.05, 0.775, 0.04, matSlate);
    addBox(vignettes.desk, 0.28, 0.11, 0.22, 0.42, 0.758, -0.05, matSage);
    vignettes.desk.visible = false;

    // 3. Living room 3-tier oak shelf + basket + side table
    addBox(vignettes.living, 0.04, 1.34, 0.36, -0.18, 0.69, -0.26, matLeg);
    addBox(vignettes.living, 0.04, 1.34, 0.36, 0.74, 0.69, -0.26, matLeg);
    for (const sy of [0.28, 0.72, 1.16]) addBox(vignettes.living, 0.88, 0.036, 0.34, 0.28, sy, -0.26, matOak);
    addBox(vignettes.living, 0.26, 0.16, 0.22, 0.12, 0.82, -0.26, matTerracotta);
    addBox(vignettes.living, 0.22, 0.14, 0.2, 0.48, 0.37, -0.26, matCeramic);
    const sideTableTop = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.28, 0.035, 32), matOak);
    sideTableTop.position.set(0.32, 0.56, 0.36); sideTableTop.castShadow = true; sideTableTop.receiveShadow = true; vignettes.living.add(sideTableTop);
    const sideTableStem = new THREE.Mesh(new THREE.CylinderGeometry(0.032, 0.055, 0.54, 20), matLeg);
    sideTableStem.position.set(0.32, 0.29, 0.36); sideTableStem.castShadow = true; vignettes.living.add(sideTableStem);
    vignettes.living.visible = false;

    // 4. Modular picking & sorting station + crates
    addBox(vignettes.warehouse, 1.36, 0.05, 0.78, 0.16, 0.62, 0, matSlate);
    addLegs(vignettes.warehouse, 1.36, 0.78, 0.60);
    vignettes.warehouse.children.slice(1).forEach(leg => { leg.position.x += 0.16; });
    addBox(vignettes.warehouse, 0.34, 0.16, 0.26, 0.46, 0.725, -0.14, matSage);
    addBox(vignettes.warehouse, 0.34, 0.16, 0.26, 0.46, 0.725, 0.18, matCeramic);
    addBox(vignettes.warehouse, 0.12, 0.12, 0.12, -0.08, 0.705, 0.06, matTerracotta);
    addBox(vignettes.warehouse, 0.11, 0.11, 0.11, -0.14, 0.70, -0.14, matOak);
    vignettes.warehouse.visible = false;

    ambientStage.add(vignettes.kitchen, vignettes.desk, vignettes.living, vignettes.warehouse);

    // Sculptural procedural embodied robot arm silhouette anchored on a studio column.
    const sketchRobot = new THREE.Group();
    sketchRobot.position.set(-0.78, 0.02, 0);
    const column = new THREE.Mesh(new THREE.CylinderGeometry(0.11, 0.14, 0.62, 24), matLeg);
    column.position.y = 0.31; column.castShadow = true; column.receiveShadow = true; sketchRobot.add(column);
    const baseYaw = new THREE.Group();
    baseYaw.position.y = 0.62; sketchRobot.add(baseYaw);
    const turret = new THREE.Mesh(new THREE.CylinderGeometry(0.085, 0.095, 0.12, 22), matCeramic);
    turret.position.y = 0.06; turret.castShadow = true; baseYaw.add(turret);
    const shoulderPitch = new THREE.Group();
    shoulderPitch.position.y = 0.12; baseYaw.add(shoulderPitch);
    const shoulderJoint = new THREE.Mesh(new THREE.SphereGeometry(0.068, 18, 14), matLeg);
    shoulderJoint.castShadow = true; shoulderPitch.add(shoulderJoint);
    const upperArm = new THREE.Mesh(new THREE.CylinderGeometry(0.044, 0.052, 0.42, 18), matCeramic);
    upperArm.position.y = 0.21; upperArm.castShadow = true; shoulderPitch.add(upperArm);
    const elbowPitch = new THREE.Group();
    elbowPitch.position.y = 0.42; shoulderPitch.add(elbowPitch);
    const elbowJoint = new THREE.Mesh(new THREE.SphereGeometry(0.054, 16, 12), matTerracotta);
    elbowJoint.castShadow = true; elbowPitch.add(elbowJoint);
    const forearm = new THREE.Mesh(new THREE.CylinderGeometry(0.034, 0.042, 0.38, 18), matCeramic);
    forearm.position.y = 0.19; forearm.castShadow = true; elbowPitch.add(forearm);
    const wristPitch = new THREE.Group();
    wristPitch.position.y = 0.38; elbowPitch.add(wristPitch);
    const wristBody = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.045, 0.05), matLeg);
    wristBody.position.y = 0.025; wristBody.castShadow = true; wristPitch.add(wristBody);
    const fingerGeo = new THREE.BoxGeometry(0.014, 0.075, 0.022);
    const leftFinger = new THREE.Mesh(fingerGeo, matCeramic);
    leftFinger.position.set(-0.028, 0.08, 0); leftFinger.castShadow = true; wristPitch.add(leftFinger);
    const rightFinger = new THREE.Mesh(fingerGeo, matCeramic);
    rightFinger.position.set(0.028, 0.08, 0); rightFinger.castShadow = true; wristPitch.add(rightFinger);
    shoulderPitch.rotation.z = -0.34;
    elbowPitch.rotation.z = -1.25;
    wristPitch.rotation.z = -0.78;
    ambientStage.add(sketchRobot);

    // Luminous spatial trajectory arc + gliding target bead.
    const arcCurve = new THREE.QuadraticBezierCurve3(
      new THREE.Vector3(-0.08, 0.86, -0.05),
      new THREE.Vector3(0.15, 1.24, 0.02),
      new THREE.Vector3(0.38, 0.80, 0.08)
    );
    const sketchArc = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(arcCurve.getPoints(36)),
      new THREE.LineBasicMaterial({ color: 0xc96442, transparent: true, opacity: 0.72 })
    );
    const arcBead = new THREE.Mesh(
      new THREE.SphereGeometry(0.022, 14, 10),
      new THREE.MeshStandardMaterial({ color: 0xc96442, emissive: 0xc96442, emissiveIntensity: 0.55, roughness: 0.3 })
    );
    arcBead.position.copy(arcCurve.getPoint(0.2));
    ambientStage.add(sketchArc, arcBead);

    const lampLight = new THREE.PointLight(0xffd2a0, 0.35, 7, 1.5);
    lampLight.position.set(1.6, 2.1, 1.5);
    ambientStage.add(lampLight);

    scene.add(ambientStage);

    const selectionBox = new THREE.Box3Helper(new THREE.Box3(), 0xc96442);
    selectionBox.visible = false;
    scene.add(selectionBox);

    viewer = {
      THREE, renderer, scene, camera, controls, loader, grid, key, fill, ambient,
      ambientStage, stageGrid, floorPad, innerPad, orbitHalo, blueprintGroup, volumeBox, workSurface, scanPlane, lampLight,
      vignettes, activePreset: 'kitchen',
      sketchRobot: { root: sketchRobot, baseYaw, shoulderPitch, elbowPitch, wristPitch, leftFinger, rightFinger },
      sketchArc: { curve: arcCurve, line: sketchArc, bead: arcBead },
      targetScale: new THREE.Vector3(1.55, 1.05, 1.15),
      blueprintInfo: { title: '新建空间工程', scaleLabel: '', busy: false },
      flyingCamera: null, darkSpace: false,
      selectionBox, ambientBounds: new THREE.Box3(new THREE.Vector3(-2.0, 0, -2.0), new THREE.Vector3(2.0, 1.6, 2.0)),
      autoOrbit: false, model: null, home: null,
    };
    dom['toggle-orbit']?.setAttribute('aria-pressed', 'false');
    if (readPreference('sceneagent.night') === '1' || document.body?.classList?.contains?.('theme-night')) applySpatialAtmosphere(true);
    updateLighting(new THREE.Box3(new THREE.Vector3(-2.6, 0, -2.6), new THREE.Vector3(2.6, 1.8, 2.6)));
    buildAmbientNodeLabels();
    syncWelcomeStage(project());
    syncSpatialBlueprint(project(), false);
    controls.addEventListener('start', () => {
      viewer.autoFrame = false;
      viewer.flyingCamera = null;
      if (viewer.autoOrbit) {
        viewer.autoOrbit = false;
        dom['toggle-orbit']?.setAttribute('aria-pressed', 'false');
      }
    });
    controls.addEventListener('change', () => {
      if (!viewer.applyingCamera) viewer.autoFrame = false;
      if (viewer.darkSpace && viewer.model && viewer.scene.fog?.isFog) updateSceneFog('#111110');
      updateSpatialHud();
      requestViewerRender();
    });
    let pointerDownAt = null;
    renderer.domElement.addEventListener('pointerdown', (event) => {
      closeContextMenu();
      pointerDownAt = event.button === 0 ? { x: event.clientX, y: event.clientY } : null;
    });
    renderer.domElement.addEventListener('pointerup', (event) => {
      if (!pointerDownAt || event.button !== 0) { pointerDownAt = null; return; }
      const moved = Math.hypot(event.clientX - pointerDownAt.x, event.clientY - pointerDownAt.y);
      pointerDownAt = null;
      if (moved >= 5 || !viewer?.model) return;
      const picked = pickAt(event);
      if (viewer.measureMode) {
        if (picked?.hit?.point) measureAdd(picked.hit.point);
        return;
      }
      selectNodeFromViewport(picked?.binding?.id || '');
    });
    renderer.domElement.addEventListener('contextmenu', (event) => {
      if (!viewer?.model || !sceneSnapshot) return;
      const picked = pickAt(event);
      if (!picked?.binding?.id) return;
      event.preventDefault();
      openContextMenu(picked.binding.id, event.clientX, event.clientY);
    });
    renderer.domElement.addEventListener('dblclick', focusPreviewAtPointer);
    renderer.domElement.addEventListener('pointermove', updateHoverInspection);
    renderer.domElement.addEventListener('pointerleave', clearHoverInspection);
    if (dom['camera-view']) dom['camera-view'].onchange = () => setCameraView(dom['camera-view'].value);
    if (dom['toggle-cutaway']) dom['toggle-cutaway'].onclick = () => {
      viewer.defaultCutawayPending = false;
      applyCutaway(!viewer.cutawayEnabled, cutawayScope());
      renderSelection();
      requestViewerRender(true);
    };
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { cancelAnimationFrame(viewer.frame); viewer.frame = 0; }
      else requestViewerRender();
    });
    new ResizeObserver(resizeViewer).observe(renderer.domElement);
    resizeViewer();
    updateSpatialHud();
    requestViewerRender(true);
    if (previewUrl) await loadPreview(previewUrl);
  } catch (error) {
    viewerError = t('三维预览暂时不可用：{error}', { error: error.message });
    if (previewUrl) showPreviewError(viewerError);
  }
}

function requestViewerRender(shadows = false) {
  if (!viewer?.renderer) return;
  if (shadows) viewer.renderer.shadowMap.needsUpdate = true;
  if (document.hidden || viewer.frame) return;
  viewer.frame = requestAnimationFrame(() => {
    viewer.frame = 0;
    let blueprintAnimating = false;
    if (viewer.ambientStage?.visible && viewer.blueprintGroup && viewer.targetScale) {
      const diff = viewer.blueprintGroup.scale.distanceTo(viewer.targetScale);
      if (diff > 0.005) {
        viewer.blueprintGroup.scale.lerp(viewer.targetScale, 0.14);
        blueprintAnimating = true;
      } else {
        viewer.blueprintGroup.scale.copy(viewer.targetScale);
      }
      const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      if (!calm) {
        const now = performance.now() * 0.001;
        if (viewer.orbitHalo) viewer.orbitHalo.rotation.z = now * 0.16;
        if (viewer.sketchRobot) {
          const r = viewer.sketchRobot;
          const wave = Math.sin(now * 0.85);
          r.baseYaw.rotation.y = Math.sin(now * 0.48) * 0.24;
          r.shoulderPitch.rotation.z = -0.34 + wave * 0.08;
          r.elbowPitch.rotation.z = -1.25 - wave * 0.11;
          r.wristPitch.rotation.z = -0.78 + wave * 0.07;
          const pinch = 0.028 + Math.sin(now * 1.1) * 0.007;
          r.leftFinger.position.x = -pinch;
          r.rightFinger.position.x = pinch;
        }
        if (viewer.sketchArc?.bead && viewer.sketchArc?.curve) {
          const u = (now * 0.26) % 1;
          viewer.sketchArc.bead.position.copy(viewer.sketchArc.curve.getPoint(u));
          viewer.sketchArc.bead.scale.setScalar(0.85 + Math.sin(u * Math.PI) * 0.35);
        }
        blueprintAnimating = true;
      }
      if (viewer.scanPlane && viewer.blueprintInfo?.busy) {
        viewer.scanPlane.visible = true;
        const phase = (performance.now() * 0.0007) % 1;
        viewer.scanPlane.position.y = phase;
        viewer.scanPlane.material.opacity = 0.08 + Math.sin(phase * Math.PI) * 0.14;
        blueprintAnimating = true;
      } else if (viewer.scanPlane) {
        viewer.scanPlane.visible = false;
      }
    }
    if (viewer.flyingCamera && viewer.THREE) {
      const f = viewer.flyingCamera;
      f.t = Math.min(1, f.t + 0.055);
      const ease = 1 - Math.pow(1 - f.t, 3);
      viewer.camera.position.lerpVectors(f.fromPos, f.toPos, ease);
      viewer.controls.target.lerpVectors(f.fromTarget, f.toTarget, ease);
      if (f.t >= 1) viewer.flyingCamera = null;
    } else if (viewer.autoOrbit && viewer.THREE) {
      const offset = viewer.camera.position.clone().sub(viewer.controls.target);
      const spherical = new viewer.THREE.Spherical().setFromVector3(offset);
      spherical.theta += viewer.ambientStage?.visible ? 0.0012 : 0.003;
      viewer.camera.position.copy(viewer.controls.target).add(offset.setFromSpherical(spherical));
    }
    const moving = viewer.controls.update();
    updateSpatialHud();
    if (viewer.qualityEnabled && viewer.composer) viewer.composer.render(); else viewer.renderer.render(viewer.scene, viewer.camera);
    if (moving || viewer.autoOrbit || viewer.flyingCamera || blueprintAnimating) requestViewerRender();
  });
}

function disposeModel(model) {
  const geometries = new Set(), materials = new Set(), textures = new Set();
  model.traverse((object) => {
    if (object.geometry) geometries.add(object.geometry);
    for (const material of Array.isArray(object.material) ? object.material : object.material ? [object.material] : []) {
      materials.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    }
  });
  for (const geometry of geometries) geometry.dispose();
  for (const material of materials) material.dispose();
  const images = new Set();
  for (const texture of textures) {
    texture.dispose();
    const image = texture.source?.data;
    if (typeof image?.close === 'function' && !images.has(image)) { image.close(); images.add(image); }
  }
}

function removeModel() {
  if (!viewer) return;
  if (viewer.model) { viewer.scene.remove(viewer.model); disposeModel(viewer.model); }
  viewer.originalVisibility?.clear();
  clearSelectionEdges();
  clearReplayOverlay();
  clearCameraRigs();
  viewer.hiddenIds = new Set();
  setViewerTools(false);
  dom['selection-chip'].hidden = true;
  viewer.cutawayIds = [];
  viewer.cutawayEnabled = false;
  viewer.cutawayScope = '';
  viewer.defaultCutawayPending = false;
  viewer.model = null;
  viewer.home = null;
  viewer.frameBounds = null;
  viewer.grid.visible = false;
  if (viewer.ambientStage) {
    viewer.ambientStage.visible = true;
    viewer.scene.fog = new viewer.THREE.FogExp2(viewer.darkSpace ? '#111110' : '#f5f3ec', 0.028);
  }
  lastWelcomeVisual = true;
  if (dom['spatial-node-labels']) dom['spatial-node-labels'].hidden = false;
  if (viewer.selectionBox) viewer.selectionBox.visible = false;
  clearHoverInspection();
  syncCutawayControls();
  updateSpatialHud();
  requestViewerRender(true);
}

function showPreviewError(message) {
  dom['scene-loading'].hidden = true;
  dom['scene-error'].textContent = message;
  dom['scene-error'].hidden = false;
  dom['preview-status'].replaceChildren(node('span', 'status-dot'), uiNode('span', '', '预览加载失败'));
}

function updateLighting(box) {
  const { THREE, key, fill } = viewer;
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, .1) * 1.1;
  key.target.position.copy(center);
  key.position.copy(center).add(new THREE.Vector3(1, 2, 1.4).normalize().multiplyScalar(radius * 3));
  fill.target.position.copy(center);
  fill.position.copy(center).add(new THREE.Vector3(-2, .8, -1).normalize().multiplyScalar(radius * 2));
  const camera = key.shadow.camera;
  camera.left = camera.bottom = -radius;
  camera.right = camera.top = radius;
  camera.near = Math.max(radius / 1000, .001);
  camera.far = radius * 6;
  camera.updateProjectionMatrix();
  key.shadow.normalBias = radius * 2 / key.shadow.mapSize.width * .2;
}

function fitViewerBounds(box, preset = viewer?.viewPreset || 'perspective') {
  if (!viewer || !box || box.isEmpty()) return;
  const { THREE, camera, controls } = viewer;
  const directions = { perspective: [.8, .58, 1], front: [0, 0, 1], top: [0, 1, .001], side: [1, 0, 0] };
  if (!directions[preset]) preset = 'perspective';
  const direction = new THREE.Vector3(...directions[preset]).normalize();
  const up = new THREE.Vector3(0, 1, 0);
  const right = up.clone().cross(direction).normalize();
  const vertical = direction.clone().cross(right).normalize();
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const extent = Math.max(size.x, size.y, size.z, .08);
  const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  let distance = extent * .1;
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    const point = new THREE.Vector3(x, y, z).sub(center);
    distance = Math.max(distance, point.dot(direction) + 1.12 * Math.abs(point.dot(right)) / (tangent * camera.aspect),
      point.dot(direction) + 1.12 * Math.abs(point.dot(vertical)) / tangent);
  }
  viewer.applyingCamera = true;
  const damping = controls.enableDamping;
  try {
    controls.enableDamping = false;
    controls.update();
    camera.up.copy(up);
    camera.position.copy(center).addScaledVector(direction, distance);
    camera.near = Math.max(extent / 1000, .001);
    camera.far = Math.max((viewer.home?.extent || extent) * 100, distance + extent * 3, 100);
    camera.updateProjectionMatrix();
    controls.minDistance = Math.max(extent * .03, camera.near * 2);
    controls.maxDistance = Math.max((viewer.home?.extent || extent) * 30, distance * 4);
    controls.target.copy(center);
    camera.lookAt(center);
    controls.update();
  } finally {
    controls.enableDamping = damping;
    viewer.applyingCamera = false;
  }
  viewer.frameBounds = box.clone();
  viewer.viewPreset = preset;
  viewer.autoFrame = true;
  if (dom['camera-view']) dom['camera-view'].value = preset;
  requestViewerRender();
}

function resizeViewer() {
  if (!viewer?.renderer) return;
  const { renderer, camera } = viewer;
  const width = renderer.domElement.clientWidth, height = renderer.domElement.clientHeight;
  if (!width || !height) return;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height, false);
  viewer.composer?.setSize(width, height);
  viewer.gtaoPass?.setSize(width, height);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  if (viewer.autoFrame && viewer.frameBounds) fitViewerBounds(viewer.frameBounds);
  requestViewerRender();
}

function setCameraView(preset) {
  const bounds = viewer?.frameBounds || viewer?.ambientBounds;
  if (bounds) fitViewerBounds(bounds, preset);
}

function updateSpatialHud() {
  if (!viewer?.camera || !viewer?.controls) return;
  const { camera, controls, THREE } = viewer;
  const dist = camera.position.distanceTo(controls.target);
  const tPos = controls.target;
  if (dom['spatial-telemetry']) {
    let text = `R ${dist.toFixed(1)}m · ${tPos.x.toFixed(1)}, ${tPos.y.toFixed(1)}, ${tPos.z.toFixed(1)}`;
    if (viewer.home?.box && !viewer.home.box.isEmpty() && THREE) {
      const s = viewer.home.box.getSize(new THREE.Vector3());
      text += ` · ${s.x.toFixed(1)}×${s.y.toFixed(1)}×${s.z.toFixed(1)}m`;
    }
    dom['spatial-telemetry'].textContent = text;
  }
  updateAmbientNodeLabels();
  updateMeasureLabel();
  updateCameraLabels();
}

function buildAmbientNodeLabels() {
  const container = dom['spatial-node-labels'];
  if (!container || !viewer?.blueprintGroup) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'spatial-node-pill';
  const dot = document.createElement('span');
  dot.className = 'node-pill-dot';
  const title = uiNode('span', 'node-pill-title', '新建空间工程');
  btn.append(dot, title);
  btn.onclick = (event) => {
    event.stopPropagation();
    if (!activeId) {
      dom['project-dialog']?.showModal?.();
      dom['project-name']?.focus?.();
    } else {
      dom['chat-input']?.focus?.();
    }
  };
  viewer.blueprintLabel = { btn, dot, title };
  container.replaceChildren(btn);
}

const presetConfigs = {
  kitchen: { dims: [1.55, 1.05, 1.15], cam: [2.85, 2.05, 3.15], target: [0, 0.36, 0] },
  desk: { dims: [1.75, 1.12, 1.1], cam: [2.95, 2.1, 3.2], target: [0, 0.36, 0] },
  living: { dims: [2.85, 1.85, 2.35], cam: [3.65, 2.35, 3.95], target: [0, 0.56, 0] },
  warehouse: { dims: [2.15, 1.25, 1.45], cam: [3.2, 2.18, 3.45], target: [0, 0.4, 0] },
};

function isWelcomeStage(current) {
  if (welcomeDraftMode) return true;
  if (!current) return true;
  const hasMessages = Boolean(current.messages?.length || streams.has(current.id));
  const hasPreview = Boolean(artifactUrl(current.artifacts?.preview));
  const hasActiveRun = Boolean(current.activeTurnId || current.jobs?.some(job => activeStatuses.has(job.status)));
  return !hasMessages && !hasPreview && !hasActiveRun;
}

function syncWelcomeRecentProjects() {
  const bar = dom['welcome-recent-bar'];
  const list = dom['welcome-recent-projects'];
  const ret = dom['welcome-return-btn'];
  const projects = state.projects || [];
  const showRecent = isWelcomeStage(project()) && projects.length > 0;
  if (bar) bar.hidden = !showRecent;
  if (ret) ret.hidden = !(welcomeDraftMode && projects.length > 0);
  if (!list) return;
  if (!showRecent) { list.replaceChildren(); return; }
  list.replaceChildren(...projects.slice(0, 3).map((entry) => {
    const btn = node('button', 'welcome-recent-pill');
    btn.type = 'button';
    btn.title = entry.name || t('未命名工程');
    const dot = node('span', `status-dot${entry.status === 'complete' ? ' is-done' : ''}`);
    btn.append(dot, node('span', '', entry.name || t('未命名工程')));
    btn.onclick = () => {
      welcomeDraftMode = false;
      welcomePresetOverride = '';
      selectProject(entry.id);
      render();
    };
    return btn;
  }));
}

function syncWelcomeStage(current) {
  const welcome = isWelcomeStage(current);
  document.body?.classList?.toggle?.('is-welcome-stage', welcome);
  dom['start-new-scene']?.setAttribute('aria-pressed', String(welcomeDraftMode));
  if (dom['scene-empty']) dom['scene-empty'].hidden = !(welcome || (!previewUrl && !viewer?.model));
  if (dom['chat-input'] && chatMode === 'generate') {
    dom['chat-input'].placeholder = t(welcome ? '描述你想构建的三维场景、物体与机器人任务…' : '输入场景或任务要求');
  }
  syncWelcomeRecentProjects();
  if (!viewer) return;
  const showAmbient = welcome || !viewer.model;
  const changed = lastWelcomeVisual !== showAmbient;
  lastWelcomeVisual = showAmbient;
  if (viewer.model) viewer.model.visible = !welcome;
  if (viewer.ambientStage) viewer.ambientStage.visible = showAmbient;
  if (dom['spatial-node-labels']) dom['spatial-node-labels'].hidden = !showAmbient;
  if (viewer.grid) viewer.grid.visible = !showAmbient && dom['toggle-grid']?.getAttribute('aria-pressed') === 'true';
  if (changed) {
    updateSceneFog();
    if (showAmbient && viewer.camera && viewer.controls) {
      const cfg = presetConfigs[viewer.activePreset || 'kitchen'] || presetConfigs.kitchen;
      flyCamera(() => {
        viewer.camera.position.set(...cfg.cam);
        viewer.controls.target.set(...cfg.target);
        viewer.camera.lookAt(viewer.controls.target);
        viewer.controls.update();
      });
    } else if (!showAmbient && viewer.home?.box) {
      flyCamera(() => fitViewerBounds(viewer.home.box));
    }
    requestViewerRender(true);
  }
}

function setWelcomeDraft(enabled) {
  const next = !!enabled;
  if (welcomeDraftMode === next) return;
  welcomeDraftMode = next;
  if (!next) welcomePresetOverride = '';
  else if (spatialMode !== 'default') setSpatialMode('default');
  render();
  if (next) dom['chat-input']?.focus?.();
}

function applyBlueprintPreset(preset, animateCamera = false) {
  if (!viewer?.THREE) return;
  const key = presetConfigs[preset] ? preset : 'kitchen';
  viewer.activePreset = key;
  const cfg = presetConfigs[key];
  if (viewer.targetScale) viewer.targetScale.set(...cfg.dims);
  if (viewer.vignettes) {
    for (const [name, group] of Object.entries(viewer.vignettes)) {
      if (group) group.visible = name === key;
    }
  }
  if (viewer.workSurface) viewer.workSurface.visible = key !== 'living';
  for (const chip of document.querySelectorAll('[data-scene-preset]')) {
    chip.classList?.toggle?.('is-previewing', chip.dataset?.scenePreset === key);
  }
  if (animateCamera && viewer.ambientStage?.visible && viewer.camera && viewer.controls) {
    flyCamera(() => {
      viewer.camera.position.set(...cfg.cam);
      viewer.controls.target.set(...cfg.target);
      viewer.camera.lookAt(viewer.controls.target);
      viewer.controls.update();
    });
  }
  requestViewerRender();
}

function previewBlueprintPreset(preset) {
  welcomePresetOverride = preset || '';
  if (!viewer) return;
  applyBlueprintPreset(preset || 'kitchen', true);
}

function syncSpatialBlueprint(current, isBusy = false) {
  if (!viewer?.blueprintGroup || !viewer?.THREE) return;
  const promptText = [welcomeDraftMode ? '' : current?.name, welcomeDraftMode ? '' : current?.prompt, dom['chat-input']?.value].filter(Boolean).join(' ').toLowerCase();
  const inferred = /仓储|分拣|工位|装箱|料箱|warehouse|bin|sort/.test(promptText) ? 'warehouse'
    : /客厅|卧室|房间|层架|货架|全屋|room|living|bedroom|shelf/.test(promptText) ? 'living'
      : /书桌|文具|书本|办公|desk|study|book/.test(promptText) ? 'desk' : 'kitchen';
  applyBlueprintPreset(welcomePresetOverride || inferred, false);
  const titleText = !current || welcomeDraftMode
    ? t('示意空间 · 拖动自由观察')
    : isBusy
      ? `${current.name || t('未命名工程')} · ${t('正在构建三维空间…')}`
      : `${current.name || t('未命名工程')} · ${t('示意空间，构建后显示真实尺寸')}`;
  viewer.blueprintInfo = { title: titleText, scaleLabel: '', busy: !!isBusy };
  if (viewer.blueprintLabel) {
    viewer.blueprintLabel.title.textContent = titleText;
    viewer.blueprintLabel.btn.classList?.toggle?.('is-busy', !!isBusy);
  }
  requestViewerRender();
}

function updateAmbientNodeLabels() {
  if (!viewer?.ambientStage?.visible || !viewer.blueprintGroup || !viewer.blueprintLabel || !viewer.renderer) return;
  const canvas = viewer.renderer.domElement;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!width || !height) return;
  viewer.camera.updateMatrixWorld();
  const v = new viewer.THREE.Vector3(0, viewer.blueprintGroup.scale.y + 0.14, 0);
  v.project(viewer.camera);
  const el = viewer.blueprintLabel.btn;
  if (!el?.style) return;
  if (v.z > 1 || v.z < -1) {
    el.style.opacity = '0';
    el.style.pointerEvents = 'none';
    return;
  }
  const x = (v.x * 0.5 + 0.5) * width;
  const y = (-v.y * 0.5 + 0.5) * height;
  el.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0) translate(-50%, -100%)`;
  el.style.opacity = '0.95';
  el.style.pointerEvents = 'auto';
}

function updateSceneFog(bg = viewer?.darkSpace ? '#111110' : '#f5f3ec') {
  if (!viewer?.THREE || !viewer?.scene) return;
  let nextFog = null;
  if (viewer.ambientStage?.visible) {
    nextFog = new viewer.THREE.FogExp2(bg, 0.028);
  } else if (viewer.darkSpace) {
    const ext = Math.max(viewer.home?.extent || 2.5, 2.5);
    const camDist = viewer.camera && viewer.controls ? viewer.camera.position.distanceTo(viewer.controls.target) : ext * 2;
    const near = Math.max(camDist + ext * 0.6, ext * 2.5);
    const far = near + Math.max(ext * 2.4, 5.5);
    nextFog = new viewer.THREE.Fog(bg, near, far);
  }
  const prev = viewer.scene.fog;
  const modeChanged = Boolean(prev) !== Boolean(nextFog) || Boolean(prev?.isFogExp2) !== Boolean(nextFog?.isFogExp2);
  viewer.scene.fog = nextFog;
  if (modeChanged && viewer.model) {
    viewer.model.traverse(object => {
      if (!object.isMesh) return;
      for (const material of Array.isArray(object.material) ? object.material : object.material ? [object.material] : []) {
        if (material) material.needsUpdate = true;
      }
    });
  }
}

function applySpatialAtmosphere(dark) {
  const isDark = !!dark;
  document.body?.classList?.toggle?.('theme-night', isDark);
  const themeMeta = document.querySelector?.('meta[name="theme-color"]');
  themeMeta?.setAttribute?.('content', isDark ? '#111110' : '#f5f3ec');
  if (dom['toggle-atmosphere']) dom['toggle-atmosphere'].setAttribute('aria-pressed', String(isDark));
  if (dom['topbar-theme-toggle']) dom['topbar-theme-toggle'].setAttribute('aria-pressed', String(isDark));
  if (dom['topbar-theme-label']) label(dom['topbar-theme-label'], isDark ? '暖夜' : '白昼');
  if (dom['atmosphere-label']) label(dom['atmosphere-label'], isDark ? '暖夜 · 已开启' : '暖夜');
  savePreference('sceneagent.night', isDark ? '1' : '0');
  if (!viewer?.THREE) return;
  viewer.darkSpace = isDark;
  const bg = isDark ? '#111110' : '#f5f3ec';
  viewer.scene.background = new viewer.THREE.Color(bg);
  viewer.scene.environmentIntensity = isDark ? 0.25 : 1;
  updateSceneFog(bg);
  if (viewer.key && viewer.ambient) {
    viewer.key.intensity = isDark ? 0.65 : 2.3;
    viewer.key.color.set(isDark ? '#ffdab3' : '#fff4e2');
    viewer.ambient.intensity = isDark ? 0.5 : 0.9;
    viewer.ambient.color.set(isDark ? '#dcc5ab' : '#fff8ee');
    viewer.ambient.groundColor.set(isDark ? '#1e1c1a' : '#c8bca8');
  }
  if (viewer.fill) viewer.fill.intensity = isDark ? 0.18 : 0.65;
  viewer.floorPad?.material?.color?.set(isDark ? '#1c1b19' : '#ebe6da');
  viewer.innerPad?.material?.color?.set(isDark ? '#24221f' : '#e3ddd0');
  viewer.workSurface?.material?.color?.set(isDark ? '#282623' : '#dfd6c5');
  if (viewer.stageGrid?.material) viewer.stageGrid.material.opacity = isDark ? 0.18 : 0.45;
  if (viewer.orbitHalo?.material) viewer.orbitHalo.material.opacity = isDark ? 0.36 : 0.22;
  if (viewer.lampLight) viewer.lampLight.intensity = isDark ? 2.8 : 0.35;
  if (viewer.matLampShade) viewer.matLampShade.emissiveIntensity = isDark ? 1.1 : 0.25;
  requestViewerRender(true);
}

function toggleSpatialAtmosphere() {
  const currentDark = viewer?.darkSpace !== undefined ? !!viewer.darkSpace : !!document.body?.classList?.contains?.('theme-night');
  applySpatialAtmosphere(!currentDark);
}

function clearHoverInspection() {
  if (viewer) viewer.hoveredNode = null;
  if (dom['spatial-hover-tag']) dom['spatial-hover-tag'].hidden = true;
  if (dom['scene-canvas']?.style) dom['scene-canvas'].style.cursor = '';
}

function updateHoverInspection(event) {
  if (!viewer?.renderer || event.buttons) { clearHoverInspection(); return; }
  const canvas = viewer.renderer.domElement, rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const pointer = new viewer.THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1,
    1 - (event.clientY - rect.top) / rect.height * 2);
  viewer.camera.updateMatrixWorld(true);
  const raycaster = new viewer.THREE.Raycaster();
  raycaster.setFromCamera(pointer, viewer.camera);

  if (!viewer.model || !sceneSnapshot) { clearHoverInspection(); return; }
  const hit = raycaster.intersectObject(viewer.model, true).find(({ object }) => {
    for (let current = object; current; current = current.parent) if (!current.visible) return false;
    return true;
  });
  if (!hit) { clearHoverInspection(); return; }
  const nodes = new Map(sceneSnapshot.nodes.map(entry => [entry.id, entry]));
  const binding = geometryBinding(hit.object, nodes);
  const entry = binding.id ? nodes.get(binding.id) : null;
  if (!entry && !binding.robot) { clearHoverInspection(); return; }
  if (dom['scene-canvas']?.style) dom['scene-canvas'].style.cursor = 'pointer';
  if (dom['spatial-hover-tag']) {
    const labelText = entry ? `${nodeTitle(entry)}${nodeKindText(entry) ? ` · ${nodeKindText(entry)}` : ''}` : t('机器人');
    dom['spatial-hover-tag'].textContent = labelText;
    if (dom['spatial-hover-tag'].style) {
      dom['spatial-hover-tag'].style.left = `${Math.min(rect.width - 140, Math.max(12, event.clientX - rect.left + 14))}px`;
      dom['spatial-hover-tag'].style.top = `${Math.min(rect.height - 40, Math.max(12, event.clientY - rect.top - 12))}px`;
    }
    dom['spatial-hover-tag'].hidden = false;
  }
}

function viewerGeometries(nodes) {
  const geometries = [];
  viewer.model.updateWorldMatrix(true, true);
  viewer.model.traverse(object => {
    if (object.isMesh) geometries.push({ object, binding: geometryBinding(object, nodes), box: new viewer.THREE.Box3().setFromObject(object) });
  });
  return geometries;
}

function importedVisible(object) {
  for (let current = object; current; current = current.parent) {
    if (!(viewer.originalVisibility?.has(current) ? viewer.originalVisibility.get(current) : current.visible)) return false;
  }
  return true;
}

function scopeGeometry(id, nodes, geometries) {
  const subtree = new Set(), pending = [id], children = sceneChildren();
  if (!nodes.has(id)) return { subtree, box: new viewer.THREE.Box3() };
  while (pending.length) {
    const current = pending.pop();
    if (subtree.has(current)) continue;
    subtree.add(current);
    pending.push(...(children.get(current) || []).map(child => child.id));
  }
  const box = new viewer.THREE.Box3();
  for (const geometry of geometries) if (subtree.has(geometry.binding.id) && importedVisible(geometry.object)) box.union(geometry.box);
  return { subtree, box };
}

function spaceAncestors(entry, nodes) {
  const ancestors = new Set(), seen = new Set();
  for (let current = entry; current;) {
    if (seen.has(current.id)) return null;
    seen.add(current.id);
    if (current.kind === 'space') ancestors.add(current.id);
    const owner = current.owner ?? current.parent;
    if (owner == null) break;
    current = nodes.get(owner);
    if (!current) return null;
  }
  return ancestors;
}

function defaultCutawayScope() {
  if (!viewer?.model || !sceneSnapshot) return '';
  const nodes = new Map(sceneSnapshot.nodes.map(entry => [entry.id, entry]));
  const spaces = sceneSnapshot.nodes.filter(entry => entry.kind === 'space');
  const roots = spaces.filter(entry => (entry.owner ?? entry.parent) == null);
  if (roots.length !== 1 || spaces.some(entry => !spaceAncestors(entry, nodes)?.has(roots[0].id))) return '';
  const { box } = scopeGeometry(roots[0].id, nodes, viewerGeometries(nodes));
  return box.isEmpty() ? '' : roots[0].id;
}

function cutawayScope() {
  if (!sceneSnapshot) return '';
  const nodes = new Map(sceneSnapshot.nodes.map(entry => [entry.id, entry]));
  const seen = new Set();
  for (let entry = nodes.get(selectedNodeId); entry && !seen.has(entry.id); entry = nodes.get(entry.owner ?? entry.parent)) {
    seen.add(entry.id);
    if (entry.kind === 'space') return entry.id;
  }
  return defaultCutawayScope();
}

function syncCutawayControls() {
  const enabled = !!viewer?.cutawayEnabled;
  if (dom['toggle-cutaway']) {
    const scope = cutawayScope();
    dom['toggle-cutaway'].disabled = !viewer?.model || !scope;
    dom['toggle-cutaway'].setAttribute('aria-pressed', String(enabled));
    const title = !viewer?.model ? '先加载三维场景' : !scope ? '当前场景没有可剖开的空间围护结构' : enabled ? '恢复完整场景' : '临时剖开所选空间的固定围护结构';
    dom['toggle-cutaway'].setAttribute('data-i18n-title', title);
    dom['toggle-cutaway'].title = t(title);
  }
  if (dom['cutaway-status']) {
    dom['cutaway-status'].hidden = !enabled;
    dom['cutaway-status'].textContent = t('剖视预览');
  }
}

function applyCutaway(enabled, id) {
  if (!viewer?.model) { syncCutawayControls(); return; }
  const nodes = new Map((sceneSnapshot?.nodes || []).map(entry => [entry.id, entry]));
  const entry = nodes.get(id), geometries = viewerGeometries(nodes);
  const { subtree, box } = scopeGeometry(id, nodes, geometries);
  const ancestors = entry?.kind === 'space' ? spaceAncestors(entry, nodes) : null;
  const enclosureBounds = new viewer.THREE.Box3();
  for (const geometry of geometries) {
    const source = nodes.get(geometry.binding.id);
    if (subtree.has(source?.id) && source.role === 'enclosure' && source.mass === 0 && !source.joint
      && importedVisible(geometry.object)) enclosureBounds.union(geometry.box);
  }
  const cutBounds = enclosureBounds.isEmpty() ? box : enclosureBounds;
  const hidden = new Set(), cutHeight = cutBounds.min.y + .75 * (cutBounds.max.y - cutBounds.min.y);
  for (const { object, binding, box: meshBox } of geometries) {
    object.visible = viewer.originalVisibility.get(object);
    const source = nodes.get(binding.id);
    if (!enabled || !ancestors || box.isEmpty() || !importedVisible(object) || source?.role !== 'enclosure'
      || source.mass !== 0 || source.joint || meshBox.max.y <= cutHeight) continue;
    let owner = nodes.get(source.owner ?? source.parent);
    const visited = new Set();
    while (owner && owner.kind !== 'space' && !visited.has(owner.id)) {
      visited.add(owner.id); owner = nodes.get(owner.owner ?? owner.parent);
    }
    const relevant = subtree.has(source.id) || ancestors.has(owner?.id);
    const overlaps = meshBox.max.x >= box.min.x && meshBox.min.x <= box.max.x
      && meshBox.max.z >= box.min.z && meshBox.min.z <= box.max.z;
    if (relevant && overlaps) { object.visible = false; hidden.add(source.id); }
  }
  hideUserNodes();
  viewer.cutawayScope = id || '';
  viewer.cutawayIds = [...hidden];
  viewer.cutawayEnabled = hidden.size > 0;
  syncCutawayControls();
}

function applyDefaultCutaway() {
  if (!viewer?.defaultCutawayPending || !sceneSnapshot) return;
  viewer.defaultCutawayPending = false;
  if (!viewer.autoFrame) { syncCutawayControls(); return; }
  applyCutaway(true, defaultCutawayScope());
}

function focusPreviewAtPointer(event) {
  if (!viewer?.model || !sceneSnapshot) return;
  const canvas = viewer.renderer.domElement, rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const pointer = new viewer.THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1,
    1 - (event.clientY - rect.top) / rect.height * 2);
  viewer.camera.updateMatrixWorld(true);
  viewer.model.updateWorldMatrix(true, true);
  const raycaster = new viewer.THREE.Raycaster();
  raycaster.setFromCamera(pointer, viewer.camera);
  const hit = raycaster.intersectObject(viewer.model, true).find(({ object }) => {
    for (let current = object; current; current = current.parent) if (!current.visible) return false;
    return true;
  });
  if (!hit) return;
  const nodes = new Map(sceneSnapshot.nodes.map(entry => [entry.id, entry]));
  const binding = geometryBinding(hit.object, nodes);
  if (!binding.id) return;
  selectedNodeId = binding.id;
  flyCamera(() => focusSceneNode(binding.id));
  renderSceneTree();
  event.preventDefault();
}

async function loadPreview(url) {
  const request = ++previewRequest;
  removeModel();
  dom['scene-error'].hidden = true;
  dom['scene-empty'].hidden = !!url;
  dom['scene-loading'].hidden = !url;
  dom['orbit-hint'].hidden = true;
  dom['reset-camera'].disabled = true;
  dom['preview-status'].replaceChildren(node('span', 'status-dot'), uiNode('span', '', url ? '正在载入' : '尚无场景'));
  if (!url) { removeModel(); return; }
  if (!viewer) {
    if (viewerError) showPreviewError(viewerError);
    return;
  }
  try {
    const gltf = await viewer.loader.loadAsync(url);
    if (request !== previewRequest) {
      disposeModel(gltf.scene);
      return;
    }
    removeModel();
    const model = gltf.scene;
    const { THREE, scene, grid } = viewer;
    model.updateWorldMatrix(true, true);
    const box = contentBounds(model, new Map((sceneSnapshot?.nodes || []).map(entry => [entry.id, entry])));
    if (box.isEmpty()) { disposeModel(model); throw new Error(t('构建产物中没有可显示的几何对象')); }
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const extent = Math.max(size.x, size.y, size.z, .1);
    scene.add(model);
    viewer.model = model;
    if (welcomeDraftMode) model.visible = false;
    if (viewer.ambientStage) {
      viewer.ambientStage.visible = welcomeDraftMode;
    }
    lastWelcomeVisual = viewer.ambientStage ? viewer.ambientStage.visible : false;
    updateSceneFog();
    if (dom['spatial-node-labels']) dom['spatial-node-labels'].hidden = !welcomeDraftMode;
    if (viewer.selectionBox) viewer.selectionBox.visible = false;
    viewer.originalVisibility = new Map();
    viewer.cutawayIds = [];
    const anisotropy = Math.min(viewer.renderer?.capabilities?.getMaxAnisotropy?.() || 1, 8);
    model.traverse(object => {
      if (!object.isMesh) return;
      viewer.originalVisibility.set(object, object.visible);
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      object.castShadow = !geometryBinding(object, new Map()).ground && materials.every(material => !material.transparent);
      object.receiveShadow = true;
      for (const material of materials) for (const texture of Object.values(material)) if (texture?.isTexture && texture.anisotropy !== anisotropy) {
        texture.anisotropy = anisotropy;
        texture.needsUpdate = true;
      }
    });
    updateLighting(box);
    grid.position.set(center.x, box.min.y + extent * .002, center.z);
    grid.scale.setScalar(Math.max(extent * 1.5, 1) / 10);
    grid.visible = dom['toggle-grid'].getAttribute('aria-pressed') === 'true';
    viewer.home = { target: center.clone(), box: box.clone(), extent };
    viewer.viewPreset = 'perspective';
    viewer.defaultCutawayPending = true;
    resetCamera(false);
    updateSceneFog();
    applyDefaultCutaway();
    dom['scene-loading'].hidden = true;
    dom['orbit-hint'].hidden = false;
    dom['reset-camera'].disabled = false;
    dom['preview-status'].replaceChildren(node('span', 'status-dot'), uiNode('span', '', '真实构建产物'));
    viewer.hiddenIds = new Set();
    setViewerTools(true);
    if (replay.track) buildReplayOverlay(replay.track);
    dom['scene-canvas'].classList.toggle('is-fresh', true);
    setTimeout(() => dom['scene-canvas'].classList.toggle('is-fresh', false), 700).unref?.();
    if (pendingShare) applyShare(pendingShare);
    if (selectedNodeId) focusSceneNode(selectedNodeId);
  } catch (error) { if (request === previewRequest) showPreviewError(t('无法载入场景：{error}', { error: error.message })); }
}

function contentBounds(model, nodes) {
  const { THREE } = viewer;
  const all = new THREE.Box3(), bound = new THREE.Box3();
  model.traverse(object => {
    if (!object.isMesh || !importedVisible(object)) return;
    const binding = geometryBinding(object, nodes);
    if (binding.ground) return;
    const box = new THREE.Box3().setFromObject(object);
    all.union(box);
    if (binding.id || binding.robot) bound.union(box);
  });
  return nodes.size && !bound.isEmpty() ? bound : all;
}

function reframeContent() {
  if (!viewer?.model || !sceneSnapshot) return;
  const { THREE, camera } = viewer;
  const box = contentBounds(viewer.model, new Map(sceneSnapshot.nodes.map(entry => [entry.id, entry])));
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
  const extent = Math.max(size.x, size.y, size.z, .1);
  viewer.home = { ...viewer.home, target: center, box, extent };
  updateLighting(box);
  viewer.grid.position.set(center.x, box.min.y + extent * .002, center.z);
  viewer.grid.scale.setScalar(Math.max(extent * 1.5, 1) / 10);
  if (viewer.autoFrame && !selectedNodeId) { fitViewerBounds(box); viewer.home.position = camera.position.clone(); }
  requestViewerRender(true);
}

function sceneNodes() { return new Map((sceneSnapshot?.nodes || []).map(entry => [entry.id, entry])); }

function subtreeIds(id) {
  const children = sceneChildren(), ids = new Set(), pending = [id];
  while (pending.length) {
    const current = pending.pop();
    if (ids.has(current)) continue;
    ids.add(current);
    pending.push(...(children.get(current) || []).map(child => child.id));
  }
  return ids;
}

function clearSelectionEdges() {
  const group = viewer?.selectionEdges;
  if (!group) return;
  for (const edges of group.children) edges.geometry.dispose();
  group.clear();
}

function syncSelectionEdges() {
  const group = viewer?.selectionEdges;
  if (!group) return;
  for (const edges of group.children) {
    edges.userData.mesh.updateWorldMatrix(true, false);
    edges.matrix.copy(edges.userData.mesh.matrixWorld);
  }
}

function highlightNode(id) {
  if (!viewer?.model) return;
  const { THREE } = viewer;
  if (!viewer.selectionEdges) {
    viewer.selectionEdges = new THREE.Group();
    viewer.selectionEdges.renderOrder = 20;
    viewer.scene.add(viewer.selectionEdges);
  }
  clearSelectionEdges();
  if (!id || !sceneSnapshot) { requestViewerRender(); return; }
  const nodes = sceneNodes(), subtree = subtreeIds(id);
  const meshes = viewerGeometries(nodes).filter(entry => subtree.has(entry.binding.id) && importedVisible(entry.object)).map(entry => entry.object);
  if (meshes.length > 160) { requestViewerRender(); return; }
  viewer.edgeMaterial ||= new THREE.LineBasicMaterial({ color: 0xc96442, transparent: true, opacity: .95, depthTest: false });
  for (const mesh of meshes) {
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 28), viewer.edgeMaterial);
    edges.matrixAutoUpdate = false;
    edges.renderOrder = 20;
    edges.userData.mesh = mesh;
    viewer.selectionEdges.add(edges);
  }
  syncSelectionEdges();
  requestViewerRender();
}

function syncSelectionChip() {
  const chip = dom['selection-chip'], entry = sceneNodes().get(selectedNodeId);
  if (!chip) return;
  chip.hidden = !entry;
  if (!entry) return;
  chip.textContent = `${analysis.nodeTitle ? analysis.nodeTitle(entry) : entry.id} · ${t(analysis.nodeKind?.(entry) || '')} ✕`;
  chip.title = t('取消选择');
}

function selectNodeFromViewport(id) {
  if (!sceneSnapshot) return;
  const nodes = sceneNodes();
  if (id && !nodes.has(id)) return;
  selectedNodeId = id || '';
  const seen = new Set();
  for (let entry = nodes.get(selectedNodeId); entry && !seen.has(entry.id);) {
    seen.add(entry.id);
    const owner = entry.owner ?? entry.parent;
    if (owner == null) break;
    expandedNodes.add(owner);
    entry = nodes.get(owner);
  }
  highlightNode(selectedNodeId);
  renderSceneTree();
  syncSelectionChip();
  dom['tree-nodes']?.querySelector?.('.selected')?.scrollIntoView?.({ block: 'nearest' });
  requestViewerRender();
}

function hideUserNodes() {
  if (!viewer?.model || !viewer.hiddenIds?.size) return;
  const nodes = sceneNodes();
  viewer.model.traverse(object => {
    if (object.isMesh && viewer.hiddenIds.has(geometryBinding(object, nodes).id)) object.visible = false;
  });
}

function refreshVisibility() {
  applyCutaway(!!viewer.cutawayEnabled, viewer.cutawayScope || '');
  highlightNode(selectedNodeId);
  renderSceneTree();
  requestViewerRender(true);
}

function setNodeHidden(id, hidden) {
  if (!viewer?.model || !sceneSnapshot || !sceneNodes().has(id)) return;
  viewer.hiddenIds ||= new Set();
  for (const item of subtreeIds(id)) hidden ? viewer.hiddenIds.add(item) : viewer.hiddenIds.delete(item);
  refreshVisibility();
}

function isolateNode(id) {
  if (!viewer?.model || !sceneSnapshot || !sceneNodes().has(id)) return;
  const keep = subtreeIds(id);
  viewer.hiddenIds = new Set(sceneSnapshot.nodes.map(entry => entry.id).filter(item => !keep.has(item)));
  refreshVisibility();
}

function showAllNodes() {
  if (!viewer?.model) return;
  viewer.hiddenIds = new Set();
  refreshVisibility();
}

function pickAt(event) {
  if (!viewer?.model) return null;
  const canvas = viewer.renderer.domElement, rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const pointer = new viewer.THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2);
  viewer.camera.updateMatrixWorld(true);
  viewer.model.updateWorldMatrix(true, true);
  const raycaster = new viewer.THREE.Raycaster();
  raycaster.setFromCamera(pointer, viewer.camera);
  const clip = viewer.renderer.clippingPlanes || [];
  const hit = raycaster.intersectObject(viewer.model, true).find(({ object, point }) => {
    for (let current = object; current; current = current.parent) if (!current.visible) return false;
    return clip.every(plane => plane.distanceToPoint(point) >= 0);
  });
  if (!hit) return null;
  return { hit, binding: geometryBinding(hit.object, sceneNodes()) };
}

function treeMatches(query = '', kind = '') {
  const text = String(query).trim().toLowerCase();
  return (sceneSnapshot?.nodes || []).filter(entry => {
    if (kind) {
      const category = entry.kind === 'space' ? 'space' : entry.role === 'enclosure' ? 'enclosure'
        : Number(entry.mass) > 0 ? 'movable' : Number.isFinite(entry.mass) ? 'static' : '';
      if (category !== kind) return false;
    }
    return !text || [entry.id, entry.label, entry.name, entry.purpose].some(value => typeof value === 'string' && value.toLowerCase().includes(text));
  });
}

function nodeTitle(entry) { return analysis.nodeTitle ? analysis.nodeTitle(entry) : entry.label || entry.name || entry.id; }
function nodeKindText(entry) { return analysis.nodeKind ? t(analysis.nodeKind(entry)) : entry.kind || ''; }

function measureAdd(point) {
  const { THREE } = viewer;
  const measure = viewer.measure ||= { points: [], group: new THREE.Group(), readout: '', active: false, label: null };
  if (!measure.group.parent) viewer.scene.add(measure.group);
  if (measure.points.length >= 2) measureClear();
  measure.points.push(point.clone());
  const radius = Math.max((viewer.home?.extent || 1) * .008, .004);
  const dot = new THREE.Mesh(new THREE.SphereGeometry(radius, 12, 8), new THREE.MeshBasicMaterial({ color: 0xc96442, depthTest: false }));
  dot.position.copy(point);
  dot.renderOrder = 30;
  measure.group.add(dot);
  if (measure.points.length < 2) return null;
  const [a, b] = measure.points;
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0xc96442, depthTest: false }));
  line.renderOrder = 30;
  measure.group.add(line);
  const distance = a.distanceTo(b);
  measure.readout = `${distance.toFixed(2)} m`;
  if (!measure.label && dom['measure-labels']) {
    measure.label = node('span', 'measure-label');
    dom['measure-labels'].append(measure.label);
  }
  if (measure.label) { measure.label.textContent = measure.readout; measure.label.hidden = false; }
  requestViewerRender();
  return distance;
}

function measureClear() {
  const measure = viewer?.measure;
  if (!measure) return;
  for (const child of [...measure.group.children]) { child.geometry?.dispose(); child.material?.dispose(); measure.group.remove(child); }
  measure.points = [];
  measure.readout = '';
  if (measure.label) measure.label.hidden = true;
  requestViewerRender();
}

function setMeasureMode(on) {
  if (!viewer) return;
  viewer.measureMode = !!on;
  dom['tool-measure']?.setAttribute('aria-pressed', String(!!on));
  if (dom['scene-canvas']?.style) dom['scene-canvas'].style.cursor = on ? 'crosshair' : '';
  if (!on) measureClear();
}

function updateMeasureLabel() {
  const measure = viewer?.measure, canvas = viewer?.renderer?.domElement;
  if (!measure?.label || measure.points.length < 2 || !canvas?.clientWidth) return;
  const middle = measure.points[0].clone().add(measure.points[1]).multiplyScalar(.5).project(viewer.camera);
  const visible = middle.z > -1 && middle.z < 1;
  measure.label.style.opacity = visible ? '1' : '0';
  measure.label.style.transform = `translate3d(${((middle.x * .5 + .5) * canvas.clientWidth).toFixed(1)}px, ${((-middle.y * .5 + .5) * canvas.clientHeight).toFixed(1)}px, 0) translate(-50%, -140%)`;
}

function setSection(fraction) {
  if (!viewer?.renderer) return;
  const box = viewer.home?.box;
  const active = fraction !== null && fraction !== undefined && fraction < 1 && box && !box.isEmpty();
  if (!active) {
    viewer.renderer.clippingPlanes = [];
    viewer.section = null;
  } else {
    const ratio = Math.min(1, Math.max(0, fraction));
    viewer.sectionPlane ||= new viewer.THREE.Plane(new viewer.THREE.Vector3(0, -1, 0), 0);
    viewer.sectionPlane.constant = box.min.y + ratio * (box.max.y - box.min.y);
    viewer.renderer.clippingPlanes = [viewer.sectionPlane];
    viewer.section = { ratio, height: ratio * (box.max.y - box.min.y) };
  }
  dom['tool-section']?.setAttribute('aria-pressed', String(!!viewer.section));
  if (dom['section-readout']) dom['section-readout'].textContent = viewer.section ? `${viewer.section.height.toFixed(2)} m` : '';
  requestViewerRender(true);
}

function setViewerTools(enabled) {
  for (const id of ['tool-measure', 'tool-section', 'tool-quality', 'tool-shot']) if (dom[id]) dom[id].disabled = !enabled;
  if (!enabled) {
    setMeasureMode(false);
    dom['section-popover'].hidden = true;
    setSection(null);
    dom['tool-section']?.setAttribute('aria-pressed', 'false');
  }
}

function shareHash() {
  if (!viewer?.camera) return '';
  return analysis.encodeShare?.({ project: activeId, position: viewer.camera.position.toArray(), target: viewer.controls.target.toArray(),
    node: selectedNodeId, view: viewer.viewPreset || 'perspective' }) || '';
}

function applyShare(share) {
  if (!viewer?.camera || !share || !viewer.model) return;
  if (share.node && sceneSnapshot) {
    if (sceneNodes().has(share.node)) selectNodeFromViewport(share.node);
    delete share.node;
  }
  if (share.position && share.target) {
    viewer.autoFrame = false;
    viewer.flyingCamera = null;
    viewer.camera.position.set(...share.position);
    viewer.controls.target.set(...share.target);
    viewer.camera.lookAt(viewer.controls.target);
    viewer.controls.update();
    if (share.view) { viewer.viewPreset = share.view; if (dom['camera-view']) dom['camera-view'].value = share.view; }
    delete share.position; delete share.target;
    requestViewerRender();
  }
  if (!share.node && !share.position) pendingShare = null;
}

function flyCamera(change) {
  if (!viewer?.camera) { change(); return; }
  const { camera, controls } = viewer;
  const fromPos = camera.position.clone(), fromTarget = controls.target.clone();
  change();
  const toPos = camera.position.clone(), toTarget = controls.target.clone();
  const calm = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (calm || fromPos.distanceTo(toPos) + fromTarget.distanceTo(toTarget) < 1e-4) { requestViewerRender(); return; }
  camera.position.copy(fromPos);
  controls.target.copy(fromTarget);
  viewer.flyingCamera = { fromPos, toPos, fromTarget, toTarget, t: 0 };
  requestViewerRender();
}

function geometryBinding(object, nodes) {
  for (let current = object; current; current = current.parent) {
    if (nodes.has(current.userData?.objectId)) return { id: current.userData.objectId };
    if (current.userData?.robot === true) return { robot: true };
    if (current.userData?.ground === true) return { ground: true };
  }
  return {};
}

function nodeBounds(entry, nodes) {
  if (!entry.bounds) return null;
  const { THREE } = viewer;
  const chain = [];
  const seen = new Set();
  for (let current = entry; current; current = nodes.get(current.frame ?? current.parent)) {
    if (seen.has(current.id)) return null;
    seen.add(current.id);
    chain.push(current);
  }
  const position = new THREE.Vector3();
  const orientation = new THREE.Quaternion();
  for (const current of chain.reverse()) {
    position.add(new THREE.Vector3(...current.at).applyQuaternion(orientation));
    const [w, x, y, z] = current.orientation || [Math.cos((current.yaw || 0) / 2), 0, 0, Math.sin((current.yaw || 0) / 2)];
    orientation.multiply(new THREE.Quaternion(x, y, z, w));
  }
  const box = new THREE.Box3();
  for (const x of [entry.bounds.min[0], entry.bounds.max[0]]) {
    for (const y of [entry.bounds.min[1], entry.bounds.max[1]]) {
      for (const z of [entry.bounds.min[2], entry.bounds.max[2]]) {
        const point = new THREE.Vector3(x, y, z).applyQuaternion(orientation).add(position);
        box.expandByPoint(new THREE.Vector3(point.x, point.z, -point.y));
      }
    }
  }
  return box;
}

function focusSceneNode(id) {
  if (!viewer?.model || !sceneSnapshot) return;
  if (!id) {
    if (viewer.selectionBox) viewer.selectionBox.visible = false;
    highlightNode(''); syncSelectionChip();
    applyDefaultCutaway(); syncCutawayControls(); requestViewerRender(true); return;
  }
  const nodes = new Map(sceneSnapshot.nodes.map((entry) => [entry.id, entry]));
  const entry = nodes.get(id);
  if (!entry) return;
  viewer.defaultCutawayPending = false;
  const { box } = scopeGeometry(id, nodes, viewerGeometries(nodes));
  if (box.isEmpty()) box.copy(nodeBounds(entry, nodes) || box);
  applyCutaway(entry.kind === 'space', entry.kind === 'space' ? id : '');
  if (!box.isEmpty()) {
    fitViewerBounds(box);
    if (viewer.selectionBox) {
      viewer.selectionBox.box.copy(box);
      viewer.selectionBox.visible = true;
    }
  } else if (viewer.selectionBox) {
    viewer.selectionBox.visible = false;
  }
  renderSelection(t('已聚焦此范围，其他场景内容保留显示。'));
  highlightNode(id); syncSelectionChip();
  requestViewerRender(true);
}

function resetCamera(resetSelection = true) {
  if (!viewer?.home) return;
  if (resetSelection) { selectedNodeId = ''; viewer.defaultCutawayPending = false; }
  applyCutaway(false, '');
  fitViewerBounds(viewer.home.box);
  viewer.home.position = viewer.camera.position.clone();
  renderSceneTree();
  requestViewerRender(true);
}

function stopReplay() {
  replay.playing = false;
  if (replay.frame) { cancelAnimationFrame(replay.frame); replay.frame = 0; }
  if (dom['replay-play']) dom['replay-play'].textContent = t('播放');
}

function clearReplayOverlay() {
  const overlay = viewer?.replay;
  if (!overlay) return;
  for (const link of overlay.robotLinks?.values?.() || []) {
    link.object.position.copy(link.basePos);
    link.object.quaternion.copy(link.baseQuat);
    link.object.updateMatrixWorld(true);
  }
  applyReplayTime(0);
  if (overlay.group?.parent) overlay.group.parent.remove(overlay.group);
  overlay.group?.traverse?.(obj => { obj.geometry?.dispose?.(); obj.material?.dispose?.(); });
  viewer.replay = null;
}

function replayLayerState() {
  return {
    path: dom['replay-layer-path']?.checked !== false,
    target: dom['replay-layer-target']?.checked !== false,
    contact: dom['replay-layer-contact']?.checked !== false,
    objects: dom['replay-layer-objects']?.checked !== false,
  };
}

function buildReplayOverlay(track) {
  if (!viewer?.scene || !viewer?.THREE || !track?.times?.length) return;
  clearReplayOverlay();
  const { THREE } = viewer, toThree = analysis.toThree || (([x, y, z]) => [x, z, -y]);
  const group = new THREE.Group();
  const pts = points => points.map(pt => new THREE.Vector3(...toThree(pt)));
  const eeLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts(track.ee)),
    new THREE.LineBasicMaterial({ color: 0xc96442, transparent: true, opacity: .88 }));
  const targetPts = track.target.filter(Boolean);
  const targetLine = new THREE.Line(new THREE.BufferGeometry().setFromPoints(targetPts.length ? pts(targetPts) : [new THREE.Vector3()]),
    new THREE.LineBasicMaterial({ color: 0x2e7d5b, transparent: true, opacity: .85 }));
  targetLine.visible = targetPts.length > 1;
  const r = Math.max((viewer.home?.extent || 1) * .014, .016);
  const marker = new THREE.Mesh(new THREE.SphereGeometry(r, 14, 10), new THREE.MeshBasicMaterial({ color: 0xc96442 }));
  marker.position.set(...toThree(track.ee[0]));
  const contactMarkers = new THREE.Group();
  contactMarkers.visible = false;
  const contactGeo = new THREE.RingGeometry(r * 1.3, r * 2.1, 24);
  contactGeo.rotateX(-Math.PI / 2);
  const contactMat = new THREE.MeshBasicMaterial({ color: 0xc4493c, side: THREE.DoubleSide, transparent: true, opacity: .9, depthTest: false });
  const contactRing = new THREE.Mesh(contactGeo, contactMat);
  contactRing.renderOrder = 25;
  contactMarkers.add(contactRing);
  group.add(eeLine, targetLine, marker, contactMarkers);
  viewer.scene.add(group);

  const boundObjects = new Map();
  const robotLinks = new Map();
  const linkNames = new Set(['panda_link0', 'panda_link1', 'panda_link2', 'panda_link3', 'panda_link4',
    'panda_link5', 'panda_link6', 'panda_link7', 'panda_hand', 'panda_leftfinger', 'panda_rightfinger']);
  if (viewer.model) {
    const nodes = sceneNodes();
    viewer.model.updateWorldMatrix(true, true);
    viewer.model.traverse(object => {
      if (linkNames.has(object.name) && !robotLinks.has(object.name) && object.parent?.name !== 'geometry') {
        robotLinks.set(object.name, { object, basePos: object.position.clone(), baseQuat: object.quaternion.clone() });
      }
      const id = object.userData?.objectId;
      if (!id || !track.objects?.[id] || !nodes.has(id) || boundObjects.has(id)) return;
      const world = new THREE.Vector3();
      object.getWorldPosition(world);
      boundObjects.set(id, { object, baseLocal: object.position.clone(), baseWorld: world });
    });
  }
  viewer.replay = { track, group, eeLine, targetLine, marker, contactMarkers, boundObjects, robotLinks };
  applyReplayTime(replay.time || 0);
  renderReplay();
}

function applyReplayTime(time) {
  replay.time = Math.max(0, Number(time) || 0);
  const track = replay.track || viewer?.replay?.track;
  if (!track) return;
  const clamped = Math.min(replay.time, track.duration || 0);
  replay.time = clamped;
  if (dom['replay-time'] && track.duration > 0) dom['replay-time'].value = String(Math.round(clamped / track.duration * 1000));
  if (dom['replay-readout']) dom['replay-readout'].textContent = `${clamped.toFixed(2)} / ${(track.duration || 0).toFixed(2)} s`;
  const overlay = viewer?.replay;
  if (!overlay) return;
  const toThree = analysis.toThree || (([x, y, z]) => [x, z, -y]);
  const sample = analysis.sample ? analysis.sample(track, clamped)
    : { ee: track.ee[0], objects: {}, contacts: [] };
  const layers = replayLayerState();
  overlay.eeLine.visible = layers.path;
  overlay.marker.visible = layers.path;
  overlay.targetLine.visible = layers.target && track.target?.some(Boolean);
  overlay.marker.position.set(...toThree(sample.ee));

  const { THREE } = viewer;
  const linkPoses = layers.objects && clamped > 0 && sample.joints && analysis.frankaLinkPoses
    ? analysis.frankaLinkPoses(sample.joints) : null;
  for (const [name, link] of overlay.robotLinks || []) {
    const pose = linkPoses?.[name];
    if (pose) {
      link.object.position.set(...pose.p);
      link.object.quaternion.set(...pose.q);
    } else {
      link.object.position.copy(link.basePos);
      link.object.quaternion.copy(link.baseQuat);
    }
    link.object.updateMatrixWorld(true);
  }
  for (const [id, entry] of overlay.boundObjects) {
    const origin = track.objects[id]?.[0], current = sample.objects?.[id];
    if (!layers.objects || !origin || !current || clamped === 0) {
      entry.object.position.copy(entry.baseLocal);
    } else {
      const delta = new THREE.Vector3(...toThree([current[0] - origin[0], current[1] - origin[1], current[2] - origin[2]]));
      if (entry.object.parent) {
        entry.object.parent.updateWorldMatrix(true, false);
        entry.object.position.copy(entry.object.parent.worldToLocal(entry.baseWorld.clone().add(delta)));
      } else {
        entry.object.position.copy(entry.baseLocal).add(delta);
      }
    }
    entry.object.updateMatrixWorld(true);
  }
  const contacts = Array.isArray(sample.contacts) ? sample.contacts : [];
  overlay.contactMarkers.visible = layers.contact && contacts.length > 0;
  if (overlay.contactMarkers.visible) {
    const targetBound = overlay.boundObjects.get(contacts[0]);
    if (targetBound) targetBound.object.getWorldPosition(overlay.contactMarkers.position);
    else overlay.contactMarkers.position.copy(overlay.marker.position);
  }
  syncSelectionEdges();
  requestViewerRender();
}

function stepReplay(now) {
  if (!replay.playing || !replay.track) return;
  const dt = Math.min(.1, Math.max(0, (now - replay.last) / 1000));
  replay.last = now;
  const speed = Number(dom['replay-speed']?.value || 1) || 1;
  let next = replay.time + dt * speed;
  if (next >= replay.track.duration) next = 0;
  applyReplayTime(next);
  if (dom['replay-sync-video']?.checked && dom['robot-video'] && !dom['robot-video'].hidden && dom['robot-video'].readyState >= 1) {
    if (Math.abs(dom['robot-video'].currentTime - next) > .15) dom['robot-video'].currentTime = next;
  }
  replay.frame = requestAnimationFrame(stepReplay);
}

function toggleReplayPlay() {
  if (!replay.track) return;
  if (replay.playing) { stopReplay(); return; }
  replay.playing = true;
  replay.last = performance.now();
  if (dom['replay-play']) dom['replay-play'].textContent = t('暂停');
  replay.frame = requestAnimationFrame(stepReplay);
}

function renderReplay() {
  const hasUrl = !!replay.url, ready = !!replay.track;
  if (dom['replay-empty']) {
    dom['replay-empty'].hidden = ready;
    const h3 = dom['replay-empty'].querySelector?.('h3') || dom['replay-empty'].children?.[0];
    if (h3) h3.textContent = replay.loading ? t('正在读取控制记录…') : replay.error ? replay.error
      : hasUrl ? t('已记录控制轨迹 · 打开本页开始读取') : t('暂无可回放的控制记录');
  }
  if (dom['replay-body']) dom['replay-body'].hidden = !ready;
  if (ready && dom['replay-note']) {
    const current = project();
    const rev = current?.latestEpisode?.revision || current?.latestBuild?.revision || '';
    const linkCount = viewer?.replay?.robotLinks?.size || 0;
    const robotNote = linkCount ? ` · ${t('已驱动机械臂 {count} 个连杆', { count: linkCount })}`
      : replay.track.joints?.some(Boolean) ? ` · ${t('当前版本预览未带连杆绑定，仅回放末端与物体')}` : '';
    dom['replay-note'].textContent = t('来源：真实控制周期记录 · {cycles} 个周期 · 版本 {revision}', {
      cycles: replay.track.cycles, revision: rev ? String(rev).slice(0, 10) : '—' })
      + robotNote
      + (replay.track.skipped ? ` · ${t('跳过 {count} 条不完整记录', { count: replay.track.skipped })}` : '');
  }
}

async function fetchTransitions(url) {
  const request = ++replay.request;
  replay.loading = true; replay.error = ''; renderReplay();
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(t('读取控制记录失败（{status}）', { status: response.status }));
    const text = await response.text();
    if (request !== replay.request) return;
    replay.track = analysis.parseTransitions(text);
    replay.loading = false;
    buildReplayOverlay(replay.track);
    renderReplay();
  } catch (error) {
    if (request !== replay.request) return;
    replay.loading = false;
    replay.error = error.message;
    renderReplay();
  }
}

function ensureReplayLoaded() {
  if (replay.url && !replay.track && !replay.loading) fetchTransitions(replay.url);
}

function clearCameraRigs() {
  const rigs = viewer?.cameraRigs;
  if (!rigs) return;
  if (rigs.group?.parent) rigs.group.parent.remove(rigs.group);
  rigs.group?.traverse?.(obj => { obj.geometry?.dispose?.(); obj.material?.dispose?.(); });
  rigs.labels?.remove?.();
  viewer.cameraRigs = null;
}

function buildCameraRigs(list) {
  clearCameraRigs();
  if (!viewer?.scene || !viewer?.THREE || !list?.length) return;
  const { THREE } = viewer, toThree = analysis.toThree || (([x, y, z]) => [x, z, -y]);
  const group = new THREE.Group();
  group.visible = dom['tool-cameras']?.getAttribute('aria-pressed') === 'true';
  const labels = node('div', 'camera-labels');
  labels.hidden = !group.visible;
  dom['scene-viewport']?.append?.(labels);
  const items = [];
  for (const cam of list) {
    const origin = new THREE.Vector3(...toThree(cam.origin));
    const forward = new THREE.Vector3(...toThree(cam.forward)).normalize();
    const corners = analysis.frustumCorners(cam, .42).map(pt => new THREE.Vector3(...toThree(pt)));
    const pts = [];
    for (const c of corners) pts.push(origin, c);
    for (let i = 0; i < 4; i++) pts.push(corners[i], corners[(i + 1) % 4]);
    const frustum = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0x4b7bb8, transparent: true, opacity: .88 }));
    group.add(frustum);
    const btn = node('button', 'camera-rig-pill', cam.id);
    btn.type = 'button';
    btn.title = t('切换到相机 {id} 的视角', { id: cam.id });
    btn.onclick = (event) => { event.stopPropagation(); activateCameraRig(cam); };
    labels.append(btn);
    items.push({ cam, origin, forward, btn });
  }
  viewer.scene.add(group);
  viewer.cameraRigs = { group, labels, items };
  updateCameraLabels();
  requestViewerRender();
}

function updateCameraLabels() {
  const rigs = viewer?.cameraRigs, canvas = viewer?.renderer?.domElement;
  if (!rigs?.group?.visible || !canvas?.clientWidth) return;
  rigs.labels.hidden = false;
  viewer.camera.updateMatrixWorld();
  for (const item of rigs.items) {
    const p = item.origin.clone().project(viewer.camera);
    const vis = p.z > -1 && p.z < 1;
    item.btn.style.opacity = vis ? '0.95' : '0';
    item.btn.style.pointerEvents = vis ? 'auto' : 'none';
    if (vis) item.btn.style.transform = `translate3d(${((p.x * .5 + .5) * canvas.clientWidth).toFixed(1)}px, ${((-p.y * .5 + .5) * canvas.clientHeight).toFixed(1)}px, 0) translate(-50%, -120%)`;
  }
}

function activateCameraRig(cam) {
  if (!viewer?.camera) return;
  const toThree = analysis.toThree || (([x, y, z]) => [x, z, -y]);
  const origin = new viewer.THREE.Vector3(...toThree(cam.origin));
  const forward = new viewer.THREE.Vector3(...toThree(cam.forward)).normalize();
  flyCamera(() => {
    viewer.autoFrame = false;
    viewer.camera.position.copy(origin);
    viewer.controls.target.copy(origin).addScaledVector(forward, Math.max(1.2, (viewer.home?.extent || 2) * .35));
    viewer.camera.lookAt(viewer.controls.target);
    viewer.controls.update();
  });
}

async function fetchTasksCameras(url) {
  const request = ++cameraRigsRequest;
  try {
    const tasks = await api(url);
    if (request !== cameraRigsRequest || url !== cameraRigsUrl) return;
    cameraRigs = analysis.findCameras ? analysis.findCameras(tasks) : [];
    if (dom['tool-cameras']) {
      dom['tool-cameras'].disabled = !cameraRigs.length;
      dom['tool-cameras'].title = cameraRigs.length ? t('显示任务冻结的相机位置与视野；点击相机名切换到它的视角') : t('当前工程没有冻结的任务相机');
    }
    if (cameraRigs.length) buildCameraRigs(cameraRigs);
  } catch {
    if (request !== cameraRigsRequest) return;
    if (dom['tool-cameras']) { dom['tool-cameras'].disabled = true; dom['tool-cameras'].title = t('读取任务相机失败'); }
  }
}

async function setQualityMode(on) {
  if (!viewer?.renderer) return;
  viewer.qualityEnabled = !!on;
  dom['tool-quality']?.setAttribute('aria-pressed', String(!!on));
  if (on && !viewer.composer) {
    try {
      const [{ EffectComposer }, { RenderPass }, { GTAOPass }, { OutputPass }] = await Promise.all([
        import('three/addons/postprocessing/EffectComposer.js'),
        import('three/addons/postprocessing/RenderPass.js'),
        import('three/addons/postprocessing/GTAOPass.js'),
        import('three/addons/postprocessing/OutputPass.js'),
      ]);
      const canvas = viewer.renderer.domElement;
      const w = canvas.clientWidth || 800, h = canvas.clientHeight || 600;
      const composer = new EffectComposer(viewer.renderer);
      const renderPass = new RenderPass(viewer.scene, viewer.camera);
      const gtaoPass = new GTAOPass(viewer.scene, viewer.camera, w, h);
      gtaoPass.output = GTAOPass.OUTPUT.Default;
      gtaoPass.blendIntensity = .65;
      const outputPass = new OutputPass();
      composer.addPass(renderPass);
      composer.addPass(gtaoPass);
      composer.addPass(outputPass);
      viewer.composer = composer;
      viewer.gtaoPass = gtaoPass;
    } catch (error) {
      viewer.qualityEnabled = false;
      dom['tool-quality']?.setAttribute('aria-pressed', 'false');
      toast(t('无法开启质感增强：{error}', { error: error.message }), true);
      return;
    }
  }
  requestViewerRender(true);
}

function openContextMenu(id, x, y) {
  const menu = dom['context-menu'], entry = sceneNodes().get(id);
  if (!menu || !entry) return;
  contextNodeId = id;
  if (dom['context-title']) dom['context-title'].textContent = nodeTitle(entry);
  menu.hidden = false;
  menu.style.left = `${Math.min(window.innerWidth - 180, Math.max(8, x))}px`;
  menu.style.top = `${Math.min(window.innerHeight - 200, Math.max(8, y))}px`;
}

function closeContextMenu() {
  if (dom['context-menu']) dom['context-menu'].hidden = true;
}

function selectResultTab(name) {
  selectedTab = name;
  for (const button of document.querySelectorAll('[data-tab]')) {
    const active = button.dataset.tab === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    dom[`panel-${button.dataset.tab}`].hidden = !active;
  }
  if (name === 'video' && dom['robot-video'] && videoUrl) dom['robot-video'].preload = 'metadata';
  if (name === 'replay') ensureReplayLoaded();
  if (name !== 'video') { closeVideoPreview(); pauseVideoPreview(); }
}

function tabNavigation(event, selector, select) {
  const buttons = [...document.querySelectorAll(selector)];
  const index = buttons.indexOf(event.target);
  if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
  select(buttons[next]);
  buttons[next].focus();
}

function selectSource(name) {
  sources[activeSource] = dom['source-editor'].value;
  activeSource = name;
  dom['source-editor'].value = sources[name] || '';
  dom['source-editor'].setAttribute('aria-label', t(sourceNames[name]));
  dom['source-editor'].setAttribute('data-i18n-aria-label', sourceNames[name]);
  for (const button of document.querySelectorAll('[data-source]')) {
    const active = button.dataset.source === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  }
}

function currentSourceRequest(request, projectId) {
  return request === sourceRequest && projectId === sourceProjectId && dom['source-dialog'].open;
}

function currentSourceFiles() {
  return { ...sources, [activeSource]: dom['source-editor'].value };
}

function closeSources() {
  sourceRequest++;
  sourceDirty = false;
  dom['source-dialog'].close();
}

async function readSources() {
  if (!dom['source-dialog'].open) return;
  const request = ++sourceRequest, projectId = sourceProjectId;
  const saving = sourceSaves.get(projectId);
  let loaded = false;
  label(dom['source-state'], '正在读取代码…');
  dom['source-editor'].disabled = true;
  dom['save-source'].disabled = true;
  dom['reload-source'].disabled = !!saving;
  try {
    if (saving) {
      await saving.catch(() => {});
      if (!currentSourceRequest(request, projectId)) return;
      dom['reload-source'].disabled = false;
    }
    const result = await api(`/api/projects/${encodeURIComponent(projectId)}/sources`);
    if (!currentSourceRequest(request, projectId)) return;
    sources = result;
    dom['source-editor'].value = sources[activeSource] || '';
    sourceDirty = false;
    loaded = true;
    label(dom['source-state'], '代码保存在工作台主机 · 修改后保存，再构建场景');
  } catch (error) {
    if (!currentSourceRequest(request, projectId)) return;
    label(dom['source-state'], '读取失败：{error}', { error: error.message });
    toast(t('读取工程代码失败：{error}', { error: error.message }), true);
  } finally {
    if (currentSourceRequest(request, projectId)) {
      dom['source-editor'].disabled = !loaded;
      dom['save-source'].disabled = !loaded;
      dom['reload-source'].disabled = false;
    }
  }
}

async function runAction(kind) {
  if (!project()) return;
  const projectId = activeId;
  pendingActions.add(actionKey(kind, projectId));
  render();
  selectResultTab('progress');
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}/${kind}`, { method: 'POST' });
    await refreshState();
  } catch (error) { toast(t('{operation}失败：{error}', { operation: t(kind === 'build' ? '构建场景' : '执行机器人'), error: error.message }), true); }
  finally { pendingActions.delete(actionKey(kind, projectId)); render(); }
}

dom['new-project'].onclick = () => { dom['project-form-error'].hidden = true; dom['project-dialog'].showModal(); dom['project-name'].focus(); };
dom['output-scope'].onchange = () => { rememberDraft(); render(); };
dom['collect-data'].onclick = () => {
  if (dom['collect-data'].disabled) return;
  selectChatMode('generate');
  chatDrafts.set(activeId, { text: dom['chat-input'].value || '', mode: 'generate', outputScope: 'robot' });
  render(); dom['chat-input'].focus();
};
document.querySelectorAll('[data-close-dialog]').forEach((button) => { button.onclick = () => dom['project-dialog'].close(); });
dom['project-form'].onsubmit = async (event) => {
  event.preventDefault();
  const name = dom['project-name'].value.trim();
  const prompt = dom['project-prompt'].value.trim();
  const options = generationOptions(null, dom['project-output'].value || 'complete');
  if (!name || !prompt) return;
  dom['create-project'].disabled = true;
  dom['project-form-error'].hidden = true;
  try {
    const result = await api('/api/projects', { method: 'POST', body: { name, prompt } });
    const projectId = (result.project || result).id;
    welcomeDraftMode = false;
    welcomePresetOverride = '';
    selectProject(projectId);
    dom['project-dialog'].close();
    dom['project-form'].reset();
    if (dom['chat-input'].value.trim() === prompt) dom['chat-input'].value = '';
    await refreshState();
    if (state.codex?.ready) {
      try { await api(`/api/projects/${encodeURIComponent(projectId)}/chat`, { method: 'POST', body: { message: prompt, mode: 'generate', ...options } }); await refreshState(); }
      catch (error) { toast(t('工程已创建，Codex 暂未开始：{error}', { error: error.message }), true); }
    }
  } catch (error) { dom['project-form-error'].textContent = error.message; dom['project-form-error'].hidden = false; }
  finally { dom['create-project'].disabled = false; }
};
dom['project-select'].onchange = () => { welcomeDraftMode = false; welcomePresetOverride = ''; selectProject(dom['project-select'].value); render(); };
dom['chat-input'].oninput = rememberDraft;
dom['chat-form'].onsubmit = async (event) => {
  event.preventDefault();
  const message = dom['chat-input'].value.trim();
  if (!message || dom['send-message'].disabled) return;
  if (!project() || welcomeDraftMode) {
    dom['project-prompt'].value = message;
    dom['project-name'].value = message.replace(/\s+/g, ' ').slice(0, 18).replace(/[，。,.；;：:]+$/, '') || '';
    if (dom['project-output']) dom['project-output'].value = dom['output-scope'].value === 'scene' ? 'scene' : 'complete';
    dom['project-dialog'].showModal();
    dom['project-name'].focus();
    dom['project-name'].select?.();
    return;
  }
  const projectId = activeId, mode = chatMode;
  let options;
  try { options = mode === 'generate' ? generationOptions(project(), dom['output-scope'].value || 'complete') : {}; }
  catch (error) { toast(error.message, true); return; }
  pendingActions.add(actionKey('chat', projectId));
  render();
  try {
    await api(`/api/projects/${encodeURIComponent(projectId)}/chat`, { method: 'POST', body: { message, mode, ...options } });
    if (chatDrafts.get(projectId)?.text.trim() === message) chatDrafts.set(projectId, { ...chatDrafts.get(projectId), text: '', mode });
    if (activeId === projectId && dom['chat-input'].value.trim() === message) { dom['chat-input'].value = ''; rememberDraft(); }
    await refreshState();
  } catch (error) { toast(t('发送消息失败：{error}', { error: error.message }), true); }
  finally { pendingActions.delete(actionKey('chat', projectId)); render(); }
};
dom['chat-input'].onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); dom['chat-form'].requestSubmit(); } };
dom.interrupt.onclick = async () => {
  const projectId = activeId;
  if (!projectId || actionPending('interrupt', projectId)) return;
  pendingActions.add(actionKey('interrupt', projectId)); render();
  try { await api(`/api/projects/${encodeURIComponent(projectId)}/interrupt`, { method: 'POST' }); streams.delete(projectId); await refreshState(); }
  catch (error) { toast(t('停止任务失败：{error}', { error: error.message }), true); }
  finally { pendingActions.delete(actionKey('interrupt', projectId)); render(); }
};
dom['resume-generation'].onclick = async () => {
  const current = project();
  if (!current?.generation?.waitingSystem || dom['resume-generation'].disabled) return;
  pendingActions.add(actionKey('resume', current.id)); render();
  try {
    await api(`/api/projects/${encodeURIComponent(current.id)}/resume`, { method: 'POST', body: { generationId: current.generation.id } });
    await refreshState();
  } catch (error) { toast(t('暂时不能继续：{error}', { error: error.message }), true); }
  finally { pendingActions.delete(actionKey('resume', current.id)); render(); }
};
dom['build-scene'].onclick = () => runAction('build');
dom['run-robot'].onclick = () => runAction('rollout');
dom['reset-camera'].onclick = resetCamera;
dom['tree-overview'].onclick = resetCamera;
dom['toggle-tree'].onclick = () => {
  const visible = dom['toggle-tree'].getAttribute('aria-pressed') !== 'true';
  dom['toggle-tree'].setAttribute('aria-pressed', String(visible));
  renderSceneTree();
};
document.querySelectorAll('[data-mode]').forEach((button) => {
  button.onclick = () => {
    selectChatMode(button.dataset.mode);
    rememberDraft();
    render();
  };
});
dom['toggle-grid'].onclick = () => {
  const visible = dom['toggle-grid'].getAttribute('aria-pressed') !== 'true';
  dom['toggle-grid'].setAttribute('aria-pressed', String(visible));
  if (viewer) viewer.grid.visible = visible && !!viewer.model;
  requestViewerRender();
};
dom.fullscreen.onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await dom['scene-viewport'].requestFullscreen();
  } catch (error) { toast(t('无法开启全屏：{error}', { error: error.message }), true); }
};
dom['scene-canvas'].onkeydown = (event) => {
  if (event.key.toLowerCase() === 'r' || event.key === 'Home') { event.preventDefault(); resetCamera(); return; }
  if (!viewer?.model || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-'].includes(event.key)) return;
  event.preventDefault();
  const offset = viewer.camera.position.clone().sub(viewer.controls.target);
  const spherical = new viewer.THREE.Spherical().setFromVector3(offset);
  if (event.key === 'ArrowLeft') spherical.theta -= .1;
  if (event.key === 'ArrowRight') spherical.theta += .1;
  if (event.key === 'ArrowUp') spherical.phi -= .1;
  if (event.key === 'ArrowDown') spherical.phi += .1;
  if (event.key === '+' || event.key === '=') spherical.radius *= .9;
  if (event.key === '-') spherical.radius *= 1.1;
  spherical.phi = Math.max(.05, Math.min(Math.PI - .05, spherical.phi));
  spherical.radius = Math.max(viewer.controls.minDistance, Math.min(viewer.controls.maxDistance, spherical.radius));
  viewer.camera.position.copy(viewer.controls.target).add(offset.setFromSpherical(spherical));
  viewer.controls.update();
};
document.querySelectorAll('[data-tab]').forEach((button) => {
  button.onclick = () => {
    selectResultTab(button.dataset.tab);
    if (dom['results-panel']?.classList?.contains?.('is-collapsed')) {
      dom['results-panel'].classList.remove('is-collapsed');
      dom['toggle-results']?.setAttribute('aria-expanded', 'true');
    }
  };
});
document.querySelector('.result-tabs').onkeydown = (event) => tabNavigation(event, '[data-tab]', (button) => selectResultTab(button.dataset.tab));
dom['expand-video'].onclick = openVideoPreview;
dom['close-video'].onclick = closeVideoPreview;
dom['video-dialog'].addEventListener('cancel', (event) => { event.preventDefault(); closeVideoPreview(); });
dom['video-dialog'].addEventListener('close', () => {
  if (dom['video-dialog'].open) return;
  pauseVideoPreview();
  dom['panel-video'].append(dom['video-content']);
  dom['expand-video'].setAttribute('aria-expanded', 'false');
  if (selectedTab === 'video' && !dom['expand-video'].hidden) dom['expand-video'].focus();
});
dom['video-camera'].onchange = () => renderVideo(project());
dom['video-episode'].onchange = () => renderVideo(project());
dom['robot-video'].onplay = () => {
  const camera = selectedVideoEpisode(project())?.media?.cameras.find(row => row.cameraId === dom['video-camera'].value);
  if (!camera) return;
  const note = camera.status === 'partial' ? t('此视角仅保存了部分记录。') : '';
  dom['video-availability'].textContent = note;
  dom['video-availability'].hidden = !note;
};
dom['robot-video'].onerror = () => { if (videoUrl) toast(t('机器人视频暂时无法播放，请检查远端视频是否已完整生成。'), true); };
dom['edit-source'].onclick = () => { sourceProjectId = activeId; sourceDirty = false; activeSource = 'scene'; sources = { scene: '', tasks: '', expert: '', rubrics: '' }; dom['source-editor'].value = ''; selectSource('scene'); dom['source-dialog'].showModal(); readSources(); };
dom['close-source'].onclick = () => { if (!sourceDirty || window.confirm(t('代码还没有保存，确定关闭吗？'))) closeSources(); };
dom['source-dialog'].addEventListener('cancel', (event) => { event.preventDefault(); if (!sourceDirty || window.confirm(t('代码还没有保存，确定关闭吗？'))) closeSources(); });
document.querySelectorAll('[data-source]').forEach((button) => { button.onclick = () => selectSource(button.dataset.source); });
document.querySelector('.source-tabs').onkeydown = (event) => tabNavigation(event, '[data-source]', (button) => selectSource(button.dataset.source));
dom['source-editor'].oninput = () => { sourceDirty = true; label(dom['source-state'], '有未保存的修改'); };
dom['source-editor'].onkeydown = (event) => {
  if (event.key === 'Tab') {
    event.preventDefault();
    const editor = dom['source-editor'];
    const start = editor.selectionStart;
    editor.setRangeText('    ', start, editor.selectionEnd, 'end');
    editor.dispatchEvent(new Event('input'));
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); dom['save-source'].click(); }
};
dom['reload-source'].onclick = () => { if (!sourceSaves.has(sourceProjectId) && (!sourceDirty || window.confirm(t('重新读取会丢弃未保存的修改，继续吗？')))) readSources(); };
dom['save-source'].onclick = async () => {
  if (sourceSaves.has(sourceProjectId) || dom['save-source'].disabled || !dom['source-dialog'].open) return;
  const request = sourceRequest, projectId = sourceProjectId, submitted = currentSourceFiles();
  dom['save-source'].disabled = true;
  dom['reload-source'].disabled = true;
  const saving = api(`/api/projects/${encodeURIComponent(projectId)}/sources`, { method: 'PUT', body: submitted }).finally(() => {
    if (sourceSaves.get(projectId) === saving) sourceSaves.delete(projectId);
  });
  sourceSaves.set(projectId, saving);
  try {
    await saving;
    if (!currentSourceRequest(request, projectId)) return;
    const current = currentSourceFiles();
    sourceDirty = Object.keys(sourceNames).some(key => current[key] !== submitted[key]);
    label(dom['source-state'], sourceDirty ? '有未保存的修改' : '已保存到工作台 · 构建后更新三维场景');
    if (!sourceDirty) toast(t('工程代码已保存'));
  } catch (error) {
    if (!currentSourceRequest(request, projectId)) return;
    label(dom['source-state'], '保存失败：{error}', { error: error.message });
    toast(t('保存工程代码失败：{error}', { error: error.message }), true);
  } finally {
    if (currentSourceRequest(request, projectId)) {
      dom['save-source'].disabled = false;
      dom['reload-source'].disabled = false;
    }
  }
};
window.addEventListener('beforeunload', (event) => { if (sourceDirty) { event.preventDefault(); event.returnValue = ''; } });
dom['ui-language'].onchange = () => changeLanguage(dom['ui-language'].value);
if (dom['toggle-orbit']) dom['toggle-orbit'].onclick = () => {
  if (!viewer) return;
  viewer.autoOrbit = !viewer.autoOrbit;
  dom['toggle-orbit'].setAttribute('aria-pressed', String(viewer.autoOrbit));
  requestViewerRender();
};
document.querySelectorAll('[data-quick-prompt]').forEach((button) => {
  const preset = button.dataset?.scenePreset || '';
  if (preset) {
    button.onpointerenter = () => previewBlueprintPreset(preset);
    button.onfocus = () => previewBlueprintPreset(preset);
  }
  button.onclick = () => {
    const prompt = button.dataset.quickPrompt || '';
    if (preset) previewBlueprintPreset(preset);
    dom['chat-input'].value = prompt;
    if (!welcomeDraftMode && activeId) rememberDraft();
    syncSpatialBlueprint(project(), false);
    dom['chat-input'].focus?.();
  };
});
dom['chat-input']?.addEventListener?.('input', () => {
  welcomePresetOverride = '';
  syncSpatialBlueprint(project(), false);
});
if (dom['start-new-scene']) dom['start-new-scene'].onclick = () => setWelcomeDraft(!welcomeDraftMode);
if (dom['brand-home']) dom['brand-home'].onclick = (event) => {
  event.preventDefault();
  if (state.projects?.length) setWelcomeDraft(!welcomeDraftMode);
  else dom['chat-input']?.focus?.();
};
if (dom['welcome-return-btn']) dom['welcome-return-btn'].onclick = () => setWelcomeDraft(false);
if (dom['toggle-results']) dom['toggle-results'].onclick = () => {
  const panel = dom['results-panel'];
  if (!panel) return;
  const collapsed = panel.classList.toggle('is-collapsed');
  dom['toggle-results'].setAttribute('aria-expanded', String(!collapsed));
  setTimeout(() => resizeViewer(), 220);
};
if (dom['toggle-atmosphere']) dom['toggle-atmosphere'].onclick = toggleSpatialAtmosphere;
if (dom['topbar-theme-toggle']) dom['topbar-theme-toggle'].onclick = toggleSpatialAtmosphere;
if (readPreference('sceneagent.night') === '1') applySpatialAtmosphere(true);
function setSpatialMode(mode) {
  if (mode !== 'immersive' && spatialMode !== 'immersive') previousSpatialMode = mode;
  spatialMode = mode;
  document.body?.classList?.toggle?.('is-immersive-3d', mode === 'immersive');
  document.body?.classList?.toggle?.('chat-wing-collapsed', mode === 'wing-hidden');
  dom['toggle-chat-wing']?.setAttribute('aria-pressed', String(mode === 'default'));
  dom['toggle-immersive']?.setAttribute('aria-pressed', String(mode === 'immersive'));
  if (dom['expand-chat-wing']) dom['expand-chat-wing'].hidden = mode !== 'wing-hidden';
  if (dom['exit-immersive']) dom['exit-immersive'].hidden = mode !== 'immersive';
  setTimeout(() => resizeViewer(), 260).unref?.();
}
function setMobilePane(pane) {
  for (const name of ['chat', 'scene', 'results']) document.body?.classList?.toggle?.(`pane-${name}`, name === pane);
  for (const button of document.querySelectorAll('[data-pane]')) button.setAttribute('aria-selected', String(button.dataset.pane === pane));
  if (pane === 'results') {
    dom['results-panel']?.classList?.toggle?.('is-collapsed', false);
    dom['toggle-results']?.setAttribute('aria-expanded', 'true');
  }
}
if (dom['toggle-chat-wing']) dom['toggle-chat-wing'].onclick = () => setSpatialMode(spatialMode === 'default' ? 'wing-hidden' : 'default');
if (dom['toggle-immersive']) dom['toggle-immersive'].onclick = () => setSpatialMode(spatialMode === 'immersive' ? previousSpatialMode : 'immersive');
if (dom['collapse-chat-wing']) dom['collapse-chat-wing'].onclick = () => setSpatialMode('wing-hidden');
if (dom['expand-chat-wing']) dom['expand-chat-wing'].onclick = () => setSpatialMode('default');
if (dom['exit-immersive']) dom['exit-immersive'].onclick = () => setSpatialMode(previousSpatialMode);
document.querySelectorAll('[data-pane]').forEach(button => { button.onclick = () => setMobilePane(button.dataset.pane); });
setMobilePane('scene');

if (dom['selection-chip']) dom['selection-chip'].onclick = () => selectNodeFromViewport('');
if (dom['tree-search']) dom['tree-search'].oninput = () => renderSceneTree();
if (dom['tree-kind']) dom['tree-kind'].onchange = () => renderSceneTree();
if (dom['toggle-results-size']) dom['toggle-results-size'].onclick = () => {
  const tall = dom['results-panel']?.classList?.toggle?.('is-tall');
  dom['toggle-results-size'].setAttribute('aria-pressed', String(!!tall));
  if (tall && dom['results-panel']?.classList?.contains?.('is-collapsed')) {
    dom['results-panel'].classList.remove('is-collapsed');
    dom['toggle-results']?.setAttribute('aria-expanded', 'true');
  }
  setTimeout(() => resizeViewer(), 220);
};
if (dom['tool-measure']) dom['tool-measure'].onclick = () => setMeasureMode(!viewer?.measureMode);
if (dom['tool-section']) dom['tool-section'].onclick = () => {
  const pop = dom['section-popover'];
  if (!pop) return;
  pop.hidden = !pop.hidden;
  if (!pop.hidden && !viewer?.section && dom['section-height']) dom['section-height'].value = '800';
  if (!pop.hidden && dom['section-height']) setSection(Number(dom['section-height'].value) / 1000);
};
if (dom['section-height']) dom['section-height'].oninput = () => setSection(Number(dom['section-height'].value) / 1000);
if (dom['section-clear']) dom['section-clear'].onclick = () => {
  if (dom['section-height']) dom['section-height'].value = '1000';
  setSection(null);
  if (dom['section-popover']) dom['section-popover'].hidden = true;
};
if (dom['tool-cameras']) dom['tool-cameras'].onclick = () => {
  const next = dom['tool-cameras'].getAttribute('aria-pressed') !== 'true';
  dom['tool-cameras'].setAttribute('aria-pressed', String(next));
  if (viewer?.cameraRigs) {
    viewer.cameraRigs.group.visible = next;
    viewer.cameraRigs.labels.hidden = !next;
    updateCameraLabels();
    requestViewerRender();
  }
};
if (dom['tool-quality']) dom['tool-quality'].onclick = () => setQualityMode(!viewer?.qualityEnabled);
if (dom['tool-shot']) dom['tool-shot'].onclick = () => {
  if (!viewer?.renderer || !viewer?.model) return;
  if (viewer.qualityEnabled && viewer.composer) viewer.composer.render();
  else viewer.renderer.render(viewer.scene, viewer.camera);
  const link = node('a');
  link.download = `${(project()?.name || 'scene').replace(/[^\w\u4e00-\u9fa5-]+/g, '_')}.png`;
  link.href = viewer.renderer.domElement.toDataURL('image/png');
  link.click();
};
if (dom['tool-share']) dom['tool-share'].onclick = async () => {
  const hash = shareHash();
  if (hash) window.location.hash = hash;
  try {
    await navigator.clipboard?.writeText?.(window.location.href);
    toast(t('已复制视角链接'));
  } catch {
    toast(t('地址栏已更新为当前视角链接'));
  }
};
if (dom['tool-help']) dom['tool-help'].onclick = () => dom['help-dialog']?.showModal?.();
if (dom['help-close']) dom['help-close'].onclick = () => dom['help-dialog']?.close?.();
if (dom['ctx-focus']) dom['ctx-focus'].onclick = () => {
  closeContextMenu();
  if (!contextNodeId) return;
  selectedNodeId = contextNodeId; renderSceneTree(); flyCamera(() => focusSceneNode(contextNodeId));
};
if (dom['ctx-hide']) dom['ctx-hide'].onclick = () => { closeContextMenu(); if (contextNodeId) setNodeHidden(contextNodeId, true); };
if (dom['ctx-isolate']) dom['ctx-isolate'].onclick = () => { closeContextMenu(); if (contextNodeId) isolateNode(contextNodeId); };
if (dom['ctx-show-all']) dom['ctx-show-all'].onclick = () => { closeContextMenu(); showAllNodes(); };
if (dom['ctx-copy']) dom['ctx-copy'].onclick = async () => {
  closeContextMenu();
  if (!contextNodeId) return;
  try { await navigator.clipboard?.writeText?.(contextNodeId); toast(t('已复制节点标识：{id}', { id: contextNodeId })); }
  catch { toast(contextNodeId); }
};
if (dom['replay-play']) dom['replay-play'].onclick = toggleReplayPlay;
if (dom['replay-time']) dom['replay-time'].oninput = () => {
  stopReplay();
  if (replay.track) applyReplayTime((Number(dom['replay-time'].value) / 1000) * replay.track.duration);
};
for (const id of ['replay-layer-path', 'replay-layer-target', 'replay-layer-contact', 'replay-layer-objects']) {
  if (dom[id]) dom[id].onchange = () => applyReplayTime(replay.time);
}
dom['robot-video']?.addEventListener?.('timeupdate', () => {
  if (dom['replay-sync-video']?.checked && !replay.playing && replay.track) applyReplayTime(dom['robot-video'].currentTime);
});
document.addEventListener?.('click', (event) => {
  if (dom['context-menu'] && !dom['context-menu'].hidden && !dom['context-menu'].contains?.(event.target)) closeContextMenu();
});
document.addEventListener?.('keydown', (event) => {
  const tag = event.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target?.isContentEditable) return;
  if (document.querySelector('dialog[open]')) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  if (event.key === '?' || (event.key === '/' && event.shiftKey)) { event.preventDefault(); dom['help-dialog']?.showModal?.(); }
  else if (event.key === 'Escape') {
    const hadMenu = dom['context-menu'] && !dom['context-menu'].hidden;
    const hadSection = dom['section-popover'] && !dom['section-popover'].hidden;
    closeContextMenu();
    if (dom['section-popover']) dom['section-popover'].hidden = true;
    if (hadMenu || hadSection) return;
    if (viewer?.measureMode) setMeasureMode(false);
    else if (selectedNodeId) selectNodeFromViewport('');
    else if (spatialMode === 'immersive') setSpatialMode(previousSpatialMode);
  } else if (['1', '2', '3', '4'].includes(key)) {
    const view = ['perspective', 'front', 'side', 'top'][Number(key) - 1];
    flyCamera(() => setCameraView(view));
  } else if (key === 'r') { flyCamera(() => resetCamera()); }
  else if (key === 'f' && selectedNodeId) { flyCamera(() => focusSceneNode(selectedNodeId)); }
  else if (key === 'h') {
    if (selectedNodeId) setNodeHidden(selectedNodeId, !viewer?.hiddenIds?.has(selectedNodeId));
    else if (viewer?.hiddenIds?.size) showAllNodes();
  } else if (key === 'm' && !dom['tool-measure']?.disabled) { setMeasureMode(!viewer?.measureMode); }
  else if (key === 'c' && !dom['toggle-cutaway']?.disabled) { dom['toggle-cutaway'].click(); }
  else if (key === 'g') { dom['toggle-grid']?.click(); }
  else if (key === 'o') { dom['toggle-orbit']?.click(); }
  else if (key === '[') { setSpatialMode(spatialMode === 'default' ? 'wing-hidden' : 'default'); }
  else if (key === 'i') { setSpatialMode(spatialMode === 'immersive' ? previousSpatialMode : 'immersive'); }
  else if (key === 'n') { toggleSpatialAtmosphere(); }
  else if (event.code === 'Space' && replay.track) { event.preventDefault(); toggleReplayPlay(); }
});

await refreshState();
changeLanguage(i18n.language, false);
connectEvents();
initializeViewer();
initializeLayout();

try {
  const { createSetupView } = await import('./setup.js');
  setupView = createSetupView({ document, api, t, i18n, toast, onActivated: refreshState });
  setupView.applyState(state);
} catch (error) {
  dom['open-settings'].onclick = () => toast(t('设置页面暂不可用：{error}', { error: error.message }), true);
}
