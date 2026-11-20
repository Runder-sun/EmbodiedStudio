"""System entry ordering only; no native SDK or robot demonstration is started."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import runtime
from runtime import jobs, worker


class SdkStartupTests(unittest.TestCase):
    def test_material_preflight_checks_json_before_any_usd_import(self):
        import hashlib
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            directory = root/'projects/fixture/revisions/rev'
            directory.mkdir(parents=True)
            source = directory/'material.mdl'; source.write_bytes(b'original')
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            audit = {'schema':'sceneagent-native-stage-audit-v2', 'files':{'material.mdl':checksum}, 'mdl':{'fixture':True}}
            path = directory/'native-stage-audit.json';path.write_text(json.dumps(audit))
            manifest = {'compiled':{'nativeStageAudit':path.name}, 'assets':{'material.mdl':checksum},
                        'frozenArtifactDigests':{path.name:hashlib.sha256(path.read_bytes()).hexdigest()}}
            (directory/'manifest.json').write_text(json.dumps(manifest))
            sdk = ModuleType('runtime.simulation'); calls = []
            sdk.configure_native_materials = lambda folder, document: calls.append((folder, document))
            with patch.dict(sys.modules, {'runtime.simulation':sdk, 'pxr':None}):
                worker.material_preflight({'root':str(root), 'projectId':'fixture', 'revision':'rev'})
                self.assertEqual(calls, [(directory.resolve(), audit)])
                source.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'closure changed'):
                    worker.material_preflight({'root':str(root), 'projectId':'fixture', 'revision':'rev'})

    def invoke(self, operation, **fields):
        events = []
        sdk = ModuleType('runtime.simulation')
        sdk._ensure_app = lambda: events.append('sdk')
        sdk.finish = lambda: events.append('finish') or {'applicationStarted': 'sdk' in events}

        def shutdown(code):
            events.append(('exit', code))
            raise SystemExit(code)
        sdk.shutdown = shutdown

        def run(payload, child):
            events.append('run')
            self.assertTrue(child)
            native = (operation in ('rollout', 'consume') or operation == 'observe' and not payload.get('episode')) and not payload.get('robotSetup')
            if native and 'sdk' not in events:
                raise RuntimeError('frozen USD imported before the native SDK; reproduced ABI order')
            return {'scope': 'startup test fixture'}

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory/'worker-parent.json').write_text(json.dumps({'pid': os.getppid(), 'processToken': 'fixture'}))
            (directory/'worker-request.json').write_text(json.dumps({'operation': operation, **fields}))
            with patch.dict(sys.modules, {'runtime.simulation': sdk}), \
                 patch.object(runtime, 'simulation', sdk, create=True), \
                 patch.object(worker.sys, 'platform', 'darwin'), \
                 patch('runtime.jobs._token', return_value='fixture'), \
                 patch.object(worker, 'run', side_effect=run), \
                 patch.object(worker, 'required_files', side_effect=lambda *_: events.append('verify') or set()), \
                 patch('runtime.jobs.write_candidate', side_effect=lambda *_: events.append('candidate')):
                with self.assertRaises(SystemExit) as exited:
                    worker.kit_entry(directory)
            failure = json.loads((directory/'worker-failure.json').read_text()) if (directory/'worker-failure.json').exists() else None
            return events, exited.exception.code, failure

    def test_native_execution_initializes_sdk_before_frozen_usd_and_keeps_publication_checks(self):
        for operation in ('rollout', 'consume', 'observe'):
            with self.subTest(operation=operation):
                events, code, failure = self.invoke(operation)
                self.assertEqual(code, 0, failure)
                self.assertEqual(events, ['sdk', 'run', 'finish', 'verify', 'candidate', ('exit', 0)])

    def test_recorded_frame_and_non_native_paths_keep_their_no_sdk_startup(self):
        for operation, fields in (('observe', {'episode': 'test_episode'}), ('prepare', {}), ('build', {})):
            with self.subTest(operation=operation):
                events, code, failure = self.invoke(operation, **fields)
                self.assertEqual(code, 0, failure)
                self.assertEqual(events, ['run', 'finish', 'verify', 'candidate', ('exit', 0)])

    def test_new_robot_installation_selects_final_roots_before_starting_the_parent_sdk(self):
        events, code, failure = self.invoke('rollout', robotSetup={'robots':[{'id':'fixture'}]})
        self.assertEqual(code, 0, failure)
        self.assertEqual(events, ['run', 'finish', 'verify', 'candidate', ('exit', 0)])

    def test_sdk_start_failure_does_not_run_or_publish_a_candidate(self):
        sdk = ModuleType('runtime.simulation')
        sdk._ensure_app = lambda: (_ for _ in ()).throw(RuntimeError('native initialization failed'))
        sdk.shutdown = lambda code: (_ for _ in ()).throw(SystemExit(code))
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory/'worker-parent.json').write_text(json.dumps({'pid': os.getppid(), 'processToken': 'fixture'}))
            (directory/'worker-request.json').write_text(json.dumps({'operation': 'rollout'}))
            with patch.dict(sys.modules, {'runtime.simulation': sdk}), \
                 patch.object(runtime, 'simulation', sdk, create=True), \
                 patch.object(worker.sys, 'platform', 'darwin'), \
                 patch('runtime.jobs._token', return_value='fixture'), \
                 patch.object(worker, 'run') as run, patch('runtime.jobs.write_candidate') as publish:
                with self.assertRaises(SystemExit) as exited:
                    worker.kit_entry(directory)
            self.assertEqual(exited.exception.code, 7)
            run.assert_not_called()
            publish.assert_not_called()
            self.assertIn('native initialization failed', (directory/'worker-failure.json').read_text())


if __name__ == '__main__':
    unittest.main()
