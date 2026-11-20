"""SYSTEM_VERIFICATION_ONLY: frozen native construction, never generated products."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.scene import World, Task
from runtime import worker


class PublicRobotProfileSourceTests(unittest.TestCase):
    def files(self):
        profile = {'schema': 'sceneagent-robot-profile-v1',
            'model': {'resourceRef': 'assets/prepared/model/report.json'},
            'ports': {'arm': {'source': {'primitive': 'joint_position', 'path': '.', 'joints': ['joint']}}},
            'streams': {}}
        return {'scene.py': 'def build(world):\n    pass\n',
                'tasks.py': 'def build(world):\n    return []\n',
                'expert.py': 'def run(env, task):\n    pass\n',
                'rubrics.py': 'def evaluate(task, facts, state):\n    pass\n',
                'case.py': 'def run(env, task):\n    pass\n',
                'robots/arm.robot.json': json.dumps(profile)}

    def test_environment_source_selection_is_exact_before_builder_mounts(self):
        files = self.files()
        refs = ['scene.py', 'tasks.py', 'rubrics.py', 'case.py', 'robots/arm.robot.json']
        selected = worker.source_files({'sourceFiles': files, 'environmentSourceRefs': refs})
        self.assertEqual(set(selected), set(refs))
        self.assertNotIn('expert.py', selected)
        self.assertEqual(worker.source_files({'sourceFiles': {k:v for k,v in files.items() if k != 'expert.py'}}),
                         {k:v for k,v in files.items() if k != 'expert.py'})
        for invalid in (refs+['expert.py'], refs+['missing.py'], refs+['scene.py'], ['scene.py']):
            with self.subTest(refs=invalid), self.assertRaises(ValueError):
                worker.source_files({'sourceFiles': files, 'environmentSourceRefs': invalid})
        files['scene.py'] = 'import private_helper\n'
        files['private_helper.py'] = 'value = 1\n'
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            worker.source_files({'sourceFiles': files, 'environmentSourceRefs': refs})

    def test_profile_is_frozen_as_data_and_never_parsed_as_python(self):
        files = self.files()
        self.assertEqual(worker.source_files({'sourceFiles': files}), files)
        parse = worker.ast.parse
        with patch.object(worker.ast, 'parse', wraps=parse) as called:
            worker.import_locks(files)
        self.assertTrue(all(call.args[1].endswith('.py') for call in called.call_args_list))

    def test_expert_receives_public_profiles_without_private_python(self):
        files = self.files()
        task = Task('t', rubric={'sourceRef': 'rubrics.py', 'helperRefs': []},
            evaluation={'cases': [{'id': 'case', 'sourceRef': 'case.py', 'helperRefs': [],
                'expected': {'success': False}, 'requirementRefs': ['SYSTEM_VERIFICATION_ONLY']}]})
        digests = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
        selected = worker.expert_sources(task, files, digests)
        self.assertEqual(set(selected['sourceFiles']), {'expert.py', 'robots/arm.robot.json'})
        self.assertEqual(selected['helperDigests']['robots/arm.robot.json'], digests['robots/arm.robot.json'])

    def test_data_helper_is_explicit_and_cannot_be_the_executable_entry(self):
        files = self.files()
        declaration = {'sourceRef': 'case.py', 'helperRefs': ['robots/arm.robot.json']}
        self.assertEqual(set(worker.source_closure(declaration, files)['sourceFiles']),
                         {'case.py', 'robots/arm.robot.json'})
        with self.assertRaises(ValueError):
            worker.source_closure({'sourceRef': 'robots/arm.robot.json', 'helperRefs': []}, files)
        bad = json.loads(files['robots/arm.robot.json']);bad['privateRubricState'] = {'answer': 1}
        with self.assertRaises(ValueError):
            worker.import_locks({'robots/arm.robot.json': json.dumps(bad)})

    def test_program_can_bind_a_real_frozen_filename_without_changing_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory)/'expert.py'
            scope = worker.program('from pathlib import Path\nlocation = Path(__file__)\n',
                                   'expert', filename=entry)
            self.assertEqual(scope['location'], entry.resolve())


class NativeWorldTests(unittest.TestCase):
    def test_native_bindings_roundtrip_without_a_robot_or_physics_kind(self):
        world = World()
        world.space('room')
        world.bind('unlisted_entity', '/World/Anything', owner='room', purpose='native SDK resource')
        world.io = {'clock': {'physicsRateHz': 200, 'controlRateHz': 50}, 'ports': {}, 'streams': {}}
        value = world.snapshot()
        self.assertNotIn('robot', value)
        self.assertNotIn('stage', value)
        self.assertEqual(value['nodes'][1]['primPath'], '/World/Anything')
        self.assertNotIn('mass', value['nodes'][1])
        self.assertNotIn('size', value['nodes'][1])
        self.assertEqual(World.restore(value).snapshot(), value)
        self.assertEqual([row['id'] for row in world.context('room')['subtree']], ['room', 'unlisted_entity'])

    def test_source_cannot_serialize_a_callable_as_a_native_reader(self):
        world = World(native=True)
        world.io['streams']['false_fact'] = {'source': lambda: 1}
        with self.assertRaises((TypeError, ValueError)):
            world.snapshot()

    def test_native_binding_rejects_paths_escaping_the_world_and_duplicate_ids(self):
        for value in ('relative', '/World/../private', '/World/./thing', '/Other/thing'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                World(native=True).bind('thing', value)
        world = World(native=True)
        world.bind('thing', '/World/Thing')
        with self.assertRaises(ValueError):
            world.bind('thing', '/World/Other')

    def test_native_task_bindings_remain_semantic_not_a_facts_dictionary(self):
        world = World(native=True)
        world.bind('tool', '/World/Robot/Tool')
        task = Task('t', bindings={'tool': 'tool'}, rubric={'sourceRef': 'rubrics.py', 'helperRefs': []})
        self.assertEqual(task.bind(world)['bindings'], {'tool': 'tool'})
        with self.assertRaises(ValueError):
            Task('t', bindings={'tool': 'missing'}, rubric=task.rubric).bind(world)

    def test_native_binding_has_no_fabricated_world_position(self):
        world = World(native=True)
        world.bind('tool', '/World/Robot/Tool')
        with self.assertRaisesRegex(ValueError, 'native|SDK'):
            world.position('tool')
        world.space('relative', frame='tool')
        with self.assertRaisesRegex(ValueError, 'native|SDK'):
            world.transform('relative')

    def test_bind_rejects_property_variant_and_invalid_prim_paths(self):
        for path in ('/World/Body.mass', '/World/Body{v=a}', '/World/bad-name'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                World(native=True).bind('body', path)

    def test_native_tasks_reject_unwired_measurements_and_ambiguous_camera_permissions(self):
        world = World(native=True)
        for measurements in ({'objects': {'required':True}}, {'camera':{'visibleToPolicy':'yes','cameraIds':[]}},
                             {'camera':{'visibleToPolicy':True,'cameraIds':['missing']}}):
            with self.subTest(measurements=measurements), self.assertRaises(ValueError):
                Task('t', rubric={'sourceRef':'rubrics.py','helperRefs':[]}, measurements=measurements).bind(world)

    def test_native_build_cannot_silently_ignore_legacy_interaction_execution(self):
        world = World(native=True)
        world.box('switch',[.1]*3,joint={'type':'slide','range':[0,.01]})
        world.box('lamp',[.1]*3)
        world.when('pressed',target='switch',event='joint_at_least',threshold=.005,
                   effect={'target':'lamp','state':{'on':True}})
        with self.assertRaisesRegex(ValueError,'native|interaction'):
            world.validate()


class NativeStageAuditTests(unittest.TestCase):
    def directory(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def write(self, root, text, name='scene-source.usda'):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#usda 1.0\n'+text)
        return path

    def test_plain_native_data_is_audited_without_starting_kit(self):
        root = self.directory()
        self.write(root, 'def Xform "World" {\n def Cube "Body" {}\n}\n')
        report = worker.audit_native_stage(root)
        self.assertEqual(report['entry'], 'scene-source.usda')
        self.assertIn('scene-source.usda', report['files'])
        self.assertEqual(report['executableSources'], [])

    def test_script_and_graph_nodes_are_rejected_even_in_inactive_variants(self):
        for body in ('def OmniGraphNode "Run" {\n token node:type = "omni.graph.scriptnode.ScriptNode"\n }',
                     'def Xform "Hidden" (active = false) {\n def OmniGraph "Run" {}\n }',
                     'def Xform "Choice" (prepend variantSets = "choice") {\n variantSet "choice" = {\n "hidden" {\n def OmniGraphNode "Run" {}\n }\n }\n }'):
            with self.subTest(body=body):
                root = self.directory()
                self.write(root, 'def Xform "World" {\n'+body+'\n}\n')
                with self.assertRaisesRegex(ValueError, 'executable|graph|script'):
                    worker.audit_native_stage(root)

    def test_external_layers_and_symlinks_do_not_join_the_frozen_closure(self):
        root = self.directory()
        outside = root.parent/(root.name+'-outside.usda')
        outside.write_text('#usda 1.0\ndef Xform "External" {}\n')
        self.addCleanup(outside.unlink)
        self.write(root, '(subLayers = [@'+str(outside)+'@])\ndef Xform "World" {}\n')
        with self.assertRaisesRegex(ValueError, 'closure|outside'):
            worker.audit_native_stage(root)
        self.write(root, 'def Xform "World" {}\n')
        (root/'link').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            worker.audit_native_stage(root)

    def test_layer_metadata_cannot_enable_startup_extensions(self):
        root = self.directory()
        self.write(root, '(customLayerData = { string[] "omni:kit:extensions" = ["generated.module"] })\ndef Xform "World" {}\n')
        with self.assertRaisesRegex(ValueError, 'executable|extension'):
            worker.audit_native_stage(root)

    def test_asset_array_dependencies_cannot_escape_the_closure(self):
        root = self.directory()
        outside = root.parent/(root.name+'-outside.usda')
        outside.write_text('#usda 1.0\ndef Xform "External" {}\n')
        self.addCleanup(outside.unlink)
        self.write(root, 'def Xform "World" {\n custom asset[] textures = [@'+str(outside)+'@]\n}\n')
        with self.assertRaisesRegex(ValueError, 'closure|outside'):
            worker.audit_native_stage(root)

    def test_payload_delete_and_order_markers_do_not_create_dependencies(self):
        root = self.directory()
        self.write(root, 'def Xform "World" (\n delete payload = @missing.usda@\n reorder payload = [@also-missing.usda@]\n) {}\n')
        report = worker.audit_native_stage(root)
        self.assertEqual(list(report['files']), ['scene-source.usda'])

    def test_positive_missing_payload_is_rejected_including_weaker_and_hidden_specs(self):
        bodies = (
            'def Xform "World" (prepend payload = @missing.usda@) {}\n',
            'def Xform "World" (prepend variantSets = "choice") {\n variantSet "choice" = {\n "hidden" (prepend payload = @missing.usda@) {}\n }\n}\n',
        )
        for body in bodies:
            root = self.directory()
            self.write(root, body)
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, 'closure|missing'):
                worker.audit_native_stage(root)
        root = self.directory()
        self.write(root, 'def Xform "World" (prepend payload = @missing.usda@) {}\n', 'weaker.usda')
        self.write(root, '(subLayers = [@weaker.usda@])\ndef Xform "World" (delete payload = @missing.usda@) {}\n')
        with self.assertRaisesRegex(ValueError, 'closure|missing'):
            worker.audit_native_stage(root)

    def test_script_property_names_are_inspected(self):
        root = self.directory()
        self.write(root, 'def Xform "World" {\n custom string inputs:script = "run()"\n}\n')
        with self.assertRaisesRegex(ValueError, 'executable|script'):
            worker.audit_native_stage(root)

    def test_inline_shader_source_cannot_bypass_external_shader_rejection(self):
        root = self.directory()
        self.write(root, 'def Xform "World" {\n def Shader "Material" {\n token info:implementationSource = "sourceCode"\n string info:osl:sourceCode = "unqualified source"\n }\n}\n')
        with self.assertRaisesRegex(ValueError, 'executable|source'):
            worker.audit_native_stage(root)

    def test_keyframed_geometry_cannot_bypass_controlled_motion_with_static_root_pose(self):
        root = self.directory()
        self.write(root, 'def Xform "World" {\n def Mesh "Object" {\n point3f[] points.timeSamples = {\n 0: [(0,0,0),(1,0,0),(0,1,0)],\n 1: [(1,0,0),(2,0,0),(1,1,0)]\n }\n }\n}\n')
        with self.assertRaisesRegex(ValueError, 'animated|motion'):
            worker.audit_native_stage(root)

    def test_usdz_is_rejected_until_package_contents_can_be_audited(self):
        from pxr import Sdf, UsdUtils
        root = self.directory()
        source = self.write(root, 'def OmniGraph "Hidden" {}\n', 'unsafe.usda')
        self.assertTrue(UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(source)), str(root/'unsafe.usdz')))
        self.write(root, 'def Xform "World" (references = @unsafe.usdz@) {}\n')
        with self.assertRaisesRegex(ValueError, 'package|USDZ'):
            worker.audit_native_stage(root)

    def test_unqualified_shader_sources_do_not_claim_a_transitive_closure(self):
        for extension in ('mdl', 'mtlx', 'osl'):
            root = self.directory()
            (root/('material.'+extension)).write_text('external dependency not represented in USD')
            self.write(root, 'def Xform "World" {\n custom asset shaderAsset = @material.'+extension+'@\n}\n')
            with self.subTest(extension=extension), self.assertRaisesRegex(ValueError, 'shader|closure'):
                worker.audit_native_stage(root)


class ControlledMdlAuditTests(unittest.TestCase):
    directory = NativeStageAuditTests.directory
    write = NativeStageAuditTests.write
    def stage(self, root, asset='OmniPBR.mdl', extra=''):
        return self.write(root, 'def Xform "World" {\n def Shader "Surface" {\n'
            ' asset info:mdl:sourceAsset = @'+asset+'@\n'
            ' token info:mdl:sourceAsset:subIdentifier = "OmniPBR"\n'+extra+'\n }\n}\n')

    def test_only_authored_bare_mdl_source_asset_can_use_sdk_resolution(self):
        from runtime import mdl
        root = self.directory()
        self.stage(root)
        def inspect(directory, entries, command, timeout):
            self.assertEqual(entries[0]['asset'], 'OmniPBR.mdl')
            self.assertEqual(entries[0]['subIdentifier'], 'OmniPBR')
            return {'schema':'sceneagent-mdl-audit-v1','sdk':{},'roots':[], 'entries':entries,
                    'files':{},'modules':[],'resources':{}}
        with patch.object(mdl, 'audit', side_effect=inspect), patch.object(mdl, 'sdk_identity', return_value={}), \
             patch.object(worker, 'controller_command', return_value=['fixture']):
            report = worker.audit_native_stage(root)
        self.assertEqual(report['schema'], 'sceneagent-native-stage-audit-v2')
        self.assertIn('mdl', report)

    def test_explicit_missing_relative_mdl_cannot_fall_back_to_sdk(self):
        from runtime import mdl
        root = self.directory()
        self.stage(root, './OmniPBR.mdl')
        with patch.object(mdl, 'audit') as inspector, self.assertRaisesRegex(ValueError,'missing|closure'):
            worker.audit_native_stage(root)
        inspector.assert_not_called()

    def test_mdl_parameter_udim_is_delegated_to_official_resource_enumeration(self):
        from runtime import mdl
        root = self.directory()
        self.stage(root, extra=' asset inputs:diffuse_texture = @tile.<UDIM>.png@')
        (root/'tile.1001.png').write_bytes(b'qualified resource fixture')
        checksum = worker.digest(root/'tile.1001.png')
        def inspect(directory, entries, command, timeout):
            authored = next(row for row in entries if not row.get('effectivePrimPath'))
            self.assertEqual(authored['parameters'], [{'name':'diffuse_texture','asset':'tile.<UDIM>.png'}])
            return {'schema':'sceneagent-mdl-audit-v1','sdk':{},'roots':[], 'entries':entries,
                    'files':{'package/tile.1001.png':checksum}, 'modules':[],
                    'resources':{'package/tile.1001.png':checksum}}
        with patch.object(mdl, 'audit', side_effect=inspect), patch.object(mdl, 'sdk_identity', return_value={}), \
             patch.object(worker, 'controller_command', return_value=['fixture']):
            report = worker.audit_native_stage(root)
        self.assertEqual(report['files']['tile.1001.png'], checksum)

    def test_connected_mdl_graph_cannot_claim_literal_resource_qualification(self):
        root = self.directory()
        self.stage(root, extra=' color3f inputs:diffuse_color_constant.connect = </World/Other.outputs:result>')
        with self.assertRaisesRegex(ValueError, 'connected MDL'):
            worker.audit_native_stage(root)

    def layered(self, root, override):
        self.write(root, '(subLayers = [@weak/model.usda@])\nover "World" {\n over "Surface" {\n'+override+'\n }\n}\n')
        self.write(root, 'def Xform "World" {\n def Shader "Surface" {\n'
            ' asset info:mdl:sourceAsset = @./material.mdl@\n'
            ' token info:mdl:sourceAsset:subIdentifier = "OmniPBR"\n }\n}\n', 'weak/model.usda')
        (root/'weak/material.mdl').write_text('SYSTEM_VERIFICATION_ONLY official audit double')

    def test_stronger_over_connection_cannot_bypass_weaker_mdl_source(self):
        root = self.directory()
        self.layered(root, ' color3f inputs:diffuse_color_constant.connect = </World/Texture.outputs:rgb>')
        with self.assertRaisesRegex(ValueError, 'connected MDL'):
            worker.audit_native_stage(root)

    def test_stronger_over_udim_uses_the_default_owning_layer_not_mdl_source_layer(self):
        from runtime import mdl
        root = self.directory()
        self.layered(root, ' asset inputs:diffuse_texture = @strong/tile.<UDIM>.png@')
        (root/'strong').mkdir()
        (root/'strong/tile.1001.png').write_bytes(b'official texture double')
        checksums = {name:worker.digest(root/name) for name in ('weak/material.mdl','strong/tile.1001.png')}
        def inspect(directory, entries, command, timeout):
            effective = next(row for row in entries if row.get('effectivePrimPath') == '/World/Surface')
            self.assertEqual(effective['layer'], 'weak/model.usda')
            self.assertEqual(effective['parameters'], [{'name':'diffuse_texture',
                'asset':'strong/tile.<UDIM>.png','layer':'scene-source.usda'}])
            return {'schema':'sceneagent-mdl-audit-v1','sdk':{},'roots':[], 'entries':entries,
                    'files':{'package/'+name:checksum for name,checksum in checksums.items()},
                    'modules':[],'resources':{}}
        with patch.object(mdl, 'audit', side_effect=inspect), patch.object(mdl, 'sdk_identity', return_value={}), \
             patch.object(worker, 'controller_command', return_value=['fixture']):
            report = worker.audit_native_stage(root)
        self.assertEqual(report['files']['strong/tile.1001.png'], checksums['strong/tile.1001.png'])

    def test_stronger_source_asset_inherits_weaker_sub_identifier(self):
        from runtime import mdl
        root = self.directory()
        self.layered(root, ' asset info:mdl:sourceAsset = @./new.mdl@')
        (root/'new.mdl').write_text('SYSTEM_VERIFICATION_ONLY alternative module')
        def inspect(directory, entries, command, timeout):
            partial = next(row for row in entries if row['layer']=='scene-source.usda' and not row.get('effectivePrimPath'))
            self.assertEqual(partial['subIdentifier'], '')
            effective = next(row for row in entries if row.get('effectivePrimPath'))
            self.assertEqual(effective['subIdentifier'], 'OmniPBR')
            self.assertEqual(effective['asset'], './new.mdl')
            return {'schema':'sceneagent-mdl-audit-v1','sdk':{},'roots':[], 'entries':entries,
                    'files':{'package/'+name:worker.digest(root/name) for name in ('new.mdl','weak/material.mdl')},
                    'modules':[],'resources':{}}
        with patch.object(mdl,'audit',side_effect=inspect), patch.object(mdl,'sdk_identity',return_value={}), \
             patch.object(worker,'controller_command',return_value=['fixture']):
            worker.audit_native_stage(root)


class OptionalWebPreviewTests(unittest.TestCase):
    """SYSTEM_VERIFICATION_ONLY: native construction doubles, no simulation."""
    def build(self, root, *, preview_error=None, change=None, compile_error=None, script=False, log_text='', audit_log_text=None,
              source_overrides=None, environment_refs=None, on_inputs=None):
        from dataclasses import asdict
        from types import SimpleNamespace
        from runtime import simulation
        from contextlib import ExitStack
        world = World(native=True)
        world.space('fixture')
        io = {'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}
        world.io = {key:value for key, value in io.items() if key != 'schema'}
        task = Task('fixture', rubric={'sourceRef':'rubrics.py','helperRefs':[]})
        files = {'scene.py':'def build(world): pass\n', 'tasks.py':'def build(world): return []\n',
                 'expert.py':'def run(env, task): pass\n', 'rubrics.py':'def evaluate(task, facts, state): pass\n'}
        files.update(source_overrides or {})
        payload = {'operation':'build','root':str(root),'projectId':'sys_preview_fixture','sourceFiles':files}
        if environment_refs is not None: payload['environmentSourceRefs'] = environment_refs
        output = None
        def command(inputs, directory, *args):
            nonlocal output
            output = directory
            if on_inputs is not None: on_inputs(inputs)
            return ['system_verification_only']
        def construct(*args, **kwargs):
            kwargs['stdout'].write(log_text.encode())
            (output/'scene-source.usda').write_text('#usda 1.0\n(subLayers = [@dependency.usda@])\ndef Xform "World" {}\n')
            (output/'dependency.usda').write_text('#usda 1.0\ndef '+('OmniGraph' if script else 'Xform')+' "Dependency" {}\n')
            (output/'scene.json').write_text(json.dumps(world.snapshot()))
            (output/'tasks.json').write_text(json.dumps([asdict(task)]))
            (output/'native-build.json').write_text(json.dumps({'camera':{},'recordingCameras':{}}))
            return SimpleNamespace(returncode=0)
        def preview(usd, path, bindings):
            path.write_bytes(b'SYSTEM_VERIFICATION_ONLY preview')
            path.with_name('preview-raw.glb').write_bytes(b'partial')
            path.with_name('preview-evidence.json').write_text('{}')
            if change:
                (path.parent/change).write_text('# changed during conversion\n')
            if preview_error:
                raise preview_error
        def audit(directory, **options):
            if audit_log_text is not None:
                (directory/'native-audit.log').write_text(audit_log_text)
                (directory/'mdl-audit.log').write_text(audit_log_text)
            return worker.audit_native_stage(directory, **options)
        compiled = {'usd':'scene-source.usda','io':io,'bindings':{},'camera':{},'initialState':{}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, 'native_build_command', side_effect=command))
            stack.enter_context(patch.object(worker.subprocess, 'run', side_effect=construct))
            stack.enter_context(patch.object(worker, 'isolated_native_audit', side_effect=audit))
            stack.enter_context(patch.object(worker, 'runtime_key', return_value='fixture-runtime'))
            stack.enter_context(patch.object(worker, 'runtime_versions', return_value={'fixture':True}))
            stack.enter_context(patch.object(worker, '__file__', str(root/'runtime/fixture/runtime/worker.py')))
            stack.enter_context(patch.object(simulation, 'compile_native_world', side_effect=compile_error,
                                            return_value=compiled))
            stack.enter_context(patch.object(simulation, 'export_preview', side_effect=preview))
            result = worker.build(payload, root)
            required = worker.required_files(payload, result)
        return result, root/'projects'/payload['projectId']/'revisions'/result['revision'], required

    def test_behavior_only_changes_do_not_enter_builder_or_environment_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); refs=['scene.py','tasks.py','rubrics.py']
            def inputs(directory):
                self.assertEqual({str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file()},set(refs))
            first,directory,_=self.build(root,environment_refs=refs,on_inputs=inputs)
            first['status']='completed';worker.save(directory/'manifest.json',first)
            second,_,_=self.build(root,environment_refs=refs,on_inputs=inputs,
                source_overrides={'expert.py':'def run(env,task): raise RuntimeError("different behavior")\n'})
            self.assertEqual(first['revision'],second['revision'])
            self.assertNotIn('expert.py',second['sourceDigests'])
            self.assertFalse((directory/'expert.py').exists())

    def test_diagnostic_log_does_not_change_revision_and_repeated_attempt_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            first,directory,required=self.build(root,log_text='2026-10-23T10:00:00Z first attempt\n')
            self.assertNotIn('native-build.log',first['assets'])
            self.assertIn(directory/'native-build.log',required)
            first['status']='completed';(directory/'manifest.json').write_text(json.dumps(first))
            second,second_directory,_=self.build(root,log_text='2026-10-23T10:00:01Z repeated attempt\n')
            self.assertEqual(second['revision'],first['revision'])
            self.assertEqual(second_directory,directory)
            self.assertEqual((directory/'native-build.log').read_text(),'2026-10-23T10:00:00Z first attempt\n')
            retained=list((root/'projects/sys_preview_fixture/build-diagnostics').glob('*.log'))
            self.assertEqual(len(retained),1)
            self.assertEqual(retained[0].read_text(),'2026-10-23T10:00:01Z repeated attempt\n')

    def test_new_diagnostic_log_still_has_exit_time_integrity_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();result,directory,_=self.build(root,log_text='original log\n')
            (directory/'native-build.log').write_text('changed diagnostic\n')
            payload={'operation':'build','root':str(root),'projectId':'sys_preview_fixture',
                'sourceFiles':{name:(directory/name).read_text() for name in result['sourceDigests']}}
            with patch.object(worker,'runtime_key',return_value='fixture-runtime'),patch.object(worker,'runtime_versions',return_value={'fixture':True}):
                with self.assertRaisesRegex(ValueError,'diagnostic'):
                    worker.required_files(payload,result)
                shortened=dict(result);shortened.pop('diagnostics')
                with self.assertRaisesRegex(ValueError,'diagnostic'):
                    worker.required_files(payload,shortened)

    def test_mdl_and_passive_audit_logs_do_not_change_scene_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            first, directory, required = self.build(root, audit_log_text='timestamp A and pid 1')
            first['status'] = 'completed'; worker.save(directory/'manifest.json', first)
            second, _, _ = self.build(root, audit_log_text='timestamp B and pid 2')
            self.assertEqual(first['revision'], second['revision'])
            self.assertNotIn('mdl-audit.log', first['assets'])
            self.assertNotIn('native-audit.log', first['assets'])
            self.assertIn(directory/'mdl-audit.log', required)
            self.assertIn(directory/'native-audit.log', required)
            self.assertEqual((directory/'mdl-audit.log').read_text(), 'timestamp A and pid 1')

    def test_preview_compatibility_failure_does_not_discard_native_build_or_publish_partial_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, directory, required = self.build(Path(temporary), preview_error=ValueError('unsupported preview material'))
            self.assertNotIn('preview', result['artifacts'])
            self.assertTrue(any('unsupported preview material' in text for text in result['limitations']))
            for name in ('preview.glb', 'preview-raw.glb', 'preview-evidence.json'):
                self.assertFalse((directory/name).exists())
                self.assertNotIn(name, result['assets'])
                self.assertNotIn(directory/name, required)

    def test_successful_preview_remains_a_required_locked_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, directory, required = self.build(Path(temporary))
            self.assertIn('preview', result['artifacts'])
            self.assertIn(directory/'preview.glb', required)
            (directory/'preview.glb').write_bytes(b'changed preview')
            with patch.object(worker, 'runtime_versions', return_value={'fixture':True}):
                with self.assertRaisesRegex(ValueError, 'digest'):
                    worker.verify_inputs(result, 'fixture-runtime', directory)

    def test_preview_failure_cannot_hide_changed_native_source_or_dependency(self):
        for name in ('scene-source.usda', 'dependency.usda'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(ValueError, 'closure|changed|digest'):
                    self.build(Path(temporary), preview_error=ValueError('preview unsupported'), change=name)

    def test_script_or_native_compile_failure_still_blocks_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'executable'):
                self.build(Path(temporary), script=True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'native interface failed'):
                self.build(Path(temporary), compile_error=RuntimeError('native interface failed'))


class NativeFrameCompositionTests(unittest.TestCase):
    def test_native_recording_pose_qualification_is_checked_before_delivery(self):
        from runtime.simulation import validate_recording_pose_streams
        from copy import deepcopy
        world=World(native=True)
        world.bind('hand','/World/Robot/Hand')
        world.space('mount',frame='hand')
        cameras={'t':{'wrist':{'frame':'mount','rateHz':25}}}
        pose={'source':{'reader':'rigid_pose','path':'/World/Robot/Hand'},'frame':'world_wxyz',
              'shape':[7],'origin':'native_output','rateHz':100}
        io={'streams':{'pose':pose}}
        validate_recording_pose_streams(world,io,cameras)
        for streams in ({},{'pose':{**pose,'rateHz':10}}, {'pose':pose,'duplicate':deepcopy(pose)},
                        {'pose':{**pose,'source':{**pose['source'],'reader':'root_pose'}}}):
            with self.subTest(streams=streams),self.assertRaisesRegex(ValueError,'rigid_pose'):
                validate_recording_pose_streams(world,{'streams':streams},cameras)
        validate_recording_pose_streams(world,{'streams':{}},{'t':{'external':{'frame':'world','rateHz':25}}})

    def test_semantic_offsets_compose_with_actual_native_parent_for_build_and_media(self):
        import math
        import numpy as np
        from pxr import Usd, UsdGeom, Gf
        from runtime.simulation import recording_camera_pose, camera_for
        stage = Usd.Stage.CreateInMemory()
        prim = UsdGeom.Xform.Define(stage, '/World/Hand')
        prim.AddTranslateOp().Set(Gf.Vec3d(3,0,1))
        prim.AddOrientOp().Set(Gf.Quatf(math.sqrt(.5),0,0,math.sqrt(.5)))
        world = World(native=True)
        world.bind('hand','/World/Hand')
        world.space('mount',frame='hand',at=(.1,.2,.3))
        world.space('framing',frame='mount',bounds={'min':[-.1,-.1,-.1],'max':[.1,.1,.1]})
        camera = {'frame':'mount','at':[.4,0,0],'orientation':[1,0,0,0]}
        expected = recording_camera_pose(world,camera,stage=stage)
        np.testing.assert_allclose(expected[:3,3],[2.8,.5,1.3],atol=1e-7)
        np.testing.assert_allclose(camera_for(world,scope='framing',stage=stage)['lookat'],[2.8,.1,1.3],atol=1e-7)
        io = {'streams':{'hand':{'source':{'reader':'rigid_pose','path':'/World/Hand'},
             'frame':'world_wxyz','origin':'native_output','shape':[7]}}}
        state = {'time':.04,'streams':{'hand':[3,0,1,math.sqrt(.5),0,0,math.sqrt(.5)]},
                 'streamStatus':{'hand':{'status':'available','sampleTime':.04}}}
        actual = recording_camera_pose(world,camera,state=state,io=io)
        np.testing.assert_allclose(actual,expected,atol=1e-7)
        state['streamStatus']['hand']['sampleTime']=0
        with self.assertRaisesRegex(ValueError,'sampled'):
            recording_camera_pose(world,camera,state=state,io=io)


if __name__ == '__main__':
    unittest.main()
