import unittest
import json
from pathlib import Path
import tempfile

from runtime.scene import World, Task, revision_key


class WorldTests(unittest.TestCase):
    def test_stable_ids_and_nested_frames(self):
        world = World()
        world.space('kitchen', at=(1, 2, 0), yaw=1.5707963267948966)
        world.box('cup', size=(.1, .1, .1), at=(1, 0, .8), parent='kitchen')
        self.assertAlmostEqual(world.position('cup')[0], 1)
        self.assertAlmostEqual(world.position('cup')[1], 3)
        self.assertEqual(world.snapshot()['nodes'][1]['id'], 'cup')
        with self.assertRaises(ValueError):
            world.box('cup', size=(.1, .1, .1))

    def test_binding_rejects_missing_object_and_joint(self):
        world = World()
        world.box('cube', size=(.04, .04, .04))
        rubric = {'sourceRef': 'rubrics.py', 'helperRefs': []}
        with self.assertRaises(ValueError):
            Task('missing', bindings={'object': 'missing'}, rubric=rubric).bind(world)
        with self.assertRaises(ValueError):
            Task('missing_joint', bindings={'object': {'objectId': 'cube', 'joint': True}}, rubric=rubric).bind(world)

    def test_task_has_no_host_scoring_or_kind_factory(self):
        for name in ('region', 'joint', 'pick_place', 'score'):
            self.assertFalse(hasattr(Task, name))
        world = World(); world.box('cube', (.1, .1, .1))
        definition = Task('custom', instruction='Generated semantics', bindings={'object': 'cube'},
                          parameters={'anything': ['a', 3]}, rubric={'sourceRef': 'rubrics.py', 'helperRefs': []})
        self.assertEqual(definition.bind(world)['parameters'], {'anything': ['a', 3]})

    def test_joint_binding_is_real_but_legacy_interactions_cannot_execute(self):
        world = World()
        world.box('button', size=(.03, .03, .01), joint={'type': 'slide', 'range': [0, .01]})
        world.box('lamp', size=(.03, .03, .03))
        bound = Task('press', bindings={'button': {'objectId': 'button', 'joint': True}},
                     rubric={'sourceRef': 'rubrics.py', 'helperRefs': []}).bind(world)
        self.assertEqual(bound['bindings']['button']['objectId'], 'button')
        world.when('pressed', target='button', event='joint_at_least', threshold=.005,
                   effect={'target': 'lamp', 'state': {'on': True}, 'color': [0, 1, 0, 1]})
        with self.assertRaises(ValueError):
            world.when('bad', target='lamp', event='joint_at_least', threshold=.01, effect={'target': 'lamp'})
        with self.assertRaisesRegex(ValueError, 'native|interaction'):
            world.snapshot()

    def test_asset_catalog_deduplicates_ids_and_keeps_missing_files_honest(self):
        from runtime.worker import assets
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root/'legacy/data/resource_closure/asset_catalog.json'
            catalog.parent.mkdir(parents=True)
            rows = [{'asset_id': 'cup-1', 'name': 'blue cup', 'visual_path': '/absent/cup.glb'},
                    {'asset_id': 'cup-1', 'name': 'blue cup', 'visual_path': '/absent/cup.glb'}]
            catalog.write_text(json.dumps({'entries': rows}))
            result = assets(root/'legacy', 'cup', root=root/'data', asset_id='cup-1')
            self.assertEqual(result['catalogAssets'], 1)
            self.assertEqual(len(result['items']), 1)
            self.assertFalse(result['items'][0]['exists'])
            self.assertFalse(result['items'][0]['simulationReady'])
            repeated = assets(root/'legacy', 'cup', root=root/'data', asset_id='cup-1')
            self.assertEqual({key: value for key, value in repeated.items() if key != 'elapsedMs'},
                             {key: value for key, value in result.items() if key != 'elapsedMs'})

    def test_rollout_rejects_changed_locked_asset_before_execution(self):
        import sys
        from unittest.mock import patch
        from runtime import worker
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);asset=directory/'model.bin';asset.write_bytes(b'original resource')
            stage=directory/'scene-source.usda';stage.write_text('#usda 1.0\ndef Xform "World" {}\n')
            audit=worker.audit_native_stage(directory)
            (directory/'native-audit.json').write_text(json.dumps(audit))
            (directory/'scene.py').write_text('# frozen generated source')
            io={'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}
            manifest={'runtime':'same-runtime','assets':{'model.bin':worker.digest(asset)},
                'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},'runtimeVersions':{'fixture':True},
                'sourceDigests':{'scene.py':worker.digest(directory/'scene.py')},'io':io,
                'frozenArtifactDigests':{name:worker.digest(directory/name) for name in ('scene-source.usda','native-audit.json')},
                'compiled':{'io':io,'usd':'scene-source.usda','nativeStageAudit':'native-audit.json'}}
            with patch.object(worker,'runtime_versions',return_value={'fixture':True}):
                worker.verify_inputs(manifest,'same-runtime',directory)
                asset.write_bytes(b'changed resource')
                with self.assertRaisesRegex(ValueError,'重新构建'):worker.verify_inputs(manifest,'same-runtime',directory)
                asset.write_bytes(b'original resource')
                (directory/'scene.py').write_text('# changed source')
                with self.assertRaisesRegex(ValueError,'source digest'):worker.verify_inputs(manifest,'same-runtime',directory)
                (directory/'scene.py').write_text('# frozen generated source')
                stage.write_text('#usda 1.0\ndef Xform "World" {\n def Cube "Changed" {}\n}\n')
                with self.assertRaisesRegex(ValueError,'artifact digest'):worker.verify_inputs(manifest,'same-runtime',directory)

    def frozen_native(self):
        import sys
        from runtime import worker
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory/'scene-source.usda').write_text('#usda 1.0\n(subLayers = [@layer.usda@])\ndef Xform "World" {}\n')
        (directory/'layer.usda').write_text('#usda 1.0\ndef Xform "World" {\n custom asset data = @model.bin@\n bool physics:kinematicEnabled = false\n vector3f physics:velocity = (0, 0, 0)\n}\n')
        (directory/'model.bin').write_bytes(b'original resource')
        (directory/'scene.py').write_text('# frozen generated source')
        audit = worker.audit_native_stage(directory)
        (directory/'native-audit.json').write_text(json.dumps(audit))
        io = {'schema':'sceneagent-native-io-v1','clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}}
        manifest = {'runtime':'same-runtime','runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
            'runtimeVersions':{'fixture':True},'io':io,
            'assets':{name:worker.digest(directory/name) for name in ('layer.usda','model.bin')},
            'sourceDigests':{'scene.py':worker.digest(directory/'scene.py')},
            'frozenArtifactDigests':{name:worker.digest(directory/name) for name in ('scene-source.usda','native-audit.json')},
            'compiled':{'io':io,'usd':'scene-source.usda','nativeStageAudit':'native-audit.json'}}
        return directory, manifest, audit

    def test_frozen_native_consumption_does_not_reparse_unchanged_layers(self):
        from unittest.mock import patch
        from runtime import worker
        directory, manifest, audit = self.frozen_native()
        with patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                patch.object(worker,'audit_native_stage',side_effect=AssertionError('consumer must not reparse')) as parse:
            paths = worker.verify_inputs(manifest,'same-runtime',directory)
        parse.assert_not_called()
        self.assertEqual(paths, {directory/'native-audit.json', *(directory/name for name in audit['files'])})

    def test_legacy_motion_diagnostics_order_does_not_change_frozen_consumption(self):
        from copy import deepcopy
        from unittest.mock import patch
        from runtime import worker
        directory, manifest, audit = self.frozen_native()
        # Legacy packages may retain these diagnostics; they are not a second file authority.
        audit['motionSources'] = [{'layer':'layer.usda','path':'/World.physics:velocity'},
                                  {'layer':'layer.usda','path':'/World.physics:kinematicEnabled'}]
        (directory/'native-audit.json').write_text(json.dumps(audit))
        manifest['frozenArtifactDigests']['native-audit.json'] = worker.digest(directory/'native-audit.json')
        reordered = deepcopy(audit)
        reordered['motionSources'].reverse()
        with patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                patch.object(worker,'audit_native_stage',return_value=reordered):
            worker.verify_inputs(manifest,'same-runtime',directory)

    def test_frozen_native_audit_and_each_dependency_require_manifest_locks(self):
        from unittest.mock import patch
        from runtime import worker
        for field, name in (('frozenArtifactDigests','native-audit.json'),
                            ('frozenArtifactDigests','scene-source.usda'),
                            ('assets','layer.usda'),('assets','model.bin')):
            directory, manifest, _ = self.frozen_native()
            del manifest[field][name]
            with self.subTest(name=name), patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                    self.assertRaisesRegex(ValueError,'audit|closure'):
                worker.verify_inputs(manifest,'same-runtime',directory)

    def test_frozen_native_rejects_changed_or_missing_audit_entry_and_dependencies(self):
        from unittest.mock import patch
        from runtime import worker
        for name in ('native-audit.json','scene-source.usda','layer.usda','model.bin'):
            for change in ('modify','delete'):
                directory, manifest, _ = self.frozen_native()
                target = directory/name
                if change == 'modify':
                    target.write_bytes(target.read_bytes()+b'changed')
                else:
                    target.unlink()
                with self.subTest(name=name,change=change), patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                        self.assertRaises((ValueError,FileNotFoundError)):
                    worker.verify_inputs(manifest,'same-runtime',directory)

    def test_frozen_native_audit_entry_schema_and_manifest_hashes_must_agree(self):
        from unittest.mock import patch
        from runtime import worker
        for change in ('schema','entry','missing_entry','rehashed_dependency'):
            directory, manifest, audit = self.frozen_native()
            if change == 'schema': audit['schema'] = 'unknown-audit'
            elif change == 'entry': audit['entry'] = 'layer.usda'
            elif change == 'missing_entry': del audit['files']['scene-source.usda']
            else:
                (directory/'model.bin').write_bytes(b'changed resource')
                manifest['assets']['model.bin'] = worker.digest(directory/'model.bin')
            (directory/'native-audit.json').write_text(json.dumps(audit))
            manifest['frozenArtifactDigests']['native-audit.json'] = worker.digest(directory/'native-audit.json')
            with self.subTest(change=change), patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                    self.assertRaises(ValueError):
                worker.verify_inputs(manifest,'same-runtime',directory)

    def test_frozen_native_audit_cannot_escape_through_path_or_symlink(self):
        from unittest.mock import patch
        from runtime import worker
        for kind in ('path','symlink'):
            directory, manifest, audit = self.frozen_native()
            outside = directory.parent/(directory.name+'-outside.bin')
            outside.write_bytes((directory/'model.bin').read_bytes())
            self.addCleanup(outside.unlink)
            if kind == 'path':
                name = '../'+outside.name
                audit['files'][name] = worker.digest(outside)
                manifest['assets'][name] = worker.digest(outside)
                (directory/'native-audit.json').write_text(json.dumps(audit))
                manifest['frozenArtifactDigests']['native-audit.json'] = worker.digest(directory/'native-audit.json')
            else:
                (directory/'model.bin').unlink()
                (directory/'model.bin').symlink_to(outside)
            with self.subTest(kind=kind), patch.object(worker,'runtime_versions',return_value={'fixture':True}), \
                    self.assertRaisesRegex(ValueError,'data root|closure|symlink'):
                worker.verify_inputs(manifest,'same-runtime',directory)


    def test_rollout_rejects_changed_runtime_before_execution(self):
        import sys
        from runtime.worker import verify_inputs, runtime_versions
        with self.assertRaisesRegex(ValueError, '重新构建'):
            verify_inputs({'runtime': 'built-runtime', 'assets': {},
                'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},'runtimeVersions':runtime_versions()}, 'changed-runtime')

    def test_revision_covers_sources_assets_runtime_seed_and_robot(self):
        args = {'sources': {'scene': 'x', 'tasks': 'y', 'expert': 'z'}, 'assets': {'a': '123'},
                'runtime': 'version', 'seed': 0, 'robot': 'panda'}
        original = revision_key(**args)
        for name, value in [('seed', 1), ('robot', 'other'), ('runtime', 'v2'), ('assets', {'a': '124'})]:
            changed = dict(args, **{name: value})
            self.assertNotEqual(original, revision_key(**changed))
        self.assertEqual(original, revision_key(**args))


if __name__ == '__main__':
    unittest.main()
