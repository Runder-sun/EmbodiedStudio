"""Numerical planning and generic execution through the named native step contract."""
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib
import json
import math
import os
from numbers import Real
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

SCHEMA = 'sceneagent-curobo-numerical-v2'
_loaded = {}

def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def value_digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def numeric(value,shape,label):
    raw=np.asarray(value)
    if any(isinstance(item,(bool,np.bool_)) or not isinstance(item,Real) for item in np.asarray(value,dtype=object).flat):
        raise ValueError('motion requires real numeric '+label+'; bool/string coercion is forbidden')
    if raw.shape!=shape or raw.dtype.kind not in 'fiu' or raw.dtype.kind=='b' or not np.isfinite(raw).all():
        raise ValueError('motion requires finite numeric '+label+' with shape '+str(shape))
    return raw.astype(float)


def rigid(value):
    matrix=numeric(value,(4,4),'rigid matrix')
    if (not np.allclose(matrix[3],[0,0,0,1],atol=1e-9,rtol=0)
            or not np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3),atol=1e-6,rtol=0)
            or abs(np.linalg.det(matrix[:3,:3])-1)>1e-6):raise ValueError('motion requires a proper rigid pose')
    return matrix


def validate_target(target,orientation):
    position=numeric(target,(3,),'world target')
    rotation=None if orientation is None else numeric(orientation,(3,3),'world rotation')
    if rotation is not None and (not np.allclose(rotation.T@rotation,np.eye(3),atol=1e-6,rtol=0)
            or abs(np.linalg.det(rotation)-1)>1e-6):raise ValueError('motion orientation must be a proper rotation')
    return position,rotation


def frame_inputs(base_world,goal_world):
    base,goal=rigid(base_world),rigid(goal_world)
    return base.copy(),np.linalg.inv(base)@goal


def pose_error(actual,expected):
    actual,expected=rigid(actual),rigid(expected)
    cosine=(np.trace(actual[:3,:3].T@expected[:3,:3])-1)/2
    return float(np.linalg.norm(actual[:3,3]-expected[:3,3])),math.acos(float(np.clip(cosine,-1,1)))


def _motion_file_stat(path):
    value=Path(path).stat()
    return {'bytes':value.st_size,'device':value.st_dev,'inode':value.st_ino,
        'mtimeNs':value.st_mtime_ns,'ctimeNs':value.st_ctime_ns}


@lru_cache(maxsize=1)
def motion_runtime_identity():
    """Read the accepted selection and file metadata only; never import a CUDA/SDK module."""
    from importlib.metadata import version,PackageNotFoundError
    import sysconfig
    selected={'schema':SCHEMA,'available':False,'loaded':False,'modules':{},'binaries':[],
        'files':{},'resources':{},'missing':[],
        'scope':'accepted dependency selection and current metadata only; no loaded/runtime/task success claim'}
    try:
        lock=json.loads(Path(__file__).with_name('motion-dependencies.json').read_text())
        verification=lock['verification'];raw=verification['actualLoadedIdentity']
        if (lock['backend']['name']!='cuRobo' or lock['backend']['commit']!='ebb71702f3f70e767f40fd8e050674af0288abe8'
                or verification['assessment']['decision']!='PASS_WITH_EXPLICIT_DEVICE_SCOPE'):
            raise ValueError('motion backend lacks its accepted pinned dependency assessment')
        selected.update(backend=deepcopy(lock['backend']),prefix=raw['prefix'],executable=raw['executable'],
            python=raw['python'],SOABI=raw['SOABI'],distributions=deepcopy(raw['distributions']),
            torchABI=deepcopy(raw['torchABI']),torchCUDA=raw['torchCUDA'])
        if (sys.prefix!=raw['prefix'] or sys.executable!=raw['executable'] or sys.version!=raw['python']
                or sysconfig.get_config_var('SOABI')!=raw['SOABI']):
            raise ValueError('motion interpreter/prefix/ABI differs from the accepted selection')
        for name,expected in raw['distributions'].items():
            try:actual=version(name)
            except PackageNotFoundError:actual=None
            if actual!=expected:raise ValueError('motion distribution selection differs: '+name)
        if raw['distributions'].get('warp-lang') is not None:
            raise ValueError('motion requires the SDK Warp family, without a pip Warp distribution')
        selected['modules']={name:row['path'] for name,row in raw['modules'].items()}
        selected['binaries']=sorted(raw['mappedNativeLibraries'])
        required={'curobo','warp','torch',*['curobo.curobolib.'+name+'_cu' for name in
            ('geom','kinematics_fused','lbfgs_step','line_search','tensor_step')]}
        if not required<=set(selected['modules']):raise ValueError('motion dependency lock omits required modules')
        required_native={selected['modules'][name] for name in required if name.startswith('curobo.curobolib.')}
        if not required_native<=set(selected['binaries']):
            raise ValueError('motion dependency lock omits a required native module mapping')
        expected={}
        for path,row in [*[(row['path'],row) for row in raw['modules'].values()],
                         *raw['mappedNativeLibraries'].items(),*lock['sourceFiles'].items()]:
            fields={key:row[key] for key in ('bytes','sha256')}
            if path in expected and expected[path]!=fields:raise ValueError('motion dependency file identity conflicts')
            expected[path]=fields
        prefix=Path(raw['prefix']).resolve()
        for name,row in expected.items():
            path=Path(name)
            if (not path.is_absolute() or path.is_symlink() or path.resolve()!=path
                    or not path.is_relative_to(prefix) or not path.is_file()
                    or path.stat().st_size!=row['bytes']):
                raise ValueError('motion selected file missing or metadata changed: '+name)
        selected['files']=expected
        selected['available']=True
    except (OSError,ValueError,TypeError,KeyError) as error:
        selected['missing'].append(type(error).__name__+': '+str(error))
    return selected


def _selected_numerical_warp():
    identity = motion_runtime_identity()
    if not identity.get('available'):
        raise ValueError('numerical dependency selection is unavailable: '+str(identity.get('missing')))
    source = Path(identity['modules']['warp'])
    if source.name != '__init__.py' or source.parent.name != 'warp' or digest(source) != identity['files'][str(source)]['sha256']:
        raise ValueError('selected numerical Warp source differs from the accepted dependency lock')
    parent = str(source.parent.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    module = importlib.import_module('warp')
    if Path(module.__file__).resolve() != source:
        raise ValueError('loaded numerical Warp source differs from its selected family')
    return module


def _sdk():
    """Import the one locked numerical backend without starting a native SDK app."""
    os.environ['CUROBO_TORCH_CUDA_GRAPH_RESET']='1'
    import torch
    torch.cuda.set_device(0)
    warp = _selected_numerical_warp()
    import curobo
    # NVIDIA AutoData Apache-2.0 compatibility bridge, fixed source 953cd37778:
    # autodata_interfaces/motion_planners/curobo/curobo_planner.py:35-38.
    if not hasattr(warp,'torch'):
        warp.torch=SimpleNamespace(device_from_torch=warp.device_from_torch)
    for name in ('lbfgs_step_cu','kinematics_fused_cu','line_search_cu','tensor_step_cu','geom_cu'):
        importlib.import_module('curobo.curobolib.'+name)
    from curobo.wrap.reacher.motion_gen import MotionGen,MotionGenConfig,MotionGenPlanConfig
    from curobo.types.base import TensorDeviceType
    from curobo.types.math import Pose
    from curobo.types.state import JointState
    from curobo.geom.types import WorldConfig
    from curobo.rollout.cost.pose_cost import PoseCostMetric
    from curobo.wrap.reacher.ik_solver import IKSolver,IKSolverConfig
    return SimpleNamespace(torch=torch,wp=warp,curobo=curobo,MotionGen=MotionGen,Config=MotionGenConfig,
        PlanConfig=MotionGenPlanConfig,TensorArgs=TensorDeviceType,Pose=Pose,JointState=JointState,
        WorldConfig=WorldConfig,Metric=PoseCostMetric,IKSolver=IKSolver,IKConfig=IKSolverConfig)


def loaded_motion_identity(identity,curobo,wp,torch):
    """After numerical warmup, verify actual families/maps and hash the locked content once."""
    if not identity.get('available') or (sys.prefix,sys.executable)!=(identity['prefix'],identity['executable']):
        raise ValueError('motion loaded interpreter or dependency selection differs')
    roots={'curobo':curobo,'warp':wp,'torch':torch}
    if any(sys.modules.get(name) is not module for name,module in roots.items()):
        raise ValueError('motion actual loaded root modules differ from their selection')
    if (getattr(curobo,'__version__',None)!=identity['distributions']['nvidia-curobo']
            or getattr(wp,'__version__',None)!='1.13.0'
            or getattr(torch,'__version__',None)!=identity['distributions']['torch']
            or getattr(getattr(torch,'version',None),'cuda',None)!=identity['torchCUDA']
            or {name:getattr(getattr(torch,'_C',None),name,None) for name in identity['torchABI']}!=identity['torchABI']):
        raise ValueError('motion loaded library version or Torch ABI differs from the selection')
    required={'curobo','warp','torch',*['curobo.curobolib.'+name+'_cu' for name in
        ('geom','kinematics_fused','lbfgs_step','line_search','tensor_step')]}
    if not required<=set(sys.modules) or any(sys.modules[name] is None for name in required):
        raise ValueError('motion actual loaded modules omit a required root or native extension')
    family_roots={name:Path(identity['modules'][name]).parent for name in ('curobo','warp')}
    paths={}
    for name,module in list(sys.modules.items()):
        family=name.partition('.')[0]
        if family not in family_roots and name not in identity['modules']:continue
        if module is None or not getattr(module,'__file__',None):
            raise ValueError('motion loaded module lacks a selected source file: '+name)
        path=Path(module.__file__).resolve();expected=identity['modules'].get(name)
        if expected is not None and str(path)!=expected:
            raise ValueError('motion loaded module family differs from the selected file: '+name)
        if family in family_roots:
            base=family_roots[family];parts=name.split('.')[1:]
            package=base.joinpath(*parts)/'__init__.py'
            parent=base.joinpath(*parts[:-1]) if parts else base
            if not path.is_relative_to(base) or not (path==package or parts and path.parent==parent and path.name.partition('.')[0]==parts[-1]):
                raise ValueError('motion loaded module escapes its selected family: '+name)
        if str(path) not in identity['files']:
            raise ValueError('motion loaded source is absent from the locked closure: '+name)
        paths[name]=str(path)
    if sys.platform!='linux':raise ValueError('motion actual native library mapping requires Linux')
    mapped=set()
    for line in Path('/proc/self/maps').read_text().splitlines():
        fields=line.split(None,5)
        if len(fields)==6 and fields[5].startswith('/'):
            mapped.add(fields[5])
    binaries=set(identity['binaries']);names={Path(path).name for path in binaries}
    related={path for path in mapped if Path(path).name in names or '/curobo/curobolib/' in path
        or '/warp/bin/' in path or '/torch/lib/' in path}
    if related!=binaries:
        raise ValueError('motion actually mapped native library family differs or is incomplete: '+str(sorted(related^binaries)))
    key=value_digest(identity)
    current={path:_motion_file_stat(path) for path in identity['files']}
    previous=_loaded.get(key,{}).get('stats',{})
    for path,row in identity['files'].items():
        if current[path]['bytes']!=row['bytes']:
            raise ValueError('motion selected source/native content bytes differ: '+path)
        if previous.get(path)!=current[path]:
            if digest(path)!=row['sha256'] or _motion_file_stat(path)!=current[path]:
                raise ValueError('motion loaded source/native content bytes differ or changed during verification: '+path)
    proof={'identitySha256':key,'mappedBinaries':sorted(binaries),'contentHashesComputed':True,
        'checkedScope':'selected cuRobo/SDK Warp source closure and actual native families; no task/model/collision coverage proof'}
    _loaded[key]={'stats':current,'proof':proof}
    return {**deepcopy(proof),'modules':paths}


def _profile_config(path, resources, source_root):
    from runtime.robot import load_profile, resolve_resource, validate_planning_config
    import yaml
    if not isinstance(resources, dict):raise ValueError('profile planning requires explicit frozen resources')
    profile = load_profile(path, source_root=source_root)
    planning = profile.get('planning')
    if not isinstance(planning, dict):
        raise ValueError('robot profile has no numerical planning configuration')
    resolve_resource(profile['model'], resources)
    filename, bundle = resolve_resource(planning, resources)
    inline = 'config' in planning
    config_base = bundle if inline else filename.parent

    def local_path(value, base, *, directory=False):
        if not isinstance(value, str) or not value or '://' in value or value.startswith('anon:'):
            raise ValueError('planning dependency requires a local frozen bundle path')
        candidate = base/value
        if any(parent.is_symlink() for parent in [candidate, *candidate.parents]):
            raise ValueError('planning dependency cannot use a symlink')
        resolved = candidate.resolve()
        if not resolved.is_relative_to(bundle):
            raise ValueError('planning dependency escapes its frozen bundle')
        if directory:
            if not resolved.is_dir():raise ValueError('planning asset root is missing')
            return resolved
        return resolve_resource({'resourceRef': planning['resourceRef'],
            'file': str(resolved.relative_to(bundle))}, resources)[0]

    def read_yaml(file):
        try:
            value = yaml.safe_load(file.read_text())
            json.dumps(value, allow_nan=False)
        except (yaml.YAMLError, TypeError, ValueError) as error:
            raise ValueError('planning YAML must contain safe finite plain data') from error
        if not isinstance(value, dict):raise ValueError('planning YAML requires a mapping')
        return value

    document = planning['config'] if inline else read_yaml(filename)
    config = deepcopy(document if inline else document.get('robot_cfg', document))
    if not isinstance(config, dict) or not isinstance(config.get('kinematics'), dict):
        raise ValueError('planning data requires the native kinematics mapping')
    kinematics = config['kinematics']
    if (kinematics.get('use_external_assets', False) is not False
            or any(kinematics.get(key) is not None for key in ('external_asset_path','external_robot_configs_path'))):
        raise ValueError('robot profile cannot select external SDK asset roots')
    asset_root = local_path(planning['assetRoot'], bundle, directory=True) if 'assetRoot' in planning else config_base
    usd = kinematics.get('use_usd_kinematics', False)
    if type(usd) is not bool:raise ValueError('native use_usd_kinematics must be boolean')
    active, inactive = ('usd_path','urdf_path') if usd else ('urdf_path','usd_path')
    model = local_path(kinematics.get(active), asset_root)
    not_activated = {key: {'value': deepcopy(kinematics[key]), 'status': 'notActivated'}
        for key in (inactive, 'isaac_usd_path') if kinematics.get(key) is not None}
    kinematics[active], kinematics[inactive], kinematics['isaac_usd_path'] = str(model), None, None
    meshes = local_path(kinematics['asset_root_path'], asset_root, directory=True) if kinematics.get('asset_root_path') else model.parent
    kinematics['asset_root_path'] = str(meshes)
    if usd:
        from runtime.worker import audit_native_stage
        audit = audit_native_stage(bundle, str(model.relative_to(bundle)))
        for name in audit['files']:
            local_path(name, bundle)
    else:
        import xml.etree.ElementTree as ET
        try:
            document = ET.parse(model)
        except ET.ParseError as error:
            raise ValueError('native planning URDF cannot be parsed') from error
        for element in document.iter():
            if element.tag in ('mesh','texture') and element.get('filename'):
                local_path(element.get('filename'), meshes)
    spheres = kinematics.get('collision_spheres')
    if isinstance(spheres, str):
        sphere_file = local_path(spheres, config_base)
        sphere_data = read_yaml(sphere_file)
        if not isinstance(sphere_data.get('collision_spheres'), dict):
            raise ValueError('collision sphere YAML requires its native collision_spheres mapping')
        kinematics['collision_spheres'] = sphere_data['collision_spheres']
    validate_planning_config(config)
    source = Path(path) if Path(path).is_absolute() else Path(source_root)/path
    provenance = {'sourceDigest': digest(source), 'resourceRef': planning['resourceRef'],
        'configSource': 'inline' if inline else 'file',
        'configFile': None if inline else str(filename.relative_to(bundle)), 'notActivated': not_activated}
    return profile, config, provenance


def _profile_ports(profile, io, port_map):
    from runtime.records import validate_io
    if not isinstance(io, dict) or not isinstance(port_map, dict):
        raise ValueError('profile planning requires verified native io and explicit port_map')
    validate_io(io)
    selected = profile['planning']['ports']
    if set(port_map)-set(profile['ports']) or any(name not in port_map for name in selected):
        raise ValueError('profile port_map must identify every selected declared port')
    ports, targets = {}, set()
    for local in selected:
        name = port_map[local]
        if not isinstance(name, str) or name not in io['ports'] or name in ports:
            raise ValueError('profile planning port is absent or ambiguous')
        port = io['ports'][name]
        source = port['source']
        declared = profile['ports'][local]['source']
        if (source.get('primitive') != 'joint_position' or declared['primitive'] != 'joint_position'
                or source.get('joints') != declared['joints'] or port['shape'] != [len(declared['joints'])]):
            raise ValueError('profile numerical positions require the exact declared native position joints')
        physical = port.get('physicalJointLimits')
        if (not isinstance(physical, list) or len(physical) != len(source['joints'])
                or any(not isinstance(pair, list) or len(pair) != 2
                    or any(value is not None and (isinstance(value, bool) or not isinstance(value, Real)
                        or not math.isfinite(value)) for value in pair)
                    or pair[0] is not None and pair[1] is not None and pair[0] > pair[1] for pair in physical)):
            raise ValueError('profile planning needs the actual physicalJointLimits')
        for joint in source['joints']:
            identity = (source.get('path'), joint)
            if identity in targets:raise ValueError('profile planning ports overlap one actual actuator')
            targets.add(identity)
        ports[name] = (local, port)
    return ports


def _profile_binding(profile, ports, names, provenance, clock):
    if not names or len(names) != len(set(names)):
        raise ValueError('profile planner returned an ambiguous joint order')
    candidates = {}
    for name, (local, port) in ports.items():
        for joint in port['source']['joints']:
            candidates.setdefault(joint, []).append((local, joint))
    joint_map = profile['planning'].get('jointMap')
    if joint_map is None:
        if any(len(candidates.get(joint, [])) != 1 for joint in names):
            raise ValueError('planner joint names require an unambiguous complete native mapping')
        mapping = {joint: candidates[joint][0] for joint in names}
    else:
        if set(joint_map) != set(names):raise ValueError('jointMap must cover the actual planner joint order')
        mapping = {joint: (item['port'], item['joint']) for joint, item in joint_map.items()}
    actual = {(local, joint) for local, port in ports.values() for joint in port['source']['joints']}
    if len(set(mapping.values())) != len(names) or set(mapping.values()) != actual:
        raise ValueError('planner joint mapping must be a complete actual actuator bijection')
    reverse = {target: names.index(joint) for joint, target in mapping.items()}
    indices, limits = {}, [None]*len(names)
    for name, (local, port) in ports.items():
        indices[name] = [reverse[(local, joint)] for joint in port['source']['joints']]
        for index, pair in zip(indices[name], port['physicalJointLimits']):limits[index] = deepcopy(pair)
    return {'portJointIndices': indices, 'physicalJointLimits': limits, 'provenance': provenance,
        'executionBinding': {'clock': deepcopy(clock), 'ports': {name: deepcopy(port) for name, (_, port) in ports.items()}}}


def _profile_bounds(planner, values):
    binding = getattr(planner, '_sceneagent_profile', None)
    if binding is None:return
    for index, (low, high) in enumerate(binding['physicalJointLimits']):
        column = np.asarray(values)[..., index]
        if low is not None and np.any(column < low-1e-6) or high is not None and np.any(column > high+1e-6):
            raise ValueError('profile numerical path exceeds actual physical joint limits')


def create_planner(robot_config, world_config, *, interpolation_dt, resources=None, io=None,
                   port_map=None, source_root=None, **options):
    """Create the installed solver from frozen program resources; no live SDK handles."""
    if (isinstance(interpolation_dt, bool) or not isinstance(interpolation_dt, Real)
            or not math.isfinite(interpolation_dt) or interpolation_dt <= 0):
        raise ValueError('numerical planner needs an explicit positive interpolation period')
    profile = None
    if isinstance(robot_config, (str, Path)):
        profile, robot_config, provenance = _profile_config(robot_config, resources, source_root)
        ports = _profile_ports(profile, io, port_map)
    if not isinstance(robot_config, dict) or not isinstance(world_config, dict):
        raise ValueError('numerical planner requires explicit native model/world configurations')
    sdk = _sdk()
    tensor = sdk.TensorArgs(device=sdk.torch.device('cuda:0'))
    config = sdk.Config.load_from_robot_config(deepcopy(robot_config),
        sdk.WorldConfig.from_dict(deepcopy(world_config)) if world_config else None, tensor_args=tensor,
        interpolation_dt=float(interpolation_dt), **options)
    planner = sdk.MotionGen(config)
    if profile is not None:
        planner._sceneagent_profile = _profile_binding(profile, ports, list(planner.joint_names), provenance, io['clock'])
    return planner


def plan_trajectory(planner, start, targets, *, plan_config=None):
    """One simultaneous model-space problem. This returns a plan, never a demonstration."""
    sdk = _sdk()
    names = list(planner.joint_names)
    if (not isinstance(start, dict) or not {'jointNames', 'position', 'velocity'} <= set(start)
            or set(start)-{'jointNames', 'position', 'velocity', 'acceleration', 'jerk'}
            or start['jointNames'] != names or not names or len(names) != len(set(names))):
        raise ValueError('numerical start must identify the exact actual planner joint order')
    position = numeric(start['position'], (len(names),), 'start position')
    _profile_bounds(planner, position)
    velocity = numeric(start['velocity'], (len(names),), 'start velocity')
    derivatives = {key: numeric(start[key], (len(names),), 'start '+key) if key in start else np.zeros(len(names))
                   for key in ('acceleration', 'jerk')}
    primary = planner.kinematics.ee_link
    if not isinstance(targets, dict) or primary not in targets:
        raise ValueError('numerical targets must include the actual model end-effector link')
    poses = {}
    for link, raw in targets.items():
        if not isinstance(link, str) or not link or link not in planner.kinematics.link_names:
            raise ValueError('numerical target requires an actual registered model link')
        pose = numeric(raw, (7,), 'xyz/wxyz target')
        if abs(np.linalg.norm(pose[3:])-1) > 1e-5:
            raise ValueError('numerical target needs a unit wxyz quaternion')
        poses[link] = sdk.Pose.from_list(pose.tolist(), planner.tensor_args)
    state = sdk.JointState(position=planner.tensor_args.to_device(position.reshape(1, -1)),
        velocity=planner.tensor_args.to_device(velocity.reshape(1, -1)), joint_names=names,
        acceleration=planner.tensor_args.to_device(derivatives['acceleration'].reshape(1,-1)),
        jerk=planner.tensor_args.to_device(derivatives['jerk'].reshape(1,-1)))
    configuration = sdk.PlanConfig(**deepcopy(plan_config or {})) if plan_config is None or isinstance(plan_config, dict) else plan_config
    result = planner.plan_single(state, poses[primary], configuration, link_poses=poses)
    sdk.torch.cuda.synchronize()
    if not bool(result.success.all()):
        raise RuntimeError('numerical planning failed: '+str(result.status)+'; no physical commands issued')
    raw = result.get_interpolated_plan()
    if raw is None or list(raw.joint_names) != names:
        raise ValueError('numerical result omitted or changed its actual joint order')
    positions = raw.position.detach().cpu().numpy()
    positions = numeric(positions, (len(positions), len(names)), 'planned positions')
    _profile_bounds(planner, positions)
    velocities = numeric(raw.velocity.detach().cpu().numpy(), positions.shape, 'planned velocities')
    period = float(result.interpolation_dt)
    if len(positions) < 2 or not math.isfinite(period) or period <= 0:
        raise ValueError('numerical path lacks a valid complete timebase')
    return {'kind': 'numerical_plan', 'actualExecution': False, 'jointOrder': names, 'start': deepcopy(start),
        'timebase': {'units': 'seconds', 'period': period, 'times': (np.arange(len(positions))*period).tolist()},
        'position': positions.tolist(), 'velocity': velocities.tolist(),
        'targets': deepcopy(targets), 'targetFrame': 'native_model_base',
        **({'portJointIndices': deepcopy(planner._sceneagent_profile['portJointIndices']),
            'executionBinding': deepcopy(planner._sceneagent_profile['executionBinding']),
            'profileProvenance': deepcopy(planner._sceneagent_profile['provenance'])}
           if getattr(planner, '_sceneagent_profile', None) is not None else {}),
        'unprovidedDerivativeAssumptions': {key:0. for key in derivatives if key not in start},
        'scope': 'provided numerical model/world snapshot; no contact task, dynamic balance or actual-execution claim'}


def execute_trajectory(env, plan, *, feedback, start_tolerance, tracking_tolerance, hold_action=None):
    """Dispatch every timed position through env.step; no task policy, retiming or recovery."""
    from runtime.records import validate_io, validate_action, read_array, _clock
    if (not isinstance(plan, dict) or plan.get('kind') != 'numerical_plan'
            or plan.get('actualExecution') is not False or not isinstance(plan.get('executionBinding'), dict)):
        raise ValueError('trajectory execution requires a bound numerical plan')
    names = plan.get('jointOrder')
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError('trajectory execution requires unique model joint names')
    size = len(names)
    positions = np.asarray(plan.get('position'), dtype=object)
    if positions.ndim != 2 or len(positions) < 2:
        raise ValueError('trajectory requires at least an initial point and one complete interval')
    positions = numeric(plan['position'], (len(positions), size), 'execution positions')
    velocities = numeric(plan.get('velocity'), positions.shape, 'execution velocities')
    start = plan.get('start')
    if not isinstance(start, dict) or start.get('jointNames') != names:
        raise ValueError('trajectory execution requires its original named start')
    start_q = numeric(start.get('position'), (size,), 'bound start position')
    start_dq = numeric(start.get('velocity'), (size,), 'bound start velocity')
    if not isinstance(start_tolerance, dict) or set(start_tolerance) != {'position','velocity'}:
        raise ValueError('start_tolerance requires position and velocity in model joint order')
    q_tolerance = numeric(start_tolerance['position'], (size,), 'start position tolerance')
    dq_tolerance = numeric(start_tolerance['velocity'], (size,), 'start velocity tolerance')
    tracking = numeric(tracking_tolerance, (size,), 'tracking tolerance')
    if any(np.any(value < 0) for value in (q_tolerance, dq_tolerance, tracking)):
        raise ValueError('trajectory tolerances must be nonnegative')
    if np.any(np.abs(positions[0]-start_q) > q_tolerance) or np.any(np.abs(velocities[0]-start_dq) > dq_tolerance):
        raise ValueError('trajectory zero-time point differs from its original start')
    opened = env.open()
    io = validate_io(opened['io'])
    binding, indices = plan['executionBinding'], plan.get('portJointIndices')
    if (binding.get('clock') != io['clock'] or not isinstance(binding.get('ports'), dict)
            or not isinstance(indices, dict) or not indices or set(indices) != set(binding['ports'])
            or any(name not in io['ports'] or binding['ports'][name] != io['ports'][name] for name in indices)):
        raise ValueError('trajectory selected native ports or clock changed')
    mapped, actuators = [], set()
    for name, order in indices.items():
        port = io['ports'][name];source = port['source']
        if (source.get('primitive') != 'joint_position' or not isinstance(order, list)
                or port['shape'] != [len(order)] or len(source.get('joints', [])) != len(order)
                or any(type(index) is not int or not 0 <= index < size for index in order)):
            raise ValueError('trajectory requires complete named position-port mapping')
        for joint in source['joints']:
            identity = (source['path'], joint)
            if identity in actuators:raise ValueError('trajectory maps one actual actuator twice')
            actuators.add(identity)
        mapped.extend(order)
        limits = port.get('physicalJointLimits')
        if not isinstance(limits, list) or len(limits) != len(order):raise ValueError('trajectory lacks physical joint limits')
        for index, (low, high) in zip(order, limits):
            if low is not None and np.any(positions[:,index] < low-1e-6) or high is not None and np.any(positions[:,index] > high+1e-6):
                raise ValueError('trajectory exceeds actual physical joint limits')
    if sorted(mapped) != list(range(size)):
        raise ValueError('trajectory must map each planned joint to one actual actuator')
    period = 1/io['clock']['controlRateHz']
    timebase = plan.get('timebase')
    if not isinstance(timebase, dict) or timebase.get('units') != 'seconds':raise ValueError('trajectory needs a seconds timebase')
    supplied_period = numeric([timebase.get('period')], (1,), 'execution period')[0]
    times = numeric(timebase.get('times'), (len(positions),), 'execution times')
    if times[0] != 0 or abs(supplied_period-period) > 1e-9 or not np.allclose(times, np.arange(len(positions))*period, atol=1e-9, rtol=0):
        raise ValueError('trajectory must contain every frozen control time starting at zero')
    holds = {} if hold_action is None else deepcopy(hold_action)
    if not isinstance(holds, dict) or set(holds)&set(indices):raise ValueError('hold_action cannot override planned ports')
    validate_action(holds, io)
    if any(name not in indices and name not in holds and (port['persistence'] != 'persistent' or port.get('initialized') is not True)
           for name, port in io['ports'].items()):
        raise ValueError('unplanned ports require initialized persistent targets or explicit hold_action')
    for row in positions:
        validate_action({**holds, **{name: row[order].tolist() for name, order in indices.items()}}, io)
    if not isinstance(feedback, dict) or set(feedback) != set(indices):raise ValueError('feedback must cover each planned port')
    readers = {}
    for name, fields in feedback.items():
        if not isinstance(fields, dict) or set(fields) != {'position','velocity'}:raise ValueError('feedback requires q and dq streams')
        source = io['ports'][name]['source']
        for field, reader in (('position','joint_positions'), ('velocity','joint_velocities')):
            stream = fields[field]
            if not isinstance(stream, str):raise ValueError('feedback requires named native streams')
            spec = io['streams'].get(stream)
            native = spec.get('source', {}) if isinstance(spec, dict) else {}
            joints = native.get('joints', [])
            if (not isinstance(stream, str) or spec is None or not isinstance(joints, list) or len(set(joints)) != len(joints)
                    or native.get('reader') != reader or native.get('path') != source['path']
                    or any(joint not in joints for joint in source['joints']) or spec['shape'] != [len(joints)]
                    or spec['origin'] != 'native_output' or spec['visibleToPolicy'] is not True
                    or spec['rateHz'] % io['clock']['controlRateHz']):
                raise ValueError('feedback must expose fresh native q/dq of the same actual joints')
            readers[name,field] = (stream, [joints.index(joint) for joint in source['joints']])
    root = opened.get('sensorSpec', {}).get('arrayRoot')
    initial = opened['observation']
    def check_clock(observation, waypoint):
        _clock(observation)
        if (observation['stageId'] != initial['stageId'] or observation['timeOrigin'] != initial['timeOrigin']
                or observation['physicsStep'] != initial['physicsStep']+waypoint*io['clock']['physicsRateHz']//io['clock']['controlRateHz']
                or abs(observation['time']-initial['time']-waypoint*period) > 1e-9
                or abs(observation['time']-observation['physicsStep']/io['clock']['physicsRateHz']) > 1e-9):
            raise ValueError('trajectory feedback stage or physical clock changed')
    def read_feedback(observation):
        values = {};q,dq = np.empty(size),np.empty(size)
        for (name,field), (stream,selection) in readers.items():
            if stream not in values:
                spec = io['streams'][stream];status = observation.get('streamStatus', {}).get(stream, {})
                if (status.get('status') != 'available' or type(status.get('physicsStep')) is not int
                        or status['physicsStep'] != observation['physicsStep']
                        or type(status.get('sampleTime')) not in (int,float)
                        or not math.isfinite(status['sampleTime'])
                        or abs(status['sampleTime']-observation['time']) > 1e-9):
                    raise ValueError('trajectory feedback is unavailable or stale: '+stream)
                value = observation.get('streams', {}).get(stream)
                if isinstance(value, dict):
                    if root is None or value.get('dtype') != spec['dtype']:raise ValueError('trajectory feedback array lacks its authorized scope or dtype')
                    value = read_array(root, value)
                values[stream] = numeric(value, tuple(spec['shape']), 'feedback '+stream)
            (q if field == 'position' else dq)[indices[name]] = values[stream][selection]
        return q,dq
    def terminal(reply):
        if any(type(reply.get(flag)) is not bool for flag in ('terminated','truncated')):
            raise ValueError('trajectory reply lacks native termination flags')
        return 'terminated' if reply['terminated'] else 'truncated' if reply['truncated'] else None
    check_clock(initial, 0)
    q,dq = read_feedback(initial)
    if np.any(np.abs(q-start_q) > q_tolerance) or np.any(np.abs(dq-start_dq) > dq_tolerance):
        raise ValueError('trajectory start is stale relative to actual q/dq')
    last, returned, confirmed, waypoint, known = deepcopy(initial),0,0,0,True
    def result(status):
        return {'kind':'trajectory_execution','status':status,'requestedCycles':len(positions)-1,
            'returnedStepCalls':returned,'confirmedPhysicalSteps':confirmed,
            'totalPhysicalSteps':confirmed if known else None,'countStatus':'known' if known else 'unknown',
            'lastConfirmedWaypoint':waypoint,'lastObservation':deepcopy(last),
            'scope':'confirmed native replies only; no task-success, grasp or collision-safety claim'}
    ended = terminal(opened)
    if ended:return result(ended)
    try:
        for index in range(1,len(positions)):
            known = False
            reply = env.step({**holds, **{name: positions[index,order].tolist() for name,order in indices.items()}}, period)
            returned += 1
            check_clock(reply,index)
            last = deepcopy(reply)
            confirmed = reply['physicsStep']-initial['physicsStep'];waypoint = index;known = True
            q,dq = read_feedback(reply)
            if np.any(np.abs(q-positions[index]) > tracking):raise ValueError('trajectory position tracking tolerance exceeded')
            ended = terminal(reply)
            if ended:return result(ended)
    except Exception as cause:
        error = RuntimeError('trajectory execution stopped: '+str(cause));error.execution = result('failed')
        raise error from cause
    return result('completed')
