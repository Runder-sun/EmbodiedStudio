import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';

// Transport only: Codex owns planning, tools, permissions and conversation state.
export class CodexBridge {
  constructor({ cwd = process.cwd(), onEvent = () => {}, onRequest,
    command = ['codex', 'app-server', '--listen', 'stdio://'], timeoutMs = 30_000, networkAccess = false,
    skillsRoot = null, skillPath = null, mcp = null, env = {}, model = null } = {}) {
    Object.assign(this, { cwd, onEvent, onRequest, command, timeoutMs, networkAccess, skillsRoot, skillPath, mcp, env, model });
    this.pending = new Map();
    this.requests = new Map();
    this.nextId = 1;
    this.child = null;
    this.ready = false;
    this.error = null;
    this.serverInfo = null;
    this.skillCwds = new Set();
    this.mcpThreads = new Set();
  }

  status() {
    return { ready: this.ready, pid: this.child?.pid ?? null,
      pending: this.pending.size, pendingRequests: this.requests.size,
      error: this.error, serverInfo: this.serverInfo, skillLoaded: this.skillCwds.size > 0,
      mcpThreads: [...this.mcpThreads] };
  }

  async start() {
    if (this.ready) return this.serverInfo;
    if (this.starting) return this.starting;
    this.starting = this._start();
    try { return await this.starting; }
    finally { this.starting = null; }
  }

  async _start() {
    this.error = null;
    const [executable, ...args] = this.command;
    if (this.mcp) {
      const config = { command: this.mcp.command, args: this.mcp.args ?? [], required: true };
      for (const [key, value] of Object.entries(config)) args.push('-c', `mcp_servers.sceneagent.${key}=${JSON.stringify(value)}`);
      for (const [key, value] of Object.entries(this.mcp.env ?? {})) {
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error(`Invalid MCP environment name: ${key}`);
        args.push('-c', `mcp_servers.sceneagent.env.${key}=${JSON.stringify(String(value))}`);
      }
    }
    const child = spawn(executable, args, { cwd: this.cwd, env: { ...process.env, ...this.env }, stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    lines.on('line', line => {
      try { this._receive(JSON.parse(line)); }
      catch (error) {
        this._fail(new Error(`Invalid Codex message: ${error.message}`));
        child.kill();
      }
    });
    // Keep diagnostic output separate from the JSON-RPC stream.
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', text => this.onEvent({ method: 'bridge/stderr', params: { text } }));
    child.stdin.on('error', error => this._fail(error));
    child.once('error', error => this._fail(error));
    child.once('close', (code, signal) => {
      lines.close();
      if (this.child !== child) return;
      this._fail(new Error(`Codex exited (${signal ?? code})`));
      this.child = null;
      this.onEvent({ method: 'bridge/status', params: this.status() });
    });
    try {
      this.serverInfo = await this._request('initialize', {
        clientInfo: { name: 'sceneagent_local', title: 'SceneAgent', version: '0.1.0' },
        capabilities: { experimentalApi: true },
      });
      await this._send({ method: 'initialized' });
      if (this.skillsRoot) await this._request('skills/extraRoots/set', { extraRoots: [resolve(this.skillsRoot)] });
      this.ready = true;
      this.onEvent({ method: 'bridge/status', params: this.status() });
      return this.serverInfo;
    } catch (error) {
      await this.close();
      throw error;
    }
  }

  async startThread({ cwd = this.cwd, instructions } = {}) {
    await this.start();
    await this.prepareSkills(cwd);
    const result = await this._request('thread/start', {
      cwd, sandbox: 'workspace-write', approvalPolicy: 'on-request', approvalsReviewer: 'user',
      ...(this.model ? { model: this.model } : {}),
      ...(instructions ? { developerInstructions: instructions } : {}),
    });
    await this.ensureMcpReady(result.thread.id);
    return result.thread;
  }

  async resumeThread(threadId, { cwd = this.cwd } = {}) {
    await this.start();
    await this.prepareSkills(cwd);
    const result = await this._request('thread/resume', {
      threadId, cwd, sandbox: 'workspace-write', approvalPolicy: 'on-request', approvalsReviewer: 'user',
      ...(this.model ? { model: this.model } : {}),
    });
    await this.ensureMcpReady(result.thread.id);
    return result.thread;
  }

  async turn(threadId, message, { cwd = this.cwd, skill = true, beforeSend } = {}) {
    await this.start();
    if (skill) await this.prepareSkills(cwd);
    await this.ensureMcpReady(threadId);
    const input = [{ type: 'text', text: message }];
    if (skill && this.skillPath) input.push({ type: 'skill', name: 'scene-generation', path: resolve(this.skillPath) });
    try { beforeSend?.(); }
    catch (error) {
      if (!(error instanceof Error)) error = new Error(String(error));
      error.nativeInputNotSent = true;
      throw error;
    }
    const result = await this._request('turn/start', {
      threadId, cwd, input,
      ...(this.model ? { model: this.model } : {}),
      approvalPolicy: 'on-request', approvalsReviewer: 'user',
      sandboxPolicy: { type: 'workspaceWrite', writableRoots: [cwd], networkAccess: this.networkAccess },
    });
    return result.turn;
  }

  async prepareSkills(cwd = this.cwd) {
    if (!this.skillPath || this.skillCwds.has(cwd)) return;
    const result = await this._request('skills/list', { cwds: [cwd], forceReload: true });
    const skill = result.data.flatMap(entry => entry.skills).find(item =>
      item.name === 'scene-generation' && resolve(item.path) === resolve(this.skillPath) && item.enabled);
    if (!skill) throw new Error(`scene-generation skill is not available: ${this.skillPath}`);
    this.skillCwds.add(cwd);
    this.onEvent({ method: 'bridge/skillReady', params: { cwd, name: skill.name, path: skill.path } });
  }

  async ensureMcpReady(threadId) {
    if (!this.mcp || this.mcpThreads.has(threadId)) return;
    const deadline = Date.now() + this.timeoutMs;
    do {
      const result = await this._request('mcpServerStatus/list', { threadId, serverName: 'sceneagent', detail: 'toolsAndAuthOnly' });
      const server = result.data.find(item => item.name === 'sceneagent');
      if (server && ['failed', 'cancelled', 'disabled', 'authenticationRequired'].includes(server.runtimeStatus)) {
        throw new Error(`sceneagent MCP ${server.runtimeStatus}: ${server.toolsError ?? 'connection unavailable'}`);
      }
      if (server?.runtimeStatus === 'connected') {
        if (server.toolsError) throw new Error(`sceneagent MCP tools failed: ${server.toolsError}`);
        const names = Object.values(server.tools).map(tool => tool.name);
        const missing = (this.mcp.requiredTools ?? []).filter(name => !names.includes(name));
        if (missing.length) throw new Error(`sceneagent MCP missing tools: ${missing.join(', ')}`);
        this.mcpThreads.add(threadId);
        this.onEvent({ method: 'bridge/mcpReady', params: { threadId, tools: names } });
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    } while (Date.now() < deadline);
    throw new Error(`sceneagent MCP was not ready after ${this.timeoutMs} ms`);
  }

  async readThread(threadId, { includeTurns = true } = {}) {
    await this.start();
    return (await this._request('thread/read', { threadId, includeTurns })).thread;
  }

  async listTurns(threadId, { cursor = null, limit = 50, sortDirection = 'asc', itemsView = 'notLoaded' } = {}) {
    await this.start();
    return this._request('thread/turns/list', { threadId, cursor, limit, sortDirection, itemsView });
  }

  async listItems(threadId, { turnId, cursor = null, limit = 50, sortDirection = 'asc' } = {}) {
    await this.start();
    return this._request('thread/items/list', { threadId, ...(turnId ? { turnId } : {}), cursor, limit, sortDirection });
  }

  async setGoal(threadId, objective, { tokenBudget, status, origin = 'user' } = {}) {
    await this.start();
    return (await this._request('thread/goal/set', { threadId, objective, origin,
      ...(tokenBudget !== undefined ? { tokenBudget } : {}), ...(status ? { status } : {}) })).goal;
  }

  async getGoal(threadId) {
    await this.start();
    return (await this._request('thread/goal/get', { threadId })).goal;
  }

  async steer(threadId, turnId, message, { beforeSend } = {}) {
    await this.start();
    try { beforeSend?.(); }
    catch (error) {
      if (!(error instanceof Error)) error = new Error(String(error));
      error.nativeInputNotSent = true;
      throw error;
    }
    return this._request('turn/steer', {
      threadId, expectedTurnId: turnId, input: [{ type: 'text', text: message }],
    });
  }

  async interrupt(threadId, turnId) {
    await this.start();
    return this._request('turn/interrupt', { threadId, turnId });
  }

  async respond(id, result) {
    if (!this.requests.has(id)) throw new Error('Codex request is no longer pending');
    await this._send({ id, result });
    this.requests.delete(id);
  }

  _receive(message) {
    if (message.method) {
      if (message.id !== undefined) {
        this.requests.set(message.id, message);
        if (this.onRequest) this.onRequest(message);
        else this.onEvent({ method: 'bridge/request', params: message });
      } else {
        if (message.method === 'serverRequest/resolved') this.requests.delete(message.params.requestId);
        this.onEvent(message);
      }
      return;
    }
    const error = message.error ? Object.assign(new Error(message.error.message), {
      code: message.error.code, data: message.error.data,
    }) : null;
    this._settle(message.id, error, message.result);
  }

  _send(message) {
    return new Promise((resolve, reject) => {
      if (!this.child || this.child.stdin.destroyed) return reject(new Error('Codex is not running'));
      this.child.stdin.write(`${JSON.stringify(message)}\n`, error => {
        if (error) return reject(error);
        this.onEvent({ method: 'transport/sent', params: { message } });
        resolve();
      });
    });
  }

  _request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this._settle(id,
        new Error(`Codex ${method} timed out after ${this.timeoutMs} ms`)), this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this._send({ id, method, params }).catch(error => this._settle(id, error));
    });
  }

  _settle(id, error, result) {
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    clearTimeout(pending.timer);
    if (error) pending.reject(error);
    else pending.resolve(result);
  }

  _fail(error) {
    this.ready = false;
    this.error = error.message;
    this.skillCwds.clear();
    this.mcpThreads.clear();
    for (const id of this.pending.keys()) this._settle(id, error);
    this.requests.clear();
  }

  async close() {
    const child = this.child;
    if (!child) return;
    this._fail(new Error('Codex connection closed'));
    await new Promise(resolve => {
      const timer = setTimeout(() => child.kill('SIGKILL'), 1_000);
      timer.unref();
      child.once('close', () => { clearTimeout(timer); resolve(); });
      child.kill();
    });
  }
}
