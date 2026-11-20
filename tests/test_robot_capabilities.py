"""SYSTEM_VERIFICATION_ONLY: native capability projection without SDK startup."""
from copy import deepcopy
import builtins
import hashlib
from importlib.util import find_spec
import io
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from runtime import robot,worker
from tests.test_native_records import IO

class RobotCapabilitiesTests(unittest.TestCase):
    def test_empty_capabilities_do_not_invent_a_robot_or_execute_a_solver(self):
        result=robot.robot_capabilities(None,'current')
        self.assertEqual(result['qualification'],'requires_scene_build')
        self.assertEqual(result['availableApis'],[])
        self.assertIsNone(result['io'])
        for key in ('nativeEntry','model','baseFrame','tcpFrame','verifiedCapabilities'):
            self.assertNotIn(key,result)

    def test_verified_scene_io_is_projected_without_task_or_model_qualification(self):
        io=deepcopy(IO);result=robot.robot_capabilities(io,'current')
        self.assertEqual(result['io'],io)
        self.assertEqual(result['availableApis'],['step'])
        self.assertEqual(result['numericalHelpers'],['runtime.motion.create_planner','runtime.motion.plan_trajectory'])
        self.assertEqual(result['qualification'],'frozen_native_io')
        self.assertIn('no model, policy or task-success qualification',result['scope'])
        result['io']['ports'].clear()
        self.assertEqual(io,IO)

    def test_metadata_derivation_does_not_import_or_start_sdk_or_cuda(self):
        original=builtins.__import__
        def checked(name,*args,**kwargs):
            if name.split('.')[0] in ('isaacsim','omni','pxr','torch','warp','curobo'):
                raise AssertionError('capability metadata imported native dependency')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=checked):
            self.assertEqual(robot.robot_capabilities(IO,'current')['qualification'],'frozen_native_io')

    def test_health_and_empty_inspection_share_unverified_capabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.object(worker,'runtime_key',return_value='current'),patch.object(worker,'runtime_versions',return_value={'fixture':True}):
                health=worker.run({'operation':'health','root':str(root)})
                empty=worker.inspect_project({'projectId':'system_test'},root)
            self.assertFalse(health['robotReady'])
            self.assertEqual(health['capabilities'],empty['capabilities'])
            self.assertEqual(health['capabilities']['qualification'],'requires_scene_build')

class RobotProfileTests(unittest.TestCase):
    def profile(self):
        return {'schema':'sceneagent-robot-profile-v1', 'model':{'resourceRef':'model-report'},
            'ports':{'arm':{'source':{'primitive':'joint_position','path':'.','joints':['a']}}},
            'streams':{'q':{'source':{'reader':'joint_positions','path':'.','joints':['a']},
                'visibleToPolicy':True,'required':True}}}

    def bundle(self, directory):
        root=Path(directory)/'bundle';(root/'files').mkdir(parents=True)
        (root/'files/model.usda').write_text('SYSTEM_VERIFICATION_ONLY resource bytes')
        (root/'files/model.yml').write_text('robot_cfg:\n  kinematics:\n    base_link: base\n    ee_link: tool\n    link_names: [tool]\n')
        self.report(root)
        return root,{'model-report':str(root/'files/model.usda')}

    def report(self, root):
        files=sorted(str(p.relative_to(root)) for p in (root/'files').rglob('*') if p.is_file())
        (root/'report.json').write_text(json.dumps({'kind':'native_files','entry':'files/model.usda',
            'nativeFiles':files,'frozenDigests':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}}))

    def test_profile_parser_is_data_only_and_preserves_the_public_schema(self):
        original=builtins.__import__
        def checked(name,*args,**kwargs):
            if name.split('.')[0] in ('isaacsim','omni','pxr','torch','warp','curobo'):
                raise AssertionError('pure profile parser imported SDK')
            return original(name,*args,**kwargs)
        value=self.profile()
        with patch('builtins.__import__',side_effect=checked):
            self.assertEqual(robot.parse_profile(json.dumps(value)),value)

    def test_profile_rejects_unknown_fields_false_facts_and_invalid_sources(self):
        mutations=[lambda p:p.update(score={'success':True}),
            lambda p:p['model'].update(factory='anything'),
            lambda p:p['ports']['arm'].update(dtype='float32'),
            lambda p:p['streams']['q'].update(origin='native_output'),
            lambda p:p['ports']['arm']['source'].update(path='../other'),
            lambda p:p['ports']['arm']['source'].update(joints=['a','a']),
            lambda p:p['streams']['q']['source'].update(reader='camera_frame'),
            lambda p:p['streams']['q']['source'].update(reader='contact_report'),
            lambda p:p['streams']['q'].update(rateHz=True),
            lambda p:p.update(initialJoints={'joints/a':{'position':True}}),
            lambda p:p.update(drives={'joints/a':{'stiffness':-1}}),
            lambda p:p.update(planning={'resourceRef':'model-report','ports':['missing']}),
            lambda p:p['model'].update(importOptions={'urdf_path':'/host/source'}),
            lambda p:p['model'].update(importOptions={'fix_base':1})]
        for mutate in mutations:
            value=self.profile();mutate(value)
            with self.subTest(value=value),self.assertRaises(ValueError):robot.parse_profile(json.dumps(value))
        for text in ('{"schema":"a","schema":"b"}',json.dumps(self.profile()).replace('true','NaN',1)):
            with self.subTest(text=text),self.assertRaises(ValueError):robot.parse_profile(text)

    def test_profile_files_require_the_explicit_frozen_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'nested/model.robot.json';path.parent.mkdir();path.write_text(json.dumps(self.profile()))
            with self.assertRaises(ValueError):robot.load_profile('nested/model.robot.json')
            self.assertEqual(robot.load_profile('nested/model.robot.json',root),self.profile())
            with self.assertRaises(ValueError):robot.load_profile(path,root/'other')
            (root/'linked.robot.json').symlink_to(path)
            with self.assertRaises(ValueError):robot.load_profile('linked.robot.json',root)
            with self.assertRaises(ValueError):robot.load_profile('../model.robot.json',root)

    def test_resource_resolver_checks_entry_membership_bytes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root,resources=self.bundle(directory)
            self.assertEqual(robot.resolve_resource({'resourceRef':'model-report'},resources),((root/'files/model.usda').resolve(),root.resolve()))
            self.assertEqual(robot.resolve_resource({'resourceRef':'model-report','file':'files/model.yml'},resources)[0],(root/'files/model.yml').resolve())
            for descriptor in ({'resourceRef':'missing'},{'resourceRef':'model-report','file':'../outside'},
                    {'resourceRef':'model-report','file':'report.json'},{'resourceRef':'model-report','file':'files/unknown'}):
                with self.subTest(descriptor=descriptor),self.assertRaises(ValueError):robot.resolve_resource(descriptor,resources)
            (root/'files/model.yml').write_text('changed')
            with self.assertRaisesRegex(ValueError,'digest|changed'):robot.resolve_resource({'resourceRef':'model-report','file':'files/model.yml'},resources)
            self.report(root);(root/'files/model.yml').unlink();(root/'files/model.yml').symlink_to(root/'files/model.usda')
            with self.assertRaises(ValueError):robot.resolve_resource({'resourceRef':'model-report','file':'files/model.yml'},resources)

    def test_resource_report_cannot_claim_a_different_entry_or_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root,resources=self.bundle(directory);path=root/'report.json';report=json.loads(path.read_text())
            report['entry']='files/model.yml';path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):robot.resolve_resource({'resourceRef':'model-report'},resources)

    def test_resource_override_still_requires_the_original_entry_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root,resources=self.bundle(directory)
            (root/'files/model.usda').write_text('changed anchor')
            with self.assertRaisesRegex(ValueError,'digest|changed'):
                robot.resolve_resource({'resourceRef':'model-report','file':'files/model.yml'},resources)

    def test_resource_descriptor_has_no_hidden_host_root_or_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            _,resources=self.bundle(directory)
            for field in ('bundleRoot','callback'):
                with self.subTest(field=field),self.assertRaises(ValueError):
                    robot.resolve_resource({'resourceRef':'model-report',field:'/host'},resources)

    def stage_fixture(self, directory, *, planning=False):
        from pxr import Gf,Usd,UsdGeom,UsdPhysics
        from runtime.scene import World
        root,resources=self.bundle(directory)
        path=root/'files/model.usda';path.unlink();source=Usd.Stage.CreateNew(str(path))
        asset=UsdGeom.Xform.Define(source,'/Asset');source.SetDefaultPrim(asset.GetPrim())
        UsdGeom.SetStageMetersPerUnit(source,1);UsdGeom.SetStageUpAxis(source,UsdGeom.Tokens.z)
        asset.AddTranslateOp().Set(Gf.Vec3d(.4,.2,0))
        body=UsdGeom.Xform.Define(source,'/Asset/body');UsdPhysics.RigidBodyAPI.Apply(body.GetPrim())
        body.AddTranslateOp().Set(Gf.Vec3d(0,0,.5))
        tool=UsdGeom.Xform.Define(source,'/Asset/body/tool');tool.AddTranslateOp().Set(Gf.Vec3d(0,0,.2))
        joint=UsdPhysics.RevoluteJoint.Define(source,'/Asset/joints/a')
        drive=UsdPhysics.DriveAPI.Apply(joint.GetPrim(),'angular');drive.CreateStiffnessAttr(4.)
        variants=asset.GetPrim().GetVariantSets().AddVariantSet('Appearance')
        variants.AddVariant('A');variants.AddVariant('B');variants.SetVariantSelection('A')
        source.GetRootLayer().Save();source=None;self.report(root)
        world=World();world.io['clock']={'physicsRateHz':200,'controlRateHz':50}
        world.resources=resources;world.source_root=Path(directory)/'sources';world.source_root.mkdir()
        world.stage=Usd.Stage.CreateNew(str(Path(directory)/'scene-source.usda'))
        default=UsdGeom.Xform.Define(world.stage,'/World');world.stage.SetDefaultPrim(default.GetPrim())
        profile=self.profile();profile['model']['variants']={'Appearance':'B'}
        if planning:
            profile['planning']={'resourceRef':'model-report','file':'files/model.yml','ports':['arm'],
                'linkMap':{'base':'body','tool':'body/tool'}}
            profile['streams']['body']={'source':{'reader':'rigid_pose','path':'body'},
                'rateHz':100,'visibleToPolicy':True,'required':True}
        (world.source_root/'model.robot.json').write_text(json.dumps(profile))
        return world,root,profile

    def test_usd_instances_keep_source_transforms_namespace_and_original_bytes(self):
        from pxr import UsdGeom
        with tempfile.TemporaryDirectory() as directory:
            world,root,_=self.stage_fixture(directory);before=(root/'files/model.usda').read_bytes()
            one=robot.load_robot(world,'9-arm','model.robot.json',at=(1,2,3))
            two=robot.load_robot(world,'other','model.robot.json',at=(4,0,0))
            self.assertEqual(one['ports'],{'arm':'9-arm.arm'});self.assertEqual(two['streams'],{'q':'other.q'})
            self.assertEqual(world.io['ports']['9-arm.arm']['source']['path'],one['primPath'])
            self.assertEqual(world.stage.GetPrimAtPath(one['primPath']).GetVariantSet('Appearance').GetVariantSelection(),'B')
            self.assertEqual(list(UsdGeom.Xformable(world.stage.GetPrimAtPath(one['primPath'])).GetLocalTransformation().ExtractTranslation()),[.4,.2,0.])
            self.assertEqual(list(UsdGeom.Xformable(world.stage.GetPrimAtPath(one['mountPath'])).GetLocalTransformation().ExtractTranslation()),[1.,2.,3.])
            self.assertEqual((root/'files/model.usda').read_bytes(),before)
            json.dumps(one,allow_nan=False)

    def test_frame_bindings_read_the_real_body_and_keep_fixed_offset_derived(self):
        with tempfile.TemporaryDirectory() as directory:
            world,_,_=self.stage_fixture(directory,planning=True)
            result=robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(set(result['frames']),{'base','tool'})
            base,tool=result['frames']['base'],result['frames']['tool']
            self.assertEqual(base['stream'],tool['stream'])
            self.assertEqual(world.io['streams'][tool['stream']]['source'],{'reader':'rigid_pose','path':result['primPath']+'/body'})
            self.assertAlmostEqual(tool['bodyFromLink'][2][3],.2)
            self.assertNotIn('native_output',json.dumps(tool))

    def test_inline_native_planning_config_does_not_read_the_model_entry_as_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory)
            config={'kinematics':{'base_link':'base','ee_link':'tool','link_names':['tool'],'use_global_cumul':True}}
            profile['planning']={'resourceRef':'model-report','ports':['arm'],'config':config,
                'linkMap':{'base':'body','tool':'body/tool'}}
            profile['streams']['body']={'source':{'reader':'rigid_pose','path':'body'},
                'rateHz':100,'visibleToPolicy':True,'required':True}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            self.assertEqual(robot.parse_profile(json.dumps(profile))['planning']['config'],config)
            self.assertEqual(robot.resolve_resource(profile['planning'],world.resources)[0],(root/'files/model.usda').resolve())
            with patch('yaml.safe_load',side_effect=AssertionError('inline config parsed resource entry as YAML')):
                result=robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(set(result['frames']),{'base','tool'})

    def test_inline_planning_is_inner_native_data_and_excludes_file(self):
        for config,with_file in [({'robot_cfg':{'kinematics':{}}},False),([],False),({'kinematics':{}},True)]:
            profile=self.profile();profile['planning']={'resourceRef':'model-report','ports':['arm'],'config':config}
            if with_file:profile['planning']['file']='files/model.yml'
            with self.subTest(config=config,with_file=with_file),self.assertRaises(ValueError):robot.parse_profile(json.dumps(profile))

    def test_native_config_validation_keeps_flags_but_rejects_bool_numeric_positions(self):
        config={'kinematics':{'use_global_cumul':True,'cspace':{'retract_config':[0.1]}}}
        self.assertIs(robot.validate_planning_config(config),config)
        invalid=[{'cspace':{'retract_config':[True]}},{'cspace':{'max_acceleration':True}},
            {'lock_joints':{'a':True}},{'collision_sphere_buffer':False},
            {'collision_spheres':{'body':[{'center':[0,True,0],'radius':.1}]}},
            {'extra_links':{'tool':{'fixed_transform':[0,0,0,True,0,0,0]}}}]
        for kin in invalid:
            with self.subTest(kin=kin),self.assertRaises(ValueError):robot.validate_planning_config({'kinematics':kin})
        for value in (float('inf'),(1,2),object()):
            with self.subTest(value=value),self.assertRaises(ValueError):robot.validate_planning_config({'kinematics':{},'value':value})
        profile=self.profile();profile['planning']={'resourceRef':'model-report','ports':['arm'],'config':{'kinematics':{},'value':1.}}
        with self.assertRaises(ValueError):robot.parse_profile(json.dumps(profile).replace('1.0','1e309'))

    def test_tool_reset_stack_is_not_a_fixed_child_of_its_raw_body(self):
        from pxr import Usd,UsdGeom
        with tempfile.TemporaryDirectory() as directory:
            world,root,_=self.stage_fixture(directory,planning=True)
            source=Usd.Stage.Open(str(root/'files/model.usda'))
            UsdGeom.Xformable(source.GetPrimAtPath('/Asset/body/tool')).SetResetXformStack(True)
            source.GetRootLayer().Save();source=None;self.report(root)
            with self.assertRaisesRegex(ValueError,'reset|fixed'):robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(world.nodes,{})

    def test_selected_variant_cannot_reset_the_instance_installation(self):
        from pxr import Usd,UsdGeom
        with tempfile.TemporaryDirectory() as directory:
            world,root,_=self.stage_fixture(directory)
            source=Usd.Stage.Open(str(root/'files/model.usda'));variants=source.GetDefaultPrim().GetVariantSet('Appearance')
            transform=UsdGeom.Xformable(source.GetDefaultPrim())
            original=transform.GetXformOpOrderAttr().Get();transform.GetXformOpOrderAttr().Clear()
            variants.SetVariantSelection('A')
            with variants.GetVariantEditContext():transform.GetXformOpOrderAttr().Set(original)
            variants.SetVariantSelection('B')
            with variants.GetVariantEditContext():transform.SetResetXformStack(True)
            self.assertTrue(transform.GetResetXformStack())
            variants.SetVariantSelection('A');source.GetRootLayer().Save();source=None;self.report(root)
            with self.assertRaisesRegex(ValueError,'reset|installation'):robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(world.nodes,{})

    def test_failure_removes_only_new_instance_and_keeps_world_maps_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            world,_,profile=self.stage_fixture(directory);old=deepcopy(world.io)
            profile['model']['variants']={'Appearance':'missing'}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            with self.assertRaises(ValueError):robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(world.io,old);self.assertEqual(world.nodes,{})
            self.assertEqual([str(p.GetPath()) for p in world.stage.Traverse()],['/World'])

    def test_drives_are_explicit_si_configuration_not_physical_limit_overrides(self):
        from pxr import UsdPhysics
        with tempfile.TemporaryDirectory() as directory:
            world,_,profile=self.stage_fixture(directory)
            profile['drives']={'joints/a':{'stiffness':10.,'damping':2.,'targetPosition':math.pi/2,'targetVelocity':math.pi}}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            result=robot.load_robot(world,'r','model.robot.json')
            drive=UsdPhysics.DriveAPI(world.stage.GetPrimAtPath(result['primPath']+'/joints/a'),'angular')
            self.assertAlmostEqual(drive.GetStiffnessAttr().Get(),10*math.pi/180,places=6)
            self.assertAlmostEqual(drive.GetDampingAttr().Get(),2*math.pi/180,places=6)
            self.assertAlmostEqual(drive.GetTargetPositionAttr().Get(),90.)
            self.assertAlmostEqual(drive.GetTargetVelocityAttr().Get(),180.)

    def test_urdf_conversion_uses_private_output_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory)
            urdf=root/'files/model.urdf';urdf.write_text('<robot name="fixture"><link name="body"/></robot>');self.report(root)
            profile['model']={'resourceRef':'model-report','file':'files/model.urdf','importOptions':{'fix_base':True,'merge_fixed_joints':False}}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile));before=urdf.read_bytes()
            def converted(path, output, options):
                self.assertEqual(path,urdf.resolve());self.assertEqual(options,profile['model']['importOptions'])
                self.assertTrue(output.is_relative_to(Path(directory).resolve()/'robot-imports'))
                output.mkdir(parents=True);target=output/'model.usda';shutil.copyfile(root/'files/model.usda',target)
                return target
            with patch.object(robot,'_import_urdf',side_effect=converted) as importer:
                robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(importer.call_count,1);self.assertEqual(urdf.read_bytes(),before)

    def test_fixed_frames_reuse_imported_display_names_and_ignore_joints(self):
        from pxr import Usd,UsdGeom,UsdPhysics
        for child in ('tool','tool-tip'):
            with self.subTest(child=child):
                stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
                parent=UsdGeom.Xform.Define(stage,'/Robot/imported_base').GetPrim();parent.SetDisplayName('base-link')
                frame=UsdGeom.Xform.Define(stage,'/Robot/imported_base/imported_tool').GetPrim();frame.SetDisplayName(child)
                joint=UsdPhysics.FixedJoint.Define(stage,'/Robot/joints/tool').GetPrim();joint.SetDisplayName(child)
                source=io.StringIO('<robot><joint type="fixed"><parent link="base-link"/><child link="'+child+'"/></joint></robot>')
                before=stage.GetRootLayer().ExportToString()
                robot._restore_fixed_frames(stage,model,source)
                self.assertEqual(stage.GetRootLayer().ExportToString(),before)

    @unittest.skipUnless(find_spec('usdex'), 'requires the OpenUSD Exchange SDK')
    def test_missing_fixed_frames_use_official_names_and_keep_offsets_without_overwrite(self):
        import usdex.core
        from pxr import Usd,UsdGeom,UsdPhysics
        stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
        parent=UsdGeom.Xform.Define(stage,'/Robot/base').GetPrim();parent.SetDisplayName('base-link')
        referenced=Usd.Stage.CreateInMemory();UsdGeom.Xform.Define(referenced,'/Reserved')
        UsdPhysics.FixedJoint.Define(referenced,'/Reserved/tool')
        parent.GetReferences().AddReference(referenced.GetRootLayer().identifier,'/Reserved')
        occupied=stage.GetPrimAtPath('/Robot/base/tool')
        names=usdex.core.NameCache()
        expected={name:names.getPrimName(parent,name) for name in ('tool','tool-tip')}
        source='<robot><joint type="fixed"><parent link="base-link"/><child link="tool"/><origin xyz="0 0 .2"/></joint><joint type="fixed"><parent link="base-link"/><child link="tool-tip"/><origin xyz=".1 0 0" rpy="0 0 1.5707963267948966"/></joint><joint type="fixed"><parent link="tool-tip"/><child link="camera mount"/><origin xyz="0 .3 0"/></joint></robot>'
        robot._restore_fixed_frames(stage,model,io.StringIO(source))
        self.assertTrue(occupied.IsA(UsdPhysics.FixedJoint))
        for original,name in expected.items():
            frame=stage.GetPrimAtPath(parent.GetPath().AppendChild(name))
            self.assertTrue(UsdGeom.Xformable(frame));self.assertEqual(frame.GetDisplayName(),original)
        tool=UsdGeom.Xformable(stage.GetPrimAtPath(parent.GetPath().AppendChild(expected['tool'])))
        self.assertEqual(list(tool.GetLocalTransformation().ExtractTranslation()),[0.,0.,.2])
        tip=stage.GetPrimAtPath(parent.GetPath().AppendChild(expected['tool-tip']))
        matrix=UsdGeom.Xformable(tip).GetLocalTransformation()
        self.assertAlmostEqual(matrix[0][1],1.);self.assertAlmostEqual(matrix[1][0],-1.)
        camera=[prim for prim in tip.GetChildren() if prim.GetDisplayName()=='camera mount']
        self.assertEqual(len(camera),1)
        self.assertEqual(list(UsdGeom.Xformable(camera[0]).GetLocalTransformation().ExtractTranslation()),[0.,.3,0.])
        before=stage.GetRootLayer().ExportToString()
        robot._restore_fixed_frames(stage,model,io.StringIO(source))
        self.assertEqual(stage.GetRootLayer().ExportToString(),before)

    def test_fixed_frame_names_remain_ambiguous_across_different_prims(self):
        from pxr import Usd,UsdGeom
        stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
        UsdGeom.Xform.Define(stage,'/Robot/base');UsdGeom.Xform.Define(stage,'/Robot/tool')
        UsdGeom.Xform.Define(stage,'/Robot/other').GetPrim().SetDisplayName('tool')
        source=io.StringIO('<robot><joint type="fixed"><parent link="base"/><child link="tool"/></joint></robot>')
        with self.assertRaisesRegex(ValueError,'ambiguous'):robot._restore_fixed_frames(stage,model,source)

    @unittest.skipUnless(find_spec('usdex'), 'requires the OpenUSD Exchange SDK')
    def test_official_link_identity_excludes_same_named_geometry_and_restores_fixed_chains(self):
        from pxr import Usd,UsdGeom,UsdShade
        stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
        parent=UsdGeom.Xform.Define(stage,'/Robot/Geometry/base_link').GetPrim();parent.AddAppliedSchema('IsaacLinkAPI')
        visual=UsdGeom.Xform.Define(stage,'/Robot/Geometry/base_link/base');visual.AddTranslateOp().Set((.4,.2,.1))
        UsdShade.MaterialBindingAPI.Apply(visual.GetPrim())
        asset=Usd.Stage.CreateInMemory();UsdGeom.Xform.Define(asset,'/Asset')
        mesh=UsdGeom.Mesh.Define(asset,'/Asset/base_1');mesh.GetPrim().SetDisplayName('base')
        instance=UsdGeom.Xform.Define(stage,'/Robot/Geometry/base_link/base_1').GetPrim();instance.SetDisplayName('base')
        instance.GetReferences().AddReference(asset.GetRootLayer().identifier,'/Asset');instance.SetInstanceable(True)
        before=visual.GetPrim().GetAllMetadata(),visual.GetLocalTransformation(),asset.GetRootLayer().ExportToString()
        source='<robot><joint type="fixed"><parent link="base_link"/><child link="base"/><origin rpy="0 0 3.141592653589793"/></joint><joint type="fixed"><parent link="base"/><child link="tip"/><origin xyz="0 0 .2"/></joint></robot>'
        robot._restore_fixed_frames(stage,model,io.StringIO(source))
        prims=list(Usd.PrimRange(model,Usd.TraverseInstanceProxies()))
        frames=robot._model_link_matches(prims,'base');self.assertEqual(len(frames),1)
        frame=frames[0];self.assertNotEqual(frame.GetPath(),visual.GetPath())
        self.assertEqual(frame.GetCustomDataByKey('sceneagent:urdfLink'),'base')
        self.assertEqual(frame.GetDisplayName(),'base')
        matrix=UsdGeom.Xformable(frame).GetLocalTransformation()
        self.assertAlmostEqual(matrix[0][0],-1.);self.assertAlmostEqual(matrix[1][1],-1.)
        tip=robot._model_link_matches(prims,'tip');self.assertEqual(len(tip),1)
        self.assertEqual(tip[0].GetParent(),frame)
        self.assertEqual(list(UsdGeom.Xformable(tip[0]).GetLocalTransformation().ExtractTranslation()),[0.,0.,.2])
        self.assertEqual((visual.GetPrim().GetAllMetadata(),visual.GetLocalTransformation(),asset.GetRootLayer().ExportToString()),before)
        self.assertFalse(any('Physics' in name for name in frame.GetPrimTypeInfo().GetAppliedAPISchemas()))
        frozen=stage.GetRootLayer().ExportToString()
        robot._restore_fixed_frames(stage,model,io.StringIO(source))
        self.assertEqual(stage.GetRootLayer().ExportToString(),frozen)

    def test_official_link_identity_keeps_true_link_ambiguity_and_requires_exact_restored_name(self):
        from pxr import Usd,UsdGeom
        stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
        for path in ('base','tool','other'):
            prim=UsdGeom.Xform.Define(stage,'/Robot/'+path).GetPrim();prim.AddAppliedSchema('IsaacLinkAPI')
        stage.GetPrimAtPath('/Robot/other').SetDisplayName('tool')
        fake=UsdGeom.Xform.Define(stage,'/Robot/fake').GetPrim();fake.SetDisplayName('wrong')
        fake.SetCustomDataByKey('sceneagent:urdfLink','another_link')
        scope=stage.DefinePrim('/Robot/not_a_frame','Scope');scope.SetCustomDataByKey('sceneagent:urdfLink','not_a_frame')
        prims=list(Usd.PrimRange(model))
        self.assertEqual(robot._model_link_matches(prims,'wrong'),[])
        self.assertEqual(robot._model_link_matches(prims,'not_a_frame'),[])
        source=io.StringIO('<robot><joint type="fixed"><parent link="base"/><child link="tool"/></joint></robot>')
        with self.assertRaisesRegex(ValueError,'ambiguous'):robot._restore_fixed_frames(stage,model,source)

    @unittest.skipUnless(find_spec('usdex'), 'requires the OpenUSD Exchange SDK')
    def test_fixed_frame_creation_does_not_reuse_an_occupied_name_cache_target(self):
        import usdex.core
        from pxr import Usd,UsdGeom
        stage=Usd.Stage.CreateInMemory();model=UsdGeom.Xform.Define(stage,'/Robot').GetPrim()
        parent=UsdGeom.Xform.Define(stage,'/Robot/base').GetPrim();parent.AddAppliedSchema('IsaacLinkAPI')
        occupied=UsdGeom.Xform.Define(stage,'/Robot/base/tool');occupied.AddTranslateOp().Set((1,2,3))
        before=stage.GetRootLayer().ExportToString()
        source=io.StringIO('<robot><joint type="fixed"><parent link="base"/><child link="tool"/></joint></robot>')
        with patch.object(usdex.core,'NameCache') as names:
            names.return_value.getPrimName.return_value='tool'
            with self.assertRaisesRegex(ValueError,'exist|occupied'):robot._restore_fixed_frames(stage,model,source)
        self.assertEqual(stage.GetRootLayer().ExportToString(),before)

    def test_planning_auto_names_use_official_links_and_restored_frames_instead_of_visuals(self):
        from pxr import Usd,UsdGeom
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory,planning=True)
            source=Usd.Stage.Open(str(root/'files/model.usda'));source.GetPrimAtPath('/Asset/body').AddAppliedSchema('IsaacLinkAPI')
            frame=source.GetPrimAtPath('/Asset/body/tool');frame.SetCustomDataByKey('sceneagent:urdfLink','tool')
            visual=UsdGeom.Xform.Define(source,'/Asset/body/tool_visual').GetPrim();visual.SetDisplayName('tool')
            profile['planning'].pop('linkMap');profile['planning'].pop('file')
            profile['planning']['config']={'kinematics':{'base_link':'body','ee_link':'tool','link_names':['tool']}}
            source.GetRootLayer().Save();source=None;self.report(root)
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            result=robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(set(result['frames']),{'body','tool'})
            self.assertAlmostEqual(result['frames']['tool']['bodyFromLink'][2][3],.2)

    def test_planning_matches_imported_display_names_and_excludes_joints(self):
        from pxr import Usd,UsdPhysics
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory,planning=True)
            source=Usd.Stage.Open(str(root/'files/model.usda'))
            source.GetPrimAtPath('/Asset/body').SetDisplayName('base-link')
            source.GetPrimAtPath('/Asset/body/tool').SetDisplayName('tool-tip')
            UsdPhysics.FixedJoint.Define(source,'/Asset/joints/tool').GetPrim().SetDisplayName('tool-tip')
            source.GetRootLayer().Save();source=None;self.report(root)
            profile['planning'].pop('linkMap')
            profile['planning'].pop('file')
            profile['planning']['config']={'kinematics':{'base_link':'base-link','ee_link':'tool-tip','link_names':['tool-tip']}}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            result=robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(set(result['frames']),{'base-link','tool-tip'})
            self.assertAlmostEqual(result['frames']['tool-tip']['bodyFromLink'][2][3],.2)

    def test_planning_name_ambiguity_requires_explicit_link_map(self):
        from pxr import Usd
        for explicit in (False,True):
            with tempfile.TemporaryDirectory() as directory,self.subTest(explicit=explicit):
                world,root,profile=self.stage_fixture(directory,planning=True)
                source=Usd.Stage.Open(str(root/'files/model.usda'))
                source.GetPrimAtPath('/Asset/body').SetDisplayName('tool')
                source.GetRootLayer().Save();source=None;self.report(root)
                if not explicit:profile['planning']['linkMap'].pop('tool')
                (world.source_root/'model.robot.json').write_text(json.dumps(profile))
                if explicit:
                    result=robot.load_robot(world,'r','model.robot.json')
                    self.assertAlmostEqual(result['frames']['tool']['bodyFromLink'][2][3],.2)
                else:
                    with self.assertRaisesRegex(ValueError,'unique|ambiguous'):robot.load_robot(world,'r','model.robot.json')

    def test_failed_import_never_deletes_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory)
            urdf=root/'files/model.urdf';urdf.write_text('<robot name="fixture"/>');self.report(root)
            profile['model']={'resourceRef':'model-report','file':'files/model.urdf'}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            existing=Path(directory)/'robot-imports'/b'r'.hex();existing.mkdir(parents=True);(existing/'preserve').write_text('old')
            with self.assertRaises(ValueError):robot.load_robot(world,'r','model.robot.json')
            self.assertEqual((existing/'preserve').read_text(),'old')

    def test_explicit_hidden_and_optional_frame_stream_permissions_are_not_bypassed(self):
        for visible,required in ((False,False),(False,True),(True,False),(True,True)):
            with tempfile.TemporaryDirectory() as directory,self.subTest(visible=visible,required=required):
                world,_,profile=self.stage_fixture(directory,planning=True)
                declaration={'source':{'reader':'rigid_pose','path':'body'},'rateHz':100,
                    'visibleToPolicy':visible,'required':required}
                profile['streams']['body']=declaration
                original=json.dumps(profile);(world.source_root/'model.robot.json').write_text(original)
                result=robot.load_robot(world,'r','model.robot.json')
                self.assertEqual(result['frames']['base']['stream'],'r.body')
                self.assertEqual(result['frames']['tool']['stream'],'r.body')
                expected={**declaration,'source':{'reader':'rigid_pose','path':result['primPath']+'/body'}}
                self.assertEqual(world.io['streams']['r.body'],expected)
                self.assertFalse(any('.__frame_' in name for name in world.io['streams']))
                self.assertEqual((world.source_root/'model.robot.json').read_text(),original)
                # Actual producer + observation permission code; numeric readback is a CPU fixture only.
                from contextlib import nullcontext
                from types import ModuleType
                import sys
                import numpy as np
                from runtime import simulation
                env=object.__new__(simulation.Environment)
                env.io={'streams':{'r.body':{**world.io['streams']['r.body'],'dtype':'float64','shape':[7]}}}
                env._closed=False;env._stream_cache={};env.physics_rate=200;env.latest_camera_frames={};env.task=None
                clock={'time':0.,'simulationTime':.01,'timeOrigin':.01,'physicsStep':0,'stageId':9}
                env._clock=lambda:dict(clock)
                env._read_source=lambda *_args,**_kwargs:np.asarray([0,0,0,1,0,0,0],dtype=np.float64)
                names=['isaacsim','isaacsim.core','isaacsim.core.experimental','isaacsim.core.experimental.utils',
                    'isaacsim.core.experimental.utils.backend']
                modules={name:ModuleType(name) for name in names}
                modules[names[-1]].use_backend=lambda *_args,**_kwargs:nullcontext()
                with patch.dict(sys.modules,modules):
                    public=env._observe_raw(truth=False);truth=env._observe_raw(truth=True)
                self.assertEqual('r.body' in public['streams'],visible)
                self.assertIn('r.body',truth['streams'])

    def test_planning_frame_requires_an_explicit_unique_sufficiently_sampled_body_stream(self):
        for failure in ('missing','slow','ambiguous'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(failure=failure):
                world,_,profile=self.stage_fixture(directory,planning=True)
                declaration={'source':{'reader':'rigid_pose','path':'body'},'rateHz':25 if failure=='slow' else 100,
                    'visibleToPolicy':False,'required':False}
                if failure!='missing':profile['streams']['body']=declaration
                else:profile['streams'].pop('body')
                if failure=='ambiguous':profile['streams']['alias']=deepcopy(declaration)
                (world.source_root/'model.robot.json').write_text(json.dumps(profile))
                with self.assertRaisesRegex(ValueError,'body|ambiguous'):robot.load_robot(world,'r','model.robot.json')
                self.assertEqual(world.nodes,{})
                self.assertEqual(world.io['streams'],{})
                self.assertEqual(world.io['ports'],{})

    def test_failure_preserves_existing_world_values_without_copying_them(self):
        with tempfile.TemporaryDirectory() as directory:
            world,_,profile=self.stage_fixture(directory)
            world.space('existing');node=world.nodes['existing']
            previous={'source':{'reader':'rigid_pose','path':'/World/existing'}}
            world.io['streams']['existing']=previous
            profile['model']['variants']={'missing':'variant'}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            with self.assertRaises(ValueError):robot.load_robot(world,'r','model.robot.json')
            self.assertIs(world.nodes['existing'],node);self.assertIs(world.io['streams']['existing'],previous)

    def test_usd_source_units_and_up_axis_are_not_silently_reinterpreted(self):
        from pxr import Usd,UsdGeom
        for metadata in ('units','axis'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(metadata=metadata):
                world,root,_=self.stage_fixture(directory)
                source=Usd.Stage.Open(str(root/'files/model.usda'))
                if metadata=='units':UsdGeom.SetStageMetersPerUnit(source,.01)
                else:UsdGeom.SetStageUpAxis(source,UsdGeom.Tokens.y)
                source.GetRootLayer().Save();source=None;self.report(root)
                with self.assertRaisesRegex(ValueError,'metre|Z-up|units'):robot.load_robot(world,'r','model.robot.json')
                self.assertEqual(world.nodes,{})

    def test_model_reset_stack_cannot_bypass_the_instance_installation(self):
        from pxr import Usd,UsdGeom
        with tempfile.TemporaryDirectory() as directory:
            world,root,_=self.stage_fixture(directory)
            source=Usd.Stage.Open(str(root/'files/model.usda'))
            UsdGeom.Xformable(source.GetDefaultPrim()).SetResetXformStack(True)
            source.GetRootLayer().Save();source=None;self.report(root)
            with self.assertRaisesRegex(ValueError,'reset|installation'):robot.load_robot(world,'r','model.robot.json',at=(1,2,3))

    def test_urdf_cannot_resolve_meshes_outside_the_declared_native_package(self):
        for filename in ('../outside.obj','package://unknown/mesh.obj'):
            with tempfile.TemporaryDirectory() as directory,self.subTest(filename=filename):
                world,root,profile=self.stage_fixture(directory)
                urdf=root/'files/model.urdf';urdf.write_text('<robot name="fixture"><link name="body"><visual><geometry><mesh filename="'+filename+'"/></geometry></visual></link></robot>');self.report(root)
                profile['model']={'resourceRef':'model-report','file':'files/model.urdf'}
                (world.source_root/'model.robot.json').write_text(json.dumps(profile))
                with patch.object(robot,'_import_urdf') as importer,self.assertRaises(ValueError):
                    robot.load_robot(world,'r','model.robot.json')
                importer.assert_not_called()

    def test_official_urdf_multiphysics_output_selects_the_fixed_physx_backend(self):
        from pxr import Usd
        with tempfile.TemporaryDirectory() as directory:
            world,root,profile=self.stage_fixture(directory)
            source=Usd.Stage.Open(str(root/'files/model.usda'));variants=source.GetDefaultPrim().GetVariantSets().AddVariantSet('Physics')
            variants.AddVariant('Newton');variants.AddVariant('Physx');variants.SetVariantSelection('Newton')
            source.GetRootLayer().Save();source=None
            urdf=root/'files/model.urdf';urdf.write_text('<robot name="fixture"><link name="body"/></robot>');self.report(root)
            profile['model']={'resourceRef':'model-report','file':'files/model.urdf'}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            def converted(path, output, options):
                output.mkdir(parents=True);target=output/'model.usda';shutil.copyfile(root/'files/model.usda',target);return target
            with patch.object(robot,'_import_urdf',side_effect=converted):result=robot.load_robot(world,'r','model.robot.json')
            self.assertEqual(world.stage.GetPrimAtPath(result['primPath']).GetVariantSet('Physics').GetVariantSelection(),'Physx')

    def test_initial_joint_state_uses_real_physx_schema_when_available(self):
        try:
            from pxr import PhysxSchema
        except ImportError:
            self.skipTest('PhysxSchema is only available in the frozen Linux SDK; pure USD drive tests run here')
        with tempfile.TemporaryDirectory() as directory:
            world,_,profile=self.stage_fixture(directory)
            profile['initialJoints']={'joints/a':{'position':math.pi/2,'velocity':math.pi}}
            (world.source_root/'model.robot.json').write_text(json.dumps(profile))
            result=robot.load_robot(world,'r','model.robot.json')
            state=PhysxSchema.JointStateAPI(world.stage.GetPrimAtPath(result['primPath']+'/joints/a'),'angular')
            self.assertAlmostEqual(state.GetPositionAttr().Get(),90.)
            self.assertAlmostEqual(state.GetVelocityAttr().Get(),180.)


if __name__=='__main__':unittest.main()
