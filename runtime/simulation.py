"""Frozen USD compilation and one Isaac 6 / PhysX execution path."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
from io import BytesIO
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import trimesh

from runtime.jobs import measure, shutdown_started
from runtime.records import Recorder, atomic_json

VERSION = '6.0.0.1'
WIDTH, HEIGHT = 1280, 720
TO_GLTF = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]])
_app = None
STATIC_CONTACT_RECIPE = 'static-unresolved-contact-v1'
COLLISION_RECIPE = {'version': 'physx-convex-local-v1', 'parameters': {
    'ErrorPercentage': 10., 'HullVertexLimit': 64, 'MaxConvexHulls': 32,
    'MinThickness': .0010000000474974513, 'ShrinkWrap': False, 'VoxelResolution': 500000}}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()




def _asset_scene(node):
    path = Path(node['path'])
    if path.suffix.lower() == '.glb':
        document, _ = _glb_document(path)
        from runtime.assets import _validate_gltf_materials
        _validate_gltf_materials(document, all_samplers=False)
    scene = trimesh.load(path, force='scene', process=False)
    if node['up_axis'] == 'Y':
        scene.apply_transform(trimesh.transformations.rotation_matrix(math.pi/2, [1, 0, 0]))
    return scene


def normalize_assets(world):
    """Fit measured Z-up geometry uniformly before task binding."""
    measured = {}
    for node in world.nodes.values():
        if node['kind'] != 'asset':
            continue
        key = (node['path'], node['up_axis'])
        if key not in measured:
            bounds = np.asarray(_asset_scene(node).bounds, dtype=float)
            if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[1] <= bounds[0]):
                raise ValueError(f"asset must have finite positive measured extents: {node['id']}")
            measured[key] = bounds
        bounds = measured[key]
        requested = np.asarray(node.get('requestedSize', node['size']), dtype=float)
        scale = float(np.min(requested/(bounds[1]-bounds[0])))
        actual = ((bounds[1]-bounds[0])*scale).tolist()
        node.update(requestedSize=requested.tolist(), actualSize=actual, size=actual,
                    sourceBounds=bounds.tolist(), scale=scale)
    return world


def _material(report_path):
    from PIL import Image
    path = Path(report_path).resolve()
    report = json.loads(path.read_text())
    if report.get('kind') != 'material':
        raise ValueError('material reference must be a prepared material report')
    images = {}
    for name, relative in report.get('maps', {}).items():
        if name not in ('baseColor', 'normal', 'metallicRoughness', 'emissive', 'occlusion'):
            raise ValueError(f'unsupported material map: {name}')
        target = (path.parent/relative).resolve()
        if Path(relative).is_absolute() or not target.is_relative_to(path.parent):
            raise ValueError('material texture references must stay inside the prepared directory')
        with Image.open(target) as image:
            images[name] = image.copy()
    factors = report.get('factors', {})
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=images.get('baseColor'), normalTexture=images.get('normal'),
        metallicRoughnessTexture=images.get('metallicRoughness'), emissiveTexture=images.get('emissive'),
        occlusionTexture=images.get('occlusion'), baseColorFactor=np.asarray(factors.get('baseColor', [1, 1, 1, 1]), dtype=float),
        metallicFactor=float(factors.get('metallic', 0)), roughnessFactor=float(factors.get('roughness', .7)),
        emissiveFactor=factors.get('emissive', [0, 0, 0]), alphaMode=report.get('alphaMode', 'OPAQUE'),
        alphaCutoff=float(report.get('alphaCutoff', .5)), doubleSided=bool(report.get('doubleSided', False)))
    material.emissiveStrength = float(report.get('emissiveStrength', 1))
    material.normalScale = float(report.get('normalScale', 1))
    material.occlusionStrength = float(report.get('occlusionStrength', 1))
    if material.normalScale != 1 or material.occlusionStrength != 1:
        raise ValueError('nondefault normalScale/occlusionStrength is unsupported by the fixed USD material path')
    return material


def _primitive_visual(node):
    size = np.asarray(node['size'])
    mesh = (trimesh.creation.cylinder(radius=size[0]/2, height=size[2], sections=32)
            if node['kind'] == 'cylinder' else trimesh.creation.box(extents=size))
    mesh.unmerge_vertices()
    uv = np.zeros((len(mesh.vertices), 2))
    period = node.get('texture_period_m')
    if period is not None:
        period = np.broadcast_to(np.asarray(period, dtype=float), (2,))
        if not np.isfinite(period).all() or np.any(period <= 0):
            raise ValueError('texture_period_m must be positive finite metres')
    for face, normal in zip(mesh.faces, mesh.face_normals):
        points = mesh.vertices[face]
        if node['kind'] == 'cylinder' and abs(normal[2]) < .5:
            angles = np.arctan2(points[:, 1], points[:, 0])/(2*math.pi) % 1
            if np.ptp(angles) > .5:
                angles[angles < .5] += 1
            value = np.column_stack((angles, points[:, 2]/size[2]+.5))
            if period is not None:
                value *= [math.pi*size[0]/period[0], size[2]/period[1]]
        else:
            axis = int(np.argmax(np.abs(normal)))
            axes = [value for value in range(3) if value != axis]
            value = points[:, axes]/(size[axes] if period is None else period)+.5
        uv[face] = value
    if period is None:
        uv *= np.asarray(node.get('uv_repeat', [1, 1]))
    material = (_material(node['material']) if node.get('material') else
                trimesh.visual.material.PBRMaterial(baseColorFactor=[1., 1., 1., 1.], metallicFactor=0, roughnessFactor=.7))
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
    return mesh


def _tint(mesh, color):
    if mesh.visual.kind != 'texture':
        mesh.visual = mesh.visual.to_texture()
    material = mesh.visual.material
    if hasattr(material, 'to_pbr'):
        material = material.to_pbr()
    material = deepcopy(material)
    factor = np.ones(4) if material.baseColorFactor is None else trimesh.visual.color.to_float(material.baseColorFactor)
    if material.alphaMode == 'OPAQUE':
        factor[3] = 1  # Opaque source alpha has no visual effect in glTF.
    rgba = factor*np.asarray(color)
    material.baseColorFactor = rgba
    if color[3] < 1 and material.alphaMode not in ('MASK', 'BLEND'):
        material.alphaMode = 'BLEND'
    mesh.visual.material = material
    return mesh


def _asset_parts(node):
    if 'scale' not in node:
        raise ValueError('assets must be normalized before task binding and compilation')
    center, parts = np.asarray(node['sourceBounds']).mean(axis=0), []
    for mesh in _asset_scene(node).dump():
        if not isinstance(mesh, trimesh.Trimesh):
            raise ValueError('prepared visual assets must contain triangle meshes')
        if isinstance(getattr(mesh.visual, 'material', None), trimesh.visual.material.MultiMaterial):
            materials, groups = mesh.visual.material.materials, mesh.visual.face_materials
            if groups is None:
                raise ValueError('multi-material mesh requires per-face indices')
            grouped = []
            for index in np.unique(groups):
                part = mesh.submesh([np.flatnonzero(groups == index)], append=True)
                part.visual.material = materials[index]
                grouped.append(part)
        else:
            grouped = [mesh]
        for part in grouped:
            part.apply_translation(-center)
            part.apply_scale(node['scale'])
            parts.append(_tint(part, node['color']))
    return parts


def _collision_geometry(parts):
    mesh = trimesh.util.concatenate(parts)
    points, indices = np.asarray(mesh.vertices, dtype='<f4'), np.asarray(mesh.faces, dtype='<i8').reshape(-1)
    if (points.ndim != 2 or points.shape[1] != 3 or not len(points) or not np.isfinite(points).all()
            or not len(indices) or len(indices) % 3 or indices.min() < 0 or indices.max() >= len(points)):
        raise ValueError('collision source must contain finite indexed triangles')
    return {'points': points.tolist(), 'indices': indices.tolist(), 'faceVertexCounts': [3]*(len(indices)//3)}


def _collision_geometry_digest(geometry):
    points, indices = np.asarray(geometry['points'], dtype='<f4'), np.asarray(geometry['indices'], dtype='<u4')
    return hashlib.sha256(struct.pack('<QQ', len(points), len(indices))+points.tobytes()+indices.tobytes()).hexdigest()


def collision_runtime_identity():
    """Read the concrete installed cooker identity without starting the SDK."""
    import tomllib
    if importlib.metadata.version('isaacsim') != VERSION or sys.version_info[:2] != (3, 12):
        raise RuntimeError('convex cooking requires locked Isaac 6.0.0.1 and Python 3.12')
    sdk = Path(sys.prefix)/'lib/python3.12/site-packages/isaacsim'
    patterns = ('extscache/omni.physx-*/config/extension.toml',
                'extscache/omni.physx-*/omni/physx/bindings/_physx.cpython-312-*.so',
                'extscache/omni.physx.cooking-*/bin/libomni.physx.cooking.plugin.so',
                'extscache/omni.usd.schema.physx-*/plugins/PhysxSchema/resources/generatedSchema.usda')
    files = {}
    for pattern in patterns:
        matches = list(sdk.glob(pattern))
        if len(matches) != 1:
            raise RuntimeError('missing or ambiguous native collision runtime: '+pattern)
        files[str(matches[0].relative_to(sdk))] = _digest(matches[0])
    extension = next(sdk.glob(patterns[0]))
    return {'isaacsim': VERSION, 'python': sys.version, 'physxExtension': tomllib.loads(extension.read_text())['package']['version'],
            'binaries': files}


def _collision_node_input(node, parts=None):
    enabled, mode = node.get('collision') is not False, node.get('collision_mode', 'box')
    result = {'enabled': enabled, 'mode': mode, 'kind': node['kind'], 'size': list(node['size']),
              'boxes': deepcopy(node.get('collision_boxes')), 'physicsMaterial': deepcopy(node.get('physics_material')),
              'coordinateSpec': 'metres; Z-up; centered body-local; uniform asset scale baked once'}
    if node['kind'] == 'asset':
        result.update(visualSha256=_digest(node['path']), normalization={'sourceUpAxis': node['up_axis'],
            'sourceBoundsZ': deepcopy(node['sourceBounds']), 'centerZ': np.asarray(node['sourceBounds']).mean(axis=0).tolist(),
            'uniformScale': node['scale'], 'requestedSizeM': list(node['requestedSize']), 'actualSizeM': list(node['actualSize'])})
    if enabled and mode in ('mesh', 'convex_decomposition'):
        if parts is None:
            parts = _asset_parts(node) if node['kind'] == 'asset' else [_primitive_visual(node)]
        result['geometrySha256'] = _collision_geometry_digest(_collision_geometry(parts))
    return result


def lock_collision_inputs(world, directory, static_contact_recipe=None):
    """Lock recipe and geometry before the worker computes the revision; World stays unchanged."""
    world.validate()
    nodes = {id: _collision_node_input(node) for id, node in world.nodes.items() if node['kind'] not in ('space', 'native')}
    convex = any(row['enabled'] and row['mode'] == 'convex_decomposition' for row in nodes.values())
    result = {'schema': 'sceneagent-collision-inputs-v1', 'nodes': nodes,
              'recipe': deepcopy(COLLISION_RECIPE) if convex else None,
              'nativeRuntime': collision_runtime_identity() if convex else None}
    if static_contact_recipe is not None:
        if static_contact_recipe != STATIC_CONTACT_RECIPE:
            raise ValueError('unsupported static contact recipe')
        result['staticContactRecipe'] = _static_contact_inputs(world, nodes)
    dependencies = Path(directory)/'dependencies'
    dependencies.mkdir(parents=True, exist_ok=True)
    atomic_json(dependencies/'collision-inputs.json', result)
    return result


def _contact_f32(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('static contact requires finite numeric values')
    try:
        raw = struct.pack('<f', value)
    except OverflowError:
        raise ValueError('static contact value exceeds Float32') from None
    result = struct.unpack('<f', raw)[0]
    if not math.isfinite(result):
        raise ValueError('static contact value exceeds Float32')
    return result, format(struct.unpack('<I', raw)[0], '08x')

def _lock_static_contact_shapes(shapes, meters_per_unit):
    """Fixed experimental policy over complete predeclared static shapes; no USD or physics calls."""
    if type(meters_per_unit) not in (int, float) or meters_per_unit != 1:
        raise ValueError('static contact recipe requires metersPerUnit 1')
    rows, paths = [], set()
    for shape in shapes:
        if set(shape) != {'path', 'type', 'approximation', 'geometrySha256', 'ownerClass', 'source'}:
            raise ValueError('invalid static contact shape declaration')
        path = shape['path']
        if (not isinstance(path, str) or not path.startswith('/World/') or '//' in path
                or any(part in ('.', '..', '') for part in path[1:].split('/'))
                or any(char in path for char in '*?[]|') or path in paths
                or shape['ownerClass'] != 'static' or shape['type'] not in ('Cube', 'Cylinder', 'Mesh')
                or shape['approximation'] != ('none' if shape['type'] == 'Mesh' else None)
                or not isinstance(shape['geometrySha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', shape['geometrySha256'])):
            raise ValueError('ineligible or duplicate static contact shape')
        paths.add(path)
        source, values, row = shape['source'], {}, deepcopy(shape)
        if not isinstance(source, dict) or set(source) != {'contact', 'rest'}:
            raise ValueError('static contact requires both source channels')
        for name, fallback in [('contact', .001), ('rest', 0.)]:
            channel = source[name]
            if not isinstance(channel, dict):
                raise ValueError('invalid static contact source')
            state = channel.get('state')
            if state == 'finite' and set(channel) == {'state', 'value', 'authored'} and type(channel['authored']) is bool:
                value, bits = _contact_f32(channel['value'])
                operation = 'preserve'
            elif channel == {'state': 'missing'} or (state == 'auto' and set(channel) == {'state', 'sentinel', 'authored'}
                    and channel['sentinel'] == '-inf' and type(channel['authored']) is bool):
                value, bits = _contact_f32(fallback)
                operation = 'set'
            else:
                raise ValueError('invalid static contact source or sentinel')
            values[name] = value
            row[name] = {'operation': operation, 'valueF32': value, 'bits': bits}
        if values['contact'] < 0 or values['contact'] <= values['rest']:
            raise ValueError('static contact must be nonnegative and strictly greater than signed rest')
        rows.append(row)
    return {'version': STATIC_CONTACT_RECIPE, 'metersPerUnit': 1, 'shapes': sorted(rows, key=lambda row: row['path'])}

def _validate_static_contact_inputs(locked):
    if not isinstance(locked, dict) or set(locked) != {'version', 'metersPerUnit', 'shapes'} or not isinstance(locked['shapes'], list):
        raise ValueError('invalid static contact input lock')
    try:
        shapes = [{key: row[key] for key in ('path', 'type', 'approximation', 'geometrySha256', 'ownerClass', 'source')}
                  for row in locked['shapes']]
        expected = _lock_static_contact_shapes(shapes, locked['metersPerUnit'])
    except (KeyError, TypeError) as error:
        raise ValueError('incomplete static contact input lock') from error
    if locked != expected:
        raise ValueError('static contact values, sources or operations differ from the fixed recipe')

def _static_contact_inputs(world, nodes):
    # This is the current writer's exact projection, not an asset/shape converter.
    def frame(path, ops=()):
        return [path, list(ops), False]
    shapes = []
    def add(path, kind, attributes, frames, approximation=None):
        geometry = {'attributes': attributes, 'frames': frames}
        shapes.append({'path': path, 'type': kind, 'approximation': approximation, 'ownerClass': 'static',
            'geometrySha256': hashlib.sha256(json.dumps(geometry, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            'source': {'contact': {'state': 'missing'}, 'rest': {'state': 'missing'}}})
    def mesh_attributes(geometry):
        return {**geometry, 'orientation': 'rightHanded', 'subdivisionScheme': 'none', 'doubleSided': False}
    ground = _collision_geometry([_primitive_visual({'kind': 'box', 'size': [20, 20, .02]})])
    add('/World/ground/visual', 'Mesh', mesh_attributes(ground), [frame('/World'),
        frame('/World/ground', [['xformOp:translate', 'double3', [0., 0., -.01]]]), frame('/World/ground/visual')], 'none')
    for id, row in nodes.items():
        node = world.nodes[id]
        if not row['enabled'] or node.get('mass', 0) > 0 or node.get('joint'):
            continue
        if row['mode'] == 'convex_decomposition':
            raise ValueError('static convex paths cannot be determined at prelock')
        root = '/World/objects/o_'+id.encode().hex()
        position, q = world.transform(id)
        parents = [frame('/World'), frame('/World/objects'), frame(root, [
            ['xformOp:translate', 'double3', list(map(float, position))], ['xformOp:orient', 'quatd', list(map(float, q))]])]
        path = root+'/collision'
        if row['mode'] == 'mesh':
            parts = _asset_parts(node) if node['kind'] == 'asset' else [_primitive_visual(node)]
            add(path, 'Mesh', mesh_attributes(_collision_geometry(parts)), parents+[frame(path)], 'none')
        elif row['boxes']:
            for index, box in enumerate(row['boxes']):
                child = path+'/box_'+str(index)
                ops = [['xformOp:translate', 'double3', list(map(float, box['at']))],
                    ['xformOp:orient', 'quatf', [_contact_f32(float(v))[0] for v in box['orientation']]],
                    ['xformOp:scale', 'float3', [_contact_f32(float(v))[0] for v in box['size']]]]
                add(child, 'Cube', {'size': 1.}, parents+[frame(path), frame(child, ops)])
        elif row['kind'] == 'cylinder':
            add(path, 'Cylinder', {'radius': node['size'][0]/2, 'height': node['size'][2], 'axis': 'Z'}, parents+[frame(path)])
        else:
            ops = [['xformOp:scale', 'float3', [_contact_f32(float(v))[0] for v in node['size']]]]
            add(path, 'Cube', {'size': 1.}, parents+[frame(path, ops)])
    return _lock_static_contact_shapes(shapes, 1)

def _static_contact_shapes(stage):
    """Read all enabled static CollisionAPI shapes; never add a rigid body or attach a view."""
    from pxr import Usd, UsdGeom, UsdPhysics
    if UsdGeom.GetStageMetersPerUnit(stage) != 1:
        raise ValueError('static contact recipe requires metersPerUnit 1')
    joints = {str(target) for prim in stage.Traverse() if prim.IsA(UsdPhysics.Joint)
              for relation in (UsdPhysics.Joint(prim).GetBody0Rel(), UsdPhysics.Joint(prim).GetBody1Rel())
              for target in relation.GetTargets()}
    rows = []
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        if not prim.HasAPI(UsdPhysics.CollisionAPI):
            continue
        chain, parent = [], prim
        while parent and not parent.IsPseudoRoot():
            chain.insert(0, parent); parent = parent.GetParent()
        if any(p.HasAPI(UsdPhysics.RigidBodyAPI) or p.HasAPI(UsdPhysics.ArticulationRootAPI) for p in chain):
            continue
        geometry_names = {'size', 'radius', 'height', 'axis', 'points', 'faceVertexCounts', 'faceVertexIndices',
                          'orientation', 'subdivisionScheme', 'doubleSided', 'physics:approximation', 'physics:collisionEnabled', 'xformOpOrder'}
        if any(attr.GetNumTimeSamples() for p in chain for attr in p.GetAttributes()
               if attr.GetName() in geometry_names or attr.GetName().startswith('xformOp:')):
            raise ValueError('time sampled static contact geometry is unsupported')
        if UsdPhysics.CollisionAPI(prim).GetCollisionEnabledAttr().Get() is not True:
            continue
        if any(str(p.GetPath()) in joints or p.IsInstance() or p.IsInstanceProxy() for p in chain):
            raise ValueError('unsupported joint/instance static contact ownership')
        kind, path = prim.GetTypeName(), str(prim.GetPath())
        if kind == 'Cube':
            attributes, approximation = {'size': UsdGeom.Cube(prim).GetSizeAttr().Get()}, None
        elif kind == 'Cylinder':
            shape = UsdGeom.Cylinder(prim)
            attributes, approximation = {'radius': shape.GetRadiusAttr().Get(), 'height': shape.GetHeightAttr().Get(),
                'axis': shape.GetAxisAttr().Get()}, None
        elif kind == 'Mesh':
            shape = UsdGeom.Mesh(prim)
            attributes = {'points': [[float(v) for v in p] for p in shape.GetPointsAttr().Get()],
                'indices': list(shape.GetFaceVertexIndicesAttr().Get()), 'faceVertexCounts': list(shape.GetFaceVertexCountsAttr().Get()),
                'orientation': shape.GetOrientationAttr().Get(), 'subdivisionScheme': shape.GetSubdivisionSchemeAttr().Get(),
                'doubleSided': shape.GetDoubleSidedAttr().Get()}
            approximation = (UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get()
                             if prim.HasAPI(UsdPhysics.MeshCollisionAPI) else 'none')
        else:
            raise ValueError('unsupported static contact shape type')
        frames = []
        for p in chain:
            xform, ops = UsdGeom.Xformable(p), []
            if not xform:
                raise ValueError('unsupported static contact coordinate ancestor')
            for op in xform.GetOrderedXformOps():
                if str(op.GetAttr().GetTypeName()) not in ('double3', 'float3', 'quatd', 'quatf'):
                    raise ValueError('unsupported static contact coordinate operation')
                value = op.Get()
                value = ([float(value.GetReal()), *map(float, value.GetImaginary())]
                         if 'quat' in str(op.GetAttr().GetTypeName()) else list(map(float, value)))
                ops.append([op.GetOpName(), str(op.GetAttr().GetTypeName()), value])
            frames.append([str(p.GetPath()), ops, xform.GetResetXformStack()])
        source = {}
        for name in ('contact', 'rest'):
            attr = prim.GetAttribute('physxCollision:'+name+'Offset')
            if not attr:
                source[name] = {'state': 'missing'}
            else:
                if attr.GetNumTimeSamples():
                    raise ValueError('time sampled static contact offsets are unsupported')
                value, authored = attr.Get(), attr.HasAuthoredValueOpinion()
                if value == -math.inf:
                    source[name] = {'state': 'auto', 'sentinel': '-inf', 'authored': authored}
                else:
                    _contact_f32(value)
                    source[name] = {'state': 'finite', 'value': value, 'authored': authored}
        if any(prim.GetAttribute('newton:'+name) and prim.GetAttribute('newton:'+name).HasAuthoredValueOpinion()
               for name in ('contactMargin', 'contactGap')):
            raise ValueError('Newton fallback static contact declarations are unsupported')
        geometry = {'attributes': attributes, 'frames': frames}
        rows.append({'path': path, 'type': kind, 'approximation': approximation, 'ownerClass': 'static',
            'geometrySha256': hashlib.sha256(json.dumps(geometry, sort_keys=True, allow_nan=False).encode()).hexdigest(), 'source': source})
    return sorted(rows, key=lambda row: row['path'])

def _consume_static_contacts(stage, locked, author=False):
    if locked is None:
        return  # Historical inputs are read-only and never adopt the experiment.
    _validate_static_contact_inputs(locked)
    actual = _static_contact_shapes(stage)
    expected = [{key: row[key] for key in ('path', 'type', 'approximation', 'geometrySha256', 'ownerClass', 'source')}
                for row in locked['shapes']]
    if not author:
        for source, row in zip(expected, locked['shapes']):
            for name in ('contact', 'rest'):
                if row[name]['operation'] == 'set':
                    source['source'] = deepcopy(source['source'])
                    source['source'][name] = {'state': 'finite', 'value': row[name]['valueF32'], 'authored': True}
    if actual != expected:
        raise ValueError('static contact shape paths/geometry/ownership/source values differ from prelock')
    if author:
        from pxr import PhysxSchema
        for row in locked['shapes']:
            if any(row[name]['operation'] == 'set' for name in ('contact', 'rest')):
                api = PhysxSchema.PhysxCollisionAPI.Apply(stage.GetPrimAtPath(row['path']))
                if row['contact']['operation'] == 'set': api.CreateContactOffsetAttr(row['contact']['valueF32'])
                if row['rest']['operation'] == 'set': api.CreateRestOffsetAttr(row['rest']['valueF32'])
        _consume_static_contacts(stage, locked)

def _single_actor_contact_max(view, actor_path):
    """Public all-shapes actor contract only; does not establish geometric or motion qualification."""
    if (view is None or not view.check() or type(view.count) is not int or view.count != 1
            or list(view.prim_paths) != [actor_path] or type(view.max_shapes) is not int or view.max_shapes <= 0):
        raise ValueError('single actor offset view is missing, invalid or bound to a different actor')
    arrays = []
    for getter in (view.get_contact_offsets, view.get_rest_offsets):
        raw = getter()
        value = np.array(raw.numpy() if hasattr(raw, 'numpy') else raw, copy=True)
        if value.shape != (1, view.max_shapes) or value.dtype.kind not in 'fi' or not np.isfinite(value).all():
            raise ValueError('single actor offsets require complete finite numeric rows')
        arrays.append(value)
    contact, rest = arrays
    if np.any(contact < 0) or np.any(contact <= rest):
        raise ValueError('single actor contact must be nonnegative and strictly greater than signed rest')
    return {'actorPath': actor_path, 'contactOffsets': contact.tolist(), 'restOffsets': rest.tolist(),
        'maxContactOffsetM': float(contact.max()), 'offsetScope': 'public_single_actor_all_shapes_contract',
        'slotShapeBinding': 'unknown', 'geometryCoverage': 'unverified', 'qualification': False}


def _validate_convex_hulls(hulls):
    if not isinstance(hulls, list) or not 1 <= len(hulls) <= COLLISION_RECIPE['parameters']['MaxConvexHulls']:
        raise ValueError('convex cooking returned no hulls or exceeded the locked hull limit')
    for hull in hulls:
        points = np.asarray(hull.get('vertices'), dtype=float)
        indices, polygons = hull.get('indices'), hull.get('polygons')
        if (points.ndim != 2 or points.shape[1] != 3 or not 4 <= len(points) <= COLLISION_RECIPE['parameters']['HullVertexLimit']
                or not np.isfinite(points).all() or np.linalg.matrix_rank(points-points[0]) < 3
                or not isinstance(indices, list) or not indices or not isinstance(polygons, list) or not polygons
                or any(not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(points) for index in indices)):
            raise ValueError('convex cooking returned malformed or degenerate geometry')
        edges, used = {}, []
        for polygon in polygons:
            start, count = polygon.get('indexBase'), polygon.get('numVertices')
            plane = np.asarray(polygon.get('plane'), dtype=float)
            if (not isinstance(start, int) or not isinstance(count, int) or start < 0 or count < 3
                    or start+count > len(indices) or plane.shape != (4,) or not np.isfinite(plane).all()):
                raise ValueError('convex polygon is malformed')
            face = indices[start:start+count]
            if len(set(face)) != len(face):
                raise ValueError('convex polygon repeats vertices')
            used.extend(range(start, start+count))
            for a, b in zip(face, face[1:]+face[:1]):
                edge = tuple(sorted((a, b)))
                edges[edge] = edges.get(edge, 0)+1
        if sorted(used) != list(range(len(indices))) or any(count != 2 for count in edges.values()):
            raise ValueError('convex geometry is not a closed polygon surface')


def _write_collision_mesh(stage, path, geometry, approximation):
    from pxr import Gf, UsdGeom, UsdPhysics
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr([Gf.Vec3f(*point) for point in geometry['points']])
    mesh.CreateFaceVertexCountsAttr(geometry['faceVertexCounts'])
    mesh.CreateFaceVertexIndicesAttr(geometry['indices'])
    mesh.CreateSubdivisionSchemeAttr('none')
    mesh.CreatePurposeAttr(UsdGeom.Tokens.guide)
    mesh.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    UsdPhysics.CollisionAPI.Apply(mesh.GetPrim())
    UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr(approximation)
    return mesh.GetPrim()


@measure('collisionCooking')
def _cook_convex_collision(geometry, directory, locked):
    """Consume the reviewed native callback in identity mesh-local coordinates; never simulate."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stage_cache, stage = None, None
    holder, task = {}, None
    try:
        app = _ensure_app()
        from pxr import PhysxSchema, Usd, UsdGeom, UsdPhysics, UsdUtils, PhysicsSchemaTools
        import omni.physx
        from omni.physx.bindings._physx import PhysxCollisionRepresentationResult
        stage = Usd.Stage.CreateInMemory()
        stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, '/Cook').GetPrim())
        UsdGeom.SetStageMetersPerUnit(stage, 1)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdPhysics.Scene.Define(stage, '/Cook/physicsScene')
        prim = _write_collision_mesh(stage, '/Cook/source', geometry, 'convexDecomposition')
        UsdPhysics.RigidBodyAPI.Apply(prim)
        api = PhysxSchema.PhysxConvexDecompositionCollisionAPI.Apply(prim)
        for name, value in locked['recipe']['parameters'].items():
            getattr(api, 'Create'+name+'Attr')(value)
            if getattr(api, 'Get'+name+'Attr')().Get() != value:
                raise ValueError('native convex recipe differs from the locked value: '+name)
        stage_cache = UsdUtils.StageCache.Get()
        stage_id = stage_cache.Insert(stage).ToLongInt()
        stage.GetRootLayer().Export(str(directory/'cook-source.usda'))
        cooker = omni.physx.get_physx_cooking_interface()
        def callback(result, convexes):
            try:
                hulls = [{'vertices': [[float(v[0]), float(v[1]), float(v[2])] for v in hull.vertices],
                    'indices': [int(index) for index in hull.indices],
                    'polygons': [{'indexBase': int(p.index_base), 'numVertices': int(p.num_vertices),
                                  'plane': [float(value) for value in p.plane]} for p in hull.polygons]} for hull in (convexes or [])]
                holder.update(result=result, hulls=hulls)
            except BaseException as error:
                holder['error'] = repr(error)
        task = cooker.request_convex_collision_representation(stage_id=stage_id,
            collision_prim_id=PhysicsSchemaTools.sdfPathToInt('/Cook/source'), run_asynchronously=True, on_result=callback)
        deadline = time.monotonic()+90
        while not holder and time.monotonic() < deadline:
            app.update()
        if not holder:
            cooker.cancel_collision_representation_task(task=task, invoke_callback=False)
            task = None
            raise TimeoutError('native convex collision callback timed out')
        if holder.get('error') or holder.get('result') != PhysxCollisionRepresentationResult.RESULT_VALID:
            raise RuntimeError('native convex collision failed: '+repr(holder))
        _validate_convex_hulls(holder['hulls'])
        atomic_json(directory/'cook-result.json', {'result': 'RESULT_VALID', 'hulls': holder['hulls'],
                    'geometrySha256': _collision_geometry_digest(geometry), 'recipe': locked['recipe'],
                    'nativeRuntime': locked['nativeRuntime'], 'coordinateSpace': 'mesh_local_unscaled',
                    'checkedScope': 'native cooking result and serialization; physical contact unverified'})
        return holder['hulls']
    except BaseException as error:
        atomic_json(directory/'cook-failure.json', {'error': repr(error), 'lastCallback': repr(holder),
                    'geometrySha256': _collision_geometry_digest(geometry), 'recipe': locked.get('recipe'),
                    'nativeRuntime': locked.get('nativeRuntime'), 'fallbackUsed': False})
        raise
    finally:
        if task is not None and not holder:
            cooker.cancel_collision_representation_task(task=task, invoke_callback=False)
        if stage_cache is not None and stage is not None:
            stage_cache.Erase(stage)


def _write_physics_material(stage, path, values, shapes):
    if values is None:
        return
    from pxr import UsdPhysics, UsdShade
    material = UsdShade.Material.Define(stage, path)
    api = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    for name, suffix in [('static_friction', 'StaticFriction'), ('dynamic_friction', 'DynamicFriction'), ('restitution', 'Restitution')]:
        if name in values:
            getattr(api, 'Create'+suffix+'Attr')(float(values[name]))
    for prim in shapes:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, materialPurpose='physics')


def verify_frozen_convex_collision(path, hulls):
    from pxr import Sdf, Usd
    try:
        layer = Sdf.Layer.FindOrOpen(str(path))
        if layer is None:
            raise ValueError('frozen convex collision USD cannot be opened')
        layer.Reload(True)
        stage = Usd.Stage.Open(str(path))
        if stage is None:
            raise ValueError('frozen convex collision USD cannot be opened')
    except Exception as error:
        raise ValueError('frozen convex collision USD cannot be opened') from error
    if str(stage.GetDefaultPrim().GetPath()) != '/Collision':
        raise ValueError('frozen convex collision default root differs')
    verify_convex_collision(stage, '/Collision', hulls)


def verify_convex_collision(stage, root_path, hulls):
    """The same returned hulls must be consumed in both the standalone and composed layers."""
    from pxr import UsdGeom, UsdPhysics
    root = stage.GetPrimAtPath(root_path)
    if (not root or not root.IsA(UsdGeom.Xform) or UsdGeom.GetStageMetersPerUnit(stage) != 1
            or UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z or UsdGeom.Xformable(root).GetOrderedXformOps()
            or UsdGeom.Xformable(root).GetResetXformStack()
            or {prim.GetName() for prim in root.GetChildren()} != {'h_'+str(i) for i in range(len(hulls))}):
        raise ValueError('frozen convex collision has different coordinates or hull coverage')
    for index, hull in enumerate(hulls):
        prim = stage.GetPrimAtPath(root_path+'/h_'+str(index))
        if (not prim.IsA(UsdGeom.Mesh) or not prim.HasAPI(UsdPhysics.CollisionAPI)
                or UsdPhysics.CollisionAPI(prim).GetCollisionEnabledAttr().Get() is not True
                or UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() != 'convexHull'
                or UsdGeom.Xformable(prim).GetOrderedXformOps() or UsdGeom.Xformable(prim).GetResetXformStack()):
            raise ValueError('frozen convex collision hull lacks its actual collision shape')
        mesh = UsdGeom.Mesh(prim)
        indices = [i for p in hull['polygons'] for i in hull['indices'][p['indexBase']:p['indexBase']+p['numVertices']]]
        if (not np.array_equal(np.asarray(mesh.GetPointsAttr().Get(), dtype='<f4'), np.asarray(hull['vertices'], dtype='<f4'))
                or list(mesh.GetFaceVertexIndicesAttr().Get()) != indices
                or list(mesh.GetFaceVertexCountsAttr().Get()) != [p['numVertices'] for p in hull['polygons']]):
            raise ValueError('frozen convex collision vertices or topology differ from the actual callback')


def _write_node_collision(stage, object_path, node, parts, directory, locked, cache=None, native_runtime=None):
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
    row = _collision_node_input(node, parts)
    if locked.get('nodes', {}).get(node['id']) != row:
        raise ValueError('locked collision inputs differ from the current node or geometry')
    result = {**row, 'shapeCount': 0, 'shapePaths': [], 'physicalContactVerified': False}
    if not row['enabled']:
        return result
    mode, path, shapes = row['mode'], object_path+'/collision', []
    if mode == 'box':
        boxes = node.get('collision_boxes')
        if boxes:
            UsdGeom.Xform.Define(stage, path)
            for index, box in enumerate(boxes):
                shape = UsdGeom.Cube.Define(stage, path+'/box_'+str(index))
                shape.CreateSizeAttr(1)
                shape.AddTranslateOp().Set(Gf.Vec3d(*box['at']))
                q = box['orientation']
                shape.AddOrientOp().Set(Gf.Quatf(q[0], Gf.Vec3f(*q[1:])))
                shape.AddScaleOp().Set(Gf.Vec3f(*box['size']))
                shapes.append(shape.GetPrim())
        elif node['kind'] == 'cylinder':
            shape = UsdGeom.Cylinder.Define(stage, path)
            shape.CreateRadiusAttr(node['size'][0]/2)
            shape.CreateHeightAttr(node['size'][2])
            shape.CreateAxisAttr('Z')
            shapes.append(shape.GetPrim())
        else:
            shape = UsdGeom.Cube.Define(stage, path)
            shape.CreateSizeAttr(1)
            shape.AddScaleOp().Set(Gf.Vec3f(*node['size']))
            shapes.append(shape.GetPrim())
        for prim in shapes:
            UsdGeom.Imageable(prim).CreatePurposeAttr(UsdGeom.Tokens.guide)
            UsdGeom.Imageable(prim).CreateVisibilityAttr(UsdGeom.Tokens.invisible)
            UsdPhysics.CollisionAPI.Apply(prim)
    elif mode == 'mesh':
        shapes.append(_write_collision_mesh(stage, path, _collision_geometry(parts), 'none'))
    else:
        current_runtime = native_runtime if native_runtime is not None else collision_runtime_identity()
        if locked.get('recipe') != COLLISION_RECIPE or locked.get('nativeRuntime') != current_runtime:
            raise ValueError('locked collision cooking recipe or runtime changed')
        geometry = _collision_geometry(parts)
        key = hashlib.sha256(json.dumps({'geometry': row['geometrySha256'], 'recipe': locked['recipe'],
            'runtime': locked['nativeRuntime']}, sort_keys=True, allow_nan=False).encode()).hexdigest()
        output = Path(directory)/'collisions'/key
        frozen_path = output/'collision.usda'
        if cache is None or key not in cache:
            hulls = _cook_convex_collision(geometry, output, locked)
            _validate_convex_hulls(hulls)
            output.mkdir(parents=True, exist_ok=True)
            frozen = Usd.Stage.CreateNew(str(frozen_path))
            frozen.SetDefaultPrim(UsdGeom.Xform.Define(frozen, '/Collision').GetPrim())
            UsdGeom.SetStageMetersPerUnit(frozen, 1)
            UsdGeom.SetStageUpAxis(frozen, UsdGeom.Tokens.z)
            for index, hull in enumerate(hulls):
                indices = [i for p in hull['polygons'] for i in hull['indices'][p['indexBase']:p['indexBase']+p['numVertices']]]
                _write_collision_mesh(frozen, '/Collision/h_'+str(index), {'points': hull['vertices'], 'indices': indices,
                    'faceVertexCounts': [p['numVertices'] for p in hull['polygons']]}, 'convexHull')
            frozen.GetRootLayer().Save()
            frozen = None
            verify_frozen_convex_collision(frozen_path, hulls)
            if cache is not None:
                cache[key] = hulls
        else:
            hulls = cache[key]
        collision = UsdGeom.Xform.Define(stage, path)
        root_path = stage.GetRootLayer().realPath
        reference = os.path.relpath(frozen_path, Path(root_path).parent) if root_path else str(frozen_path)
        collision.GetPrim().GetReferences().AddReference(reference, '/Collision')
        shapes = [stage.GetPrimAtPath(path+'/h_'+str(index)) for index in range(len(hulls))]
        returned = np.concatenate([np.asarray(hull['vertices']) for hull in hulls])
        source = np.asarray(geometry['points'])
        result.update(hullCount=len(hulls), artifact=str(frozen_path.relative_to(directory)), artifactSha256=_digest(frozen_path),
            cookResultArtifact=str((output/'cook-result.json').relative_to(directory)),
            returnedBoundsM=[returned.min(axis=0).tolist(), returned.max(axis=0).tolist()],
            maximumBoundsDifferenceM=float(np.max(np.abs(np.array([returned.min(axis=0), returned.max(axis=0)])-
                                                          np.array([source.min(axis=0), source.max(axis=0)])))))
    _write_physics_material(stage, object_path+'/physics_material', node.get('physics_material'), shapes)
    result.update(shapeCount=len(shapes), shapePaths=[str(prim.GetPath()) for prim in shapes])
    return result


def _pose(position, quaternion):
    result = trimesh.transformations.quaternion_matrix(quaternion)
    result[:3, 3] = position
    return result


def _usd_world(stage, path):
    from pxr import UsdGeom
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsA(UsdGeom.Xformable):
        raise ValueError(f'world transform is missing: {path}')
    return np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)).T


def _pose_error(actual, expected):
    cosine = (np.trace(actual[:3, :3].T@expected[:3, :3])-1)/2
    return float(np.linalg.norm(actual[:3, 3]-expected[:3, 3])), math.acos(float(np.clip(cosine, -1, 1)))






_mdl_configuration = None


def configure_native_materials(directory, audit):
    """Pure file/JSON check before Kit; a live renderer cannot change its resolution roots."""
    global _mdl_configuration
    from runtime import mdl
    report = mdl.verify(audit, directory)
    if report is None:
        return
    configuration = {'roots':[str((Path(directory)/name).resolve()) for name in report['roots']],
                     'sdk':report['sdk']}
    if _app is not None and configuration != _mdl_configuration:
        raise ValueError('a live SDK cannot switch frozen MDL roots; use a fresh native process')
    _mdl_configuration = configuration


def _ensure_app():
    global _app
    if _app is None:
        with measure('sdkInitialization'):
            if importlib.metadata.version('isaacsim') != VERSION or sys.version_info[:2] != (3, 12):
                raise RuntimeError('requires locked Isaac 6.0.0.1 and Python 3.12')
            if _mdl_configuration is not None:
                os.environ.pop('MDL_USER_PATH', None)
                os.environ.pop('MDL_SYSTEM_PATH', None)
            from isaacsim import SimulationApp
            gpu = int(os.environ['SCENEAGENT_GPU'])
            _app = SimulationApp({'headless': True, 'renderer': 'RealTimePathTracing',
                'active_gpu': gpu, 'physics_gpu': 0, 'multi_gpu': False, 'max_gpu_count': 1,
                'width': WIDTH, 'height': HEIGHT, 'anti_aliasing': 2, 'enable_crashreporter': False,
                'extra_args': ['--/app/settings/persistent=false', '--/app/useFabricSceneDelegate=false',
                               '--/rtx/post/motionblur/enabled=false', '--/rtx/post/tonemap/autoExposureMode=0',
                               *(['--/app/mdl/additionalSystemPaths='+json.dumps(_mdl_configuration['roots'])]
                                 if _mdl_configuration is not None else [])]})
            import isaacsim.core.experimental.utils.app as app_utils
            app_utils.enable_extension('isaacsim.sensors.experimental.rtx', enabled=True)
            if _app.config['fast_shutdown'] is not True:
                raise RuntimeError('official default fast shutdown must remain enabled')
    return _app


@measure('finish')
def finish():
    if _app is not None:
        import omni.replicator.core as rep
        rep.orchestrator.wait_until_complete()
    return {'replicatorCompleted': True, 'applicationStarted': _app is not None,
            'terminationMode': 'official_fast_shutdown' if _app is not None else 'no_sdk_application',
            'fullVendorCleanupVerified': False}


def shutdown(exit_code):
    shutdown_started(_app is not None)
    if _app is not None:
        _app.close(wait_for_replicator=True, skip_cleanup=False, exit_code=int(exit_code))
        raise RuntimeError('official fast shutdown unexpectedly returned')


def _setup_stage(path):
    _ensure_app()
    import isaacsim.core.experimental.utils.stage as stage_utils
    from isaacsim.core.simulation_manager import SimulationManager as Sim
    from isaacsim.core.rendering_manager import RenderingManager as Render
    from pxr import Sdf
    layer = Sdf.Layer.FindOrOpen(str(path))
    if layer:
        layer.Reload(True)
    opened, stage = stage_utils.open_stage(str(path))
    if not opened or not Sim.switch_physics_engine('physx'):
        raise RuntimeError('failed to open frozen PhysX USD')
    if Sim.get_active_physics_engine() != 'physx':
        raise RuntimeError('only PhysX is permitted')
    Sim.setup_simulation(dt=.002, device='cpu')
    Sim.enable_gpu_dynamics(False)
    Sim.set_broadphase_type('MBP')
    Sim.enable_fabric(False)
    import carb
    from omni.physx.bindings import _physx
    settings = carb.settings.get_settings()
    settings.set_bool(_physx.SETTING_UPDATE_TO_USD_USING_XFORM_COMMON_API, False)
    if settings.get_as_bool(_physx.SETTING_UPDATE_TO_USD_USING_XFORM_COMMON_API) is not False:
        raise RuntimeError('official default quaternion USD output was not selected')
    Render.set_dt(.04)
    return stage


@measure('stageClose')
def _close_stage():
    import isaacsim.core.experimental.utils.app as app_utils
    import isaacsim.core.experimental.utils.stage as stage_utils
    app_utils.stop(commit=True)
    if not stage_utils.close_stage():
        raise RuntimeError('could not close the prior stage')


def _write_material(stage, path, material, directory):
    from pxr import Gf, Sdf, UsdShade
    surface = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path+'/surface')
    shader.CreateIdAttr('UsdPreviewSurface')
    shader.CreateOutput('surface', Sdf.ValueTypeNames.Token)
    surface.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
    factor = (np.ones(4) if material.baseColorFactor is None else
              trimesh.visual.color.to_float(material.baseColorFactor))
    mode = material.alphaMode or 'OPAQUE'
    cutoff = float(material.alphaCutoff) if material.alphaCutoff is not None else .5
    if mode not in ('OPAQUE', 'MASK', 'BLEND'):
        raise ValueError('unsupported source alpha mode')
    alpha_enabled = mode == 'BLEND' or (mode == 'MASK' and cutoff > 0)
    effective_alpha = float(factor[3]) if alpha_enabled else 1.
    emissive = np.array(material.emissiveFactor if material.emissiveFactor is not None else [0, 0, 0], dtype=float)*getattr(material, 'emissiveStrength', 1)
    rough = material.roughnessFactor if material.roughnessFactor is not None else 1
    metal = material.metallicFactor if material.metallicFactor is not None else 1
    values = [('diffuseColor', Sdf.ValueTypeNames.Color3f, Gf.Vec3f(*factor[:3])),
              ('roughness', Sdf.ValueTypeNames.Float, float(rough)), ('metallic', Sdf.ValueTypeNames.Float, float(metal)),
              ('opacity', Sdf.ValueTypeNames.Float, effective_alpha),
              ('emissiveColor', Sdf.ValueTypeNames.Color3f, Gf.Vec3f(*map(float, emissive))),
              ('opacityThreshold', Sdf.ValueTypeNames.Float, cutoff if mode == 'MASK' else 0)]
    for name, value_type, value in values:
        shader.CreateInput(name, value_type).Set(value)
    shader.GetPrim().SetCustomDataByKey('sceneagent:alphaMode', mode)
    reader = UsdShade.Shader.Define(stage, path+'/st')
    reader.CreateIdAttr('UsdPrimvarReader_float2')
    reader.CreateInput('varname', Sdf.ValueTypeNames.Token).Set('st')
    reader.CreateOutput('result', Sdf.ValueTypeNames.Float2)
    color_connections = [('diffuseColor', 'rgb', Sdf.ValueTypeNames.Color3f)]
    if alpha_enabled:
        color_connections.append(('opacity', 'a', Sdf.ValueTypeNames.Float))
    maps = [('baseColor', material.baseColorTexture, 'sRGB', (*factor[:3], effective_alpha), (0, 0, 0, 0), color_connections),
            ('metallicRoughness', material.metallicRoughnessTexture, 'raw', (1, rough, metal, 1), (0, 0, 0, 0),
             [('roughness', 'g', Sdf.ValueTypeNames.Float), ('metallic', 'b', Sdf.ValueTypeNames.Float)]),
            ('normal', material.normalTexture, 'raw', (2, 2, 2, 1), (-1, -1, -1, 0), [('normal', 'rgb', Sdf.ValueTypeNames.Normal3f)]),
            ('occlusion', material.occlusionTexture, 'raw', (1, 1, 1, 1), (0, 0, 0, 0), [('occlusion', 'r', Sdf.ValueTypeNames.Float)]),
            ('emissive', material.emissiveTexture, 'sRGB', (*emissive, 1), (0, 0, 0, 0), [('emissiveColor', 'rgb', Sdf.ValueTypeNames.Color3f)])]
    for name, image, color_space, scale, bias, connections in maps:
        if image is None:
            continue
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        content = buffer.getvalue()
        target = directory/('texture-'+hashlib.sha256(content).hexdigest()[:24]+'.png')
        target.write_bytes(content)
        texture = UsdShade.Shader.Define(stage, path+'/'+name)
        texture.CreateIdAttr('UsdUVTexture')
        texture.CreateInput('file', Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(os.path.relpath(target, stage.GetRootLayer().realPath and Path(stage.GetRootLayer().realPath).parent)))
        texture.CreateInput('sourceColorSpace', Sdf.ValueTypeNames.Token).Set(color_space)
        texture.CreateInput('scale', Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*map(float, scale)))
        texture.CreateInput('bias', Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*map(float, bias)))
        texture.CreateInput('wrapS', Sdf.ValueTypeNames.Token).Set('repeat')
        texture.CreateInput('wrapT', Sdf.ValueTypeNames.Token).Set('repeat')
        texture.CreateInput('st', Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(), 'result')
        for input_name, channel, value_type in connections:
            texture.CreateOutput(channel, Sdf.ValueTypeNames.Float3 if channel == 'rgb' else Sdf.ValueTypeNames.Float)
            shader.CreateInput(input_name, value_type).ConnectToSource(texture.ConnectableAPI(), channel)
    return surface


def _write_mesh(stage, path, mesh, directory):
    from pxr import Gf, Sdf, UsdGeom, UsdShade
    usd = UsdGeom.Mesh.Define(stage, path)
    usd.CreatePointsAttr([Gf.Vec3f(*point) for point in mesh.vertices])
    usd.CreateFaceVertexCountsAttr([3]*len(mesh.faces))
    usd.CreateFaceVertexIndicesAttr(mesh.faces.reshape(-1).tolist())
    usd.CreateNormalsAttr([Gf.Vec3f(*normal) for normal in mesh.vertex_normals])
    usd.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    usd.CreateSubdivisionSchemeAttr('none')
    usd.CreateDoubleSidedAttr(bool(mesh.visual.material.doubleSided))
    uv = getattr(mesh.visual, 'uv', None)
    if uv is not None:
        st = UsdGeom.PrimvarsAPI(usd).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        st.Set([Gf.Vec2f(*value) for value in uv])
    material = _write_material(stage, path+'_material', mesh.visual.material, directory)
    UsdShade.MaterialBindingAPI.Apply(usd.GetPrim()).Bind(material)
    return usd


def _camera_points(world, id, stage=None):
    import itertools
    from runtime.scene import quaternion_rotate
    node = world.nodes[id]
    if node.get('primPath') and world.native:
        from pxr import Usd, UsdGeom
        if stage is None or not stage.GetPrimAtPath(node['primPath']):
            raise ValueError('native camera framing requires its frozen stage binding')
        bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]).ComputeWorldBound(
            stage.GetPrimAtPath(node['primPath'])).ComputeAlignedRange()
        return [np.asarray(bounds.GetMin()), np.asarray(bounds.GetMax())] if not bounds.IsEmpty() else [_usd_world(stage, node['primPath'])[:3, 3]]
    bounds = node.get('bounds')
    if not bounds and node.get('size'):
        half = np.asarray(node['size'])/2
        bounds = {'min': -half, 'max': half}
    if world.native and stage is not None:
        matrix = recording_camera_pose(world, {'frame':id,'at':[0,0,0],'orientation':[1,0,0,0]}, stage=stage)
        return [(matrix@np.r_[point,1])[:3] for point in itertools.product(*zip(bounds['min'], bounds['max']))] if bounds else [matrix[:3,3]]
    position, quaternion = world.transform(id)
    return ([np.asarray(position)+quaternion_rotate(quaternion, point)
             for point in itertools.product(*zip(bounds['min'], bounds['max']))]
            if bounds else [np.asarray(position)])


def camera_for(world, scope=None, task=None, *, stage=None):
    if scope is not None and task is not None:
        raise ValueError('camera must select either a node scope or a task')
    points = []
    references = ([task.bindings[role] for role in task.measurements.get('camera', {}).get('bindings', list(task.bindings))]
                  if task else [])
    ids = ([value if isinstance(value, str) else value.get('objectId', value.get('frame'))
            for value in references] if task else [node['id'] for node in world.context(scope)['subtree']]
           if scope is not None else list(world.nodes))
    for id in sorted(set(ids)):
        if world.native and world.nodes[id]['kind'] == 'space' and not world.nodes[id].get('bounds'):
            continue
        points.extend(_camera_points(world, id, stage))
    if not points:
        if not world.native or stage is None:
            raise ValueError('camera framing has no source geometry')
        from pxr import Usd, UsdGeom
        bounds = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]).ComputeWorldBound(
            stage.GetPrimAtPath('/World')).ComputeAlignedRange()
        if bounds.IsEmpty():
            raise ValueError('native stage has no frameable geometry')
        points = [np.asarray(bounds.GetMin()), np.asarray(bounds.GetMax())]
    low, high = np.min(points, axis=0), np.max(points, axis=0)
    hidden = []
    if scope is not None:
        cut_height = float(low[2]+.75*(high[2]-low[2]))
        hidden = [id for id, node in world.nodes.items() if node.get('role') == 'enclosure'
                  and not node.get('mass') and not node.get('joint')
                  and np.max(_camera_points(world, id, stage), axis=0)[2] > cut_height]
    return {'lookat': ((low+high)/2).tolist(), 'distance': max(1.8, float(np.linalg.norm(high-low))*1.1),
            'azimuth': 135, 'elevation': -30, 'width': WIDTH, 'height': HEIGHT,
            'scope': 'task:'+task.id if task else scope or 'whole_world',
            'primPath': '/World/task_camera_'+task.id.encode().hex() if task else '/World/camera',
            'viewMode': 'visual_cutaway' if hidden else 'full_geometry', 'hiddenVisualNodeIds': hidden,
            'visibilityScope': 'camera_framing; disclosed visual cutaway; occlusion and semantic completeness are not checked'}


def recording_camera_pose(world, declaration, state=None, stage=None, *, io=None, array_directory=None):
    """Compose authored local frames with an actual native stage or frozen raw pose stream."""
    def native_pose(path):
        if stage is not None:
            if not stage.GetPrimAtPath(path):
                raise ValueError('camera/construction frame has no native prim: '+path)
            return _usd_world(stage, path)
        from runtime.records import read_array
        candidates = [name for name, spec in (io or {}).get('streams', {}).items()
            if spec['source'].get('reader') == 'rigid_pose'
            and spec['source'].get('path') == path and spec.get('frame') == 'world_wxyz'
            and spec.get('origin') == 'native_output' and spec.get('shape') == [7]]
        if state is None or len(candidates) != 1 or candidates[0] not in state.get('streams', {}):
            raise ValueError('camera mount has no unambiguous actual native pose stream')
        name = candidates[0]
        status = state.get('streamStatus', {}).get(name, {})
        if status.get('status') != 'available' or abs(status.get('sampleTime', -1)-state['time']) > 1e-7:
            raise ValueError('camera mount pose was not sampled at the actual frame time')
        value = state['streams'][name]
        pose = read_array(array_directory, value) if isinstance(value, dict) else np.asarray(value)
        if pose.shape != (7,) or not np.isfinite(pose).all() or abs(np.linalg.norm(pose[3:])-1)>1e-5:
            raise ValueError('camera mount native pose is invalid')
        return _pose(pose[:3], pose[3:])

    frame, parent = declaration['frame'], np.eye(4)
    if isinstance(frame, dict):
        if set(frame) != {'nativePath'}:
            raise ValueError('camera mount requires an explicit nativePath')
        parent = native_pose(frame['nativePath'])
    elif frame != 'world':
        if world is None:
            raise ValueError('a scene camera mount requires its frozen World')
        relative, current = np.eye(4), frame
        while current is not None:
            node = world.nodes[current]
            if node.get('primPath'):
                parent = native_pose(node['primPath'])@relative
                break
            if node['kind'] == 'native':
                raise ValueError('native frame requires an actual SDK stage or measured pose')
            relative = _pose(node['at'], node['orientation'])@relative
            current = node['frame']
        else:
            parent = relative
    return parent@_pose(declaration['at'], declaration['orientation'])


def recording_camera_path(task_id, camera_id):
    return '/World/record_camera_'+task_id.encode().hex()+'_'+camera_id.encode().hex()


def validate_recording_pose_streams(world, io, cameras):
    """Every dynamic camera must be independently checkable at each actual frame tick."""
    for task_cameras in cameras.values():
        for spec in task_cameras.values():
            frame = spec['frame']
            path = frame.get('nativePath') if isinstance(frame, dict) else None
            while path is None and frame not in (None, 'world'):
                node = world.nodes[frame]
                path, frame = node.get('primPath'), node['frame']
            if path is None:
                continue
            candidates = [row for row in io['streams'].values()
                if row['source'].get('reader') == 'rigid_pose' and row['source'].get('path') == path
                and row.get('frame') == 'world_wxyz' and row.get('shape') == [7]
                and row.get('origin') == 'native_output']
            if len(candidates) != 1 or candidates[0]['rateHz'] % spec['rateHz']:
                raise ValueError('native camera mount requires one rigid_pose stream at every camera tick: '+path)


def compile_recording_cameras(stage, world, task):
    from runtime.scene import validate_recording
    recording = validate_recording(task.recording, world)
    cameras = {}
    for declaration in recording['cameras']:
        width, height = declaration['resolution']
        camera_id = declaration['cameraId']
        spec = {**deepcopy(declaration), 'width': width, 'height': height,
                'primPath': recording_camera_path(task.id, camera_id),
                'scope': 'task:'+task.id, 'viewMode': 'full_geometry', 'hiddenVisualNodeIds': [],
                'worldFromCamera': recording_camera_pose(world, declaration, stage=stage).tolist(),
                'visibilityScope': 'declared simulated sensor; occlusion is retained'}
        cameras[camera_id] = _write_camera(stage, spec)
    return cameras


def camera_intrinsics(width, height):
    # USD Camera authors focal length/apertures as float32, including this aspect ratio.
    vertical_aperture = float(np.float32(36*height/width))
    return {'fx': width*24/36, 'fy': height*24/vertical_aperture, 'cx': width/2, 'cy': height/2}


def _write_camera(stage, spec):
    from pxr import Gf, UsdGeom
    camera = UsdGeom.Camera.Define(stage, spec.get('primPath', '/World/camera'))
    camera.GetPrim().ApplyAPI('OmniSensorAPI')
    camera.CreateFocalLengthAttr(24)
    camera.CreateHorizontalApertureAttr(36)
    camera.CreateVerticalApertureAttr(36*spec['height']/spec['width'])
    camera.CreateClippingRangeAttr(Gf.Vec2f(.01, 1000))
    if 'worldFromCamera' in spec:
        matrix = Gf.Matrix4d(np.asarray(spec['worldFromCamera']).T.tolist())
    else:
        azimuth, elevation = math.radians(spec['azimuth']), math.radians(-spec['elevation'])
        offset = spec['distance']*np.array([math.cos(elevation)*math.cos(azimuth), math.cos(elevation)*math.sin(azimuth), math.sin(elevation)])
        target = np.asarray(spec['lookat'], dtype=float)
        eye = target+offset
        matrix = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(0, 0, 1)).GetInverse()
    ops = camera.GetOrderedXformOps()
    if not ops:
        ops = [camera.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble),
               camera.AddOrientOp(UsdGeom.XformOp.PrecisionDouble)]
    names = [str(op.GetOpName()) for op in ops]
    if names not in (['xformOp:translate', 'xformOp:orient'],
                     ['xformOp:translate', 'xformOp:orient', 'xformOp:scale']) or (
            len(ops) == 3 and not np.array_equal(ops[2].Get(), [1, 1, 1])):
        raise ValueError('camera requires the verified translate/orient stack with optional identity scale')
    types = {UsdGeom.XformOp.PrecisionDouble: (Gf.Vec3d, Gf.Quatd),
             UsdGeom.XformOp.PrecisionFloat: (Gf.Vec3f, Gf.Quatf),
             UsdGeom.XformOp.PrecisionHalf: (Gf.Vec3h, Gf.Quath)}
    ops[0].Set(types[ops[0].GetPrecision()][0](matrix.ExtractTranslation()))
    ops[1].Set(types[ops[1].GetPrecision()][1](matrix.ExtractRotationQuat()))
    return {**spec, 'primPath': str(camera.GetPath()), 'worldFromCamera': np.asarray(matrix).T.tolist(), 'projection': 'perspective',
            'intrinsics': camera_intrinsics(spec['width'], spec['height']),
            'coordinateSpec': 'metres; USD camera -Z forward/+Y up; column vectors'}


def _apply_camera_view(stage, spec, bindings):
    from pxr import Usd, UsdGeom
    spec = deepcopy(spec)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        if 'worldFromCamera' not in spec:
            spec = _write_camera(stage, spec)
        for id in spec.get('hiddenVisualNodeIds', []):
            parent = stage.GetPrimAtPath(bindings['objectPaths'][id])
            for prim in parent.GetChildren():
                if prim.GetName().startswith('visual_') and prim.IsA(UsdGeom.Mesh):
                    UsdGeom.Imageable(prim).CreateVisibilityAttr().Set(UsdGeom.Tokens.invisible)
    return spec


@measure('usdBuild')
def author_native_helpers(world, stage, directory):
    """Existing geometry conveniences author USD once; native source owns all other prims."""
    from pxr import Gf, PhysxSchema, Sdf, UsdGeom, UsdLux, UsdPhysics
    directory = Path(directory)
    visuals = directory/'visuals'
    visuals.mkdir(exist_ok=True)
    normalize_assets(world)
    collision_inputs = lock_collision_inputs(world, directory)
    native_runtime = collision_inputs.get('nativeRuntime')
    object_paths, joint_paths, articulation_paths, dynamic, collisions, convex_cache = {}, {}, {}, [], {}, {}
    for node in world.nodes.values():
        if node['kind'] in ('space', 'native'):
            continue
        if node.get('primPath'):
            if not stage.GetPrimAtPath(node['primPath']):
                raise ValueError('explicit helper binding has no native prim: '+node['id'])
            continue
        object_path = '/World/objects/o_'+node['id'].encode().hex()
        matrix = recording_camera_pose(world, {'frame': node['id'], 'at':[0,0,0],
            'orientation':[1,0,0,0]}, stage=stage)
        position = matrix[:3,3]
        q = Gf.Matrix4d(matrix.T.tolist()).ExtractRotationQuat()
        quaternion = [q.GetReal(), *q.GetImaginary()]
        parent = UsdGeom.Xform.Define(stage, object_path)
        parent.AddTranslateOp().Set(Gf.Vec3d(*position))
        parent.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(quaternion[0], Gf.Vec3d(*quaternion[1:])))
        if node.get('joint'):
            articulation_paths[node['id']] = object_path
            UsdPhysics.ArticulationRootAPI.Apply(parent.GetPrim())
            anchor = UsdGeom.Xform.Define(stage, object_path+'/anchor')
            UsdPhysics.RigidBodyAPI.Apply(anchor.GetPrim())
            mass = UsdPhysics.MassAPI.Apply(anchor.GetPrim())
            mass.CreateMassAttr(1.)
            mass.CreateDiagonalInertiaAttr(Gf.Vec3f(1))
            fixed = UsdPhysics.FixedJoint.Define(stage, object_path+'/fixed_root')
            fixed.CreateBody1Rel().SetTargets([anchor.GetPath()])
            fixed.CreateLocalPos0Attr(Gf.Vec3f(*position))
            fixed.CreateLocalRot0Attr(Gf.Quatf(quaternion[0], Gf.Vec3f(*quaternion[1:])))
            fixed.CreateLocalPos1Attr(Gf.Vec3f(0))
            fixed.CreateLocalRot1Attr(Gf.Quatf(1))
            object_path += '/body'
            parent = UsdGeom.Xform.Define(stage, object_path)
            parent.AddTranslateOp().Set(Gf.Vec3d(0))
            parent.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(1))
        object_paths[node['id']] = object_path
        node['primPath'] = object_path
        parts = _asset_parts(node) if node['kind'] == 'asset' else [_tint(_primitive_visual(node), node['color'])]
        for index, mesh in enumerate(parts):
            _write_mesh(stage, object_path+'/visual_'+str(index), mesh, visuals)
        collisions[node['id']] = _write_node_collision(stage, object_path, node, parts, directory, collision_inputs, convex_cache, native_runtime)
        if node.get('mass', 0) > 0 or node.get('joint'):
            dynamic.append(node['id'])
            UsdPhysics.RigidBodyAPI.Apply(parent.GetPrim())
            parent.GetPrim().GetAttribute('physics:velocity').Set(Gf.Vec3f(0))
            parent.GetPrim().GetAttribute('physics:angularVelocity').Set(Gf.Vec3f(0))
            UsdPhysics.MassAPI.Apply(parent.GetPrim()).CreateMassAttr(float(node.get('mass') or .1))
            PhysxSchema.PhysxRigidBodyAPI.Apply(parent.GetPrim()).CreateSleepThresholdAttr(0)
        if node.get('joint'):
            source = node['joint']
            joint_path = articulation_paths[node['id']]+'/object_joint'
            joint_paths[node['id']] = joint_path
            axis = np.asarray(source.get('axis', [0, 1, 0]), dtype=float)
            axis /= np.linalg.norm(axis)
            alignment = Gf.Rotation(Gf.Vec3d(1, 0, 0), Gf.Vec3d(*axis)).GetQuat()
            joint = (UsdPhysics.RevoluteJoint.Define(stage, joint_path) if source.get('type') == 'hinge'
                     else UsdPhysics.PrismaticJoint.Define(stage, joint_path))
            joint.CreateBody0Rel().SetTargets([anchor.GetPath()])
            joint.CreateBody1Rel().SetTargets([Sdf.Path(object_path)])
            joint.CreateLocalPos0Attr(Gf.Vec3f(0))
            joint.CreateLocalPos1Attr(Gf.Vec3f(0))
            joint.CreateLocalRot0Attr(Gf.Quatf(alignment))
            joint.CreateLocalRot1Attr(Gf.Quatf(alignment))
            joint.CreateAxisAttr('X')
            factor = 180/math.pi if source.get('type') == 'hinge' else 1
            low, high = source.get('range', [0, .2])
            joint.CreateLowerLimitAttr(float(low)*factor)
            joint.CreateUpperLimitAttr(float(high)*factor)
            PhysxSchema.JointStateAPI.Apply(joint.GetPrim(), 'angular' if factor != 1 else 'linear')
    if world.lights is None:
        if not any(UsdLux.LightAPI(prim) for prim in stage.Traverse()):
            UsdLux.DomeLight.Define(stage, '/World/lights/fill').CreateIntensityAttr(450)
            key = UsdLux.SphereLight.Define(stage, '/World/lights/key')
            key.CreateIntensityAttr(30000)
            key.CreateRadiusAttr(.6)
            target = np.asarray(camera_for(world, stage=stage)['lookat'])
            UsdGeom.Xformable(key).AddTranslateOp().Set(Gf.Vec3d(*(target+[1,-3,4])))
    else:
        for source in world.lights:
            path = '/World/lights/l_'+source['id'].encode().hex()
            if stage.GetPrimAtPath(path):
                raise ValueError('declared light would overwrite an existing native prim')
            if source['kind'] == 'sphere':
                light = UsdLux.SphereLight.Define(stage, path)
                light.CreateRadiusAttr(source['radius'])
            else:
                light = UsdLux.RectLight.Define(stage, path)
                light.CreateWidthAttr(source['width'])
                light.CreateHeightAttr(source['height'])
            light.CreateIntensityAttr(source['intensity'])
            light.CreateColorAttr(Gf.Vec3f(*source['color']))
            declaration = {**source, 'frame': source['frame'] or 'world'}
            matrix = recording_camera_pose(world, declaration, stage=stage)
            UsdGeom.Xformable(light).AddTransformOp().Set(Gf.Matrix4d(matrix.T.tolist()))
    return {'bindings': object_paths, 'collisionInputs': collision_inputs, 'collisions': collisions}








def _usd_schema_resources():
    sdk = Path(sys.prefix)/'lib/python3.12/site-packages/isaacsim'
    physx = sdk/'extscache/omni.usd.schema.physx-110.1.11+110.1.1.lx64.r.cp312.u7f4/plugins'
    return [(physx/'PhysxSchema/resources', 'physxSchema', None),
        (physx/'PhysxSchemaAddition/resources', 'physxSchemaAddition', None),
        (physx/'OmniUsdPhysicsDeformableSchema/resources', 'omniUsdPhysicsDeformableSchema', None),
        (sdk/'exts/isaacsim.robot.schema/usd/schema/isaac/robot_schema', 'RobotSchema',
         ('23858f0faed3c31fa7fe04436ad683e51b6c9a08635ee7cdd75843ba61d875c7',
          '237f281fb5706cf7a114afb6030fad7eba4252c27a338003646e03cd451762f4')),
        (sdk/'exts/omni.usd.schema.newton/usd/schema/newton/newton_usd_schemas', 'newton',
         ('a217686deb6220d8e6ba4838bd11ae6638bd7dc795157ff768462aac0d438d37',
          'e5031e9d82153908d9a1cab0e897d41e2b659986a932726d13b35c902041dc46')),
        (sdk/'extscache/omni.usd.schema.omni_sensors-0.0.0+6312fa25.lx64.r.cp312/usd_plugins', 'omniSensor',
         ('26f77866ed55a4d23e9a3dd95e67a3f6bd431d397ff3b7662da3573697011cec',
          'ee4f6b7a2101e474bf23b43615fc3426e7993e65216dba282037d33345fd1a37'))]


def _initialize_usd_schemas():
    """One fixed SDK registry for child and CPU readers; never loads Kit or edits a stage."""
    if importlib.metadata.version('isaacsim') != VERSION or sys.version_info[:2] != (3, 12):
        raise RuntimeError('USD schema reading requires locked Isaac 6.0.0.1 and Python 3.12')
    from pxr import Plug
    registry = Plug.Registry()
    types = {}
    for resource, name, locked in _usd_schema_resources():
        info_path = resource/'plugInfo.json'
        if not info_path.is_file() or not (resource/'generatedSchema.usda').is_file():
            raise ValueError('missing fixed USD schema resource: '+str(resource))
        if locked and tuple(_digest(resource/filename) for filename in ('plugInfo.json', 'generatedSchema.usda')) != locked:
            raise ValueError('fixed USD schema source changed: '+name)
        text = '\n'.join(line for line in info_path.read_text().splitlines() if not line.lstrip().startswith('#'))
        metadata = json.loads(text)['Plugins']
        if len(metadata) != 1 or metadata[0]['Name'] != name:
            raise ValueError('fixed USD schema provider metadata differs: '+name)
        expected = metadata[0]['Info']
        def check(plugin):
            if Path(plugin.resourcePath).resolve() != resource.resolve() or plugin.metadata != expected:
                raise ValueError('USD schema provider was registered from another source: '+name)
        previous = registry.GetPluginWithName(name)
        if previous: check(previous)
        returned = registry.RegisterPlugins(str(info_path))
        if any(plugin.name != name for plugin in returned):
            raise ValueError('unexpected fixed USD schema registration: '+name)
        plugin = registry.GetPluginWithName(name)
        if not plugin:
            raise ValueError('fixed USD schema registration failed: '+name)
        check(plugin)
        if locked:
            for type_name, row in expected['Types'].items():
                alias = row.get('alias', {}).get('UsdSchemaBase')
                if alias in ('IsaacRobotAPI', 'IsaacLinkAPI', 'IsaacJointAPI', 'NewtonSceneAPI', 'OmniSensorAPI'):
                    owner = registry.GetPluginForType(registry.FindTypeByName(type_name))
                    if not owner or owner.name != name:
                        raise ValueError('fixed USD schema type owner differs: '+alias)
                    types[alias] = name
    # All providers must exist before the singleton composes its prim definitions.
    from pxr import Usd
    schema = Usd.SchemaRegistry()
    for alias in ('IsaacRobotAPI', 'IsaacLinkAPI', 'IsaacJointAPI', 'NewtonSceneAPI', 'OmniSensorAPI'):
        if alias not in types or not schema.FindAppliedAPIPrimDefinition(alias):
            raise ValueError('fixed USD schema definition unavailable: '+alias)


def _cpu_environment():
    sdk = Path(sys.prefix)/'lib/python3.12/site-packages/isaacsim'
    libs = sdk/'extscache/omni.usd.libs-1.0.3+6312fa25.lx64.r.cp312'
    physx = sdk/'extscache/omni.usd.schema.physx-110.1.11+110.1.1.lx64.r.cp312.u7f4'
    result = dict(os.environ)
    result.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(Path(__file__).resolve().parent.parent)+':'+str(libs)+':'+str(physx),
                  LD_LIBRARY_PATH=str(libs/'bin')+':'+str(physx/'bin')+':'+str(sdk/'kit/kernel/plugins'),
                  PXR_PLUGINPATH_NAME=os.pathsep.join(str(path) for path, _, _ in _usd_schema_resources()))
    return result












def _contact_vector(value, size):
    try:
        valid = len(value) == size and all(type(x) in (int, float) and math.isfinite(x) for x in value)
    except (TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError('nonfinite or malformed native _contact_vector: '+repr(value))
    return [float(x) for x in value]


def _contact_u64(value, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value < 2**64:
        raise ValueError('missing or invalid uint64 identity: '+repr(value))
    return str(value)


def _copy_public_contacts(result, stage_id, decode_path):
    # Locked expectation from the existing native consumer and two-array SDK callback contract.
    # An observed different arity is a failed probe, never a guessed alternate decoder.
    if not isinstance(result, tuple):
        raise ValueError('get_contact_report is not a tuple: '+str(type(result)))
    if len(result) != 2:
        raise ValueError('get_contact_report expected exactly two arrays; observed arity '+str(len(result)))
    headers, data = result
    copied, used = [], set()
    for header in headers:
        if any(type(x) is not int for x in (header.stage_id, header.contact_data_offset, header.num_contact_data)):
            raise ValueError('public contact stage/slice fields must be integers')
        start, count = header.contact_data_offset, header.num_contact_data
        if int(header.stage_id) != stage_id or start < 0 or count < 0 or start+count > len(data):
            raise ValueError('stale stage or invalid public contact slice: '+repr((header.stage_id, start, count, len(data))))
        indices = set(range(start, start+count))
        if used.intersection(indices):
            raise ValueError('overlapping public contact slices')
        used.update(indices)
        row = {'stageId': int(header.stage_id), 'eventCode': int(header.type),
               'eventName': header.type.name, 'offset': start, 'count': count,
               'proto0': int(header.proto_index0), 'proto1': int(header.proto_index1), 'points': []}
        for side in ('0', '1'):
            for kind in ('actor', 'collider'):
                value = getattr(header, kind+side)
                row[kind+side+'Id'] = _contact_u64(value)
                path = decode_path(value)
                if not isinstance(path, str) or not path.startswith('/'):
                    raise ValueError('contact ID has no real USD path')
                row[kind+side] = path
        for index in range(start, start+count):
            point = data[index]
            row['points'].append({'positionWorldM': _contact_vector(point.position, 3),
                'normal': _contact_vector(point.normal, 3), 'impulseNative': _contact_vector(point.impulse, 3),
                'separationM': _contact_vector([point.separation], 1)[0],
                'faceIndex0': int(point.face_index0), 'faceIndex1': int(point.face_index1),
                'material0Id': _contact_u64(int(point.material0), True), 'material1Id': _contact_u64(int(point.material1), True)})
        copied.append(row)
    if len(used) != len(data):
        raise ValueError('public contact data not covered by header slices')
    return {'observedTupleArity': 2, 'headerCount': len(copied), 'dataCount': len(data), 'headers': copied}


def _native_contact_arrays(copied, actor_paths):
    path_indices = {path: index for index, path in enumerate(actor_paths)}
    headers = np.empty((copied['headerCount'], 13), dtype=np.uint64)
    points, impulses = np.empty((copied['dataCount'], 3), dtype=np.float32), np.empty((copied['dataCount'], 3), dtype=np.float32)
    for index, row in enumerate(copied['headers']):
        headers[index] = [row['eventCode'], int(row['actor0Id']), int(row['actor1Id']),
            int(row['collider0Id']), int(row['collider1Id']), row['proto0'], row['proto1'], row['offset'], row['count'],
            *[path_indices[row[key]] for key in ('actor0','actor1','collider0','collider1')]]
        for offset, value in enumerate(row['points'], row['offset']):
            points[offset], impulses[offset] = value['positionWorldM'], value['impulseNative']
    return {'headers': headers, 'points': points, 'impulses': impulses}


def _physical_plain(value):
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):return value if math.isfinite(value) else {'schemaSpecialFloat':str(value)}
    try:return [_physical_plain(item) for item in value]
    except TypeError:return str(value)


def _physical_parameters(stage):
    from pxr import Usd,UsdGeom,UsdPhysics
    result={'@stage':{'type':'stage_metadata','schemas':[],'relationships':{},'attributes':{
        'metersPerUnit':UsdGeom.GetStageMetersPerUnit(stage),'kilogramsPerUnit':UsdPhysics.GetStageKilogramsPerUnit(stage),
        'upAxis':str(UsdGeom.GetStageUpAxis(stage)),
        'physicsScenes':[str(prim.GetPath()) for prim in Usd.PrimRange.Stage(stage) if prim.IsA(UsdPhysics.Scene)]}}}
    for prim in Usd.PrimRange.Stage(stage,Usd.TraverseInstanceProxies()):
        attrs={}
        for attr in prim.GetAttributes():
            name=attr.GetName()
            if name in ('physics:velocity','physics:angularVelocity') or (name.startswith('drive:') and name.endswith((':targetPosition',':targetVelocity'))):continue
            if name.startswith('xformOp') and (prim.HasAPI(UsdPhysics.RigidBodyAPI) or prim.IsA(UsdGeom.Camera)):
                continue  # Native body poses and declared sensor extrinsics have their own recorded checks.
            if name.startswith(('physics:','physx','xformOp','drive:')) or name in ('size','radius','height','points','faceVertexCounts','faceVertexIndices'):
                attrs[name]={'value':_physical_plain(attr.Get()),'authored':attr.HasAuthoredValueOpinion(),'timeSamples':list(attr.GetTimeSamples())}
        result[str(prim.GetPath())]={'type':prim.GetTypeName(),'schemas':list(prim.GetAppliedSchemas()),'attributes':attrs,
            'relationships':{rel.GetName():list(map(str,rel.GetTargets())) for rel in prim.GetRelationships()}}
    return result






def _physical_clock(env):
    import omni.usd
    from isaacsim.core.simulation_manager import SimulationManager as Sim
    stage = int(omni.usd.get_context().get_stage_id())
    step, value = int(Sim.get_num_physics_steps()), float(Sim.get_simulation_time())
    if stage != env.stage_id or stage <= 0 or step < 0 or not math.isfinite(value):
        raise ValueError('native physical clock/stage is unavailable or changed')
    return {'stageId': stage, 'absolutePhysicsStep': step, 'simulationTime': value}














def _verify_native_physics_scene(manager, path, rate, gpu, broadphase):
    scenes = manager.get_physics_scenes()
    if len(scenes) != 1 or str(scenes[0].prim.GetPath()) != path:
        raise ValueError('native manager does not own the unique frozen physics scene: '+path)
    scene = scenes[0]
    if (abs(scene.get_dt()-1/rate) > 1e-12 or scene.get_enabled_gpu_dynamics() != gpu
            or scene.get_broadphase_type() != broadphase):
        raise ValueError('native manager physics differs from the frozen clock, device or broadphase')
    return scene


def _setup_native_stage(path, clock):
    """Open audited frozen USD; preserve its actual native physical configuration."""
    from runtime.robot import native_clock
    physics, control, _ = native_clock(clock)
    _ensure_app()
    import isaacsim.core.experimental.utils.stage as stage_utils
    from isaacsim.core.simulation_manager import SimulationManager as Sim
    from isaacsim.core.rendering_manager import RenderingManager as Render
    from pxr import PhysxSchema, Sdf, UsdGeom, UsdPhysics
    layer = Sdf.Layer.FindOrOpen(str(path))
    if layer:
        layer.Reload(True)
    opened, stage = stage_utils.open_stage(str(path))
    if not opened or not Sim.switch_physics_engine('physx'):
        raise RuntimeError('failed to open frozen native PhysX USD')
    scenes = [prim for prim in stage.Traverse() if prim.IsA(UsdPhysics.Scene)]
    if len(scenes) != 1 or UsdGeom.GetStageMetersPerUnit(stage) != 1 or UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
        raise ValueError('native I/O requires one actual physics scene and the metre/Z-up stage convention')
    scene = PhysxSchema.PhysxSceneAPI(scenes[0])
    if scene.GetTimeStepsPerSecondAttr().Get() != physics:
        raise ValueError('native stage physics rate differs from its declared I/O clock')
    gpu = bool(scene.GetEnableGPUDynamicsAttr().Get())
    scene_path, broadphase = str(scenes[0].GetPath()), scene.GetBroadphaseTypeAttr().Get()
    selected = _verify_native_physics_scene(Sim, scene_path, physics, gpu, broadphase)
    Sim.setup_simulation(dt=1/physics, device='cuda:0' if gpu else 'cpu')
    Sim.enable_gpu_dynamics(gpu)
    # set_device chooses a broadphase as a convenience; retain the frozen source
    # selection rather than silently turning its SAP/MBP/GPU choice into another.
    selected.set_broadphase_type(broadphase)
    _verify_native_physics_scene(Sim, scene_path, physics, gpu, broadphase)
    Sim.enable_fabric(False)
    import carb
    from omni.physx.bindings import _physx
    # A CUDA tensor pipeline suppresses USD readback by default.  The solver
    # remains on the assigned GPU; this enables its raw output mirror.
    carb.settings.get_settings().set_bool('/physics/suppressReadback', False)
    for setting in (_physx.SETTING_UPDATE_TO_USD, _physx.SETTING_UPDATE_VELOCITIES_TO_USD,
                    _physx.SETTING_UPDATE_PARTICLES_TO_USD):
        carb.settings.get_settings().set_bool(setting, True)
    Render.set_dt(1/control)
    return stage


def _native_array(value, label, *, limits=False):
    if hasattr(value, 'numpy'):
        value = value.numpy()
    value = np.asarray(value)
    if value.dtype.kind not in 'fiu' or (np.isnan(value).any() if limits else not np.isfinite(value).all()):
        raise ValueError('invalid native readback: '+label)
    return value.copy()


def _same_native_state(left, right, *, _root=True):
    if isinstance(left, dict) and isinstance(right, dict):
        ignored = {'cameraFrames'} if _root else set()
        keys = set(left)-ignored
        return keys == set(right)-ignored and all(_same_native_state(left[key], right[key], _root=False) for key in keys)
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        return np.array_equal(left, right)
    return left == right


def _verify_native_bindings(stage, bindings):
    from pxr import Sdf
    paths = bindings.get('objectPaths')
    if not isinstance(paths, dict):
        raise ValueError('native semantic bindings require one object-path map')
    for name, raw in paths.items():
        if not isinstance(raw, str):
            raise ValueError('native binding path must be a string: '+name)
        path = Sdf.Path(raw)
        if (not path.IsAbsolutePath() or not path.IsPrimPath() or path.ContainsPrimVariantSelection()
                or not raw.startswith('/World/') or not stage.GetPrimAtPath(path)):
            raise ValueError('native binding does not identify an existing absolute prim: '+name)


def _reject_authored_particle_outputs(stage, io):
    for declaration in io['streams'].values():
        source = declaration.get('source', {})
        if source.get('reader') != 'particle_state':
            continue
        prim = stage.GetPrimAtPath(source.get('path', ''))
        if not prim:
            raise ValueError('particle source prim is missing')
        output = prim.GetAttribute('physxParticle:simulationPoints')
        if output and output.HasAuthoredValueOpinion():
            raise ValueError('authored simulationPoints cannot qualify as native particle output')


def _prepare_native_particle_sources(stage, io):
    sources = {row['source']['path'] for row in io['streams'].values() if row.get('source', {}).get('reader') == 'particle_state'}
    if not sources:
        return set()
    from pxr import PhysxSchema, UsdGeom, UsdPhysics
    _reject_authored_particle_outputs(stage, io)
    scenes = [prim for prim in stage.Traverse() if prim.IsA(UsdPhysics.Scene)]
    if len(scenes) != 1 or not PhysxSchema.PhysxSceneAPI(scenes[0]).GetEnableGPUDynamicsAttr().Get():
        raise ValueError('particle source requires one enabled native GPU physics scene')
    for path in sources:
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsA(UsdGeom.Points) or not prim.HasAPI(PhysxSchema.PhysxParticleSetAPI) or prim.HasAPI(UsdPhysics.RigidBodyAPI):
            raise ValueError('particle source is not an independent native Points particle set')
        if not np.allclose(_usd_world(stage,path),np.eye(4),atol=1e-9,rtol=0):
            raise ValueError('particle raw world coordinates require an identity prim world frame; other frames are unsupported')
        api = PhysxSchema.PhysxParticleAPI(prim)
        systems = api.GetParticleSystemRel().GetTargets()
        if not api.GetParticleEnabledAttr().Get() or len(systems) != 1:
            raise ValueError('particle set is disabled or lacks one native particle system')
        system = PhysxSchema.PhysxParticleSystem(stage.GetPrimAtPath(systems[0]))
        if (not system or not system.GetParticleSystemEnabledAttr().Get()
                or system.GetSimulationOwnerRel().GetTargets() != [scenes[0].GetPath()]):
            raise ValueError('particle system is disabled or does not own the unique active physics scene')
        positions = _native_array(UsdGeom.Points(prim).GetPointsAttr().Get(), 'particle initial positions')
        velocities = _native_array(UsdGeom.Points(prim).GetVelocitiesAttr().Get(), 'particle initial velocities')
        if positions.ndim != 2 or positions.shape[1] != 3 or not len(positions) or velocities.shape != positions.shape:
            raise ValueError('particle source requires finite explicit initial position/velocity arrays')
        mass = UsdPhysics.MassAPI(prim)
        declared_mass, density = mass.GetMassAttr().Get(), mass.GetDensityAttr().Get()
        if not any(isinstance(value, (float, int)) and math.isfinite(value) and value > 0 for value in (declared_mass, density)):
            raise ValueError('particle source needs an explicit positive mass or density configuration')
    return sources


def _verify_native_camera(stage, spec, control_rate, *, recording=True):
    from pxr import Sdf, UsdGeom
    path = Sdf.Path(spec['primPath'])
    prim = stage.GetPrimAtPath(path)
    if not path.IsAbsolutePath() or not path.IsPrimPath() or path.ContainsPrimVariantSelection() or not prim or not prim.IsA(UsdGeom.Camera):
        raise ValueError('frozen camera does not reference an actual native Camera prim')
    if recording:
        rate = spec['rateHz']
        if type(rate) is not int or rate <= 0 or rate > control_rate or control_rate % rate:
            raise ValueError('camera sampling must divide the frozen control rate')
    width, height = spec.get('resolution', [spec.get('width'), spec.get('height')])
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        raise ValueError('camera resolution is invalid')
    camera = UsdGeom.Camera(prim)
    focal, horizontal, vertical = [float(value.Get()) for value in
        (camera.GetFocalLengthAttr(), camera.GetHorizontalApertureAttr(), camera.GetVerticalApertureAttr())]
    if any(not math.isfinite(value) or value <= 0 for value in (focal, horizontal, vertical)):
        raise ValueError('native camera calibration is unavailable')
    expected = {'fx': width*focal/horizontal, 'fy': height*focal/vertical, 'cx': width/2, 'cy': height/2}
    if set(spec.get('intrinsics', {})) != set(expected) or any(abs(spec['intrinsics'][key]-value) > 1e-8 for key, value in expected.items()):
        raise ValueError('frozen camera intrinsics differ from the actual native lens')
    if not np.allclose(_usd_world(stage, str(path)), spec['worldFromCamera'], atol=1e-6, rtol=0):
        raise ValueError('frozen camera pose differs from its actual native transform')


class Environment:
    """One trusted native stage and clock; generated programs only submit named commands."""
    def __init__(self, compiled, world, directory, progress, camera=None, record=True, task=None,
                 source_directory=None, episode_id=None, rubric_session=None, measurement_spec=None,
                 *, _discover=False, cameras=None):
        from runtime.robot import NATIVE_IO_SCHEMA, native_clock
        if compiled.get('io', {}).get('schema') != NATIVE_IO_SCHEMA:
            raise ValueError('this worker requires a frozen native I/O package; legacy packages use their frozen worker')
        self.compiled, self.world = deepcopy(compiled), world
        self.source_directory = Path(source_directory or directory).resolve()
        self.directory, self.progress, self.task = Path(directory), progress, task
        self.directory.mkdir(parents=True, exist_ok=True)
        self.io = deepcopy(compiled['io'])
        self.physics_rate, self.control_rate, self.substeps = native_clock(self.io['clock'])
        self.physical_seconds = task.execution['physicalSeconds'] if task is not None else 30
        self.record, self.recorder, self.rubric_session = record, None, rubric_session
        self.stage, self.stage_id, self._closed, self._failed = None, 0, False, None
        self._operation_lock = threading.Lock()
        self.native_articulations, self.native_bodies, self.native_port_bindings = {}, {}, {}
        self.active_targets, self._stream_cache, self._particle_cache, self._contact_cache = {}, {}, {}, {}
        self.steps, self.frames, self.camera_epoch = 0, [], 0
        self.camera_resources, self.camera_failures, self.latest_camera_frames = {}, {}, {}
        self.sensor_directory = self.directory/'policy-sensors'
        self.recording = deepcopy(task.recording) if task is not None else {'cameras': [], 'primaryCameraId': None}
        if cameras is not None and (record or camera is not None or not isinstance(cameras, dict) or not cameras):
            raise ValueError('batch cameras require an explicit non-recording camera selection')
        self.camera_specs = (deepcopy(cameras) if cameras is not None
            else {camera.get('cameraId', 'preview'): deepcopy(camera)} if camera is not None
            else deepcopy(compiled.get('recordingCameras', {}).get(task.id, {})) if task is not None else {})
        self.primary_camera_id = next(iter(self.camera_specs), None) if camera is not None or cameras is not None else self.recording['primaryCameraId']
        self.camera_spec = deepcopy(self.camera_specs.get(self.primary_camera_id, {}))
        self._camera_override = camera is not None or cameras is not None
        self.latest_camera = None
        try:
            self._open_native(_discover)
            if record:
                import imageio.v2 as imageio
                for name, resource in self.camera_resources.items():
                    spec = self.camera_specs[name]
                    if spec.get('video', False) and name not in self.camera_failures:
                        target = self.directory/'videos'/(name+'.mp4')
                        target.parent.mkdir(exist_ok=True)
                        try:
                            resource['writer'] = imageio.get_writer(str(target), fps=spec['rateHz'],
                                codec='libx264', quality=7, macro_block_size=1)
                        except Exception as exc:
                            self._disable_optional_camera(name, exc)
                self.recorder = Recorder(self.directory, io=self.io, recording=self.recording,
                    task=asdict(task) if task is not None else None)
            self.capture_frame()
            if self.recorder:
                self.recorder.begin({'observation': self._observe_raw(task), 'truth': self._observe_raw(truth=True)}, rubric_session)
                self.initial_state = deepcopy(self.recorder.index['initialSnapshot']['truth'])
                self._publish_policy_frames()
            else:
                from runtime.records import validate_streams
                self.initial_state = validate_streams(self.export_state(), self.io,
                    truth=True, directory=self.directory, write=True)
        except BaseException as exc:
            try:
                self.close('initialization_failed')
            except BaseException as cleanup:
                exc.add_note('native open cleanup failed: '+repr(cleanup))
            raise

    def _native_articulation(self, path):
        from pxr import Usd, UsdPhysics
        from isaacsim.core.experimental.prims import Articulation
        prim = self.stage.GetPrimAtPath(path)
        if not prim:
            raise ValueError('native articulation path is absent: '+path)
        roots = [str(p.GetPath()) for p in Usd.PrimRange(prim) if p.HasAPI(UsdPhysics.ArticulationRootAPI)]
        if len(roots) != 1:
            raise ValueError('native source must identify exactly one actual articulation root: '+path)
        root = roots[0]
        if root not in self.native_articulations:
            self.native_articulations[root] = Articulation(root, reset_xform_op_properties=False)
        return root, self.native_articulations[root]

    def _open_native(self, discover):
        from runtime import mdl
        audit_path = self.source_directory/self.compiled.get('nativeStageAudit', 'native-stage-audit.json')
        material_audit = mdl.read_audit(self.source_directory, audit_path.name) if audit_path.is_file() else None
        if material_audit is not None:
            configure_native_materials(self.source_directory, material_audit)
        _ensure_app()
        from isaacsim.core.experimental.prims import RigidPrim
        from isaacsim.core.experimental.utils.backend import use_backend
        from isaacsim.core.simulation_manager import SimulationManager as Sim
        import isaacsim.core.experimental.utils.app as app_utils
        import omni.usd
        import warp as wp
        path = (self.source_directory/self.compiled['usd']).resolve()
        if not path.is_relative_to(self.source_directory) or _digest(path) != self.compiled['frozenUsdSha256']:
            raise ValueError('native frozen USD source is missing or changed')
        from pxr import Sdf, Usd
        source_layer = Sdf.Layer.FindOrOpen(str(path))
        source_layer.Reload(True)
        source_stage = Usd.Stage.Open(source_layer)
        self._qualified_particle_paths = _prepare_native_particle_sources(source_stage, self.io)
        source_stage = None
        self.stage = _setup_native_stage(path, self.io['clock'])
        material_rendering = mdl.verify_native_materials(self.stage, self.source_directory, material_audit) if material_audit else None
        self.stage_id = int(omni.usd.get_context().get_stage_id())
        _verify_native_bindings(self.stage, self.compiled['bindings'])
        for camera_specs in self.compiled.get('recordingCameras', {}).values():
            for spec in camera_specs.values():
                _verify_native_camera(self.stage, spec, self.control_rate)
        if self.compiled.get('camera'):
            _verify_native_camera(self.stage, self.compiled['camera'], self.control_rate, recording=False)
        if self._camera_override:
            self.camera_specs = {name: _apply_camera_view(self.stage, spec, self.compiled['bindings'])
                for name, spec in self.camera_specs.items()}
            for spec in self.camera_specs.values():
                _verify_native_camera(self.stage, spec, self.control_rate, recording='rateHz' in spec)
            self.camera_spec = deepcopy(self.camera_specs[self.primary_camera_id])
        self.camera_epoch += 1
        self.native_contact_paths = self._enable_native_contact_reports()
        with wp.ScopedDevice('cpu'), use_backend('usd', raise_on_unsupported=True, raise_on_fallback=True):
            for name, spec in [*self.io['ports'].items(), *self.io['streams'].items()]:
                source = spec.get('source', {})
                reader, primitive, target = source.get('reader'), source.get('primitive'), source.get('path')
                if primitive is not None or reader in ('joint_positions', 'joint_velocities', 'joint_position_targets',
                        'joint_velocity_targets', 'joint_efforts', 'root_pose', 'root_velocity'):
                    self._native_articulation(target)
                elif reader in ('rigid_pose', 'rigid_velocity'):
                    if not isinstance(target, str) or not self.stage.GetPrimAtPath(target):
                        raise ValueError('native rigid-body path is absent: '+str(target))
                    self.native_bodies[target] = RigidPrim(target, reset_xform_op_properties=False)
            if self.camera_specs:
                from isaacsim.sensors.experimental.rtx import CameraSensor, RtxCamera
                import omni.replicator.core as rep
                for name, spec in self.camera_specs.items():
                    try:
                        self._initialize_camera(name, spec, RtxCamera, CameraSensor, rep, wp)
                    except Exception as exc:
                        self._disable_optional_camera(name, exc)
        app_utils.play(commit=True)
        actual = _physical_clock(self)
        self.time_origin, self._step_origin = actual['simulationTime'], actual['absolutePhysicsStep']
        self._simulation_time = self.time_origin
        self._sample_times = {self.time_origin: 0}
        self.initialization = {'phase': 'native_manager_initialized', 'actualClock': deepcopy(actual),
            'relativeOrigin': self.time_origin, 'explicitPhysicsSteps': 0,
            'scope': 'SDK play initialization may advance physics; no state or target is restored or invented'}
        if material_rendering is not None:
            self.initialization['mdl'] = material_rendering
        with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
            if any(not view.is_physics_tensor_entity_valid() for view in
                   [*self.native_articulations.values(), *self.native_bodies.values()]):
                raise ValueError('native tensor handle is not available')
            verified = self._discover_io()
        if not discover and verified != self.io:
            raise ValueError('native SDK I/O differs from its frozen contract')
        self.io = verified
        self.compiled['io'] = deepcopy(verified)
        if _digest(path) != self.compiled['frozenUsdSha256']:
            raise ValueError('native stage opening changed its frozen source file')

    def _port_read(self, name):
        spec, binding = self.io['ports'][name], self.native_port_bindings[name]
        getter = {'joint_position': 'get_dof_position_targets', 'joint_velocity': 'get_dof_velocity_targets',
                  'joint_effort': 'get_dof_efforts'}[spec['source']['primitive']]
        value = _native_array(getattr(binding['articulation'], getter)(dof_indices=binding['indices']), name)
        if value.shape != (1, len(binding['indices'])):
            raise ValueError('native target readback shape differs from its actuator binding: '+name)
        return value[0]

    def _enable_native_contact_reports(self):
        from pxr import PhysxSchema, PhysicsSchemaTools, Usd, UsdPhysics
        actors = set()
        for declaration in self.io['streams'].values():
            source = declaration.get('source', {})
            if source.get('reader') != 'contact_report':
                continue
            if (set(source) != {'reader', 'path', 'actors', 'field'} or source['field'] not in ('headers', 'points', 'impulses')
                    or not isinstance(source['actors'], list) or not source['actors']
                    or len(source['actors']) != len(set(source['actors']))
                    or not self.stage.GetPrimAtPath(source['path']).IsA(UsdPhysics.Scene)):
                raise ValueError('contact report requires one real physics scene, explicit rigid actors and a raw field')
            actors.update(source['actors'])
        for path in actors:
            prim = self.stage.GetPrimAtPath(path)
            if not prim or not prim.HasAPI(UsdPhysics.RigidBodyAPI):
                raise ValueError('native contact reporter is not a real rigid actor: '+path)
            PhysxSchema.PhysxContactReportAPI.Apply(prim).CreateThresholdAttr(0.)
        return sorted(str(prim.GetPath()) for prim in Usd.PrimRange.Stage(self.stage, Usd.TraverseInstanceProxies())
            if prim.HasAPI(UsdPhysics.RigidBodyAPI) or prim.HasAPI(UsdPhysics.CollisionAPI)) if actors else []

    def _discover_io(self):
        from runtime.robot import native_values, validate_native_ports
        verified = {'schema': self.io['schema'], 'clock': deepcopy(self.io['clock']), 'ports': {}, 'streams': {}}
        for name, declaration in self.io['ports'].items():
            source = deepcopy(declaration.get('source'))
            if (not isinstance(source, dict) or set(source) != {'primitive', 'path', 'joints'}
                    or source['primitive'] not in ('joint_position', 'joint_velocity', 'joint_effort')
                    or not isinstance(source['joints'], list) or not source['joints']
                    or len(source['joints']) != len(set(source['joints']))):
                raise ValueError('unsupported or incomplete native command source: '+name)
            root, art = self._native_articulation(source['path'])
            source['path'] = root
            if any(joint not in art.dof_names for joint in source['joints']):
                raise ValueError('command references an absent native joint: '+name)
            indices = [list(art.dof_names).index(joint) for joint in source['joints']]
            self.native_port_bindings[name] = {'articulation': art, 'indices': indices}
            types = [int(art.dof_types[index]) for index in indices]
            if any(kind not in (0, 1) for kind in types):
                raise ValueError('native actuator type is unsupported: '+name)
            gains = [_native_array(value, name+' gains')[0] for value in art.get_dof_gains(dof_indices=indices)]
            stiffness, damping = gains
            mode = source['primitive']
            if (mode == 'joint_position' and (np.any(stiffness <= 0) or np.any(damping < 0))
                    or mode == 'joint_velocity' and (np.any(stiffness != 0) or np.any(damping <= 0))
                    or mode == 'joint_effort' and (np.any(stiffness != 0) or np.any(damping != 0))):
                raise ValueError('native joint drive gains do not support its frozen command mode: '+name)
            if mode == 'joint_position':
                lower, upper = art.get_dof_limits(dof_indices=indices)
                limits = np.column_stack((_native_array(lower, name+' lower limits', limits=True)[0],
                                          _native_array(upper, name+' upper limits', limits=True)[0]))
            else:
                maximum = _native_array(getattr(art, 'get_dof_max_velocities' if mode == 'joint_velocity'
                    else 'get_dof_max_efforts')(dof_indices=indices), name+' limits', limits=True)[0]
                limits = np.column_stack((-maximum, maximum))
            position_units = ['rad' if kind == 0 else 'm' for kind in types]
            units = (position_units if mode == 'joint_position' else
                [unit+'/s' for unit in position_units] if mode == 'joint_velocity' else
                ['N*m' if kind == 0 else 'N' for kind in types])
            current = self._port_read(name)
            native_values(current, [len(indices)], name+' initialized target')
            self.active_targets[name] = current.tolist()
            verified['ports'][name] = {'source': source, 'dtype': str(current.dtype), 'shape': [len(indices)],
                'units': units, 'limits': [[None,None] for _ in indices] if mode == 'joint_position' else [[None if not math.isfinite(lo) else float(lo),
                    None if not math.isfinite(hi) else float(hi)] for lo, hi in limits],
                **({'physicalJointLimits': [[None if not math.isfinite(lo) else float(lo),
                    None if not math.isfinite(hi) else float(hi)] for lo, hi in limits],
                    'limitMeaning': 'limits bound the desired drive reference, which has no SDK position-target bound; physicalJointLimits are actual joint stops, not a target clamp'}
                   if mode == 'joint_position' else {'limitMeaning': 'conservative tool command envelope from native configured actuator limits; not claimed as a hard SDK target-buffer bound'}),
                'persistence': 'per_physics_step' if mode == 'joint_effort' else 'persistent',
                'initialized': True, 'nativeDofPaths': [str(art.dof_paths[0][i]) for i in indices],
                'nativeDofIndices': indices,
                'driveGains': {'stiffness': stiffness.tolist(), 'damping': damping.tolist()},
                'readback': 'actuation_buffer_not_measured_torque' if mode == 'joint_effort' else 'native_drive_target'}
        validate_native_ports(verified['ports'])
        for name, declaration in self.io['streams'].items():
            source = deepcopy(declaration.get('source'))
            if not isinstance(source, dict) or not isinstance(source.get('reader'), str):
                raise ValueError('native stream requires an explicit read-only source: '+name)
            rate = declaration.get('rateHz', self.control_rate)
            if type(rate) is not int or rate <= 0 or self.physics_rate % rate:
                raise ValueError('stream rate must divide the actual physics rate: '+name)
            if any(type(declaration.get(key, default)) is not bool for key, default in
                   (('visibleToPolicy', False), ('required', True))):
                raise ValueError('native stream permissions must be booleans: '+name)
            metadata = self._stream_metadata(source)
            verified['streams'][name] = {'source': source, **metadata, 'rateHz': rate,
                'visibleToPolicy': declaration.get('visibleToPolicy', False), 'required': declaration.get('required', True)}
        return verified

    def _stream_metadata(self, source):
        reader = source['reader']
        if reader == 'camera_frame':
            raise ValueError('camera_frame is not a numeric stream; use the frozen Task.recording cameraFrames channel')
        value = self._read_source(source)
        if reader.startswith('joint_'):
            _, art = self._native_articulation(source['path'])
            types = [int(art.dof_types[list(art.dof_names).index(joint)]) for joint in source['joints']]
            units = ['rad' if kind == 0 else 'm' for kind in types]
            if reader in ('joint_velocities', 'joint_velocity_targets'):
                units = [unit+'/s' for unit in units]
            elif reader == 'joint_efforts':
                units = ['N*m' if kind == 0 else 'N' for kind in types]
            frame = 'joint'
        elif reader in ('root_pose', 'rigid_pose'):
            units, frame = ['m']*3+['unitless']*4, 'world_wxyz'
        elif reader in ('root_velocity', 'rigid_velocity'):
            units, frame = ['m/s']*3+['rad/s']*3, 'world'
        elif reader == 'particle_state':
            units, frame = 'm' if source['field'] == 'positions' else 'm/s', 'world'
        elif reader == 'contact_report':
            fields = source['field']
            return {'dtype': str(value.dtype), 'shape': [None, value.shape[1]],
                'units': 'native_integer_fields' if fields == 'headers' else 'm' if fields == 'points' else 'N*s',
                'frame': 'native_identity' if fields == 'headers' else 'world', 'origin': 'native_output',
                'columns': ['eventCode','actor0NativeId','actor1NativeId','collider0NativeId','collider1NativeId',
                    'proto0','proto1','offset','count','actor0PathIndex','actor1PathIndex','collider0PathIndex','collider1PathIndex'] if fields == 'headers' else ['x','y','z'],
                'actorPaths': deepcopy(self.native_contact_paths),
                **({'runtimeIdentityColumns': [1,2,3,4], 'pathIndexColumns': [9,10,11,12],
                    'identityProjection': 'SDK decode of each original native ID into the frozen actorPaths table; each header stage_id checked against sample.stageId'} if fields == 'headers' else {}),
                'scope': 'native reported events only; an empty report does not prove contact absence'}
        else:
            raise ValueError('unsupported native read-only primitive: '+reader)
        return {'dtype': str(value.dtype), 'shape': ([None, *value.shape[1:]] if reader == 'particle_state' else list(value.shape)),
                'units': units, 'frame': frame, 'origin': 'native_output',
                **({'frameQualification':'particle prim and ancestors must retain the identity world transform (matrix tolerance 1e-9); checked before activation and after actual SDK synchronization',
                    'readbackQualification':'source-authored simulationPoints are rejected; after actual native GPU physics and synchronization, fluid smoothing uses SDK simulationPoints, otherwise Points.points; velocities are read independently'}
                   if reader == 'particle_state' else {})}

    def _read_source(self, source, *, cache=None):
        reader = source['reader']
        joint_getters = {'joint_positions': 'get_dof_positions', 'joint_velocities': 'get_dof_velocities',
            'joint_position_targets': 'get_dof_position_targets', 'joint_velocity_targets': 'get_dof_velocity_targets',
            'joint_efforts': 'get_dof_efforts'}
        if reader in joint_getters:
            if set(source) != {'reader', 'path', 'joints'}:
                raise ValueError('native joint reader needs only path and actual ordered joints')
            root, art = self._native_articulation(source['path'])
            source['path'] = root
            joints = source['joints']
            if not isinstance(joints, list) or not joints or len(joints) != len(set(joints)) or any(j not in art.dof_names for j in joints):
                raise ValueError('native reader joint selection is invalid')
            indices = [list(art.dof_names).index(joint) for joint in joints]
            return _native_array(getattr(art, joint_getters[reader])(dof_indices=indices), reader)[0]
        if reader in ('root_pose', 'root_velocity', 'rigid_pose', 'rigid_velocity'):
            if set(source) != {'reader', 'path'}:
                raise ValueError('native pose/velocity reader needs an exact prim path')
            if reader.startswith('root_'):
                root, view = self._native_articulation(source['path'])
                source['path'] = root
            else:
                view = self.native_bodies[source['path']]
            parts = view.get_world_poses() if reader.endswith('pose') else view.get_velocities()
            values = [_native_array(value, reader)[0] for value in parts]
            return np.concatenate(values)
        if reader == 'particle_state':
            return self._read_particles(source, cache=cache['particles'] if cache is not None else None)
        if reader == 'contact_report':
            return self._read_contact_report(source, cache=cache['contacts'] if cache is not None else None)
        raise ValueError('unsupported native read-only primitive: '+reader)

    def _read_contact_report(self, source, *, cache=None):
        import omni.physx
        from pxr import PhysicsSchemaTools
        clock = self._clock()
        cache = self._contact_cache if cache is None else cache
        if cache.get('clock') != clock:
            copied = _copy_public_contacts(omni.physx.get_physx_simulation_interface().get_contact_report(),
                self.stage_id, lambda value: str(PhysicsSchemaTools.intToSdfPath(value)))
            for row in copied['headers']:
                for key in ('actor0','actor1','collider0','collider1'):
                    if row[key] not in self.native_contact_paths or int(PhysicsSchemaTools.sdfPathToInt(row[key])) != int(row[key+'Id']):
                        raise ValueError('native contact identity is outside the frozen actual actor/collider map')
            if self._clock() != clock:
                raise ValueError('native contact read advanced the physical clock')
            cache.clear()
            cache.update(clock=clock, **_native_contact_arrays(copied, self.native_contact_paths))
        return cache[source['field']].copy()

    def _read_particles(self, source, *, cache=None):
        from pxr import PhysxSchema, UsdGeom
        import carb
        import omni.physx
        from omni.physx.bindings import _physx
        from isaacsim.core.simulation_manager import SimulationManager as Sim
        if set(source) != {'reader', 'path', 'field'} or source['field'] not in ('positions', 'velocities'):
            raise ValueError('particle reader supports only native simulation positions or velocities')
        prim = self.stage.GetPrimAtPath(source['path'])
        if not prim or not prim.HasAPI(PhysxSchema.PhysxParticleSetAPI) or not prim.IsA(UsdGeom.Points):
            raise ValueError('particle reader requires an actual native Points particle set')
        if not np.allclose(_usd_world(self.stage,source['path']),np.eye(4),atol=1e-9,rtol=0):
            raise ValueError('particle prim world frame changed; nonidentity raw frames are unsupported')
        api = PhysxSchema.PhysxParticleAPI(prim)
        systems = api.GetParticleSystemRel().GetTargets()
        manager_scenes = Sim.get_physics_scenes()
        if source['path'] not in self._qualified_particle_paths or not api.GetParticleEnabledAttr().Get() or len(systems) != 1:
            raise ValueError('particle source lacks its frozen pre-activation qualification')
        system = PhysxSchema.PhysxParticleSystem(self.stage.GetPrimAtPath(systems[0]))
        if (not system or not system.GetParticleSystemEnabledAttr().Get() or Sim.get_active_physics_engine() != 'physx'
                or len(manager_scenes) != 1 or system.GetSimulationOwnerRel().GetTargets() != [manager_scenes[0].prim.GetPath()]
                or not manager_scenes[0].get_enabled_gpu_dynamics()
                or omni.physx.get_physx_interface().get_overwrite_gpu_setting() == 0):
            raise ValueError('particle system is not bound to the active native GPU physics owner')
        actual = _physical_clock(self)
        if not Sim.is_simulating() or actual['absolutePhysicsStep'] <= 0:
            raise ValueError('particle native output requires actual physics execution')
        settings = carb.settings.get_settings()
        particle_api, points = PhysxSchema.PhysxParticleSetAPI(prim), UsdGeom.Points(prim)
        system_prim = system.GetPrim()
        def readback_mode():
            smoothing = bool(particle_api.GetFluidAttr().Get() and system_prim.HasAPI(PhysxSchema.PhysxParticleSmoothingAPI)
                and PhysxSchema.PhysxParticleSmoothingAPI(system_prim).GetParticleSmoothingEnabledAttr().Get())
            isosurface = bool(particle_api.GetFluidAttr().Get() and system_prim.HasAPI(PhysxSchema.PhysxParticleIsosurfaceAPI)
                and PhysxSchema.PhysxParticleIsosurfaceAPI(system_prim).GetIsosurfaceEnabledAttr().Get())
            flags = tuple(settings.get_as_bool(key) for key in ('/physics/suppressReadback',
                _physx.SETTING_UPDATE_TO_USD, _physx.SETTING_UPDATE_PARTICLES_TO_USD, _physx.SETTING_UPDATE_VELOCITIES_TO_USD))
            return smoothing, isosurface, flags
        mode = readback_mode()
        smoothing, isosurface, (suppressed, update_usd, update_particles, update_velocities) = mode
        field = source['field']
        clock = self._clock()
        cache = self._particle_cache if cache is None else cache
        key = source['path'], field
        if (suppressed or not update_usd or (isosurface or field == 'positions' and smoothing) and not update_particles
                or field == 'velocities' and not update_velocities):
            cache.pop(key, None)
            raise ValueError('native particle '+field+' output readback is disabled')
        cached = cache.get(key)
        if cached is None or cached['clock'] != clock or cached['mode'] != mode:
            cache.pop(key, None)
            before = self._clock()
            omni.physx.get_physx_simulation_interface().fetch_results()
            omni.physx.get_physx_interface().update_transformations(False, True, True, False)
            if self._clock() != before:
                raise ValueError('particle result synchronization advanced the physical clock')
            if readback_mode() != mode:
                raise ValueError('particle readback configuration changed during synchronization')
            if not np.allclose(_usd_world(self.stage,source['path']),np.eye(4),atol=1e-9,rtol=0):
                raise ValueError('synchronized particle prim world frame is nonidentity and unsupported')
            attribute = points.GetVelocitiesAttr() if field == 'velocities' else (
                particle_api.GetSimulationPointsAttr() if smoothing else points.GetPointsAttr())
            raw = attribute.Get()
            if raw is None:
                raise ValueError('SDK particle '+str(attribute.GetName())+' output is unavailable')
            value = _native_array(raw, 'particle '+field)
            if value.ndim != 2 or value.shape[1] != 3:
                raise ValueError('native particle '+field+' buffer has invalid shape')
            other = cache.get((source['path'], 'velocities' if field == 'positions' else 'positions'))
            if other is not None and other['clock'] == clock and other['mode'] == mode and other['value'].shape != value.shape:
                raise ValueError('native particle position/velocity buffers have different shapes')
            cache[key] = cached = {'clock': clock, 'mode': mode, 'value': value}
        return cached['value'].copy()

    def _clock(self):
        actual = _physical_clock(self)
        step = actual['absolutePhysicsStep']-self._step_origin
        value = actual['simulationTime']-self.time_origin
        if step < 0 or abs(value-step/self.physics_rate) > 1e-7:
            raise ValueError('native state no longer matches the frozen physical clock')
        self._simulation_time = actual['simulationTime']
        return {'time': value, 'simulationTime': actual['simulationTime'], 'timeOrigin': self.time_origin,
                'physicsStep': step, 'stageId': actual['stageId']}

    @property
    def time(self):
        return self._simulation_time-self.time_origin

    def observe(self, task=None, truth=False):
        if not truth and self.recorder is not None and self.recorder.committed_observation is not None:
            return deepcopy(self.recorder.committed_observation)
        return self._observe_raw(task, truth)

    @measure('stateRead')
    def _observe_raw(self, task=None, truth=False, *, fresh=False):
        if self._closed:
            state = deepcopy(self._closed_state)
            if state is None:
                raise RuntimeError('native state was unavailable at shutdown')
            if not truth:
                state['streams'] = {name: value for name, value in state['streams'].items() if self.io['streams'][name]['visibleToPolicy']}
                state['streamStatus'] = {name: value for name, value in state['streamStatus'].items() if self.io['streams'][name]['visibleToPolicy']}
            return state
        from isaacsim.core.experimental.utils.backend import use_backend
        clock = self._clock()
        values, statuses = {}, {}
        read_cache = {'particles': {}, 'contacts': {}} if fresh else None
        with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
            for name, spec in self.io['streams'].items():
                if not truth and not spec['visibleToPolicy']:
                    continue
                cached = self._stream_cache.get(name)
                period = self.physics_rate//spec['rateHz']
                due = fresh or clock['physicsStep'] % period == 0 and (cached is None or cached['step'] != clock['physicsStep'])
                if due:
                    try:
                        value = self._read_source(spec['source'], cache=read_cache)
                        if str(value.dtype) != spec['dtype'] or len(value.shape) != len(spec['shape']) or any(
                                expected is not None and actual != expected for actual, expected in zip(value.shape, spec['shape'])):
                            raise ValueError('native stream shape/dtype changed')
                        cached = {'value': value.copy(), 'step': clock['physicsStep'], 'time': clock['time']}
                        if not fresh:
                            self._stream_cache[name] = cached
                    except Exception as exc:
                        if spec['required']:
                            raise ValueError('required native stream '+name+' is unavailable: '+str(exc)) from exc
                        cached = {'status': 'unavailable', 'reason': type(exc).__name__+': '+str(exc), 'step': clock['physicsStep']}
                        if not fresh:
                            self._stream_cache[name] = cached
                if cached is None or cached.get('status') == 'unavailable':
                    reason = cached['reason'] if cached is not None else 'no native sample at the declared sampling phase'
                    if spec['required']:
                        raise ValueError('required native stream '+name+' is unavailable: '+reason)
                    statuses[name] = {'status': 'unavailable', 'reason': reason}
                    continue
                values[name] = deepcopy(cached['value'])
                statuses[name] = {'status': 'available', 'sampleTime': cached['time'], 'physicsStep': cached['step']}
        if self._clock() != clock:
            raise ValueError('native read-only observation changed the physical clock')
        cameras = deepcopy(self.latest_camera_frames)
        if not truth:
            declaration = self.task.measurements.get('camera', {}) if self.task is not None else {}
            permitted = set(declaration.get('cameraIds', [])) if declaration.get('visibleToPolicy', False) else set()
            cameras = {name: frame for name, frame in cameras.items() if name in permitted}
        return {**clock, 'streams': values, 'streamStatus': statuses, 'cameraFrames': cameras}

    def export_state(self):
        return self._observe_raw(truth=True, fresh=True)

    def _write_ports(self, commands, *, instantaneous_only=False):
        setters = {'joint_position': 'set_dof_position_targets', 'joint_velocity': 'set_dof_velocity_targets',
                   'joint_effort': 'set_dof_efforts'}
        executed = {}
        for name, value in commands.items():
            spec, binding = self.io['ports'][name], self.native_port_bindings[name]
            if instantaneous_only and spec['persistence'] == 'persistent':
                continue
            array = np.asarray(value, dtype=spec['dtype']).reshape(1, -1)
            getattr(binding['articulation'], setters[spec['source']['primitive']])(array, dof_indices=binding['indices'])
            actual = self._port_read(name)
            if not np.array_equal(actual, array[0]):
                raise ValueError('native actuator target readback differs from the submitted command: '+name)
            executed[name] = actual.tolist()
            if spec['persistence'] == 'persistent':
                self.active_targets[name] = actual.tolist()
        return executed

    def step(self, action, seconds=None):
        from runtime.robot import native_control_action, native_control_cycles
        if self.episode_done or self._failed:
            raise ValueError('ended or failed native episode requires a fresh host process')
        if not self._operation_lock.acquire(blocking=False):
            raise ValueError('one native operation already owns this physical clock')
        native_attempted = False
        try:
            cycles = native_control_cycles(1/self.control_rate if seconds is None else seconds,
                self.io['clock'], self.time, self.physical_seconds)
            commands = native_control_action(action, self.io, self.active_targets)
            from isaacsim.core.simulation_manager import SimulationManager as Sim
            from isaacsim.core.experimental.utils.backend import use_backend
            for _ in range(cycles):
                if self.episode_done:
                    break
                before, truth_before = self._observe_raw(self.task), self._observe_raw(truth=True)
                samples, previous = [], self._clock()
                with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
                    native_attempted = True
                    executed = self._write_ports(commands)
                for tick in range(self.substeps):
                    if tick:
                        with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
                            self._write_ports(commands, instantaneous_only=True)
                    with measure('physicsStep'):
                        Sim.step(steps=1, update_fabric=False)
                    actual = self._clock()
                    if actual['physicsStep'] != previous['physicsStep']+1 or abs(actual['simulationTime']-previous['simulationTime']-1/self.physics_rate) > 1e-7:
                        raise RuntimeError('native step did not advance exactly one frozen physical tick')
                    self.steps = actual['physicsStep']
                    self._sample_times[actual['simulationTime']] = self.steps
                    self._particle_cache.clear()
                    self._contact_cache.clear()
                    sample = self._observe_raw(truth=True)
                    selected = {name for name, spec in self.io['streams'].items() if spec['rateHz'] > self.control_rate
                        and actual['physicsStep'] % (self.physics_rate//spec['rateHz']) == 0}
                    samples.append({**sample, 'streams': {name: value for name, value in sample['streams'].items() if name in selected},
                        'streamStatus': {name: value for name, value in sample['streamStatus'].items() if name in selected}})
                    previous = actual
                self.capture_frame()
                after, truth_after = self._observe_raw(self.task), self._observe_raw(truth=True)
                if self.recorder:
                    self.recorder.commit(before, commands, executed, after, self.substeps, (), truth_before,
                        truth_after, physical_samples=samples, truncated=self.time >= self.physical_seconds-1e-9)
                    self._publish_policy_frames()
            return self.observe(self.task)
        except BaseException as exc:
            if not native_attempted:
                raise
            from runtime.records import validate_streams
            tail, actual_clock = None, None
            lower = self.steps
            try:
                actual_clock = self._clock()
                self.steps = actual_clock['physicsStep']
            except BaseException as clock_error:
                exc.add_note('native failure clock unavailable: '+repr(clock_error))
            try:
                tail = validate_streams(self.export_state(), self.io, truth=True, directory=self.directory, write=True)
            except BaseException as read_error:
                exc.add_note('native failure tail unavailable: '+repr(read_error))
            self._failed = {'error': {'type': type(exc).__name__, 'message': str(exc)}, 'actualTail': tail,
                'committedCycles': self.recorder.cycles if self.recorder else 0,
                'actualClock': actual_clock, 'confirmedStepLowerBound': lower,
                'totalExecutedPhysicalSteps': actual_clock['physicsStep'] if actual_clock is not None else None,
                'uncommittedPhysicalSteps': (actual_clock['physicsStep']-(self.recorder.physical_steps if self.recorder else 0)
                    if actual_clock is not None else None),
                'countStatus': 'known' if actual_clock is not None else 'unknown'}
            try:
                atomic_json(self.directory/'native-step-failure.json', self._failed)
            except BaseException as diagnostic_error:
                exc.add_note('native failure diagnostic unavailable: '+repr(diagnostic_error))
            raise
        finally:
            self._operation_lock.release()

    @property
    def episode_done(self):
        return bool(self._closed or self._failed or self.recorder and self.recorder.episode_done
                    or self.time >= self.physical_seconds-1e-9)

    @property
    def last_result(self):
        return self.recorder.last_result if self.recorder else None

    def reset(self, seed=0):
        if int(seed) != int(self.world.seed) or self.steps or self._closed or self._failed:
            raise ValueError('reset requires this frozen seed and a fresh native episode')
        return deepcopy(self.recorder.initial_snapshot['observation'] if self.recorder else self.observe(self.task))

    def _geometry_rows(self, state):
        return []

    def _validate_geometry(self, state, rows, phase='capture'):
        if any(state[key] != self._clock()[key] for key in ('physicsStep', 'simulationTime', 'stageId')):
            raise ValueError('native camera state and physical clock differ')

    def _publish_policy_frames(self):
        if self.recorder is None or self.recorder.failed or self.recorder.committed_observation is None:
            return
        from runtime.records import array_references, read_array
        observation = self.recorder.committed_observation
        for reference in array_references(observation):
            array = read_array(self.directory, reference)
            del array
            relative = Path(reference['ref'])
            source, target = self.directory/relative, self.sensor_directory/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.link(source, target)
            array = read_array(self.sensor_directory, reference)
            del array
        for name, frame in observation.get('cameraFrames', {}).items():
            if 'path' not in frame:
                continue
            relative = Path(frame['path'])
            source = self.directory/relative
            if (relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != ('frames', name)
                    or source.is_symlink() or not source.resolve().is_relative_to(self.directory.resolve())
                    or _digest(source) != frame['sha256']):
                raise ValueError('policy sensor source leaves its frozen camera or changed')
            target = self.sensor_directory/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.link(source, target)
            elif target.is_symlink() or _digest(target) != frame['sha256']:
                raise ValueError('policy sensor publication changed')

    def close(self, reason='completed'):
        if self._closed:
            return
        primary = None
        try:
            self._closed_state = self.export_state() if self.stage is not None and hasattr(self, 'time_origin') else None
        except BaseException as exc:
            primary = exc
        for operation in (lambda: self.recorder.close(reason) if self.recorder else None,
                          lambda: self._close_camera_resources(invalidate_sensor=True),
                          lambda: _close_stage() if self.stage is not None else None):
            try:
                operation()
            except BaseException as exc:
                if primary is None: primary = exc
                else: primary.add_note('native close: '+repr(exc))
        self.stage, self._closed = None, True
        self.native_articulations.clear()
        self.native_bodies.clear()
        try:
            if self.sensor_directory.exists():
                import shutil
                shutil.rmtree(self.sensor_directory)
        except BaseException as exc:
            if primary is None: primary = exc
            else: primary.add_note('policy sensor cleanup: '+repr(exc))
        if primary is not None:
            raise primary

    def _initialize_camera(self, camera_id, spec, RtxCamera, CameraSensor, rep, wp):
        if spec.get('modalities', ['rgb']) != ['rgb']:
            raise ValueError('declared pixel modality is unsupported; this runtime implements RGB only')
        width, height = spec.get('resolution', [spec.get('width', WIDTH), spec.get('height', HEIGHT)])
        resource = self.camera_resources[camera_id] = {'seen': set(), 'frameCount': 0, 'writer': None}
        resource['camera'] = RtxCamera(spec['primPath'], tick_rate=0.0, reset_xform_op_properties=False)
        # Isaac 6 SensorBase initializes its cleanup members before constructor work.
        # Retain the instance so a partial constructor can be explicitly invalidated.
        resource['sensor'] = CameraSensor.__new__(CameraSensor)
        CameraSensor.__init__(resource['sensor'], resource['camera'], resolution=(height, width), annotators=['rgb'])
        if not np.allclose(_usd_world(self.stage, spec['primPath']), spec['worldFromCamera'], atol=1e-6, rtol=0):
            raise RuntimeError('camera constructor changed the frozen sensor pose')
        resource['product'] = str(resource['sensor'].render_product.GetPath())
        resource['reference'] = rep.AnnotatorRegistry.get_annotator('ReferenceTime', device='cpu')
        resource['read_time'] = rep.AnnotatorRegistry.get_annotator('IsaacReadSimulationTime', init_params={'resetOnStop': True}, device='cpu')
        resource['referenceAttached'] = True
        resource['reference'].attach([resource['product']])
        resource['readTimeAttached'] = True
        resource['read_time'].attach([resource['product']])
        resource['rgb_out'] = wp.empty((height, width, 3), dtype=wp.uint8, device='cpu')


    def _disable_optional_camera(self, camera_id, error):
        if self.camera_specs[camera_id].get('required', True):
            raise error
        try:
            self._close_camera_resources((camera_id,), invalidate_sensor=True)
        except BaseException as cleanup:
            error.add_note('optional camera cleanup failed: '+repr(cleanup))
            raise error
        reason = type(error).__name__+': '+str(error)
        self.camera_failures[camera_id] = reason
        self.latest_camera_frames[camera_id] = {'cameraId': camera_id, 'status': 'unavailable', 'reason': reason}


    def capture_frame(self):
        from PIL import Image
        from pxr import Usd
        import omni.replicator.core as rep
        due = [name for name in self.camera_resources if name not in self.camera_failures
               and self.steps % round(self.physics_rate/self.camera_specs[name].get('rateHz', 25)) == 0]
        if not due:
            return deepcopy(self.latest_camera_frames.get(self.primary_camera_id))
        before = self.export_state()
        deadline = time.monotonic()+90 if self.frames else None
        failures_before = dict(self.camera_failures)
        geometry = self._geometry_rows(before)
        self._validate_geometry(before, geometry, phase='capture')
        for name in due:
            spec = self.camera_specs[name]
            if 'frame' in spec:
                pose = recording_camera_pose(self.world, spec, state=before, io=self.io)
                with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
                    self.camera_specs[name] = _write_camera(self.stage, {**spec, 'worldFromCamera': pose.tolist()})
        pending, attempts, rejected, last = set(due), 0, {name: {} for name in due}, {}
        while pending and (deadline is None or time.monotonic() < deadline):
            with measure('render'):
                rep.orchestrator.step(delta_time=0., pause_timeline=False, wait_for_render=True)
            if deadline is None:
                # The supervised cold render can compile RTX shaders before frames are ready.
                deadline = time.monotonic()+90
            if not _same_native_state(self.export_state(), before):
                raise RuntimeError('pure rendering changed physical state or clock')
            attempts += 1
            for name in due:
                if name not in pending:
                    continue
                resource, spec = self.camera_resources[name], self.camera_specs[name]
                try:
                    a = {key: int(value) for key, value in (resource['reference'].get_data() or {}).items()}
                    pixels, _ = resource['sensor'].get_data('rgb', out=resource['rgb_out'])
                    clock = {key: float(value) for key, value in (resource['read_time'].get_data() or {}).items()}
                    b = {key: int(value) for key, value in (resource['reference'].get_data() or {}).items()}
                except Exception as exc:
                    if not _same_native_state(self.export_state(), before):
                        raise RuntimeError('sensor reading changed physical state or clock') from exc
                    if spec.get('required', True):
                        raise
                    self.camera_failures[name] = type(exc).__name__+': '+str(exc)
                    self.latest_camera_frames[name] = {'cameraId': name, 'status': 'unavailable', 'reason': self.camera_failures[name]}
                    pending.remove(name)
                    continue
                if not _same_native_state(self.export_state(), before):
                    raise RuntimeError('sensor reading changed physical state or clock')
                reference_key = json.dumps(a, sort_keys=True)
                own_time = clock.get('simulationTime')
                reasons = [label for label, failed in (
                    ('pixels_missing', pixels is None), ('reference_missing', not a), ('reference_changed', a != b),
                    ('reference_seen', reference_key in resource['seen']),
                    ('clock_mismatch', own_time is None or abs(own_time-before['simulationTime']) > 1e-7)) if failed]
                last[name] = {'referenceBefore': a, 'referenceAfter': b, 'readSimulationTime': clock, 'rejectedBy': reasons,
                    'pixelsIsNone': pixels is None,
                    'pixelsShape': list(pixels.shape) if getattr(pixels, 'shape', None) is not None else None,
                    'pixelsDtype': str(pixels.dtype) if getattr(pixels, 'dtype', None) is not None else None}
                if reasons:
                    for reason in reasons:
                        rejected[name][reason] = rejected[name].get(reason, 0)+1
                    continue
                rgb = pixels.numpy().copy()
                width, height = spec.get('resolution', [spec.get('width', WIDTH), spec.get('height', HEIGHT)])
                if rgb.shape != (height, width, 3) or rgb.dtype != np.uint8:
                    rejected[name]['rgb_format'] = rejected[name].get('rgb_format', 0)+1
                    continue
                if not _same_native_state(self.export_state(), before):
                    raise RuntimeError('pure rendering changed physical state or clock')
                matches = [step for sample_time, step in self._sample_times.items() if abs(sample_time-own_time) <= 1e-7]
                if matches != [before['physicsStep']]:
                    raise RuntimeError('frame cannot be paired to the submitted physics step')
                frame_index = resource['frameCount']
                filename = f'frames/{name}/{frame_index:06d}.png'
                target = self.directory/filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with measure('imageWrite'):
                    Image.fromarray(rgb).save(target)
                    with target.open('rb') as stream:
                        os.fsync(stream.fileno())
                if resource.get('writer') is not None:
                    with measure('videoEncode'):
                        resource['writer'].append_data(rgb)
                frame = {'index': len(self.frames), 'cameraId': name, 'frameIndex': frame_index,
                    'sampleId': before['physicsStep'], 'path': filename, 'sha256': _digest(target),
                    'time': own_time-self.time_origin, 'ownTime': own_time, 'simulationTime': own_time,
                    'timeOrigin': self.time_origin, 'physicsStep': before['physicsStep'], 'stageId': self.stage_id,
                    'cameraEpoch': self.camera_epoch, 'renderProduct': resource['product'],
                    'rawReferenceTime': a, 'readSimulationTime': clock,
                    'worldFromCamera': _usd_world(self.stage, spec['primPath']).tolist(),
                    'intrinsics': spec['intrinsics'], 'coordinateSpec': spec['coordinateSpec'],
                    'cameraScope': spec['scope'], 'cameraPrimPath': spec['primPath'],
                    'viewMode': spec.get('viewMode', 'full_geometry'),
                    'hiddenVisualNodeIds': deepcopy(spec.get('hiddenVisualNodeIds', [])),
                    'visibilityScope': spec.get('visibilityScope', 'camera_framing; occlusion is retained'),
                    'runtimeGeometry': {'physicsStep': before['physicsStep'],
                        'source': 'fresh declared native streams and physical clock checked during RGB capture',
                        'robot_model_alignment_check': 'unverified'}}
                resource['seen'].add(reference_key)
                resource['frameCount'] += 1
                self.frames.append(frame)
                self.latest_camera_frames[name] = deepcopy(frame)
                pending.remove(name)
        if pending:
            for name in sorted(pending):
                self.camera_failures[name] = 'no coherent RGB frame at the submitted native physics time'
                self.latest_camera_frames[name] = {'cameraId': name, 'status': 'unavailable', 'reason': self.camera_failures[name]}
        if self.camera_failures != failures_before:
            from runtime.records import validate_streams
            atomic_json(self.directory/'camera-failure.json', {'scope': 'declared sensor failure; no synthetic frames',
                'cameraFailures': self.camera_failures,
                'before': validate_streams(before, self.io, truth=True, directory=self.directory, write=True),
                'after': validate_streams(self.export_state(), self.io, truth=True, directory=self.directory, write=True),
                'renderAttempts': attempts, 'rejections': rejected, 'lastAttempt': last})
        if any(self.camera_specs[name].get('required', True) for name in pending):
            raise TimeoutError('required camera did not produce a coherent new frame')
        self.latest_camera = deepcopy(self.latest_camera_frames.get(self.primary_camera_id))
        if self.primary_camera_id in self.camera_specs:
            self.camera_spec = deepcopy(self.camera_specs[self.primary_camera_id])
        return deepcopy(self.latest_camera)


    def _close_camera_resources(self, camera_ids=None, *, invalidate_sensor=False):
        primary = None
        for name, resource in self.camera_resources.items():
            if camera_ids is not None and name not in camera_ids:
                continue
            for kind, flag in (('reference', 'referenceAttached'), ('read_time', 'readTimeAttached'), ('sensor', None), ('writer', None)):
                value = resource.get(kind)
                if value is None:
                    continue
                try:
                    if kind == 'sensor':
                        if invalidate_sensor:
                            # Exact private partial-constructor cleanup of locked Isaac 6.
                            value._invalidate_sensor()
                        else:
                            value.detach_annotators(['rgb'])
                    elif kind == 'writer':
                        with measure('videoEncode'):
                            value.close()
                        path = self.directory/'videos'/(name+'.mp4')
                        if path.exists():
                            with path.open('rb') as stream:
                                os.fsync(stream.fileno())
                    elif resource.get(flag, False):
                        value.detach()
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note('camera cleanup: '+repr(exc))
                resource[kind] = None
                if flag is not None:
                    resource[flag] = False
        if primary is not None:
            raise primary




def compile_native_world(world, directory, io_declarations, *, recording_cameras=None, camera=None, _output_directory=None):
    """Called only in the trusted fresh process after passive native-source audit."""
    from runtime.robot import NATIVE_IO_SCHEMA
    directory = Path(directory).resolve()
    path = directory/'scene-source.usda'
    bindings = {name: node['primPath'] for name, node in world.nodes.items() if node.get('primPath')}
    compiled = {'schema': 'sceneagent-native-compiled-v1', 'usd': path.name,
        'frozenUsdSha256': _digest(path), 'bindings': {'objectPaths': bindings},
        'io': {'schema': NATIVE_IO_SCHEMA, **deepcopy(io_declarations)}}
    compiled['recordingCameras'] = deepcopy(recording_cameras or {})
    compiled['camera'] = deepcopy(camera)
    environment = Environment(compiled, world, _output_directory or directory, lambda *_args, **_kwargs: None,
        record=False, source_directory=directory, _discover=True)
    try:
        compiled['io'] = deepcopy(environment.io)
        validate_recording_pose_streams(world, compiled['io'], compiled['recordingCameras'])
        compiled['initialState'] = deepcopy(environment.initial_state)
        compiled['nativeInitialTargets'] = deepcopy(environment.active_targets)
        compiled['initialization'] = deepcopy(environment.initialization)
    finally:
        environment.close()
    if _digest(path) != compiled['frozenUsdSha256']:
        raise ValueError('native compilation changed the immutable source')
    return compiled




def _glb_document(path):
    raw = Path(path).read_bytes()
    if len(raw) < 28 or struct.unpack_from('<4sII', raw) != (b'glTF', 2, len(raw)):
        raise ValueError('invalid embedded GLB')
    offset, document, binary = 12, None, None
    while offset < len(raw):
        length, kind = struct.unpack_from('<I4s', raw, offset)
        if length % 4 or offset+8+length > len(raw):
            raise ValueError('invalid GLB chunk')
        data = raw[offset+8:offset+8+length]
        if kind == b'JSON':
            document = json.loads(data)
        elif kind == b'BIN\x00':
            binary = data
        offset += 8+length
    if document is None or binary is None:
        raise ValueError('GLB requires JSON and embedded binary')
    return document, binary


def _write_glb(path, document, binary):
    encoded = json.dumps(document, separators=(',', ':'), allow_nan=False).encode()
    encoded += b' '*(-len(encoded) % 4)
    Path(path).write_bytes(struct.pack('<4sII', b'glTF', 2, 28+len(encoded)+len(binary))+
        struct.pack('<I4s', len(encoded), b'JSON')+encoded+struct.pack('<I4s', len(binary), b'BIN\x00')+binary)


def _index_nodes(document, expected_children=None):
    nodes, paths, worlds, locals_, visited = document['nodes'], {}, {}, {}, set()
    def visit(index, prefix, parent):
        if not 0 <= index < len(nodes) or index in visited:
            raise ValueError('ambiguous or malformed GLB hierarchy')
        visited.add(index)
        node = nodes[index]
        path = prefix+'/'+node.get('name', str(index))
        if path in paths:
            raise ValueError('duplicate full GLB path')
        if 'matrix' in node:
            local = np.asarray(node['matrix'], dtype=float).reshape(4, 4, order='F')
        else:
            q = node.get('rotation', [0, 0, 0, 1])
            local = _pose(node.get('translation', [0, 0, 0]), [q[3], *q[:3]])
            local[:3, :3] = local[:3, :3]@np.diag(node.get('scale', [1, 1, 1]))
        if not np.isfinite(local).all():
            raise ValueError('nonfinite GLB matrix')
        paths[path], locals_[index], worlds[index] = index, local, parent@local
        for child in node.get('children', []):
            visit(child, path, worlds[index])
    roots = document['scenes'][document.get('scene', 0)]['nodes']
    if len(roots) != 1 or nodes[roots[0]].get('name') != 'World':
        raise ValueError('require one verified World scene root')
    visit(roots[0], '', np.eye(4))
    outer, allowed = roots[0], {'name', 'children', 'matrix', 'translation', 'rotation', 'scale'}
    children = nodes[outer].get('children', [])
    inner = [index for index in children if nodes[index].get('name') == 'World']
    if inner:
        if (len(children) != 1 or len(inner) != 1 or not set(nodes[outer]).issubset(allowed)
                or not np.allclose(locals_[outer], TO_GLTF, atol=1e-12, rtol=0)
                or not set(nodes[inner[0]]).issubset(allowed) or not np.allclose(locals_[inner[0]], np.eye(4), atol=1e-12, rtol=0)):
            raise ValueError('unverified conversion wrapper transform or payload')
        if expected_children is not None and {nodes[i].get('name') for i in nodes[inner[0]].get('children', [])} != set(expected_children):
            raise ValueError('conversion wrapper namespace differs from frozen USD')
        aliases = {p.replace('/World/World/', '/World/', 1): i for p, i in paths.items() if p.startswith('/World/World/')}
        if set(aliases)&set(paths):
            raise ValueError('ambiguous conversion wrapper alias')
        paths.update(aliases)
    return paths, worlds


def _accessor(document, binary, index):
    spec = document['accessors'][index]
    view = document['bufferViews'][spec['bufferView']]
    if 'sparse' in spec or view.get('buffer', 0) != 0:
        raise ValueError('unsupported GLB accessor storage')
    dtype = np.dtype({5126: '<f4', 5125: '<u4', 5123: '<u2', 5121: 'u1'}[spec['componentType']])
    width = {'VEC3': 3, 'VEC2': 2, 'SCALAR': 1, 'VEC4': 4}[spec['type']]
    count, stride = spec['count'], view.get('byteStride', width*dtype.itemsize)
    relative = spec.get('byteOffset', 0)
    begin = view.get('byteOffset', 0)+relative
    length = (count-1)*stride+width*dtype.itemsize
    if count <= 0 or relative < 0 or stride < width*dtype.itemsize or relative+length > view['byteLength'] or begin+length > len(binary):
        raise ValueError('malformed accessor buffer range')
    values = np.ndarray((count, width), dtype=dtype, buffer=binary, offset=begin, strides=(stride, dtype.itemsize)).copy()
    if spec.get('normalized', False):
        if spec['componentType'] not in (5121, 5123):
            raise ValueError('unsupported normalized GLB accessor')
        values = values.astype(float)/np.iinfo(dtype).max
    return values


def _triangle_signature(triangles):
    if not np.isfinite(triangles).all():
        raise ValueError('nonfinite triangle positions')
    rows = [tuple(sorted(tuple(round(float(x), 6) for x in point) for point in triangle)) for triangle in triangles]
    return hashlib.sha256(repr(sorted(rows)).encode()).hexdigest()


def _native_material(shader):
    from pxr import UsdShade
    asset = shader.GetPrim().GetAttribute('info:mdl:sourceAsset').Get()
    if asset is None or shader.GetPrim().GetAttribute('info:mdl:sourceAsset:subIdentifier').Get() != 'OmniPBR':
        raise ValueError('unsupported native material shader')
    mdl = Path(asset.resolvedPath or asset.path)
    if _digest(mdl) != 'd384fc800f120e3cad1b7ca8837f9c554f9c34343d194813fca27caa493001f2':
        raise ValueError('locked native MDL source changed')
    values = {}
    pattern = r'^\s*(?:uniform\s+)?(color|float|bool)\s+(\w+)\s*=\s*(color\([^)]*\)|float\([^)]*\)|true|false|[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*\[\['
    for kind, name, expression in re.findall(pattern, mdl.read_text(), re.M):
        if kind == 'bool':
            values[name] = expression == 'true'
        else:
            arguments = expression[expression.index('(')+1:-1] if '(' in expression else expression
            v = list(map(float, arguments.split(',')))
            values[name] = (v*3 if len(v) == 1 else v) if kind == 'color' else v[0]
    allowed = {'diffuse_color_constant', 'diffuse_tint', 'reflection_roughness_constant', 'metallic_constant',
               'specular_level', 'enable_emission', 'emissive_color', 'emissive_intensity'}
    for input_ in shader.GetInputs():
        name = str(input_.GetBaseName())
        if name not in allowed or input_.GetConnectedSource():
            raise ValueError('unverified native MDL material branch')
        v = input_.Get()
        values[name] = list(map(float, v)) if str(input_.GetTypeName()) == 'color3f' else v
    if values['enable_opacity'] or values['enable_ORM_texture'] or values['ao_to_diffuse'] != 0 or values['specular_level'] != .5:
        raise ValueError('unsupported native material defaults')
    return {'baseColor': [*(np.array(values['diffuse_color_constant'])*values['diffuse_tint']), 1],
            'roughness': values['reflection_roughness_constant'], 'metallic': values['metallic_constant'],
            'emissive': (np.array(values['emissive_color'])*values['emissive_intensity']/(math.pi*1133.6)).tolist() if values['enable_emission'] else [0, 0, 0],
            'alphaMode': 'OPAQUE', 'alphaCutoff': .5, 'nativeMdlSha256': _digest(mdl)}


def _resolved_shader_input(shader, name):
    """Read USD's effective interface value or terminal shader output, then its Sdr default."""
    from pxr import Sdr
    input_ = shader.GetInput(name)
    attributes = input_.GetValueProducingAttributes() if input_ else []
    if len(attributes) > 1:
        raise ValueError('ambiguous effective shader input: '+name)
    if attributes:
        attribute = attributes[0]
        if attribute.GetName().startswith('outputs:'):
            return None, attribute
        else:
            value = attribute.Get()
            if value is not None:
                return value, None
            if attribute.HasAuthoredValueOpinion():
                raise ValueError('authored shader input has no usable value: '+name)
    if input_ and input_.GetRawConnectedSourcePaths():
        raise ValueError('shader input connection has no effective value: '+name)
    if input_ and input_.GetAttr().HasAuthoredValueOpinion() and not attributes:
        raise ValueError('authored shader input has no usable value: '+name)
    definition = Sdr.Registry().GetShaderNodeByIdentifier(shader.GetIdAttr().Get())
    property_ = definition.GetShaderInput(name) if definition else None
    value = property_.GetDefaultValue() if property_ else None
    if value is None:
        raise ValueError('registered SDK shader input default is unavailable: '+name)
    return value, None


def _auto_texture_is_srgb(image, data):
    """Resolve USD's 8-bit RGB auto rule without treating editing metadata as color."""
    byte_rgb = image.mode in ('RGB', 'RGBA') and (
        image.format == 'JPEG' and getattr(image, 'bits', None) == 8 or
        image.format == 'PNG' and len(data) > 24 and data[24] == 8)
    if not byte_rgb:
        return False
    if image.info.get('icc_profile'):
        from PIL import Image, ImageCms
        try:
            profile = ImageCms.ImageCmsProfile(BytesIO(image.info['icc_profile']))
            srgb = ImageCms.createProfile('sRGB')
            if not profile.profile.is_matrix_shaper or profile.profile.xcolor_space.strip() != 'RGB':
                return False
            for channel in ('red_colorant', 'green_colorant', 'blue_colorant'):
                if not np.allclose(getattr(profile.profile, channel)[0], getattr(srgb, channel)[0], atol=.001, rtol=0):
                    return False
            ramp = Image.fromarray(np.repeat(np.arange(256, dtype=np.uint8)[:, None, None], 3, axis=2))
            converted = ImageCms.profileToProfile(ramp, profile, srgb, outputMode='RGB')
            return converted.tobytes() == ramp.tobytes()
        except (OSError, ValueError, ImageCms.PyCMSError):
            return False
    exif = image.getexif()
    exif_space = exif.get_ifd(0x8769).get(0xA001) if 0x8769 in exif else exif.get(0xA001)
    if exif_space not in (None, 1):
        return False
    if 'gamma' in image.info and abs(image.info['gamma']-.45455) > .00001:
        return False
    if 'chromaticity' in image.info and not np.allclose(image.info['chromaticity'],
            [.3127, .3290, .64, .33, .30, .60, .15, .06], atol=.0001, rtol=0):
        return False
    return True


def _derive_preview_normal(document, binary, original_index, source_data, original_data, image, color_space, scale, bias):
    """Append a preview-only normal map; retain one global vector scale for filtering."""
    from PIL import Image
    vectors = np.asarray(image, dtype=float)[..., :3]/255
    if color_space == 'sRGB':
        vectors = np.where(vectors <= .04045, vectors/12.92, ((vectors+.055)/1.055)**2.4)
    vectors = vectors*np.asarray(scale[:3])+np.asarray(bias[:3])
    lengths = np.linalg.norm(vectors, axis=2)
    if not np.isfinite(vectors).all() or np.any(lengths <= 1e-12):
        raise ValueError('invalid or zero source normal vector')
    vector_scale = max(1, float(np.max(np.abs(vectors))))
    pixels = np.rint((vectors/vector_scale+1)*127.5).astype(np.uint8)
    decoded = (pixels.astype(float)/255*2-1)*vector_scale
    decoded_lengths = np.linalg.norm(decoded, axis=2)
    if np.any(decoded_lengths <= 1e-12):
        raise ValueError('zero quantized preview normal vector')
    errors = np.degrees(np.arccos(np.clip(np.sum(vectors*decoded, axis=2)/lengths/decoded_lengths, -1, 1)))
    component_error = float(np.max(np.abs(decoded-vectors)))
    if np.max(errors) > 1 or component_error > vector_scale/255+1e-12:
        raise ValueError('preview normal quantization exceeds one degree or RGB8 error bound')
    encoded = BytesIO()
    Image.fromarray(pixels, 'RGB').save(encoded, format='PNG')
    data = encoded.getvalue()
    provenance = {'algorithm': 'usd-rgb8-normal-to-gltf-v1', 'originalTextureIndex': original_index,
        'sourceImageSha256': hashlib.sha256(source_data).hexdigest(),
        'originalImageSha256': hashlib.sha256(original_data).hexdigest(),
        'derivedImageSha256': hashlib.sha256(data).hexdigest(), 'sourceColorSpace': color_space,
        'sourceScale': list(map(float, scale[:3])), 'sourceBias': list(map(float, bias[:3])),
        'vectorScale': vector_scale, 'maxDirectionErrorDegrees': float(np.max(errors)),
        'maxComponentError': component_error, 'componentErrorBound': vector_scale/255}
    for index, texture in enumerate(document['textures']):
        if texture.get('extras', {}).get('sceneagentNormalDerivation') != provenance:
            continue
        view = document['bufferViews'][document['images'][texture['source']]['bufferView']]
        existing = binary[view.get('byteOffset', 0):view.get('byteOffset', 0)+view['byteLength']]
        if hashlib.sha256(existing).hexdigest() != provenance['derivedImageSha256']:
            raise ValueError('existing derived normal image differs from source-bound derivation')
        return index, provenance
    if not isinstance(binary, bytearray) or len(document.get('buffers', [])) != 1:
        raise ValueError('derived preview normal requires one mutable embedded GLB buffer')
    original_texture = document['textures'][original_index]
    if original_texture.get('extensions'):
        raise ValueError('unsupported texture extension for derived preview normal')
    binary.extend(b'\x00'*(-len(binary) % 4))
    view_index, image_index = len(document['bufferViews']), len(document['images'])
    document['bufferViews'].append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': len(data)})
    binary.extend(data)
    binary.extend(b'\x00'*(-len(binary) % 4))
    document['buffers'][0]['byteLength'] = len(binary)
    document['images'].append({'bufferView': view_index, 'mimeType': 'image/png'})
    document['textures'].append({**original_texture, 'source': image_index, 'extras': {
        **original_texture.get('extras', {}), 'sceneagentNormalDerivation': provenance}})
    return len(document['textures'])-1, provenance


def _recover_material(stage, material, target, document, binary):
    from PIL import Image
    from pxr import UsdShade
    outputs = [output.GetConnectedSource() for output in material.GetSurfaceOutputs() if output.GetConnectedSource()]
    if len(outputs) != 1:
        raise ValueError('ambiguous effective material surface')
    shader = UsdShade.Shader(outputs[0][0].GetPrim())
    native = shader.GetIdAttr().Get() != 'UsdPreviewSurface'
    if native:
        source = _native_material(shader)
    else:
        resolved = {name: _resolved_shader_input(shader, name) for name in
            ('diffuseColor', 'opacity', 'roughness', 'metallic', 'emissiveColor', 'opacityThreshold')}
        threshold, threshold_output = resolved['opacityThreshold']
        if threshold_output is not None:
            raise ValueError('nonconstant opacity threshold cannot be represented in glTF')
        declared_mode = shader.GetPrim().GetCustomDataByKey('sceneagent:alphaMode')
        if declared_mode in ('OPAQUE', 'MASK') and threshold == 0 and (resolved['opacity'][0] != 1 or resolved['opacity'][1] is not None):
            raise ValueError('declared opaque/mask-zero provenance contradicts effective USD opacity')
        values = {name: value for name, (value, output) in resolved.items() if output is None}
        opacity_image_transparent = False
        normal_strength = None
        normal_derivation = None
        specs = [('diffuseColor', 'rgb', 'sRGB', 'baseColorTexture'), ('opacity', 'a', 'sRGB', 'baseColorTexture'),
                 ('roughness', 'g', 'raw', 'metallicRoughnessTexture'), ('metallic', 'b', 'raw', 'metallicRoughnessTexture'),
                 ('normal', 'rgb', 'raw', 'normalTexture'), ('occlusion', 'r', 'raw', 'occlusionTexture'),
                 ('emissiveColor', 'rgb', 'sRGB', 'emissiveTexture')]
        for name, channel, color_space, exported_name in specs:
            input_ = shader.GetInput(name)
            connected = (resolved[name] if name in resolved else _resolved_shader_input(shader, name))[1] if input_ else None
            if not connected:
                continue
            texture = UsdShade.Shader(connected.GetPrim())
            if connected.GetBaseName() != channel or texture.GetIdAttr().Get() != 'UsdUVTexture':
                raise ValueError('unsupported effective texture connection')
            texture_space, space_output = _resolved_shader_input(texture, 'sourceColorSpace')
            if space_output is not None or texture_space not in (('raw', 'sRGB', 'auto') if name == 'normal' else (color_space, 'auto')):
                raise ValueError('unsupported effective texture connection: sourceColorSpace')
            factors = [_resolved_shader_input(texture, field) for field in ('scale', 'bias')]
            if any(output is not None for _, output in factors):
                raise ValueError('nonconstant texture scale/bias cannot be represented in glTF')
            scale, bias = [list(value) for value, _ in factors]
            if name == 'normal':
                # The connected rgb output and glTF normalTexture both ignore alpha.
                normal_strength = scale[0]/2
                if not np.isfinite([*scale[:3], *bias[:3]]).all():
                    raise ValueError('nonfinite source normal scale/bias')
                normal_direct = texture_space == 'raw' and np.allclose(
                        [*scale[:3], *bias[:3]], [2*normal_strength, 2*normal_strength, 2,
                         -normal_strength, -normal_strength, -1], atol=2e-7, rtol=0)
            elif any(bias):
                raise ValueError('texture bias cannot be represented as a glTF factor')
            if name == 'occlusion' and scale != [1, 1, 1, 1]:
                raise ValueError('AO scale differs from glTF occlusion strength')
            holder = target.get('pbrMetallicRoughness', {}) if exported_name in ('baseColorTexture', 'metallicRoughnessTexture') else target
            if exported_name not in holder:
                raise ValueError('official export omitted a required source map')
            info = holder[exported_name]
            original_index = info['index']
            previous_derivation = document['textures'][original_index].get('extras', {}).get('sceneagentNormalDerivation') if name == 'normal' else None
            if previous_derivation:
                original_index = previous_derivation['originalTextureIndex']
                if normal_direct or info.get('scale', 1) != 1:
                    raise ValueError('existing derived normal conflicts with effective source decoding')
            image = document['images'][document['textures'][original_index]['source']]
            view = document['bufferViews'][image['bufferView']]
            data = binary[view.get('byteOffset', 0):view.get('byteOffset', 0)+view['byteLength']]
            asset, file_output = _resolved_shader_input(texture, 'file')
            if file_output is not None:
                raise ValueError('unsupported effective texture file connection')
            source_data = Path(asset.resolvedPath or asset.path).read_bytes()
            with Image.open(BytesIO(source_data)) as image:
                effective_space = texture_space
                if texture_space == 'auto':
                    if (color_space != 'sRGB' and name != 'normal') or not _auto_texture_is_srgb(image, source_data):
                        raise ValueError('unsupported effective texture connection: auto color encoding is unverified')
                    effective_space = 'sRGB'
                if name == 'normal' and not normal_direct and not (image.mode in ('RGB', 'RGBA') and (
                        image.format == 'JPEG' and getattr(image, 'bits', None) == 8 or
                        image.format == 'PNG' and len(source_data) > 24 and source_data[24] == 8)):
                    raise ValueError('derived preview normal requires verified RGB8 source encoding')
                expected_image = image.convert('RGBA')
            with Image.open(BytesIO(data)) as image:
                actual_image = image.convert('RGBA')
            if expected_image.size != actual_image.size or expected_image.tobytes() != actual_image.tobytes():
                raise ValueError('exported map was changed/baked; refusing duplicate factor application')
            if name == 'normal':
                actual, expected_strength = info.get('scale', 1), normal_strength if normal_direct else 1
                if not math.isfinite(actual) or (abs(actual-expected_strength) > 2e-7 and abs(actual-1) > 2e-7):
                    raise ValueError('unexpected existing normal scale; refusing overwrite')
                if not normal_direct:
                    index, normal_derivation = _derive_preview_normal(document, binary, original_index,
                        source_data, data, expected_image, effective_space, scale, bias)
                    if previous_derivation and previous_derivation != normal_derivation:
                        raise ValueError('existing derived normal conflicts with effective source decoding')
                    info['index'], normal_strength = index, 1
                info['scale'] = float(normal_strength)
            elif name == 'diffuseColor':
                values[name] = scale[:3]
            elif name == 'opacity':
                values[name] = scale[3]
                opacity_image_transparent = expected_image.getextrema()[3][0] < 255
            elif name in ('roughness', 'metallic'):
                values[name] = scale[1 if name == 'roughness' else 2]
            elif name == 'emissiveColor':
                values[name] = scale[:3]
        source = {'baseColor': [*values['diffuseColor'], values['opacity']],
            'roughness': values['roughness'], 'metallic': values['metallic'],
            'emissive': list(values['emissiveColor']), 'alphaCutoff': threshold}
        if normal_strength is not None:
            source['normalScale'] = float(normal_strength)
        if normal_derivation is not None:
            source['normalTextureDerivation'] = normal_derivation
        source['alphaMode'] = declared_mode or ('MASK' if threshold > 0 else 'BLEND'
            if source['baseColor'][3] < 1 or opacity_image_transparent else 'OPAQUE')
    pbr = target.setdefault('pbrMetallicRoughness', {})
    rgba = pbr.get('baseColorFactor', [1, 1, 1, 1])
    expected_rgba = list(map(float, source['baseColor']))
    if not np.allclose(rgba, expected_rgba, atol=2e-7, rtol=0):
        if native:
            diffuse = shader.GetInput('diffuse_color_constant')
            authored = bool(diffuse) and diffuse.GetAttr().HasAuthoredValueOpinion()
            known = 'baseColorFactor' not in pbr and not authored
        else:
            known = (np.allclose(rgba[:3], expected_rgba[:3], atol=2e-7, rtol=0) or rgba[:3] == [1, 1, 1]) and rgba[3] in (1, expected_rgba[3])
        if not known:
            raise ValueError('unexpected existing RGB/opacity factor; refusing overwrite')
    for field, name in [('roughnessFactor', 'roughness'), ('metallicFactor', 'metallic')]:
        actual, expected = pbr.get(field, 1), float(source[name])
        if not math.isfinite(actual) or (abs(actual-expected) > 2e-7 and not (
                field not in pbr or (native and field == 'metallicFactor' and actual == 0 and expected > 0))):
            raise ValueError('unexpected existing '+field+'; refusing overwrite')
    pbr['baseColorFactor'] = list(map(float, source['baseColor']))
    pbr['roughnessFactor'], pbr['metallicFactor'] = float(source['roughness']), float(source['metallic'])
    target['alphaMode'] = source['alphaMode']
    if source['alphaMode'] == 'MASK':
        target['alphaCutoff'] = float(source['alphaCutoff'])
    emission = np.asarray(source['emissive'], dtype=float)
    if not np.isfinite(emission).all() or np.any(emission < 0):
        raise ValueError('invalid effective emission')
    strength = max(1, float(np.max(emission)))
    target['emissiveFactor'] = (emission/strength).tolist()
    if native:
        extension = target.setdefault('extensions', {}).setdefault('KHR_materials_specular', {})
        if extension.get('specularFactor', .5) != .5 or extension.get('specularColorFactor', [1, 1, 1]) not in ([1, 1, 1], [2, 2, 2]):
            raise ValueError('unexpected native specular factor; refusing overwrite')
        extension.update(specularFactor=.5, specularColorFactor=[2, 2, 2])
        if 'KHR_materials_specular' not in document.setdefault('extensionsUsed', []):
            document['extensionsUsed'].append('KHR_materials_specular')
    if strength > 1:
        target.setdefault('extensions', {})['KHR_materials_emissive_strength'] = {'emissiveStrength': strength}
        if 'KHR_materials_emissive_strength' not in document.setdefault('extensionsUsed', []):
            document['extensionsUsed'].append('KHR_materials_emissive_strength')
    return source


def _preview_mesh_matches(mesh, triangles, faces=None):
    """Verify polygon coverage while allowing the converter to choose diagonals."""
    from collections import Counter
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=float)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=int)
    indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=int)
    triangles = np.asarray(triangles, dtype=float)
    if (points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all()
            or np.any(counts < 3) or counts.sum() != len(indices) or np.any(indices < 0)
            or np.any(indices >= len(points)) or triangles.ndim != 3 or triangles.shape[1:] != (3, 3)
            or not np.isfinite(triangles).all()):
        raise ValueError('invalid source/exported mesh topology')
    faces = np.arange(len(counts)) if faces is None else np.asarray(faces, dtype=int)
    if np.any(faces < 0) or np.any(faces >= len(counts)) or len(set(faces)) != len(faces):
        raise ValueError('invalid source face subset')
    offsets = np.r_[0, counts.cumsum()]
    if np.all(counts[faces] == 3):
        source = points[indices[offsets[faces, None]+np.arange(3)]]
        return _triangle_signature(source) == _triangle_signature(triangles)
    # USD's native mesh rendering fan-triangulates each authored face. Preserve
    # that exact surface, including concave fans and coincident pole vertices.
    fan = np.asarray([points[indices[[offsets[face], offsets[face]+i, offsets[face]+i+1]]]
                      for face in faces for i in range(1, counts[face]-1)])
    if _triangle_signature(fan) == _triangle_signature(triangles):
        return True
    polygons = [points[indices[offsets[face]:offsets[face+1]]] for face in faces]
    # Use authored face vertices, not an independently chosen tessellation. The
    # boundary and projected area checks reject omitted/overlapping triangles.
    keys = lambda vertices: [tuple(np.round(vertex, 6)) for vertex in vertices]
    polygon_keys = [keys(polygon) for polygon in polygons]
    by_vertex = {}
    for face, vertices in enumerate(polygon_keys):
        for vertex in vertices:
            by_vertex.setdefault(vertex, set()).add(face)
    groups = [[] for _ in polygons]
    for triangle in triangles:
        vertices = keys(triangle)
        candidates = set.intersection(*(by_vertex.get(vertex, set()) for vertex in vertices))
        if len(candidates) != 1:
            return False
        groups[candidates.pop()].append(vertices)
    edge = lambda a, b: tuple(sorted((a, b)))
    for polygon, vertices, group in zip(polygons, polygon_keys, groups):
        if len(set(vertices)) != len(vertices) or len(group) != len(vertices)-2:
            return False
        boundary = Counter(edge(vertices[i], vertices[(i+1) % len(vertices)]) for i in range(len(vertices)))
        actual = Counter(edge(triangle[i], triangle[(i+1) % 3]) for triangle in group for i in range(3))
        if any(actual[value] != 1 for value in boundary) or any(
                count != (1 if value in boundary else 2) for value, count in actual.items()):
            return False
        normal = np.cross(polygon, np.roll(polygon, -1, axis=0)).sum(axis=0)
        # Projected coverage proves alternate diagonals only for planar faces;
        # exact native fans, including nonplanar ones, already passed above.
        if np.max(np.abs((polygon-polygon[0])@normal)) > 1e-6*np.linalg.norm(normal):
            return False
        axis = int(np.argmax(np.abs(normal)))
        area = abs(normal[axis])/2
        positions = dict(zip(vertices, polygon))
        pieces = [np.asarray([positions[vertex] for vertex in triangle]) for triangle in group]
        areas = [abs(np.cross(piece[1]-piece[0], piece[2]-piece[0])[axis])/2 for piece in pieces]
        if area <= 1e-12 or any(value <= 1e-12 for value in areas) or not np.isclose(sum(areas), area, atol=1e-10, rtol=1e-6):
            return False
    return True


def _preview_mesh_index(stage, path, paths, worlds, document, binary):
    from pxr import Usd, UsdGeom
    index = paths.get(path)
    if index is not None and 'mesh' not in document['nodes'][index]:
        raise ValueError('exact source mesh path has no GLB geometry: '+path)
    prim = stage.GetPrimAtPath(path)
    parent = prim
    while parent and parent.IsValid() and str(parent.GetPath()) not in paths:
        parent = parent.GetParent()
    index = paths.get(str(parent.GetPath())) if parent else None
    if index is None or 'mesh' not in document['nodes'][index]:
        raise ValueError('source mesh has no verified ancestor fold: '+path)
    visible = []
    for child in Usd.PrimRange(parent, Usd.TraverseInstanceProxies()):
        if (not child.IsA(UsdGeom.Mesh) or UsdGeom.Imageable(child).ComputeVisibility() == UsdGeom.Tokens.invisible
                or UsdGeom.Imageable(child).ComputePurpose() not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render)):
            continue
        owner = child
        while owner and str(owner.GetPath()) not in paths:
            owner = owner.GetParent()
        # Exported child branches retain their own geometry and are checked
        # independently; only the source mesh folded into this node belongs here.
        if owner == parent:
            visible.append(child)
    if len(visible) != 1 or visible[0] != prim:
        raise ValueError('ambiguous source descendants cannot use mesh fold: '+path)
    if np.max(np.abs(worlds[index]-TO_GLTF@_usd_world(stage, path))) > 1e-6:
        raise ValueError('folded mesh world matrix differs from frozen USD: '+path)
    exported = []
    for primitive in document['meshes'][document['nodes'][index]['mesh']]['primitives']:
        vertices = _accessor(document, binary, primitive['attributes']['POSITION'])
        indices = _accessor(document, binary, primitive['indices']).reshape(-1) if 'indices' in primitive else np.arange(len(vertices))
        if primitive.get('mode', 4) != 4 or len(indices) % 3 or np.any(indices >= len(vertices)):
            raise ValueError('folded GLB mesh has invalid triangle topology')
        exported.extend(vertices[indices.reshape(-1, 3)])
    if not _preview_mesh_matches(UsdGeom.Mesh(prim), np.asarray(exported)):
        raise ValueError('folded mesh complete geometry differs from source: '+path)
    return index


def _preview_material_source(stage, prim, material_index, document, binary, primitive=None):
    from pxr import UsdGeom, UsdShade
    material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    if material_index is None:
        if material:
            raise ValueError('official export omitted a bound source material')
        primvars = UsdGeom.PrimvarsAPI(prim)
        expected, authored = {'displayColor': [1, 1, 1], 'displayOpacity': [1]}, False
        for name, width in (('displayColor', 3), ('displayOpacity', 1)):
            value = primvars.FindPrimvarWithInheritance(name)
            if value and value.GetAttr().HasAuthoredValueOpinion():
                authored = True
                data = np.asarray(value.ComputeFlattened(), dtype=float)
                if value.GetInterpolation() != UsdGeom.Tokens.constant or data.size != width:
                    raise ValueError('unbound nonconstant source appearance is not yet verified: '+name)
                if not np.isfinite(data).all() or np.any(data < 0) or np.any(data > 1):
                    raise ValueError('unbound source appearance exceeds supported color range: '+name)
                expected[name] = data.reshape(-1).tolist()
        # With no glTF material the effective alpha mode is OPAQUE: COLOR_0.a
        # is ignored, even when the official exporter writes it as zero.
        if abs(expected['displayOpacity'][0]-1) > 2e-7:
            raise ValueError('GLB opaque default cannot preserve source display opacity')
        if primitive is None:
            if authored:
                raise ValueError('unbound source appearance has no verified GLB color')
            return None
        color_index = primitive['attributes'].get('COLOR_0')
        if color_index is None:
            colors, tolerance = np.ones((1, 3)), 2e-7
        else:
            spec = document['accessors'][color_index]
            component = spec['componentType']
            if (spec['type'] not in ('VEC3', 'VEC4') or component not in (5121, 5123, 5126)
                    or spec.get('normalized', False) != (component != 5126)):
                raise ValueError('unsupported GLB vertex color encoding')
            colors = _accessor(document, binary, color_index)
            if (len(colors) != document['accessors'][primitive['attributes']['POSITION']]['count']
                    or not np.isfinite(colors).all() or np.any(colors < 0) or np.any(colors > 1)):
                raise ValueError('invalid GLB vertex color values or count')
            tolerance = 2e-7+(1/(255 if component == 5121 else 65535) if component != 5126 else 0)
        if not np.allclose(colors[:, :3], expected['displayColor'], atol=tolerance, rtol=0):
            raise ValueError('unbound source display color differs from GLB vertex color')
        return None
    target = document['materials'][material_index]
    if not material or material.GetPrim().GetName() != target.get('name'):
        raise ValueError('geometry-derived material identity differs from USD binding')
    return _recover_material(stage, material, target, document, binary)


@measure('glbBuild')
def export_preview(usd_path, path, bindings):
    """Official conversion plus source-bound, geometry-verified material factors."""
    import isaacsim.core.experimental.utils.app as app_utils
    from isaacsim.core.rendering_manager import RenderingManager as Render
    from pxr import Sdf, Usd, UsdGeom, UsdPhysics
    app_utils.enable_extension('omni.kit.asset_converter', enabled=True)
    import omni.kit.asset_converter as converter
    usd_path, path = Path(usd_path), Path(path)
    material_rendering = None
    material_audit_path = usd_path.parent/'native-stage-audit.json'
    if material_audit_path.is_file():
        material_audit = json.loads(material_audit_path.read_text())
        if material_audit.get('mdl') is not None:
            from runtime import mdl
            configure_native_materials(usd_path.parent, material_audit)
            _ensure_app()
            import isaacsim.core.experimental.utils.stage as stage_utils
            opened, material_stage = stage_utils.open_stage(str(usd_path))
            if not opened:
                raise ValueError('could not open frozen MDL source for preview identity verification')
            try:
                material_rendering = mdl.verify_native_materials(material_stage, usd_path.parent, material_audit)
            finally:
                material_stage = None
                if not stage_utils.close_stage():
                    raise ValueError('could not close MDL preview verification stage')
    before_sha = _digest(usd_path)
    from runtime.preview_geometry import prepare as prepare_preview_geometry
    conversion_source, primitive_geometry = prepare_preview_geometry(usd_path, path.with_name('preview-source.usda'))
    layer = Sdf.Layer.FindOrOpen(str(usd_path))
    layer.Reload(True)
    raw = path.with_name('preview-raw.glb')
    async def convert():
        context = converter.AssetConverterContext()
        context.ignore_camera = context.ignore_light = True
        context.ignore_materials = context.merge_all_meshes = context.single_mesh = False
        context.embed_textures = context.use_meter_as_world_unit = True
        task = converter.get_instance().create_converter_task(str(conversion_source), str(raw), None, context)
        success = await task.wait_until_finished()
        if not success or task.get_error_message():
            raise RuntimeError('official GLB conversion failed: '+str(task.get_error_message()))
    future, deadline = asyncio.ensure_future(convert()), time.monotonic()+180
    while not future.done() and time.monotonic() < deadline:
        Render.render()
    if not future.done():
        future.cancel()
        raise TimeoutError('official GLB conversion timed out')
    future.result()
    document, binary = _glb_document(raw)
    original_binary, binary = binary, bytearray(binary)
    stage = Usd.Stage.Open(str(conversion_source))
    children = {row['path'].split('/')[2] for row in primitive_geometry['sourcePrimitives']
                if row['path'].startswith('/World/')}
    paths, worlds = _index_nodes(document, children)
    mapped, material_sources, visible_count, excluded_non_render = [], {}, 0, []
    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        p = str(prim.GetPath())
        if not prim.IsA(UsdGeom.Mesh):
            continue
        purpose = UsdGeom.Imageable(prim).ComputePurpose()
        if purpose not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render):
            excluded_non_render.append({'primPath': p, 'purpose': str(purpose),
                'visibility': str(UsdGeom.Imageable(prim).ComputeVisibility()),
                'collisionAPI': prim.HasAPI(UsdPhysics.CollisionAPI)})
            continue
        if UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        visible_count += 1
        index = _preview_mesh_index(stage, p, paths, worlds, document, binary)
        if index is None or 'mesh' not in document['nodes'][index] or np.max(np.abs(worlds[index]-TO_GLTF@_usd_world(stage, p))) > 1e-6:
            raise ValueError('preview mesh hierarchy/world pose differs from frozen USD: '+p)
        mesh = UsdGeom.Mesh(prim)
        face_count = len(mesh.GetFaceVertexCountsAttr().Get())
        glb_mesh = document['meshes'][document['nodes'][index]['mesh']]
        candidates = []
        for i, primitive in enumerate(glb_mesh['primitives']):
            pos = _accessor(document, binary, primitive['attributes']['POSITION'])
            face = _accessor(document, binary, primitive['indices']).reshape(-1) if 'indices' in primitive else np.arange(len(pos))
            if primitive.get('mode', 4) != 4 or len(face) % 3 or np.any(face >= len(pos)):
                raise ValueError('unsupported GLB visual topology')
            candidates.append((i, pos[face.reshape(-1, 3)], primitive.get('material')))
        subsets = [child for child in prim.GetChildren() if child.IsA(UsdGeom.Subset)]
        groups = [(child, np.asarray(UsdGeom.Subset(child).GetIndicesAttr().Get(), dtype=int)) for child in subsets] if subsets else [(prim, np.arange(face_count))]
        used = set()
        for child, faces in groups:
            matches = [value for value in candidates if _preview_mesh_matches(mesh, value[1], faces)]
            if len(matches) != 1 or matches[0][0] in used:
                raise ValueError('ambiguous triangle/subset GLB binding')
            used.add(matches[0][0])
            material_index = matches[0][2]
            source = _preview_material_source(stage, child, material_index, document, binary,
                glb_mesh['primitives'][matches[0][0]])
            if material_index is not None:
                if material_index in material_sources and material_sources[material_index] != source:
                    raise ValueError('GLB material index has conflicting source bindings')
                material_sources[material_index] = source
            mapped.append({'usdMesh': p, 'usdSubset': str(child.GetPath()), 'glbNode': index, 'glbMaterial': material_index})
        if used != set(range(len(candidates))):
            raise ValueError('unmapped GLB primitive')
        roots = [(id, root) for id, root in bindings.get('objectPaths', {}).items() if p == root or p.startswith(root+'/')]
        object_id = max(roots, key=lambda row: len(row[1]))[0] if roots else None
        extras = document['nodes'][index].setdefault('extras', {})
        extras['appearanceCompiled'] = True
        if object_id is not None:
            extras['objectId'] = object_id
    if visible_count == 0 or _digest(usd_path) != before_sha:
        raise ValueError('empty preview or frozen USD changed during conversion')
    expected_primitives = {row['path'] for row in primitive_geometry['sourcePrimitives']}
    if {row['usdMesh'] for row in mapped} != expected_primitives:
        raise ValueError('preview does not cover every visible native primitive')
    for link in bindings.get('links', []):
        if link not in paths:
            raise ValueError('robot link missing from preview')
        error = _pose_error(worlds[paths[link]], TO_GLTF@_usd_world(stage, link))
        if error[0] > .001 or error[1] > .002:
            raise ValueError('preview robot initial joint pose mismatch')
    if binary[:len(original_binary)] != original_binary:
        raise ValueError('preview derivation changed original binary geometry/textures')
    _write_glb(path, document, binary)
    if _glb_document(path)[1] != binary:
        raise ValueError('preview serialization changed verified binary geometry/textures')
    evidence = {'algorithm': 'frozen-usd-effective-materials-v4', 'sourceUsdSha256': before_sha,
        'rawGlbSha256': _digest(raw), 'previewSha256': _digest(path), 'binaryUnchanged': binary == original_binary,
        'originalBinaryPreservedAsPrefix': True,
        'visibleMeshes': visible_count, 'bindings': mapped, 'materialSources': material_sources,
        'primitiveGeometry': primitive_geometry,
        'renderPurposes': ['default', 'render'], 'excludedNonRenderMeshes': excluded_non_render,
        'absolutePhotometryVerified': False, 'fullBSDFVerified': False}
    if material_rendering is not None:
        evidence['mdl'] = material_rendering
    atomic_json(path.with_name('preview-evidence.json'), evidence)
    if conversion_source != usd_path:
        conversion_source.unlink()
    return evidence






def render_asset_thumbnail(node, directory):
    """RTX thumbnail uses exactly the same authored mesh/PBR path as scene builds."""
    _ensure_app()
    from PIL import Image
    from pxr import Gf, Usd, UsdGeom, UsdLux
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    from runtime.scene import World
    world = World(native=True)
    bounds = _asset_scene(node).bounds
    world.asset('asset_preview', node['path'], (bounds[1]-bounds[0]).tolist(), node['up_axis'], color=node.get('color', [1, 1, 1, 1]))
    normalize_assets(world)
    parts = _asset_parts(world.nodes['asset_preview'])
    extent = float(np.linalg.norm(world.nodes['asset_preview']['size']))
    path = directory/'thumbnail.usda'
    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, '/World').GetPrim())
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    for index, mesh in enumerate(parts):
        _write_mesh(stage, '/World/asset/visual_'+str(index), mesh, directory)
    UsdLux.DomeLight.Define(stage, '/World/lights/fill').CreateIntensityAttr(700)
    key = UsdLux.SphereLight.Define(stage, '/World/lights/key')
    key.CreateIntensityAttr(30000)
    key.CreateRadiusAttr(extent*.3)
    UsdGeom.Xformable(key).AddTranslateOp().Set(Gf.Vec3d(extent, -2*extent, 2*extent))
    _write_camera(stage, {'lookat': [0, 0, 0], 'distance': extent*1.7, 'azimuth': -55, 'elevation': -25,
        'width': WIDTH, 'height': HEIGHT, 'scope': 'asset_preview'})
    stage.GetRootLayer().Save()
    stage = None
    _setup_stage(path)
    import warp as wp
    import isaacsim.core.experimental.utils.app as app_utils
    from isaacsim.sensors.experimental.rtx import RtxCamera, CameraSensor
    from isaacsim.core.rendering_manager import RenderingManager as Render
    from isaacsim.core.experimental.utils.backend import use_backend
    with use_backend('usd', raise_on_unsupported=True, raise_on_fallback=True):
        camera = RtxCamera('/World/camera', tick_rate=0.0, reset_xform_op_properties=False)
        sensor = CameraSensor(camera, resolution=(HEIGHT, WIDTH), annotators=['rgb'])
    output = wp.empty((HEIGHT, WIDTH, 3), dtype=wp.uint8, device='cpu')
    try:
        app_utils.play(commit=True)
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            with measure('render'):
                Render.render()
            data, _ = sensor.get_data('rgb', out=output)
            if data is not None:
                image = data.numpy().copy()
                if image.shape == (HEIGHT, WIDTH, 3) and image.std() > 5:
                    with measure('imageWrite'):
                        Image.fromarray(image).resize((320, 320)).save(directory/'thumbnail.png')
                    return str((directory/'thumbnail.png').resolve())
        raise TimeoutError('actual RTX asset thumbnail did not produce pixels')
    finally:
        sensor.detach_annotators(sensor.annotators)
        _close_stage()
