"""Synchronous host skills; movement comes only from native PhysX drives."""
import math
from numbers import Real
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


CONTROL_APIS = {
    'step': 'frozen named native position/velocity/effort commands on one physical clock; not task success',
}

NATIVE_IO_SCHEMA = 'sceneagent-native-io-v1'
PROFILE_SCHEMA = 'sceneagent-robot-profile-v1'


def _fields(value, required, optional, label):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value)-set(required)-set(optional):
        raise ValueError('invalid or unknown '+label+' fields')


def _text(value, label):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('profile requires nonempty '+label)
    return value


def _relative(value, *, prim=False, directory=False):
    _text(value, 'relative path')
    path = PurePosixPath(value)
    if (path.is_absolute() or '\\' in value or '..' in path.parts or str(path) != value
            or value == '.' and not (prim or directory)
            or prim and value != '.' and any(not part.isidentifier() for part in path.parts)):
        raise ValueError('profile path must be canonical and relative within its model or resource package')
    return value


def _names(value, label):
    if (not isinstance(value, list) or not value or any(not isinstance(name, str) or not name for name in value)
            or len(set(value)) != len(value)):
        raise ValueError('profile requires unique '+label)


def _number(value, label, *, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value) or nonnegative and value < 0:
        raise ValueError('profile requires finite '+label)


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:raise ValueError('duplicate JSON key: '+key)
            result[key] = value
        return result
    def constant(value):raise ValueError('nonfinite JSON number: '+value)
    def finite(value):
        result = float(value)
        if not math.isfinite(result):raise ValueError('nonfinite JSON number: '+value)
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)


def validate_planning_config(config):
    """Validate public native data, not a second implementation of cuRobo's schema."""
    if not isinstance(config, dict) or not isinstance(config.get('kinematics'), dict):
        raise ValueError('planning config must be an inner native object containing kinematics')
    try:plain = _json(json.dumps(config, allow_nan=False))
    except (TypeError, ValueError, OverflowError):raise ValueError('planning config requires plain finite JSON data') from None
    if plain != config:raise ValueError('planning config requires plain JSON keys and arrays')
    def numbers(value):
        if isinstance(value, dict):
            for child in value.values():numbers(child)
        elif isinstance(value, list):
            for child in value:numbers(child)
        else:_number(value, 'native planning numeric value')
    kin = config['kinematics']
    for name in ('lock_joints', 'collision_sphere_buffer', 'self_collision_buffer'):
        if kin.get(name) is not None:numbers(kin[name])
    if isinstance(kin.get('collision_spheres'), dict):numbers(kin['collision_spheres'])
    cspace = kin.get('cspace', {})
    if isinstance(cspace, dict):
        for name in ('retract_config', 'null_space_weight', 'cspace_distance_weight', 'max_acceleration', 'max_jerk'):
            if cspace.get(name) is not None:numbers(cspace[name])
    if isinstance(kin.get('extra_links'), dict):
        for link in kin['extra_links'].values():
            if isinstance(link, dict) and link.get('fixed_transform') is not None:numbers(link['fixed_transform'])
    return config


def parse_profile(text):
    """One public pure-data contract. This function never imports an SDK."""
    profile = _json(text)
    _fields(profile, ('schema', 'model', 'ports', 'streams'), ('planning', 'initialJoints', 'drives'), 'robot profile')
    if profile['schema'] != PROFILE_SCHEMA:raise ValueError('unsupported robot profile schema')
    model = profile['model']
    _fields(model, ('resourceRef',), ('file', 'prim', 'variants', 'importOptions'), 'model')
    _text(model['resourceRef'], 'model resourceRef')
    if 'file' in model:_relative(model['file'])
    if 'prim' in model:
        path = _text(model['prim'], 'USD source prim')
        if not path.startswith('/') or path == '/' or any(not part.isidentifier() for part in path[1:].split('/')):
            raise ValueError('USD source prim must be an absolute prim path')
    if 'variants' in model:
        if not isinstance(model['variants'], dict):raise ValueError('model variants must be a string map')
        for key, value in model['variants'].items():_text(key, 'variant set');_text(value, 'variant selection')
    if 'importOptions' in model:
        options = model['importOptions']
        boolean = ('merge_fixed_joints', 'merge_mesh', 'allow_self_collision', 'collision_from_visuals',
                   'run_asset_transformer', 'run_multi_physics_conversion')
        numeric = ('override_joint_stiffness', 'override_joint_damping', 'link_density')
        _fields(options, (), (*boolean, *numeric, 'fix_base', 'collision_type', 'joint_drive_type', 'joint_target_type'), 'URDF importOptions')
        for name, value in options.items():
            if name in boolean and type(value) is not bool or name == 'fix_base' and value is not None and type(value) is not bool:
                raise ValueError('URDF option must be boolean: '+name)
            if name in numeric and value is not None:
                _number(value, 'URDF '+name, nonnegative=True)
                if name == 'link_density' and value == 0:raise ValueError('URDF link_density must be positive')
            choices = {'collision_type': ('Convex Hull', 'Convex Decomposition', 'Bounding Sphere', 'Bounding Cube'),
                       'joint_drive_type': ('force', 'acceleration', None), 'joint_target_type': ('none', 'position', 'velocity', None)}
            if name in choices and value not in choices[name]:raise ValueError('unsupported URDF option: '+name)
    for kind in ('ports', 'streams'):
        if not isinstance(profile[kind], dict):raise ValueError('profile requires named '+kind)
        actuators = set()
        for name, declaration in profile[kind].items():
            if not isinstance(name, str) or not name.isidentifier():raise ValueError('profile local names must be identifiers')
            _fields(declaration, ('source',), () if kind == 'ports' else ('rateHz', 'visibleToPolicy', 'required'), kind+' declaration')
            source = declaration['source']
            if kind == 'ports':
                _fields(source, ('primitive', 'path', 'joints'), (), 'command source')
                if source['primitive'] not in ('joint_position', 'joint_velocity', 'joint_effort'):
                    raise ValueError('unsupported native command primitive')
            else:
                if not isinstance(source, dict):raise ValueError('invalid native stream source')
                reader = source.get('reader')
                joint = reader in ('joint_positions', 'joint_velocities', 'joint_position_targets', 'joint_velocity_targets', 'joint_efforts')
                if not joint and reader not in ('root_pose', 'root_velocity', 'rigid_pose', 'rigid_velocity', 'particle_state'):
                    raise ValueError('unsupported robot profile reader; cameras and scene contacts are declared separately')
                _fields(source, ('reader', 'path', 'joints') if joint else ('reader', 'path', 'field') if reader == 'particle_state' else ('reader', 'path'), (), 'stream source')
                if reader == 'particle_state' and source['field'] not in ('positions', 'velocities'):
                    raise ValueError('unsupported particle field')
                if 'rateHz' in declaration and (type(declaration['rateHz']) is not int or declaration['rateHz'] <= 0):
                    raise ValueError('profile sampling rate must be a positive integer')
                if any(key in declaration and type(declaration[key]) is not bool for key in ('visibleToPolicy', 'required')):
                    raise ValueError('profile stream permission must be boolean')
            _relative(source['path'], prim=True)
            if 'joints' in source:
                _names(source['joints'], 'native joints')
                if kind == 'ports':
                    for joint in source['joints']:
                        identity = (source['path'], joint)
                        if identity in actuators:raise ValueError('profile control ports overlap an actuator')
                        actuators.add(identity)
    if 'planning' in profile:
        planning = profile['planning']
        _fields(planning, ('resourceRef', 'ports'), ('file', 'config', 'assetRoot', 'jointMap', 'linkMap'), 'planning')
        _text(planning['resourceRef'], 'planning resourceRef');_names(planning['ports'], 'planning ports')
        if 'config' in planning:
            if 'file' in planning:raise ValueError('planning config and file are mutually exclusive')
            validate_planning_config(planning['config'])
        if 'file' in planning:_relative(planning['file'])
        if 'assetRoot' in planning:_relative(planning['assetRoot'], directory=True)
        for port in planning['ports']:
            if port not in profile['ports']:raise ValueError('planning references an unknown profile port')
        if 'jointMap' in planning:
            if not isinstance(planning['jointMap'], dict):raise ValueError('planning jointMap must be an object')
            mapped = set()
            for name, value in planning['jointMap'].items():
                _text(name, 'planner joint');_fields(value, ('port', 'joint'), (), 'planning joint mapping')
                if value['port'] not in planning['ports'] or value['joint'] not in profile['ports'][value['port']]['source']['joints']:
                    raise ValueError('planning jointMap references an unknown actuator')
                identity = (value['port'], value['joint'])
                if identity in mapped:raise ValueError('planning jointMap duplicates an actuator')
                mapped.add(identity)
        if 'linkMap' in planning:
            if not isinstance(planning['linkMap'], dict):raise ValueError('planning linkMap must be an object')
            for name, path in planning['linkMap'].items():_text(name, 'planner link');_relative(path, prim=True)
    for kind, fields in (('initialJoints', ('position', 'velocity')), ('drives', ('stiffness', 'damping', 'targetPosition', 'targetVelocity'))):
        if kind not in profile:continue
        if not isinstance(profile[kind], dict):raise ValueError('profile '+kind+' must be a path map')
        for path, values in profile[kind].items():
            _relative(path, prim=True);_fields(values, (), fields, kind)
            if not values:raise ValueError('empty '+kind+' declaration')
            for key, value in values.items():_number(value, kind+' '+key, nonnegative=key in ('stiffness', 'damping'))
    return profile


def _plain_file(path, root):
    path, root = Path(path), Path(root)
    try:relative = path.relative_to(root)
    except ValueError:raise ValueError('file escapes its frozen source or resource root') from None
    current = root
    for part in ('', *relative.parts):
        current = current/part
        if current.is_symlink():raise ValueError('frozen input contains a symbolic link')
    if not path.is_file():raise ValueError('frozen input file is missing')
    return path.resolve()


def load_profile(path, source_root=None):
    path = Path(path)
    if not path.name.endswith('.robot.json') or '..' in path.parts:raise ValueError('profile requires a frozen *.robot.json source')
    if not path.is_absolute() and source_root is None:raise ValueError('relative profile requires an explicit frozen source_root')
    root = Path(source_root).absolute() if source_root is not None else path.parent
    if not path.is_absolute():path = root/path
    return parse_profile(_plain_file(path, root).read_text())


def resolve_resource(descriptor, resources):
    """Resolve only files in the report owning the host-provided absolute entry."""
    _fields(descriptor, ('resourceRef',), ('file', 'prim', 'variants', 'importOptions',
        'ports', 'jointMap', 'assetRoot', 'linkMap', 'config'), 'resource descriptor')
    _text(descriptor['resourceRef'], 'resourceRef')
    if not isinstance(resources, dict) or descriptor['resourceRef'] not in resources:
        raise ValueError('profile references an undeclared frozen resource')
    entry = Path(resources[descriptor['resourceRef']])
    if not entry.is_absolute() or '..' in entry.parts:raise ValueError('frozen resource entry must be explicit and absolute')
    matches = []
    for root in entry.parents:
        report_path = root/'report.json'
        if report_path.is_file() and not report_path.is_symlink():
            report = _json(report_path.read_text())
            if isinstance(report, dict) and report.get('kind') == 'native_files':
                name = report.get('entry')
                if isinstance(name, str) and _relative(name) and root/name == entry:matches.append((root, report))
    if len(matches) != 1:raise ValueError('frozen resource entry has no unique matching native_files report')
    root, report = matches[0]
    names = report.get('nativeFiles')
    if (not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names) or report['entry'] not in names):
        raise ValueError('invalid native resource file manifest')
    for name in names:
        _relative(name)
        if not name.startswith('files/'):raise ValueError('invalid native resource member')
    _plain_file(entry, root)
    name = descriptor.get('file', report['entry']);_relative(name)
    if name not in names:raise ValueError('resource file is not declared in nativeFiles')
    selected = _plain_file(root/name, root)
    for member in {name, report['entry']}:
        expected = report.get('frozenDigests', {}).get(member)
        digest = hashlib.sha256()
        with _plain_file(root/member, root).open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024), b''):digest.update(block)
        if not isinstance(expected, str) or digest.hexdigest() != expected:
            raise ValueError('frozen resource digest changed or missing')
    return selected, root.resolve()


def _import_urdf(path, output, options):
    """Only the pinned official importer runs, inside the existing authoring sandbox."""
    from runtime.simulation import _ensure_app
    _ensure_app()
    import isaacsim.core.experimental.utils.app as app_utils
    app_utils.enable_extension('isaacsim.asset.importer.urdf', enabled=True)
    from isaacsim.asset.importer.urdf import URDFImporter, URDFImporterConfig
    configuration = URDFImporterConfig(urdf_path=str(path), usd_path=str(output), **options)
    return _plain_file(Path(URDFImporter(configuration).import_urdf()), output)


def _urdf_resources(path, descriptor, resources, bundle):
    import xml.etree.ElementTree as ET
    for element in ET.parse(path).iter():
        if element.tag not in ('mesh', 'texture'):continue
        filename = element.get('filename')
        if not isinstance(filename, str) or not filename or '://' in filename or '\\' in filename:
            raise ValueError('URDF mesh/texture requires an explicit frozen package file; URI fallback is unsupported')
        target = Path(os.path.normpath(str(path.parent/filename)))
        try:relative = target.relative_to(bundle).as_posix()
        except ValueError:raise ValueError('URDF mesh/texture escapes its frozen resource package') from None
        resolve_resource({'resourceRef':descriptor['resourceRef'], 'file':relative}, resources)


def _model_link_matches(prims, name):
    from pxr import UsdGeom
    official = any('IsaacLinkAPI' in prim.GetPrimTypeInfo().GetAppliedAPISchemas() for prim in prims)
    return [prim for prim in prims if UsdGeom.Xformable(prim)
            and (prim.GetName() == name or prim.GetDisplayName() == name)
            and (not official or 'IsaacLinkAPI' in prim.GetPrimTypeInfo().GetAppliedAPISchemas()
                 or prim.GetCustomDataByKey('sceneagent:urdfLink') == name)]


def _restore_fixed_frames(stage, model, urdf):
    """Merged massless URDF links remain authored fixed frames, never fake bodies."""
    import xml.etree.ElementTree as ET
    import numpy as np
    from pxr import Gf, Usd, UsdGeom
    from trimesh.transformations import euler_matrix
    pending = [joint for joint in ET.parse(urdf).findall('joint') if joint.get('type') == 'fixed']
    names = None
    while pending:
        remaining, changed = [], False
        for joint in pending:
            parent, child = joint.find('parent').get('link'), joint.find('child').get('link')
            prims = list(Usd.PrimRange(model, Usd.TraverseInstanceProxies()))
            children = _model_link_matches(prims, child)
            if len(children) > 1:raise ValueError('ambiguous imported fixed frame: '+child)
            if children:continue
            parents = _model_link_matches(prims, parent)
            if len(parents) > 1:raise ValueError('ambiguous imported fixed frame: '+parent)
            if len(parents) != 1:
                remaining.append(joint)
                continue
            _text(child, 'URDF fixed frame name')
            origin = joint.find('origin')
            xyz = [float(value) for value in (origin.get('xyz', '0 0 0') if origin is not None else '0 0 0').split()]
            rpy = [float(value) for value in (origin.get('rpy', '0 0 0') if origin is not None else '0 0 0').split()]
            native_values(xyz, [3], 'fixed frame position');native_values(rpy, [3], 'fixed frame rotation')
            matrix = euler_matrix(*rpy, axes='sxyz');matrix[:3, 3] = xyz
            if names is None:
                import usdex.core
                names = usdex.core.NameCache()
            name = names.getPrimName(parents[0], child)
            if not name:raise ValueError('invalid imported fixed frame name: '+child)
            target = parents[0].GetPath().AppendChild(name)
            if stage.GetPrimAtPath(target):raise ValueError('imported fixed frame target already exists: '+str(target))
            frame = UsdGeom.Xform.Define(stage, target)
            frame.GetPrim().SetCustomDataByKey('sceneagent:urdfLink', child)
            if name != child:usdex.core.setDisplayName(frame.GetPrim(), child)
            frame.AddTransformOp().Set(Gf.Matrix4d(*matrix.T.reshape(-1).tolist()))
            changed = True
        if remaining and not changed:raise ValueError('fixed URDF frames have no unique imported ancestor')
        pending = remaining


def _joint_configuration(stage, root, profile):
    from pxr import UsdPhysics
    for kind in ('initialJoints', 'drives'):
        for path, values in profile.get(kind, {}).items():
            prim = stage.GetPrimAtPath(root if path == '.' else root+'/'+path)
            angular = prim.IsA(UsdPhysics.RevoluteJoint)
            if not angular and not prim.IsA(UsdPhysics.PrismaticJoint):
                raise ValueError('profile configuration requires an actual revolute/prismatic joint: '+path)
            instance = 'angular' if angular else 'linear'
            if kind == 'initialJoints':
                from pxr import PhysxSchema
                state = PhysxSchema.JointStateAPI.Apply(prim, instance)
                for field, value in values.items():
                    converted = math.degrees(value) if angular else value
                    (state.CreatePositionAttr if field == 'position' else state.CreateVelocityAttr)(float(converted))
            else:
                drive = UsdPhysics.DriveAPI.Apply(prim, instance)
                writers = {'stiffness': drive.CreateStiffnessAttr, 'damping': drive.CreateDampingAttr,
                           'targetPosition': drive.CreateTargetPositionAttr, 'targetVelocity': drive.CreateTargetVelocityAttr}
                for field, value in values.items():
                    converted = (math.radians(value) if field in ('stiffness', 'damping') else math.degrees(value)) if angular else value
                    writers[field](float(converted))


def _planning_frames(stage, root, profile, resources, streams, instance, clock):
    if 'planning' not in profile:return {}
    import numpy as np
    from pxr import Usd, UsdGeom, UsdPhysics
    planning = profile['planning']
    path, _ = resolve_resource(planning, resources)
    if 'config' in planning:
        data = planning['config']
    else:
        import yaml
        data = yaml.safe_load(path.read_text())
        data = data.get('robot_cfg', data) if isinstance(data, dict) else None
    kin = validate_planning_config(data)['kinematics']
    links = [kin.get('base_link'), kin.get('ee_link'), *(kin.get('link_names') or [])]
    if any(not isinstance(link, str) or not link for link in links):raise ValueError('planning model requires actual base/tool link names')
    links = list(dict.fromkeys(links))
    mapping = profile['planning'].get('linkMap', {})
    if set(mapping)-set(links):raise ValueError('planning linkMap contains a link absent from its model')
    prims = list(Usd.PrimRange(stage.GetPrimAtPath(root), Usd.TraverseInstanceProxies()))
    result, by_body = {}, {}
    physics, control, _ = native_clock(clock)
    for link in links:
        matches = ([stage.GetPrimAtPath(root if mapping[link] == '.' else root+'/'+mapping[link])]
                   if link in mapping else _model_link_matches(prims, link))
        if len(matches) != 1 or not matches[0] or not UsdGeom.Xformable(matches[0]):
            raise ValueError('planner link has no unique actual model prim: '+link)
        prim, body = matches[0], matches[0]
        while body and str(body.GetPath()).startswith(root) and not body.HasAPI(UsdPhysics.RigidBodyAPI):
            transform = UsdGeom.Xformable(body)
            if transform and (transform.TransformMightBeTimeVarying() or transform.GetResetXformStack()):
                raise ValueError('planner fixed frame is time-varying or resets its rigid-body ancestry: '+link)
            body = body.GetParent()
        if (not body or not str(body.GetPath()).startswith(root) or not body.HasAPI(UsdPhysics.RigidBodyAPI)
                or UsdPhysics.RigidBodyAPI(body).GetRigidBodyEnabledAttr().Get() is False):
            raise ValueError('planner link has no actual rigid-body ancestor: '+link)
        body_path = str(body.GetPath())
        if body_path not in by_body:
            reusable = [name for name, spec in streams.items()
                if spec['source'] == {'reader':'rigid_pose', 'path':body_path}
                and type(spec.get('rateHz', control)) is int and spec.get('rateHz', control) >= control
                and physics % spec.get('rateHz', control) == 0 and spec.get('rateHz', control) % control == 0]
            if len(reusable) > 1:raise ValueError('ambiguous profile raw body-pose streams')
            if not reusable:
                raise ValueError('planner frame requires an explicit rigid_pose stream for '+body_path
                    +' at a rate divisible by controlRateHz and dividing physicsRateHz; declare policy visibility and required explicitly')
            by_body[body_path] = reusable[0]
        world_body = np.asarray(UsdGeom.Xformable(body).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T
        world_link = np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())).T
        matrix = np.linalg.inv(world_body)@world_link
        if (not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0,0,0,1], atol=1e-9, rtol=0)
                or not np.allclose(matrix[:3,:3].T@matrix[:3,:3], np.eye(3), atol=1e-7, rtol=0)
                or abs(np.linalg.det(matrix[:3,:3])-1) > 1e-7):
            raise ValueError('planner fixed frame is not a rigid transform')
        result[link] = {'stream':by_body[body_path], 'bodyPath':body_path, 'bodyFromLink':matrix.tolist(),
            'scope':'authored static fixed-frame conversion of a real body-pose stream; not a direct native TCP measurement'}
    return result


def load_robot(world, id, profile_path, *, owner=None, at=(0,0,0), orientation=(1,0,0,0), frame=None, purpose=''):
    """Author one profile instance. Formal SDK discovery still owns its actual I/O."""
    from runtime.scene import valid_id
    valid_id(id)
    native_clock(world.io['clock'])
    profile = load_profile(profile_path, getattr(world, 'source_root', None))
    if id in world.nodes:raise ValueError('robot instance ID already exists')
    if owner is not None and owner not in world.nodes:raise ValueError('unknown robot owner')
    if isinstance(frame, str) and frame not in world.nodes:raise ValueError('unknown robot frame')
    position = native_values(at, [3], 'installation position')
    quaternion = native_values(orientation, [4], 'installation quaternion')
    if abs(sum(float(x*x) for x in quaternion)-1) > 1e-8:raise ValueError('installation quaternion must be unit wxyz')
    stage = world.stage
    layer = stage.GetRootLayer()
    if not layer.realPath:raise ValueError('robot construction requires the real isolated output stage')
    directory = Path(layer.realPath).resolve().parent
    resources = {ref:str(Path(entry) if Path(entry).is_absolute() else directory/entry) for ref, entry in world.resources.items()}
    source, bundle = resolve_resource(profile['model'], resources)
    suffix = source.suffix.lower()
    if suffix not in ('.usd', '.usda', '.usdc', '.urdf'):raise ValueError('profile model requires USD or URDF')
    if suffix == '.urdf' and any(key in profile['model'] for key in ('prim', 'variants')):
        raise ValueError('URDF model cannot declare USD prim or variants')
    if suffix != '.urdf' and 'importOptions' in profile['model']:
        raise ValueError('USD model cannot declare URDF importOptions')
    if suffix == '.urdf':_urdf_resources(source, profile['model'], resources, bundle)
    ports = {id+'.'+name:deepcopy(value) for name, value in profile['ports'].items()}
    streams = {id+'.'+name:deepcopy(value) for name, value in profile['streams'].items()}
    if set(ports)&set(world.io['ports']) or set(streams)&set(world.io['streams']):
        raise ValueError('robot instance I/O namespace already exists')
    from pxr import Gf, Usd, UsdGeom
    from runtime.simulation import recording_camera_pose
    mount = '/World/robot_'+id.encode().hex()
    root = mount+'/model'
    if stage.GetPrimAtPath(mount):raise ValueError('robot installation prim already exists')
    placement = recording_camera_pose(world, {'frame':'world' if frame is None else frame,
        'at':position.tolist(), 'orientation':quaternion.tolist()}, stage=stage)
    for declaration in [*ports.values(), *streams.values()]:
        relative = declaration['source']['path']
        declaration['source']['path'] = root if relative == '.' else root+'/'+relative
        if 'rateHz' in declaration and world.io['clock']['physicsRateHz'] % declaration['rateHz']:
            raise ValueError('profile stream rate must divide the scene physics rate')
    previous_nodes = set(world.nodes)
    previous_ports, previous_streams = set(world.io['ports']), set(world.io['streams'])
    imported = None
    try:
        model_source = source
        if suffix == '.urdf':
            destination = directory/'robot-imports'/id.encode().hex()
            if destination.exists():raise ValueError('robot import output already exists')
            imported = destination
            model_source = _import_urdf(source, imported, profile['model'].get('importOptions', {}))
        model_stage = Usd.Stage.Open(str(model_source))
        if (UsdGeom.GetStageUpAxis(model_stage) != UsdGeom.Tokens.z
                or abs(UsdGeom.GetStageMetersPerUnit(model_stage)-1) > 1e-9):
            raise ValueError('robot source must declare metre units and Z-up; no implicit rescaling is applied')
        source_prim = (model_stage.GetPrimAtPath(profile['model']['prim']) if 'prim' in profile['model']
                       else model_stage.GetDefaultPrim())
        if not source_prim:raise ValueError('robot model has no selected source prim')
        UsdGeom.Xform.Define(stage, mount).AddTransformOp().Set(Gf.Matrix4d(*placement.T.reshape(-1).tolist()))
        model = stage.DefinePrim(root)
        reference = model_source.relative_to(directory).as_posix()
        model.GetReferences().AddReference(reference, source_prim.GetPath())
        if not UsdGeom.Xformable(model):raise ValueError('selected robot source prim must be Xformable')
        for name, choice in profile['model'].get('variants', {}).items():
            sets = model.GetVariantSets()
            if name not in sets.GetNames() or choice not in sets.GetVariantSet(name).GetVariantNames():
                raise ValueError('robot model variant is absent: '+name+'='+choice)
            sets.GetVariantSet(name).SetVariantSelection(choice)
        if suffix == '.urdf':
            sets = model.GetVariantSets()
            if 'Physics' in sets.GetNames():
                variants = sets.GetVariantSet('Physics')
                choices = [name for name in variants.GetVariantNames() if name.casefold() == 'physx']
                if len(choices) != 1:raise ValueError('official URDF import lacks a unique PhysX backend variant')
                variants.SetVariantSelection(choices[0])
            _restore_fixed_frames(stage, model, source)
        if UsdGeom.Xformable(model).GetResetXformStack():
            raise ValueError('selected model root resetXformStack would bypass its installation mount')
        for declaration in [*ports.values(), *streams.values()]:
            if not stage.GetPrimAtPath(declaration['source']['path']):raise ValueError('profile source prim is absent from the model')
        _joint_configuration(stage, root, profile)
        frames = _planning_frames(stage, root, profile, resources, streams, id, world.io['clock'])
        if set(streams)&set(world.io['streams']):raise ValueError('robot frame-stream namespace already exists')
        world.bind(id, root, owner=owner, frame=frame if isinstance(frame, str) else None, purpose=purpose)
        world.io['ports'].update(ports);world.io['streams'].update(streams)
        return {'id':id, 'primPath':root, 'mountPath':mount,
            'ports':{name:id+'.'+name for name in profile['ports']},
            'streams':{name:id+'.'+name for name in profile['streams']}, 'frames':frames}
    except Exception:
        stage.RemovePrim(mount)
        if id not in previous_nodes:world.nodes.pop(id, None)
        for kind, owned, previous in (('ports', ports, previous_ports), ('streams', streams, previous_streams)):
            for name in owned:
                if name not in previous:world.io[kind].pop(name, None)
        if imported is not None:
            import shutil
            shutil.rmtree(imported, ignore_errors=True)
        raise


def native_clock(clock):
    if (not isinstance(clock, dict) or set(clock) != {'physicsRateHz', 'controlRateHz'}
            or any(type(value) is not int or value <= 0 for value in clock.values())
            or clock['physicsRateHz'] % clock['controlRateHz']):
        raise ValueError('native clock needs positive integer physics/control rates with an exact divisor')
    physics, control = clock['physicsRateHz'], clock['controlRateHz']
    return physics, control, physics//control


def native_control_cycles(seconds, clock, elapsed, limit):
    _, rate, _ = native_clock(clock)
    if isinstance(seconds, bool) or not isinstance(seconds, Real) or not math.isfinite(seconds):
        raise ValueError('control duration must be a finite real number')
    cycles = seconds*rate
    if round(cycles) < 1 or abs(cycles-round(cycles)) > 1e-8:
        raise ValueError('control duration must contain complete frozen control periods')
    if elapsed+round(cycles)/rate > limit+1e-9:
        raise ValueError('request exceeds the remaining physical episode budget')
    return round(cycles)


def native_values(value, shape, label):
    import numpy as np
    raw = np.asarray(value)
    if (raw.shape != tuple(shape) or raw.dtype.kind not in 'fiu'
            or any(isinstance(item, (bool, np.bool_)) or not isinstance(item, Real)
                   for item in np.asarray(value, dtype=object).flat)
            or not np.isfinite(raw).all()):
        raise ValueError('native '+label+' requires finite real numbers with exact shape '+str(shape))
    return raw


def validate_native_ports(ports):
    actuators = set()
    for name, port in ports.items():
        source = port['source']
        for joint in source['joints']:
            identity = (source['path'], joint)
            if identity in actuators:
                raise ValueError('native control ports overlap the same physical actuator: '+name)
            actuators.add(identity)


def native_control_action(commands, io, active_targets):
    """Validate the entire named request before any actuator receives a write."""
    from runtime.records import validate_action
    validate_action(commands, io)
    result = {}
    for name, port in io['ports'].items():
        if name not in commands:
            if port['persistence'] != 'persistent':
                raise ValueError('native nonpersistent port requires an explicit value each step: '+name)
            if active_targets.get(name) is None:
                raise ValueError('omission requires an initialized native persistent target: '+name)
            native_values(active_targets[name], port['shape'], name+' active target')
            continue
        value = native_values(commands[name], port['shape'], name)
        if any(lo is not None and item < lo or hi is not None and item > hi
               for item, (lo, hi) in zip(value, port['limits'])):
            raise ValueError('native command exceeds the frozen actuator limits: '+name)
        result[name] = value.tolist()
    return result


def control_entrypoints(kind='expert', planned=True):
    return [] if kind == 'isolation' else list(CONTROL_APIS)


def robot_capabilities(io, current_runtime):
    """Describe the already verified scene I/O, without selecting any robot model."""
    from copy import deepcopy
    verified = isinstance(io, dict) and io.get('schema') == NATIVE_IO_SCHEMA
    return {'schema': 'sceneagent-native-capabilities-v2', 'runtime': current_runtime,
            'engine': 'isaacsim', 'physics': 'physx',
            'io': deepcopy(io) if verified else None,
            'availableApis': control_entrypoints() if verified else [],
            'apiScope': dict(CONTROL_APIS),
            'numericalHelpers': ['runtime.motion.create_planner', 'runtime.motion.plan_trajectory'],
            'scope': 'frozen SDK-derived named ports and read-only streams; no model, policy or task-success qualification',
            'qualification': 'frozen_native_io' if verified else 'requires_scene_build'}
