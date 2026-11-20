import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdtemp, cp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

test('a fresh checkout loads safe display defaults without a private config', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'studio-config-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await cp(new URL('../src/config.mjs', import.meta.url), path.join(root, 'src/config.mjs'));
  const read = () => spawnSync(process.execPath, ['--input-type=module', '-e',
    "import { config, configuration } from './src/config.mjs'; console.log(JSON.stringify({ ...config, diagnostics:configuration.diagnostics }));"], { cwd: root, encoding: 'utf8' });
  const result = read();
  assert.equal(result.status, 0, result.stderr);
  const defaults = JSON.parse(result.stdout);
  assert.equal(defaults.port, 8765);
  assert.equal(defaults.remoteHost, null);

  await writeFile(path.join(root, 'studio.config.json'), JSON.stringify({ port: 8777, remoteHost: 'my-render-host' }));
  const configured = read();
  assert.equal(configured.status, 0, configured.stderr);
  const overrides = JSON.parse(configured.stdout);
  assert.equal(overrides.port, 8777);
  assert.equal(overrides.remoteHost, 'my-render-host');

  await writeFile(path.join(root, 'studio.config.json'), 'invalid config');
  const invalid = read();
  assert.equal(invalid.status, 0, invalid.stderr);
  assert.equal(JSON.parse(invalid.stdout).diagnostics.length, 1);
});
