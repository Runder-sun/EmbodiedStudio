"""SYSTEM_VERIFICATION_ONLY evaluator-launch fixtures; no simulator or generated product."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


class FrozenEvaluatorEntryTests(unittest.TestCase):
    device = {'index': 3, 'uuid': 'GPU-30000000-0000-0000-0000-000000000000'}
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1]/'examples/evaluate.py'
        spec = importlib.util.spec_from_file_location('candidate_evaluator_entry', source)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def fixture(self):
        directory = tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        manifest = root/'projects/p_fixture/revisions/rev/manifest.json'
        manifest.parent.mkdir(parents=True)
        worker = root/'runtime/frozen/runtime/worker.py'
        worker.parent.mkdir(parents=True)
        worker.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'type':'progress','stage':'fixture'}))\nprint(json.dumps({'type':'result','result':{'operation':p['operation'],'taskId':p['taskId'],'device':p['device'],'source':p['candidate']['sourceFiles']['candidate.py']}}))\n")
        data = {'schema':'sceneagent-build-v6','runtimeExecution':{'python':sys.executable,'prefix':sys.prefix},
                'runtimeSnapshotRef':'runtime/frozen','projectId':'p_fixture','revision':'rev','seed':0}
        manifest.write_text(json.dumps(data))
        candidate = root/'candidate.py';candidate.write_text('def run(env, task):\n    pass\n')
        return root,manifest,candidate,data

    def test_launches_only_declared_frozen_worker_and_preserves_candidate_source(self):
        root,manifest,candidate,data = self.fixture()
        result = self.module.evaluate(manifest,candidate,'task',root=root,device=self.device)
        self.assertEqual(result,{'operation':'evaluate','taskId':'task','device':self.device,'source':candidate.read_text()})

    def test_custom_worker_budget_requires_support_and_reaches_the_frozen_worker(self):
        root,manifest,candidate,data = self.fixture()
        data['runtimeExecution']['workerTimeoutParameter'] = 'workerTimeoutSeconds'
        manifest.write_text(json.dumps(data))
        worker = root/'runtime/frozen/runtime/worker.py'
        worker.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'type':'result','result':{'budget':p.get('workerTimeoutSeconds'),'present':'workerTimeoutSeconds' in p}}))\n")
        result = self.module.evaluate(manifest,candidate,'task',root=root,device=self.device,worker_timeout_seconds=100000.5)
        self.assertEqual(result, {'budget':100000.5,'present':True})
        default = self.module.evaluate(manifest,candidate,'task',root=root,device=self.device)
        self.assertEqual(default, {'budget':None,'present':False})

    def test_old_worker_and_invalid_budget_are_rejected_before_starting_a_process(self):
        root,manifest,candidate,data = self.fixture()
        with patch.object(self.module.subprocess,'Popen') as launch, self.assertRaisesRegex(ValueError,'workerTimeoutSeconds'):
            self.module.evaluate(manifest,candidate,'task',root=root,device=self.device,worker_timeout_seconds=1800)
        launch.assert_not_called()
        data['runtimeExecution']['workerTimeoutParameter'] = 'workerTimeoutSeconds'
        manifest.write_text(json.dumps(data))
        for value in (True, False, '1800', 0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), patch.object(self.module.subprocess,'Popen') as launch, self.assertRaisesRegex(ValueError,'worker'):
                self.module.evaluate(manifest,candidate,'task',root=root,device=self.device,worker_timeout_seconds=value)
            launch.assert_not_called()

    def test_public_robot_profile_helper_is_transmitted_without_execution(self):
        root,manifest,candidate,data = self.fixture()
        profile = root/'arm.robot.json';profile.write_text('{"configuration":"SYSTEM_VERIFICATION_ONLY"}')
        worker = root/'runtime/frozen/runtime/worker.py'
        worker.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'type':'result','result':p['candidate']['sourceFiles']}))\n")
        result = self.module.evaluate(manifest,candidate,'task',root=root,device=self.device,
                                      helpers=['arm.robot.json'])
        self.assertEqual(result['arm.robot.json'], profile.read_text())
        with self.assertRaises(ValueError):
            self.module.evaluate(manifest,profile,'task',root=root,device=self.device)
        ordinary = root/'private.json';ordinary.write_text('{}')
        with self.assertRaises(ValueError):
            self.module.evaluate(manifest,candidate,'task',root=root,device=self.device,
                                 helpers=['private.json'])

    def test_standalone_evaluation_requires_explicit_assigned_device_before_launch(self):
        root,manifest,candidate,data = self.fixture()
        with patch.object(self.module.subprocess,'Popen') as launch, self.assertRaisesRegex(ValueError, 'GPU'):
            self.module.evaluate(manifest,candidate,'task',root=root)
        launch.assert_not_called()

    def test_old_protocol_or_wrong_prefix_is_rejected_before_launch(self):
        root,manifest,candidate,data = self.fixture()
        for bad in ({**data,'schema':'sceneagent-build-v4'}, {**data,'schema':'sceneagent-build-v5'},
                    {**data,'runtimeExecution':{'python':sys.executable,'prefix':'/wrong-prefix'}}):
            manifest.write_text(json.dumps(bad))
            with patch.object(self.module.subprocess,'Popen') as launch, self.assertRaises(ValueError):
                self.module.evaluate(manifest,candidate,'task',root=root,device=self.device)
            launch.assert_not_called()

    def test_runtime_path_escape_is_rejected(self):
        root,manifest,candidate,data = self.fixture()
        data['runtimeSnapshotRef']='../outside'
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'frozen runtime'):
            self.module.evaluate(manifest,candidate,'task',root=root,device=self.device)

    def test_failed_frozen_process_is_not_a_valid_task_grade(self):
        root,manifest,candidate,data = self.fixture()
        (root/'runtime/frozen/runtime/worker.py').write_text("import sys\nsys.stderr.write('actual evaluator dependency failure')\nsys.exit(7)\n")
        with self.assertRaisesRegex(RuntimeError,'actual evaluator dependency failure'):
            self.module.evaluate(manifest,candidate,'task',root=root,device=self.device)


if __name__ == '__main__':unittest.main()
