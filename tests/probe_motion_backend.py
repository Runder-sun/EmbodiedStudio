"""SYSTEM_VERIFICATION_ONLY: real-library/SDK compatibility; never a generated product.

Run each phase in a fresh process and keep every receipt, including failed attempts.
No package installation, source mutation, task policy, or production project is performed here.
"""
import argparse
import faulthandler
from contextlib import contextmanager
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time
import traceback
from types import SimpleNamespace
import xml.etree.ElementTree as ET


COMMIT = 'ebb71702f3f70e767f40fd8e050674af0288abe8'
REPORT = {'purpose': 'SYSTEM_VERIFICATION_ONLY', 'checks': [], 'upstreamCommit': COMMIT}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda: file.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def save():
    target = Path(REPORT['receipt'])
    temporary = target.with_suffix('.tmp')
    with temporary.open('w') as file:
        json.dump(REPORT, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(target)


@contextmanager
def check(name):
    row = {'name': name, 'status': 'running'}
    REPORT['checks'].append(row)
    save()
    started = time.perf_counter()
    try:
        yield row
    except BaseException as error:
        row.update(status='failed', error=type(error).__name__ + ': ' + str(error),
                   traceback=traceback.format_exc())
        raise
    else:
        row['status'] = 'passed'
    finally:
        row['seconds'] = time.perf_counter() - started
        save()
        print(json.dumps({'check': name, 'status': row['status'], 'seconds': row['seconds']}), flush=True)


def identity(*, full=False):
    started = time.perf_counter()
    def file_identity(path):
        path = Path(path)
        stat = path.stat()
        result = {'bytes': stat.st_size, 'device': stat.st_dev, 'inode': stat.st_ino,
                  'mtimeNs': stat.st_mtime_ns, 'ctimeNs': stat.st_ctime_ns}
        if full:
            result['sha256'] = sha256(path)
        return result
    distributions = {}
    for name in ('isaacsim', 'torch', 'warp-lang', 'numpy', 'nvidia-curobo', 'trimesh', 'yourdfpy'):
        try:
            distributions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            distributions[name] = None
    modules = {}
    for name, module in sorted(sys.modules.items()):
        if name in ('torch', 'warp', 'numpy', 'curobo', 'isaacsim', 'trimesh', 'scipy',
                    'yaml', 'yourdfpy', 'networkx', 'quaternion') or name.startswith(('curobo.', 'warp.')):
            path = getattr(module, '__file__', None)
            if path and Path(path).is_file():
                modules[name] = {'path': str(Path(path).resolve()), **file_identity(path),
                                 'reportedVersion': str(getattr(module, '__version__', 'unavailable'))}
    mapped = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                     if '/' in line and any(key in line for key in ('/torch/', '/warp/', '/curobo/', '/torch-extensions/'))})
    native = {path: file_identity(path)
              for path in mapped if Path(path).is_file() and '.so' in Path(path).name}
    result = {'executable': sys.executable, 'prefix': sys.prefix, 'python': sys.version,
              'SOABI': sysconfig.get_config_var('SOABI'), 'distributions': distributions,
              'modules': modules, 'mappedNativeLibraries': native}
    gpu_query = subprocess.run(['nvidia-smi', '-q', '-x'], capture_output=True, text=True, check=True)
    devices = []
    for gpu in ET.fromstring(gpu_query.stdout).findall('gpu'):
        for process in gpu.findall('./processes/process_info'):
            if process.findtext('pid') == str(os.getpid()):
                devices.append({'uuid': gpu.findtext('uuid'),
                                'memory': process.findtext('used_memory'),
                                'type': process.findtext('type')})
    result['actualProcessGpuDevices'] = devices
    if 'torch' in sys.modules:
        torch = sys.modules['torch']
        result['torchABI'] = {name: getattr(torch._C, name, None) for name in
                             ('_GLIBCXX_USE_CXX11_ABI', '_PYBIND11_COMPILER_TYPE',
                              '_PYBIND11_STDLIB', '_PYBIND11_BUILD_ABI')}
        result['torchCUDA'] = torch.version.cuda
        if torch.cuda.is_initialized():
            result['cudaMemory'] = {
                'allocated': torch.cuda.memory_allocated(),
                'reserved': torch.cuda.memory_reserved(),
                'peakAllocated': torch.cuda.max_memory_allocated(),
                'peakReserved': torch.cuda.max_memory_reserved()}
    if 'warp' in sys.modules:
        result['warpKernelCacheDirectory'] = str(sys.modules['warp'].config.kernel_cache_dir)
    result['contentHashesComputed'] = full
    result['identityVerificationSeconds'] = time.perf_counter() - started
    return result


def imports(args):
    import torch
    torch.cuda.set_device(0)
    import warp
    # NVIDIA AutoData (Apache-2.0), pinned 953cd37778abd79f1c4621aa20183b0c5f9096a9:
    # autodata_interfaces/motion_planners/curobo/curobo_planner.py:35-38.
    # Adapt the removed namespace, without changing locked cuRobo or Warp source.
    applied = not hasattr(warp, 'torch')
    if applied:
        warp.torch = SimpleNamespace(device_from_torch=warp.device_from_torch)
    REPORT['compatibilityAdaptations'] = [{
        'name': 'AutoData Warp device_from_torch namespace bridge', 'applied': applied,
        'source': 'https://github.com/isaac-sim/AutoData/blob/953cd37778abd79f1c4621aa20183b0c5f9096a9/autodata_interfaces/motion_planners/curobo/curobo_planner.py#L35-L38',
        'license': 'Apache-2.0', 'changesInstalledSources': False}]
    import curobo
    import numpy
    site = Path(sys.prefix) / 'lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
    for name, module, version in (('torch', torch, '2.11.0+cu128'),
                                  ('warp', warp, '1.13.0'), ('numpy', numpy, '2.3.1')):
        actual_version = str(module.__version__)
        if name == 'warp':
            actual_version = actual_version.removesuffix('+cu12')
        if actual_version != version:
            raise RuntimeError('actual imported ' + name + ' version differs from the selected prefix')
        selected = site / name / '__init__.py'
        if name == 'warp':
            matches = list((site / 'isaacsim/extscache').glob('omni.warp.core-1.13.0+*/warp/__init__.py'))
            if len(matches) != 1:
                raise RuntimeError('exactly one SDK Warp1.13 source family is required')
            selected = matches[0]
        if Path(module.__file__).resolve() != selected.resolve():
            raise RuntimeError('actual imported ' + name + ' family differs from the selected prefix')
    if not Path(curobo.__file__).resolve().is_relative_to(site / 'curobo'):
        raise RuntimeError('actual imported cuRobo family differs from the selected prefix')
    from curobo.wrap.reacher.motion_gen import MotionGen, MotionGenConfig, MotionGenPlanConfig
    from curobo.types.base import TensorDeviceType
    from curobo.types.math import Pose
    from curobo.types.state import JointState
    from curobo.geom.types import WorldConfig
    from curobo.rollout.cost.pose_cost import PoseCostMetric
    for name in ('lbfgs_step_cu', 'kinematics_fused_cu', 'line_search_cu', 'tensor_step_cu', 'geom_cu'):
        importlib.import_module('curobo.curobolib.' + name)
    actual = str(torch.cuda.get_device_properties(0).uuid)
    if actual.removeprefix('GPU-') != args.gpu_uuid.removeprefix('GPU-'):
        raise RuntimeError('actual CUDA device differs from assigned UUID: ' + actual)
    return torch, MotionGen, MotionGenConfig, MotionGenPlanConfig, TensorDeviceType, Pose, JointState, WorldConfig, PoseCostMetric


def numerical(args):
    with check('actual_imports_and_native_extensions') as row:
        torch, MotionGen, Config, PlanConfig, TensorArgs, Pose, JointState, WorldConfig, Metric = imports(args)
        row['device'] = {'logicalIndex': 0, 'uuid': args.gpu_uuid,
                         'name': torch.cuda.get_device_name(0)}
    with check('actual_warp_cuda_initialization') as row:
        import warp
        warp.init()
        devices = warp.get_cuda_devices()
        row.update(toolkitVersion=warp.get_cuda_toolkit_version(),
                   driverVersion=warp.get_cuda_driver_version(),
                   actualCudaDevices=list(map(str, devices)))
        if len(devices) != 1:
            raise RuntimeError('Warp must expose exactly the assigned CUDA device')
    tensor_args = TensorArgs(device=torch.device('cuda:0'))
    with check('cold_model_and_world_load') as row:
        world = WorldConfig.from_dict({'cuboid': {'probe_obstacle': {
            'dims': [.2, .2, .2], 'pose': [3., 3., 3., 1., 0., 0., 0.]}}})
        config = Config.load_from_robot_config('franka.yml', world, tensor_args=tensor_args,
            use_cuda_graph=True, num_trajopt_seeds=4, num_graph_seeds=4,
            interpolation_dt=.02, collision_cache={'obb': 8, 'mesh': 8},
            position_threshold=.001, rotation_threshold=.002)
        generator = MotionGen(config)
        row['jointOrder'] = list(generator.kinematics.joint_names)
        row['sourceKind'] = 'upstream bundled Franka numerical fixture; native calibration is a separate check'
    with check('cold_warmup'):
        generator.warmup(enable_graph=True, warmup_js_trajopt=False)
        torch.cuda.synchronize()
    q = generator.get_retract_config().reshape(1, -1)
    start = JointState.from_position(q, joint_names=generator.kinematics.joint_names)
    q_goal = q.clone()
    q_goal[0, 0] += .02
    goal = generator.compute_kinematics(JointState.from_position(q_goal,
        joint_names=generator.kinematics.joint_names)).ee_pose
    with check('warm_numerical_ik') as row:
        result = generator.ik_solver.solve_single(goal, retract_config=q,
            seed_config=q.reshape(1, 1, -1), return_seeds=4)
        torch.cuda.synchronize()
        if not bool(result.success.any()):
            raise RuntimeError('actual numerical IK did not return any valid candidate')
        row['success'] = result.success.detach().cpu().tolist()
        row['positionError'] = result.position_error.detach().cpu().tolist()
    with check('warm_goalset_path') as row:
        goals = Pose(position=torch.stack([goal.position[0] + 5., goal.position[0]], dim=0).unsqueeze(0),
                     quaternion=torch.stack([goal.quaternion[0], goal.quaternion[0]], dim=0).unsqueeze(0))
        result = generator.plan_goalset(start, goals, PlanConfig(max_attempts=4))
        torch.cuda.synchronize()
        if not bool(result.success.any()):
            raise RuntimeError('goalset path failed: ' + str(result.status))
        if result.goalset_index is None or int(result.goalset_index.item()) != 1:
            raise RuntimeError('goalset planning did not select the reachable second candidate')
        path = result.get_interpolated_plan()
        row['waypoints'] = len(path.position)
        row['jointOrder'] = list(path.joint_names)
        row['selectedGoalIndex'] = int(result.goalset_index.item())
    with check('world_pose_update_changes_actual_collision_result') as row:
        blocked = Pose(position=goal.position.clone(), quaternion=goal.quaternion.clone())
        generator.world_coll_checker.update_obstacle_pose('probe_obstacle', blocked, update_cpu_reference=True)
        result = generator.plan_single(start, goal, PlanConfig(max_attempts=2))
        torch.cuda.synchronize()
        if bool(result.success.any()):
            raise RuntimeError('obstacle covering the goal did not reject the path')
        row['blockedStatus'] = str(result.status)
        far = Pose.from_list([3., 3., 3., 1., 0., 0., 0.], tensor_args)
        generator.world_coll_checker.update_obstacle_pose('probe_obstacle', far, update_cpu_reference=True)
        result = generator.plan_single(start, goal, PlanConfig(max_attempts=4))
        if not bool(result.success.any()):
            raise RuntimeError('restored collision world did not recover the same feasible path')
    with check('temporary_pose_metric_restores_after_exception') as row:
        costs = [rollout.goal_cost for rollout in generator.get_all_pose_rollout_instances()]
        before = [(cost.run_vec_weight.clone(), cost.vec_weight.clone(),
                   cost.offset_tstep_fraction.clone(), cost.project_distance_tensor.clone(),
                   cost.offset_waypoint.clone()) for cost in costs]
        row['beforeRunWeights'] = [saved[0].detach().cpu().tolist() for saved in before]
        same_pose = generator.compute_kinematics(start).ee_pose
        metric = Metric(hold_partial_pose=True,
            hold_vec_weight=tensor_args.to_device([1., 1., 1., 0., 0., 0.]))
        try:
            if not generator.update_pose_cost_metric(metric, start, same_pose):
                raise RuntimeError('valid numeric partial-pose metric was rejected')
            raise LookupError('SYSTEM_VERIFICATION_ONLY: injected request failure')
        except LookupError:
            pass
        finally:
            # reset_metric() normalizes all rollouts; it is not a transaction rollback.
            # Restore exactly the fields this partial-pose request changes, per rollout.
            for cost, saved in zip(costs, before):
                cost.update_metric(Metric(hold_partial_pose=True, hold_vec_weight=saved[0],
                    reach_partial_pose=True, reach_vec_weight=saved[1],
                    project_to_goal_frame=bool(saved[3].item())), update_offset_waypoint=False)
        for cost, saved in zip(costs, before):
            if not all(torch.equal(actual, expected) for actual, expected in zip(
                    (cost.run_vec_weight, cost.vec_weight, cost.offset_tstep_fraction,
                     cost.project_distance_tensor, cost.offset_waypoint), saved)):
                raise RuntimeError('metric state after scoped restore differs from actual pre-request state')
        row['rolloutCount'] = len(costs)
        row['scope'] = 'real library state restoration; adapter must still enforce request scopes'
    with check('planning_payload_sphere_attach_detach_lifecycle') as row:
        kinematics = generator.kinematics.kinematics_config
        count = kinematics.get_number_of_spheres('attached_object')
        before = kinematics.get_link_spheres('attached_object').clone()
        if int((before[:, 3] > 0).sum()) != 0:
            raise RuntimeError('fresh numerical fixture unexpectedly has an active payload')
        spheres = tensor_args.to_device([[0., 0., .12, .015]] * count)
        try:
            generator.attach_spheres_to_robot(sphere_tensor=spheres, link_name='attached_object')
            active = kinematics.get_link_spheres('attached_object')
            if int((active[:, 3] > 0).sum()) != count:
                raise RuntimeError('library did not activate the declared planning spheres')
        finally:
            generator.detach_spheres_from_robot(link_name='attached_object')
        after = kinematics.get_link_spheres('attached_object')
        if int((after[:, 3] > 0).sum()) != 0:
            raise RuntimeError('detach left active planning payload spheres')
        row.update(sphereCount=count,
            scope='numerical attachment slots only; not physical grasp or surface-coverage qualification')
    return generator


def native(args):
    import numpy as np
    from isaacsim import SimulationApp
    app = None
    with check('isaac_same_process_startup') as row:
        directory = Path(args.output).resolve().parent
        app = SimulationApp({'headless': True, 'active_gpu': args.gpu_index, 'physics_gpu': 0,
            'multi_gpu': False, 'max_gpu_count': 1, 'renderer': 'RaytracedLighting',
            'enable_crashreporter': False, 'extra_args': ['--/app/settings/persistent=false',
                '--portable', '--portable-root', str(directory / 'kit'),
                '--/log/file=' + str(directory / 'isaac-sdk.log'),
                '--/renderer/multiGpu/activeGpus=' + str(args.gpu_index)]})
        row['renderPhysicalGpuIndex'] = args.gpu_index
        import carb
        row['configuredActiveGpus'] = carb.settings.get_settings().get('/renderer/multiGpu/activeGpus')
    generator = numerical(args)
    with check('native_franka_actual_q_fk_and_tcp_calibration') as row:
        import omni.usd
        from pxr import Gf, PhysxSchema, UsdGeom, UsdPhysics
        import warp as wp
        import isaacsim.core.experimental.utils.app as app_utils
        from isaacsim.core.experimental.prims import Articulation, RigidPrim
        from isaacsim.core.experimental.utils.backend import use_backend
        from isaacsim.core.simulation_manager import SimulationManager as Sim
        from curobo.types.state import JointState
        import torch
        import trimesh
        package = Path(args.robot_package)
        closure = json.loads((package / 'closure-manifest.json').read_text())
        entry = (package / closure['nativeEntry']).resolve()
        if not entry.is_relative_to(package.resolve()):
            raise RuntimeError('native robot entry escapes the read-only package')
        omni.usd.get_context().new_stage()
        stage = omni.usd.get_context().get_stage()
        UsdGeom.SetStageMetersPerUnit(stage, 1.)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.Xform.Define(stage, '/World')
        physics = UsdPhysics.Scene.Define(stage, '/World/physics')
        physics.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
        physics.CreateGravityMagnitudeAttr(9.81)
        physx = PhysxSchema.PhysxSceneAPI.Apply(physics.GetPrim())
        physx.CreateTimeStepsPerSecondAttr(500)
        physx.CreateEnableGPUDynamicsAttr(False)
        physx.CreateBroadphaseTypeAttr('MBP')
        robot = UsdGeom.Xform.Define(stage, '/World/probe_robot')
        robot.GetPrim().GetReferences().AddReference(str(entry))
        with wp.ScopedDevice('cpu'), use_backend('usd', raise_on_unsupported=True, raise_on_fallback=True):
            articulation = Articulation('/World/probe_robot', reset_xform_op_properties=False)
            links = RigidPrim(['/World/probe_robot/panda_link0', '/World/probe_robot/panda_hand'],
                              reset_xform_op_properties=False)
        app_utils.play(commit=True)
        with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
            names = list(articulation.dof_names)
            order = list(generator.kinematics.joint_names)
            indices = articulation.get_dof_indices(order)
            fixture_q = generator.get_retract_config().reshape(-1).detach().cpu().tolist()
            all_indices = articulation.get_dof_indices(order + ['panda_finger_joint1', 'panda_finger_joint2'])
            drive_indices = articulation.get_dof_indices(order + ['panda_finger_joint1'])
            articulation.set_default_state(dof_positions=[fixture_q + [.04, .04]],
                dof_velocities=[[0.] * 9], dof_indices=all_indices)
            articulation.reset_to_default_state()
            articulation.set_dof_position_targets([fixture_q + [.04]], dof_indices=drive_indices)
            articulation.set_dof_velocity_targets([[0.] * 8], dof_indices=drive_indices)
        before_steps, before_time = int(Sim.get_num_physics_steps()), float(Sim.get_simulation_time())
        for _ in range(5):
            Sim.step(steps=1, update_fabric=False)
        with use_backend('tensor', raise_on_unsupported=True, raise_on_fallback=True):
            q = articulation.get_dof_positions(dof_indices=indices).numpy().copy()
            low, high = articulation.get_dof_limits(dof_indices=indices)
            native_limits = np.column_stack((low.numpy()[0], high.numpy()[0]))
            positions, quaternions = links.get_world_poses()
            positions, quaternions = positions.numpy().copy(), quaternions.numpy().copy()
        model_limits = generator.kinematics.get_joint_limits().position.detach().cpu().numpy().T
        if (model_limits.shape != native_limits.shape or
                np.any(model_limits[:, 0] < native_limits[:, 0] - .0002) or
                np.any(model_limits[:, 1] > native_limits[:, 1] + .0002)):
            raise RuntimeError('planning model allows positions outside actual native joint limits')
        if np.any(q[0] < native_limits[:, 0] - .0002) or np.any(q[0] > native_limits[:, 1] + .0002):
            raise RuntimeError('measured FK calibration pose is outside native joint limits')
        row['fixtureInitialJointTargets'] = fixture_q + [.04, .04]
        row['fixtureScope'] = 'isolated native reset to a legal nonzero numerical fixture; not a delivered trajectory'
        base = trimesh.transformations.quaternion_matrix(quaternions[0])
        base[:3, 3] = positions[0]
        actual = trimesh.transformations.quaternion_matrix(quaternions[1])
        actual[:3, 3] = positions[1]
        predicted = base @ generator.compute_kinematics(JointState.from_position(
            torch.as_tensor(q, device='cuda:0', dtype=torch.float32), joint_names=order)).ee_pose.get_matrix()[0].detach().cpu().numpy()
        position_error = float(np.linalg.norm(actual[:3, 3] - predicted[:3, 3]))
        angle = float(np.arccos(np.clip((np.trace(actual[:3, :3].T @ predicted[:3, :3]) - 1) / 2, -1, 1)))
        cache = UsdGeom.XformCache()
        hand_usd = np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath('/World/probe_robot/panda_hand'))).T
        tcp_usd = np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath('/World/probe_robot/panda_hand/tool_center'))).T
        tcp_local = np.linalg.inv(hand_usd) @ tcp_usd
        predicted_tcp, actual_tcp = predicted @ tcp_local, actual @ tcp_local
        tcp_error = float(np.linalg.norm(predicted_tcp[:3, 3] - actual_tcp[:3, 3]))
        row.update(jointOrder=order, actualDofNames=names, actualQ=q.tolist(),
            nativeJointLimits=native_limits.tolist(), modelJointLimits=model_limits.tolist(),
            handErrorM=position_error, handErrorRad=angle, tcpErrorM=tcp_error,
            actualTcpLocal=tcp_local.tolist(), robotEntry=str(entry), robotEntrySha256=sha256(entry),
            stageId=int(omni.usd.get_context().get_stage_id()))
        if position_error > .002 or angle > .002 or tcp_error > .002:
            raise RuntimeError('cuRobo bundled model differs from actual native Franka FK/TCP')
        row['actualPhysicalSteps'] = int(Sim.get_num_physics_steps()) - before_steps
        row['actualSimulationSeconds'] = float(Sim.get_simulation_time()) - before_time
        if row['actualPhysicalSteps'] != 5 or abs(row['actualSimulationSeconds'] - .01) > 1e-7:
            raise RuntimeError('native physics clock differs from the five requested 2ms steps')
        row['scope'] = 'native model/coordinate/library coexistence only; not manipulation qualification'
    REPORT['identity'] = identity(full=True)
    if {row['uuid'] for row in REPORT['identity']['actualProcessGpuDevices']} != {args.gpu_uuid}:
        raise RuntimeError('native process did not stay on its assigned GPU UUID')
    REPORT['identityCapturedBeforeSdkShutdown'] = True
    REPORT['status'] = 'passed'
    save()  # The SDK may terminate the process during official fast shutdown.
    app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=('numerical', 'native'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--gpu-index', required=True, type=int)
    parser.add_argument('--gpu-uuid', required=True)
    parser.add_argument('--robot-package')
    args = parser.parse_args()
    if args.phase == 'native' and not args.robot_package:
        parser.error('native phase requires a real frozen robot package')
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_uuid
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    # The pinned upstream explicitly requires this opt-in for changing goal shapes.
    os.environ['CUROBO_TORCH_CUDA_GRAPH_RESET'] = '1'
    REPORT['upstreamOptions'] = {'CUROBO_TORCH_CUDA_GRAPH_RESET': '1'}
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    REPORT.update(receipt=str(Path(args.output).resolve()), phase=args.phase,
        status='running', startedAtUtc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        assignedGpu={'physicalIndex': args.gpu_index, 'uuid': args.gpu_uuid},
        probeSourceSha256=sha256(__file__), command=list(sys.argv))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    # Diagnostic stack prints only; this does not cancel or budget the probe.
    faulthandler.dump_traceback_later(60., repeat=True)
    started = time.perf_counter()
    code = 0
    try:
        (native if args.phase == 'native' else numerical)(args)
        REPORT['status'] = 'passed'
    except BaseException as error:
        REPORT.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        code = 1
    finally:
        REPORT['wallSeconds'] = time.perf_counter() - started
        try:
            if 'identity' not in REPORT:
                REPORT['identity'] = identity()
            if REPORT['status'] == 'passed' and {
                    row['uuid'] for row in REPORT['identity']['actualProcessGpuDevices']} != {args.gpu_uuid}:
                raise RuntimeError('probe did not stay on its assigned GPU UUID')
        except BaseException as error:
            REPORT['identityFailure'] = type(error).__name__ + ': ' + str(error)
            REPORT['status'] = 'failed'
            code = 1
        save()
    faulthandler.cancel_dump_traceback_later()
    return code


if __name__ == '__main__':
    sys.exit(main())
