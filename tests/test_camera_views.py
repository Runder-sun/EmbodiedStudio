import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.scene import Task, World


def camera_task(id, object_id, *, parameters=None, bindings=None):
    return Task(id, bindings=bindings or {'subject': object_id}, parameters=parameters or {},
                rubric={'sourceRef':'rubrics.py','helperRefs':[]})


def publish_camera_episode(root, episode, manifest):
    """SYSTEM_VERIFICATION_ONLY publication records; no simulator execution."""
    from runtime import worker
    project_id=episode.parent.parent.name;job_id='camera_fixture_'+episode.name
    reference=str((episode/'manifest.json').relative_to(root))
    manifest={**manifest,'projectId':project_id,'episodeId':episode.name,'artifacts':{'manifest':reference}}
    worker.save(episode/'manifest.json',manifest)
    result={'projectId':project_id,'revision':manifest['revision'],'episodes':[{
        'episode':episode.name,'taskId':manifest['taskId'],'artifacts':manifest['artifacts'],'score':{'success':False}}]}
    result=worker.finalize({'operation':'rollout','root':str(root),'projectId':project_id,'jobId':job_id},result)
    job=root/'jobs'/job_id;job.mkdir(parents=True)
    worker.save(job/'status.json',{'jobId':job_id,'state':'completed'});worker.save(job/'result.json',result)
    worker.validate_publication(json.loads((episode/'manifest.json').read_text()),root)


class CameraFramingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU geometry dependency unavailable: {exc.name}')
        cls.simulation = simulation

    def test_task_view_keeps_robot_and_goal_readable_in_large_building(self):
        world = World()
        world.space('building', bounds={'min': [-20, -15, 0], 'max': [20, 15, 8]})
        world.box('cube', [.055] * 3, at=(.5, -.1, .8), parent='building')
        task = camera_task('push', 'cube', parameters={'goal':[.5,.15,.8]})
        task.bind(world)
        whole = self.simulation.camera_for(world)
        focused = self.simulation.camera_for(world, task=task)
        self.assertLess(focused['distance'], 3.5)
        self.assertLess(focused['distance'], whole['distance'] / 5)
        self.assertLess(abs(focused['lookat'][0]), 1)

    def test_node_view_uses_owned_descendants_even_without_space_bounds(self):
        world = World()
        world.space('room', at=(30, 0, 0))
        world.box('floor', (4, 3, .1), parent='room')
        world.box('chair', (1, 1, 1), at=(1, 0, .6), parent='room')
        focused = self.simulation.camera_for(world, scope='room')
        self.assertAlmostEqual(focused['lookat'][0], 30)
        self.assertLess(focused['distance'], 7)

    def test_node_view_does_not_expand_to_distant_robot(self):
        world = World()
        world.space('room', at=(30, 0, 0), bounds={'min': [-2, -1.5, 0], 'max': [2, 1.5, 3]})
        focused = self.simulation.camera_for(world, scope='room')
        self.assertAlmostEqual(focused['lookat'][0], 30)
        self.assertLess(focused['distance'], 7)

    def test_environment_honors_explicit_camera_without_changing_compiled_camera(self):
        compiled = {'camera':{'scope':'whole_world','lookat':[0,0,0]},
            'io':{'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}}
        original = copy.deepcopy(compiled)
        camera = {'scope':'room','lookat':[30,0,1]}
        selected=[]
        def stopped_open(environment, discover):
            selected.append(copy.deepcopy(environment.camera_spec))
            raise RuntimeError('SYSTEM_VERIFICATION_ONLY stopped before SDK')
        with tempfile.TemporaryDirectory() as temporary, patch.object(self.simulation.Environment,'_open_native',stopped_open):
            with self.assertRaisesRegex(RuntimeError,'stopped before SDK'):
                self.simulation.Environment(compiled,World(native=True),temporary,
                    lambda *a,**k:None,camera=camera,record=False)
        self.assertEqual(selected,[camera])
        self.assertEqual(compiled,original)

    def test_node_view_uses_ownership_and_rotated_size_corners(self):
        world = World()
        world.space('room')
        world.space('unrelated_frame', at=(100, 0, 0))
        world.box('long', (4, 1, 1), owner='room', frame=None, yaw=math.pi/2)
        world.box('small', (1, 1, 1), at=(3, 0, 0), owner='room', frame=None)
        focused = self.simulation.camera_for(world, scope='room')
        self.assertAlmostEqual(focused['lookat'][0], 1.5)
        self.assertAlmostEqual(focused['distance'], math.sqrt(33)*1.1)

    def test_task_parameters_do_not_replace_actual_bound_frame_geometry(self):
        world = World()
        world.space('goal_frame', at=(.2, .4, .7), yaw=math.pi/2)
        world.box('cube', [.055]*3, at=(.5, 0, .8))
        task = camera_task('push', 'cube', bindings={'subject':'cube','reference':{'frame':'goal_frame'}}, parameters={'goal':[.3,0,0], 'tolerance':[1,.05,.02]})
        task.bind(world)
        focused = self.simulation.camera_for(world, task=task)
        task.parameters = {'goal':[9999]*3, 'tolerance':[9999]*3}
        self.assertEqual(self.simulation.camera_for(world, task=task), focused)

    def test_joint_goal_is_not_mistaken_for_xyz_position(self):
        world = World()
        world.box('drawer', (.1, .1, .1), at=(.5, 0, .8), joint={'type': 'slide', 'range': [0, 10000]})
        task = camera_task('open', 'drawer', bindings={'subject':{'objectId':'drawer','joint':True}}, parameters={'goal':9999})
        task.bind(world)
        self.assertLess(self.simulation.camera_for(world, task=task)['distance'], 3.5)

    def test_task_video_preserves_walls_slabs_roof_and_stairs(self):
        world = World()
        world.space('building', bounds={'min': [-8, -5, 0], 'max': [9, 3, 6]})
        world.space('room', owner='building', bounds={'min': [-2, -1, 0], 'max': [3, 3, 3]})
        world.box('wall', (5, .12, 2.88), at=(.5, 3, 1.46), owner='room', role='enclosure')
        world.box('floor', (5, 4, .12), at=(.5, 1, -.04), owner='room', role='enclosure')
        world.box('upper_slab', (5, 4, .12), at=(.5, 1, 2.96), owner='building', role='enclosure')
        world.box('roof', (5, 4, .12), at=(.5, 1, 6), owner='building', role='enclosure')
        world.box('stairs', (.32, 1.35, 2), at=(4, -3.6, 1), owner='building', role='enclosure')
        world.box('cube', [.055]*3, at=(.48, -.1, .758), owner='room', mass=.06)
        task = camera_task('push', 'cube', parameters={'goal':[.48,.12,.758]})
        task.bind(world)
        original, binding = world.snapshot(), copy.deepcopy(task.bindings)
        focused = self.simulation.camera_for(world, task=task)
        self.assertEqual(focused['hiddenVisualNodeIds'], [])
        self.assertEqual(focused['viewMode'], 'full_geometry')
        self.assertEqual(focused['scope'], 'task:push')
        self.assertLess(focused['distance'], 3.5)
        self.assertEqual(world.snapshot(), original)
        self.assertEqual(task.bindings, binding)
        diagnostic = self.simulation.camera_for(world, scope='room')
        self.assertIn('wall', diagnostic['hiddenVisualNodeIds'])
        self.assertEqual(diagnostic['viewMode'], 'visual_cutaway')
        self.assertEqual(world.snapshot(), original)

    def test_cutaway_only_hides_explicit_static_enclosures(self):
        world = World()
        world.space('room', bounds={'min': [-2, -2, 0], 'max': [2, 2, 3]})
        world.box('wall', (4, .1, 3), at=(0, 2, 1.5), parent='room', role='enclosure')
        world.box('floor', (4, 4, .1), parent='room', role='enclosure')
        world.box('roof_without_role', (4, 4, .1), at=(0, 0, 3), parent='room')
        world.box('dynamic_enclosure', (.1, .1, .1), at=(0, 0, 3), parent='room', role='enclosure', mass=1)
        world.box('moving_enclosure', (.1, .1, .1), at=(0, 0, 3), parent='room', role='enclosure',
                  joint={'type': 'slide', 'range': [0, .1]})
        original = world.snapshot()
        focused = self.simulation.camera_for(world, scope='room')
        self.assertEqual(focused['hiddenVisualNodeIds'], ['wall'])
        self.assertEqual(world.snapshot(), original)


class ObservationScopeTests(unittest.TestCase):
    def fixture(self, root):
        world = World()
        world.space('room', bounds={'min': [-2, -2, 0], 'max': [2, 2, 3]})
        world.box('cube', [.055] * 3, at=(.5, 0, .8), parent='room')
        task = camera_task('push', 'cube', parameters={'goal':[.5,.15,.8]})
        manifest = {'revision': 'rev', 'tasks': [task.bind(world)], 'camera': {'scope': 'whole_world'}}
        project = root / 'projects' / 'project'
        directory = project / 'revisions' / 'rev'
        directory.mkdir(parents=True)
        (directory/'scene-source.usda').write_text('#usda 1.0\ndef Xform "World" {}\n')
        manifest['compiled'] = {'usd':'scene-source.usda'}
        return project, directory, manifest, world

    def test_scene_observation_does_not_claim_requested_scope_from_whole_camera(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.fixture(root)
            scratch = fixture[1] / 'observations' / 'scratch'
            (scratch / 'frames').mkdir(parents=True)
            (scratch / 'frames' / '000000.png').write_bytes(b'camera pixels')

            class WholeCamera:
                camera_spec = {'scope': 'whole_world'}
                latest_camera = {'path': 'frames/000000.png', 'cameraScope': 'whole_world',
                    'worldFromCamera':[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
                    'intrinsics':{}, 'coordinateSpec':'SYSTEM_VERIFICATION_ONLY camera double'}

                def capture_frame(self):
                    return {'path': 'frames/000000.png', 'cameraScope': 'whole_world'}

                def close(self):
                    pass

            with patch.object(worker, 'frozen', return_value=fixture), \
                 patch.object(worker, 'load_environment', return_value=WholeCamera()):
                result = worker.observe({'projectId': 'project', 'revision': 'rev', 'nodeScope': 'room'}, root)
            if result['cameraScope'] in ('whole_world', 'frozen_whole_world'):
                self.assertNotEqual(result['checkedScope'], 'room')

    def test_scene_observation_reads_accepted_constructor_frame_without_rendering_again(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self.fixture(root)
            scratch = fixture[1] / 'observations' / 'scratch'
            (scratch / 'frames').mkdir(parents=True)
            pixels = b'accepted constructor camera pixels'
            (scratch / 'frames' / '000000.png').write_bytes(pixels)

            class AcceptedCamera:
                camera_spec = {'scope': 'room', 'primPath': '/World/camera'}
                latest_camera = {'path': 'frames/000000.png', 'cameraScope': 'room', 'physicsStep': 0,
                    'rawReferenceTime': {'numerator': 104000000, 'denominator': 1000000000},
                    'worldFromCamera':[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
                    'intrinsics':{}, 'coordinateSpec':'SYSTEM_VERIFICATION_ONLY camera double'}

                def capture_frame(self):
                    raise AssertionError('duplicate reference at unchanged physical time')

                def step(self, *args):
                    raise AssertionError('observation must not advance physical state')

                def close(self):
                    pass

            with patch.object(worker, 'frozen', return_value=fixture), \
                 patch.object(worker, 'load_environment', return_value=AcceptedCamera()):
                result = worker.observe({'projectId': 'project', 'revision': 'rev', 'nodeScope': 'room'}, root)
            self.assertEqual((root / result['artifactRef']).read_bytes(), pixels)
            self.assertEqual(result['checkedScope'], 'rendered_camera_frame')
            saved = json.loads((root / result['artifactRef']).with_suffix('.camera.json').read_text())
            self.assertEqual(saved['physicsStep'], 0)
            self.assertEqual(saved['rawReferenceTime'], AcceptedCamera.latest_camera['rawReferenceTime'])

    def test_episode_observation_reports_its_recorded_camera_scope(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = self.fixture(root)
            episode = fixture[0] / 'episodes' / 'episode'
            episode.mkdir(parents=True)
            (episode / 'frame.png').write_bytes(b'recorded task camera pixels')
            recording = {'primaryCameraId':'front','cameras':[{'cameraId':'front','frame':'world','at':[0,0,1],
                'orientation':[1,0,0,0],'resolution':[32,32],'rateHz':25,'modalities':['rgb'],
                'video':False,'required':True,'source':'simulation_observer'}]}
            publish_camera_episode(root,episode,{'revision':'rev','taskId':'push','recording':recording})
            (episode / 'trajectory.json').write_text(json.dumps({'frames': [{'path': 'frame.png','cameraId':'front','index':0,
                'sha256':worker.digest(episode/'frame.png'),
                'cameraScope': 'task:push','cameraPrimPath':'/camera/front','worldFromCamera':[[1,0,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]],
                'intrinsics':{'fx':1,'fy':1,'cx':1,'cy':1},'coordinateSpec':{'fixture':'CPU'}}]}))
            with patch.object(worker, 'frozen', return_value=fixture):
                result = worker.observe({'projectId': 'project', 'revision': 'rev', 'episode': 'episode'}, root)
            self.assertEqual(result['cameraScope'], 'task:push')

    def test_recording_cameras_are_not_overridden_by_preview_framing(self):
        from runtime import worker
        try:
            from runtime import simulation
        except ModuleNotFoundError as exc:
            self.skipTest(f'CPU geometry dependency unavailable: {exc.name}')
        from types import SimpleNamespace
        world = World()
        world.box('cube', [.055]*3)
        world.box('roof', (5, 4, .12), at=(.5, 1, 3), role='enclosure')
        task = camera_task('push', 'cube')
        task.bind(world)
        task_camera = {**simulation.camera_for(world, task=task),
                                  'worldFromCamera': [[1, 0, 0, 2]]}
        manifest = {'camera': {'scope':'whole_world'},'io':{'schema':'sceneagent-native-io-v1'},
            'compiled': {'camera': {'scope':'whole_world'},'recordingCameras':{task.id:{'front':task_camera}}}}
        original = copy.deepcopy(task.bindings)
        def environment(compiled, world, output, progress, camera, record, task, source_directory, **kwargs):
            return SimpleNamespace(camera_spec=camera,compiled=compiled)
        with patch.object(simulation, 'Environment', side_effect=environment):
            for record in (True, False):
                loaded = worker.load_environment(Path('/frozen'), manifest, world, Path('/output'), task, record=record,
                    camera=None if record else task_camera)
                self.assertEqual(loaded.camera_spec, None if record else task_camera)
                self.assertEqual(loaded.compiled['recordingCameras'][task.id]['front'],task_camera)
                if not record:
                    self.assertEqual(loaded.camera_spec['hiddenVisualNodeIds'], [])
                    self.assertEqual(loaded.camera_spec['viewMode'], 'full_geometry')
        self.assertEqual(task.bindings, original)


class ObservationNativeSelectionTests(unittest.TestCase):
    """SYSTEM_VERIFICATION_ONLY real PNGs with a renderer double; no SDK execution."""
    def fixture(self,root,*,real_usd=False):
        from types import SimpleNamespace
        from runtime import simulation as sim,worker
        from PIL import Image
        world=World();world.space('room')
        if real_usd:world.bind('cube','/World/native_cube',owner='room')
        else:world.box('cube',[.1]*3,parent='room')
        def camera(id,at):
            return {'cameraId':id,'frame':'world','at':at,'orientation':(
                [.8726087337115855,.41440512596212475,.11089295695941749,.23350619161678698] if real_usd else [1,0,0,0]),
                'resolution':[32,16],'rateHz':25,'modalities':['rgb'],'video':False,'required':True,'source':'simulation_observer'}
        first=Task('first',rubric={'sourceRef':'rubrics.py','helperRefs':[]},
            recording={'cameras':[camera('front',[1,0,1]),camera('side',[0,1,1])],'primaryCameraId':'front'})
        second=Task('second',rubric={'sourceRef':'rubrics.py','helperRefs':[]},
            recording={'cameras':[camera('front',[2,0,1]),camera('second_only',[0,2,1])],'primaryCameraId':'second_only'})
        def lens(stage,spec):
            width,height=spec['width'],spec['height']
            return {**spec,'primPath':spec.get('primPath','/World/camera'),
                'worldFromCamera':spec.get('worldFromCamera',[[1,0,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]]),
                'intrinsics':{'fx':width*24/36,'fy':width*24/36,'cx':width/2,'cy':height/2},
                'coordinateSpec':'metres; USD camera -Z forward/+Y up; column vectors'}
        if real_usd:
            from pxr import Gf,Usd,UsdGeom
            # Real frozen geometry and camera math; the sensor boundary remains a test double.
            original_apply=Usd.Prim.ApplyAPI
            def apply(prim, schema, *args):
                return True if schema=='OmniSensorAPI' else original_apply(prim,schema,*args)
            self.enterContext(patch.object(Usd.Prim,'ApplyAPI',apply))
            stage=Usd.Stage.CreateInMemory()
            cube=UsdGeom.Cube.Define(stage,'/World/native_cube');cube.CreateSizeAttr(1.)
            cube.AddTranslateOp().Set(Gf.Vec3d(2.5,-1.25,.8))
            cube.AddScaleOp().Set(Gf.Vec3d(4,1,2))
            preview=sim._write_camera(stage,sim.camera_for(world,stage=stage))
            compiled={'camera':preview,'recordingCameras':{task.id:sim.compile_recording_cameras(stage,world,task) for task in (first,second)}}
        else:
            with patch.object(sim,'_write_camera',side_effect=lens):
                preview=lens(None,sim.camera_for(world))
                compiled={'camera':preview,'recordingCameras':{task.id:sim.compile_recording_cameras(None,world,task) for task in (first,second)}}
        manifest={'revision':'rev','tasks':[first.bind(world),second.bind(world)],'camera':preview,'compiled':compiled}
        project=root/'projects/p1';directory=project/'revisions/rev';directory.mkdir(parents=True)
        compiled['usd']='scene-source.usda'
        if real_usd:
            stage.GetRootLayer().Export(str(directory/compiled['usd']))
        else:
            (directory/compiled['usd']).write_text('#usda 1.0\ndef Xform "World" {}\n')
        self.enterContext(patch.object(worker,'frozen',return_value=(project,directory,manifest,world)))
        calls=[]
        def load(directory,manifest,world,output,task,record,camera):
            calls.append((task.id,copy.deepcopy(camera)))
            selected=copy.deepcopy(camera);camera_id=selected.get('cameraId','preview')
            if real_usd:
                selected=sim._apply_camera_view(stage,selected,{'objectPaths':{}})
            selected.setdefault('primPath','/World/camera')
            width,height=selected.get('resolution',[selected['width'],selected['height']])
            selected.setdefault('intrinsics',{'fx':width*24/36,'fy':width*24/36,'cx':width/2,'cy':height/2})
            selected.setdefault('worldFromCamera',[[1,0,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]])
            selected.setdefault('coordinateSpec','metres; USD camera -Z forward/+Y up; column vectors')
            path=output/f'frames/{camera_id}/000000.png';path.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(width,height),(40,20,10)).save(path)
            frame={'path':str(path.relative_to(output)),'sha256':worker.digest(path),'cameraId':camera_id,
                'index':0,'frameIndex':0,'cameraScope':selected['scope'],'cameraPrimPath':selected['primPath'],
                'worldFromCamera':sim._usd_world(stage,selected['primPath']).tolist() if real_usd else selected['worldFromCamera'],
                'intrinsics':selected['intrinsics'],
                'coordinateSpec':selected['coordinateSpec']}
            return SimpleNamespace(latest_camera=frame,camera_spec=selected,close=lambda:None)
        self.enterContext(patch.object(worker,'load_environment',side_effect=load))
        return project,directory,manifest,world,calls

    def payload(self,root,**fields):
        return {'operation':'observe','root':str(root),'projectId':'p1','revision':'rev',**fields}

    def batch_fixture(self,root,*,missing=None,clock_change=False):
        from types import SimpleNamespace
        from runtime import worker
        fixture=self.fixture(root,real_usd=True);single=worker.load_environment.side_effect
        def load(directory,manifest,world,output,task,record,camera=None,*,cameras=None):
            frames,specs={},{}
            for index,(name,camera) in enumerate(cameras.items()):
                env=single(directory,manifest,world,output,task,record,camera)
                frame=env.latest_camera
                frame.update(index=index,time=0.,simulationTime=1.,timeOrigin=1.,physicsStep=0,stageId=17)
                if clock_change and index:frame['physicsStep']=1
                if name!=missing:frames[name]=frame
                specs[name]=env.camera_spec
            return SimpleNamespace(latest_camera_frames=frames,camera_specs=specs,close=lambda:None)
        loader=self.enterContext(patch.object(worker,'load_environment',side_effect=load))
        return fixture,loader

    def test_batch_views_open_once_and_keep_every_camera_bound_to_its_frozen_source(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();_,loader=self.batch_fixture(root)
            payload=self.payload(root,cameraIds=['side','front']);result=worker.observe(payload,root)
            self.assertEqual(loader.call_count,1)
            self.assertEqual(result['cameraIds'],['side','front'])
            self.assertEqual([row['cameraId'] for row in result['observations']],['side','front'])
            self.assertEqual(len(worker.required_files(payload,result)),4)
            changed=copy.deepcopy(result);changed['observations'][0]['cameraId']='front'
            with self.assertRaises(ValueError):worker.required_files(payload,changed)
            changed=copy.deepcopy(result);changed['observations'].pop()
            with self.assertRaises(ValueError):worker.required_files(payload,changed)

    def test_batch_selection_rejects_empty_duplicates_conflicting_scope_and_foreign_task(self):
        from runtime import worker
        invalid=[{'cameraIds':[]},{'cameraIds':['front','front']},{'cameraIds':['front']*5},
            {'cameraIds':'front'},{'cameraIds':[None]},{'cameraIds':['front'],'cameraId':'front'},
            {'cameraIds':['front'],'nodeId':'room'},{'cameraIds':['front'],'nodeScope':'room'},
            {'cameraIds':['front'],'episode':'e1'},{'cameraIds':['front'],'frame':0},
            {'cameraIds':['second_only']}]
        for fields in invalid:
            with tempfile.TemporaryDirectory() as name,self.subTest(fields=fields):
                root=Path(name).resolve();_,loader=self.batch_fixture(root)
                with self.assertRaises(ValueError):worker.observe(self.payload(root,**fields),root)
                loader.assert_not_called()

    def test_batch_rejects_missing_frames_and_inconsistent_physical_clocks(self):
        from runtime import worker
        for fields in ({'missing':'front'},{'clock_change':True}):
            with tempfile.TemporaryDirectory() as name,self.subTest(fields=fields):
                root=Path(name).resolve();self.batch_fixture(root,**fields)
                with self.assertRaises(ValueError):worker.observe(self.payload(root,cameraIds=['side','front']),root)

    def test_batch_parent_rejects_later_clock_or_pixel_tampering(self):
        from runtime import worker
        for change in ('clock','pixels'):
            with tempfile.TemporaryDirectory() as name,self.subTest(change=change):
                root=Path(name).resolve();self.batch_fixture(root)
                payload=self.payload(root,cameraIds=['side','front']);result=worker.observe(payload,root)
                path=root/result['observations'][1]['artifactRef']
                if change=='clock':
                    sidecar=path.with_suffix('.camera.json');frame=json.loads(sidecar.read_text())
                    frame['stageId']+=1;sidecar.write_text(json.dumps(frame))
                else:path.write_bytes(b'changed pixels')
                with self.assertRaises(ValueError):worker.required_files(payload,result)

    def test_default_and_explicit_camera_use_the_first_tasks_frozen_views(self):
        from runtime import worker
        for selected in (None,'side'):
            with tempfile.TemporaryDirectory() as name,self.subTest(selected=selected):
                root=Path(name).resolve();_,_,manifest,_,calls=self.fixture(root)
                payload=self.payload(root,**({'cameraId':selected} if selected else {}))
                result=worker.observe(payload,root)
                self.assertEqual(result['cameraId'],selected or 'front')
                self.assertEqual(result['cameraScope'],'task:first')
                self.assertEqual(calls[-1][0],'first')
                self.assertEqual(result['cameraPrimPath'],manifest['compiled']['recordingCameras']['first'][selected or 'front']['primPath'])
                self.assertTrue(worker.required_files(payload,result))

    def test_real_usd_oblique_primary_and_node_roundtrip_preserve_parent_source_checks(self):
        import numpy as np
        from runtime import worker
        for scope in (None,'room'):
            with tempfile.TemporaryDirectory() as name,self.subTest(scope=scope):
                root=Path(name).resolve();self.fixture(root,real_usd=True)
                payload=self.payload(root,**({'nodeId':scope} if scope else {}))
                result=worker.observe(payload,root)
                frame=json.loads((root/result['artifactRef']).with_suffix('.camera.json').read_text())
                difference=np.max(np.abs(np.asarray(result['camera']['worldFromCamera'])-frame['worldFromCamera']))
                self.assertEqual(difference,0)
                self.assertEqual(result['camera']['intrinsics'],frame['intrinsics'])
                self.assertTrue(worker.required_files(payload,result))

    def test_live_camera_metadata_reports_measured_sensor_precision(self):
        import numpy as np
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();_,_,manifest,_,_=self.fixture(root,real_usd=True)
            frozen=copy.deepcopy(manifest['compiled']['recordingCameras'])
            original=worker.load_environment.side_effect
            def sensor_precision(*args,**kwargs):
                environment=original(*args,**kwargs)
                environment.latest_camera['worldFromCamera']=np.asarray(
                    environment.latest_camera['worldFromCamera'],dtype=np.float32).astype(float).tolist()
                return environment
            worker.load_environment.side_effect=sensor_precision
            payload=self.payload(root);result=worker.observe(payload,root)
            frame=json.loads((root/result['artifactRef']).with_suffix('.camera.json').read_text())
            self.assertEqual(result['camera']['worldFromCamera'],frame['worldFromCamera'])
            self.assertEqual(manifest['compiled']['recordingCameras'],frozen)
            self.assertTrue(worker.required_files(payload,result))

    def test_measured_world_pose_cannot_replace_the_frozen_camera_source(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();self.fixture(root,real_usd=True)
            payload=self.payload(root);result=worker.observe(payload,root)
            sidecar=(root/result['artifactRef']).with_suffix('.camera.json')
            frame=json.loads(sidecar.read_text());frame['worldFromCamera'][0][3]+=.01
            result['camera']['worldFromCamera']=copy.deepcopy(frame['worldFromCamera'])
            sidecar.write_text(json.dumps(frame))
            with self.assertRaisesRegex(ValueError,'frozen source'):
                worker.required_files(payload,result)

    def test_native_node_roundtrip_uses_actual_frozen_geometry_for_both_scope_spellings(self):
        import numpy as np
        from runtime import simulation,worker
        for fields in ({'nodeId':'cube'},{'nodeScope':'room'}):
            with tempfile.TemporaryDirectory() as name,self.subTest(fields=fields):
                root=Path(name).resolve();_,directory,_,world,calls=self.fixture(root,real_usd=True)
                payload=self.payload(root,**fields);source=(directory/'scene-source.usda').read_bytes()
                self.assertTrue(world.native);self.assertEqual(world.nodes['cube']['primPath'],'/World/native_cube')
                result=worker.observe(payload,root)
                np.testing.assert_allclose(result['camera']['lookat'],[2.5,-1.25,.8],atol=1e-7,rtol=0)
                self.assertAlmostEqual(result['camera']['distance'],math.sqrt(21)*1.1)
                path=root/result['artifactRef']
                self.assertEqual(worker.required_files(payload,result),{path,path.with_suffix('.camera.json')})
                self.assertEqual(len(calls),1);self.assertIsNone(simulation._app)
                self.assertEqual((directory/'scene-source.usda').read_bytes(),source)

    def test_native_node_parent_keeps_source_scope_pose_cutaway_and_pixel_guards(self):
        from runtime import worker
        for change in ('scope','lookat','distance','viewMode','hiddenVisualNodeIds','pose','pixels'):
            with tempfile.TemporaryDirectory() as name,self.subTest(change=change):
                root=Path(name).resolve();self.fixture(root,real_usd=True)
                payload=self.payload(root,nodeId='cube');result=worker.observe(payload,root)
                self.assertTrue(worker.required_files(payload,result))
                if change=='scope':result['nodeScope']='room'
                elif change=='lookat':result['camera']['lookat'][0]+=.1
                elif change=='distance':result['camera']['distance']+=.1
                elif change=='viewMode':result['camera']['viewMode']='visual_cutaway'
                elif change=='hiddenVisualNodeIds':result['camera']['hiddenVisualNodeIds']=['cube']
                elif change=='pose':result['camera']['worldFromCamera'][0][3]+=.1
                else:(root/result['artifactRef']).write_bytes(b'changed SYSTEM_VERIFICATION_ONLY pixels')
                with self.assertRaises(ValueError):worker.required_files(payload,result)

    def test_native_node_parent_rejects_missing_frozen_prim_binding(self):
        from pxr import Usd
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();_,directory,manifest,_,_=self.fixture(root,real_usd=True)
            payload=self.payload(root,nodeId='cube');result=worker.observe(payload,root)
            stage=Usd.Stage.Open(str(directory/manifest['compiled']['usd']))
            stage.RemovePrim('/World/native_cube');stage.GetRootLayer().Save()
            with self.assertRaisesRegex(ValueError,'native camera framing requires its frozen stage binding'):
                worker.required_files(payload,result)

    def test_observation_rejects_matched_nonfinite_or_nonrigid_camera_matrices(self):
        from runtime import worker
        for change in ('nan','scale','reflection','bottom_row','shape'):
            with tempfile.TemporaryDirectory() as name,self.subTest(change=change):
                root=Path(name).resolve();self.fixture(root);payload=self.payload(root)
                result=worker.observe(payload,root);sidecar=(root/result['artifactRef']).with_suffix('.camera.json')
                frame=json.loads(sidecar.read_text());matrix=frame['worldFromCamera']
                if change=='nan':matrix[0][0]=float('nan')
                elif change=='scale':matrix[0][0]=1.01
                elif change=='reflection':matrix[0][0]=-1
                elif change=='bottom_row':matrix[3][3]=.9
                else:matrix.pop()
                sidecar.write_text(json.dumps(frame));frame=json.loads(sidecar.read_text())
                result['camera']['worldFromCamera']=copy.deepcopy(frame['worldFromCamera'])
                with self.assertRaises(ValueError):worker.required_files(payload,result)

    def test_observation_pose_tolerance_does_not_replace_source_identity_lens_or_pixel_guards(self):
        from runtime import worker
        for change in ('pose_above_tolerance','source','camera','task','lens','pixels'):
            with tempfile.TemporaryDirectory() as name,self.subTest(change=change):
                root=Path(name).resolve();self.fixture(root);payload=self.payload(root)
                result=worker.observe(payload,root);path=root/result['artifactRef'];sidecar=path.with_suffix('.camera.json')
                frame=json.loads(sidecar.read_text())
                if change=='pose_above_tolerance':frame['worldFromCamera'][0][3]+=5e-12
                elif change=='source':result['camera']['source']='simulated_robot_sensor'
                elif change=='camera':result['cameraId']='side'
                elif change=='task':frame['cameraScope']='task:second'
                elif change=='lens':
                    frame['intrinsics']['fx']+=.001
                    result['camera']['intrinsics']=copy.deepcopy(frame['intrinsics'])
                else:path.write_bytes(b'changed CPU test pixels')
                sidecar.write_text(json.dumps(frame))
                with self.assertRaises(ValueError):worker.required_files(payload,result)

    def test_a_camera_declared_only_in_the_second_task_cannot_be_selected(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();*_,calls=self.fixture(root)
            with self.assertRaisesRegex(ValueError,'first frozen task|frozen observation camera'):
                worker.observe(self.payload(root,cameraId='second_only'),root)
            self.assertEqual(calls,[])

    def test_node_scope_keeps_preview_framing_and_rejects_forged_scope(self):
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();self.fixture(root);payload=self.payload(root,nodeScope='room')
            result=worker.observe(payload,root)
            self.assertEqual(result['cameraId'],'preview');self.assertEqual(result['cameraScope'],'room')
            self.assertTrue(worker.required_files(payload,result))
            changed=copy.deepcopy(result);changed['cameraScope']='whole_world'
            with self.assertRaisesRegex(ValueError,'observation.*(camera|scope)'):
                worker.required_files(payload,changed)

    def test_parent_rejects_same_camera_id_from_a_different_task_and_changed_resolution(self):
        import base64
        from PIL import Image
        from runtime import worker
        for mutation in ('different_task','resolution'):
            with tempfile.TemporaryDirectory() as name,self.subTest(mutation=mutation):
                root=Path(name).resolve();_,_,manifest,_,_=self.fixture(root);payload=self.payload(root,cameraId='front')
                result=worker.observe(payload,root);path=root/result['artifactRef'];sidecar=path.with_suffix('.camera.json')
                frame=json.loads(sidecar.read_text())
                if mutation == 'different_task':
                    camera=manifest['compiled']['recordingCameras']['second']['front']
                    result.update(camera=copy.deepcopy(camera),cameraScope=camera['scope'],cameraPrimPath=camera['primPath'])
                    frame.update(cameraScope=camera['scope'],cameraPrimPath=camera['primPath'],worldFromCamera=camera['worldFromCamera'])
                else:
                    Image.new('RGB',(48,16),(40,20,10)).save(path)
                    frame['sha256']=worker.digest(path)
                    result['image']['data']=base64.b64encode(path.read_bytes()).decode()
                sidecar.write_text(json.dumps(frame))
                with self.assertRaisesRegex(ValueError,'observation|observed image'):
                    worker.required_files(payload,result)

    def test_recorded_episode_camera_identity_stays_bound_to_its_own_task(self):
        from PIL import Image
        from runtime import worker
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();project,_,manifest,_,calls=self.fixture(root)
            episode=project/'episodes/e1';(episode/'frames/front').mkdir(parents=True)
            path=episode/'frames/front/000000.png';Image.new('RGB',(32,16),(40,20,10)).save(path)
            camera=manifest['compiled']['recordingCameras']['first']['front']
            receipt={'revision':'rev','taskId':'first','recording':manifest['tasks'][0]['recording']}
            frame={'index':0,'frameIndex':0,'cameraId':'front','path':'frames/front/000000.png','sha256':worker.digest(path),
                'cameraScope':camera['scope'],'cameraPrimPath':camera['primPath'],'worldFromCamera':camera['worldFromCamera'],
                'intrinsics':camera['intrinsics'],'coordinateSpec':camera['coordinateSpec']}
            publish_camera_episode(root,episode,receipt);trajectory={'frames':[frame]}
            (episode/'trajectory.json').write_text(json.dumps(trajectory))
            payload=self.payload(root,episode='e1',cameraId='front');result=worker.observe(payload,root)
            self.assertEqual(calls,[]);self.assertTrue(worker.required_files(payload,result))
            foreign=manifest['compiled']['recordingCameras']['second']['front']
            frame.update(cameraScope=foreign['scope'],cameraPrimPath=foreign['primPath'],worldFromCamera=foreign['worldFromCamera'])
            (episode/'trajectory.json').write_text(json.dumps(trajectory))
            result=worker.observe(payload,root)
            with self.assertRaisesRegex(ValueError,'observed episode camera.*frozen'):
                worker.required_files(payload,result)


class CameraSessionLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from pxr import Gf, Plug, Usd, UsdGeom, UsdPhysics
            from runtime import simulation
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f'CPU USD dependency unavailable: {exc.name}')
        import sys
        for plugin in Path(sys.prefix).glob('lib/python*/site-packages/isaacsim/extscache/omni.usd.schema.omni_sensors-*/usd_plugins'):
            Plug.Registry().RegisterPlugins(str(plugin))
        if Usd.SchemaRegistry().FindAppliedAPIPrimDefinition('OmniSensorAPI') is None:
            raise unittest.SkipTest('Official sensor USD schema unavailable; no SDK startup in CPU tests')
        cls.Gf, cls.Usd, cls.UsdGeom, cls.UsdPhysics, cls.simulation = Gf, Usd, UsdGeom, UsdPhysics, simulation

    def native_lighting_fixture(self,directory,multiple=False):
        from pxr import UsdLux
        stage=self.Usd.Stage.CreateNew(str(directory/'scene-source.usda'))
        stage.SetDefaultPrim(self.UsdGeom.Xform.Define(stage,'/World').GetPrim())
        self.UsdGeom.SetStageMetersPerUnit(stage,1);self.UsdGeom.SetStageUpAxis(stage,self.UsdGeom.Tokens.z)
        world=World(native=True,lights=[]);world.stage=stage
        world.space('room',bounds={'min':[-2.3,-2.3,.02],'max':[3,2.2,3.02]})
        world.box('floor',(5.3,4.5,.04),at=(.35,-.05,0),owner='room',role='enclosure')
        world.box('wall',(5.3,.12,3),at=(.35,2.2,1.52),owner='room',role='enclosure')
        world.box('ceiling',(5.3,4.5,.1),at=(.35,-.05,3.07),owner='room',role='enclosure')
        world.box('cube',[.055]*3,at=(.48,-.1,.758),owner='room')
        world.light('source_lamp','sphere',1200,at=(.3,-.2,2.5),radius=.1)
        if multiple:world.light('second_lamp','rect',900,at=(1.4,.2,2.4),width=.5,height=.3)
        compiled=self.simulation.author_native_helpers(world,stage,directory)
        for number in range(2 if multiple else 1):
            spec=self.simulation.camera_for(world,stage=stage)
            self.simulation._write_camera(stage,{**spec,'primPath':'/World/source_camera_'+str(number)})
        stage.GetRootLayer().Save()
        return stage,world,compiled


    def test_explicit_source_light_and_camera_survive_frozen_root_reload(self):
        import hashlib,numpy as np
        from pxr import Sdf,UsdLux
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);stage,world,compiled=self.native_lighting_fixture(directory)
            source=directory/'scene-source.usda';before=source.read_bytes()
            light_path='/World/lights/l_'+ 'source_lamp'.encode().hex()
            original=self.simulation._usd_world(stage,light_path).copy()
            spec=self.simulation.camera_for(world,stage=stage);spec['primPath']='/World/source_camera_0'
            spec['lookat']=[.5,.2,1.2]
            self.simulation._apply_camera_view(stage,spec,{'objectPaths':compiled['bindings']})
            self.assertFalse(stage.GetSessionLayer().empty)
            self.assertEqual(source.read_bytes(),before)
            reloaded=self.Usd.Stage.Open(Sdf.Layer.FindOrOpen(str(source)),Sdf.Layer.CreateAnonymous())
            self.assertTrue(reloaded.GetSessionLayer().empty)
            light=UsdLux.SphereLight(reloaded.GetPrimAtPath(light_path))
            self.assertTrue(light);self.assertEqual(light.GetIntensityAttr().Get(),1200)
            self.assertAlmostEqual(light.GetRadiusAttr().Get(),.1)
            np.testing.assert_allclose(self.simulation._usd_world(reloaded,light_path),original,atol=1e-12)
            for id in ('floor','wall','ceiling'):
                path=compiled['bindings'][id]
                self.assertEqual(self.UsdGeom.Imageable(reloaded.GetPrimAtPath(path+'/visual_0')).ComputeVisibility(),self.UsdGeom.Tokens.inherited)
                self.assertTrue(reloaded.GetPrimAtPath(path+'/collision').HasAPI(self.UsdPhysics.CollisionAPI))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),hashlib.sha256(before).hexdigest())


    def test_multiple_declared_lights_and_cameras_do_not_relocate_or_rewrite_source(self):
        import numpy as np
        from pxr import UsdLux
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);stage,world,compiled=self.native_lighting_fixture(directory,multiple=True)
            before=(directory/'scene-source.usda').read_bytes()
            lights=[str(p.GetPath()) for p in stage.Traverse() if p.HasAPI(UsdLux.LightAPI)]
            self.assertEqual(len(lights),2)
            poses={path:self.simulation._usd_world(stage,path).copy() for path in lights}
            for number in range(2):
                path='/World/source_camera_'+str(number)
                self.assertTrue(self.UsdGeom.Camera(stage.GetPrimAtPath(path)))
                spec={**self.simulation.camera_for(world,stage=stage),'primPath':path,'lookat':[number+.2,0,1]}
                self.simulation._apply_camera_view(stage,spec,{'objectPaths':compiled['bindings']})
            self.assertEqual((directory/'scene-source.usda').read_bytes(),before)
            for path in lights:np.testing.assert_allclose(self.simulation._usd_world(stage,path),poses[path],atol=1e-12)


    def test_new_camera_keeps_verified_translate_and_double_orient_stack(self):
        stage = self.Usd.Stage.CreateInMemory()
        spec = {'lookat': [.3, 0, 1], 'distance': 1.8, 'azimuth': 135, 'elevation': -30,
                'width': 1280, 'height': 720, 'scope': 'task:push', 'primPath': '/World/task_camera_70757368'}
        self.simulation._write_camera(stage, spec)
        camera = self.UsdGeom.Camera(stage.GetPrimAtPath(spec['primPath']))
        ops = camera.GetOrderedXformOps()
        self.assertEqual([str(op.GetOpName()) for op in ops], ['xformOp:translate', 'xformOp:orient'])
        self.assertEqual(ops[1].GetPrecision(), self.UsdGeom.XformOp.PrecisionDouble)
        self.assertFalse(camera.GetPrim().GetAttribute('xformOp:transform'))

    def test_session_camera_reuses_original_ops_precision_order_and_root_values(self):
        stage = self.Usd.Stage.CreateInMemory()
        camera = self.UsdGeom.Camera.Define(stage, '/World/camera')
        translate = camera.AddTranslateOp(self.UsdGeom.XformOp.PrecisionFloat)
        translate.Set(self.Gf.Vec3f(1, 2, 3))
        orient = camera.AddOrientOp(self.UsdGeom.XformOp.PrecisionDouble)
        orient.Set(self.Gf.Quatd(1, self.Gf.Vec3d(0)))
        before = stage.GetRootLayer().ExportToString()
        ordered = camera.GetXformOpOrderAttr().Get()
        precision = [op.GetPrecision() for op in camera.GetOrderedXformOps()]
        spec = {'lookat': [10, 0, 1], 'distance': 5, 'azimuth': 135, 'elevation': -30,
                'width': 1280, 'height': 720, 'scope': 'room', 'primPath': '/World/camera'}
        self.simulation._apply_camera_view(stage, spec, {'objectPaths': {}})
        self.assertEqual(stage.GetRootLayer().ExportToString(), before)
        self.assertEqual(camera.GetXformOpOrderAttr().Get(), ordered)
        self.assertEqual([op.GetPrecision() for op in camera.GetOrderedXformOps()], precision)
        self.assertFalse(camera.GetPrim().GetAttribute('xformOp:transform'))
        session = stage.GetSessionLayer().GetPrimAtPath('/World/camera')
        self.assertNotIn('xformOpOrder', session.properties)
        self.assertIn('xformOp:translate', session.properties)
        self.assertIn('xformOp:orient', session.properties)

    def test_session_camera_preserves_official_export_identity_scale_stack(self):
        import numpy as np
        stage = self.Usd.Stage.CreateInMemory()
        camera = self.UsdGeom.Camera.Define(stage, '/World/camera')
        camera.AddTranslateOp(self.UsdGeom.XformOp.PrecisionDouble).Set(
            self.Gf.Vec3d(-3.230039358139038, 5.730039119720459, 6.07855749130249))
        camera.AddOrientOp(self.UsdGeom.XformOp.PrecisionDouble).Set(self.Gf.Quatd(
            -0.3314135670661926, self.Gf.Vec3d(-0.19134171307086945, 0.4619397819042206, 0.8001031279563904)))
        scale = camera.AddScaleOp(self.UsdGeom.XformOp.PrecisionDouble)
        scale.Set(self.Gf.Vec3d(1))
        root_before = stage.GetRootLayer().ExportToString()
        order = camera.GetXformOpOrderAttr().Get()
        precision = [op.GetPrecision() for op in camera.GetOrderedXformOps()]
        spec = {'lookat': [10, 0, 1], 'distance': 5, 'azimuth': 135, 'elevation': -30,
                'width': 1280, 'height': 720, 'scope': 'room', 'primPath': '/World/camera'}
        active = self.simulation._apply_camera_view(stage, spec, {'objectPaths': {}})
        self.assertEqual(stage.GetRootLayer().ExportToString(), root_before)
        self.assertEqual(camera.GetXformOpOrderAttr().Get(), order)
        self.assertEqual([op.GetPrecision() for op in camera.GetOrderedXformOps()], precision)
        self.assertEqual(scale.Get(), self.Gf.Vec3d(1))
        self.assertFalse(camera.GetPrim().GetAttribute('xformOp:transform'))
        session = stage.GetSessionLayer().GetPrimAtPath('/World/camera')
        self.assertNotIn('xformOpOrder', session.properties)
        self.assertNotIn('xformOp:scale', session.properties)
        actual = np.asarray(self.UsdGeom.Xformable(camera).ComputeLocalToWorldTransform(0)).T
        np.testing.assert_allclose(actual, active['worldFromCamera'], atol=1e-6, rtol=0)

    def test_session_camera_rejects_nonunit_scale_without_reauthoring_it(self):
        for value in ((1, 1, 1.01), (1, 1, .999999999)):
            with self.subTest(scale=value):
                stage = self.Usd.Stage.CreateInMemory()
                camera = self.UsdGeom.Camera.Define(stage, '/World/camera')
                camera.AddTranslateOp(self.UsdGeom.XformOp.PrecisionDouble).Set(self.Gf.Vec3d(0))
                camera.AddOrientOp(self.UsdGeom.XformOp.PrecisionDouble).Set(self.Gf.Quatd(1, self.Gf.Vec3d(0)))
                scale = camera.AddScaleOp(self.UsdGeom.XformOp.PrecisionDouble)
                scale.Set(self.Gf.Vec3d(*value))
                root_before = stage.GetRootLayer().ExportToString()
                order = camera.GetXformOpOrderAttr().Get()
                spec = {'lookat': [10, 0, 1], 'distance': 5, 'azimuth': 135, 'elevation': -30,
                        'width': 1280, 'height': 720, 'scope': 'room', 'primPath': '/World/camera'}
                with self.assertRaises(ValueError):
                    self.simulation._apply_camera_view(stage, spec, {'objectPaths': {}})
                self.assertEqual(stage.GetRootLayer().ExportToString(), root_before)
                self.assertEqual(scale.Get(), self.Gf.Vec3d(*value))
                self.assertEqual(camera.GetXformOpOrderAttr().Get(), order)

    def test_temporary_camera_and_cutaway_leave_frozen_geometry_and_collision_intact(self):
        stage = self.Usd.Stage.CreateInMemory()
        self.UsdGeom.Xform.Define(stage, '/World')
        parent = self.UsdGeom.Xform.Define(stage, '/World/objects/wall')
        parent.AddTranslateOp().Set(self.Gf.Vec3d(0, 2, 1.5))
        visual = self.UsdGeom.Mesh.Define(stage, '/World/objects/wall/visual_0')
        visual.CreatePointsAttr([self.Gf.Vec3f(0), self.Gf.Vec3f(1, 0, 0), self.Gf.Vec3f(0, 1, 0)])
        collision = self.UsdGeom.Cube.Define(stage, '/World/objects/wall/collision')
        collision.CreateSizeAttr(1)
        self.UsdPhysics.CollisionAPI.Apply(collision.GetPrim())
        whole = {'lookat': [0, 0, 1], 'distance': 5, 'azimuth': 135, 'elevation': -30,
                 'width': 1280, 'height': 720, 'scope': 'whole_world', 'primPath': '/World/camera'}
        frozen_camera = self.simulation._write_camera(stage, whole)
        root_before = stage.GetRootLayer().ExportToString()
        points_before = visual.GetPointsAttr().Get()
        pose_before = parent.GetLocalTransformation()
        focused = {**whole, 'lookat': [10, 0, 1], 'scope': 'room', 'hiddenVisualNodeIds': ['wall']}
        camera = self.simulation._apply_camera_view(stage, focused, {'objectPaths': {'wall': str(parent.GetPath())}})
        self.assertEqual(stage.GetRootLayer().ExportToString(), root_before)
        self.assertEqual(visual.GetPointsAttr().Get(), points_before)
        self.assertEqual(parent.GetLocalTransformation(), pose_before)
        self.assertTrue(collision.GetPrim().HasAPI(self.UsdPhysics.CollisionAPI))
        self.assertEqual(collision.GetSizeAttr().Get(), 1)
        self.assertEqual(visual.ComputeVisibility(), self.UsdGeom.Tokens.invisible)
        self.assertNotEqual(camera['worldFromCamera'], frozen_camera['worldFromCamera'])
        self.assertEqual(camera['scope'], 'room')

    def test_frozen_task_camera_is_selected_without_reauthoring_its_pose(self):
        stage = self.Usd.Stage.CreateInMemory()
        spec = {'lookat': [.3, 0, 1], 'distance': 1.8, 'azimuth': 135, 'elevation': -30,
                'width': 1280, 'height': 720, 'scope': 'task:push', 'primPath': '/World/task_camera_70757368',
                'hiddenVisualNodeIds': []}
        camera = self.simulation._write_camera(stage, spec)
        before = stage.GetRootLayer().ExportToString()
        active = self.simulation._apply_camera_view(stage, camera, {'objectPaths': {}})
        self.assertEqual(active, camera)
        self.assertEqual(stage.GetRootLayer().ExportToString(), before)
        self.assertEqual(stage.GetSessionLayer().ExportToString(), '#usda 1.0\n\n')


if __name__ == '__main__':
    unittest.main()
