// Pure helpers for the workbench page. They only read recorded data; nothing here computes a score,
// moves an object that was not recorded, or translates names that the generator did not provide.
(() => {
  const VIEWS = ['perspective', 'front', 'side', 'top'];
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const vector = (value, size = 3) => Array.isArray(value) && value.length >= size && value.slice(0, size).every(finite)
    ? value.slice(0, size) : null;
  const toThree = ([x, y, z]) => [x, z, -y];

  function parseTransitions(text, { maxPoints = 600 } = {}) {
    const states = [];
    let cycles = 0, skipped = 0;
    for (const line of String(text).split('\n')) {
      if (!line.trim()) continue;
      let row;
      try { row = JSON.parse(line); } catch { skipped++; continue; }
      const before = row?.truthBefore || row?.observation, after = row?.truthAfter || row?.nextObservation;
      const afterEe = vector(after?.endEffector), afterTime = finite(after?.time) ? after.time : row?.timeEnd;
      if (!afterEe || !finite(afterTime)) { skipped++; continue; }
      if (!states.length) {
        const beforeEe = vector(before?.endEffector), beforeTime = finite(before?.time) ? before.time : row.timeStart;
        if (beforeEe && finite(beforeTime)) {
          states.push({ time: beforeTime, ee: beforeEe, target: vector(row.observation?.targetPosition),
            joints: Array.isArray(before?.jointPosition) && before.jointPosition.every(finite) ? before.jointPosition : null,
            objects: before.objects || {}, contacts: Array.isArray(before.contacts) ? before.contacts : [] });
        }
      }
      states.push({ time: afterTime, ee: afterEe, target: vector(row.nextObservation?.targetPosition),
        joints: Array.isArray(after?.jointPosition) && after.jointPosition.every(finite) ? after.jointPosition : null,
        objects: after.objects || {}, contacts: Array.isArray(after.contacts) ? after.contacts : [] });
      cycles++;
    }
    if (!cycles) throw new Error('没有可用的控制记录');
    const moved = new Set();
    const first = states[0].objects;
    for (const state of states) for (const [id, at] of Object.entries(state.objects)) {
      const origin = vector(first[id]), position = vector(at);
      if (origin && position && Math.hypot(position[0] - origin[0], position[1] - origin[1], position[2] - origin[2]) > 1e-3) moved.add(id);
    }
    const step = Math.max(1, Math.ceil(states.length / maxPoints));
    const kept = states.filter((_, index) => index % step === 0 || index === states.length - 1);
    const objects = {};
    for (const id of moved) {
      let previous = vector(first[id]);
      objects[id] = kept.map(state => { previous = vector(state.objects[id]) || previous; return previous; });
    }
    let previousTarget = kept.find(state => state.target)?.target || null;
    const target = kept.map(state => { previousTarget = state.target || previousTarget; return previousTarget; });
    const times = kept.map(state => state.time);
    return { schema: 'sceneagent-replay-track-v1', cycles, skipped, times, duration: times.at(-1) - times[0],
      ee: kept.map(state => state.ee), joints: kept.map(state => state.joints), target,
      contacts: kept.map(state => state.contacts), objects };
  }

  function lerp(a, b, ratio) { return a.map((value, index) => value + (b[index] - value) * ratio); }

  function sample(track, time) {
    const start = track.times[0], clamped = Math.min(Math.max(Number(time) || 0, 0), track.duration);
    const absolute = start + clamped;
    let low = 0, high = track.times.length - 1;
    while (high - low > 1) {
      const middle = (low + high) >> 1;
      if (track.times[middle] <= absolute) low = middle; else high = middle;
    }
    if (absolute >= track.times[high] - 1e-12) low = high;
    const next = Math.min(low + 1, track.times.length - 1);
    const span = track.times[next] - track.times[low];
    const ratio = span > 0 ? Math.min(1, Math.max(0, (absolute - track.times[low]) / span)) : 0;
    const objects = {};
    for (const [id, path] of Object.entries(track.objects)) objects[id] = lerp(path[low], path[next], ratio);
    const target = track.target[low] && track.target[next] ? lerp(track.target[low], track.target[next], ratio) : track.target[low];
    const jLow = track.joints?.[low], jNext = track.joints?.[next];
    const joints = jLow && jNext && jLow.length === jNext.length ? lerp(jLow, jNext, ratio) : jLow || null;
    return { time: clamped, index: low, ee: lerp(track.ee[low], track.ee[next], ratio), joints, target, objects, contacts: track.contacts[low] };
  }

  const PRIORITY = ['success', 'status', 'score', 'reward', 'inGoal', 'stable', 'robotContact', 'seconds', 'physicsSteps', 'controlCycles'];
  function reportRows(report) {
    const rows = [];
    const visit = (value, path, depth) => {
      if (value === null || value === undefined) return;
      if (Array.isArray(value)) {
        if (value.length && value.length <= 12 && value.every(item => finite(item) || typeof item === 'string')) {
          rows.push({ key: path, kind: 'vector', value: `[${value.join(', ')}]` });
        }
        return;
      }
      if (typeof value === 'object') {
        if (depth < 3) for (const [key, inner] of Object.entries(value)) visit(inner, path ? `${path}.${key}` : key, depth + 1);
        return;
      }
      const kind = typeof value;
      if (['boolean', 'number', 'string'].includes(kind)) rows.push({ key: path, kind, value });
    };
    visit(report, '', 0);
    const rank = key => { const index = PRIORITY.indexOf(key); return index < 0 ? PRIORITY.length : index; };
    return rows.map((row, index) => ({ row, index })).sort((a, b) => rank(a.row.key) - rank(b.row.key) || a.index - b.index).map(({ row }) => row);
  }

  const NODE_KINDS = { box: '方块', cylinder: '圆柱', sphere: '球体', capsule: '胶囊体', asset: '资产', mesh: '网格', robot: '机器人' };
  function nodeTitle(entry) {
    return entry?.label || entry?.name || String(entry?.id ?? '').replace(/_/g, ' ');
  }
  function nodeKind(entry) {
    if (entry?.kind === 'space') return '空间';
    if (entry?.role === 'enclosure') return '围护结构';
    if (entry?.kind === 'robot') return '机器人';
    if (finite(entry?.mass)) return entry.mass > 0 ? '可动物体' : '静态物体';
    return NODE_KINDS[entry?.kind] || entry?.kind || '';
  }

  const EVENT_TYPES = { result: '作业结果已返回', submitted: '作业已提交', delivery_receipt: '交付回执已记录', generation_complete: '生成任务完成',
    delivery_revalidated: '交付已复核', cancelled: '作业已取消', generation_stopped: '生成任务已停止', tool_result: '工具已返回', tool_call: '调用工具', progress: '进度更新', domain_event: '远端状态更新' };
  const EVENT_STAGES = { program: '程序执行', geometry: '几何构建', ready: '场景就绪', simulation: '仿真运行', control: '机器人控制', complete: '执行完成' };
  const EVENT_TOOLS = { job: '远端作业', scene_observe: '场景观察', project_deliver: '项目交付', project_inspect: '项目检查', task_check: '任务检查',
    robot_run: '机器人执行', assets_lookup: '资产检索', assets_prepare: '资产准备' };
  function eventLabel(entry) {
    const type = entry?.type, stage = EVENT_STAGES[entry?.stage];
    if (['domain_event', 'progress'].includes(type) && stage) return stage;
    if (!EVENT_TYPES[type]) return type || '';
    if (['tool_call', 'tool_result'].includes(type) && entry.name) return `${EVENT_TYPES[type]} · ${EVENT_TOOLS[entry.name] || entry.name}`;
    return EVENT_TYPES[type];
  }

  function matrix(value) {
    return Array.isArray(value) && value.length === 4 && value.every(row => vector(row, 4)) ? value : null;
  }
  function findCameras(tasks) {
    const found = [];
    const visit = (value, name, depth) => {
      if (!value || typeof value !== 'object' || depth > 6) return;
      const pose = matrix(value.worldFromCamera), k = value.intrinsics;
      if (pose && k && [k.fx, k.fy, k.cx, k.cy, value.width, value.height].every(finite)) {
        found.push({ id: String(value.cameraId || value.id || name || `camera-${found.length + 1}`), matrix: pose,
          origin: [pose[0][3], pose[1][3], pose[2][3]], forward: [-pose[0][2], -pose[1][2], -pose[2][2]],
          up: [pose[0][1], pose[1][1], pose[2][1]], width: value.width, height: value.height, intrinsics: k });
        return;
      }
      for (const [key, inner] of Object.entries(value)) visit(inner, Array.isArray(value) ? name : key, depth + 1);
    };
    visit(tasks, '', 0);
    return found;
  }
  function frustumCorners(camera, depth) {
    const { fx, fy, cx, cy } = camera.intrinsics, m = camera.matrix;
    return [[0, 0], [camera.width, 0], [camera.width, camera.height], [0, camera.height]].map(([u, v]) => {
      const p = [(u - cx) / fx * depth, -(v - cy) / fy * depth, -depth];
      return [0, 1, 2].map(row => m[row][0] * p[0] + m[row][1] * p[1] + m[row][2] * p[2] + m[row][3]);
    });
  }

  const round = value => Math.round(value * 1000) / 1000;
  function encodeShare({ project, position, target, node, view }) {
    const parts = [];
    if (project) parts.push(`p=${encodeURIComponent(project)}`);
    if (vector(position) && vector(target)) parts.push(`c=${position.map(round).join(',')}`, `t=${target.map(round).join(',')}`);
    if (node) parts.push(`n=${encodeURIComponent(node)}`);
    if (VIEWS.includes(view)) parts.push(`v=${view}`);
    return parts.length ? `#${parts.join('&')}` : '';
  }
  function decodeShare(hash) {
    const result = {};
    const fields = new Map(String(hash || '').replace(/^#/, '').split('&').filter(Boolean).map(part => {
      const index = part.indexOf('=');
      return index < 0 ? [part, ''] : [part.slice(0, index), part.slice(index + 1)];
    }));
    const decode = value => { try { return decodeURIComponent(value); } catch { return ''; } };
    const numbers = value => {
      const list = String(value || '').split(',').map(Number);
      return list.length === 3 && list.every(finite) && String(value).split(',').every(entry => entry.trim() !== '') ? list : null;
    };
    if (fields.get('p')) result.project = decode(fields.get('p'));
    const position = numbers(fields.get('c')), target = numbers(fields.get('t'));
    if (position && target) { result.position = position; result.target = target; }
    if (fields.get('n')) result.node = decode(fields.get('n'));
    if (VIEWS.includes(fields.get('v'))) result.view = fields.get('v');
    return result;
  }


  function qmul(a, b) {
    const [ax, ay, az, aw] = a, [bx, by, bz, bw] = b;
    return [
      aw * bx + ax * bw + ay * bz - az * by,
      aw * by - ax * bz + ay * bw + az * bx,
      aw * bz + ax * by - ay * bx + az * bw,
      aw * bw - ax * bx - ay * by - az * bz,
    ];
  }
  function qrot(q, v) {
    const [x, y, z, w] = q, [vx, vy, vz] = v;
    const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
    return [vx + w * tx + y * tz - z * ty, vy + w * ty + z * tx - x * tz, vz + w * tz + x * ty - y * tx];
  }
  function qz(angle) { return [0, 0, Math.sin(angle / 2), Math.cos(angle / 2)]; }
  function qx(angle) { return [Math.sin(angle / 2), 0, 0, Math.cos(angle / 2)]; }
  function mulT(t1, t2) {
    const rp = qrot(t1.q, t2.p);
    return { p: [t1.p[0] + rp[0], t1.p[1] + rp[1], t1.p[2] + rp[2]], q: qmul(t1.q, t2.q) };
  }
  const H = Math.PI / 2;
  const FRANKA_CHAIN = [
    ['panda_link0', [0, 0, 0], [0, 0, 0, 1], -1],
    ['panda_link1', [0, 0, 0.333], [0, 0, 0, 1], 0],
    ['panda_link2', [0, 0, 0], qx(-H), 1],
    ['panda_link3', [0, -0.316, 0], qx(H), 2],
    ['panda_link4', [0.0825, 0, 0], qx(H), 3],
    ['panda_link5', [-0.0825, 0.384, 0], qx(-H), 4],
    ['panda_link6', [0, 0, 0], qx(H), 5],
    ['panda_link7', [0.088, 0, 0], qx(H), 6],
    ['panda_hand', [0, 0, 0.107], qz(-Math.PI / 4), -1],
  ];
  function frankaLinkPoses(joints) {
    if (!Array.isArray(joints) || joints.length < 7 || !joints.slice(0, 7).every(finite)) return null;
    let cur = { p: [0, 0, 0], q: [0, 0, 0, 1] };
    const poses = {};
    for (const [name, p, q, idx] of FRANKA_CHAIN) {
      cur = mulT(cur, { p, q });
      if (idx >= 0) cur = mulT(cur, { p: [0, 0, 0], q: qz(joints[idx]) });
      poses[name] = cur;
    }
    const f1 = finite(joints[7]) ? joints[7] : 0.04;
    const f2 = finite(joints[8]) ? joints[8] : f1;
    poses.panda_leftfinger = mulT(poses.panda_hand, { p: [0, f1, 0.0584], q: [0, 0, 0, 1] });
    poses.panda_rightfinger = mulT(poses.panda_hand, { p: [0, -f2, 0.0584], q: [0, 0, 0, 1] });
    return poses;
  }

  window.SceneAgentAnalysis = Object.freeze({ toThree, parseTransitions, sample, frankaLinkPoses, reportRows, nodeTitle, nodeKind, eventLabel,
    findCameras, frustumCorners, encodeShare, decodeShare, VIEWS });
})();
