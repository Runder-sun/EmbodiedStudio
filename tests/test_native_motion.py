"""SYSTEM_VERIFY_ONLY: numerical planning contracts; SDK doubles are not physical evidence."""
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np
import yaml
from runtime import motion


class Tensor:
    def __init__(self,value):self.value=np.asarray(value)
    def detach(self):return self
    def cpu(self):return self
    def numpy(self):return self.value.copy()
    def all(self):return self
    def item(self):return self.value.item()
    def __bool__(self):return bool(self.value.all())


class NativeMotionTests(unittest.TestCase):
    def test_numerical_warp_bootstrap_uses_only_the_existing_locked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory).resolve()/'warp/__init__.py';source.parent.mkdir();source.write_text('# locked test source\n')
            identity={'available':True,'modules':{'warp':str(source)},
                'files':{str(source):{'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}}
            module=SimpleNamespace(__file__=str(source))
            with patch.object(motion,'motion_runtime_identity',return_value=identity), \
                 patch.object(motion.importlib,'import_module',return_value=module),patch.object(sys,'path',list(sys.path)):
                self.assertIs(motion._selected_numerical_warp(),module)
                self.assertEqual(sys.path[0],str(source.parent.parent))
                source.write_text('# changed source\n')
                with self.assertRaisesRegex(ValueError,'source'):
                    motion._selected_numerical_warp()
            with patch.object(motion,'motion_runtime_identity',return_value={'available':False,'missing':['unqualified']}):
                with self.assertRaisesRegex(ValueError,'selection'):
                    motion._selected_numerical_warp()

    def test_an_explicit_empty_world_uses_the_upstream_no_world_checker_contract(self):
        sdk,_=self.sdk()
        with patch.object(motion,'_sdk',return_value=sdk):
            motion.create_planner({'kinematics':{}},{},interpolation_dt=.02)
        self.assertIsNone(sdk.Config.load_from_robot_config.call_args.args[1])
        sdk.WorldConfig.from_dict.assert_not_called()

    def sdk(self):
        tensor=SimpleNamespace(to_device=lambda x:Tensor(x))
        sdk=SimpleNamespace(TensorArgs=Mock(return_value=tensor),
            torch=SimpleNamespace(device=lambda x:x,cuda=SimpleNamespace(synchronize=Mock())),
            WorldConfig=SimpleNamespace(from_dict=Mock(side_effect=lambda value:value)),
            Config=SimpleNamespace(load_from_robot_config=Mock(return_value='config')),MotionGen=Mock(return_value='planner'),
            Pose=SimpleNamespace(from_list=Mock(side_effect=lambda value,*a,**k:tuple(value))),
            JointState=Mock(side_effect=lambda **values:SimpleNamespace(**values)),
            PlanConfig=Mock(return_value='native_options'))
        return sdk,tensor

    def test_planner_consumes_generic_model_and_world_without_model_name_or_drive_execution(self):
        sdk,_=self.sdk()
        with patch.object(motion,'_sdk',return_value=sdk):
            self.assertEqual(motion.create_planner({'kinematics':{'urdf_path':'/bundle/arbitrary.urdf'}},
                {'cuboid':{}},interpolation_dt=.01),'planner')
        call=sdk.Config.load_from_robot_config.call_args
        self.assertEqual(call.args[0]['kinematics']['urdf_path'],'/bundle/arbitrary.urdf')
        self.assertEqual(call.kwargs['interpolation_dt'],.01)

    def test_two_end_effector_targets_are_one_joint_problem_not_two_paths(self):
        sdk,tensor=self.sdk()
        result=SimpleNamespace(success=Tensor(True),status='SUCCESS',interpolation_dt=.01,
            get_interpolated_plan=lambda:SimpleNamespace(position=Tensor([[0,0,0],[.1,.2,.3]]),
                velocity=Tensor([[0,0,0],[0,0,0]]),joint_names=['a','b','c']))
        planner=SimpleNamespace(joint_names=['a','b','c'],tensor_args=tensor,
            kinematics=SimpleNamespace(ee_link='left',link_names=['left','right']),plan_single=Mock(return_value=result))
        targets={'left':[0,0,.4,1,0,0,0],'right':[0,.2,.4,1,0,0,0]}
        with patch.object(motion,'_sdk',return_value=sdk):
            trajectory=motion.plan_trajectory(planner,{'jointNames':['a','b','c'],
                'position':[0,0,0],'velocity':[0,0,0]},targets)
        planner.plan_single.assert_called_once()
        state=sdk.JointState.call_args.kwargs
        self.assertIn('acceleration',state);self.assertIn('jerk',state)
        self.assertEqual(set(planner.plan_single.call_args.kwargs['link_poses']),{'left','right'})
        self.assertEqual(trajectory['jointOrder'],['a','b','c'])
        self.assertEqual(trajectory['timebase']['times'],[0.,.01])
        self.assertEqual(trajectory['kind'],'numerical_plan')
        self.assertFalse(trajectory['actualExecution'])
        self.assertEqual(trajectory['unprovidedDerivativeAssumptions'],{'acceleration':0.,'jerk':0.})

    def test_ambiguous_joint_order_bad_quaternion_or_missing_link_never_calls_solver(self):
        sdk,tensor=self.sdk()
        planner=SimpleNamespace(joint_names=['a','b'],tensor_args=tensor,
            kinematics=SimpleNamespace(ee_link='tip',link_names=['tip']),plan_single=Mock())
        start={'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]}
        with patch.object(motion,'_sdk',return_value=sdk):
            for initial,targets in (({**start,'jointNames':['b','a']},{'tip':[0,0,0,1,0,0,0]}),
                    (start,{'tip':[0,0,0,0,0,0,0]}),(start,{'other':[0,0,0,1,0,0,0]})):
                with self.subTest(targets=targets),self.assertRaises(ValueError):
                    motion.plan_trajectory(planner,initial,targets)
        planner.plan_single.assert_not_called()

    def profile_fixture(self,directory):
        root=Path(directory).resolve();bundle=root/'bundle';bundle.mkdir()
        paths={
            'files/model/robot.urdf':'<robot name="model"><link name="base"><visual><geometry><mesh filename="link.stl"/></geometry></visual></link></robot>',
            'files/model/meshes/link.stl':'solid link\nendsolid link\n',
            'files/planning/spheres.yml':yaml.safe_dump({'collision_spheres':{'base':[{'center':[0,0,0],'radius':.1}]}}),
        }
        config={'robot_cfg':{'kinematics':{'urdf_path':'model/robot.urdf','asset_root_path':'model/meshes',
            'collision_spheres':'spheres.yml','base_link':'base','ee_link':'tip','link_names':['tip','other'],
            'cspace':{'joint_names':['a','b'],'retract_config':[0.,0.]}}}}
        paths['files/planning/robot.yml']=yaml.safe_dump(config)
        for name,text in paths.items():
            path=bundle/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        profile={'schema':'sceneagent-robot-profile-v1','model':{'resourceRef':'model-ref'},
            'ports':{'arm':{'source':{'primitive':'joint_position','path':'.','joints':['b','a']}}},'streams':{},
            'planning':{'resourceRef':'model-ref','file':'files/planning/robot.yml','ports':['arm'],'assetRoot':'files'}}
        source=root/'sources';source.mkdir();entry=source/'model.robot.json';entry.write_text(json.dumps(profile))
        io={'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'streams':{},
            'ports':{'robot.arm':{'source':{'primitive':'joint_position','path':'/World/robot','joints':['b','a']},
                'dtype':'float32','shape':[2],'units':['rad','rad'],'limits':[[None,None],[None,None]],
                'physicalJointLimits':[[-1.,1.],[-1.,1.]],'persistence':'persistent'}}}
        resources={'model-ref':str(bundle/'files/model/robot.urdf')}
        def freeze():
            (bundle/'report.json').write_text(json.dumps({'kind':'native_files','entry':'files/model/robot.urdf',
                'nativeFiles':list(paths),'frozenDigests':{name:hashlib.sha256((bundle/name).read_bytes()).hexdigest() for name in paths}}))
        freeze()
        sdk,tensor=self.sdk()
        result=SimpleNamespace(success=Tensor(True),status='SUCCESS',interpolation_dt=.02,
            get_interpolated_plan=lambda:SimpleNamespace(position=Tensor([[0,0],[.1,.2]]),
                velocity=Tensor([[0,0],[0,0]]),joint_names=['a','b']))
        planner=SimpleNamespace(joint_names=['a','b'],tensor_args=tensor,
            kinematics=SimpleNamespace(ee_link='tip',link_names=['tip','other']),plan_single=Mock(return_value=result))
        sdk.MotionGen.return_value=planner
        return SimpleNamespace(root=root,bundle=bundle,source=source,entry=entry,profile=profile,
            config=config,paths=paths,resources=resources,io=io,freeze=freeze,sdk=sdk,planner=planner,result=result)

    def profile_planner(self,fixture,**changes):
        arguments={'resources':fixture.resources,'io':fixture.io,'port_map':{'arm':'robot.arm'},
            'source_root':fixture.source,'interpolation_dt':.02,**changes}
        with patch.object(motion,'_sdk',return_value=fixture.sdk):
            return motion.create_planner('model.robot.json',{},**arguments)

    def test_profile_resolves_only_frozen_native_files_and_reorders_named_actuators(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory);before=f.entry.read_bytes()
            planner=self.profile_planner(f)
            config=f.sdk.Config.load_from_robot_config.call_args.args[0]
            self.assertEqual(config['kinematics']['urdf_path'],str(f.bundle/'files/model/robot.urdf'))
            self.assertEqual(config['kinematics']['asset_root_path'],str(f.bundle/'files/model/meshes'))
            self.assertEqual(config['kinematics']['collision_spheres'],{'base':[{'center':[0,0,0],'radius':.1}]})
            with patch.object(motion,'_sdk',return_value=f.sdk):
                result=motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                    {'tip':[0,0,0,1,0,0,0],'other':[0,.2,0,1,0,0,0]})
            self.assertEqual(result['portJointIndices'],{'robot.arm':[1,0]})
            self.assertEqual(f.entry.read_bytes(),before)
            planner.plan_single.assert_called_once()

    def test_explicit_complete_joint_alias_map_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory)
            f.profile['ports']['arm']['source']['joints']=['native_x','native_y']
            f.io['ports']['robot.arm']['source']['joints']=['native_x','native_y']
            f.profile['planning']['jointMap']={'a':{'port':'arm','joint':'native_y'},'b':{'port':'arm','joint':'native_x'}}
            f.entry.write_text(json.dumps(f.profile))
            planner=self.profile_planner(f)
            with patch.object(motion,'_sdk',return_value=f.sdk):
                result=motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                    {'tip':[0,0,0,1,0,0,0]})
            self.assertEqual(result['portJointIndices'],{'robot.arm':[1,0]})

    def test_profile_cannot_fall_back_to_sdk_assets_or_undeclared_mesh(self):
        for defect in ('missing','missing_spheres','outside','undeclared_mesh','network','external_root','bad_yaml','symlink'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);kin=f.config['robot_cfg']['kinematics']
                if defect=='missing':kin['urdf_path']='missing.urdf'
                if defect=='missing_spheres':kin['collision_spheres']='missing.yml'
                if defect=='outside':
                    (f.root/'outside.urdf').write_text('<robot name="outside"/>');kin['urdf_path']=str(f.root/'outside.urdf')
                if defect=='undeclared_mesh':
                    (f.bundle/'files/model/meshes/unknown.stl').write_text('not declared')
                    (f.bundle/'files/model/robot.urdf').write_text('<robot name="r"><link name="l"><visual><geometry><mesh filename="unknown.stl"/></geometry></visual></link></robot>')
                if defect=='network':kin['urdf_path']='https://example.invalid/robot.urdf'
                if defect=='external_root':kin['external_asset_path']=str(f.root)
                if defect=='symlink':
                    (f.bundle/'files/link.urdf').symlink_to(f.bundle/'files/model/robot.urdf');kin['urdf_path']='link.urdf'
                text=yaml.safe_dump(f.config) if defect!='bad_yaml' else '!!python/object/apply:os.system ["echo should-not-run"]'
                (f.bundle/'files/planning/robot.yml').write_text(text);f.freeze()
                with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises((ValueError,OSError)):
                    motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
                backend.assert_not_called()

    def test_profile_requires_explicit_source_root_resources_and_position_io(self):
        for defect in ('source_root','resources','io','port_map','mode','native_joint','limits'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);changes={}
                if defect in ('source_root','resources','io','port_map'):changes[defect]=None
                if defect=='mode':f.io['ports']['robot.arm']['source']['primitive']='joint_effort'
                if defect=='native_joint':f.io['ports']['robot.arm']['source']['joints']=['b','missing']
                if defect=='limits':del f.io['ports']['robot.arm']['physicalJointLimits']
                with self.assertRaises((ValueError,OSError)):
                    self.profile_planner(f,**changes)

    def test_joint_alias_mapping_must_cover_each_actual_actuator_once(self):
        for joint_map in ({'a':{'port':'arm','joint':'a'}},
                {'a':{'port':'arm','joint':'a'},'b':{'port':'arm','joint':'a'}},
                {'a':{'port':'other','joint':'a'},'b':{'port':'arm','joint':'b'}}):
            with self.subTest(joint_map=joint_map),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);f.profile['planning']['jointMap']=joint_map;f.entry.write_text(json.dumps(f.profile))
                with self.assertRaises(ValueError):self.profile_planner(f)
                f.planner.plan_single.assert_not_called()

    def test_profile_uses_physical_joint_limits_for_start_and_interpolated_plan(self):
        for kind in ('start','result'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);planner=self.profile_planner(f)
                start={'jointNames':['a','b'],'position':[1.1,0] if kind=='start' else [0,0],'velocity':[0,0]}
                if kind=='result':
                    f.result.get_interpolated_plan=lambda:SimpleNamespace(position=Tensor([[0,0],[0,1.1]]),
                        velocity=Tensor([[0,0],[0,0]]),joint_names=['a','b'])
                with patch.object(motion,'_sdk',return_value=f.sdk),self.assertRaisesRegex(ValueError,'physical'):
                    motion.plan_trajectory(planner,start,{'tip':[0,0,0,1,0,0,0]})
                if kind=='start':planner.plan_single.assert_not_called()

    def test_unknown_additional_target_never_reaches_solver(self):
        sdk,tensor=self.sdk()
        result=SimpleNamespace(success=Tensor(True),status='SUCCESS',interpolation_dt=.02,
            get_interpolated_plan=lambda:SimpleNamespace(position=Tensor([[0,0],[.1,.2]]),
                velocity=Tensor([[0,0],[0,0]]),joint_names=['a','b']))
        planner=SimpleNamespace(joint_names=['a','b'],tensor_args=tensor,
            kinematics=SimpleNamespace(ee_link='tip',link_names=['tip']),plan_single=Mock(return_value=result))
        with patch.object(motion,'_sdk',return_value=sdk),self.assertRaisesRegex(ValueError,'link'):
            motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                {'tip':[0,0,0,1,0,0,0],'invented':[0,0,0,1,0,0,0]})
        planner.plan_single.assert_not_called()

    def test_inactive_upstream_paths_are_not_activated_or_written_back(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory);kin=f.config['robot_cfg']['kinematics']
            kin['usd_path']='/old/sdk/robot.usd';kin['isaac_usd_path']='https://old.invalid/robot.usd'
            config_file=f.bundle/'files/planning/robot.yml';config_file.write_text(yaml.safe_dump(f.config));f.freeze()
            original=config_file.read_bytes();planner=self.profile_planner(f)
            used=f.sdk.Config.load_from_robot_config.call_args.args[0]['kinematics']
            self.assertIsNone(used['usd_path']);self.assertIsNone(used['isaac_usd_path'])
            with patch.object(motion,'_sdk',return_value=f.sdk):
                plan=motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                    {'tip':[0,0,0,1,0,0,0]})
            inactive=plan['profileProvenance']['notActivated']
            self.assertEqual(inactive['usd_path'],{'value':'/old/sdk/robot.usd','status':'notActivated'})
            self.assertEqual(config_file.read_bytes(),original)
            kin['use_usd_kinematics']=True;config_file.write_text(yaml.safe_dump(f.config));f.freeze()
            with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises(ValueError):
                motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
            backend.assert_not_called()

    def test_active_usd_reuses_passive_audit_and_every_dependency_requires_native_membership(self):
        for undeclared in (False,True):
            with self.subTest(undeclared=undeclared),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);usd='files/model/robot.usda';f.paths[usd]='#usda 1.0\n'
                (f.bundle/usd).write_text(f.paths[usd])
                f.config['robot_cfg']['kinematics'].update(use_usd_kinematics=True,usd_path='model/robot.usda')
                (f.bundle/'files/planning/robot.yml').write_text(yaml.safe_dump(f.config));f.freeze()
                files={usd:'not-used-by-this-contract-test'}
                if undeclared:
                    (f.bundle/'extra.png').write_text('undeclared');files['extra.png']='not-used'
                with patch('runtime.worker.audit_native_stage',return_value={'files':files}) as audit:
                    if undeclared:
                        with self.assertRaises(ValueError):self.profile_planner(f)
                    else:
                        self.profile_planner(f)
                        used=f.sdk.Config.load_from_robot_config.call_args.args[0]['kinematics']
                        self.assertEqual(used['usd_path'],str(f.bundle/usd));self.assertIsNone(used['urdf_path'])
                    audit.assert_called_once_with(f.bundle,usd)

    def test_default_asset_root_is_yaml_directory_and_internal_parent_paths_stay_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory);del f.profile['planning']['assetRoot'];f.entry.write_text(json.dumps(f.profile))
            kin=f.config['robot_cfg']['kinematics'];kin['urdf_path']='../model/robot.urdf';kin['asset_root_path']='../model/meshes'
            (f.bundle/'files/planning/robot.yml').write_text(yaml.safe_dump(f.config));f.freeze()
            self.profile_planner(f)
            self.assertEqual(f.sdk.Config.load_from_robot_config.call_args.args[0]['kinematics']['urdf_path'],
                str(f.bundle/'files/model/robot.urdf'))

    def test_two_articulations_with_repeated_joint_names_need_an_explicit_bijection(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory)
                f.profile['ports']={name:{'source':{'primitive':'joint_position','path':name,'joints':['joint']}} for name in ('left','right')}
                f.profile['planning']['ports']=['left','right']
                f.io['ports']={name:{'source':{'primitive':'joint_position','path':'/World/robot/'+name,'joints':['joint']},
                    'dtype':'float32','shape':[1],'units':['rad'],'limits':[[None,None]],
                    'physicalJointLimits':[[-1,1]],'persistence':'persistent'} for name in ('left','right')}
                if explicit:f.profile['planning']['jointMap']={'a':{'port':'left','joint':'joint'},'b':{'port':'right','joint':'joint'}}
                f.entry.write_text(json.dumps(f.profile))
                if not explicit:
                    with self.assertRaises(ValueError):self.profile_planner(f,port_map={'left':'left','right':'right'})
                else:
                    planner=self.profile_planner(f,port_map={'left':'left','right':'right'})
                    with patch.object(motion,'_sdk',return_value=f.sdk):
                        plan=motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                            {'tip':[0,0,0,1,0,0,0],'other':[0,0,0,1,0,0,0]})
                    self.assertEqual(plan['portJointIndices'],{'left':[0],'right':[1]})

    def inline_profile(self,fixture,*,explicit_root=False):
        planning=fixture.profile['planning'];planning.pop('file')
        config=json.loads(json.dumps(fixture.config['robot_cfg']))
        config['kinematics']['collision_spheres']='files/planning/spheres.yml'
        if not explicit_root:
            planning.pop('assetRoot')
            config['kinematics']['urdf_path']='files/model/robot.urdf'
            config['kinematics']['asset_root_path']='files/model/meshes'
        config['kinematics'].update(use_usd_kinematics=False,use_global_cumul=True)
        planning['config']=config;fixture.entry.write_text(json.dumps(fixture.profile))
        return config

    def test_inline_config_uses_the_bundle_not_entry_yaml_and_keeps_file_mapping(self):
        for explicit in (False,True):
            with self.subTest(explicit_root=explicit),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);file_planner=self.profile_planner(f)
                file_indices=file_planner._sceneagent_profile['portJointIndices']
                self.inline_profile(f,explicit_root=explicit);original=f.entry.read_bytes()
                original_reader=Path.read_text
                def guarded_read(path,*args,**kwargs):
                    if path==f.bundle/'files/model/robot.urdf':raise AssertionError('model entry must not be read as planning YAML')
                    return original_reader(path,*args,**kwargs)
                with patch.object(Path,'read_text',guarded_read):planner=self.profile_planner(f)
                used=f.sdk.Config.load_from_robot_config.call_args.args[0]['kinematics']
                self.assertEqual(used['urdf_path'],str(f.bundle/'files/model/robot.urdf'))
                self.assertEqual(used['asset_root_path'],str(f.bundle/'files/model/meshes'))
                self.assertIs(used['use_global_cumul'],True);self.assertIs(used['use_usd_kinematics'],False)
                self.assertEqual(planner._sceneagent_profile['portJointIndices'],file_indices)
                with patch.object(motion,'_sdk',return_value=f.sdk):
                    result=motion.plan_trajectory(planner,{'jointNames':['a','b'],'position':[0,0],'velocity':[0,0]},
                        {'tip':[0,0,0,1,0,0,0]})
                self.assertEqual(result['profileProvenance']['configSource'],'inline')
                self.assertIsNone(result['profileProvenance']['configFile'])
                self.assertEqual(result['profileProvenance']['sourceDigest'],hashlib.sha256(original).hexdigest())
                self.assertEqual(f.entry.read_bytes(),original)

    def test_inline_config_and_file_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory);f.profile['planning']['config']=f.config['robot_cfg']
            f.entry.write_text(json.dumps(f.profile))
            with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises(ValueError):
                motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
            backend.assert_not_called()

    def test_inline_config_rejects_escape_missing_files_and_sdk_asset_fallback(self):
        for defect in ('outside','missing','network','external_root','undeclared_spheres'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);config=self.inline_profile(f);kin=config['kinematics']
                if defect=='outside':
                    (f.root/'outside.urdf').write_text('<robot name="outside"/>');kin['urdf_path']=str(f.root/'outside.urdf')
                if defect=='missing':kin['urdf_path']='robot/franka_description/franka_panda.urdf'
                if defect=='network':kin['urdf_path']='https://example.invalid/robot.urdf'
                if defect=='external_root':kin['external_asset_path']=str(f.root)
                if defect=='undeclared_spheres':
                    (f.bundle/'unknown.yml').write_text('collision_spheres: {}');kin['collision_spheres']='unknown.yml'
                f.entry.write_text(json.dumps(f.profile))
                with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises(ValueError):
                    motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
                backend.assert_not_called()

    def test_inline_numeric_arrays_and_spheres_cannot_coerce_boolean_or_nonfinite(self):
        for defect in ('q_bool','lock_bool','center_bool','radius_bool','nonfinite'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                f=self.profile_fixture(directory);config=self.inline_profile(f);kin=config['kinematics']
                kin['collision_spheres']={'base':[{'center':[0,0,0],'radius':.1}]}
                if defect=='q_bool':kin['cspace']['retract_config']=[True,0]
                if defect=='lock_bool':kin['lock_joints']={'finger':True}
                if defect=='center_bool':kin['collision_spheres']['base'][0]['center']=[True,0,0]
                if defect=='radius_bool':kin['collision_spheres']['base'][0]['radius']=True
                if defect=='nonfinite':kin['cspace']['retract_config']=[float('nan'),0]
                f.entry.write_text(json.dumps(f.profile))
                with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises(ValueError):
                    motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
                backend.assert_not_called()

    def test_file_collision_numeric_bool_uses_the_same_native_data_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            f=self.profile_fixture(directory)
            (f.bundle/'files/planning/spheres.yml').write_text(yaml.safe_dump({'collision_spheres':{'base':[{'center':[False,0,0],'radius':.1}]}}))
            f.freeze()
            with patch.object(motion,'_sdk',return_value=f.sdk) as backend,self.assertRaises(ValueError):
                motion.create_planner(f.entry,{},resources=f.resources,io=f.io,port_map={'arm':'robot.arm'},interpolation_dt=.02)
            backend.assert_not_called()

    def test_legacy_dictionary_keeps_original_native_boolean_options_and_has_no_profile_metadata(self):
        sdk,_=self.sdk();config={'kinematics':{'use_usd_kinematics':False,'use_global_cumul':True}}
        with patch.object(motion,'_sdk',return_value=sdk):
            self.assertEqual(motion.create_planner(config,{},interpolation_dt=.02),'planner')
        self.assertEqual(sdk.Config.load_from_robot_config.call_args.args[0],config)


class TrajectoryExecutionTests(unittest.TestCase):
    def fixture(self):
        from copy import deepcopy
        clock={'physicsRateHz':200,'controlRateHz':50}
        def port(path,joints):
            return {'source':{'primitive':'joint_position','path':path,'joints':joints},
                'dtype':'float32','shape':[len(joints)],'units':['rad']*len(joints),
                'limits':[[None,None] for _ in joints],'physicalJointLimits':[[-2,2] for _ in joints],
                'persistence':'persistent','initialized':True}
        ports={'left':port('/World/left',['b','a']),'right':port('/World/right',['a']),
            'gripper':port('/World/left',['finger'])}
        streams={}
        for name,path,joints in [('left','/World/left',['a','b']),('right','/World/right',['a'])]:
            for suffix,reader,unit in [('q','joint_positions','rad'),('dq','joint_velocities','rad/s')]:
                streams[name+'_'+suffix]={'source':{'reader':reader,'path':path,'joints':joints},
                    'dtype':'float32','shape':[len(joints)],'units':[unit]*len(joints),
                    'origin':'native_output','frame':'native_joint_order','rateHz':50,
                    'visibleToPolicy':True,'required':True}
        io={'schema':'sceneagent-native-io-v1','clock':clock,'ports':ports,'streams':streams}
        positions=[[0.,.1,.2],[.02,.12,.22],[.04,.14,.24]]
        plan={'kind':'numerical_plan','actualExecution':False,'jointOrder':['model_a','model_b','model_c'],
            'start':{'jointNames':['model_a','model_b','model_c'],'position':positions[0],'velocity':[0,0,0]},
            'position':positions,'velocity':[[0,0,0]]*3,
            'timebase':{'units':'seconds','period':.02,'times':[0.,.02,.04]},
            'portJointIndices':{'left':[1,0],'right':[2]},
            'executionBinding':{'clock':deepcopy(clock),'ports':{k:deepcopy(ports[k]) for k in ('left','right')}}}
        def observation(i):
            return {'time':i*.02,'simulationTime':10+i*.02,'timeOrigin':10.,'physicsStep':i*4,'stageId':91,
                'streams':{'left_q':positions[i][:2],'left_dq':[0.,0.],
                    'right_q':[positions[i][2]],'right_dq':[0.]},
                'streamStatus':{name:{'status':'available','sampleTime':i*.02,'physicsStep':i*4} for name in streams},
                'cameraFrames':{}}
        opened={'io':io,'sensorSpec':{'arrayRoot':None},'observation':observation(0),
            'terminated':False,'truncated':False}
        replies=[{**observation(i),'terminated':False,'truncated':False} for i in (1,2)]
        env=SimpleNamespace(open=Mock(return_value=opened),step=Mock(side_effect=replies))
        kwargs={'feedback':{name:{'position':name+'_q','velocity':name+'_dq'} for name in ('left','right')},
            'start_tolerance':{'position':[1e-5]*3,'velocity':[1e-5]*3},'tracking_tolerance':[1e-5]*3}
        return SimpleNamespace(env=env,opened=opened,io=io,plan=plan,replies=replies,kwargs=kwargs)

    def test_profile_plan_binds_its_original_start_and_selected_native_io(self):
        from copy import deepcopy
        helper=NativeMotionTests()
        with tempfile.TemporaryDirectory() as directory:
            f=helper.profile_fixture(directory);planner=helper.profile_planner(f)
            start={'jointNames':['a','b'],'position':[0.,0.],'velocity':[0.,0.]}
            original=deepcopy(start)
            with patch.object(motion,'_sdk',return_value=f.sdk):
                plan=motion.plan_trajectory(planner,start,{'tip':[0,0,0,1,0,0,0]})
            self.assertEqual(plan['start'],original)
            self.assertEqual(plan['executionBinding'],{'clock':f.io['clock'],'ports':f.io['ports']})
            start['position'][0]=9;f.io['ports']['robot.arm']['source']['path']='changed'
            self.assertEqual(plan['start'],original)
            self.assertEqual(plan['executionBinding']['ports']['robot.arm']['source']['path'],'/World/robot')

    def test_all_points_keep_the_t0_clock_and_reorder_multiple_native_articulations(self):
        f=self.fixture()
        with patch.object(motion,'_sdk',side_effect=AssertionError('execution cannot invoke a solver')):
            result=motion.execute_trajectory(f.env,f.plan,**f.kwargs)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['requestedCycles'],2)
        self.assertEqual(result['returnedStepCalls'],2)
        self.assertEqual(result['confirmedPhysicalSteps'],8)
        self.assertEqual(result['totalPhysicalSteps'],8)
        self.assertEqual(result['lastConfirmedWaypoint'],2)
        self.assertNotIn('success',result)
        self.assertEqual(f.env.step.call_args_list[0].args,({'left':[.12,.02],'right':[.22]},.02))
        self.assertEqual(f.env.step.call_args_list[1].args,({'left':[.14,.04],'right':[.24]},.02))
        f.env.open.assert_called_once_with()

    def test_entire_path_and_holds_are_rejected_before_the_first_action(self):
        for defect in ('last_nan','last_shape','missing_time','time_order','period','zero_time','near_zero_time','bool_time',
                       'duplicate_index','missing_index','bool_index','wrong_source','unbound','wrong_start',
                       'wrong_initial_velocity','negative_tolerance','bool_tolerance','hold_overlap','hold_shape',
                       'uninitialized','missing_effort','physical_limit'):
            with self.subTest(defect=defect):
                f=self.fixture()
                if defect=='last_nan':f.plan['position'][-1][0]=float('nan')
                if defect=='last_shape':f.plan['position'][-1].pop()
                if defect=='missing_time':f.plan['timebase']['times'].pop()
                if defect=='time_order':f.plan['timebase']['times']=[0.,.04,.02]
                if defect=='period':f.plan['timebase']['period']=.01
                if defect=='zero_time':f.plan['timebase']['times'][0]=.001
                if defect=='near_zero_time':f.plan['timebase']['times'][0]=1e-10
                if defect=='bool_time':f.plan['timebase']['times'][0]=False
                if defect=='duplicate_index':f.plan['portJointIndices']['left']=[0,0]
                if defect=='missing_index':del f.plan['portJointIndices']['right']
                if defect=='bool_index':f.plan['portJointIndices']['right']=[True]
                if defect=='wrong_source':f.io['ports']['left']['source']['path']='/World/other'
                if defect=='unbound':del f.plan['executionBinding']
                if defect=='wrong_start':f.plan['start']['position']=[.3,.1,.2]
                if defect=='wrong_initial_velocity':f.opened['observation']['streams']['right_dq']=[.4]
                if defect=='negative_tolerance':f.kwargs['tracking_tolerance'][0]=-.1
                if defect=='bool_tolerance':f.kwargs['start_tolerance']['position'][0]=True
                if defect=='hold_overlap':f.kwargs['hold_action']={'left':[0,0]}
                if defect=='hold_shape':f.kwargs['hold_action']={'gripper':[0,0]}
                if defect=='uninitialized':f.io['ports']['gripper']['initialized']=False
                if defect=='missing_effort':f.io['ports']['gripper']['persistence']='per_physics_step'
                if defect=='physical_limit':f.plan['position'][-1][0]=3.
                with self.assertRaises(ValueError):motion.execute_trajectory(f.env,f.plan,**f.kwargs)
                f.env.step.assert_not_called()

    def test_explicit_nonplanned_commands_are_preserved_without_zero_fill(self):
        f=self.fixture();f.io['ports']['gripper']['initialized']=False
        f.kwargs['hold_action']={'gripper':[.031]}
        motion.execute_trajectory(f.env,f.plan,**f.kwargs)
        self.assertEqual([call.args[0]['gripper'] for call in f.env.step.call_args_list],[ [.031], [.031] ])

    def test_feedback_requires_exact_native_identity_permission_and_freshness(self):
        for defect in ('path','joint','hidden','authored','rate','stale','future','sample_nan','sample_bool_step','wrong_stage_clock','nonfinite'):
            with self.subTest(defect=defect):
                f=self.fixture();spec=f.io['streams']['left_q'];obs=f.opened['observation']
                if defect=='path':spec['source']['path']='/World/right'
                if defect=='joint':spec['source']['joints']=['unrelated','b']
                if defect=='hidden':spec['visibleToPolicy']=False
                if defect=='authored':spec['origin']='authored_configuration'
                if defect=='rate':spec['rateHz']=25
                if defect=='stale':obs['streamStatus']['left_q']['physicsStep']=1
                if defect=='future':obs['streamStatus']['left_q']['sampleTime']=.01
                if defect=='sample_nan':obs['streamStatus']['left_q']['sampleTime']=float('nan')
                if defect=='sample_bool_step':obs['streamStatus']['left_q']['physicsStep']=False
                if defect=='wrong_stage_clock':obs['simulationTime']=20.
                if defect=='nonfinite':obs['streams']['left_q'][0]=float('inf')
                with self.assertRaises(ValueError):motion.execute_trajectory(f.env,f.plan,**f.kwargs)
                f.env.step.assert_not_called()

    def test_committed_array_feedback_uses_existing_verified_read_only_reader(self):
        f=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'arrays').mkdir()
            value=np.asarray([0.,.1],dtype=np.float32);file=root/'arrays/q.npy';np.save(file,value)
            ref={'ref':'arrays/q.npy','sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'dtype':'float32','shape':[2]}
            f.opened['sensorSpec']['arrayRoot']=directory;f.opened['observation']['streams']['left_q']=ref
            from runtime.records import read_array
            with patch('runtime.records.read_array',wraps=read_array) as read:
                motion.execute_trajectory(f.env,f.plan,**f.kwargs)
            self.assertEqual(read.call_count,1)
            f.env.step.reset_mock();ref['sha256']='0'*64
            with self.assertRaises(ValueError):motion.execute_trajectory(f.env,f.plan,**f.kwargs)
            f.env.step.assert_not_called()

    def test_terminal_flags_at_open_and_after_each_reply_stop_the_next_command(self):
        for phase in ('open','step'):
            for flag in ('terminated','truncated'):
                with self.subTest(phase=phase,flag=flag):
                    f=self.fixture()
                    (f.opened if phase=='open' else f.replies[0])[flag]=True
                    result=motion.execute_trajectory(f.env,f.plan,**f.kwargs)
                    self.assertEqual(result['status'],flag)
                    self.assertEqual(f.env.step.call_count,0 if phase=='open' else 1)
                    self.assertEqual(result['returnedStepCalls'],0 if phase=='open' else 1)

    def test_tracking_failure_saves_the_actual_reply_and_stops_without_recovery(self):
        f=self.fixture();f.replies[0]['streams']['left_q']=[.5,.12]
        with self.assertRaises(RuntimeError) as raised:motion.execute_trajectory(f.env,f.plan,**f.kwargs)
        evidence=raised.exception.execution
        self.assertEqual(evidence['lastObservation'],f.replies[0])
        self.assertEqual(evidence['confirmedPhysicalSteps'],4)
        self.assertEqual(evidence['totalPhysicalSteps'],4)
        self.assertEqual(evidence['returnedStepCalls'],1)
        self.assertEqual(f.env.step.call_count,1)

    def test_failed_step_preserves_its_cause_and_confirmed_prefix_with_unknown_tail(self):
        f=self.fixture();cause=OSError('SDK failed after an unknown number of physical ticks')
        f.env.step.side_effect=[f.replies[0],cause]
        with self.assertRaises(RuntimeError) as raised:motion.execute_trajectory(f.env,f.plan,**f.kwargs)
        self.assertIs(raised.exception.__cause__,cause)
        evidence=raised.exception.execution
        self.assertEqual(evidence['status'],'failed')
        self.assertEqual(evidence['lastObservation'],f.replies[0])
        self.assertEqual(evidence['confirmedPhysicalSteps'],4)
        self.assertIsNone(evidence['totalPhysicalSteps'])
        self.assertEqual(evidence['countStatus'],'unknown')
        self.assertEqual(evidence['lastConfirmedWaypoint'],1)
        self.assertEqual(evidence['returnedStepCalls'],1)
        self.assertEqual(f.env.step.call_count,2)

    def test_changed_stage_clock_or_stale_reply_never_advances_to_a_later_waypoint(self):
        for defect in ('stage','origin','physics','time','sample'):
            with self.subTest(defect=defect):
                f=self.fixture();reply=f.replies[0]
                if defect=='stage':reply['stageId']=92
                if defect=='origin':reply['timeOrigin']=11.
                if defect=='physics':reply['physicsStep']=8
                if defect=='time':reply['time']=.04
                if defect=='sample':reply['streamStatus']['right_q']['physicsStep']=0
                with self.assertRaises(RuntimeError) as raised:motion.execute_trajectory(f.env,f.plan,**f.kwargs)
                self.assertEqual(raised.exception.execution['lastObservation'],
                    reply if defect=='sample' else f.opened['observation'])
                self.assertEqual(raised.exception.execution['countStatus'],'known' if defect=='sample' else 'unknown')
                self.assertEqual(f.env.step.call_count,1)


if __name__=='__main__':unittest.main()
