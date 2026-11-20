"""SYSTEM_VERIFY_ONLY: native-I/O CPU behavior; no mock result qualifies physical execution."""
from contextlib import nullcontext
from copy import deepcopy
from importlib.util import find_spec
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from runtime import simulation


class Array:
    def __init__(self, value):self.value = np.asarray(value, dtype=np.float32)
    def numpy(self):return self.value.copy()


class Articulation:
    def __init__(self):
        self.dof_names = ['axis', 'slider', 'wheel', 'motor']
        self.dof_types = [0, 1, 0, 0]
        self.dof_paths = [['/World/mechanism/'+name for name in self.dof_names]]
        self.position, self.velocity, self.effort = np.array([[.2, .03, 0, 0]], np.float32), np.zeros((1,4), np.float32), np.zeros((1,4), np.float32)
        self.calls = []
    def get_dof_gains(self, *, dof_indices):
        return Array(np.array([[20,20,0,0]])[:, dof_indices]), Array(np.array([[2,2,2,0]])[:, dof_indices])
    def get_dof_limits(self, *, dof_indices):
        return Array(np.array([[-2,0,-2,-2]])[:, dof_indices]), Array(np.array([[2,.1,2,2]])[:, dof_indices])
    def get_dof_max_velocities(self, *, dof_indices):return Array([[2]*len(dof_indices)])
    def get_dof_max_efforts(self, *, dof_indices):return Array([[5]*len(dof_indices)])
    def get_dof_position_targets(self, *, dof_indices):return Array(self.position[:, dof_indices])
    def get_dof_velocity_targets(self, *, dof_indices):return Array(self.velocity[:, dof_indices])
    def get_dof_efforts(self, *, dof_indices):return Array(self.effort[:, dof_indices])
    def get_dof_positions(self, *, dof_indices):return Array(self.position[:, dof_indices])
    def get_dof_velocities(self, *, dof_indices):return Array(self.velocity[:, dof_indices])
    def set_dof_position_targets(self, value, *, dof_indices):
        self.calls.append(('position', deepcopy(value).tolist())); self.position[:, dof_indices] = value
    def set_dof_velocity_targets(self, value, *, dof_indices):
        self.calls.append(('velocity', deepcopy(value).tolist())); self.velocity[:, dof_indices] = value
    def set_dof_efforts(self, value, *, dof_indices):
        self.calls.append(('effort', deepcopy(value).tolist())); self.effort[:, dof_indices] = value


class NativeEnvironmentTests(unittest.TestCase):
    def test_batch_camera_constructor_uses_only_selected_views_and_one_capture(self):
        from runtime.scene import World
        io={'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}
        selected={'side':{'cameraId':'side'},'front':{'cameraId':'front'}}
        state={'time':0.,'simulationTime':1.,'timeOrigin':1.,'physicsStep':0,'stageId':17,
            'streams':{},'streamStatus':{},'cameraFrames':{}}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(simulation.Environment,'_open_native',autospec=True) as opened, \
             patch.object(simulation.Environment,'capture_frame',autospec=True) as captured, \
             patch.object(simulation.Environment,'export_state',return_value=state):
            env=simulation.Environment({'io':io,'recordingCameras':{'other':{'unselected':{}}}},
                World(native=True),Path(directory),Mock(),record=False,cameras=selected)
            opened.assert_called_once_with(env,False);captured.assert_called_once_with(env)
            self.assertEqual(env.camera_specs,selected)
            self.assertEqual(env.primary_camera_id,'side');self.assertTrue(env._camera_override)
            self.assertEqual(env.initial_state['physicsStep'],0)
            env.camera_specs['side']['cameraId']='changed'
            self.assertEqual(selected['side']['cameraId'],'side')

    def test_native_scene_manager_must_match_the_actual_source_path_and_physics(self):
        path='/World/customSolver'
        scene=SimpleNamespace(prim=SimpleNamespace(GetPath=lambda:path),get_dt=lambda:.005,
            get_enabled_gpu_dynamics=lambda:True,get_broadphase_type=lambda:'GPU')
        manager=SimpleNamespace(get_physics_scenes=lambda:[scene])
        self.assertIs(simulation._verify_native_physics_scene(manager,path,200,True,'GPU'),scene)
        for scenes,expected in (([],path),([scene,scene],path),([scene],'/World/physicsScene')):
            with self.subTest(scenes=len(scenes),path=expected),self.assertRaises(ValueError):
                simulation._verify_native_physics_scene(SimpleNamespace(get_physics_scenes=lambda:scenes),expected,200,True,'GPU')
        for rate,gpu,broadphase in ((100,True,'GPU'),(200,False,'GPU'),(200,True,'MBP')):
            with self.subTest(rate=rate,gpu=gpu,broadphase=broadphase),self.assertRaises(ValueError):
                simulation._verify_native_physics_scene(manager,path,rate,gpu,broadphase)

    def test_authored_particle_output_cannot_be_promoted_to_a_native_fact(self):
        from pxr import Sdf,Usd,UsdGeom
        stage=Usd.Stage.CreateInMemory();points=UsdGeom.Points.Define(stage,'/World/particles')
        io={'streams':{'p':{'source':{'reader':'particle_state','path':'/World/particles','field':'positions'}}}}
        simulation._reject_authored_particle_outputs(stage,io)
        points.GetPrim().CreateAttribute('physxParticle:simulationPoints',Sdf.ValueTypeNames.Point3fArray).Set([(1,2,3)])
        with self.assertRaisesRegex(ValueError,'authored'):
            simulation._reject_authored_particle_outputs(stage,io)

    def test_semantic_native_bindings_reject_missing_variant_relative_and_property_paths(self):
        from pxr import Usd
        stage=Usd.Stage.CreateInMemory();stage.DefinePrim('/World/actual')
        simulation._verify_native_bindings(stage,{'objectPaths':{'object':'/World/actual'}})
        for path in ('World/actual','/World/missing','/World/actual.xformOp:translate','/World/actual{v=one}'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                simulation._verify_native_bindings(stage,{'objectPaths':{'object':path}})

    def test_recording_calibration_is_checked_against_actual_usd_and_control_cadence(self):
        from pxr import Usd,UsdGeom
        env=object.__new__(simulation.Environment);env.control_rate=50
        env.stage=Usd.Stage.CreateInMemory()
        camera=UsdGeom.Camera.Define(env.stage,'/World/camera')
        camera.CreateFocalLengthAttr(24);camera.CreateHorizontalApertureAttr(36);camera.CreateVerticalApertureAttr(27)
        spec={'primPath':'/World/camera','resolution':[640,480],'rateHz':25,
            'intrinsics':simulation.camera_intrinsics(640,480),'worldFromCamera':np.eye(4).tolist()}
        simulation._verify_native_camera(env.stage,spec,env.control_rate)
        for altered in ({**spec,'rateHz':100},{**spec,'rateHz':20},
                {**spec,'intrinsics':{**spec['intrinsics'],'fx':1}},
                {**spec,'primPath':'/World/missing'}):
            with self.subTest(spec=altered),self.assertRaises(ValueError):
                simulation._verify_native_camera(env.stage,altered,env.control_rate)

    def test_sdk_startup_precedes_extension_imports(self):
        env=object.__new__(simulation.Environment)
        with tempfile.TemporaryDirectory() as name:
            env.source_directory=Path(name);env.compiled={}
            with patch.object(simulation,'_ensure_app',side_effect=RuntimeError('SDK startup reached')):
                with self.assertRaisesRegex(RuntimeError,'SDK startup reached'):env._open_native(False)

    def environment(self):
        env = object.__new__(simulation.Environment)
        env.io = {'schema': 'sceneagent-native-io-v1', 'clock': {'physicsRateHz':200,'controlRateHz':50},
            'ports': {name:{'source':{'primitive':kind,'path':'/World/mechanism','joints':joints}}
                for name,kind,joints in [('arm','joint_position',['axis','slider']),
                    ('wheel','joint_velocity',['wheel']),('motor','joint_effort',['motor'])]},
            'streams': {'q': {'source':{'reader':'joint_positions','path':'/World/mechanism','joints':['slider','axis']},
                'visibleToPolicy':True,'required':True,'rateHz':200}}}
        env.native_port_bindings, env.active_targets = {}, {}
        env.physics_rate, env.control_rate, env.substeps = 200,50,4
        art = Articulation()
        env._native_articulation = Mock(return_value=('/World/mechanism',art))
        env.native_bodies = {};env.camera_specs = {};env.recorder=None;env.task=None
        return env,art

    def backend(self):
        names = ['isaacsim','isaacsim.core','isaacsim.core.experimental','isaacsim.core.experimental.utils',
                 'isaacsim.core.experimental.utils.backend','isaacsim.core.simulation_manager']
        modules = {name:ModuleType(name) for name in names}
        modules[names[-2]].use_backend = lambda *a,**k:nullcontext()
        return modules

    def test_discovery_uses_actual_order_modes_units_limits_and_target_buffer(self):
        env,art = self.environment()
        frozen = env._discover_io()
        self.assertEqual(frozen['ports']['arm']['units'], ['rad','m'])
        self.assertEqual(frozen['ports']['arm']['nativeDofIndices'], [0,1])
        self.assertEqual(frozen['ports']['motor']['nativeDofIndices'], [3])
        self.assertEqual(frozen['ports']['arm']['limits'], [[None,None],[None,None]])
        np.testing.assert_allclose(frozen['ports']['arm']['physicalJointLimits'], [[-2,2],[0,.1]])
        self.assertEqual(frozen['ports']['wheel']['units'], ['rad/s'])
        self.assertEqual(frozen['ports']['motor']['persistence'], 'per_physics_step')
        self.assertEqual(frozen['ports']['motor']['readback'], 'actuation_buffer_not_measured_torque')
        self.assertEqual(frozen['streams']['q']['shape'], [2])
        self.assertEqual(frozen['streams']['q']['dtype'], 'float32')
        self.assertEqual(frozen['streams']['q']['source']['joints'], ['slider','axis'])
        self.assertEqual(art.calls, [])

    def test_declared_metadata_cannot_override_sdk_facts(self):
        env,art = self.environment()
        env.io['ports']['arm'].update(dtype='float64', units=['bananas'], initialized=False)
        frozen = env._discover_io()
        self.assertEqual(frozen['ports']['arm']['dtype'],'float32')
        self.assertTrue(frozen['ports']['arm']['initialized'])
        env.io['ports']['arm']['source']['primitive'] = 'joint_effort'
        with self.assertRaisesRegex(ValueError, 'gains'):
            env._discover_io()
        self.assertEqual(art.calls, [])

    def test_resolved_aliases_and_unknown_readers_fail_without_native_writes(self):
        env,art = self.environment()
        env.io['ports']['alias'] = {'source':{'primitive':'joint_position','path':'/World/parent','joints':['axis']}}
        with self.assertRaisesRegex(ValueError, 'overlap'):env._discover_io()
        del env.io['ports']['alias']
        env.io['streams']['spoof'] = {'source':{'reader':'arbitrary_python','path':'/World/mechanism'}}
        with self.assertRaisesRegex(ValueError, 'unsupported'):env._discover_io()
        self.assertEqual(art.calls, [])

    def test_observe_is_permission_filtered_cached_by_actual_clock_and_never_steps(self):
        env,art = self.environment();env.io = env._discover_io()
        env._closed=False;env._stream_cache={};env.latest_camera_frames={}
        clock={'time':0.,'simulationTime':.01,'timeOrigin':.01,'physicsStep':0,'stageId':9}
        env._clock=Mock(return_value=clock)
        env.io['streams']['hidden']={**deepcopy(env.io['streams']['q']),'visibleToPolicy':False}
        modules=self.backend()
        with patch.dict(sys.modules, modules):
            observed=env.observe()
            self.assertEqual(set(observed['streams']),{'q'})
            self.assertEqual(set(env.observe(truth=True)['streams']),{'q','hidden'})
            env._clock=Mock(side_effect=[clock,{**clock,'physicsStep':1,'time':.005,'simulationTime':.015}])
            with self.assertRaisesRegex(ValueError,'changed'):env.observe()
        self.assertEqual(art.calls, [])

    def step_fixture(self):
        env,art=self.environment();env.io=env._discover_io()
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        env.directory=Path(temporary.name);env._closed=False;env._failed=None;env.recorder=None
        env._operation_lock=threading.Lock();env._simulation_time=.01;env.time_origin=.01
        env.physical_seconds=.04;env.steps=0;env._sample_times={};env._particle_cache={};env._contact_cache={};env.task=None
        env.capture_frame=Mock();env._publish_policy_frames=Mock()
        def clock():return {'time':env.steps/200,'simulationTime':.01+env.steps/200,
            'timeOrigin':.01,'physicsStep':env.steps,'stageId':9}
        env._clock=Mock(side_effect=clock)
        env.observe=Mock(side_effect=lambda *a,**k:{**clock(),'streams':{},'streamStatus':{},'cameraFrames':{}})
        env._observe_raw=env.observe
        modules=self.backend()
        def step(**kwargs):env.steps+=1;env._simulation_time=.01+env.steps/200
        modules['isaacsim.core.simulation_manager'].SimulationManager=SimpleNamespace(step=Mock(side_effect=step))
        return env,art,modules

    def test_effort_is_reissued_each_actual_tick_and_omitted_persistent_targets_are_not_filled(self):
        env,art,modules=self.step_fixture()
        with patch.dict(sys.modules,modules):env.step({'motor':[.2]},seconds=.02)
        self.assertEqual([name for name,_ in art.calls],['effort']*4)
        self.assertEqual(env.steps,4)
        modules['isaacsim.core.simulation_manager'].SimulationManager.step.assert_called_with(steps=1,update_fabric=False)

    def test_long_task_truncates_at_its_own_horizon_after_crossing_five_minutes(self):
        from runtime.scene import Task, World
        from runtime.worker import episode_budgets
        world=World();world.box('body',(.1,.1,.1))
        for seconds in (301,1800,86400.):
            with self.subTest(seconds=seconds):
                task=Task('long',execution={'physicalSeconds':seconds},
                    rubric={'sourceRef':'rubrics.py','helperRefs':[]})
                task.bind(world)
                env,art,modules=self.step_fixture()
                env.physical_seconds=episode_budgets({},task)['physicalSeconds']
                committed=[]
                def commit(*args,**kwargs):
                    committed.append(kwargs['truncated'])
                    env.recorder.episode_done=kwargs['truncated']
                env.recorder=SimpleNamespace(episode_done=False,commit=commit)
                with patch.dict(sys.modules,modules):
                    env.steps=300*200;env._simulation_time=.01+300
                    env.step({'motor':[.2]},seconds=.02)
                    self.assertFalse(env.episode_done);self.assertEqual(committed,[False])
                    env.steps=round((seconds-.02)*200);env._simulation_time=.01+env.steps/200
                    env.step({'motor':[.2]},seconds=.02)
                    self.assertAlmostEqual(env.time,seconds)
                    self.assertTrue(env.episode_done);self.assertEqual(committed,[False,True])
                    with self.assertRaisesRegex(ValueError,'ended or failed'):env.step({'motor':[.2]})
                self.assertEqual(modules['isaacsim.core.simulation_manager'].SimulationManager.step.call_count,8)

    def test_invalid_or_over_budget_request_does_not_write_advance_or_poison_episode(self):
        env,art,modules=self.step_fixture()
        with patch.dict(sys.modules,modules):
            for action,seconds in (({'motor':[True]},.02),({'arm':[0,.03]},.02),({'motor':[0]},.06),
                    ({'arm':[1e100,.03],'motor':[0]},.02)):
                with self.subTest(action=action),self.assertRaises(ValueError):env.step(action,seconds)
                self.assertIsNone(env._failed)
        self.assertEqual(art.calls,[]);self.assertEqual(env.steps,0)

    def test_public_policy_view_and_array_mount_use_only_the_unique_committed_observation(self):
        from runtime.records import _array_reference,read_array
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        env=object.__new__(simulation.Environment)
        env.directory=Path(temporary.name);env.sensor_directory=env.directory/'sensors'
        visible=_array_reference(env.directory,np.arange(100,dtype=np.float32))
        private=_array_reference(env.directory,np.arange(200,dtype=np.float32))
        observation={'streams':{'sensor':visible},'cameraFrames':{}}
        env.recorder=SimpleNamespace(failed=False,committed_observation=deepcopy(observation),
            index={'privateTruth':private})
        env._observe_raw=Mock(side_effect=AssertionError('public read must not expose an uncommitted raw value'))
        self.assertEqual(env.observe(),observation)
        env._publish_policy_frames()
        np.testing.assert_array_equal(read_array(env.sensor_directory,visible),np.arange(100,dtype=np.float32))
        self.assertFalse((env.sensor_directory/private['ref']).exists())
        env._observe_raw.assert_not_called()

    def test_fresh_host_guard_reads_actual_source_without_retiming_the_policy_sensor(self):
        env,art=self.environment();env.io=env._discover_io()
        env.io['streams']['q']['rateHz']=50
        env._closed=False;env._stream_cache={};env.latest_camera_frames={}
        clock={'time':0.,'simulationTime':.01,'timeOrigin':.01,'physicsStep':0,'stageId':9}
        env._clock=Mock(return_value=clock)
        with patch.dict(sys.modules,self.backend()):
            first=env.observe()
            art.position[0,0]=.4
            fresh=env.export_state()
            cached=env.observe()
        np.testing.assert_array_equal(first['streams']['q'],cached['streams']['q'])
        self.assertNotEqual(float(first['streams']['q'][1]),float(fresh['streams']['q'][1]))
        self.assertEqual(first['streamStatus'],cached['streamStatus'])

    def test_native_contact_projection_preserves_uint64_ids_and_empty_buffer_shape(self):
        paths=['/World/a','/World/b','/World/c','/World/d']
        empty=simulation._native_contact_arrays({'headerCount':0,'dataCount':0,'headers':[]},paths)
        self.assertEqual(empty['headers'].shape,(0,13));self.assertEqual(empty['points'].shape,(0,3))
        copied={'headerCount':1,'dataCount':1,'headers':[{'stageId':9,'eventCode':1,
            'actor0Id':str(2**63+1),'actor1Id':'12','collider0Id':'13','collider1Id':'14',
            'actor0':paths[0],'actor1':paths[1],'collider0':paths[2],'collider1':paths[3],
            'proto0':2**32-1,'proto1':2**32-1,'offset':0,'count':1,
            'points':[{'positionWorldM':[.1,.2,.3],'impulseNative':[0,0,.2]}]}]}
        values=simulation._native_contact_arrays(copied,paths)
        self.assertEqual(values['headers'].dtype,np.uint64)
        self.assertEqual(int(values['headers'][0,1]),2**63+1)
        self.assertEqual(values['headers'][0,9:].tolist(),[0,1,2,3])
        self.assertEqual(values['impulses'].shape,(1,3))

    @unittest.skipUnless(find_spec('pxr') and find_spec('pxr.PhysxSchema'), 'requires the Isaac Sim PhysX schema')
    def test_contact_map_includes_real_instance_proxy_colliders_without_expanding_geometry(self):
        import pxr
        from pxr import Usd,UsdGeom,UsdPhysics
        for instance in (False,True):
            with self.subTest(instance=instance):
                env=object.__new__(simulation.Environment);env.stage=Usd.Stage.CreateInMemory()
                UsdPhysics.Scene.Define(env.stage,'/World/physicsScene')
                body=UsdGeom.Xform.Define(env.stage,'/World/body').GetPrim()
                UsdPhysics.RigidBodyAPI.Apply(body)
                UsdGeom.Xform.Define(env.stage,'/Library')
                UsdPhysics.CollisionAPI.Apply(UsdGeom.Cube.Define(env.stage,'/Library/collider').GetPrim())
                UsdGeom.Mesh.Define(env.stage,'/Library/visual')
                geometry=UsdGeom.Xform.Define(env.stage,'/World/body/geometry').GetPrim()
                geometry.GetReferences().AddInternalReference('/Library');geometry.SetInstanceable(instance)
                collider=env.stage.GetPrimAtPath('/World/body/geometry/collider')
                self.assertEqual(collider.IsInstanceProxy(),instance)
                references=geometry.GetMetadata('references')
                env.io={'streams':{'contact':{'source':{'reader':'contact_report','path':'/World/physicsScene',
                    'actors':['/World/body'],'field':'headers'}}}}
                # This unused native path module is absent from the CPU-only SDK process.
                with patch.object(pxr,'PhysicsSchemaTools',SimpleNamespace(),create=True):
                    paths=env._enable_native_contact_reports()
                self.assertEqual(paths,['/Library/collider','/World/body','/World/body/geometry/collider'])
                self.assertEqual(geometry.IsInstance(),instance)
                self.assertTrue(collider.HasAPI(UsdPhysics.CollisionAPI))
                self.assertEqual(collider.GetAttribute('size').Get(),2.)
                # Report setup may add its API to the body; the referenced geometry remains authored as-is.
                self.assertEqual(geometry.GetMetadata('references'),references)

    @unittest.skipUnless(find_spec('pxr') and find_spec('pxr.PhysxSchema'), 'requires the Isaac Sim PhysX schema')
    def test_no_contact_stream_does_not_enable_reporters_or_publish_a_path_table(self):
        import pxr
        from pxr import Usd,UsdGeom,UsdPhysics
        env=object.__new__(simulation.Environment);env.stage=Usd.Stage.CreateInMemory()
        UsdPhysics.RigidBodyAPI.Apply(UsdGeom.Xform.Define(env.stage,'/World/body').GetPrim())
        env.io={'streams':{}};before=env.stage.GetRootLayer().ExportToString()
        with patch.object(pxr,'PhysicsSchemaTools',SimpleNamespace(),create=True):
            self.assertEqual(env._enable_native_contact_reports(),[])
        self.assertEqual(env.stage.GetRootLayer().ExportToString(),before)

    def test_contact_reads_reject_unknown_paths_and_mismatched_native_ids_before_caching(self):
        import pxr
        paths=['/World/a','/World/b','/World/a/collider','/World/b/collider']
        native_ids={path:index+1 for index,path in enumerate(paths)}
        row={'eventCode':1,'proto0':2**32-1,'proto1':2**32-1,'offset':0,'count':0,'points':[]}
        for key,path in zip(('actor0','actor1','collider0','collider1'),paths):
            row[key]=path;row[key+'Id']=str(native_ids[path])
        modules={name:ModuleType(name) for name in ('omni','omni.physx')}
        modules['omni'].physx=modules['omni.physx']
        modules['omni.physx'].get_physx_simulation_interface=lambda:SimpleNamespace(get_contact_report=lambda:None)
        for failure in ('unknown_path','wrong_id'):
            for key in ('actor0','actor1','collider0','collider1'):
                with self.subTest(failure=failure,field=key):
                    env=object.__new__(simulation.Environment);env.stage_id=9;env.native_contact_paths=paths
                    env._contact_cache={};env._clock=Mock(return_value={'stageId':9,'physicsStep':1})
                    copied={'headerCount':1,'dataCount':0,'headers':[deepcopy(row)]}
                    if failure=='unknown_path':copied['headers'][0][key]='/World/missing'
                    else:copied['headers'][0][key+'Id']='99'
                    tools=SimpleNamespace(intToSdfPath=lambda value:'unused',sdfPathToInt=native_ids.__getitem__)
                    with patch.dict(sys.modules,modules),patch.object(pxr,'PhysicsSchemaTools',tools,create=True), \
                            patch.object(simulation,'_copy_public_contacts',return_value=copied):
                        with self.assertRaisesRegex(ValueError,'outside the frozen actual actor/collider map'):
                            env._read_contact_report({'field':'headers'})
                    self.assertEqual(env._contact_cache,{})

    def test_intermediate_rate_samples_keep_due_ticks_even_when_the_cycle_ends_between_samples(self):
        for rate,substeps in ((50,4),(40,5)):
            with self.subTest(controlRate=rate):
                env,art,modules=self.step_fixture()
                env.io['clock']['controlRateHz']=rate;env.control_rate=rate;env.substeps=substeps
                env.io['streams']['q']['rateHz']=100
                env._stream_cache={};env.latest_camera_frames={}
                env._observe_raw=simulation.Environment._observe_raw.__get__(env)
                env.observe=simulation.Environment.observe.__get__(env)
                commits=[]
                env.recorder=SimpleNamespace(episode_done=False,committed_observation=None,cycles=0,physical_steps=0,
                    commit=lambda *a,**k:commits.append((a,k)))
                def step(**kwargs):
                    env.steps+=1;env._simulation_time=.01+env.steps/200;art.position[0,0]=env.steps*.01
                modules['isaacsim.core.simulation_manager'].SimulationManager.step=Mock(side_effect=step)
                with patch.dict(sys.modules,modules):env.step({'motor':[.2]},1/rate)
                samples=commits[0][1]['physical_samples']
                self.assertEqual(len(samples),substeps)
                due=[sample for sample in samples if 'q' in sample['streams']]
                self.assertEqual([sample['physicsStep'] for sample in due],[2,4])
                self.assertEqual([sample['streamStatus']['q']['physicsStep'] for sample in due],[2,4])
                np.testing.assert_allclose([sample['streams']['q'][1] for sample in due],[.02,.04])
                if substeps==5:
                    self.assertEqual(samples[-1]['streams'],{})
                    self.assertEqual(commits[0][0][7]['streamStatus']['q']['physicsStep'],4)

    def test_optional_stream_failure_does_not_retry_off_phase_or_retime_a_stale_value(self):
        env,_=self.environment();env.io=env._discover_io()
        env.io['streams']['q'].update(rateHz=100,required=False)
        env._closed=False;env._stream_cache={};env.latest_camera_frames={};env.steps=0
        env._clock=lambda:{'time':env.steps/200,'simulationTime':.01+env.steps/200,
            'timeOrigin':.01,'physicsStep':env.steps,'stageId':9}
        def read(source,**kwargs):
            if env.steps==2:raise RuntimeError('native optional sample failed')
            return np.array([env.steps,env.steps],dtype=np.float32)
        env._read_source=Mock(side_effect=read)
        with patch.dict(sys.modules,self.backend()):
            env.observe();env.steps=2
            self.assertEqual(env.observe()['streamStatus']['q']['status'],'unavailable')
            env.steps=3
            self.assertNotIn('q',env.observe()['streams'])
            self.assertEqual(env._read_source.call_count,2)
            env.steps=4;actual=env.observe()
            self.assertEqual(actual['streamStatus']['q']['physicsStep'],4)
            np.testing.assert_array_equal(actual['streams']['q'],[4,4])


if __name__=='__main__':unittest.main()
