import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime.worker import run, publish_asset_result, prepare_asset, generate_asset, asset_source_entry


class AssetWorkerTests(unittest.TestCase):
    def test_asset_lookup_passes_collection_and_joint_requirement_without_dropping_false(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {'operation': 'assets', 'root': directory, 'legacyRoot': directory,
                       'query': 'cabinet', 'limit': 4, 'libraryTier': 'preferred',
                       'collection': 'future-preferred-source'}
            for required in (True, False):
                with self.subTest(required=required), patch('runtime.worker.assets', return_value={'items': []}) as lookup:
                    self.assertEqual(run({**payload, 'requiresJoints': required}), {'items': []})
                    self.assertEqual(lookup.call_args.kwargs,
                                     {'libraryTier': 'preferred', 'collection': 'future-preferred-source',
                                      'requiresJoints': required})

    def test_native_prepare_publication_verifies_declared_files_without_inventing_a_glb(self):
        from runtime.worker import required_files
        from runtime.jobs import write_candidate, verify_candidate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root/'assets/public/model.urdf'
            entry.parent.mkdir(parents=True)
            entry.write_text('<robot name="SYSTEM_VERIFICATION_ONLY"/>')
            payload = {'operation':'prepare','root':directory,'resourceFile':str(entry)}
            report = prepare_asset(payload, root/'legacy', root)
            work = root/'jobs/fixture'
            work.mkdir(parents=True)
            files = required_files(payload, report)
            write_candidate(work, payload, report, files,
                            dict.fromkeys(('records','images','encoder','replicator'), True))
            self.assertEqual(verify_candidate(work, payload, 0, required_files), report)
            self.assertEqual({str(path.relative_to(Path(report['reportPath']).parent)) for path in files},
                             set(report['preparedFiles']))
            Path(report['path']).write_text('truncated')
            with self.assertRaisesRegex(ValueError, 'digest|changed'):
                verify_candidate(work, payload, 0, required_files)

    def test_asset_source_selection_is_checked_at_remote_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {'operation': 'prepare', 'root': directory, 'legacyRoot': directory,
                       'assetId': 'chair', 'url': 'https://example.com/chair.glb'}
            with patch('runtime.assets.prepare', side_effect=AssertionError('dispatched before source validation')):
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    run(payload)

    def test_resource_file_dispatch_freezes_native_package_without_a_mesh_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / 'assets/public/model.urdf'
            entry.parent.mkdir(parents=True)
            entry.write_text('<robot name="SYSTEM_VERIFICATION_ONLY"/>')
            report = prepare_asset({'resourceFile': str(entry)}, root / 'legacy', root)
            self.assertEqual(report['kind'], 'native_files')
            self.assertEqual(Path(report['path']).read_text(), entry.read_text())
            self.assertEqual(report['source']['sourcePaths'], [str(entry.resolve())])
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                prepare_asset({'resourceFile': str(entry), 'assetId': 'other'}, root / 'legacy', root)
            with self.assertRaisesRegex(ValueError, 'dependencyFiles'):
                prepare_asset({'assetId': 'other', 'dependencyFiles': [str(entry)]}, root / 'legacy', root)

    def test_native_prepare_forwards_mdl_roots_into_the_frozen_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = root / 'assets/public/model.usda'; entry.parent.mkdir(parents=True)
            entry.write_text('#usda 1.0')
            module = entry.parent / 'mdl/material.mdl'; module.parent.mkdir()
            module.write_text('mdl 1.7; // SYSTEM_VERIFICATION_ONLY')
            report = prepare_asset({'resourceFile': str(entry), 'dependencyFiles': [str(module)],
                'mdlSearchRoots': [str(module.parent)]}, root / 'legacy', root)
            self.assertEqual(report['inputs']['mdlSearchRoots'], ['files/public/mdl'])
            saved = json.loads(Path(report['reportPath']).read_text())
            frozen = Path(report['reportPath']).parent / saved['inputs']['mdlSearchRoots'][0] / module.name
            self.assertEqual(frozen.read_bytes(), module.read_bytes())
            with self.assertRaisesRegex(ValueError, 'mdlSearchRoots'):
                prepare_asset({'assetId': 'other', 'mdlSearchRoots': []}, root / 'legacy', root)

    def test_isolated_asset_source_can_declare_native_files_in_a_separate_result_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources, output, result = root / 'sources', root / 'output', root / 'result'
            for path in (sources, output, result):
                path.mkdir()
            (sources / 'asset.py').write_text('# isolated fixture')
            declaration = {'entry': 'model.usda', 'files': ['model.usda', 'weights.pt']}
            def build(materials):
                self.assertEqual(materials, {})
                (output / 'model.usda').write_text('#usda 1.0')
                (output / 'weights.pt').write_bytes(b'opaque fixture')
                return declaration
            def paths(value):
                value = str(value)
                for prefix, destination in (('/inputs/sources', sources), ('/output', output), ('/result', result)):
                    if value == prefix or value.startswith(prefix + '/'):
                        return destination / value[len(prefix):].lstrip('/')
                return Path(value)
            with patch('runtime.worker.Path', side_effect=paths), \
                    patch('runtime.worker.program', return_value={'build_asset': build}), \
                    patch('runtime.worker.sys.stdin', io.StringIO(json.dumps({'sourceFile': 'asset.py', 'materials': {}}))):
                asset_source_entry()
            self.assertEqual(json.loads((result / 'output.json').read_text()), {'kind': 'native_files', 'output': declaration})
            self.assertEqual({path.name for path in output.iterdir()}, set(declaration['files']))

    def test_mesh_asset_source_keeps_visual_glb_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources, output, result = root / 'sources', root / 'output', root / 'result'
            for path in (sources, output, result):
                path.mkdir()
            (sources / 'asset.py').write_text('# isolated mesh fixture')
            class Mesh:
                def export(self, destination, file_type):
                    if destination != '/output/visual.glb' or file_type != 'glb':
                        raise AssertionError('mesh export contract changed')
                    (output / 'visual.glb').write_bytes(b'fixture glb')
            def paths(value):
                value = str(value)
                for prefix, destination in (('/inputs/sources', sources), ('/result', result)):
                    if value == prefix or value.startswith(prefix + '/'):
                        return destination / value[len(prefix):].lstrip('/')
                return Path(value)
            with patch('runtime.worker.Path', side_effect=paths), \
                    patch('runtime.worker.program', return_value={'build_asset': lambda materials: Mesh()}), \
                    patch.dict('sys.modules', {'trimesh': SimpleNamespace(Scene=Mesh, Trimesh=Mesh)}), \
                    patch('runtime.worker.sys.stdin', io.StringIO(json.dumps({'sourceFile': 'asset.py', 'materials': {}}))):
                asset_source_entry()
            self.assertEqual((output / 'visual.glb').read_bytes(), b'fixture glb')
            self.assertEqual(json.loads((result / 'output.json').read_text()), {'kind': 'static_mesh'})

    def test_isolated_native_output_is_frozen_and_undeclared_output_keeps_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {'scene.py': '# fixture', 'tasks.py': '# fixture', 'expert.py': '# fixture', 'asset.py': '# fixture'}
            digest = hashlib.sha256(json.dumps(files, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            payload = {'sourceFile': 'asset.py', 'sourceFiles': files, 'sourceHash': digest, 'expectedSourceHash': digest}
            hidden = False
            def child(command, **kwargs):
                output = Path(command[command.index('/output') - 1])
                result = Path(command[command.index('/result') - 1])
                (output / 'model.usda').write_text('#usda 1.0')
                if hidden:
                    (output / 'undeclared.pt').write_bytes(b'fixture')
                (result / 'output.json').write_text(json.dumps({'kind': 'native_files',
                    'output': {'entry': 'model.usda', 'files': ['model.usda']}}))
                return SimpleNamespace(returncode=0, stderr='', stdout='ordinary source logs')
            with patch('runtime.worker.controller_command', side_effect=lambda *args: ['bwrap', 'python', 'worker.py', '--asset-source']), \
                    patch('runtime.worker.subprocess.run', side_effect=child), patch('runtime.worker.progress'), \
                    patch('runtime.assets._versions', return_value={'algorithm': 'fixture'}):
                report = generate_asset(payload, root / 'legacy', root)
                self.assertEqual(report['kind'], 'native_files')
                self.assertTrue(Path(report['path']).is_file())
                self.assertEqual(list((root / 'assets/generated').glob('generating-*')), [])
                hidden = True
                with patch('runtime.assets.generated_cached_report', return_value=None), self.assertRaisesRegex(ValueError, 'declared'):
                    generate_asset(payload, root / 'legacy', root)
            failures = list((root / 'assets/generated').glob('generating-*/failure.json'))
            self.assertEqual(len(failures), 1)
            self.assertEqual(json.loads(failures[0].read_text())['sourceHash'], digest)
            self.assertTrue((failures[0].parent / 'output/undeclared.pt').is_file())

    def test_frozen_asset_package_includes_report_provenance_and_source(self):
        from runtime.worker import copy_asset_dependencies
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'prepared'
            source.mkdir()
            (source/'visual.glb').write_bytes(b'visual')
            (source/'report.json').write_text(json.dumps({'preparedFiles': ['visual.glb', 'report.json', 'source.json']}))
            (source/'source.json').write_text('{"origin":"generated","sourceFiles":{"asset.py":"build_asset"}}')
            with patch('runtime.assets.dependency_paths', return_value=[(source/'visual.glb').resolve()], create=True):
                copy_asset_dependencies(source/'visual.glb', root/'frozen')
            self.assertTrue((root/'frozen/report.json').is_file())
            self.assertTrue((root/'frozen/source.json').is_file())

    def test_failed_thumbnail_preserves_failure_evidence_without_fabricated_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {'path': str(root/'asset.glb'), 'reportPath': str(root/'report.json'),
                      'thumbnail': {'status': 'failed', 'reason': 'render unavailable'}}
            result = publish_asset_result(report, {'projectId': 'p_asset'}, root)
            self.assertEqual(result['thumbnail']['status'], 'failed')
            self.assertNotIn('image', result)
            self.assertTrue((root/result['artifactRef']).is_file())

    def publisher_preview_fixture(self, root):
        package = root/'assets/collections/fixture/chair'
        image = package/'.thumbs/chair.png'
        image.parent.mkdir(parents=True)
        pixels = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6gS8AAAAASUVORK5CYII=')
        image.write_bytes(pixels)
        preview = {'scope': 'per_object', 'type': 'publisher_render', 'file': '.thumbs/chair.png',
                   'sha256': hashlib.sha256(pixels).hexdigest(), 'url': 'https://example.com/pinned/chair.png'}
        item = {'id': 'collection:fixture:chair', 'format': 'native_files', 'packageRoot': str(package),
                'qualityEvidence': {'publisherPreviews': [preview]}, 'visualReady': False}
        return item, preview, pixels

    def test_asset_detail_exposes_pinned_publisher_pixels_without_preparing_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item, preview, pixels = self.publisher_preview_fixture(root)
            with patch('runtime.worker.assets', return_value={'items': [item]}):
                result = run({'operation': 'assets', 'root': directory, 'legacyRoot': directory,
                              'assetId': item['id']})
            selected = result['items'][0]
            self.assertEqual(base64.b64decode(selected['image']['data']), pixels)
            self.assertEqual(selected['preview']['kind'], 'publisher_render')
            self.assertEqual(selected['preview']['sha256'], preview['sha256'])
            self.assertFalse(selected['preview']['systemRendered'])
            self.assertFalse(selected['visualReady'])
            self.assertFalse((root/'assets/prepared').exists())

    def test_asset_detail_does_not_read_unpinned_overview_or_changed_publisher_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item, preview, _ = self.publisher_preview_fixture(root)
            for values in ({'sha256': None}, {'scope': 'source_overview'}):
                with self.subTest(values=values), patch('runtime.worker.assets', return_value={'items': [item]}):
                    item['qualityEvidence']['publisherPreviews'] = [{**preview, **values}]
                    result = run({'operation': 'assets', 'root': directory, 'assetId': item['id']})
                    self.assertNotIn('image', result['items'][0])
            item['qualityEvidence']['publisherPreviews'] = [preview]
            (Path(item['packageRoot'])/preview['file']).write_bytes(b'changed')
            with patch('runtime.worker.assets', return_value={'items': [item]}), self.assertRaisesRegex(ValueError, 'preview.*changed'):
                run({'operation': 'assets', 'root': directory, 'assetId': item['id']})

    def test_native_prepare_can_publish_frozen_publisher_preview_without_calling_it_a_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item, preview, pixels = self.publisher_preview_fixture(root)
            prepared = root/'assets/prepared/fixture'
            image = prepared/'files'/preview['file']
            image.parent.mkdir(parents=True)
            image.write_bytes(pixels)
            report = {'kind': 'native_files', 'path': str(prepared/'files/model.usda'),
                      'reportPath': str(prepared/'report.json'), 'nativeFiles': ['files/'+preview['file']],
                      'frozenDigests': {'files/'+preview['file']: preview['sha256']},
                      'inputs': {'sourceMetadata': item}}
            result = publish_asset_result(report, {'projectId': 'p_preview'}, root)
            self.assertEqual(base64.b64decode(result['image']['data']), pixels)
            self.assertEqual((root/result['thumbnailRef']).read_bytes(), pixels)
            self.assertEqual(result['preview']['kind'], 'publisher_render')
            self.assertFalse(result['preview']['systemRendered'])
            self.assertNotIn('thumbnailPath', report)
            self.assertNotIn('visualReady', result)

    def test_missing_local_publisher_image_keeps_asset_metadata_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item, preview, _ = self.publisher_preview_fixture(root)
            (Path(item['packageRoot'])/preview['file']).unlink()
            with patch('runtime.worker.assets', return_value={'items': [item]}):
                result = run({'operation': 'assets', 'root': directory, 'assetId': item['id']})
            self.assertEqual(result['items'][0]['id'], item['id'])
            self.assertNotIn('image', result['items'][0])


if __name__ == '__main__':
    unittest.main()
