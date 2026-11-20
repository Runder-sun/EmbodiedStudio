// SYSTEM_VERIFICATION_ONLY: isolated DOM/Three fixtures for the workbench page; never formal scene products.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as THREE from 'three';

const code = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const analysisCode = await readFile(new URL('../web/analysis.js', import.meta.url), 'utf8');
const ids = [...new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]))];

function classList() {
  const names = new Set();
  return { names, contains: name => names.has(name),
    toggle(name, force) { const on = force === undefined ? !names.has(name) : !!force; on ? names.add(name) : names.delete(name); return on; },
    add(name) { names.add(name); }, remove(name) { names.delete(name); } };
}
function element(id) {
  return { id, children: [], attributes: {}, dataset: {}, hidden: false, value: '', textContent: '', className: '', classList: classList(),
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    style: { setProperty() {} }, addEventListener() {}, append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }, querySelectorAll() { return []; },
    get options() { return this.children; }, pause() {}, load() {}, focus() {}, select() {}, removeAttribute(name) { delete this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name] ?? 'false'; } };
}
function page({ model, snapshot, savedPrefs = {}, extra = {} } = {}) {
  const elements = ids.map(element);
  const body = { classList: classList() };
  const themeMeta = { content: '#f5f3ec', attributes: { name: 'theme-color', content: '#f5f3ec' },
    setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'content') this.content = String(value); } };
  const storage = new Map(Object.entries(savedPrefs));
  const document = { body, getElementById: id => elements.find(item => item.id === id), createElement: () => element('created'),
    createTextNode: text => ({ text }), querySelectorAll: selector => selector === '[id]' ? elements : [],
    querySelector: selector => selector === 'meta[name="theme-color"]' ? themeMeta : {} };
  const window = { addEventListener() {}, location: { origin: 'http://localhost', hash: '' }, innerWidth: 1440 };
  new Function('window', analysisCode)(window);
  const scene = new THREE.Scene(), key = new THREE.DirectionalLight(), fill = new THREE.DirectionalLight();
  scene.add(key, key.target, fill, fill.target);
  const camera = new THREE.PerspectiveCamera(42, 1, .01, 1000);
  const viewer = { THREE, scene, key, fill, grid: new THREE.GridHelper(), camera,
    controls: { target: new THREE.Vector3(), update() {} }, renderer: { clippingPlanes: [], shadowMap: {}, domElement: {} },
    loader: { async loadAsync() { return { scene: model }; } }, model: null, home: null, ...extra };
  const run = new Function('document', 'window', 'localStorage', 'injected',
    'const requestAnimationFrame = () => 1; const cancelAnimationFrame = () => {};\n' +
    code.slice(0, code.indexOf('\nawait refreshState();')) +
    '\nviewer = injected.viewer; sceneSnapshot = injected.snapshot;' +
    '\nreturn { loadPreview, reframeContent, setSpatialMode, selectNodeFromViewport, setNodeHidden, isolateNode, showAllNodes, treeMatches,' +
    ' syncSpatialBlueprint, shareHash, applyShare, buildReplayOverlay, applyReplayTime, measureAdd, measureClear, setSection, displayEvent, applyCutaway, applySpatialAtmosphere,' +
    ' applyState, setWelcomeDraft, previewBlueprintPreset,' +
    ' dom, selected() { return selectedNodeId; }, expanded() { return expandedNodes; }, snapshot(value) { sceneSnapshot = value; } };');
  const p = run(document, window, {
    getItem(key) { return storage.get(key) ?? ''; },
    setItem(key, value) { storage.set(key, String(value)); },
  }, { viewer, snapshot });
  return { ...p, viewer, body, elements, window, storage, themeMeta, byId: id => elements.find(item => item.id === id) };
}
function mesh(id, size, at, bound = true) {
  const item = new THREE.Mesh(new THREE.BoxGeometry(...size), new THREE.MeshStandardMaterial());
  item.position.set(...at);
  if (bound) item.userData.objectId = id;
  return item;
}
function fixture() {
  const model = new THREE.Group();
  const a = mesh('a', [1, 1, 1], [0, .5, 0]), b = mesh('b', [1, 1, 1], [3, .5, 0]), slab = mesh('', [20, .2, 20], [0, -.1, 0], false);
  model.add(a, b, slab);
  const nodes = [{ id: 'room', kind: 'space', owner: null }, { id: 'a', kind: 'box', owner: 'room', mass: .1 },
    { id: 'b', kind: 'box', owner: 'room', mass: 0, purpose: '实验台' }];
  return { model, a, b, slab, nodes };
}

test('immersive and chat-wing modes are mutually exclusive and restore the previous state', () => {
  const p = page();
  const wing = p.byId('toggle-chat-wing'), immersive = p.byId('toggle-immersive');
  assert.equal(typeof wing.onclick, 'function');
  immersive.onclick();
  assert.equal(immersive.getAttribute('aria-pressed'), 'true');
  assert.equal(wing.getAttribute('aria-pressed'), 'false', 'immersive hides the chat wing, so it cannot look pressed');
  assert.ok(p.body.classList.contains('is-immersive-3d'));
  immersive.onclick();
  assert.equal(immersive.getAttribute('aria-pressed'), 'false');
  assert.equal(wing.getAttribute('aria-pressed'), 'true');
  wing.onclick();
  assert.equal(wing.getAttribute('aria-pressed'), 'false');
  assert.ok(p.body.classList.contains('chat-wing-collapsed'));
  immersive.onclick();
  wing.onclick();
  assert.equal(immersive.getAttribute('aria-pressed'), 'false', 'opening the chat wing leaves immersive mode');
  assert.equal(wing.getAttribute('aria-pressed'), 'true');
  assert.ok(!p.body.classList.contains('is-immersive-3d') && !p.body.classList.contains('chat-wing-collapsed'));
});

test('framing uses the bound scene content, so an unbound ground slab cannot shrink the room', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  assert.ok(p.viewer.home.extent < 6, `extent ${p.viewer.home.extent}`);
  assert.ok(p.viewer.home.box.max.x < 5);
});

test('a scene snapshot that arrives after the model reframes it once, unless the user already moved the camera', async () => {
  const f = fixture();
  const p = page({ model: f.model });
  await p.loadPreview('memory.glb');
  assert.ok(p.viewer.home.extent > 15, 'without a snapshot nothing can be told apart yet');
  p.snapshot({ nodes: f.nodes });
  p.reframeContent();
  assert.ok(p.viewer.home.extent < 6);
  const moved = page({ model: fixture().model });
  await moved.loadPreview('memory.glb');
  moved.viewer.autoFrame = false;
  const before = moved.viewer.camera.position.clone();
  moved.snapshot({ nodes: f.nodes });
  moved.reframeContent();
  assert.ok(moved.viewer.camera.position.equals(before), 'a user-chosen camera is never overwritten');
  assert.ok(moved.viewer.home.extent < 6, 'the home frame itself is still corrected');
});

test('clicking a mesh selects its scene node, expands its ancestors and keeps the camera still', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  const before = p.viewer.camera.position.clone();
  p.selectNodeFromViewport('a');
  assert.equal(p.selected(), 'a');
  assert.ok(p.expanded().has('room'));
  assert.ok(p.viewer.camera.position.equals(before));
  assert.ok(p.viewer.selectionEdges?.children.length > 0, 'selected geometry gets a visible edge highlight');
  p.selectNodeFromViewport('');
  assert.equal(p.selected(), '');
  assert.equal(p.viewer.selectionEdges.children.length, 0);
});

test('hide, isolate and show-all work on scene nodes and survive cutaway changes', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  p.setNodeHidden('a', true);
  assert.equal(f.a.visible, false); assert.equal(f.b.visible, true);
  p.applyCutaway(false, '');
  assert.equal(f.a.visible, false, 'toggling cutaway must not bring a hidden object back');
  p.showAllNodes();
  assert.equal(f.a.visible, true);
  p.isolateNode('a');
  assert.equal(f.a.visible, true); assert.equal(f.b.visible, false);
  p.showAllNodes();
  assert.equal(f.b.visible, true);
  p.setNodeHidden('room', true);
  assert.equal(f.a.visible, false, 'hiding a space hides everything inside it');
  assert.equal(f.b.visible, false);
});

test('tree search matches ids and purposes and the kind filter uses recorded mass and role', () => {
  const p = page({ snapshot: { nodes: [
    { id: 'west_wall', kind: 'box', role: 'enclosure', mass: 0, purpose: '西墙' },
    { id: 'cup', kind: 'box', mass: .1, purpose: '' }, { id: 'room', kind: 'space' }] } });
  assert.deepEqual(p.treeMatches('', '').map(row => row.id), ['west_wall', 'cup', 'room']);
  assert.deepEqual(p.treeMatches('WALL', '').map(row => row.id), ['west_wall']);
  assert.deepEqual(p.treeMatches('西', '').map(row => row.id), ['west_wall']);
  assert.deepEqual(p.treeMatches('', 'movable').map(row => row.id), ['cup']);
  assert.deepEqual(p.treeMatches('', 'space').map(row => row.id), ['room']);
  assert.deepEqual(p.treeMatches('', 'enclosure').map(row => row.id), ['west_wall']);
  assert.deepEqual(p.treeMatches('nothing', ''), []);
});

test('the empty-space sketch is labelled as a sketch and never shows guessed metres', () => {
  const p = page({ extra: { blueprintGroup: new THREE.Group(), targetScale: new THREE.Vector3(),
    blueprintLabel: { title: { textContent: '' }, btn: { classList: { toggle() {} } } } } });
  p.syncSpatialBlueprint({ name: '厨房岛台', prompt: '厨房桌面任务' }, false);
  assert.match(p.viewer.blueprintInfo.title, /示意/);
  assert.doesNotMatch(p.viewer.blueprintInfo.title, /\d+\.\d×/);
  assert.equal(p.viewer.blueprintInfo.scaleLabel, '');
});

test('share links carry project, camera, selection and view, and restore the camera when applied', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  p.viewer.camera.position.set(4, 3, 2); p.viewer.controls.target.set(0, .5, 0);
  p.selectNodeFromViewport('a');
  const hash = p.shareHash();
  assert.match(hash, /^#p=.*&c=4,3,2&t=0,0\.5,0&n=a&v=perspective$|^#c=4,3,2&t=0,0\.5,0&n=a&v=perspective$/);
  p.viewer.camera.position.set(9, 9, 9);
  p.applyShare({ position: [1, 2, 3], target: [0, 0, 0], node: 'b' });
  assert.deepEqual(p.viewer.camera.position.toArray(), [1, 2, 3]);
  assert.equal(p.selected(), 'b');
});

test('replay overlay draws recorded paths and moves only bound, recorded objects by their recorded displacement', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  const track = { times: [0, .1], duration: .1, ee: [[0, 0, 1], [1, 0, 1]], target: [[0, 0, 0], [0, 1, 0]],
    contacts: [[], ['a']], objects: { a: [[0, 0, .5], [0, .4, .5]], ghost: [[0, 0, 0], [5, 5, 5]] } };
  p.buildReplayOverlay(track);
  const overlay = p.viewer.replay;
  assert.ok(overlay.group.parent === p.viewer.scene);
  const start = f.a.getWorldPosition(new THREE.Vector3());
  p.applyReplayTime(.1);
  assert.deepEqual(overlay.marker.position.toArray(), [1, 1, -0]);
  const end = f.a.getWorldPosition(new THREE.Vector3());
  assert.ok(Math.abs(end.x - start.x) < 1e-9 && Math.abs(end.y - start.y) < 1e-9 && Math.abs(end.z - start.z + .4) < 1e-9,
    'a recorded +0.4 m in simulation y is -0.4 m in viewer z');
  assert.ok(overlay.contactMarkers.visible, 'the recorded contact is shown at the contacted object');
  p.applyReplayTime(0);
  const again = f.a.getWorldPosition(new THREE.Vector3());
  assert.ok(again.distanceTo(start) < 1e-9, 'scrubbing back restores the original pose');
  assert.equal(overlay.contactMarkers.visible, false);
});

test('measuring two points reports their real distance and clearing removes the marks', () => {
  const p = page();
  assert.equal(p.measureAdd(new THREE.Vector3(0, 0, 0)), null);
  const distance = p.measureAdd(new THREE.Vector3(0, 0, .5));
  assert.equal(distance, .5);
  assert.equal(p.viewer.measure.readout, '0.50 m');
  p.measureAdd(new THREE.Vector3(1, 1, 1));
  assert.equal(p.viewer.measure.points.length, 1, 'a third click starts a new measurement');
  p.measureClear();
  assert.equal(p.viewer.measure.points.length, 0);
});

test('section planes clip from the top by a fraction of the content height and can be removed', async () => {
  const f = fixture();
  const p = page({ model: f.model, snapshot: { nodes: f.nodes } });
  await p.loadPreview('memory.glb');
  const { min, max } = p.viewer.home.box;
  p.setSection(.5);
  const [plane] = p.viewer.renderer.clippingPlanes;
  assert.deepEqual(plane.normal.toArray(), [0, -1, 0]);
  assert.ok(Math.abs(plane.constant - (min.y + (max.y - min.y) / 2)) < 1e-9);
  p.setSection(null);
  assert.equal(p.viewer.renderer.clippingPlanes.length, 0);
});

test('generation log shows plain labels for known remote keys', () => {
  const p = page();
  assert.equal(p.displayEvent({ type: 'delivery_receipt' }).message, '交付回执已记录');
  assert.equal(p.displayEvent({ type: 'tool_call', name: 'scene_observe' }).message, '调用工具 · 场景观察');
  assert.equal(p.displayEvent({ type: 'brand_new' }).message, 'brand_new');
});

test('replay moves bound Franka robot link nodes from recorded jointPosition and restores them on reset', async () => {
  const model = new THREE.Group();
  const franka = new THREE.Group(); franka.name = 'franka'; model.add(franka);
  const linkNames = ['panda_link0', 'panda_link1', 'panda_link2', 'panda_link3', 'panda_link4',
    'panda_link5', 'panda_link6', 'panda_link7', 'panda_hand', 'panda_leftfinger', 'panda_rightfinger'];
  const links = {};
  for (const name of linkNames) {
    const node = new THREE.Group(); node.name = name;
    const inner = new THREE.Mesh(new THREE.BoxGeometry(.1, .1, .1), new THREE.MeshStandardMaterial());
    inner.name = name; node.add(inner); franka.add(node); links[name] = node;
  }
  const p = page({ model, snapshot: { nodes: [] } });
  await p.loadPreview('memory.glb');
  const q0 = [0, -Math.PI / 4, 0, -3 * Math.PI / 4, 0, Math.PI / 2, Math.PI / 4, .04, .04];
  const q1 = [1.0, -Math.PI / 4, 0, -3 * Math.PI / 4, 0, Math.PI / 2, Math.PI / 4, .01, .01];
  const track = { times: [0, .1], duration: .1, ee: [[.3, 0, 1.2], [.2, .3, 1.2]], target: [null, null],
    joints: [q0, q1], contacts: [[], []], objects: {} };
  p.buildReplayOverlay(track);
  assert.equal(p.viewer.replay.robotLinks.size, 11);
  p.applyReplayTime(.1);
  assert.ok(links.panda_hand.position.y > .15, 'rotating joint1 by +1 rad moves the hand into +y in Franka base frame');
  p.applyReplayTime(0);
  assert.ok(Math.abs(links.panda_hand.position.y) < 1e-6);
});

test('studio visual system defines editorial typography tokens, studio palettes and dark theme atmosphere', async () => {
  const css = await readFile(new URL('../web/styles.css', import.meta.url), 'utf8');
  for (const token of ['--font-sans:', '--font-serif:', '--font-mono:', '#c96442', '#f5f3ec', '#111110', '#d97757']) {
    assert.ok(css.includes(token), `expected styles.css to include ${token}`);
  }
  const p = page();
  p.applySpatialAtmosphere(true);
  assert.equal(p.viewer.scene.background.getHexString(), '111110');
  assert.ok(p.viewer.scene.environmentIntensity < 0.5);
  p.applySpatialAtmosphere(false);
  assert.equal(p.viewer.scene.background.getHexString(), 'f5f3ec');
  assert.equal(p.viewer.scene.environmentIntensity, 1);
});

test('chat-wing inline collapse, floating expand and exit-immersive controls stay synchronized with spatial modes', () => {
  const p = page();
  const wing = p.byId('toggle-chat-wing');
  const immersive = p.byId('toggle-immersive');
  const collapseBtn = p.byId('collapse-chat-wing');
  const expandBtn = p.byId('expand-chat-wing');
  const exitImmersiveBtn = p.byId('exit-immersive');
  assert.ok(collapseBtn && expandBtn && exitImmersiveBtn, 'new layout control elements should exist in index.html');
  assert.equal(typeof collapseBtn.onclick, 'function');
  assert.equal(typeof expandBtn.onclick, 'function');
  assert.equal(typeof exitImmersiveBtn.onclick, 'function');

  collapseBtn.onclick();
  assert.ok(p.body.classList.contains('chat-wing-collapsed'));
  assert.equal(wing.getAttribute('aria-pressed'), 'false');
  assert.equal(expandBtn.hidden, false);

  expandBtn.onclick();
  assert.ok(!p.body.classList.contains('chat-wing-collapsed'));
  assert.equal(wing.getAttribute('aria-pressed'), 'true');
  assert.equal(expandBtn.hidden, true);

  immersive.onclick();
  assert.ok(p.body.classList.contains('is-immersive-3d'));
  assert.equal(exitImmersiveBtn.hidden, false);
  assert.equal(expandBtn.hidden, true);

  exitImmersiveBtn.onclick();
  assert.ok(!p.body.classList.contains('is-immersive-3d'));
  assert.equal(exitImmersiveBtn.hidden, true);
  assert.equal(wing.getAttribute('aria-pressed'), 'true');
});

test('day/night studio atmosphere switch syncs topbar toggle, viewport pill, theme-color meta, labels and localStorage', () => {
  const p = page();
  const topbarTheme = p.byId('topbar-theme-toggle');
  const topbarLabel = p.byId('topbar-theme-label');
  const viewportTheme = p.byId('toggle-atmosphere');
  const viewportLabel = p.byId('atmosphere-label');
  assert.ok(topbarTheme && topbarLabel && viewportTheme && viewportLabel, 'theme toggle elements should exist in index.html');
  assert.equal(typeof topbarTheme.onclick, 'function');

  topbarTheme.onclick();
  assert.ok(p.body.classList.contains('theme-night'));
  assert.equal(topbarTheme.getAttribute('aria-pressed'), 'true');
  assert.equal(viewportTheme.getAttribute('aria-pressed'), 'true');
  assert.equal(topbarLabel.textContent, '暖夜');
  assert.equal(viewportLabel.textContent, '暖夜 · 已开启');
  assert.equal(p.themeMeta.attributes.content, '#111110');
  assert.equal(p.storage.get('sceneagent.night'), '1');

  viewportTheme.onclick();
  assert.ok(!p.body.classList.contains('theme-night'));
  assert.equal(topbarTheme.getAttribute('aria-pressed'), 'false');
  assert.equal(viewportTheme.getAttribute('aria-pressed'), 'false');
  assert.equal(topbarLabel.textContent, '白昼');
  assert.equal(viewportLabel.textContent, '暖夜');
  assert.equal(p.themeMeta.attributes.content, '#f5f3ec');
  assert.equal(p.storage.get('sceneagent.night'), '0');
});

test('welcome start stage activates for empty workspaces and toggles cleanly with existing loaded scenes', async () => {
  const f = fixture();
  const ambientStage = new THREE.Group();
  ambientStage.visible = true;
  const p = page({ model: f.model, snapshot: { nodes: f.nodes }, extra: { ambientStage, blueprintGroup: new THREE.Group(), targetScale: new THREE.Vector3(2.2, 1.4, 1.9) } });
  const startBtn = p.byId('start-new-scene');
  const returnBtn = p.byId('welcome-return-btn');
  const recentWrap = p.byId('welcome-recent-projects');
  assert.ok(startBtn && returnBtn && recentWrap, 'welcome stage navigation controls should exist in index.html');

  p.applyState({ codex: { ready: true }, config: {}, projects: [] });
  assert.ok(p.body.classList.contains('is-welcome-stage'), 'empty workspace opens in native-3D welcome start stage');
  assert.equal(returnBtn.hidden, true, 'no return button when no projects exist');

  p.applyState({
    codex: { ready: true }, config: {},
    projects: [{ id: 'p1', name: '厨房测试', status: 'complete', prompt: '做个厨房',
      messages: [{ id: 'm1', role: 'user', content: '做个厨房' }], jobs: [], artifacts: {} }],
  });
  await p.loadPreview('memory.glb');
  assert.ok(!p.body.classList.contains('is-welcome-stage'), 'populated project enters split workspace');
  assert.equal(p.viewer.model.visible, true);
  assert.equal(ambientStage.visible, false);

  startBtn.onclick();
  assert.ok(p.body.classList.contains('is-welcome-stage'), 'start-new-scene opens welcome start stage');
  assert.equal(p.viewer.model.visible, false, 'loaded model pauses visibility while previewing welcome diorama');
  assert.equal(ambientStage.visible, true, 'native-3D welcome diorama becomes visible');
  assert.equal(returnBtn.hidden, false);
  assert.equal(recentWrap.children.length, 1);

  returnBtn.onclick();
  assert.ok(!p.body.classList.contains('is-welcome-stage'));
  assert.equal(p.viewer.model.visible, true, 'returning restores loaded model without reloading');
  assert.equal(ambientStage.visible, false);
});

test('previewBlueprintPreset switches 3D diorama presets while keeping sketch labelling honest', () => {
  const ambientStage = new THREE.Group();
  ambientStage.visible = true;
  const vignettes = { kitchen: new THREE.Group(), desk: new THREE.Group(), living: new THREE.Group(), warehouse: new THREE.Group() };
  const p = page({ extra: { ambientStage, blueprintGroup: new THREE.Group(), targetScale: new THREE.Vector3(), vignettes,
    blueprintLabel: { title: { textContent: '' }, btn: { classList: { toggle() {} } } } } });
  p.previewBlueprintPreset('living');
  assert.equal(p.viewer.activePreset, 'living');
  assert.equal(vignettes.living.visible, true);
  assert.equal(vignettes.kitchen.visible, false);
  assert.ok(p.viewer.targetScale.x > 2.5, 'living room preset expands volume scale');
  p.previewBlueprintPreset('kitchen');
  assert.equal(p.viewer.activePreset, 'kitchen');
  assert.equal(vignettes.kitchen.visible, true);
  assert.equal(vignettes.living.visible, false);
  assert.ok(p.viewer.targetScale.x < 2.0, 'kitchen tabletop preset tightens volume scale');
});
