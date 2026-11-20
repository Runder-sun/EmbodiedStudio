"""SYSTEM_VERIFICATION_ONLY: frozen-byte publication contracts; no SDK/scene acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime import worker


class PreviewPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root/'projects/p_preview/revisions/rev_old'
        self.directory.mkdir(parents=True)
        self.payload = {'root': str(self.root), 'projectId': 'p_preview', 'revision': 'rev_old',
                        'requestId': 'preview_attempt', 'operation': 'observe', 'output': 'web_preview'}
        for name, text in {'scene-source.usda': '# SYSTEM_VERIFICATION_ONLY frozen USD bytes',
                           'scene.py': 'raise RuntimeError("must never execute the producer")',
                           'asset.bin': 'SYSTEM_VERIFICATION_ONLY dependency'}.items():
            (self.directory/name).write_text(text)
        audit = {'schema': 'sceneagent-native-stage-audit-v1', 'entry': 'scene-source.usda',
                 'files': {name: worker.digest(self.directory/name) for name in ('scene-source.usda', 'asset.bin')}}
        worker.save(self.directory/'native-stage-audit.json', audit)
        manifest_ref = str((self.directory/'manifest.json').relative_to(self.root))
        self.manifest = {'schema': 'sceneagent-build-v6', 'engine': 'isaacsim', 'physics': 'physx',
            'status': 'completed', 'projectId': 'p_preview', 'revision': 'rev_old', 'runtime': 'old_runtime',
            'publicationJobId': 'j_build', 'artifacts': {'manifest': manifest_ref},
            'runtimeExecution': {'python': sys.executable, 'prefix': sys.prefix}, 'runtimeVersions': {},
            'io': {'schema': 'sceneagent-native-io-v1', 'clock': {'physicsRateHz': 200, 'controlRateHz': 50}, 'ports': {}, 'streams': {}},
            'compiled': {'usd': 'scene-source.usda', 'frozenUsdSha256': worker.digest(self.directory/'scene-source.usda'),
                         'nativeStageAudit': 'native-stage-audit.json', 'bindings': {'objectPaths': {}}},
            'assets': {'asset.bin': worker.digest(self.directory/'asset.bin')},
            'sourceDigests': {'scene.py': worker.digest(self.directory/'scene.py')},
            'frozenArtifactDigests': {name: worker.digest(self.directory/name) for name in ('scene-source.usda', 'native-stage-audit.json')}}
        self.publish()
        self.addCleanup(patch.stopall)
        patch.object(worker, 'runtime_key', return_value='converter').start()
        patch.object(worker, 'runtime_versions', return_value={}).start()
        patch.object(worker, 'progress').start()

    def publish(self):
        worker.save(self.directory/'manifest.json', self.manifest)
        job = self.root/'jobs/j_build'; job.mkdir(parents=True, exist_ok=True)
        worker.save(job/'status.json', {'jobId': 'j_build', 'state': 'completed'})
        worker.save(job/'result.json', {'projectId': 'p_preview', 'revision': 'rev_old',
            'publicationArtifactDigests': {self.manifest['artifacts']['manifest']: worker.digest(self.directory/'manifest.json')}})

    def export(self, source, target, bindings):
        target.write_bytes(b'SYSTEM_VERIFICATION_ONLY derived GLB transport fixture')
        raw = target.with_name('preview-raw.glb'); raw.write_bytes(b'SYSTEM_VERIFICATION_ONLY converter bytes')
        worker.save(target.with_name('preview-evidence.json'), {'sourceUsdSha256': worker.digest(source),
            'previewSha256': worker.digest(target), 'rawGlbSha256': worker.digest(raw),
            'originalBinaryPreservedAsPrefix': True, 'visibleMeshes': 1})

    def derive(self):
        with patch.dict(sys.modules, {'runtime.simulation': SimpleNamespace(export_preview=self.export)}):
            return worker.observe(self.payload, self.root)

    def test_old_runtime_preview_preserves_published_bytes_and_keeps_physical_loading_strict(self):
        before = {path: path.read_bytes() for path in self.directory.iterdir()}
        result = self.derive()
        self.assertEqual(result['source']['manifestSha256'], worker.digest(self.directory/'manifest.json'))
        self.assertEqual(result['revision'], 'rev_old')
        self.assertEqual(result['converterRuntime'], 'converter')
        self.assertEqual(worker.required_files(self.payload, result),
            set(before) | {worker.within(self.root, ref) for ref in result['artifacts'].values()} | {worker.within(self.root, result['artifactRef'])})
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with self.assertRaisesRegex(ValueError, '内核已变化'):
            worker.frozen(self.root, self.payload)

    def test_changed_frozen_sources_and_dependencies_cannot_be_derived_or_published(self):
        for name in ('manifest.json', 'scene-source.usda', 'scene.py', 'asset.bin', 'native-stage-audit.json'):
            with self.subTest(name=name):
                path = self.directory/name; original = path.read_bytes(); path.write_bytes(original+b' ')
                try:
                    with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                        worker._web_preview_inputs(self.payload, self.root)
                finally:
                    path.write_bytes(original)

    def test_parent_rejects_forged_result_paths_bindings_and_modified_outputs(self):
        result = self.derive()
        for key, value in (('projectId', None), ('source', {**result['source'], 'usdSha256': 'f'*64}),
                           ('converterRuntime', 'foreign_converter'),
                           ('artifactRef', 'projects/foreign/receipt.json'),
                           ('artifacts', {**result['artifacts'], 'preview': 'projects/foreign/preview.glb'})):
            changed = {**deepcopy(result), key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'web preview result'):
                worker.required_files(self.payload, changed)
        preview = worker.within(self.root, result['artifacts']['preview']); preview.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'web preview result'):
            worker.required_files(self.payload, result)

    def test_scope_and_native_stage_closure_cannot_be_omitted(self):
        for key, value in (('cameraIds', ['front']), ('nodeId', 'room'), ('episode', 'ep'), ('frame', 0)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'cannot mix'):
                worker._web_preview_inputs({**self.payload, key: value}, self.root)
        self.manifest['compiled']['nativeStageAudit'] = 'unfrozen.json'; self.publish()
        with self.assertRaisesRegex(ValueError, 'audit is not frozen'):
            worker._web_preview_inputs(self.payload, self.root)


if __name__ == '__main__':
    unittest.main()
