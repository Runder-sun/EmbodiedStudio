import { config } from './config.mjs';

const base = process.env.SCENEAGENT_URL || 'http://127.0.0.1:' + config.port;
const [command, id, ...extra] = process.argv.slice(2);
async function request(route, data, method = data === undefined ? 'GET' : 'POST') {
  const response = await fetch(base + route, { method,
    ...(data === undefined ? {} : { headers: { 'content-type': 'application/json' }, body: JSON.stringify(data) }) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'HTTP ' + response.status);
  return result;
}
function unwrap(value) { return value.structuredContent ?? value; }
async function waitJob(projectId, initial) {
  let value = initial, last = '';
  while (value.jobId && !['completed','failed','cancelled'].includes(value.status)) {
    const context = await request('/api/projects/' + encodeURIComponent(projectId));
    value = unwrap(await request('/api/tools/job', { projectId, generationId: context.generation.id,
      jobId: value.jobId, action: 'wait', cursor: value.cursor || 0, waitMs: 30000 }));
    const marker = JSON.stringify([value.status, value.cursor]);
    if (marker !== last) {
      console.log(JSON.stringify({ type: 'progress', jobId: value.jobId, status: value.status, events: value.events || [] }));
      last = marker;
    }
  }
  if (['failed','cancelled'].includes(value.status)) throw new Error(value.error || value.status);
  return value;
}
try {
  let result;
  if (command === 'health') result = await request('/api/health');
  else if (command === 'assets') result = await request('/api/assets?q=' + encodeURIComponent(id || ''));
  else if (command === 'inspect' && id) result = await request('/api/projects/' + encodeURIComponent(id));
  else if (command === 'tool' && id) {
    const input = JSON.parse(extra.join(' ') || '{}');
    result = await request('/api/tools/' + encodeURIComponent(id), input);
  } else if (command === 'generate' && id) {
    const input = JSON.parse(extra.join(' ') || '{}');
    result = await request('/api/projects/' + encodeURIComponent(id) + '/chat', { ...input, mode: 'generate' });
  } else if (['build','rollout'].includes(command) && id) {
    const input = command === 'rollout' && extra.length ? { taskIds: extra } : {};
    const started = await request('/api/projects/' + encodeURIComponent(id) + '/' + command, input);
    console.log(JSON.stringify({ type: 'started', ...started }));
    result = await waitJob(id, started);
  } else {
    console.log('用法：health | assets 关键词 | inspect 项目ID | generate 项目ID JSON参数 | build 项目ID | rollout 项目ID [任务ID] | tool 工具名 JSON参数');
    process.exitCode = 1;
  }
  if (result !== undefined) console.log(JSON.stringify({ type: 'result', result }));
} catch (error) {
  console.error(JSON.stringify({ type: 'error', error: error.message.includes('fetch failed') ? '本地工作台未启动，请先运行 npm start。' : error.message }));
  process.exitCode = 1;
}
