"""SYSTEM_VERIFICATION_ONLY CPU fixtures, not simulator captures or generated products."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.read_demonstrations import consume, samples
from runtime.records import Recorder, atomic_json, load_batch
from runtime.scene import Task, World
from runtime.worker import media_summary
from tests.test_records import RubricFixture, COMMAND, BASE_IO


class DemonstrationConsumerTests(unittest.TestCase):
    def publish(self, root, episode, manifest=None):
        manifest = manifest or json.loads((episode/'manifest.json').read_text())
        manifest['artifactDigests'] = {k:hashlib.sha256((root/v).read_bytes()).hexdigest()
                                      for k,v in manifest['artifacts'].items() if k != 'manifest'}
        atomic_json(episode/'manifest.json', manifest)
        job = root/'jobs/j1'; job.mkdir(parents=True, exist_ok=True)
        atomic_json(job/'status.json', {'jobId':'j1', 'state':'completed'})
        atomic_json(job/'result.json', {'projectId':'p1', 'revision':'rev1', 'episodes':[{'episode':'e1','taskId':'t1'}],
            'publicationArtifactDigests':{manifest['artifacts']['manifest']:hashlib.sha256((episode/'manifest.json').read_bytes()).hexdigest()}})

    def fixture(self, root, *, image_size=(32,32), missing=None, optional=False, policy_reference='latest', unavailable_reason='CPU fixture unavailable sensor', frame_metadata=None, physics_rate=500, passive=False, success=True):
        from PIL import Image
        import imageio.v2 as imageio
        import numpy as np
        episode = root/'projects/p1/episodes/e1'; episode.mkdir(parents=True)
        world = World(native=True); world.bind('cube','/World/cube')
        cameras = [{'cameraId':name, 'frame':'world' if name == 'front' else {'nativePath':'/World/franka/panda_hand'},
            'at':[0,0,1] if name == 'front' else [0,0,.1], 'orientation':[1,0,0,0],
            'resolution':[32,32], 'rateHz':25, 'modalities':['rgb'], 'video':True,
            'required':not (name == 'wrist' and optional), 'source':'simulation_observer'} for name in ('front','wrist')]
        recording = {'cameras':cameras, 'primaryCameraId':'front'}
        task = Task('t1', instruction='CPU fixture instruction', bindings={'subject':'cube'},
                    rubric={'sourceRef':'rubrics.py', 'helperRefs':[]}, recording=recording,
                    measurements={'camera':{'cameraIds':['front'], 'visibleToPolicy':True}})
        io = deepcopy(BASE_IO)
        if passive:io['ports']={}
        io['clock']['physicsRateHz']=physics_rate
        io['streams']['hand']={'source':{'reader':'rigid_pose','path':'/World/franka/panda_hand'},
            'dtype':'float64','shape':[7],'units':['m']*3+['1']*4,'frame':'world_wxyz','origin':'native_output',
            'rateHz':50,'visibleToPolicy':False,'required':True}
        def matrix(z):return [[1,0,0,0],[0,1,0,0],[0,0,1,z],[0,0,0,1]]
        def state(t):return {'time':t, 'simulationTime':1+t, 'timeOrigin':1, 'physicsStep':round(t*physics_rate),
            'stageId':'s1', 'linkMatrices':{'/World/franka/panda_hand':matrix(.8+5*t)}, 'objects':{}}
        frames, frame_sets = [], {}
        for fi,t in enumerate((0.,.04)):
            current = {}
            for camera in cameras:
                name = camera['cameraId']
                if name == missing:continue
                relative = f'frames/{name}/{fi:06d}.png'; path = episode/relative; path.parent.mkdir(parents=True,exist_ok=True)
                Image.new('RGB', image_size if name == 'front' else (32,32), (40+40*fi,20,10)).save(path)
                frame = {'index':len(frames), 'cameraId':name, 'frameIndex':fi, 'sampleId':round(t*physics_rate),
                    'cameraScope':'task:t1','cameraPrimPath':'/World/record_camera_'+b't1'.hex()+'_'+name.encode().hex(),
                    'physicsStep':round(t*physics_rate), 'time':t, 'simulationTime':1+t, 'timeOrigin':1, 'stageId':'s1',
                    'path':relative, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                    'worldFromCamera':matrix(1 if name == 'front' else .9+5*t),
                    'intrinsics':{'fx':32*24/36,'fy':32*24/36,'cx':16.,'cy':16.}}
                frame.update(frame_metadata or {})
                frames.append(frame); current[name] = frame
            frame_sets[t] = current
        def observed(t, truth=False):
            selected = frame_sets[0. if t < .04 else .04]
            value = {k:state(t)[k] for k in ('time','simulationTime','timeOrigin','physicsStep','stageId')}
            value.update(streams={'q':[0]*9,'dq':[0]*9},streamStatus={},
                cameraFrames=deepcopy(selected if truth else {k:v for k,v in selected.items() if k == 'front'}))
            if truth and missing is not None:
                value['cameraFrames'][missing] = {'cameraId':missing,'status':'unavailable','reason':unavailable_reason}
            if not truth and policy_reference == 'omitted':
                value['cameraFrames'] = {}
            elif not truth and policy_reference == 'stale':
                value['cameraFrames'] = {'front': deepcopy(frame_sets[0.]['front'])}
            if truth:
                value['streams']['hand']=[0,0,.8+5*t,1,0,0,0]
                value['streamStatus']['hand']={'status':'available','sampleTime':t,'physicsStep':round(t*physics_rate)}
            return value
        class Annotated(RubricFixture):
            def preview(self, phase, facts):
                value = super().preview(phase,facts)
                if phase == 'transition':value['result']['details'] = {'annotations':[{'id':'numeric','text':'Generated fixture phase','bindings':['subject']}]}
                return value
        recorder = Recorder(episode, io=io, recording=recording, task=asdict(task)); session = Annotated(success=success)
        recorder.begin({'observation':observed(0.),'truth':observed(0.,True)},session)
        for before,after in ((0.,.02),(.02,.04)):
            action={} if passive else COMMAND
            recorder.commit(observed(before),action,action,observed(after),physics_rate//50,truth_before=observed(before,True),truth_after=observed(after,True))
        recorder.close('completed')
        artifacts = {k:str((episode/v).relative_to(root)) for k,v in {'manifest':'manifest.json','records':'records.json',
            'transitions':'transitions.jsonl','trajectory':'trajectory.json','annotations':'annotations.json'}.items()}
        for camera in cameras:
            name = camera['cameraId']
            if name == missing:continue
            target = episode/f'videos/{name}.mp4'; target.parent.mkdir(exist_ok=True)
            with imageio.get_writer(target,fps=25,codec='libx264',macro_block_size=1) as writer:
                for fi in range(2):writer.append_data(np.full((32,32,3),40+40*fi,dtype=np.uint8))
            artifacts['video:'+name] = str(target.relative_to(root))
        artifacts['video'] = artifacts['video:front']
        manifest = {'schema':'sceneagent-episode-v6','engine':'isaacsim','physics':'physx','projectId':'p1','revision':'rev1','episodeId':'e1','taskId':'t1',
            'manifestRef':'projects/p1/revisions/rev1/manifest.json','io':io,
            'recording':recording,'publicationJobId':'j1','status':'completed','demonstrationEligible':success is True,
            'executionStatus':'completed','purpose':'demonstration','controllerKind':'expert',
            'executionKind':'actuator_physics_native',
            'rubric':session.descriptor,'result':recorder.last_result,'artifacts':artifacts,
            'cameraFailures':{missing:unavailable_reason} if missing else {}}
        manifest['media'] = media_summary(manifest,frames)
        atomic_json(episode/'trajectory.json',{'schema':'sceneagent-studio-trajectory-v6','revision':'rev1','task':asdict(task),
            'initialState':observed(0.,True),'recording':recording,'frames':frames,'media':manifest['media'],
            'cameraFailures':deepcopy(manifest['cameraFailures'])})
        batch = load_batch(episode)
        atomic_json(episode/'annotations.json',{'schema':'sceneagent-generated-annotations-v1','taskId':'t1','rubric':batch['rubric'],
            'bindings':batch['task']['bindings'],'annotations':batch['semanticAnnotations']})
        build = {'tasks':[asdict(task)],'io':io,'compiled':{'io':io},'artifacts':{'manifest':manifest['manifestRef']}}
        self.addCleanup(patch.stopall)
        patch('runtime.worker.frozen',return_value=(None,root/'projects/p1/revisions/rev1',build,world)).start()
        self.publish(root,episode,manifest)
        return episode

    def test_independent_behavior_source_is_read_and_rechecked(self):
        from runtime.worker import behavior_sources, behavior_snapshot
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);episode=self.fixture(root)
            source='def run(env, task):\n    pass\n'
            selected=behavior_sources({'sourceRef':'separate.py','helperRefs':[],
                'sourceFiles':{'separate.py':source},'sourceDigests':{'separate.py':hashlib.sha256(source.encode()).hexdigest()}})
            reference,_=behavior_snapshot(root,root/'projects/p1',selected,write=True)
            manifest=json.loads((episode/'manifest.json').read_text())
            manifest.update(behaviorRef=reference,controllerSource=selected['sourceRef'],controllerDigest=selected['sourceDigest'],
                controllerHelperDigests=selected['helperDigests'])
            self.publish(root,episode,manifest)
            self.assertEqual(consume(episode)['behaviorRef'],reference)
            (root/reference).parent.joinpath('separate.py').write_text(source+'# changed\n')
            with self.assertRaisesRegex(ValueError,'digest|source'):consume(episode)

    def test_complete_failure_and_none_data_keep_their_true_result_and_label(self):
        for outcome in (False, None):
            with tempfile.TemporaryDirectory() as temporary, self.subTest(outcome=outcome):
                episode = self.fixture(Path(temporary), success=outcome)
                report = consume(episode)
                self.assertTrue(report['passed'])
                self.assertIs(report['result']['success'], outcome)
                self.assertFalse(report['demonstrationEligible'])
                first = next(samples(episode))
                self.assertFalse(first['demonstrationEligible'])
            patch.stopall()

    def test_hidden_required_camera_is_readable_but_was_not_policy_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary)); report = consume(episode); first = next(samples(episode))
            self.assertTrue(report['passed']); self.assertEqual(len(report['media']['cameras']),2)
            self.assertEqual(len(first['action']['arm']),8); self.assertEqual(len(first['observation']['streams']['q']),9)
            self.assertEqual(report['io']['ports']['arm']['shape'],[8])
            self.assertEqual(set(first['cameras']),{'front','wrist'}); self.assertEqual(first['policyCameraIds'],['front'])
            self.assertNotIn('rubricState',json.dumps(first))

    def test_rehashed_passive_records_cannot_claim_robot_demonstration_eligibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);episode=self.fixture(root,passive=True)
            # All record/media hashes are valid and eligibility was explicitly forged.
            with self.assertRaisesRegex(ValueError,'control ports'):
                consume(episode)

    def test_nondefault_physics_clock_drives_frames_and_native_pose_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            episode=self.fixture(Path(temporary),physics_rate=200)
            report=consume(episode);rows=list(samples(episode))
            self.assertEqual(report['io']['clock']['physicsRateHz'],200)
            self.assertEqual(report['physicalSteps'],8)
            self.assertEqual(rows[-1]['nextObservation']['physicsStep'],8)
            self.assertEqual(rows[-1]['nextObservation']['cameraFrames']['front']['sampleId'],8)
            trajectory=json.loads((episode/'trajectory.json').read_text())
            self.assertEqual([frame['sampleId'] for frame in trajectory['frames'] if frame['cameraId']=='wrist'],[0,8])

    def test_equal_length_transition_tamper_is_rejected_after_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary)); path = episode/'transitions.jsonl'
            before = path.read_bytes(); after = before.replace(b'"q": [0',b'"q": [1',1)
            self.assertEqual(len(before),len(after)); self.assertNotEqual(before,after); path.write_bytes(after)
            with self.assertRaisesRegex(ValueError,'digest'):consume(episode)

    def test_policy_references_match_the_latest_actual_declared_sample(self):
        for mode in ('omitted', 'stale'):
            with tempfile.TemporaryDirectory() as temporary,self.subTest(mode=mode):
                episode = self.fixture(Path(temporary),policy_reference=mode)
                with self.assertRaisesRegex(ValueError,'camera|sample|sensor'):
                    consume(episode)
            patch.stopall()

    def test_camera_identity_matches_its_frozen_task_and_sensor(self):
        for changed in ({'cameraScope':'task:other'}, {'cameraPrimPath':'/World/other_sensor'}):
            with tempfile.TemporaryDirectory() as temporary,self.subTest(changed=changed):
                episode = self.fixture(Path(temporary),frame_metadata=changed)
                with self.assertRaisesRegex(ValueError,'camera|sensor'):
                    consume(episode)
            patch.stopall()

    def test_optional_missing_camera_requires_an_explicit_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); episode = self.fixture(root,missing='wrist',optional=True)
            manifest = json.loads((episode/'manifest.json').read_text()); manifest['cameraFailures'] = {}
            path = episode/'trajectory.json'; trajectory = json.loads(path.read_text())
            trajectory['cameraFailures'] = {}
            manifest['media'] = media_summary(manifest,trajectory['frames']); trajectory['media'] = manifest['media']
            atomic_json(path,trajectory); self.publish(root,episode,manifest)
            with self.assertRaisesRegex(ValueError,'missing|reason|failure'):consume(episode)

    def test_camera_pixels_must_match_the_committed_frame_digest(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary))
            Image.new('RGB',(32,32),'white').save(episode/'frames/wrist/000000.png')
            with self.assertRaisesRegex(ValueError,'pixels|digest'):consume(episode)

    def test_camera_dimensions_are_checked_even_with_a_valid_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary),image_size=(16,16))
            with self.assertRaisesRegex(ValueError,'pixels|camera'):consume(episode)

    def test_stale_wrist_extrinsics_and_rehashed_annotations_are_rejected(self):
        for kind in ('extrinsics','annotations'):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); episode = self.fixture(root)
                path = episode/('trajectory.json' if kind == 'extrinsics' else 'annotations.json'); value = json.loads(path.read_text())
                if kind == 'extrinsics':
                    wrist = [f for f in value['frames'] if f['cameraId'] == 'wrist']; wrist[-1]['worldFromCamera'] = wrist[0]['worldFromCamera']
                else:value['annotations'][0]['text'] = 'Unobserved text'
                atomic_json(path,value); self.publish(root,episode)
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'extrinsics|annotations'):consume(episode)
            patch.stopall()

    def test_optional_absence_is_disclosed_but_required_absence_rejects(self):
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary),missing='wrist',optional=True); report = consume(episode)
            self.assertEqual(report['media']['cameras'][1]['status'],'unavailable'); self.assertTrue(report['media']['cameras'][1]['reason'])
        patch.stopall()
        with tempfile.TemporaryDirectory() as temporary:
            episode = self.fixture(Path(temporary),missing='wrist')
            with self.assertRaisesRegex(ValueError,'required camera'):consume(episode)

    def test_unsupported_optional_mask_is_disclosed_without_claiming_pixel_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            reason = 'declared pixel modality is unsupported; this runtime implements RGB only'
            root = Path(temporary); episode = self.fixture(root,missing='wrist',optional=True,unavailable_reason=reason)
            manifest = json.loads((episode/'manifest.json').read_text())
            trajectory = json.loads((episode/'trajectory.json').read_text())
            recording = manifest['recording']
            recording['cameras'][1].update(modalities=['instance_segmentation'],video=False)
            trajectory['recording'] = deepcopy(recording);trajectory['task']['recording'] = deepcopy(recording)
            reason = 'declared pixel modality is unsupported; this runtime implements RGB only'
            manifest['cameraFailures']['wrist'] = reason;trajectory['cameraFailures'] = deepcopy(manifest['cameraFailures'])
            manifest['media'] = media_summary(manifest,trajectory['frames']);trajectory['media'] = deepcopy(manifest['media'])
            index = json.loads((episode/'records.json').read_text());index['recording'] = deepcopy(recording)
            atomic_json(episode/'records.json',index);atomic_json(episode/'trajectory.json',trajectory)
            world = World(native=True);world.bind('cube','/World/cube')
            build = {'tasks':[trajectory['task']],'io':manifest['io'],'compiled':{'io':manifest['io']},
                     'artifacts':{'manifest':manifest['manifestRef']}}
            with patch('runtime.worker.frozen',return_value=(None,root/'projects/p1/revisions/rev1',build,world)):
                self.publish(root,episode,manifest);report = consume(episode)
            self.assertEqual(report['media']['cameras'][1]['status'],'unavailable')
            self.assertIn('unsupported',report['media']['cameras'][1]['reason'])

    def test_parent_media_check_rejects_another_episodes_video_reference(self):
        import shutil
        from runtime import worker
        for replay in (False,True):
            with tempfile.TemporaryDirectory() as temporary,self.subTest(replay=replay):
                root = Path(temporary);episode = self.fixture(root)
                _,_,build,world = worker.frozen(root,{})
                task = Task(**build['tasks'][0])
                manifest = json.loads((episode/'manifest.json').read_text())
                trajectory = json.loads((episode/'trajectory.json').read_text())
                if replay:
                    target = root/'projects/p1/consumer-reports/c1/replay-e1'
                    shutil.copytree(episode,target);episode = target
                    for camera_id in ('front','wrist'):
                        manifest['artifacts']['video:'+camera_id] = str((episode/f'videos/{camera_id}.mp4').relative_to(root))
                    manifest['artifacts']['video'] = manifest['artifacts']['video:front']
                    manifest['media'] = worker.media_summary(manifest,trajectory['frames']);trajectory['media'] = manifest['media']
                worker._verify_media(episode,manifest,trajectory,root=root,world=world,task=task)
                other = root/'projects/p1/episodes/e2/videos/front.mp4';other.parent.mkdir(parents=True)
                shutil.copyfile(episode/'videos/front.mp4',other)
                manifest['artifacts']['video:front'] = str(other.relative_to(root))
                manifest['artifacts']['video'] = manifest['artifacts']['video:front']
                manifest['artifactDigests']['video:front'] = hashlib.sha256(other.read_bytes()).hexdigest()
                manifest['artifactDigests']['video'] = manifest['artifactDigests']['video:front']
                manifest['media'] = worker.media_summary(manifest,trajectory['frames']);trajectory['media'] = manifest['media']
                with self.assertRaisesRegex(ValueError,'video artifact.*(scope|camera|episode)'):
                    worker._verify_media(episode,manifest,trajectory,root=root,world=world,task=task)
            patch.stopall()

    def test_parent_media_check_rejects_unavailable_or_undeclared_video_aliases(self):
        from runtime import worker
        for camera_id in ('wrist','invented'):
            with tempfile.TemporaryDirectory() as temporary,self.subTest(camera_id=camera_id):
                root=Path(temporary);episode=self.fixture(root,missing='wrist',optional=True)
                _,_,build,world=worker.frozen(root,{})
                manifest=json.loads((episode/'manifest.json').read_text())
                trajectory=json.loads((episode/'trajectory.json').read_text())
                manifest['artifacts']['video:'+camera_id]='projects/p1/episodes/e2/videos/'+camera_id+'.mp4'
                manifest['artifactDigests']['video:'+camera_id]='a'*64
                with self.assertRaisesRegex(ValueError,'video artifact.*(scope|camera|episode|recorded)'):
                    worker._verify_media(episode,manifest,trajectory,root=root,world=world,task=Task(**build['tasks'][0]))
            patch.stopall()
