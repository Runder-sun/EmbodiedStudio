import json
from pathlib import Path
import tempfile
import unittest

from runtime.worker import import_locks, source_files, verify_inputs


class EvaluationContractsTests(unittest.TestCase):
    def test_scene_only_consumer_accepts_empty_control_without_expert(self):
        """CPU records/scoring only; reused physics and isolation remain synthetic."""
        from copy import deepcopy
        import hashlib
        from unittest.mock import patch
        from runtime import worker
        from runtime.records import read_transitions
        from tests.test_frozen_evaluation import FrozenEvaluationTests

        fixture = FrozenEvaluationTests()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            _, directory, manifest, _ = fixture.context(root)
            manifest['io']['ports'] = {}
            manifest['compiled']['io'] = deepcopy(manifest['io'])
            manifest['sourceDigests'].pop('expert.py')
            (directory/'expert.py').unlink()
            source = 'def run(env, task):\n    env.step({})\n    env.step({})\n'
            (directory/'case_two.py').write_text(source)
            manifest['sourceDigests']['case_two.py'] = hashlib.sha256(source.encode()).hexdigest()
            payload = fixture.payload(operation='consume', root=str(root), requestId='empty_control_environment',
                requiredOutputs=['generation', 'evaluation'], environmentRefs=[{'revision': 'rev1'}])
            with fixture.isolated_consumer(), \
                 patch.object(worker, 'expert_sources', side_effect=AssertionError('scene scope read expert sources')), \
                 patch.object(worker, '_behavior_data', side_effect=AssertionError('scene scope read behavior data')), \
                 patch.object(worker, '_interface_source', wraps=worker._interface_source) as interface_source:
                payload['validationEpisodes'] = fixture.case_episodes(root)
                result = worker.consume_internal(payload, root)
                self.assertTrue(result['passed'])
                self.assertNotIn('demonstrations', result)
                self.assertTrue(worker.required_files(payload, result))
                interface_source.assert_any_call({}, manifest['io'])
            probe = root/result['evaluation']['interfaces'][0]['episodeRef']
            episode = json.loads(probe.read_text())
            self.assertEqual(episode['executionKind'], 'native_physics')
            self.assertEqual(episode['nativeInitialTargets'], {})
            self.assertFalse(episode['demonstrationEligible'])
            transitions, _, tail = read_transitions(probe.parent)
            self.assertFalse(tail)
            self.assertEqual([row['executedAction'] for row in transitions], [{}])

    def test_frozen_controller_has_only_public_task_and_motion_or_step(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from runtime import worker
        from runtime.scene import Task, World
        world = World(); world.box('body', (.1, .1, .1))
        calls = []
        env = SimpleNamespace(world=world, time=0, io={'clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}},
            observe=lambda *args, **kwargs: {'time':0}, reset=lambda seed:calls.append(('reset',seed)),
            step=lambda values,seconds:calls.append(('step',values,seconds)) or {},
            last_result={'success':False,'terminated':False,'reward':None,'details':{'privateObjectTruth':[9,8,7]}}, truncated=False)
        definition = Task('fixture', bindings={'object': 'body'}, parameters={'position': [.2, 0, .1]},
                          rubric={'sourceRef': 'rubrics.py', 'helperRefs': []})
        definition.bind(world)
        command = [sys.executable, '-c', 'from runtime.worker import controller_entry; controller_entry()']
        source = ("def run(env, task):\n    assert not hasattr(task, 'rubric')\n"
                  "    assert not hasattr(env, 'push')\n    assert not hasattr(env, 'move_ee')\n"
                  "    assert not hasattr(env, 'motion')\n    observation = env.step({'arm': task.parameters['position']}, .02)\n"
                  "    assert 'details' not in observation['result']\n")
        with patch.object(worker, 'controller_command', return_value=command):
            result = worker.run_controller(env, definition, source, 'case', source_files={'case.py': source}, source_ref='case.py')
        self.assertEqual(calls, [('reset',0), ('step',{'arm':[.2,0,.1]},.02)])
        self.assertEqual(result['actionCalls'], 1)

    def test_controller_exception_is_an_execution_error_not_expected_failure(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from runtime import worker
        from runtime.scene import Task, World
        world = World(); world.box('body', (.1, .1, .1))
        definition = Task('fixture', rubric={'sourceRef': 'rubrics.py', 'helperRefs': []})
        env = SimpleNamespace(world=world,time=0,io={'clock':{'physicsRateHz':200,'controlRateHz':50},'ports':{},'streams':{}},
            observe=lambda *args:{}, reset=lambda seed:None)
        command = [sys.executable, '-c', 'from runtime.worker import controller_entry; controller_entry()']
        source = "def run(env, task):\n    raise RuntimeError('interrupted controller')\n"
        with patch.object(worker, 'controller_command', return_value=command), self.assertRaisesRegex(RuntimeError, 'interrupted controller'):
            worker.run_controller(env, definition, source, 'case')

    def test_only_complete_frozen_project_imports_and_declared_dependencies_are_allowed(self):
        files = {'scene.py': 'import helper\n', 'tasks.py': 'from runtime.scene import Task\n',
                 'expert.py': '', 'helper.py': 'import math\n'}
        self.assertIn('helper', import_locks(files))
        self.assertIn('requests', import_locks(dict(files, **{'expert.py': 'import requests\n'})))
        with self.assertRaisesRegex(ValueError, 'relative'):
            source_files({'sourceFiles': dict(files, **{'../bad.py': ''})})

    def test_exact_asset_lookup_is_not_limited_to_the_first_page(self):
        from runtime.worker import run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / 'legacy/data/resource_closure/asset_catalog.json'
            index.parent.mkdir(parents=True)
            index.write_text(json.dumps({'entries': [
                {'asset_id': f'asset-{number}', 'name': f'asset {number}', 'visual_path': '/missing.glb'}
                for number in range(3)]}))
            result = run({'operation': 'assets', 'root': str(root / 'data'), 'legacyRoot': str(root / 'legacy'),
                          'assetId': 'asset-2', 'limit': 1})
            self.assertEqual([row['id'] for row in result['items']], ['asset-2'])

    def test_task_check_difference_runs_from_frozen_base_to_candidate(self):
        from unittest.mock import patch
        from runtime.scene import World
        from runtime.worker import check
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/project'
            base_directory = project / 'revisions/base'
            candidate_directory = project / 'revisions/candidate'
            base_directory.mkdir(parents=True)
            candidate_directory.mkdir()
            before = World()
            before.box('cube', (.1, .1, .1))
            after = World.restore(before.snapshot())
            after.nodes['cube']['at'] = [1, 0, 0]
            after.box('new_cube', (.1, .1, .1))
            (base_directory / 'scene.json').write_text(json.dumps(before.snapshot()))
            with patch('runtime.worker.frozen', return_value=(project, candidate_directory,
                       {'revision': 'candidate', 'tasks': [], 'sourceDigests': {}}, after)):
                result = check({'projectId': 'project', 'baseRevision': 'base'}, root)
            difference = result['difference']
            self.assertEqual([row['id'] for row in difference['added']], ['new_cube'])
            self.assertEqual(difference['removed'], [])
            self.assertEqual(difference['changed'][0]['before']['at'], [0, 0, 0])
            self.assertEqual(difference['changed'][0]['after']['at'], [1, 0, 0])

    def test_snapshot_input_wins_over_legacy_entry_and_tampering_is_rejected(self):
        import sys
        from runtime.worker import runtime_versions
        files = {'scene.py': '', 'tasks.py': '', 'expert.py': '', 'helper.py': '# frozen\n'}
        self.assertEqual(source_files({'sourceFiles': files, 'sources': {'scene': 'changed'}}), files)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'helper.py').write_text('# changed\n')
            with self.assertRaisesRegex(ValueError, 'source digest'):
                verify_inputs({'runtime': 'same', 'runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
                    'runtimeVersions':runtime_versions(), 'assets': {}, 'sourceDigests': {'helper.py': 'wrong'}}, 'same', path)


class ControllerResourceSandboxTests(unittest.TestCase):
    """SYSTEM_VERIFICATION_ONLY: real bwrap/RPC isolation, with no simulator or physics claims."""
    def setUp(self):
        import shutil
        import sys
        from types import SimpleNamespace
        from runtime.scene import Task, World
        if not sys.platform.startswith('linux') or not shutil.which('bwrap'):
            self.skipTest('resource sandbox verification requires Linux bubblewrap')
        temporary = tempfile.TemporaryDirectory(prefix='system-verification-resource-sandbox-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ('scene.py', 'grade.py', 'base/scene.py', 'base/grade.py'):
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYSTEM_VERIFICATION_ONLY private source')
        world = World(); world.box('object', [.1, .1, .1])
        self.calls = []
        self.env = SimpleNamespace(world=world, time=0, source_directory=self.root, resource_refs={},
            io={'clock':{'physicsRateHz':200, 'controlRateHz':50}, 'ports':{}, 'streams':{}},
            observe=lambda *args, **kwargs: {'time':0}, reset=lambda seed:self.calls.append(('reset', seed)),
            step=lambda values, seconds:self.calls.append(('step', values, seconds)) or {},
            last_result={'success':False, 'terminated':False, 'reward':None, 'details':{}}, truncated=False)
        self.task = Task('sandbox_fixture', bindings={'object':'object'}, rubric={'sourceRef':'grade.py', 'helperRefs':[]})
        self.task.bind(world)

    def test_same_named_base_and_added_resources_are_readable_without_private_sources(self):
        from unittest.mock import patch
        from runtime import worker
        self.env.resource_refs = {'base':'base/dependencies/resources/package/files/model.txt',
                                  'new':'dependencies/resources/package/files/model.txt'}
        for label, name in self.env.resource_refs.items():
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(label.upper()+'_BYTES')
        source = ("from pathlib import Path\n\ndef run(env, task):\n"
            "    resources = env.open()['resources']\n"
            "    assert resources == {'base':'/base-resources/package/files/model.txt','new':'/resources/package/files/model.txt'}\n"
            "    values = {name:Path(path).read_text() for name,path in resources.items()}\n"
            "    assert values == {'base':'BASE_BYTES','new':'NEW_BYTES'}\n"
            "    for path in resources.values():\n"
            "        try:\n            Path(path).write_text('unauthorized')\n"
            "        except OSError:\n            pass\n"
            "        else:\n            raise AssertionError('frozen resource was writable')\n"
            "    private_root = Path("+repr(str(self.root))+")\n"
            "    assert not private_root.exists()\n"
            "    for name in ('scene.py','grade.py','base/scene.py','base/grade.py'):\n"
            "        try:\n            (private_root/name).read_bytes()\n"
            "        except OSError:\n            pass\n"
            "        else:\n            raise AssertionError('private source was readable')\n"
            "    env.step(values)\n")
        command = worker.controller_command
        with patch.object(worker, 'controller_command', side_effect=lambda **options:command(**{**options, 'gpu':False})) as called:
            result = worker.run_controller(self.env, self.task, source, 'expert', max_seconds=30,
                source_files={'behavior.py':source}, source_ref='behavior.py')
        self.assertEqual(called.call_args.kwargs['resources'], self.root/'dependencies/resources')
        self.assertEqual(called.call_args.kwargs['base_resources'], self.root/'base/dependencies/resources')
        self.assertEqual(self.calls, [('reset',0), ('step',{'base':'BASE_BYTES','new':'NEW_BYTES'},.02)])
        self.assertEqual(result['actionCalls'], 1)
        self.assertTrue(result['isolated'])
        for label, name in self.env.resource_refs.items():
            self.assertEqual((self.root/name).read_text(), label.upper()+'_BYTES')

    def test_unknown_and_escaping_resource_paths_are_rejected_before_process_creation(self):
        from unittest.mock import patch
        from runtime import worker
        for name in ('dependencies/resources', 'base/dependencies/resources'):
            (self.root/name).mkdir(parents=True)
        entries = ('unknown/package/file.txt', 'dependencies/resources2/file.txt',
            'dependencies/resources/../../grade.py', 'base/dependencies/resources/../../scene.py',
            '../grade.py', str(self.root/'grade.py'))
        for entry in entries:
            with self.subTest(entry=entry), patch.object(worker.subprocess, 'Popen', side_effect=AssertionError('invalid resource reached process creation')) as process, \
                 patch.object(worker, 'controller_command') as command:
                self.env.resource_refs = {'invalid':entry}
                with self.assertRaisesRegex(ValueError, 'root|resource|artifact'):
                    worker.run_controller(self.env, self.task, 'def run(env, task):\n    pass\n')
                process.assert_not_called(); command.assert_not_called()

    def test_original_resource_mount_stays_compatible_when_no_base_resource_directory_exists(self):
        from unittest.mock import patch
        from runtime import worker
        name = 'dependencies/resources/package/files/model.txt'
        path = self.root/name; path.parent.mkdir(parents=True); path.write_text('ORIGINAL_BYTES')
        self.env.resource_refs = {'original':name}
        source = ("from pathlib import Path\n\ndef run(env, task):\n"
            "    resources = env.open()['resources']\n"
            "    assert resources == {'original':'/resources/package/files/model.txt'}\n"
            "    assert Path(resources['original']).read_text() == 'ORIGINAL_BYTES'\n"
            "    assert not Path('/base-resources').exists()\n"
            "    env.step({})\n")
        command = worker.controller_command
        with patch.object(worker, 'controller_command', side_effect=lambda **options:command(**{**options, 'gpu':False})) as called:
            result = worker.run_controller(self.env, self.task, source, 'expert', max_seconds=30,
                source_files={'behavior.py':source}, source_ref='behavior.py')
        self.assertEqual(called.call_args.kwargs, {'sensors':None, 'resources':self.root/'dependencies/resources', 'gpu':True})
        self.assertEqual(self.calls, [('reset',0), ('step',{},.02)])
        self.assertEqual(result['actionCalls'], 1)


if __name__ == '__main__':
    unittest.main()
