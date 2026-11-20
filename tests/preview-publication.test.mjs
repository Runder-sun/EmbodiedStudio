import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { publicProject } from '../src/server.mjs';

// SYSTEM_VERIFICATION_ONLY: transport/UI fixtures, never generator products.
function project() {
  const manifest = 'projects/p_preview/revisions/rev_a/manifest.json';
  return { id: 'p_preview', name: 'SYSTEM_VERIFICATION_ONLY preview', prompt: '', status: 'complete',
    sourceHash: 'source', messages: [], events: [], jobs: [], artifacts: {},
    generation: { id: 'g_preview', status: 'complete', requirementVersion: 1, requestHash: 'request' },
    delivery: { accepted: true, status: 'accepted', generationId: 'g_preview', requirementVersion: 1, requestHash: 'request', marker: 'preserved' },
    latestBuild: { projectId: 'p_preview', revision: 'rev_a', sourceHash: 'source',
      artifacts: { manifest }, publicationArtifactDigests: { [manifest]: 'a'.repeat(64) },
      compiled: { usd: 'scene-source.usda', frozenUsdSha256: 'b'.repeat(64) },
      rendering: { webPreview: { available: false, reason: 'historical converter failure' } } } };
}

function previewJob(p, overrides = {}) {
  const directory = `projects/${p.id}/previews/rev_a/converter/new_preview`;
  return { id: 'j_preview', requestId: 'new_preview', kind: 'observe', status: 'completed', revision: 'rev_a',
    generationId: p.generation.id, payload: { task: { operation: 'observe', output: 'web_preview', revision: 'rev_a' } },
    result: { schema: 'sceneagent-web-preview-v1', projectId: p.id, revision: 'rev_a', checkedScope: 'web_preview',
      source: { manifestRef: p.latestBuild.artifacts.manifest, manifestSha256: 'a'.repeat(64),
        usdRef: `projects/${p.id}/revisions/rev_a/scene-source.usda`, usdSha256: 'b'.repeat(64) },
      converterRuntime: 'converter', artifacts: { preview: directory + '/preview.glb', previewEvidence: directory + '/preview-evidence.json' },
      artifactRef: directory + '/receipt.json', digests: { preview: 'c'.repeat(64), previewEvidence: 'd'.repeat(64) } },
    ...overrides };
}

test('published derived preview is displayed for the same frozen revision without changing its manifest or accepted receipt', () => {
  const p = project();
  p.jobs.push(previewJob(p));
  const before = JSON.stringify(p);
  const shown = publicProject(p);
  assert.match(shown.artifacts.preview, /previews%2Frev_a%2Fconverter%2Fnew_preview%2Fpreview.glb/);
  assert.equal(shown.webPreview.status, 'completed');
  assert.equal(shown.webPreview.jobId, 'j_preview');
  assert.deepEqual(shown.latestBuild.rendering, p.latestBuild.rendering, 'the frozen failed conversion remains historical fact');
  assert.equal(shown.delivery.accepted, true);
  assert.equal(JSON.stringify(p), before, 'display derivation never rewrites the stored build, requirement, or receipt');
});

test('preview display rejects pending, foreign, stale and unbound derived results', () => {
  for (const change of [
    job => { job.status = 'running'; },
    job => { job.result.projectId = 'another_project'; },
    job => { job.result.revision = 'older_revision'; },
    job => { job.result.source.manifestSha256 = 'e'.repeat(64); },
    job => { job.result.source.usdSha256 = 'e'.repeat(64); },
    job => { job.result.artifacts.preview = 'projects/another_project/previews/preview.glb'; },
    job => { job.result.artifacts.preview = 'projects/p_preview/previews/rev_a/converter/../preview.glb'; },
    job => { job.result.checkedScope = 'rendered_camera_frame'; },
  ]) {
    const p = project(), job = previewJob(p); change(job); p.jobs.push(job);
    assert.equal(publicProject(p).artifacts.preview, undefined);
  }
});

test('a failed refresh retains the last same-version published preview and reports the actual failure', () => {
  const p = project();
  p.jobs.push(previewJob(p), previewJob(p, { id: 'j_failed', status: 'failed', result: null, error: 'actual converter diagnostic' }));
  const shown = publicProject(p);
  assert.ok(shown.artifacts.preview);
  assert.equal(shown.webPreview.status, 'failed');
  assert.equal(shown.webPreview.error, 'actual converter diagnostic');
});

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
function page(projects) {
  const requests = [];
  const element = id => ({ id, value: '', dataset: {}, children: [], hidden: false, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    style: { setProperty() {} }, classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, getAttribute() { return 'false'; },
    removeAttribute() {}, append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    get options() { return this.children; }, pause() {}, load() {}, focus() {} });
  const elements = [...html.matchAll(/\bid="([^"]+)"/g)].map(row => element(row[1]));
  const document = { getElementById: id => elements.find(e => e.id === id), querySelectorAll: s => s === '[id]' ? elements : [],
    querySelector: () => element('query'), createElement: element, createTextNode: text => ({ text }) };
  const fetch = (path, options) => new Promise(resolve => requests.push({ path, options,
    resolve: () => resolve({ ok: true, headers: { get() { return 'application/json'; } }, async json() { return {}; } }) }));
  const run = new Function('document', 'window', 'localStorage', 'fetch', code.slice(0, code.indexOf('\nawait refreshState();')) +
    '\nrefreshState=async()=>{};return {dom,applyState};');
  const p = run(document, { addEventListener() {}, location: { origin: 'http://localhost' } },
    { getItem() { return ''; }, setItem() {} }, fetch);
  const apply = () => p.applyState({ codex: { ready: true }, config: {}, projects });
  apply();
  return { ...p, requests, apply };
}

test('opening an idle frozen scene with no preview requests one same-version derived observation', async () => {
  const p = project(), browser = page([publicProject(p)]);
  assert.equal(browser.requests.length, 1);
  assert.equal(browser.requests[0].path, '/api/tools/scene_observe');
  const input = JSON.parse(browser.requests[0].options.body);
  assert.equal(input.output, 'web_preview');
  assert.equal(input.revision, p.latestBuild.revision);
  assert.equal(input.projectId, p.id);
  assert.equal(input.generationId, p.generation.id);
  assert.ok(input.requestId);
  browser.apply(); browser.apply();
  assert.equal(browser.requests.length, 1, 'SSE rerenders cannot launch duplicate conversion work');
  browser.requests[0].resolve();
});

test('the webpage waits for active generation or compute work before recovering a missing preview', () => {
  for (const change of [
    p => { p.generation.status = 'active'; p.activeTurnId = 'turn'; },
    p => { p.jobs.push({ id: 'j_running', kind: 'rollout', status: 'running' }); },
  ]) {
    const p = project(); change(p);
    assert.equal(page([publicProject(p)]).requests.length, 0);
  }
});

test('published preview and an existing conversion do not start duplicate recovery work', () => {
  const published = project(); published.jobs.push(previewJob(published));
  assert.equal(page([publicProject(published)]).requests.length, 0);
  const pending = project(); pending.jobs.push(previewJob(pending, { status: 'running', result: null }));
  assert.equal(page([publicProject(pending)]).requests.length, 0);
});

test('a failed conversion waits for explicit retry and the next attempt uses a new request identity', async () => {
  const p = project(), shown = publicProject(p), browser = page([shown]);
  const first = JSON.parse(browser.requests[0].options.body);
  shown.webPreview = { status: 'failed', jobId: 'failed_preview', error: 'actual failure' };
  browser.requests[0].resolve();
  await new Promise(resolve => setImmediate(resolve));
  browser.apply(); browser.apply();
  assert.equal(browser.requests.length, 1, 'rendering a failure cannot create a retry loop');
  const retry = browser.dom['scene-error'].children.find(child => child.id === 'button');
  assert.equal(retry.textContent, '重新生成网页预览');
  retry.onclick();
  assert.equal(browser.requests.length, 2);
  assert.notEqual(JSON.parse(browser.requests[1].options.body).requestId, first.requestId);
  browser.requests[1].resolve();
});
