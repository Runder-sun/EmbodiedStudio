import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as THREE from 'three';

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const element = id => ({ id, children: [], addEventListener() {}, append(...children) { this.children.push(...children); },
  replaceChildren(...children) { this.children = children; }, setAttribute() {}, getAttribute() { return 'false'; },
  style: { setProperty() {} }, classList: { toggle() {} } });
function page(model, snapshot) {
  const elements = ids.map(element);
  const document = { getElementById: id => elements.find(item => item.id === id),
    querySelectorAll: selector => selector === '[id]' ? elements : [], querySelector: () => ({}),
    createElement: element, createTextNode: text => ({ text }) };
  const scene = new THREE.Scene(), key = new THREE.DirectionalLight(), fill = new THREE.DirectionalLight();
  const viewer = { THREE, scene, key, fill, grid: new THREE.GridHelper(),
    camera: new THREE.PerspectiveCamera(42, 1, .01, 1000),
    controls: { target: new THREE.Vector3(), update() {} },
    loader: { async loadAsync() { return { scene: model }; } }, model: null, home: null };
  const run = new Function('document', 'window', 'localStorage', 'injected',
    code.slice(0, code.indexOf('\nawait refreshState();')) +
    '\nviewer = injected.viewer; sceneSnapshot = injected.snapshot;' +
    '\nreturn { loadPreview, focus(id) { selectedNodeId = id; focusSceneNode(id); }, resetCamera };');
  return { ...run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer, snapshot }),
    viewer, detail: document.getElementById('tree-selection') };
}
function fixture(rotation = false) {
  const model = new THREE.Group(), meshes = {}, nodes = [
    { id: 'building', kind: 'space', owner: null },
    { id: 'floor', kind: 'space', owner: 'building' },
    { id: 'room', kind: 'space', owner: 'floor', at: [1000, 0, 0], bounds: { min: [0, 0, 0], max: [1, 1, 1] } },
  ];
  const root = new THREE.Group(); model.add(root);
  if (rotation) { root.rotation.z = Math.PI / 2; root.position.set(2, 4, -3); }
  function add(id, size, at, extra = {}, owner = 'room', binding = true) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(...size), new THREE.MeshStandardMaterial());
    mesh.name = id === 'cabinet' ? 'roof' : 'unrelated-name';
    mesh.position.set(...at);
    if (binding) mesh.userData.objectId = id;
    root.add(mesh); meshes[id] = mesh;
    nodes.push({ id, kind: 'box', owner, mass: 0, joint: null, ...extra });
    return mesh;
  }
  if (!rotation) {
    add('base', [6, .1, 5], [0, 0, 0], { role: 'enclosure' });
    add('wall', [.1, 3, 5], [-3, 1.5, 0], { role: 'enclosure' });
    add('ceiling', [6, .1, 5], [0, 3, 0], { role: 'enclosure' }, 'floor');
    add('upper', [6, .1, 5], [0, 3.2, 0], { role: 'enclosure' }, 'building');
    add('cabinet', [1, 3, 1], [2, 1.5, 0]);
  } else {
    // Local x maps to world Y: the low plate must remain and the high plate must hide.
    add('base', [.1, 4, 3], [0, 0, 0], { role: 'enclosure' });
    add('wall', [4, .1, 3], [2, 2, 0], { role: 'enclosure' });
    add('ceiling', [.1, 4, 3], [4, 0, 0], { role: 'enclosure' }, 'floor');
  }
  return { model, meshes, nodes, add };
}

test('closed room uses exported geometry and reveals static ancestor ceilings without hiding furniture or robot', async () => {
  const f = fixture();
  f.add('unbound', [1, 1, 1], [1, 1, 0], {}, 'room', false);
  const p = page(f.model, { nodes: f.nodes }); await p.loadPreview('memory.glb');
  const before = f.model.children[0].children.map(mesh => ({ mesh, geometry: mesh.geometry, matrix: mesh.matrix.clone() }));
  p.focus('room');
  for (const id of ['wall', 'ceiling', 'upper']) assert.equal(f.meshes[id].visible, false, id);
  for (const id of ['base', 'cabinet', 'unbound']) assert.equal(f.meshes[id].visible, true, id);
  assert.ok(p.viewer.controls.target.x < 10, 'exported geometry takes precedence over stale source bounds');
  const text = p.detail.children.map(child => child.textContent).join(' ');
  for (const id of ['wall', 'ceiling', 'upper']) assert.ok(text.includes(id), 'actual hidden ID disclosed');
  for (const item of before) { assert.equal(item.mesh.geometry, item.geometry); assert.deepEqual(item.mesh.matrix.elements, item.matrix.elements); }
});

test('whole view restores imported visibility and repeat scope changes do not accumulate hides', async () => {
  const f = fixture();
  f.add('importHidden', [1, 1, 1], [0, 1, 0]).visible = false;
  const p = page(f.model, { nodes: f.nodes }); await p.loadPreview('memory.glb');
  assert.equal(f.meshes.importHidden.visible, false);
  for (let i = 0; i < 2; i++) {
    p.focus('room'); assert.equal(f.meshes.ceiling.visible, false);
    p.focus('cabinet'); assert.equal(f.meshes.ceiling.visible, true);
    p.focus('room'); assert.equal(f.meshes.ceiling.visible, false);
    p.resetCamera(); assert.equal(f.meshes.ceiling.visible, true); assert.equal(f.meshes.importHidden.visible, false);
  }
});

test('Y-up cut height uses actual transformed mesh bounds, including rotated nonnative enclosure', async () => {
  const f = fixture(true); const p = page(f.model, { nodes: f.nodes }); await p.loadPreview('memory.glb');
  p.focus('room'); assert.equal(f.meshes.base.visible, true); assert.equal(f.meshes.ceiling.visible, false);
  assert.ok(p.viewer.controls.target.y >= 4);
});

test('dynamic, jointed, implicit mass, task and unbound geometries cannot qualify as cutaway enclosure', async () => {
  const f = fixture();
  f.add('dynamic', [1, 1, 1], [0, 5, 0], { role: 'enclosure', mass: .1 });
  f.add('jointed', [1, 1, 1], [0, 5, 0], { role: 'enclosure', joint: { type: 'revolute' } });
  f.add('implicitMass', [1, 1, 1], [0, 5, 0], { role: 'enclosure', mass: undefined });
  f.add('task', [1, 1, 1], [0, 5, 0], { role: 'push_target' });
  f.add('unbound', [1, 1, 1], [0, 5, 0], { role: 'enclosure' }, 'room', false);
  const p = page(f.model, { nodes: f.nodes }); await p.loadPreview('memory.glb');
  p.focus('room');
  for (const id of ['dynamic', 'jointed', 'implicitMass', 'task', 'unbound']) assert.equal(f.meshes[id].visible, true, id);
});
