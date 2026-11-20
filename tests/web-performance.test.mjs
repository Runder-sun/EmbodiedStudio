// SYSTEM_VERIFICATION_ONLY: isolated DOM/Three fixtures, never formal scene products.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as THREE from 'three';

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
function page() {
  const requests = [], frames = new Map();
  let nextFrame = 0, reportSerializations = 0, measuredReport;
  const matches = (row, selector) => selector.startsWith('.') ? row.className?.split(' ').includes(selector.slice(1))
    : selector === '[data-event-key]' ? row.dataset.eventKey !== undefined : row.tagName === selector;
  function element(tagName) {
    const row = { tagName, id: tagName, children: [], dataset: {}, attributes: {}, className: '', value: '', textContent: '',
      hidden: false, open: false, scrollHeight: 1000, clientHeight: 100, scrollTop: 20, replaces: 0,
      style: { setProperty() {} }, classList: { toggle() {} }, addEventListener() {},
      setAttribute(key, value) { this.attributes[key] = value; }, getAttribute(key) { return this.attributes[key] || 'false'; },
      removeAttribute(key) { delete this[key]; }, pause() {}, load() {},
      append(...children) { this.children.push(...children.flatMap(child => child.tagName === '#fragment' ? child.children : [child])); },
      replaceChildren(...children) { this.replaces++; this.children = []; this.append(...children); },
      querySelectorAll(selector) { const result = []; const visit = child => { if (matches(child, selector)) result.push(child); child.children?.forEach(visit); }; this.children.forEach(visit); return result; },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      get options() { return this.children; }, get lastElementChild() { return this.children.at(-1); } };
    return row;
  }
  const elements = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => Object.assign(element('div'), { id: match[1] }));
  const document = { hidden: false, addEventListener() {}, getElementById: id => elements.find(row => row.id === id),
    querySelectorAll: selector => selector === '[id]' ? elements : [], querySelector: () => element('query'),
    createElement: element, createDocumentFragment: () => element('#fragment'), createTextNode: text => ({ text }) };
  const fetch = (path, options = {}) => new Promise((resolve, reject) => requests.push({ path, options, reject, resolve(value) {
    resolve({ ok: true, headers: { get() { return 'application/json'; } }, async json() { return value; } });
  } }));
  const json = { ...JSON, stringify(value, ...args) { if (value === measuredReport) reportSerializations++; return JSON.stringify(value, ...args); } };
  const run = new Function('document', 'window', 'localStorage', 'fetch', 'JSON', 'requestAnimationFrame', 'cancelAnimationFrame',
    code.slice(0, code.indexOf('\nawait refreshState();')) + '\nreturn { dom, applyState, refreshState, handleEvent, renderChat, renderData, loadPreview, removeModel,' +
    ' setViewer(value){viewer=value;}, renderFrame(shadows){requestViewerRender(shadows);},' +
    ' setReport(value,url){reportValue=value;reportUrl=artifactUrl(url);}, current(){return project();} };');
  const p = run(document, { addEventListener() {}, location: { origin: 'http://localhost' } }, { getItem() { return ''; }, setItem() {} }, fetch, json,
    callback => { frames.set(++nextFrame, callback); return nextFrame; }, id => frames.delete(id));
  return { ...p, document, requests, frames,
    report(value, url) { measuredReport = value; p.setReport(value, url); }, serializations() { return reportSerializations; },
    frame() { const queued = [...frames.values()]; frames.clear(); queued.forEach(callback => callback()); } };
}
function project(overrides = {}) { return { id: 'p_SYSTEM_VERIFICATION_ONLY', name: 'SYSTEM_VERIFICATION_ONLY', status: 'active', messages: [], events: [], jobs: [], artifacts: {}, ...overrides }; }
const state = current => ({ projects: [current], approvals: [], config: {}, codex: { ready: true } });

test('an SSE snapshot invalidates earlier GET state and its late errors', async () => {
  for (const failure of [false, true]) {
    const p = page(); p.applyState(state(project())); const pending = p.refreshState();
    p.handleEvent({ kind: 'state', state: state(project({ status: 'complete' })) });
    if (failure) p.requests[0].reject(new Error('obsolete query failed'));
    else p.requests[0].resolve(state(project({ status: 'active' })));
    await pending;
    assert.equal(p.current().status, 'complete');
    assert.equal(p.dom.toast.textContent, '', 'an obsolete failure must stay quiet');
  }
});

test('concurrent refreshes share a request and one trailing query for changes during the read', async () => {
  const p = page(); p.applyState(state(project()));
  const first = p.refreshState(), second = p.refreshState(), third = p.refreshState();
  assert.equal(p.requests.length, 1);
  p.requests[0].resolve(state(project({ status: 'waiting_jobs' }))); await settle();
  assert.equal(p.requests.length, 2, 'concurrent callers need only one fresh trailing read');
  p.requests[1].resolve(state(project({ status: 'complete' }))); await Promise.all([first, second, third]);
  assert.equal(p.current().status, 'complete');
});

test('a live job update also invalidates a GET that started before that update', async () => {
  const p = page(), current = project(); p.applyState(state(current));
  const pending = p.refreshState();
  p.handleEvent({ kind: 'job', projectId: current.id, job: { id: 'job1', kind: 'build', status: 'completed' } });
  p.requests[0].resolve(state(project())); await pending;
  assert.equal(p.current().jobs[0]?.status, 'completed');
  p.handleEvent({ kind: 'state', state: state(current) });
});

test('unchanged snapshots retain message, record, delivery and data nodes and reading position', () => {
  const p = page(), current = project({ messages: [{ id: 'm1', role: 'assistant', content: 'first' }],
    events: [{ eventId: 'e1', type: 'codex', method: 'item/completed', params: { item: { id: 'item1', type: 'commandExecution', command: 'fixture' } } }] });
  p.applyState(state(current)); const ids = ['chat-feed', 'event-list', 'job-status', 'delivery-products', 'data-content'];
  const before = Object.fromEntries(ids.map(id => [id, { node: p.dom[id].children[0], replaces: p.dom[id].replaces }]));
  const detail = p.dom['event-list'].querySelector('details'); detail.open = true; detail.querySelector('pre').scrollTop = 17;
  p.dom['panel-progress'].scrollTop = 41; p.dom['chat-feed'].scrollTop = 35;
  p.applyState(structuredClone(state(current)));
  for (const id of ids) { assert.equal(p.dom[id].children[0], before[id].node, id); assert.equal(p.dom[id].replaces, before[id].replaces, id); }
  assert.equal(detail.open, true); assert.equal(p.dom['panel-progress'].scrollTop, 41); assert.equal(p.dom['chat-feed'].scrollTop, 35);
  current.events.push({ eventId: 'e2', type: 'user_message' }); p.applyState(state(current));
  const retained = p.dom['event-list'].querySelector('details');
  assert.equal(retained.open, true, 'new records must preserve the open details'); assert.equal(retained.querySelector('pre').scrollTop, 17);
  assert.equal(p.dom['panel-progress'].scrollTop, 41);
});

test('streaming updates only the active message body and preserves earlier message nodes', () => {
  const p = page(), current = project({ activeTurnId: 't1', messages: [{ id: 'old', role: 'user', content: 'request' }, { id: 'live', role: 'assistant', content: 'hello', status: 'streaming' }] });
  p.applyState(state(current)); const rows = [...p.dom['chat-feed'].children], replaces = p.dom['chat-feed'].replaces;
  p.handleEvent({ kind: 'codex', projectId: current.id, method: 'item/agentMessage/delta', params: { itemId: 'live', delta: ' world' } });
  assert.deepEqual(p.dom['chat-feed'].children, rows); assert.equal(p.dom['chat-feed'].replaces, replaces);
  assert.equal(rows[1].lastElementChild.textContent, 'hello world');
});

test('a full report is formatted once when opened and survives unrelated artifact changes', () => {
  const p = page(), current = project({ artifacts: { report: '/fixture-report.json' } }), report = { status: 'SYSTEM_VERIFICATION_ONLY', samples: Array.from({ length: 100 }, (_, i) => i) };
  p.report(report, current.artifacts.report); p.applyState(state(current)); assert.equal(p.serializations(), 0);
  const detail = p.dom['data-content'].querySelector('details'); detail.open = true; detail.ontoggle();
  assert.equal(p.serializations(), 1); detail.querySelector('pre').scrollTop = 19;
  current.artifacts.manifest = '/fixture-manifest.json'; p.applyState(state(current));
  assert.equal(p.dom['data-content'].querySelector('details'), detail); assert.equal(detail.open, true); assert.equal(detail.querySelector('pre').scrollTop, 19);
  detail.open = false; detail.ontoggle(); detail.open = true; detail.ontoggle(); assert.equal(p.serializations(), 1);
});

function model() {
  const geometry = new THREE.BoxGeometry(), material = new THREE.MeshStandardMaterial(), texture = new THREE.Texture(); material.map = texture;
  const counts = { geometry: 0, material: 0, texture: 0 }; for (const [key, value] of Object.entries({ geometry, material, texture })) value.addEventListener('dispose', () => counts[key]++);
  const scene = new THREE.Group(), mesh = new THREE.Mesh(geometry, material); scene.add(mesh, new THREE.Mesh(geometry, material)); return { scene, mesh, counts };
}
function viewer(loader) { return { THREE, scene: new THREE.Scene(), key: new THREE.DirectionalLight(), fill: new THREE.DirectionalLight(), grid: new THREE.GridHelper(),
  camera: new THREE.PerspectiveCamera(42, 1, .01, 1000), controls: { target: new THREE.Vector3(), update() {} }, loader, model: null, home: null }; }

test('stale and current previews release all shared resources once and remove old visibility references', async () => {
  const p = page(), pending = [], stale = model(), current = model(), v = viewer({ loadAsync() { return new Promise(resolve => pending.push(resolve)); } }); p.setViewer(v);
  const first = p.loadPreview('/old.glb'), second = p.loadPreview('/new.glb'); pending[0]({ scene: stale.scene }); await first;
  assert.deepEqual(stale.counts, { geometry: 1, material: 1, texture: 1 });
  pending[1]({ scene: current.scene }); await second; await p.loadPreview('');
  assert.deepEqual(current.counts, { geometry: 1, material: 1, texture: 1 }); assert.equal(v.originalVisibility.size, 0); assert.deepEqual(v.cutawayIds, []);
});

test('rendering coalesces changes, stops after damping and waits while the document is hidden', () => {
  const p = page(); let renders = 0, updates = 0;
  const v = { renderer: { shadowMap: {}, render() { renders++; } }, controls: { update() { return ++updates < 3; } } }; p.setViewer(v);
  p.renderFrame(true); p.renderFrame(); assert.equal(p.frames.size, 1); assert.equal(v.renderer.shadowMap.needsUpdate, true);
  p.frame(); p.frame(); p.frame(); assert.equal(renders, 3); assert.equal(p.frames.size, 0);
  p.document.hidden = true; p.renderFrame(); assert.equal(p.frames.size, 0);
  p.document.hidden = false; p.renderFrame(); p.frame(); assert.equal(renders, 4); assert.equal(p.frames.size, 0);
});
