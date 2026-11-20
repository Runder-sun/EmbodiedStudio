"""Small scene and task contracts; the agent owns construction and planning."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


def revision_key(**inputs):
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()[:24]


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ValueError('IDs must contain 1–80 letters, numbers, underscores or hyphens')
    return value


_UNSET = object()


def _vector(value, length, label):
    try:
        result = [float(x) for x in value]
    except (TypeError, ValueError):
        raise ValueError(f'invalid {label}') from None
    if len(result) != length or not all(math.isfinite(x) for x in result):
        raise ValueError(f'invalid {label}')
    return result


def _light_vector(value, length, label):
    if not isinstance(value, (list, tuple)) or any(type(item) not in (int, float) for item in value):
        raise ValueError(f'{label} requires numeric values in a list or tuple')
    return _vector(value, length, label)


def _quaternion(value):
    result = _vector(value, 4, 'orientation')
    norm = math.hypot(*result)
    if norm < 1e-12:
        raise ValueError('orientation must be a nonzero quaternion')
    result = [x/norm for x in result]
    # q and -q describe the same rotation; keep one source representation.
    if next(x for x in result if x != 0) < 0:
        result = [-x for x in result]
    return result


def _multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return [w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j,
            w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v]


def quaternion_rotate(q, value):
    return _multiply(_multiply(q, [0, *value]), [q[0], -q[1], -q[2], -q[3]])[1:]


def _depths(graph, label):
    """Validate parent chains without consuming Python's recursion stack."""
    depths = {}
    for start in graph:
        chain, visiting = [], set()
        current = start
        while current is not None and current not in depths:
            if current not in graph:
                raise ValueError(f'unknown {label}: {current}')
            if current in visiting:
                raise ValueError(f'{label} cycle: {current}')
            visiting.add(current)
            chain.append(current)
            current = graph[current]
        depth = depths.get(current, 0)
        for id in reversed(chain):
            depth += 1
            depths[id] = depth
    return depths


def _relation_cycles(nodes, relations, kind):
    edges = {id: [] for id in nodes}
    for relation in relations:
        if relation['kind'] == kind:
            edges[relation['source']].append(relation['target'])
    state = {}
    for start in nodes:
        if state.get(start) == 2:
            continue
        stack = [(start, False)]
        while stack:
            id, leaving = stack.pop()
            if leaving:
                state[id] = 2
                continue
            if state.get(id) == 1:
                raise ValueError(f'{kind} cycle: {id}')
            if state.get(id) == 2:
                continue
            state[id] = 1
            stack.append((id, True))
            stack.extend((target, False) for target in reversed(edges[id]))


class World:
    """One node table: ownership, coordinate references and relations stay separate."""
    def __init__(self, seed=0, lights=None, *, native=True):
        if native is not True:
            raise ValueError('legacy World construction requires the package frozen worker')
        self.seed = seed
        self.nodes = {}
        self.relations = []
        self.interactions = []
        self.lights = deepcopy(lights)
        self.native = native
        self.io = {'clock': {}, 'ports': {}, 'streams': {}}
        self._validate_lights()

    def bind(self, id, prim_path, *, owner=None, frame=None, purpose='', requirements=None,
             completion=None, bounds=None):
        """Bind semantics to native scene data, without describing its physics twice."""
        if (not self.native or not isinstance(prim_path, str) or not prim_path.startswith('/World/')
                or any(not part.isidentifier() for part in prim_path[1:].split('/'))):
            raise ValueError('native bindings require an absolute prim path inside /World')
        return self._add(id, 'native', (0, 0, 0), owner=owner, frame=frame, purpose=purpose,
                         requirements=requirements, completion=completion, bounds=bounds, primPath=prim_path)

    def robot(self, id, profile, *, owner=None, at=(0, 0, 0), orientation=(1, 0, 0, 0),
              frame=None, purpose=''):
        """Load one public robot profile using the existing native model and I/O path."""
        from runtime.robot import load_robot
        return load_robot(self, id, profile, owner=owner, at=at, orientation=orientation,
                          frame=frame, purpose=purpose)

    def _add(self, id, kind, at, parent=None, yaw=0, owner=_UNSET, frame=_UNSET,
             orientation=None, bounds=None, purpose='', requirements=None, completion=None, **fields):
        valid_id(id)
        if id in self.nodes:
            raise ValueError(f'duplicate object ID: {id}')
        owner = parent if owner is _UNSET else owner
        frame = parent if frame is _UNSET else frame
        if parent is not None and (owner != parent or frame != parent):
            raise ValueError('parent cannot conflict with explicit owner/frame')
        for label, reference in [('owner', owner), ('frame', frame)]:
            if reference is not None and reference not in self.nodes:
                raise ValueError(f'unknown {label}: {reference}')
        yaw = _vector([yaw], 1, 'yaw')[0]
        if orientation is not None and yaw != 0:
            raise ValueError('use orientation or yaw, not both')
        quaternion = _quaternion(orientation if orientation is not None else
                                 [math.cos(yaw/2), 0, 0, math.sin(yaw/2)])
        node = dict(id=id, kind=kind, owner=owner, frame=frame, at=_vector(at, 3, 'position'),
                    orientation=quaternion, bounds=deepcopy(bounds), purpose=purpose,
                    requirements=deepcopy(requirements if requirements is not None else []),
                    completion=deepcopy(completion if completion is not None else []), **fields)
        self._validate_node(node)
        self.nodes[id] = node
        return id

    def space(self, id, at=(0, 0, 0), parent=None, yaw=0, **fields):
        return self._add(id, 'space', at, parent, yaw, **fields)

    def box(self, id, size, at=(0, 0, 0), parent=None, yaw=0, color=_UNSET,
            mass=0, joint=None, role='', material=None, uv_repeat=_UNSET, texture_period_m=None, **fields):
        if color is _UNSET:
            color = (1, 1, 1, 1) if material is not None else (.7, .7, .7, 1)
        if texture_period_m is not None and uv_repeat is not _UNSET:
            raise ValueError('texture_period_m and uv_repeat are mutually exclusive')
        if texture_period_m is not None:
            fields['texture_period_m'] = _vector([texture_period_m], 1, 'texture_period_m')[0]
        repeat = None if texture_period_m is not None else _vector(
            (1, 1) if uv_repeat is _UNSET else uv_repeat, 2, 'uv_repeat')
        return self._add(id, 'box', at, parent, yaw, size=_vector(size, 3, 'size'),
                         color=_vector(color, 4, 'color'), mass=float(mass),
                         joint=deepcopy(joint), role=role, material=material,
                         uv_repeat=repeat, **fields)

    def cylinder(self, id, radius, height, **fields):
        result = self.box(id, (radius * 2, radius * 2, height), **fields)
        self.nodes[id]['kind'] = 'cylinder'
        return result

    def asset(self, id, path, size, up_axis, collision_mode='box', collision_boxes=None, **fields):
        if up_axis not in ('Y', 'Z'):
            raise ValueError('asset up_axis must be explicitly Y or Z')
        fields.setdefault('color', (1, 1, 1, 1))
        fields['collision_mode'] = collision_mode
        if collision_boxes is not None:
            fields['collision_boxes'] = deepcopy(collision_boxes)
        result = self.box(id, size, **fields)
        self.nodes[id].update(kind='asset', path=str(path), up_axis=up_axis)
        return result

    def light(self, id, kind, intensity, at=(0, 0, 0), frame=None, orientation=(1, 0, 0, 0),
              color=(1, 1, 1), radius=None, width=None, height=None):
        light = dict(id=id, kind=kind, intensity=_light_vector([intensity], 1, 'light intensity')[0],
                     at=_light_vector(at, 3, 'light position'), frame=frame,
                     orientation=_light_vector(orientation, 4, 'light orientation'), color=_light_vector(color, 3, 'light color'))
        for name, value in [('radius', radius), ('width', width), ('height', height)]:
            if value is not None:
                light[name] = _light_vector([value], 1, 'light '+name)[0]
        self._validate_light(light)
        if self.lights is None:
            self.lights = []
        if any(item['id'] == id for item in self.lights):
            raise ValueError(f'duplicate light ID: {id}')
        light['orientation'] = _quaternion(light['orientation'])
        self.lights.append(light)
        return id

    def _validate_light(self, light):
        if not isinstance(light, dict):
            raise ValueError('light must be a declaration')
        dimensions = {'sphere': {'radius'}, 'rect': {'width', 'height'}}.get(light.get('kind'))
        if dimensions is None or set(light) != {'id', 'kind', 'intensity', 'at', 'frame', 'orientation', 'color'} | dimensions:
            raise ValueError('lights support sphere with radius or rect with width and height')
        valid_id(light['id'])
        if light['frame'] is not None and light['frame'] not in self.nodes:
            raise ValueError('unknown light frame: '+str(light['frame']))
        _light_vector(light['at'], 3, 'light position')
        if abs(math.hypot(*_light_vector(light['orientation'], 4, 'light orientation'))-1) > 1e-8:
            raise ValueError('light orientation must be a unit quaternion')
        if any(value < 0 or value > 1 for value in _light_vector(light['color'], 3, 'light color')):
            raise ValueError('light color must contain values from zero to one')
        for name in {'intensity'} | dimensions:
            if _light_vector([light[name]], 1, 'light '+name)[0] <= 0:
                raise ValueError('light '+name+' must be positive finite')

    def _validate_lights(self):
        if self.lights is not None:
            if not isinstance(self.lights, list):
                raise ValueError('lights must be None or a list')
            light_ids = set()
            for light in self.lights:
                self._validate_light(light)
                if light['id'] in light_ids:
                    raise ValueError('duplicate light ID: '+light['id'])
                light_ids.add(light['id'])

    def relate(self, kind, source, target, **metadata):
        if kind not in ('supported_by', 'contained_in', 'attached_to'):
            raise ValueError(f'unsupported relation: {kind}')
        if source not in self.nodes or target not in self.nodes:
            raise ValueError('relation endpoints must bind existing objects')
        if any((r['kind'], r['source'], r['target']) == (kind, source, target) for r in self.relations):
            raise ValueError('duplicate relation')
        self.relations.append(dict(kind=kind, source=source, target=target, **deepcopy(metadata)))

    def when(self, id, target, event, effect, threshold=0, mode='once'):
        valid_id(id)
        if any(item['id'] == id for item in self.interactions):
            raise ValueError(f'duplicate interaction ID: {id}')
        rule = dict(id=id, target=target, event=event,
                    threshold=_vector([threshold], 1, 'threshold')[0], effect=deepcopy(effect), mode=mode)
        self._validate_interaction(rule)
        self.interactions.append(rule)

    def _validate_node(self, node):
        valid_id(node['id'])
        _vector(node['at'], 3, 'position')
        quaternion = _vector(node['orientation'], 4, 'orientation')
        if abs(math.hypot(*quaternion)-1) > 1e-8:
            raise ValueError('orientation must be a unit quaternion')
        if node['kind'] not in ('space', 'box', 'cylinder', 'asset', 'native'):
            raise ValueError(f"unsupported node kind: {node['kind']}")
        if node['kind'] == 'native':
            path = node.get('primPath')
            if (not self.native or not isinstance(path, str) or not path.startswith('/World/')
                    or any(not part.isidentifier() for part in path[1:].split('/'))):
                raise ValueError('invalid native prim binding')
        if not isinstance(node['purpose'], str):
            raise ValueError('purpose must be text')
        bounds = node['bounds']
        if bounds is not None:
            low = _vector(bounds['min'], 3, 'bounds.min')
            high = _vector(bounds['max'], 3, 'bounds.max')
            if any(a >= b for a, b in zip(low, high)):
                raise ValueError('bounds must have positive extents')
        if node['kind'] not in ('space', 'native'):
            if any(x <= 0 for x in _vector(node['size'], 3, 'size')):
                raise ValueError(f"size must be positive: {node['id']}")
            if _vector([node['mass']], 1, 'mass')[0] < 0:
                raise ValueError('mass must not be negative')
            _vector(node['color'], 4, 'color')
            if node.get('material') is not None and (not isinstance(node['material'], str) or not node['material']):
                raise ValueError('material must reference a prepared material report')
            if node.get('texture_period_m') is not None:
                if _vector([node['texture_period_m']], 1, 'texture_period_m')[0] <= 0:
                    raise ValueError('texture_period_m must be positive finite metres')
                if node.get('uv_repeat') is not None:
                    raise ValueError('texture_period_m and uv_repeat are mutually exclusive')
            elif any(value <= 0 for value in _vector(node.get('uv_repeat', [1, 1]), 2, 'uv_repeat')):
                raise ValueError('uv_repeat must contain two positive finite values')
            mode = node.get('collision_mode', 'box')
            if mode not in ('box', 'mesh', 'convex_decomposition'):
                raise ValueError('unsupported collision_mode')
            if mode == 'mesh' and (node['mass'] > 0 or node.get('joint')):
                raise ValueError('mesh collision is only supported for static objects without joints')
            boxes = node.get('collision_boxes')
            if boxes is not None:
                if mode != 'box' or not isinstance(boxes, list) or not 1 <= len(boxes) <= 32:
                    raise ValueError('collision_boxes require box mode and 1–32 boxes')
                for box in boxes:
                    if not isinstance(box, dict) or set(box) != {'size', 'at', 'orientation'}:
                        raise ValueError('collision_boxes support size, at and orientation only')
                    if any(x <= 0 for x in _vector(box['size'], 3, 'collision_boxes size')):
                        raise ValueError('collision_boxes size must be positive')
                    _vector(box['at'], 3, 'collision_boxes position')
                    if abs(math.hypot(*_vector(box['orientation'], 4, 'collision_boxes orientation'))-1) > 1e-8:
                        raise ValueError('collision_boxes orientation must be a unit quaternion')
            material = node.get('physics_material')
            if material is not None:
                if not isinstance(material, dict) or not material or set(material)-{'static_friction', 'dynamic_friction', 'restitution'}:
                    raise ValueError('physics_material supports static_friction, dynamic_friction and restitution')
                for name, value in material.items():
                    number = _vector([value], 1, 'physics_material '+name)[0]
                    if number < 0 or name == 'restitution' and number > 1:
                        raise ValueError('physics_material has an invalid '+name)
        joint = node.get('joint')
        if joint:
            if joint.get('type', 'slide') not in ('slide', 'hinge'):
                raise ValueError('joints support slide or hinge')
            if math.hypot(*_vector(joint.get('axis', [0, 1, 0]), 3, 'joint axis')) == 0:
                raise ValueError('joint axis must not be zero')
            low, high = _vector(joint.get('range', [0, .2]), 2, 'joint range')
            if low >= high:
                raise ValueError('joint range must increase')
            if joint.get('frame') is not None and joint['frame'] not in self.nodes:
                raise ValueError(f"unknown joint frame: {joint['frame']}")
        if node['kind'] == 'asset' and node['up_axis'] not in ('Y', 'Z'):
            raise ValueError('asset up_axis must be explicitly Y or Z')

    def _validate_interaction(self, rule):
        if rule.get('mode', 'once') not in ('once', 'edge', 'continuous'):
            raise ValueError('interaction mode supports once, edge or continuous')
        if rule['target'] not in self.nodes or rule['effect'].get('target') not in self.nodes:
            raise ValueError('interaction source and effect target must bind existing objects')
        if rule['event'] not in ('robot_contact', 'joint_at_least'):
            raise ValueError(f"unsupported interaction event: {rule['event']}")
        if rule['event'] == 'joint_at_least' and not self.nodes[rule['target']].get('joint'):
            raise ValueError('joint trigger requires a real object joint')
        if set(rule['effect']) - {'target', 'state', 'color'}:
            raise ValueError('effects support logical state and appearance only')
        _vector([rule['threshold']], 1, 'threshold')
        if 'color' in rule['effect']:
            _vector(rule['effect']['color'], 4, 'effect color')

    def validate(self):
        if self.native and self.interactions:
            raise ValueError('native construction cannot execute legacy World interactions; use qualified native components')
        for id, node in self.nodes.items():
            if id != node['id']:
                raise ValueError('node table key and stable ID differ')
            self._validate_node(node)
        owner_depths = _depths({id: node['owner'] for id, node in self.nodes.items()}, 'owner')
        frame_depths = _depths({id: node['frame'] for id, node in self.nodes.items()}, 'frame')
        self._validate_lights()
        seen = set()
        for relation in self.relations:
            key = (relation['kind'], relation['source'], relation['target'])
            if key in seen:
                raise ValueError('duplicate relation')
            seen.add(key)
            if key[0] not in ('supported_by', 'contained_in', 'attached_to'):
                raise ValueError(f'unsupported relation: {key[0]}')
            if key[1] not in self.nodes or key[2] not in self.nodes:
                raise ValueError('unknown relation endpoint')
        for kind in ('supported_by', 'contained_in'):
            _relation_cycles(self.nodes, self.relations, kind)
        seen = set()
        for rule in self.interactions:
            valid_id(rule['id'])
            if rule['id'] in seen:
                raise ValueError('duplicate interaction ID')
            seen.add(rule['id'])
            self._validate_interaction(rule)
        if (not isinstance(self.io, dict) or set(self.io) != {'clock', 'ports', 'streams'}
              or any(not isinstance(value, dict) for value in self.io.values())):
            raise ValueError('native I/O requires clock, ports and streams declarations')
        json.dumps(self.snapshot(validate=False), allow_nan=False)
        return {'valid': True, 'nodeCount': len(self.nodes), 'relationCount': len(self.relations),
                'maxDepth': max(owner_depths.values(), default=0),
                'maxFrameDepth': max(frame_depths.values(), default=0)}

    def transform(self, id):
        chain, seen = [], set()
        while id is not None:
            if id in seen:
                raise ValueError(f'frame cycle: {id}')
            if id not in self.nodes:
                raise ValueError(f'unknown frame: {id}')
            seen.add(id)
            node = self.nodes[id]
            if node['kind'] == 'native':
                raise ValueError('native bindings require actual SDK pose reads')
            chain.append(node)
            id = node['frame']
        position, orientation = [0, 0, 0], [1, 0, 0, 0]
        for node in reversed(chain):
            delta = quaternion_rotate(orientation, node['at'])
            position = [a+b for a, b in zip(position, delta)]
            orientation = _quaternion(_multiply(orientation, node['orientation']))
        return position, orientation

    def pose(self, id):
        position, (w, x, y, z) = self.transform(id)
        return position, math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))

    def position(self, id):
        return self.transform(id)[0]

    def context(self, nodeId=None):
        self.validate()
        if nodeId is None:
            chains = {}
            for id, node in self.nodes.items():
                chain, frame = [], node['frame']
                while frame is not None:
                    chain.append(frame)
                    frame = self.nodes[frame]['frame']
                chains[id] = chain
            return deepcopy({'nodeId': None, 'subtree': list(self.nodes.values()),
                             'ancestors': [], 'frameChains': chains, 'externalNodes': [],
                             'relations': self.relations, 'interactions': self.interactions, 'lights': self.lights})
        if nodeId not in self.nodes:
            raise ValueError(f'unknown node: {nodeId}')
        children = {id: [] for id in self.nodes}
        for id, node in self.nodes.items():
            if node['owner'] is not None:
                children[node['owner']].append(id)
        subtree, queue = [], [nodeId]
        while queue:
            id = queue.pop()
            subtree.append(id)
            queue.extend(reversed(children[id]))
        included, external, ancestors, chains = set(subtree), set(), [], {}
        owner = self.nodes[nodeId]['owner']
        while owner is not None:
            ancestors.append(owner)
            owner = self.nodes[owner]['owner']
        for id in subtree:
            chain, frame = [], self.nodes[id]['frame']
            while frame is not None:
                chain.append(frame)
                if frame not in included:
                    external.add(frame)
                frame = self.nodes[frame]['frame']
            chains[id] = chain
        relations = [r for r in self.relations if r['source'] in included or r['target'] in included]
        interactions = [r for r in self.interactions if r['target'] in included or r['effect']['target'] in included]
        for relation in relations:
            external.update({relation['source'], relation['target']}-included)
        for interaction in interactions:
            external.update({interaction['target'], interaction['effect']['target']}-included)
        for light in self.lights or []:
            if light['frame'] is not None and light['frame'] not in included:
                external.add(light['frame'])
        for id in list(external):
            frame = self.nodes[id]['frame']
            while frame is not None:
                if frame not in included:
                    external.add(frame)
                frame = self.nodes[frame]['frame']
        return deepcopy({'nodeId': nodeId, 'subtree': [self.nodes[id] for id in subtree],
                         'ancestors': [self.nodes[id] for id in reversed(ancestors)], 'frameChains': chains,
                         'externalNodes': [self.nodes[id] for id in sorted(external)],
                         'relations': relations, 'interactions': interactions, 'lights': self.lights})

    def diff(self, other):
        before, after = self.snapshot(), other.snapshot()
        old, new = self.nodes, other.nodes
        changed = [{'id': id, 'before': deepcopy(old[id]), 'after': deepcopy(new[id]),
                    'fields': sorted(key for key in old[id].keys() | new[id].keys()
                                     if old[id].get(key, _UNSET) != new[id].get(key, _UNSET))}
                   for id in sorted(old.keys() & new.keys()) if old[id] != new[id]]
        result = {'baseHash': revision_key(world=before), 'sourceHash': revision_key(world=after),
                  'added': [deepcopy(new[id]) for id in sorted(new.keys()-old.keys())],
                  'removed': sorted(old.keys()-new.keys()), 'changed': changed}
        for key in ('relations', 'interactions'):
            result[key] = {'added': [r for r in after[key] if r not in before[key]],
                           'removed': [r for r in before[key] if r not in after[key]]}
        result['world'] = {key: {'before': before.get(key), 'after': after.get(key)}
                           for key in ('seed', 'lights', 'io') if before.get(key) != after.get(key)}
        return result

    def snapshot(self, validate=True):
        if validate:
            self.validate()
        return deepcopy({'schema': 'sceneagent-studio-world-v3',
                         'units': 'meters', 'upAxis': 'Z', 'seed': self.seed,
                         'io': self.io, 'nodes': list(self.nodes.values()),
                         'relations': self.relations, 'interactions': self.interactions,
                         **({'lights': self.lights} if self.lights is not None else {})})

    @classmethod
    def restore(cls, snapshot):
        if snapshot['schema'] != 'sceneagent-studio-world-v3':
            raise ValueError('unsupported world schema; use the package frozen worker')
        if snapshot.get('units') != 'meters' or snapshot.get('upAxis') != 'Z':
            raise ValueError('world must use meters and Z-up')
        world = cls(snapshot['seed'])
        for original in snapshot['nodes']:
            node = deepcopy(original)
            valid_id(node['id'])
            if node['id'] in world.nodes:
                raise ValueError(f"duplicate object ID: {node['id']}")
            if 'parent' in node or 'yaw' in node:
                raise ValueError('world nodes must store only owner/frame and orientation')
            world.nodes[node['id']] = node
        world.io = deepcopy(snapshot['io'])
        world.relations = deepcopy(snapshot.get('relations', []))
        world.interactions = deepcopy(snapshot.get('interactions', []))
        world.lights = deepcopy(snapshot.get('lights'))
        world.validate()
        return world


def validate_source_declaration(declaration):
    if (not isinstance(declaration, dict) or set(declaration) != {'sourceRef', 'helperRefs'}
            or not isinstance(declaration['helperRefs'], list)):
        raise ValueError('source requires sourceRef and helperRefs only')
    names = [declaration['sourceRef'], *declaration['helperRefs']]
    for index, name in enumerate(names):
        if (not isinstance(name, str) or not name or '\\' in name
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or str(PurePosixPath(name)) != name
                or not (PurePosixPath(name).suffix == '.py' or index > 0 and name.endswith('.robot.json'))):
            raise ValueError('sources require a relative Python entry and Python or public robot profile helpers')
    if len(set(names)) != len(names):
        raise ValueError('duplicate source reference')
    return names


def validate_recording(recording, world=None):
    if (not isinstance(recording, dict) or set(recording) != {'cameras', 'primaryCameraId'}
            or not isinstance(recording['cameras'], list)):
        raise ValueError('recording requires cameras and primaryCameraId')
    ids = set()
    for camera in recording['cameras']:
        if (not isinstance(camera, dict) or set(camera) != {'cameraId', 'frame', 'at', 'orientation',
                'resolution', 'rateHz', 'modalities', 'video', 'required', 'source'}):
            raise ValueError('camera requires its complete frozen recording declaration')
        id = valid_id(camera['cameraId'])
        if id in ids:
            raise ValueError('duplicate recording cameraId')
        ids.add(id)
        frame = camera['frame']
        if isinstance(frame, str):
            if frame != 'world' and (world is None or frame not in world.nodes):
                if world is not None:
                    raise ValueError('unknown camera frame: '+frame)
                valid_id(frame)
        elif isinstance(frame, dict) and set(frame) == {'nativePath'}:
            path = frame['nativePath']
            if (world is not None and not world.native or not isinstance(path, str)
                    or not path.startswith('/World/')
                    or any(part in ('', '.', '..') for part in path[1:].split('/'))):
                raise ValueError('camera nativePath must bind the actual native scene')
        else:
            raise ValueError('camera frame requires world, a scene node, or an explicit nativePath')
        _light_vector(camera['at'], 3, 'camera position')
        if abs(math.hypot(*_light_vector(camera['orientation'], 4, 'camera orientation'))-1) > 1e-8:
            raise ValueError('camera orientation must be a unit wxyz quaternion')
        if (not isinstance(camera['resolution'], list) or len(camera['resolution']) != 2
                or any(type(value) is not int or value <= 0 for value in camera['resolution'])):
            raise ValueError('camera resolution requires positive integer width and height')
        if type(camera['rateHz']) is not int or camera['rateHz'] <= 0:
            raise ValueError('camera rateHz must be a positive integer')
        if world is not None:
            rate = world.io.get('clock', {}).get('physicsRateHz')
            if rate is not None and (type(rate) is not int or rate <= 0 or rate % camera['rateHz']):
                raise ValueError('camera rateHz must divide the actual physics clock')
        modalities = camera['modalities']
        if (not isinstance(modalities, list) or not modalities
                or any(not isinstance(name, str) or not name for name in modalities)
                or len(set(modalities)) != len(modalities)):
            raise ValueError('unsupported camera modality declaration')
        if any(type(camera[key]) is not bool for key in ('video', 'required')):
            raise ValueError('camera video and required must be boolean')
        if camera['video'] and any(value % 2 for value in camera['resolution']):
            raise ValueError('video resolution requires even width and height for the current H.264/yuv420p encoder')
        if camera['video'] and 'rgb' not in modalities:
            raise ValueError('camera video requires an rgb modality')
        if camera['source'] not in ('simulation_observer', 'simulated_robot_sensor'):
            raise ValueError('camera source must disclose its simulated observation origin')
    primary = recording['primaryCameraId']
    if (ids and (not isinstance(primary, str) or primary not in ids)) or (not ids and primary is not None):
        raise ValueError('primaryCameraId must select a declared recording camera')
    return recording


@dataclass
class Task:
    id: str
    instruction: str = ''
    bindings: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    rubric: dict = field(default_factory=dict)
    measurements: dict = field(default_factory=dict)
    evaluation: dict = field(default_factory=lambda: {'cases': []})
    recording: dict = field(default_factory=lambda: {'cameras': [], 'primaryCameraId': None})
    execution: dict = field(default_factory=lambda: {'physicalSeconds': 30})

    def validate_evaluation(self, required=False):
        if not isinstance(self.evaluation, dict) or set(self.evaluation) != {'cases'}:
            raise ValueError('evaluation requires generated cases')
        cases = self.evaluation['cases']
        if not isinstance(cases, list) or (required and not cases):
            raise ValueError('evaluation requires nonempty generated cases')
        ids = set()
        for case in cases:
            if (not isinstance(case, dict) or set(case) != {
                    'id', 'sourceRef', 'helperRefs', 'expected', 'requirementRefs'}):
                raise ValueError('case requires source, expected and requirementRefs')
            valid_id(case['id'])
            if case['id'] in ids:
                raise ValueError('duplicate evaluation case ID')
            ids.add(case['id'])
            names = validate_source_declaration({key: case[key] for key in ('sourceRef', 'helperRefs')})
            if set(names).intersection(('expert.py', 'scene.py', 'tasks.py')):
                raise ValueError('evaluation source must be independent from scene, task and expert sources')
            expected = case['expected']
            if (not isinstance(expected, dict) or not expected
                    or set(expected)-{'success', 'terminated', 'reward', 'details'}):
                raise ValueError('case expected must select generated result fields')
            if 'success' in expected and expected['success'] is not None and type(expected['success']) is not bool:
                raise ValueError('expected success must be boolean or null')
            if 'terminated' in expected and type(expected['terminated']) is not bool:
                raise ValueError('expected terminated must be boolean')
            if 'reward' in expected and expected['reward'] is not None and type(expected['reward']) not in (int, float):
                raise ValueError('expected reward must be number or null')
            refs = case['requirementRefs']
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ValueError('case requires requirement references')
            json.dumps(expected, allow_nan=False)

    def bind(self, world):
        world.validate()
        valid_id(self.id)
        if not isinstance(self.instruction, str):
            raise ValueError('task instruction must be text')
        if any(not isinstance(value, dict) for value in (self.bindings, self.parameters, self.measurements)):
            raise ValueError('task bindings, parameters and measurements must be objects')
        validate_recording(self.recording, world)
        if world.native:
            if set(self.measurements)-{'camera'}:
                raise ValueError('native measurements use frozen io streams and explicit camera permission')
            camera = self.measurements.get('camera')
            if camera is not None and (not isinstance(camera, dict)
                    or set(camera) != {'cameraIds', 'visibleToPolicy'}
                    or type(camera['visibleToPolicy']) is not bool or not isinstance(camera['cameraIds'], list)
                    or any(not isinstance(id, str) for id in camera['cameraIds'])
                    or len(set(camera['cameraIds'])) != len(camera['cameraIds'])
                    or set(camera['cameraIds'])-{row['cameraId'] for row in self.recording['cameras']}):
                raise ValueError('camera permission requires explicit visibleToPolicy and existing cameraIds')
        if (not isinstance(self.execution, dict) or set(self.execution) != {'physicalSeconds'}
                or type(self.execution['physicalSeconds']) not in (int, float)
                or not math.isfinite(self.execution['physicalSeconds']) or not 0 < self.execution['physicalSeconds']):
            raise ValueError('execution physicalSeconds must be finite and positive')
        names = validate_source_declaration(self.rubric)
        if set(names).intersection(('expert.py', 'scene.py', 'tasks.py')):
            raise ValueError('rubric source must be independent from scene, task and expert sources')
        self.validate_evaluation()
        for role, reference in self.bindings.items():
            valid_id(role)
            if isinstance(reference, str):
                id = reference
            elif isinstance(reference, dict) and set(reference) in ({'objectId'}, {'objectId', 'joint'}, {'frame'}):
                id = reference.get('objectId', reference.get('frame'))
                if 'objectId' in reference and world.nodes.get(id, {}).get('kind') == 'space':
                    raise ValueError('object binding requires a physical or visual object')
                if 'joint' in reference and (reference['joint'] is not True or not world.nodes.get(id, {}).get('joint')):
                    raise ValueError('joint binding requires the declared real object joint')
            else:
                raise ValueError('binding requires a stable node, object joint or frame reference')
            if not isinstance(id, str) or id not in world.nodes:
                raise ValueError('task binding references a missing scene node')
        result = asdict(self)
        json.dumps(result, allow_nan=False)
        return result
