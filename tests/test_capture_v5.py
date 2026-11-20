"""SYSTEM_VERIFICATION_ONLY CPU sensor/renderer stubs; never SDK execution evidence."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from runtime.scene import Task, World
from runtime.records import Recorder
from runtime import simulation as sim
from tests.test_records import RubricFixture, BASE_IO


class CaptureBoundaryTests(unittest.TestCase):
    def test_scene_frame_mount_never_silently_uses_a_moved_ancestor_initial_pose(self):
        world = World();world.bind('body','/World/body')
        world.space('sensor_frame',at=[.1,0,.2],frame='body')
        declaration = {'frame':'sensor_frame','at':[0,0,0],'orientation':[1,0,0,0]}
        state = {'time':0.,'streams':{'body_pose':[2,0,.7,1,0,0,0]},
                 'streamStatus':{'body_pose':{'status':'available','sampleTime':0.}}}
        io={'streams':{'body_pose':{'source':{'reader':'rigid_pose','path':'/World/body'},
            'shape':[7],'frame':'world_wxyz','origin':'native_output'}}}
        pose = sim.recording_camera_pose(world,declaration,state=state,io=io)
        np.testing.assert_allclose(pose[:3,3],[2.1,0,.9],atol=1e-9,rtol=0)
        with self.assertRaisesRegex(ValueError,'actual native pose'):
            sim.recording_camera_pose(world,declaration,io=io)

    def test_environment_receives_the_complete_frozen_observation_contract(self):
        from runtime import worker
        full = deepcopy(BASE_IO)
        manifest = {'revision':'r1','runtime':'runtime-fixture','camera':{},'io':full,
                    'compiled':{'camera':{},'io':deepcopy(full)}}
        captured = {}
        def environment(compiled,*args,**fields):
            captured.update(compiled); return SimpleNamespace(compiled=compiled)
        with patch.object(sim,'Environment',side_effect=environment):
            worker.load_environment(Path('/fixture'),manifest,World(native=True),Path('/fixture-output'),Task('numeric'))
        self.assertEqual(captured['io'],full)
        self.assertEqual(manifest['compiled']['io'],full)

    def fixture(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        env = object.__new__(sim.Environment); env.directory = Path(temp.name)
        env.world = World(native=True); env.sensor_directory = env.directory/'policy-sensors'
        declarations = [{'cameraId':name, 'frame':'world' if name == 'front' else {'nativePath':'/World/fixture_robot/tool'},
            'at':[0,0,1] if name == 'front' else [0,0,.1], 'orientation':[1,0,0,0],
            'resolution':[32,32], 'rateHz':25, 'modalities':['rgb'], 'video':False,
            'required':name == 'front', 'source':'simulation_observer'} for name in ('front','wrist')]
        env.task = Task('t1',recording={'cameras':declarations,'primaryCameraId':'front'},
                        measurements={'camera':{'cameraIds':['front'],'visibleToPolicy':True}},
                        rubric={'sourceRef':'rubrics.py','helperRefs':[]})
        env.recording = env.task.recording
        env.io=deepcopy(BASE_IO)
        env.io['streams']['hand']={'source':{'reader':'rigid_pose','path':'/World/fixture_robot/tool'},
            'dtype':'float64','shape':[7],'units':['m']*3+['1']*4,'frame':'world_wxyz','origin':'native_output',
            'rateHz':50,'visibleToPolicy':False,'required':True}
        env.physics_rate,env.control_rate=500,50
        env._closed,env._failed=False,None
        env.native_articulations,env.native_bodies={},{}
        env.frames, env.latest_camera_frames, env.camera_failures = [], {}, {}
        env.primary_camera_id = 'front'; env.stage_id, env.camera_epoch, env.time_origin = 7, 1, 1
        env.stage = SimpleNamespace(GetSessionLayer=lambda:None,GetPrimAtPath=lambda path:object())
        env.recorder = None; env.steps = 0
        env.state_value = {}
        def advance(step):
            env.steps = step
            env.state_value = {'physicsStep':step,'simulationTime':1+step/500,'time':step/500,'timeOrigin':1,
                'stageId':7,'streams':{'q':[0]*9,'dq':[0]*9,'hand':[0,0,.8+step*.001,1,0,0,0]},
                'streamStatus':{'hand':{'status':'available','sampleTime':step/500,'physicsStep':step}},'cameraFrames':{}}
            env._sample_times = {1+step/500:step}
        env.advance = advance; advance(0)
        env.export_state = lambda:deepcopy(env.state_value)
        env._clock=lambda:{key:env.state_value[key] for key in ('time','simulationTime','timeOrigin','physicsStep','stageId')}
        env.camera_specs,env.camera_resources,poses = {},{},{}
        for declaration in declarations:
            name = declaration['cameraId']; prim = '/C/'+name
            spec = {**declaration,'primPath':prim,'intrinsics':{'fx':32*24/36,'fy':32*24/36,'cx':16,'cy':16},
                    'coordinateSpec':'CPU fixture camera axes','scope':'task:t1'}
            env.camera_specs[name] = spec
            pixels = SimpleNamespace(numpy=lambda:np.zeros((32,32,3),dtype=np.uint8))
            env.camera_resources[name] = {'seen':set(),'frameCount':0,'writer':None,'product':'/RP/'+name,
                'reference':SimpleNamespace(get_data=lambda:{'frame':env.steps+1}),
                'read_time':SimpleNamespace(get_data=lambda:{'simulationTime':env.state_value['simulationTime']}),
                'sensor':SimpleNamespace(get_data=Mock(return_value=(pixels,None))),'rgb_out':None}
        def write_camera(stage,spec):poses[spec['primPath']] = np.asarray(spec['worldFromCamera']);return spec
        self.enterContext(patch.object(sim,'_write_camera',side_effect=write_camera))
        def world_matrix(stage,path):
            if path=='/World/fixture_robot/tool':
                return sim._pose(env.state_value['streams']['hand'][:3],env.state_value['streams']['hand'][3:])
            return poses[path]
        self.enterContext(patch.object(sim,'_usd_world',side_effect=world_matrix))
        module = ModuleType('isaacsim.core.rendering_manager'); module.RenderingManager = SimpleNamespace(render=Mock())
        omni=ModuleType('omni');replicator=ModuleType('omni.replicator');core=ModuleType('omni.replicator.core')
        omni.replicator=replicator;replicator.core=core
        core.orchestrator=SimpleNamespace(step=Mock(side_effect=lambda **kwargs:module.RenderingManager.render()))
        pxr = ModuleType('pxr');pxr.Usd = SimpleNamespace(EditContext=lambda *args:nullcontext())
        self.enterContext(patch.dict(sys.modules,{'isaacsim.core.rendering_manager':module,'pxr':pxr,
            'omni':omni,'omni.replicator':replicator,'omni.replicator.core':core}))
        env.render = module.RenderingManager.render
        env.capture_step=core.orchestrator.step
        return env

    def observation(self,env,time,truth=False):
        value=env.export_state()
        if not truth:
            value['streams']={name:data for name,data in value['streams'].items() if env.io['streams'][name]['visibleToPolicy']}
            value['streamStatus']={name:data for name,data in value['streamStatus'].items() if env.io['streams'][name]['visibleToPolicy']}
        value['cameraFrames']=deepcopy(env.latest_camera_frames if truth else {
            k:v for k,v in env.latest_camera_frames.items() if k=='front'})
        return value

    def test_multiple_views_keep_physics_unchanged_and_track_current_wrist_mount(self):
        env = self.fixture();before = env.export_state();env.capture_frame()
        self.assertEqual(env.export_state(),before);self.assertEqual(len(env.frames),2)
        env.advance(20);before = env.export_state();env.capture_frame()
        self.assertEqual(env.export_state(),before)
        wrist = [f for f in env.frames if f['cameraId'] == 'wrist']
        self.assertNotEqual(wrist[0]['worldFromCamera'],wrist[1]['worldFromCamera'])
        for f in wrist:self.assertAlmostEqual(f['worldFromCamera'][2][3],.9+f['physicsStep']*.001)

    def test_camera_mount_uses_recorded_native_pose_when_usd_transform_lags(self):
        env = self.fixture();env.advance(20);before = env.export_state()
        actual_world = sim._usd_world.side_effect
        def lagged_world(stage, path):
            matrix = actual_world(stage, path).copy()
            if path == '/World/fixture_robot/tool':
                matrix[0, 3] += 3.2e-6
            return matrix
        with patch.object(sim, '_usd_world', side_effect=lagged_world):
            env.capture_frame()
        wrist = next(frame for frame in env.frames if frame['cameraId'] == 'wrist')
        expected = sim.recording_camera_pose(env.world, env.camera_specs['wrist'],
            state=before, io=env.io)
        np.testing.assert_allclose(wrist['worldFromCamera'], expected, atol=1e-12, rtol=0)
        self.assertEqual(env.export_state(), before)

    def test_capture_waits_for_actual_annotators_with_zero_timeline_delta(self):
        env=self.fixture();before=env.export_state();env.capture_frame()
        env.capture_step.assert_called_once_with(delta_time=0.,pause_timeline=False,wait_for_render=True)
        self.assertEqual(env.export_state(),before)

    def test_first_frame_coherence_budget_starts_after_renderer_bootstrap(self):
        for coherent in (True, False):
            with self.subTest(coherent=coherent):
                env=self.fixture();before=env.export_state()
                sensor=env.camera_resources['front']['sensor'].get_data
                sensor.side_effect=[(None,None),(None,None),sensor.return_value if coherent else (None,None)]
                wall=[0.]
                durations=iter((265,20,20 if coherent else 71))
                def render():
                    wall[0]+=next(durations)
                env.render.side_effect=render
                with patch.object(sim.time,'monotonic',side_effect=lambda:wall[0]):
                    if coherent:
                        env.capture_frame()
                        self.assertEqual(env.latest_camera_frames['front']['physicsStep'],before['physicsStep'])
                    else:
                        with self.assertRaisesRegex(TimeoutError,'coherent new frame'):
                            env.capture_frame()
                self.assertEqual(env.render.call_count,3)
                self.assertEqual(env.export_state(),before)

    def test_primary_wrist_metadata_tracks_the_current_mounted_source_without_frame_backfill(self):
        env=self.fixture();env.primary_camera_id='wrist';env.recording['primaryCameraId']='wrist'
        spec=env.camera_specs['wrist']
        spec['worldFromCamera']=sim.recording_camera_pose(env.world,spec,state=env.export_state(),stage=env.stage).tolist()
        env.camera_spec=deepcopy(spec);env.capture_frame();initial=deepcopy(env.camera_spec)
        env.advance(20);before=env.export_state();env.capture_frame()
        self.assertEqual(env.export_state(),before)
        self.assertEqual(env.camera_spec,env.camera_specs['wrist'])
        self.assertNotEqual(initial['worldFromCamera'],env.camera_spec['worldFromCamera'])
        np.testing.assert_allclose(env.camera_spec['worldFromCamera'],
            sim.recording_camera_pose(env.world,spec,state=before,stage=env.stage),atol=1e-12,rtol=0)
        self.assertNotIn('sha256',env.camera_spec);self.assertNotIn('index',env.camera_spec)
        self.assertEqual(env.latest_camera['cameraId'],'wrist')

    def test_optional_sensor_exception_stops_only_its_prefix(self):
        env = self.fixture();env.capture_frame();env.advance(20)
        sensor = env.camera_resources['wrist']['sensor'].get_data
        sensor.side_effect = RuntimeError('CPU optional sensor failed')
        env.capture_frame()
        self.assertEqual(env.camera_resources['wrist']['frameCount'],1)
        self.assertIn('wrist',env.camera_failures)
        self.assertEqual(env.latest_camera_frames['wrist']['status'],'unavailable')
        calls = sensor.call_count;env.advance(40);env.capture_frame();self.assertEqual(sensor.call_count,calls)
        self.assertEqual(env.camera_resources['front']['frameCount'],3)

    def test_sensor_failure_preserves_typed_raw_array_diagnostics(self):
        env=self.fixture();env.state_value['streams']['q']=np.zeros(100,dtype=np.float64)
        env.io['streams']['q'].update(shape=[100],units=['rad']*100)
        env.camera_resources['wrist']['sensor'].get_data.side_effect=RuntimeError('native sensor failed')
        env.capture_frame()
        import json
        failure=json.loads((env.directory/'camera-failure.json').read_text())
        self.assertEqual(failure['cameraFailures']['wrist'],'RuntimeError: native sensor failed')
        self.assertEqual(failure['before']['physicsStep'],0)
        from runtime.records import read_array
        np.testing.assert_array_equal(read_array(env.directory,failure['before']['streams']['q']),np.zeros(100))
        self.assertFalse(env.sensor_directory.exists())

    def test_optional_timeout_does_not_hide_renderer_advancing_physics(self):
        env = self.fixture();env.capture_frame();env.advance(20)
        env.camera_resources['wrist']['sensor'].get_data.return_value = (None,None)
        count = [0]
        def render():
            count[0] += 1
            if count[0] == 2:
                env.state_value['physicsStep'] += 1
                env.state_value['time'] += .002
                env.state_value['simulationTime'] += .002
        env.render.side_effect = render
        with patch.object(sim.time,'monotonic',side_effect=[0,1,2,91]), self.assertRaisesRegex(RuntimeError,'rendering|physical'):
            env.capture_frame()

    def test_same_tick_renderer_mutation_is_checked_against_fresh_native_values(self):
        env=self.fixture();del env.export_state
        env.camera_specs.pop('wrist');env.camera_resources.pop('wrist')
        env.recording['cameras']=[row for row in env.recording['cameras'] if row['cameraId']=='front']
        env._stream_cache={}
        def read_source(source, *, cache=None):
            field={'joint_positions':'q','joint_velocities':'dq','rigid_pose':'hand'}[source['reader']]
            return np.asarray(env.state_value['streams'][field],dtype=np.float64)
        env._read_source=read_source
        backend=ModuleType('isaacsim.core.experimental.utils.backend')
        backend.use_backend=lambda *args,**kwargs:nullcontext()
        env.render.side_effect=lambda:env.state_value['streams']['q'].__setitem__(0,.25)
        with patch.dict(sys.modules,{'isaacsim.core.experimental.utils.backend':backend}), \
             self.assertRaisesRegex(RuntimeError,'changed physical'):
            env.capture_frame()

    def test_real_exporter_keeps_multiview_frame_metadata_separate_from_physical_state(self):
        env=self.fixture();del env.export_state
        env._stream_cache={}
        def read_source(source, *, cache=None):
            field={'joint_positions':'q','joint_velocities':'dq','rigid_pose':'hand'}[source['reader']]
            return np.asarray(env.state_value['streams'][field],dtype=np.float64)
        env._read_source=read_source
        backend=ModuleType('isaacsim.core.experimental.utils.backend')
        backend.use_backend=lambda *args,**kwargs:nullcontext()
        with patch.dict(sys.modules,{'isaacsim.core.experimental.utils.backend':backend}):
            env.capture_frame()
        self.assertEqual(len(env.frames),2)
        self.assertEqual(env.state_value['streams']['q'],[0]*9)
        self.assertEqual(env._clock()['physicsStep'],0)

    def test_pixel_publication_never_includes_an_uncommitted_future_frame(self):
        env = self.fixture();env.capture_frame()
        recorder = Recorder(env.directory,io=env.io,recording=env.recording,task=asdict(env.task));env.recorder = recorder
        self.addCleanup(recorder.close,'interrupted')
        initial = {'observation':self.observation(env,0),'truth':self.observation(env,0,True)}
        env._publish_policy_frames();self.assertFalse(env.sensor_directory.exists())
        recorder.begin(initial,RubricFixture());env._publish_policy_frames()
        self.assertTrue((env.sensor_directory/'frames/front/000000.png').is_file())
        self.assertFalse((env.sensor_directory/'frames/wrist/000000.png').exists())
        env.advance(20);env.capture_frame()
        try:env._publish_policy_frames()
        except (ValueError,RuntimeError):pass
        self.assertFalse((env.sensor_directory/'frames/front/000001.png').exists())

    def test_array_publication_uses_only_the_accepted_observation_not_a_future_source_file(self):
        from runtime.records import validate_streams, read_array
        env=self.fixture();env.capture_frame()
        env.io['streams']['visible_points']={'source':{'reader':'particle_state','path':'/World/points','field':'positions'},
            'dtype':'float32','shape':[None,3],'units':'m','frame':'world','origin':'native_output',
            'rateHz':50,'visibleToPolicy':True,'required':True}
        points=np.arange(210,dtype=np.float32).reshape(70,3)
        env.state_value['streams']['visible_points']=points
        env.state_value['streamStatus']['visible_points']={'status':'available','sampleTime':0.,'physicsStep':0}
        recorder=Recorder(env.directory,io=env.io,recording=env.recording,task=asdict(env.task));env.recorder=recorder
        self.addCleanup(recorder.close,'interrupted')
        recorder.begin({'observation':self.observation(env,0),'truth':self.observation(env,0,True)},RubricFixture())
        env._publish_policy_frames()
        accepted=recorder.committed_observation['streams']['visible_points']
        self.assertTrue((env.sensor_directory/accepted['ref']).is_file())
        self.assertFalse(read_array(env.sensor_directory,accepted).flags.writeable)
        env.advance(10)
        env.state_value['streams']['visible_points']=points+1
        env.state_value['streamStatus']['visible_points']={'status':'available','sampleTime':.02,'physicsStep':10}
        future=validate_streams(self.observation(env,.02),env.io,directory=env.directory,write=True)['streams']['visible_points']
        self.assertTrue((env.directory/future['ref']).is_file())
        env._publish_policy_frames()
        self.assertFalse((env.sensor_directory/future['ref']).exists())

    def test_camera_cleanup_error_still_attempts_to_close_the_owned_stage(self):
        env = self.fixture()
        for resource in env.camera_resources.values():
            resource.update(reference=SimpleNamespace(detach=Mock()),referenceAttached=True,
                read_time=SimpleNamespace(detach=Mock()),readTimeAttached=True,
                sensor=SimpleNamespace(detach_annotators=Mock(),_invalidate_sensor=Mock()),writer=None)
        env.camera_resources['front']['writer'] = SimpleNamespace(close=Mock(side_effect=OSError('CPU encoder close failed')))
        with patch.object(sim,'_close_stage') as close_stage:
            with self.assertRaises(OSError):env.close()
        close_stage.assert_called_once()

    def test_close_releases_sensor_mount_even_when_encoder_close_fails(self):
        env = self.fixture();env._closed = False
        env.sensor_directory.mkdir();(env.sensor_directory/'retained.png').write_bytes(b'CPU fixture only')
        for resource in env.camera_resources.values():
            resource.update(reference=SimpleNamespace(detach=Mock()),referenceAttached=True,
                read_time=SimpleNamespace(detach=Mock()),readTimeAttached=True,
                sensor=SimpleNamespace(detach_annotators=Mock(),_invalidate_sensor=Mock()),writer=None)
        env.camera_resources['front']['writer'] = SimpleNamespace(close=Mock(side_effect=OSError('CPU encoder close failed')))
        with patch.object(sim,'_close_stage'),patch.object(sim,'_usd_world',return_value=np.eye(4)):
            with self.assertRaises(OSError):env.close()
        self.assertFalse(env.sensor_directory.exists())

    def test_open_abort_also_releases_a_partially_published_sensor_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            def partial_open(env,*args):
                env.stage=object();env.sensor_directory.mkdir()
                (env.sensor_directory/'partial.png').write_bytes(b'CPU committed frame fixture')
                raise RuntimeError('CPU failure after reset pixel publication')
            with patch.object(sim.Environment,'_open_native',partial_open),patch.object(sim,'_close_stage') as close_stage:
                with self.assertRaisesRegex(RuntimeError,'CPU failure'):
                    sim.Environment({'io':deepcopy(BASE_IO)},World(native=True),output,Mock(),record=False)
            close_stage.assert_called_once()
            self.assertFalse((output/'policy-sensors').exists())
