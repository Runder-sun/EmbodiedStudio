"""SYSTEM_VERIFICATION_ONLY: immutable installation contracts, not robot task success."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from runtime import worker
from runtime.scene import Task


class RobotSetupContractsTests(unittest.TestCase):
    def setup(self):
        profile=json.dumps({'schema':'sceneagent-robot-profile-v1','model':{'resourceRef':'assets/model/report.json'},
            'ports':{'arm':{'source':{'primitive':'joint_position','path':'.','joints':['axis']}}},
            'streams':{'q':{'source':{'reader':'joint_positions','path':'.','joints':['axis']},'visibleToPolicy':True,'required':True}}})
        return {'robots':[{'id':'left','profileRef':'models/example.robot.json'}],
            'resourceRefs':['assets/model/report.json'],'sourceFiles':{'models/example.robot.json':profile},
            'sourceDigests':{'models/example.robot.json':hashlib.sha256(profile.encode()).hexdigest()}}

    def test_profile_snapshot_is_exact_data_and_supports_multiple_instances(self):
        value=self.setup();value['robots'].append({'id':'right','profileRef':'models/example.robot.json','at':[1,0,0]})
        before=deepcopy(value);result=worker.robot_setup_sources(value)
        self.assertEqual(value,before)
        self.assertEqual(len(result['robots']),2)
        self.assertEqual(result['sourceDigests'],value['sourceDigests'])
        self.assertEqual(result['robots'][0]['orientation'],[1,0,0,0])

    def test_invalid_or_undeclared_installation_inputs_are_rejected(self):
        mutations=[lambda x:x['robots'].append(deepcopy(x['robots'][0])),
            lambda x:x['robots'][0].update(profileRef='../outside.robot.json'),
            lambda x:x['robots'][0].update(at=[True,0,0]),
            lambda x:x['robots'][0].update(orientation=[2,0,0,0]),
            lambda x:x['sourceFiles'].update({'other.py':'pass'}),
            lambda x:x['sourceFiles'].update({'models/example.robot.json':x['sourceFiles']['models/example.robot.json']+' '}),
            lambda x:x.update(score={'success':True})]
        for mutate in mutations:
            value=self.setup();mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):worker.robot_setup_sources(value)

    def test_recording_extension_preserves_existing_cameras_primary_and_task_semantics(self):
        original={'cameraId':'overview','frame':'world','at':[0,0,2],'orientation':[1,0,0,0],
            'resolution':[64,64],'rateHz':10,'modalities':['rgb'],'video':True,'required':True,'source':'simulation_observer'}
        task=Task('fixture',instruction='SYSTEM_VERIFICATION_ONLY',recording={'cameras':[original],'primaryCameraId':'overview'})
        extra={**original,'cameraId':'wrist','frame':{'nativePath':'/World/robot_left/model/tool'}}
        value={'fixture':{'cameras':[original,extra],'primaryCameraId':'overview'}}
        result=worker.robot_setup_tasks([worker.asdict(task)],value)
        self.assertEqual(result[0]['recording']['cameras'],[original,extra])
        self.assertEqual(task.recording['cameras'],[original])
        self.assertEqual(result[0]['measurements'],task.measurements)
        for bad in [{'fixture':{'cameras':[extra],'primaryCameraId':'wrist'}},
                    {'fixture':{'cameras':[original,extra],'primaryCameraId':'wrist'}}, {'unknown':value['fixture']}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):worker.robot_setup_tasks([worker.asdict(task)],bad)

    def test_worker_rejects_installation_without_behavior_and_external_creation(self):
        from unittest.mock import patch
        root=Path('/system-fixture')
        for operation in (worker.rollout,worker.evaluate_internal):
            with self.subTest(operation=operation.__name__), patch.object(worker,'frozen') as frozen:
                with self.assertRaisesRegex(ValueError,'behavior|installation|robotSetup'):
                    operation({'projectId':'p','robotSetup':self.setup()},root)
                frozen.assert_not_called()

    def test_install_authoring_preserves_world_declarations_instead_of_sdk_contract(self):
        try:
            from pxr import Usd,UsdGeom
        except ModuleNotFoundError:
            self.skipTest('requires locked SDK USD reader')
        from unittest.mock import patch
        from runtime.scene import World
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();(root/'base').mkdir()
            stage=Usd.Stage.CreateNew(str(root/'base/scene-source.usda'))
            stage.SetDefaultPrim(UsdGeom.Xform.Define(stage,'/World').GetPrim());stage.GetRootLayer().Save();stage=None
            world=World();world.space('space',bounds={'min':[-2,-2,0],'max':[2,2,2]})
            task=Task('fixture',bindings={'space':'space'},rubric={'sourceRef':'grade.py','helperRefs':[]})
            request={'baseWorld':world.snapshot(),'baseIo':{'schema':'sceneagent-native-io-v1',**world.io},
                'baseUsd':'scene-source.usda','resources':{},'recordingCameras':{},'tasks':[worker.asdict(task)],
                'robotSetup':{'robots':[{'id':'left','profileRef':'arm.robot.json'}]}}
            def robot(value,id,profile,**kwargs):
                UsdGeom.Xform.Define(value.stage,'/World/'+id)
                value.bind(id,'/World/'+id)
                return {'id':id}
            with patch.object(World,'robot',robot):worker._robot_setup_entry(request,root)
            result=json.loads((root/'scene.json').read_text())
            self.assertEqual(set(result['io']),{'clock','ports','streams'})
            self.assertEqual(json.loads((root/'tasks.json').read_text()),[worker.asdict(task)])

    def test_controller_reads_base_and_added_public_resources_without_private_sources(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys
        from runtime.scene import World
        world=World();world.box('object',[.1,.1,.1])
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            refs={'base':'base/dependencies/resources/package/files/model.txt',
                  'new':'dependencies/resources/package/files/model.txt'}
            for label,entry in refs.items():
                path=root/entry;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(label)
            env=SimpleNamespace(world=world,io={'clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}},
                source_directory=root,resource_refs=refs,time=0,truncated=False,
                observe=lambda *a,**k:{'time':0},reset=lambda seed:None,step=lambda values,seconds:{},
                last_result={'success':False,'terminated':False,'reward':None,'details':{}})
            task=Task('fixture',bindings={'object':'object'},rubric={'sourceRef':'grade.py','helperRefs':[]});task.bind(world)
            source="def run(env, task):\n    opened=env.open()\n    assert opened['resources']=={'base':'/base-resources/package/files/model.txt','new':'/resources/package/files/model.txt'}\n    env.step({})\n"
            command=[sys.executable,'-c','from runtime.worker import controller_entry; controller_entry()']
            with patch.object(worker,'controller_command',return_value=command) as called:
                result=worker.run_controller(env,task,source,'expert',source_files={'behavior.py':source},source_ref='behavior.py')
            self.assertEqual(result['actionCalls'],1)
            self.assertEqual(called.call_args.kwargs['resources'],root/'dependencies/resources')
            self.assertEqual(called.call_args.kwargs['base_resources'],root/'base/dependencies/resources')

    def test_authoring_failure_is_reported_even_when_sdk_returns_zero(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();base,manifest,world,_,_,_=self.installed_fixture(root)
            setup=self.setup();setup['resourceRefs']=[]
            manifest.update(seed=0);manifest['compiled']['usd']='scene-source.usda'
            def failed(command, **kwargs):
                temporary=next((root/'projects/p/robot-setups').glob('installing-*'))
                worker.save(temporary/'installation-failure.json',{'error':'ValueError: ambiguous imported fixed frame: base'})
                return SimpleNamespace(returncode=0)
            with patch.dict('sys.modules',{'runtime.simulation':SimpleNamespace(compile_native_world=None)}), \
                 patch.object(worker,'native_build_command',return_value=['SYSTEM_VERIFICATION_ONLY']), \
                 patch.object(worker,'audit_native_stage',return_value={}), \
                 patch.object(worker.subprocess,'run',side_effect=failed), \
                 self.assertRaisesRegex(RuntimeError,'ambiguous imported fixed frame'):
                worker.robot_environment(root,root/'projects/p',base,manifest,world,
                    {'robotSetup':setup,'jobId':'new_installation'},write=True)

    def installed_fixture(self, root):
        from runtime.scene import World,revision_key
        base=root/'projects/p/revisions/base';base.mkdir(parents=True)
        world=World();base_world=world.snapshot()
        (base/'scene.json').write_text(json.dumps(base_world));(base/'rubrics.py').write_text('ORIGINAL_FROZEN_SOURCE')
        manifest={'projectId':'p','revision':'base','runtime':'current','capabilities':{'robotSetup':True},
            'artifacts':{'manifest':'projects/p/revisions/base/manifest.json'},'assets':{},
            'frozenArtifactDigests':{'scene.json':worker.digest(base/'scene.json')},
            'sourceDigests':{'rubrics.py':worker.digest(base/'rubrics.py')},
            'io':world.io,'compiled':{'io':world.io},'tasks':[worker.asdict(Task('fixture'))]}
        (base/'manifest.json').write_text(json.dumps(manifest))
        inputs={'baseManifestRef':manifest['artifacts']['manifest'],'baseManifestSha256':worker.digest(base/'manifest.json'),
            'baseRevision':'base','runtime':'current','setup':worker.robot_setup_sources(self.setup()),
            'resourceReports':{},'installationJobId':'creator'}
        directory=root/'projects/p/robot-setups'/revision_key(**inputs);(directory/'base').mkdir(parents=True)
        for name in ['scene.json','rubrics.py']:(directory/'base'/name).write_bytes((base/name).read_bytes())
        (directory/'rubrics.py').write_bytes((base/'rubrics.py').read_bytes())
        world.bind('left','/World/robot_6c656674/model')
        (directory/'scene.json').write_text(json.dumps(world.snapshot()))
        for name,text in inputs['setup']['sourceFiles'].items():
            path=directory/'profiles'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        descriptor={'schema':'sceneagent-robot-setup-v1','projectId':'p','inputs':inputs,
            'environment':{'resources':{},**{key:deepcopy(manifest[key]) for key in ['assets','frozenArtifactDigests','sourceDigests','io','compiled','tasks']}}}
        path=directory/'manifest.json';path.write_text(json.dumps(descriptor))
        ref=str(path.relative_to(root));job=root/'jobs/creator';job.mkdir(parents=True)
        (job/'status.json').write_text(json.dumps({'jobId':'creator','state':'completed'}))
        (job/'result.json').write_text(json.dumps({'projectId':'p','revision':'base','robotSetupRef':ref,
            'robotSetupSha256':worker.digest(path),'publicationArtifactDigests':{ref:worker.digest(path)}}))
        return base,manifest,World.restore(base_world),directory,descriptor,ref

    def test_installation_reuse_requires_independent_published_descriptor_digest(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();base,manifest,world,directory,descriptor,ref=self.installed_fixture(root)
            with patch.object(worker,'verify_inputs',return_value=set()),patch.object(worker,'runtime_key',return_value='current'):
                worker.robot_environment(root,root/'projects/p',base,manifest,world,{'robotSetupRef':ref})
                for change in ['source','compiled']:
                    changed=deepcopy(descriptor)
                    if change=='source':
                        (directory/'rubrics.py').write_text('CHANGED_GRADE')
                        changed['environment']['sourceDigests']['rubrics.py']=worker.digest(directory/'rubrics.py')
                    else:changed['environment']['compiled']['initialState']={'forged':True}
                    (directory/'manifest.json').write_text(json.dumps(changed))
                    with self.subTest(change=change),self.assertRaisesRegex(ValueError,'publication|published|installation'):
                        worker.robot_environment(root,root/'projects/p',base,manifest,world,{'robotSetupRef':ref})
                    (directory/'rubrics.py').write_bytes((base/'rubrics.py').read_bytes())
                    (directory/'manifest.json').write_text(json.dumps(descriptor))
                (root/'jobs/creator/status.json').write_text(json.dumps({'jobId':'creator','state':'running'}))
                with self.assertRaisesRegex(ValueError,'verified successful exit'):
                    worker.robot_environment(root,root/'projects/p',base,manifest,world,{'robotSetupRef':ref,'jobId':'creator'})

    def test_no_installation_retains_the_exact_base_environment(self):
        values=(Path('/system-fixture/base'),{'revision':'base'},object())
        actual=worker.robot_environment(Path('/system-fixture'),Path('/system-fixture/projects/p'),*values,{})
        self.assertEqual(actual[:3],values)
        self.assertEqual(actual[3],set())
