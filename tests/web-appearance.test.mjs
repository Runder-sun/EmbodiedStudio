import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const ids = [...new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1])
  .concat(['camera-view', 'toggle-cutaway', 'cutaway-status']))];
const element = id => ({ id, attributes: {}, addEventListener() {}, append() {}, replaceChildren() {},
  setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name] || 'false'; },
  style: { setProperty() {} }, classList: { toggle() {} } });
const elements = ids.map(element);
const document = { getElementById: id => elements.find(element => element.id === id),
  querySelectorAll: selector => selector === '[id]' ? elements : [], querySelector: () => ({}),
  createElement: element, createTextNode: text => ({ text }) };
// Load the actual page functions and event handlers without starting network/viewer boot.
const run = new Function('document', 'window', 'localStorage', 'injected',
  'const requestAnimationFrame = () => 1; const cancelAnimationFrame = () => {};\n' +
  code.slice(0, code.indexOf('\nawait refreshState();')) +
  '\nviewer = injected.viewer; sceneSnapshot = injected.snapshot;' +
  '\nreturn { loadPreview, geometryBinding, updateLighting, resetCamera, resizeViewer, setCameraView, applyCutaway, removeModel, focusPreviewAtPointer,' +
  ' selected() { return selectedNodeId; }, snapshot(value) { sceneSnapshot = value; focusSceneNode(selectedNodeId); },' +
  ' focus(id) { selectedNodeId = id; focusSceneNode(id); } };');

function viewer(model) {
  const scene = new THREE.Scene();
  const key = new THREE.DirectionalLight();
  const fill = new THREE.DirectionalLight();
  key.shadow.mapSize.set(2048, 2048);
  scene.add(key, key.target, fill, fill.target);
  const camera = new THREE.PerspectiveCamera(42, 1, .01, 1000);
  return { THREE, scene, key, fill, grid: new THREE.GridHelper(), camera,
    controls: { target: new THREE.Vector3(), update() { camera.lookAt(this.target); camera.updateMatrixWorld(true); } },
    loader: { async loadAsync() { return { scene: model }; } }, model: null, home: null };
}

async function appearance(compiled) {
  const material = new THREE.MeshStandardMaterial({ opacity: .4, transparent: true });
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(), material);
  mesh.name = 'chair_visual_0';
  mesh.userData = { objectId: 'chair', appearanceCompiled: compiled === true };
  const scene = new THREE.Scene(); scene.add(mesh);
  scene.userData.appearanceCompiled = compiled === 'parent';
  const page = run(document, { addEventListener() {} }, { getItem() { return ''; } },
    { viewer: viewer(scene), snapshot: { nodes: [{ id: 'chair', color: [1, 1, 1, .5] }] } });
  await page.loadPreview('memory.glb');
  assert.equal(mesh.material, material);
  assert.equal(mesh.receiveShadow, true);
  assert.equal(mesh.castShadow, false, 'transparent material should not cast an opaque shadow');
  return mesh.material;
}

test('loading a preview keeps imported material alpha without a second source tint', async () => {
  for (const compiled of [true, 'parent', false]) assert.equal((await appearance(compiled)).opacity, .4);
});

test('scene filtering binds exported IDs and robot flags without guessing names', () => {
  const snapshot = { nodes: [{ id: 'chair' }] };
  const { geometryBinding } = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { snapshot });
  const bind = object => geometryBinding(object, new Map(snapshot.nodes.map(entry => [entry.id, entry])));
  for (const name of ['chair', 'chair_visual_0', 'robot_1', 'ground']) {
    const object = new THREE.Object3D();
    object.name = name;
    assert.deepEqual(bind(object), {}, name);
  }
  const parent = new THREE.Object3D();
  parent.userData = { objectId: 'chair', usdPrimPath: '/World/Objects/o_6368616972', appearanceCompiled: true };
  const child = new THREE.Object3D(); parent.add(child);
  assert.deepEqual(bind(child), { id: 'chair' });
  parent.userData = { robot: true, usdPrimPath: '/World/Robot', appearanceCompiled: true };
  assert.deepEqual(bind(child), { robot: true });
  parent.userData = { ground: true, usdPrimPath: '/World/Ground', appearanceCompiled: true };
  assert.deepEqual(bind(child), { ground: true });
});

test('desktop and building bounds fit inside the actual shadow camera', () => {
  const setup = viewer(new THREE.Scene());
  const { updateLighting } = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
  for (const [minimum, maximum] of [[[-1, 0, -1], [1, 1.5, 1]], [[-15, 0, -12], [15, 6, 12]]]) {
    updateLighting(new THREE.Box3(new THREE.Vector3(...minimum), new THREE.Vector3(...maximum)));
    setup.scene.updateMatrixWorld(true);
    setup.key.shadow.updateMatrices(setup.key);
    for (const x of [minimum[0], maximum[0]]) {
      for (const y of [minimum[1], maximum[1]]) {
        for (const z of [minimum[2], maximum[2]]) {
          const clip = new THREE.Vector3(x, y, z).project(setup.key.shadow.camera);
          assert.ok(Math.max(Math.abs(clip.x), Math.abs(clip.y), Math.abs(clip.z)) <= 1);
        }
      }
    }
  }
});

test('a simulator ground does not shrink the actual scene in the overview', async () => {
  const model = new THREE.Scene();
  const object = new THREE.Mesh(new THREE.BoxGeometry(2, 1, 2), new THREE.MeshStandardMaterial());
  object.position.y = .5;
  object.userData.objectId = 'table';
  model.add(object);
  const groundRoot = new THREE.Object3D();
  groundRoot.userData.ground = true;
  groundRoot.add(new THREE.Mesh(new THREE.BoxGeometry(20, .02, 20), new THREE.MeshStandardMaterial()));
  model.add(groundRoot);
  const setup = viewer(model);
  const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
  await page.loadPreview('memory.glb');
  assert.equal(setup.home.extent, 2);
  assert.equal(groundRoot.children[0].visible, true);
});

function assertFramed(camera, box) {
  camera.updateMatrixWorld(true);
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    const clip = new THREE.Vector3(x, y, z).project(camera);
    assert.ok(Math.abs(clip.x) <= 1 && Math.abs(clip.y) <= 1 && Math.abs(clip.z) <= 1,
      `scene corner is clipped: ${clip.toArray()}`);
  }
}

test('reset frames actual box corners using the current narrow aspect rather than the load aspect', async () => {
  const model = new THREE.Group();
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(6, 3, 5), new THREE.MeshStandardMaterial());
  model.add(mesh);
  const setup = viewer(model); setup.camera.aspect = 1.7;
  const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
  await page.loadPreview('memory.glb');
  const box = new THREE.Box3().setFromObject(model);
  assertFramed(setup.camera, box);
  setup.camera.aspect = .55; setup.camera.updateProjectionMatrix();
  page.resetCamera();
  assertFramed(setup.camera, box);
});

function rooms() {
  const model = new THREE.Group(), meshes = {}, nodes = [
    { id: 'roomA', kind: 'space', owner: null }, { id: 'roomB', kind: 'space', owner: null },
  ];
  function add(id, at, size, owner, extra = {}) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(...size), new THREE.MeshStandardMaterial());
    mesh.position.set(...at); mesh.userData.objectId = id; model.add(mesh); meshes[id] = mesh;
    nodes.push({ id, kind: 'box', owner, mass: 0, joint: null, ...extra });
    return mesh;
  }
  for (const [id, x] of [['roomA', 0], ['roomB', 14]]) {
    add(id + 'Floor', [x, 0, 0], [6, .1, 5], id, { role: 'enclosure' });
    add(id + 'Roof', [x, 3, 0], [6, .1, 5], id, { role: 'enclosure' });
    add(id + 'Table', [x, .6, 0], [2, 1.2, 1], id);
  }
  return { model, meshes, nodes, add };
}

test('focusing one room never cuts away another rooms proven static enclosure', async () => {
  const fixture = rooms(), setup = viewer(fixture.model);
  const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup, snapshot: { nodes: fixture.nodes } });
  await page.loadPreview('memory.glb');
  page.focus('roomA');
  assert.equal(fixture.meshes.roomARoof.visible, false);
  assert.equal(fixture.meshes.roomBRoof.visible, true);
  assert.deepEqual(setup.cutawayIds, ['roomARoof']);
});

test('all four views frame transformed box corners across wide and narrow aspects with actual OrbitControls', async () => {
  for (const shape of [[6, 3, 5], [10, .1, 2], [.2, 8, .4], [.01, .01, .01]]) {
    const model = new THREE.Group();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(...shape), new THREE.MeshStandardMaterial());
    mesh.position.set(3, 1, -2); mesh.rotation.set(.2, .5, .1); model.add(mesh);
    const setup = viewer(model); setup.controls = new OrbitControls(setup.camera, null); setup.controls.enableDamping = true;
    const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
    await page.loadPreview('memory.glb');
    for (const aspect of [.15, .55, 1, 2.4, 6]) for (const view of ['perspective', 'front', 'top', 'side']) {
      setup.camera.aspect = aspect;
      page.setCameraView(view);
      assertFramed(setup.camera, setup.home.box);
      assert.ok(setup.camera.position.toArray().every(Number.isFinite));
      assert.equal(setup.controls.enableDamping, true);
      assert.equal(document.getElementById('camera-view').value, view);
    }
  }
});

test('automatic resize refits while manual navigation keeps position and target', async () => {
  const model = new THREE.Group(); model.add(new THREE.Mesh(new THREE.BoxGeometry(6, 3, 5), new THREE.MeshStandardMaterial()));
  const setup = viewer(model), canvas = { clientWidth: 1200, clientHeight: 700 };
  let pixelRatio;
  setup.renderer = { domElement: canvas, setSize() {}, setPixelRatio(value) { pixelRatio = value; }, shadowMap: {} };
  const page = run(document, { devicePixelRatio: 3, addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
  await page.loadPreview('memory.glb');
  canvas.clientWidth = 330; canvas.clientHeight = 600; page.resizeViewer();
  assertFramed(setup.camera, setup.home.box); assert.equal(pixelRatio, 2);
  setup.autoFrame = false; setup.camera.position.set(7, 5, 11); setup.controls.target.set(1, .2, -2);
  const position = setup.camera.position.clone(), target = setup.controls.target.clone();
  canvas.clientWidth = 1400; canvas.clientHeight = 400; page.resizeViewer();
  assert.deepEqual(setup.camera.position, position); assert.deepEqual(setup.controls.target, target);
  assert.equal(setup.camera.aspect, 3.5);
});

function singleRoom() {
  const fixture = rooms();
  fixture.nodes = fixture.nodes.filter(entry => !entry.id.startsWith('roomB'));
  for (const [id, mesh] of Object.entries(fixture.meshes)) if (id.startsWith('roomB')) fixture.model.remove(mesh);
  return fixture;
}

test('unique room defaults to disclosed reversible cutaway and reset restores every original visibility', async () => {
  const fixture = singleRoom();
  const hidden = fixture.add('originalHidden', [0, 4, 0], [1, 1, 1], 'roomA', { role: 'enclosure' }); hidden.visible = false;
  const setup = viewer(fixture.model), page = run(document, { addEventListener() {} }, { getItem() { return ''; } },
    { viewer: setup, snapshot: { nodes: fixture.nodes } });
  await page.loadPreview('memory.glb');
  assert.equal(fixture.meshes.roomARoof.visible, false);
  assert.equal(document.getElementById('toggle-cutaway').getAttribute('aria-pressed'), 'true');
  assert.equal(document.getElementById('cutaway-status').hidden, false);
  assert.equal(document.getElementById('cutaway-status').textContent, '剖视预览');
  page.setCameraView('top'); assert.equal(fixture.meshes.roomARoof.visible, false);
  page.applyCutaway(false, 'roomA');
  assert.equal(fixture.meshes.roomARoof.visible, true); assert.equal(hidden.visible, false);
  page.applyCutaway(true, 'roomA'); page.resetCamera();
  assert.equal(setup.cutawayEnabled, false); assert.equal(fixture.meshes.roomARoof.visible, true);
  assert.equal(hidden.visible, false); assert.equal(document.getElementById('cutaway-status').hidden, true);
  assert.equal(document.getElementById('toggle-cutaway').getAttribute('aria-pressed'), 'false');
});

test('multiple or invalid space roots never default to cutaway and model replacement clears the old mode', async () => {
  const first = singleRoom(), second = rooms(), setup = viewer(first.model);
  let current = first.model;
  setup.loader.loadAsync = async () => ({ scene: current });
  const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup, snapshot: { nodes: first.nodes } });
  await page.loadPreview('first.glb'); assert.equal(setup.cutawayEnabled, true);
  current = second.model; page.snapshot({ nodes: second.nodes });
  await page.loadPreview('second.glb');
  assert.equal(setup.cutawayEnabled, false);
  assert.equal(second.meshes.roomARoof.visible, true); assert.equal(second.meshes.roomBRoof.visible, true);
  page.snapshot({ nodes: [...second.nodes.filter(entry => !entry.id.startsWith('roomB')),
    { id: 'orphan', kind: 'space', owner: 'missing', frame: 'roomA' }] });
  await page.loadPreview('invalid.glb'); assert.equal(setup.cutawayEnabled, false);
  page.removeModel();
  assert.equal(setup.cutawayEnabled, false); assert.deepEqual(setup.cutawayIds, []);
  assert.equal(document.getElementById('toggle-cutaway').disabled, true);
});

test('late scene metadata applies one default only and never overrides earlier manual navigation', async () => {
  for (const manual of [false, true]) {
    const fixture = singleRoom(), setup = viewer(fixture.model);
    const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
    await page.loadPreview('memory.glb');
    const position = setup.camera.position.clone();
    if (manual) setup.autoFrame = false;
    page.snapshot({ nodes: fixture.nodes });
    assert.equal(setup.cutawayEnabled, !manual); assert.deepEqual(setup.camera.position, position);
    page.applyCutaway(false, 'roomA'); page.snapshot({ nodes: fixture.nodes });
    assert.equal(setup.cutawayEnabled, false);
  }
});

test('cutaway follows ownership rather than frame, keeps unknown, dynamic and jointed meshes, and includes relevant ancestor roof', async () => {
  const fixture = rooms();
  fixture.nodes.unshift({ id: 'building', kind: 'space', owner: null });
  fixture.nodes.find(entry => entry.id === 'roomA').owner = 'building';
  fixture.nodes.find(entry => entry.id === 'roomB').owner = 'building';
  fixture.nodes.find(entry => entry.id === 'roomARoof').frame = 'roomB';
  fixture.nodes.find(entry => entry.id === 'roomBRoof').frame = 'roomA';
  fixture.add('ancestorRoof', [0, 3.3, 0], [6, .1, 5], 'building', { role: 'enclosure' });
  const cases = { dynamic: { mass: .1 }, jointed: { joint: { type: 'revolute' } }, unknownMass: { mass: undefined } };
  for (const [id, extra] of Object.entries(cases)) fixture.add(id, [0, 4, 0], [1, 1, 1], 'roomA', { role: 'enclosure', ...extra });
  const unbound = fixture.add('unbound', [0, 4, 0], [1, 1, 1], 'roomA', { role: 'enclosure' });
  unbound.userData = {}; unbound.name = 'roomARoof';
  const setup = viewer(fixture.model), page = run(document, { addEventListener() {} }, { getItem() { return ''; } },
    { viewer: setup, snapshot: { nodes: fixture.nodes } });
  await page.loadPreview('memory.glb'); page.focus('roomA');
  assert.equal(fixture.meshes.roomARoof.visible, false); assert.equal(fixture.meshes.roomBRoof.visible, true);
  assert.equal(fixture.meshes.ancestorRoof.visible, false);
  for (const id of [...Object.keys(cases), 'unbound']) assert.equal(fixture.meshes[id].visible, true, id);
});

test('double click uses actual visible exported binding and refuses an unbound front hit despite its name', async () => {
  for (const bound of [true, false]) {
    const model = new THREE.Group();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial());
    mesh.userData = bound ? { objectId: 'actual' } : {}; mesh.name = 'actual'; model.add(mesh);
    const setup = viewer(model);
    setup.renderer = { domElement: { getBoundingClientRect() { return { left: 10, top: 20, width: 400, height: 400 }; } }, shadowMap: {} };
    const page = run(document, { addEventListener() {} }, { getItem() { return ''; } },
      { viewer: setup, snapshot: { nodes: [{ id: 'actual', kind: 'box', owner: null }] } });
    await page.loadPreview('memory.glb'); page.setCameraView('front');
    let prevented = false;
    page.focusPreviewAtPointer({ clientX: 210, clientY: 220, preventDefault() { prevented = true; } });
    assert.equal(page.selected(), bound ? 'actual' : ''); assert.equal(prevented, bound);
  }
});

test('texture filtering is hardware capped without replacing or tinting imported PBR materials', async () => {
  for (const maximum of [4, 32]) {
    const model = new THREE.Group(), texture = new THREE.Texture();
    const material = new THREE.MeshStandardMaterial({ color: '#7865b0', roughness: .62, metalness: .31, opacity: .4, transparent: true, map: texture });
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(), material); model.add(mesh);
    const color = material.color.clone(), setup = viewer(model);
    setup.renderer = { capabilities: { getMaxAnisotropy() { return maximum; } }, shadowMap: {} };
    const page = run(document, { addEventListener() {} }, { getItem() { return ''; } }, { viewer: setup });
    await page.loadPreview('memory.glb');
    assert.equal(mesh.material, material); assert.equal(material.map, texture); assert.deepEqual(material.color, color);
    assert.equal(material.roughness, .62); assert.equal(material.metalness, .31); assert.equal(material.opacity, .4);
    assert.equal(texture.anisotropy, Math.min(maximum, 8));
  }
});

test('inherited imported invisibility neither expands framing nor qualifies a hidden enclosure as a default room', async () => {
  const model = new THREE.Group();
  model.add(new THREE.Mesh(new THREE.BoxGeometry(2, 1, 2), new THREE.MeshStandardMaterial()));
  const hidden = new THREE.Group(); hidden.visible = false;
  const roof = new THREE.Mesh(new THREE.BoxGeometry(80, 1, 80), new THREE.MeshStandardMaterial());
  roof.position.set(50, 10, 0); roof.userData.objectId = 'hiddenRoof'; hidden.add(roof); model.add(hidden);
  const setup = viewer(model), page = run(document, { addEventListener() {} }, { getItem() { return ''; } },
    { viewer: setup, snapshot: { nodes: [{ id: 'room', kind: 'space', owner: null },
      { id: 'hiddenRoof', kind: 'box', owner: 'room', role: 'enclosure', mass: 0, joint: null }] } });
  await page.loadPreview('memory.glb');
  assert.equal(setup.home.extent, 2); assert.equal(setup.cutawayEnabled, false);
  page.focus('room'); page.resetCamera();
  assert.equal(hidden.visible, false); assert.equal(roof.visible, true);
  assert.deepEqual(setup.cutawayIds, []);
});


test('top preset keeps real OrbitControls spherical basis consistent with camera up',async()=>{
 const model=new THREE.Scene();const mesh=new THREE.Mesh(new THREE.BoxGeometry(4,2,3),new THREE.MeshStandardMaterial());mesh.userData={objectId:'box'};model.add(mesh);
 const v=viewer(model);v.controls=new OrbitControls(v.camera,null);
 const page=run(document,{addEventListener(){}},{getItem(){return '';},setItem(){}},{viewer:v,snapshot:{nodes:[{id:'box'}]}});
 await page.loadPreview('memory.glb');page.setCameraView('top');
 const geometric=v.camera.position.clone().sub(v.controls.target).angleTo(v.camera.up);
 assert.ok(Math.abs(v.controls.getPolarAngle()-geometric)<1e-5,`controls polar=${v.controls.getPolarAngle()}, actual angle to up=${geometric}`);
});

test('real orbit and pan after top view stay consistent and reset frames all corners',async()=>{
 const model=new THREE.Group();model.add(new THREE.Mesh(new THREE.BoxGeometry(4,2,3),new THREE.MeshStandardMaterial()));
 const setup=viewer(model),keys=new Map();
 const root={addEventListener(){},removeEventListener(){}};
 const canvas={clientWidth:900,clientHeight:500,style:{},ownerDocument:root,getRootNode(){return root;},addEventListener(){},removeEventListener(){}};
 const keyTarget={addEventListener(name,handler){keys.set(name,handler);},removeEventListener(name){keys.delete(name);}};
 setup.controls=new OrbitControls(setup.camera,canvas);setup.controls.listenToKeyEvents(keyTarget);
 const page=run(document,{addEventListener(){}},{getItem(){return '';},setItem(){}},{viewer:setup});
 try {
  await page.loadPreview('memory.glb');page.setCameraView('top');
  keys.get('keydown')({code:'ArrowDown',ctrlKey:true,preventDefault(){}});
  const geometric=setup.camera.position.clone().sub(setup.controls.target).angleTo(setup.camera.up);
  assert.ok(geometric>.001);assert.ok(Math.abs(setup.controls.getPolarAngle()-geometric)<1e-5);
  const previous=setup.controls.target.clone();keys.get('keydown')({code:'ArrowRight',preventDefault(){}});
  assert.ok(setup.controls.target.distanceTo(previous)>0,'the real pan handler changes the target');
  page.resetCamera();assertFramed(setup.camera,setup.home.box);
  assert.ok(setup.controls.target.distanceTo(setup.home.box.getCenter(new THREE.Vector3()))<1e-8);
 } finally {setup.controls.dispose();}
});
