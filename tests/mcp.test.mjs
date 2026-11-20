import test from 'node:test';
import assert from 'node:assert/strict';
import { createSceneAgentMcp, TOOL_SCHEMAS } from '../src/mcp.mjs';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

async function connect(t, options) {
  const server = await createSceneAgentMcp({ studioUrl: 'http://127.0.0.1:4567', ...options });
  const client = new Client({ name: 'sceneagent-test', version: '0.1.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  t.after(async () => { await client.close(); await server.close(); });
  return client;
}

test('Kit tools expose one optional worker budget and forward only explicitly supplied values', async t => {
  const sent = [];
  const client = await connect(t, { projectId: 'p', generationId: 'g', fetchImpl: async (_url, options) => {
    sent.push(JSON.parse(options.body)); return Response.json({ jobId: 'j', status: 'queued' });
  } });
  const tools = (await client.listTools()).tools;
  const kit = ['assets_prepare', 'scene_build', 'scene_observe', 'robot_run', 'project_deliver'];
  for (const tool of tools) {
    assert.equal(Boolean(tool.inputSchema.properties.workerTimeoutSeconds), kit.includes(tool.name), tool.name);
    assert.equal(tool.inputSchema.required.includes('workerTimeoutSeconds'), false);
  }
  await client.callTool({ name: 'robot_run', arguments: { revision: 'rev', taskIds: ['task'], requestId: 'long', workerTimeoutSeconds: 100000 } });
  assert.equal(sent[0].workerTimeoutSeconds, 100000);
  await client.callTool({ name: 'scene_observe', arguments: { revision: 'rev', requestId: 'default' } });
  assert.equal(Object.hasOwn(sent[1], 'workerTimeoutSeconds'), false);
});

test('SDK discovery exports the single tool catalog and forwards a bounded asynchronous result', async t => {
  const calls = [];
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      return Response.json({ status: 'pending', jobId: 'job-1', cursor: 3 });
    } });
  const listed = await client.listTools();
  assert.equal(listed.tools.length, 10);
  assert.deepEqual(listed.tools.map(tool => tool.name), Object.keys(TOOL_SCHEMAS));
  const result = await client.callTool({ name: 'job', arguments: { jobId: 'job-1', action: 'wait', waitMs: 30_000 } });
  assert.deepEqual(result.structuredContent, { status: 'pending', jobId: 'job-1', cursor: 3 });
  assert.deepEqual(JSON.parse(result.content[0].text), result.structuredContent);
  assert.equal(calls[0].url, 'http://127.0.0.1:4567/api/tools/job');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(calls[0].options.headers['x-sceneagent-origin'], 'codex-mcp');
  assert.deepEqual(JSON.parse(calls[0].options.body), { jobId: 'job-1', action: 'wait', waitMs: 30_000,
    projectId: 'project-1', generationId: 'generation-1' });
  assert.equal(calls[0].options.signal.aborted, false);
});

test('MDL search roots remain an optional declaration on the existing asset tool', async t => {
  const sent = [];
  const client = await connect(t, { projectId: 'p', generationId: 'g', fetchImpl: async (_url, options) => {
    sent.push(JSON.parse(options.body)); return Response.json({ jobId: 'j', status: 'queued' });
  } });
  const schema = TOOL_SCHEMAS.assets_prepare.inputSchema;
  assert.equal(schema.required.includes('mdlSearchRoots'), false);
  assert.equal(schema.properties.mdlSearchRoots.type, 'array');
  await client.callTool({ name: 'assets_prepare', arguments: { requestId: 'mdl', resourceFile: '/public/model.usda',
    dependencyFiles: ['/public/mdl/module.mdl'], mdlSearchRoots: ['/public/mdl'] } });
  assert.deepEqual(sent[0].mdlSearchRoots, ['/public/mdl']);
});

test('actual image blocks and structured scope pass through the SDK without conversion to URLs', async t => {
  const image = { type: 'image', data: 'iVBORw0KGgo=', mimeType: 'image/png' };
  const receipt = { structuredContent: { revision: 'revision-1', nodeScope: ['room-1'] },
    content: [{ type: 'text', text: '实际房间图像' }, image] };
  const client = await connect(t, { fetchImpl: async () => Response.json(receipt) });
  const result = await client.callTool({ name: 'scene_observe', arguments: {
    projectId: 'project-1', generationId: 'generation-1', revision: 'revision-1', nodeId: 'room-1' } });
  assert.deepEqual(result, receipt);
  assert.deepEqual(result.content[1], image);
});

test('batch camera selection and both actual image blocks pass through the existing MCP tool', async t => {
  let sent;
  const images=[{type:'image',mimeType:'image/png',data:'Zmlyc3Q='},{type:'image',mimeType:'image/png',data:'c2Vjb25k'}];
  const receipt={structuredContent:{revision:'revision-1',cameraIds:['front','side']},content:[{type:'text',text:'SYSTEM_VERIFICATION_ONLY'},...images]};
  const client=await connect(t,{projectId:'project-1',generationId:'generation-1',fetchImpl:async(_url,options)=>{
    sent=JSON.parse(options.body);return Response.json(receipt);
  }});
  const result=await client.callTool({name:'scene_observe',arguments:{revision:'revision-1',cameraIds:['front','side'],requestId:'batch'}});
  assert.deepEqual(sent.cameraIds,['front','side']);
  assert.equal(sent.projectId,'project-1');
  assert.equal(sent.generationId,'generation-1');
  assert.deepEqual(result.content.slice(1),images);
});

test('missing or different session scope is rejected before HTTP dispatch', async t => {
  let calls = 0;
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async () => { calls++; return Response.json({}); } });
  const result = await client.callTool({ name: 'project_inspect', arguments: { projectId: 'another-project' } });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /scope does not match/);
  assert.equal(calls, 0);
  const unscoped = await connect(t, { fetchImpl: async () => { calls++; return Response.json({}); } });
  const missing = await unscoped.callTool({ name: 'project_inspect', arguments: {} });
  assert.equal(missing.isError, true);
  assert.match(missing.content[0].text, /projectId and generationId are required/);
  assert.equal(calls, 0);
});

test('HTTP and transport failures become explicit tool errors without fabricated results', async t => {
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async () => Response.json({ error: 'revision belongs to another project' }, { status: 409 }) });
  const result = await client.callTool({ name: 'scene_observe', arguments: { revision: 'revision-1' } });
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, 'revision belongs to another project');
  assert.equal('structuredContent' in result, false);
  const offline = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async () => { throw new TypeError('connection refused'); } });
  const failed = await offline.callTool({ name: 'project_inspect', arguments: {} });
  assert.equal(failed.isError, true);
  assert.equal(failed.content[0].text, 'connection refused');
  await assert.rejects(client.callTool({ name: 'invented_tool', arguments: {} }), /Unknown SceneAgent tool/);
});

test('a configured remote MCP HTTP target is rejected', async () => {
  await assert.rejects(createSceneAgentMcp({ studioUrl: 'https://example.com' }), /local Studio HTTP/);
});

test('project inspect transmits one complete JSON text after checking duplicate data', async t => {
  const data = { projectId: 'project-1', requirements: [{ text: '完整原文' }], worldContext: { subtree: [{ id: 'world' }] } };
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async () => Response.json({ structuredContent: data, content: [{ type: 'text', text: JSON.stringify(data) }] }) });
  const result = await client.callTool({ name: 'project_inspect', arguments: {} });
  assert.equal(Object.hasOwn(result, 'structuredContent'), false);
  assert.deepEqual(JSON.parse(result.content[0].text), data);
});

for (const text of ['{}', 'not JSON', 'null']) test('project inspect cannot hide conflicting structured data by removing its duplicate: ' + text, async t => {
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1',
    fetchImpl: async () => Response.json({ structuredContent: { projectId: 'project-1' }, content: [{ type: 'text', text }] }) });
  const result = await client.callTool({ name: 'project_inspect', arguments: {} });
  assert.equal(result.isError, true);
});


test('v5 observation names a frozen camera and removes no-op view and focus', () => {
  const fields=TOOL_SCHEMAS.scene_observe.inputSchema.properties;
  assert.ok(fields.cameraId);
  assert.equal(fields.view,undefined);assert.equal(fields.focus,undefined);
  assert.equal(Object.keys(TOOL_SCHEMAS).length,10);
});

test('a system blocker report reaches the scoped host as a control request', async t => {
  let sent;
  const client = await connect(t, { projectId: 'project-1', generationId: 'generation-1', fetchImpl: async (_url, options) => {
    sent = JSON.parse(options.body);
    return Response.json({ status: 'waiting_system', id: 'generation-1' });
  } });
  const result = await client.callTool({ name: 'generation_wait_system', arguments: {
    requestId: 'wait-1', reason: '系统执行器没有完成', evidence: ['原作业错误记录'],
  } });
  assert.equal(result.structuredContent.status, 'waiting_system');
  assert.equal(sent.generationId, 'generation-1');
  assert.equal(sent.requestId, 'wait-1');
  assert.equal(TOOL_SCHEMAS.generation_wait_system.annotations.readOnlyHint, false);
});

test('discovery requires only scope fields without defaults and never mutates the shared catalog', async t => {
  const original = structuredClone(TOOL_SCHEMAS);
  for (const defaults of [{}, { projectId: 'p' }, { generationId: 'g' }, { projectId: 'p', generationId: 'g' }]) {
    const client = await connect(t, { ...defaults, fetchImpl: async () => Response.json({}) });
    for (const entry of (await client.listTools()).tools) {
      for (const key of ['projectId', 'generationId']) assert.equal(entry.inputSchema.required.includes(key), !defaults[key], entry.name + ':' + key);
    }
  }
  assert.deepEqual(TOOL_SCHEMAS, original);
  for (const entry of Object.values(TOOL_SCHEMAS)) assert.ok(['projectId', 'generationId'].every(key => entry.inputSchema.required.includes(key)));
});

test('case selection is public and retired repair hints remain constructible only for historical retries', () => {
  assert.ok(TOOL_SCHEMAS.robot_run.inputSchema.properties.controller.properties.caseId);
  for (const name of ['scene_build', 'task_check']) {
    const field = TOOL_SCHEMAS[name].inputSchema.properties.changedNodeIds;
    assert.equal(field.deprecated, true);
    assert.match(field.description, /historical|retry/i);
  }
});

test('historical repair parameters reach the host unchanged for exact receipt lookup', async t => {
  let sent;
  const client = await connect(t, { projectId: 'p', generationId: 'g', fetchImpl: async (_url, options) => {
    sent = JSON.parse(options.body);
    return Response.json({ jobId: 'historical', status: 'completed' });
  } });
  const args = { requestId: 'old_build', expectedSourceHash: 'old', changedNodeIds: ['old-node'] };
  const result = await client.callTool({ name: 'scene_build', arguments: args });
  assert.equal(result.structuredContent.jobId, 'historical');
  assert.deepEqual(sent, { ...args, projectId: 'p', generationId: 'g' });
});

test('robot installation profiles and frozen setup references use the existing robot_run contract', async t => {
  const sent = [];
  const client = await connect(t, { projectId: 'p', generationId: 'g', fetchImpl: async (_url, options) => {
    sent.push(JSON.parse(options.body)); return Response.json({ jobId: 'setup', status: 'queued' });
  } });
  const run = (await client.listTools()).tools.find(row => row.name === 'robot_run').inputSchema;
  assert.equal(run.properties.robotSetup.properties.robots.minItems, 1);
  assert.match(run.properties.robotSetup.properties.robots.items.properties.profileRef.description, /project.*robot\.json/i);
  assert.deepEqual(run.properties.robotSetup.required, ['robots', 'resourceRefs']);
  assert.equal(run.properties.robotSetup.additionalProperties, false);
  assert.equal(run.properties.robotSetupRef.type, 'string');
  const args = { revision: 'rev', taskIds: ['task'], requestId: 'new_setup',
    behavior: { sourceRef: 'expert.py', helperRefs: [] },
    robotSetup: { robots: [{ id: 'left', profileRef: 'arm.robot.json' }, { id: 'right', profileRef: 'arm.robot.json',
      at: [1, 0, 0], orientation: [1, 0, 0, 0], frame: 'table', owner: 'room' }],
    resourceRefs: ['assets/prepared/arm/report.json'], recording: { task: { cameras: [], primaryCameraId: null } } } };
  await client.callTool({ name: 'robot_run', arguments: args });
  assert.deepEqual(sent[0], { ...args, projectId: 'p', generationId: 'g' });
  const reuse = { revision: 'rev', taskIds: ['task'], requestId: 'reuse_setup', robotSetupRef: 'projects/p/robot-setups/hash/manifest.json' };
  await client.callTool({ name: 'robot_run', arguments: reuse });
  assert.deepEqual(sent[1], { ...reuse, projectId: 'p', generationId: 'g' });
});
