import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { createStudio } from '../src/server.mjs';

test('a clean Studio starts the web installer and only activates an explicit saved draft', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'embodiedstudio-setup-api-'));
  const file = path.join(root, 'studio.config.json');
  const studio = await createStudio({ port:0, configFile:file });
  const address = await studio.start();
  t.after(async () => { await studio.close(); await rm(root, { recursive:true, force:true }); });
  const get = async route => fetch(address + route);
  const request = async (route, data, method='POST') => fetch(address + route, {
    method, body:JSON.stringify(data), headers:{ 'content-type':'application/json', origin:address }
  });
  const check = await get('/api/setup');
  assert.equal(check.status, 200);
  const setup = await check.json();
  assert.equal(setup.configuration.configured, false);
  assert.equal(setup.configuration.file, file);
  assert.equal((await (await get('/api/state')).json()).config.configured, false);
  assert.equal((await request('/api/setup/config', { action:'save', baseHash:'stale', value:{} }, 'PUT')).status, 409);
  const draft = await (await request('/api/setup/config', { action:'save', baseHash:null, value:{
    workspaceRoot:path.join(root,'workspace'), runtimeMode:'ssh', remoteHost:'test-host',
    remoteRoot:'/srv/data', remotePython:'/srv/data/env/bin/python'
  } }, 'PUT')).json();
  assert.ok(draft.draftHash);
  await assert.rejects(readFile(file), { code:'ENOENT' });
  const response = await request('/api/setup/config', { action:'activate', draftHash:draft.draftHash }, 'PUT');
  assert.equal(response.status, 200);
  assert.equal((await response.json()).restartRequired, true);
  assert.equal(JSON.parse(await readFile(file,'utf8')).remoteHost,'test-host');
  assert.equal((await (await get('/api/state')).json()).config.configured, false);
  assert.equal((await (await get('/api/setup')).json()).configuration.restartRequired, true);
  await studio.close();
  const restarted = await createStudio({ port:0, configFile:file,
    bridge:{ async start(){}, async close(){}, status:()=>({ ready:true }) } });
  try {
    const newAddress = await restarted.start();
    const loaded = await (await fetch(newAddress + '/api/setup')).json();
    assert.equal(loaded.configuration.configured, true);
    assert.equal(loaded.configuration.effective.remoteHost, 'test-host');
    assert.equal(loaded.configuration.restartRequired, false);
    assert.equal(loaded.configuration.draftHash, null);
  } finally { await restarted.close(); }
});
