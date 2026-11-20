// SYSTEM_VERIFICATION_ONLY: isolated synthetic records for the page helper module; never formal scene products.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const code = await readFile(new URL('../web/analysis.js', import.meta.url), 'utf8');
const window = {};
new Function('window', code)(window); // same realm, so deepEqual compares plain arrays
const A = window.SceneAgentAnalysis;

function state(k) {
  return { time: k * .02, endEffector: [k * .1, 0, 1], jointPosition: [0], contacts: k >= 2 ? ['red_block'] : [],
    objects: { red_block: [.5, k * .01, .7], table: [0, 0, .5] } };
}
function row(i) {
  const target = k => [.5, k * .01, .7];
  return JSON.stringify({ cycleId: i, timeStart: i * .02, timeEnd: (i + 1) * .02,
    observation: { time: i * .02, endEffector: state(i).endEffector, targetPosition: target(i) },
    nextObservation: { time: (i + 1) * .02, endEffector: state(i + 1).endEffector, targetPosition: target(i + 1) },
    truthBefore: state(i), truthAfter: state(i + 1) });
}
function fixture(count = 5) { return Array.from({ length: count }, (_, i) => row(i)).join('\n') + '\n'; }

test('transition records become a compact track with only objects that actually moved', () => {
  const track = A.parseTransitions(fixture());
  assert.equal(track.cycles, 5);
  assert.deepEqual(Object.keys(track.objects), ['red_block'], 'static objects are not replayed');
  assert.equal(track.times.length, 6, 'start state plus one state after every control cycle');
  assert.equal(track.times[0], 0);
  assert.ok(Math.abs(track.duration - .1) < 1e-9);
  assert.deepEqual(track.ee[5], [.5, 0, 1]);
  assert.deepEqual(track.contacts[1], []);
  assert.deepEqual(track.contacts[3], ['red_block']);
});

test('malformed lines are counted and never invented; an empty file is an error', () => {
  const track = A.parseTransitions('not json\n' + fixture(2) + '\n{"cycleId":9}\n');
  assert.equal(track.cycles, 2);
  assert.equal(track.skipped, 2);
  assert.throws(() => A.parseTransitions('\n\n'), /没有可用的控制记录/);
});

test('long tracks are thinned without dropping the first or last recorded state', () => {
  const track = A.parseTransitions(fixture(400), { maxPoints: 50 });
  assert.ok(track.times.length <= 51);
  assert.equal(track.times[0], 0);
  assert.ok(Math.abs(track.times.at(-1) - 8) < 1e-9);
});

test('sampling interpolates recorded positions and keeps contacts of the earlier state', () => {
  const track = A.parseTransitions(fixture());
  const middle = A.sample(track, .05);
  assert.ok(Math.abs(middle.ee[0] - .25) < 1e-9);
  assert.deepEqual(middle.contacts, ['red_block']);
  assert.deepEqual(A.sample(track, -4).ee, track.ee[0]);
  assert.deepEqual(A.sample(track, 99).ee, track.ee.at(-1));
  assert.equal(A.sample(track, 99).time, track.duration);
});

test('simulation z-up coordinates map to the viewer y-up frame', () => {
  assert.deepEqual(A.toThree([1, 2, 3]), [1, 3, -2]);
});

test('report rows keep nested recorded values, drop empty ones and never compute a score', () => {
  const rows = A.reportRows({ success: true, seconds: 6.04, task: { id: 'push', tolerance: [.05, .06] },
    error: null, actual: [.5, .1, .757], big: Array.from({ length: 40 }, (_, i) => i) });
  const byKey = Object.fromEntries(rows.map(entry => [entry.key, entry]));
  assert.equal(byKey.success.kind, 'boolean');
  assert.equal(byKey.seconds.value, 6.04);
  assert.equal(byKey['task.id'].value, 'push');
  assert.equal(byKey['task.tolerance'].value, '[0.05, 0.06]');
  assert.equal(byKey.actual.value, '[0.5, 0.1, 0.757]');
  assert.equal(byKey.error, undefined);
  assert.equal(byKey.big, undefined, 'long arrays stay in the raw report');
  assert.equal(rows[0].key, 'success', 'headline fields come first');
});

test('scene node labels stay readable without inventing translations', () => {
  assert.equal(A.nodeTitle({ id: 'west_wall' }), 'west wall');
  assert.equal(A.nodeTitle({ id: 'x', label: '西墙' }), '西墙');
  assert.equal(A.nodeKind({ kind: 'box', role: 'enclosure' }), '围护结构');
  assert.equal(A.nodeKind({ kind: 'space' }), '空间');
  assert.equal(A.nodeKind({ kind: 'box', mass: .06 }), '可动物体');
  assert.equal(A.nodeKind({ kind: 'box', mass: 0 }), '静态物体');
  assert.equal(A.nodeKind({ kind: 'custom-kind' }), 'custom-kind');
});

test('known event keys get plain labels and unknown ones keep their original key', () => {
  assert.equal(A.eventLabel({ type: 'delivery_receipt' }), '交付回执已记录');
  assert.equal(A.eventLabel({ type: 'tool_call', name: 'scene_observe' }), '调用工具 · 场景观察');
  assert.equal(A.eventLabel({ type: 'domain_event', stage: 'geometry' }), '几何构建');
  assert.equal(A.eventLabel({ type: 'something_new' }), 'something_new');
});

test('camera poses are read from frozen task records and turned into frustum corners', () => {
  const identityLookingDown = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 2], [0, 0, 0, 1]];
  const tasks = [{ id: 't', cameras: { main: { worldFromCamera: identityLookingDown, width: 100, height: 50,
    intrinsics: { fx: 50, fy: 50, cx: 50, cy: 25 } } } }];
  const cameras = A.findCameras(tasks);
  assert.equal(cameras.length, 1);
  assert.equal(cameras[0].id, 'main');
  const corners = A.frustumCorners(cameras[0], 1);
  assert.equal(corners.length, 4);
  assert.deepEqual(cameras[0].origin, [0, 0, 2]);
  assert.deepEqual(cameras[0].forward.map(v => Math.round(v * 1e6) / 1e6 + 0), [0, 0, -1]);
  assert.deepEqual(corners[0].map(v => Math.round(v * 1e6) / 1e6 + 0), [-1, .5, 1]);
});

test('a camera without complete frozen intrinsics is not drawn', () => {
  assert.deepEqual(A.findCameras([{ cameras: { bad: { worldFromCamera: [[1]] } } }]), []);
});

test('share state round-trips through the page address and rejects malformed values', () => {
  const hash = A.encodeShare({ project: 'p_1', position: [1, 2, 3.5], target: [0, .5, 0], node: 'red_block', view: 'top' });
  assert.deepEqual(A.decodeShare(hash), { project: 'p_1', position: [1, 2, 3.5], target: [0, .5, 0], node: 'red_block', view: 'top' });
  assert.deepEqual(A.decodeShare('#p=p_1&c=1,x,3'), { project: 'p_1' });
  assert.deepEqual(A.decodeShare(''), {});
  assert.deepEqual(A.decodeShare('#v=weird'), {});
});

test('transition records preserve and interpolate recorded joint positions', () => {
  const track = A.parseTransitions(fixture());
  assert.deepEqual(track.joints[0], [0]);
  const s0 = A.parseTransitions([
    JSON.stringify({ cycleId: 0, timeStart: 0, timeEnd: .1,
      truthBefore: { time: 0, endEffector: [0,0,1], jointPosition: [0, 0, 0, 0, 0, 0, 0, .04, .04], objects: {} },
      truthAfter: { time: .1, endEffector: [0,0,1], jointPosition: [1, 0, 0, 0, 0, 0, 0, .01, .01], objects: {} } }),
  ].join('\n'));
  const mid = A.sample(s0, .05);
  assert.ok(Math.abs(mid.joints[0] - .5) < 1e-9);
  assert.ok(Math.abs(mid.joints[7] - .025) < 1e-9);
});

test('frankaLinkPoses matches verified kinematics for q0 and finger opening', () => {
  const poses = A.frankaLinkPoses([0, -Math.PI / 4, 0, -3 * Math.PI / 4, 0, Math.PI / 2, Math.PI / 4, .04, .04]);
  assert.ok(Math.abs(poses.panda_hand.p[0] - 0.30692) < 1e-3);
  assert.ok(Math.abs(poses.panda_hand.p[2] - 0.58995) < 1e-3);
  assert.ok(Math.abs(poses.panda_leftfinger.p[1] + 0.04) < 1e-4);
  assert.ok(Math.abs(poses.panda_rightfinger.p[1] - 0.04) < 1e-4);
  assert.equal(A.frankaLinkPoses([0, 1]), null);
});
